"""
Gemini-based symbol detection.

Sends a screenshot crop together with reference symbol images to Gemini and
asks it to identify which symbol is displayed.

Public interface
----------------
GeminiDetector(manifest_path, api_key)
    Symbol detector using Gemini 2.5 Flash.

    detect(crop: PIL.Image) -> GeminiDetectResult
        Identify the symbol in *crop* using Gemini with reference images.

GeminiDetectResult
    Dataclass: symbol (str), confidence (str), raw_response (dict).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"

_DETECT_PROMPT_TEMPLATE = """\
You are a game symbol analyst. Below are reference images of game symbols with their names.

Reference symbols:
{symbol_list}

The final image is a screenshot crop from the game. Compare it against the reference \
images and identify which symbol it shows.

Respond ONLY with valid JSON:
{{"symbol": "<symbol_name_from_list>", "confidence": "<high|medium|low>"}}
"""


@dataclass
class GeminiDetectResult:
    """Outcome of a Gemini symbol detection call."""

    symbol: str
    """Name of the identified symbol."""

    confidence: str
    """Confidence level: ``'high'``, ``'medium'``, or ``'low'``."""

    raw_response: dict[str, Any]
    """The raw parsed JSON dict from Gemini."""


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)```$", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


class GeminiDetector:
    """
    Gemini 2.5 Flash symbol detector.

    Sends the query crop alongside all reference symbol images and asks Gemini
    to identify which symbol is shown. Reference images are loaded from the
    manifest on construction.

    Parameters
    ----------
    manifest_path:
        Path to the ``symbols.yml`` asset manifest.
    api_key:
        Gemini API key.
    """

    def __init__(self, manifest_path: Path, api_key: str) -> None:
        self.api_key = api_key
        self._manifest_path = Path(manifest_path)
        self._manifest_dir = self._manifest_path.parent
        self._symbol_info: dict[str, dict] = {}
        self._load_manifest()

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self, crop: Image.Image) -> GeminiDetectResult:
        """
        Identify the symbol in *crop* by sending it and reference images to Gemini.

        Parameters
        ----------
        crop:
            PIL Image crop to identify.

        Returns
        -------
        GeminiDetectResult
            The identified symbol name, confidence level, and raw parsed response.
        """
        ref_images = self._load_reference_images()
        return self._call_gemini(crop, ref_images)

    # ------------------------------------------------------------------
    # Internal — separated for mocking in tests
    # ------------------------------------------------------------------

    def _call_gemini(
        self,
        crop: Image.Image,
        ref_images: list[tuple[str, Image.Image]],
    ) -> GeminiDetectResult:
        """
        Execute the Gemini API call.

        Parameters
        ----------
        crop:
            Query image crop.
        ref_images:
            List of ``(symbol_name, PIL.Image)`` tuples for reference.

        Separated from ``detect()`` so tests can patch ``_call_gemini`` directly.
        """
        import google.generativeai as genai

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(_MODEL_NAME)

        symbol_list = "\n".join(
            f"- {name}: {self._symbol_info.get(name, {}).get('description', name)}"
            for name, _ in ref_images
        )
        prompt = _DETECT_PROMPT_TEMPLATE.format(symbol_list=symbol_list)

        # Content: reference images + query crop + prompt
        content_parts: list[Any] = []
        for _name, img in ref_images:
            content_parts.append(img)
        content_parts.append(crop)
        content_parts.append(prompt)

        response = model.generate_content(content_parts)
        raw_text = getattr(response, "text", "") or ""
        cleaned = _strip_fence(raw_text)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Gemini detect: non-JSON response: %r", raw_text[:200])
            data = {"symbol": "unknown", "confidence": "low"}

        return GeminiDetectResult(
            symbol=data.get("symbol", "unknown"),
            confidence=data.get("confidence", "low"),
            raw_response=data,
        )

    def _load_reference_images(self) -> list[tuple[str, Image.Image]]:
        """Load all reference images from the manifest as PIL Images."""
        result = []
        for symbol_key, info in self._symbol_info.items():
            file_path = self._manifest_dir / info["file"]
            img = Image.open(file_path).convert("RGB")
            result.append((symbol_key, img))
        return result

    def _load_manifest(self) -> None:
        manifest_text = self._manifest_path.read_text(encoding="utf-8")
        manifest = yaml.safe_load(manifest_text)
        self._symbol_info = manifest.get("symbols", {})
