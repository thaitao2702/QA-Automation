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

    def test_click_button_action_is_supported(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"].append(
            {"id": "step-03", "action": "click_button", "params": {"name": "spin"}}
        )
        assert validate_suite(doc, source="ok.yml") == []

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


class TestSaveAndBridgeAssertVar:
    def test_save_field_and_bridge_assert_var_valid(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"] = [
            {
                "id": "step-01",
                "action": "bridge_action",
                "params": {"name": "getLastPayout"},
                "save": "declaredVar",
            },
            {
                "id": "step-02",
                "action": "bridge_assert",
                "params": {"method": "testFreeSpin", "args": ["$declaredVar"], "expected": True},
            },
        ]
        errors = validate_suite(doc, source="vars.yml")
        assert errors == []

    def test_non_string_save_rejected(self) -> None:
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"][0]["save"] = 123
        errors = validate_suite(doc, source="bad-save.yml")
        assert any(e.path.endswith(".save") for e in errors)


# ---------------------------------------------------------------------------
# runs: — one execution, many test-case verdicts
# ---------------------------------------------------------------------------


def _suite_with_run() -> dict:
    """Smallest valid suite that uses a `runs:` block instead of `tests:`."""
    return {
        "suite": "Runs demo",
        "game": "zeus",
        "version": "1.0",
        "config": {"test_timeout_ms": 60000, "suite_timeout_ms": 600000},
        "runs": [
            {
                "id": "run-01",
                "title": "One bonus, many cases",
                "cases": [
                    {"id": "ZZ-30", "title": "3 coins -> 3 free spins"},
                    {"id": "ZZ-32", "title": "coins held"},
                ],
                "steps": [
                    {"id": "step-01", "action": "bridge_action", "params": {"name": "spinUntilBonus"}},
                    {
                        "id": "step-02",
                        "case": "ZZ-30",
                        "assert": {"bridge": {"method": "getFreeSpinsRemaining", "expected": 3}},
                    },
                    {
                        "id": "step-03",
                        "case": ["ZZ-32"],
                        "action": "bridge_assert",
                        "params": {"method": "verifyHoldSpinCoinsHeld", "args": ["$events"], "expected": True},
                    },
                ],
            }
        ],
    }


