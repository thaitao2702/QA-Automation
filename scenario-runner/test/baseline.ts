/**
 * Shared helpers for the baseline tests: load what Python answered, and compare a TypeScript answer
 * with it the way the generator recorded it (baseline/make_baseline.py `plain` + `call`).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { CellSet } from "../src/profile/mechanisms.js";
import { isDict } from "../src/py.js";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = join(HERE, "..", "..");
export const BASELINE = join(HERE, "..", "baseline");

export function loadBaseline<T = any>(name: string): T {
  return JSON.parse(readFileSync(join(BASELINE, name), "utf-8")) as T;
}

/** Python `json.dumps(x, sort_keys=True)` for the sort key `plain()` orders sets by. */
function pyJsonKey(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(v);
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(pyJsonKey).join(", ")}]`;
  const o = v as Record<string, unknown>;
  return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}: ${pyJsonKey(o[k])}`).join(", ")}}`;
}

/** The TS value reduced to what `plain()` would have written for the Python one. */
export function plain(v: unknown): unknown {
  if (v === undefined) return null;
  if (typeof v === "number" && !Number.isFinite(v)) {
    return { __float__: Number.isNaN(v) ? "nan" : v > 0 ? "inf" : "-inf" };
  }
  if (v instanceof CellSet) return v.sorted().map((c) => [...c]);
  if (v instanceof Set) {
    return [...v].map(plain).sort((a, b) => (pyJsonKey(a) < pyJsonKey(b) ? -1 : pyJsonKey(a) > pyJsonKey(b) ? 1 : 0));
  }
  if (Array.isArray(v)) return v.map(plain);
  if (isDict(v)) {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v)) out[k] = plain(x);
    return out;
  }
  return v;
}

export type Recorded = { ok: unknown } | { err: string };

/** Run `fn` and record it the way `call()` does. */
export function call(fn: () => unknown): Recorded {
  try {
    return { ok: plain(fn()) };
  } catch (e) {
    return { err: (e as Error)?.name ?? "Error" };
  }
}

/** Deep equality on recorded JSON: exact numbers (0 == -0), same keys, same order for lists. */
export function same(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a === "number" && typeof b === "number") return a === b;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (isDict(a) && isDict(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]));
  }
  return false;
}

/** `free_spins` -> `freeSpins`, `_pending_count` -> `_pendingCount`. */
export function camel(name: string): string {
  return name.replace(/(?<!^)_([a-z0-9])/g, (_m, c: string) => c.toUpperCase());
}

/** Collect mismatches instead of failing on the first, so one run shows the whole gap. */
export class Diff {
  readonly lines: string[] = [];
  checked = 0;

  expect(where: string, got: unknown, want: unknown): void {
    this.checked += 1;
    if (!same(got, want)) {
      const g = JSON.stringify(got);
      const w = JSON.stringify(want);
      this.lines.push(`${where}\n    ts: ${g.length > 300 ? `${g.slice(0, 300)}…` : g}\n    py: ${w.length > 300 ? `${w.slice(0, 300)}…` : w}`);
    }
  }

  report(limit = 40): string {
    return `${this.lines.length} of ${this.checked} differ\n${this.lines.slice(0, limit).join("\n")}`;
  }
}
