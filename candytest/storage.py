from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_data_dir() -> Path:
    override = os.environ.get("CANDYTEST_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".candytest"


class Database:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.path = self.data_dir / "candytest.sqlite3"
        self.initialize()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                conn.executescript("""
                CREATE TABLE gateways (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE test_jobs (
                    id INTEGER PRIMARY KEY,
                    engine TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    rounds INTEGER NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    model_override TEXT,
                    gateway_snapshot TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE TABLE test_runs (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES test_jobs(id) ON DELETE CASCADE,
                    gateway_id INTEGER NOT NULL,
                    gateway_name TEXT NOT NULL,
                    round_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    answer TEXT,
                    is_correct INTEGER,
                    elapsed_seconds REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    total_tokens INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX idx_runs_job ON test_runs(job_id, gateway_id, round_number);
                CREATE INDEX idx_runs_gateway ON test_runs(gateway_id, created_at);
                PRAGMA user_version = 1;
                """)
            if version < 2:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version = 2;
                """)
            # A subprocess cannot survive an application restart. Do not leave a
            # permanently "running" job blocking all future work.
            conn.execute("""UPDATE test_jobs SET status = 'failed', completed_at = ?,
                         error = COALESCE(error, '应用重启，未完成任务已停止')
                         WHERE status IN ('queued', 'running', 'cancelling')""", (utcnow(),))

    @staticmethod
    def public_gateway(row: sqlite3.Row) -> dict[str, Any]:
        key_saved = bool(row["api_key"])
        return {
            "id": row["id"], "name": row["name"], "base_url": row["base_url"],
            "model": row["model"], "enabled": bool(row["enabled"]),
            "api_key_saved": key_saved, "api_key_masked": "••••••••" if key_saved else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def proxy_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM app_settings WHERE key IN ('proxy_enabled', 'proxy_url')"
            ).fetchall()
        values = {row["key"]: row["value"] for row in rows}
        return {
            "enabled": values.get("proxy_enabled") == "1",
            "url": values.get("proxy_url", ""),
        }

    def save_proxy_settings(self, enabled: bool, url: str) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    (("proxy_enabled", "1" if enabled else "0", now), ("proxy_url", url, now)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"enabled": enabled, "url": url}

    def gateways(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM gateways WHERE deleted_at IS NULL ORDER BY id").fetchall()
        return [self.public_gateway(row) for row in rows]

    def gateway_records(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM gateways WHERE deleted_at IS NULL AND enabled = 1 AND id IN ({placeholders})", ids
            ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    def create_gateway(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute("""INSERT INTO gateways
                (name,base_url,api_key,model,enabled,created_at,updated_at)
                VALUES (:name,:base_url,:api_key,:model,:enabled,:created_at,:updated_at)""",
                {**item, "created_at": now, "updated_at": now})
            row = conn.execute("SELECT * FROM gateways WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self.public_gateway(row)

    def update_gateway(self, gateway_id: int, item: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as conn:
            old = conn.execute("SELECT * FROM gateways WHERE id = ? AND deleted_at IS NULL", (gateway_id,)).fetchone()
            if not old:
                return None
            key = item["api_key"] if item.get("api_key") else old["api_key"]
            conn.execute("""UPDATE gateways SET name=?,base_url=?,api_key=?,model=?,enabled=?,updated_at=? WHERE id=?""",
                         (item["name"], item["base_url"], key, item["model"], item["enabled"], now, gateway_id))
            row = conn.execute("SELECT * FROM gateways WHERE id = ?", (gateway_id,)).fetchone()
        return self.public_gateway(row)

    def delete_gateway(self, gateway_id: int) -> bool:
        with self.connect() as conn:
            changed = conn.execute("UPDATE gateways SET deleted_at=?, enabled=0, updated_at=? WHERE id=? AND deleted_at IS NULL",
                                   (utcnow(), utcnow(), gateway_id)).rowcount
        return bool(changed)

    def create_job(self, payload: dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute("""INSERT INTO test_jobs
              (engine,mode,rounds,reasoning_effort,model_override,gateway_snapshot,status,started_at)
              VALUES (:engine,:mode,:rounds,:reasoning_effort,:model_override,:gateway_snapshot,'queued',:started_at)""", payload)
            return int(cur.lastrowid)

    def set_job_status(self, job_id: int, status: str, error: str | None = None) -> None:
        values = (status, utcnow() if status in {"completed", "failed", "cancelled"} else None, error, job_id)
        with self.connect() as conn:
            conn.execute("UPDATE test_jobs SET status=?, completed_at=?, error=? WHERE id=?", values)

    def add_run(self, run: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO test_runs
              (job_id,gateway_id,gateway_name,round_number,status,answer,is_correct,elapsed_seconds,
               input_tokens,output_tokens,reasoning_tokens,total_tokens,error,created_at)
              VALUES (:job_id,:gateway_id,:gateway_name,:round_number,:status,:answer,:is_correct,:elapsed_seconds,
               :input_tokens,:output_tokens,:reasoning_tokens,:total_tokens,:error,:created_at)""", run)

    def active_job(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM test_jobs WHERE status IN ('queued','running','cancelling') ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def latest_job(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM test_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            job = conn.execute("SELECT * FROM test_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return None
            runs = conn.execute("SELECT * FROM test_runs WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
            history_rows = conn.execute("""SELECT gateway_id,
                SUM(CASE WHEN status = 'graded' THEN 1 ELSE 0 END) graded,
                SUM(CASE WHEN status = 'graded' AND is_correct = 1 THEN 1 ELSE 0 END) correct,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) errors,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) cancelled
                FROM test_runs GROUP BY gateway_id""").fetchall()
        import json
        snapshots = json.loads(job["gateway_snapshot"])
        history = {row["gateway_id"]: dict(row) for row in history_rows}
        stats = {str(x["id"]): {"gateway_id": x["id"], "gateway_name": x["name"], "completed": 0,
                 "correct": 0, "incorrect": 0, "errors": 0, "cancelled": 0, "graded": 0, "accuracy": None,
                 "historical_graded": 0, "historical_correct": 0, "historical_errors": 0,
                 "historical_cancelled": 0, "historical_accuracy": None} for x in snapshots}
        result_runs = []
        for row in runs:
            run = dict(row)
            run["is_correct"] = None if run["is_correct"] is None else bool(run["is_correct"])
            result_runs.append(run)
            stat = stats.setdefault(str(row["gateway_id"]), {
                "gateway_id": row["gateway_id"], "gateway_name": row["gateway_name"], "completed": 0,
                "correct": 0, "incorrect": 0, "errors": 0, "cancelled": 0, "graded": 0, "accuracy": None,
                "historical_graded": 0, "historical_correct": 0, "historical_errors": 0,
                "historical_cancelled": 0, "historical_accuracy": None,
            })
            stat["completed"] += 1
            if row["status"] == "error":
                stat["errors"] += 1
            elif row["status"] == "cancelled":
                stat["cancelled"] += 1
            elif row["status"] == "graded":
                stat["graded"] += 1
                if row["is_correct"]:
                    stat["correct"] += 1
                else:
                    stat["incorrect"] += 1
        for stat in stats.values():
            if stat["graded"]:
                stat["accuracy"] = round(stat["correct"] * 100 / stat["graded"], 1)
            historical = history.get(stat["gateway_id"], {})
            stat["historical_graded"] = historical.get("graded", 0) or 0
            stat["historical_correct"] = historical.get("correct", 0) or 0
            stat["historical_errors"] = historical.get("errors", 0) or 0
            stat["historical_cancelled"] = historical.get("cancelled", 0) or 0
            if stat["historical_graded"]:
                stat["historical_accuracy"] = round(
                    stat["historical_correct"] * 100 / stat["historical_graded"], 1
                )
        graded = sum(x["graded"] for x in stats.values())
        correct = sum(x["correct"] for x in stats.values())
        incorrect = sum(x["incorrect"] for x in stats.values())
        errors = sum(x["errors"] for x in stats.values())
        cancelled = sum(x["cancelled"] for x in stats.values())
        completed = sum(x["completed"] for x in stats.values())
        return {"id": job["id"], "engine": job["engine"], "mode": job["mode"], "rounds": job["rounds"],
                "reasoning_effort": job["reasoning_effort"], "model_override": job["model_override"],
                "status": job["status"], "started_at": job["started_at"], "completed_at": job["completed_at"],
                "error": job["error"], "gateways": list(stats.values()), "runs": result_runs,
                "summary": {"completed": completed, "planned": job["rounds"] * len(stats),
                            "graded": graded, "correct": correct, "incorrect": incorrect,
                            "errors": errors, "cancelled": cancelled,
                            "accuracy": round(correct * 100 / graded, 1) if graded else None}}

    def history(self) -> dict[str, Any]:
        with self.connect() as conn:
            aggregate = conn.execute("""SELECT gateway_id, MAX(gateway_name) gateway_name,
                SUM(CASE WHEN status = 'graded' THEN 1 ELSE 0 END) graded,
                SUM(CASE WHEN status = 'graded' AND is_correct = 1 THEN 1 ELSE 0 END) correct,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) errors,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) cancelled
                FROM test_runs GROUP BY gateway_id ORDER BY gateway_name COLLATE NOCASE""").fetchall()
            recent = conn.execute("""SELECT r.*, j.engine FROM test_runs r JOIN test_jobs j ON j.id=r.job_id
                                  ORDER BY r.id DESC LIMIT 100""").fetchall()
        sites = []
        for row in aggregate:
            d = dict(row); d["accuracy"] = round(d["correct"] * 100 / d["graded"], 1) if d["graded"] else None
            sites.append(d)
        return {"gateways": sites, "runs": [dict(r) for r in recent]}

    def clear_history(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM test_runs")
            conn.execute("DELETE FROM test_jobs")
            conn.execute("COMMIT")
