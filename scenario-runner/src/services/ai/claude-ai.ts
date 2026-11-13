/**
 * Claude AI analysis helpers — agentic, frame-sampled alternative to Gemini (port of
 * framework/helpers/claude_ai.py).
 *
 * Drop-in replacements for `GeminiVideoHelper` / `GeminiImageHelper` that analyse media with a
 * **Claude Code CLI sub-agent** instead of a single multimodal API call. They mirror the Gemini
 * helper interfaces exactly (same method signatures, same result fields, same `costSummary` keys).
 *
 * How it works — for each assertion we:
 *   1. (video only) sample the trimmed clip into JPEG frames with ffmpeg at `CLAUDE_AI_FPS`
 *      frames/second into a per-call work dir (`{stem}-claude/frame-0001.jpg` ...).
 *   2. (video only) attach every reference symbol/UI image INLINE, each preceded by its caption.
 *      The image path embeds every reference's description AND path directly in the prompt, so
 *      the sub-agent never opens a manifest file — it reads only the image files themselves (one
 *      fewer Read round-trip per assert).
 *   3. spawn `claude -p --output-format json` with the work dir as cwd and read-only tools (`Read`,
 *      `Glob`) pre-approved (image), or one `stream-json` message with the frames inline (video),
 *      then emit ONLY a JSON object matching the schema.
 *   4. parse the CLI's JSON envelope, pull the agent's final `result` text, and extract the verdict
 *      object from it.
 *
 * Configuration (environment)
 *   AI_PROVIDER=claude      select this provider
 *   CLAUDE_AI_FPS           base frames/second to sample from video (default 1; auto-reduced for
 *                           long clips to stay within CLAUDE_AI_MAX_FRAMES)
 *   CLAUDE_AI_MAX_FRAMES    max frames sent per video assertion (default 95; keeps one request
 *                           under the API's 100-image-per-request cap)
 *   CLAUDE_AI_TOKEN_BUDGET  token budget for one inline video request (default 48000; read per call)
 *   CLAUDE_CLI_PATH         path/name of the claude executable (default "claude")
 *   CLAUDE_MODEL            model passed as --model (default "sonnet")
 *   CLAUDE_CLI_TIMEOUT      per-call subprocess timeout, seconds (default 600)
 *
 * Cost: the provider uses local Claude Code auth (no API key). As with the local provider,
 * `costSummary` reports the gemini-compatible keys with `estimated_cost_usd == 0.0` and zero
 * tokens, tracking only the call count. Any usage the CLI reports is ignored by design.
 */
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { isDict, pyFloat, pyInt, pyJson, pyRound, truthy, type Dict } from "../../py.js";
import { geminiResult, type GeminiResult } from "./gemini.js";
import type { VideoResult } from "./gemini-video.js";
import { buildSchemaPrompt, extractJson } from "./local-ai.js";
import { FileNotFoundError, runProcess, TimeoutExpired, which } from "./proc.js";
import {
  cpSlice, excStr, floorDiv, jsonLoads, JSONDecodeError, logger, osErrorText, pathJoin, pathNorm, pathParent,
  pathStem, pathSuffix, pyCeil, pyError, pySplitlines, pyStrip, strOf,
} from "./py-support.js";
import { resolvePath, type SymbolReference } from "./symbol-reference.js";

// Defaults (overridable via env — see module docstring).
export const DEFAULT_FPS = 1.0;
// Frame budget for one video assertion. The hard API cap is 100 images per request (200k-context
// models like Sonnet); we default just below it to leave headroom for the reference-asset images,
// which share the request and also count toward the cap. At the base 1 fps this keeps ~95s clips
// whole; longer clips step the fps down.
export const DEFAULT_MAX_FRAMES = 95;
// Hard per-request image cap (Anthropic Messages API, 200k-context models). Frames AND reference
// images must together stay under this in one inline assertion request.
export const HARD_IMAGE_CAP = 100;
// Token budget for one inline video assertion request. Image COUNT alone is not enough: a long
// clip at 1 fps can exceed the model's usable context window even under the 100-image cap
// (observed "Prompt is too long" around ~85 images). Frames are therefore also capped by estimated
// tokens (derived from the source frame size).
export const DEFAULT_TOKEN_BUDGET = 48000;
export const DEFAULT_CLI = "claude";
export const DEFAULT_MODEL = "sonnet";
// Per-call CLI timeout. Generous because a video assertion inline-prefills up to ~95 frames in one
// pass (measured ~237s for 86 frames) — see CLAUDE_CLI_TIMEOUT.
export const DEFAULT_TIMEOUT = 600;

