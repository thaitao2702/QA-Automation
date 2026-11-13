/**
 * Insufficient balance, the auto-play variants, the tutorial, spin input / turbo, reconnect, the
 * second session and free spins on an empty wallet. Port of scenario_engine.py lines 3296-4486
 * (`spin_balance_needs_stake` .. `FreespinZeroBalance`).
 *
 * `GAME.button(...)!` below: a role this build lacks resolves to null, and `clickButton` /
 * `holdButton` refuse a null name exactly as the Python did with None (raise, or false when
 * `optional`). The `!` only satisfies the type; it does not change what happens.
 */
import { type Dict, get, or, py, pyEq, pyFloat, pyInt, pyRound, pyStr, PyTypeError, PyValueError, r, sum, truthy } from "../py.js";
import { Bet, BetRejected, isFreespinTrigger, isTerminal } from "../engine/bet.js";
import { awaitBetResponse, drainToBase, waitSpinClickable } from "../engine/bridge.js";
import type { DriveContext } from "../engine/drive.js";
import { autoStopButton, baseRoundsSince, confirmStopped, finishRound, playOutBonus, playOutFreespins, SPEED_JS,
  spinAndFinish } from "../engine/flows.js";
import { GAME } from "../engine/game.js";
import { sleep } from "../engine/page.js";
import { ScenarioResult } from "../engine/result.js";
import { arrivedAt, autoNext } from "../engine/shared.js";
import { checkParams, type Params, param, Scenario, type ScenarioClass } from "./base.js";

// ── Local Python-semantics helpers (not in py.ts) ─────────────────────────────────────

/** `str(e)` for a caught exception, as an f-string `{e}` prints it. */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : pyStr(e);
}

/** Python's IndexError. */
class PyIndexError extends Error {
  override name = "IndexError";
}

/** `xs[i]` on a list: a negative index counts from the end, out of range raises IndexError. */
function pyIndex<T>(xs: readonly T[], i: number): T {
  if (!Number.isInteger(i)) throw new PyTypeError("list indices must be integers or slices, not float");
  const j = i < 0 ? xs.length + i : i;
  if (j < 0 || j >= xs.length) throw new PyIndexError("list index out of range");
  return xs[j];
}

/** Python `a % n` (the result takes the sign of `n`). */
function pyMod(a: number, n: number): number {
  return ((a % n) + n) % n;
}

/** `asyncio.create_task(coro)`, reduced to what a watcher needs: the promise and `task.done()`. */
function startTask<T>(p: Promise<T>): { promise: Promise<T>; done: () => boolean } {
  let finished = false;
  p.then(() => { finished = true; }, () => { finished = true; });   // also marks a rejection handled
  return { promise: p, done: () => finished };
}

/** `AutoPlay._COUNT_INDEX` (auto:count:<idx> -> spins). AutoPlay lives in another scenario group. */
const AUTO_PLAY_COUNT_INDEX: ReadonlyMap<number, number> = new Map([[10, 0], [20, 1], [30, 2], [50, 3]]);

// ── Insufficient balance ──────────────────────────────────────────────────────────────

/** Is the balance still enough to place the SMALLEST bet? Then the wallet alone has not created
 * an insufficient state and the stake has to be raised above what is left. */
export function spinBalanceNeedsStake(balance: number, minBet: number): boolean {
  return balance >= minBet;
}

/**
 * Decide how to force a balance-below-stake state, given that Playsite moves only WHOLE currency
 * units (so an integer withdraw preserves the balance's fractional part).
 *
 *   ["wallet", remainder] -> withdraw floor(balance); the balance becomes `remainder` (its fractional
 *                            part). Whether `remainder` is already below `minBet` decides which stake
 *                            the caller then spins at, but EITHER WAY the withdrawal is the right
 *                            first move: it is what brings a balance of 29,951.45 into the range
 *                            where any denomination is unaffordable.
 *   ["bet", null]         -> nothing to withdraw (balance < 1) -> the caller must RAISE the stake
 *                            above the balance instead.
 *
 * Previously this returned "bet" whenever `remainder >= minBet`, which threw away the withdrawal
 * entirely — and the caller then looked for a denomination above the FULL balance, found none (max
 * bet 100 vs balance 29,951.45) and gave up. Withdrawing first and choosing the stake against what
 * is LEFT combines the two strategies instead of treating them as exclusive.
 */
export function planInsufficient(balance: number, minBet: number): ["wallet", number] | ["bet", null] {
  // `minBet` is unused, as in the Python (see the docstring: the caller decides the stake).
  const whole = Math.floor(balance);
  if (whole >= 1) return ["wallet", pyRound(balance - whole, 2)];
  return ["bet", null];
}

export class InsufficientBalance extends Scenario {
  readonly name = "insufficient-balance";
  readonly description = "Force a balance-below-stake state, then attempt a spin — the client should block it "
    + "with an out-of-money popup. Tries the WALLET first (a whole-unit Playsite withdrawal "
    + "down below the min bet); if the balance can't be shrunk that far, RAISES the stake "
    + "above the balance instead. Restores the wallet/bet at the end. Needs PLAYSITE_* config "
    + "for the wallet path.";
  static readonly PARAMS: readonly string[] = [];

  constructor(p: Params = {}) {
    super();
    checkParams(InsufficientBalance, p);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const info = await ctx.eval(`() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const num = (x) => (typeof x === 'number' && !isNaN(x)) ? x : null;
            return { balance: num(b.getBalance ? b.getBalance() : (s ? s.balance : null)),
                     denoms: (s && s.denominations) || [],
                     betAmount: num(b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)) }; }`);
    const balanceRead = get(info, "balance");
    let curBet: unknown = get(info, "betAmount");
    const denoms = (or(get(info, "denoms"), []) as unknown[]).map((d) => pyFloat(d));
    if (balanceRead === null || !denoms.length) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "need balance + denominations from the bridge to plan an insufficient state" });
    }
    let balance = pyFloat(balanceRead);
    // Work by VALUE, not array position: the bridge maps denominations[i] <-> bet:row:i, but don't
    // assume the array is sorted -- read the min as a value and keep each denom's index.
    const orig = balance;
    const minBet = Math.min(...denoms);
    const [mode, remainder] = planInsufficient(balance, minBet);

    let chosenBet = minBet;
    if (mode === "wallet") {
      try {
        await ctx.setBalance(remainder);       // withdraw floor(balance) -> balance = remainder
        await ctx.reloadAndStart();            // client re-reads the reduced balance
        // ⚠ RE-READ THE BET. The reload does not keep the bet the previous run left: a wallet of
        // ~0.24 reopened at 0.10 where base-win had left 7.00. Judging "already insufficient" on the
        // pre-reload 7.00 made this spin at 0.10 -- affordable, so no out-of-money popup, and TC-008
        // red on a game that behaved correctly (2026-09-29).
        curBet = (await ctx.eval(
          "() => { const b = window.__gameTestBridge; const s = b.getSnapshot ? b.getSnapshot() : {};"
          + " return b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null); }")) ?? null;
      } catch (e) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
          { reason: `could not set balance: ${errText(e)}` });
      }
      balance = remainder;                     // everything below judges the REMAINING money
    }

    if (spinBalanceNeedsStake(balance, minBet)) {
      // The wallet could not go below the minimum bet on its own (Playsite moves whole units, so a
      // 0.45 remainder survives a 29,951 withdrawal while the min bet is 0.20). Raise the STAKE above
      // what is left instead: pick the smallest denomination strictly greater than the balance, and
      // use its own array index for bet:row (correct whether or not the array is sorted).
      const above = denoms.map((d, i): [number, number] => [d, i]).filter(([d]) => d > balance)
        .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      if (!above.length) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
          { reason: py`balance ${pyRound(balance, 2)} >= max bet ${Math.max(...denoms)} `
            + "and not reducible with whole-unit withdrawals" });
      }
      let idx: number;
      [chosenBet, idx] = above[0];
      if (curBet !== null && pyFloat(curBet) > balance) {
        // Already insufficient at the CURRENT stake -> don't touch the bet (re-selecting the active
        // row is a no-op the bridge never lets resolve). Just spin.
        chosenBet = pyFloat(curBet);
      } else {
        await ctx.clickButton(GAME.button("stake_up")!);                   // open the denomination list
        await ctx.clickButton(GAME.button("bet_row", { i: idx })!);         // pick that stake
        await ctx.clickButton(GAME.button("bet_close")!, { optional: true });
      }
    }
    const spinBalance = pyRound(balance, 2);

    // attempt a spin -> should be refused; hold so the out-of-money popup is in frame
    await ctx.clickButton(GAME.button("spin")!, { optional: true, dwellMs: 2000 });
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
      facts: { mode, spin_balance: spinBalance, chosen_bet: chosenBet, min_bet: minBet, orig_balance: pyRound(orig, 2) },
    });
  }
}

