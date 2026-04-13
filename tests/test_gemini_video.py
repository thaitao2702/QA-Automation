"""
Tests for framework/helpers/gemini_video.py — Gemini 2.5 Flash video analysis.

Acceptance criteria:
- Creating a cached context from reference asset images succeeds and returns a
  reusable context identifier.
- Sending a video with cached context and prompt returns a structured response
  matching the output schema.
- Multiple video analyses reusing the same cached context do not re-upload
  reference images.
- Cached context is properly released/deleted when batch processing completes.
- Token usage and cost tracking work identically to the image helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from framework.helpers.gemini_video import GeminiVideoHelper, VideoResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_helper(api_key: str = "test-key") -> GeminiVideoHelper:
    return GeminiVideoHelper(api_key=api_key)


def _make_image_paths(tmp_path: Path, count: int = 2) -> list[Path]:
    """Create minimal PNG files for reference assets."""
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
        b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    paths = []
    for i in range(count):
        p = tmp_path / f"symbol_{i}.png"
        p.write_bytes(png_bytes)
        paths.append(p)
    return paths


def _make_video_path(tmp_path: Path) -> Path:
    """Create a stub WebM file."""
    p = tmp_path / "session.webm"
    p.write_bytes(b"WEBM_STUB_CONTENT")
    return p


def _make_cache_response(name: str = "cached_contexts/test-ctx-id") -> MagicMock:
    """Return a mock CachedContent object."""
    mock = MagicMock()
    mock.name = name
    return mock


def _make_video_response(
    text: str,
    input_tokens: int = 2000,
    output_tokens: int = 150,
) -> MagicMock:
    mock = MagicMock()
    mock.text = text
    mock.usage_metadata.prompt_token_count = input_tokens
    mock.usage_metadata.candidates_token_count = output_tokens
    return mock


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestGeminiVideoHelperConstruction:
    def test_stores_api_key(self) -> None:
        helper = GeminiVideoHelper(api_key="key-123")
        assert helper.api_key == "key-123"

    def test_initial_token_counts_are_zero(self) -> None:
        helper = _make_helper()
        assert helper.total_input_tokens == 0
        assert helper.total_output_tokens == 0

    def test_no_active_contexts_at_start(self) -> None:
        helper = _make_helper()
        assert helper.active_context_count == 0


# ---------------------------------------------------------------------------
# Create cached context
# ---------------------------------------------------------------------------


class TestCreateCachedContext:
    def test_returns_context_identifier_string(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path, 3)
        mock_cache = _make_cache_response("cached_contexts/abc123")

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        assert isinstance(ctx_id, str)
        assert len(ctx_id) > 0

    def test_context_id_derived_from_api_response_name(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/my-context-id")

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        assert "my-context-id" in ctx_id

    def test_create_cache_called_with_image_paths(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path, 2)
        mock_cache = _make_cache_response()

        with patch.object(helper, "_create_cache", return_value=mock_cache) as mock_create:
            helper.create_cached_context(images)

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        # Image paths must be passed to the cache creation
        args, kwargs = call_args
        passed_images = args[0] if args else kwargs.get("image_paths", [])
        assert set(passed_images) == set(images)

    def test_active_context_count_increments(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/ctx-1")

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            helper.create_cached_context(images)

        assert helper.active_context_count == 1

    def test_multiple_contexts_tracked(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        mock_cache_a = _make_cache_response("cached_contexts/ctx-a")
        mock_cache_b = _make_cache_response("cached_contexts/ctx-b")

        with patch.object(helper, "_create_cache", side_effect=[mock_cache_a, mock_cache_b]):
            helper.create_cached_context(images)
            helper.create_cached_context(images)

        assert helper.active_context_count == 2


# ---------------------------------------------------------------------------
# Analyse video with cached context
# ---------------------------------------------------------------------------


class TestAnalyseVideo:
    def test_returns_video_result_object(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/ctx-id")
        mock_resp = _make_video_response('{"spin_triggered": true, "confidence": "high"}')

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            result = helper.analyse_video(video, ctx_id, "Did spin trigger?", {})

        assert isinstance(result, VideoResult)

    def test_parsed_dict_matches_response_structure(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/ctx-id")
        mock_resp = _make_video_response(
            '{"spin_triggered": true, "reels_spun": true, "confidence": "high"}'
        )

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            result = helper.analyse_video(
                video, ctx_id, "Analyse this spin", {"spin_triggered": {"type": "boolean"}}
            )

        assert result.success is True
        assert result.data["spin_triggered"] is True
        assert result.data["confidence"] == "high"

    def test_context_id_passed_to_api_not_raw_images(self, tmp_path: Path) -> None:
        """The most important acceptance criterion: context ID is reused, images not re-uploaded."""
        helper = _make_helper()
        images = _make_image_paths(tmp_path, 3)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/shared-ctx")
        mock_resp = _make_video_response('{"ok": true}')

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", return_value=mock_resp) as mock_api:
            helper.analyse_video(video, ctx_id, "Test prompt", {})
            helper.analyse_video(video, ctx_id, "Test prompt 2", {})

        # _call_video_api called twice
        assert mock_api.call_count == 2
        # Each call must receive the context ID, not the raw image list
        for video_call in mock_api.call_args_list:
            args, kwargs = video_call
            # ctx_id must appear in the call; raw image paths must NOT
            all_args = list(args) + list(kwargs.values())
            assert ctx_id in all_args
            for img in images:
                assert img not in all_args

    def test_reusing_context_does_not_call_create_cache_again(
        self, tmp_path: Path
    ) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/ctx-once")
        mock_resp = _make_video_response('{"ok": true}')

        with patch.object(helper, "_create_cache", return_value=mock_cache) as mock_create:
            ctx_id = helper.create_cached_context(images)
            # Reuse same context for multiple analyses
            with patch.object(helper, "_call_video_api", return_value=mock_resp):
                helper.analyse_video(video, ctx_id, "prompt 1", {})
                helper.analyse_video(video, ctx_id, "prompt 2", {})

        # create_cache only called once
        assert mock_create.call_count == 1


# ---------------------------------------------------------------------------
# Release cached context
# ---------------------------------------------------------------------------


class TestReleaseCachedContext:
    def test_release_calls_delete_api(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/ctx-to-delete")

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_delete_cache") as mock_delete:
            helper.release_context(ctx_id)

        mock_delete.assert_called_once_with(ctx_id)

    def test_active_context_count_decrements_after_release(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        mock_cache = _make_cache_response("cached_contexts/ctx-1")

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        assert helper.active_context_count == 1

        with patch.object(helper, "_delete_cache"):
            helper.release_context(ctx_id)

        assert helper.active_context_count == 0

    def test_release_all_releases_every_active_context(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        mock_cache_a = _make_cache_response("cached_contexts/ctx-a")
        mock_cache_b = _make_cache_response("cached_contexts/ctx-b")

        with patch.object(helper, "_create_cache", side_effect=[mock_cache_a, mock_cache_b]):
            ctx_a = helper.create_cached_context(images)
            ctx_b = helper.create_cached_context(images)

        with patch.object(helper, "_delete_cache") as mock_delete:
            helper.release_all_contexts()

        assert mock_delete.call_count == 2
        assert helper.active_context_count == 0


# ---------------------------------------------------------------------------
# Token usage and cost tracking
# ---------------------------------------------------------------------------


class TestVideoTokenTracking:
    def test_tracks_input_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response()
        mock_resp = _make_video_response('{"x": 1}', input_tokens=5000, output_tokens=200)

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, ctx_id, "prompt", {})

        assert helper.total_input_tokens == 5000

    def test_multiple_calls_accumulate_tokens(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response()
        mock_resp = _make_video_response('{"x": 1}', input_tokens=3000, output_tokens=100)

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, ctx_id, "p1", {})
            helper.analyse_video(video, ctx_id, "p2", {})

        assert helper.total_input_tokens == 6000
        assert helper.total_output_tokens == 200

    def test_cost_summary_includes_video_calls(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response()
        mock_resp = _make_video_response('{"x": 1}', input_tokens=1000, output_tokens=50)

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", return_value=mock_resp):
            helper.analyse_video(video, ctx_id, "prompt", {})

        summary = helper.cost_summary()
        assert "gemini_video_calls" in summary
        assert summary["gemini_video_calls"] == 1
        assert "estimated_cost_usd" in summary


# ---------------------------------------------------------------------------
# Graceful failure
# ---------------------------------------------------------------------------


class TestVideoGracefulFailure:
    def test_api_error_returns_failure_result(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response()

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", side_effect=Exception("API error")):
            result = helper.analyse_video(video, ctx_id, "prompt", {})

        assert result.success is False
        assert result.reason is not None

    def test_api_error_does_not_raise(self, tmp_path: Path) -> None:
        helper = _make_helper()
        images = _make_image_paths(tmp_path)
        video = _make_video_path(tmp_path)
        mock_cache = _make_cache_response()

        with patch.object(helper, "_create_cache", return_value=mock_cache):
            ctx_id = helper.create_cached_context(images)

        with patch.object(helper, "_call_video_api", side_effect=RuntimeError("network down")):
            result = helper.analyse_video(video, ctx_id, "prompt", {})

        assert isinstance(result, VideoResult)
