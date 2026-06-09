"""
Playsite automation client.

A small, framework-agnostic Python client over the GDP backend that the
Playsite front-end uses. It exposes the three steps an automation framework
needs:

    login  ->  choose currency  ->  get game url

Each method maps 1:1 to a backend endpoint the live Playsite calls:

    login()        ->  POST {base}/auth/login        {username, password}
    get_currencies ->  GET  {base}/user/getBalance    (player wallets)
    get_game_list  ->  GET  {base}/game/list          (game codes, needs auth)
    get_game_url   ->  POST {base}/game/openGame       {gameCode, currency, language}

Only dependency is `requests` (pip install requests).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests


# Default backend. Override with the PLAYSITE_API_URL env var or the
# `base_url` constructor argument. These mirror the front-end .env files:
#   dev      -> https://dev.royaledge.io/api
#   release  -> https://release.royaledge.io/api
DEFAULT_BASE_URL = "https://release.royaledge.io/api"


class PlaysiteError(Exception):
    """Raised when the backend returns a non-success response.

    Attributes:
        message:      Human-readable message from the backend (or a default).
        message_code: Backend error code, e.g. "SC_GAME_NOT_SUPPORTED_CURRENCY".
        status:       The backend `status` field, usually "failed".
        payload:      The full decoded JSON body, for debugging.
    """

    def __init__(
        self,
        message: str,
        message_code: Optional[str] = None,
        status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.message_code = message_code
        self.status = status
        self.payload = payload


class PlaysiteClient:
    """Stateful client for the login -> currency -> game-url flow.

    After `login()`, the access token is stored on the instance and reused by
    the authenticated calls, so a typical run is:

        client = PlaysiteClient()
        client.login("player1", "secret")
        url = client.get_game_url(game_code="MEGA888", currency="USD")
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = (
            base_url or os.environ.get("PLAYSITE_API_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

        # Populated by login()
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.username: Optional[str] = None

    # ----------------------------------------------------------------- helpers
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            raise PlaysiteError("Not logged in. Call login() first.")
        return {"Authorization": f"Bearer {self.token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers())

        resp = self._session.request(
            method,
            self._url(path),
            json=json_body,
            headers=headers,
            timeout=self.timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            raise PlaysiteError(
                f"Non-JSON response from {path} (HTTP {resp.status_code}): "
                f"{resp.text[:200]}",
                status=str(resp.status_code),
            )
        return body

    @staticmethod
    def _is_success(body: Dict[str, Any]) -> bool:
        # The backend signals success either with status == "success" or simply
        # by returning a populated `data` with no failure markers.
        status = body.get("status")
        if status is not None:
            return status == "success"
        return body.get("messageCode") is None

    # ------------------------------------------------------------------- 1. login
    def login(self, username: str, password: str) -> Dict[str, Any]:
        """Authenticate and store the access/refresh tokens on the client.

        Returns the backend `data` object (contains accessToken, refreshToken,
        and any profile fields). Raises PlaysiteError on bad credentials.
        """
        body = self._request(
            "POST",
            "/auth/login",
            json_body={"username": username, "password": password},
        )
        data = body.get("data") or {}
        access_token = data.get("accessToken")

        if not access_token:
            raise PlaysiteError(
                body.get("message") or "Login failed",
                message_code=body.get("messageCode"),
                status=body.get("status"),
                payload=body,
            )

        self.token = access_token
        self.refresh_token = data.get("refreshToken")
        self.username = username
        return data

    # -------------------------------------------------- 2. choose currency (lookup)
    def get_currencies(self) -> List[Dict[str, Any]]:
        """Return the player's wallets so a currency can be chosen.

        Each item looks like: {"currency": "USD", "balance": 1000, "isDefault": 1}
        Requires a prior login(). Currency selection itself is left to the
        caller — pass the chosen `currency` code into get_game_url().
        """
        body = self._request("GET", "/user/getBalance", auth=True)
        if not self._is_success(body):
            raise PlaysiteError(
                body.get("message") or "Failed to fetch balance/currencies",
                message_code=body.get("messageCode"),
                status=body.get("status"),
                payload=body,
            )
        data = body.get("data") or {}
        # Front-end reads wallets from data.balance
        return data.get("balance") or []

    def get_currency_codes(self) -> List[str]:
        """Convenience: just the currency codes the player holds, e.g. ['USD','THB']."""
        return [w.get("currency") for w in self.get_currencies() if w.get("currency")]

    # ----------------------------------------------------------- (optional) games
    def get_game_list(self) -> List[Dict[str, Any]]:
        """Return game categories (each with a `gameList` of {code, name, ...}).

        Useful to discover valid `gameCode` values. Requires login().
        Flatten with: [g for cat in client.get_game_list() for g in cat['gameList']]
        """
        body = self._request("GET", "/game/list", auth=True)
        data = body.get("data") or {}
        return data.get("games") or []

    def get_game_codes(self) -> List[str]:
        """Convenience: flat list of every available game code."""
        return [
            game.get("code")
            for cat in self.get_game_list()
            for game in (cat.get("gameList") or [])
            if game.get("code")
        ]

    # ------------------------------------------------------------- 3. get game url
    def get_game_url(
        self,
        game_code: str,
        currency: str,
        language: str = "en",
    ) -> str:
        """Launch a game and return its vendor URL.

        currency is required and explicit — pass one of the codes from
        get_currency_codes(). Raises PlaysiteError on failure (e.g.
        SC_GAME_NOT_SUPPORTED_CURRENCY, SC_GAME_UNDER_MAINTENANCE).
        """
        body = self._request(
            "POST",
            "/game/openGame",
            json_body={
                "gameCode": game_code,
                "currency": currency,
                "language": language,
            },
            auth=True,
        )
        data = body.get("data") or {}
        game_url = data.get("gameUrl")

        if not self._is_success(body) or not game_url:
            raise PlaysiteError(
                body.get("message") or "Failed to get game URL",
                message_code=body.get("messageCode"),
                status=body.get("status"),
                payload=body,
            )
        return game_url

    # --------------------------------------------------------- whole flow helper
    def login_and_get_game_url(
        self,
        username: str,
        password: str,
        game_code: str,
        currency: str,
        language: str = "en",
    ) -> str:
        """One-call convenience: login -> get_game_url. Returns the game URL."""
        self.login(username, password)
        return self.get_game_url(game_code, currency, language=language)


# Module-level convenience so callers don't have to build a client first.
def login_and_get_game_url(
    username: str,
    password: str,
    game_code: str,
    currency: str,
    language: str = "en",
    base_url: Optional[str] = None,
) -> str:
    """Functional shortcut for the full flow. Returns the game URL string."""
    return PlaysiteClient(base_url=base_url).login_and_get_game_url(
        username, password, game_code, currency, language=language
    )
