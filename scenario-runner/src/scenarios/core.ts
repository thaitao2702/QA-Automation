/**
 * Core scenarios: base wins, free spins to the end, bet changes, idle base, auto-play, and the two
 * History walks. Port of scenario_engine.py lines 2109-3295 (BaseWin .. FreespinThenHistory), plus
 * the module-level helpers defined among them (`close_opener_if_left`, `close_history`, the
 * `_*_ASK` texts). `open_bet_list` / `close_bet_list` live in engine/shared.
 */
import { Bet, isFreespinTrigger, winClusters, winSteps } from "../engine/bet.js";
import { waitSpinClickable } from "../engine/bridge.js";
import type { DriveContext } from "../engine/drive.js";
import { AUTO_JS, confirmStopped, playOutBonus, playOutFreespins, spinAndFinish } from "../engine/flows.js";
import { GAME } from "../engine/game.js";
import { stepMultipliers } from "../engine/invariants.js";
import { sleep } from "../engine/page.js";
import { ScenarioResult } from "../engine/result.js";
import { BUTTON_WATCH_READ_JS, closeBetList, openBetList } from "../engine/shared.js";
import {
  type Dict, get, isNumber, or, py, pyEq, pyFloat, pyInt, pyList, pyLt, pyRound, pySorted, pyStr, PyTypeError,
  pyTypeName, PyValueError, sum, truthy,
} from "../py.js";
import { checkParams, param, type Params, Scenario, type ScenarioClass } from "./base.js";

// ── Local Python-semantics helpers ───────────────────────────────────────────────────

/** `xs[i]` with Python's rules: a negative index counts from the end, out of range raises
 * IndexError, a non-integer raises TypeError (JavaScript would answer `undefined` to all three). */
function pyIndex<T>(xs: readonly T[], i: unknown): T {
  const k0 = typeof i === "boolean" ? Number(i) : i;
  if (typeof k0 !== "number" || !Number.isInteger(k0)) {
    throw new PyTypeError(`list indices must be integers or slices, not ${pyTypeName(i)}`);
  }
  const k = k0 < 0 ? xs.length + k0 : k0;
  if (k < 0 || k >= xs.length) {
    const e = new Error("list index out of range");
    e.name = "IndexError";
    throw e;
  }
  return xs[k];
}

/** Python `a % n` for n > 0: the result takes the divisor's sign (`-1 % 5 == 4`). */
function pyMod(a: number, n: number): number {
  return ((a % n) + n) % n;
}

/** `isinstance(v, int)` — which, in Python, includes bool. */
function isPyInt(v: unknown): v is number | boolean {
  return typeof v === "boolean" || (typeof v === "number" && Number.isInteger(v));
}

/** `hasattr(obj, name)`. */
function hasAttr(obj: object, name: string): boolean {
  return name in obj;
}

/** The scatter census a few profiles expose (`bonus_reels` / `max_scatters_on_a_reel`). */
interface ScatterCensus {
  bonusReels(raw: Dict): number[] | null;
  maxScattersOnAReel(raw: Dict): number | null;
}

function census(): ScatterCensus {
  return GAME as unknown as ScatterCensus;
}

const BET_INDEX_JS = "() => { const b = window.__gameTestBridge; "
  + "return b.getBetIndex ? b.getBetIndex() : -1; }";
const BET_AMOUNT_JS = "() => { const b = window.__gameTestBridge; "
  + "return b.getBetAmount ? b.getBetAmount() : null; }";

// =====================================================================================
// base-win
// =====================================================================================

export class BaseWin extends Scenario {
  static readonly PARAMS: readonly string[] = ["max_spins", "skip_big_win"];
  override readonly name = "base-win";
  override readonly description = "Spin the base game until a round PAYS, then settle. Mechanism-agnostic: it asks "
    + "only `payout > 0`, so it works on a payline game as readily as a cascade one — "
    + "use it for the balance-credit check. (`base-cluster-win` is the cascade-specific "
    + "cousin: it additionally requires >= 2 cascade steps, which a payline game never "
    + "produces.) Exposes res = the winning round + facts.spins_to_win / win_payout / "
    + "win_stake. Unreached if nothing pays inside the spin budget.\n"
    + "It also publishes the BALANCE READOUT'S THREE VALUES for the winning round — "
    + "balance_before_spin, balance_while_spinning (the stake is taken at spin START, "
    + "so the readout sits here for the whole spin) and balance_after_win. "
    + "A prompt about either half of the money has to name the two endpoints of the "
    + "step it is judging, not just the destination; naming only `res.balance` earned "
    + "a correct NO on Mah_008, because that is a value the readout passes THROUGH. "
    + "The names are `base-cluster-win`'s and the authoring doc's, deliberately: one concept "
    + "must not have two names across two scenarios, or a prompt moved between "
    + "them silently binds to null. See docs/scenario-assert-authoring.md §2. "
    + "PORTABLE, and verified against the captured payloads of all three games. "
    + "The one thing to know is WHY `win_stake` is always > 0 here: a paying round "
    + "with stake 0 exists on every game with a bonus -- Zeus's terminal Hold & "
    + "Spin round reports payout 261 at stake 0 and is NOT a trigger, so it reads "
    + "exactly like a base win. It never reaches this loop because `base_spin()` "
    + "only returns rounds the framework CLICKED; bonus rounds arrive through "
    + "`awaitFreespinRound` inside `play_out_freespins`. That is an invariant of "
    + "the drive, not an accident -- a scenario that collects rounds some other "
    + "way must re-check it.";

  readonly maxSpins: number;
  /** False keeps a BIG/MEGA win celebration on screen instead of tapping it away. Needed when a
   * case grades the balance COUNT-UP: the skip cuts the count-up short, so "did the balance count
   * up?" could read "no" on a big win through the test's own doing. */
  readonly skipBigWin: boolean;

  constructor(p: Params = {}) {
    super();
    checkParams(BaseWin, p);
    this.maxSpins = param(p, "max_spins", 60);
    this.skipBigWin = param(p, "skip_big_win", true);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    ctx.skipBigWins = this.skipBigWin;
    const rounds: Bet[] = [];
    for (let i = 0; i < this.maxSpins; i++) {
      const b = await ctx.baseSpin();
      rounds.push(b);
      if (isFreespinTrigger(b)) {
        // a bonus pays too, but its money arrives across the feature -- drain it and keep looking
        // for a plain BASE win, which is what the balance-credit case is about
        const fs = await playOutBonus(ctx, b);
        rounds.push(...fs.slice(1));
        continue;
      }
      await ctx.settle();
      if (b.payout > 0) {
        // The readout's three values, derived from the round's own numbers via conservation
        // (balance == previous - stake + payout). Computed here rather than left to the case,
        // because a suite cannot do arithmetic: a `video:` prompt can only interpolate facts, so an
        // endpoint that is not a fact cannot be named.
        const whileSpinning = pyRound(b.balance - b.payout, 2);
        return new ScenarioResult(this.name, true, b, rounds, ctx.timeline, ctx.events,
          [start, ctx.nowS()], {
            round_windows: ctx.windows(),
            marks: { win: b.seq },
            facts: {
              spins_to_win: rounds.length, win_payout: b.payout,
              win_stake: b.stake, balance_after_win: b.balance,
              // as the client PRINTS them (profile DISPLAY_*) — what an AI read of the screen is
              // compared with
              win_payout_display: GAME.displayAmount(b.payout),
              balance_after_win_display: GAME.displayAmount(b.balance),
              balance_before_spin: pyRound(whileSpinning + b.stake, 2),
              balance_while_spinning: whileSpinning,
            },
          });
      }
    }
    return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: { spins_played: rounds.length },
        reason: py`no paying base round in ${this.maxSpins} spins`,
      });
  }
}

// =====================================================================================
// base-cluster-win
// =====================================================================================

export class BaseClusterWin extends Scenario {
  static readonly PARAMS: readonly string[] = ["max_spins"];
  override readonly name = "base-cluster-win";
  override readonly description = "Base game: spin until a cluster win with >= 2 cascades (which climbs the multiplier), "
    + "then play ONE MORE fresh base spin so the multiplier RESET on the next round is also "
    + "on screen. A cluster win is expensive to hit, so this single play covers BOTH the "
    + "win (balance credit + multiplier-per-cascade) and the reset. Exposes res = the winning "
    + "spin, rounds = [win, next]; marks: win = the cascade win, reset = the following spin "
    + "(bind win checks to `at: win`, the reset check to `at: reset`).";

  readonly maxSpins: number;

