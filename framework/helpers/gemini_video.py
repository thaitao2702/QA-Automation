"""
Gemini video analysis helper using google.genai SDK.

Before upload, the trimmed clip is re-encoded into a **montage** video: every output
frame is a composite of ``_MONTAGE_COLS`` sub-frames spaced ``_MONTAGE_INTERVAL_S``
apart, tiled LEFT→RIGHT in time. Gemini samples video at ~1 frame/second and bills a
flat ~70 tokens/frame at LOW/MEDIUM resolution, so packing 4 sub-frames into each
sampled frame buys 0.25s temporal resolution at 1fps token cost (4× the timing detail,
same price). The prompt carries an explanation of the layout so the model reads the
panels left→right, then continues frame-to-frame in time.

Public interface
----------------
GeminiVideoHelper(api_key)
    Video analysis wrapper with cost tracking.

    analyse_video(video_path, prompt, output_schema, ref_images=None) -> VideoResult
        Build the montage, upload and analyse it. Optionally include reference images.

    total_input_tokens: int
    total_output_tokens: int
    cost_summary() -> dict

VideoResult
    Dataclass: success (bool), data (dict | None), reason (str | None),
               input_tokens (int), output_tokens (int), raw_text (str | None).

Pricing
-------
Gemini 3 Flash Preview (used for video — lower token/frame cost):
  Input:  $0.50 per 1M tokens
  Output: $3.00 per 1M tokens

Video token cost per frame (1 frame/second):
  Gemini 3 Flash: 70 tokens/frame (LOW/MEDIUM resolution — identical cost)
  Gemini 2.5 Flash: 256 tokens/frame (medium resolution)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from framework.helpers.gemini_pricing import get_pricing

_DEFAULT_MODEL = "gemini-3-flash-preview"
_DEFAULT_RESOLUTION = "MEDIUM"

# --- Montage packing --------------------------------------------------------
# Each uploaded frame is a horizontal strip of _MONTAGE_COLS sub-frames captured
# _MONTAGE_INTERVAL_S apart. At 1fps Gemini sampling this yields _MONTAGE_INTERVAL_S
# temporal resolution for the ~70-token/frame (LOW/MEDIUM) price of a single frame.
_MONTAGE_COLS = 4                       # sub-frames per composite frame (left→right in time)
_MONTAGE_INTERVAL_S = 0.25              # wall-clock gap between adjacent sub-frames
_MONTAGE_SAMPLE_FPS = int(round(1.0 / _MONTAGE_INTERVAL_S))  # frames pulled before tiling (=4)
_MONTAGE_PADDING_PX = 6                 # white gutter between sub-frames (helps segmentation)
_FFMPEG_TIMEOUT = 120                   # seconds for the montage re-encode


def _montage_filter() -> str:
    """ffmpeg -vf chain: sample at _MONTAGE_SAMPLE_FPS, tile N sub-frames left→right, even dims.

    ``tile=Nx1`` consumes N consecutive sampled frames and emits one composite, so the
    output frame rate is _MONTAGE_SAMPLE_FPS / N = 1fps — exactly Gemini's sampling rate.
    The final scale forces even width/height (required by yuv420p / libx264).
    """
    return (
        f"fps={_MONTAGE_SAMPLE_FPS},"
        f"tile={_MONTAGE_COLS}x1:padding={_MONTAGE_PADDING_PX}:color=white,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def _montage_preamble() -> str:
    """Explain the montage layout to the model so it reads sub-frames left→right in time."""
    n = _MONTAGE_COLS
    interval = _MONTAGE_INTERVAL_S
    stamps = ", ".join(f"+{i * interval:.2f}s" for i in range(n))
    last = f"+{(n - 1) * interval:.2f}s"
    return (
        "HOW TO READ THIS VIDEO (IMPORTANT):\n"
        f"To give you finer timing detail cheaply, every single frame of this video is a "
        f"COMPOSITE image built from {n} sub-frames captured {interval:.2f} seconds apart, "
        "arranged LEFT TO RIGHT in chronological order and separated by thin white gutters. "
        "Within one composite frame, read the panels left-to-right: the leftmost panel is the "
        f"earliest moment (+0.00s) and each panel to its right is {interval:.2f}s later "
        f"({stamps}); the rightmost panel is {last}. The NEXT composite frame continues in "
        "time — its leftmost panel is the moment immediately after the previous frame's "
        "rightmost panel. So the true chronological order is: frame1 left→right, then frame2 "
        f"left→right, and so on — treat the whole clip as one continuous {n}-frames-per-second "
        "sequence when reasoning about motion, transitions, timing, and what happens "
        "before/after."
    )


def _build_montage(source: Path, dest: Path) -> bool:
    """Re-encode *source* into the 1fps montage clip described by ``_montage_filter``.

    Returns True on success, False (logged) on missing ffmpeg, timeout, non-zero exit,
    or missing output — the caller then falls back to uploading the raw clip.
    """
    if not source.exists():
        logger.error("Montage source not found: %s", source)
        return False
    cmd = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-vf", _montage_filter(),
        "-an",                    # drop audio
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found — cannot build montage video. Install from "
            "https://ffmpeg.org/download.html or via your OS package manager."
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out building montage from %s", source)
        return False
    if result.returncode != 0:
        logger.error("ffmpeg montage failed (exit %d): %s", result.returncode, (result.stderr or "")[-500:])
        return False
    if not dest.exists():
        logger.error("ffmpeg produced no montage output for %s", source)
        return False
    return True


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

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        resolution: str | None = None,
        reference: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self._model = model or _DEFAULT_MODEL
        self._resolution = (resolution or _DEFAULT_RESOLUTION).upper()
        self._pricing = get_pricing(self._model)
        # SymbolReference (framework.helpers.symbol_reference) — its labeled images
        # are prepended to EVERY request so the model has ground-truth context.
        self._reference = reference
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
            self._total_input_tokens * self._pricing.input_per_token
            + self._total_output_tokens * self._pricing.output_per_token
        )

    @property
    def model(self) -> str:
        """The video model id (e.g. ``gemini-3-flash-preview``)."""
        return self._model

    def cost_for_tokens(self, input_tokens: int, output_tokens: int) -> float:
        """USD cost of a single call with the given token counts (same pricing as the cumulative estimate)."""
        return (
            input_tokens * self._pricing.input_per_token
            + output_tokens * self._pricing.output_per_token
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
        reference: Any | None = None,
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
            response = self._call_video_api(
                video_path, prompt, output_schema, ref_images, reference=reference
            )
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
            "model": self._model,
            "resolution": self._resolution,
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
        reference: Any | None = None,
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

        # Re-encode into the montage clip (4 sub-frames/second packed left→right) so one
        # sampled frame carries 0.25s temporal resolution at ~70 tokens. On any ffmpeg
        # failure fall back to the raw clip AND drop the montage explanation, so the prompt
        # never describes a layout the uploaded video does not have.
        montage_path = video_path.with_name(f"{video_path.stem}-montage.mp4")
        montage_ok = _build_montage(video_path, montage_path)
        upload_path = montage_path if montage_ok else video_path
        if not montage_ok:
            logger.warning(
                "Montage build failed for %s — uploading raw clip (no sub-frame packing)",
                video_path.name,
            )

        # Upload video and wait for processing
        logger.info("Uploading video: %s", upload_path.name)
        video_file = self._upload_and_wait(client, upload_path)

        # Build contents: video + optional ref images + prompt
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
        # Lead with the montage layout instructions (only when the montage was actually
        # built) so the model knows to read each frame's sub-panels left→right in time.
        preamble = f"{_montage_preamble()}\n\n" if montage_ok else ""
        full_prompt = f"{preamble}{prompt}\n\n{schema_hint}"

        from PIL import Image

        # Prepend the symbol/UI reference pack (labeled ground-truth images) BEFORE
        # the video, so the model can identify what it sees in the video that follows.
        # A per-call ``reference`` (scoped selection) overrides the constructor default.
        contents: list[Any] = self._reference_parts(Image, reference)
        contents.append(video_file)

        # Add per-call reference images if provided (e.g. comparison frames)
        if ref_images:
            for img_path in ref_images:
                if img_path.exists():
                    contents.append(Image.open(img_path))

        contents.append(full_prompt)

        # Call Gemini with media resolution config
        response = client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                media_resolution=resolution_map[self._resolution],
            ),
        )
        return response

    def _reference_parts(self, image_cls: Any, reference: Any | None = None) -> list[Any]:
        """Build [header, label, image, ...] reference parts; [] if no reference.

        A per-call ``reference`` (scoped selection) overrides the constructor default.
        """
        ref = reference if reference is not None else self._reference
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
            "--- END REFERENCE ASSETS. Now analyse the VIDEO below against the task. ---"
        )
        return parts

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
            candidates_tokens = getattr(usage, "candidates_token_count", 0) or 0
            thoughts_tokens = getattr(usage, "thoughts_token_count", 0) or 0
            output_tokens = candidates_tokens + thoughts_tokens
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
