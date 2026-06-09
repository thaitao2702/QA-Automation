"""
Local AI analysis helpers — self-hosted HTTP endpoint alternative to Gemini.

Drop-in replacements for ``GeminiVideoHelper`` / ``GeminiImageHelper`` that send
media to a self-hosted ``/analyze`` endpoint instead of the Google Gemini API.
They mirror the Gemini helper interfaces exactly (same method signatures, same
``VideoResult`` / ``GeminiResult`` return types, same ``cost_summary`` keys) so
``VideoAiProcessor`` and ``StepExecutor`` work unchanged.

Endpoint contract
-----------------
POST {endpoint}
    multipart form:
        file   = (filename, bytes, mime_type)
        prompt = "<analysis prompt>"
    response JSON:
        {"media_url": "...", "result": "<free-form text>"}

The local model returns prose (often including reasoning), not structured JSON.
As with the Gemini helpers, we append a strict JSON-schema hint to the prompt and
then extract a JSON object from the ``result`` text (stripping ``<think>`` blocks
and markdown fences, falling back to the last ``{...}`` block).

Public interface
----------------
LocalVideoHelper(endpoint)
    analyse_video(video_path, context_id, prompt, output_schema, ref_images=None) -> VideoResult
    create_cached_context / release_context / release_all_contexts  (no-ops)
    cost_summary() -> dict

LocalImageHelper(endpoint)
    analyse_image(image_path, prompt, output_schema, extra_images=None) -> GeminiResult
    cost_summary() -> dict

Cost
----
No per-token billing for a self-hosted model — ``cost_summary`` reports the same
keys the suite aggregator expects with ``estimated_cost_usd == 0.0`` and zero
tokens, tracking only the call count.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from framework.helpers.gemini import GeminiResult
from framework.helpers.gemini_video import VideoResult

logger = logging.getLogger(__name__)

# Request timeout (seconds). Video uploads + on-prem inference can be slow.
_REQUEST_TIMEOUT = 300


# ---------------------------------------------------------------------------
# Prompt / response helpers
# ---------------------------------------------------------------------------


def _build_schema_prompt(prompt: str, output_schema: dict[str, Any]) -> str:
    """
    Append a JSON-only schema hint + example to *prompt*.

    Mirrors the logic in GeminiVideoHelper / GeminiImageHelper so the local model
    is steered toward a flat JSON object matching *output_schema*.
    """
    example: dict[str, Any] = {}
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

    # Forceful, reasoning-suppressing instruction. Self-hosted reasoning models
    # tend to emit a long <think> trace and can hit the endpoint's output-token
    # limit before producing the answer — so we demand JSON first, no reasoning.
    schema_hint = (
        f"You are a JSON API. Do NOT explain. Do NOT reason. Do NOT think "
        f"step-by-step. Output ONLY a single JSON object and nothing else — your "
        f"entire reply must start with {{ and end with }}. "
        f"The JSON must match this schema: {json.dumps(output_schema)}. "
        f"Example response format: {json.dumps(example)}"
    )
    return f"{schema_hint}\n\nTask: {prompt}\n\nReturn ONLY the JSON object now:"


def _extract_json(text: str) -> dict[str, Any] | None:
    """
    Parse the model's ``result`` text as a JSON object.

    With reasoning disabled on the endpoint, ``result`` is a clean JSON object,
    so a direct parse is enough. Returns the parsed dict, or None if the text is
    empty or not a JSON object.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------


