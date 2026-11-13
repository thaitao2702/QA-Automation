/**
 * The runner's BO glue (port of scripts/run_scenario.py `bo_setup` … `BO_SETTLE_S`): push a run's
 * `paytable:` block to the back office before the game loads, read the live base bet a max-win cap
 * is computed from, and reset the agent's paytable afterwards.
 */
import type { Page } from "playwright";
import { pyEval, PyEvalError } from "../../engine/expr.js";
import { evaluate } from "../../engine/page.js";
import { get, isDict, or, py, PyAttributeError, pyFloat, pyFloatRepr, pyRepr, pyRound, PyRuntimeError, pyTypeName, tryFloat, truthy } from "../../py.js";
import { excStr, pyFloatStr } from "./compat.js";

/** What `bo_setup` / `bo_reset` / `_resolve_max_payout` use of a BoHelper (a fake in tests). */
export interface BoForSetup {
  paytableCode?: string | null;
  referenceBet?: unknown;
  resolvePaytable(opts: { ref?: string | null; inline?: unknown }): unknown;
  ensurePaytable(code: string, opts: { paytableContent?: unknown }): Promise<unknown>;
  selectAgentPaytable(code: string, opts: { maxPayout?: unknown }): Promise<unknown>;
  updatePaytable(code: string, content: unknown): Promise<unknown>;
  resetAgentPaytable(): Promise<unknown>;
}

/** `pt.get(k)` — a `paytable:` value that is not a mapping raised AttributeError in Python. */
function ptGet(pt: unknown, key: string): any {
  if (!isDict(pt)) throw new PyAttributeError(`'${pyTypeName(pt)}' object has no attribute 'get'`);
  return get(pt, key);
}

/**
 * BO paytable SETUP for a run: ensure the automation paytable exists, point this agent's game at
 * it, and (if content is given) rewrite it to force a determined layout. `pt` is the run's
 * `paytable:` value — a fixture-ref string, or a dict {code?, ref?, inline?, max_win_x?,
 * max_payout?}. Returns the code. Raises if BO isn't configured (so the run is skipped rather than
 * silently un-forced).
 *
 * MAX WIN. `max_win_x: 10` caps the agent's payout at 10x the bet -- the LIVE base stake the caller
 * read off the open game and passed in as `bet`, falling back to `bo.referenceBet` when there was
 * no game to read. `max_win_x` also takes an EXPRESSION over that bet ("10 * bet"), and
 * `max_payout: <amount>` sets the cap directly. This is what makes a forced bonus END: an
 * easy-trigger paytable can award free spins that retrigger for hundreds of rounds, and the server
 * truncates at the cap and sets `hasReachedMaxWin` — which `isTerminal` already accepts. Without a
 * cap those runs are bounded only by the scenario's spin budget. The teardown clears the override,
 * so the cap never leaks into the next run.
 */
export async function boSetup(bo: BoForSetup | null, game: string, pt: unknown, bet: unknown = null): Promise<string> {
  if (typeof pt === "string") pt = { ref: pt };
  if (bo === null || bo === undefined) {
    throw new PyRuntimeError("paytable requested but BO not configured -- set the bo: block in "
      + `games/${game}/env.config.*.yml (or BO_* env vars)`);
  }
  // per-run `code:` > the game's bo.paytable_code > the historical default
  const code = or(or(ptGet(pt, "code"), bo.paytableCode ?? null), "AutomationTest") as string;
  const ref = ptGet(pt, "ref");
  const inline = ptGet(pt, "inline");
  const content = truthy(ref) || inline !== null ? bo.resolvePaytable({ ref, inline }) : null;
  const maxPayout = resolveMaxPayout(bo, game, pt, bet);
  await bo.ensurePaytable(code, { paytableContent: content });     // idempotent create (clones default if no content)
  await bo.selectAgentPaytable(code, { maxPayout });                // this agent now plays `code`
  if (content !== null) {
    await bo.updatePaytable(code, content);                         // force the exact forced layout
  }
  console.log(py`  paytable[${code}] set` + (truthy(ref) ? py` (ref=${ref})` : "")
    + (maxPayout !== null ? `, max win ${pyFloatStr(maxPayout)}` : ""));
  return code;
}

