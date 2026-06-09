"""
Tests for framework/helpers/gemini_video.py — Gemini video analysis.

Acceptance criteria:
- Sending a video with prompt returns a structured VideoResult.
- Token usage and cost tracking accumulate correctly across calls.
- Graceful failure on API errors (never raises, returns failure result).
- Model and resolution are configurable.
- Cost calculation uses correct per-model pricing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.helpers.gemini_video import GeminiVideoHelper, VideoResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_helper(
    api_key: str = "test-key",
    model: str | None = None,
    resolution: str | None = None,
) -> GeminiVideoHelper:
    return GeminiVideoHelper(api_key=api_key, model=model, resolution=resolution)


def _make_video_path(tmp_path: Path) -> Path:
    """Create a stub WebM file."""
    p = tmp_path / "session.webm"
    p.write_bytes(b"WEBM_STUB_CONTENT")
    return p


def _make_video_response(
    text: str,
    input_tokens: int = 2000,
    output_tokens: int = 150,
    thoughts_tokens: int = 0,
) -> MagicMock:
    mock = MagicMock()
    mock.text = text
    mock.usage_metadata.prompt_token_count = input_tokens
    mock.usage_metadata.candidates_token_count = output_tokens
    mock.usage_metadata.thoughts_token_count = thoughts_tokens
    return mock


# ---------------------------------------------------------------------------
# Construction and configuration
# ---------------------------------------------------------------------------


class TestGeminiVideoHelperConstruction:
    def test_stores_api_key(self) -> None:
        helper = GeminiVideoHelper(api_key="key-123")
        assert helper.api_key == "key-123"

    def test_initial_token_counts_are_zero(self) -> None:
        helper = _make_helper()
        assert helper.total_input_tokens == 0
        assert helper.total_output_tokens == 0

    def test_default_model(self) -> None:
        helper = _make_helper()
        assert helper._model == "gemini-3-flash-preview"

    def test_custom_model(self) -> None:
        helper = _make_helper(model="gemini-2.5-flash")
        assert helper._model == "gemini-2.5-flash"

    def test_default_resolution(self) -> None:
        helper = _make_helper()
        assert helper._resolution == "MEDIUM"

    def test_custom_resolution(self) -> None:
        helper = _make_helper(resolution="HIGH")
        assert helper._resolution == "HIGH"

    def test_resolution_normalized_to_uppercase(self) -> None:
        helper = _make_helper(resolution="low")
        assert helper._resolution == "LOW"


# ---------------------------------------------------------------------------
# Context management (no-op)
# ---------------------------------------------------------------------------


class TestContextManagement:
    def test_create_cached_context_returns_inline(self, tmp_path: Path) -> None:
        helper = _make_helper()
        ctx_id = helper.create_cached_context([])
        assert ctx_id == "inline"

    def test_release_context_does_not_raise(self) -> None:
        helper = _make_helper()
        helper.release_context("inline")  # Should not raise


# ---------------------------------------------------------------------------
# Analyse video
# ---------------------------------------------------------------------------


class TestAnalyseVideo:
    def test_returns_video_result_object(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response('{"spin_triggered": true, "confidence": "high"}')

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            result = helper.analyse_video(video, "inline", "Did spin trigger?", {})

        assert isinstance(result, VideoResult)

    def test_parsed_dict_matches_response_structure(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response(
            '{"spin_triggered": true, "reels_spun": true, "confidence": "high"}'
        )

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            result = helper.analyse_video(
                video, "inline", "Analyse this spin", {"spin_triggered": {"type": "boolean"}}
            )

        assert result.success is True
        assert result.data["spin_triggered"] is True
        assert result.data["confidence"] == "high"


# ---------------------------------------------------------------------------
# Token usage and cost tracking
# ---------------------------------------------------------------------------


class TestVideoTokenTracking:
    def test_tracks_input_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response('{"x": 1}', input_tokens=5000, output_tokens=200)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, "inline", "prompt", {})

        assert helper.total_input_tokens == 5000

    def test_multiple_calls_accumulate_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response('{"x": 1}', input_tokens=3000, output_tokens=100)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, "inline", "p1", {})
            helper.analyse_video(video, "inline", "p2", {})

        assert helper.total_input_tokens == 6000
        assert helper.total_output_tokens == 200

    def test_thoughts_tokens_included_in_output(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response(
            '{"x": 1}', input_tokens=2000, output_tokens=100, thoughts_tokens=500
        )

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, "inline", "prompt", {})

        assert helper.total_output_tokens == 600  # 100 candidates + 500 thoughts

    def test_cost_summary_includes_video_calls(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response('{"x": 1}', input_tokens=1000, output_tokens=50)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, "inline", "prompt", {})

        summary = helper.cost_summary()
        assert summary["gemini_video_calls"] == 1
        assert summary["model"] == "gemini-3-flash-preview"
        assert summary["resolution"] == "MEDIUM"
        assert "estimated_cost_usd" in summary

    def test_cost_uses_model_pricing(self, tmp_path: Path) -> None:
        """Gemini 3 Flash: $0.50/1M in, $3.00/1M out."""
        helper = _make_helper(model="gemini-3-flash-preview")
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response(
            '{"x": 1}', input_tokens=1_000_000, output_tokens=1_000_000
        )

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, "inline", "prompt", {})

        # 1M input × $0.50/1M + 1M output × $3.00/1M = $3.50
        assert helper.estimated_cost_usd == pytest.approx(3.50, abs=0.01)

    def test_cost_differs_by_model(self, tmp_path: Path) -> None:
        """Gemini 2.5 Flash should be cheaper per output token."""
        helper_25 = _make_helper(model="gemini-2.5-flash")
        helper_3 = _make_helper(model="gemini-3-flash-preview")
        video = _make_video_path(tmp_path)
        mock_resp = _make_video_response(
            '{"x": 1}', input_tokens=10000, output_tokens=1000
        )

        with patch.object(helper_25, "_call_video_api", return_value=mock_resp):
            helper_25.analyse_video(video, "inline", "prompt", {})
        with patch.object(helper_3, "_call_video_api", return_value=mock_resp):
            helper_3.analyse_video(video, "inline", "prompt", {})

        # Same tokens, different pricing → different costs
        assert helper_25.estimated_cost_usd != helper_3.estimated_cost_usd


# ---------------------------------------------------------------------------
# Graceful failure
# ---------------------------------------------------------------------------


class TestVideoGracefulFailure:
    def test_api_error_returns_failure_result(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)

        with patch.object(helper, "_call_video_api", side_effect=Exception("API error")):
            result = helper.analyse_video(video, "inline", "prompt", {})

        assert result.success is False
        assert result.reason is not None

    def test_api_error_does_not_raise(self, tmp_path: Path) -> None:
        helper = _make_helper()
        video = _make_video_path(tmp_path)

        with patch.object(helper, "_call_video_api", side_effect=RuntimeError("network down")):
            result = helper.analyse_video(video, "inline", "prompt", {})

        assert isinstance(result, VideoResult)
