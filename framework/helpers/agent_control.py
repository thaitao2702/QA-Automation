"""
Claude-driven, vision-based game control for canvas games (no DOM, no bridge).

Cocos/HTML5-canvas games render every button and label as pixels on a single
``<canvas>`` — there is nothing to select in the DOM, which is why the project
ships a JS test bridge (``window.__gameTestBridge``). This module is the
bridge-free alternative: it drives the game the way a human does, by *looking*
at the screen and *tapping* pixel coordinates.

Loop (perceive → decide → act):

    1. screenshot the canvas
    2. ask a Claude Code CLI sub-agent: "to advance <goal>, what ONE action?"
    3. perform it with Playwright (mouse click / key press / wait)
    4. repeat until the agent reports ``done`` or ``fail`` (or max_steps)

Game-agnostic by design: it knows nothing about a specific game's layout. The
only inputs are the natural-language ``goal`` and the on-screen pixels, so the
same controller drives any game — including ones with no test bridge at all.
Pairs with the bridge in a hybrid suite (bridge for result injection / exact
assertions, this for the clicking), but needs neither.

Provider: reuses the Claude Code CLI sub-agent pattern from ``claude_ai.py``
(local Claude Code auth, no API key). Each decision is one ``claude -p`` call
that Reads the latest screenshot and returns a JSON action.

Configuration (env):
    CLAUDE_MODEL / AGENT_MODEL   model passed to the CLI (default: sonnet)
    AGENT_MAX_STEPS              default per-goal action budget (default: 8)
    AGENT_SETTLE_MS              pause after each action for animations (default: 400)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from framework.helpers.claude_ai import _ClaudeCliClient, _image_content_blocks

# Shared prompt/JSON helpers (same ones claude_ai uses) so the timed CLI path
# produces an identical prompt and parses the envelope the same way.
from framework.helpers.local_ai import _build_schema_prompt, _extract_json

logger = logging.getLogger(__name__)

# Default canvas size (mirrors the suite runner's Playwright viewport). The real
# size is read from the page at runtime; this is only the fallback.
_DEFAULT_VIEWPORT = (531, 919)

# JSON action the decision sub-agent must return each turn.
_ACTION_SCHEMA: dict[str, Any] = {
    "action": {"type": "string", "enum": ["click", "press_key", "wait", "done", "fail"]},
    "x": {"type": "integer"},
    "y": {"type": "integer"},
    "key": {"type": "string"},
    "ms": {"type": "integer"},
    "reason": {"type": "string"},
}


@dataclass
class AgentStep:
    """One perceive→decide→act iteration, recorded for the report/transcript."""

    index: int
    action: str
    detail: str
    reason: str
    screenshot: str
    # Per-turn timing (epoch seconds for the 'when' events + derived ms durations).
    # None only if the turn aborted before timing could be recorded.
    timing: dict[str, Any] | None = None


@dataclass
class AgentResult:
    """Outcome of driving the game toward a single goal."""

    success: bool
    goal: str
    steps: list[AgentStep] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success": self.success,
            "error": self.error,
            "steps": [asdict(s) for s in self.steps],
        }


def _format_history(history: list[dict[str, Any]]) -> str:
    """Render prior actions as a compact list so the agent doesn't repeat itself."""
    if not history:
        return "  (none yet — this is the first action)"
    lines = []
    for i, h in enumerate(history):
        act = h.get("action", "?")
        bits = [f"{k}={h[k]}" for k in ("x", "y", "key", "ms") if h.get(k) is not None]
        arg = (" " + " ".join(bits)) if bits else ""
        reason = h.get("reason") or ""
        lines.append(f"  {i + 1}. {act}{arg} — {reason}")
    return "\n".join(lines)


def _reference_legend(reference: Any, inline: bool) -> str:
    """Short note about the reference asset images attached to a turn.

    Each reference image is itself preceded by a caption (see ``_image_content_blocks``),
    so this only flags that they are present and how to treat them — no per-asset list.
    Empty when there is no reference or the turn is not inline.
    """
    items = getattr(reference, "items", []) if reference is not None else []
    if not inline or not items:
        return ""
    return (
        f"\n\nThe {len(items)} image(s) attached after the current screen are REFERENCE images "
        "showing how each game symbol / UI element is SUPPOSED to look — each is preceded by a "
        "line describing what it is. Treat them as the ground-truth, expected appearance: use them "
        "to recognise those elements on the current screen and to base your judgment on whether "
        "what you see matches. Do not report on the reference images themselves."
    )


def _reference_items(reference: Any) -> list[tuple[Path, str]]:
    """The (image_path, label) reference items to attach inline (empty when none)."""
    return [(Path(p), label) for p, label in (getattr(reference, "items", []) if reference is not None else [])]