/**
 * Does this run's `paytable:` block need a BET before its cap can be resolved? True only for
 * `max_win_x` with no absolute `max_payout` overriding it.
 *
 * The runner asks this BEFORE touching BO, because a bet-derived cap has to be resolved against the
 * bet the game is really sitting at — which means opening the game to read it first. An absolute
 * `max_payout` needs nothing, and neither does a run with no cap, so those keep the cheap path: push
 * the paytable, load once, go.
 */
export function maxWinNeedsBet(pt: unknown): boolean {
  if (!isDict(pt)) return false;
  return get(pt, "max_payout") === null && get(pt, "max_win_x") !== null;
}

// The names pyEval knows that a `max_win_x` expression must NOT see: Python evaluated it with no
// builtins and a four-name namespace (`bet`, `min`, `max`, `round`).
const BLOCKED_NAMES = new Set(["abs", "len", "sum", "int", "float", "sorted", "all", "any", "set"]);
const BLOCKED_PREFIX = "__bo_blocked__";

/** A float as a Python literal pyEval reads back as the same float (never as an int). */
function floatLiteral(x: number): string {
  if (Number.isNaN(x)) return "(1e999-1e999)";
  if (x === Infinity) return "(1e999)";
  if (x === -Infinity) return "(-1e999)";
  const s = pyFloatRepr(x);
  return `(${/[.e]/.test(s) ? s : `${s}.0`})`;
}

/**
 * `eval(x, {"__builtins__": {}}, {"bet": bet, "min": min, "max": max, "round": round})`.
 *
 * `bet` is bound by substituting it, as a float literal, into the source pyEval reads; the
 * builtins pyEval has beyond min/max/round are renamed so they raise Python's NameError when (and
 * only when) evaluation reaches them. Tokens are split the way pyEval splits them, so a `bet` inside
 * a string literal is left alone.
 */
export function evalMaxWinX(src: string, bet: number): { value: unknown; repr: string } {
  let out = "";
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    const num = /^(?:\d(?:_?\d)*)?(?:\.\d(?:_?\d)*|\.)?(?:[eE][+-]?\d(?:_?\d)*)?/.exec(src.slice(i));
    if (num && num[0] && /\d/.test(num[0]) && (/\d/.test(ch) || (ch === "." && /\d/.test(src[i + 1] ?? "")))) {
      out += num[0];
      i += num[0].length;
      // `2bet` is "invalid decimal literal" to Python: hand pyEval the name untouched so it says so
      const glued = /^[A-Za-z_][A-Za-z_0-9]*/.exec(src.slice(i));
      if (glued) {
        out += glued[0];
        i += glued[0].length;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      let j = i + 1;
      while (j < src.length && src[j] !== ch) j += src[j] === "\\" && j + 1 < src.length ? 2 : 1;
      out += src.slice(i, j + 1);
      i = j + 1;
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      const name = /^[A-Za-z_][A-Za-z_0-9]*/.exec(src.slice(i))![0];
      if (name === "bet") out += floatLiteral(bet);
      else if (BLOCKED_NAMES.has(name)) out += BLOCKED_PREFIX + name;
      else out += name;
      i += name.length;
      continue;
    }
    out += ch;
    i++;
  }
  try {
    const r = pyEval(out);
    return { value: r.value, repr: r.repr };
  } catch (e) {
    if (e instanceof PyEvalError && e.message.includes(BLOCKED_PREFIX)) {
      throw new PyEvalError(e.kind, e.message.split(BLOCKED_PREFIX).join(""));
    }
    throw e;
  }
}

/**
 * The run's max-win cap as an absolute amount, or null to leave the agent's default alone.
 *
 * `max_payout` wins if both are given. `max_win_x` is a MULTIPLE OF THE BET and takes two forms:
 *
 *     max_win_x: 10                  ->  10 x bet
 *     max_win_x: "10 * bet"          ->  the same thing, written out
 *     max_win_x: "max(50, 10 * bet)" ->  a floor under the multiple
 *
 * The expression form is the "function of the current bet" a bare multiplier could not express. It
 * is evaluated with NO builtins and a four-name namespace (`bet` plus `min`/`max`/`round`), so a
 * suite cannot smuggle arbitrary code into the runner through a config file.
 *
 * WHICH BET. `bet` is the live BASE stake the caller read off the open game and passed in — the
 * plain stake, never the ante-inflated one, so a run that switches Ante Bet on is capped against the
 * bet the player started from and its cap matches the equivalent non-ante run. That needs no
 * arithmetic: the bet display already shows the base and does not move when ante is toggled (see
 * `BASE_BET_JS`). `bo.referenceBet` from the game's env.config is the fallback for when there was
 * no game to read, or the bet came back unreadable. Raising when neither is available is
 * deliberate: a silently-unresolved multiplier would leave the bonus uncapped and the run would
 * simply take a very long time instead of failing.
 */
