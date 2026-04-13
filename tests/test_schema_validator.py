"""
Tests for framework/schema_validator.py

Acceptance criteria:
- A well-formed test suite YAML passes with zero errors.
- A YAML missing required fields produces specific errors identifying
  the missing field and its document path.
- A YAML with an unsupported action type produces an error naming the
  invalid action and its location.
- Assert blocks with AI mode specified but missing required AI fields
  produce validation errors.
"""

import pytest

from framework.schema_validator import ValidationError, validate_suite


# ---------------------------------------------------------------------------
# Fixtures — valid documents
# ---------------------------------------------------------------------------


def _minimal_valid_suite() -> dict:
    """Return the smallest document that passes all validation rules."""
    return {
        "suite": "Core Mechanics",
        "game": "zeus",
        "version": "1.0",
        "config": {
            "detection_method": "both",
            "video": True,
            "test_timeout_ms": 60000,
            "suite_timeout_ms": 600000,
            "stop_conditions": {
                "fail_fast": False,
                "max_consecutive_failures": 3,
                "max_total_failures": 10,
            },
        },
        "tests": [
            {
                "id": "ZZ-01",
                "title": "Normal spin completes",
                "steps": [
                    {"id": "step-01", "action": "bridge_action", "params": {"name": "spin"}},
                    {
                        "id": "step-02",
                        "action": "wait_for_state",
                        "params": {"state": "idle", "timeout_ms": 10000},
                    },
                ],
            }
        ],
    }


def _suite_with_image_assert() -> dict:
    doc = _minimal_valid_suite()
    doc["tests"][0]["steps"].append(
        {
            "id": "step-03",
            "assert": {
                "bridge": {"method": "getBalance", "expected": 100},
                "ai": {
                    "mode": "image",
                    "ref_screenshot": "step-02",
                    "prompt": "What is the balance shown?",
                    "output_schema": {"balance": {"type": "number"}},
                    "expected": {"balance": 100},
                },
            },
        }
    )
    return doc


def _suite_with_video_assert() -> dict:
    doc = _minimal_valid_suite()
    doc["tests"][0]["steps"].append(
        {
            "id": "step-03",
            "assert": {
                "bridge": {"method": "getAppState", "expected": "idle"},
                "ai": {
                    "mode": "video",
                    "video_trim": {"from_step": "step-01", "to_step": "step-02"},
                    "prompt": "Did the reels spin and stop?",
                    "output_schema": {"reels_spun": {"type": "boolean"}},
                    "expected": {"reels_spun": True},
                },
            },
        }
    )
    return doc


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _error_paths(errors: list[ValidationError]) -> list[str]:
    return [e.path for e in errors]


def _error_messages(errors: list[ValidationError]) -> list[str]:
    return [e.message for e in errors]


# ---------------------------------------------------------------------------
# Valid document tests
# ---------------------------------------------------------------------------


