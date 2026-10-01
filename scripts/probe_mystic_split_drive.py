#!/usr/bin/env python3
"""
Probe: drive Mystic (7x7 cascading) from the server BET response, using the SPLIT
primitive interface (the framework owns all state inference + driving).

ONE SOURCE OF TRUTH
    State is derived ONLY from the raw server response fields -- never a bridge state
    getter (isFreeSpinMode, freeSpinsLeft, ...). Those are per-game client code; the
    server response is stable across every game on the cascading-slots mechanism.

    Fields the drive relies on (confirmed from live captures):
        gameState        1=base, 2=freespin, 3=buybonus, 4=promo. The TRIGGER spin
                         already carries gameState 2, with stake > 0.
        freeSpins        remaining after this round; counts down; 0 on the terminal round
        totalFreeSpin    total awarded; constant across a bonus
        stake            > 0 for base/trigger; 0 for free spins
        payout / balance conservation: balance == prev - stake + payout, every round.
                         result.balance is AUTHORITATIVE (post-payout) -- a live
                         getSnapshot().balance lags behind the win animation.
        hasReachedMaxWin server capped the win; truncates the chain -- the terminal round
                         can be gameState:1 with freeSpins>0. Exit on gameState/freeSpins,
                         never on a spin count.

THE SPLIT DRIVE  (framework decides; bridge exposes dumb primitives)
    boot     : startGame()
    base     : waitForSpinClickable()  ->  getButtonRect('spin')  ->  REAL page.mouse.click
               ->  awaitBetResponse()   (consume the response for that click)
    freespin : awaitFreespinRound()     (client auto-plays; NO click), until a terminal round
    The framework infers base-vs-freespin from each response and drives accordingly.

    Bridge functions (all implemented on the Mystic bridge):
      waitForSpinClickable(stableMs?, timeoutMs?=60000) -> ButtonRect   (owns clearing celebrations)
      getButtonRect('spin') -> ButtonRect | null
      awaitBetResponse(timeoutMs?=15000) -> GameBetResult   (base; cursor-based, race-free)
      awaitFreespinRound(timeoutMs?=60000) -> GameBetResult (freespin; returns terminal round too)
    Both await-fns REJECT (never hang) with {reason, snapshot}: insufficient_balance,
    popup:<label>, connection:<state>, awaiting_response, no_bet_response,
    round_consume_in_flight. Consume exactly one response per round, in order.

DETERMINISM NOTE
    7x7 has no forced-outcome paytable, so this is a YOLO probe: real RNG,
    spin-until-condition. "Deterministic" = the response-driven driving is consistent
    and the per-response invariants always hold -- not that the RNG repeats.

USAGE
    python scripts/probe_mystic_split_drive.py --env dev
    python scripts/probe_mystic_split_drive.py --url "http://.../index.html?data=..." --cycles 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

REPO = Path(__file__).resolve().parents[1]
VIEWPORT = {"width": 531, "height": 919}     # mobile dims, matches the runner
BASE_STATE = 1                               # gameState == 1 is base
FREESPIN_STATE = 2                           # gameState == 2 is free-spin (confirmed from captures)


def resolve_url(env: str, override: Optional[str]) -> str:
    """Reuse the committed env.config url; no suite_runner needed."""
    if override:
        url = override
    else:
        suffix = "stg" if env in ("staging", "stg") else env
        cfg = REPO / "games" / "mystic" / f"env.config.{suffix}.yml"
        m = re.search(r'^\s*url:\s*"([^"]+)"', cfg.read_text(encoding="utf-8"), re.MULTILINE)
        if not m or not m.group(1).strip():
            sys.exit(f"No static url in {cfg}. Pass --url with a fresh launch URL.")
        url = m.group(1)
    if "bridge=1" not in url:
        url += ("&" if "?" in url else "?") + "bridge=1"
    return url


# ---------------------------------------------------------------------------
# The BET response -- reduced to the fields we drive on. Nothing else is read.
# ---------------------------------------------------------------------------
@dataclass
class Bet:
    seq: int
    game_state: Optional[int]
    free_spins: int
    total_free_spin: int
    stake: float
    payout: float
    balance: float
    has_max_win: bool
    round_id: Optional[str]

    @classmethod
    def parse(cls, r: dict, seq: int) -> "Bet":
        return cls(
            seq=seq,
            game_state=r.get("gameState"),
            free_spins=int(r.get("freeSpins") or 0),
            total_free_spin=int(r.get("totalFreeSpin") or 0),
            stake=float(r.get("stake") or 0),
            payout=float(r.get("payout") or 0),
            balance=float(r.get("balance") or 0),           # authoritative post-payout balance
            has_max_win=bool(r.get("hasReachedMaxWin")),
            round_id=r.get("roundId"),
        )

    def row(self) -> dict:
        return {k: getattr(self, k) for k in
                ("seq", "game_state", "free_spins", "total_free_spin",
                 "stake", "payout", "balance", "has_max_win", "round_id")}


# ---------------------------------------------------------------------------
# Bridge primitives -- boot + the split drive. No state getters used for gating.
# ---------------------------------------------------------------------------
async def missing_api(page: Page) -> list:
    """Which required bridge functions are absent (empty list = all present)."""
    return await page.evaluate(
        """() => {
            const b = window.__gameTestBridge;
            if (!b) return ['__gameTestBridge'];
            return ['startGame','waitForSpinClickable','getButtonRect','awaitBetResponse','awaitFreespinRound']
                .filter(f => typeof b[f] !== 'function');
        }"""
    )


async def spin_gate_reason(page: Page) -> str:
    """On a hang, read WHY the spin can't fire -- diagnostics only, never for gating."""
    try:
        info = await page.evaluate(
            """() => {
                const b = window.__gameTestBridge; let can = null, popups = null;
                try { can = b.canDo ? b.canDo('spin') : null; } catch (_) {}
                try { popups = b.getSnapshot ? b.getSnapshot().visiblePopups : null; } catch (_) {}
                return { can, popups };
            }"""
        )
        return (f"spin gate: canDo('spin')={json.dumps(info.get('can'))}, "
                f"visiblePopups={json.dumps(info.get('popups'))}")
    except Exception as e:
        return f"(could not read spin gate: {e})"


