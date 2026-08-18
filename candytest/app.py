from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

from . import CODEX_EFFORTS, MAX_ROUNDS, PI_EFFORTS
from .cli import cli_availability
from .jobs import JobManager
from .storage import Database, default_data_dir


def api_error(code: str, message: str, status: int = 400):
    return jsonify({"error": {"code": code, "message": message}}), status


def json_body() -> tuple[dict[str, Any] | None, Any | None]:
    if not request.is_json:
        return None, api_error("INVALID_CONTENT_TYPE", "请求必须使用 application/json", 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, api_error("INVALID_JSON", "请求 JSON 格式无效")
    return data, None


def text(data: dict[str, Any], field: str, *, required: bool = True, limit: int = 1000) -> str | None:
    value = data.get(field)
    if value is None:
        if not required:
            return None
        raise ValueError(f"{field} 必须是长度合理的非空文本")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{field} 必须是长度合理的非空文本")
    value = value.strip()
    if not value:
        if not required:
            return None
        raise ValueError(f"{field} 必须是长度合理的非空文本")
    return value


def normalize_gateway(data: dict[str, Any], require_key: bool) -> dict[str, Any]:
    try:
        name = text(data, "name", limit=100)
        base_url = text(data, "base_url", limit=2000)
        model = text(data, "model", limit=300)
        api_key = text(data, "api_key", required=require_key, limit=2000)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    parsed = urlparse(base_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url 必须是有效的 http:// 或 https:// Responses API 地址")
    # Preserve a supplied path (including /v1) but eliminate redundant trailing slashes.
    base_url = (base_url or "").rstrip("/")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须为布尔值")
    return {"name": name, "base_url": base_url, "api_key": api_key, "model": model, "enabled": int(enabled)}


def normalize_proxy(data: dict[str, Any]) -> dict[str, Any]:
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须为布尔值")
    raw_url = data.get("url", "")
    if not isinstance(raw_url, str) or len(raw_url) > 2000:
        raise ValueError("代理 URL 必须是长度合理的文本")
    proxy_url = raw_url.strip().rstrip("/")
    if enabled and not proxy_url:
        raise ValueError("启用代理时必须填写 Clash HTTP 代理 URL")
    if proxy_url:
        parsed = urlparse(proxy_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("代理 URL 端口无效") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or port is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("代理 URL 必须为 http://主机:端口，且不能包含认证、路径或查询参数")
    return {"enabled": enabled, "url": proxy_url}


def configured_host() -> str:
    host = os.environ.get("CANDYTEST_HOST", "127.0.0.1")
    # The placeholder used by earlier builds is not a bindable address.
    # Keep it tolerated for existing environments, but use a real loopback host.
    if host == "[" + "IP]":
        host = "127.0.0.1"
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("CANDYTEST_HOST 必须是回环 IP 地址（例如 127.0.0.1 或 ::1）") from exc
    return host


def configured_port() -> int:
    raw = os.environ.get("CANDYTEST_PORT", "8765")
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("CANDYTEST_PORT 必须是 1-65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("CANDYTEST_PORT 必须是 1-65535 的整数")
    return port


def create_app(data_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=32 * 1024)
    app.json.ensure_ascii = False
    db = Database(data_dir)
    manager = JobManager(db)
    app.extensions["candytest_db"] = db
    app.extensions["candytest_jobs"] = manager

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/runtime")
    def runtime():
        return jsonify({"engines": cli_availability(), "defaults": {"rounds": 5, "reasoning_effort": "low",
                       "mode": "parallel", "timeout_seconds": 300}, "data_dir": str(db.data_dir)})

    @app.get("/api/settings/proxy")
    def get_proxy_settings():
        return jsonify({"proxy": db.proxy_settings()})

    @app.put("/api/settings/proxy")
    def update_proxy_settings():
        data, error = json_body()
        if error:
            return error
        try:
            item = normalize_proxy(data)
        except ValueError as exc:
            return api_error("INVALID_PROXY", str(exc))
        return jsonify({"proxy": db.save_proxy_settings(item["enabled"], item["url"])})

    @app.get("/api/gateways")
    def get_gateways():
        return jsonify({"gateways": db.gateways()})

    @app.post("/api/gateways")
    def create_gateway():
        data, error = json_body()
        if error: return error
        try:
            item = normalize_gateway(data, True)
        except ValueError as exc:
            return api_error("INVALID_GATEWAY", str(exc))
        return jsonify({"gateway": db.create_gateway(item)}), 201

    @app.put("/api/gateways/<int:gateway_id>")
    def update_gateway(gateway_id: int):
        data, error = json_body()
        if error: return error
        try:
            item = normalize_gateway(data, False)
        except ValueError as exc:
            return api_error("INVALID_GATEWAY", str(exc))
        saved = db.update_gateway(gateway_id, item)
        if not saved: return api_error("GATEWAY_NOT_FOUND", "中转站不存在或已删除", 404)
        return jsonify({"gateway": saved})

    @app.delete("/api/gateways/<int:gateway_id>")
    def delete_gateway(gateway_id: int):
        if not db.delete_gateway(gateway_id):
            return api_error("GATEWAY_NOT_FOUND", "中转站不存在或已删除", 404)
        return jsonify({"ok": True})

    @app.post("/api/jobs")
    def create_job():
        data, error = json_body()
        if error: return error
        engine = data.get("engine")
        if engine not in {"pi", "codex"}:
            return api_error("INVALID_ENGINE", "引擎必须为 pi 或 Codex")
        if not cli_availability()[engine]:
            return api_error("CLI_UNAVAILABLE", f"{engine} CLI 未安装或不在 PATH 中", 409)
        mode = data.get("mode", "parallel")
        if mode not in {"serial", "parallel"}:
            return api_error("INVALID_MODE", "测试模式必须为 serial 或 parallel")
        rounds = data.get("rounds", 5)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= MAX_ROUNDS:
            return api_error("INVALID_ROUNDS", f"每站轮数必须是 1 到 {MAX_ROUNDS} 的整数")
        effort = data.get("reasoning_effort", "low")
        allowed = PI_EFFORTS if engine == "pi" else CODEX_EFFORTS
        if effort not in allowed:
            return api_error("INVALID_REASONING", f"{engine} 不支持该 reasoning effort")
        override = data.get("model_override")
        if override is not None:
            if not isinstance(override, str) or len(override.strip()) > 300:
                return api_error("INVALID_MODEL", "模型覆盖值必须是长度合理的文本")
            override = override.strip() or None
        gateway_ids = data.get("gateway_ids")
        if (not isinstance(gateway_ids, list) or not gateway_ids or any(not isinstance(x, int) or isinstance(x, bool) for x in gateway_ids)
                or len(set(gateway_ids)) != len(gateway_ids)):
            return api_error("INVALID_GATEWAYS", "请至少选择一个不重复的中转站")
        gateways = db.gateway_records(gateway_ids)
        if len(gateways) != len(gateway_ids):
            return api_error("INVALID_GATEWAYS", "所选中转站不存在、已删除或未启用")
        proxy = db.proxy_settings()
        proxy_url = proxy["url"] if proxy["enabled"] else None
        try:
            job_id = manager.start(engine, mode, rounds, effort, override, gateways, proxy_url)
        except RuntimeError as exc:
            return api_error("JOB_CONFLICT", str(exc), 409)
        return jsonify({"job_id": job_id, "status": "queued"}), 201

    @app.get("/api/jobs/current")
    def current_job():
        job_id = db.active_job() or db.latest_job()
        return jsonify({"job": db.job(job_id) if job_id else None})

    @app.get("/api/jobs/<int:job_id>")
    def job(job_id: int):
        result = db.job(job_id)
        if not result: return api_error("JOB_NOT_FOUND", "测试任务不存在", 404)
        return jsonify({"job": result})

    @app.post("/api/jobs/<int:job_id>/cancel")
    def cancel_job(job_id: int):
        result = db.job(job_id)
        if not result:
            return api_error("JOB_NOT_FOUND", "测试任务不存在", 404)
        if result["status"] not in {"queued", "running", "cancelling"}:
            return api_error("JOB_NOT_ACTIVE", "该测试任务已经结束，无法中断", 409)
        if not manager.cancel(job_id):
            return api_error("JOB_NOT_ACTIVE", "该测试任务不在当前进程中运行", 409)
        return jsonify({"job_id": job_id, "status": "cancelling"})

    @app.get("/api/history")
    def history():
        return jsonify(db.history())

    @app.delete("/api/history")
    def clear_history():
        data, error = json_body()
        if error: return error
        if data.get("confirm") is not True:
            return api_error("CONFIRM_REQUIRED", "请确认清空历史记录")
        if db.active_job() is not None:
            return api_error("JOB_CONFLICT", "测试任务运行时不能清空历史", 409)
        db.clear_history()
        return jsonify({"ok": True})

    @app.errorhandler(413)
    def too_large(_):
        return api_error("REQUEST_TOO_LARGE", "请求内容过大", 413)

    return app


def main() -> None:
    from waitress import serve

    host, port = configured_host(), configured_port()
    serve(create_app(), host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
