/**
 * Invariant library (Layer 2) — the `stream:` predicates a suite names: rounds -> {valid, violations}.
 * Exact and free: every one is computed from the server responses, never read off the screen.
 *
 * Port of the invariant block of scripts/scenario_engine.py. Violation messages keep the Python
 * wording (they reach result.json and the report). Profile reads go through `strict(GAME)`, so an
 * invariant pointed at a game that lacks what it needs RAISES, as the Python did, instead of
 * quietly answering on `undefined`.
 */
import { type Dict, get, has, isConversionError, isDict, isList, isNumber, or, py, pyEq, pyFloat, pyInt, pyList,
  pyLt, pyRepr, pyRound, pySorted, pyStr, PyText, PyTuple, r, strict, sum, tryFloat, truthy } from "../py.js";
import { Bet, entersHoldspin, isFreespinTrigger, isTerminal, multiplierLadderOf } from "./bet.js";
import { GAME } from "./game.js";

export interface Verdict {
  valid: boolean;
  violations: string[];
}

const ok = (v: string[]): Verdict => ({ valid: !v.length, violations: v });
const fail = (msg: string): Verdict => ({ valid: false, violations: [msg] });

/** The profile, with Python attribute semantics (see `strict`). */
function G(): any {
  return strict(GAME);
}

const rawOf = (b: Bet): Dict => (or(b.raw, {}) as Dict);

/** `balance == previous balance - stake + payout`, across the whole stream. A round that does not
 * REPORT a balance (zero, or negative — Dragon VIP's respins send `balance: -1`) is folded into the
 * next one that does: its stake and payout are carried, and the terminal round still has to account
 * for every one of them. */
export function conservation(rounds: Bet[]): Verdict {
  const v: string[] = [];
  let known: number | null = null;     // the last balance the server actually reported
  let owedStake = 0;
  let owedPayout = 0;                  // carried from rounds that reported none
  rounds.forEach((c, i) => {
    if (c.balance === null || c.balance <= 0) {
      owedStake += c.stake;
      owedPayout += c.payout;
      return;
    }
    if (known !== null) {
      const staked = owedStake + c.stake;
      const paid = owedPayout + c.payout;
      if (Math.abs(known - staked + paid - c.balance) > 1e-6) {
        const carried = owedStake || owedPayout
          ? py` (carrying ${owedStake} staked / ${owedPayout} paid from round(s) that reported no balance)` : "";
        v.push(py`round ${i}: balance ${c.balance} != ${known} - ${staked} + ${paid}` + carried);
      }
    }
    known = c.balance;
    owedStake = 0;
    owedPayout = 0;
  });
  return ok(v);
}

/** A free spin is an AUTO-played round (phase 'freespin'); the trigger is a paid base bet that also
 * carries gameState 2, so this keys off phase, not gameState. */
export function stakeZeroInFreespins(rounds: Bet[]): Verdict {
  return ok(rounds.flatMap((x, i) => (x.phase === "freespin" && x.stake !== 0
    ? [py`round ${i}: free-spin stake ${x.stake} != 0`] : [])));
}

/** freeSpins only decreases or resets upward (retrigger); never drifts arbitrarily. */
export function freespinsMonotonic(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const fs = rounds.filter((x) => x.phase === "freespin");
  for (let i = 1; i < fs.length; i++) {
    if (fs[i].free_spins > fs[i - 1].free_spins && fs[i].total_free_spin <= fs[i - 1].total_free_spin) {
      v.push(py`fs round ${i}: freeSpins rose ${fs[i - 1].free_spins}->${fs[i].free_spins} without a retrigger`);
    }
  }
  return ok(v);
}

export function terminalReached(rounds: Bet[]): Verdict {
  if (!rounds.length) return fail("no rounds");
  const last = rounds[rounds.length - 1];
  const isOk = isTerminal(last);
  return { valid: isOk, violations: isOk ? [] : [py`last round not terminal: fs=${last.free_spins} gs=${last.game_state}`] };
}

// ---------------------------------------------------------------------------
// Hold & Spin invariants (Zeus, ZZ-30..ZZ-38). The bridge removed its verifyHoldSpin* analysers —
// "the framework owns the logic" — so the rules live here, computed from the response stream.
// ---------------------------------------------------------------------------

/** The feature's rounds: the trigger plus every respin after it. An instant 9-coin win IS the whole
 * feature, and `isTrigger` answers false to it, hence `entersHoldspin`. */
function holdspinRounds(rounds: Bet[]): Bet[] {
  const out: Bet[] = [];
  let started = false;
  for (const b of rounds) {
    if (!started && entersHoldspin(b)) started = true;
    if (started) out.push(b);
  }
  return out;
}

function pairs<T>(xs: T[]): Array<[T, T]> {
  const out: Array<[T, T]> = [];
  for (let i = 1; i < xs.length; i++) out.push([xs[i - 1], xs[i]]);
  return out;
}

