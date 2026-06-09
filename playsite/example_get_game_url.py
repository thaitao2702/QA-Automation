"""
Runnable example: login -> choose currency -> get game url.

Usage:
    pip install requests
    python example_get_game_url.py --user player1 --password secret \
        --currency USD --game MEGA888

    # Or point at a different environment:
    PLAYSITE_API_URL=https://dev.royaledge.io/api python example_get_game_url.py ...

    # Omit --game to print the available game codes, omit --currency to print
    # the player's wallets, so you can discover valid values first.
"""

import argparse
import sys

from playsite_client import PlaysiteClient, PlaysiteError


def main() -> int:
    parser = argparse.ArgumentParser(description="Get a Playsite game URL.")
    parser.add_argument("--user", required=True, help="Player username")
    parser.add_argument("--password", required=True, help="Player password")
    parser.add_argument("--currency", help="Currency code, e.g. USD")
    parser.add_argument("--game", help="Game code, e.g. MEGA888")
    parser.add_argument("--language", default="en", help="Language code (default: en)")
    parser.add_argument(
        "--base-url",
        help="Backend base URL (else PLAYSITE_API_URL env or the built-in default)",
    )
    args = parser.parse_args()

    client = PlaysiteClient(base_url=args.base_url)

    try:
        # 1. login
        client.login(args.user, args.password)
        print(f"Logged in as {client.username}", file=sys.stderr)

        # 2. choose currency (discover options if not supplied)
        if not args.currency:
            print("Available wallets:", file=sys.stderr)
            for w in client.get_currencies():
                flag = " (default)" if w.get("isDefault") == 1 else ""
                print(f"  {w.get('currency')}: {w.get('balance')}{flag}", file=sys.stderr)
            return 0

        # Discover game codes if not supplied
        if not args.game:
            print("Available game codes:", file=sys.stderr)
            for code in client.get_game_codes():
                print(f"  {code}", file=sys.stderr)
            return 0

        # 3. get game url
        url = client.get_game_url(args.game, args.currency, language=args.language)
        print(url)  # stdout = just the URL, so it's easy to capture in a shell
        return 0

    except PlaysiteError as e:
        print(f"ERROR: {e.message} (code={e.message_code})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
