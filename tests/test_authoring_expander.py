"""Tests for framework/authoring/expander.py — expansion, ids, aliases, @case, lint."""

import pytest

from framework.authoring.errors import FlowCompileError
from framework.authoring.expander import Expander
from framework.authoring.loader import load_vocab

VOCAB = load_vocab("zeus")


def _steps(step_lines, *, setup=None, teardown=None):
    """Expand a single ungrouped test's scopes and return its compiled dict."""
    test = {"id": "T1", "title": "t", "steps": step_lines}
    if setup is not None:
        test["setup"] = setup
    if teardown is not None:
        test["teardown"] = teardown
    data = {"suite": "s", "game": "zeus", "version": "1", "config": {}, "tests": [test]}
    result = Expander(data, {}, VOCAB, "t.flow.yml").run()
    return result, result.doc["tests"][0]


def _real(result, step):
    """Map a placeholder-id step back to its real id."""
    return result.real_ids[step["id"]]


# --- expansion & id numbering ----------------------------------------------


def test_start_the_game_expands_to_navigate_ready_startgame():
    result, test = _steps(["start the game"])
    actions = [s["action"] for s in test["steps"]]
    # startGame itself resolves at IDLE + sceneReady (bridge-api §1.1) — no extra wait.
    assert actions == ["navigate", "wait_for_state", "bridge_action"]
    assert test["steps"][2]["params"]["name"] == "startGame"


def test_ids_are_sequential_per_scope():
    result, test = _steps(["increase bet", "decrease bet"])
    reals = [_real(result, s) for s in test["steps"]]
    assert reals == ["step-01", "step-02"]


def test_spin_and_wait_uses_wait_for_spin_complete():
    result, test = _steps(["spin and wait for result"])
    # spin() is fire-and-return; the composite spins + waits the whole round.
    assert len(test["steps"]) == 1
    assert test["steps"][0]["params"]["name"] == "waitForSpinComplete"
    assert test["steps"][0]["params"]["timeoutMs"] == 60000


def test_int_expected_is_int_not_str():
    result, test = _steps(["expect bet index is 2"])
    assert test["steps"][0]["params"]["expected"] == 2


# --- aliases / @alias resolution -------------------------------------------


def test_capture_alias_resolves_in_image_check():
    result, test = _steps(
        [
            "take a screenshot as a",
            "increase bet",
            "take a screenshot as b",
            "check that the bet increased between a and b",
        ]
    )
    # The image assert's ref_screenshot must be the two capture step real ids.
    img = test["steps"][-1]
    refs = img["assert"]["ai"]["ref_screenshot"]
    assert refs == ["step-01", "step-03"]


def test_ref_to_undefined_name_raises():
    with pytest.raises(FlowCompileError, match="undefined name"):
        _steps(["check that the bet increased between x and y"])


def test_redefining_a_name_raises():
    with pytest.raises(FlowCompileError, match="already defined"):
        _steps(["take a screenshot as dup", "take a screenshot as dup"])


def test_remember_balance_saves_var_and_compares():
    result, test = _steps(
        ["remember balance as start", "spin and wait for result", "expect balance decreased from start"]
    )
    # 'remember balance as start' saves the var; the decreased-from check is a single
    # script step comparing the live balance to vars['start'].
    assert test["steps"][0]["save"] == "start"
    assert test["steps"][-1]["action"] == "script"
    assert "vars['start']" in test["steps"][-1]["params"]["cmd"]


# --- lint guards ------------------------------------------------------------


def test_start_auto_without_open_popup_raises():
    with pytest.raises(FlowCompileError, match="open the auto popup"):
        _steps(["start 10 auto spins"])


def test_start_auto_with_open_popup_ok():
    _, test = _steps(["open the auto popup", "start 10 auto spins"])
    names = [s.get("params", {}).get("name") for s in test["steps"]]
    assert names == ["openAutoPopup", "startAutoSpin"]


# --- unknown verbs ----------------------------------------------------------


def test_unknown_verb_raises_with_suggestions():
    with pytest.raises(FlowCompileError) as exc:
        _steps(["expect last payour is 0"])
    assert exc.value.suggestions  # nearest patterns offered


# --- @case attribution ------------------------------------------------------


def _run(step_lines, case_ids=("C1", "C2")):
    cases = [{"id": c, "title": c} for c in case_ids]
    run = {"id": "R1", "cases": cases, "steps": step_lines}
    data = {"suite": "s", "game": "zeus", "version": "1", "config": {}, "runs": [run]}
    result = Expander(data, {}, VOCAB, "t.flow.yml").run()
    return result, result.doc["runs"][0]


def test_case_tag_lands_on_assertion_step():
    _, run = _run(["expect free spins remaining is 0 @case C1"])
    assert run["steps"][0]["case"] == "C1"


def test_case_tag_multiple_ids_becomes_list():
    _, run = _run(["expect free spins remaining is 0 @case C1, C2"])
    assert run["steps"][0]["case"] == ["C1", "C2"]


def test_case_tag_unknown_id_raises():
    with pytest.raises(FlowCompileError, match="unknown case id"):
        _run(["expect free spins remaining is 0 @case ZZ-99"])


def test_case_tag_on_non_asserting_verb_raises():
    with pytest.raises(FlowCompileError, match="makes no assertion"):
        _run(["increase bet @case C1"])


def test_case_tag_outside_run_raises():
    with pytest.raises(FlowCompileError, match="only valid on a run"):
        _steps(["expect bet index is 0 @case C1"])