# JS wrapper: run an awaiting bridge call, surface a {reason,snapshot} rejection cleanly
# instead of a raw Playwright trace. __CALL__ is the JS expression to await.
_AWAIT_JS = """async (t) => {
    const b = window.__gameTestBridge;
    try { return { ok: true, result: await __CALL__ }; }
    catch (e) {
        let reason = (e && e.reason) || (e && e.message) || String(e);
        let snap = null;
        try { snap = (e && e.snapshot) || (b.getSnapshot ? b.getSnapshot() : null); } catch (_) {}
        return { ok: false, error: reason, snapshot: snap };
    }
}"""


async def _await_response(page: Page, seq: int, label: str, call_expr: str, timeout_ms: int) -> Bet:
    """Await one of the bridge's response primitives; return a parsed Bet or raise a clean error."""
    js = _AWAIT_JS.replace("__CALL__", call_expr)
    try:
        res = await asyncio.wait_for(
            page.evaluate(js, timeout_ms),
            timeout=(timeout_ms / 1000) + 5,     # python backstop just beyond the bridge's own bound
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"{label} did not resolve within {timeout_ms}ms; {await spin_gate_reason(page)}")
    except Exception as e:                       # e.g. execution context destroyed (page reloaded)
        raise RuntimeError(f"{label} evaluate failed: {e}")
    if res is None:
        raise RuntimeError(f"{label} evaluate returned null")
    if not res.get("ok"):
        snap = res.get("snapshot")
        hint = f"\n  snapshot: {json.dumps(snap)[:500]}" if snap else ""
        raise RuntimeError(f"{label} rejected -> {res.get('error')}{hint}")
    raw = res.get("result")
    if raw is None:
        raise RuntimeError(f"{label} resolved but result was null -- expected a GameBetResult")
    return Bet.parse(raw, seq)


async def wait_spin_clickable(page: Page, timeout_ms: int) -> None:
    """1. Readiness -- separate, explicit step. Owns clearing celebrations bridge-side."""
    res = await asyncio.wait_for(
        page.evaluate(
            _AWAIT_JS.replace("__CALL__", "b.waitForSpinClickable(undefined, t)"),
            timeout_ms,
        ),
        timeout=(timeout_ms / 1000) + 5,
    )
    if not res.get("ok"):
        snap = res.get("snapshot")
        hint = f"\n  snapshot: {json.dumps(snap)[:500]}" if snap else ""
        raise RuntimeError(f"waitForSpinClickable rejected -> {res.get('error')}{hint}")


