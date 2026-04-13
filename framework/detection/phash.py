"""
Perceptual hash (pHash) symbol detection.

Loads reference symbol images from a ``symbols.yml`` manifest, computes pHash
for each, and identifies the closest match for a given crop image using
Hamming distance.

Public interface
----------------
PhashDetector(manifest_path, threshold)
    Symbol detector using perceptual hashing.

    detect(crop: PIL.Image) -> PhashResult
        Identify the symbol in *crop* by comparing pHash against references.

PhashResult
    Dataclass: symbol (str), hamming_distance (int), confident (bool).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imagehash
import yaml
from PIL import Image

logger = logging.getLogger(__name__)

# Default Hamming distance threshold — distance <= threshold is "confident".
DEFAULT_THRESHOLD: int = 10


@dataclass
class PhashResult:
    """Outcome of a pHash symbol detection."""

    symbol: str
    """Name of the closest matching symbol from the manifest."""

    hamming_distance: int
    """Hamming distance between query hash and closest reference hash."""

    confident: bool
    """True when ``hamming_distance <= threshold``."""


class PhashDetector:
    """
    pHash-based symbol detector.

    On construction, loads all reference images listed in the manifest and
    pre-computes their pHash values. ``detect()`` then computes the pHash of
    the query crop and returns the closest match by Hamming distance.

    Parameters
    ----------
    manifest_path:
        Path to the ``symbols.yml`` asset manifest.
    threshold:
        Hamming distance threshold below which a match is considered confident.
        Default is ``DEFAULT_THRESHOLD`` (10).
    """

    def __init__(
        self,
        manifest_path: Path,
        threshold: int = DEFAULT_THRESHOLD,
    ) -> None:
        self.threshold = threshold
        self._manifest_path = Path(manifest_path)
        self._manifest_dir = self._manifest_path.parent

        # Load and hash all reference symbols
        self._symbol_hashes: dict[str, Any] = {}
        self._load_manifest()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def known_symbols(self) -> set[str]:
        """Set of symbol names loaded from the manifest."""
        return set(self._symbol_hashes.keys())

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self, crop: Image.Image) -> PhashResult:
        """
        Identify the symbol in *crop* by comparing pHash against reference hashes.

        Parameters
        ----------
        crop:
            PIL Image of the symbol to identify (typically a cell crop from the
            game screenshot).

        Returns
        -------
        PhashResult
            The closest matching symbol name, Hamming distance, and confidence flag.
        """
        query_hash = imagehash.phash(crop)

        best_symbol: str = ""
        best_distance: int = 2**64  # impossibly large initial value

        for symbol_name, ref_hash in self._symbol_hashes.items():
            distance = query_hash - ref_hash
            if distance < best_distance:
                best_distance = distance
                best_symbol = symbol_name

        # Coerce numpy types to plain Python ints/bools for clean equality checks.
        distance_int = int(best_distance)
        confident = bool(distance_int <= self.threshold)
        logger.debug(
            "pHash detect: best=%s distance=%d confident=%s",
            best_symbol,
            distance_int,
            confident,
        )
        return PhashResult(
            symbol=best_symbol,
            hamming_distance=distance_int,
            confident=confident,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_manifest(self) -> None:
        """Load manifest and pre-compute pHash for every reference symbol."""
        manifest_text = self._manifest_path.read_text(encoding="utf-8")
        manifest = yaml.safe_load(manifest_text)

        symbols: dict[str, dict] = manifest.get("symbols", {})
        for symbol_key, info in symbols.items():
            file_rel = info.get("file", "")
            file_path = self._manifest_dir / file_rel

            if not file_path.exists():
                raise FileNotFoundError(
                    f"Reference image for symbol '{symbol_key}' not found: {file_path}"
                )

            try:
                img = Image.open(file_path).convert("RGB")
                self._symbol_hashes[symbol_key] = imagehash.phash(img)
                logger.debug("Loaded reference pHash for '%s': %s", symbol_key, file_path)
            except Exception as exc:
                raise OSError(
                    f"Failed to load reference image for '{symbol_key}' at {file_path}: {exc}"
                ) from exc
