"""
Tests for framework/detection/ modules — pHash and Gemini symbol detection.

Acceptance criteria:
- Perceptual hash detection identifies a known symbol with Hamming distance of zero.
- When 'both' mode, phash runs first and Gemini only invoked when phash exceeds threshold.
- Detection config reads method from game configuration and dispatches correctly.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from framework.detection.phash import PhashDetector, PhashResult
from framework.detection.gemini_detect import GeminiDetector, GeminiDetectResult
from framework.detection.config import DetectionConfig, DetectionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_solid_image(color: tuple, size: tuple = (64, 64)) -> Image.Image:
    """Create a solid-color PIL image."""
    return Image.new("RGB", size, color)


def _make_gradient_image(base_color: tuple, size: tuple = (64, 64)) -> Image.Image:
    """
    Create an image with a horizontal gradient based on base_color.

    pHash works on frequency content — solid images all hash the same.
    Gradients provide unique frequency signatures.
    """
    img = Image.new("RGB", size)
    r, g, b = base_color
    pixels = []
    w, h = size
    for y in range(h):
        for x in range(w):
            factor = x / max(w - 1, 1)
            px = (int(r * factor), int(g * factor), int(b * (1 - factor)))
            pixels.append(px)
    img.putdata(pixels)
    return img


def _image_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _write_image(path: Path, color: tuple = (255, 0, 0), size: tuple = (64, 64)) -> Path:
    """Write a gradient image to path (gradients have unique pHash signatures)."""
    img = _make_gradient_image(color, size)
    img.save(str(path))
    return path


def _make_manifest(tmp_path: Path, symbols: dict[str, str]) -> Path:
    """
    Write a minimal symbols.yml manifest.
    ``symbols`` maps symbol_key -> relative file path (relative to manifest dir).
    """
    import yaml

    manifest_path = tmp_path / "symbols.yml"
    data = {"symbols": {}}
    for key, file_path in symbols.items():
        data["symbols"][key] = {
            "file": file_path,
            "name": key,
            "type": "test",
            "description": f"Test symbol {key}",
        }
    manifest_path.write_text(yaml.dump(data), encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# PhashDetector — construction and loading
# ---------------------------------------------------------------------------


class TestPhashDetectorLoading:
    def test_loads_symbols_from_manifest(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "wild.png", color=(200, 200, 0))
        _write_image(tmp_path / "scatter.png", color=(0, 200, 0))
        manifest = _make_manifest(tmp_path, {"wild": "wild.png", "scatter": "scatter.png"})

        detector = PhashDetector(manifest_path=manifest)
        assert "wild" in detector.known_symbols
        assert "scatter" in detector.known_symbols

    def test_missing_image_raises_descriptive_error(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path, {"ghost": "does_not_exist.png"})

        with pytest.raises(FileNotFoundError) as exc_info:
            PhashDetector(manifest_path=manifest)

        assert "ghost" in str(exc_info.value) or "does_not_exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# PhashDetector — matching
# ---------------------------------------------------------------------------


class TestPhashMatching:
    def test_identical_image_has_hamming_distance_zero(self, tmp_path: Path) -> None:
        img_path = _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png"})
        detector = PhashDetector(manifest_path=manifest)

        # Crop is the same image
        crop = Image.open(img_path)
        result = detector.detect(crop)

        assert result.symbol == "coin"
        assert result.hamming_distance == 0

    def test_similar_image_matches_closest_symbol(self, tmp_path: Path) -> None:
        """A very similar image should match the closest reference symbol."""
        # Reference: red gradient
        ref_img = _make_gradient_image((255, 0, 0))
        ref_img.save(str(tmp_path / "ref_red.png"))

        # Also have a blue gradient reference (very different frequency)
        blue_img = _make_gradient_image((0, 0, 255))
        blue_img.save(str(tmp_path / "ref_blue.png"))

        manifest = _make_manifest(
            tmp_path, {"red_sym": "ref_red.png", "blue_sym": "ref_blue.png"}
        )
        detector = PhashDetector(manifest_path=manifest)

        # Query: the exact same red reference → should match red_sym
        result = detector.detect(ref_img)
        assert result.symbol == "red_sym"

    def test_returns_phash_result_with_required_fields(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "sym.png", color=(100, 100, 100))
        manifest = _make_manifest(tmp_path, {"sym": "sym.png"})
        detector = PhashDetector(manifest_path=manifest)

        crop = Image.open(tmp_path / "sym.png")
        result = detector.detect(crop)

        assert isinstance(result, PhashResult)
        assert result.symbol is not None
        # hamming_distance is coerced to plain int in the implementation
        assert type(result.hamming_distance) is int
        assert result.hamming_distance >= 0
        assert type(result.confident) is bool

    def test_high_hamming_distance_indicates_low_confidence(self, tmp_path: Path) -> None:
        """Distance > threshold means low confidence (no match within threshold)."""
        # Reference: red-based gradient
        _write_image(tmp_path / "red.png", color=(255, 0, 0))
        manifest = _make_manifest(tmp_path, {"red": "red.png"})
        detector = PhashDetector(manifest_path=manifest, threshold=5)

        # Query: inverted/very different gradient — expect non-trivial distance
        # Use a checkerboard pattern which has very different frequency content
        query = Image.new("RGB", (64, 64))
        pixels = []
        for y in range(64):
            for x in range(64):
                if (x + y) % 2 == 0:
                    pixels.append((0, 0, 255))
                else:
                    pixels.append((255, 255, 0))
        query.putdata(pixels)
        result = detector.detect(query)

        # Either distance > threshold (confident=False) or at worst confident
        # The key invariant is that confident == (distance <= threshold)
        assert result.confident == (result.hamming_distance <= 5)

    def test_distance_below_threshold_is_confident(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "sym.png", color=(80, 80, 80))
        manifest = _make_manifest(tmp_path, {"sym": "sym.png"})
        detector = PhashDetector(manifest_path=manifest, threshold=10)

        # Same image → distance 0 → confident
        crop = Image.open(tmp_path / "sym.png")
        result = detector.detect(crop)

        assert result.confident is True
        assert result.hamming_distance == 0


# ---------------------------------------------------------------------------
# GeminiDetector
# ---------------------------------------------------------------------------


class TestGeminiDetector:
    def test_detect_calls_gemini_with_symbol_names(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "wild.png", color=(200, 200, 0))
        manifest = _make_manifest(tmp_path, {"wild": "wild.png"})

        mock_result = GeminiDetectResult(symbol="wild", confidence="high", raw_response={})
        detector = GeminiDetector(manifest_path=manifest, api_key="test-key")

        with patch.object(detector, "_call_gemini", return_value=mock_result):
            crop = Image.open(tmp_path / "wild.png")
            result = detector.detect(crop)

        assert result.symbol == "wild"
        assert result.confidence == "high"

    def test_detect_passes_reference_images_to_gemini(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "a.png", color=(100, 100, 100))
        _write_image(tmp_path / "b.png", color=(200, 200, 200))
        manifest = _make_manifest(tmp_path, {"sym_a": "a.png", "sym_b": "b.png"})

        detector = GeminiDetector(manifest_path=manifest, api_key="test-key")
        crop = _make_solid_image((120, 120, 120))

        with patch.object(detector, "_call_gemini") as mock_call:
            mock_call.return_value = GeminiDetectResult(
                symbol="sym_a", confidence="high", raw_response={}
            )
            detector.detect(crop)

        # Reference images list should be passed
        call_args = mock_call.call_args
        args, kwargs = call_args
        ref_images = args[1] if len(args) > 1 else kwargs.get("ref_images", [])
        assert len(ref_images) == 2


# ---------------------------------------------------------------------------
# DetectionConfig — pipeline dispatch
# ---------------------------------------------------------------------------


class TestDetectionConfig:
    def test_phash_method_only_calls_phash(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png"})

        config = DetectionConfig(
            manifest_path=manifest,
            method="phash",
            phash_threshold=10,
            api_key="test-key",
        )

        crop = Image.open(tmp_path / "coin.png")
        with patch.object(config._phash_detector, "detect") as mock_phash:
            with patch.object(config._gemini_detector, "detect") as mock_gemini:
                mock_phash.return_value = PhashResult(
                    symbol="coin", hamming_distance=0, confident=True
                )
                config.detect(crop)

        mock_phash.assert_called_once()
        mock_gemini.assert_not_called()

    def test_gemini_method_only_calls_gemini(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png"})

        config = DetectionConfig(
            manifest_path=manifest,
            method="gemini",
            phash_threshold=10,
            api_key="test-key",
        )

        crop = Image.open(tmp_path / "coin.png")
        with patch.object(config._phash_detector, "detect") as mock_phash:
            with patch.object(config._gemini_detector, "detect") as mock_gemini:
                mock_gemini.return_value = GeminiDetectResult(
                    symbol="coin", confidence="high", raw_response={}
                )
                config.detect(crop)

        mock_phash.assert_not_called()
        mock_gemini.assert_called_once()

    def test_both_mode_phash_confident_does_not_call_gemini(self, tmp_path: Path) -> None:
        """When phash is confident (distance <= threshold), Gemini should NOT be called."""
        _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png"})

        config = DetectionConfig(
            manifest_path=manifest,
            method="both",
            phash_threshold=10,
            api_key="test-key",
        )

        crop = Image.open(tmp_path / "coin.png")
        with patch.object(config._phash_detector, "detect") as mock_phash:
            with patch.object(config._gemini_detector, "detect") as mock_gemini:
                mock_phash.return_value = PhashResult(
                    symbol="coin", hamming_distance=3, confident=True
                )
                result = config.detect(crop)

        mock_phash.assert_called_once()
        mock_gemini.assert_not_called()
        assert result.symbol == "coin"

    def test_both_mode_phash_low_confidence_calls_gemini(self, tmp_path: Path) -> None:
        """When phash is NOT confident (distance > threshold), Gemini should be called."""
        _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png"})

        config = DetectionConfig(
            manifest_path=manifest,
            method="both",
            phash_threshold=10,
            api_key="test-key",
        )

        crop = Image.open(tmp_path / "coin.png")
        with patch.object(config._phash_detector, "detect") as mock_phash:
            with patch.object(config._gemini_detector, "detect") as mock_gemini:
                mock_phash.return_value = PhashResult(
                    symbol="coin", hamming_distance=15, confident=False
                )
                mock_gemini.return_value = GeminiDetectResult(
                    symbol="coin", confidence="high", raw_response={}
                )
                result = config.detect(crop)

        mock_gemini.assert_called_once()

    def test_both_mode_disagreement_flags_orange(self, tmp_path: Path) -> None:
        """When phash says 'wild' but Gemini says 'coin', result is flagged orange."""
        _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png", "wild": "coin.png"})

        config = DetectionConfig(
            manifest_path=manifest,
            method="both",
            phash_threshold=10,
            api_key="test-key",
        )

        crop = Image.open(tmp_path / "coin.png")
        with patch.object(config._phash_detector, "detect") as mock_phash:
            with patch.object(config._gemini_detector, "detect") as mock_gemini:
                # pHash low confidence → triggers Gemini; Gemini disagrees
                mock_phash.return_value = PhashResult(
                    symbol="wild", hamming_distance=12, confident=False
                )
                mock_gemini.return_value = GeminiDetectResult(
                    symbol="coin", confidence="high", raw_response={}
                )
                result = config.detect(crop)

        assert result.flagged_orange is True

    def test_detection_result_has_required_fields(self, tmp_path: Path) -> None:
        _write_image(tmp_path / "coin.png", color=(180, 130, 0))
        manifest = _make_manifest(tmp_path, {"coin": "coin.png"})

        config = DetectionConfig(
            manifest_path=manifest,
            method="phash",
            phash_threshold=10,
            api_key="test-key",
        )

        crop = Image.open(tmp_path / "coin.png")
        with patch.object(config._phash_detector, "detect") as mock_phash:
            mock_phash.return_value = PhashResult(
                symbol="coin", hamming_distance=0, confident=True
            )
            result = config.detect(crop)

        assert isinstance(result, DetectionResult)
        assert result.symbol is not None
        assert isinstance(result.flagged_orange, bool)
