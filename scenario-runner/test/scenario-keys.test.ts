/**
 * Every ported scenario exposes the fact names the Python one did, with the same name, description
 * and constructor defaults (baseline/scenario_keys.json, from baseline/scenario_keys.py). A fact
 * key the port lost would make a suite's `{facts.x}` bind to null — the quietest failure there is.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import { BASELINE, loadBaseline } from "./baseline.js";

const PY = loadBaseline<Record<string, { class: string; keys: string[]; description: string | null; params: Record<string, unknown> }>>("scenario_keys.json");
const DIR = join(BASELINE, "..", "src", "scenarios");

interface TsScenario {
  file: string;
  className: string;
  keys: Set<string>;
}

/** name -> the string property keys of every object literal inside that scenario class. */
function scan(): Map<string, TsScenario> {
  const out = new Map<string, TsScenario>();
  for (const file of readdirSync(DIR).filter((f) => f.endsWith(".ts") && f !== "base.ts" && f !== "index.ts")) {
    const sf = ts.createSourceFile(file, readFileSync(join(DIR, file), "utf-8"), ts.ScriptTarget.ES2022, true);
    const visitClass = (cls: ts.ClassDeclaration) => {
      let name: string | null = null;
      for (const m of cls.members) {
        if (ts.isPropertyDeclaration(m) && m.name.getText(sf) === "name" && m.initializer && ts.isStringLiteralLike(m.initializer)) {
          name = m.initializer.text;
        }
      }
      if (!name) return;
      const keys = new Set<string>();
      const walk = (n: ts.Node) => {
        if (ts.isObjectLiteralExpression(n)) {
          for (const p of n.properties) {
            if ((ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p)) && p.name) {
              if (ts.isIdentifier(p.name) || ts.isStringLiteralLike(p.name)) keys.add(p.name.text);
            }
          }
        }
        ts.forEachChild(n, walk);
      };
      walk(cls);
      out.set(name, { file, className: cls.name?.text ?? "?", keys });
    };
    ts.forEachChild(sf, (n) => {
      if (ts.isClassDeclaration(n)) visitClass(n);
    });
  }
  return out;
}

describe("scenario contract", () => {
  it("the registry has exactly the Python scenarios, with the same descriptions and params", async () => {
    const { REGISTRY } = await import("../src/scenarios/index.js");
    expect([...REGISTRY.keys()].sort()).toEqual(Object.keys(PY).sort());
    const bad: string[] = [];
    for (const [name, py] of Object.entries(PY)) {
      const Cls = REGISTRY.get(name)!;
      const inst = new Cls();
      if (inst.description !== py.description) bad.push(`${name}: description differs`);
      const want = Object.keys(py.params).filter((k) => !k.startsWith("*")).sort();
      const got = [...Cls.PARAMS].sort();
      const takesKw = Object.keys(py.params).some((k) => k.startsWith("**"));
      const ok = takesKw ? want.every((k) => got.includes(k)) : JSON.stringify(got) === JSON.stringify(want);
      if (!ok) bad.push(`${name}: PARAMS ${JSON.stringify(got)} != python ${JSON.stringify(want)}`);
    }
    expect(bad).toEqual([]);
  });
});

describe("scenario fact names", () => {
  const ported = scan();
  for (const [name, py] of Object.entries(PY)) {
    const t = ported.get(name);
    it.skipIf(!t)(`${name} (${py.class})`, () => {
      const missing = py.keys.filter((k) => !t!.keys.has(k));
      expect(missing, `${name} in ${t!.file}: Python dict keys absent from the TS class`).toEqual([]);
    });
  }
});
