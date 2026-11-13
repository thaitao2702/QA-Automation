/**
 * AI video-assert execution for the scenario runner (port of scripts/ai_video.py).
 *
 * For each pending `video:` assert on a case: trim `session.webm` to the assert's sub-clip window,
 * run the configured AI provider over it, and grade — never producing `red` (AI is probabilistic; a
 * mismatch is `orange` for a human, the API holds the truth).
 *
 * Reuses the framework's interface-compatible video helpers (Gemini / Claude / local), selected by
 * AI_PROVIDER. The helper builds its own montage; we only trim the window first.
 *
 * Two assert forms (produced by `buildCases`):
 *   check:  {kind:"check", check:"<claim>"}                 -> AI returns passed + confidence
 *   read:   {kind:"read",  read:"<label>", equals|near}     -> AI extracts the value; WE compare
 * plus the raw.yml `schema` form (prompt + output_schema + expected field map).
 *
 * Every result dict keeps the Python's field names (`status`, `note`, `data`, `mismatches`,
 * `reason`, `shot_results`, `clip_results`, `tokens`, ...): result.json and the report read them.
 */
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { get, has, isDict, or, pyFloatRepr, pyInt, pyList, pyRound, truthy, type Dict } from "../../py.js";
import { loadEnvFiles, REPO_ROOT } from "../env.js";
import { ClaudeImageHelper, ClaudeVideoHelper } from "./claude-ai.js";
import { GeminiImageHelper, type GeminiResult } from "./gemini.js";
import { GeminiVideoHelper, type VideoResult } from "./gemini-video.js";
import { LocalImageHelper, LocalVideoHelper } from "./local-ai.js";
import { runProcess } from "./proc.js";
import {
  dget, excStr, excType, jsonLoads, need, pathJoin, pathNorm, pyDict, pyError, pyFixed, pyFloatAny, pyFloatStr, pyMax,
  pyStrip, reprOf, strAdd, strOf, unpack2,
} from "./py-support.js";
import { loadReferenceSelection, loadSymbolReference, type SymbolReference } from "./symbol-reference.js";

/** What `runAi` needs from a video provider (Gemini / Claude / local helper, or a test double). */
export interface VideoProvider {
  analyseVideo(
    videoPath: string, contextId: string | null, prompt: string, outputSchema: Dict,
    opts?: { refImages?: string[] | null; reference?: SymbolReference | null; highFps?: boolean; timestamp?: boolean },
  ): Promise<VideoResult>;
  costSummary?(): Dict;
}

/** What `runAi` needs from an image provider. */
export interface ImageProvider {
  analyseImage(
    imagePath: string, prompt: string, outputSchema: Dict,
    opts?: { extraImages?: string[] | null; reference?: SymbolReference | null },
  ): Promise<GeminiResult>;
  costSummary?(): Dict;
}

export const CHECK_SCHEMA: Dict = {
  passed: { type: "boolean" },
  confidence: { type: "string", enum: ["high", "medium", "low"] },
  reason: { type: "string" },
};
export const READ_SCHEMA: Dict = {
  value: { type: "string" },
  confidence: { type: "string", enum: ["high", "medium", "low"] },
};

const env = (name: string, dflt = ""): string => process.env[name] ?? dflt;

/** Load env + build the configured video AI helper (gemini | claude | local). null if unavailable. */
export function makeVideoProvider(envName: string): (VideoProvider & { costSummary(): Dict }) | null {
  try {
    loadEnvFiles(envName);
  } catch {
    // ignore
  }
  const provider = pyStrip(env("AI_PROVIDER", "gemini")).toLowerCase();
  try {
    if (provider === "claude") return new ClaudeVideoHelper();
    if (provider === "local") {
      const ep = pyStrip(env("LOCAL_AI_ENDPOINT"));
      if (!ep) return null;
      return new LocalVideoHelper(ep);
    }
    const key = pyStrip(env("GEMINI_API_KEY"));
    if (!key) return null;
    return new GeminiVideoHelper(key, env("GEMINI_VIDEO_MODEL") || null, env("GEMINI_VIDEO_RESOLUTION") || null);
  } catch (e) {
    console.log(`  ! AI provider init failed: ${excStr(e)}`);
    return null;
  }
}

/**
 * ffmpeg trim to [start, end] seconds (window null -> whole video). Stream-copy, then fall back to
 * a re-encode if the copy produced nothing (odd keyframes).
 */
export async function trimClip(sessionWebm: string, window: unknown, dest: string): Promise<boolean> {
  if (!existsSync(sessionWebm)) return false;

  const run = async (reencode: boolean): Promise<boolean> => {
    const cmd = ["ffmpeg", "-y"];
    if (truthy(window)) {
      const [start, end] = unpack2(window);
      cmd.push("-ss", pyFixed(pyMax(0.0, pyFloatAny(start)), 3), "-to", pyFixed(pyFloatAny(end), 3));
    }
    cmd.push("-i", pathNorm(sessionWebm), "-an");
    cmd.push(...(reencode ? ["-c:v", "libvpx-vp9", "-b:v", "1M"] : ["-c", "copy"]));
    cmd.push(pathNorm(dest));     // the output path (was missing -> ffmpeg "no output file")
    let r;
    try {
      r = await runProcess(cmd, { timeout: 180 });
    } catch (e) {
      console.log(`  ! ffmpeg trim failed: ${excStr(e)}`);
      return false;
    }
    return r.returncode === 0 && existsSync(dest);
  };

  return (await run(false)) || (await run(true));
}