  constructor(p: Params = {}) {
    super();
    checkParams(BaseClusterWin, p);
    this.maxSpins = param(p, "max_spins", 200);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    if (GAME.mechanism !== "cascade") {
      // A payline game reports one grid per round, so `cascades >= 2` can never be true and this
      // would spin its whole budget to no purpose. Say so immediately instead: use `base-win` for
      // a plain balance-credit check.
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          reason: `'${GAME.name}' is a ${GAME.mechanism} game -- it has no `
            + "cascade steps, so a >=2-cascade win cannot occur. Use the "
            + "'base-win' scenario for any-win checks.",
        });
    }
    let win: Bet | null = null;
    for (let i = 0; i < this.maxSpins; i++) {
      const b = await ctx.baseSpin();
      if (isFreespinTrigger(b)) {
        // A cascade can land the trigger, and then the client auto-plays the whole bonus. It has
        // to be played out HERE: the next iteration starts with waitForSpinClickable, which by
        // contract does not resolve during free spins, so leaving the bonus running burns that
        // spin's whole budget and fails with 'spin_state:STOP' while the snapshot still reads
        // gameMode FREE_SPIN. Not a win for this case either way — a triggering round's multiplier
        // is the bonus's, not the base game's — so drain and keep hunting.
        await playOutFreespins(ctx, b);
        continue;
      }
      if (b.payout > 0 && b.cascades >= 2) {
        win = b;
        break;
      }
    }
    if (win === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        reason: py`no >=2-cascade win in ${this.maxSpins} base spins`,
      });
    }
    await ctx.settle();                        // finish the winning round's presentation (inside its clip)
    const nxt = await ctx.baseSpin();          // a fresh new round -> the base multiplier should reset to x1
    if (isFreespinTrigger(nxt)) {
      await playOutFreespins(ctx, nxt);        // drain if this spin happened to trigger free spins
    } else {
      await ctx.settle();
    }
    // The multiplier ladder is the POINT of this scenario, so expose it: the per-step climb inside
    // the winning round, and where the following round started. Read from the response (never
    // assumed to be +1 per cascade -- Mystic's base ladder is 1,2,3,5,8,10).
    const climb = stepMultipliers(win);
    const after = stepMultipliers(nxt);
    const configured = await ctx.uiRead(
      "() => { const b = window.__gameTestBridge;"
      + " return b && b.getMultiplierLevels ? b.getMultiplierLevels() : null; }");
    return new ScenarioResult(this.name, true, win, [win, nxt], ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { win: win.seq, reset: nxt.seq },
        facts: {
          cascades: win.cascades,
          // ⚠ `cascades` is GRIDS EVALUATED, not what a viewer counts. The chain ends with a step
          // that pays nothing — that step clears no symbols and drops nothing in, so the number of
          // VISIBLE clear-and-drop events is one fewer. A prompt that says "cascaded {cascades}
          // times" overstates it by one and earns a correct NO (see Mah_043).
          cascade_events: GAME.clusterWins(GAME.steps(or(win.raw, {}) as Dict), win.payout),
          win_payout: win.payout, win_stake: win.stake,
          // The BALANCE READOUT passes through three values, and `res.balance` is only the middle
          // one. The stake is taken at spin START, so while the reels resolve the readout already
          // reads balance-minus-win; the win is credited at the end of the count-up; and the next
          // spin's stake comes straight off again. A prompt that asks "does it rise to
          // {res.balance}" without saying this gets a correct NO (see Mah_008).
          balance_before_spin: pyRound(win.balance - win.payout + win.stake, 2),
          balance_while_spinning: pyRound(win.balance - win.payout, 2),
          balance_after_win: win.balance,
          balance_after_next_bet: pyRound(win.balance - nxt.stake, 2),
          multipliers: climb,             // e.g. [1, 2, 3, 5, 8, 10]
          first_multiplier: climb.length ? climb[0] : null,
          top_multiplier: climb.length ? climb[climb.length - 1] : null,
          multiplier_climbed: climb.length >= 2 && pyLt(climb[0], climb[climb.length - 1]),
          next_multiplier: after.length ? after[0] : null,
          multiplier_reset: climb.length > 0 && after.length > 0 && pyEq(after[0], climb[0]),
          // config, where the build reports it -- lets a case check the observed climb against the
          // configured ladder exactly
          configured_ladder: get(or(configured, {}), "baseGame"),
        },
      });
  }
}

// =====================================================================================
// fs-complete
// =====================================================================================

export class FreeSpinComplete extends Scenario {
  static readonly PARAMS: readonly string[] = ["max_base", "arrive_when", "max_spins"];
  override readonly name = "fs-complete";
  override readonly description = "Spin base until free spins trigger, then observe the whole bonus to its "
    + "terminal round. Exposes rounds = [trigger, ...free spins..., terminal]; "
    + "trigger = rounds[0], terminal = rounds[-1].";

  readonly maxBase: number;
  /**
   * Hunt for a trigger of a PARTICULAR SHAPE, not just any trigger. Keys, all optional:
   *   bonus_reels     -- the award table is keyed on how many REELS carry the scatter, so
   *                      "3 reels pays 8, 4 pays 12, 5 pays 15" needs one trigger of each
   *   min_on_one_reel -- at least this many scatters on a single reel: the case a ways-based award
   *                      count would get wrong, and pure luck without asking for it
   * A trigger that does not match is PLAYED OUT and the hunt continues -- leaving a bonus owed
   * would poison every round after it.
   */
  readonly arriveWhen: Dict;

  constructor(p: Params = {}) {
    super();
    checkParams(FreeSpinComplete, p);
    this.maxBase = or(param<unknown>(p, "max_spins", null), param(p, "max_base", 400)) as number;
    this.arriveWhen = or(param<Dict | null>(p, "arrive_when", null), {}) as Dict;
  }

  private matches(b: Bet): boolean {
    const wantReels = get(this.arriveWhen, "bonus_reels");
    const wantStack = get(this.arriveWhen, "min_on_one_reel");
    const raw = or(b.raw, {}) as Dict;
    if (wantReels !== null) {
      const reels = hasAttr(GAME, "bonusReels") ? census().bonusReels(raw) : null;
      if (reels === null || reels.length !== pyInt(wantReels)) return false;
    }
    if (wantStack !== null) {
      const most = hasAttr(GAME, "maxScattersOnAReel") ? census().maxScattersOnAReel(raw) : null;
      if (most === null || most < pyInt(wantStack)) return false;
    }
    return true;
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    let trigger: Bet | null = null;
    let skipped = 0;
    if (truthy(this.arriveWhen) && !hasAttr(GAME, "bonusReels")) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          reason: "`arrive_when` needs a profile that can read the scatter "
            + `census off the grid; ${GAME.name} cannot`,
        });
    }
    for (let i = 0; i < this.maxBase; i++) {
      const b = await ctx.baseSpin();
      if (isFreespinTrigger(b)) {
        if (this.matches(b)) {
          trigger = b;
          break;
        }
        skipped += 1;
        ctx.mark(py`TRIGGER SKIPPED: wrong shape for ${this.arriveWhen} -- draining it`);
        await playOutBonus(ctx, b);
      }
    }
    if (trigger === null) {
      const why = truthy(this.arriveWhen)
        ? py` matching ${this.arriveWhen} (${skipped} other trigger(s) played out)`
        : "";
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        reason: py`no free-spin trigger${why} in ${this.maxBase} base spins`,
      });
    }

    // The number on the spin button, read after each free spin settles, against what the response
    // says is left. Free spins are auto-played, so this is the only place it can be sampled per
    // round.
    const counterHits: boolean[] = [];
    const counterReadings: Array<{ seq: number; shown: unknown; response: number }> = [];

    const readCounter = async (bet: Bet): Promise<void> => {
      // ⚠ WAIT FOR THE ROUND TO FINISH PRESENTING. `onRound` fires the moment the RESPONSE lands,
      // and the spin button has not been redrawn yet -- it still shows the previous count. Reading
      // here compares this round's `freeSpins` against last round's label, so every round
      // mismatches by one and the case reports `0 == 8`: a perfect score for a game that is
      // counting down correctly.
      await ctx.uiRead(
        "async () => { const b = window.__gameTestBridge;"
        + " for (let i = 0; i < 200; i++) {"
        + "   const s = b && b.getSnapshot ? b.getSnapshot() : {};"
        + "   const busy = s.busyOps || [];"
        + "   if (!busy.includes('reels') && !busy.includes('winCountUp')"
        + "       && !busy.includes('balanceCountUp') && !busy.includes('roundReady')) return true;"
        + "   await new Promise(r => setTimeout(r, 50)); }"
        + " return false; }");
      const shown = (await ctx.uiRead(
        "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
        + " return s.freeSpinsRemaining; }")) ?? null;
      // Both values are kept, not just the verdict. A systematic offset is a different finding
      // from a random one, and `0 of 8` cannot tell them apart.
      counterReadings.push({ seq: bet.seq, shown, response: bet.free_spins });
      counterHits.push(pyEq(shown, bet.free_spins));
    };

    const rounds = await playOutFreespins(ctx, trigger, { onRound: readCounter });
    // expose interesting sub-clips as marks (internal logic; QA just names them via `at:`)
    const marks: Record<string, unknown> = {};
    const fs = rounds.filter((r) => r.phase === "freespin");
    if (fs.length) {
      let big = fs[0];                         // FS round with most cascades (the FIRST, on a tie)
      for (const r of fs) if (r.cascades > big.cascades) big = r;
      marks.big_cascade = big.seq;
    }
    const raw0 = or(trigger.raw, {}) as Dict;
    // How LONG the bonus actually was. A run with a tight `max_win_x` can have it capped after one
    // round (Rule 9), which still satisfies terminal_reached and {terminal.free_spins} == 0 while
    // showing the player almost nothing -- so a case that grades the bonus UNFOLDING (a counter
    // counting down, a multiplier sampled across rounds) asserts this instead.
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks,
        facts: {
          free_spins_played: fs.length,
          capped: rounds.some((r) => r.has_max_win),
          total_rounds: rounds.length,
          fs_rounds: fs.length,
          counter_matches_rounds: counterHits.filter((ok) => ok).length,
          counter_readings: counterReadings,
          // every distinct (shown - response); [0] is correct, [1] is a label one spin behind, a
          // mixed list is a real inconsistency
          counter_offsets: pySorted(new Set(counterReadings
            .filter((r) => isNumber(r.shown))
            .map((r) => Number(r.shown) - r.response))),
          triggers_skipped: skipped,
          // the trigger's own shape, for a case that grades the AWARD against it (null on a
          // profile that cannot read the grid)
          trigger_bonus_reels: hasAttr(GAME, "bonusReels")
            ? (or(census().bonusReels(raw0), []) as unknown[]).length
            : null,
          trigger_max_bonus_per_reel: hasAttr(GAME, "maxScattersOnAReel")
            ? census().maxScattersOnAReel(raw0)
            : null,
        },
      });
  }
}