// ---------------------------------------------------------------------------
// Auto-play variants + Tutorial (Mah_010/015/016/017/018/020, Mah_057..063).
// These drive extra bridge affordances (auto:stop / auto:stopAtWin / auto:count:4=Infinity /
// info popup) that are documented but NOT yet exercised live — treat their outcomes as
// provisional until a first live run confirms the button behaviour.
// (`_auto_next` / `_arrived_at` live in engine/shared as `autoNext` / `arrivedAt`.)
// ---------------------------------------------------------------------------

const STOP_ON_KNOWN = ["any", "base_win", "freespin"];

export class AutoStopAtWin extends Scenario {    // Mah_015
  readonly name = "auto-stop-at-win";
  readonly description = "Enable 'stop at any win', run auto with an unlimited count so ONLY a win stops it, "
    + "and observe base spins until the first win. A win comes in TWO SHAPES and they "
    + "look nothing alike on screen, so `stop_on` picks which one this run is about:\n"
    + "  base_win -- the spin itself paid; the clip is one spin and a win presentation.\n"
    + "  freespin -- the spin awarded a FREE-SPIN FEATURE, which is then drained; the "
    + "triggering spin shows an award, not a line win, and the money arrives across the "
    + "bonus.\n"
    + "  any (default) -- whichever lands first. Fine for a smoke check, but a case "
    + "written for it cannot phrase a prompt: it does not know what will be on screen.\n"
    + "A stop of the wrong shape is DISCARDED — auto is restarted and the hunt resumes — "
    + "so a run is deterministic in what it captures (facts.discarded_stops counts them). "
    + "Pair `stop_on: freespin` with an easy-trigger paytable and a max-win cap, or the "
    + "hunt is long and the bonus is unbounded.\n"
    + "facts.won_payout is the WHOLE event: the spin's payout for a base win, the sum "
    + "across the drained bonus for a feature (the trigger round pays 0, so reporting its "
    + "payout published 0.0 as 'the win'). facts.won_via says which shape it was.\n"
    + "Marks `win` (the round that ended auto) and `win_end` (that event's last round, "
    + "whose window runs through the stop and the idle tail) — span them with "
    + "`at: [win, win_end]` to film trigger -> feature -> back to base. `tail_dwell_ms` "
    + "adds padding after the stop so the idle base screen is unmistakably in frame. "
    + "Uses auto:stopAtWin / auto:count:4 (Infinity).\n"
    + "Mark `lead_in` is a MOMENT, not a round: up to `lead_in_rounds` (3) auto spins "
    + "before the win, or the auto:confirm click when fewer were played — span "
    + "`at: [lead_in, win_end]` to show auto running BEFORE it stopped. "
    + "`min_spins_before_win` restarts auto when the win comes sooner than that many "
    + "non-winning auto spins, so the clip has spins to show (facts.spins_before_win / "
    + "lead_in_rounds / lead_in_from_auto_start).";
  static readonly PARAMS = ["max_spins", "watch_ms", "dwell_ms", "stop_on", "max_attempts", "tail_dwell_ms",
    "lead_in_rounds", "min_spins_before_win"];

  readonly maxSpins: number;
  readonly watchMs: number;
  readonly dwellMs: number;
  readonly stopOn: string;               // which WIN SHAPE this run is about
  readonly maxAttempts: number;          // restarts allowed when the stop is the other shape
  readonly tailDwellMs: number;          // extra hold on the idle base screen after the stop
  readonly leadInRounds: number;         // auto spins before the win `lead_in` shows
  readonly minSpinsBeforeWin: number;    // fewer non-winning auto spins -> restart

  constructor(p: Params = {}) {
    super();
    checkParams(AutoStopAtWin, p);
    this.maxSpins = param(p, "max_spins", 200);
    this.watchMs = param(p, "watch_ms", 6000);
    this.dwellMs = param(p, "dwell_ms", 3000);
    const stopOn = param(p, "stop_on", "any");
    if (!STOP_ON_KNOWN.includes(stopOn)) {
      throw new PyValueError(py`unknown stop_on ${r(stopOn)} -- known: ${STOP_ON_KNOWN.join(", ")}`);
    }
    this.stopOn = stopOn;
    this.maxAttempts = param(p, "max_attempts", 6);
    this.tailDwellMs = param(p, "tail_dwell_ms", 0);
    this.leadInRounds = param(p, "lead_in_rounds", 3);
    this.minSpinsBeforeWin = param(p, "min_spins_before_win", 0);
  }

  /**
   * Open the auto panel, enable stop-at-any-win, pick the unlimited count, confirm. Returns the clip
   * time just before the confirm click -- where the client starts spinning on its own.
   *
   * The confirm click does NOT dwell. The client fires its first spin ON that click, and the default
   * 2 s dwell let the whole spin play and settle before the framework was listening: its response
   * was then taken from the bridge's buffer, and the round's clip window opened after the result was
   * already on screen (Dragon VIP DV-15, 2026-09-15: spin at ~13.1 s, window from 15.4 s). Listening
   * at once puts the window where the spin is.
   */
  private async startAuto(ctx: DriveContext): Promise<number> {
    await ctx.clickButton(GAME.button("auto")!);
    await ctx.clickButton(GAME.button("auto_stop_at_win")!);                              // enable stop-at-any-win
    await ctx.clickButton(GAME.button("auto_count", { i: GAME.AUTO_COUNTS.length - 1 })!);  // Infinity
    ctx.mark("AUTO CONFIRMED");
    const confirmedAt = ctx.nowS();
    await ctx.clickButton(GAME.button("auto_confirm")!, { dwellMs: 0 });
    return confirmedAt;
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const rounds: Bet[] = [];
    let win: Bet | null = null;
    let winEnd: Bet | null = null;
    let wonVia = "none";
    let wonPayout = 0.0;
    const discarded: string[] = [];
    let leadInAt: number | null = null;
    let spinsBeforeWin = 0;
    let leadIn = 0;

    for (let attempt = 0; attempt < this.maxAttempts; attempt++) {
      const confirmedAt = await this.startAuto(ctx);
      const spins: Bet[] = [];                // THIS attempt's auto base spins, in order
      let got: [string, number, Bet] | null = null;
      for (let i = 0; i < this.maxSpins; i++) {
        const before = rounds.length;
        const b = await autoNext(ctx, rounds);
        spins.push(b);
        ctx.mark(py`AUTO SPIN ${spins.length}: payout ${b.payout}`);
        if (isFreespinTrigger(b)) {
          // THE TRIGGER ROUND ITSELF PAYS NOTHING. The award is free spins; the money arrives across
          // the rounds `autoNext` just drained. Sum the whole event.
          got = ["freespin", pyRound(sum(rounds.slice(before).map((x) => x.payout)), 6), b];
          break;
        }
        if (b.payout > 0) {
          got = ["base_win", b.payout, b];
          break;
        }
      }
      if (got === null) break;                 // budget spent without any win at all
      const [via, amount, b] = got;
      if (["any", via].includes(this.stopOn) && spins.length - 1 >= this.minSpinsBeforeWin) {
        [wonVia, wonPayout, win, winEnd] = [via, amount, b, rounds[rounds.length - 1]];
        spinsBeforeWin = spins.length - 1;
        leadIn = Math.min(this.leadInRounds, spinsBeforeWin);
        // Where the lead-in starts. Every auto round but the first is listened for BEFORE it is
        // fired, so its response arrives as that spin starts; the first one's may have been
        // buffered, so a lead-in reaching back to it starts at the confirm click.
        leadInAt = leadIn === spinsBeforeWin ? confirmedAt
          : Math.max(confirmedAt, arrivedAt(ctx, spins[spinsBeforeWin - leadIn]) - 1.0);
        break;
      }
      // The other shape -- or the right one before auto had played enough spins to be SEEN running.
      // Auto has stopped either way (that is what stop-at-win does), so the run cannot simply keep
      // spinning: settle back to base and START AUTO AGAIN to keep hunting.
      discarded.push(!["any", via].includes(this.stopOn) ? via : "too_early");
      await ctx.settle();
    }

    ctx.mark("WIN LANDED");
    const stop = await confirmStopped(ctx, { watchMs: this.watchMs, dwellMs: this.dwellMs, label: "AUTO STOPPED at the win" });
    const stopped = stop.stopped;
    // DO NOT stop auto here when the game failed to stop itself.
    //
    // This used to click auto:stop as a tidy-up. It made the CLIP LIE: the video window runs to the
    // end of the scenario, so the footage the AI grades would show auto halting — because the
    // framework halted it — and the prompt's "did auto stop on screen?" came back true on a game that
    // had just failed the very behaviour under test. The stream assert still went red (`stopped` is
    // read before any click), so the verdict was never wrong; but the video was evidence for the
    // opposite conclusion, which is the false-green shape Rule 10 is about.
    //
    // Nothing is left running: cleanup calls `stopAutoplay` once the asserts are graded, which is the
    // right place for repair -- after the evidence is captured, not inside it.
    if (truthy(this.tailDwellMs)) {
      // Padding so the clip ends on the game plainly IDLE — after a feature the last thing the AI
      // sees would otherwise be the bonus outro, and "is auto still running?" is being asked of a
      // screen that is still animating.
      //
      // When auto did NOT stop, the tail is still recorded -- it is the proof. The mark says so, so
      // the action timeline does not caption a still-spinning game as sitting still.
      ctx.mark(truthy(stopped) ? "IDLE TAIL -- base game sitting still after the win"
        : "TAIL -- auto did NOT stop at the win; still running");
      await ctx.dwell(this.tailDwellMs);
    }
    if (win === null) {
      let reason = py`no win within ${this.maxSpins} auto spins`;
      if (discarded.length) {
        reason = py`no ${this.stopOn} win in ${this.maxAttempts} attempt(s): the stop was `
          + py`${discarded.join(", ")} instead ('too_early' = a win before `
          + py`${this.minSpinsBeforeWin} non-winning auto spin(s)). For `
          + "stop_on=freespin use an easy-trigger paytable; for stop_on=base_win do NOT.";
      }
      return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
        facts: { stop_on: this.stopOn, won_via: "none", discarded_stops: discarded.length },
        reason,
      });
    }
    // `win` is the round that ENDED auto; `winEnd` is the last round of that event -- the same round
    // for a base win, the terminal free spin for a feature. A suite spans them (`at: [win, win_end]`)
    // to get the whole win AND the stop: the last round's window runs to the end of the scenario (see
    // DriveContext.windows), so it includes confirmStopped's hold and the idle tail. Marking only
    // `win` clipped a free-spin stop to the trigger spin, which shows no win at all -- the AI
    // reported win_shown=false, correctly.
    return new ScenarioResult(this.name, true, win, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      marks: { win: win.seq, win_end: (winEnd as Bet).seq, lead_in: { t: pyRound(leadInAt as number, 3) } },
      facts: {
        spins_to_win: rounds.length, won_payout: wonPayout,
        // auto base spins this attempt played before the winning one, and how many of them the
        // `lead_in` mark puts on the clip
        spins_before_win: spinsBeforeWin,
        lead_in_rounds: leadIn,
        lead_in_from_auto_start: leadIn === spinsBeforeWin,
        min_spins_before_win: this.minSpinsBeforeWin,
        won_via: wonVia,            // base_win | freespin
        // A bare `{facts.won_via} == freespin` cannot be asserted: the inline stream evaluator
        // interpolates the VALUE into an expression, so it becomes `freespin == freespin` ->
        // NameError. Assert this boolean instead; keep won_via for the prompt text.
        won_by_feature: wonVia === "freespin",
        stop_on: this.stopOn,
        trigger_payout: win.payout,  // 0.0 when the win was a feature
        feature_rounds: wonVia === "freespin" ? rounds.length - 1 - rounds.indexOf(win) : 0,
        discarded_stops: discarded.length,
        ...stop, auto_stopped: stopped,
      },
    });
  }
}

