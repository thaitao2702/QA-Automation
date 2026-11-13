/**
 * Gemini 2.5 Flash image analysis helper (port of framework/helpers/gemini.py).
 *
 * Sends screenshot images with structured prompts to the Gemini API and returns parsed JSON
 * responses. Tracks per-call token usage and accumulates costs across a suite run.
 *
 *     new GeminiImageHelper(apiKey)
 *       .analyseImage(imagePath, prompt, outputSchema) -> GeminiResult (never raises)
 *       .totalInputTokens / .totalOutputTokens / .callCount / .estimatedCostUsd
 *       .costSummary() / .reset()
 *
 * Pricing — Gemini 2.5 Flash: input $0.30 / 1M tokens, output $2.50 / 1M tokens.
 */
import { readFileSync } from "node:fs";
import { isDict, pyJson, truthy, type Dict } from "../../py.js";
import { getPricing, type ModelPricing } from "./gemini-pricing.js";
import { cpSlice, excStr, jsonLoads, JSONDecodeError, keyError, logger, osErrorText, pyError, pyStrip, reprStr } from "./py-support.js";
import type { SymbolReference } from "./symbol-reference.js";
import { pyRound } from "../../py.js";

const DEFAULT_MODEL = "gemini-2.5-flash";

/** Outcome of a single Gemini image analysis call. Field names are the Python dataclass's. */
export interface GeminiResult {
  success: boolean;
  data: any;
  reason: string | null;
  input_tokens: number;
  output_tokens: number;
  raw_text: string | null;
}

export function geminiResult(
  success: boolean, data: any, reason: string | null,
  extra: { input_tokens?: number; output_tokens?: number; raw_text?: string | null } = {},
): GeminiResult {
  return {
    success, data, reason,
    input_tokens: extra.input_tokens ?? 0,
    output_tokens: extra.output_tokens ?? 0,
    raw_text: extra.raw_text ?? null,
  };
}

/** `re.match(r"^```(?:json)?\s*(.*?)```$", s, re.DOTALL)` — `\s` as Python's str regex reads it. */
const FENCE_RE = /^```(?:json)?[\t\n\v\f\r\x1c-\x20\x85\xa0\u{1680}\u{2000}-\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}]*([\s\S]*?)```$/u;

/**
 * Remove optional ```json ... ``` (or ``` ... ```) fences from Gemini responses.
 *
 * Gemini sometimes wraps structured output in code fences even when JSON mode is requested — this
 * strips them so the JSON parse is clean.
 */
export function stripMarkdownFence(text: string): string {
  const stripped = pyStrip(text);
  // Match optional language tag after opening fence
  const match = FENCE_RE.exec(stripped);
  if (match) return pyStrip(match[1]);
  return stripped;
}

/** Build an example response so the model returns flat values, not schema objects. */
export function exampleFor(outputSchema: Dict): Dict {
  const example: Dict = {};
  for (const [key, spec] of Object.entries(outputSchema)) {
    const typ = isDict(spec) ? (Object.prototype.hasOwnProperty.call(spec, "type") ? spec.type : "string") : "string";
    if (typ === "boolean") {
      example[key] = true;
    } else if (typ === "string") {
      const en = isDict(spec) ? (spec.enum ?? null) : null;
      example[key] = truthy(en) ? first(en) : "example";
    } else if (typ === "number" || typ === "integer") {
      example[key] = 0;
    } else {
      example[key] = null;
    }
  }
  return example;
}

/** `enum[0]`. */
function first(v: unknown): unknown {
  if (Array.isArray(v)) return v[0];
  if (typeof v === "string") return Array.from(v)[0];
  // a dict's keys came from JSON / YAML as str, so the int key 0 is never there
  if (isDict(v)) throw keyError(0);
  throw pyError("TypeError", `'${typeof v === "number" ? (Number.isInteger(v) ? "int" : "float") : "bool"}' object is not subscriptable`);
}