def _build_control_prompt(
    image_ref: str | None,
    goal: str,
    history: list[dict[str, Any]],
    viewport: tuple[int, int],
    inline: bool = False,
    follow_up: bool = False,
    reference: Any = None,
) -> str:
    """Build the decision prompt for the Claude CLI sub-agent.

    When ``inline`` is True the screenshot is attached to the message as an image
    block (no Read tool, no disk path) — one model pass instead of a tool
    round-trip. When False (one-shot fallback), ``image_ref`` is an absolute path
    the agent opens with the Read tool.

    When ``follow_up`` is True (a warm session's turns 2+), the full instructions,
    coordinate system, action schema, and text history are OMITTED — they were
    established on the first turn and the persistent conversation already holds
    them. Only the goal anchor + the new screenshot are sent, which cuts the
    per-turn input tokens sharply. (The first turn, all check: asserts, and the
    one-shot fallback — a fresh process with no memory — still get the full prompt.)
    """
    if follow_up:
        return (
            "Here is the CURRENT game screen (attached) after your last action. "
            "Continue toward the goal:\n"
            f"  GOAL: {goal}\n"
            "Choose the SINGLE next action and reply with the same JSON action object "
            "as before (set `reason` to what you see and why)."
        )

    vw, vh = viewport
    look = (
        "Study the CURRENT game screen shown in the attached screenshot. THEN choose "
        if inline
        else f"FIRST, use the Read tool to open the screenshot image at `{image_ref}` and study "
             "the CURRENT game screen. THEN choose "
    )
    return (
        f"You are an autonomous UI tester controlling a slot/casino game that is "
        f"rendered entirely on an HTML5 canvas of size {vw}x{vh} pixels (width x height). "
        f"There is no DOM and no accessibility tree — you interact ONLY by clicking pixel "
        f"coordinates or pressing keys, exactly like a human tapping a touch screen.\n\n"
        f"{look}the SINGLE next "
        f"action that best advances this goal:\n"
        f"  GOAL: {goal}\n\n"
        f"Coordinate system: origin (0,0) is the TOP-LEFT corner; x increases to the right up "
        f"to {vw}, y increases downward up to {vh}. Return the CENTRE of the control to tap.\n\n"
        f"Choose one value for `action`:\n"
        f"  - \"click\": tap the canvas at (x, y). Provide integer x and y.\n"
        f"  - \"press_key\": press a key. Provide `key` as a Playwright key name "
        f"(e.g. \"Space\", \"Escape\", \"Enter\").\n"
        f"  - \"wait\": the screen is still animating/loading and you want a fresh look "
        f"before acting. You will get a NEW screenshot IMMEDIATELY — no need to set `ms`, "
        f"because the time spent analysing this turn already lets the animation progress.\n"
        f"  - \"done\": the goal is ALREADY satisfied on this screen (no x/y needed).\n"
        f"  - \"fail\": the goal cannot be achieved (blocked, error popup, missing control); "
        f"explain in `reason`.\n\n"
        f"Rules:\n"
        f"  - Exactly ONE action per turn. You get a fresh screenshot after it is performed.\n"
        f"  - Use the fewest actions possible. If the goal already looks satisfied, return done.\n"
        f"  - Always set `reason` to a short note on what you see and why you chose this action.\n\n"
        f"Actions already performed this episode (most recent last):\n"
        f"{_format_history(history)}\n"
        + _reference_legend(reference, inline)
    )


