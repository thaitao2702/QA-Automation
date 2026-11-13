/**
 * Mechanism classes: what a FAMILY of slots shares on top of SlotGame (port of the mechanism half
 * of scripts/game_profile.py).
 */
import {
  type Dict, get, has, isDict, isList, isNumber, or, pyInt, pyList, pyRound, pyStr, sum, tryFloat, truthy,
} from "../py.js";
import { SlotGame } from "./slot-game.js";

// =====================================================================================
// Mechanism: cluster-pays with cascading steps
// =====================================================================================

/** Wins clear, symbols fall in, the round continues until a step pays nothing. */
export class CascadeGame extends SlotGame {
  override mechanism = "cascade";

  F_STEP_PAYOUT = "payout";
  F_STEP_WINNINGS = "winnings";
  F_STEP_GRID = "views";
  F_STEP_MULTIPLIER = "multiplierLevel";

  /** How many cascade steps PAID — the unit the history detail pages by (one grid per step, so a
   * step paying several clusters still counts once) and the unit the multiplier ladder climbs. The
   * chain ends with a non-winning step, so a winning round has `len(steps) - 1`. */
  override clusterWins(steps: unknown[], payout = 0): number {
    const typed = pyList(or(steps, [])).filter(isDict);
    if (!typed.length) return 0;
    if (typed.some((s) => has(s, this.F_STEP_PAYOUT))) {
      let n = 0;
      for (const s of typed) {
        const f = tryFloat(or(get(s, this.F_STEP_PAYOUT), 0));
        if (f !== null && f > 0) n += 1;
      }
      return n;
    }
    if (typed.some((s) => isList(get(s, this.F_STEP_WINNINGS)))) {
      return typed.filter((s) => truthy(get(s, this.F_STEP_WINNINGS))).length;
    }
    return payout > 0 ? Math.max(0, typed.length - 1) : 0;
  }

  /** Total winning CLUSTERS (every `winnings[]` entry). Exceeds clusterWins whenever one step paid
   * several clusters at once. Diagnostic only. */
  override winClusters(steps: unknown[]): number {
    let n = 0;
    for (const s of pyList(or(steps, []))) {
      if (isDict(s) && isList(get(s, this.F_STEP_WINNINGS))) n += (s[this.F_STEP_WINNINGS] as unknown[]).length;
    }
    return n;
  }

  /** Per-step multiplier as the server reported it. ⚠ a LADDER from game config (Mystic base game
   * runs 1,2,3,5,8,10) — never assume `+1` per cascade. */
  multiplierLadder(steps: unknown[]): unknown[] {
    return pyList(or(steps, [])).filter((s): s is Dict => isDict(s) && has(s, this.F_STEP_MULTIPLIER))
      .map((s) => get(s, this.F_STEP_MULTIPLIER));
  }
}

// =====================================================================================
// Mechanism: Hold & Spin (respins that hold symbols)
// =====================================================================================

/** A set of grid cells, compared by value. Python used a set of (row, col) tuples. */
export class CellSet {
  private readonly cells = new Map<string, [number, number]>();

  constructor(cells: Iterable<[number, number]> = []) {
    for (const c of cells) this.add(c);
  }

  add([r, c]: [number, number]): void {
    this.cells.set(`${r},${c}`, [r, c]);
  }

  has([r, c]: [number, number]): boolean {
    return this.cells.has(`${r},${c}`);
  }

  get size(): number {
    return this.cells.size;
  }

  values(): [number, number][] {
    return [...this.cells.values()];
  }

  /** `self <= other` */
  isSubsetOf(other: CellSet): boolean {
    return this.values().every((c) => other.has(c));
  }

  /** `self - other` */
  minus(other: CellSet): CellSet {
    return new CellSet(this.values().filter((c) => !other.has(c)));
  }

  /** Sorted as Python sorts the tuples: by row, then column. */
  sorted(): [number, number][] {
    return this.values().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  }
}

/**
 * N special symbols trigger a respin feature: the triggering symbols are HELD, only the empty
 * positions respin, a new symbol RESETS the respin counter, and the feature ends when the counter
 * hits zero or the grid fills. All checkable from the response, so this mechanism's cases are HARD
 * `stream:` asserts, not AI reads.
 */
