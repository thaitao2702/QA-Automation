/**
 * Gemini video analysis helper (port of framework/helpers/gemini_video.py).
 *
 * Before upload, the trimmed clip is re-encoded into a **montage** video: every output frame is a
 * composite of `MONTAGE_COLS` sub-frames spaced `MONTAGE_INTERVAL_S` apart, tiled LEFT->RIGHT in
 * time. Gemini samples video at ~1 frame/second and bills a flat ~70 tokens/frame at LOW/MEDIUM
 * resolution, so packing 4 sub-frames into each sampled frame buys 0.25s temporal resolution at
 * 1fps token cost (4x the timing detail, same price). The prompt carries an explanation of the
 * layout so the model reads the panels left->right, then continues frame-to-frame in time.
 *
 * Pricing — Gemini 3 Flash Preview (used for video — lower token/frame cost): input $0.50 / 1M,
 * output $3.00 / 1M. Video token cost per frame (1 frame/second): Gemini 3 Flash 70 tokens/frame
 * (LOW/MEDIUM resolution — identical cost); Gemini 2.5 Flash 256 tokens/frame (medium).
 */
import { existsSync } from "node:fs";
import { pyRound, truthy, type Dict } from "../../py.js";
import { geminiResult, geminiSchemaHint, imagePart, referenceParts, stripMarkdownFence, usageOf, type GeminiResult, type Part } from "./gemini.js";
import { getPricing, type ModelPricing } from "./gemini-pricing.js";
import { FileNotFoundError, runProcess, sleep, TimeoutExpired } from "./proc.js";
import { cpSlice, excStr, jsonLoads, JSONDecodeError, keyError, logger, pathJoin, pathName, pathNorm, pathParent, pathStem, pathSuffix, pyFixed } from "./py-support.js";
import type { SymbolReference } from "./symbol-reference.js";

const DEFAULT_MODEL = "gemini-3-flash-preview";
const DEFAULT_RESOLUTION = "MEDIUM";

// --- Montage packing --------------------------------------------------------
// Each uploaded frame is a horizontal strip of MONTAGE_COLS sub-frames captured MONTAGE_INTERVAL_S
// apart. At 1fps Gemini sampling this yields MONTAGE_INTERVAL_S temporal resolution for the
// ~70-token/frame (LOW/MEDIUM) price of a single frame.
export const MONTAGE_COLS = 4;                       // sub-frames per composite frame (left->right in time)
export const MONTAGE_INTERVAL_S = 0.25;              // wall-clock gap between adjacent sub-frames
export const MONTAGE_SAMPLE_FPS = pyRound(1.0 / MONTAGE_INTERVAL_S);  // frames pulled before tiling (=4)
export const MONTAGE_PADDING_PX = 6;                 // white gutter between sub-frames (helps segmentation)
export const FFMPEG_TIMEOUT = 120;                   // seconds for the montage re-encode

export const MONTAGE_HIGH_FPS = 16;                  // `high_fps`: 4 composites/sec x 4 sub-frames = 16fps effective

/** Outcome of a single Gemini video analysis call. Field names are the Python dataclass's. */
export type VideoResult = GeminiResult;

/** First available TrueType font for the drawtext timestamp overlay (best-effort, cross-OS). */
export function fontFile(): string | null {
  for (const p of ["C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf"]) {
    if (existsSync(p)) return p;
  }
  return null;
}

/** drawtext filter burning the clip-relative timestamp into the top-left of each sub-frame. */
export function drawtextTs(): string {
  const parts = ["drawtext=text='t\\=%{pts\\:hms}'", "fontcolor=yellow", "fontsize=18",
    "x=5", "y=5", "box=1", "boxcolor=black@0.5"];
  const font = fontFile();
  if (font) {                                         // filtergraph path escaping: \ -> / and : -> \:
    parts.splice(1, 0, `fontfile='${font.replace(/\\/g, "/").replace(/:/g, "\\:")}'`);
  }
  return parts.join(":");
}

/**
 * ffmpeg -vf chain: sample at `sampleFps`, optionally burn a per-sub-frame timestamp, tile N
 * sub-frames left->right, even dims.
 *
 * `tile=Nx1` consumes N consecutive sampled frames and emits one composite, so the output frame
 * rate is sampleFps / N. Default sampleFps=4 -> 1 composite/sec, 0.25s detail (4fps effective);
 * `high_fps` uses sampleFps=16 -> 4 composites/sec, 0.0625s detail (16fps effective).
 */
