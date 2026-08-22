"""Offline WebDAV mirror tests using a small in-process Basic-Auth WebDAV server."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import requests
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from candytest.storage import Database
from candytest.webdav_sync import (
    WebDAVClient,
    WebDAVConfigError,
    WebDAVConfigStore,
    WebDAVNoRemoteDataError,
    WebDAVRequestError,
    WebDAVSyncConflictError,
    WebDAVSyncService,
    WebDAVValidationError,
    parse_manifest,
)

FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None
if FLASK_AVAILABLE:
    from candytest.app import create_app


class _WebDAVState:
    username = "alice"
    password = "not-a-real-password"

    def __init__(self) -> None:
        self.directories = {"/dav"}
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.fail_delete: set[str] = set()


class _WebDAVHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_FixtureServer"

    def log_message(self, format, *args):  # noqa: A003
        pass

    @property
    def state(self) -> _WebDAVState:
        return self.server.state

    def _path(self) -> str:
        return unquote(urlsplit(self.path).path).rstrip("/") or "/"

    def _authorised(self) -> bool:
        expected = "Basic " + base64.b64encode(
            f"{self.state.username}:{self.state.password}".encode()
        ).decode()
        return self.headers.get("Authorization") == expected

    def _reject_if_needed(self) -> bool:
        self.state.requests.append((self.command, self._path(), self.headers.get("Authorization")))
        if self._authorised():
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="CandyTest"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_PROPFIND(self):  # noqa: N802
        if self._reject_if_needed():
            return
        path = self._path()
        self._respond(207 if path in self.state.directories else 404)

    def do_MKCOL(self):  # noqa: N802
        if self._reject_if_needed():
            return
        path = self._path()
        parent = path.rsplit("/", 1)[0] or "/"
        if path in self.state.directories:
            self._respond(405)
        elif parent not in self.state.directories:
            self._respond(409)
        else:
            self.state.directories.add(path)
            self._respond(201)

    def do_PUT(self):  # noqa: N802
        if self._reject_if_needed():
            return
        path = self._path()
        parent = path.rsplit("/", 1)[0] or "/"
        if parent not in self.state.directories:
            self._respond(409)
            return
        length = int(self.headers.get("Content-Length", "0"))
        previous = path in self.state.files
        self.state.files[path] = self.rfile.read(length)
        self._respond(204 if previous else 201)

    def do_GET(self):  # noqa: N802
        if self._reject_if_needed():
            return
        path = self._path()
        if path not in self.state.files:
            self._respond(404)
            return
        self._respond(200, self.state.files[path])

    def do_DELETE(self):  # noqa: N802
        if self._reject_if_needed():
            return
        path = self._path()
        if path in self.state.fail_delete:
            self._respond(500)
            return
        exists = path in self.state.files or path in self.state.directories
        if not exists:
            self._respond(404)
            return
        self.state.files = {key: value for key, value in self.state.files.items()
                            if key != path and not key.startswith(path + "/")}
        self.state.directories = {key for key in self.state.directories
                                  if key != path and not key.startswith(path + "/")}
        self._respond(204)


class _FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _WebDAVHandler)
        self.state = _WebDAVState()


class WebDAVFixture:
    def __enter__(self):
        self.server = _FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/dav"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class WebDAVSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_dir = self.root / "db"
        self.db = Database(self.db_dir)
        self.store = WebDAVConfigStore(self.db_dir)
        self.service = WebDAVSyncService(self.db, self.store)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def settings(url: str, remote_path: str = "/Candy Test/") -> dict:
        return {
            "server_url": url,
            "remote_path": remote_path,
            "username": "alice",
            "password": "not-a-real-password",
        }

    def configure(self, url: str, remote_path: str = "/Candy Test/") -> dict:
        return self.store.save(self.settings(url, remote_path))

    def add_data(self, db: Database | None = None, name: str = "同步站") -> None:
        target = db or self.db
        target.create_gateway({
            "name": name, "base_url": "https://gateway.example/v1", "api_key": "mirror-secret",
            "model": "test-model", "enabled": 1,
        })
        target.save_proxy_settings(True, "http://localhost:7890")

    def test_v1_sidecar_migrates_without_lifecycle_switches(self):
        legacy_device = "123e4567-e89b-42d3-a456-426614174000"
        legacy_revision = "123e4567-e89b-42d3-a456-426614174001"
        legacy = {
            **self.settings("https://cloud.example/dav", "Candy Test"),
            "version": 1,
            "auto_pull_start": True,
            "auto_push_exit": False,
            "device_id": legacy_device,
            "last_seen_revision": legacy_revision,
        }
        self.store.path.write_text(json.dumps(legacy), encoding="utf-8")

        public = self.store.public()
        migrated = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 2)
        self.assertNotIn("auto_pull_start", migrated)
        self.assertNotIn("auto_push_exit", migrated)
        self.assertNotIn("auto_pull_start", public)
        self.assertNotIn("auto_push_exit", public)
        self.assertEqual(migrated["server_url"], "https://cloud.example/dav")
        self.assertEqual(migrated["remote_path"], "Candy Test")
        self.assertEqual(migrated["username"], "alice")
        self.assertEqual(migrated["password"], "not-a-real-password")
        self.assertEqual(migrated["device_id"], legacy_device)
        self.assertEqual(migrated["last_seen_revision"], legacy_revision)

    def test_sidecar_masks_password_retains_blank_and_uses_local_permissions(self):
        with WebDAVFixture() as fixture:
            public = self.configure(fixture.url)
            self.assertNotIn("password", public)
            self.assertTrue(public["password_saved"])
            self.assertEqual(public["remote_path"], "Candy Test")
            revision = "123e4567-e89b-42d3-a456-426614174000"
            self.store.set_last_seen_revision(revision)
            # Password-only changes keep the mirror lineage.
            self.store.save({**self.settings(fixture.url), "password": "new-password"})
            self.assertEqual(self.store.public()["last_seen_revision"], revision)
            # Endpoint identity changes reset it, while a blank password keeps
            # the newly saved password.
            self.store.save({**self.settings(fixture.url), "password": "   ", "username": "renamed"})
            internal = self.store.get()
            self.assertEqual(internal["password"], "new-password")
            self.assertEqual(internal["username"], "renamed")
            self.assertIsNone(internal["last_seen_revision"])
            if os.name != "nt":
                self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            for invalid in ("", "/", "a//b", "a/../b", r"a\\b"):
                with self.assertRaises(WebDAVConfigError):
                    self.store.save({**self.settings(fixture.url), "remote_path": invalid})

    def test_url_encoding_basic_auth_and_proxy_session(self):
        with WebDAVFixture() as fixture:
            config = self.configure(fixture.url, "/中文 folder/")
            internal = self.store.get()
            client = WebDAVClient(internal, "http://proxy.invalid:7890")
            try:
                self.assertFalse(client.session.trust_env)
                self.assertEqual(client.session.proxies["http"], "http://proxy.invalid:7890")
                self.assertEqual(client.session.proxies["https"], "http://proxy.invalid:7890")
                self.assertIn("%E4%B8%AD%E6%96%87%20folder", client.root_url)
            finally:
                client.close()
            unicode_client = WebDAVClient({**internal, "username": "用户", "password": "密码"})
            try:
                prepared = unicode_client.session.prepare_request(requests.Request("GET", unicode_client.root_url))
                encoded = prepared.headers["Authorization"].split(" ", 1)[1]
                self.assertEqual(base64.b64decode(encoded).decode("utf-8"), "用户:密码")
            finally:
                unicode_client.close()
            # Actual fixture request proves Basic Auth is sent (without routing
            # through the deliberately nonexistent proxy).
            self.assertTrue(self.service.test_connection()["reachable"])
            self.assertTrue(all(auth and auth.startswith("Basic ") for _, _, auth in fixture.server.state.requests))

    def test_connection_probe_creates_root_and_cleans_probe(self):
        with WebDAVFixture() as fixture:
            self.configure(fixture.url)
            result = self.service.test_connection()
            self.assertTrue(result["tested"])
            paths = fixture.server.state.files
            self.assertFalse(any(".candytest-probe-" in path for path in paths))
            self.assertIn("/dav/Candy Test/revisions", fixture.server.state.directories)

    def test_first_push_and_pull_mirror_full_database_without_backup(self):
        with WebDAVFixture() as fixture:
            self.configure(fixture.url)
            self.add_data()
            pushed = self.service.push()
            self.assertEqual(pushed["warnings"], [])
            self.assertEqual(self.store.public()["last_seen_revision"], pushed["revision"])

            second_dir = self.root / "second"
            second = Database(second_dir)
            second_store = WebDAVConfigStore(second_dir)
            second_store.save(self.settings(fixture.url))
            pulled = WebDAVSyncService(second, second_store).pull()
            self.assertEqual(pulled["revision"], pushed["revision"])
            with second.connect() as conn:
                gateway = conn.execute("SELECT name, api_key FROM gateways").fetchone()
                proxy = dict(conn.execute("SELECT key,value FROM app_settings").fetchall())
            self.assertEqual(tuple(gateway), ("同步站", "mirror-secret"))
            self.assertEqual(proxy, {"proxy_enabled": "1", "proxy_url": "http://localhost:7890"})
            self.assertFalse((second_dir / "backups").exists())

    def test_v2_remote_snapshot_is_migrated_and_can_be_replaced(self):
        with WebDAVFixture() as fixture:
            self.configure(fixture.url)
            legacy_dir = self.root / "legacy"
            legacy = Database(legacy_dir)
            self.add_data(legacy, "旧版站点")
            snapshot = self.root / "legacy-v2.sqlite3"
            legacy.create_snapshot(snapshot)
            conn = sqlite3.connect(snapshot)
            try:
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("ALTER TABLE gateways DROP COLUMN multiplier")
                conn.execute("ALTER TABLE gateways DROP COLUMN sort_order")
                conn.execute("PRAGMA user_version = 2")
                conn.commit()
            finally:
                conn.close()
            payload = snapshot.read_bytes()
            revision = "123e4567-e89b-42d3-a456-426614174001"
            device_id = "123e4567-e89b-42d3-a456-426614174000"
            root_path = "/dav/Candy Test"
            fixture.server.state.directories.update({root_path, f"{root_path}/revisions", f"{root_path}/revisions/{revision}"})
            fixture.server.state.files[f"{root_path}/revisions/{revision}/candytest.sqlite3"] = payload
            fixture.server.state.files[f"{root_path}/manifest.json"] = json.dumps({
                "format": "candytest-sqlite-mirror", "version": 1,
                "revision": revision, "previous_revision": None,
                "device_id": device_id, "created_at": "2026-01-01T00:00:00+00:00",
                "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
                "db_user_version": 2,
            }).encode()

            self.assertTrue(self.service.connection_status()["reachable"])
            self.assertEqual(self.service.pull()["revision"], revision)
            site = self.db.gateways()[0]
            self.assertEqual((site["name"], site["multiplier"]), ("旧版站点", 1.0))
            with self.db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

            pushed = self.service.push()
            remote_manifest = json.loads(fixture.server.state.files[f"{root_path}/manifest.json"])
            self.assertEqual(remote_manifest["db_user_version"], 3)
            self.assertEqual(pushed["revision"], remote_manifest["revision"])

    def test_manifest_and_hash_tampering_are_rejected(self):
        with WebDAVFixture() as fixture:
            self.configure(fixture.url)
            self.add_data()
            pushed = self.service.push()
            manifest_path = "/dav/Candy Test/manifest.json"
            bad = json.loads(fixture.server.state.files[manifest_path])
            bad["unexpected"] = True
            fixture.server.state.files[manifest_path] = json.dumps(bad).encode()
            with self.assertRaises(WebDAVValidationError):
                self.service.pull()

            # Restore a syntactically sound manifest then corrupt its snapshot.
            fixture.server.state.files[manifest_path] = json.dumps({
                "format": "candytest-sqlite-mirror", "version": 1,
                "revision": pushed["revision"], "previous_revision": None,
                "device_id": pushed["device_id"], "created_at": pushed["created_at"],
                "sha256": "0" * 64, "size": pushed["size"], "db_user_version": 3,
            }).encode()
            with self.assertRaises(WebDAVValidationError):
                self.service.pull()

    def test_conflict_force_no_remote_and_cleanup_warning(self):
        with WebDAVFixture() as fixture:
            self.configure(fixture.url)
            with self.assertRaises(WebDAVNoRemoteDataError):
                self.service.pull()
            self.add_data()
            first = self.service.push()
            # A different device revision makes a normal push conflict.
            manifest_path = "/dav/Candy Test/manifest.json"
            remote = json.loads(fixture.server.state.files[manifest_path])
            remote["revision"] = "123e4567-e89b-42d3-a456-426614174000"
            fixture.server.state.files[manifest_path] = json.dumps(remote).encode()
            with self.assertRaises(WebDAVSyncConflictError):
                self.service.push()
            # Force accepts it.  The cleanup failure is explicitly only a warning.
            fixture.server.state.fail_delete.add("/dav/Candy Test/revisions/123e4567-e89b-42d3-a456-426614174000")
            forced = self.service.push(force=True)
            self.assertTrue(forced["warnings"])
            self.assertEqual(self.store.public()["last_seen_revision"], forced["revision"])
            self.assertNotEqual(first["revision"], forced["revision"])

    def test_push_rechecks_remote_revision_before_manifest_activation(self):
        with WebDAVFixture() as fixture:
            self.configure(fixture.url)
            self.add_data()
            changed = {"revision": "123e4567-e89b-42d3-a456-426614174000"}
            with patch.object(self.service, "_manifest", side_effect=[None, changed]):
                with self.assertRaises(WebDAVSyncConflictError):
                    self.service.push()
            self.assertNotIn("/dav/Candy Test/manifest.json", fixture.server.state.files)
            self.assertFalse(any(
                path.startswith("/dav/Candy Test/revisions/")
                for path in fixture.server.state.directories
            ))

    def test_manifest_parser_rejects_missing_or_bad_fields(self):
        with self.assertRaises(WebDAVValidationError):
            parse_manifest(b"{}")
        with self.assertRaises(WebDAVValidationError):
            parse_manifest(b"not-json")
        manifest = {
            "format": "candytest-sqlite-mirror", "version": 1,
            "revision": "123e4567-e89b-42d3-a456-426614174001", "previous_revision": None,
            "device_id": "123e4567-e89b-42d3-a456-426614174000",
            "created_at": "2026-01-01T00:00:00+00:00", "sha256": "0" * 64,
            "size": 100, "db_user_version": 3,
        }
        for invalid_version in ([2], 2.0, True, 4):
            with self.subTest(db_user_version=invalid_version), self.assertRaises(WebDAVValidationError):
                parse_manifest(json.dumps({**manifest, "db_user_version": invalid_version}).encode())


@unittest.skipUnless(FLASK_AVAILABLE, "Flask is not installed; install requirements.txt to run WebDAV API tests")
class WebDAVApiLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(self.root)
        self.app.testing = True
        self.client = self.app.test_client()
        self.db = self.app.extensions["candytest_db"]
        self.store = self.app.extensions["candytest_webdav_store"]
        self.service = self.app.extensions["candytest_webdav"]
        self.manager = self.app.extensions["candytest_jobs"]

    @staticmethod
    def settings(url: str) -> dict:
        return {
            "server_url": url,
            "remote_path": "CandyTest",
            "username": "alice",
            "password": "not-a-real-password",
        }

    def add_gateway(self, name: str = "API 站点") -> dict:
        return self.db.create_gateway({
            "name": name,
            "base_url": "https://gateway.example/v1",
            "api_key": "api-route-secret",
            "model": "test-model",
            "enabled": 1,
        })

    def test_settings_test_push_status_and_confirmed_pull_routes(self):
        with WebDAVFixture() as fixture:
            saved = self.client.put(
                "/api/settings/webdav", json=self.settings(fixture.url)
            )
            self.assertEqual(saved.status_code, 200)
            public = saved.get_json()["webdav"]
            self.assertNotIn("password", public)
            self.assertTrue(public["password_saved"])
            self.assertEqual(self.client.post("/api/webdav/test", json={}).status_code, 200)

            gateway = self.add_gateway()
            pushed = self.client.post("/api/webdav/push", json={"force": False})
            self.assertEqual(pushed.status_code, 200)
            revision = pushed.get_json()["result"]["revision"]
            status = self.client.get("/api/webdav/status").get_json()["status"]
            self.assertEqual(status["remote_revision"], revision)

            self.db.update_gateway(gateway["id"], {
                "name": "本机临时修改", "base_url": gateway["base_url"],
                "api_key": None, "model": gateway["model"], "enabled": 1,
            })
            self.assertEqual(
                self.client.post("/api/webdav/pull", json={"confirm": False}).status_code,
                400,
            )
            pulled = self.client.post("/api/webdav/pull", json={"confirm": True})
            self.assertEqual(pulled.status_code, 200)
            self.assertEqual(self.db.gateways()[0]["name"], "API 站点")
            settings = self.client.get("/api/settings/webdav").get_json()
            self.assertEqual(settings["last_operation"]["operation"], "pull")
            self.assertNotIn("automatic", settings["last_operation"])
            self.assertNotIn("auto_pull_start", settings["webdav"])
            self.assertNotIn("auto_push_exit", settings["webdav"])
            self.assertFalse((self.root / "backups").exists())

    def test_sync_reservation_blocks_jobs_and_mutations(self):
        self.assertTrue(self.manager.reserve_sync())
        try:
            with self.assertRaisesRegex(RuntimeError, "WebDAV 同步"):
                self.manager.start("pi", "serial", 1, "low", None, [])
            response = self.client.post("/api/gateways", json={})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["error"]["code"], "SYNC_BUSY")
        finally:
            self.manager.release_sync()

    def test_run_main_serves_without_lifecycle_sync(self):
        import run

        fake_app = object()
        with patch("run.configured_host", return_value="127.0.0.1"), \
             patch("run.configured_port", return_value=8765), \
             patch("run.create_app", return_value=fake_app), \
             patch("run.threading.Timer") as timer, \
             patch("run.serve") as serve:
            run.main()
        serve.assert_called_once_with(fake_app, host="127.0.0.1", port=8765, threads=8)
        timer.return_value.start.assert_called_once()
        source = Path(run.__file__).read_text(encoding="utf-8")
        self.assertNotIn("perform_startup_sync", source)
        self.assertNotIn("perform_shutdown_sync", source)

    def test_app_main_serves_without_lifecycle_sync(self):
        import candytest.app as app_module

        fake_app = object()
        with patch.object(app_module, "configured_host", return_value="127.0.0.1"), \
             patch.object(app_module, "configured_port", return_value=8765), \
             patch.object(app_module, "create_app", return_value=fake_app), \
             patch("waitress.serve") as serve:
            app_module.main()
        serve.assert_called_once_with(fake_app, host="127.0.0.1", port=8765, threads=8)
        source = Path(app_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("perform_startup_sync", source)
        self.assertNotIn("perform_shutdown_sync", source)


if __name__ == "__main__":
    unittest.main()
