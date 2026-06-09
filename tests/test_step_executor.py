"""
Tests for framework/runner/step_executor.py

Acceptance criteria:
- All supported action types dispatch to the correct helper call.
- Unsupported action type raises UnsupportedActionError — never silently ignored.
- After each step, timeline entry is recorded with id, action, start_ms, end_ms.
- After each step, bridge events are drained and appended to artifact.
- assert steps with bridge block call the correct getter and compare to expected.
- assert steps with AI image block call gemini_image.analyse_image and check expected.
- assert steps with video AI block mark ai_pending=True without calling Gemini.
- StepResult.success is False on assertion mismatch; error describes the mismatch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.runner.step_executor import StepExecutor, StepResult, UnsupportedActionError


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_executor(
    tmp_path: Path,
    bridge_overrides: dict | None = None,
    gemini_image: Any = None,
) -> tuple[StepExecutor, MagicMock, MagicMock, MagicMock]:
    """Create a StepExecutor with mocked dependencies."""
    from framework.helpers.artifact import ArtifactContext

    run_dir = tmp_path / "test-results" / "zeus" / "dev" / "suite-test-2026-04-10-120000"
    artifact = ArtifactContext(
        run_dir=run_dir,
        test_id="ZZ-01",
    )

    bridge = AsyncMock()
    # Default bridge behaviour
    bridge.drain_events = AsyncMock(return_value=[])
    bridge.wait_for_state = AsyncMock(return_value=None)
    bridge.get_free_spins_remaining = AsyncMock(return_value=3)
    bridge.spin = AsyncMock(return_value=None)
    bridge._eval = AsyncMock(return_value=None)
    if bridge_overrides:
        for attr, val in bridge_overrides.items():
            setattr(bridge, attr, val)

    network = AsyncMock()
    network.go_offline = AsyncMock(return_value=None)
    network.go_online = AsyncMock(return_value=None)

    page = AsyncMock()
    page.goto = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    page.keyboard = AsyncMock()
    page.keyboard.press = AsyncMock(return_value=None)
    page.mouse = AsyncMock()
    page.mouse.click = AsyncMock(return_value=None)
    page.click = AsyncMock(return_value=None)

    executor = StepExecutor(
        bridge=bridge,
        network=network,
        artifact=artifact,
        gemini_image=gemini_image,
        page=page,
        test_start_time=0.0,
    )
    return executor, bridge, network, page


# ---------------------------------------------------------------------------
# Unsupported action
# ---------------------------------------------------------------------------


class TestUnsupportedAction:
    def test_unknown_action_raises_error(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "fly_to_moon"}
        with pytest.raises(UnsupportedActionError, match="fly_to_moon"):
            asyncio.get_event_loop().run_until_complete(executor.execute(step))

    def test_error_message_lists_supported_actions(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "bad_action"}
        with pytest.raises(UnsupportedActionError) as exc_info:
            asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert "navigate" in str(exc_info.value)
        assert "bridge_action" in str(exc_info.value)

    def test_empty_action_raises_error(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": ""}
        with pytest.raises(UnsupportedActionError):
            asyncio.get_event_loop().run_until_complete(executor.execute(step))


# ---------------------------------------------------------------------------
# Timeline recording
# ---------------------------------------------------------------------------


class TestTimeline:
    def test_timeline_entry_recorded_after_step(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "capture"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert len(executor.timeline) == 1

    def test_timeline_entry_has_required_fields(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "capture"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        entry = executor.timeline[0]
        assert entry["id"] == "step-01"
        assert entry["action"] == "capture"
        assert "start_ms" in entry
        assert "end_ms" in entry
        assert isinstance(entry["start_ms"], int)
        assert isinstance(entry["end_ms"], int)

    def test_timeline_end_ms_gte_start_ms(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "capture"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        entry = executor.timeline[0]
        assert entry["end_ms"] >= entry["start_ms"]

    def test_multiple_steps_build_timeline(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        for i in range(3):
            step = {"id": f"step-0{i+1}", "action": "capture"}
            asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert len(executor.timeline) == 3


# ---------------------------------------------------------------------------
# Event draining
# ---------------------------------------------------------------------------


class TestEventDrain:
    def test_events_drained_after_each_step(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(tmp_path)
        events = [{"t": 0.5, "type": "spin_result", "data": {}}]
        bridge.drain_events = AsyncMock(return_value=events)

        step = {"id": "step-01", "action": "capture"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))

        bridge.drain_events.assert_awaited_once()
        assert executor._artifact.events_path.exists()

    def test_empty_drain_does_not_write_events(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(tmp_path)
        bridge.drain_events = AsyncMock(return_value=[])

        step = {"id": "step-01", "action": "capture"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))

        assert not executor._artifact.events_path.exists()

    def test_drain_failure_does_not_crash_step(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(tmp_path)
        bridge.drain_events = AsyncMock(side_effect=RuntimeError("bridge gone"))

        step = {"id": "step-01", "action": "capture"}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is True  # step itself succeeded


# ---------------------------------------------------------------------------
# Action: navigate
# ---------------------------------------------------------------------------


class TestNavigate:
    def test_navigate_calls_page_goto(self, tmp_path: Path) -> None:
        executor, _, _, page = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "navigate", "params": {"url": "http://example.com"}}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        page.goto.assert_awaited_once_with("http://example.com", wait_until="networkidle")

    def test_navigate_missing_url_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "navigate", "params": {}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False
        assert "url" in (result.error or "")


# ---------------------------------------------------------------------------
# Action: wait_for_state
# ---------------------------------------------------------------------------


class TestWaitForState:
    def test_wait_for_state_calls_bridge(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "wait_for_state", "params": {"state": "idle"}}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        bridge.wait_for_state.assert_awaited_once_with("idle", timeout_ms=30000)

    def test_wait_for_state_custom_timeout(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(tmp_path)
        step = {
            "id": "step-01",
            "action": "wait_for_state",
            "params": {"state": "idle", "timeout_ms": 5000},
        }
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        bridge.wait_for_state.assert_awaited_once_with("idle", timeout_ms=5000)

    def test_wait_for_state_missing_state_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "wait_for_state", "params": {}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False

    def test_wait_for_state_with_capture_takes_screenshot(self, tmp_path: Path) -> None:
        executor, _, _, page = _make_executor(tmp_path)
        step = {
            "id": "step-01",
            "action": "wait_for_state",
            "params": {"state": "idle"},
            "capture": True,
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.screenshot_path is not None
        assert result.screenshot_path.exists()


# ---------------------------------------------------------------------------
# Action: capture
# ---------------------------------------------------------------------------


class TestCapture:
    def test_capture_saves_screenshot(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "capture"}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is True
        assert result.screenshot_path is not None
        assert result.screenshot_path.exists()

    def test_capture_uses_incremental_naming(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        result1 = asyncio.get_event_loop().run_until_complete(
            executor.execute({"id": "step-01", "action": "capture"})
        )
        result2 = asyncio.get_event_loop().run_until_complete(
            executor.execute({"id": "step-02", "action": "capture"})
        )
        assert result1.screenshot_path != result2.screenshot_path
        assert "step-01" in result1.screenshot_path.name
        assert "step-02" in result2.screenshot_path.name


# ---------------------------------------------------------------------------
# Action: go_offline / go_online
# ---------------------------------------------------------------------------


class TestNetworkActions:
    def test_go_offline_calls_network_helper(self, tmp_path: Path) -> None:
        executor, _, network, _ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "go_offline"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        network.go_offline.assert_awaited_once()

    def test_go_online_calls_network_helper(self, tmp_path: Path) -> None:
        executor, _, network, _ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "go_online"}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        network.go_online.assert_awaited_once()


# ---------------------------------------------------------------------------
# Action: press_key
# ---------------------------------------------------------------------------


class TestPressKey:
    def test_press_key_calls_keyboard(self, tmp_path: Path) -> None:
        executor, _, _, page = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "press_key", "params": {"key": "Space"}}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        page.keyboard.press.assert_awaited_once_with("Space")

    def test_press_key_missing_key_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "press_key", "params": {}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False


# ---------------------------------------------------------------------------
# Action: click
# ---------------------------------------------------------------------------


class TestClick:
    def test_click_with_selector_calls_page_click(self, tmp_path: Path) -> None:
        executor, _, _, page = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "click", "params": {"selector": "#spin-btn"}}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        page.click.assert_awaited_once_with("#spin-btn")

    def test_click_with_coordinates_calls_mouse_click(self, tmp_path: Path) -> None:
        executor, _, _, page = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "click", "params": {"x": 100, "y": 200}}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        page.mouse.click.assert_awaited_once_with(100.0, 200.0)

    def test_click_missing_params_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "click", "params": {}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False


# ---------------------------------------------------------------------------
# Action: wait_ms
# ---------------------------------------------------------------------------


class TestWaitMs:
    def test_wait_ms_returns_success(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "wait_ms", "params": {"ms": 1}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is True

    def test_wait_ms_zero_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "wait_ms", "params": {"ms": 0}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False


# ---------------------------------------------------------------------------
# Action: bridge_action
# ---------------------------------------------------------------------------


class TestBridgeAction:
    def test_spin_calls_bridge_spin(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "bridge_action", "params": {"name": "spin"}}
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        bridge.spin.assert_awaited_once()

    def test_missing_name_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {"id": "step-01", "action": "bridge_action", "params": {}}
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False


# ---------------------------------------------------------------------------
# Action: bridge_assert
# ---------------------------------------------------------------------------


class TestBridgeAssert:
    def test_bridge_assert_passes_when_match(self, tmp_path: Path) -> None:
        bridge_overrides = {"get_free_spins_remaining": AsyncMock(return_value=3)}
        executor, *_ = _make_executor(tmp_path, bridge_overrides=bridge_overrides)
        step = {
            "id": "step-01",
            "action": "bridge_assert",
            "params": {"method": "getFreeSpinsRemaining", "expected": 3},
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is True
        assert result.bridge_assert_result is not None
        assert result.bridge_assert_result["passed"] is True

    def test_bridge_assert_fails_on_mismatch(self, tmp_path: Path) -> None:
        bridge_overrides = {"get_free_spins_remaining": AsyncMock(return_value=1)}
        executor, *_ = _make_executor(tmp_path, bridge_overrides=bridge_overrides)
        step = {
            "id": "step-01",
            "action": "bridge_assert",
            "params": {"method": "getFreeSpinsRemaining", "expected": 3},
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False
        assert "1" in (result.error or "")
        assert "3" in (result.error or "")

    def test_bridge_assert_missing_method_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {
            "id": "step-01",
            "action": "bridge_assert",
            "params": {"expected": 3},
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False


# ---------------------------------------------------------------------------
# Action: assert (bridge + AI image)
# ---------------------------------------------------------------------------


class TestAssertStep:
    def test_assert_bridge_passes(self, tmp_path: Path) -> None:
        bridge_overrides = {"get_free_spins_remaining": AsyncMock(return_value=3)}
        executor, *_ = _make_executor(tmp_path, bridge_overrides=bridge_overrides)
        step = {
            "id": "step-01",
            "assert": {
                "bridge": {"method": "getFreeSpinsRemaining", "expected": 3},
            },
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is True
        assert result.bridge_assert_result["passed"] is True

    def test_assert_bridge_fails(self, tmp_path: Path) -> None:
        bridge_overrides = {"get_free_spins_remaining": AsyncMock(return_value=0)}
        executor, *_ = _make_executor(tmp_path, bridge_overrides=bridge_overrides)
        step = {
            "id": "step-01",
            "assert": {
                "bridge": {"method": "getFreeSpinsRemaining", "expected": 3},
            },
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False

    def test_assert_video_ai_marks_pending(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {
            "id": "step-01",
            "assert": {
                "ai": {
                    "mode": "video",
                    "video_trim": {"from_step": "step-01", "to_step": "step-03"},
                    "prompt": "Did the reels spin?",
                    "output_schema": {},
                    "expected": {},
                }
            },
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.ai_pending is True
        assert result.ai_result is None  # not processed inline

    def test_assert_image_ai_calls_gemini(self, tmp_path: Path) -> None:
        from framework.helpers.gemini import GeminiResult

        gemini = MagicMock()
        gemini.analyse_image = MagicMock(
            return_value=GeminiResult(
                success=True,
                data={"free_spins": 3, "confidence": "high"},
                reason=None,
                input_tokens=100,
                output_tokens=20,
            )
        )

        executor, *_ = _make_executor(tmp_path, gemini_image=gemini)

        # Save a fake screenshot so ref_screenshot resolves
        screenshot_path = executor._artifact.screenshot_path(2)
        screenshot_path.write_bytes(b"\x89PNG" + b"\x00" * 10)

        step = {
            "id": "step-03",
            "assert": {
                "ai": {
                    "mode": "image",
                    "ref_screenshot": "step-02",
                    "prompt": "How many free spins?",
                    "output_schema": {"free_spins": {"type": "number"}},
                    "expected": {"free_spins": 3, "confidence": "high"},
                }
            },
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is True
        assert result.ai_result is not None
        assert result.ai_result["passed"] is True
        gemini.analyse_image.assert_called_once()

    def test_assert_image_ai_fails_on_mismatch(self, tmp_path: Path) -> None:
        from framework.helpers.gemini import GeminiResult

        gemini = MagicMock()
        gemini.analyse_image = MagicMock(
            return_value=GeminiResult(
                success=True,
                data={"free_spins": 1, "confidence": "low"},
                reason=None,
                input_tokens=100,
                output_tokens=20,
            )
        )

        executor, *_ = _make_executor(tmp_path, gemini_image=gemini)
        screenshot_path = executor._artifact.screenshot_path(2)
        screenshot_path.write_bytes(b"\x89PNG" + b"\x00" * 10)

        step = {
            "id": "step-03",
            "assert": {
                "ai": {
                    "mode": "image",
                    "ref_screenshot": "step-02",
                    "prompt": "How many free spins?",
                    "output_schema": {},
                    "expected": {"free_spins": 3, "confidence": "high"},
                }
            },
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False
        assert result.ai_result["passed"] is False

    def test_assert_unknown_ai_mode_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        step = {
            "id": "step-01",
            "assert": {
                "ai": {
                    "mode": "hologram",
                    "prompt": "Does it look good?",
                    "output_schema": {},
                    "expected": {},
                }
            },
        }
        result = asyncio.get_event_loop().run_until_complete(executor.execute(step))
        assert result.success is False
        assert "hologram" in (result.error or "")


# ---------------------------------------------------------------------------
# save: capture step result into a variable
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSaveVariable:
    def test_save_stores_bridge_action_result(self, tmp_path: Path) -> None:
        executor, bridge, *_ = _make_executor(
            tmp_path, bridge_overrides={"get_last_payout": AsyncMock(return_value=12.5)}
        )
        step = {
            "id": "step-05",
            "action": "bridge_action",
            "params": {"name": "getLastPayout"},
            "save": "payout",
        }
        result = _run(executor.execute(step))
        assert result.success is True
        assert executor.vars["payout"] == 12.5

    def test_saved_value_recorded_in_timeline(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(
            tmp_path, bridge_overrides={"get_balance": AsyncMock(return_value=1000)}
        )
        step = {
            "id": "step-01",
            "action": "bridge_action",
            "params": {"name": "getBalance"},
            "save": "balance_before",
        }
        _run(executor.execute(step))
        assert executor.timeline[0]["saved"] == {"balance_before": 1000}

    def test_save_skipped_when_step_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        # bridge_action with no name fails → nothing saved
        step = {"id": "step-01", "action": "bridge_action", "params": {}, "save": "x"}
        result = _run(executor.execute(step))
        assert result.success is False
        assert "x" not in executor.vars


# ---------------------------------------------------------------------------
# bridge_assert with a saved variable / args (deterministic, bridge-owned logic)
# ---------------------------------------------------------------------------


class TestBridgeAssertWithVar:
    def test_single_var_ref_in_args(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        executor.vars["declaredVar"] = 7
        bridge._eval = AsyncMock(return_value=True)

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "testFreeSpin", "args": ["$declaredVar"], "expected": True},
        }
        result = _run(executor.execute(step))

        assert result.success is True
        expr = bridge._eval.call_args.args[0]
        assert expr == "window.__gameTestBridge.testFreeSpin(7)"
        assert result.bridge_assert_result["args"] == [7]

    def test_var_ref_at_any_position(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        executor.vars["payout"] = 5
        executor.vars["grid"] = [[1, 2], [3, 4]]
        bridge._eval = AsyncMock(return_value=True)

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {
                "method": "fn",
                "args": ["$grid", 0, "$payout", "zeus"],
                "expected": True,
            },
        }
        _run(executor.execute(step))

        expr = bridge._eval.call_args.args[0]
        assert expr == 'window.__gameTestBridge.fn([[1, 2], [3, 4]], 0, 5, "zeus")'

    def test_fails_on_mismatch(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        executor.vars["declaredVar"] = 7
        bridge._eval = AsyncMock(return_value=False)

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "testFreeSpin", "args": ["$declaredVar"], "expected": True},
        }
        result = _run(executor.execute(step))

        assert result.success is False
        assert "bridge_assert failed" in (result.error or "")
        assert "testFreeSpin" in (result.error or "")

    def test_missing_saved_var_fails(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value=True)

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "testFreeSpin", "args": ["$neverSaved"], "expected": True},
        }
        result = _run(executor.execute(step))

        assert result.success is False
        assert "neverSaved" in (result.error or "")
        bridge._eval.assert_not_called()

    def test_dollar_escape_is_literal(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value=True)

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "fn", "args": ["$$5.00"], "expected": True},
        }
        _run(executor.execute(step))

        expr = bridge._eval.call_args.args[0]
        assert expr == 'window.__gameTestBridge.fn("$5.00")'

    def test_args_must_be_list(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value=True)

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "fn", "args": "notalist", "expected": True},
        }
        result = _run(executor.execute(step))
        assert result.success is False
        assert "must be a list" in (result.error or "")

    def test_no_args_uses_python_wrapper(self, tmp_path: Path) -> None:
        # Backward compat: args-less bridge_assert calls the Python wrapper, not _eval.
        executor, bridge, _, _ = _make_executor(
            tmp_path, bridge_overrides={"is_spin_enabled": AsyncMock(return_value=True)}
        )
        bridge._eval = AsyncMock(return_value="UNUSED")

        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "isSpinEnabled", "expected": True},
        }
        result = _run(executor.execute(step))

        assert result.success is True
        bridge._eval.assert_not_called()


# ---------------------------------------------------------------------------
# bridge_action with an explicit args list (direct JS call, savable result)
# ---------------------------------------------------------------------------


class TestBridgeActionArgs:
    def test_args_calls_bridge_eval_directly(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value=None)
        step = {
            "id": "step-01",
            "action": "bridge_action",
            "params": {"name": "startEventCapture", "args": ["bonus"]},
        }
        result = _run(executor.execute(step))
        assert result.success is True
        expr = bridge._eval.call_args.args[0]
        assert expr == 'window.__gameTestBridge.startEventCapture("bonus")'

    def test_args_result_is_saved(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        events = [{"type": "spin_result", "data": {"spinState": "START_BONUS"}}]
        bridge._eval = AsyncMock(return_value=events)
        step = {
            "id": "step-03",
            "action": "bridge_action",
            "params": {"name": "endEventCapture", "args": ["bonus"]},
            "save": "events",
        }
        result = _run(executor.execute(step))
        assert result.success is True
        assert executor.vars["events"] == events

    def test_args_resolves_saved_var(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        executor.vars["events"] = [1, 2, 3]
        bridge._eval = AsyncMock(return_value=[1, 2, 3])
        step = {
            "id": "step-04",
            "action": "bridge_action",
            "params": {"name": "filterBonusEvent", "args": ["$events"]},
        }
        _run(executor.execute(step))
        expr = bridge._eval.call_args.args[0]
        assert expr == "window.__gameTestBridge.filterBonusEvent([1, 2, 3])"

    def test_args_object_arg_serialised(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value={"matched": True, "spins": 1})
        step = {
            "id": "step-02",
            "action": "bridge_action",
            "params": {"name": "spinUntilBonus", "args": [{"maxSpins": 60}]},
        }
        result = _run(executor.execute(step))
        assert result.success is True
        expr = bridge._eval.call_args.args[0]
        assert expr == 'window.__gameTestBridge.spinUntilBonus({"maxSpins": 60})'

    def test_args_missing_var_fails_without_eval(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value=None)
        step = {
            "id": "step-01",
            "action": "bridge_action",
            "params": {"name": "filterBonusEvent", "args": ["$nope"]},
        }
        result = _run(executor.execute(step))
        assert result.success is False
        assert "nope" in (result.error or "")
        bridge._eval.assert_not_called()


# ---------------------------------------------------------------------------
# bridge_assert with a path into an object-returning bridge function
# ---------------------------------------------------------------------------


class TestBridgeAssertPath:
    def test_path_extracts_field_pass(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        executor.vars["events"] = [{"type": "spin_result"}]
        bridge._eval = AsyncMock(
            return_value={"valid": True, "bonusFound": True, "respinCount": 4, "violations": []}
        )
        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {
                "method": "verifyHoldSpinCoinsHeld",
                "args": ["$events"],
                "path": "valid",
                "expected": True,
            },
        }
        result = _run(executor.execute(step))
        assert result.success is True
        assert result.bridge_assert_result["actual"] is True
        assert result.bridge_assert_result["path"] == "valid"

    def test_path_extracts_field_fail(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        executor.vars["events"] = []
        bridge._eval = AsyncMock(return_value={"valid": False, "violations": [{"detail": "x"}]})
        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {
                "method": "verifyHoldSpinFreespinCountDown",
                "args": ["$events"],
                "path": "valid",
                "expected": True,
            },
        }
        result = _run(executor.execute(step))
        assert result.success is False
        assert "verifyHoldSpinFreespinCountDown" in (result.error or "")
        assert ".valid" in (result.error or "")

    def test_path_nested_and_list_index(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value={"a": {"b": [10, 20, 30]}})
        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "fn", "args": [], "path": "a.b.1", "expected": 20},
        }
        result = _run(executor.execute(step))
        assert result.success is True

    def test_path_missing_key_fails_clearly(self, tmp_path: Path) -> None:
        executor, bridge, _, _ = _make_executor(tmp_path)
        bridge._eval = AsyncMock(return_value={"valid": True})
        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "fn", "args": [], "path": "nope", "expected": True},
        }
        result = _run(executor.execute(step))
        assert result.success is False
        assert "path 'nope'" in (result.error or "")

    def test_path_works_with_python_wrapper(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(
            tmp_path,
            bridge_overrides={"get_snapshot": AsyncMock(return_value={"isOnBonusGame": True})},
        )
        step = {
            "id": "step-06",
            "action": "bridge_assert",
            "params": {"method": "getSnapshot", "path": "isOnBonusGame", "expected": True},
        }
        result = _run(executor.execute(step))
        assert result.success is True


# ---------------------------------------------------------------------------
# camelCase to snake_case conversion
# ---------------------------------------------------------------------------


class TestCamelToSnake:
    def test_get_free_spins_remaining(self) -> None:
        assert StepExecutor._camel_to_snake("getFreeSpinsRemaining") == "get_free_spins_remaining"

    def test_spin_unchanged(self) -> None:
        assert StepExecutor._camel_to_snake("spin") == "spin"

    def test_is_turbo_active(self) -> None:
        assert StepExecutor._camel_to_snake("isTurboActive") == "is_turbo_active"

    def test_get_app_state(self) -> None:
        assert StepExecutor._camel_to_snake("getAppState") == "get_app_state"