export class AutoUserStop extends Scenario {    // Mah_017
  readonly name = "auto-user-stop";
  readonly description = "Start auto with an unlimited count, let a few spins play, then the USER stops it "
    + "(auto:stop). Exposes facts.spins_before_stop / auto_stopped for the check that auto "
    + "halts immediately on demand.";
  static readonly PARAMS = ["spins_before_stop", "watch_ms", "dwell_ms"];

  readonly spinsBeforeStop: number;
  readonly watchMs: number;
  readonly dwellMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(AutoUserStop, p);
    this.spinsBeforeStop = param(p, "spins_before_stop", 3);
    this.watchMs = param(p, "watch_ms", 6000);
    this.dwellMs = param(p, "dwell_ms", 3000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    await ctx.clickButton(GAME.button("auto")!);
    await ctx.clickButton(GAME.button("auto_count", { i: GAME.AUTO_COUNTS.length - 1 })!);   // Infinity so it won't stop on its own
    await ctx.clickButton(GAME.button("auto_confirm")!);
    const rounds: Bet[] = [];
    for (let i = 0; i < this.spinsBeforeStop; i++) await autoNext(ctx, rounds);
    ctx.mark(`USER STOPS auto after ${rounds.length} spins`);
    await ctx.clickButton(autoStopButton()!, { dwellMs: 2000 });   // user stops auto
    // "it halted on demand" is a negative claim -- watch, then hold, so it is proved and filmed
    const stop = await confirmStopped(ctx, { watchMs: this.watchMs, dwellMs: this.dwellMs, label: "AUTO STOPPED by the user" });
    return new ScenarioResult(this.name, true, rounds.length ? rounds[rounds.length - 1] : null, rounds, ctx.timeline,
      ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: { spins_before_stop: rounds.length, ...stop, auto_stopped: stop.stopped },
      });
  }
}

export class AutoReloadCancels extends Scenario {    // Mah_018
  readonly name = "auto-reload-cancels";
  readonly description = "Start auto, let a spin or two play, then RELOAD the game; auto should be cancelled "
    + "on re-entry. Exposes facts.spins_before_reload / auto_cancelled for the check that "
    + "auto does not resume after a reload.";
  static readonly PARAMS = ["spins_before_reload", "watch_ms", "dwell_ms"];

  readonly spinsBeforeReload: number;
  // A reload lands on a FRESH, idle game, which looks exactly like "auto was cancelled" and exactly
  // like "the clip just ended". Watch for a bet and then hold, so the stillness is evidence rather
  // than the recording running out (Rule 4).
  readonly watchMs: number;
  readonly dwellMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(AutoReloadCancels, p);
    this.spinsBeforeReload = param(p, "spins_before_reload", 2);
    this.watchMs = param(p, "watch_ms", 6000);
    this.dwellMs = param(p, "dwell_ms", 3000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    await ctx.clickButton(GAME.button("auto")!);
    await ctx.clickButton(GAME.button("auto_count", { i: GAME.AUTO_COUNTS.length - 1 })!);
    await ctx.clickButton(GAME.button("auto_confirm")!);
    const rounds: Bet[] = [];
    for (let i = 0; i < this.spinsBeforeReload; i++) await autoNext(ctx, rounds);
    ctx.mark(`RELOAD after ${rounds.length} auto spins`);
    await ctx.reloadAndStart();                   // reload should cancel auto
    // The reload is supposed to CANCEL auto. Proving that needs the same negative evidence: nothing
    // further wagered, the client's flag off, and footage of the game sitting still.
    const stop = await confirmStopped(ctx, { watchMs: this.watchMs, dwellMs: this.dwellMs, label: "AUTO CANCELLED by the reload" });
    const cancelled = stop.stopped;
    return new ScenarioResult(this.name, true, rounds.length ? rounds[rounds.length - 1] : null, rounds, ctx.timeline,
      ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: { spins_before_reload: rounds.length, ...stop, auto_cancelled: cancelled },
      });
  }
}

export class AutoChangeBetFirst extends Scenario {    // Mah_020
  readonly name = "auto-change-bet-first";
  readonly description = "Change the bet to a specific denomination FIRST, then start auto for N spins; every "
    + "auto spin must use the chosen stake. Exposes res = the first auto spin + "
    + "facts.picked_bet for the response check {res.stake} == picked_bet.";
  static readonly PARAMS = ["count", "target_index"];

  readonly count: number;
  readonly targetIndex: number | null;

  constructor(p: Params = {}) {
    super();
    checkParams(AutoChangeBetFirst, p);
    this.count = param(p, "count", 10);
    this.targetIndex = param<number | null>(p, "target_index", null);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const info = await ctx.eval(`() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return { denoms: (s && s.denominations) || [],
                     betIndex: (b.getBetIndex ? b.getBetIndex() : -1) }; }`);
    const denoms = (or(get(info, "denoms"), []) as unknown[]).map((d) => pyFloat(d));
    if (!denoms.length) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "no denominations from the bridge" });
    }
    const cur = get(info, "betIndex");
    const n = denoms.length;
    let target = this.targetIndex !== null ? this.targetIndex : (n > 3 ? 3 : 0);
    if (cur !== null && pyEq(cur, target)) {        // the active row isn't re-selectable
      target = pyMod(target + 1, n);
    }
    const picked = pyIndex(denoms, target);
    await ctx.clickButton(GAME.button("stake_up")!);                 // dwells ~3s
    await ctx.clickButton(GAME.button("bet_row", { i: target })!);    // dwells ~3s
    await ctx.clickButton(GAME.button("bet_close")!, { optional: true });
    const idx = AUTO_PLAY_COUNT_INDEX.get(this.count) ?? 0;
    await ctx.clickButton(GAME.button("auto")!);
    await ctx.clickButton(GAME.button("auto_count", { i: idx })!);
    await ctx.clickButton(GAME.button("auto_confirm")!);
    const rounds: Bet[] = [];
    let base = 0;
    while (base < this.count) {
      await autoNext(ctx, rounds);
      base += 1;
    }
    await ctx.settle();
    const first = rounds.find((x) => x.phase === "auto") ?? (rounds.length ? rounds[0] : null);
    return new ScenarioResult(this.name, true, first, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      facts: { picked_bet: picked, picked_index: target, auto_count: this.count },
    });
  }
}

