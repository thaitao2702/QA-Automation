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
        "wait_ms",
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


def _validate_assert_ai(ai: Any, path: str, collector: _Collector) -> None:
    """Validate the assert.ai sub-block."""
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

    # Validate mode-specific required fields
    for req_field in AI_MODE_REQUIRED_FIELDS[mode]:
        _require_field(ai, req_field, path, collector)


def _validate_assert_bridge(bridge: Any, path: str, collector: _Collector) -> None:
    """Validate the assert.bridge sub-block."""
    if not isinstance(bridge, dict):
        collector.add(path, f"assert.bridge must be a mapping, got {type(bridge).__name__}")
        return

    _require_field(bridge, "method", path, collector, str)
    _require_field(bridge, "expected", path, collector)


def _validate_assert_block(assert_block: Any, path: str, collector: _Collector) -> None:
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
        _validate_assert_ai(assert_block["ai"], f"{path}.ai", collector)


def _validate_step(step: Any, path: str, collector: _Collector) -> None:
    """Validate a single step object."""
    if not isinstance(step, dict):
        collector.add(path, f"step must be a mapping, got {type(step).__name__}")
        return

    # id is required
    _require_field(step, "id", path, collector, str)

    # Every step must have an action (except pure assert steps which may omit it)
    if "assert" in step:
        # This step is an assertion step; action field is optional
        _validate_assert_block(step["assert"], f"{path}.assert", collector)
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

    # Tests array is required
    if not _require_field(doc, "tests", "$", collector, list):
        return collector.errors()

    tests = doc["tests"]
    if len(tests) == 0:
        collector.add("$.tests", "'tests' list must not be empty")
        return collector.errors()

    for idx, test in enumerate(tests):
        _validate_test(test, f"$.tests[{idx}]", collector)

    return collector.errors()
