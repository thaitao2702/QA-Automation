"""
Symbol / UI reference loader for AI assertions.

Builds a reusable "reference pack" from a game's ``assets.yml`` (legacy name:
``symbols.yml``) so EVERY image/video AI request can be primed with the canonical
appearance + description of each symbol and UI element BEFORE the screenshot/video
under test. This gives the model ground-truth context to identify what it is looking at.

Only assets whose reference image file actually exists on disk are included
(the manifest may list assets whose image has not been added yet).

Public interface
----------------
SymbolReference
    header: str                     intro text shown before the reference images
    items: list[(Path, str)]        (image_path, label) for each existing asset
    is_empty -> bool
    descriptions_text() -> str      header + all labels as one text block

load_symbol_reference(game, base_dir) -> SymbolReference
    Read games/{game}/docs/assets/assets.yml (or legacy symbols.yml) and build the reference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

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


def load_symbol_reference(game: str, base_dir: Path) -> SymbolReference:
    """
    Build a SymbolReference from ``games/{game}/docs/assets/assets.yml`` (or legacy ``symbols.yml``).

    Every entry under ``symbols:`` and ``ui_elements:`` that has an existing image
    file becomes one ``(image_path, label)`` item, ordered symbols-then-ui. Missing
    files are skipped (logged). Always returns a SymbolReference (possibly empty).
    """
    asset_dir = base_dir / "games" / game / "docs" / "assets"
    # Prefer the current manifest name (assets.yml); fall back to the legacy
    # symbols.yml so games onboarded before the rename keep working.
    manifest_path = asset_dir / "assets.yml"
    if not manifest_path.exists():
        manifest_path = asset_dir / "symbols.yml"
    header = _HEADER_TEMPLATE.format(game=game)

    if not manifest_path.exists():
        logger.warning("asset manifest (assets.yml / symbols.yml) not found for %s: %s", game, asset_dir)
        return SymbolReference(header=header, items=[])

    import yaml  # lazy

    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("Could not parse %s: %s", manifest_path, exc)
        return SymbolReference(header=header, items=[])

    asset_dir = manifest_path.parent
    items: list[tuple[Path, str]] = []
    missing = 0

    for section in ("symbols", "ui_elements"):
        for key, entry in (manifest.get(section) or {}).items():
            if not isinstance(entry, dict):
                continue
            file_rel = entry.get("file", "")
            if not file_rel:
                continue
            img_path = asset_dir / file_rel
            if not img_path.exists():
                missing += 1
                continue
            name = entry.get("name", key)
            desc = entry.get("description", "")
            label = f"Asset '{key}' — {name}" + (f": {desc}" if desc else "")
            items.append((img_path, label))

    logger.info(
        "Symbol reference for %s: %d asset image(s) loaded, %d missing file(s)",
        game, len(items), missing,
    )
    return SymbolReference(header=header, items=items)