export function montageFilter(sampleFps: number = MONTAGE_SAMPLE_FPS, timestamp = false): string {
  const stamp = timestamp ? `${drawtextTs()},` : "";
  return `fps=${sampleFps},`
    + `${stamp}`
    + `tile=${MONTAGE_COLS}x1:padding=${MONTAGE_PADDING_PX}:color=white,`
    + "scale=trunc(iw/2)*2:trunc(ih/2)*2";
}

/** Explain the montage layout to the model so it reads sub-frames left->right in time. */
export function montagePreamble(sampleFps: number = MONTAGE_SAMPLE_FPS, timestamp = false): string {
  const n = MONTAGE_COLS;
  const interval = 1.0 / sampleFps;
  const stamps = Array.from({ length: n }, (_, i) => `+${pyFixed(i * interval, 3)}s`).join(", ");
  const last = `+${pyFixed((n - 1) * interval, 3)}s`;
  const tsNote = timestamp
    ? " Each sub-frame ALSO has a burned-in timestamp in its top-left corner (yellow, "
      + "'t=HH:MM:SS.mmm') showing that panel's time from the start of the clip — use these exact "
      + "timestamps to reason about precisely WHEN things happen and to line up with any ACTION "
      + "TIMELINE given below."
    : "";
  return "HOW TO READ THIS VIDEO (IMPORTANT):\n"
    + "To give you finer timing detail cheaply, every single frame of this video is a "
    + `COMPOSITE image built from ${n} sub-frames captured ${pyFixed(interval, 3)} seconds apart, `
    + "arranged LEFT TO RIGHT in chronological order and separated by thin white gutters. "
    + "Within one composite frame, read the panels left-to-right: the leftmost panel is the "
    + `earliest moment (+0.000s) and each panel to its right is ${pyFixed(interval, 3)}s later `
    + `(${stamps}); the rightmost panel is ${last}. The NEXT composite frame continues in `
    + "time — its leftmost panel is the moment immediately after the previous frame's "
    + "rightmost panel. So the true chronological order is: frame1 left→right, then frame2 "
    + `left→right, and so on — treat the whole clip as one continuous ${sampleFps}-frames-per-second `
    + "sequence when reasoning about motion, transitions, timing, and what happens "
    + `before/after.${tsNote}`;
}

/** The ffmpeg command `buildMontage` runs. */
export function montageCommand(source: string, dest: string, sampleFps: number = MONTAGE_SAMPLE_FPS, timestamp = false): string[] {
  return [
    "ffmpeg", "-y",
    "-i", pathNorm(source),
    "-vf", montageFilter(sampleFps, timestamp),
    "-an",                    // drop audio
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    pathNorm(dest),
  ];
}

/**
 * Re-encode `source` into the montage clip described by `montageFilter`.
 *
 * Returns true on success, false (logged) on missing ffmpeg, timeout, non-zero exit, or missing
 * output — the caller then falls back to uploading the raw clip.
 */
export async function buildMontage(source: string, dest: string, sampleFps: number = MONTAGE_SAMPLE_FPS, timestamp = false): Promise<boolean> {
  if (!existsSync(source)) {
    logger.error("Montage source not found: %s", pathNorm(source));
    return false;
  }
  const cmd = montageCommand(source, dest, sampleFps, timestamp);
  let result;
  try {
    result = await runProcess(cmd, { timeout: FFMPEG_TIMEOUT });
  } catch (e) {
    if (e instanceof FileNotFoundError || (e as Error)?.name === "FileNotFoundError") {
      logger.error("ffmpeg not found — cannot build montage video. Install from "
        + "https://ffmpeg.org/download.html or via your OS package manager.");
      return false;
    }
    if (e instanceof TimeoutExpired || (e as Error)?.name === "TimeoutExpired") {
      logger.error("ffmpeg timed out building montage from %s", pathNorm(source));
      return false;
    }
    throw e;
  }
  if (result.returncode !== 0) {
    logger.error("ffmpeg montage failed (exit %d): %s", result.returncode, cpSlice(result.stderr || "", -500));
    return false;
  }
  if (!existsSync(dest)) {
    logger.error("ffmpeg produced no montage output for %s", pathNorm(source));
    return false;
  }
  return true;
}