// ffmpeg frame-sampling timeout (seconds).
export const FFMPEG_TIMEOUT = 120;

// Env markers Claude Code injects into its child processes. The `claude` CLI refuses to launch a
// *nested* session when it sees these ("Claude Code cannot be launched inside another Claude Code
// session"). Our CLI calls are headless one-shot `-p` invocations — not interactive sessions — so
// we strip these markers when spawning so AI asserts work even when the QA runner is itself driven
// from inside Claude Code.
export const NESTED_SESSION_ENV_MARKERS = ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"];

/** A copy of the current env with the nested-session markers removed. No-op outside Claude Code
 * (the markers are simply absent there), so it is safe in every context. */
export function claudeSubprocessEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const marker of NESTED_SESSION_ENV_MARKERS) {
    for (const k of Object.keys(env)) if (k.toUpperCase() === marker) delete env[k];
  }
  return env;
}

// ---------------------------------------------------------------------------
// ffmpeg frame sampling
// ---------------------------------------------------------------------------

// pathlib's glob is case-insensitive on Windows
const FRAME_RE = process.platform === "win32" ? /^frame-.*\.jpg$/is : /^frame-.*\.jpg$/s;

function frameFiles(dir: string): string[] {
  try {
    return readdirSync(dir).filter((f) => FRAME_RE.test(f)).map((f) => pathJoin(dir, f));
  } catch {
    return [];
  }
}

/** `str(float)` for an fps value in an ffmpeg argument. */
function floatStr(x: number): string {
  return strOf(x);
}

/**
 * Sample `videoPath` into `frame-%04d.jpg` images in `outDir` at `fps`.
 *
 * Returns true only if ffmpeg succeeds AND at least one frame was written; false (logged) on
 * missing ffmpeg, timeout, or non-zero exit.
 */
export async function sampleFrames(videoPath: string, outDir: string, fps: number): Promise<boolean> {
  mkdirSync(outDir, { recursive: true });
  // Clear stale frames so a re-sample at a lower fps doesn't leave higher-numbered frames behind
  // (which would inflate the count and the inline image set).
  for (const old of frameFiles(outDir)) {
    try {
      unlinkSync(old);
    } catch {
      // ignore
    }
  }
  const pattern = pathJoin(outDir, "frame-%04d.jpg");
  const cmd = [
    "ffmpeg",
    "-y",
    "-i", pathNorm(videoPath),
    "-vf", `fps=${floatStr(fps)}`,
    "-qscale:v", "3",
    pattern,
  ];
  let result;
  try {
    result = await runProcess(cmd, { timeout: FFMPEG_TIMEOUT });
  } catch (e) {
    if (e instanceof FileNotFoundError || (e as Error)?.name === "FileNotFoundError") {
      logger.error("ffmpeg not found — cannot sample frames for the claude provider. "
        + "Install from https://ffmpeg.org/download.html and ensure it is on PATH.");
      return false;
    }
    if (e instanceof TimeoutExpired || (e as Error)?.name === "TimeoutExpired") {
      logger.error("ffmpeg timed out sampling frames from %s", pathNorm(videoPath));
      return false;
    }
    throw e;
  }

  if (result.returncode !== 0) {
    logger.error("ffmpeg frame sampling failed (exit %d): %s", result.returncode, cpSlice(result.stderr || "", -500));
    return false;
  }

  const produced = frameFiles(outDir).length > 0;
  if (!produced) logger.error("ffmpeg produced no frames from %s (fps=%s)", pathNorm(videoPath), fps);
  return produced;
}

/** The clip duration in seconds via ffprobe, or null if unavailable. */
export async function probeDuration(videoPath: string): Promise<number | null> {
  const cmd = [
    "ffprobe", "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1",
    pathNorm(videoPath),
  ];
  let out;
  try {
    out = await runProcess(cmd, { timeout: 30 });
  } catch (exc) {
    const n = (exc as Error)?.name;
    if (n === "FileNotFoundError" || n === "TimeoutExpired") {
      logger.warning("ffprobe unavailable (%s); using base sampling fps", excStr(exc));
      return null;
    }
    throw exc;
  }
  if (out.returncode !== 0) {
    logger.warning("ffprobe failed: %s", cpSlice(out.stderr || "", -200));
    return null;
  }
  try {
    return pyFloat(pyStrip(out.stdout || ""));
  } catch {
    return null;
  }
}

