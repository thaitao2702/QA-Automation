"""
Generate schema reference from the framework's source of truth.

Reads schema_validator.py and step_executor.py to extract the current
supported actions, fields, assert formats, and suite structure. Outputs
a machine-readable YAML file and a human-readable Markdown file that
skills (e.g., write-test) can read instead of hardcoding rules.

Usage:
    python scripts/generate_schema_reference.py

Output:
    framework/schema_reference.yml   — machine-readable reference
    framework/schema_reference.md    — human-readable reference for skills

Run this script after any change to schema_validator.py or step_executor.py
to keep skills in sync with the framework.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.schema_validator import (
    AI_MODE_REQUIRED_FIELDS,
    AI_MODES,
    SUPPORTED_ACTIONS,
)


# ---------------------------------------------------------------------------
# Extract action params from step_executor.py handler docstrings/signatures
# ---------------------------------------------------------------------------

def _extract_action_params() -> dict[str, dict]:
    """Parse step_executor.py to extract required params per action."""
    executor_path = _REPO_ROOT / "framework" / "runner" / "step_executor.py"
    source = executor_path.read_text(encoding="utf-8")

    # Map action -> {required_params, optional_params, description}
    action_info: dict[str, dict] = {}

    # Known params from handler implementations
    action_params = {
        "navigate": {
            "required": [],
            "optional": ["url", "url_script", "bridge_timeout_ms"],
            "description": "Navigate browser to URL. Use 'url' for static URL or 'url_script' to resolve dynamically.",
            "defaults": {"bridge_timeout_ms": 30000},
            "note": (
                "Provide either 'url' or 'url_script', not both. When the URL carries "
                "'bridge=1', navigate waits for window.__gameTestBridge to register "
                "before the step succeeds (networkidle fires while the game is still "
                "booting), so the next bridge call cannot race the game. Raise "
                "'bridge_timeout_ms' only for a build that is legitimately slow to boot."
            ),
        },
        "wait_for_state": {
            "required": ["state"],
            "optional": ["timeout_ms"],
            "description": "Wait until bridge reports the given app state.",
            "defaults": {"timeout_ms": 30000},
        },
        "bridge_action": {
            "required": ["name"],
            "optional": ["args"],
            "description": "Call a named bridge method; the return value is stored in the step result (use 'save:' to keep it). Extra params are forwarded as method arguments.",
            "note": "Two forms. (1) 'args: [..]' — ordered positional arguments calling the bridge JS method directly (window.__gameTestBridge.<name>(..)); each element is a literal except a string '$name' which resolves to a variable saved by an earlier step (use '$$' to escape a literal leading $). This is the reliable way to call methods that take an options object/array or have no Python wrapper — e.g. the Hold & Spin capture/analyser API (startEventCapture, endEventCapture, spinUntilBonus, captureHoldSpinBonus, filterBonusEvent). (2) No 'args' — any additional key-value pairs are passed as method kwargs (camelCase→snake_case), falling back to triggerAction when no wrapper exists.",
        },
        "bridge_assert": {
            "required": ["method", "expected"],
            "optional": ["args", "path"],
            "description": "Call a bridge method and assert its return value equals expected. Arguments may reference variables saved by earlier steps — the assertion logic lives in the game bridge (deterministic).",
            "note": "With no 'args', calls a BridgeHelper getter (e.g. isSpinEnabled). 'args: [..]' is an ordered argument list: each element is a literal, except a string '$name' which resolves to the variable saved as <name> (use '$$' to escape a literal leading $). A saved variable can appear at any position. When 'args' is given the bridge JS method is called directly, so game-exposed assert functions (e.g. testFreeSpin, verifyHoldSpinCoinsHeld) need no Python wrapper. 'expected' also resolves a '$name' to a saved variable, so a value an earlier step computed can be the comparison target — letting a 'script' compute a value and a following case-tagged 'bridge_assert' own the verdict. Optional 'path' drills into a nested field of the returned value before the equality check (dot-separated; integer segments index lists) — e.g. an analyser returning {valid, bonusFound, respinCount, violations} is asserted with 'path: valid, expected: true' without pinning non-deterministic siblings.",
        },
        "capture": {
            "required": [],
            "optional": [],
            "description": "Take a JPEG screenshot and save to the test artifact directory.",
        },
        "assert": {
            "required": [],
            "optional": [],
            "description": "Pure assertion step. Requires an 'assert' block with 'bridge' and/or 'ai' sub-blocks.",
            "note": "The 'action' field can be omitted for assert steps — if 'assert' block is present, it's inferred.",
        },
        "script": {
            "required": ["cmd"],
            "optional": [],
            "description": "Execute JavaScript in the browser context with access to previous step results and bridge.",
            "note": "Context provides: steps (previous results), vars (saved variables), bridge (window.__gameTestBridge), assert(cond, msg), assert_equal(a, b).",
        },
        "go_offline": {
            "required": [],
            "optional": [],
            "description": "Set browser context to offline mode (all network requests fail).",
        },
        "go_online": {
            "required": [],
            "optional": [],
            "description": "Restore browser context to online mode.",
        },
        "press_key": {
            "required": ["key"],
            "optional": [],
            "description": "Press a keyboard key (e.g., 'Space', 'Enter', 'Escape').",
        },
        "click": {
            "required": [],
            "optional": ["selector", "x", "y"],
            "description": "Click a CSS selector or canvas coordinates.",
            "note": "Provide either 'selector' OR both 'x' and 'y'.",
        },
        "click_button": {
            "required": ["name"],
            "optional": ["require_visible"],
            "description": "Click a named game button at its real on-screen coordinates (a genuine user tap), not by invoking its bridge action.",
            "note": "Resolves 'name' to a screen rect via the bridge's getButtonRect(name) (bridge-api.md §3.7), then page.mouse.click(centerX, centerY). Names: spin, stop, auto, buyBonus, nextBet, previousBet, trophy (always), plus popup element names while that popup is open (e.g. bet:row:2, auto:package:0, buyBonus:confirm). Fails if the button is absent (unknown name or its popup is closed) or — unless require_visible: false — if the rect is not visible/clickable. The resolved rect is the step result (save:-able). Pair with a wait_for_state/wait_ms afterwards if the tap opens a popup or starts a spin. 'name' supports {{var}} substitution, so a button can be derived at run time from a value an earlier step captured with 'save:' — e.g. name 'bet:row:{{targetRow}}' taps the denomination row computed from the server's config instead of a hardcoded index (a {{var}} that was never saved fails the step).",
        },
        "agent_action": {
            "required": ["goal"],
            "optional": ["max_steps", "model"],
            "description": "Drive the game toward a natural-language goal using Claude vision (no bridge, no DOM): screenshot → Claude picks the next click/keypress → Playwright performs it → loop until done or max_steps (default 8). Sugar: `do: <goal>`.",
            "note": "Bridge-free, game-agnostic control: a Claude Code CLI sub-agent looks at a screenshot of the canvas and returns the next single action (click x/y, press_key, wait, done, fail); the runner performs it and re-screenshots, looping until the agent reports the goal met or 'max_steps' (default 8) is reached. 'goal' is plain English (e.g. 'open the auto-spin popup', 'increase the bet by one step') — keep goals small and observable. Uses local Claude Code auth (no API key); model via 'model:' or CLAUDE_MODEL/AGENT_MODEL (default sonnet); tunables AGENT_MAX_STEPS/AGENT_SETTLE_MS. Warm session: all do:/check: in a test share ONE persistent claude process (AGENT_STREAM=0 forces one-shot). Result is a transcript {goal, success, error, steps[]}; per-turn screenshots saved under agent/<step-id>/; every decision is appended to decisions.jsonl. Hybrid-friendly: combine with bridge_action/bridge_assert in the same test. Readable sugar: write 'do: <goal>' at the step top-level instead of action: agent_action + params.goal.",
        },
        "agent_assert": {
            "required": ["check"],
            "optional": ["ref", "model"],
            "description": "Verify a natural-language claim about the screen using Claude vision (no bridge): screenshot → Claude judges if `check` is true → step passes/fails. `ref` = an earlier `capture` step id for before/after claims. Sugar: `check: <claim>` (+ optional `ref:`).",
            "note": "Bridge-free, vision-based verification: a Claude Code CLI sub-agent screenshots the canvas and judges whether a plain-English 'check' claim is true, returning {passed, reason, confidence}; the step passes only when passed is true. 'check' is a single observable claim (e.g. 'the spin button is visible', 'an auto-spin popup is open') — one thing per step. 'ref' is the id of an earlier 'capture' step, supplying a 'before' screenshot so claims like 'the bet is higher than before' can compare against it (resolved from the captures taken so far, robust to the screenshot counter). Uses local Claude Code auth (no API key); model via 'model:' or CLAUDE_MODEL/AGENT_MODEL (default sonnet); the verdict shows in the report. Readable sugar: write 'check: <claim>' (with optional 'ref:') at the step top-level instead of action: agent_assert.",
        },
        "wait_ms": {
            "required": ["ms"],
            "optional": [],
            "description": "Sleep for N milliseconds. ms must be > 0.",
        },
        "bo_paytable_ensure": {
            "required": [],
            "optional": ["paytable_code", "paytable_ref", "paytable", "rtp", "game_id", "sas_entity_id"],
            "description": "Ensure the automation paytable exists on the master game so it is selectable per-agent (idempotent suite-setup step).",
            "note": "paytable_code defaults to 'AutomationTest'. Optional paytable_ref/paytable seed the content of a newly created paytable (cloned from the game default when omitted). rtp (HIGH|MEDIUM|LOW, default MEDIUM) applies only on create. game_id/sas_entity_id default to the bo: block in games/{game}/env.config.yml. Requires BO_TOKEN/BO_REFRESH_TOKEN in .env.{suffix}.",
        },
        "bo_paytable_select": {
            "required": ["paytable_code"],
            "optional": ["agent_id", "currency_id", "game_code"],
            "description": "Point the agent's game at a named paytable so spins use it (suite-setup step).",
            "note": "agent_id/currency_id/game_code default to the bo: block in games/{game}/env.config.yml. Pair with bo_paytable_reset in teardown. Requires BO_TOKEN/BO_REFRESH_TOKEN in .env.{suffix}.",
        },
        "bo_paytable_update": {
            "required": ["paytable_code"],
            "optional": ["paytable_ref", "paytable", "game_id", "sas_entity_id"],
            "description": "Set the content of a paytable via the back-office API to force a group's determined result.",
            "note": "Provide either 'paytable_ref' (fixture stem under games/{game}/paytables/) or 'paytable' (inline dict/list/JSON string). game_id/sas_entity_id default to the bo: block in games/{game}/env.config.yml. Requires BO_TOKEN/BO_REFRESH_TOKEN in .env.{suffix}.",
        },
        "bo_paytable_reset": {
            "required": [],
            "optional": ["agent_id", "currency_id", "game_code"],
            "description": "Reset the agent's game paytable back to the game default (teardown step; undoes bo_paytable_select).",
            "note": "agent_id/currency_id/game_code default to the bo: block in games/{game}/env.config.yml. Resets to the game default paytable, not the value set before the test. Requires BO_TOKEN/BO_REFRESH_TOKEN in .env.{suffix}.",
        },
        "wallet_balance": {
            "required": [],
            "optional": ["currency"],
            "description": "Read the player's Playsite balance via the wallet API. Stored in the step result (use 'save:' to assert it later).",
            "note": "With a 'currency' (or the configured default) the result is the numeric balance for that currency; with none it is the full list of the player's wallets. currency defaults to playsite.currency in games/{game}/env.config.{suffix}.yml (or the PLAYSITE_CURRENCY env var). Requires PLAYSITE_API_URL/PLAYSITE_USER/PLAYSITE_PASS in .env.{suffix}.",
        },
        "wallet_deposit": {
            "required": ["amount"],
            "optional": ["currency"],
            "description": "Deposit funds into the player's wallet via the Playsite API (server-side balance change for deterministic setup).",
            "note": "amount is an int/float/numeric string. currency defaults to playsite.currency in games/{game}/env.config.{suffix}.yml (or PLAYSITE_CURRENCY); a step param overrides it. Result is the updated wallet {currency, balance} (save:-able). Requires PLAYSITE_API_URL/PLAYSITE_USER/PLAYSITE_PASS in .env.{suffix}.",
        },
        "wallet_withdraw": {
            "required": ["amount"],
            "optional": ["currency"],
            "description": "Withdraw funds from the player's wallet via the Playsite API (server-side balance change for deterministic setup).",
            "note": "amount is an int/float/numeric string. currency defaults to playsite.currency in games/{game}/env.config.{suffix}.yml (or PLAYSITE_CURRENCY); a step param overrides it. Result is the updated wallet {currency, balance} (save:-able). Fails if the balance is insufficient. Requires PLAYSITE_API_URL/PLAYSITE_USER/PLAYSITE_PASS in .env.{suffix}.",
        },
        "wallet_set": {
            "required": ["amount"],
            "optional": ["currency"],
            "description": "Set the player's balance to an absolute target by depositing/withdrawing the difference (robust forcing + restore).",
            "note": "amount is the TARGET balance (int/float/numeric string, or a \"$saved\" reference to an earlier wallet_balance result). It reads the current balance and applies only the delta, so it lands on exactly amount whatever the balance was. Use it to force a precise test balance (e.g. below the bet to test a disabled spin) and — paired with a wallet_balance 'save:' in the same test's setup — to restore the original in teardown ('amount: \"$orig_balance\"'). currency defaults to playsite.currency / PLAYSITE_CURRENCY. Requires PLAYSITE_API_URL/PLAYSITE_USER/PLAYSITE_PASS in .env.{suffix}.",
        },
    }

    return action_params


# ---------------------------------------------------------------------------
# Generate YAML reference
# ---------------------------------------------------------------------------

def _generate_yml(action_params: dict) -> str:
    lines = [
        f"# Schema Reference — auto-generated by generate_schema_reference.py",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# DO NOT EDIT — re-run the script after framework changes.",
        f"",
        f"# Source files:",
        f"#   framework/schema_validator.py",
        f"#   framework/runner/step_executor.py",
        f"",
        f"supported_actions:",
    ]

    for action in sorted(SUPPORTED_ACTIONS):
        info = action_params.get(action, {})
        lines.append(f"  {action}:")
        lines.append(f"    description: {json.dumps(info.get('description', ''))}")

        req = info.get("required", [])
        lines.append(f"    required_params: [{', '.join(req)}]")

        opt = info.get("optional", [])
        lines.append(f"    optional_params: [{', '.join(opt)}]")

        defaults = info.get("defaults", {})
        if defaults:
            lines.append(f"    defaults:")
            for k, v in defaults.items():
                lines.append(f"      {k}: {v}")

        note = info.get("note")
        if note:
            lines.append(f"    note: {json.dumps(note)}")

        lines.append("")

    lines.append("ai_modes:")
    for mode in sorted(AI_MODES):
        req_fields = AI_MODE_REQUIRED_FIELDS.get(mode, [])
        lines.append(f"  {mode}:")
        lines.append(f"    required_fields: [{', '.join(req_fields)}]")
    lines.append("  note: \"In a run, an AI assert may use 'expected_by_case' (a {case_id: expected-subset} map) instead of a single 'expected' block — one model call, many case verdicts.\"")
    lines.append("  prompt_substitution: \"'prompt' (and string values in 'expected'/'expected_by_case') may contain {{name}} placeholders, replaced at execution time with the value saved by an earlier step's 'save: name' — for deterministic asserts. A missing variable fails the step. Same for an agent_assert 'check:' claim.\"")
    lines.append("  reference_selection:")
    lines.append("    description: \"Scope which manifest assets prime an AI assert. Settable on the assert.ai AND at test/group level (assert > test > group; the most specific level that sets either field wins entirely). Unset everywhere -> the full pack. Validated at suite load: unknown group/item fails fast.\"")
    lines.append("    fields:")
    lines.append("      - { name: asset_groups, type: list, description: \"Manifest group names (e.g. [symbols, ui_elements, ui]); loads every item in each.\" }")
    lines.append("      - { name: assets, type: list, description: \"Qualified '{group}.{name}' items (e.g. [ui_elements.bet_label, symbols.coin]); loads exactly those.\" }")
    lines.append("      - { name: ref_assets, type: list, description: \"DEPRECATED — bare asset names resolved across all groups (warns). Use asset_groups/assets.\" }")
    lines.append("")

    lines.append("assert_bridge:")
    lines.append("  required_fields: [method, expected]")
    lines.append("  description: \"Call bridge method and compare return value to expected. Equality check only.\"")
    lines.append("")

    lines.append("suite_structure:")
    lines.append("  root_required_fields:")
    lines.append("    - { name: suite, type: str, description: \"Human-readable suite title\" }")
    lines.append("    - { name: game, type: str, description: \"Game identifier\" }")
    lines.append("    - { name: version, type: any, description: \"Schema version\" }")
    lines.append("    - { name: config, type: dict, description: \"Test configuration\" }")
    lines.append("  tests_groups_or_runs:")
    lines.append("    description: \"A suite must define at least one of 'tests', 'groups', or 'runs'.\"")
    lines.append("    fields:")
    lines.append("      - { name: tests, type: list, description: \"Ungrouped test objects (run first, no group setup/teardown)\" }")
    lines.append("      - { name: groups, type: list, description: \"Groups of tests; each group's setup runs once before and teardown once after its tests\" }")
    lines.append("      - { name: runs, type: list, description: \"Run objects (one execution -> many case verdicts); peer to tests/groups\" }")
    lines.append("  root_optional_fields:")
    lines.append("    - { name: setup, type: list, description: \"Suite-level setup steps (run once before all groups)\" }")
    lines.append("    - { name: teardown, type: list, description: \"Suite-level teardown steps (run once after all groups)\" }")
    lines.append("  group_required_fields:")
    lines.append("    - { name: id, type: str, description: \"Unique group id (used by --group; e.g. base-game)\" }")
    lines.append("  group_tests_or_runs:")
    lines.append("    description: \"A group must define 'tests' and/or 'runs' (at least one).\"")
    lines.append("    fields:")
    lines.append("      - { name: tests, type: list, description: \"Non-empty list of test objects in this group\" }")
    lines.append("      - { name: runs, type: list, description: \"Run objects scoped to this group\" }")
    lines.append("  group_optional_fields:")
    lines.append("    - { name: title, type: str }")
    lines.append("    - { name: setup, type: list, description: \"Group setup — runs once before the group's tests\" }")
    lines.append("    - { name: teardown, type: list, description: \"Group teardown — runs once after the group's tests\" }")
    lines.append("    - { name: asset_groups, type: list, description: \"Default reference scope for this group's AI asserts (see ai_modes.reference_selection)\" }")
    lines.append("    - { name: assets, type: list, description: \"Default '{group}.{name}' reference items for this group's AI asserts\" }")
    lines.append("  run_required_fields:")
    lines.append("    - { name: id, type: str, description: \"Unique run id (unique across all test/run/case ids)\" }")
    lines.append("    - { name: cases, type: list, description: \"Case definitions {id, title, description?} (>=1)\" }")
    lines.append("    - { name: steps, type: list, description: \"Shared procedural steps (non-empty); one execution -> many case verdicts\" }")
    lines.append("  run_optional_fields:")
    lines.append("    - { name: title, type: str }")
    lines.append("    - { name: setup, type: list }")
    lines.append("    - { name: teardown, type: list }")
    lines.append("    - { name: asset_groups, type: list, description: \"Default reference scope for this run's AI asserts (see ai_modes.reference_selection)\" }")
    lines.append("    - { name: assets, type: list, description: \"Default '{group}.{name}' reference items for this run's AI asserts\" }")
    lines.append("  case_fields:")
    lines.append("    - { name: id, type: str, description: \"Unique case id (selectable via --test)\" }")
    lines.append("    - { name: title, type: str }")
    lines.append("    - { name: description, type: str }")
    lines.append("  config_required_fields:")
    lines.append("    - { name: test_timeout_ms, type: int }")
    lines.append("    - { name: suite_timeout_ms, type: int }")
    lines.append("  config_optional_fields:")
    lines.append("    - { name: stop_conditions, type: dict }")
    lines.append("    - { name: detection_method, type: str }")
    lines.append("    - { name: video, type: bool }")
    lines.append("  test_required_fields:")
    lines.append("    - { name: id, type: str }")
    lines.append("    - { name: title, type: str }")
    lines.append("    - { name: steps, type: list, description: \"Non-empty list of step objects\" }")
    lines.append("  test_optional_fields:")
    lines.append("    - { name: setup, type: list }")
    lines.append("    - { name: teardown, type: list }")
    lines.append("    - { name: description, type: str }")
    lines.append("    - { name: asset_groups, type: list, description: \"Default reference scope for this test's AI asserts (see ai_modes.reference_selection)\" }")
    lines.append("    - { name: assets, type: list, description: \"Default '{group}.{name}' reference items for this test's AI asserts\" }")
    lines.append("  step_required_fields:")
    lines.append("    - { name: id, type: str, description: \"Every step must have an id\" }")
    lines.append("  step_conditional_fields:")
    lines.append("    - { name: action, type: str, description: \"Required unless step has assert block\" }")
    lines.append("    - { name: assert, type: dict, description: \"Required if action is assert or omitted\" }")
    lines.append("  step_optional_fields:")
    lines.append("    - { name: params, type: dict, description: \"Action parameters\" }")
    lines.append("    - { name: description, type: str }")
    lines.append("    - { name: save, type: str, description: \"Store this step's result into vars[<name>] for a later bridge_assert\" }")
    lines.append("    - { name: case, type: \"str|list\", description: \"In a run, tag this assert step to a case id or list of case ids\" }")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generate Markdown reference
# ---------------------------------------------------------------------------

def _generate_md(action_params: dict) -> str:
    lines = [
        f"# Schema Reference",
        f"",
        f"Auto-generated by `scripts/generate_schema_reference.py` on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
        f"",
        f"**DO NOT EDIT** — re-run the script after any change to `schema_validator.py` or `step_executor.py`.",
        f"",
        f"---",
        f"",
        f"## Supported Action Types ({len(SUPPORTED_ACTIONS)} total)",
        f"",
        f"| Action | Required Params | Optional Params | Description |",
        f"|--------|----------------|-----------------|-------------|",
    ]

    for action in sorted(SUPPORTED_ACTIONS):
        info = action_params.get(action, {})
        req = ", ".join(info.get("required", [])) or "none"
        opt = ", ".join(info.get("optional", [])) or "none"
        desc = info.get("description", "")
        lines.append(f"| `{action}` | {req} | {opt} | {desc} |")

    lines.append("")
    lines.append("### Action Notes")
    lines.append("")
    for action in sorted(SUPPORTED_ACTIONS):
        info = action_params.get(action, {})
        note = info.get("note")
        defaults = info.get("defaults")
        if note or defaults:
            lines.append(f"**`{action}`:**")
            if note:
                lines.append(f"- {note}")
            if defaults:
                for k, v in defaults.items():
                    lines.append(f"- Default `{k}`: {v}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Assert Block")
    lines.append("")
    lines.append("An `assert` block must contain at least `bridge` or `ai` (or both).")
    lines.append("")
    lines.append("### assert.bridge")
    lines.append("")
    lines.append("| Field | Type | Required | Description |")
    lines.append("|-------|------|----------|-------------|")
    lines.append("| `method` | string | Yes | Bridge getter method name |")
    lines.append("| `expected` | any | Yes | Expected return value (equality check) |")
    lines.append("")
    lines.append("**No other fields are supported.** Do not use `expected_type`, `expected_min_count`, etc.")
    lines.append("")
    lines.append("### assert.ai")
    lines.append("")
    lines.append(f"Supported modes: {', '.join(sorted(AI_MODES))}")
    lines.append("")

    for mode in sorted(AI_MODES):
        req_fields = AI_MODE_REQUIRED_FIELDS.get(mode, [])
        lines.append(f"**mode: {mode}** — required fields: {', '.join(f'`{f}`' for f in req_fields)}")
        lines.append("")

    lines.append("**Saved-value substitution (deterministic asserts).** The `prompt` (and any")
    lines.append("string value in `expected` / `expected_by_case`) may contain `{{name}}`")
    lines.append("placeholders. At step-execution time each is replaced with the value an earlier")
    lines.append("step captured via `save: name`, so the AI confirms a known value instead of")
    lines.append("judging a fuzzy one. A `{{name}}` whose variable was never saved fails the step.")
    lines.append("Same mechanism applies to the `check:` claim of an `agent_assert` step.")
    lines.append("")
    lines.append("```yaml")
    lines.append("- id: step-01")
    lines.append("  action: bridge_action")
    lines.append("  params: { name: getDisplayBet }")
    lines.append("  save: bet")
    lines.append("- id: step-02")
    lines.append("  action: capture")
    lines.append("- id: step-03")
    lines.append("  assert:")
    lines.append("    ai:")
    lines.append("      mode: image")
    lines.append("      ref_screenshot: step-02")
    lines.append("      prompt: \"Does the bet label show {{bet}}?\"   # -> '... show 1.00?'")
    lines.append("      output_schema: { matches: { type: boolean } }")
    lines.append("      expected: { matches: true }")
    lines.append("```")
    lines.append("")

    lines.append("### Reference selection (`asset_groups` / `assets`)")
    lines.append("")
    lines.append("Scope which manifest assets prime an AI assert instead of sending the full pack.")
    lines.append("Settable on the `assert.ai` block AND at **test** / **group** level as an inherited")
    lines.append("default. Precedence: **assert > test > group** — the most specific level that sets")
    lines.append("either field wins entirely (no merge). Unset everywhere -> the full reference pack")
    lines.append("(backward compatible). References are validated at suite load: an unknown group or")
    lines.append("`{group}.{name}` item fails before any test runs.")
    lines.append("")
    lines.append("| Field | Type | Description |")
    lines.append("|-------|------|-------------|")
    lines.append("| `asset_groups` | list | Manifest group names (e.g. `[symbols, ui_elements, ui]`); loads every item in each, in section order. |")
    lines.append("| `assets` | list | Qualified `{group}.{name}` items (e.g. `[ui_elements.bet_label, symbols.coin]`); loads exactly those. |")
    lines.append("| `ref_assets` | list | **Deprecated** alias — bare names resolved across all groups (warns). Migrate to `asset_groups`/`assets`. |")
    lines.append("")
    lines.append("```yaml")
    lines.append("- id: step-03")
    lines.append("  assert:")
    lines.append("    ai:")
    lines.append("      mode: image")
    lines.append("      ref_screenshot: step-02")
    lines.append("      assets: [ui_elements.bet_label]   # only the bet label primes the model")
    lines.append("      prompt: \"Is the bet label readable and non-zero?\"")
    lines.append("      output_schema: { ok: { type: boolean } }")
    lines.append("      expected: { ok: true }")
    lines.append("```")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Suite File Structure")
    lines.append("")
    lines.append("### Root-level keys")
    lines.append("")
    lines.append("| Key | Type | Required | Description |")
    lines.append("|-----|------|----------|-------------|")
    lines.append("| `suite` | string | Yes | Human-readable suite title (NOT a dict) |")
    lines.append("| `game` | string | Yes | Game identifier |")
    lines.append("| `version` | any | Yes | Schema version |")
    lines.append("| `config` | dict | Yes | Test configuration |")
    lines.append("| `tests` | list | Yes* | Ungrouped test objects. *Required only if `groups` is absent — a suite needs `tests` and/or `groups`. |")
    lines.append("| `groups` | list | Yes* | Test groups (see below). *Required only if `tests` is absent. |")
    lines.append("| `setup` | list | No | Suite-level setup steps (run once before all groups) |")
    lines.append("| `teardown` | list | No | Suite-level teardown steps (run once after all groups) |")
    lines.append("")
    lines.append("### Group object (`groups[]`)")
    lines.append("")
    lines.append("A group clusters tests that share an expensive precondition (e.g. the same")
    lines.append("back-office paytable). Its `setup` runs **once** before the group's tests and its")
    lines.append("`teardown` runs **once** after — not per test. Running a single test via `--test`")
    lines.append("still runs its group's setup/teardown.")
    lines.append("")
    lines.append("| Key | Type | Required |")
    lines.append("|-----|------|----------|")
    lines.append("| `id` | string | Yes (unique; used by `--group`) |")
    lines.append("| `tests` | list | Yes (non-empty) |")
    lines.append("| `title` | string | No |")
    lines.append("| `setup` | list | No (runs once before the group's tests) |")
    lines.append("| `teardown` | list | No (runs once after the group's tests) |")
    lines.append("")
    lines.append("Test ids must be **unique across the whole suite** (so `--test <id>` is unambiguous).")
    lines.append("")
    lines.append("### Config block")
    lines.append("")
    lines.append("| Key | Type | Required |")
    lines.append("|-----|------|----------|")
    lines.append("| `test_timeout_ms` | int | Yes |")
    lines.append("| `suite_timeout_ms` | int | Yes |")
    lines.append("| `stop_conditions` | dict | No |")
    lines.append("| `detection_method` | string | No |")
    lines.append("| `video` | bool | No |")
    lines.append("")
    lines.append("### Test object")
    lines.append("")
    lines.append("| Key | Type | Required |")
    lines.append("|-----|------|----------|")
    lines.append("| `id` | string | Yes |")
    lines.append("| `title` | string | Yes |")
    lines.append("| `steps` | list | Yes (non-empty) |")
    lines.append("| `setup` | list | No |")
    lines.append("| `teardown` | list | No |")
    lines.append("| `description` | string | No |")
    lines.append("")
    lines.append("### Step object")
    lines.append("")
    lines.append("| Key | Type | Required |")
    lines.append("|-----|------|----------|")
    lines.append("| `id` | string | Yes (always) |")
    lines.append("| `action` | string | Yes (unless step has `assert` block) |")
    lines.append("| `params` | dict | Depends on action |")
    lines.append("| `assert` | dict | Required if action is `assert` or omitted |")
    lines.append("| `description` | string | No |")
    lines.append("| `capture` | bool | No (take screenshot after step) |")
    lines.append("| `save` | string | No (store this step's result into `vars[<name>]` for a later `bridge_assert`) |")
    lines.append("")
    lines.append("### Saving and asserting variables")
    lines.append("")
    lines.append("Add `save: <name>` to any result-producing step (typically `bridge_action`)")
    lines.append("to capture its result into a per-test variable. Assert on it later with")
    lines.append("`bridge_assert`: its `args` list passes arguments to a bridge function, where")
    lines.append("a string `\"$name\"` resolves to a saved variable (any position), and a string")
    lines.append("containing `{{name}}` interpolates saved values into a larger string —")
    lines.append("`\"bet:row:{{targetRow}}\"` -> `\"bet:row:3\"`. Use `\"$name\"` to pass a value")
    lines.append("(type preserved), `{{name}}` to build one. The same `{{name}}` works in")
    lines.append("`click_button params.name` and in AI prompts. The function's")
    lines.append("return is compared to `expected`. The assertion logic lives in the game")
    lines.append("bridge, so it is deterministic. Variables are scoped to a single test")
    lines.append("(setup → steps → teardown).")
    lines.append("")
    lines.append("```yaml")
    lines.append("- id: step-05")
    lines.append("  action: bridge_action")
    lines.append("  params: { name: getLastPayout }")
    lines.append("  save: payout               # vars['payout'] = result")
    lines.append("- id: step-06")
    lines.append("  action: bridge_assert")
    lines.append("  params:")
    lines.append("    method: inRange          # bridge.inRange(<payout>, 0, 100)")
    lines.append("    args: [ \"$payout\", 0, 100 ]")
    lines.append("    expected: true")
    lines.append("```")
    lines.append("")
    lines.append("### Capturing an event stream and asserting it with a bridge analyser")
    lines.append("")
    lines.append("`bridge_action` with `args` calls the bridge JS method directly and keeps")
    lines.append("its return value, so methods that take an options object/array or have no")
    lines.append("Python wrapper (event capture, bonus analysers) are driven deterministically")
    lines.append("— no `script`. Combine with a `bridge_assert` `path` to assert one field of an")
    lines.append("object-returning analyser:")
    lines.append("")
    lines.append("```yaml")
    lines.append("- id: step-01                         # start an independent capture buffer")
    lines.append("  action: bridge_action")
    lines.append("  params: { name: startEventCapture, args: [ \"bonus\" ] }")
    lines.append("- id: step-02                         # blocks through the whole bonus")
    lines.append("  action: bridge_action")
    lines.append("  params: { name: spin, args: [ 120000 ] }")
    lines.append("- id: step-03                         # returns the captured CollectedEvent[]")
    lines.append("  action: bridge_action")
    lines.append("  params: { name: endEventCapture, args: [ \"bonus\" ] }")
    lines.append("  save: raw")
    lines.append("- id: step-04                         # slice the START_BONUS..END_BONUS episode")
    lines.append("  action: bridge_action")
    lines.append("  params: { name: filterBonusEvent, args: [ \"$raw\" ] }")
    lines.append("  save: events")
    lines.append("- id: step-05                         # analyser returns {valid, ...}; assert .valid")
    lines.append("  action: bridge_assert")
    lines.append("  params:")
    lines.append("    method: verifyHoldSpinCoinsHeld   # bridge.verifyHoldSpinCoinsHeld(events).valid")
    lines.append("    args: [ \"$events\" ]")
    lines.append("    path: valid")
    lines.append("    expected: true")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Runs — one execution, many test-case verdicts")
    lines.append("")
    lines.append("A `runs:` entry (top-level, peer to `tests:`/`groups:`, or inside a group)")
    lines.append("executes ONE procedural sequence (setup → steps → teardown) and produces a")
    lines.append("verdict for each of several named **cases**. Use it when multiple test cases")
    lines.append("share the same expensive execution (e.g. enter a free-spin bonus once, then")
    lines.append("assert several things about it) instead of replaying it per case.")
    lines.append("")
    lines.append("| Key | Type | Required | Description |")
    lines.append("|-----|------|----------|-------------|")
    lines.append("| `id` | string | Yes | Unique run id (unique across all test/run/case ids) |")
    lines.append("| `title` | string | No | Human-readable run title |")
    lines.append("| `cases` | list | Yes | Case definitions `{id, title, description?}` (≥1) |")
    lines.append("| `setup` | list | No | Run-level setup steps |")
    lines.append("| `steps` | list | Yes | The procedural steps (non-empty) |")
    lines.append("| `teardown` | list | No | Run-level teardown steps |")
    lines.append("")
    lines.append("**Tagging asserts to cases.** An assert-bearing step (an `assert` block, or")
    lines.append("`action: bridge_assert`) carries a `case:` tag — a case id or a list of ids")
    lines.append("(a shared analyser can feed several cases). A case's bridge verdict is the AND")
    lines.append("of its tagged non-AI asserts. A plain (untagged) step's failure is fatal — it")
    lines.append("means the run could not produce the data, so all cases go red.")
    lines.append("")
    lines.append("**AI output partitioned per case (`expected_by_case`).** A single AI assert")
    lines.append("(one trim + one model call) returns a multi-field output; `expected_by_case`")
    lines.append("maps each case id to the subset of fields it is judged against — one call,")
    lines.append("many case verdicts. Use `expected_by_case` instead of `expected`+`case` on the")
    lines.append("AI block (not both). Every declared case must be covered by a `case:` tag or")
    lines.append("an `expected_by_case` key.")
    lines.append("")
    lines.append("Each case becomes its own verdict line in `summary.json`, all sharing the")
    lines.append("run's single video/timeline/events (`run_id` links them). `--test <case-id>`")
    lines.append("runs the whole run but reports that case.")
    lines.append("")
    lines.append("```yaml")
    lines.append("runs:")
    lines.append("  - id: fs-3coin-countdown-run")
    lines.append("    title: \"One bonus, assert ZZ-30/32/33/35\"")
    lines.append("    cases:")
    lines.append("      - { id: ZZ-30, title: \"Trigger FS 3 coins -> 3 free spins\" }")
    lines.append("      - { id: ZZ-32, title: \"Coins held fixed across re-spins\" }")
    lines.append("      - { id: ZZ-33, title: \"Only non-coin cells re-spin\" }")
    lines.append("    setup: [ ... ]")
    lines.append("    steps:")
    lines.append("      - id: step-01                       # plain step (no case)")
    lines.append("        action: bridge_action")
    lines.append("        params: { name: spinUntilBonus }")
    lines.append("      - id: step-02                       # one case")
    lines.append("        case: ZZ-30")
    lines.append("        assert: { bridge: { method: getFreeSpinsRemaining, expected: 3 } }")
    lines.append("      - id: step-03                       # shared analyser -> two cases")
    lines.append("        case: [ZZ-32, ZZ-33]")
    lines.append("        action: bridge_assert")
    lines.append("        params: { method: verifyHoldSpinCoinsHeld, args: [\"$events\"], path: valid, expected: true }")
    lines.append("      - id: step-10                       # ONE video AI call -> per-case verdicts")
    lines.append("        assert:")
    lines.append("          ai:")
    lines.append("            mode: video")
    lines.append("            video_trim: { from_step: step-01, to_step: step-09 }")
    lines.append("            output_schema: { coins_held: {type: boolean}, only_noncoin_cells_spin: {type: boolean}, confidence: {type: string} }")
    lines.append("            expected_by_case:")
    lines.append("              ZZ-32: { coins_held: true, confidence: high }")
    lines.append("              ZZ-33: { only_noncoin_cells_spin: true, confidence: high }")
    lines.append("    teardown: [ ... ]")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    action_params = _extract_action_params()

    out_dir = _REPO_ROOT / "framework"

    yml_path = out_dir / "schema_reference.yml"
    yml_content = _generate_yml(action_params)
    yml_path.write_text(yml_content, encoding="utf-8")
    print(f"Written: {yml_path}")

    md_path = out_dir / "schema_reference.md"
    md_content = _generate_md(action_params)
    md_path.write_text(md_content, encoding="utf-8")
    print(f"Written: {md_path}")


if __name__ == "__main__":
    main()
