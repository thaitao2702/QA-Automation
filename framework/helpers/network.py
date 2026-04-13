"""
Browser network condition control via Playwright and Chrome DevTools Protocol.

Provides helpers for taking the browser context offline, restoring it, and
applying bandwidth/latency throttling via CDP Network.emulateNetworkConditions.

Public interface
----------------
NetworkHelper(context)
    Wraps a Playwright BrowserContext.

    go_offline() -> None
        Set the browser context to offline mode (all requests fail).

    go_online() -> None
        Restore network connectivity.

    throttle(cdp_session, download_throughput, upload_throughput, latency_ms) -> None
        Apply CDP network emulation (simulated slow connection).

    reset_throttle(cdp_session) -> None
        Remove CDP network emulation (restore unlimited throughput).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class NetworkHelper:
    """
    Browser network condition control.

    Parameters
    ----------
    context:
        A Playwright ``BrowserContext`` object.
    """

    def __init__(self, context: Any) -> None:
        self.context = context

    # ------------------------------------------------------------------
    # Offline / online
    # ------------------------------------------------------------------

    async def go_offline(self) -> None:
        """
        Set the browser context to offline mode.

        All network requests will fail immediately. Useful for testing
        reconnection / error handling behaviour.
        """
        logger.debug("Setting browser context to offline")
        await self.context.set_offline(True)

    async def go_online(self) -> None:
        """
        Restore browser context to online mode.

        Reverses the effect of ``go_offline()``.
        """
        logger.debug("Restoring browser context to online")
        await self.context.set_offline(False)

    # ------------------------------------------------------------------
    # CDP throttling
    # ------------------------------------------------------------------

    async def throttle(
        self,
        cdp_session: Any,
        download_throughput: int,
        upload_throughput: int,
        latency_ms: int,
    ) -> None:
        """
        Apply network throttling via CDP Network.emulateNetworkConditions.

        This simulates a slow or unreliable connection at the browser level.
        Requires a Chrome DevTools Protocol session (opened via
        ``browser_context.new_cdp_session(page)``).

        Parameters
        ----------
        cdp_session:
            An active CDP session (Playwright ``CDPSession``).
        download_throughput:
            Maximum download speed in bytes/second. Use ``-1`` for unlimited.
        upload_throughput:
            Maximum upload speed in bytes/second. Use ``-1`` for unlimited.
        latency_ms:
            Minimum round-trip latency in milliseconds. Use ``0`` for none.
        """
        logger.debug(
            "Throttling: down=%d B/s up=%d B/s latency=%dms",
            download_throughput,
            upload_throughput,
            latency_ms,
        )
        await cdp_session.send(
            "Network.emulateNetworkConditions",
            {
                "offline": False,
                "downloadThroughput": download_throughput,
                "uploadThroughput": upload_throughput,
                "latency": latency_ms,
            },
        )

    async def reset_throttle(self, cdp_session: Any) -> None:
        """
        Remove CDP network throttling (restore unlimited throughput).

        Parameters
        ----------
        cdp_session:
            The same CDP session used in ``throttle()``.
        """
        logger.debug("Resetting network throttle")
        await cdp_session.send(
            "Network.emulateNetworkConditions",
            {
                "offline": False,
                "downloadThroughput": -1,
                "uploadThroughput": -1,
                "latency": 0,
            },
        )
