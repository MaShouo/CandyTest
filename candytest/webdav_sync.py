"""WebDAV complete SQLite-mirror primitives for CandyTest.

This module intentionally has no Flask/UI dependency.  It synchronizes one
consistent SQLite backup at a time; ``webdav.json`` is a device-local sidecar
and is never part of the mirrored database.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.auth import AuthBase

from .storage import CURRENT_SCHEMA_VERSION, MIGRATABLE_SCHEMA_VERSIONS, Database

MAX_TRANSFER_BYTES = 500 * 1024 * 1024
SIDECAR_VERSION = 2
MANIFEST_FILENAME = "manifest.json"
SNAPSHOT_FILENAME = "candytest.sqlite3"
MANIFEST_FORMAT = "candytest-sqlite-mirror"
MANIFEST_VERSION = 1
_REQUEST_TIMEOUT = (10, 60)  # connect, read seconds
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WebDAVSyncError(RuntimeError):
    code = "WEBDAV_ERROR"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)

    @property
    def message(self) -> str:
        return "WebDAV 同步失败"


class WebDAVConfigError(WebDAVSyncError):
    code = "INVALID_WEBDAV_CONFIG"

    @property
    def message(self) -> str:
        return "WebDAV 配置无效"


class WebDAVNotConfiguredError(WebDAVSyncError):
    code = "WEBDAV_NOT_CONFIGURED"

    @property
    def message(self) -> str:
        return "尚未配置 WebDAV"


class WebDAVBusyError(WebDAVSyncError):
    code = "WEBDAV_BUSY"

    @property
    def message(self) -> str:
        return "已有测试或同步操作正在进行"


class WebDAVNoRemoteDataError(WebDAVSyncError):
    code = "WEBDAV_NO_REMOTE_DATA"

    @property
    def message(self) -> str:
        return "远端没有可拉取的数据"


class WebDAVSyncConflictError(WebDAVSyncError):
    code = "WEBDAV_CONFLICT"

    @property
    def message(self) -> str:
        return "远端数据已变化，请先拉取或使用强制推送"


class WebDAVValidationError(WebDAVSyncError):
    code = "WEBDAV_INVALID_DATA"

    @property
    def message(self) -> str:
        return "远端同步数据校验失败"


class _Utf8BasicAuth(AuthBase):
    """Send Basic credentials without requests' latin-1-only encoder."""

    def __init__(self, username: str, password: str) -> None:
        self.value = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

    def __call__(self, request):
        request.headers["Authorization"] = self.value
        return request


class WebDAVLocalDatabaseError(WebDAVSyncError):
    code = "WEBDAV_LOCAL_DATABASE_FAILED"

    @property
    def message(self) -> str:
        return "本地数据库快照操作失败"


class WebDAVRequestError(WebDAVSyncError):
    """A safe transport error: it never includes a response body or URL auth."""

    code = "WEBDAV_REQUEST_FAILED"

    def __init__(self, status_code: int | None = None, message: str | None = None) -> None:
        self.status_code = status_code
        if status_code is not None:
            safe = f"WebDAV 请求失败（HTTP {status_code}）"
        else:
            safe = message or "无法连接 WebDAV 服务"
        super().__init__(safe)