export class HoldSpinGame extends SlotGame {
  override mechanism = "hold-spin";

  TRIGGER_COUNT = 3;        // special symbols needed to trigger
  RESPINS_ON_TRIGGER = 3;   // respins awarded, and the value the counter RESETS to
  GRID_SLOTS = 9;           // a full grid ends the feature immediately

  F_COIN_POSITIONS = "coinPositions";   // [{row, col, coinValue, type, multiplier?}]
  F_COIN_COUNT = "coinCount";
  F_COIN_VALUE = "coinValue";
  F_COIN_TYPE = "type";

  override HAS_FREE_SPINS = true;

  /** The held positions — the set that must only ever GROW during the feature. */
  coinPositions(raw: Dict): CellSet {
    const out = new CellSet();
    for (const c of (or(get(raw, this.F_COIN_POSITIONS), []) as unknown[])) {
      if (isDict(c) && get(c, "row") !== null && get(c, "col") !== null) out.add([pyInt(c.row), pyInt(c.col)]);
    }
    return out;
  }

  /** How many special symbols are on the grid: the server's own count, else the position list. */
  coinCount(raw: Dict): number {
    const n = get(raw, this.F_COIN_COUNT);
    if (isNumber(n)) return pyInt(n);
    return this.coinPositions(raw).size;
  }

  /** Summed face value of the held symbols — the feature's minimum payout so far. */
  coinTotal(raw: Dict): number {
    let total = 0;
    for (const c of (or(get(raw, this.F_COIN_POSITIONS), []) as unknown[])) {
      if (isDict(c)) {
        const f = tryFloat(or(get(c, this.F_COIN_VALUE), 0));
        if (f !== null) total += f;
      }
    }
    return pyRound(total, 6);
  }

  gridFull(raw: Dict): boolean {
    return this.coinCount(raw) >= this.GRID_SLOTS;
  }

  /** Did THIS round award the feature? State and counters first; the symbol count is only a
   * FALLBACK for a build that reports neither — the round which ENDS the feature carries a full grid
   * of specials while already reporting base state and no respins. */
  override isTrigger(raw: Dict): boolean {
    const [rem, awarded] = this.freeSpins(raw);
    if (awarded > 0 || rem > 0) return true;
    if (this.isBonusState(this.gameState(raw))) return true;
    if (has(raw, this.F_GAME_STATE) || (this.F_FS_CONTAINER && has(raw, this.F_FS_CONTAINER))) return false;
    return this.coinCount(raw) >= this.TRIGGER_COUNT;
  }
}

// =====================================================================================
// Mechanism: line / ways evaluation on a fixed grid
// =====================================================================================

export type WinTier = readonly [string, number];

/**
 * One grid per round, paid along fixed LINES — plus, in some games, a WAYS symbol that pays from
 * any reel. The readable unit is a per-reel symbol census: almost every rule a line game states is
 * a predicate over it, so it is checkable EXACTLY from the response.
 *
 * `grid()` returns null (never an empty grid) when the response carries no readable one: an empty
 * census would make every "symbol X is absent" rule pass vacuously.
 */
export class LineGame extends SlotGame {
  override mechanism = "line";

  REELS = 5;
  ROWS = 3;
  // PAYLINES / LINE_DIVISOR are declared on SlotGame; a line game defaults the divisor to 20.
  override LINE_DIVISOR: number | null = 20;

  F_STEP_GRID = "views";              // [{id, value: [symbol, ...]}] — one entry per REEL
  F_STEP_WINNINGS = "winnings";
  F_MULTIPLIER = "multiplier";        // the round's random multiplier

  SYM_WILD: string | number | null = "Wild";
  SYM_SCATTER: string | number | null = "FreeSpin";
  MULTIPLIER_VALUES: readonly number[] = [];   // the configured ladder, if the game has one
  WIN_TIERS: readonly WinTier[] = [];          // (name, lower bound as a multiple of bet), ascending

  /** Symbols compare loosely, across TYPES: a cell may be a numeric id (Chaos sends `11`) or a name
   * (`FreeSpin!`), so both sides fold to the same string. */
  static _norm(sym: unknown): string {
    return pyStr(sym).trim().replace(/!+$/, "").toLowerCase();
  }

