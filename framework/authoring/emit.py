"""Validate the expanded suite and render it as the runnable ``{suite}.yml``.

The expander leaves every step ``id`` as a unique ``__S{n}__`` placeholder. Here
we (1) build the real-id document and run it through the existing
``schema_validator.validate_suite`` — never writing a file that would fail
validation — and (2) dump the placeholder document and swap each placeholder line
for the real id plus a ``# <- "<verb>" (file:Ln)`` provenance comment. Placeholders
are globally unique, so the swap is exact and order-independent even though real
step ids repeat across scopes.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from framework.authoring.errors import FlowCompileError
from framework.authoring.expander import ExpandResult
from framework.schema_validator import validate_suite

_ID_LINE = re.compile(r"^(?P<indent>\s*)-\s+id:\s+(?P<pid>__S\d+__)\s*$")


def _to_real(obj: Any, real_ids: dict[str, str]) -> Any:
    """Deep-copy, replacing placeholder ``id`` values with their real ids."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "id" and isinstance(v, str) and v in real_ids:
                out[k] = real_ids[v]
            else:
                out[k] = _to_real(v, real_ids)
        return out
    if isinstance(obj, list):
        return [_to_real(x, real_ids) for x in obj]
    return obj


def validate(result: ExpandResult, source: str) -> dict:
    """Validate the real-id document; raise FlowCompileError on any schema error."""
    real_doc = _to_real(result.doc, result.real_ids)
    errors = validate_suite(real_doc, source=source)
    if errors:
        lines = [f"compiled suite failed schema validation ({len(errors)} error(s)):"]
        lines.extend(f"  - {e.path}: {e.message}" for e in errors)
        raise FlowCompileError("\n".join(lines), source=source)
    return real_doc


def _dump(doc: dict) -> str:
    return yaml.safe_dump(
        doc,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
        indent=2,
    )


def render(result: ExpandResult, header: list[str]) -> str:
    """Render final YAML text: header + dumped body with ids + provenance."""
    body = _dump(result.doc)
    out_lines: list[str] = list(header)
    replaced = 0
    for line in body.splitlines():
        m = _ID_LINE.match(line)
        if m:
            pid = m.group("pid")
            indent = m.group("indent")
            real = result.real_ids.get(pid, pid)
            prov = result.prov.get(pid)
            if prov:
                out_lines.append(f"{indent}# <- {prov}")
            out_lines.append(f"{indent}- id: {real}")
            replaced += 1
        else:
            out_lines.append(line)
    if replaced != len(result.real_ids):
        # Defensive: every placeholder must appear exactly once in the dump.
        raise FlowCompileError(
            f"internal: matched {replaced} step id lines but expected {len(result.real_ids)}"
        )
    text = "\n".join(out_lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def header_lines(source_flow: str, regen_cmd: str) -> list[str]:
    bar = "# " + "=" * 75
    return [
        bar,
        "# GENERATED FILE - DO NOT EDIT. Edit the .flow.yml source and recompile.",
        f"# Source: {source_flow}",
        f"# Regenerate: {regen_cmd}",
        bar,
        "",
    ]