def _is_uuid4(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID4_RE.fullmatch(value))


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _normalise_server_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise WebDAVConfigError("WebDAV 服务地址不能为空")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        # Accessing .port performs strict validation for malformed values such
        # as ``https://host:not-a-port``.
        _ = parsed.port
    except ValueError as exc:
        raise WebDAVConfigError("WebDAV 服务地址无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise WebDAVConfigError("WebDAV 服务地址仅支持 http:// 或 https://")
    if parsed.username is not None or parsed.password is not None:
        raise WebDAVConfigError("WebDAV 服务地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise WebDAVConfigError("WebDAV 服务地址不能包含查询参数或片段")
    # Keep a server supplied path, but avoid changing its encoded form except
    # for a redundant final slash.  Remote path components are quoted below.
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _normalise_remote_path(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or len(value) > 1000:
        raise WebDAVConfigError("WebDAV 远端目录无效")
    raw = value.strip().strip("/")
    if not raw:
        raise WebDAVConfigError("WebDAV 远端目录不能为空")
    parts = raw.split("/")
    if any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise WebDAVConfigError("WebDAV 远端目录不能包含空路径、.、.. 或反斜杠")
    return "/".join(parts), tuple(parts)


def _join_url(base: str, *segments: str) -> str:
    quoted = "/".join(quote(str(segment), safe="") for segment in segments)
    return f"{base.rstrip('/')}/{quoted}" if quoted else base.rstrip("/")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WebDAVConfigStore:
    """Read/write the local credential sidecar without ever exposing its password."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "webdav.json"
        self._lock = threading.RLock()
        # Remove only interrupted-operation leftovers; never remove webdav.json.
        if self.data_dir.is_dir():
            for pattern in (".webdav-*.tmp", ".candytest-restore-*.sqlite3"):
                for stale in self.data_dir.glob(pattern):
                    try:
                        stale.unlink()
                    except OSError:
                        pass

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "version": SIDECAR_VERSION,
            "server_url": "",
            "remote_path": "",
            "username": "",
            "password": "",
            "device_id": "",
            "last_seen_revision": None,
        }

    @staticmethod
    def _legacy_fields() -> set[str]:
        return {
            "version", "server_url", "remote_path", "username", "password",
            "auto_pull_start", "auto_push_exit", "device_id", "last_seen_revision",
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._defaults()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WebDAVConfigError("无法读取 WebDAV 本地配置") from exc
        if not isinstance(loaded, dict):
            raise WebDAVConfigError("WebDAV 本地配置格式无效")

        version = loaded.get("version")
        if version == 1 and set(loaded) == self._legacy_fields():
            # v1 stored lifecycle switches which no longer exist.  Preserve
            # every connection and mirror-lineage field, then atomically
            # rewrite the sidecar so future reads never retain those switches.
            if (not isinstance(loaded["auto_pull_start"], bool)
                    or not isinstance(loaded["auto_push_exit"], bool)):
                raise WebDAVConfigError("WebDAV 本地配置字段无效")
            loaded = {
                key: loaded[key]
                for key in self._defaults()
                if key != "version"
            } | {"version": SIDECAR_VERSION}
            self._validate_config(loaded)
            self._write(loaded)
        elif version != SIDECAR_VERSION or set(loaded) != set(self._defaults()):
            raise WebDAVConfigError("WebDAV 本地配置格式无效")

        self._validate_config(loaded)
        return loaded

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        for key in ("server_url", "remote_path", "username", "password"):
            if not isinstance(config[key], str):
                raise WebDAVConfigError("WebDAV 本地配置字段无效")
        if (len(config["server_url"]) > 2048 or len(config["remote_path"]) > 1000
                or len(config["username"]) > 500 or len(config["password"]) > 4096):
            raise WebDAVConfigError("WebDAV 本地配置字段过长")
        if config["device_id"] and not _is_uuid4(config["device_id"]):
            raise WebDAVConfigError("WebDAV 设备标识无效")
        if config["last_seen_revision"] is not None and not _is_uuid4(config["last_seen_revision"]):
            raise WebDAVConfigError("WebDAV 同步版本无效")

    def _write(self, config: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(prefix=".webdav-", suffix=".tmp", dir=self.data_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(config, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            # Best effort directory fsync; unsupported on Windows.
            try:
                directory_fd = os.open(self.data_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def get(self) -> dict[str, Any]:
        """Return an internal copy; callers must not serialize this directly."""
        with self._lock:
            return dict(self._read())

    def public(self) -> dict[str, Any]:
        with self._lock:
            config = self._read()
        server_url = _normalise_server_url(config["server_url"]) if config["server_url"] else ""
        remote_path = _normalise_remote_path(config["remote_path"])[0] if config["remote_path"] else ""
        return {
            "configured": bool(server_url and remote_path and config["username"] and config["password"]),
            "server_url": server_url,
            "remote_path": remote_path,
            "username": config["username"],
            "password_saved": bool(config["password"]),
            "device_id": config["device_id"] or None,
            "last_seen_revision": config["last_seen_revision"],
        }

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist an update; blank password deliberately retains it."""
        if not isinstance(values, dict):
            raise WebDAVConfigError()
        with self._lock:
            previous = self._read()
            server_url = _normalise_server_url(values.get("server_url", previous["server_url"]))
            remote_path, _ = _normalise_remote_path(values.get("remote_path", previous["remote_path"]))
            username = values.get("username", previous["username"])
            if not isinstance(username, str) or not username.strip() or len(username) > 500:
                raise WebDAVConfigError("WebDAV 用户名不能为空或过长")
            password_value = values.get("password", previous["password"])
            if not isinstance(password_value, str) or len(password_value) > 4096:
                raise WebDAVConfigError("WebDAV 密码无效")
            password = previous["password"] if not password_value.strip() else password_value
            if not password:
                raise WebDAVConfigError("WebDAV 密码不能为空")
            device_id = previous["device_id"] or _new_uuid()
            identity_changed = (
                previous["server_url"], previous["remote_path"], previous["username"]
            ) != (server_url, remote_path, username.strip())
            config = {
                "version": SIDECAR_VERSION,
                "server_url": server_url,
                "remote_path": remote_path,
                "username": username.strip(),
                "password": password,
                "device_id": device_id,
                # A different endpoint/user is a different mirror lineage.
                # Password rotation alone deliberately keeps sync state.
                "last_seen_revision": None if identity_changed else previous["last_seen_revision"],
            }
            self._write(config)
        return self.public()

    # Explicit aliases make the intended API discoverable to route handlers.
    update = save

    def set_last_seen_revision(self, revision: str | None) -> None:
        if revision is not None and not _is_uuid4(revision):
            raise WebDAVConfigError("WebDAV 同步版本无效")
        with self._lock:
            config = self._read()
            if not config["device_id"]:
                config["device_id"] = _new_uuid()
            config["last_seen_revision"] = revision
            self._write(config)


class WebDAVClient:
    """Small WebDAV client with bounded responses and no ambient proxy/auth."""

    def __init__(self, config: dict[str, Any], proxy_url: str | None = None) -> None:
        self.server_url = config["server_url"]
        self.remote_path, parts = _normalise_remote_path(config["remote_path"])
        self._remote_parts = parts
        self.root_url = _join_url(self.server_url, *parts)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = _Utf8BasicAuth(config["username"], config["password"])
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def close(self) -> None:
        self.session.close()

    def url(self, *segments: str) -> str:
        return _join_url(self.root_url, *segments)

    def _request(self, method: str, url: str, *, stream: bool = False, data: Any = None,
                 headers: dict[str, str] | None = None, allow: tuple[int, ...] = (200, 201, 204, 207)) -> requests.Response:
        try:
            response = self.session.request(method, url, data=data, headers=headers, stream=stream,
                                            timeout=_REQUEST_TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            raise WebDAVRequestError(message="无法连接 WebDAV 服务") from exc
        if response.status_code not in allow:
            response.close()
            raise WebDAVRequestError(response.status_code)
        return response

    def propfind(self, url: str, depth: int = 0, *, missing_ok: bool = False) -> bool:
        try:
            response = self._request("PROPFIND", url, headers={"Depth": str(depth)}, allow=(207, 200))
        except WebDAVRequestError as exc:
            if missing_ok and exc.status_code == 404:
                return False
            raise
        response.close()
        return True

    def mkcol(self, url: str) -> bool:
        try:
            response = self._request("MKCOL", url, allow=(201, 405))
        except WebDAVRequestError:
            raise
        created = response.status_code == 201
        response.close()
        return created

    def get_bytes(self, url: str, *, missing_ok: bool = False, max_bytes: int = MAX_TRANSFER_BYTES) -> bytes | None:
        try:
            response = self._request("GET", url, stream=True, allow=(200,))
        except WebDAVRequestError as exc:
            if missing_ok and exc.status_code == 404:
                return None
            raise
        try:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > max_bytes):
                raise WebDAVValidationError("WebDAV 响应大小无效")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise WebDAVValidationError("WebDAV 响应超过大小限制")
                chunks.append(chunk)
            if length is not None and total != int(length):
                raise WebDAVValidationError("WebDAV 响应长度不一致")
            return b"".join(chunks)
        finally:
            response.close()

    def download_file(self, url: str, destination: Path, *, expected_size: int) -> None:
        try:
            response = self._request("GET", url, stream=True, allow=(200,))
        except WebDAVRequestError:
            raise
        try:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) != expected_size):
                raise WebDAVValidationError("远端快照 Content-Length 不匹配")
            if expected_size <= 0 or expected_size > MAX_TRANSFER_BYTES:
                raise WebDAVValidationError("远端快照大小无效")
            total = 0
            with destination.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > expected_size or total > MAX_TRANSFER_BYTES:
                        raise WebDAVValidationError("远端快照超过大小限制")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if total != expected_size:
                raise WebDAVValidationError("远端快照实际大小不匹配")
        finally:
            response.close()

    def put_bytes(self, url: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        if len(data) > MAX_TRANSFER_BYTES:
            raise WebDAVValidationError("上传内容超过大小限制")
        response = self._request("PUT", url, data=data, headers={"Content-Type": content_type}, allow=(200, 201, 204))
        response.close()

    def put_file(self, url: str, path: Path) -> None:
        size = path.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise WebDAVValidationError("上传快照超过大小限制")
        with path.open("rb") as handle:
            response = self._request("PUT", url, data=handle, headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)}, allow=(200, 201, 204))
        response.close()

    def delete(self, url: str, *, missing_ok: bool = True) -> bool:
        try:
            response = self._request("DELETE", url, allow=(200, 202, 204))
        except WebDAVRequestError as exc:
            if missing_ok and exc.status_code == 404:
                return False
            raise
        response.close()
        return True

    def ensure_root_and_revisions(self) -> None:
        # Deliberately do not recursively make remote_path: server_url is the
        # parent and must already exist, so a typo cannot create a tree there.
        if not self.propfind(self.root_url, missing_ok=True):
            self.mkcol(self.root_url)
            if not self.propfind(self.root_url, missing_ok=True):
                raise WebDAVRequestError(message="无法创建 WebDAV 根目录")
        revisions = self.url("revisions")
        if not self.propfind(revisions, missing_ok=True):
            self.mkcol(revisions)
            if not self.propfind(revisions, missing_ok=True):
                raise WebDAVRequestError(message="无法创建 WebDAV revisions 目录")


