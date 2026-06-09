import requests


def getGameUrl(api_url: str, game_code: str, lang: str = "en") -> dict:
    """
    Play a game as guest without registration.

    Steps:
      1. Call /game/play to get a guest access token
      2. Call /game/openGame with the token to get the game URL

    Args:
        api_url:    Base API URL (e.g. "https://demo.royaledge.ventures/api")
        game_code:  The game code to open (e.g. "fire-horse")
        lang:       Language code (default "en")

    Returns:
        dict with keys:
          - success (bool)
          - game_url (str | None): the URL to open the game
          - access_token (str | None): the guest session token
          - error (str | None): error message if failed
    """
    headers = {"Content-Type": "application/json;charset=UTF-8"}

    # Call /game/play — returns guest session with game URL directly
    resp = requests.post(
        f"{api_url}/game/play",
        params={"gameCode": game_code},
        headers=headers,
    )
    resp.raise_for_status()
    result = resp.json()

    data = result.get("data")
    if not data or not data.get("url"):
        return {
            "success": False,
            "game_url": None,
            "access_token": None,
            "error": result.get("message") or result.get("messageCode") or "Guest login failed",
        }

    return {
        "success": True,
        "game_url": data["url"],
        "access_token": data.get("accessToken"),
        "error": None,
    }


if __name__ == "__main__":
    import argparse
    import sys

    # Environment → API URL mapping
    ENV_API = {
        "dev": "https://dev.royaledge.io/api",
        "staging": "https://staging.royaledge.io/api",
        "prod": "https://play.royaledge.io/api",
    }

    GAME_CODE = "sl_33_zappin_zeus"

    parser = argparse.ArgumentParser(description="Get Zeus game URL for QA testing")
    parser.add_argument("--env", required=True, choices=list(ENV_API.keys()), help="Environment")
    parser.add_argument("--base-url", default=None, help="Override the origin (scheme+host+port) of the returned URL")
    args = parser.parse_args()

    api_url = ENV_API[args.env]
    result = getGameUrl(api_url, GAME_CODE)

    if result["success"]:
        game_url = result["game_url"]
        if args.base_url:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(game_url)
            override = urlparse(args.base_url)
            game_url = urlunparse((
                override.scheme,
                override.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            ))
        # Print only the URL to stdout — suite runner captures this
        sys.stdout.write(game_url + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(f"Error: {result['error']}\n")
        sys.exit(1)
