"""Load the readable flow file (with line tracking) and the vocab layers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from framework.authoring.errors import FlowCompileError
from framework.authoring.vocab import VerbTable, build_verb_table

# framework/authoring/loader.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_VOCAB = REPO_ROOT / "framework" / "authoring" / "vocab" / "core.yml"


def flow_path(game: str, suite: str, base_dir: Path | None = None) -> Path:
    root = base_dir or REPO_ROOT
    return root / "games" / game / "test-suites" / f"{suite}.flow.yml"


def compiled_path(game: str, suite: str, base_dir: Path | None = None) -> Path:
    root = base_dir or REPO_ROOT
    return root / "games" / game / "test-suites" / f"{suite}.yml"


def game_vocab_path(game: str, base_dir: Path | None = None) -> Path:
    root = base_dir or REPO_ROOT
    return root / "games" / game / "test-suites" / "vocab.yml"


# ---------------------------------------------------------------------------
# Flow parsing with line numbers
# ---------------------------------------------------------------------------


def _build_line_map(node: yaml.Node, path: tuple, out: dict[tuple, int]) -> None:
    """Map a path tuple (keys/indices) to its 1-based source line."""
    out[path] = node.start_mark.line + 1
    if isinstance(node, yaml.MappingNode):
        for key_node, val_node in node.value:
            key = key_node.value
            _build_line_map(val_node, path + (key,), out)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _build_line_map(item, path + (i,), out)


def load_flow(path: Path) -> tuple[dict, dict[tuple, int]]:
    """Return (data, line_map). line_map keys are path tuples into ``data``."""
    if not path.exists():
        raise FlowCompileError(f"flow file not found: {path}", source=path.name)
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise FlowCompileError(f"invalid YAML: {exc}", source=path.name) from exc

    if not isinstance(data, dict):
        raise FlowCompileError("flow file must be a YAML mapping", source=path.name)
    if not any(k in data for k in ("tests", "groups", "runs")):
        raise FlowCompileError(
            "flow file must define at least one of 'tests', 'groups', 'runs'", source=path.name
        )

    line_map: dict[tuple, int] = {}
    if root is not None:
        _build_line_map(root, (), line_map)
    return data, line_map


# ---------------------------------------------------------------------------
# Vocab loading
# ---------------------------------------------------------------------------


def _read_verbs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FlowCompileError(f"invalid vocab YAML: {exc}", source=path.name) from exc
    verbs = doc.get("verbs", []) if isinstance(doc, dict) else []
    if not isinstance(verbs, list):
        raise FlowCompileError("'verbs' must be a list", source=path.name)
    return verbs


def load_vocab(game: str, base_dir: Path | None = None) -> VerbTable:
    """Build the merged verb table from core + per-game vocab."""
    core = _read_verbs(CORE_VOCAB)
    if not core:
        raise FlowCompileError(f"core vocab is empty or missing: {CORE_VOCAB}")
    game_entries = _read_verbs(game_vocab_path(game, base_dir))
    return build_verb_table(core, game_entries)
