"""
Back-office (BO) API integration package.

Vendored low-level modules that talk to the Royaledge back-office API, used by the
test runner via ``framework.helpers.bo.BoHelper``:

* ``bo_api_client``        — transport + automatic token refresh (``BoApiClient``).
* ``agent_game_settings``  — per-agent paytable selection (set / reset / inspect).
* ``update_game_paytable`` — master-game paytable content (ensure / edit).

Usage examples and the end-to-end deterministic-result flow are documented in
``docs/bo-integration.md``. Public symbols are re-exported here for convenience, so
callers can ``from framework.bo import BoApiClient, ensure_game_paytable, ...``.
"""

from framework.bo.bo_api_client import ApiError, AuthError, BoApiClient
from framework.bo.agent_game_settings import (
    agent_paytable,
    build_submit_row,
    find_default_paytable,
    find_game_row,
    find_paytable_by_code,
    get_agent_currencies,
    get_agent_game_settings,
    iter_game_rows,
    reset_agent_paytable_to_default,
    set_agent_paytable,
    update_agent_game_settings,
)
from framework.bo.update_game_paytable import (
    build_update_payload,
    ensure_game_paytable,
    find_game_paytable,
    get_game_detail,
    set_paytable,
    update_game,
    update_game_paytable,
)

__all__ = [
    # transport
    "BoApiClient",
    "ApiError",
    "AuthError",
    # per-agent paytable selection
    "agent_paytable",
    "build_submit_row",
    "find_default_paytable",
    "find_game_row",
    "find_paytable_by_code",
    "get_agent_currencies",
    "get_agent_game_settings",
    "iter_game_rows",
    "reset_agent_paytable_to_default",
    "set_agent_paytable",
    "update_agent_game_settings",
    # master-game paytable content
    "build_update_payload",
    "ensure_game_paytable",
    "find_game_paytable",
    "get_game_detail",
    "set_paytable",
    "update_game",
    "update_game_paytable",
]
