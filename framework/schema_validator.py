"""
YAML test suite schema validator for the QA platform.

Validates a parsed test suite YAML document against the expected schema.
Returns a list of ValidationError objects so callers can report all issues
in one pass rather than failing on the first.

Public interface
----------------
validate_suite(doc: dict, source: str = "<unknown>") -> list[ValidationError]
    Validate a parsed YAML document. Returns [] on success.

ValidationError
    Dataclass with fields: path (str), message (str), source (str).

Usage example
-------------
    import yaml
    from framework.schema_validator import validate_suite

    with open("games/zeus/test-suites/suite-01-core.yml") as fh:
        doc = yaml.safe_load(fh)

    errors = validate_suite(doc, source="suite-01-core.yml")
    for err in errors:
        logger.error("%s", err)  # str(err) -> "[source] path: message"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported action type values (Section 3 of qa-platform-plan.md)
# ---------------------------------------------------------------------------
SUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {
        "navigate",
        "wait_for_state",
        "bridge_action",
        "bridge_assert",
        "capture",
        "assert",
        "script",
        "go_offline",
        "go_online",
        "press_key",
        "click",
        "agent_action",
        "agent_assert",
        "wait_ms",
        "bo_paytable_ensure",
        "bo_paytable_select",
        "bo_paytable_update",
        "bo_paytable_reset",
    }
)

# AI assert modes
AI_MODES: frozenset[str] = frozenset({"image", "video"})

# Fields required on the assert.ai block for each mode
AI_MODE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "image": ["ref_screenshot", "prompt", "output_schema", "expected"],
    "video": ["video_trim", "prompt", "output_schema", "expected"],
}


# ---------------------------------------------------------------------------
# ValidationError
# ---------------------------------------------------------------------------


@dataclass
class ValidationError:
    """A single schema violation found during validation.

    Attributes:
        path:    Dot-notation path in the document (e.g. "tests[0].steps[1].action").
        message: Human-readable description of the violation.
        source:  File name or label passed to validate_suite() for traceability.
    """

    path: str
    message: str
    source: str = "<unknown>"

    def __str__(self) -> str:
        return f"[{self.source}] {self.path}: {self.message}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Collector:
    """Accumulates ValidationError objects during a validation pass."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._errors: list[ValidationError] = []

    def add(self, path: str, message: str) -> None:
        self._errors.append(ValidationError(path=path, message=message, source=self._source))

    def errors(self) -> list[ValidationError]:
        return list(self._errors)


