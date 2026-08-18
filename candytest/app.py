from __future__ import annotations

import ipaddress
import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

from . import CODEX_EFFORTS, MAX_ROUNDS, PI_EFFORTS
from .cli import cli_availability
from .jobs import JobManager
from .storage import Database, default_data_dir
from .webdav_sync import (
    WebDAVBusyError,
    WebDAVConfigError,
    WebDAVConfigStore,
    WebDAVNoRemoteDataError,
    WebDAVNotConfiguredError,
    WebDAVRequestError,
    WebDAVSyncConflictError,
    WebDAVSyncError,
    WebDAVSyncService,
    WebDAVValidationError,
)


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


def _proxy_url(db: Database) -> str | None:
    proxy = db.proxy_settings()
    return proxy["url"] if proxy["enabled"] else None


def _record_sync_state(app: Flask, operation: str, status: str, *, automatic: bool,
                       result: dict[str, Any] | None = None,
                       error: WebDAVSyncError | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "operation": operation,
        "status": status,
        "automatic": automatic,
        "result": result,
        "error": None if error is None else {"code": error.code, "message": str(error)},
    }
    state = app.extensions["candytest_sync_state"]
    with state["lock"]:
        state["last"] = item
    return item


def sync_runtime_state(app: Flask) -> dict[str, Any] | None:
    state = app.extensions["candytest_sync_state"]
    with state["lock"]:
        last = state["last"]
        return None if last is None else dict(last)


def perform_startup_sync(app: Flask) -> dict[str, Any]:
    """Best-effort startup Pull. Failure is recorded but never blocks startup."""
    store: WebDAVConfigStore = app.extensions["candytest_webdav_store"]
    service: WebDAVSyncService = app.extensions["candytest_webdav"]
    db: Database = app.extensions["candytest_db"]
    try:
        config = store.get()
        if not config["auto_pull_start"]:
            return _record_sync_state(app, "pull", "disabled", automatic=True)
        result = service.pull(proxy_url=_proxy_url(db))
        return _record_sync_state(app, "pull", "completed", automatic=True, result=result)
    except WebDAVSyncError as exc:
        return _record_sync_state(app, "pull", "failed", automatic=True, error=exc)
    except Exception:
        exc = WebDAVSyncError("启动自动 Pull 发生本地错误，已继续使用本地数据")
        return _record_sync_state(app, "pull", "failed", automatic=True, error=exc)


def perform_shutdown_sync(app: Flask) -> dict[str, Any]:
    """Best-effort normal-exit Push; a remote conflict is always skipped."""
    store: WebDAVConfigStore = app.extensions["candytest_webdav_store"]
    service: WebDAVSyncService = app.extensions["candytest_webdav"]
    db: Database = app.extensions["candytest_db"]
    try:
        config = store.get()
        if not config["auto_push_exit"]:
            return _record_sync_state(app, "push", "disabled", automatic=True)
        result = service.push(force=False, proxy_url=_proxy_url(db))
        return _record_sync_state(app, "push", "completed", automatic=True, result=result)
    except WebDAVSyncConflictError as exc:
        return _record_sync_state(app, "push", "skipped", automatic=True, error=exc)
    except WebDAVSyncError as exc:
        return _record_sync_state(app, "push", "failed", automatic=True, error=exc)
    except Exception:
        exc = WebDAVSyncError("退出自动 Push 发生本地错误，已跳过")
        return _record_sync_state(app, "push", "failed", automatic=True, error=exc)