/** ZZ-32/33: a symbol, once landed, is HELD — the occupied set may only grow. */
export function holdspinCoinsHeld(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  for (const [prev, cur] of pairs(holdspinRounds(rounds))) {
    const was = g.coinPositions(rawOf(prev));
    const now = g.coinPositions(rawOf(cur));
    const lost = was.minus(now);
    if (lost.size) {
      const shown = lost.sorted().map((c: [number, number]) => new PyTuple(c));
      v.push(py`seq ${cur.seq}: held position(s) ${shown} vanished (${was.size} -> ${now.size} coins)`);
    }
  }
  return ok(v);
}

/** ZZ-34: landing a new symbol RESETS the respin counter to its full value. Checked only on rounds
 * that actually added one. */
export function holdspinResetOnNewCoin(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  const seq = holdspinRounds(rounds);
  const want = g.RESPINS_ON_TRIGGER;
  for (const [prev, cur] of pairs(seq)) {
    const gained = g.coinCount(rawOf(cur)) - g.coinCount(rawOf(prev));
    if (gained <= 0) continue;
    if (g.gridFull(rawOf(cur))) continue;          // a full grid ends the feature; no reset is due
    if (cur.free_spins !== want) {
      v.push(py`seq ${cur.seq}: +${gained} coin(s) but respins = ${cur.free_spins}, expected a reset to ${want}`);
    }
  }
  return ok(v);
}

/** ZZ-35: a respin that adds NO new symbol decrements the counter by exactly one. */
export function holdspinCountdown(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  for (const [prev, cur] of pairs(holdspinRounds(rounds))) {
    if (g.coinCount(rawOf(cur)) !== g.coinCount(rawOf(prev))) continue;
    if (cur.free_spins !== prev.free_spins - 1) {
      v.push(py`seq ${cur.seq}: no new coin, respins went ${prev.free_spins} -> ${cur.free_spins}, expected ${prev.free_spins - 1}`);
    }
  }
  return ok(v);
}

/** ZZ-36/37: the feature ends when the counter hits 0 OR the grid fills — and not before. */
export function holdspinEndsCorrectly(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  const seq = holdspinRounds(rounds);
  if (!seq.length) return fail("no hold-spin rounds in this stream");
  const last = seq[seq.length - 1];
  if (last.free_spins !== 0 && !g.gridFull(rawOf(last))) {
    v.push(py`seq ${last.seq}: feature ended with ${last.free_spins} respin(s) left and only ${g.coinCount(rawOf(last))}/${g.GRID_SLOTS} coins`);
  }
  for (const b of seq.slice(0, -1)) {
    if (g.gridFull(rawOf(b))) v.push(py`seq ${b.seq}: grid was full (${g.GRID_SLOTS} coins) but the feature continued`);
    if (b.free_spins === 0) v.push(py`seq ${b.seq}: respins hit 0 but the feature continued`);
  }
  return ok(v);
}

/** ZZ-30: landing the trigger count awards the full respin allowance. */
export function holdspinAward(rounds: Bet[]): Verdict {
  const g = G();
  const seq = holdspinRounds(rounds);
  if (!seq.length) return fail("no hold-spin trigger in this stream");
  const t0 = seq[0];
  const coins = g.coinCount(rawOf(t0));
  if (coins < g.TRIGGER_COUNT) {
    return fail(py`seq ${t0.seq}: triggered on ${coins} coin(s), fewer than the ${g.TRIGGER_COUNT} required`);
  }
  if (g.gridFull(rawOf(t0))) return ok([]);         // full grid skips the respins entirely
  const want = g.RESPINS_ON_TRIGGER;
  const got = t0.free_spins || t0.total_free_spin;
  return got === want ? ok([]) : fail(py`seq ${t0.seq}: ${coins} coins awarded ${got} respins, expected ${want}`);
}

/** The feature pays the summed face value of the coins it collected — the Coin Win Meter. Graded
 * against the terminal round's ACCUMULATED total; falls back to the terminal payout for a build with
 * no accumulator, and passes when face values are not reported at all. */
export function holdspinPayoutMatchesCoins(rounds: Bet[]): Verdict {
  const g = G();
  const seq = holdspinRounds(rounds);
  if (!seq.length) return fail("no hold-spin rounds in this stream");
  const last = seq[seq.length - 1];
  const coinsTotal = g.coinTotal(rawOf(last));
  if (coinsTotal <= 0) return ok([]);               // build does not report face values
  const acc = get(rawOf(last), "accumulatedWinAmount");
  const paid = isNumber(acc) ? pyFloat(acc) : last.payout;
  if (Math.abs(paid - coinsTotal) > 0.01) return fail(py`coins on the grid total ${coinsTotal} but the feature paid ${paid}`);
  return ok([]);
}

// ---------------------------------------------------------------------------
// Line-game invariants (Chaos Runner): predicates over the per-reel symbol census. ⚠ When the grid
// is unreadable the census helpers return null, and these report `cannot verify` as a VIOLATION
// rather than passing — a rule that passes because its data was missing is a vacuous green.
// ---------------------------------------------------------------------------

function lineRounds(rounds: Bet[]): Bet[] {
  return rounds.filter((b) => b instanceof Bet);
}

function cannotVerify(what: string): Verdict {
  return fail(`cannot verify ${what}: the response carried no readable grid. The chaos profile's grid `
    + "field names are UNVERIFIED (no BET payload captured yet) — capture one and correct "
    + "game_profile.ChaosRunner before trusting this case.");
}