  /** The round's grid as a list of REELS, each a list of symbol names top->bottom. null when the
   * response carries no readable grid. Never `[]`. */
  grid(raw: Dict): string[][] | null {
    for (const step of this.steps(raw) ?? []) {
      if (!isDict(step)) continue;
      const views = get(step, this.F_STEP_GRID);
      if (!isList(views) || !views.length) continue;
      const reels: string[][] = [];
      for (const col of views) {
        if (isDict(col) && isList(get(col, "value"))) reels.push((col.value as unknown[]).map(pyStr));
        else if (isList(col)) reels.push(col.map(pyStr));
      }
      if (reels.length) return reels;
    }
    return null;
  }

  /** How many `symbol` landed on each reel (index 0 = reel 1). null when the grid is unreadable OR
   * `symbol` is null — counting an unknown symbol would return all-zeros and pass vacuously. */
  reelCounts(raw: Dict, symbol: unknown): number[] | null {
    if (symbol === null || symbol === undefined) return null;
    const reels = this.grid(raw);
    if (reels === null) return null;
    const want = LineGame._norm(symbol);
    return reels.map((reel) => reel.filter((s) => LineGame._norm(s) === want).length);
  }

  /** Total `symbol` on the grid, or null when the grid is unreadable. */
  symbolCount(raw: Dict, symbol: unknown): number | null {
    const counts = this.reelCounts(raw, symbol);
    return counts === null ? null : sum(counts);
  }

  /** 1-BASED reel numbers carrying `symbol` (reads like the design docs). null when unreadable. */
  reelsWith(raw: Dict, symbol: unknown): number[] | null {
    const counts = this.reelCounts(raw, symbol);
    return counts === null ? null : counts.flatMap((n, i) => (n > 0 ? [i + 1] : []));
  }

  /** The WAYS count for a symbol across specific reels: the product of its per-reel counts. Zero
   * when a required reel is missing it. null when unreadable. */
  ways(raw: Dict, symbol: unknown, reels: readonly number[]): number | null {
    const counts = this.reelCounts(raw, symbol);
    if (counts === null) return null;
    let total = 1;
    for (const r of reels) {
      if (r - 1 >= counts.length) return 0;
      total *= counts.at(r - 1) as number;
    }
    return total;
  }

  /** The round's random multiplier, or null when the response does not report one. */
  multiplier(raw: Dict): number | null {
    const v = get(raw, this.F_MULTIPLIER);
    return v === null ? null : tryFloat(v);
  }

  /** Win tier by payout/stake against this game's configured bands. */
  winTier(payout: number, stake: number): string {
    if (!this.WIN_TIERS.length || stake <= 0 || payout <= 0) return "LOSE";
    const x = payout / stake;
    let tier = this.WIN_TIERS[0][0];
    for (const [name, lo] of this.WIN_TIERS) if (x >= lo) tier = name;
    return tier;
  }
}

/**
 * One grid per round, paid by WAYS: every left-to-right run of a symbol on adjacent reels starting
 * at reel 1 pays, once per combination of the cells involved. It subclasses LineGame because the
 * readable unit is the same per-reel census. The line defaults are cleared: there are no paylines
 * and no per-line divisor, and the round carries ONE grid at the top level, so `steps()` returns the
 * round itself and `Bet.cascades` stays 1. `WAYS_TOTAL` is reported, never assumed.
 */
export class WaysGame extends LineGame {
  override mechanism = "ways";

  override PAYLINES: number | null = null;
  override LINE_DIVISOR: number | null = null;

  override ROWS = 4;
  WAYS_TOTAL: number | null = null;

  /** The round IS the step: the grid sits at the top level. */
  override steps(raw: Dict): unknown[] {
    return isDict(raw) ? [raw] : [];
  }

  /** The advertised ways count, from the geometry this profile declares. */
  waysTotal(): number | null {
    if (this.WAYS_TOTAL !== null) return this.WAYS_TOTAL;
    if (this.REELS && this.ROWS) return this.ROWS ** this.REELS;
    return null;
  }
}



