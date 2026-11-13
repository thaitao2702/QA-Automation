import { describe, expect, it } from "vitest";
import { pyFloat, pyFloatRepr, pyInt, PyRandom, pyRepr, pyRound, pyStr, quantizeDecimal, sum } from "../src/py.js";
import { call, Diff, loadBaseline } from "./baseline.js";

const B = loadBaseline("py_compat.json");

describe("python semantics", () => {
  it("round() matches CPython", () => {
    const d = new Diff();
    for (const c of B.round) {
      const got = call(() => (c.nd === null ? pyRound(c.x) : pyRound(c.x, c.nd)));
      d.expect(`round(${c.x}, ${c.nd})`, got, "ok" in c ? { ok: c.ok } : { err: c.err });
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("repr(float) matches CPython (integer-valued floats excepted)", () => {
    const d = new Diff();
    for (const c of B.repr) {
      const want = String(c.s).replace(/\.0$/, "");
      d.expect(`repr(${c.x})`, pyFloatRepr(c.x), want);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("sum() is compensated like CPython 3.12+", () => {
    const d = new Diff();
    for (const c of B.sum) d.expect(`sum(${JSON.stringify(c.xs)})`, sum(c.xs), c.s);
    expect(d.lines, d.report()).toEqual([]);
  });

  it("int() / float() accept and refuse what CPython does", () => {
    const d = new Diff();
    for (const c of B.conv) {
      d.expect(`int(${JSON.stringify(c.v)})`, call(() => pyInt(c.v)), c.int);
      d.expect(`float(${JSON.stringify(c.v)})`, call(() => pyFloat(c.v)), c.float);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("Decimal quantize matches", () => {
    const d = new Diff();
    for (const c of B.quantize) {
      d.expect(`quantize(${JSON.stringify(c.v)}, ${c.places}, ${c.mode})`,
        quantizeDecimal(c.v, c.places, c.mode), c.s);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("random.Random(seed) draws the same numbers as CPython", () => {
    const d = new Diff();
    const range = (n: number) => [...Array(n).keys()];
    for (const c of B.random) {
      const seed = BigInt(c.seed);
      const r = new PyRandom(seed);
      d.expect(`sample16_7 seed ${c.seed}`, new PyRandom(seed).sample(range(16), 7), c.sample16_7);
      d.expect(`sample100_9 seed ${c.seed}`, new PyRandom(seed).sample(range(100), 9), c.sample100_9);
      d.expect(`sample16_16 seed ${c.seed}`, new PyRandom(seed).sample(range(16), 16), c.sample16_16);
      d.expect(`random seed ${c.seed}`, [r.random(), r.random(), r.random()], c.random);
      d.expect(`randrange seed ${c.seed}`, new PyRandom(seed).randrange(1000000), c.randrange);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("str() / repr() match (integer-valued floats excepted)", () => {
    const d = new Diff();
    for (const c of B.str) {
      d.expect(`str(${JSON.stringify(c.v)})`, pyStr(c.v), c.str);
      d.expect(`repr(${JSON.stringify(c.v)})`, pyRepr(c.v), c.repr);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});
