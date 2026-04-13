"""
Tests for scripts/get_url.py — URL resolver script.

Acceptance criteria:
- Running with --game and --env for a valid environment outputs a URL to stdout
  and exits with status 0.
- Running with an invalid environment name exits with non-zero status and emits
  an error message.
- Running without required arguments shows usage help and exits non-zero.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "get_url.py"


def _write_env_config(tmp_path: Path, content: dict) -> Path:
    """Write an env.config.yml for a fake game and return the config path."""
    game_dir = tmp_path / "games" / "test-game"
    game_dir.mkdir(parents=True)
    config_path = game_dir / "env.config.yml"
    with config_path.open("w") as fh:
        yaml.dump(content, fh)
    return config_path


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """
    Create a minimal fake qa-platform repo layout under tmp_path so
    get_url.py can locate games/test-game/env.config.yml.

    get_url.py computes repo_root as two levels above itself
    (scripts/ -> repo root).  We replicate that structure under tmp_path
    and patch the SCRIPT path to point there.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    # Copy the real script into the fake repo so __file__ resolves correctly
    real_src = SCRIPT.read_text(encoding="utf-8")
    fake_script = scripts_dir / "get_url.py"
    fake_script.write_text(real_src, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetUrlValid:
    """Script outputs the URL to stdout and exits 0 for a valid environment."""

    def test_static_url_printed_to_stdout(self, fake_repo: Path) -> None:
        expected_url = "https://dev.example.com/game/index.html"
        _write_env_config(
            fake_repo,
            {
                "environments": {
                    "dev": {"url": expected_url, "browser_params": "?bridge=1"},
                }
            },
        )

        result = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "get_url.py"),
             "--game", "test-game", "--env", "dev"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"
        assert result.stdout.strip() == expected_url

    def test_url_script_strategy(self, fake_repo: Path) -> None:
        expected_url = "https://staging.example.com/game/index.html"
        # url_script echoes a static URL — simple portable command
        _write_env_config(
            fake_repo,
            {
                "environments": {
                    "staging": {
                        "url_script": f"{sys.executable} -c \"import sys; sys.stdout.write('{expected_url}\\n')\"",
                        "browser_params": "?bridge=1",
                    }
                }
            },
        )

        result = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "get_url.py"),
             "--game", "test-game", "--env", "staging"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout.strip() == expected_url


class TestGetUrlInvalidEnv:
    """Script exits non-zero and emits an error when the environment is unknown."""

    def test_nonexistent_env_nonzero_exit(self, fake_repo: Path) -> None:
        _write_env_config(
            fake_repo,
            {
                "environments": {
                    "dev": {"url": "https://dev.example.com/", "browser_params": ""},
                }
            },
        )

        result = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "get_url.py"),
             "--game", "test-game", "--env", "does-not-exist"],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        # Error message should mention the unknown environment name
        combined = result.stderr + result.stdout
        assert "does-not-exist" in combined, (
            f"Expected error to mention the env name. stderr: {result.stderr}"
        )

    def test_nonexistent_env_no_url_on_stdout(self, fake_repo: Path) -> None:
        _write_env_config(
            fake_repo,
            {"environments": {"dev": {"url": "https://dev.example.com/", "browser_params": ""}}},
        )

        result = subprocess.run(
            [sys.executable, str(fake_repo / "scripts" / "get_url.py"),
             "--game", "test-game", "--env", "nonexistent"],
            capture_output=True,
            text=True,
        )

        assert result.stdout.strip() == "", "stdout should be empty on error"


class TestGetUrlMissingArgs:
    """Script shows usage help and exits non-zero when required arguments are absent."""

    def test_missing_all_args_exits_nonzero(self) -> None:
        result = _run([])
        assert result.returncode != 0

    def test_missing_env_arg_exits_nonzero(self) -> None:
        result = _run(["--game", "zeus"])
        assert result.returncode != 0

    def test_missing_game_arg_exits_nonzero(self) -> None:
        result = _run(["--env", "dev"])
        assert result.returncode != 0

    def test_missing_args_shows_usage(self) -> None:
        result = _run([])
        combined = result.stderr + result.stdout
        # argparse always puts "usage:" in its error output
        assert "usage" in combined.lower(), f"Expected usage hint. Output: {combined}"