/** (width, height) of the video's first video stream via ffprobe, or null. */
export async function probeDimensions(videoPath: string): Promise<[number, number] | null> {
  const cmd = [
    "ffprobe", "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "stream=width,height",
    "-of", "csv=p=0:s=x",
    pathNorm(videoPath),
  ];
  let out;
  try {
    out = await runProcess(cmd, { timeout: 30 });
  } catch (exc) {
    const n = (exc as Error)?.name;
    if (n === "FileNotFoundError" || n === "TimeoutExpired") return null;
    throw exc;
  }
  if (out.returncode !== 0) return null;
  try {
    const parts = pyStrip(out.stdout || "").split("x").slice(0, 2);
    if (parts.length < 2) return null;
    return [pyInt(parts[0]), pyInt(parts[1])];
  } catch {
    return null;
  }
}

/**
 * Pick a sampling fps so the projected frame count fits within `maxFrames`.
 *
 * Short clips keep `baseFps`. For longer clips the sampling interval steps up in whole seconds
 * (base 1 fps -> 1 frame / 2s -> 1 / 3s ...) until the count fits, so a single assertion request
 * stays under the API's per-request image cap. The frame count is derived from the probed duration
 * BEFORE any frames are written to disk.
 */
export function adaptiveFps(durationS: number | null, baseFps: number, maxFrames: number): number {
  if (!truthy(durationS) || durationS! <= 0 || maxFrames <= 0) return baseFps;
  if (durationS! * baseFps <= maxFrames) return baseFps;
  const interval = pyCeil(durationS! / maxFrames);  // whole seconds per frame
  return 1.0 / interval;
}

/** Map an image file extension to its API media type (default jpeg). */
export function mediaType(path: string): string {
  const m: Record<string, string> = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
  };
  return m[pathSuffix(path).toLowerCase()] ?? "image/jpeg";
}

/**
 * Build message content blocks with each image PRECEDED by its caption.
 *
 * Interleaving a short description immediately before each base64 image gives the model explicit
 * context for that specific picture ("this is the Coin symbol", "this is gameplay frame 7")
 * instead of leaving it to guess from position — which materially improves recognition when many
 * reference images are attached.
 */
export function imageContentBlocks(imagePaths: string[], captions: Array<string | null> | null = null): Dict[] {
  const blocks: Dict[] = [];
  imagePaths.forEach((p, i) => {
    const cap = captions && i < captions.length ? captions[i] : null;
    if (cap) blocks.push({ type: "text", text: cap });
    const b64 = readFileSync(p).toString("base64");
    blocks.push({ type: "image", source: { type: "base64", media_type: mediaType(p), data: b64 } });
  });
  return blocks;
}

// ---------------------------------------------------------------------------
// Shared CLI client
// ---------------------------------------------------------------------------

/** Shared Claude Code CLI invocation + reference-asset manifest writing. */
export class ClaudeCliClient {
  readonly _reference: SymbolReference | null;
  protected _callCount = 0;
  readonly _fps: number;
  readonly _maxFrames: number;
  readonly _cliPath: string;
  readonly _model: string;
  readonly _timeout: number;

  constructor(reference: SymbolReference | null = null) {
    this._reference = reference;

    // Resolve config from the environment once.
    this._fps = ClaudeCliClient.envFloat("CLAUDE_AI_FPS", DEFAULT_FPS);
    // Max frames sent in one video assertion — long clips have their sampling fps reduced
    // adaptively to honor this so a single request stays under the API's per-request image cap
    // (see adaptiveFps).
    this._maxFrames = pyInt(ClaudeCliClient.envFloat("CLAUDE_AI_MAX_FRAMES", DEFAULT_MAX_FRAMES));
    this._cliPath = pyStrip(process.env.CLAUDE_CLI_PATH ?? "") || DEFAULT_CLI;
    // Default to Sonnet — fast/cheap enough for frame reading; the CLI would otherwise inherit its
    // own (pricier) default model. Override via CLAUDE_MODEL.
    this._model = pyStrip(process.env.CLAUDE_MODEL ?? "") || DEFAULT_MODEL;
    this._timeout = pyInt(ClaudeCliClient.envFloat("CLAUDE_CLI_TIMEOUT", DEFAULT_TIMEOUT));
  }

