from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import DEFAULT_TIMEOUT_SECONDS, PROMPT

ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")
SECRET_ENV = "CANDYTEST_GATEWAY_API_KEY"


class InvocationCancelled(RuntimeError):
    """Raised when the user requests cancellation of an active CLI call."""


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort termination of a CLI wrapper and all of its children."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def _run_cancellable(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int,
    cancel_event: threading.Event,
) -> subprocess.CompletedProcess[str]:
    if cancel_event.is_set():
        raise InvocationCancelled("用户已中断测试")
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        **popen_options,
    )
    deadline = time.monotonic() + timeout
    pending_input: str | None = PROMPT
    while True:
        try:
            stdout, stderr = proc.communicate(input=pending_input, timeout=0.2)
            return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            pending_input = None
            if cancel_event.is_set():
                _terminate_process_tree(proc)
                proc.communicate()
                raise InvocationCancelled("用户已中断测试")
            if time.monotonic() >= deadline:
                _terminate_process_tree(proc)
                proc.communicate()
                raise RuntimeError(f"调用超时（{timeout} 秒）")


def resolve_executable(engine: str) -> str | None:
    candidates = ((f"{engine}.cmd", f"{engine}.exe", engine) if os.name == "nt" else (engine,))
    return next((path for name in candidates if (path := shutil.which(name))), None)


def cli_availability() -> dict[str, bool]:
    return {"pi": bool(resolve_executable("pi")), "codex": bool(resolve_executable("codex"))}


def redact(text: str | None, secret: str) -> str | None:
    if not text:
        return text
    return text.replace(secret, "[已脱敏 API Key]") if secret else text


def _number(data: Any, *keys: str) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _assistant_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")


def parse_pi_jsonl(stdout: str) -> dict[str, Any]:
    answer = ""
    usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"message_end", "turn_end"}:
            message = event.get("message")
            answer = _assistant_text(message) or answer
            current = message.get("usage") if isinstance(message, dict) else None
            usage = {"input_tokens": _number(current, "input", "input_tokens"),
                     "output_tokens": _number(current, "output", "output_tokens"),
                     "total_tokens": _number(current, "totalTokens", "total_tokens", "total")}
        elif event.get("type") == "agent_end":
            for message in reversed(event.get("messages", [])):
                found = _assistant_text(message)
                if found:
                    answer = found
                    break
    if usage["total_tokens"] is None and usage["input_tokens"] is not None and usage["output_tokens"] is not None:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return {"answer": answer, **usage, "reasoning_tokens": None}


def parse_codex_jsonl(stdout: str) -> dict[str, Any]:
    answer = ""
    usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "total_tokens": None}
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                answer = item["text"]
        elif event.get("type") == "turn.completed":
            current = event.get("usage")
            usage = {"input_tokens": _number(current, "input_tokens", "input"),
                     "output_tokens": _number(current, "output_tokens", "output"),
                     "reasoning_tokens": _number(current, "reasoning_output_tokens", "reasoning_tokens"),
                     "total_tokens": _number(current, "total_tokens", "total")}
    if usage["total_tokens"] is None and usage["input_tokens"] is not None and usage["output_tokens"] is not None:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return {"answer": answer, **usage}


def _pi_models_config(base_url: str, model: str) -> str:
    # The value is an environment-variable reference, never the real key.
    # Mark the custom Responses model as reasoning-capable so the selected pi
    # thinking level is forwarded instead of silently clamped to off.
    thinking_levels = {
        "off": "off", "minimal": "minimal", "low": "low", "medium": "medium",
        "high": "high", "xhigh": "xhigh", "max": "max",
    }
    return json.dumps({"providers": {"candytest": {
        "api": "openai-responses", "baseUrl": base_url,
        "apiKey": f"${{{SECRET_ENV}}}",
        "models": [{
            "id": model, "name": model, "reasoning": True,
            "thinkingLevelMap": thinking_levels,
        }],
    }}}, ensure_ascii=False)


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _codex_config(base_url: str) -> str:
    return "\n".join((
        'model_provider = "candytest"',
        "[model_providers.candytest]",
        'name = "CandyTest Responses Gateway"',
        f"base_url = {_toml_quote(base_url)}",
        'env_key = "CANDYTEST_GATEWAY_API_KEY"',
        'wire_api = "responses"',
        "",
    ))


def invoke(engine: str, gateway: dict[str, Any], model: str, effort: str,
           timeout: int = DEFAULT_TIMEOUT_SECONDS,
           cancel_event: threading.Event | None = None,
           proxy_url: str | None = None) -> dict[str, Any]:
    """Execute exactly one isolated CLI test. This function never logs a secret."""
    executable = resolve_executable(engine)
    if not executable:
        raise RuntimeError(f"未找到 {engine} CLI，请安装后重新启动应用")
    key = gateway["api_key"]
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"candytest-{engine}-") as temp:
        root = Path(temp)
        cwd = root / "work"
        cwd.mkdir()
        env = os.environ.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                     "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            env.pop(name, None)
        if proxy_url:
            # pi and Codex both honor these standard variables. HTTPS targets
            # still use an HTTP CONNECT tunnel through Clash's HTTP port.
            env["HTTP_PROXY"] = proxy_url
            env["HTTPS_PROXY"] = proxy_url
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url
        env[SECRET_ENV] = key
        if engine == "pi":
            config_dir = root / "pi-config"
            config_dir.mkdir()
            (config_dir / "models.json").write_text(_pi_models_config(gateway["base_url"], model), encoding="utf-8")
            session_dir = root / "session"
            env["PI_CODING_AGENT_DIR"] = str(config_dir)
            command = [executable, "--mode", "json", "--print", "--provider", "candytest", "--model", model,
                       "--thinking", effort, "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
                       "--no-context-files", "--no-approve", "--session-dir", str(session_dir)]
            parser = parse_pi_jsonl
        elif engine == "codex":
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(_codex_config(gateway["base_url"]), encoding="utf-8")
            env["CODEX_HOME"] = str(codex_home)
            command = [executable, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                       "--disable", "memories", "-c", "model_provider=candytest", "-c", f"model_reasoning_effort={effort}",
                       "-m", model]
            parser = parse_codex_jsonl
        else:
            raise RuntimeError("不支持的 CLI 引擎")
        try:
            if cancel_event is None:
                proc = subprocess.run(
                    command, input=PROMPT, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=cwd, env=env, timeout=timeout,
                )
            else:
                proc = _run_cancellable(
                    command, cwd=cwd, env=env, timeout=timeout, cancel_event=cancel_event,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"调用超时（{timeout} 秒）") from exc
        finally:
            env.pop(SECRET_ENV, None)
        elapsed = time.perf_counter() - started
        if cancel_event is not None and cancel_event.is_set():
            raise InvocationCancelled("用户已中断测试")
        if proc.returncode != 0:
            detail = redact((proc.stderr or proc.stdout or "CLI 调用失败").strip(), key)
            raise RuntimeError(detail or "CLI 调用失败")
        parsed = parser(proc.stdout)
        parsed["elapsed_seconds"] = elapsed
        return parsed
