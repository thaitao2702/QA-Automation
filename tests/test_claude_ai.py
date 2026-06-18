"""
Tests for framework/helpers/claude_ai.py — Claude Code CLI AI provider.

Acceptance criteria:
- analyse_video samples frames (ffmpeg) then runs a `claude -p --output-format json`
  sub-agent and returns the Gemini-compatible result dataclasses.
- analyse_image stages the screenshot then runs the sub-agent the same way.
- The agent's final ``result`` text is parsed into structured ``data``.
- Frame-sampling failure, CLI errors, non-JSON output, and subprocess
  exceptions all return success=False (never raise).
- cost_summary() returns gemini-compatible keys with a call count and zero cost.
- The reference pack is written to ASSETS.md for the sub-agent to read.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.helpers.claude_ai import (
    ClaudeImageHelper,
    ClaudeVideoHelper,
    _sample_frames,
)
from framework.helpers.gemini import GeminiResult
from framework.helpers.gemini_video import VideoResult

_RUN = "framework.helpers.claude_ai.subprocess.run"
_SAMPLE = "framework.helpers.claude_ai._sample_frames"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _make_video(tmp_path: Path) -> Path:
    p = tmp_path / "trimmed.webm"
    p.write_bytes(b"WEBM_STUB")
    return p


def _make_image(tmp_path: Path) -> Path:
    p = tmp_path / "shot.png"
    p.write_bytes(b"PNG_STUB")
    return p


def _make_proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Mock a completed subprocess with a CLI JSON envelope on stdout."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.stderr = stderr
    mock.returncode = returncode
    return mock


def _envelope(result_text: str, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": result_text})


class _FakeRef:
    """Stand-in for SymbolReference with a couple of items."""

    def __init__(self, items: list[tuple[Path, str]]) -> None:
        self.header = "REFERENCE ASSETS for testing."
        self.items = items

    @property
    def is_empty(self) -> bool:
        return not self.items


def _work_dir_for(media: Path) -> Path:
    return media.parent / f"{media.stem}-claude"


# ---------------------------------------------------------------------------
# _sample_frames
# ---------------------------------------------------------------------------


class TestSampleFrames:
    def test_ffmpeg_missing_returns_false(self, tmp_path: Path) -> None:
        with patch(_RUN, side_effect=FileNotFoundError()):
            assert _sample_frames(_make_video(tmp_path), tmp_path / "out", 2.0) is False

    def test_nonzero_exit_returns_false(self, tmp_path: Path) -> None:
        with patch(_RUN, return_value=_make_proc("", returncode=1, stderr="boom")):
            assert _sample_frames(_make_video(tmp_path), tmp_path / "out", 2.0) is False

    def test_timeout_returns_false(self, tmp_path: Path) -> None:
        with patch(_RUN, side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)):
            assert _sample_frames(_make_video(tmp_path), tmp_path / "out", 2.0) is False

    def test_zero_exit_but_no_frames_returns_false(self, tmp_path: Path) -> None:
        with patch(_RUN, return_value=_make_proc("", returncode=0)):
            assert _sample_frames(_make_video(tmp_path), tmp_path / "out", 2.0) is False

    def test_success_when_frames_produced(self, tmp_path: Path) -> None:
        out = tmp_path / "out"

        def _fake_run(cmd, **kwargs):  # noqa: ANN001
            out.mkdir(parents=True, exist_ok=True)
            (out / "frame-0001.jpg").write_bytes(b"JPG")
            return _make_proc("", returncode=0)

        with patch(_RUN, side_effect=_fake_run):
            assert _sample_frames(_make_video(tmp_path), out, 2.0) is True


# ---------------------------------------------------------------------------
# ClaudeVideoHelper
# ---------------------------------------------------------------------------


class TestClaudeVideoHelper:
    def test_returns_video_result(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope('{"spin_triggered": true, "confidence": "high"}'))

        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc) as run:
            result = helper.analyse_video(
                video, "claude", "Did spin trigger?",
                {"spin_triggered": {"type": "boolean"}},
            )

        assert isinstance(result, VideoResult)
        assert result.success is True
        assert result.data == {"spin_triggered": True, "confidence": "high"}
        assert result.input_tokens == 0 and result.output_tokens == 0
        # Invoked the CLI in headless JSON mode, cwd=work dir, prompt via stdin.
        cmd = run.call_args.args[0]
        assert "-p" in cmd and "--output-format" in cmd and "json" in cmd
        assert run.call_args.kwargs["cwd"] == str(_work_dir_for(video))
        assert run.call_args.kwargs["input"]  # prompt fed on stdin, not as -p value

    def test_prompt_has_frame_and_schema_instructions(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        schema = {"ok": {"type": "boolean"}}
        proc = _make_proc(_envelope('{"ok": true}'))

        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc) as run:
            helper.analyse_video(video, "claude", "Judge it", schema)

        prompt = run.call_args.kwargs["input"]
        assert "frame-*.jpg" in prompt
        assert "NUMERICAL ORDER" in prompt
        assert "Judge it" in prompt
        assert json.dumps(schema) in prompt  # schema hint from _build_schema_prompt

    def test_writes_assets_file_with_reference(self, tmp_path: Path) -> None:
        ref_img = _make_image(tmp_path)
        helper = ClaudeVideoHelper(reference=_FakeRef([(ref_img, "Asset 'coin' — Coin")]))
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope('{"ok": true}'))

        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc) as run:
            helper.analyse_video(video, "claude", "p", {"ok": {"type": "boolean"}})

        assets = _work_dir_for(video) / "ASSETS.md"
        assert assets.exists()
        body = assets.read_text(encoding="utf-8")
        assert "Asset 'coin' — Coin" in body
        assert str(ref_img.resolve()) in body
        # The prompt tells the agent to read ASSETS.md first.
        prompt = run.call_args.kwargs["input"]
        assert "ASSETS.md" in prompt

    def test_frame_sampling_failure(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        with patch(_SAMPLE, return_value=False), patch(_RUN) as run:
            result = helper.analyse_video(video, "claude", "p", {})
        assert result.success is False
        assert "frame sampling" in (result.reason or "").lower()
        run.assert_not_called()  # never reached the CLI

    def test_non_json_stdout_fails(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc("not json at all", returncode=0)
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc):
            result = helper.analyse_video(video, "claude", "p", {"ok": {"type": "boolean"}})
        assert result.success is False
        assert "parseable" in (result.reason or "").lower()

    def test_agent_result_prose_fails(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope("The video shows a slot game with no wins."))
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc):
            result = helper.analyse_video(video, "claude", "p", {"ok": {"type": "boolean"}})
        assert result.success is False
        assert result.data is None

    def test_error_envelope_never_raises(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope("boom", is_error=True))
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc):
            result = helper.analyse_video(video, "claude", "p", {})
        assert result.success is False
        assert "claude cli error" in (result.reason or "").lower()

    def test_nonzero_exit_never_raises(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc("", returncode=2, stderr="crashed")
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc):
            result = helper.analyse_video(video, "claude", "p", {})
        assert result.success is False

    def test_subprocess_timeout_never_raises(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        with patch(_SAMPLE, return_value=True), patch(
            _RUN, side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)
        ):
            result = helper.analyse_video(video, "claude", "p", {})
        assert result.success is False

    def test_context_methods_are_noops(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        assert helper.create_cached_context([_make_image(tmp_path)]) == "claude"
        helper.release_context("claude")  # no raise
        helper.release_all_contexts()  # no raise
        assert helper.active_context_count == 0

    def test_cost_summary_keys(self, tmp_path: Path) -> None:
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope('{"ok": true}'))
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc):
            helper.analyse_video(video, "claude", "p", {"ok": {"type": "boolean"}})

        summary = helper.cost_summary()
        assert summary["model"] == "claude"
        assert summary["gemini_video_calls"] == 1
        assert summary["gemini_input_tokens"] == 0
        assert summary["gemini_output_tokens"] == 0
        assert summary["estimated_cost_usd"] == 0.0

    def test_exposes_model_and_cost_for_tokens(self) -> None:
        # VideoAiProcessor calls helper.model + helper.cost_for_tokens() directly on the
        # per-test path; the claude helper must expose them (GeminiVideoHelper-compatible)
        # or _process_test crashes after analyse_video and silently leaves the test
        # 'pending'. See: HANDOFF-video-ai-fold.md.
        helper = ClaudeVideoHelper()
        assert isinstance(helper.model, str) and helper.model
        assert helper.cost_for_tokens(1234, 56) == 0.0  # local auth is unmetered


# ---------------------------------------------------------------------------
# ClaudeImageHelper
# ---------------------------------------------------------------------------


class TestClaudeImageHelper:
    def test_returns_gemini_result_and_stages_screenshot(self, tmp_path: Path) -> None:
        helper = ClaudeImageHelper()
        image = _make_image(tmp_path)
        proc = _make_proc(_envelope('{"balance": 100}'))

        with patch(_RUN, return_value=proc) as run:
            result = helper.analyse_image(
                image, "Read balance", {"balance": {"type": "integer"}}
            )

        assert isinstance(result, GeminiResult)
        assert result.success is True
        assert result.data == {"balance": 100}
        # Screenshot copied into the work dir for the agent to Read.
        assert (_work_dir_for(image) / "screenshot.png").exists()
        prompt = run.call_args.kwargs["input"]
        assert "screenshot.png" in prompt

    def test_extra_images_staged(self, tmp_path: Path) -> None:
        helper = ClaudeImageHelper()
        image = _make_image(tmp_path)
        extra = tmp_path / "extra.png"
        extra.write_bytes(b"PNG2")
        proc = _make_proc(_envelope('{"ok": true}'))

        with patch(_RUN, return_value=proc):
            result = helper.analyse_image(
                image, "p", {"ok": {"type": "boolean"}}, extra_images=[extra]
            )

        assert result.success is True
        assert (_work_dir_for(image) / "screenshot-extra-1.png").exists()

    def test_exposes_model_and_cost_for_tokens(self) -> None:
        helper = ClaudeImageHelper()
        assert isinstance(helper.model, str) and helper.model
        assert helper.cost_for_tokens(1234, 56) == 0.0

    def test_cli_error_never_raises(self, tmp_path: Path) -> None:
        helper = ClaudeImageHelper()
        image = _make_image(tmp_path)
        with patch(_RUN, side_effect=OSError("spawn failed")):
            result = helper.analyse_image(image, "p", {})
        assert result.success is False

    def test_cost_summary_keys(self, tmp_path: Path) -> None:
        helper = ClaudeImageHelper()
        image = _make_image(tmp_path)
        proc = _make_proc(_envelope('{"ok": true}'))
        with patch(_RUN, return_value=proc):
            helper.analyse_image(image, "p", {"ok": {"type": "boolean"}})

        summary = helper.cost_summary()
        assert summary["model"] == "claude"
        assert summary["gemini_image_calls"] == 1
        assert summary["estimated_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Env configuration
# ---------------------------------------------------------------------------


class TestEnvConfig:
    def test_fps_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_AI_FPS", "5")
        helper = ClaudeVideoHelper()
        assert helper._fps == 5.0

    def test_bad_fps_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_AI_FPS", "notanumber")
        helper = ClaudeVideoHelper()
        assert helper._fps == 2.0

    def test_default_model_is_sonnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        assert ClaudeVideoHelper()._model == "sonnet"

    def test_default_model_passed_to_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope('{"ok": true}'))
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc) as run:
            helper.analyse_video(video, "claude", "p", {"ok": {"type": "boolean"}})
        cmd = run.call_args.args[0]
        assert "--model" in cmd and "sonnet" in cmd

    def test_model_flag_added(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-8")
        helper = ClaudeVideoHelper()
        video = _make_video(tmp_path)
        proc = _make_proc(_envelope('{"ok": true}'))
        with patch(_SAMPLE, return_value=True), patch(_RUN, return_value=proc) as run:
            helper.analyse_video(video, "claude", "p", {"ok": {"type": "boolean"}})
        cmd = run.call_args.args[0]
        assert "--model" in cmd and "claude-opus-4-8" in cmd