// =====================================================================================
// freespin-cascade
// =====================================================================================

export class FreeSpinCascade extends Scenario {
  static readonly PARAMS: readonly string[] = ["max_base", "max_triggers"];
  override readonly name = "freespin-cascade";
  override readonly description = "Trigger free spins and find a FREE SPIN with a cluster/cascade win (>= 2 cascades) "
    + "that is FOLLOWED by at least one more free spin — so both the multiplier CLIMB (that "
    + "cascade round) and the RESET on the next free spin are captured in one bonus. Retries "
    + "with fresh triggers if a bonus doesn't yield one. Exposes res = the cascade free spin; "
    + "marks: fs_cascade = that round, fs_reset = the following free spin.";

  readonly maxBase: number;        // base spins per attempt to reach a trigger
  readonly maxTriggers: number;    // how many bonuses to try before giving up

  constructor(p: Params = {}) {
    super();
    checkParams(FreeSpinCascade, p);
    this.maxBase = param(p, "max_base", 400);
    this.maxTriggers = param(p, "max_triggers", 5);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    for (let t = 0; t < this.maxTriggers; t++) {
      let trigger: Bet | null = null;
      for (let i = 0; i < this.maxBase; i++) {
        const b = await ctx.baseSpin();
        if (isFreespinTrigger(b)) {
          trigger = b;
          break;
        }
      }
      if (trigger === null) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], {
            reason: py`no free-spin trigger in ${this.maxBase} base spins`,
          });
      }
      const rounds = await playOutFreespins(ctx, trigger);   // play the whole bonus; settles back to base
      const fs = rounds.filter((r) => r.phase === "freespin");
      for (let i = 0; i < fs.length - 1; i++) {              // need a FOLLOWING free spin for the reset
        if (fs[i].cascades >= 2 && fs[i].payout > 0) {       // a cascade WIN inside the bonus
          return new ScenarioResult(this.name, true, fs[i], rounds, ctx.timeline, ctx.events,
            [start, ctx.nowS()], {
              round_windows: ctx.windows(),
              marks: { fs_cascade: fs[i].seq, fs_reset: fs[i + 1].seq },
            });
        }
      }
      // no qualifying cascade-then-spin in this bonus -> spin base again for another trigger
    }
    return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
      reason: py`no cascade free spin followed by another free spin in ${this.maxTriggers} bonuses`,
    });
  }
}

// =====================================================================================
// Shared texts and close helpers
// =====================================================================================

/** (Unused in the Python too; kept with it.) `%s` is the profile's close-control name. */
export const BET_CLOSE_ASK = "the denomination list would not close via the `bet_close` control "
  + "(profile name: %s). Either the bridge does not know that name, or it resolves but the click "
  + "is inert. BRIDGE ASK: confirm the close control's name with `getAllButtonRects()` while the "
  + "list is OPEN, and make it reject rather than resolve when it cannot do its job "
  + "(docs/game-bridge-contract.md §8).";

// NOTE: an earlier version of this string asserted that NOTHING on the Chaos build opens the
// denomination popup. That was wrong — `stake_up` (`stakeIncrease` -> native `betPlus`) does open
// it, verified live 2026-09-02. It is kept generic because the scenarios that use it run on any
// build, and it must describe what the run OBSERVED rather than a conclusion about one game.
export const BET_DRIVE_ASK = "the stake control did not open a denomination popup and did not step the bet, so "
  + "there is no way to drive the bet on this build. BRIDGE ASK: a named control that "
  + "opens the bet popup, or main-screen steppers that work without it.";

/** `%s` is the History close control's name (or `none`). */
export const HISTORY_CLOSE_ASK = "the History panel would not close. The close control is `%s`; either the bridge does not know "
  + "that name, it resolves but the click is inert, or dismissPopups() does not cover this popup. "
  + "Confirm with `getAllButtonRects()` while History is OPEN, and make dismissPopups() report "
  + "honestly -- it must return only what it VERIFIED closed "
  + "(docs/game-bridge-contract.md; games/zeus/bridge-api.md §6).";

/**
 * After closing a surface reached THROUGH a panel (profile `BUTTON_VIA`), close that panel too if
 * the game went back to it rather than to the main screen. Grandma: History and the guide live in
 * Settings, so closing History may leave `SettingPopup` open. Uses the panel's own close control
 * (`<opener role>_close`, e.g. `settings_close`), falling back to `dismissPopups()`. Returns true
 * when a panel was left open and was closed here.
 */
export async function closeOpenerIfLeft(ctx: DriveContext, name: string): Promise<boolean> {
  const opener = GAME.via(name);
  const panel = opener ? GAME.popup(opener) : null;
  if (!panel || !(await ctx.popups()).includes(panel)) return false;
  if (!(await ctx.clickButton(`${opener}_close`, { optional: true, timeoutMs: 5000 }))) {
    await ctx.dismissPopups();
  }
  return true;
}

/**
 * Close the History panel (and any detail over it), PROVE it closed, and SAY SO STEP BY STEP. The
 * History twin of `closeBetList`: the same 70-second hang was reachable through this path, when
 * `_inspect` ended with a bare `dismiss_popups()` + `wait_spin_clickable` inside `except: pass`.
 *
 * **Why it logs every step rather than just returning a verdict.** `closed: false` says the screen
 * was not clear; it does not say whether the close control was MISSING, present but INERT, or
 * present and working with something else on top -- three different fixes. So each step goes to
 * BOTH stdout and the timeline. A healthy pass reads:
 *
 *     close-history [normal_win]: open before -> history
 *     close-history [normal_win]: click `history:close` -> clicked in 0.44s; still open -> none
 *     close-history [normal_win]: CLOSED via the close button in 0.5s; spin clickable in 0.28s
 *
 * facts: `closed` / `close_button_worked` / `needed_dismiss` / `popups_after` / `close_seconds` /
 * `spin_ready_seconds`
 */
