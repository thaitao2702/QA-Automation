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
script, go_offline, go_online, press_key, click, click_button, wait_ms,
bo_paytable_ensure, bo_paytable_select, bo_paytable_update, bo_paytable_reset
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _effective_action(step: dict[str, Any]) -> str:
    """Resolve a step's action, honoring human-readable sugar fields.

    Precedence: explicit ``action`` > ``assert`` block > ``do:`` (agent_action)
    > ``check:`` (agent_assert). The ``do``/``check`` sugar lets a bridge-free
    suite read as plain English steps — ``do: "spin the reels"`` /
    ``check: "the balance went down"`` — instead of action/params boilerplate.
    """
    if step.get("action"):
        return str(step["action"])
    if "assert" in step:
        return "assert"
    if "do" in step:
        return "agent_action"
    if "check" in step:
        return "agent_assert"
    return ""


# Token for injecting a saved variable into AI prompt text: {{name}}.
_AI_VAR_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


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
    "click_button": _CYAN,
    "press_key": _CYAN,
    "wait_ms": _DIM,
    "go_offline": _RED,
    "go_online": _GREEN,
    "bo_paytable_ensure": _BLUE,
    "bo_paytable_select": _BLUE,
    "bo_paytable_update": _BLUE,
    "bo_paytable_reset": _BLUE,
    "wallet_balance": _BLUE,
    "wallet_deposit": _BLUE,
    "wallet_withdraw": _BLUE,
    "wallet_set": _BLUE,
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
    bo:
        Optional BoHelper for back-office paytable updates (bo_paytable_update).
    wallet:
        Optional WalletHelper for Playsite balance changes (wallet_balance/
        wallet_deposit/wallet_withdraw).
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
        bo: Any = None,
        wallet: Any = None,
        reference: Any = None,
        ai_scheduler: Any = None,
        ai_image_sink: Any = None,
        ref_cache: Any = None,
        group_doc: Any = None,
        test_doc: Any = None,
        asset_index: Any = None,
    ) -> None:
        self._bridge = bridge
        self._network = network
        self._artifact = artifact
        self._gemini_image = gemini_image
        self._page = page
        # Launch-at-step AI image asserts: a suite-level AiAssertScheduler (capped
        # background tasks) and a record-bound sink callable(step, result, request_info)
        # that folds each result into the owning test/run record. None outside a suite run.
        self._ai_scheduler = ai_scheduler
        self._ai_image_sink = ai_image_sink
        self._test_start_time: float = test_start_time or time.time()
        self._game_url: str = game_url or ""
        self._bo = bo
        # Optional WalletHelper for Playsite balance steps (None outside a suite run
        # or when Playsite creds are absent -> wallet_* steps fail with a clear error).
        self._wallet = wallet
        # Scoped asset reference (from a test/group `assets:`) shared by this test's
        # agent_action / agent_assert steps; None = no reference (pure-pixel agent).
        self._reference = reference
        # Shared ScopedReferenceCache + this test's group/test docs + the asset index,
        # so an image AI assert can resolve its effective `asset_groups`/`assets`
        # selection (assert > test > group) and send only the scoped reference pack.
        # None outside a suite run -> image asserts use the helper's full-pack default.
        self._ref_cache = ref_cache
        self._group_doc = group_doc
        self._test_doc = test_doc
        self._asset_index = asset_index
        # Accumulates timeline entries: list of dicts with id, action, start_ms, end_ms
        self.timeline: list[dict[str, Any]] = []
        # Named variables saved via a step's `save:` field, passed to bridge
        # functions later by `bridge_assert` (var/args). Per-test scope: one
        # executor lives for setup→steps→teardown.
        self.vars: dict[str, Any] = {}
        # capture step id -> saved screenshot Path, so agent_assert can resolve a
        # `ref:` to an earlier 'before' screenshot robustly (independent of the
        # screenshot auto-increment counter).
        self._capture_paths: dict[str, Path] = {}
        # One warm `claude` process shared by this test's agent_action/agent_assert
        # steps (lazily started, closed by aclose()). None until first use; the
        # `_failed` flag latches a permanent one-shot fallback for this test.
        self._claude_session: Any = None
        self._claude_session_failed: bool = False
        # Per-test audit log of every Claude decision (do turns + check verdicts).
        self._decision_log: Any = None

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
        action = _effective_action(step)
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

        # Save this step's result into a named variable for later bridge_assert steps.
        save_name = step.get("save")
        if save_name and result.success:
            self.vars[save_name] = result.result
            entry["saved"] = {save_name: result.result}
            if result.result is None:
                logger.warning(
                    "save '%s' stored None — step %s produced no result value",
                    save_name, step_id,
                )
            else:
                logger.info(
                    "  %s%s = %r%s", _DIM, save_name, result.result, _RESET
                )

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

        action = _effective_action(step)
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
            "click_button": self._handle_click_button,
            "agent_action": self._handle_agent_action,
            "agent_assert": self._handle_agent_assert,
            "wait_ms": self._handle_wait_ms,
            "bo_paytable_ensure": self._handle_bo_paytable_ensure,
            "bo_paytable_select": self._handle_bo_paytable_select,
            "bo_paytable_update": self._handle_bo_paytable_update,
            "bo_paytable_reset": self._handle_bo_paytable_reset,
            "wallet_balance": self._handle_wallet_balance,
            "wallet_deposit": self._handle_wallet_deposit,
            "wallet_withdraw": self._handle_wallet_withdraw,
            "wallet_set": self._handle_wallet_set,
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

        When the URL carries ``bridge=1``, waits for ``window.__gameTestBridge``
        to register before returning. ``networkidle`` fires while the game is
        still booting — the bridge appears seconds later — so without this wait
        the first bridge call races the game and fails with "bridge is not
        registered". ``wait_for_state`` polls through that error and hides the
        race; suites that skip it (Mystic) hit it head-on.
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

        if "bridge=1" in url:
            # Local import: the executor keeps `page` duck-typed and never imports
            # playwright at module level (see suite_runner._run_suite).
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            timeout_ms = int(params.get("bridge_timeout_ms", 30_000))
            try:
                await self._page.wait_for_function(
                    "() => window.__gameTestBridge != null", timeout=timeout_ms
                )
            except PlaywrightTimeoutError:
                return StepResult(
                    success=False,
                    error=(
                        f"navigate: window.__gameTestBridge did not register within "
                        f"{timeout_ms}ms of page load. The page loaded but the bridge "
                        f"never appeared — check that the build ships the QA bridge and "
                        f"that '?bridge=1' reached the game (raise 'bridge_timeout_ms' "
                        f"if this build is legitimately slower to boot)."
                    ),
                )
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

    def _resolve_args(self, raw_args: Any) -> tuple[list[Any], str | None]:
        """Resolve a YAML ``args`` list to concrete call values.

        Each element is passed through literally, except a string ``"$name"``
        which resolves to the variable saved as *name* by an earlier step's
        ``save:`` field (use ``"$$"`` to escape a literal leading ``$`` —
        ``"$$5"`` → ``"$5"``). A saved variable may appear at any position.

        A string may also **embed** saved values with ``{{name}}`` placeholders —
        ``"bet:row:{{targetRow}}"`` → ``"bet:row:3"``. ``"$name"`` replaces the whole
        element (keeping its type); ``{{name}}`` interpolates into a larger string.
        Without this, ``waitForButtonClickable`` args and ``click_button params.name``
        would read identically in YAML but only the latter would resolve.

        Returns ``(arg_values, error)``; *error* is ``None`` on success. Shared
        by ``bridge_action`` and ``bridge_assert`` so both have identical
        variable-substitution semantics.
        """
        if not isinstance(raw_args, list):
            return [], "'args' must be a list"
        arg_values: list[Any] = []
        for item in raw_args:
            if isinstance(item, str) and item.startswith("$") and not item.startswith("$$"):
                name = item[1:]
                if name not in self.vars:
                    return [], (
                        f"variable '{name}' (arg '{item}') was not saved by an "
                        f"earlier step (use 'save: {name}')"
                    )
                arg_values.append(self.vars[name])
            elif isinstance(item, str) and item.startswith("$$"):
                arg_values.append(item[1:])  # escaped literal: drop one '$'
            elif isinstance(item, str) and "{{" in item:
                resolved, err = self._resolve_text_vars(item)
                if err is not None:
                    return [], err
                arg_values.append(resolved)
            else:
                arg_values.append(item)
        return arg_values, None

    def _resolve_param(self, value: Any) -> tuple[Any, str | None]:
        """Resolve a single scalar param with the same ``$name`` rules as ``_resolve_args``.

        A string ``"$name"`` resolves to the variable saved by an earlier step's
        ``save:`` field (``"$$"`` escapes a literal leading ``$``); anything else
        passes through unchanged. Lets a wallet step take ``amount: "$orig_balance"``.
        Returns ``(resolved, error)``.
        """
        resolved, err = self._resolve_args([value])
        if err is not None:
            return None, err
        return resolved[0], None

    def _resolve_text_vars(self, text: Any) -> tuple[Any, str | None]:
        """Substitute ``{{name}}`` placeholders in *text* with saved variables.

        Each ``{{name}}`` becomes ``str(self.vars[name])`` — a value a prior step
        captured via ``save: name``. Lets an AI prompt embed a real value read
        earlier so the model confirms a known fact instead of judging a fuzzy one
        (deterministic AI assert). A ``{{name}}`` whose variable was never saved is
        an error — the step fails at execution time, not at deferred-AI time.
        Non-string *text*, or text with no ``{{`` token, is returned unchanged.

        Returns ``(resolved, error)``; *error* is ``None`` on success.
        """
        if not isinstance(text, str) or "{{" not in text:
            return text, None
        missing: list[str] = []

        def _sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name not in self.vars:
                missing.append(name)
                return m.group(0)
            return str(self.vars[name])

        resolved = _AI_VAR_TOKEN.sub(_sub, text)
        if missing:
            return text, (
                f"variable '{missing[0]}' referenced in an AI prompt was not saved "
                f"by an earlier step (use 'save: {missing[0]}')"
            )
        return resolved, None

    def _resolve_in_leaves(self, value: Any) -> tuple[Any, str | None]:
        """Recursively resolve ``{{name}}`` in every string leaf of a nested value."""
        if isinstance(value, str):
            return self._resolve_text_vars(value)
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for k, v in value.items():
                rv, err = self._resolve_in_leaves(v)
                if err is not None:
                    return value, err
                out[k] = rv
            return out, None
        if isinstance(value, list):
            out_list: list[Any] = []
            for v in value:
                rv, err = self._resolve_in_leaves(v)
                if err is not None:
                    return value, err
                out_list.append(rv)
            return out_list, None
        return value, None

    def _resolve_ai_block_vars(self, ai_block: dict[str, Any]) -> str | None:
        """Resolve ``{{name}}`` placeholders in an ``assert.ai`` block, in place.

        Substitutes saved variables into ``prompt`` and into every string leaf of
        ``expected`` / ``expected_by_case``. Structural keys (``output_schema``,
        ``mode``, ``ref_screenshot``, ``ref_assets``, ``video_trim``) are left
        untouched. Runs at step-execution time while ``self.vars`` is live, so the
        deferred image/video processors that read this same block later see the
        resolved text. Returns the first error string, or ``None`` on success.
        """
        if "prompt" in ai_block:
            resolved, err = self._resolve_text_vars(ai_block["prompt"])
            if err is not None:
                return err
            ai_block["prompt"] = resolved
        for key in ("expected", "expected_by_case"):
            if key in ai_block:
                resolved, err = self._resolve_in_leaves(ai_block[key])
                if err is not None:
                    return err
                ai_block[key] = resolved
        return None

    async def _handle_bridge_action(self, step: dict[str, Any]) -> StepResult:
        """bridge_action — call a named method on the bridge.

        Two param forms:
          * ``args: [...]`` — ordered positional arguments (literals + ``"$saved"``
            variable refs, same rules as ``bridge_assert``). The bridge JS method is
            invoked **directly** (``window.__gameTestBridge.<name>(...)``) and its
            return value is stored in ``StepResult.result`` (so it can be ``save:``d).
            Use this for methods that take an options object / array or have no Python
            wrapper — e.g. the Hold & Spin capture + analyser API (``startEventCapture``,
            ``endEventCapture``, ``spinUntilBonus``, ``captureHoldSpinBonus``,
            ``filterBonusEvent``).
          * no ``args`` — extra params are mapped to the BridgeHelper Python wrapper's
            keyword arguments (camelCase → snake_case), falling back to the generic
            ``triggerAction`` eval when no wrapper exists.
        """
        params = step.get("params") or {}
        name = params.get("name", "")
        if not name:
            return StepResult(
                success=False,
                error="bridge_action requires 'params.name'",
            )

        # Explicit positional-args form — call the bridge JS method directly and
        # keep the return value. When 'args' is present, other params (besides
        # 'name') are ignored.
        if params.get("args") is not None:
            arg_values, err = self._resolve_args(params["args"])
            if err is not None:
                return StepResult(success=False, error=f"bridge_action: {err}")
            js_args = ", ".join(json.dumps(a) for a in arg_values)
            expr = f"window.__gameTestBridge.{name}({js_args})"
            try:
                result_value = await self._bridge._eval(expr)
            except Exception as exc:  # noqa: BLE001
                return StepResult(
                    success=False,
                    error=f"bridge_action: calling {name}(...) failed: {exc}",
                )
            screenshot_path = await self._capture_screenshot() if step.get("capture") else None
            return StepResult(success=True, result=result_value, screenshot_path=screenshot_path)

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
        """bridge_assert — call a bridge method and compare its return to expected.

        Deterministic: the assertion logic lives in the game bridge, not in the
        test runner. Arguments may reference variables saved by earlier steps.

        params:
          method (required) — bridge method name (camelCase, e.g. isSpinEnabled,
                              testFreeSpin).
          expected (required) — value the method's return is compared against (==).
          args (optional) — list of arguments in call order. Each element is a
                            literal, except a string ``"$name"`` which resolves to
                            the variable saved as *name*. Use ``"$$"`` to escape a
                            literal leading ``$`` (e.g. ``"$$5"`` → ``"$5"``). A
                            saved variable can appear at any position.

        With no ``args`` this calls the BridgeHelper Python wrapper (covers all
        existing getters). When ``args`` is given it calls the bridge JS method
        directly, so newly game-exposed functions need no Python wrapper.
        """
        params = step.get("params") or {}
        method_name = params.get("method", "")
        path = params.get("path")

        # Resolve a "$name" in `expected` to a variable saved by an earlier step,
        # so a value a prior step computed can be the comparison target (same rule
        # as `args` and the wallet `amount`). Lets a `script` step compute a value
        # and a following case-tagged `bridge_assert` own the verdict.
        expected, err = self._resolve_param(params.get("expected"))
        if err is not None:
            return StepResult(success=False, error=f"bridge_assert: {err}")

        if not method_name:
            return StepResult(
                success=False,
                error="bridge_assert requires 'params.method'",
            )

        raw_args = params.get("args")
        if raw_args is not None:
            # Resolve each arg, substituting "$name" with the saved variable value.
            arg_values, err = self._resolve_args(raw_args)
            if err is not None:
                return StepResult(success=False, error=f"bridge_assert: {err}")

            # Call the bridge JS method directly. Playwright awaits the returned
            # promise if the bridge function is async.
            js_args = ", ".join(json.dumps(a) for a in arg_values)
            expr = f"window.__gameTestBridge.{method_name}({js_args})"
            try:
                actual = await self._bridge._eval(expr)
            except Exception as exc:  # noqa: BLE001
                return StepResult(
                    success=False,
                    error=f"bridge_assert: calling {method_name}(...) failed: {exc}",
                )
        else:
            arg_values = []
            method = getattr(self._bridge, self._camel_to_snake(method_name), None)
            if method is None:
                return StepResult(
                    success=False,
                    error=f"bridge_assert: method '{method_name}' not found on BridgeHelper",
                )
            actual = await method()

        # Optionally drill into a nested field of the returned value before the
        # equality check. Lets object-returning bridge functions (e.g.
        # verifyHoldSpinCoinsHeld → {valid, bonusFound, respinCount, violations})
        # be asserted on one field — `path: valid, expected: true` — without
        # pinning non-deterministic siblings like respinCount.
        compared = actual
        if path is not None:
            compared, perr = self._extract_path(actual, str(path))
            if perr is not None:
                return StepResult(
                    success=False,
                    error=f"bridge_assert: path '{path}' on {method_name}(...) result failed: {perr}",
                    bridge_assert_result={
                        "method": method_name, "args": arg_values, "path": path,
                        "actual_full": actual, "expected": expected, "passed": False,
                    },
                )

        passed = compared == expected
        assert_result = {
            "method": method_name,
            "args": arg_values,
            "actual": compared,
            "expected": expected,
            "passed": passed,
        }
        if path is not None:
            assert_result["path"] = path
            assert_result["actual_full"] = actual
        logger.debug(
            "bridge_assert %s(%r)%s: actual=%r expected=%r passed=%s",
            method_name, arg_values, f".{path}" if path is not None else "",
            compared, expected, passed,
        )

        if not passed:
            where = f"{method_name}({arg_values!r})" + (f".{path}" if path is not None else "")
            return StepResult(
                success=False,
                error=f"bridge_assert failed: {where} returned {compared!r}, expected {expected!r}",
                bridge_assert_result=assert_result,
            )
        return StepResult(success=True, bridge_assert_result=assert_result)

    async def _handle_capture(self, step: dict[str, Any]) -> StepResult:
        """capture — take a screenshot and save to artifact dir."""
        screenshot_path = await self._capture_screenshot()
        sid = step.get("id")
        if sid:
            # Remember this capture so an agent_assert `ref:` can resolve to it.
            self._capture_paths[str(sid)] = screenshot_path
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
            # Inject {{name}} saved-variable placeholders into prompt/expected NOW,
            # while self.vars is live — the deferred image/video processors read
            # this same block later. A missing variable fails the step here.
            var_err = self._resolve_ai_block_vars(ai_block)
            if var_err is not None:
                errors.append(var_err)
            else:
                mode = ai_block.get("mode", "")
                if mode == "image":
                    # Image AI launches NOW as a capped background task (the
                    # referenced screenshot already exists). Verdict stays pending
                    # until the task's sink folds it into the record.
                    ai_pending = True
                    self._launch_image_ai(step, ai_block)
                elif mode == "video":
                    # Video AI is deferred — its clip only exists after the suite's
                    # shared recording is finalized (video_ai_processor handles it).
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

    def _image_reference_for(self, ai_block: dict[str, Any]) -> Any:
        """Resolve the scoped reference pack for one image AI assert.

        Computes the effective ``asset_groups``/``assets`` selection (assert > test >
        group) and returns the cached scoped SymbolReference, or ``None`` to let the
        helper use its full-pack default (no cache wired, or no selection anywhere).
        """
        if self._ref_cache is None:
            return None
        from framework.helpers.symbol_reference import effective_selection

        sel = effective_selection(
            self._group_doc, self._test_doc, ai_block, self._asset_index
        )
        if sel is None:
            return None
        return self._ref_cache.for_selection(*sel)

    def _launch_image_ai(self, step: dict[str, Any], ai_block: dict[str, Any]) -> None:
        """Submit an image AI assertion to the background scheduler now.

        Resolves the ref screenshot(s) from this test's captures and fires a capped
        background task whose sink folds the verdict into the record. If a ref is
        missing, folds a fail immediately (no task). No-op when no scheduler/sink is
        wired (e.g. no AI provider) — the step stays pending, matching prior behavior.
        """
        if not (self._ai_scheduler and self._ai_scheduler.enabled and self._ai_image_sink):
            logger.debug("Image AI assertion: no scheduler/sink — left pending for step %s", step.get("id"))
            return
        prompt = ai_block.get("prompt", "")
        output_schema = ai_block.get("output_schema", {})
        expected = ai_block.get("expected", {})
        paths, missing = self._resolve_step_ref_paths(ai_block.get("ref_screenshot", ""))
        request_info = {
            "prompt": prompt,
            "output_schema": output_schema,
            "expected": expected,
            "screenshots": [p.name for p in paths],
        }
        if missing:
            from framework.helpers.gemini import GeminiResult
            failed = GeminiResult(
                success=False, data=None,
                reason=f"screenshot(s) not found: {', '.join(missing)}",
            )
            self._ai_image_sink(step, failed, request_info)
            return
        self._ai_scheduler.submit_image(
            screenshot_paths=paths,
            prompt=prompt,
            output_schema=output_schema,
            sink=lambda result, _s=step, _ri=request_info: self._ai_image_sink(_s, result, _ri),
            reference=self._image_reference_for(ai_block),
        )

    def _resolve_step_ref_paths(self, ref: Any) -> tuple[list[Path], list[str]]:
        """Resolve ``ref_screenshot`` id(s) to screenshot Paths AT step time.

        Uses ``self._capture_paths`` (capture-step-id -> Path, populated as capture
        steps run) first, then a ``step-NN`` numeric fallback. Returns
        ``(paths, missing)``. Because this runs at the assert step, every referenced
        capture must PRECEDE it (the normal pattern; a forward ref fails fast).
        """
        ref_ids = ref if isinstance(ref, list) else [ref]
        paths: list[Path] = []
        missing: list[str] = []
        for rid in ref_ids:
            found = self._capture_paths.get(str(rid))
            if (found is None or not found.exists()) and isinstance(rid, str) and rid.startswith("step-"):
                try:
                    found = self._artifact.screenshot_path(int(rid.split("-")[1]))
                except (ValueError, IndexError):
                    found = None
            if found and found.exists():
                paths.append(found)
            else:
                missing.append(str(rid))
        return paths, missing

    async def _handle_script(self, step: dict[str, Any]) -> StepResult:
        """script — evaluate a JS expression in the browser page context.

        The expression has access to:
          - ``steps`` — dict of previous step results (keyed by step id)
          - ``vars`` — dict of variables saved via step ``save:`` fields
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
            const vars = {json.dumps(self.vars)};
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
            # Capture the IIFE's resolved value so a script's `return X` can be kept
            # with `save:` (like bridge_action). Previously discarded, so `save:` on a
            # script stored None. Purely additive — a script with no `return` yields
            # None, unchanged. Note: mutating the injected `vars` copy does NOT persist
            # back; produce values via `return` + `save:`, not `vars.X = ...`.
            value = await self._page.evaluate(js)
            return StepResult(success=True, result=value)
        except Exception as exc:  # noqa: BLE001
            return StepResult(success=False, error=self._clean_eval_error(str(exc)))

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

    async def _handle_click_button(self, step: dict[str, Any]) -> StepResult:
        """click_button — click a named button at its real on-screen coordinates.

        Resolves ``params.name`` to a screen rectangle via the bridge's
        ``getButtonRect(name)`` (bridge-api.md §3.7), then fires a real OS-level
        pointer event at the rect's centre (``page.mouse.click(centerX, centerY)``).
        This emulates a genuine user tap on the canvas instead of invoking the
        button's bridge action directly.

        Fails (never silently) when the button is absent (unknown name or its popup
        is closed) or, unless ``params.require_visible: false``, when the rect is not
        visible/clickable. The resolved rect is returned as the step result so it can
        be ``save:``d for a later assert.
        """
        params = step.get("params") or {}
        name = params.get("name", "")
        if not name:
            return StepResult(success=False, error="click_button action requires 'params.name'")

        # Resolve {{var}} tokens so a button name can be derived at run time from a
        # value an earlier step saved -- e.g. name: "bet:row:{{targetRow}}" taps the
        # denomination row computed from the server's config instead of a hardcoded
        # index. Same substitution AI prompts use; a missing variable fails the step.
        name, err = self._resolve_text_vars(name)
        if err:
            return StepResult(success=False, error=f"click_button: {err}")

        try:
            rect = await self._bridge.get_button_rect(name)
        except Exception as exc:  # noqa: BLE001 — surface a clear step failure, never crash
            return StepResult(
                success=False,
                error=f"click_button: getButtonRect({name!r}) failed: {exc}",
            )

        if not rect:
            return StepResult(
                success=False,
                error=f"click_button: button '{name}' not found "
                "(unknown name or its owning popup is closed)",
            )

        require_visible = params.get("require_visible", True)
        if require_visible and not rect.get("visible", False):
            return StepResult(
                success=False,
                error=f"click_button: button '{name}' is not visible/clickable "
                f"(visible=false); rect={rect}",
            )

        cx, cy = rect.get("centerX"), rect.get("centerY")
        if cx is None or cy is None:
            return StepResult(
                success=False,
                error=f"click_button: button '{name}' rect has no centerX/centerY: {rect}",
            )

        await self._page.mouse.click(float(cx), float(cy))
        screenshot_path = await self._capture_screenshot() if step.get("capture") else None
        return StepResult(success=True, result=rect, screenshot_path=screenshot_path)

    def _get_claude_session(self) -> Any:
        """Return the per-test warm Claude session, lazily starting it once.

        Shared by all ``do:``/``check:`` steps in this test so the process
        cold-start is paid once instead of per turn. Returns None (→ one-shot
        fallback) when disabled via ``AGENT_STREAM`` in {0,false,no}, when a prior
        session died, or when startup fails.
        """
        if os.environ.get("AGENT_STREAM", "").strip().lower() in ("0", "false", "no"):
            return None
        if self._claude_session_failed:
            return None
        if self._claude_session is not None:
            if self._claude_session.alive:
                return self._claude_session
            # Died mid-test → don't hand out a broken pipe; latch one-shot fallback.
            self._claude_session_failed = True
            return None
        try:
            from framework.helpers.agent_control import ClaudeStreamSession, PrewarmStreamPool

            # Default: a single long-lived warm session. Measured A/B shows this is
            # the fastest option — the CLI/API prompt-caches the accumulated context,
            # so per-turn latency stays flat even over many turns. AGENT_PREWARM=1 opts
            # into the pre-warm pool (background replacement + swap between turns); it
            # bounds accumulated context but forfeits that prompt cache on each swap, so
            # it is slightly SLOWER per turn — only worth it to cap context growth on a
            # pathologically long single step (avoid hitting the context window).
            if os.environ.get("AGENT_PREWARM", "0").strip().lower() in ("1", "true", "yes", "on"):
                sess = PrewarmStreamPool()
                kind = "pre-warm pool"
            else:
                sess = ClaudeStreamSession()
                kind = "warm session"
            sess.start(self._artifact.test_dir)
            self._claude_session = sess
            logger.info("Started Claude %s for this test (PID-backed stream-json)", kind)
            return sess
        except Exception as exc:  # noqa: BLE001 — any startup failure → one-shot
            logger.warning("Could not start warm Claude session: %s; using one-shot calls", exc)
            self._claude_session_failed = True
            return None

    def _get_decision_log(self) -> Any:
        """Return the per-test decision audit log (``decisions.jsonl``), created once."""
        if self._decision_log is None:
            from framework.helpers.agent_control import DecisionLog

            self._decision_log = DecisionLog(self._artifact.test_dir / "decisions.jsonl")
        return self._decision_log

    async def aclose(self) -> None:
        """Reap the per-test warm Claude session, if any. Idempotent."""
        sess, self._claude_session = self._claude_session, None
        if sess is not None:
            try:
                await asyncio.to_thread(sess.close)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing warm Claude session: %s", exc)

    async def _handle_agent_action(self, step: dict[str, Any]) -> StepResult:
        """agent_action — drive the game toward a goal with Claude vision (no bridge).

        Bridge-free control for canvas games: screenshots the canvas, asks a
        Claude Code CLI sub-agent for the next click/keypress, performs it with
        Playwright, and loops until the goal is met or the step budget runs out.
        See framework/helpers/agent_control.py. Game-agnostic — the only inputs
        are the natural-language goal and the on-screen pixels.
        """
        # Imported lazily so the Claude/Gemini helper chain only loads when an
        # agent_action step actually runs.
        from framework.helpers.agent_control import AgentController

        params = step.get("params") or {}
        # 'do:' is the human-readable sugar for the goal (top-level on the step).
        goal = params.get("goal") or step.get("do") or params.get("prompt")
        if not goal:
            return StepResult(
                success=False,
                error="agent_action requires a goal ('params.goal' or a top-level 'do:')",
            )
        max_steps = int(params.get("max_steps") or step.get("max_steps") or 0) or None
        model = params.get("model") or step.get("model")

        # Per-step screenshot dir under the test artifact directory.
        step_id = step.get("id", "agent")
        work_dir = self._artifact.test_dir / "agent" / str(step_id)

        vp = self._page.viewport_size or {}
        viewport = (int(vp.get("width", 531)), int(vp.get("height", 919)))

        controller = AgentController(
            self._page, work_dir, viewport=viewport, max_steps=max_steps, model=model,
            session=self._get_claude_session(),
            decision_log=self._get_decision_log(),
            reference=self._reference,
        )
        result = await controller.run(goal, max_steps=max_steps)
        return StepResult(
            success=result.success,
            result=result.to_dict(),
            error=None if result.success else result.error,
        )

    async def _handle_agent_assert(self, step: dict[str, Any]) -> StepResult:
        """agent_assert — verify a natural-language claim about the screen (no bridge).

        Bridge-free, vision-based assertion: screenshots the current canvas, asks a
        Claude Code CLI sub-agent whether the plain-English claim is true, and the
        step passes/fails on its verdict. An optional ``ref:`` (a prior ``capture``
        step id) supplies a 'before' screenshot for before/after claims.

        Readable sugar: ``check: "<claim>"`` (top-level on the step) is equivalent
        to ``action: agent_assert`` with ``params.check``.
        """
        from framework.helpers.agent_control import AgentVerifier

        params = step.get("params") or {}
        check = params.get("check") or step.get("check") or params.get("prompt")
        if not check:
            return StepResult(
                success=False,
                error="agent_assert requires a claim ('params.check' or a top-level 'check:')",
            )
        # Inject {{name}} saved-variable placeholders into the claim (deterministic check).
        check, var_err = self._resolve_text_vars(check)
        if var_err is not None:
            return StepResult(success=False, error=f"agent_assert: {var_err}")
        model = params.get("model") or step.get("model")

        step_id = str(step.get("id", "agent-assert"))
        now = self._artifact.test_dir / f"{step_id}-now.jpg"
        await self._page.screenshot(path=str(now), type="jpeg")

        ref_path: Path | None = None
        ref_id = params.get("ref") or step.get("ref")
        if ref_id:
            ref_path = self._capture_paths.get(str(ref_id))
            if ref_path is None:
                return StepResult(
                    success=False,
                    error=f"agent_assert ref '{ref_id}' not found — it must be the id of an earlier 'capture' step",
                )

        verifier = AgentVerifier(
            model=model, session=self._get_claude_session(), reference=self._reference
        )
        parsed, raw = await asyncio.to_thread(verifier.verify, now, check, ref_path)
        passed = bool(parsed and parsed.get("passed"))
        reason = (parsed or {}).get("reason") or (raw[:200] if not parsed else "")
        confidence = (parsed or {}).get("confidence")
        ai_result = {
            "check": check,
            "passed": passed,
            "reason": reason,
            "confidence": confidence,
        }

        # Log the verdict to console (do: turns already log; checks did not) and to
        # the per-test decision audit log (with the raw model response).
        _verdict = "PASS" if passed else "FAIL"
        _color = _GREEN if passed else _RED
        logger.info("  %scheck[%s] %s%s — %s", _color, step_id, _verdict, _RESET, (reason or "")[:90])
        self._get_decision_log().record({
            "step": step_id,
            "kind": "assert",
            "check": check,
            "ref": str(ref_id) if ref_id else None,
            "passed": passed,
            "reason": reason,
            "confidence": confidence,
            "raw": raw,
        })

        return StepResult(
            success=passed,
            error=None if passed else f"agent_assert failed: {reason}",
            ai_result=ai_result,
        )

    async def _handle_wait_ms(self, step: dict[str, Any]) -> StepResult:
        """wait_ms — sleep for a fixed number of milliseconds."""
        params = step.get("params") or {}
        ms = int(params.get("ms", 0))
        if ms <= 0:
            return StepResult(success=False, error="wait_ms action requires 'params.ms' > 0")
        await asyncio.sleep(ms / 1000.0)
        return StepResult(success=True)

    async def _handle_bo_paytable_ensure(self, step: dict[str, Any]) -> StepResult:
        """bo_paytable_ensure — ensure the automation paytable exists on the master game.

        Idempotent setup step (run once in suite setup). Creates the named paytable
        if missing so it becomes selectable per-agent, and returns its id.

        params:
          paytable_code (optional, default "AutomationTest") — the paytable name.
          paytable_ref OR paytable (optional) — initial content for a newly created
                                      paytable (fixture stem under games/{game}/paytables/,
                                      or inline dict/list/JSON string). Cloned from the
                                      game default when omitted.
          rtp (optional, default "MEDIUM") — HIGH | MEDIUM | LOW (only used on create).
          game_id, sas_entity_id (optional) — override env BO_GAME_ID/BO_SAS_ENTITY_ID.
        """
        if self._bo is None:
            return StepResult(
                success=False,
                error="bo_paytable_ensure unavailable: BO not configured (no BoHelper)",
            )

        params = step.get("params") or {}
        code = params.get("paytable_code") or "AutomationTest"

        try:
            content = None
            if params.get("paytable_ref") or params.get("paytable") is not None:
                content = self._bo.resolve_paytable(
                    ref=params.get("paytable_ref"),
                    inline=params.get("paytable"),
                )
            paytable_id = await self._bo.ensure_paytable(
                code,
                paytable_content=content,
                rtp=params.get("rtp", "MEDIUM"),
                game_id=params.get("game_id"),
                sas_entity_id=params.get("sas_entity_id"),
            )
        except Exception as exc:  # noqa: BLE001 — surface AuthError/ApiError/ValueError as step error
            return StepResult(success=False, error=str(exc))

        return StepResult(success=True, result={"paytable_code": code, "paytable_id": paytable_id})

    async def _handle_bo_paytable_select(self, step: dict[str, Any]) -> StepResult:
        """bo_paytable_select — point the agent's game at a named paytable.

        Setup step — the agent's spins use this paytable until reset.

        params:
          paytable_code (required) — the paytable name to select (e.g. "AutomationTest").
          agent_id, currency_id, game_code (optional) — override env BO_AGENT_ID/
                                      BO_CURRENCY_ID/BO_GAME_CODE.
        """
        if self._bo is None:
            return StepResult(
                success=False,
                error="bo_paytable_select unavailable: BO not configured (no BoHelper)",
            )

        params = step.get("params") or {}
        code = params.get("paytable_code")
        if not code:
            return StepResult(
                success=False,
                error="bo_paytable_select requires 'params.paytable_code'",
            )

        try:
            resp = await self._bo.select_agent_paytable(
                code,
                agent_id=params.get("agent_id"),
                currency_id=params.get("currency_id"),
                game_code=params.get("game_code"),
            )
        except Exception as exc:  # noqa: BLE001 — surface AuthError/ApiError/ValueError as step error
            return StepResult(success=False, error=str(exc))

        message = resp.get("message") if isinstance(resp, dict) else None
        return StepResult(success=True, result=message)

    async def _handle_bo_paytable_update(self, step: dict[str, Any]) -> StepResult:
        """bo_paytable_update — set the content of a paytable via the back-office API.

        Use this per group of test cases to force the determined result for that
        group (base game, free spin, …) by rewriting the automation paytable's content.

        params:
          paytable_code (required) — the paytable's `code` to replace.
          paytable_ref OR paytable — fixture stem under games/{game}/paytables/, or
                                      inline dict/list/JSON string.
          game_id, sas_entity_id (optional) — override env BO_GAME_ID/BO_SAS_ENTITY_ID.
        """
        if self._bo is None:
            return StepResult(
                success=False,
                error="bo_paytable_update unavailable: BO not configured (no BoHelper)",
            )

        params = step.get("params") or {}
        code = params.get("paytable_code")
        if not code:
            return StepResult(
                success=False,
                error="bo_paytable_update requires 'params.paytable_code'",
            )

        try:
            paytable = self._bo.resolve_paytable(
                ref=params.get("paytable_ref"),
                inline=params.get("paytable"),
            )
            resp = await self._bo.update_paytable(
                code,
                paytable,
                game_id=params.get("game_id"),
                sas_entity_id=params.get("sas_entity_id"),
            )
        except Exception as exc:  # noqa: BLE001 — surface AuthError/ApiError/ValueError as step error
            return StepResult(success=False, error=str(exc))

        message = resp.get("message") if isinstance(resp, dict) else None
        return StepResult(success=True, result=message)

    async def _handle_bo_paytable_reset(self, step: dict[str, Any]) -> StepResult:
        """bo_paytable_reset — reset the agent's game paytable to the game default.

        Teardown step — undoes bo_paytable_select.

        params:
          agent_id, currency_id, game_code (optional) — override env BO_AGENT_ID/
                                      BO_CURRENCY_ID/BO_GAME_CODE.
        """
        if self._bo is None:
            return StepResult(
                success=False,
                error="bo_paytable_reset unavailable: BO not configured (no BoHelper)",
            )

        params = step.get("params") or {}
        try:
            resp = await self._bo.reset_agent_paytable(
                agent_id=params.get("agent_id"),
                currency_id=params.get("currency_id"),
                game_code=params.get("game_code"),
            )
        except Exception as exc:  # noqa: BLE001 — surface AuthError/ApiError/ValueError as step error
            return StepResult(success=False, error=str(exc))

        message = resp.get("message") if isinstance(resp, dict) else None
        return StepResult(success=True, result=message)

    # ------------------------------------------------------------------
    # Wallet (Playsite balance) actions
    # ------------------------------------------------------------------

    async def _handle_wallet_balance(self, step: dict[str, Any]) -> StepResult:
        """wallet_balance — read the player's Playsite balance.

        params:
          currency (optional) — currency code; defaults to playsite.currency /
            PLAYSITE_CURRENCY. With a currency in effect the result is the numeric
            balance for it (save:-able for a later bridge_assert); with none, the full
            list of the player's wallets.
        """
        if self._wallet is None:
            return StepResult(
                success=False,
                error="wallet_balance unavailable: Playsite wallet not configured (no WalletHelper)",
            )
        params = step.get("params") or {}
        currency, err = self._resolve_param(params.get("currency"))
        if err is not None:
            return StepResult(success=False, error=f"wallet_balance: {err}")
        try:
            balance = await self._wallet.get_balance(currency=currency)
        except Exception as exc:  # noqa: BLE001 — surface PlaysiteError/WalletConfigError as step error
            return StepResult(success=False, error=str(exc))
        return StepResult(success=True, result=balance)

    async def _handle_wallet_set(self, step: dict[str, Any]) -> StepResult:
        """wallet_set — set the player's balance to an absolute target.

        Reads the current balance and deposits/withdraws the difference so the balance
        ends at exactly `amount`, whatever it was before. This is the robust way to
        (a) force a precise balance for a test (e.g. below the bet) and (b) restore the
        pre-test balance in teardown via `amount: "$orig_balance"`, where orig_balance
        was captured by a `wallet_balance` step in the same test's setup.

        params:
          amount (required) — the TARGET balance to set (int/float/numeric string, or a
            "$saved" reference to an earlier wallet_balance result).
          currency (optional) — defaults to playsite.currency / PLAYSITE_CURRENCY.
        """
        if self._wallet is None:
            return StepResult(
                success=False,
                error="wallet_set unavailable: Playsite wallet not configured (no WalletHelper)",
            )
        params = step.get("params") or {}
        if params.get("amount") is None:
            return StepResult(
                success=False, error="wallet_set requires 'params.amount' (the target balance)"
            )
        target, err = self._resolve_param(params.get("amount"))
        if err is not None:
            return StepResult(success=False, error=f"wallet_set: {err}")
        currency, err = self._resolve_param(params.get("currency"))
        if err is not None:
            return StepResult(success=False, error=f"wallet_set: {err}")
        try:
            data = await self._wallet.set_balance(target, currency=currency)
        except Exception as exc:  # noqa: BLE001 — surface PlaysiteError/WalletConfigError as step error
            return StepResult(success=False, error=str(exc))
        return StepResult(success=True, result=data)

    async def _handle_wallet_deposit(self, step: dict[str, Any]) -> StepResult:
        """wallet_deposit — add funds to the player's wallet via the Playsite API.

        params:
          amount (required) — amount to deposit (int/float/numeric string).
          currency (optional) — currency code; defaults to playsite.currency /
            PLAYSITE_CURRENCY.
        Result is the updated wallet data ({currency, balance}); use save: to assert it.
        """
        return await self._run_wallet_op(step, "deposit")

    async def _handle_wallet_withdraw(self, step: dict[str, Any]) -> StepResult:
        """wallet_withdraw — remove funds from the player's wallet via the Playsite API.

        params:
          amount (required) — amount to withdraw (int/float/numeric string).
          currency (optional) — currency code; defaults to playsite.currency /
            PLAYSITE_CURRENCY.
        Result is the updated wallet data ({currency, balance}); use save: to assert it.
        """
        return await self._run_wallet_op(step, "withdraw")

    async def _run_wallet_op(self, step: dict[str, Any], op: str) -> StepResult:
        """Shared body for wallet_deposit / wallet_withdraw (op in {deposit, withdraw})."""
        if self._wallet is None:
            return StepResult(
                success=False,
                error=f"wallet_{op} unavailable: Playsite wallet not configured (no WalletHelper)",
            )
        params = step.get("params") or {}
        if params.get("amount") is None:
            return StepResult(success=False, error=f"wallet_{op} requires 'params.amount'")
        amount, err = self._resolve_param(params.get("amount"))
        if err is not None:
            return StepResult(success=False, error=f"wallet_{op}: {err}")
        currency, err = self._resolve_param(params.get("currency"))
        if err is not None:
            return StepResult(success=False, error=f"wallet_{op}: {err}")
        try:
            method = self._wallet.deposit if op == "deposit" else self._wallet.withdraw
            data = await method(amount, currency=currency)
        except Exception as exc:  # noqa: BLE001 — surface PlaysiteError/WalletConfigError as step error
            return StepResult(success=False, error=str(exc))
        return StepResult(success=True, result=data)

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
        gemini_result = self._gemini_image.analyse_image(
            screenshot_path, prompt, output_schema,
            reference=self._image_reference_for(ai_block),
        )

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
        action = _effective_action(step)
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
            args = params.get("args")
            arg_str = ", ".join(str(a) for a in args) if isinstance(args, list) else ""
            call = f"{params.get('method', '?')}({arg_str})"
            return f"{call} == {params.get('expected', '?')!r}"

        if action == "click":
            return params.get("selector", "")

        if action == "click_button":
            return params.get("name", "")

        if action == "agent_action":
            goal = params.get("goal") or step.get("do") or params.get("prompt") or ""
            return goal[:60] + "..." if len(goal) > 60 else goal

        if action == "agent_assert":
            check = params.get("check") or step.get("check") or params.get("prompt") or ""
            return check[:60] + "..." if len(check) > 60 else check

        if action == "wait_ms":
            return f"{params.get('ms', '?')}ms"

        if action == "script":
            cmd = params.get("cmd", "")
            return cmd[:80] + "..." if len(cmd) > 80 else cmd

        if action == "bo_paytable_ensure":
            code = params.get("paytable_code", "AutomationTest")
            return f"ensure {code}"

        if action == "bo_paytable_select":
            return f"select {params.get('paytable_code', '?')}"

        if action == "bo_paytable_update":
            code = params.get("paytable_code", "?")
            src = params.get("paytable_ref") or ("inline" if params.get("paytable") is not None else "?")
            return f"code={code} {src}"

        if action == "bo_paytable_reset":
            return "reset → default"

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
    def _clean_eval_error(msg: str) -> str:
        """Strip Playwright boilerplate from a page.evaluate error, keep the inner message."""
        if "Error:" in msg:
            msg = msg[msg.index("Error:"):]
            if "\n    at " in msg:
                msg = msg[: msg.index("\n    at ")]
        return msg.strip()

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
    def _extract_path(value: Any, path: str) -> tuple[Any, str | None]:
        """Walk a dot-separated *path* into a nested dict/list *value*.

        ``"valid"`` → ``value["valid"]``; ``"a.b.0"`` → ``value["a"]["b"][0]``.
        Integer segments index into lists (negative indices allowed). Returns
        ``(extracted, error)``; *error* is ``None`` on success and a descriptive
        string when the path cannot be resolved.
        """
        current = value
        for seg in path.split("."):
            if isinstance(current, dict):
                if seg not in current:
                    return None, f"key '{seg}' not in result (keys: {list(current.keys())})"
                current = current[seg]
            elif isinstance(current, list):
                if not seg.lstrip("-").isdigit():
                    return None, f"segment '{seg}' is not a valid list index"
                idx = int(seg)
                if not (-len(current) <= idx < len(current)):
                    return None, f"index {idx} out of range (len {len(current)})"
                current = current[idx]
            else:
                return None, f"cannot descend into {type(current).__name__} at segment '{seg}'"
        return current, None

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """
        Convert a camelCase bridge method name to snake_case.

        Examples: "spin" -> "spin", "getFreeSpinsRemaining" -> "get_free_spins_remaining"
        """
        import re
        s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()
