"""
Claude AI analysis helpers — agentic, frame-sampled alternative to Gemini.

Drop-in replacements for ``GeminiVideoHelper`` / ``GeminiImageHelper`` that analyse
media with a **Claude Code CLI sub-agent** instead of a single multimodal API call.
They mirror the Gemini helper interfaces exactly (same method signatures, same
``VideoResult`` / ``GeminiResult`` return types, same ``cost_summary`` keys) so
``VideoAiProcessor`` and ``StepExecutor`` work unchanged.

How it works
------------
For each assertion we:

  1. (video only) sample the trimmed clip into JPEG frames with ffmpeg at
     ``CLAUDE_AI_FPS`` frames/second into a per-call work dir
     (``{stem}-claude/frame-0001.jpg`` …).
  2. write an ``ASSETS.md`` into the work dir listing each reference symbol/UI
     element with its description and the absolute path to its ground-truth image.
  3. spawn ``claude -p "<prompt>" --output-format json`` with the work dir as cwd
     and read-only tools (``Read``, ``Glob``) pre-approved. The prompt tells the
     sub-agent to read ``ASSETS.md`` (and its reference images) first, then read the
     frames / screenshot in order, then emit ONLY a JSON object matching the schema.
  4. parse the CLI's JSON envelope, pull the agent's final ``result`` text, and
     extract the verdict object from it.

Configuration (environment)
---------------------------
AI_PROVIDER=claude         select this provider
CLAUDE_AI_FPS              frames/second to sample from video (default 2)
CLAUDE_CLI_PATH            path/name of the claude executable (default "claude")
CLAUDE_MODEL               model passed as --model (default "sonnet")
CLAUDE_CLI_TIMEOUT         per-call subprocess timeout, seconds (default 300)

Cost
----
The provider uses local Claude Code auth (no API key). As with the local provider,
``cost_summary`` reports the gemini-compatible keys with ``estimated_cost_usd == 0.0``
and zero tokens, tracking only the call count. Any usage the CLI reports is ignored
by design.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from framework.helpers.gemini import GeminiResult
from framework.helpers.gemini_video import VideoResult

# Reuse the JSON-only schema-hint builder and the result parser (single source of
# truth shared with the local provider).
from framework.helpers.local_ai import _build_schema_prompt, _extract_json

logger = logging.getLogger(__name__)

# Defaults (overridable via env — see module docstring).
_DEFAULT_FPS = 2.0
_DEFAULT_CLI = "claude"
_DEFAULT_MODEL = "sonnet"
_DEFAULT_TIMEOUT = 300

# ffmpeg frame-sampling timeout (seconds).
_FFMPEG_TIMEOUT = 120


# ---------------------------------------------------------------------------
# ffmpeg frame sampling
# ---------------------------------------------------------------------------


def _sample_frames(video_path: Path, out_dir: Path, fps: float) -> bool:
    """
    Sample *video_path* into ``frame-%04d.jpg`` images in *out_dir* at *fps*.

    Mirrors ``video_ai_processor._trim_video``'s ffmpeg subprocess + error
    handling. Returns True only if ffmpeg succeeds AND at least one frame was
    written; False (logged) on missing ffmpeg, timeout, or non-zero exit.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "frame-%04d.jpg")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-qscale:v", "3",
        pattern,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT,
        )
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found — cannot sample frames for the claude provider. "
            "Install from https://ffmpeg.org/download.html and ensure it is on PATH."
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out sampling frames from %s", video_path)
        return False

    if result.returncode != 0:
        logger.error(
            "ffmpeg frame sampling failed (exit %d): %s",
            result.returncode,
            (result.stderr or "")[-500:],
        )
        return False

    produced = any(out_dir.glob("frame-*.jpg"))
    if not produced:
        logger.error("ffmpeg produced no frames from %s (fps=%s)", video_path, fps)
    return produced


# ---------------------------------------------------------------------------
# Shared CLI client
# ---------------------------------------------------------------------------