export async function closeHistory(ctx: DriveContext, opts: { timeoutMs?: number; label?: string } = {}): Promise<Dict> {
  const timeoutMs = opts.timeoutMs ?? 8000;
  const label = opts.label ?? "";
  const tag = label ? `close-history [${label}]` : "close-history";
  const t0 = ctx.nowS();

  // ANY visible popup counts, not just the profile's label -- for the reason spelled out in
  // closeBetList: when the profile's spelling and the build's disagree, a label-only check answers
  // "already closed" while the panel is on screen.
  const openNow = async (): Promise<string[]> => [...(await ctx.popups())];

  const log = (line: string, bad = false): void => {
    console.log(`  ${bad ? "!! " : ""}${tag}: ${line}`);
    ctx.step("close-history", `${tag}: ${line}`, ctx.nowS());
  };

  const before = await openNow();
  log(`open before -> ${before.join(", ") || "none"}`);
  if (!before.length) {
    // Worth its own line rather than a silent early return: "there was nothing to close" and "I
    // closed it" are the same boolean, and only one of them exercises the close control.
    log("nothing was open -- no close attempted");
    return {
      closed: true, close_button_worked: true, needed_dismiss: false,
      popups_after: [], close_seconds: pyRound(ctx.nowS() - t0, 2),
      spin_ready_seconds: null,
    };
  }

  // `history_close` is null on a build that publishes no close control; clickButton treats a null
  // role as not-clickable rather than burning the timeout, so this costs nothing there. A round
  // DETAIL open on top of the list covers `history:close` -- a player closes the detail first, then
  // History. Without this every close waited out the full timeout on a covered button and had to
  // be rescued by dismissPopups() (Grandma, 2026-09-30).
  const detailPopup = GAME.popup("history_detail");
  const detailClose = GAME.button("detail_close");
  if (detailPopup && detailClose && before.includes(detailPopup)) {
    const td = ctx.nowS();
    const shut = await ctx.clickButton("detail_close", { optional: true, timeoutMs });
    log(py`click \`${detailClose}\` (the detail sits on top of the list) -> `
      + py`${shut ? "clicked" : "NOT CLICKABLE"} in ${pyRound(ctx.nowS() - td, 2)}s`,
    !shut);
  }

  const role = GAME.button("history_close");
  const tb = ctx.nowS();
  const clicked = await ctx.clickButton("history_close", { optional: true, timeoutMs });
  // History reached THROUGH a panel (profile `BUTTON_VIA`, Grandma: Settings) can close back into
  // that panel instead of the main screen. That is the game working, not the close failing.
  if (clicked && (await closeOpenerIfLeft(ctx, "history"))) {
    log(py`closed the \`${GAME.via("history")}\` panel History was opened from`);
  }
  const afterClick = await openNow();
  const worked = clicked && !afterClick.length;
  if (role === null) {
    log("no `history_close` control in this profile -- going straight to dismissPopups()");
  } else if (clicked) {
    log(py`click \`${role}\` -> clicked in ${pyRound(ctx.nowS() - tb, 2)}s; `
      + `still open -> ${afterClick.join(", ") || "none"}`);
  } else {
    log(py`click \`${role}\` -> NOT CLICKABLE after ${pyRound(ctx.nowS() - tb, 2)}s`, true);
  }

  let neededDismiss = false;
  if (!worked) {
    const td = ctx.nowS();
    const verified = await ctx.dismissPopups();
    neededDismiss = true;
    // dismissPopups() returns only what it OBSERVED close -- a short list means something refused.
    log(`dismissPopups() verified closed -> ${verified.join(", ") || "nothing"} `
      + py`(${pyRound(ctx.nowS() - td, 2)}s)`, !verified.length);
  }

  const left = await openNow();
  const secs = pyRound(ctx.nowS() - t0, 2);
  let spinReady: number | null = null;
  if (left.length) {
    // Do NOT go on to wait for spin readiness: a visible popup blocks it by contract, so the wait
    // could only end in a timeout. Say what is still up instead -- that IS the evidence.
    log(py`STILL OPEN -> ${left.join(", ")}  (${secs}s)`, true);
    console.log("     a visible popup is a blocking reason for waitForSpinClickable, so every later "
      + "wait in this run will refuse while that is up.");
    console.log(`     BRIDGE ASK: ${HISTORY_CLOSE_ASK.replace("%s", () => pyStr(or(role, "none")))}`);
  } else {
    const how = worked ? "the close button" : "dismissPopups()";
    const ts = ctx.nowS();
    try {
      await waitSpinClickable(ctx.page, timeoutMs);
      spinReady = pyRound(ctx.nowS() - ts, 2);
      log(py`CLOSED via ${how} in ${secs}s; spin clickable in ${spinReady}s`);
    } catch {
      // The screen IS clear, so this is not the popup -- something else holds readiness. Its own
      // line, because it sends you to a different bug than "it would not close".
      log(py`CLOSED via ${how} in ${secs}s, but spin did NOT become clickable within `
        + py`${timeoutMs / 1000}s -- the screen is clear, so look past the popup`, true);
    }
  }
  return {
    closed: !left.length, close_button_worked: !!worked,
    needed_dismiss: neededDismiss, popups_after: left,
    close_seconds: secs, spin_ready_seconds: spinReady,
  };
}

// =====================================================================================
// change-bet
// =====================================================================================

export class ChangeBet extends Scenario {
  static readonly PARAMS: readonly string[] = ["target_index"];
  override readonly name = "change-bet";
  override readonly description = "Change the bet to a specific denomination. Takes whichever path the build offers: "
    + "open the denomination list and tap a row, or — when there is no list "
    + "(profile.HAS_BET_POPUP is False) — step the stake control until it lands. Places "
    + "NO bet, so there is no stream data; exposes facts.picked_bet / picked_index and "
    + "what it ACTUALLY landed on (landed_bet / landed_index / bet_changed), plus the "
    + "video window for an AI check that the on-screen amount matches.";

  /** Denomination index to pick; null -> auto-pick a non-current one. */
  readonly targetIndex: number | null;

  constructor(p: Params = {}) {
    super();
    checkParams(ChangeBet, p);
    this.targetIndex = param<number | null>(p, "target_index", null);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const denoms = (await ctx.eval("() => { const b = window.__gameTestBridge; "
      + "const s = b.getSnapshot ? b.getSnapshot() : null; "
      + "return s ? s.denominations : null; }")) ?? null;
    const cur = (await ctx.eval(BET_INDEX_JS)) ?? null;
    if (!truthy(denoms)) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { reason: "no denominations available from the bridge" });
    }
    const list = denoms as unknown[];
    const n = list.length;
    let target: number = this.targetIndex !== null ? this.targetIndex : (n > 3 ? 3 : 0);
    if (cur !== null && pyEq(cur, target)) {         // the current row is deliberately not re-selectable
      target = pyMod(target + 1, n);
    }
    const picked = pyIndex(list, target);

    // Ask the game, do not trust the flag: open the list and see whether it appeared.
    if (await openBetList(ctx)) {
      ctx.mark(py`BET LIST: pick row ${target} (${picked})`);
      await ctx.clickButton(GAME.button("bet_row", { i: target }) as string, { optional: true, timeoutMs: 15000 });
      await ctx.clickButton(GAME.button("bet_close") as string, { optional: true, timeoutMs: 8000 });
    } else {
      // No list to open: the stake controls step the bet one denomination at a time. Walk from the
      // current index to the target, which exercises BOTH directions when the target is below the
      // current bet -- and is the only path a build like this offers.
      const steps = target - (isPyInt(cur) && Number(cur) >= 0 ? Number(cur) : 0);
      const role = steps > 0 ? "stakeIncrease" : "stakeDecrease";
      ctx.mark(py`BET: step ${steps > 0 ? "up" : "down"} ${Math.abs(steps)}x to ${picked}`);
      for (let i = 0; i < Math.abs(steps); i++) {
        if (!(await ctx.clickButton(role, { optional: true, timeoutMs: 10000, dwellMs: ctx.stepDwellMs }))) break;
      }
      await ctx.dwell();                             // let the final amount sit in frame
    }
    await ctx.settle();
    const landed = (await ctx.eval(BET_INDEX_JS)) ?? null;
    const landedBet = (await ctx.eval(BET_AMOUNT_JS)) ?? null;
    const facts: Dict = {
      picked_index: target, picked_bet: picked, prev_index: cur,
      landed_index: landed, landed_bet: landedBet, bet_changed: !pyEq(landed, cur),
      denom_count: n, min_bet: list[0], max_bet: list[n - 1],
    };
    if (pyEq(landed, cur)) {
      // Nothing moved. That is not "the game refuses to change bet" -- it is this suite being
      // unable to DRIVE the change, so it is unreached (inconclusive), not a failure. A build that
      // later grows the missing control makes this pass with no code change.
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          facts,
          reason: py`the bet never left index ${cur} (${landedBet}) -- ` + BET_DRIVE_ASK,
        });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        facts: {
          picked_index: target, picked_bet: picked, prev_index: cur,
          // what the game ACTUALLY landed on -- so "the bet changed" is a hard check rather than an
          // assumption that the taps worked
          landed_index: landed, landed_bet: landedBet,
          bet_changed: !pyEq(landed, cur),
          denom_count: n, min_bet: list[0], max_bet: list[n - 1],
        },
      });
  }
}

// =====================================================================================
// browse-denominations
// =====================================================================================

const BROWSE_INFO_JS = `() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const gi = b.getGameInfo ? b.getGameInfo() : null;
            // \`getGameSettings()\` is gone from the contract and from bridge v3 -- settings live
            // on getGameInfo(). The old fallback was already dead: both reads prefer gi.settings.
            const st = (gi && gi.settings) || {};
            return { denoms: s.denominations || st.denominations || [], currency: gi ? gi.currency : null }; }`;