/** CR_046: the triggering round awards `base(reels) x ways` free spins (Bonus counts [1,2,0,2,0] ->
 * 3 reels, ways 4, base 12 -> 48). Keyed off the GRID, never the server's counter against itself. */
export function freespinAward(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  for (const b of lineRounds(rounds)) {
    if (b.phase === "freespin") continue;             // only the triggering round awards
    const want = g.expectedFsAward(rawOf(b));
    if (want === null) {
      if (isFreespinTrigger(b)) return cannotVerify("the free-spin award");
      continue;
    }
    const got = b.total_free_spin;
    if (want === 0 && got === 0) continue;
    checked += 1;
    if (want !== got) {
      const reels = or(g.scatterReels(rawOf(b)), []) as number[];
      v.push(py`seq ${b.seq}: Bonus on ${reels.length} reel(s) ${reels} x ways ${g.scatterWays(rawOf(b))} should award ${want}, server awarded ${got}`);
    }
  }
  if (!checked && !v.length) return fail("no free-spin award observed in this stream");
  return ok(v);
}

/** The round paid exactly what its own paylines say: `payout == randomMultiplier x SUM(payoutRate)
 * x baseStake / betLines` (+ scatter, capped). The synthetic free-spin pseudo-line is excluded. */
export function payoutMatchesPaylines(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  for (const b of lineRounds(rounds)) {
    const raw = rawOf(b);
    const want = g.expectedPayout(raw);
    if (want === null) continue;                     // this round reported no payline array
    checked += 1;
    if (Math.abs(want - b.payout) > 0.005) {
      const rates = g.winLines(raw).map((p: Dict) => pyStr(get(p, "payoutRate"))).join("+") || "0";
      const cap = g.maxWinCap(raw);
      v.push(py`seq ${b.seq} (${b.phase}): credited ${b.payout}, but lines (${new PyText(rates)}) x ${g.baseStake(raw)}/${g.betLines(raw)} x mult ${g.multiplier(raw)} = ${pyRound(g.expectedLineWin(raw), 6)} plus scatter ${g.scatterMultiplier(raw)} x ${g.baseStake(raw)} = ${pyRound(g.expectedScatterWin(raw), 6)}`
        + (cap !== null ? py`, capped at ${cap}` : "") + py` -> ${want}`);
    }
  }
  if (!checked) return fail("no round reported a `paylines` array — cannot derive the payout");
  return ok(v);
}

/** CR_063: P1 ("Fuel") pays by WAYS across reels 1/3/5, at 10x per way, derived from the GRID. */
export function scatterPayMatchesP1Ways(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  for (const b of lineRounds(rounds)) {
    const raw = rawOf(b);
    const got = g.scatterMultiplier(raw);
    const ways = g.p1Ways(raw);
    if (ways === null) {
      if (got) return cannotVerify("the P1 scatter payout");
      continue;
    }
    if (!got && !ways) continue;                     // no P1 win this round
    checked += 1;
    const want = ways * g.P1_MULTIPLIER;
    if (Math.abs(want - got) > 1e-9) {
      const counts = g.reelCounts(raw, g.SYM_P1);
      v.push(py`seq ${b.seq}: P1 counts ${counts} give ${ways} way(s) x ${g.P1_MULTIPLIER} = ${want}, but scatterMultiplier is ${got}`);
    }
  }
  if (!checked && !v.length) return fail("no P1 scatter win in this stream");
  return ok(v);
}

/** A spin credits `min(rawPayout, cap left in the round)`, and flags `hasReachedMaxWin` exactly when
 * it was truncated. The cap counts the WHOLE round: `left = cap - (accumulatedWinAmount - payout)`.
 * A gross landing EXACTLY on what was left (within half a cent) accepts either flag value. */
export function maxWinCapApplied(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  for (const b of lineRounds(rounds)) {
    const raw = rawOf(b);
    const cap = b.max_win !== null ? b.max_win : g.maxWinCap(raw);
    if (cap === null || !has(raw, g.F_RAW_PAYOUT)) continue;
    const gross = tryFloat(or(get(raw, g.F_RAW_PAYOUT), 0));
    if (gross === null) continue;
    checked += 1;
    const before = g.roundWonBefore(raw);
    const left = g.maxWinLeft(raw, cap);
    const want = pyRound(Math.min(gross, left), 10);
    if (Math.abs(want - b.payout) > 0.005) {
      v.push(py`seq ${b.seq}: rawPayout ${gross} against cap ${cap} (${before} already won this round, ${left} left) should credit ${want}, but payout is ${b.payout}`);
    }
    const flag = truthy(get(raw, g.F_MAX_WIN));
    if (Math.abs(gross - left) <= 0.005) continue;   // exactly on the cap: reached, not cut
    const truncated = gross > left;
    if (flag !== truncated) {
      v.push(py`seq ${b.seq}: hasReachedMaxWin is ${flag} but rawPayout ${gross} vs ${left} left of cap ${cap} says it should be ${truncated}`);
    }
  }
  if (!checked) return fail("no round reported both a max-win cap and a rawPayout — nothing was checked");
  return ok(v);
}

