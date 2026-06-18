"""
Playwright-side bridge helper for the Zeus game test bridge.

Wraps ``page.evaluate()`` calls to the game's ``window.__gameTestBridge``
object, which is registered when the game URL includes ``?bridge=1``.

Public interface
----------------
BridgeHelper(page)
    Wraps a Playwright Page. All methods are async and mirror the bridge API.

BridgeMissingError
    Raised when ``window.__gameTestBridge`` is not registered on the page.
    Indicates the game was loaded without ``?bridge=1`` or before the bridge
    registered itself.

BridgeTimeoutError
    Raised by ``wait_for_state`` / ``wait_for_idle`` when the target app state
    is not reached within the given timeout.

Bridge API reference: games/zeus/bridge-api.md — this wrapper mirrors the spec's
Actions, Queries, Wait helpers, and the §5 named event-capture + Hold & Spin
analyser API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# JS expression that evaluates to the bridge or raises if absent.
_BRIDGE = "window.__gameTestBridge"

# Sentinel: evaluate("window.__gameTestBridge") returns this when the bridge
# object is not on the window (undefined serialises as None in Playwright).
_BRIDGE_ABSENT = None


class BridgeMissingError(RuntimeError):
    """Raised when ``window.__gameTestBridge`` is not registered on the page."""


class BridgeTimeoutError(TimeoutError):
    """Raised when ``wait_for_state`` does not reach the target state in time."""


class BridgeHelper:
    """
    Python wrappers for all bridge actions, queries, waits, and event
    collection methods.

    Parameters
    ----------
    page:
        A Playwright ``Page`` object that has the game loaded with ``?bridge=1``.
    """

    def __init__(self, page: Any) -> None:
        self.page = page

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _eval(self, expr: str) -> Any:
        """
        Evaluate *expr* against the page and return the result.

        First verifies that the bridge is registered; if not, raises
        ``BridgeMissingError``.
        """
        # Check bridge presence before any call so the error message is always
        # descriptive regardless of which method was invoked.
        presence = await self.page.evaluate(f"typeof {_BRIDGE} !== 'undefined' && {_BRIDGE} !== null ? true : null")
        if presence is None:
            raise BridgeMissingError(
                f"window.__gameTestBridge is not registered on the page. "
                f"Ensure the game URL includes '?bridge=1' and that the bridge "
                f"has finished initialising before calling bridge methods."
            )
        return await self.page.evaluate(expr)

    async def _eval_arg(self, js_fn: str, arg: Any) -> Any:
        """Evaluate a one-argument JS function against the page.

        For bridge methods that take *data* (e.g. ``filterBonusEvent(events)`` and
        the Hold & Spin analysers) — Playwright JSON-serialises ``arg`` and passes
        it as the function's single parameter. Verifies bridge presence first, like
        :meth:`_eval`.
        """
        presence = await self.page.evaluate(f"typeof {_BRIDGE} !== 'undefined' && {_BRIDGE} !== null ? true : null")
        if presence is None:
            raise BridgeMissingError(
                "window.__gameTestBridge is not registered on the page. "
                "Ensure the game URL includes '?bridge=1'."
            )
        return await self.page.evaluate(js_fn, arg)

    @staticmethod
    def _js_bool(value: bool) -> str:
        return "true" if value else "false"

    # ------------------------------------------------------------------
    # Actions — emulate real user interactions
    # ------------------------------------------------------------------

    async def start_game(self) -> None:
        """Emulate user clicking the Start button. Resolves when AppState reaches idle."""
        await self._eval(f"{_BRIDGE}.startGame()")

    async def dismiss_popups(self) -> list[str]:
        """Dismiss all currently visible popups; return the names dismissed (bridge-api §1.2)."""
        result = await self._eval(f"{_BRIDGE}.dismissPopups()")
        return list(result) if result else []

    async def is_popup_visible(self) -> bool:
        """Return whether any popup is currently blocking the game (bridge-api §1.3)."""
        return await self._eval(f"{_BRIDGE}.isPopupVisible()")

    async def spin(self, timeout_ms: int | None = None) -> None:
        """Emulate user touching the spin button.

        Per the bridge contract (bridge-api §2.1) ``spin()`` is **fire-and-return** —
        it triggers the click and does NOT wait for the round to settle. When
        ``timeout_ms`` is given this instead performs the *spin-and-wait* composite
        via ``waitForSpinComplete(timeout_ms)``, so a step written
        ``{name: spin, timeoutMs: N}`` blocks through the whole round (including a
        triggered Hold & Spin bonus). Omit ``timeout_ms`` and compose your own wait
        (``spin()`` then ``wait_for_idle()``) when you need to observe intermediate
        states.
        """
        if timeout_ms is not None:
            await self._eval(f"{_BRIDGE}.waitForSpinComplete({int(timeout_ms)})")
        else:
            await self._eval(f"{_BRIDGE}.spin()")

    async def stop_spin(self) -> None:
        """Emulate user touching the stop button during result animation."""
        await self._eval(f"{_BRIDGE}.stopSpin()")

    async def next_bet(self) -> None:
        """Emulate user clicking the next bet button."""
        await self._eval(f"{_BRIDGE}.nextBet()")

    async def previous_bet(self) -> None:
        """Emulate user clicking the previous bet button."""
        await self._eval(f"{_BRIDGE}.previousBet()")

    async def select_bet(self, index: int) -> None:
        """
        Emulate user selecting a denomination at the given zero-based *index*.
        """
        await self._eval(f"{_BRIDGE}.selectBet({index})")

    async def open_auto_popup(self) -> None:
        """Emulate user clicking the auto button to open the auto-spin popup."""
        await self._eval(f"{_BRIDGE}.openAutoPopup()")

    async def select_auto_spin_package(self, count: int | float = 10) -> None:
        """Tap a package-count button (10/20/30/50/Infinity) in the open auto popup
        (bridge-api §2.7.1). Default 10 — the smallest package."""
        js_count = "Infinity" if count == float("inf") else str(int(count))
        await self._eval(f"{_BRIDGE}.selectAutoSpinPackage({js_count})")

    async def toggle_auto_spin_stop_at_win(self, enabled: bool) -> None:
        """Tap the 'stop at any win' toggle in the open auto popup (bridge-api §2.7.2)."""
        await self._eval(f"{_BRIDGE}.toggleAutoSpinStopAtWin({self._js_bool(enabled)})")

    async def confirm_auto_spin(self) -> None:
        """Tap Confirm in the open auto popup; the first auto-spin fires immediately
        with no intervening IDLE (bridge-api §2.7.3)."""
        await self._eval(f"{_BRIDGE}.confirmAutoSpin()")

    async def start_auto_spin(
        self, count: int | float, stop_by_win: bool, delay_ms: int | None = None
    ) -> None:
        """Composite (bridge-api §2.7.4): select the package, set the stop-at-win
        toggle, and confirm in one call. **Async** — each inner tap is separated by
        ``delay_ms`` (JS default 500ms) so the package-select tween plays and the
        event recorder captures each as a distinct frame; pass ``delay_ms=0`` to fire
        all three in the same tick.

        Parameters
        ----------
        count:
            Number of auto spins (``math.inf`` / ``float('inf')`` for continuous).
        stop_by_win:
            Whether to stop auto-spin on any payout > 0.
        delay_ms:
            Pacing between the three inner taps. ``None`` uses the JS default (500ms).
        """
        js_count = "Infinity" if count == float("inf") else str(int(count))
        js_stop = self._js_bool(stop_by_win)
        if delay_ms is not None:
            await self._eval(f"{_BRIDGE}.startAutoSpin({js_count}, {js_stop}, {int(delay_ms)})")
        else:
            await self._eval(f"{_BRIDGE}.startAutoSpin({js_count}, {js_stop})")

    async def stop_auto_spin(self) -> None:
        """Emulate user clicking the stop auto button."""
        await self._eval(f"{_BRIDGE}.stopAutoSpin()")

    async def open_buy_bonus_popup(self) -> None:
        """Emulate user clicking the buy bonus button."""
        await self._eval(f"{_BRIDGE}.openBuyBonusPopup()")

    async def select_buy_bonus_package(self, index: int = 0) -> None:
        """Tap a package toggle (0=Casual, 1=Bold, 2=Epic) in the open buy-bonus
        popup (bridge-api §2.10.1). Default 0 — the cheapest package."""
        await self._eval(f"{_BRIDGE}.selectBuyBonusPackage({int(index)})")

    async def increase_buy_bonus_bet(self) -> None:
        """Tap the next-bet arrow inside the buy-bonus popup (bridge-api §2.10.2).
        No-op at max bet (button auto-disabled)."""
        await self._eval(f"{_BRIDGE}.increaseBuyBonusBet()")

    async def decrease_buy_bonus_bet(self) -> None:
        """Tap the previous-bet arrow inside the buy-bonus popup (bridge-api §2.10.3).
        No-op at min bet (button auto-disabled)."""
        await self._eval(f"{_BRIDGE}.decreaseBuyBonusBet()")

    async def confirm_buy_bonus(self, package_index: int, delay_ms: int | None = None) -> None:
        """Composite (bridge-api §2.10.4): select the package, tap Buy (surfacing the
        confirmation sub-popup), then tap Confirm. Resolves when bonus mode completes
        and the game returns to idle.

        Parameters
        ----------
        package_index:
            ``0`` = Casual, ``1`` = Bold, ``2`` = Epic.
        delay_ms:
            Pacing between the inner taps for replay distinguishability. ``None`` uses
            the JS default (500ms); pass ``0`` to fire same-tick.
        """
        if delay_ms is not None:
            await self._eval(f"{_BRIDGE}.confirmBuyBonus({int(package_index)}, {int(delay_ms)})")
        else:
            await self._eval(f"{_BRIDGE}.confirmBuyBonus({int(package_index)})")

    async def set_turbo(self, enabled: bool) -> None:
        """Emulate user clicking the turbo toggle."""
        await self._eval(f"{_BRIDGE}.setTurbo({self._js_bool(enabled)})")

    async def set_sound(self, enabled: bool) -> None:
        """Emulate user clicking the sound on/off button (controls both SFX and BGM)."""
        await self._eval(f"{_BRIDGE}.setSound({self._js_bool(enabled)})")

    async def open_settings(self) -> None:
        """Emulate user clicking the settings button."""
        await self._eval(f"{_BRIDGE}.openSettings()")

    async def open_info(self) -> None:
        """Emulate user clicking the info/paytable button."""
        await self._eval(f"{_BRIDGE}.openInfo()")

    async def open_history(self) -> None:
        """Emulate user clicking the history button."""
        await self._eval(f"{_BRIDGE}.openHistory()")

    async def hold_spin_start(self) -> None:
        """Emulate user holding the space bar or long-pressing spin during bonus mode."""
        await self._eval(f"{_BRIDGE}.holdSpinStart()")

    async def hold_spin_release(self) -> None:
        """Emulate user releasing the hold spin button."""
        await self._eval(f"{_BRIDGE}.holdSpinRelease()")

    async def skip_bonus_transition(self) -> None:
        """Emulate user clicking skip during the Hold&Spin entry animation."""
        await self._eval(f"{_BRIDGE}.skipBonusTransition()")

    async def skip_big_win(self) -> None:
        """Emulate user pressing space during the big win celebration popup."""
        await self._eval(f"{_BRIDGE}.skipBigWin()")

    async def skip_total_win(self) -> None:
        """Emulate user acknowledging the Hold&Spin end-of-bonus total-win popup.

        Self-asserting (like ``skip_big_win``): throws ``skipTotalWin() not
        applicable: no_totalwin`` if no total-win popup is showing. Fires the
        popup's skip handler fire-and-forget, so the caller should immediately
        ``wait_for_idle()`` to land back in the base game. This is what actually
        runs the bonus-result → transitionToBaseGame handler — a plain
        ``dismiss_popups`` hides the popup without resuming the game. See the
        required post-bonus sequence in bridge-api.md §4.
        """
        await self._eval(f"{_BRIDGE}.skipTotalWin()")

    async def open_dev_mode(self) -> None:
        """Emulate user clicking the dev mode button (requires env=dev in URL)."""
        await self._eval(f"{_BRIDGE}.openDevMode()")

    async def set_dev_mode_result(self, grid: list[list[int]]) -> None:
        """
        Configure a 3x3 result grid via DevModeManager for the next spin.

        Parameters
        ----------
        grid:
            3x3 nested list of symbol IDs (see bridge-api.md Section 7).
            Example: ``[[9, 9, 9], [1, 1, 1], [4, 4, 4]]``
        """
        # Serialise without spaces so the JS inline string stays compact.
        js_grid = json.dumps(grid, separators=(",", ":"))
        await self._eval(f"{_BRIDGE}.setDevModeResult({js_grid})")

    async def open_trophy(self) -> None:
        """Emulate user clicking the trophy button (available after mega/ultra wins)."""
        await self._eval(f"{_BRIDGE}.openTrophy()")

    # ------------------------------------------------------------------
    # Queries — read-only state inspection
    # ------------------------------------------------------------------

    async def get_app_state(self) -> str:
        """Return the current AppState string (e.g. ``'idle'``, ``'betting'``)."""
        return await self._eval(f"{_BRIDGE}.getAppState()")

    async def get_spin_state(self) -> str:
        """Return the current SpinState (e.g. ``'NORMAL'``, ``'IN_BONUS'``)."""
        return await self._eval(f"{_BRIDGE}.getSpinState()")

    async def get_balance(self) -> float:
        """Return the current player balance."""
        return await self._eval(f"{_BRIDGE}.getBalance()")

    async def get_bet_amount(self) -> float:
        """Return the current bet amount."""
        return await self._eval(f"{_BRIDGE}.getBetAmount()")

    async def get_bet_index(self) -> int:
        """Return the current bet index into the denominations array."""
        return await self._eval(f"{_BRIDGE}.getBetIndex()")

    async def get_denominations(self) -> list[float]:
        """Return all available bet levels."""
        return await self._eval(f"{_BRIDGE}.getDenominations()")

    async def get_free_spins_remaining(self) -> int:
        """Return remaining free spins in bonus mode."""
        return await self._eval(f"{_BRIDGE}.getFreeSpinsRemaining()")

    async def get_auto_spins_remaining(self) -> int | float:
        """Return remaining auto spins (``math.inf`` possible for continuous mode)."""
        return await self._eval(f"{_BRIDGE}.getAutoSpinsRemaining()")

    async def get_last_payout(self) -> float:
        """Return the payout from the last completed spin."""
        return await self._eval(f"{_BRIDGE}.getLastPayout()")

    async def get_last_win_type(self) -> str:
        """Return the win type from the last spin (e.g. ``'LOSE'``, ``'BIG_WIN'``)."""
        return await self._eval(f"{_BRIDGE}.getLastWinType()")

    async def is_turbo_active(self) -> bool:
        """Return whether turbo mode is currently enabled."""
        return await self._eval(f"{_BRIDGE}.isTurboActive()")

    async def is_sound_enabled(self) -> bool:
        """Return whether sound effects are enabled."""
        return await self._eval(f"{_BRIDGE}.isSoundEnabled()")

    async def is_music_enabled(self) -> bool:
        """Return whether background music is enabled."""
        return await self._eval(f"{_BRIDGE}.isMusicEnabled()")

    async def is_promo_mode(self) -> bool:
        """Return whether a promotional game is currently active."""
        return await self._eval(f"{_BRIDGE}.isPromoMode()")

    async def get_reel_result(self) -> list[list[int]]:
        """Return the 3x3 grid of symbol IDs from the last server response."""
        return await self._eval(f"{_BRIDGE}.getReelResult()")

    async def get_coin_positions(self) -> list[dict]:
        """Return coins on the grid with ``{row, col, coinValue, type}``."""
        return await self._eval(f"{_BRIDGE}.getCoinPositions()")

    async def get_paylines(self) -> list[dict]:
        """Return winning lines with ``{symbols, payline, payoutRate, winType}``."""
        return await self._eval(f"{_BRIDGE}.getPaylines()")

    # -- On-screen geometry (bridge-api.md §3.7) ---------------------------

    async def get_button_rect(self, name: str) -> dict | None:
        """Return a named button's on-screen rectangle in page CSS pixels.

        The returned dict has ``x/y/width/height`` (top-left origin),
        ``centerX/centerY`` (pass to ``page.mouse.click``), ``visible`` (renderable
        and on-canvas) and ``interactable``. Returns ``None`` when *name* is unknown
        or its owning popup is closed. Names: ``spin``, ``stop``, ``auto``,
        ``buyBonus``, ``nextBet``, ``previousBet``, ``trophy`` (always) plus popup
        element names while that popup is open (e.g. ``bet:row:2``, ``auto:package:0``).
        """
        return await self._eval(f"{_BRIDGE}.getButtonRect({json.dumps(name)})")

    async def get_all_button_rects(self) -> dict[str, dict]:
        """Return all currently-resolvable button rects keyed by name.

        Includes the main-screen buttons plus the element rects of whichever popups
        are currently open. See :meth:`get_button_rect` for the rect shape.
        """
        return await self._eval(f"{_BRIDGE}.getAllButtonRects()")

    async def is_spin_enabled(self) -> bool:
        """Return whether the spin button is interactable."""
        return await self._eval(f"{_BRIDGE}.isSpinEnabled()")

    async def is_buy_bonus_enabled(self) -> bool:
        """Return whether the buy bonus button is interactable."""
        return await self._eval(f"{_BRIDGE}.isBuyBonusEnabled()")

    async def is_auto_enabled(self) -> bool:
        """Return whether the auto button is interactable."""
        return await self._eval(f"{_BRIDGE}.isAutoEnabled()")

    async def is_next_bet_enabled(self) -> bool:
        """Return whether the next bet button is interactable."""
        return await self._eval(f"{_BRIDGE}.isNextBetEnabled()")

    async def is_previous_bet_enabled(self) -> bool:
        """Return whether the previous bet button is interactable."""
        return await self._eval(f"{_BRIDGE}.isPreviousBetEnabled()")

    async def is_stop_auto_visible(self) -> bool:
        """Return whether the stop-auto button is currently visible."""
        return await self._eval(f"{_BRIDGE}.isStopAutoVisible()")

    async def get_game_info(self) -> dict:
        """Return the full server game config (GameInfo interface)."""
        return await self._eval(f"{_BRIDGE}.getGameInfo()")

    async def get_currency(self) -> str:
        """Return the currency code (e.g. ``'USD'``)."""
        return await self._eval(f"{_BRIDGE}.getCurrency()")

    async def get_paytable(self) -> list[dict]:
        """Return symbol payout rates."""
        return await self._eval(f"{_BRIDGE}.getPaytable()")

    async def get_free_spin_packages(self) -> list[dict]:
        """Return available buy-bonus packages with ``{name, freeSpins, price}``."""
        return await self._eval(f"{_BRIDGE}.getFreeSpinPackages()")

    async def get_display_balance(self) -> str:
        """Return the formatted balance string shown to the player."""
        return await self._eval(f"{_BRIDGE}.getDisplayBalance()")

    async def get_display_bet(self) -> str:
        """Return the formatted bet string shown to the player."""
        return await self._eval(f"{_BRIDGE}.getDisplayBet()")

    async def get_display_win(self) -> str:
        """Return the formatted win string shown to the player."""
        return await self._eval(f"{_BRIDGE}.getDisplayWin()")

    async def get_snapshot(self) -> dict:
        """Return a full state dump (all key fields in one call)."""
        return await self._eval(f"{_BRIDGE}.getSnapshot()")

    # ------------------------------------------------------------------
    # Event collection — replay/timeline buffer used by the suite runner.
    # Distinct from the named-capture API below (bridge-api §5): this is the
    # single global collector the runner drains per test for the timeline.
    # ------------------------------------------------------------------

    async def start_event_collection(self) -> None:
        """Start capturing game events with timestamps."""
        await self._eval(f"{_BRIDGE}.startEventCollection()")

    async def stop_event_collection(self) -> None:
        """Stop capturing events and unregister hooks."""
        await self._eval(f"{_BRIDGE}.stopEventCollection()")

    async def drain_events(self) -> list[dict]:
        """
        Return all collected events since the last drain and clear the buffer.

        Returns
        -------
        list[dict]
            Each dict has ``t`` (float seconds), ``type`` (str), and
            ``data`` (dict) fields matching the ``CollectedEvent`` interface.
        """
        result = await self._eval(f"{_BRIDGE}.drainEvents()")
        # Playwright returns JS arrays as Python lists of dicts already.
        # Guard against None in case of empty bridge response.
        if result is None:
            return []
        return list(result)

    # ------------------------------------------------------------------
    # Named event capture & Hold & Spin analysers (bridge-api §5)
    # ------------------------------------------------------------------

    async def start_event_capture(self, label: str) -> None:
        """Begin a named capture buffer keyed by ``label`` (bridge-api §5.1).

        Multiple labels can run concurrently. Calling with an existing label
        deliberately **resets** that label's buffer.
        """
        await self._eval(f"{_BRIDGE}.startEventCapture({json.dumps(label)})")

    async def end_event_capture(self, label: str) -> list[dict]:
        """Stop the named capture and return its ``CollectedEvent[]`` (bridge-api §5.1).

        Returns ``[]`` if no capture under that label is active (idempotent).
        """
        result = await self._eval(f"{_BRIDGE}.endEventCapture({json.dumps(label)})")
        return list(result) if result else []

    async def filter_bonus_event(self, events: list[dict]) -> list[dict]:
        """Pure: slice a captured stream to the ``START_BONUS``..``END_BONUS`` episode
        (bridge-api §5.2). Returns ``[]`` if no ``START_BONUS`` is present in input."""
        result = await self._eval_arg(f"(ev) => {_BRIDGE}.filterBonusEvent(ev)", events)
        return list(result) if result else []

    async def capture_hold_spin_bonus(
        self,
        max_spins: int | None = None,
        timeout_ms: int | None = None,
        per_spin_timeout_ms: int | None = None,
    ) -> dict:
        """Composite recorder (bridge-api §5.2): spin until a Hold & Spin bonus
        triggers, let it play out, and return the episode as
        ``{triggered, spinsToTrigger, events, respinCount}``."""
        opts = self._spin_until_options(max_spins, timeout_ms, per_spin_timeout_ms)
        return await self._eval_arg(f"(o) => {_BRIDGE}.captureHoldSpinBonus(o)", opts)

    async def verify_hold_spin_coins_held(self, events: list[dict]) -> dict:
        """Analyser (bridge-api §5.3): every coin in event N is still at the same
        ``(col, row)`` in event N+1. Returns ``{valid, bonusFound, respinCount, violations}``."""
        return await self._eval_arg(f"(ev) => {_BRIDGE}.verifyHoldSpinCoinsHeld(ev)", events)

    async def verify_hold_spin_freespin_reset_on_new_coin(self, events: list[dict]) -> dict:
        """Analyser (bridge-api §5.3): a re-spin that lands new coins resets freespins to 3."""
        return await self._eval_arg(
            f"(ev) => {_BRIDGE}.verifyHoldSpinFreespinResetOnNewCoin(ev)", events
        )

    async def verify_hold_spin_freespin_count_down(self, events: list[dict]) -> dict:
        """Analyser (bridge-api §5.3): a re-spin with no new coin decrements freespins by 1."""
        return await self._eval_arg(
            f"(ev) => {_BRIDGE}.verifyHoldSpinFreespinCountDown(ev)", events
        )

    @staticmethod
    def _spin_until_options(
        max_spins: int | None, timeout_ms: int | None, per_spin_timeout_ms: int | None
    ) -> dict:
        """Build the camelCase ``SpinUntilOptions`` dict, omitting unset keys so the
        bridge applies its own defaults (bridge-api §4.1 / §12)."""
        opts: dict[str, Any] = {}
        if max_spins is not None:
            opts["maxSpins"] = max_spins
        if timeout_ms is not None:
            opts["timeoutMs"] = timeout_ms
        if per_spin_timeout_ms is not None:
            opts["perSpinTimeoutMs"] = per_spin_timeout_ms
        return opts

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def wait_for_state(
        self,
        state: str,
        timeout_ms: int = 30_000,
        poll_interval_ms: int = 200,
    ) -> None:
        """
        Poll ``getAppState()`` until it equals *state* or timeout elapses.

        Parameters
        ----------
        state:
            Target AppState string (e.g. ``'idle'``).
        timeout_ms:
            Maximum wait time in milliseconds (default 30 000).
        poll_interval_ms:
            How often to poll in milliseconds (default 200).

        Raises
        ------
        BridgeTimeoutError
            If the state is not reached within *timeout_ms*.
        """
        poll_s = poll_interval_ms / 1000.0
        elapsed_ms = 0
        current = "<bridge not registered>"

        while True:
            try:
                current = await self.get_app_state()
            except BridgeMissingError:
                # Bridge hasn't registered yet (e.g. game still loading).
                # Treat as "not at target state" and keep polling.
                pass
            else:
                if current == state:
                    return

            if elapsed_ms >= timeout_ms:
                raise BridgeTimeoutError(
                    f"Timed out after {timeout_ms}ms waiting for AppState='{state}'. "
                    f"Last observed state: '{current}'."
                )

            await asyncio.sleep(poll_s)
            elapsed_ms += poll_interval_ms

    async def wait_for_idle(
        self,
        timeout_ms: int = 30_000,
        poll_interval_ms: int = 200,
    ) -> None:
        """Shorthand for ``wait_for_state('idle')``."""
        await self.wait_for_state("idle", timeout_ms=timeout_ms, poll_interval_ms=poll_interval_ms)

    async def wait_for_spin_complete(self, timeout_ms: int = 60_000) -> None:
        """Fire a spin and wait for the whole round to settle (bridge-api §4).

        Waits for the entire animation, payout, transitions, and any triggered
        Hold & Spin bonus. Default 60s — bump to 120s+ when the spin may trigger a
        bonus. Delegates to the bridge's native ``waitForSpinComplete``.
        """
        await self._eval(f"{_BRIDGE}.waitForSpinComplete({int(timeout_ms)})")

    async def wait_for_ui_settle(self, delay_ms: int = 1000) -> None:
        """
        Wait for UI animations to catch up after game state reaches IDLE.

        The bridge state transitions to IDLE before UI animations complete
        (win payline highlight, spin button restore, etc.). This helper
        calls the game's waitForUISettle() which delays by *delay_ms*.
        """
        await self.page.evaluate(f"() => {_BRIDGE}.waitForUISettle({delay_ms})")

    async def wait_for_total_win_popup(self, timeout_ms: int | None = None) -> None:
        """Wait for the Hold&Spin end-of-bonus total-win popup to appear.

        When a Hold&Spin bonus ends the game parks in ``result`` showing a
        total-win popup and will NOT reach idle until it is acknowledged — so
        ``wait_for_idle()`` alone DEADLOCKS against the popup. The correct
        post-bonus sequence is ``wait_for_total_win_popup()`` →
        ``skip_total_win()`` → ``wait_for_idle()`` (bridge-api.md §4). The bridge
        polls ``isShowingTotalWin`` and rejects with the current AppState on
        timeout (bridge default 60s).
        """
        if timeout_ms is not None:
            await self._eval(f"{_BRIDGE}.waitForTotalWinPopup({timeout_ms})")
        else:
            await self._eval(f"{_BRIDGE}.waitForTotalWinPopup()")

    async def spin_until_bonus(
        self,
        max_spins: int | None = None,
        timeout_ms: int | None = None,
        per_spin_timeout_ms: int | None = None,
    ) -> dict:
        """Spin repeatedly until a Hold & Spin bonus triggers, resolving the **moment**
        bonus mode is entered — before the bonus plays out — so the caller can interact
        with it (bridge-api §4.1). Returns ``{spins, matched}`` (``matched`` is False if
        the spin/time budget ran out). Leaves the game inside the bonus.

        Does not catch promo-triggered bonuses; check :meth:`is_promo_mode` first.
        """
        opts = self._spin_until_options(max_spins, timeout_ms, per_spin_timeout_ms)
        return await self._eval_arg(f"(o) => {_BRIDGE}.spinUntilBonus(o)", opts)