export class BrowseDenominations extends Scenario {
  static readonly PARAMS: readonly string[] = [];
  override readonly name = "browse-denominations";
  override readonly description = "Open the bet-amount list and scroll to the HIGHEST option then the LOWEST, so a "
    + "SCROLLABLE list reveals both ends on screen (not every option is visible at once). "
    + "Places no bet. Exposes facts.denom_count / min_bet / max_bet / currency for an AI "
    + "check that the list matches the configured denominations.";

  constructor(p: Params = {}) {
    super();
    checkParams(BrowseDenominations, p);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const info = await ctx.eval(BROWSE_INFO_JS);
    const denoms = or(get(info, "denoms"), []) as unknown[];
    if (!truthy(denoms)) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { reason: "no denominations available from the bridge" });
    }
    const n = denoms.length;
    const startIdx = (await ctx.eval(BET_INDEX_JS)) ?? null;
    let closing: Dict = {
      closed: true, close_button_worked: true, needed_dismiss: false,
      popups_after: [],                           // no list opened -> nothing to close
    };
    const shown = await openBetList(ctx);
    if (shown) {
      await ctx.scrollBetRow(n - 1);              // scroll to the biggest option
      await ctx.scrollBetRow(0);                  // then to the smallest option
      closing = await closeBetList(ctx);          // ...and PROVE the list went away
    } else {
      // No list on this build -- step the stake up a few notches and back so the range is still
      // shown on screen, which is what the AI check actually looks at.
      ctx.mark("BET: step up through several denominations");
      for (let i = 0; i < Math.min(4, n - 1); i++) {
        await ctx.clickButton(GAME.button("stake_up") as string, {
          optional: true, timeoutMs: 10000, dwellMs: ctx.stepDwellMs,   // each amount must be seen
        });
      }
      await ctx.dwell();
      ctx.mark("BET: step back down");
      for (let i = 0; i < Math.min(4, n - 1); i++) {
        await ctx.clickButton(GAME.button("stake_down") as string, {
          optional: true, timeoutMs: 10000, dwellMs: ctx.stepDwellMs,
        });
      }
      await ctx.dwell();
    }
    await ctx.settle();
    const landed = (await ctx.eval(BET_INDEX_JS)) ?? null;
    // `list_shown` is the proof the list was on screen. There is deliberately no "browsed = the bet
    // index moved" fact: this scenario only SCROLLS the list and closes it keeping the current bet,
    // so the index never moves on a working game -- asserting it went red on DR-02 while the list
    // plainly opened. The index pair stays as a diagnostic.
    const facts: Dict = {
      denom_count: n, min_bet: denoms[0], max_bet: denoms[n - 1],
      currency: get(info, "currency"), has_bet_popup: GAME.HAS_BET_POPUP,
      start_index: startIdx, end_index: landed, list_shown: shown,
    };
    Object.assign(facts, closing);
    if (!shown && pyEq(landed, startIdx)) {
      // The denominations came from CONFIG, so asserting on them would pass whether or not the
      // player can actually see them. Without a list on screen there is nothing to check.
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          facts,
          reason: "the denomination list was never shown -- " + BET_DRIVE_ASK,
        });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], { facts });
  }
}

// =====================================================================================
// boot-base
// =====================================================================================

const BOOT_INFO_JS = `() => {
            const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            const gi = b.getGameInfo ? b.getGameInfo() : null;
            // \`getGameSettings()\` is gone from the contract and from bridge v3 -- settings live
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
                         : null }; }`;

export class BootBase extends Scenario {
  static readonly PARAMS: readonly string[] = ["dwell_s"];
  override readonly name = "boot-base";
  override readonly description = "Sit in a clean base game (no spins) and record it. Exposes gameInfo facts — "
    + "bet_amount, bet_index, balance, currency, denom_count, min_bet, max_bet — for "
    + "the UI and denomination checks.";

  /** Seconds of idle base game to record for the UI check. */
  readonly dwellS: number;

  constructor(p: Params = {}) {
    super();
    checkParams(BootBase, p);
    this.dwellS = param(p, "dwell_s", 2);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const info = await ctx.eval(BOOT_INFO_JS);
    await sleep(this.dwellS * 1000);     // let the idle base UI sit in frame for the AI
    // A full-resolution still of the loaded base screen. "Is every UI element present and not
    // broken" is exactly the question a montage frame is worst at and a still is best at, so this
    // scenario captures one and a case can grade it with `image:` instead of `video:`.
    await ctx.screenshot("base");
    await ctx.settle();
    const d = or(get(info, "denoms"), []) as unknown[];
    // `defaultBetLevel` is an INDEX into denominations, and `default_bet_known` is the gate: without
    // it, comparing against a missing value would read as a confident pass on no data.
    const lvl = get(info, "defaultBetLevel");
    const defaultAmount = lvl !== null && !pyLt(lvl, 0) && pyLt(lvl, d.length) ? pyIndex(d, lvl) : null;
    // THE FIRST-SESSION RULE (docs/game-bridge-contract.md §5; games/zeus/bridge-api.md §3).
    // `defaultBetLevel` governs only a player's FIRST session -- every later one opens at
    // `lastBet.baseStake`. So the bet a correct game opens at is:
    //     expected_open_bet = lastBet ? lastBet.baseStake : denominations[defaultBetLevel]
    // A case that instead asserts `betIndex == defaultBetLevel` fails a correct game on every
    // returning session, which is most of them. That is what `opens_at_default` below does, and it
    // is kept UNCHANGED on purpose: no suite asserts it, the contract's changelog records the
    // decision to leave it, and quietly redefining a published fact is worse than adding one. A
    // "game opens at the default bet" case binds `opens_at_expected_bet`.
    const lastStake = get(info, "lastBetStake");
    const firstSession = lastStake === null;
    const expectedOpen = !firstSession ? lastStake : defaultAmount;
    const betAmount = get(info, "betAmount");
    const facts: Dict = {
      bet_amount: betAmount, bet_index: get(info, "betIndex"),
      balance: get(info, "balance"), currency: get(info, "currency"),
      denom_count: d.length, min_bet: d.length ? d[0] : get(info, "minBet"),
      max_bet: d.length ? d[d.length - 1] : get(info, "maxBet"),
      default_bet_level: lvl, default_bet_known: lvl !== null,
      default_bet_amount: defaultAmount,
      // the first-session rule, and the inputs that decided it
      last_bet_stake: lastStake, first_session: firstSession,
      expected_open_bet: expectedOpen,
      // `null` when nothing published an expectation -- NOT false, which would read as "the game
      // opened at the wrong bet" on a build that simply did not say
      opens_at_expected_bet: expectedOpen === null || betAmount === null
        ? null
        : Math.abs(pyFloat(betAmount) - pyFloat(expectedOpen)) < 1e-9,
      // LATENT, and deliberately left as it was -- see the note above
      opens_at_default: lvl !== null && pyEq(get(info, "betIndex"), lvl),
    };
    // shots: ctx.shots is what makes the captured still reachable by an `image:` assert --
    // capturing it is not enough, the result has to carry it.
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], { facts, shots: ctx.shots });
  }
}

// =====================================================================================
// base-spin
// =====================================================================================

export class BaseSpin extends Scenario {
  static readonly PARAMS: readonly string[] = [];
  override readonly name = "base-spin";
  override readonly description = "One base spin of ANY outcome (win or lose). Exposes res = that spin, for "
    + "balance-debit / spin-mechanic checks.";

  constructor(p: Params = {}) {
    super();
    checkParams(BaseSpin, p);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const [b, rounds] = await spinAndFinish(ctx);   // a base spin may TRIGGER -- play the bonus out
    return new ScenarioResult(this.name, true, b, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], { round_windows: ctx.windows(), marks: { spin: b.seq } });
  }
}

// =====================================================================================
// auto-play
// =====================================================================================

export class AutoPlay extends Scenario {
  static readonly PARAMS: readonly string[] = ["count", "stop_probe_ms", "stop_dwell_ms", "setup_dwell_ms"];
  override readonly name = "auto-play";
  override readonly description = "Start auto-play for N spins (auto -> auto:count -> auto:confirm); the CLIENT then "
    + "auto-spins and we collect all N base results in order, then it stops. If an auto "
    + "spin TRIGGERS free spins, the client auto-plays the whole bonus — we drain those "
    + "rounds (they don't count toward N) and then resume collecting base spins. Exposes "
    + "rounds + facts.auto_count / rounds_played / bonus_count for the auto checks.\n"
    + "facts.stakes_seen / stake_constant answer 'the bet did not change during the "
    + "sequence' EXACTLY — an AI read of the bet label cannot tell 'never changed' from "
    + "'changed and changed back between two sampled frames'. "
    + "After the count is reached it WATCHES for another bet for stop_probe_ms and holds "
    + "the stopped screen for stop_dwell_ms, so 'auto stopped' is evidence "
    + "(facts.auto_stopped / extra_round_after_stop / auto_flag_after) and the clip "
    + "actually contains the stopped game instead of ending on the last payout.";

