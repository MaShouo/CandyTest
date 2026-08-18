"""Offline regression tests for CandyTest.

All CLI execution is replaced with mocks; no test contacts a gateway or needs an
installed pi/Codex executable.  The suite uses only unittest so it can run once
Flask (the application dependency) has been installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from candytest import PROMPT
from candytest import cli
from candytest.jobs import JobManager
from candytest.cli import InvocationCancelled
from candytest.storage import Database, utcnow


SECRET = "dummy-test-key"
PROXY = "http://localhost:7890"


def gateway(gateway_id: int = 1, name: str = "站点 A") -> dict:
    return {
        "id": gateway_id,
        "name": name,
        "base_url": "https://gateway.example/v1",
        "api_key": SECRET,
        "model": "test-model",
        "enabled": 1,
    }


def job_payload() -> dict:
    return {
        "engine": "pi",
        "mode": "serial",
        "rounds": 1,
        "reasoning_effort": "medium",
        "model_override": None,
        "gateway_snapshot": json.dumps([{"id": 1, "name": "站点 A"}]),
        "started_at": utcnow(),
    }


def run_payload(job_id: int, *, gateway_id: int = 1, name: str = "站点 A",
                status: str = "graded", correct: int | None = 1) -> dict:
    return {
        "job_id": job_id, "gateway_id": gateway_id, "gateway_name": name,
        "round_number": 1, "status": status,
        "answer": "答案是 21" if status == "graded" else None,
        "is_correct": correct, "elapsed_seconds": 0.1,
        "input_tokens": 2, "output_tokens": 3, "reasoning_tokens": None,
        "total_tokens": 5, "error": "simulated error" if status == "error" else None,
        "created_at": utcnow(),
    }


class CliParsingAndIsolationTests(unittest.TestCase):
    def test_grading_matches_only_independent_21(self):
        self.assertTrue(cli.ANSWER_PATTERN.search("结果为 21。"))
        self.assertTrue(cli.ANSWER_PATTERN.search("21, 但是需要说明"))
        self.assertFalse(cli.ANSWER_PATTERN.search("121"))
        self.assertFalse(cli.ANSWER_PATTERN.search("210"))
        self.assertFalse(cli.ANSWER_PATTERN.search(""))

    def test_parse_pi_jsonl_ignores_noise_and_uses_assistant_usage(self):
        stdout = "\n".join((
            "not json",
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"答案 21"}],"usage":{"input":12,"output":9}}}',
            "{broken",
        ))
        self.assertEqual(cli.parse_pi_jsonl(stdout), {
            "answer": "答案 21", "input_tokens": 12, "output_tokens": 9,
            "total_tokens": 21, "reasoning_tokens": None,
        })

    def test_parse_pi_jsonl_uses_agent_end_as_fallback(self):
        stdout = json.dumps({"type": "agent_end", "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": [{"type": "text", "text": "fallback 21"}]},
        ]})
        parsed = cli.parse_pi_jsonl(stdout)
        self.assertEqual(parsed["answer"], "fallback 21")
        self.assertIsNone(parsed["total_tokens"])

    def test_parse_codex_jsonl_reads_last_message_and_usage(self):
        stdout = "\n".join((
            "noise",
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "old"}}),
            "[]",
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final 21"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 7, "output_tokens": 8, "reasoning_output_tokens": 4,
            }}),
        ))
        self.assertEqual(cli.parse_codex_jsonl(stdout), {
            "answer": "final 21", "input_tokens": 7, "output_tokens": 8,
            "reasoning_tokens": 4, "total_tokens": 15,
        })

    def test_invoke_keeps_key_out_of_pi_and_codex_configs_and_arguments(self):
        captures: list[tuple[list[str], str, dict[str, str]]] = []

        def fake_run(command, **kwargs):
            root = Path(kwargs["cwd"]).parent
            config = root / ("pi-config/models.json" if "pi" in Path(command[0]).name else "codex-home/config.toml")
            captures.append((command, config.read_text(encoding="utf-8"), dict(kwargs["env"])))
            if "pi" in Path(command[0]).name:
                stdout = json.dumps({"type": "message_end", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "21"}], "usage": {}}})
            else:
                stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "21"}})
            self.assertEqual(kwargs["input"], PROMPT)
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with patch("candytest.cli.resolve_executable", side_effect=lambda engine: f"fake-{engine}"), \
             patch("candytest.cli.subprocess.run", side_effect=fake_run):
            self.assertEqual(cli.invoke("pi", gateway(), "override-model", "medium", 1, proxy_url=PROXY)["answer"], "21")
            self.assertEqual(cli.invoke("codex", gateway(), "override-model", "medium", 1)["answer"], "21")

        self.assertEqual(len(captures), 2)
        for command, config, env in captures:
            self.assertNotIn(SECRET, config)
            self.assertNotIn(SECRET, " ".join(command))
            self.assertEqual(env[cli.SECRET_ENV], SECRET)
            self.assertIn("https://gateway.example/v1", config)
            self.assertIn("responses", config)
        self.assertIn("override-model", captures[0][1])  # pi model registration
        self.assertEqual(captures[0][2]["HTTP_PROXY"], PROXY)
        self.assertEqual(captures[0][2]["HTTPS_PROXY"], PROXY)
        self.assertNotIn(PROXY, captures[0][1])
        self.assertNotIn(PROXY, " ".join(captures[0][0]))
        self.assertNotIn("HTTP_PROXY", captures[1][2])
        self.assertNotIn("HTTPS_PROXY", captures[1][2])
        pi_model = json.loads(captures[0][1])["providers"]["candytest"]["models"][0]
        self.assertTrue(pi_model["reasoning"])
        self.assertEqual(pi_model["thinkingLevelMap"]["xhigh"], "xhigh")
        self.assertIn("override-model", captures[1][0])  # Codex --model argument

    def test_fake_executables_run_end_to_end_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_script = root / "fake_cli.py"
            fake_script.write_text(
                "import json, os, sys\n"
                "sys.stdin.reconfigure(encoding='utf-8', errors='replace')\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "prompt = sys.stdin.read()\n"
                "if '不使用任何外部工具' not in prompt: raise SystemExit(2)\n"
                "if not os.environ.get('CANDYTEST_GATEWAY_API_KEY'): raise SystemExit(3)\n"
                "if os.environ.get('PI_CODING_AGENT_DIR'):\n"
                " print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'最终答案 21'}],'usage':{'input':5,'output':2,'totalTokens':7}}}, ensure_ascii=False))\n"
                "elif os.environ.get('CODEX_HOME'):\n"
                " print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'最终答案 21'}}, ensure_ascii=False))\n"
                " print(json.dumps({'type':'turn.completed','usage':{'input_tokens':6,'output_tokens':3,'reasoning_output_tokens':1}}))\n"
                "else: raise SystemExit(4)\n",
                encoding="utf-8",
            )
            if os.name == "nt":
                wrapper = root / "fake.cmd"
                wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{fake_script}" %*\r\n', encoding="utf-8")
            else:
                wrapper = root / "fake"
                wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_script}" "$@"\n', encoding="utf-8")
                wrapper.chmod(0o700)
            with patch("candytest.cli.resolve_executable", return_value=str(wrapper)):
                pi_result = cli.invoke("pi", gateway(), "test-model", "medium", 5)
                codex_result = cli.invoke("codex", gateway(), "test-model", "medium", 5)
            self.assertEqual((pi_result["answer"], pi_result["total_tokens"]), ("最终答案 21", 7))
            self.assertEqual((codex_result["answer"], codex_result["reasoning_tokens"]), ("最终答案 21", 1))

    def test_cancellable_process_terminates_promptly(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.2, cancel_event.set)
        timer.start()
        started = time.perf_counter()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(InvocationCancelled):
                cli._run_cancellable(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=Path(temp), env=os.environ.copy(), timeout=5,
                    cancel_event=cancel_event,
                )
        timer.cancel()
        self.assertLess(time.perf_counter() - started, 5)

    def test_nonzero_cli_output_is_redacted(self):
        with patch("candytest.cli.resolve_executable", return_value="fake-pi"), \
             patch("candytest.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", f"failed {SECRET}")):
            with self.assertRaisesRegex(RuntimeError, "已脱敏 API Key") as raised:
                cli.invoke("pi", gateway(), "test-model", "medium", 1)
        self.assertNotIn(SECRET, str(raised.exception))


class FrontendSafetyTests(unittest.TestCase):
    def test_frontend_uses_strict_80_threshold_and_safe_text_rendering(self):
        source = (Path(__file__).parents[1] / "candytest/static/app.js").read_text(encoding="utf-8")
        self.assertIn("< 80", source)
        self.assertIn("textContent", source)
        self.assertNotIn("innerHTML", source)
        self.assertIn('details[open][data-detail-key]', source)
        self.assertIn("d.open = opened.has(key)", source)
        self.assertIn('input[type=checkbox]:checked', source)
        self.assertIn("checkbox.checked = gateway.enabled && selected.has(gateway.id)", source)
        self.assertIn('previous = select.value || "low"', source)
        self.assertIn('e.code !== "WEBDAV_CONFLICT"', source)
        self.assertIn("Pull 不会备份", source)
        template = (Path(__file__).parents[1] / "candytest/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="webdavForm"', template)
        self.assertNotIn('webdavAutoPull', template)
        self.assertNotIn('webdavAutoPush', template)
        self.assertNotIn('auto_pull_start', source)
        self.assertNotIn('auto_push_exit', source)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def create_site(self, name: str = "站点 A") -> dict:
        return self.db.create_gateway({
            "name": name, "base_url": "https://example.test/v1/", "api_key": SECRET,
            "model": "model-a", "enabled": 1,
        })

    def test_proxy_settings_default_and_persistence(self):
        self.assertEqual(self.db.proxy_settings(), {"enabled": False, "url": ""})
        self.assertEqual(
            self.db.save_proxy_settings(True, PROXY),
            {"enabled": True, "url": PROXY},
        )
        self.assertEqual(self.db.proxy_settings(), {"enabled": True, "url": PROXY})
        self.db.save_proxy_settings(False, PROXY)
        self.assertEqual(self.db.proxy_settings(), {"enabled": False, "url": PROXY})

    def test_gateway_crud_keeps_key_private_and_blank_update_preserves_it(self):
        site = self.create_site()
        self.assertNotIn("api_key", site)
        self.assertTrue(site["api_key_saved"])
        self.assertEqual(site["api_key_masked"], "••••••••")
        saved = self.db.update_gateway(site["id"], {
            "name": "重命名", "base_url": "https://changed.test/v1", "api_key": None,
            "model": "model-b", "enabled": 1,
        })
        self.assertEqual(saved["name"], "重命名")
        self.assertEqual(self.db.gateway_records([site["id"]])[0]["api_key"], SECRET)
        self.assertTrue(self.db.delete_gateway(site["id"]))
        self.assertEqual(self.db.gateways(), [])
        self.assertEqual(self.db.gateway_records([site["id"]]), [])

    def test_history_groups_by_gateway_and_excludes_errors_from_denominator(self):
        site = self.create_site()
        job_id = self.db.create_job(job_payload())
        self.db.add_run(run_payload(job_id, correct=1))
        self.db.add_run(run_payload(job_id, correct=0))
        self.db.add_run(run_payload(job_id, status="error", correct=None))
        self.db.add_run(run_payload(job_id, status="cancelled", correct=None))
        history = self.db.history()
        aggregate = history["gateways"][0]
        self.assertEqual(
            (aggregate["graded"], aggregate["correct"], aggregate["errors"], aggregate["cancelled"]),
            (2, 1, 1, 1),
        )
        self.assertEqual(aggregate["accuracy"], 50.0)
        self.assertEqual(len(history["runs"]), 4)

    def test_accuracy_threshold_is_low_below_80_and_not_at_80(self):
        site = self.create_site()
        job_id = self.db.create_job(job_payload())
        for correct in (1, 1, 1, 0):
            self.db.add_run(run_payload(job_id, correct=correct))
        self.assertEqual(self.db.history()["gateways"][0]["accuracy"], 75.0)
        self.db.add_run(run_payload(job_id, correct=1))
        self.assertEqual(self.db.history()["gateways"][0]["accuracy"], 80.0)

    def test_snapshot_is_independent_and_contains_committed_wal_data(self):
        site = self.create_site()
        snapshot = Path(self.temp.name) / "exports" / "candytest.sqlite3"
        # Keep a writer connection open so this committed proxy update is in
        # the source WAL when sqlite3.backup creates the single-file snapshot.
        with self.db.connect() as conn:
            now = utcnow()
            conn.executemany(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (("proxy_enabled", "1", now), ("proxy_url", PROXY, now)),
            )
            self.assertTrue(Path(f"{self.db.path}-wal").is_file())
            self.assertEqual(self.db.create_snapshot(snapshot), snapshot)
        self.assertTrue(snapshot.is_file())
        if os.name != "nt":
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

        # Mutations after backup cannot change the single-file snapshot.
        self.db.update_gateway(site["id"], {
            "name": "已修改", "base_url": "https://changed.example/v1", "api_key": "other-key",
            "model": "other-model", "enabled": 1,
        })
        self.db.save_proxy_settings(False, "")
        conn = sqlite3.connect(snapshot)
        try:
            stored = conn.execute("SELECT name, api_key FROM gateways WHERE id = ?", (site["id"],)).fetchone()
            settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
        finally:
            conn.close()
        self.assertEqual(stored, ("站点 A", SECRET))
        self.assertEqual(settings, {"proxy_enabled": "1", "proxy_url": PROXY})

    def test_restore_snapshot_recovers_all_persisted_data_without_backup(self):
        site = self.create_site()
        self.db.save_proxy_settings(True, PROXY)
        job_id = self.db.create_job(job_payload())
        self.db.add_run(run_payload(job_id, correct=1))
        self.db.set_job_status(job_id, "completed")
        snapshot = Path(self.temp.name) / "full-mirror.sqlite3"
        self.db.create_snapshot(snapshot)

        self.db.update_gateway(site["id"], {
            "name": "本地更改", "base_url": "https://changed.example/v1", "api_key": "different-key",
            "model": "different-model", "enabled": 0,
        })
        self.db.save_proxy_settings(False, "")
        self.db.clear_history()
        self.create_site("仅本地")

        self.db.restore_snapshot(snapshot)
        with self.db.connect() as conn:
            gateways = conn.execute("SELECT name, api_key FROM gateways ORDER BY id").fetchall()
            settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
            jobs = conn.execute("SELECT id, status FROM test_jobs").fetchall()
            runs = conn.execute("SELECT job_id, answer, is_correct FROM test_runs").fetchall()
        self.assertEqual([tuple(row) for row in gateways], [("站点 A", SECRET)])
        self.assertEqual(settings, {"proxy_enabled": "1", "proxy_url": PROXY})
        self.assertEqual([tuple(row) for row in jobs], [(job_id, "completed")])
        self.assertEqual([tuple(row) for row in runs], [(job_id, "答案是 21", 1)])
        self.assertFalse(list(Path(self.temp.name).glob(".candytest-restore-*.sqlite3")))

    def test_snapshot_validation_rejects_bad_schema_versions_and_restore_keeps_database(self):
        site = self.create_site()
        invalid = Path(self.temp.name) / "not-a-snapshot.sqlite3"
        invalid.write_bytes(b"not a database")
        with self.assertRaises(ValueError):
            self.db.restore_snapshot(invalid)
        self.assertEqual(self.db.gateway_records([site["id"]])[0]["api_key"], SECRET)

        for version in (1, 3):
            snapshot = Path(self.temp.name) / f"version-{version}.sqlite3"
            self.db.create_snapshot(snapshot)
            conn = sqlite3.connect(snapshot)
            try:
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(ValueError, "版本不兼容"):
                self.db.validate_snapshot(snapshot)

    def test_connect_lock_covers_the_connection_lifetime(self):
        acquired = threading.Event()

        def open_connection() -> None:
            with self.db.connect():
                acquired.set()

        with self.db.connect():
            worker = threading.Thread(target=open_connection)
            worker.start()
            self.assertFalse(acquired.wait(0.15))
        self.assertTrue(acquired.wait(2))
        worker.join(2)
        self.assertFalse(worker.is_alive())


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name))
        self.manager = JobManager(self.db)
        self.sites = [gateway(1, "A"), gateway(2, "B")]

    def tearDown(self):
        self.temp.cleanup()

    def make_job(self, mode: str, rounds: int) -> int:
        payload = job_payload()
        payload.update({"mode": mode, "rounds": rounds,
                        "gateway_snapshot": json.dumps([{"id": x["id"], "name": x["name"]} for x in self.sites])})
        return self.db.create_job(payload)

    def test_serial_mode_keeps_gateway_and_round_order(self):
        events: list[str] = []
        proxies: list[str | None] = []

        def fake_invoke(_engine, site, _model, _effort, _timeout, _cancel_event, proxy_url):
            events.append(site["name"])
            proxies.append(proxy_url)
            return {"answer": "21", "elapsed_seconds": 0.0}

        job_id = self.make_job("serial", 2)
        with patch("candytest.jobs.invoke", side_effect=fake_invoke):
            self.manager._run_job(job_id, "pi", "serial", 2, "medium", None, self.sites, proxy_url=PROXY)
        self.assertEqual(events, ["A", "A", "B", "B"])
        self.assertEqual(proxies, [PROXY] * 4)
        stored_job = self.db.job(job_id)
        self.assertEqual(stored_job["status"], "completed")
        self.assertEqual(stored_job["summary"], {
            "completed": 4, "planned": 4, "graded": 4, "correct": 4,
            "incorrect": 0, "errors": 0, "cancelled": 0, "accuracy": 100.0,
        })
        self.assertEqual(stored_job["gateways"][0]["historical_accuracy"], 100.0)

    def test_parallel_mode_overlaps_sites_but_never_rounds_of_one_site(self):
        lock = threading.Lock()
        active_by_site = {"A": 0, "B": 0}
        max_by_site = {"A": 0, "B": 0}
        active_total = 0
        max_total = 0

        def fake_invoke(_engine, site, _model, _effort, _timeout, _cancel_event, _proxy_url):
            nonlocal active_total, max_total
            with lock:
                active_by_site[site["name"]] += 1
                active_total += 1
                max_by_site[site["name"]] = max(max_by_site[site["name"]], active_by_site[site["name"]])
                max_total = max(max_total, active_total)
            time.sleep(0.04)
            with lock:
                active_by_site[site["name"]] -= 1
                active_total -= 1
            return {"answer": "21", "elapsed_seconds": 0.04}

        job_id = self.make_job("parallel", 2)
        with patch("candytest.jobs.invoke", side_effect=fake_invoke):
            self.manager._run_job(job_id, "pi", "parallel", 2, "medium", None, self.sites)
        self.assertGreaterEqual(max_total, 2)
        self.assertEqual(max_by_site, {"A": 1, "B": 1})
        self.assertEqual(len(self.db.job(job_id)["runs"]), 4)

    def test_cancel_stops_active_call_and_future_rounds(self):
        entered = threading.Event()

        def blocking_invoke(_engine, _site, _model, _effort, _timeout, cancel_event, _proxy_url):
            entered.set()
            cancel_event.wait(5)
            raise InvocationCancelled("用户已中断测试")

        with patch("candytest.jobs.invoke", side_effect=blocking_invoke):
            job_id = self.manager.start("pi", "serial", 3, "medium", None, [self.sites[0]])
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.manager.cancel(job_id))
            deadline = time.time() + 3
            while self.db.job(job_id)["status"] not in {"cancelled", "failed"} and time.time() < deadline:
                time.sleep(0.02)

        stored = self.db.job(job_id)
        self.assertEqual(stored["status"], "cancelled")
        self.assertEqual(len(stored["runs"]), 1)
        self.assertEqual(stored["runs"][0]["status"], "cancelled")
        self.assertEqual(stored["summary"]["graded"], 0)
        self.assertEqual(stored["summary"]["cancelled"], 1)
        self.assertFalse(self.manager.cancel(job_id))


FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(FLASK_AVAILABLE, "Flask is not installed; install requirements.txt to run API tests")
class ApiTests(unittest.TestCase):
    def setUp(self):
        from candytest.app import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name))
        self.app.testing = True
        self.client = self.app.test_client()
        self.db = self.app.extensions["candytest_db"]
        self.site_body = {"name": "测试站", "base_url": "https://gateway.example/v1/", "api_key": SECRET,
                          "model": "test-model", "enabled": True}

    def tearDown(self):
        self.temp.cleanup()

    def add_site(self) -> int:
        response = self.client.post("/api/gateways", json=self.site_body)
        self.assertEqual(response.status_code, 201)
        return response.get_json()["gateway"]["id"]

    def test_gateway_api_never_returns_key_and_blank_edit_retains_key(self):
        site_id = self.add_site()
        created = self.client.get("/api/gateways").get_json()["gateways"][0]
        self.assertNotIn("api_key", created)
        self.assertEqual(created["api_key_masked"], "••••••••")
        self.assertNotIn(SECRET, json.dumps(created))
        update = {**self.site_body, "name": "已编辑", "api_key": ""}
        self.assertEqual(self.client.put(f"/api/gateways/{site_id}", json=update).status_code, 200)
        self.assertEqual(self.db.gateway_records([site_id])[0]["api_key"], SECRET)

    def test_api_validates_content_gateway_and_unavailable_cli(self):
        self.assertEqual(self.client.post("/api/gateways", data="{}").status_code, 415)
        invalid = {**self.site_body, "base_url": "ftp://gateway.example"}
        bad = self.client.post("/api/gateways", json=invalid)
        self.assertEqual((bad.status_code, bad.get_json()["error"]["code"]), (400, "INVALID_GATEWAY"))
        site_id = self.add_site()
        with patch("candytest.app.cli_availability", return_value={"pi": False, "codex": False}):
            response = self.client.post("/api/jobs", json={"engine": "pi", "gateway_ids": [site_id]})
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (409, "CLI_UNAVAILABLE"))

    def test_proxy_api_validates_and_passes_snapshot_to_new_job(self):
        self.assertEqual(
            self.client.get("/api/settings/proxy").get_json()["proxy"],
            {"enabled": False, "url": ""},
        )
        invalid = self.client.put(
            "/api/settings/proxy", json={"enabled": True, "url": "socks5://localhost:7891"}
        )
        self.assertEqual((invalid.status_code, invalid.get_json()["error"]["code"]), (400, "INVALID_PROXY"))
        saved = self.client.put(
            "/api/settings/proxy", json={"enabled": True, "url": PROXY + "/"}
        )
        self.assertEqual(saved.get_json()["proxy"], {"enabled": True, "url": PROXY})
        site_id = self.add_site()
        manager = self.app.extensions["candytest_jobs"]
        with patch("candytest.app.cli_availability", return_value={"pi": True, "codex": False}), \
             patch.object(manager, "start", return_value=123) as start:
            response = self.client.post("/api/jobs", json={"engine": "pi", "gateway_ids": [site_id]})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(start.call_args.args[-1], PROXY)

    def test_job_conflict_and_confirmed_history_clear(self):
        site_id = self.add_site()
        job_id = self.db.create_job(job_payload())
        self.db.set_job_status(job_id, "running")
        with patch("candytest.app.cli_availability", return_value={"pi": True, "codex": False}):
            response = self.client.post("/api/jobs", json={"engine": "pi", "gateway_ids": [site_id]})
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (409, "JOB_CONFLICT"))
        manager = self.app.extensions["candytest_jobs"]
        with patch.object(manager, "cancel", return_value=True):
            cancelled = self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual((cancelled.status_code, cancelled.get_json()["status"]), (200, "cancelling"))
        self.db.set_job_status(job_id, "failed")
        inactive = self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual((inactive.status_code, inactive.get_json()["error"]["code"]), (409, "JOB_NOT_ACTIVE"))
        self.db.add_run(run_payload(job_id))
        self.assertEqual(self.client.delete("/api/history", json={"confirm": False}).status_code, 400)
        self.assertEqual(self.client.delete("/api/history", json={"confirm": True}).status_code, 200)
        self.assertEqual(self.client.get("/api/history").get_json()["runs"], [])
        self.assertEqual(len(self.client.get("/api/gateways").get_json()["gateways"]), 1)


@unittest.skipUnless(FLASK_AVAILABLE, "Flask is not installed; install requirements.txt to run API tests")
class ServerAuthTests(unittest.TestCase):
    password = "correct horse battery staple"
    secret_key = "a" * 64

    def server_environ(self, **overrides: str) -> dict[str, str]:
        from werkzeug.security import generate_password_hash
        values = {
            "CANDYTEST_DEPLOYMENT": "server",
            "CANDYTEST_HOST": "127.0.0.1",
            "CANDYTEST_ADMIN_USERNAME": "admin",
            "CANDYTEST_ADMIN_PASSWORD_HASH": generate_password_hash(self.password),
            "CANDYTEST_SECRET_KEY": self.secret_key,
            "CANDYTEST_COOKIE_SECURE": "0",
        }
        values.update(overrides)
        return values

    def make_client(self, **overrides: str):
        from candytest.app import create_app
        temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, self.server_environ(**overrides), clear=False):
            app = create_app(Path(temp.name))
        app.testing = True
        return temp, app, app.test_client()

    @staticmethod
    def csrf(response) -> str:
        import re
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        if not match:
            match = re.search(r'name="csrf-token" content="([^"]+)"', response.get_data(as_text=True))
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self, client):
        page = client.get("/login")
        token = self.csrf(page)
        response = client.post("/login", data={"username": "admin", "password": self.password, "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        return self.csrf(client.get("/"))

    def test_deployment_and_host_validation(self):
        from candytest.app import configured_deployment, configured_host
        with patch.dict(os.environ, {"CANDYTEST_DEPLOYMENT": "broken"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "local 或 server"):
                configured_deployment()
        with patch.dict(os.environ, {"CANDYTEST_DEPLOYMENT": "local", "CANDYTEST_HOST": "0.0.0.0"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "回环"):
                configured_host()
        for host in ("0.0.0.0", "127.0.0.1", "10.0.0.1", "8.8.8.8", "::"):
            with patch.dict(os.environ, {"CANDYTEST_DEPLOYMENT": "server", "CANDYTEST_HOST": host}, clear=False):
                self.assertEqual(configured_host(), host)
        for host in ("224.0.0.1", "240.0.0.1"):
            with patch.dict(os.environ, {"CANDYTEST_DEPLOYMENT": "server", "CANDYTEST_HOST": host}, clear=False):
                with self.assertRaises(RuntimeError):
                    configured_host()

    def test_server_fails_closed_when_credentials_or_secret_are_invalid(self):
        from candytest.app import create_app
        with tempfile.TemporaryDirectory() as temp:
            for overrides in (
                {"CANDYTEST_ADMIN_USERNAME": ""},
                {"CANDYTEST_ADMIN_PASSWORD_HASH": ""},
                {"CANDYTEST_ADMIN_PASSWORD_HASH": "not-a-password-hash"},
                {"CANDYTEST_SECRET_KEY": "too-short"},
            ):
                with patch.dict(os.environ, self.server_environ(**overrides), clear=False):
                    with self.assertRaises(RuntimeError):
                        create_app(Path(temp))

    def test_server_accepts_base64_password_hash_for_compose(self):
        import base64
        from werkzeug.security import generate_password_hash

        encoded = base64.b64encode(generate_password_hash(self.password).encode("utf-8")).decode("ascii")
        temp, app, client = self.make_client(
            CANDYTEST_ADMIN_PASSWORD_HASH="",
            CANDYTEST_ADMIN_PASSWORD_HASH_B64=encoded,
        )
        try:
            self.login(client)
            self.assertEqual(client.get("/api/runtime").status_code, 200)
        finally:
            temp.cleanup()

    def test_login_protection_csrf_logout_cookie_and_security_headers(self):
        temp, app, client = self.make_client()
        try:
            self.assertEqual(client.get("/healthz").get_json(), {"status": "ok"})
            self.assertEqual(client.get("/healthz").headers["Cache-Control"], "no-store")
            self.assertEqual(client.get("/").status_code, 302)
            api = client.get("/api/gateways")
            self.assertEqual((api.status_code, api.get_json()["error"]["code"]), (401, "AUTH_REQUIRED"))
            unauthenticated_post = client.post("/api/gateways", json={})
            self.assertEqual((unauthenticated_post.status_code, unauthenticated_post.get_json()["error"]["code"]), (401, "AUTH_REQUIRED"))
            page = client.get("/login")
            self.assertEqual(page.headers["Cache-Control"], "no-store")
            self.assertIn("default-src 'self'", page.headers["Content-Security-Policy"])
            self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(page.headers["X-Frame-Options"], "DENY")
            self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn("Strict-Transport-Security", page.headers)
            bad_csrf = client.post("/login", data={"username": "admin", "password": self.password})
            self.assertEqual(bad_csrf.status_code, 403)
            token = self.csrf(page)
            bad_password = client.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": token})
            self.assertEqual(bad_password.status_code, 401)
            self.assertIn("用户名或密码错误", bad_password.get_data(as_text=True))
            csrf = self.login(client)
            self.assertIn('name="csrf-token"', client.get("/").get_data(as_text=True))
            denied = client.post("/api/gateways", json={})
            self.assertEqual((denied.status_code, denied.get_json()["error"]["code"]), (403, "CSRF_FAILED"))
            allowed = client.post("/api/gateways", json={
                "name": "server", "base_url": "https://gateway.example/v1", "api_key": SECRET,
                "model": "model", "enabled": True,
            }, headers={"X-CSRF-Token": csrf})
            self.assertEqual(allowed.status_code, 201)
            logout_denied = client.post("/logout")
            self.assertEqual((logout_denied.status_code, logout_denied.get_json()["error"]["code"]), (403, "CSRF_FAILED"))
            logout = client.post("/logout", headers={"X-CSRF-Token": csrf})
            self.assertEqual(logout.get_json(), {"ok": True})
            self.assertEqual(client.get("/api/gateways").status_code, 401)
        finally:
            temp.cleanup()

    def test_login_rate_limiter_uses_generic_failure_message(self):
        temp, app, client = self.make_client()
        try:
            token = self.csrf(client.get("/login"))
            for _ in range(6):
                response = client.post("/login", data={
                    "username": "admin", "password": "wrong", "csrf_token": token,
                })
                self.assertEqual(response.status_code, 401)
                self.assertIn("用户名或密码错误，或登录尝试过于频繁", response.get_data(as_text=True))
        finally:
            temp.cleanup()

    def test_server_secure_cookie_default_and_local_remains_unprotected(self):
        temp, app, client = self.make_client(CANDYTEST_COOKIE_SECURE="1")
        try:
            login_page = client.get("/login")
            response = client.post("/login", data={
                "username": "admin", "password": self.password, "csrf_token": self.csrf(login_page),
            })
            cookie = response.headers["Set-Cookie"]
            self.assertIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)
        finally:
            temp.cleanup()
        from candytest.app import create_app
        with tempfile.TemporaryDirectory() as local_temp, patch.dict(os.environ, {"CANDYTEST_DEPLOYMENT": "local"}, clear=False):
            local = create_app(Path(local_temp))
            local.testing = True
            response = local.test_client().get("/")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("退出登录", response.get_data(as_text=True))

    def test_run_only_opens_browser_in_local_mode(self):
        import run
        sentinel = RuntimeError("stop serving")
        with patch("run.create_app", return_value=object()), patch("run.serve", side_effect=sentinel), \
             patch("run.webbrowser.open") as opened, patch("run.threading.Timer") as timer, \
             patch("run.configured_host", return_value="127.0.0.1"), patch("run.configured_port", return_value=8765), \
             patch("run.configured_deployment", return_value="local"):
            with self.assertRaisesRegex(RuntimeError, "stop serving"):
                run.main()
            timer.assert_called_once()
            opened.assert_not_called()
        with patch("run.create_app", return_value=object()), patch("run.serve", side_effect=sentinel), \
             patch("run.webbrowser.open") as opened, patch("run.threading.Timer") as timer, \
             patch("run.configured_host", return_value="0.0.0.0"), patch("run.configured_port", return_value=8765), \
             patch("run.configured_deployment", return_value="server"):
            with self.assertRaisesRegex(RuntimeError, "stop serving"):
                run.main()
            timer.assert_not_called()
            opened.assert_not_called()
