"""
Detection pipeline configuration and dispatch.

Reads the detection method from game configuration and routes symbol detection
to the appropriate pipeline: ``phash``, ``gemini``, or ``both``.

When ``both``:
1. Run pHash first (fast, free).
2. If pHash Hamming distance > threshold (low confidence), also run Gemini.
3. If pHash and Gemini agree → return result with ``flagged_orange=False``.
4. If they disagree → return result with ``flagged_orange=True`` (human review).

Public interface
----------------
DetectionConfig(manifest_path, method, phash_threshold, api_key)
    Detection pipeline dispatcher.

    detect(crop: PIL.Image) -> DetectionResult
        Run the configured detection pipeline on *crop*.

DetectionResult
    Dataclass: symbol (str), confidence (str), flagged_orange (bool),
               phash_result (PhashResult | None), gemini_result (GeminiDetectResult | None).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PIL import Image

from framework.detection.phash import PhashDetector, PhashResult
from framework.detection.gemini_detect import GeminiDetector, GeminiDetectResult

logger = logging.getLogger(__name__)

DetectionMethod = Literal["phash", "gemini", "both"]


@dataclass
class DetectionResult:
    """Outcome of the detection pipeline."""

    symbol: str
    """The identified symbol name."""

    confidence: str
    """Confidence level: ``'high'``, ``'medium'``, or ``'low'``."""

    flagged_orange: bool
    """
    True when pHash and Gemini disagreed (requires human review).
    Only applicable in ``both`` mode.
    """

    phash_result: PhashResult | None = field(default=None)
    """Raw pHash result, if pHash was run."""

    gemini_result: GeminiDetectResult | None = field(default=None)
    """Raw Gemini result, if Gemini was run."""


class DetectionConfig:
    """
    Symbol detection pipeline dispatcher.

    Constructs both a ``PhashDetector`` and a ``GeminiDetector`` from the same
    manifest. The ``detect()`` method routes to the appropriate pipeline based
    on the configured *method*.

    Parameters
    ----------
    manifest_path:
        Path to the ``symbols.yml`` asset manifest.
    method:
        Detection method: ``'phash'``, ``'gemini'``, or ``'both'``.
    phash_threshold:
        Hamming distance threshold for pHash confidence. Default: 10.
    api_key:
        Gemini API key (only used when method includes Gemini).
    """

    def __init__(
        self,
        manifest_path: Path,
        method: DetectionMethod = "both",
        phash_threshold: int = 10,
        api_key: str = "",
    ) -> None:
        self.method: DetectionMethod = method
        self._phash_detector = PhashDetector(
            manifest_path=manifest_path,
            threshold=phash_threshold,
        )
        self._gemini_detector = GeminiDetector(
            manifest_path=manifest_path,
            api_key=api_key,
        )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self, crop: Image.Image) -> DetectionResult:
        """
        Run the configured detection pipeline on *crop*.

        Parameters
        ----------
        crop:
            PIL Image of the symbol to identify.

        Returns
        -------
        DetectionResult
            Identified symbol with confidence and optional orange flag.
        """
        if self.method == "phash":
            return self._run_phash_only(crop)
        elif self.method == "gemini":
            return self._run_gemini_only(crop)
        else:  # 'both'
            return self._run_both(crop)

    # ------------------------------------------------------------------
    # Pipeline implementations
    # ------------------------------------------------------------------

    def _run_phash_only(self, crop: Image.Image) -> DetectionResult:
        phash_res = self._phash_detector.detect(crop)
        confidence = "high" if phash_res.confident else "low"
        return DetectionResult(
            symbol=phash_res.symbol,
            confidence=confidence,
            flagged_orange=False,
            phash_result=phash_res,
            gemini_result=None,
        )

    def _run_gemini_only(self, crop: Image.Image) -> DetectionResult:
        gemini_res = self._gemini_detector.detect(crop)
        return DetectionResult(
            symbol=gemini_res.symbol,
            confidence=gemini_res.confidence,
            flagged_orange=False,
            phash_result=None,
            gemini_result=gemini_res,
        )

    def _run_both(self, crop: Image.Image) -> DetectionResult:
        """
        Run pHash first. If confident, return immediately (no Gemini call).
        If low confidence, run Gemini and check for disagreement.
        """
        phash_res = self._phash_detector.detect(crop)

        if phash_res.confident:
            # pHash is confident — skip Gemini entirely
            logger.debug(
                "Both-mode: pHash confident (distance=%d) → returning '%s'",
                phash_res.hamming_distance,
                phash_res.symbol,
            )
            return DetectionResult(
                symbol=phash_res.symbol,
                confidence="high",
                flagged_orange=False,
                phash_result=phash_res,
                gemini_result=None,
            )

        # pHash low confidence — call Gemini as fallback
        logger.debug(
            "Both-mode: pHash low confidence (distance=%d) → calling Gemini",
            phash_res.hamming_distance,
        )
        gemini_res = self._gemini_detector.detect(crop)

        # Disagreement check
        flagged = phash_res.symbol != gemini_res.symbol
        if flagged:
            logger.warning(
                "Both-mode: pHash='%s' vs Gemini='%s' — DISAGREE, flagging orange",
                phash_res.symbol,
                gemini_res.symbol,
            )

        # When they disagree, trust Gemini (higher semantic accuracy)
        final_symbol = gemini_res.symbol if flagged else phash_res.symbol
        confidence = gemini_res.confidence if flagged else "medium"

        return DetectionResult(
            symbol=final_symbol,
            confidence=confidence,
            flagged_orange=flagged,
            phash_result=phash_res,
            gemini_result=gemini_res,
        )