export class AutoUntilEmpty extends Scenario {    // Mah_010 / Mah_016
  readonly name = "auto-until-empty";
  readonly description = "FUND the wallet for ~fund_spins spins at the current stake, then run auto with an "
    + "unlimited count until the balance runs out; the client should auto-STOP and show the "
    + "out-of-money popup. Sets the wallet to EXACTLY fund_spins x stake (the Playsite "
    + "API takes fractional amounts) to keep the run short -- but the END CONDITION is "
    + "the server balance falling below one bet, checked after every round, NOT a spin "
    + "count: an auto spin can win, and a free-spin trigger pays across a bonus, so the "
    + "wallet is not necessarily empty after fund_spins spins. Then confirms no further "
    + "round is wagered and holds on the stopped screen for the video. Exposes "
    + "facts.funded_balance / bet / spins_played / final_balance / balance_below_bet / "
    + "winning_rounds / stopped_on_empty; reached only if it really ran out. "
    + "Needs PLAYSITE_* config.";
  static readonly PARAMS = ["fund_spins", "cap", "watch_ms", "dwell_ms"];

  readonly fundSpins: number;      // ~how many spins to fund before the wallet runs dry
  readonly cap: number;
  readonly watchMs: number;
  readonly dwellMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(AutoUntilEmpty, p);
    this.fundSpins = param(p, "fund_spins", 2);
    this.cap = param(p, "cap", 200);
    this.watchMs = param(p, "watch_ms", 6000);
    this.dwellMs = param(p, "dwell_ms", 3000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // Size the drain off the ACTIVE stake, read from the bridge. The wallet target below is an
    // absolute figure (fund_spins x stake), so the current balance is not needed to compute it.
    const info = await ctx.eval(`() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const num = (x) => (typeof x === 'number' && !isNaN(x)) ? x : null;
            return { bet: num(b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)),
                     denoms: (s && s.denominations) || [] }; }`);
    const denoms = (or(get(info, "denoms"), []) as unknown[]).map((d) => pyFloat(d));
    const betRead = truthy(get(info, "bet")) ? get(info, "bet") : (denoms.length ? Math.min(...denoms) : null);
    if (ctx.wallet === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "wallet not configured -- cannot fund a deterministic drain (set PLAYSITE_*)" });
    }
    if (!truthy(betRead)) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "no active stake / denominations from the bridge to size the drain" });
    }
    const bet = pyFloat(betRead);
    let funded: number;
    try {
      // EXACTLY fund_spins bets. This used to round the delta to a whole currency unit, because the
      // helper claimed Playsite could only move whole units -- it cannot only do that, it takes
      // fractional amounts and settles them to the cent (verified live to 0.001 on 2026-09-03). The
      // rounding made the funded balance an accident of whatever the wallet happened to hold: from
      // 11840.99 it funded 0.99, which is ONE spin at a 0.6 stake, not the two that were asked for.
      // Ask for the number we actually want.
      funded = pyRound(this.fundSpins * bet, 2);
      // A long retry window, because the server refuses to shrink the balance for a while after the
      // game has been playing -- it reports "Insufficient balance" against a balance that never
      // moves, and it clears on its own. Funding runs right after load, which is inside that window.
      // Waiting is safe here: auto has not been started yet, so nothing is spinning while the retry
      // loop sleeps.
      await ctx.setBalance(funded, 10, 2.0);
      await ctx.reloadAndStart();                // client re-reads the funded balance
    } catch (e) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: `could not fund the wallet for a drain: ${errText(e)}` });
    }

    await ctx.clickButton(GAME.button("auto")!);
    await ctx.clickButton(GAME.button("auto_count", { i: GAME.AUTO_COUNTS.length - 1 })!);
    await ctx.clickButton(GAME.button("auto_confirm")!);
    const rounds: Bet[] = [];
    // WATCH THE BALANCE, do not count spins.
    //
    // Funding `fund_spins x bet` does NOT mean the wallet is empty after that many spins: an auto
    // spin can WIN, which tops the balance back up, and a free-spin trigger pays across a whole
    // bonus. Treating "n spins have happened" as "the money is gone" would declare the drain
    // finished while the player could still afford to bet -- and the out-of-money popup the case is
    // about would never have been the reason auto stopped.
    //
    // So the funded amount is only a STARTING POINT that keeps the run short, and the actual end
    // condition is read from the server's own figure after each round: `b.balance` is the balance
    // with the stake debited and any payout credited. The drain is over when that drops below one
    // bet, whenever that happens to be.
    let belowBet = false;
    let winningRounds = 0;
    let finalBalance: number | null = null;
    for (let i = 0; i < this.cap; i++) {
      let b: Bet;
      try {
        b = await ctx.observeBet();
      } catch {
        break;                                   // no next auto spin came
      }
      rounds.push(b);
      if (isFreespinTrigger(b)) {
        // The bonus pays into the balance across its rounds, so the figure that matters is the LAST
        // round of the feature, not the trigger.
        const fs = await playOutBonus(ctx, b, { settle: false });
        rounds.push(...fs.slice(1));
        b = rounds[rounds.length - 1];
      }
      if (truthy(b.payout)) winningRounds += 1;
      finalBalance = b.balance;
      if (finalBalance !== null && finalBalance < bet) {
        belowBet = true;         // cannot afford another spin -- this is the real end
        break;
      }
    }
    // Now the NEGATIVE evidence and the footage. confirmStopped watches for a further wagered round
    // (there must be none) and then HOLDS on the stopped screen, because a couple of seconds is not
    // enough for the AI to see that the game is out of money and sitting still.
    ctx.mark("BALANCE BELOW ONE BET");
    const stop = await confirmStopped(ctx, { watchMs: this.watchMs, dwellMs: this.dwellMs, label: "AUTO STOPPED, out of money" });
    // Both halves are required: the money really ran out, AND nothing was wagered after it did.
    const stoppedOnEmpty = belowBet && !truthy(get(stop, "extra_round_after_stop"));
    let reason: string | null;
    if (!belowBet) {
      reason = py`the balance never fell below one bet (${bet}) within ${this.cap} auto spins -- `
        + py`${winningRounds} of ${rounds.length} round(s) paid, leaving ${finalBalance}. `
        + "Auto stopping here would not have been an out-of-money stop.";
    } else if (!stoppedOnEmpty) {
      reason = py`a further round was wagered after the balance fell below one bet (${finalBalance})`;
    } else {
      reason = null;
    }
    return new ScenarioResult(this.name, stoppedOnEmpty, rounds.length ? rounds[rounds.length - 1] : null, rounds,
      ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: {
          funded_balance: funded, bet, fund_spins: this.fundSpins,
          spins_played: rounds.length, stopped_on_empty: stoppedOnEmpty,
          // the server's final figure, and that it is under one bet -- the evidence that the stop was
          // about MONEY and not something else
          final_balance: finalBalance,
          balance_below_bet: belowBet,
          // wins are why a spin count cannot be trusted as the end condition
          winning_rounds: winningRounds,
          ...stop,
        },
        reason,
      });
  }
}

export class OpenTutorial extends Scenario {    // Mah_057 / 059 / 060
  readonly name = "open-tutorial";
  readonly description = "Open the in-game Info / Tutorial popup and BROWSE THE WHOLE GUIDE: dwell on the top of "
    + "the page, then wheel DOWN to the bottom one viewport at a time (90% steps, so no band "
    + "of the document falls between two shots), pausing at each screenful, and rest at the "
    + "end. The tutorial spans several screens, so a clip of only the first screen cannot "
    + "support 'the content/images are all correct' — this exposes every screenful in one "
    + "clip instead. The page is a CROSS-ORIGIN WebView: only a real mouse wheel can scroll "
    + "it (the bridge is blocked by the browser) and its scroll position cannot be read back, "
    + "so the step count is fixed and over-wheeling harmlessly clamps at the end. Marks "
    + "TUTORIAL TOP / BOTTOM for the ACTION TIMELINE. Exposes facts.tutorial_open (the "
    + "WebView was really there, not an empty popup), reached_bottom (the full planned scroll "
    + "was issued — that the pixels MOVED is a video claim, not a bridge one) and "
    + "scroll_steps. Set scroll_back=True to also page back UP (Mah_058 next/previous). "
    + "Unreached if the info button never becomes clickable.";
  static readonly PARAMS = ["dwell_s", "scroll_steps", "scroll_dwell_ms", "scroll_back", "shots"];

  readonly dwellS: number;           // hold on the FIRST screen before paging down
  readonly scrollSteps: number;      // fixed: bottom cannot be detected cross-origin
  readonly scrollDwellMs: number;    // hold per screenful so the AI sees each one
  readonly scrollBack: boolean;      // also page back up to the top (Mah_058)
  readonly shots: boolean;

