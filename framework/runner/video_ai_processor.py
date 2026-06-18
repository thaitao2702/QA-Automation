"""
Video AI processor for the QA platform.

Each standalone test (and each run) that carries a video AI assert is processed
independently the moment it completes: the processor trims the test's video with
ffmpeg, sends the trimmed clip to the AI helper (with the assert's scoped reference
pack attached inline), and updates result.json with the AI verdict. There is no
batch grouping — that previously keyed off the deprecated ``ref_assets`` field only
to share a Gemini context-cache upload, which is now a no-op.

Public interface
----------------
VideoAiProcessor(game, suite_doc, base_dir, gemini_video_helper)
    Manager. Call notify_test_complete() / notify_run_complete() as work finishes.

    notify_test_complete(record) -> None
        Queue a completed standalone test (with a video assert) for processing.

    wait_all() -> None
        Dispatch + await all queued analyses. Call after suite execution finishes.
        A safety net marks any record whose verdict never resolved as fail (never
        leaves a video assert silently 'pending').

Standalone CLI
--------------
python framework/runner/video_ai_processor.py \\
    --results-dir test-results/zeus/dev/2026-04-07-suite-06 \\
    --game zeus

Handles Gemini failures gracefully — marks test as ai_result=fail with reason.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from framework.runner.suite_runner import TestRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict calculation (duplicated from suite_runner to avoid import side-effects)
# ---------------------------------------------------------------------------


def _compute_verdict(bridge_result: str, ai_result: str) -> str:
    """
    Compute triple-validation final verdict.

    Intentionally standalone (not imported from suite_runner) to avoid triggering
    Playwright imports when video_ai_processor is used as a standalone CLI tool.
    """
    if bridge_result == "skip" and ai_result == "skip":
        return "skip"
    if ai_result == "pending":
        return "pending"
    if bridge_result == "pass" and ai_result in ("pass", "skip"):
        return "green"
    if bridge_result == "fail" and ai_result == "fail":
        return "red"
    if bridge_result == "fail" or ai_result == "fail":
        return "orange"
    return "green"


# Ensure repo root on path when run standalone
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# ffmpeg trimming
# ---------------------------------------------------------------------------


def _trim_video(
    source: Path,
    dest: Path,
    start_s: float,
    end_s: float,
) -> bool:
    """
    Trim *source* video to the [start_s, end_s] window and write *dest*.

    Uses ``-c copy`` for fast stream copy (no re-encode).

    Returns True on success, False if ffmpeg fails or is not found.
    """
    if not source.exists():
        logger.error("Video source not found for trimming: %s", source)
        return False

    cmd = [
        "ffmpeg",
        "-y",           # overwrite output
        "-ss", str(start_s),
        "-to", str(end_s),
        "-i", str(source),
        "-c", "copy",
        str(dest),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error("ffmpeg failed (exit %d): %s", result.returncode, result.stderr[-500:])
            return False
        return True
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found. Install from https://ffmpeg.org/download.html "
            "or via your OS package manager."
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out trimming %s", source)
        return False


def _resolve_trim_timestamps(
    timeline: dict[str, Any],
    from_step_id: str,
    to_step_id: str,
) -> tuple[float, float] | None:
    """
    Resolve video trim start/end seconds from step IDs in timeline.json.

    The timeline contains step offsets in milliseconds relative to video_start_time.
    video_event_offset is the gap between video recording start and event collection start.

    Returns (start_s, end_s) or None if step IDs not found.
    """
    steps = timeline.get("steps", [])
    step_map: dict[str, dict[str, Any]] = {s["id"]: s for s in steps}

    from_step = step_map.get(from_step_id)
    to_step = step_map.get(to_step_id)

    if from_step is None:
        logger.warning("Trim from_step '%s' not found in timeline", from_step_id)
        return None
    if to_step is None:
        logger.warning("Trim to_step '%s' not found in timeline", to_step_id)
        return None

    # start_ms / end_ms are relative to test_start (executor._test_start_time).
    # video_event_offset = event_collection_start - video_start
    # So step timestamps are already aligned with video_event_offset added.
    video_event_offset_s = float(timeline.get("video_event_offset", 0.0))

    start_s = from_step["start_ms"] / 1000.0 + video_event_offset_s
    end_s = to_step["end_ms"] / 1000.0 + video_event_offset_s

    # Guard against negative / reversed values
    start_s = max(0.0, start_s)
    if end_s <= start_s:
        logger.warning(
            "Trim end (%ss) <= start (%ss) for steps %s→%s; using start+30s fallback",
            end_s,
            start_s,
            from_step_id,
            to_step_id,
        )
        end_s = start_s + 30.0

    return start_s, end_s


# ---------------------------------------------------------------------------
# Video test discovery
# ---------------------------------------------------------------------------


def _iter_suite_tests(suite_doc: dict[str, Any]):
    """Yield every test in the suite — top-level ``tests:`` plus all ``groups[].tests``."""
    yield from suite_doc.get("tests") or []
    for group in suite_doc.get("groups") or []:
        yield from group.get("tests") or []


def _iter_suite_runs(suite_doc: dict[str, Any]):
    """Yield every run — top-level ``runs:`` plus all ``groups[].runs``."""
    yield from suite_doc.get("runs") or []
    for group in suite_doc.get("groups") or []:
        yield from group.get("runs") or []


def _has_video_assert_step(doc: dict[str, Any]) -> bool:
    """True if any of the doc's steps carry an ``assert.ai.mode == 'video'`` block."""
    for step in doc.get("steps", []):
        if step.get("assert", {}).get("ai", {}).get("mode") == "video":
            return True
    return False