export function resolveMaxPayout(bo: { referenceBet?: unknown } | null, game: string, pt: unknown, bet: unknown = null): number | null {
  if (ptGet(pt, "max_payout") !== null) return pyFloat(ptGet(pt, "max_payout"));
  const x = ptGet(pt, "max_win_x");
  if (x === null) return null;
  if (bet === null || bet === undefined) bet = bo?.referenceBet ?? null;
  if (!truthy(bet)) {
    throw new PyRuntimeError(
      `paytable.max_win_x=${pyRepr(x)} needs a bet to multiply. The live bet could not be read `
      + `from the game, and games/${game}/env.config.*.yml has no \`reference_bet:\` under its `
      + "bo: block to fall back on. Add the bet the scenario plays at (the game's opening "
      + "stake), or give an absolute `max_payout:`.");
  }
  const betF = pyFloat(bet);
  let value: unknown;
  if (typeof x === "string") {
    let r: { value: unknown; repr: string };
    try {
      r = evalMaxWinX(x, betF);
    } catch (e) {
      throw new PyRuntimeError(
        `paytable.max_win_x=${pyRepr(x)} is not an expression this runner can evaluate (${excStr(e)}). `
        + "Use a number, or an expression over `bet` such as '10 * bet'.", { cause: e });
    }
    if (typeof r.value === "boolean" || typeof r.value !== "number") {
      throw new PyRuntimeError(`paytable.max_win_x=${pyRepr(x)} evaluated to ${r.repr}, which is not a number.`);
    }
    value = r.value;
  } else {
    value = pyFloat(x) * betF;
  }
  return pyRound(pyFloat(value), 2);
}

// The live BASE stake, read off an OPEN game.
//
// `getSnapshot().betAmount` IS ALREADY THE BASE BET -- it does not move when Ante Bet is switched
// on. Measured live 2026-09-04: with ante ON the snapshot read 4.00 while the server deducted
// `stake: 12.00` per spin (balance 12080.79 -> 12068.79 -> 12056.79), i.e. betAmount == baseStake
// and the ante multiplier lives only in what is charged. This code first divided betAmount by the
// ante multiplier to "recover" the base and produced 1.33 from a 4.00 bet, capping the run at a
// third of what the suite asked for. Do not reintroduce that: the bet display is the base, and
// `bet` in a `max_win_x` expression is the base, so the two already agree.
export const BASE_BET_JS = `() => {
    const b = window.__gameTestBridge;
    if (!b || !b.getSnapshot) return null;
    const s = b.getSnapshot() || {};
    const shown = Number(s.betAmount);
    return (isFinite(shown) && shown > 0) ? shown : null; }`;

/** The game's current BASE bet, or null when it cannot be read. Never raises: an unreadable bet is
 * a reason to fall back on `referenceBet`, not to fail the run. */
export async function readBaseBet(page: Page): Promise<number | null> {
  let v: unknown;
  try {
    v = await evaluate(page, BASE_BET_JS);
  } catch {
    return null;
  }
  const f = tryFloat(v);
  if (f === null) return null;
  return f > 0 ? f : null;
}

/** BO paytable TEARDOWN: reset the agent's game paytable to the game default (best-effort).
 * Returns true when the reset went through. */
export async function boReset(bo: Pick<BoForSetup, "resetAgentPaytable"> | null, label = ""): Promise<boolean> {
  if (bo === null || bo === undefined) return false;
  const after = label ? ` after ${label}` : "";
  try {
    await bo.resetAgentPaytable();
    console.log(`  paytable reset${after}`);
    return true;
  } catch (e) {
    console.log(`  ! paytable reset failed${after}: ${excStr(e)}`);
    return false;
  }
}

/** Seconds for a BO change to reach the game server. BO answers 200 when IT has stored the change;
 * the game server gets it later over Kafka (Rule 20), the same delay the denomination swap waits. */
export const BO_SETTLE_S = 5.0;