class _AgentDecider(_ClaudeCliClient):
    """One-shot (cold) timed decision via the Claude Code CLI.

    Used as the fallback when no warm session is available. The warm path is
    ``ClaudeStreamSession`` (one persistent process reused across a test's turns).
    """

    def _run_cli_timed(
        self, work_dir: Path, prompt: str, output_schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """Run ``claude -p`` via Popen with explicit spawn/send/result timestamps.

        Mirrors ``_ClaudeCliClient._run_agent`` (same command + envelope parsing)
        but uses Popen so we can timestamp each phase. The prompt is small text
        (the screenshot is read from disk by the agent, not piped), so writing
        stdin then reading stdout cannot deadlock; stderr is sent to a temp file
        to remove the only other deadlock risk.
        """
        full_prompt = _build_schema_prompt(prompt, output_schema, allow_reasoning=True)
        cli = self._resolve_cli()
        cmd = [cli, "-p", "--output-format", "json", "--allowedTools", "Read", "Glob"]
        if self._model:
            cmd.extend(["--model", self._model])

        # One-shot spawn is always 'cold' (new process pays Node/CLI startup).
        timing: dict[str, Any] = {"cli_duration_ms": None, "cli_api_ms": None, "cold": True}
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errf:
            timing["t_spawn"] = time.time()
            proc = subprocess.Popen(
                cmd, cwd=str(work_dir),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
                text=True, encoding="utf-8",
            )
            stdout = ""
            try:
                proc.stdin.write(full_prompt)
                proc.stdin.flush()
                proc.stdin.close()
                timing["t_prompt_sent"] = time.time()
                stdout = proc.stdout.read()           # blocks until the process exits
                proc.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                timing["t_result"] = time.time()
                return None, "claude CLI timed out", timing
            finally:
                try:
                    proc.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
            timing["t_result"] = time.time()

        parsed, raw, dur_ms, api_ms = _parse_cli_envelope(stdout)
        timing["cli_duration_ms"] = dur_ms
        timing["cli_api_ms"] = api_ms
        return parsed, raw, timing


def _parse_cli_envelope(stdout: str) -> tuple[dict[str, Any] | None, str, int | None, int | None]:
    """Parse the ``--output-format json`` envelope → (action, raw, duration_ms, api_ms)."""
    try:
        env = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        return None, (stdout or "").strip(), None, None
    if not isinstance(env, dict):
        return None, str(env), None, None
    dur = env.get("duration_ms")
    api = env.get("duration_api_ms")
    if env.get("is_error"):
        return None, str(env.get("result") or env), dur, api
    raw = str(env.get("result", ""))
    return _extract_json(raw), raw, dur, api


class _StreamError(RuntimeError):
    """Raised when the persistent stream-json session fails (spawn/timeout/EOF/write)."""


class ClaudeStreamSession(_ClaudeCliClient):
    """One long-lived ``claude`` process reused across many prompts (warm session).

    Spawns ``claude -p --input-format stream-json --output-format stream-json`` ONCE
    and feeds it a JSON user-message per turn, reading events until that turn's
    ``result`` — so the per-turn process cold-start (~3–7s) is paid only once per
    session instead of once per turn. Intended to be owned by a per-test
    ``StepExecutor`` and shared by every ``do:`` and ``check:`` in that test.

    A background reader thread drains stdout into a queue so ``send`` can enforce a
    real timeout on the otherwise-blocking pipe read (Windows ``select`` can't poll
    pipes). ``send`` is synchronous/blocking — call it via ``asyncio.to_thread`` —
    and must be serialized (one turn at a time), which the single test executor
    guarantees.
    """

    # One process keeps conversation memory across turns, so AgentController may strip
    # the prompt on follow-up turns (see _decide). Contrast PrewarmStreamPool.
    stateless_turns = False

    def __init__(self, model: str | None = None) -> None:
        super().__init__()
        chosen = model or os.environ.get("AGENT_MODEL", "").strip()
        if chosen:
            self._model = chosen
        self._proc: subprocess.Popen | None = None
        self._errf: Any = None
        self._q: "queue.Queue[dict | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self.alive: bool = False
        self.t_spawn: float | None = None
        self.first_send_done: bool = False
        # Set once the reference asset images have been attached to this session's
        # conversation; they persist there, so they are sent on one turn only.
        self.refs_sent: bool = False

    def start(self, cwd: Path | str) -> None:
        """Spawn the persistent process. Raises ``_StreamError`` on failure."""
        cli = self._resolve_cli()
        # No tools: screenshots are sent inline as image blocks (see send), so the
        # agent never needs the Read tool — one model pass per turn, not a tool
        # round-trip.
        cmd = [
            cli, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self._model:
            cmd.extend(["--model", self._model])
        self._errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.t_spawn = time.time()
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(cwd),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._errf,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as exc:
            self.alive = False
            raise _StreamError(f"failed to spawn claude stream session: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.alive = True

    def _read_loop(self) -> None:
        """Drain stdout into the queue; push None on EOF."""
        try:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:  # noqa: BLE001 — reader is best-effort
            pass
        finally:
            self._q.put(None)  # EOF sentinel

    def send(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        image_paths: list[Path] | None = None,
        wrap_schema: bool = True,
        captions: list[str | None] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """Send one user turn and return ``(parsed | None, raw_text, timing)``.

        When ``image_paths`` is given, the screenshot(s) are attached inline as
        base64 image blocks (no Read tool) so the model answers in one pass.
        When ``wrap_schema`` is False the prompt is sent as-is (no JSON schema hint
        appended) — used for warm follow-up turns where the schema is already
        established in the conversation. Blocking — run via ``asyncio.to_thread``.
        Raises ``_StreamError`` (and marks the session dead) on write failure,
        per-turn timeout, or process exit.
        """
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise _StreamError("session not alive")
        full = _build_schema_prompt(prompt, output_schema, allow_reasoning=True) if wrap_schema else prompt
        if image_paths:
            content: Any = [{"type": "text", "text": full}] + _image_content_blocks(image_paths, captions)
        else:
            content = full
        message = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
        cold = not self.first_send_done
        timing: dict[str, Any] = {
            "cli_duration_ms": None, "cli_api_ms": None,
            "t_spawn": self.t_spawn if cold else None, "cold": cold,
        }
        try:
            self._proc.stdin.write(message + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.alive = False
            raise _StreamError(f"failed to write to claude session: {exc}") from exc
        timing["t_prompt_sent"] = time.time()

        deadline = time.time() + self._timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.alive = False
                raise _StreamError(f"no result within {self._timeout}s")
            try:
                ev = self._q.get(timeout=remaining)
            except queue.Empty:
                self.alive = False
                raise _StreamError(f"no result within {self._timeout}s")
            if ev is None:
                self.alive = False
                raise _StreamError("claude session stdout closed (process exited)")
            if ev.get("type") == "result":
                timing["t_result"] = time.time()
                self.first_send_done = True
                timing["cli_duration_ms"] = ev.get("duration_ms")
                timing["cli_api_ms"] = ev.get("duration_api_ms")
                if ev.get("is_error"):
                    return None, str(ev.get("result") or ev), timing
                raw = str(ev.get("result", ""))
                return _extract_json(raw), raw, timing

    def close(self) -> None:
        """Close stdin and reap the process (idempotent)."""
        proc, self._proc = self._proc, None
        self.alive = False
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._errf:
                self._errf.close()
        except Exception:  # noqa: BLE001
            pass


class PrewarmStreamPool:
    """Hide per-process cold start by background-pre-warming a replacement process.

    Holds one ACTIVE ``ClaudeStreamSession`` that serves turns and one STANDBY that
    is spawned ahead of time and left to boot. An idle ``claude`` process boots in
    the background and makes NO API call until it receives a message (verified
    empirically), so a warm spare costs nothing but the OS process — no extra model
    calls, no rate-limit hit. Once a standby has had long enough to finish booting
    (``AGENT_PREWARM_READY_MS``, default ~9.5s ≥ the observed ~5-9s cold start), the
    next ``send`` promotes it, retires the old active one (reaped off the hot path),
    and spawns a fresh standby.

    Two payoffs, both relying on every turn being a SELF-CONTAINED prompt (full
    instructions + short TEXT history + current screenshot — see
    ``AgentController._decide``), so a swap to a fresh process loses nothing:

      * Latency hiding — each replacement boots WHILE the active process serves real
        turns, so only the very first turn of a test pays a cold start.
      * Bounded context — the active process is swapped every ``ready_s / turn_s``
        turns (~2-3), so only a handful of screenshots ever accumulate in one
        process. That keeps per-turn decision latency flat instead of climbing the
        way a single long-lived session does as it re-uploads every prior image.

    Duck-types the subset of ``ClaudeStreamSession`` the callers use
    (``start`` / ``alive`` / ``send`` / ``close``), so it drops in transparently.
    """

    # Swaps processes between turns → no shared conversation memory, so every turn must
    # send the full self-contained prompt (AgentController checks this flag in _decide).
    stateless_turns = True

    def __init__(self, model: str | None = None) -> None:
        self._model = model or os.environ.get("AGENT_MODEL", "").strip() or None
        self._cwd: Path | None = None
        self._active: ClaudeStreamSession | None = None
        self._standby: ClaudeStreamSession | None = None
        self._standby_t: float = 0.0
        # How long after spawn a standby is assumed booted & ready to promote. Set
        # >= the observed cold start (~5-9s). Higher just lets the active serve a few
        # more turns (more accumulated images) before swapping.
        self._ready_s = _env_int("AGENT_PREWARM_READY_MS", 9500) / 1000.0
        # Reset to False on each process swap (a fresh process has none of the prior
        # conversation), so reference images get re-attached to the new active process.
        self.refs_sent: bool = False

    @property
    def alive(self) -> bool:
        return self._active is not None and self._active.alive

    def start(self, cwd: Path | str) -> None:
        """Spawn the first (active) process. It boots lazily on the first send."""
        self._cwd = Path(cwd)
        self._active = ClaudeStreamSession(model=self._model)
        self._active.start(self._cwd)

    def send(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        image_paths: list[Path] | None = None,
        wrap_schema: bool = True,
        captions: list[str | None] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """Promote a ready standby, ensure one is warming, then serve this turn."""
        if self._active is None:
            raise _StreamError("pool not started")
        self._rotate()
        try:
            return self._active.send(prompt, output_schema, image_paths, wrap_schema, captions)
        except _StreamError:
            # Active died mid-turn: if a standby exists, promote it and retry once.
            if self._standby is not None and self._standby.alive:
                self._promote()
                return self._active.send(prompt, output_schema, image_paths, wrap_schema, captions)
            raise

    def _rotate(self) -> None:
        """Promote the standby once it has booted; always keep one standby warming."""
        if (self._standby is not None and self._standby.alive
                and (time.time() - self._standby_t) >= self._ready_s):
            self._promote()
        if self._standby is None:
            self._spawn_standby()

    def _promote(self) -> None:
        """Swap active <- standby (booted in the background), reap the old, re-warm."""
        old = self._active
        self._active = self._standby
        self._standby = None
        if self._active is not None:
            # Booted in the background → cold start already paid; time its first real
            # send as warm (no boot block) rather than cold.
            self._active.first_send_done = True
        # New process has no prior conversation → references must be re-attached.
        self.refs_sent = False
        if old is not None:
            # Reap off the hot path so closing the old process never stalls this turn.
            threading.Thread(target=old.close, daemon=True).start()
        self._spawn_standby()

    def _spawn_standby(self) -> None:
        if self._cwd is None:
            return
        try:
            s = ClaudeStreamSession(model=self._model)
            s.start(self._cwd)
            self._standby = s
            self._standby_t = time.time()
        except _StreamError as exc:
            logger.warning("prewarm: standby spawn failed (%s); will retry next turn", exc)
            self._standby = None

    def close(self) -> None:
        """Close both the active and standby processes (idempotent)."""
        for s in (self._active, self._standby):
            if s is not None:
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
        self._active = None
        self._standby = None


# JSON verdict the assertion sub-agent must return.
_ASSERT_SCHEMA: dict[str, Any] = {
    "passed": {"type": "boolean"},
    "reason": {"type": "string"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}


def _build_assert_prompt(
    current: Path, check: str, ref: Path | None, inline: bool = False, reference: Any = None
) -> str:
    """Build the single-screen (or before/after) vision-assertion prompt.

    When ``inline`` is True the screenshot(s) are attached to the message as image
    block(s) (no Read tool); the first attached image is the CURRENT screen and the
    second (if any) is the EARLIER reference. When False (fallback), the images are
    opened from disk with the Read tool.
    """
    if inline:
        lines = [
            "You are a meticulous QA inspector. Judge whether ONE claim is TRUE of a game "
            "screen rendered entirely on an HTML5 canvas. Look carefully at the attached "
            "screenshot(s) before deciding.",
            "",
            f"CLAIM: {check}",
            "",
            "The FIRST attached image is the CURRENT screen."
            + (" The SECOND attached image is an EARLIER reference screen (the 'before' "
               "state, for before/after or 'higher/lower/same as before' claims)."
               if ref is not None else ""),
        ]
    else:
        lines = [
            "You are a meticulous QA inspector. Judge whether ONE claim is TRUE of a game "
            "screen rendered entirely on an HTML5 canvas. Use the Read tool to open the "
            "image(s) listed below and look carefully before deciding.",
            "",
            f"CLAIM: {check}",
            "",
            "Images to Read:",
            f"  - CURRENT screen: {current.resolve()}",
        ]
        if ref is not None:
            lines.append(
                f"  - EARLIER reference screen (the 'before' state, for before/after or "
                f"'higher/lower/same as before' claims): {ref.resolve()}"
            )
    lines += [
        "",
        "Judge ONLY what is visibly true in the CURRENT screenshot right now. Ignore any "
        "expectation, intent, or earlier actions in this conversation — do not assume the "
        "claim is true because of what you previously did; look at the pixels.",
        "Decide strictly: set passed=true ONLY if the claim is clearly and visibly "
        "satisfied. If it is false, ambiguous, or you cannot tell, set passed=false and "
        "describe what you actually see in `reason`. Set `confidence` to how certain you "
        "are (high/medium/low).",
    ]
    return "\n".join(lines) + _reference_legend(reference, inline)


class AgentVerifier(_ClaudeCliClient):
    """One-shot vision assertion via the Claude Code CLI: 'is this claim true of the screen?'

    Bridge-free verification counterpart to ``AgentController``. Reads the current
    screenshot (optionally plus a 'before' reference) and returns a structured
    verdict ``{passed, reason, confidence}``.
    """

    def __init__(
        self,
        model: str | None = None,
        session: "ClaudeStreamSession | None" = None,
        reference: Any = None,
    ) -> None:
        super().__init__()
        chosen = model or os.environ.get("AGENT_MODEL", "").strip()
        if chosen:
            self._model = chosen
        # Optional warm per-test session; falls back to one-shot when absent/dead.
        self._session = session
        # Optional scoped reference pack (asset images attached inline after the screens).
        self._reference = reference

    def verify(
        self, current: Path, check: str, ref: Path | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """Return ``(verdict | None, raw_text)``. Blocking — call via asyncio.to_thread."""
        sess = self._session
        if sess is not None and sess.alive:
            try:
                attach_refs = (
                    self._reference is not None
                    and not getattr(self._reference, "is_empty", True)
                    and not getattr(sess, "refs_sent", True)
                )
                ref_items = _reference_items(self._reference) if attach_refs else []
                images = [current] + ([ref] if ref is not None else []) + [p for p, _l in ref_items]
                captions = None
                if attach_refs:
                    captions = (
                        ["This is the CURRENT screen under test:"]
                        + (["This is the EARLIER 'before' screen (for before/after claims):"]
                           if ref is not None else [])
                        + [f"This image shows {label}" for _p, label in ref_items]
                    )
                    sess.refs_sent = True
                prompt = _build_assert_prompt(
                    current, check, ref, inline=True,
                    reference=self._reference if attach_refs else None,
                )
                parsed, raw, _timing = sess.send(
                    prompt, _ASSERT_SCHEMA, image_paths=images, captions=captions
                )
                return parsed, raw
            except _StreamError as exc:
                logger.warning("warm session failed in assert (%s); one-shot fallback", exc)
                try:
                    sess.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None
        # One-shot fallback: Read the image(s) from disk.
        prompt = _build_assert_prompt(current, check, ref, inline=False)
        return self._run_agent(current.parent, prompt, _ASSERT_SCHEMA)


class DecisionLog:
    """Append-only JSONL sink recording EVERY Claude agent decision in a test.

    One file per test (``decisions.jsonl`` in the test artifact dir) capturing both
    ``do:`` control turns and ``check:`` assert verdicts — including the raw model
    response — so there is a complete audit of what the Claude process decided and
    why. Each ``record`` is timestamped automatically.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def record(self, rec: dict[str, Any]) -> None:
        """Append one decision as a JSON line (ts/at prepended)."""
        now = time.time()
        line = {"ts": round(now, 3), "at": _hms(now), **rec}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to write decision log %s: %s", self._path, exc)


class AgentController:
    """Drive a Playwright page toward natural-language goals using vision only.

    Parameters
    ----------
    page:
        The active Playwright Page (the game canvas).
    work_dir:
        Directory for per-turn screenshots (also the sub-agent's cwd).
    viewport:
        (width, height) of the canvas. Defaults to the page's own viewport.
    max_steps:
        Default action budget per goal (override per ``run`` call).
    model:
        Optional CLI model override (else CLAUDE_MODEL/AGENT_MODEL/sonnet).
    settle_ms:
        Pause after each action so animations land before the next screenshot.
    """

    def __init__(
        self,
        page: Any,
        work_dir: Path | str,
        viewport: tuple[int, int] | None = None,
        max_steps: int | None = None,
        model: str | None = None,
        settle_ms: int | None = None,
        session: "ClaudeStreamSession | None" = None,
        decision_log: "DecisionLog | None" = None,
        reference: Any = None,
    ) -> None:
        self._page = page
        self._work_dir = Path(work_dir)
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._step_id = self._work_dir.name  # work_dir == test_dir/agent/<step-id>
        self._viewport = viewport or _DEFAULT_VIEWPORT
        self._max_steps = max_steps or int(_env_int("AGENT_MAX_STEPS", 8))
        self._settle_s = (settle_ms if settle_ms is not None else _env_int("AGENT_SETTLE_MS", 400)) / 1000.0

        # Optional warm per-test session (shared with other steps); when present and
        # alive, each turn streams through it instead of spawning a fresh process.
        self._session = session
        # Optional per-test decision audit log (every turn's decision + raw response).
        self._decision_log = decision_log
        # Optional scoped reference pack — its asset images are attached inline (after
        # the screenshot) on the full-prompt turns so the agent has symbol/UI ground truth.
        self._reference = reference
        self._decider = _AgentDecider()
        resolved_model = model or os.environ.get("AGENT_MODEL", "").strip()
        if resolved_model:
            self._decider._model = resolved_model

    async def _decide(
        self, shot: Path, goal: str, history: list[dict[str, Any]], turn_index: int = 0
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """Get the next action: via the warm session/pool if alive, else a one-shot spawn.

        A single warm session keeps conversation memory, so on turns 2+ we strip the
        instructions / coordinate system / schema / history (``follow_up``) and send
        only the new screenshot — they were established on turn 0 and are still in
        context, so re-sending is redundant (big input-token cut on later turns). The
        pre-warm pool swaps processes between turns (``stateless_turns``), so it has no
        shared memory and gets the full SELF-CONTAINED prompt every turn. The one-shot
        fallback (a fresh process, no memory) always gets the full prompt too.
        """
        sess = self._session
        if sess is not None and sess.alive:
            # Strip the prompt only when the session retains memory across turns.
            follow_up = turn_index > 0 and not getattr(sess, "stateless_turns", False)
            try:
                if follow_up:
                    # Refs (and instructions) were established on the full-prompt turn and
                    # persist in the conversation, so this turn sends only the screenshot.
                    prompt = _build_control_prompt(None, goal, [], self._viewport, inline=True, follow_up=True)
                    return await asyncio.to_thread(sess.send, prompt, _ACTION_SCHEMA, [shot], False)
                # Full prompt: screenshot FIRST, then the reference asset images — but
                # only on the session's first full turn (they persist in the conversation,
                # so re-sending them each turn would multiply images toward the cap).
                attach_refs = (
                    self._reference is not None
                    and not getattr(self._reference, "is_empty", True)
                    and not getattr(sess, "refs_sent", True)
                )
                prompt = _build_control_prompt(
                    None, goal, history, self._viewport, inline=True,
                    reference=self._reference if attach_refs else None,
                )
                if attach_refs:
                    ref_items = _reference_items(self._reference)
                    images = [shot] + [p for p, _l in ref_items]
                    captions = (
                        ["This is the CURRENT, LIVE game screen — act on THIS (it is not a reference):"]
                        + [f"This image shows {label}" for _p, label in ref_items]
                    )
                    sess.refs_sent = True
                else:
                    images, captions = [shot], None
                return await asyncio.to_thread(
                    sess.send, prompt, _ACTION_SCHEMA, images, True, captions
                )
            except _StreamError as exc:
                logger.warning("warm session failed (%s); falling back to one-shot", exc)
                try:
                    sess.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None
        # One-shot fallback: a FRESH process with no memory → always the full prompt
        # + full history, reading the screenshot from disk (absolute path).
        prompt = _build_control_prompt(str(shot.resolve()), goal, history, self._viewport, inline=False)
        return await asyncio.to_thread(
            self._decider._run_cli_timed, self._work_dir, prompt, _ACTION_SCHEMA
        )

    async def run(self, goal: str, max_steps: int | None = None) -> AgentResult:
        """Iterate perceive→decide→act until the goal is met, failed, or budget runs out."""
        budget = max_steps or self._max_steps
        steps: list[AgentStep] = []
        history: list[dict[str, Any]] = []

        for i in range(budget):
            t_turn = time.time()
            shot = self._work_dir / f"frame-{i:02d}.jpg"
            await self._page.screenshot(path=str(shot), type="jpeg")
            t_shot = time.time()

            # Decide the next action. A single warm session keeps memory, so turns 2+
            # strip the instructions/schema/history (follow_up); the pre-warm pool swaps
            # processes so it sends the full self-contained prompt — _decide picks based
            # on the session's `stateless_turns` flag.
            parsed, raw, dt = await self._decide(shot, goal, history, turn_index=i)
            if not parsed or "action" not in parsed:
                timing = self._build_timing(t_turn, t_shot, dt, t_turn, t_turn)
                self._emit_timing(i, "?", None, "no parseable action", timing)
                self._record_decision(i, "?", None, "no parseable action", raw, timing, goal)
                return AgentResult(
                    False, goal, steps,
                    error=f"agent returned no parseable action (raw: {raw[:200]!r})",
                )

            act = str(parsed.get("action", "")).lower()
            reason = str(parsed.get("reason", "") or "")
            history.append(
                {"action": act, "reason": reason,
                 **{k: parsed.get(k) for k in ("x", "y", "key", "ms") if parsed.get(k) is not None}}
            )

            # Perform the chosen action (timed). done/fail have no real action.
            detail = ""
            perform_error: str | None = None
            t_act_start = time.time()
            if act not in ("done", "fail"):
                try:
                    detail = await self._perform(act, parsed)
                except _AgentActionError as exc:
                    perform_error = str(exc)
            t_act_done = time.time()

            timing = self._build_timing(t_turn, t_shot, dt, t_act_start, t_act_done)
            self._emit_timing(i, act, parsed, reason, timing)
            self._record_decision(i, act, parsed, reason, raw, timing, goal)
            steps.append(AgentStep(i, act, detail, reason, shot.name, timing))

            if act == "done":
                return AgentResult(True, goal, steps)
            if act == "fail":
                return AgentResult(False, goal, steps, error=reason or "agent reported fail")
            if perform_error is not None:
                return AgentResult(False, goal, steps, error=perform_error)

            # Settle only after an action that changes the screen (click / key press),
            # giving the animation it triggered a moment to start before the next frame.
            # A 'wait' gets NO settle: the decision round-trip already advanced real time,
            # so we loop straight to a fresh screenshot (see _perform).
            if act in ("click", "press_key"):
                await asyncio.sleep(self._settle_s)

        return AgentResult(False, goal, steps, error=f"goal not reached within {budget} steps")

    def _build_timing(
        self,
        t_turn: float,
        t_shot: float,
        dt: dict[str, Any],
        t_act_start: float,
        t_act_done: float,
    ) -> dict[str, Any]:
        """Assemble the per-turn timing record (absolute 'when' + derived ms)."""
        t_spawn = dt.get("t_spawn")
        t_sent = dt.get("t_prompt_sent")
        t_result = dt.get("t_result")
        return {
            # absolute 'when' events (epoch seconds)
            "t_turn_start": t_turn,
            "t_screenshot_done": t_shot,
            "t_spawn": t_spawn,
            "t_prompt_sent": t_sent,
            "t_result": t_result,
            "t_action_start": t_act_start,
            "t_action_done": t_act_done,
            # derived durations (ms)
            "screenshot_ms": _ms(t_shot, t_turn),
            "prompt_send_ms": _ms(t_sent, t_spawn),
            # decision = prompt-sent → result: the true per-turn latency. For a cold
            # turn (one-shot, or a warm session's first turn) it includes the process
            # startup; for warm turns it does not — so warm turns read lower here.
            "decision_ms": _ms(t_result, t_sent),
            "action_ms": _ms(t_act_done, t_act_start),
            "settle_ms": round(self._settle_s * 1000),
            # True only when this turn reused an already-warm session (no cold-start).
            "warm": not dt.get("cold", True),
            # CLI self-reported (from the result/json envelope)
            "cli_duration_ms": dt.get("cli_duration_ms"),
            "cli_api_ms": dt.get("cli_api_ms"),
        }

    def _emit_timing(
        self,
        i: int,
        act: str,
        parsed: dict[str, Any] | None,
        reason: str,
        timing: dict[str, Any],
    ) -> None:
        """Log the per-turn timing and append one JSON line to timings.jsonl."""
        click = ""
        if parsed and act == "click":
            click = f" ({parsed.get('x')},{parsed.get('y')})"
        logger.info(
            "  agent[%d] %s%s | %s decision=%sms (cli=%sms api=%sms) screenshot=%sms "
            "action=%sms | %s",
            i, act, click,
            "WARM" if timing.get("warm") else "cold",
            timing["decision_ms"], timing["cli_duration_ms"], timing["cli_api_ms"],
            timing["screenshot_ms"], timing["action_ms"],
            (reason or "")[:70],
        )
        logger.info(
            "    when: spawn=%s prompt_sent=%s result=%s action_done=%s",
            _hms(timing["t_spawn"]), _hms(timing["t_prompt_sent"]),
            _hms(timing["t_result"]), _hms(timing["t_action_done"]),
        )
        try:
            rec: dict[str, Any] = {"turn": i, "action": act, "reason": reason, **timing}
            if parsed and act == "click":
                rec["click"] = [parsed.get("x"), parsed.get("y")]
            with (self._work_dir / "timings.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to write timings.jsonl: %s", exc)

    def _record_decision(
        self,
        turn: int,
        act: str,
        parsed: dict[str, Any] | None,
        reason: str,
        raw: str,
        timing: dict[str, Any],
        goal: str,
    ) -> None:
        """Append this turn's control decision (with raw model response) to the audit log."""
        if self._decision_log is None:
            return
        p = parsed or {}
        self._decision_log.record({
            "step": self._step_id,
            "kind": "action",
            "turn": turn,
            "goal": goal,
            "action": act,
            "x": p.get("x"), "y": p.get("y"), "key": p.get("key"), "ms": p.get("ms"),
            "reason": reason,
            "warm": timing.get("warm"),
            "decision_ms": timing.get("decision_ms"),
            "cli_duration_ms": timing.get("cli_duration_ms"),
            "raw": raw,
        })

    async def _perform(self, act: str, decision: dict[str, Any]) -> str:
        """Execute one click/press_key/wait and return a short detail string."""
        if act == "click":
            x, y = decision.get("x"), decision.get("y")
            if x is None or y is None:
                raise _AgentActionError("click action missing x/y")
            cx = _clamp(float(x), 0, self._viewport[0])
            cy = _clamp(float(y), 0, self._viewport[1])
            await self._page.mouse.click(cx, cy)
            return f"({int(cx)},{int(cy)})"

        if act == "press_key":
            key = decision.get("key")
            if not key:
                raise _AgentActionError("press_key action missing 'key'")
            await self._page.keyboard.press(str(key))
            return str(key)

        if act == "wait":
            # No artificial sleep. By the time this 'wait' decision returned, the
            # perceive→decide round-trip (seconds) had already elapsed since the
            # analysed frame, so we immediately grab a fresh screenshot rather than
            # sleeping again. Any `ms` the model supplied is intentionally ignored.
            return "re-check (no sleep)"

        raise _AgentActionError(f"unknown agent action {act!r}")


class _AgentActionError(RuntimeError):
    """Raised when a decided action is malformed or unknown."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _ms(a: float | None, b: float | None) -> int | None:
    """Milliseconds between two epoch timestamps (None if either is missing)."""
    if a is None or b is None:
        return None
    return round((a - b) * 1000)


def _hms(t: float | None) -> str:
    """Format an epoch timestamp as local HH:MM:SS.mmm (— if missing)."""
    if t is None:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using %s", name, raw, default)
        return default
