/**
 * Turning a suite run's `cases:` into graded cases (port of run_scenario.py `build_cases` and its
 * helpers). Stream asserts are graded now (exact, free); video and image asserts become structured,
 * interpolated specs for the deferred AI pass.
 */
import { get, has, isDict, pyStr, truthy } from "../py.js";
import type { Verdict } from "../engine/invariants.js";
import type { ScenarioResult, Shot } from "../engine/result.js";
import { resolveEach, resolveTokens, resolveWindow, runStreamAssert } from "../engine/tokens.js";

export type Dict = Record<string, any>;

export type CaseVerdict = "inconclusive" | "red" | "pending" | "green" | "orange";

export interface StreamResult {
  spec: unknown;
  result: Verdict;
}

export interface Case {
  id: unknown;
  title: unknown;
  stream: StreamResult[];
  video: Dict[];
  image: Dict[];
  verdict?: CaseVerdict | string;
  [k: string]: unknown;
}

export function computeVerdict(reached: boolean, streamResults: StreamResult[], hasAi: boolean): CaseVerdict {
  if (!reached) return "inconclusive";
  if (streamResults.some((r) => !r.result.valid)) return "red";
  if (hasAi) return "pending";         // AI video/image asserts deferred to the AI pass
  return "green";
}

function interpolator(result: ScenarioResult): (s: unknown) => string {
  return (s) => (result.reached ? resolveTokens(pyStr(s), result) : pyStr(s));
}

function expectedOf(d: Dict, rt: (s: unknown) => string): Dict {
  const out: Dict = {};
  for (const [k, v] of Object.entries((get(d, "expected") || {}) as Dict)) out[k] = typeof v === "string" ? rt(v) : v;
  return out;
}

/** Turn a `video:` assert into a structured, interpolated spec for the deferred AI pass: kind
 * check|read|schema, prompt/read/expected with `{res.*}` bound in, and the `at:` sub-clip window. */
export function videoSpec(d: unknown, result: ScenarioResult): Dict {
  const rt = interpolator(result);
  const dd = isDict(d) ? (d as Dict) : null;
  const at = dd ? get(dd, "at") : null;
  const each = dd ? get(dd, "each") : null;
  const spec: Dict = {
    at,
    // Which reference pictures prime this call. Unset -> the game's whole manifest.
    asset_groups: dd ? get(dd, "asset_groups") : null,
    assets: dd ? get(dd, "assets") : null,
    window: result.reached && !truthy(each) ? resolveWindow(result, at) : null,
    status: "pending",
  };
  if (truthy(each)) {
    // `each: <list mark>` -> ONE CLIP PER ROUND, each graded on its own; the assert passes only if
    // every clip passes.
    spec.each = each;
    let windows: unknown[] = result.reached ? resolveEach(result, each) : [];
    // `max_rounds: N` caps the AI calls, spread EVENLY over the list so every bonus stays represented.
    const cap = get(dd, "max_rounds");
    if (typeof cap === "number" && Number.isInteger(cap) && cap > 0 && windows.length > cap) {
      spec.rounds_available = windows.length;
      const step = windows.length / cap;
      windows = Array.from({ length: cap }, (_, k) => windows[Math.trunc(k * step)]);
    }
    spec.windows = windows;
  }
  if (dd) {                                             // video sampling modes (passed to the provider)
    if (truthy(get(dd, "high_fps"))) spec.high_fps = true;
    if (truthy(get(dd, "timestamp"))) spec.timestamp = true;
  }
  if (typeof d === "string") {
    Object.assign(spec, { kind: "check", check: rt(d) });
  } else if (dd && (has(dd, "prompt") || has(dd, "output_schema") || has(dd, "expected"))) {
    // raw.yml-style explicit form: full prompt + output_schema + expected field map
    Object.assign(spec, {
      kind: "schema",
      prompt: rt(has(dd, "prompt") ? dd.prompt : ""),
      output_schema: has(dd, "output_schema") ? dd.output_schema : {},
      expected: expectedOf(dd, rt),
    });
  } else if (dd && has(dd, "check")) {
    Object.assign(spec, { kind: "check", check: rt(dd.check) });
  } else {
    if (!dd) {
      const e = new Error(`'${Array.isArray(d) ? "list" : typeof d}' object has no attribute 'get'`);
      e.name = "AttributeError";
      throw e;
    }
    Object.assign(spec, { kind: "read", read: rt(has(dd, "read") ? dd.read : "") });
    if (has(dd, "equals")) spec.equals = rt(dd.equals);
    if (has(dd, "near")) {
      const near = dd.near as Dict;
      spec.near = { value: rt(has(near, "value") ? near.value : ""), tol: has(near, "tol") ? near.tol : 0 };
    }
  }
  return spec;
}