async def click_spin(page: Page) -> None:
    """2. Click only -- read coords, issue a real coordinate click. No readiness wait inside."""
    rect = await page.evaluate("() => window.__gameTestBridge.getButtonRect('spin')")
    if not rect:
        raise RuntimeError("getButtonRect('spin') returned null -- spin button unavailable")
    await page.mouse.click(rect["centerX"], rect["centerY"])


async def await_base(page: Page, seq: int, timeout_ms: int) -> Bet:
    """3. Consume the response for the click just issued."""
    return await _await_response(page, seq, "awaitBetResponse()", "b.awaitBetResponse(t)", timeout_ms)


async def await_freespin(page: Page, seq: int, timeout_ms: int) -> Bet:
    """Free-spin phase: passively consume the next auto-played round (no click)."""
    return await _await_response(page, seq, "awaitFreespinRound()", "b.awaitFreespinRound(t)", timeout_ms)


# ---------------------------------------------------------------------------
# Probe -- all classification is response-only
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    log: list = field(default_factory=list)          # every Bet.row(), in order
    state_map: dict = field(default_factory=dict)     # discovered gameState -> label
    violations: list = field(default_factory=list)

    def observe(self, b: Bet, phase: str) -> None:
        self.log.append({"phase": phase, **b.row()})
        # discover the gameState<->mode mapping empirically (base=1 / freespin=2 seeded)
        if b.game_state is not None:
            in_fs = b.free_spins > 0 or b.total_free_spin > 0
            label = "base" if b.game_state == BASE_STATE else ("freespin" if in_fs else "unknown")
            prev = self.state_map.get(b.game_state)
            if prev and label != "unknown" and prev != label:
                self.violations.append(
                    f"gameState={b.game_state} classified as both {prev} and {label} (seq {b.seq})")
            elif label != "unknown":
                self.state_map[b.game_state] = label
        # conservation (uses authoritative result.balance): base debits stake, free spins bet 0
        if len(self.log) >= 2:
            prev_bal = self.log[-2]["balance"]
            expect = prev_bal - b.stake + b.payout
            if b.balance and abs(expect - b.balance) > 1e-6:
                self.violations.append(
                    f"seq {b.seq}: balance {b.balance} != {prev_bal} - stake {b.stake} + payout {b.payout}")
        # response-only invariant: an auto-played free spin never charges stake
        if phase == "freespin" and b.stake != 0:
            self.violations.append(f"seq {b.seq}: free-spin round has nonzero stake {b.stake}")

    def report(self) -> dict:
        return {"total_responses": len(self.log), "discovered_state_map": self.state_map,
                "violations": self.violations, "log": self.log}


