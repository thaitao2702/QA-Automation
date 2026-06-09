"""
Tests for framework/runner/video_ai_processor.py

Acceptance criteria:
- Tests are correctly grouped by sorted ref_assets key.
- notify_test_complete dispatches a batch only when ALL tests in a group are done.
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
    _group_key,
    _extract_video_tests,
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


def _make_video_test(test_id: str, ref_assets: list[str]) -> dict:
    return {
        "id": test_id,
        "title": f"Test {test_id}",
        "steps": [
            {
                "id": "step-01",
                "assert": {
                    "ai": {
                        "mode": "video",
                        "video_trim": {"from_step": "step-01", "to_step": "step-02"},
                        "ref_assets": ref_assets,
                        "prompt": "Did reels spin?",
                        "output_schema": {"spun": {"type": "boolean"}},
                        "expected": {"spun": True},
                    }
                },
            }
        ],
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
# Group key
# ---------------------------------------------------------------------------


class TestGroupKey:
    def test_sorted_assets(self) -> None:
        assert _group_key(["wild", "scatter"]) == "scatter,wild"

    def test_single_asset(self) -> None:
        assert _group_key(["coin"]) == "coin"

    def test_empty(self) -> None:
        assert _group_key([]) == ""


# ---------------------------------------------------------------------------
# extract_video_tests
# ---------------------------------------------------------------------------


class TestExtractVideoTests:
    def test_groups_tests_by_ref_assets(self) -> None:
        suite = _make_suite_doc([
            _make_video_test("ZZ-01", ["scatter", "wild"]),
            _make_video_test("ZZ-02", ["scatter", "wild"]),
            _make_video_test("ZZ-03", ["coin"]),
        ])
        groups = _extract_video_tests(suite)
        assert "scatter,wild" in groups
        assert "coin" in groups
        assert len(groups["scatter,wild"]) == 2
        assert len(groups["coin"]) == 1

    def test_non_video_tests_excluded(self) -> None:
        suite = _make_suite_doc([
            {
                "id": "ZZ-01",
                "title": "No video",
                "steps": [{"id": "step-01", "action": "capture"}],
            }
        ])
        groups = _extract_video_tests(suite)
        assert len(groups) == 0


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
# VideoAiProcessor — batch dispatch
# ---------------------------------------------------------------------------


class TestVideoAiProcessorBatchDispatch:
    def test_batch_not_dispatched_until_all_complete(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([
            _make_video_test("ZZ-01", ["scatter"]),
            _make_video_test("ZZ-02", ["scatter"]),
        ])

        tasks_created = []
        processor = VideoAiProcessor(
            game="zeus",
            suite_doc=suite,
            base_dir=tmp_path,
            gemini_video_helper=None,
        )

        record1 = _make_stub_record("ZZ-01", suite["tests"][0])
        record1.ai_pending_steps = [suite["tests"][0]["steps"][0]]

        # Notify only first test — batch should NOT be dispatched yet
        asyncio.get_event_loop().run_until_complete(
            processor.notify_test_complete(record1)
        )
        assert len(processor._tasks) == 0

    def test_batch_dispatched_when_all_complete(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([
            _make_video_test("ZZ-01", ["scatter"]),
            _make_video_test("ZZ-02", ["scatter"]),
        ])

        processor = VideoAiProcessor(
            game="zeus",
            suite_doc=suite,
            base_dir=tmp_path,
            gemini_video_helper=None,  # no Gemini — process_group handles gracefully
        )

        record1 = _make_stub_record("ZZ-01", suite["tests"][0])
        record1.ai_pending_steps = [suite["tests"][0]["steps"][0]]

        record2 = _make_stub_record("ZZ-02", suite["tests"][1])
        record2.ai_pending_steps = [suite["tests"][1]["steps"][0]]

        asyncio.get_event_loop().run_until_complete(
            processor.notify_test_complete(record1)
        )
        asyncio.get_event_loop().run_until_complete(
            processor.notify_test_complete(record2)
        )
        # After both, a task should have been created
        assert len(processor._tasks) == 1

    def test_no_gemini_marks_tests_as_fail(self, tmp_path: Path) -> None:
        suite = _make_suite_doc([
            _make_video_test("ZZ-01", ["scatter"]),
        ])

        processor = VideoAiProcessor(
            game="zeus",
            suite_doc=suite,
            base_dir=tmp_path,
            gemini_video_helper=None,
        )

        record1 = _make_stub_record("ZZ-01", suite["tests"][0])
        record1.ai_pending_steps = [suite["tests"][0]["steps"][0]]

        asyncio.get_event_loop().run_until_complete(
            processor.notify_test_complete(record1)
        )
        asyncio.get_event_loop().run_until_complete(processor.wait_all())

        assert record1.ai_result == "fail"
        assert "GeminiVideoHelper" in (record1.error or "")

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