/** Which captured stills an `image:` assert covers: null/'all' (every shot) or a case-insensitive
 * substring of the shot label ('AFTER', 'settings', 'my '). */
export function shotsFor(result: ScenarioResult, at: unknown): Shot[] {
  const shots = result.shots ?? [];
  if (at === null || at === undefined || at === "" || at === "all") return [...shots];
  const key = pyStr(at).trim().toLowerCase();
  return shots.filter((s) => s.label.toLowerCase().includes(key));
}

/** Turn an `image:` assert into a structured spec. One AI call PER CAPTURED SHOT — each surface is
 * judged on its own full-resolution still — and the assert passes only if every shot passes. */
export function imageSpec(d: unknown, result: ScenarioResult): Dict {
  const rt = interpolator(result);
  const dd = isDict(d) ? (d as Dict) : null;
  const at = dd ? get(dd, "at") : null;
  const spec: Dict = {
    at,
    status: "pending",
    kind: "schema",
    // `combine: true` -> every still goes to the AI in ONE call, as pages of one scrolling surface.
    combine: !!(dd && truthy(get(dd, "combine"))),
    shots: shotsFor(result, at).map((s) => ({ label: s.label, path: s.path, file: s.file })),
  };
  if (typeof d === "string") {
    Object.assign(spec, { prompt: rt(d), output_schema: {}, expected: {} });
  } else {
    if (!dd) {
      const e = new Error(`'${Array.isArray(d) ? "list" : typeof d}' object has no attribute 'get'`);
      e.name = "AttributeError";
      throw e;
    }
    const src = dd;
    Object.assign(spec, {
      prompt: rt(has(src, "prompt") ? src.prompt : ""),
      output_schema: has(src, "output_schema") ? src.output_schema : {},
      expected: expectedOf(src, rt),
    });
    // `compare_shots: {field, ratio, tol}` -- a figure that must SCALE between the two shots
    // (ai-video compareShots). `ratio` is usually a fact, so it is token-resolved.
    const cs = get(src, "compare_shots");
    if (isDict(cs)) {
      spec.compare_shots = {
        field: get(cs as Dict, "field"),
        ratio: rt(has(cs as Dict, "ratio") ? (cs as Dict).ratio : ""),
        tol: has(cs as Dict, "tol") ? (cs as Dict).tol : 0.02,
      };
    }
  }
  return spec;
}

/** Number of ACTUAL AI calls for a run's cases. Video specs sharing a `group` collapse to one call;
 * an `each:` spec is one call per round; IMAGE specs cost one call PER SHOT, deduped by (group, shot). */
export function aiCalls(cases: Case[]): number {
  const seen = new Set<string>();
  let n = 0;
  for (const c of cases) {
    for (const spec of c.video ?? []) {
      const g = get(spec, "group");
      if (g !== null) {
        const key = `v:${pyStr(g)}`;
        if (seen.has(key)) continue;
        seen.add(key);
      }
      n += has(spec, "each") ? ((spec.windows as unknown[] | undefined)?.length ?? 0) : 1;
    }
    (c.image ?? []).forEach((spec, i) => {
      const g = get(spec, "group") ?? `${pyStr(c.id)}-img${i}`;
      if (truthy(get(spec, "combine"))) {          // every still in one call, so it costs one
        const key = `i:${pyStr(g)}|__combined__`;
        if (!seen.has(key)) {
          seen.add(key);
          n += 1;
        }
        return;
      }
      for (const shot of (spec.shots ?? []) as Shot[]) {
        const key = `i:${pyStr(g)}|${shot.file}`;
        if (seen.has(key)) continue;
        seen.add(key);
        n += 1;
      }
    });
  }
  return n;
}

