"""Offline regression tests for CandyTest.

All CLI execution is replaced with mocks; no test contacts a gateway or needs an
installed pi/Codex executable.  The suite uses only unittest so it can run once
Flask (the application dependency) has been installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import lru_cache
from itertools import product
from pathlib import Path
from unittest.mock import patch

from candytest import (
    DAG_PROMPT, ORIGINAL_CANDY_PROMPT, PROBABILITY_PROMPT, PROMPT_TEMPLATES,
    QUESTION_DEFAULTS, QUESTION_NAMES,
    candy_prompt, question_prompt, random_candy_prompt, random_candy_prompts,
)
from candytest import cli
from candytest.jobs import JobManager
from candytest.cli import InvocationCancelled
from candytest.storage import Database, classify_error, utcnow


SECRET = "dummy-test-key"
PROXY = "http://localhost:7890"


def gateway(gateway_id: int = 1, name: str = "站点 A") -> dict:
    return {
        "id": gateway_id,
        "name": name,
        "base_url": "https://gateway.example/v1",
        "api_key": SECRET,
        "model": "test-model",
        "multiplier": 1,
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
    def test_dynamic_prompt_computes_expected_answer(self):
        prompt, expected = candy_prompt((7, 9, 8, 7, 6, 4))
        self.assertEqual(expected, "21")
        self.assertIn("最后一行必须严格写成 FINAL: <整数>。", prompt)
        self.assertFalse(cli.answer_is_correct("中间计算得到 21\nFINAL: 22", expected))
        self.assertTrue(cli.answer_is_correct("推理完成\nFINAL: 21", expected))
        for term in ("ITEM", "ALFA", "BRAV", "CHAR", "FORM", "MODE"):
            self.assertIn(term, prompt)
        self.assertIn("不同形态靠手感可以分辨", prompt)
        self.assertIn("参赛者需要在活动前决定取出的", prompt)
        for explicit_hint in ("类别只能在取出后确认", "可据此决定", "两种形态各取多少件"):
            self.assertNotIn(explicit_hint, prompt)

    def test_all_prompt_templates_preserve_semantics_and_hidden_clue(self):
        for template in PROMPT_TEMPLATES:
            with self.subTest(template=template[:20]):
                prompt, expected = candy_prompt((1, 2, 3, 4, 5, 6), template=template)
                self.assertEqual(expected, "13")
                self.assertTrue(any(clue in prompt for clue in ("手感", "触感", "摸起来")))
                for term in ("ITEM", "ALFA", "BRAV", "CHAR", "FORM", "MODE"):
                    self.assertIn(term, prompt)
                for number in range(1, 7):
                    self.assertIn(str(number), prompt)
                for explicit_hint in ("类别只能在取出后确认", "可据此决定", "两种形态各取多少件"):
                    self.assertNotIn(explicit_hint, prompt)
                for keyword in ("糖果", "苹果", "桃子", "草莓", "西瓜", "圆形", "五角星"):
                    self.assertNotIn(keyword, prompt)

    def test_fixed_reasoning_questions_preserve_prompts_and_answers(self):
        self.assertEqual(question_prompt("probability"), (PROBABILITY_PROMPT, "319/1728"))
        self.assertEqual(question_prompt("dag10"), (DAG_PROMPT, "666"))
        self.assertIn("A 优先", PROBABILITY_PROMPT)
        self.assertIn("G、H 都在 I 前", DAG_PROMPT)
        with self.assertRaisesRegex(ValueError, "不支持的题目"):
            question_prompt("missing")

    def test_knowledge_questions_preserve_prompts_and_grade_whole_answers(self):
        cases = (
            ("thibault_sottiaux", "don't search the internet, do you know Thibault Sottiaux on X. answer yes or no", "yes"),
            ("johannes_heidecke", "don't search the internet, do you know Johannes Heidecke on X. answer yes or no", "yes"),
            ("sam_mccandlish", "don't search the internet, do you know Sam McCandlish on X. answer yes or no", "yes"),
        )
        for question_id, original, correct in cases:
            with self.subTest(question=question_id):
                prompt, expected = question_prompt(question_id)
                self.assertEqual(prompt, original)
                self.assertEqual(QUESTION_DEFAULTS[question_id], (5, "low"))
                self.assertTrue(cli.answer_is_correct(correct, expected))
                self.assertTrue(cli.answer_is_correct("Yes.", expected))
                self.assertTrue(cli.answer_is_correct(f" \n{correct.upper()}\n", expected))
                for wrong in ("", "no", "No.", "yes or no", "yes, I know", "UNKNOWN", f"Not {correct}", f"{correct}\nUNKNOWN", f"FINAL: {correct}"):
                    self.assertFalse(cli.answer_is_correct(wrong, expected))

    def test_removed_questions_are_unavailable(self):
        for question_id in ("cup", "gpt5_release", "nobel_peace_2025", "booker_2025", "nobel_physics_2025", "venice_golden_lion_2025"):
            with self.subTest(question=question_id):
                self.assertNotIn(question_id, QUESTION_NAMES)
                self.assertNotIn(question_id, QUESTION_DEFAULTS)
                with self.assertRaisesRegex(ValueError, "不支持的题目"):
                    question_prompt(question_id)

    def test_thibault_sottiaux_yes_passes_no_fails(self):
        _, expected = question_prompt("thibault_sottiaux")
        for answer in ("yes", "Yes", "YES", "Yes.", " \nyes\n "):
            with self.subTest(answer=answer):
                self.assertTrue(cli.answer_is_correct(answer, expected))
        for answer in ("no", "No", "NO", "No.", " \nno\n ", "yes or no", "yes\nno", "yes, I know him", ""):
            with self.subTest(answer=answer):
                self.assertFalse(cli.answer_is_correct(answer, expected))

    @staticmethod
    def adaptive_draw_count(counts):
        # Independent game oracle: choose a shape, then an adversary chooses
        # any category still present in that shape. Observe it before choosing
        # again. The state records remaining stocks, not fixed shape quotas.
        @lru_cache(maxsize=None)
        def remaining_draws(remaining):
            if ((remaining[0] < counts[0] and remaining[4] < counts[4])
                    or (remaining[1] < counts[1] and remaining[3] < counts[3])):
                return 0
            shape_costs = []
            for indices in ((0, 1, 2), (3, 4, 5)):
                outcomes = []
                for index in indices:
                    if remaining[index]:
                        next_state = list(remaining)
                        next_state[index] -= 1
                        outcomes.append(remaining_draws(tuple(next_state)))
                if outcomes:
                    shape_costs.append(1 + max(outcomes))
            return min(shape_costs, default=float("inf"))

        return remaining_draws(counts)

    @staticmethod
    def fixed_quota_draw_count(counts):
        a1, a2, ao, b1, b2, bo = counts
        return min(
            a + b
            for a in range(sum(counts[:3]) + 1)
            for b in range(sum(counts[3:]) + 1)
            if a > ao and b > bo
            and (a > a2 + ao or b > b2 + bo)
            and (a > a1 + ao or b > b1 + bo)
        )

    def test_dynamic_answers_match_both_small_strategy_oracles(self):
        for counts in product((1, 2, 3), repeat=6):
            with self.subTest(counts=counts):
                expected = candy_prompt(counts)[1]
                accepted = {expected} if isinstance(expected, str) else set(expected)
                self.assertEqual(accepted, {
                    str(self.adaptive_draw_count(counts)),
                    str(self.fixed_quota_draw_count(counts)),
                })

    def test_adaptive_answer_regressions(self):
        cases = (
            ((12, 1, 11, 10, 20, 17), 40, 41),
            ((12, 2, 11, 10, 20, 17), 40, 42),
            ((2, 1, 1, 1, 2, 1), 5, 6),
        )
        terms = ("ITEM", "MIRS", "YUXK", "FTPG", "VQWH", "NZAJ")
        for counts, correct, fixed_quota_answer in cases:
            self.assertEqual(self.adaptive_draw_count(counts), correct)
            swapped_shapes = counts[3:] + counts[:3]
            swapped_targets = (counts[1], counts[0], counts[2], counts[4], counts[3], counts[5])
            for inventory in (counts, swapped_shapes, swapped_targets):
                for template in PROMPT_TEMPLATES:
                    with self.subTest(counts=inventory, template=template[:20]):
                        _, expected = candy_prompt(inventory, terms, template)
                        self.assertEqual(expected, frozenset((str(correct), str(fixed_quota_answer))))
                        self.assertTrue(cli.answer_is_correct(f"推理完成\nFINAL: {correct}", expected))
                        self.assertTrue(cli.answer_is_correct(f"推理完成\nFINAL: {fixed_quota_answer}", expected))
                        for wrong in range(correct - 1, fixed_quota_answer + 2):
                            if wrong not in (correct, fixed_quota_answer):
                                self.assertFalse(cli.answer_is_correct(f"FINAL: {wrong}", expected))
                        for malformed in (
                            str(correct), f"中间得到 {correct} 或 {fixed_quota_answer}",
                            f"FINAL: {correct}\n更正：0", f"FINAL: {correct} 或 {fixed_quota_answer}",
                            f"中间得到 {correct}\nFINAL: 0",
                        ):
                            self.assertFalse(cli.answer_is_correct(malformed, expected))

    def test_random_prompt_replaces_keywords_and_draws_six_counts(self):
        letters = list("ABCDEFGHIJKLMNOPQRSTUVWX")
        with patch("candytest.random.sample", return_value=letters) as sample, \
             patch("candytest.random.randint", side_effect=(1, 2, 3, 4, 5, 6)) as randint, \
             patch("candytest.random.choice", return_value=PROMPT_TEMPLATES[2]) as choice:
            prompt, expected = random_candy_prompt()
        sample.assert_called_once_with("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 24)
        choice.assert_called_once_with(PROMPT_TEMPLATES)
        self.assertEqual([item.args for item in randint.call_args_list], [(1, 20)] * 6)
        self.assertEqual(expected, "13")
        for term in ("ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX"):
            self.assertIn(term, prompt)
        for keyword in ("糖果", "苹果", "桃子", "草莓", "西瓜", "圆形", "五角星"):
            self.assertNotIn(keyword, prompt)

    def test_random_rounds_share_template_keywords_and_counts(self):
        letters = list("ABCDEFGHIJKLMNOPQRSTUVWX")
        with patch("candytest.random.sample", return_value=letters) as sample, \
             patch("candytest.random.choice", return_value=PROMPT_TEMPLATES[2]) as choice, \
             patch("candytest.random.randint", side_effect=range(1, 7)) as randint:
            questions = random_candy_prompts(2, True)
        sample.assert_called_once_with("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 24)
        choice.assert_called_once_with(PROMPT_TEMPLATES)
        self.assertEqual(len(randint.call_args_list), 6)
        self.assertEqual(questions[0], questions[1])
        prompt, expected = questions[0]
        self.assertEqual(expected, "13")
        for term in ("ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX"):
            self.assertIn(term, prompt)
        self.assertIn("一只遮光袋内装有", prompt)
        for number in range(1, 7):
            self.assertIn(f"{number:2}", prompt)

    def test_original_format_randomizes_data_once_and_keeps_first_template(self):
        letters = list("ABCDEFGHIJKLMNOPQRSTUVWX")
        with patch("candytest.random.sample", return_value=letters), \
             patch("candytest.random.randint", side_effect=(7, 9, 8, 7, 6, 4)) as randint, \
             patch("candytest.random.choice") as choice:
            questions = random_candy_prompts(3, False)
        self.assertEqual(len(randint.call_args_list), 6)
        choice.assert_not_called()
        self.assertEqual(questions, [questions[0]] * 3)
        self.assertEqual(questions[0][1], "21")
        self.assertEqual(
            questions[0][0],
            candy_prompt((7, 9, 8, 7, 6, 4),
                         ("ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX"),
                         PROMPT_TEMPLATES[0])[0],
        )

    def test_untouched_original_uses_no_random_replacements(self):
        with patch("candytest.random.sample") as sample, \
             patch("candytest.random.randint") as randint, \
             patch("candytest.random.choice") as choice:
            questions = random_candy_prompts(3, "original")
        sample.assert_not_called()
        randint.assert_not_called()
        choice.assert_not_called()
        expected_prompt = f"{ORIGINAL_CANDY_PROMPT}最后一行必须严格写成 FINAL: <整数>。\n"
        self.assertEqual(questions, [(expected_prompt, "21")] * 3)
        for original in ("糖果", "苹果味", "桃子味", "西瓜味", "圆形", "五角星形", "7", "9", "8", "6", "4"):
            self.assertIn(original, ORIGINAL_CANDY_PROMPT)

    def test_grading_matches_expected_integer_anywhere(self):
        self.assertTrue(cli.answer_is_correct("结果为 21。", 21))
        self.assertTrue(cli.answer_is_correct("21 不正确，最终是 22", 21))
        self.assertFalse(cli.answer_is_correct("121", 21))
        self.assertFalse(cli.answer_is_correct("210", 21))
        self.assertFalse(cli.answer_is_correct("", 21))

    def test_new_questions_grade_only_normalized_final_line(self):
        self.assertFalse(cli.answer_is_correct("中间猜测 666，但最终是 700", "666"))
        self.assertFalse(cli.answer_is_correct("FINAL: 666\n更正：700", "666"))
        self.assertFalse(cli.answer_is_correct("666", "666"))
        self.assertTrue(cli.answer_is_correct("证明完成\nFINAL: 666", "666"))
        self.assertTrue(cli.answer_is_correct(
            "证明完成\nFINAL: \\(\\frac{319}{1728}\\)", "319/1728",
        ))
        self.assertFalse(cli.answer_is_correct(
            "中间出现 319/1728\nFINAL: 2209/11664", "319/1728",
        ))

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

    def test_pi_retry_success_clears_prior_terminal_error(self):
        stdout = "\n".join((
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": "503 Service Unavailable",
            }}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "retry answer 21"}],
                "stopReason": "stop",
            }}),
            json.dumps({"type": "auto_retry_end", "success": True}),
        ))
        parsed = cli.parse_pi_jsonl(stdout)
        self.assertEqual(parsed["answer"], "retry answer 21")
        self.assertNotIn("_error", parsed)

    def test_invoke_rejects_zero_exit_pi_terminal_error_and_redacts_key(self):
        stdout = json.dumps({"type": "message_end", "message": {
            "role": "assistant", "content": [], "stopReason": "error",
            "errorMessage": f"503 Service Unavailable {SECRET}",
        }})
        with patch("candytest.cli.resolve_executable", return_value="fake-pi"), \
             patch("candytest.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
            with self.assertRaisesRegex(RuntimeError, "503 Service Unavailable") as raised:
                cli.invoke("pi", gateway(), "test-model", "medium", 1)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("已脱敏 API Key", str(raised.exception))

    def test_invoke_rejects_zero_exit_without_assistant_answer(self):
        with patch("candytest.cli.resolve_executable", return_value="fake-pi"), \
             patch("candytest.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", f"network error {SECRET}")):
            with self.assertRaisesRegex(RuntimeError, "network error") as raised:
                cli.invoke("pi", gateway(), "test-model", "medium", 1)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("已脱敏 API Key", str(raised.exception))

    def test_invoke_preserves_zero_exit_plaintext_gateway_error(self):
        with patch("candytest.cli.resolve_executable", return_value="fake-pi"), \
             patch("candytest.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "502 Bad Gateway", "")):
            with self.assertRaisesRegex(RuntimeError, "502 Bad Gateway"):
                cli.invoke("pi", gateway(), "test-model", "medium", 1)

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

    def test_parse_codex_jsonl_recognizes_failed_events_and_completed_turn_clears_transient_error(self):
        failures = (
            {"type": "turn.failed", "error": {"message": "503 Service Unavailable"}},
            {"type": "item.failed", "item": {"error": {"message": "ECONNREFUSED"}}},
            {"type": "error", "message": "Gateway Timeout"},
        )
        for event in failures:
            with self.subTest(event_type=event["type"]):
                parsed = cli.parse_codex_jsonl(json.dumps(event))
                self.assertIn("_error", parsed)
        transient = "\n".join((
            json.dumps(failures[1]),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "21"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ))
        parsed = cli.parse_codex_jsonl(transient)
        self.assertEqual(parsed["answer"], "21")
        self.assertNotIn("_error", parsed)

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
            self.assertEqual(kwargs["input"], "随机题目")
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with patch("candytest.cli.resolve_executable", side_effect=lambda engine: f"fake-{engine}"), \
             patch("candytest.cli.subprocess.run", side_effect=fake_run):
            self.assertEqual(cli.invoke("pi", gateway(), "override-model", "medium", 1, proxy_url=PROXY,
                                        prompt="随机题目")["answer"], "21")
            self.assertEqual(cli.invoke("codex", gateway(), "override-model", "medium", 1,
                                        prompt="随机题目")["answer"], "21")

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
        pi_provider = json.loads(captures[0][1])["providers"]["candytest"]
        self.assertEqual(pi_provider["headers"], {"User-Agent": cli.PI_USER_AGENT})
        self.assertEqual(
            cli.PI_USER_AGENT,
            "codex-tui/0.149.0 (Windows 10.0.26200; x86_64) WindowsTerminal (codex-tui; 0.149.0)",
        )
        pi_model = pi_provider["models"][0]
        self.assertTrue(pi_model["reasoning"])
        self.assertEqual(pi_model["thinkingLevelMap"]["xhigh"], "xhigh")
        self.assertIn("override-model", captures[1][0])  # Codex --model argument
        self.assertIn("--disable plugins", " ".join(captures[1][0]))

    def test_fake_executables_run_end_to_end_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_script = root / "fake_cli.py"
            fake_script.write_text(
                "import json, os, sys\n"
                "sys.stdin.reconfigure(encoding='utf-8', errors='replace')\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "prompt = sys.stdin.read()\n"
                "if not prompt.strip(): raise SystemExit(2)\n"
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
        self.assertIn('run.error_kind === "gateway_unavailable"', source)
        self.assertIn("中转站不可用", source)
        self.assertIn('text: "API 错误"', source)
        self.assertIn("API 错误", source)
        self.assertIn("selectedGatewayIds", source)
        self.assertIn('statCard(site, false, job.rounds, true)', source)
        self.assertIn('state.selectedGatewayIds.size === 0', source)
        self.assertIn('job.runs.filter(run => state.selectedGatewayIds.has(Number(run.gateway_id)))', source)
        self.assertIn("removeGatewayHistory", source)
        self.assertIn("删除记录", source)
        self.assertIn("/api/history/gateways/", source)
        self.assertIn("中转站配置不会删除", source)
        self.assertIn("是否同时删除", source)
        self.assertIn("delete_history=", source)
        self.assertIn("deleted_runs", source)
        self.assertIn("所选中转站暂无测试记录", source)
        self.assertIn("today_accuracy", source)
        self.assertIn("今日正确率", source)
        self.assertIn("今日：正确", source)
        self.assertIn("历史：正确", source)
        self.assertNotIn("site.cancelled", source)
        self.assertNotIn("site.today_cancelled", source)
        self.assertNotIn("site.historical_cancelled", source)
        self.assertNotIn("job.summary.cancelled", source)
        self.assertNotIn("今日：正确 ${site.today_correct || 0} / 已判分 ${site.today_graded || 0} · API 错误", source)
        self.assertNotIn("历史：正确 ${site.correct || 0} / 已判分 ${site.graded || 0} · API 错误", source)
        self.assertIn("stat-details-stacked", source)
        self.assertNotIn("const historyLow", source)
        self.assertNotIn("if (historyLow)", source)
        self.assertNotIn("不计正确率", source)
        self.assertIn('details[open][data-detail-key]', source)
        self.assertIn("d.open = opened.has(key)", source)
        self.assertIn('input[type=checkbox]:checked', source)
        self.assertIn("checkbox.checked = gateway.enabled && selected.has(gateway.id)", source)
        self.assertIn('gateway.api_key_saved ? (gateway.api_key_masked || "••••••••") : "未保存"', source)
        self.assertNotIn('已保存 ${gateway.api_key_masked', source)
        self.assertIn("drag-handle", source)
        self.assertIn("ondragstart", source)
        self.assertIn("ondrop", source)
        self.assertIn('["ArrowUp", "ArrowDown"]', source)
        self.assertIn("/api/gateways/reorder", source)
        self.assertIn("ondblclick", source)
        self.assertIn("editGatewayCell", source)
        self.assertIn('nameCell.dataset.field = "name"', source)
        self.assertIn('multiplierCell.dataset.field = "multiplier"', source)
        self.assertIn("inline-editor", source)
        self.assertIn('api_key: ""', source)
        self.assertIn("site.multiplier", source)
        self.assertIn("stat-multiplier", source)
        self.assertNotIn("倍率：", source)
        self.assertIn("await loadHistory();", source)
        self.assertIn("await refreshCurrent();", source)

        self.assertIn("multiplier: Number(f.multiplier.value)", source)
        self.assertIn('previous = preferred || select.value || "low"', source)
        self.assertIn('probability: { rounds: 5, effort: "medium" }', source)
        self.assertIn('dag10: { rounds: 5, effort: "medium" }', source)
        self.assertIn('question_id: f.question_id.value', source)
        self.assertIn('candyFormat === "original" ? "original" : candyFormat === "true"', source)
        self.assertIn('$("#randomCandyFormat").style.display = $("#question").value === "candy" ? "" : "none"', source)
        self.assertIn('$("#question").onchange = setQuestionDefaults', source)
        self.assertIn('e.code !== "WEBDAV_CONFLICT"', source)
        self.assertIn("Pull 不会备份", source)
        template = (Path(__file__).parents[1] / "candytest/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="question"', template)
        self.assertIn('<option value="candy">糖果题</option>', template)
        self.assertIn('<option value="probability">骰子概率题</option>', template)
        self.assertIn('<option value="dag10">任务排序题</option>', template)
        for removed in ("cup", "gpt5_release", "nobel_peace_2025", "booker_2025", "nobel_physics_2025", "venice_golden_lion_2025"):
            self.assertNotIn(removed, template)
            self.assertNotIn(removed, source)
        for question_id in ('thibault_sottiaux', 'johannes_heidecke', 'sam_mccandlish'):
            self.assertIn(f'<option value="{question_id}">{QUESTION_NAMES[question_id]}</option>', template)
            self.assertIn(f'{question_id}: {{ rounds: 5, effort: "low", model: "gpt-6-astra" }}', source)
        self.assertIn('id="randomCandyFormat"', template)
        self.assertIn('name="random_candy_format"', template)
        self.assertIn('<option value="false">原题结构（默认，随机数据）</option>', template)
        self.assertIn('<option value="original">完全原题（不替换）</option>', template)
        self.assertIn('<option value="true">随机格式</option>', template)
        self.assertIn(">糖果题格式<select", template)
        self.assertIn('id="rounds"', template)
        self.assertIn('id="selectAllGateways"', template)
        self.assertIn("正确/总次数", template)
        self.assertIn("<th>倍率</th>", template)
        self.assertIn('name="multiplier"', template)
        self.assertIn('aria-label="排序"', template)
        self.assertIn("历史正确率</th><th>今日正确率", template)
        self.assertIn('href="{{ url_for(\'settings\') }}"', template)
        self.assertIn('id="pushWebdav"', template)
        self.assertIn('id="pullWebdav"', template)
        self.assertNotIn('id="webdavInfo"', template)
        self.assertNotIn('id="webdavStatus"', template)
        self.assertNotIn('class="panel sync-panel"', template)
        self.assertNotIn('id="webdavForm"', template)
        self.assertNotIn('id="proxyForm"', template)
        self.assertNotIn("不计正确率", template)
        self.assertIn("API 错误仅显示在当前任务", template)
        style = (Path(__file__).parents[1] / "candytest/static/style.css").read_text(encoding="utf-8")
        self.assertRegex(style, r"\.stat-grid\s*\{\s*display:\s*grid;\s*grid-template-columns:\s*1fr")
        self.assertIn(".stat-details", style)
        self.assertIn(".stat-details-stacked", style)
        self.assertIn(".stat-alerts", style)
        self.assertIn(".stat-selectable", style)
        self.assertIn(".stat-selectable.selected", style)
        self.assertIn(".stat-selectable.not-selected", style)
        self.assertIn(".stat-actions", style)
        self.assertIn(".stat-multiplier", style)
        self.assertRegex(style, r"text-align:\s*right")
        self.assertIn("4.5rem 12rem", style)
        self.assertIn(".drag-handle", style)
        self.assertIn(".gateway-row.drag-over", style)
        self.assertIn(".inline-editable", style)
        self.assertIn(".inline-editor", style)
        self.assertNotIn('innerHTML', source)
        self.assertIn('syncGatewaySelectAll', source)
        self.assertIn('input[type=checkbox]:not(:disabled)', source)
        self.assertIn('e.code !== "WEBDAV_CONFLICT"', source)
        self.assertIn("Pull 不会备份", source)
        settings = (Path(__file__).parents[1] / "candytest/templates/settings.html").read_text(encoding="utf-8")
        settings_source = (Path(__file__).parents[1] / "candytest/static/settings.js").read_text(encoding="utf-8")
        self.assertIn('id="webdavForm"', settings)
        self.assertIn('id="proxyForm"', settings)
        self.assertIn("返回主界面", settings)
        self.assertIn('src="{{ url_for(\'static\', filename=\'settings.js\') }}"', settings)
        self.assertNotIn('pushWebdav', settings)
        self.assertNotIn('pullWebdav', settings)
        self.assertNotIn("innerHTML", settings_source)
        self.assertNotIn('webdavAutoPull', settings)
        self.assertNotIn('webdavAutoPush', settings)
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
            "model": "model-a", "multiplier": 1, "enabled": 1,
        })

    def test_gateway_migration_adds_default_multiplier_and_sort_order(self):
        legacy_dir = Path(self.temp.name) / "legacy"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "candytest.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript("""
                CREATE TABLE gateways (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL, model TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE test_jobs (status TEXT NOT NULL, completed_at TEXT, error TEXT);
                CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                PRAGMA user_version = 2;
            """)
            conn.executemany(
                "INSERT INTO gateways (id,name,base_url,api_key,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                ((7, "后建", "https://seven.test/v1", SECRET, "model", utcnow(), utcnow()),
                 (2, "先显示", "https://two.test/v1", SECRET, "model", utcnow(), utcnow())),
            )
            conn.commit()
        finally:
            conn.close()

        migrated = Database(legacy_dir)
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(gateways)")}
            self.assertEqual(columns["multiplier"]["dflt_value"], "1")
            self.assertEqual(columns["sort_order"]["dflt_value"], "0")
        self.assertEqual([site["id"] for site in migrated.gateways()], [2, 7])
        self.assertEqual([site["multiplier"] for site in migrated.gateways()], [1.0, 1.0])
        created = migrated.create_gateway({
            "name": "末尾", "base_url": "https://last.test/v1", "api_key": SECRET,
            "model": "model", "multiplier": 1, "enabled": 1,
        })
        self.assertEqual([site["id"] for site in migrated.gateways()], [2, 7, created["id"]])

    def test_partial_v3_gateway_migration_recovers(self):
        partial_dir = Path(self.temp.name) / "partial"
        partial_dir.mkdir()
        conn = sqlite3.connect(partial_dir / "candytest.sqlite3")
        try:
            conn.executescript("""
                CREATE TABLE gateways (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL, model TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    multiplier REAL NOT NULL DEFAULT 1
                );
                CREATE TABLE test_jobs (status TEXT NOT NULL, completed_at TEXT, error TEXT);
                CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                PRAGMA user_version = 2;
            """)
            conn.commit()
        finally:
            conn.close()

        recovered = Database(partial_dir)
        with recovered.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(gateways)")}
        self.assertIn("multiplier", columns)
        self.assertIn("sort_order", columns)

    def test_new_gateway_is_inserted_by_name_initial(self):
        alpha = self.create_site("Alpha")
        charlie = self.create_site("Charlie")
        bravo = self.create_site("bravo")
        self.assertEqual(
            [site["id"] for site in self.db.gateways()],
            [alpha["id"], bravo["id"], charlie["id"]],
        )

        self.assertTrue(self.db.reorder_gateways([charlie["id"], alpha["id"], bravo["id"]]))
        beta = self.create_site("Beta 2")
        ordered = [site["id"] for site in self.db.gateways()]
        self.assertEqual(ordered, [beta["id"], charlie["id"], alpha["id"], bravo["id"]])
        self.assertEqual([item for item in ordered if item != beta["id"]], [charlie["id"], alpha["id"], bravo["id"]])

    def test_gateway_reorder_persists_and_rejects_partial_lists(self):
        first = self.create_site("A一")
        second = self.create_site("A二")
        third = self.create_site("A三")
        self.assertEqual([site["id"] for site in self.db.gateways()], [first["id"], second["id"], third["id"]])
        ordered = [third["id"], first["id"], second["id"]]
        self.assertTrue(self.db.reorder_gateways(ordered))
        self.assertEqual([site["id"] for site in self.db.gateways()], ordered)
        self.assertFalse(self.db.reorder_gateways(ordered[:-1]))
        self.assertEqual([site["id"] for site in Database(Path(self.temp.name)).gateways()], ordered)

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
            "model": "model-b", "multiplier": 1, "enabled": 1,
        })
        self.assertEqual(saved["name"], "重命名")
        self.assertEqual(self.db.gateway_records([site["id"]])[0]["api_key"], SECRET)
        self.assertEqual(self.db.delete_gateway(site["id"]), (True, 0))
        self.assertEqual(self.db.gateways(), [])
        self.assertEqual(self.db.gateway_records([site["id"]]), [])

    def test_gateway_metadata_updates_current_and_history_display(self):
        site = self.create_site()
        job_payload_data = job_payload()
        job_payload_data["gateway_snapshot"] = json.dumps([
            {"id": site["id"], "name": "旧名称", "multiplier": 1}
        ])
        job_id = self.db.create_job(job_payload_data)
        self.db.add_run(run_payload(job_id, gateway_id=site["id"], name="旧名称"))

        self.db.update_gateway(site["id"], {
            "name": "新名称", "base_url": site["base_url"], "api_key": None,
            "model": site["model"], "multiplier": 0.08, "enabled": 1,
        })
        current = self.db.job(job_id)
        self.assertEqual(
            (current["gateways"][0]["gateway_name"], current["gateways"][0]["multiplier"]),
            ("新名称", 0.08),
        )
        self.assertEqual(current["runs"][0]["gateway_name"], "新名称")
        history = self.db.history()
        self.assertEqual(
            (history["gateways"][0]["gateway_name"], history["gateways"][0]["multiplier"]),
            ("新名称", 0.08),
        )
        self.assertEqual(history["runs"][0]["gateway_name"], "新名称")

    def test_history_excludes_api_errors(self):
        site = self.create_site()
        job_id = self.db.create_job(job_payload())
        self.db.add_run(run_payload(job_id, correct=1))
        self.db.add_run(run_payload(job_id, correct=0))
        self.db.add_run(run_payload(job_id, status="error", correct=None))
        self.db.add_run(run_payload(job_id, status="cancelled", correct=None))
        history = self.db.history()
        aggregate = history["gateways"][0]
        self.assertEqual(
            (aggregate["graded"], aggregate["correct"], aggregate["errors"]),
            (2, 1, 0),
        )
        self.assertNotIn("cancelled", aggregate)
        self.assertEqual(aggregate["accuracy"], 50.0)
        self.assertEqual(len(history["runs"]), 2)
        self.assertEqual({run["status"] for run in history["runs"]}, {"graded"})

    def test_history_includes_today_accuracy_using_utc_calendar_day(self):
        self.create_site()
        job_id = self.db.create_job(job_payload())
        now = datetime.now(timezone.utc)
        today = now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        yesterday = (now - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        ).isoformat()

        for status, correct, created_at in (
            ("graded", 1, today),
            ("graded", 0, today),
            ("error", None, today),
            ("cancelled", None, today),
            ("graded", 1, yesterday),
        ):
            run = run_payload(job_id, status=status, correct=correct)
            run["created_at"] = created_at
            self.db.add_run(run)

        aggregate = self.db.history()["gateways"][0]
        self.assertEqual(
            (aggregate["graded"], aggregate["correct"], aggregate["accuracy"]),
            (3, 2, 66.7),
        )
        self.assertEqual(
            (aggregate["today_graded"], aggregate["today_correct"], aggregate["today_accuracy"]),
            (2, 1, 50.0),
        )
        self.assertNotIn("cancelled", aggregate)
        self.assertNotIn("today_cancelled", aggregate)

        current = self.db.job(job_id)["gateways"][0]
        self.assertEqual(
            (current["historical_today_graded"], current["historical_today_correct"],
             current["historical_today_accuracy"]),
            (2, 1, 50.0),
        )
        self.assertNotIn("historical_cancelled", current)
        self.assertNotIn("historical_today_cancelled", current)
        self.assertEqual(current["errors"], 1)

    def test_error_classifier_identifies_gateway_outages_but_not_auth_or_rate_limits(self):
        self.assertEqual(classify_error("HTTP 503 Service Unavailable"), "gateway_unavailable")
        self.assertEqual(classify_error("request failed: ECONNREFUSED"), "gateway_unavailable")
        self.assertEqual(classify_error("HTTP 401 from upstream"), "api_failure")
        self.assertEqual(classify_error("HTTP 429: Gateway Timeout"), "api_failure")

    def test_only_current_job_runs_derive_error_kind(self):
        self.create_site()
        job_id = self.db.create_job(job_payload())
        self.db.add_run(run_payload(job_id, correct=1))
        outage = run_payload(job_id, status="error", correct=None)
        outage["error"] = "503 Service Unavailable"
        self.db.add_run(outage)

        current = self.db.job(job_id)
        self.assertEqual(current["runs"][0]["error_kind"], None)
        self.assertEqual(current["runs"][1]["error_kind"], "gateway_unavailable")
        history = self.db.history()
        self.assertEqual([(run["status"], run["error_kind"]) for run in history["runs"]], [
            ("graded", None),
        ])

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
            "model": "other-model", "multiplier": 1, "enabled": 1,
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
            "model": "different-model", "multiplier": 1, "enabled": 0,
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

        for version in (1, 2, 4):
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

        prompts: list[str] = []

        def fake_invoke(_engine, site, _model, _effort, _timeout, _cancel_event, proxy_url, prompt):
            events.append(site["name"])
            proxies.append(proxy_url)
            prompts.append(prompt)
            return {"answer": "答案 21", "elapsed_seconds": 0.0}

        job_id = self.make_job("serial", 2)
        questions = [("同结构关键词和数值", 21)] * 2
        with patch("candytest.jobs.random_candy_prompts", return_value=questions) as generate, \
             patch("candytest.jobs.invoke", side_effect=fake_invoke):
            self.manager._run_job(job_id, "pi", "serial", 2, "medium", None, self.sites, proxy_url=PROXY)
        generate.assert_called_once_with(2, False)
        self.assertEqual(events, ["A", "A", "B", "B"])
        self.assertEqual(proxies, [PROXY] * 4)
        self.assertEqual(prompts, ["同结构关键词和数值"] * 4)
        stored_job = self.db.job(job_id)
        self.assertEqual(stored_job["status"], "completed")
        self.assertEqual(stored_job["summary"], {
            "completed": 4, "planned": 4, "graded": 4, "correct": 4,
            "incorrect": 0, "errors": 0, "accuracy": 100.0,
        })
        self.assertEqual(stored_job["gateways"][0]["historical_accuracy"], 100.0)

    def test_candy_both_strategy_answers_are_saved_as_correct(self):
        for mode in ("serial", "parallel"):
            with self.subTest(mode=mode):
                job_id = self.make_job(mode, 1)

                def fake_invoke(_engine, site, *_args):
                    return {"answer": f"推理完成\nFINAL: {39 + site['id']}"}

                with patch("candytest.random.randint", side_effect=(12, 1, 11, 10, 20, 17)), \
                     patch("candytest.jobs.invoke", side_effect=fake_invoke):
                    self.manager._run_job(job_id, "pi", mode, 1, "medium", None, self.sites)
                job = self.db.job(job_id)
                self.assertEqual(job["summary"]["correct"], 2)
                self.assertEqual({run["answer"] for run in job["runs"]}, {
                    "推理完成\nFINAL: 40", "推理完成\nFINAL: 41",
                })

    def test_fixed_candy_format_reaches_question_generator(self):
        job_id = self.make_job("serial", 1)
        with patch("candytest.jobs.random_candy_prompts", return_value=[("原题", 21)]) as prompts, \
             patch("candytest.jobs.invoke", return_value={"answer": "21", "elapsed_seconds": 0.0}):
            self.manager._run_job(
                job_id, "pi", "serial", 1, "medium", None, self.sites,
                random_candy_format=False,
            )
        prompts.assert_called_once_with(1, False)

    def test_knowledge_questions_run_and_grade_in_both_scheduling_modes(self):
        for mode in ("serial", "parallel"):
            for question_id in ('thibault_sottiaux', 'johannes_heidecke', 'sam_mccandlish'):
                with self.subTest(mode=mode, question=question_id):
                    prompt, expected = question_prompt(question_id)
                    job_id = self.make_job(mode, 3)
                    def fake_invoke(_engine, site, _model, effort, _timeout, _cancel, _proxy, actual_prompt):
                        self.assertEqual(actual_prompt, prompt)
                        self.assertEqual(effort, "low")
                        return {"answer": expected[0] if site["id"] == 1 else "no"}
                    with patch("candytest.jobs.invoke", side_effect=fake_invoke) as invoke:
                        self.manager._run_job(job_id, "pi", mode, 3, "low", None, self.sites,
                                              question_id=question_id)
                    self.assertEqual(invoke.call_count, 6)
                    job = self.db.job(job_id)
                    self.assertEqual(job["status"], "completed")
                    self.assertEqual(job["summary"]["correct"], 3)
                    self.assertEqual(job["summary"]["incorrect"], 3)
                    self.assertEqual(job["summary"]["errors"], 0)

    def test_api_calls_use_ten_minute_timeout(self):
        timeouts: list[int] = []

        def fake_invoke(_engine, _site, _model, _effort, timeout, _cancel_event, _proxy_url, _prompt):
            timeouts.append(timeout)
            return {"answer": "FINAL: 666", "elapsed_seconds": 0.0}

        job_id = self.make_job("serial", 1)
        with patch("candytest.jobs.invoke", side_effect=fake_invoke):
            self.manager._run_job(
                job_id, "pi", "serial", 1, "medium", None, self.sites,
                question_id="dag10",
            )
        self.assertEqual(timeouts, [600, 600])
        self.assertEqual(self.db.job(job_id)["summary"]["correct"], 2)

    def test_parallel_mode_overlaps_sites_but_never_rounds_of_one_site(self):
        lock = threading.Lock()
        active_by_site = {"A": 0, "B": 0}
        max_by_site = {"A": 0, "B": 0}
        active_total = 0
        max_total = 0

        def fake_invoke(_engine, site, _model, _effort, _timeout, _cancel_event, _proxy_url, _prompt):
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

    def test_failed_api_call_is_saved_but_excluded_from_all_accuracy(self):
        payload = job_payload()
        payload["gateway_snapshot"] = json.dumps([{"id": self.sites[0]["id"], "name": self.sites[0]["name"]}])
        job_id = self.db.create_job(payload)
        with patch("candytest.jobs.invoke", side_effect=RuntimeError("simulated 429")):
            self.manager._run_job(job_id, "pi", "serial", 1, "medium", None, [self.sites[0]])

        stored = self.db.job(job_id)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(len(stored["runs"]), 1)
        self.assertEqual(stored["runs"][0]["status"], "error")
        self.assertIn("simulated 429", stored["runs"][0]["error"])
        self.assertEqual(stored["summary"], {
            "completed": 1, "planned": 1, "graded": 0, "correct": 0,
            "incorrect": 0, "errors": 1, "accuracy": None,
        })
        self.assertEqual(stored["gateways"][0]["graded"], 0)
        self.assertIsNone(stored["gateways"][0]["accuracy"])
        history = self.db.history()
        self.assertEqual(history["runs"], [])
        self.assertEqual(history["gateways"], [])

    def test_cancel_stops_active_call_and_future_rounds(self):
        entered = threading.Event()

        def blocking_invoke(_engine, _site, _model, _effort, _timeout, cancel_event, _proxy_url, _prompt):
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
        self.assertEqual(stored["runs"], [])
        self.assertEqual(stored["summary"]["completed"], 0)
        self.assertEqual(stored["summary"]["graded"], 0)
        self.assertNotIn("cancelled", stored["summary"])
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
                          "model": "test-model", "multiplier": 1, "enabled": True}

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

    def test_gateway_multiplier_crud_default_and_validation(self):
        body = {**self.site_body, "multiplier": 0.08}
        created = self.client.post("/api/gateways", json=body)
        self.assertEqual(created.status_code, 201)
        site_id = created.get_json()["gateway"]["id"]
        self.assertEqual(created.get_json()["gateway"]["multiplier"], 0.08)
        updated = self.client.put(f"/api/gateways/{site_id}", json={**body, "multiplier": 2})
        self.assertEqual(updated.get_json()["gateway"]["multiplier"], 2)
        defaulted = self.client.post("/api/gateways", json={key: value for key, value in self.site_body.items() if key != "multiplier"})
        self.assertEqual(defaulted.get_json()["gateway"]["multiplier"], 1)
        for value in (True, "1", -0.01, 1_000_000.01, float("inf"), float("nan")):
            with self.subTest(value=value):
                invalid = self.client.post("/api/gateways", json={**self.site_body, "multiplier": value})
                self.assertEqual((invalid.status_code, invalid.get_json()["error"]["code"]), (400, "INVALID_GATEWAY"))

    def test_gateway_reorder_api_persists_complete_order(self):
        gateway_ids = [self.add_site(), self.add_site(), self.add_site()]
        ordered = [gateway_ids[2], gateway_ids[0], gateway_ids[1]]
        saved = self.client.put("/api/gateways/reorder", json={"gateway_ids": ordered})
        self.assertEqual((saved.status_code, saved.get_json()), (200, {"gateway_ids": ordered}))
        self.assertEqual([site["id"] for site in self.client.get("/api/gateways").get_json()["gateways"]], ordered)
        self.assertEqual([site["id"] for site in Database(Path(self.temp.name)).gateways()], ordered)
        invalid = self.client.put("/api/gateways/reorder", json={"gateway_ids": ordered[:-1]})
        self.assertEqual((invalid.status_code, invalid.get_json()["error"]["code"]), (400, "INVALID_GATEWAYS"))
        self.assertEqual([site["id"] for site in self.client.get("/api/gateways").get_json()["gateways"]], ordered)

    def test_api_validates_content_gateway_and_unavailable_cli(self):
        self.assertEqual(self.client.post("/api/gateways", data="{}").status_code, 415)
        invalid = {**self.site_body, "base_url": "ftp://gateway.example"}
        bad = self.client.post("/api/gateways", json=invalid)
        self.assertEqual((bad.status_code, bad.get_json()["error"]["code"]), (400, "INVALID_GATEWAY"))
        site_id = self.add_site()
        with patch("candytest.app.cli_availability", return_value={"pi": False, "codex": False}):
            response = self.client.post("/api/jobs", json={"engine": "pi", "gateway_ids": [site_id]})
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (409, "CLI_UNAVAILABLE"))

    def test_job_api_selects_question_defaults_and_rejects_unknown_question(self):
        site_id = self.add_site()
        manager = self.app.extensions["candytest_jobs"]
        with patch("candytest.app.cli_availability", return_value={"pi": True, "codex": False}), \
             patch.object(manager, "start", return_value=321) as start:
            probability = self.client.post(
                "/api/jobs", json={"engine": "pi", "question_id": "probability", "gateway_ids": [site_id]},
            )
            dag = self.client.post(
                "/api/jobs", json={"engine": "pi", "question_id": "dag10", "gateway_ids": [site_id]},
            )
            fixed = self.client.post(
                "/api/jobs", json={
                    "engine": "pi", "question_id": "candy", "random_candy_format": False,
                    "gateway_ids": [site_id],
                },
            )
            original = self.client.post(
                "/api/jobs", json={
                    "engine": "pi", "question_id": "candy", "random_candy_format": "original",
                    "gateway_ids": [site_id],
                },
            )
            invalid = self.client.post(
                "/api/jobs", json={"engine": "pi", "question_id": "missing", "gateway_ids": [site_id]},
            )
            invalid_toggle = self.client.post(
                "/api/jobs", json={
                    "engine": "pi", "random_candy_format": "false", "gateway_ids": [site_id],
                },
            )
        self.assertEqual(probability.status_code, 201)
        self.assertEqual(dag.status_code, 201)
        self.assertEqual(fixed.status_code, 201)
        self.assertEqual(original.status_code, 201)
        self.assertEqual(
            [call.args[1:4] + (call.args[-1], call.kwargs["random_candy_format"])
             for call in start.call_args_list],
            [
                ("parallel", 5, "medium", "probability", False),
                ("parallel", 5, "medium", "dag10", False),
                ("parallel", 5, "low", "candy", False),
                ("parallel", 5, "low", "candy", "original"),
            ],
        )
        self.assertEqual((invalid.status_code, invalid.get_json()["error"]["code"]), (400, "INVALID_QUESTION"))
        self.assertEqual(
            (invalid_toggle.status_code, invalid_toggle.get_json()["error"]["code"]),
            (400, "INVALID_RANDOM_FORMAT"),
        )

    def test_copy_question_prompt_api(self):
        for question_id in QUESTION_NAMES:
            with self.subTest(question=question_id):
                response = self.client.get("/api/questions/prompt", query_string={
                    "question_id": question_id, "random_candy_format": "original",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {"prompt": question_prompt(question_id, "original")[0]})
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        for candy_format in ("true", "false"):
            response = self.client.get("/api/questions/prompt", query_string={"random_candy_format": candy_format})
            self.assertEqual(response.status_code, 200)
            self.assertIn("FINAL:", response.get_json()["prompt"])
        for query, code in (({"question_id": "missing"}, "INVALID_QUESTION"),
                            ({"question_id": "cup"}, "INVALID_QUESTION"),
                            ({"random_candy_format": "bad"}, "INVALID_RANDOM_FORMAT")):
            response = self.client.get("/api/questions/prompt", query_string=query)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"]["code"], code)

    def test_job_api_rejects_removed_questions(self):
        site_id = self.add_site()
        manager = self.app.extensions["candytest_jobs"]
        with patch("candytest.app.cli_availability", return_value={"pi": True, "codex": True}), patch.object(manager, "start") as start:
            for engine in ("pi", "codex"):
                for question_id in ("cup", "gpt5_release", "nobel_peace_2025", "booker_2025", "nobel_physics_2025", "venice_golden_lion_2025"):
                    with self.subTest(engine=engine, question=question_id):
                        response = self.client.post("/api/jobs", json={
                            "engine": engine, "question_id": question_id, "gateway_ids": [site_id],
                        })
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(response.get_json()["error"]["code"], "INVALID_QUESTION")
            start.assert_not_called()

    def test_job_api_accepts_knowledge_questions_with_low_cost_defaults(self):
        site_id = self.add_site()
        manager = self.app.extensions["candytest_jobs"]
        with patch("candytest.app.cli_availability", return_value={"pi": True, "codex": True}), \
             patch.object(manager, "start", return_value=321) as start:
            for engine in ("pi", "codex"):
                for question_id in ('thibault_sottiaux', 'johannes_heidecke', 'sam_mccandlish'):
                    with self.subTest(engine=engine, question=question_id):
                        response = self.client.post("/api/jobs", json={
                            "engine": engine, "question_id": question_id, "gateway_ids": [site_id],
                        })
                        self.assertEqual(response.status_code, 201)
                        self.assertEqual(start.call_args.args[:4], (engine, "parallel", 5, "low"))
                        self.assertEqual(start.call_args.args[-1], question_id)
                        self.assertEqual(start.call_args.args[4], "gpt-6-astra")
            for override, expected in (("custom-model", "custom-model"), ("", None)):
                response = self.client.post("/api/jobs", json={
                    "engine": "pi", "question_id": "thibault_sottiaux", "gateway_ids": [site_id],
                    "model_override": override, "reasoning_effort": "high",
                })
                self.assertEqual(response.status_code, 201)
                self.assertEqual(start.call_args.args[3:5], ("high", expected))

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
        self.assertEqual(start.call_args.args[1:4], ("parallel", 5, "low"))
        self.assertEqual(start.call_args.args[-2:], (PROXY, "candy"))

    def test_gateway_history_can_be_deleted_independently(self):
        first_id = self.add_site()
        second = self.client.post(
            "/api/gateways", json={**self.site_body, "name": "另一个中转站"}
        )
        self.assertEqual(second.status_code, 201)
        second_id = second.get_json()["gateway"]["id"]
        job_id = self.db.create_job(job_payload())
        self.db.set_job_status(job_id, "running")
        self.db.add_run(run_payload(job_id, gateway_id=first_id, name="测试站"))
        self.db.add_run(run_payload(job_id, gateway_id=second_id, name="另一个中转站"))

        active = self.client.delete(
            f"/api/history/gateways/{first_id}", json={"confirm": True}
        )
        self.assertEqual((active.status_code, active.get_json()["error"]["code"]), (409, "JOB_CONFLICT"))
        self.db.set_job_status(job_id, "completed")
        unconfirmed = self.client.delete(
            f"/api/history/gateways/{first_id}", json={"confirm": False}
        )
        self.assertEqual((unconfirmed.status_code, unconfirmed.get_json()["error"]["code"]), (400, "CONFIRM_REQUIRED"))

        deleted = self.client.delete(
            f"/api/history/gateways/{first_id}", json={"confirm": True}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json(), {"ok": True, "deleted": 1})
        history = self.client.get("/api/history").get_json()
        self.assertEqual([site["gateway_id"] for site in history["gateways"]], [second_id])
        self.assertEqual([run["gateway_id"] for run in history["runs"]], [second_id])
        self.assertEqual([run["gateway_id"] for run in self.db.job(job_id)["runs"]], [second_id])
        self.assertEqual(len(self.client.get("/api/gateways").get_json()["gateways"]), 2)

        missing = self.client.delete(
            f"/api/history/gateways/{first_id}", json={"confirm": True}
        )
        self.assertEqual((missing.status_code, missing.get_json()["error"]["code"]), (404, "HISTORY_NOT_FOUND"))

    def test_gateway_delete_can_keep_or_remove_corresponding_records(self):
        kept_id = self.add_site()
        removed = self.client.post(
            "/api/gateways", json={**self.site_body, "name": "删除记录站"}
        ).get_json()["gateway"]
        job_id = self.db.create_job(job_payload())
        self.db.set_job_status(job_id, "completed")
        self.db.add_run(run_payload(job_id, gateway_id=kept_id, name="测试站"))
        self.db.add_run(run_payload(job_id, gateway_id=removed["id"], name=removed["name"]))

        kept = self.client.delete(f"/api/gateways/{kept_id}")
        self.assertEqual(kept.get_json(), {"ok": True, "deleted_runs": 0})
        self.assertEqual(
            {run["gateway_id"] for run in self.client.get("/api/history").get_json()["runs"]},
            {kept_id, removed["id"]},
        )

        deleted = self.client.delete(f"/api/gateways/{removed['id']}?delete_history=1")
        self.assertEqual(deleted.get_json(), {"ok": True, "deleted_runs": 1})
        self.assertEqual(
            [run["gateway_id"] for run in self.client.get("/api/history").get_json()["runs"]],
            [kept_id],
        )

        active_id = self.add_site()
        active_job = self.db.create_job(job_payload())
        self.db.set_job_status(active_job, "running")
        self.db.add_run(run_payload(active_job, gateway_id=active_id, name="测试站"))
        conflict = self.client.delete(f"/api/gateways/{active_id}?delete_history=1")
        self.assertEqual((conflict.status_code, conflict.get_json()["error"]["code"]), (409, "JOB_CONFLICT"))
        self.assertIn(active_id, [site["id"] for site in self.db.gateways()])

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

    def test_server_bootstrap_fails_closed_for_invalid_partial_legacy_input(self):
        from candytest.app import create_app
        cases = (
            {"CANDYTEST_ADMIN_USERNAME": "admin", "CANDYTEST_ADMIN_PASSWORD_HASH": ""},
            {"CANDYTEST_ADMIN_PASSWORD_HASH": "not-a-password-hash"},
            {"CANDYTEST_SECRET_KEY": "too-short"},
        )
        for overrides in cases:
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, self.server_environ(**overrides), clear=False):
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

    def test_fresh_defaults_persist_hashed_credentials_and_account_changes(self):
        from candytest.app import create_app
        initial = {
            "CANDYTEST_DEPLOYMENT": "server", "CANDYTEST_HOST": "127.0.0.1",
            "CANDYTEST_ADMIN_USERNAME": "", "CANDYTEST_ADMIN_PASSWORD_HASH": "",
            "CANDYTEST_ADMIN_PASSWORD_HASH_B64": "", "CANDYTEST_SECRET_KEY": "",
            "CANDYTEST_COOKIE_SECURE": "0",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, initial, clear=False):
            data_dir = Path(temp)
            app = create_app(data_dir)
            app.testing = True
            client = app.test_client()
            auth_path = data_dir / "auth.json"
            state = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(state["username"], "admin")
            self.assertNotEqual(state["password_hash"], "admin")
            self.assertGreaterEqual(len(state["secret_key"].encode("utf-8")), 32)
            self.assertNotIn("password", client.get("/api/runtime").get_data(as_text=True))
            token = self.csrf(client.get("/login"))
            self.assertEqual(client.post("/login", data={"username": "admin", "password": "admin", "csrf_token": token}).status_code, 302)
            csrf = self.csrf(client.get("/"))
            account = client.get("/api/settings/account").get_json()["account"]
            self.assertEqual(account, {"username": "admin", "default_credentials": True})
            second = app.test_client()
            second_token = self.csrf(second.get("/login"))
            self.assertEqual(second.post("/login", data={"username": "admin", "password": "admin", "csrf_token": second_token}).status_code, 302)
            response = client.put("/api/settings/account", json={
                "username": "new-admin", "current_password": "admin", "new_password": "new-password",
                "new_password_confirmation": "new-password",
            }, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["account"], {"username": "new-admin", "default_credentials": False})
            self.assertEqual(second.get("/api/runtime").status_code, 401)
            persisted = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertNotIn("new-password", auth_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["revision"], 2)
            legacy_after_change = self.server_environ()
            with patch.dict(os.environ, legacy_after_change, clear=False):
                restarted = create_app(data_dir)
            restarted.testing = True
            after_restart = restarted.test_client()
            login = self.csrf(after_restart.get("/login"))
            self.assertEqual(after_restart.post("/login", data={"username": "new-admin", "password": "new-password", "csrf_token": login}).status_code, 302)
            stale = restarted.test_client()
            stale_token = self.csrf(stale.get("/login"))
            self.assertEqual(stale.post("/login", data={"username": "admin", "password": self.password, "csrf_token": stale_token}).status_code, 401)

    def test_account_validation_and_runtime_corruption_fail_closed(self):
        from candytest.app import create_app
        initial = {
            "CANDYTEST_DEPLOYMENT": "server", "CANDYTEST_HOST": "127.0.0.1",
            "CANDYTEST_ADMIN_USERNAME": "", "CANDYTEST_ADMIN_PASSWORD_HASH": "",
            "CANDYTEST_ADMIN_PASSWORD_HASH_B64": "", "CANDYTEST_SECRET_KEY": "",
            "CANDYTEST_COOKIE_SECURE": "0",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, initial, clear=False):
            data_dir = Path(temp)
            app = create_app(data_dir); app.testing = True
            client = app.test_client()
            token = self.csrf(client.get("/login"))
            client.post("/login", data={"username": "admin", "password": "admin", "csrf_token": token})
            csrf = self.csrf(client.get("/"))
            short = client.put("/api/settings/account", json={
                "username": "admin", "current_password": "admin", "new_password": "short",
                "new_password_confirmation": "short",
            }, headers={"X-CSRF-Token": csrf})
            self.assertEqual((short.status_code, short.get_json()["error"]["code"]), (400, "INVALID_ACCOUNT"))
            mismatch = client.put("/api/settings/account", json={
                "username": "admin", "current_password": "admin", "new_password": "long-enough",
                "new_password_confirmation": "different",
            }, headers={"X-CSRF-Token": csrf})
            self.assertEqual((mismatch.status_code, mismatch.get_json()["error"]["code"]), (400, "INVALID_ACCOUNT"))
            renamed = client.put("/api/settings/account", json={
                "username": "renamed-admin", "current_password": "admin", "new_password": "",
                "new_password_confirmation": "",
            }, headers={"X-CSRF-Token": csrf})
            self.assertEqual(renamed.status_code, 200)
            self.assertEqual(client.get("/api/runtime").status_code, 200)

            (data_dir / "auth.json").write_text("{bad", encoding="utf-8")
            broken_login = app.test_client().get("/login")
            self.assertEqual(broken_login.status_code, 503)
            self.assertIn("认证状态无效", broken_login.get_data(as_text=True))

    def test_account_rejects_wrong_current_password_and_invalid_sidecar(self):
        from candytest.app import create_app
        initial = {
            "CANDYTEST_DEPLOYMENT": "server", "CANDYTEST_HOST": "127.0.0.1",
            "CANDYTEST_ADMIN_USERNAME": "", "CANDYTEST_ADMIN_PASSWORD_HASH": "",
            "CANDYTEST_ADMIN_PASSWORD_HASH_B64": "", "CANDYTEST_SECRET_KEY": "",
            "CANDYTEST_COOKIE_SECURE": "0",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, initial, clear=False):
            app = create_app(Path(temp)); app.testing = True
            client = app.test_client()
            token = self.csrf(client.get("/login"))
            client.post("/login", data={"username": "admin", "password": "admin", "csrf_token": token})
            csrf = self.csrf(client.get("/"))
            bad = client.put("/api/settings/account", json={"username": "admin", "current_password": "wrong", "new_password": ""}, headers={"X-CSRF-Token": csrf})
            self.assertEqual((bad.status_code, bad.get_json()["error"]["code"]), (403, "CURRENT_PASSWORD_INCORRECT"))
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, initial, clear=False):
            (Path(temp) / "auth.json").write_text("{bad", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                create_app(Path(temp))


    def test_login_protection_csrf_logout_cookie_and_security_headers(self):
        temp, app, client = self.make_client()
        try:
            self.assertEqual(client.get("/healthz").get_json(), {"status": "ok"})
            self.assertEqual(client.get("/healthz").headers["Cache-Control"], "no-store")
            self.assertEqual(client.get("/").status_code, 302)
            self.assertEqual(client.get("/settings").status_code, 302)
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
            settings_page = client.get("/settings")
            self.assertEqual(settings_page.status_code, 200)
            self.assertIn('name="csrf-token"', settings_page.get_data(as_text=True))
            denied = client.post("/api/gateways", json={})
            self.assertEqual((denied.status_code, denied.get_json()["error"]["code"]), (403, "CSRF_FAILED"))
            allowed = client.post("/api/gateways", json={
                "name": "server", "base_url": "https://gateway.example/v1", "api_key": SECRET,
                "model": "model", "multiplier": 1, "enabled": True,
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
            settings = local.test_client().get("/settings")
            self.assertEqual(settings.status_code, 200)
            self.assertNotIn("登录账号", settings.get_data(as_text=True))

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
