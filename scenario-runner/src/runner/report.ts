/**
 * The run's human outputs: per-run replay.html, the session replay, and report.html (port of
 * `write_replay`, `write_session_replay` and `write_report` in run_scenario.py). The HTML templates
 * are the Python runner's own, so both runners produce the same pages.
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ScenarioResult } from "../engine/result.js";
import { isDict, pyJson, pyRound, pyStr, PyTuple } from "../py.js";
import { REPO_ROOT } from "../services/env.js";
import type { Case } from "./cases.js";

export const REPLAY_TEMPLATE = join(REPO_ROOT, "framework", "templates", "replay.html");
export const REPORT_TEMPLATE = join(REPO_ROOT, "scripts", "report_template.html");

export interface Section {
  name: string;
  start_s: number;
  end_s: number | null;
  verdict: string;
}

export interface Processed {
  name: string;
  error?: string | null;
  run_dir?: string;
  result?: ScenarioResult;
  cases?: Case[];
  description?: string;
  duration_ms?: number;
  section?: Section;
  api_events?: Array<Record<string, any>>;
  verdict?: string;
}

/** Python's `html.escape(str(s))` (quote=True). */
export function esc(s: unknown): string {
  return displayValue(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}

/** `str(v)` as the Python report printed it: a clip window was a TUPLE there, `(1.5, 4.5)`. */
function displayValue(v: unknown): string {
  if (Array.isArray(v) && v.length === 2 && v.every((x) => typeof x === "number")) return pyStr(new PyTuple(v));
  return pyStr(v);
}

const BADGE: Readonly<Record<string, string>> = {
  green: "#2e7d32", orange: "#ef6c00", red: "#c62828", pending: "#616161", inconclusive: "#6a1b9a", error: "#b71c1c",
};

function badge(v: unknown): string {
  const s = pyStr(v);
  let tone = ({ pass: "green", fail: "red" } as Record<string, string>)[s] ?? s;
  tone = tone in BADGE ? tone : "pending";
  const label = ({ green: "Passed", red: "Failed", orange: "Partly Failed" } as Record<string, string>)[s] ?? s;
  return `<span class="badge ${tone}">${esc(label)}</span>`;
}

/** `json.dumps(v)` with Python's spacing — the report shows answers this way. */
const dumps = (v: unknown): string => pyJson(v ?? null);

/** The run verdict from its cases. */
export function runVerdict(reached: boolean, cases: Case[]): string {
  if (!reached) return "inconclusive";
  if (cases.some((c) => c.verdict === "red")) return "red";
  if (cases.some((c) => c.verdict === "orange")) return "orange";
  if (cases.some((c) => c.verdict === "pending")) return "pending";
  return "green";
}

/** JSON-safe copy for files (Python wrote with `default=str`). */
export function jsonable(v: unknown): unknown {
  if (v instanceof PyTuple) return v.items.map(jsonable);
  if (v instanceof Set) return [...v].map(jsonable);
  if (typeof v === "bigint") return Number(v);
  if (v === undefined) return null;
  if (Array.isArray(v)) return v.map(jsonable);
  if (isDict(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, jsonable(x)]));
  if (v !== null && typeof v === "object") return pyStr(v);
  return v;
}

/** `sections` = every run's span in the shared session video (the seek bar's chapters); `section`
 * = this run's own, which the replay opens on and highlights. */
export function writeReplay(runDir: string, runId: string, title: string, verdict: string, result: ScenarioResult,
  durationMs: number, sections: Section[] = [], section: Section | null = null, apiEvents: Array<Record<string, any>> = []): void {
  if (!existsSync(REPLAY_TEMPLATE)) return;
  const events = [...(result.events as Array<Record<string, any>>), ...apiEvents]
    .map((e, i) => [e, i] as const)
    .sort((a, b) => (Number(a[0].t ?? 0) - Number(b[0].t ?? 0)) || a[1] - b[1])
    .map(([e]) => e);
  const replayData = {
    test_id: runId, title, final_verdict: verdict, bridge_result: verdict, ai_result: "pending",
    duration_ms: durationMs, error: result.reason, video_file: "../session.webm", video_event_offset: 0,
    steps: result.timeline, events, sections,
    current_section: sections.length && section && sections.includes(section) ? sections.indexOf(section) : null,
  };
  const html = readFileSync(REPLAY_TEMPLATE, "utf-8").replace("%%REPLAY_DATA%%", () => pyJson(jsonable(replayData)));
  writeFileSync(join(runDir, "replay.html"), html, "utf-8");
}