  /** auto:count:<idx> -> spins */
  static readonly COUNT_INDEX: ReadonlyMap<number, number> = new Map([[10, 0], [20, 1], [30, 2], [50, 3]]);

  /** The spin count at an autoplay index, per the profile (null = unlimited). */
  static countAt(i: number): number | null {
    const vals = GAME.AUTO_COUNTS;
    return i >= 0 && i < vals.length ? vals[i] : null;
  }

  /** An index to select BEFORE the real one, so the count visibly changes. null when the target is
   * not the panel's opening value and the selection is already observable. Only index 0 needs it —
   * that is what the panel opens on. Prefer a FINITE, clearly different count (never the unlimited
   * entry, whose label is a symbol rather than a number). */
  viaIndex(idx: number): number | null {
    if (idx !== 0) return null;
    for (let i = GAME.AUTO_COUNTS.length - 1; i > 0; i--) {
      if (GAME.AUTO_COUNTS[i] !== null) return i;          // skip the Infinity entry
    }
    return null;
  }

  readonly count: number;
  /** The three SETUP clicks (open panel -> pick count -> confirm) each change the screen and are then
   * immediately replaced by the next one. At the default 2s dwell the montage samples roughly two
   * frames per step, one of which can land mid-animation -- so "the panel showed the chosen count"
   * is unwatchable, for a human and for the AI. Hold each one longer, and MARK it so a
   * timestamp-mode assert can say exactly when it happened. */
  readonly setupDwellMs: number;
  /** How long to WATCH FOR ANOTHER BET after the count is reached. Auto stopping is the claim under
   * test, so it is proved by nothing further being wagered -- not assumed because we counted N.
   * Doubles as footage of the stopped game. */
  readonly stopProbeMs: number;
  /** ...and how long to hold the settled, stopped screen afterwards, so the AI has frames of "auto
   * is over" rather than a clip that cuts the moment the last payout lands. */
  readonly stopDwellMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(AutoPlay, p);
    this.count = param(p, "count", 10);
    this.setupDwellMs = param(p, "setup_dwell_ms", 2000);
    this.stopProbeMs = param(p, "stop_probe_ms", 6000);
    this.stopDwellMs = param(p, "stop_dwell_ms", 3000);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // Python's dict lookup: only a NUMBER key can match (10.0 finds 10; True, "10" do not).
    const idx = typeof this.count === "number" ? (AutoPlay.COUNT_INDEX.get(this.count) ?? 0) : 0;
    // The panel OPENS on index 0 (Mystic: 10 — confirmed live, confirming with no count click gives
    // the same autoSpinsRemaining as clicking auto:count:0). Selecting the count it is already
    // showing changes nothing, on screen or in state, so "the player chose N" is unwatchable and an
    // AI asked to see it answers no, correctly. Rather than pay for a longer run just to make the
    // step visible, DETOUR: select a different count first, then come back. The panel visibly moves
    // out and back, the selection control is demonstrably working, and the run is still the
    // shortest one. (Verified live: auto:count:2 then auto:count:0 lands on 10.)
    const viaIdx = this.viaIndex(idx);
    ctx.mark("OPEN the auto-play panel");
    await ctx.clickButton(GAME.button("auto") as string, { dwellMs: this.setupDwellMs });
    if (viaIdx !== null) {
      ctx.mark(py`SELECT ${AutoPlay.countAt(viaIdx)} first, so the count visibly CHANGES`);
      await ctx.clickButton(GAME.button("auto_count", { i: viaIdx }) as string, { dwellMs: this.setupDwellMs });
    }
    ctx.mark(py`CHANGE BACK to the ${this.count}-spins option`);
    await ctx.clickButton(GAME.button("auto_count", { i: idx }) as string, { dwellMs: this.setupDwellMs });
    ctx.mark("CONFIRM pressed");
    // dwellMs 0: read the count the client actually accepted BEFORE spins start burning it
    await ctx.clickButton(GAME.button("auto_confirm") as string, { dwellMs: 0 });
    const started = or(await ctx.uiRead(AUTO_JS, {}), {}) as Dict;
    await ctx.dwell(this.setupDwellMs);
    const rounds: Bet[] = [];   // every recorded round (auto base spins + any drained bonus rounds)
    let baseSpins = 0;          // count of AUTO BASE spins only -> this is what auto_count refers to
    let bonusCount = 0;         // how many auto spins triggered a bonus
    while (pyLt(baseSpins, this.count)) {
      const b = await ctx.observeBet();             // next auto base spin (client fires it; no click)
      rounds.push(b);
      baseSpins += 1;
      if (isFreespinTrigger(b)) {
        // the client now auto-plays the whole bonus; drain it (no settle -> auto resumes the next
        // base spin on its own) and don't count bonus rounds toward auto_count.
        bonusCount += 1;
        const fs = await playOutBonus(ctx, b, { settle: false });
        rounds.push(...fs.slice(1));                // fs[0] is the trigger, already appended
      }
    }
    ctx.mark(py`LAST of ${this.count} auto spins done -- watching for any further spin`);
    await ctx.settle();                             // let the final auto spin's presentation finish
    const stop = await confirmStopped(ctx, {
      watchMs: this.stopProbeMs, dwellMs: this.stopDwellMs, label: py`AUTO STOPPED after ${this.count} spins`,
    });
    const lock = or(await ctx.uiRead(BUTTON_WATCH_READ_JS, {}), {}) as Dict;
    const paidStakes = new Set(rounds.filter((r) => r.phase !== "freespin").map((r) => r.stake));
    return new ScenarioResult(this.name, true, rounds.length ? rounds[rounds.length - 1] : null, rounds,
      ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: {
          auto_count: this.count, rounds_played: baseSpins,
          // what the client accepted, read the instant auto started. `rounds_played == auto_count`
          // proves the count was applied; this shows WHAT it applied when they disagree.
          auto_spins_at_start: get(started, "left"),
          count_is_panel_default: idx === 0,
          // the count we detoured through to make the change visible
          via_count: viaIdx !== null ? AutoPlay.countAt(viaIdx) : null,
          ...stop,                                  // stopped / extra_round_after_stop
          auto_stopped: stop.stopped,               // kept: cases bind this name
          bonus_count: bonusCount, total_rounds: rounds.length,
          // paid rounds only: a free spin legitimately costs 0, so including them would always
          // report the bet as "changed".
          stakes_seen: paidStakes.size,
          // Which controls a player could still reach while the run was going. `[]` is the claim: a
          // bet stepper or a Buy Bonus button live mid-autoplay is a real defect.
          buttons_polled_during_auto: pyInt(or(get(or(lock, {}), "polls"), 0)),
          buttons_enabled_during_auto: pySorted(Object.keys(or(get(or(lock, {}), "enabled"), {}) as Dict)),
          stake_constant: paidStakes.size <= 1,
          completed: !pyLt(baseSpins, this.count),
        },
      });
  }
}

// =====================================================================================
// spin-then-history
// =====================================================================================

type Outcome = readonly [key: string, label: string, test: (n: number) => boolean];

export class SpinThenHistory extends Scenario {
  static readonly PARAMS: readonly string[] = ["settle_before_history_s", "detail_scroll_steps", "dwell_ms",
    "list_timeout_ms", "max_spins", "outcomes"];
  override readonly name = "spin-then-history";
  override readonly description = "Inspect a PLAIN BASE round in History for each outcome shape the MECHANISM can "
    + "produce: a LOSING spin, a NORMAL win (one paying step) and — on a cascade game "
    + "only — a CASCADE win (two or more). A single-grid game (line, hold-and-spin) "
    + "reports one paying step for any win, so `cascade_win` is dropped automatically "
    + "and the hunt is just lose/win; narrow it further with `params: {outcomes: [...]}`. "
    + "It keeps spinning and, the moment a spin matches an "
    + "outcome still outstanding, stops and inspects THAT round: open History, open the "
    + "newest row's bet detail, scroll it by ONE PAGE PER PAYING STEP (0 for a loss, 1 for "
    + "a normal win, N for a cascade) — `detail_scroll_steps` overrides that for a build "
    + "whose detail does not page, then close up and carry on hunting the rest. One "
    + "spin sequence therefore covers them all, instead of a run each hunting "
    + "separately. Each inspection is MARKED as `no_win` / `normal_win` / `cascade_win`, "
    + "so a `video:` assert targets one with `at:`; each round's clip spans its own spin "
    + "through its own History walk. A spin that TRIGGERS the bonus is drained and skipped "
    + "— a bonus ticket is a different shape (one sub-ticket per free spin) and belongs to "
    + "`freespin-then-history`. Exposes facts.<outcome>_found / _cluster_wins / _scrolls / "
    + "_detail_opened / _round_id / _stake / _payout, plus outcomes_found / all_outcomes / "
    + "spins / bonuses_drained. Unreached only if NO outcome was inspected at all.";

