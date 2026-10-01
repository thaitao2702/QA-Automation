#!/usr/bin/env python3
"""
Probe: drive Mystic (7x7 cascading) entirely from the server BET response.

ONE SOURCE OF TRUTH
    The server bet response is the only thing this script reads to decide what to do.
    It never reads a bridge state getter (getSnapshot, waitForStableGameState,
    isFreeSpinMode, freeSpinsLeft, getFreeSpinsRemaining, ...). Those are per-game
    CLIENT implementations and drift between games; the server response is stable
    across every game that shares the cascading-slots mechanism.

    State is derived only from response fields (confirmed against live captures):
        gameState        1 = base, 2 = free-spin. The TRIGGERING spin already carries
                         gameState 2 (with stake > 0 -- it's the charged base bet that
                         landed 3+ FreeSpin! scatters). Promo value still to be discovered.
        freeSpins        remaining free spins (counts down; totalFreeSpin stays constant)
        totalFreeSpin    free spins awarded on the triggering spin
        stake            base spins charge it; FREE spins are stake 0 (asserted invariant)
        payout / balance conservation: balance == prev - stake + payout, every response
        roundId          shared across a whole bonus (trigger + all its free spins);
                         `id` is the per-spin key

THE BRIDGE PRIMITIVE  (the only driving call, besides boot)
        window.__gameTestBridge.placeBet(opts?) : Promise<GameBetResult>

    Atomic: "advance the game by exactly one server round and resolve with THAT round's
    untouched server response" (the inner `result` object, same payload setForcedResult
    takes). No client-derived fields, no interpretation.
      - Base game        -> initiates one bet, awaits its BET socket response, resolves it.
      - Free-spin phase  -> the client auto-plays; placeBet() resolves with the NEXT
                            free-spin round's response, in order (the bridge FIFO-buffers
                            so no round is dropped between calls). It does NOT send a bet.
    Bounded timeout -> rejects with {reason, snapshot}, never the 180s hang.

    => The framework loop is uniform: `resp = await placeBet()` every iteration, then
       classify from `resp` alone.

THE LOOP
    boot     : startGame()  (its waitForDeterministicStart plays out any resumed bonus,
               so we begin in a clean base state -- that is boot/driving, not a state read)
    base     : placeBet(); repeat until a response shows free spins were awarded
    freespin : placeBet() (observe auto-played rounds); repeat until free spins run out
    repeat for --cycles circuits.

DETERMINISM NOTE
    7x7 has no paytable that forces a fixed outcome and setForcedResult is one-shot
    client-side, so this is a YOLO probe: real RNG, spin-until-condition. "Deterministic"
    here means the response-derived driving is consistent and the per-response invariants
    always hold -- not that the RNG repeats.

USAGE
    python scripts/probe_mystic_response_driven.py --env dev
    python scripts/probe_mystic_response_driven.py --url "http://.../index.html?data=..." --cycles 3 --headed
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
from typing import Any, Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

REPO = Path(__file__).resolve().parents[1]
VIEWPORT = {"width": 531, "height": 919}     # mobile dims, matches the runner
BASE_STATE = 1                                # gameState == 1 is base (confirmed raw.yml:444)


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
            balance=float(r.get("balance") or 0),
            has_max_win=bool(r.get("hasReachedMaxWin")),
            round_id=r.get("roundId"),
        )

    def row(self) -> dict:
        return {k: getattr(self, k) for k in
                ("seq", "game_state", "free_spins", "total_free_spin",
                 "stake", "payout", "balance", "has_max_win", "round_id")}


# ---------------------------------------------------------------------------
# Bridge: exactly two calls -- startGame() to boot, placeBet() to drive.
# ---------------------------------------------------------------------------
async def has_place_bet(page: Page) -> bool:
    return await page.evaluate(
        "() => !!(window.__gameTestBridge && typeof window.__gameTestBridge.placeBet === 'function')")


async def spin_gate_reason(page: Page) -> str:
    """On a placeBet hang, read WHY the spin can't fire -- confirms popup/animation gates."""
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


async def place_bet(page: Page, seq: int, timeout_ms: int) -> Bet:
    """The single atomic driving primitive. Resolves with one round's raw response.
    Wraps the bridge call in a JS try/catch so a rejection surfaces as a clean reason
    (+ snapshot) instead of a raw Playwright trace; the outer guard covers a total hang."""
    try:
        res = await asyncio.wait_for(
            page.evaluate(
                """async () => {
                    const b = window.__gameTestBridge;
                    try {
                        const r = await b.placeBet();   // add opts if the bridge takes a stake
                        return { ok: true, result: r };
                    } catch (e) {
                        let reason = (e && e.reason) || (e && e.message) || String(e);
                        let snap = null;
                        try { snap = (e && e.snapshot) || (b.getSnapshot ? b.getSnapshot() : null); } catch (_) {}
                        return { ok: false, error: reason, snapshot: snap };
                    }
                }"""
            ),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"placeBet() did not resolve within {timeout_ms}ms; {await spin_gate_reason(page)}")
    except Exception as e:                      # e.g. execution context destroyed (page reloaded)
        raise RuntimeError(f"placeBet() evaluate failed: {e}")
    if res is None:
        raise RuntimeError("placeBet() evaluate returned null")
    if not res.get("ok"):
        snap = res.get("snapshot")
        hint = f"\n  snapshot: {json.dumps(snap)[:500]}" if snap else ""
        raise RuntimeError(f"placeBet() rejected -> {res.get('error')}{hint}")
    raw = res.get("result")
    if raw is None:
        raise RuntimeError("placeBet() resolved but result was null -- expected a GameBetResult")
    return Bet.parse(raw, seq)


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
        # discover the gameState<->mode mapping empirically (base=1 seeded; 2=freespin confirmed)
        if b.game_state is not None:
            in_fs = b.free_spins > 0 or b.total_free_spin > 0
            label = "base" if b.game_state == BASE_STATE else ("freespin" if in_fs else "unknown")
            prev = self.state_map.get(b.game_state)
            if prev and label != "unknown" and prev != label:
                self.violations.append(
                    f"gameState={b.game_state} classified as both {prev} and {label} (seq {b.seq})")
            elif label != "unknown":
                self.state_map[b.game_state] = label
        # conservation: base debits stake, free spins bet 0; payout credits either way
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


