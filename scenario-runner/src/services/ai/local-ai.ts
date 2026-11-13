/**
 * Local AI analysis helpers — self-hosted HTTP endpoint alternative to Gemini (port of
 * framework/helpers/local_ai.py).
 *
 * Drop-in replacements for `GeminiVideoHelper` / `GeminiImageHelper` that send media to a
 * self-hosted `/analyze` endpoint instead of the Google Gemini API. They mirror the Gemini helper
 * interfaces exactly (same method signatures, same result fields, same `costSummary` keys).
 *
 * Endpoint contract
 *     POST {endpoint}
 *         multipart form: prompt = "<analysis prompt>", file = (filename, bytes, mime_type)
 *         response JSON:  {"media_url": "...", "result": "<free-form text>"}
 *
 * The local model returns prose (often including reasoning), not structured JSON. As with the
 * Gemini helpers, we append a strict JSON-schema hint to the prompt and then extract a JSON object
 * from the `result` text (falling back to the last `{...}` block).
 *
 * Cost: no per-token billing for a self-hosted model — `costSummary` reports the same keys the
 * suite aggregator expects with `estimated_cost_usd == 0.0` and zero tokens, tracking only the
 * call count.
 */
import { readFileSync } from "node:fs";
import { isDict, pyJson, type Dict } from "../../py.js";
import { exampleFor, geminiResult, type GeminiResult } from "./gemini.js";
import type { VideoResult } from "./gemini-video.js";
import { cpSlice, excStr, jsonLoads, JSONDocument, JSONDecodeError, logger, osErrorText, pathName, pyError, pyStrip, strOf } from "./py-support.js";
import type { SymbolReference } from "./symbol-reference.js";

/** Request timeout (seconds). Video uploads + on-prem inference can be slow. */
export const REQUEST_TIMEOUT = 300;

/**
 * Append a schema hint + example to `prompt` so the reply is parseable JSON.
 *
 * Mirrors the logic in GeminiVideoHelper / GeminiImageHelper so the model is steered toward a flat
 * JSON object matching `outputSchema`.
 *
 * allowReasoning:
 *     false (default, used for the LOCAL self-hosted provider): demand JSON ONLY, no reasoning.
 *     Self-hosted reasoning models otherwise emit a long <think> trace and can hit the endpoint's
 *     output-token limit before answering.
 *     true (used for Claude): let the model think/reason in plain text first, then end with the
 *     JSON object as the LAST thing in the reply. Pair with `extractJson` (tolerant), which pulls
 *     the final JSON object out of a reply that may contain reasoning before it.
 */
export function buildSchemaPrompt(prompt: string, outputSchema: Dict, allowReasoning = false): string {
  const example = exampleFor(outputSchema);

  // Env override: AGENT_REASONING in {0,false,no,off} forces strict (no reasoning) even when the
  // caller requested it — lets a run A/B reasoning without code edits.
  const reasoningOn = allowReasoning
    && !["0", "false", "no", "off"].includes(pyStrip(process.env.AGENT_REASONING ?? "1").toLowerCase());
  if (reasoningOn) {
    // Reasoning allowed (Claude): think first, then a final JSON object last.
    const schemaHint = "Work through the task carefully — you MAY think step by step in plain "
      + "text first if it helps you decide. When you are ready, finish your reply "
      + "with your answer as a SINGLE JSON object: it must be the LAST thing in "
      + "your reply, on its own, and valid JSON matching this schema: "
      + `${pyJson(outputSchema)}. Example of the final JSON object: `
      + `${pyJson(example)}`;
    return `${schemaHint}\n\nTask: ${prompt}\n\nReason if needed, then give the final JSON object:`;
  }

  // Forceful, reasoning-suppressing instruction (local self-hosted models).
  const schemaHint = "You are a JSON API. Do NOT explain. Do NOT reason. Do NOT think "
    + "step-by-step. Output ONLY a single JSON object and nothing else — your "
    + "entire reply must start with { and end with }. "
    + `The JSON must match this schema: ${pyJson(outputSchema)}. `
    + `Example response format: ${pyJson(example)}`;
  return `${schemaHint}\n\nTask: ${prompt}\n\nReturn ONLY the JSON object now:`;
}