  /**
   * (fact key, mark label, predicate on the round's PAYING-STEP count -- see winSteps()).
   *
   * `cascade_win` is impossible on a single-grid game: `winSteps()` reports 1 paying step for any
   * winning round outside a cascade mechanism, so nothing ever matches `n >= 2`. The constructor
   * drops it when GAME.mechanism != "cascade" rather than letting the hunt burn its whole budget.
   */
  static readonly OUTCOMES: readonly Outcome[] = [
    ["no_win", "NO WIN: a losing spin", (n) => n === 0],
    ["normal_win", "NORMAL WIN: one paying step", (n) => n === 1],
    ["cascade_win", "CASCADE WIN: two or more paying steps", (n) => n >= 2],
  ];
  /** `inspect` writes its facts as `${key}_<suffix>`, which a static walk over dict literals cannot
   * see — so every correct `{facts.no_win_stake}` assert would be reported as "not exposed" (and a
   * real typo would hide among the noise). Declaring the prefixes lets validate_suites expand
   * prefix x suffix; a MISSPELLED SUFFIX still fails. Keep in sync with OUTCOMES. */
  static readonly FACT_PREFIXES: readonly string[] = SpinThenHistory.OUTCOMES.map(([k]) => k);

  readonly settleBeforeHistoryS: number;    // let the round reach History first
  /** null -> DERIVE the page count from the response: one page per cluster win, so a losing spin
   * scrolls not at all. An int overrides it (debugging a detail that pages differently). */
  readonly detailScrollSteps: number | null;
  readonly dwellMs: number;                 // 2s on screen after each open
  readonly listTimeoutMs: number;           // how long the list/detail may take
  readonly maxSpins: number;                // budget for hunting all the outcomes
  readonly outcomes: Outcome[];

  constructor(p: Params = {}) {
    super();
    checkParams(SpinThenHistory, p);
    this.settleBeforeHistoryS = param(p, "settle_before_history_s", 2.0);
    this.detailScrollSteps = param<number | null>(p, "detail_scroll_steps", null);
    this.dwellMs = param(p, "dwell_ms", 2000);
    this.listTimeoutMs = param(p, "list_timeout_ms", 10000);
    this.maxSpins = param(p, "max_spins", 80);
    const outcomes = param<unknown>(p, "outcomes", null);
    // which outcomes to hunt; default all three. Narrow it to shorten a run.
    const want = truthy(outcomes)
      ? new Set<unknown>(pyList(outcomes))
      : new Set<unknown>(SpinThenHistory.OUTCOMES.map(([k]) => k));
    if (!truthy(outcomes) && GAME.mechanism !== "cascade") {
      // A single-grid game cannot produce two paying steps in one round, so hunting for one would
      // burn the whole budget. Drop it rather than fail on it.
      want.delete("cascade_win");
    }
    const known = new Set<unknown>(SpinThenHistory.OUTCOMES.map(([k]) => k));
    const bad = [...want].filter((k) => !known.has(k));
    if (bad.length) {
      throw new PyValueError(`unknown outcome(s): ${pySorted(bad).map((k) => pyStr(k)).join(", ")} -- known: `
        + `${pySorted([...known]).map((k) => pyStr(k)).join(", ")}`);
    }
    this.outcomes = SpinThenHistory.OUTCOMES.filter((o) => want.has(o[0]));
  }

  /** Open History, open the newest row's detail, scroll it by this round's cluster-win count, then
   * clear the screen so the hunt can continue. */
  private async inspect(ctx: DriveContext, bet: Bet, key: string, facts: Dict): Promise<void> {
    const pages = this.detailScrollSteps !== null ? this.detailScrollSteps : winSteps(bet);
    // The row is written server-side and can lag the settled animation, so WAIT BEFORE OPENING: the
    // list is built on open, and dwelling afterwards cannot recover a row that wasn't there.
    await sleep(this.settleBeforeHistoryS * 1000);
    ctx.mark(`HISTORY [${key}]: open the panel`);
    await ctx.clickButton(GAME.button("history") as string, { dwellMs: this.dwellMs });
    // Opening only KICKS OFF the list fetch -- wait for rows to actually render before reading.
    const rows = await ctx.waitForHistoryRows(this.listTimeoutMs);
    const top = rows.length ? rows[0] : null;       // index 0 = newest (bridge-qa-api.md)
    const ids = Object.entries(or(get(or(top, {}), "data"), {}) as Dict)
      .filter(([k, v]) => ["betid", "roundid", "id", "gameroundid"].includes(k.toLowerCase())
        && v !== null && v !== undefined && v !== "")
      .map(([, v]) => pyStr(v));
    let detailOpened = false;
    let scrolls = 0;
    const rect = or(get(or(top, {}), "rect"), {}) as Dict;
    if (get(rect, "centerX") !== null) {
      const beforePopups = new Set(await ctx.popups());   // baseline for the "something new opened" test
      ctx.mark(`BET DETAIL [${key}]: open the newest row`);
      await ctx.page.mouse.click(rect.centerX as number, rect.centerY as number);
      await ctx.dwell(this.dwellMs);                       // detail content loads too
      detailOpened = await ctx.waitForHistoryDetail(this.listTimeoutMs, beforePopups);
      if (detailOpened) {
        if (truthy(pages)) {
          ctx.mark(py`BET DETAIL [${key}]: scroll ${pages} page(s) -- one per cluster win`);
          scrolls = await ctx.scrollPopup({ steps: pages, label: `bet detail (${key})` });
        } else {
          ctx.mark(`BET DETAIL [${key}]: no cluster win -- nothing below the fold`);
          await ctx.dwell(this.dwellMs);                   // still hold it in frame for the AI
        }
      }
    }
    const wantId = pyStr(or(bet.round_id, ""));
    Object.assign(facts, {
      [`${key}_found`]: true,
      [`${key}_cluster_wins`]: winSteps(bet),
      [`${key}_win_clusters`]: winClusters(bet),
      [`${key}_cascades`]: bet.cascades,
      [`${key}_scrolls`]: scrolls,
      [`${key}_history_rows`]: rows.length,
      [`${key}_detail_opened`]: detailOpened,
      [`${key}_round_id`]: wantId || "none",
      [`${key}_round_id_match`]: wantId.length > 0 && ids.includes(wantId),
      [`${key}_stake`]: bet.stake,
      [`${key}_payout`]: bet.payout,
      // the payout AS THE CLIENT PRINTS IT (profile DISPLAY_*): what an AI read of the detail's win
      // amount must be compared with, not the raw figure
      [`${key}_payout_display`]: GAME.displayAmount(bet.payout),
    });
    // Clear the screen (detail + panel) so the next spin can run, and PROVE it cleared. This used to
    // be `dismiss_popups()` plus a 60s `wait_spin_clickable` in a bare `except: pass`, which turned a
    // panel that would not close into a silent 60s hang per inspected outcome. (Python note: the
    // facts are written as ONE literal with templated keys because validate_suites reads the
    // per-outcome suffixes out of dict literals.)
    const closed = await closeHistory(ctx, { label: key });
    Object.assign(facts, {
      [`${key}_panel_closed`]: closed.closed,
      [`${key}_popups_after`]: (closed.popups_after as string[]).join(",") || "none",
      // HOW it closed, so a green cannot hide a close control that never works: `closed` is true
      // whether the button did it or the dismissPopups() fallback rescued it, and only the second
      // is a bridge defect.
      [`${key}_close_button_worked`]: closed.close_button_worked,
      [`${key}_needed_dismiss`]: closed.needed_dismiss,
      // ...and how long, so the next 70-second stall is a NUMBER in the report rather than
      // something someone notices by watching the clock.
      [`${key}_close_seconds`]: closed.close_seconds,
      [`${key}_spin_ready_seconds`]: closed.spin_ready_seconds,
    });
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const rounds: Bet[] = [];
    const pending = [...this.outcomes];
    const facts: Dict = Object.fromEntries(SpinThenHistory.OUTCOMES.map(([k]) => [`${k}_found`, false]));
    const marks: Record<string, unknown> = {};
    const done: string[] = [];
    let bonuses = 0;
    let res: Bet | null = null;
    for (let i = 0; i < this.maxSpins; i++) {
      if (!pending.length) break;
      const b = await ctx.baseSpin();
      rounds.push(b);
      if (isFreespinTrigger(b)) {
        // A BONUS round: its ticket holds one sub-ticket per free spin, which is what
        // `freespin-then-history` covers. Drain it and keep hunting -- leaving it as the newest row
        // would put a bonus ticket in front of a case about a normal bet.
        bonuses += 1;
        const fs = await playOutBonus(ctx, b);
        rounds.push(...fs.slice(1));
        continue;
      }
      await ctx.settle();                          // base win/loss: let the presentation finish
      const clusterWinSteps = winSteps(b);
      const hit = pending.find((o) => o[2](clusterWinSteps)) ?? null;
      if (hit === null) continue;                  // an outcome we already have -> spin again
      const [key, label] = hit;
      pending.splice(pending.indexOf(hit), 1);
      ctx.mark(py`${label} (round ${b.seq}, ${clusterWinSteps} paying step(s))`);
      marks[key] = b.seq;                          // -> `video: at: <key>`
      await this.inspect(ctx, b, key, facts);
      done.push(key);
      if (res === null || key === "cascade_win") res = b;   // richest round becomes {res.*}
    }
    const r0: Bet | null = res;
    Object.assign(facts, {
      outcomes_found: done.join(",") || "none", outcome_count: done.length,
      all_outcomes: !pending.length,
      missing: pending.map(([k]) => k).join(",") || "none",
      spins: rounds.length, bonuses_drained: bonuses,
      is_base_round: r0 !== null && pyEq(r0.game_state, GAME.BASE_STATE) && r0.stake > 0,
    });
    if (!done.length) {
      return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(), facts,
          reason: py`no outcome inspected in ${rounds.length} spins (${bonuses} bonus round(s) drained)`,
        });
    }
    return new ScenarioResult(this.name, true, res, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], { round_windows: ctx.windows(), marks, facts });
  }
}