/** Every reported `payoutRate` equals the BO paytable's multiplier for that combination. Lines whose
 * rate cannot be derived are SKIPPED, never assumed correct. */
export function paylineRateMatchesPaytable(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  let skipped = 0;
  for (const b of lineRounds(rounds)) {
    for (const p of g.winLines(rawOf(b)) as Dict[]) {
      const want = g.expectedLineRate(p);
      if (want === null) {
        skipped += 1;
        continue;
      }
      checked += 1;
      const got = tryFloat(or(get(p, "payoutRate"), 0), 0);
      if (Math.abs(want - got) > 1e-9) {
        v.push(py`seq ${b.seq}: line ${get(p, "payline")} '${get(p, "winType")}' reported rate ${got}, but the paytable gives ${want}`);
      }
    }
  }
  if (!checked) return fail(py`no payline rate could be derived (${skipped} skipped) — nothing was checked`);
  return ok(v);
}

/** Symbols only land on the reels their reel strips allow: P1 has weight 0 on reels 2 and 4, Wild
 * has weight 0 on reel 1. A symbol whose id this profile does not know is skipped. */
export function symbolReelRestrictions(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  const rules: Array<[unknown, string, Set<number> | null, Set<number> | null]> = [
    [g.SYM_P1, "P1", new Set(g.P1_REELS_FROM_CONFIG), null],
    [g.SYM_WILD, "Wild", null, new Set(g.WILD_FORBIDDEN_REELS)],
  ];
  for (const b of lineRounds(rounds)) {
    for (const [sym, label, allowed, forbidden] of rules) {
      if (sym === null) continue;                     // id unknown -> cannot check
      const seen = g.reelsWith(rawOf(b), sym) as number[] | null;
      if (seen === null) continue;
      checked += 1;
      const bad = seen.filter((x) => (allowed !== null && !allowed.has(x)) || (forbidden !== null && forbidden.has(x)));
      if (bad.length) {
        const where = allowed !== null ? py`allowed only on ${pySorted(allowed)}` : py`never allowed on ${pySorted(forbidden!)}`;
        v.push(py`seq ${b.seq}: ${label} landed on reel(s) ${bad} — ${new PyText(where)} (reel-strip weight is 0 there)`);
      }
    }
  }
  if (!checked) {
    return fail("no reel restriction could be checked — the symbol ids involved are unknown "
      + "(see game_profile.ChaosRunner.SYM_P1) or the grid was unreadable");
  }
  return ok(v);
}

/** `winlose` is the round's net: payout - stake. A free spin costs 0, so its net is its payout. */
export function winloseConsistent(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  for (const b of lineRounds(rounds)) {
    const raw = rawOf(b);
    if (!has(raw, g.F_WINLOSE)) continue;
    const got = tryFloat(or(get(raw, g.F_WINLOSE), 0));
    if (got === null) continue;
    const want = b.payout - b.stake;
    if (Math.abs(want - got) > 0.005) v.push(py`seq ${b.seq}: winlose ${got} != payout ${b.payout} - stake ${b.stake} = ${want}`);
  }
  return ok(v);
}

/** A retrigger inside the bonus ADDS to the remaining counter. Asserts the shape (the counter rose on
 * a round that landed the trigger count), not a number — the award is undocumented. */
export function freespinRetriggerAdds(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  const fs = lineRounds(rounds).filter((b) => b.phase === "freespin");
  for (const [prev, cur] of pairs(fs)) {
    if (cur.free_spins <= prev.free_spins - 1) continue;      // normal count-down
    const n = g.scatters(rawOf(cur));
    if (n === null) return cannotVerify("a free-spin retrigger");
    if (n < g.SCATTERS_TO_TRIGGER) {
      v.push(py`seq ${cur.seq}: remaining rose ${prev.free_spins}->${cur.free_spins} on ${n} scatter(s), fewer than the ${g.SCATTERS_TO_TRIGGER} a retrigger needs`);
    }
  }
  return ok(v);
}

/** CR_062: P1 ("Fuel") appears ONLY on reels 1, 3 and 5 — never on 2 or 4. */
export function p1ReelsOnly(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  for (const b of lineRounds(rounds)) {
    const seen = g.reelsWith(rawOf(b), g.SYM_P1) as number[] | null;
    if (seen === null) return cannotVerify("P1's reel positions");
    checked += 1;
    const bad = seen.filter((x) => !g.P1_REELS.includes(x));
    if (bad.length) v.push(py`seq ${b.seq}: P1 landed on reel(s) ${bad}; allowed only ${[...g.P1_REELS]}`);
  }
  if (!checked) return fail("no rounds to check");
  return ok(v);
}

/** CR_063: a P1 ways win reaches the money — `rawPayout` (the GROSS win, not the capped `payout`)
 * covers `ways x 10 x baseStake`. `>=` because a line win can land in the same round. */