def _extract_video_test_ids(suite_doc: dict[str, Any]) -> set[str]:
    """Ids of standalone ``tests:`` (not runs) that contain a video AI assert.

    Each such test is processed on its own the moment it completes — there is no batch
    grouping. (Grouping previously keyed off ``ref_assets`` only to share a Gemini
    context-cache upload; references are now attached inline per call, so it added
    nothing but a fragile dependency on a deprecated field.)
    """
    return {
        test["id"]
        for test in _iter_suite_tests(suite_doc)
        if isinstance(test.get("id"), str) and _has_video_assert_step(test)
    }


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class VideoAiProcessor:
    """
    Manages deferred video AI processing.

    Each standalone test (and each run) that carries a video assert is dispatched on
    its own when it completes — no batching. ``wait_all`` awaits them all and never
    leaves a verdict at 'pending'.

    Parameters
    ----------
    game:
        Game identifier (e.g. "zeus").
    suite_doc:
        Parsed suite YAML document (used to discover video AI tests).
    base_dir:
        qa-platform repository root.
    gemini_video_helper:
        GeminiVideoHelper instance. If None, video AI is skipped gracefully.
    """

    def __init__(
        self,
        game: str,
        suite_doc: dict[str, Any],
        base_dir: Path,
        gemini_video_helper: Any | None = None,
        run_test_ids: set[str] | None = None,
        semaphore: "asyncio.Semaphore | None" = None,
        ref_cache: Any = None,
        asset_index: dict[str, set[str]] | None = None,
    ) -> None:
        self._game = game
        self._suite_doc = suite_doc
        self._base_dir = base_dir
        self._gemini = gemini_video_helper
        # Shared AI-assert concurrency limiter (claude → CLAUDE_AI_MAX_CONCURRENCY,
        # shared with the at-step image scheduler). None (standalone CLI use) →
        # effectively unbounded so the CLI entrypoint keeps working.
        self._sem = semaphore if semaphore is not None else asyncio.Semaphore(1000)
        # Shared ScopedReferenceCache + asset index. With these, each video AI assert
        # gets its own scoped reference (assert > test > group), passed per-call. Without
        # them (standalone CLI), fall back to the record's reference / helper full pack.
        self._ref_cache = ref_cache
        self._asset_index = asset_index

        # Ids of standalone tests carrying a video assert. Each is processed on its own
        # the moment it completes — no batch grouping. Restricted to the tests that will
        # actually run under the --test/--group filter so we never wait on something that
        # won't execute.
        self._video_test_ids = _extract_video_test_ids(suite_doc)
        if run_test_ids is not None:
            self._video_test_ids &= run_test_ids
        # Asyncio tasks for in-flight video AI analyses.
        self._tasks: list[asyncio.Task[None]] = []
        # Completed test records (standalone tests) awaiting video AI processing.
        self._ready_tests: list[Any] = []
        # Completed runs awaiting video AI processing (each run is self-contained).
        self._ready_runs: list[Any] = []

    def _video_reference_for(self, record: Any, ai_block: Any) -> Any:
        """Resolve the scoped reference pack for one video AI assert.

        Computes the effective ``asset_groups``/``assets`` selection (assert > test >
        group) and returns the cached scoped SymbolReference. Falls back to the record's
        test/group-scoped ``reference`` (and ultimately the helper's full-pack default
        via ``None``) when no cache is wired (standalone CLI) or nothing is selected.
        """
        if self._ref_cache is None:
            return getattr(record, "reference", None)
        from framework.helpers.symbol_reference import effective_selection

        doc = getattr(record, "test_doc", None) or getattr(record, "run_doc", None)
        sel = effective_selection(
            getattr(record, "group_doc", None), doc, ai_block, self._asset_index
        )
        if sel is None:
            return getattr(record, "reference", None)
        return self._ref_cache.for_selection(*sel)

    async def notify_test_complete(self, record: "TestRecord") -> None:
        """Queue a completed standalone test for video AI processing (no batching)."""
        if record.test_id in self._video_test_ids:
            self._ready_tests.append(record)

    async def notify_run_complete(self, record: Any) -> None:
        """Queue a completed run for video AI processing (runs are self-contained)."""
        self._ready_runs.append(record)

    async def wait_all(self) -> None:
        """Dispatch + await every queued test/run's video AI analysis.

        After the gather, a safety net marks any dispatched record whose AI verdict is
        still ``pending`` as ``fail`` — so a crashed/cancelled analysis can never leave a
        video assert silently unresolved.
        """
        dispatched: list[Any] = []
        for record in self._ready_tests:
            dispatched.append(record)
            self._tasks.append(asyncio.create_task(self._process_test(record, context_id=None)))
        self._ready_tests.clear()

        for run_record in self._ready_runs:
            dispatched.append(run_record)
            self._tasks.append(asyncio.create_task(self._process_run(run_record)))
        self._ready_runs.clear()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Safety net: a dispatched record must end with a real verdict, never 'pending'.
        for record in dispatched:
            self._fail_if_still_pending(record)

    def _fail_if_still_pending(self, record: Any) -> None:
        """Mark a record (and any of a run's cases) as fail if its video verdict never set."""
        reason = "video AI did not complete (no verdict produced)"
        cases = getattr(record, "cases", None)
        if cases is not None:
            # Run record: cases is a dict[case_id -> case verdict].
            pending = [cv for cv in cases.values() if getattr(cv, "ai_result", None) == "pending"]
            if pending:
                logger.warning("Run %s: %d case(s) left video AI pending — marking fail",
                               getattr(record, "run_id", "?"), len(pending))
                self._fail_run_cases_ai(record, reason)
        elif getattr(record, "ai_result", None) == "pending":
            logger.warning("Test %s: video AI left pending — marking fail",
                           getattr(record, "test_id", "?"))
            self._update_ai_result(record, "fail", reason)

    # ------------------------------------------------------------------
    # Per-test / per-run processing
    # ------------------------------------------------------------------

    async def _process_test(self, record: Any, context_id: str | None) -> None:
        """Trim video for one test and send to Gemini for analysis."""
        if self._gemini is None:
            self._update_ai_result(record, "fail", "GeminiVideoHelper not configured")
            return
        artifact = record.artifact
        if artifact is None:
            logger.warning("No artifact for test %s — skipping video AI", record.test_id)
            return

        # Load timeline
        timeline_path = artifact.timeline_path
        if not timeline_path.exists():
            self._update_ai_result(record, "fail", "timeline.json not found")
            return

        with timeline_path.open("r", encoding="utf-8") as fh:
            timeline = json.load(fh)

        # Find all video AI assert steps in this test
        test_doc = record.test_doc
        video_ai_steps = [
            step for step in test_doc.get("steps", [])
            if step.get("assert", {}).get("ai", {}).get("mode") == "video"
        ]

        if not video_ai_steps:
            return

        video_path = artifact.video_path

        # Accumulate this test's video-AI token usage + price across its asserts, so the
        # per-test result.html/result.json shows the cost (not only the suite summary).
        ai_calls = 0
        ai_in_tokens = 0
        ai_out_tokens = 0
        ai_cost = 0.0

        # Scope the helper's reference pack to this test's `assets:` (if any).
        for step in video_ai_steps:
            ai_block = step["assert"]["ai"]
            video_trim = ai_block.get("video_trim", {})
            from_step = video_trim.get("from_step", "")
            to_step = video_trim.get("to_step", "")
            prompt = ai_block.get("prompt", "")
            output_schema = ai_block.get("output_schema", {})
            expected = ai_block.get("expected", {})
            step_id = step.get("id", "")

            # Resolve trim timestamps
            timestamps = _resolve_trim_timestamps(timeline, from_step, to_step)
            if timestamps is None:
                self._update_ai_result(
                    record, "fail",
                    f"Could not resolve trim timestamps for step {step_id}"
                )
                continue

            start_s, end_s = timestamps
            trimmed_path = artifact.test_dir / f"trimmed-{step_id}.webm"

            # Trim video
            trim_ok = await asyncio.to_thread(
                _trim_video, video_path, trimmed_path, start_s, end_s
            )
            if not trim_ok:
                self._update_ai_result(
                    record, "fail",
                    f"ffmpeg trimming failed for step {step_id}"
                )
                continue

            # Resolve the scoped reference + send to the model (gated by the shared AI
            # concurrency cap). Reference resolution is inside the try so a failure there
            # produces a verdict too, instead of propagating and leaving the test pending.
            try:
                reference = self._video_reference_for(record, ai_block)
                async with self._sem:
                    gemini_result = await asyncio.to_thread(
                        self._gemini.analyse_video,
                        trimmed_path,
                        context_id,
                        prompt,
                        output_schema,
                        None,  # ref_images (unused; reference pack carries the assets)
                        reference,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("Gemini video analysis failed for %s: %s", record.test_id, exc)
                self._update_ai_result(record, "fail", f"Gemini API error: {exc}")
                continue

            if not gemini_result.success:
                ai_data = {
                    "passed": False,
                    "reason": gemini_result.reason,
                    "request": {
                        "prompt": prompt,
                        "output_schema": output_schema,
                        "expected": expected,
                        "video_trim": {"from_step": from_step, "to_step": to_step, "start_s": start_s, "end_s": end_s},
                    },
                    "raw_response": gemini_result.raw_text,
                }
            else:
                # Check expected fields
                actual_data = gemini_result.data or {}
                mismatches = []
                for key, exp_val in (expected or {}).items():
                    act_val = actual_data.get(key)
                    if act_val != exp_val:
                        mismatches.append(f"{key}: got {act_val!r}, expected {exp_val!r}")

                passed = len(mismatches) == 0
                ai_data = {
                    "passed": passed,
                    "data": actual_data,
                    "expected": expected,
                    "mismatches": mismatches,
                    "input_tokens": gemini_result.input_tokens,
                    "output_tokens": gemini_result.output_tokens,
                    "request": {
                        "prompt": prompt,
                        "output_schema": output_schema,
                        "video_trim": {"from_step": from_step, "to_step": to_step, "start_s": start_s, "end_s": end_s},
                    },
                    "raw_response": gemini_result.raw_text,
                }

            # Token usage + estimated price for this assert (a failed-judgment call still
            # bills). Resolved defensively (like _process_run): this bookkeeping runs
            # OUTSIDE the try above, so a helper missing cost_for_tokens/model (e.g. the
            # claude provider) must NOT raise here — that would propagate, get swallowed by
            # wait_all's gather, and silently leave the test 'pending' for the safety net.
            cost_fn = getattr(self._gemini, "cost_for_tokens", None)
            call_cost = cost_fn(
                gemini_result.input_tokens, gemini_result.output_tokens
            ) if cost_fn else 0.0
            ai_data["input_tokens"] = gemini_result.input_tokens
            ai_data["output_tokens"] = gemini_result.output_tokens
            ai_data["cost_usd"] = round(call_cost, 6)
            ai_data["model"] = getattr(self._gemini, "model", "unknown")
            ai_calls += 1
            ai_in_tokens += gemini_result.input_tokens
            ai_out_tokens += gemini_result.output_tokens
            ai_cost += call_cost

            # Persist step AI result JSON
            step_num = self._extract_step_number(step_id)
            if step_num is not None:
                ai_path = artifact.ai_result_path(step_num)
                ai_path.write_text(
                    json.dumps(ai_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Update record ai_result
            if ai_data.get("passed"):
                record.ai_result = "pass"
            else:
                record.ai_result = "fail"
                if not record.error:
                    record.error = ai_data.get("reason") or "; ".join(
                        ai_data.get("mismatches", [])
                    )

        # Recompute final verdict now that AI result is known
        record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)

        # Rewrite result.json with updated AI result
        if artifact:
            extra: dict[str, Any] = {
                "error": record.error,
                "duration_ms": record.duration_ms,
                "timed_out": record.timed_out,
                "test_id": record.test_id,
                "title": record.test_doc.get("title", ""),
            }
            if ai_calls > 0:
                extra.update({
                    "ai_video_calls": ai_calls,
                    "ai_input_tokens": ai_in_tokens,
                    "ai_output_tokens": ai_out_tokens,
                    "ai_cost_usd": round(ai_cost, 6),
                    "ai_model": getattr(self._gemini, "model", "unknown"),
                })
            artifact.write_result(
                bridge_result=record.bridge_result,
                ai_result=record.ai_result,
                final_verdict=record.final_verdict,
                extra=extra,
            )
            logger.info(
                "Video AI result for %s: ai=%s verdict=%s",
                record.test_id,
                record.ai_result,
                record.final_verdict,
            )

    @staticmethod
    def _update_ai_result(record: Any, ai_result: str, reason: str) -> None:
        """Update record.ai_result and rewrite result.json."""
        record.ai_result = ai_result
        if not record.error and reason:
            record.error = reason

        record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)

        artifact = record.artifact
        if artifact:
            artifact.write_result(
                bridge_result=record.bridge_result,
                ai_result=record.ai_result,
                final_verdict=record.final_verdict,
                extra={
                    "error": record.error,
                    "duration_ms": record.duration_ms,
                    "timed_out": getattr(record, "timed_out", False),
                    "test_id": record.test_id,
                    "title": record.test_doc.get("title", ""),
                },
            )

    # ------------------------------------------------------------------
    # Run processing (one execution, many case verdicts)
    # ------------------------------------------------------------------

    @staticmethod
    def _match_expected(
        data: dict[str, Any], expected: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """Compare an AI output dict against an expected subset; return (passed, mismatches)."""
        mismatches: list[str] = []
        for key, exp_val in (expected or {}).items():
            act_val = data.get(key)
            if act_val != exp_val:
                mismatches.append(f"{key}: got {act_val!r}, expected {exp_val!r}")
        return (len(mismatches) == 0, mismatches)

    @staticmethod
    def _step_case_bindings(ai_block: dict[str, Any], step: dict[str, Any]) -> dict[str, dict]:
        """Map each case the step serves to the expected subset it is judged against.

        Uses ``expected_by_case`` when present, else the single ``expected`` applied
        to every id in the step's ``case`` tag.
        """
        ebc = ai_block.get("expected_by_case")
        if isinstance(ebc, dict):
            return {cid: (sub if isinstance(sub, dict) else {}) for cid, sub in ebc.items()}
        expected = ai_block.get("expected", {})
        tag = step.get("case")
        if isinstance(tag, str):
            cids = [tag]
        elif isinstance(tag, list):
            cids = [c for c in tag if isinstance(c, str)]
        else:
            cids = []
        return {cid: expected for cid in cids}

    def _set_case_ai(self, record: Any, case_id: str, ai_result: str, error: str | None) -> None:
        cv = record.cases.get(case_id)
        if cv is None:
            return
        cv.ai_result = ai_result
        if ai_result == "fail" and error and not cv.error:
            cv.error = error

    def _write_run_result(
        self, record: Any, ai_calls: int, ai_in: int, ai_out: int, ai_cost: float
    ) -> None:
        # Recompute each case's final verdict now that AI results are known.
        for cv in record.cases.values():
            cv.final_verdict = _compute_verdict(cv.bridge_result, cv.ai_result)
        extra: dict[str, Any] = {
            "run_id": record.run_id,
            "title": record.run_doc.get("title", ""),
            "duration_ms": record.duration_ms,
            "timed_out": record.timed_out,
            "error": record.error,
        }
        if ai_calls > 0:
            extra.update({
                "ai_video_calls": ai_calls,
                "ai_input_tokens": ai_in,
                "ai_output_tokens": ai_out,
                "ai_cost_usd": round(ai_cost, 6),
                "ai_model": getattr(self._gemini, "model", "unknown"),
            })
        if record.artifact:
            record.artifact.write_run_result(cases=record.cases_as_dicts(), extra=extra)

    def _fail_run_cases_ai(self, record: Any, reason: str) -> None:
        """Mark every still-pending case's AI as failed (run-global failure)."""
        for cv in record.cases.values():
            if cv.ai_result == "pending":
                cv.ai_result = "fail"
                if not cv.error:
                    cv.error = reason
        self._write_run_result(record, 0, 0, 0, 0.0)

    async def _process_run(self, record: Any) -> None:
        """Resolve a run's deferred video AI asserts, attributing each to its case(s)."""
        artifact = record.artifact
        if artifact is None:
            return

        timeline_path = artifact.timeline_path
        if not timeline_path.exists():
            self._fail_run_cases_ai(record, "timeline.json not found")
            return
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._fail_run_cases_ai(record, f"Could not read timeline.json: {exc}")
            return

        video_steps = [
            s for s in record.run_doc.get("steps", [])
            if s.get("assert", {}).get("ai", {}).get("mode") == "video"
        ]
        if not video_steps:
            return
        if self._gemini is None:
            self._fail_run_cases_ai(record, "GeminiVideoHelper not configured")
            return

        # References are attached inline per call (the scoped reference pack), so there is
        # no shared per-run asset upload — context caching is a no-op kept only for the
        # helper's API signature.
        context_id = "inline"

        video_path = artifact.video_path
        ai_calls = ai_in = ai_out = 0
        ai_cost = 0.0
        try:
            for step in video_steps:
                ai_block = step["assert"]["ai"]
                video_trim = ai_block.get("video_trim", {})
                prompt = ai_block.get("prompt", "")
                output_schema = ai_block.get("output_schema", {})
                step_id = step.get("id", "")
                bindings = self._step_case_bindings(ai_block, step)

                timestamps = _resolve_trim_timestamps(
                    timeline, video_trim.get("from_step", ""), video_trim.get("to_step", "")
                )
                if timestamps is None:
                    for cid in bindings:
                        self._set_case_ai(record, cid, "fail", f"Could not resolve trim for step {step_id}")
                    continue

                start_s, end_s = timestamps
                trimmed_path = artifact.test_dir / f"trimmed-{step_id}.webm"
                trim_ok = await asyncio.to_thread(_trim_video, video_path, trimmed_path, start_s, end_s)
                if not trim_ok:
                    for cid in bindings:
                        self._set_case_ai(record, cid, "fail", f"ffmpeg trimming failed for step {step_id}")
                    continue

                reference = self._video_reference_for(record, ai_block)
                try:
                    async with self._sem:
                        gemini_result = await asyncio.to_thread(
                            self._gemini.analyse_video,
                            trimmed_path, context_id, prompt, output_schema, None, reference,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Run %s video AI failed: %s", record.run_id, exc)
                    for cid in bindings:
                        self._set_case_ai(record, cid, "fail", f"AI error: {exc}")
                    continue

                # Cost accounting (a failed-judgment call still bills).
                cost_fn = getattr(self._gemini, "cost_for_tokens", None)
                call_cost = cost_fn(gemini_result.input_tokens, gemini_result.output_tokens) if cost_fn else 0.0
                ai_calls += 1
                ai_in += gemini_result.input_tokens
                ai_out += gemini_result.output_tokens
                ai_cost += call_cost

                per_case: dict[str, Any] = {}
                if not gemini_result.success:
                    for cid in bindings:
                        self._set_case_ai(record, cid, "fail", gemini_result.reason)
                        per_case[cid] = {"passed": False, "reason": gemini_result.reason}
                else:
                    data = gemini_result.data or {}
                    for cid, subset in bindings.items():
                        passed, mismatches = self._match_expected(data, subset)
                        self._set_case_ai(
                            record, cid, "pass" if passed else "fail",
                            "; ".join(mismatches) if mismatches else None,
                        )
                        per_case[cid] = {"passed": passed, "mismatches": mismatches}

                # Persist step-N-ai.json (per-case verdicts for this single AI call).
                step_num = self._extract_step_number(step_id)
                if step_num is not None:
                    ai_data = {
                        "data": gemini_result.data if gemini_result.success else None,
                        "expected_by_case": bindings,
                        "per_case": per_case,
                        "input_tokens": gemini_result.input_tokens,
                        "output_tokens": gemini_result.output_tokens,
                        "cost_usd": round(call_cost, 6),
                        "model": getattr(self._gemini, "model", "unknown"),
                        "raw_response": gemini_result.raw_text,
                    }
                    artifact.ai_result_path(step_num).write_text(
                        json.dumps(ai_data, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
        finally:
            try:
                await asyncio.to_thread(self._gemini.release_context, context_id)
            except Exception:  # noqa: BLE001
                pass

        self._write_run_result(record, ai_calls, ai_in, ai_out, ai_cost)
        logger.info(
            "Run %s video AI done: %s",
            record.run_id,
            {cid: cv.final_verdict for cid, cv in record.cases.items()},
        )

    @staticmethod
    def _extract_step_number(step_id: str) -> int | None:
        """Extract integer from step id like 'step-02' -> 2."""
        if not step_id:
            return None
        parts = step_id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])
        return None


# ---------------------------------------------------------------------------
# Standalone CLI for reprocessing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reprocess video AI assertions for a completed suite run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python framework/runner/video_ai_processor.py "
            "--results-dir test-results/zeus/dev/2026-04-07-suite-06 --game zeus"
        ),
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        metavar="DIR",
        help="Path to the suite run results directory containing per-test subdirs",
    )
    parser.add_argument(
        "--game",
        required=True,
        metavar="GAME",
        help="Game identifier (e.g. zeus)",
    )
    parser.add_argument(
        "--base-dir",
        metavar="DIR",
        default=None,
        help="qa-platform repository root (default: auto-detected)",
    )
    return parser


async def _reprocess_async(args: argparse.Namespace) -> int:
    """
    Standalone reprocessing: scan a results directory for pending video AI tests
    and rerun Gemini analysis on them.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    base_dir = Path(args.base_dir).resolve() if args.base_dir else _REPO_ROOT
    results_dir = Path(args.results_dir).resolve()

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable not set")
        return 1

    # Find all result.json files with ai_result=pending
    pending_tests: list[Path] = []
    for result_file in sorted(results_dir.rglob("result.json")):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            if data.get("ai_result") == "pending":
                pending_tests.append(result_file)
        except (json.JSONDecodeError, OSError):
            pass

    if not pending_tests:
        logger.info("No pending video AI tests found in %s", results_dir)
        return 0

    logger.info("Found %d pending video AI test(s)", len(pending_tests))

    from framework.helpers.gemini_video import GeminiVideoHelper
    from framework.helpers.symbol_reference import load_symbol_reference

    # Symbol/UI reference pack — prepended to every request (same as the live runner).
    symbol_reference = load_symbol_reference(args.game, base_dir)
    gemini = GeminiVideoHelper(api_key, reference=symbol_reference)

    # Build minimal stub records from result.json + timeline.json
    processed = 0
    failed = 0
    for result_file in pending_tests:
        test_dir = result_file.parent
        try:
            result_data = json.loads(result_file.read_text(encoding="utf-8"))
            timeline_path = test_dir / "timeline.json"
            if not timeline_path.exists():
                logger.warning("No timeline.json in %s — skipping", test_dir)
                failed += 1
                continue
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read artifacts in %s: %s", test_dir, exc)
            failed += 1
            continue

        logger.info("Reprocessing: %s", test_dir.name)

        # We need the suite YAML to get the step prompts/schemas.
        # Parse test_id and suite name from dir name: {date}-{suite}-{test_id}
        dir_name = test_dir.name
        test_id = result_data.get("test_id", "")
        # Suite name extraction: strip leading date and trailing test_id
        # Pattern: 2026-04-07-suite-06-freespin-ZZ-30 -> suite = "suite-06-freespin"
        suite_match = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)-[A-Z]+-\d+$", dir_name)
        suite_name = suite_match.group(1) if suite_match else ""

        suite_path = base_dir / "games" / args.game / "test-suites" / f"{suite_name}.yml"
        if not suite_path.exists():
            logger.warning("Suite file not found: %s — skipping", suite_path)
            failed += 1
            continue

        import yaml
        with suite_path.open("r", encoding="utf-8") as fh:
            suite_doc = yaml.safe_load(fh)

        # Find the test in the suite (flat or grouped)
        test_doc = next(
            (t for t in _iter_suite_tests(suite_doc) if t.get("id") == test_id),
            None,
        )
        if test_doc is None:
            logger.warning("Test %s not found in suite %s — skipping", test_id, suite_name)
            failed += 1
            continue

        # Build a minimal stub record
        from framework.helpers.artifact import ArtifactContext

        # Derive artifact params from directory path and result
        # test_dir: base_dir/test-results/{game}/{env}/{date-suite-testid}
        # We need env; parse from path
        parts = test_dir.parts
        try:
            results_idx = next(i for i, p in enumerate(parts) if p == "test-results")
            env = parts[results_idx + 2]
        except (StopIteration, IndexError):
            env = "unknown"

        # Re-create ArtifactContext pointing at the existing test_dir
        # run_dir is the parent of the test directory
        artifact = ArtifactContext(
            run_dir=test_dir.parent,
            test_id=test_id,
        )

        # Stub record-like object
        class _StubRecord:
            pass

        stub = _StubRecord()
        stub.test_id = test_id  # type: ignore[attr-defined]
        stub.test_doc = test_doc  # type: ignore[attr-defined]
        stub.artifact = artifact  # type: ignore[attr-defined]
        stub.bridge_result = result_data.get("bridge_result", "skip")  # type: ignore[attr-defined]
        stub.ai_result = "pending"  # type: ignore[attr-defined]
        stub.final_verdict = "pending"  # type: ignore[attr-defined]
        stub.error = result_data.get("error")  # type: ignore[attr-defined]
        stub.duration_ms = result_data.get("duration_ms", 0)  # type: ignore[attr-defined]
        stub.timed_out = result_data.get("timed_out", False)  # type: ignore[attr-defined]

        # Skip tests with no video AI steps. Reference assets are injected globally
        # by the helper (symbol_reference), so no per-test asset upload is needed.
        video_steps = [
            step for step in test_doc.get("steps", [])
            if step.get("assert", {}).get("ai", {}).get("mode") == "video"
        ]
        if not video_steps:
            logger.info("No video AI steps found in test %s — skipping", test_id)
            continue

        context_id = None

        processor = VideoAiProcessor(
            game=args.game,
            suite_doc=suite_doc,
            base_dir=base_dir,
            gemini_video_helper=gemini,
        )
        try:
            await processor._process_test(stub, context_id)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Processing failed for %s: %s", test_id, exc)
            VideoAiProcessor._update_ai_result(stub, "fail", str(exc))
            failed += 1
        finally:
            try:
                gemini.release_context(context_id)
            except Exception:  # noqa: BLE001
                pass

    logger.info("Reprocessing complete: %d processed, %d failed", processed, failed)
    return 0 if failed == 0 else 1


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_reprocess_async(args)))


if __name__ == "__main__":
    main()
