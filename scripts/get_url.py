"""
URL resolver script for the QA platform.

Reads a game's per-environment config file (env.config.{suffix}.yml) and outputs
the fully-qualified game URL for the requested environment to stdout.

Usage:
    python scripts/get_url.py --game <game> --env <env>

Exit codes:
    0 — URL resolved successfully; URL is printed to stdout
    1 — Missing required arguments, invalid environment, or config error
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

# Ensure the repo root is importable when invoked as a standalone script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.env_files import game_config_path, load_env_files, load_game_config

logger = logging.getLogger(__name__)


def _url_script_env(env_entry: dict) -> dict:
    """Build the environment for a ``url_script`` subprocess.

    Starts from the current environment (root-config values from the .env
    overlay — PLAYSITE_API_URL / PLAYSITE_USER / ...) and overlays the
    game-specific ``playsite:`` block so the resolver script receives the game's
    code/currency without baking them into the url_script command line. Mirrors
    suite_runner._url_script_env.
    """
    sub_env = os.environ.copy()
    playsite = env_entry.get("playsite") or {}
    mapping = {
        "game_code": "PLAYSITE_GAME_CODE",
        "currency": "PLAYSITE_CURRENCY",
        "language": "PLAYSITE_LANGUAGE",
    }
    for key, var in mapping.items():
        value = playsite.get(key)
        if value not in (None, ""):
            sub_env[var] = str(value)
    return sub_env


def resolve_url(env_entry: dict, env: str, config_path: Path) -> str:
    """Extract and resolve the game URL from a per-environment config mapping.

    The config supports two URL resolution strategies, tried in order:
      1. ``url`` — a static URL string returned directly. Preferred when it has
         a non-empty value.
      2. ``url_script`` — a shell command whose stdout is the URL. Used as a
         fallback when ``url`` is empty or absent. The framework runs it and
         captures output.

    A config may define both keys: a non-empty ``url`` wins for a quick manual
    run, and clearing it lets ``url_script`` resolve the URL dynamically.

    Args:
        env_entry:   The parsed per-environment config mapping (the whole file).
        env:         Environment name (e.g. "dev", "staging", "prod").
        config_path: Path to the config file (used in error messages).

    Returns:
        The resolved URL string.

    Raises:
        ValueError: if the config is missing both url_script and url, or if the
                    url_script exits with non-zero status.
    """
    # Strategy 1: static URL (preferred when non-empty)
    static_url = str(env_entry.get("url") or "").strip()
    if static_url:
        return static_url

    # Strategy 2: shell command whose stdout is the URL (fallback)
    if env_entry.get("url_script"):
        script_cmd = env_entry["url_script"]
        result = subprocess.run(
            script_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            env=_url_script_env(env_entry),
        )
        if result.returncode != 0:
            raise ValueError(
                f"url_script for environment '{env}' exited with status "
                f"{result.returncode}.\n"
                f"Command: {script_cmd}\n"
                f"stderr: {result.stderr.strip()}"
            )
        url = result.stdout.strip()
        if not url:
            raise ValueError(
                f"url_script for environment '{env}' produced no output.\n"
                f"Command: {script_cmd}"
            )
        return url

    raise ValueError(
        f"Game config '{config_path}' has neither 'url' nor 'url_script' defined"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve the game URL for a given environment. "
            "Reads games/<game>/env.config.yml and prints the URL to stdout."
        ),
        epilog=(
            "Examples:\n"
            "  python scripts/get_url.py --game zeus --env dev\n"
            "  python scripts/get_url.py --game zeus --env staging"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--game",
        required=True,
        metavar="GAME",
        help="Game identifier (matches directory name under games/)",
    )
    parser.add_argument(
        "--env",
        required=True,
        metavar="ENV",
        help="Environment name (e.g. dev, staging, prod)",
    )
    return parser


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")

    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        # Load the root .env overlay so a url_script subprocess inherits the
        # shared PLAYSITE_API_URL / credentials (the runner does this too).
        load_env_files(args.env)
        config_path = game_config_path(args.game, args.env)
        config = load_game_config(args.game, args.env)
        url = resolve_url(config, args.env, config_path)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    # stdout is the contract: callers (suite_runner.py) read the URL from stdout.
    sys.stdout.write(url + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