/** A display amount as a number: `"$1,234.50"` -> 1234.5; null when it is not one. */
export function toNum(x: unknown): number | null {
  try {
    return pyFloatStr(strOf(x).replace(/,/g, "").replace(/ /g, "").replace(/\$/g, ""));
  } catch {
    return null;
  }
}

/** Type-aware equality for raw.yml-style expected fields (bool / number / string). */
export function valuesMatch(actual: unknown, expected: unknown): boolean {
  if (typeof expected === "boolean") return truthy(actual) === expected;
  const an = toNum(actual);
  const en = toNum(expected);
  if (an !== null && en !== null) return Math.abs(an - en) < 1e-6;
  return pyStrip(strOf(actual)).toLowerCase() === pyStrip(strOf(expected)).toLowerCase();
}

/** [matched, detail]. Numeric where possible, else string equality. */
export function compare(got: string, equals: unknown, near: unknown): [boolean, string] {
  if (equals !== null) {
    const gn = toNum(got);
    const en = toNum(equals);
    if (gn !== null && en !== null) return [Math.abs(gn - en) < 1e-6, `read ${reprOf(got)} vs expected ${reprOf(equals)}`];
    return [pyStrip(got) === pyStrip(strOf(equals)), `read ${reprOf(got)} vs expected ${reprOf(equals)}`];
  }
  if (near !== null) {
    const gn = toNum(got);
    const vn = toNum(dget(near, "value"));
    const tol = pyFloatAny(or(dget(near, "tol", 0), 0));
    if (gn !== null && vn !== null) return [Math.abs(gn - vn) <= tol, `read ${reprOf(got)} vs ${reprOf(dget(near, "value"))} +/-${strOf(tol)}`];
    return [false, `non-numeric near-compare: ${reprOf(got)}`];
  }
  return [true, "no expected value to compare"];
}

/** `res.input_tokens` / `getattr(res, "input_tokens", 0)`. */
function tokensOf(res: Dict): [unknown, unknown] {
  return [has(res, "input_tokens") ? res.input_tokens : 0, has(res, "output_tokens") ? res.output_tokens : 0];
}

const REASON_ASK_VIDEO = "\n\nAlso set \"reason\" to a brief prose explanation of what you saw in the "
  + "video and why you answered each field that way — especially explain anything that "
  + "does NOT match what was described.";

/** The prompt and schema one video spec sends (the first half of `runVideoAssert`). */
export function videoPrompt(spec: Dict): [string, Dict] {
  const kind = need(spec, "kind");
  let prompt: unknown;
  let schema: Dict;
  if (kind === "check") {
    prompt = need(spec, "check");
    schema = { ...CHECK_SCHEMA };
  } else if (kind === "read") {
    prompt = `Look at ${strOf(need(spec, "read"))}. Report the EXACT value shown there as text — `
      + "digits and separators exactly as displayed, nothing else.";
    schema = { ...READ_SCHEMA };
  } else {  // schema
    prompt = need(spec, "prompt");
    schema = pyDict(or(get(spec, "output_schema"), {}));
  }

  // always ask the model for a prose explanation of WHY it answered as it did
  if (!has(schema, "reason")) schema.reason = { type: "string" };
  let text = strAdd(prompt, REASON_ASK_VIDEO);
  if (truthy(get(spec, "timeline_hint"))) {
    text = strAdd(text, spec.timeline_hint);        // clip-relative action times for timestamp mode
  }
  return [text, schema];
}

/**
 * Analyse one trimmed clip for one video spec. Returns grade fields to merge into the spec. Status
 * is only ever pass / orange (AI never yields red). Three forms:
 *   check  -> boolean claim         (passed + confidence)
 *   read   -> extract a value; WE compare to equals/near
 *   schema -> raw.yml style: full prompt + output_schema + expected field map.
 */
