/**
 * Every profile answers every recorded input exactly as the Python profile did
 * (baseline/profiles.json, from baseline/make_baseline.py `profiles()`).
 */
import { describe, expect, it } from "vitest";
import { getProfile } from "../src/profile/games.js";
import { SERVER_ERRORS, type SlotGame } from "../src/profile/slot-game.js";
import { isDict } from "../src/py.js";
import { call, camel, Diff, loadBaseline, plain } from "./baseline.js";

const B = loadBaseline("profiles.json");
const CAPS: Record<string, unknown> = {
  none: null,
  set: { max_payout: 10.0, max_multiplier: 500.0 },
  "mult-only": { max_payout: null, max_multiplier: 2.5 },
};

type AnyGame = SlotGame & Record<string, any>;

function method(g: AnyGame, py: string): ((...a: unknown[]) => unknown) | undefined {
  const fn = g[camel(py)];
  return typeof fn === "function" ? fn.bind(g) : undefined;
}

function rawOf(label: string, inputs: Record<string, unknown>): unknown {
  return inputs[label];
}

/** Rebuild the generator's input set from the fixtures, keyed like the baseline. */
function inputs(): { bets: Record<string, unknown>; infos: Record<string, unknown> } {
  return loadBaseline("profiles_inputs.json");
}

describe("game profiles", () => {
  const { bets, infos } = inputs();

  for (const [name, rec] of Object.entries<any>(B.profiles)) {
    describe(name, () => {
      const g = getProfile(name) as AnyGame;

      it("constants", () => {
        const d = new Diff();
        for (const [k, v] of Object.entries(rec.attributes)) d.expect(`${name}.${k}`, plain(g[k]), v);
        const extra = Object.keys(g).filter((k) => /^[A-Z][A-Z0-9_]*$/.test(k) && !(k in rec.attributes));
        d.expect(`${name}: fields Python does not have`, extra, []);
        expect(d.lines, d.report()).toEqual([]);
      });

      it("bet readers", () => {
        const d = new Diff();
        for (const [label, want] of Object.entries<any>(rec.bets)) {
          const raw = rawOf(label, bets) as any;
          for (const [key, w] of Object.entries<any>(want)) {
            const where = `${name} ${label} ${key}`;
            const [base, variant] = key.split("@");
            if (key === "expected_line_rate") {
              const lines = Array.isArray(raw?.paylines) ? raw.paylines : [];
              d.expect(where, lines.map((p: unknown) => call(() => g.expectedLineRate(isDict(p) ? p : null))), w);
            } else if (key === "way_payout") {
              const ways = Array.isArray(raw?.wayDetails) ? raw.wayDetails.filter(isDict) : [];
              d.expect(where, ways.map((wy: unknown) => call(() => g.wayPayout(wy, raw.baseStake ?? null))), w);
            } else if ((base === "max_win_cap" || base === "max_win_left") && variant) {
              const [capsLabel, extra] = variant.split("+");
              g.launch_caps = structuredClone(CAPS[capsLabel]) as any;
              const fn = method(g, base)!;
              d.expect(where, call(() => (extra ? fn(raw, 7.0) : fn(raw))), w);
              g.launch_caps = null;
            } else if (base === "is_big_win") {
              const th = variant === "None" ? null : variant === "x" ? "x" : Number(variant);
              d.expect(where, call(() => g.isBigWin(raw, th)), w);
            } else if (["cluster_wins", "win_clusters", "multiplier_ladder"].includes(base)) {
              const steps = g.steps(raw);
              const payout = typeof raw?.payout === "number" ? raw.payout : 0;
              const fn = method(g, base)!;
              d.expect(where, call(() => (base === "cluster_wins" && variant !== "payout0" ? fn(steps, payout) : fn(steps))), w);
            } else if (["reel_counts", "symbol_count", "reels_with"].includes(base)) {
              const sym = variant === "None" ? null : /^\d+$/.test(variant) ? Number(variant) : variant;
              d.expect(where, call(() => method(g, base)!(raw, sym)), w);
            } else if (base === "ways") {
              const reels = JSON.parse(key.split("@")[2]);
              d.expect(where, call(() => g.ways(raw, 5, reels)), w);
            } else {
              const fn = method(g, key);
              if (!fn) {
                d.expect(where, "method missing in TS", w);
                continue;
              }
              d.expect(where, call(() => fn(raw)), w);
            }
          }
        }
        expect(d.lines, d.report()).toEqual([]);
      });

      it("gameInfo readers", () => {
        const d = new Diff();
        for (const [label, want] of Object.entries<any>(rec.infos)) {
          const raw = infos[label];
          for (const [key, w] of Object.entries<any>(want)) {
            const where = `${name} ${label} ${key}`;
            if (key === "remember_launch") {
              g.launch_caps = { max_payout: 99.0, max_multiplier: null };
              d.expect(where, call(() => g.rememberLaunch(raw)), w);
              d.expect(`${where} -> launch_caps`, plain(g.launch_caps), want.launch_caps_after);
              g.launch_caps = null;
            } else if (key !== "launch_caps_after") {
              d.expect(where, call(() => method(g, key)!(raw)), w);
            }
          }
        }
        expect(d.lines, d.report()).toEqual([]);
      });

      it("dialect", () => {
        const d = new Diff();
        const dl = rec.dialect;
        for (const [r, w] of Object.entries(dl.button)) d.expect(`button(${r})`, call(() => g.button(r)), w);
        for (const [r, w] of Object.entries(dl["button@i=3"])) d.expect(`button(${r}, i=3)`, call(() => g.button(r, { i: 3 })), w);
        for (const [r, w] of Object.entries(dl.popup)) d.expect(`popup(${r})`, call(() => g.popup(r)), w);
        for (const [n, w] of Object.entries(dl.resolve)) d.expect(`resolve(${n})`, call(() => g.resolve(n)), w);
        for (const [n, w] of Object.entries(dl.via)) d.expect(`via(${n})`, call(() => g.via(n)), w);
        for (const [n, w] of Object.entries(dl.resolve_popup)) d.expect(`resolve_popup(${n})`, call(() => g.resolvePopup(n)), w);
        for (const c of dl.display_amount) {
          const { v, ...w } = c;
          d.expect(`display_amount(${JSON.stringify(v)})`, call(() => g.displayAmount(v)), w);
        }
        for (const c of dl.line_bet) {
          const { v, ...w } = c;
          d.expect(`line_bet(${v})`, call(() => g.lineBet(v)), w);
        }
        d.expect("api_check_js", call(() => g.apiCheckJs()), dl.api_check_js);
        for (const c of dl.win_tier ?? []) {
          const { p, s, ...w } = c;
          d.expect(`win_tier(${p}, ${s})`, call(() => g.winTier(p, s)), w);
        }
        for (const c of dl.family_of ?? []) {
          const { v, ...w } = c;
          d.expect(`family_of(${JSON.stringify(v)})`, call(() => g.familyOf(v)), w);
        }
        if (dl.ways_total) d.expect("ways_total", call(() => g.waysTotal()), dl.ways_total);
        expect(d.lines, d.report()).toEqual([]);
      });
    });
  }

  it("server error table", () => {
    const d = new Diff();
    for (const [k, v] of Object.entries(B.server_errors)) d.expect(`code ${k}`, SERVER_ERRORS[Number(k)], v);
    d.expect("same codes", Object.keys(SERVER_ERRORS).length, Object.keys(B.server_errors).length);
    expect(d.lines, d.report()).toEqual([]);
  });

  it("unknown profile falls back to mystic", () => {
    expect(call(() => getProfile("nope").name)).toEqual(B.unknown_profile);
  });
});