/** The JSON-only instruction the Gemini helpers append to every prompt. */
export function geminiSchemaHint(outputSchema: Dict): string {
  return `Respond ONLY with valid JSON matching this schema: ${pyJson(outputSchema)}. `
    + "Do not include any other text. "
    + `Example response format: ${pyJson(exampleFor(outputSchema))}`;
}

/** An image as a Gemini inline part. The Python opened it with PIL (`Image.open`), which raises on
 * a missing or unreadable file; the SDK then re-saved JPEG as JPEG and everything else as PNG. */
export interface InlineImagePart {
  inlineData: { mimeType: string; data: string };
}

export function sniffImage(bytes: Buffer): string | null {
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  if (bytes.length >= 6 && /^GIF8[79]a$/.test(bytes.subarray(0, 6).toString("latin1"))) return "image/gif";
  if (bytes.length >= 12 && bytes.subarray(0, 4).toString("latin1") === "RIFF" && bytes.subarray(8, 12).toString("latin1") === "WEBP") return "image/webp";
  if (bytes.length >= 2 && bytes.subarray(0, 2).toString("latin1") === "BM") return "image/bmp";
  return null;
}

export function imagePart(path: string): InlineImagePart {
  let bytes: Buffer;
  try {
    bytes = readFileSync(path);
  } catch (e) {
    throw pyError("FileNotFoundError", osErrorText(e, path));
  }
  const mimeType = sniffImage(bytes);
  if (mimeType === null) throw pyError("UnidentifiedImageError", `cannot identify image file ${reprStr(path)}`);
  return { inlineData: { mimeType, data: bytes.toString("base64") } };
}

export type Part = { text: string } | InlineImagePart | { fileData: { fileUri?: string; mimeType?: string } };

/** Build [header, label, image, ...] reference parts; [] if no reference. A per-call `reference`
 * (scoped selection) overrides the constructor default; pass null to use the full pack. */
export function referenceParts(ref: SymbolReference | null, endLine: string): Part[] {
  if (ref === null || ref.isEmpty) return [];
  const parts: Part[] = [{ text: ref.header }];
  for (const [imgPath, label] of ref.items) {
    try {
      parts.push({ text: label });
      parts.push(imagePart(imgPath));
    } catch (exc) {  // a bad ref image must not break the assert
      logger.warning("Skipping reference asset %s: %s", imgPath, excStr(exc));
      parts.pop();  // drop the label we just appended
    }
  }
  parts.push({ text: endLine });
  return parts;
}

/** Token usage of a GenerateContentResponse: [input, candidates + thoughts]. */
export function usageOf(response: any): [number, number] {
  const usage = response?.usageMetadata;
  const input = usage?.promptTokenCount || 0;
  const candidates = usage?.candidatesTokenCount || 0;
  const thoughts = usage?.thoughtsTokenCount || 0;
  return [input, candidates + thoughts];
}

/**
 * Gemini image analysis with structured output and cost tracking.
 *
 * `model` defaults to `gemini-2.5-flash`.
 */
export class GeminiImageHelper {
  readonly apiKey: string;
  readonly _model: string;
  private readonly _pricing: ModelPricing;
  // SymbolReference — its labeled images are prepended to EVERY request so the model has
  // ground-truth context.
  readonly _reference: SymbolReference | null;
  private _totalInputTokens = 0;
  private _totalOutputTokens = 0;
  private _callCount = 0;

  constructor(apiKey: string, model: string | null = null, reference: SymbolReference | null = null) {
    this.apiKey = apiKey;
    this._model = model || DEFAULT_MODEL;
    this._pricing = getPricing(this._model);
    this._reference = reference;
  }

  /** Cumulative input tokens across all calls in this session. */
  get totalInputTokens(): number {
    return this._totalInputTokens;
  }

  /** Cumulative output tokens across all calls in this session. */
  get totalOutputTokens(): number {
    return this._totalOutputTokens;
  }

  /** Total number of API calls attempted (success or failure). */
  get callCount(): number {
    return this._callCount;
  }