export async function runVideoAssert(provider: VideoProvider, clip: string, spec: Dict): Promise<Dict> {
  const kind = need(spec, "kind");
  const [prompt, schema] = videoPrompt(spec);

  const res = await provider.analyseVideo(clip, null, prompt, schema, {
    reference: get(spec, "reference") as SymbolReference | null,
    highFps: truthy(get(spec, "high_fps")),
    timestamp: truthy(get(spec, "timestamp")),
  });
  const tokens = tokensOf(res as unknown as Dict);
  if (!truthy(res.success) || !truthy(res.data)) {
    return { status: "orange", confidence: "low", reason: "", note: `AI error: ${strOf(res.reason)}`, tokens };
  }

  const data = res.data;
  const conf = strOf(dget(data, "confidence", "")).toLowerCase();
  const confident = conf === "high" || conf === "medium";
  const reason = pyStrip(strOf(dget(data, "reason", "")));

  const note = (ok: boolean, detail: string): string => {
    // on a miss, lead with the prose reason so the report explains WHY
    if (ok) return detail || reason || "matched";
    return pyStrip(detail + (reason ? ` - ${reason}` : ""), " -");
  };

  if (kind === "check") {
    const passed = truthy(dget(data, "passed"));
    const ok = passed && confident;
    return {
      status: ok ? "pass" : "orange", passed, confidence: conf,
      reason, note: note(ok, passed ? "" : "claim not satisfied"),
      tokens,
    };
  }

  if (kind === "read") {
    const got = pyStrip(strOf(dget(data, "value", "")));
    const [matched, detail] = compare(got, get(spec, "equals"), get(spec, "near"));
    const ok = matched && confident;
    return {
      status: ok ? "pass" : "orange", read: got,
      expected: has(spec, "equals") ? spec.equals : get(spec, "near"), matched,
      confidence: conf, reason, note: note(ok, detail), tokens,
    };
  }

  // schema (raw.yml style): every expected field must match what the model reported
  return gradeSchema(data, get(spec, "expected"), reason, tokens);
}

export const CONF_RANK: Readonly<Record<string, number>> = { low: 0, medium: 1, high: 2 };

const rank = (s: string, dflt: number): number => (Object.prototype.hasOwnProperty.call(CONF_RANK, s) ? CONF_RANK[s] : dflt);

/**
 * Match one expected field. `confidence` is a QUALITY GATE, not an exact value: the model's
 * self-rating passes when it MEETS OR EXCEEDS the expected level (expected medium -> medium or high
 * is fine, only low fails). All other fields use exact type-aware equality.
 */
export function fieldOk(key: string, actual: unknown, expected: unknown): boolean {
  if (key === "confidence") {
    return rank(pyStrip(strOf(actual)).toLowerCase(), -1) >= rank(pyStrip(strOf(expected)).toLowerCase(), 0);
  }
  // A LIST is "any of these": more than one answer is correct (a walking wild kicks a letter out of
  // its cell but throws a cat or dog at the screen — either is a passing kick).
  if (Array.isArray(expected)) return expected.some((e) => valuesMatch(actual, e));
  return valuesMatch(actual, expected);
}

/**
 * Grade an AI `data` object against an `expected` field map (schema form). Split out so a SHARED
 * video call can be graded against several cases' expected subsets without re-calling.
 */
export function gradeSchema(data: unknown, expected: unknown, reason: unknown, tokens: unknown): Dict {
  const exp = or(expected, {});
  if (!isDict(exp)) throw pyError("AttributeError", `'${Array.isArray(exp) ? "list" : typeof exp === "string" ? "str" : "object"}' object has no attribute 'items'`);
  const mismatches: string[] = [];
  for (const [k, v] of Object.entries(exp)) {
    if (!fieldOk(k, dget(data, k), v)) {
      mismatches.push(`${k}: got ${reprOf(dget(data, k))}, expected ${k === "confidence" ? "at least " : ""}${reprOf(v)}`);
    }
  }
  const ok = mismatches.length === 0;
  const why = pyStrip(strOf(or(reason, "")));
  const note = ok ? why || "matched" : pyStrip(mismatches.join("; ") + (why ? ` - ${why}` : ""), " -");
  return { status: ok ? "pass" : "orange", data, mismatches, reason: why, note, tokens };
}

/** After the AI pass, fold video AND image results into the case verdict. Hard tiers win. */
export function recomputeVerdict(kase: Dict): string {
  const verdict = need(kase, "verdict");
  if (verdict === "inconclusive" || verdict === "red") return verdict;   // stream/reach already decided it
  const ai = [...pyList(has(kase, "video") ? kase.video : []), ...pyList(has(kase, "image") ? kase.image : [])];
  if (ai.some((v) => dget(v, "status") === "pending")) return "pending";
  if (ai.some((v) => dget(v, "status") === "orange")) return "orange";
  return "green";
}

/** `x / 1000.0` — a number (or bool) only, as Python's `/` allows. */
function msToS(v: unknown): number {
  if (typeof v === "number") return v / 1000.0;
  if (typeof v === "boolean") return Number(v) / 1000.0;
  throw pyError("TypeError", `unsupported operand type(s) for /: '${v === null ? "NoneType" : typeof v === "string" ? "str" : Array.isArray(v) ? "list" : "dict"}' and 'float'`);
}

const TIMELINE_ACTIONS = ["hold", "hold_key", "dblclick", "click", "key", "net", "scroll", "second_session", "mark"];

/**
 * For a timestamp-mode assert, build a clip-relative ACTION TIMELINE from this run's timeline.json
 * (holds / key-holds / double-clicks / clicks / network events that fall in the clip window) so the
 * model can line the burned-in timestamps up with when we acted.
 */
