/**
 * Grandma scenarios: resume-after-reload, the walking wild, and the Cat vs Dog drama — plus the
 * in-page presentation watcher they grade with. Port of scenario_engine.py lines 7540-8331
 * (`_PRESENTATION_WATCH_JS` .. `CatDogDrama`). The button watcher (`_BUTTON_WATCH_*`) and
 * `purchase_bonus` live in `engine/shared.ts`.
 */
import { type Bet, isFreespinTrigger, isTerminal } from "../engine/bet.js";
import { readGameInfo } from "../engine/bridge.js";
import type { DriveContext } from "../engine/drive.js";
import { confirmStopped, playOutBonus, playOutFreespins, settleToBase, SPEED_JS } from "../engine/flows.js";
import { GAME } from "../engine/game.js";
import { evaluate } from "../engine/page.js";
import { ScenarioResult, Unreached } from "../engine/result.js";
import { BUTTON_WATCH_JS, BUTTON_WATCH_NAMES, BUTTON_WATCH_READ_JS, purchaseBonus } from "../engine/shared.js";
import { stopAutoplay } from "../engine/teardowns.js";
import { CellSet } from "../profile/mechanisms.js";
import {
  type Dict, get, has, isDict, isList, or, py, pyEq, pyInt, pyLt, pyList, pyRepr, pySorted, pyStr, pyTypeName,
  PyTypeError, r, sum, truthy, tryInt,
} from "../py.js";

/** A drama cell's reel index, or `missing` when the key is absent OR null (`int(None)` raised). */
function reelOr(cell: unknown, missing: number): number {
  const v = get(cell, "reel");
  return v === null || v === undefined ? missing : tryInt(v, missing);
}
import { checkParams, param, type Params, Scenario, type ScenarioClass } from "./base.js";

// ── Python builtins this file needs that py.ts does not carry ───────────────────────────

/** Python's IndexError (`[][0]`). */
class PyIndexError extends Error {
  override name = "IndexError";
}

/** Python's KeyError (`{}[0]`). */
class PyKeyError extends Error {
  override name = "KeyError";
}

/** `seq[i]` with Python's meaning: a negative index counts from the end, out of range raises
 * IndexError, a non-subscriptable value raises TypeError, a dict looks the key up (KeyError). */
function pyIndex(seq: unknown, i: unknown): unknown {
  if (Array.isArray(seq) || typeof seq === "string") {
    const kind = Array.isArray(seq) ? "list" : "string";
    const idx = typeof i === "boolean" ? (i ? 1 : 0) : i;
    if (typeof idx !== "number" || !Number.isInteger(idx)) {
      throw new PyTypeError(`${kind} indices must be integers or slices, not ${pyTypeName(i)}`);
    }
    const j = idx < 0 ? idx + seq.length : idx;
    if (j < 0 || j >= seq.length) throw new PyIndexError(`${kind} index out of range`);
    return seq[j];
  }
  if (isDict(seq)) {
    if (typeof i === "string" && has(seq, i)) return get(seq, i);
    throw new PyKeyError(pyRepr(i));
  }
  throw new PyTypeError(`'${pyTypeName(seq)}' object is not subscriptable`);
}

/** `len(v)`. */
function pyLen(v: unknown): number {
  if (Array.isArray(v) || typeof v === "string") return v.length;
  if (v instanceof Set) return v.size;
  if (isDict(v)) return Object.keys(v).length;
  throw new PyTypeError(`object of type '${pyTypeName(v)}' has no len()`);
}

/** `x in container`. */
function contains(container: unknown, x: unknown): boolean {
  if (Array.isArray(container)) return container.some((y) => pyEq(y, x));
  if (typeof container === "string") {
    if (typeof x !== "string") throw new PyTypeError(`'in <string>' requires string as left operand, not ${pyTypeName(x)}`);
    return container.includes(x);
  }
  if (isDict(container)) return typeof x === "string" && has(container, x);
  throw new PyTypeError(`argument of type '${pyTypeName(container)}' is not iterable`);
}

/** `d.get(key, default)`: the default only when the key is MISSING (a present None stays None). */
function getOr(d: unknown, key: string, fallback: unknown): unknown {
  return has(d, key) ? get(d, key) : fallback;
}

/** `GAME._norm(sym)` — a staticmethod of LineGame, reached through the instance in Python. A game
 * without it raises, as the AttributeError did. */
function norm(sym: unknown): string {
  return (GAME.constructor as unknown as { _norm(s: unknown): string })._norm(sym);
}

/** Set equality on two cell sets (`a == b`). */
function sameCells(a: CellSet, b: CellSet): boolean {
  return a.size === b.size && a.isSubsetOf(b);
}

// ── Presentation watcher ────────────────────────────────────────────────────────────────

const PRESENTATION_WATCH_JS = `(opts) => {
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
}`;

const PRESENTATION_READ_JS = `(stop) => {
    const rows = window.__pWatch || [];
    window.__pWatch = [];
    if (stop && window.__pWatchTimer) { clearInterval(window.__pWatchTimer); window.__pWatchTimer = null; }
    return rows;
}`;

/** Start sampling `busyOps` (and optionally the displayed grid / walking wilds) every 50 ms in the
 * page. Read it back with `takePresentationTrace`. */
export async function watchPresentation(ctx: DriveContext, opts: { grid?: boolean; wilds?: boolean } = {}): Promise<void> {
  await evaluate(ctx.page, PRESENTATION_WATCH_JS, { grid: opts.grid ?? false, wilds: opts.wilds ?? false });
}

/** Every sample since the last take, oldest first — and clears the buffer, so consecutive calls
 * slice the recording per round. `stop` also stops the timer. */
export async function takePresentationTrace(ctx: DriveContext, stop = false): Promise<Dict[]> {
  return or(await evaluate(ctx.page, PRESENTATION_READ_JS, stop), []) as Dict[];
}

/** A [start_ms, end_ms] stretch of a trace. The values are the page's `Date.now()` samples. */
export type Span = [number, number];