_MANIFEST_FIELDS = frozenset({
    "format", "version", "revision", "previous_revision", "device_id", "created_at", "sha256", "size", "db_user_version",
})


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise WebDAVValidationError("远端 manifest 字段无效")
    if value["format"] != MANIFEST_FORMAT or value["version"] != MANIFEST_VERSION:
        raise WebDAVValidationError("远端 manifest 格式不兼容")
    if not _is_uuid4(value["revision"]) or not _is_uuid4(value["device_id"]):
        raise WebDAVValidationError("远端 manifest UUID 无效")
    previous = value["previous_revision"]
    if previous is not None and not _is_uuid4(previous):
        raise WebDAVValidationError("远端 manifest previous_revision 无效")
    if not isinstance(value["created_at"], str):
        raise WebDAVValidationError("远端 manifest 时间无效")
    try:
        parsed_time = datetime.fromisoformat(value["created_at"])
    except ValueError as exc:
        raise WebDAVValidationError("远端 manifest 时间无效") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise WebDAVValidationError("远端 manifest 时间必须带时区")
    size = value["size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_TRANSFER_BYTES:
        raise WebDAVValidationError("远端 manifest 快照大小无效")
    if not isinstance(value["sha256"], str) or not _SHA256_RE.fullmatch(value["sha256"]):
        raise WebDAVValidationError("远端 manifest SHA-256 无效")
    db_version = value["db_user_version"]
    if (not isinstance(db_version, int) or isinstance(db_version, bool)
            or db_version not in MIGRATABLE_SCHEMA_VERSIONS | {CURRENT_SCHEMA_VERSION}):
        raise WebDAVValidationError("远端数据库版本不兼容")
    return dict(value)


def parse_manifest(raw: bytes) -> dict[str, Any]:
    if len(raw) > 64 * 1024:
        raise WebDAVValidationError("远端 manifest 过大")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebDAVValidationError("远端 manifest 不是有效 JSON") from exc
    return validate_manifest(decoded)


class WebDAVSyncService:
    def __init__(self, db: Database, config_store: WebDAVConfigStore,
                 operation_reserver: Callable[[], Any] | None = None,
                 operation_releaser: Callable[[], Any] | None = None) -> None:
        self.db = db
        self.config_store = config_store
        self.operation_reserver = operation_reserver
        self.operation_releaser = operation_releaser
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise WebDAVBusyError("WebDAV 同步进行中，暂时不能修改设置")
        try:
            return self.config_store.save(values)
        finally:
            self._lock.release()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise WebDAVBusyError()
        reserved = False
        try:
            if self.operation_reserver is not None:
                result = self.operation_reserver()
                if result is False:
                    raise WebDAVBusyError()
                reserved = True
            elif self.db.active_job() is not None:
                raise WebDAVBusyError()
            yield
        finally:
            if reserved and self.operation_releaser is not None:
                try:
                    self.operation_releaser()
                except Exception:
                    pass
            self._lock.release()

    def _config(self) -> dict[str, Any]:
        config = self.config_store.get()
        if not (config["server_url"] and config["remote_path"] and config["username"] and config["password"]):
            raise WebDAVNotConfiguredError()
        if not config["device_id"] or not _is_uuid4(config["device_id"]):
            raise WebDAVConfigError("WebDAV 设备标识无效")
        # Re-validate before any transport use, including a manually edited sidecar.
        config["server_url"] = _normalise_server_url(config["server_url"])
        config["remote_path"], _ = _normalise_remote_path(config["remote_path"])
        return config

    @contextmanager
    def _client(self, proxy_url: str | None) -> Iterator[tuple[WebDAVClient, dict[str, Any]]]:
        config = self._config()
        client = WebDAVClient(config, proxy_url)
        try:
            yield client, config
        finally:
            client.close()

    @staticmethod
    def _manifest(client: WebDAVClient) -> dict[str, Any] | None:
        raw = client.get_bytes(client.url(MANIFEST_FILENAME), missing_ok=True, max_bytes=64 * 1024)
        return None if raw is None else parse_manifest(raw)

    @staticmethod
    def _conflicts(local: str | None, remote: str | None) -> bool:
        return local != remote and (local is not None or remote is not None)

    def connection_status(self, proxy_url: str | None = None) -> dict[str, Any]:
        public = self.config_store.public()
        result = {
            **public,
            "reachable": False,
            "remote_revision": None,
            "conflict": False,
            "error": None,
        }
        if not public["configured"]:
            return result
        try:
            with self._client(proxy_url) as (client, _):
                if not client.propfind(client.root_url, missing_ok=True):
                    result["error"] = {"code": "WEBDAV_NO_REMOTE_DATA", "message": "远端根目录尚未创建"}
                    return result
                manifest = self._manifest(client)
                remote_revision = manifest["revision"] if manifest else None
                result.update({
                    "reachable": True,
                    "remote_revision": remote_revision,
                    "conflict": self._conflicts(public["last_seen_revision"], remote_revision),
                })
        except WebDAVSyncError as exc:
            result["error"] = {"code": exc.code, "message": str(exc)}
        return result

    def test_connection(self, proxy_url: str | None = None) -> dict[str, Any]:
        with self._operation():
            with self._client(proxy_url) as (client, _):
                client.ensure_root_and_revisions()
                probe = client.url(f".candytest-probe-{_new_uuid()}")
                payload = b"candytest-webdav-probe-v1\n"
                try:
                    client.put_bytes(probe, payload, content_type="application/octet-stream")
                    received = client.get_bytes(probe, max_bytes=len(payload))
                    if received != payload:
                        raise WebDAVValidationError("WebDAV 探测读写校验失败")
                finally:
                    try:
                        client.delete(probe, missing_ok=True)
                    except WebDAVSyncError:
                        pass
        status = self.connection_status(proxy_url)
        status["tested"] = True
        return status

    def push(self, force: bool = False, proxy_url: str | None = None) -> dict[str, Any]:
        with self._operation():
            with self._client(proxy_url) as (client, config):
                client.ensure_root_and_revisions()
                remote = self._manifest(client)
                local_revision = config["last_seen_revision"]
                remote_revision = remote["revision"] if remote else None
                if not force and self._conflicts(local_revision, remote_revision):
                    raise WebDAVSyncConflictError()

                revision = _new_uuid()
                revision_url = client.url("revisions", revision)
                activated = False
                manifest_written = False
                warnings: list[str] = []
                try:
                    with tempfile.TemporaryDirectory(prefix="candytest-webdav-") as temporary:
                        snapshot = Path(temporary) / SNAPSHOT_FILENAME
                        try:
                            self.db.create_snapshot(snapshot)
                            size = snapshot.stat().st_size
                            if not 0 < size <= MAX_TRANSFER_BYTES:
                                raise WebDAVValidationError("本地数据库快照大小无效")
                            digest = _sha256(snapshot)
                        except WebDAVSyncError:
                            raise
                        except Exception as exc:
                            raise WebDAVLocalDatabaseError() from exc
                        client.mkcol(revision_url)
                        client.put_file(_join_url(revision_url, SNAPSHOT_FILENAME), snapshot)
                        uploaded = Path(temporary) / "uploaded.sqlite3"
                        client.download_file(_join_url(revision_url, SNAPSHOT_FILENAME), uploaded, expected_size=size)
                        if _sha256(uploaded) != digest:
                            raise WebDAVValidationError("上传后的快照 SHA-256 不匹配")
                        try:
                            self.db.validate_snapshot(uploaded)
                        except WebDAVSyncError:
                            raise
                        except Exception as exc:
                            raise WebDAVLocalDatabaseError() from exc
                        manifest = {
                            "format": MANIFEST_FORMAT,
                            "version": MANIFEST_VERSION,
                            "revision": revision,
                            "previous_revision": remote_revision,
                            "device_id": config["device_id"],
                            "created_at": _iso_now(),
                            "sha256": digest,
                            "size": size,
                            "db_user_version": CURRENT_SCHEMA_VERSION,
                        }
                        # Recheck immediately before activation. This cannot be
                        # a perfect CAS on servers without ETag/LOCK support,
                        # but catches remote changes made during the upload.
                        current = self._manifest(client)
                        current_revision = current["revision"] if current else None
                        if not force and current_revision != remote_revision:
                            raise WebDAVSyncConflictError()
                        manifest_data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        client.put_bytes(client.url(MANIFEST_FILENAME), manifest_data, content_type="application/json")
                        # A successful PUT can already be visible to other devices
                        # even if the confirmation GET subsequently fails.  It is no
                        # longer safe to delete this revision in that situation.
                        manifest_written = True
                        confirmed = self._manifest(client)
                        if confirmed != manifest:
                            raise WebDAVValidationError("远端 manifest 回读校验失败")
                        activated = True
                finally:
                    if not activated and not manifest_written:
                        try:
                            client.delete(revision_url, missing_ok=True)
                        except WebDAVSyncError:
                            pass
                self.config_store.set_last_seen_revision(revision)
                if remote_revision and remote_revision != revision:
                    try:
                        client.delete(client.url("revisions", remote_revision), missing_ok=True)
                    except WebDAVSyncError as exc:
                        warnings.append(f"旧版本清理失败：{exc}")
                return {
                    "revision": revision,
                    "device_id": config["device_id"],
                    "created_at": manifest["created_at"],
                    "size": manifest["size"],
                    "warnings": warnings,
                }

    def pull(self, proxy_url: str | None = None) -> dict[str, Any]:
        with self._operation():
            with self._client(proxy_url) as (client, _):
                manifest = self._manifest(client)
                if manifest is None:
                    raise WebDAVNoRemoteDataError()
                with tempfile.TemporaryDirectory(prefix="candytest-webdav-") as temporary:
                    snapshot = Path(temporary) / SNAPSHOT_FILENAME
                    client.download_file(
                        client.url("revisions", manifest["revision"], SNAPSHOT_FILENAME),
                        snapshot,
                        expected_size=manifest["size"],
                    )
                    if _sha256(snapshot) != manifest["sha256"]:
                        raise WebDAVValidationError("远端快照 SHA-256 不匹配")
                    try:
                        self.db.upgrade_snapshot(snapshot, manifest["db_user_version"])
                        self.db.restore_snapshot(snapshot)
                    except WebDAVSyncError:
                        raise
                    except Exception as exc:
                        raise WebDAVLocalDatabaseError() from exc
                self.config_store.set_last_seen_revision(manifest["revision"])
                return {
                    "revision": manifest["revision"],
                    "device_id": manifest["device_id"],
                    "created_at": manifest["created_at"],
                    "size": manifest["size"],
                    "warnings": [],
                }