export function p1WaysPayout(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  let uncapped = 0;
  for (const b of lineRounds(rounds)) {
    const raw = rawOf(b);
    const ways = g.p1Ways(raw);
    if (ways === null) return cannotVerify("P1's ways payout");
    if (ways <= 0) continue;
    if (!has(raw, g.F_RAW_PAYOUT)) {
      uncapped += 1;
      continue;                                       // no gross figure -> nothing safe to compare
    }
    const gross = tryFloat(or(get(raw, g.F_RAW_PAYOUT), 0));
    if (gross === null) continue;
    const base = g.baseStake(raw);
    const expect = ways * g.P1_MULTIPLIER * base;
    checked += 1;
    if (gross + 0.005 < expect) {
      v.push(py`seq ${b.seq}: ${ways} P1 way(s) on base bet ${base} should gross at least ${pyRound(expect, 4)}, but rawPayout is ${gross}`);
    }
  }
  if (!checked && !v.length) return fail(py`no P1 win with a rawPayout to check in this stream (${uncapped} skipped)`);
  return ok(v);
}

/** CR_057/058: every round's random multiplier is one of the configured ladder values. */
export function multiplierInLadder(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  for (const b of lineRounds(rounds)) {
    const m = g.multiplier(rawOf(b));
    if (m === null) continue;                        // this build does not report one per round
    checked += 1;
    const ladder = (g.MULTIPLIER_VALUES as unknown[]).map(pyFloat);
    if (!ladder.some((x) => pyEq(x, m))) {
      v.push(py`seq ${b.seq} (${b.phase}): multiplier x${m} is not in the configured ladder ${[...g.MULTIPLIER_VALUES]}`);
    }
  }
  if (!checked) {
    return fail("no round reported a multiplier — the profile's F_MULTIPLIER field name is UNVERIFIED; "
      + "capture a real BET response and correct game_profile.ChaosRunner");
  }
  return ok(v);
}

/** The per-step multiplier ladder of one cascade round, [] where the profile has no such notion. */
export function stepMultipliers(b: Bet): unknown[] {
  const lad = multiplierLadderOf(GAME.steps(rawOf(b)));
  return lad === null ? [] : lad.filter((m) => m !== null && m !== undefined);
}

/** Mah_043-045: within ONE cascade round the multiplier RISES step by step — strictly increasing,
 * never `+1` per cascade (Mystic's base game runs 1, 2, 3, 5, 8, 10). */
export function cascadeMultiplierClimbs(rounds: Bet[]): Verdict {
  const v: string[] = [];
  let checked = 0;
  for (const b of rounds) {
    const lad = stepMultipliers(b);
    if (lad.length < 2) continue;                    // nothing cascaded in this round
    checked += 1;
    for (let i = 1; i < lad.length; i++) {
      if (!pyLt(lad[i - 1], lad[i])) {
        v.push(py`seq ${b.seq}: multiplier did not rise at step ${i} -> ${lad}`);
        break;
      }
    }
  }
  if (!checked) {
    return fail("no round in this stream had >= 2 cascade steps with a per-step multiplier -- "
      + "nothing to judge (use a scenario that reaches a cascade win)");
  }
  return ok(v);
}

/** Mah_043-045: the climb belongs to ONE round; the next round starts back where the last began. */
export function cascadeMultiplierResets(rounds: Bet[]): Verdict {
  for (let i = 0; i < rounds.length - 1; i++) {
    const lad = stepMultipliers(rounds[i]);
    if (lad.length < 2) continue;
    const nxt = stepMultipliers(rounds[i + 1]);
    if (!nxt.length) {
      return fail(py`seq ${rounds[i + 1].seq}: the round after the cascade reports no multiplier -- cannot tell whether it reset`);
    }
    if (!pyEq(nxt[0], lad[0])) {
      return fail(py`seq ${rounds[i + 1].seq}: multiplier did not reset -- the cascade round started at x${lad[0]} and climbed to x${lad[lad.length - 1]}, the next round started at x${nxt[0]}`);
    }
    return ok([]);
  }
  return fail("no cascade round followed by another round in this stream -- nothing to judge");
}

/** CR_051: with Ante Bet on, the DEDUCTED stake is base bet x the ante multiplier, while the payout
 * base stays the plain bet. TWO checks: the ratio alone passes a server that multiplied BOTH, so the
 * ante rounds are also compared against the NON-ante rounds of the same stream. */
export function anteStakeMultiplier(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  const anteBases: Array<[number, number]> = [];
  const plainBases: Array<[number, number]> = [];
  for (const b of lineRounds(rounds)) {
    if (b.phase === "freespin") continue;            // free spins cost 0
    const raw = rawOf(b);
    const base = g.baseStake(raw);
    if (!g.isAnteRound(raw)) {
      if (base > 0) plainBases.push([b.seq, base]);
      continue;
    }
    const mult = g.anteMultiplier(raw);
    checked += 1;
    anteBases.push([b.seq, base]);
    if (mult === null) {
      v.push(py`seq ${b.seq}: stake ${b.stake} exceeds base ${base} but the round reports no \`${g.F_ANTE_MULTIPLIER}\` — cannot check the ratio`);
    } else if (Math.abs(b.stake - base * mult) > 1e-6) {
      v.push(py`seq ${b.seq}: ante round deducted ${b.stake}, expected base ${base} x ${mult} = ${pyRound(base * mult, 6)}`);
    }
  }
  if (anteBases.length && plainBases.length) {
    const plainBase = plainBases[0][1];
    for (const [seq, base] of anteBases) {
      if (Math.abs(base - plainBase) > 1e-6) {
        v.push(py`seq ${seq}: ante inflated the PAYOUT BASE to ${base}; it must stay ${plainBase} (seq ${plainBases[0][0]}) — ante multiplies what is deducted, never the base payouts derive from`);
      }
    }
  }
  if (!checked && !v.length) {
    return fail(py`no ante-bet round in this stream: every round had stake == baseStake and \`${g.F_ANTE_MULTIPLIER}\` == 0. No captured payload yet shows ante engaged, so how the server reports it is still unconfirmed (games/chaos/bet-response.md §Ante).`);
  }
  return ok(v);
}

