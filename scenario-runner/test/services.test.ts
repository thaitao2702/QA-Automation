import { describe, expect, it } from "vitest";
import { parseDotenv } from "../src/services/env.js";
import { _walletStepForTests as step } from "../src/services/wallet.js";

describe("wallet step (Decimal quantize, as the Python moved the money)", () => {
  it("rounds a deposit UP and a withdrawal DOWN to the thousandth", () => {
    expect(step(0.1, 15069.237)).toBe(-15069.137);
    expect(step(100, 99.9995)).toBe(0.001);
    expect(step(100, 100.0004)).toBe(0);
    expect(step(11785.03, 111.96)).toBe(11673.07);
    expect(step(0, 0.0759)).toBe(-0.075);
    expect(step(5, 5)).toBe(0);
  });
});

describe("dotenv parsing", () => {
  it("reads quotes, comments, export and interpolation like python-dotenv", () => {
    const env = parseDotenv([
      "# comment", "A=1", "export B = two ", "C='lit ${A}'", 'D="x\ny ${A}"', "E=val # note",
      "F=${A}-${MISSING:-dflt}", "BAD LINE", "G=", 'H="multi', 'line"',
    ].join("\n"), {});
    expect(env).toEqual({ A: "1", B: "two", C: "lit ${A}", D: "x\ny 1", E: "val", F: "1-dflt", G: "", H: "multi\nline" });
  });
});
