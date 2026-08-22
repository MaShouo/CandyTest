from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CURRENT_SCHEMA_VERSION = 3
MIGRATABLE_SCHEMA_VERSIONS = frozenset({2})
REQUIRED_SNAPSHOT_TABLES = frozenset({"gateways", "test_jobs", "test_runs", "app_settings"})

_AUTH_OR_RATE_LIMIT = re.compile(r"(?<!\d)(?:401|429)(?!\d)")
_GATEWAY_UNAVAILABLE = re.compile(
    r"(?<!\d)(?:500|502|503|504|521|522|523|524|525|526|529)(?!\d)"
    r"|\bbad\s+gateway\b|\bservice\s+unavailable\b|\bgateway\s+timeout\b"
    r"|\binternal\s+server\s+error\b|\bupstream\b"
    r"|(?:无可用(?:渠道|通道|提供商)|(?:渠道|通道|提供商)不可用|中转站不可用)"
    r"|\bno\s+available\s+(?:channels?|providers?)\b|\b(?:channels?|providers?)\s+unavailable\b"
    r"|\b(?:econnrefused|enotfound|etimedout|econnreset)\b"
    r"|\bfetch\s+(?:failed|error)\b|\bnetwork\s+(?:error|failure|unavailable)\b"
    r"|\b(?:connection|connect)\s+(?:refused|reset|failed|error|closed|aborted|unavailable)\b"
    r"|\b(?:dns|name\s+resolution)\s+(?:failed|failure|error|lookup|unavailable)\b"
    r"|\b(?:request\s+)?(?:timed?\s*out|timeout(?:error)?)\b"
    r"|(?:网络|连接).{0,12}(?:错误|异常|失败|不可用|超时|被拒绝|重置)|请求超时",
    re.IGNORECASE,
)


def classify_error(error: str | None) -> str:
    """Classify persisted CLI errors without changing the database schema."""
    text = error or ""
    # An auth or rate-limit status has a more useful, non-outage meaning even
    # when a gateway includes generic upstream wording in the same response.
    if _AUTH_OR_RATE_LIMIT.search(text):
        return "api_failure"
    if _GATEWAY_UNAVAILABLE.search(text):
        return "gateway_unavailable"
    return "api_failure"