/** CR_055/056: the ante configuration in force when the bonus started is FROZEN for its duration. */
export function anteFrozenInFreespins(rounds: Bet[]): Verdict {
  const g = G();
  const fs = lineRounds(rounds).filter((b) => b.phase === "freespin" || b.phase === "teardown");
  if (!fs.length) return fail("no free-spin rounds in this stream");
  const seen = new Set<number>();
  for (const b of fs) {
    const m = g.anteMultiplier(rawOf(b));
    if (m !== null) seen.add(m);                     // a free spin may not restate the multiplier
  }
  if (!seen.size) {
    return fail("no free spin reported an ante multiplier — cannot verify it stayed frozen "
      + "(no captured payload yet shows the field carrying a value; see bet-response.md)");
  }
  if (seen.size > 1) {
    return fail(py`ante multiplier changed mid-bonus: saw ${pySorted(seen)} across ${fs.length} free spins; the value at trigger must stay frozen until the bonus ends`);
  }
  return ok([]);
}

/** DV-60: a Premium Reel spin is charged `baseStake x premiumReelMultiplier`, and a package never
 * moves `baseStake`. Paid rounds only; compared to the cent. */
export function premiumReelStake(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let checked = 0;
  let plainRound: [number, number] | null = null;
  const premiumBases: Array<[number, number]> = [];
  for (const b of lineRounds(rounds)) {
    if (b.phase === "freespin" || b.phase === "teardown") continue;
    const raw = rawOf(b);
    const base = g.baseStake(raw);
    const rid = g.premiumReelId(raw);
    if (!rid) {
      if (plainRound === null && base > 0 && !g.isAnteRound(raw)) plainRound = [b.seq, base];
      continue;
    }
    checked += 1;
    premiumBases.push([b.seq, base]);
    const mult = g.premiumReelMultiplier(raw);
    if (mult <= 0) v.push(py`seq ${b.seq}: echoes package ${rid} but no \`${g.F_PREMIUM_REEL_MULTIPLIER}\``);
    else if (Math.abs(b.stake - base * mult) > 0.005) {
      v.push(py`seq ${b.seq}: package ${rid} charged ${b.stake}, expected base ${base} x ${mult} = ${pyRound(base * mult, 4)}`);
    }
  }
  if (plainRound) {
    for (const [seq, base] of premiumBases) {
      if (Math.abs(base - plainRound[1]) > 0.005) {
        v.push(py`seq ${seq}: the package moved the PAYOUT BASE to ${base}; it must stay ${plainRound[1]} (seq ${plainRound[0]}) -- a package multiplies what is deducted, never the base payouts derive from`);
      }
    }
  }
  if (!checked && !v.length) {
    return fail(py`no Premium Reel round in this stream: every paid round echoed \`${g.F_PREMIUM_REEL_ID}\` 0 -- nothing to judge`);
  }
  return ok(v);
}

/** DV-64 / DV-66: every bonus round echoes exactly the Premium Reel package of the round that OPENED
 * its bonus. Each bonus in the stream is judged against its own opener. */
export function premiumReelFrozenInFreespins(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  let judged = 0;
  let opener: Bet | null = null;
  for (const b of lineRounds(rounds)) {
    if (b.phase === "teardown") continue;
    if (b.phase !== "freespin") {
      opener = b;
      continue;
    }
    if (opener === null) continue;
    judged += 1;
    const want: [number, number] = [g.premiumReelId(rawOf(opener)), g.premiumReelMultiplier(rawOf(opener))];
    const got: [number, number] = [g.premiumReelId(rawOf(b)), g.premiumReelMultiplier(rawOf(b))];
    if (got[0] !== want[0] || Math.abs(got[1] - want[1]) > 1e-9) {
      v.push(py`seq ${b.seq}: bonus round echoes package ${got[0]} x${got[1]}, but its bonus was opened by seq ${opener.seq} with package ${want[0]} x${want[1]}`);
    }
  }
  if (!judged) return fail("no bonus round after an opening round in this stream");
  return ok(v);
}

/** CR_075: the win tier the round falls into matches the configured bands, measured against the
 * BASE stake (whether the game measures base bet or the ante-inflated amount is undocumented). */
export function winTierBands(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  for (const b of lineRounds(rounds)) {
    const base = g.baseStake(rawOf(b));
    if (base <= 0 || b.payout <= 0) continue;
    const tier = g.winTier(b.payout, base);
    const x = b.payout / base;
    const floors = new Map<string, number>(g.WIN_TIERS as Array<[string, number]>);
    const lo = floors.get(tier);
    if (lo !== undefined && x + 1e-9 < lo) v.push(py`seq ${b.seq}: ${pyRound(x, 3)}x classified ${tier} whose floor is ${lo}x`);
  }
  return ok(v);
}

