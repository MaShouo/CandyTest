from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash


_AUTH_FILE = "auth.json"
_AUTH_KEYS = frozenset({"username", "password_hash", "secret_key", "revision"})


class AuthStateError(RuntimeError):
    """Server authentication state cannot be safely used."""


def _validate_username(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise AuthStateError("管理员用户名无效")
    return value


def _validate_hash(value: object) -> str:
    if (not isinstance(value, str) or not value or value.count("$") < 2
            or not value.startswith(("scrypt:", "pbkdf2:"))):
        raise AuthStateError("管理员密码哈希无效")
    try:
        # Werkzeug parses the hash before comparing it.  The empty password is
        # only a parser input and is never persisted.
        check_password_hash(value, "")
    except (TypeError, ValueError) as exc:
        raise AuthStateError("管理员密码哈希无效") from exc
    return value


def _validate_secret(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
        raise AuthStateError("会话密钥无效")
    return value


def _validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _AUTH_KEYS:
        raise AuthStateError("认证状态文件格式无效")
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AuthStateError("认证状态文件版本无效")
    return {
        "username": _validate_username(value.get("username")),
        "password_hash": _validate_hash(value.get("password_hash")),
        "secret_key": _validate_secret(value.get("secret_key")),
        "revision": revision,
    }


def _legacy_state() -> dict[str, Any]:
    """Build an initial state from optional, old environment credentials."""
    username = os.environ.get("CANDYTEST_ADMIN_USERNAME", "")
    password_hash = os.environ.get("CANDYTEST_ADMIN_PASSWORD_HASH", "")
    password_hash_b64 = os.environ.get("CANDYTEST_ADMIN_PASSWORD_HASH_B64", "")
    secret_key = os.environ.get("CANDYTEST_SECRET_KEY", "")

    if password_hash and password_hash_b64:
        raise AuthStateError("管理员密码哈希只能配置一种格式")
    if password_hash_b64:
        try:
            password_hash = base64.b64decode(password_hash_b64, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise AuthStateError("CANDYTEST_ADMIN_PASSWORD_HASH_B64 格式无效") from exc

    supplied_credentials = bool(username or password_hash)
    if supplied_credentials and (not username or not password_hash):
        raise AuthStateError("旧版管理员用户名和密码哈希必须同时配置")
    if supplied_credentials:
        username = _validate_username(username)
        password_hash = _validate_hash(password_hash)
    else:
        username = "admin"
        password_hash = generate_password_hash("admin")

    if secret_key:
        secret_key = _validate_secret(secret_key)
    else:
        # token_urlsafe(48) has substantially more than 32 bytes of entropy.
        secret_key = secrets.token_urlsafe(48)
    return {"username": username, "password_hash": password_hash,
            "secret_key": secret_key, "revision": 1}


class ServerAuthStore:
    """Device-local, durable credentials separate from mirrored SQLite data."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / _AUTH_FILE
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        with self._lock:
            if self.path.exists() or self.path.is_symlink():
                self._read()
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            else:
                self._write(_legacy_state())

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > 128 * 1024:
                raise AuthStateError("认证状态文件不安全或格式无效")
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except AuthStateError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthStateError("认证状态文件无法读取或已损坏") from exc
        return _validate_state(state)

    def _write(self, state: dict[str, Any]) -> None:
        state = _validate_state(state)
        temporary = self.data_dir / f"{_AUTH_FILE}.tmp-{secrets.token_hex(12)}"
        payload = (json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
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
            self._fsync_dir(self.data_dir)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise AuthStateError("认证状态文件无法安全保存") from exc

    def current(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def public(self) -> dict[str, Any]:
        state = self.current()
        return {
            "username": state["username"],
            "default_credentials": (
                state["username"] == "admin" and check_password_hash(state["password_hash"], "admin")
            ),
        }

    def verify(self, username: str, password: str) -> dict[str, Any] | None:
        state = self.current()
        if username == state["username"] and check_password_hash(state["password_hash"], password):
            return state
        return None

    def change(self, *, current_password: str, username: str, new_password: str | None) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if not check_password_hash(state["password_hash"], current_password):
                raise PermissionError("当前密码不正确")
            updated = dict(state)
            updated["username"] = _validate_username(username)
            if new_password is not None:
                if len(new_password) < 8 or len(new_password) > 4096:
                    raise ValueError("新密码长度必须为 8 到 4096 个字符")
                updated["password_hash"] = generate_password_hash(new_password)
            updated["revision"] = state["revision"] + 1
            self._write(updated)
            return {"username": updated["username"], "default_credentials": (
                updated["username"] == "admin" and check_password_hash(updated["password_hash"], "admin")
            ), "revision": updated["revision"]}