  get callCount(): number {
    return this._callCount;
  }

  /** The model id used for asserts (e.g. `sonnet`). Part of the GeminiVideoHelper-compatible
   * interface the runner relies on for per-test/per-run cost bookkeeping. */
  get model(): string {
    return this._model;
  }

  /** USD cost of a single call — always 0.0 (local Claude Code auth is unmetered). */
  costForTokens(_inputTokens: number, _outputTokens: number): number {
    return 0.0;
  }

  static envFloat(name: string, dflt: number): number {
    const raw = pyStrip(process.env[name] ?? "");
    if (!raw) return dflt;
    try {
      return pyFloat(raw);
    } catch {
      logger.warning("Ignoring non-numeric %s=%r; using %s", name, raw, dflt);
      return dflt;
    }
  }

  // -- reference manifest --

  /**
   * Write `ASSETS.md` describing each reference asset + its image path.
   *
   * The sub-agent reads this first to learn the canonical appearance of every symbol/UI element
   * before judging the frames. Returns the path, or null when there is no reference pack. A
   * per-call `reference` (scoped selection) overrides the constructor default.
   */
  writeAssetsFile(workDir: string, reference: SymbolReference | null = null): string | null {
    const ref = reference !== null ? reference : this._reference;
    if (ref === null || ref.isEmpty) return null;

    mkdirSync(workDir, { recursive: true });
    const lines = [
      "# Reference assets",
      "",
      ref.header ?? "Ground-truth game symbols and UI elements.",
      "",
      "Read each image below to learn what the asset looks like before "
        + "judging the frames/screenshot. These are reference context only — do "
        + "not describe or judge the reference images themselves.",
      "",
    ];
    for (const [imgPath, label] of ref.items) {
      lines.push(`- ${label}`);
      lines.push(`  image: ${resolvePath(imgPath)}`);
    }
    lines.push("");

    const assetsPath = pathJoin(workDir, "ASSETS.md");
    writeText(assetsPath, lines.join("\n"));
    return assetsPath;
  }

  // -- CLI invocation --

  /**
   * Resolve the claude executable (PATH, then the native ~/.local/bin install).
   *
   * Tries the configured name on PATH (plus `.cmd` on Windows), then the native installer location
   * `~/.local/bin/claude(.exe)`. The latter matters because Claude Code migrating from the npm
   * global to the native install removes the old `claude.CMD` PATH shim, which otherwise makes
   * every spawn fail with `[WinError 2] file not found`.
   */
  resolveCli(): string {
    for (const candidate of [this._cliPath, `${this._cliPath}.cmd`]) {
      const found = which(candidate);
      if (found) return found;
    }
    // Native-install fallback (only when the user hasn't pinned a custom path).
    if (this._cliPath === "claude" || this._cliPath === "claude.exe") {
      for (const native of [pathJoin(homedir(), ".local", "bin", "claude.exe"), pathJoin(homedir(), ".local", "bin", "claude")]) {
        if (existsSync(native)) return native;
      }
    }
    // Fall back to the raw name and let the spawn raise FileNotFoundError, which the caller turns
    // into a failure result.
    return this._cliPath;
  }

  /** The `-p` command line for the Read/Glob agent. */
  agentCommand(cli: string): string[] {
    const cmd = [cli, "-p", "--output-format", "json", "--allowedTools", "Read", "Glob"];
    if (this._model) cmd.push("--model", this._model);
    return cmd;
  }

