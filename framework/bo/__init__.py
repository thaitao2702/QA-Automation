"""
Back-office (BO) API integration package.

Vendored low-level modules that talk to the Royaledge back-office API, used by the
test runner via ``framework.helpers.bo.BoHelper``:

* ``bo_api_client``        — transport + automatic token refresh (``BoApiClient``).
* ``agent_game_settings``  — per-agent paytable selection (set / reset / inspect).
* ``update_game_paytable`` — master-game paytable content (ensure / edit).
* ``denominations``        — which denomination a game uses per currency: read, and assign on the
  master game or on one agent's row. (Creating/editing a denomination is a BO API this repo does
  not have yet — ``docs/bo-integration.md`` §7.)

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
from framework.bo.denominations import (
    assign_agent_denomination,
    assign_game_denomination,
    automation_denomination_name,
    create_denomination,
    ensure_agent_automation_denomination,
    ensure_automation_denomination,
    find_denomination,
    find_game_currency,
    game_sub_category_id,
    get_agent_denomination,
    get_denomination_list,
    list_game_denominations,
    reset_agent_denomination_to_default,
    set_currency_denomination,
    to_submit_denomination,
    update_denomination,
    update_denominations,
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
    # denominations — see docs/bo-integration.md §7
    "assign_agent_denomination",
    "assign_game_denomination",
    "automation_denomination_name",
    "create_denomination",
    "ensure_agent_automation_denomination",
    "ensure_automation_denomination",
    "find_denomination",
    "find_game_currency",
    "game_sub_category_id",
    "get_agent_denomination",
    "get_denomination_list",
    "list_game_denominations",
    "reset_agent_denomination_to_default",
    "set_currency_denomination",
    "to_submit_denomination",
    "update_denomination",
    "update_denominations",
    # master-game paytable content
    "build_update_payload",
    "ensure_game_paytable",
    "find_game_paytable",
    "get_game_detail",
    "set_paytable",
    "update_game",
    "update_game_paytable",
]
