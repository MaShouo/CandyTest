from __future__ import annotations

import ipaddress
import json
import math
import os
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from .auth import AuthStateError, ServerAuthStore
from . import CODEX_EFFORTS, DEFAULT_TIMEOUT_SECONDS, MAX_ROUNDS, PI_EFFORTS, QUESTION_DEFAULTS, QUESTION_DEFAULT_MODELS, QUESTION_NAMES, question_prompt
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
    multiplier = data.get("multiplier", 1)
    if (not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool)
            or (isinstance(multiplier, float) and not math.isfinite(multiplier))
            or not 0 <= multiplier <= 1_000_000):
        raise ValueError("multiplier 必须为 0 到 1000000 的有限数字")
    return {"name": name, "base_url": base_url, "api_key": api_key, "model": model,
            "multiplier": multiplier, "enabled": int(enabled)}


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


def configured_deployment() -> str:
    deployment = os.environ.get("CANDYTEST_DEPLOYMENT", "local").strip().lower()
    if deployment not in {"local", "server"}:
        raise RuntimeError("CANDYTEST_DEPLOYMENT 只能为 local 或 server")
    return deployment


def configured_host() -> str:
    deployment = configured_deployment()
    host = os.environ.get("CANDYTEST_HOST", "127.0.0.1" if deployment == "local" else "0.0.0.0")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError("CANDYTEST_HOST 必须是有效的 IP 地址") from exc
    if deployment == "local":
        if not address.is_loopback:
            raise RuntimeError("local 模式下 CANDYTEST_HOST 必须是回环 IP 地址（例如 127.0.0.1 或 ::1）")
    elif address.is_multicast or (address.is_reserved and not address.is_unspecified) or not (
        address.is_unspecified or address.is_loopback or address.is_private or address.is_global
    ):
        raise RuntimeError("server 模式下 CANDYTEST_HOST 必须是未指定、回环、私有或全局 IP，且不能为保留/组播地址")
    return host


def _server_cookie_secure() -> bool:
    raw_secure = os.environ.get("CANDYTEST_COOKIE_SECURE", "1")
    if raw_secure not in {"0", "1"}:
        raise RuntimeError("CANDYTEST_COOKIE_SECURE 只能为 0 或 1")
    return raw_secure == "1"


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _csrf_valid() -> bool:
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return isinstance(expected, str) and isinstance(supplied, str) and secrets.compare_digest(expected, supplied)


