/**
 * The AI pass (src/services/ai/) answers as the Python did (baseline/ai.json, written by
 * baseline/make_ai_baseline.py). Every case in the baseline carries its INPUT and the data that
 * drives the stand-ins (processes, Gemini client, HTTP endpoint, stub providers), so this file only
 * replays: it holds no case lists of its own.
 */
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, sep } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { isDict, pyJson, type Dict } from "../src/py.js";
import { call, camel, Diff, loadBaseline, plain as basePlain, REPO } from "./baseline.js";

const hooks = vi.hoisted(() => ({
  run: null as null | ((cmd: string[], opts: any) => Promise<any>),
  sleep: null as null | ((s: number) => Promise<void>),
  genai: null as null | ((opts: any) => any),
}));

vi.mock("../src/services/ai/proc.js", async (orig) => {
  const real = await orig<typeof import("../src/services/ai/proc.js")>();
  return {
    ...real,
    runProcess: (cmd: string[], opts?: any) => (hooks.run ? hooks.run(cmd, opts ?? {}) : real.runProcess(cmd, opts)),
    sleep: (s: number) => (hooks.sleep ? hooks.sleep(s) : real.sleep(s)),
  };
});

vi.mock("@google/genai", async (orig) => {
  const real = await orig<typeof import("@google/genai")>();
  return { ...real, GoogleGenAI: function GoogleGenAI(o: any) { return hooks.genai!(o); } };
});

vi.mock("../src/services/env.js", async (orig) => {
  const real = await orig<typeof import("../src/services/env.js")>();
  return { ...real, loadEnvFiles: () => [] };
});

const ai = await import("../src/services/ai/ai-video.js");
const claude = await import("../src/services/ai/claude-ai.js");
const gem = await import("../src/services/ai/gemini.js");
const gemVideo = await import("../src/services/ai/gemini-video.js");
const pricing = await import("../src/services/ai/gemini-pricing.js");
const local = await import("../src/services/ai/local-ai.js");
const symref = await import("../src/services/ai/symbol-reference.js");
const support = await import("../src/services/ai/py-support.js");
const proc = await import("../src/services/ai/proc.js");

const B = loadBaseline<Record<string, any>>("ai.json");
const ROOT = realpathSync.native(REPO);
let TMP = "";

// ── helpers ──────────────────────────────────────────────────────────────────────────

/** Python prints a float `7.0`; a JS number cannot remember it was one (see PORTING.md). */
function dropFloatZero(x: unknown): unknown {
  if (typeof x === "string") return x.replace(/(\d)\.0(?!\d)/g, "$1");
  if (Array.isArray(x)) return x.map(dropFloatZero);
  if (x && typeof x === "object") {
    return Object.fromEntries(Object.entries(x as Record<string, unknown>).map(([k, v]) => [k, dropFloatZero(v)]));
  }
  return x;
}

const clone = <T>(v: T): T => structuredClone(v);

/** `plain()` from ./baseline.js, except that a `"__proto__"` key survives (json.loads keeps it as an
 * ordinary key; `out[k] = ...` would set the prototype instead). */
function plain(v: unknown): unknown {
  if (isDict(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, plain(x)]));
  if (Array.isArray(v)) return v.map(plain);
  return basePlain(v);
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

let SUBS: Array<[RegExp, string]> = [];

function setSubs(): void {
  const variants = (p: string) => [p, p.replace(/\\/g, "\\\\"), p.replace(/\\/g, "/")];
  const pairs: Array<[string, string]> = [
    ...variants(TMP).map((v) => [v, "<TMP>"] as [string, string]),
    ...variants(ROOT).map((v) => [v, "<ROOT>"] as [string, string]),
  ];
  pairs.sort((a, b) => b[0].length - a[0].length);
  SUBS = pairs.map(([a, b]) => [new RegExp(escapeRe(a), "gi"), b]);
}

/** Machine paths -> <TMP> / <ROOT>, as the generator wrote them. */
function norm(v: any): any {
  if (typeof v === "string") {
    for (const [re, b] of SUBS) v = v.replace(re, b);
    return v;
  }
  if (Array.isArray(v)) return v.map(norm);
  if (isDict(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, norm(x)]));
  return v;
}

