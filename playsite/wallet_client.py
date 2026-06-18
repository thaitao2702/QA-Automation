"""
Playsite wallet client: get balance, deposit, withdraw.

Reuses the auth/login flow from playsite_client.py (same folder), so the
usage stays "login once, then steps" — you never pass a token:

    from wallet_client import WalletClient

    w = WalletClient()
    w.login("player1", "secret")        # username + password (token stored internally)
    w.get_balance()                     # -> [{"currency": "USD", "balance": 1000, "isDefault": 1}, ...]
    w.deposit(amount=100, currency="USD")
    w.withdraw(amount=50,  currency="USD")

Maps 1:1 to the backend endpoints the Playsite front-end uses:

    get_balance ->  GET  {base}/user/getBalance
    deposit     ->  POST {base}/wallet/deposit    {amount, currency}
    withdraw    ->  POST {base}/wallet/withdraw    {amount, currency}

Only dependency is `requests` (pip install requests).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from playsite_client import PlaysiteClient, PlaysiteError

# amount accepted as int, float, or numeric string
Amount = Union[int, float, str]


class WalletClient(PlaysiteClient):
    """Wallet operations on top of the shared login/auth client."""

    # ------------------------------------------------------------- get balance
    def get_balance(self) -> List[Dict[str, Any]]:
        """Return every wallet the player holds.

        Each item: {"currency": "USD", "balance": 1000, "isDefault": 1}
        Requires a prior login().
        """
        body = self._request("GET", "/user/getBalance", auth=True)
        if not self._is_success(body):
            raise PlaysiteError(
                body.get("message") or "Failed to fetch balance",
                message_code=body.get("messageCode"),
                status=body.get("status"),
                payload=body,
            )
        data = body.get("data") or {}
        # Front-end reads wallets from data.balance
        return data.get("balance") or []

    def get_balance_for(self, currency: str) -> Optional[float]:
        """Return the numeric balance for one currency, or None if no wallet."""
        for wallet in self.get_balance():
            if wallet.get("currency") == currency:
                return wallet.get("balance")
        return None

    # ----------------------------------------------------------------- deposit
    def deposit(self, amount: Amount, currency: str) -> Dict[str, Any]:
        """Deposit `amount` into the `currency` wallet.

        Returns the backend `data` (updated wallet, e.g. {"currency","balance"}).
        Raises PlaysiteError on failure.
        """
        return self._wallet_op("/wallet/deposit", amount, currency)

    # ---------------------------------------------------------------- withdraw
    def withdraw(self, amount: Amount, currency: str) -> Dict[str, Any]:
        """Withdraw `amount` from the `currency` wallet.

        Returns the backend `data` (updated wallet). Raises PlaysiteError on
        failure (e.g. insufficient balance).
        """
        return self._wallet_op("/wallet/withdraw", amount, currency)

    # ------------------------------------------------------------------ shared
    def _wallet_op(self, path: str, amount: Amount, currency: str) -> Dict[str, Any]:
        body = self._request(
            "POST",
            path,
            json_body={"amount": amount, "currency": currency},
            auth=True,
        )
        if not self._is_success(body):
            raise PlaysiteError(
                body.get("message") or f"{path} failed",
                message_code=body.get("messageCode"),
                status=body.get("status"),
                payload=body,
            )
        return body.get("data") or {}


# ----------------------------------------------------------------- module-level
def get_balance(username: str, password: str, base_url: Optional[str] = None):
    """One-shot: login then return the player's wallets."""
    w = WalletClient(base_url=base_url)
    w.login(username, password)
    return w.get_balance()


def deposit(
    username: str,
    password: str,
    amount: Amount,
    currency: str,
    base_url: Optional[str] = None,
):
    """One-shot: login then deposit. Returns the updated wallet data."""
    w = WalletClient(base_url=base_url)
    w.login(username, password)
    return w.deposit(amount, currency)


def withdraw(
    username: str,
    password: str,
    amount: Amount,
    currency: str,
    base_url: Optional[str] = None,
):
    """One-shot: login then withdraw. Returns the updated wallet data."""
    w = WalletClient(base_url=base_url)
    w.login(username, password)
    return w.withdraw(amount, currency)


# ----------------------------------------------------------------------- CLI
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Playsite wallet operations.")
    parser.add_argument("action", choices=["balance", "deposit", "withdraw"])
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--currency", help="Required for deposit/withdraw")
    parser.add_argument("--amount", help="Required for deposit/withdraw")
    parser.add_argument("--base-url", help="Else PLAYSITE_API_URL env or default")
    args = parser.parse_args()

    w = WalletClient(base_url=args.base_url)
    try:
        w.login(args.user, args.password)

        if args.action == "balance":
            for wallet in w.get_balance():
                flag = " (default)" if wallet.get("isDefault") == 1 else ""
                print(f"{wallet.get('currency')}: {wallet.get('balance')}{flag}")
        else:
            if not args.currency or args.amount is None:
                parser.error(f"{args.action} requires --currency and --amount")
            op = w.deposit if args.action == "deposit" else w.withdraw
            result = op(args.amount, args.currency)
            print(result)
        sys.exit(0)
    except PlaysiteError as e:
        print(f"ERROR: {e.message} (code={e.message_code})", file=sys.stderr)
        sys.exit(1)