  constructor(p: Params = {}) {
    super();
    checkParams(OpenTutorial, p);
    this.dwellS = param(p, "dwell_s", 2);
    this.scrollSteps = param(p, "scroll_steps", 10);
    this.scrollDwellMs = param(p, "scroll_dwell_ms", 2000);
    this.scrollBack = param(p, "scroll_back", false);
    this.shots = param(p, "shots", true);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const opened = await ctx.clickButton(GAME.button("info")!, { optional: true, dwellMs: ctx.stepDwellMs });   // open info/tutorial
    ctx.mark("TUTORIAL TOP: the guide is open at the top of the page");
    await ctx.dwell(pyInt(this.dwellS * 1000));                  // hold so the first screen is in frame
    const rect = opened ? ((await ctx.tutorialRect()) ?? null) : null;
    let down = 0;
    let up = 0;
    // Shoot each screenful as we page down. The tutorial cases that ask about TEXT (spelling,
    // translation) and IMAGE QUALITY (blurred, broken, wrong asset) are exactly the ones a
    // downsampled video montage cannot answer — they need a full-resolution still per screen, graded
    // on its own. Scrolling one step at a time (rather than handing the whole count to
    // scrollTutorial) is what makes a per-screen shot possible.
    if (opened && truthy(this.shots)) await ctx.screenshot("tutorial-01");
    // A build whose guide is a native popup rather than a cross-origin WebView reports no rect (Zeus:
    // HAS_TUTORIAL_WEBVIEW=False). Wheel the popup itself instead of skipping the scroll entirely —
    // otherwise every page after the first is simply never seen.
    if (truthy(rect) || opened) {
      for (let i = 0; i < this.scrollSteps; i++) {
        if (truthy(rect)) {
          down += await ctx.scrollTutorial({ steps: 1, dwellMs: this.scrollDwellMs });
        } else {
          down += await ctx.scrollPopup({ steps: 1, dwellMs: this.scrollDwellMs, label: "tutorial", fraction: 0.9 });
        }
        if (truthy(this.shots)) await ctx.screenshot(`tutorial-${String(i + 2).padStart(2, "0")}`);
      }
      ctx.mark("TUTORIAL BOTTOM: the guide has been paged to the end");
      await ctx.dwell(pyInt(this.dwellS * 1000));              // rest on the last screenful
      if (truthy(this.scrollBack)) {
        up = await ctx.scrollTutorial({ steps: this.scrollSteps, dwellMs: this.scrollDwellMs, up: true });
        ctx.mark("TUTORIAL TOP AGAIN: paged back up to the start");
        await ctx.dwell(pyInt(this.dwellS * 1000));
      }
    }
    await ctx.dismissPopups();                                   // close it so the next scenario is clean
    if (!opened) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "info/tutorial button not clickable" });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
      facts: {
        tutorial_open: truthy(rect), scroll_steps: down,
        reached_bottom: truthy(rect) && pyEq(down, this.scrollSteps),
        scrolled_back: up, dwell_s: this.dwellS,
        shot_count: ctx.shots.length,
        is_webview: truthy(rect),
        has_tutorial_webview: GAME.HAS_TUTORIAL_WEBVIEW,
      },
      shots: ctx.shots,
    });
  }
}

// ---------------------------------------------------------------------------
// Spin input, Turbo, Reconnect, FS-zero-balance, Reels-Glow (Mah_035-040 / 021-023 /
// 027-034 / 011 / 041-042). These drive real pointer/keyboard/network input and are mostly
// video-graded — timing-sensitive and LIVE-UNVERIFIED; treat first runs as smoke tests.
// ---------------------------------------------------------------------------
export class SpinInputs extends Scenario {    // Mah_035-040
  readonly name = "spin-inputs";
  readonly description = "All spin INPUT variants in one recording: spin via the Spin button (035); spin via "
    + "Space (036); hold-Spin -> continuous auto-spin, release stops it (037/039); hold-Spin "
    + "during a spin -> sudden stop (038); double-click -> sudden stop (040). One shared "
    + "video graded per case (expected_by_case).\n"
    + "Exposes facts.click_rounds / key_rounds / hold_done / dblclick_done / rounds_total "
    + "— the EXACT half of 'one input started exactly one spin', which no amount of "
    + "watching the reels settles reliably: a double-fire looks like one spin on video "
    + "but is two responses in the stream.\n"
    + "HOLD: the paid spins that start while the button is held are COUNTED "
    + "(facts.hold_rounds) and so are any after release (spins_after_release) -- "
    + "'holding starts auto-spin, releasing stops it' as numbers, not an impression.\n"
    + "SUDDEN STOP: both gestures land on the build's STOP control (`stop`, falling "
    + "back to `spin`) WHILE the reels turn. Aiming them at `spin` waited for the spin "
    + "button to be clickable, which on a build with a separate stop control (Grandma) "
    + "is only once the reels have STOPPED -- so the 'sudden stop' landed on a finished "
    + "spin. facts.sudden_hold_done / dblclick_done say the gesture really landed "
    + "mid-spin; extra_spin_after_dblclick catches a double-click that fired a spin.";
  static readonly PARAMS = ["hold_ms"];

  readonly holdMs: number;          // how long the spin button is held for the auto-spin hold

  constructor(p: Params = {}) {
    super();
    checkParams(SpinInputs, p);
    this.holdMs = param(p, "hold_ms", 1800);
  }

  /** Record every round response that arrives while `task` (a hold gesture) is running. Paid rounds
   * are filed as `base`, free-spin rounds as `freespin`. No big-win skip here: the skip taps the
   * screen, which would land in the middle of the hold. */
  private async collectBets(ctx: DriveContext, task: { done: () => boolean }): Promise<Bet[]> {
    const got: Bet[] = [];
    while (!task.done()) {
      let raw: any;
      try {
        raw = await awaitBetResponse(ctx.page, 700);
      } catch {
        continue;                                // nothing in this slice; keep watching
      }
      const t0 = ctx.nowS();
      const b = Bet.parse(raw, ctx.events.length);
      ctx.record(b, b.stake > 0 ? "base" : "freespin", t0);
      got.push(b);
    }
    return got;
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // each action is MARKED so the timestamp-mode ACTION TIMELINE pins its exact time for the AI.
    // Every spin here can TRIGGER free spins. The counters below therefore count PAID BASE rounds
    // only (baseRoundsSince) -- a drained bonus must not read as "the input fired extra spins" --
    // and each round is taken to a clean base with finishRound, so the run never ends inside a bonus.
    ctx.mark("SPIN via the Spin button");                     // 035
    let before = ctx.events.length;
    const [b1, r1] = await spinAndFinish(ctx);
    const clickRounds = baseRoundsSince(ctx, before);
    ctx.mark("SPIN via the Space key");                       // 036
    before = ctx.events.length;
    await ctx.pressKey(" ");
    // phase "base": a Space press is a PAID BASE spin the player made. It is observed rather than
    // clicked only because the gesture is a key, and `baseRoundsSince` counts by phase.
    const b2 = await ctx.observeBet(null, "base");
    const r2 = await finishRound(ctx, b2);
    const keyRounds = baseRoundsSince(ctx, before);
    // ⚠ Mark text is read by the model (ACTION TIMELINE): name the GESTURE, never its outcome.
    ctx.mark("HOLD the Spin button, then release");           // 037 + 039
    const holdTask = startTask(ctx.holdButton(GAME.button("spin")!, { holdMs: this.holdMs, optional: true }));
    const holdBets = await this.collectBets(ctx, holdTask);   // every spin the hold fired
    const holdDone = await holdTask.promise;
    // After release (the hold's dwell has already passed), a NEW paid spin means auto kept going. A
    // bonus the hold triggered keeps playing on its own -- those are free rounds, not counted here.
    const afterRelease: Bet[] = [];
    try {
      const raw = await awaitBetResponse(ctx.page, 3000);
      const nb = Bet.parse(raw, ctx.events.length);
      ctx.record(nb, nb.stake > 0 ? "base" : "freespin", ctx.nowS());
      afterRelease.push(nb);
    } catch (e) {
      if (e instanceof BetRejected) throw e;   // a bet WAS sent (and refused): never read that as "stopped"
      // nothing came: auto stopped with the release
    }
    const held = [...holdBets, ...afterRelease];
    const last = held.length ? held[held.length - 1] : null;
    if (last !== null && !isTerminal(last)) {                 // the hold left a bonus running
      const rec = async (raw: any): Promise<void> => {
        ctx.record(Bet.parse(raw, ctx.events.length), "freespin", ctx.nowS());
      };
      await drainToBase(ctx.page, true, rec, ctx.fsTimeoutMs, 500, false);
    }
    await ctx.settle();
    const rHold = held.filter((b) => b.stake > 0);

    // SUDDEN STOP: aim at the STOP control, which is clickable only WHILE the reels turn.
    const stopCtl = or(GAME.button("stop"), GAME.button("spin"));
    const b3 = await ctx.baseSpin();                          // 038: sudden stop via a brief hold mid-spin
    ctx.mark("SUDDEN STOP: hold the stop control during the spin");
    const suddenHold = await ctx.holdButton(stopCtl!, { holdMs: 200, optional: true, timeoutMs: 5000 });
    const r3 = await finishRound(ctx, b3);                    // settles, or drains a triggered bonus
    const b4 = await ctx.baseSpin();                          // 040: double-click sudden stop
    ctx.mark("SUDDEN STOP: double-click the stop control during the spin");
    const dblDone = await ctx.doubleClickButton(stopCtl!, { optional: true, timeoutMs: 5000 });
    let r4 = await finishRound(ctx, b4);
    // A double-click is two clicks: if the second one fired a SPIN, that is a double-fire -- one
    // gesture, two paid rounds. Watch briefly for it, and finish it if it came.
    let extra: Bet | null = null;
    try {
      const raw = await awaitBetResponse(ctx.page, 2000);
      extra = Bet.parse(raw, ctx.events.length);
      ctx.record(extra, "base", ctx.nowS());
      r4 = [...r4, ...(await finishRound(ctx, extra))];
    } catch (e) {
      if (e instanceof BetRejected) throw e;   // a bet WAS sent (and refused): never read that as "no double-fire"
      // no extra spin: the double-click did not double-fire
    }
    const rounds = [...r1, ...r2, ...rHold, ...r3, ...r4];
    return new ScenarioResult(this.name, true, b1, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      marks: { btn: b1.seq, space: b2.seq },
      facts: {
        click_rounds: clickRounds, key_rounds: keyRounds,
        hold_done: holdDone, hold_ms: this.holdMs,
        // paid spins that started while the button was held
        hold_rounds: holdBets.filter((b) => b.stake > 0).length,
        // paid spins that started AFTER the release (auto kept going)
        spins_after_release: afterRelease.filter((b) => b.stake > 0).length,
        stop_control: stopCtl,
        sudden_hold_done: suddenHold,
        dblclick_done: dblDone,
        extra_spin_after_dblclick: extra !== null,
        rounds_total: rounds.length,
      },
    });
  }
}

