/**
 * Compare two scenario-runner result folders (one written by the Python runner, one by the TS
 * runner) for the same suite: per run, result.json (reached, reason, facts, marks, cases with their
 * stream results and verdicts), events.jsonl (the round rows) and timeline.json (the sequence of
 * steps), plus summary.json. Timing, clip windows and absolute paths differ by nature and are
 * dropped; Python's `7.0` prints as `7` here (see PORTING.md).
 *
 *   npx tsx test/fake-game/compare.ts <python-result-dir> <ts-result-dir>
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { basename, join } from "node:path";

const TIMING = new Set(["t", "start_ms", "end_ms", "video_window", "window", "windows", "round_windows",
  "duration_ms", "base_ready_seconds", "auto_flag_cleared_s", "path", "group"]);
const TIMING_SUFFIXES = ["_seconds", "_s", "_ms"];

function norm(v: unknown, key = ""): unknown {
  if (TIMING.has(key)) return "<timing>";
  // A measured duration (`*_close_seconds`, `offline_s`) is wall-clock, not logic.
  if (typeof v === "number" && TIMING_SUFFIXES.some((s) => key.endsWith(s))) return "<timing>";
  if (typeof v === "string") {
    let s = v.replace(/(\d)\.0(?!\d)/g, "$1");
    s = s.replace(/[A-Za-z]:[\\/][^\s'"]*?[\\/](_shots|_clips)[\\/]/g, "<dir>/$1/");
    s = s.replace(/after \d+(\.\d+)?s/g, "after <n>s");
    return s;
  }
  if (Array.isArray(v)) return v.map((x) => norm(x, key));
  if (v && typeof v === "object") {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(v as object).sort()) out[k] = norm((v as Record<string, unknown>)[k], k);
    return out;
  }
  return v;
}

function readJson(p: string): unknown {
  return existsSync(p) ? JSON.parse(readFileSync(p, "utf-8")) : "<missing>";
}

function readJsonl(p: string): unknown[] {
  if (!existsSync(p)) return ["<missing>"];
  return readFileSync(p, "utf-8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

const diffs: string[] = [];

function diff(where: string, a: unknown, b: unknown): void {
  if (JSON.stringify(a) === JSON.stringify(b)) return;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) diffs.push(`${where}: length py=${a.length} ts=${b.length}`);
    for (let i = 0; i < Math.min(a.length, b.length); i++) diff(`${where}[${i}]`, a[i], b[i]);
    return;
  }
  if (a && b && typeof a === "object" && typeof b === "object") {
    const keys = new Set([...Object.keys(a as object), ...Object.keys(b as object)]);
    for (const k of keys) diff(`${where}.${k}`, (a as any)[k], (b as any)[k]);
    return;
  }
  diffs.push(`${where}\n    py: ${JSON.stringify(a)}\n    ts: ${JSON.stringify(b)}`);
}

/** The timeline as the sequence of what happened (ids carry counters, descriptions carry text). */
function steps(timeline: unknown): unknown {
  if (!Array.isArray(timeline)) return timeline;
  return timeline.map((s: any) => norm({ id: s.id, action: s.action, description: s.description, result: s.result }));
}

const [py, ts] = process.argv.slice(2);
if (!py || !ts) {
  console.error("usage: compare.ts <python-result-dir> <ts-result-dir>");
  process.exit(2);
}
const runsOf = (root: string) => readdirSync(root).filter((d) => statSync(join(root, d)).isDirectory() && !d.startsWith("_")).sort();
diff("runs", runsOf(py), runsOf(ts));
for (const run of runsOf(py)) {
  if (!existsSync(join(ts, run))) continue;
  const rp = norm(readJson(join(py, run, "result.json")));
  const rt = norm(readJson(join(ts, run, "result.json")));
  diff(`${run}/result.json`, rp, rt);
  diff(`${run}/events.jsonl`, norm(readJsonl(join(py, run, "events.jsonl"))), norm(readJsonl(join(ts, run, "events.jsonl"))));
  diff(`${run}/timeline`, steps(readJson(join(py, run, "timeline.json"))), steps(readJson(join(ts, run, "timeline.json"))));
}
diff("summary.json", norm(readJson(join(py, "summary.json"))), norm(readJson(join(ts, "summary.json"))));
if (diffs.length) {
  console.log(`${diffs.length} difference(s) between ${basename(py)} (py) and ${basename(ts)} (ts):`);
  for (const d of diffs.slice(0, 80)) console.log(`  ${d}`);
  process.exit(1);
}
console.log(`identical (after normalisation): ${runsOf(py).length} runs`);
