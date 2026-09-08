from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import DEFAULT_TIMEOUT_SECONDS, ExpectedAnswer, question_prompt, random_candy_prompts
from .cli import InvocationCancelled, answer_is_correct, invoke
from .storage import Database, utcnow


class JobManager:
    """One process-local scheduler: sites may overlap, rounds within a site may not."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._active_id: int | None = None
        self._cancel_event: threading.Event | None = None
        self._sync_reserved = False

    def reserve_sync(self) -> bool:
        """Atomically reserve the process for one WebDAV operation."""
        with self._lock:
            if self._sync_reserved or self._active_id is not None or self.db.active_job() is not None:
                return False
            self._sync_reserved = True
            return True

    def release_sync(self) -> None:
        with self._lock:
            self._sync_reserved = False

    def sync_reserved(self) -> bool:
        with self._lock:
            return self._sync_reserved

    def start(self, engine: str, mode: str, rounds: int, effort: str,
              model_override: str | None, gateways: list[dict[str, Any]],
              proxy_url: str | None = None, question_id: str = "candy",
              random_candy_format: bool | str = False) -> int:
        with self._lock:
            if self._sync_reserved:
                raise RuntimeError("WebDAV 同步进行中，暂时不能启动测试")
            if self._active_id is not None or self.db.active_job() is not None:
                raise RuntimeError("已有测试任务正在运行")
            snapshot = [{"id": site["id"], "name": site["name"], "multiplier": site.get("multiplier")} for site in gateways]
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
                args=(job_id, engine, mode, rounds, effort, model_override, gateways,
                      cancel_event, proxy_url, question_id, random_candy_format),
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
                 proxy_url: str | None = None, question_id: str = "candy",
                 random_candy_format: bool | str = False) -> None:
        # A default keeps direct unit-level calls backwards compatible.
        cancel_event = cancel_event or threading.Event()
        with self._lock:
            self.db.set_job_status(job_id, "cancelling" if cancel_event.is_set() else "running")
        final_status = "completed"
        final_error: str | None = None
        try:
            questions = (
                random_candy_prompts(rounds, random_candy_format) if question_id == "candy"
                else [question_prompt(question_id) for _ in range(rounds)]
            )
            timeout = DEFAULT_TIMEOUT_SECONDS
            if mode == "parallel":
                with ThreadPoolExecutor(max_workers=len(gateways), thread_name_prefix="candytest") as pool:
                    futures = [
                        pool.submit(
                            self._run_gateway, job_id, engine, effort,
                            model_override, gateway, questions, cancel_event, proxy_url,
                            timeout,
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
                        job_id, engine, effort, model_override, gateway, questions,
                        cancel_event, proxy_url, timeout,
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

    def _run_gateway(self, job_id: int, engine: str, effort: str,
                     model_override: str | None, gateway: dict[str, Any],
                     questions: list[tuple[str, ExpectedAnswer]],
                     cancel_event: threading.Event | None = None,
                     proxy_url: str | None = None,
                     timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        cancel_event = cancel_event or threading.Event()
        # Deliberately sequential: providers commonly rate-limit one API key/model.
        for number, (prompt, expected) in enumerate(questions, 1):
            if cancel_event.is_set():
                break
            try:
                result = invoke(
                    engine, gateway, model_override or gateway["model"], effort,
                    timeout, cancel_event, proxy_url, prompt,
                )
                answer = result.get("answer", "")
                run = {
                    "job_id": job_id, "gateway_id": gateway["id"],
                    "gateway_name": gateway["name"], "round_number": number,
                    "status": "graded", "answer": answer,
                    "is_correct": int(answer_is_correct(answer, expected)),
                    "elapsed_seconds": result.get("elapsed_seconds"),
                    "input_tokens": result.get("input_tokens"),
                    "output_tokens": result.get("output_tokens"),
                    "reasoning_tokens": result.get("reasoning_tokens"),
                    "total_tokens": result.get("total_tokens"),
                    "error": None, "created_at": utcnow(),
                }
            except InvocationCancelled:
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
