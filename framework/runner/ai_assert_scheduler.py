"""Concurrency-capped scheduler for non-blocking AI image assertions.

An ``assert.ai`` (image mode) launches as a detached background asyncio task the
moment its step executes, so the model call overlaps the rest of the test instead
of waiting for the end of the suite. A provider-aware semaphore caps the **claude**
provider — which spawns one CLI process per assert — to ``CLAUDE_AI_MAX_CONCURRENCY``
(default 2); gemini/local (plain HTTP calls) are effectively unbounded.

The scheduler is **record-agnostic**: each submit takes a ``sink`` callback that
folds the raw provider result into the owning test/run record. Grading (turning a
model answer into pass/fail) lives in :func:`grade_image`, called by the sink, so
``step-N-ai.json`` stays byte-identical to the legacy deferred path.

Video asserts are NOT scheduled here — their clip only exists after the suite's
shared recording is finalized; they run in ``video_ai_processor`` under the same
semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

from framework.helpers.gemini import GeminiResult

logger = logging.getLogger(__name__)

# Large bound for HTTP providers — keeps a single ``async with sem`` code path
# instead of branching on provider at every call site.
_UNBOUNDED = 1000


def make_ai_semaphore(provider: str) -> asyncio.Semaphore:
    """Return a concurrency limiter sized for the AI *provider*.

    ``claude`` spawns one CLI process per assert, so it is capped to
    ``CLAUDE_AI_MAX_CONCURRENCY`` (default 2, min 1). ``gemini``/``local`` are
    remote HTTP calls and get a large bound (effectively unbounded).
    """
    if (provider or "").strip().lower() == "claude":
        try:
            n = int(os.environ.get("CLAUDE_AI_MAX_CONCURRENCY", "2"))
        except ValueError:
            n = 2
        n = max(1, n)
    else:
        n = _UNBOUNDED
    return asyncio.Semaphore(n)


def grade_image(result: GeminiResult, request_info: dict[str, Any]) -> tuple[dict, str, str | None]:
    """Grade an image AI *result* against ``request_info['expected']``.

    Returns ``(ai_data, ai_result, error)`` where ``ai_result`` is ``"pass"`` or
    ``"fail"`` and ``ai_data`` is the dict written to ``step-N-ai.json``. Mirrors
    the legacy deferred comparison exactly: a failed API call fails; otherwise
    every key in ``expected`` must equal the model's value (strict ``!=``).
    """
    expected = request_info.get("expected", {}) or {}
    if not result.success:
        ai_data = {
            "passed": False,
            "reason": result.reason,
            "request": request_info,
            "raw_response": result.raw_text,
        }
        return ai_data, "fail", f"AI image assert failed: {result.reason}"

    actual_data = result.data or {}
    mismatches: list[str] = []
    for key, exp_val in expected.items():
        act_val = actual_data.get(key)
        if act_val != exp_val:
            mismatches.append(f"{key}: got {act_val!r}, expected {exp_val!r}")
    passed = len(mismatches) == 0
    ai_data = {
        "passed": passed,
        "data": actual_data,
        "expected": expected,
        "mismatches": mismatches,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "request": request_info,
        "raw_response": result.raw_text,
    }
    error = None if passed else f"AI image assert failed: {'; '.join(mismatches)}"
    return ai_data, ("pass" if passed else "fail"), error


class AiAssertScheduler:
    """Runs ``assert.ai`` IMAGE analyses as capped, detached background tasks.

    :meth:`submit_image` fires a task immediately and returns; the task runs the
    provider call under the shared semaphore (off the event loop via
    ``asyncio.to_thread``) and hands the raw ``GeminiResult`` to a record-bound
    ``sink``. The scheduler keeps **strong references** to every task (so they are
    not garbage-collected mid-flight — a known ``create_task`` footgun) and
    :meth:`await_all` joins them once at suite end.
    """

    def __init__(self, semaphore: asyncio.Semaphore, image_helper: Any) -> None:
        self._sem = semaphore
        self._helper = image_helper
        self._tasks: list[asyncio.Task] = []

    @property
    def enabled(self) -> bool:
        """True when a real image helper is configured (provider produced one)."""
        return self._helper is not None

    def submit_image(
        self,
        *,
        screenshot_paths: list[Path],
        prompt: str,
        output_schema: dict[str, Any],
        sink: Callable[[GeminiResult], None],
        reference: Any | None = None,
    ) -> None:
        """Launch an image analysis in the background; ``sink(result)`` folds it.

        ``reference`` is the scoped reference pack for this assert (or ``None`` to use
        the helper's full-pack default).
        """
        self._tasks.append(
            asyncio.create_task(
                self._run_image(screenshot_paths, prompt, output_schema, sink, reference)
            )
        )

    async def _run_image(
        self,
        paths: list[Path],
        prompt: str,
        output_schema: dict[str, Any],
        sink: Callable[[GeminiResult], None],
        reference: Any | None = None,
    ) -> None:
        try:
            async with self._sem:
                result = await asyncio.to_thread(
                    self._helper.analyse_image,
                    paths[0],
                    prompt,
                    output_schema,
                    extra_images=paths[1:] if len(paths) > 1 else None,
                    reference=reference,
                )
        except Exception as exc:  # noqa: BLE001 — a crashed task folds a fail, never hangs
            logger.exception("AI image assertion task crashed")
            result = GeminiResult(success=False, data=None, reason=f"AI task error: {exc}")
        try:
            sink(result)
        except Exception:  # noqa: BLE001 — a bad sink must not take down await_all()
            logger.exception("AI image assertion sink failed")

    async def await_all(self) -> None:
        """Join all in-flight image tasks. Called once at suite end."""
        if not self._tasks:
            return
        logger.info(
            "\033[1m\033[35m▶ AI Processing\033[0m joining %d image assertion task(s)",
            len(self._tasks),
        )
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
