"""
Gemini 2.5 Flash image analysis helper.

Sends screenshot images with structured prompts to the Gemini 2.5 Flash API
and returns parsed JSON responses. Tracks per-call token usage and accumulates
costs across a suite run.

Public interface
----------------
GeminiImageHelper(api_key)
    Gemini image analysis wrapper with cost tracking.

    analyse_image(image_path, prompt, output_schema) -> GeminiResult
        Send an image with a text prompt and an expected JSON schema.
        Returns a GeminiResult (success or failure).

    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    estimated_cost_usd: float
    cost_summary() -> dict
    reset() -> None

GeminiResult
    Dataclass holding the analysis outcome.
    Fields: success (bool), data (dict | None), reason (str | None),
            input_tokens (int), output_tokens (int), raw_text (str | None).

Pricing
-------
Gemini 2.5 Flash:
  Input:  $0.30 per 1M tokens
  Output: $2.50 per 1M tokens
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from framework.helpers.gemini_pricing import get_pricing

_DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass
class GeminiResult:
    """Outcome of a single Gemini image analysis call."""

    success: bool
    data: dict[str, Any] | None
    reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    raw_text: str | None = None


def _strip_markdown_fence(text: str) -> str:
    """
    Remove optional ```json ... ``` (or ``` ... ```) fences from Gemini responses.

    Gemini sometimes wraps structured output in code fences even when JSON mode
    is requested — this strips them so ``json.loads`` can parse cleanly.
    """
    stripped = text.strip()
    # Match optional language tag after opening fence
    match = re.match(r"^```(?:json)?\s*(.*?)```$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


class GeminiImageHelper:
    """
    Gemini image analysis with structured output and cost tracking.

    Parameters
    ----------
    api_key:
        Gemini API key.
    model:
        Model identifier (e.g., ``'gemini-2.5-flash'``). Defaults to env
        ``GEMINI_IMAGE_MODEL`` or ``'gemini-2.5-flash'``.
    """

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        reference: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self._model = model or _DEFAULT_MODEL
        self._pricing = get_pricing(self._model)
        # SymbolReference (framework.helpers.symbol_reference) — its labeled images
        # are prepended to EVERY request so the model has ground-truth context.
        self._reference = reference
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._call_count: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_input_tokens(self) -> int:
        """Cumulative input tokens across all calls in this session."""
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        """Cumulative output tokens across all calls in this session."""
        return self._total_output_tokens

    @property
    def call_count(self) -> int:
        """Total number of API calls attempted (success or failure)."""
        return self._call_count

    @property
    def estimated_cost_usd(self) -> float:
        """Estimated USD cost based on accumulated token counts and model pricing."""
        return (
            self._total_input_tokens * self._pricing.input_per_token
            + self._total_output_tokens * self._pricing.output_per_token
        )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def analyse_image(
        self,
        image_path: Path,
        prompt: str,
        output_schema: dict[str, Any],
        extra_images: list[Path] | None = None,
    ) -> GeminiResult:
        """
        Send *image_path* with *prompt* to Gemini and parse the structured response.

        Parameters
        ----------
        image_path:
            Path to the screenshot PNG/JPEG file.
        prompt:
            Text prompt describing the analysis task.
        output_schema:
            Expected JSON schema for the response (used to validate structure
            and instruct Gemini on the expected output format).

        Returns
        -------
        GeminiResult
            Always returns a result — never raises. On API failure or invalid
            JSON, ``success`` is ``False`` and ``reason`` describes the problem.
        """
        self._call_count += 1

        try:
            response = self._call_api(image_path, prompt, output_schema, extra_images=extra_images)
            return self._parse_response(response)
        except Exception as exc:  # noqa: BLE001 — intentional broad catch for resilience
            logger.warning("Gemini API call failed: %s", exc)
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Gemini API error: {exc}",
                raw_text=None,
            )

    def cost_summary(self) -> dict[str, Any]:
        """
        Return a cost summary dict compatible with the suite ``summary.json`` schema.

        Example output::

            {
                "gemini_image_calls": 12,
                "gemini_input_tokens": 45000,
                "gemini_output_tokens": 3200,
                "estimated_cost_usd": 0.04,
            }
        """
        return {
            "model": self._model,
            "gemini_image_calls": self._call_count,
            "gemini_input_tokens": self._total_input_tokens,
            "gemini_output_tokens": self._total_output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }

    def reset(self) -> None:
        """Reset all counters. Use between suite runs when reusing the helper."""
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._call_count = 0

    # ------------------------------------------------------------------
    # Internal API call (separated for easy mocking in tests)
    # ------------------------------------------------------------------

    def _call_api(
        self,
        image_path: Path,
        prompt: str,
        output_schema: dict[str, Any],
        extra_images: list[Path] | None = None,
    ) -> Any:
        """
        Execute the Gemini API call and return the raw response object.

        This method is intentionally thin so tests can patch it with
        ``patch.object(helper, '_call_api', return_value=mock_resp)``.

        Raises on any API / network error — the caller handles exceptions.
        """
        from google import genai  # Lazy import — new google.genai SDK
        from PIL import Image

        client = genai.Client(api_key=self.api_key)

        images = [Image.open(image_path)]
        if extra_images:
            for p in extra_images:
                images.append(Image.open(p))

        # Build an example response so the model returns flat values, not schema objects
        example = {}
        for key, spec in output_schema.items():
            typ = spec.get("type", "string") if isinstance(spec, dict) else "string"
            if typ == "boolean":
                example[key] = True
            elif typ == "string":
                enum = spec.get("enum") if isinstance(spec, dict) else None
                example[key] = enum[0] if enum else "example"
            elif typ in ("number", "integer"):
                example[key] = 0
            else:
                example[key] = None

        schema_hint = (
            f"Respond ONLY with valid JSON matching this schema: {json.dumps(output_schema)}. "
            f"Do not include any other text. "
            f"Example response format: {json.dumps(example)}"
        )
        full_prompt = f"{prompt}\n\n{schema_hint}"

        # Prepend the symbol/UI reference pack (labeled ground-truth images) so the
        # model can identify what it sees in the screenshot(s) that follow.
        contents: list[Any] = self._reference_parts(Image)
        contents.extend(images)
        contents.append(full_prompt)
        response = client.models.generate_content(
            model=self._model,
            contents=contents,
        )
        return response

    def _reference_parts(self, image_cls: Any) -> list[Any]:
        """Build [header, label, image, ...] reference parts; [] if no reference."""
        ref = self._reference
        if ref is None or getattr(ref, "is_empty", True):
            return []
        parts: list[Any] = [ref.header]
        for img_path, label in ref.items:
            try:
                parts.append(label)
                parts.append(image_cls.open(img_path))
            except Exception as exc:  # noqa: BLE001 — a bad ref image must not break the assert
                logger.warning("Skipping reference asset %s: %s", img_path, exc)
                parts.pop()  # drop the label we just appended
        parts.append(
            "--- END REFERENCE ASSETS. Now analyse the screenshot(s) below against the task. ---"
        )
        return parts

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response: Any) -> GeminiResult:
        """Parse a Gemini GenerateContentResponse into a GeminiResult."""
        # Extract token usage (google.genai SDK)
        input_tokens = 0
        output_tokens = 0
        try:
            usage = response.usage_metadata
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            candidates_tokens = getattr(usage, "candidates_token_count", 0) or 0
            thoughts_tokens = getattr(usage, "thoughts_token_count", 0) or 0
            output_tokens = candidates_tokens + thoughts_tokens
        except AttributeError:
            logger.debug("Token usage metadata not available in response")

        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

        raw_text = getattr(response, "text", None) or ""

        # Attempt to parse JSON from response text
        cleaned = _strip_markdown_fence(raw_text)
        try:
            data = json.loads(cleaned)
            return GeminiResult(
                success=True,
                data=data,
                reason=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=raw_text,
            )
        except json.JSONDecodeError as exc:
            logger.warning("Gemini returned non-JSON response: %r — %s", raw_text[:200], exc)
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Response was not valid JSON: {exc}. Raw: {raw_text[:200]}",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=raw_text,
            )