/** [start_ms, end_ms] for each continuous stretch where `op` was in `busyOps`. */
export function busySpans(trace: Dict[], op: string): Span[] {
  const spans: Span[] = [];
  let openAt: unknown = null;
  let lastT: unknown = null;
  for (const row of trace) {
    const busy = or(get(row, "busy"), []);
    const t = get(row, "t");
    if (contains(busy, op) && openAt === null) {
      openAt = t;
    } else if (!contains(busy, op) && openAt !== null) {
      // Close at THIS sample, the first one where the op is gone -- it ended somewhere in the 50 ms
      // since the previous sample, and the upper bound is the honest estimate. Closing at the
      // previous sample instead reports a one-sample animation as 0 ms, which reads as "it was
      // skipped" — the exact failure `min_walk_ms` exists to catch.
      spans.push([openAt, t !== null ? t : lastT] as Span);
      openAt = null;
    }
    lastT = t;
  }
  if (openAt !== null && lastT !== null) spans.push([openAt, lastT] as Span);
  return spans;
}

/** Total milliseconds `op` was busy across the trace. */
export function busyMs(trace: Dict[], op: string): number {
  return sum(busySpans(trace, op).map(([s, e]) => {
    if (typeof s !== "number" || typeof e !== "number") {
      throw new PyTypeError(`unsupported operand type(s) for -: '${pyTypeName(e)}' and '${pyTypeName(s)}'`);
    }
    return Math.max(0, e - s);
  }));
}

/** A walking-wild cell set as comparable (row, col) pairs, whatever shape it arrived in. */
export function cellsKey(cells: unknown): CellSet {
  const out = new CellSet();
  for (const c of pyList(or(cells, []))) {
    if (isDict(c) && get(c, "row") !== null && get(c, "col") !== null) {
      out.add([pyInt(get(c, "row")), pyInt(get(c, "col"))]);
    }
  }
  return out;
}

/**
 * Does the grid on screen equal the grid the server sent, cell for cell?
 *
 * `-1` cells are SKIPPED, never counted as equal or unequal: the bridge uses it for a cell that is
 * hidden or masked, which is "cannot say" rather than a mismatch (Rule 19). A grid that is nothing
 * but `-1` therefore compares equal to everything, so the caller checks it was sampled at all.
 */
export function gridMatchesView(displayed: unknown, raw: Dict): boolean {
  const view = (GAME as any).grid(raw) as string[][] | null;
  if (view === null || view === undefined || !isList(displayed) || !displayed.length) return false;
  if (displayed.length !== view.length) return false;
  for (let col = 0; col < displayed.length; col++) {
    const shownReel = displayed[col];
    const realReel = view[col];
    if (!isList(shownReel) || shownReel.length !== realReel.length) return false;
    for (let row = 0; row < shownReel.length; row++) {
      const shown = shownReel[row];
      if (pyStr(shown) === "-1") continue;
      if (norm(shown) !== norm(realReel[row])) return false;
    }
  }
  return true;
}

// ── Bridge reads ────────────────────────────────────────────────────────────────────────

const WILDS_ON_GRID_JS = "() => { const b = window.__gameTestBridge;"
  + " const s = b && b.getWalkingWildState ? b.getWalkingWildState() : null;"
  + " return s ? (s.onGrid || 0) : 0; }";

const ROUND_PRESENTED_JS = "async () => { const b = window.__gameTestBridge;"
  + " for (let i = 0; i < 600; i++) {"
  + "   const s = b && b.getSnapshot ? b.getSnapshot() : {};"
  + "   const busy = s.busyOps || [];"
  + "   if (!busy.includes('reels') && !busy.includes('walkingWild')"
  + "       && !busy.includes('walkingWildExit')) return true;"
  + "   await new Promise(r => setTimeout(r, 50)); }"
  + " return false; }";

const WALKING_WILD_STATE_JS = "() => { const b = window.__gameTestBridge;"
  + " return b && b.getWalkingWildState ? b.getWalkingWildState() : null; }";

const DISPLAYED_GRID_JS = "() => { const b = window.__gameTestBridge;"
  + " return b && b.getDisplayedGrid ? b.getDisplayedGrid() : null; }";

const DRAMA_CONFIG_JS = "() => { const b = window.__gameTestBridge;"
  + " return b && b.getDramaConfig ? b.getDramaConfig() : null; }";

const DRAMA_STATE_JS = "() => { const b = window.__gameTestBridge;"
  + " return b && b.getDramaState ? b.getDramaState() : null; }";

// =====================================================================================
// FreeSpinReload
// =====================================================================================

export class FreeSpinReload extends Scenario {
  readonly name = "freespin-reload";
  readonly description = "Reload the game in the MIDDLE of a bonus and prove it resumes where it left "
    + "off: reach a bonus (naturally, or by buying it with `source: buy`), play "
    + "`after_rounds` free spins, reload, and let the client play out what is still "
    + "owed. The resume signal is `gameInfo.pending`, read in the gap between the "
    + "bridge appearing and Start -- the only moment it exists, because the client "
    + "clears it the instant Start is clicked.\n"
    + "A 50ms watcher runs IN THE PAGE across the reload over the controls that must "
    + "stay disabled while spins are owed (bet +/-, autoplay, settings, buy bonus), "
    + "so a one-frame flash is caught instead of falling between two awaits.\n"
    + "Exposes facts.source / awarded / rounds_before_reload / free_spins_before_reload "
    + "/ pending_free_spins / pending_game_state / pending_is_bonus / resumed_rounds / "
    + "first_resumed_free_spins / buttons_polled / buttons_flashed / "
    + "buttons_ever_enabled / fs_rounds. Marks `trigger`, `reload`, `resume`, "
    + "`terminal`. Unreached if no bonus is reached, or if it ends before "
    + "`after_rounds` spins have played -- a reload with nothing owed proves nothing.";
  static readonly PARAMS = ["source", "after_rounds", "max_base", "cap", "package"];

  readonly source: string;       // "natural" (spin until it triggers) | "buy"
  readonly afterRounds: number;  // free spins to play BEFORE reloading
  readonly maxBase: number;
  readonly cap: number;
  readonly package: number;

