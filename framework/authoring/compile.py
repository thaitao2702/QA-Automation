"""Compile a readable ``{suite}.flow.yml`` into the runnable ``{suite}.yml``.

CLI:
    python -m framework.authoring.compile --game zeus --suite test
    python -m framework.authoring.compile --game zeus --suite test --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from framework.authoring import emit
from framework.authoring.errors import FlowCompileError
from framework.authoring.expander import Expander
from framework.authoring.loader import (
    compiled_path,
    flow_path,
    load_flow,
    load_vocab,
)


def compile_flow(
    game: str,
    suite: str,
    base_dir: Path | None = None,
    *,
    write: bool = True,
) -> str:
    """Compile the flow file and (optionally) write ``{suite}.yml``. Returns the YAML text.

    Raises FlowCompileError on any vocab/parse/expand/schema problem (nothing is
    written in that case).
    """
    flow_p = flow_path(game, suite, base_dir)
    vocab = load_vocab(game, base_dir)
    data, line_map = load_flow(flow_p)

    expander = Expander(data, line_map, vocab, source=flow_p.name)
    result = expander.run()

    emit.validate(result, source=flow_p.name)
    regen = f"python -m framework.authoring.compile --game {game} --suite {suite}"
    text = emit.render(result, emit.header_lines(flow_p.name, regen))

    if write:
        compiled_path(game, suite, base_dir).write_text(text, encoding="utf-8")
    return text


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a readable .flow.yml into a runnable suite YAML")
    parser.add_argument("--game", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--base-dir", default=None, help="repo root override (tests/CI)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compile + validate, diff against the existing .yml, exit 1 on drift (no write)",
    )
    args = parser.parse_args(argv)
    base = Path(args.base_dir) if args.base_dir else None

    try:
        text = compile_flow(args.game, args.suite, base, write=not args.check)
    except FlowCompileError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_p = compiled_path(args.game, args.suite, base)
    if args.check:
        existing = out_p.read_text(encoding="utf-8") if out_p.exists() else ""
        if existing != text:
            print(f"DRIFT: {out_p} is out of date — run the compiler to regenerate.", file=sys.stderr)
            return 1
        print(f"OK: {out_p.name} matches its .flow.yml source.")
        return 0

    print(f"Compiled -> {out_p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