class TestValidSuites:
    def test_minimal_valid_suite_passes(self) -> None:
        errors = validate_suite(_minimal_valid_suite(), source="minimal.yml")
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_suite_with_image_assert_passes(self) -> None:
        errors = validate_suite(_suite_with_image_assert(), source="image-assert.yml")
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_suite_with_video_assert_passes(self) -> None:
        errors = validate_suite(_suite_with_video_assert(), source="video-assert.yml")
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_suite_with_setup_and_teardown_passes(self) -> None:
        doc = _minimal_valid_suite()
        doc["setup"] = [
            {"id": "s-01", "action": "navigate", "params": {"url_script": "get_game_url"}},
        ]
        doc["teardown"] = [
            {"id": "t-01", "action": "script", "params": {"cmd": "cleanup.py"}},
        ]
        errors = validate_suite(doc, source="with-hooks.yml")
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_suite_with_test_level_setup_teardown_passes(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["setup"] = [
            {"id": "ts-01", "action": "bridge_action", "params": {"name": "setDevMode"}},
        ]
        doc["tests"][0]["teardown"] = [
            {"id": "tt-01", "action": "bridge_action", "params": {"name": "resetDevMode"}},
        ]
        errors = validate_suite(doc, source="test-hooks.yml")
        assert errors == [], f"Expected no errors, got: {errors}"


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    def test_missing_suite_name_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["suite"]
        errors = validate_suite(doc, source="no-suite.yml")
        assert any("suite" in e.message for e in errors), (
            f"Expected error mentioning 'suite'. Got: {errors}"
        )

    def test_missing_suite_name_error_at_root_path(self) -> None:
        doc = _minimal_valid_suite()
        del doc["suite"]
        errors = validate_suite(doc, source="no-suite.yml")
        # The error should be reported at the document root ($)
        assert any(e.path == "$" for e in errors)

    def test_missing_game_field_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["game"]
        errors = validate_suite(doc, source="no-game.yml")
        assert any("game" in e.message for e in errors)

    def test_missing_version_field_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["version"]
        errors = validate_suite(doc, source="no-version.yml")
        assert any("version" in e.message for e in errors)

    def test_missing_config_block_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["config"]
        errors = validate_suite(doc, source="no-config.yml")
        assert any("config" in e.message for e in errors)

    def test_missing_test_timeout_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["config"]["test_timeout_ms"]
        errors = validate_suite(doc, source="no-timeout.yml")
        assert any("test_timeout_ms" in e.message for e in errors)

    def test_missing_suite_timeout_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["config"]["suite_timeout_ms"]
        errors = validate_suite(doc, source="no-suite-timeout.yml")
        assert any("suite_timeout_ms" in e.message for e in errors)

    def test_missing_test_id_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["tests"][0]["id"]
        errors = validate_suite(doc, source="no-test-id.yml")
        assert any("id" in e.message for e in errors)

    def test_missing_test_title_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["tests"][0]["title"]
        errors = validate_suite(doc, source="no-title.yml")
        assert any("title" in e.message for e in errors)

    def test_missing_step_id_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["tests"][0]["steps"][0]["id"]
        errors = validate_suite(doc, source="no-step-id.yml")
        assert any("id" in e.message for e in errors)

    def test_missing_step_action_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["tests"][0]["steps"][0]["action"]
        errors = validate_suite(doc, source="no-action.yml")
        assert any("action" in e.message for e in errors)

    def test_missing_tests_list_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        del doc["tests"]
        errors = validate_suite(doc, source="no-tests.yml")
        assert any("tests" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Unsupported action type
# ---------------------------------------------------------------------------


class TestUnsupportedActionType:
    def test_invalid_action_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"][0]["action"] = "fly_to_moon"
        errors = validate_suite(doc, source="bad-action.yml")
        assert any("fly_to_moon" in e.message for e in errors), (
            f"Expected error mentioning the invalid action name. Got: {errors}"
        )

    def test_invalid_action_error_has_step_path(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"][0]["action"] = "teleport"
        errors = validate_suite(doc, source="bad-action.yml")
        # Error path must reference the step's action field
        assert any("step" in e.path or "action" in e.path for e in errors), (
            f"Expected path to reference the step location. Got paths: {_error_paths(errors)}"
        )

    def test_multiple_invalid_actions_all_reported(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"][0]["action"] = "invalid_one"
        doc["tests"][0]["steps"].append(
            {"id": "step-03", "action": "invalid_two", "params": {}}
        )
        errors = validate_suite(doc, source="multi-bad.yml")
        messages = _error_messages(errors)
        assert any("invalid_one" in m for m in messages)
        assert any("invalid_two" in m for m in messages)


# ---------------------------------------------------------------------------
# AI assert mode — missing required fields
# ---------------------------------------------------------------------------


class TestAiAssertValidation:
    def test_image_mode_missing_ref_screenshot_produces_error(self) -> None:
        doc = _suite_with_image_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["ref_screenshot"]
        errors = validate_suite(doc, source="no-ref-screenshot.yml")
        assert any("ref_screenshot" in e.message for e in errors), (
            f"Expected error for missing ref_screenshot. Got: {errors}"
        )

    def test_image_mode_missing_prompt_produces_error(self) -> None:
        doc = _suite_with_image_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["prompt"]
        errors = validate_suite(doc, source="no-prompt.yml")
        assert any("prompt" in e.message for e in errors)

    def test_image_mode_missing_output_schema_produces_error(self) -> None:
        doc = _suite_with_image_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["output_schema"]
        errors = validate_suite(doc, source="no-schema.yml")
        assert any("output_schema" in e.message for e in errors)

    def test_image_mode_missing_expected_produces_error(self) -> None:
        doc = _suite_with_image_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["expected"]
        errors = validate_suite(doc, source="no-expected.yml")
        assert any("expected" in e.message for e in errors)

    def test_video_mode_missing_video_trim_produces_error(self) -> None:
        doc = _suite_with_video_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["video_trim"]
        errors = validate_suite(doc, source="no-trim.yml")
        assert any("video_trim" in e.message for e in errors)

    def test_video_mode_missing_prompt_produces_error(self) -> None:
        doc = _suite_with_video_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["prompt"]
        errors = validate_suite(doc, source="no-prompt-video.yml")
        assert any("prompt" in e.message for e in errors)

    def test_unsupported_ai_mode_produces_error(self) -> None:
        doc = _suite_with_image_assert()
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["mode"] = "hologram"
        errors = validate_suite(doc, source="bad-mode.yml")
        assert any("hologram" in e.message for e in errors)

    def test_missing_ai_mode_produces_error(self) -> None:
        doc = _suite_with_image_assert()
        del doc["tests"][0]["steps"][-1]["assert"]["ai"]["mode"]
        errors = validate_suite(doc, source="no-mode.yml")
        assert any("mode" in e.message for e in errors)

    def test_assert_block_with_no_bridge_or_ai_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"].append(
            {"id": "step-03", "assert": {}}
        )
        errors = validate_suite(doc, source="empty-assert.yml")
        assert any("bridge" in e.message or "ai" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_non_dict_document_produces_error(self) -> None:
        errors = validate_suite(["not", "a", "dict"], source="list.yml")
        assert len(errors) >= 1
        assert errors[0].path == "$"

    def test_empty_tests_list_produces_error(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"] = []
        errors = validate_suite(doc, source="empty-tests.yml")
        assert any("tests" in e.path or "tests" in e.message for e in errors)

    def test_source_label_appears_in_errors(self) -> None:
        doc = _minimal_valid_suite()
        del doc["suite"]
        errors = validate_suite(doc, source="my-suite-file.yml")
        assert all(e.source == "my-suite-file.yml" for e in errors)

    def test_validation_error_str_includes_source_and_path(self) -> None:
        err = ValidationError(path="$.tests[0]", message="test error", source="file.yml")
        text = str(err)
        assert "file.yml" in text
        assert "$.tests[0]" in text
        assert "test error" in text
