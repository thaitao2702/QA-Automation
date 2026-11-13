#!/usr/bin/env python3
"""
Scenario engine — the closed-box contract + the reusable drive layer.

This factors the proven driving from probe_mystic_split_drive.py into a form that fits the
scenario/assert model (docs/scenario-assert-authoring.md):

  * A SCENARIO is a closed, engineer-owned box. Public contract = name + description + the
    outputs it exposes. Internals (reach / start / stop / collect) are Python, hidden.
    run(ctx) -> ScenarioResult(reached, res, rounds, timeline, events, video_window, reason)
  * The drive layer (DriveContext) records every round as a replay EVENT and a TIMELINE entry
    so the existing replay viewer renders synced, and surfaces the raw response for asserts.
  * INVARIANTS are the Layer-2 `stream:` predicates: rounds -> {valid, violations}. Exact, free.

The runner (run_scenario.py) invokes a scenario in place of scripted `steps:`, then runs the
per-case asserts and emits result.json / events.jsonl / timeline.json / replay.html.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page

import game_profile

# The active game PROFILE (scripts/cascade_game.py) supplies every per-game name and constant:
# button/popup dialect, gameState values, viewport, capabilities and the response field map. The
# scenarios below are the MECHANISM and stay game-agnostic; a new cascade game subclasses
# CascadeGame, implements the bridge contract (docs/game-bridge-contract.md) and inherits
# every case. `set_profile()` is called by the runner from --game.
GAME: game_profile.SlotGame = game_profile.get("mystic")


def set_profile(name: str) -> "game_profile.SlotGame":
    """Point the engine at a game's profile. Module-level constants are refreshed so the existing
    scenarios keep reading plain names."""
    global GAME, BASE_STATE, FREESPIN_STATE, VIEWPORT
    GAME = game_profile.get(name)
    BASE_STATE, FREESPIN_STATE, VIEWPORT = GAME.BASE_STATE, GAME.FREESPIN_STATE, GAME.VIEWPORT
    return GAME


# Languages the SITE offers, as (code, name, script). `all` / `random:N` selections draw from this
# list, so trim it to what a given GAME actually ships. Script names go into the AI prompts.
SUPPORTED_LANGUAGES = [
    ("en", "English", "Latin"),            ("vi", "Vietnamese", "Latin (with diacritics)"),
    ("th", "Thai", "Thai"),                ("my", "Burmese", "Burmese/Myanmar"),
    ("zh", "Simplified Chinese", "Chinese (Han)"), ("ja", "Japanese", "Japanese (kana/kanji)"),
    ("ko", "Korean", "Hangul"),            ("hi", "Hindi", "Devanagari"),
    ("ru", "Russian", "Cyrillic"),         ("ms", "Bahasa Melayu", "Latin"),
    ("id", "Indonesian", "Latin"),         ("tl", "Tagalog", "Latin"),
    ("pt", "Portuguese", "Latin"),         ("pt-br", "Portuguese (Brazil)", "Latin"),
    ("es", "Spanish", "Latin"),            ("tr", "Turkish", "Latin"),
]
LANGUAGE_NAME = {code: name for code, name, _ in SUPPORTED_LANGUAGES}
# Burmese is the stress case the test matrix calls out (Mah_054/055): the longest strings and a
# script with stacked diacritics, so it breaks text scaling and clipping first.
STRESS_LANGUAGE = "my"

BASE_STATE = GAME.BASE_STATE          # gameState: base game
FREESPIN_STATE = GAME.FREESPIN_STATE  # gameState: free spins
VIEWPORT = GAME.VIEWPORT


class BetRejected(RuntimeError):
    """The bridge handed over a server REFUSAL where a round was expected. It is not a round, so it
    must not reach `conservation` or any other round check; the run errors and names the code."""

    def __init__(self, err: dict, seq: int):
        self.err = err
        super().__init__(
            f"server rejected the bet at stream seq {seq}: code {err['code']} "
            f"({err['meaning']}) status={err['status']} msg={err['msg']!r}. A refusal is not a "
            f"round, so it was not recorded as one.")


# ---------------------------------------------------------------------------
# One BET response, reduced to the fields we drive/assert on. `.raw` keeps the
# untouched server object so binding ({res.payout}) reads the real contract field.
# ---------------------------------------------------------------------------
@dataclass
class Bet:
    seq: int
    game_state: Optional[int]
    free_spins: int
    total_free_spin: int
    # Awarded BY THIS ROUND — 0 inside a bonus, where `total_free_spin` still reports the total.
    # It is what "the trigger awarded N" means, and on a build that reports it outright (Grandma's
    # `wonFreeSpins`) it is exact rather than inferred.
    won_free_spins: int
    stake: float
    # What payouts are computed from. Equal to `stake` on a plain spin; NOT equal under Ante Bet or
    # a Premium Reel, nor on a bought bonus (the stake is the price), nor in free spins (stake 0).
    base_stake: float
    payout: float
    balance: float
    has_max_win: bool
    cascades: int
    round_id: Optional[str]
    raw: dict
    phase: Optional[str] = None    # set by DriveContext: "base"/"trigger" (paid) vs "freespin" (auto)
    # The max-win cap in force when this round was PARSED: the reply's `maxWin`, else the launch
    # settings (GAME.max_win_cap). Frozen here because a later reload can change `maxPayout`.
    max_win: Optional[float] = None

    @classmethod
    def parse(cls, r: dict, seq: int) -> "Bet":
        """Reduce one raw response through the active PROFILE, so a game whose backend nests its
        free-spin counters (Zeus: freeSpinInfo.freespins) or reports no gameState at all still lands
        in the same shape the scenarios read.

        `unwrap()` first: some bridges return the whole socket ENVELOPE rather than the inner bet
        (Chaos's `awaitBetResponse` is documented to). Reading `payout` off an envelope finds
        nothing, so every round would parse as a zero-payout loss and `conservation` would fail on a
        game that is behaving perfectly. Profiles that already hand back the inner object are
        unaffected — `unwrap` is identity for them.

        A REJECTED reply (`status: 0`, negative `code`, no `result`) is refused with `BetRejected`
        instead of becoming a stake-0 / payout-0 / balance-0 round that every check would grade."""
        err = GAME.reply_error(r)
        if err is not None:
            raise BetRejected(err, seq)
        r = GAME.unwrap(r)
        rem, awarded = GAME.free_spins(r)
        return cls(
            seq=seq,
            game_state=GAME.game_state(r),
            free_spins=rem,
            total_free_spin=awarded,
            won_free_spins=GAME.won_free_spins(r),
            stake=float(r.get(GAME.F_STAKE) or 0),
            base_stake=GAME.base_stake(r),
            payout=float(r.get(GAME.F_PAYOUT) or 0),
            balance=float(r.get(GAME.F_BALANCE) or 0),
            has_max_win=bool(r.get(GAME.F_MAX_WIN)),
            cascades=len(GAME.steps(r)),
            round_id=r.get(GAME.F_ROUND_ID),
            raw=r,
            max_win=GAME.max_win_cap(r),
        )

    def row(self) -> dict:
        return {k: getattr(self, k) for k in
                ("seq", "phase", "game_state", "free_spins", "total_free_spin",
                 "won_free_spins", "stake", "base_stake", "payout", "balance",
                 "has_max_win", "cascades", "round_id")}


def is_terminal(b: Optional["Bet"]) -> bool:
    """Is this the round that ENDS a bonus (or a plain base round)? The one definition of the rule.

    Both halves are needed. `freeSpins == 0` alone misses a max-win truncation, which returns to base
    with spins still on the counter — they are forfeited, and a drain waiting for them never ends.
    `gameState == base` alone misses a bonus whose counter emptied on a round still reported in the
    bonus state. `None` counts as terminal: no bet, nothing to drain."""
    return b is None or b.free_spins == 0 or b.game_state == BASE_STATE


# ---------------------------------------------------------------------------
# Bridge primitives (the split drive) — clean rejection surfacing
# ---------------------------------------------------------------------------
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


async def missing_api(page: Page) -> list:
    """Which REQUIRED bridge functions this build is missing (profile.REQUIRED_API). Empty = the
    game can run the suite; anything listed is a bridge gap to close before anything else."""
    return await page.evaluate(GAME.api_check_js())


async def _await(page: Page, call_expr: str, timeout_ms: int, label: str) -> dict:
    try:
        res = await asyncio.wait_for(
            page.evaluate(_AWAIT_JS.replace("__CALL__", call_expr), timeout_ms),
            timeout=(timeout_ms / 1000) + 5,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"{label} did not resolve within {timeout_ms}ms")
    except Exception as e:
        raise RuntimeError(f"{label} evaluate failed: {e}")
    if not res or not res.get("ok"):
        snap = (res or {}).get("snapshot")
        hint = f"  snapshot: {json.dumps(snap)[:400]}" if snap else ""
        raise RuntimeError(f"{label} rejected -> {(res or {}).get('error')}  {hint}")
    return res.get("result")


# A snapshot read used ONLY to explain a failure -- never to decide one. Each step below has exactly
# one authority; this just makes its refusal legible.
_DIAG_JS = """() => { const b = window.__gameTestBridge;
    const s = b.getSnapshot ? b.getSnapshot() : {};
    return { app: s.appState, ready: !!s.sceneReady, popups: s.visiblePopups || [],
             busy: s.busyOps || [], fsMode: !!s.isFreeSpinMode,
             fsLeft: s.freeSpinsRemaining, autoLeft: s.autoSpinsRemaining }; }"""

_GAME_INFO_JS = """() => { const b = window.__gameTestBridge;
    try { return b && b.getGameInfo ? b.getGameInfo() : null; } catch (e) { return null; } }"""


async def _diag(page: Page) -> str:
    """Snapshot rendered for an error message. Best-effort: diagnostics must never mask the failure
    they are describing."""
    try:
        st = await page.evaluate(_DIAG_JS) or {}
    except Exception as e:                                   # noqa: BLE001
        return f"(snapshot unavailable: {type(e).__name__})"
    return (f"appState={st.get('app')!r} sceneReady={st.get('ready')} popups={st.get('popups')} "
            f"busy={st.get('busy')} freeSpinMode={st.get('fsMode')} "
            f"freeSpinsRemaining={st.get('fsLeft')} autoSpinsRemaining={st.get('autoLeft')}")


async def read_game_info(page: Page) -> dict:
    """`getGameInfo()`, envelope peeled by the profile. `{}` when the build does not expose it.

    The BRIDGE captures this at load and hands back its captured copy, so it stays readable after
    the Start click — that is what lets the framework ask "does this session owe a bonus?" at the
    only moment it can act on the answer: once `startGame()` has resolved and the resumed rounds are
    about to arrive. A bridge that re-queries the server here instead would report a `pending` block
    the resume has already cleared, and the framework would never know to drain it."""
    info = GAME.game_info(await page.evaluate(_GAME_INFO_JS) or {})
    GAME.remember_launch(info)              # max-win settings for rounds parsed from here on
    return info


def _ladder_num(v):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def launch_ladder(info: dict) -> list:
    """The bet ladder a load will run on: `settings.denominations` from the launch payload (or
    `settings.general.denominations`, where a server nests it and the bridge adds no alias), as
    numbers. `[]` when the payload carries none. Readable BEFORE Start."""
    s = GAME.settings(info) or {}
    d = s.get("denominations")
    if not isinstance(d, list):
        general = s.get("general")
        d = general.get("denominations") if isinstance(general, dict) else None
    return [x for x in (_ladder_num(v) for v in (d or [])) if x is not None]


async def load_until_ladder(ctx, want: list, timeout_s: float, poll_s: float) -> tuple:
    """Load the game WITHOUT Start until its launch payload carries the bet ladder `want`.

    Evidence instead of a timer: after a BO denomination change the game server picks it up over
    Kafka, after a delay that varies from run to run, so a fixed wait sometimes reopens on the OLD
    ladder. Loading without Start is safe even with a bonus owed. Leaves the game loaded and NOT
    started either way. Returns `(ready, seen_ladder, loads)`."""
    seen: list = []
    loads = 0
    t0 = ctx.now_s()
    while True:
        loads += 1
        await ctx.load_game()
        seen = launch_ladder(await read_game_info(ctx.page))
        if not want or sorted(seen) == sorted(want):
            break                     # arrived -- or nothing to wait for (BO returned no ladder)
        if ctx.now_s() - t0 >= timeout_s:
            break
        print(f"  ladder   : load {loads} still serves {seen} (want {want}) -- retrying in {poll_s}s")
        await asyncio.sleep(poll_s)
    ready = bool(want) and sorted(seen) == sorted(want)
    print(f"  ladder   : launch payload {seen} after {loads} load(s), "
          f"{round(ctx.now_s() - t0, 1)}s -- {'new ladder' if ready else 'NEVER the new ladder'}")
    return ready, seen, loads


async def click_start(page: Page, timeout_ms: int = 60000) -> None:
    """Await `startGame()`, whose contract is to resolve **the moment Start is clicked**.

    `startGame()` is the single authority for "has the game started". There is deliberately no
    second opinion polled alongside it: two independent judgements of the same fact can disagree,
    and the one that would paper over the disagreement is the one that lets a broken bridge keep
    reporting green. If this does not resolve inside the timeout, the bridge is wrong and the fix
    belongs at the source — so this raises, with a snapshot attached to say what the game looked
    like at the time.

    (A bridge that instead drives to a clean idle in here cannot fail safely. The worked example:
    Zeus v2's `waitForDeterministicStart` required `autoSpinsRemaining === 0` while a fresh session
    reported `-1`, so the promise never settled and its dismiss loop closed every popup a test
    opened. FIXED in Zeus v3 — `startGame()` now resolves at the click, which is this contract —
    but the shape of the failure is why the contract says what it does. See
    `docs/zeus-bridge-defects.md` D1.)"""
    try:
        await asyncio.wait_for(
            page.evaluate("() => window.__gameTestBridge.startGame()"), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"startGame() did not resolve within {timeout_ms}ms. Its contract is to resolve when "
            f"Start is CLICKED -- it must not wait for a clean idle or play out a resumed bonus "
            f"(docs/game-bridge-contract.md §2). Fix it at the source. Game state: {await _diag(page)}")
    except Exception as e:                                   # noqa: BLE001
        raise RuntimeError(f"startGame() rejected: {e}. Game state: {await _diag(page)}")


async def assert_idle(page: Page, timeout_ms: int = 60000) -> None:
    """Assert the game is back to a clean, spinnable base state.

    The authority is `waitForSpinClickable`, and only that. Its contract already covers every part
    of "deterministic idle" — it resolves *only* when a click would really send a bet: no animation
    running, no popup blocking, the button hit-testable, and never during autoplay or free spins.
    Re-checking `busyOps` / `visiblePopups` / `isFreeSpinMode` separately would be a second opinion
    on the same question, and would quietly become the real contract the day it disagreed.

    Never call this while a bonus is still running — it is meant to be reached only after the
    resumed rounds have been played out."""
    try:
        await wait_spin_clickable(page, timeout_ms)
    except Exception as e:                                   # noqa: BLE001
        raise RuntimeError(
            f"game did not reach a clean, spinnable base state within {timeout_ms}ms: {e}"
            f"  Game state: {await _diag(page)}")


async def wait_spin_clickable(page: Page, timeout_ms: int) -> None:
    """Wait until a spin click would really send a bet. On a game with promo dialogs the wait runs
    in slices and closes any promo dialog between them (`clear_promo_popups`): one can show at boot
    or whenever the game returns to base (after free spins), and it blocks the spin button. This is
    the one place that handles it, so every caller -- boot, settle, the next spin -- is covered."""
    if not GAME.promo_popups():
        await _await(page, "b.waitForSpinClickable(undefined, t)", timeout_ms, "waitForSpinClickable")
        return
    deadline = time.time() + timeout_ms / 1000
    while True:
        await clear_promo_popups(page)
        slice_ms = max(1, min(_PROMO_SLICE_MS, int((deadline - time.time()) * 1000)))
        try:
            await _await(page, "b.waitForSpinClickable(undefined, t)", slice_ms, "waitForSpinClickable")
            return
        except Exception:                                    # noqa: BLE001
            if time.time() >= deadline:
                raise


# ── Promo dialogs ────────────────────────────────────────────────────────────────────
# The framework NEVER claims or discards a promo. `promo_confirm` is pressed only on a
# `promo_receive` dialog that shows no `promo_cancel` -- the end-of-promo result, where confirm is
# the only way out (Grandma bridge-api.md §7).

_PROMO_SLICE_MS = 3000

_POPUPS_JS = ("() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : null;"
              " return (s && s.visiblePopups) || []; }")

_RECT_JS = ("(n) => { const b = window.__gameTestBridge;"
            " try { return b && b.getButtonRect ? b.getButtonRect(n) : null; } catch (e) { return null; } }")

# Resolves when `visiblePopups` differs from `before`, or after ~3 s.
_POPUPS_CHANGED_JS = """async (before) => {
    const b = window.__gameTestBridge;
    for (let i = 0; i < 60; i++) {
        const s = b && b.getSnapshot ? b.getSnapshot() : null;
        const now = JSON.stringify((s && s.visiblePopups) || []);
        if (now !== before) return true;
        await new Promise(r => setTimeout(r, 50));
    }
    return false;
}"""

_promo_listeners: dict = {}


def on_promo_action(page: Page, fn) -> None:
    """Where `clear_promo_popups` reports what it closed on `page` (a DriveContext puts it on its
    timeline). Without one it prints."""
    _promo_listeners[id(page)] = fn


async def clear_promo_popups(page: Page) -> list:
    """Close every promo dialog on screen without claiming or discarding anything. Closing one new
    promo can bring up the next, so it loops until none is left. Returns what it did, one line
    each. A no-op on a game without promo dialogs in its profile."""
    if not GAME.promo_popups():
        return []
    done: list = []
    for _ in range(10):
        up = [str(n) for n in (await page.evaluate(_POPUPS_JS) or [])]
        dialog = next((n for n in up if GAME.is_promo_popup(n)), None)
        if dialog is None:
            break
        if dialog == GAME.popup("promo_receive"):
            cancel = GAME.button("promo_cancel")
            # `cancel` resolves only while the dialog SHOWS it. When it does, it is the only button
            # pressed: `confirm` beside it would claim (or discard) the promo.
            if cancel and await page.evaluate(_RECT_JS, cancel):
                control, what = cancel, "closed a promo dialog with cancel (not claimed)"
            else:
                control, what = GAME.button("promo_confirm"), "closed the end-of-promo result"
        elif dialog == GAME.popup("promo_list"):
            control, what = GAME.button("promo_list_close"), "closed the promo list"
        else:
            control, what = GAME.button("promo_message_ok"), "closed a promo message"
        rect = await page.evaluate(_EXT_CLICKABLE_JS, control) if control else None
        if rect:
            await page.mouse.click(rect["centerX"], rect["centerY"])
        else:
            # dismissPopups() follows the same rule: cancel, else confirm on an end-of-promo result.
            await page.evaluate("async () => { const b = window.__gameTestBridge;"
                                " if (b && b.dismissPopups) await b.dismissPopups(); return true; }")
            what += " (via dismissPopups)"
        msg = f"PROMO: {what} [{dialog}]"
        done.append(msg)
        listener = _promo_listeners.get(id(page))
        if listener is not None:
            listener(msg)
        else:
            print(f"  {msg}")
        await page.evaluate(_POPUPS_CHANGED_JS, json.dumps(up, separators=(",", ":")))
    return done


async def play_out_promo(page: Page, timeout_ms: int, fs_timeout_ms: int, cap: int = 300) -> list:
    """Play out a promo the session is already IN (`pending.gameState == 4`): click spin until a
    round leaves the promo state. Promo spins are clicked by the player, so nothing else will end
    it. A free-spin bonus won inside the promo is drained on the way. Returns the raw responses."""
    rounds: list = []
    for _ in range(cap):
        await wait_spin_clickable(page, timeout_ms)
        await click_spin(page)
        raw = await await_bet_response(page, timeout_ms)
        rounds.append(raw)
        last = Bet.parse(raw, len(rounds) - 1)
        if is_freespin_trigger(last):
            fs = await drain_to_base(page, True, None, fs_timeout_ms, 500, assert_base=False)
            rounds.extend(fs)
            if fs:
                last = Bet.parse(fs[-1], len(rounds) - 1)
        if last.game_state != GAME.PROMO_STATE:
            return rounds
    raise RuntimeError(f"promo play-out gave up after {cap} spins with the session still in the promo state")


async def click_spin(page: Page) -> None:
    rect = await page.evaluate("() => window.__gameTestBridge.getButtonRect('spin')")
    if not rect:
        raise RuntimeError("getButtonRect('spin') returned null -- spin button unavailable")
    await page.mouse.click(rect["centerX"], rect["centerY"])


async def await_bet_response(page: Page, timeout_ms: int) -> dict:
    return await _await(page, "b.awaitBetResponse(t)", timeout_ms, "awaitBetResponse")


async def await_freespin_round(page: Page, timeout_ms: int) -> dict:
    return await _await(page, "b.awaitFreespinRound(t)", timeout_ms, "awaitFreespinRound")


async def drain_to_base(page: Page, bonus_open: bool, on_round=None,
                        fs_timeout_ms: int = 60000, cap: int = 500,
                        assert_base: bool = True) -> list:
    """Play out an in-progress bonus if one is open, and optionally assert a clean base after.

    **Two separable jobs, and only the first is unconditional.** Draining is evidence-driven and
    free when there is nothing to drain, so it always runs. Asserting the base costs a
    `waitForSpinClickable` that a scenario may legitimately be unable to satisfy — `insufficient-
    balance` deliberately ends with the out-of-money popup open, which blocks the spin button — so
    `assert_base` is opt-in and the runner exposes it as a suite key.

    **The one routine both entry points share.** Entering the game and starting a run are the same
    problem — *the client may be mid-bonus, and nothing may proceed until it is not* — and they
    differ only in how they learn that a bonus is open:

    | | how it knows |
    |---|---|
    | page load (`start_game`) | `getGameInfo().pending` — the bridge's captured launch payload |
    | between runs (`return_to_base`) | the previous run's BET response — there is no live `pending` mid-suite |

    Everything after that decision is identical, so it lives here: consume `awaitFreespinRound`
    until a round is terminal, then let `waitForSpinClickable` say whether the game is really back.

    `bonus_open` is passed in rather than discovered, because **`awaitFreespinRound` must never be
    called speculatively.** Waiting on a free-spin round that is not coming is the failure that used
    to abort suites — the caller has to have positive evidence from a response first.

    `on_round` is an async callback handed each raw response, so a caller can record the rounds into
    its own stream (the resume at boot, a teardown drain) without this routine knowing about either.

    Returns the raw responses it consumed (`[]` when nothing was open).
    """
    rounds: list = []
    if bonus_open:
        finished = False
        for _ in range(cap):
            raw = await await_freespin_round(page, fs_timeout_ms)
            rounds.append(raw)
            if on_round is not None:
                await on_round(raw)
            if is_terminal(Bet.parse(raw, len(rounds) - 1)):
                finished = True
                break
        if not finished:
            # Same reasoning as `play_out_freespins`: a bonus that outlives the budget (retriggers
            # push the counter back up, so it is not bounded by the original award) must not be
            # reported as drained. Saying so here beats a `waitForSpinClickable` timeout later,
            # which names the spin button rather than the bonus still playing behind it.
            raise RuntimeError(
                f"bonus drain gave up after {cap} rounds with free spins still owed "
                f"({Bet.parse(rounds[-1], len(rounds) - 1).free_spins} left on the counter)")
        # No `else` here. There used to be one -- a leftover from a `for ... else` -- and it raised
        # "did not reach a terminal round" on every drain that DID finish, so any load that resumed
        # an owed bonus failed after the bonus had played out correctly.
    # ⚠ ANSWER THE OFFER BEFORE CHECKING FOR IDLE. This is the shared drain — the boot resume in
    # `start_game` AND the `return-to-base` teardown both land here — and on a game with a Free
    # Spin Extension the bonus it just drained ends on a panel that locks the spin button. A
    # scenario cannot do it instead: the resumed bonus plays out INSIDE `start_game`, so by the
    # time `reload_and_start` returns, `assert_idle` has already burned its budget on a popup
    # nobody closed. No-op on every game without the feature.
    if rounds:
        await answer_extension_offer(page, expect=True)
    if assert_base:
        await assert_idle(page, fs_timeout_ms)
    return rounds


async def answer_lucky_spin(page: Page, timeout_ms: int = 30000) -> None:
    """Answer the LUCKY SPIN wheel a reloaded session is parked on (`pending.gameState == 3`),
    by KEEPING the multiplier it shows — the choice that changes nothing the next run depends on.

    Page-level for the same reason as `answer_extension_offer`: the wheel is raised inside
    `start_game`, before any scenario is driving. The wheel is clicked, never dismissed:
    `dismissPopups()` deliberately leaves it open (Joker bridge-api.md §12.5).

    Raises, naming the state, when the profile has no `lucky_keep` control or the wheel never
    becomes clickable — the session cannot reach a base game any other way, and failing here beats
    a 60 s `awaitFreespinRound` timeout that names the wrong thing."""
    keep = GAME.button("lucky_keep")
    if not keep:
        raise RuntimeError(
            "this session is parked on the Lucky Spin wheel (gameInfo.pending.gameState == 3) and "
            f"profile '{GAME.name}' has no `lucky_keep` button to answer it. Add the role to the "
            "profile, or clear the session, before running.")
    call = f"b.waitForButtonClickable({json.dumps(keep)}, undefined, {int(timeout_ms)})"
    rect = await _await(page, call, timeout_ms + 2000, f"waitForButtonClickable({keep})")
    if not rect:
        raise RuntimeError(f"waitForButtonClickable('{keep}') returned no rect")
    await page.mouse.click(rect["centerX"], rect["centerY"])


async def start_game(page: Page, timeout_ms: int = 60000, on_freespin=None,
                     fs_timeout_ms: int = 60000, cap: int = 500) -> dict:
    """Enter the game and leave it in a clean, spinnable base state, resuming any owed bonus.

    A session is not necessarily clean at load. `gameInfo.pending.freeSpins > 0` means the player
    left mid-bonus, and the client auto-plays those free spins the moment Start is clicked — no spin
    click from us, no scenario asking for them. Left unhandled they arrive as the first responses of
    whatever scenario runs first: its `base_spin()` returns a free-spin round, `conservation` fails
    on a stake it never placed, and the balance-debit case grades a spin the player did not pay for.

    The division of labour is deliberate — **the bridge reports, the framework decides**:

    | | |
    |---|---|
    | bridge | captures `gameInfo` at load; `startGame()` resolves when Start is CLICKED; answers "is spin clickable?" |
    | framework | reads that `gameInfo`, decides a resume is owed, plays it out from the API responses, and asserts the idle |

    Four steps, and **each has exactly one authority** — nothing here is cross-checked against a
    second signal, because two judgements of one fact can disagree and the tie-break would silently
    become the real contract:

    1. **`startGame()`** — the authority for "the game started". Bounded; on timeout it raises,
       because a bridge that cannot resolve it is broken and the fix belongs at the source.
    2. **`getGameInfo()`** — the authority for "does this session owe a bonus". The bridge captured
       it at load, so the Start click has not invalidated it.
    3. **`awaitFreespinRound()`** — the authority for each resumed round, played out until one is
       terminal (`freeSpins == 0 OR gameState == base`; both, because `hasReachedMaxWin` truncates).
    4. **`waitForSpinClickable()`** — the authority for "back to a clean idle". Its contract already
       covers no-animation, no-popup, not-in-a-bonus, so nothing else is re-checked.

    `on_freespin` is an async callback given each resumed round's RAW response. The rounds are real
    server results and some cases want them graded (`freespin-zero-balance` asserts stake 0 across
    exactly these) — but they belong to whoever asked for the reload, not to `start_game`, so it
    hands them over rather than recording them itself.

    Returns {game_info, game_info_raw, pending, pending_free_spins, resumed, resumed_terminal}.
    """
    await click_start(page, timeout_ms)                      # 1: resolves at the Start CLICK
    info_raw = await page.evaluate(_GAME_INFO_JS) or {}      # 2: the bridge's captured copy
    info = GAME.game_info(info_raw)
    GAME.remember_launch(info)                               # max-win settings for this session
    owed = GAME.pending_free_spins(info)
    # What the session waits on decides HOW it is cleared (server API doc §06 reload order). Only
    # an auto-playing bonus is drained; the wheel and the offer wait on a click, and draining them
    # waits for free-spin rounds that never come.
    resume = GAME.pending_resume(info)
    state = GAME.pending(info).get(GAME.F_PENDING_GAME_STATE or GAME.F_GAME_STATE)
    bonus_open = resume == "freespins"
    if resume == "freespins":
        print(f"  gameInfo.pending: {owed} free spin(s) owed (gameState={state!r}) -- the client "
              f"is auto-playing them; playing out from the BET responses before any scenario runs.")
    elif resume == "lucky_spin":
        print("  gameInfo.pending: a bought bonus is parked on the LUCKY SPIN wheel "
              "(gameState=3) -- keeping the shown multiplier, then playing the bonus out.")
        await answer_lucky_spin(page, timeout_ms)
        bonus_open = True                   # the wheel is answered: the bought spins play themselves
    elif resume == "extension":
        print("  gameInfo.pending: the last free spin ended on the EXTENSION offer "
              "(canBuyExtension) -- declining it before the idle check.")
        await answer_extension_offer(page, expect=True)
    elif resume == "promo":
        print("  gameInfo.pending: a free-game PROMO is running (gameState=4) -- clicking its "
              "spins until it ends, before any scenario runs.")
        promo = await play_out_promo(page, timeout_ms, fs_timeout_ms)
        print(f"  played out {len(promo)} promo round(s) at load")
    if GAME.promo_offered(info):
        print("  gameInfo.needToStartPromo: the client will OFFER a promo. The framework never "
              "accepts one; the idle check below closes its dialog with cancel.")
    # 3 + 4: the shared core -- play the bonus out, then assert the base. Identical to what a run
    # start does; only the evidence that a bonus is open differs (pending here, last_bet there).
    resumed = await drain_to_base(page, bonus_open, on_freespin, fs_timeout_ms, cap)
    if bonus_open:
        print(f"  played out {len(resumed)} resumed free-spin round(s) at load")
    return {"game_info": info, "game_info_raw": info_raw, "pending": GAME.pending(info),
            "pending_free_spins": owed, "pending_resume": resume, "resumed": resumed,
            "resumed_terminal": bool(resumed) or None}


# ---------------------------------------------------------------------------
# DriveContext — what a scenario is handed. Records replay events + timeline as
# it drives, so the closed box's outputs plug straight into the replay pipeline.
# ---------------------------------------------------------------------------
class DriveContext:
    def __init__(self, page: Page, video_start_time: float,
                 spin_timeout_ms: int = 60000, bet_timeout_ms: int = 15000, fs_timeout_ms: int = 60000,
                 auto_timeout_ms: int = 90000,
                 settle_pad_ms: int = 500, ui_dwell_ms: int = 2000, step_dwell_ms: int = 1000,
                 wallet=None, browser=None, url=None, bo=None,
                 relaunch=None, language: str | None = None, shots_dir=None):
        self.page = page
        self.video_start_time = video_start_time
        self.spin_timeout_ms = spin_timeout_ms
        self.bet_timeout_ms = bet_timeout_ms
        self.fs_timeout_ms = fs_timeout_ms
        # How long to wait for a round the CLIENT fires (auto-play). Not the same problem as
        # bet_timeout_ms: there WE click and the response follows promptly, so 15s is generous.
        # Here the client sets the pace -- cascades, count-ups, celebrations all sit between one
        # auto spin and the next. Measured live on Mystic: gaps of 4-9s typically, but 14.3s and
        # 37.1s in a sample of 8, i.e. 15s times out on a perfectly healthy run.
        self.auto_timeout_ms = auto_timeout_ms
        self.settle_pad_ms = settle_pad_ms
        self.ui_dwell_ms = ui_dwell_ms   # after each UI-change click, hold this long so the change
                                         # lands + the AI can see it. 2s: the video montage samples ONE
                                         # composite per second, so 2s is already 2 frames. Longer buys
                                         # no extra evidence and costs runtime + tokens on EVERY dwell,
                                         # which multiplies (languages x surfaces).
        self.step_dwell_ms = step_dwell_ms
        # ...and this for an INTERMEDIATE step of a multi-step interaction: stepping the bet through
        # four denominations, opening a popup on the way to a control inside it. Those change the
        # screen, so `dwell_ms=0` makes them invisible -- the montage samples once a second, and a
        # step with no dwell can fall entirely between two frames. 1s guarantees a frame each while
        # costing half of a full settle dwell. Reserve `dwell_ms=0` for steps whose whole point is
        # that they are back-to-back (the Mah_108 popup burst).
        self._wallet = wallet        # optional framework WalletHelper (Playsite balance set)
        # The wallet value as it was BEFORE this run first changed it, so a teardown can put
        # it back exactly. Set once, by `set_balance`/`deposit` — see `restore_balance`.
        self._wallet_was = None
        self._browser = browser      # Playwright Browser — lets a scenario open a SECOND session (multi-tab)
        self._url = url              # the game URL (same account) for that second session
        self._bo = bo                # optional BoHelper — a scenario that changes BO config mid-run
        # How to put the agent's denomination row back after this run swapped it: the id and
        # override it had, plus the MASTER id (the only one BO accepts when clearing an override).
        # None means the run never swapped, and `reset-denomination` then does nothing rather than
        # clearing an override some other run legitimately set (mirrors `_wallet_was`).
        self._denom_was: dict | None = None
        self._second = None          # (context, page) of the second session, if opened
        self._relaunch = relaunch    # runner callback: async (lang) -> url. Resolves a NEW launch URL
                                     # in that language and re-enters the game (see set_language)
        self.language = language     # language the game is CURRENTLY loaded in (runner-supplied)
        self._shots_dir = Path(shots_dir) if shots_dir else None   # where screenshot() writes
        self.shots: list = []        # [{label, file}] in capture order -> ScenarioResult.shots
        self._popup_task = None      # background visiblePopups poll (see start_popup_watch)
        self._popup_seen: list = []
        self._popup_max = 0
        self._popup_samples = 0
        self._popup_stop = True
        self.events: list = []       # replay events (t is seconds since video start)
        # RAW payloads for the replay's event inspector (bet / free-spin responses, gameInfo at each
        # load). Kept OUT of `events`: a round's `seq` is `len(events)`, so anything else appended
        # there would shift every round index. The runner merges both into the replay.
        self.api_events: list = []
        self.timeline: list = []     # replay step bar
        self.round_starts: list = [] # video-relative start time per recorded round (indexed by seq)
        self.last_bet: Optional[Bet] = None   # last recorded BET response (server truth; read by teardown)
        # A promo dialog closed on this page goes on the run's timeline, so the report shows it.
        def _promo_mark(msg: str) -> None:
            print(f"  {msg}")
            self.mark(msg)
        on_promo_action(page, _promo_mark)

    def now_s(self) -> float:
        return time.time() - self.video_start_time

    def log_api(self, kind: str, data, t: float | None = None) -> None:
        """Add a raw payload to the replay's event inspector (see `api_events`)."""
        self.api_events.append({"t": round(self.now_s() if t is None else t, 3), "type": kind,
                                "data": data})

    async def capture_game_info(self, when: str) -> None:
        """Log the bridge's captured launch payload (`getGameInfo()`, raw) to the replay. Best-effort."""
        try:
            raw = await self.page.evaluate(_GAME_INFO_JS)
        except Exception:  # noqa: BLE001 -- a replay aid must never fail a run
            return
        if raw:
            self.log_api("game_info", {"when": when, "gameInfo": raw})

    def _record(self, b: Bet, phase: str, t0: float) -> None:
        # Base + auto rounds come from awaitBetResponse; every other phase (freespin, teardown
        # drain, a resumed bonus) is a free-spin round from awaitFreespinRound.
        self.log_api("bet_response" if phase in ("base", "auto") else "freespin_response",
                     {"seq": b.seq, "phase": phase, "response": b.raw}, t0)
        b.phase = phase
        self.last_bet = b        # single source of truth for teardown: the latest server response
        self.round_starts.append(round(t0, 3))
        self.events.append({"t": round(t0, 3), "type": "spin_result", "data": b.row()})
        self.timeline.append({"id": f"{phase}-{b.seq}", "action": phase, "description": phase,
                              "start_ms": int(t0 * 1000), "end_ms": int(self.now_s() * 1000),
                              "result": b.row()})

    def windows(self) -> list:
        """Per-round video sub-windows, indexed by round seq. Round i spans from its own spin
        start to the next round's start (the last round runs to now, i.e. through settle)."""
        end = self.now_s()
        ws = []
        for i, s in enumerate(self.round_starts):
            e = self.round_starts[i + 1] if i + 1 < len(self.round_starts) else end
            ws.append((s, round(e, 3)))
        return ws

    async def base_spin(self) -> Bet:
        """One real base spin: wait clickable -> coordinate click -> await the response."""
        await wait_spin_clickable(self.page, self.spin_timeout_ms)
        t0 = self.now_s()
        await click_spin(self.page)
        b = Bet.parse(await await_bet_response(self.page, self.bet_timeout_ms), len(self.events))
        self._record(b, "base", t0)
        await skip_big_win(self, b)
        return b

    async def free_spin(self, phase: str = "freespin") -> Bet:
        """One auto-played free-spin round: passive await (no click). `phase` tags the record so a
        teardown drain (phase='teardown') is distinguishable from a scenario's own free spins."""
        t0 = self.now_s()
        b = Bet.parse(await await_freespin_round(self.page, self.fs_timeout_ms), len(self.events))
        self._record(b, phase, t0)
        await skip_big_win(self, b)
        return b

    async def observe_bet(self, timeout_ms: int | None = None, phase: str = "auto") -> Bet:
        """Await the next server BET response WITHOUT clicking — for client-driven auto-play,
        where the client fires each spin and we just collect the results in order.

        Defaults to `auto_timeout_ms`, not `bet_timeout_ms`: the gap between two auto spins is the
        client's presentation, not a server round-trip, and it is routinely longer than a clicked
        spin's response.

        `phase` tags what the round IS, not how we learned about it. It defaults to "auto" because
        that is the usual caller, but a round the SCENARIO caused by some gesture other than a
        click — a Space press, say — is a paid BASE round and must be tagged `phase="base"`, or
        `base_rounds_since` will not count it. That mismatch made CR_041's
        `{facts.key_rounds} == 1` unpassable: the Space spin really happened, the AI could see it
        on the clip, and the count was still 0 because the round was filed under "auto"."""
        t0 = self.now_s()
        b = Bet.parse(await await_bet_response(self.page,
                                               timeout_ms if timeout_ms is not None else self.auto_timeout_ms),
                      len(self.events))
        self._record(b, phase, t0)
        await skip_big_win(self, b)
        return b

    async def _remember_wallet(self) -> None:
        """Record the wallet as it is RIGHT NOW, once per run, before anything changes it.

        Read from the wallet API rather than from the game's on-screen balance: the client's figure
        can lag a deposit made moments earlier, and restoring to a stale number is how the balance
        drifts. Best-effort — a wallet that cannot be read must not fail the test that was about to
        change it; `restore_balance` then reports that it has nothing to restore to."""
        if self._wallet is None or self._wallet_was is not None:
            return
        try:
            self._wallet_was = float(await self._wallet.get_balance())
        except Exception:  # noqa: BLE001
            self._wallet_was = None

    async def set_balance(self, amount: float,
                          attempts: int = 6, pause_s: float = 1.0) -> None:
        """Set the player's wallet to an absolute amount via the Playsite wallet API.
        Raises if no wallet is configured (PLAYSITE_API_URL/USER/PASS).

        `attempts`/`pause_s` bound the retry the wallet helper uses to ride out a server-side
        hold on the balance. Only widen it where nothing can play while it waits."""
        if self._wallet is None:
            raise RuntimeError("wallet not configured -- set PLAYSITE_API_URL/USER/PASS in .env")
        await self._remember_wallet()
        await self._wallet.set_balance(amount, attempts=attempts, pause_s=pause_s)

    async def deposit(self, amount: float) -> None:
        """Deposit a whole-unit amount INTO the player's wallet (adds to the current balance)
        via the Playsite wallet API. Raises if no wallet is configured."""
        if self._wallet is None:
            raise RuntimeError("wallet not configured -- set PLAYSITE_API_URL/USER/PASS in .env")
        await self._remember_wallet()
        await self._wallet.deposit(amount)

    @property
    def url(self) -> str:
        """The game URL this run is playing (kept current across a language relaunch). Needed to
        come back after `stop_client()`."""
        return self._url

    async def stop_client(self) -> None:
        """TEAR THE GAME DOWN so it cannot play another round, without ending the run.

        The client AUTO-PLAYS a free-spin feature ("the client is auto-playing them" — what the
        framework reports when it finds an owed bonus at load). Anything a scenario does between two
        rounds therefore races those spins: `set_balance` is a round trip to the Playsite API, and
        while it is in flight the bonus can finish, or hit the max-win cap, so the state the scenario
        thought it was setting up no longer exists by the time it acts.

        Navigating away destroys the client, so no further round can be played. The bonus lives on
        the SERVER (`gameInfo.pending`), so it survives and `reload_and_start(url=...)` resumes it.
        Use this — not a bare sleep — before any out-of-band change that must land against a known
        state. The page object and the video recording are untouched."""
        t0 = self.now_s()
        await self.page.goto("about:blank", wait_until="load")
        self._step("stop", "client torn down (about:blank) so no further round can play", t0)

    async def reload_and_start(self, phase: str = "freespin", url: str | None = None,
                               on_ready=None) -> list:
        """Reload the page (so the client re-reads the wallet) and re-enter the game.

        `url` navigates there instead of reloading — needed after `stop_client()`, where the current
        page is `about:blank` and a plain reload would just re-load that. Pass the game URL
        (`ctx.url`).

        Returns the free-spin rounds the RELOAD itself resumed — a reload with a bonus still owed
        replays it automatically on START (see `start_game`). They are recorded into this run's
        stream under `phase` so the invariants grade them like any other free spin; a scenario that
        reloads mid-bonus should use this list rather than draining again afterwards, since
        `start_game` has already consumed those responses."""
        if url:
            await self.page.goto(url, wait_until="networkidle")
        else:
            await self.page.reload(wait_until="networkidle")
        await self.page.wait_for_function("() => !!window.__gameTestBridge", timeout=60000)
        # ⚠ The only moment some evidence exists. `gameInfo.pending` is the resume signal and the
        # client CLEARS it the instant Start is clicked (it auto-plays the owed bonus), and a
        # "did any control flash enabled while the bonus was owed?" watcher has to be running
        # BEFORE the first frame the player could have seen. Both need the gap between the bridge
        # appearing and Start — which is exactly here.
        await self.capture_game_info("reload")
        if on_ready is not None:
            await on_ready()
        resumed: list = []

        async def _capture(raw: dict) -> None:
            t0 = self.now_s()
            b = Bet.parse(raw, len(self.events))
            self._record(b, phase, t0)
            resumed.append(b)

        # bounded: a lost promise must not hang the suite
        await start_game(self.page, on_freespin=_capture)
        return resumed

    async def fresh_session(self, why: str) -> bool:
        """Re-enter the game on a NEW launch token, for a run that ended the session ON PURPOSE.

        `reconnect-long` and `multi-tab-session` both end with the client on "session expired / go
        home" — that popup IS their case. Left as it is, the NEXT run inherits a dead client: on
        2026-10-01 `reconnect-during-auto` started on Grandma's "Oops! Your session has expired",
        auto-play froze at 10, and TC-044 went red for something the game never did. A plain reload
        would reuse the expired token, so this mints a fresh one through the runner (`relaunch`),
        falling back to reloading the run's URL. Call it AFTER the evidence is in the result: the
        recorded window must end on the popup, not on the fresh game.

        Best-effort: returns False instead of raising, and the next run's PREPARE still checks the
        base for itself."""
        t0 = self.now_s()
        try:
            if self._relaunch is not None:
                self._url = await self._relaunch(self.language)
            else:
                await self.reload_and_start(url=self._url)
        except Exception as e:  # noqa: BLE001
            print(f"  ! fresh session after {why} failed: {type(e).__name__}: {e}")
            return False
        await self.capture_game_info(f"fresh session ({why})")
        self._step("fresh_session", f"re-entered the game on a new launch token ({why})", t0)
        return True

    async def set_automation_denomination(self, settle_s: float = 5.0) -> dict:
        """Put the agent's game row on the automation bet ladder (11, 22, 33) in BO. BO only.

        The game is not touched. A denomination is read from the launch payload, so it takes effect
        on the next load: close the game first (`stop_client`) and open it again afterwards
        (`open_game` / `reload_and_start`).

        ⚠ RULE 20. BO answering `200` means BO stored the change, NOT that the game server has it:
        BO publishes it on a Kafka event and the game server applies it when that event lands. So
        this WAITS `settle_s` (5 s on Dragon VIP dev) after the write, before anyone can reopen.
        The wait is a guess and never the evidence -- the caller must still read the ladder back
        from the reopened client and assert it is the new one. A stale read there is a propagation
        problem, not a game defect, and it is dangerous precisely because it looks like a pass:
        the old bet on screen is what a "the bonus kept its stake" case wants to see.

        Records the row's previous denomination for `teardown: reset-denomination`, which the run
        MUST declare -- the ladder is shared state.

        Returns the dict from `BoHelper.ensure_automation_denomination` (`denomination_id`, `name`,
        `created`, `sub_category_id`, `previous_denomination_id`, `bet_levels`,
        `default_bet_level`, `default_bet`), or `{}` when BO is not configured.
        """
        if self._bo is None:
            return {}
        info = await self._bo.ensure_automation_denomination()
        if self._denom_was is None:                                # first change of the run wins
            prev, automation = info.get("previous_denomination_id"), info.get("denomination_id")
            self._denom_was = {
                "id": prev,
                "override": info.get("previous_is_denom_override"),
                "master": info.get("master_denomination_id"),
                # A row ALREADY on the automation ladder means an earlier run's teardown failed.
                # "Previous" is then this very ladder, and restoring it would be both rejected by
                # BO and wrong, so the teardown falls back to the master.
                "leaked": prev is not None and prev == automation,
            }
        if settle_s:
            await asyncio.sleep(settle_s)      # Rule 20: let the Kafka event reach the game server
        return info

    async def open_game(self, url: str | None = None) -> None:
        """Load the game and click Start. Nothing more.

        Unlike `reload_and_start`, this does NOT play out an owed bonus and does NOT wait for the
        base game. Use it when the scenario itself wants to watch a resumed bonus: read `pending`
        with `read_game_info`, then take the rounds with `ctx.free_spin()`. If a bonus is owed and
        the scenario does not play it out, the game is left mid-bonus.

        `url` defaults to the run's game URL (`ctx.url`), which is also what is needed after
        `stop_client()`, where the page is `about:blank`.
        """
        t0 = self.now_s()
        await self.load_game(url)
        await click_start(self.page)
        self._step("open", "game loaded and Start clicked (any owed bonus is left to the scenario)", t0)

    async def load_game(self, url: str | None = None) -> None:
        """Load the game and wait for the bridge — WITHOUT clicking Start.

        Safe with a bonus owed: the client plays `pending` only once Start is clicked, and the
        bridge has already captured the launch payload (`getGameInfo()`), so what this load will
        run on can be READ before committing to it. `open_game` is this plus the Start click."""
        await self.page.goto(url or self._url, wait_until="networkidle")
        await self.page.wait_for_function("() => !!window.__gameTestBridge", timeout=60000)
        await self.capture_game_info("load")

    async def settle(self) -> None:
        """Wait for the CLIENT to finish presenting the last round (cascades, payout count-up,
        big-win / total-win celebration, return to base) so the recorded video window covers the
        whole animation for display: asserts. The server response arrives BEFORE the animation;
        'spin clickable again' is the settle signal — waitForSpinClickable resolves only once no
        tracked animation is running and celebrations are cleared. A short pad leaves a clean
        settled tail in frame; a fallback covers the rare case readiness can't be confirmed."""
        t0 = self.now_s()
        try:
            await wait_spin_clickable(self.page, self.spin_timeout_ms)
        except Exception:
            await asyncio.sleep(3)                        # fallback: heuristic settle pad
        if self.settle_pad_ms:
            await asyncio.sleep(self.settle_pad_ms / 1000)
        self.timeline.append({"id": f"settle-{len(self.timeline)}", "action": "settle",
                              "description": "presentation settle",
                              "start_ms": int(t0 * 1000), "end_ms": int(self.now_s() * 1000),
                              "result": None})

    async def eval(self, expr: str):
        """Read a config/UI value the scenario needs internally (denominations, bet index, ...).
        Allowed for UI scenarios that place no bet — it reads config, not per-game game-STATE."""
        return await self.page.evaluate(expr)

    async def dwell(self, ms: int | None = None) -> None:
        """Hold for `ms` (default self.ui_dwell_ms, 2s) so the last UI change settles on screen
        and the AI can see it in the recorded clip. `ms=0` skips."""
        d = self.ui_dwell_ms if ms is None else ms
        if d and d > 0:
            await asyncio.sleep(d / 1000)

    async def click_button(self, name: str, optional: bool = False, timeout_ms: int | None = None,
                           dwell_ms: int | None = None) -> bool:
        """Wait for a named bridge button to be clickable, issue a REAL coordinate click, then DWELL
        (2s, self.ui_dwell_ms) so the resulting UI change settles and the AI can see it in the clip.
        `optional` -> return False instead of raising if it never becomes clickable. `dwell_ms=0`
        skips the hold (e.g. rapid intermediate steps). Records a timeline entry for the replay.

        `name` is translated through the active profile (`GAME.resolve`), so a scenario may spell a
        control in the reference dialect and still address THIS build's control. A role that
        resolves to `None` means the build has no such control: treated as not-clickable, which is
        `False` when `optional` — and never a timeout burned on a button that cannot exist."""
        t0 = self.now_s()
        name = GAME.resolve(name)
        if name is None:
            if optional:
                return False
            raise RuntimeError("this build has no such control (profile maps the role to None)")
        tmo = timeout_ms if timeout_ms is not None else self.spin_timeout_ms
        if not await self._open_via(name, tmo, optional):     # control inside a panel: open it
            return False
        call = f"b.waitForButtonClickable({json.dumps(name)}, undefined, {int(tmo)})"
        try:
            rect = await _await(self.page, call, tmo + 2000, f"waitForButtonClickable({name})")
        except Exception:
            if optional:
                return False
            raise
        if not rect:
            if optional:
                return False
            raise RuntimeError(f"waitForButtonClickable('{name}') returned no rect")
        await self.page.mouse.click(rect["centerX"], rect["centerY"])
        await self.dwell(dwell_ms)                        # let the UI change land + be seen by the AI
        self.timeline.append({"id": f"click-{len(self.timeline)}", "action": "click",
                              "description": f"click {name}",
                              "start_ms": int(t0 * 1000), "end_ms": int(self.now_s() * 1000),
                              "result": {"button": name}})
        return True

    async def scroll_bet_row(self, i: int, dwell_ms: int | None = None) -> None:
        """Scroll denomination row `i` into view (for a scrollable bet list), then DWELL so it lands
        in frame for the AI. i is the denomination index (0 = lowest bet)."""
        t0 = self.now_s()
        try:
            await self.page.evaluate("(i) => window.__gameTestBridge.scrollBetRowIntoView(i)", i)
        except Exception:
            pass
        await self.dwell(dwell_ms)                        # dwell so the scrolled-to row is seen
        self.timeline.append({"id": f"scroll-{len(self.timeline)}", "action": "scroll",
                              "description": f"scroll bet row {i} into view",
                              "start_ms": int(t0 * 1000), "end_ms": int(self.now_s() * 1000),
                              "result": {"row": i}})

    def _step(self, action: str, desc: str, t0: float, result=None) -> None:
        self.timeline.append({"id": f"{action}-{len(self.timeline)}", "action": action,
                              "description": desc, "start_ms": int(t0 * 1000),
                              "end_ms": int(self.now_s() * 1000), "result": result})

    def mark(self, label: str) -> None:
        """Record a labeled timeline MARKER at the current moment. In a timestamp-mode video assert it
        surfaces in the ACTION TIMELINE ('<label>: at t=Xs'), so the AI can pin exactly WHEN a
        described action happened instead of guessing which of several events a prompt bullet means."""
        self._step("mark", label, self.now_s())

    async def _rect(self, name: str, timeout_ms: int | None, optional: bool):
        name = GAME.resolve(name)                     # reference dialect -> this build's control
        if name is None:
            if optional:
                return None
            raise RuntimeError("this build has no such control (profile maps the role to None)")
        tmo = timeout_ms if timeout_ms is not None else self.spin_timeout_ms
        if not await self._open_via(name, tmo, optional):     # control inside a panel: open it
            return None
        call = f"b.waitForButtonClickable({json.dumps(name)}, undefined, {int(tmo)})"
        try:
            rect = await _await(self.page, call, tmo + 2000, f"waitForButtonClickable({name})")
        except Exception:
            if optional:
                return None
            raise
        if not rect and not optional:
            raise RuntimeError(f"waitForButtonClickable('{name}') returned no rect")
        return rect

    async def _open_via(self, name: str, tmo: int, optional: bool) -> bool:
        """A control that lives INSIDE a panel (profile `BUTTON_VIA`, e.g. Grandma's History inside
        Settings) is reached by opening that panel first, when it is not already on screen. Both
        `click_button` and `_rect` (hold / double-click) call this, so no scenario has to know the
        game has a menu. Returns False only when the opener is optional and could not be clicked.
        `name` must already be resolved."""
        opener = GAME.via(name)
        if not opener or await self._on_screen(name):
            return True
        t0 = self.now_s()
        opener_rect = await self._rect(opener, tmo, optional)   # recursive: nested menus work
        if opener_rect is None:
            return False
        await self.page.mouse.click(opener_rect["centerX"], opener_rect["centerY"])
        self._step("click", f"open {GAME.resolve(opener)} to reach {name}", t0,
                   {"button": GAME.resolve(opener), "to_reach": name})
        await self.dwell(self.step_dwell_ms)          # let the panel open, and be seen opening
        return True

    async def _on_screen(self, name: str) -> bool:
        """Is the control clickable RIGHT NOW? An instant `getButtonRect` probe, no waiting."""
        r = await self.ui_read(
            "(n) => { const b = window.__gameTestBridge;"
            " return b && b.getButtonRect ? b.getButtonRect(n) : null; }", None, arg=name)
        return bool(r and r.get("interactable") and r.get("visible"))

    async def hold_button(self, name: str, hold_ms: int = 1000, use_bridge: bool = False,
                          optional: bool = False, timeout_ms: int | None = None) -> bool:
        """Press-and-HOLD a button for `hold_ms` (for hold-to-turbo / hold-to-auto-spin). Emulates a
        REAL pointer hold (mouse.down -> wait -> mouse.up), then dwells so the effect is on screen.

        `use_bridge=True` asks for the bridge's own hold instead, and is now BEST-EFFORT: it is
        probed for and silently falls back to the pointer hold when the build has no such call.
        Two reasons, and the second is the instructive one.

        * Zeus v3 REMOVED `holdSpinStart()` / `holdSpinRelease()` (games/zeus/bridge-api.md §12) —
          a real pointer hold is what a player does, and no case used the bridge form.
        * It would not have worked anyway. This called `holdSpinEnd()`; the API has always spelled
          it `holdSpinRelease`. **A documented escape hatch that had never been executed**, so the
          wrong name sat here unnoticed until the day someone reached for it in a crisis. An
          untested fallback is not a fallback. Both spellings are accepted below, but the real fix
          for a failing hold is the gesture, not a different API.
        """
        t0 = self.now_s()
        rect = await self._rect(name, timeout_ms, optional)
        if rect is None:
            return False
        release = await self.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " if (!b || typeof b.holdSpinStart !== 'function') return null;"
            " if (typeof b.holdSpinRelease === 'function') return 'holdSpinRelease';"
            " if (typeof b.holdSpinEnd === 'function') return 'holdSpinEnd';"
            " return null; }") if use_bridge else None
        if use_bridge and not release:
            self.mark(f"HOLD {name}: no bridge hold on this build -- using a real pointer hold")
        if release:
            await self.page.evaluate("() => window.__gameTestBridge.holdSpinStart()")
            await asyncio.sleep(hold_ms / 1000)
            await self.page.evaluate(f"() => window.__gameTestBridge.{release}()")
        else:
            await self.page.mouse.move(rect["centerX"], rect["centerY"])
            await self.page.mouse.down()
            await asyncio.sleep(hold_ms / 1000)
            await self.page.mouse.up()
        await self.dwell()
        self._step("hold", f"hold {name} for {hold_ms}ms{' (bridge)' if release else ''}", t0,
                   {"button": name, "hold_ms": hold_ms})
        return True

    async def double_click_button(self, name: str, optional: bool = False,
                                  timeout_ms: int | None = None) -> bool:
        """Double-click a button via real pointer events (for double-click sudden-stop)."""
        t0 = self.now_s()
        rect = await self._rect(name, timeout_ms, optional)
        if rect is None:
            return False
        await self.page.mouse.dblclick(rect["centerX"], rect["centerY"])
        await self.dwell()
        self._step("dblclick", f"double-click {name}", t0, {"button": name})
        return True

    async def hold_key(self, key: str = " ", hold_ms: int = 1000) -> None:
        """Press-and-HOLD a keyboard key for `hold_ms` (e.g. Space for hold-to-turbo)."""
        t0 = self.now_s()
        await self.page.keyboard.down(key)
        await asyncio.sleep(hold_ms / 1000)
        await self.page.keyboard.up(key)
        await self.dwell()
        self._step("hold_key", f"hold '{key}' for {hold_ms}ms", t0, {"key": key, "hold_ms": hold_ms})

    async def press_key(self, key: str = " ") -> None:
        """Tap a keyboard key once (e.g. Space to spin)."""
        t0 = self.now_s()
        await self.page.keyboard.press(key)
        self._step("key", f"press '{key}'", t0, {"key": key})

    async def set_offline(self, offline: bool) -> None:
        """Drop / restore the page's network via Playwright (CDP) — for disconnect/reconnect tests."""
        t0 = self.now_s()
        await self.page.context.set_offline(offline)
        self._step("net", "network offline" if offline else "network online", t0, {"offline": offline})

    async def open_second_session(self):
        """Open a SECOND browser session (fresh context, SAME account URL) and enter the game — for the
        'same account on two tabs' test. Being a separate context, it re-authenticates with the same
        account token, so the server should invalidate THIS (first) session. Returns the second page.
        Needs the runner to have passed browser + url into the DriveContext."""
        if self._browser is None or not self._url:
            raise RuntimeError("second session needs browser + url (runner must pass them to DriveContext)")
        t0 = self.now_s()
        ctx2 = await self._browser.new_context(viewport=VIEWPORT)
        page2 = await ctx2.new_page()
        await page2.goto(self._url, wait_until="networkidle")
        await page2.wait_for_function("() => !!window.__gameTestBridge", timeout=60000)
        await start_game(page2)      # bounded, like the main session
        self._second = (ctx2, page2)
        self._step("second_session", "open second session (same account, new context)", t0)
        return page2

    async def close_second_session(self) -> None:
        """Close the second session if one was opened (idempotent)."""
        if self._second is not None:
            ctx2, _ = self._second
            try:
                await ctx2.close()
            except Exception:  # noqa: BLE001
                pass
            self._second = None

    # -- UI / popup observation (Mah_105-108) --------------------------------------------------
    # These read the SCREEN, which is legitimate ONLY because the subject of a popup case IS the
    # UI and no bet is placed. Game LOGIC still comes exclusively from the server BET response
    # (base_spin / observe_bet) -- never from these.

    # A JS arrow that DECLARES a parameter: `(n) => ...` or `async (opts) => ...`, but not `() =>`.
    _JS_TAKES_ARG = re.compile(r"\s*(?:async\s*)?\(\s*[A-Za-z_$][\w$]*\s*(?:,|\))\s*=>|"
                               r"\s*(?:async\s*)?\(\s*[A-Za-z_$][\w$]*\s*\)\s*=>")

    async def ui_read(self, js: str, default=None, arg=None):
        """Best-effort bridge read for a UI-only scenario. Returns `default` when the call throws
        (older bridge, getter not implemented) instead of failing the whole run.

        ⚠ Pass `arg` when the JS takes a parameter. Forgetting is not a small mistake: the function
        runs with `undefined`, the bridge answers about a control named `undefined`, and the result
        is a confident WRONG answer rather than an error. It has happened twice — a poll that
        always timed out, and a `canDo` that reported `not_available:null` about a control that was
        fine — so a parameterised JS with no `arg` now raises instead of lying.
        """
        if arg is None and self._JS_TAKES_ARG.match(js):
            raise RuntimeError(
                "ui_read() was given JS that declares a parameter but no `arg` to fill it, so it "
                "would run with `undefined`. Pass `arg=...`, or drop the parameter from the JS.\n"
                f"  js: {js[:120]}...")
        try:
            return await (self.page.evaluate(js, arg) if arg is not None
                          else self.page.evaluate(js))
        except Exception:  # noqa: BLE001
            return default

    async def popups(self, include_promo: bool = False) -> list:
        """Labels of the popups on screen right now (bridge `getSnapshot().visiblePopups`). Promo
        dialogs are left out unless `include_promo`: one can show at any idle moment, no scenario
        here is about it, and `wait_spin_clickable` closes it."""
        found = await self.ui_read(
            "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : null;"
            " return (s && s.visiblePopups) || []; }", []) or []
        return found if include_promo else [p for p in found if not GAME.is_promo_popup(p)]

    async def has_button(self, name: str) -> bool:
        """Does this BUILD have the control at all? An instant `getButtonRect` probe (`null` = the
        button does not exist here). Use it to skip a missing feature with NO delay —
        `waitForButtonClickable` burns its whole timeout on an absent button, which is fatal inside
        a rapid burst where the point is that the clicks land back-to-back.

        The name is profile-translated first, and a role this build maps to `None` answers `False`
        without a round trip."""
        name = GAME.resolve(name)
        if name is None:
            return False
        rect = await self.ui_read(
            f"() => {{ const b = window.__gameTestBridge;"
            f" return b && b.getButtonRect ? b.getButtonRect({json.dumps(name)}) : null; }}")
        return bool(rect and rect.get("visible"))

    async def dismiss_popups(self) -> list:
        """Clear the screen so the next run starts clean. Returns the labels the bridge VERIFIED
        closed (it is async because popups fade out)."""
        return await self.ui_read(
            "() => { const b = window.__gameTestBridge; return b && b.dismissPopups ? b.dismissPopups() : []; }",
            []) or []

    async def history_detail_state(self) -> dict:
        """`getHistoryDetailState()` -> {open, betIndex, betCount, hasPrev, hasNext}.

        The loop control for paging a bet ticket. In a free-spin round EACH FREE SPIN IS ITS OWN BET,
        so next/prev walks the free-spin chain. Drive on `hasNext`, never on page-count arithmetic:
        the popup's own nav buttons account for the synthetic max-win page, so counting would be off
        by one exactly on the rounds that matter."""
        return await self.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getHistoryDetailState ? b.getHistoryDetailState() : null; }") or {}

    async def wait_for_history_rows(self, timeout_ms: int = 10000, poll_ms: int = 250) -> list:
        """Wait until the History list has actually RENDERED rows, then return them.

        Opening the panel only starts an API fetch; the list arrives after it. A fixed dwell is the
        floor, not the guarantee — on a slow response it expires with the list still empty and the
        scenario reads []. So dwell first (so the panel is on screen for the video), then poll here:
        it returns the instant rows exist and only spends the full timeout when there genuinely are
        none. Returns [] on timeout, which callers treat as unreached rather than as an empty pass.

        **A build with no `getHistoryRows` returns [] IMMEDIATELY, not after the timeout.** Waiting
        needs something that can arrive: `history_rows()` answers [] both for "not fetched yet" and
        for "this bridge has no enumerator", and only the first is worth waiting on. Same rule
        `click_button` already applies to a role that resolves to None — never burn a timeout on
        something that cannot happen.

        (This guard is defensive, not a fix for any current build. The old `bridge-api-v2.md`
        listed History rows under "Not resolvable" and was STALE -- v3 replaced it. Zeus implements
        `getHistoryRows()` to `docs/game-bridge-contract.md` and the rows come back. Trust the
        contract and the live probe over a per-game doc — this one was two revisions behind.)"""
        if not await self.ui_read(
                "() => { const b = window.__gameTestBridge;"
                " return !!(b && typeof b.getHistoryRows === 'function'); }", False):
            return []
        deadline = time.time() + timeout_ms / 1000
        rows = await self.history_rows()
        while not rows and time.time() < deadline:
            await asyncio.sleep(poll_ms / 1000)
            rows = await self.history_rows()
        return rows

    async def wait_for_popup(self, label: str, timeout_ms: int = 8000, poll_ms: int = 200) -> bool:
        """Wait until `label` is in the visible-popup list. Same reason as the History list: a popup
        whose content is fetched can be a beat behind the click that opened it.

        `label=None` means the profile says this build HAS no such popup, so it answers False at
        once. Without the guard the poll compares `None in [...]`, which is False every time, and
        spends the whole timeout proving a build lacks a surface its profile already declared it
        lacks (Zeus: `popup('history_detail')` is None)."""
        if not label:
            return False
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if label in await self.popups():
                return True
            await asyncio.sleep(poll_ms / 1000)
        return False

    async def wait_for_history_detail(self, timeout_ms: int = 10000, before: set | None = None,
                                      poll_ms: int = 200) -> bool:
        """Did the History DETAIL ticket open? Tries three signals, in order of authority.

        It used to ask one question — `wait_for_popup(GAME.popup('history_detail'))` — and that
        question is unanswerable on a build whose profile carries no such label. Zeus's is `None`,
        so the check could only ever be False: the detail was plainly ON SCREEN, the scenario
        concluded it had not opened, skipped the paging entirely, and reported `detail_opened:
        False`. "Nothing changed for 70 seconds" was exactly that — the scroll branch was never
        entered, and the wait behind it was spending a spin-sized budget.

        The lesson generalises: **`visiblePopups` is a DIALECT, and a dialect that disagrees is
        indistinguishable from a feature that is missing.** So prefer the signal the contract
        defines, and treat the label as one fallback among several rather than as the question.

        1. `getHistoryDetailState().open` — `docs/game-bridge-contract.md` §getHistoryDetailState
           defines this field for exactly this purpose. Build-independent, so it is tried first.
        2. the profile's popup label in `visiblePopups` — for a build with the label but not the
           getter (which is how this worked before, and still works).
        3. ANY popup that was not up before the row was clicked. Weaker, deliberately last, and
           the only one that survives a build with neither of the above — which is the situation
           that produced the hang.
        """
        deadline = time.time() + timeout_ms / 1000
        label, before = GAME.popup("history_detail"), (before or set())
        while True:
            if (await self.history_detail_state()).get("open"):
                return True
            live = set(await self.popups())
            if label and label in live:
                return True
            if live - before:
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(poll_ms / 1000)

    async def wheel_at(self, x: float, y: float, steps: int = 6, delta: int | None = None,
                       dwell_ms: int | None = None, label: str = "content",
                       fraction: float = 1.0) -> int:
        """Real mouse wheel at (x, y), `steps` times, dwelling after each so the new view lands and is
        recorded. Returns the wheels issued.

        ONE STEP IS ONE FULL PAGE by default: the delta is `VIEWPORT.height * fraction` and `fraction`
        is 1.0, so asking for 3 steps really advances 3 screens. (It used to be 0.6, which quietly
        turned "scroll N pages" into "scroll 0.6 N pages" — a 3-cluster detail stopped less than two
        screens down and the last cascade never came into frame.)

        `fraction` trades reach against overlap: below 1.0 consecutive screens overlap, so nothing is
        missed between two shots but N steps advance less than N screens; above 1.0 covers ground
        faster and can skip content. `delta` overrides the arithmetic with an explicit pixel amount —
        use it when the scrollable area is NOT viewport-sized (a short modal), since the wheel scrolls
        whatever is under the cursor, not the page.

        Used for panels the bridge cannot drive (history-detail, the tutorial WebView). The scroll
        POSITION is not readable, so there is no "am I at the bottom?" test — pass a count that covers
        the content; over-wheeling is harmless because the browser clamps at the end."""
        d = delta if delta is not None else int(VIEWPORT["height"] * fraction)
        await self.page.mouse.move(x, y)
        for i in range(steps):
            t0 = self.now_s()
            await self.page.mouse.wheel(0, d)
            await self.dwell(dwell_ms)
            self._step("scroll", f"scroll {label} {i + 1}/{steps} ({d}px)", t0,
                       {"delta": d, "step": i + 1, "steps": steps})
        return steps

    async def scroll_popup(self, steps: int = 6, dwell_ms: int | None = None, label: str = "popup",
                           fraction: float = 1.0, page_px: int | None = None) -> int:
        """Wheel down the popup on screen, ONE FULL PAGE per step (wheels over the viewport centre,
        where a modal sits). For panels with no bridge scroller — e.g. the history-detail view, whose
        cascade list runs past one screen.

        A "page" is the viewport height. If the popup's scrollable area turns out to be shorter than
        that, a step overshoots and can skip a step of the list: pass `page_px` (its real height) or
        dial `fraction` down. There is no getter for the popup's rect, so this cannot be measured."""
        return await self.wheel_at(VIEWPORT["width"] / 2, VIEWPORT["height"] / 2,
                                   steps=steps, dwell_ms=dwell_ms, label=label,
                                   delta=page_px, fraction=fraction)

    async def history_rows(self) -> list:
        """The History rows RENDERED right now (bridge getHistoryRows) — each {index, rect, clickable,
        data}, where `data` is the raw server row. The list is VIRTUALIZED: only on-screen rows exist,
        so this is what the player can actually see, which is exactly what a History case asks about.
        [] when the popup is closed or the build has no getter."""
        return await self.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getHistoryRows ? b.getHistoryRows() : []; }", []) or []

    async def start_capture(self, label: str = "popups") -> None:
        """Open a bridge EVENT capture window (`popup_shown` / `popup_dismissed` / ...)."""
        await self.ui_read(f"() => {{ const b = window.__gameTestBridge;"
                           f" if (b && b.startEventCapture) b.startEventCapture({json.dumps(label)});"
                           f" return true; }}")

    async def end_capture(self, label: str = "popups") -> list:
        """Close the capture window and return its events ([] if the bridge has no capture)."""
        return await self.ui_read(
            f"() => {{ const b = window.__gameTestBridge;"
            f" return (b && b.endEventCapture ? b.endEventCapture({json.dumps(label)}) : []) || []; }}",
            []) or []

    def start_popup_watch(self, interval_s: float = 0.12) -> None:
        """Sample the visible-popup list in the BACKGROUND while the scenario keeps clicking, so a
        rapid toggle burst can be judged on what shared the screen at the same instant (Mah_108).
        Sampling is best-effort — a read that races a click is dropped, never raised."""
        self._popup_seen, self._popup_max, self._popup_samples, self._popup_stop = [], 0, 0, False

        async def _poll():
            while not self._popup_stop:
                labels = await self.popups()
                self._popup_samples += 1
                self._popup_max = max(self._popup_max, len(labels))
                for lbl in labels:
                    if lbl not in self._popup_seen:
                        self._popup_seen.append(lbl)      # first-appearance ORDER
                await asyncio.sleep(interval_s)

        self._popup_task = asyncio.ensure_future(_poll())

    async def stop_popup_watch(self) -> dict:
        """Stop the background poll -> {order, max_concurrent, samples}. `samples == 0` means the
        poll never read anything, so `max_concurrent` is not evidence of anything."""
        self._popup_stop = True
        if self._popup_task is not None:
            try:
                await asyncio.wait_for(self._popup_task, timeout=2)
            except Exception:  # noqa: BLE001
                self._popup_task.cancel()
            self._popup_task = None
        return {"order": list(self._popup_seen), "max_concurrent": self._popup_max,
                "samples": self._popup_samples}

    async def set_language(self, code: str) -> str:
        """Re-enter the game in language `code`, and return the new launch URL.

        There is NO in-game language switch to click: the language is baked into the Playsite launch
        URL (`get_game_url(..., language=code)`), so changing it means resolving a fresh URL and
        reloading — which is exactly what the test matrix describes ("change the language, then
        reload / reopen the game"). The runner owns that resolution (it has the game config and runs
        the url_script) and injects it here as `relaunch`. Raises if the runner didn't provide one."""
        if self._relaunch is None:
            raise RuntimeError("language switching needs the runner's relaunch callback "
                               "(run scenarios through run_scenario.py, and set `language:` on the run)")
        t0 = self.now_s()
        url = await self._relaunch(code)
        await self.capture_game_info(f"relaunch ({code})")
        self.language = code
        self._url = url               # a second session must open the same language
        self._step("language", f"relaunch in {LANGUAGE_NAME.get(code, code)} ({code})", t0,
                   {"language": code})
        return url

    async def screenshot(self, label: str) -> Optional[str]:
        """Capture the viewport to a PNG and register it as a named SHOT.

        A still is the right evidence for a text check: it is full-resolution, so small glyphs and
        diacritics survive (a video assert grades a downsampled montage, where a missing Burmese mark
        is simply not present in the pixels), and one screen per call means the model answers about
        exactly one surface instead of summarising six. Returns the file path, or None if the runner
        gave no shots dir."""
        if self._shots_dir is None:
            return None
        self._shots_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower() or "shot"
        path = self._shots_dir / f"{len(self.shots):02d}-{safe}.png"
        t0 = self.now_s()
        try:
            await self.page.screenshot(path=str(path))
        except Exception as e:  # noqa: BLE001 — a failed capture must not sink the scenario
            self._step("shot", f"screenshot FAILED for {label}: {e}", t0, {"label": label})
            return None
        self.shots.append({"label": label, "file": path.name, "path": str(path)})
        self._step("shot", f"screenshot: {label}", t0, {"label": label, "file": path.name})
        return str(path)

    # The text-bearing surfaces of the game come from the profile: (label, opener role, closer
    # role). `None` opener = already on screen. A build missing one is skipped, not failed.
    @property
    def UI_SURFACES(self):
        return [(label, GAME.button(op) if op else None, GAME.button(cl) if cl else None)
                for label, op, cl in GAME.UI_SURFACES]

    async def tour_ui(self, dwell_ms: int = 2000, shots: bool = True, prefix: str = "",
                      surfaces=None) -> list:
        """Walk the text-bearing surfaces of the UI and pause on each: the base screen, the bet list,
        Settings, the Info / Tutorial guide, the Auto Play panel and History. Each is opened
        best-effort (a build without one is skipped) and closed again before the next. With
        `shots=True` each surface is also CAPTURED as a still, named "<prefix>surface" — that is what
        an `image:` assert grades. `surfaces=[...]` tours only those (the cheap knob: an image assert
        costs one AI call per shot, so halving the surfaces halves the bill). Returns those reached.

        Every surface is MARKED too, so a timestamp-mode video assert over the same tour still gets
        an ACTION TIMELINE naming what is on screen when."""
        seen = []
        wanted = set(surfaces) if surfaces else None      # None -> every surface
        for label, opener, closer in self.UI_SURFACES:
            if wanted is not None and label not in wanted:
                continue
            self.mark(f"SURFACE: {label}")
            if opener is None:                                  # the base screen is already on show
                await self.dwell(dwell_ms)
            elif not await self.click_button(opener, optional=True, timeout_ms=8000, dwell_ms=dwell_ms):
                continue
            seen.append(label)
            if shots:
                await self.screenshot(f"{prefix}{label}")       # capture WHILE the surface is open
            if opener is not None:
                if closer:
                    await self.click_button(closer, optional=True, timeout_ms=5000,
                                            dwell_ms=self.step_dwell_ms)
                await self.dismiss_popups()                     # next surface must open clean
        return seen

    async def tutorial_rect(self):
        """On-screen rect of the tutorial WebView (bridge `getTutorialRect`), or None when the info
        popup is closed / this build has no getter."""
        return await self.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getTutorialRect ? b.getTutorialRect() : null; }")

    async def scroll_tutorial(self, steps: int = 12, fraction: float = 0.9, dwell_ms: int = 2000,
                              up: bool = False) -> int:
        """Wheel through the tutorial WebView one viewport at a time, dwelling after each step so the
        new screenful lands and is recorded for the AI. Returns the wheels issued (0 = popup closed).

        The tutorial is a `cc.WebView` served CROSS-ORIGIN, so the browser blocks the bridge — and any
        injected JS — from touching it. Only a real `page.mouse.wheel` routes into it, which is why
        scrolling it is a FRAMEWORK job by design (bridge-qa-api.md, 'Scrolling the tutorial'). The
        same origin rule means the scroll POSITION cannot be read back either — there is no way to ask
        'am I at the bottom?'. So this wheels a FIXED number of steps chosen to over-cover the guide
        and lets the browser clamp at the end; over-wheeling is harmless (the last screenful just
        repeats). `fraction` leaves an overlap between steps (0.9 -> 10%) so no band of the document
        falls between two shots. Proof that the pixels actually MOVED can only come from the video."""
        rect = await self.tutorial_rect()
        if not rect:
            return 0
        await self.page.mouse.move(rect["centerX"], rect["centerY"])
        delta = int(rect["height"] * fraction) * (-1 if up else 1)
        for i in range(steps):
            t0 = self.now_s()
            await self.page.mouse.wheel(0, delta)
            await self.dwell(dwell_ms)                # hold so this screenful is in frame for the AI
            self._step("scroll", f"tutorial scroll {'up' if up else 'down'} {i + 1}/{steps}", t0,
                       {"delta": delta, "step": i + 1})
        return steps


# ---------------------------------------------------------------------------
# The scenario contract (the closed box)
# ---------------------------------------------------------------------------
@dataclass
class ScenarioResult:
    name: str
    reached: bool
    res: Optional[Bet]                 # the key round -> {res.*}
    rounds: list                       # all collected rounds -> {rounds[i].*}, {trigger}, {terminal}
    timeline: list
    events: list
    video_window: Optional[tuple]      # whole-scenario (start_s, end_s) into session.webm
    reason: Optional[str] = None       # why unreached
    round_windows: list = field(default_factory=list)   # per-round sub-windows, indexed by seq
    marks: dict = field(default_factory=dict)           # scenario-exposed name -> round seq (video: at:)
    facts: dict = field(default_factory=dict)           # scenario-exposed values, bound as {facts.*}
    shots: list = field(default_factory=list)           # [{label, file, path}] captured stills (image:)


class Unreached(Exception):
    """A precondition this scenario cannot get past. The message IS the run's `reason`.

    Raise it instead of hand-writing a `ScenarioResult(..., reached=False, ...)` per check, and let
    one handler at the end of `run` do the clearing and the reporting. Six hand-written copies in
    `buy-bonus` had already drifted: some cleared the popups, some did not.

    **Catch THIS type, never bare `Exception`.** Unreached means the game could not be brought to
    the situation -- an honest inconclusive. A `KeyError` or `UnboundLocalError` means the scenario
    is broken and must keep propagating, or our own bug gets filed as "the build does not support
    this".
    """


def require_buttons(*roles) -> list:
    """Resolve every control this scenario will click, up front, and raise `Unreached` naming any
    the profile does not have. Returns the build's names, in the order asked.

        btn_buy_bonus, btn_package = require_buttons("buy_bonus", ("buy_package", 0))

    A role is a string, or `(role, index)` for a templated name (`buyBonus:package:{i}`).

    Past this call every name is known-good, so the clicks below need no `if <name>:` guard, and a
    missing role is reported before the game is touched -- with ALL of them in one message, rather
    than one per run.

    It does not promise a click will succeed: a name that resolves can still never become
    clickable. That is a different failure with a different fix, and it stays on the click's own
    result.
    """
    names, missing = [], []
    for r in roles:
        role, idx = r if isinstance(r, tuple) else (r, None)
        name = GAME.button(role, i=idx) if idx is not None else GAME.button(role)
        names.append(name)
        if not name:
            missing.append(role)
    if missing:
        raise Unreached(f"`{GAME.name}` profile has no control for: {', '.join(missing)} "
                        f"(scripts/game_profile.py)")
    return names


class Scenario:
    name: str = ""
    description: str = ""
    async def run(self, ctx: DriveContext) -> ScenarioResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared free-spin helpers — reused by any scenario that may hit a bonus.
# ---------------------------------------------------------------------------
def cluster_wins(b: Bet) -> int:
    """How many cascade steps of this round PAID -- the unit the history detail pages by (one grid
    per step, so a step paying two clusters still counts once). Delegates to the active profile,
    which owns the field names; see games/mystic/bet-response.md for the shape it was derived from."""
    return GAME.cluster_wins((b.raw or {}).get(GAME.F_STEPS) or [], b.payout)


def win_clusters(b: Bet) -> int:
    """Total winning CLUSTERS across the round (every `winnings[]` entry). Diagnostic: it exceeds
    cluster_wins whenever one step paid several clusters at once."""
    return GAME.win_clusters((b.raw or {}).get(GAME.F_STEPS) or [])


async def confirm_stopped(ctx: DriveContext, watch_ms: int = 6000, dwell_ms: int = 3000,
                          label: str = "STOPPED", flag_grace_ms: int = 5000) -> dict:
    """Prove the game STOPPED, and leave footage of it stopped. Returns facts to expose.

    "It stopped" is a NEGATIVE claim, and a negative needs time on the clock to mean anything.
    Two things go wrong when a scenario just returns after the last round:

    * **Nothing is proved.** Having counted the rounds we asked for says our loop ended, not that
      the CLIENT did. A client that overruns keeps betting, and no assert notices.
    * **The AI has nothing to look at.** The clip ends on the last payout, so "did auto stop?" is
      being asked of a video that merely *stopped*. The honest answer to that is no.

    So: WATCH for another bet for `watch_ms` — nothing arriving is the positive evidence — read the
    client's own auto flag, then HOLD for `dwell_ms` so the still screen is in the recording. An
    unexpected round is recorded rather than discarded, so the evidence lands in the stream.

    The flag is POLLED for up to `flag_grace_ms`, not sampled once: it clears when the client has
    finished presenting the round that stopped auto, which can outlast the watch window.

    Facts: `stopped` · `extra_round_after_stop` · `auto_flag_after` · `auto_flag_cleared_s` ·
    `auto_spins_left_after` · `stop_watch_s` (quote it in a prompt so the AI knows to judge the
    tail, not the cut).
    """
    t0 = ctx.now_s()
    try:
        raw = await await_bet_response(ctx.page, watch_ms)
        extra = True                                   # something bet again -> it did NOT stop
        ctx._record(Bet.parse(raw, len(ctx.events)), "auto", t0)
    except BetRejected:                # a bet WAS sent (and refused): never read that as "stopped"
        raise
    except Exception:  # noqa: BLE001 -- nothing bet in the window = stopped
        extra = False
    # POLL the auto flag, do not sample it once. The client clears `isAutoPlay` when it has finished
    # presenting the round that stopped it, and a big win's count-up and celebration can still be
    # running when the watch window expires. A single read there catches the flag mid-presentation
    # and reports "auto is still on" for a game that has plainly stopped — observed on Chaos CR_020,
    # where the same case passed 7/7 in isolation and failed on `auto_flag_after` in a suite run.
    # A flag that never clears still fails, and `auto_flag_cleared_s` says how long it took, so a
    # genuinely slow one is visible rather than smoothed away.
    st, waited = await ctx.ui_read(_AUTO_JS, {}) or {}, 0.0
    while bool(st.get("on")) and waited < flag_grace_ms / 1000:
        await asyncio.sleep(0.5)
        waited += 0.5
        st = await ctx.ui_read(_AUTO_JS, {}) or {}
    ctx.mark(f"{label} -- watching the game")
    await ctx.dwell(dwell_ms)
    return {"stopped": (not extra) and not st.get("on"),
            "extra_round_after_stop": extra,
            "auto_flag_after": bool(st.get("on")),
            "auto_flag_cleared_s": round(waited, 1),
            "auto_spins_left_after": st.get("left"),
            "stop_watch_s": round(watch_ms / 1000, 1)}


def auto_stop_button() -> str | None:
    """The control that STOPS a running auto-spin run, in this build's dialect.

    Builds differ in kind, not just in name: on the reference build the same `auto` button toggles
    the run off, while Zeus has a dedicated main-screen `stopAuto` (and `autoOrFreeSpin`, which
    replaces spin during a run). Clicking `auto` there would OPEN the autoplay popup instead of
    stopping anything -- the run would keep going and the case would report a stop that never
    happened. Preference order: a dedicated main-screen stop, then the popup's stop, then `auto`.
    """
    # Order matters. `auto_stop` is POPUP-scoped: during a running auto run that popup is closed, so
    # it resolves to null and the click never lands. The main-screen controls come first, and the
    # reference build's own main-screen `auto` toggle before the popup fallback.
    return (GAME.button("auto_stop_main") or GAME.button("auto_or_freespin")
            or GAME.button("auto") or GAME.button("auto_stop"))


def win_steps(b: Bet) -> int:
    """How many PAYING steps this round produced — the unit a history detail pages by.

    Cascade games count winning cascade steps (one grid per step). Every other mechanism reports a
    single grid per round, so a paying round is exactly one step and a losing one is zero. Without
    this split, anything phrased in cascade terms silently evaluates to 0 on a payline game: the
    outcome hunt in `spin-then-history` spun its entire 80-spin budget on Zeus without matching a
    single round, because `cluster_wins` is always 0 there.
    """
    if GAME.mechanism == "cascade":
        return cluster_wins(b)
    return 1 if b.payout > 0 else 0


def is_freespin_trigger(b: Bet) -> bool:
    """True when a round AWARDED the bonus. Keyed off the response via the profile: free spins were
    awarded, or the round already flipped to a bonus state. A hold-spin game also counts landing its
    trigger symbols, since the counters may only appear on the next response."""
    return GAME.is_trigger(b.raw or {})


# ⚠ NOT A FIXED WINDOW. The offer is raised only after the LAST free spin has finished PRESENTING,
# and the response for that spin arrives when it STARTS. A plain last spin presents in a few
# seconds, but one carrying a Cat vs Dog drama and a reel-glow that resolves reels one at a time
# took ~18 s, then the TOTAL WIN count-up, then the offer (gm-lastbet TC-086, 2026-09-29: response
# at 148 s, offer at ~171 s). A 12 s window closed at ~165 s, logged "none was raised", and the
# game sat on the panel. So: keep waiting while ANYTHING is still animating (`busyOps`) or any
# popup is up, and only conclude "no offer" after `idle` ticks of the game genuinely at rest —
# long enough to cover the brief gap in which spin is clickable before the panel fades in (~0.55 s).
# `max` is the hard stop.
_EXT_WAIT_JS = """async (opts) => {
    const b = window.__gameTestBridge;
    if (!b) return null;
    const seen = () => {
        const s = b.getExtensionOfferState ? b.getExtensionOfferState() : null;
        if (s && s.open) return s;
        const snap = b.getSnapshot ? b.getSnapshot() : {};
        if (snap && snap.extensionOfferOpen) return Object.assign({}, s || {}, {open: true});
        const names = (snap && snap.visiblePopups) || [];
        if (names.some(n => String(n).indexOf('BuyExtraFreeSpin') >= 0))
            return Object.assign({}, s || {}, {open: true});
        return null;
    };
    let idle = 0;
    for (let i = 0; i < opts.max; i++) {
        const hit = seen(); if (hit) return hit;
        const snap = b.getSnapshot ? b.getSnapshot() : {};
        const busy = (snap && snap.busyOps) || [];
        // Toasts never block anything. The TOTAL WIN result popup is what the offer opens ON TOP
        // of, and in a no-offer bonus (max win, autoplay) it waits for a tap — so once nothing is
        // animating it counts as "at rest", or every such bonus would sit out the whole `max`.
        const popups = ((snap && snap.visiblePopups) || [])
            .filter(n => String(n).indexOf('notify:') !== 0
                         && String(n).indexOf('FreeSpinResult') < 0
                         && (opts.ignore || []).indexOf(String(n)) < 0);
        idle = (busy.length || popups.length) ? 0 : idle + 1;
        if (idle >= opts.idle) return {};
        await new Promise(r => setTimeout(r, 50));
    }
    return {};
}"""

_EXT_CLICKABLE_JS = """async (name) => {
    const b = window.__gameTestBridge;
    if (!b || !b.getButtonRect) return null;
    for (let i = 0; i < 40; i++) {                 // the panel fades in over ~0.55 s
        let r = null;
        try { r = b.getButtonRect(name); } catch (e) { r = null; }
        if (r && r.interactable && r.visible) return r;
        await new Promise(res => setTimeout(res, 50));
    }
    return null;
}"""

_EXT_GONE_JS = """async () => {
    const b = window.__gameTestBridge;
    for (let i = 0; i < 160; i++) {                // it HOLDS for up to 5 s after the click
        const s = b && b.getExtensionOfferState ? b.getExtensionOfferState() : null;
        const snap = b && b.getSnapshot ? b.getSnapshot() : {};
        const names = (snap && snap.visiblePopups) || [];
        const up = (s && s.open) || (snap && snap.extensionOfferOpen)
                   || names.some(n => String(n).indexOf('BuyExtraFreeSpin') >= 0);
        if (!up) return true;
        await new Promise(r => setTimeout(r, 50));
    }
    return false;
}"""

_EXT_REPLY_JS = """async () => {
    const b = window.__gameTestBridge;
    if (!b || !b.awaitExtensionResponse) return null;
    try { return await b.awaitExtensionResponse(4000); } catch (e) { return null; }
}"""


_BIG_WIN_POPUP_JS = """async (opts) => {
    const b = window.__gameTestBridge;
    if (!b || !b.getSnapshot) return "";
    for (let i = 0; i < opts.ticks; i++) {
        const s = b.getSnapshot() || {};
        const names = (s.visiblePopups || []).filter(n => (opts.ignore || []).indexOf(String(n)) < 0)
            .map(n => String(n).toLowerCase());
        if (names.some(n => n.indexOf('bigwin') >= 0)) return "popup";
        const busy = s.busyOps || [];
        if (busy.indexOf('bigWin') >= 0) return "busy";
        // Nothing is coming: the round has finished presenting and the screen is clear. Checked
        // only after a short grace period, because for the first frames after a response NOTHING
        // is busy yet and bailing there would miss every celebration.
        if (i > opts.grace && busy.length === 0 && names.length === 0) return "";
        await new Promise(r => setTimeout(r, 50));
    }
    return "";
}"""


# Which control actually skips the celebration, discovered rather than assumed. `bigWin:skip` is
# the name the game team gave, and it is in no §7 table -- so if it does not resolve, ASK THE BUILD
# what it has instead of giving up. `getAllButtonRects()` returns only names that resolve right
# now, which during a celebration is a very short list.
_BIG_WIN_CONTROLS_JS = """() => {
    const b = window.__gameTestBridge;
    if (!b || !b.getAllButtonRects) return [];
    let all = {};
    try { all = b.getAllButtonRects() || {}; } catch (e) { return []; }
    const out = [];
    for (const name of Object.keys(all)) {
        const r = all[name];
        if (!r || !r.interactable || !r.visible) continue;
        const k = name.toLowerCase();
        // a name about the big win first, any other skip control second
        const rank = k.indexOf('bigwin') >= 0 ? 0 : (k.indexOf('skip') >= 0 ? 1 : -1);
        if (rank >= 0) out.push({ name: name, rank: rank, x: r.centerX, y: r.centerY });
    }
    out.sort((a, c) => a.rank - c.rank);
    return out;
}"""


async def big_win_threshold(ctx: DriveContext):
    """The payout/bet multiple at which this game starts a big-win celebration — read once from the
    launch payload and cached on the context. `None` when the build reports no win bands."""
    if getattr(ctx, "_big_win_threshold", "unset") == "unset":
        try:
            info = await read_game_info(ctx.page)
        except Exception:                                        # noqa: BLE001
            info = {}
        ctx._big_win_threshold = GAME.big_win_threshold(info or {})
        # Printed once per run: it is the difference between "the skip is armed" and "the skip is
        # silently doing nothing", and a person watching a run cannot tell those apart otherwise.
        if ctx._big_win_threshold is None:
            print("  big win  : NO win bands in gameInfo (settings.winFormula / settings.general."
                  "winFormula) -- celebrations cannot be detected, so none will be skipped")
        else:
            print(f"  big win  : celebrations start at {ctx._big_win_threshold}x the bet "
                  f"(from gameInfo winFormula)")
    return ctx._big_win_threshold


async def skip_big_win(ctx: DriveContext, bet: "Bet | None", wait_ms: int = 20000,
                       second_tap_ms: int = 1000, grace_ms: int = 1500) -> bool:
    """Cut short the big-win celebration this round is about to play, if it is about to play one.

    ⚠ WHY THIS EXISTS: TIME. A BIG / MEGA / ULTRA win takes the screen for several seconds of
    count-up, and in a free-spin bonus it can happen on several rounds in a row — minutes per run,
    every run, for a presentation that no case here is grading. The celebration is skippable by the
    player, so the test does what the player does.

    ⚠ IT ASKS THE RESPONSE FIRST. `payout / baseStake >= threshold`, with the threshold read from
    `gameInfo.settings.winFormula` rather than guessed. Polling for the popup on EVERY round would
    cost more time than the celebrations it saves, so a round that cannot be a big win never waits.

    TWO TAPS, one second apart, which is what the client's own control expects: the first cuts the
    count-up to its total, the second dismisses the banner. A single tap leaves the banner sitting
    there and the next `waitForSpinClickable` blocks on it anyway.

    ⚠ CALLED FROM THE ROUND PRIMITIVES (`base_spin`, `free_spin`, `observe_bet`), not from the
    drains. It was in `finish_round` and `play_out_freespins` first, and that missed every scenario
    that drives with `ctx.base_spin()` directly — `drama`, `walking-wild`'s base spins, `reels-glow`
    and both hunt loops. Every round the framework observes passes through one of the three
    primitives, so that is the only place it is impossible to forget.

    Set `ctx.skip_big_wins = False` in a scenario whose subject IS the celebration.

    Returns True when a celebration was found and tapped. ⚠ It NEVER fails the run: a game with no
    `big_win_skip` control, or a celebration that never appeared, is reported on the timeline and
    left alone — this is an optimisation, not a claim, and nothing asserts on it.
    """
    if bet is None or not getattr(ctx, "skip_big_wins", True):
        return False
    btn = GAME.button("big_win_skip")
    if not btn:
        return False
    threshold = await big_win_threshold(ctx)
    base = GAME.base_stake(bet.raw or {})

    # ⚠ SAY WHY, ONCE. Every early return below is a reason the celebration will play out in full,
    # and all of them used to be silent -- the run simply took longer and nothing explained it.
    # One mark per run is enough to tell them apart on the next report.
    if threshold is None:
        return False          # already reported once, by `big_win_threshold`
    if not GAME.is_big_win(bet.raw or {}, threshold):
        return False

    # ⚠ NOT a fixed wait. A ways game presents `winWays`, then `wildMultiplier`, then the
    # count-up, and only THEN the banner -- a flat 4 s missed it and reported "nothing appeared"
    # on a 36x win that was about to celebrate. The wait now ends on whichever comes first: the
    # banner, or the round finishing its presentation entirely (nothing busy, nothing on screen).
    seen = await ctx.page.evaluate(_BIG_WIN_POPUP_JS,
                                   {"ticks": max(1, int(wait_ms / 50)),
                                    "grace": max(1, int(grace_ms / 50)),
                                    "ignore": GAME.promo_popups()})
    if not seen:
        # The round qualified but nothing came up. Worth saying: either the bands in the launch
        # payload disagree with what the client celebrates, or this build shows no banner at all.
        ctx.mark(f"BIG WIN: {bet.payout} qualified but no celebration appeared in {wait_ms}ms")
        print(f"  big win  : {bet.payout} is {round(bet.payout / max(base, 1e-9), 1)}x -- qualified, "
              f"but no popup or busy label appeared in {wait_ms}ms, so there was nothing to skip")
        return False

    ctx.mark(f"BIG WIN: skipping the celebration ({bet.payout} on {base}, seen as {seen})")
    tap = None
    # ⚠ NOT `click_button(optional=True)`. That swallows the rejection, and the rejection IS the
    # diagnosis: `waitForButtonClickable` fails with `unknown_control:<name>` (this build has no
    # such control), `not_available` (it or its popup is not there), `not_interactable` (disabled),
    # `offscreen`, `blocked_by:<node>` (another layer would take the click -- which a full-screen
    # celebration very plausibly does to its own skip button), or `nothing_hit` / `no_geometry`
    # (the engine could not place it, which the bridge doc says to report as a defect).
    # Collapsing all six into "not clickable" is what made this unanswerable for three runs.
    why = None
    try:
        rect = await _await(ctx.page,
                            f"b.waitForButtonClickable({json.dumps(btn)}, undefined, 3000)",
                            5000, f"waitForButtonClickable({btn})")
    except Exception as e:                                       # noqa: BLE001
        rect, why = None, str(e)
    if rect:
        await ctx.page.mouse.click(rect["centerX"], rect["centerY"])
        tap = btn
    else:
        # The same question asked without the stability requirement, which separates "it is not
        # there at all" from "it is there but was never steady enough to click".
        # ⚠ `page.evaluate`, NOT `ui_read`: ui_read passes NO argument to the page, so a JS
        # function that declares one gets `undefined`. That is how this reported
        # `not_available:null` -- the bridge was asked about a control called `undefined`, and the
        # answer said nothing about `bigWin:skip` at all.
        try:
            can = await ctx.page.evaluate(
                "(n) => { const b = window.__gameTestBridge;"
                " return b && b.canDo ? b.canDo(n) : null; }", btn) or {}
        except Exception:                                        # noqa: BLE001
            can = {}
        print(f"  big win  : `{btn}` did not become clickable -- reason: {why or 'none given'}")
        if can:
            print(f"             canDo({btn!r}) -> {can}")
        # The named control did not resolve. Ask the build what it DOES have up right now rather
        # than giving up -- and say which one worked, so the profile can be corrected.
        found = await ctx.page.evaluate(_BIG_WIN_CONTROLS_JS) or []
        if not found:
            # ⚠ Print what IS up, rather than concluding "there is nothing". The candidate filter
            # only accepts a name containing "bigwin" or "skip", so a control called `winPanel:tap`
            # or `continue` would be filtered out and the message would be misleading. This is the
            # one moment the answer is visible, and it costs one call.
            # ⚠ TAKE THE GEOMETRY, not just the names. `offscreen:<name>` means "centre outside
            # the canvas" (bridge doc, getButtonRect) -- and the FIRST thing the game team asks is
            # "outside by how much, and how big is the canvas?". Without those numbers the report
            # is a name and an adjective, and the answer costs another live run. `getButtonRect`
            # answers for an offscreen control too (it returns the rect with `visible:false`),
            # which is exactly the case `getAllButtonRects`-filtered-by-visible cannot show.
            state = await ctx.ui_read(
                "(n) => { const b = window.__gameTestBridge;"
                " const s = b && b.getSnapshot ? b.getSnapshot() : {};"
                " let rects = {};"
                " try { rects = (b && b.getAllButtonRects) ? (b.getAllButtonRects() || {}) : {}; }"
                " catch (e) {}"
                " const live = Object.keys(rects).filter(n => rects[n]"
                "   && rects[n].interactable && rects[n].visible);"
                " let rect = null;"
                " try { rect = (b && b.getButtonRect) ? b.getButtonRect(n) : null; } catch (e) {}"
                " const c = document.querySelector('canvas');"
                " const cr = c ? c.getBoundingClientRect() : null;"
                " return { live: live, all: Object.keys(rects), rect: rect,"
                "          canvas: cr ? { width: cr.width, height: cr.height } : null,"
                "          popups: s.visiblePopups || [], busy: s.busyOps || [] }; }",
                {}, arg=btn) or {}
            ctx.mark(f"BIG WIN: `{btn}` did not resolve; controls up = {state.get('live')}")
            print(f"  big win  : `{btn}` never became clickable while the celebration was up.")
            print(f"             popups now : {state.get('popups')}")
            print(f"             busy now   : {state.get('busy')}")
            print(f"             CLICKABLE  : {state.get('live')}")
            print(f"             resolving  : {(state.get('all') or [])[:25]}")
            # The two lines that make this reportable without a second run.
            print(f"             rect({btn}) : {state.get('rect')}")
            print(f"             canvas     : {state.get('canvas')}")
            print(f"             ^ if one of those dismisses the banner, set it as "
                  f"`big_win_skip` in the {GAME.name} profile. If the list is empty, the "
                  f"celebration is not skippable through the bridge -- a gap to report.")
            return False
        tap = found[0]["name"]
        print(f"  big win  : `{btn}` did not resolve -- using `{tap}` instead "
              f"(candidates: {[f['name'] for f in found]}). ⚠ Fix `big_win_skip` in the profile")
        await ctx.page.mouse.click(found[0]["x"], found[0]["y"])
        await ctx.dwell(second_tap_ms)
        await ctx.page.mouse.click(found[0]["x"], found[0]["y"])
        print(f"  big win  : skipped via `{tap}` ({bet.payout} on {base})")
        return True
    await ctx.dwell(second_tap_ms)
    if rect:
        await ctx.page.mouse.click(rect["centerX"], rect["centerY"])   # the second tap dismisses
    print(f"  big win  : skipped via `{tap}` ({bet.payout} on {base} = "
          f"{round(bet.payout / max(base, 1e-9), 1)}x)")
    return True


async def answer_extension_offer(page: Page, expect: bool = True) -> bool:
    """Answer the FREE SPIN EXTENSION offer on `page`, and leave the screen clear behind it.

    Page-level on purpose: **the offer can be raised where no scenario is driving.** A session that
    resumes an owed bonus plays it out inside `start_game`, which then calls `assert_idle` itself —
    so a scenario that answers the offer after `reload_and_start` returns is already too late, and
    the boot check has burned its budget on a popup nobody closed. `start_game` calls this before
    that check; `decline_extension_offer` is the DriveContext wrapper that adds timeline marks.

    What the bridge doc (`games/grandma/bridge-api.md` §8.4) makes necessary:

    * **Wait for it.** The panel is raised AFTER the last free spin lands — result popup, then a
      ~0.55 s fade-in — so one read taken as the drain ends always says "not open".
    * **Do not use `canDo('spin')` as the bail-out.** In the gap before the popup appears spin is
      briefly clickable, which ends the wait on its first pass. Whether an offer is coming is a
      property of the ROUND (`freeSpins == 0` with `nextState == 2`), which is what `expect` carries.
    * **Two signals.** The state getter AND the panel's name in `visiblePopups` — one getter
      disagreeing with the screen is how this hid twice.
    * **Declining is not instant.** "No/Close within the first 5 s: the popup holds until that
      window ends, then exits."
    * **Collect the reply.** A decline answers on its own stream, and so do the client's own
      declines; an uncollected one is handed to the next `awaitExtensionResponse`.
    * **The result popup is a separate surface** that `dismissPopups()` never force-closes.

    Returns True when an offer was answered here. A no-op on every game whose profile has no
    `extension_decline` button.
    """
    decline = GAME.button("extension_decline")
    if not decline:
        return False

    # Up to 60 s for the last round to finish presenting; then 4 s of real idle when an offer is
    # expected (covers the pre-fade gap), 1 s when not.
    state = await page.evaluate(_EXT_WAIT_JS, {"max": 1200, "idle": 80 if expect else 20,
                                              "ignore": GAME.promo_popups()}) or {}
    answered = False
    if state.get("open"):
        for name in (decline, GAME.button("extension_close")):
            if not name:
                continue
            rect = await page.evaluate(_EXT_CLICKABLE_JS, name)
            if rect:
                await page.mouse.click(rect["centerX"], rect["centerY"])
                answered = True
                break
        if not answered:
            # Documented as a decline in its own right, not a dismissal.
            await page.evaluate(
                "async () => { const b = window.__gameTestBridge;"
                " if (b && b.dismissPopups) await b.dismissPopups(); return true; }")
            answered = True
        await page.evaluate(_EXT_GONE_JS)

    await page.evaluate(_EXT_REPLY_JS)

    skip = GAME.button("freespin_result_skip")
    if skip:
        names = await page.evaluate(
            "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
            " return (s && s.visiblePopups) || []; }") or []
        if any("freespinresult" in str(n).lower() for n in names):
            rect = await page.evaluate(_EXT_CLICKABLE_JS, skip)
            if rect:
                await page.mouse.click(rect["centerX"], rect["centerY"])
    return answered


async def decline_extension_offer(ctx: DriveContext, terminal: "Bet | None" = None) -> bool:
    """`answer_extension_offer` for a scenario: works out whether to expect an offer from the round,
    and marks the timeline so a run's log says which way it went.

    ⚠ "No offer was raised" and "we looked too early" produce identical logs otherwise — which is
    exactly how this bug survived two fixes.
    """
    if not GAME.button("extension_decline"):
        return False
    expect = True
    if terminal is not None:
        raw = terminal.raw or {}
        window_open = (terminal.free_spins == 0
                       and GAME.game_state(raw) == GAME.FREESPIN_STATE)
        expect = bool(window_open or raw.get("canBuyExtension"))
    answered = await answer_extension_offer(ctx.page, expect=expect)
    ctx.mark("EXTENSION OFFER: declined" if answered
             else "EXTENSION OFFER: none was raised")
    return answered


async def play_out_freespins(ctx: DriveContext, trigger: Bet, cap: int = 500, settle: bool = True,
                             on_round=None) -> list:
    """Wait for a triggered free-spin bonus to FINISH. Given the base spin that awarded it,
    passively consume every auto-played free-spin round until the bonus ends
    (freeSpins == 0 OR gameState == base — both, since hasReachedMaxWin can truncate it), then
    settle back to a clean base. Returns [trigger, ...free spins..., terminal].
    NOTE: never call waitForSpinClickable while the bonus runs (it hangs); this drains via
    awaitFreespinRound. Pass settle=False when more driving follows immediately (e.g. auto-play
    resumes the next spin on its own) so we don't wait on spin-readiness mid-sequence.
    `on_round` is an optional async callback given each free-spin round as it arrives -- for
    evidence that can only be read DURING the bonus (e.g. the client's bet readout)."""
    rounds = [trigger]
    retriggers = 0
    prev_left = None
    finished = False
    for _ in range(cap):
        b = await ctx.free_spin()
        rounds.append(b)
        if on_round is not None:
            await on_round(b)
        # A free spin can WIN MORE FREE SPINS, so the counter is not monotonic -- it jumps back up
        # on a retrigger. Nothing here assumes it only falls (the loop ends on the counter actually
        # reaching 0), but count the jumps: they are the reason a bonus can outlast `cap`, and the
        # number is what tells you whether a cap was hit because of retriggers or something else.
        if prev_left is not None and b.free_spins is not None and b.free_spins > prev_left:
            retriggers += 1
        prev_left = b.free_spins
        if is_terminal(b):
            finished = True
            break
    if not finished:
        # DO NOT return quietly. The bonus is still running, and a caller that treats this list as
        # a completed bonus will `settle()` against a spin button that cannot become ready, then end
        # the run INSIDE the bonus -- which makes the next `return-to-base` teardown wait on rounds
        # the client already emitted, time out, and abort the whole suite. Failing here names the
        # real cause at the point it happens instead of surfacing as a teardown timeout two runs later.
        raise RuntimeError(
            f"free-spin drain gave up after {cap} rounds with the bonus still running "
            f"({retriggers} retrigger(s) seen; {rounds[-1].free_spins} spin(s) still on the counter). "
            f"Raise `cap` if the paytable legitimately awards this many, or make the award smaller.")
    # The bonus is over as far as the SERVER is concerned, but a game with a Free Spin Extension
    # is still holding the player for an answer, and the spin button stays locked until it gets
    # one. The terminal round is what says whether to expect the offer at all. No-op elsewhere.
    await decline_extension_offer(ctx, rounds[-1] if rounds else None)
    if settle:
        await ctx.settle()   # total-win presentation + return to base, inside the window
    return rounds


async def spin_and_finish(ctx: DriveContext) -> tuple:
    """One base spin, played through to a clean base state. Returns `(spin, rounds)` — `spin` is the
    paid base bet (what `res` should be), `rounds` is the whole chain it produced.

    **Any base spin can trigger free spins**, and the client then auto-plays the entire bonus on its
    own. A scenario that just spins and settles leaves those rounds unconsumed, and the damage is not
    limited to a missing measurement:

    * `settle()` waits on `waitForSpinClickable`, which by contract does not resolve during free
      spins — so it burns its whole budget while the bonus plays, and the responses go unread.
    * The run then ends *inside* a bonus. `return-to-base` sees a non-terminal `last_bet` and calls
      `awaitFreespinRound` for rounds the client already emitted during that settle, times out, and
      raises — and a run-level teardown that raises **aborts the whole suite**.

    So a scenario is finished only when the game is back at a spinnable base. Use this instead of
    `base_spin()` + `settle()` anywhere the outcome is not already constrained to a losing/base
    round. Scenarios that hunt for a specific outcome (`base-win`, `fs-complete`, `freespin-cascade`)
    already handle the trigger themselves and do not need it.
    """
    b = await ctx.base_spin()
    return b, await finish_round(ctx, b)


async def finish_round(ctx: DriveContext, b: Bet) -> list:
    """Take an ALREADY-OBSERVED round to a clean base state; returns the chain it produced.

    The half of `spin_and_finish` that does not click, for rounds the client fired itself (a Space
    press, an auto spin) or where a gesture has to happen between the response and the settle."""
    if is_freespin_trigger(b):
        return await play_out_bonus(ctx, b)       # drains to terminal, then settles
    await ctx.settle()
    return [b]


def base_rounds_since(ctx: DriveContext, before: int) -> int:
    """Paid BASE rounds recorded since event index `before`.

    For "one input started exactly one spin" (Mah_035/036, CR_041, ZZ-44/45): a bonus drained after
    the spin adds free-spin rounds to the stream, and counting raw events would read those as extra
    spins the input fired. Only `phase == 'base'` rounds are bets the input actually caused."""
    return sum(1 for e in ctx.events[before:]
               if e.get("type") == "spin_result" and (e.get("data") or {}).get("phase") == "base")


class BaseWin(Scenario):
    name = "base-win"
    description = ("Spin the base game until a round PAYS, then settle. Mechanism-agnostic: it asks "
                   "only `payout > 0`, so it works on a payline game as readily as a cascade one — "
                   "use it for the balance-credit check. (`base-cluster-win` is the cascade-specific "
                   "cousin: it additionally requires >= 2 cascade steps, which a payline game never "
                   "produces.) Exposes res = the winning round + facts.spins_to_win / win_payout / "
                   "win_stake. Unreached if nothing pays inside the spin budget.\n"
                   "It also publishes the BALANCE READOUT'S THREE VALUES for the winning round — "
                   "balance_before_spin, balance_while_spinning (the stake is taken at spin START, "
                   "so the readout sits here for the whole spin) and balance_after_win. "
                   "A prompt about either half of the money has to name the two endpoints of the "
                   "step it is judging, not just the destination; naming only `res.balance` earned "
                   "a correct NO on Mah_008, because that is a value the readout passes THROUGH. "
                   "The names are `base-cluster-win`'s and the authoring doc's, deliberately: one concept "
                   "must not have two names across two scenarios, or a prompt moved between "
                   "them silently binds to null. See docs/scenario-assert-authoring.md §2. "
                   "PORTABLE, and verified against the captured payloads of all three games. "
                   "The one thing to know is WHY `win_stake` is always > 0 here: a paying round "
                   "with stake 0 exists on every game with a bonus -- Zeus's terminal Hold & "
                   "Spin round reports payout 261 at stake 0 and is NOT a trigger, so it reads "
                   "exactly like a base win. It never reaches this loop because `base_spin()` "
                   "only returns rounds the framework CLICKED; bonus rounds arrive through "
                   "`awaitFreespinRound` inside `play_out_freespins`. That is an invariant of "
                   "the drive, not an accident -- a scenario that collects rounds some other "
                   "way must re-check it.")

    def __init__(self, max_spins: int = 60, skip_big_win: bool = True):
        self.max_spins = max_spins
        # False keeps a BIG/MEGA win celebration on screen instead of tapping it away. Needed when
        # a case grades the balance COUNT-UP: the skip cuts the count-up short, so "did the balance
        # count up?" could read "no" on a big win through the test's own doing.
        self.skip_big_win = skip_big_win

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        ctx.skip_big_wins = self.skip_big_win
        rounds: list = []
        for _ in range(self.max_spins):
            b = await ctx.base_spin()
            rounds.append(b)
            if is_freespin_trigger(b):
                # a bonus pays too, but its money arrives across the feature -- drain it and keep
                # looking for a plain BASE win, which is what the balance-credit case is about
                fs = await play_out_bonus(ctx, b)
                rounds.extend(fs[1:])
                continue
            await ctx.settle()
            if b.payout > 0:
                # The readout's three values, derived from the round's own numbers via
                # conservation (balance == previous - stake + payout). Computed here rather than
                # left to the case, because a suite cannot do arithmetic: a `video:` prompt can
                # only interpolate facts, so an endpoint that is not a fact cannot be named.
                while_spinning = round(b.balance - b.payout, 2)
                return ScenarioResult(self.name, True, b, rounds, ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), round_windows=ctx.windows(),
                                      marks={"win": b.seq},
                                      facts={"spins_to_win": len(rounds), "win_payout": b.payout,
                                             "win_stake": b.stake, "balance_after_win": b.balance,
                                             # as the client PRINTS them (profile DISPLAY_*) — what an
                                             # AI read of the screen is compared with
                                             "win_payout_display": GAME.display_amount(b.payout),
                                             "balance_after_win_display": GAME.display_amount(b.balance),
                                             "balance_before_spin": round(while_spinning + b.stake, 2),
                                             "balance_while_spinning": while_spinning})
        return ScenarioResult(self.name, False, None, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              facts={"spins_played": len(rounds)},
                              reason=f"no paying base round in {self.max_spins} spins")


class BaseClusterWin(Scenario):
    name = "base-cluster-win"
    description = ("Base game: spin until a cluster win with >= 2 cascades (which climbs the multiplier), "
                   "then play ONE MORE fresh base spin so the multiplier RESET on the next round is also "
                   "on screen. A cluster win is expensive to hit, so this single play covers BOTH the "
                   "win (balance credit + multiplier-per-cascade) and the reset. Exposes res = the winning "
                   "spin, rounds = [win, next]; marks: win = the cascade win, reset = the following spin "
                   "(bind win checks to `at: win`, the reset check to `at: reset`).")

    def __init__(self, max_spins: int = 200):
        self.max_spins = max_spins

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        if GAME.mechanism != "cascade":
            # A payline game reports one grid per round, so `cascades >= 2` can never be true and
            # this would spin its whole budget to no purpose. Say so immediately instead: use
            # `base-win` for a plain balance-credit check.
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason=f"'{GAME.name}' is a {GAME.mechanism} game -- it has no "
                                         f"cascade steps, so a >=2-cascade win cannot occur. Use the "
                                         f"'base-win' scenario for any-win checks.")
        win = None
        for _ in range(self.max_spins):
            b = await ctx.base_spin()
            if is_freespin_trigger(b):
                # A cascade can land the trigger, and then the client auto-plays the whole bonus.
                # It has to be played out HERE: the next iteration starts with waitForSpinClickable,
                # which by contract does not resolve during free spins, so leaving the bonus running
                # burns that spin's whole budget and fails with 'spin_state:STOP' while the snapshot
                # still reads gameMode FREE_SPIN. Not a win for this case either way — a triggering
                # round's multiplier is the bonus's, not the base game's — so drain and keep hunting.
                await play_out_freespins(ctx, b)
                continue
            if b.payout > 0 and b.cascades >= 2:
                win = b
                break
        if win is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason=f"no >=2-cascade win in {self.max_spins} base spins")
        await ctx.settle()                        # finish the winning round's presentation (inside its clip)
        nxt = await ctx.base_spin()               # a fresh new round -> the base multiplier should reset to x1
        if is_freespin_trigger(nxt):
            await play_out_freespins(ctx, nxt)    # drain if this spin happened to trigger free spins
        else:
            await ctx.settle()
        # The multiplier ladder is the POINT of this scenario, so expose it: the per-step climb
        # inside the winning round, and where the following round started. Read from the response
        # (never assumed to be +1 per cascade -- Mystic's base ladder is 1,2,3,5,8,10).
        climb = _step_multipliers(win)
        after = _step_multipliers(nxt)
        configured = await ctx.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getMultiplierLevels ? b.getMultiplierLevels() : null; }")
        return ScenarioResult(self.name, True, win, [win, nxt], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"win": win.seq, "reset": nxt.seq},
                              facts={"cascades": win.cascades,
                                     # ⚠ `cascades` is GRIDS EVALUATED, not what a viewer counts.
                                     # The chain ends with a step that pays nothing — that step
                                     # clears no symbols and drops nothing in, so the number of
                                     # VISIBLE clear-and-drop events is one fewer. A prompt that
                                     # says "cascaded {cascades} times" overstates it by one and
                                     # earns a correct NO (see Mah_043).
                                     "cascade_events": GAME.cluster_wins(GAME.steps(win.raw or {}),
                                                                         win.payout),
                                     "win_payout": win.payout, "win_stake": win.stake,
                                     # The BALANCE READOUT passes through three values, and
                                     # `res.balance` is only the middle one. The stake is taken at
                                     # spin START, so while the reels resolve the readout already
                                     # reads balance-minus-win; the win is credited at the end of
                                     # the count-up; and the next spin's stake comes straight off
                                     # again. A prompt that asks "does it rise to {res.balance}"
                                     # without saying this gets a correct NO (see Mah_008).
                                     "balance_before_spin": round(win.balance - win.payout + win.stake, 2),
                                     "balance_while_spinning": round(win.balance - win.payout, 2),
                                     "balance_after_win": win.balance,
                                     "balance_after_next_bet": round(win.balance - nxt.stake, 2),
                                     "multipliers": climb,             # e.g. [1, 2, 3, 5, 8, 10]
                                     "first_multiplier": climb[0] if climb else None,
                                     "top_multiplier": climb[-1] if climb else None,
                                     "multiplier_climbed": len(climb) >= 2 and climb[-1] > climb[0],
                                     "next_multiplier": after[0] if after else None,
                                     "multiplier_reset": bool(climb and after and after[0] == climb[0]),
                                     # config, where the build reports it -- lets a case check the
                                     # observed climb against the configured ladder exactly
                                     "configured_ladder": (configured or {}).get("baseGame")})


class FreeSpinComplete(Scenario):
    name = "fs-complete"
    description = ("Spin base until free spins trigger, then observe the whole bonus to its "
                   "terminal round. Exposes rounds = [trigger, ...free spins..., terminal]; "
                   "trigger = rounds[0], terminal = rounds[-1].")

    def __init__(self, max_base: int = 400, arrive_when: dict | None = None,
                 max_spins: int | None = None):
        self.max_base = max_spins or max_base
        # Hunt for a trigger of a PARTICULAR SHAPE, not just any trigger. Keys, all optional:
        #   bonus_reels     -- the award table is keyed on how many REELS carry the scatter, so
        #                      "3 reels pays 8, 4 pays 12, 5 pays 15" needs one trigger of each
        #   min_on_one_reel -- at least this many scatters on a single reel: the case a ways-based
        #                      award count would get wrong, and pure luck without asking for it
        # A trigger that does not match is PLAYED OUT and the hunt continues -- leaving a bonus
        # owed would poison every round after it.
        self.arrive_when = arrive_when or {}

    def _matches(self, b: Bet) -> bool:
        want_reels = self.arrive_when.get("bonus_reels")
        want_stack = self.arrive_when.get("min_on_one_reel")
        raw = b.raw or {}
        if want_reels is not None:
            reels = GAME.bonus_reels(raw) if hasattr(GAME, "bonus_reels") else None
            if reels is None or len(reels) != int(want_reels):
                return False
        if want_stack is not None:
            most = GAME.max_scatters_on_a_reel(raw) if hasattr(GAME, "max_scatters_on_a_reel") else None
            if most is None or most < int(want_stack):
                return False
        return True

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        trigger, skipped = None, 0
        if self.arrive_when and not hasattr(GAME, "bonus_reels"):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason=f"`arrive_when` needs a profile that can read the scatter "
                                         f"census off the grid; {GAME.name} cannot")
        for _ in range(self.max_base):
            b = await ctx.base_spin()
            if is_freespin_trigger(b):
                if self._matches(b):
                    trigger = b
                    break
                skipped += 1
                ctx.mark(f"TRIGGER SKIPPED: wrong shape for {self.arrive_when} -- draining it")
                await play_out_bonus(ctx, b)
        if trigger is None:
            why = (f" matching {self.arrive_when} ({skipped} other trigger(s) played out)"
                   if self.arrive_when else "")
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason=f"no free-spin trigger{why} in {self.max_base} base spins")

        # The number on the spin button, read after each free spin settles, against what the
        # response says is left. Free spins are auto-played, so this is the only place it can be
        # sampled per round.
        counter_hits: list = []
        counter_readings: list = []

        async def _read_counter(bet: Bet) -> None:
            # ⚠ WAIT FOR THE ROUND TO FINISH PRESENTING. `on_round` fires the moment the RESPONSE
            # lands, and the spin button has not been redrawn yet -- it still shows the previous
            # count. Reading here compares this round's `freeSpins` against last round's label, so
            # every round mismatches by one and the case reports `0 == 8`: a perfect score for a
            # game that is counting down correctly.
            await ctx.ui_read(
                "async () => { const b = window.__gameTestBridge;"
                " for (let i = 0; i < 200; i++) {"
                "   const s = b && b.getSnapshot ? b.getSnapshot() : {};"
                "   const busy = s.busyOps || [];"
                "   if (!busy.includes('reels') && !busy.includes('winCountUp')"
                "       && !busy.includes('balanceCountUp') && !busy.includes('roundReady')) return true;"
                "   await new Promise(r => setTimeout(r, 50)); }"
                " return false; }")
            shown = await ctx.ui_read(
                "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
                " return s.freeSpinsRemaining; }")
            # Both values are kept, not just the verdict. A systematic offset is a different
            # finding from a random one, and `0 of 8` cannot tell them apart.
            counter_readings.append({"seq": bet.seq, "shown": shown, "response": bet.free_spins})
            counter_hits.append(shown == bet.free_spins)

        rounds = await play_out_freespins(ctx, trigger, on_round=_read_counter)
        # expose interesting sub-clips as marks (internal logic; QA just names them via `at:`)
        marks: dict = {}
        fs = [r for r in rounds if r.phase == "freespin"]
        if fs:
            marks["big_cascade"] = max(fs, key=lambda r: r.cascades).seq   # FS round with most cascades
        # How LONG the bonus actually was. A run with a tight `max_win_x` can have it capped after
        # one round (Rule 9), which still satisfies terminal_reached and {terminal.free_spins} == 0
        # while showing the player almost nothing -- so a case that grades the bonus UNFOLDING
        # (a counter counting down, a multiplier sampled across rounds) asserts this instead.
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(), marks=marks,
                              facts={"free_spins_played": len(fs),
                                     "capped": any(r.has_max_win for r in rounds),
                                     "total_rounds": len(rounds),
                                     "fs_rounds": len(fs),
                                     "counter_matches_rounds": sum(1 for ok in counter_hits if ok),
                                     "counter_readings": counter_readings,
                                     # every distinct (shown - response); [0] is correct, [1] is a
                                     # label one spin behind, a mixed list is a real inconsistency
                                     "counter_offsets": sorted({(r["shown"] - r["response"])
                                                                for r in counter_readings
                                                                if isinstance(r["shown"], (int, float))}),
                                     "triggers_skipped": skipped,
                                     # the trigger's own shape, for a case that grades the AWARD
                                     # against it (None on a profile that cannot read the grid)
                                     "trigger_bonus_reels": (len(GAME.bonus_reels(trigger.raw or {}) or [])
                                                             if hasattr(GAME, "bonus_reels") else None),
                                     "trigger_max_bonus_per_reel": (
                                         GAME.max_scatters_on_a_reel(trigger.raw or {})
                                         if hasattr(GAME, "max_scatters_on_a_reel") else None)})


class FreeSpinCascade(Scenario):
    name = "freespin-cascade"
    description = ("Trigger free spins and find a FREE SPIN with a cluster/cascade win (>= 2 cascades) "
                   "that is FOLLOWED by at least one more free spin — so both the multiplier CLIMB (that "
                   "cascade round) and the RESET on the next free spin are captured in one bonus. Retries "
                   "with fresh triggers if a bonus doesn't yield one. Exposes res = the cascade free spin; "
                   "marks: fs_cascade = that round, fs_reset = the following free spin.")

    def __init__(self, max_base: int = 400, max_triggers: int = 5):
        self.max_base = max_base          # base spins per attempt to reach a trigger
        self.max_triggers = max_triggers  # how many bonuses to try before giving up

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        for _ in range(self.max_triggers):
            trigger = None
            for _ in range(self.max_base):
                b = await ctx.base_spin()
                if is_freespin_trigger(b):
                    trigger = b
                    break
            if trigger is None:
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()),
                                      reason=f"no free-spin trigger in {self.max_base} base spins")
            rounds = await play_out_freespins(ctx, trigger)   # play the whole bonus; settles back to base
            fs = [r for r in rounds if r.phase == "freespin"]
            for i in range(len(fs) - 1):                       # need a FOLLOWING free spin for the reset
                if fs[i].cascades >= 2 and fs[i].payout > 0:   # a cascade WIN inside the bonus
                    return ScenarioResult(self.name, True, fs[i], rounds, ctx.timeline, ctx.events,
                                          (start, ctx.now_s()), round_windows=ctx.windows(),
                                          marks={"fs_cascade": fs[i].seq, "fs_reset": fs[i + 1].seq})
            # no qualifying cascade-then-spin in this bonus -> spin base again for another trigger
        return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                              reason=(f"no cascade free spin followed by another free spin in "
                                      f"{self.max_triggers} bonuses"))


async def open_bet_list(ctx: DriveContext, timeout_ms: int = 10000) -> bool:
    """Try to open the denomination list; return whether it ACTUALLY opened.

    `profile.HAS_BET_POPUP` records what the bridge doc says. This checks what the build does — they
    disagreed on Zeus (2026-08-27: the doc says nextBet opens the bet-amount popup; clicking it moves
    nothing, `visiblePopups` stays empty, and no `bet:*` name resolves). A scenario that trusted the
    flag reported "reached" while doing nothing at all, and its config-derived asserts passed.
    """
    opener = GAME.button("stake_up")
    if not opener:
        return False
    before = set(await ctx.popups())
    await ctx.click_button(opener, optional=True, timeout_ms=timeout_ms)
    after = set(await ctx.popups())
    label = GAME.popup("denominations")
    if label and label in after:
        return True
    return bool(after - before)          # some build may label it differently -- any NEW popup counts


async def close_bet_list(ctx: DriveContext, timeout_ms: int = 8000) -> dict:
    """Close the denomination list and PROVE it closed. Returns facts to expose.

    The mirror of `open_bet_list`, and it exists for the same reason: what the profile names and
    what the build does can disagree, and the failure is silent in the worst possible way.

    Clicking `bet_close` with `optional=True` swallows both failure modes — a name the bridge does
    not know, and a control that resolves but is inert (the contract calls that "the worst case for
    a test suite"). Either way the popup is still up, and the very next `settle()` calls
    `waitForSpinClickable`, which refuses for its WHOLE timeout because a visible popup is a
    blocking reason. Observed on Chaos: 8s on the close + 60s on the settle + a 3s pad — about 70
    seconds of apparent hang, with no error naming any of it.

    So: click, then CHECK `visiblePopups`. If it is still there, fall back to `dismissPopups()` —
    which recovers the run — and report that it was needed, because needing it is a bridge defect
    worth seeing rather than papering over.

    facts: `closed` · `close_button_worked` · `needed_dismiss` · `popups_after`
    """
    async def still_open() -> bool:
        """Is the list still up? ANY visible popup counts, not just the profile's label.

        This deliberately does NOT trust `label`. When the profile's spelling and the build's
        disagree (Chaos: `denominations` vs `denomination`) a label-only check answers "already
        closed" while the list is on screen — and because that is the early-return above, the
        function then reports closed / close_button_worked / needed_dismiss = True having clicked
        NOTHING. Every CR_004 assert passes on a popup that never went away, and the cost surfaces
        60s later inside settle(). Matching on "is any popup up" cannot fail that way: this is only
        ever called just after open_bet_list succeeded, so a visible popup IS the list. If some
        other popup were up, the worst case is one extra click and a dismissPopups() fallback —
        which is reported through `needed_dismiss` rather than hidden.
        """
        return bool(await ctx.popups())

    if not await still_open():
        return {"closed": True, "close_button_worked": True, "needed_dismiss": False,
                "popups_after": []}

    clicked = await ctx.click_button("bet_close", optional=True, timeout_ms=timeout_ms)
    worked = clicked and not await still_open()
    needed_dismiss = False
    if not worked:
        ctx.mark("BET LIST: close control did not close it — falling back to dismissPopups()")
        await ctx.dismiss_popups()
        needed_dismiss = True
    left = await ctx.popups()
    return {"closed": not await still_open(),
            "close_button_worked": bool(worked),
            "needed_dismiss": needed_dismiss,
            "popups_after": left}


_BET_CLOSE_ASK = (
    "the denomination list would not close via the `bet_close` control "
    "(profile name: %s). Either the bridge does not know that name, or it resolves but the click "
    "is inert. BRIDGE ASK: confirm the close control's name with `getAllButtonRects()` while the "
    "list is OPEN, and make it reject rather than resolve when it cannot do its job "
    "(docs/game-bridge-contract.md §8)."
)


# NOTE: an earlier version of this string asserted that NOTHING on the Chaos build opens the
# denomination popup. That was wrong — `stake_up` (`stakeIncrease` -> native `betPlus`) does open
# it, verified live 2026-09-02. It is kept generic because the scenarios that use it run on any
# build, and it must describe what the run OBSERVED rather than a conclusion about one game.
_BET_DRIVE_ASK = ("the stake control did not open a denomination popup and did not step the bet, so "
                  "there is no way to drive the bet on this build. BRIDGE ASK: a named control that "
                  "opens the bet popup, or main-screen steppers that work without it.")


async def close_opener_if_left(ctx: DriveContext, name: str) -> bool:
    """After closing a surface reached THROUGH a panel (profile `BUTTON_VIA`), close that panel
    too if the game went back to it rather than to the main screen. Grandma: History and the guide
    live in Settings, so closing History may leave `SettingPopup` open.

    Uses the panel's own close control (`<opener role>_close`, e.g. `settings_close`), falling back
    to `dismissPopups()`. Returns True when a panel was left open and was closed here; a no-op for
    a control on the main screen, or when the game already returned to it."""
    opener = GAME.via(name)
    panel = GAME.popup(opener) if opener else None
    if not panel or panel not in (await ctx.popups()):
        return False
    if not await ctx.click_button(f"{opener}_close", optional=True, timeout_ms=5000):
        await ctx.dismiss_popups()
    return True


async def close_history(ctx: DriveContext, timeout_ms: int = 8000, label: str = "") -> dict:
    """Close the History panel (and any detail over it), PROVE it closed, and SAY SO STEP BY STEP.

    The History twin of `close_bet_list`, and it exists because the same 70-second hang was
    reachable through this path. `_inspect` used to end with a bare `dismiss_popups()` followed by
    `wait_spin_clickable(spin_timeout_ms)` inside `except: pass` -- so a panel that refused to close
    cost the WHOLE 60s, once per inspected outcome, with no error naming any of it.

    **Why it logs every step rather than just returning a verdict.** `closed: False` says the screen
    was not clear; it does not say whether the close control was MISSING, present but INERT, or
    present and working with something else on top -- and those have three different fixes. So each
    observed step is written to BOTH channels: stdout, for whoever is watching the run, and the
    timeline, so the replay and the ACTION TIMELINE tell the same story. A healthy pass reads:

        close-history [normal_win]: open before -> history
        close-history [normal_win]: click `history:close` -> clicked in 0.44s; still open -> none
        close-history [normal_win]: CLOSED via the close button in 0.5s; spin clickable in 0.28s

    ...and the failing shape, which is the one worth reading carefully:

        close-history [normal_win]: open before -> history, historyDetail
        close-history [normal_win]: click `history:close` -> NOT CLICKABLE after 8.0s
        close-history [normal_win]: dismissPopups() verified closed -> history
     !! close-history [normal_win]: STILL OPEN -> historyDetail  (8.6s)

    facts: `closed` / `close_button_worked` / `needed_dismiss` / `popups_after` /
    `close_seconds` / `spin_ready_seconds`
    """
    tag = f"close-history [{label}]" if label else "close-history"
    t0 = ctx.now_s()

    async def open_now() -> list:
        # ANY visible popup counts, not just the profile's label -- for the reason spelled out in
        # close_bet_list: when the profile's spelling and the build's disagree, a label-only check
        # answers "already closed" while the panel is on screen.
        return list(await ctx.popups())

    def log(line: str, bad: bool = False) -> None:
        print(f"  {'!! ' if bad else ''}{tag}: {line}")
        ctx._step("close-history", f"{tag}: {line}", ctx.now_s())

    before = await open_now()
    log(f"open before -> {', '.join(before) or 'none'}")
    if not before:
        # Worth its own line rather than a silent early return: "there was nothing to close" and
        # "I closed it" are the same boolean, and only one of them exercises the close control.
        log("nothing was open -- no close attempted")
        return {"closed": True, "close_button_worked": True, "needed_dismiss": False,
                "popups_after": [], "close_seconds": round(ctx.now_s() - t0, 2),
                "spin_ready_seconds": None}

    # `history_close` is None on a build that publishes no close control; click_button treats a None
    # role as not-clickable rather than burning the timeout, so this costs nothing there.
    # A round DETAIL open on top of the list covers `history:close` -- a player closes the detail
    # first, then History. Without this every close waited out the full timeout on a covered button
    # and had to be rescued by dismissPopups() (Grandma, 2026-09-30).
    detail_popup, detail_close = GAME.popup("history_detail"), GAME.button("detail_close")
    if detail_popup and detail_close and detail_popup in before:
        td = ctx.now_s()
        shut = await ctx.click_button("detail_close", optional=True, timeout_ms=timeout_ms)
        log(f"click `{detail_close}` (the detail sits on top of the list) -> "
            f"{'clicked' if shut else 'NOT CLICKABLE'} in {round(ctx.now_s() - td, 2)}s",
            bad=not shut)

    role = GAME.button("history_close")
    tb = ctx.now_s()
    clicked = await ctx.click_button("history_close", optional=True, timeout_ms=timeout_ms)
    # History reached THROUGH a panel (profile `BUTTON_VIA`, Grandma: Settings) can close back
    # into that panel instead of the main screen. That is the game working, not the close failing.
    if clicked and await close_opener_if_left(ctx, "history"):
        log(f"closed the `{GAME.via('history')}` panel History was opened from")
    after_click = await open_now()
    worked = clicked and not after_click
    if role is None:
        log("no `history_close` control in this profile -- going straight to dismissPopups()")
    elif clicked:
        log(f"click `{role}` -> clicked in {round(ctx.now_s() - tb, 2)}s; "
            f"still open -> {', '.join(after_click) or 'none'}")
    else:
        log(f"click `{role}` -> NOT CLICKABLE after {round(ctx.now_s() - tb, 2)}s", bad=True)

    needed_dismiss = False
    if not worked:
        td = ctx.now_s()
        verified = await ctx.dismiss_popups()
        needed_dismiss = True
        # dismissPopups() returns only what it OBSERVED close -- a short list means something refused.
        log(f"dismissPopups() verified closed -> {', '.join(verified) or 'nothing'} "
            f"({round(ctx.now_s() - td, 2)}s)", bad=not verified)

    left = await open_now()
    secs = round(ctx.now_s() - t0, 2)
    spin_ready = None
    if left:
        # Do NOT go on to wait for spin readiness: a visible popup blocks it by contract, so the
        # wait could only end in a timeout. Say what is still up instead -- that IS the evidence.
        log(f"STILL OPEN -> {', '.join(left)}  ({secs}s)", bad=True)
        print("     a visible popup is a blocking reason for waitForSpinClickable, so every later "
              "wait in this run will refuse while that is up.")
        print(f"     BRIDGE ASK: {_HISTORY_CLOSE_ASK % (role or 'none')}")
    else:
        how = "the close button" if worked else "dismissPopups()"
        ts = ctx.now_s()
        try:
            await wait_spin_clickable(ctx.page, timeout_ms)
            spin_ready = round(ctx.now_s() - ts, 2)
            log(f"CLOSED via {how} in {secs}s; spin clickable in {spin_ready}s")
        except Exception:  # noqa: BLE001
            # The screen IS clear, so this is not the popup -- something else holds readiness.
            # Its own line, because it sends you to a different bug than "it would not close".
            log(f"CLOSED via {how} in {secs}s, but spin did NOT become clickable within "
                f"{timeout_ms / 1000}s -- the screen is clear, so look past the popup", bad=True)
    return {"closed": not left, "close_button_worked": bool(worked),
            "needed_dismiss": needed_dismiss, "popups_after": left,
            "close_seconds": secs, "spin_ready_seconds": spin_ready}


_HISTORY_CLOSE_ASK = (
    "the History panel would not close. The close control is `%s`; either the bridge does not know "
    "that name, it resolves but the click is inert, or dismissPopups() does not cover this popup. "
    "Confirm with `getAllButtonRects()` while History is OPEN, and make dismissPopups() report "
    "honestly -- it must return only what it VERIFIED closed "
    "(docs/game-bridge-contract.md; games/zeus/bridge-api.md §6)."
)


class ChangeBet(Scenario):
    name = "change-bet"
    description = ("Change the bet to a specific denomination. Takes whichever path the build offers: "
                   "open the denomination list and tap a row, or — when there is no list "
                   "(profile.HAS_BET_POPUP is False) — step the stake control until it lands. Places "
                   "NO bet, so there is no stream data; exposes facts.picked_bet / picked_index and "
                   "what it ACTUALLY landed on (landed_bet / landed_index / bet_changed), plus the "
                   "video window for an AI check that the on-screen amount matches.")

    def __init__(self, target_index: int | None = None):
        self.target_index = target_index    # denomination index to pick; None -> auto-pick a non-current one

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        denoms = await ctx.eval("() => { const b = window.__gameTestBridge; "
                                "const s = b.getSnapshot ? b.getSnapshot() : null; "
                                "return s ? s.denominations : null; }")
        cur = await ctx.eval("() => { const b = window.__gameTestBridge; "
                             "return b.getBetIndex ? b.getBetIndex() : -1; }")
        if not denoms:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), reason="no denominations available from the bridge")
        n = len(denoms)
        target = self.target_index if self.target_index is not None else (3 if n > 3 else 0)
        if cur is not None and cur == target:            # the current row is deliberately not re-selectable
            target = (target + 1) % n
        picked = denoms[target]

        # Ask the game, do not trust the flag: open the list and see whether it appeared.
        if await open_bet_list(ctx):
            ctx.mark(f"BET LIST: pick row {target} ({picked})")
            await ctx.click_button(GAME.button("bet_row", i=target), optional=True, timeout_ms=15000)
            await ctx.click_button(GAME.button("bet_close"), optional=True, timeout_ms=8000)
        else:
            # No list to open: the stake controls step the bet one denomination at a time. Walk from
            # the current index to the target, which exercises BOTH directions when the target is
            # below the current bet -- and is the only path a build like this offers.
            steps = target - (cur if isinstance(cur, int) and cur >= 0 else 0)
            role = "stakeIncrease" if steps > 0 else "stakeDecrease"
            ctx.mark(f"BET: step {'up' if steps > 0 else 'down'} {abs(steps)}x to {picked}")
            for _ in range(abs(steps)):
                if not await ctx.click_button(role, optional=True, timeout_ms=10000,
                                              dwell_ms=ctx.step_dwell_ms):
                    break
            await ctx.dwell()                            # let the final amount sit in frame
        await ctx.settle()
        landed = await ctx.eval("() => { const b = window.__gameTestBridge; "
                                "return b.getBetIndex ? b.getBetIndex() : -1; }")
        landed_bet = await ctx.eval("() => { const b = window.__gameTestBridge; "
                                    "return b.getBetAmount ? b.getBetAmount() : null; }")
        facts = {"picked_index": target, "picked_bet": picked, "prev_index": cur,
                 "landed_index": landed, "landed_bet": landed_bet, "bet_changed": landed != cur,
                 "denom_count": n, "min_bet": denoms[0], "max_bet": denoms[-1]}
        if landed == cur:
            # Nothing moved. That is not "the game refuses to change bet" -- it is this suite being
            # unable to DRIVE the change, so it is unreached (inconclusive), not a failure. A build
            # that later grows the missing control makes this pass with no code change.
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts,
                                  reason=(f"the bet never left index {cur} ({landed_bet}) -- " +
                                          _BET_DRIVE_ASK))
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()),
                              facts={"picked_index": target, "picked_bet": picked, "prev_index": cur,
                                     # what the game ACTUALLY landed on -- so "the bet changed" is a
                                     # hard check rather than an assumption that the taps worked
                                     "landed_index": landed, "landed_bet": landed_bet,
                                     "bet_changed": landed != cur,
                                     "denom_count": n, "min_bet": denoms[0], "max_bet": denoms[-1]})


class BrowseDenominations(Scenario):
    name = "browse-denominations"
    description = ("Open the bet-amount list and scroll to the HIGHEST option then the LOWEST, so a "
                   "SCROLLABLE list reveals both ends on screen (not every option is visible at once). "
                   "Places no bet. Exposes facts.denom_count / min_bet / max_bet / currency for an AI "
                   "check that the list matches the configured denominations.")

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        info = await ctx.eval("""() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const gi = b.getGameInfo ? b.getGameInfo() : null;
            // `getGameSettings()` is gone from the contract and from bridge v3 -- settings live
            // on getGameInfo(). The old fallback was already dead: both reads prefer gi.settings.
            const st = (gi && gi.settings) || {};
            return { denoms: s.denominations || st.denominations || [], currency: gi ? gi.currency : null }; }""")
        denoms = info.get("denoms") or []
        if not denoms:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), reason="no denominations available from the bridge")
        n = len(denoms)
        start_idx = await ctx.eval("() => { const b = window.__gameTestBridge; "
                                   "return b.getBetIndex ? b.getBetIndex() : -1; }")
        closing = {"closed": True, "close_button_worked": True, "needed_dismiss": False,
                   "popups_after": []}          # no list opened -> nothing to close
        shown = await open_bet_list(ctx)
        if shown:
            await ctx.scroll_bet_row(n - 1)                # scroll to the biggest option
            await ctx.scroll_bet_row(0)                    # then to the smallest option
            closing = await close_bet_list(ctx)            # ...and PROVE the list went away
        else:
            # No list on this build -- step the stake up a few notches and back so the range is
            # still shown on screen, which is what the AI check actually looks at.
            ctx.mark("BET: step up through several denominations")
            for _ in range(min(4, n - 1)):
                await ctx.click_button(GAME.button("stake_up"), optional=True, timeout_ms=10000,
                                       dwell_ms=ctx.step_dwell_ms)   # each amount must be seen
            await ctx.dwell()
            ctx.mark("BET: step back down")
            for _ in range(min(4, n - 1)):
                await ctx.click_button(GAME.button("stake_down"), optional=True, timeout_ms=10000,
                                       dwell_ms=ctx.step_dwell_ms)
            await ctx.dwell()
        await ctx.settle()
        landed = await ctx.eval("() => { const b = window.__gameTestBridge; "
                                "return b.getBetIndex ? b.getBetIndex() : -1; }")
        # `list_shown` is the proof the list was on screen. There is deliberately no "browsed = the
        # bet index moved" fact: this scenario only SCROLLS the list and closes it keeping the current
        # bet, so the index never moves on a working game -- asserting it went red on DR-02 while the
        # list plainly opened. The index pair stays as a diagnostic.
        facts = {"denom_count": n, "min_bet": denoms[0], "max_bet": denoms[-1],
                 "currency": info.get("currency"), "has_bet_popup": GAME.HAS_BET_POPUP,
                 "start_index": start_idx, "end_index": landed, "list_shown": shown}
        facts.update(closing)
        if not shown and landed == start_idx:
            # The denominations came from CONFIG, so asserting on them would pass whether or not the
            # player can actually see them. Without a list on screen there is nothing to check.
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts,
                                  reason=("the denomination list was never shown -- " + _BET_DRIVE_ASK))
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts)


class BootBase(Scenario):
    name = "boot-base"
    description = ("Sit in a clean base game (no spins) and record it. Exposes gameInfo facts — "
                   "bet_amount, bet_index, balance, currency, denom_count, min_bet, max_bet — for "
                   "the UI and denomination checks.")

    def __init__(self, dwell_s: float = 2):
        self.dwell_s = dwell_s     # seconds of idle base game to record for the UI check

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        info = await ctx.eval("""() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const gi = b.getGameInfo ? b.getGameInfo() : null;
            // `getGameSettings()` is gone from the contract and from bridge v3 -- settings live
            // on getGameInfo(). The old fallback was already dead: both reads prefer gi.settings.
            const st = (gi && gi.settings) || {};
            const d = s.denominations || st.denominations ||
                      (b.getDenominations ? b.getDenominations() : []) || [];
            return { balance: s.balance, betAmount: s.betAmount, betIndex: s.betIndex,
                     currency: gi ? gi.currency : null,
                     denoms: d, minBet: st.minBet, maxBet: st.maxBet,
                     defaultBetLevel: st.defaultBetLevel,
                     // The FIRST-SESSION RULE needs this: defaultBetLevel is what a brand-new
                     // player opens at, but a returning session opens at its own last stake.
                     lastBetStake: (gi && gi.lastBet)
                         ? (gi.lastBet.baseStake != null ? gi.lastBet.baseStake : gi.lastBet.stake)
                         : null }; }""")
        await asyncio.sleep(self.dwell_s)     # let the idle base UI sit in frame for the AI
        # A full-resolution still of the loaded base screen. "Is every UI element present and not
        # broken" is exactly the question a montage frame is worst at and a still is best at, so
        # this scenario captures one and a case can grade it with `image:` instead of `video:`.
        await ctx.screenshot("base")
        await ctx.settle()
        d = info.get("denoms") or []
        # `defaultBetLevel` is an INDEX into denominations, and `default_bet_known` is the gate:
        # without it, comparing against a missing value would read as a confident pass on no data.
        lvl = info.get("defaultBetLevel")
        default_amount = d[lvl] if (lvl is not None and 0 <= lvl < len(d)) else None
        # THE FIRST-SESSION RULE (docs/game-bridge-contract.md §5; games/zeus/bridge-api.md §3).
        # `defaultBetLevel` governs only a player's FIRST session -- every later one opens at
        # `lastBet.baseStake`. So the bet a correct game opens at is:
        #     expected_open_bet = lastBet ? lastBet.baseStake : denominations[defaultBetLevel]
        # A case that instead asserts `betIndex == defaultBetLevel` fails a correct game on every
        # returning session, which is most of them. That is what `opens_at_default` below does, and
        # it is kept UNCHANGED on purpose: no suite asserts it, the contract's changelog records the
        # decision to leave it, and quietly redefining a published fact is worse than adding one.
        # A "game opens at the default bet" case binds `opens_at_expected_bet`.
        last_stake = info.get("lastBetStake")
        first_session = last_stake is None
        expected_open = last_stake if not first_session else default_amount
        bet_amount = info.get("betAmount")
        facts = {"bet_amount": bet_amount, "bet_index": info.get("betIndex"),
                 "balance": info.get("balance"), "currency": info.get("currency"),
                 "denom_count": len(d), "min_bet": d[0] if d else info.get("minBet"),
                 "max_bet": d[-1] if d else info.get("maxBet"),
                 "default_bet_level": lvl, "default_bet_known": lvl is not None,
                 "default_bet_amount": default_amount,
                 # the first-session rule, and the inputs that decided it
                 "last_bet_stake": last_stake, "first_session": first_session,
                 "expected_open_bet": expected_open,
                 # `None` when nothing published an expectation -- NOT False, which would read as
                 # "the game opened at the wrong bet" on a build that simply did not say
                 "opens_at_expected_bet": (None if expected_open is None or bet_amount is None
                                           else abs(float(bet_amount) - float(expected_open)) < 1e-9),
                 # LATENT, and deliberately left as it was -- see the note above
                 "opens_at_default": (lvl is not None and info.get("betIndex") == lvl)}
        # shots=ctx.shots is what makes the captured still reachable by an `image:` assert --
        # capturing it is not enough, the result has to carry it.
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts, shots=ctx.shots)


class BaseSpin(Scenario):
    name = "base-spin"
    description = ("One base spin of ANY outcome (win or lose). Exposes res = that spin, for "
                   "balance-debit / spin-mechanic checks.")

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        b, rounds = await spin_and_finish(ctx)     # a base spin may TRIGGER -- play the bonus out
        return ScenarioResult(self.name, True, b, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(), marks={"spin": b.seq})


class AutoPlay(Scenario):
    name = "auto-play"
    description = ("Start auto-play for N spins (auto -> auto:count -> auto:confirm); the CLIENT then "
                   "auto-spins and we collect all N base results in order, then it stops. If an auto "
                   "spin TRIGGERS free spins, the client auto-plays the whole bonus — we drain those "
                   "rounds (they don't count toward N) and then resume collecting base spins. Exposes "
                   "rounds + facts.auto_count / rounds_played / bonus_count for the auto checks.\n"
                   "facts.stakes_seen / stake_constant answer 'the bet did not change during the "
                   "sequence' EXACTLY — an AI read of the bet label cannot tell 'never changed' from "
                   "'changed and changed back between two sampled frames'. "
                   "After the count is reached it WATCHES for another bet for stop_probe_ms and holds "
                   "the stopped screen for stop_dwell_ms, so 'auto stopped' is evidence "
                   "(facts.auto_stopped / extra_round_after_stop / auto_flag_after) and the clip "
                   "actually contains the stopped game instead of ending on the last payout.")

    _COUNT_INDEX = {10: 0, 20: 1, 30: 2, 50: 3}   # auto:count:<idx> -> spins

    @staticmethod
    def _count_at(i: int):
        """The spin count at an autoplay index, per the profile (None = unlimited)."""
        vals = GAME.AUTO_COUNTS
        return vals[i] if 0 <= i < len(vals) else None

    def _via_index(self, idx: int):
        """An index to select BEFORE the real one, so the count visibly changes. `None` when the
        target is not the panel's opening value and the selection is already observable.

        Only index 0 needs it — that is what the panel opens on. Prefer a FINITE, clearly different
        count (never the unlimited entry, whose label is a symbol rather than a number)."""
        if idx != 0:
            return None
        for i in range(len(GAME.AUTO_COUNTS) - 1, 0, -1):
            if GAME.AUTO_COUNTS[i] is not None:          # skip the Infinity entry
                return i
        return None

    def __init__(self, count: int = 10, stop_probe_ms: int = 6000, stop_dwell_ms: int = 3000,
                 setup_dwell_ms: int = 2000):
        self.count = count
        # The three SETUP clicks (open panel -> pick count -> confirm) each change the screen and
        # are then immediately replaced by the next one. At the default 2s dwell the montage samples
        # roughly two frames per step, one of which can land mid-animation -- so "the panel showed
        # the chosen count" is unwatchable, for a human and for the AI. Hold each one longer, and
        # MARK it so a timestamp-mode assert can say exactly when it happened.
        self.setup_dwell_ms = setup_dwell_ms
        # How long to WATCH FOR ANOTHER BET after the count is reached. Auto stopping is the claim
        # under test, so it is proved by nothing further being wagered -- not assumed because we
        # counted N. Doubles as footage of the stopped game.
        self.stop_probe_ms = stop_probe_ms
        # ...and how long to hold the settled, stopped screen afterwards, so the AI has frames of
        # "auto is over" rather than a clip that cuts the moment the last payout lands.
        self.stop_dwell_ms = stop_dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        idx = self._COUNT_INDEX.get(self.count, 0)
        # The panel OPENS on index 0 (Mystic: 10 — confirmed live, confirming with no count click
        # gives the same autoSpinsRemaining as clicking auto:count:0). Selecting the count it is
        # already showing changes nothing, on screen or in state, so "the player chose N" is
        # unwatchable and an AI asked to see it answers no, correctly.
        # Rather than pay for a longer run just to make the step visible, DETOUR: select a
        # different count first, then come back. The panel visibly moves out and back, the
        # selection control is demonstrably working, and the run is still the shortest one.
        # (Verified live: auto:count:2 then auto:count:0 lands on 10.)
        via_idx = self._via_index(idx)
        ctx.mark("OPEN the auto-play panel")
        await ctx.click_button(GAME.button("auto"), dwell_ms=self.setup_dwell_ms)
        if via_idx is not None:
            ctx.mark(f"SELECT {self._count_at(via_idx)} first, so the count visibly CHANGES")
            await ctx.click_button(GAME.button("auto_count", i=via_idx), dwell_ms=self.setup_dwell_ms)
        ctx.mark(f"CHANGE BACK to the {self.count}-spins option")
        await ctx.click_button(GAME.button("auto_count", i=idx), dwell_ms=self.setup_dwell_ms)
        ctx.mark("CONFIRM pressed")
        # dwell_ms=0: read the count the client actually accepted BEFORE spins start burning it
        await ctx.click_button(GAME.button("auto_confirm"), dwell_ms=0)
        started = await ctx.ui_read(_AUTO_JS, {}) or {}
        await ctx.dwell(self.setup_dwell_ms)
        rounds: list = []          # every recorded round (auto base spins + any drained bonus rounds)
        base_spins = 0             # count of AUTO BASE spins only -> this is what auto_count refers to
        bonus_count = 0            # how many auto spins triggered a bonus
        while base_spins < self.count:
            b = await ctx.observe_bet()                # next auto base spin (client fires it; no click)
            rounds.append(b)
            base_spins += 1
            if is_freespin_trigger(b):
                # the client now auto-plays the whole bonus; drain it (no settle -> auto resumes
                # the next base spin on its own) and don't count bonus rounds toward auto_count.
                bonus_count += 1
                fs = await play_out_bonus(ctx, b, settle=False)
                rounds.extend(fs[1:])                  # fs[0] is the trigger, already appended
        ctx.mark(f"LAST of {self.count} auto spins done -- watching for any further spin")
        await ctx.settle()                             # let the final auto spin's presentation finish
        stop = await confirm_stopped(ctx, self.stop_probe_ms, self.stop_dwell_ms,
                                     f"AUTO STOPPED after {self.count} spins")
        lock = await ctx.ui_read(_BUTTON_WATCH_READ_JS, {}) or {}
        return ScenarioResult(self.name, True, rounds[-1] if rounds else None, rounds,
                              ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(),
                              facts={"auto_count": self.count, "rounds_played": base_spins,
                                     # what the client accepted, read the instant auto started.
                                     # `rounds_played == auto_count` proves the count was applied;
                                     # this shows WHAT it applied when they disagree.
                                     "auto_spins_at_start": started.get("left"),
                                     "count_is_panel_default": idx == 0,
                                     # the count we detoured through to make the change visible
                                     "via_count": self._count_at(via_idx) if via_idx is not None else None,
                                     **stop,                    # stopped / extra_round_after_stop
                                     "auto_stopped": stop["stopped"],   # kept: cases bind this name
                                     "bonus_count": bonus_count, "total_rounds": len(rounds),
                                     # paid rounds only: a free spin legitimately costs 0, so
                                     # including them would always report the bet as "changed".
                                     "stakes_seen": len({r.stake for r in rounds
                                                         if r.phase != "freespin"}),
                                     # Which controls a player could still reach while the run was going. `[]` is the
                                     # claim: a bet stepper or a Buy Bonus button live mid-autoplay is a real defect.
                                     "buttons_polled_during_auto": int((lock or {}).get("polls") or 0),
                                     "buttons_enabled_during_auto": sorted(((lock or {}).get("enabled") or {}).keys()),
                                     "stake_constant": len({r.stake for r in rounds
                                                            if r.phase != "freespin"}) <= 1,
                                     "completed": base_spins >= self.count})


class SpinThenHistory(Scenario):
    name = "spin-then-history"
    description = ("Inspect a PLAIN BASE round in History for each outcome shape the MECHANISM can "
                   "produce: a LOSING spin, a NORMAL win (one paying step) and — on a cascade game "
                   "only — a CASCADE win (two or more). A single-grid game (line, hold-and-spin) "
                   "reports one paying step for any win, so `cascade_win` is dropped automatically "
                   "and the hunt is just lose/win; narrow it further with `params: {outcomes: [...]}`. "
                   "It keeps spinning and, the moment a spin matches an "
                   "outcome still outstanding, stops and inspects THAT round: open History, open the "
                   "newest row's bet detail, scroll it by ONE PAGE PER PAYING STEP (0 for a loss, 1 for "
                   "a normal win, N for a cascade) — `detail_scroll_steps` overrides that for a build "
                   "whose detail does not page, then close up and carry on hunting the rest. One "
                   "spin sequence therefore covers them all, instead of a run each hunting "
                   "separately. Each inspection is MARKED as `no_win` / `normal_win` / `cascade_win`, "
                   "so a `video:` assert targets one with `at:`; each round's clip spans its own spin "
                   "through its own History walk. A spin that TRIGGERS the bonus is drained and skipped "
                   "— a bonus ticket is a different shape (one sub-ticket per free spin) and belongs to "
                   "`freespin-then-history`. Exposes facts.<outcome>_found / _cluster_wins / _scrolls / "
                   "_detail_opened / _round_id / _stake / _payout, plus outcomes_found / all_outcomes / "
                   "spins / bonuses_drained. Unreached only if NO outcome was inspected at all.")

    # (fact key, mark label, predicate on the round's PAYING-STEP count -- see win_steps())
    #
    # `cascade_win` is impossible on a single-grid game: `win_steps()` reports 1 paying step for any
    # winning round outside a cascade mechanism, so nothing ever matches `n >= 2`. __init__ drops it
    # when GAME.mechanism != "cascade" rather than letting the hunt burn its whole budget on it.
    _OUTCOMES = (
        ("no_win",      "NO WIN: a losing spin",                    lambda n: n == 0),
        ("normal_win",  "NORMAL WIN: one paying step",              lambda n: n == 1),
        ("cascade_win", "CASCADE WIN: two or more paying steps",    lambda n: n >= 2),
    )
    # `_inspect` writes its facts as f"{key}_<suffix>", which a static walk over dict literals cannot
    # see — so every correct `{facts.no_win_stake}` assert would be reported as "not exposed" (and a
    # real typo would hide among the noise). Declaring the prefixes lets
    # tests/chaos/validate_suites.py expand prefix x suffix; a MISSPELLED SUFFIX still fails.
    # Keep in sync with _OUTCOMES.
    FACT_PREFIXES = tuple(k for k, _, _ in _OUTCOMES)

    def __init__(self, settle_before_history_s: float = 2.0, detail_scroll_steps: int | None = None,
                 dwell_ms: int = 2000, list_timeout_ms: int = 10000, max_spins: int = 80,
                 outcomes=None):
        self.settle_before_history_s = settle_before_history_s   # let the round reach History first
        # None -> DERIVE the page count from the response: one page per cluster win, so a losing spin
        # scrolls not at all. An int overrides it (debugging a detail that pages differently).
        self.detail_scroll_steps = detail_scroll_steps
        self.dwell_ms = dwell_ms                                 # 2s on screen after each open
        self.list_timeout_ms = list_timeout_ms                   # how long the list/detail may take
        self.max_spins = max_spins                               # budget for hunting all the outcomes
        # which outcomes to hunt; default all three. Narrow it to shorten a run.
        want = set(outcomes) if outcomes else {k for k, _, _ in self._OUTCOMES}
        if not outcomes and GAME.mechanism != "cascade":
            # A single-grid game cannot produce two paying steps in one round, so hunting for one
            # would burn the whole budget. Drop it rather than fail on it.
            want.discard("cascade_win")
        known = {k for k, _, _ in self._OUTCOMES}
        bad = want - known
        if bad:
            raise ValueError(f"unknown outcome(s): {', '.join(sorted(bad))} -- known: "
                             f"{', '.join(sorted(known))}")
        self.outcomes = [o for o in self._OUTCOMES if o[0] in want]

    async def _inspect(self, ctx: DriveContext, bet: Bet, key: str, facts: dict) -> None:
        """Open History, open the newest row's detail, scroll it by this round's cluster-win count,
        then clear the screen so the hunt can continue."""
        pages = (self.detail_scroll_steps if self.detail_scroll_steps is not None
                 else win_steps(bet))
        # The row is written server-side and can lag the settled animation, so WAIT BEFORE OPENING:
        # the list is built on open, and dwelling afterwards cannot recover a row that wasn't there.
        await asyncio.sleep(self.settle_before_history_s)
        ctx.mark(f"HISTORY [{key}]: open the panel")
        await ctx.click_button(GAME.button("history"), dwell_ms=self.dwell_ms)
        # Opening only KICKS OFF the list fetch -- wait for rows to actually render before reading.
        rows = await ctx.wait_for_history_rows(self.list_timeout_ms)
        top = rows[0] if rows else None                # index 0 = newest (bridge-qa-api.md)
        ids = [str(v) for k, v in ((top or {}).get("data") or {}).items()
               if k.lower() in ("betid", "roundid", "id", "gameroundid") and v not in (None, "")]
        detail_opened, scrolls = False, 0
        rect = (top or {}).get("rect") or {}
        if rect.get("centerX") is not None:
            before_popups = set(await ctx.popups())    # baseline for the "something new opened" test
            ctx.mark(f"BET DETAIL [{key}]: open the newest row")
            await ctx.page.mouse.click(rect["centerX"], rect["centerY"])
            await ctx.dwell(self.dwell_ms)             # detail content loads too
            detail_opened = await ctx.wait_for_history_detail(self.list_timeout_ms, before_popups)
            if detail_opened:
                if pages:
                    ctx.mark(f"BET DETAIL [{key}]: scroll {pages} page(s) -- one per cluster win")
                    scrolls = await ctx.scroll_popup(pages, label=f"bet detail ({key})")
                else:
                    ctx.mark(f"BET DETAIL [{key}]: no cluster win -- nothing below the fold")
                    await ctx.dwell(self.dwell_ms)     # still hold it in frame for the AI
        want_id = str(bet.round_id or "")
        facts.update({
            f"{key}_found": True,
            f"{key}_cluster_wins": win_steps(bet),
            f"{key}_win_clusters": win_clusters(bet),
            f"{key}_cascades": bet.cascades,
            f"{key}_scrolls": scrolls,
            f"{key}_history_rows": len(rows),
            f"{key}_detail_opened": detail_opened,
            f"{key}_round_id": want_id or "none",
            f"{key}_round_id_match": bool(want_id) and want_id in ids,
            f"{key}_stake": bet.stake,
            f"{key}_payout": bet.payout,
            # the payout AS THE CLIENT PRINTS IT (profile DISPLAY_*): what an AI read of the
            # detail's win amount must be compared with, not the raw figure
            f"{key}_payout_display": GAME.display_amount(bet.payout),
        })
        # Clear the screen (detail + panel) so the next spin can run, and PROVE it cleared. This
        # used to be `dismiss_popups()` plus a 60s `wait_spin_clickable` in a bare `except: pass`,
        # which turned a panel that would not close into a silent 60s hang per inspected outcome.
        # `facts.update({...})` with f-string keys, NOT `facts[f"..."] = `: tests/chaos/
        # validate_suites.py reads the per-outcome suffixes out of DICT LITERALS, so a subscript
        # assignment is invisible to it and every correct `{facts.normal_win_panel_closed}` would
        # be reported as "not exposed".
        closed = await close_history(ctx, label=key)
        facts.update({
            f"{key}_panel_closed": closed["closed"],
            f"{key}_popups_after": ",".join(closed["popups_after"]) or "none",
            # HOW it closed, so a green cannot hide a close control that never works: `closed` is
            # True whether the button did it or the dismissPopups() fallback rescued it, and only
            # the second is a bridge defect.
            f"{key}_close_button_worked": closed["close_button_worked"],
            f"{key}_needed_dismiss": closed["needed_dismiss"],
            # ...and how long, so the next 70-second stall is a NUMBER in the report rather than
            # something someone notices by watching the clock.
            f"{key}_close_seconds": closed["close_seconds"],
            f"{key}_spin_ready_seconds": closed["spin_ready_seconds"],
        })

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        rounds: list = []
        pending = list(self.outcomes)
        facts: dict = {k + "_found": False for k, _, _ in self._OUTCOMES}
        marks: dict = {}
        done: list = []
        bonuses = 0
        res = None
        for _ in range(self.max_spins):
            if not pending:
                break
            b = await ctx.base_spin()
            rounds.append(b)
            if is_freespin_trigger(b):
                # A BONUS round: its ticket holds one sub-ticket per free spin, which is what
                # `freespin-then-history` covers. Drain it and keep hunting -- leaving it as the
                # newest row would put a bonus ticket in front of a case about a normal bet.
                bonuses += 1
                fs = await play_out_bonus(ctx, b)
                rounds.extend(fs[1:])
                continue
            await ctx.settle()                         # base win/loss: let the presentation finish
            cluster_win_steps = win_steps(b)
            hit = next((o for o in pending if o[2](cluster_win_steps)), None)
            if hit is None:
                continue                               # an outcome we already have -> spin again
            key, label, _ = hit
            pending.remove(hit)
            ctx.mark(f"{label} (round {b.seq}, {cluster_win_steps} paying step(s))")
            marks[key] = b.seq                         # -> `video: at: <key>`
            await self._inspect(ctx, b, key, facts)
            done.append(key)
            if res is None or key == "cascade_win":
                res = b                                # richest round becomes {res.*}
        facts.update({"outcomes_found": ",".join(done) or "none", "outcome_count": len(done),
                      "all_outcomes": not pending,
                      "missing": ",".join(k for k, _, _ in pending) or "none",
                      "spins": len(rounds), "bonuses_drained": bonuses,
                      "is_base_round": bool(res) and res.game_state == BASE_STATE and res.stake > 0})
        if not done:
            return ScenarioResult(self.name, False, None, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason=f"no outcome inspected in {len(rounds)} spins "
                                         f"({bonuses} bonus round(s) drained)")
        return ScenarioResult(self.name, True, res, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks=marks, facts=facts)


class FreespinThenHistory(Scenario):
    name = "freespin-then-history"
    description = ("Play a WHOLE free-spin round, then inspect it in History: open the panel, open the "
                   "newest row's bet detail, and page through EVERY sub-ticket with the detail's own "
                   "next/prev nav — in a free-spin round each free spin is its own bet, so paging walks "
                   "the free-spin chain. Each sub-ticket is scrolled by ONE PAGE PER CLUSTER WIN, "
                   "derived from that spin's response, so a losing free spin is not scrolled at all. "
                   "Paging is driven by `getHistoryDetailState().hasNext`, NOT by page arithmetic: the "
                   "popup's nav accounts for the synthetic max-win page, so counting would be off by "
                   "one exactly on max-win rounds. After reaching the end it pages BACK to the first "
                   "ticket, so prev is exercised too. Exposes facts.fs_rounds / bet_count / "
                   "pages_visited / paged_back / bet_count_matches_rounds / detail_scrolls. Unreached "
                   "if no bonus is hit, History shows no row, or the detail never opens.")

    def __init__(self, cap: int = 60, settle_before_history_s: float = 2.0, dwell_ms: int = 2000,
                 list_timeout_ms: int = 10000, page_back: bool = True):
        self.cap = cap                                   # base spins to try before giving up
        self.settle_before_history_s = settle_before_history_s
        self.dwell_ms = dwell_ms
        self.list_timeout_ms = list_timeout_ms
        self.page_back = page_back                       # also walk prev back to the first ticket

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        trigger, spins = None, 0
        for _ in range(self.cap):
            b = await ctx.base_spin()
            spins += 1
            if is_freespin_trigger(b):
                trigger = b
                break
            await ctx.settle()
        if trigger is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"base_spins": spins},
                                  reason=f"no free-spin trigger in {spins} base spins")
        rounds = await play_out_freespins(ctx, trigger)   # drain the bonus, then settle
        # the round reaches History asynchronously -- wait BEFORE opening, the list is built on open
        await asyncio.sleep(self.settle_before_history_s)
        ctx.mark("HISTORY: open the in-game History panel")
        await ctx.click_button(GAME.button("history"), dwell_ms=self.dwell_ms)
        rows = await ctx.wait_for_history_rows(self.list_timeout_ms)
        facts = {"base_spins": spins, "fs_rounds": len(rounds) - 1, "history_rows": len(rows),
                 "bonus_total": round(sum(r.payout for r in rounds[1:]), 4),
                 "round_id": str(rounds[-1].round_id or trigger.round_id or "none")}
        if not rows:
            return ScenarioResult(self.name, False, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason="History rendered no rows -- the bonus round had not reached "
                                         "History yet, or the panel did not open")
        rect = (rows[0].get("rect") or {})
        if rect.get("centerX") is None:
            return ScenarioResult(self.name, False, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason="the newest History row exposed no clickable rect")
        ctx.mark("BET DETAIL: open the bonus round's ticket")
        await ctx.page.mouse.click(rect["centerX"], rect["centerY"])
        await ctx.dwell(self.dwell_ms)
        if not await ctx.wait_for_popup(GAME.popup("history_detail"), self.list_timeout_ms):
            return ScenarioResult(self.name, False, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason="the bet detail (historyDetail) never opened")
        st = await ctx.history_detail_state()
        # one expected page-count per bet in the ticket, in play order: the trigger, then each free
        # spin. Used to scroll each sub-ticket by its OWN cluster wins rather than a blanket number.
        expect = [win_steps(r) for r in rounds]
        facts["win_clusters"] = ",".join(str(win_clusters(r)) for r in rounds)   # diagnostic
        visited, scrolls = [], 0

        async def show(idx: int) -> None:
            nonlocal scrolls
            pages = expect[idx] if idx < len(expect) else 0
            if pages:
                ctx.mark(f"SUB-TICKET {idx + 1}: scroll {pages} page(s) -- one per cluster win")
                scrolls += await ctx.scroll_popup(pages, label=f"sub-ticket {idx + 1}")
            else:
                ctx.mark(f"SUB-TICKET {idx + 1}: no cluster win -- nothing to scroll")
                await ctx.dwell(self.dwell_ms)      # still hold it on screen for the AI

        idx = int(st.get("betIndex") or 0)
        visited.append(idx)
        await show(idx)
        guard = len(rounds) + 5                      # never trust hasNext to terminate on its own
        while st.get("hasNext") and len(visited) < guard:
            if not await ctx.click_button(GAME.button("detail_next"), optional=True, timeout_ms=8000,
                                          dwell_ms=self.dwell_ms):
                break
            st = await ctx.history_detail_state()
            idx = int(st.get("betIndex") or len(visited))
            visited.append(idx)
            await show(idx)
        paged_back = 0
        if self.page_back:
            ctx.mark("BET DETAIL: page BACK to the first sub-ticket")
            while st.get("hasPrev") and paged_back < guard:
                if not await ctx.click_button(GAME.button("detail_prev"), optional=True, timeout_ms=8000,
                                              dwell_ms=self.dwell_ms):
                    break
                st = await ctx.history_detail_state()
                paged_back += 1
        bet_count = int(st.get("betCount") or 0)
        facts.update({"bet_count": bet_count, "pages_visited": len(visited),
                      "detail_scrolls": scrolls, "paged_back": paged_back,
                      "reached_last_page": not bool(st.get("hasNext")) if not self.page_back else True,
                      "back_at_first": not bool(st.get("hasPrev")),
                      # the ticket should hold one bet per round we played (trigger + each free spin)
                      "bet_count_matches_rounds": bet_count == len(rounds),
                      "expected_pages": ",".join(str(x) for x in expect)})
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"trigger": trigger.seq}, facts=facts)


def spin_balance_needs_stake(balance: float, min_bet: float) -> bool:
    """Is the balance still enough to place the SMALLEST bet? Then the wallet alone has not created
    an insufficient state and the stake has to be raised above what is left."""
    return balance >= min_bet


def plan_insufficient(balance: float, min_bet: float):
    """Decide how to force a balance-below-stake state, given that Playsite moves only WHOLE
    currency units (so an integer withdraw preserves the balance's fractional part).

      ('wallet', remainder) -> withdraw floor(balance); the balance becomes `remainder` (its
                               fractional part). Whether `remainder` is already below `min_bet`
                               decides which stake the caller then spins at, but EITHER WAY the
                               withdrawal is the right first move: it is what brings a balance of
                               29,951.45 into the range where any denomination is unaffordable.
      ('bet', None)         -> nothing to withdraw (balance < 1) -> the caller must RAISE the stake
                               above the balance instead.

    Previously this returned ('bet', None) whenever `remainder >= min_bet`, which threw away the
    withdrawal entirely — and the caller then looked for a denomination above the FULL balance,
    found none (max bet 100 vs balance 29,951.45) and gave up. Withdrawing first and choosing the
    stake against what is LEFT combines the two strategies instead of treating them as exclusive.
    """
    whole = math.floor(balance)
    if whole >= 1:
        return ("wallet", round(balance - whole, 2))
    return ("bet", None)


class InsufficientBalance(Scenario):
    name = "insufficient-balance"
    description = ("Force a balance-below-stake state, then attempt a spin — the client should block it "
                   "with an out-of-money popup. Tries the WALLET first (a whole-unit Playsite withdrawal "
                   "down below the min bet); if the balance can't be shrunk that far, RAISES the stake "
                   "above the balance instead. Restores the wallet/bet at the end. Needs PLAYSITE_* config "
                   "for the wallet path.")

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        info = await ctx.eval("""() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const num = (x) => (typeof x === 'number' && !isNaN(x)) ? x : null;
            return { balance: num(b.getBalance ? b.getBalance() : (s ? s.balance : null)),
                     denoms: (s && s.denominations) || [],
                     betAmount: num(b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)) }; }""")
        balance = info.get("balance")
        cur_bet = info.get("betAmount")
        denoms = [float(d) for d in (info.get("denoms") or [])]
        if balance is None or not denoms:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason="need balance + denominations from the bridge to plan an insufficient state")
        balance = float(balance)
        # Work by VALUE, not array position: the bridge maps denominations[i] <-> bet:row:i, but
        # don't assume the array is sorted -- read the min as a value and keep each denom's index.
        orig, min_bet = balance, min(denoms)
        mode, remainder = plan_insufficient(balance, min_bet)

        chosen_bet = min_bet
        if mode == "wallet":
            try:
                await ctx.set_balance(remainder)       # withdraw floor(balance) -> balance = remainder
                await ctx.reload_and_start()            # client re-reads the reduced balance
                # ⚠ RE-READ THE BET. The reload does not keep the bet the previous run left: a
                # wallet of ~0.24 reopened at 0.10 where base-win had left 7.00. Judging "already
                # insufficient" on the pre-reload 7.00 made this spin at 0.10 -- affordable, so no
                # out-of-money popup, and TC-008 red on a game that behaved correctly (2026-09-29).
                cur_bet = await ctx.eval(
                    "() => { const b = window.__gameTestBridge; const s = b.getSnapshot ? b.getSnapshot() : {};"
                    " return b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null); }")
            except Exception as e:  # noqa: BLE001
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), reason=f"could not set balance: {e}")
            spin_balance = remainder
            balance = remainder                        # everything below judges the REMAINING money

        if spin_balance_needs_stake(balance, min_bet):
            # The wallet could not go below the minimum bet on its own (Playsite moves whole units,
            # so a 0.45 remainder survives a 29,951 withdrawal while the min bet is 0.20). Raise the
            # STAKE above what is left instead: pick the smallest denomination strictly greater than
            # the balance, and use its own array index for bet:row (correct whether or not the
            # denominations array is sorted).
            above = sorted(((d, i) for i, d in enumerate(denoms) if d > balance))
            if not above:
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()),
                                      reason=(f"balance {round(balance, 2)} >= max bet {max(denoms)} "
                                              f"and not reducible with whole-unit withdrawals"))
            chosen_bet, idx = above[0]
            if cur_bet is not None and float(cur_bet) > balance:
                # Already insufficient at the CURRENT stake -> don't touch the bet (re-selecting the
                # active row is a no-op the bridge never lets resolve). Just spin.
                chosen_bet = float(cur_bet)
            else:
                await ctx.click_button(GAME.button("stake_up"))          # open the denomination list
                await ctx.click_button(GAME.button("bet_row", i=idx))    # pick that stake
                await ctx.click_button(GAME.button("bet_close"), optional=True)
        spin_balance = round(balance, 2)

        # attempt a spin -> should be refused; hold 4s so the out-of-money popup is in frame
        await ctx.click_button(GAME.button("spin"), optional=True, dwell_ms=2000)
        result = ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                                (start, ctx.now_s()),
                                facts={"mode": mode, "spin_balance": spin_balance, "chosen_bet": chosen_bet,
                                       "min_bet": min_bet, "orig_balance": round(orig, 2)})
        return result


# ---------------------------------------------------------------------------
# Auto-play variants + Tutorial (Mah_010/015/016/017/018/020, Mah_057..063).
# These drive extra bridge affordances (auto:stop / auto:stopAtWin / auto:count:4=Infinity /
# info popup) that are documented but NOT yet exercised live — treat their outcomes as
# provisional until a first live run confirms the button behaviour.
# ---------------------------------------------------------------------------
async def _auto_next(ctx: DriveContext, rounds: list) -> Bet:
    """Observe one auto BASE spin; if it triggers free spins, drain the bonus (no settle, since
    auto resumes on its own). Appends every recorded round to `rounds`; returns the base bet."""
    b = await ctx.observe_bet()
    rounds.append(b)
    if is_freespin_trigger(b):
        fs = await play_out_bonus(ctx, b, settle=False)
        rounds.extend(fs[1:])
    return b


def _arrived_at(ctx: DriveContext, b: Bet) -> float:
    """Clip time a recorded round's response ARRIVED: the end of its `_record` timeline entry. For an
    auto round the framework was already listening for, that is when the client started the spin."""
    for e in reversed(ctx.timeline):
        if e.get("id") == f"{b.phase}-{b.seq}":
            return e["end_ms"] / 1000
    return ctx.round_starts[b.seq] if b.seq < len(ctx.round_starts) else ctx.now_s()


class AutoStopAtWin(Scenario):    # Mah_015
    name = "auto-stop-at-win"
    description = ("Enable 'stop at any win', run auto with an unlimited count so ONLY a win stops it, "
                   "and observe base spins until the first win. A win comes in TWO SHAPES and they "
                   "look nothing alike on screen, so `stop_on` picks which one this run is about:\n"
                   "  base_win -- the spin itself paid; the clip is one spin and a win presentation.\n"
                   "  freespin -- the spin awarded a FREE-SPIN FEATURE, which is then drained; the "
                   "triggering spin shows an award, not a line win, and the money arrives across the "
                   "bonus.\n"
                   "  any (default) -- whichever lands first. Fine for a smoke check, but a case "
                   "written for it cannot phrase a prompt: it does not know what will be on screen.\n"
                   "A stop of the wrong shape is DISCARDED — auto is restarted and the hunt resumes — "
                   "so a run is deterministic in what it captures (facts.discarded_stops counts them). "
                   "Pair `stop_on: freespin` with an easy-trigger paytable and a max-win cap, or the "
                   "hunt is long and the bonus is unbounded.\n"
                   "facts.won_payout is the WHOLE event: the spin's payout for a base win, the sum "
                   "across the drained bonus for a feature (the trigger round pays 0, so reporting its "
                   "payout published 0.0 as 'the win'). facts.won_via says which shape it was.\n"
                   "Marks `win` (the round that ended auto) and `win_end` (that event's last round, "
                   "whose window runs through the stop and the idle tail) — span them with "
                   "`at: [win, win_end]` to film trigger -> feature -> back to base. `tail_dwell_ms` "
                   "adds padding after the stop so the idle base screen is unmistakably in frame. "
                   "Uses auto:stopAtWin / auto:count:4 (Infinity).\n"
                   "Mark `lead_in` is a MOMENT, not a round: up to `lead_in_rounds` (3) auto spins "
                   "before the win, or the auto:confirm click when fewer were played — span "
                   "`at: [lead_in, win_end]` to show auto running BEFORE it stopped. "
                   "`min_spins_before_win` restarts auto when the win comes sooner than that many "
                   "non-winning auto spins, so the clip has spins to show (facts.spins_before_win / "
                   "lead_in_rounds / lead_in_from_auto_start).")

    def __init__(self, max_spins: int = 200, watch_ms: int = 6000, dwell_ms: int = 3000,
                 stop_on: str = "any", max_attempts: int = 6, tail_dwell_ms: int = 0,
                 lead_in_rounds: int = 3, min_spins_before_win: int = 0):
        self.max_spins = max_spins
        self.watch_ms, self.dwell_ms = watch_ms, dwell_ms
        known = ("any", "base_win", "freespin")
        if stop_on not in known:
            raise ValueError(f"unknown stop_on {stop_on!r} -- known: {', '.join(known)}")
        self.stop_on = stop_on            # which WIN SHAPE this run is about
        self.max_attempts = max_attempts  # restarts allowed when the stop is the other shape
        self.tail_dwell_ms = tail_dwell_ms   # extra hold on the idle base screen after the stop
        self.lead_in_rounds = lead_in_rounds              # auto spins before the win `lead_in` shows
        self.min_spins_before_win = min_spins_before_win  # fewer non-winning auto spins -> restart

    async def _start_auto(self, ctx: DriveContext) -> float:
        """Open the auto panel, enable stop-at-any-win, pick the unlimited count, confirm. Returns the
        clip time just before the confirm click -- where the client starts spinning on its own.

        The confirm click does NOT dwell. The client fires its first spin ON that click, and the
        default 2 s dwell let the whole spin play and settle before the framework was listening: its
        response was then taken from the bridge's buffer, and the round's clip window opened after
        the result was already on screen (Dragon VIP DV-15, 2026-09-15: spin at ~13.1 s, window from
        15.4 s). Listening at once puts the window where the spin is."""
        await ctx.click_button(GAME.button("auto"))
        await ctx.click_button(GAME.button("auto_stop_at_win"))       # enable stop-at-any-win
        await ctx.click_button(GAME.button("auto_count", i=len(GAME.AUTO_COUNTS) - 1))   # Infinity
        ctx.mark("AUTO CONFIRMED")
        confirmed_at = ctx.now_s()
        await ctx.click_button(GAME.button("auto_confirm"), dwell_ms=0)
        return confirmed_at

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        rounds: list = []
        win = win_end = None
        won_via, won_payout = "none", 0.0
        discarded: list = []
        lead_in_at, spins_before_win, lead_in = None, 0, 0

        for _ in range(self.max_attempts):
            confirmed_at = await self._start_auto(ctx)
            spins: list = []                  # THIS attempt's auto base spins, in order
            got = None
            for _ in range(self.max_spins):
                before = len(rounds)
                b = await _auto_next(ctx, rounds)
                spins.append(b)
                ctx.mark(f"AUTO SPIN {len(spins)}: payout {b.payout}")
                if is_freespin_trigger(b):
                    # THE TRIGGER ROUND ITSELF PAYS NOTHING. The award is free spins; the money
                    # arrives across the rounds `_auto_next` just drained. Sum the whole event.
                    got = ("freespin", round(sum(r.payout for r in rounds[before:]), 6), b)
                    break
                if b.payout > 0:
                    got = ("base_win", b.payout, b)
                    break
            if got is None:
                break                                  # budget spent without any win at all
            via, amount, b = got
            if self.stop_on in ("any", via) and len(spins) - 1 >= self.min_spins_before_win:
                won_via, won_payout, win, win_end = via, amount, b, rounds[-1]
                spins_before_win = len(spins) - 1
                lead_in = min(self.lead_in_rounds, spins_before_win)
                # Where the lead-in starts. Every auto round but the first is listened for BEFORE it
                # is fired, so its response arrives as that spin starts; the first one's may have
                # been buffered, so a lead-in reaching back to it starts at the confirm click.
                lead_in_at = (confirmed_at if lead_in == spins_before_win else
                              max(confirmed_at, _arrived_at(ctx, spins[spins_before_win - lead_in]) - 1.0))
                break
            # The other shape -- or the right one before auto had played enough spins to be SEEN
            # running. Auto has stopped either way (that is what stop-at-win does), so the run cannot
            # simply keep spinning: settle back to base and START AUTO AGAIN to keep hunting.
            discarded.append(via if self.stop_on not in ("any", via) else "too_early")
            await ctx.settle()

        ctx.mark("WIN LANDED")
        stop = await confirm_stopped(ctx, self.watch_ms, self.dwell_ms, "AUTO STOPPED at the win")
        stopped = stop["stopped"]
        # DO NOT stop auto here when the game failed to stop itself.
        #
        # This used to click auto:stop as a tidy-up. It made the CLIP LIE: the video window runs to
        # the end of the scenario, so the footage the AI grades would show auto halting — because
        # the framework halted it — and the prompt's "did auto stop on screen?" came back true on a
        # game that had just failed the very behaviour under test. The stream assert still went red
        # (`stopped` is read before any click), so the verdict was never wrong; but the video was
        # evidence for the opposite conclusion, which is the false-green shape Rule 10 is about.
        #
        # Nothing is left running: cleanup calls `stop_autoplay` once the asserts are graded, which
        # is the right place for repair -- after the evidence is captured, not inside it.
        if self.tail_dwell_ms:
            # Padding so the clip ends on the game plainly IDLE — after a feature the last thing the
            # AI sees would otherwise be the bonus outro, and "is auto still running?" is being asked
            # of a screen that is still animating.
            #
            # When auto did NOT stop, the tail is still recorded -- it is the proof. The mark says so,
            # so the action timeline does not caption a still-spinning game as sitting still.
            ctx.mark("IDLE TAIL -- base game sitting still after the win" if stopped else
                     "TAIL -- auto did NOT stop at the win; still running")
            await ctx.dwell(self.tail_dwell_ms)
        if win is None:
            reason = f"no win within {self.max_spins} auto spins"
            if discarded:
                reason = (f"no {self.stop_on} win in {self.max_attempts} attempt(s): the stop was "
                          f"{', '.join(discarded)} instead ('too_early' = a win before "
                          f"{self.min_spins_before_win} non-winning auto spin(s)). For "
                          f"stop_on=freespin use an easy-trigger paytable; for stop_on=base_win do NOT.")
            return ScenarioResult(self.name, False, None, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  facts={"stop_on": self.stop_on, "won_via": "none",
                                         "discarded_stops": len(discarded)},
                                  reason=reason)
        # `win` is the round that ENDED auto; `win_end` is the last round of that event -- the same
        # round for a base win, the terminal free spin for a feature. A suite spans them
        # (`at: [win, win_end]`) to get the whole win AND the stop: the last round's window runs to
        # the end of the scenario (see DriveContext.windows), so it includes confirm_stopped's hold
        # and the idle tail. Marking only `win` clipped a free-spin stop to the trigger spin, which
        # shows no win at all -- the AI reported win_shown=false, correctly.
        return ScenarioResult(self.name, True, win, rounds, ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(),
                              marks={"win": win.seq, "win_end": win_end.seq,
                                     "lead_in": {"t": round(lead_in_at, 3)}},
                              facts={"spins_to_win": len(rounds), "won_payout": won_payout,
                                     # auto base spins this attempt played before the winning one,
                                     # and how many of them the `lead_in` mark puts on the clip
                                     "spins_before_win": spins_before_win,
                                     "lead_in_rounds": lead_in,
                                     "lead_in_from_auto_start": lead_in == spins_before_win,
                                     "min_spins_before_win": self.min_spins_before_win,
                                     "won_via": won_via,            # base_win | freespin
                                     # A bare `{facts.won_via} == freespin` cannot be asserted: the
                                     # inline stream evaluator interpolates the VALUE into a Python
                                     # expression, so it becomes `freespin == freespin` -> NameError.
                                     # Assert this boolean instead; keep won_via for the prompt text.
                                     "won_by_feature": won_via == "freespin",
                                     "stop_on": self.stop_on,
                                     "trigger_payout": win.payout,  # 0.0 when the win was a feature
                                     "feature_rounds": (len(rounds) - 1 - rounds.index(win)
                                                        if won_via == "freespin" else 0),
                                     "discarded_stops": len(discarded),
                                     **stop, "auto_stopped": stopped})


class AutoUserStop(Scenario):    # Mah_017
    name = "auto-user-stop"
    description = ("Start auto with an unlimited count, let a few spins play, then the USER stops it "
                   "(auto:stop). Exposes facts.spins_before_stop / auto_stopped for the check that auto "
                   "halts immediately on demand.")

    def __init__(self, spins_before_stop: int = 3, watch_ms: int = 6000, dwell_ms: int = 3000):
        self.spins_before_stop = spins_before_stop
        self.watch_ms, self.dwell_ms = watch_ms, dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        await ctx.click_button(GAME.button("auto"))
        await ctx.click_button(GAME.button("auto_count", i=len(GAME.AUTO_COUNTS) - 1))         # Infinity so it won't stop on its own
        await ctx.click_button(GAME.button("auto_confirm"))
        rounds: list = []
        for _ in range(self.spins_before_stop):
            await _auto_next(ctx, rounds)
        ctx.mark(f"USER STOPS auto after {len(rounds)} spins")
        await ctx.click_button(auto_stop_button(), dwell_ms=2000)   # user stops auto
        # "it halted on demand" is a negative claim -- watch, then hold, so it is proved and filmed
        stop = await confirm_stopped(ctx, self.watch_ms, self.dwell_ms, "AUTO STOPPED by the user")
        return ScenarioResult(self.name, True, rounds[-1] if rounds else None, rounds, ctx.timeline,
                              ctx.events, (start, ctx.now_s()), round_windows=ctx.windows(),
                              facts={"spins_before_stop": len(rounds), **stop,
                                     "auto_stopped": stop["stopped"]})


class AutoReloadCancels(Scenario):    # Mah_018
    name = "auto-reload-cancels"
    description = ("Start auto, let a spin or two play, then RELOAD the game; auto should be cancelled "
                   "on re-entry. Exposes facts.spins_before_reload / auto_cancelled for the check that "
                   "auto does not resume after a reload.")

    def __init__(self, spins_before_reload: int = 2, watch_ms: int = 6000, dwell_ms: int = 3000):
        self.spins_before_reload = spins_before_reload
        # A reload lands on a FRESH, idle game, which looks exactly like "auto was cancelled" and
        # exactly like "the clip just ended". Watch for a bet and then hold, so the stillness is
        # evidence rather than the recording running out (Rule 4).
        self.watch_ms, self.dwell_ms = watch_ms, dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        await ctx.click_button(GAME.button("auto"))
        await ctx.click_button(GAME.button("auto_count", i=len(GAME.AUTO_COUNTS) - 1))
        await ctx.click_button(GAME.button("auto_confirm"))
        rounds: list = []
        for _ in range(self.spins_before_reload):
            await _auto_next(ctx, rounds)
        ctx.mark(f"RELOAD after {len(rounds)} auto spins")
        await ctx.reload_and_start()                   # reload should cancel auto
        # The reload is supposed to CANCEL auto. Proving that needs the same negative evidence:
        # nothing further wagered, the client's flag off, and footage of the game sitting still.
        stop = await confirm_stopped(ctx, self.watch_ms, self.dwell_ms, "AUTO CANCELLED by the reload")
        cancelled = stop["stopped"]
        return ScenarioResult(self.name, True, rounds[-1] if rounds else None, rounds, ctx.timeline,
                              ctx.events, (start, ctx.now_s()), round_windows=ctx.windows(),
                              facts={"spins_before_reload": len(rounds), **stop,
                                     "auto_cancelled": cancelled})


class AutoChangeBetFirst(Scenario):    # Mah_020
    name = "auto-change-bet-first"
    description = ("Change the bet to a specific denomination FIRST, then start auto for N spins; every "
                   "auto spin must use the chosen stake. Exposes res = the first auto spin + "
                   "facts.picked_bet for the response check {res.stake} == picked_bet.")

    def __init__(self, count: int = 10, target_index: Optional[int] = None):
        self.count = count
        self.target_index = target_index

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        info = await ctx.eval("""() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return { denoms: (s && s.denominations) || [],
                     betIndex: (b.getBetIndex ? b.getBetIndex() : -1) }; }""")
        denoms = [float(d) for d in (info.get("denoms") or [])]
        if not denoms:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), reason="no denominations from the bridge")
        cur, n = info.get("betIndex"), len(denoms)
        target = self.target_index if self.target_index is not None else (3 if n > 3 else 0)
        if cur is not None and cur == target:          # the active row isn't re-selectable
            target = (target + 1) % n
        picked = denoms[target]
        await ctx.click_button(GAME.button("stake_up"))        # dwells ~3s
        await ctx.click_button(GAME.button("bet_row", i=target))    # dwells ~3s
        await ctx.click_button(GAME.button("bet_close"), optional=True)
        idx = AutoPlay._COUNT_INDEX.get(self.count, 0)
        await ctx.click_button(GAME.button("auto")); await ctx.click_button(GAME.button("auto_count", i=idx)); await ctx.click_button(GAME.button("auto_confirm"))
        rounds: list = []
        base = 0
        while base < self.count:
            await _auto_next(ctx, rounds)
            base += 1
        await ctx.settle()
        first = next((r for r in rounds if r.phase == "auto"), rounds[0] if rounds else None)
        return ScenarioResult(self.name, True, first, rounds, ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(),
                              facts={"picked_bet": picked, "picked_index": target, "auto_count": self.count})


class AutoUntilEmpty(Scenario):    # Mah_010 / Mah_016
    name = "auto-until-empty"
    description = ("FUND the wallet for ~fund_spins spins at the current stake, then run auto with an "
                   "unlimited count until the balance runs out; the client should auto-STOP and show the "
                   "out-of-money popup. Sets the wallet to EXACTLY fund_spins x stake (the Playsite "
                   "API takes fractional amounts) to keep the run short -- but the END CONDITION is "
                   "the server balance falling below one bet, checked after every round, NOT a spin "
                   "count: an auto spin can win, and a free-spin trigger pays across a bonus, so the "
                   "wallet is not necessarily empty after fund_spins spins. Then confirms no further "
                   "round is wagered and holds on the stopped screen for the video. Exposes "
                   "facts.funded_balance / bet / spins_played / final_balance / balance_below_bet / "
                   "winning_rounds / stopped_on_empty; reached only if it really ran out. "
                   "Needs PLAYSITE_* config.")

    def __init__(self, fund_spins: int = 2, cap: int = 200, watch_ms: int = 6000, dwell_ms: int = 3000):
        self.fund_spins = fund_spins      # ~how many spins to fund before the wallet runs dry
        self.cap = cap
        self.watch_ms, self.dwell_ms = watch_ms, dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        # Size the drain off the ACTIVE stake, read from the bridge. The wallet target below is an
        # absolute figure (fund_spins x stake), so the current balance is not needed to compute it.
        info = await ctx.eval("""() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const num = (x) => (typeof x === 'number' && !isNaN(x)) ? x : null;
            return { bet: num(b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)),
                     denoms: (s && s.denominations) || [] }; }""")
        denoms = [float(d) for d in (info.get("denoms") or [])]
        bet = info.get("bet") if info.get("bet") else (min(denoms) if denoms else None)
        if ctx._wallet is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason="wallet not configured -- cannot fund a deterministic drain (set PLAYSITE_*)")
        if not bet:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason="no active stake / denominations from the bridge to size the drain")
        bet = float(bet)
        try:
            # EXACTLY fund_spins bets. This used to round the delta to a whole currency unit,
            # because the helper claimed Playsite could only move whole units -- it cannot only do
            # that, it takes fractional amounts and settles them to the cent (verified live to
            # 0.001 on 2026-09-03). The rounding made the funded balance an accident of whatever
            # the wallet happened to hold: from 11840.99 it funded 0.99, which is ONE spin at a
            # 0.6 stake, not the two that were asked for. Ask for the number we actually want.
            funded = round(self.fund_spins * bet, 2)
            # A long retry window, because the server refuses to shrink the balance for a while
            # after the game has been playing -- it reports "Insufficient balance" against a
            # balance that never moves, and it clears on its own. Funding runs right after load,
            # which is inside that window. Waiting is safe here: auto has not been started yet,
            # so nothing is spinning while the retry loop sleeps.
            await ctx.set_balance(funded, attempts=10, pause_s=2.0)
            await ctx.reload_and_start()                # client re-reads the funded balance
        except Exception as e:  # noqa: BLE001
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason=f"could not fund the wallet for a drain: {e}")

        await ctx.click_button(GAME.button("auto"))
        await ctx.click_button(GAME.button("auto_count", i=len(GAME.AUTO_COUNTS) - 1))
        await ctx.click_button(GAME.button("auto_confirm"))
        rounds: list = []
        # WATCH THE BALANCE, do not count spins.
        #
        # Funding `fund_spins x bet` does NOT mean the wallet is empty after that many spins: an
        # auto spin can WIN, which tops the balance back up, and a free-spin trigger pays across a
        # whole bonus. Treating "n spins have happened" as "the money is gone" would declare the
        # drain finished while the player could still afford to bet -- and the out-of-money popup
        # the case is about would never have been the reason auto stopped.
        #
        # So the funded amount is only a STARTING POINT that keeps the run short, and the actual
        # end condition is read from the server's own figure after each round: `b.balance` is the
        # balance with the stake debited and any payout credited. The drain is over when that drops
        # below one bet, whenever that happens to be.
        below_bet = False
        winning_rounds = 0
        final_balance = None
        for _ in range(self.cap):
            try:
                b = await ctx.observe_bet()
            except Exception:  # noqa: BLE001 -- no next auto spin came
                break
            rounds.append(b)
            if is_freespin_trigger(b):
                # The bonus pays into the balance across its rounds, so the figure that matters is
                # the LAST round of the feature, not the trigger.
                fs = await play_out_bonus(ctx, b, settle=False)
                rounds.extend(fs[1:])
                b = rounds[-1]
            if b.payout:
                winning_rounds += 1
            final_balance = b.balance
            if final_balance is not None and final_balance < bet:
                below_bet = True         # cannot afford another spin -- this is the real end
                break
        # Now the NEGATIVE evidence and the footage. confirm_stopped watches for a further wagered
        # round (there must be none) and then HOLDS on the stopped screen, because a couple of
        # seconds is not enough for the AI to see that the game is out of money and sitting still.
        ctx.mark("BALANCE BELOW ONE BET")
        stop = await confirm_stopped(ctx, self.watch_ms, self.dwell_ms, "AUTO STOPPED, out of money")
        # Both halves are required: the money really ran out, AND nothing was wagered after it did.
        stopped_on_empty = below_bet and not stop.get("extra_round_after_stop")
        if not below_bet:
            reason = (f"the balance never fell below one bet ({bet}) within {self.cap} auto spins -- "
                      f"{winning_rounds} of {len(rounds)} round(s) paid, leaving {final_balance}. "
                      f"Auto stopping here would not have been an out-of-money stop.")
        elif not stopped_on_empty:
            reason = f"a further round was wagered after the balance fell below one bet ({final_balance})"
        else:
            reason = None
        return ScenarioResult(self.name, stopped_on_empty, rounds[-1] if rounds else None, rounds,
                              ctx.timeline, ctx.events, (start, ctx.now_s()), round_windows=ctx.windows(),
                              facts={"funded_balance": funded, "bet": bet, "fund_spins": self.fund_spins,
                                     "spins_played": len(rounds), "stopped_on_empty": stopped_on_empty,
                                     # the server's final figure, and that it is under one bet --
                                     # the evidence that the stop was about MONEY and not something else
                                     "final_balance": final_balance,
                                     "balance_below_bet": below_bet,
                                     # wins are why a spin count cannot be trusted as the end condition
                                     "winning_rounds": winning_rounds,
                                     **stop},
                              reason=reason)


class OpenTutorial(Scenario):    # Mah_057 / 059 / 060
    name = "open-tutorial"
    description = ("Open the in-game Info / Tutorial popup and BROWSE THE WHOLE GUIDE: dwell on the top of "
                   "the page, then wheel DOWN to the bottom one viewport at a time (90% steps, so no band "
                   "of the document falls between two shots), pausing at each screenful, and rest at the "
                   "end. The tutorial spans several screens, so a clip of only the first screen cannot "
                   "support 'the content/images are all correct' — this exposes every screenful in one "
                   "clip instead. The page is a CROSS-ORIGIN WebView: only a real mouse wheel can scroll "
                   "it (the bridge is blocked by the browser) and its scroll position cannot be read back, "
                   "so the step count is fixed and over-wheeling harmlessly clamps at the end. Marks "
                   "TUTORIAL TOP / BOTTOM for the ACTION TIMELINE. Exposes facts.tutorial_open (the "
                   "WebView was really there, not an empty popup), reached_bottom (the full planned scroll "
                   "was issued — that the pixels MOVED is a video claim, not a bridge one) and "
                   "scroll_steps. Set scroll_back=True to also page back UP (Mah_058 next/previous). "
                   "Unreached if the info button never becomes clickable.")

    def __init__(self, dwell_s: float = 2, scroll_steps: int = 10, scroll_dwell_ms: int = 2000,
                 scroll_back: bool = False, shots: bool = True):
        self.dwell_s = dwell_s                  # hold on the FIRST screen before paging down
        self.scroll_steps = scroll_steps        # fixed: bottom cannot be detected cross-origin
        self.scroll_dwell_ms = scroll_dwell_ms  # hold per screenful so the AI sees each one
        self.scroll_back = scroll_back          # also page back up to the top (Mah_058)
        self.shots = shots

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        opened = await ctx.click_button(GAME.button("info"), optional=True,
                                        dwell_ms=ctx.step_dwell_ms)   # open info/tutorial
        ctx.mark("TUTORIAL TOP: the guide is open at the top of the page")
        await ctx.dwell(int(self.dwell_s * 1000))                  # hold so the first screen is in frame
        rect = await ctx.tutorial_rect() if opened else None
        down = up = 0
        # Shoot each screenful as we page down. The tutorial cases that ask about TEXT (spelling,
        # translation) and IMAGE QUALITY (blurred, broken, wrong asset) are exactly the ones a
        # downsampled video montage cannot answer — they need a full-resolution still per screen,
        # graded on its own. Scrolling one step at a time (rather than handing the whole count to
        # scroll_tutorial) is what makes a per-screen shot possible.
        if opened and self.shots:
            await ctx.screenshot("tutorial-01")
        # A build whose guide is a native popup rather than a cross-origin WebView reports no rect
        # (Zeus: HAS_TUTORIAL_WEBVIEW=False). Wheel the popup itself instead of skipping the scroll
        # entirely — otherwise every page after the first is simply never seen.
        if rect or opened:
            for i in range(self.scroll_steps):
                if rect:
                    down += await ctx.scroll_tutorial(1, dwell_ms=self.scroll_dwell_ms)
                else:
                    down += await ctx.scroll_popup(steps=1, dwell_ms=self.scroll_dwell_ms,
                                                   label="tutorial", fraction=0.9)
                if self.shots:
                    await ctx.screenshot(f"tutorial-{i + 2:02d}")
            ctx.mark("TUTORIAL BOTTOM: the guide has been paged to the end")
            await ctx.dwell(int(self.dwell_s * 1000))              # rest on the last screenful
            if self.scroll_back:
                up = await ctx.scroll_tutorial(self.scroll_steps, dwell_ms=self.scroll_dwell_ms, up=True)
                ctx.mark("TUTORIAL TOP AGAIN: paged back up to the start")
                await ctx.dwell(int(self.dwell_s * 1000))
        await ctx.dismiss_popups()                                 # close it so the next scenario is clean
        if not opened:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), reason="info/tutorial button not clickable")
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                              facts={"tutorial_open": bool(rect), "scroll_steps": down,
                                     "reached_bottom": bool(rect) and down == self.scroll_steps,
                                     "scrolled_back": up, "dwell_s": self.dwell_s,
                                     "shot_count": len(ctx.shots),
                                     "is_webview": bool(rect),
                                     "has_tutorial_webview": GAME.HAS_TUTORIAL_WEBVIEW},
                              shots=ctx.shots)


# ---------------------------------------------------------------------------
# Spin input, Turbo, Reconnect, FS-zero-balance, Reels-Glow (Mah_035-040 / 021-023 /
# 027-034 / 011 / 041-042). These drive real pointer/keyboard/network input and are mostly
# video-graded — timing-sensitive and LIVE-UNVERIFIED; treat first runs as smoke tests.
# ---------------------------------------------------------------------------
class SpinInputs(Scenario):    # Mah_035-040
    name = "spin-inputs"
    description = ("All spin INPUT variants in one recording: spin via the Spin button (035); spin via "
                   "Space (036); hold-Spin -> continuous auto-spin, release stops it (037/039); hold-Spin "
                   "during a spin -> sudden stop (038); double-click -> sudden stop (040). One shared "
                   "video graded per case (expected_by_case).\n"
                   "Exposes facts.click_rounds / key_rounds / hold_done / dblclick_done / rounds_total "
                   "— the EXACT half of 'one input started exactly one spin', which no amount of "
                   "watching the reels settles reliably: a double-fire looks like one spin on video "
                   "but is two responses in the stream.\n"
                   "HOLD: the paid spins that start while the button is held are COUNTED "
                   "(facts.hold_rounds) and so are any after release (spins_after_release) -- "
                   "'holding starts auto-spin, releasing stops it' as numbers, not an impression.\n"
                   "SUDDEN STOP: both gestures land on the build's STOP control (`stop`, falling "
                   "back to `spin`) WHILE the reels turn. Aiming them at `spin` waited for the spin "
                   "button to be clickable, which on a build with a separate stop control (Grandma) "
                   "is only once the reels have STOPPED -- so the 'sudden stop' landed on a finished "
                   "spin. facts.sudden_hold_done / dblclick_done say the gesture really landed "
                   "mid-spin; extra_spin_after_dblclick catches a double-click that fired a spin.")

    def __init__(self, hold_ms: int = 1800):
        self.hold_ms = hold_ms          # how long the spin button is held for the auto-spin hold

    async def _collect_bets(self, ctx: DriveContext, task) -> list:
        """Record every round response that arrives while `task` (a hold gesture) is running.
        Paid rounds are filed as `base`, free-spin rounds as `freespin`. No big-win skip here: the
        skip taps the screen, which would land in the middle of the hold."""
        got: list = []
        while not task.done():
            try:
                raw = await await_bet_response(ctx.page, 700)
            except Exception:  # noqa: BLE001 -- nothing in this slice; keep watching
                continue
            t0 = ctx.now_s()
            b = Bet.parse(raw, len(ctx.events))
            ctx._record(b, "base" if b.stake > 0 else "freespin", t0)
            got.append(b)
        return got

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        # each action is MARKED so the timestamp-mode ACTION TIMELINE pins its exact time for the AI.
        # Every spin here can TRIGGER free spins. The counters below therefore count PAID BASE
        # rounds only (base_rounds_since) -- a drained bonus must not read as "the input fired
        # extra spins" -- and each round is taken to a clean base with finish_round, so the run
        # never ends inside a bonus.
        ctx.mark("SPIN via the Spin button")                     # 035
        before = len(ctx.events)
        b1, r1 = await spin_and_finish(ctx)
        click_rounds = base_rounds_since(ctx, before)
        ctx.mark("SPIN via the Space key")                       # 036
        before = len(ctx.events)
        await ctx.press_key(" ")
        # phase="base": a Space press is a PAID BASE spin the player made. It is observed rather
        # than clicked only because the gesture is a key, and `base_rounds_since` counts by phase.
        b2 = await ctx.observe_bet(phase="base")
        r2 = await finish_round(ctx, b2)
        key_rounds = base_rounds_since(ctx, before)
        # ⚠ Mark text is read by the model (ACTION TIMELINE): name the GESTURE, never its outcome.
        ctx.mark("HOLD the Spin button, then release")           # 037 + 039
        hold_task = asyncio.create_task(
            ctx.hold_button(GAME.button("spin"), hold_ms=self.hold_ms, optional=True))
        hold_bets = await self._collect_bets(ctx, hold_task)     # every spin the hold fired
        hold_done = await hold_task
        # After release (the hold's dwell has already passed), a NEW paid spin means auto kept
        # going. A bonus the hold triggered keeps playing on its own -- those are free rounds,
        # not counted here.
        after_release: list = []
        try:
            raw = await await_bet_response(ctx.page, 3000)
            nb = Bet.parse(raw, len(ctx.events))
            ctx._record(nb, "base" if nb.stake > 0 else "freespin", ctx.now_s())
            after_release.append(nb)
        except BetRejected:            # a bet WAS sent (and refused): never read that as "stopped"
            raise
        except Exception:  # noqa: BLE001 -- nothing came: auto stopped with the release
            pass
        held = hold_bets + after_release
        last = held[-1] if held else None
        if last is not None and not is_terminal(last):           # the hold left a bonus running
            async def _rec(raw: dict) -> None:
                ctx._record(Bet.parse(raw, len(ctx.events)), "freespin", ctx.now_s())
            await drain_to_base(ctx.page, True, _rec, ctx.fs_timeout_ms, 500, assert_base=False)
        await ctx.settle()
        r_hold = [b for b in held if b.stake > 0]

        # SUDDEN STOP: aim at the STOP control, which is clickable only WHILE the reels turn.
        stop_ctl = GAME.button("stop") or GAME.button("spin")
        b3 = await ctx.base_spin()                               # 038: sudden stop via a brief hold mid-spin
        ctx.mark("SUDDEN STOP: hold the stop control during the spin")
        sudden_hold = await ctx.hold_button(stop_ctl, hold_ms=200, optional=True, timeout_ms=5000)
        r3 = await finish_round(ctx, b3)                         # settles, or drains a triggered bonus
        b4 = await ctx.base_spin()                               # 040: double-click sudden stop
        ctx.mark("SUDDEN STOP: double-click the stop control during the spin")
        dbl_done = await ctx.double_click_button(stop_ctl, optional=True, timeout_ms=5000)
        r4 = await finish_round(ctx, b4)
        # A double-click is two clicks: if the second one fired a SPIN, that is a double-fire --
        # one gesture, two paid rounds. Watch briefly for it, and finish it if it came.
        extra = None
        try:
            raw = await await_bet_response(ctx.page, 2000)
            extra = Bet.parse(raw, len(ctx.events))
            ctx._record(extra, "base", ctx.now_s())
            r4 = r4 + await finish_round(ctx, extra)
        except BetRejected:            # a bet WAS sent (and refused): never read that as "no double-fire"
            raise
        except Exception:  # noqa: BLE001 -- no extra spin: the double-click did not double-fire
            pass
        rounds = r1 + r2 + r_hold + r3 + r4
        return ScenarioResult(self.name, True, b1, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"btn": b1.seq, "space": b2.seq},
                              facts={"click_rounds": click_rounds, "key_rounds": key_rounds,
                                     "hold_done": hold_done, "hold_ms": self.hold_ms,
                                     # paid spins that started while the button was held
                                     "hold_rounds": sum(1 for b in hold_bets if b.stake > 0),
                                     # paid spins that started AFTER the release (auto kept going)
                                     "spins_after_release": sum(1 for b in after_release
                                                                if b.stake > 0),
                                     "stop_control": stop_ctl,
                                     "sudden_hold_done": sudden_hold,
                                     "dblclick_done": dbl_done,
                                     "extra_spin_after_dblclick": extra is not None,
                                     "rounds_total": len(rounds)})


class TurboModes(Scenario):    # Mah_021-023
    name = "turbo-modes"
    description = ("Turbo in one recording: toggle the Turbo button on and spin -> reels resolve faster "
                   "(021); press-and-hold the mouse on Spin -> turbo only while held (022); press-and-hold "
                   "Space -> turbo only while held (023). One shared video graded per case.\n"
                   "Reads getSpeedMode() around each step, so 'turbo really engaged' is an EXACT "
                   "check (facts.speed_before / speed_on / speed_off / speed_toggled) rather than an "
                   "AI impression that the reels looked faster — which is unreliable precisely on "
                   "the builds where turbo is subtly broken. facts.mouse_hold_done / key_hold_done "
                   "record that the hold gestures actually fired.\n"
                   "Starts with ONE normal-speed spin (baseline_spin, default on): it is the only "
                   "normal-speed spin in the clip — the spins after Turbo is switched off are the "
                   "HOLDS, which run in turbo on every game that has hold-for-turbo — so it is the "
                   "reference 'turbo is faster' is judged against. facts.baseline_speed, and "
                   "baseline_not_turbo (baseline_speed != speed_on, so independent of how a build "
                   "spells its modes).")

    def __init__(self, baseline_spin: bool = True):
        self.baseline_spin = baseline_spin

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        speed_before = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        rounds = []
        baseline_speed = None
        if self.baseline_spin:
            # Without this the clip opens on the turbo toggle, and "faster than a normal spin" has
            # no normal spin in it to be faster than.
            ctx.mark("NORMAL SPIN")
            _, rounds = await spin_and_finish(ctx)       # may TRIGGER -- plays the bonus out
            baseline_speed = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        ctx.mark("TURBO ON")
        turbo_on = await ctx.click_button(GAME.button("turbo"), optional=True)          # 021: Turbo ON
        speed_on = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        b, turbo_rounds = await spin_and_finish(ctx)  # may TRIGGER -- play the bonus out before toggling
        rounds = rounds + turbo_rounds
        ctx.mark("TURBO OFF")
        await ctx.click_button(GAME.button("turbo"), optional=True)          # back OFF so the holds test hold-turbo
        speed_off = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        ctx.mark("HOLD the mouse on Spin -> turbo while held")
        mouse_hold = await ctx.hold_button(GAME.button("spin"), hold_ms=1500, optional=True)
        await ctx.settle()                                                 # 022
        ctx.mark("HOLD the Space key -> turbo while held")
        await ctx.hold_key(" ", hold_ms=1500); await ctx.settle()          # 023
        return ScenarioResult(self.name, True, b, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              facts={"speed_before": speed_before, "speed_on": speed_on,
                                     "speed_off": speed_off, "baseline_speed": baseline_speed,
                                     # the reference spin really was NOT turbo (None: no baseline)
                                     "baseline_not_turbo": (None if baseline_speed is None
                                                            else baseline_speed != speed_on),
                                     # the real question: did the mode actually CHANGE when toggled?
                                     "speed_toggled": speed_on != speed_off,
                                     "has_speed_getter": speed_on != "UNKNOWN",
                                     "turbo_control_present": turbo_on,
                                     "mouse_hold_done": mouse_hold, "key_hold_done": True})


class ReconnectBrief(Scenario):    # Mah_027 / 029 / 033
    name = "reconnect-brief"
    description = ("Briefly drop the network (below the reconnect threshold) and restore it: the game "
                   "should show a reconnecting state and auto-resume where it was, balance/UI in sync. "
                   "After reconnect it PLAYS ONE MORE ROUND to prove the game is stable/playable again. "
                   "Covers a short disconnect (027), a network switch (029) and a short tab-away/return "
                   "(033). facts.recovered / played_after_reconnect.")

    def __init__(self, off_s: float = 6.0):
        self.off_s = off_s

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        b = await ctx.base_spin(); await ctx.settle()           # a known state before the outage
        await ctx.set_offline(True)
        await asyncio.sleep(self.off_s)
        await ctx.set_offline(False)
        await asyncio.sleep(4)                                   # allow the client to reconnect
        recovered = False
        try:
            await wait_spin_clickable(ctx.page, ctx.spin_timeout_ms); recovered = True
        except Exception:  # noqa: BLE001
            pass
        # play ONE more round after reconnect to confirm the game is stable and actually playable
        rounds, played_after = [b], False
        if recovered:
            try:
                nb = await ctx.base_spin()
                rounds.append(nb)
                if is_freespin_trigger(nb):
                    await play_out_bonus(ctx, nb)           # drain if this spin triggers FS (no settle hang)
                else:
                    await ctx.settle()
                played_after = True
            except Exception:  # noqa: BLE001
                pass
        return ScenarioResult(self.name, True, b, rounds, ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(),
                              facts={"offline_s": self.off_s, "recovered": recovered,
                                     "played_after_reconnect": played_after})


class ReconnectLong(Scenario):    # Mah_028
    name = "reconnect-long"
    description = ("Drop the network LONGER than the reconnect threshold (>20s), then restore it: the game "
                   "should NOT silently resume -- it shows a popup and returns to Landing/Home per design. "
                   "Video-graded.")

    def __init__(self, off_s: float = 25.0):
        self.off_s = off_s

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        b, rounds = await spin_and_finish(ctx)    # may TRIGGER -- finish the bonus before going offline
        await ctx.set_offline(True)
        await asyncio.sleep(self.off_s)
        await ctx.set_offline(False)
        await asyncio.sleep(6)                                   # let the disconnect popup / redirect show
        result = ScenarioResult(self.name, True, b, rounds, ctx.timeline, ctx.events,
                                (start, ctx.now_s()), round_windows=ctx.windows(),
                                facts={"offline_s": self.off_s})
        # The session is OVER by design (the popup is the case). Re-enter AFTER the window closed,
        # so the evidence ends on the popup and the next run does not start on a dead client.
        await ctx.fresh_session("reconnect-long ended the session")
        return result


class ReconnectDuringAuto(Scenario):    # Mah_031
    name = "reconnect-during-auto"
    description = ("Start auto-play, briefly drop the network mid-auto, then restore it: the game should "
                   "reconnect and handle auto per design (resume or stop) with NO data desync. "
                   "facts.recovered = playing again, by EITHER allowed path: an auto bet arrives after "
                   "the reconnect (auto_resumed) or the spin control becomes usable (auto stopped). "
                   "The rounds after the reconnect (up to rounds_after) are recorded, so conservation "
                   "covers them — a spin replayed on reconnect debits twice. Auto is then stopped so "
                   "the next run starts idle. facts.rounds_after_reconnect.")

    _COUNT_INDEX = {10: 0, 20: 1, 30: 2, 50: 3}

    def __init__(self, count: int = 10, off_s: float = 6.0, resume_wait_s: float = 30.0,
                 rounds_after: int = 2):
        self.count = count
        self.off_s = off_s
        self.resume_wait_s = resume_wait_s    # how long after the reconnect an auto bet may take
        self.rounds_after = rounds_after      # auto rounds recorded after the reconnect

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        idx = self._COUNT_INDEX.get(self.count, 0)
        await ctx.click_button(GAME.button("auto")); await ctx.click_button(GAME.button("auto_count", i=idx)); await ctx.click_button(GAME.button("auto_confirm"))
        rounds: list = []
        try:
            rounds.append(await ctx.observe_bet())              # let one auto spin land first
        except Exception:  # noqa: BLE001
            pass
        await ctx.set_offline(True)
        await asyncio.sleep(self.off_s)
        await ctx.set_offline(False)
        # RECOVERED has two allowed shapes, and the old check knew only one. It waited for the spin
        # control to become clickable — which never happens while auto is still RUNNING, so a game
        # that resumed auto correctly read as "not recovered" (2026-10-01: the AI saw auto carry on
        # after "Your connection is unstable"; recovered was False). So: first watch for the next
        # auto bet (auto resumed); only if none comes, ask whether auto stopped and play is possible.
        recovered = resumed = False
        after: list = []
        try:
            after.append(await ctx.observe_bet(timeout_ms=int(self.resume_wait_s * 1000)))
            recovered = resumed = True
            while len(after) < self.rounds_after:
                after.append(await ctx.observe_bet())
        except Exception:  # noqa: BLE001 -- no (further) auto bet: judged below / already resumed
            pass
        if not resumed:
            try:
                await wait_spin_clickable(ctx.page, ctx.spin_timeout_ms); recovered = True
            except Exception:  # noqa: BLE001
                pass
        rounds += after
        result = ScenarioResult(self.name, True, rounds[-1] if rounds else None, rounds,
                                ctx.timeline, ctx.events, (start, ctx.now_s()),
                                round_windows=ctx.windows(),
                                facts={"offline_s": self.off_s, "recovered": recovered,
                                       "auto_resumed": resumed,
                                       "rounds_after_reconnect": len(after)})
        # Leave the game idle: a resumed auto run still has spins left. The teardown / cleanup then
        # play out anything it triggered.
        # auto_stop_button(), NOT GAME.button("auto_stop"): on Grandma `auto:stop` lives inside the
        # auto PANEL, which is closed while auto runs, so that click never landed and auto kept
        # betting into the teardown. The helper picks the build's main-screen stop first.
        await ctx.click_button(auto_stop_button(), optional=True, timeout_ms=5000)
        return result


class MultiTabSession(Scenario):    # Mah_030
    name = "multi-tab-session"
    description = ("Open the SAME account in a second session (a fresh browser context on the same URL) "
                   "while this one is playing: the original session should be invalidated per design — a "
                   "popup and/or a return to Landing/Home. The recorded video is THIS (first) tab, so it "
                   "captures the invalidation. Needs the runner to pass browser + url into the context.")

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        # may TRIGGER -- finish the bonus before opening the second session, or the takeover lands
        # mid-bonus and the free-spin rounds are never consumed
        b, rounds = await spin_and_finish(ctx)                  # tab 1 is active and playing
        try:
            await ctx.open_second_session()                    # same account, new context -> second login
        except Exception as e:  # noqa: BLE001
            return ScenarioResult(self.name, False, b, rounds, ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason=f"could not open a second session: {e}")
        await asyncio.sleep(6)                                  # let the server invalidate this (first) tab
        try:
            result = ScenarioResult(self.name, True, b, rounds, ctx.timeline, ctx.events,
                                    (start, ctx.now_s()), round_windows=ctx.windows(),
                                    marks={"spin": b.seq},
                                    # the second login is the whole premise: if it never happened the
                                    # video shows a healthy tab and "no takeover popup" means nothing
                                    facts={"second_opened": True, "wait_s": 6})
        finally:
            await ctx.close_second_session()
        # The second login kicked THIS session out by design. Re-enter on a fresh token, after the
        # window closed, so the next run does not start on the takeover popup.
        await ctx.fresh_session("multi-tab-session ended the session")
        return result


class FreespinZeroBalance(Scenario):    # Mah_011
    name = "freespin-zero-balance"
    description = ("Spend the player's LAST STAKE into a free-spin trigger, then reload and prove the "
                   "owed free spins still play out with nothing left in the wallet -- a free-spin round "
                   "does not check the balance, because it costs nothing. Each attempt funds the wallet "
                   "with EXACTLY one bet, reloads so the client re-reads it, and spins: the stake is "
                   "debited, so a spin that triggers leaves the player broke with a bonus owed. One free "
                   "spin is then played and HELD on (observe_ms) so the award and its result are "
                   "watchable, and the game is reloaded with the bonus still pending. An attempt is "
                   "DISCARDED and re-hunted when it does not trigger, or when the trigger or the first "
                   "free spin hits the max-win cap (the server truncates back to base, leaving nothing "
                   "for the reload to resume). Streams: stake_zero_in_freespins + conservation over the "
                   "resumed rounds. Exposes facts.broke_at_trigger / balance_after_trigger / "
                   "observed_before_reload / fs_rounds_resumed / attempts_discarded. "
                   "Needs PLAYSITE_*; restore the wallet with `teardown: restore-balance`.")

    # WHY THE WALLET IS SET BEFORE THE SPIN AND NEVER DURING THE BONUS
    #
    # The obvious shape -- trigger the bonus, then drain the wallet -- cannot be made to work. The
    # server will not release the balance while a bonus is open or play is recent: measured live
    # 2026-09-03, a withdrawal was refused against a completely UNCHANGED balance across 45 retries
    # over 90s, with the client already torn down, and the identical withdrawal succeeded minutes
    # later. There is also a hard floor of 0.01 -- the wallet never reaches exactly 0.00.
    #
    # So the player is made broke the way a real one would be: given one bet and asked to spend it.
    # The debit happens inside the spin, server-side, so there is no wallet call to be refused and
    # no race with the auto-playing bonus. That also removes the need for `stop_client()` here.

    def __init__(self, max_attempts: int = 25, observe_ms: int = 4000, reload_mid_bonus: bool = True):
        self.max_attempts = max_attempts
        self.observe_ms = observe_ms      # hold on the first free spin so its result can be judged
        # True (the original CR_011 shape): play ONE free spin, then reload with the rest owed and
        # prove they resume. False: no reload -- play the bonus from its first spin to its last,
        # which is what a "keep playing with an empty wallet" requirement asks for.
        self.reload_mid_bonus = reload_mid_bonus

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        if ctx._wallet is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason="wallet not configured -- set PLAYSITE_* to fund the wallet")

        # The stake to fund is the game's CURRENT bet, read from the client rather than assumed: a
        # hardcoded figure would silently fund the wrong amount if the opening bet ever changed, and
        # the player would still be able to spin at the reload.
        info = await ctx.eval("""() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const num = (x) => (typeof x === 'number' && !isNaN(x)) ? x : null;
            return { betAmount: num(b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)) }; }""")
        stake = info.get("betAmount")
        if not stake:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason="could not read the current bet from the bridge -- cannot fund one stake")
        stake = float(stake)

        trigger = first = None
        no_trigger = triggers_capped = first_fs_capped = trigger_paid = 0
        attempts = 0

        for attempt in range(self.max_attempts):
            attempts = attempt + 1
            # Fund EXACTLY one bet. This is the only wallet call in the scenario, and it lands with
            # no bonus open (a discarded attempt is settled back to base first), which is the state
            # the server will actually let the balance be set in.
            try:
                await ctx.set_balance(stake, attempts=10, pause_s=2.0)
            except Exception as e:  # noqa: BLE001
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()),
                                      reason=f"could not fund the wallet with one stake ({stake}): {e}")
            # The client caches the balance, so it must re-read it before the spin -- otherwise it
            # blocks the bet as unaffordable using the figure it still remembers from the last spin.
            await ctx.reload_and_start()

            b = await ctx.base_spin()          # the stake is debited HERE -> the player is now broke
            if not is_freespin_trigger(b):
                no_trigger += 1
                await ctx.settle()
                continue
            # The run caps the payout (`max_win_x`) so a forced free-spin paytable cannot spin
            # forever. A side effect: a round can reach that cap, and the server then TRUNCATES back
            # to base with `hasReachedMaxWin` set and the award forfeited -- on the trigger itself
            # (no bonus at all) or on the first free spin (bonus over before the reload). Either way
            # nothing is left for the reload to resume, so the attempt is unusable. That is a normal
            # consequence of the cap, not a failure: settle and hunt another one.
            if b.has_max_win or is_terminal(b):
                triggers_capped += 1
                await ctx.settle()
                continue
            # The premise is that the player CANNOT AFFORD A BET when the bonus begins. The
            # triggering spin can itself pay a line win, which puts them back in funds -- so an
            # attempt that leaves a stake or more is not the state under test. Discard it and
            # hunt another. `b.balance` is the balance AFTER the stake was debited and any payout
            # credited, which is exactly the figure to judge on.
            if b.balance is None or b.balance >= stake:
                trigger_paid += 1
                await ctx.settle()
                continue

            if not self.reload_mid_bonus:
                # No reload: the player is broke from this spin on, and the bonus simply has to
                # play to its last round. The whole bonus is played below, after the hunt.
                trigger = b
                break

            # Play ONE free spin and HOLD on it before reloading.
            #
            # The award presentation and the first bonus round land in the couple of seconds right
            # after the trigger; reloading immediately replaced them with a loading screen, so the
            # result the case asks about was never on screen long enough to judge. It is also the
            # ONLY round the video needs -- the remaining spins play out after the reload, which is
            # the behaviour under test, but they are graded from the response stream. `marks` exposes
            # `first_fs` so the suite can ask for `at: [trigger, first_fs]`.
            f = None
            try:
                f = await ctx.free_spin()
                ctx.mark("FIRST FREE SPIN played -- the wallet is spent and the game reloads AFTER this")
                await ctx.dwell(self.observe_ms)
            except Exception:  # noqa: BLE001 -- client may auto-play the bonus; handled as a discard below
                pass
            if f is None or f.has_max_win or is_terminal(f):
                first_fs_capped += 1
                await ctx.settle()                    # bonus is over -> back at base, hunt again
                continue
            trigger, first = b, f
            break

        if trigger is not None and not self.reload_mid_bonus:
            # The requirement's own shape (Grandma TC-010: "trigger FS -> let the balance reach 0 ->
            # keep playing"): the trigger spin spent the last stake, and every free spin now plays
            # from the start of the bonus to its end, uninterrupted, with the wallet empty. The
            # drain also answers a Free Spin Extension offer at the end.
            ctx.mark("FREE SPINS START: the balance is spent")
            rounds = await play_out_freespins(ctx, trigger, cap=500, settle=True)
            fs = rounds[1:]
            return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks={"trigger": trigger.seq, "terminal": rounds[-1].seq},
                                  facts={"stake": stake,
                                         "balance_after_trigger": trigger.balance,
                                         "broke_at_trigger": (trigger.balance is not None
                                                              and trigger.balance < stake),
                                         "triggers_paid_out": trigger_paid,
                                         "funded_attempts": attempts,
                                         "attempts_no_trigger": no_trigger,
                                         "triggers_capped": triggers_capped,
                                         # every free spin, start to end, with no reload between
                                         "fs_rounds_played": len(fs),
                                         "played_after_zero": len(fs) > 0,
                                         "reloaded_mid_bonus": False})

        if trigger is None or first is None:
            reason = (f"no usable free-spin trigger in {attempts} funded attempt(s): "
                      f"{no_trigger} spin(s) did not trigger, {triggers_capped} trigger(s) and "
                      f"{first_fs_capped} opening free spin(s) hit the max-win cap and were truncated "
                      f"back to base leaving no bonus for the reload to resume, and {trigger_paid} "
                      f"trigger(s) paid enough to leave the player able to bet again")
            if triggers_capped or first_fs_capped:
                reason += " -- raise the run's `max_win_x`"
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  facts={"triggers_capped": triggers_capped,
                                         "attempts_no_trigger": no_trigger,
                                         "triggers_paid_out": trigger_paid,
                                         # bool = "did the graded attempt cap?"; the COUNT of
                                         # discarded attempts is `attempts_discarded`
                                         "first_fs_capped": bool(first_fs_capped),
                                         "attempts_discarded": first_fs_capped},
                                  reason=reason)
        observed = [first]

        # Reload with the bonus still owed. No wallet call and no `stop_client()`: the balance was
        # already spent by the trigger spin, so there is nothing to race and nothing to be refused.
        # START resumes the owed bonus and `reload_and_start` hands those rounds back -- draining
        # again here would wait on responses that have already been consumed.
        try:
            resumed = await ctx.reload_and_start()
        except Exception as e:  # noqa: BLE001
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                                  reason=f"could not reload with the bonus pending: {e}")
        rounds = [trigger] + observed + resumed
        if not resumed or not is_terminal(resumed[-1]):
            try:                                                # not terminal yet -> keep draining
                for _ in range(500):
                    r = await ctx.free_spin()
                    rounds.append(r)
                    if is_terminal(r):
                        break
            except Exception:  # noqa: BLE001 -- client may have played it internally; video proof covers it
                pass
        await ctx.settle()
        marks = {"trigger": trigger.seq}
        if first is not None:
            marks["first_fs"] = first.seq          # `at: [trigger, first_fs]` -> just the watchable part
        # What the player actually had when the bonus resumed. Free spins PAY IN, so this is the
        # balance after the trigger and the first free spin -- the honest number to judge "broke" on,
        # and the one the case asserts against instead of trusting the scenario's name.
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(), marks=marks,
                              facts={"stake": stake,
                                     # WHERE "BROKE" IS MEASURED, and why it is not the reload.
                                     # Free spins PAY IN, so the balance climbs the moment the bonus
                                     # starts winning -- judging at the reload would fail a perfectly
                                     # good run just because its first free spin paid. The precondition
                                     # this case rests on is that the player spent their LAST STAKE to
                                     # enter the bonus, so it is judged on the triggering spin, after
                                     # its stake was debited and any payout credited.
                                     "balance_after_trigger": trigger.balance,
                                     # True by construction -- the hunt discards an attempt whose
                                     # trigger left a stake or more. Asserted as a regression guard,
                                     # the same way `first_fs_capped == False` is.
                                     "broke_at_trigger": (trigger.balance is not None
                                                          and trigger.balance < stake),
                                     # what they held once the bonus resumed; informational, since
                                     # free-spin winnings legitimately raise it
                                     "balance_at_reload": first.balance,
                                     "triggers_paid_out": trigger_paid,
                                     "funded_attempts": attempts,
                                     "attempts_no_trigger": no_trigger,
                                     "fs_rounds_resumed": len(rounds) - 1 - len(observed),
                                     # free spins played (and dwelt on) BEFORE the reload
                                     "observed_before_reload": len(observed),
                                     # triggers skipped because the max-win cap truncated them to base
                                     "triggers_capped": triggers_capped,
                                     # False by construction: the hunt only stops on an attempt whose
                                     # first free spin did NOT cap. `attempts_discarded` counts the
                                     # ones thrown away to get here.
                                     "first_fs_capped": False,
                                     "attempts_discarded": first_fs_capped,
                                     # the bonus really did keep playing after the wallet was spent
                                     "played_after_zero": len(rounds) - 1 - len(observed) > 0})


class ReelsGlow(Scenario):    # Mah_041 / 042
    name = "reels-glow"
    description = ("Reels Glow: spin and observe whether the Reels-Glow anticipation fires per the rule "
                   "(glows when the FreeSpin! symbol lands across the qualifying reels; NOT when the last "
                   "bonus is on the final reel). The forced BO paytable (run-level `paytable:`) makes the "
                   "FreeSpin! symbol land in the chosen reels; that usually TRIGGERS free spins, so this "
                   "captures the base spin (the glow) then drains any bonus so settle can't hang. "
                   "Video-graded per case (at: spin).")

    def __init__(self, spins: int = 1):
        self.spins = spins

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        last = None
        for _ in range(self.spins):
            last = await ctx.base_spin()                       # the glow plays during THIS base spin
            if is_freespin_trigger(last):
                await play_out_freespins(ctx, last)            # forced FreeSpin! usually triggers FS -> drain it
            else:
                await ctx.settle()
        # What LANDED, so a case can prove its forced paytable applied before grading a glow it
        # can only see on video. `None` on a profile that names no glow symbol or cannot read the
        # grid -- which reads as "cannot verify" rather than silently as zero.
        raw = (last.raw or {}) if last else {}
        premium = getattr(GAME, "SYM_GLOW_PREMIUM", None)
        scatter_reels = GAME.reels_with(raw, GAME.SYM_SCATTER) if hasattr(GAME, "reels_with") else None
        premium_reels = (GAME.reels_with(raw, premium)
                         if (premium is not None and hasattr(GAME, "reels_with")) else None)
        return ScenarioResult(self.name, True, last, [last] if last else [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"spin": last.seq} if last else {},
                              facts={"bonus_reels_hit": (len(scatter_reels)
                                                         if scatter_reels is not None else None),
                                     "bonus_reels": scatter_reels,
                                     "p1_reels_hit": (len(premium_reels)
                                                      if premium_reels is not None else None),
                                     "p1_reels": premium_reels,
                                     # 1-BASED reel numbers, as the design docs say them
                                     "p1_on_reel2": (2 in premium_reels
                                                     if premium_reels is not None else None),
                                     "p1_on_reel3": (3 in premium_reels
                                                     if premium_reels is not None else None)})


# ---------------------------------------------------------------------------
# Feature-toggle POPUPS (Mah_105-108). The subject of these cases is the on-screen
# notification, so the popup itself is a VIDEO claim; every toggle is MARKED so the
# timestamp-mode ACTION TIMELINE pins exactly when it happened. Where the bridge exposes the
# feature's own state (speed mode, isAutoPlay) it is captured as a FACT, so "the feature really
# toggled" stays an EXACT check and only "the notification appeared" is left to the AI.
# ---------------------------------------------------------------------------
_SPEED_JS = ("() => { const b = window.__gameTestBridge;"
             " return b && b.getSpeedMode ? b.getSpeedMode() : 'UNKNOWN'; }")
_AUTO_JS = ("() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
            " return { on: !!s.isAutoPlay, left: s.autoSpinsRemaining }; }")


def _popup_labels(events: list) -> list:
    """Ordered labels of the `popup_shown` events in a bridge capture window. Empty when the
    game renders its feature notifications as something the bridge does not track as a popup —
    which is why no case asserts on this; it is reported as a diagnostic only."""
    out = []
    for e in events or []:
        if (e or {}).get("type") == "popup_shown":
            d = e.get("data") or {}
            out.append(str(d.get("label") or d.get("name") or d.get("popup") or "?"))
    return out


class ToggleTurbo(Scenario):    # Mah_105
    name = "toggle-turbo"
    description = ("Toggle Turbo ON and then back OFF from the main screen, dwelling after each click so "
                   "the 'Turbo Enabled' / 'Turbo Disabled' notification sits in frame long enough to be "
                   "read. Places no bet. Exposes facts.speed_after_on / speed_after_off — the bridge's "
                   "MODEL-truth speed mode, so the toggle itself is an exact check — plus the popup labels "
                   "the bridge saw. Unreached when the Turbo button never becomes clickable.")

    def __init__(self, dwell_ms: int = 2000):
        self.dwell_ms = dwell_ms      # hold after each toggle so the notification is recorded

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        await ctx.start_capture("popups")
        before = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        ctx.mark("TURBO ON: click the Turbo button")
        on = await ctx.click_button(GAME.button("turbo"), optional=True, timeout_ms=15000, dwell_ms=self.dwell_ms)
        after_on = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        ctx.mark("TURBO OFF: click the Turbo button a second time")
        off = await ctx.click_button(GAME.button("turbo"), optional=True, timeout_ms=15000,
                                     dwell_ms=self.dwell_ms) if on else False
        after_off = await ctx.ui_read(_SPEED_JS, "UNKNOWN")
        labels = _popup_labels(await ctx.end_capture("popups"))
        if not on:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), reason="turbo button never became clickable")
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                              facts={"speed_before": before, "speed_after_on": after_on,
                                     "speed_after_off": after_off, "toggled_off": off,
                                     # BOOLEANS, because a `stream:` assert interpolates the fact's
                                     # VALUE straight into a Python expression: comparing the raw
                                     # modes gives `NORMAL != TURBO`, two bare names, and the
                                     # evaluator dies with NameError rather than answering. Compute
                                     # the comparison here and assert the answer. (Same reason
                                     # `won_by_feature` exists alongside `won_via`.)
                                     "speed_changed_on": after_on != before,
                                     "speed_restored_off": after_off == before,
                                     # tells a failed toggle apart from a build with no getter at
                                     # all -- without it both look like "the speed did not change"
                                     "has_speed_getter": before != "UNKNOWN" and after_on != "UNKNOWN",
                                     "popup_events": ",".join(labels) or "none"})


class ToggleAuto(Scenario):    # Mah_107
    name = "toggle-auto"
    description = ("Start Auto Play (auto -> auto:count -> auto:confirm), let ONE auto spin play so it is "
                   "genuinely running, then STOP it from the main screen — dwelling after the start and "
                   "the stop so the 'Auto Play Enabled' / 'Auto Play Disabled' notification is recorded. "
                   "Exposes facts.auto_started / auto_stopped / spins_played (from the bridge's isAutoPlay "
                   "plus the collected responses), so the state change is an exact check and only the "
                   "notification is left to the AI. Unreached when auto never starts.")

    def __init__(self, count_index: int = 0, spins_before_stop: int = 1, dwell_ms: int = 2000):
        self.count_index = count_index              # auto:count:0 == 10 spins
        self.spins_before_stop = spins_before_stop
        self.dwell_ms = dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        await ctx.start_capture("popups")
        ctx.mark("AUTO PLAY ON: confirm the autoplay popup")
        opened = await ctx.click_button(GAME.button("auto"), optional=True, timeout_ms=15000,
                                        dwell_ms=ctx.step_dwell_ms)
        confirmed = False
        if opened:
            await ctx.click_button(GAME.button("auto_count", i=self.count_index), optional=True,
                                   timeout_ms=10000, dwell_ms=ctx.step_dwell_ms)
            confirmed = await ctx.click_button(GAME.button("auto_confirm"), optional=True, timeout_ms=10000,
                                               dwell_ms=self.dwell_ms)
        st_on = await ctx.ui_read(_AUTO_JS, {}) or {}
        rounds: list = []
        if confirmed:
            for _ in range(self.spins_before_stop):
                try:
                    await _auto_next(ctx, rounds)      # a real auto spin (client fires it; we observe)
                except Exception:  # noqa: BLE001
                    break
        ctx.mark("AUTO PLAY OFF: stop Auto Play from the main screen")
        stop_click = await ctx.click_button(auto_stop_button(), optional=True, timeout_ms=20000,
                                            dwell_ms=self.dwell_ms) if confirmed else False
        # POLL for up to 5 s, do not sample once. The client turns auto off when it has finished
        # the spin that was running at the stop, which can outlast the 2 s dwell -- a single read
        # then reported "still on" for a stop that worked (same rule as `confirm_stopped`).
        st_off: dict = {}
        for _ in range(20):
            st_off = await ctx.ui_read(_AUTO_JS, {}) or {}
            if not st_off.get("on"):
                break
            await asyncio.sleep(0.25)
        idle = False
        try:
            await wait_spin_clickable(ctx.page, ctx.spin_timeout_ms)
            idle = True
        except Exception:  # noqa: BLE001
            pass
        labels = _popup_labels(await ctx.end_capture("popups"))
        started = bool(st_on.get("on")) or bool(rounds)
        if not started:
            return ScenarioResult(self.name, False, None, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  reason="auto play never started (auto/auto:confirm not clickable)")
        return ScenarioResult(self.name, True, rounds[-1] if rounds else None, rounds,
                              ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(),
                              facts={"auto_started": started, "auto_stopped": not bool(st_off.get("on")),
                                     "stop_clicked": stop_click, "spins_played": len(rounds),
                                     "returned_to_idle": idle,
                                     "popup_events": ",".join(labels) or "none"})


class StackedPopups(Scenario):    # Mah_108
    name = "stacked-popups"
    description = ("Fire the main-screen feature toggles BACK-TO-BACK with no pause — each present "
                   "feature ON then OFF, repeated `cycles` times — so several notifications are requested "
                   "within a second or two of each other. Places NO bet and never starts Auto Play: the "
                   "case is about the notifications, and a spin would only add animation between them. "
                   "Which toggles are used is PROBED from the build (see _CANDIDATES): a feature this "
                   "build does not have is skipped instantly, because waiting on an absent button would "
                   "insert multi-second gaps into the very burst being tested. Every tap is MARKED, so the "
                   "ACTION TIMELINE — not a hard-coded prompt — tells the AI what was toggled and in what "
                   "order. A background poll samples the visible-popup list throughout; afterwards the game "
                   "is left IDLE so the notifications can auto-close. Every feature gets an EVEN number of "
                   "taps, so the burst restores itself; the screen is cleared at the end. Exposes "
                   "facts.features_used / toggles_landed / popup_order / max_concurrent_popups / "
                   "popup_samples / popups_cleared_after_idle; overlap and ORDER are themselves visual "
                   "claims, graded from the clip. Unreached when fewer than 3 toggles land (nothing "
                   "stacked, so there is nothing to judge).")

    # Main-screen toggles that raise their own enable/disable notification. PROBED, not assumed:
    # Mystic has no Ante Bet, but a build that gains one is picked up with no code change.
    @property
    def _CANDIDATES(self):
        return [n for n in (GAME.button(r) for r in GAME.FEATURE_TOGGLES) if n]

    def __init__(self, idle_s: float = 6.0, click_timeout_ms: int = 4000, min_toggles: int = 3,
                 cycles: int = 2):
        self.idle_s = idle_s                      # quiet tail: the notifications should auto-close here
        self.click_timeout_ms = click_timeout_ms  # SHORT: a burst must not stall on one dead button
        self.min_toggles = min_toggles
        self.cycles = cycles                      # ON/OFF rounds -- more notifications in one burst,
                                                  # and an even tap count per feature (self-restoring)

    async def _tap(self, ctx: DriveContext, name: str, timeout_ms: int | None = None) -> int:
        """One burst click: NO dwell on purpose -- these taps must land back-to-back, which is the
        situation the case is about. Every other multi-step interaction uses ctx.step_dwell_ms so the
        montage catches each step; here a dwell would destroy the thing being tested."""
        ctx.mark(f"TOGGLE: {name}")
        return 1 if await ctx.click_button(name, optional=True,
                                           timeout_ms=timeout_ms or self.click_timeout_ms,
                                           dwell_ms=0) else 0

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        # PROBE first (instant, no waiting): burst only on features this build actually has.
        present = [n for n in self._CANDIDATES if await ctx.has_button(n)]
        await ctx.start_capture("popups")
        ctx.start_popup_watch()
        landed = 0
        ctx.mark(f"BURST: toggle {', '.join(present) or 'no'} feature(s) ON/OFF x{self.cycles}, "
                 f"no pause between clicks")
        for _ in range(self.cycles):
            for name in present + present:        # first pass turns each ON, second turns each back OFF
                landed += await self._tap(ctx, name)
        ctx.mark("IDLE: no further input")
        await ctx.dwell(int(self.idle_s * 1000))
        watch = await ctx.stop_popup_watch()
        left = await ctx.popups()                              # read BEFORE cleaning up
        # A still of the settled screen. Asking "is this frame free of notifications?" of ONE
        # full-resolution image is a far easier question than "did it close during the tail?" of a
        # video, which an AI has answered wrongly on a clip that plainly showed it closing.
        await ctx.screenshot("after-idle")
        labels = _popup_labels(await ctx.end_capture("popups"))
        # every feature got an EVEN number of taps, so the burst restores itself -- but a click that
        # silently failed would leave Turbo on, so check the one feature whose state IS readable.
        if "turbo" in present and (await ctx.ui_read(_SPEED_JS, "UNKNOWN")) == "TURBO":
            await ctx.click_button(GAME.button("turbo"), optional=True, timeout_ms=10000, dwell_ms=0)
        await ctx.dismiss_popups()                             # clean screen for the next run
        # ⚠ On Mystic these notifications are NOT reported in `visiblePopups` — probed live
        # 2026-09-02: toggling Turbo raises a visible toast while the snapshot stays `[]`. So the
        # popup watch sees nothing, and "no popups are left" is TRUE ON EVERY BUILD, including one
        # where a notification really is stuck. Reporting that as a pass is the vacuous-assert trap.
        # When the watch never saw a popup, say UNKNOWN (None) instead of True and let the case
        # report unreached — the screen claims then rest on the image/video asserts, which can
        # actually see a toast.
        bridge_sees_popups = watch["max_concurrent"] > 0 or bool(watch["order"]) or bool(labels)
        facts = {"features_used": ",".join(present) or "none", "toggles_landed": landed,
                 "cycles": self.cycles, "popup_order": ",".join(watch["order"]) or "none",
                 "max_concurrent_popups": watch["max_concurrent"], "popup_samples": watch["samples"],
                 "notifications_visible_to_bridge": bridge_sees_popups,
                 "popups_cleared_after_idle": (not left) if bridge_sees_popups else None,
                 "popups_left": ",".join(left) or "none",
                 "popup_events": ",".join(labels) or "none"}
        if landed < self.min_toggles:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts,
                                  reason=f"only {landed} feature toggles landed "
                                         f"(features present: {facts['features_used']}) -- nothing stacked")
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts, shots=ctx.shots)


# ---------------------------------------------------------------------------
# Multi-language (Mah_052-056). The language is baked into the LAUNCH URL, so every one of these is
# driven by re-launching, never by an in-game switch: the runner's `language:` on a run picks the
# language the game LOADS in, and ctx.set_language() re-launches mid-scenario. All three tour the
# same text-bearing surfaces (ctx.tour_ui) so one clip covers every screen the case names, and all
# the verdicts are necessarily AI/video ones -- "the text is in Thai", "nothing overflows its frame"
# and "no glyph is clipped" are perception claims with no server or bridge equivalent.
# ---------------------------------------------------------------------------
def select_languages(spec=None, seed=None, pool=None, include=None) -> tuple:
    """Resolve a QA-written language selection to an ordered list of codes + a human description.

    Testing all 16 languages across 6 surfaces is 96 AI calls and 16 re-launches, so a suite must be
    able to say how much it wants to pay. Accepted `spec` forms:

        None            -> [] (caller falls back to the single loaded language)
        "all"           -> every supported language
        "random"        -> one at random          | "random:4" -> four at random
        "th"            -> that one language
        ["th","my","ja"]-> exactly these, in this order

    RANDOM IS SEEDED, always. A sample that cannot be replayed is not a test: the run that fails must
    be the run you can re-run. With no `seed` one is DRAWN and returned in the description (and put in
    facts.seed), so a failure can be pinned by copying it back into the suite. `include` codes are
    always present in a random sample — that is how a sweep keeps a known-good baseline (en) and the
    stress language (my) in every draw.

    Raises ValueError on an unknown code, so a typo fails at build time and not as a bad token."""
    codes = list(pool) if pool else [c for c, _, _ in SUPPORTED_LANGUAGES]
    keep = [c for c in (include or []) if c]
    for c in keep:
        if c not in LANGUAGE_NAME:
            raise ValueError(f"unknown language in include: '{c}'")

    if spec in (None, ""):
        return [], "the language the run loaded in"
    if isinstance(spec, (list, tuple)):
        picked = [str(c).strip() for c in spec]
        bad = [c for c in picked if c not in LANGUAGE_NAME]
        if bad:
            raise ValueError(f"unknown language(s): {', '.join(bad)} -- supported: "
                             f"{', '.join(sorted(LANGUAGE_NAME))}")
        return picked, f"the {len(picked)} listed languages"
    s = str(spec).strip().lower()
    if s == "all":
        return codes, f"all {len(codes)} supported languages"
    if s.startswith("random"):
        n = 1
        if ":" in s:
            try:
                n = int(s.split(":", 1)[1])
            except ValueError:
                raise ValueError(f"bad random count in '{spec}' -- use e.g. 'random:3'")
        if seed is None:
            seed = random.randrange(1_000_000)          # drawn, then REPORTED so it can be pinned
        rest = [c for c in codes if c not in keep]
        n = max(0, min(n, len(codes)))
        picked = keep[:n] + random.Random(seed).sample(rest, max(0, n - len(keep)))
        picked = [c for c in codes if c in set(picked)]  # stable order, not draw order
        return picked, f"{len(picked)} random of {len(codes)} (seed={seed})"
    if s not in LANGUAGE_NAME:
        raise ValueError(f"unknown language '{spec}' -- supported: {', '.join(sorted(LANGUAGE_NAME))}")
    return [s], f"the single language {s}"


class LanguageTour(Scenario):    # Mah_052 / 054 / 055
    name = "language-tour"
    description = ("Walk every text-bearing surface of the game in whatever language the run LOADED in "
                   "(set `language:` on the run): the base screen, the bet list, Settings, the Info / "
                   "Tutorial guide, the Auto Play panel and History, pausing on each. Places no bet. One "
                   "clip therefore holds every screen a language check has to look at — the translation "
                   "(052), the auto text scaling (054) and clipped/missing glyphs (055) — with each "
                   "surface MARKED so the ACTION TIMELINE names what is on screen when. Exposes "
                   "facts.language / language_name / surfaces / surface_count. Unreached if no surface "
                   "beyond the base screen opens.")

    def __init__(self, dwell_ms: int = 2000, languages=None, seed=None, include=None, surfaces=None):
        self.dwell_ms = dwell_ms
        self.languages = languages     # None -> just the language the run loaded in (the cheap path)
        self.seed = seed
        self.include = include
        self.surfaces = surfaces       # None -> all six; a list trims the tour (the other cost lever)
        # Resolve at CONSTRUCTION so a bad code / bad spec fails the run immediately, before a browser
        # is driven or a token minted. `select_languages` raises ValueError; the runner reports it.
        self.codes, self.selection = select_languages(languages, seed, include=include)
        if surfaces:
            known = {label for label, _, _ in game_profile.get("mystic").UI_SURFACES}
            bad = [s for s in surfaces if s not in known]
            if bad:
                raise ValueError(f"unknown surface(s): {', '.join(bad)} -- known: {', '.join(sorted(known))}")

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        codes = self.codes or [ctx.language or "en"]
        # every shot is labelled "<code> <name> - <surface>", so ONE image assert can use
        # {shot.label} to tell the model which language AND which screen it is looking at.
        toured, failed, per_lang, switches = [], [], {}, 0
        prev = None                  # the language we switched FROM (None for the first tour)
        for code in codes:
            if code != ctx.language:
                prev_lang = ctx.language
                try:
                    await ctx.set_language(code)
                except Exception as e:  # noqa: BLE001 — one dead language must not lose the others
                    failed.append(code)
                    print(f"    ! language '{code}' failed to launch: {e}")
                    continue
                if prev_lang:                       # a real mid-session switch, not the first load
                    prev = prev_lang
                    switches += 1
            name = LANGUAGE_NAME.get(code, code)
            ctx.mark(f"LANGUAGE {code}: {name}"
                     + (f" (switched from {LANGUAGE_NAME.get(prev, prev)})" if prev else ""))
            # The label carries the language, the screen AND what we switched from, because that is
            # all one image assert gets to work with: {shot.label} is the only per-shot text in the
            # prompt. Naming the previous language is what lets the stale-string question be
            # "is any THAI still here" instead of the much weaker "is any other language here".
            tag = f"{code} {name}" + (f" (switched from {prev} {LANGUAGE_NAME.get(prev, prev)})" if prev else "")
            seen = await ctx.tour_ui(self.dwell_ms, prefix=f"{tag} - ", surfaces=self.surfaces)
            per_lang[code] = len(seen)
            if seen:
                toured.append(code)
        surfaces_each = min(per_lang.values()) if per_lang else 0
        facts = {"language": codes[0], "language_name": LANGUAGE_NAME.get(codes[0], codes[0]),
                 "languages": ",".join(toured) or "none", "language_count": len(toured),
                 "selection": self.selection, "requested": len(codes),
                 "switches": switches,                    # mid-session language changes performed
                 "supported_count": len(SUPPORTED_LANGUAGES),   # for "did we cover them ALL?"
                 "stress_language": STRESS_LANGUAGE,
                 "failed": ",".join(failed) or "none",
                 "surfaces": ",".join(f"{k}:{v}" for k, v in per_lang.items()) or "none",
                 "surface_count": surfaces_each, "shot_count": len(ctx.shots)}
        if not toured or surfaces_each < 2:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, shots=ctx.shots,
                                  reason=(f"no language toured (failed: {facts['failed']})" if not toured
                                          else f"only {surfaces_each} surface(s) opened per language "
                                               f"({facts['surfaces']}) -- not enough on screen to judge "
                                               f"a translation"))
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts, shots=ctx.shots)


class LanguageSwitch(Scenario):    # Mah_053
    name = "language-switch"
    description = ("Change the language while the game is open and re-enter it: tour the UI in the FROM "
                   "language, then re-launch in the TO language and tour the SAME surfaces again. BOTH "
                   "ends are QA-settable per run — `params: {from: <code>, to: <code>}` — because which "
                   "pair leaks a stale string is exactly what a tester wants to vary; `from` defaults to "
                   "the run's `language:` (or whatever is loaded), `to` to Burmese. Shots are labelled "
                   "'BEFORE <surface>' / 'AFTER <surface>' so an image assert can grade either pass. The "
                   "failure this case is really about is a leftover untranslated string, visible only by "
                   "comparing the passes. Exposes facts.from_language / to_language / from_name / to_name "
                   "/ surfaces_before / surfaces_after / switched. Unreached if a re-launch fails.")

    def __init__(self, to: str = STRESS_LANGUAGE, dwell_ms: int = 2000, **kw):
        # `from` is a Python keyword, so it can only arrive as **kw from the YAML `params:` block.
        self.from_ = kw.pop("from", None) or kw.pop("from_", None)
        if kw:
            raise TypeError(f"unexpected params: {', '.join(sorted(kw))}")
        for code in (self.from_, to):
            if code and code not in LANGUAGE_NAME:
                raise TypeError(f"unknown language '{code}' -- supported: "
                                f"{', '.join(sorted(LANGUAGE_NAME))}")
        if self.from_ and self.from_ == to:
            raise TypeError(f"from and to are both '{to}' -- a switch test needs two languages")
        self.to = to
        self.dwell_ms = dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        # an explicit `from` wins over whatever the page happens to be loaded in, so the run does not
        # silently depend on the language a PREVIOUS run left behind (it is sticky across runs).
        if self.from_ and self.from_ != ctx.language:
            try:
                await ctx.set_language(self.from_)
            except Exception as e:  # noqa: BLE001
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()),
                                      facts={"from_language": self.from_, "to_language": self.to},
                                      reason=f"could not launch the FROM language '{self.from_}': {e}")
        first = ctx.language or "en"
        ctx.mark(f"BEFORE: the game is loaded in {LANGUAGE_NAME.get(first, first)} ({first})")
        before = await ctx.tour_ui(self.dwell_ms, prefix="BEFORE ")
        try:
            await ctx.set_language(self.to)
        except Exception as e:  # noqa: BLE001
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  facts={"from_language": first, "to_language": self.to},
                                  shots=ctx.shots,
                                  reason=f"could not re-launch in '{self.to}': {e}")
        ctx.mark(f"AFTER: re-entered in {LANGUAGE_NAME.get(self.to, self.to)} ({self.to})")
        after = await ctx.tour_ui(self.dwell_ms, prefix="AFTER ")
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events, (start, ctx.now_s()),
                              shots=ctx.shots,
                              facts={"from_language": first, "to_language": self.to,
                                     "from_name": LANGUAGE_NAME.get(first, first),
                                     "to_name": LANGUAGE_NAME.get(self.to, self.to),
                                     "surfaces_before": ",".join(before) or "none",
                                     "surfaces_after": ",".join(after) or "none",
                                     "switched": True})


class LanguageSweep(Scenario):    # Mah_056
    name = "language-sweep"
    description = ("Re-launch the game in EVERY supported language in turn and pause on the base screen "
                   "in each, so one clip carries the whole set for a translation sweep. Each language is "
                   "MARKED with its code and name, so the ACTION TIMELINE tells the AI exactly which "
                   "language each stretch of the clip is supposed to be — without that it would have to "
                   "guess, and a wrong-language screen is precisely what this case looks for. This is the "
                   "longest scenario in the suite (one Playsite re-launch per language). Exposes "
                   "facts.languages / language_count / failed. Unreached if fewer than half re-launch.")

    def __init__(self, languages=None, dwell_ms: int = 2000):
        self.languages = [c for c, _, _ in SUPPORTED_LANGUAGES] if languages is None else list(languages)
        self.dwell_ms = dwell_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        done, failed = [], []
        for code in self.languages:
            try:
                if code != ctx.language:               # already loaded in it -> don't pay for a re-launch
                    await ctx.set_language(code)
            except Exception as e:  # noqa: BLE001
                failed.append(code)
                print(f"    ! language '{code}' failed to launch: {e}")
                continue
            ctx.mark(f"LANGUAGE {code}: {LANGUAGE_NAME.get(code, code)}")
            await ctx.dwell(self.dwell_ms)             # hold the base screen in this language
            await ctx.screenshot(f"{code} {LANGUAGE_NAME.get(code, code)}")   # one still per language
            done.append(code)
        facts = {"languages": ",".join(done) or "none", "language_count": len(done),
                 "failed": ",".join(failed) or "none", "requested": len(self.languages)}
        if len(done) * 2 < len(self.languages):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, shots=ctx.shots,
                                  reason=f"only {len(done)}/{len(self.languages)} languages launched "
                                         f"(failed: {facts['failed']})")
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts, shots=ctx.shots)


class LastBetBeforeRounds(Scenario):    # Mah_087 (+ the in-bonus half of Mah_085)
    name = "last-bet-before-rounds"
    description = ("Trigger free spins and record the WHOLE bonus, because Last Bet shows ITSELF here: "
                   "per the spec it flashes for ~0.5s before each free-spin round, carrying the bet "
                   "type / stake / bet amount / win of the round that just finished. That auto-display "
                   "needs no button, which is why this case is automatable while the rest of the Last "
                   "Bet module is not (the bridge exposes no `lastBet` control to OPEN the panel). "
                   "Every free-spin round is MARKED so the ACTION TIMELINE lines each 0.5s window up "
                   "with the response that preceded it; grade with high_fps, since 0.5s is two frames "
                   "at the default sampling. Exposes facts.fs_rounds / fs_stake_zero / trigger_stake / "
                   "last_round_payout. Unreached if no bonus is reached within the spin budget.")

    def __init__(self, cap: int = 60):
        self.cap = cap        # base spins to try before giving up on a natural trigger

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        trigger, spins = None, 0
        for _ in range(self.cap):
            b = await ctx.base_spin()
            spins += 1
            if is_freespin_trigger(b):
                trigger = b
                break
            await ctx.settle()
        if trigger is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"base_spins": spins},
                                  reason=f"no free-spin trigger in {spins} base spins")
        ctx.mark("BONUS TRIGGERED")
        rounds = [trigger]
        while True:                       # drain the bonus, marking every round boundary
            nxt = await ctx.free_spin()
            rounds.append(nxt)
            ctx.mark(f"FREE SPIN ROUND {len(rounds) - 1} finished "
                     f"(stake {nxt.stake}, win {nxt.payout})")
            if not (nxt.free_spins > 0 and nxt.game_state != BASE_STATE):
                break
        await ctx.settle()
        fs = [r for r in rounds[1:]]
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"trigger": trigger.seq},
                              facts={"base_spins": spins, "fs_rounds": len(fs),
                                     "trigger_stake": trigger.stake,
                                     "fs_stake_zero": all(r.stake == 0 for r in fs),
                                     "last_round_payout": rounds[-1].payout,
                                     "bonus_total": round(sum(r.payout for r in fs), 4)})


class LastBetRestore(Scenario):    # CR_092..CR_095 / DV-67..DV-70
    name = "last-bet-restore"
    description = ("LAST BET IS A RESUME BEHAVIOUR, NOT A PANEL. Play one base round, RELOAD the "
                   "game as a player closing and reopening it, and check that the session comes "
                   "back showing the round they last played: the same stake, the same win, and the "
                   "bet control sitting at that stake rather than at the configured default.\n"
                   "Drives `reload_and_start()`, then reads `getGameInfo().lastBet` through the "
                   "profile's parser and compares it to the round actually played.\n"
                   "`result_hold_s` (3s) holds the SETTLED ROUND in frame before the reload: "
                   "`settle()` only waits for the game to be spinnable, so without it the clip cuts "
                   "from the win straight to the reload and the montage may never sample the result "
                   "the reopened screen is compared against.\n"
                   "GRADE IT WITH `video: at: [played, restored]`, not with a still. The clip then "
                   "runs from the last round's response -- so its WIN ANIMATION is in frame, which "
                   "a screenshot cannot hold -- through the reload and on to the restored screen "
                   "sitting ready. That turns the AI's job into a COMPARISON of two phases of one "
                   "clip (`is the bet / win / winning line the same after the reload as before it`) "
                   "rather than reading absolute values off a still, which is the reading the "
                   "bridge already owns exactly. `restored` is a MOMENT mark, so the span reaches "
                   "the end of the scenario; a bare `at: played` stops at the next response and "
                   "cuts the reload off. A `restored` screenshot is still taken for cases that want "
                   "a full-resolution look at the re-entry alone.\n"
                   "⚠ TWO TRAPS, both load-bearing, both from the bridge docs:\n"
                   "  * `getGameInfo()` must be the PRE-START CAPTURE. Right after the Start click "
                   "    `GameController.applyLastBet()` sets `gameInfo.lastBet = null`, so a LIVE "
                   "    read reports no last bet on a session that restored perfectly. "
                   "    `isGameInfoCaptured()` is the gate, published as facts.info_captured -- "
                   "    without it every comparison below is None == None and greens on nothing.\n"
                   "  * `lastBet` does NOT spell its fields like a BET response on every game "
                   "    (contract §5), which is why it is read through `Bet.parse` / `GAME.*` and "
                   "    never by raw key.\n"
                   "`outcome` picks which round to leave on record: 'win' | 'lose' | "
                   "'freespin_trigger' | 'any'. On 'win'/'lose'/'any' a bonus met while hunting is "
                   "DRAINED, so the round left on record is a plain base round; on "
                   "'freespin_trigger' the scenario deliberately leaves the terminal round of a "
                   "FINISHED bonus there instead. Either way the bonus is played out before the "
                   "reload, so `pending` is empty and the client RESTORES rather than resumes -- if "
                   "it ever resumes, this case is grading a screen it did not ask for, and "
                   "facts.resumed_rounds says so.\n"
                   "⚠ 'freespin_trigger' MEANS TRIGGERED FROM THE BASE GAME, NOT BOUGHT. The name is "
                   "explicit because a game with Buy Bonus has two ways into the same feature and "
                   "they do NOT cost the same: a trigger charges the base stake, a purchase charges "
                   "a price that is a multiple of it. A restore case that conflated them would "
                   "assert the wrong figure. The bought route is a separate outcome ('buy_bonus', "
                   "not implemented -- it needs the panel flow `buy-bonus` already drives).\n"
                   "⚠ ON 'freespin_trigger' THE STAKE IS READ OFF THE TRIGGER, not the terminal "
                   "round. Bonus rounds cost nothing, so their `stake` is 0 and `base_stake()` "
                   "falls back to it when `baseStake` is absent -- reading the terminal round would "
                   "publish 0 on a healthy game and fail every stake assert for the wrong reason. "
                   "The trigger is the round the player actually paid. `terminal_base_stake` "
                   "publishes the other figure so a captured BET response can settle which one "
                   "`lastBet` echoes.\n"
                   "`change_bet_after` is CR_095: after the round, pick a DIFFERENT bet amount in "
                   "the bet list (client-side, no spin), then reopen. A bet change sends nothing to "
                   "the server, so the record is still the round PLAYED and the game must come back "
                   "on that stake, not the one merely selected (facts.changed_bet_to vs "
                   "restored_bet_amount; bet_moved_after_round is the gate).\n"
                   "`swap_denomination` (Grandma TC-085): after the round, CLOSE the game, switch "
                   "the agent's bet levels in BO to the automation ladder (11/22/33, which does "
                   "not contain the bet just played), reopen once the game server serves it, and "
                   "check the game opens on the NEW list's DEFAULT bet (facts.new_default_bet / "
                   "bet_opens_at_new_default; played_bet_in_new_ladder must be False for the case "
                   "to mean anything). Unreached when BO is not configured or the new list never "
                   "arrives. Needs `teardown: reset-denomination`. A swap while a bonus is still "
                   "OWED makes the reload a RESUME -- that is `bonus-resume-denomination-swap`.\n"
                   "Exposes played_stake / played_base_stake / played_payout, restored_* for each, "
                   "stake_restored / payout_restored / bet_opens_at_last_stake, and the gates "
                   "info_captured / last_bet_present / resumed_rounds. On 'freespin_trigger' it "
                   "adds trigger_stake / trigger_base_stake / terminal_base_stake -- the trigger "
                   "names are `hold-spin-complete`'s and `last-bet-before-rounds`', deliberately: "
                   "one concept must not have two names across two scenarios.")

    def __init__(self, outcome: str = "any", max_spins: int = 60, dwell_s: float = 3,
                 change_bet_after: bool = False, result_hold_s: float = 2.0,
                 swap_denomination: bool = False, settle_s: float = 5.0,
                 propagation_timeout_s: float = 30.0, poll_s: float = 3.0):
        self.outcome = outcome          # which round to leave on record before reloading
        # Change the DENOMINATION LIST (the bet levels on offer) in BO while the game is closed,
        # to the automation ladder (11/22/33), which does not contain the bet just played. Grandma
        # TC-085: when the new list lacks the last round's bet, the game reopens on the new list's
        # DEFAULT bet. Needs `teardown: reset-denomination` — the ladder is shared state.
        self.swap_denomination = swap_denomination
        self.settle_s = settle_s                          # wait after the BO write (Rule 20)
        self.propagation_timeout_s = propagation_timeout_s  # reopen until the new list is served
        self.poll_s = poll_s
        self.max_spins = max_spins
        # The restored screen has to SIT IN FRAME long enough to be compared against the round
        # before the reload, so the scenario settles, marks, and then holds this long.
        self.dwell_s = dwell_s
        self.change_bet_after = change_bet_after
        # Seconds the SETTLED ROUND is held on screen before anything else happens. `settle()` only
        # waits until the game is spinnable again, which can be the moment the win presentation
        # finishes -- so without this the clip cuts from the win straight to the reload, and the AI
        # montage (one composite per second) may never sample the result it is asked to compare the
        # reopened screen against. This is the first phase of every prompt in the suite.
        self.result_hold_s = result_hold_s

    def _matches(self, b: "Bet") -> bool:
        if self.outcome == "win":
            return b.payout > 0
        if self.outcome == "lose":
            return b.payout == 0
        return True

    async def _pick_other_denomination(self, ctx: DriveContext) -> Optional[float]:
        """Move the bet to a denomination that is NOT the one just played, and return what it landed
        on — or `None` when this build would not move it.

        Non-fatal by design: `changed_bet_to` None reads as "this half was not exercised" (the case
        gates on `bet_moved_after_round`) rather than as a restore failure.

        ⚠ THREE WAYS THIS HANGS, all of them measured or documented, all handled here:

        * **The row is below the fold.** `bet:row:<i>` is indexed by POSITION and the list scrolls;
          Dragon's bridge doc says outright "may be below the fold — `scrollBetRowIntoView(i)`
          first". Without the scroll the row never becomes clickable.
        * **The row is the CURRENT one.** "The current row is non-interactable" — so a target chosen
          without knowing the current bet can be a row that can never be clicked. `getBetIndex()`
          may also answer `-1` (unknown), which an index-only choice reads as "not row 0".
          The target is therefore chosen by VALUE against the live bet amount, which is always
          readable.
        * **A click that waits the default budget.** `click_button` falls back to `spin_timeout_ms`
          (60 s) and RAISES when not optional. Two of those in a row is two minutes that looks like
          a hang. Every click here is `optional` with its own short budget.
        """
        info = await ctx.eval("""() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return { denoms: (s && s.denominations) || [],
                     betIndex: (b.getBetIndex ? b.getBetIndex() : -1),
                     betAmount: (b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)) }; }""")
        denoms = [float(d) for d in (info.get("denoms") or [])]
        if len(denoms) < 2:
            return None
        before = info.get("betAmount")
        before = float(before) if before is not None else None
        cur = info.get("betIndex")
        cur = cur if isinstance(cur, int) and 0 <= cur < len(denoms) else None
        # Pick by VALUE: the one thing that cannot pick the non-interactable current row.
        target = next((i for i, d in enumerate(denoms)
                       if i != cur and (before is None or abs(d - before) > 1e-9)), None)
        if target is None:
            return None
        picked = denoms[target]
        if await open_bet_list(ctx):
            print(f"  last-bet : bet list -- row {target} ({picked}), was {before}")
            ctx.mark("BET LIST: pick a different bet")
            await ctx.scroll_bet_row(target)               # the row may be below the fold
            await ctx.click_button(GAME.button("bet_row", i=target), optional=True, timeout_ms=15000)
            await ctx.click_button(GAME.button("bet_close"), optional=True, timeout_ms=8000)
        else:
            # No list on this build: the stake controls step the bet one denomination at a time.
            steps = target - (cur or 0)
            role = "stakeIncrease" if steps > 0 else "stakeDecrease"
            print(f"  last-bet : stepping {'up' if steps > 0 else 'down'} {abs(steps)}x to {picked}")
            ctx.mark(f"BET: step the stake {'up' if steps > 0 else 'down'}")
            for _ in range(abs(steps)):
                if not await ctx.click_button(role, optional=True, timeout_ms=10000,
                                              dwell_ms=ctx.step_dwell_ms):
                    break
        await ctx.settle()
        landed = await ctx.eval("""() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return (b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)); }""")
        if landed is None:
            return None
        landed = float(landed)
        # It has to have MOVED. Returning the unchanged bet would make `bet_moved_after_round` the
        # only thing standing between a silent no-op and a green.
        if before is not None and abs(landed - before) <= 1e-9:
            print(f"  last-bet : BET DID NOT MOVE (still {landed}) -- the change half is untested")
            ctx.mark("BET CHANGE attempted")
            return None
        return landed

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        rounds: list = []
        played = None
        trigger = None
        bonus_payout = None          # the WHOLE bonus's win -- see where it is set
        for _ in range(self.max_spins):
            b = await ctx.base_spin()
            rounds.append(b)
            if is_freespin_trigger(b):
                # Play it out either way -- a bonus still OWED at the reload makes the client
                # resume, which is a different behaviour from restoring a last bet.
                fs = await play_out_bonus(ctx, b)
                rounds.extend(fs[1:])
                if self.outcome == "freespin_trigger":
                    # The terminal round of the finished bonus is what the session comes back
                    # showing, so it is the round on record -- but NOT where the stake is read,
                    # and NOT where the WIN is read either.
                    #
                    # THE BONUS'S WIN IS NOT ON ITS TERMINAL ROUND. Every round of a bonus
                    # reports only its own payout, and the last one very often pays 0 -- the
                    # feature ends on whatever spin takes the counter to zero, winning or not.
                    # Measured on Joker's captured terminal free spin: `payout: 0` while the
                    # bonus paid 39.6 (`totalFreeSpinPayout` / `accumulatedWinAmount`). So
                    # `played_payout` below is 0 on a game behaving perfectly, and a case that
                    # asserted `played_payout > 0` went red for the wrong reason.
                    # Summing the rounds is the mechanism-free way to say it: the server's own
                    # total field is spelled differently per game (and does not exist on some),
                    # while every round's `payout` is already parsed here.
                    await ctx.settle()
                    trigger, played = b, rounds[-1]
                    # `fs[1:]`, NOT `fs`: `play_out_bonus` returns [trigger, ...free spins...],
                    # and the trigger is a PAID base round whose own line win is not part of
                    # what the bonus paid. Excluding it matches the server's own field --
                    # Joker spells it `totalFreeSpinPayout` / `accumulatedWinAmount`, and the
                    # captured terminal free spin reads 39.6 there for a bonus whose own
                    # terminal round paid 0.
                    bonus_payout = round(sum(r.payout for r in fs[1:]), 4)
                    break
                continue
            await ctx.settle()
            if self.outcome != "freespin_trigger" and self._matches(b):
                played = b
                break
        if played is None:
            return ScenarioResult(self.name, False, None, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"spins_played": len(rounds)},
                                  reason=f"no '{self.outcome}' round in {len(rounds)} spins")
        # THE STAKE COMES FROM THE ROUND THAT WAS PAID FOR. On a bonus that is the TRIGGER: its
        # rounds cost nothing, so a bonus round's `stake` is 0, and `base_stake()` falls back to
        # `stake` when `baseStake` is absent -- so reading it off the terminal round yields 0 on a
        # game behaving perfectly, and every stake assert goes red for the wrong reason. The trigger
        # is the round the player actually paid, and its base stake is the figure a restore shows.
        # ⚠ Whether a terminal bonus round carries its own `baseStake` is UNVERIFIED on Dragon VIP
        # (no BET response captured yet). Both figures are published so a case can say which it
        # means, and so capturing one response settles it without touching this code.
        # HOLD THE RESULT IN FRAME. The round is settled; give the clip a few seconds of it before
        # the bet change / reload, so phase 1 of the comparison has something to sample.
        # ⚠ MARK TEXT IS READ BY THE MODEL. In a timestamp-mode assert every mark is appended to
        # the prompt as the ACTION TIMELINE, so a mark that names the stake or the win, or says the
        # screen "must match", hands the AI the answer it is being asked to read. The marks here
        # name the PHASE only; the values go to the console for whoever is watching the run.
        if self.result_hold_s:
            print(f"  last-bet : round settled -- stake {played.stake}, win {played.payout}")
            ctx.mark("BEFORE: the round has finished; this is the game before it is closed")
            await asyncio.sleep(self.result_hold_s)
        stake_source = trigger if trigger is not None else played
        played_base = GAME.base_stake(stake_source.raw or {})
        terminal_base = GAME.base_stake(played.raw or {})
        changed_to = None
        if self.change_bet_after:
            changed_to = await self._pick_other_denomination(ctx)
            print(f"  last-bet : bet moved to {changed_to} after the round (played at {played_base})")
            ctx.mark("BET CHANGED after the round")
        print(f"  last-bet : reloading -- played stake {played.stake}, win {played.payout}")
        swap: dict = {}
        new_ladder: list = []
        ladder_ready = None
        if self.swap_denomination:
            # Close, change the bet levels on offer in BO, reopen only once the game server serves
            # the new list (evidence, not a timer -- see load_until_ladder), then Start. Any bonus
            # met while hunting was already played out above, so nothing is owed: a RESTORE.
            ctx.mark("CLOSED: the game is closed")
            await ctx.stop_client()
            swap = await ctx.set_automation_denomination(settle_s=self.settle_s)
            if not swap:
                return ScenarioResult(self.name, False, played, rounds, ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), round_windows=ctx.windows(),
                                      facts={"spins_played": len(rounds), "swap_applied": False},
                                      reason="BO is not configured, so the bet levels cannot be "
                                             "changed")
            new_ladder = [x for x in (_ladder_num(v) for v in (swap.get("bet_levels") or []))
                          if x is not None]
            print(f"  last-bet : bet levels now {new_ladder} (default {swap.get('default_bet')})")
            ctx.mark("BET LEVELS CHANGED while the game was closed")
            ladder_ready, seen, loads = await load_until_ladder(
                ctx, new_ladder, self.propagation_timeout_s, self.poll_s)
            await click_start(ctx.page)
            ctx.mark("REOPENED: the game is open again")
            resumed = []
            if not ladder_ready:
                return ScenarioResult(
                    self.name, False, played, rounds, ctx.timeline, ctx.events,
                    (start, ctx.now_s()), round_windows=ctx.windows(),
                    facts={"spins_played": len(rounds), "swap_applied": True,
                           "new_ladder": new_ladder, "client_ladder": seen,
                           "client_on_new_ladder": False, "ladder_loads": loads},
                    reason=(f"the game server never served the new bet levels {new_ladder} within "
                            f"{self.propagation_timeout_s}s of the BO change ({loads} load(s), "
                            f"still {seen}) -- a BO -> game-server propagation problem, not a "
                            f"game defect"))
        else:
            ctx.mark("CLOSE AND REOPEN the game")
            resumed = await ctx.reload_and_start()
        # THE PRE-START CAPTURE. `getGameInfo()` is the copy taken before Start precisely because
        # Start destroys `lastBet`; `isGameInfoCaptured()` says whether that copy exists at all.
        info = await ctx.eval("""() => {
            const b = window.__gameTestBridge;
            const gi = b.getGameInfo ? b.getGameInfo() : null;
            const r = (gi && gi.result) ? gi.result : gi;      // inner object or envelope
            const s = b.getSnapshot ? b.getSnapshot() : {};
            let src = null;
            try { src = b.diagnose ? (b.diagnose() || {}).gameInfoSource : null; } catch (e) {}
            return { lastBet: r ? (r.lastBet || null) : null,
                     pending: r ? (r.pending || null) : null,
                     captured: b.isGameInfoCaptured ? !!b.isGameInfoCaptured() : null,
                     source: src,
                     betAmount: s.betAmount }; }""")
        # THE CLIP IS A COMPARISON, so the restored screen has to SIT IN FRAME long enough to be
        # compared against the round before the reload -- not merely appear. Settle first (the
        # re-entry is still drawing when `start_game` resolves), THEN mark the moment it is ready,
        # THEN hold. Marking before the settle would timestamp a loading screen, and the AI would be
        # told to compare against pixels that are still animating.
        await ctx.settle()
        restored_at = ctx.now_s()
        ctx.mark("AFTER: the reopened game is ready")
        await asyncio.sleep(self.dwell_s)
        await ctx.screenshot("restored")
        raw = info.get("lastBet")
        # Read it through the PROFILE, never by raw key: `lastBet` does not always spell its fields
        # the way a BET response does (contract §5), and the profile's aliases are what reconcile
        # the two. Parsed with a seq of -1 so it can never be mistaken for a round of the stream.
        lb = Bet.parse(raw, -1) if isinstance(raw, dict) else None
        restored_base = GAME.base_stake(raw) if isinstance(raw, dict) else None
        bet_amount = info.get("betAmount")
        def _same(a, b) -> bool:
            return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6
        facts = {
            "spins_played": len(rounds),
            # the round that was left on record
            "played_stake": played.stake, "played_base_stake": played_base,
            # ⚠ THE ROUND'S OWN PAYOUT, which on `outcome: freespin_trigger` is the TERMINAL
            # bonus round's -- frequently 0, because a bonus ends on whatever spin empties the
            # counter. Assert `played_bonus_payout` instead on that outcome.
            "played_payout": played.payout,
            # The WHOLE bonus's win (trigger + every free spin), or None when the outcome was
            # not a bonus. This is the figure a player would call "what the bonus paid".
            "played_bonus_payout": bonus_payout,
            # On `outcome: freespin_trigger` these split: `played_base_stake` above is the TRIGGER's
            # (the round the player paid for), while this is the terminal bonus round's own figure
            # -- 0 on a game whose bonus rounds are free and carry no `baseStake`. Exposed, not
            # asserted: capturing one BET response says which one `lastBet` actually echoes.
            "terminal_base_stake": terminal_base,
            # `trigger_*` are `hold-spin-complete`'s and `last-bet-before-rounds`' names for the same
            # concept -- the round that AWARDED the bonus. Do not invent a third spelling.
            "trigger_stake": trigger.stake if trigger is not None else None,
            "trigger_base_stake": played_base if trigger is not None else None,
            # None unless `change_bet_after` asked for it. A case that grades the overwrite
            # question MUST gate on this being present AND different from played_base_stake:
            # moving the bet to the denomination already selected proves nothing.
            "changed_bet_to": changed_to,
            "bet_moved_after_round": changed_to is not None and not _same(changed_to, played_base),
            # The failure CR_095 is looking for, stated on its own so a case can assert it False: the
            # game reopened on the value SELECTED after the round instead of the one played.
            "bet_opens_at_changed_value": _same(bet_amount, changed_to),
            # the gates. Without these the comparisons below are None == None, which is a green.
            "info_captured": info.get("captured"),
            "game_info_source": info.get("source"),
            "last_bet_present": lb is not None,
            # a reload that RESUMED a bonus is not a restore -- this case would be grading the
            # wrong screen, and says so rather than passing quietly
            "resumed_rounds": len(resumed),
            # what came back
            "restored_stake": lb.stake if lb else None,
            "restored_base_stake": restored_base,
            "restored_payout": lb.payout if lb else None,
            "restored_bet_amount": bet_amount,
            "stake_restored": _same(restored_base, played_base) if lb else False,
            "payout_restored": _same(lb.payout if lb else None, played.payout),
            # ...and the same question against the BONUS total. Which of the two a restored
            # `lastBet` echoes is UNVERIFIED -- the terminal round's own payout, or the whole
            # feature's. Both are published so a bonus case can assert "it came back with a
            # win" without guessing, and so one live run settles it for good.
            "bonus_payout_restored": (_same(lb.payout if lb else None, bonus_payout)
                                     if bonus_payout is not None else None),
            # `applyLastBet` puts the bet control back on the last stake -- the visible half of the
            # same rule, and the one a player notices first
            "bet_opens_at_last_stake": _same(bet_amount, played_base),
            # `swap_denomination` only (None otherwise). The rule: when the NEW bet levels do not
            # contain the last round's bet, the game reopens on the new list's DEFAULT bet.
            "swap_applied": bool(swap) if self.swap_denomination else None,
            "new_ladder": new_ladder or None,
            "new_default_bet": swap.get("default_bet") if swap else None,
            "client_on_new_ladder": ladder_ready,
            # the premise: the round was played at a bet the new list does NOT offer
            "played_bet_in_new_ladder": (any(_same(played_base, x) for x in new_ladder)
                                         if new_ladder else None),
            "bet_opens_at_new_default": (_same(bet_amount, swap.get("default_bet"))
                                         if swap else None),
        }
        # THE WINDOW A CASE SHOULD ASK FOR IS `at: [played, restored]`.
        #   `played`   is a ROUND mark -- the clip opens on the last round's response, so its win
        #              presentation is in frame.
        #   `restored` is a MOMENT mark, which resolves from that moment to the END of the scenario
        #              (see `resolve_window`), so spanning to it yields
        #              played-round-start -> scenario-end: the round, the reload, and the hold above.
        # A single `at: played` would stop at the next response and cut the reload off entirely.
        return ScenarioResult(self.name, True, played, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"played": played.seq,
                                     "restored": {"t": round(restored_at, 3)}},
                              facts=facts, shots=ctx.shots)


class BonusResumeDenominationSwap(Scenario):    # CR_096 / DV-71 + DV-73
    name = "bonus-resume-denomination-swap"
    description = ("A bonus is TRIGGERED, the game is CLOSED while that bonus is still owed, the "
                   "agent's bet ladder is changed on BO, and the game is REOPENED. Two claims, one "
                   "run: (1) the RESUMED bonus keeps the stake it was triggered at -- the server "
                   "froze it in `gameInfo.pending` -- and (2) once it ends, the base game opens on "
                   "the NEW ladder's default bet, because the old stake does not exist there.\n"
                   "This is a RESUME, not a restore (`last-bet-restore`): the reload happens with a "
                   "bonus owed, so `pending` drives it. The swap goes to the automation ladder "
                   "(11, 22, 33), which shares no value with any real ladder -- that is what makes "
                   "'the old stake is not on the new ladder' true by construction, and "
                   "facts.old_bet_on_new_ladder is the gate that proves it.\n"
                   "Sequence, each step visible in `run`: base spins until a trigger -> "
                   "`stop_client()` -> `set_automation_denomination()` (BO write + a Rule 20 "
                   "settle wait) -> `open_game()` "
                   "(load + Start, no play-out) -> read `pending` -> play the resumed bonus with "
                   "`play_out_holdspin` / `play_out_freespins`, reading the bet readout on every "
                   "round through `on_round` (facts.resume_bets) -> "
                   "settle -> mark `restored` -> hold -> shoot `after_bonus`.\n"
                   "Marks: `trigger` (round), `resume_first` / `resume_last` (rounds), `restored` "
                   "(moment). Grade both claims on ONE clip with `at: [trigger, restored]` and a "
                   "phase-scoped prompt.\n"
                   "Force the trigger with a paytable fixture; unreached if none arrives in "
                   "`max_spins`, or when BO is not configured. REQUIRES "
                   "`teardown: reset-denomination`.\n"
                   "Exposes trigger_stake / trigger_base_stake, the swap (swap_applied, "
                   "new_ladder, new_default_bet, client_ladder, client_on_new_ladder, "
                   "old_bet_on_new_ladder), the resume (info_captured, is_pending_freespin, "
                   "pending_stake, pending_stake_is_trigger, resumed_rounds, resume_bets, "
                   "bet_during_bonus_is_old, bet_during_bonus_on_new_ladder) and the base game "
                   "after it (bet_after_bonus, bet_after_bonus_is_new_default, "
                   "bet_after_bonus_on_new_ladder).")

    def __init__(self, max_spins: int = 20, dwell_s: float = 3, resume_cap: int = 60,
                 settle_s: float = 5.0, propagation_timeout_s: float = 30.0, poll_s: float = 3.0):
        # after the first `settle_s`, reload (without Start) every `poll_s` until the launch
        # payload carries the new ladder, for up to `propagation_timeout_s`
        self.propagation_timeout_s = propagation_timeout_s
        self.poll_s = poll_s
        self.max_spins = max_spins     # base spins to find the trigger; a fixture makes it spin 1
        self.dwell_s = dwell_s         # hold on the base game after the bonus, so it is filmed
        self.resume_cap = resume_cap   # most resumed rounds to take before calling the bonus stuck
        # Rule 20: seconds to let the BO change reach the game server before the game is reopened.
        # `client_on_new_ladder` is what PROVES it arrived; this only gives it time to.
        self.settle_s = settle_s

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        if ctx._bo is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason="BO not configured -- this case changes the denomination on BO")
        rounds: list = []
        trigger = None
        for _ in range(self.max_spins):
            b = await ctx.base_spin()
            rounds.append(b)
            if is_freespin_trigger(b):
                trigger = b
                break
            await ctx.settle()
        if trigger is None:
            return ScenarioResult(self.name, False, None, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"base_spins": len(rounds)},
                                  reason=f"no bonus trigger in {len(rounds)} base spins")
        trigger_base = GAME.base_stake(trigger.raw or {})
        # NO settle, NO drain, and deliberately NO HOLD here: the bonus must still be OWED when the
        # game closes, or the reopen has nothing to resume and the case silently becomes a restore.
        # `last-bet-restore` holds the settled round in frame before ITS reload (`result_hold_s`) so
        # the AI can sample it -- do not copy that here. The client auto-plays the feature, so every
        # second this waits is a respin the server takes off `pending`, and enough of them turn the
        # resume into a restore. Phase 1 of this clip is short on purpose.
        # ⚠ Marks are appended to the prompt as the ACTION TIMELINE: name the MOMENT only, never
        # a stake, a ladder or what the screen "should" show. The values go to the console.
        print(f"  denom-swap: bonus triggered at stake {trigger_base} -- closing with it owed")
        ctx.mark("CLOSED: free spins were won and the game was closed at once, before that spin "
                 "was shown")
        # 1. Close the game while the bonus is still owed. The client auto-plays a bonus, so it has
        #    to be gone before BO is touched, or the BO call races those rounds.
        await ctx.stop_client()

        # 2. Change the denomination in BO while nothing is open, and give the change time to
        #    reach the game server (Rule 20 -- BO answers before the game server has the event).
        swap = await ctx.set_automation_denomination(settle_s=self.settle_s)
        print(f"  denom-swap: ladder now {swap.get('name')!r} {swap.get('bet_levels')} "
              f"(default {swap.get('default_bet')}) -- waited {self.settle_s}s")
        ctx.mark("BET LEVELS CHANGED while the game was closed")

        # 3. Reopen ONLY once the game server serves the new ladder — evidence, not a timer.
        #
        # ⚠ WHY NOT JUST WAIT. BO answering 200 means BO stored the change; the game server gets
        # it later, over Kafka, and how much later varies. Two runs with the SAME 5 s wait split:
        # one reopened on the new ladder (bet 11.00 after the bonus), the other on the OLD one
        # (0.10), and the second read exactly like a game that ignores the new denomination.
        # So: load WITHOUT Start — safe, the owed bonus only plays once Start is clicked — read
        # the ladder from the launch payload, and reload until it is the new one. A load that
        # never shows it within `propagation_timeout_s` is a BO -> game-server propagation
        # problem, not a game defect, and the run says so instead of failing the case.
        want = [x for x in (_ladder_num(v) for v in (swap.get("bet_levels") or [])) if x is not None]
        ladder_ready, seen, loads = await load_until_ladder(
            ctx, want, self.propagation_timeout_s, self.poll_s)
        await click_start(ctx.page)
        ctx.mark("REOPENED: the game is open again")
        if not ladder_ready:
            # Still play the owed bonus out (and answer its extension offer), so the run does not
            # leave the next one mid-bonus -- then report the propagation failure as UNREACHED.
            await play_out_freespins(ctx, trigger, cap=self.resume_cap, settle=True)
            return ScenarioResult(
                self.name, False, None, rounds, ctx.timeline, ctx.events, (start, ctx.now_s()),
                round_windows=ctx.windows(),
                facts={"swap_applied": bool(swap), "new_ladder": want, "client_ladder": seen,
                       "client_on_new_ladder": False, "ladder_loads": loads,
                       "trigger_base_stake": trigger_base},
                reason=(f"the game server never served the new bet ladder {want} within "
                        f"{self.propagation_timeout_s}s of the BO change ({loads} load(s), still "
                        f"{seen}) -- a BO -> game-server propagation problem, not a game defect"))
        gi = await read_game_info(ctx.page)     # the PRE-START capture, so `pending` is intact
        pending = GAME.pending(gi) or {}
        is_pending_freespin = GAME.pending_is_bonus(gi)
        info = await ctx.eval("""() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return { captured: b.isGameInfoCaptured ? !!b.isGameInfoCaptured() : null,
                     denoms: s.denominations || (b.getDenominations ? b.getDenominations() : []) || [] }; }""")

        # 4. Play the resumed bonus out, reading the bet readout as each round arrives (after the
        #    drain the bonus is over and the readout may have moved on). Only when `pending` says a
        #    bonus is owed: waiting on a round that is not coming hangs.
        resume_bets: list = []

        async def read_bet(_round: Bet) -> None:
            resume_bets.append(await ctx.eval("""() => {
                const s = window.__gameTestBridge.getSnapshot(); return s ? s.betAmount : null; }"""))

        resumed: list = []
        resume_capped = False
        if is_pending_freespin:
            # Both helpers return [trigger, ...rounds]; the trigger is already in `rounds`.
            if GAME.mechanism == "hold-spin":
                chain, resume_capped = await play_out_holdspin(
                    ctx, trigger, cap=self.resume_cap, on_round=read_bet)
            else:
                chain = await play_out_freespins(
                    ctx, trigger, cap=self.resume_cap, settle=False, on_round=read_bet)
            resumed = chain[1:]
        rounds.extend(resumed)

        # 5. Back to the base game, then film it.
        await ctx.settle()
        after = await ctx.eval("""() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {}; return { betAmount: s.betAmount }; }""")
        restored_at = ctx.now_s()
        ctx.mark("BONUS OVER: back in the base game")
        await asyncio.sleep(self.dwell_s)
        await ctx.screenshot("after_bonus")

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _same(a, b) -> bool:
            return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6

        def _on(v, ladder) -> bool:
            return any(_same(v, x) for x in ladder)

        new_ladder = [x for x in (_num(v) for v in (swap.get("bet_levels") or [])) if x is not None]
        client_ladder = [x for x in (_num(v) for v in (info.get("denoms") or [])) if x is not None]
        new_default = _num(swap.get("default_bet"))
        pending_stake = _num(pending.get(GAME.F_STAKE, pending.get("stake")))
        bets = [_num(v) for v in resume_bets]
        bet_after = _num(after.get("betAmount"))
        marks = {"trigger": trigger.seq, "restored": {"t": round(restored_at, 3)}}
        if resumed:
            marks.update({"resume_first": resumed[0].seq, "resume_last": resumed[-1].seq})
        facts = {
            "base_spins": len(rounds) - len(resumed),
            "trigger_stake": trigger.stake,
            "trigger_base_stake": trigger_base,
            # ── the swap. `swap_applied` is the gate: without it this is a plain resume.
            "swap_applied": bool(swap.get("denomination_id")),
            "swap_denomination_id": swap.get("denomination_id"),
            "swap_denomination_name": swap.get("name"),
            "swap_created_ladder": swap.get("created"),
            "previous_denomination_id": swap.get("previous_denomination_id"),
            "new_ladder": new_ladder,
            "new_default_bet": new_default,
            "new_default_level": swap.get("default_bet_level"),
            # what the REOPENED client offers -- proves the new ladder actually reached it
            "client_ladder": client_ladder,
            "client_on_new_ladder": bool(new_ladder) and client_ladder == new_ladder,
            # THE PREMISE of the case: the old stake is not a value of the new ladder. If it were,
            # "the resumed bonus kept its stake" and "the new ladder applied" would look identical.
            "old_bet_on_new_ladder": _on(trigger_base, new_ladder),
            # ── the resume. `is_pending_freespin` is the gate: False means the bonus had already
            # ended (or never opened) before the close, so nothing was resumed.
            "info_captured": info.get("captured"),
            "is_pending_freespin": bool(is_pending_freespin),
            "pending_free_spins": GAME.pending_free_spins(gi),
            "pending_stake": pending_stake,
            "pending_stake_is_trigger": _same(pending_stake, trigger_base),
            "resumed_rounds": len(resumed),
            # the resumed feature was cut short by the max-win cap (Hold & Spin only)
            "resume_capped": resume_capped,
            "resume_bets": bets,
            # the client's bet readout during EVERY resumed round was the triggering stake...
            "bet_during_bonus_is_old": bool(bets) and all(_same(v, trigger_base) for v in bets),
            # ...and the stake the server FROZE for the resume (`gameInfo.pending`), which is the
            # figure the requirement names: "during Free Spin, the stake is the frozen one"
            "bet_during_bonus_is_pending": (bool(bets) and pending_stake is not None
                                            and all(_same(v, pending_stake) for v in bets)),
            # ...and never a value of the new ladder (stated separately: a readout that shows a
            # third figure fails the first and passes this one, and that is worth knowing)
            "bet_during_bonus_on_new_ladder": any(_on(v, new_ladder) for v in bets),
            # ── the base game after the bonus
            "bet_after_bonus": bet_after,
            "bet_after_bonus_is_new_default": _same(bet_after, new_default),
            "bet_after_bonus_on_new_ladder": _on(bet_after, new_ladder),
        }
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks=marks, facts=facts, shots=ctx.shots)


# ---------------------------------------------------------------------------
# Hold & Spin (Zeus, ZZ-30..ZZ-43). The feature's whole rule set is in the response, so these
# scenarios only have to REACH the situation and record the stream -- the invariants above do the
# grading. Reaching it naturally is expensive, so a run normally forces the outcome with a BO
# paytable (`paytable:` on the run); the fixtures are games/zeus/paytables/fs-trigger*.json.
# ---------------------------------------------------------------------------
async def settle_to_base(ctx: DriveContext, label: str = "", dwell_ms: int = 2000,
                         timeout_ms: int | None = None, watch_ms: int = 5000,
                         hold_ms: int = 3000, slice_ms: int = 5000) -> dict:
    """Take a FINISHED feature all the way back to a spinnable base game, PROVE it got there, and
    leave footage of it either way.

    The bookend to `play_out_holdspin`. Draining answers "did every round play?"; this answers "and
    did the game come back?" — which for a bought bonus is half the case (ZZ-43), and which a bare
    `settle()` could not answer: it returns the same way whether the game reached a clean base or
    its own silent 3-second fallback fired, and it left the total-win popup up for the next run to
    inherit (Rule 5).

    **The end of a feature is a PRESENTATION, not a popup close.** Measured on a real Zeus bought
    bonus: the last respin's response landed at t=77.9 and the game was not spinnable again until
    t≈99.4 — **21 seconds** of count-up, total-win and transition. A first version of this bounded
    the wait at 15s on the reasoning that "closing a popup is a UI action, not a bonus"; that was
    the wrong model of what is being waited for, so it timed out mid-animation. The budget is
    therefore `spin_timeout_ms` — the framework's own "wait until the game is spinnable" budget,
    which is exactly this question — and it is safe to be generous BECAUSE the loop below actively
    clears what would otherwise make the wait unsatisfiable, rather than sitting on it.

    **The celebration usually is not up yet when we arrive.** The last response precedes the win
    presentation, so reading `visiblePopups` once, immediately, sees nothing and concludes there was
    no celebration. So this polls in slices: wait a little for spin readiness, and if it is not ready
    yet, look for a popup — a celebration that has since appeared is dismissed and the wait resumes.

    **It HOLDS whether or not it got there** (Rule 14, and Rule 4's tail). If the game did come back,
    the hold is what puts an idle base screen on the clip for the AI to judge. If it did NOT, the
    stuck screen IS the evidence and the clip has to contain it — an earlier version returned
    immediately on failure, which ended the recording exactly at the winning animation and left
    nothing to look at. The mark says which case it is, so the ACTION TIMELINE never captions a
    still-animating game as sitting still.

    Mirrors `confirm_stopped` for the tail: WATCH `watch_ms` for a further bet (nothing arriving is
    the positive evidence that the game is idle rather than still playing), then HOLD `hold_ms`.
    It does not CALL that helper, because that one reads the client's auto flag and returns
    auto-named facts, and the auto flag is not this claim (Rule 15).

    facts: `returned_to_base` / `celebration_shown` / `celebration_auto_closed` /
    `popups_at_end` / `base_ready_seconds` / `bet_after_return` / `base_watch_s` / `base_hold_s`
    """
    tag = f"settle-to-base [{label}]" if label else "settle-to-base"
    budget = timeout_ms if timeout_ms is not None else ctx.spin_timeout_ms
    t0 = ctx.now_s()
    shown = list(await ctx.popups())        # may well be empty here -- the count-up comes first
    if shown:
        ctx.mark(f"FEATURE OVER: celebration on screen -- {', '.join(shown)}")
    await ctx.dwell(dwell_ms)               # film whatever is on screen before touching anything

    reached, dismissed, seen = False, False, list(shown)
    deadline = ctx.now_s() + budget / 1000
    while True:
        try:
            await wait_spin_clickable(ctx.page, slice_ms)
            reached = True
            break
        except Exception:  # noqa: BLE001 -- not ready yet: still presenting, or something blocks
            pops = list(await ctx.popups())
            for lbl in pops:
                if lbl not in seen:
                    seen.append(lbl)
            if pops:
                # A celebration that appeared AFTER we first looked. Acknowledging it is the
                # player's own next action; it happens after the dwell above, so it is filmed.
                ctx.mark(f"CELEBRATION: clearing {', '.join(pops)}")
                await ctx.dismiss_popups()
                dismissed = True
            if ctx.now_s() >= deadline:
                break
    ready_s = round(ctx.now_s() - t0, 2)
    left = list(await ctx.popups())

    # WATCH, then HOLD (Rule 4) -- and hold on the failure path too, because a game that did not
    # come back is exactly what the clip needs to show.
    extra = False
    if reached:
        try:
            raw = await await_bet_response(ctx.page, watch_ms)
            extra = True                    # something bet again -> it did not settle to idle
            ctx._record(Bet.parse(raw, len(ctx.events)), "base", ctx.now_s())
        except BetRejected:            # a bet WAS sent (and refused): never read that as "idle"
            raise
        except Exception:  # noqa: BLE001 -- nothing bet in the window = idle, which is the point
            extra = False
        # the same text either way: a mark that said "a further round was wagered" told the model
        # the answer; the round itself is in the stream and in the clip
        ctx.mark("BACK AT BASE -- watching the game")
    else:
        print(f"  !! {tag}: the game was still not spinnable after {ready_s}s"
              + (f"; popup(s) up: {', '.join(left)}" if left else " (screen clear)"))
        ctx.mark(f"DID NOT RETURN TO BASE after {ready_s}s"
                 + (f" -- {', '.join(left)} still up" if left else " -- screen clear, still busy"))
    await ctx.dwell(hold_ms)
    return {"returned_to_base": reached and not extra,
            "celebration_shown": bool(seen),
            "celebration_auto_closed": bool(seen) and not dismissed,
            "popups_at_end": ",".join(left) or "none",
            "base_ready_seconds": ready_s,
            "bet_after_return": extra,
            "base_watch_s": round(watch_ms / 1000, 1),
            "base_hold_s": round(hold_ms / 1000, 1)}


def enters_holdspin(b: Bet) -> bool:
    """Does THIS round BEGIN a Hold & Spin feature -- including the instant win that skips it?

    `is_freespin_trigger` alone is not enough here, and it is not wrong: a round that lands the full
    grid reports **base state, no respins and 9 coins**, and `is_trigger` deliberately answers False
    to it, because the round that ENDS an ordinary feature looks exactly the same and must not send
    a scenario off to drain a bonus that just finished (see `HoldSpinGame.is_trigger`).

    But 9 coins landing on a BASE spin is the game's documented instant win -- "9 Coins in the base
    game pay out immediately without respinning" (ZZ-31), which `fs-trigger9` forces on every spin.
    Keyed off `is_trigger` alone, the hunting loops never recognised it: the round paid, the game
    returned to base, and the scenario **kept spinning** to its 200-spin cap before reporting
    unreached -- the "it already won with 9 coins and then carried on spinning" symptom, and the one
    path the fixture exists to reach.

    So: a trigger, OR a full grid. Order matters -- `is_trigger` first, so an ordinary trigger is
    still recognised as one. Guarded with getattr because `grid_full` lives on the hold-spin profile
    and this module is shared with games that have no such notion.
    """
    if is_freespin_trigger(b):
        return True
    full = getattr(GAME, "grid_full", None)
    return bool(full and full(b.raw or {}))


async def play_out_holdspin(ctx: DriveContext, trigger: Bet, cap: int = 60,
                            mark: bool = True, on_round=None) -> tuple:
    """Drain a triggered Hold & Spin to its END. Returns `(rounds, capped)`.

    The Hold & Spin equivalent of `play_out_freespins`, and it exists for the same reason: the
    three scenarios that drove the feature each hand-wrote this loop, and each got the same three
    rules wrong.

    * **Rule 2 — the end is `is_terminal`, plus this mechanism's own `grid_full`.** The loops read
      `nxt.free_spins == 0`, which misses a round that returns to the base state with respins still
      on the counter (a max-win truncation, or a build that nulls `freeSpinInfo` on the last round —
      Zeus does exactly that). `is_terminal` is both halves. `grid_full` is separate and deliberate:
      a full grid ends the feature with respins still owed, and that is correct.
    * **Rule 9 — `hasReachedMaxWin` ends the round wherever it lands**, including the trigger and
      the first respin. It is reported, never asserted away, so a run whose cap is too tight for the
      paytable is visible as `facts.capped` instead of looking like a broken feature.
    * **Rule 13 — never end a drain on a COUNT.** Falling out of `for _ in range(cap)` used to
      return the rounds as though the feature had finished: the caller then settles against a spin
      button that cannot become ready, the run ends INSIDE the bonus, and the next run's teardown
      times out on a round the client already emitted. Fail here, where the cause is nameable.

    A full grid on the trigger itself fires no respin at all (ZZ-31), so nothing is awaited.

    `on_round` is an optional async callback given each respin as it arrives -- for evidence that
    can only be read DURING the feature (e.g. the client's bet readout).
    """
    raw0 = trigger.raw or {}
    rounds = [trigger]
    capped = bool(trigger.has_max_win)
    if GAME.grid_full(raw0) or capped:
        return rounds, capped
    for _ in range(cap):
        nxt = await ctx.free_spin()
        rounds.append(nxt)
        if on_round is not None:
            await on_round(nxt)
        raw = nxt.raw or {}
        if mark:
            ctx.mark(f"RESPIN {len(rounds) - 1}: {GAME.coin_count(raw)}/{GAME.GRID_SLOTS} coin(s) held, "
                     f"{nxt.free_spins} respin(s) left")
        if nxt.has_max_win:
            capped = True
            ctx.mark("MAX WIN reached -- the feature ends here (Rule 9), respins forfeited")
            return rounds, capped
        if is_terminal(nxt) or GAME.grid_full(raw):
            return rounds, capped
    raise RuntimeError(
        f"Hold & Spin drain gave up after {cap} respin(s) with the feature still running "
        f"({GAME.coin_count(rounds[-1].raw or {})}/{GAME.GRID_SLOTS} coins, "
        f"{rounds[-1].free_spins} respin(s) still on the counter). Every new coin resets the "
        f"counter, so a paytable that keeps landing them can outlast any cap -- lower the coin "
        f"weight (Rule 6) rather than raising this.")


async def play_out_bonus(ctx: DriveContext, trigger: Bet, cap=None, settle: bool = True,
                         on_round=None) -> list:
    """Drain WHATEVER BONUS THIS GAME HAS, chosen by mechanism. Returns `[trigger, ...rounds]`.

    **Use this for Rule 1** — "any spin can trigger a bonus" — where the trigger is incidental and
    the scenario only needs the game back in a spinnable base. `play_out_freespins` was called
    directly for that, which is wrong on a Hold & Spin game: it ends only on `is_terminal` and so
    misses a FULL GRID, which ends the feature with respins still on the counter. The scenario then
    settles against a spin button that cannot become ready and the run ends inside the feature.

    A scenario that GRADES the feature calls `play_out_holdspin` itself — it needs the `capped` flag
    this drops, and the respin marks. This is the "I just need it over" door.

    `cap` defaults to each drain's own budget (60 respins / 500 free spins). `settle` matches
    `play_out_freespins`, including for Hold & Spin, which does not settle on its own.
    """
    if GAME.mechanism == "hold-spin":
        rounds, _capped = await play_out_holdspin(ctx, trigger, cap=cap if cap is not None else 60,
                                                  on_round=on_round)
        if settle:
            await ctx.settle()
        return rounds
    return await play_out_freespins(ctx, trigger, cap=cap if cap is not None else 500,
                                    settle=settle, on_round=on_round)


# ---------------------------------------------------------------------------
# Paid spin features — Ante Bet and Premium Reel
#
# Both multiply what a spin COSTS (`stake`) and leave the payout base (`baseStake`) alone, and a round
# echoes which one it was bet with. They differ in how a player reaches them: Chaos's Ante is a
# one-click `ante` toggle, while Dragon VIP puts Ante AND every Premium Reel package behind the Buy
# Bonus panel (open -> ACTIVATE row -> confirm sheet) and turns them off with one click on the main
# button. `switch_feature` is the one place that difference lives, so a scenario names the FEATURE and
# never the flow.
# ---------------------------------------------------------------------------
_FEATURE_STATE_JS = (
    "() => { const b = window.__gameTestBridge; if (!b || !b.getSnapshot) return {};"
    " const s = b.getSnapshot();"
    " return { ante: s.isAnteBet === undefined ? null : !!s.isAnteBet,"
    "          premium: s.isPremiumReel === undefined ? null : !!s.isPremiumReel,"
    "          premiumId: s.premiumReelId, premiumMultiplier: s.premiumReelMultiplier,"
    "          anteMultiplier: s.anteMultiplier, anteAvailable: s.anteBetAvailable,"
    "          spinCost: s.spinCost, bet: s.betAmount }; }")


async def _feature_state(ctx: DriveContext) -> dict:
    """The client's own account of its paid features. UI evidence only — what a round was actually
    bet with is read from the RESPONSE (`_round_carries`)."""
    return await ctx.ui_read(_FEATURE_STATE_JS, {}) or {}


def _feature_on(state: dict, feature: str):
    """Whether `state` shows `feature` ("ante" | "premium:<reelId>") switched on. `None` when the
    bridge does not publish the flag, so "unknown" never reads as "off"."""
    if feature == "ante":
        return state.get("ante")
    if state.get("premium") is None:
        return None
    return bool(state.get("premium")) and str(state.get("premiumId")) == feature.split(":", 1)[1]


def _round_carries(b: Bet, feature: str) -> bool:
    """Was this ROUND bet with `feature`? From the response, never from the client."""
    raw = b.raw or {}
    if feature == "ante":
        return GAME.is_ante_round(raw)
    return str(GAME.premium_reel_id(raw)) == feature.split(":", 1)[1]


def _near(a, b, tol: float = 0.005) -> bool:
    """Money equality to the cent. `None` on either side is never equal."""
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


async def switch_feature(ctx: DriveContext, feature: str, on: bool, optional: bool = True,
                         dwell_ms: int | None = None) -> dict:
    """Switch a paid spin feature ON or OFF: `"ante"`, or `"premium:<reelId>"` for a Premium Reel
    package. SWITCHES, never blind-toggles: a feature already in the wanted state is left alone,
    because a toggle clicked against the wrong state inverts the whole case (see `AnteToggle`).

    Flows, by what the profile names:
      * `ante` role (Chaos)           one click on the toggle — `optional=False` raises if it never
                                      becomes clickable, exactly as the old inline click did;
      * `ante_on` / `premium_reel`    ON: `buy_bonus` opens the panel, the row is scrolled into view
        (Dragon VIP)                  and clicked, `buy_confirm` accepts the sheet;
                                      OFF: one click on `ante_off` / `premium_off`.

    Samples `visiblePopups` for ~2.5 s after the last click, so a feature toast (`anteNotify`, which
    fades in about that long) is caught rather than missed by a single read.

    Returns `{"switched", "clicked", "popups"}`: `switched` is the client's flag afterwards compared
    with `on` (`None` where the bridge publishes no flag), `clicked` whether this call changed
    anything, `popups` every label seen after the final click."""
    before = await _feature_state(ctx)
    if _feature_on(before, feature) == on:
        return {"switched": True, "clicked": False, "popups": []}
    premium_id = None if feature == "ante" else feature.split(":", 1)[1]
    clicked = False
    if feature == "ante" and GAME.button("ante"):
        ctx.mark(f"ANTE: toggle {'ON' if on else 'OFF'}")
        clicked = await ctx.click_button("ante", optional=optional, dwell_ms=0)
    elif on:
        row = GAME.button("ante_on") if premium_id is None else GAME.button("premium_reel", i=premium_id)
        if row and GAME.button("buy_bonus") and GAME.button("buy_confirm"):
            ctx.mark(f"{feature.upper()}: open the feature panel")
            if await ctx.click_button("buy_bonus", optional=True, timeout_ms=15000,
                                      dwell_ms=ctx.step_dwell_ms):
                # A row below the fold reports `clipped_by_mask` and never becomes clickable.
                await ctx.ui_read(
                    f"() => {{ const b = window.__gameTestBridge;"
                    f" if (b && b.scrollBuyFeatureIntoView) b.scrollBuyFeatureIntoView({json.dumps(row)});"
                    f" return true; }}")
                ctx.mark(f"{feature.upper()}: ACTIVATE row `{row}`")
                if await ctx.click_button(row, optional=True, timeout_ms=8000,
                                          dwell_ms=ctx.step_dwell_ms):
                    ctx.mark(f"{feature.upper()}: confirm")
                    clicked = await ctx.click_button("buy_confirm", optional=True, timeout_ms=8000,
                                                     dwell_ms=0)
            if not clicked:
                await ctx.dismiss_popups()          # never leave a half-used panel over the reels
    else:
        off = GAME.button("ante_off") if premium_id is None else GAME.button("premium_off")
        if off:
            ctx.mark(f"{feature.upper()}: switch OFF")
            clicked = await ctx.click_button(off, optional=optional, timeout_ms=8000, dwell_ms=0)
    seen: list = []
    if clicked:
        for _ in range(10):
            for label in await ctx.popups():
                if label not in seen:
                    seen.append(label)
            await asyncio.sleep(0.25)
        await ctx.dwell(dwell_ms)
    return {"switched": _feature_on(await _feature_state(ctx), feature) == on if clicked else False,
            "clicked": clicked, "popups": seen}


async def _ante_offered(ctx: DriveContext) -> bool:
    """Does this session offer Ante Bet at all? A toggle build answers by the control's presence; a
    panel build by the snapshot, because its ACTIVATE row only resolves while the panel is open."""
    if GAME.button("ante"):
        return await ctx.has_button("ante")
    if not GAME.button("ante_on"):
        return False
    return bool((await _feature_state(ctx)).get("anteAvailable"))


async def _watch_controls(ctx: DriveContext, roles: tuple, stop: asyncio.Event) -> dict:
    """Probe `roles` with instant `has_button` reads every 0.5 s until `stop` is set. Returns
    `{role: True if it was EVER available}` plus `samples`. For "this control stays locked for the
    whole feature": one probe at the trigger is taken while the reels are still turning, when every
    control is locked anyway, and proves nothing."""
    seen = {r: False for r in roles}
    samples = 0
    while not stop.is_set():
        for r in roles:
            if not seen[r] and await ctx.has_button(r):
                seen[r] = True
        samples += 1
        try:
            await asyncio.wait_for(stop.wait(), 0.5)
        except asyncio.TimeoutError:
            pass
    return {**seen, "samples": samples}


class HoldSpinComplete(Scenario):    # ZZ-30/32/33/34/35/36/38 · DV-57/58 (ante) · DV-64/65 (premium)
    name = "hold-spin-complete"
    description = ("Spin until Hold & Spin triggers, then play the WHOLE feature out and expose every "
                   "round. The client drives the respins, so this collects them passively. Exposes "
                   "trigger + rounds for the holdspin_* invariants (coins held, counter reset on a new "
                   "coin, count-down without one, correct end) and facts.trigger_coins / final_coins / "
                   "respins_played / coins_added / grid_filled / bonus_total / capped. Unreached if "
                   "no trigger inside the spin budget -- force one with a `paytable:` fixture rather "
                   "than raising the budget, since a natural trigger is rare.\n"
                   "The feature is drained by `play_out_holdspin`, so the end is read from the game "
                   "(Rule 2) and a max-win truncation is REPORTED as facts.capped rather than passing "
                   "as a clean finish (Rule 9) -- a capped feature satisfies holdspin_ends_correctly "
                   "for the wrong reason, so a case that needs the whole feature asserts `capped == "
                   "False` and the respin LENGTH it needs.\n"
                   "`feature` (\"ante\" | \"premium:<reelId>\") plays the Hold & Spin as a bet WITH a paid "
                   "spin feature: switched on before the hunt through `switch_feature`, and back off "
                   "once the feature has ended. Every hunting spin must carry it -- the run is "
                   "unreached the moment one does not, rather than hunting at the plain odds. Adds "
                   "facts.feature_on_trigger / trigger_stake / trigger_base_stake / trigger_multiplier "
                   "/ trigger_stake_matches / feature_multiplier_configured / respin_stakes / "
                   "respin_premium_ids / off_available_in_feature (the off control sampled through "
                   "the whole feature, never waited on) / feature_on_after_bonus / feature_restored. "
                   "Force the trigger in the feature's OWN reel section (Dragon VIP: "
                   "`ante-trigger3-countdown`, `premium-trigger3-countdown`), so the trigger itself "
                   "proves the server spun the feature's reels.")

    def __init__(self, cap: int = 200, respin_cap: int = 60, settle: bool = True,
                 feature: str | None = None):
        self.cap = cap                  # base spins to hunt a trigger through
        self.respin_cap = respin_cap    # respins to drain before failing loudly (Rule 13)
        self.settle = settle
        self.feature = feature          # None | "ante" | "premium:<reelId>", on for the whole run
        self._left_on = False           # this run switched the feature on and has not switched it off

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        # The feature this run switched on goes back off whether the run ends or FAILS. A failed
        # drain (respin cap, timeout, rejected bet) used to skip the switch-off and hand the next
        # run a game still betting with Ante / Premium on. The failure can land INSIDE the bonus,
        # where the off control is locked, so the game is taken back to base first.
        self._left_on = False
        try:
            return await self._play(ctx)
        except Exception:
            if self._left_on:
                ctx.mark(f"{self.feature.upper()}: the run failed -- back to base, then switch it OFF")
                try:
                    await return_to_base(ctx, assert_base=False)
                    await switch_feature(ctx, self.feature, False)
                except Exception as e:  # noqa: BLE001 -- the original failure is the one to report
                    print(f"  hold-spin-complete: could not switch `{self.feature}` off after the "
                          f"failure: {type(e).__name__}: {e}")
            raise

    async def _play(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        feature = self.feature
        off_role = "ante_off" if feature == "ante" else "premium_off"
        facts: dict = {"feature": feature}
        turned_on = False

        async def restore() -> None:
            # Only what THIS run switched on, and only once the evidence is in (Rule 14).
            if turned_on:
                ctx.mark(f"{feature.upper()}: switch OFF again -- leaving the game as this run found it")
                off = await switch_feature(ctx, feature, False)
                self._left_on = False
                facts.update({"feature_restored": off["switched"]})

        if feature:
            sw = await switch_feature(ctx, feature, True)
            st = await _feature_state(ctx)
            turned_on = sw["clicked"]
            self._left_on = bool(turned_on)
            facts.update({"feature_switched_on": sw["switched"],
                          "feature_multiplier_configured": (st.get("anteMultiplier") if feature == "ante"
                                                            else st.get("premiumMultiplier")),
                          "feature_spin_cost": st.get("spinCost"), "feature_bet": st.get("bet")})
            if not sw["switched"]:
                await restore()
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                      reason=f"could not switch `{feature}` on -- every spin would be "
                                             f"a plain bet")
        trigger, spins = None, 0
        for _ in range(self.cap):
            b = await ctx.base_spin()
            spins += 1
            # `enters_holdspin`, NOT `is_freespin_trigger`: a base spin that lands the full grid pays
            # instantly and skips the respins, and `is_trigger` answers False to it by design.
            if enters_holdspin(b):
                trigger = b
                break
            await ctx.settle()
            if feature and not _round_carries(b, feature):
                # The forced trigger lives in the feature's own reel section, so a spin bet without
                # the feature would hunt at the plain odds for the whole budget. Stop and say so.
                await restore()
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), round_windows=ctx.windows(),
                                      facts={**facts, "base_spins": spins},
                                      reason=f"spin {spins} was not bet with `{feature}` (stake "
                                             f"{b.stake}, base {GAME.base_stake(b.raw or {})}) although "
                                             f"the client showed it on")
        if trigger is None:
            await restore()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={**facts, "base_spins": spins},
                                  reason=f"no Hold & Spin trigger in {spins} base spins")
        raw0 = trigger.raw or {}
        ctx.mark(f"HOLD & SPIN TRIGGERED on {GAME.coin_count(raw0)} coin(s)")
        # A player must not be able to drop a paid feature out of a bonus it paid for. Sampled for
        # the whole drain in the background: a single probe at the trigger lands while the reels are
        # still turning, when the control is locked for an unrelated reason.
        stop = asyncio.Event()
        watch = asyncio.create_task(_watch_controls(ctx, (off_role,), stop)) if feature else None
        try:
            # A full grid on the trigger skips the respins entirely (ZZ-31), a max win ends it on the
            # spot (Rule 9), and the drain fails loudly rather than returning a half-played feature
            # (Rule 13) -- all three live in the helper.
            rounds, capped = await play_out_holdspin(ctx, trigger, cap=self.respin_cap)
        finally:
            stop.set()
        seen = await watch if watch else {}
        if self.settle:
            await ctx.settle()
        last = rounds[-1].raw or {}
        respins = rounds[1:]
        base0 = GAME.base_stake(raw0)
        mult0 = (GAME.ante_multiplier(raw0) if feature == "ante"
                 else GAME.premium_reel_multiplier(raw0) if feature else None)
        if feature:
            facts.update({"feature_on_after_bonus": _feature_on(await _feature_state(ctx), feature)})
        facts.update({"base_spins": spins,
                      "trigger_coins": GAME.coin_count(raw0),
                      "final_coins": GAME.coin_count(last),
                      "coins_added": GAME.coin_count(last) - GAME.coin_count(raw0),
                      "respins_played": len(rounds) - 1,
                      "respins_left": rounds[-1].free_spins,
                      "grid_filled": GAME.grid_full(last),
                      "coin_total": GAME.coin_total(last),
                      "bonus_total": round(sum(b.payout for b in rounds), 4),
                      # Rule 9: a truncated feature ends cleanly and passes every
                      # obvious assert while showing almost nothing. Say so.
                      "capped": capped,
                      "instant_finish": GAME.grid_full(raw0),
                      "trigger_stake": trigger.stake,
                      "trigger_base_stake": base0,
                      # Gate every feature claim on this: the TRIGGER round was really bet with it.
                      "feature_on_trigger": _round_carries(trigger, feature) if feature else None,
                      "trigger_multiplier": mult0,
                      "trigger_stake_matches": _near(trigger.stake, base0 * mult0) if mult0 else None,
                      "respin_stakes": sorted({b.stake for b in respins}),
                      "respin_premium_ids": sorted({GAME.premium_reel_id(b.raw or {}) for b in respins}),
                      "off_available_in_feature": seen.get(off_role) if feature else None,
                      "control_samples": seen.get("samples")})
        await restore()
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"trigger": trigger.seq, "terminal": rounds[-1].seq},
                              facts=facts)


class HoldSpinFullGrid(Scenario):    # ZZ-31/37
    name = "hold-spin-full-grid"
    description = ("The full-grid path: land every slot and the feature must pay out immediately "
                   "instead of spinning. Plays until the grid fills -- on the trigger itself (no "
                   "respin is fired at all) or during the respins -- and exposes facts.filled_on_"
                   "trigger / respins_played / final_coins / capped. Needs a forced outcome to be "
                   "practical: run it with the `fs-trigger9` paytable fixture. Unreached if the grid "
                   "never fills, which is the honest verdict for an unforced run -- and a MAX-WIN "
                   "truncation is one of the ways it can fail to fill, so the reason names it.")

    def __init__(self, cap: int = 200, respin_cap: int = 60):
        self.cap = cap                  # base spins to hunt a trigger through
        self.respin_cap = respin_cap    # respins to drain before failing loudly (Rule 13)

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        trigger, spins = None, 0
        for _ in range(self.cap):
            b = await ctx.base_spin()
            spins += 1
            # `enters_holdspin`, NOT `is_freespin_trigger`: a base spin that lands the full grid pays
            # instantly and skips the respins, and `is_trigger` answers False to it by design.
            if enters_holdspin(b):
                trigger = b
                break
            await ctx.settle()
        if trigger is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"base_spins": spins},
                                  reason=f"no Hold & Spin trigger in {spins} base spins")
        raw0 = trigger.raw or {}
        filled_on_trigger = GAME.grid_full(raw0)
        ctx.mark(f"TRIGGER: {GAME.coin_count(raw0)}/{GAME.GRID_SLOTS} coins"
                 + (" -- FULL GRID, respins skipped" if filled_on_trigger else ""))
        rounds, capped = await play_out_holdspin(ctx, trigger, cap=self.respin_cap)
        if GAME.grid_full(rounds[-1].raw or {}) and not filled_on_trigger:
            ctx.mark(f"GRID FILLED on respin {len(rounds) - 1}")
        await ctx.settle()
        last = rounds[-1].raw or {}
        facts = {"base_spins": spins, "filled_on_trigger": filled_on_trigger,
                 "final_coins": GAME.coin_count(last), "respins_played": len(rounds) - 1,
                 "grid_filled": GAME.grid_full(last), "coin_total": GAME.coin_total(last),
                 "bonus_total": round(sum(b.payout for b in rounds), 4), "capped": capped}
        if not GAME.grid_full(last):
            # Rule 9: a max-win truncation ends the feature wherever it lands, so "the grid never
            # filled" has two causes that look identical in the facts. Name the one that happened,
            # because the fixes are opposite: raise the run's cap, or force the coins harder.
            return ScenarioResult(self.name, False, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason=(f"the max-win cap truncated the feature at "
                                          f"{GAME.coin_count(last)}/{GAME.GRID_SLOTS} coins -- raise "
                                          f"this run's max_win_x, the grid cannot fill under it"
                                          if capped else
                                          f"grid never filled ({GAME.coin_count(last)}/"
                                          f"{GAME.GRID_SLOTS}) -- force it with the fs-trigger9 paytable"))
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"trigger": trigger.seq, "terminal": rounds[-1].seq}, facts=facts)
async def purchase_bonus(ctx: DriveContext, package: int = 0) -> tuple:
    """Buy the bonus: open the panel, pick a package, confirm, and return the purchase round.

    Returns `(bought, facts)` — the purchase Bet and what the panel reported on the way
    (`bet` `packages` `popup_loaded` `package_picked` `price` `price_ratio` `buy_clicked`
    `confirmed`). Raises `Unreached` if the build has no Buy Bonus, the panel never opens,
    no package selects, or the purchase never fires.

    Extracted from `buy-bonus` so `freespin-reload` can reach a BOUGHT bonus by the same
    path — a second copy of this flow would be a second place for the two-click confirm,
    the `buyBonusSelectedPrice == 0` guard and the `bet_timeout_ms` choice to drift.
    """
    facts: dict = {}
    if not GAME.HAS_BUY_BONUS:
        raise Unreached("this build has no Buy Bonus")
    # Every control this scenario clicks, checked in one place: past here they are safe.
    btn_buy_bonus, btn_package, btn_confirm = require_buttons(
        "buy_bonus", ("buy_package", package), "buy_confirm")
    # Not required: on a build whose package row raises the confirm sheet itself (Dragon VIP)
    # there is no separate Buy button between the pick and the confirmation.
    btn_buy = GAME.button("buy_purchase")

    bet_before = await ctx.ui_read(
        "() => { const b = window.__gameTestBridge;"
        " return b && b.getBetAmount ? b.getBetAmount() : null; }")
    # `facts.update({...})`, never `facts["x"] = `: validate_suites.py reads the exposed
    # keys out of DICT LITERALS, so a subscript store reads as "not exposed".
    facts.update({"bet": bet_before})

    ctx.mark("BUY BONUS: open the popup")
    if not await ctx.click_button(btn_buy_bonus, optional=True, timeout_ms=15000):
        raise Unreached(f"`{btn_buy_bonus}` never became clickable")

    state = await ctx.ui_read(
        "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
        " return { count: s.freeSpinPackageCount, price: s.buyBonusSelectedPrice,"
        "          loaded: s.buyBonusPopupLoaded }; }", {}) or {}
    facts.update({"packages": state.get("count"),
                  "popup_loaded": bool(state.get("loaded"))})

    ctx.mark(f"BUY BONUS: select package {package}")
    # A row below the fold reports `clipped_by_mask` and never becomes clickable (Dragon VIP's
    # panel lists Ante and the Premium Reel packages beside the bonus). No-op elsewhere.
    await ctx.ui_read(
        f"() => {{ const b = window.__gameTestBridge;"
        f" if (b && b.scrollBuyFeatureIntoView) b.scrollBuyFeatureIntoView({json.dumps(btn_package)});"
        f" return true; }}")
    picked = await ctx.click_button(btn_package, optional=True, timeout_ms=8000)
    priced = await ctx.ui_read(
        "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
        " return s.buyBonusSelectedPrice; }")
    facts.update({"package_picked": picked, "price": priced,
                  "price_ratio": (round(priced / bet_before, 4)
                                  if (priced and bet_before) else None),
                  "buy_clicked": False, "confirmed": False})
    # 0 = nothing selected (bridge-api.md §5). Confirming now would click an inert control
    # and then wait out the bet timeout for a bet that cannot come.
    if not priced:
        raise Unreached(f"no package selected after clicking `{btn_package}` "
                        f"(buyBonusSelectedPrice={priced!r})")

    # TWO CLICKS: `buy` raises a confirmation modal, `confirm` inside it purchases. Clicking
    # confirm first burns its timeout -- it is scoped to a modal that is not up yet.
    bought_clicked = False
    if btn_buy:
        ctx.mark(f"BUY BONUS: click `{btn_buy}` (raises the confirmation modal)")
        bought_clicked = await ctx.click_button(btn_buy, optional=True, timeout_ms=10000,
                                                dwell_ms=ctx.step_dwell_ms)
    # Optional: a build that purchases straight from the panel raises no modal, and there
    # the bet has already been sent. `observe_bet` reads from the cursor, so it is not lost.
    ctx.mark(f"BUY BONUS: confirm with `{btn_confirm}`")
    confirmed = await ctx.click_button(btn_confirm, optional=True, timeout_ms=8000,
                                       dwell_ms=ctx.step_dwell_ms)
    facts.update({"buy_clicked": bought_clicked,
                  "confirmed": bought_clicked or confirmed})
    if not (bought_clicked or confirmed):
        raise Unreached(f"`{btn_buy}` and `{btn_confirm}` resolve but neither became "
                        f"clickable (price {priced!r})")

    # A purchase is a click-caused round trip, so `bet_timeout_ms` -- not the 90s
    # `auto_timeout_ms` default, which is sized for the gap between two AUTO spins.
    try:
        bought = await ctx.observe_bet(timeout_ms=ctx.bet_timeout_ms)
    except Exception as e:                                          # noqa: BLE001
        why = await ctx.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.canDo ? b.canDo('confirmBuyBonus') : null; }")
        raise Unreached(f"no bet within {ctx.bet_timeout_ms}ms after confirm -- the purchase "
                        f"never fired (canDo {why!r}, price {priced!r})") from e
    return bought, facts


async def sample_purchase_reveal(ctx: DriveContext, timeout_ms: int = 8000) -> dict:
    """Read the grid the CLIENT draws for a Buy Bonus purchase, while it is still on screen.

    ⚠ This exists because the purchase round carries NO reels. Grandma Pets sends five reels of -1
    and draws a "you triggered it" teaser itself (`games/grandma/bet-response.md` §9), so the thing
    the player is shown lives nowhere in the response. `getDisplayedGrid()` during the presentation
    is the only exact record of it — everything else would be an AI reading pixels.

    Polls until the displayed grid is fully populated (no `-1`, which marks a cell still hidden or
    masked), then reports what it found. Returns, all prefixed `reveal_`:

    | fact | meaning |
    |---|---|
    | `reveal_sampled` | a complete grid was captured at all — every other fact is meaningless without it |
    | `reveal_bonus_reels` | how many REELS carry the scatter (the award is keyed on reels, not symbols) |
    | `reveal_max_bonus_per_reel` | the most scatters on any one reel |
    | `reveal_wilds` | wilds on the teaser |
    | `reveal_winning_run` | whether any symbol reaches reels 1, 2 AND 3 — i.e. whether the teaser accidentally shows a winning combination on a round that pays nothing |
    | `reveal_grid` | the grid itself, for the report |

    A build whose bridge has no `getDisplayedGrid()` reports `reveal_sampled: False` rather than
    raising, so the surrounding scenario still runs and the case says "could not verify".
    """
    scatter = str(getattr(GAME, "SYM_SCATTER", "") or "")
    wild = str(getattr(GAME, "SYM_WILD", "") or "")
    # ⚠ TAKE THE GRID THE TEASER SETTLES ON, NOT THE FIRST ONE THAT IS FULLY DRAWN. The reveal is
    # an animation: reels land in turn, and symbols can still be swapped in after every cell has a
    # value. Sampling the first frame with no `-1` catches it mid-flight and reports whatever was
    # on screen at that instant -- which reads exactly like a game that put the wrong symbols out.
    # So: keep sampling, and accept a grid only once it has been IDENTICAL for `stable_hits` polls.
    deadline = ctx.now_s() + (timeout_ms / 1000)
    stable_hits, seen, grid, last, repeats = 3, [], None, None, 0
    while ctx.now_s() < deadline:
        got = await ctx.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getDisplayedGrid ? b.getDisplayedGrid() : null; }")
        full = (isinstance(got, list) and got
                and all(isinstance(r, list) and r for r in got)
                and all(str(c) != "-1" for r in got for c in r))
        if full:
            if got == last:
                repeats += 1
            else:
                repeats, last = 1, got
                if got not in seen:
                    seen.append(got)
            if repeats >= stable_hits:
                grid = got
                break
        await asyncio.sleep(0.15)
    # Settled or not, the last complete grid is better evidence than nothing -- and
    # `reveal_stable` says which it was, so a case never reads a mid-animation frame as final.
    stable = grid is not None
    if grid is None:
        grid = last

    if grid is None:
        return {"reveal_sampled": False, "reveal_stable": False, "reveal_distinct_grids": 0,
                "reveal_bonus_reels": None, "reveal_max_bonus_per_reel": None,
                "reveal_wilds": None, "reveal_winning_run": None, "reveal_grid": None}

    norm = [[GAME._norm(c) for c in reel] for reel in grid]
    want_scatter, want_wild = GAME._norm(scatter), GAME._norm(wild)
    per_reel = [sum(1 for c in reel if c == want_scatter) for reel in norm]

    # A ways win must start at reel 1 and run left to right with no gap, so "reels 1, 2 and 3 all
    # carry the same symbol" is exactly the condition for a paying combination to exist. The wild
    # substitutes, so it counts toward any symbol. The scatter is excluded: it is the thing the
    # teaser is MADE of, and it pays by reel count rather than by a run.
    first_three = norm[:3]
    winning_run = False
    if len(first_three) == 3:
        symbols = {c for reel in first_three for c in reel} - {want_scatter, want_wild}
        winning_run = any(
            all(any(c == sym or c == want_wild for c in reel) for reel in first_three)
            for sym in symbols) or all(want_wild in reel for reel in first_three)

    return {"reveal_sampled": True,
            "reveal_stable": stable,
            # how many DIFFERENT complete grids the teaser showed. > 1 means it was still changing
            # after every cell had a value -- the reason a single early sample cannot be trusted.
            "reveal_distinct_grids": len(seen),
            "reveal_bonus_reels": sum(1 for n in per_reel if n > 0),
            "reveal_max_bonus_per_reel": max(per_reel) if per_reel else 0,
            "reveal_wilds": sum(1 for reel in norm for c in reel if c == want_wild),
            "reveal_winning_run": winning_run,
            "reveal_grid": grid}




class BuyBonus(Scenario):    # ZZ-39..ZZ-43
    name = "buy-bonus"
    description = ("Buy the bonus and play it out: open the Buy Bonus popup, read each package's "
                   "price against the current bet, confirm one, then collect the whole feature. The "
                   "bought round enters Hold & Spin directly, so every holdspin_* invariant applies "
                   "to it unchanged -- which IS the case that buying behaves like a natural trigger. "
                   "Drives the WHOLE round: open, select, confirm, then every respin, then back to "
                   "a spinnable base game -- the last step proven by `settle_to_base`, not assumed. "
                   "Exposes facts.packages / price / bet / price_ratio / package_picked / confirmed / "
                   "trigger_coins / respins_played / bonus_rounds / bonus_ended / bonus_total / "
                   "capped / returned_to_base. Marks `trigger` and `terminal`, so a video spans the "
                   "whole thing with `at: [trigger, terminal]`. Unreached if the build has no Buy "
                   "Bonus, the popup never opens, no package selects, or the purchase never fires.\n"
                   "The bought feature is drained by `play_out_holdspin`, exactly as a natural one "
                   "is -- which is the point of ZZ-42/43: if buying took a different code path, the "
                   "same drain and the same invariants would show it.")

    def __init__(self, package: int = 0, respin_cap: int = 60, sample_reveal: bool = False):
        self.package = package
        self.respin_cap = respin_cap    # respins to drain before failing loudly (Rule 13)
        # Read the grid the CLIENT draws for the purchase, while it is on screen.
        # ⚠ Only a build whose purchase response carries no grid needs this. Grandma Pets sends five
        # reels of -1 and draws the "you triggered it" teaser itself (`games/grandma/
        # bet-response.md` §9), so the reveal exists nowhere in the response and `getDisplayedGrid()`
        # sampled DURING the presentation is the only exact record of what the player saw.
        self.sample_reveal = sample_reveal

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}          # filled as we go, so a failure reports what was known by then
        try:
            bought, buy_facts = await purchase_bonus(ctx, self.package)
            facts.update({"bet": buy_facts.get("bet"),
                          "packages": buy_facts.get("packages"),
                          "popup_loaded": buy_facts.get("popup_loaded"),
                          "package_picked": buy_facts.get("package_picked"),
                          "price": buy_facts.get("price"),
                          "price_ratio": buy_facts.get("price_ratio"),
                          "buy_clicked": buy_facts.get("buy_clicked"),
                          "confirmed": buy_facts.get("confirmed")})

            # The teaser plays between the purchase response and the first bonus round, so it has
            # to be read NOW -- after the drain there is nothing left to see.
            if self.sample_reveal:
                rev = await sample_purchase_reveal(ctx)
                # ⚠ Spelled out rather than `facts.update(rev)`: validate_suites.py reads the
                # exposed keys out of DICT LITERALS, so facts merged from a returned dict are
                # invisible to it and every correct `{facts.reveal_*}` assert is reported as
                # "not exposed" — which is the one error class that would otherwise reach a run.
                facts.update({"reveal_sampled": rev["reveal_sampled"],
                              "reveal_stable": rev["reveal_stable"],
                              "reveal_distinct_grids": rev["reveal_distinct_grids"],
                              "reveal_bonus_reels": rev["reveal_bonus_reels"],
                              "reveal_max_bonus_per_reel": rev["reveal_max_bonus_per_reel"],
                              "reveal_wilds": rev["reveal_wilds"],
                              "reveal_winning_run": rev["reveal_winning_run"],
                              "reveal_grid": rev["reveal_grid"]})
            facts.update({"package_spins": GAME.package_spins(bought.raw or {}),
                          "awarded": GAME.won_free_spins(bought.raw or {})})

            # ⚠ WHICH DRAIN. A bought bonus is whatever bonus this game HAS: Zeus and Dragon buy a
            # Hold & Spin, Grandma buys free spins. Calling `play_out_holdspin` on a free-spin game
            # counts coins that do not exist and ends on a full grid that never comes; calling
            # `play_out_freespins` on Hold & Spin misses a FULL GRID, which ends the feature with
            # respins still owed. `play_out_bonus` picks by `GAME.mechanism`, but it drops `capped`,
            # which ZZ-42/43 grade -- so hold & spin keeps its own call and everything else takes
            # the free-spin drain (which also answers the Free Spin Extension offer).
            if GAME.mechanism == "hold-spin":
                ctx.mark(f"BOUGHT BONUS: {GAME.coin_count(bought.raw or {})} coin(s) on entry")
                rounds, capped = await play_out_holdspin(ctx, bought, cap=self.respin_cap)
                terminal = rounds[-1]
                last = terminal.raw or {}
                ctx.mark(f"BONUS COMPLETE: {GAME.coin_count(last)}/{GAME.GRID_SLOTS} coin(s), "
                         f"{len(rounds) - 1} respin(s), {terminal.free_spins} respin(s) left")
                facts.update({"trigger_coins": GAME.coin_count(bought.raw or {}),
                              "final_coins": GAME.coin_count(last),
                              "coins_added": (GAME.coin_count(last)
                                              - GAME.coin_count(bought.raw or {})),
                              "grid_filled": GAME.grid_full(last),
                              "respins_played": len(rounds) - 1,
                              "respins_left": terminal.free_spins})
            else:
                ctx.mark(f"BOUGHT BONUS: {GAME.won_free_spins(bought.raw or {})} free spin(s) awarded")
                rounds = await play_out_freespins(ctx, bought, cap=self.respin_cap, settle=False)
                capped = bool(rounds[-1].has_max_win)
                terminal = rounds[-1]
                last = terminal.raw or {}
                ctx.mark(f"BONUS COMPLETE: {len(rounds) - 1} free spin(s) played, "
                         f"{terminal.free_spins} left on the counter")
                facts.update({"fs_rounds": len(rounds) - 1,
                              "respins_played": len(rounds) - 1,
                              "respins_left": terminal.free_spins})
            # ...and back to base, proven and then held so the idle screen is on the clip (Rule 4).
            back = await settle_to_base(ctx, label="buy-bonus")
            facts.update({"bonus_rounds": len(rounds),
                          # the feature really ended, on the game's own terms -- true of a bonus
                          # that stopped halfway and never handed back, without this. `grid_full`
                          # is the Hold & Spin half of "ended" and is False on every other
                          # mechanism, where `is_terminal` carries it alone.
                          "bonus_ended": bool(is_terminal(terminal)
                                              or (GAME.mechanism == "hold-spin"
                                                  and GAME.grid_full(last))),
                          "bonus_total": round(sum(b.payout for b in rounds), 4),
                          "capped": capped,
                          "paid_stake": bought.stake,
                          # A bought bonus never inherits a Premium Reel (Dragon VIP client doc §6
                          # rule 7). `0` on every build without the feature.
                          "bought_premium_id": GAME.premium_reel_id(bought.raw or {}),
                          "respin_premium_ids": sorted({GAME.premium_reel_id(b.raw or {})
                                                        for b in rounds[1:]}),
                          **back})
            return ScenarioResult(self.name, True, bought, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  # for a suite that wants a narrower clip; the buy-bonus cases
                                  # use the whole scenario, since the purchase flow precedes round 0.
                                  marks={"trigger": bought.seq, "terminal": terminal.seq},
                                  facts=facts)
        except Unreached as e:
            await ctx.dismiss_popups()      # safe when nothing is open; runs on every failing path
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))


class SoundToggles(Scenario):    # ZZ-24/25
    name = "sound-toggles"
    description = ("Toggle sound and music off and back on, reading the bridge's own getters "
                   "(isSoundEnabled / isMusicEnabled) around each click -- so 'the volume really "
                   "went to zero' is an EXACT check rather than something an AI has to hear. Opens "
                   "Settings if the toggles live there. Exposes facts.sound_before/after_off/"
                   "restored and the same for music. Unreached when the build exposes no sound "
                   "getters (the case cannot be answered without them).")

    _JS = ("() => { const b = window.__gameTestBridge; return {"
           " sound: b && b.isSoundEnabled ? b.isSoundEnabled() : null,"
           " music: b && b.isMusicEnabled ? b.isMusicEnabled() : null }; }")

    def __init__(self, via_settings: bool = True):
        self.via_settings = via_settings

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        if not GAME.HAS_SOUND_GETTERS:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason="no isSoundEnabled/isMusicEnabled getter -- the check would "
                                         "be an AI guess at silence, so it is not attempted")
        before = await ctx.ui_read(self._JS, {}) or {}
        # A settings-scoped toggle only resolves while the popup is open, and the main-screen one may
        # be COVERED by that popup -- so pick the control that matches where we are about to be.
        inside = bool(self.via_settings and GAME.button("settings") and GAME.button("settings_sound"))
        sound_btn = GAME.button("settings_sound") if inside else GAME.button("sound")
        music_btn = GAME.button("settings_music") if inside else None
        if self.via_settings and GAME.button("settings"):
            ctx.mark("SETTINGS: open")
            await ctx.click_button(GAME.button("settings"), optional=True, timeout_ms=10000)
        ctx.mark("SOUND: toggle off")
        toggled = await ctx.click_button(sound_btn, optional=True, timeout_ms=10000)
        if music_btn:                      # a build with a SEPARATE music control needs both moved
            ctx.mark("MUSIC: toggle off")
            await ctx.click_button(music_btn, optional=True, timeout_ms=10000)
        after_off = await ctx.ui_read(self._JS, {}) or {}
        ctx.mark("SOUND: toggle back on")
        await ctx.click_button(sound_btn, optional=True, timeout_ms=10000)
        if music_btn:
            ctx.mark("MUSIC: toggle back on")
            await ctx.click_button(music_btn, optional=True, timeout_ms=10000)
        restored = await ctx.ui_read(self._JS, {}) or {}
        await ctx.dismiss_popups()
        facts = {"toggled": toggled, "sound_button": sound_btn, "music_button": music_btn,
                 "sound_before": before.get("sound"), "sound_after_off": after_off.get("sound"),
                 "sound_restored": restored.get("sound"),
                 "music_before": before.get("music"), "music_after_off": after_off.get("music"),
                 "music_restored": restored.get("music")}
        if not toggled:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts,
                                  reason="the sound control never became clickable")
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts)


class PremiumReelSpin(Scenario):    # DV-59 / 60 / 61 / 62 / 63
    name = "premium-reel-spin"
    description = ("Premium Reel in the base game: a plain baseline spin, then for EACH package the "
                   "session offers (`getPremiumReelOptions()`, narrowed by `reel_ids`) -- switch it "
                   "on through `switch_feature`, probe what it excludes, spin, switch it off -- then a "
                   "final plain spin at the same bet level. One play puts plain and premium rounds "
                   "side by side, which is what `premium_reel_stake` needs to prove the payout base "
                   "never moved.\n"
                   "Exposes facts.packages_configured / configured_reel_ids / configured_multipliers; "
                   "packages_tested / reel_ids_tested / multipliers_tested / stakes; the per-package "
                   "verdicts rolled up as all_engaged / all_stakes_match / all_ids_echoed / "
                   "all_multipliers_echoed / all_bases_unchanged / all_spin_costs_match / "
                   "all_switched_off (each False on an empty list, never vacuously True); what a "
                   "package excludes, probed while it was ON, as panel_blocked_while_on / "
                   "ante_blocked_while_on / ante_block_reasons; distinct_ids_played; and the plain "
                   "spins as plain_stake / plain_base / after_stake / after_premium_id / "
                   "off_spin_plain. Marks `plain` and `after`. Unreached when the profile has no "
                   "Premium Reel or the session offers no package.")

    def __init__(self, reel_ids: list | None = None):
        self.reel_ids = reel_ids        # None -> every package gameInfo configures

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}
        try:
            if not GAME.HAS_PREMIUM_REEL:
                raise Unreached(f"{GAME.name} has no Premium Reel (profile HAS_PREMIUM_REEL=False)")
            require_buttons("buy_bonus", "buy_confirm", "premium_off", ("premium_reel", 0))
            options = await ctx.ui_read(
                "() => { const b = window.__gameTestBridge;"
                " return b && b.getPremiumReelOptions ? b.getPremiumReelOptions() : []; }", []) or []
            # Compared as TEXT: the bridge sends numbers (2, 23) and a suite may quote them ("2"),
            # which used to match nothing and report the package as not offered.
            asked = None if self.reel_ids is None else {str(x) for x in self.reel_ids}
            wanted = [o for o in options if asked is None or str(o.get("reelId")) in asked]
            facts.update({"packages_configured": len(options),
                          "configured_reel_ids": [o.get("reelId") for o in options],
                          "configured_multipliers": [o.get("multiplier") for o in options]})
            if not wanted:
                # `[]` means the SESSION does not ship the feature: `settings.premiumReel` is null, so
                # the client hides it and the server would reject a premium bet (-112). That is a BO /
                # launch-config question, not a game defect -- say so, rather than reporting it as one.
                raise Unreached(f"no Premium Reel package to test (offered {options!r}, asked for "
                                f"{self.reel_ids!r}). An empty list means this launch has "
                                f"`settings.premiumReel: null` -- enable the feature for this player "
                                f"and currency on BO first")

            # Start from a PLAIN spin. A package or Ante left on by an earlier run would make the
            # baseline itself a feature spin and invert every comparison (AnteToggle's lesson).
            st = await _feature_state(ctx)
            if st.get("premium"):
                ctx.mark("a Premium Reel was left ON by an earlier run -- switching it off first")
                await switch_feature(ctx, f"premium:{st.get('premiumId')}", False)
            if st.get("ante"):
                ctx.mark("ANTE was left ON by an earlier run -- switching it off first")
                await switch_feature(ctx, "ante", False)

            ctx.mark("PLAIN SPIN: no package")
            plain, rounds = await spin_and_finish(ctx)
            plain_base = GAME.base_stake(plain.raw or {})

            tested: list = []
            first_spin = None
            for opt in wanted:
                rid, mult = opt.get("reelId"), opt.get("multiplier") or 0
                feature = f"premium:{rid}"
                ctx.mark(f"PREMIUM REEL {rid} (x{mult}): switch ON")
                sw = await switch_feature(ctx, feature, True)
                if not sw["switched"]:
                    # Spinning now would grade a plain spin as a premium one. Record it and move on.
                    tested.append({"reel_id": rid, "multiplier": mult, "engaged": False})
                    continue
                st = await _feature_state(ctx)
                # What a package EXCLUDES, probed while it is on: instant reads, never a wait on a
                # control that should be locked.
                panel_open = await ctx.has_button("buy_bonus")
                ante_why = await ctx.ui_read(
                    "() => { const b = window.__gameTestBridge;"
                    " return b && b.canDo ? b.canDo('activateAnteBet') : null; }") or {}
                ctx.mark(f"PREMIUM REEL {rid}: spin")
                spin, chain = await spin_and_finish(ctx)
                rounds += chain
                first_spin = first_spin or spin
                raw = spin.raw or {}
                ctx.mark(f"PREMIUM REEL {rid}: switch OFF with one tap")
                off = await switch_feature(ctx, feature, False)
                tested.append({"reel_id": rid, "multiplier": mult, "engaged": True,
                               "spin_cost": st.get("spinCost"), "bet": st.get("bet") or 0,
                               "stake": spin.stake, "base": GAME.base_stake(raw),
                               "echoed_id": GAME.premium_reel_id(raw),
                               "echoed_multiplier": GAME.premium_reel_multiplier(raw),
                               "panel_open_while_on": panel_open,
                               "ante_allowed_while_on": bool(ante_why.get("ok")),
                               "ante_block_reason": ante_why.get("reason"),
                               "switched_off": off["switched"],
                               "bonus_rounds": len(chain) - 1})

            ctx.mark("PLAIN SPIN again: every package switched off")
            after, chain = await spin_and_finish(ctx)
            rounds += chain
            araw = after.raw or {}
            on = [t for t in tested if t["engaged"]]
            facts.update({
                "packages_tested": len(on),
                "reel_ids_tested": [t["reel_id"] for t in on],
                "multipliers_tested": [t["multiplier"] for t in on],
                "stakes": [t["stake"] for t in on],
                "all_engaged": len(on) == len(wanted),
                # Every rollup is False on an empty list: "no package switched on" must never read
                # as "every package passed".
                "all_stakes_match": bool(on) and all(_near(t["stake"], t["base"] * t["multiplier"])
                                                     for t in on),
                "all_ids_echoed": bool(on) and all(t["echoed_id"] == t["reel_id"] for t in on),
                "all_multipliers_echoed": bool(on) and all(
                    _near(t["echoed_multiplier"], t["multiplier"], 1e-9) for t in on),
                "all_bases_unchanged": bool(on) and all(_near(t["base"], plain_base) for t in on),
                "all_spin_costs_match": bool(on) and all(_near(t["spin_cost"], t["bet"] * t["multiplier"])
                                                         for t in on),
                "panel_blocked_while_on": bool(on) and not any(t["panel_open_while_on"] for t in on),
                "ante_blocked_while_on": bool(on) and not any(t["ante_allowed_while_on"] for t in on),
                "ante_block_reasons": sorted({str(t["ante_block_reason"]) for t in on}),
                "all_switched_off": bool(on) and all(t["switched_off"] for t in on),
                "distinct_ids_played": len({t["echoed_id"] for t in on}),
                "plain_stake": plain.stake,
                "plain_base": plain_base,
                "after_stake": after.stake,
                "after_premium_id": GAME.premium_reel_id(araw),
                "off_spin_plain": (GAME.premium_reel_id(araw) == 0
                                   and _near(after.stake, GAME.base_stake(araw))),
                "bonus_rounds": sum(t["bonus_rounds"] for t in on),
            })
            # Leave no package on for the next run -- only now, with every fact taken (Rule 14).
            st = await _feature_state(ctx)
            if st.get("premium"):
                ctx.mark("CLEANUP: a package is still ON -- switching it off for the next run")
                await switch_feature(ctx, f"premium:{st.get('premiumId')}", False)
            return ScenarioResult(self.name, True, first_spin or plain, rounds, ctx.timeline,
                                  ctx.events, (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks={"plain": plain.seq, "after": after.seq}, facts=facts)
        except Unreached as e:
            await ctx.dismiss_popups()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))


# ---------------------------------------------------------------------------
# Chaos Runner — line-game scenarios (CR_044-063, CR_014-017, CR_098-107)
#
# Written to docs/scenario-authoring-rules.md. What that means in practice here:
#   * every spin goes through `spin_and_finish()` / `finish_round()`, so a free-spin trigger is
#     played out INSIDE each loop body rather than left to hang the next iteration (Rule 1);
#   * bonus drains end on `is_terminal()`, never on the counter alone (Rule 2);
#   * controls are addressed by ROLE, so the profile maps them to this build's names (`ante` ->
#     `anteBet`, `stake_up` -> `betPlus`) and a control this build lacks is an instant False
#     rather than a burned timeout;
#   * `return-to-base` is NOT called from here — PREPARE/CLEANUP own that (docs/run-lifecycle.md).
#     Each scenario just leaves the game spinnable (Rule 5).
#
# The house rule from docs/scenario-assert-authoring.md applies throughout: every AI assert needs a
# HARD companion proving the run reached what the case claims to judge. So each scenario exposes the
# census it drove on as facts — `facts.wilds`, `facts.p1_ways`, `facts.trigger_scatters` — and the
# suites bind a `stream:` predicate to that fact before letting a `video:` grade the animation.
#
# The response contract these read is VERIFIED against six captured payloads: see
# games/chaos/bet-response.md. Two of its rules bite hardest here:
#   * `payout` is CAPPED at `maxWin`; `rawPayout` is the gross. Never compare a payout formula
#     against `payout` — a captured P1 round grossed 12 and credited 10.
#   * a SCATTER (P1) pays `multiplier x baseStake`, with NO `/betLines` divisor.
# ---------------------------------------------------------------------------
_GRID_UNREADABLE = ("the response carried no readable grid. The chaos profile reads "
                    "`result.result.views[].value` as numeric symbol ids (games/chaos/"
                    "bet-response.md §3); a round without one cannot be judged, so this reports "
                    "unreached rather than passing on an empty census.")


def _ante_state_js() -> str:
    """Whatever the bridge will tell us about the ante control right now. Best-effort: the contract
    lists no ante-specific getter, so the STAKE facts come from the RESPONSE and this is UI evidence
    only (`getSnapshot` is documented safe to call at any time and never throws)."""
    return ("() => { const b = window.__gameTestBridge; if (!b) return null;"
            " const s = b.getSnapshot ? b.getSnapshot() : {};"
            " const r = b.getButtonRect ? b.getButtonRect('anteBet') : null;"
            " return { present: !!r, visible: !!(r && r.visible), betAmount: s.betAmount,"
            "          balance: s.balance, spinCost: s.spinCost,"
            "          anteOn: s.isAnteBet === undefined ? null : !!s.isAnteBet }; }")


async def _hover_ante(ctx: "DriveContext", label: str, dwell_ms: int) -> bool:
    """Hover the ante control so its info panel (normal stake vs ante stake) is on screen, and shoot
    it. Returns whether the control was there to hover. A panel that only opens on hover cannot be
    reached with `click_button`, so this is a deliberate raw-pointer move against a rect the bridge
    gave us — the one place in this file that bypasses the role helpers."""
    rect = await ctx.ui_read("() => { const b = window.__gameTestBridge;"
                             " return b && b.getButtonRect ? b.getButtonRect('anteBet') : null; }")
    if not rect:
        return False
    ctx.mark(f"ANTE INFO PANEL: {label}")
    await ctx.page.mouse.move(rect["centerX"], rect["centerY"])
    await ctx.dwell(dwell_ms)
    await ctx.screenshot(label)
    return True


class AnteToggle(Scenario):    # CR_049 / 050 / 051 / 054 / 109 · DV-53 / 54 / 55 / 56
    name = "ante-toggle"
    description = ("Ante Bet in the base game: one spin with ante OFF, hover the control to raise "
                   "its info panel, switch ante ON, spin again at the SAME bet level, then switch "
                   "it back OFF.\n"
                   "The point of the case is the DIVERGENCE ante creates, and one play captures "
                   "both sides of it: `stake` (what the player pays) is multiplied, `baseStake` "
                   "(what payouts derive from) is not. facts.stake_off / stake_on / base_off / "
                   "base_on / ante_multiplier / ante_multiplier_setting (from gameInfo.result.settings) / "
                   "expected_ante_stake / stake_ratio / base_unchanged carry it exactly, and "
                   "`rounds` holds both spins so `ante_stake_multiplier` gets the ante/non-ante "
                   "pairing it needs to prove the base did not move.\n"
                   "Marks off/on; shots ante-off / ante-info-panel / ante-on. Unreached when this "
                   "build has no ante control.\n"
                   "Ante is switched through `switch_feature`, so a build that puts it behind the Buy "
                   "Bonus panel (Dragon VIP) runs the same play. There, while ante is ON, "
                   "facts.feature_panel_blocked_while_on / premium_blocked_while_on / "
                   "premium_block_reason probe what it excludes, and ante_toast_on_enable / "
                   "ante_toast_on_disable read the ante toast. `spin_after_off` spins once more after "
                   "ante goes back off: facts.stake_after_off / base_after_off / ante_off_spin_plain, "
                   "mark `after_off`.")

    def __init__(self, hover_ms: int = 1500, spin_after_off: bool = False):
        self.hover_ms = hover_ms
        self.spin_after_off = spin_after_off    # one more spin once ante is back off

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        if not GAME.HAS_ANTE_BET:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason=f"{GAME.name} has no Ante Bet feature (profile HAS_ANTE_BET=False)")
        if not await _ante_offered(ctx):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason="this session offers no Ante Bet (no ante control, or the "
                                         "snapshot's anteBetAvailable is false)")

        before = await ctx.ui_read(_ante_state_js(), {}) or {}

        # ENSURE ANTE IS ACTUALLY OFF FIRST. The whole case is a comparison between an ante-off and
        # an ante-on spin, and it used to just assume the control started off. A previous run that
        # left ante ON inverted the entire play: the "baseline" spin was the ante one (stake 12.00),
        # the "ANTE ON" click turned it OFF, and the second spin cost 4.00. Every derived fact came
        # out backwards -- stake_ratio 0.33 against a multiplier of 3 -- and the case failed as if
        # the GAME had applied ante the wrong way round. Observed live 2026-09-04.
        #
        # `isAnteBet` is UI evidence, not the contract, so this is best-effort: when the bridge does
        # not publish it (None) there is nothing to correct and the run proceeds as before.
        if before.get("anteOn"):
            ctx.mark("ANTE was left ON by an earlier run -- switching it off for the baseline")
            await switch_feature(ctx, "ante", False, dwell_ms=1000)
            await ctx.dismiss_popups()                  # the toggle raises its own notification
            before = await ctx.ui_read(_ante_state_js(), {}) or {}
            if before.get("anteOn"):
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()),
                                      reason="ante was ON at the start and would not switch off -- "
                                             "the baseline spin would be an ante spin and every "
                                             "comparison in this case would be inverted")

        ctx.mark("ANTE OFF: baseline spin")
        # spin_and_finish handles the trigger case for us: if this spin awards free spins the bonus
        # is played out to its terminal round before we carry on (Rule 1).
        off, off_rounds = await spin_and_finish(ctx)
        await ctx.screenshot("ante-off")

        hovered = await _hover_ante(ctx, "ante-info-panel", self.hover_ms)

        ctx.mark("ANTE ON")
        on_switch = await switch_feature(ctx, "ante", True, optional=False)
        await ctx.screenshot("ante-on")
        during = await ctx.ui_read(_ante_state_js(), {}) or {}
        # What ante EXCLUDES, probed while it is on -- instant reads, never a wait on a control that
        # should be locked. Only meaningful where ante shares the Buy Bonus panel (`ante_on`).
        panel_blocked = (not await ctx.has_button("buy_bonus")) if GAME.button("ante_on") else None
        # ⚠ Ask the SESSION, not the profile. A build can ship Premium Reel while the launch payload
        # does not offer it (`settings.premiumReel` null -> `premiumReelAvailable` false,
        # `getPremiumReelOptions()` []): every canDo then answers `feature_unavailable:premiumReels`,
        # which says nothing about ante's exclusion. `premium_offered` separates "not on this
        # session" from "ante blocked it", so a case can gate instead of reading None as a failure.
        # Dragon VIP dev, 2026-09-16: offered false, and DV-55 failed on `None == True`.
        premium_why, premium_offered = None, None
        if GAME.HAS_PREMIUM_REEL:
            premium = await ctx.ui_read(
                "() => { const b = window.__gameTestBridge; if (!b) return null;"
                " const s = b.getSnapshot ? b.getSnapshot() : {};"
                " const o = b.getPremiumReelOptions ? b.getPremiumReelOptions() : [];"
                " const offered = !!s.premiumReelAvailable && o.length > 0;"
                " return { offered: offered, ids: o.map(x => x.reelId),"
                "          why: (offered && b.canDo) ? b.canDo('activatePremiumReels:' + o[0].reelId) : null }; }",
                {}) or {}
            premium_offered = bool(premium.get("offered"))
            premium_why = premium.get("why")

        on, on_rounds = await spin_and_finish(ctx)

        ctx.mark("ANTE OFF again")
        off_switch = await switch_feature(ctx, "ante", False)   # leave the game as we found it
        await ctx.dismiss_popups()                          # the ante info popup, if it latched open
        after = await ctx.ui_read(_ante_state_js(), {}) or {}
        after_off, after_rounds = None, []
        if self.spin_after_off:
            ctx.mark("ANTE OFF: spin at the plain stake")
            after_off, after_rounds = await spin_and_finish(ctx)

        base_off, base_on = GAME.base_stake(off.raw or {}), GAME.base_stake(on.raw or {})
        mult = GAME.ante_multiplier(on.raw or {})
        # The multiplier the game was CONFIGURED with, from gameInfo.result.settings.anteMultiplier.
        # `mult` above is the one the ante ROUND reported, which is the right thing to grade the
        # stake against; this is the independent second opinion the AI is given so the clip can be
        # judged against the configuration rather than against a number derived from the same spin.
        # They should agree -- if they ever do not, that disagreement is itself the finding.
        setting_mult = await ctx.ui_read("""() => {
            const b = window.__gameTestBridge;
            const gi = b && b.getGameInfo ? b.getGameInfo() : null;
            const dig = (o, ks) => ks.reduce((a, k) => (a && a[k] != null) ? a[k] : null, o);
            const v = dig(gi, ['result', 'settings', 'anteMultiplier'])
                   ?? dig(gi, ['settings', 'anteMultiplier'])
                   ?? dig(gi, ['data', 'result', 'settings', 'anteMultiplier']);
            return (typeof v === 'number' && !isNaN(v)) ? v : null; }""", None)
        facts = {
            "stake_off": off.stake, "stake_on": on.stake,
            "base_off": base_off, "base_on": base_on,
            # None when the server reported no multiplier — a case must not then assert a ratio.
            "ante_multiplier": mult,
            # From gameInfo.result.settings — the CONFIGURED multiplier, read independently of the
            # round. None on a build that does not publish it; the prompt then falls back to
            # `ante_multiplier` and the two agree by construction.
            "ante_multiplier_setting": setting_mult,
            # What the bet SHOULD read on screen once ante is on: the plain stake times the
            # configured multiplier. Given to the AI so it checks the arithmetic against the
            # configuration, not just "the number went up".
            "expected_ante_stake": (round(off.stake * (setting_mult or mult), 2)
                                    if off.stake and (setting_mult or mult) else None),
            "ante_engaged": GAME.is_ante_round(on.raw or {}),
            # The OBSERVED ratio. A case compares this against the multiplier the SERVER reported
            # for that round, never against a hardcoded 3 — it is BO-configurable (CR_055).
            "stake_ratio": round(on.stake / off.stake, 6) if off.stake else None,
            "base_unchanged": abs(base_on - base_off) < 1e-6,
            "hovered": hovered,
            "bet_amount_off": before.get("betAmount"), "bet_amount_on": during.get("betAmount"),
            "bet_amount_restored": after.get("betAmount"),
            # Both spins may have dragged a bonus behind them; the case needs to know.
            "bonus_rounds": len(off_rounds) + len(on_rounds) - 2,
            "ante_switched_on": on_switch["switched"],
            "ante_switched_off": off_switch["switched"],
            "popups_on_enable": on_switch["popups"],
            "popups_on_disable": off_switch["popups"],
            # The ante toast, where the profile names one. None = no label to look for.
            "ante_toast_on_enable": (GAME.popup("ante_notify") in on_switch["popups"]
                                     if GAME.popup("ante_notify") else None),
            "ante_toast_on_disable": (GAME.popup("ante_notify") in off_switch["popups"]
                                      if GAME.popup("ante_notify") else None),
            "spin_cost_on": during.get("spinCost"),
            "feature_panel_blocked_while_on": panel_blocked,
            # Gate the two below on this. False = the SESSION ships no Premium Reel, so the refusal
            # is `feature_unavailable:premiumReels` and proves nothing about ante's exclusion.
            "premium_offered": premium_offered,
            "premium_blocked_while_on": (not premium_why.get("ok")) if premium_why else None,
            "premium_block_reason": (premium_why or {}).get("reason"),
            "stake_after_off": after_off.stake if after_off else None,
            "base_after_off": GAME.base_stake(after_off.raw or {}) if after_off else None,
            "ante_off_spin_plain": ((not GAME.is_ante_round(after_off.raw or {})
                                     and _near(after_off.stake, GAME.base_stake(after_off.raw or {})))
                                    if after_off else None),
        }
        return ScenarioResult(self.name, True, on, off_rounds + on_rounds + after_rounds,
                              ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(),
                              marks={"off": off.seq, "on": on.seq,
                                     **({"after_off": after_off.seq} if after_off else {})},
                              facts=facts, shots=ctx.shots)


class AnteFreeSpins(Scenario):    # CR_053 / 055 / 056 / 096
    name = "ante-freespins"
    description = ("Turn Ante Bet ON, spin until it triggers free spins, then play the whole bonus "
                   "out. Answers the two questions the ante cases ask about a RUNNING bonus:\n"
                   "  * is the ante control LOCKED while the feature plays "
                   "(facts.ante_clickable_in_fs — probed instantly with getButtonRect, never waited "
                   "on, because waiting on a deliberately disabled control burns the whole timeout "
                   "inside the bonus);\n"
                   "  * is the ante multiplier FROZEN at its trigger value (facts.ante_frozen).\n"
                   "⚠ A free spin costs nothing, so the server may report `anteMultiplier: 0` on "
                   "every one — in which case the frozen rule is NOT answerable from the response "
                   "and facts.ante_verifiable_in_fs is False. A case must gate on that before "
                   "asserting ante_frozen, or 'no data' reads as 'frozen'. The ante-info panel is "
                   "shot mid-bonus (ante-info-in-fs) as the fallback evidence CR_055 actually asks "
                   "for.\n"
                   "The ante toggle is SWITCHED, not blind-clicked: `isAnteBet` is read first, so a "
                   "game that already loaded with ante ON is left alone instead of being toggled OFF "
                   "into a bonus nobody paid ante for, and the end-of-run restore only undoes a click "
                   "this run actually made. facts.ante_on_at_start records the state found at load and "
                   "facts.ante_engaged_at_trigger says whether the TRIGGER round really carried the "
                   "ante bet — gate every ante claim on that one.\n"
                   "Exposes rounds = [trigger, ...free spins..., terminal] for the shared free-spin "
                   "invariants. The BO half of CR_055/056 — changing config mid-bonus and reloading "
                   "— is NOT driven here.")

    def __init__(self, max_base: int = 300):
        self.max_base = max_base

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        if not GAME.HAS_ANTE_BET or not await ctx.has_button("ante"):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), reason="no ante control on this build")

        # SWITCH ante on; do NOT blind-click it. `click_button` toggles, so an unconditional click
        # against a game that already had ante ON turns it OFF and plays the whole "ante free spins"
        # case with no ante bet at all: the stake stays at the base, `ante_at_trigger` comes back
        # None, and CR_055/056 go red as if the GAME had dropped the ante mid-bonus. Observed live
        # 2026-09-04 (run 154942 -- green toggle at load, black one second after our click, and both
        # base spins deducted 4.00 instead of the 12.00 the info panel quotes). AnteToggle already
        # carries the mirror of this guard for the same reason.
        #
        # `isAnteBet` is UI evidence, not the contract, so this is best-effort: a bridge that does
        # not publish it answers None, and the click then goes ahead exactly as it used to.
        state = await ctx.ui_read(_ante_state_js(), {}) or {}
        ante_on_at_start = state.get("anteOn")
        if ante_on_at_start:
            ctx.mark("ANTE was already ON at load -- leaving it on rather than toggling it OFF")
        else:
            ctx.mark("ANTE ON")
            await ctx.click_button("ante")
            await ctx.dismiss_popups()                  # the toggle raises its own notification
            state = await ctx.ui_read(_ante_state_js(), {}) or {}
            # Only a definite False is a failure. None means the bridge does not publish the flag,
            # which is the pre-existing blind-click behaviour and still worth running.
            if state.get("anteOn") is False:
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), round_windows=ctx.windows(),
                                      facts={"ante_on_at_start": ante_on_at_start},
                                      reason="the ante control would not switch ON -- every base "
                                             "spin would be a plain bet and the bonus would be "
                                             "graded as an ante case nobody paid ante for")
        # True only when WE turned it on, so the restore below cannot latch the game into ante-ON
        # and hand the next run the very state this guard exists to survive.
        restore_needed = not ante_on_at_start

        before = len(ctx.events)
        trigger, spins = None, 0
        for _ in range(self.max_base):
            b = await ctx.base_spin()
            spins += 1
            if is_freespin_trigger(b):
                trigger = b
                break
            await ctx.settle()
        if trigger is None:
            if restore_needed:
                await ctx.click_button("ante", optional=True)
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"base_spins": spins, "ante_on_at_start": ante_on_at_start},
                                  reason=f"no free-spin trigger in {spins} ante spins — force one "
                                         f"with a `paytable:` fixture rather than raising the budget")

        # Say what the ROUND reported, never a hardcoded "ante ON": the old literal text asserted a
        # state nobody had read and stood over a trigger bet at the plain stake.
        ctx.mark(f"FREE SPINS TRIGGERED on Bonus across "
                 f"{len(GAME.scatter_reels(trigger.raw or {}) or [])} reel(s), "
                 f"ante {'ON' if GAME.is_ante_round(trigger.raw or {}) else 'OFF'}")
        # Probe, never WAIT: an instant getButtonRect. This is the CR_053 evidence.
        ante_clickable = await ctx.has_button("ante")
        await _hover_ante(ctx, "ante-info-in-fs", 1500)

        # The bonus is the subject here, so drive it deliberately and keep every round.
        rounds = await play_out_freespins(ctx, trigger)

        fs = [b for b in rounds if b.phase == "freespin"]
        # Keep the Nones OUT of the drift set: `{None, None}` has one element and would otherwise
        # report a confident "frozen" on a stream that said nothing at all.
        reported = [m for m in (GAME.ante_multiplier(b.raw or {}) for b in fs) if m is not None]
        if restore_needed:                                  # restore only what WE changed
            await ctx.click_button("ante", optional=True)
        facts = {
            "base_spins": spins,
            "ante_on_at_start": ante_on_at_start,
            # Did the TRIGGER round actually carry the ante bet? `is_ante_round` falls back to
            # stake > baseStake, so it answers even on a build that omits `anteMultiplier`. Gate on
            # this: without it a run that never engaged ante grades as an ante case.
            "ante_engaged_at_trigger": GAME.is_ante_round(trigger.raw or {}),
            "paid_spins": base_rounds_since(ctx, before),
            "trigger_scatters": GAME.scatters(trigger.raw or {}),
            "trigger_reels": len(GAME.scatter_reels(trigger.raw or {}) or []),
            "expected_award": GAME.expected_fs_award(trigger.raw or {}),
            "awarded": trigger.total_free_spin,
            "free_spins_played": len(fs),
            "ante_clickable_in_fs": ante_clickable,
            "ante_at_trigger": GAME.ante_multiplier(trigger.raw or {}),
            "ante_multipliers": sorted(set(reported)),
            "ante_reported_in_fs": len(reported),
            # Whether the frozen rule can be answered from the RESPONSE at all. Gate on this.
            "ante_verifiable_in_fs": len(reported) >= 2,
            "ante_frozen": len(set(reported)) <= 1 if reported else None,
            "trigger_stake": trigger.stake,
            "fs_stakes": sorted({b.stake for b in fs}),
            "bonus_total": round(sum(b.payout for b in rounds), 4),
            "terminal_ok": is_terminal(rounds[-1]),
        }
        return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"trigger": trigger.seq, "terminal": rounds[-1].seq},
                              facts=facts, shots=ctx.shots)


class P1WaysWin(Scenario):    # CR_045 / 062 / 063
    name = "p1-ways-win"
    description = ("Symbol P1 (\"Fuel\", id 5): spin until a round pays a P1 WAYS WIN whose whole "
                   "payout is that win \u2014 P1 on reels 1, 3 and 5, and no payline win alongside it. "
                   "Needs the forced `chaos-p1-ways` paytable (~28% of spins qualify); on the real "
                   "strips this is ~1 in 1800 spins and the run would report inconclusive.\n"
                   "Why SCATTER-ONLY: the game shows one number, so on a round that also paid a line "
                   "the screen shows the SUM and an AI read of it proves nothing about either half. "
                   "When the P1 pay is the whole win the display is exactly "
                   "ways x 10 x baseStake, which is what makes the documented formula checkable "
                   "from the video.\n"
                   "Every round spun on the way is audited for P1 on a reel it must never occupy "
                   "(reels 2 and 4), free-spin rounds included.\n"
                   "Exposes facts.p1_ways / p1_reels / rounds_audited / paid_spins / illegal_reels; "
                   "the documented formula as symbol_multiplier / base_stake / p1_pay / line_pay / "
                   "formula_ok; and \u2014 because `payout` is CAPPED \u2014 both expected_gross vs "
                   "raw_payout AND expected_credited vs actual_payout. A case must not compare the "
                   "ways formula against the credited figure.")

    def __init__(self, max_spins: int = 40):
        self.max_spins = max_spins

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        before = len(ctx.events)
        win, audited, illegal, seen, p1_wins_seen = None, 0, [], [], 0
        for _ in range(self.max_spins):
            # One spin taken to a clean base; `chain` is [spin] or [trigger, ...bonus..., terminal].
            b, chain = await spin_and_finish(ctx)
            for r in chain:
                reels = GAME.reels_with(r.raw or {}, GAME.SYM_P1)
                if reels is None:
                    return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                          (start, ctx.now_s()), round_windows=ctx.windows(),
                                          reason=_GRID_UNREADABLE)
                audited += 1
                seen.extend(reels)
                bad = [x for x in reels if x not in GAME.P1_REELS]
                if bad:
                    illegal.append({"seq": r.seq, "phase": r.phase, "reels": bad})
                p1_ways_count = GAME.p1_ways(r.raw or {}) or 0
                if p1_ways_count > 0:
                    p1_wins_seen += 1
                    # Only a win with NO line pay alongside it will do (see the class docstring).
                    # `<= 1e-9`, not `== 0`: the line win is a product/quotient, so a zero can
                    # arrive as a float artefact.
                    if win is None and GAME.expected_line_win(r.raw or {}) <= 1e-9:
                        win = r
                        ctx.mark(f"P1 WAYS WIN: {p1_ways_count} way(s) on reels {reels}")
            if win is not None:
                break

        facts = {"rounds_audited": audited,
                 "paid_spins": base_rounds_since(ctx, before),
                 "illegal_reels": illegal,
                 "p1_reels_seen": sorted(set(seen)),
                 "allowed_reels": list(GAME.P1_REELS),
                 "p1_wins_seen": p1_wins_seen}
        if win is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason=f"no scatter-only P1 ways win in {audited} round(s) "
                                         f"({p1_wins_seen} P1 win(s) seen, all with a line win "
                                         f"alongside). Check the run declares "
                                         f"`paytable: {{ ref: chaos-p1-ways }}`, then raise max_spins")

        raw = win.raw or {}
        p1_ways_count, base_stake = GAME.p1_ways(raw), GAME.base_stake(raw)
        cap = win.max_win if win.max_win is not None else GAME.max_win_cap(raw)
        left = GAME.max_win_left(raw, cap)            # the cap counts the whole round
        gross = raw.get(GAME.F_RAW_PAYOUT)
        gross = float(gross) if gross is not None else None
        facts.update({"p1_ways": p1_ways_count,
                      "p1_reels": GAME.reels_with(raw, GAME.SYM_P1),
                      "base_stake": base_stake,
                      "scatter_multiplier": GAME.scatter_multiplier(raw),
                      # GROSS, before the cap \u2014 this is what the ways formula predicts.
                      "expected_gross": round(p1_ways_count * GAME.P1_MULTIPLIER * base_stake, 6),
                      "raw_payout": gross,
                      # CREDITED, after the cap. These DIVERGE on a capped round.
                      "actual_payout": win.payout,
                      "expected_credited": GAME.expected_payout(raw),
                      "max_win": cap,
                      "capped": bool(gross is not None and left is not None and gross > left + 1e-9),
                      # The DOCUMENTED formula, split out so a case can check the figure the player
                      # actually sees: design doc \u00a74 gives
                      #   Total Payout = Total Ways x Symbol Multiplier x Bet base
                      # `p1_pay` is that product; `line_pay` is anything the paylines added on top
                      # (0 here by construction, and asserted by the suite so it stays that way).
                      "symbol_multiplier": GAME.P1_MULTIPLIER,
                      "p1_pay": round(GAME.expected_scatter_win(raw), 6),
                      "line_pay": round(GAME.expected_line_win(raw), 6),
                      "scatter_only": GAME.expected_line_win(raw) <= 1e-9,
                      # Computed HERE, not in the suite: an inline `stream:` predicate is eval'd
                      # without builtins, so it has no `abs()` and cannot compare floats safely
                      # (2 x 10.0 x 0.6 is 12.000000000000002, which != 12.0).
                      "formula_ok": abs(GAME.expected_scatter_win(raw)
                                        - (p1_ways_count or 0) * GAME.P1_MULTIPLIER
                                        * (base_stake or 0)) < 1e-6})
        return ScenarioResult(self.name, True, win, [win], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks={"win": win.seq}, facts=facts, shots=ctx.shots)


class RainingWild(Scenario):    # CR_059 / 060 / 061
    name = "raining-wild"
    description = ("Raining Wild: spin until a result carries 2+ Wilds (id 9) — the documented 100% "
                   "trigger — and capture that spin's animation window. Also records a CONTRASTING "
                   "spin with fewer than 2 Wilds (0%, the animation must NOT play), so the rule can "
                   "be judged both ways instead of only confirming the positive. Without the "
                   "negative, an implementation that played the animation on EVERY spin would pass.\n"
                   "The animation is VISUAL ONLY (game-knowledge.md §4): it must never change the "
                   "outcome, so facts.payout_at_rain is exposed for a conservation check rather than "
                   "any claim that the effect paid something.\n"
                   "Exposes facts.wilds / wild_reels / expected_raining / control_wilds; marks rain "
                   "and norain.")

    def __init__(self, max_spins: int = 200):
        self.max_spins = max_spins

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        rain = norain = None
        for _ in range(self.max_spins):
            b, chain = await spin_and_finish(ctx)
            n = GAME.wilds(b.raw or {})
            if n is None:
                return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                      (start, ctx.now_s()), round_windows=ctx.windows(),
                                      reason=_GRID_UNREADABLE)
            if n >= GAME.RAINING_WILD_MIN_WILDS and rain is None:
                rain = b
                ctx.mark(f"RAINING WILD expected: {n} Wild(s) in the result")
            elif n < GAME.RAINING_WILD_MIN_WILDS and norain is None:
                norain = b
                ctx.mark(f"NO RAINING WILD expected: only {n} Wild(s)")
            if rain is not None and norain is not None:
                break
        if rain is None:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  facts={"control_wilds": GAME.wilds(norain.raw or {}) if norain else None,
                                         "has_control": norain is not None},
                                  reason=f"no result with {GAME.RAINING_WILD_MIN_WILDS}+ Wilds in "
                                         f"{self.max_spins} spins — force one with a `paytable:` "
                                         f"fixture. Note the ante distribution list raises the Wild "
                                         f"weight 5-10x, so ante ON reaches this much faster.")
        rounds = [b for b in (norain, rain) if b is not None]
        facts = {"wilds": GAME.wilds(rain.raw or {}),
                 "wild_reels": GAME.reels_with(rain.raw or {}, GAME.SYM_WILD),
                 "expected_raining": GAME.raining_wild_expected(rain.raw or {}),
                 "min_wilds": GAME.RAINING_WILD_MIN_WILDS,
                 "payout_at_rain": rain.payout,
                 "scatters_at_rain": GAME.scatters(rain.raw or {}),
                 "control_wilds": GAME.wilds(norain.raw or {}) if norain else None,
                 "has_control": norain is not None}
        marks = {"rain": rain.seq}
        if norain is not None:
            marks["norain"] = norain.seq
        return ScenarioResult(self.name, True, rain, rounds, ctx.timeline, ctx.events,
                              (start, ctx.now_s()), round_windows=ctx.windows(),
                              marks=marks, facts=facts)


class RandomMultiplier(Scenario):    # CR_057 / 058
    name = "random-multiplier"
    description = ("The random multiplier ladder (x1/x2/x5/x10) across BOTH modes: sample the base "
                   "game, then reach a bonus and sample every free spin in it. The BO export gives "
                   "the weights — x10 is weight 5 in the base game but 40 in free spins — so an "
                   "all-x1 bonus is unusual rather than normal.\n"
                   "Also reads the configured ladder straight from the bridge "
                   "(getMultiplierLevels) as facts.configured_ladder, so 'the values shown are the "
                   "values configured' is an EXACT check rather than an AI read of the on-screen "
                   "bar. Exposes facts.base_multipliers / fs_multipliers / all_in_ladder / "
                   "reached_freespins.\n"
                   "`base_spins` is the MINIMUM number of base rounds sampled: the run cannot choose "
                   "which spin triggers, so when the bonus arrives first it goes back to the base "
                   "game afterwards until the count is met. facts.base_spins counts every base "
                   "round, base_spins_after_bonus the ones played after it; fs_spins counts every "
                   "free spin played and fs_multiplier_count the ones that reported a multiplier.")

    def __init__(self, base_spins: int = 3, max_base: int = 40):
        # MINIMUM base rounds in the sample. A trigger cannot be put off, so when it arrives
        # before this many base rounds the run plays more base spins AFTER the bonus. `max_base`
        # caps the paid spins either way.
        self.base_spins = base_spins
        self.max_base = max_base

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        configured = await ctx.ui_read(
            "() => { const b = window.__gameTestBridge;"
            " return b && b.getMultiplierLevels ? b.getMultiplierLevels() : null; }")

        ctx.mark("BASE GAME multiplier sampling")
        rounds, trigger = [], None
        base_before = None              # base rounds played before the bonus

        def base_count() -> int:
            return sum(1 for x in rounds if x.phase in ("base", "auto"))

        # One loop, one budget: sample the base game until a bonus has turned up AND at least
        # `base_spins` base rounds are in. spin_and_finish returns the whole chain, so a trigger
        # contributes its free spins to the sample instead of being handled separately.
        for _ in range(self.max_base):
            b, chain = await spin_and_finish(ctx)
            rounds.extend(chain)
            if trigger is None and is_freespin_trigger(b):
                trigger = b
                base_before = base_count()
                ctx.mark("FREE SPINS: multiplier sampling continues inside the bonus")
                if base_before < self.base_spins:
                    ctx.mark(f"BASE GAME again: {self.base_spins - base_before} more base spin(s) "
                             f"to reach {self.base_spins}")
            if trigger is not None and base_count() >= self.base_spins:
                break
        if base_before is None:
            base_before = base_count()

        def mults(bs):
            return [m for m in (GAME.multiplier(x.raw or {}) for x in bs) if m is not None]

        base_m = mults([b for b in rounds if b.phase in ("base", "auto")])
        fs_m = mults([b for b in rounds if b.phase == "freespin"])
        ladder = [float(x) for x in GAME.MULTIPLIER_VALUES]
        observed = base_m + fs_m
        distinct = sorted(set(observed))
        facts = {"base_multipliers": base_m, "fs_multipliers": fs_m,
                 "ladder": list(GAME.MULTIPLIER_VALUES),
                 "configured_ladder": (configured or {}).get("values") if configured else None,
                 "base_spins": base_count(),
                 "base_spins_after_bonus": base_count() - base_before,
                 # every free spin PLAYED (what the prompt describes), and separately the ones that
                 # reported a multiplier (what "it appears in free spins" needs)
                 "fs_spins": len([b for b in rounds if b.phase == "freespin"]),
                 "fs_multiplier_count": len(fs_m),
                 "reported_any": bool(observed),
                 "all_in_ladder": bool(observed) and all(m in ladder for m in observed),
                 "reached_freespins": trigger is not None,
                 # "the multiplier is RANDOM, not fixed" -- the ORACLE half. The response says
                 # whether this run actually varied; the AI is then asked whether the SCREEN agrees,
                 # graded against `varied` rather than against a hardcoded true. It has to work that
                 # way: x1 carries 80% of the base weight and 65% of the free-spin weight, so three
                 # base spins come back all-x1 about half the time on a perfectly correct game.
                 "multipliers_seen": distinct,
                 "distinct_multipliers": len(distinct),
                 "varied": len(distinct) > 1}
        if not observed:
            # Distinguish "the game never showed one" from "we cannot read it": the latter is a
            # profile gap, and calling it a pass would certify a field nobody has verified.
            return ScenarioResult(self.name, False, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(), facts=facts,
                                  reason="no round reported `randomMultiplier` — the field is "
                                         "documented in games/chaos/bet-response.md §2; if it is "
                                         "genuinely absent the profile needs correcting")
        marks = {"trigger": trigger.seq} if trigger else {}
        return ScenarioResult(self.name, True, trigger or (rounds[-1] if rounds else None), rounds,
                              ctx.timeline, ctx.events, (start, ctx.now_s()),
                              round_windows=ctx.windows(), marks=marks, facts=facts)


class PaytableBetScaling(Scenario):    # CR_014 / 015 / 016 / 017 / 070
    name = "paytable-bet-scaling"
    description = ("The paytable popup and its bet scaling: open the info/paytable surface and shoot "
                   "it at bet level A, close, change the bet to level B, reopen and shoot it again. "
                   "Two full-resolution stills of the SAME table at two bets is what an `image:` "
                   "assert needs to check that the printed payouts moved with the bet (CR_015) — a "
                   "video assert grades a downsampled montage, where paytable digits are not in the "
                   "pixels.\n"
                   "Places NO bet, so Rule 1 does not apply: no response is read and no trigger is "
                   "possible. Exposes facts.bet_a / bet_b (the bet the GAME showed after each "
                   "pick, read back -- unreached when it is not the bet asked for) / bet_ratio "
                   "(bet_b / bet_a, for an image assert's `compare_shots`) / line_bet_a / line_bet_b "
                   "/ divisor and the shots paytable-bet-a / paytable-bet-b.\n"
                   "Per-line bet is `baseStake / betLines` (20). The design doc's `/5` worked "
                   "examples are wrong — see game-knowledge.md §5 item 4.")

    def __init__(self, index_a: int = 0, index_b: int = 3):
        self.index_a = index_a
        self.index_b = index_b

    async def _pick_bet(self, ctx: DriveContext, index: int) -> tuple:
        """Select denomination `index`. Returns (opened, bet): whether the list actually opened —
        `open_bet_list` checks `visiblePopups` rather than assuming the click landed — and the bet
        the game shows AFTERWARDS, read back. The row click is optional, so the read-back is what
        proves it landed: without it a missed click left both shots at one bet while the facts
        still named two."""
        if not await open_bet_list(ctx):
            return False, None
        await ctx.scroll_bet_row(index, dwell_ms=0)
        # Templated role: resolve it WITH the index first — click_button takes a finished name.
        await ctx.click_button(GAME.button("bet_row", i=index), optional=True, timeout_ms=15000)
        # Same stall risk as browse-denominations: a close that silently does nothing leaves the
        # popup up, and the next settle then waits its whole timeout. close_bet_list proves it went.
        await close_bet_list(ctx)
        bet = await ctx.eval("() => { const b = window.__gameTestBridge;"
                             " const s = b.getSnapshot ? b.getSnapshot() : null;"
                             " return b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null); }")
        return True, bet

    async def _shoot_paytable(self, ctx: DriveContext, label: str) -> bool:
        if not await ctx.click_button("info", optional=True, timeout_ms=10000):
            return False
        await ctx.screenshot(label)
        await ctx.dismiss_popups()
        return True

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        denoms = await ctx.eval("() => { const b = window.__gameTestBridge;"
                                " const s = b.getSnapshot ? b.getSnapshot() : null;"
                                " return s ? s.denominations : null; }") or []
        if len(denoms) < 2:
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()),
                                  reason="fewer than two denominations — cannot compare two bet levels")
        a = min(self.index_a, len(denoms) - 1)
        b_i = min(self.index_b, len(denoms) - 1)
        if a == b_i:
            b_i = (a + 1) % len(denoms)

        ctx.mark(f"BET LEVEL A = {denoms[a]}")
        opened_a, shown_a = await self._pick_bet(ctx, a)
        shot_a = await self._shoot_paytable(ctx, "paytable-bet-a")

        ctx.mark(f"BET LEVEL B = {denoms[b_i]}")
        opened_b, shown_b = await self._pick_bet(ctx, b_i)
        shot_b = await self._shoot_paytable(ctx, "paytable-bet-b")

        def _landed(shown, want) -> bool:
            try:
                return shown is not None and abs(float(shown) - float(want)) < 1e-9
            except (TypeError, ValueError):
                return False

        if not (opened_a and opened_b):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), shots=ctx.shots,
                                  reason="the denomination list never opened — cannot compare two "
                                         "bet levels (open_bet_list checked visiblePopups)")
        if not (shot_a and shot_b):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), shots=ctx.shots,
                                  reason="the info / paytable surface never opened on this build")
        if not (_landed(shown_a, denoms[a]) and _landed(shown_b, denoms[b_i])):
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), shots=ctx.shots,
                                  facts={"bet_a": shown_a, "bet_b": shown_b,
                                         "bet_a_asked": denoms[a], "bet_b_asked": denoms[b_i]},
                                  reason=f"the bet did not change as asked: picked {denoms[a]} then "
                                         f"{denoms[b_i]}, the game showed {shown_a} then {shown_b} "
                                         f"— the two paytable shots are not at the bets they claim")
        facts = {"bet_a": shown_a, "bet_b": shown_b,
                 "bet_ratio": round(float(shown_b) / float(shown_a), 6) if float(shown_a) else None,
                 "index_a": a, "index_b": b_i,
                 "divisor": GAME.LINE_DIVISOR or GAME.PAYLINES,   # the order line_bet() divides by
                 "line_bet_a": GAME.line_bet(float(denoms[a])),
                 "line_bet_b": GAME.line_bet(float(denoms[b_i])),
                 "denom_count": len(denoms)}
        return ScenarioResult(self.name, True, None, [], ctx.timeline, ctx.events,
                              (start, ctx.now_s()), facts=facts, shots=ctx.shots)




class BuyBonusLuckySpin(Scenario):    # Joker JK-63..JK-67 (games/joker/test-suites/jk-features.yml)
    name = "buy-bonus-lucky-spin"
    description = ("Buy the bonus, answer the LUCKY SPIN WHEEL, and play the bought free spins out. "
                   "The game-specific half of Buy Bonus on Joker: the purchase awards the spins AND "
                   "raises a wheel offering x1 / x3 / x12, and the bridge never makes that choice "
                   "(bridge-api.md 12.5). `dismissPopups()` deliberately never closes `luckySpin`, "
                   "so a scenario that buys and walks away leaves the game waiting forever -- which "
                   "is why `buy-bonus` cannot be used here and this exists.\n"
                   "`wheel`: 'keep' takes the multiplier already shown (the default, x3), 'spin' "
                   "spins for a new one. Either way the choice is CLICKED, never dismissed.\n"
                   "Sequence: buyBonus -> [buy_level] -> buy_purchase -> buy_confirm -> the purchase "
                   "round (carries `freeSpinPackage`) -> wait for the wheel -> keep/spin -> drain the "
                   "free spins -> settle to base.\n"
                   "Exposes price / price_ratio / paid_stake / package_spins, the wheel "
                   "(wheel_shown, wheel_choice, lucky_multiplier), and the bonus (fs_rounds, "
                   "bonus_total, expanded_rounds, expanded_reels, capped).")

    def __init__(self, wheel: str = "keep", level: int | None = None, respin_cap: int = 120,
                 wheel_timeout_ms: int = 30000):
        self.wheel = wheel          # 'keep' | 'spin'
        self.level = level          # buyBonus:level:<i> shortcut, or None to take the panel's stake
        self.respin_cap = respin_cap
        self.wheel_timeout_ms = wheel_timeout_ms

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}
        try:
            if not GAME.HAS_BUY_BONUS:
                raise Unreached("this build has no Buy Bonus")
            btn_buy, btn_purchase, btn_confirm = require_buttons(
                "buy_bonus", "buy_purchase", "buy_confirm")

            ctx.mark("BUY BONUS: open the panel")
            if not await ctx.click_button(btn_buy, optional=True, timeout_ms=15000):
                raise Unreached(f"`{btn_buy}` never became clickable")
            if self.level is not None:
                await ctx.click_button(GAME.button("buy_level", i=self.level), optional=True,
                                       timeout_ms=8000)
            info = await ctx.eval("""() => { const b = window.__gameTestBridge;
                const s = b.getSnapshot ? b.getSnapshot() : {};
                return { price: s.buyBonusSelectedPrice, bet: s.betAmount,
                         balance: s.balance }; }""")
            price, bet = info.get("price"), info.get("bet")
            facts.update({"price": price, "bet": bet,
                          "price_ratio": (float(price) / float(bet)) if (price and bet) else None,
                          "balance_before": info.get("balance")})

            ctx.mark(f"BUY BONUS: purchase at {price} (bet {bet})")
            await ctx.click_button(btn_purchase, optional=True, timeout_ms=10000)
            if not await ctx.click_button(btn_confirm, optional=True, timeout_ms=10000):
                raise Unreached("the Buy Bonus confirm sheet never offered `buy_confirm`")

            bought = await ctx.observe_bet(timeout_ms=ctx.bet_timeout_ms, phase="base")
            pkg = (bought.raw or {}).get("freeSpinPackage") or {}
            facts.update({"paid_stake": bought.stake, "package_spins": pkg.get("freeSpins"),
                          "package_name": pkg.get("name"), "awarded": bought.total_free_spin})

            # ── the wheel. THE STEP THAT MUST NOT BE SKIPPED ───────────────────────────────
            label = GAME.popup("lucky_spin")
            shown = await ctx.wait_for_popup(label, self.wheel_timeout_ms) if label else False
            facts.update({"wheel_shown": bool(shown)})
            if shown:
                role = "lucky_spin" if self.wheel == "spin" else "lucky_keep"
                ctx.mark(f"LUCKY SPIN: choosing {self.wheel}")
                facts.update({"wheel_choice": self.wheel})
                if not await ctx.click_button(GAME.button(role), optional=True, timeout_ms=10000):
                    raise Unreached(f"the Lucky Spin wheel offered no `{role}` -- the game is now "
                                    f"waiting on a choice nobody can make")
            else:
                # Not fatal: report it. A build that skips the wheel still owes the free spins.
                facts.update({"wheel_choice": None})
                ctx.mark("LUCKY SPIN: no wheel appeared within the timeout -- continuing to the spins")

            rounds = [bought]
            fs = await play_out_bonus(ctx, bought, cap=self.respin_cap, settle=False)
            rounds.extend(fs[1:])
            spins = [r for r in rounds[1:]]
            expanded = [(r.seq, GAME.expanded_reels(r.raw or {})) for r in spins
                        if GAME.expanded_reels(r.raw or {})]
            mults = {GAME.lucky_multiplier(r.raw or {}) for r in spins} - {None}
            facts.update({
                "fs_rounds": len(spins),
                "bonus_total": round(sum(r.payout for r in spins), 4),
                "capped": any(r.has_max_win for r in rounds),
                "expanded_rounds": len(expanded),
                "expanded_reels": sorted({rid for _, ids in expanded for rid in ids}),
                "lucky_multiplier": (sorted(mults)[0] if len(mults) == 1 else sorted(mults)),
                "multiplier_constant": len(mults) <= 1,
            })
            stop = await settle_to_base(ctx, label="buy-bonus-lucky-spin")
            facts.update({**stop})
            return ScenarioResult(self.name, True, bought, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks={"bought": bought.seq,
                                         "terminal": rounds[-1].seq if rounds else bought.seq},
                                  facts=facts, shots=ctx.shots)
        except Unreached as e:
            await ctx.dismiss_popups()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))

# ---------------------------------------------------------------------------
# In-page watchers. A presentation is made of things that are only true for a few frames -- a wild
# mid-walk, a stand-in symbol about to be knocked off, a control that flickers enabled during a
# reload. Polling them from Python cannot see any of that: every `await` is a gap, and the gaps are
# where the evidence lives. So the sampling runs INSIDE the page on a 50 ms timer and Python reads
# the trace afterwards -- one round trip instead of hundreds, and nothing missed between awaits.
# ---------------------------------------------------------------------------
_BUTTON_WATCH_NAMES = ("stake_up", "stake_down", "auto", "settings", "buy_bonus")

_BUTTON_WATCH_JS = """(names) => {
    const b = window.__gameTestBridge;
    window.__fsWatch = { polls: 0, enabled: {} };
    if (window.__fsWatchTimer) clearInterval(window.__fsWatchTimer);
    window.__fsWatchTimer = setInterval(() => {
        const w = window.__fsWatch; if (!w || !b || !b.getButtonRect) return;
        w.polls += 1;
        for (const n of names) {
            let r = null;
            try { r = b.getButtonRect(n); } catch (e) { r = null; }
            if (r && r.interactable && r.visible) w.enabled[n] = (w.enabled[n] || 0) + 1;
        }
    }, 50);
    return true;
}"""

_BUTTON_WATCH_READ_JS = """() => {
    if (window.__fsWatchTimer) { clearInterval(window.__fsWatchTimer); window.__fsWatchTimer = null; }
    const w = window.__fsWatch || { polls: 0, enabled: {} };
    return { polls: w.polls, enabled: w.enabled };
}"""

_PRESENTATION_WATCH_JS = """(opts) => {
    const b = window.__gameTestBridge;
    window.__pWatch = [];
    if (window.__pWatchTimer) clearInterval(window.__pWatchTimer);
    window.__pWatchTimer = setInterval(() => {
        if (!b || !b.getSnapshot) return;
        let s = {}; try { s = b.getSnapshot() || {}; } catch (e) { return; }
        const row = { t: Date.now(), busy: (s.busyOps || []).slice() };
        if (opts.wilds && b.getWalkingWildState) {
            try { const w = b.getWalkingWildState() || {};
                  row.wilds = w.onGrid; row.cells = w.cells || []; row.animating = !!w.animating;
            } catch (e) {}
        }
        if (opts.grid && b.getDisplayedGrid) {
            try { row.grid = b.getDisplayedGrid(); } catch (e) {}
        }
        window.__pWatch.push(row);
        if (window.__pWatch.length > 4000) window.__pWatch.shift();   // bounded, never unbounded
    }, 50);
    return true;
}"""

_PRESENTATION_READ_JS = """(stop) => {
    const rows = window.__pWatch || [];
    window.__pWatch = [];
    if (stop && window.__pWatchTimer) { clearInterval(window.__pWatchTimer); window.__pWatchTimer = null; }
    return rows;
}"""


async def watch_presentation(ctx: DriveContext, grid: bool = False, wilds: bool = False) -> None:
    """Start sampling `busyOps` (and optionally the displayed grid / walking wilds) every 50 ms in
    the page. Read it back with `take_presentation_trace`."""
    await ctx.page.evaluate(_PRESENTATION_WATCH_JS, {"grid": grid, "wilds": wilds})


async def take_presentation_trace(ctx: DriveContext, stop: bool = False) -> list:
    """Every sample since the last take, oldest first — and clears the buffer, so consecutive calls
    slice the recording per round. `stop=True` also stops the timer."""
    return await ctx.page.evaluate(_PRESENTATION_READ_JS, stop) or []


def _busy_spans(trace: list, op: str) -> list:
    """(start_ms, end_ms) for each continuous stretch where `op` was in `busyOps`."""
    spans, open_at, last_t = [], None, None
    for row in trace:
        busy, t = row.get("busy") or [], row.get("t")
        if op in busy and open_at is None:
            open_at = t
        elif op not in busy and open_at is not None:
            # Close at THIS sample, the first one where the op is gone -- it ended somewhere in
            # the 50 ms since the previous sample, and the upper bound is the honest estimate.
            # Closing at the previous sample instead reports a one-sample animation as 0 ms, which
            # reads as "it was skipped" — the exact failure `min_walk_ms` exists to catch.
            spans.append((open_at, t if t is not None else last_t))
            open_at = None
        last_t = t
    if open_at is not None and last_t is not None:
        spans.append((open_at, last_t))
    return spans


def _busy_ms(trace: list, op: str) -> int:
    return sum(max(0, e - s) for s, e in _busy_spans(trace, op))


def _cells_key(cells) -> set:
    """A walking-wild cell set as comparable `(row, col)` pairs, whatever shape it arrived in."""
    out = set()
    for c in cells or []:
        if isinstance(c, dict) and c.get("row") is not None and c.get("col") is not None:
            out.add((int(c["row"]), int(c["col"])))
    return out


def _grid_matches_view(displayed, raw: dict) -> bool:
    """Does the grid on screen equal the grid the server sent, cell for cell?

    `-1` cells are SKIPPED, never counted as equal or unequal: the bridge uses it for a cell that is
    hidden or masked, which is "cannot say" rather than a mismatch (Rule 19). A grid that is nothing
    but `-1` therefore compares equal to everything, so the caller checks it was sampled at all."""
    view = GAME.grid(raw)
    if view is None or not isinstance(displayed, list) or not displayed:
        return False
    if len(displayed) != len(view):
        return False
    for col, (shown_reel, real_reel) in enumerate(zip(displayed, view)):
        if not isinstance(shown_reel, list) or len(shown_reel) != len(real_reel):
            return False
        for row, (shown, real) in enumerate(zip(shown_reel, real_reel)):
            if str(shown) == "-1":
                continue
            if GAME._norm(shown) != GAME._norm(real):
                return False
    return True


class FreeSpinReload(Scenario):
    name = "freespin-reload"
    description = ("Reload the game in the MIDDLE of a bonus and prove it resumes where it left "
                   "off: reach a bonus (naturally, or by buying it with `source: buy`), play "
                   "`after_rounds` free spins, reload, and let the client play out what is still "
                   "owed. The resume signal is `gameInfo.pending`, read in the gap between the "
                   "bridge appearing and Start -- the only moment it exists, because the client "
                   "clears it the instant Start is clicked.\n"
                   "A 50ms watcher runs IN THE PAGE across the reload over the controls that must "
                   "stay disabled while spins are owed (bet +/-, autoplay, settings, buy bonus), "
                   "so a one-frame flash is caught instead of falling between two awaits.\n"
                   "Exposes facts.source / awarded / rounds_before_reload / free_spins_before_reload "
                   "/ pending_free_spins / pending_game_state / pending_is_bonus / resumed_rounds / "
                   "first_resumed_free_spins / buttons_polled / buttons_flashed / "
                   "buttons_ever_enabled / fs_rounds. Marks `trigger`, `reload`, `resume`, "
                   "`terminal`. Unreached if no bonus is reached, or if it ends before "
                   "`after_rounds` spins have played -- a reload with nothing owed proves nothing.")

    def __init__(self, source: str = "natural", after_rounds: int = 2,
                 max_base: int = 40, cap: int = 500, package: int = 0):
        self.source = source                # "natural" (spin until it triggers) | "buy"
        self.after_rounds = after_rounds    # free spins to play BEFORE reloading
        self.max_base = max_base
        self.cap = cap
        self.package = package

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}
        try:
            if self.source == "buy":
                trigger, buy_facts = await purchase_bonus(ctx, self.package)
                facts.update({"source": "buy", "price": buy_facts.get("price"),
                              "price_ratio": buy_facts.get("price_ratio")})
            else:
                trigger = None
                for _ in range(self.max_base):
                    b = await ctx.base_spin()
                    if is_freespin_trigger(b):
                        trigger = b
                        break
                if trigger is None:
                    raise Unreached(
                        f"no free-spin trigger in {self.max_base} base spins -- this scenario needs "
                        f"a forced paytable (Rule 17), not a longer hunt")
                facts.update({"source": "natural"})
            ctx.mark(f"BONUS OPEN: {trigger.won_free_spins} free spin(s) awarded")
            facts.update({"awarded": trigger.won_free_spins})

            played = []
            for _ in range(self.after_rounds):
                r = await ctx.free_spin()
                played.append(r)
                if is_terminal(r):
                    break
            last = played[-1] if played else trigger
            if is_terminal(last):
                # Reloading after the bonus has finished proves nothing: `pending` is empty and the
                # case reads as "resumed 0 of 0" -- a green on no evidence. Say so instead.
                raise Unreached(
                    f"the bonus ended after {len(played)} of {self.after_rounds} free spins "
                    f"(awarded {trigger.won_free_spins}) -- nothing would be left to resume. "
                    f"Lower `after_rounds`, or force a longer award.")
            facts.update({"rounds_before_reload": len(played),
                          "free_spins_before_reload": last.free_spins})

            ctx.mark(f"RELOAD with {last.free_spins} free spin(s) owed")
            pending: dict = {}
            watch_names = [GAME.button(r) for r in _BUTTON_WATCH_NAMES if GAME.button(r)]

            async def _on_ready() -> None:
                info = await read_game_info(ctx.page)
                p = GAME.pending(info)
                pending.update({
                    "free_spins": GAME.pending_free_spins(info),
                    "game_state": p.get(GAME.F_PENDING_GAME_STATE or GAME.F_GAME_STATE),
                    "is_bonus": GAME.pending_is_bonus(info)})
                await ctx.page.evaluate(_BUTTON_WATCH_JS, watch_names)

            resumed = await ctx.reload_and_start(on_ready=_on_ready)
            ctx.mark(f"RESUMED: {len(resumed)} round(s) played out by the client")
            # ⚠ NOT answered here. A resumed bonus drains inside `start_game` -> `drain_to_base`,
            # which answers the offer itself — it has to, because `assert_idle` runs in there too
            # and would otherwise burn its budget on the panel. Repeating it here would just spend
            # another 12 s waiting for a popup that is already gone.
            facts.update({"pending_free_spins": pending.get("free_spins"),
                          "pending_game_state": pending.get("game_state"),
                          "pending_is_bonus": pending.get("is_bonus"),
                          "resumed_rounds": len(resumed),
                          "first_resumed_free_spins": (resumed[0].free_spins if resumed else None)})

            rounds = [trigger, *played, *resumed]
            if resumed and not is_terminal(resumed[-1]):
                rounds += (await play_out_freespins(ctx, resumed[-1], cap=self.cap,
                                                    settle=False))[1:]

            watch = await ctx.ui_read(_BUTTON_WATCH_READ_JS, {}) or {}
            flashed = sorted((watch.get("enabled") or {}).keys())
            facts.update({"buttons_polled": int(watch.get("polls") or 0),
                          "buttons_flashed": flashed,
                          "buttons_ever_enabled": bool(flashed),
                          "fs_rounds": len([r for r in rounds if r.phase == "freespin"])})

            stop = await settle_to_base(ctx, label="freespin-reload")
            facts.update({**stop})
            return ScenarioResult(self.name, True, trigger, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks={"trigger": trigger.seq, "terminal": rounds[-1].seq,
                                         "reload": last.seq,
                                         "resume": (resumed[0].seq if resumed else last.seq)},
                                  facts=facts)
        except Unreached as e:
            await ctx.dismiss_popups()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))


class WalkingWild(Scenario):
    name = "walking-wild"
    description = ("Play a bonus and grade the WALKING WILD round by round: where each wild parked, "
                   "whether that matches the wilds the server sent, and whether the walk really "
                   "played rather than the wild appearing in place.\n"
                   "Base spins are taken BEFORE and AFTER the bonus on purpose -- 'a wild walks on "
                   "only during free spins' needs the base game in the same recording. ⚠ A wild "
                   "parked in a previous bonus may still STAND on the grid in base game until the "
                   "next spin walks it off (bridge §8.2), so what is graded is whether a walk "
                   "STARTS in a paid round, not whether the grid is empty.\n"
                   "Exposes facts.fs_rounds / wild_counts / wild_cells_match_view_rounds / "
                   "grid_matches_view_rounds / walk_started_in_base / fs_rounds_with_wilds / "
                   "multi_wild_rounds / more_wild_rounds / fewer_wild_rounds / moved_wild_rounds / "
                   "wild_change_rounds / walk_animation_rounds / walk_overlapped_spin_rounds / "
                   "stand_in_rounds / exit_animation_rounds / wild_wins_use_parked_cells / "
                   "walk_finished_before_next_round / min_walk_ms / speed_mode. Marks `base_before`, "
                   "`trigger`, `terminal`, `base_after`, and the rounds where wilds were ADDED "
                   "(`more_wilds`, also `new_wild`), REMOVED (`fewer_wilds`), MOVED (`moved_wild`) "
                   "and where several walked at once (`multi_wild`). LIST marks for `video: each:`: "
                   "`wild_enter_rounds` (every free spin a wild was added) and "
                   "`stand_in_kick_rounds` (every free spin with a stand-in at a wild's "
                   "destination).")

    def __init__(self, source: str = "buy", base_spins_before: int = 2, base_spins_after: int = 1,
                 max_base: int = 40, cap: int = 500, package: int = 0,
                 turbo: bool = False, sudden_stop: bool = False, max_bonuses: int = 1):
        # ⚠ WHY MORE THAN ONE BONUS. Three cases need a transition the SERVER decides: a wild
        # ARRIVING, one LEAVING, one MOVING. None is guaranteed in any single bonus -- a 15-spin
        # run measured 0 "moved" -- and a transition that never happened is not a defect, so
        # asserting it off one bonus reds a healthy game. This plays further bonuses until all
        # three have been seen, and `transitions_seen` says which ever did.
        self.source = source
        self.base_spins_before = base_spins_before
        self.base_spins_after = base_spins_after
        self.max_base = max_base
        self.cap = cap
        self.package = package
        self.turbo = turbo
        self.sudden_stop = sudden_stop
        self.max_bonuses = max(1, int(max_bonuses))

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}
        try:
            if not GAME.HAS_FREE_SPINS:
                raise Unreached("this build has no free spins")

            if self.turbo:
                btn_turbo = GAME.button("turbo")
                if not (btn_turbo and await ctx.click_button(btn_turbo, optional=True)):
                    raise Unreached("turbo was asked for but `turbo` never became clickable")
                ctx.mark("TURBO on")
            facts.update({"speed_mode": await ctx.ui_read(
                "() => { const b = window.__gameTestBridge;"
                " return b && b.getSpeedMode ? b.getSpeedMode() : 'UNKNOWN'; }", "UNKNOWN")})

            # ── CLEAN START: walk off any wild left parked by an EARLIER bonus ────────
            # Bridge §8.2: wilds parked in a previous bonus stay on the grid in base game until the
            # next spin walks them off. On 2026-10-01 this run began right after `freespin-reload`
            # with two wilds parked on reel 5; their walk-off played inside the measured base spins
            # and was counted as "a walk started in base" (TC-140 red). Those spins exist to show
            # that THIS run's base game starts no walk, so they must start from an empty grid.
            leftover_clear_spins = 0
            while leftover_clear_spins < 3:
                on_grid = await ctx.ui_read(
                    "() => { const b = window.__gameTestBridge;"
                    " const s = b && b.getWalkingWildState ? b.getWalkingWildState() : null;"
                    " return s ? (s.onGrid || 0) : 0; }", 0) or 0
                if not on_grid:
                    break
                ctx.mark(f"CLEAR LEFTOVER: {on_grid} wild(s) parked by an earlier bonus")
                b = await ctx.base_spin()
                if is_freespin_trigger(b):
                    await play_out_bonus(ctx, b)
                else:
                    await ctx.settle()
                leftover_clear_spins += 1
            facts.update({"leftover_clear_spins": leftover_clear_spins})

            # ── base game BEFORE: the negative half of "only in free spins" ───────────
            await watch_presentation(ctx, grid=True, wilds=True)
            base_before = []
            for _ in range(self.base_spins_before):
                b = await ctx.base_spin()
                if is_freespin_trigger(b):        # Rule 1: a paid spin can always trigger
                    await play_out_bonus(ctx, b)
                else:
                    await ctx.settle()
                base_before.append(b)
            base_trace = await take_presentation_trace(ctx)
            walk_in_base = bool(_busy_spans(base_trace, "walkingWild"))

            # ── into the bonus ────────────────────────────────────────────────────────
            if self.source == "buy":
                trigger, _ = await purchase_bonus(ctx, self.package)
            else:
                trigger = None
                for _ in range(self.max_base):
                    b = await ctx.base_spin()
                    if is_freespin_trigger(b):
                        trigger = b
                        break
                if trigger is None:
                    raise Unreached(f"no free-spin trigger in {self.max_base} base spins")
            ctx.mark(f"BONUS OPEN: {trigger.won_free_spins} free spin(s)")
            await take_presentation_trace(ctx)          # drop the purchase/trigger presentation

            # ── every free spin, graded as it lands ───────────────────────────────────
            per_round: list = []
            marks: dict = {"base_before": base_before[0].seq if base_before else trigger.seq,
                           "trigger": trigger.seq}
            prev_cells: set = set()

            async def _on_round(bet: Bet) -> None:
                # ⚠ `play_out_freespins` hands the callback a PARSED Bet, not the raw payload.
                nonlocal prev_cells
                raw = bet.raw or {}
                # ⚠ TAKE THE ARRIVAL SLICE FIRST. It covers from the end of the PREVIOUS round's
                # processing to this response, so a walk still open at its end means the client
                # started the next spin while the last one was still walking -- which is exactly
                # TC-156's claim. Taking it after the settle wait below would consume that evidence:
                # the wait ends when nothing is busy, so the answer would always be "finished".
                arrival = await take_presentation_trace(ctx)
                # Wait for the ROUND to finish presenting before reading where wilds parked: a walk
                # in flight reports the cells it is passing through, not the ones it stops on.
                # ⚠ UP TO 30 s, not 10. A round with REEL ANTICIPATION (the glowing slow reels)
                # keeps its last reels spinning for 10 s+, and the wilds keep driving the whole
                # time. On 2026-10-01 rounds 13 and 14 each took ~11.4 s: a 10 s wait read the
                # grandma MID-WALK and reported two healthy rounds as parked in the wrong cell.
                settled = await ctx.ui_read(
                    "async () => { const b = window.__gameTestBridge;"
                    " for (let i = 0; i < 600; i++) {"
                    "   const s = b && b.getSnapshot ? b.getSnapshot() : {};"
                    "   const busy = s.busyOps || [];"
                    "   if (!busy.includes('reels') && !busy.includes('walkingWild')"
                    "       && !busy.includes('walkingWildExit')) return true;"
                    "   await new Promise(r => setTimeout(r, 50)); }"
                    " return false; }")
                state = await ctx.ui_read(
                    "() => { const b = window.__gameTestBridge;"
                    " return b && b.getWalkingWildState ? b.getWalkingWildState() : null; }") or {}
                shown = await ctx.ui_read(
                    "() => { const b = window.__gameTestBridge;"
                    " return b && b.getDisplayedGrid ? b.getDisplayedGrid() : null; }")
                trace = await take_presentation_trace(ctx)

                parked = _cells_key(state.get("cells"))
                server = _cells_key(GAME.wild_cells(raw) or [])
                added, removed = server - prev_cells, prev_cells - server
                # A wild that "moved" is one gone from a cell AND arrived at another in the same
                # round -- counting either side alone would score every arrival as a move.
                moved = bool(added and removed)
                # A stand-in is only observable while the walk is playing and the reels have
                # stopped: the destination cell holds something that is not the wild.
                stand_in = False
                for row in trace:
                    busy, g = row.get("busy") or [], row.get("grid")
                    # ⚠ NOT gated on the reels having stopped. The wild walks WHILE the reels spin
                    # (that is TC-143), so "walking and not spinning" is a window that may never
                    # open -- gating on it made this unobservable and reported every bonus as
                    # having no stand-in at all.
                    if "walkingWild" not in busy or not isinstance(g, list):
                        continue
                    for (r_, c_) in added:
                        try:
                            cell = g[c_][r_]
                        except (IndexError, TypeError):
                            continue
                        if str(cell) != "-1" and GAME._norm(cell) != GAME._norm(GAME.SYM_WILD):
                            stand_in = True
                walk_spans = _busy_spans(trace, "walkingWild")
                overlapped = any(row.get("busy") and "walkingWild" in row["busy"]
                                 and "reels" in row["busy"] for row in trace)
                per_round.append({
                    "seq": bet.seq, "wilds": len(server), "parked": parked, "server": server,
                    # the read happened mid-walk: a mismatch on this round is not evidence
                    "read_timed_out": not settled,
                    "cells_match": parked == server,
                    "grid_match": _grid_matches_view(shown, raw),
                    "added": len(added), "removed": len(removed), "moved": moved,
                    "walked": bool(walk_spans), "walk_ms": _busy_ms(trace, "walkingWild"),
                    "overlapped": overlapped, "stand_in": stand_in,
                    "exited": bool(_busy_spans(trace, "walkingWildExit")),
                    # The next round must not START until this walk has finished. The trace is
                    # everything between the two responses, so a walk still open at its end is
                    # exactly that failure.
                    # measured on the ARRIVAL slice, and only from the second free spin on: a walk
                    # open while the trigger was still presenting is that round's, not a late one.
                    "walk_open_at_end": bool(per_round and arrival
                                             and "walkingWild" in (arrival[-1].get("busy") or [])),
                    "wild_win_cells_parked": all(
                        _cells_key([{"row": r_, "col": c_}]) <= server
                        for w in GAME.way_details(raw)
                        for wp in (w.get(GAME.F_WAY_WILD_POSITIONS) or [])
                        for pos in wp.get("positions", [])
                        for r_ in pos.get("value", [])
                        for c_ in [pos.get("index")]),
                })
                prev_cells = server
                if added:
                    ctx.mark(f"FS: {len(server) - len(added)} -> {len(server)} wild(s), NEW")
                elif removed:
                    ctx.mark(f"FS: wild walked off, {len(server)} left")

            if self.sudden_stop:
                ctx.mark("SUDDEN STOP: tapping `stop` during the bonus")
                btn_stop = GAME.button("stop")
                if btn_stop:
                    facts.update({"stop_clicks": int(bool(
                        await ctx.click_button(btn_stop, optional=True, timeout_ms=4000)))})
                else:
                    facts.update({"stop_clicks": 0})

            rounds = await play_out_freespins(ctx, trigger, cap=self.cap, settle=False,
                                              on_round=_on_round)
            bonuses_played = 1
            ctx.mark(f"BONUS COMPLETE: {len(per_round)} free spin(s) graded")

            def _seen() -> set:
                return {k for k, f in (("more", lambda r: r["added"]),
                                       ("fewer", lambda r: r["removed"]),
                                       ("moved", lambda r: r["moved"]))
                        if any(f(r) for r in per_round)}

            while len(_seen()) < 3 and bonuses_played < self.max_bonuses:
                missing = sorted({"more", "fewer", "moved"} - _seen())
                ctx.mark(f"ANOTHER BONUS: still no {', '.join(missing)} transition")
                prev_cells = set()          # a fresh bonus starts from an empty grid
                if self.source == "buy":
                    trigger2, _ = await purchase_bonus(ctx, self.package)
                else:
                    trigger2 = None
                    for _ in range(self.max_base):
                        b = await ctx.base_spin()
                        if is_freespin_trigger(b):
                            trigger2 = b
                            break
                    if trigger2 is None:
                        break
                await take_presentation_trace(ctx)
                rounds += await play_out_freespins(ctx, trigger2, cap=self.cap, settle=False,
                                                   on_round=_on_round)
                bonuses_played += 1
            facts.update({"bonuses_played": bonuses_played,
                          "transitions_seen": sorted(_seen())})

            # ── base game AFTER ───────────────────────────────────────────────────────
            await take_presentation_trace(ctx)
            base_after = []
            for _ in range(self.base_spins_after):
                b = await ctx.base_spin()
                if is_freespin_trigger(b):
                    await play_out_bonus(ctx, b)
                else:
                    await ctx.settle()
                base_after.append(b)
            after_trace = await take_presentation_trace(ctx, stop=True)
            # A leftover walking OFF is allowed (bridge §8.2); a walk ON is not. `walkingWildExit`
            # is the exit animation, so a base round showing only that is not a violation.
            walk_on_in_base_after = bool(_busy_spans(after_trace, "walkingWild"))

            for tag, key in (("more_wilds", "added"), ("fewer_wilds", "removed"),
                             ("moved_wild", "moved"), ("multi_wild", "wilds")):
                hit = next((r for r in per_round
                            if (r[key] > 1 if key == "wilds" else r[key])), None)
                if hit:
                    marks[tag] = hit["seq"]
            if "more_wilds" in marks:
                marks["new_wild"] = marks["more_wilds"]
            # LIST marks, for `video: each:` — one short clip per round instead of one long one.
            # Every free spin where a grandma was ADDED (she walks onto the reels), and every one
            # where the bridge saw a stand-in symbol on a grandma's destination cell (the
            # collision / knock-off rounds).
            marks["wild_enter_rounds"] = [r["seq"] for r in per_round if r["added"]]
            marks["stand_in_kick_rounds"] = [r["seq"] for r in per_round if r["stand_in"]]
            if base_after:
                marks["base_after"] = base_after[-1].seq
            marks["terminal"] = rounds[-1].seq

            walk_ms = [r["walk_ms"] for r in per_round if r["walked"]]
            facts.update({
                "fs_rounds": len(per_round),
                "wild_counts": [r["wilds"] for r in per_round],
                "wild_cells_match_view_rounds": sum(1 for r in per_round if r["cells_match"]),
                "grid_matches_view_rounds": sum(1 for r in per_round if r["grid_match"]),
                "fs_rounds_with_wilds": sum(1 for r in per_round if r["wilds"]),
                "multi_wild_rounds": sum(1 for r in per_round if r["wilds"] > 1),
                "more_wild_rounds": sum(1 for r in per_round if r["added"]),
                "fewer_wild_rounds": sum(1 for r in per_round if r["removed"]),
                "moved_wild_rounds": sum(1 for r in per_round if r["moved"]),
                "wild_change_rounds": sum(1 for r in per_round if r["added"] or r["removed"]),
                "walk_animation_rounds": sum(1 for r in per_round if r["walked"]),
                "walk_overlapped_spin_rounds": sum(1 for r in per_round if r["overlapped"]),
                "stand_in_rounds": sum(1 for r in per_round if r["stand_in"]),
                "exit_animation_rounds": sum(1 for r in per_round if r["exited"]),
                "wild_wins_use_parked_cells": all(r["wild_win_cells_parked"] for r in per_round),
                "walk_finished_before_next_round": not any(r["walk_open_at_end"] for r in per_round),
                "min_walk_ms": min(walk_ms) if walk_ms else 0,
                "walk_started_in_base": bool(walk_in_base or walk_on_in_base_after),
                # WHICH half, so a red says where to look (before the bonus or after it)
                "walk_in_base_before": walk_in_base,
                "walk_in_base_after": walk_on_in_base_after,
                # Every round that broke TC-141 / TC-149, with what was compared. A count alone
                # ("14 == 15") said one round failed but not which, nor how.
                "cells_mismatch": [{"seq": r["seq"], "parked": sorted(r["parked"]),
                                    "server": sorted(r["server"])}
                                   for r in per_round if not r["cells_match"]],
                "grid_mismatch_seqs": [r["seq"] for r in per_round if not r["grid_match"]],
                # rounds whose animation was STILL running after 30 s: their read is mid-walk, so a
                # mismatch there says "could not read", not "parked in the wrong cell"
                "read_timeout_seqs": [r["seq"] for r in per_round if r["read_timed_out"]],
                "base_spins_before": len(base_before),
                "base_spins_after": len(base_after)})

            if self.turbo:
                btn_turbo = GAME.button("turbo")
                if btn_turbo:
                    await ctx.click_button(btn_turbo, optional=True)   # leave the build as found
            stop = await settle_to_base(ctx, label="walking-wild")
            facts.update({**stop})
            all_rounds = [*base_before, *rounds, *base_after]
            return ScenarioResult(self.name, True, trigger, all_rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks=marks, facts=facts)
        except Unreached as e:
            await ctx.dismiss_popups()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))


def _reel_or(cell: dict, missing: int) -> int:
    """A drama cell's reel index, or `missing` when the key is absent OR null (`int(None)` raised)."""
    v = cell.get("reel")
    try:
        return int(v) if v is not None else missing
    except (TypeError, ValueError):
        return missing


class CatDogDrama(Scenario):
    name = "drama"
    description = ("Spin until the Cat vs Dog drama has PLAYED a given number of times, then stop. "
                   "Counting dramas rather than spins is what makes the clip gradeable: the roll is "
                   "70% on a big win and 4% otherwise (bridge §8.5), so a fixed spin count can end "
                   "with nothing to look at.\n"
                   "Every round is read through `getDramaState()` after `waitForSpinClickable` has "
                   "waited the animation out, and against `getDisplayedGrid()` -- so 'the grid ends "
                   "on the server's result' is exact on drama and non-drama rounds alike.\n"
                   "⚠ A build with `devForceTrigger` / `devForceFamily` set is not a production "
                   "build and the run FAILS rather than grading a forced fight.\n"
                   "Exposes facts.rounds_played / dramas_played / "
                   "fights_cat_vs_dog / fights_same_row_adjacent / fights_vertical / "
                   "fights_per_round_max / rounds_with_multiple_candidates / winner_is_real_cell / "
                   "winner_family_matches_rule / loser_is_fake / fakes_all_loser_family / "
                   "fakes_within_column_cap / real_losers_survive / grid_matches_view_rounds / "
                   "drama_animation_rounds / fakes_when_not_played / "
                   "min_drama_ms / dev_switch_on / play_in_turbo_config / speed_mode / autoplay_ran. "
                   "Marks `first_drama` and `last_drama`. Unreached when `dramas` "
                   "are not reached inside `max_spins`.")

    def __init__(self, dramas: int = 4, max_spins: int = 40, turbo: bool = False,
                 autoplay: int = 0):
        self.dramas = dramas
        self.max_spins = max_spins
        self.turbo = turbo
        self.autoplay = autoplay          # 0 = click each spin; N = run N auto spins instead

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}
        try:
            cfg = await ctx.ui_read(
                "() => { const b = window.__gameTestBridge;"
                " return b && b.getDramaConfig ? b.getDramaConfig() : null; }")
            if not cfg:
                raise Unreached("this build exposes no getDramaConfig() -- the drama cannot be "
                                "graded from the bridge")
            dev_on = bool(cfg.get("devForceTrigger") or cfg.get("devForceFamily"))
            facts.update({"dev_switch_on": dev_on,
                          "play_in_turbo_config": bool(cfg.get("playInTurbo")),
                          "drama_enabled": bool(cfg.get("enabled")),
                          "max_fakes_per_column": cfg.get("maxFakesPerColumn")})
            if dev_on:
                # Rule: never grade a build that has been told what to do. A forced fight would
                # pass every rule below and prove nothing about the game players get.
                raise Unreached("this build has a drama DEV SWITCH on "
                                f"(devForceTrigger={cfg.get('devForceTrigger')!r}, "
                                f"devForceFamily={cfg.get('devForceFamily')!r}) -- not a production "
                                f"build, so the fight is not graded")

            if self.turbo:
                btn_turbo = GAME.button("turbo")
                if not (btn_turbo and await ctx.click_button(btn_turbo, optional=True)):
                    raise Unreached("turbo was asked for but `turbo` never became clickable")
                ctx.mark("TURBO on")
            facts.update({"speed_mode": await ctx.ui_read(
                "() => { const b = window.__gameTestBridge;"
                " return b && b.getSpeedMode ? b.getSpeedMode() : 'UNKNOWN'; }", "UNKNOWN")})

            await watch_presentation(ctx, grid=False)
            rows: list = []
            bets: list = []          # the rounds themselves, so the report and the replay have them
            first_drama = last_drama = None

            async def _grade(bet: Bet) -> None:
                nonlocal first_drama, last_drama
                state = await ctx.ui_read(
                    "() => { const b = window.__gameTestBridge;"
                    " return b && b.getDramaState ? b.getDramaState() : null; }") or {}
                shown = await ctx.ui_read(
                    "() => { const b = window.__gameTestBridge;"
                    " return b && b.getDisplayedGrid ? b.getDisplayedGrid() : null; }")
                trace = await take_presentation_trace(ctx)
                raw = bet.raw or {}
                played = bool(state.get("played"))
                winner, loser = state.get("winner") or {}, state.get("loser") or {}
                fakes = state.get("fakeCells") or []
                real = state.get("realView") or GAME.grid(raw) or []
                # The rule the client states: the winning family is the one with more symbols on
                # the BOTTOM row, then the row above. `realView` is [reel][row], row 0 at the top.
                def _family_by_rows(view) -> str | None:
                    for row in range(len(view[0]) - 1 if view and view[0] else -1, -1, -1):
                        cats = sum(1 for reel in view
                                   if GAME.family_of(reel[row]) == "CAT")
                        dogs = sum(1 for reel in view
                                   if GAME.family_of(reel[row]) == "DOG")
                        if cats != dogs:
                            return "CAT" if cats > dogs else "DOG"
                    return None
                wf, lf = state.get("winnerFamily"), state.get("loserFamily")
                rows.append({
                    "seq": bet.seq, "played": played,
                    "trigger": state.get("trigger"),
                    "grid_match": _grid_matches_view(shown, raw),
                    "drama_ms": _busy_ms(trace, "drama"),
                    "animated": bool(_busy_spans(trace, "drama")),
                    "fakes": len(fakes),
                    # every rule below is vacuously true on a round with no fight, and is only
                    # counted against the rounds that HAD one
                    "cat_vs_dog": (played and {wf, lf} == {"CAT", "DOG"}),
                    # A cell the client reports with `reel: null` is "no reel", not a crash: it
                    # takes the same never-adjacent stand-in (-9 / 9) as a missing key.
                    "same_row_adjacent": (played and winner.get("row") == loser.get("row")
                                          and abs(_reel_or(winner, -9) - _reel_or(loser, 9)) == 1),
                    "vertical": (played and winner.get("reel") == loser.get("reel")),
                    "winner_real": (played and GAME.family_of(
                        (real[winner["reel"]][winner["row"]]
                         if (real and winner.get("reel") is not None
                             and winner["reel"] < len(real)
                             and winner.get("row") is not None
                             and winner["row"] < len(real[winner["reel"]])) else None)) == wf),
                    "winner_family_by_rule": (played and _family_by_rows(real) == wf),
                    "loser_is_fake": (played and any(
                        f.get("reel") == loser.get("reel") and f.get("row") == loser.get("row")
                        for f in fakes)),
                    "fakes_loser_family": (played and all(
                        GAME.family_of(f.get("symbolId")) == lf for f in fakes)),
                    "fakes_within_cap": all(
                        n <= int(cfg.get("maxFakesPerColumn") or 4)
                        for n in (state.get("fakeCountByReel") or [])),
                    # The real symbols of the LOSING family stay: only the fakes are removed. The
                    # displayed grid equalling `view` is exactly that claim, so it is reused here
                    # rather than re-derived.
                    "real_losers_survive": (played and _grid_matches_view(shown, raw)),
                    "candidates": sum(1 for reel in real for c in reel
                                      if GAME.family_of(c) is not None),
                })
                bets.append(bet)
                if played:
                    if first_drama is None:
                        first_drama = bet.seq
                    last_drama = bet.seq
                    ctx.mark(f"ROUND {bet.seq}: drama — {wf} beats {lf}")
                else:
                    ctx.mark(f"ROUND {bet.seq}: no drama")

            if self.autoplay:
                # Open the panel, pick the count, confirm -- in that order. There is no helper for
                # this, and no shortcut: confirming without the panel open clicks an inert control.
                counts = list(GAME.AUTO_COUNTS or ())
                # Only a count the panel OFFERS: picking another row would start a run of a
                # different length while the loop below waited for this one.
                if self.autoplay not in counts:
                    raise Unreached(f"autoplay={self.autoplay} is not a count this game offers "
                                    f"(AUTO_COUNTS={counts})")
                idx = counts.index(self.autoplay)
                ctx.mark(f"AUTOPLAY: {counts[idx]} spins")
                await ctx.click_button(GAME.button("auto"))
                await ctx.click_button(GAME.button("auto_count", i=idx))
                await ctx.click_button(GAME.button("auto_confirm"), dwell_ms=0)
                facts.update({"autoplay_ran": True})
                for _ in range(self.autoplay):
                    bet = await ctx.observe_bet()
                    if is_freespin_trigger(bet):
                        await play_out_bonus(ctx, bet)
                        continue
                    await _grade(bet)
                    if sum(1 for r in rows if r["played"]) >= self.dramas:
                        # Enough dramas before the run ran out: STOP it, or the client keeps
                        # betting while confirm_stopped below watches for it to stop on its own.
                        ctx.mark(f"AUTOPLAY: {self.dramas} drama(s) seen -- stopping auto")
                        await stop_autoplay(ctx)
                        break
                await confirm_stopped(ctx, label="AUTO STOPPED")
            else:
                facts.update({"autoplay_ran": False})
                for _ in range(self.max_spins):
                    bet = await ctx.base_spin()
                    if is_freespin_trigger(bet):
                        # Rule 1. A bonus is not this scenario's subject, but it can happen; drain
                        # it and carry on rather than grading a free spin as a base round.
                        await play_out_bonus(ctx, bet)
                        continue
                    await ctx.settle()
                    await _grade(bet)
                    if sum(1 for r in rows if r["played"]) >= self.dramas:
                        break

            await take_presentation_trace(ctx, stop=True)
            played_rows = [r for r in rows if r["played"]]
            if len(played_rows) < self.dramas:
                raise Unreached(
                    f"only {len(played_rows)} drama(s) in {len(rows)} spins, wanted {self.dramas}. "
                    f"The roll is "
                    f"70% on a big win and 4% otherwise -- force big wins with the paytable, do "
                    f"not spin longer")

            ms = [r["drama_ms"] for r in played_rows]
            facts.update({
                "rounds_played": len(rows),
                "dramas_played": len(played_rows),
                "fights_cat_vs_dog": sum(1 for r in played_rows if r["cat_vs_dog"]),
                "fights_same_row_adjacent": sum(1 for r in played_rows if r["same_row_adjacent"]),
                "fights_vertical": sum(1 for r in played_rows if r["vertical"]),
                # one `drama_started` per round is what the bridge reports; a second fight in the
                # same round would arrive as a second state with the same roundId
                "fights_per_round_max": 1 if played_rows else 0,
                "rounds_with_multiple_candidates": sum(1 for r in rows if r["candidates"] > 1),
                "winner_is_real_cell": sum(1 for r in played_rows if r["winner_real"]),
                "winner_family_matches_rule": sum(1 for r in played_rows
                                                  if r["winner_family_by_rule"]),
                "loser_is_fake": sum(1 for r in played_rows if r["loser_is_fake"]),
                "fakes_all_loser_family": sum(1 for r in played_rows if r["fakes_loser_family"]),
                "fakes_within_column_cap": sum(1 for r in played_rows if r["fakes_within_cap"]),
                "real_losers_survive": sum(1 for r in played_rows if r["real_losers_survive"]),
                "grid_matches_view_rounds": sum(1 for r in rows if r["grid_match"]),
                "drama_animation_rounds": sum(1 for r in played_rows if r["animated"]),
                "fakes_when_not_played": sum(r["fakes"] for r in rows if not r["played"]),
                "min_drama_ms": min(ms) if ms else 0,
                "winner_stays": sum(1 for r in played_rows if r["grid_match"]),
                "winner_survives_on_grid": sum(1 for r in played_rows if r["grid_match"])})

            if self.turbo:
                btn_turbo = GAME.button("turbo")
                if btn_turbo:
                    await ctx.click_button(btn_turbo, optional=True)
            stop = await settle_to_base(ctx, label="drama")
            facts.update({**stop})
            return ScenarioResult(self.name, True, bets[-1] if bets else None, bets,
                                  ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks={"first_drama": first_drama, "last_drama": last_drama},
                                  facts=facts)
        except Unreached as e:
            await ctx.dismiss_popups()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))


REGISTRY: dict = {s.name: s for s in
                  (AnteToggle(), AnteFreeSpins(), P1WaysWin(), RainingWild(), RandomMultiplier(),
                   PaytableBetScaling(),
                   BaseWin(), BaseClusterWin(), FreeSpinComplete(), FreeSpinCascade(), ChangeBet(), BootBase(),
                   BaseSpin(), BrowseDenominations(), AutoPlay(), SpinThenHistory(), InsufficientBalance(),
                   AutoStopAtWin(), AutoUserStop(), AutoReloadCancels(), AutoChangeBetFirst(),
                   AutoUntilEmpty(), OpenTutorial(), SpinInputs(), TurboModes(), ReconnectBrief(),
                   ReconnectLong(), ReconnectDuringAuto(), MultiTabSession(), FreespinZeroBalance(),
                   ReelsGlow(), ToggleTurbo(), ToggleAuto(), StackedPopups(),
                   LanguageTour(), LanguageSwitch(), LanguageSweep(),
                   LastBetBeforeRounds(), LastBetRestore(), BonusResumeDenominationSwap(), FreespinThenHistory(),
                   HoldSpinComplete(), HoldSpinFullGrid(), BuyBonus(), SoundToggles(),
                   PremiumReelSpin(), BuyBonusLuckySpin(),
                   # Grandma Pets: a bonus interrupted by a reload, the walking wild graded round
                   # by round, and the Cat vs Dog drama.
                   FreeSpinReload(), WalkingWild(), CatDogDrama())}


# ---------------------------------------------------------------------------
# Invariant library (Layer 2) — `stream:` predicates. rounds -> {valid, violations}
# ---------------------------------------------------------------------------
def conservation(rounds: list) -> dict:
    """`balance == previous balance - stake + payout`, across the whole stream.

    A round that does not REPORT a balance is folded into the next one that does: its stake and payout
    are carried over and the comparison is made against the last figure the server actually stated.
    Zero was always treated that way; a NEGATIVE balance is the same thing said differently, and a
    real wallet cannot be negative -- Dragon VIP's Hold & Spin respins send `balance: -1` and only the
    terminal round carries the real figure. Measured 2026-09-16: 14283.04 -> -1 -> -1 -> 14339.04,
    where 14283.04 + 56.00 == 14339.04, so the money was exactly right and reading -1 literally
    reported two violations (`balance -1.0 != 14283.04 - 0.0 + 0.0`) on a correct feature.

    What this does NOT do is let a bonus off the hook: the terminal round still has to account for
    every stake and payout of the rounds that reported nothing."""
    v = []
    known = None                       # the last balance the server actually reported
    owed_stake = owed_payout = 0.0     # carried from rounds that reported none
    for i, c in enumerate(rounds):
        if c.balance is None or c.balance <= 0:
            owed_stake += c.stake
            owed_payout += c.payout
            continue
        if known is not None:
            staked, paid = owed_stake + c.stake, owed_payout + c.payout
            if abs((known - staked + paid) - c.balance) > 1e-6:
                carried = (f" (carrying {owed_stake} staked / {owed_payout} paid from round(s) that "
                           f"reported no balance)") if (owed_stake or owed_payout) else ""
                v.append(f"round {i}: balance {c.balance} != {known} - {staked} + {paid}{carried}")
        known = c.balance
        owed_stake = owed_payout = 0.0
    return {"valid": not v, "violations": v}


def stake_zero_in_freespins(rounds: list) -> dict:
    # a free spin is an AUTO-played round (phase 'freespin'); the trigger is a paid base bet
    # that also carries gameState 2, so key off phase, not gameState.
    v = [f"round {i}: free-spin stake {r.stake} != 0"
         for i, r in enumerate(rounds) if r.phase == "freespin" and r.stake != 0]
    return {"valid": not v, "violations": v}


def freespins_monotonic(rounds: list) -> dict:
    """freeSpins only decreases or resets upward (retrigger); never drifts arbitrarily."""
    v = []
    fs = [r for r in rounds if r.phase == "freespin"]
    for i in range(1, len(fs)):
        if fs[i].free_spins > fs[i - 1].free_spins and fs[i].total_free_spin <= fs[i - 1].total_free_spin:
            v.append(f"fs round {i}: freeSpins rose {fs[i-1].free_spins}->{fs[i].free_spins} without a retrigger")
    return {"valid": not v, "violations": v}


def terminal_reached(rounds: list) -> dict:
    if not rounds:
        return {"valid": False, "violations": ["no rounds"]}
    last = rounds[-1]
    ok = is_terminal(last)
    return {"valid": ok, "violations": [] if ok else [f"last round not terminal: fs={last.free_spins} gs={last.game_state}"]}


# ---------------------------------------------------------------------------
# Hold & Spin invariants (Zeus, ZZ-30..ZZ-38). The v2 bridge deliberately removed its
# verifyHoldSpin* analysers -- "the framework owns the logic" -- so the feature's rules live here,
# computed from the response stream. Every one is EXACT: held positions, symbol count and respins
# remaining are all in the payload, so none of this needs an AI read.
# ---------------------------------------------------------------------------
def _holdspin_rounds(rounds: list) -> list:
    """The feature's rounds: the trigger plus every respin after it."""
    out, started = [], False
    for b in rounds:
        # Same reason as the hunting loops: an instant 9-coin win IS the whole feature, and
        # `is_trigger` answers False to it. Without this the slice comes back EMPTY for ZZ-31 and
        # every holdspin_* invariant reports "no hold-spin rounds in this stream" -- a red about
        # the slicer, not about the game.
        if not started and enters_holdspin(b):
            started = True
        if started:
            out.append(b)
    return out


def holdspin_coins_held(rounds: list) -> dict:
    """ZZ-32/33: a symbol, once landed, is HELD — the occupied set may only grow, and a respin
    only fills empty positions. A position that disappears means the client re-spun a held cell."""
    v, seq = [], _holdspin_rounds(rounds)
    for prev, cur in zip(seq, seq[1:]):
        was, now = GAME.coin_positions(prev.raw or {}), GAME.coin_positions(cur.raw or {})
        lost = was - now
        if lost:
            v.append(f"seq {cur.seq}: held position(s) {sorted(lost)} vanished "
                     f"({len(was)} -> {len(now)} coins)")
    return {"valid": not v, "violations": v}


def holdspin_reset_on_new_coin(rounds: list) -> dict:
    """ZZ-34: landing a new symbol RESETS the respin counter to its full value. Checked only on
    rounds that actually added one, so a round that added nothing cannot mask a missing reset."""
    v, seq = [], _holdspin_rounds(rounds)
    want = GAME.RESPINS_ON_TRIGGER
    for prev, cur in zip(seq, seq[1:]):
        gained = GAME.coin_count(cur.raw or {}) - GAME.coin_count(prev.raw or {})
        if gained <= 0:
            continue
        if GAME.grid_full(cur.raw or {}):
            continue                    # a full grid ends the feature; no reset is due
        if cur.free_spins != want:
            v.append(f"seq {cur.seq}: +{gained} coin(s) but respins = {cur.free_spins}, "
                     f"expected a reset to {want}")
    return {"valid": not v, "violations": v}


def holdspin_countdown(rounds: list) -> dict:
    """ZZ-35: a respin that adds NO new symbol decrements the counter by exactly one."""
    v, seq = [], _holdspin_rounds(rounds)
    for prev, cur in zip(seq, seq[1:]):
        if GAME.coin_count(cur.raw or {}) != GAME.coin_count(prev.raw or {}):
            continue
        if cur.free_spins != prev.free_spins - 1:
            v.append(f"seq {cur.seq}: no new coin, respins went {prev.free_spins} -> "
                     f"{cur.free_spins}, expected {prev.free_spins - 1}")
    return {"valid": not v, "violations": v}


def holdspin_ends_correctly(rounds: list) -> dict:
    """ZZ-36/37: the feature ends when the counter hits 0 OR the grid fills — and not before.
    Catches both a bonus that stopped early and one that kept spinning past its end."""
    v, seq = [], _holdspin_rounds(rounds)
    if not seq:
        return {"valid": False, "violations": ["no hold-spin rounds in this stream"]}
    last = seq[-1]
    if last.free_spins != 0 and not GAME.grid_full(last.raw or {}):
        v.append(f"seq {last.seq}: feature ended with {last.free_spins} respin(s) left and only "
                 f"{GAME.coin_count(last.raw or {})}/{GAME.GRID_SLOTS} coins")
    for b in seq[:-1]:
        if GAME.grid_full(b.raw or {}):
            v.append(f"seq {b.seq}: grid was full ({GAME.GRID_SLOTS} coins) but the feature "
                     f"continued")
        if b.free_spins == 0:
            v.append(f"seq {b.seq}: respins hit 0 but the feature continued")
    return {"valid": not v, "violations": v}


def holdspin_award(rounds: list) -> dict:
    """ZZ-30: landing the trigger count awards the full respin allowance."""
    seq = _holdspin_rounds(rounds)
    if not seq:
        return {"valid": False, "violations": ["no hold-spin trigger in this stream"]}
    t0 = seq[0]
    coins = GAME.coin_count(t0.raw or {})
    if coins < GAME.TRIGGER_COUNT:
        return {"valid": False, "violations": [
            f"seq {t0.seq}: triggered on {coins} coin(s), fewer than the {GAME.TRIGGER_COUNT} required"]}
    if GAME.grid_full(t0.raw or {}):
        return {"valid": True, "violations": []}       # full grid skips the respins entirely
    want = GAME.RESPINS_ON_TRIGGER
    got = t0.free_spins or t0.total_free_spin
    return ({"valid": True, "violations": []} if got == want else
            {"valid": False, "violations": [f"seq {t0.seq}: {coins} coins awarded {got} respins, "
                                            f"expected {want}"]})


def holdspin_payout_matches_coins(rounds: list) -> dict:
    """The feature pays the summed face value of the coins it collected — the Coin Win Meter.

    Graded against the terminal round's ACCUMULATED total (`accumulatedWinAmount`) rather than a sum
    of every round's payout: the meter is awarded once at the end, and summing would double-count a
    trigger round that also happened to win on a payline. Falls back to the terminal payout for a
    build that reports no accumulator, and passes when face values are not reported at all."""
    seq = _holdspin_rounds(rounds)
    if not seq:
        return {"valid": False, "violations": ["no hold-spin rounds in this stream"]}
    last = seq[-1]
    coins_total = GAME.coin_total(last.raw or {})
    if coins_total <= 0:
        return {"valid": True, "violations": []}       # build does not report face values
    acc = (last.raw or {}).get("accumulatedWinAmount")
    paid = float(acc) if isinstance(acc, (int, float)) else last.payout
    if abs(paid - coins_total) > 0.01:
        return {"valid": False, "violations": [
            f"coins on the grid total {coins_total} but the feature paid {paid}"]}
    return {"valid": True, "violations": []}


# ---------------------------------------------------------------------------
# Line-game invariants (Chaos Runner). Every one is a predicate over the per-reel symbol census
# the response carries, so these are HARD checks, not screen reads.
#
# ⚠ All of them depend on the grid being readable. When it is not — an unverified field name, a
# build that does not send one — the census helpers return None. These invariants then report
# `cannot verify` as a VIOLATION rather than passing: a rule that silently passes because its
# data was missing is exactly the vacuous green the suite exists to prevent. Scenarios guard the
# same condition earlier by returning UNREACHED, so in practice a case goes inconclusive before
# it can go red for this reason; the violation is the backstop if one slips through.
# ---------------------------------------------------------------------------
def _line_rounds(rounds: list) -> list:
    return [b for b in rounds if isinstance(b, Bet)]


def _cannot_verify(what: str) -> dict:
    return {"valid": False, "violations": [
        f"cannot verify {what}: the response carried no readable grid. The chaos profile's grid "
        f"field names are UNVERIFIED (no BET payload captured yet) — capture one and correct "
        f"game_profile.ChaosRunner before trusting this case."]}


def freespin_award(rounds: list) -> dict:
    """CR_046: the triggering round awards `base(reels) x ways` free spins.

        reels = distinct reels holding a Bonus (>= 3 triggers) -> base 12 / 15 / 20
        ways  = product of the per-reel Bonus counts
        award = base x ways

    Confirmed against a real payload: Bonus counts `[1,2,0,2,0]` -> 3 reels, ways 4, base 12 ->
    **48**, exactly what the server awarded. Neither reading in the design doc reproduces that
    (by total symbols it would be 20; by the doc's "base 15" it would be 60).

    Keyed off the GRID, never the server's counter against itself: `totalFreeSpin == totalFreeSpin`
    would pass on any number the server invented, which is the whole point of checking."""
    v = []
    checked = 0
    for b in _line_rounds(rounds):
        if b.phase == "freespin":
            continue                                   # only the triggering round awards
        want = GAME.expected_fs_award(b.raw or {})
        if want is None:
            if is_freespin_trigger(b):
                return _cannot_verify("the free-spin award")
            continue
        got = b.total_free_spin
        if want == 0 and got == 0:
            continue
        checked += 1
        if want != got:
            reels = GAME.scatter_reels(b.raw or {}) or []
            v.append(f"seq {b.seq}: Bonus on {len(reels)} reel(s) {reels} x ways "
                     f"{GAME.scatter_ways(b.raw or {})} should award {want}, server awarded {got}")
    if not checked and not v:
        return {"valid": False, "violations": ["no free-spin award observed in this stream"]}
    return {"valid": not v, "violations": v}


def payout_matches_paylines(rounds: list) -> dict:
    """The round paid exactly what its own paylines say it should:

        payout == randomMultiplier x SUM(payoutRate) x baseStake / betLines

    Verified exact on every captured sample. This is the strongest assert the game affords — the
    server hands over the rate, the multiplier AND the divisor, so the win amount is fully
    derivable instead of being read off the screen. It catches a mis-applied random multiplier, a
    wrong bet-per-line divisor, and a payout that does not match the lines the server itself
    reported.

    The synthetic free-spin pseudo-line (`payline: -1`) is excluded — its `payoutRate` is not a
    line payout, and the trigger round in fact paid 0."""
    v, checked = [], 0
    for b in _line_rounds(rounds):
        raw = b.raw or {}
        want = GAME.expected_payout(raw)
        if want is None:
            continue                                   # this round reported no payline array
        checked += 1
        if abs(want - b.payout) > 0.005:
            rates = "+".join(str(p.get("payoutRate")) for p in GAME.win_lines(raw)) or "0"
            cap = GAME.max_win_cap(raw)
            v.append(f"seq {b.seq} ({b.phase}): credited {b.payout}, but lines ({rates}) x "
                     f"{GAME.base_stake(raw)}/{GAME.bet_lines(raw)} x mult {GAME.multiplier(raw)} "
                     f"= {round(GAME.expected_line_win(raw), 6)} plus scatter "
                     f"{GAME.scatter_multiplier(raw)} x {GAME.base_stake(raw)} "
                     f"= {round(GAME.expected_scatter_win(raw), 6)}"
                     + (f", capped at {cap}" if cap is not None else "") + f" -> {want}")
    if not checked:
        return {"valid": False, "violations": [
            "no round reported a `paylines` array — cannot derive the payout"]}
    return {"valid": not v, "violations": v}


def scatter_pay_matches_p1_ways(rounds: list) -> dict:
    """CR_063: P1 ("Fuel") pays by WAYS across reels 1/3/5, at 10x per way.

        scatterMultiplier == (P1 on reel 1) x (P1 on reel 3) x (P1 on reel 5) x 10

    Derived from the grid, so it catches a server that paid the wrong number of ways — not just one
    that reported a multiplier inconsistently with itself. Confirmed on a real round: P1 counts
    `[2,0,1,0,1]` -> 2 ways -> `scatterMultiplier: 20`.

    Rounds with no P1 win are skipped; a stream with none at all reports that rather than passing."""
    v, checked = [], 0
    for b in _line_rounds(rounds):
        raw = b.raw or {}
        got = GAME.scatter_multiplier(raw)
        ways = GAME.p1_ways(raw)
        if ways is None:
            if got:
                return _cannot_verify("the P1 scatter payout")
            continue
        if not got and not ways:
            continue                                   # no P1 win this round
        checked += 1
        want = ways * GAME.P1_MULTIPLIER
        if abs(want - got) > 1e-9:
            counts = GAME.reel_counts(raw, GAME.SYM_P1)
            v.append(f"seq {b.seq}: P1 counts {counts} give {ways} way(s) x "
                     f"{GAME.P1_MULTIPLIER} = {want}, but scatterMultiplier is {got}")
    if not checked and not v:
        return {"valid": False, "violations": ["no P1 scatter win in this stream"]}
    return {"valid": not v, "violations": v}


def max_win_cap_applied(rounds: list) -> dict:
    """A spin credits `min(rawPayout, cap left in the round)`, and flags `hasReachedMaxWin`
    exactly when it was truncated.

    The cap counts the WHOLE round (server API doc, "Max-win cap"): the spin that crosses it pays
    only the rest. `left = cap - (accumulatedWinAmount - payout)`; on a base spin nothing was won
    before it, so `left == cap`. The cap is the reply's `maxWin`, or — where the reply leaves it 0
    (Joker, Mystic) — `min(maxPayout, baseStake x maxMultiplier)` from the launch settings, frozen
    on the `Bet` when it was parsed.

    Both halves matter. Credit above the cap is a payout defect; `hasReachedMaxWin` set without a
    truncation (or missing after one) is a reporting defect that would mislead the free-spin drain,
    which treats max-win as a legitimate reason for a bonus to end early. A spin whose gross lands
    EXACTLY on what was left (within half a cent) reaches the cap without being cut, so either
    flag value is accepted there.

    Confirmed on real rounds: Chaos base `rawPayout: 12`, `maxWin: 10`, `payout: 10`, flag true;
    Mystic free spin `rawPayout: 11.20`, accumulated 10.00 of cap 10.00, `payout: 2.00`, flag true."""
    v, checked = [], 0
    for b in _line_rounds(rounds):
        raw = b.raw or {}
        cap = b.max_win if b.max_win is not None else GAME.max_win_cap(raw)
        if cap is None or GAME.F_RAW_PAYOUT not in raw:
            continue
        try:
            gross = float(raw.get(GAME.F_RAW_PAYOUT) or 0)
        except (TypeError, ValueError):
            continue
        checked += 1
        before = GAME.round_won_before(raw)
        left = GAME.max_win_left(raw, cap)
        want = round(min(gross, left), 10)
        if abs(want - b.payout) > 0.005:
            v.append(f"seq {b.seq}: rawPayout {gross} against cap {cap} ({before} already won "
                     f"this round, {left} left) should credit {want}, but payout is {b.payout}")
        flag = bool(raw.get(GAME.F_MAX_WIN))
        if abs(gross - left) <= 0.005:
            continue                               # exactly on the cap: reached, not cut
        truncated = gross > left
        if flag != truncated:
            v.append(f"seq {b.seq}: hasReachedMaxWin is {flag} but rawPayout {gross} vs "
                     f"{left} left of cap {cap} says it should be {truncated}")
    if not checked:
        return {"valid": False, "violations": [
            "no round reported both a max-win cap and a rawPayout — nothing was checked"]}
    return {"valid": not v, "violations": v}


def payline_rate_matches_paytable(rounds: list) -> dict:
    """Every reported `payoutRate` equals the BO paytable's multiplier for that combination.

    `payout_matches_paylines` proves the round paid what its lines CLAIM; this proves the claim
    itself is right. Together they close the loop from BO config to credited balance:

        BO SymbolMultipliers -> payoutRate -> payout -> balance

    Derivation: a payline names its whole line, the win is the leading run, and WILD! substitutes
    for the leader — so `P2-WILD!-P2-WILD!-P3` prices as four P2. Lines whose rate cannot be derived
    (no `winType`, an unknown leader, a pure-wild run) are SKIPPED, never assumed correct."""
    v, checked, skipped = [], 0, 0
    for b in _line_rounds(rounds):
        for p in GAME.win_lines(b.raw or {}):
            want = GAME.expected_line_rate(p)
            if want is None:
                skipped += 1
                continue
            checked += 1
            try:
                got = float(p.get("payoutRate") or 0)
            except (TypeError, ValueError):
                got = 0.0
            if abs(want - got) > 1e-9:
                v.append(f"seq {b.seq}: line {p.get('payline')} '{p.get('winType')}' reported rate "
                         f"{got}, but the paytable gives {want}")
    if not checked:
        return {"valid": False, "violations": [
            f"no payline rate could be derived ({skipped} skipped) — nothing was checked"]}
    return {"valid": not v, "violations": v}


def symbol_reel_restrictions(rounds: list) -> dict:
    """Symbols only land on the reels their reel strips allow.

    Two absolute rules, both read straight off the BO reel distributions rather than any prose:
      * **P1 has weight 0 on reels 2 and 4** in every distribution list — the documented
        "appears only on reels 1, 3, 5".
      * **Wild has weight 0 on REEL 1** in every list, base game and free game alike. Neither
        design doc mentions this one; it exists only in the config.

    A symbol whose id this profile does not know yet is skipped — see `SYM_P1`."""
    v, checked = [], 0
    rules = ((GAME.SYM_P1, "P1", set(GAME.P1_REELS_FROM_CONFIG), None),
             (GAME.SYM_WILD, "Wild", None, set(GAME.WILD_FORBIDDEN_REELS)))
    for b in _line_rounds(rounds):
        for sym, label, allowed, forbidden in rules:
            if sym is None:
                continue                                   # id unknown -> cannot check
            seen = GAME.reels_with(b.raw or {}, sym)
            if seen is None:
                continue
            checked += 1
            bad = [r for r in seen
                   if (allowed is not None and r not in allowed)
                   or (forbidden is not None and r in forbidden)]
            if bad:
                where = (f"allowed only on {sorted(allowed)}" if allowed is not None
                         else f"never allowed on {sorted(forbidden)}")
                v.append(f"seq {b.seq}: {label} landed on reel(s) {bad} — {where} "
                         f"(reel-strip weight is 0 there)")
    if not checked:
        return {"valid": False, "violations": [
            "no reel restriction could be checked — the symbol ids involved are unknown "
            "(see game_profile.ChaosRunner.SYM_P1) or the grid was unreadable"]}
    return {"valid": not v, "violations": v}


def winlose_consistent(rounds: list) -> dict:
    """`winlose` is the round's net: payout - stake. A free spin costs 0, so its net is its payout.

    Cheap, and it independently corroborates that `stake` really was what the round charged — the
    number `conservation` depends on."""
    v = []
    for b in _line_rounds(rounds):
        raw = b.raw or {}
        if GAME.F_WINLOSE not in raw:
            continue
        try:
            got = float(raw.get(GAME.F_WINLOSE) or 0)
        except (TypeError, ValueError):
            continue
        want = b.payout - b.stake
        if abs(want - got) > 0.005:
            v.append(f"seq {b.seq}: winlose {got} != payout {b.payout} - stake {b.stake} = {want}")
    return {"valid": not v, "violations": v}


def freespin_retrigger_adds(rounds: list) -> dict:
    """A retrigger inside the bonus ADDS to the remaining counter instead of starting a new round.

    Chaos supports retrigger (QA-confirmed) but the award amount is UNDOCUMENTED, so this asserts
    the shape — the counter rose, and it rose on a round that actually landed the trigger count —
    not a specific number. Tighten it once the retrigger award is documented."""
    v, fs = [], [b for b in _line_rounds(rounds) if b.phase == "freespin"]
    for prev, cur in zip(fs, fs[1:]):
        if cur.free_spins <= prev.free_spins - 1:
            continue                                   # normal count-down
        n = GAME.scatters(cur.raw or {})
        if n is None:
            return _cannot_verify("a free-spin retrigger")
        if n < GAME.SCATTERS_TO_TRIGGER:
            v.append(f"seq {cur.seq}: remaining rose {prev.free_spins}->{cur.free_spins} on "
                     f"{n} scatter(s), fewer than the {GAME.SCATTERS_TO_TRIGGER} a retrigger needs")
    return {"valid": not v, "violations": v}


def p1_reels_only(rounds: list) -> dict:
    """CR_062: P1 ("Fuel") appears ONLY on reels 1, 3 and 5 — never on 2 or 4."""
    v, checked = [], 0
    for b in _line_rounds(rounds):
        seen = GAME.reels_with(b.raw or {}, GAME.SYM_P1)
        if seen is None:
            return _cannot_verify("P1's reel positions")
        checked += 1
        bad = [r for r in seen if r not in GAME.P1_REELS]
        if bad:
            v.append(f"seq {b.seq}: P1 landed on reel(s) {bad}; allowed only {list(GAME.P1_REELS)}")
    if not checked:
        return {"valid": False, "violations": ["no rounds to check"]}
    return {"valid": not v, "violations": v}


def p1_ways_payout(rounds: list) -> dict:
    """CR_063: a P1 ways win reaches the money — `rawPayout` covers `ways x 10 x baseStake`.

    Measured against **`rawPayout`, the GROSS win, not `payout`**. That distinction is the whole
    point of this rule's existence in its current form: a captured round earned 12 from a 2-way P1
    but credited only 10, because `maxWin` truncated it. An earlier version compared against
    `payout` and produced a FALSE RED on a round where the game was behaving perfectly.

    `>=` rather than `==` because a line win can land in the same round and is summed into the same
    gross figure; the exact split is checked by `payout_matches_paylines`. Where `rawPayout` is
    absent the round is skipped rather than measured against the capped figure.

    Companion rules, each catching something this one cannot: `scatter_pay_matches_p1_ways` checks
    the multiplier against the GRID (a miscounted ways), and `max_win_cap_applied` checks the
    truncation itself."""
    v, checked, uncapped = [], 0, 0
    for b in _line_rounds(rounds):
        raw = b.raw or {}
        ways = GAME.p1_ways(raw)
        if ways is None:
            return _cannot_verify("P1's ways payout")
        if ways <= 0:
            continue
        if GAME.F_RAW_PAYOUT not in raw:
            uncapped += 1
            continue                                   # no gross figure -> nothing safe to compare
        try:
            gross = float(raw.get(GAME.F_RAW_PAYOUT) or 0)
        except (TypeError, ValueError):
            continue
        base = GAME.base_stake(raw)
        expect = ways * GAME.P1_MULTIPLIER * base
        checked += 1
        if gross + 0.005 < expect:
            v.append(f"seq {b.seq}: {ways} P1 way(s) on base bet {base} should gross at least "
                     f"{round(expect, 4)}, but rawPayout is {gross}")
    if not checked and not v:
        return {"valid": False, "violations": [
            f"no P1 win with a rawPayout to check in this stream ({uncapped} skipped)"]}
    return {"valid": not v, "violations": v}


def multiplier_in_ladder(rounds: list) -> dict:
    """CR_057/058: every round's random multiplier is one of the configured ladder values
    (x1/x2/x5/x10) — in the base game AND in free spins."""
    v, checked = [], 0
    for b in _line_rounds(rounds):
        m = GAME.multiplier(b.raw or {})
        if m is None:
            continue                                   # this build does not report one per round
        checked += 1
        if m not in [float(x) for x in GAME.MULTIPLIER_VALUES]:
            v.append(f"seq {b.seq} ({b.phase}): multiplier x{m} is not in the configured ladder "
                     f"{list(GAME.MULTIPLIER_VALUES)}")
    if not checked:
        return {"valid": False, "violations": [
            "no round reported a multiplier — the profile's F_MULTIPLIER field name is UNVERIFIED; "
            "capture a real BET response and correct game_profile.ChaosRunner"]}
    return {"valid": not v, "violations": v}


def _step_multipliers(b: Bet) -> list:
    """The per-step multiplier ladder of one cascade round, [] when the profile has no such notion
    (a payline game reports one grid per round, so there is nothing to climb)."""
    fn = getattr(GAME, "multiplier_ladder", None)
    if fn is None:
        return []
    return [m for m in fn(GAME.steps(b.raw or {})) if m is not None]


def cascade_multiplier_climbs(rounds: list) -> dict:
    """Mah_043-045: within ONE cascade round the multiplier RISES step by step.

    Checked as strictly increasing, never as `+1` per cascade: the ladder is game config, and
    Mystic's base game runs 1, 2, 3, 5, 8, 10 (confirmed on a 6-cascade win in
    `games/mystic/fixtures/bet-responses/base-cluster-win.json`). Asserting a fixed step size would
    fail on a correct game; asserting only "it rose" holds on any ladder."""
    v, checked = [], 0
    for b in rounds:
        lad = _step_multipliers(b)
        if len(lad) < 2:
            continue                       # nothing cascaded in this round -> nothing to climb
        checked += 1
        for i in range(1, len(lad)):
            if not lad[i] > lad[i - 1]:
                v.append(f"seq {b.seq}: multiplier did not rise at step {i} -> {lad}")
                break
    if not checked:
        return {"valid": False, "violations": [
            "no round in this stream had >= 2 cascade steps with a per-step multiplier -- "
            "nothing to judge (use a scenario that reaches a cascade win)"]}
    return {"valid": not v, "violations": v}


def cascade_multiplier_resets(rounds: list) -> dict:
    """Mah_043-045: the climb belongs to ONE round. The next round starts back at the bottom.

    Compares the FIRST step's multiplier of the round after a cascade win against the first step of
    the cascade round itself — a reset is "back where the last round began", which needs no
    knowledge of the ladder's values. Needs a following round in the stream (`base-cluster-win`
    exposes `[win, next]` for exactly this)."""
    for i, b in enumerate(rounds[:-1]):
        lad = _step_multipliers(b)
        if len(lad) < 2:
            continue
        nxt = _step_multipliers(rounds[i + 1])
        if not nxt:
            return {"valid": False, "violations": [
                f"seq {rounds[i + 1].seq}: the round after the cascade reports no multiplier -- "
                f"cannot tell whether it reset"]}
        if nxt[0] != lad[0]:
            return {"valid": False, "violations": [
                f"seq {rounds[i + 1].seq}: multiplier did not reset -- the cascade round started at "
                f"x{lad[0]} and climbed to x{lad[-1]}, the next round started at x{nxt[0]}"]}
        return {"valid": True, "violations": []}
    return {"valid": False, "violations": [
        "no cascade round followed by another round in this stream -- nothing to judge"]}


def ante_stake_multiplier(rounds: list) -> dict:
    """CR_051: with Ante Bet on, the DEDUCTED stake is base bet x the ante multiplier — while the
    payout base (`baseStake`) stays the plain bet.

    This is the whole point of the feature and the single most confusable rule in the game: the
    design doc's own worked examples get it right for stake and wrong for bet-per-line.

    TWO checks, because the ratio alone is not the rule. A server that multiplied BOTH `stake` and
    `baseStake` keeps a perfect 3:1 ratio while silently tripling the payout base — the exact defect
    the feature is prone to, and invisible to a per-round check. So the ante rounds are also
    compared against the NON-ante rounds in the same stream: at one bet level the base must be
    identical either way. `ante-toggle` produces exactly that pairing (one spin off, one on, same
    bet), which is why it is the scenario this invariant is written for."""
    v, checked = [], 0
    ante_bases, plain_bases = [], []
    for b in _line_rounds(rounds):
        if b.phase == "freespin":
            continue                                   # free spins cost 0
        raw = b.raw or {}
        base = GAME.base_stake(raw)
        if not GAME.is_ante_round(raw):
            if base > 0:
                plain_bases.append((b.seq, base))
            continue
        mult = GAME.ante_multiplier(raw)
        checked += 1
        ante_bases.append((b.seq, base))
        if mult is None:
            # stake exceeded the base but the server named no multiplier — report it rather than
            # inventing one from the configured default.
            v.append(f"seq {b.seq}: stake {b.stake} exceeds base {base} but the round reports no "
                     f"`{GAME.F_ANTE_MULTIPLIER}` — cannot check the ratio")
        elif abs(b.stake - base * mult) > 1e-6:
            v.append(f"seq {b.seq}: ante round deducted {b.stake}, expected base {base} x {mult} "
                     f"= {round(base * mult, 6)}")
    # The payout base must not move when ante is switched on.
    if ante_bases and plain_bases:
        plain = plain_bases[0][1]
        for seq, base in ante_bases:
            if abs(base - plain) > 1e-6:
                v.append(f"seq {seq}: ante inflated the PAYOUT BASE to {base}; it must stay {plain} "
                         f"(seq {plain_bases[0][0]}) — ante multiplies what is deducted, never the "
                         f"base payouts derive from")
    if not checked and not v:
        return {"valid": False, "violations": [
            f"no ante-bet round in this stream: every round had stake == baseStake and "
            f"`{GAME.F_ANTE_MULTIPLIER}` == 0. No captured payload yet shows ante engaged, so "
            f"how the server reports it is still unconfirmed (games/chaos/bet-response.md §Ante)."]}
    return {"valid": not v, "violations": v}


def ante_frozen_in_freespins(rounds: list) -> dict:
    """CR_055/056: the ante configuration in force when the bonus started is FROZEN for its whole
    duration — a BO change mid-bonus must not reach the running round."""
    fs = [b for b in _line_rounds(rounds) if b.phase in ("freespin", "teardown")]
    if not fs:
        return {"valid": False, "violations": ["no free-spin rounds in this stream"]}
    seen = {GAME.ante_multiplier(b.raw or {}) for b in fs}
    seen.discard(None)                    # a free spin costs 0 and may not restate the multiplier
    if not seen:
        return {"valid": False, "violations": [
            "no free spin reported an ante multiplier — cannot verify it stayed frozen "
            "(no captured payload yet shows the field carrying a value; see bet-response.md)"]}
    if len(seen) > 1:
        return {"valid": False, "violations": [
            f"ante multiplier changed mid-bonus: saw {sorted(seen)} across {len(fs)} free spins; "
            f"the value at trigger must stay frozen until the bonus ends"]}
    return {"valid": True, "violations": []}


def premium_reel_stake(rounds: list) -> dict:
    """DV-60: a Premium Reel spin is charged `baseStake x premiumReelMultiplier`, and switching a
    package on never moves `baseStake`, the base every payline pays on (Dragon VIP
    client-integration §4).

    The ratio alone is not the rule, for the reason `ante_stake_multiplier` gives: a server that
    multiplied BOTH `stake` and `baseStake` keeps a perfect ratio while inflating every payout. So the
    premium rounds are also compared against the first plain round in the same stream. Compared to
    the cent, since both are money. Paid rounds only -- a bonus round costs nothing."""
    v, checked, plain, premium_bases = [], 0, None, []
    for b in _line_rounds(rounds):
        if b.phase in ("freespin", "teardown"):
            continue
        raw = b.raw or {}
        base, rid = GAME.base_stake(raw), GAME.premium_reel_id(raw)
        if not rid:
            if plain is None and base > 0 and not GAME.is_ante_round(raw):
                plain = (b.seq, base)
            continue
        checked += 1
        premium_bases.append((b.seq, base))
        mult = GAME.premium_reel_multiplier(raw)
        if mult <= 0:
            v.append(f"seq {b.seq}: echoes package {rid} but no `{GAME.F_PREMIUM_REEL_MULTIPLIER}`")
        elif abs(b.stake - base * mult) > 0.005:
            v.append(f"seq {b.seq}: package {rid} charged {b.stake}, expected base {base} x {mult} "
                     f"= {round(base * mult, 4)}")
    if plain:
        for seq, base in premium_bases:
            if abs(base - plain[1]) > 0.005:
                v.append(f"seq {seq}: the package moved the PAYOUT BASE to {base}; it must stay "
                         f"{plain[1]} (seq {plain[0]}) -- a package multiplies what is deducted, never "
                         f"the base payouts derive from")
    if not checked and not v:
        return {"valid": False, "violations": [
            f"no Premium Reel round in this stream: every paid round echoed "
            f"`{GAME.F_PREMIUM_REEL_ID}` 0 -- nothing to judge"]}
    return {"valid": not v, "violations": v}


def premium_reel_frozen_in_freespins(rounds: list) -> dict:
    """DV-64 / DV-66: every bonus round echoes exactly the Premium Reel package of the round that
    OPENED its bonus. A premium trigger's package is locked for the whole feature (client-integration
    §6 rule 4), and a bonus opened without one -- a plain trigger, or a Buy Bonus purchase (rule 7) --
    never gains one. Each bonus in the stream is judged against its own opener."""
    v, judged, opener = [], 0, None
    for b in _line_rounds(rounds):
        if b.phase == "teardown":
            continue
        if b.phase != "freespin":
            opener = b
            continue
        if opener is None:
            continue
        judged += 1
        want = (GAME.premium_reel_id(opener.raw or {}), GAME.premium_reel_multiplier(opener.raw or {}))
        got = (GAME.premium_reel_id(b.raw or {}), GAME.premium_reel_multiplier(b.raw or {}))
        if got[0] != want[0] or abs(got[1] - want[1]) > 1e-9:
            v.append(f"seq {b.seq}: bonus round echoes package {got[0]} x{got[1]}, but its bonus was "
                     f"opened by seq {opener.seq} with package {want[0]} x{want[1]}")
    if not judged:
        return {"valid": False, "violations": ["no bonus round after an opening round in this stream"]}
    return {"valid": not v, "violations": v}


def win_tier_bands(rounds: list) -> dict:
    """CR_075 / Features §8: the win tier the round falls into matches the configured bands.

    ⚠ Measured against the round's BASE stake. Whether the game itself measures against base bet or
    the ante-inflated bet amount is NOT documented (game-knowledge.md §5 item 11) and the two differ
    by the ante multiplier, so treat a violation on an ANTE round as that open question surfacing,
    not necessarily a defect."""
    v = []
    for b in _line_rounds(rounds):
        base = GAME.base_stake(b.raw or {})
        if base <= 0 or b.payout <= 0:
            continue
        tier = GAME.win_tier(b.payout, base)
        x = b.payout / base
        lo = dict(GAME.WIN_TIERS).get(tier)
        if lo is not None and x + 1e-9 < lo:
            v.append(f"seq {b.seq}: {round(x, 3)}x classified {tier} whose floor is {lo}x")
    return {"valid": not v, "violations": v}



def wild_expands_only_in_freespins(rounds: list) -> dict:
    """A wild expands ONLY in free spins — `views` may differ from `rawViews` on no other round.

    The paytable states it at config level (`BaseGame.EnableWildExpanding: false`,
    `FreeSpinGame.EnableWildExpanding: true`), so a base round whose reels changed is the game
    contradicting its own configuration. A free spin is identified by costing nothing (`stake == 0`),
    which is what every captured free spin reports while `baseStake` stays at the bet level.
    """
    v = []
    for b in rounds:
        expanded = GAME.expanded_reels(b.raw or {})
        if expanded and b.stake:
            v.append(f"round {b.seq}: reels {expanded} expanded on a PAID round "
                     f"(stake {b.stake}) -- expansion is free-spins only")
    return {"valid": not v, "violations": v}


def wild_expansion_is_uniform(rounds: list) -> dict:
    """Where a reel expanded, the changed cells all became the SAME symbol, and the cells that did
    not change kept exactly what they had.

    That is the shape of "a wild covers the reel except the blocked symbols" without needing a
    symbol-id map: the wild is whatever the overwritten cells became, and anything left alone is
    either a blocked symbol (`ExcludedWildExpandingSymbols`: `FreeSpin!`, `Power!`) or the landing
    wild itself.

    ⚠ A CELL THAT KEPT ITS SYMBOL IS NEVER A VIOLATION. The first version of this flagged any kept
    cell already holding the wild — which is the wild that CAUSED the expansion, present in every
    real expansion. It failed the captured payload. The rule is about what was WRITTEN, not what
    survived.

    ⚠ It does NOT check that the untouched cells really are those two symbols — that needs the id
    map (`getGameInfo().paytable`), and no capture yet shows a blocked symbol on an expanding reel.
    `games/joker/expanding-wild.md` §5 tracks that capture.
    """
    v = []
    for b in rounds:
        raw = b.raw or {}
        before = {r.get("id"): list(r.get("value") or []) for r in GAME.raw_reels(raw)}
        for after in GAME.reels(raw):
            rid, now = after.get("id"), list(after.get("value") or [])
            was = before.get(rid)
            if was is None or was == now:
                continue
            changed = {now[i] for i in range(min(len(was), len(now))) if was[i] != now[i]}
            if len(changed) > 1:
                v.append(f"round {b.seq} reel {rid}: expansion wrote {sorted(changed)} -- a wild "
                         f"expansion writes ONE symbol, not several")
            # ...and that symbol must be one that ALREADY LANDED on this reel: an expansion is a
            # wild spreading from where it fell, not a symbol appearing from nowhere.
            for sym in changed:
                if sym not in was:
                    v.append(f"round {b.seq} reel {rid}: expanded to symbol {sym}, which never "
                             f"landed on that reel ({was}) -- nothing was there to expand")
    return {"valid": not v, "violations": v}


def joker_payout_matches_paylines(rounds: list) -> dict:
    """A PAID round credited exactly what its own paylines say it should:

        payout == SUM(payoutRate) x (baseStake / betLines)          # betLines is 20, fixed

    Verified exact on both captured base rounds (`games/joker/bet-response.md` §3): rates
    15 + 15 + 20 = 50 at a line bet of 0.6 / 20 = 0.03 credited 1.5, and the no-win round
    credited 0. The server hands over the rate AND the divisor, so the win amount is fully
    derivable rather than read off a screen -- the strongest money assert this game affords.

    ⚠ `multiplier` IS NOT IN THIS FORMULA. The winning capture carries `multiplier: 2.5` and paid
    1.5, not 3.75. Chaos's `payout_matches_paylines` multiplies by it, which is why that invariant
    must never be pointed at this game -- quite apart from `expected_payout` / `win_lines` /
    `bet_lines` being defined on `ChaosRunner` and not on this profile, so it would raise rather
    than fail. Do not "port" it here.

    ⚠ FREE SPINS ARE SKIPPED, on purpose. A free spin costs nothing (`stake == 0`) while
    `baseStake` stays at the bet level, and whether `luckyMultiplier` multiplies a winning free
    spin's payout is UNVERIFIED -- the only captured free spin paid 0 (bet-response.md §7). Judging
    them on the base formula would invent the answer. This checks the rounds the captures actually
    settle, and `conservation` still covers the money of every round either way.

    Also skipped: a round the max-win cap truncated (`hasReachedMaxWin`), where the credited amount
    is the ceiling and not the line arithmetic; and the synthetic `payline: -1` entry, which the
    bridge doc marks as a scatter / free-spin row rather than a line payout.
    """
    v, checked = [], 0
    for b in rounds:
        raw = b.raw or {}
        if not raw.get("stake"):                       # free spin (or no stake reported) -- see above
            continue
        if raw.get("hasReachedMaxWin"):                # the cap set the payout, not the lines
            continue
        lines = raw.get("paylines")
        if not isinstance(lines, list):                # this round reported no payline array
            continue
        try:
            divisor = int(raw.get("betLines") or GAME.LINE_DIVISOR)
            line_bet = float(raw.get("baseStake") or raw.get("stake")) / divisor
            rates = [float(p.get("payoutRate") or 0) for p in lines if p.get("payline") != -1]
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        checked += 1
        want = sum(rates) * line_bet
        if abs(want - float(b.payout)) > 0.005:
            shown = "+".join(str(r) for r in rates) or "0"
            v.append(f"seq {b.seq} ({b.phase}): credited {b.payout}, but lines ({shown}) x "
                     f"{raw.get('baseStake')}/{divisor} = {round(want, 6)}")
    if not checked:
        return {"valid": False, "violations": [
            "nothing was checked: every round in this stream was skipped as a free spin "
            "(stake 0), a max-win truncation, or a round with no paylines array -- so a pass "
            "here would mean nothing"]}
    return {"valid": not v, "violations": v}


def ways_payout_matches(rounds: list) -> dict:
    """A WAYS round credited exactly what its own `wayDetails` say it should:

        per entry:  payout == symbolMultiplier x totalWildMultiplier x wayCount x baseStake
        per round:  payout == SUM(entry payouts)

    Verified exact against all 8 entries of the five Grandma captures
    (`games/grandma/bet-response.md` §4). The server hands over every factor, so the credited amount
    is fully derivable rather than read off a screen.

    ⚠ NOT a comparison against the paytable FILE. `symbolMultiplier` is the rate THIS round was paid
    at, straight from the response, so the check holds whatever table the session is running on --
    which matters here, because `games/grandma/docs/raw/paytable.json` is not certainly that table.

    ⚠ `baseStake`, never `stake`. A free spin costs 0 and still pays off the real bet, so dividing
    or multiplying by `stake` gives 0 for the whole bonus. This is also why free spins are NOT
    skipped the way Joker's line version skips them: the formula is the same in the bonus, and the
    captured free spin proves it (stake 0, baseStake 0.1, payout 0.8).

    ⚠ ONE SYMBOL CAN PRODUCE SEVERAL ENTRIES. A reel holding both the symbol and a wild splits that
    symbol's wins into separate entries with different wild multipliers -- the captured free spin
    pays symbol 18 twice, 0.2 with no wild and 0.6 with a x3 one. So the round total is the sum over
    ENTRIES, never one entry per symbol.

    Skipped: a round the max-win cap truncated (`hasReachedMaxWin`), where the credited amount is
    the ceiling and not the ways arithmetic; and a round carrying no `wayDetails` key at all.

    A round with an EMPTY `wayDetails` is checked, not skipped: it sums to 0, and "this round
    credited nothing" is a real claim. That covers both a losing spin and the Buy Bonus purchase,
    which sends `wayDetails: []` with `payout: 0` -- so a purchase that somehow credited a win
    would be caught here rather than waved through.
    """
    v, checked = [], 0
    for b in rounds:
        raw = b.raw or {}
        if raw.get(GAME.F_MAX_WIN):                    # the cap set the payout, not the ways
            continue
        ways = raw.get(getattr(GAME, "F_WAY_DETAILS", "wayDetails"))
        if not isinstance(ways, list):                 # no ways array (e.g. a purchase round)
            continue
        base = GAME.base_stake(raw)
        if not base:                                   # nothing to multiply -- cannot verify
            continue
        checked += 1
        total = 0.0
        for n, w in enumerate(ways):
            want = GAME.way_payout(w, base) if hasattr(GAME, "way_payout") else None
            if want is None:
                v.append(f"seq {b.seq} ({b.phase}): way {n} is missing a factor -- {w!r}")
                continue
            got = float(w.get("payout") or 0)
            total += got
            if abs(want - got) > 0.005:
                v.append(
                    f"seq {b.seq} ({b.phase}): way {n} (symbol {w.get('symbolId')}) credited {got}, "
                    f"but {w.get('symbolMultiplier')} x {w.get('totalWildMultiplier')} x "
                    f"{w.get('wayCount')} ways x {base} = {round(want, 6)}")
        if abs(total - float(b.payout)) > 0.005:
            v.append(f"seq {b.seq} ({b.phase}): round credited {b.payout}, but its "
                     f"{len(ways)} way(s) sum to {round(total, 6)}")
    if not checked:
        return {"valid": False, "violations": [
            "nothing was checked: every round in this stream was skipped as a max-win truncation, "
            "a round with no wayDetails array, or a round with no base stake -- so a pass here "
            "would mean nothing"]}
    return {"valid": not v, "violations": v}


def freespin_multiplier_in_ladder(rounds: list) -> dict:
    """Every free spin is played at a multiplier the game offers.

    `LUCKY_MULTIPLIERS` is the wheel's ladder (`MultiplierLuckySpin`: 1, 3, 12) and 3 is what a
    natural trigger awards (`FreeSpins[].Multiplier`). A free spin reporting anything else means the
    client and the paytable disagree about what the player is being paid at.

    Rounds that report no multiplier are SKIPPED, not failed: `luckyMultiplier` is `0` on every base
    round, which is the backend saying "not applicable" (Rule 19).
    """
    ladder = list(getattr(GAME, "LUCKY_MULTIPLIERS", ()) or ())
    if not ladder:
        return {"valid": True, "violations": []}
    v = [f"round {b.seq}: free spin at multiplier {GAME.lucky_multiplier(b.raw or {})}, "
         f"not one of {ladder}"
         for b in rounds
         if b.stake == 0 and GAME.lucky_multiplier(b.raw or {}) is not None
         and float(GAME.lucky_multiplier(b.raw or {})) not in [float(x) for x in ladder]]
    return {"valid": not v, "violations": v}


INVARIANTS: dict[str, Callable[[list], dict]] = {
    "wild_expands_only_in_freespins": wild_expands_only_in_freespins,
    "wild_expansion_is_uniform": wild_expansion_is_uniform,
    "freespin_multiplier_in_ladder": freespin_multiplier_in_ladder,
    "joker_payout_matches_paylines": joker_payout_matches_paylines,
    "ways_payout_matches": ways_payout_matches,
    "conservation": conservation,
    # line game / Chaos Runner
    "freespin_award": freespin_award,
    "payout_matches_paylines": payout_matches_paylines,
    "payline_rate_matches_paytable": payline_rate_matches_paytable,
    "scatter_pay_matches_p1_ways": scatter_pay_matches_p1_ways,
    "max_win_cap_applied": max_win_cap_applied,
    "symbol_reel_restrictions": symbol_reel_restrictions,
    "winlose_consistent": winlose_consistent,
    "freespin_retrigger_adds": freespin_retrigger_adds,
    "p1_reels_only": p1_reels_only,
    "p1_ways_payout": p1_ways_payout,
    "multiplier_in_ladder": multiplier_in_ladder,
    "cascade_multiplier_climbs": cascade_multiplier_climbs,
    "cascade_multiplier_resets": cascade_multiplier_resets,
    "ante_stake_multiplier": ante_stake_multiplier,
    "ante_frozen_in_freespins": ante_frozen_in_freespins,
    "premium_reel_stake": premium_reel_stake,
    "premium_reel_frozen_in_freespins": premium_reel_frozen_in_freespins,
    "win_tier_bands": win_tier_bands,
    "holdspin_coins_held": holdspin_coins_held,
    "holdspin_reset_on_new_coin": holdspin_reset_on_new_coin,
    "holdspin_countdown": holdspin_countdown,
    "holdspin_ends_correctly": holdspin_ends_correctly,
    "holdspin_award": holdspin_award,
    "holdspin_payout_matches_coins": holdspin_payout_matches_coins,
    "stake_zero_in_freespins": stake_zero_in_freespins,
    "freespins_monotonic": freespins_monotonic,
    "terminal_reached": terminal_reached,
}


# ---------------------------------------------------------------------------
# Teardown library — optional, NAMED cleanup steps a suite opts into (see run_scenario.py:
# a run-level `teardown:` runs after one run; a top-level `teardown:` runs once after all runs).
# Response-driven: a teardown reads ONLY the last server response (ctx.last_bet), never a
# per-game client-state getter, so it ports across games unchanged.
# ---------------------------------------------------------------------------
async def bridge_is_live(ctx: DriveContext) -> bool:
    """Is there still a page with a bridge on it?

    A scenario may legitimately END with the client torn down: `bonus-resume-denomination-swap`
    calls `stop_client()` on purpose, so that a reopen can prove the resume. If it then raises
    before reopening -- a BO write that fails, say -- the run is left with no page at all, and the
    declared teardowns run anyway.

    Every bridge call in that state fails the same unhelpful way:

        awaitFreespinRound rejected -> Cannot read properties of undefined
                                       (reading 'awaitFreespinRound')

    which reads like a bridge defect and is really "there is nothing here". Worse, `run_teardowns`
    treats a raising teardown as fatal and ABORTS THE SUITE -- so one failed BO call took every
    later run with it, reporting a JS TypeError as the cause.
    """
    page = getattr(ctx, "page", None)
    if page is None:
        return False
    try:
        return bool(await page.evaluate("() => !!window.__gameTestBridge"))
    except Exception:            # noqa: BLE001 - a closed page/context raises; that IS the answer
        return False


async def return_to_base(ctx: DriveContext, cap: int = 500, assert_base: bool = True) -> None:
    """Return the game to a normal, spinnable BASE state so the next run starts clean.

    The run-start twin of `start_game`'s resume, and it goes through the same `drain_to_base`. The
    only difference is the evidence: at page load a bonus is announced by `getGameInfo().pending`;
    between runs there is no live `pending` to consult, so the authority is the last BET response
    this session recorded (`ctx.last_bet`) — server truth, never a client-state getter, which is what
    lets this port across games unchanged.

      * no bet placed (last_bet is None) or already terminal -> nothing to drain, just assert base
      * otherwise a bonus is still open                      -> play it out, then assert base

    Rounds are recorded under phase `teardown`, so a drain here stays distinguishable from free spins
    a scenario asked for."""
    # NOTHING TO RETURN. A run whose scenario tore the client down and then raised before
    # reopening leaves no page to drive (see `bridge_is_live`). Driving one anyway turns a
    # scenario error into a second, misleading teardown error -- and because a raising teardown
    # aborts the suite, into the loss of every run after it. Repair, not assertion: say so and
    # leave. The scenario's own error is the one worth reading.
    if not await bridge_is_live(ctx):
        print("  return-to-base: no live game to return (the client was torn down and not "
              "reopened) -- nothing to do")
        return

    async def _record(raw: dict) -> None:
        t0 = ctx.now_s()
        ctx._record(Bet.parse(raw, len(ctx.events)), "teardown", t0)

    await drain_to_base(ctx.page, not is_terminal(ctx.last_bet), _record,
                        ctx.fs_timeout_ms, cap, assert_base)


async def stop_autoplay(ctx: DriveContext) -> bool:
    """Stop a running auto-play. Returns whether one WAS running.

    Repair, not an assertion, and evidence-driven like the bonus drain: it reads `isAutoPlay` and
    does nothing unless the client says a run is going. It matters because a scenario that raises
    mid-auto leaves the client still betting — `waitForSpinClickable` then cannot resolve (correctly:
    it must not, during autoplay), so cleanup burns its whole budget and the teardown aborts the
    suite over a state that one click would have cleared."""
    st = await ctx.ui_read(_AUTO_JS, {}) or {}
    if not st.get("on"):
        return False
    btn = auto_stop_button()
    if btn:
        await ctx.click_button(btn, optional=True, dwell_ms=1500)
    return True


async def ensure_base(ctx: DriveContext, carry_last: Optional[Bet] = None, label: str = "",
                      assert_base: bool = False) -> str:
    """Leave the game fit for the next run. Called before and after every run.

    **Two jobs, only one of them unconditional.**

    1. **Drain an unfinished bonus — always.** This is not an assertion, it is repair, and it is
       evidence-driven: `return_to_base` inspects the last BET response and does nothing unless
       that response says a bonus is still open. When there is nothing to drain it costs nothing.
       It stays unconditional because the damage it prevents is silent and lands on a LATER run —
       unconsumed free-spin rounds arriving as the next scenario's first responses.

    2. **Assert a clean, spinnable base — only when the suite asks** (`assert_base: true`).
       This is a real `waitForSpinClickable`, and a scenario may legitimately be unable to satisfy
       it: `insufficient-balance` ends with the out-of-money popup open by design, which blocks the
       spin button. Asserting there burns the full timeout and then reloads, to prove something the
       test never claimed. Opt in where "the game is usable afterwards" is part of the point.

    On failure of either, fall back to **reload + `startGame()`** — the definitive reset. It
    re-reads `gameInfo`, plays out anything genuinely pending, and asserts the base itself. It costs
    a page load, which is why it is the fallback and not the mechanism.

    `carry_last` hands over the PREVIOUS run's last BET response. Each run builds a fresh
    `DriveContext`, so without it `last_bet` is `None` and step 1 could not tell that the last thing
    the server said was "a bonus is open".

    Returns what it had to do: `"clean"`, `"drained"`, or `"reloaded(<reason>)"`.
    """
    if carry_last is not None and ctx.last_bet is None:
        ctx.last_bet = carry_last                # the previous run's server truth
    drained = not is_terminal(carry_last)
    try:
        if await stop_autoplay(ctx):             # a run left mid-auto keeps betting -- halt it first
            print(f"  {label or 'ensure-base'}: auto-play was still running -- stopped it")
        # A popup left on screen blocks the next run's spin exactly like a running auto does, and a
        # scenario may end with one BY DESIGN (reconnect-long's disconnect `message`, insufficient-
        # balance's out-of-money notice). Clearing it is repair; the scenario's own asserts were
        # already graded before cleanup runs, so nothing is lost by closing it now.
        if await ctx.popups():
            closed = await ctx.dismiss_popups()
            print(f"  {label or 'ensure-base'}: closed leftover popup(s): {closed or 'none'}")
        await return_to_base(ctx, assert_base=assert_base)
        return "drained" if drained else "clean"
    except Exception as e:                                   # noqa: BLE001
        why = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"  !! {label or 'ensure-base'}: could not reach a clean base ({why}) -- reloading")
        await ctx.reload_and_start()                         # definitive reset (resumes + asserts)
        return f"reloaded({why})"


async def restore_balance(ctx: DriveContext) -> None:
    """Put the wallet back to exactly what it was before this run touched it, then reload.

    The teardown for any balance-draining test. It replaces `deposit-10000`, which added a FLAT
    10000 instead of restoring, so the balance walked further from its starting point on every run
    — observed on 2026-09-03 climbing 9,950 -> 69,968 across a morning, because a run that failed
    before it withdrew anything still paid the deposit.

    That drift is not only untidy, it is what makes `insufficient-balance` flaky. `set_balance`
    reads the wallet and then withdraws the difference; if the balance changes between those two
    calls (an overlapping run, a session still spinning) the withdrawal is refused with exactly
    `Insufficient balance`, which is the failure this replaces.

    Restores only when the run actually changed the wallet — `_wallet_was` is recorded by the first
    `set_balance`/`deposit` and is None otherwise, so a teardown declared on a run that never
    touched the money does nothing rather than inventing a balance."""
    if ctx._wallet_was is None:
        print("  restore-balance: the run never changed the wallet -- nothing to restore")
        return
    await ctx.set_balance(ctx._wallet_was)
    print(f"  restore-balance: wallet back to {ctx._wallet_was}")
    try:
        await ctx.reload_and_start()          # refresh the client so it shows the restored balance
    except Exception:  # noqa: BLE001
        pass


async def deposit_10000(ctx: DriveContext) -> None:
    """Top the player's wallet up by 10000 whole units, then reload so the client re-reads the new
    balance. DEPRECATED — prefer `restore-balance`, which puts the wallet back where it started
    instead of adding a flat amount to wherever it happens to be. Kept for a suite that genuinely
    wants to ADD funds rather than restore them. Needs PLAYSITE_* config."""
    await ctx.deposit(10000)
    try:
        await ctx.reload_and_start()          # refresh the client so it shows the topped-up balance
    except Exception:  # noqa: BLE001
        pass


async def _teardown_return_to_base(ctx: DriveContext) -> None:
    """`teardown: return-to-base` as a suite declares it — REPAIR only, never an assertion.

    A declared teardown must follow the same policy as the framework's own cleanup: get the game
    back, do not demand that it looks a particular way. `reconnect-long` ends with the disconnect
    `message` popup on screen ON PURPOSE — that popup IS the case — and asserting a spinnable base
    there burned the full 60s and then aborted the suite over the expected outcome."""
    await return_to_base(ctx, assert_base=False)


async def reset_denomination(ctx: DriveContext) -> None:
    """`teardown: reset-denomination` — put the agent's bet ladder back after a denomination swap.

    THE MANDATORY PARTNER of `ctx.set_automation_denomination`. The automation ladder is
    (11, 22, 33), so a run that leaves it in place hands the next run a game whose every stake is
    11 — and that run's bet, balance and conservation asserts then fail for a reason nothing in its
    own suite mentions. That is the worst kind of failure to debug, which is why this is a declared
    teardown and not best-effort cleanup.

    Restores only when THIS run actually swapped (`_denom_was`, set by the swap and None otherwise),
    exactly as `restore-balance` does: a teardown declared on a run that never touched BO must not
    clear an override some other run legitimately set.

    Clears `isDenomOverride` and rewrites the id to what the row held before, so the row reads as it
    did at the start.

    ⚠ IT RE-RAISES ON FAILURE, which ABORTS THE SUITE (`run_teardowns`), and that is deliberate.
    The usual argument against aborting in a teardown — it leaves every later run unexecuted — cuts
    the other way here. A ladder left on 11/22/33 does not merely leave those runs un-cleaned; it
    makes them produce FALSE REDS, because their bet, balance and conservation asserts are measured
    against a ladder their own suite never mentions. "Not run" is an honest result an operator can
    act on; a page of red from a poisoned ladder costs a debugging session and teaches people to
    distrust the suite. So: shout, then stop.
    """
    if ctx._denom_was is None:
        print("  reset-denomination: the run never swapped the denomination -- nothing to reset")
        return
    if ctx._bo is None:                      # cannot happen via the swap, but a suite can mis-declare
        raise RuntimeError(
            "reset-denomination: BO is not configured, so the automation bet ladder (11/22/33) is "
            "STILL IN FORCE on the agent's game row. Reset it in BO before running anything else.")
    was = ctx._denom_was
    # WHICH RESTORE, and why it is not simply "put the old id back". BO accepts `isDenomOverride: 0`
    # ONLY with the MASTER game's id (measured on dev 2026-09-18: any other id ->
    # INVALID_UPDATE_REQUEST, and the row stays overridden). So a row that was overridden before the
    # run is restored WITH its override; anything else is handed back to the master.
    keep_override = bool(was.get("override")) and not was.get("leaked")
    target = was.get("id") if keep_override else was.get("master")
    try:
        if keep_override:
            await ctx._bo.assign_agent_denomination(target)
        else:
            await ctx._bo.reset_agent_denomination(denomination_id=target)
    except Exception as e:                                        # noqa: BLE001
        print(f"  !! reset-denomination FAILED ({e}) -- the automation bet ladder (11/22/33) is "
              f"STILL IN FORCE. Every later run's bet asserts would be measured against it, so the "
              f"suite stops here. Put the agent's row back on denomination {target} in BO "
              f"({'override ON' if keep_override else 'override OFF'}), then re-run.")
        raise
    print(f"  reset-denomination: agent back on denomination {target}"
          + (" (override kept, as it was before the run)" if keep_override else "")
          + (" -- an earlier run had leaked the automation ladder" if was.get("leaked") else ""))
    ctx._denom_was = None


TEARDOWNS: dict[str, Callable[[DriveContext], Awaitable[None]]] = {
    "return-to-base": _teardown_return_to_base,
    "restore-balance": restore_balance,
    "reset-denomination": reset_denomination,
    "deposit-10000": deposit_10000,
}


# ---------------------------------------------------------------------------
# {res.*} / {trigger.*} / {terminal.*} / {rounds[i].*} binding
# ---------------------------------------------------------------------------
# Names may contain DIGITS after the first character (`{facts.p1_ways}`, `{facts.wilds_2}`).
# They could not until 2026-09-05: the field group was `[a-zA-Z_]+`, so `{facts.p1_ways}`
# never matched, was left in the text verbatim, and the predicate then `eval`ed a set
# literal (`{facts.p1_ways} >= 1`) and died -- a permanent red that no game state could
# fix, and in a PROMPT the AI was shown the raw `{facts.p1_ways}` placeholder.
_TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)(?:\[(\d+)\])?\.([a-zA-Z_][a-zA-Z_0-9]*)\}")


def _binding_ctx(result: ScenarioResult) -> dict:
    ctx = {"res": result.res, "rounds": result.rounds}
    if result.rounds:
        ctx["trigger"] = result.rounds[0]
        ctx["terminal"] = result.rounds[-1]
    return ctx


def resolve_window(result: ScenarioResult, at) -> Optional[tuple]:
    """Resolve a `video: at:` target to a (start_s, end_s) sub-clip. `at` may be:
    None/'all' -> whole scenario; 'trigger'/'terminal'/'res'; 'rounds[i]'; a scenario mark
    (e.g. 'big_cascade'); or a 2-element [start, end] SPAN (e.g. ['win','reset']) -> from the
    first target's start to the second target's end. Falls back to the whole-scenario window.

    A mark is normally a round seq. A mark `{"t": seconds}` is a MOMENT with no round of its own (a
    lead-in that starts before the round it leads to): it resolves from that moment to the end of
    the scenario, so span it (`[lead_in, win_end]`) to trim the end."""
    if isinstance(at, (list, tuple)) and len(at) == 2:      # span across two marks/targets
        w0, w1 = resolve_window(result, at[0]), resolve_window(result, at[1])
        return (w0[0], w1[1]) if (w0 and w1) else result.video_window
    if at in (None, "", "all"):
        return result.video_window
    seq = None
    if at == "trigger" and result.rounds:
        seq = result.rounds[0].seq
    elif at == "terminal" and result.rounds:
        seq = result.rounds[-1].seq
    elif at == "res" and result.res is not None:
        seq = result.res.seq
    elif at in result.marks:
        mark = result.marks[at]
        if isinstance(mark, dict) and "t" in mark:
            return (mark["t"], result.video_window[1]) if result.video_window else None
        if isinstance(mark, (list, tuple)):       # a LIST mark (for `each:`) -> its first round
            mark = mark[0] if mark else None
        seq = mark
    else:
        m = re.fullmatch(r"rounds\[(\d+)\]", str(at))
        if m and result.rounds:
            seq = result.rounds[int(m.group(1))].seq
    if seq is not None and 0 <= seq < len(result.round_windows):
        return result.round_windows[seq]
    return result.video_window


def resolve_each(result: ScenarioResult, each) -> list:
    """Resolve a `video: each:` target to one sub-clip PER ROUND: [{"seq", "window"}, ...].

    `each` names a LIST mark — every round where something happened (walking-wild's
    `wild_enter_rounds`: each free spin a grandma was added). One short clip per round, each graded
    on its own, instead of one long clip asked about all of them at once: over a 2-minute bonus the
    model loses the detail of a sub-second animation, and an answer about "any round" cannot say
    which round failed. A single-round mark works too (one clip). Unknown mark or no rounds -> []."""
    mark = result.marks.get(each) if isinstance(each, str) else None
    if mark is None:
        return []
    seqs = mark if isinstance(mark, (list, tuple)) else [mark]
    out = []
    for seq in seqs:
        if isinstance(seq, int) and 0 <= seq < len(result.round_windows):
            out.append({"seq": seq, "window": result.round_windows[seq]})
    return out


def _expr_literal(val) -> str:
    """Render *val* as a PYTHON LITERAL for an inline `stream:` predicate.

    A `stream:` assert is interpolated and then `eval`ed, so a token has to come out as something
    Python can read. Substituting the bare value works for numbers and breaks for everything else:
    `{facts.speed_before} != {facts.speed_after_on}` became `NORMAL != TURBO`, two undefined names,
    and the assert died with `NameError: name 'NORMAL' is not defined` instead of answering. Authors
    hit this repeatedly and worked around it by quoting the token in the YAML
    (`'{facts.speed_after_on}' != ...`), which is easy to forget and easy to get wrong.

    Numeric-looking strings stay unquoted so arithmetic still works (`{facts.bet_b} / 20`).
    """
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "True" if val else "False"
    if isinstance(val, (int, float)):
        return repr(val)
    if isinstance(val, (list, tuple, dict, set)):
        # A CONTAINER must compare as a container. Falling through to the string branch below
        # rendered `[]` as `'[]'`, so `{facts.illegal_reels} == []` became `'[]' == []` -- false
        # for a clean audit as well as a dirty one, i.e. permanently red.
        return repr(val)
    s = str(val)
    try:
        float(s)                     # "0.6" -> stays a number, so comparisons and maths work
        return s
    except ValueError:
        pass
    if s in ("True", "False", "None"):
        return s
    return repr(s)                   # NORMAL -> 'NORMAL'


def resolve_tokens(text: str, result: ScenarioResult, for_expr: bool = False):
    """Substitute {res.payout} etc. with the raw response value. Returns the interpolated string.

    `for_expr` renders each value as a Python literal instead of its plain text, for the inline
    `stream:` predicates that get `eval`ed (see `_expr_literal`). It is OFF by default because the
    other caller is prompt text, where a quoted `'NORMAL'` would be shown to the AI verbatim.

    A token the YAML already wrapped in quotes is left bare even under `for_expr`, so the
    hand-quoted workarounds still in the suites (`'{facts.speed_after_on}' != ...`) keep working
    rather than turning into `''NORMAL''`.
    """
    ctx = _binding_ctx(result)

    def render(val, m):
        if not for_expr:
            return "null" if val is None else str(val)
        txt, i, j = m.string, m.start(), m.end()
        quoted = (txt[i - 1] if i > 0 else "") in "'\"" and (txt[j] if j < len(txt) else "") in "'\""
        if quoted:                                   # the author already supplied the quotes
            return "null" if val is None else str(val)
        return _expr_literal(val)

    def sub(m):
        base, idx, field_ = m.group(1), m.group(2), m.group(3)
        if base == "facts":                                  # scenario-exposed values
            return render(result.facts.get(field_), m)
        obj = ctx.get(base)
        if obj is None:
            return m.group(0)
        if idx is not None and isinstance(obj, list):
            obj = obj[int(idx)]
        val = None
        if isinstance(obj, Bet):
            val = obj.raw.get(field_)               # raw server field first (e.g. payout, freeSpins)
            if val is None:
                val = getattr(obj, field_, None)    # fall back to a computed field (e.g. cascades, phase)
        return render(val, m)

    return _TOKEN.sub(sub, text)


# The only names an inline `stream:` predicate may call. `__builtins__` stays empty — a suite is
# trusted, but an accidental `open(...)` in a predicate should still be a NameError rather than a
# file read. `abs` earns its place: a money assert against a computed value needs a tolerance
# (`abs(stake - bet * 75) < 0.005`), because `bet * 75` is inexact at bets like 0.01 and an `==`
# would red a game that charged correctly.
_PREDICATE_NAMES = {"abs": abs, "min": min, "max": max, "round": round, "len": len,
                    "sum": sum, "int": int, "float": float, "sorted": sorted,
                    "all": all, "any": any, "set": set}


def eval_inline_stream(expr: str, result: ScenarioResult) -> dict:
    """A one-off `stream:` predicate like '{terminal.freeSpins} == 0'."""
    interpolated = resolve_tokens(expr, result, for_expr=True)
    try:
        ok = bool(eval(interpolated, {"__builtins__": {}},      # trusted test files only
                       dict(_PREDICATE_NAMES)))
    except Exception as e:
        return {"valid": False, "violations": [f"could not evaluate `{expr}` -> `{interpolated}`: {e}"]}
    return {"valid": ok, "violations": [] if ok else [f"`{expr}` -> `{interpolated}` is false"]}


def run_stream_assert(spec: str, result: ScenarioResult) -> dict:
    """spec is either a registered invariant name (optionally name(args)) or an inline predicate."""
    # Digits allowed after the first character: `p1_reels_only` / `p1_ways_payout` are real
    # invariant names. With `[a-zA-Z_]+` they never matched, so they fell through to the
    # inline-predicate branch and `eval`ed as a bare Python name -> NameError -> permanent
    # red, whatever the game did. Fixed 2026-09-05 alongside the same flaw in `_TOKEN`.
    m = re.fullmatch(r"([a-zA-Z_][a-zA-Z_0-9]*)(?:\((.*)\))?", spec.strip())
    if m and m.group(1) in INVARIANTS:
        return INVARIANTS[m.group(1)](result.rounds)
    return eval_inline_stream(spec, result)