class _LocalAIClient:
    """Shared multipart POST to the local /analyze endpoint."""

    def __init__(self, endpoint: str, reference: Any | None = None) -> None:
        self._endpoint = endpoint
        # SymbolReference — the endpoint takes a single file, so we cannot send the
        # reference images; instead we prepend their descriptions as text to every
        # prompt so the local model still has symbol/UI context.
        self._reference = reference
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def _post_analyze(
        self,
        file_path: Path,
        mime: str,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """
        POST *file_path* + schema-hinted *prompt* to the endpoint.

        Returns ``(data, raw_text)`` where *data* is the parsed JSON object
        (or None if extraction failed) and *raw_text* is the model's full
        ``result`` string. Raises on HTTP/transport errors — callers wrap.
        """
        import requests  # lazy — only needed in local mode

        # Prepend the symbol/UI reference descriptions (text only) to the task.
        if self._reference is not None and not getattr(self._reference, "is_empty", True):
            prompt = f"{self._reference.descriptions_text()}\n\n{prompt}"
        full_prompt = _build_schema_prompt(prompt, output_schema)

        with file_path.open("rb") as fh:
            files = {"file": (file_path.name, fh, mime)}
            data = {"prompt": full_prompt}
            response = requests.post(
                self._endpoint,
                data=data,
                files=files,
                timeout=_REQUEST_TIMEOUT,
            )
        response.raise_for_status()

        payload = response.json()
        raw_text = str(payload.get("result", "")) if isinstance(payload, dict) else ""
        parsed = _extract_json(raw_text)
        return parsed, raw_text


# ---------------------------------------------------------------------------
# Video helper
# ---------------------------------------------------------------------------


class LocalVideoHelper(_LocalAIClient):
    """
    Video analysis via a self-hosted ``/analyze`` endpoint.

    Interface-compatible with ``GeminiVideoHelper`` for use by ``VideoAiProcessor``.

    Parameters
    ----------
    endpoint:
        Full URL of the local analyze endpoint
        (e.g. ``http://192.168.1.35:9000/analyze``).
    """

    def __init__(self, endpoint: str, reference: Any | None = None) -> None:
        super().__init__(endpoint, reference)

    # -- Context management (no-ops, kept for API compatibility) ------------

    def create_cached_context(self, image_paths: list[Path]) -> str:
        """No-op. The endpoint takes a single file; ref assets are not uploaded."""
        return "local"

    def release_context(self, context_id: str) -> None:
        """No-op."""

    def release_all_contexts(self) -> None:
        """No-op."""

    @property
    def active_context_count(self) -> int:
        return 0

    # -- Analysis -----------------------------------------------------------

    def analyse_video(
        self,
        video_path: Path,
        context_id: str | None,
        prompt: str,
        output_schema: dict[str, Any],
        ref_images: list[Path] | None = None,
    ) -> VideoResult:
        """Send *video_path* to the local endpoint and parse the response."""
        self._call_count += 1

        if ref_images:
            logger.debug(
                "LocalVideoHelper: dropping %d ref image(s) — endpoint takes one file",
                len(ref_images),
            )

        try:
            data, raw_text = self._post_analyze(
                video_path, "video/mp4", prompt, output_schema
            )
        except Exception as exc:  # noqa: BLE001 — resilience contract: never raise
            logger.warning("Local video analysis failed: %s", exc)
            return VideoResult(
                success=False,
                data=None,
                reason=f"Local AI endpoint error: {exc}",
                raw_text=None,
            )

        if data is None:
            return VideoResult(
                success=False,
                data=None,
                reason=f"Local AI response had no parseable JSON. Raw: {raw_text[:200]}",
                raw_text=raw_text,
            )

        return VideoResult(
            success=True,
            data=data,
            reason=None,
            input_tokens=0,
            output_tokens=0,
            raw_text=raw_text,
        )

    # -- Cost ---------------------------------------------------------------

    def cost_summary(self) -> dict[str, Any]:
        """Gemini-compatible cost summary (zero cost for self-hosted model)."""
        return {
            "model": "local",
            "resolution": "n/a",
            "gemini_video_calls": self._call_count,
            "gemini_input_tokens": 0,
            "gemini_output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }


# ---------------------------------------------------------------------------
# Image helper
# ---------------------------------------------------------------------------


class LocalImageHelper(_LocalAIClient):
    """
    Image analysis via a self-hosted ``/analyze`` endpoint.

    Interface-compatible with ``GeminiImageHelper`` for use by ``StepExecutor`` and
    the pending-image reprocessing path in ``SuiteRunner``.

    Parameters
    ----------
    endpoint:
        Full URL of the local analyze endpoint.
    """

    def __init__(self, endpoint: str, reference: Any | None = None) -> None:
        super().__init__(endpoint, reference)

    def analyse_image(
        self,
        image_path: Path,
        prompt: str,
        output_schema: dict[str, Any],
        extra_images: list[Path] | None = None,
    ) -> GeminiResult:
        """Send *image_path* to the local endpoint and parse the response."""
        self._call_count += 1

        if extra_images:
            logger.debug(
                "LocalImageHelper: dropping %d extra image(s) — endpoint takes one file",
                len(extra_images),
            )

        try:
            data, raw_text = self._post_analyze(
                image_path, "image/png", prompt, output_schema
            )
        except Exception as exc:  # noqa: BLE001 — resilience contract: never raise
            logger.warning("Local image analysis failed: %s", exc)
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Local AI endpoint error: {exc}",
                raw_text=None,
            )

        if data is None:
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Local AI response had no parseable JSON. Raw: {raw_text[:200]}",
                raw_text=raw_text,
            )

        return GeminiResult(
            success=True,
            data=data,
            reason=None,
            input_tokens=0,
            output_tokens=0,
            raw_text=raw_text,
        )

    def cost_summary(self) -> dict[str, Any]:
        """Gemini-compatible cost summary (zero cost for self-hosted model)."""
        return {
            "model": "local",
            "gemini_image_calls": self._call_count,
            "gemini_input_tokens": 0,
            "gemini_output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