export function timelineHint(runDir: string, window: unknown): string {
  if (!truthy(window)) return "";
  const w = window as unknown[];
  const ws = pyFloatAny(w[0]);
  const we = pyFloatAny(w[1]);
  let steps: unknown;
  try {
    steps = jsonLoads(readFileSync(pathJoin(runDir, "timeline.json"), "utf-8"));
  } catch {
    return "";
  }
  const notes: string[] = [];
  for (const s of pyList(steps)) {
    const act = dget(s, "action");
    if (!TIMELINE_ACTIONS.includes(act as string)) continue;
    const a = msToS(dget(s, "start_ms", 0));
    const b = msToS(dget(s, "end_ms", dget(s, "start_ms", 0)));
    if (b < ws || a > we) continue;                                   // outside this clip
    const ra = pyMax(0.0, a - ws);
    const rb = pyMax(0.0, b - ws);
    const desc = strOf(dget(s, "description", act));
    notes.push(act === "hold" || act === "hold_key"
      ? `${desc}: pressed at t=${pyFixed(ra, 2)}s, released at t=${pyFixed(rb, 2)}s`
      : `${desc}: at t=${pyFixed(ra, 2)}s`);
  }
  if (!notes.length) return "";
  return "\n\nACTION TIMELINE (clip-relative seconds, matching the burned-in timestamps):\n- " + notes.join("\n- ");
}

/**
 * Build the configured STILL-IMAGE AI helper (gemini | claude | local). null if unavailable.
 * Mirrors makeVideoProvider — same env vars, the image-side helper of the same family.
 */
export function makeImageProvider(envName: string): (ImageProvider & { costSummary(): Dict }) | null {
  try {
    loadEnvFiles(envName);
  } catch {
    // ignore
  }
  const provider = pyStrip(env("AI_PROVIDER", "gemini")).toLowerCase();
  try {
    if (provider === "claude") return new ClaudeImageHelper();
    if (provider === "local") {
      const ep = pyStrip(env("LOCAL_AI_ENDPOINT"));
      if (!ep) return null;
      return new LocalImageHelper(ep);
    }
    const key = pyStrip(env("GEMINI_API_KEY"));
    if (!key) return null;
    return new GeminiImageHelper(key);
  } catch (e) {
    console.log(`  ! AI image provider init failed: ${excStr(e)}`);
    return null;
  }
}

const REASON_ASK_IMAGE = "\n\nAlso set \"reason\" to a brief prose explanation of what you saw in the "
  + "image and why you answered each field that way — especially explain anything that "
  + "does NOT match what was described.";

/** The prompt and schema one image spec sends for one shot (the first half of `runImageAssert`). */
export function imagePrompt(spec: Dict, label = "", extras: string[] | null = null): [string, Dict] {
  const raw = need(spec, "prompt");
  if (typeof raw !== "string") throw pyError("AttributeError", `'${raw === null ? "NoneType" : typeof raw}' object has no attribute 'replace'`);
  let prompt = raw.split("{shot.label}").join(label);
  const schema = pyDict(or(get(spec, "output_schema"), {}));
  if (!has(schema, "reason")) schema.reason = { type: "string" };
  if (truthy(extras)) {
    prompt = `You are given ${extras!.length + 1} screenshots of ONE screen, in order, captured as `
      + `it was scrolled from top to bottom: ${label}. They are consecutive parts of the `
      + "SAME surface, not separate screens. Answer about the surface AS A WHOLE — if "
      + "something appears on any one of them it is present; only say it is absent if it "
      + `is on none of them.\n\n${prompt}`;
  }
  return [prompt + REASON_ASK_IMAGE, schema];
}

/**
 * Analyse a still (or a SET of stills) against one image spec. Same schema-grading contract as the
 * video path (gradeSchema), so an image assert is orange-only too — never red.
 *
 * `{shot.label}` in the prompt is replaced with this shot's label. One assert covers many shots, so
 * this is how a prompt says something different about each — e.g. a language sweep whose shots are
 * labelled "th Thai", "my Burmese", ... tells the model which language THIS screen should be in.
 *
 * `extras` sends further stills alongside the first, in ONE call — for a SCROLLING surface, where
 * the screenfuls are pages of a single document rather than separate surfaces. Asking each page in
 * isolation cannot answer "is there an RTP section?": the honest answer for page 2 is "no" even
 * when page 5 has one, so every page but one reports a failure the surface does not have.
 */
export async function runImageAssert(
  provider: ImageProvider, shot: string, spec: Dict, label = "", extras: string[] | null = null,
): Promise<Dict> {
  const [prompt, schema] = imagePrompt(spec, label, extras);
  const res = await provider.analyseImage(shot, prompt, schema, { extraImages: truthy(extras) ? [...extras!] : null });
  const tokens = tokensOf(res as unknown as Dict);
  if (!truthy(res.success) || !truthy(res.data)) {
    return { status: "orange", confidence: "low", reason: "", note: `AI error: ${strOf(res.reason)}`, tokens };
  }
  return gradeSchema(res.data, get(spec, "expected"), pyStrip(strOf(dget(res.data, "reason", ""))), tokens);
}

