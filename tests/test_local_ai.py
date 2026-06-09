"""
Tests for framework/helpers/local_ai.py — self-hosted AI provider.

Acceptance criteria:
- analyse_video / analyse_image POST to the configured endpoint and return the
  Gemini-compatible result dataclasses (VideoResult / GeminiResult).
- The ``result`` text is parsed into structured ``data`` even when it contains
  reasoning prose and/or a markdown-fenced JSON block.
- Pure-text responses with no JSON yield success=False with a reason.
- HTTP / transport errors never raise — they return a failure result.
- cost_summary() returns gemini-compatible keys with a call count and zero cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from framework.helpers.gemini import GeminiResult
from framework.helpers.gemini_video import VideoResult
from framework.helpers.local_ai import (
    LocalImageHelper,
    LocalVideoHelper,
    _extract_json,
)

_ENDPOINT = "http://192.168.1.35:9000/analyze"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_video(tmp_path: Path) -> Path:
    p = tmp_path / "trimmed.webm"
    p.write_bytes(b"WEBM_STUB")
    return p


def _make_image(tmp_path: Path) -> Path:
    p = tmp_path / "shot.png"
    p.write_bytes(b"PNG_STUB")
    return p


def _make_post_response(result_text: str, status: int = 200) -> MagicMock:
    """Mock a requests.Response with a JSON {media_url, result} body."""
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = {
        "media_url": "http://192.168.1.35:9000/media/abc.webm",
        "result": result_text,
    }
    mock.raise_for_status.return_value = None
    return mock


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_raw_json_object(self) -> None:
        assert _extract_json('{"spins": 9, "won": false}') == {"spins": 9, "won": False}

    def test_pretty_printed_json(self) -> None:
        # The endpoint returns a clean, pretty-printed object with thinking off.
        text = (
            '{\n  "multiple_spins_occurred": true,\n'
            '  "win_displayed_after_spin": false,\n  "confidence": "high"\n}'
        )
        assert _extract_json(text) == {
            "multiple_spins_occurred": True,
            "win_displayed_after_spin": False,
            "confidence": "high",
        }

    def test_surrounding_whitespace_tolerated(self) -> None:
        assert _extract_json('  \n {"ok": true}\n  ') == {"ok": True}

    def test_nested_object(self) -> None:
        assert _extract_json('{"a": 1, "meta": {"b": 2}}') == {"a": 1, "meta": {"b": 2}}

    def test_prose_returns_none(self) -> None:
        assert _extract_json("The video shows a slot game with no wins.") is None

    def test_non_object_json_returns_none(self) -> None:
        # A bare array/number is valid JSON but not the dict we need.
        assert _extract_json("[1, 2, 3]") is None

    def test_empty_returns_none(self) -> None:
        assert _extract_json("") is None


# ---------------------------------------------------------------------------
# LocalVideoHelper
# ---------------------------------------------------------------------------


class TestLocalVideoHelper:
    def test_returns_video_result(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)
        resp = _make_post_response('{"spin_triggered": true, "confidence": "high"}')

        with patch("requests.post", return_value=resp) as mock_post:
            result = helper.analyse_video(
                video, "local", "Did spin trigger?", {"spin_triggered": {"type": "boolean"}}
            )

        assert isinstance(result, VideoResult)
        assert result.success is True
        assert result.data == {"spin_triggered": True, "confidence": "high"}
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        # Posted to the configured endpoint with file + prompt.
        args, kwargs = mock_post.call_args
        assert args[0] == _ENDPOINT
        assert "file" in kwargs["files"]
        assert "prompt" in kwargs["data"]

    def test_parses_pretty_printed_json(self, tmp_path: Path) -> None:
        # With reasoning disabled the endpoint returns a clean pretty-printed object.
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)
        clean = (
            '{\n  "multiple_spins_occurred": true,\n'
            '  "win_displayed_after_spin": false,\n  "confidence": "high"\n}'
        )
        resp = _make_post_response(clean)

        with patch("requests.post", return_value=resp):
            result = helper.analyse_video(
                video, "local", "Summarise spins",
                {"multiple_spins_occurred": {"type": "boolean"}},
            )

        assert result.success is True
        assert result.data == {
            "multiple_spins_occurred": True,
            "win_displayed_after_spin": False,
            "confidence": "high",
        }
        assert result.raw_text == clean

    def test_pure_text_no_json_fails(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)
        resp = _make_post_response("The video shows a slot game. No wins occurred.")

        with patch("requests.post", return_value=resp):
            result = helper.analyse_video(video, "local", "prompt", {"x": {"type": "boolean"}})

        assert result.success is False
        assert result.data is None
        assert "no parseable JSON" in (result.reason or "").lower() or "parseable" in (result.reason or "")

    def test_http_error_never_raises(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)

        with patch("requests.post", side_effect=ConnectionError("connection refused")):
            result = helper.analyse_video(video, "local", "prompt", {})

        assert result.success is False
        assert "endpoint error" in (result.reason or "").lower()

    def test_ref_images_dropped_not_fatal(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)
        ref = _make_image(tmp_path)
        resp = _make_post_response('{"ok": true}')

        with patch("requests.post", return_value=resp):
            result = helper.analyse_video(
                video, "local", "prompt", {"ok": {"type": "boolean"}}, ref_images=[ref]
            )

        assert result.success is True

    def test_context_methods_are_noops(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        assert helper.create_cached_context([_make_image(tmp_path)]) == "local"
        helper.release_context("local")  # no raise
        helper.release_all_contexts()  # no raise
        assert helper.active_context_count == 0

    def test_cost_summary_keys(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)
        resp = _make_post_response('{"ok": true}')
        with patch("requests.post", return_value=resp):
            helper.analyse_video(video, "local", "p", {"ok": {"type": "boolean"}})

        summary = helper.cost_summary()
        assert summary["model"] == "local"
        assert summary["gemini_video_calls"] == 1
        assert summary["gemini_input_tokens"] == 0
        assert summary["gemini_output_tokens"] == 0
        assert summary["estimated_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# LocalImageHelper
# ---------------------------------------------------------------------------


class TestLocalImageHelper:
    def test_returns_gemini_result(self, tmp_path: Path) -> None:
        helper = LocalImageHelper(_ENDPOINT)
        image = _make_image(tmp_path)
        resp = _make_post_response('{"balance": 100}')

        with patch("requests.post", return_value=resp) as mock_post:
            result = helper.analyse_image(image, "Read balance", {"balance": {"type": "integer"}})

        assert isinstance(result, GeminiResult)
        assert result.success is True
        assert result.data == {"balance": 100}
        # image/png mime sent.
        _, kwargs = mock_post.call_args
        assert kwargs["files"]["file"][2] == "image/png"

    def test_http_error_never_raises(self, tmp_path: Path) -> None:
        helper = LocalImageHelper(_ENDPOINT)
        image = _make_image(tmp_path)
        with patch("requests.post", side_effect=TimeoutError("timed out")):
            result = helper.analyse_image(image, "p", {})
        assert result.success is False

    def test_extra_images_dropped_not_fatal(self, tmp_path: Path) -> None:
        helper = LocalImageHelper(_ENDPOINT)
        image = _make_image(tmp_path)
        extra = _make_image(tmp_path)
        resp = _make_post_response('{"ok": true}')
        with patch("requests.post", return_value=resp):
            result = helper.analyse_image(
                image, "p", {"ok": {"type": "boolean"}}, extra_images=[extra]
            )
        assert result.success is True

    def test_cost_summary_keys(self, tmp_path: Path) -> None:
        helper = LocalImageHelper(_ENDPOINT)
        image = _make_image(tmp_path)
        resp = _make_post_response('{"ok": true}')
        with patch("requests.post", return_value=resp):
            helper.analyse_image(image, "p", {"ok": {"type": "boolean"}})

        summary = helper.cost_summary()
        assert summary["model"] == "local"
        assert summary["gemini_image_calls"] == 1
        assert summary["estimated_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Schema hint construction
# ---------------------------------------------------------------------------


class TestSchemaPrompt:
    def test_prompt_includes_schema_and_example(self, tmp_path: Path) -> None:
        helper = LocalVideoHelper(_ENDPOINT)
        video = _make_video(tmp_path)
        resp = _make_post_response('{"spins": 9}')
        schema = {"spins": {"type": "integer"}}

        with patch("requests.post", return_value=resp) as mock_post:
            helper.analyse_video(video, "local", "How many spins?", schema)

        sent_prompt = mock_post.call_args.kwargs["data"]["prompt"]
        assert "How many spins?" in sent_prompt
        assert json.dumps(schema) in sent_prompt
        assert "ONLY a single JSON object" in sent_prompt
