"""
Tests for framework/helpers/symbol_reference.py — scoped AI reference selection.

Covers:
- build_asset_index over all manifest sections (incl. ui:).
- load_reference_selection union / order / dedup-by-path / missing-file skip.
- load_symbol_reference default pack excludes ui:.
- effective_selection precedence (assert > test > group) + ref_assets alias.
- ScopedReferenceCache.for_selection memoization (None vs [] distinct; order kept).
"""

from pathlib import Path

import pytest

from framework.helpers.symbol_reference import (
    ScopedReferenceCache,
    build_asset_index,
    effective_selection,
    load_reference_selection,
    load_symbol_reference,
)

GAME = "g"


@pytest.fixture()
def base_dir(tmp_path: Path) -> Path:
    """A tmp repo root with games/g/docs/assets/assets.yml + a few image files."""
    assets = tmp_path / "games" / GAME / "docs" / "assets"
    (assets / "symbols").mkdir(parents=True)
    (assets / "ui").mkdir(parents=True)
    # Image files that exist on disk (content irrelevant — only existence is checked).
    for rel in ("symbols/coin.jpg", "symbols/wild.jpg", "ui/bet.jpg", "ui/base.jpg"):
        (assets / rel).write_bytes(b"x")
    manifest = """\
symbols:
  coin: { file: symbols/coin.jpg, name: "Coin", description: "gold coin" }
  wild: { file: symbols/wild.jpg, name: "Wild" }
  ghost: { file: symbols/missing.jpg, name: "Ghost" }   # image file absent
ui_elements:
  bet_label: { file: ui/bet.jpg, name: "Bet" }
  no_file: { name: "No File Item" }                      # entry without file:
ui:
  base_game: { file: ui/base.jpg, name: "Base Game" }
"""
    (assets / "assets.yml").write_text(manifest, encoding="utf-8")
    return tmp_path


def _paths(ref):
    return [Path(p).name for p, _label in ref.items]


# --------------------------------------------------------------------------- #
# build_asset_index
# --------------------------------------------------------------------------- #


def test_build_asset_index_covers_all_sections(base_dir: Path):
    idx = build_asset_index(GAME, base_dir)
    assert set(idx) == {"symbols", "ui_elements", "ui"}
    assert idx["symbols"] == {"coin", "wild", "ghost"}  # incl. missing-image item
    assert idx["ui_elements"] == {"bet_label", "no_file"}  # incl. no-file item
    assert idx["ui"] == {"base_game"}


def test_build_asset_index_missing_manifest(tmp_path: Path):
    assert build_asset_index(GAME, tmp_path) == {}


# --------------------------------------------------------------------------- #
# load_symbol_reference (default full pack)
# --------------------------------------------------------------------------- #


def test_full_pack_excludes_ui_section(base_dir: Path):
    ref = load_symbol_reference(GAME, base_dir)
    # symbols (coin, wild; ghost skipped) + ui_elements (bet) — NOT ui.base_game.
    assert _paths(ref) == ["coin.jpg", "wild.jpg", "bet.jpg"]
    # Label uses name + " — desc" when a description is present.
    labels = {label for _p, label in ref.items}
    assert "Coin — gold coin" in labels
    assert "Wild" in labels


# --------------------------------------------------------------------------- #
# load_reference_selection
# --------------------------------------------------------------------------- #


def test_selection_both_none_is_full_pack(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, None, None)
    assert _paths(ref) == ["coin.jpg", "wild.jpg", "bet.jpg"]


def test_selection_by_group(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, ["ui_elements"], None)
    assert _paths(ref) == ["bet.jpg"]  # no_file has no file -> skipped


def test_selection_by_items_in_listed_order(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, None, ["symbols.wild", "symbols.coin"])
    assert _paths(ref) == ["wild.jpg", "coin.jpg"]


def test_selection_can_load_ui_section(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, ["ui"], None)
    assert _paths(ref) == ["base.jpg"]


def test_selection_group_order_preserved(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, ["ui", "symbols"], None)
    assert _paths(ref) == ["base.jpg", "coin.jpg", "wild.jpg"]


def test_selection_dedup_by_path(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, ["symbols"], ["symbols.coin"])
    # coin appears once (group items first, explicit assets entry deduped).
    assert _paths(ref) == ["coin.jpg", "wild.jpg"]


def test_selection_missing_image_file_skipped(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, None, ["symbols.ghost"])
    assert ref.items == []  # ghost's image file does not exist


def test_selection_unknown_group_and_name_contribute_nothing(base_dir: Path):
    ref = load_reference_selection(GAME, base_dir, ["nope"], ["symbols.nope", "bare"])
    assert ref.items == []


# --------------------------------------------------------------------------- #
# effective_selection
# --------------------------------------------------------------------------- #


def test_effective_selection_none_when_unset(base_dir: Path):
    assert effective_selection({}, {}, {}, {}) is None


def test_effective_selection_assert_over_test_over_group(base_dir: Path):
    group = {"asset_groups": ["symbols"]}
    test = {"assets": ["ui_elements.bet_label"]}
    ai = {"assets": ["symbols.coin"]}
    assert effective_selection(group, test, ai) == (None, ["symbols.coin"])
    # No assert-level selection -> test wins.
    assert effective_selection(group, test, {}) == (None, ["ui_elements.bet_label"])
    # No assert/test -> group wins.
    assert effective_selection(group, {}, {}) == (["symbols"], None)


def test_effective_selection_level_wins_entirely_no_merge(base_dir: Path):
    # ai sets asset_groups -> its own ref_assets at the same level is ignored, and
    # lower levels do not merge in.
    group = {"assets": ["symbols.wild"]}
    ai = {"asset_groups": ["ui"], "ref_assets": ["coin"]}
    assert effective_selection(group, {}, ai) == (["ui"], None)


def test_effective_selection_ref_assets_alias(base_dir: Path):
    idx = build_asset_index(GAME, base_dir)
    # Bare names resolve to qualified group.name across all sections.
    sel = effective_selection({}, {}, {"ref_assets": ["coin", "base_game"]}, idx)
    assert sel == (None, ["symbols.coin", "ui.base_game"])


def test_effective_selection_ref_assets_all_unknown_falls_back_full(base_dir: Path):
    idx = build_asset_index(GAME, base_dir)
    # Nothing resolves -> None (full pack), not an empty selection.
    assert effective_selection({}, {}, {"ref_assets": ["nope"]}, idx) is None


# --------------------------------------------------------------------------- #
# ScopedReferenceCache
# --------------------------------------------------------------------------- #


def test_cache_memoizes_and_distinguishes_none_from_empty(base_dir: Path):
    cache = ScopedReferenceCache(GAME, base_dir)
    a = cache.for_selection(["symbols"], None)
    b = cache.for_selection(["symbols"], None)
    assert a is b  # same selection -> same cached object
    full = cache.for_selection(None, None)
    empty = cache.for_selection(None, [])
    assert full is not empty
    assert _paths(full) == ["coin.jpg", "wild.jpg", "bet.jpg"]
    assert empty.items == []  # explicit empty -> no references


def test_cache_order_not_normalized(base_dir: Path):
    cache = ScopedReferenceCache(GAME, base_dir)
    ab = cache.for_selection(["ui", "symbols"], None)
    ba = cache.for_selection(["symbols", "ui"], None)
    assert ab is not ba
    assert _paths(ab) == ["base.jpg", "coin.jpg", "wild.jpg"]
    assert _paths(ba) == ["coin.jpg", "wild.jpg", "base.jpg"]