/**
 * How many AI calls run at once: `AI_CONCURRENCY`, else 4 — or 1 for a `local` provider, which is
 * usually one model on one GPU and gains nothing from queueing more requests at it.
 */
export function aiWorkers(dflt = 4): number {
  const raw = pyStrip(env("AI_CONCURRENCY"));
  // str.isdigit(): Unicode decimal digits count too, and int() reads them
  if (/^\p{Nd}+$/u.test(raw) && pyFloatStr(raw) > 0) return pyInt(pyFloatStr(raw));
  return pyStrip(env("AI_PROVIDER", "gemini")).toLowerCase() === "local" ? 1 : dflt;
}

// ── the parallel pass ────────────────────────────────────────────────────────────────

/**
 * `ThreadPoolExecutor(max_workers=n)`: calls start in submission order, at most `n` at a time.
 * Every future is pre-handled, so one that fails before it is awaited cannot crash the process
 * (Python keeps the exception in the future until `.result()`).
 */
export class Pool {
  private active = 0;
  private readonly queue: Array<() => void> = [];
  private readonly all: Promise<unknown>[] = [];

  constructor(readonly maxWorkers: number) {
    if (!(maxWorkers > 0)) throw pyError("ValueError", "max_workers must be greater than 0");
  }

  submit<T>(fn: () => Promise<T>): Promise<T> {
    const p = new Promise<T>((resolve, reject) => {
      const start = () => {
        this.active += 1;
        Promise.resolve()
          .then(fn)
          .then(resolve, reject)
          .finally(() => {
            this.active -= 1;
            const next = this.queue.shift();
            if (next) next();
          });
      };
      if (this.active < this.maxWorkers) start();
      else this.queue.push(start);
    });
    p.catch(() => {});
    this.all.push(p);
    return p;
  }

  /** `shutdown(wait=True)`: every submitted call, queued ones included, runs to completion. */
  async shutdown(): Promise<void> {
    await Promise.allSettled(this.all);
  }
}

type Future = Promise<Dict>;
type PlanEntry<I> = [Dict, string, Array<[I, Future, boolean]>];

/** [data, reason] of a finished shared call; [null, null] if it failed or raised. */
export async function answerOf(fut: Future): Promise<[unknown, unknown]> {
  let r: Dict;
  try {
    r = await fut;
  } catch {
    return [null, null];
  }
  return [get(r, "data"), get(r, "reason")];
}

/** The result dict of a call THIS spec made (a copy: shared callers grade from the same one). */
export async function ownResult(fut: Future): Promise<Dict> {
  try {
    return { ...(await fut) };
  } catch (e) {  // one bad call must not sink the pass
    return { status: "orange", confidence: "low", reason: "", note: `AI error: ${excType(e)}: ${excStr(e)}`, tokens: [0, 0] };
  }
}

/** Grade a spec from ANOTHER spec's call (same group / same still): no second AI call. */
export async function regrade(fut: Future, expected: unknown): Promise<Dict> {
  const [data, reason] = await answerOf(fut);
  if (data === null) return { status: "orange", note: "shared AI call failed", tokens: [0, 0] };
  return gradeSchema(data, expected, reason, [0, 0]);
}

const callKey = (grp: unknown, k: unknown) => `${reprOf(grp)}\u0000${reprOf(k)}`;

/** `str(Path(p))` of a shot's `path` (Path(None) raises TypeError). */
function shotPath(p: unknown): string {
  if (typeof p !== "string") throw pyError("TypeError", `argument should be a str or an os.PathLike object where __fspath__ returns a str, not '${p === null ? "NoneType" : typeof p}'`);
  return pathNorm(p);
}

/**
 * Submit every pending `image:` call of ONE run; return what `finishImage` needs.
 *
 * ONE AI call per captured SHOT, so each surface is judged on its own full-resolution still rather
 * than as one summary of six. Calls are keyed per (group, shot) so two cases sharing a tour (e.g.
 * text-scaling and clipped-glyphs over the same screens) cost one call, not two.
 */
