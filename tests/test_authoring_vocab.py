"""Tests for framework/authoring/vocab.py — pattern compile, capture, merge."""

import pytest

from framework.authoring.errors import FlowCompileError
from framework.authoring.vocab import (
    build_verb_table,
    compile_pattern,
    normalize,
)


def _entry(pattern, **expand):
    return {"pattern": pattern, "expands": [{"action": "noop", **expand}]}


# --- pattern compilation ----------------------------------------------------


def test_compile_pattern_literal_and_slots():
    regex, slots, spec = compile_pattern("expect bet index is {n:int}")
    assert [s.name for s in slots] == ["n"]
    assert slots[0].type == "int"
    assert regex.match("expect bet index is 3")
    assert regex.match("EXPECT BET INDEX IS 3")  # case-insensitive literals
    assert not regex.match("expect bet index is x")  # int slot
    assert spec > 0


def test_whitespace_normalized_match():
    table = build_verb_table([_entry("spin and wait for result")], [])
    assert table.match("  spin   and  wait for result ") is not None


# --- typed capture ----------------------------------------------------------


def test_int_capture_is_typed():
    table = build_verb_table([_entry("set count {n:int}")], [])
    verb, caps = table.match("set count 7")
    assert caps["n"] == 7 and isinstance(caps["n"], int)


def test_ident_is_lowercased():
    table = build_verb_table([_entry("save as {x:ident}")], [])
    _, caps = table.match("save as MyShot")
    assert caps["x"] == "myshot"


def test_str_slot_captures_quoted():
    # A {x:str} slot matches a "double-quoted" token and captures between the quotes.
    table = build_verb_table([_entry("force grid {name:str}")], [])
    _, caps = table.match('force grid "no-win"')
    assert caps["name"] == "no-win"


# --- specificity / ambiguity ------------------------------------------------


def test_more_literal_pattern_wins():
    # Both can match "spin now"; the literal one is more specific.
    table = build_verb_table(
        [_entry("spin {what:ident}"), _entry("spin now")],
        [],
    )
    verb, _ = table.match("spin now")
    assert verb.pattern == "spin now"


def test_equal_specificity_is_ambiguous():
    table = build_verb_table([_entry("do {a:ident}"), _entry("do {b:ident}")], [])
    with pytest.raises(FlowCompileError, match="ambiguous"):
        table.match("do thing")


def test_no_match_returns_none():
    table = build_verb_table([_entry("spin")], [])
    assert table.match("teleport") is None


# --- merge / override -------------------------------------------------------


def test_game_overrides_core_on_identical_pattern():
    core = [{"pattern": "spin", "expands": [{"action": "core"}]}]
    game = [{"pattern": "spin", "expands": [{"action": "game"}]}]
    table = build_verb_table(core, game)
    verb, _ = table.match("spin")
    assert verb.layer == "game"
    assert verb.expands[0]["action"] == "game"


def test_duplicate_within_layer_raises():
    with pytest.raises(FlowCompileError, match="duplicate pattern"):
        build_verb_table([_entry("spin"), _entry("spin")], [])


# --- suggestions ------------------------------------------------------------


def test_suggest_returns_near_patterns():
    table = build_verb_table(
        [_entry("expect last payout is {n:int}"), _entry("expect bet index is {n:int}")],
        [],
    )
    sugg = table.suggest("expect last payour is 0")
    assert any("payout" in s for s in sugg)


def test_normalize_collapses_space():
    assert normalize("  a   b ") == "a b"
