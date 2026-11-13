/**
 * Bet.parse and every invariant answer as the Python did on every recorded stream
 * (baseline/invariants.json, from baseline/make_baseline.py `invariants()`).
 */
import { describe, expect, it } from "vitest";
import { Bet, BetRejected, entersHoldspin, isFreespinTrigger, isTerminal, winSteps } from "../src/engine/bet.js";
import { GAME, setProfile } from "../src/engine/game.js";
import { INVARIANTS } from "../src/engine/invariants.js";
import { call, Diff, loadBaseline } from "./baseline.js";

const B = loadBaseline("invariants.json");

/** Python prints a float `7.0`; a JS number cannot remember it was one (see PORTING.md). */
function dropFloatZero(x: unknown): unknown {
  if (typeof x === "string") return x.replace(/(\d)\.0(?!\d)/g, "$1");
  if (Array.isArray(x)) return x.map(dropFloatZero);
  if (x && typeof x === "object") {
    return Object.fromEntries(Object.entries(x as Record<string, unknown>).map(([k, v]) => [k, dropFloatZero(v)]));
  }
  return x;
}

/** Checks whose Python violation ORDER came from iterating a set of strings — randomised per process
 * by Python's hash seed, so there is no order to match. Compared as a multiset. */
const UNORDERED = new Set(["wild_expansion_is_uniform"]);

describe("Bet.parse + invariants", () => {
  it("BetRejected wording", () => {
    setProfile("mystic");
    let msg = "";
    try {
      Bet.parse({ status: 0, code: -2002, msg: "Insufficient balance", timestamp: 1 }, 3);
    } catch (e) {
      expect(e).toBeInstanceOf(BetRejected);
      msg = (e as Error).message;
    }
    expect(msg).toBe(B.rejected_message);
  });

  const byProfile = new Map<string, Array<[string, any]>>();
  for (const [label, rec] of Object.entries<any>(B.streams)) {
    const list = byProfile.get(rec.profile) ?? [];
    list.push([label, rec]);
    byProfile.set(rec.profile, list);
  }

  for (const [profile, streams] of byProfile) {
    it(`streams under ${profile}`, () => {
      const d = new Diff();
      for (const [label, rec] of streams) {
        setProfile(profile);
        GAME.launch_caps = null;
        const bets: Bet[] = [];
        rec.rounds.forEach((rd: any, i: number) => {
          const got = call(() => {
            const b = Bet.parse(structuredClone(rd.raw), i);
            b.phase = rd.phase;
            bets.push(b);
            return {
              ...b.row(), max_win: b.max_win, terminal: isTerminal(b), win_steps: winSteps(b),
              enters_holdspin: call(() => entersHoldspin(b)), is_trigger: isFreespinTrigger(b),
            };
          });
          d.expect(`${label} parse[${i}]`, got, rec.parse[i]);
        });
        for (const [name, want] of Object.entries<any>(rec.results)) {
          const fn = INVARIANTS[name];
          if (!fn) {
            d.expect(`${label} ${name}`, "missing in TS", want);
            continue;
          }
          let got: any = dropFloatZero(call(() => fn(bets)));
          let exp: any = dropFloatZero(want);
          if (UNORDERED.has(name) && got.ok && exp.ok) {
            got = { ok: { ...got.ok, violations: [...got.ok.violations].sort() } };
            exp = { ok: { ...exp.ok, violations: [...exp.ok.violations].sort() } };
          }
          d.expect(`${label} ${name}`, got, exp);
        }
      }
      expect(d.lines, d.report(60)).toEqual([]);
    });
  }

  it("registry has exactly the Python invariants", () => {
    const anyRec: any = Object.values(B.streams)[0];
    expect(Object.keys(INVARIANTS).sort()).toEqual(Object.keys(anyRec.results).sort());
  });
});