/** '<TMP>/a/b' / '<ROOT>/a/b' -> a real path. */
function real(s: any): any {
  if (typeof s !== "string") return s;
  for (const [k, v] of [["<TMP>", TMP], ["<ROOT>", ROOT]] as const) {
    if (s.startsWith(k)) return v + s.slice(k.length).replace(/\//g, sep);
  }
  return s;
}

function tmpDir(...parts: string[]): string {
  const p = join(TMP, ...parts);
  mkdirSync(p, { recursive: true });
  return p;
}

type Rec = { ok: unknown } | { err: string; msg: string };

function recOf(v: unknown): Rec {
  return { ok: norm(plain(v)) };
}

function errOf(e: unknown): Rec {
  return { err: (e as Error)?.name ?? "Error", msg: norm((e as Error)?.message ?? String(e)) };
}

function rec(fn: () => unknown): Rec {
  try {
    return recOf(fn());
  } catch (e) {
    return errOf(e);
  }
}

async function recAsync(fn: () => Promise<unknown>): Promise<Rec> {
  try {
    return recOf(await fn());
  } catch (e) {
    return errOf(e);
  }
}

const ENV_KEYS: string[] = B.env_keys;

async function withEnv<T>(vars: Record<string, string> | null | undefined, fn: () => T | Promise<T>): Promise<T> {
  const saved = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  for (const k of ENV_KEYS) delete process.env[k];
  for (const [k, v] of Object.entries(vars ?? {})) process.env[k] = v;
  try {
    return await fn();
  } finally {
    for (const k of ENV_KEYS) delete process.env[k];
    for (const [k, v] of Object.entries(saved)) if (v !== undefined) process.env[k] = v;
  }
}

/** console.log lines while `fn` runs (Python's captured stdout, split into lines). */
async function captured<T>(fn: () => Promise<T>): Promise<[T, string[]]> {
  const out: string[] = [];
  const spy = vi.spyOn(console, "log").mockImplementation((...a: unknown[]) => {
    out.push(a.map(String).join(" "));
  });
  try {
    const r = await fn();
    return [r, norm(support.pySplitlines(out.map((l) => `${l}\n`).join("")))];
  } finally {
    spy.mockRestore();
  }
}

function check(d: Diff, where: string, got: unknown, want: unknown): void {
  d.expect(where, dropFloatZero(got), dropFloatZero(want));
}

function refFrom(spec: any): any {
  if (spec === null || spec === undefined) return null;
  const base = real(spec.base ?? "<ROOT>");
  if (spec.full) return symref.loadSymbolReference(spec.game, base, spec.categories ?? null);
  return symref.loadReferenceSelection(spec.game, base, spec.groups ?? null, spec.assets ?? null);
}

function refSummary(ref: any): any {
  if (ref === null || ref === undefined) return null;
  return norm({ header: ref.header, items: ref.items.map(([p, l]: [string, string]) => [p, l]), is_empty: ref.isEmpty });
}

// ── stand-ins (mirrors of the generator's) ───────────────────────────────────────────

function program(cmd: string[]): string {
  const name = basename(String(cmd[0])).toLowerCase();
  for (const p of ["ffmpeg", "ffprobe"]) if (name.startsWith(p)) return p;
  return "cli";
}

function hashedInput(text: string | null | undefined): unknown {
  if (text === null || text === undefined) return null;
  let msg: any;
  try {
    msg = support.jsonLoads(support.pyStrip(text));
  } catch {
    return norm(text);
  }
  if (!(isDict(msg) && msg.type === "user")) return norm(text);
  for (const block of (msg as any).message.content) {
    if (block.type === "image") block.source.data = createHash("sha256").update(block.source.data, "ascii").digest("hex");
  }
  return norm(msg);
}

class Procs {
  readonly script: Record<string, any[]>;
  readonly calls: any[] = [];

  constructor(script: Record<string, any[]> | null | undefined) {
    this.script = clone(script ?? {});
  }

  private next(prog: string): any {
    const acts = this.script[prog];
    if (!acts || !acts.length) return {};
    return acts.length > 1 ? acts.shift() : acts[0];
  }

  run = async (cmd: string[], opts: any): Promise<any> => {
    const r: any = {
      cmd: norm(cmd.map(String)), timeout: opts.timeout ?? null,
      cwd: opts.cwd ? basename(opts.cwd) : null, input: hashedInput(opts.input),
    };
    if (opts.env !== undefined) {
      r.env = Object.fromEntries(["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BASELINE_OTHER"].map((k) => [k, k in opts.env]));
    }
    this.calls.push(r);
    let act = this.next(program(cmd));
    if (act.if_drawtext !== undefined && act.if_drawtext !== null && cmd.some((c) => String(c).includes("drawtext"))) act = act.if_drawtext;
    const last = String(cmd[cmd.length - 1]);
    if ((act.fail_names ?? []).includes(basename(last))) act = { rc: 1, stderr: "forced failure" };
    if (act.raise === "timeout") throw new proc.TimeoutExpired(cmd, opts.timeout);
    if (act.raise === "missing") throw new proc.FileNotFoundError(cmd[0]);
    if (act.create) writeFileSync(last, "output");
    for (let i = 0; i < (act.frames ?? 0); i++) {
      writeFileSync(join(dirname(last), `frame-${String(i + 1).padStart(4, "0")}.jpg`), `frame-${i + 1}`);
    }
    return { returncode: act.rc ?? 0, stdout: act.stdout ?? "", stderr: act.stderr ?? "" };
  };
}

/** base64 of every file under TMP -> its name (Python recorded the PIL image's file name). */
function knownImages(): Map<string, string> {
  const m = new Map<string, string>();
  const walk = (d: string) => {
    for (const f of readdirSync(d)) {
      const p = join(d, f);
      if (statSync(p).isDirectory()) walk(p);
      else m.set(readFileSync(p).toString("base64"), f);
    }
  };
  walk(TMP);
  return m;
}

class FakeGenai {
  readonly log: any[] = [];
  private readonly states: Array<string | null>;
  private readonly images = knownImages();

  constructor(readonly script: any) {
    this.script = script ?? {};
    this.states = [...(this.script.states ?? ["ACTIVE"])];
  }

  private nextState(): string | null {
    return (this.states.length > 1 ? this.states.shift() : this.states[0]) ?? null;
  }

  private partOf(p: any): any {
    if ("text" in p) return { text: norm(p.text) };
    if ("inlineData" in p) return { image: this.images.get(p.inlineData.data) ?? "?", mime: p.inlineData.mimeType };
    if ("fileData" in p) return { file: { uri: p.fileData.fileUri, mime_type: p.fileData.mimeType } };
    return { other: Object.keys(p).join(",") };
  }

  client = (o: any): any => {
    this.log.push({ op: "client", api_key: o.apiKey });
    return {
      files: {
        upload: async (p: any) => {
          this.log.push({ op: "upload", file: basename(String(p.file)) });
          const st = this.nextState();
          const mime = String(p.file).endsWith(".mp4") ? "video/mp4" : "video/webm";
          return { name: "files/abc", uri: "https://example.invalid/files/abc", mimeType: mime, state: st ?? undefined };
        },
        get: async (p: any) => {
          this.log.push({ op: "get", name: p.name });
          const st = this.nextState();
          return { name: p.name, uri: "https://example.invalid/files/abc", mimeType: "video/mp4", state: st ?? undefined };
        },
      },
      models: {
        generateContent: async (p: any) => {
          const parts = p.contents.length === 1 && p.contents[0].parts ? p.contents[0].parts : p.contents;
          this.log.push({ op: "generate", model: p.model, contents: parts.map((x: any) => this.partOf(x)), config: p.config?.mediaResolution ?? null });
          const r = this.script.response ?? {};
          if ("raise" in r) throw new Error(r.raise);
          const usage = r.usage;
          return {
            text: r.text ?? undefined,
            usageMetadata: usage === null || usage === undefined ? undefined : {
              promptTokenCount: usage.prompt ?? undefined,
              candidatesTokenCount: usage.candidates ?? undefined,
              thoughtsTokenCount: usage.thoughts ?? undefined,
            },
          };
        },
      },
    };
  };
}

function makeAnswer(a: any): any {
  if (a.raise) throw support.pyError("RuntimeError", a.raise);
  const r: any = { success: "success" in a ? a.success : true, data: clone(a.data ?? null), reason: a.reason ?? null };
  for (const k of ["input_tokens", "output_tokens"]) if (k in a) r[k] = a[k];
  return r;
}

class StubVideo {
  readonly calls: any[] = [];
  constructor(readonly answers: Dict, readonly dflt: any) {}

  async analyseVideo(clip: string, contextId: string | null, prompt: string, schema: Dict, opts: any = {}): Promise<any> {
    const name = basename(clip);
    this.calls.push(norm({
      clip: name, context_id: contextId, prompt, schema: clone(schema), reference: refSummary(opts.reference ?? null),
      high_fps: opts.highFps ?? false, timestamp: opts.timestamp ?? false,
    }));
    return makeAnswer(this.answers[name] ?? this.dflt);
  }
}

class StubImage {
  readonly calls: any[] = [];
  constructor(readonly answers: Dict, readonly dflt: any) {}

  async analyseImage(path: string, prompt: string, schema: Dict, opts: any = {}): Promise<any> {
    const name = basename(path);
    this.calls.push(norm({
      shot: name, prompt, schema: clone(schema),
      extras: opts.extraImages === null || opts.extraImages === undefined ? null : opts.extraImages.map((p: string) => basename(p)),
    }));
    return makeAnswer(this.answers[name] ?? this.dflt);
  }
}

function resultOf(r: any): any {
  return norm(plain({ success: r.success, data: r.data, reason: r.reason, input_tokens: r.input_tokens, output_tokens: r.output_tokens, raw_text: r.raw_text }));
}

// ── setup ────────────────────────────────────────────────────────────────────────────

let savedEnv: Record<string, string | undefined> = {};
let stderrSpy: any;

beforeAll(() => {
  TMP = realpathSync.native(mkdtempSync(join(tmpdir(), "aibase-ts-")));
  setSubs();
  for (const [rel, spec] of Object.entries(B.files as Record<string, any>)) {
    const p = join(TMP, ...rel.split("/"));
    mkdirSync(dirname(p), { recursive: true });
    if ("copy" in spec) copyFileSync(real(spec.copy), p);
    else writeFileSync(p, spec.text, "utf-8");
  }
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  for (const k of ENV_KEYS) delete process.env[k];
  stderrSpy = vi.spyOn(process.stderr, "write").mockImplementation(() => true);   // the Python's log lines
});

afterAll(() => {
  stderrSpy?.mockRestore();
  for (const [k, v] of Object.entries(savedEnv)) if (v !== undefined) process.env[k] = v;
  hooks.run = hooks.sleep = hooks.genai = null;
  try {
    rmSync(TMP, { recursive: true, force: true });
  } catch {
    // Windows may still hold a handle
  }
});

// ── tests ────────────────────────────────────────────────────────────────────────────

describe("grading answers against expected", () => {
  it("to_num / values_match / field_ok / compare / grade_schema / recompute_verdict", () => {
    const d = new Diff();
    const G = B.grading;
    for (const c of G.to_num) check(d, `to_num ${JSON.stringify(c.in)}`, rec(() => ai.toNum(c.in)), c.out);
    for (const c of G.read_number) check(d, `read_number ${JSON.stringify(c.in)}`, rec(() => ai.readNumber(c.in)), c.out);
    for (const c of G.compare_shots) check(d, `compare_shots ${JSON.stringify(c.in)}`, rec(() => ai.compareShots(c.in[0], c.in[1])), c.out);
    for (const c of G.values_match) check(d, `values_match ${JSON.stringify(c.in)}`, rec(() => ai.valuesMatch(c.in[0], c.in[1])), c.out);
    for (const c of G.field_ok) check(d, `field_ok ${JSON.stringify(c.in)}`, rec(() => ai.fieldOk(c.in[0], c.in[1], c.in[2])), c.out);
    for (const c of G.compare) check(d, `compare ${JSON.stringify(c.in)}`, rec(() => ai.compare(c.in[0], c.in[1], c.in[2])), c.out);
    for (const c of G.grade_schema) {
      const [data, expected, reason, tokens] = clone(c.in);
      check(d, `grade_schema ${JSON.stringify(c.in)}`, rec(() => ai.gradeSchema(data, expected, reason, tokens)), c.out);
    }
    for (const c of G.recompute_verdict) check(d, `verdict ${JSON.stringify(c.in)}`, rec(() => ai.recomputeVerdict(clone(c.in))), c.out);
    expect(d.lines, d.report()).toEqual([]);
  });

  it("ai_workers from AI_CONCURRENCY / AI_PROVIDER", async () => {
    const d = new Diff();
    for (const c of B.grading.ai_workers) {
      check(d, `ai_workers ${JSON.stringify(c.env)}`, await withEnv(c.env, () => rec(() => ai.aiWorkers(c.default))), c.out);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("one assert: prompt sent + grade", () => {
  it("run_video_assert", async () => {
    const d = new Diff();
    for (const c of B.video_assert) {
      const stub = new StubVideo({}, c.in.answer);
      const spec = clone(c.in.spec);
      if (c.in.reference) spec.reference = refFrom(c.in.reference);
      const out = await recAsync(() => ai.runVideoAssert(stub as any, join(tmpDir("va"), "clip.webm"), spec));
      check(d, `video ${JSON.stringify(c.in.spec).slice(0, 80)}`, { call: stub.calls[0] ?? null, out }, { call: c.call, out: c.out });
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("run_image_assert", async () => {
    const d = new Diff();
    for (const c of B.image_assert) {
      const stub = new StubImage({}, c.in.answer);
      const extras = c.in.extras === null ? null : c.in.extras.map((e: string) => join(TMP, "img", e));
      const out = await recAsync(() => ai.runImageAssert(stub as any, join(TMP, "img", c.in.shot), clone(c.in.spec), c.in.label, extras));
      check(d, `image ${JSON.stringify(c.in.spec).slice(0, 80)}`, { call: stub.calls[0] ?? null, out }, { call: c.call, out: c.out });
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("timeline_hint", () => {
    const d = new Diff();
    for (const c of B.timeline_hint) {
      const dir = tmpDir(...c.dir.split("/"));
      if (c.in.raw !== undefined && c.in.raw !== null) writeFileSync(join(dir, "timeline.json"), c.in.raw, "utf-8");
      else if (c.in.timeline !== undefined && c.in.timeline !== null) writeFileSync(join(dir, "timeline.json"), JSON.stringify(c.in.timeline), "utf-8");
      check(d, `timeline ${c.dir} ${JSON.stringify(c.in.window)}`, rec(() => ai.timelineHint(dir, c.in.window)), c.out);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("ffmpeg", () => {
  it("trim_clip: arguments, copy -> re-encode fallback, failures", async () => {
    const d = new Diff();
    for (const c of B.trim) {
      const procs = new Procs(c.in.procs);
      hooks.run = procs.run;
      const dest = join(tmpDir(...c.dir.split("/")), "clip.webm");
      const [out, stdout] = await captured(() => recAsync(() => ai.trimClip(join(TMP, c.in.session), c.in.window, dest)));
      hooks.run = null;
      check(d, `trim ${c.dir}`, { out, calls: procs.calls, stdout }, { out: c.out, calls: c.calls, stdout: c.stdout });
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("montage filter, preamble, drawtext, build", async () => {
    const d = new Diff();
    const M = B.montage;
    check(d, "constants", {
      cols: gemVideo.MONTAGE_COLS, interval: gemVideo.MONTAGE_INTERVAL_S, sample_fps: gemVideo.MONTAGE_SAMPLE_FPS,
      padding: gemVideo.MONTAGE_PADDING_PX, timeout: gemVideo.FFMPEG_TIMEOUT, high_fps: gemVideo.MONTAGE_HIGH_FPS,
    }, M.constants);
    check(d, "font_file", gemVideo.fontFile(), M.font_file);
    check(d, "drawtext", gemVideo.drawtextTs(), M.drawtext);
    for (const c of M.filters) check(d, `filter ${c.in}`, gemVideo.montageFilter(c.in[0], c.in[1]), c.out);
    for (const c of M.preambles) check(d, `preamble ${c.in}`, gemVideo.montagePreamble(c.in[0], c.in[1]), c.out);
    for (const c of M.builds) {
      const procs = new Procs(c.in.procs);
      hooks.run = procs.run;
      const dest = join(tmpDir(...c.dir.split("/")), "out-montage.mp4");
      const out = await recAsync(() => gemVideo.buildMontage(join(TMP, ...c.in.source.split("/")), dest, c.in.fps, c.in.timestamp));
      hooks.run = null;
      check(d, `build ${c.dir}`, { out, calls: procs.calls }, { out: c.out, calls: c.calls });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("Gemini helpers", () => {
  it("GeminiVideoHelper.analyse_video (montage, upload, contents, parse, cost)", async () => {
    const d = new Diff();
    for (const c of B.gemini_video) {
      const fake = new FakeGenai(c.in.genai);
      const procs = new Procs(c.in.procs);
      const sleeps: number[] = [];
      const clip = join(tmpDir(...c.dir.split("/")), c.in.clip);
      writeFileSync(clip, "clip");
      hooks.run = procs.run;
      hooks.genai = fake.client;
      hooks.sleep = async (s) => {
        sleeps.push(s);
      };
      const helper = new gemVideo.GeminiVideoHelper(`key-${c.dir.split("/")[1]}`, c.in.model ?? null, c.in.resolution ?? null, refFrom(c.in.ctor_reference));
      const o = c.in.opts;
      const res = await helper.analyseVideo(clip, null, c.in.prompt, clone(c.in.schema), {
        refImages: o.ref_images ? o.ref_images.map(real) : null, reference: refFrom(o.reference),
        highFps: o.high_fps ?? false, timestamp: o.timestamp ?? false,
      });
      hooks.run = null;
      hooks.genai = null;
      hooks.sleep = null;
      check(d, `gemini video ${c.in.prompt}`, {
        result: resultOf(res), log: norm(fake.log), calls: procs.calls, sleeps, cost: plain(helper.costSummary()),
        estimated: helper.estimatedCostUsd, model: helper.model, per_call: helper.costForTokens(1000, 100),
        tokens: [helper.totalInputTokens, helper.totalOutputTokens],
      }, {
        result: c.result, log: c.log, calls: c.calls, sleeps: c.sleeps, cost: c.cost, estimated: c.estimated,
        model: c.model, per_call: c.per_call, tokens: c.tokens,
      });
    }
    // cumulative cost over several calls on ONE helper
    const fake = new FakeGenai({ response: B.gemini_video[0].in.genai.response });
    const procs = new Procs({ ffmpeg: [{ create: true }] });
    hooks.run = procs.run;
    hooks.genai = fake.client;
    const helper = new gemVideo.GeminiVideoHelper("k");
    const clip = join(tmpDir("gv", "cum"), "c.webm");
    writeFileSync(clip, "clip");
    for (let i = 0; i < 3; i++) await helper.analyseVideo(clip, null, "again", { ...B.gemini_video[0].in.schema });
    hooks.run = null;
    hooks.genai = null;
    check(d, "cumulative", { cost: plain(helper.costSummary()), estimated: helper.estimatedCostUsd }, B.gemini_cumulative);
    expect(d.lines, d.report()).toEqual([]);
  });

  it("GeminiImageHelper.analyse_image", async () => {
    const d = new Diff();
    for (const c of B.gemini_image) {
      const fake = new FakeGenai(c.in.genai);
      hooks.genai = fake.client;
      const helper = new gem.GeminiImageHelper("img-key", c.in.model ?? null);
      const res = await helper.analyseImage(real(`<TMP>/${c.in.image}`), c.in.prompt, clone(c.in.schema), {
        extraImages: c.in.extras && c.in.extras.length ? c.in.extras.map((e: string) => real(`<TMP>/${e}`)) : null,
        reference: refFrom(c.in.reference),
      });
      hooks.genai = null;
      const got: any = {
        result: resultOf(res), log: norm(fake.log), cost: plain(helper.costSummary()), estimated: helper.estimatedCostUsd,
        calls: helper.callCount, tokens: [helper.totalInputTokens, helper.totalOutputTokens],
      };
      helper.reset();
      got.after_reset = plain(helper.costSummary());
      const want = { ...c };
      delete want.in;
      check(d, `gemini image ${c.in.prompt}`, got, want);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("pricing table and per-call cost", () => {
    const d = new Diff();
    const P = B.pricing;
    check(d, "supported", [...pricing.SUPPORTED_MODELS], P.supported);
    for (const c of P.models) {
      const p = pricing.getPricing(c.in);
      check(d, `pricing ${c.in}`, {
        model: p.model, input_cost_per_1m: p.inputCostPer1m, output_cost_per_1m: p.outputCostPer1m,
        input_per_token: p.inputPerToken, output_per_token: p.outputPerToken,
      }, c.out);
    }
    for (const c of P.per_call) {
      check(d, `per_call ${c.in}`, new gemVideo.GeminiVideoHelper("k", c.in[0]).costForTokens(c.in[1], c.in[2]), c.out);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("parsing model replies", () => {
  it("fences, json.loads (CPython error text), extract_json, schema prompts", async () => {
    const d = new Diff();
    const T = B.text;
    for (const c of T.fence) {
      check(d, `fence ${JSON.stringify(c.in)}`, gem.stripMarkdownFence(c.in), c.out);
      check(d, `fence/video ${JSON.stringify(c.in)}`, gem.stripMarkdownFence(c.in), c.video);
    }
    for (const c of T.json_loads) check(d, `json.loads ${JSON.stringify(c.in)}`, rec(() => support.jsonLoads(c.in)), c.out);
    for (const c of T.extract_json) check(d, `extract_json ${JSON.stringify(c.in)}`, rec(() => local.extractJson(c.in)), c.out);
    for (const c of T.schema_prompt) {
      const [prompt, schema, allow, reasoning] = c.in;
      const got = await withEnv(reasoning === null ? {} : { AGENT_REASONING: reasoning }, () => rec(() => local.buildSchemaPrompt(prompt, clone(schema), allow)));
      check(d, `schema prompt ${JSON.stringify(c.in).slice(0, 60)}`, got, c.out);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("local endpoint helpers", () => {
  it("LocalVideoHelper / LocalImageHelper (multipart POST, parse, errors)", async () => {
    const d = new Diff();
    for (const c of B.local) {
      const posts: any[] = [];
      const h = c.in.http;
      vi.stubGlobal("fetch", async (url: string, init: any) => {
        const form = init.body as FormData;
        const file = form.get("file") as File;
        posts.push({ url, prompt: norm(form.get("prompt")), file: [file.name, file.type, file.size], timeout: local.REQUEST_TIMEOUT });
        return new Response(h.body, { status: h.status, statusText: h.reason });
      });
      const Cls = c.in.kind === "video" ? local.LocalVideoHelper : local.LocalImageHelper;
      const helper: any = new Cls(c.endpoint, refFrom(c.in.ctor_reference));
      const path = real(`<TMP>/${c.in.file}`);
      const res = c.in.kind === "video"
        ? await helper.analyseVideo(path, null, c.in.prompt, clone(c.in.schema), {
          refImages: (c.in.ref_images ?? []).length ? c.in.ref_images.map((p: string) => real(`<TMP>/${p}`)) : null,
          reference: refFrom(c.in.reference),
        })
        : await helper.analyseImage(path, c.in.prompt, clone(c.in.schema), {
          extraImages: (c.in.extras ?? []).length ? c.in.extras.map((p: string) => real(`<TMP>/${p}`)) : null,
          reference: refFrom(c.in.reference),
        });
      vi.unstubAllGlobals();
      check(d, `local ${c.in.prompt}`, { result: resultOf(res), posts, cost: plain(helper.costSummary()), calls: helper.callCount },
        { result: c.result, posts: c.posts, cost: c.cost, calls: c.calls });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("Claude CLI helpers", () => {
  it("config from env, cost, CLI resolution, nested-session env", async () => {
    const d = new Diff();
    const CL = B.claude;
    for (const c of CL.configs) {
      const got = await withEnv(c.env, () => rec(() => {
        const v = new claude.ClaudeVideoHelper();
        const i = new claude.ClaudeImageHelper();
        return {
          attrs: { _fps: v._fps, _max_frames: v._maxFrames, _cli_path: v._cliPath, _model: v._model, _timeout: v._timeout },
          model: v.model, cost_for_tokens: v.costForTokens(5, 5), video_cost: v.costSummary(), image_cost: i.costSummary(),
          context: v.createCachedContext([]), active: v.activeContextCount, resolve_cli: norm(v.resolveCli()),
        };
      }));
      check(d, `config ${JSON.stringify(c.env)}`, got, c.out);
    }
    const subEnv = await withEnv({ CLAUDECODE: "1", CLAUDE_CODE_ENTRYPOINT: "cli", BASELINE_OTHER: "y" }, () => {
      const e = claude.claudeSubprocessEnv();
      return Object.fromEntries(["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BASELINE_OTHER"].map((k) => [k, k in e]));
    });
    check(d, "subprocess env", subEnv, CL.subprocess_env);
    for (const c of CL.adaptive_fps) {
      const dur = typeof c.in[0] === "string" ? Number(c.in[0] === "inf" ? Infinity : NaN) : c.in[0];
      check(d, `adaptive_fps ${JSON.stringify(c.in)}`, rec(() => claude.adaptiveFps(dur, c.in[1], c.in[2])), c.out);
    }
    for (const c of CL.media_type) check(d, `media_type ${c.in}`, claude.mediaType(c.in), c.out);
    expect(d.lines, d.report()).toEqual([]);
  });

  it("prompt builders and ASSETS.md", async () => {
    const d = new Diff();
    const P = B.claude.prompts;
    await withEnv({}, () => {
      const synth = refFrom({ game: "synth", base: "<TMP>", groups: ["symbols", "ui"], assets: null });
      const img = new claude.ClaudeImageHelper();
      const vid = new claude.ClaudeVideoHelper();
      const vidRef = new claude.ClaudeVideoHelper(synth);
      const refs: Record<string, any> = { synth, empty: new symref.SymbolReference("h") };
      for (const c of P.image) {
        check(d, `image prompt ${c.in[0]}`, norm(img.buildImagePrompt(c.in[0], c.in[1], c.in[2] === null ? null : refs[c.in[2]])), c.out);
      }
      for (const c of P.inline) {
        const items = Array.from({ length: c.in[1] }, () => ["x", "l"] as [string, string]);
        check(d, `inline prompt ${JSON.stringify(c.in)}`, vid.buildInlineVideoPrompt(c.in[0], items, c.in[2]), c.out);
      }
      check(d, "read-tool prompts", [vid.buildVideoPrompt("Old path"), vidRef.buildVideoPrompt("Old path with assets")], P.read_tool);
      const file = vidRef.writeAssetsFile(tmpDir("assets", "work"))!;
      check(d, "ASSETS.md", { path: norm(file), text: norm(readFileSync(file, "utf-8")), none: vid.writeAssetsFile(tmpDir("assets", "none")) },
        B.claude.assets_file);
    });
    expect(d.lines, d.report()).toEqual([]);
  });

  it("ClaudeImageHelper.analyse_image (staging, -p call, envelope parsing)", async () => {
    const d = new Diff();
    for (const c of B.claude.image) {
      const dir = tmpDir(...c.dir.split("/"));
      if (c.in.image !== "missing.png") copyFileSync(join(TMP, "img", "shot.png"), join(dir, c.in.image));
      for (const e of c.in.extras ?? []) if (e !== "nothere.png") copyFileSync(join(TMP, "img", "extra1.png"), join(dir, e));
      const procs = new Procs(c.in.procs);
      hooks.run = procs.run;
      const [res, cost] = await withEnv(c.in.env, async () => {
        const helper = new claude.ClaudeImageHelper();
        const r = await helper.analyseImage(join(dir, c.in.image), c.in.prompt, clone(c.in.schema), {
          extraImages: (c.in.extras ?? []).length ? c.in.extras.map((e: string) => join(dir, e)) : null,
          reference: refFrom(c.in.reference),
        });
        return [r, plain(helper.costSummary())];
      });
      hooks.run = null;
      const stagedDir = join(dir, `${c.in.image.replace(/\.[^.]+$/, "")}-claude`);
      const staged = existsSync(stagedDir) ? readdirSync(stagedDir).sort() : null;
      check(d, `claude image ${c.in.prompt}`, { result: resultOf(res), calls: procs.calls, staged, cost },
        { result: c.result, calls: c.calls, staged: c.staged, cost: c.cost });
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it("ClaudeVideoHelper.analyse_video (probe, frame budget, sampling, inline stream-json)", async () => {
    const d = new Diff();
    for (const c of B.claude.video) {
      const dir = tmpDir(...c.dir.split("/"));
      const clip = join(dir, "clip.webm");
      const work = tmpDir(...c.dir.split("/"), "clip-claude");
      for (let i = 0; i < (c.in.stale ?? 0); i++) writeFileSync(join(work, `frame-${String(90 + i).padStart(4, "0")}.jpg`), "stale");
      const probe = c.in.probe_actions ?? c.in.probe.map((s: string) => ({ stdout: s }));
      const procs = new Procs({ ffprobe: probe, ffmpeg: c.in.ffmpeg ?? [{ frames: c.in.frames ?? 0 }], cli: c.in.cli.length ? c.in.cli : [{}] });
      hooks.run = procs.run;
      const [res, cost] = await withEnv(c.in.env, async () => {
        const helper = new claude.ClaudeVideoHelper();
        const r = await helper.analyseVideo(clip, null, c.in.prompt, clone(c.in.schema), { reference: refFrom(c.in.reference) });
        return [r, plain(helper.costSummary())];
      });
      hooks.run = null;
      check(d, `claude video ${c.in.prompt}`, { result: resultOf(res), calls: procs.calls, frames: readdirSync(work).sort(), cost },
        { result: c.result, calls: c.calls, frames: c.frames, cost: c.cost });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("symbol / UI reference packs", () => {
  const summary = (ref: any) => ({ ...refSummary(ref), descriptions: norm(ref.descriptionsText()) });

  it("load, select, index, effective selection, cache", () => {
    const d = new Diff();
    const S = B.symbols;
    for (const c of S.loads) check(d, `load ${JSON.stringify(c.in)}`, rec(() => summary(symref.loadSymbolReference(c.in[0], real(c.in[1]), c.in[2]))), c.out);
    for (const c of S.selections) {
      check(d, `select ${JSON.stringify(c.in)}`, rec(() => summary(symref.loadReferenceSelection(c.in[0], real(c.in[1]), c.in[2], c.in[3]))), c.out);
    }
    for (const c of S.indexes) check(d, `index ${JSON.stringify(c.in)}`, rec(() => symref.buildAssetIndex(c.in[0], real(c.in[1]))), c.out);
    const synthIndex = symref.buildAssetIndex("synth", TMP);
    for (const c of S.effective) {
      const [g, t, a] = clone(c.in);
      check(d, `effective ${JSON.stringify(c.in)}`, rec(() => symref.effectiveSelection(g, t, a, c.no_index ? null : synthIndex)), c.out);
    }
    check(d, "ordered", symref.orderedGroups({ ui: new Set(), extra: new Set(), symbols: new Set(), ui_elements: new Set() }), S.ordered);
    const cache = new symref.ScopedReferenceCache("synth", TMP);
    const a = cache.forSelection(["symbols"], null);
    const b = cache.forSelection(["symbols"], null);
    const cc = cache.forSelection(null, null);
    const dd = cache.scoped(null);
    const e = cache.forSelection([], null);
    const f = cache.forSelection(null, []);
    check(d, "cache", { same: a === b, full_same: cc === dd, distinct: e !== f, full: summary(cc), groups: summary(a) }, S.cache);
    check(d, "default sections", symref.DEFAULT_SECTIONS, S.default_sections);
    expect(d.lines, d.report()).toEqual([]);
  });

  it("_reference_for (what primes each video call)", async () => {
    const d = new Diff();
    for (const c of B.reference_for) {
      const [out, stdout] = await captured(async () => rec(() => refSummary(ai.referenceFor(c.in[0], c.in[1]))));
      check(d, `reference_for ${JSON.stringify(c.in)}`, { out, stdout }, { out: c.out, stdout: c.stdout });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("provider selection from env", () => {
  const ATTRS = ["api_key", "_model", "_resolution", "_fps", "_max_frames", "_cli_path", "_timeout", "_endpoint"];
  const describeP = (p: any) => (p === null ? null : {
    class: p.constructor.name,
    attrs: Object.fromEntries(ATTRS.filter((k) => camel(k) in p).map((k) => [k, p[camel(k)]])),
    model: p.model ?? null,
    cost: plain(p.costSummary()),
  });

  it("make_video_provider / make_image_provider", async () => {
    const d = new Diff();
    for (const c of B.providers) {
      const [got, stdout] = await captured(() => withEnv(c.env, () => ({
        video: describeP(ai.makeVideoProvider("dev")), image: describeP(ai.makeImageProvider("dev")),
      })));
      check(d, `providers ${JSON.stringify(c.env)}`, { ...got, stdout }, { video: c.video, image: c.image, stdout: c.stdout });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("run_ai: the whole deferred pass", () => {
  it("plans, shares calls by group, grades each/combined/shots, folds verdicts", async () => {
    const d = new Diff();
    const R = B.run_ai;
    for (const s of R.scenarios) {
      const runs: Array<[string, Dict[]]> = R.runs.map((r: any) => {
        const runDir = tmpDir(s.root, r.dir);
        if (r.timeline !== null) writeFileSync(join(runDir, "timeline.json"), JSON.stringify(r.timeline), "utf-8");
        const cases = clone(r.cases);
        for (const c of cases) {
          for (const sp of c.image ?? []) for (const sh of sp.shots ?? []) sh.path = real(sh.path.replace("<TMP>/", `<TMP>/${s.root}/`));
        }
        return [runDir, cases];
      });
      const session = join(tmpDir(s.root), "session.webm");
      writeFileSync(session, "session");
      const vstub = new StubVideo(R.answers_video, { success: true, input_tokens: 100, output_tokens: 20, data: { passed: true, confidence: "medium", reason: "default" } });
      const istub = new StubImage(R.answers_image, { success: true, input_tokens: 100, output_tokens: 20, data: { ok: true } });
      const procs = new Procs({ ffmpeg: [{ create: true, fail_names: s.in.ffmpeg_fail }] });
      hooks.run = procs.run;
      const [out, stdout] = await captured(() => withEnv(s.in.env, () => recAsync(() => ai.runAi(
        s.in.video ? (vstub as any) : null, s.in.image ? (istub as any) : null, session, runs,
        { game: s.in.game, workers: s.in.workers },
      ))));
      hooks.run = null;
      let vcalls = vstub.calls;
      let pcalls = procs.calls;
      if (s.in.sorted_calls) {
        vcalls = [...vcalls].sort((a, b) => (a.clip < b.clip ? -1 : a.clip > b.clip ? 1 : 0));
        const key = (c: any) => pyJson(c.cmd);       // the generator sorted by json.dumps(cmd)
        pcalls = [...pcalls].sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
      }
      check(d, `run_ai ${s.in.name}`, {
        out, cases: norm(plain(runs.flatMap(([, cs]) => cs))), video_calls: vcalls, image_calls: istub.calls, ffmpeg: pcalls, stdout,
        clips_dirs: runs.map(([dir]) => existsSync(join(dir, "_clips"))),
      }, {
        out: s.out, cases: s.cases, video_calls: s.video_calls, image_calls: s.image_calls, ffmpeg: s.ffmpeg, stdout: s.stdout,
        clips_dirs: s.clips_dirs,
      });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

describe("process helpers", () => {
  it("TimeoutExpired / FileNotFoundError read like Python's", () => {
    expect(new proc.FileNotFoundError("x").message).toBe(B.missing_exe);
    expect(call(() => proc.list2cmdline(["a b", 'c"d', "e\\", ""]))).toEqual({ ok: '"a b" c\\"d e\\ ""' });
  });
});
