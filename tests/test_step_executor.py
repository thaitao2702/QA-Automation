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
# Action: click_button (real-coordinate click via getButtonRect)
# ---------------------------------------------------------------------------


class TestClickButton:
    _RECT = {
        "x": 10.0, "y": 20.0, "width": 40.0, "height": 40.0,
        "centerX": 30.0, "centerY": 40.0, "visible": True, "interactable": True,
    }

    def _run(self, executor, step):
        return asyncio.get_event_loop().run_until_complete(executor.execute(step))

    def test_resolves_rect_and_clicks_center(self, tmp_path: Path) -> None:
        executor, bridge, _, page = _make_executor(
            tmp_path, bridge_overrides={"get_button_rect": AsyncMock(return_value=self._RECT)}
        )
        step = {"id": "s1", "action": "click_button", "params": {"name": "spin"}}
        result = self._run(executor, step)
        assert result.success is True
        bridge.get_button_rect.assert_awaited_once_with("spin")
        page.mouse.click.assert_awaited_once_with(30.0, 40.0)
        assert result.result == self._RECT  # rect returned so it is save:-able

    def test_missing_name_fails(self, tmp_path: Path) -> None:
        executor, *_ = _make_executor(tmp_path)
        result = self._run(executor, {"id": "s1", "action": "click_button", "params": {}})
        assert result.success is False
        assert "name" in result.error

    def test_unknown_button_fails_without_clicking(self, tmp_path: Path) -> None:
        executor, _, _, page = _make_executor(
            tmp_path, bridge_overrides={"get_button_rect": AsyncMock(return_value=None)}
        )
        step = {"id": "s1", "action": "click_button", "params": {"name": "ghost"}}
        result = self._run(executor, step)
        assert result.success is False
        assert "not found" in result.error
        page.mouse.click.assert_not_awaited()

    def test_invisible_button_fails_by_default(self, tmp_path: Path) -> None:
        rect = {**self._RECT, "visible": False}
        executor, _, _, page = _make_executor(
            tmp_path, bridge_overrides={"get_button_rect": AsyncMock(return_value=rect)}
        )
        step = {"id": "s1", "action": "click_button", "params": {"name": "spin"}}
        result = self._run(executor, step)
        assert result.success is False
        assert "visible" in result.error
        page.mouse.click.assert_not_awaited()

    def test_require_visible_false_clicks_invisible(self, tmp_path: Path) -> None:
        rect = {**self._RECT, "visible": False}
        executor, _, _, page = _make_executor(
            tmp_path, bridge_overrides={"get_button_rect": AsyncMock(return_value=rect)}
        )
        step = {
            "id": "s1", "action": "click_button",
            "params": {"name": "spin", "require_visible": False},
        }
        result = self._run(executor, step)
        assert result.success is True
        page.mouse.click.assert_awaited_once_with(30.0, 40.0)


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

    def _image_assert_scheduler(self, tmp_path, gemini):
        """Build an executor wired to a real (gemini-provider) AI scheduler + recording sink."""
        from framework.runner.ai_assert_scheduler import AiAssertScheduler, make_ai_semaphore

        executor, *_ = _make_executor(tmp_path, gemini_image=gemini)
        scheduler = AiAssertScheduler(make_ai_semaphore("gemini"), gemini)
        sink_calls: list = []
        executor._ai_scheduler = scheduler
        executor._ai_image_sink = lambda step, result, ri: sink_calls.append((step, result, ri))
        # 'step-02' resolves via the numeric fallback to screenshot index 2.
        executor._artifact.screenshot_path(2).write_bytes(b"\x89PNG" + b"\x00" * 10)
        return executor, scheduler, sink_calls

    def test_assert_image_ai_launches_async_and_grades_pass(self, tmp_path: Path) -> None:
        from framework.helpers.gemini import GeminiResult
        from framework.runner.ai_assert_scheduler import grade_image

        gemini = MagicMock()
        gemini.analyse_image = MagicMock(return_value=GeminiResult(
            success=True, data={"free_spins": 3, "confidence": "high"}, reason=None,
            input_tokens=100, output_tokens=20))
        executor, scheduler, sink_calls = self._image_assert_scheduler(tmp_path, gemini)

        step = {
            "id": "step-03",
            "assert": {"ai": {
                "mode": "image", "ref_screenshot": "step-02",
                "prompt": "How many free spins?",
                "output_schema": {"free_spins": {"type": "number"}},
                "expected": {"free_spins": 3, "confidence": "high"},
            }},
        }
        async def _scenario():
            result = await executor.execute(step)
            # Image AI is launched in the background — pending, not graded inline.
            assert result.ai_pending is True
            assert result.ai_result is None
            await scheduler.await_all()

        asyncio.get_event_loop().run_until_complete(_scenario())
        gemini.analyse_image.assert_called_once()
        assert len(sink_calls) == 1
        _step, res, ri = sink_calls[0]
        ai_data, ai_result, err = grade_image(res, ri)
        assert ai_result == "pass" and ai_data["passed"] is True and err is None

    def test_assert_image_ai_grades_fail_on_mismatch(self, tmp_path: Path) -> None:
        from framework.helpers.gemini import GeminiResult
        from framework.runner.ai_assert_scheduler import grade_image

        gemini = MagicMock()
        gemini.analyse_image = MagicMock(return_value=GeminiResult(
            success=True, data={"free_spins": 1, "confidence": "low"}, reason=None,
            input_tokens=100, output_tokens=20))
        executor, scheduler, sink_calls = self._image_assert_scheduler(tmp_path, gemini)

        step = {
            "id": "step-03",
            "assert": {"ai": {
                "mode": "image", "ref_screenshot": "step-02",
                "prompt": "How many free spins?", "output_schema": {},
                "expected": {"free_spins": 3, "confidence": "high"},
            }},
        }
        async def _scenario():
            result = await executor.execute(step)
            assert result.ai_pending is True
            await scheduler.await_all()

        asyncio.get_event_loop().run_until_complete(_scenario())
        _step, res, ri = sink_calls[0]
        ai_data, ai_result, err = grade_image(res, ri)
        assert ai_result == "fail" and ai_data["passed"] is False

    def test_assert_image_ai_missing_ref_folds_fail_immediately(self, tmp_path: Path) -> None:
        gemini = MagicMock()
        gemini.analyse_image = MagicMock()
        executor, scheduler, sink_calls = self._image_assert_scheduler(tmp_path, gemini)

        step = {
            "id": "step-03",
            "assert": {"ai": {
                "mode": "image", "ref_screenshot": "step-99",  # no such capture
                "prompt": "?", "output_schema": {}, "expected": {},
            }},
        }
        asyncio.get_event_loop().run_until_complete(executor.execute(step))
        # Missing ref → sink called immediately with a failed result, no task launched.
        assert len(sink_calls) == 1
        assert sink_calls[0][1].success is False
        gemini.analyse_image.assert_not_called()

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


