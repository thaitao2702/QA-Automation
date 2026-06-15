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
CLAUDE_AI_FPS              base frames/second to sample from video (default 1; auto-reduced
                           for long clips to stay within CLAUDE_AI_MAX_FRAMES)
CLAUDE_AI_MAX_FRAMES       max frames sent per video assertion (default 95; keeps one
                           request under the API's 100-image-per-request cap)
CLAUDE_CLI_PATH            path/name of the claude executable (default "claude")
CLAUDE_MODEL               model passed as --model (default "sonnet")
CLAUDE_CLI_TIMEOUT         per-call subprocess timeout, seconds (default 600)

Cost
----
The provider uses local Claude Code auth (no API key). As with the local provider,
``cost_summary`` reports the gemini-compatible keys with ``estimated_cost_usd == 0.0``
and zero tokens, tracking only the call count. Any usage the CLI reports is ignored
by design.
"""

from __future__ import annotations

import base64
import json
import logging
import math
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
_DEFAULT_FPS = 1.0
# Frame budget for one video assertion. The hard API cap is 100 images per request
# (200k-context models like Sonnet); we default just below it to leave headroom for
# the reference-asset images, which share the request and also count toward the cap.
# At the base 1 fps this keeps ~95s clips whole; longer clips step the fps down.
_DEFAULT_MAX_FRAMES = 95
# Hard per-request image cap (Anthropic Messages API, 200k-context models). Frames
# AND reference images must together stay under this in one inline assertion request.
_HARD_IMAGE_CAP = 100
# Token budget for one inline video assertion request. Image COUNT alone is not enough:
# a long clip at 1 fps can exceed the model's usable context window even under the
# 100-image cap (observed "Prompt is too long" around ~85 images). Frames are therefore
# also capped by estimated tokens (derived from the source frame size).
_DEFAULT_TOKEN_BUDGET = 48000
_DEFAULT_CLI = "claude"
_DEFAULT_MODEL = "sonnet"
# Per-call CLI timeout. Generous because a video assertion inline-prefills up to
# ~95 frames in one pass (measured ~237s for 86 frames) — see CLAUDE_CLI_TIMEOUT.
_DEFAULT_TIMEOUT = 600

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
    # Clear stale frames so a re-sample at a lower fps doesn't leave higher-numbered
    # frames behind (which would inflate the count and the inline image set).
    for _old in out_dir.glob("frame-*.jpg"):
        try:
            _old.unlink()
        except OSError:
            pass
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


def _probe_duration(video_path: Path) -> float | None:
    """Return the clip duration in seconds via ffprobe, or None if unavailable."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe unavailable (%s); using base sampling fps", exc)
        return None
    if out.returncode != 0:
        logger.warning("ffprobe failed: %s", (out.stderr or "")[-200:])
        return None
    try:
        return float((out.stdout or "").strip())
    except ValueError:
        return None