class _ClaudeCliClient:
    """Shared Claude Code CLI invocation + reference-asset manifest writing."""

    def __init__(self, reference: Any | None = None) -> None:
        self._reference = reference
        self._call_count = 0

        # Resolve config from the environment once.
        self._fps = self._env_float("CLAUDE_AI_FPS", _DEFAULT_FPS)
        self._cli_path = os.environ.get("CLAUDE_CLI_PATH", "").strip() or _DEFAULT_CLI
        # Default to Sonnet — fast/cheap enough for frame reading; the CLI would
        # otherwise inherit its own (pricier) default model. Override via CLAUDE_MODEL.
        self._model = os.environ.get("CLAUDE_MODEL", "").strip() or _DEFAULT_MODEL
        self._timeout = int(self._env_float("CLAUDE_CLI_TIMEOUT", _DEFAULT_TIMEOUT))

    @property
    def call_count(self) -> int:
        return self._call_count

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning("Ignoring non-numeric %s=%r; using %s", name, raw, default)
            return default

    # -- reference manifest -------------------------------------------------

    def _write_assets_file(self, work_dir: Path) -> Path | None:
        """
        Write ``ASSETS.md`` describing each reference asset + its image path.

        The sub-agent reads this first to learn the canonical appearance of every
        symbol/UI element before judging the frames. Returns the path, or None when
        there is no reference pack.
        """
        ref = self._reference
        if ref is None or getattr(ref, "is_empty", True):
            return None

        work_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Reference assets",
            "",
            getattr(ref, "header", "Ground-truth game symbols and UI elements."),
            "",
            "Read each image below to learn what the asset looks like before "
            "judging the frames/screenshot. These are reference context only — do "
            "not describe or judge the reference images themselves.",
            "",
        ]
        for img_path, label in getattr(ref, "items", []):
            lines.append(f"- {label}")
            lines.append(f"  image: {Path(img_path).resolve()}")
        lines.append("")

        assets_path = work_dir / "ASSETS.md"
        assets_path.write_text("\n".join(lines), encoding="utf-8")
        return assets_path

    # -- CLI invocation -----------------------------------------------------

    def _resolve_cli(self) -> str | None:
        """Resolve the claude executable (also try ``.cmd`` on Windows)."""
        for candidate in (self._cli_path, f"{self._cli_path}.cmd"):
            found = shutil.which(candidate)
            if found:
                return found
        # Fall back to the raw name and let subprocess raise FileNotFoundError,
        # which the caller turns into a failure result.
        return self._cli_path

    def _run_agent(
        self,
        work_dir: Path,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """
        Run the Claude Code CLI sub-agent in *work_dir* and parse its verdict.

        Returns ``(data, raw_text)`` where *data* is the extracted JSON object
        (or None if extraction failed) and *raw_text* is the agent's final reply.
        Raises on transport/subprocess errors — callers wrap.
        """
        full_prompt = _build_schema_prompt(prompt, output_schema)
        cli = self._resolve_cli()
        # Pass the prompt via STDIN, not as a -p argument. On Windows the CLI is a
        # `claude.cmd` batch wrapper, and a long/multi-line -p value mangles the
        # following flags (e.g. --output-format is dropped, so the envelope is lost).
        # Keeping only stable flags on the command line and feeding the prompt on
        # stdin is robust and also avoids command-line length limits.
        cmd = [
            cli,
            "-p",
            "--output-format", "json",
            "--allowedTools", "Read", "Glob",
        ]
        if self._model:
            cmd.extend(["--model", self._model])

        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )

        # The CLI prints a JSON envelope on stdout with --output-format json.
        try:
            envelope = json.loads((proc.stdout or "").strip())
        except json.JSONDecodeError:
            # Non-JSON stdout — surface stderr/stdout for debugging.
            raw = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"claude CLI exited {proc.returncode}: {(proc.stderr or '')[-300:]}"
                )
            return None, raw

        if isinstance(envelope, dict) and envelope.get("is_error"):
            raise RuntimeError(
                f"claude CLI reported an error: {envelope.get('result') or envelope}"
            )

        raw_text = ""
        if isinstance(envelope, dict):
            raw_text = str(envelope.get("result", ""))
        parsed = _extract_json(raw_text)
        return parsed, raw_text


# ---------------------------------------------------------------------------
# Video helper
# ---------------------------------------------------------------------------


