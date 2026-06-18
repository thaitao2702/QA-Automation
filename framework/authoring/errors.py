"""Error type for the flow compiler.

A single, descriptive exception so the CLI and tests can catch one thing. Every
raise carries enough context (source file, line, offending verb, suggestions or
validator errors) for a QA author to fix the ``.flow.yml`` without reading code.
"""

from __future__ import annotations


class FlowCompileError(Exception):
    """A flow file could not be compiled.

    Attributes
    ----------
    message:
        The human-readable problem.
    source:
        The ``.flow.yml`` file name (or label) the error is about.
    line:
        1-based line number in ``source`` when known, else ``None``.
    verb:
        The offending verb line when the error is about one, else ``None``.
    suggestions:
        Nearby vocabulary patterns (for an unmatched verb), else ``[]``.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        line: int | None = None,
        verb: str | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        self.message = message
        self.source = source
        self.line = line
        self.verb = verb
        self.suggestions = suggestions or []
        super().__init__(self.render())

    def render(self) -> str:
        where = self.source or "<flow>"
        if self.line is not None:
            where = f"{where}:{self.line}"
        parts = [f"{where}: {self.message}"]
        if self.verb is not None:
            parts.append(f"    verb: {self.verb!r}")
        if self.suggestions:
            parts.append("    did you mean:")
            parts.extend(f"      - {s}" for s in self.suggestions)
        return "\n".join(parts)