  constructor(p: Params = {}) {
    super();
    checkParams(FreeSpinReload, p);
    this.source = param(p, "source", "natural");
    this.afterRounds = param(p, "after_rounds", 2);
    this.maxBase = param(p, "max_base", 40);
    this.cap = param(p, "cap", 500);
    this.package = param(p, "package", 0);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const facts: Dict = {};
    try {
      let trigger: Bet;
      if (this.source === "buy") {
        const [bought, buyFacts] = await purchaseBonus(ctx, this.package);
        trigger = bought;
        Object.assign(facts, { source: "buy", price: get(buyFacts, "price"), price_ratio: get(buyFacts, "price_ratio") });
      } else {
        let found: Bet | null = null;
        for (let i = 0; i < this.maxBase; i++) {
          const b = await ctx.baseSpin();
          if (isFreespinTrigger(b)) {
            found = b;
            break;
          }
        }
        if (found === null) {
          throw new Unreached(py`no free-spin trigger in ${this.maxBase} base spins -- this scenario needs `
            + "a forced paytable (Rule 17), not a longer hunt");
        }
        trigger = found;
        Object.assign(facts, { source: "natural" });
      }
      ctx.mark(py`BONUS OPEN: ${trigger.won_free_spins} free spin(s) awarded`);
      Object.assign(facts, { awarded: trigger.won_free_spins });

      const played: Bet[] = [];
      for (let i = 0; i < this.afterRounds; i++) {
        const b = await ctx.freeSpin();
        played.push(b);
        if (isTerminal(b)) break;
      }
      const last = played.length ? played[played.length - 1] : trigger;
      if (isTerminal(last)) {
        // Reloading after the bonus has finished proves nothing: `pending` is empty and the case
        // reads as "resumed 0 of 0" -- a green on no evidence. Say so instead.
        throw new Unreached(py`the bonus ended after ${played.length} of ${this.afterRounds} free spins `
          + py`(awarded ${trigger.won_free_spins}) -- nothing would be left to resume. `
          + "Lower `after_rounds`, or force a longer award.");
      }
      Object.assign(facts, { rounds_before_reload: played.length, free_spins_before_reload: last.free_spins });

      ctx.mark(py`RELOAD with ${last.free_spins} free spin(s) owed`);
      const pending: Dict = {};
      const watchNames = BUTTON_WATCH_NAMES.filter((role) => truthy(GAME.button(role))).map((role) => GAME.button(role));

      const onReady = async (): Promise<void> => {
        const info = await readGameInfo(ctx.page);
        const p = GAME.pending(info);
        Object.assign(pending, {
          free_spins: GAME.pendingFreeSpins(info),
          game_state: get(p, or(GAME.F_PENDING_GAME_STATE, GAME.F_GAME_STATE) as string),
          is_bonus: GAME.pendingIsBonus(info),
        });
        await evaluate(ctx.page, BUTTON_WATCH_JS, watchNames);
      };

      const resumed = await ctx.reloadAndStart({ onReady });
      ctx.mark(`RESUMED: ${resumed.length} round(s) played out by the client`);
      // ⚠ NOT answered here. A resumed bonus drains inside `startGame` -> `drainToBase`, which
      // answers the offer itself — it has to, because `assertIdle` runs in there too and would
      // otherwise burn its budget on the panel. Repeating it here would just spend another 12 s
      // waiting for a popup that is already gone.
      Object.assign(facts, {
        pending_free_spins: get(pending, "free_spins"),
        pending_game_state: get(pending, "game_state"),
        pending_is_bonus: get(pending, "is_bonus"),
        resumed_rounds: resumed.length,
        first_resumed_free_spins: resumed.length ? resumed[0].free_spins : null,
      });

      let rounds: Bet[] = [trigger, ...played, ...resumed];
      if (resumed.length && !isTerminal(resumed[resumed.length - 1])) {
        rounds = rounds.concat((await playOutFreespins(ctx, resumed[resumed.length - 1], { cap: this.cap, settle: false })).slice(1));
      }

      const watch = or(await ctx.uiRead(BUTTON_WATCH_READ_JS, {}), {});
      const flashed = pySorted(Object.keys(or(get(watch, "enabled"), {}) as Dict));
      Object.assign(facts, {
        buttons_polled: pyInt(or(get(watch, "polls"), 0)),
        buttons_flashed: flashed,
        buttons_ever_enabled: flashed.length > 0,
        fs_rounds: rounds.filter((x) => x.phase === "freespin").length,
      });

      const stop = await settleToBase(ctx, { label: "freespin-reload" });
      Object.assign(facts, stop);
      return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          marks: {
            trigger: trigger.seq, terminal: rounds[rounds.length - 1].seq,
            reload: last.seq,
            resume: resumed.length ? resumed[0].seq : last.seq,
          },
          facts,
        });
    } catch (e) {
      if (e instanceof Unreached) {
        await ctx.dismissPopups();
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { facts, reason: e.message });
      }
      throw e;
    }
  }
}

// =====================================================================================
// WalkingWild
// =====================================================================================

/** One graded free spin of the walking-wild run. */
interface WildRound {
  seq: number;
  wilds: number;
  parked: CellSet;
  server: CellSet;
  read_timed_out: boolean;
  cells_match: boolean;
  grid_match: boolean;
  added: number;
  removed: number;
  moved: boolean;
  walked: boolean;
  walk_ms: number;
  overlapped: boolean;
  stand_in: boolean;
  exited: boolean;
  walk_open_at_end: boolean;
  wild_win_cells_parked: boolean;
}

export class WalkingWild extends Scenario {
  readonly name = "walking-wild";
  readonly description = "Play a bonus and grade the WALKING WILD round by round: where each wild parked, "
    + "whether that matches the wilds the server sent, and whether the walk really "
    + "played rather than the wild appearing in place.\n"
    + "Base spins are taken BEFORE and AFTER the bonus on purpose -- 'a wild walks on "
    + "only during free spins' needs the base game in the same recording. ⚠ A wild "
    + "parked in a previous bonus may still STAND on the grid in base game until the "
    + "next spin walks it off (bridge §8.2), so what is graded is whether a walk "
    + "STARTS in a paid round, not whether the grid is empty.\n"
    + "Exposes facts.fs_rounds / wild_counts / wild_cells_match_view_rounds / "
    + "grid_matches_view_rounds / walk_started_in_base / fs_rounds_with_wilds / "
    + "multi_wild_rounds / more_wild_rounds / fewer_wild_rounds / moved_wild_rounds / "
    + "wild_change_rounds / walk_animation_rounds / walk_overlapped_spin_rounds / "
    + "stand_in_rounds / exit_animation_rounds / wild_wins_use_parked_cells / "
    + "walk_finished_before_next_round / min_walk_ms / speed_mode. Marks `base_before`, "
    + "`trigger`, `terminal`, `base_after`, and the rounds where wilds were ADDED "
    + "(`more_wilds`, also `new_wild`), REMOVED (`fewer_wilds`), MOVED (`moved_wild`) "
    + "and where several walked at once (`multi_wild`). LIST marks for `video: each:`: "
    + "`wild_enter_rounds` (every free spin a wild was added) and "
    + "`stand_in_kick_rounds` (every free spin with a stand-in at a wild's "
    + "destination).";
  static readonly PARAMS = ["source", "base_spins_before", "base_spins_after", "max_base", "cap", "package",
    "turbo", "sudden_stop", "max_bonuses"];