  /**
   * Run the Claude Code CLI sub-agent in `workDir` and parse its verdict.
   *
   * Returns `[data, rawText]` where `data` is the extracted JSON object (or null if extraction
   * failed) and `rawText` is the agent's final reply. Raises on transport/subprocess errors —
   * callers wrap.
   */
  async runAgent(workDir: string, prompt: string, outputSchema: Dict): Promise<[Dict | null, string]> {
    const fullPrompt = buildSchemaPrompt(prompt, outputSchema, true);
    const cli = this.resolveCli();
    // Pass the prompt via STDIN, not as a -p argument. On Windows the CLI is a `claude.cmd` batch
    // wrapper, and a long/multi-line -p value mangles the following flags (e.g. --output-format is
    // dropped, so the envelope is lost). Keeping only stable flags on the command line and feeding
    // the prompt on stdin is robust and also avoids command-line length limits.
    const cmd = this.agentCommand(cli);

    const proc = await runProcess(cmd, {
      cwd: pathNorm(workDir),
      input: fullPrompt,
      timeout: this._timeout,
      env: claudeSubprocessEnv(),
    });

    // The CLI prints a JSON envelope on stdout with --output-format json.
    let envelope: unknown;
    try {
      envelope = jsonLoads(pyStrip(proc.stdout || ""));
    } catch (e) {
      if (!(e instanceof JSONDecodeError)) throw e;
      // Non-JSON stdout — surface stderr/stdout for debugging.
      const raw = pyStrip(proc.stdout || proc.stderr || "");
      if (proc.returncode !== 0) {
        throw pyError("RuntimeError", `claude CLI exited ${proc.returncode}: ${cpSlice(proc.stderr || "", -300)}`);
      }
      return [null, raw];
    }

    if (isDict(envelope) && truthy(envelope.is_error)) {
      throw pyError("RuntimeError", `claude CLI reported an error: ${strOf(truthy(envelope.result) ? envelope.result : envelope)}`);
    }

    let rawText = "";
    if (isDict(envelope)) rawText = strOf(Object.prototype.hasOwnProperty.call(envelope, "result") ? envelope.result : "");
    const parsed = extractJson(rawText);
    return [parsed, rawText];
  }

  /** The command line for the inline (stream-json) one-shot call. */
  inlineCommand(cli: string): string[] {
    const cmd = [cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"];
    if (this._model) cmd.push("--model", this._model);
    return cmd;
  }

  /**
   * Run a one-shot Claude call with images attached INLINE (base64), no Read tool.
   *
   * Sends a single `stream-json` user message whose content is the text prompt followed by one
   * image block per path, in order, then reads events until the `result`. One model pass over all
   * images — far faster than the agent opening each file with the Read tool. The caller must keep
   * the image count under the API's per-request cap (the adaptive sampler guarantees this). Raises
   * on timeout / CLI error (callers wrap into a failure result).
   */
  async runAgentInline(
    workDir: string, prompt: string, outputSchema: Dict, imagePaths: string[], captions: Array<string | null> | null = null,
  ): Promise<[Dict | null, string]> {
    const fullPrompt = buildSchemaPrompt(prompt, outputSchema, true);
    const cli = this.resolveCli();
    const cmd = this.inlineCommand(cli);

    const content: Dict[] = [{ type: "text", text: fullPrompt }];
    content.push(...imageContentBlocks(imagePaths, captions));
    const message = pyJson({ type: "user", message: { role: "user", content } });

    // Single message then EOF (stdin closed) -> the print-mode process emits its result and exits.
    let stdout: string;
    try {
      ({ stdout } = await runProcess(cmd, {
        cwd: pathNorm(workDir),
        input: `${message}\n`,
        timeout: this._timeout,
        env: claudeSubprocessEnv(),
      }));
    } catch (e) {
      if ((e as Error)?.name === "TimeoutExpired") {
        throw pyError("RuntimeError", `claude inline CLI timed out after ${this._timeout}s`);
      }
      throw e;
    }

    for (let line of pySplitlines(stdout || "")) {
      line = pyStrip(line);
      if (!line) continue;
      let ev: unknown;
      try {
        ev = jsonLoads(line);
      } catch (e) {
        if (e instanceof JSONDecodeError) continue;
        throw e;
      }
      if (isDict(ev) && ev.type === "result") {
        if (truthy(ev.is_error)) {
          throw pyError("RuntimeError", `claude CLI reported an error: ${strOf(truthy(ev.result) ? ev.result : ev)}`);
        }
        const rawText = strOf(Object.prototype.hasOwnProperty.call(ev, "result") ? ev.result : "");
        return [extractJson(rawText), rawText];
      }
    }
    return [null, cpSlice(stdout || "", 0, 500)];
  }
}

/** `Path.write_text(text, encoding="utf-8")`: text mode writes "\n" as the platform newline. */
function writeText(path: string, text: string): void {
  writeFileSync(path, process.platform === "win32" ? text.replace(/\n/g, "\r\n") : text, "utf-8");
}

// ---------------------------------------------------------------------------
// Video helper
// ---------------------------------------------------------------------------

/** Video analysis via a frame-sampling Claude Code CLI sub-agent. Interface-compatible with
 * `GeminiVideoHelper`. */
export class ClaudeVideoHelper extends ClaudeCliClient {
  // -- Context management (no-ops, kept for API compatibility) --