def create_app(data_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=32 * 1024)
    app.json.ensure_ascii = False
    db = Database(data_dir)
    manager = JobManager(db)
    webdav_store = WebDAVConfigStore(db.data_dir)
    webdav = WebDAVSyncService(
        db, webdav_store, manager.reserve_sync, manager.release_sync,
    )
    app.extensions["candytest_db"] = db
    app.extensions["candytest_jobs"] = manager
    app.extensions["candytest_webdav_store"] = webdav_store
    app.extensions["candytest_webdav"] = webdav
    app.extensions["candytest_sync_state"] = {"lock": threading.Lock(), "last": None}

    def sync_mutation_error():
        if manager.sync_reserved():
            return api_error("SYNC_BUSY", "WebDAV 同步进行中，请稍后再修改数据", 409)
        return None

    def webdav_error(exc: WebDAVSyncError):
        if isinstance(exc, WebDAVConfigError):
            status = 400
        elif isinstance(exc, (WebDAVBusyError, WebDAVNotConfiguredError,
                              WebDAVNoRemoteDataError, WebDAVSyncConflictError)):
            status = 409
        elif isinstance(exc, WebDAVValidationError):
            status = 422
        elif isinstance(exc, WebDAVRequestError):
            status = 502
        else:
            status = 500
        return api_error(exc.code, str(exc), status)

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
        conflict = sync_mutation_error()
        if conflict:
            return conflict
        data, error = json_body()
        if error:
            return error
        try:
            item = normalize_proxy(data)
        except ValueError as exc:
            return api_error("INVALID_PROXY", str(exc))
        return jsonify({"proxy": db.save_proxy_settings(item["enabled"], item["url"])})

    @app.get("/api/settings/webdav")
    def get_webdav_settings():
        try:
            settings = webdav_store.public()
        except WebDAVSyncError as exc:
            return webdav_error(exc)
        return jsonify({"webdav": settings, "last_operation": sync_runtime_state(app)})

    @app.put("/api/settings/webdav")
    def update_webdav_settings():
        data, error = json_body()
        if error:
            return error
        try:
            settings = webdav.save_config(data)
        except WebDAVSyncError as exc:
            return webdav_error(exc)
        return jsonify({"webdav": settings})

    @app.post("/api/webdav/test")
    def test_webdav_connection():
        try:
            result = webdav.test_connection(proxy_url=_proxy_url(db))
            _record_sync_state(app, "test", "completed", automatic=False, result=result)
            return jsonify({"status": result})
        except WebDAVSyncError as exc:
            _record_sync_state(app, "test", "failed", automatic=False, error=exc)
            return webdav_error(exc)

    @app.get("/api/webdav/status")
    def webdav_status():
        try:
            status = webdav.connection_status(proxy_url=_proxy_url(db))
        except WebDAVSyncError as exc:
            return webdav_error(exc)
        return jsonify({
            "status": status,
            "last_operation": sync_runtime_state(app),
        })

    @app.post("/api/webdav/push")
    def push_webdav():
        data, error = json_body()
        if error:
            return error
        force = data.get("force", False)
        if not isinstance(force, bool):
            return api_error("INVALID_FORCE", "force 必须为布尔值")
        try:
            result = webdav.push(force=force, proxy_url=_proxy_url(db))
            _record_sync_state(app, "push", "completed", automatic=False, result=result)
            return jsonify({"result": result})
        except WebDAVSyncError as exc:
            _record_sync_state(app, "push", "failed", automatic=False, error=exc)
            return webdav_error(exc)

    @app.post("/api/webdav/pull")
    def pull_webdav():
        data, error = json_body()
        if error:
            return error
        if data.get("confirm") is not True:
            return api_error("CONFIRM_REQUIRED", "Pull 会无备份覆盖本地全部数据，请明确确认")
        try:
            result = webdav.pull(proxy_url=_proxy_url(db))
            _record_sync_state(app, "pull", "completed", automatic=False, result=result)
            return jsonify({"result": result})
        except WebDAVSyncError as exc:
            _record_sync_state(app, "pull", "failed", automatic=False, error=exc)
            return webdav_error(exc)

    @app.get("/api/gateways")
    def get_gateways():
        return jsonify({"gateways": db.gateways()})

    @app.post("/api/gateways")
    def create_gateway():
        conflict = sync_mutation_error()
        if conflict:
            return conflict
        data, error = json_body()
        if error: return error
        try:
            item = normalize_gateway(data, True)
        except ValueError as exc:
            return api_error("INVALID_GATEWAY", str(exc))
        return jsonify({"gateway": db.create_gateway(item)}), 201

    @app.put("/api/gateways/<int:gateway_id>")
    def update_gateway(gateway_id: int):
        conflict = sync_mutation_error()
        if conflict:
            return conflict
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
        conflict = sync_mutation_error()
        if conflict:
            return conflict
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
        conflict = sync_mutation_error()
        if conflict:
            return conflict
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
    app = create_app()
    perform_startup_sync(app)
    try:
        serve(app, host=host, port=port, threads=8)
    finally:
        perform_shutdown_sync(app)


if __name__ == "__main__":
    main()