export class TurboModes extends Scenario {    // Mah_021-023
  readonly name = "turbo-modes";
  readonly description = "Turbo in one recording: toggle the Turbo button on and spin -> reels resolve faster "
    + "(021); press-and-hold the mouse on Spin -> turbo only while held (022); press-and-hold "
    + "Space -> turbo only while held (023). One shared video graded per case.\n"
    + "Reads getSpeedMode() around each step, so 'turbo really engaged' is an EXACT "
    + "check (facts.speed_before / speed_on / speed_off / speed_toggled) rather than an "
    + "AI impression that the reels looked faster — which is unreliable precisely on "
    + "the builds where turbo is subtly broken. facts.mouse_hold_done / key_hold_done "
    + "record that the hold gestures actually fired.\n"
    + "Starts with ONE normal-speed spin (baseline_spin, default on): it is the only "
    + "normal-speed spin in the clip — the spins after Turbo is switched off are the "
    + "HOLDS, which run in turbo on every game that has hold-for-turbo — so it is the "
    + "reference 'turbo is faster' is judged against. facts.baseline_speed, and "
    + "baseline_not_turbo (baseline_speed != speed_on, so independent of how a build "
    + "spells its modes).";
  static readonly PARAMS = ["baseline_spin"];

  readonly baselineSpin: boolean;

  constructor(p: Params = {}) {
    super();
    checkParams(TurboModes, p);
    this.baselineSpin = param(p, "baseline_spin", true);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // `?? null`: a getter answering undefined reaches Python as None.
    const speedBefore = (await ctx.uiRead(SPEED_JS, "UNKNOWN")) ?? null;
    let rounds: Bet[] = [];
    let baselineSpeed: unknown = null;
    if (truthy(this.baselineSpin)) {
      // Without this the clip opens on the turbo toggle, and "faster than a normal spin" has no
      // normal spin in it to be faster than.
      ctx.mark("NORMAL SPIN");
      [, rounds] = await spinAndFinish(ctx);       // may TRIGGER -- plays the bonus out
      baselineSpeed = (await ctx.uiRead(SPEED_JS, "UNKNOWN")) ?? null;
    }
    ctx.mark("TURBO ON");
    const turboOn = await ctx.clickButton(GAME.button("turbo")!, { optional: true });          // 021: Turbo ON
    const speedOn = (await ctx.uiRead(SPEED_JS, "UNKNOWN")) ?? null;
    const [b, turboRounds] = await spinAndFinish(ctx);  // may TRIGGER -- play the bonus out before toggling
    rounds = [...rounds, ...turboRounds];
    ctx.mark("TURBO OFF");
    await ctx.clickButton(GAME.button("turbo")!, { optional: true });          // back OFF so the holds test hold-turbo
    const speedOff = (await ctx.uiRead(SPEED_JS, "UNKNOWN")) ?? null;
    ctx.mark("HOLD the mouse on Spin -> turbo while held");
    const mouseHold = await ctx.holdButton(GAME.button("spin")!, { holdMs: 1500, optional: true });
    await ctx.settle();                                                 // 022
    ctx.mark("HOLD the Space key -> turbo while held");
    await ctx.holdKey(" ", 1500);
    await ctx.settle();                                                 // 023
    return new ScenarioResult(this.name, true, b, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      facts: {
        speed_before: speedBefore, speed_on: speedOn,
        speed_off: speedOff, baseline_speed: baselineSpeed,
        // the reference spin really was NOT turbo (null: no baseline)
        baseline_not_turbo: baselineSpeed === null ? null : !pyEq(baselineSpeed, speedOn),
        // the real question: did the mode actually CHANGE when toggled?
        speed_toggled: !pyEq(speedOn, speedOff),
        has_speed_getter: !pyEq(speedOn, "UNKNOWN"),
        turbo_control_present: turboOn,
        mouse_hold_done: mouseHold, key_hold_done: true,
      },
    });
  }
}

export class ReconnectBrief extends Scenario {    // Mah_027 / 029 / 033
  readonly name = "reconnect-brief";
  readonly description = "Briefly drop the network (below the reconnect threshold) and restore it: the game "
    + "should show a reconnecting state and auto-resume where it was, balance/UI in sync. "
    + "After reconnect it PLAYS ONE MORE ROUND to prove the game is stable/playable again. "
    + "Covers a short disconnect (027), a network switch (029) and a short tab-away/return "
    + "(033). facts.recovered / played_after_reconnect.";
  static readonly PARAMS = ["off_s"];

  readonly offS: number;

  constructor(p: Params = {}) {
    super();
    checkParams(ReconnectBrief, p);
    this.offS = param(p, "off_s", 6.0);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const b = await ctx.baseSpin();
    await ctx.settle();                                     // a known state before the outage
    await ctx.setOffline(true);
    await sleep(this.offS * 1000);
    await ctx.setOffline(false);
    await sleep(4 * 1000);                                  // allow the client to reconnect
    let recovered = false;
    try {
      await waitSpinClickable(ctx.page, ctx.spinTimeoutMs);
      recovered = true;
    } catch {
      // not spinnable again
    }
    // play ONE more round after reconnect to confirm the game is stable and actually playable
    const rounds = [b];
    let playedAfter = false;
    if (recovered) {
      try {
        const nb = await ctx.baseSpin();
        rounds.push(nb);
        if (isFreespinTrigger(nb)) {
          await playOutBonus(ctx, nb);           // drain if this spin triggers FS (no settle hang)
        } else {
          await ctx.settle();
        }
        playedAfter = true;
      } catch {
        // the round after the reconnect failed
      }
    }
    return new ScenarioResult(this.name, true, b, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      facts: { offline_s: this.offS, recovered, played_after_reconnect: playedAfter },
    });
  }
}

export class ReconnectLong extends Scenario {    // Mah_028
  readonly name = "reconnect-long";
  readonly description = "Drop the network LONGER than the reconnect threshold (>20s), then restore it: the game "
    + "should NOT silently resume -- it shows a popup and returns to Landing/Home per design. "
    + "Video-graded.";
  static readonly PARAMS = ["off_s"];

  readonly offS: number;

  constructor(p: Params = {}) {
    super();
    checkParams(ReconnectLong, p);
    this.offS = param(p, "off_s", 25.0);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const [b, rounds] = await spinAndFinish(ctx);    // may TRIGGER -- finish the bonus before going offline
    await ctx.setOffline(true);
    await sleep(this.offS * 1000);
    await ctx.setOffline(false);
    await sleep(6 * 1000);                                   // let the disconnect popup / redirect show
    const result = new ScenarioResult(this.name, true, b, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      facts: { offline_s: this.offS },
    });
    // The session is OVER by design (the popup is the case). Re-enter AFTER the window closed, so the
    // evidence ends on the popup and the next run does not start on a dead client.
    await ctx.freshSession("reconnect-long ended the session");
    return result;
  }
}