export function planImage(pool: Pool, provider: ImageProvider, cases: Dict[]): Array<PlanEntry<Dict | null>> {
  const calls = new Map<string, Future>();                 // (group, file) -> future; scoped to this run
  const plan: Array<PlanEntry<Dict | null>> = [];
  for (const c of cases) {
    (pyList(has(c, "image") ? c.image : []) as Dict[]).forEach((spec, i) => {
      if (dget(spec, "status") !== "pending") return;
      const shots = (or(get(spec, "shots"), []) as Dict[]);
      if (!shots.length) {
        Object.assign(spec, { status: "orange", note: "no screenshots were captured for this assert" });
        return;
      }
      const grp = or(get(spec, "group"), `${strOf(need(c, "id"))}-img${i}`);
      if (truthy(get(spec, "combine"))) {
        // ONE call over every still of a scrolling surface (see runImageAssert).
        const key = callKey(grp, "__combined__");
        const own = !calls.has(key);
        if (own) {
          const first = shotPath(need(shots[0], "path"));
          const label = shots.map((sh, j) => {
            const l = need(sh, "label");
            if (typeof l !== "string") throw pyError("TypeError", `sequence item ${j}: expected str instance, ${l === null ? "NoneType" : typeof l} found`);
            return l;
          }).join(", ");
          const extras = shots.slice(1).map((sh) => shotPath(need(sh, "path")));
          calls.set(key, pool.submit(() => runImageAssert(provider, first, spec, label, extras)));
        }
        plan.push([spec, "combined", [[null, calls.get(key)!, own]]]);
        return;
      }
      const parts: Array<[Dict | null, Future, boolean]> = [];
      for (const shot of shots) {
        const key = callKey(grp, need(shot, "file"));
        const own = !calls.has(key);
        if (own) {
          const path = shotPath(need(shot, "path"));
          const label = need(shot, "label");
          calls.set(key, pool.submit(() => runImageAssert(provider, path, spec, label)));
        }
        parts.push([shot, calls.get(key)!, own]);
      }
      plan.push([spec, "shots", parts]);
    });
  }
  return plan;
}

const tok = (r: Dict, i: number): number => ((has(r, "tokens") ? r.tokens : [0, 0]) as number[])[i];

/**
 * Fold the finished image calls into their specs. The assert passes only if EVERY shot passes, and
 * the note names the ones that didn't — otherwise a single bad surface would disappear into an
 * aggregate answer.
 */
const NUMBER = /-?\d[\d,]*(?:\.\d+)?/;

/** The first number in an AI-read text ("$1,250.00" -> 1250), or null when there is none. */
export function readNumber(v: unknown): number | null {
  const m = NUMBER.exec(v === null || v === undefined ? "" : strOf(v));
  return m ? Number(m[0].replace(/,/g, "")) : null;
}

/**
 * `compare_shots: {field, ratio, tol}` on a per-shot `image:` assert: the number the AI read in
 * `field` on the SECOND shot, divided by the one on the FIRST, must equal `ratio` within `tol`
 * (relative, default 0.02). It says what no per-shot `expected` can: that a figure SCALED between
 * two captures (a paytable shot at two bets). Returns [ok, note].
 */
export function compareShots(cmp: Dict, rows: Dict[]): [boolean, string] {
  const field = dget(cmp, "field") as string;
  if (rows.length !== 2) return [false, `compare_shots needs exactly 2 screens, got ${rows.length}`];
  const firstText = dget(or(dget(rows[0], "data"), {}), field);
  const secondText = dget(or(dget(rows[1], "data"), {}), field);
  const first = readNumber(firstText);
  const second = readNumber(secondText);
  const want = readNumber(dget(cmp, "ratio"));
  const tol = pyFloatAny(has(cmp, "tol") ? cmp.tol : 0.02);
  if (want === null) return [false, `compare_shots: ratio ${reprOf(dget(cmp, "ratio"))} is not a number`];
  if (first === null || second === null || first === 0) {
    return [false, `${strOf(field)}: could not read two numbers (${reprOf(firstText)}, ${reprOf(secondText)})`];
  }
  const got = second / first;
  const ok = Math.abs(got - want) <= tol * Math.abs(want);
  return [ok, `${strOf(field)}: ${reprOf(firstText)} -> ${reprOf(secondText)}, ratio ${pyFloatRepr(pyRound(got, 4))} `
    + `${ok ? "matches" : "does not match"} ${pyFloatRepr(want)} (tol ${pyFloatRepr(tol)})`];
}

export async function finishImage(plan: Array<PlanEntry<Dict | null>>, cases: Dict[]): Promise<void> {
  for (const [spec, mode, parts] of plan) {
    const results: Array<[Dict, Dict | null]> = [];
    for (const [shot, fut, own] of parts) {
      results.push([own ? await ownResult(fut) : await regrade(fut, get(spec, "expected")), shot]);
    }
    if (mode === "combined") {
      const r = results[0][0];
      const shots = (or(get(spec, "shots"), []) as Dict[]);
      r.shot = `${shots.length} screen(s) graded together`;
      r.files = shots.map((sh) => dget(sh, "file"));
      spec.shot_results = [r];
      spec.status = r.status;
      spec.note = or(get(r, "note"), "");
      spec.tokens = has(r, "tokens") ? r.tokens : [0, 0];
      continue;
    }
    const rows: Dict[] = [];
    const failed: string[] = [];
    for (const [r, shot] of results) {
      r.shot = need(shot!, "label");
      r.file = dget(shot, "file");         // so the report can show the still it graded
      rows.push(r);
      if (r.status !== "pass") failed.push(`${strOf(need(shot!, "label"))}: ${strOf(has(r, "note") ? r.note : "")}`);
    }
    let cmpNote: string | null = null;
    if (truthy(get(spec, "compare_shots"))) {
      const [cmpOk, note] = compareShots(spec.compare_shots as Dict, rows);
      cmpNote = note;
      spec.compare_shots_ok = cmpOk;
      if (!cmpOk) failed.push(note);
    }
    spec.shot_results = rows;
    spec.status = !failed.length ? "pass" : "orange";
    spec.note = !failed.length ? `all ${rows.length} screens matched` + (cmpNote ? `; ${cmpNote}` : "") : failed.join(" | ");
    spec.tokens = [rows.reduce((n, r) => n + tok(r, 0), 0), rows.reduce((n, r) => n + tok(r, 1), 0)];
  }
  for (const c of cases) c.verdict = recomputeVerdict(c);
}