async def drive(page: Page, probe: Probe, cycles: int, max_base: int, bet_timeout_ms: int) -> None:
    """One flat driver. Every iteration is a single placeBet(); the mode machine and
    every transition are decided from the returned response alone."""
    mode = "base"
    cycles_done = 0
    base_streak = 0     # consecutive base spins with no trigger (dry-streak guard)
    fs_round = 0

    while cycles_done < cycles:
        b = await place_bet(page, len(probe.log), bet_timeout_ms)

        if mode == "base":
            base_streak += 1
            triggered = b.total_free_spin > 0 or (b.game_state not in (None, BASE_STATE))
            probe.observe(b, "trigger" if triggered else "base")
            print(f"  base spin {base_streak:>3}: gameState={b.game_state} fs={b.free_spins} "
                  f"totalFs={b.total_free_spin} payout={b.payout} bal={b.balance}"
                  + ("   <== FREE SPINS TRIGGERED" if triggered else ""))
            if triggered:
                print(f"  --- free-spin phase: {b.total_free_spin} awarded (observing auto-play) ---")
                mode, fs_round = "freespin", 0
            elif base_streak >= max_base:
                raise TimeoutError(f"no trigger in {max_base} base spins (dry streak -- inconclusive)")
            continue

        # mode == "freespin": placeBet() yields the next auto-played free-spin round.
        # A base-state round here means the sequence already ended and placeBet() sent a
        # fresh base bet -- so the circuit is complete and this round opens the next one.
        if b.game_state == BASE_STATE and b.total_free_spin == 0:
            probe.observe(b, "base")
            cycles_done += 1
            print(f"  free spins done after {fs_round} rounds; back to base (bal={b.balance})")
            mode, base_streak = "base", 1
            continue

        probe.observe(b, "freespin")
        fs_round += 1
        print(f"  fs round  {fs_round:>3}: gameState={b.game_state} fs={b.free_spins} "
              f"totalFs={b.total_free_spin} payout={b.payout} bal={b.balance} maxWin={b.has_max_win}")
        # exit signals, response-only: last free spin (freeSpins==0) or early max-win stop
        if b.free_spins == 0 or b.has_max_win:
            cycles_done += 1
            if b.has_max_win:
                print("  --- max win reached: bonus terminates early ---")
            print(f"  free spins complete after {fs_round} rounds")
            mode, base_streak = "base", 0
        elif fs_round > 500:
            probe.violations.append("free-spin phase exceeded 500 rounds -- no exit detected")
            return


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="dev")
    ap.add_argument("--url", default=None, help="override launch URL (bridge=1 added if missing)")
    ap.add_argument("--cycles", type=int, default=1, help="base->freespin->base circuits to run")
    ap.add_argument("--max-base-spins", type=int, default=400)
    ap.add_argument("--bet-timeout-ms", type=int, default=60000)
    ap.add_argument("--headless", action="store_true", help="run without a visible window (default: headed)")
    ap.add_argument("--slow-mo", type=int, default=0, help="ms to slow each action for easier watching")
    args = ap.parse_args()

    # An abandoned placeBet() evaluate errors with TargetClosedError once we close the
    # browser after a timeout; that's expected teardown noise, not a real failure.
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

    print(f"Mystic response-driven probe | cycles={args.cycles}")
    print(f"URL: {url[:80]}...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, slow_mo=args.slow_mo)
        page = await (await browser.new_context(viewport=VIEWPORT)).new_page()
        report: dict = probe.report()
        try:
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function("() => !!window.__gameTestBridge", timeout=60000)
            if not await has_place_bet(page):
                print("\nERROR: window.__gameTestBridge.placeBet() is not implemented yet.\n"
                      "       This probe drives exclusively through it -- see the contract at the\n"
                      "       top of this file. Add the atomic placeBet() to the Mystic bridge first.\n")
                return 2
            print("bridge + placeBet() present; starting game...")
            await page.evaluate("() => window.__gameTestBridge.startGame()")  # boot -> clean base
            await drive(page, probe, args.cycles, args.max_base_spins, args.bet_timeout_ms)
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
    print(f"gameState mapping  : {report['discovered_state_map']}  (base=1 seeded; rest discovered)")
    print(f"invariant breaches : {len(report['violations'])}")
    for v in report["violations"]:
        print(f"   ! {v}")
    print(f"full log written   : {out_dir / f'probe-{tag}.json'}")
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