/**
 * Parse the model's `result` text as a JSON object.
 *
 * Fast path: the whole reply is a clean JSON object (strict mode / local) — a direct parse.
 * Fallback (reasoning mode): the model may think in plain text first and emit the JSON object
 * last, so scan for the LAST decodable `{...}` object (this also tolerates ```json code fences).
 * Returns the parsed dict, or null if no JSON object can be recovered.
 */
export function extractJson(text: string): Dict | null {
  if (!text) return null;
  const s = pyStrip(text);

  // Fast path — the entire reply is the JSON object.
  try {
    const parsed = jsonLoads(s);
    if (isDict(parsed)) return parsed;
  } catch (e) {
    if (!(e instanceof JSONDecodeError)) throw e;
  }

  // Tolerant path — find the last decodable top-level {...} object in the text.
  const doc = new JSONDocument(s);
  const cps = doc.cps;
  let last: Dict | null = null;
  let i = 0;
  const n = cps.length;
  while (i < n) {
    if (cps[i] === "{") {
      let obj: unknown;
      let end: number;
      try {
        [obj, end] = doc.rawDecode(i);
      } catch (e) {
        if (!(e instanceof JSONDecodeError)) throw e;
        i += 1;
        continue;
      }
      if (isDict(obj)) {
        last = obj;
        i = end;
        continue;
      }
    }
    i += 1;
  }
  return last;
}

/** `requests.HTTPError` text from `raise_for_status()`. */
function httpErrorText(status: number, reason: string, url: string): string | null {
  if (status >= 400 && status < 500) return `${status} Client Error: ${reason} for url: ${url}`;
  if (status >= 500 && status < 600) return `${status} Server Error: ${reason} for url: ${url}`;
  return null;
}

/** Shared multipart POST to the local /analyze endpoint. */
export class LocalAIClient {
  readonly _endpoint: string;
  // SymbolReference — the endpoint takes a single file, so we cannot send the reference images;
  // instead we prepend their descriptions as text to every prompt so the local model still has
  // symbol/UI context.
  readonly _reference: SymbolReference | null;
  protected _callCount = 0;

  constructor(endpoint: string, reference: SymbolReference | null = null) {
    this._endpoint = endpoint;
    this._reference = reference;
  }

  get callCount(): number {
    return this._callCount;
  }

  /**
   * POST `filePath` + schema-hinted `prompt` to the endpoint. Returns `[data, rawText]` where
   * `data` is the parsed JSON object (or null if extraction failed) and `rawText` is the model's
   * full `result` string. Raises on HTTP/transport errors — callers wrap. A per-call `reference`
   * (scoped selection) overrides the constructor default.
   */
  async postAnalyze(
    filePath: string, mime: string, prompt: string, outputSchema: Dict, reference: SymbolReference | null = null,
  ): Promise<[Dict | null, string]> {
    // Prepend the symbol/UI reference descriptions (text only) to the task.
    const ref = reference !== null ? reference : this._reference;
    if (ref !== null && !ref.isEmpty) prompt = `${ref.descriptionsText()}\n\n${prompt}`;
    const fullPrompt = buildSchemaPrompt(prompt, outputSchema);

    let bytes: Buffer;
    try {
      bytes = readFileSync(filePath);
    } catch (e) {
      throw pyError("FileNotFoundError", osErrorText(e, filePath));
    }
    // requests encodes the `data` fields before the `files`
    const form = new FormData();
    form.append("prompt", fullPrompt);
    form.append("file", new Blob([bytes], { type: mime }), pathName(filePath));
    const response = await fetch(this._endpoint, {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT * 1000),
    });
    const err = httpErrorText(response.status, response.statusText, response.url || this._endpoint);
    if (err !== null) throw pyError("HTTPError", err);

    const payload = jsonLoads(await response.text());
    const rawText = isDict(payload) ? strOf(Object.prototype.hasOwnProperty.call(payload, "result") ? payload.result : "") : "";
    const parsed = extractJson(rawText);
    return [parsed, rawText];
  }
}