/**
 * The symbol/UI reference pack primed into one AI call — the pictures that let a model say "the
 * orange tabby", not "a cat".
 *
 * ⚠ Without it a prompt naming a symbol is asking the model to guess. Grandma Pets has THREE cats
 * and TWO dogs; nothing in a video says which is P3 unless the manifest shows it.
 *
 * Scope comes from the assert (`assets:` / `asset_groups:` on the `video:` block); with neither,
 * the game's whole manifest is sent. null when the game has no manifest, which is not an error —
 * most prompts describe what they mean in words.
 */
export function referenceFor(game: string | null, spec: Dict): SymbolReference | null {
  if (!game) return null;
  try {
    const base = REPO_ROOT;
    const groups = get(spec, "asset_groups");
    const items = get(spec, "assets");
    const isEmptyList = (v: unknown) => Array.isArray(v) && v.length === 0;
    // An EXPLICIT empty list means "send no pictures" — for a prompt that names no symbol at all (a
    // counter, a button state). Distinct from the field being absent, which means "send the lot".
    // Without the distinction every such call carries the whole manifest for nothing.
    if (isEmptyList(groups) && (items === null || isEmptyList(items))) return null;
    if (isEmptyList(items) && groups === null) return null;
    if (truthy(groups) || truthy(items)) return loadReferenceSelection(game, base, groups, items);
    return loadSymbolReference(game, base);
  } catch (e) {
    console.log(`  ! asset reference unavailable for ${game}: ${excStr(e)}`);
    return null;
  }
}

/**
 * Trim one assert's sub-clip and analyse it. Runs on a worker, so it only READS `spec`: the
 * reference pack goes on a private copy (it is not JSON-serialisable and must stay out of
 * result.json anyway).
 */
export async function videoJob(provider: VideoProvider, sessionWebm: string, clip: string, spec: Dict, game: string | null): Promise<Dict> {
  if (!(await trimClip(sessionWebm, get(spec, "window"), clip))) return { status: "orange", note: "clip trim failed" };
  const call: Dict = { ...spec };
  call.reference = referenceFor(game, spec);
  return runVideoAssert(provider, clip, call);
}

/** `Path.mkdir(exist_ok=True)` (no parents). */
function mkdirExistOk(p: string): void {
  try {
    mkdirSync(p);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "EEXIST" && statSync(p).isDirectory()) return;
    throw e;
  }
}

/**
 * Submit every pending `video:` call of ONE run; return the plan `finishVideo` grades.
 *
 * Plan entries: `[spec, "one", [[null, future, own]]]` for a single clip, and `[spec, "each",
 * [[window, future, own], ...]]` for an `each:` spec — one clip per round. A spec in a `group`
 * already submitted makes no call of its own: it is graded from that call's answer (per clip, for
 * `each`).
 */
export function planVideo(
  pool: Pool, provider: VideoProvider, sessionWebm: string, runDir: string, cases: Dict[], game: string | null,
): Array<PlanEntry<Dict | null>> {
  const clips = pathJoin(runDir, "_clips");
  mkdirExistOk(clips);
  const calls = new Map<string, [Future, string | null]>();   // (group, clip index) -> future; scoped to this run
  const plan: Array<PlanEntry<Dict | null>> = [];
  for (const c of cases) {
    (pyList(has(c, "video") ? c.video : []) as Dict[]).forEach((spec, i) => {
      if (dget(spec, "status") !== "pending") return;
      const grp = get(spec, "group");
      if (has(spec, "each")) {
        const parts: Array<[Dict | null, Future, boolean]> = [];
        (pyList(or(get(spec, "windows"), [])) as Dict[]).forEach((w, k) => {
          const key = grp !== null ? callKey(grp, k) : null;
          if (key !== null && calls.has(key)) {
            const [fut, clipName] = calls.get(key)!;
            parts.push([{ ...w, clip: clipName }, fut, false]);
            return;
          }
          const job: Dict = { ...spec, window: need(w, "window") };
          if (truthy(get(spec, "timestamp"))) job.timeline_hint = timelineHint(runDir, w.window);
          // named after the FIRST case that makes the call; sharers link to the same file
          const clipName = `${strOf(need(c, "id"))}-${i}-r${strOf(need(w, "seq"))}.webm`;
          const clipPath = pathJoin(clips, clipName);
          const fut = pool.submit(() => videoJob(provider, sessionWebm, clipPath, job, game));
          if (key !== null) calls.set(key, [fut, clipName]);
          parts.push([{ ...w, clip: clipName }, fut, true]);
        });
        plan.push([spec, "each", parts]);
        return;
      }
      if (truthy(get(spec, "timestamp")) && !has(spec, "timeline_hint")) {   // annotate action times once
        spec.timeline_hint = timelineHint(runDir, get(spec, "window"));
      }
      const key = grp !== null ? callKey(grp, 0) : null;
      if (key !== null && calls.has(key)) {
        plan.push([spec, "one", [[null, calls.get(key)![0], false]]]);
        return;
      }
      const clipPath = pathJoin(clips, `${strOf(need(c, "id"))}-${i}.webm`);
      const fut = pool.submit(() => videoJob(provider, sessionWebm, clipPath, spec, game));
      if (key !== null) calls.set(key, [fut, null]);
      plan.push([spec, "one", [[null, fut, true]]]);
    });
  }
  return plan;
}