/** A wild expands ONLY in free spins — `views` may differ from `rawViews` on no paid round. */
export function wildExpandsOnlyInFreespins(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  for (const b of rounds) {
    const expanded = g.expandedReels(rawOf(b)) as unknown[];
    if (expanded.length && b.stake) {
      v.push(py`round ${b.seq}: reels ${expanded} expanded on a PAID round (stake ${b.stake}) -- expansion is free-spins only`);
    }
  }
  return ok(v);
}

/** A hashable key for a JSON scalar, equal where Python's dict keys would be (1 == 1.0 == True). */
function keyOf(x: unknown): string {
  if (isNumber(x)) return `n:${Number(x)}`;
  if (x === null || x === undefined) return "none";
  return `s:${pyRepr(x)}`;
}

/** Where a reel expanded, the changed cells all became the SAME symbol, and that symbol already
 * LANDED on the reel. ⚠ A cell that KEPT its symbol is never a violation — the rule is about what
 * was WRITTEN. (Changed symbols are listed in first-seen order; Python's set order was arbitrary.) */
export function wildExpansionIsUniform(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = G();
  for (const b of rounds) {
    const raw = rawOf(b);
    const before = new Map<string, unknown[]>();
    for (const reel of g.rawReels(raw) as unknown[]) {
      const d = strictDict(reel);
      before.set(keyOf(get(d, "id")), pyList(or(get(d, "value"), [])));
    }
    for (const after of g.reels(raw) as unknown[]) {
      const d = strictDict(after);
      const rid = get(d, "id");
      const now = pyList(or(get(d, "value"), []));
      const was = before.get(keyOf(rid));
      if (was === undefined || was === null || pyEq(was, now)) continue;
      const changed: unknown[] = [];
      for (let i = 0; i < Math.min(was.length, now.length); i++) {
        if (!pyEq(was[i], now[i]) && !changed.some((c) => pyEq(c, now[i]))) changed.push(now[i]);
      }
      if (changed.length > 1) {
        v.push(py`round ${b.seq} reel ${rid}: expansion wrote ${pySorted(changed)} -- a wild expansion writes ONE symbol, not several`);
      }
      for (const sym of changed) {
        if (!was.some((w) => pyEq(w, sym))) {
          v.push(py`round ${b.seq} reel ${rid}: expanded to symbol ${sym}, which never landed on that reel (${was}) -- nothing was there to expand`);
        }
      }
    }
  }
  return ok(v);
}

/** A PAID Joker round credited exactly `SUM(payoutRate) x (baseStake / betLines)`. ⚠ `multiplier` IS
 * NOT IN THIS FORMULA (the winning capture carries 2.5 and paid 1.5). Free spins and max-win
 * truncations are skipped, as is the `payline: -1` scatter row. */
export function jokerPayoutMatchesPaylines(rounds: Bet[]): Verdict {
  const v: string[] = [];
  let checked = 0;
  for (const b of rounds) {
    const raw = rawOf(b);
    if (!truthy(get(raw, "stake"))) continue;        // free spin (or no stake reported)
    if (truthy(get(raw, "hasReachedMaxWin"))) continue;   // the cap set the payout, not the lines
    const lines = get(raw, "paylines");
    if (!isList(lines)) continue;                    // this round reported no payline array
    let divisor: number;
    let lineBet: number;
    let rates: number[];
    try {
      divisor = pyInt(or(get(raw, "betLines"), GAME.LINE_DIVISOR));
      if (divisor === 0) continue;                   // Python: ZeroDivisionError -> skipped
      lineBet = pyFloat(or(get(raw, "baseStake"), get(raw, "stake"))) / divisor;
      rates = lines.filter((p) => !pyEq(get(strictDict(p), "payline"), -1))
        .map((p) => pyFloat(or(get(p, "payoutRate"), 0)));
    } catch (e) {
      if (isConversionError(e)) continue;
      throw e;
    }
    checked += 1;
    const want = sum(rates) * lineBet;
    if (Math.abs(want - b.payout) > 0.005) {
      const shown = rates.map(pyStr).join("+") || "0";
      v.push(py`seq ${b.seq} (${b.phase}): credited ${b.payout}, but lines (${new PyText(shown)}) x ${get(raw, "baseStake")}/${divisor} = ${pyRound(want, 6)}`);
    }
  }
  if (!checked) {
    return fail("nothing was checked: every round in this stream was skipped as a free spin "
      + "(stake 0), a max-win truncation, or a round with no paylines array -- so a pass "
      + "here would mean nothing");
  }
  return ok(v);
}

/** A WAYS round credited exactly what its own `wayDetails` say: per entry `symbolMultiplier x
 * totalWildMultiplier x wayCount x baseStake`, and per round the SUM of the entries. ⚠ `baseStake`,
 * never `stake` — free spins are checked too. An EMPTY `wayDetails` sums to 0 and is checked. */
