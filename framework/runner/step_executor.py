"""
Step executor for the QA platform test runner.

Maps YAML action type strings to Python helper calls. After each step the
executor records a wall-clock timestamp in the timeline and drains the bridge
event buffer to the artifact log.

Public interface
----------------
StepExecutor(bridge, network, artifact, gemini_image, page)
    Execute individual test steps.

    execute(step) -> StepResult
        Execute a single YAML step dict. Returns a StepResult with the outcome.

StepResult
    Dataclass: success (bool), error (str | None), screenshot_path (Path | None),
               ai_result (dict | None), bridge_assert_result (dict | None),
               start_ms (int), end_ms (int).

UnsupportedActionError
    Raised when a step specifies an unknown action type — never silently ignored.

Supported action types
----------------------
navigate, wait_for_state, bridge_action, bridge_assert, capture, assert,
script, go_offline, go_online, press_key, click, wait_ms
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI color helpers for console output
# ---------------------------------------------------------------------------
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"

_ACTION_COLORS: dict[str, str] = {
    "navigate": _BLUE,
    "wait_for_state": _YELLOW,
    "bridge_action": _CYAN,
    "bridge_assert": _MAGENTA,
    "capture": _GREEN,
    "assert": _MAGENTA,
    "script": _YELLOW,
    "click": _CYAN,
    "press_key": _CYAN,
    "wait_ms": _DIM,
    "go_offline": _RED,
    "go_online": _GREEN,
}


def _step_log(step_id: str, action: str, description: str, detail: str = "") -> str:
    """Format a colorful step log line."""
    color = _ACTION_COLORS.get(action, _CYAN)
    desc = f" — {description}" if description else ""
    extra = f"  {_DIM}{detail}{_RESET}" if detail else ""
    return f"  {color}{_BOLD}{action}{_RESET} {_DIM}[{step_id}]{_RESET}{desc}{extra}"


def _result_log(step_id: str, success: bool, duration_ms: int, detail: str = "") -> str:
    """Format a colorful result log line."""
    icon = f"{_GREEN}✓{_RESET}" if success else f"{_RED}✗{_RESET}"
    extra = f"  {detail}" if detail else ""
    return f"  {icon} {_DIM}[{step_id}] {duration_ms}ms{_RESET}{extra}"


class UnsupportedActionError(ValueError):
    """Raised when a YAML step specifies an action type not handled by the executor."""


@dataclass
class StepResult:
    """Outcome of a single executed step."""

    success: bool
    error: str | None = None
    screenshot_path: Path | None = None
    # Return value from bridge_action queries (getBalance, getGameInfo, etc.)
    result: Any = None
    # AI assertion result (populated inline for image mode; None for video/pending)
    ai_result: dict[str, Any] | None = None
    # Bridge assertion result (populated for assert/bridge_assert steps)
    bridge_assert_result: dict[str, Any] | None = None
    # Wall-clock timestamps relative to test start (milliseconds)
    start_ms: int = 0
    end_ms: int = 0
    # Whether the AI result is pending (video mode — processed asynchronously)
    ai_pending: bool = False
    # Step id for timeline correlation
    step_id: str = ""
    action: str = ""


class StepExecutor:
    """
    Dispatches YAML step dicts to Python helper calls.

    Parameters
    ----------
    bridge:
        BridgeHelper wrapping the active Playwright page.
    network:
        NetworkHelper wrapping the active Playwright browser context.
    artifact:
        ArtifactContext for saving screenshots and appending events.
    gemini_image:
        GeminiImageHelper for inline AI image assertions.
    page:
        The raw Playwright Page (needed for press_key / click).
    test_start_time:
        Unix timestamp when the test started (for timeline ms offsets).
    """

    def __init__(
        self,
        bridge: Any,
        network: Any,
        artifact: Any,
        gemini_image: Any,
        page: Any,
        test_start_time: float | None = None,
        game_url: str | None = None,
    ) -> None:
        self._bridge = bridge
        self._network = network
        self._artifact = artifact
        self._gemini_image = gemini_image
        self._page = page
        self._test_start_time: float = test_start_time or time.time()
        self._game_url: str = game_url or ""
        # Accumulates timeline entries: list of dicts with id, action, start_ms, end_ms
        self.timeline: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def execute(self, step: dict[str, Any]) -> StepResult:
        """
        Execute a single YAML step and return the result.

        Parameters
        ----------
        step:
            A parsed YAML step dict, e.g.
            ``{"id": "step-01", "action": "bridge_action", "params": {"name": "spin"}}``.

        Returns
        -------
        StepResult
            Never raises — all errors are captured in StepResult.error.
        """
        step_id = step.get("id", "")
        description = step.get("description", "")
        action = step.get("action", "assert" if "assert" in step else "")
        start_ms = self._elapsed_ms()

        # Log step start with action detail
        detail = self._step_detail(step)
        logger.info(_step_log(step_id, action, description, detail))

        try:
            result = await self._dispatch(step)
        except UnsupportedActionError:
            raise  # propagate immediately — callers should know about schema errors
        except Exception as exc:  # noqa: BLE001
            result = StepResult(success=False, error=str(exc))

        end_ms = self._elapsed_ms()
        duration = end_ms - start_ms
        result.start_ms = start_ms
        result.end_ms = end_ms
        result.step_id = step_id
        result.action = action

        # Log step result
        err_detail = f"{_RED}{result.error}{_RESET}" if result.error else ""
        logger.info(_result_log(step_id, result.success, duration, err_detail))

        # Record timeline entry (include result for script step access)
        entry: dict[str, Any] = {
            "id": step_id,
            "action": action,
            "description": step.get("description", ""),
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        if result.result is not None:
            entry["result"] = result.result
        self.timeline.append(entry)

        # Emit step action as an event so it appears in the replay console
        step_event = {
            "t": round(start_ms / 1000.0, 3),
            "type": "step",
            "data": {
                "id": step_id,
                "action": action,
                "description": description,
                "detail": detail,
                "success": result.success,
                "error": result.error,
                "duration_ms": duration,
            },
        }
        self._artifact.append_events([step_event])

        # Drain bridge events after every step — skip for actions that
        # reload the page (bridge is not registered yet after navigation).
        if action not in ("navigate",):
            await self._drain_events()

        return result

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, step: dict[str, Any]) -> StepResult:
        """Route step to the appropriate handler based on action type."""
        # assert steps may omit "action" field
        if "assert" in step and step.get("action") in (None, "", "assert"):
            return await self._handle_assert(step)

        action = step.get("action", "")
        params: dict[str, Any] = step.get("params") or {}

        handlers = {
            "navigate": self._handle_navigate,
            "wait_for_state": self._handle_wait_for_state,
            "bridge_action": self._handle_bridge_action,
            "bridge_assert": self._handle_bridge_assert,
            "capture": self._handle_capture,
            "assert": self._handle_assert,
            "script": self._handle_script,
            "go_offline": self._handle_go_offline,
            "go_online": self._handle_go_online,
            "press_key": self._handle_press_key,
            "click": self._handle_click,
            "wait_ms": self._handle_wait_ms,
        }

        handler = handlers.get(action)
        if handler is None:
            raise UnsupportedActionError(
                f"Unsupported action type '{action}' in step '{step.get('id', '?')}'. "
                f"Supported types: {', '.join(sorted(handlers.keys()))}."
            )

        return await handler(step)

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    async def _handle_navigate(self, step: dict[str, Any]) -> StepResult:
        """navigate — open a URL in the page.

        Supports two param forms:
          params.url          — navigate to a literal URL.
          params.url_script   — resolve to the game URL set at construction time.
        """
        params = step.get("params") or {}
        url = params.get("url", "")
        if not url and params.get("url_script"):
            url = self._game_url
        if not url:
            return StepResult(
                success=False,
                error="navigate action requires 'params.url' or 'params.url_script' (with game_url set on executor)",
            )
        await self._page.goto(url, wait_until="networkidle")
        return StepResult(success=True)

    async def _handle_wait_for_state(self, step: dict[str, Any]) -> StepResult:
        """wait_for_state — poll bridge until getAppState() == state."""
        params = step.get("params") or {}
        state = params.get("state", "")
        timeout_ms = int(params.get("timeout_ms", 30_000))
        if not state:
            return StepResult(
                success=False,
                error="wait_for_state action requires 'params.state'",
            )
        logger.debug("Waiting for state: %s (timeout=%dms)", state, timeout_ms)
        await self._bridge.wait_for_state(state, timeout_ms=timeout_ms)
        # Capture screenshot if step has capture: true
        screenshot_path: Path | None = None
        if step.get("capture"):
            screenshot_path = await self._capture_screenshot()
        return StepResult(success=True, screenshot_path=screenshot_path)

    async def _handle_bridge_action(self, step: dict[str, Any]) -> StepResult:
        """bridge_action — call a named method on the bridge."""
        params = step.get("params") or {}
        name = params.get("name", "")
        if not name:
            return StepResult(
                success=False,
                error="bridge_action requires 'params.name'",
            )

        result_value = None
        method = getattr(self._bridge, self._camel_to_snake(name), None)
        if method is None:
            # Fall through to generic triggerAction (covers future bridge methods)
            logger.debug("bridge_action: no Python wrapper for '%s', using eval", name)
            action_params = {k: v for k, v in params.items() if k != "name"}
            js_params = json.dumps(action_params) if action_params else "null"
            result_value = await self._bridge._eval(
                f"window.__gameTestBridge.triggerAction && "
                f"window.__gameTestBridge.triggerAction({json.dumps(name)}, {js_params})"
            )
        else:
            # Map extra params to keyword arguments (convert camelCase to snake_case)
            call_params = {
                self._camel_to_snake(k): v
                for k, v in params.items() if k != "name"
            }
            if call_params:
                result_value = await method(**call_params)
            else:
                result_value = await method()

        screenshot_path: Path | None = None
        if step.get("capture"):
            screenshot_path = await self._capture_screenshot()
        return StepResult(success=True, result=result_value, screenshot_path=screenshot_path)

    async def _handle_bridge_assert(self, step: dict[str, Any]) -> StepResult:
        """bridge_assert — call a bridge getter and compare to expected value."""
        params = step.get("params") or {}
        method_name = params.get("method", "")
        expected = params.get("expected")

        if not method_name:
            return StepResult(
                success=False,
                error="bridge_assert requires 'params.method'",
            )

        method = getattr(self._bridge, self._camel_to_snake(method_name), None)
        if method is None:
            return StepResult(
                success=False,
                error=f"bridge_assert: method '{method_name}' not found on BridgeHelper",
            )

        actual = await method()
        passed = actual == expected
        assert_result = {"method": method_name, "actual": actual, "expected": expected, "passed": passed}
        logger.debug("bridge_assert %s: actual=%r expected=%r passed=%s", method_name, actual, expected, passed)

        if not passed:
            return StepResult(
                success=False,
                error=f"bridge_assert failed: {method_name} returned {actual!r}, expected {expected!r}",
                bridge_assert_result=assert_result,
            )
        return StepResult(success=True, bridge_assert_result=assert_result)

    async def _handle_capture(self, step: dict[str, Any]) -> StepResult:
        """capture — take a screenshot and save to artifact dir."""
        screenshot_path = await self._capture_screenshot()
        return StepResult(success=True, screenshot_path=screenshot_path)

    async def _handle_assert(self, step: dict[str, Any]) -> StepResult:
        """
        assert — execute bridge and/or AI assertion.

        Bridge assertion: synchronous comparison, result written to StepResult.
        AI image assertion: inline Gemini call, result written to StepResult.
        AI video assertion: bridge runs synchronously; AI marked pending for batch.
        """
        assert_block = step.get("assert", {})

        bridge_result: dict[str, Any] | None = None
        ai_result: dict[str, Any] | None = None
        ai_pending = False
        errors: list[str] = []

        # --- Bridge assertion ---
        bridge_block = assert_block.get("bridge")
        if bridge_block:
            method_name = bridge_block.get("method", "")
            expected = bridge_block.get("expected")
            if not method_name:
                errors.append("assert.bridge missing 'method' field")
            else:
                method = getattr(self._bridge, self._camel_to_snake(method_name), None)
                if method is None:
                    errors.append(f"bridge method '{method_name}' not found")
                else:
                    actual = await method()
                    passed = actual == expected
                    bridge_result = {
                        "method": method_name,
                        "actual": actual,
                        "expected": expected,
                        "passed": passed,
                    }
                    if not passed:
                        errors.append(
                            f"bridge assert failed: {method_name} = {actual!r}, expected {expected!r}"
                        )

        # --- AI assertion (skip if bridge already failed — fail fast) ---
        ai_block = assert_block.get("ai")
        if ai_block and not errors:
            mode = ai_block.get("mode", "")
            if mode == "image":
                # Image AI is deferred — processed after all tests complete
                ai_pending = True
                logger.debug("Image AI assertion deferred for step %s", step.get("id"))
            elif mode == "video":
                # Video AI is deferred — mark pending
                ai_pending = True
                logger.debug("Video AI assertion deferred to batch processor for step %s", step.get("id"))
            else:
                errors.append(f"assert.ai has unknown mode '{mode}'")

        success = len(errors) == 0
        error_str = "; ".join(errors) if errors else None

        return StepResult(
            success=success,
            error=error_str,
            bridge_assert_result=bridge_result,
            ai_result=ai_result,
            ai_pending=ai_pending,
        )

    async def _handle_script(self, step: dict[str, Any]) -> StepResult:
        """script — evaluate a JS expression in the browser page context.

        The expression has access to:
          - ``steps`` — dict of previous step results (keyed by step id)
          - ``bridge`` — the game test bridge (window.__gameTestBridge)
          - ``assert(cond, msg)`` — throws if cond is falsy
          - ``assert_equal(a, b)`` — deep-equal comparison, throws on mismatch
        """
        params = step.get("params") or {}
        cmd = params.get("cmd", "")
        if not cmd:
            return StepResult(success=False, error="script action requires 'params.cmd'")

        # Build the steps result map from timeline + stored results
        steps_data = {}
        for entry in self.timeline:
            sid = entry.get("id", "")
            if sid and "result" in entry:
                steps_data[sid] = entry

        # Evaluate in browser page context with helpers injected
        js = f"""
        (async () => {{
            const steps = {json.dumps(steps_data)};
            const bridge = window.__gameTestBridge;

            function assert(cond, msg) {{
                if (!cond) throw new Error(msg || 'Assertion failed');
            }}

            function assert_equal(actual, expected) {{
                const a = JSON.stringify(actual);
                const b = JSON.stringify(expected);
                if (a !== b) {{
                    throw new Error(
                        'assert_equal failed:\\n  actual:   ' + a +
                        '\\n  expected: ' + b
                    );
                }}
            }}

            {cmd}
        }})()
        """
        try:
            await self._page.evaluate(js)
            return StepResult(success=True)
        except Exception as exc:  # noqa: BLE001
            # Extract the meaningful error message from Playwright wrapper
            msg = str(exc)
            # Strip Playwright boilerplate, keep the inner Error message
            if "Error:" in msg:
                msg = msg[msg.index("Error:") :]
                # Trim after first newline of stack trace
                if "\n    at " in msg:
                    msg = msg[: msg.index("\n    at ")]
            return StepResult(success=False, error=msg.strip())

    async def _handle_go_offline(self, step: dict[str, Any]) -> StepResult:
        """go_offline — set browser context to offline mode."""
        await self._network.go_offline()
        return StepResult(success=True)

    async def _handle_go_online(self, step: dict[str, Any]) -> StepResult:
        """go_online — restore browser context to online mode."""
        await self._network.go_online()
        return StepResult(success=True)

    async def _handle_press_key(self, step: dict[str, Any]) -> StepResult:
        """press_key — press a keyboard key via Playwright."""
        params = step.get("params") or {}
        key = params.get("key", "")
        if not key:
            return StepResult(success=False, error="press_key action requires 'params.key'")
        await self._page.keyboard.press(key)
        return StepResult(success=True)

    async def _handle_click(self, step: dict[str, Any]) -> StepResult:
        """click — click a canvas coordinate or CSS selector."""
        params = step.get("params") or {}
        selector = params.get("selector")
        x = params.get("x")
        y = params.get("y")

        if selector:
            await self._page.click(selector)
        elif x is not None and y is not None:
            await self._page.mouse.click(float(x), float(y))
        else:
            return StepResult(
                success=False,
                error="click action requires 'params.selector' or both 'params.x' and 'params.y'",
            )
        return StepResult(success=True)

    async def _handle_wait_ms(self, step: dict[str, Any]) -> StepResult:
        """wait_ms — sleep for a fixed number of milliseconds."""
        params = step.get("params") or {}
        ms = int(params.get("ms", 0))
        if ms <= 0:
            return StepResult(success=False, error="wait_ms action requires 'params.ms' > 0")
        await asyncio.sleep(ms / 1000.0)
        return StepResult(success=True)

    # ------------------------------------------------------------------
    # AI image assertion (inline)
    # ------------------------------------------------------------------

    async def _run_ai_image_assert(
        self,
        step: dict[str, Any],
        ai_block: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Execute an inline AI image assertion.

        Returns (ai_result_dict, error_str). error_str is None on success.
        """
        if self._gemini_image is None:
            return None, "AI image assertion requires GeminiImageHelper (not provided)"

        # Resolve the screenshot path referenced in ref_screenshot
        ref_step_id = ai_block.get("ref_screenshot", "")
        prompt = ai_block.get("prompt", "")
        output_schema = ai_block.get("output_schema", {})
        expected = ai_block.get("expected", {})

        # Find the screenshot — look in the artifact directory
        screenshot_path = self._resolve_ref_screenshot(ref_step_id)
        if screenshot_path is None or not screenshot_path.exists():
            return None, (
                f"AI image assert: screenshot '{ref_step_id}' not found in artifact dir"
            )

        logger.info("Running AI image assert (step %s): %s", step.get("id"), ref_step_id)
        gemini_result = self._gemini_image.analyse_image(screenshot_path, prompt, output_schema)

        if not gemini_result.success:
            ai_result = {
                "passed": False,
                "reason": gemini_result.reason,
                "raw_text": gemini_result.raw_text,
            }
            return ai_result, f"AI image assert failed: {gemini_result.reason}"

        # Check expected fields
        actual_data = gemini_result.data or {}
        mismatches = []
        for key, exp_val in (expected or {}).items():
            act_val = actual_data.get(key)
            if act_val != exp_val:
                mismatches.append(f"{key}: got {act_val!r}, expected {exp_val!r}")

        passed = len(mismatches) == 0
        ai_result = {
            "passed": passed,
            "data": actual_data,
            "expected": expected,
            "mismatches": mismatches,
            "input_tokens": gemini_result.input_tokens,
            "output_tokens": gemini_result.output_tokens,
        }

        # Persist AI result to step-N-ai.json
        step_num = self._extract_step_number(step.get("id", ""))
        if step_num is not None:
            ai_path = self._artifact.ai_result_path(step_num)
            ai_path.write_text(
                json.dumps(ai_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        error = None if passed else f"AI image assert failed: {'; '.join(mismatches)}"
        return ai_result, error

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _step_detail(step: dict[str, Any]) -> str:
        """Extract a short detail string from a step for logging."""
        action = step.get("action", "")
        params = step.get("params") or {}

        if action == "navigate":
            if params.get("url_script"):
                return "url_script"
            url = params.get("url", "")
            return url[:60] + "..." if len(url) > 60 else url

        if action == "wait_for_state":
            return f"state={params.get('state', '?')}"

        if action == "bridge_action":
            name = params.get("name", "")
            extra = {k: v for k, v in params.items() if k != "name"}
            if extra:
                args = ", ".join(f"{k}={v!r}" for k, v in extra.items())
                return f"{name}({args})"
            return f"{name}()"

        if action == "bridge_assert":
            return f"{params.get('method', '?')} == {params.get('expected', '?')!r}"

        if action == "click":
            return params.get("selector", "")

        if action == "wait_ms":
            return f"{params.get('ms', '?')}ms"

        if action == "script":
            cmd = params.get("cmd", "")
            return cmd[:80] + "..." if len(cmd) > 80 else cmd

        if action in ("", "assert") and "assert" in step:
            ai = step.get("assert", {}).get("ai", {})
            mode = ai.get("mode", "")
            if mode:
                return f"ai/{mode}"

        return ""

    async def _capture_screenshot(self) -> Path:
        """Take a screenshot and save it via the artifact context."""
        screenshot_bytes: bytes = await self._page.screenshot(type="jpeg")
        return self._artifact.save_screenshot(screenshot_bytes)

    async def _drain_events(self) -> None:
        """Drain bridge event buffer and append to events.jsonl."""
        try:
            events = await self._bridge.drain_events()
            if events:
                self._artifact.append_events(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to drain bridge events: %s", exc)

    def _elapsed_ms(self) -> int:
        """Milliseconds elapsed since test start."""
        return int((time.time() - self._test_start_time) * 1000)

    def _resolve_ref_screenshot(self, ref_step_id: str) -> Path | None:
        """
        Resolve a ref_screenshot value (e.g. "step-02") to a file path.

        Returns None if the reference cannot be resolved.
        """
        if not ref_step_id:
            return None
        # ref_step_id is a step id like "step-02" — find corresponding jpg
        step_num = self._extract_step_number(ref_step_id)
        if step_num is not None:
            return self._artifact.screenshot_path(step_num)
        # Try direct name lookup in artifact dir
        candidate = self._artifact.test_dir / f"{ref_step_id}.jpg"
        return candidate if candidate.exists() else None

    @staticmethod
    def _extract_step_number(step_id: str) -> int | None:
        """Extract integer from step id like 'step-02' -> 2."""
        if not step_id:
            return None
        parts = step_id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])
        return None

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """
        Convert a camelCase bridge method name to snake_case.

        Examples: "spin" -> "spin", "getFreeSpinsRemaining" -> "get_free_spins_remaining"
        """
        import re
        s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()
