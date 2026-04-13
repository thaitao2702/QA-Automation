"""
Tests for framework/runner/reporter.py

Acceptance criteria:
- Collects all result.json files from a results directory.
- Re-computes verdict from bridge_result + ai_result (not blindly trusts stored verdict).
- summary.json is written with correct counts and test list.
- report.html is generated with one row per test and correct verdict colors.
- replay.html is generated per test using the template with inlined data.
- Triple-validation verdict logic: both pass=green, one fail=orange, both fail=red.
- Pending AI result yields pending verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.runner.reporter import Reporter, _compute_verdict


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------


class TestComputeVerdict:
    def test_both_pass_is_green(self) -> None:
        assert _compute_verdict("pass", "pass") == "green"

    def test_bridge_pass_ai_skip_is_green(self) -> None:
        assert _compute_verdict("pass", "skip") == "green"

    def test_bridge_fail_ai_fail_is_red(self) -> None:
        assert _compute_verdict("fail", "fail") == "red"

    def test_bridge_pass_ai_fail_is_orange(self) -> None:
        assert _compute_verdict("pass", "fail") == "orange"

    def test_bridge_fail_ai_pass_is_orange(self) -> None:
        assert _compute_verdict("fail", "pass") == "orange"

    def test_ai_pending_is_pending(self) -> None:
        assert _compute_verdict("pass", "pending") == "pending"
        assert _compute_verdict("fail", "pending") == "pending"

    def test_both_skip_is_skip(self) -> None:
        assert _compute_verdict("skip", "skip") == "skip"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_test_result(
    parent: Path,
    test_id: str,
    bridge_result: str = "pass",
    ai_result: str = "pass",
    title: str = "Test title",
    duration_ms: int = 1000,
    error: str | None = None,
) -> Path:
    """Write a result.json and return the directory."""
    test_dir = parent / test_id
    test_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "test_id": test_id,
        "title": title,
        "bridge_result": bridge_result,
        "ai_result": ai_result,
        "final_verdict": _compute_verdict(bridge_result, ai_result),
        "duration_ms": duration_ms,
        "error": error,
        "timed_out": False,
    }
    (test_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")
    return test_dir


def _write_minimal_template(template_dir: Path) -> None:
    """Write a minimal replay.html template with the placeholder."""
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "replay.html").write_text(
        "<html><body><script>const DATA=%%REPLAY_DATA%%;</script></body></html>",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------


class TestResultCollection:
    def test_collects_result_files(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")
        _write_test_result(results_dir, "ZZ-02")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.total == 2
        ids = {e.test_id for e in summary.tests}
        assert "ZZ-01" in ids
        assert "ZZ-02" in ids

    def test_skips_setup_teardown_dirs(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")
        # Create pseudo-test dirs that should be ignored
        _write_test_result(results_dir, "_setup")
        _write_test_result(results_dir, "_teardown")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        ids = {e.test_id for e in summary.tests}
        assert "_setup" not in ids
        assert "_teardown" not in ids

    def test_empty_results_dir_produces_empty_summary(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.total == 0
        assert summary.counts["green"] == 0


# ---------------------------------------------------------------------------
# Verdict recomputation
# ---------------------------------------------------------------------------


class TestVerdictRecomputation:
    def test_verdict_recomputed_from_raw_results(self, tmp_path: Path) -> None:
        """Stored final_verdict should be overridden by fresh bridge+ai computation."""
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        test_dir = results_dir / "ZZ-01"
        test_dir.mkdir()
        # Store a wrong verdict intentionally
        (test_dir / "result.json").write_text(
            json.dumps({
                "test_id": "ZZ-01",
                "title": "Test",
                "bridge_result": "fail",
                "ai_result": "fail",
                "final_verdict": "green",  # WRONG — should be red
                "duration_ms": 500,
                "error": None,
                "timed_out": False,
            }),
            encoding="utf-8",
        )

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.tests[0].final_verdict == "red"

    def test_pending_ai_yields_pending_verdict(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01", bridge_result="pass", ai_result="pending")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.tests[0].final_verdict == "pending"
        assert summary.counts["pending"] == 1


# ---------------------------------------------------------------------------
# Verdict counts
# ---------------------------------------------------------------------------


class TestVerdictCounts:
    def test_counts_match_verdicts(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01", bridge_result="pass", ai_result="pass")
        _write_test_result(results_dir, "ZZ-02", bridge_result="pass", ai_result="fail")
        _write_test_result(results_dir, "ZZ-03", bridge_result="fail", ai_result="fail")
        _write_test_result(results_dir, "ZZ-04", bridge_result="pass", ai_result="pending")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.counts["green"] == 1
        assert summary.counts["orange"] == 1
        assert summary.counts["red"] == 1
        assert summary.counts["pending"] == 1


# ---------------------------------------------------------------------------
# summary.json
# ---------------------------------------------------------------------------


class TestSummaryJson:
    def test_summary_json_is_written(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.summary_path.exists()

    def test_summary_json_contains_tests_list(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        data = json.loads(summary.summary_path.read_text(encoding="utf-8"))
        assert "tests" in data
        assert len(data["tests"]) == 1
        assert data["tests"][0]["test_id"] == "ZZ-01"

    def test_summary_json_preserves_existing_costs(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")

        # Pre-write summary.json with cost data
        existing = {
            "costs": {
                "gemini_image_calls": 5,
                "estimated_cost_usd": 0.04,
            }
        }
        (results_dir / "summary.json").write_text(json.dumps(existing), encoding="utf-8")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        reporter.generate()

        data = json.loads((results_dir / "summary.json").read_text(encoding="utf-8"))
        assert data["costs"]["gemini_image_calls"] == 5


# ---------------------------------------------------------------------------
# report.html
# ---------------------------------------------------------------------------


class TestReportHtml:
    def test_report_html_is_written(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        assert summary.report_path.exists()
        assert summary.report_path.name == "report.html"

    def test_report_html_contains_test_id(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-07", title="Important test")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        html = summary.report_path.read_text(encoding="utf-8")
        assert "ZZ-07" in html
        assert "Important test" in html

    def test_report_html_shows_error_for_red_tests(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(
            results_dir,
            "ZZ-05",
            bridge_result="fail",
            ai_result="fail",
            error="getFreeSpinsRemaining returned 1, expected 3",
        )

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        html = summary.report_path.read_text(encoding="utf-8")
        assert "getFreeSpinsRemaining" in html

    def test_report_html_is_valid_html_structure(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        summary = reporter.generate()

        html = summary.report_path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html
        assert "<table" in html
        assert "</table>" in html


# ---------------------------------------------------------------------------
# replay.html
# ---------------------------------------------------------------------------


class TestReplayHtml:
    def test_replay_html_written_per_test(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        test_dir = _write_test_result(results_dir, "ZZ-01")

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        reporter.generate()

        assert (test_dir / "replay.html").exists()

    def test_replay_html_inlines_test_data(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        test_dir = _write_test_result(results_dir, "ZZ-42", title="My test")

        # Write events.jsonl
        (test_dir / "events.jsonl").write_text(
            '{"t": 1.5, "type": "spin_result", "data": {"payout": 10.0}}\n',
            encoding="utf-8",
        )
        # Write timeline.json
        (test_dir / "timeline.json").write_text(
            json.dumps({
                "video_start_time": 0.0,
                "event_collection_start_time": 0.5,
                "video_event_offset": 0.5,
                "steps": [{"id": "step-01", "action": "capture", "start_ms": 100, "end_ms": 300}],
            }),
            encoding="utf-8",
        )

        template_dir = tmp_path / "templates"
        _write_minimal_template(template_dir)

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        reporter.generate()

        replay_html = (test_dir / "replay.html").read_text(encoding="utf-8")
        # DATA JSON should contain test_id and events
        assert "ZZ-42" in replay_html
        assert "spin_result" in replay_html
        assert "step-01" in replay_html

    def test_replay_html_missing_template_skips_gracefully(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        _write_test_result(results_dir, "ZZ-01")

        # Template dir exists but has no replay.html
        template_dir = tmp_path / "templates"
        template_dir.mkdir()

        reporter = Reporter(results_dir=results_dir, template_dir=template_dir)
        # Should not raise — replay generation is best-effort
        summary = reporter.generate()
        assert summary.total == 1  # reporter still completed


# ---------------------------------------------------------------------------
# HTML escaping
# ---------------------------------------------------------------------------


class TestHtmlEscape:
    def test_escapes_angle_brackets(self) -> None:
        from framework.runner.reporter import _html_escape
        assert _html_escape("<script>") == "&lt;script&gt;"

    def test_escapes_ampersand(self) -> None:
        from framework.runner.reporter import _html_escape
        assert _html_escape("a & b") == "a &amp; b"

    def test_none_returns_empty_string(self) -> None:
        from framework.runner.reporter import _html_escape
        assert _html_escape(None) == ""
