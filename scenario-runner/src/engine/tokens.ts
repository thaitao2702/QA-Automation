/**
 * Binding a suite to a result: `{res.payout}` / `{facts.x}` tokens, `video: at:` windows, `each:`
 * clips, and the inline `stream:` predicates (port of the binding block of scenario_engine.py).
 */
import { get, isDict, isList, py, pyFloat, pyLt, pyRepr, pyStr } from "../py.js";
import { Bet } from "./bet.js";
import { pyEval } from "./expr.js";
import { INVARIANTS, type Verdict } from "./invariants.js";
import type { ScenarioResult, Window } from "./result.js";

/** Names may contain DIGITS after the first character (`{facts.p1_ways}`). They could not until
 * 2026-09-05, and a predicate then evaluated `{facts.p1_ways}` as a set literal and died. */
const TOKEN = /\{([a-zA-Z_][a-zA-Z_0-9]*)(?:\[(\d+)\])?\.([a-zA-Z_][a-zA-Z_0-9]*)\}/g;

/** The Bet attributes a token may fall back to (Python's `getattr(bet, field)`). */
const BET_FIELDS = new Set(["seq", "game_state", "free_spins", "total_free_spin", "won_free_spins", "stake",
  "base_stake", "payout", "balance", "has_max_win", "cascades", "round_id", "raw", "phase", "max_win"]);

function indexError(): Error {
  const e = new Error("list index out of range");
  e.name = "IndexError";
  return e;
}

function bindingCtx(result: ScenarioResult): Record<string, unknown> {
  const ctx: Record<string, unknown> = { res: result.res, rounds: result.rounds };
  if (result.rounds.length) {
    ctx.trigger = result.rounds[0];
    ctx.terminal = result.rounds[result.rounds.length - 1];
  }
  return ctx;
}

function seqWindow(result: ScenarioResult, seq: unknown): Window | null {
  if (seq === null || seq === undefined) return result.video_window;
  // Python: `0 <= seq < len(round_windows)` — raises on a non-number, like Python's comparison.
  if (!pyLt(seq, 0) && pyLt(seq, result.round_windows.length)) return result.round_windows[Number(seq)];
  return result.video_window;
}

/**
 * Resolve a `video: at:` target to a [start_s, end_s] sub-clip. `at` may be null/'all' (whole
 * scenario); 'trigger'/'terminal'/'res'; 'rounds[i]'; a scenario mark; or a 2-element SPAN
 * (`['win', 'reset']`) from the first target's start to the second's end. Falls back to the
 * whole-scenario window. A mark `{"t": seconds}` is a MOMENT with no round of its own: it resolves
 * from that moment to the end of the scenario, so span it to trim the end.
 */
export function resolveWindow(result: ScenarioResult, at: unknown): Window | null {
  if (isList(at) && at.length === 2) {
    const w0 = resolveWindow(result, at[0]);
    const w1 = resolveWindow(result, at[1]);
    return w0 && w1 ? [w0[0], w1[1]] : result.video_window;
  }
  if (at === null || at === undefined || at === "" || at === "all") return result.video_window;
  let seq: unknown = null;
  if (at === "trigger" && result.rounds.length) seq = result.rounds[0].seq;
  else if (at === "terminal" && result.rounds.length) seq = result.rounds[result.rounds.length - 1].seq;
  else if (at === "res" && result.res !== null) seq = result.res.seq;
  else if (typeof at === "string" && Object.prototype.hasOwnProperty.call(result.marks, at)) {
    let mark = result.marks[at];
    if (isDict(mark) && "t" in mark) return result.video_window ? [mark.t as number, result.video_window[1]] : null;
    if (isList(mark)) mark = mark.length ? mark[0] : null;   // a LIST mark (for `each:`) -> its first round
    seq = mark;
  } else {
    const m = /^rounds\[(\d+)\]$/.exec(pyStr(at));
    if (m && result.rounds.length) {
      const b = result.rounds[Number(m[1])];
      if (!b) throw indexError();
      seq = b.seq;
    }
  }
  return seq !== null ? seqWindow(result, seq) : result.video_window;
}

/**
 * Resolve a `video: each:` target to one sub-clip PER ROUND: [{seq, window}, ...]. `each` names a
 * LIST mark — every round where something happened — so each round is graded on its own instead of
 * one long clip asked about all of them at once. Unknown mark or no rounds -> [].
 */
