#!/usr/bin/env python3
"""
Big-win probe — why is the celebration not being skipped?

`skip_big_win()` can decline for four different reasons and the run looks identical in all of them:
slower, with no explanation. This answers which one, against the live build, without running a suite.

    python scripts/probe_big_win.py --game grandma --env dev
    python scripts/probe_big_win.py --game grandma --env dev --spins 40

It spins until a round qualifies as a big win, then reports, in order:

  1. the win bands the launch payload actually carries, and the threshold derived from them;
  2. whether this build knows the skip control the profile names;
  3. what surfaced when the big win landed -- popup names and busy labels, sampled at 50 ms;
  4. whether tapping the control twice actually cleared it.

⚠ It BETS. It spins at the current stake until a big win happens or `--spins` is exhausted, so
point it at a test account. Everything else is read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario_engine as se   # noqa: E402
import run_scenario as rs      # noqa: E402

_WATCH_JS = """() => {
    window.__bwWatch = { popups: {}, busy: {}, polls: 0 };
    if (window.__bwTimer) clearInterval(window.__bwTimer);
    window.__bwTimer = setInterval(() => {
        const b = window.__gameTestBridge;
        if (!b || !b.getSnapshot) return;
        const s = b.getSnapshot() || {};
        const w = window.__bwWatch;
        w.polls += 1;
        for (const n of (s.visiblePopups || [])) w.popups[n] = (w.popups[n] || 0) + 1;
        for (const o of (s.busyOps || [])) w.busy[o] = (w.busy[o] || 0) + 1;
    }, 50);
    return true;
}"""

_READ_JS = """() => {
    if (window.__bwTimer) { clearInterval(window.__bwTimer); window.__bwTimer = null; }
    return window.__bwWatch || { popups: {}, busy: {}, polls: 0 };
}"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default="grandma")
    ap.add_argument("--env", default="dev")
    ap.add_argument("--url", default=None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--spins", type=int, default=30, help="how many spins to hunt a big win through")
    args = ap.parse_args()

    GAME = se.set_profile(args.game)
    url = rs.resolve_url(args.game, args.env, args.url)
    print(f"profile : {GAME.name} ({GAME.mechanism})")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless)
        page = await (await browser.new_context(viewport=GAME.VIEWPORT)).new_page()
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_function("() => !!window.__gameTestBridge", timeout=60000)
        await se.start_game(page)

        # ── 1. the bands, and what they imply ────────────────────────────────────────
        info = await se.read_game_info(page)
        bands = GAME.win_formula(info)
        threshold = GAME.big_win_threshold(info)
        print("\n-- 1. win bands (gameInfo) --")
        if not bands:
            raw_settings = GAME.settings(info)
            print("   winFormula : ABSENT -- this is fatal for the skip: with no bands there is no")
            print("                threshold, so every celebration plays out in full.")
            print(f"   settings keys seen: {sorted(raw_settings)[:12]}")
            gen = raw_settings.get("general")
            if isinstance(gen, dict):
                print(f"   settings.general keys: {sorted(gen)[:12]}")
        else:
            for b in bands:
                print(f"   {str(b.get('name')):<14} {b.get('min')} .. {b.get('max')}")
            print(f"   celebrated tiers -> {GAME.CELEBRATED_TIERS}")
            print(f"   THRESHOLD        -> {threshold}x the bet")

        # ── 2. does the build have the control? ──────────────────────────────────────
        role = GAME.button("big_win_skip")
        print("\n-- 2. the skip control --")
        print(f"   profile names    : {role!r}")
        names = await page.evaluate(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getButtonNames ? b.getButtonNames() : null; }")
        if names is None:
            print("   getButtonNames() : not implemented -- cannot confirm the name exists")
        else:
            hit = [n for n in names if "big" in str(n).lower() or "win" in str(n).lower()]
            print(f"   build knows it   : {role in names}")
            print(f"   name(s) like it  : {hit or 'NONE -- the celebration may not be skippable by name'}")

        # ── 3. spin until one qualifies, watching what appears ───────────────────────
        print(f"\n-- 3. hunting a big win ({args.spins} spins max) --")
        found = None
        for i in range(args.spins):
            await se.wait_spin_clickable(page, 60000)
            await page.evaluate(_WATCH_JS)
            await se.click_spin(page)
            raw = await se.await_bet_response(page, 30000)
            base, payout = GAME.base_stake(raw), float(raw.get(GAME.F_PAYOUT) or 0)
            mult = round(payout / base, 2) if base else None
            big = GAME.is_big_win(raw, threshold)
            print(f"   spin {i + 1:>3}: payout {payout} on {base}  = {mult}x   big_win={big}")
            if big:
                # ⚠ Not a flat wait. A ways game presents winWays -> wildMultiplier -> count-up
                # before the banner, which is why a 4 s window reported "nothing appeared" on a
                # 36x win. Watch until the banner shows or the round finishes presenting.
                await page.evaluate(se._BIG_WIN_POPUP_JS, {"ticks": 400, "grace": 30})
                found = await page.evaluate(_READ_JS)
                break
            await page.evaluate(_READ_JS)
            if se.is_freespin_trigger(se.Bet.parse(raw, i)):
                print("   (a bonus triggered -- draining it before carrying on)")
                await se.drain_to_base(page, True)

        if not found:
            print("   no big win in that many spins. Raise --spins, or force one with a paytable.")
            await browser.close()
            return 0

        print("\n-- 4. what appeared when it landed --")
        print(f"   samples    : {found.get('polls')}")
        print(f"   popups     : {json.dumps(found.get('popups'), indent=None)}")
        print(f"   busy ops   : {json.dumps(found.get('busy'), indent=None)}")
        if not found.get("popups") and not found.get("busy"):
            print("   ⚠ NOTHING surfaced -- the celebration is drawn without a popup or a busy")
            print("     label, so there is nothing for the skip to wait for. That is a bridge gap.")

        # ── 5. can it be tapped away? ────────────────────────────────────────────────
        print("\n-- 5. tapping the control --")
        rect = await page.evaluate(
            "(n) => { const b = window.__gameTestBridge;"
            " return b && b.getButtonRect ? b.getButtonRect(n) : null; }", role)
        print(f"   getButtonRect({role!r}) -> {rect}")
        # and what the build DOES have up, which is the answer when the name above is wrong
        cands = await page.evaluate(se._BIG_WIN_CONTROLS_JS) or []
        print(f"   skip-like controls up right now: {[c['name'] for c in cands] or 'NONE'}")
        if rect and rect.get("interactable") and rect.get("visible"):
            await page.mouse.click(rect["centerX"], rect["centerY"])
            await asyncio.sleep(1)
            await page.mouse.click(rect["centerX"], rect["centerY"])
            await asyncio.sleep(1)
            after = await page.evaluate(
                "() => { const b = window.__gameTestBridge; const s = b.getSnapshot ? b.getSnapshot() : {};"
                " return { popups: s.visiblePopups || [], busy: s.busyOps || [] }; }")
            print(f"   after two taps -> {after}")
            print("   VERDICT: the two-tap skip works" if not after["popups"]
                  else "   VERDICT: the popup SURVIVED two taps -- the control is not the skip")
        else:
            print("   VERDICT: the control never became clickable while the celebration was up --")
            print("            either the name is wrong or the celebration is not skippable by it.")

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