def _require_field(
    obj: dict,
    field_name: str,
    path: str,
    collector: _Collector,
    expected_type: type | None = None,
) -> bool:
    """Check that field_name exists in obj and optionally has the expected type.

    Returns True if the field is present (and type-correct when expected_type is
    given), so callers can skip deeper validation when a parent field is absent.
    """
    if field_name not in obj:
        collector.add(path, f"required field '{field_name}' is missing")
        return False

    value = obj[field_name]
    if expected_type is not None and not isinstance(value, expected_type):
        collector.add(
            f"{path}.{field_name}",
            f"expected {expected_type.__name__}, got {type(value).__name__}",
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Step validators
# ---------------------------------------------------------------------------


def _validate_assert_ai(
    ai: Any,
    path: str,
    collector: _Collector,
    run_case_ids: set[str] | None = None,
) -> None:
    """Validate the assert.ai sub-block.

    In a run (``run_case_ids`` not None) an AI assert may bind its output to cases
    with ``expected_by_case`` instead of a single ``expected`` block.
    """
    if not isinstance(ai, dict):
        collector.add(path, f"assert.ai must be a mapping, got {type(ai).__name__}")
        return

    # mode is required and must be a known value
    if not _require_field(ai, "mode", path, collector, str):
        return

    mode = ai["mode"]
    if mode not in AI_MODES:
        collector.add(
            f"{path}.mode",
            f"unsupported AI mode '{mode}'; supported modes: {', '.join(sorted(AI_MODES))}",
        )
        return

    # In a run, `expected_by_case` substitutes for the single `expected` block.
    uses_expected_by_case = run_case_ids is not None and "expected_by_case" in ai

    # Validate mode-specific required fields
    for req_field in AI_MODE_REQUIRED_FIELDS[mode]:
        if req_field == "expected" and uses_expected_by_case:
            continue
        _require_field(ai, req_field, path, collector)

    # expected_by_case: runs-only, partitions the AI output across cases
    if "expected_by_case" in ai:
        if run_case_ids is None:
            collector.add(
                f"{path}.expected_by_case",
                "'expected_by_case' is only valid in a run's assert (use 'expected' in tests)",
            )
        else:
            _validate_expected_by_case(ai, path, collector, run_case_ids)


def _validate_expected_by_case(
    ai: dict, path: str, collector: _Collector, run_case_ids: set[str]
) -> None:
    """Validate ``assert.ai.expected_by_case`` — a {case_id: expected subset} mapping."""
    ebc = ai["expected_by_case"]
    p = f"{path}.expected_by_case"
    if not isinstance(ebc, dict):
        collector.add(p, f"expected_by_case must be a mapping, got {type(ebc).__name__}")
        return
    if "expected" in ai:
        collector.add(p, "use either 'expected' or 'expected_by_case', not both")
    if not ebc:
        collector.add(p, "expected_by_case must not be empty")
    output_schema = ai.get("output_schema")
    schema_keys = set(output_schema) if isinstance(output_schema, dict) else set()
    for cid, subset in ebc.items():
        if cid not in run_case_ids:
            collector.add(f"{p}.{cid}", f"case '{cid}' is not declared in this run's cases")
        if not isinstance(subset, dict):
            collector.add(f"{p}.{cid}", f"expected subset must be a mapping, got {type(subset).__name__}")
            continue
        for key in subset:
            if schema_keys and key not in schema_keys:
                collector.add(f"{p}.{cid}.{key}", f"field '{key}' is not in output_schema")


def _validate_assert_bridge(bridge: Any, path: str, collector: _Collector) -> None:
    """Validate the assert.bridge sub-block."""
    if not isinstance(bridge, dict):
        collector.add(path, f"assert.bridge must be a mapping, got {type(bridge).__name__}")
        return

    _require_field(bridge, "method", path, collector, str)
    _require_field(bridge, "expected", path, collector)


def _validate_assert_block(
    assert_block: Any,
    path: str,
    collector: _Collector,
    run_case_ids: set[str] | None = None,
) -> None:
    """Validate a top-level assert block (requires at least bridge or ai)."""
    if not isinstance(assert_block, dict):
        collector.add(path, f"assert block must be a mapping, got {type(assert_block).__name__}")
        return

    has_bridge = "bridge" in assert_block
    has_ai = "ai" in assert_block

    if not has_bridge and not has_ai:
        collector.add(path, "assert block must contain at least 'bridge' or 'ai'")
        return

    if has_bridge:
        _validate_assert_bridge(assert_block["bridge"], f"{path}.bridge", collector)

    if has_ai:
        _validate_assert_ai(assert_block["ai"], f"{path}.ai", collector, run_case_ids)


def _validate_step(
    step: Any,
    path: str,
    collector: _Collector,
    run_case_ids: set[str] | None = None,
) -> None:
    """Validate a single step object.

    When ``run_case_ids`` is provided the step belongs to a run, so an optional
    ``case`` tag (str | list[str]) is validated against the run's declared cases.
    """
    if not isinstance(step, dict):
        collector.add(path, f"step must be a mapping, got {type(step).__name__}")
        return

    # id is required
    _require_field(step, "id", path, collector, str)

    # Optional 'save': capture this step's result into a named variable.
    if "save" in step and not isinstance(step["save"], str):
        collector.add(f"{path}.save", "expected str (variable name) for 'save'")

    # Every step must have an action, an assert block, or a human-readable
    # sugar field (do: -> agent_action, check: -> agent_assert).
    if "assert" in step:
        # This step is an assertion step; action field is optional
        _validate_assert_block(step["assert"], f"{path}.assert", collector, run_case_ids)
    elif "do" in step and "action" not in step:
        if not isinstance(step["do"], str):
            collector.add(f"{path}.do", "expected str (a natural-language action for 'do:')")
    elif "check" in step and "action" not in step:
        if not isinstance(step["check"], str):
            collector.add(f"{path}.check", "expected str (a natural-language claim for 'check:')")
    else:
        if not _require_field(step, "action", path, collector, str):
            return

        action = step["action"]
        if action not in SUPPORTED_ACTIONS:
            collector.add(
                f"{path}.action",
                f"unsupported action type '{action}'; "
                f"supported types: {', '.join(sorted(SUPPORTED_ACTIONS))}",
            )

        # assert steps embedded in action steps (action: assert with assert block)
        if action == "assert" and "assert" not in step:
            collector.add(path, "step with action 'assert' must have an 'assert' block")

    # Run-only: validate the 'case' tag binding this step to test cases.
    if run_case_ids is not None and "case" in step:
        _validate_step_case_tag(step, path, collector, run_case_ids)


def _validate_step_case_tag(
    step: dict, path: str, collector: _Collector, run_case_ids: set[str]
) -> None:
    """Validate a run step's ``case`` tag (a case id or list of ids)."""
    case_val = step["case"]
    if isinstance(case_val, str):
        case_list = [case_val]
    elif isinstance(case_val, list) and all(isinstance(c, str) for c in case_val):
        case_list = case_val
    else:
        collector.add(f"{path}.case", "'case' must be a string or list of strings")
        return

    # Only assert-bearing steps may carry a case tag.
    is_assert = (
        "assert" in step
        or "check" in step
        or step.get("action") in ("assert", "bridge_assert", "agent_assert")
    )
    if not is_assert:
        collector.add(
            f"{path}.case",
            "'case' may only tag assert steps (an 'assert' block, 'check:', or action "
            "'assert'/'bridge_assert'/'agent_assert')",
        )

    for cid in case_list:
        if cid not in run_case_ids:
            collector.add(f"{path}.case", f"case '{cid}' is not declared in this run's cases")


def _validate_steps_list(steps: Any, path: str, collector: _Collector) -> None:
    """Validate a steps array (used in test bodies and setup/teardown blocks)."""
    if not isinstance(steps, list):
        collector.add(path, f"'steps' must be a list, got {type(steps).__name__}")
        return

    if len(steps) == 0:
        collector.add(path, "'steps' must not be empty")
        return

    for idx, step in enumerate(steps):
        _validate_step(step, f"{path}[{idx}]", collector)


def _validate_step_sequence(
    seq: Any, seq_name: str, path: str, collector: _Collector
) -> None:
    """Validate a setup or teardown sequence (list of step-like objects without 'steps' wrapper)."""
    if not isinstance(seq, list):
        collector.add(path, f"'{seq_name}' must be a list, got {type(seq).__name__}")
        return

    for idx, step in enumerate(seq):
        _validate_step(step, f"{path}[{idx}]", collector)


# ---------------------------------------------------------------------------
# Test validator
# ---------------------------------------------------------------------------


def _validate_assets(container: Any, path: str, collector: _Collector) -> None:
    """Validate an optional ``assets:`` field — a list of asset-category names.

    Scopes which categories from the game's assets.yml are loaded (inline, with
    descriptions) into the AI prompts for this test/run/group. Category names are
    not resolved here (unknown names are skipped + logged at load time), mirroring
    how missing asset files are handled.
    """
    if not isinstance(container, dict) or "assets" not in container:
        return
    assets = container["assets"]
    if not isinstance(assets, list) or not all(isinstance(a, str) for a in assets):
        collector.add(f"{path}.assets", "expected a list of asset-category names (strings)")


def _validate_test(test: Any, path: str, collector: _Collector) -> None:
    """Validate a single test entry."""
    if not isinstance(test, dict):
        collector.add(path, f"test entry must be a mapping, got {type(test).__name__}")
        return

    _require_field(test, "id", path, collector, str)
    _require_field(test, "title", path, collector, str)

    if not _require_field(test, "steps", path, collector, list):
        return

    _validate_steps_list(test["steps"], f"{path}.steps", collector)

    # Optional setup / teardown at the test level
    if "setup" in test:
        _validate_step_sequence(test["setup"], "setup", f"{path}.setup", collector)

    if "teardown" in test:
        _validate_step_sequence(test["teardown"], "teardown", f"{path}.teardown", collector)

    _validate_assets(test, path, collector)


# ---------------------------------------------------------------------------
# Run validator (one execution, many test-case verdicts)
# ---------------------------------------------------------------------------


def _validate_case(case: Any, path: str, collector: _Collector) -> str | None:
    """Validate a single case definition; return its id (or None if invalid)."""
    if not isinstance(case, dict):
        collector.add(path, f"case must be a mapping, got {type(case).__name__}")
        return None
    _require_field(case, "id", path, collector, str)
    _require_field(case, "title", path, collector, str)
    if "description" in case and not isinstance(case["description"], str):
        collector.add(f"{path}.description", "expected str")
    return case["id"] if isinstance(case.get("id"), str) else None


def _step_covered_cases(step: Any, case_ids: set[str]) -> set[str]:
    """Return the declared case ids a step asserts (via ``case`` or ``expected_by_case``)."""
    covered: set[str] = set()
    if not isinstance(step, dict):
        return covered
    tag = step.get("case")
    if isinstance(tag, str):
        covered.add(tag)
    elif isinstance(tag, list):
        covered.update(c for c in tag if isinstance(c, str))
    assert_block = step.get("assert")
    if isinstance(assert_block, dict):
        ai = assert_block.get("ai")
        if isinstance(ai, dict) and isinstance(ai.get("expected_by_case"), dict):
            covered.update(c for c in ai["expected_by_case"] if isinstance(c, str))
    return covered & case_ids


def _validate_run(run: Any, path: str, collector: _Collector, all_ids: list[str]) -> None:
    """Validate a single run (one execution producing verdicts for many cases).

    ``all_ids`` accumulates the run id and every case id for suite-wide uniqueness.
    """
    if not isinstance(run, dict):
        collector.add(path, f"run must be a mapping, got {type(run).__name__}")
        return

    _require_field(run, "id", path, collector, str)
    if "title" in run and not isinstance(run["title"], str):
        collector.add(f"{path}.title", "expected str")

    # cases (required, non-empty)
    case_ids: set[str] = set()
    declared_case_ids: list[str] = []
    if _require_field(run, "cases", path, collector, list):
        if len(run["cases"]) == 0:
            collector.add(f"{path}.cases", "run 'cases' must not be empty")
        for idx, case in enumerate(run["cases"]):
            cid = _validate_case(case, f"{path}.cases[{idx}]", collector)
            if cid is not None:
                case_ids.add(cid)
                declared_case_ids.append(cid)
        _check_duplicates(declared_case_ids, "case id", f"{path}.cases", collector)

    # Run id + case ids join the suite-wide namespace (must not collide with tests).
    if isinstance(run.get("id"), str):
        all_ids.append(run["id"])
    all_ids.extend(declared_case_ids)

    # Optional run-level setup / teardown
    if "setup" in run:
        _validate_step_sequence(run["setup"], "setup", f"{path}.setup", collector)
    if "teardown" in run:
        _validate_step_sequence(run["teardown"], "teardown", f"{path}.teardown", collector)

    _validate_assets(run, path, collector)

    # steps (required, non-empty) — validated with run case context
    if not _require_field(run, "steps", path, collector, list):
        return
    if len(run["steps"]) == 0:
        collector.add(f"{path}.steps", "run 'steps' must not be empty")
        return

    covered: set[str] = set()
    for idx, step in enumerate(run["steps"]):
        _validate_step(step, f"{path}.steps[{idx}]", collector, run_case_ids=case_ids)
        covered |= _step_covered_cases(step, case_ids)

    # Every declared case must be asserted by at least one step.
    for cid in declared_case_ids:
        if cid not in covered:
            collector.add(
                path,
                f"case '{cid}' has no asserts tagged to it (use a step 'case:' or 'expected_by_case')",
            )


def _validate_runs_list(
    runs: Any, path: str, collector: _Collector, all_ids: list[str]
) -> None:
    """Validate a 'runs' list (top-level or inside a group)."""
    if not isinstance(runs, list):
        collector.add(path, f"'runs' must be a list, got {type(runs).__name__}")
        return
    if len(runs) == 0:
        collector.add(path, "'runs' list must not be empty")
        return
    for idx, run in enumerate(runs):
        _validate_run(run, f"{path}[{idx}]", collector, all_ids)


# ---------------------------------------------------------------------------
# Group validator
# ---------------------------------------------------------------------------


def _validate_group(
    group: Any, path: str, collector: _Collector, all_ids: list[str]
) -> None:
    """Validate a single test group (a paytable/setup-sharing cluster of tests/runs).

    A group runs its optional ``setup`` once before, and ``teardown`` once after,
    the tests/runs it contains. ``all_ids`` accumulates every test id, run id and
    case id seen for suite-wide duplicate detection.
    """
    if not isinstance(group, dict):
        collector.add(path, f"group must be a mapping, got {type(group).__name__}")
        return

    _require_field(group, "id", path, collector, str)

    if "title" in group and not isinstance(group["title"], str):
        collector.add(f"{path}.title", "expected str")

    # Optional group-level setup / teardown (same shape as suite/test sequences)
    if "setup" in group:
        _validate_step_sequence(group["setup"], "setup", f"{path}.setup", collector)
    if "teardown" in group:
        _validate_step_sequence(group["teardown"], "teardown", f"{path}.teardown", collector)

    _validate_assets(group, path, collector)

    has_tests = "tests" in group
    has_runs = "runs" in group
    if not has_tests and not has_runs:
        collector.add(path, "group must define 'tests' and/or 'runs'")
        return

    if has_tests:
        if not isinstance(group["tests"], list):
            collector.add(f"{path}.tests", f"group 'tests' must be a list, got {type(group['tests']).__name__}")
        elif len(group["tests"]) == 0:
            collector.add(f"{path}.tests", "group 'tests' must not be empty")
        else:
            for idx, test in enumerate(group["tests"]):
                _validate_test(test, f"{path}.tests[{idx}]", collector)
                if isinstance(test, dict) and isinstance(test.get("id"), str):
                    all_ids.append(test["id"])

    if has_runs:
        _validate_runs_list(group["runs"], f"{path}.runs", collector, all_ids)


def _check_duplicates(
    ids: list[str], label: str, path: str, collector: _Collector
) -> None:
    """Flag any id that appears more than once (test ids must be unique to be runnable via --test)."""
    seen: set[str] = set()
    dups: set[str] = set()
    for i in ids:
        if i in seen:
            dups.add(i)
        seen.add(i)
    for d in sorted(dups):
        collector.add(path, f"duplicate {label}: {d!r}")


# ---------------------------------------------------------------------------
# Config block validator
# ---------------------------------------------------------------------------


def _validate_config(config: Any, path: str, collector: _Collector) -> None:
    """Validate the suite-level config block."""
    if not isinstance(config, dict):
        collector.add(path, f"config block must be a mapping, got {type(config).__name__}")
        return

    _require_field(config, "test_timeout_ms", path, collector, int)
    _require_field(config, "suite_timeout_ms", path, collector, int)

    if "stop_conditions" in config:
        sc = config["stop_conditions"]
        sc_path = f"{path}.stop_conditions"
        if not isinstance(sc, dict):
            collector.add(sc_path, f"stop_conditions must be a mapping, got {type(sc).__name__}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_suite(doc: Any, source: str = "<unknown>") -> list[ValidationError]:
    """Validate a parsed test suite YAML document.

    Args:
        doc:    The result of ``yaml.safe_load()`` on a test suite file.
        source: A label (typically the file name) included in each error for
                context. Defaults to ``"<unknown>"``.

    Returns:
        A list of ValidationError objects. An empty list means the document
        passed all checks. Errors are ordered as encountered (top-down).

    The function never raises; all problems are returned as ValidationError
    objects so callers can handle them all at once.
    """
    collector = _Collector(source)

    if not isinstance(doc, dict):
        collector.add("$", f"suite document must be a YAML mapping, got {type(doc).__name__}")
        return collector.errors()

    # Suite-level required fields
    _require_field(doc, "suite", "$", collector, str)
    _require_field(doc, "game", "$", collector, str)
    _require_field(doc, "version", "$", collector)

    # Config block is required
    if _require_field(doc, "config", "$", collector, dict):
        _validate_config(doc["config"], "$.config", collector)

    # Optional suite-level setup / teardown
    if "setup" in doc:
        _validate_step_sequence(doc["setup"], "setup", "$.setup", collector)

    if "teardown" in doc:
        _validate_step_sequence(doc["teardown"], "teardown", "$.teardown", collector)

    # A suite must define at least one of 'tests', 'groups', or 'runs'.
    has_tests = "tests" in doc
    has_groups = "groups" in doc
    has_runs = "runs" in doc
    if not has_tests and not has_groups and not has_runs:
        collector.add("$", "suite must define 'tests', 'groups', and/or 'runs'")
        return collector.errors()

    # Shared id namespace: test ids + run ids + case ids must all be unique
    # suite-wide so --test can select one unambiguously.
    all_ids: list[str] = []

    # Top-level (ungrouped) tests
    if has_tests:
        if not isinstance(doc["tests"], list):
            collector.add("$.tests", f"'tests' must be a list, got {type(doc['tests']).__name__}")
        elif len(doc["tests"]) == 0 and not has_groups and not has_runs:
            collector.add("$.tests", "'tests' list must not be empty")
        else:
            for idx, test in enumerate(doc["tests"]):
                _validate_test(test, f"$.tests[{idx}]", collector)
                if isinstance(test, dict) and isinstance(test.get("id"), str):
                    all_ids.append(test["id"])

    # Top-level (ungrouped) runs
    if has_runs:
        _validate_runs_list(doc["runs"], "$.runs", collector, all_ids)

    # Groups (each with optional setup/teardown around its tests/runs)
    if has_groups:
        groups = doc["groups"]
        if not isinstance(groups, list):
            collector.add("$.groups", f"'groups' must be a list, got {type(groups).__name__}")
        elif len(groups) == 0:
            collector.add("$.groups", "'groups' list must not be empty")
        else:
            group_ids: list[str] = []
            for idx, group in enumerate(groups):
                _validate_group(group, f"$.groups[{idx}]", collector, all_ids)
                if isinstance(group, dict) and isinstance(group.get("id"), str):
                    group_ids.append(group["id"])
            _check_duplicates(group_ids, "group id", "$.groups", collector)

    # Every test/run/case id must be unique suite-wide.
    _check_duplicates(all_ids, "id", "$", collector)

    return collector.errors()
