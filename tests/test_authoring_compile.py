"""Golden-file + integration tests for framework/authoring/compile.py."""

import shutil
from pathlib import Path

import yaml

from framework.authoring.compile import _main, compile_flow
from framework.authoring.loader import REPO_ROOT
from framework.schema_validator import validate_suite

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "authoring"
FLOW = FIXTURES / "sample.flow.yml"
EXPECTED = FIXTURES / "sample.expected.yml"
GAME_VOCAB = REPO_ROOT / "games" / "zeus" / "test-suites" / "vocab.yml"


def _base_dir(tmp_path: Path) -> Path:
    """Build a tmp repo layout: games/zeus/test-suites/{sample.flow.yml, vocab.yml}."""
    dst = tmp_path / "games" / "zeus" / "test-suites"
    dst.mkdir(parents=True)
    shutil.copy(FLOW, dst / "sample.flow.yml")
    shutil.copy(GAME_VOCAB, dst / "vocab.yml")
    return tmp_path


def test_golden_matches(tmp_path):
    base = _base_dir(tmp_path)
    text = compile_flow("zeus", "sample", base_dir=base, write=True)
    assert text == EXPECTED.read_text(encoding="utf-8")


def test_compile_is_idempotent(tmp_path):
    base = _base_dir(tmp_path)
    a = compile_flow("zeus", "sample", base_dir=base, write=True)
    b = compile_flow("zeus", "sample", base_dir=base, write=True)
    assert a == b


def test_compiled_output_passes_schema(tmp_path):
    base = _base_dir(tmp_path)
    text = compile_flow("zeus", "sample", base_dir=base, write=True)
    doc = yaml.safe_load(text)
    assert validate_suite(doc, source="sample.yml") == []


def test_compiled_file_written_to_disk(tmp_path):
    base = _base_dir(tmp_path)
    compile_flow("zeus", "sample", base_dir=base, write=True)
    out = base / "games" / "zeus" / "test-suites" / "sample.yml"
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("# ")


def test_check_mode_detects_drift(tmp_path, capsys):
    base = _base_dir(tmp_path)
    # First compile writes the file; --check should then report OK.
    compile_flow("zeus", "sample", base_dir=base, write=True)
    rc = _main(["--game", "zeus", "--suite", "sample", "--base-dir", str(base), "--check"])
    assert rc == 0

    # Corrupt the generated file -> --check must report drift (exit 1).
    out = base / "games" / "zeus" / "test-suites" / "sample.yml"
    out.write_text("tampered\n", encoding="utf-8")
    rc = _main(["--game", "zeus", "--suite", "sample", "--base-dir", str(base), "--check"])
    assert rc == 1