/** Python's `mimetypes.guess_type` for the two clip kinds the helper uploads. */
const UPLOAD_MIME: Record<string, string> = { ".mp4": "video/mp4", ".webm": "video/webm" };

/**
 * Gemini video analysis using the official SDK. Uploads video via the Files API, waits for
 * processing, then sends the video with a prompt for structured analysis.
 */
export class GeminiVideoHelper {
  readonly apiKey: string;
  readonly _model: string;
  readonly _resolution: string;
  private readonly _pricing: ModelPricing;
  // SymbolReference — its labeled images are prepended to EVERY request so the model has
  // ground-truth context.
  readonly _reference: SymbolReference | null;
  private _totalInputTokens = 0;
  private _totalOutputTokens = 0;
  private _videoCallCount = 0;

  constructor(apiKey: string, model: string | null = null, resolution: string | null = null, reference: SymbolReference | null = null) {
    this.apiKey = apiKey;
    this._model = model || DEFAULT_MODEL;
    this._resolution = (resolution || DEFAULT_RESOLUTION).toUpperCase();
    this._pricing = getPricing(this._model);
    this._reference = reference;
  }

  get totalInputTokens(): number {
    return this._totalInputTokens;
  }

  get totalOutputTokens(): number {
    return this._totalOutputTokens;
  }

  get estimatedCostUsd(): number {
    return this._totalInputTokens * this._pricing.inputPerToken + this._totalOutputTokens * this._pricing.outputPerToken;
  }

  /** The video model id (e.g. `gemini-3-flash-preview`). */
  get model(): string {
    return this._model;
  }

  /** USD cost of a single call with the given token counts (same pricing as the cumulative estimate). */
  costForTokens(inputTokens: number, outputTokens: number): number {
    return inputTokens * this._pricing.inputPerToken + outputTokens * this._pricing.outputPerToken;
  }

  // -- Context management (no-op — kept for API compatibility) --

  /** No-op. Context caching removed — ref images passed inline. */
  createCachedContext(_imagePaths: string[]): string {
    return "inline";
  }

  releaseContext(_contextId: string): void {}

  releaseAllContexts(): void {}

  get activeContextCount(): number {
    return 0;
  }

  /**
   * Upload `videoPath` to Gemini and analyse it. `contextId` is ignored (kept for API
   * compatibility); `refImages` are optional comparison images sent alongside the video.
   */
  async analyseVideo(
    videoPath: string, contextId: string | null, prompt: string, outputSchema: Dict,
    opts: { refImages?: string[] | null; reference?: SymbolReference | null; highFps?: boolean; timestamp?: boolean } = {},
  ): Promise<VideoResult> {
    void contextId;
    this._videoCallCount += 1;
    try {
      const response = await this.callVideoApi(videoPath, prompt, outputSchema, opts.refImages ?? null,
        opts.reference ?? null, !!opts.highFps, !!opts.timestamp);
      return this.parseResponse(response);
    } catch (exc) {
      logger.warning("Gemini video API call failed: %s", excStr(exc));
      return geminiResult(false, null, `Gemini video API error: ${excStr(exc)}`);
    }
  }

  /** A cost summary dict compatible with `summary.json`. */
  costSummary(): Dict {
    return {
      model: this._model,
      resolution: this._resolution,
      gemini_video_calls: this._videoCallCount,
      gemini_input_tokens: this._totalInputTokens,
      gemini_output_tokens: this._totalOutputTokens,
      estimated_cost_usd: pyRound(this.estimatedCostUsd, 6),
    };
  }

  /** Upload a file to Gemini and wait for it to become ACTIVE. */
  async uploadAndWait(client: any, path: string): Promise<any> {
    const mimeType = UPLOAD_MIME[pathSuffix(path).toLowerCase()];
    let file = await client.files.upload({ file: pathNorm(path), ...(mimeType ? { config: { mimeType } } : {}) });

    // Video files need processing before use
    while (!file.state || file.state !== "ACTIVE") {
      logger.debug("Processing file... state=%s", file.state);
      await sleep(5);
      file = await client.files.get({ name: file.name });
    }
    return file;
  }

