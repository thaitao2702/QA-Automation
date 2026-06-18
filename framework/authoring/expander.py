"""Expand readable verb scopes into structured steps.

Walks the suite tree (suite setup/teardown, tests, groups, runs) and turns each
verb line into one or more today-style step dicts. Cross-cutting concerns:

* **ids** — sequential per scope (``step-01`` …); references resolve to these.
* **placeholders** — every emitted step's ``id`` is first set to a globally-unique
  ``__S{n}__`` token, mapped to its real id. This lets the emitter inject
  provenance comments by exact, order-independent token replacement.
* **aliases** — ``take a screenshot as shot1`` / ``remember balance as before`` /
  a trailing ``… as name`` register ``name -> emitted step id`` for the owner
  (test/run/suite), referenced later as ``@name`` (id) or ``{name}`` (var).
* **@case** — a trailing ``@case ID[,ID2]`` tags the verb's assertion step(s).
* **lint guards** — no ``selectBet``; ``startAutoSpin`` needs a prior
  ``openAutoPopup`` in the same scope.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from framework.authoring.errors import FlowCompileError
from framework.authoring.vocab import CompiledVerb, VerbTable, normalize

_EXACT_VAR = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_EXACT_ALIAS = re.compile(r"^@\{([A-Za-z_][A-Za-z0-9_]*)\}$")  # template: @{slot}
_EXACT_ALIAS_DIRECT = re.compile(r"^@([A-Za-z_][A-Za-z0-9_]*)$")  # raw block: @name
_EMBED = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EMBED_ALIAS = re.compile(r"@\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EMBED_ALIAS_DIRECT = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_AS_SUFFIX = re.compile(r"^(?P<verb>.*?)\s+as\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE)
_CASE_SUFFIX = re.compile(r"^(?P<verb>.*?)\s+@case\s+(?P<ids>[^@]+)$", re.IGNORECASE)


@dataclass
class _Alias:
    real_id: str
    line: int | None


class _Scope:
    """Per-owner alias table shared across its setup/steps/teardown."""

    def __init__(self) -> None:
        self.aliases: dict[str, _Alias] = {}

    def define(self, name: str, real_id: str, line: int | None, source: str) -> None:
        if name in self.aliases:
            raise FlowCompileError(
                f"name {name!r} is already defined in this test/run "
                f"(first at line {self.aliases[name].line})",
                source=source,
                line=line,
            )
        self.aliases[name] = _Alias(real_id, line)

    def resolve_id(self, name: str, line: int | None, source: str) -> str:
        if name not in self.aliases:
            raise FlowCompileError(
                f"reference to undefined name {name!r}", source=source, line=line
            )
        return self.aliases[name].real_id


@dataclass
class ExpandResult:
    doc: dict
    real_ids: dict[str, str]  # placeholder -> real scoped id
    prov: dict[str, str]  # placeholder -> provenance text (first step of a verb only)


class Expander:
    def __init__(self, data: dict, line_map: dict, vocab: VerbTable, source: str) -> None:
        self.data = data
        self.line_map = line_map
        self.vocab = vocab
        self.source = source
        self._pid = 0
        self.real_ids: dict[str, str] = {}
        self.prov: dict[str, str] = {}

    # -- public ----------------------------------------------------------
    def run(self) -> ExpandResult:
        doc = self._expand_suite()
        return ExpandResult(doc=doc, real_ids=self.real_ids, prov=self.prov)

    # -- helpers ---------------------------------------------------------
    def _fresh_pid(self) -> str:
        self._pid += 1
        return f"__S{self._pid}__"

    def _line(self, path: tuple) -> int | None:
        return self.line_map.get(path)

    # -- suite tree ------------------------------------------------------
    def _expand_suite(self) -> dict:
        d = self.data
        out: dict[str, Any] = {}
        for key in ("suite", "game", "version"):
            if key in d:
                out[key] = d[key]
        if "config" in d:
            out["config"] = d["config"]
        if "setup" in d or "teardown" in d:
            scope = _Scope()
            if "setup" in d:
                out["setup"] = self._scope(d["setup"], ("setup",), "setup", scope)
            if "teardown" in d:
                out["teardown"] = self._scope(d["teardown"], ("teardown",), "teardown", scope)
        if "tests" in d:
            out["tests"] = [
                self._expand_test(t, ("tests", i)) for i, t in enumerate(d["tests"])
            ]
        if "groups" in d:
            out["groups"] = [
                self._expand_group(g, ("groups", i)) for i, g in enumerate(d["groups"])
            ]
        if "runs" in d:
            out["runs"] = [self._expand_run(r, ("runs", i)) for i, r in enumerate(d["runs"])]
        return out

    def _expand_test(self, test: dict, path: tuple) -> dict:
        if not isinstance(test, dict):
            raise FlowCompileError("each test must be a mapping", source=self.source)
        out: dict[str, Any] = {}
        for key in ("id", "title", "description"):
            if key in test:
                out[key] = test[key]
        scope = _Scope()
        if "setup" in test:
            out["setup"] = self._scope(test["setup"], path + ("setup",), "setup", scope)
        out["steps"] = self._scope(test.get("steps", []), path + ("steps",), "step", scope)
        if "teardown" in test:
            out["teardown"] = self._scope(test["teardown"], path + ("teardown",), "teardown", scope)
        for key in ("asset_groups", "assets"):
            if key in test:
                out[key] = test[key]
        return out

    def _expand_group(self, group: dict, path: tuple) -> dict:
        if not isinstance(group, dict):
            raise FlowCompileError("each group must be a mapping", source=self.source)
        out: dict[str, Any] = {}
        for key in ("id", "title", "asset_groups", "assets"):
            if key in group:
                out[key] = group[key]
        scope = _Scope()
        if "setup" in group:
            out["setup"] = self._scope(group["setup"], path + ("setup",), "setup", scope)
        if "tests" in group:
            out["tests"] = [
                self._expand_test(t, path + ("tests", i)) for i, t in enumerate(group["tests"])
            ]
        if "runs" in group:
            out["runs"] = [
                self._expand_run(r, path + ("runs", i)) for i, r in enumerate(group["runs"])
            ]
        if "teardown" in group:
            out["teardown"] = self._scope(
                group["teardown"], path + ("teardown",), "teardown", scope
            )
        return out

    def _expand_run(self, run: dict, path: tuple) -> dict:
        if not isinstance(run, dict):
            raise FlowCompileError("each run must be a mapping", source=self.source)
        out: dict[str, Any] = {}
        for key in ("id", "title"):
            if key in run:
                out[key] = run[key]
        cases = run.get("cases", [])
        out["cases"] = cases
        case_ids = {c["id"] for c in cases if isinstance(c, dict) and "id" in c}
        scope = _Scope()
        if "setup" in run:
            out["setup"] = self._scope(run["setup"], path + ("setup",), "setup", scope)
        out["steps"] = self._scope(
            run.get("steps", []), path + ("steps",), "step", scope, case_ids=case_ids
        )
        if "teardown" in run:
            out["teardown"] = self._scope(run["teardown"], path + ("teardown",), "teardown", scope)
        for key in ("asset_groups", "assets"):
            if key in run:
                out[key] = run[key]
        return out

    # -- one scope (a list of verb/raw items) ----------------------------
    def _scope(
        self,
        items: Any,
        base: tuple,
        prefix: str,
        scope: _Scope,
        case_ids: set[str] | None = None,
    ) -> list[dict]:
        if items is None:
            return []
        if not isinstance(items, list):
            raise FlowCompileError(
                f"expected a list of steps, got {type(items).__name__}",
                source=self.source,
                line=self._line(base),
            )
        out: list[dict] = []
        counter = {"n": 0}
        seen_auto_popup = False
        for i, item in enumerate(items):
            line = self._line(base + (i,))
            if isinstance(item, dict) and "raw" in item:
                steps = self._emit_raw(item, prefix, counter, scope, line)
            elif isinstance(item, str):
                steps = self._emit_verb(item, prefix, counter, scope, case_ids, line)
            else:
                raise FlowCompileError(
                    "a step must be a verb string or a {raw: ...} mapping",
                    source=self.source,
                    line=line,
                )
            # lint guards across the emitted steps
            for s in steps:
                self._lint(s, seen_auto_popup, line)
                if _bridge_name(s) == "openAutoPopup":
                    seen_auto_popup = True
            out.extend(steps)
        return out

    # -- raw passthrough -------------------------------------------------
    def _emit_raw(
        self, item: dict, prefix: str, counter: dict, scope: _Scope, line: int | None
    ) -> list[dict]:
        raw = item["raw"]
        if not isinstance(raw, dict):
            raise FlowCompileError("'raw' must be a mapping (a today-style step)", source=self.source, line=line)
        body = {k: v for k, v in raw.items() if k != "id"}
        body = self._substitute(body, {}, scope, line)
        counter["n"] += 1
        real = f"{prefix}-{counter['n']:02d}"
        pid = self._fresh_pid()
        step = {"id": pid, **body}
        self.real_ids[pid] = real
        self.prov[pid] = f"raw ({self.source}:{line})" if line else "raw"
        return [step]

    # -- verb expansion --------------------------------------------------
    def _emit_verb(
        self,
        raw_line: str,
        prefix: str,
        counter: dict,
        scope: _Scope,
        case_ids: set[str] | None,
        line: int | None,
    ) -> list[dict]:
        text = normalize(raw_line)

        # 1. peel off a trailing "@case ID[,ID2]"
        cases: list[str] = []
        m = _CASE_SUFFIX.match(text)
        if m:
            text = m.group("verb").strip()
            cases = [c.strip() for c in re.split(r"[,\s]+", m.group("ids").strip()) if c.strip()]

        # 2. match the verb; fall back to peeling a trailing "as <name>"
        alias_name: str | None = None
        matched = self.vocab.match(text)
        if matched is None:
            am = _AS_SUFFIX.match(text)
            if am:
                cand = self.vocab.match(am.group("verb").strip())
                if cand is not None:
                    matched = cand
                    alias_name = am.group("name").lower()
                    text = am.group("verb").strip()
        if matched is None:
            raise FlowCompileError(
                "no vocabulary verb matches this line",
                source=self.source,
                line=line,
                verb=raw_line.strip(),
                suggestions=self.vocab.suggest(text),
            )
        verb, captures = matched

        # 3. validate ref slots exist as aliases in this scope
        for slot in verb.slots:
            if slot.type == "ref":
                scope.resolve_id(captures[slot.name], line, self.source)

        # 4. substitute the template and assign ids
        expanded = self._substitute(copy.deepcopy(verb.expands), captures, scope, line)
        if not isinstance(expanded, list):
            raise FlowCompileError(f"verb {verb.pattern!r} did not expand to a list", source=self.source, line=line)
        steps: list[dict] = []
        first_real: str | None = None
        for j, raw_step in enumerate(expanded):
            if not isinstance(raw_step, dict):
                raise FlowCompileError(
                    f"verb {verb.pattern!r} expanded to a non-mapping step", source=self.source, line=line
                )
            counter["n"] += 1
            real = f"{prefix}-{counter['n']:02d}"
            pid = self._fresh_pid()
            step = {"id": pid, **{k: v for k, v in raw_step.items() if k != "id"}}
            self.real_ids[pid] = real
            if j == 0:
                first_real = real
                self.prov[pid] = f'"{text}" ({self.source}:{line})' if line else f'"{text}"'
            steps.append(step)

        # 5. register an alias (capture_alias or trailing "as name")
        defined = alias_name
        if verb.capture_alias is not None:
            defined = captures[verb.capture_alias]
        if defined is not None and first_real is not None:
            scope.define(defined, first_real, line, self.source)

        # 6. apply @case to the assertion-bearing emitted steps
        if cases:
            if case_ids is None:
                raise FlowCompileError(
                    "@case is only valid on a run's steps", source=self.source, line=line, verb=raw_line.strip()
                )
            unknown = [c for c in cases if c not in case_ids]
            if unknown:
                raise FlowCompileError(
                    f"@case references unknown case id(s): {', '.join(unknown)}",
                    source=self.source,
                    line=line,
                )
            targets = [s for s in steps if _is_assertion_step(s)]
            if not targets:
                raise FlowCompileError(
                    f"@case on a verb that makes no assertion: {text!r}",
                    source=self.source,
                    line=line,
                    verb=raw_line.strip(),
                )
            tag: Any = cases[0] if len(cases) == 1 else cases
            for s in targets:
                s["case"] = copy.deepcopy(tag)

        return steps

    # -- template substitution -------------------------------------------
    def _substitute(self, value: Any, captures: dict, scope: _Scope, line: int | None) -> Any:
        if isinstance(value, dict):
            return {k: self._substitute(v, captures, scope, line) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute(v, captures, scope, line) for v in value]
        if isinstance(value, str):
            return self._substitute_str(value, captures, scope, line)
        return value

    def _substitute_str(self, s: str, captures: dict, scope: _Scope, line: int | None) -> Any:
        m = _EXACT_VAR.match(s)
        if m:  # whole value is {name} -> typed capture (strict)
            name = m.group(1)
            if name not in captures:
                raise FlowCompileError(f"template references unknown slot {{{name}}}", source=self.source, line=line)
            return captures[name]
        m = _EXACT_ALIAS.match(s)
        if m:  # whole value is @{slot} -> resolved step id (template)
            return scope.resolve_id(captures.get(m.group(1), m.group(1)), line, self.source)
        m = _EXACT_ALIAS_DIRECT.match(s)
        if m:  # whole value is @name -> resolved step id (raw block)
            return scope.resolve_id(m.group(1).lower(), line, self.source)

        def repl_alias_slot(mo: re.Match) -> str:
            return scope.resolve_id(captures.get(mo.group(1), mo.group(1)), line, self.source)

        def repl_alias_direct(mo: re.Match) -> str:
            name = mo.group(1).lower()
            # Lenient: only substitute when it resolves (don't clobber prose '@').
            return scope.aliases[name].real_id if name in scope.aliases else mo.group(0)

        def repl_var(mo: re.Match) -> str:
            name = mo.group(1)
            # Lenient: leave unknown braces (e.g. literal text in a raw prompt) as-is.
            return str(captures[name]) if name in captures else mo.group(0)

        s = _EMBED_ALIAS.sub(repl_alias_slot, s)
        s = _EMBED_ALIAS_DIRECT.sub(repl_alias_direct, s)
        s = _EMBED.sub(repl_var, s)
        return s

    # -- lint guards -----------------------------------------------------
    def _lint(self, step: dict, seen_auto_popup: bool, line: int | None) -> None:
        name = _bridge_name(step)
        if name == "selectBet":
            raise FlowCompileError(
                "selectBet is forbidden in flows; use 'increase/decrease bet' or 'go to min bet'",
                source=self.source,
                line=line,
            )
        if name == "startAutoSpin" and not seen_auto_popup:
            raise FlowCompileError(
                "startAutoSpin must be preceded by 'open the auto popup' in the same scope",
                source=self.source,
                line=line,
            )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _bridge_name(step: dict) -> str | None:
    if step.get("action") in ("bridge_action", "bridge_assert"):
        params = step.get("params") or {}
        return params.get("name") or params.get("method")
    return None


def _is_assertion_step(step: dict) -> bool:
    action = step.get("action")
    if action == "bridge_assert":
        return True
    if action == "assert" or "assert" in step:
        return True
    return False
