#!/usr/bin/env python3
"""
Bridge probe — load a game, start it, and report what the suite would see.

Answers the question a hanging run cannot: *what is it waiting for?* Prints the required-API check,
the snapshot, whether `waitForSpinClickable` resolves (and how long it takes), the buttons the build
actually exposes, and how those compare to the active profile's dialect.

    python scripts/probe_bridge.py --game zeus --env dev
    python scripts/probe_bridge.py --game mystic --env local --headless

Read-only: it starts the game and looks. It places no bet and clicks nothing but Start.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario_engine as se   # noqa: E402
import run_scenario as rs      # noqa: E402


async def timed(label: str, coro, budget_s: float):
    """Await `coro` with a budget, reporting how long it took or how it failed."""
    t0 = time.time()
    try:
        r = await asyncio.wait_for(coro, timeout=budget_s)
        return True, round(time.time() - t0, 2), r
    except asyncio.TimeoutError:
        return False, round(time.time() - t0, 2), f"DID NOT RESOLVE within {budget_s}s"
    except Exception as e:                                   # noqa: BLE001
        return False, round(time.time() - t0, 2), f"{type(e).__name__}: {str(e)[:200]}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default="mystic")
    ap.add_argument("--env", default="dev")
    ap.add_argument("--url", default=None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--spin-budget", type=float, default=20.0,
                    help="seconds to give waitForSpinClickable before calling it stuck")
    args = ap.parse_args()

    GAME = se.set_profile(args.game)
    url = rs.resolve_url(args.game, args.env, args.url)
    print(f"profile : {GAME.name} ({GAME.mechanism})")
    print(f"url     : {url[:100]}...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless)
        page = await (await browser.new_context(viewport=GAME.VIEWPORT)).new_page()
        errors: list = []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))

        print("\n-- load --")
        await page.goto(url, wait_until="networkidle")
        try:
            await page.wait_for_function("() => !!window.__gameTestBridge", timeout=30000)
            print("   bridge   : present")
        except Exception:
            print("   bridge   : ABSENT after 30s -- nothing can run. Is the build made with the "
                  "QA bridge, and is `bridge=1` on the URL?")
            await browser.close()
            return 1

        missing = await page.evaluate(GAME.api_check_js())
        print(f"   required : {'all present' if not missing else 'MISSING ' + str(missing)}")

        ok, secs, res = await timed("startGame", page.evaluate(
            "() => window.__gameTestBridge.startGame()"), 90)
        print(f"   startGame: {'ok' if ok else res} ({secs}s)")

        print("\n-- snapshot --")
        snap = await page.evaluate(
            "() => { const b = window.__gameTestBridge;"
            " return b.getSnapshot ? b.getSnapshot() : null; }") or {}
        for k in ("appState", "balance", "betAmount", "betIndex", "isAutoPlay", "isFreeSpinMode",
                  "freeSpinsRemaining", "autoSpinsRemaining", "visiblePopups", "busyOps",
                  "isBetPending", "isShowingBigWin", "isShowingTotalWin", "isOnBonusGame",
                  "isHoldSpinActive", "isLockSpinButton", "hasActivePromo", "sceneReady"):
            if k in snap:
                print(f"   {k:20s} {json.dumps(snap[k])[:70]}")
        extra = [k for k in snap if k not in
                 {"appState","balance","betAmount","betIndex","isAutoPlay","isFreeSpinMode",
                  "freeSpinsRemaining","autoSpinsRemaining","visiblePopups","busyOps","isBetPending",
                  "isShowingBigWin","isShowingTotalWin","isOnBonusGame","isHoldSpinActive",
                  "isLockSpinButton","hasActivePromo","sceneReady"}]
        if extra:
            print(f"   (other fields: {', '.join(extra)})")

        print("\n-- can we spin? --")
        can = await page.evaluate(
            "() => { const b = window.__gameTestBridge;"
            " return b.canDo ? b.canDo('spin') : null; }")
        print(f"   canDo('spin')         : {json.dumps(can)}")
        rect = await page.evaluate(
            "() => { const b = window.__gameTestBridge; return b.getButtonRect('spin'); }")
        print(f"   getButtonRect('spin') : {json.dumps(rect)}")
        ok, secs, res = await timed(
            "waitForSpinClickable",
            page.evaluate("(t) => window.__gameTestBridge.waitForSpinClickable(undefined, t)",
                          int(args.spin_budget * 1000)),
            args.spin_budget + 5)
        print(f"   waitForSpinClickable  : {'resolved' if ok else res} ({secs}s)")
        if not ok:
            print("   ^^ THIS is what a run stalls on: settle() waits on it after every round.")

        print("\n-- buttons the build exposes --")
        rects = await page.evaluate(
            "() => { const b = window.__gameTestBridge;"
            " return b.getAllButtonRects ? Object.keys(b.getAllButtonRects()) : null; }")
        if rects is None:
            print("   getAllButtonRects not implemented -- probing the profile's names one by one")
            rects = []
            for role, name in GAME.BUTTONS.items():
                if not name or "{" in name:
                    continue
                r = await page.evaluate("(n) => !!window.__gameTestBridge.getButtonRect(n)", name)
                if r:
                    rects.append(name)
        print(f"   {len(rects)} on screen: {', '.join(sorted(rects))}")

        print("\n-- profile dialect vs reality --")
        bad = []
        for role, name in sorted(GAME.BUTTONS.items()):
            if not name:
                continue
            probe = name.format(i=0) if "{" in name else name
            present = await page.evaluate(
                "(n) => window.__gameTestBridge.getButtonRect(n) !== null", probe)
            mark = "ok " if present else "-- "
            if not present:
                bad.append(f"{role} -> {probe}")
            print(f"   {mark} {role:18s} {probe}")
        print("\n   NOTE: a popup-scoped control (auto:*, bet:*, buyBonus:*) is legitimately absent "
              "while its popup is closed. Only MAIN-SCREEN misses are real problems.")
        if bad:
            print(f"   not resolvable right now: {len(bad)}")

        if errors:
            print(f"\n-- page errors ({len(errors)}) --")
            for e in errors[:5]:
                print("   ", e)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