  /** No-op. Reference assets are attached per call. */
  createCachedContext(_imagePaths: string[]): string {
    return "claude";
  }

  releaseContext(_contextId: string): void {}

  releaseAllContexts(): void {}

  get activeContextCount(): number {
    return 0;
  }

  /** The frame budget for one inline request: bounded by BOTH the per-request image cap AND a
   * token budget. */
  frameBudget(nRefs: number, dims: [number, number] | null): { tokensPerFrame: number; tokenBudget: number; frameBudget: number } {
    const tokensPerFrame = dims ? floorDiv(dims[0] * dims[1], 750) : 650;
    const tokenBudget = pyInt(ClaudeCliClient.envFloat("CLAUDE_AI_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET));
    const reserve = nRefs * 500 + 4000;  // reference images + the text prompt/captions
    const framesByTokens = Math.max(1, floorDiv(tokenBudget - reserve, Math.max(tokensPerFrame, 1)));
    const frameBudget = Math.max(1, Math.min(this._maxFrames, HARD_IMAGE_CAP - nRefs - 2, framesByTokens));
    return { tokensPerFrame, tokenBudget, frameBudget };
  }

  /**
   * Sample `videoPath` into frames and judge them with a Claude sub-agent. `highFps` / `timestamp`
   * are accepted for interface parity with the Gemini montage helper; this helper samples stills
   * (no composite montage), so they are advisory only.
   */
  async analyseVideo(
    videoPath: string, contextId: string | null, prompt: string, outputSchema: Dict,
    opts: { refImages?: string[] | null; reference?: SymbolReference | null; highFps?: boolean; timestamp?: boolean } = {},
  ): Promise<VideoResult> {
    void contextId;
    this._callCount += 1;

    const workDir = pathJoin(pathParent(videoPath), `${pathStem(videoPath)}-claude`);

    // Reference assets are attached inline alongside the frames and also count toward the
    // per-request image cap, so resolve them first and reserve room for them when budgeting frames
    // (the full symbol pack can be ~20+ images). A per-call `reference` (scoped selection)
    // overrides the constructor default.
    const reference = opts.reference ?? null;
    const ref = reference !== null ? reference : this._reference;
    let refItems: Array<[string, string]> = [];
    if (ref !== null && !ref.isEmpty) {
      refItems = ref.items.filter(([img]) => existsSync(img)).map(([img, label]) => [pathNorm(img), label]);
    }
    const refPaths = refItems.map(([img]) => img);
    const nRefs = refPaths.length;
    // Frame budget bounded by BOTH the per-request image cap AND a token budget: a long clip at
    // 1 fps can blow the model's context window even under 100 images ("Prompt is too long"), so
    // also cap frames by estimated tokens.
    const dims = await probeDimensions(videoPath);
    const { tokensPerFrame, tokenBudget, frameBudget } = this.frameBudget(nRefs, dims);

    // Dynamically pick the sampling fps from the clip length so frames + references fit both
    // budgets. Short clips keep the base fps; long clips step the fps down.
    const duration = await probeDuration(videoPath);
    const fps = adaptiveFps(duration, this._fps, frameBudget);
    logger.info("Video sampling: duration=%ss base=%sfps -> %sfps (budget=%s frames; %d refs, ~%d tok/frame, token_budget=%d)",
      truthy(duration) ? pyRound(duration!, 1) : "?", this._fps, pyRound(fps, 3), frameBudget, nRefs, tokensPerFrame, tokenBudget);

    if (!(await sampleFrames(videoPath, workDir, fps))) {
      return geminiResult(false, null, `ffmpeg frame sampling failed (fps=${floatStr(fps)})`);
    }

    // Attach the frames + reference assets INLINE as image blocks — one model pass, no per-frame
    // Read round-trips. Order: reference assets first (so the model learns each symbol), then
    // frames in chronological order.
    // sorted(Path...): Windows paths compare case-folded
    const key = (p: string) => (process.platform === "win32" ? p.toLowerCase() : p);
    const framePaths = frameFiles(workDir).sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
    const nFrames = framePaths.length;
    // Caption each image: reference descriptions first, then chronological frame indices.
    const captions = [
      ...refItems.map(([, label]) => `This image shows ${label}`),
      ...framePaths.map((_, i) => `This is gameplay frame ${i + 1}/${nFrames} (chronological order)`),
    ];
    const agentPrompt = this.buildInlineVideoPrompt(prompt, refItems, nFrames);
    logger.info("Video assert (inline): %d frame(s) + %d reference image(s) -> %d total images",
      nFrames, refPaths.length, nFrames + refPaths.length);

    let data: Dict | null;
    let rawText: string;
    try {
      [data, rawText] = await this.runAgentInline(workDir, agentPrompt, outputSchema, [...refPaths, ...framePaths], captions);
    } catch (exc) {  // resilience contract: never raise
      logger.warning("Claude video analysis failed: %s", excStr(exc));
      return geminiResult(false, null, `Claude CLI error: ${excStr(exc)}`);
    }

    if (data === null) {
      return geminiResult(false, null, `Claude response had no parseable JSON. Raw: ${cpSlice(rawText, 0, 200)}`, { raw_text: rawText });
    }
    return geminiResult(true, data, null, { input_tokens: 0, output_tokens: 0, raw_text: rawText });
  }

  /** Prepend agent instructions (read assets, then frames in order) to `prompt`. (The Read-tool
   * path; the inline path below replaced it.) */
  buildVideoPrompt(prompt: string): string {
    const hasAssets = this._reference !== null && !this._reference.isEmpty;
    const steps: string[] = [];
    if (hasAssets) {
      steps.push("1. Read `ASSETS.md` in this directory and use the Read tool on each "
        + "reference image it lists, to learn the canonical appearance of each "
        + "game symbol and UI element.");
    }
    steps.push(`${steps.length + 1}. List the \`frame-*.jpg\` files (e.g. with Glob) and Read `
      + `them in NUMERICAL ORDER. They are sampled at ${floatStr(this._fps)} fps from a `
      + "gameplay video; `frame-0001.jpg` is the earliest moment and the frames are "
      + "chronological. Reason about what changes across the sequence.");
    steps.push(`${steps.length + 1}. Then answer the task below.`);
    const instructions = "You are analysing a short gameplay video that has been sampled into image "
      + `frames in your current directory.\n\n${steps.join("\n")}`;
    return `${instructions}\n\nTask:\n${prompt}`;
  }

  /**
   * Prompt for the inline path: describe the attached images, then the task. Image order matches
   * what `analyseVideo` attaches — reference assets first (so the model learns what each game
   * symbol looks like), then the gameplay frames in chronological order.
   */
  buildInlineVideoPrompt(prompt: string, refItems: Array<[string, string]>, nFrames: number): string {
    const lines = [
      "You are analysing a short gameplay video supplied as still frames attached to "
        + "this message (no files to open). Each attached image is IMMEDIATELY PRECEDED by "
        + "a line saying what it is.",
    ];
    if (refItems.length) {
      lines.push(`The first ${refItems.length} are REFERENCE images showing how each game symbol / `
        + "UI element is SUPPOSED to look — treat them as the ground-truth expected appearance "
        + "for recognising and judging those elements in the frames; do not report on the "
        + "reference images themselves.");
    }
    lines.push(`The remaining ${nFrames} are the gameplay VIDEO FRAMES in CHRONOLOGICAL ORDER `
      + "(earliest first). Reason about what changes from frame to frame.");
    lines.push("");
    lines.push(`Task:\n${prompt}`);
    return lines.join("\n");
  }

  /** Gemini-compatible cost summary (zero cost — local Claude Code auth). */
  costSummary(): Dict {
    return {
      model: "claude",
      resolution: "n/a",
      gemini_video_calls: this._callCount,
      gemini_input_tokens: 0,
      gemini_output_tokens: 0,
      estimated_cost_usd: 0.0,
    };
  }
}

// ---------------------------------------------------------------------------
// Image helper
// ---------------------------------------------------------------------------

/** Image analysis via a Claude Code CLI sub-agent. Interface-compatible with `GeminiImageHelper`. */
export class ClaudeImageHelper extends ClaudeCliClient {
  /** Judge `imagePath` (a screenshot) with a Claude sub-agent. */
  async analyseImage(
    imagePath: string, prompt: string, outputSchema: Dict,
    opts: { extraImages?: string[] | null; reference?: SymbolReference | null } = {},
  ): Promise<GeminiResult> {
    this._callCount += 1;

    const workDir = pathJoin(pathParent(imagePath), `${pathStem(imagePath)}-claude`);
    mkdirSync(workDir, { recursive: true });

    // Copy the screenshot (and any extras) into the work dir so the agent reads them by simple
    // relative names from its cwd.
    const extraNames: string[] = [];
    let copying = imagePath;
    try {
      copyFileSync(imagePath, pathJoin(workDir, "screenshot.png"));
      (opts.extraImages ?? []).forEach((extra, k) => {
        const name = `screenshot-extra-${k + 1}.png`;
        copying = extra;
        copyFileSync(extra, pathJoin(workDir, name));
        extraNames.push(name);
      });
    } catch (exc) {
      const text = osErrorText(exc, copying);
      logger.warning("Claude image analysis setup failed: %s", text);
      return geminiResult(false, null, `Could not stage screenshot for Claude: ${text}`);
    }

    // No ASSETS.md: the reference descriptions + paths go straight into the prompt so the agent's
    // only tool calls are reading the image files themselves.
    const agentPrompt = this.buildImagePrompt(prompt, extraNames, opts.reference ?? null);

    let data: Dict | null;
    let rawText: string;
    try {
      [data, rawText] = await this.runAgent(workDir, agentPrompt, outputSchema);
    } catch (exc) {  // resilience contract: never raise
      logger.warning("Claude image analysis failed: %s", excStr(exc));
      return geminiResult(false, null, `Claude CLI error: ${excStr(exc)}`);
    }

    if (data === null) {
      return geminiResult(false, null, `Claude response had no parseable JSON. Raw: ${cpSlice(rawText, 0, 200)}`, { raw_text: rawText });
    }
    return geminiResult(true, data, null, { input_tokens: 0, output_tokens: 0, raw_text: rawText });
  }

  /**
   * Build the full agent prompt for one image assert — all context inline.
   *
   * Every reference image's description AND absolute path are embedded here, so the agent never
   * opens a separate manifest: its only tool calls are reading the image files. The references are
   * framed as the NORMAL, expected appearance — the canonical, correct way the real game and its UI
   * are meant to look in this state — so the agent judges the screenshot under test against them.
   * The per-call `reference` (scoped selection) overrides the constructor default.
   */
  buildImagePrompt(prompt: string, extraNames: string[], reference: SymbolReference | null = null): string {
    const ref = reference !== null ? reference : this._reference;
    const hasRefs = ref !== null && !ref.isEmpty;

    const steps: string[] = [];
    if (hasRefs) {
      const refLines: string[] = [];
      for (const [imgPath, label] of ref!.items) {
        refLines.push(`     - ${label}`);
        refLines.push(`       file: ${resolvePath(imgPath)}`);
      }
      steps.push("Use the Read tool to open each REFERENCE image file listed below. Each "
        + "one is the NORMAL, expected appearance — the canonical, correct way the "
        + "real game and its UI are meant to look in this state. Study them so you "
        + "know what 'correct' looks like before judging anything:\n"
        + refLines.join("\n"));
    }
    let shots = "`screenshot.png`";
    if (extraNames.length) shots += ` and ${extraNames.map((n) => `\`${n}\``).join(", ")}`;
    steps.push(`Read ${shots} — the actual screenshot under test, captured from a live run.`);
    if (hasRefs) {
      steps.push("Answer the task below, comparing the screenshot under test against the "
        + "reference image(s) above.");
    } else {
      steps.push("Then answer the task below.");
    }

    const numbered = steps.map((s, i) => `${i + 1}. ${s}`).join("\n");
    const instructions = `You are analysing a game screenshot in your current directory.\n\n${numbered}`;
    return `${instructions}\n\nTask:\n${prompt}`;
  }

  /** Gemini-compatible cost summary (zero cost — local Claude Code auth). */
  costSummary(): Dict {
    return {
      model: "claude",
      gemini_image_calls: this._callCount,
      gemini_input_tokens: 0,
      gemini_output_tokens: 0,
      estimated_cost_usd: 0.0,
    };
  }
}
