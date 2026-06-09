"""
Tests for framework/helpers/artifact.py — test artifact management.

Acceptance criteria:
- Creating an artifact context for a test produces the correctly-named output directory.
- Appending events writes one JSON object per line, preserved even if process
  terminates mid-write.
- Timeline data structure includes video-event offset and per-step timestamps.
- Screenshot save uses sequential step numbering.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from framework.helpers.artifact import ArtifactContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    tmp_path: Path,
    test_id: str = "ZZ-01",
    run_dir: Path | None = None,
) -> ArtifactContext:
    """Create an ArtifactContext rooted at tmp_path."""
    if run_dir is None:
        run_dir = tmp_path / "test-results" / "zeus" / "dev" / "suite-01-2026-04-10-120000"
    return ArtifactContext(
        run_dir=run_dir,
        test_id=test_id,
    )


# ---------------------------------------------------------------------------
# Directory naming convention
# ---------------------------------------------------------------------------


class TestDirectoryNaming:
    def test_directory_path_matches_convention(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "test-results" / "zeus" / "dev" / "suite-01-2026-04-10-120000"
        ctx = _make_context(tmp_path, test_id="ZZ-01", run_dir=run_dir)
        expected = run_dir / "ZZ-01"
        assert ctx.test_dir == expected

    def test_directory_is_created_on_construction(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.test_dir.exists()
        assert ctx.test_dir.is_dir()

    def test_directory_includes_test_id(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="ZZ-42")
        assert ctx.test_dir.name == "ZZ-42"

    def test_different_test_ids_have_separate_directories(self, tmp_path: Path) -> None:
        ctx_a = _make_context(tmp_path, test_id="ZZ-01")
        ctx_b = _make_context(tmp_path, test_id="ZZ-02")
        assert ctx_a.test_dir != ctx_b.test_dir


# ---------------------------------------------------------------------------
# Screenshot saving
# ---------------------------------------------------------------------------


class TestScreenshotSaving:
    def test_first_screenshot_has_step_01_filename(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        fake_jpg = b"\xff\xd8\xff" + b"\x00" * 20  # minimal bytes
        path = ctx.save_screenshot(fake_jpg)
        assert path.name == "step-01.jpg"

    def test_second_screenshot_has_step_02_filename(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        fake_jpg = b"\xff\xd8\xff" + b"\x00" * 20
        ctx.save_screenshot(fake_jpg)
        path = ctx.save_screenshot(fake_jpg)
        assert path.name == "step-02.jpg"

    def test_screenshot_is_written_to_test_directory(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        fake_png = b"\x89PNG"
        path = ctx.save_screenshot(fake_png)
        assert path.parent == ctx.test_dir

    def test_screenshot_bytes_are_preserved(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\xAB\xCD" * 10
        path = ctx.save_screenshot(fake_png)
        assert path.read_bytes() == fake_png

    def test_screenshot_counter_is_independent_per_context(self, tmp_path: Path) -> None:
        ctx_a = _make_context(tmp_path, test_id="ZZ-01")
        ctx_b = _make_context(tmp_path, test_id="ZZ-02")
        fake_jpg = b"\xff\xd8\xff"
        ctx_a.save_screenshot(fake_jpg)
        ctx_a.save_screenshot(fake_jpg)
        path_b = ctx_b.save_screenshot(fake_jpg)
        # ctx_b starts from step-01 regardless of ctx_a's counter
        assert path_b.name == "step-01.jpg"


# ---------------------------------------------------------------------------
# Event log (append-safe JSONL)
# ---------------------------------------------------------------------------


class TestEventLog:
    def test_append_events_creates_events_file(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.append_events([{"t": 0.1, "type": "state_change", "data": {}}])
        assert ctx.events_path.exists()

    def test_single_event_written_as_json_line(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        event = {"t": 1.23, "type": "spin_result", "data": {"payout": 10.0}}
        ctx.append_events([event])
        lines = ctx.events_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["type"] == "spin_result"
        assert parsed["data"]["payout"] == 10.0

    def test_multiple_appends_produce_multiple_json_lines(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.append_events([{"t": 0.1, "type": "a", "data": {}}])
        ctx.append_events([{"t": 0.2, "type": "b", "data": {}}])
        ctx.append_events([{"t": 0.3, "type": "c", "data": {}}])
        lines = ctx.events_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        types = [json.loads(l)["type"] for l in lines]
        assert types == ["a", "b", "c"]

    def test_batch_append_writes_one_line_per_event(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        events = [
            {"t": 0.1, "type": "x", "data": {}},
            {"t": 0.2, "type": "y", "data": {}},
        ]
        ctx.append_events(events)
        lines = ctx.events_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_each_line_is_valid_independent_json(self, tmp_path: Path) -> None:
        """Each line must be independently parseable (crash-safe requirement)."""
        ctx = _make_context(tmp_path)
        for i in range(5):
            ctx.append_events([{"t": float(i) * 0.1, "type": f"ev{i}", "data": {"i": i}}])
        lines = ctx.events_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.strip():
                parsed = json.loads(line)
                assert "type" in parsed

    def test_events_path_accessor_returns_correct_path(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.events_path == ctx.test_dir / "events.jsonl"


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class TestTimeline:
    def test_write_timeline_creates_json_file(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.write_timeline(
            video_start_time=1711360800.123,
            event_collection_start_time=1711360800.456,
            steps=[
                {"id": "step-01", "action": "bridge_action", "start_ms": 100, "end_ms": 500},
            ],
        )
        assert ctx.timeline_path.exists()

    def test_timeline_contains_video_start_time(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.write_timeline(
            video_start_time=1711360800.123,
            event_collection_start_time=1711360800.456,
            steps=[],
        )
        data = json.loads(ctx.timeline_path.read_text(encoding="utf-8"))
        assert data["video_start_time"] == pytest.approx(1711360800.123)

    def test_timeline_contains_video_event_offset(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.write_timeline(
            video_start_time=1711360800.0,
            event_collection_start_time=1711360800.333,
            steps=[],
        )
        data = json.loads(ctx.timeline_path.read_text(encoding="utf-8"))
        assert "video_event_offset" in data
        # Offset = event_collection_start - video_start
        assert data["video_event_offset"] == pytest.approx(0.333, abs=1e-3)

    def test_timeline_contains_steps_list(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        steps = [
            {"id": "step-01", "action": "bridge_action", "start_ms": 100, "end_ms": 500},
            {"id": "step-02", "action": "wait_for_state", "start_ms": 500, "end_ms": 2000},
        ]
        ctx.write_timeline(
            video_start_time=1711360800.0,
            event_collection_start_time=1711360800.0,
            steps=steps,
        )
        data = json.loads(ctx.timeline_path.read_text(encoding="utf-8"))
        assert len(data["steps"]) == 2
        assert data["steps"][0]["id"] == "step-01"

    def test_timeline_path_accessor(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.timeline_path == ctx.test_dir / "timeline.json"


# ---------------------------------------------------------------------------
# Result file
# ---------------------------------------------------------------------------


class TestResultFile:
    def test_write_result_creates_json_file(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.write_result(
            bridge_result="pass",
            ai_result="pass",
            final_verdict="green",
        )
        assert ctx.result_path.exists()

    def test_result_contains_verdict_fields(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.write_result(
            bridge_result="pass",
            ai_result="fail",
            final_verdict="orange",
        )
        data = json.loads(ctx.result_path.read_text(encoding="utf-8"))
        assert data["bridge_result"] == "pass"
        assert data["ai_result"] == "fail"
        assert data["final_verdict"] == "orange"

    def test_result_path_accessor(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.result_path == ctx.test_dir / "result.json"

    def test_write_result_with_extra_data(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        ctx.write_result(
            bridge_result="pass",
            ai_result="pending",
            final_verdict="pending",
            extra={"error": None, "duration_ms": 1234},
        )
        data = json.loads(ctx.result_path.read_text(encoding="utf-8"))
        assert data["error"] is None
        assert data["duration_ms"] == 1234


# ---------------------------------------------------------------------------
# Path accessors
# ---------------------------------------------------------------------------


class TestPathAccessors:
    def test_video_path_accessor(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.video_path == ctx.test_dir / "session.webm"

    def test_replay_path_accessor(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.replay_path == ctx.test_dir / "replay.html"

    def test_screenshot_path_for_step(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.screenshot_path(3) == ctx.test_dir / "step-03.jpg"

    def test_ai_result_path_for_step(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path)
        assert ctx.ai_result_path(2) == ctx.test_dir / "step-02-ai.json"


# ---------------------------------------------------------------------------
# Per-case report (write_case_report) + result.html replay links
# ---------------------------------------------------------------------------


class TestWriteCaseReport:
    def _case(self, **over: object) -> dict:
        case = {
            "id": "ZZ-30",
            "title": "Trigger FS with 3 coins",
            "bridge_result": "pass",
            "ai_result": "pass",
            "final_verdict": "green",
            "error": None,
            "step_ids": ["step-02", "step-07"],
        }
        case.update(over)
        return case

    def test_writes_case_file_named_by_id(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="run-1")
        out = ctx.write_case_report(self._case())
        assert out == ctx.test_dir / "case-ZZ-30.html"
        assert out.exists()

    def test_sanitises_unsafe_id_in_filename(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="run-1")
        out = ctx.write_case_report(self._case(id="ZZ/30 x"))
        assert out.name == "case-ZZ_30_x.html"

    def test_report_links_to_replay_and_video(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="run-1")
        html = ctx.write_case_report(self._case()).read_text(encoding="utf-8")
        assert 'href="replay.html"' in html
        assert 'href="session.webm"' in html

    def test_report_shows_verdict_and_steps(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="run-1")
        html = ctx.write_case_report(self._case()).read_text(encoding="utf-8")
        assert ">Pass<" in html          # verdict label for green
        assert "ZZ-30" in html
        assert "step-02" in html and "step-07" in html

    def test_report_renders_error_banner(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="run-1")
        html = ctx.write_case_report(
            self._case(final_verdict="red", bridge_result="fail", error="boom <x>")
        ).read_text(encoding="utf-8")
        assert ">Fail<" in html
        assert "boom &lt;x&gt;" in html  # html-escaped


class TestResultHtmlReplayLink:
    def test_result_html_links_to_replay(self, tmp_path: Path) -> None:
        ctx = _make_context(tmp_path, test_id="ZZ-01")
        ctx.write_result(
            "pass", "pass", "green",
            extra={"test_id": "ZZ-01", "title": "t", "duration_ms": 100},
        )
        html = (ctx.test_dir / "result.html").read_text(encoding="utf-8")
        assert 'href="replay.html"' in html
        assert 'href="session.webm"' in html