def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day_bounds() -> tuple[str, str]:
    """Return the current UTC calendar day as ISO-8601 bounds."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(timespec="seconds"), (start + timedelta(days=1)).isoformat(timespec="seconds")


def default_data_dir() -> Path:
    override = os.environ.get("CANDYTEST_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".candytest"


class Database:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
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
        # Keep the lock for the complete connection lifecycle.  In particular,
        # snapshot restore must not replace the database beneath an in-flight
        # request connection.
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            try:
                yield conn
            finally:
                conn.close()

    @staticmethod
    def _fsync_file(path: Path) -> None:
        fd = os.open(path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _chmod_database(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _sidecar_paths(path: Path) -> tuple[Path, Path]:
        return Path(f"{path}-wal"), Path(f"{path}-shm")

    def validate_snapshot(self, snapshot_path: Path | str,
                          accepted_versions: set[int] | frozenset[int] | None = None) -> int:
        """Reject anything except a complete snapshot for an accepted schema version."""
        snapshot = Path(snapshot_path)
        try:
            info = snapshot.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size < 100:
                raise ValueError("快照不是有效的 SQLite 数据库文件")
            with snapshot.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    raise ValueError("快照不是有效的 SQLite 数据库文件")
        except OSError as exc:
            raise ValueError("无法读取数据库快照") from exc

        # immutable + mode=ro guarantees validation neither mutates the input
        # nor accidentally creates a database for a malformed path.
        try:
            uri = f"{snapshot.resolve().as_uri()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)
            try:
                integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
                if [row[0] for row in integrity_rows] != ["ok"]:
                    raise ValueError("数据库快照完整性校验失败")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                versions = accepted_versions or frozenset({CURRENT_SCHEMA_VERSION})
                if version not in versions:
                    expected = "/".join(str(item) for item in sorted(versions))
                    raise ValueError(
                        f"数据库快照版本不兼容：需要 {expected}，实际 {version}"
                    )
                table_rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                tables = {row[0] for row in table_rows}
                if not REQUIRED_SNAPSHOT_TABLES.issubset(tables):
                    raise ValueError("数据库快照缺少必要的数据表")
            finally:
                conn.close()
        except ValueError:
            raise
        except sqlite3.Error as exc:
            raise ValueError("数据库快照无法以只读模式打开") from exc
        return version

    @staticmethod
    def _migrate_gateways_v3(conn: sqlite3.Connection) -> None:
        """Apply or resume schema v3 atomically."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(gateways)")}
            if "multiplier" not in columns:
                conn.execute("ALTER TABLE gateways ADD COLUMN multiplier REAL NOT NULL DEFAULT 1")
            if "sort_order" not in columns:
                conn.execute("ALTER TABLE gateways ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.execute("PRAGMA user_version = 3")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def upgrade_snapshot(self, snapshot_path: Path | str, expected_version: int) -> None:
        """Validate and upgrade a downloaded legacy snapshot in place."""
        snapshot = Path(snapshot_path)
        versions = MIGRATABLE_SCHEMA_VERSIONS | {CURRENT_SCHEMA_VERSION}
        version = self.validate_snapshot(snapshot, versions)
        if version != expected_version:
            raise ValueError("远端 manifest 与数据库快照版本不一致")
        if version in MIGRATABLE_SCHEMA_VERSIONS:
            conn = sqlite3.connect(snapshot, timeout=15, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode = DELETE")
                self._migrate_gateways_v3(conn)
            finally:
                conn.close()
            self._fsync_file(snapshot)
        self.validate_snapshot(snapshot)

    def create_snapshot(self, snapshot_path: Path | str) -> Path:
        """Create a durable single-file SQLite backup, including committed WAL data."""
        snapshot = Path(snapshot_path)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.resolve() == self.path.resolve():
            raise ValueError("快照路径不能是当前数据库")

        with self._lock:
            for path in (snapshot, *self._sidecar_paths(snapshot)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            with self.connect() as source:
                destination = sqlite3.connect(snapshot, timeout=15, isolation_level=None)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            self.validate_snapshot(snapshot)
            self._fsync_file(snapshot)
            self._chmod_database(snapshot)
        return snapshot

    def restore_snapshot(self, snapshot_path: Path | str) -> None:
        """Atomically replace the local database with a validated snapshot.

        No backup is made here: callers intentionally choose complete-mirror
        semantics.  Until os.replace succeeds, the current local database is
        untouched.
        """
        snapshot = Path(snapshot_path)
        if snapshot.resolve() == self.path.resolve():
            raise ValueError("不能用当前数据库恢复自身")

        temporary_path: Path | None = None
        with self._lock:
            self.validate_snapshot(snapshot)
            with self.connect() as conn:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and checkpoint[0]:
                    raise RuntimeError("无法完成本地数据库 checkpoint，拒绝恢复")

            # Remove WAL/SHM before replacement. If either is still locked,
            # abort while the current database is untouched.
            for sidecar in self._sidecar_paths(self.path):
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise RuntimeError("无法清理本地数据库 sidecar，拒绝恢复") from exc

            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".candytest-restore-", suffix=".sqlite3", dir=self.data_dir
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
            try:
                shutil.copyfile(snapshot, temporary_path)
                self._fsync_file(temporary_path)
                self.validate_snapshot(temporary_path)
                self._chmod_database(temporary_path)
                os.replace(temporary_path, self.path)
                temporary_path = None
                # A new sidecar should not exist while no connection is open;
                # ignore cleanup races after the irreversible replace rather
                # than reporting a failed restore after the new DB is active.
                for sidecar in self._sidecar_paths(self.path):
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
                self.initialize()
                self._chmod_database(self.path)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass

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
            if version < 3:
                self._migrate_gateways_v3(conn)

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
            "model": row["model"], "multiplier": row["multiplier"], "enabled": bool(row["enabled"]),
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
            rows = conn.execute("SELECT * FROM gateways WHERE deleted_at IS NULL ORDER BY sort_order, id").fetchall()
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
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT id,name FROM gateways WHERE deleted_at IS NULL ORDER BY sort_order,id"
                ).fetchall()
                initial = item["name"][0].casefold()
                insert_at = next(
                    (index for index, row in enumerate(rows) if row["name"][:1].casefold() > initial),
                    len(rows),
                )
                conn.executemany(
                    "UPDATE gateways SET sort_order=? WHERE id=?",
                    ((index + (index >= insert_at), row["id"]) for index, row in enumerate(rows)),
                )
                cur = conn.execute("""INSERT INTO gateways
                    (name,base_url,api_key,model,multiplier,enabled,sort_order,created_at,updated_at)
                    VALUES (:name,:base_url,:api_key,:model,:multiplier,:enabled,:sort_order,
                    :created_at,:updated_at)""",
                    {**item, "multiplier": item.get("multiplier", 1), "sort_order": insert_at,
                     "created_at": now, "updated_at": now})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            row = conn.execute("SELECT * FROM gateways WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self.public_gateway(row)

    def reorder_gateways(self, gateway_ids: list[int]) -> bool:
        """Persist a complete gateway ordering, rejecting stale or partial lists."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stored_ids = [row["id"] for row in conn.execute(
                    "SELECT id FROM gateways WHERE deleted_at IS NULL"
                ).fetchall()]
                if len(gateway_ids) != len(stored_ids) or set(gateway_ids) != set(stored_ids):
                    conn.execute("ROLLBACK")
                    return False
                conn.executemany(
                    "UPDATE gateways SET sort_order=? WHERE id=? AND deleted_at IS NULL",
                    enumerate(gateway_ids),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return True

    def update_gateway(self, gateway_id: int, item: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as conn:
            old = conn.execute("SELECT * FROM gateways WHERE id = ? AND deleted_at IS NULL", (gateway_id,)).fetchone()
            if not old:
                return None
            key = item["api_key"] if item.get("api_key") else old["api_key"]
            conn.execute("""UPDATE gateways SET name=?,base_url=?,api_key=?,model=?,multiplier=?,enabled=?,updated_at=? WHERE id=?""",
                         (item["name"], item["base_url"], key, item["model"], item.get("multiplier", old["multiplier"]), item["enabled"], now, gateway_id))
            row = conn.execute("SELECT * FROM gateways WHERE id = ?", (gateway_id,)).fetchone()
        return self.public_gateway(row)

    def delete_gateway(self, gateway_id: int, delete_history: bool = False) -> tuple[bool, int]:
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    "UPDATE gateways SET deleted_at=?, enabled=0, updated_at=? WHERE id=? AND deleted_at IS NULL",
                    (now, now, gateway_id),
                ).rowcount
                deleted = 0
                if changed and delete_history:
                    if conn.execute(
                        "SELECT 1 FROM test_jobs WHERE status IN ('queued','running','cancelling') LIMIT 1"
                    ).fetchone():
                        raise RuntimeError("测试任务运行时不能删除历史")
                    deleted = conn.execute(
                        "DELETE FROM test_runs WHERE gateway_id=?", (gateway_id,)
                    ).rowcount
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return bool(changed), int(deleted)

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
            today_start, tomorrow_start = utc_day_bounds()
            history_rows = conn.execute("""SELECT gateway_id,
                SUM(CASE WHEN status = 'graded' THEN 1 ELSE 0 END) graded,
                SUM(CASE WHEN status = 'graded' AND is_correct = 1 THEN 1 ELSE 0 END) correct,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) errors,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) cancelled,
                SUM(CASE WHEN status = 'graded' AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_graded,
                SUM(CASE WHEN status = 'graded' AND is_correct = 1 AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_correct,
                SUM(CASE WHEN status = 'error' AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_errors,
                SUM(CASE WHEN status = 'cancelled' AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_cancelled
                FROM test_runs GROUP BY gateway_id""", (
                    today_start, tomorrow_start, today_start, tomorrow_start,
                    today_start, tomorrow_start, today_start, tomorrow_start
                )).fetchall()
        import json
        snapshots = json.loads(job["gateway_snapshot"])
        history = {row["gateway_id"]: dict(row) for row in history_rows}
        stats = {str(x["id"]): {"gateway_id": x["id"], "gateway_name": x["name"], "completed": 0,
                 "correct": 0, "incorrect": 0, "errors": 0, "cancelled": 0, "graded": 0, "accuracy": None,
                 "historical_graded": 0, "historical_correct": 0, "historical_errors": 0,
                 "historical_cancelled": 0, "historical_accuracy": None,
                 "historical_today_graded": 0, "historical_today_correct": 0,
                 "historical_today_errors": 0, "historical_today_cancelled": 0,
                 "historical_today_accuracy": None} for x in snapshots}
        result_runs = []
        for row in runs:
            run = dict(row)
            run["is_correct"] = None if run["is_correct"] is None else bool(run["is_correct"])
            run["error_kind"] = classify_error(run["error"]) if run["status"] == "error" else None
            result_runs.append(run)
            stat = stats.setdefault(str(row["gateway_id"]), {
                "gateway_id": row["gateway_id"], "gateway_name": row["gateway_name"], "completed": 0,
                "correct": 0, "incorrect": 0, "errors": 0, "cancelled": 0, "graded": 0, "accuracy": None,
                "historical_graded": 0, "historical_correct": 0, "historical_errors": 0,
                "historical_cancelled": 0, "historical_accuracy": None,
                "historical_today_graded": 0, "historical_today_correct": 0,
                "historical_today_errors": 0, "historical_today_cancelled": 0,
                "historical_today_accuracy": None,
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
            stat["historical_today_graded"] = historical.get("today_graded", 0) or 0
            stat["historical_today_correct"] = historical.get("today_correct", 0) or 0
            stat["historical_today_errors"] = historical.get("today_errors", 0) or 0
            stat["historical_today_cancelled"] = historical.get("today_cancelled", 0) or 0
            if stat["historical_graded"]:
                stat["historical_accuracy"] = round(
                    stat["historical_correct"] * 100 / stat["historical_graded"], 1
                )
            if stat["historical_today_graded"]:
                stat["historical_today_accuracy"] = round(
                    stat["historical_today_correct"] * 100 / stat["historical_today_graded"], 1
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
            today_start, tomorrow_start = utc_day_bounds()
            aggregate = conn.execute("""SELECT gateway_id, MAX(gateway_name) gateway_name,
                SUM(CASE WHEN status = 'graded' THEN 1 ELSE 0 END) graded,
                SUM(CASE WHEN status = 'graded' AND is_correct = 1 THEN 1 ELSE 0 END) correct,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) errors,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) cancelled,
                SUM(CASE WHEN status = 'graded' AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_graded,
                SUM(CASE WHEN status = 'graded' AND is_correct = 1 AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_correct,
                SUM(CASE WHEN status = 'error' AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_errors,
                SUM(CASE WHEN status = 'cancelled' AND created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) today_cancelled
                FROM test_runs GROUP BY gateway_id ORDER BY gateway_name COLLATE NOCASE""", (
                    today_start, tomorrow_start, today_start, tomorrow_start,
                    today_start, tomorrow_start, today_start, tomorrow_start
                )).fetchall()
            recent = conn.execute("""SELECT r.*, j.engine FROM test_runs r JOIN test_jobs j ON j.id=r.job_id
                                  ORDER BY r.id DESC LIMIT 100""").fetchall()
        sites = []
        for row in aggregate:
            d = dict(row)
            d["accuracy"] = round(d["correct"] * 100 / d["graded"], 1) if d["graded"] else None
            d["today_accuracy"] = (
                round(d["today_correct"] * 100 / d["today_graded"], 1)
                if d["today_graded"] else None
            )
            sites.append(d)
        return {
            "gateways": sites,
            "runs": [
                {**dict(row), "error_kind": classify_error(row["error"]) if row["status"] == "error" else None}
                for row in recent
            ],
        }

    def clear_gateway_history(self, gateway_id: int) -> int:
        """Delete persisted runs for one gateway while preserving its configuration."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM test_runs WHERE gateway_id=?", (gateway_id,)
            ).rowcount
            conn.execute("COMMIT")
        return int(deleted)

    def clear_history(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM test_runs")
            conn.execute("DELETE FROM test_jobs")
            conn.execute("COMMIT")
