"""
Gemini model pricing registry.

Centralized pricing table for all supported Gemini models. Used by both
GeminiImageHelper and GeminiVideoHelper to calculate API costs.

Usage
-----
    from framework.helpers.gemini_pricing import get_pricing, SUPPORTED_MODELS

    pricing = get_pricing("gemini-2.5-flash")
    cost = input_tokens * pricing.input_per_token + output_tokens * pricing.output_per_token
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Pricing for a single Gemini model."""

    model: str
    input_cost_per_1m: float    # USD per 1M input tokens
    output_cost_per_1m: float   # USD per 1M output tokens

    @property
    def input_per_token(self) -> float:
        return self.input_cost_per_1m / 1_000_000

    @property
    def output_per_token(self) -> float:
        return self.output_cost_per_1m / 1_000_000


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------

_PRICING: dict[str, ModelPricing] = {
    "gemini-2.5-flash": ModelPricing(
        model="gemini-2.5-flash",
        input_cost_per_1m=0.30,
        output_cost_per_1m=2.50,
    ),
    "gemini-3-flash-preview": ModelPricing(
        model="gemini-3-flash-preview",
        input_cost_per_1m=0.50,
        output_cost_per_1m=3.00,
    ),
}

SUPPORTED_MODELS = list(_PRICING.keys())

# Default fallback pricing (gemini-2.5-flash)
_DEFAULT_PRICING = _PRICING["gemini-2.5-flash"]


def get_pricing(model: str) -> ModelPricing:
    """
    Get pricing for a model. Returns default pricing if model is unknown.

    Parameters
    ----------
    model:
        Model identifier (e.g., ``'gemini-2.5-flash'``).
    """
    return _PRICING.get(model, _DEFAULT_PRICING)
