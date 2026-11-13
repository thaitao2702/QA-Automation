/**
 * One BET response, reduced to the fields the engine drives and asserts on. `.raw` keeps the
 * untouched server object, so a binding like `{res.payout}` reads the real contract field.
 *
 * The field names are snake_case on purpose: they are a DATA contract, not just code. Suites bind
 * them (`{trigger.base_stake}`), and `row()` writes them into events.jsonl / timeline.json.
 */
import type { CascadeGame, HoldSpinGame } from "../profile/mechanisms.js";
import type { ReplyError } from "../profile/slot-game.js";
import { type Dict, floatOr0, get, or, py, pyEq, r, truthy } from "../py.js";
import { GAME } from "./game.js";

/** The bridge handed over a server REFUSAL where a round was expected. It is not a round, so it must
 * not reach `conservation` or any other round check; the run errors and names the code. */
export class BetRejected extends Error {
  override name = "BetRejected";

  constructor(readonly err: ReplyError, seq: number) {
    super(py`server rejected the bet at stream seq ${seq}: code ${err.code} (${err.meaning}) status=${err.status} msg=${r(err.msg)}. A refusal is not a round, so it was not recorded as one.`);
  }
}

export type Phase = "base" | "trigger" | "freespin" | "auto" | "teardown" | string;

export class Bet {
  phase: Phase | null = null;   // set by DriveContext: "base"/"trigger" (paid) vs "freespin" (auto)

  constructor(
    public seq: number,
    public game_state: unknown,
    public free_spins: number,
    public total_free_spin: number,
    // Awarded BY THIS ROUND — 0 inside a bonus, where `total_free_spin` still reports the total.
    public won_free_spins: number,
    public stake: number,
    // What payouts are computed from. Equal to `stake` on a plain spin; NOT equal under Ante Bet or
    // a Premium Reel, on a bought bonus (the stake is the price), nor in free spins (stake 0).
    public base_stake: number,
    public payout: number,
    public balance: number,
    public has_max_win: boolean,
    public cascades: number,
    public round_id: unknown,
    public raw: Dict,
    // The max-win cap in force when this round was PARSED: the reply's `maxWin`, else the launch
    // settings. Frozen here because a later reload can change `maxPayout`.
    public max_win: number | null = null,
  ) {}

  /**
   * Reduce one raw response through the active PROFILE, so a game whose backend nests its counters
   * (Zeus: freeSpinInfo.freespins) still lands in the same shape. `unwrap()` first: some bridges
   * return the whole socket ENVELOPE. A REJECTED reply is refused with `BetRejected` instead of
   * becoming a stake-0 / payout-0 / balance-0 round that every check would grade.
   */
  static parse(raw: unknown, seq: number): Bet {
    const err = GAME.replyError(raw);
    if (err !== null) throw new BetRejected(err, seq);
    const rr = GAME.unwrap(raw);
    const [rem, awarded] = GAME.freeSpins(rr);
    return new Bet(
      seq,
      GAME.gameState(rr),
      rem,
      awarded,
      GAME.wonFreeSpins(rr),
      floatOr0(get(rr, GAME.F_STAKE)),
      GAME.baseStake(rr),
      floatOr0(get(rr, GAME.F_PAYOUT)),
      floatOr0(get(rr, GAME.F_BALANCE)),
      truthy(get(rr, GAME.F_MAX_WIN)),
      GAME.steps(rr).length,
      get(rr, GAME.F_ROUND_ID),
      rr,
      GAME.maxWinCap(rr),
    );
  }

  row(): Dict {
    return {
      seq: this.seq, phase: this.phase, game_state: this.game_state, free_spins: this.free_spins,
      total_free_spin: this.total_free_spin, won_free_spins: this.won_free_spins, stake: this.stake,
      base_stake: this.base_stake, payout: this.payout, balance: this.balance,
      has_max_win: this.has_max_win, cascades: this.cascades, round_id: this.round_id,
    };
  }
}

/** Is this the round that ENDS a bonus (or a plain base round)? The one definition of the rule.
 * Both halves are needed: `freeSpins == 0` alone misses a max-win truncation, which returns to base
 * with spins still on the counter; `gameState == base` alone misses a bonus whose counter emptied on
 * a round still reported in the bonus state. null counts as terminal: nothing to drain. */
export function isTerminal(b: Bet | null | undefined): boolean {
  return b === null || b === undefined || b.free_spins === 0 || pyEq(b.game_state, GAME.BASE_STATE);
}

/** How many cascade steps of this round PAID — the unit the history detail pages by. */
export function clusterWins(b: Bet): number {
  return GAME.clusterWins(or(get(or(b.raw, {}), GAME.F_STEPS), []) as unknown[], b.payout);
}

/** Total winning CLUSTERS across the round. Diagnostic. */
export function winClusters(b: Bet): number {
  return GAME.winClusters(or(get(or(b.raw, {}), GAME.F_STEPS), []) as unknown[]);
}

/** How many PAYING steps this round produced — the unit a history detail pages by. Cascade games
 * count winning cascade steps; every other mechanism reports one grid per round, so a paying round
 * is exactly one step. */
export function winSteps(b: Bet): number {
  if (GAME.mechanism === "cascade") return clusterWins(b);
  return b.payout > 0 ? 1 : 0;
}

/** True when a round AWARDED the bonus, keyed off the response via the profile. */
export function isFreespinTrigger(b: Bet): boolean {
  return GAME.isTrigger(or(b.raw, {}) as Dict);
}

/** Does THIS round BEGIN a Hold & Spin feature — including the instant win that skips it? A
 * round landing the full grid reports base state, no respins and 9 coins, and `isTrigger` answers
 * false to it (the round that ENDS a feature looks the same). But 9 coins on a BASE spin is the
 * documented instant win (ZZ-31). So: a trigger, OR a full grid — trigger first. */
export function entersHoldspin(b: Bet): boolean {
  if (isFreespinTrigger(b)) return true;
  const g = GAME as Partial<HoldSpinGame>;
  return typeof g.gridFull === "function" && g.gridFull.call(GAME, or(b.raw, {}) as Dict);
}

/** The per-step multiplier ladder, when the active profile has one (cascade games). */
export function multiplierLadderOf(steps: unknown[]): unknown[] | null {
  const g = GAME as Partial<CascadeGame>;
  return typeof g.multiplierLadder === "function" ? g.multiplierLadder.call(GAME, steps) : null;
}
