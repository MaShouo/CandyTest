from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import DEFAULT_TIMEOUT_SECONDS
from .cli import ANSWER_PATTERN, InvocationCancelled, invoke
from .storage import Database, utcnow


class JobManager:
    """One process-local scheduler: sites may overlap, rounds within a site may not."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._active_id: int | None = None
        self._cancel_event: threading.Event | None = None

    def start(self, engine: str, mode: str, rounds: int, effort: str,
              model_override: str | None, gateways: list[dict[str, Any]],
              proxy_url: str | None = None) -> int:
        with self._lock:
            if self._active_id is not None or self.db.active_job() is not None:
                raise RuntimeError("已有测试任务正在运行")
            snapshot = [{"id": site["id"], "name": site["name"]} for site in gateways]
            job_id = self.db.create_job({
                "engine": engine, "mode": mode, "rounds": rounds,
                "reasoning_effort": effort, "model_override": model_override,
                "gateway_snapshot": json.dumps(snapshot, ensure_ascii=False),
                "started_at": utcnow(),
            })
            cancel_event = threading.Event()
            self._active_id = job_id
            self._cancel_event = cancel_event
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id, engine, mode, rounds, effort, model_override, gateways, cancel_event, proxy_url),
                daemon=True,
            )
            thread.start()
            return job_id

    def cancel(self, job_id: int) -> bool:
        """Request cancellation; active CLI process trees stop within one poll interval."""
        with self._lock:
            if self._active_id != job_id or self._cancel_event is None:
                return False
            self._cancel_event.set()
            self.db.set_job_status(job_id, "cancelling")
            return True

    def _run_job(self, job_id: int, engine: str, mode: str, rounds: int, effort: str,
                 model_override: str | None, gateways: list[dict[str, Any]],
                 cancel_event: threading.Event | None = None,
                 proxy_url: str | None = None) -> None:
        # A default keeps direct unit-level calls backwards compatible.
        cancel_event = cancel_event or threading.Event()
        with self._lock:
            self.db.set_job_status(job_id, "cancelling" if cancel_event.is_set() else "running")
        final_status = "completed"
        final_error: str | None = None
        try:
            if mode == "parallel":
                with ThreadPoolExecutor(max_workers=len(gateways), thread_name_prefix="candytest") as pool:
                    futures = [
                        pool.submit(
                            self._run_gateway, job_id, engine, rounds, effort,
                            model_override, gateway, cancel_event, proxy_url,
                        )
                        for gateway in gateways
                    ]
                    for future in futures:
                        future.result()
            else:
                for gateway in gateways:
                    if cancel_event.is_set():
                        break
                    self._run_gateway(
                        job_id, engine, rounds, effort, model_override, gateway,
                        cancel_event, proxy_url,
                    )
            if cancel_event.is_set():
                final_status = "cancelled"
        except Exception as exc:  # individual invocation errors are recorded below
            if cancel_event.is_set():
                final_status = "cancelled"
            else:
                final_status = "failed"
                final_error = f"调度器异常：{str(exc)[:500]}"
        finally:
            with self._lock:
                self.db.set_job_status(job_id, final_status, final_error)
                if self._active_id == job_id:
                    self._active_id = None
                    self._cancel_event = None

    def _run_gateway(self, job_id: int, engine: str, rounds: int, effort: str,
                     model_override: str | None, gateway: dict[str, Any],
                     cancel_event: threading.Event | None = None,
                     proxy_url: str | None = None) -> None:
        cancel_event = cancel_event or threading.Event()
        # Deliberately sequential: providers commonly rate-limit one API key/model.
        for number in range(1, rounds + 1):
            if cancel_event.is_set():
                break
            try:
                result = invoke(
                    engine, gateway, model_override or gateway["model"], effort,
                    DEFAULT_TIMEOUT_SECONDS, cancel_event, proxy_url,
                )
                answer = result.get("answer", "")
                run = {
                    "job_id": job_id, "gateway_id": gateway["id"],
                    "gateway_name": gateway["name"], "round_number": number,
                    "status": "graded", "answer": answer,
                    "is_correct": int(bool(ANSWER_PATTERN.search(answer))),
                    "elapsed_seconds": result.get("elapsed_seconds"),
                    "input_tokens": result.get("input_tokens"),
                    "output_tokens": result.get("output_tokens"),
                    "reasoning_tokens": result.get("reasoning_tokens"),
                    "total_tokens": result.get("total_tokens"),
                    "error": None, "created_at": utcnow(),
                }
            except InvocationCancelled:
                run = {
                    "job_id": job_id, "gateway_id": gateway["id"],
                    "gateway_name": gateway["name"], "round_number": number,
                    "status": "cancelled", "answer": None, "is_correct": None,
                    "elapsed_seconds": None, "input_tokens": None,
                    "output_tokens": None, "reasoning_tokens": None,
                    "total_tokens": None, "error": "用户已中断测试",
                    "created_at": utcnow(),
                }
                self.db.add_run(run)
                break
            except Exception as exc:
                # cli.invoke already removes the supplied key from subprocess output.
                message = str(exc).replace(gateway["api_key"], "[已脱敏 API Key]")[:4000]
                run = {
                    "job_id": job_id, "gateway_id": gateway["id"],
                    "gateway_name": gateway["name"], "round_number": number,
                    "status": "error", "answer": None, "is_correct": None,
                    "elapsed_seconds": None, "input_tokens": None,
                    "output_tokens": None, "reasoning_tokens": None,
                    "total_tokens": None, "error": message, "created_at": utcnow(),
                }
            self.db.add_run(run)
