"""
Gemini video analysis helper using google.genai SDK.

Uploads video files to Gemini, waits for processing, then sends the video
with a text prompt for analysis. Returns structured JSON responses.

Public interface
----------------
GeminiVideoHelper(api_key)
    Video analysis wrapper with cost tracking.

    analyse_video(video_path, prompt, output_schema, ref_images=None) -> VideoResult
        Upload and analyse a video file. Optionally include reference images.

    total_input_tokens: int
    total_output_tokens: int
    cost_summary() -> dict

VideoResult
    Dataclass: success (bool), data (dict | None), reason (str | None),
               input_tokens (int), output_tokens (int), raw_text (str | None).

Pricing
-------
Gemini 2.5 Flash:
  Input:  $0.075 per 1M tokens (context <= 200k)
  Output: $0.30  per 1M tokens
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing (USD per token)
# ---------------------------------------------------------------------------
_INPUT_COST_PER_TOKEN: float = 0.075 / 1_000_000
_OUTPUT_COST_PER_TOKEN: float = 0.30 / 1_000_000

_MODEL_NAME = "gemini-2.5-flash"
_MEDIA_RESOLUTION = "MEDIUM"


@dataclass
class VideoResult:
    """Outcome of a single Gemini video analysis call."""

    success: bool
    data: dict[str, Any] | None
    reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    raw_text: str | None = None


def _strip_markdown_fence(text: str) -> str:
    """Remove ```json ... ``` fences from Gemini text output."""
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)```$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


class GeminiVideoHelper:
    """
    Gemini video analysis using the google.genai SDK.

    Uploads video via the Files API, waits for processing, then sends
    the video with a prompt for structured analysis.

    Parameters
    ----------
    api_key:
        Gemini API key.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._video_call_count: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._total_output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self._total_input_tokens * _INPUT_COST_PER_TOKEN
            + self._total_output_tokens * _OUTPUT_COST_PER_TOKEN
        )

    # ------------------------------------------------------------------
    # Context management (no-op — kept for API compatibility)
    # ------------------------------------------------------------------

    def create_cached_context(self, image_paths: list[Path]) -> str:
        """No-op. Context caching removed — ref images passed inline."""
        return "inline"

    def release_context(self, context_id: str) -> None:
        """No-op."""

    def release_all_contexts(self) -> None:
        """No-op."""

    @property
    def active_context_count(self) -> int:
        return 0

    # ------------------------------------------------------------------
    # Video analysis
    # ------------------------------------------------------------------

    def analyse_video(
        self,
        video_path: Path,
        context_id: str | None,
        prompt: str,
        output_schema: dict[str, Any],
        ref_images: list[Path] | None = None,
    ) -> VideoResult:
        """
        Upload *video_path* to Gemini and analyse it.

        Parameters
        ----------
        video_path:
            Path to the video file (WebM, MP4, etc.).
        context_id:
            Ignored (kept for API compatibility with video_ai_processor).
        prompt:
            Analysis prompt describing what to observe.
        output_schema:
            Expected JSON schema for the response.
        ref_images:
            Optional reference images to include alongside the video.
        """
        self._video_call_count += 1

        try:
            response = self._call_video_api(video_path, prompt, output_schema, ref_images)
            return self._parse_response(response)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini video API call failed: %s", exc)
            return VideoResult(
                success=False,
                data=None,
                reason=f"Gemini video API error: {exc}",
                raw_text=None,
            )

    # ------------------------------------------------------------------
    # Cost tracking
    # ------------------------------------------------------------------

    def cost_summary(self) -> dict[str, Any]:
        """Return a cost summary dict compatible with ``summary.json``."""
        return {
            "gemini_video_calls": self._video_call_count,
            "gemini_input_tokens": self._total_input_tokens,
            "gemini_output_tokens": self._total_output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }

    # ------------------------------------------------------------------
    # Internal API calls
    # ------------------------------------------------------------------

    def _upload_and_wait(self, client: Any, path: Path) -> Any:
        """Upload a file to Gemini and wait for it to become ACTIVE."""
        file = client.files.upload(file=str(path))

        # Video files need processing before use
        while not file.state or file.state.name != "ACTIVE":
            logger.debug("Processing file... state=%s", file.state)
            time.sleep(5)
            file = client.files.get(name=file.name)

        return file

    def _call_video_api(
        self,
        video_path: Path,
        prompt: str,
        output_schema: dict[str, Any],
        ref_images: list[Path] | None = None,
    ) -> Any:
        """
        Upload video, build contents, and call Gemini.

        Uses the google.genai SDK (not the deprecated google.generativeai).
        """
        from google import genai
        from google.genai import types

        resolution_map = {
            "LOW": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "MEDIUM": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "HIGH": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        }

        client = genai.Client(api_key=self.api_key)

        # Upload video and wait for processing
        logger.info("Uploading video: %s", video_path.name)
        video_file = self._upload_and_wait(client, video_path)

        # Build contents: video + optional ref images + prompt
        schema_hint = (
            f"Respond ONLY with valid JSON matching this schema: {json.dumps(output_schema)}. "
            f"Do not include any other text."
        )
        full_prompt = f"{prompt}\n\n{schema_hint}"

        contents: list[Any] = [video_file]

        # Add reference images if provided
        if ref_images:
            from PIL import Image

            for img_path in ref_images:
                if img_path.exists():
                    contents.append(Image.open(img_path))

        contents.append(full_prompt)

        # Call Gemini with media resolution config
        response = client.models.generate_content(
            model=_MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                media_resolution=resolution_map[_MEDIA_RESOLUTION],
            ),
        )
        return response

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response: Any) -> VideoResult:
        """Parse a Gemini GenerateContentResponse into a VideoResult."""
        input_tokens = 0
        output_tokens = 0
        try:
            usage = response.usage_metadata
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        except AttributeError:
            pass

        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

        raw_text = getattr(response, "text", None) or ""
        cleaned = _strip_markdown_fence(raw_text)

        try:
            data = json.loads(cleaned)
            return VideoResult(
                success=True,
                data=data,
                reason=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=raw_text,
            )
        except json.JSONDecodeError as exc:
            logger.warning("Gemini video returned non-JSON: %r — %s", raw_text[:200], exc)
            return VideoResult(
                success=False,
                data=None,
                reason=f"Response was not valid JSON: {exc}. Raw: {raw_text[:200]}",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=raw_text,
            )
