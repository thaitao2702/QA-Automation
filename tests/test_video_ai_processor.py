"""
Tests for framework/runner/video_ai_processor.py

Acceptance criteria:
- Standalone video tests are discovered (_extract_video_test_ids) and processed each
  on its own — no ref_assets batching.
- notify_test_complete queues a test; wait_all dispatches + awaits it and never leaves
  a verdict at 'pending' (safety net).
- Video trimming uses correct start/end from timeline timestamps.
- Gemini failure marks test as ai_result=fail with a reason.
- Standalone CLI finds result.json files with ai_result=pending.
- _trim_video returns False when ffmpeg is missing or fails.
- _resolve_trim_timestamps returns None for unknown step IDs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.runner.video_ai_processor import (
    VideoAiProcessor,
    _extract_video_test_ids,
    _iter_suite_runs,
    _resolve_trim_timestamps,
    _trim_video,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_suite_doc(tests: list[dict]) -> dict:
    return {
        "suite": "Test Suite",
        "game": "zeus",
        "version": "1.0",
        "config": {"test_timeout_ms": 60000, "suite_timeout_ms": 600000},
        "tests": tests,
    }


def _make_video_test(test_id: str, assets: list[str] | None = None) -> dict:
    ai: dict = {
        "mode": "video",
        "video_trim": {"from_step": "step-01", "to_step": "step-02"},
        "prompt": "Did reels spin?",
        "output_schema": {"spun": {"type": "boolean"}},
        "expected": {"spun": True},
    }
    if assets:
        ai["assets"] = assets
    return {
        "id": test_id,
        "title": f"Test {test_id}",
        "steps": [{"id": "step-01", "assert": {"ai": ai}}],
    }


def _make_stub_record(
    test_id: str,
    test_doc: dict,
    artifact_dir: Path | None = None,
) -> Any:
    """Create a minimal stub mimicking SuiteRunner.TestRecord."""

    class _Stub:
        pass

    stub = _Stub()
    stub.test_id = test_id
    stub.test_doc = test_doc
    stub.bridge_result = "pass"
    stub.ai_result = "pending"
    stub.final_verdict = "pending"
    stub.error = None
    stub.duration_ms = 1000
    stub.timed_out = False

    if artifact_dir is not None:
        from framework.helpers.artifact import ArtifactContext
        from datetime import date

        artifact_dir.mkdir(parents=True, exist_ok=True)
        ctx = ArtifactContext.__new__(ArtifactContext)
        ctx._base_dir = artifact_dir.parent.parent.parent.parent
        ctx._game = "zeus"
        ctx._env = "dev"
        ctx._suite = "suite-test"
        ctx._test_id = test_id
        ctx._run_date = date(2026, 4, 10)
        ctx._screenshot_counter = 0
        stub.artifact = ctx
    else:
        stub.artifact = None

    return stub


# ---------------------------------------------------------------------------
# extract_video_test_ids
# ---------------------------------------------------------------------------


class TestExtractVideoTestIds:
    def test_collects_ids_of_video_tests(self) -> None:
        suite = _make_suite_doc([
            _make_video_test("ZZ-01", ["symbols.scatter"]),
            _make_video_test("ZZ-02"),
            _make_video_test("ZZ-03", ["symbols.coin"]),
        ])
        assert _extract_video_test_ids(suite) == {"ZZ-01", "ZZ-02", "ZZ-03"}

    def test_non_video_tests_excluded(self) -> None:
        suite = _make_suite_doc([
            {
                "id": "ZZ-01",
                "title": "No video",
                "steps": [{"id": "step-01", "action": "capture"}],
            }
        ])
        assert _extract_video_test_ids(suite) == set()


# ---------------------------------------------------------------------------
# _resolve_trim_timestamps
# ---------------------------------------------------------------------------


class TestResolveTrimTimestamps:
    def _make_timeline(self, steps: list[dict]) -> dict:
        return {
            "video_start_time": 1000.0,
            "event_collection_start_time": 1000.5,
            "video_event_offset": 0.5,
            "steps": steps,
        }

    def test_resolves_start_and_end(self) -> None:
        timeline = self._make_timeline([
            {"id": "step-01", "action": "capture", "start_ms": 1000, "end_ms": 2000},
            {"id": "step-02", "action": "capture", "start_ms": 2000, "end_ms": 3000},
        ])
        result = _resolve_trim_timestamps(timeline, "step-01", "step-02")
        assert result is not None
        start_s, end_s = result
        # step-01 start: 1000ms = 1.0s + 0.5s offset = 1.5s
        assert abs(start_s - 1.5) < 0.01
        # step-02 end: 3000ms = 3.0s + 0.5s offset = 3.5s
        assert abs(end_s - 3.5) < 0.01

    def test_unknown_from_step_returns_none(self) -> None:
        timeline = self._make_timeline([
            {"id": "step-01", "action": "capture", "start_ms": 0, "end_ms": 1000},
        ])
        result = _resolve_trim_timestamps(timeline, "step-99", "step-01")
        assert result is None

    def test_unknown_to_step_returns_none(self) -> None:
        timeline = self._make_timeline([
            {"id": "step-01", "action": "capture", "start_ms": 0, "end_ms": 1000},
        ])
        result = _resolve_trim_timestamps(timeline, "step-01", "step-99")
        assert result is None

    def test_negative_start_clamped_to_zero(self) -> None:
        timeline = self._make_timeline([
            {"id": "step-01", "action": "capture", "start_ms": 0, "end_ms": 1000},
            {"id": "step-02", "action": "capture", "start_ms": 1000, "end_ms": 2000},
        ])
        # No offset — both steps at their exact ms values
        timeline["video_event_offset"] = 0.0
        result = _resolve_trim_timestamps(timeline, "step-01", "step-02")
        assert result is not None
        start_s, _ = result
        assert start_s >= 0.0


# ---------------------------------------------------------------------------
# _trim_video
# ---------------------------------------------------------------------------


class TestTrimVideo:
    def test_returns_false_when_source_missing(self, tmp_path: Path) -> None:
        source = tmp_path / "nonexistent.webm"
        dest = tmp_path / "trimmed.webm"
        result = _trim_video(source, dest, 0.0, 5.0)
        assert result is False

    def test_returns_false_when_ffmpeg_not_found(self, tmp_path: Path) -> None:
        source = tmp_path / "source.webm"
        source.write_bytes(b"\x1aE\xdf\xa3")  # minimal WebM magic bytes
        dest = tmp_path / "trimmed.webm"

        with patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg not found")):
            result = _trim_video(source, dest, 0.0, 5.0)

        assert result is False

    def test_returns_false_when_ffmpeg_exits_nonzero(self, tmp_path: Path) -> None:
        source = tmp_path / "source.webm"
        source.write_bytes(b"\x1aE\xdf\xa3")
        dest = tmp_path / "trimmed.webm"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ffmpeg error"

        with patch("subprocess.run", return_value=mock_result):
            result = _trim_video(source, dest, 0.0, 5.0)

        assert result is False

    def test_returns_true_on_success(self, tmp_path: Path) -> None:
        source = tmp_path / "source.webm"
        source.write_bytes(b"\x1aE\xdf\xa3")
        dest = tmp_path / "trimmed.webm"

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = _trim_video(source, dest, 0.0, 5.0)

        assert result is True


# ---------------------------------------------------------------------------
# VideoAiProcessor — per-test dispatch (no batching)
# ---------------------------------------------------------------------------


def _proc(suite: dict, tmp_path: Path) -> VideoAiProcessor:
    return VideoAiProcessor(
        game="zeus", suite_doc=suite, base_dir=tmp_path, gemini_video_helper=None
    )


class TestVideoAiProcessorDispatch:
    def test_notify_queues_test_without_dispatching(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([_make_video_test("ZZ-01")])
        processor = _proc(suite, tmp_path)
        record1 = _make_stub_record("ZZ-01", suite["tests"][0])

        asyncio.get_event_loop().run_until_complete(processor.notify_test_complete(record1))
        # Queued, not yet dispatched — tasks are created in wait_all().
        assert processor._ready_tests == [record1]
        assert len(processor._tasks) == 0

    def test_unknown_test_id_not_queued(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([_make_video_test("ZZ-01")])
        processor = _proc(suite, tmp_path)
        other = _make_stub_record("ZZ-99", {"id": "ZZ-99", "steps": []})

        asyncio.get_event_loop().run_until_complete(processor.notify_test_complete(other))
        assert processor._ready_tests == []

    def test_wait_all_processes_each_queued_test(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([_make_video_test("ZZ-01"), _make_video_test("ZZ-02")])
        processor = _proc(suite, tmp_path)
        r1 = _make_stub_record("ZZ-01", suite["tests"][0])
        r2 = _make_stub_record("ZZ-02", suite["tests"][1])
        loop = asyncio.get_event_loop()

        loop.run_until_complete(processor.notify_test_complete(r1))
        loop.run_until_complete(processor.notify_test_complete(r2))
        loop.run_until_complete(processor.wait_all())
        # Each test processed independently; with no Gemini both fail (never pending).
        assert r1.ai_result == "fail"
        assert r2.ai_result == "fail"

    def test_no_gemini_marks_tests_as_fail(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([_make_video_test("ZZ-01")])
        processor = _proc(suite, tmp_path)
        record1 = _make_stub_record("ZZ-01", suite["tests"][0])
        loop = asyncio.get_event_loop()

        loop.run_until_complete(processor.notify_test_complete(record1))
        loop.run_until_complete(processor.wait_all())

        assert record1.ai_result == "fail"
        assert "GeminiVideoHelper" in (record1.error or "")

    def test_safety_net_fails_unresolved_pending(self, tmp_path: Path) -> None:
        # The wait_all safety net must never leave a dispatched video assert at 'pending'.
        suite = _make_suite_doc([_make_video_test("ZZ-01")])
        processor = _proc(suite, tmp_path)
        record1 = _make_stub_record("ZZ-01", suite["tests"][0])
        record1.ai_result = "pending"

        processor._fail_if_still_pending(record1)
        assert record1.ai_result == "fail"
        assert "did not complete" in (record1.error or "")

    def test_wait_all_completes_without_tasks(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([])
        processor = VideoAiProcessor(
            game="zeus",
            suite_doc=suite,
            base_dir=tmp_path,
            gemini_video_helper=None,
        )
        # Should not raise
        asyncio.get_event_loop().run_until_complete(processor.wait_all())

    def test_helper_without_cost_api_folds_to_verdict_not_pending(self, tmp_path: Path) -> None:
        # Regression: the claude provider's video helper exposes analyse_video but (pre-fix)
        # had no `model` / `cost_for_tokens`. _process_test's cost bookkeeping runs OUTSIDE
        # its try, so an AttributeError there propagated, was swallowed by wait_all's gather,
        # and left the test silently 'pending' for the safety net to mark "did not complete"
        # — even though the AI call succeeded. See HANDOFF-video-ai-fold.md.
        from framework.helpers.artifact import ArtifactContext
        from framework.helpers.gemini_video import VideoResult

        class _ClaudeLikeHelper:
            """A video helper that, like the real ClaudeVideoHelper, exposes analyse_video
            but deliberately NO `model` / `cost_for_tokens` — so _process_test must resolve
            them defensively (getattr) instead of crashing."""

            def __init__(self, data: dict) -> None:
                self._data = data
                self.calls = 0

            def analyse_video(self, *args: Any, **kwargs: Any) -> VideoResult:
                self.calls += 1
                return VideoResult(
                    success=True, data=self._data, reason=None,
                    input_tokens=0, output_tokens=0, raw_text=json.dumps(self._data),
                )

        suite = _make_suite_doc([_make_video_test("ZZ-31")])
        helper = _ClaudeLikeHelper({"spun": True})  # expected {"spun": True} -> pass
        processor = VideoAiProcessor("zeus", suite, tmp_path, gemini_video_helper=helper)

        artifact = ArtifactContext(run_dir=tmp_path, test_id="ZZ-31")
        artifact.timeline_path.write_text(
            json.dumps({
                "video_event_offset": 0.0,
                "steps": [
                    {"id": "step-01", "start_ms": 0, "end_ms": 1000},
                    {"id": "step-02", "start_ms": 1000, "end_ms": 2000},
                ],
            }),
            encoding="utf-8",
        )
        rec = _make_stub_record("ZZ-31", suite["tests"][0])
        rec.artifact = artifact

        loop = asyncio.get_event_loop()
        loop.run_until_complete(processor.notify_test_complete(rec))
        with patch("framework.runner.video_ai_processor._trim_video", return_value=True):
            loop.run_until_complete(processor.wait_all())

        # A real verdict from the successful AI call — NOT the safety-net 'pending'->fail.
        assert helper.calls == 1
        assert rec.ai_result == "pass"
        assert rec.final_verdict == "green"
        assert "did not complete" not in (rec.error or "")
        # step-01-ai.json is written (the crash used to happen before this), and the
        # missing-model fallback is recorded rather than raising.
        ai_json = json.loads(artifact.ai_result_path(1).read_text(encoding="utf-8"))
        assert ai_json["passed"] is True
        assert ai_json["model"] == "unknown"
        assert ai_json["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# _update_ai_result
# ---------------------------------------------------------------------------


class TestUpdateAiResult:
    def test_sets_ai_result_and_error(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([_make_video_test("ZZ-01", ["scatter"])])
        record = _make_stub_record("ZZ-01", suite["tests"][0])

        VideoAiProcessor._update_ai_result(record, "fail", "Gemini API unavailable")

        assert record.ai_result == "fail"
        assert record.error == "Gemini API unavailable"

    def test_recomputes_final_verdict(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([_make_video_test("ZZ-01", ["scatter"])])
        record = _make_stub_record("ZZ-01", suite["tests"][0])
        record.bridge_result = "fail"

        VideoAiProcessor._update_ai_result(record, "fail", "Gemini down")

        assert record.final_verdict == "red"


# ---------------------------------------------------------------------------
# Runs — per-case AI attribution (one AI call, many case verdicts)
# ---------------------------------------------------------------------------


def _gemini_ok(data: dict, in_tok: int = 100, out_tok: int = 10) -> MagicMock:
    g = MagicMock()
    g.create_cached_context = MagicMock(return_value="ctx")
    g.release_context = MagicMock(return_value=None)
    g.model = "gemini-3-flash-preview"
    g.cost_for_tokens = MagicMock(return_value=0.001)
    result = MagicMock()
    result.success = True
    result.data = data
    result.reason = None
    result.input_tokens = in_tok
    result.output_tokens = out_tok
    result.raw_text = json.dumps(data)
    g.analyse_video = MagicMock(return_value=result)
    return g


class TestMatchExpected:
    def test_all_match(self) -> None:
        ok, mm = VideoAiProcessor._match_expected({"a": 1, "b": True}, {"a": 1})
        assert ok is True and mm == []

    def test_mismatch_listed(self) -> None:
        ok, mm = VideoAiProcessor._match_expected({"a": 2}, {"a": 1})
        assert ok is False and "a" in mm[0]


class TestIterSuiteRuns:
    def test_top_level_and_group_runs(self) -> None:
        suite = {
            "runs": [{"id": "r1"}],
            "groups": [{"id": "g", "runs": [{"id": "r2"}]}],
        }
        ids = {r["id"] for r in _iter_suite_runs(suite)}
        assert ids == {"r1", "r2"}


class TestProcessRunPerCase:
    def _make_run_record(self, tmp_path: Path):
        from framework.runner.suite_runner import RunRecord
        from framework.helpers.artifact import ArtifactContext

        run_doc = {
            "id": "fs-run",
            "title": "bonus",
            "cases": [
                {"id": "ZZ-32", "title": "coins held"},
                {"id": "ZZ-33", "title": "only non-coin spin"},
            ],
            "steps": [
                {
                    "id": "step-05",
                    "assert": {
                        "ai": {
                            "mode": "video",
                            "video_trim": {"from_step": "step-01", "to_step": "step-04"},
                            "prompt": "report",
                            "output_schema": {
                                "coins_held": {"type": "boolean"},
                                "only_noncoin_cells_spin": {"type": "boolean"},
                            },
                            "expected_by_case": {
                                "ZZ-32": {"coins_held": True},
                                "ZZ-33": {"only_noncoin_cells_spin": True},
                            },
                        }
                    },
                },
            ],
        }
        rec = RunRecord(run_doc, 0)
        artifact = ArtifactContext(run_dir=tmp_path, test_id="fs-run")
        artifact.test_dir.mkdir(parents=True, exist_ok=True)
        artifact.timeline_path.write_text(json.dumps({"steps": [], "video_event_offset": 0.0}), encoding="utf-8")
        rec.artifact = artifact
        # both cases start pending (they have a video assert)
        for cv in rec.cases.values():
            cv.ai_result = "pending"
            cv.ai_pending_steps = [run_doc["steps"][0]]
        return rec

    def test_one_call_partitions_across_cases(self, tmp_path: Path) -> None:
        rec = self._make_run_record(tmp_path)
        # AI says coins held (ZZ-32 ✓) but non-coin-only is False (ZZ-33 ✗)
        gemini = _gemini_ok({"coins_held": True, "only_noncoin_cells_spin": False})
        proc = VideoAiProcessor("zeus", {"runs": [rec.run_doc]}, tmp_path, gemini_video_helper=gemini)

        with patch("framework.runner.video_ai_processor._trim_video", return_value=True), \
             patch("framework.runner.video_ai_processor._resolve_trim_timestamps", return_value=(0.0, 1.0)):
            asyncio.run(proc._process_run(rec))

        assert rec.cases["ZZ-32"].ai_result == "pass"
        assert rec.cases["ZZ-33"].ai_result == "fail"
        # ONE Gemini call served both cases
        assert gemini.analyse_video.call_count == 1
        # final verdicts recomputed (bridge skip + ai pass -> green; + ai fail -> orange)
        assert rec.cases["ZZ-32"].final_verdict == "green"
        assert rec.cases["ZZ-33"].final_verdict == "orange"
        # result.json written with kind=run
        data = json.loads((rec.artifact.test_dir / "result.json").read_text())
        assert data["kind"] == "run"
        assert {c["id"] for c in data["cases"]} == {"ZZ-32", "ZZ-33"}

    def test_missing_timeline_fails_all_cases(self, tmp_path: Path) -> None:
        rec = self._make_run_record(tmp_path)
        rec.artifact.timeline_path.unlink()  # remove timeline
        gemini = _gemini_ok({"coins_held": True})
        proc = VideoAiProcessor("zeus", {"runs": [rec.run_doc]}, tmp_path, gemini_video_helper=gemini)

        asyncio.run(proc._process_run(rec))

        assert rec.cases["ZZ-32"].ai_result == "fail"
        assert rec.cases["ZZ-33"].ai_result == "fail"
        gemini.analyse_video.assert_not_called()


# ---------------------------------------------------------------------------
# Scoped reference resolution for video AI asserts (_video_reference_for)
# ---------------------------------------------------------------------------


def _video_cache_and_index(tmp_path: Path):
    from framework.helpers.symbol_reference import ScopedReferenceCache, build_asset_index

    assets = tmp_path / "games" / "g" / "docs" / "assets"
    (assets / "ui").mkdir(parents=True)
    (assets / "symbols").mkdir(parents=True)
    (assets / "ui" / "bet.jpg").write_bytes(b"x")
    (assets / "symbols" / "coin.jpg").write_bytes(b"x")
    (assets / "assets.yml").write_text(
        "ui_elements:\n  bet_label: { file: ui/bet.jpg, name: Bet }\n"
        "symbols:\n  coin: { file: symbols/coin.jpg, name: Coin }\n",
        encoding="utf-8",
    )
    return ScopedReferenceCache("g", tmp_path), build_asset_index("g", tmp_path)


class TestVideoReferenceFor:
    def _proc(self, tmp_path, with_cache=True):
        kwargs = {}
        if with_cache:
            cache, index = _video_cache_and_index(tmp_path)
            kwargs = {"ref_cache": cache, "asset_index": index}
        return VideoAiProcessor(
            game="g", suite_doc=_make_suite_doc([]), base_dir=tmp_path,
            gemini_video_helper=MagicMock(), **kwargs,
        )

    def test_no_cache_falls_back_to_record_reference(self, tmp_path: Path):
        proc = self._proc(tmp_path, with_cache=False)
        rec = _make_stub_record("ZZ-1", {})
        rec.reference = "RECORD_REF"
        assert proc._video_reference_for(rec, {"mode": "video"}) == "RECORD_REF"

    def test_assert_level_selection_scopes_reference(self, tmp_path: Path):
        proc = self._proc(tmp_path)
        rec = _make_stub_record("ZZ-1", {})
        rec.group_doc = None
        rec.reference = None
        ref = proc._video_reference_for(rec, {"mode": "video", "assets": ["symbols.coin"]})
        assert [Path(p).name for p, _l in ref.items] == ["coin.jpg"]

    def test_group_level_default_applies_when_assert_unset(self, tmp_path: Path):
        proc = self._proc(tmp_path)
        rec = _make_stub_record("ZZ-1", {})
        rec.group_doc = {"asset_groups": ["ui_elements"]}
        rec.reference = None
        ref = proc._video_reference_for(rec, {"mode": "video"})
        assert [Path(p).name for p, _l in ref.items] == ["bet.jpg"]
