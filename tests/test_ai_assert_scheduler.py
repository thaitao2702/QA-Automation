"""Tests for framework/runner/ai_assert_scheduler.py — cap, crash-safety, grading."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from framework.helpers.gemini import GeminiResult
from framework.runner.ai_assert_scheduler import (
    AiAssertScheduler,
    grade_image,
    make_ai_semaphore,
)


class _FakeImageHelper:
    """Records peak concurrency of analyse_image calls (runs in to_thread workers)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.calls = 0

    def analyse_image(self, image_path, prompt, output_schema, extra_images=None):
        with self._lock:
            self.current += 1
            self.calls += 1
            self.max_seen = max(self.max_seen, self.current)
        time.sleep(0.08)
        with self._lock:
            self.current -= 1
        return GeminiResult(success=True, data={"ok": True}, reason=None)


def _submit_n(sched: AiAssertScheduler, n: int, sink) -> None:
    for _ in range(n):
        sched.submit_image(screenshot_paths=[Path("x.jpg")], prompt="p", output_schema={}, sink=sink)


# --- make_ai_semaphore -----------------------------------------------------


def test_make_ai_semaphore_claude_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_AI_MAX_CONCURRENCY", raising=False)
    assert make_ai_semaphore("claude")._value == 2


def test_make_ai_semaphore_claude_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_MAX_CONCURRENCY", "3")
    assert make_ai_semaphore("claude")._value == 3


def test_make_ai_semaphore_other_providers_unbounded():
    assert make_ai_semaphore("gemini")._value >= 1000
    assert make_ai_semaphore("local")._value >= 1000


# --- concurrency cap -------------------------------------------------------


def test_claude_cap_limits_concurrency(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_MAX_CONCURRENCY", "2")
    helper = _FakeImageHelper()
    results: list = []

    async def run():
        sched = AiAssertScheduler(make_ai_semaphore("claude"), helper)
        _submit_n(sched, 6, results.append)
        await sched.await_all()

    asyncio.get_event_loop().run_until_complete(run())
    assert helper.calls == 6
    assert helper.max_seen <= 2          # the cap held
    assert len(results) == 6


def test_other_provider_runs_unbounded():
    helper = _FakeImageHelper()

    async def run():
        sched = AiAssertScheduler(make_ai_semaphore("gemini"), helper)
        _submit_n(sched, 6, lambda r: None)
        await sched.await_all()

    asyncio.get_event_loop().run_until_complete(run())
    assert helper.calls == 6
    assert helper.max_seen >= 3          # clearly above the claude cap of 2


# --- crash safety ----------------------------------------------------------


def test_crashed_task_folds_fail_and_does_not_raise():
    class _Boom:
        def analyse_image(self, *a, **k):
            raise RuntimeError("boom")

    results: list = []

    async def run():
        sched = AiAssertScheduler(make_ai_semaphore("gemini"), _Boom())
        sched.submit_image(screenshot_paths=[Path("x.jpg")], prompt="p", output_schema={}, sink=results.append)
        await sched.await_all()          # must not raise

    asyncio.get_event_loop().run_until_complete(run())
    assert len(results) == 1
    assert results[0].success is False and "boom" in (results[0].reason or "")


# --- grade_image -----------------------------------------------------------


def test_grade_image_pass():
    res = GeminiResult(success=True, data={"matches": True, "confidence": "high"}, reason=None)
    ai_data, ai_result, err = grade_image(res, {"expected": {"matches": True, "confidence": "high"}})
    assert ai_result == "pass" and ai_data["passed"] is True and err is None


def test_grade_image_mismatch_includes_field():
    res = GeminiResult(success=True, data={"matches": True, "confidence": "medium"}, reason=None)
    ai_data, ai_result, err = grade_image(res, {"expected": {"matches": True, "confidence": "high"}})
    assert ai_result == "fail"
    assert ai_data["mismatches"] and "confidence" in (err or "")


def test_grade_image_api_failure():
    res = GeminiResult(success=False, data=None, reason="rate limited")
    ai_data, ai_result, err = grade_image(res, {"expected": {}})
    assert ai_result == "fail" and ai_data["passed"] is False and "rate limited" in (err or "")