export async function finishVideo(plan: Array<PlanEntry<Dict | null>>, cases: Dict[]): Promise<void> {
  for (const [spec, mode, parts] of plan) {
    if (mode === "one") {
      const [, fut, own] = parts[0];
      Object.assign(spec, own ? await ownResult(fut) : await regrade(fut, get(spec, "expected")));
      continue;
    }
    // `each`: every clip graded on its own; the assert passes only if EVERY clip passes, and the
    // note names the rounds that did not — the reason to split the recording at all.
    if (!parts.length) {
      Object.assign(spec, {
        status: "orange", clip_results: [], tokens: [0, 0],
        note: `no round of this kind happened (\`${strOf(get(spec, "each"))}\` is empty) — nothing to grade`,
      });
      continue;
    }
    const rows: Dict[] = [];
    const failed: string[] = [];
    for (const [w, fut, own] of parts) {
      const r = own ? await ownResult(fut) : await regrade(fut, get(spec, "expected"));
      r.seq = need(w!, "seq");
      r.window = need(w!, "window");
      r.clip = dget(w, "clip");
      rows.push(r);
      if (get(r, "status") !== "pass") failed.push(`round ${strOf(need(w!, "seq"))}: ${strOf(has(r, "note") ? r.note : "")}`);
    }
    spec.clip_results = rows;
    spec.status = !failed.length ? "pass" : "orange";
    spec.note = !failed.length ? `all ${rows.length} clips matched` : failed.join(" | ");
    spec.tokens = [rows.reduce((n, r) => n + tok(r, 0), 0), rows.reduce((n, r) => n + tok(r, 1), 0)];
  }
  for (const c of cases) c.verdict = recomputeVerdict(c);
}

/**
 * Grade every pending AI assert (video AND image) of EVERY run, with the calls in parallel.
 *
 * `runs` = [[runDir, cases], ...]. The calls are independent — each is one clip or one still and
 * one prompt — so they go on ONE pool shared by the whole suite, `workers` (default `aiWorkers()`)
 * at a time. Before this each call waited for the previous one, and a suite of ~40 calls at 20-60 s
 * each spent most of its wall-clock idle on the network. Grading stays sequential and in run order
 * once the answers are in, so results and reports read exactly as before.
 *
 * `sessionWebm` is the single shared session video at the run root (not per-run copies). `game`
 * names the asset manifest whose pictures prime each video call (see `referenceFor`).
 */
export async function runAi(
  videoProvider: VideoProvider | null, imageProvider: ImageProvider | null, sessionWebm: string,
  runs: Array<[string, Dict[]]>, opts: { game?: string | null; workers?: number | null } = {},
): Promise<void> {
  const game = opts.game ?? null;
  const workers = opts.workers || aiWorkers();
  const pool = new Pool(workers);
  try {
    const plans: Array<[Dict[], Array<PlanEntry<Dict | null>>, Array<PlanEntry<Dict | null>>]> = [];
    for (const [runDir, cases] of runs) {
      const vplan = videoProvider !== null ? planVideo(pool, videoProvider, sessionWebm, runDir, cases, game) : [];
      const iplan = imageProvider !== null ? planImage(pool, imageProvider, cases) : [];
      plans.push([cases, vplan, iplan]);
    }
    // both plans are [spec, mode, [[item, future, own], ...]]; count the calls really made
    let n = 0;
    for (const [, vp, ip] of plans) {
      for (const plan of [vp, ip]) for (const [, , parts] of plan) for (const part of parts) n += part[2] ? 1 : 0;
    }
    if (n) console.log(`  ${n} AI call(s) running, ${Math.min(workers, n)} at a time...`);
    for (const [cases, vplan, iplan] of plans) {              // each await blocks until that answer is in
      if (vplan.length) await finishVideo(vplan, cases);
      if (iplan.length) await finishImage(iplan, cases);
    }
  } finally {
    await pool.shutdown();
  }
}
