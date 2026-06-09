"""
Royaledge backoffice API client with automatic token refresh.

Replicates the frontend base-query behaviour
(src/components/fe_foundation/lib/api.tsx:175-338):

  - Every request body carries a `traceId` (uuid4).
  - Token expiry is detected by the response body field
    `messageCode == "TOKEN_EXPIRED"` (NOT by HTTP 401).
  - On expiry: POST `auth/refresh-token` with {traceId, refreshToken}.
        success  -> status in ("success", "SC_OK"); new token at data.data.token
        store new token, retry the original request once.
        failure  -> raise AuthError (frontend logs the user out here).
  - Tokens map to the frontend storage keys: ctoken (access), rtoken (refresh).

Requirements: pip install requests
"""

import logging
import threading
import uuid

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://staging-boapi.royaledge.io/sas-web/api/v1"

# messageCode values the frontend treats specially
TOKEN_EXPIRED = "TOKEN_EXPIRED"
UNAUTHORIZED = "UNAUTHORIZED_USER"
SUCCESS_STATUSES = ("success", "SC_OK")


class AuthError(Exception):
    """Raised when the session is invalid and cannot be refreshed (re-login needed)."""


class ApiError(Exception):
    """Raised when an API call returns a non-success status."""


class BoApiClient:
    """
    Thin client that handles auth + transparent token refresh.

    Usage:
        client = BoApiClient(token="<ctoken>", refresh_token="<rtoken>")
        data = client.post("game/view", {"gameId": 123, "sasEntityId": 1})

    Pass `on_token_refresh(new_token)` to persist the rotated token
    (e.g. write it back to a file / DB) so it survives across runs.
    """

    def __init__(
        self,
        token,
        refresh_token,
        api_base=API_BASE,
        on_token_refresh=None,
        lang="en",
        timeout=90,
    ):
        self.token = token
        self.refresh_token = refresh_token
        self.api_base = api_base.rstrip("/")
        self.on_token_refresh = on_token_refresh
        self.lang = lang
        self.timeout = timeout
        self._session = requests.Session()
        # serialize refreshes so concurrent calls don't refresh multiple times
        # (mirrors the mutex in the frontend base query)
        self._refresh_lock = threading.Lock()

    # ---- internals -------------------------------------------------------
    def _headers(self):
        return {
            "authorization": f"Bearer {self.token}",
            "Accept": "*/*;",
            "Content-Type": "application/json; charset=UTF-8",
            "Lang": self.lang,
        }

    def _raw_post(self, endpoint, body):
        """Single POST: inject traceId, send, return parsed JSON."""
        payload = dict(body or {})
        payload.setdefault("traceId", str(uuid.uuid4()))
        resp = self._session.post(
            f"{self.api_base}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _refresh(self):
        """POST auth/refresh-token, update self.token. Raise AuthError on failure."""
        if not self.refresh_token:
            raise AuthError("No refresh token available — re-login required.")
        logger.info("Access token expired; refreshing.")
        result = self._raw_post(
            "auth/refresh-token", {"refreshToken": self.refresh_token}
        )
        if result.get("status") not in SUCCESS_STATUSES:
            raise AuthError(
                f"Token refresh failed: {result.get('message')} "
                f"({result.get('messageCode')})"
            )
        new_token = (result.get("data") or {}).get("token")
        if not new_token:
            raise AuthError("Token refresh response missing data.token.")
        self.token = new_token
        if self.on_token_refresh:
            self.on_token_refresh(new_token)
        logger.info("Access token refreshed.")
        return new_token

    # ---- public API ------------------------------------------------------
    def post(self, endpoint, body=None):
        """
        POST to an endpoint and return the parsed JSON body.

        On messageCode == TOKEN_EXPIRED, refreshes the access token (once)
        and retries the original request. Raises ApiError on any other
        non-success status, AuthError when the session can't be recovered.
        """
        result = self._raw_post(endpoint, body)

        if result.get("messageCode") == TOKEN_EXPIRED:
            with self._refresh_lock:
                # re-check after acquiring the lock: another thread may have
                # already refreshed while we waited.
                retry = self._raw_post(endpoint, body)
                if retry.get("messageCode") != TOKEN_EXPIRED:
                    result = retry
                else:
                    self._refresh()
                    result = self._raw_post(endpoint, body)

        message_code = result.get("messageCode")
        if message_code == UNAUTHORIZED:
            raise AuthError("Unauthorized — re-login required.")

        if result.get("status") not in SUCCESS_STATUSES:
            raise ApiError(
                f"{endpoint} failed: {result.get('message')} ({message_code})"
            )
        return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    client = BoApiClient(
        token="<paste access token (ctoken)>",
        refresh_token="<paste refresh token (rtoken)>",
        on_token_refresh=lambda t: logger.info("New token: %s...", t[:12]),
    )

    game = client.post("game/view", {"gameId": 12345, "sasEntityId": 1})
    logger.info("Game name: %s", game["data"]["name"])
