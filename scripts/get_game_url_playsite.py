"""
Resolve a game URL via the Playsite backend (login flow). Game-agnostic.

This is a project-level, drop-in ``url_script`` target for any game's
env.config.{suffix}.yml. It mirrors the runnable example in
``playsite/example_get_game_url.py`` but is wired for the QA runner:

    login  ->  choose currency  ->  get game url  ->  print URL to stdout

The suite runner captures stdout as the game URL, so this script prints ONLY the
URL on success (everything else goes to stderr) and exits non-zero on failure.

Config ownership (read top-to-bottom for each value: CLI arg -> env var):

  ROOT config — shared infra, in the per-env .env overlay (.env.dev / .env.stg /
  ...), loaded by the runner before this script is called:
    PLAYSITE_API_URL    backend API base                      (required)
    PLAYSITE_USER       player username                       (required)
    PLAYSITE_PASS       player password                       (required)

  GAME config — game-specific, in games/<game>/env.config.{suffix}.yml under a
  `playsite:` block. The runner injects these into this script's environment:
    PLAYSITE_GAME_CODE  game code   (from playsite.game_code)  (required)
    PLAYSITE_CURRENCY   currency    (from playsite.currency)   (optional; default
                                     is the player's default/first wallet)
    PLAYSITE_LANGUAGE   language    (from playsite.language)   (optional; default en)

Every value also has a matching CLI flag (--api-url / --user / --game / ...) that
overrides the env var, mainly for ad-hoc manual runs.

Usage (normally invoked by the runner via the config's url_script line):
    python scripts/get_game_url_playsite.py --env=dev
    python scripts/get_game_url_playsite.py --env=dev \
        --base-url=http://192.168.1.65:7456            # serve from a local IP
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the repo root and its `playsite/` package importable regardless of CWD.
#   scripts/get_game_url_playsite.py  ->  parents[1] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLAYSITE_DIR = _REPO_ROOT / "playsite"
for _p in (_REPO_ROOT, _PLAYSITE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from playsite_client import PlaysiteClient, PlaysiteError  # noqa: E402

from framework.env_files import env_file_suffix  # noqa: E402


def _override_origin(game_url: str, base_url: str) -> str:
    """Replace the scheme+host+port of ``game_url`` with that of ``base_url``.

    Used when the game is served from a local IP but the launch token must come
    from a remote (e.g. dev) backend.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(game_url)
    override = urlparse(base_url)
    return urlunparse(
        (override.scheme, override.netloc, parsed.path,
         parsed.params, parsed.query, parsed.fragment)
    )


def _pick_currency(client: PlaysiteClient, requested: str | None) -> str:
    """Return the currency to launch with.

    Uses ``requested`` when given; otherwise the player's default wallet, falling
    back to the first wallet. Raises PlaysiteError if the player has no wallets.
    """
    if requested:
        return requested
    wallets = client.get_currencies()
    if not wallets:
        raise PlaysiteError("Player has no wallets; set playsite.currency / PLAYSITE_CURRENCY")
    default = next((w for w in wallets if w.get("isDefault") == 1), wallets[0])
    code = default.get("currency")
    if not code:
        raise PlaysiteError("Could not determine a currency from the player's wallets")
    return code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve a game URL via the Playsite login flow (game-agnostic).",
    )
    parser.add_argument(
        "--env", required=True,
        help="Environment name (used to name the .env overlay in error messages)",
    )
    parser.add_argument("--api-url", default=None,
                        help="Backend API base (else PLAYSITE_API_URL from the root .env)")
    parser.add_argument("--user", default=None, help="Player username (else PLAYSITE_USER)")
    parser.add_argument("--password", default=None, help="Player password (else PLAYSITE_PASS)")
    parser.add_argument("--game", default=None,
                        help="Game code (else PLAYSITE_GAME_CODE, injected from playsite.game_code)")
    parser.add_argument("--currency", default=None,
                        help="Currency code (else PLAYSITE_CURRENCY; default: player's default wallet)")
    parser.add_argument("--language", default=None,
                        help="Language code (else PLAYSITE_LANGUAGE, default: en)")
    parser.add_argument("--base-url", default=None,
                        help="Override the origin (scheme+host+port) of the returned game URL")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    suffix = env_file_suffix(args.env)

    # Root config (shared infra): API base + credentials, from the .env overlay.
    api_url = args.api_url or os.environ.get("PLAYSITE_API_URL")
    user = args.user or os.environ.get("PLAYSITE_USER")
    password = args.password or os.environ.get("PLAYSITE_PASS")
    # Game config (game-specific): injected by the runner from the playsite block.
    game_code = args.game or os.environ.get("PLAYSITE_GAME_CODE")
    currency = args.currency or os.environ.get("PLAYSITE_CURRENCY")
    language = args.language or os.environ.get("PLAYSITE_LANGUAGE") or "en"

    if not api_url:
        sys.stderr.write(
            f"ERROR: missing API base. Set PLAYSITE_API_URL in the .env.{suffix} "
            "overlay (root config), or pass --api-url.\n"
        )
        return 1
    if not user or not password:
        sys.stderr.write(
            f"ERROR: missing credentials. Set PLAYSITE_USER / PLAYSITE_PASS in the "
            f".env.{suffix} overlay (root config), or pass --user / --password.\n"
        )
        return 1
    if not game_code:
        sys.stderr.write(
            "ERROR: missing game code. Set playsite.game_code in the game's "
            "env.config (it is injected as PLAYSITE_GAME_CODE), or pass --game.\n"
        )
        return 1

    client = PlaysiteClient(base_url=api_url)
    try:
        client.login(user, password)
        sys.stderr.write(f"Logged in as {client.username} @ {api_url}\n")

        currency = _pick_currency(client, currency)
        sys.stderr.write(f"Game: {game_code}  Currency: {currency}\n")

        game_url = client.get_game_url(game_code, currency, language=language)
        if args.base_url:
            game_url = _override_origin(game_url, args.base_url)
    except PlaysiteError as exc:
        sys.stderr.write(f"ERROR: {exc.message} (code={exc.message_code})\n")
        return 1

    # stdout is the contract: the suite runner reads the URL from stdout.
    sys.stdout.write(game_url + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
