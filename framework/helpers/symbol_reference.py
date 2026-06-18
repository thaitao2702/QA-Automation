"""
Symbol / UI reference loader for AI assertions.

Builds a reusable "reference pack" from a game's ``assets.yml`` (legacy name:
``symbols.yml``) so an image/video AI request can be primed with the canonical
appearance + description of each symbol and UI element BEFORE the screenshot/video
under test. This gives the model ground-truth context to identify what it is looking at.

The manifest is **group → item**: top-level sections (``symbols:``, ``ui_elements:``,
``ui:``) are *groups*; each key under them is an *item*. References can therefore be
**scoped** to exactly what an assert needs:

- ``asset_groups: [<group>, …]``      — every item in those groups.
- ``assets: ["<group>.<name>", …]``   — specific items, qualified by group.

Only assets whose reference image file actually exists on disk are included
(the manifest may list assets whose image has not been added yet).

Public interface
----------------
SymbolReference
    header: str                     intro text shown before the reference images
    items: list[(Path, str)]        (image_path, label) for each existing asset
    is_empty -> bool
    descriptions_text() -> str      header + all labels as one text block

load_symbol_reference(game, base_dir, categories=None) -> SymbolReference
    Full pack (or a list of whole sections). Default = ["symbols", "ui_elements"].
load_reference_selection(game, base_dir, asset_groups, assets) -> SymbolReference
    Deduped union of whole groups + qualified items. Both None = full pack.
build_asset_index(game, base_dir) -> dict[str, set[str]]
    group -> item names (ALL sections) — used for load-time validation.
effective_selection(group_doc, test_doc, ai_block, asset_index=None)
    Resolve the (asset_groups, assets) in effect, assert > test > group.
ScopedReferenceCache
    Memoize scoped packs by selection so each is loaded from disk once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SymbolReference:
    """A prebuilt set of reference assets to prepend to every AI request."""

    header: str
    items: list[tuple[Path, str]] = field(default_factory=list)  # (image_path, label)

    @property
    def is_empty(self) -> bool:
        return not self.items

    def descriptions_text(self) -> str:
        """All labels as one text block — for providers that cannot take images."""
        lines = [self.header, ""]
        lines.extend(f"- {label}" for _, label in self.items)
        return "\n".join(lines)


_HEADER_TEMPLATE = (
    "REFERENCE ASSETS for the game '{game}'. The labeled images that follow are the "
    "canonical, ground-truth appearance of each game symbol and UI element, each "
    "preceded by its name and description. Use them to recognise what appears in the "
    "screenshot/video under test below. These are reference context only — do not "
    "describe or judge the reference images themselves."
)

# The default full pack deliberately excludes the ``ui:`` section (full-screen states
# like base_game / free_spin overlay): those are only useful when explicitly selected.
_DEFAULT_SECTIONS = ["symbols", "ui_elements"]

# ``ref_assets`` (bare names) is deprecated — warn once per process when first resolved.
_REF_ASSETS_DEPRECATION_WARNED = False


def _read_manifest_index(
    game: str, base_dir: Path
) -> tuple[dict[tuple[str, str], tuple[Path | None, str]], dict[str, list[str]], str]:
    """Read the asset manifest once into a (group, name)-keyed index.

    Returns ``(index, group_order, header)`` where:
      - ``index[(group, name)]`` = ``(image_path | None, label)`` for every entry of
        every top-level dict section. ``image_path`` may not exist on disk (callers
        filter); it is ``None`` when the entry has no ``file:`` field.
      - ``group_order[group]`` = the entry names in manifest (YAML) order.
      - ``header`` = the reference-pack intro text.
    Missing/unparseable manifest -> empty index + group_order (header still returned).
    """
    asset_dir = base_dir / "games" / game / "docs" / "assets"
    # Prefer the current manifest name (assets.yml); fall back to the legacy symbols.yml.
    manifest_path = asset_dir / "assets.yml"
    if not manifest_path.exists():
        manifest_path = asset_dir / "symbols.yml"
    header = _HEADER_TEMPLATE.format(game=game)

    index: dict[tuple[str, str], tuple[Path | None, str]] = {}
    group_order: dict[str, list[str]] = {}

    if not manifest_path.exists():
        logger.warning("asset manifest (assets.yml / symbols.yml) not found for %s: %s", game, asset_dir)
        return index, group_order, header

    import yaml  # lazy

    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("Could not parse %s: %s", manifest_path, exc)
        return index, group_order, header

    asset_dir = manifest_path.parent
    if not isinstance(manifest, dict):
        return index, group_order, header

    for group, entries in manifest.items():
        if not isinstance(entries, dict):
            continue  # non-section top-level keys (metadata, scalars) are not groups
        names: list[str] = []
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            names.append(name)
            file_rel = entry.get("file", "")
            img_path: Path | None = (asset_dir / file_rel) if file_rel else None
            disp = entry.get("name", name)
            desc = entry.get("description", "")
            # Human-readable label only (no internal manifest key) — what the model
            # sees as the per-image caption.
            label = disp + (f" — {desc}" if desc else "")
            index[(group, name)] = (img_path, label)
        group_order[group] = names

    return index, group_order, header


def load_symbol_reference(
    game: str, base_dir: Path, categories: list[str] | None = None
) -> SymbolReference:
    """
    Build a SymbolReference from whole manifest sections.

    ``categories=None`` loads the default full pack ``["symbols", "ui_elements"]``;
    pass e.g. ``["ui_elements"]`` to scope to those sections. Entries with an existing
    image file become ``(image_path, label)`` items, in section then manifest order.
    Missing files (and unknown sections) are skipped and logged. Always returns a
    SymbolReference (possibly empty).
    """
    index, group_order, header = _read_manifest_index(game, base_dir)
    sections = categories if categories is not None else _DEFAULT_SECTIONS
    items: list[tuple[Path, str]] = []
    missing = 0

    for section in sections:
        names = group_order.get(section)
        if not names:
            logger.warning("asset category '%s' not found / empty for %s", section, game)
            continue
        for name in names:
            img_path, label = index[(section, name)]
            if img_path is None:
                continue
            if not img_path.exists():
                missing += 1
                continue
            items.append((img_path, label))

    logger.info(
        "Symbol reference for %s [%s]: %d asset image(s) loaded, %d missing file(s)",
        game, ",".join(sections), len(items), missing,
    )
    return SymbolReference(header=header, items=items)


def load_reference_selection(
    game: str,
    base_dir: Path,
    asset_groups: list[str] | None,
    assets: list[str] | None,
) -> SymbolReference:
    """
    Build a SymbolReference from a (groups, items) selection.

    The loaded reference is the **deduped union** of:
      - every item of each ``asset_groups`` group (in listed group order, items in
        manifest order), then
      - each ``assets`` ``"{group}.{name}"`` item (in listed order),
    deduped by resolved image path (first occurrence wins).

    ``asset_groups is None and assets is None`` -> the full pack (today's default).
    Items whose image file is missing on disk are warned + skipped; unknown
    group/name is warned and contributes nothing (load-time validation catches these).
    """
    if asset_groups is None and assets is None:
        return load_symbol_reference(game, base_dir)

    index, group_order, header = _read_manifest_index(game, base_dir)
    items: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    missing = 0

    def _add(group: str, name: str) -> None:
        nonlocal missing
        entry = index.get((group, name))
        if entry is None:
            logger.warning("unknown asset '%s.%s' — not in manifest for %s", group, name, game)
            return
        img_path, label = entry
        if img_path is None:
            return  # manifest entry without a file: field
        if not img_path.exists():
            missing += 1
            logger.warning("reference asset '%s.%s' image not found: %s", group, name, img_path)
            return
        resolved = img_path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        items.append((img_path, label))

    for group in (asset_groups or []):
        names = group_order.get(group)
        if names is None:
            logger.warning("unknown asset group '%s' for %s", group, game)
            continue
        for name in names:
            _add(group, name)

    for qualified in (assets or []):
        if "." not in qualified:
            logger.warning("asset '%s' must be '{group}.{name}' (qualified) — skipped", qualified)
            continue
        group, name = qualified.split(".", 1)
        _add(group, name)

    logger.info(
        "Reference selection for %s: asset_groups=%s assets=%s -> %d image(s), %d missing file(s)",
        game, asset_groups, assets, len(items), missing,
    )
    return SymbolReference(header=header, items=items)


def build_asset_index(game: str, base_dir: Path) -> dict[str, set[str]]:
    """group -> set(item names) over ALL manifest sections (for load-time validation).

    Includes items whose image file is missing (a real-but-imageless name is not a
    typo). Returns ``{}`` when the manifest is absent/unparseable (callers skip
    validation in that case).
    """
    _index, group_order, _header = _read_manifest_index(game, base_dir)
    return {group: set(names) for group, names in group_order.items()}


def _ordered_groups(asset_index: dict[str, set[str]]) -> list[str]:
    """Group lookup order for the ``ref_assets`` alias: symbols, ui_elements, then rest."""
    preferred = [g for g in _DEFAULT_SECTIONS if g in asset_index]
    preferred += [g for g in asset_index if g not in _DEFAULT_SECTIONS]
    return preferred


def _resolve_ref_assets_alias(
    names: list[str], asset_index: dict[str, set[str]] | None
) -> list[str]:
    """Resolve deprecated bare ``ref_assets`` names to qualified ``"{group}.{name}"``.

    A name found in multiple groups resolves deterministically (symbols > ui_elements >
    other sections in manifest order) with an ambiguity warning; an unknown name is
    warned and dropped. Returns ``[]`` when no index is available (cannot qualify).
    """
    global _REF_ASSETS_DEPRECATION_WARNED
    if not _REF_ASSETS_DEPRECATION_WARNED:
        logger.warning(
            "'ref_assets' is deprecated; use 'asset_groups'/'assets' (\"{group}.{name}\"). "
            "Resolving bare names across all asset groups."
        )
        _REF_ASSETS_DEPRECATION_WARNED = True

    if not asset_index:
        return []

    order = _ordered_groups(asset_index)
    qualified: list[str] = []
    for name in names:
        hits = [g for g in order if name in asset_index[g]]
        if not hits:
            logger.warning("ref_asset '%s' not found in any asset group — skipped", name)
            continue
        if len(hits) > 1:
            logger.warning(
                "ref_asset '%s' is ambiguous (groups: %s); using '%s'",
                name, ", ".join(hits), hits[0],
            )
        qualified.append(f"{hits[0]}.{name}")
    return qualified


def effective_selection(
    group_doc: Any,
    test_doc: Any,
    ai_block: Any,
    asset_index: dict[str, set[str]] | None = None,
) -> tuple[list[str] | None, list[str] | None] | None:
    """Resolve the (asset_groups, assets) selection in effect, assert > test > group.

    The most specific level (ai_block, then test, then group) that sets *any* of
    ``asset_groups`` / ``assets`` / ``ref_assets`` wins **entirely** — no cross-level or
    cross-field merge. A level that sets the new fields ignores its own ``ref_assets``.
    A ``ref_assets`` level is resolved via the deprecated alias into a qualified
    ``assets`` list (empty resolution falls back to the full pack -> ``None``).

    Returns ``(asset_groups, assets)`` (either element may be ``None``), or ``None``
    when no level sets anything (caller uses the full pack default).
    """
    for level in (ai_block, test_doc, group_doc):
        if not isinstance(level, dict):
            continue
        has_groups = isinstance(level.get("asset_groups"), list)
        has_assets = isinstance(level.get("assets"), list)
        if has_groups or has_assets:
            return (
                level.get("asset_groups") if has_groups else None,
                level.get("assets") if has_assets else None,
            )
        if isinstance(level.get("ref_assets"), list):
            qualified = _resolve_ref_assets_alias(level["ref_assets"], asset_index)
            return (None, qualified) if qualified else None
    return None


class ScopedReferenceCache:
    """Build (and memoize) scoped SymbolReferences for one game by selection.

    Reused across tests so the same selection is only loaded from disk once.
    ``for_selection(None, None)`` returns the full pack. Cache keys preserve order
    (image order is meaningful in the prompt) and distinguish ``None`` (unset/full
    pack) from ``[]`` (explicitly empty).
    """

    def __init__(self, game: str, base_dir: Path) -> None:
        self._game = game
        self._base_dir = base_dir
        self._cache: dict[
            tuple[tuple[str, ...] | None, tuple[str, ...] | None], SymbolReference
        ] = {}

    def for_selection(
        self, asset_groups: list[str] | None, assets: list[str] | None
    ) -> SymbolReference:
        key = (
            tuple(asset_groups) if asset_groups is not None else None,
            tuple(assets) if assets is not None else None,
        )
        if key not in self._cache:
            self._cache[key] = load_reference_selection(
                self._game, self._base_dir, asset_groups, assets
            )
        return self._cache[key]

    def scoped(self, categories: list[str] | None) -> SymbolReference:
        """Back-compat: a whole-section (category) list, or None for the full pack."""
        return self.for_selection(categories, None)