let sharedCounter = 0;
const sharedIds = new WeakMap<object, number>();

/** A stable id per shared block object — Python used `id(shared)`. */
function sharedId(block: object): number {
  let id = sharedIds.get(block);
  if (id === undefined) {
    sharedCounter += 1;
    id = sharedCounter;
    sharedIds.set(block, id);
  }
  return id;
}

/**
 * Stream asserts run now (Hard); video/image asserts are structured for the deferred AI pass.
 *
 * A run may also carry a SHARED `video:` block (one) and/or `videos:` (a list), each with
 * `expected_by_case`: ONE AI call over one clip, graded against several cases' expected subsets. It
 * expands into a per-case spec sharing a `group` key, so the clip is analysed once and reused.
 */
export function buildCases(runDoc: Dict, result: ScenarioResult): Case[] {
  const cases: Case[] = [];
  const caseMap = new Map<string, Case>();
  const key = (id: unknown) => pyStr(id);
  for (const kase of (get(runDoc, "cases") || []) as Dict[]) {
    const streamResults: StreamResult[] = [];
    const videoSpecs: Dict[] = [];
    const imageSpecs: Dict[] = [];
    for (const a of (get(kase, "assert") || []) as Dict[]) {
      if (has(a, "stream")) {
        const r = result.reached ? runStreamAssert(a.stream, result) : { valid: false, violations: ["scenario unreached"] };
        streamResults.push({ spec: a.stream, result: r });
      } else if (has(a, "video")) {
        videoSpecs.push(videoSpec(a.video, result));
      } else if (has(a, "image")) {
        imageSpecs.push(imageSpec(a.image, result));
      }
    }
    const c: Case = { id: get(kase, "id"), title: has(kase, "title") ? kase.title : "", stream: streamResults, video: videoSpecs, image: imageSpecs };
    cases.push(c);
    caseMap.set(key(c.id), c);
  }

  const bindExpected = (exp: unknown): Dict => {
    const out: Dict = {};
    for (const [k, v] of Object.entries((exp || {}) as Dict)) {
      out[k] = typeof v === "string" && result.reached ? resolveTokens(v, result) : v;
    }
    return out;
  };

  const blocks = [get(runDoc, "video"), ...((get(runDoc, "videos") || []) as unknown[])];
  for (const shared of blocks) {
    if (!(isDict(shared) && truthy(get(shared, "expected_by_case")))) continue;
    const { expected_by_case: byCase, ...rest } = shared as Dict;
    const base = videoSpec(rest, result);
    const group = `shared-${sharedId(shared)}`;
    for (const [cid, exp] of Object.entries(byCase as Dict)) {
      const c = caseMap.get(key(cid));
      if (!c) continue;
      c.video.push({ ...base, group, expected: bindExpected(exp) });
    }
  }

  const sharedImg = get(runDoc, "image");
  if (isDict(sharedImg) && truthy(get(sharedImg, "expected_by_case"))) {
    const { expected_by_case: byCase, ...rest } = sharedImg as Dict;
    const base = imageSpec(rest, result);
    const group = `shared-img-${sharedId(sharedImg)}`;
    for (const [cid, exp] of Object.entries(byCase as Dict)) {
      const c = caseMap.get(key(cid));
      if (!c) continue;
      c.image.push({ ...base, group, expected: bindExpected(exp) });
    }
  }

  for (const c of cases) c.verdict = computeVerdict(result.reached, c.stream, c.video.length > 0 || c.image.length > 0);
  return cases;
}