  /** Estimated USD cost based on accumulated token counts and model pricing. */
  get estimatedCostUsd(): number {
    return this._totalInputTokens * this._pricing.inputPerToken + this._totalOutputTokens * this._pricing.outputPerToken;
  }

  /**
   * Send `imagePath` with `prompt` to Gemini and parse the structured response. Always returns a
   * result — never raises. On API failure or invalid JSON, `success` is false and `reason`
   * describes the problem.
   */
  async analyseImage(
    imagePath: string, prompt: string, outputSchema: Dict,
    opts: { extraImages?: string[] | null; reference?: SymbolReference | null } = {},
  ): Promise<GeminiResult> {
    this._callCount += 1;
    try {
      const response = await this.callApi(imagePath, prompt, outputSchema, opts.extraImages ?? null, opts.reference ?? null);
      return this.parseResponse(response);
    } catch (exc) {  // intentional broad catch for resilience
      logger.warning("Gemini API call failed: %s", excStr(exc));
      return geminiResult(false, null, `Gemini API error: ${excStr(exc)}`);
    }
  }

  /** A cost summary dict compatible with the suite `summary.json` schema. */
  costSummary(): Dict {
    return {
      model: this._model,
      gemini_image_calls: this._callCount,
      gemini_input_tokens: this._totalInputTokens,
      gemini_output_tokens: this._totalOutputTokens,
      estimated_cost_usd: pyRound(this.estimatedCostUsd, 6),
    };
  }

  /** Reset all counters. Use between suite runs when reusing the helper. */
  reset(): void {
    this._totalInputTokens = 0;
    this._totalOutputTokens = 0;
    this._callCount = 0;
  }

  /** Execute the Gemini API call and return the raw response. Raises on any API / network error —
   * the caller handles exceptions. */
  async callApi(
    imagePath: string, prompt: string, outputSchema: Dict,
    extraImages: string[] | null = null, reference: SymbolReference | null = null,
  ): Promise<any> {
    const { GoogleGenAI } = await import("@google/genai");
    const client = new GoogleGenAI({ apiKey: this.apiKey });

    const images: Part[] = [imagePart(imagePath)];
    if (truthy(extraImages)) for (const p of extraImages!) images.push(imagePart(p));

    const fullPrompt = `${prompt}\n\n${geminiSchemaHint(outputSchema)}`;

    // Prepend the symbol/UI reference pack (labeled ground-truth images) so the model can identify
    // what it sees in the screenshot(s) that follow. A per-call `reference` (scoped selection)
    // overrides the constructor default.
    const contents: Part[] = this.referenceParts(reference);
    contents.push(...images);
    contents.push({ text: fullPrompt });
    return client.models.generateContent({ model: this._model, contents: [{ role: "user", parts: contents }] });
  }

  referenceParts(reference: SymbolReference | null = null): Part[] {
    return referenceParts(reference !== null ? reference : this._reference,
      "--- END REFERENCE ASSETS. Now analyse the screenshot(s) below against the task. ---");
  }

  /** Parse a Gemini GenerateContentResponse into a GeminiResult. */
  parseResponse(response: any): GeminiResult {
    const [inputTokens, outputTokens] = usageOf(response);
    this._totalInputTokens += inputTokens;
    this._totalOutputTokens += outputTokens;

    const rawText: string = response?.text || "";

    // Attempt to parse JSON from response text
    const cleaned = stripMarkdownFence(rawText);
    try {
      const data = jsonLoads(cleaned);
      return geminiResult(true, data, null, { input_tokens: inputTokens, output_tokens: outputTokens, raw_text: rawText });
    } catch (exc) {
      if (!(exc instanceof JSONDecodeError)) throw exc;
      logger.warning("Gemini returned non-JSON response: %r — %s", cpSlice(rawText, 0, 200), exc.message);
      return geminiResult(false, null, `Response was not valid JSON: ${exc.message}. Raw: ${cpSlice(rawText, 0, 200)}`,
        { input_tokens: inputTokens, output_tokens: outputTokens, raw_text: rawText });
    }
  }
}
