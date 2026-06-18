"""Vocabulary: readable verb patterns -> structured-step templates.

A vocab entry is ``{pattern: "<words and {slot:type} placeholders>", expands: [<steps>]}``
(plus an optional ``capture_alias`` marking a verb whose emitted step is recorded
under an author-chosen name). Patterns are compiled to anchored, whitespace-normalized,
case-insensitive regexes; authors never write regex.

Two layers are merged into one table: a shared ``core.yml`` and a per-game
``vocab.yml``. A per-game pattern identical to a core one *overrides* it
(intentional specialization). Duplicates *within* a layer are a load-time error.

Slot types
----------
``int``    a signed integer        -> captured as ``int``
``ident``  a new variable name     -> captured lower-cased str  (any author word)
``ref``    an existing variable    -> captured lower-cased str
``str``    a double-quoted literal -> captured str (between the quotes)
``bool``   true/false/on/off/...   -> captured as ``bool``
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from framework.authoring.errors import FlowCompileError

# A {name:type} placeholder inside a pattern.
_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*):(int|ident|ref|str|bool)\}")

# Regex fragment per slot type (formatted with the capture group name).
_TYPE_GROUP = {
    "int": r"(?P<{name}>-?\d+)",
    "ident": r"(?P<{name}>[A-Za-z_][A-Za-z0-9_]*)",
    "ref": r"(?P<{name}>[A-Za-z_][A-Za-z0-9_]*)",
    "str": r'"(?P<{name}>[^"]*)"',
    "bool": r"(?P<{name}>true|false|on|off|yes|no)",
}

_BOOL_TRUE = {"true", "on", "yes"}


def normalize(line: str) -> str:
    """Collapse runs of whitespace to single spaces and strip ends."""
    return " ".join(str(line).split())


@dataclass
class Slot:
    name: str
    type: str


@dataclass
class CompiledVerb:
    pattern: str  # original, whitespace-normalized
    regex: re.Pattern
    slots: list[Slot]
    expands: list[Any]
    specificity: int
    layer: str  # "core" | "game"
    capture_alias: str | None = None  # e.g. "{alias}" -> the ident slot to alias

    def slot_types(self) -> dict[str, str]:
        return {s.name: s.type for s in self.slots}


def compile_pattern(pattern: str) -> tuple[re.Pattern, list[Slot], int]:
    """Compile one readable pattern to (regex, slots, specificity)."""
    norm = normalize(pattern)
    parts: list[str] = ["^"]
    slots: list[Slot] = []
    literal_chars = 0
    pos = 0
    for m in _SLOT_RE.finditer(norm):
        literal = norm[pos : m.start()]
        parts.append(re.escape(literal))
        literal_chars += len(literal.replace(" ", ""))
        name, typ = m.group(1), m.group(2)
        if any(s.name == name for s in slots):
            raise FlowCompileError(f"duplicate slot {{{name}}} in pattern {pattern!r}")
        slots.append(Slot(name, typ))
        parts.append(_TYPE_GROUP[typ].format(name=name))
        pos = m.end()
    tail = norm[pos:]
    parts.append(re.escape(tail))
    literal_chars += len(tail.replace(" ", ""))
    parts.append("$")
    regex = re.compile("".join(parts), re.IGNORECASE)
    return regex, slots, literal_chars


def _coerce(value: str, typ: str, *, pattern: str) -> Any:
    if typ == "int":
        try:
            return int(value)
        except ValueError:  # pragma: no cover - guarded by regex
            raise FlowCompileError(f"expected an integer, got {value!r} in {pattern!r}")
    if typ in ("ident", "ref"):
        return value.lower()
    if typ == "bool":
        return value.lower() in _BOOL_TRUE
    return value  # str


def compile_verb(entry: dict, layer: str) -> CompiledVerb:
    if not isinstance(entry, dict) or "pattern" not in entry or "expands" not in entry:
        raise FlowCompileError(
            f"vocab entry must be a mapping with 'pattern' and 'expands', got {entry!r}"
        )
    pattern = normalize(entry["pattern"])
    regex, slots, spec = compile_pattern(pattern)
    expands = entry["expands"]
    if not isinstance(expands, list) or not expands:
        raise FlowCompileError(f"'expands' must be a non-empty list in pattern {pattern!r}")
    capture_alias = entry.get("capture_alias")
    if capture_alias is not None:
        inner = str(capture_alias).strip().strip("{}")
        if not any(s.name == inner and s.type == "ident" for s in slots):
            raise FlowCompileError(
                f"capture_alias {capture_alias!r} must reference an {{{inner}:ident}} slot "
                f"in pattern {pattern!r}"
            )
        capture_alias = inner
    return CompiledVerb(
        pattern=pattern,
        regex=regex,
        slots=slots,
        expands=expands,
        specificity=spec,
        layer=layer,
        capture_alias=capture_alias,
    )


@dataclass
class VerbTable:
    """A merged, match-ready set of compiled verbs."""

    verbs: list[CompiledVerb] = field(default_factory=list)

    def match(self, line: str) -> tuple[CompiledVerb, dict[str, Any]] | None:
        """Return (verb, captures) for the best match, or None.

        Highest specificity wins. A tie between two matching patterns is an
        ambiguous-vocab error (raised), not a silent pick.
        """
        norm = normalize(line)
        hits: list[tuple[CompiledVerb, re.Match]] = []
        for v in self.verbs:
            m = v.regex.match(norm)
            if m:
                hits.append((v, m))
        if not hits:
            return None
        hits.sort(key=lambda h: h[0].specificity, reverse=True)
        if len(hits) > 1 and hits[0][0].specificity == hits[1][0].specificity:
            raise FlowCompileError(
                f"ambiguous verb {line!r}: matches both "
                f"{hits[0][0].pattern!r} and {hits[1][0].pattern!r}",
                verb=line,
            )
        verb, m = hits[0]
        captures = {
            s.name: _coerce(m.group(s.name), s.type, pattern=verb.pattern) for s in verb.slots
        }
        return verb, captures

    def suggest(self, line: str, n: int = 3) -> list[str]:
        """Nearest patterns to an unmatched line (for error messages)."""
        patterns = [v.pattern for v in self.verbs]
        close = difflib.get_close_matches(normalize(line), patterns, n=n, cutoff=0.0)
        return close


def build_verb_table(core_entries: list[dict], game_entries: list[dict]) -> VerbTable:
    """Merge core + per-game entries; per-game overrides identical core pattern."""
    by_pattern: dict[str, CompiledVerb] = {}

    def add_layer(entries: list[dict], layer: str) -> None:
        seen: set[str] = set()
        for entry in entries:
            v = compile_verb(entry, layer)
            if v.pattern in seen:
                raise FlowCompileError(
                    f"duplicate pattern {v.pattern!r} within the {layer} vocab layer"
                )
            seen.add(v.pattern)
            by_pattern[v.pattern] = v  # game layer added second -> overrides core

    add_layer(core_entries, "core")
    add_layer(game_entries, "game")
    return VerbTable(verbs=list(by_pattern.values()))