/** ONE replay of the whole session at the run root: every run's steps and events on the one
 * timeline, tagged with their run (round numbers restart in every run). False when nothing to show. */
export function writeSessionReplay(root: string, suite: string, processed: Processed[], sections: Section[]): boolean {
  if (!existsSync(REPLAY_TEMPLATE) || !existsSync(join(root, "session.webm"))) return false;
  const runs = processed.filter((p) => p.run_dir);
  if (!runs.length) return false;
  const steps: Array<Record<string, any>> = [];
  const events: Array<Record<string, any>> = [];
  runs.forEach((p, k0) => {
    const k = k0 + 1;
    for (const s of p.result!.timeline as unknown as Array<Record<string, any>>) {
      steps.push({ ...s, id: `${k}·${s.id}`, description: `${p.name}: ${s.description || s.action}` });
    }
    for (const e of [...(p.result!.events as Array<Record<string, any>>), ...(p.api_events ?? [])]) {
      const data = e.data;
      events.push({ ...e, data: isDict(data) ? { run: p.name, ...data } : data });
    }
  });
  const order = ["red", "error", "inconclusive", "orange", "pending", "green"];
  const rank = (v: string) => (order.includes(v) ? order.indexOf(v) : order.length);
  const verdict = runs.map((p) => p.verdict || "pending").reduce((a, b) => (rank(b) < rank(a) ? b : a));
  const t0 = sections.length ? sections[0].start_s : 0;
  const last = sections[sections.length - 1];
  const t1 = sections.length && last.end_s ? last.end_s : t0;
  const stable = <T extends Record<string, any>>(xs: T[], key: string) => xs.map((x, i) => [x, i] as const)
    .sort((a, b) => (Number(a[0][key] ?? 0) - Number(b[0][key] ?? 0)) || a[1] - b[1]).map(([x]) => x);
  const replayData = {
    test_id: suite, title: `${suite} · full session`, final_verdict: verdict, bridge_result: verdict, ai_result: "—",
    duration_ms: Math.trunc((t1 - t0) * 1000), error: null, video_file: "session.webm", video_event_offset: 0,
    steps: stable(steps, "start_ms"), events: stable(events, "t"), sections, current_section: null,
  };
  const html = readFileSync(REPLAY_TEMPLATE, "utf-8").replace("%%REPLAY_DATA%%", () => pyJson(jsonable(replayData)));
  writeFileSync(join(root, "replay.html"), html, "utf-8");
  return true;
}

/** Human-readable report.html for the whole run: every case, its verdict, the stream results, each
 * video assert's prompt + the AI's answer + clip window, and each image assert per shot. */
