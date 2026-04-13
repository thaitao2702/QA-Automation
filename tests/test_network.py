"""
Tests for framework/helpers/network.py — browser network control.

Acceptance criteria:
- Network helper correctly applies offline state to the browser context.
- Going online restores connectivity.
- Throttle applies CDP network emulation conditions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.helpers.network import NetworkHelper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context() -> MagicMock:
    """Mock Playwright BrowserContext with set_offline as AsyncMock."""
    ctx = MagicMock()
    ctx.set_offline = AsyncMock()
    ctx._impl_obj = MagicMock()
    return ctx


def _make_cdp_session() -> MagicMock:
    """Mock CDP session with send as AsyncMock."""
    session = MagicMock()
    session.send = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# NetworkHelper construction
# ---------------------------------------------------------------------------


class TestNetworkHelperConstruction:
    def test_stores_context(self) -> None:
        ctx = _make_context()
        helper = NetworkHelper(ctx)
        assert helper.context is ctx


# ---------------------------------------------------------------------------
# Offline / online
# ---------------------------------------------------------------------------


class TestOfflineOnline:
    @pytest.mark.asyncio
    async def test_go_offline_calls_set_offline_true(self) -> None:
        ctx = _make_context()
        helper = NetworkHelper(ctx)
        await helper.go_offline()
        ctx.set_offline.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_go_online_calls_set_offline_false(self) -> None:
        ctx = _make_context()
        helper = NetworkHelper(ctx)
        await helper.go_online()
        ctx.set_offline.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_go_offline_then_online_in_sequence(self) -> None:
        ctx = _make_context()
        helper = NetworkHelper(ctx)
        await helper.go_offline()
        await helper.go_online()
        assert ctx.set_offline.await_count == 2
        calls = [c.args[0] for c in ctx.set_offline.call_args_list]
        assert calls == [True, False]


# ---------------------------------------------------------------------------
# Throttle (CDP)
# ---------------------------------------------------------------------------


class TestThrottle:
    @pytest.mark.asyncio
    async def test_throttle_sends_cdp_network_emulate_command(self) -> None:
        ctx = _make_context()
        cdp = _make_cdp_session()
        helper = NetworkHelper(ctx)

        await helper.throttle(
            cdp_session=cdp,
            download_throughput=500 * 1024,
            upload_throughput=500 * 1024,
            latency_ms=100,
        )

        cdp.send.assert_awaited_once()
        call_args = cdp.send.call_args
        # Should call Network.emulateNetworkConditions
        assert "Network.emulateNetworkConditions" in call_args[0]

    @pytest.mark.asyncio
    async def test_throttle_passes_correct_cdp_params(self) -> None:
        ctx = _make_context()
        cdp = _make_cdp_session()
        helper = NetworkHelper(ctx)

        await helper.throttle(
            cdp_session=cdp,
            download_throughput=250_000,
            upload_throughput=125_000,
            latency_ms=200,
        )

        call_args = cdp.send.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params["downloadThroughput"] == 250_000
        assert params["uploadThroughput"] == 125_000
        assert params["latency"] == 200

    @pytest.mark.asyncio
    async def test_throttle_offline_flag_false_by_default(self) -> None:
        ctx = _make_context()
        cdp = _make_cdp_session()
        helper = NetworkHelper(ctx)

        await helper.throttle(
            cdp_session=cdp,
            download_throughput=100_000,
            upload_throughput=50_000,
            latency_ms=50,
        )

        call_args = cdp.send.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params.get("offline") is False

    @pytest.mark.asyncio
    async def test_reset_network_conditions(self) -> None:
        """Resetting should disable throttle (set unlimited throughput)."""
        ctx = _make_context()
        cdp = _make_cdp_session()
        helper = NetworkHelper(ctx)

        await helper.reset_throttle(cdp_session=cdp)

        cdp.send.assert_awaited_once()
        call_args = cdp.send.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        # -1 means unlimited in CDP Network.emulateNetworkConditions
        assert params["downloadThroughput"] == -1
        assert params["uploadThroughput"] == -1
        assert params["latency"] == 0
