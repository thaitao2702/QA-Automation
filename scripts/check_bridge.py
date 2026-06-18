"""
Bridge liveness check — does window.__gameTestBridge still work on the dev build?

Reuses the framework's own URL resolver (playsite flow + ?bridge=1) and the real
BridgeHelper, so this exercises the exact code path the suite uses. No spins, no
back-office calls — fast and BO-free. Verifies, in order:

  1. URL resolution (playsite launch token)
  2. navigate + bridge registration            <- the §6.3 blocker
  3. query path   (getAppState, getBalance, getSnapshot, ...)
  4. action path  (startGame, nextBet, previousBet)
  5. assert path  (getBetIndex comparisons after each action)

Usage:
    .venv/Scripts/python.exe scripts/check_bridge.py            # headless
    .venv/Scripts/python.exe scripts/check_bridge.py --headed   # watch it
    .venv/Scripts/python.exe scripts/check_bridge.py --env dev
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from framework.env_files import load_env_files
from framework.runner.suite_runner import _resolve_game_url
from framework.helpers.bridge import BridgeHelper, BridgeMissingError

from playwright.async_api import async_playwright

GAME = "zeus"

_passes = 0
_fails = 0


def ok(msg: str) -> None:
    global _passes
    _passes += 1
    print(f"  [OK]   {msg}")


def fail(msg: str) -> None:
    global _fails
    _fails += 1
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"  [..]   {msg}")


async def run(env: str, headed: bool) -> int:
    load_env_files(env, BASE_DIR)

    # --- 1. URL resolution -------------------------------------------------
    print("\n=== 1. URL resolution ===")
    try:
        url = _resolve_game_url(GAME, env, BASE_DIR)
        ok(f"resolved game URL ({len(url)} chars)")
        info(url[:120] + ("..." if len(url) > 120 else ""))
        if "bridge=1" not in url:
            fail("URL is missing '?bridge=1' — bridge will NOT register")
    except Exception as e:  # noqa: BLE001
        fail(f"URL resolution failed (launch-token / playsite blocker): {e}")
        return 1

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        page = await browser.new_page()
        bridge = BridgeHelper(page)

        # --- 2. navigate + bridge registration -----------------------------
        print("\n=== 2. Navigate + bridge registration ===")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            ok("page navigated (domcontentloaded)")
        except Exception as e:  # noqa: BLE001
            fail(f"navigation failed: {e}")
            await browser.close()
            return 1

        try:
            await page.wait_for_function(
                "typeof window.__gameTestBridge !== 'undefined' && window.__gameTestBridge !== null",
                timeout=40000,
            )
            ok("window.__gameTestBridge registered")
        except Exception:  # noqa: BLE001
            fail("bridge NOT registered within 40s (the §6.3 blocker is still present)")
            await browser.close()
            return _summary()

        # --- 3. query path -------------------------------------------------
        print("\n=== 3. Query path ===")
        try:
            state = await bridge.get_app_state()
            ok(f"getAppState() -> '{state}'")
        except Exception as e:  # noqa: BLE001
            fail(f"getAppState() failed: {e}")
            state = None

        # --- 4. action path: startGame -------------------------------------
        print("\n=== 4. Action path ===")
        if state == "ready":
            try:
                await bridge.start_game()
                await bridge.wait_for_state("idle", timeout_ms=30000)
                ok("startGame() -> idle")
            except Exception as e:  # noqa: BLE001
                fail(f"startGame()/wait idle failed: {e}")
        else:
            info(f"state is '{state}', not 'ready' — skipping startGame()")

        # read-only queries (after game started)
        for name, coro in [
            ("getBalance", bridge.get_balance()),
            ("getBetAmount", bridge.get_bet_amount()),
            ("getBetIndex", bridge.get_bet_index()),
            ("getDenominations", bridge.get_denominations()),
            ("isSpinEnabled", bridge.is_spin_enabled()),
        ]:
            try:
                val = await coro
                if name == "getDenominations" and isinstance(val, list):
                    val = f"[{len(val)} levels] {val[:3]}..."
                ok(f"{name}() -> {val}")
            except Exception as e:  # noqa: BLE001
                fail(f"{name}() failed: {e}")

        try:
            snap = await bridge.get_snapshot()
            keys = list(snap.keys()) if isinstance(snap, dict) else snap
            ok(f"getSnapshot() -> {len(keys)} keys" if isinstance(keys, list) else f"getSnapshot() -> {keys}")
        except Exception as e:  # noqa: BLE001
            fail(f"getSnapshot() failed: {e}")

        # --- 5. action + assert roundtrip: nextBet / previousBet -----------
        print("\n=== 5. Action + assert roundtrip (bet) ===")
        try:
            base_idx = await bridge.get_bet_index()
            if await bridge.is_next_bet_enabled():
                await bridge.next_bet()                       # ACTION
                new_idx = await bridge.get_bet_index()        # ASSERT input
                if new_idx == base_idx + 1:
                    ok(f"nextBet(): index {base_idx} -> {new_idx} (assert passed)")
                else:
                    fail(f"nextBet(): expected {base_idx + 1}, got {new_idx}")

                await bridge.previous_bet()                   # ACTION
                back_idx = await bridge.get_bet_index()       # ASSERT input
                if back_idx == base_idx:
                    ok(f"previousBet(): index {new_idx} -> {back_idx} (assert passed)")
                else:
                    fail(f"previousBet(): expected {base_idx}, got {back_idx}")
            else:
                info("isNextBetEnabled() is false — cannot exercise bet action roundtrip")
        except BridgeMissingError as e:
            fail(f"bridge dropped mid-test: {e}")
        except Exception as e:  # noqa: BLE001
            fail(f"bet action/assert roundtrip failed: {e}")

        if headed:
            info("headed mode — browser stays open 8s for inspection")
            await asyncio.sleep(8)
        await browser.close()

    return _summary()


def _summary() -> int:
    print("\n" + "=" * 48)
    verdict = "BRIDGE OK" if _fails == 0 else "BRIDGE HAS FAILURES"
    print(f"  {verdict}  —  {_passes} passed, {_fails} failed")
    print("=" * 48 + "\n")
    return 0 if _fails == 0 else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Check if the game test bridge still works")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.env, args.headed)))


if __name__ == "__main__":
    main()