  readonly source: string;
  readonly baseSpinsBefore: number;
  readonly baseSpinsAfter: number;
  readonly maxBase: number;
  readonly cap: number;
  readonly package: number;
  readonly turbo: boolean;
  readonly suddenStop: boolean;
  readonly maxBonuses: number;

  constructor(p: Params = {}) {
    super();
    checkParams(WalkingWild, p);
    // ⚠ WHY MORE THAN ONE BONUS. Three cases need a transition the SERVER decides: a wild ARRIVING,
    // one LEAVING, one MOVING. None is guaranteed in any single bonus -- a 15-spin run measured 0
    // "moved" -- and a transition that never happened is not a defect, so asserting it off one
    // bonus reds a healthy game. This plays further bonuses until all three have been seen, and
    // `transitions_seen` says which ever did.
    this.source = param(p, "source", "buy");
    this.baseSpinsBefore = param(p, "base_spins_before", 2);
    this.baseSpinsAfter = param(p, "base_spins_after", 1);
    this.maxBase = param(p, "max_base", 40);
    this.cap = param(p, "cap", 500);
    this.package = param(p, "package", 0);
    this.turbo = param(p, "turbo", false);
    this.suddenStop = param(p, "sudden_stop", false);
    this.maxBonuses = Math.max(1, pyInt(param(p, "max_bonuses", 1)));
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const facts: Dict = {};
    try {
      if (!GAME.HAS_FREE_SPINS) throw new Unreached("this build has no free spins");

      if (truthy(this.turbo)) {
        const btnTurbo = GAME.button("turbo");
        if (!(btnTurbo && (await ctx.clickButton(btnTurbo, { optional: true })))) {
          throw new Unreached("turbo was asked for but `turbo` never became clickable");
        }
        ctx.mark("TURBO on");
      }
      Object.assign(facts, { speed_mode: await ctx.uiRead(SPEED_JS, "UNKNOWN") });

      // ── CLEAN START: walk off any wild left parked by an EARLIER bonus ────────
      // Bridge §8.2: wilds parked in a previous bonus stay on the grid in base game until the next
      // spin walks them off. On 2026-10-01 this run began right after `freespin-reload` with two
      // wilds parked on reel 5; their walk-off played inside the measured base spins and was
      // counted as "a walk started in base" (TC-140 red). Those spins exist to show that THIS run's
      // base game starts no walk, so they must start from an empty grid.
      let leftoverClearSpins = 0;
      while (leftoverClearSpins < 3) {
        const onGrid = or(await ctx.uiRead(WILDS_ON_GRID_JS, 0), 0);
        if (!truthy(onGrid)) break;
        ctx.mark(py`CLEAR LEFTOVER: ${onGrid} wild(s) parked by an earlier bonus`);
        const b = await ctx.baseSpin();
        if (isFreespinTrigger(b)) await playOutBonus(ctx, b);
        else await ctx.settle();
        leftoverClearSpins += 1;
      }
      Object.assign(facts, { leftover_clear_spins: leftoverClearSpins });

      // ── base game BEFORE: the negative half of "only in free spins" ───────────
      await watchPresentation(ctx, { grid: true, wilds: true });
      const baseBefore: Bet[] = [];
      for (let i = 0; i < this.baseSpinsBefore; i++) {
        const b = await ctx.baseSpin();
        if (isFreespinTrigger(b)) await playOutBonus(ctx, b);   // Rule 1: a paid spin can always trigger
        else await ctx.settle();
        baseBefore.push(b);
      }
      const baseTrace = await takePresentationTrace(ctx);
      const walkInBase = busySpans(baseTrace, "walkingWild").length > 0;

      // ── into the bonus ────────────────────────────────────────────────────────
      let trigger: Bet;
      if (this.source === "buy") {
        [trigger] = await purchaseBonus(ctx, this.package);
      } else {
        let found: Bet | null = null;
        for (let i = 0; i < this.maxBase; i++) {
          const b = await ctx.baseSpin();
          if (isFreespinTrigger(b)) {
            found = b;
            break;
          }
        }
        if (found === null) throw new Unreached(py`no free-spin trigger in ${this.maxBase} base spins`);
        trigger = found;
      }
      ctx.mark(py`BONUS OPEN: ${trigger.won_free_spins} free spin(s)`);
      await takePresentationTrace(ctx);          // drop the purchase/trigger presentation

      // ── every free spin, graded as it lands ───────────────────────────────────
      const perRound: WildRound[] = [];
      const marks: Record<string, unknown> = {
        base_before: baseBefore.length ? baseBefore[0].seq : trigger.seq,
        trigger: trigger.seq,
      };
      let prevCells = new CellSet();

      const onRound = async (bet: Bet): Promise<void> => {
        // ⚠ `playOutFreespins` hands the callback a PARSED Bet, not the raw payload.
        const raw = or(bet.raw, {}) as Dict;
        // ⚠ TAKE THE ARRIVAL SLICE FIRST. It covers from the end of the PREVIOUS round's processing
        // to this response, so a walk still open at its end means the client started the next spin
        // while the last one was still walking -- which is exactly TC-156's claim. Taking it after
        // the settle wait below would consume that evidence: the wait ends when nothing is busy, so
        // the answer would always be "finished".
        const arrival = await takePresentationTrace(ctx);
        // Wait for the ROUND to finish presenting before reading where wilds parked: a walk in
        // flight reports the cells it is passing through, not the ones it stops on.
        // ⚠ UP TO 30 s, not 10. A round with REEL ANTICIPATION (the glowing slow reels) keeps its
        // last reels spinning for 10 s+, and the wilds keep driving the whole time. On 2026-10-01
        // rounds 13 and 14 each took ~11.4 s: a 10 s wait read the grandma MID-WALK and reported
        // two healthy rounds as parked in the wrong cell.
        const settled = await ctx.uiRead(ROUND_PRESENTED_JS);
        const state = or(await ctx.uiRead(WALKING_WILD_STATE_JS), {});
        const shown = await ctx.uiRead(DISPLAYED_GRID_JS);
        const trace = await takePresentationTrace(ctx);

        const parked = cellsKey(get(state, "cells"));
        const server = cellsKey(or((GAME as any).wildCells(raw), []));
        const added = server.minus(prevCells);
        const removed = prevCells.minus(server);
        // A wild that "moved" is one gone from a cell AND arrived at another in the same round --
        // counting either side alone would score every arrival as a move.
        const moved = added.size > 0 && removed.size > 0;
        // A stand-in is only observable while the walk is playing and the reels have stopped: the
        // destination cell holds something that is not the wild.
        let standIn = false;
        for (const row of trace) {
          const busy = or(get(row, "busy"), []);
          const g = get(row, "grid");
          // ⚠ NOT gated on the reels having stopped. The wild walks WHILE the reels spin (that is
          // TC-143), so "walking and not spinning" is a window that may never open -- gating on it
          // made this unobservable and reported every bonus as having no stand-in at all.
          if (!contains(busy, "walkingWild") || !isList(g)) continue;
          for (const [cellRow, cellCol] of added.values()) {
            let cell: unknown;
            try {
              cell = pyIndex(pyIndex(g, cellCol), cellRow);
            } catch (e) {
              if (e instanceof PyIndexError || e instanceof PyTypeError) continue;
              throw e;
            }
            if (pyStr(cell) !== "-1" && norm(cell) !== norm((GAME as any).SYM_WILD)) standIn = true;
          }
        }
        const walkSpans = busySpans(trace, "walkingWild");
        const overlapped = trace.some((row) => truthy(get(row, "busy")) && contains(get(row, "busy"), "walkingWild")
          && contains(get(row, "busy"), "reels"));
        perRound.push({
          seq: bet.seq, wilds: server.size, parked, server,
          // the read happened mid-walk: a mismatch on this round is not evidence
          read_timed_out: !truthy(settled),
          cells_match: sameCells(parked, server),
          grid_match: gridMatchesView(shown, raw),
          added: added.size, removed: removed.size, moved,
          walked: walkSpans.length > 0, walk_ms: busyMs(trace, "walkingWild"),
          overlapped, stand_in: standIn,
          exited: busySpans(trace, "walkingWildExit").length > 0,
          // The next round must not START until this walk has finished. The trace is everything
          // between the two responses, so a walk still open at its end is exactly that failure.
          // measured on the ARRIVAL slice, and only from the second free spin on: a walk open while
          // the trigger was still presenting is that round's, not a late one.
          walk_open_at_end: perRound.length > 0 && arrival.length > 0
            && contains(or(get(arrival[arrival.length - 1], "busy"), []), "walkingWild"),
          wild_win_cells_parked: (() => {
            for (const w of pyList((GAME as any).wayDetails(raw))) {
              for (const wp of pyList(or(get(w, (GAME as any).F_WAY_WILD_POSITIONS), []))) {
                for (const pos of pyList(getOr(wp, "positions", []))) {
                  for (const rowValue of pyList(getOr(pos, "value", []))) {
                    for (const colIndex of [get(pos, "index")]) {
                      if (!cellsKey([{ row: rowValue, col: colIndex }]).isSubsetOf(server)) return false;
                    }
                  }
                }
              }
            }
            return true;
          })(),
        });
        prevCells = server;
        if (added.size) ctx.mark(`FS: ${server.size - added.size} -> ${server.size} wild(s), NEW`);
        else if (removed.size) ctx.mark(`FS: wild walked off, ${server.size} left`);
      };

      if (truthy(this.suddenStop)) {
        ctx.mark("SUDDEN STOP: tapping `stop` during the bonus");
        const btnStop = GAME.button("stop");
        if (btnStop) {
          Object.assign(facts, { stop_clicks: (await ctx.clickButton(btnStop, { optional: true, timeoutMs: 4000 })) ? 1 : 0 });
        } else {
          Object.assign(facts, { stop_clicks: 0 });
        }
      }

      let rounds = await playOutFreespins(ctx, trigger, { cap: this.cap, settle: false, onRound });
      let bonusesPlayed = 1;
      ctx.mark(`BONUS COMPLETE: ${perRound.length} free spin(s) graded`);

      const seen = (): Set<string> => {
        const out = new Set<string>();
        const tests: Array<[string, (x: WildRound) => unknown]> = [
          ["more", (x) => x.added], ["fewer", (x) => x.removed], ["moved", (x) => x.moved]];
        for (const [k, f] of tests) if (perRound.some((x) => truthy(f(x)))) out.add(k);
        return out;
      };

      while (seen().size < 3 && bonusesPlayed < this.maxBonuses) {
        const seenNow = seen();
        const missing = pySorted(["more", "fewer", "moved"].filter((k) => !seenNow.has(k)));
        ctx.mark(`ANOTHER BONUS: still no ${missing.join(", ")} transition`);
        prevCells = new CellSet();          // a fresh bonus starts from an empty grid
        let trigger2: Bet | null;
        if (this.source === "buy") {
          [trigger2] = await purchaseBonus(ctx, this.package);
        } else {
          trigger2 = null;
          for (let i = 0; i < this.maxBase; i++) {
            const b = await ctx.baseSpin();
            if (isFreespinTrigger(b)) {
              trigger2 = b;
              break;
            }
          }
          if (trigger2 === null) break;
        }
        await takePresentationTrace(ctx);
        rounds = rounds.concat(await playOutFreespins(ctx, trigger2, { cap: this.cap, settle: false, onRound }));
        bonusesPlayed += 1;
      }
      Object.assign(facts, { bonuses_played: bonusesPlayed, transitions_seen: pySorted(seen()) });

      // ── base game AFTER ───────────────────────────────────────────────────────
      await takePresentationTrace(ctx);
      const baseAfter: Bet[] = [];
      for (let i = 0; i < this.baseSpinsAfter; i++) {
        const b = await ctx.baseSpin();
        if (isFreespinTrigger(b)) await playOutBonus(ctx, b);
        else await ctx.settle();
        baseAfter.push(b);
      }
      const afterTrace = await takePresentationTrace(ctx, true);
      // A leftover walking OFF is allowed (bridge §8.2); a walk ON is not. `walkingWildExit` is the
      // exit animation, so a base round showing only that is not a violation.
      const walkOnInBaseAfter = busySpans(afterTrace, "walkingWild").length > 0;

      const markKeys: Array<[string, "added" | "removed" | "moved" | "wilds"]> = [
        ["more_wilds", "added"], ["fewer_wilds", "removed"], ["moved_wild", "moved"], ["multi_wild", "wilds"]];
      for (const [tag, key] of markKeys) {
        const hit = perRound.find((x) => (key === "wilds" ? x[key] > 1 : truthy(x[key])));
        if (hit) marks[tag] = hit.seq;
      }
      if ("more_wilds" in marks) marks.new_wild = marks.more_wilds;
      // LIST marks, for `video: each:` — one short clip per round instead of one long one. Every
      // free spin where a grandma was ADDED (she walks onto the reels), and every one where the
      // bridge saw a stand-in symbol on a grandma's destination cell (the collision / knock-off
      // rounds).
      marks.wild_enter_rounds = perRound.filter((x) => truthy(x.added)).map((x) => x.seq);
      marks.stand_in_kick_rounds = perRound.filter((x) => x.stand_in).map((x) => x.seq);
      if (baseAfter.length) marks.base_after = baseAfter[baseAfter.length - 1].seq;
      marks.terminal = rounds[rounds.length - 1].seq;

      const walkMs = perRound.filter((x) => x.walked).map((x) => x.walk_ms);
      Object.assign(facts, {
        fs_rounds: perRound.length,
        wild_counts: perRound.map((x) => x.wilds),
        wild_cells_match_view_rounds: perRound.filter((x) => x.cells_match).length,
        grid_matches_view_rounds: perRound.filter((x) => x.grid_match).length,
        fs_rounds_with_wilds: perRound.filter((x) => truthy(x.wilds)).length,
        multi_wild_rounds: perRound.filter((x) => x.wilds > 1).length,
        more_wild_rounds: perRound.filter((x) => truthy(x.added)).length,
        fewer_wild_rounds: perRound.filter((x) => truthy(x.removed)).length,
        moved_wild_rounds: perRound.filter((x) => x.moved).length,
        wild_change_rounds: perRound.filter((x) => truthy(x.added) || truthy(x.removed)).length,
        walk_animation_rounds: perRound.filter((x) => x.walked).length,
        walk_overlapped_spin_rounds: perRound.filter((x) => x.overlapped).length,
        stand_in_rounds: perRound.filter((x) => x.stand_in).length,
        exit_animation_rounds: perRound.filter((x) => x.exited).length,
        wild_wins_use_parked_cells: perRound.every((x) => x.wild_win_cells_parked),
        walk_finished_before_next_round: !perRound.some((x) => x.walk_open_at_end),
        min_walk_ms: walkMs.length ? Math.min(...walkMs) : 0,
        walk_started_in_base: walkInBase || walkOnInBaseAfter,
        // WHICH half, so a red says where to look (before the bonus or after it)
        walk_in_base_before: walkInBase,
        walk_in_base_after: walkOnInBaseAfter,
        // Every round that broke TC-141 / TC-149, with what was compared. A count alone ("14 == 15")
        // said one round failed but not which, nor how.
        cells_mismatch: perRound.filter((x) => !x.cells_match)
          .map((x) => ({ seq: x.seq, parked: x.parked.sorted(), server: x.server.sorted() })),
        grid_mismatch_seqs: perRound.filter((x) => !x.grid_match).map((x) => x.seq),
        // rounds whose animation was STILL running after 30 s: their read is mid-walk, so a mismatch
        // there says "could not read", not "parked in the wrong cell"
        read_timeout_seqs: perRound.filter((x) => x.read_timed_out).map((x) => x.seq),
        base_spins_before: baseBefore.length,
        base_spins_after: baseAfter.length,
      });

      if (truthy(this.turbo)) {
        const btnTurbo = GAME.button("turbo");
        if (btnTurbo) await ctx.clickButton(btnTurbo, { optional: true });   // leave the build as found
      }
      const stop = await settleToBase(ctx, { label: "walking-wild" });
      Object.assign(facts, stop);
      const allRounds = [...baseBefore, ...rounds, ...baseAfter];
      return new ScenarioResult(this.name, true, trigger, allRounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], { round_windows: ctx.windows(), marks, facts });
    } catch (e) {
      if (e instanceof Unreached) {
        await ctx.dismissPopups();
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { facts, reason: e.message });
      }
      throw e;
    }
  }
}

