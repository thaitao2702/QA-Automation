"""
Tests for framework/helpers/wallet.py (WalletHelper).

Acceptance:
- Lazily logs in exactly once and reuses the client across calls.
- A step's explicit currency overrides the configured default.
- get_balance returns a numeric balance when a currency is in effect, else all wallets.
- deposit/withdraw forward the amount + resolved currency to the client.
- Missing currency or missing credentials raise WalletConfigError (never silently).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from framework.helpers.wallet import WalletConfigError, WalletHelper


def _run(coro):
    # Reuse the shared loop (matches test_step_executor) instead of asyncio.run(),
    # which would close the loop and break other tests collected in the same process.
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeClient:
    """Stand-in for the Playsite WalletClient (no network)."""

    instances: list["_FakeClient"] = []

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url
        self.username: str | None = None
        self.logins = 0
        self.ops: list[tuple] = []
        _FakeClient.instances.append(self)

    def login(self, username: str, password: str) -> None:
        self.logins += 1
        self.username = username

    def get_balance(self):
        return [{"currency": "TRY", "balance": 1000, "isDefault": 1}]

    def get_balance_for(self, currency: str):
        return 1000 if currency == "TRY" else None

    def deposit(self, amount, currency: str):
        self.ops.append(("deposit", amount, currency))
        return {"currency": currency, "balance": 1000 + float(amount)}

    def withdraw(self, amount, currency: str):
        self.ops.append(("withdraw", amount, currency))
        return {"currency": currency, "balance": 1000 - float(amount)}


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr("framework.helpers.wallet.WalletClient", _FakeClient)
    return _FakeClient


def _creds(monkeypatch, currency: str | None) -> None:
    monkeypatch.setenv("PLAYSITE_API_URL", "http://api")
    monkeypatch.setenv("PLAYSITE_USER", "u")
    monkeypatch.setenv("PLAYSITE_PASS", "p")
    if currency is None:
        monkeypatch.delenv("PLAYSITE_CURRENCY", raising=False)
    else:
        monkeypatch.setenv("PLAYSITE_CURRENCY", currency)


class TestWalletHelper:
    def test_deposit_then_withdraw_logs_in_once(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency="TRY")
        w = WalletHelper("g", "dev", tmp_path)
        out1 = _run(w.deposit(100))
        out2 = _run(w.withdraw(40))
        assert out1 == {"currency": "TRY", "balance": 1100.0}
        assert out2 == {"currency": "TRY", "balance": 960.0}
        assert len(fake_client.instances) == 1            # one client built and reused
        client = fake_client.instances[0]
        assert client.logins == 1                          # logged in exactly once
        assert client.base_url == "http://api"
        assert client.ops == [("deposit", 100, "TRY"), ("withdraw", 40, "TRY")]

    def test_step_currency_overrides_default(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency="TRY")
        w = WalletHelper("g", "dev", tmp_path)
        _run(w.deposit(10, currency="USD"))
        assert fake_client.instances[0].ops == [("deposit", 10, "USD")]

    def test_balance_numeric_with_default_currency(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency="TRY")
        w = WalletHelper("g", "dev", tmp_path)
        assert _run(w.get_balance()) == 1000

    def test_balance_all_wallets_without_currency(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency=None)
        w = WalletHelper("g", "dev", tmp_path)
        assert _run(w.get_balance()) == [{"currency": "TRY", "balance": 1000, "isDefault": 1}]

    def test_deposit_without_any_currency_raises(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency=None)
        w = WalletHelper("g", "dev", tmp_path)
        with pytest.raises(WalletConfigError):
            _run(w.deposit(100))

    def test_missing_credentials_raises(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PLAYSITE_API_URL", "http://api")
        monkeypatch.delenv("PLAYSITE_USER", raising=False)
        monkeypatch.delenv("PLAYSITE_PASS", raising=False)
        monkeypatch.setenv("PLAYSITE_CURRENCY", "TRY")
        w = WalletHelper("g", "dev", tmp_path)
        with pytest.raises(WalletConfigError):
            _run(w.deposit(100))

    # set_balance reads the current balance (FakeClient.get_balance_for("TRY") == 1000)
    # and applies only the delta to land on the exact target.
    def test_set_balance_deposits_delta_when_below_target(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency="TRY")
        w = WalletHelper("g", "dev", tmp_path)
        _run(w.set_balance(1500))
        assert fake_client.instances[0].ops == [("deposit", 500.0, "TRY")]

    def test_set_balance_withdraws_delta_when_above_target(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency="TRY")
        w = WalletHelper("g", "dev", tmp_path)
        _run(w.set_balance(200))
        assert fake_client.instances[0].ops == [("withdraw", 800.0, "TRY")]

    def test_set_balance_noop_when_already_at_target(self, fake_client, monkeypatch, tmp_path: Path) -> None:
        _creds(monkeypatch, currency="TRY")
        w = WalletHelper("g", "dev", tmp_path)
        out = _run(w.set_balance(1000))
        assert fake_client.instances[0].ops == []  # no deposit/withdraw needed
        assert out == {"currency": "TRY", "balance": 1000.0}