export function writeReport(root: string, suite: string, processed: Processed[], aiCost: Record<string, any>): string {
  const counts = new Map<string, number>();
  const sections: string[] = [];
  for (const p of processed) {
    if (!p.run_dir) {
      sections.push(`<section class="scenario" data-status="error"><h2>${esc(p.name)} ${badge("error")}</h2>`
        + `<div class="mm">${esc(p.error ?? null)}</div></section>`);
      continue;
    }
    const name = p.name;
    const cases = p.cases ?? [];
    const runV = runVerdict(p.result!.reached, cases);
    const caseHtml: string[] = [];
    // The clip each AI video assert was graded on: the montage first (what the model received),
    // the raw clip when no montage was built. Cases sharing one AI call reuse the first's file.
    const clipsDir = join(p.run_dir, "_clips");
    const groupClip = new Map<string, string>();
    const aiClip = (caseId: unknown, i: number, group: unknown): string | null => {
      for (const fn of [`${pyStr(caseId)}-${i}-montage.mp4`, `${pyStr(caseId)}-${i}.webm`]) {
        if (existsSync(join(clipsDir, fn))) {
          if (group !== null && group !== undefined && !groupClip.has(String(group))) groupClip.set(String(group), fn);
          return fn;
        }
      }
      return group !== null && group !== undefined ? (groupClip.get(String(group)) ?? null) : null;
    };
    const clipsSeen: Array<[string, unknown]> = [];
    for (const c of cases) {
      counts.set(String(c.verdict), (counts.get(String(c.verdict)) ?? 0) + 1);
      (c.video ?? []).forEach((v, i) => {
        const fn = aiClip(c.id, i, v.group ?? null);
        if (fn && !clipsSeen.some(([f]) => f === fn)) clipsSeen.push([fn, c.id]);
      });
      const parts = [`<summary><span class="case-title">${esc(c.id)} — ${esc(c.title ?? "")}</span> ${badge(c.verdict)}`
        + '<span class="chev" aria-hidden="true"></span></summary>'];
      for (const s of c.stream) {
        const ok = s.result.valid;
        const viol = ok ? "" : ` · ${esc(s.result.violations.join("; "))}`;
        parts.push(`<div class="a ${ok ? "ok" : "bad"}">stream · <code>${esc(s.spec)}</code> → ${ok ? "pass" : "FAIL"}${viol}</div>`);
      }
      for (const v of c.video) {
        if ("each" in v) {
          const rows: Array<Record<string, any>> = v.clip_results || [];
          const nOk = rows.filter((r) => r.status === "pass").length;
          const clipLink = (r: Record<string, any>): string => {
            const raw = String(r.clip || "");
            for (const fn of [raw.replace(".webm", "-montage.mp4"), raw]) {
              if (fn && existsSync(join(clipsDir, fn))) return ` <a href="${esc(name)}/_clips/${esc(fn)}" target="_blank">▶ clip</a>`;
            }
            return "";
          };
          const rowHtml = rows.map((r) => `<div class="ans">round ${esc(r.seq ?? null)} ${badge(r.status ?? "?")} `
            + `clip ${esc(r.window ?? null)}${clipLink(r)} — AI answered: <code>${esc(dumps(r.data))}</code>`
            + (r.status !== "pass" && r.note ? `<div class="mm">${esc(r.note)}</div>` : "")
            + "</div>").join("");
          parts.push('<div class="a v">'
            + `video[each: ${esc(v.each)}] ${badge(v.status ?? "?")} ${nOk}/${rows.length} clips`
            + (v.rounds_available ? ` (sampled from ${esc(v.rounds_available)} rounds, max_rounds=${(v.windows || []).length})` : "")
            + `<div class="prompt">${esc(v.prompt ?? null)}</div>`
            + (truthyish(v.expected) ? `<div class="ans">expected: <code>${esc(dumps(v.expected))}</code></div>` : "")
            + (v.status !== "pass" && v.note ? `<div class="mm">${esc(v.note)}</div>` : "")
            + rowHtml + "</div>");
          continue;
        }
        const answer = v.kind === "read" ? v.read : v.data;
        parts.push('<div class="a v">'
          + `video[${esc(v.at || "all")}] ${badge(v.status ?? "?")} clip ${esc(v.window ?? null)}`
          + `<div class="prompt">${esc(v.prompt || v.check || v.read || null)}</div>`
          + `<div class="ans">AI answered: <code>${esc(dumps(answer))}</code></div>`
          + (truthyish(v.expected) ? `<div class="ans">expected: <code>${esc(dumps(v.expected))}</code></div>` : "")
          + (truthyish(v.mismatches) ? `<div class="mm">mismatches: ${esc((v.mismatches || []).join("; "))}</div>` : "")
          + (v.reason ? `<div class="why">why: ${esc(v.reason)}</div>` : "")
          + "</div>");
      }
      for (const v of c.image ?? []) {
        const shots: Array<Record<string, any>> = v.shots || [];
        const results: Array<Record<string, any>> = v.shot_results || [];
        const paired = results.map((res, i) => [res, res.file || (i < shots.length ? shots[i].file : null)] as const);
        const nOk = results.filter((x) => x.status === "pass").length;
        const badN = results.length - nOk;
        const head = `image · ${results.length || shots.length} shot(s) · ${nOk} passed, ${badN} failed`;
        const rows = paired.map(([res, fname]) => {
          const ok = res.status === "pass";
          const why = String(res.note || "");
          const files: string[] = res.files || (fname ? [fname] : []);
          return `<details class="shot ${ok ? "ok" : "bad"}"${ok ? "" : " open"}>`
            + `<summary>${badge(res.status ?? "?")} <b>${esc(res.shot ?? "?")}</b>`
            + (!ok && why ? ` — ${esc(why.slice(0, 160))}` : "")
            + "</summary>"
            + (res.data !== null && res.data !== undefined ? `<div class="ans">AI answered: <code>${esc(dumps(res.data))}</code></div>` : "")
            + (truthyish(res.mismatches) ? `<div class="mm">mismatches: ${esc((res.mismatches || []).join("; "))}</div>` : "")
            + (res.reason ? `<div class="why">why: ${esc(res.reason)}</div>` : "")
            + files.map((fn) => `<a href="${esc(`${name}/_shots/${fn}`)}" target="_blank">`
              + `<img class="shotimg" src="${esc(`${name}/_shots/${fn}`)}" loading="lazy"></a>`).join("")
            + "</details>";
        });
        parts.push('<div class="a i">'
          + `<details${v.status === "pass" ? "" : " open"}>`
          + `<summary>${badge(v.status ?? "?")} ${esc(head)}</summary>`
          + `<div class="prompt">${esc(v.prompt || "")}</div>`
          + (truthyish(v.expected) ? `<div class="ans">expected: <code>${esc(dumps(v.expected))}</code></div>` : "")
          + (v.status !== "pass" && v.note ? `<div class="mm">${esc(v.note)}</div>` : "")
          + `<div class="shots">${rows.join("")}</div>`
          + "</details></div>");
      }
      caseHtml.push(`<div class="case" data-status="${esc(c.verdict)}"><details class="case-body">${parts.join("")}</details></div>`);
    }
    const aiLinks = clipsSeen.map(([fn, cid]) => `<a class="ai" href="${esc(`${name}/_clips/${fn}`)}" target="_blank" `
      + 'title="The clip the AI graded">'
      + `${esc(`▶ AI video${clipsSeen.length > 1 ? ` · ${pyStr(cid)}` : ""}`)}</a>`).join("");
    sections.push(`<section class="scenario" data-status="${runV}"><h2>${esc(name)} ${badge(runV)}</h2>`
      + caseHtml.join("")
      + `<footer class="artifacts"><a href="${esc(name)}/replay.html">▶ Watch replay</a>`
      + `${aiLinks}<a href="${esc(name)}/result.json">View result JSON ↗</a></footer></section>`);
  }
  const tally = [...counts.entries()].map(([k, n]) => `${badge(k)}×${n}`).join(" ") || "no cases";
  const cost = truthyish(aiCost)
    ? `${aiCost.gemini_video_calls ?? 0} AI call(s), ${(aiCost.gemini_input_tokens ?? 0) + (aiCost.gemini_output_tokens ?? 0)} tokens, $${pyStr(aiCost.estimated_cost_usd ?? 0)}`
    : "AI not run";
  const total = [...counts.values()].reduce((a, b) => a + b, 0);
  const errors = processed.filter((p) => !p.run_dir).length;
  const passed = counts.get("green") ?? 0;
  const rate = total ? pyRound((100 * passed) / total) : 0;
  const metrics = ([
    ["Test cases", total, `Across ${processed.length} scenarios`],
    ["Passed", passed, total ? `${rate}% of all cases` : "No cases recorded"],
    ["Needs attention", (counts.get("red") ?? 0) + (counts.get("orange") ?? 0), "Failed or review required"],
    ["Run errors", errors, "Scenario execution errors"],
  ] as const).map(([label, value, hint]) => `<div class="metric"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>`).join("");
  const template = readFileSync(REPORT_TEMPLATE, "utf-8");
  const session = existsSync(join(root, "replay.html"))
    ? '<a class="session-replay" href="replay.html" title="Every run of this suite on one timeline">▶ Watch full session replay</a>'
    : "";
  const replacements: Record<string, string> = {
    SUITE: esc(suite), METRICS: metrics, TALLY: tally, SESSION: session, COST: esc(cost),
    SECTIONS: sections.join(""), COUNT: String(processed.length),
  };
  // A single substitution keeps placeholder-like user content literal.
  const doc = template.replace(/%%([A-Z]+)%%/g, (_m, k: string) => {
    if (!(k in replacements)) throw new Error(`KeyError: '${k}'`);
    return replacements[k];
  });
  const out = join(root, "report.html");
  writeFileSync(out, doc, "utf-8");
  return out;
}

/** Python truthiness for a report field (`if v.get("expected")`). */
function truthyish(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v as object).length > 0;
  return true;
}