export function waysPayoutMatches(rounds: Bet[]): Verdict {
  const v: string[] = [];
  const g = GAME as any;
  let checked = 0;
  for (const b of rounds) {
    const raw = rawOf(b);
    if (truthy(get(raw, GAME.F_MAX_WIN))) continue;  // the cap set the payout, not the ways
    const ways = get(raw, "F_WAY_DETAILS" in g ? g.F_WAY_DETAILS : "wayDetails");
    if (!isList(ways)) continue;                     // no ways array
    const base = GAME.baseStake(raw);
    if (!base) continue;                             // nothing to multiply -- cannot verify
    checked += 1;
    let total = 0;
    ways.forEach((w, n) => {
      const want = typeof g.wayPayout === "function" ? g.wayPayout(strictDict(w), base) : null;
      if (want === null) {
        v.push(py`seq ${b.seq} (${b.phase}): way ${n} is missing a factor -- ${r(w)}`);
        return;
      }
      const got = pyFloat(or(get(w, "payout"), 0));
      total += got;
      if (Math.abs(want - got) > 0.005) {
        v.push(py`seq ${b.seq} (${b.phase}): way ${n} (symbol ${get(w, "symbolId")}) credited ${got}, but ${get(w, "symbolMultiplier")} x ${get(w, "totalWildMultiplier")} x ${get(w, "wayCount")} ways x ${base} = ${pyRound(want, 6)}`);
      }
    });
    if (Math.abs(total - b.payout) > 0.005) {
      v.push(py`seq ${b.seq} (${b.phase}): round credited ${b.payout}, but its ${ways.length} way(s) sum to ${pyRound(total, 6)}`);
    }
  }
  if (!checked) {
    return fail("nothing was checked: every round in this stream was skipped as a max-win truncation, "
      + "a round with no wayDetails array, or a round with no base stake -- so a pass here "
      + "would mean nothing");
  }
  return ok(v);
}

/** A non-dict `w` makes Python's `w.get(...)` raise AttributeError; keep that. */
function strictDict(w: unknown): Dict {
  if (!isDict(w)) {
    const e = new Error(`'${typeof w}' object has no attribute 'get'`);
    e.name = "AttributeError";
    throw e;
  }
  return w;
}

/** Every free spin is played at a multiplier the game offers (`LUCKY_MULTIPLIERS`: 1, 3, 12). Rounds
 * reporting no multiplier are SKIPPED: `luckyMultiplier` is 0 on every base round. */
export function freespinMultiplierInLadder(rounds: Bet[]): Verdict {
  const g = GAME as any;
  const ladder: unknown[] = [...(or(g.LUCKY_MULTIPLIERS, []) as unknown[])];
  if (!ladder.length) return ok([]);
  const values = ladder.map(pyFloat);
  return ok(rounds.flatMap((b) => {
    const m = b.stake === 0 ? g.luckyMultiplier(rawOf(b)) : null;
    if (m === null || values.some((x) => pyEq(x, pyFloat(m)))) return [];
    return [py`round ${b.seq}: free spin at multiplier ${g.luckyMultiplier(rawOf(b))}, not one of ${ladder}`];
  }));
}

export const INVARIANTS: Readonly<Record<string, (rounds: Bet[]) => Verdict>> = {
  wild_expands_only_in_freespins: wildExpandsOnlyInFreespins,
  wild_expansion_is_uniform: wildExpansionIsUniform,
  freespin_multiplier_in_ladder: freespinMultiplierInLadder,
  joker_payout_matches_paylines: jokerPayoutMatchesPaylines,
  ways_payout_matches: waysPayoutMatches,
  conservation,
  // line game / Chaos Runner
  freespin_award: freespinAward,
  payout_matches_paylines: payoutMatchesPaylines,
  payline_rate_matches_paytable: paylineRateMatchesPaytable,
  scatter_pay_matches_p1_ways: scatterPayMatchesP1Ways,
  max_win_cap_applied: maxWinCapApplied,
  symbol_reel_restrictions: symbolReelRestrictions,
  winlose_consistent: winloseConsistent,
  freespin_retrigger_adds: freespinRetriggerAdds,
  p1_reels_only: p1ReelsOnly,
  p1_ways_payout: p1WaysPayout,
  multiplier_in_ladder: multiplierInLadder,
  cascade_multiplier_climbs: cascadeMultiplierClimbs,
  cascade_multiplier_resets: cascadeMultiplierResets,
  ante_stake_multiplier: anteStakeMultiplier,
  ante_frozen_in_freespins: anteFrozenInFreespins,
  premium_reel_stake: premiumReelStake,
  premium_reel_frozen_in_freespins: premiumReelFrozenInFreespins,
  win_tier_bands: winTierBands,
  holdspin_coins_held: holdspinCoinsHeld,
  holdspin_reset_on_new_coin: holdspinResetOnNewCoin,
  holdspin_countdown: holdspinCountdown,
  holdspin_ends_correctly: holdspinEndsCorrectly,
  holdspin_award: holdspinAward,
  holdspin_payout_matches_coins: holdspinPayoutMatchesCoins,
  stake_zero_in_freespins: stakeZeroInFreespins,
  freespins_monotonic: freespinsMonotonic,
  terminal_reached: terminalReached,
};