def _ai_video_step(expected_by_case: dict, step_id: str = "step-ai") -> dict:
    return {
        "id": step_id,
        "assert": {
            "ai": {
                "mode": "video",
                "video_trim": {"from_step": "step-01", "to_step": "step-01"},
                "prompt": "Report each field.",
                "output_schema": {
                    "entered_bonus": {"type": "boolean"},
                    "coins_held": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "expected_by_case": expected_by_case,
            }
        },
    }


class TestRunsAndCases:
    def test_valid_run_passes(self) -> None:
        assert validate_suite(_suite_with_run(), source="runs.yml") == []

    def test_run_missing_cases(self) -> None:
        doc = _suite_with_run()
        del doc["runs"][0]["cases"]
        errors = validate_suite(doc, source="r.yml")
        assert any("cases" in e.message or e.path.endswith("cases") for e in errors)

    def test_run_empty_cases(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["cases"] = []
        errors = validate_suite(doc, source="r.yml")
        assert any("cases" in (e.message + e.path) for e in errors)

    def test_run_missing_steps(self) -> None:
        doc = _suite_with_run()
        del doc["runs"][0]["steps"]
        errors = validate_suite(doc, source="r.yml")
        assert any("steps" in (e.message + e.path) for e in errors)

    def test_case_missing_title(self) -> None:
        doc = _suite_with_run()
        del doc["runs"][0]["cases"][0]["title"]
        errors = validate_suite(doc, source="r.yml")
        assert any("title" in e.message for e in errors)

    def test_case_tag_references_undeclared_case(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["steps"][1]["case"] = "ZZ-99"
        errors = validate_suite(doc, source="r.yml")
        assert any("ZZ-99" in e.message for e in errors)

    def test_case_tag_on_non_assert_step_rejected(self) -> None:
        doc = _suite_with_run()
        # step-01 is a plain bridge_action — tagging it with a case is invalid
        doc["runs"][0]["steps"][0]["case"] = "ZZ-30"
        errors = validate_suite(doc, source="r.yml")
        assert any("may only tag assert steps" in e.message for e in errors)

    def test_case_tag_as_list_passes(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["steps"][2]["case"] = ["ZZ-30", "ZZ-32"]
        assert validate_suite(doc, source="r.yml") == []

    def test_case_tag_bad_type_rejected(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["steps"][1]["case"] = 123
        errors = validate_suite(doc, source="r.yml")
        assert any("must be a string or list" in e.message for e in errors)

    def test_duplicate_case_id(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["cases"][1]["id"] = "ZZ-30"
        errors = validate_suite(doc, source="r.yml")
        assert any("ZZ-30" in e.message and "duplicate" in e.message for e in errors)

    def test_run_id_collides_with_test_id(self) -> None:
        doc = _suite_with_run()
        doc["tests"] = [
            {
                "id": "run-01",  # collides with the run id
                "title": "t",
                "steps": [{"id": "s1", "action": "capture"}],
            }
        ]
        errors = validate_suite(doc, source="r.yml")
        assert any("run-01" in e.message and "duplicate" in e.message for e in errors)

    def test_case_with_no_coverage_rejected(self) -> None:
        doc = _suite_with_run()
        # remove the step covering ZZ-32
        doc["runs"][0]["steps"] = doc["runs"][0]["steps"][:2]
        errors = validate_suite(doc, source="r.yml")
        assert any("ZZ-32" in e.message and "no asserts" in e.message for e in errors)

    def test_run_inside_group_passes(self) -> None:
        doc = _suite_with_run()
        run = doc.pop("runs")[0]
        doc["groups"] = [{"id": "g1", "title": "grp", "runs": [run]}]
        assert validate_suite(doc, source="r.yml") == []

    def test_group_with_only_runs_passes(self) -> None:
        doc = _suite_with_run()
        run = doc.pop("runs")[0]
        doc["groups"] = [{"id": "g1", "runs": [run]}]
        assert validate_suite(doc, source="r.yml") == []

    # --- expected_by_case (AI output partitioned across cases) ---

    def test_expected_by_case_valid(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["steps"].append(
            _ai_video_step(
                {
                    "ZZ-30": {"entered_bonus": True, "confidence": "high"},
                    "ZZ-32": {"coins_held": True, "confidence": "high"},
                }
            )
        )
        assert validate_suite(doc, source="r.yml") == []

    def test_expected_by_case_covers_cases(self) -> None:
        # A case covered ONLY by expected_by_case (no `case:` tag) is valid.
        doc = _suite_with_run()
        # drop the bridge step covering ZZ-32; cover it via expected_by_case instead
        doc["runs"][0]["steps"] = doc["runs"][0]["steps"][:2]
        doc["runs"][0]["steps"].append(
            _ai_video_step({"ZZ-32": {"coins_held": True}})
        )
        assert validate_suite(doc, source="r.yml") == []

    def test_expected_by_case_undeclared_case_rejected(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["steps"].append(_ai_video_step({"ZZ-99": {"coins_held": True}}))
        errors = validate_suite(doc, source="r.yml")
        assert any("ZZ-99" in e.message for e in errors)

    def test_expected_by_case_unknown_field_rejected(self) -> None:
        doc = _suite_with_run()
        doc["runs"][0]["steps"].append(_ai_video_step({"ZZ-30": {"not_a_field": True}}))
        errors = validate_suite(doc, source="r.yml")
        assert any("not_a_field" in e.message for e in errors)

    def test_expected_by_case_with_plain_expected_rejected(self) -> None:
        doc = _suite_with_run()
        step = _ai_video_step({"ZZ-30": {"coins_held": True}})
        step["assert"]["ai"]["expected"] = {"coins_held": True}
        doc["runs"][0]["steps"].append(step)
        errors = validate_suite(doc, source="r.yml")
        assert any("not both" in e.message for e in errors)

    def test_expected_by_case_in_test_rejected(self) -> None:
        # expected_by_case is runs-only; using it in a normal test is an error.
        doc = _minimal_valid_suite()
        doc["tests"][0]["steps"].append(
            {
                "id": "step-09",
                "assert": {
                    "ai": {
                        "mode": "video",
                        "video_trim": {"from_step": "step-01", "to_step": "step-02"},
                        "prompt": "x",
                        "output_schema": {"ok": {"type": "boolean"}},
                        "expected_by_case": {"ZZ-01": {"ok": True}},
                    }
                },
            }
        )
        errors = validate_suite(doc, source="t.yml")
        assert any("expected_by_case" in e.path and "only valid in a run" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Reference selection: asset_groups / assets / ref_assets validation
# ---------------------------------------------------------------------------


_ASSET_INDEX = {
    "symbols": {"coin", "wild"},
    "ui_elements": {"bet_label", "win_label"},
    "ui": {"base_game", "free_spin"},
}


class TestReferenceSelectionValidation:
    def test_skipped_when_no_asset_index(self):
        # Without an index, even nonsense references pass (unit-test mode).
        doc = _suite_with_image_assert()
        doc["tests"][0]["assets"] = ["totally.bogus"]
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["asset_groups"] = ["nope"]
        assert validate_suite(doc, source="t.yml") == []

    def test_valid_selection_passes(self):
        doc = _suite_with_image_assert()
        doc["tests"][0]["asset_groups"] = ["symbols"]
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["assets"] = ["ui_elements.bet_label"]
        assert validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX) == []

    def test_unknown_asset_group_fails(self):
        doc = _suite_with_image_assert()
        doc["tests"][0]["asset_groups"] = ["symbols", "bogus"]
        errors = validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX)
        assert any("asset_groups" in e.path and "bogus" in e.message for e in errors)

    def test_unqualified_asset_fails(self):
        doc = _suite_with_image_assert()
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["assets"] = ["coin"]
        errors = validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX)
        assert any("assets" in e.path and "group" in e.message.lower() for e in errors)

    def test_unknown_group_in_qualified_asset_fails(self):
        doc = _suite_with_image_assert()
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["assets"] = ["nope.coin"]
        errors = validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX)
        assert any("assets" in e.path for e in errors)

    def test_unknown_item_in_qualified_asset_fails(self):
        doc = _suite_with_image_assert()
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["assets"] = ["symbols.nope"]
        errors = validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX)
        assert any("assets" in e.path and "nope" in e.message for e in errors)

    def test_ref_assets_unknown_name_does_not_fail_load(self):
        # Deprecated alias is lenient: unknown bare names never fail the load.
        doc = _suite_with_image_assert()
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["ref_assets"] = ["hold_spin_overlay"]
        assert validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX) == []

    def test_ref_assets_wrong_shape_fails(self):
        doc = _suite_with_image_assert()
        doc["tests"][0]["steps"][-1]["assert"]["ai"]["ref_assets"] = "coin"  # not a list
        errors = validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX)
        assert any("ref_assets" in e.path for e in errors)

    def test_group_level_selection_validated(self):
        doc = _minimal_valid_suite()
        del doc["tests"]
        doc["groups"] = [
            {
                "id": "grp",
                "asset_groups": ["bogus"],
                "tests": [
                    {"id": "ZZ-9", "title": "t", "steps": [
                        {"id": "s1", "action": "bridge_action", "params": {"name": "spin"}}
                    ]}
                ],
            }
        ]
        errors = validate_suite(doc, source="t.yml", asset_index=_ASSET_INDEX)
        assert any("asset_groups" in e.path and "bogus" in e.message for e in errors)
