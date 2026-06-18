"""
Tests for framework/runner/suite_runner.py

Acceptance criteria:
- TestRecord initialises with correct defaults (skip verdicts, no timeout, no error).
- _resolve_game_url reads env.config.yml, returns static URL with browser_params appended.
- _resolve_game_url raises FileNotFoundError when config is missing.
- _resolve_game_url raises KeyError when the requested environment is absent.
- _resolve_game_url falls back to url_script when no static url is present.
- _compute_verdict follows the triple-validation truth table exactly.
- _check_stop_conditions returns None when no thresholds are breached.
- _check_stop_conditions fires fail_fast on the first red verdict.
- _check_stop_conditions fires max_total_failures when threshold is met.
- _check_stop_conditions fires max_consecutive_failures from the tail.
- _run_steps returns (True, None, []) when all steps pass.
- _run_steps returns (False, error, []) on step failure.
- _run_steps collects ai_pending video steps without blocking.
- _run_steps stops on UnsupportedActionError.
- SuiteRunner._find_suite_file locates .yml and .yaml variants.
- SuiteRunner._find_suite_file raises FileNotFoundError for missing suites.
- SuiteRunner._load_suite raises ValueError on schema validation failure.
- SuiteRunner._results_dir returns the correct date-stamped path.
- SuiteRunner._build_summary produces correct verdict counts and cost breakdown.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from framework.runner.suite_runner import (
    CaseVerdict,
    RunRecord,
    SuiteRunner,
    TestRecord,
    _attribute_step_to_cases,
    _check_stop_conditions,
    _compute_verdict,
    _finalize_case_verdicts,
    _resolve_game_url,
    _run_steps,
    _step_case_ids,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_env_config(base_dir: Path, game: str, environments: dict) -> Path:
    """Write an env.config.yml under games/{game}/."""
    game_dir = base_dir / "games" / game
    game_dir.mkdir(parents=True, exist_ok=True)
    config_path = game_dir / "env.config.yml"
    config_path.write_text(
        yaml.dump({"environments": environments}),
        encoding="utf-8",
    )
    return config_path


def _write_suite_file(
    base_dir: Path,
    game: str,
    suite_name: str,
    doc: dict | None = None,
    ext: str = ".yml",
) -> Path:
    """Write a suite YAML file and return its path."""
    suite_dir = base_dir / "games" / game / "test-suites"
    suite_dir.mkdir(parents=True, exist_ok=True)
    suite_path = suite_dir / f"{suite_name}{ext}"
    if doc is None:
        doc = _minimal_valid_suite()
    suite_path.write_text(yaml.dump(doc), encoding="utf-8")
    return suite_path


def _minimal_valid_suite() -> dict:
    """Smallest suite document that passes schema validation."""
    return {
        "suite": "Test Suite",
        "game": "zeus",
        "version": "1.0",
        "config": {
            "detection_method": "both",
            "video": True,
            "test_timeout_ms": 60000,
            "suite_timeout_ms": 600000,
            "stop_conditions": {
                "fail_fast": False,
                "max_consecutive_failures": 3,
                "max_total_failures": 10,
            },
        },
        "tests": [
            {
                "id": "ZZ-01",
                "title": "Test one",
                "steps": [
                    {"id": "step-01", "action": "capture"},
                ],
            }
        ],
    }


def _make_record(
    test_id: str = "ZZ-01",
    verdict: str = "green",
    bridge: str = "pass",
    ai: str = "pass",
) -> TestRecord:
    """Create a TestRecord with preset verdict fields."""
    doc = {"id": test_id, "title": f"Test {test_id}", "steps": []}
    rec = TestRecord(doc, 0)
    rec.bridge_result = bridge
    rec.ai_result = ai
    rec.final_verdict = verdict
    return rec


def _make_mock_executor(
    results: list[MagicMock] | None = None,
) -> tuple[MagicMock, list]:
    """Create a mock StepExecutor with canned step results."""
    from framework.runner.step_executor import StepResult

    executor = MagicMock()
    executor.timeline = []

    if results is None:
        # Default: one passing step
        results = [
            StepResult(
                success=True,
                error=None,
                screenshot_path=None,
                bridge_assert_result=None,
                ai_result=None,
                ai_pending=False,
            )
        ]

    call_count = {"i": 0}

    async def _execute(step):
        idx = call_count["i"]
        call_count["i"] += 1
        return results[idx]

    executor.execute = AsyncMock(side_effect=_execute)
    return executor, results


# ---------------------------------------------------------------------------
# TestRecord — initialisation
# ---------------------------------------------------------------------------


class TestTestRecord:
    def test_defaults_to_skip_verdicts(self) -> None:
        doc = {"id": "ZZ-99", "title": "A test", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.bridge_result == "skip"
        assert rec.ai_result == "skip"

    def test_defaults_to_pending_final_verdict(self) -> None:
        doc = {"id": "ZZ-99", "title": "A test", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.final_verdict == "pending"

    def test_no_timeout_by_default(self) -> None:
        doc = {"id": "ZZ-99", "title": "A test", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.timed_out is False

    def test_no_error_by_default(self) -> None:
        doc = {"id": "ZZ-99", "title": "A test", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.error is None

    def test_test_id_from_doc(self) -> None:
        doc = {"id": "ZZ-42", "title": "Custom", "steps": []}
        rec = TestRecord(doc, 5)
        assert rec.test_id == "ZZ-42"

    def test_test_id_fallback_when_missing(self) -> None:
        doc = {"title": "No ID", "steps": []}
        rec = TestRecord(doc, 3)
        assert rec.test_id == "test-3"

    def test_ai_pending_steps_empty_by_default(self) -> None:
        doc = {"id": "ZZ-01", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.ai_pending_steps == []

    def test_artifact_none_by_default(self) -> None:
        doc = {"id": "ZZ-01", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.artifact is None

    def test_duration_ms_zero_by_default(self) -> None:
        doc = {"id": "ZZ-01", "steps": []}
        rec = TestRecord(doc, 0)
        assert rec.duration_ms == 0


# ---------------------------------------------------------------------------
# _resolve_game_url — static URL
# ---------------------------------------------------------------------------


class TestResolveGameUrlStatic:
    def test_returns_static_url(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "https://dev.example.com/game", "browser_params": ""},
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert url == "https://dev.example.com/game"

    def test_appends_browser_params(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "https://dev.example.com/game", "browser_params": "?bridge=1&debug=1"},
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert "bridge=1" in url
        assert "debug=1" in url

    def test_uses_ampersand_when_url_has_query(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "https://dev.example.com/game?token=abc", "browser_params": "?bridge=1"},
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert "?token=abc&bridge=1" in url

    def test_uses_question_mark_when_url_has_no_query(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "https://dev.example.com/game", "browser_params": "?bridge=1"},
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert "game?bridge=1" in url

    def test_strips_whitespace_from_url(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "  https://dev.example.com/game  ", "browser_params": ""},
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert not url.startswith(" ")
        assert not url.endswith(" ")


# ---------------------------------------------------------------------------
# _resolve_game_url — error cases
# ---------------------------------------------------------------------------


class TestResolveGameUrlErrors:
    def test_missing_config_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="env.config.yml"):
            _resolve_game_url("nonexistent", "dev", tmp_path)

    def test_unknown_env_raises_key_error(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "https://dev.example.com/", "browser_params": ""},
        })
        with pytest.raises(KeyError, match="staging"):
            _resolve_game_url("zeus", "staging", tmp_path)

    def test_unknown_env_lists_available(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"url": "https://dev.example.com/", "browser_params": ""},
            "prod": {"url": "https://prod.example.com/", "browser_params": ""},
        })
        with pytest.raises(KeyError) as exc_info:
            _resolve_game_url("zeus", "staging", tmp_path)
        msg = str(exc_info.value)
        assert "dev" in msg
        assert "prod" in msg

    def test_no_url_or_url_script_raises_value_error(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {"browser_params": "?bridge=1"},
        })
        with pytest.raises(ValueError, match="neither"):
            _resolve_game_url("zeus", "dev", tmp_path)


# ---------------------------------------------------------------------------
# _resolve_game_url — url_script
# ---------------------------------------------------------------------------


class TestResolveGameUrlScript:
    def test_url_script_returns_stdout(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {
                "url_script": f'{sys.executable} -c "print(\'https://resolved.example.com\')"',
                "browser_params": "",
            },
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert url == "https://resolved.example.com"

    def test_url_script_appends_browser_params(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {
                "url_script": f'{sys.executable} -c "print(\'https://resolved.example.com\')"',
                "browser_params": "?bridge=1",
            },
        })
        url = _resolve_game_url("zeus", "dev", tmp_path)
        assert "bridge=1" in url

    def test_url_script_failure_raises_value_error(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {
                "url_script": f'{sys.executable} -c "import sys; sys.exit(1)"',
                "browser_params": "",
            },
        })
        with pytest.raises(ValueError, match="failed"):
            _resolve_game_url("zeus", "dev", tmp_path)

    def test_url_script_empty_output_raises_value_error(self, tmp_path: Path) -> None:
        _write_env_config(tmp_path, "zeus", {
            "dev": {
                "url_script": f'{sys.executable} -c "pass"',
                "browser_params": "",
            },
        })
        with pytest.raises(ValueError, match="no output"):
            _resolve_game_url("zeus", "dev", tmp_path)


# ---------------------------------------------------------------------------
# _compute_verdict — triple-validation truth table
# ---------------------------------------------------------------------------


class TestComputeVerdict:
    def test_both_pass_is_green(self) -> None:
        assert _compute_verdict("pass", "pass") == "green"

    def test_bridge_pass_ai_skip_is_green(self) -> None:
        assert _compute_verdict("pass", "skip") == "green"

    def test_both_fail_is_red(self) -> None:
        assert _compute_verdict("fail", "fail") == "red"

    def test_bridge_pass_ai_fail_is_orange(self) -> None:
        assert _compute_verdict("pass", "fail") == "orange"

    def test_bridge_fail_ai_pass_is_orange(self) -> None:
        assert _compute_verdict("fail", "pass") == "orange"

    def test_ai_pending_is_pending_regardless_of_bridge(self) -> None:
        assert _compute_verdict("pass", "pending") == "pending"
        assert _compute_verdict("fail", "pending") == "pending"

    def test_both_skip_is_skip(self) -> None:
        assert _compute_verdict("skip", "skip") == "skip"

    def test_bridge_skip_ai_pass_is_green(self) -> None:
        # skip bridge + pass AI should be green (no bridge assertion, AI passed)
        result = _compute_verdict("skip", "pass")
        # Based on the source: bridge=skip,ai=pass → falls through to green
        assert result == "green"

    def test_bridge_fail_ai_skip_is_orange(self) -> None:
        result = _compute_verdict("fail", "skip")
        # bridge=fail, ai=skip → one fails → orange
        assert result == "orange"


# ---------------------------------------------------------------------------
# _check_stop_conditions — no breach
# ---------------------------------------------------------------------------


class TestCheckStopConditionsNoBreach:
    def test_empty_conditions_returns_none(self) -> None:
        result = _check_stop_conditions({}, [], "green")
        assert result is None

    def test_all_green_returns_none(self) -> None:
        records = [_make_record(verdict="green") for _ in range(5)]
        conditions = {"fail_fast": False, "max_total_failures": 10, "max_consecutive_failures": 3}
        result = _check_stop_conditions(conditions, records, "green")
        assert result is None

    def test_orange_does_not_trigger_fail_fast(self) -> None:
        records = [_make_record(verdict="orange")]
        conditions = {"fail_fast": True}
        result = _check_stop_conditions(conditions, records, "orange")
        assert result is None

    def test_pending_does_not_trigger_fail_fast(self) -> None:
        records = [_make_record(verdict="pending")]
        conditions = {"fail_fast": True}
        result = _check_stop_conditions(conditions, records, "pending")
        assert result is None


# ---------------------------------------------------------------------------
# _check_stop_conditions — fail_fast
# ---------------------------------------------------------------------------


class TestCheckStopConditionsFailFast:
    def test_fail_fast_on_first_red(self) -> None:
        records = [_make_record(verdict="red")]
        conditions = {"fail_fast": True}
        result = _check_stop_conditions(conditions, records, "red")
        assert result is not None
        assert "fail_fast" in result

    def test_fail_fast_false_does_not_stop(self) -> None:
        records = [_make_record(verdict="red")]
        conditions = {"fail_fast": False}
        result = _check_stop_conditions(conditions, records, "red")
        assert result is None


# ---------------------------------------------------------------------------
# _check_stop_conditions — max_total_failures
# ---------------------------------------------------------------------------


class TestCheckStopConditionsMaxTotal:
    def test_stops_at_threshold(self) -> None:
        records = [
            _make_record("ZZ-01", verdict="red"),
            _make_record("ZZ-02", verdict="green"),
            _make_record("ZZ-03", verdict="red"),
        ]
        conditions = {"max_total_failures": 2}
        result = _check_stop_conditions(conditions, records, "red")
        assert result is not None
        assert "max_total_failures" in result

    def test_does_not_stop_below_threshold(self) -> None:
        records = [
            _make_record("ZZ-01", verdict="red"),
            _make_record("ZZ-02", verdict="green"),
        ]
        conditions = {"max_total_failures": 5}
        result = _check_stop_conditions(conditions, records, "green")
        assert result is None

    def test_orange_not_counted_as_failure(self) -> None:
        records = [
            _make_record("ZZ-01", verdict="orange"),
            _make_record("ZZ-02", verdict="orange"),
            _make_record("ZZ-03", verdict="orange"),
        ]
        conditions = {"max_total_failures": 2}
        result = _check_stop_conditions(conditions, records, "orange")
        assert result is None


# ---------------------------------------------------------------------------
# _check_stop_conditions — max_consecutive_failures
# ---------------------------------------------------------------------------


class TestCheckStopConditionsMaxConsecutive:
    def test_three_consecutive_reds_triggers(self) -> None:
        records = [
            _make_record("ZZ-01", verdict="green"),
            _make_record("ZZ-02", verdict="red"),
            _make_record("ZZ-03", verdict="red"),
            _make_record("ZZ-04", verdict="red"),
        ]
        conditions = {"max_consecutive_failures": 3}
        result = _check_stop_conditions(conditions, records, "red")
        assert result is not None
        assert "max_consecutive_failures" in result

    def test_interrupted_streak_does_not_trigger(self) -> None:
        records = [
            _make_record("ZZ-01", verdict="red"),
            _make_record("ZZ-02", verdict="red"),
            _make_record("ZZ-03", verdict="green"),
            _make_record("ZZ-04", verdict="red"),
        ]
        conditions = {"max_consecutive_failures": 3}
        result = _check_stop_conditions(conditions, records, "red")
        assert result is None

    def test_consecutive_counts_from_tail(self) -> None:
        """Only the trailing streak matters."""
        records = [
            _make_record("ZZ-01", verdict="red"),
            _make_record("ZZ-02", verdict="red"),
            _make_record("ZZ-03", verdict="red"),
            _make_record("ZZ-04", verdict="green"),  # breaks the streak
            _make_record("ZZ-05", verdict="red"),
        ]
        conditions = {"max_consecutive_failures": 3}
        result = _check_stop_conditions(conditions, records, "red")
        assert result is None  # only 1 consecutive from tail


# ---------------------------------------------------------------------------
# _run_steps — all pass
# ---------------------------------------------------------------------------


class TestRunStepsAllPass:
    def test_returns_true_on_all_passing(self, tmp_path: Path) -> None:
        from framework.runner.step_executor import StepResult

        executor, _ = _make_mock_executor([
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
        ])
        steps = [{"id": "step-01", "action": "capture"}, {"id": "step-02", "action": "capture"}]
        ok, err, pending = asyncio.get_event_loop().run_until_complete(
            _run_steps(executor, steps, "test")
        )
        assert ok is True
        assert err is None
        assert pending == []

    def test_executes_all_steps(self, tmp_path: Path) -> None:
        from framework.runner.step_executor import StepResult

        executor, _ = _make_mock_executor([
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
        ])
        steps = [{"id": "step-01", "action": "capture"}, {"id": "step-02", "action": "capture"}]
        asyncio.get_event_loop().run_until_complete(_run_steps(executor, steps, "test"))
        assert executor.execute.await_count == 2


# ---------------------------------------------------------------------------
# _run_steps — failure
# ---------------------------------------------------------------------------


class TestRunStepsFailure:
    def test_returns_false_on_step_failure(self) -> None:
        from framework.runner.step_executor import StepResult

        executor, _ = _make_mock_executor([
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
            StepResult(success=False, error="assertion mismatch", screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
        ])
        steps = [{"id": "step-01", "action": "capture"}, {"id": "step-02", "action": "bridge_assert"}]
        ok, err, _ = asyncio.get_event_loop().run_until_complete(
            _run_steps(executor, steps, "test")
        )
        assert ok is False
        assert err == "assertion mismatch"

    def test_continues_after_step_failure(self) -> None:
        """Steps continue after a non-fatal failure (teardown still runs)."""
        from framework.runner.step_executor import StepResult

        executor, _ = _make_mock_executor([
            StepResult(success=False, error="fail1", screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
        ])
        steps = [{"id": "step-01"}, {"id": "step-02"}]
        asyncio.get_event_loop().run_until_complete(_run_steps(executor, steps, "test"))
        # Both steps should have been executed
        assert executor.execute.await_count == 2

    def test_first_error_captured(self) -> None:
        from framework.runner.step_executor import StepResult

        executor, _ = _make_mock_executor([
            StepResult(success=False, error="first error", screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
            StepResult(success=False, error="second error", screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=False),
        ])
        steps = [{"id": "step-01"}, {"id": "step-02"}]
        _, err, _ = asyncio.get_event_loop().run_until_complete(
            _run_steps(executor, steps, "test")
        )
        assert err == "first error"


# ---------------------------------------------------------------------------
# _run_steps — video AI pending
# ---------------------------------------------------------------------------


class TestRunStepsVideoPending:
    def test_collects_pending_video_steps(self) -> None:
        from framework.runner.step_executor import StepResult

        video_step = {"id": "step-01", "assert": {"ai": {"mode": "video"}}}
        executor, _ = _make_mock_executor([
            StepResult(success=True, error=None, screenshot_path=None,
                       bridge_assert_result=None, ai_result=None, ai_pending=True),
        ])
        steps = [video_step]
        ok, err, pending = asyncio.get_event_loop().run_until_complete(
            _run_steps(executor, steps, "test")
        )
        assert ok is True
        assert len(pending) == 1
        assert pending[0] is video_step


# ---------------------------------------------------------------------------
# _run_steps — UnsupportedActionError
# ---------------------------------------------------------------------------


class TestRunStepsUnsupportedAction:
    def test_stops_on_unsupported_action(self) -> None:
        from framework.runner.step_executor import UnsupportedActionError, StepResult

        call_count = {"i": 0}

        async def _execute(step):
            call_count["i"] += 1
            if call_count["i"] == 1:
                raise UnsupportedActionError("fly_to_moon")
            return StepResult(success=True, error=None, screenshot_path=None,
                              bridge_assert_result=None, ai_result=None, ai_pending=False)

        executor = MagicMock()
        executor.timeline = []
        executor.execute = AsyncMock(side_effect=_execute)

        steps = [{"id": "step-01", "action": "fly_to_moon"}, {"id": "step-02", "action": "capture"}]
        ok, err, _ = asyncio.get_event_loop().run_until_complete(
            _run_steps(executor, steps, "test")
        )
        assert ok is False
        assert "fly_to_moon" in (err or "")
        # Second step should NOT have been called
        assert executor.execute.await_count == 1


# ---------------------------------------------------------------------------
# SuiteRunner._find_suite_file
# ---------------------------------------------------------------------------


class TestFindSuiteFile:
    def test_finds_yml_file(self, tmp_path: Path) -> None:
        _write_suite_file(tmp_path, "zeus", "suite-01", ext=".yml")
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        path = runner._find_suite_file()
        assert path.exists()
        assert path.name == "suite-01.yml"

    def test_finds_yaml_file(self, tmp_path: Path) -> None:
        _write_suite_file(tmp_path, "zeus", "suite-02", ext=".yaml")
        runner = SuiteRunner("zeus", "suite-02", "dev", base_dir=tmp_path)
        path = runner._find_suite_file()
        assert path.exists()
        assert path.name == "suite-02.yaml"

    def test_raises_for_missing_suite(self, tmp_path: Path) -> None:
        # Create the directory but not the file
        (tmp_path / "games" / "zeus" / "test-suites").mkdir(parents=True)
        runner = SuiteRunner("zeus", "missing-suite", "dev", base_dir=tmp_path)
        with pytest.raises(FileNotFoundError, match="missing-suite"):
            runner._find_suite_file()

    def test_raises_for_missing_game_dir(self, tmp_path: Path) -> None:
        runner = SuiteRunner("nonexistent", "suite-01", "dev", base_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            runner._find_suite_file()


# ---------------------------------------------------------------------------
# SuiteRunner._load_suite
# ---------------------------------------------------------------------------


class TestLoadSuite:
    def test_loads_valid_suite(self, tmp_path: Path) -> None:
        path = _write_suite_file(tmp_path, "zeus", "suite-01")
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        doc = runner._load_suite(path)
        assert doc["suite"] == "Test Suite"
        assert doc["game"] == "zeus"

    def test_raises_on_invalid_suite(self, tmp_path: Path) -> None:
        # Write an invalid suite (missing required fields)
        invalid_doc = {"suite": "Bad Suite"}  # missing game, version, config, tests
        path = _write_suite_file(tmp_path, "zeus", "bad-suite", doc=invalid_doc)
        runner = SuiteRunner("zeus", "bad-suite", "dev", base_dir=tmp_path)
        with pytest.raises(ValueError, match="validation failed"):
            runner._load_suite(path)

    def test_error_message_includes_count(self, tmp_path: Path) -> None:
        invalid_doc = {"not_a_suite": True}
        path = _write_suite_file(tmp_path, "zeus", "bad", doc=invalid_doc)
        runner = SuiteRunner("zeus", "bad", "dev", base_dir=tmp_path)
        with pytest.raises(ValueError, match=r"\d+ error"):
            runner._load_suite(path)


# ---------------------------------------------------------------------------
# SuiteRunner._results_dir
# ---------------------------------------------------------------------------


class TestResultsDir:
    def test_path_contains_date_and_suite(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01-core", "dev", base_dir=tmp_path)
        result_dir = runner._results_dir(date(2026, 4, 12))
        assert "2026-04-12" in str(result_dir)
        assert "suite-01-core" in str(result_dir)

    def test_path_contains_game_and_env(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "staging", base_dir=tmp_path)
        result_dir = runner._results_dir(date(2026, 4, 12))
        assert "zeus" in str(result_dir)
        assert "staging" in str(result_dir)

    def test_path_under_test_results(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        result_dir = runner._results_dir(date(2026, 4, 12))
        assert "test-results" in str(result_dir)


# ---------------------------------------------------------------------------
# SuiteRunner._build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def _make_runner_with_records(
        self, tmp_path: Path, records: list[TestRecord]
    ) -> SuiteRunner:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        runner._suite_doc = _minimal_valid_suite()
        runner._records = records
        return runner

    def test_counts_match_records(self, tmp_path: Path) -> None:
        records = [
            _make_record("ZZ-01", verdict="green"),
            _make_record("ZZ-02", verdict="green"),
            _make_record("ZZ-03", verdict="orange"),
            _make_record("ZZ-04", verdict="red"),
            _make_record("ZZ-05", verdict="pending"),
        ]
        runner = self._make_runner_with_records(tmp_path, records)

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason=None,
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["counts"]["green"] == 2
        assert summary["counts"]["orange"] == 1
        assert summary["counts"]["red"] == 1
        assert summary["counts"]["pending"] == 1
        assert summary["counts"]["skip"] == 0
        assert summary["total"] == 5

    def test_summary_contains_suite_metadata(self, tmp_path: Path) -> None:
        runner = self._make_runner_with_records(tmp_path, [])

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason=None,
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["suite"] == "Test Suite"
        assert summary["game"] == "zeus"
        assert summary["env"] == "dev"
        assert summary["run_date"] == "2026-04-12"

    def test_stop_reason_included(self, tmp_path: Path) -> None:
        runner = self._make_runner_with_records(tmp_path, [])

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason="fail_fast: stopped on first red verdict",
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["stop_reason"] == "fail_fast: stopped on first red verdict"

    def test_null_stop_reason_when_no_stop(self, tmp_path: Path) -> None:
        runner = self._make_runner_with_records(tmp_path, [])

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason=None,
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["stop_reason"] is None

    def test_costs_zero_without_gemini(self, tmp_path: Path) -> None:
        runner = self._make_runner_with_records(tmp_path, [])

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason=None,
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["costs"]["gemini_image_calls"] == 0
        assert summary["costs"]["gemini_video_calls"] == 0
        assert summary["costs"]["estimated_cost_usd"] == 0.0

    def test_costs_from_gemini_image(self, tmp_path: Path) -> None:
        runner = self._make_runner_with_records(tmp_path, [])

        mock_gemini_image = MagicMock()
        mock_gemini_image.cost_summary.return_value = {
            "gemini_image_calls": 5,
            "gemini_input_tokens": 25000,
            "gemini_output_tokens": 1000,
            "estimated_cost_usd": 0.05,
        }

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason=None,
            gemini_image=mock_gemini_image,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["costs"]["gemini_image_calls"] == 5
        assert summary["costs"]["estimated_cost_usd"] == 0.05

    def test_tests_list_in_summary(self, tmp_path: Path) -> None:
        rec = _make_record("ZZ-42", verdict="green")
        rec.duration_ms = 1500
        rec.error = None
        rec.timed_out = False
        runner = self._make_runner_with_records(tmp_path, [rec])

        import time
        summary = runner._build_summary(
            suite_start=time.time(),
            stop_reason=None,
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert len(summary["tests"]) == 1
        t = summary["tests"][0]
        assert t["test_id"] == "ZZ-42"
        assert t["final_verdict"] == "green"
        assert t["bridge_result"] == "pass"
        assert t["duration_ms"] == 1500
        assert t["timed_out"] is False

    def test_duration_ms_is_positive(self, tmp_path: Path) -> None:
        runner = self._make_runner_with_records(tmp_path, [])

        import time
        start = time.time() - 1.0  # started 1 second ago
        summary = runner._build_summary(
            suite_start=start,
            stop_reason=None,
            gemini_image=None,
            gemini_video=None,
            run_date=date(2026, 4, 12),
        )

        assert summary["duration_ms"] > 0


# ---------------------------------------------------------------------------
# SuiteRunner construction
# ---------------------------------------------------------------------------


class TestSuiteRunnerConstruction:
    def test_stores_game_suite_env(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        assert runner.game == "zeus"
        assert runner.suite_name == "suite-01"
        assert runner.env == "dev"
        assert runner.base_dir == tmp_path

    def test_api_key_from_param(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path, gemini_api_key="test-key")
        assert runner._api_key == "test-key"

    def test_api_key_from_env_var(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"GEMINI_API_KEY": "env-key"}):
            runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        assert runner._api_key == "env-key"

    def test_api_key_empty_when_missing(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {}, clear=True):
            runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        assert runner._api_key == ""

    def test_records_empty_initially(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        assert runner._records == []


# ---------------------------------------------------------------------------
# runs: — one execution, many case verdicts
# ---------------------------------------------------------------------------


def _result(success: bool = True, ai_pending: bool = False, error: str | None = None):
    from framework.runner.step_executor import StepResult

    return StepResult(success=success, error=error, ai_pending=ai_pending)


def _run_doc(cases: list[tuple[str, str]]) -> dict:
    return {
        "id": "run-01",
        "title": "demo run",
        "cases": [{"id": cid, "title": title} for cid, title in cases],
        "steps": [],
    }


class TestStepCaseIds:
    def test_string_tag(self) -> None:
        assert _step_case_ids({"id": "s", "case": "ZZ-30"}) == ["ZZ-30"]

    def test_list_tag(self) -> None:
        assert _step_case_ids({"id": "s", "case": ["ZZ-32", "ZZ-33"]}) == ["ZZ-32", "ZZ-33"]

    def test_expected_by_case_keys(self) -> None:
        step = {"id": "s", "assert": {"ai": {"expected_by_case": {"ZZ-30": {}, "ZZ-35": {}}}}}
        assert set(_step_case_ids(step)) == {"ZZ-30", "ZZ-35"}

    def test_untagged_step(self) -> None:
        assert _step_case_ids({"id": "s", "action": "bridge_action"}) == []


class TestCaseVerdictAggregation:
    def test_bridge_pass_is_green(self) -> None:
        rec = RunRecord(_run_doc([("ZZ-30", "t")]), 0)
        _attribute_step_to_cases(rec, {"id": "s1", "case": "ZZ-30"}, _result(success=True))
        _finalize_case_verdicts(rec, None)
        cv = rec.cases["ZZ-30"]
        assert cv.bridge_result == "pass"
        assert cv.ai_result == "skip"
        assert cv.final_verdict == "green"

    def test_bridge_fail_only_is_orange(self) -> None:
        # bridge fail + ai skip -> orange (suspicious); red requires both to fail,
        # matching the framework's existing _compute_verdict semantics.
        rec = RunRecord(_run_doc([("ZZ-30", "t")]), 0)
        _attribute_step_to_cases(rec, {"id": "s1", "case": "ZZ-30"}, _result(success=False, error="mismatch"))
        _finalize_case_verdicts(rec, None)
        cv = rec.cases["ZZ-30"]
        assert cv.bridge_result == "fail"
        assert cv.final_verdict == "orange"
        assert cv.error == "mismatch"

    def test_shared_assert_feeds_two_cases(self) -> None:
        rec = RunRecord(_run_doc([("ZZ-32", "t"), ("ZZ-33", "t")]), 0)
        _attribute_step_to_cases(rec, {"id": "s1", "case": ["ZZ-32", "ZZ-33"]}, _result(success=True))
        _finalize_case_verdicts(rec, None)
        assert rec.cases["ZZ-32"].final_verdict == "green"
        assert rec.cases["ZZ-33"].final_verdict == "green"
        assert rec.cases["ZZ-32"].step_ids == ["s1"]
        assert rec.cases["ZZ-33"].step_ids == ["s1"]

    def test_ai_step_marks_pending(self) -> None:
        rec = RunRecord(_run_doc([("ZZ-35", "t")]), 0)
        step = {"id": "s1", "case": "ZZ-35", "assert": {"ai": {"mode": "video"}}}
        _attribute_step_to_cases(rec, step, _result(success=True, ai_pending=True))
        _finalize_case_verdicts(rec, None)
        cv = rec.cases["ZZ-35"]
        assert cv.ai_result == "pending"
        assert cv.final_verdict == "pending"
        assert cv.ai_pending_steps == [step]

    def test_case_with_no_assert_is_skip(self) -> None:
        rec = RunRecord(_run_doc([("ZZ-30", "t")]), 0)
        _finalize_case_verdicts(rec, None)
        cv = rec.cases["ZZ-30"]
        assert cv.bridge_result == "skip"
        assert cv.final_verdict in ("skip", "green")  # skip+skip -> skip

    def test_expected_by_case_only_attributes(self) -> None:
        # An AI step with expected_by_case but no `case:` tag still binds to cases.
        rec = RunRecord(_run_doc([("ZZ-30", "t"), ("ZZ-32", "t")]), 0)
        step = {"id": "s1", "assert": {"ai": {"mode": "video", "expected_by_case": {"ZZ-30": {}, "ZZ-32": {}}}}}
        _attribute_step_to_cases(rec, step, _result(success=True, ai_pending=True))
        _finalize_case_verdicts(rec, None)
        assert rec.cases["ZZ-30"].ai_result == "pending"
        assert rec.cases["ZZ-32"].ai_result == "pending"

    def test_untagged_failure_is_run_fatal(self) -> None:
        rec = RunRecord(_run_doc([("ZZ-30", "t"), ("ZZ-32", "t")]), 0)
        # a tagged assert passed...
        _attribute_step_to_cases(rec, {"id": "s1", "case": "ZZ-30"}, _result(success=True))
        # ...but a plain step failed → the run can't be trusted
        _attribute_step_to_cases(rec, {"id": "s0", "action": "wait_for_state"}, _result(success=False, error="never idle"))
        assert rec.untagged_failure is True
        run_fatal = rec.untagged_first_error if rec.untagged_failure else None
        _finalize_case_verdicts(rec, run_fatal)
        assert rec.cases["ZZ-30"].final_verdict == "red"
        assert rec.cases["ZZ-32"].final_verdict == "red"
        assert "never idle" in (rec.cases["ZZ-30"].error or "")

    def test_worst_verdict_rollup(self) -> None:
        rec = RunRecord(_run_doc([("a", "t"), ("b", "t")]), 0)
        rec.cases["a"].final_verdict = "green"
        rec.cases["b"].final_verdict = "red"
        assert rec.worst_verdict == "red"
        assert rec.final_verdict == "red"

    def test_test_id_compat_property(self) -> None:
        rec = RunRecord(_run_doc([("a", "t")]), 0)
        assert rec.test_id == "run-01"
        assert rec.test_doc["id"] == "run-01"


class TestBuildSummaryRunFlattening:
    def test_run_flattens_to_per_case_entries(self, tmp_path: Path) -> None:
        from framework.helpers.artifact import ArtifactContext

        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        runner._suite_doc = {"suite": "S", "tests": []}

        rec = RunRecord(_run_doc([("ZZ-30", "Trigger"), ("ZZ-32", "Held"), ("ZZ-33", "NonCoin")]), 0)
        rec.artifact = ArtifactContext(run_dir=tmp_path / "run", test_id="run-01")
        rec.duration_ms = 1234
        rec.cases["ZZ-30"].bridge_result = "pass"; rec.cases["ZZ-30"].final_verdict = "green"
        rec.cases["ZZ-32"].bridge_result = "pass"; rec.cases["ZZ-32"].final_verdict = "green"
        rec.cases["ZZ-33"].bridge_result = "fail"; rec.cases["ZZ-33"].final_verdict = "red"
        rec.cases["ZZ-33"].error = "coins moved"
        runner._records = [rec]

        import time
        summary = runner._build_summary(
            suite_start=time.time(), stop_reason=None, gemini_image=None, gemini_video=None
        )

        assert summary["total"] == 3
        assert summary["counts"]["green"] == 2
        assert summary["counts"]["red"] == 1
        ids = {t["test_id"]: t for t in summary["tests"]}
        assert set(ids) == {"ZZ-30", "ZZ-32", "ZZ-33"}
        # all share the run's artifact dir and carry run_id
        assert all(t["run_id"] == "run-01" for t in summary["tests"])
        shared = {t["artifact_dir"] for t in summary["tests"]}
        assert len(shared) == 1
        assert ids["ZZ-33"]["error"] == "coins moved"
        assert ids["ZZ-30"]["duration_ms"] == 1234

    def test_mixed_tests_and_runs(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        runner._suite_doc = {"suite": "S", "tests": []}
        test_rec = _make_record("T-01", verdict="green")
        run_rec = RunRecord(_run_doc([("C-1", "t"), ("C-2", "t")]), 0)
        run_rec.cases["C-1"].final_verdict = "green"
        run_rec.cases["C-2"].final_verdict = "orange"
        runner._records = [test_rec, run_rec]

        import time
        summary = runner._build_summary(
            suite_start=time.time(), stop_reason=None, gemini_image=None, gemini_video=None
        )
        # 1 test + 2 cases = 3 summary entries
        assert summary["total"] == 3
        assert summary["counts"]["green"] == 2
        assert summary["counts"]["orange"] == 1


# ---------------------------------------------------------------------------
# Fail-fast on setup failure (_mark_skipped)
# ---------------------------------------------------------------------------


class TestMarkSkipped:
    def test_skipped_test_is_red_no_artifact(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        doc = {"id": "ZZ-01", "title": "t", "steps": []}
        rec = runner._mark_skipped(doc, 0, "group 'g' setup failed: boom", is_run=False)
        assert isinstance(rec, TestRecord)
        assert rec.final_verdict == "red"
        assert rec.bridge_result == "fail"
        assert rec.ai_result == "skip"
        assert rec.error == "group 'g' setup failed: boom"
        assert rec.artifact is None
        assert runner._records == [rec]

    def test_skipped_run_marks_all_cases_red(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        rec = runner._mark_skipped(
            _run_doc([("C-1", "t"), ("C-2", "t")]), 0, "suite setup failed: boom", is_run=True
        )
        assert isinstance(rec, RunRecord)
        assert rec.error == "suite setup failed: boom"
        assert rec.artifact is None
        assert all(cv.final_verdict == "red" for cv in rec.cases.values())
        assert all(cv.error == "suite setup failed: boom" for cv in rec.cases.values())
        assert rec.worst_verdict == "red"

    def test_skipped_records_counted_red_in_summary(self, tmp_path: Path) -> None:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        runner._suite_doc = {"suite": "S", "tests": []}
        runner._mark_skipped({"id": "ZZ-01", "title": "t", "steps": []}, 0, "setup failed", is_run=False)
        runner._mark_skipped(_run_doc([("C-1", "t"), ("C-2", "t")]), 1, "setup failed", is_run=True)

        import time
        summary = runner._build_summary(
            suite_start=time.time(), stop_reason="suite setup failed", gemini_image=None, gemini_video=None
        )
        # 1 skipped test + 2 skipped run-cases = 3 red entries, artifact_dir None
        assert summary["total"] == 3
        assert summary["counts"]["red"] == 3
        assert all(t["artifact_dir"] is None for t in summary["tests"])
        assert summary["stop_reason"] == "suite setup failed"


# ---------------------------------------------------------------------------
# Per-case report files + summary navigation links
# ---------------------------------------------------------------------------


class TestReportLinks:
    def _runner(self, tmp_path: Path, records: list) -> SuiteRunner:
        runner = SuiteRunner("zeus", "suite-01", "dev", base_dir=tmp_path)
        runner._suite_doc = _minimal_valid_suite()
        runner._records = records
        return runner

    def test_single_test_report_and_replay_paths(self, tmp_path: Path) -> None:
        from framework.helpers.artifact import ArtifactContext
        rec = _make_record("ZZ-42", verdict="green")
        rec.artifact = ArtifactContext(run_dir=tmp_path / "run", test_id="ZZ-42")
        runner = self._runner(tmp_path, [rec])

        import time
        summary = runner._build_summary(
            suite_start=time.time(), stop_reason=None,
            gemini_image=None, gemini_video=None,
        )
        entry = summary["tests"][0]
        assert entry["report"] == "ZZ-42/result.html"
        assert entry["replay"] == "ZZ-42/replay.html"

    def test_run_cases_get_per_case_report_paths(self, tmp_path: Path) -> None:
        from framework.helpers.artifact import ArtifactContext
        run_doc = {
            "id": "run-1",
            "title": "One bonus",
            "cases": [{"id": "ZZ-30", "title": "c30"}, {"id": "ZZ-32", "title": "c32"}],
        }
        rr = RunRecord(run_doc, 0)
        rr.artifact = ArtifactContext(run_dir=tmp_path / "run", test_id="run-1")
        for cv in rr.cases.values():
            cv.bridge_result = cv.ai_result = "pass"
            cv.final_verdict = "green"
        runner = self._runner(tmp_path, [rr])

        import time
        summary = runner._build_summary(
            suite_start=time.time(), stop_reason=None,
            gemini_image=None, gemini_video=None,
        )
        reports = {t["test_id"]: t["report"] for t in summary["tests"]}
        assert reports["ZZ-30"] == "run-1/case-ZZ-30.html"
        assert reports["ZZ-32"] == "run-1/case-ZZ-32.html"
        assert all(t["replay"] == "run-1/replay.html" for t in summary["tests"])

    def test_report_none_without_artifact(self, tmp_path: Path) -> None:
        rec = _make_record("ZZ-01", verdict="green")  # no artifact set
        runner = self._runner(tmp_path, [rec])
        import time
        summary = runner._build_summary(
            suite_start=time.time(), stop_reason=None,
            gemini_image=None, gemini_video=None,
        )
        assert summary["tests"][0]["report"] is None

    def test_summary_html_links_id_to_report(self, tmp_path: Path) -> None:
        from framework.runner.suite_runner import _write_summary_html
        summary = {
            "suite": "S", "game": "zeus", "env": "dev", "run_date": "2026-04-12",
            "duration_ms": 1000, "total": 1, "counts": {"green": 1},
            "stop_reason": None, "costs": {},
            "tests": [{
                "test_id": "ZZ-42", "title": "t", "bridge_result": "pass",
                "ai_result": "pass", "final_verdict": "green", "duration_ms": 1000,
                "error": None, "timed_out": False,
                "report": "ZZ-42/result.html", "replay": "ZZ-42/replay.html",
            }],
        }
        out = tmp_path / "summary.html"
        _write_summary_html(summary, out)
        html = out.read_text(encoding="utf-8")
        assert '<a href="ZZ-42/result.html">ZZ-42</a>' in html

    def test_summary_html_plain_id_without_report(self, tmp_path: Path) -> None:
        from framework.runner.suite_runner import _write_summary_html
        summary = {
            "suite": "S", "game": "zeus", "env": "dev", "run_date": "2026-04-12",
            "duration_ms": 1000, "total": 1, "counts": {"green": 1},
            "stop_reason": None, "costs": {},
            "tests": [{
                "test_id": "ZZ-99", "title": "t", "bridge_result": "pass",
                "ai_result": "pass", "final_verdict": "green", "duration_ms": 1000,
                "error": None, "timed_out": False, "report": None, "replay": None,
            }],
        }
        out = tmp_path / "summary.html"
        _write_summary_html(summary, out)
        html = out.read_text(encoding="utf-8")
        assert "ZZ-99" in html
        assert "<a href=" not in html  # no link when no report


# ---------------------------------------------------------------------------
# At-step image AI fold — per-case attribution for runs (_fold_run_image_ai)
# ---------------------------------------------------------------------------


def test_fold_run_image_ai_attributes_per_case(tmp_path):
    from unittest.mock import MagicMock

    from framework.helpers.gemini import GeminiResult
    from framework.runner.suite_runner import RunRecord, SuiteRunner

    runner = SuiteRunner(game="zeus", suite_name="x", env="dev", base_dir=tmp_path)
    run_doc = {"id": "R1", "title": "r", "cases": [{"id": "C1", "title": "c1"}, {"id": "C2", "title": "c2"}]}
    record = RunRecord(run_doc, 0)
    for cv in record.cases.values():
        cv.bridge_result = "pass"
    record.artifact = MagicMock()

    step = {
        "id": "step-05",
        "assert": {"ai": {"mode": "image", "expected_by_case": {"C1": {"matches": True}, "C2": {"flag": True}}}},
    }
    # One screenshot answer judged per case: C1 wants matches=True (got True -> pass);
    # C2 wants flag=True (got False -> fail).
    result = GeminiResult(success=True, data={"matches": True, "flag": False}, reason=None)
    runner._fold_run_image_ai(record, step, result, {})

    assert record.cases["C1"].ai_result == "pass"
    assert record.cases["C2"].ai_result == "fail"
    assert record.cases["C1"].final_verdict == "green"    # bridge pass + ai pass
    assert record.cases["C2"].final_verdict == "orange"   # bridge pass + ai fail
    record.artifact.write_run_result.assert_called_once()
