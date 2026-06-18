"""Readable-YAML test authoring.

QA authors a ``{suite}.flow.yml`` of one-line verbs; :func:`compile_flow`
expands them against a vocabulary and emits today's runnable ``{suite}.yml``
(validated by ``framework.schema_validator.validate_suite``). The compiled
YAML is unchanged in form, so the runner needs no changes.

See ``docs/authoring-flow.md`` for the readable schema and verb catalog.
"""

from framework.authoring.errors import FlowCompileError

__all__ = ["FlowCompileError", "compile_flow"]


def compile_flow(*args, **kwargs):
    # Lazy import to keep ``import framework.authoring`` cheap and avoid pulling
    # in PyYAML/validator at package import time.
    from framework.authoring.compile import compile_flow as _impl

    return _impl(*args, **kwargs)