class ClaudeVideoHelper(_ClaudeCliClient):
    """
    Video analysis via a frame-sampling Claude Code CLI sub-agent.

    Interface-compatible with ``GeminiVideoHelper`` for use by ``VideoAiProcessor``.
    """

    def __init__(self, reference: Any | None = None) -> None:
        super().__init__(reference)

    # -- Context management (no-ops, kept for API compatibility) ------------

    def create_cached_context(self, image_paths: list[Path]) -> str:
        """No-op. Reference assets are written per-call into ASSETS.md."""
        return "claude"

    def release_context(self, context_id: str) -> None:
        """No-op."""

    def release_all_contexts(self) -> None:
        """No-op."""

    @property
    def active_context_count(self) -> int:
        return 0

    # -- Analysis -----------------------------------------------------------

    def analyse_video(
        self,
        video_path: Path,
        context_id: str | None,
        prompt: str,
        output_schema: dict[str, Any],
        ref_images: list[Path] | None = None,
    ) -> VideoResult:
        """Sample *video_path* into frames and judge them with a Claude sub-agent."""
        self._call_count += 1

        work_dir = video_path.parent / f"{video_path.stem}-claude"

        if not _sample_frames(video_path, work_dir, self._fps):
            return VideoResult(
                success=False,
                data=None,
                reason=f"ffmpeg frame sampling failed (fps={self._fps})",
                raw_text=None,
            )

        self._write_assets_file(work_dir)
        agent_prompt = self._build_video_prompt(prompt)

        try:
            data, raw_text = self._run_agent(work_dir, agent_prompt, output_schema)
        except Exception as exc:  # noqa: BLE001 — resilience contract: never raise
            logger.warning("Claude video analysis failed: %s", exc)
            return VideoResult(
                success=False,
                data=None,
                reason=f"Claude CLI error: {exc}",
                raw_text=None,
            )

        if data is None:
            return VideoResult(
                success=False,
                data=None,
                reason=f"Claude response had no parseable JSON. Raw: {raw_text[:200]}",
                raw_text=raw_text,
            )

        return VideoResult(
            success=True,
            data=data,
            reason=None,
            input_tokens=0,
            output_tokens=0,
            raw_text=raw_text,
        )

    def _build_video_prompt(self, prompt: str) -> str:
        """Prepend agent instructions (read assets, then frames in order) to *prompt*."""
        has_assets = self._reference is not None and not getattr(
            self._reference, "is_empty", True
        )
        steps = []
        if has_assets:
            steps.append(
                "1. Read `ASSETS.md` in this directory and use the Read tool on each "
                "reference image it lists, to learn the canonical appearance of each "
                "game symbol and UI element."
            )
        steps.append(
            f"{len(steps) + 1}. List the `frame-*.jpg` files (e.g. with Glob) and Read "
            f"them in NUMERICAL ORDER. They are sampled at {self._fps} fps from a "
            "gameplay video; `frame-0001.jpg` is the earliest moment and the frames are "
            "chronological. Reason about what changes across the sequence."
        )
        steps.append(f"{len(steps) + 1}. Then answer the task below.")
        instructions = (
            "You are analysing a short gameplay video that has been sampled into image "
            "frames in your current directory.\n\n" + "\n".join(steps)
        )
        return f"{instructions}\n\nTask:\n{prompt}"

    # -- Cost ---------------------------------------------------------------

    def cost_summary(self) -> dict[str, Any]:
        """Gemini-compatible cost summary (zero cost — local Claude Code auth)."""
        return {
            "model": "claude",
            "resolution": "n/a",
            "gemini_video_calls": self._call_count,
            "gemini_input_tokens": 0,
            "gemini_output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }


# ---------------------------------------------------------------------------
# Image helper
# ---------------------------------------------------------------------------


class ClaudeImageHelper(_ClaudeCliClient):
    """
    Image analysis via a Claude Code CLI sub-agent.

    Interface-compatible with ``GeminiImageHelper`` for use by ``StepExecutor`` and
    the pending-image reprocessing path in ``SuiteRunner``.
    """

    def __init__(self, reference: Any | None = None) -> None:
        super().__init__(reference)

    def analyse_image(
        self,
        image_path: Path,
        prompt: str,
        output_schema: dict[str, Any],
        extra_images: list[Path] | None = None,
    ) -> GeminiResult:
        """Judge *image_path* (a screenshot) with a Claude sub-agent."""
        self._call_count += 1

        work_dir = image_path.parent / f"{image_path.stem}-claude"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Copy the screenshot (and any extras) into the work dir so the agent reads
        # them by simple relative names from its cwd.
        try:
            shutil.copyfile(image_path, work_dir / "screenshot.png")
            extra_names: list[str] = []
            for i, extra in enumerate(extra_images or [], start=1):
                name = f"screenshot-extra-{i}.png"
                shutil.copyfile(extra, work_dir / name)
                extra_names.append(name)
        except OSError as exc:
            logger.warning("Claude image analysis setup failed: %s", exc)
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Could not stage screenshot for Claude: {exc}",
                raw_text=None,
            )

        self._write_assets_file(work_dir)
        agent_prompt = self._build_image_prompt(prompt, extra_names)

        try:
            data, raw_text = self._run_agent(work_dir, agent_prompt, output_schema)
        except Exception as exc:  # noqa: BLE001 — resilience contract: never raise
            logger.warning("Claude image analysis failed: %s", exc)
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Claude CLI error: {exc}",
                raw_text=None,
            )

        if data is None:
            return GeminiResult(
                success=False,
                data=None,
                reason=f"Claude response had no parseable JSON. Raw: {raw_text[:200]}",
                raw_text=raw_text,
            )

        return GeminiResult(
            success=True,
            data=data,
            reason=None,
            input_tokens=0,
            output_tokens=0,
            raw_text=raw_text,
        )

    def _build_image_prompt(self, prompt: str, extra_names: list[str]) -> str:
        """Prepend agent instructions (read assets, then screenshot) to *prompt*."""
        has_assets = self._reference is not None and not getattr(
            self._reference, "is_empty", True
        )
        steps = []
        if has_assets:
            steps.append(
                "1. Read `ASSETS.md` in this directory and use the Read tool on each "
                "reference image it lists, to learn the canonical appearance of each "
                "game symbol and UI element."
            )
        shots = "`screenshot.png`"
        if extra_names:
            shots += " and " + ", ".join(f"`{n}`" for n in extra_names)
        steps.append(f"{len(steps) + 1}. Read {shots} (the screenshot under test).")
        steps.append(f"{len(steps) + 1}. Then answer the task below.")
        instructions = (
            "You are analysing a game screenshot in your current directory.\n\n"
            + "\n".join(steps)
        )
        return f"{instructions}\n\nTask:\n{prompt}"

    def cost_summary(self) -> dict[str, Any]:
        """Gemini-compatible cost summary (zero cost — local Claude Code auth)."""
        return {
            "model": "claude",
            "gemini_image_calls": self._call_count,
            "gemini_input_tokens": 0,
            "gemini_output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
