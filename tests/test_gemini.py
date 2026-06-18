"""
Tests for framework/helpers/gemini.py — Gemini 2.5 Flash image analysis helper.

Acceptance criteria:
- Sending an image with prompt and output schema to Gemini returns a parsed
  dict matching the requested schema structure.
- Token usage (input and output tokens) is tracked per call and accessible as
  cumulative totals for cost aggregation.
- When Gemini API is unavailable or returns an error, the method returns a
  structured failure result with a reason string.
- Estimated cost calculation uses token counts and the Gemini 2.5 Flash pricing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.helpers.gemini import GeminiImageHelper, GeminiResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_helper(api_key: str = "test-key") -> GeminiImageHelper:
    return GeminiImageHelper(api_key=api_key)


def _make_mock_response(
    text: str,
    input_tokens: int = 1000,
    output_tokens: int = 100,
) -> MagicMock:
    """Build a mock Gemini GenerateContentResponse."""
    mock_resp = MagicMock()
    mock_resp.text = text
    mock_resp.usage_metadata.prompt_token_count = input_tokens
    mock_resp.usage_metadata.candidates_token_count = output_tokens
    mock_resp.usage_metadata.thoughts_token_count = 0
    return mock_resp


def _make_image_path(tmp_path: Path) -> Path:
    """Write a minimal valid PNG to a temp file and return its path."""
    # 1x1 white PNG (valid but tiny)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
        b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img = tmp_path / "screenshot.png"
    img.write_bytes(png_bytes)
    return img


# ---------------------------------------------------------------------------
# Construction and configuration
# ---------------------------------------------------------------------------


class TestGeminiImageHelperConstruction:
    def test_stores_api_key(self) -> None:
        helper = GeminiImageHelper(api_key="my-key")
        assert helper.api_key == "my-key"

    def test_initial_token_counts_are_zero(self) -> None:
        helper = _make_helper()
        assert helper.total_input_tokens == 0
        assert helper.total_output_tokens == 0

    def test_initial_call_count_is_zero(self) -> None:
        helper = _make_helper()
        assert helper.call_count == 0


# ---------------------------------------------------------------------------
# Analyse image — structured response parsing
# ---------------------------------------------------------------------------


class TestAnalyseImage:
    def test_returns_gemini_result_object(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        schema = {"free_spins": {"type": "number"}, "confidence": {"type": "string"}}
        mock_resp = _make_mock_response('{"free_spins": 3, "confidence": "high"}')

        with patch.object(helper, "_call_api", return_value=mock_resp):
            result = helper.analyse_image(img, prompt="How many spins?", output_schema=schema)

        assert isinstance(result, GeminiResult)

    def test_parsed_dict_matches_schema_structure(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        schema = {"free_spins": {"type": "number"}, "confidence": {"type": "string"}}
        mock_resp = _make_mock_response('{"free_spins": 3, "confidence": "high"}')

        with patch.object(helper, "_call_api", return_value=mock_resp):
            result = helper.analyse_image(img, prompt="How many spins?", output_schema=schema)

        assert result.success is True
        assert result.data["free_spins"] == 3
        assert result.data["confidence"] == "high"

    def test_response_with_markdown_json_fence_is_parsed(self, tmp_path: Path) -> None:
        """Gemini sometimes wraps JSON in ```json ... ``` fences."""
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        schema = {"value": {"type": "number"}}
        raw = "```json\n{\"value\": 42}\n```"
        mock_resp = _make_mock_response(raw)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            result = helper.analyse_image(img, prompt="What value?", output_schema=schema)

        assert result.success is True
        assert result.data["value"] == 42

    def test_image_path_is_passed_to_api(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"ok": true}')

        with patch.object(helper, "_call_api", return_value=mock_resp) as mock_call:
            helper.analyse_image(img, prompt="Check image", output_schema={"ok": {"type": "boolean"}})

        mock_call.assert_called_once()
        call_args = mock_call.call_args
        # Image path should be passed (either positional or keyword)
        args, kwargs = call_args
        assert img in args or img in kwargs.values()


# ---------------------------------------------------------------------------
# Token usage and cost tracking
# ---------------------------------------------------------------------------


class TestTokenUsageTracking:
    def test_single_call_tracks_input_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=2500, output_tokens=50)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.total_input_tokens == 2500

    def test_single_call_tracks_output_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=2500, output_tokens=50)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.total_output_tokens == 50

    def test_multiple_calls_accumulate_input_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=1000, output_tokens=100)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.total_input_tokens == 3000

    def test_multiple_calls_accumulate_output_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=1000, output_tokens=100)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.total_output_tokens == 200

    def test_call_count_increments(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=500, output_tokens=50)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.call_count == 2

    def test_estimated_cost_increases_with_calls(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=10000, output_tokens=500)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        cost_one = helper.estimated_cost_usd
        assert cost_one > 0.0

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.estimated_cost_usd > cost_one

    def test_cost_summary_returns_dict_with_required_keys(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=1000, output_tokens=100)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        summary = helper.cost_summary()
        assert "gemini_image_calls" in summary
        assert "gemini_input_tokens" in summary
        assert "gemini_output_tokens" in summary
        assert "estimated_cost_usd" in summary
        assert summary["gemini_image_calls"] == 1


# ---------------------------------------------------------------------------
# Graceful failure handling
# ---------------------------------------------------------------------------


class TestGracefulFailure:
    def test_api_exception_returns_failure_result(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)

        with patch.object(helper, "_call_api", side_effect=Exception("API unavailable")):
            result = helper.analyse_image(
                img, prompt="test", output_schema={"x": {"type": "number"}}
            )

        assert result.success is False
        assert result.data is None

    def test_api_exception_result_has_reason_string(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)

        with patch.object(helper, "_call_api", side_effect=Exception("Connection timeout")):
            result = helper.analyse_image(
                img, prompt="test", output_schema={"x": {"type": "number"}}
            )

        assert result.reason is not None
        assert len(result.reason) > 0

    def test_invalid_json_response_returns_failure(self, tmp_path: Path) -> None:
        """If Gemini returns non-JSON text, result should be a structured failure."""
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response("Sorry, I cannot analyse this image.")

        with patch.object(helper, "_call_api", return_value=mock_resp):
            result = helper.analyse_image(
                img, prompt="test", output_schema={"x": {"type": "number"}}
            )

        assert result.success is False
        assert result.reason is not None

    def test_failure_does_not_raise_exception(self, tmp_path: Path) -> None:
        """No unhandled exception should escape from analyse_image."""
        helper = _make_helper()
        img = _make_image_path(tmp_path)

        with patch.object(helper, "_call_api", side_effect=RuntimeError("network error")):
            # Should not raise
            result = helper.analyse_image(
                img, prompt="test", output_schema={"x": {"type": "number"}}
            )

        assert isinstance(result, GeminiResult)

    def test_failure_does_not_increment_call_count_for_tracking(
        self, tmp_path: Path
    ) -> None:
        """Failed calls should still be counted so cost reports are accurate."""
        helper = _make_helper()
        img = _make_image_path(tmp_path)

        with patch.object(helper, "_call_api", side_effect=Exception("error")):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        # call_count tracks attempts regardless of success
        assert helper.call_count == 1


# ---------------------------------------------------------------------------
# Cost calculation correctness
# ---------------------------------------------------------------------------


class TestCostCalculation:
    def test_cost_is_nonzero_after_real_token_counts(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        # Gemini 2.5 Flash pricing: $0.30 per 1M input, $2.50 per 1M output
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=100_000, output_tokens=1_000)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        assert helper.estimated_cost_usd > 0.0

    def test_reset_clears_all_counters(self, tmp_path: Path) -> None:
        helper = _make_helper()
        img = _make_image_path(tmp_path)
        mock_resp = _make_mock_response('{"x": 1}', input_tokens=500, output_tokens=50)

        with patch.object(helper, "_call_api", return_value=mock_resp):
            helper.analyse_image(img, prompt="test", output_schema={"x": {"type": "number"}})

        helper.reset()
        assert helper.total_input_tokens == 0
        assert helper.total_output_tokens == 0
        assert helper.call_count == 0
        assert helper.estimated_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Per-call reference override (scoped selection)
# ---------------------------------------------------------------------------


class _FakeRef:
    """Minimal SymbolReference stand-in for _reference_parts."""

    def __init__(self, header: str, items: list[tuple[Path, str]]):
        self.header = header
        self.items = items

    @property
    def is_empty(self) -> bool:
        return not self.items


class _FakeImage:
    """image_cls stub: .open(path) returns a sentinel string."""

    @staticmethod
    def open(path):
        return f"IMG:{Path(path).name}"


class TestReferenceOverride:
    def test_per_call_reference_overrides_constructor_default(self):
        default = _FakeRef("DEFAULT", [(Path("a/coin.jpg"), "Coin")])
        helper = GeminiImageHelper(api_key="k", reference=default)

        scoped = _FakeRef("SCOPED", [(Path("b/bet.jpg"), "Bet")])
        parts = helper._reference_parts(_FakeImage, scoped)
        assert parts[0] == "SCOPED"
        assert "IMG:bet.jpg" in parts
        assert "IMG:coin.jpg" not in parts

    def test_none_reference_uses_constructor_default(self):
        default = _FakeRef("DEFAULT", [(Path("a/coin.jpg"), "Coin")])
        helper = GeminiImageHelper(api_key="k", reference=default)
        parts = helper._reference_parts(_FakeImage, None)
        assert parts[0] == "DEFAULT"
        assert "IMG:coin.jpg" in parts

    def test_analyse_image_threads_reference_into_call_api(self):
        helper = GeminiImageHelper(api_key="k")
        scoped = _FakeRef("SCOPED", [])
        with patch.object(helper, "_call_api", return_value=_make_mock_response('{"x": 1}')) as m:
            helper.analyse_image(
                Path("s.png"), prompt="p", output_schema={"x": {"type": "number"}},
                reference=scoped,
            )
        assert m.call_args.kwargs["reference"] is scoped
