"""
URL resolver script for the QA platform.

Reads a game's environment config file (env.config.yml) and outputs the
fully-qualified game URL for the requested environment to stdout.

Usage:
    python scripts/get_url.py --game <game> --env <env>

Exit codes:
    0 — URL resolved successfully; URL is printed to stdout
    1 — Missing required arguments, invalid environment, or config error
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def find_env_config(game: str) -> Path:
    """Return the path to the game's environment config file.

    Searches relative to this script's repository root so the script works
    regardless of the calling working directory.
    """
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "games" / game / "env.config.yml"
    return config_path


def load_env_config(config_path: Path) -> dict:
    """Parse the YAML environment config file.

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if the file cannot be parsed as valid YAML.
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"Environment config not found: {config_path}\n"
            f"Expected file at games/<game>/env.config.yml"
        )

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse environment config {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError(
            f"Invalid environment config {config_path}: top-level must be a YAML mapping"
        )

    return config


def resolve_url(config: dict, env: str, config_path: Path) -> str:
    """Extract and resolve the game URL for the requested environment.

    The env config supports two URL resolution strategies:
      1. ``url_script`` — a shell command whose stdout is the URL.
         The framework runs it and captures output.
      2. ``url`` — a static URL string returned directly.

    Args:
        config:      Parsed env.config.yml content.
        env:         Environment name (e.g. "dev", "staging", "prod").
        config_path: Path to the config file (used in error messages).

    Returns:
        The resolved URL string.

    Raises:
        KeyError:   if the requested environment is not in the config.
        ValueError: if the environment entry is missing both url_script and url,
                    or if the url_script exits with non-zero status.
    """
    environments = config.get("environments")
    if not isinstance(environments, dict):
        raise ValueError(
            f"Invalid config {config_path}: 'environments' key must be a YAML mapping"
        )

    if env not in environments:
        available = ", ".join(sorted(environments.keys()))
        raise KeyError(
            f"Environment '{env}' not found in {config_path}.\n"
            f"Available environments: {available}"
        )

    env_entry = environments[env]
    if not isinstance(env_entry, dict):
        raise ValueError(
            f"Invalid config entry for environment '{env}' in {config_path}: "
            f"expected a YAML mapping"
        )

    # Strategy 1: static URL
    if "url" in env_entry:
        return str(env_entry["url"]).strip()

    # Strategy 2: shell command whose stdout is the URL
    if "url_script" in env_entry:
        script_cmd = env_entry["url_script"]
        result = subprocess.run(
            script_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
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
        f"Environment '{env}' in {config_path} has neither 'url' nor 'url_script' defined"
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
        config_path = find_env_config(args.game)
        config = load_env_config(config_path)
        url = resolve_url(config, args.env, config_path)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except KeyError as exc:
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
