"""
Tests for framework/helpers/bridge.py — Playwright bridge wrappers.

Acceptance criteria:
- Calling a bridge action method executes the corresponding page.evaluate()
  call with the correct JavaScript expression and returns the result.
- When the game bridge is not registered on the page, calling any method
  raises a descriptive error indicating the bridge is missing.
- Event drain method returns a Python list of dictionaries parsed from the
  JavaScript bridge response.
- Wait-for-state method polls at intervals and raises a timeout error if the
  target state is not reached within the timeout.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from framework.helpers.bridge import BridgeHelper, BridgeMissingError, BridgeTimeoutError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page(evaluate_return=None) -> MagicMock:
    """
    Return a mock Playwright Page whose evaluate() resolves immediately.

    The bridge helper calls evaluate() twice per public method: once to check
    bridge presence (returns truthy) and once for the actual JS expression.
    We route the presence check to True and all subsequent calls to
    ``evaluate_return``.
    """
    page = MagicMock()

    async def _evaluate(expr: str, *args, **kwargs):
        # Presence check expression contains the typeof guard
        if "typeof" in expr:
            return True
        return evaluate_return

    page.evaluate = AsyncMock(side_effect=_evaluate)
    return page


def _make_page_no_bridge() -> MagicMock:
    """Return a mock Page where __gameTestBridge is not registered (returns None)."""
    page = MagicMock()

    async def _evaluate_no_bridge(expr: str, *args, **kwargs):
        # Presence check returns None — bridge absent
        if "typeof" in expr:
            return None
        return None

    page.evaluate = AsyncMock(side_effect=_evaluate_no_bridge)
    return page


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestBridgeConstruction:
    def test_bridge_stores_page(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        assert bridge.page is page


# ---------------------------------------------------------------------------
# Actions — correct JS expression
# ---------------------------------------------------------------------------


class TestBridgeActions:
    """Each action must call page.evaluate with the correct JS expression."""

    @pytest.mark.asyncio
    async def test_spin_evaluates_spin_call(self) -> None:
        page = _make_page(evaluate_return=None)
        bridge = BridgeHelper(page)
        await bridge.spin()
        # _eval() makes 2 evaluate calls: presence check + actual call.
        assert page.evaluate.await_count == 2
        # The last call is the actual bridge call.
        expr = page.evaluate.call_args[0][0]
        assert "spin()" in expr
        assert "__gameTestBridge" in expr

    @pytest.mark.asyncio
    async def test_stop_spin_evaluates_stop_spin_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.stop_spin()
        expr = page.evaluate.call_args[0][0]
        assert "stopSpin()" in expr

    @pytest.mark.asyncio
    async def test_next_bet_evaluates_next_bet_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.next_bet()
        expr = page.evaluate.call_args[0][0]
        assert "nextBet()" in expr

    @pytest.mark.asyncio
    async def test_previous_bet_evaluates_previous_bet_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.previous_bet()
        expr = page.evaluate.call_args[0][0]
        assert "previousBet()" in expr

    @pytest.mark.asyncio
    async def test_select_bet_passes_index(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.select_bet(3)
        expr = page.evaluate.call_args[0][0]
        assert "selectBet(3)" in expr

    @pytest.mark.asyncio
    async def test_open_auto_popup_evaluates_correct_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.open_auto_popup()
        expr = page.evaluate.call_args[0][0]
        assert "openAutoPopup()" in expr

    @pytest.mark.asyncio
    async def test_start_auto_spin_passes_count_and_stop_by_win(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.start_auto_spin(count=20, stop_by_win=True)
        expr = page.evaluate.call_args[0][0]
        assert "startAutoSpin(20, true)" in expr

    @pytest.mark.asyncio
    async def test_start_auto_spin_false_stop_by_win(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.start_auto_spin(count=10, stop_by_win=False)
        expr = page.evaluate.call_args[0][0]
        assert "startAutoSpin(10, false)" in expr

    @pytest.mark.asyncio
    async def test_stop_auto_spin_evaluates_correct_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.stop_auto_spin()
        expr = page.evaluate.call_args[0][0]
        assert "stopAutoSpin()" in expr

    @pytest.mark.asyncio
    async def test_open_buy_bonus_popup_evaluates_correct_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.open_buy_bonus_popup()
        expr = page.evaluate.call_args[0][0]
        assert "openBuyBonusPopup()" in expr

    @pytest.mark.asyncio
    async def test_confirm_buy_bonus_passes_package_index(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.confirm_buy_bonus(1)
        expr = page.evaluate.call_args[0][0]
        assert "confirmBuyBonus(1)" in expr

    @pytest.mark.asyncio
    async def test_set_turbo_true_passes_correct_value(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.set_turbo(True)
        expr = page.evaluate.call_args[0][0]
        assert "setTurbo(true)" in expr

    @pytest.mark.asyncio
    async def test_set_turbo_false_passes_correct_value(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.set_turbo(False)
        expr = page.evaluate.call_args[0][0]
        assert "setTurbo(false)" in expr

    @pytest.mark.asyncio
    async def test_set_sound_evaluates_correct_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.set_sound(True)
        expr = page.evaluate.call_args[0][0]
        assert "setSound(true)" in expr

    @pytest.mark.asyncio
    async def test_set_dev_mode_result_passes_grid(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        grid = [[9, 9, 9], [1, 1, 1], [4, 4, 4]]
        await bridge.set_dev_mode_result(grid)
        expr = page.evaluate.call_args[0][0]
        assert "setDevModeResult" in expr
        assert "[[9,9,9],[1,1,1],[4,4,4]]" in expr


# ---------------------------------------------------------------------------
# Queries — correct JS expression and return value passthrough
# ---------------------------------------------------------------------------


class TestBridgeQueries:
    @pytest.mark.asyncio
    async def test_get_app_state_returns_string(self) -> None:
        page = _make_page(evaluate_return="idle")
        bridge = BridgeHelper(page)
        result = await bridge.get_app_state()
        assert result == "idle"
        expr = page.evaluate.call_args[0][0]
        assert "getAppState()" in expr

    @pytest.mark.asyncio
    async def test_get_balance_returns_number(self) -> None:
        page = _make_page(evaluate_return=1000.0)
        bridge = BridgeHelper(page)
        result = await bridge.get_balance()
        assert result == 1000.0
        expr = page.evaluate.call_args[0][0]
        assert "getBalance()" in expr

    @pytest.mark.asyncio
    async def test_get_reel_result_returns_grid(self) -> None:
        grid = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        page = _make_page(evaluate_return=grid)
        bridge = BridgeHelper(page)
        result = await bridge.get_reel_result()
        assert result == grid

    @pytest.mark.asyncio
    async def test_get_snapshot_returns_dict(self) -> None:
        snapshot = {"appState": "idle", "balance": 500.0}
        page = _make_page(evaluate_return=snapshot)
        bridge = BridgeHelper(page)
        result = await bridge.get_snapshot()
        assert result == snapshot
        expr = page.evaluate.call_args[0][0]
        assert "getSnapshot()" in expr

    @pytest.mark.asyncio
    async def test_get_spin_state_evaluates_correct_call(self) -> None:
        page = _make_page(evaluate_return="NORMAL")
        bridge = BridgeHelper(page)
        result = await bridge.get_spin_state()
        assert result == "NORMAL"
        expr = page.evaluate.call_args[0][0]
        assert "getSpinState()" in expr

    @pytest.mark.asyncio
    async def test_get_free_spins_remaining_evaluates_correct_call(self) -> None:
        page = _make_page(evaluate_return=3)
        bridge = BridgeHelper(page)
        result = await bridge.get_free_spins_remaining()
        assert result == 3

    @pytest.mark.asyncio
    async def test_is_spin_enabled_returns_bool(self) -> None:
        page = _make_page(evaluate_return=True)
        bridge = BridgeHelper(page)
        result = await bridge.is_spin_enabled()
        assert result is True


# ---------------------------------------------------------------------------
# Bridge missing — descriptive error
# ---------------------------------------------------------------------------


class TestBridgeMissing:
    @pytest.mark.asyncio
    async def test_spin_raises_bridge_missing_when_bridge_not_registered(self) -> None:
        """When page.evaluate returns None (bridge absent), raise BridgeMissingError."""
        page = _make_page_no_bridge()
        bridge = BridgeHelper(page)
        with pytest.raises(BridgeMissingError) as exc_info:
            await bridge.spin()
        assert "bridge" in str(exc_info.value).lower()
        assert "__gameTestBridge" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_balance_raises_bridge_missing_when_bridge_not_registered(
        self,
    ) -> None:
        page = _make_page_no_bridge()
        bridge = BridgeHelper(page)
        with pytest.raises(BridgeMissingError):
            await bridge.get_balance()

    @pytest.mark.asyncio
    async def test_error_message_names_missing_bridge(self) -> None:
        """Error message must clearly identify that __gameTestBridge is missing."""
        page = _make_page_no_bridge()
        bridge = BridgeHelper(page)
        with pytest.raises(BridgeMissingError) as exc_info:
            await bridge.get_snapshot()
        msg = str(exc_info.value)
        assert "__gameTestBridge" in msg


# ---------------------------------------------------------------------------
# Event collection
# ---------------------------------------------------------------------------


class TestEventCollection:
    @pytest.mark.asyncio
    async def test_start_event_collection_evaluates_correct_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.start_event_collection()
        expr = page.evaluate.call_args[0][0]
        assert "startEventCollection()" in expr

    @pytest.mark.asyncio
    async def test_stop_event_collection_evaluates_correct_call(self) -> None:
        page = _make_page()
        bridge = BridgeHelper(page)
        await bridge.stop_event_collection()
        expr = page.evaluate.call_args[0][0]
        assert "stopEventCollection()" in expr

    @pytest.mark.asyncio
    async def test_drain_events_returns_list_of_dicts(self) -> None:
        """drainEvents JS response is parsed into a Python list of dicts."""
        js_response = [
            {"t": 0.123, "type": "state_change", "data": {"from": "betting", "to": "idle"}},
            {"t": 0.456, "type": "spin_result", "data": {"payout": 10.0}},
        ]
        page = _make_page(evaluate_return=js_response)
        bridge = BridgeHelper(page)
        result = await bridge.drain_events()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "state_change"
        assert result[1]["data"]["payout"] == 10.0

    @pytest.mark.asyncio
    async def test_drain_events_returns_empty_list_when_no_events(self) -> None:
        page = _make_page(evaluate_return=[])
        bridge = BridgeHelper(page)
        result = await bridge.drain_events()
        assert result == []

    @pytest.mark.asyncio
    async def test_drain_events_evaluates_correct_call(self) -> None:
        page = _make_page(evaluate_return=[])
        bridge = BridgeHelper(page)
        await bridge.drain_events()
        expr = page.evaluate.call_args[0][0]
        assert "drainEvents()" in expr


# ---------------------------------------------------------------------------
# Wait helpers
# ---------------------------------------------------------------------------


class TestWaitForState:
    @pytest.mark.asyncio
    async def test_wait_for_state_returns_when_state_matches_immediately(self) -> None:
        page = _make_page(evaluate_return="idle")
        bridge = BridgeHelper(page)
        # Should complete without raising
        await bridge.wait_for_state("idle", timeout_ms=5000, poll_interval_ms=50)

    @pytest.mark.asyncio
    async def test_wait_for_state_polls_until_state_changes(self) -> None:
        """Returns 'betting' twice then 'idle' — should resolve on third getAppState call."""
        # _eval() calls evaluate twice per invocation: presence check + actual call.
        # Pattern: [presence=True, state='betting'] x2, then [presence=True, state='idle']
        # We use a stateful side-effect that returns True for typeof checks and cycles
        # through state values for the actual getAppState call.
        state_responses = iter(["betting", "betting", "idle"])

        async def _evaluate(expr: str, *args, **kwargs):
            if "typeof" in expr:
                return True
            return next(state_responses)

        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=_evaluate)
        bridge = BridgeHelper(page)
        await bridge.wait_for_state("idle", timeout_ms=5000, poll_interval_ms=1)
        # 3 successful getAppState calls = 6 evaluate() calls (presence + actual each time)
        assert page.evaluate.await_count == 6

    @pytest.mark.asyncio
    async def test_wait_for_state_raises_timeout_error(self) -> None:
        """State never becomes 'idle' — should raise BridgeTimeoutError."""
        page = _make_page(evaluate_return="betting")
        bridge = BridgeHelper(page)
        with pytest.raises(BridgeTimeoutError) as exc_info:
            await bridge.wait_for_state("idle", timeout_ms=50, poll_interval_ms=10)
        assert "idle" in str(exc_info.value)
        # "Timed out" contains "timed" which covers the timeout concept
        assert "timed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_wait_for_idle_is_shorthand_for_idle_state(self) -> None:
        page = _make_page(evaluate_return="idle")
        bridge = BridgeHelper(page)
        await bridge.wait_for_idle(timeout_ms=1000)
        expr = page.evaluate.call_args[0][0]
        assert "getAppState()" in expr