class LoginRateLimiter:
    """Small in-process brake for repeated password guessing; no proxy headers trusted."""

    def __init__(self, limit: int = 5, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._attempts: dict[str, tuple[int, float]] = {}

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            count, until = self._attempts.get(key, (0, 0.0))
            if until and now < until:
                return False
            if until:
                self._attempts.pop(key, None)
            return True

    def failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            count, until = self._attempts.get(key, (0, 0.0))
            if until and now < until:
                return
            count += 1
            self._attempts[key] = (count, now + self.window_seconds if count >= self.limit else 0.0)

    def success(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


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


def _record_sync_state(app: Flask, operation: str, status: str, *,
                       result: dict[str, Any] | None = None,
                       error: WebDAVSyncError | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "operation": operation,
        "status": status,
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


def create_app(data_dir: Path | None = None) -> Flask:
    deployment = configured_deployment()
    app_data_dir = data_dir or default_data_dir()
    auth = ServerAuthStore(app_data_dir) if deployment == "server" else None
    cookie_secure = _server_cookie_secure() if deployment == "server" else False
    app = Flask(__name__)
    app.config.update(
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=32 * 1024,
        SECRET_KEY=auth.current()["secret_key"] if auth else secrets.token_urlsafe(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    app.json.ensure_ascii = False
    app.extensions["candytest_deployment"] = deployment
    app.extensions["candytest_auth"] = auth
    app.extensions["candytest_login_limiter"] = LoginRateLimiter()
    db = Database(app_data_dir)
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

    def _login_page(error: str | None = None, status: int = 200):
        if "csrf_token" not in session:
            session["csrf_token"] = _new_csrf_token()
        try:
            default_credentials = auth.public()["default_credentials"] if auth else False
        except AuthStateError:
            default_credentials = False
            error = "认证状态无效，请检查数据目录中的 auth.json。"
            status = 503
        return render_template(
            "login.html", csrf_token=session["csrf_token"], error=error,
            default_credentials=default_credentials,
        ), status

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if ((deployment == "server" and not request.path.startswith("/static/"))
                or request.path == "/login" or request.path == "/healthz"
                or request.path.startswith("/api/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def server_auth_guard():
        if deployment != "server":
            return None
        if request.path in {"/login", "/healthz"} or request.endpoint == "static":
            return None
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return api_error("AUTH_REQUIRED", "请先登录", 401)
            return redirect(url_for("login"))
        try:
            state = auth.current()
        except AuthStateError:
            session.clear()
            if request.path.startswith("/api/"):
                return api_error("AUTH_REQUIRED", "认证状态无效，请联系管理员", 401)
            return redirect(url_for("login"))
        if session.get("auth_revision") != state["revision"]:
            session.clear()
            if request.path.startswith("/api/"):
                return api_error("AUTH_REQUIRED", "登录已失效，请重新登录", 401)
            return redirect(url_for("login"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_valid():
            if request.path.startswith("/api/") or request.path == "/logout" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return api_error("CSRF_FAILED", "请求校验失败，请刷新页面后重试", 403)
            return _login_page("请求校验失败，请刷新页面后重试。", 403)
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if deployment != "server":
            return redirect(url_for("index"))
        if request.method == "GET":
            if session.get("authenticated"):
                try:
                    if session.get("auth_revision") == auth.current()["revision"]:
                        return redirect(url_for("index"))
                except AuthStateError:
                    pass
                session.clear()
            return _login_page()
        if not _csrf_valid():
            return _login_page("请求校验失败，请刷新页面后重试。", 403)
        limiter: LoginRateLimiter = app.extensions["candytest_login_limiter"]
        client_key = request.remote_addr or "unknown"
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        try:
            state = auth.verify(username, password) if limiter.allowed(client_key) else None
        except AuthStateError:
            session.clear()
            return _login_page("认证状态无效，请检查数据目录中的 auth.json。", 503)
        if state is None:
            limiter.failure(client_key)
            return _login_page("用户名或密码错误，或登录尝试过于频繁。", 401)
        limiter.success(client_key)
        session.clear()
        session["authenticated"] = True
        session["auth_revision"] = state["revision"]
        session["csrf_token"] = _new_csrf_token()
        session.permanent = True
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        if deployment != "server":
            return redirect(url_for("index"))
        # The guard validates this CSRF token before this route is entered.
        session.clear()
        return jsonify({"ok": True})

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        try:
            default_credentials = auth.public()["default_credentials"] if auth else False
        except AuthStateError:
            session.clear()
            return _login_page("认证状态无效，请检查数据目录中的 auth.json。", 503)
        return render_template(
            "index.html", deployment=deployment,
            csrf_token=session.get("csrf_token", "") if deployment == "server" else "",
            default_credentials=default_credentials,
        )

    @app.get("/settings")
    def settings():
        try:
            default_credentials = auth.public()["default_credentials"] if auth else False
        except AuthStateError:
            session.clear()
            return _login_page("认证状态无效，请检查数据目录中的 auth.json。", 503)
        return render_template(
            "settings.html", deployment=deployment,
            csrf_token=session.get("csrf_token", "") if deployment == "server" else "",
            default_credentials=default_credentials,
        )

    @app.get("/api/runtime")
    def runtime():
        return jsonify({"engines": cli_availability(), "deployment": deployment,
                       "defaults": {"rounds": 5, "reasoning_effort": "low",
                       "mode": "parallel", "timeout_seconds": DEFAULT_TIMEOUT_SECONDS}, "data_dir": str(db.data_dir)})

    if deployment == "server":
        @app.get("/api/settings/account")
        def get_account_settings():
            try:
                return jsonify({"account": auth.public()})
            except AuthStateError:
                session.clear()
                return api_error("AUTH_STATE_INVALID", "认证状态无效，请检查 auth.json", 503)

        @app.put("/api/settings/account")
        def update_account_settings():
            data, error = json_body()
            if error:
                return error
            current_password = data.get("current_password")
            username = data.get("username")
            new_password = data.get("new_password", "")
            confirmation = data.get("new_password_confirmation")
            if (not isinstance(current_password, str) or not current_password or len(current_password) > 4096
                    or not isinstance(username, str) or not isinstance(new_password, str)
                    or len(new_password) > 4096):
                return api_error("INVALID_ACCOUNT", "请填写当前密码、有效用户名和长度合理的新密码")
            username = username.strip()
            if not username or len(username) > 200:
                return api_error("INVALID_ACCOUNT", "用户名不能为空，且不能超过 200 个字符")
            if confirmation is not None and (not isinstance(confirmation, str) or confirmation != new_password):
                return api_error("INVALID_ACCOUNT", "两次输入的新密码不一致")
            try:
                account = auth.change(
                    current_password=current_password, username=username,
                    new_password=new_password if new_password else None,
                )
            except PermissionError:
                return api_error("CURRENT_PASSWORD_INCORRECT", "当前密码不正确", 403)
            except AuthStateError:
                session.clear()
                return api_error("AUTH_STATE_INVALID", "认证状态无效，请检查 auth.json", 503)
            except ValueError as exc:
                return api_error("INVALID_ACCOUNT", str(exc))
            session["auth_revision"] = account.pop("revision")
            return jsonify({"account": account})

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
            _record_sync_state(app, "test", "completed", result=result)
            return jsonify({"status": result})
        except WebDAVSyncError as exc:
            _record_sync_state(app, "test", "failed", error=exc)
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
            _record_sync_state(app, "push", "completed", result=result)
            return jsonify({"result": result})
        except WebDAVSyncError as exc:
            _record_sync_state(app, "push", "failed", error=exc)
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
            _record_sync_state(app, "pull", "completed", result=result)
            return jsonify({"result": result})
        except WebDAVSyncError as exc:
            _record_sync_state(app, "pull", "failed", error=exc)
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

    @app.put("/api/gateways/reorder")
    def reorder_gateways():
        conflict = sync_mutation_error()
        if conflict:
            return conflict
        data, error = json_body()
        if error:
            return error
        gateway_ids = data.get("gateway_ids")
        if (not isinstance(gateway_ids, list)
                or any(not isinstance(item, int) or isinstance(item, bool) for item in gateway_ids)
                or len(set(gateway_ids)) != len(gateway_ids)):
            return api_error("INVALID_GATEWAYS", "排序必须包含不重复的中转站 ID")
        if not db.reorder_gateways(gateway_ids):
            return api_error("INVALID_GATEWAYS", "排序必须包含全部未删除的中转站")
        return jsonify({"gateway_ids": gateway_ids})

    @app.delete("/api/gateways/<int:gateway_id>")
    def delete_gateway(gateway_id: int):
        conflict = sync_mutation_error()
        if conflict:
            return conflict
        raw_delete_history = request.args.get("delete_history", "0")
        if raw_delete_history not in {"0", "1"}:
            return api_error("INVALID_DELETE_HISTORY", "delete_history 必须为 0 或 1")
        try:
            changed, deleted_runs = db.delete_gateway(gateway_id, raw_delete_history == "1")
        except RuntimeError as exc:
            return api_error("JOB_CONFLICT", str(exc), 409)
        if not changed:
            return api_error("GATEWAY_NOT_FOUND", "中转站不存在或已删除", 404)
        return jsonify({"ok": True, "deleted_runs": deleted_runs})

    @app.get("/api/questions/prompt")
    def get_question_prompt():
        question_id = request.args.get("question_id", "candy")
        if question_id not in QUESTION_NAMES:
            return api_error("INVALID_QUESTION", "请选择有效题目")
        candy_format = request.args.get("random_candy_format", "false")
        if candy_format not in {"false", "true", "original"}:
            return api_error("INVALID_RANDOM_FORMAT", "请选择有效的糖果题格式")
        prompt, _ = question_prompt(
            question_id, "original" if candy_format == "original" else candy_format == "true",
        )
        response = jsonify({"prompt": prompt})
        response.headers["Cache-Control"] = "no-store"
        return response

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
        question_id = data.get("question_id", "candy")
        if not isinstance(question_id, str) or question_id not in QUESTION_NAMES:
            return api_error("INVALID_QUESTION", "请选择有效题目")
        random_candy_format = data.get("random_candy_format", False)
        if not isinstance(random_candy_format, bool) and random_candy_format != "original":
            return api_error("INVALID_RANDOM_FORMAT", "请选择有效的糖果题格式")
        default_rounds, default_effort = QUESTION_DEFAULTS[question_id]
        rounds = data.get("rounds", default_rounds)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= MAX_ROUNDS:
            return api_error("INVALID_ROUNDS", f"每站轮数必须是 1 到 {MAX_ROUNDS} 的整数")
        effort = data.get("reasoning_effort", default_effort)
        allowed = PI_EFFORTS if engine == "pi" else CODEX_EFFORTS
        if effort not in allowed:
            return api_error("INVALID_REASONING", f"{engine} 不支持该 reasoning effort")
        override = data.get("model_override", QUESTION_DEFAULT_MODELS.get(question_id))
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
            job_id = manager.start(
                engine, mode, rounds, effort, override, gateways, proxy_url, question_id,
                random_candy_format=random_candy_format,
            )
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

    @app.delete("/api/history/gateways/<int:gateway_id>")
    def clear_gateway_history(gateway_id: int):
        conflict = sync_mutation_error()
        if conflict:
            return conflict
        data, error = json_body()
        if error: return error
        if data.get("confirm") is not True:
            return api_error("CONFIRM_REQUIRED", "请确认删除该中转站的历史记录")
        if db.active_job() is not None:
            return api_error("JOB_CONFLICT", "测试任务运行时不能删除历史", 409)
        deleted = db.clear_gateway_history(gateway_id)
        if not deleted:
            return api_error("HISTORY_NOT_FOUND", "该中转站没有可删除的历史记录", 404)
        return jsonify({"ok": True, "deleted": deleted})

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
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
