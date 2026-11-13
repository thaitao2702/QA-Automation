/**
 * Gemini model pricing registry (port of framework/helpers/gemini_pricing.py).
 *
 * Centralized pricing table for all supported Gemini models. Used by both GeminiImageHelper and
 * GeminiVideoHelper to calculate API costs:
 *
 *     const pricing = getPricing("gemini-2.5-flash");
 *     const cost = inputTokens * pricing.inputPerToken + outputTokens * pricing.outputPerToken;
 */

/** Pricing for a single Gemini model. */
export class ModelPricing {
  constructor(
    readonly model: string,
    /** USD per 1M input tokens */
    readonly inputCostPer1m: number,
    /** USD per 1M output tokens */
    readonly outputCostPer1m: number,
  ) {
    Object.freeze(this);
  }

  get inputPerToken(): number {
    return this.inputCostPer1m / 1_000_000;
  }

  get outputPerToken(): number {
    return this.outputCostPer1m / 1_000_000;
  }
}

const PRICING: Readonly<Record<string, ModelPricing>> = {
  "gemini-2.5-flash": new ModelPricing("gemini-2.5-flash", 0.30, 2.50),
  "gemini-3-flash-preview": new ModelPricing("gemini-3-flash-preview", 0.50, 3.00),
};

export const SUPPORTED_MODELS: readonly string[] = Object.keys(PRICING);

/** Default fallback pricing (gemini-2.5-flash). */
const DEFAULT_PRICING = PRICING["gemini-2.5-flash"];

/** Pricing for a model; the default pricing if the model is unknown. */
export function getPricing(model: string): ModelPricing {
  return Object.prototype.hasOwnProperty.call(PRICING, model) ? PRICING[model] : DEFAULT_PRICING;
}