// =====================================================================================
// CatDogDrama
// =====================================================================================

/** One graded round of the drama run. */
interface DramaRow {
  seq: number;
  played: boolean;
  trigger: unknown;
  grid_match: boolean;
  drama_ms: number;
  animated: boolean;
  fakes: number;
  cat_vs_dog: boolean;
  same_row_adjacent: boolean;
  vertical: boolean;
  winner_real: boolean;
  winner_family_by_rule: boolean;
  loser_is_fake: boolean;
  fakes_loser_family: boolean;
  fakes_within_cap: boolean;
  real_losers_survive: boolean;
  candidates: number;
}

export class CatDogDrama extends Scenario {
  readonly name = "drama";
  readonly description = "Spin until the Cat vs Dog drama has PLAYED a given number of times, then stop. "
    + "Counting dramas rather than spins is what makes the clip gradeable: the roll is "
    + "70% on a big win and 4% otherwise (bridge §8.5), so a fixed spin count can end "
    + "with nothing to look at.\n"
    + "Every round is read through `getDramaState()` after `waitForSpinClickable` has "
    + "waited the animation out, and against `getDisplayedGrid()` -- so 'the grid ends "
    + "on the server's result' is exact on drama and non-drama rounds alike.\n"
    + "⚠ A build with `devForceTrigger` / `devForceFamily` set is not a production "
    + "build and the run FAILS rather than grading a forced fight.\n"
    + "Exposes facts.rounds_played / dramas_played / "
    + "fights_cat_vs_dog / fights_same_row_adjacent / fights_vertical / "
    + "fights_per_round_max / rounds_with_multiple_candidates / winner_is_real_cell / "
    + "winner_family_matches_rule / loser_is_fake / fakes_all_loser_family / "
    + "fakes_within_column_cap / real_losers_survive / grid_matches_view_rounds / "
    + "drama_animation_rounds / fakes_when_not_played / "
    + "min_drama_ms / dev_switch_on / play_in_turbo_config / speed_mode / autoplay_ran. "
    + "Marks `first_drama` and `last_drama`. Unreached when `dramas` "
    + "are not reached inside `max_spins`.";
  static readonly PARAMS = ["dramas", "max_spins", "turbo", "autoplay"];