async def drive(page: Page, probe: Probe, cycles: int, max_base: int,
                spin_timeout_ms: int, bet_timeout_ms: int, fs_timeout_ms: int) -> None:
    """Two-phase driver. Base spins are real coordinate clicks; free spins are passive awaits.
    Every transition is decided from the returned response alone."""
    cycles_done = 0
    while cycles_done < cycles:
        # ── base game: spin until free spins trigger ──────────────────────
        trigger = None
        for i in range(1, max_base + 1):
            await wait_spin_clickable(page, spin_timeout_ms)          # 1. readiness (separate)
            await click_spin(page)                                    # 2. real coordinate click
            b = await await_base(page, len(probe.log), bet_timeout_ms)  # 3. consume the response
            entered = b.total_free_spin > 0 or b.game_state == FREESPIN_STATE
            probe.observe(b, "trigger" if entered else "base")
            print(f"  base spin {i:>3}: gameState={b.game_state} fs={b.free_spins} "
                  f"totalFs={b.total_free_spin} stake={b.stake} payout={b.payout} bal={b.balance}"
                  + ("   <== FREE SPINS TRIGGERED" if entered else ""))
            if entered:
                trigger = b
                break
        if trigger is None:
            raise TimeoutError(f"no trigger in {max_base} base spins (dry streak -- inconclusive)")

        # ── free-spin chain: observe auto-played rounds, NO clicks ────────
        print(f"  --- free-spin phase: {trigger.total_free_spin} awarded (observing auto-play) ---")
        fs_round = 0
        while True:
            b = await await_freespin(page, len(probe.log), fs_timeout_ms)
            probe.observe(b, "freespin")
            fs_round += 1
            print(f"  fs round  {fs_round:>3}: gameState={b.game_state} fs={b.free_spins} "
                  f"totalFs={b.total_free_spin} stake={b.stake} payout={b.payout} "
                  f"bal={b.balance} maxWin={b.has_max_win}")
            # terminal, response-only: freeSpins==0 (normal) OR gameState back to base (max-win truncation)
            if b.free_spins == 0 or b.game_state == BASE_STATE:
                if b.has_max_win:
                    print("  --- max win reached: bonus truncated early ---")
                print(f"  free spins complete after {fs_round} rounds; back to base (bal={b.balance})")
                break
            if fs_round > 500:
                probe.violations.append("free-spin phase exceeded 500 rounds -- no exit detected")
                return

        cycles_done += 1
        print(f"=== cycle {cycles_done}/{cycles} complete ===")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="dev")
    ap.add_argument("--url", default=None, help="override launch URL (bridge=1 added if missing)")
    ap.add_argument("--cycles", type=int, default=1, help="base->freespin->base circuits to run")
    ap.add_argument("--max-base-spins", type=int, default=400)
    ap.add_argument("--spin-timeout-ms", type=int, default=60000, help="waitForSpinClickable bound")
    ap.add_argument("--bet-timeout-ms", type=int, default=15000, help="awaitBetResponse bound")
    ap.add_argument("--fs-timeout-ms", type=int, default=60000, help="awaitFreespinRound bound")
    ap.add_argument("--headless", action="store_true", help="run without a visible window (default: headed)")
    ap.add_argument("--slow-mo", type=int, default=0, help="ms to slow each action for easier watching")
    args = ap.parse_args()

    # An abandoned evaluate errors with TargetClosedError once we close the browser after a
    # timeout; that's expected teardown noise, not a real failure.
    def _hush_teardown(loop, context):
        exc = context.get("exception")
        if exc is not None and "has been closed" in str(exc):
            return
        loop.default_exception_handler(context)
    asyncio.get_running_loop().set_exception_handler(_hush_teardown)

    url = resolve_url(args.env, args.url)
    probe = Probe()
    out_dir = REPO / "test-results" / "mystic" / args.env
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = time.strftime("%d-%m-%Y-%H%M%S")

    print(f"Mystic split-drive probe | cycles={args.cycles}")
    print(f"URL: {url[:80]}...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, slow_mo=args.slow_mo)
        page = await (await browser.new_context(viewport=VIEWPORT)).new_page()
        report: dict = probe.report()
        try:
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function("() => !!window.__gameTestBridge", timeout=60000)
            missing = await missing_api(page)
            if missing:
                print(f"\nERROR: the Mystic bridge is missing required functions: {missing}\n"
                      "       This probe drives through waitForSpinClickable / getButtonRect /\n"
                      "       awaitBetResponse / awaitFreespinRound -- see the contract at the top.\n")
                return 2
            print("bridge split API present; starting game...")
            await page.evaluate("() => window.__gameTestBridge.startGame()")   # boot -> clean base
            await drive(page, probe, args.cycles, args.max_base_spins,
                        args.spin_timeout_ms, args.bet_timeout_ms, args.fs_timeout_ms)
        except (TimeoutError, PWTimeout, RuntimeError) as e:
            print(f"\nProbe stopped: {e}")
        except Exception as e:                       # never a raw traceback -- still write the log
            print(f"\nProbe stopped (unexpected): {type(e).__name__}: {e}")
        finally:
            report = probe.report()
            (out_dir / f"probe-{tag}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            await browser.close()

    print("\n----- RESULT -----")
    print(f"responses observed : {report['total_responses']}")
    print(f"gameState mapping  : {report['discovered_state_map']}  (1=base, 2=freespin seeded)")
    print(f"invariant breaches : {len(report['violations'])}")
    for v in report["violations"]:
        print(f"   ! {v}")
    print(f"full log written   : {out_dir / f'probe-{tag}.json'}")
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