  /** Upload video, build contents, and call Gemini. */
  async callVideoApi(
    videoPath: string, prompt: string, outputSchema: Dict, refImages: string[] | null = null,
    reference: SymbolReference | null = null, highFps = false, timestamp = false,
  ): Promise<any> {
    const { GoogleGenAI, MediaResolution } = await import("@google/genai");
    const resolutionMap: Record<string, unknown> = {
      LOW: MediaResolution.MEDIA_RESOLUTION_LOW,
      MEDIUM: MediaResolution.MEDIA_RESOLUTION_MEDIUM,
      HIGH: MediaResolution.MEDIA_RESOLUTION_HIGH,
    };

    const client = new GoogleGenAI({ apiKey: this.apiKey });

    // Re-encode into the montage clip (sub-frames packed left->right) so one sampled frame carries
    // fine temporal detail at ~70 tokens. `high_fps` raises sampling 4x (16fps effective) for fast
    // action; `timestamp` burns a per-sub-frame clock. On ffmpeg failure fall back to the raw clip
    // AND drop the montage explanation; if only the timestamp pass fails (e.g. no drawtext font),
    // retry without it so we keep the sub-frame packing.
    const sampleFps = highFps ? MONTAGE_HIGH_FPS : MONTAGE_SAMPLE_FPS;
    const montagePath = pathJoin(pathParent(videoPath), `${pathStem(videoPath)}-montage.mp4`);
    let montageOk = await buildMontage(videoPath, montagePath, sampleFps, timestamp);
    if (!montageOk && timestamp) {
      logger.warning("Timestamped montage failed for %s — retrying without timestamp", pathName(videoPath));
      timestamp = false;
      montageOk = await buildMontage(videoPath, montagePath, sampleFps, false);
    }
    const uploadPath = montageOk ? montagePath : videoPath;
    if (!montageOk) {
      logger.warning("Montage build failed for %s — uploading raw clip (no sub-frame packing)", pathName(videoPath));
    }

    // Upload video and wait for processing
    logger.info("Uploading video: %s", pathName(uploadPath));
    const videoFile = await this.uploadAndWait(client, uploadPath);

    const schemaHint = geminiSchemaHint(outputSchema);
    // Lead with the montage layout instructions (only when the montage was actually built) so the
    // model knows to read each frame's sub-panels left->right in time.
    const preamble = montageOk ? `${montagePreamble(sampleFps, timestamp)}\n\n` : "";
    const fullPrompt = `${preamble}${prompt}\n\n${schemaHint}`;

    // Prepend the symbol/UI reference pack (labeled ground-truth images) BEFORE the video, so the
    // model can identify what it sees in the video that follows. A per-call `reference` (scoped
    // selection) overrides the constructor default.
    const contents: Part[] = this.referenceParts(reference);
    contents.push({ fileData: { fileUri: videoFile.uri, mimeType: videoFile.mimeType } });

    // Add per-call reference images if provided (e.g. comparison frames)
    if (truthy(refImages)) {
      for (const imgPath of refImages!) if (existsSync(imgPath)) contents.push(imagePart(imgPath));
    }

    contents.push({ text: fullPrompt });

    // Call Gemini with media resolution config
    if (!Object.prototype.hasOwnProperty.call(resolutionMap, this._resolution)) throw keyError(this._resolution);
    return client.models.generateContent({
      model: this._model,
      contents: [{ role: "user", parts: contents }],
      config: { mediaResolution: resolutionMap[this._resolution] as any },
    });
  }

  referenceParts(reference: SymbolReference | null = null): Part[] {
    return referenceParts(reference !== null ? reference : this._reference,
      "--- END REFERENCE ASSETS. Now analyse the VIDEO below against the task. ---");
  }

  /** Parse a Gemini GenerateContentResponse into a VideoResult. */
  parseResponse(response: any): VideoResult {
    const [inputTokens, outputTokens] = usageOf(response);
    this._totalInputTokens += inputTokens;
    this._totalOutputTokens += outputTokens;

    const rawText: string = response?.text || "";
    const cleaned = stripMarkdownFence(rawText);
    try {
      const data = jsonLoads(cleaned);
      return geminiResult(true, data, null, { input_tokens: inputTokens, output_tokens: outputTokens, raw_text: rawText });
    } catch (exc) {
      if (!(exc instanceof JSONDecodeError)) throw exc;
      logger.warning("Gemini video returned non-JSON: %r — %s", cpSlice(rawText, 0, 200), exc.message);
      return geminiResult(false, null, `Response was not valid JSON: ${exc.message}. Raw: ${cpSlice(rawText, 0, 200)}`,
        { input_tokens: inputTokens, output_tokens: outputTokens, raw_text: rawText });
    }
  }
}
