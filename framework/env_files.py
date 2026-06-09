"""
Per-environment .env loading for the QA platform.

Loads a shared base ``.env`` and then an environment-specific overlay
``.env.{suffix}`` that overrides it. The suffix maps the runner's env name
(as used by ``--env`` and ``env.config.yml``) to the file name — e.g. the
``staging`` environment loads ``.env.stg``.

    dev      -> .env.dev
    staging  -> .env.stg
    prod     -> .env.prod
    local    -> .env.local

Base .env holds shared secrets (e.g. GEMINI_API_KEY); the per-env overlay holds
environment-specific config such as the back-office endpoint, game identifiers,
and BO tokens.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# framework/env_files.py -> framework -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent

# env name -> .env file suffix (defaults to the env name itself when not listed)
ENV_FILE_SUFFIX: dict[str, str] = {
    "dev": "dev",
    "staging": "stg",
    "stg": "stg",
    "prod": "prod",
    "local": "local",
}


def env_file_suffix(env: str) -> str:
    """Return the .env file suffix for an environment name."""
    return ENV_FILE_SUFFIX.get(env, env)


def game_config_path(game: str, env: str, base_dir: Path | str | None = None) -> Path:
    """
    Return the per-environment game config file path.

    Game config is split per environment using the same suffix mapping as the
    .env overlays, e.g. ``--env staging`` -> ``games/{game}/env.config.stg.yml``.
    Each file is self-contained (only that environment's config) — there is no
    shared base file and no ``environments:`` wrapper.
    """
    root = Path(base_dir).resolve() if base_dir else _REPO_ROOT
    return root / "games" / game / f"env.config.{env_file_suffix(env)}.yml"


def load_game_config(game: str, env: str, base_dir: Path | str | None = None) -> dict:
    """
    Load and parse the per-environment game config (see ``game_config_path``).

    Returns the parsed top-level mapping for the environment. Raises
    ``FileNotFoundError`` if the file is missing and ``ValueError`` if it is not
    a YAML mapping or cannot be parsed.
    """
    import yaml

    path = game_config_path(game, env, base_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"Game config not found: {path}. Expected a per-environment file at "
            f"games/{game}/env.config.{env_file_suffix(env)}.yml"
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse game config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"Invalid game config {path}: top-level must be a YAML mapping")
    return config


def load_env_files(env: str, base_dir: Path | str | None = None) -> list[str]:
    """
    Load base ``.env`` then the ``.env.{suffix}`` overlay (overriding base).

    Idempotent and safe to call repeatedly. Returns the list of file names that
    were actually loaded (for logging).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv not installed; skipping .env loading")
        return []

    root = Path(base_dir).resolve() if base_dir else _REPO_ROOT
    loaded: list[str] = []

    base = root / ".env"
    if base.exists():
        load_dotenv(base)
        loaded.append(base.name)

    overlay = root / f".env.{env_file_suffix(env)}"
    if overlay.exists():
        load_dotenv(overlay, override=True)
        loaded.append(overlay.name)

    return loaded