export class ReconnectDuringAuto extends Scenario {    // Mah_031
  readonly name = "reconnect-during-auto";
  readonly description = "Start auto-play, briefly drop the network mid-auto, then restore it: the game should "
    + "reconnect and handle auto per design (resume or stop) with NO data desync. "
    + "facts.recovered = playing again, by EITHER allowed path: an auto bet arrives after "
    + "the reconnect (auto_resumed) or the spin control becomes usable (auto stopped). "
    + "The rounds after the reconnect (up to rounds_after) are recorded, so conservation "
    + "covers them — a spin replayed on reconnect debits twice. Auto is then stopped so "
    + "the next run starts idle. facts.rounds_after_reconnect.";
  static readonly PARAMS = ["count", "off_s", "resume_wait_s", "rounds_after"];

  static readonly COUNT_INDEX: ReadonlyMap<number, number> = new Map([[10, 0], [20, 1], [30, 2], [50, 3]]);

  readonly count: number;
  readonly offS: number;
  readonly resumeWaitS: number;    // how long after the reconnect an auto bet may take
  readonly roundsAfter: number;    // auto rounds recorded after the reconnect

  constructor(p: Params = {}) {
    super();
    checkParams(ReconnectDuringAuto, p);
    this.count = param(p, "count", 10);
    this.offS = param(p, "off_s", 6.0);
    this.resumeWaitS = param(p, "resume_wait_s", 30.0);
    this.roundsAfter = param(p, "rounds_after", 2);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const idx = ReconnectDuringAuto.COUNT_INDEX.get(this.count) ?? 0;
    await ctx.clickButton(GAME.button("auto")!);
    await ctx.clickButton(GAME.button("auto_count", { i: idx })!);
    await ctx.clickButton(GAME.button("auto_confirm")!);
    const rounds: Bet[] = [];
    try {
      rounds.push(await ctx.observeBet());              // let one auto spin land first
    } catch {
      // no auto spin landed before the outage
    }
    await ctx.setOffline(true);
    await sleep(this.offS * 1000);
    await ctx.setOffline(false);
    // RECOVERED has two allowed shapes, and the old check knew only one. It waited for the spin
    // control to become clickable — which never happens while auto is still RUNNING, so a game that
    // resumed auto correctly read as "not recovered" (2026-10-01: the AI saw auto carry on after
    // "Your connection is unstable"; recovered was False). So: first watch for the next auto bet
    // (auto resumed); only if none comes, ask whether auto stopped and play is possible.
    let recovered = false;
    let resumed = false;
    const after: Bet[] = [];
    try {
      after.push(await ctx.observeBet(pyInt(this.resumeWaitS * 1000)));
      recovered = resumed = true;
      while (after.length < this.roundsAfter) after.push(await ctx.observeBet());
    } catch {
      // no (further) auto bet: judged below / already resumed
    }
    if (!resumed) {
      try {
        await waitSpinClickable(ctx.page, ctx.spinTimeoutMs);
        recovered = true;
      } catch {
        // not spinnable either
      }
    }
    rounds.push(...after);
    const result = new ScenarioResult(this.name, true, rounds.length ? rounds[rounds.length - 1] : null, rounds,
      ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: { offline_s: this.offS, recovered, auto_resumed: resumed, rounds_after_reconnect: after.length },
      });
    // Leave the game idle: a resumed auto run still has spins left. The teardown / cleanup then play
    // out anything it triggered.
    // autoStopButton(), NOT GAME.button("auto_stop"): on Grandma `auto:stop` lives inside the auto
    // PANEL, which is closed while auto runs, so that click never landed and auto kept betting into
    // the teardown. The helper picks the build's main-screen stop first.
    await ctx.clickButton(autoStopButton()!, { optional: true, timeoutMs: 5000 });
    return result;
  }
}

export class MultiTabSession extends Scenario {    // Mah_030
  readonly name = "multi-tab-session";
  readonly description = "Open the SAME account in a second session (a fresh browser context on the same URL) "
    + "while this one is playing: the original session should be invalidated per design — a "
    + "popup and/or a return to Landing/Home. The recorded video is THIS (first) tab, so it "
    + "captures the invalidation. Needs the runner to pass browser + url into the context.";
  static readonly PARAMS: readonly string[] = [];

  constructor(p: Params = {}) {
    super();
    checkParams(MultiTabSession, p);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // may TRIGGER -- finish the bonus before opening the second session, or the takeover lands
    // mid-bonus and the free-spin rounds are never consumed
    const [b, rounds] = await spinAndFinish(ctx);           // tab 1 is active and playing
    try {
      await ctx.openSecondSession();                       // same account, new context -> second login
    } catch (e) {
      return new ScenarioResult(this.name, false, b, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: `could not open a second session: ${errText(e)}` });
    }
    await sleep(6 * 1000);                                  // let the server invalidate this (first) tab
    let result: ScenarioResult;
    try {
      result = new ScenarioResult(this.name, true, b, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { spin: b.seq },
        // the second login is the whole premise: if it never happened the video shows a healthy tab
        // and "no takeover popup" means nothing
        facts: { second_opened: true, wait_s: 6 },
      });
    } finally {
      await ctx.closeSecondSession();
    }
    // The second login kicked THIS session out by design. Re-enter on a fresh token, after the
    // window closed, so the next run does not start on the takeover popup.
    await ctx.freshSession("multi-tab-session ended the session");
    return result;
  }
}

export class FreespinZeroBalance extends Scenario {    // Mah_011
  readonly name = "freespin-zero-balance";
  readonly description = "Spend the player's LAST STAKE into a free-spin trigger, then reload and prove the "
    + "owed free spins still play out with nothing left in the wallet -- a free-spin round "
    + "does not check the balance, because it costs nothing. Each attempt funds the wallet "
    + "with EXACTLY one bet, reloads so the client re-reads it, and spins: the stake is "
    + "debited, so a spin that triggers leaves the player broke with a bonus owed. One free "
    + "spin is then played and HELD on (observe_ms) so the award and its result are "
    + "watchable, and the game is reloaded with the bonus still pending. An attempt is "
    + "DISCARDED and re-hunted when it does not trigger, or when the trigger or the first "
    + "free spin hits the max-win cap (the server truncates back to base, leaving nothing "
    + "for the reload to resume). Streams: stake_zero_in_freespins + conservation over the "
    + "resumed rounds. Exposes facts.broke_at_trigger / balance_after_trigger / "
    + "observed_before_reload / fs_rounds_resumed / attempts_discarded. "
    + "Needs PLAYSITE_*; restore the wallet with `teardown: restore-balance`.";
  static readonly PARAMS = ["max_attempts", "observe_ms", "reload_mid_bonus"];

  // WHY THE WALLET IS SET BEFORE THE SPIN AND NEVER DURING THE BONUS
  //
  // The obvious shape -- trigger the bonus, then drain the wallet -- cannot be made to work. The
  // server will not release the balance while a bonus is open or play is recent: measured live
  // 2026-09-03, a withdrawal was refused against a completely UNCHANGED balance across 45 retries
  // over 90s, with the client already torn down, and the identical withdrawal succeeded minutes
  // later. There is also a hard floor of 0.01 -- the wallet never reaches exactly 0.00.
  //
  // So the player is made broke the way a real one would be: given one bet and asked to spend it.
  // The debit happens inside the spin, server-side, so there is no wallet call to be refused and no
  // race with the auto-playing bonus. That also removes the need for `stopClient()` here.

  readonly maxAttempts: number;
  readonly observeMs: number;        // hold on the first free spin so its result can be judged
  // true (the original CR_011 shape): play ONE free spin, then reload with the rest owed and prove
  // they resume. false: no reload -- play the bonus from its first spin to its last, which is what a
  // "keep playing with an empty wallet" requirement asks for.
  readonly reloadMidBonus: boolean;

