/**
 * The `stream:` evaluator and the case builder answer as Python did
 * (baseline/exprs.json and baseline/cases.json, from baseline/make_baseline.py).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";
import { Bet } from "../src/engine/bet.js";
import { pyEval } from "../src/engine/expr.js";
import { GAME, setProfile } from "../src/engine/game.js";
import { ScenarioResult } from "../src/engine/result.js";
import { aiCalls, buildCases } from "../src/runner/cases.js";
import { call, Diff, loadBaseline, REPO } from "./baseline.js";

const EXPRS = loadBaseline<Record<string, any>>("exprs.json");
const CASES = loadBaseline("cases.json");
const INPUTS = loadBaseline("invariants.json");

/** Python prints a float `7.0`; a JS number cannot remember it was one (see PORTING.md). */
function dropFloatZero(x: unknown): unknown {
  if (typeof x === "string") return x.replace(/(\d)\.0(?!\d)/g, "$1");
  if (Array.isArray(x)) return x.map(dropFloatZero);
  if (x && typeof x === "object") {
    return Object.fromEntries(Object.entries(x as Record<string, unknown>).map(([k, v]) => [k, dropFloatZero(v)]));
  }
  return x;
}

describe("stream: evaluator", () => {
  it("matches CPython eval on every recorded expression", () => {
    const d = new Diff();
    for (const [src, want] of Object.entries(EXPRS)) {
      let got: any;
      try {
        const v = pyEval(src);
        got = { ok: v.repr, truth: v.truth };
      } catch (e) {
        got = { err: (e as Error).name, msg: (e as Error).message };
      }
      if ("ok" in want) d.expect(src, { ...got, ok: dropFloatZero(got.ok) }, { ...want, ok: dropFloatZero(want.ok) });
      else d.expect(src, got, want);
    }
    expect(d.lines, d.report(60)).toEqual([]);
  });
});

/** The fixture rounds the generator used for a game, as raw replies. */
function fixtureRaws(game: string): unknown[] {
  const rec: any = Object.values(INPUTS.streams).find((s: any) => s.profile === (game === "zeus" ? "zeus" : game) && false);
  void rec;
  const stream = INPUTS.streams[`${game === "zeus" ? "zeus" : game}/all#${game === "zeus" ? "zeus" : game}`];
  return stream.rounds.map((r: any) => r.raw);
}

/** Group ids are object ids in Python and counters here: compare which specs SHARE one, not its name. */
function normaliseGroups(cases: unknown): unknown {
  const names = new Map<string, string>();
  const walk = (x: any): any => {
    if (Array.isArray(x)) return x.map(walk);
    if (x && typeof x === "object") {
      const out: any = {};
      for (const [k, v] of Object.entries(x)) {
        if (k === "group" && typeof v === "string") {
          if (!names.has(v)) names.set(v, `${v.startsWith("shared-img-") ? "img" : "vid"}#${names.size}`);
          out[k] = names.get(v);
        } else out[k] = walk(v);
      }
      return out;
    }
    return x;
  };
  return walk(cases);
}

describe("build_cases", () => {
  const suites = new Map<string, any>();
  const runsOf = (suite: string): any[] => {
    if (!suites.has(suite)) {
      const doc = parseYaml(readFileSync(join(REPO, suite), "utf-8"), { version: "1.1" }) ?? {};
      suites.set(suite, (doc.runs ?? []).filter((r: any) => r && typeof r === "object").map((r: any) => r.run ?? r));
    }
    return suites.get(suite);
  };

  const bySuite = new Map<string, any[]>();
  for (const rec of CASES.runs) {
    const l = bySuite.get(rec.suite) ?? [];
    l.push(rec);
    bySuite.set(rec.suite, l);
  }

  for (const [suite, recs] of bySuite) {
    it(suite, () => {
      const d = new Diff();
      for (const rec of recs) {
        setProfile(rec.game);
        GAME.launch_caps = null;
        const runDoc = runsOf(suite)[rec.run];
        const bets = fixtureRaws(rec.fixture_game).map((raw, i) => {
          const b = Bet.parse(structuredClone(raw), i);
          b.phase = rec.phases[i];
          return b;
        });
        const result = new ScenarioResult(runDoc?.scenario ?? null, rec.reached, bets[0] ?? null, bets, [], [], [1.5, 88.25], {
          reason: rec.reached ? null : "synthetic: not reached",
          round_windows: bets.map((_, i) => [i * 10.0, i * 10.0 + 4.5] as [number, number]),
          marks: structuredClone(rec.marks),
          facts: structuredClone(rec.facts),
          shots: structuredClone(rec.shots),
        });
        const got = call(() => buildCases(runDoc, result));
        const where = `${suite} run ${rec.run} [${rec.variant}] (${runDoc?.name ?? runDoc?.scenario})`;
        d.expect(where, dropFloatZero(normaliseGroups(got)), dropFloatZero(normaliseGroups(rec.cases)));
        if ("ok" in got && rec.ai_calls !== undefined) d.expect(`${where} ai_calls`, aiCalls(got.ok as any), rec.ai_calls);
      }
      expect(d.lines, d.report(20)).toEqual([]);
    });
  }
});
