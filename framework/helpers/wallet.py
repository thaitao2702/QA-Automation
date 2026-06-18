"""
Wallet helper for the QA platform test runner.

Wraps the standalone Playsite ``WalletClient`` (``playsite/wallet_client.py``) so a
test suite can read and change the player's balance from a YAML step — the wallet
analogue of :class:`framework.helpers.bo.BoHelper`. It powers the
``wallet_balance`` / ``wallet_deposit`` / ``wallet_withdraw`` step actions.

The underlying client maps 1:1 to the backend the Playsite front-end uses:

    get_balance ->  GET  {base}/user/getBalance
    deposit     ->  POST {base}/wallet/deposit    {amount, currency}
    withdraw    ->  POST {base}/wallet/withdraw    {amount, currency}

Configuration
-------------
Wallet config reuses the same Playsite values the URL resolver already uses, so a
suite that launches via the Playsite login flow needs no extra config:

* **Environment-scoped** endpoint + credentials come from the per-environment .env
  files (base ``.env`` + ``.env.{suffix}`` overlay — see ``framework/env_files.py``)::

      PLAYSITE_API_URL   backend API base URL for this environment
      PLAYSITE_USER      player username
      PLAYSITE_PASS      player password

* **Default currency** is taken from the game's ``playsite:`` block in
  ``games/{game}/env.config.{suffix}.yml`` (``playsite.currency``), and a
  ``PLAYSITE_CURRENCY`` environment variable overrides it. A step may always pass an
  explicit ``currency`` param, which wins over both.

Construction is cheap and never touches the network; the client is built and logged
in lazily on the first wallet call, and missing credentials raise only then (so a
suite without wallet steps runs fine without Playsite config).

Public interface
----------------
WalletHelper(game, env, base_dir=None)
    async get_balance(currency=None) -> float | list[dict]
        currency given (or a default configured) -> the numeric balance for it;
        otherwise the full list of the player's wallets.
    async deposit(amount, currency=None) -> dict   # updated wallet {currency, balance}
    async withdraw(amount, currency=None) -> dict   # updated wallet {currency, balance}
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from framework.env_files import env_file_suffix, load_env_files, load_game_config

logger = logging.getLogger(__name__)

# Repo root: framework/helpers/wallet.py -> framework -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The Playsite client modules live in playsite/ and import each other by bare module
# name (e.g. ``from playsite_client import ...``), so they must be importable as
# top-level modules — mirror scripts/get_game_url_playsite.py and put playsite/ on
# sys.path rather than treating it as a package.
_PLAYSITE_DIR = _REPO_ROOT / "playsite"
if str(_PLAYSITE_DIR) not in sys.path:
    sys.path.insert(0, str(_PLAYSITE_DIR))

from wallet_client import WalletClient  # noqa: E402  (after sys.path insert)
from playsite_client import PlaysiteError  # noqa: E402  (after sys.path insert)

Amount = int | float | str


class WalletConfigError(RuntimeError):
    """Raised when a wallet step runs but required config/credentials are missing."""


class WalletHelper:
    """Expose Playsite wallet operations (balance / deposit / withdraw) to the runner."""

    def __init__(
        self,
        game: str,
        env: str,
        base_dir: Path | None = None,
    ) -> None:
        self.game = game
        self.env = env
        self.base_dir = Path(base_dir).resolve() if base_dir else _REPO_ROOT

        # Ensure the per-environment .env overlay is loaded (idempotent — the suite
        # runner also loads it; this makes standalone WalletHelper usage self-sufficient).
        load_env_files(self.env, self.base_dir)

        # Environment-scoped endpoint + credentials (from .env / .env.{suffix}).
        self._base_url: str | None = os.environ.get("PLAYSITE_API_URL", "").strip() or None
        self._username: str | None = os.environ.get("PLAYSITE_USER", "").strip() or None
        self._password: str | None = os.environ.get("PLAYSITE_PASS", "").strip() or None

        # Default currency: PLAYSITE_CURRENCY env overrides the game's playsite.currency.
        env_currency = os.environ.get("PLAYSITE_CURRENCY", "").strip()
        self._default_currency: str | None = (
            env_currency or self._load_game_currency() or None
        )

        # Lazily built, logged-in client (guarded against concurrent first-use).
        self._client_instance: WalletClient | None = None
        self._login_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_game_currency(self) -> str | None:
        """Read ``playsite.currency`` from the per-environment game config.

        Missing file/section -> None (currency then comes from PLAYSITE_CURRENCY or a
        step param). Mirrors BoHelper's game-config read.
        """
        try:
            config = load_game_config(self.game, self.env, self.base_dir)
        except (FileNotFoundError, ValueError) as exc:
            logger.debug("Could not read playsite currency from game config: %s", exc)
            return None
        playsite = config.get("playsite") or {}
        currency = playsite.get("currency")
        return str(currency).strip() if currency not in (None, "") else None

    def _resolve_currency(self, currency: str | None) -> str:
        """A step's explicit currency wins; else the configured default. Required."""
        cur = (currency or self._default_currency or "").strip()
        if not cur:
            raise WalletConfigError(
                "No currency for the wallet step — pass params.currency, or set "
                f"playsite.currency in games/{self.game}/env.config."
                f"{env_file_suffix(self.env)}.yml (or the PLAYSITE_CURRENCY env var)."
            )
        return cur

    # ------------------------------------------------------------------
    # Client (lazy, logged in once)
    # ------------------------------------------------------------------

    def _client(self) -> WalletClient:
        """Build + log in the WalletClient on first use; raise if creds are missing.

        Runs the blocking login under a lock so concurrent first-use wallet steps log
        in exactly once.
        """
        if self._client_instance is not None:
            return self._client_instance
        with self._login_lock:
            if self._client_instance is not None:  # set while we waited for the lock
                return self._client_instance
            if not self._username or not self._password:
                raise WalletConfigError(
                    "Playsite credentials missing — set PLAYSITE_USER and PLAYSITE_PASS "
                    f"in .env.{env_file_suffix(self.env)} (root config)."
                )
            client = WalletClient(base_url=self._base_url)
            client.login(self._username, self._password)
            logger.info("Wallet: logged in as %s @ %s", client.username, client.base_url)
            self._client_instance = client
            return self._client_instance

    # ------------------------------------------------------------------
    # Operations (async; the blocking HTTP runs in a worker thread)
    # ------------------------------------------------------------------

    async def get_balance(self, currency: str | None = None) -> Any:
        """Return the numeric balance for *currency* (or the configured default), or
        the full list of the player's wallets when no currency is in effect.

        Propagates PlaysiteError / WalletConfigError to the caller.
        """
        cur = (currency or self._default_currency or "").strip()
        if cur:
            return await asyncio.to_thread(self._balance_for_sync, cur)
        return await asyncio.to_thread(self._all_balances_sync)

    async def deposit(self, amount: Amount, currency: str | None = None) -> dict[str, Any]:
        """Deposit *amount* into the *currency* wallet; return the updated wallet data."""
        cur = self._resolve_currency(currency)
        return await asyncio.to_thread(self._op_sync, "deposit", amount, cur)

    async def withdraw(self, amount: Amount, currency: str | None = None) -> dict[str, Any]:
        """Withdraw *amount* from the *currency* wallet; return the updated wallet data."""
        cur = self._resolve_currency(currency)
        return await asyncio.to_thread(self._op_sync, "withdraw", amount, cur)

    async def set_balance(self, target: Amount, currency: str | None = None) -> dict[str, Any]:
        """Set the *currency* wallet to exactly *target* by depositing/withdrawing the
        difference from the current balance. Returns the updated wallet data.

        Robust to the current balance being anything (it reads it first), so this is
        the reliable primitive for forcing a known balance for a test AND for restoring
        the original balance in teardown — even if the test itself changed the balance.
        Propagates PlaysiteError / WalletConfigError.
        """
        cur = self._resolve_currency(currency)
        return await asyncio.to_thread(self._set_sync, target, cur)

    # -- blocking bodies, run via asyncio.to_thread --------------------------

    def _all_balances_sync(self) -> list[dict[str, Any]]:
        return self._client().get_balance()

    def _balance_for_sync(self, currency: str) -> Any:
        return self._client().get_balance_for(currency)

    def _op_sync(self, op: str, amount: Amount, currency: str) -> dict[str, Any]:
        client = self._client()
        return (client.deposit if op == "deposit" else client.withdraw)(amount, currency)

    def _set_sync(self, target: Amount, currency: str) -> dict[str, Any]:
        client = self._client()
        current = client.get_balance_for(currency)
        current_f = float(current) if current is not None else 0.0
        # Round the delta to cents so float dust never triggers a spurious tiny op.
        delta = round(float(target) - current_f, 2)
        if delta > 0:
            return client.deposit(delta, currency)
        if delta < 0:
            return client.withdraw(round(-delta, 2), currency)
        return {"currency": currency, "balance": current_f}  # already at target


# Surface PlaysiteError so callers can ``from framework.helpers.wallet import PlaysiteError``
# instead of reaching into the playsite package.
__all__ = ["WalletHelper", "WalletConfigError", "PlaysiteError", "Amount"]