# ---------------------------------------------------------------------------
# AI prompt variable substitution — {{name}} from saved vars (deterministic asserts)
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_resolve_text_vars_substitutes(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    executor.vars.update({"bet": "1.00", "n": 3})
    assert executor._resolve_text_vars("shows {{bet}}") == ("shows 1.00", None)
    assert executor._resolve_text_vars("a {{bet}} b {{n}}") == ("a 1.00 b 3", None)
    assert executor._resolve_text_vars("ws {{ bet }}") == ("ws 1.00", None)  # whitespace tolerated
    assert executor._resolve_text_vars("no token here") == ("no token here", None)


def test_resolve_text_vars_coerces_and_passthrough(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    executor.vars.update({"x": 1234.5})
    assert executor._resolve_text_vars("v={{x}}") == ("v=1234.5", None)
    assert executor._resolve_text_vars(42) == (42, None)  # non-string passthrough


def test_resolve_text_vars_unknown_var_errors(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    resolved, err = executor._resolve_text_vars("shows {{missing}}")
    assert resolved == "shows {{missing}}"  # left unchanged on error
    assert err is not None and "missing" in err and "save: missing" in err


def test_assert_image_prompt_resolved_in_place(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    executor.vars["bet"] = "1.00"
    ai = {
        "mode": "image",
        "ref_screenshot": "step-02",
        "prompt": "Does the bet label show {{bet}}?",
        "output_schema": {"matches": {"type": "boolean"}},
        "expected": {"matches": True},
    }
    step = {"id": "step-03", "assert": {"ai": ai}}
    result = _run(executor.execute(step))
    assert result.success is True
    assert result.ai_pending is True
    # Resolved IN PLACE so the deferred image/video processor reads the real value.
    assert step["assert"]["ai"] is ai
    assert ai["prompt"] == "Does the bet label show 1.00?"


def test_assert_expected_string_leaf_resolved(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    executor.vars["bet"] = "1.00"
    step = {
        "id": "step-03",
        "assert": {
            "ai": {
                "mode": "image",
                "ref_screenshot": "step-02",
                "prompt": "Read the bet label.",
                "output_schema": {"shown": {"type": "string"}},
                "expected": {"shown": "{{bet}}"},
            }
        },
    }
    result = _run(executor.execute(step))
    assert result.success is True
    assert step["assert"]["ai"]["expected"]["shown"] == "1.00"


def test_assert_video_prompt_resolved(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    executor.vars["payout"] = 250
    step = {
        "id": "step-10",
        "assert": {
            "ai": {
                "mode": "video",
                "video_trim": {"from_step": "step-01", "to_step": "step-09"},
                "prompt": "Was the total payout {{payout}}?",
                "output_schema": {"ok": {"type": "boolean"}},
                "expected": {"ok": True},
            }
        },
    }
    result = _run(executor.execute(step))
    assert result.ai_pending is True
    assert step["assert"]["ai"]["prompt"] == "Was the total payout 250?"


def test_assert_unknown_var_fails_step(tmp_path: Path) -> None:
    executor, *_ = _make_executor(tmp_path)
    step = {
        "id": "step-03",
        "assert": {
            "ai": {
                "mode": "image",
                "ref_screenshot": "step-02",
                "prompt": "Does it show {{nope}}?",
                "output_schema": {"matches": {"type": "boolean"}},
                "expected": {"matches": True},
            }
        },
    }
    result = _run(executor.execute(step))
    assert result.success is False
    assert result.ai_pending is False
    assert "nope" in (result.error or "")


def test_agent_assert_unknown_var_fails_before_verify(tmp_path: Path) -> None:
    # A check: with an unsaved var fails at execution, before the vision call.
    executor, *_ = _make_executor(tmp_path)
    result = _run(executor.execute({"id": "s1", "check": "the bet is {{missing}}"}))
    assert result.success is False
    assert "agent_assert" in (result.error or "") and "missing" in (result.error or "")


# ---------------------------------------------------------------------------
# Scoped reference resolution for image AI asserts (_image_reference_for)
# ---------------------------------------------------------------------------


def _ref_cache_and_index(tmp_path: Path):
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


def _executor_with_cache(tmp_path: Path, group_doc=None, test_doc=None):
    cache, index = _ref_cache_and_index(tmp_path)
    return StepExecutor(
        bridge=MagicMock(), network=MagicMock(), artifact=MagicMock(),
        gemini_image=MagicMock(), page=MagicMock(),
        ref_cache=cache, group_doc=group_doc, test_doc=test_doc, asset_index=index,
    )


class TestImageReferenceFor:
    def test_no_cache_returns_none(self):
        ex = StepExecutor(bridge=None, network=None, artifact=None, gemini_image=None, page=None)
        assert ex._image_reference_for({"assets": ["symbols.coin"]}) is None

    def test_no_selection_anywhere_returns_none(self, tmp_path: Path):
        ex = _executor_with_cache(tmp_path)
        assert ex._image_reference_for({"mode": "image"}) is None

    def test_assert_level_selection_scopes_reference(self, tmp_path: Path):
        ex = _executor_with_cache(tmp_path)
        ref = ex._image_reference_for({"assets": ["symbols.coin"]})
        assert ref is not None
        assert [Path(p).name for p, _l in ref.items] == ["coin.jpg"]

    def test_test_level_default_used_when_assert_unset(self, tmp_path: Path):
        ex = _executor_with_cache(tmp_path, test_doc={"asset_groups": ["ui_elements"]})
        ref = ex._image_reference_for({"mode": "image"})
        assert [Path(p).name for p, _l in ref.items] == ["bet.jpg"]

    def test_assert_overrides_test(self, tmp_path: Path):
        ex = _executor_with_cache(tmp_path, test_doc={"asset_groups": ["ui_elements"]})
        ref = ex._image_reference_for({"assets": ["symbols.coin"]})
        assert [Path(p).name for p, _l in ref.items] == ["coin.jpg"]


# ---------------------------------------------------------------------------
# Wallet (Playsite balance) actions
# ---------------------------------------------------------------------------


class _FakeWallet:
    """Stand-in for WalletHelper: records calls, returns canned balances."""

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.calls: list[tuple] = []
        self._raise_on = raise_on

    async def get_balance(self, currency: str | None = None):
        self.calls.append(("balance", currency))
        if self._raise_on == "balance":
            raise RuntimeError("playsite boom")
        if currency:
            return 1000.0
        return [{"currency": "USD", "balance": 1000.0, "isDefault": 1}]

    async def deposit(self, amount, currency: str | None = None):
        self.calls.append(("deposit", amount, currency))
        if self._raise_on == "deposit":
            raise RuntimeError("playsite boom")
        return {"currency": currency or "USD", "balance": 1100.0}

    async def withdraw(self, amount, currency: str | None = None):
        self.calls.append(("withdraw", amount, currency))
        return {"currency": currency or "USD", "balance": 900.0}

    async def set_balance(self, target, currency: str | None = None):
        self.calls.append(("set", target, currency))
        if self._raise_on == "set":
            raise RuntimeError("playsite boom")
        return {"currency": currency or "USD", "balance": target}


def _wallet_executor(wallet: Any) -> StepExecutor:
    return StepExecutor(
        bridge=MagicMock(), network=MagicMock(), artifact=MagicMock(),
        gemini_image=MagicMock(), page=MagicMock(), wallet=wallet,
    )


def _run_step(executor: StepExecutor, step: dict) -> StepResult:
    return asyncio.get_event_loop().run_until_complete(executor.execute(step))


class TestWalletActions:
    def test_balance_with_currency_returns_numeric(self) -> None:
        w = _FakeWallet()
        res = _run_step(
            _wallet_executor(w),
            {"id": "s", "action": "wallet_balance", "params": {"currency": "TRY"}},
        )
        assert res.success and res.result == 1000.0
        assert w.calls == [("balance", "TRY")]

    def test_balance_without_currency_returns_wallets(self) -> None:
        w = _FakeWallet()
        res = _run_step(_wallet_executor(w), {"id": "s", "action": "wallet_balance"})
        assert res.success
        assert res.result == [{"currency": "USD", "balance": 1000.0, "isDefault": 1}]
        assert w.calls == [("balance", None)]

    def test_deposit_forwards_amount_and_currency(self) -> None:
        w = _FakeWallet()
        res = _run_step(
            _wallet_executor(w),
            {"id": "s", "action": "wallet_deposit", "params": {"amount": 250, "currency": "TRY"}},
        )
        assert res.success and res.result == {"currency": "TRY", "balance": 1100.0}
        assert w.calls == [("deposit", 250, "TRY")]

    def test_withdraw_forwards_amount_default_currency(self) -> None:
        w = _FakeWallet()
        res = _run_step(
            _wallet_executor(w),
            {"id": "s", "action": "wallet_withdraw", "params": {"amount": 50}},
        )
        assert res.success and res.result == {"currency": "USD", "balance": 900.0}
        assert w.calls == [("withdraw", 50, None)]

    def test_deposit_requires_amount(self) -> None:
        w = _FakeWallet()
        res = _run_step(
            _wallet_executor(w), {"id": "s", "action": "wallet_deposit", "params": {}}
        )
        assert not res.success and "requires 'params.amount'" in res.error
        assert w.calls == []  # never reached the helper

    def test_no_wallet_helper_fails_clearly(self) -> None:
        res = _run_step(
            _wallet_executor(None),
            {"id": "s", "action": "wallet_deposit", "params": {"amount": 1}},
        )
        assert not res.success and "not configured" in res.error

    def test_helper_error_is_surfaced_as_step_failure(self) -> None:
        w = _FakeWallet(raise_on="deposit")
        res = _run_step(
            _wallet_executor(w),
            {"id": "s", "action": "wallet_deposit", "params": {"amount": 1, "currency": "TRY"}},
        )
        assert not res.success and "playsite boom" in res.error

    def test_set_forwards_target_and_currency(self) -> None:
        w = _FakeWallet()
        res = _run_step(
            _wallet_executor(w),
            {"id": "s", "action": "wallet_set", "params": {"amount": 0, "currency": "TRY"}},
        )
        assert res.success and res.result == {"currency": "TRY", "balance": 0}
        assert w.calls == [("set", 0, "TRY")]

    def test_set_requires_amount(self) -> None:
        w = _FakeWallet()
        res = _run_step(_wallet_executor(w), {"id": "s", "action": "wallet_set", "params": {}})
        assert not res.success and "target balance" in res.error
        assert w.calls == []

    def test_set_resolves_saved_var_for_restore(self) -> None:
        w = _FakeWallet()
        ex = _wallet_executor(w)
        ex.vars["orig_balance"] = 1234.5  # captured by a wallet_balance step in setup
        res = _run_step(
            ex,
            {"id": "s", "action": "wallet_set", "params": {"amount": "$orig_balance", "currency": "TRY"}},
        )
        assert res.success
        assert w.calls == [("set", 1234.5, "TRY")]

    def test_deposit_resolves_saved_var_amount(self) -> None:
        w = _FakeWallet()
        ex = _wallet_executor(w)
        ex.vars["topup"] = 500
        res = _run_step(
            ex,
            {"id": "s", "action": "wallet_deposit", "params": {"amount": "$topup", "currency": "TRY"}},
        )
        assert res.success and w.calls == [("deposit", 500, "TRY")]

    def test_set_missing_var_fails_before_helper(self) -> None:
        w = _FakeWallet()
        res = _run_step(
            _wallet_executor(w),
            {"id": "s", "action": "wallet_set", "params": {"amount": "$nope"}},
        )
        assert not res.success and "nope" in res.error
        assert w.calls == []