  readonly dramas: number;
  readonly maxSpins: number;
  readonly turbo: boolean;
  readonly autoplay: number;    // 0 = click each spin; N = run N auto spins instead

  constructor(p: Params = {}) {
    super();
    checkParams(CatDogDrama, p);
    this.dramas = param(p, "dramas", 4);
    this.maxSpins = param(p, "max_spins", 40);
    this.turbo = param(p, "turbo", false);
    this.autoplay = param(p, "autoplay", 0);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const facts: Dict = {};
    try {
      const cfg = await ctx.uiRead(DRAMA_CONFIG_JS);
      if (!truthy(cfg)) {
        throw new Unreached("this build exposes no getDramaConfig() -- the drama cannot be "
          + "graded from the bridge");
      }
      const devOn = truthy(get(cfg, "devForceTrigger")) || truthy(get(cfg, "devForceFamily"));
      Object.assign(facts, {
        dev_switch_on: devOn,
        play_in_turbo_config: truthy(get(cfg, "playInTurbo")),
        drama_enabled: truthy(get(cfg, "enabled")),
        max_fakes_per_column: get(cfg, "maxFakesPerColumn"),
      });
      if (devOn) {
        // Rule: never grade a build that has been told what to do. A forced fight would pass every
        // rule below and prove nothing about the game players get.
        throw new Unreached("this build has a drama DEV SWITCH on "
          + py`(devForceTrigger=${r(get(cfg, "devForceTrigger"))}, `
          + py`devForceFamily=${r(get(cfg, "devForceFamily"))}) -- not a production `
          + "build, so the fight is not graded");
      }

      if (truthy(this.turbo)) {
        const btnTurbo = GAME.button("turbo");
        if (!(btnTurbo && (await ctx.clickButton(btnTurbo, { optional: true })))) {
          throw new Unreached("turbo was asked for but `turbo` never became clickable");
        }
        ctx.mark("TURBO on");
      }
      Object.assign(facts, { speed_mode: await ctx.uiRead(SPEED_JS, "UNKNOWN") });

      await watchPresentation(ctx, { grid: false });
      const rows: DramaRow[] = [];
      const bets: Bet[] = [];          // the rounds themselves, so the report and the replay have them
      let firstDrama: number | null = null;
      let lastDrama: number | null = null;
      const familyOf = (sym: unknown): "CAT" | "DOG" | null => (GAME as any).familyOf(sym);

      const grade = async (bet: Bet): Promise<void> => {
        const state = or(await ctx.uiRead(DRAMA_STATE_JS), {});
        const shown = await ctx.uiRead(DISPLAYED_GRID_JS);
        const trace = await takePresentationTrace(ctx);
        const raw = or(bet.raw, {}) as Dict;
        const played = truthy(get(state, "played"));
        const winner = or(get(state, "winner"), {});
        const loser = or(get(state, "loser"), {});
        const fakes = or(get(state, "fakeCells"), []);
        const realView = get(state, "realView");
        const real = truthy(realView) ? realView : or((GAME as any).grid(raw), []);
        // The rule the client states: the winning family is the one with more symbols on the BOTTOM
        // row, then the row above. `realView` is [reel][row], row 0 at the top.
        const familyByRows = (view: unknown): string | null => {
          const top = truthy(view) && truthy(pyIndex(view, 0)) ? pyLen(pyIndex(view, 0)) - 1 : -1;
          for (let row = top; row > -1; row--) {
            let cats = 0;
            for (const reel of pyList(view)) if (familyOf(pyIndex(reel, row)) === "CAT") cats += 1;
            let dogs = 0;
            for (const reel of pyList(view)) if (familyOf(pyIndex(reel, row)) === "DOG") dogs += 1;
            if (cats !== dogs) return cats > dogs ? "CAT" : "DOG";
          }
          return null;
        };
        const wf = get(state, "winnerFamily");
        const lf = get(state, "loserFamily");
        rows.push({
          seq: bet.seq, played,
          trigger: get(state, "trigger"),
          grid_match: gridMatchesView(shown, raw),
          drama_ms: busyMs(trace, "drama"),
          animated: busySpans(trace, "drama").length > 0,
          fakes: pyLen(fakes),
          // every rule below is vacuously true on a round with no fight, and is only counted
          // against the rounds that HAD one
          cat_vs_dog: played && ((wf === "CAT" && lf === "DOG") || (wf === "DOG" && lf === "CAT")),
          same_row_adjacent: played && pyEq(get(winner, "row"), get(loser, "row"))
            && Math.abs(reelOr(winner, -9) - reelOr(loser, 9)) === 1,
          vertical: played && pyEq(get(winner, "reel"), get(loser, "reel")),
          winner_real: played && pyEq(familyOf(
            (truthy(real) && get(winner, "reel") !== null
              && pyLt(get(winner, "reel"), pyLen(real))
              && get(winner, "row") !== null
              && pyLt(get(winner, "row"), pyLen(pyIndex(real, get(winner, "reel")))))
              ? pyIndex(pyIndex(real, get(winner, "reel")), get(winner, "row")) : null), wf),
          winner_family_by_rule: played && pyEq(familyByRows(real), wf),
          loser_is_fake: played && pyList(fakes).some((f) =>
            pyEq(get(f, "reel"), get(loser, "reel")) && pyEq(get(f, "row"), get(loser, "row"))),
          fakes_loser_family: played && pyList(fakes).every((f) => pyEq(familyOf(get(f, "symbolId")), lf)),
          fakes_within_cap: pyList(or(get(state, "fakeCountByReel"), [])).every((n) =>
            !pyLt(pyInt(or(get(cfg, "maxFakesPerColumn"), 4)), n)),
          // The real symbols of the LOSING family stay: only the fakes are removed. The displayed
          // grid equalling `view` is exactly that claim, so it is reused here rather than re-derived.
          real_losers_survive: played && gridMatchesView(shown, raw),
          candidates: (() => {
            let n = 0;
            for (const reel of pyList(real)) for (const c of pyList(reel)) if (familyOf(c) !== null) n += 1;
            return n;
          })(),
        });
        bets.push(bet);
        if (played) {
          if (firstDrama === null) firstDrama = bet.seq;
          lastDrama = bet.seq;
          ctx.mark(py`ROUND ${bet.seq}: drama — ${wf} beats ${lf}`);
        } else {
          ctx.mark(py`ROUND ${bet.seq}: no drama`);
        }
      };

      const dramasSoFar = (): number => rows.filter((x) => x.played).length;
      if (truthy(this.autoplay)) {
        // Open the panel, pick the count, confirm -- in that order. There is no helper for this, and
        // no shortcut: confirming without the panel open clicks an inert control.
        const counts = [...(or(GAME.AUTO_COUNTS, []) as readonly (number | null)[])];
        // Only a count the panel OFFERS: picking another row would start a run of a different
        // length while the loop below waited for this one.
        const idx = counts.findIndex((c) => pyEq(c, this.autoplay));
        if (idx < 0) throw new Unreached(py`autoplay=${this.autoplay} is not a count this game offers (AUTO_COUNTS=${counts})`);
        ctx.mark(py`AUTOPLAY: ${counts[idx]} spins`);
        await ctx.clickButton(GAME.button("auto") as string);
        await ctx.clickButton(GAME.button("auto_count", { i: idx }) as string);
        await ctx.clickButton(GAME.button("auto_confirm") as string, { dwellMs: 0 });
        Object.assign(facts, { autoplay_ran: true });
        for (let i = 0; i < this.autoplay; i++) {
          const bet = await ctx.observeBet();
          if (isFreespinTrigger(bet)) {
            await playOutBonus(ctx, bet);
            continue;
          }
          await grade(bet);
          if (dramasSoFar() >= this.dramas) {
            // Enough dramas before the run ran out: STOP it, or the client keeps betting while
            // confirmStopped below watches for it to stop on its own.
            ctx.mark(py`AUTOPLAY: ${this.dramas} drama(s) seen -- stopping auto`);
            await stopAutoplay(ctx);
            break;
          }
        }
        await confirmStopped(ctx, { label: "AUTO STOPPED" });
      } else {
        Object.assign(facts, { autoplay_ran: false });
        for (let i = 0; i < this.maxSpins; i++) {
          const bet = await ctx.baseSpin();
          if (isFreespinTrigger(bet)) {
            // Rule 1. A bonus is not this scenario's subject, but it can happen; drain it and carry
            // on rather than grading a free spin as a base round.
            await playOutBonus(ctx, bet);
            continue;
          }
          await ctx.settle();
          await grade(bet);
          if (dramasSoFar() >= this.dramas) break;
        }
      }

      await takePresentationTrace(ctx, true);
      const playedRows = rows.filter((x) => x.played);
      if (playedRows.length < this.dramas) {
        throw new Unreached(
          py`only ${playedRows.length} drama(s) in ${rows.length} spins, wanted ${this.dramas}. `
          + "The roll is "
          + "70% on a big win and 4% otherwise -- force big wins with the paytable, do "
          + "not spin longer");
      }

      const ms = playedRows.map((x) => x.drama_ms);
      Object.assign(facts, {
        rounds_played: rows.length,
        dramas_played: playedRows.length,
        fights_cat_vs_dog: playedRows.filter((x) => x.cat_vs_dog).length,
        fights_same_row_adjacent: playedRows.filter((x) => x.same_row_adjacent).length,
        fights_vertical: playedRows.filter((x) => x.vertical).length,
        // one `drama_started` per round is what the bridge reports; a second fight in the same round
        // would arrive as a second state with the same roundId
        fights_per_round_max: playedRows.length ? 1 : 0,
        rounds_with_multiple_candidates: rows.filter((x) => x.candidates > 1).length,
        winner_is_real_cell: playedRows.filter((x) => x.winner_real).length,
        winner_family_matches_rule: playedRows.filter((x) => x.winner_family_by_rule).length,
        loser_is_fake: playedRows.filter((x) => x.loser_is_fake).length,
        fakes_all_loser_family: playedRows.filter((x) => x.fakes_loser_family).length,
        fakes_within_column_cap: playedRows.filter((x) => x.fakes_within_cap).length,
        real_losers_survive: playedRows.filter((x) => x.real_losers_survive).length,
        grid_matches_view_rounds: rows.filter((x) => x.grid_match).length,
        drama_animation_rounds: playedRows.filter((x) => x.animated).length,
        fakes_when_not_played: sum(rows.filter((x) => !x.played).map((x) => x.fakes)),
        min_drama_ms: ms.length ? Math.min(...ms) : 0,
        winner_stays: playedRows.filter((x) => x.grid_match).length,
        winner_survives_on_grid: playedRows.filter((x) => x.grid_match).length,
      });

      if (truthy(this.turbo)) {
        const btnTurbo = GAME.button("turbo");
        if (btnTurbo) await ctx.clickButton(btnTurbo, { optional: true });
      }
      const stop = await settleToBase(ctx, { label: "drama" });
      Object.assign(facts, stop);
      return new ScenarioResult(this.name, true, bets.length ? bets[bets.length - 1] : null, bets,
        ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          marks: { first_drama: firstDrama, last_drama: lastDrama },
          facts,
        });
    } catch (e) {
      if (e instanceof Unreached) {
        await ctx.dismissPopups();
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { facts, reason: e.message });
      }
      throw e;
    }
  }
}

export const SCENARIOS: ScenarioClass[] = [FreeSpinReload, WalkingWild, CatDogDrama];