/**
 * Video analysis via a self-hosted `/analyze` endpoint. Interface-compatible with
 * `GeminiVideoHelper`. `endpoint` is the full URL (e.g. `http://192.168.1.35:9000/analyze`).
 */
export class LocalVideoHelper extends LocalAIClient {
  // -- Context management (no-ops, kept for API compatibility) --

  /** No-op. The endpoint takes a single file; ref assets are not uploaded. */
  createCachedContext(_imagePaths: string[]): string {
    return "local";
  }

  releaseContext(_contextId: string): void {}

  releaseAllContexts(): void {}

  get activeContextCount(): number {
    return 0;
  }

  /** Send `videoPath` to the local endpoint and parse the response. `highFps` / `timestamp` are
   * accepted for interface parity (the raw clip is sent as-is). */
  async analyseVideo(
    videoPath: string, contextId: string | null, prompt: string, outputSchema: Dict,
    opts: { refImages?: string[] | null; reference?: SymbolReference | null; highFps?: boolean; timestamp?: boolean } = {},
  ): Promise<VideoResult> {
    void contextId;
    this._callCount += 1;

    if (opts.refImages && opts.refImages.length) {
      logger.debug("LocalVideoHelper: dropping %d ref image(s) — endpoint takes one file", opts.refImages.length);
    }

    let data: Dict | null;
    let rawText: string;
    try {
      [data, rawText] = await this.postAnalyze(videoPath, "video/mp4", prompt, outputSchema, opts.reference ?? null);
    } catch (exc) {  // resilience contract: never raise
      logger.warning("Local video analysis failed: %s", excStr(exc));
      return geminiResult(false, null, `Local AI endpoint error: ${excStr(exc)}`);
    }

    if (data === null) {
      return geminiResult(false, null, `Local AI response had no parseable JSON. Raw: ${cpSlice(rawText, 0, 200)}`, { raw_text: rawText });
    }
    return geminiResult(true, data, null, { input_tokens: 0, output_tokens: 0, raw_text: rawText });
  }

  /** Gemini-compatible cost summary (zero cost for self-hosted model). */
  costSummary(): Dict {
    return {
      model: "local",
      resolution: "n/a",
      gemini_video_calls: this._callCount,
      gemini_input_tokens: 0,
      gemini_output_tokens: 0,
      estimated_cost_usd: 0.0,
    };
  }
}

/** Image analysis via a self-hosted `/analyze` endpoint. Interface-compatible with
 * `GeminiImageHelper`. */
export class LocalImageHelper extends LocalAIClient {
  /** Send `imagePath` to the local endpoint and parse the response. */
  async analyseImage(
    imagePath: string, prompt: string, outputSchema: Dict,
    opts: { extraImages?: string[] | null; reference?: SymbolReference | null } = {},
  ): Promise<GeminiResult> {
    this._callCount += 1;

    if (opts.extraImages && opts.extraImages.length) {
      logger.debug("LocalImageHelper: dropping %d extra image(s) — endpoint takes one file", opts.extraImages.length);
    }

    let data: Dict | null;
    let rawText: string;
    try {
      [data, rawText] = await this.postAnalyze(imagePath, "image/png", prompt, outputSchema, opts.reference ?? null);
    } catch (exc) {  // resilience contract: never raise
      logger.warning("Local image analysis failed: %s", excStr(exc));
      return geminiResult(false, null, `Local AI endpoint error: ${excStr(exc)}`);
    }

    if (data === null) {
      return geminiResult(false, null, `Local AI response had no parseable JSON. Raw: ${cpSlice(rawText, 0, 200)}`, { raw_text: rawText });
    }
    return geminiResult(true, data, null, { input_tokens: 0, output_tokens: 0, raw_text: rawText });
  }

  /** Gemini-compatible cost summary (zero cost for self-hosted model). */
  costSummary(): Dict {
    return {
      model: "local",
      gemini_image_calls: this._callCount,
      gemini_input_tokens: 0,
      gemini_output_tokens: 0,
      estimated_cost_usd: 0.0,
    };
  }
}