  constructor(p: Params = {}) {
    super();
    checkParams(FreespinZeroBalance, p);
    this.maxAttempts = param(p, "max_attempts", 25);
    this.observeMs = param(p, "observe_ms", 4000);
    this.reloadMidBonus = param(p, "reload_mid_bonus", true);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    if (ctx.wallet === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "wallet not configured -- set PLAYSITE_* to fund the wallet" });
    }

    // The stake to fund is the game's CURRENT bet, read from the client rather than assumed: a
    // hardcoded figure would silently fund the wrong amount if the opening bet ever changed, and the
    // player would still be able to spin at the reload.
    const info = await ctx.eval(`() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const num = (x) => (typeof x === 'number' && !isNaN(x)) ? x : null;
            return { betAmount: num(b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)) }; }`);
    const stakeRead = get(info, "betAmount");
    if (!truthy(stakeRead)) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "could not read the current bet from the bridge -- cannot fund one stake" });
    }
    const stake = pyFloat(stakeRead);

    let trigger: Bet | null = null;
    let first: Bet | null = null;
    let noTrigger = 0;
    let triggersCapped = 0;
    let firstFsCapped = 0;
    let triggerPaid = 0;
    let attempts = 0;

    for (let attempt = 0; attempt < this.maxAttempts; attempt++) {
      attempts = attempt + 1;
      // Fund EXACTLY one bet. This is the only wallet call in the scenario, and it lands with no
      // bonus open (a discarded attempt is settled back to base first), which is the state the
      // server will actually let the balance be set in.
      try {
        await ctx.setBalance(stake, 10, 2.0);
      } catch (e) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
          { reason: py`could not fund the wallet with one stake (${stake}): ${errText(e)}` });
      }
      // The client caches the balance, so it must re-read it before the spin -- otherwise it blocks
      // the bet as unaffordable using the figure it still remembers from the last spin.
      await ctx.reloadAndStart();

      const b = await ctx.baseSpin();          // the stake is debited HERE -> the player is now broke
      if (!isFreespinTrigger(b)) {
        noTrigger += 1;
        await ctx.settle();
        continue;
      }
      // The run caps the payout (`max_win_x`) so a forced free-spin paytable cannot spin forever. A
      // side effect: a round can reach that cap, and the server then TRUNCATES back to base with
      // `hasReachedMaxWin` set and the award forfeited -- on the trigger itself (no bonus at all) or
      // on the first free spin (bonus over before the reload). Either way nothing is left for the
      // reload to resume, so the attempt is unusable. That is a normal consequence of the cap, not a
      // failure: settle and hunt another one.
      if (b.has_max_win || isTerminal(b)) {
        triggersCapped += 1;
        await ctx.settle();
        continue;
      }
      // The premise is that the player CANNOT AFFORD A BET when the bonus begins. The triggering
      // spin can itself pay a line win, which puts them back in funds -- so an attempt that leaves a
      // stake or more is not the state under test. Discard it and hunt another. `b.balance` is the
      // balance AFTER the stake was debited and any payout credited, which is exactly the figure to
      // judge on.
      if (b.balance === null || b.balance >= stake) {
        triggerPaid += 1;
        await ctx.settle();
        continue;
      }

      if (!truthy(this.reloadMidBonus)) {
        // No reload: the player is broke from this spin on, and the bonus simply has to play to its
        // last round. The whole bonus is played below, after the hunt.
        trigger = b;
        break;
      }

      // Play ONE free spin and HOLD on it before reloading.
      //
      // The award presentation and the first bonus round land in the couple of seconds right after
      // the trigger; reloading immediately replaced them with a loading screen, so the result the
      // case asks about was never on screen long enough to judge. It is also the ONLY round the video
      // needs -- the remaining spins play out after the reload, which is the behaviour under test,
      // but they are graded from the response stream. `marks` exposes `first_fs` so the suite can
      // ask for `at: [trigger, first_fs]`.
      let f: Bet | null = null;
      try {
        f = await ctx.freeSpin();
        ctx.mark("FIRST FREE SPIN played -- the wallet is spent and the game reloads AFTER this");
        await ctx.dwell(this.observeMs);
      } catch {
        // client may auto-play the bonus; handled as a discard below
      }
      if (f === null || f.has_max_win || isTerminal(f)) {
        firstFsCapped += 1;
        await ctx.settle();                    // bonus is over -> back at base, hunt again
        continue;
      }
      [trigger, first] = [b, f];
      break;
    }

    if (trigger !== null && !truthy(this.reloadMidBonus)) {
      // The requirement's own shape (Grandma TC-010: "trigger FS -> let the balance reach 0 -> keep
      // playing"): the trigger spin spent the last stake, and every free spin now plays from the
      // start of the bonus to its end, uninterrupted, with the wallet empty. The drain also answers a
      // Free Spin Extension offer at the end.
      ctx.mark("FREE SPINS START: the balance is spent");
      const rounds = await playOutFreespins(ctx, trigger, { cap: 500, settle: true });
      const fs = rounds.slice(1);
      return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { trigger: trigger.seq, terminal: rounds[rounds.length - 1].seq },
        facts: {
          stake,
          balance_after_trigger: trigger.balance,
          broke_at_trigger: trigger.balance !== null && trigger.balance < stake,
          triggers_paid_out: triggerPaid,
          funded_attempts: attempts,
          attempts_no_trigger: noTrigger,
          triggers_capped: triggersCapped,
          // every free spin, start to end, with no reload between
          fs_rounds_played: fs.length,
          played_after_zero: fs.length > 0,
          reloaded_mid_bonus: false,
        },
      });
    }

    if (trigger === null || first === null) {
      let reason = py`no usable free-spin trigger in ${attempts} funded attempt(s): `
        + py`${noTrigger} spin(s) did not trigger, ${triggersCapped} trigger(s) and `
        + py`${firstFsCapped} opening free spin(s) hit the max-win cap and were truncated `
        + py`back to base leaving no bonus for the reload to resume, and ${triggerPaid} `
        + "trigger(s) paid enough to leave the player able to bet again";
      if (triggersCapped || firstFsCapped) reason += " -- raise the run's `max_win_x`";
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        facts: {
          triggers_capped: triggersCapped,
          attempts_no_trigger: noTrigger,
          triggers_paid_out: triggerPaid,
          // bool = "did the graded attempt cap?"; the COUNT of discarded attempts is
          // `attempts_discarded`
          first_fs_capped: truthy(firstFsCapped),
          attempts_discarded: firstFsCapped,
        },
        reason,
      });
    }
    const observed: Bet[] = [first];

    // Reload with the bonus still owed. No wallet call and no `stopClient()`: the balance was already
    // spent by the trigger spin, so there is nothing to race and nothing to be refused. START resumes
    // the owed bonus and `reloadAndStart` hands those rounds back -- draining again here would wait
    // on responses that have already been consumed.
    let resumed: Bet[];
    try {
      resumed = await ctx.reloadAndStart();
    } catch (e) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: `could not reload with the bonus pending: ${errText(e)}` });
    }
    const rounds = [trigger, ...observed, ...resumed];
    if (!resumed.length || !isTerminal(resumed[resumed.length - 1])) {
      try {                                                // not terminal yet -> keep draining
        for (let i = 0; i < 500; i++) {
          const rnd = await ctx.freeSpin();
          rounds.push(rnd);
          if (isTerminal(rnd)) break;
        }
      } catch {
        // client may have played it internally; video proof covers it
      }
    }
    await ctx.settle();
    const marks: Dict = { trigger: trigger.seq };
    if (first !== null) marks.first_fs = first.seq;      // `at: [trigger, first_fs]` -> just the watchable part
    // What the player actually had when the bonus resumed. Free spins PAY IN, so this is the balance
    // after the trigger and the first free spin -- the honest number to judge "broke" on, and the one
    // the case asserts against instead of trusting the scenario's name.
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(), marks,
      facts: {
        stake,
        // WHERE "BROKE" IS MEASURED, and why it is not the reload. Free spins PAY IN, so the balance
        // climbs the moment the bonus starts winning -- judging at the reload would fail a perfectly
        // good run just because its first free spin paid. The precondition this case rests on is
        // that the player spent their LAST STAKE to enter the bonus, so it is judged on the
        // triggering spin, after its stake was debited and any payout credited.
        balance_after_trigger: trigger.balance,
        // True by construction -- the hunt discards an attempt whose trigger left a stake or more.
        // Asserted as a regression guard, the same way `first_fs_capped == False` is.
        broke_at_trigger: trigger.balance !== null && trigger.balance < stake,
        // what they held once the bonus resumed; informational, since free-spin winnings
        // legitimately raise it
        balance_at_reload: first.balance,
        triggers_paid_out: triggerPaid,
        funded_attempts: attempts,
        attempts_no_trigger: noTrigger,
        fs_rounds_resumed: rounds.length - 1 - observed.length,
        // free spins played (and dwelt on) BEFORE the reload
        observed_before_reload: observed.length,
        // triggers skipped because the max-win cap truncated them to base
        triggers_capped: triggersCapped,
        // False by construction: the hunt only stops on an attempt whose first free spin did NOT
        // cap. `attempts_discarded` counts the ones thrown away to get here.
        first_fs_capped: false,
        attempts_discarded: firstFsCapped,
        // the bonus really did keep playing after the wallet was spent
        played_after_zero: rounds.length - 1 - observed.length > 0,
      },
    });
  }
}

export const SCENARIOS: ScenarioClass[] = [
  InsufficientBalance, AutoStopAtWin, AutoUserStop, AutoReloadCancels, AutoChangeBetFirst, AutoUntilEmpty,
  OpenTutorial, SpinInputs, TurboModes, ReconnectBrief, ReconnectLong, ReconnectDuringAuto, MultiTabSession,
  FreespinZeroBalance,
];