// =====================================================================================
// freespin-then-history
// =====================================================================================

export class FreespinThenHistory extends Scenario {
  static readonly PARAMS: readonly string[] = ["cap", "settle_before_history_s", "dwell_ms", "list_timeout_ms",
    "page_back"];
  override readonly name = "freespin-then-history";
  override readonly description = "Play a WHOLE free-spin round, then inspect it in History: open the panel, open the "
    + "newest row's bet detail, and page through EVERY sub-ticket with the detail's own "
    + "next/prev nav — in a free-spin round each free spin is its own bet, so paging walks "
    + "the free-spin chain. Each sub-ticket is scrolled by ONE PAGE PER CLUSTER WIN, "
    + "derived from that spin's response, so a losing free spin is not scrolled at all. "
    + "Paging is driven by `getHistoryDetailState().hasNext`, NOT by page arithmetic: the "
    + "popup's nav accounts for the synthetic max-win page, so counting would be off by "
    + "one exactly on max-win rounds. After reaching the end it pages BACK to the first "
    + "ticket, so prev is exercised too. Exposes facts.fs_rounds / bet_count / "
    + "pages_visited / paged_back / bet_count_matches_rounds / detail_scrolls. Unreached "
    + "if no bonus is hit, History shows no row, or the detail never opens.";

  readonly cap: number;                     // base spins to try before giving up
  readonly settleBeforeHistoryS: number;
  readonly dwellMs: number;
  readonly listTimeoutMs: number;
  readonly pageBack: boolean;               // also walk prev back to the first ticket

  constructor(p: Params = {}) {
    super();
    checkParams(FreespinThenHistory, p);
    this.cap = param(p, "cap", 60);
    this.settleBeforeHistoryS = param(p, "settle_before_history_s", 2.0);
    this.dwellMs = param(p, "dwell_ms", 2000);
    this.listTimeoutMs = param(p, "list_timeout_ms", 10000);
    this.pageBack = param(p, "page_back", true);
  }

  override async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    let trigger: Bet | null = null;
    let spins = 0;
    for (let i = 0; i < this.cap; i++) {
      const b = await ctx.baseSpin();
      spins += 1;
      if (isFreespinTrigger(b)) {
        trigger = b;
        break;
      }
      await ctx.settle();
    }
    if (trigger === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          facts: { base_spins: spins },
          reason: py`no free-spin trigger in ${spins} base spins`,
        });
    }
    const rounds = await playOutFreespins(ctx, trigger);   // drain the bonus, then settle
    // the round reaches History asynchronously -- wait BEFORE opening, the list is built on open
    await sleep(this.settleBeforeHistoryS * 1000);
    ctx.mark("HISTORY: open the in-game History panel");
    await ctx.clickButton(GAME.button("history") as string, { dwellMs: this.dwellMs });
    const rows = await ctx.waitForHistoryRows(this.listTimeoutMs);
    const facts: Dict = {
      base_spins: spins, fs_rounds: rounds.length - 1, history_rows: rows.length,
      bonus_total: pyRound(sum(rounds.slice(1).map((r) => r.payout)), 4),
      round_id: pyStr(or(or(rounds[rounds.length - 1].round_id, trigger.round_id), "none")),
    };
    if (!rows.length) {
      return new ScenarioResult(this.name, false, trigger, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(), facts,
          reason: "History rendered no rows -- the bonus round had not reached "
            + "History yet, or the panel did not open",
        });
    }
    const rect = or(get(rows[0], "rect"), {}) as Dict;
    if (get(rect, "centerX") === null) {
      return new ScenarioResult(this.name, false, trigger, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(), facts,
          reason: "the newest History row exposed no clickable rect",
        });
    }
    ctx.mark("BET DETAIL: open the bonus round's ticket");
    await ctx.page.mouse.click(rect.centerX as number, rect.centerY as number);
    await ctx.dwell(this.dwellMs);
    if (!(await ctx.waitForPopup(GAME.popup("history_detail"), this.listTimeoutMs))) {
      return new ScenarioResult(this.name, false, trigger, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(), facts,
          reason: "the bet detail (historyDetail) never opened",
        });
    }
    let st = await ctx.historyDetailState();
    // one expected page-count per bet in the ticket, in play order: the trigger, then each free
    // spin. Used to scroll each sub-ticket by its OWN cluster wins rather than a blanket number.
    const expect = rounds.map((r) => winSteps(r));
    facts.win_clusters = rounds.map((r) => pyStr(winClusters(r))).join(",");   // diagnostic
    const visited: number[] = [];
    let scrolls = 0;

    const show = async (idx: number): Promise<void> => {
      const pages = idx < expect.length ? pyIndex(expect, idx) : 0;
      if (pages) {
        ctx.mark(py`SUB-TICKET ${idx + 1}: scroll ${pages} page(s) -- one per cluster win`);
        scrolls += await ctx.scrollPopup({ steps: pages, label: py`sub-ticket ${idx + 1}` });
      } else {
        ctx.mark(py`SUB-TICKET ${idx + 1}: no cluster win -- nothing to scroll`);
        await ctx.dwell(this.dwellMs);       // still hold it on screen for the AI
      }
    };

    let idx = pyInt(or(get(st, "betIndex"), 0));
    visited.push(idx);
    await show(idx);
    const guard = rounds.length + 5;          // never trust hasNext to terminate on its own
    while (truthy(get(st, "hasNext")) && visited.length < guard) {
      if (!(await ctx.clickButton(GAME.button("detail_next") as string, {
        optional: true, timeoutMs: 8000, dwellMs: this.dwellMs,
      }))) break;
      st = await ctx.historyDetailState();
      idx = pyInt(or(get(st, "betIndex"), visited.length));
      visited.push(idx);
      await show(idx);
    }
    let pagedBack = 0;
    if (truthy(this.pageBack)) {
      ctx.mark("BET DETAIL: page BACK to the first sub-ticket");
      while (truthy(get(st, "hasPrev")) && pagedBack < guard) {
        if (!(await ctx.clickButton(GAME.button("detail_prev") as string, {
          optional: true, timeoutMs: 8000, dwellMs: this.dwellMs,
        }))) break;
        st = await ctx.historyDetailState();
        pagedBack += 1;
      }
    }
    const betCount = pyInt(or(get(st, "betCount"), 0));
    Object.assign(facts, {
      bet_count: betCount, pages_visited: visited.length,
      detail_scrolls: scrolls, paged_back: pagedBack,
      reached_last_page: !truthy(this.pageBack) ? !truthy(get(st, "hasNext")) : true,
      back_at_first: !truthy(get(st, "hasPrev")),
      // the ticket should hold one bet per round we played (trigger + each free spin)
      bet_count_matches_rounds: betCount === rounds.length,
      expected_pages: expect.map((x) => pyStr(x)).join(","),
    });
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], { round_windows: ctx.windows(), marks: { trigger: trigger.seq }, facts });
  }
}

export const SCENARIOS: ScenarioClass[] = [
  BaseWin, BaseClusterWin, FreeSpinComplete, FreeSpinCascade, ChangeBet, BrowseDenominations, BootBase,
  BaseSpin, AutoPlay, SpinThenHistory, FreespinThenHistory,
];