def _probe_dimensions(video_path: Path) -> tuple[int, int] | None:
    """Return (width, height) of the video's first video stream via ffprobe, or None."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(video_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        w, h = (out.stdout or "").strip().split("x")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return None


def _adaptive_fps(duration_s: float | None, base_fps: float, max_frames: int) -> float:
    """Pick a sampling fps so the projected frame count fits within *max_frames*.

    Short clips keep *base_fps*. For longer clips the sampling interval steps up in
    whole seconds (base 1 fps -> 1 frame / 2s -> 1 / 3s ...) until the count fits, so a
    single assertion request stays under the API's per-request image cap. The frame
    count is derived from the probed duration BEFORE any frames are written to disk.
    """
    if not duration_s or duration_s <= 0 or max_frames <= 0:
        return base_fps
    if duration_s * base_fps <= max_frames:
        return base_fps
    interval = math.ceil(duration_s / max_frames)  # whole seconds per frame
    return 1.0 / interval


def _media_type(path: Path) -> str:
    """Map an image file extension to its API media type (default jpeg)."""
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")


def _image_content_blocks(
    image_paths: list[Path], captions: list[str | None] | None = None
) -> list[dict[str, Any]]:
    """Build message content blocks with each image PRECEDED by its caption.

    Interleaving a short description immediately before each base64 image gives the
    model explicit context for that specific picture ("this is the Coin symbol",
    "this is gameplay frame 7") instead of leaving it to guess from position — which
    materially improves recognition when many reference images are attached.
    """
    blocks: list[dict[str, Any]] = []
    for i, p in enumerate(image_paths):
        cap = captions[i] if captions and i < len(captions) else None
        if cap:
            blocks.append({"type": "text", "text": cap})
        b64 = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": _media_type(Path(p)), "data": b64},
        })
    return blocks


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
        # Max frames sent in one video assertion — long clips have their sampling fps
        # reduced adaptively to honor this so a single request stays under the API's
        # per-request image cap (see _adaptive_fps).
        self._max_frames = int(self._env_float("CLAUDE_AI_MAX_FRAMES", _DEFAULT_MAX_FRAMES))
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
        """Resolve the claude executable (PATH, then the native ~/.local/bin install).

        Tries the configured name on PATH (plus ``.cmd`` on Windows), then the native
        installer location ``~/.local/bin/claude(.exe)``. The latter matters because
        Claude Code migrating from the npm global to the native install removes the
        old ``claude.CMD`` PATH shim, which otherwise makes every spawn fail with
        ``[WinError 2] file not found``.
        """
        for candidate in (self._cli_path, f"{self._cli_path}.cmd"):
            found = shutil.which(candidate)
            if found:
                return found
        # Native-install fallback (only when the user hasn't pinned a custom path).
        if self._cli_path in ("claude", "claude.exe"):
            for native in (
                Path.home() / ".local" / "bin" / "claude.exe",
                Path.home() / ".local" / "bin" / "claude",
            ):
                if native.exists():
                    return str(native)
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
        full_prompt = _build_schema_prompt(prompt, output_schema, allow_reasoning=True)
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

    def _run_agent_inline(
        self,
        work_dir: Path,
        prompt: str,
        output_schema: dict[str, Any],
        image_paths: list[Path],
        captions: list[str | None] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Run a one-shot Claude call with images attached INLINE (base64), no Read tool.

        Sends a single ``stream-json`` user message whose content is the text prompt
        followed by one image block per path, in order, then reads events until the
        ``result``. One model pass over all images — far faster than the agent opening
        each file with the Read tool. The caller must keep the image count under the
        API's per-request cap (the adaptive sampler guarantees this). Raises on
        timeout / CLI error (callers wrap into a failure result).
        """
        full_prompt = _build_schema_prompt(prompt, output_schema, allow_reasoning=True)
        cli = self._resolve_cli()
        cmd = [
            cli, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self._model:
            cmd.extend(["--model", self._model])

        content: list[dict[str, Any]] = [{"type": "text", "text": full_prompt}]
        content.extend(_image_content_blocks(image_paths, captions))
        message = json.dumps({"type": "user", "message": {"role": "user", "content": content}})

        # Single message then EOF (stdin closed by communicate) → the print-mode process
        # emits its result and exits; communicate drains both pipes with a real timeout.
        proc = subprocess.Popen(
            cmd, cwd=str(work_dir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            stdout, _stderr = proc.communicate(input=message + "\n", timeout=self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(f"claude inline CLI timed out after {self._timeout}s")

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                if ev.get("is_error"):
                    raise RuntimeError(f"claude CLI reported an error: {ev.get('result') or ev}")
                raw_text = str(ev.get("result", ""))
                return _extract_json(raw_text), raw_text
        return None, (stdout or "")[:500]


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

        # Reference assets are attached inline alongside the frames and also count
        # toward the per-request image cap, so resolve them first and reserve room for
        # them when budgeting frames (the full symbol pack can be ~20+ images).
        ref_items: list[tuple[Path, str]] = []
        if self._reference is not None and not getattr(self._reference, "is_empty", True):
            ref_items = [
                (Path(img), label)
                for img, label in getattr(self._reference, "items", [])
                if Path(img).exists()
            ]
        ref_paths = [img for img, _label in ref_items]
        n_refs = len(ref_paths)
        # Frame budget bounded by BOTH the per-request image cap AND a token budget: a
        # long clip at 1 fps can blow the model's context window even under 100 images
        # ("Prompt is too long"), so also cap frames by estimated tokens.
        dims = _probe_dimensions(video_path)
        tokens_per_frame = (dims[0] * dims[1]) // 750 if dims else 650
        token_budget = int(self._env_float("CLAUDE_AI_TOKEN_BUDGET", _DEFAULT_TOKEN_BUDGET))
        reserve = n_refs * 500 + 4000  # reference images + the text prompt/captions
        frames_by_tokens = max(1, (token_budget - reserve) // max(tokens_per_frame, 1))
        frame_budget = max(1, min(self._max_frames, _HARD_IMAGE_CAP - n_refs - 2, frames_by_tokens))

        # Dynamically pick the sampling fps from the clip length so frames + references
        # fit both budgets. Short clips keep the base fps; long clips step the fps down.
        duration = _probe_duration(video_path)
        fps = _adaptive_fps(duration, self._fps, frame_budget)
        logger.info(
            "Video sampling: duration=%ss base=%sfps -> %sfps (budget=%s frames; "
            "%d refs, ~%d tok/frame, token_budget=%d)",
            round(duration, 1) if duration else "?", self._fps, round(fps, 3),
            frame_budget, n_refs, tokens_per_frame, token_budget,
        )

        if not _sample_frames(video_path, work_dir, fps):
            return VideoResult(
                success=False,
                data=None,
                reason=f"ffmpeg frame sampling failed (fps={fps})",
                raw_text=None,
            )

        # Attach the frames + reference assets INLINE as image blocks — one model pass,
        # no per-frame Read round-trips. Order: reference assets first (so the model
        # learns each symbol), then frames in chronological order.
        frame_paths = sorted(work_dir.glob("frame-*.jpg"))
        n_frames = len(frame_paths)
        # Caption each image: reference descriptions first, then chronological frame indices.
        captions = (
            [f"This image shows {label}" for _img, label in ref_items]
            + [f"This is gameplay frame {i + 1}/{n_frames} (chronological order)" for i in range(n_frames)]
        )
        agent_prompt = self._build_inline_video_prompt(prompt, ref_items, n_frames)
        logger.info(
            "Video assert (inline): %d frame(s) + %d reference image(s) -> %d total images",
            n_frames, len(ref_paths), n_frames + len(ref_paths),
        )

        try:
            data, raw_text = self._run_agent_inline(
                work_dir, agent_prompt, output_schema, ref_paths + frame_paths, captions
            )
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

    def _build_inline_video_prompt(
        self, prompt: str, ref_items: list[tuple[Path, str]], n_frames: int
    ) -> str:
        """Prompt for the inline path: describe the attached images, then the task.

        Image order matches what ``analyse_video`` attaches — reference assets first
        (so the model learns what each game symbol looks like), then the gameplay
        frames in chronological order.
        """
        lines = [
            "You are analysing a short gameplay video supplied as still frames attached to "
            "this message (no files to open). Each attached image is IMMEDIATELY PRECEDED by "
            "a line saying what it is."
        ]
        if ref_items:
            lines.append(
                f"The first {len(ref_items)} are REFERENCE images showing how each game symbol / "
                "UI element is SUPPOSED to look — treat them as the ground-truth expected appearance "
                "for recognising and judging those elements in the frames; do not report on the "
                "reference images themselves."
            )
        lines.append(
            f"The remaining {n_frames} are the gameplay VIDEO FRAMES in CHRONOLOGICAL ORDER "
            f"(earliest first). Reason about what changes from frame to frame."
        )
        lines.append("")
        lines.append(f"Task:\n{prompt}")
        return "\n".join(lines)

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