export function resolveEach(result: ScenarioResult, each: unknown): Array<{ seq: number; window: Window }> {
  const mark = typeof each === "string" ? (result.marks[each] ?? null) : null;
  if (mark === null) return [];
  const seqs = isList(mark) ? mark : [mark];
  const out: Array<{ seq: number; window: Window }> = [];
  for (const seq of seqs) {
    if (typeof seq === "number" && Number.isInteger(seq) && seq >= 0 && seq < result.round_windows.length) {
      out.push({ seq, window: result.round_windows[seq] });
    }
  }
  return out;
}

/**
 * Render `val` as a PYTHON LITERAL for an inline `stream:` predicate. Substituting the bare value
 * works for numbers and breaks for everything else (`NORMAL != TURBO` -> two undefined names).
 * Numeric-looking strings stay unquoted so arithmetic still works (`{facts.bet_b} / 20`); a
 * container renders as a container (`{facts.illegal_reels} == []`).
 */
export function exprLiteral(val: unknown): string {
  if (val === null || val === undefined) return "None";
  if (typeof val === "boolean") return val ? "True" : "False";
  if (typeof val === "number") return pyRepr(val);
  if (isList(val) || isDict(val) || val instanceof Set) return pyRepr(val);
  const s = pyStr(val);
  try {
    pyFloat(s);                    // "0.6" -> stays a number, so comparisons and maths work
    return s;
  } catch {
    // not numeric
  }
  if (s === "True" || s === "False" || s === "None") return s;
  return pyRepr(s);                // NORMAL -> 'NORMAL'
}

/** Python's `"" in "'\""` is True — so a token at the very start or end of the text counts as
 * quoted on that side. Kept, because it decides how such a token renders. */
const isQuoteOrEdge = (ch: string): boolean => ch === "" || ch === "'" || ch === '"';

/**
 * Substitute `{res.payout}` etc. with the raw response value. `forExpr` renders each value as a
 * Python literal instead of its plain text, for the predicates that get evaluated; it is OFF for
 * prompt text, where a quoted `'NORMAL'` would be shown to the AI verbatim. A token the YAML already
 * wrapped in quotes is left bare even under `forExpr`, so `'{facts.speed_after_on}' != ...` works.
 */
export function resolveTokens(text: string, result: ScenarioResult, forExpr = false): string {
  const ctx = bindingCtx(result);
  return text.replace(TOKEN, (whole: string, base: string, idx: string | undefined, field: string, offset: number) => {
    const render = (val: unknown): string => {
      if (!forExpr) return val === null || val === undefined ? "null" : pyStr(val);
      const before = offset > 0 ? text[offset - 1] : "";
      const end = offset + whole.length;
      const after = end < text.length ? text[end] : "";
      if (isQuoteOrEdge(before) && isQuoteOrEdge(after)) return val === null || val === undefined ? "null" : pyStr(val);
      return exprLiteral(val);
    };
    if (base === "facts") return render(get(result.facts, field));   // scenario-exposed values
    let obj = Object.prototype.hasOwnProperty.call(ctx, base) ? ctx[base] : null;
    if (obj === null || obj === undefined) return whole;
    if (idx !== undefined && isList(obj)) {
      const item = obj[Number(idx)];
      if (item === undefined) throw indexError();
      obj = item;
    }
    let val: unknown = null;
    if (obj instanceof Bet) {
      val = get(obj.raw, field);                       // raw server field first (payout, freeSpins)
      if (val === null && BET_FIELDS.has(field)) {
        val = (obj as unknown as Record<string, unknown>)[field] ?? null;   // computed field (cascades, phase)
      }
    }
    return render(val);
  });
}

/** A one-off `stream:` predicate like '{terminal.freeSpins} == 0'. */
export function evalInlineStream(expr: string, result: ScenarioResult): Verdict {
  const interpolated = resolveTokens(expr, result, true);
  let isOk: boolean;
  try {
    isOk = pyEval(interpolated).truth;
  } catch (e) {
    // Python caught every Exception here, so every failure to evaluate is an answer, not a crash.
    return { valid: false, violations: [py`could not evaluate \`${expr}\` -> \`${interpolated}\`: ${(e as Error).message}`] };
  }
  return { valid: isOk, violations: isOk ? [] : [py`\`${expr}\` -> \`${interpolated}\` is false`] };
}

/** `spec` is either a registered invariant name (optionally `name(args)`) or an inline predicate.
 * Digits are allowed after the first character: `p1_reels_only` is a real invariant name. */
export function runStreamAssert(spec: unknown, result: ScenarioResult): Verdict {
  const text = pyStr(spec);
  const m = /^([a-zA-Z_][a-zA-Z_0-9]*)(?:\((.*)\))?$/.exec(text.trim());
  if (m && Object.prototype.hasOwnProperty.call(INVARIANTS, m[1])) return INVARIANTS[m[1]](result.rounds);
  return evalInlineStream(text, result);
}
