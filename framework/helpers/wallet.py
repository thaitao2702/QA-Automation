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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
import sys
import threading
import time
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

    def _relogin(self) -> WalletClient:
        """Throw the logged-in client away and build a fresh one."""
        with self._login_lock:
            self._client_instance = None
        return self._client()

    def _with_login_retry(self, call):
        """Run *call(client)*, logging in again once if the session has expired.

        The Playsite access token outlives a short run but NOT a long one, and the place it
        expires is the worst possible one: `restore-balance` runs last, after the scenario has
        already moved the money, so an expired token there leaves the wallet wherever the test
        left it. Observed live 2026-09-03 -- a green `freespin-zero-balance` finished with the
        balance at 111.96 instead of the 11785.03 it started from, because the teardown died with
        "Token is expired".

        The credentials are held for the whole run, so a session that has simply aged out is
        recoverable without troubling the caller: log in again and repeat the call once.
        """
        try:
            return call(self._client())
        except PlaysiteError as e:
            if "expire" not in str(getattr(e, "message", e)).lower():
                raise
            logger.info("Wallet session expired; logging in again and retrying.")
            return call(self._relogin())

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

    async def set_balance(self, target: Amount, currency: str | None = None,
                          attempts: int = 6, pause_s: float = 1.0) -> dict[str, Any]:
        """Set the *currency* wallet to exactly *target* by depositing/withdrawing the
        difference from the current balance. Returns the updated wallet data.

        Robust to the current balance being anything (it reads it first), so this is
        the reliable primitive for forcing a known balance for a test AND for restoring
        the original balance in teardown — even if the test itself changed the balance.
        Propagates PlaysiteError / WalletConfigError.

        *attempts* / *pause_s* bound the retry described in ``_set_sync``. The default is a
        few seconds, which covers an ordinary settle. Raise it for a caller that is changing
        the wallet right after a round or with a bonus open, where the server's hold takes
        appreciably longer to lift — but only where WAITING IS SAFE, i.e. where nothing can
        play while the retry loop is running.
        """
        cur = self._resolve_currency(currency)
        return await asyncio.to_thread(self._set_sync, target, cur, attempts, pause_s)

    # -- blocking bodies, run via asyncio.to_thread --------------------------

    def _all_balances_sync(self) -> list[dict[str, Any]]:
        return self._with_login_retry(lambda c: c.get_balance())

    def _balance_for_sync(self, currency: str) -> Any:
        return self._with_login_retry(lambda c: c.get_balance_for(currency))

    def _op_sync(self, op: str, amount: Amount, currency: str) -> dict[str, Any]:
        return self._with_login_retry(
            lambda c: (c.deposit if op == "deposit" else c.withdraw)(amount, currency))

    def _set_sync(self, target: Amount, currency: str,
                  attempts: int = 6, pause_s: float = 1.0) -> dict[str, Any]:
        """Move the wallet to *target*, retrying a refusal that is only momentary.

        A withdrawal can be refused as "Insufficient balance" for two quite different reasons,
        and BOTH are survivable by re-reading and trying again:

        * a RACE -- the balance fell between the read and the op (a base spin settled), so the
          amount computed from the stale read no longer fits. Re-reading recomputes it.
        * a TRANSIENT HOLD -- the wallet reports more than it will release while a round is still
          settling server-side. Measured live on dev 2026-09-03 against a pending free-spin bonus:
          `withdraw(11752.0)` was refused against a balance of exactly 11752.0 (unchanged, so not a
          race) and the SAME withdrawal succeeded seconds later at the same balance. The hold is
          small and short-lived, so waiting clears it.

        This matters for `freespin-zero-balance`, which zeroes the wallet immediately after
        `stop_client()` -- exactly when the last free-spin round is still settling. Without the
        retry that scenario cannot reach its own precondition.

        Each attempt re-reads the balance, so the amount is always computed from a fresh number.
        """
        last: PlaysiteError | None = None
        first_read: float | None = None
        for attempt in range(attempts):
            current = self._balance_for_sync(currency)
            current_f = float(current) if current is not None else 0.0
            if first_read is None:
                first_read = current_f
            # Move in THOUSANDTHS — the wallet's own precision. ⚠ A game balance carries a third
            # decimal (a 0.075 win at a 0.10 stake), and rounding the delta to the nearest CENT left
            # 15069.237 -> target 0.10 at 0.097: less than one bet, so `freespin-zero-balance` got
            # "Insufficient balance" on the one spin it had funded (2026-09-29). Playsite settles
            # 3-decimal amounts exactly (verified live 2026-09-29: withdraw 0.005 moved
            # 15072.542 -> 15072.537, deposit 0.005 moved it back). Rounding is still one-sided —
            # withdraw DOWN, deposit UP — so a balance with more decimals than that lands just ABOVE
            # the target, never below it.
            diff = Decimal(str(target)) - Decimal(str(current_f))
            step = (diff.quantize(Decimal("0.001"), rounding=ROUND_CEILING) if diff > 0
                    else -((-diff).quantize(Decimal("0.001"), rounding=ROUND_FLOOR)))
            delta = float(step)
            if delta == 0:
                return {"currency": currency, "balance": current_f}  # within a cent above target
            # Playsite accepts FRACTIONAL amounts: /wallet/deposit and /wallet/withdraw take the
            # amount as a plain JSON number and settle it to the THOUSANDTH. Verified live on dev:
            # withdraw(0.01) moved 11743.01 -> 11743.00 (2026-09-03), and withdraw(0.005) moved
            # 15072.542 -> 15072.537 exactly (2026-09-29).
            #
            # This used to raise unless the delta was a whole number of units, which made any wallet
            # sitting on a fractional balance (i.e. any wallet that had been played) impossible to
            # zero -- that guard, not the API, is what blocked CR_011's freespin-zero-balance.
            # Send an int when the delta happens to be whole, so ordinary whole-unit moves keep
            # posting exactly the payload they always did.
            amount: Amount = (int(round(abs(delta))) if round(delta) == delta
                              else round(abs(delta), 3))
            try:
                return self._op_sync("deposit" if delta > 0 else "withdraw", amount, currency)
            except PlaysiteError as e:
                last = e
                logger.info(
                    "wallet %s %s %s -> %s refused (%s); balance read %s, attempt %d/%d",
                    currency, "deposit" if delta > 0 else "withdraw", amount, target,
                    e.message, current_f, attempt + 1, attempts)
                if attempt < attempts - 1:
                    time.sleep(pause_s)
        # Out of attempts. Report the numbers, so a caller can tell a moving balance (a race that
        # never settled) from a stuck one (a hold that never cleared) without re-running anything.
        try:
            now: Any = self._balance_for_sync(currency)
        except Exception:  # noqa: BLE001 -- diagnostics must never mask the original error
            now = "unreadable"
        moved = str(now) != str(first_read)
        raise PlaysiteError(
            f"{last.message if last else 'wallet op failed'} after {attempts} attempts over "
            f"~{attempts * pause_s:.0f}s (target {target} {currency}: balance first read "
            f"{first_read}, now {now} -- "
            f"{'balance moved, an unsettled race' if moved else 'balance UNCHANGED, a hold that did not clear'})",
            message_code=getattr(last, "message_code", None),
        ) from last


# Surface PlaysiteError so callers can ``from framework.helpers.wallet import PlaysiteError``
# instead of reaching into the playsite package.
__all__ = ["WalletHelper", "WalletConfigError", "PlaysiteError", "Amount"]
