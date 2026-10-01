"""
Update an agent's per-game paytable in the Royaledge backoffice
(the /agent/game/settings/update/:id page).

Mirrors the frontend flow (src/pages/Agent/GameSetting/GameSettingUpdate.tsx):

  GET   agentgamecurrency/view     -> agent's game settings list (per currency)
      body: { page, pageSize, sortBy, sortDirection,
              filters: { gameCode }, agentId, currencyId }
      NOTE: filters.gameCode is a *partial* match, so the response returns the
      whole game family (..._VIP, ..._KR, ...) — we exact-match gameCode in code.

  (pick the game row by exact gameCode, then pick a paytable from payTableList
   by its name = payTableCode, e.g. "AutomationTest" -> payTableId 362)

  POST  agentgamecurrency/update   -> { gameSettings:[row], autoFinalization, agentId }

Submit row shape (GameSettingUpdate.tsx:120-150):
  { id, status, maxPayoutCustom, override, payTableId, denominationId,
    rtpLevel, maxMultiplier, isDenomOverride, isDefaultPaytable }

Paytable flag (PAYTABLE_DEFAULT_STATUS, reversed vs denom):
  isDefaultPaytable: 1 = following the game default, 0 = overridden
  -> derived from the chosen paytable's isDefault flag, so:
       AutomationTest (isDefault=0) -> 0   (your setup payload)
       the default paytable (isDefault=1) -> 1   (your teardown payload)

Auth + token refresh is delegated to BoApiClient (bo_api_client.py).
Requirements: pip install requests
"""

import logging
from contextlib import contextmanager

from framework.bo.bo_api_client import BoApiClient

logger = logging.getLogger(__name__)

STATUS_ACTIVE = 1
STATUS_INACTIVE = 0

# PAYTABLE_DEFAULT_STATUS (GameSettingUpdate.tsx:69-72)
PAYTABLE_FOLLOWING_DEFAULT = 1  # DEFAULT
PAYTABLE_OVERRIDDEN = 0  # OVERRIDDEN

DEFAULT_AUTO_FINALIZATION = {
    "autoFinalizationStatus": 1,
    "autoFinalizationDuration": "0.2",
}


def _num(value):
    """Mimic JS Number(): drop trailing .0 so 300000.0000 -> 300000 (int)."""
    f = float(value)
    return int(f) if f.is_integer() else f


# ---------------------------------------------------------------------------
# Low-level API
# ---------------------------------------------------------------------------
def get_agent_currencies(client, agent_id):
    """POST currency/list_by_agent -> list of {id, code, ...} for the agent."""
    body = client.post("currency/list_by_agent", {"agentId": str(agent_id)})
    return body.get("data") or []


def get_agent_game_settings(
    client,
    agent_id,
    currency_id,
    game_code=None,
    page=1,
    page_size=100,
    sort_by="id",
    sort_direction="desc",
):
    """
    POST agentgamecurrency/view -> the `data` object:
      { gameSettings: [row, ...], maxPayout, total,
        autoFinalizationStatus, autoFinalizationDuration, ... }
    """
    request = {
        "page": page,
        "pageSize": page_size,
        "sortBy": sort_by,
        "sortDirection": sort_direction,
        "agentId": str(agent_id),
        "currencyId": currency_id,
    }
    if game_code:
        request["filters"] = {"gameCode": game_code}
    body = client.post("agentgamecurrency/view", request)
    return body["data"]


def update_agent_game_settings(client, agent_id, game_settings, auto_finalization=None):
    """
    POST agentgamecurrency/update with the exact submit payload.
    `game_settings` is a list of fully-built rows. Use this directly when you
    already know every field. traceId is injected automatically by the client.
    """
    payload = {
        "gameSettings": game_settings,
        "autoFinalization": auto_finalization or dict(DEFAULT_AUTO_FINALIZATION),
        "agentId": str(agent_id),
    }
    return client.post("agentgamecurrency/update", payload)


# ---------------------------------------------------------------------------
# Helpers: locate the game row and the paytable
# ---------------------------------------------------------------------------
def iter_game_rows(settings_data):
    """gameSettings may be an array or an object/map — yield the row values."""
    game_settings = settings_data.get("gameSettings") or {}
    if isinstance(game_settings, dict):
        return list(game_settings.values())
    return list(game_settings)


def find_game_row(settings_data, *, game_code=None, game_setting_id=None):
    """
    Find one game row by EXACT gameCode (preferred) or by row id.
    The GET filter is partial, so exact-matching here is required.
    """
    rows = iter_game_rows(settings_data)
    for row in rows:
        if game_code is not None and row.get("gameCode") == game_code:
            return row
        if game_setting_id is not None and row.get("id") == game_setting_id:
            return row
    available = [(r.get("id"), r.get("gameCode")) for r in rows]
    raise ValueError(
        f"Game row not found (gameCode={game_code!r}, id={game_setting_id}). "
        f"Available (id, gameCode): {available}"
    )


def find_paytable_by_code(row, paytable_code):
    """Find a paytable in row.payTableList by its name (payTableCode)."""
    for pt in row.get("payTableList") or []:
        if pt.get("payTableCode") == paytable_code:
            return pt
    available = [p.get("payTableCode") for p in (row.get("payTableList") or [])]
    raise ValueError(
        f"Paytable {paytable_code!r} not found for game {row.get('gameCode')!r}. "
        f"Available: {available}"
    )


def find_default_paytable(row, prefer_rtp_level=None):
    """
    Return the game-default paytable (isDefault == ACTIVE) from row.payTableList.
    If several defaults exist, prefer one matching prefer_rtp_level.
    """
    defaults = [
        p for p in (row.get("payTableList") or []) if p.get("isDefault") == STATUS_ACTIVE
    ]
    if not defaults:
        available = [
            (p.get("payTableId"), p.get("payTableCode"))
            for p in (row.get("payTableList") or [])
        ]
        raise ValueError(
            f"No default paytable for game {row.get('gameCode')!r}. Available: {available}"
        )
    if prefer_rtp_level:
        for p in defaults:
            if p.get("rtpLevel") == prefer_rtp_level:
                return p
    return defaults[0]


def find_current_paytable(row):
    """The paytable the row is on RIGHT NOW — the payTableList entry whose payTableId matches.

    For a change that is not about the paytable (a denomination assignment), `build_submit_row`
    still needs a paytable to rebuild the row from. Falls back to a synthetic entry carrying the
    row's own ids, so a row whose list does not include its current paytable still round-trips
    unchanged instead of being silently moved to another one.
    """
    for pt in row.get("payTableList") or []:
        if pt.get("payTableId") == row.get("payTableId"):
            return pt
    return {
        "payTableId": row.get("payTableId"),
        "isDefault": (
            STATUS_ACTIVE
            if row.get("isDefaultPaytable") == PAYTABLE_FOLLOWING_DEFAULT
            else STATUS_INACTIVE
        ),
    }


def build_submit_row(row, paytable, max_payout_default, max_payout=None,
                     denomination_id=None, is_denom_override=None):
    """
    Build one submit gameSettings row from a raw GET row + a chosen paytable,
    changing only the paytable and preserving every other current value.
    Reproduces handleOnBeforeSubmit + getInitialMaxPayout field-by-field.

    isDefaultPaytable is derived from the chosen paytable's isDefault flag.

    `max_payout` OVERRIDES the agent's max payout for this game:
      * a number -> turn the override ON and cap the payout at that amount. A forced paytable can
        award a very long bonus (a Chaos free-spin run at a 40% scatter weight retriggers for
        hundreds of spins), and the cap is what makes such a run END: the server truncates at the
        cap and flags `hasReachedMaxWin`, which the framework already treats as terminal.
      * `False` -> clear the override, restoring the agent's default cap (used by the teardown, so
        a test's cap cannot leak into the next run).
      * `None` (default) -> preserve whatever the row already has.

    `denomination_id` / `is_denom_override` do the same job for the DENOMINATION (the callers are
    in framework/bo/denominations.py). `None` preserves the row's current value, which is what
    keeps a paytable change from moving the denomination and vice versa. `is_denom_override`
    truthy = this agent OVERRIDES the master game's denomination (1); falsy = follow the master
    default (0) — the reverse reading of `isDefaultPaytable` above.
    """
    # getInitialMaxPayout (GameSettingUpdate.tsx:287-303)
    override = row.get("override") or STATUS_INACTIVE
    custom = row.get("maxPayoutCustom")
    if override == STATUS_INACTIVE or not custom or _is_nan(custom):
        effective_max = max_payout_default
    else:
        effective_max = custom
    if max_payout is False:                 # teardown: back to the agent default
        override, effective_max = STATUS_INACTIVE, max_payout_default
    elif max_payout is not None:            # setup: cap this game for the run
        override, effective_max = STATUS_ACTIVE, max_payout

    is_default_paytable = (
        PAYTABLE_FOLLOWING_DEFAULT
        if paytable.get("isDefault") == STATUS_ACTIVE
        else PAYTABLE_OVERRIDDEN
    )

    return {
        "id": row.get("id"),
        "status": STATUS_INACTIVE if not row.get("status") else STATUS_ACTIVE,
        # JS: maxPayoutCustom ? Number(...) : null
        "maxPayoutCustom": _num(effective_max) if effective_max else None,
        "override": STATUS_INACTIVE if not override else STATUS_ACTIVE,
        "payTableId": paytable.get("payTableId"),
        # `denomination_id` / `is_denom_override` default to None = PRESERVE, so a paytable change
        # never moves the denomination, and a denomination change never moves the paytable.
        "denominationId": (
            row.get("denominationId") if denomination_id is None else denomination_id
        ),
        "rtpLevel": row.get("rtpLevel"),
        # JS: maxMultiplier ? Number(...) : null  (0 -> null)
        "maxMultiplier": _num(row["maxMultiplier"]) if row.get("maxMultiplier") else None,
        "isDenomOverride": (
            row.get("isDenomOverride") if is_denom_override is None
            else (STATUS_ACTIVE if is_denom_override else STATUS_INACTIVE)
        ),
        "isDefaultPaytable": is_default_paytable,
    }


def _is_nan(value):
    try:
        float(value)
        return False
    except (TypeError, ValueError):
        return True


def _auto_finalization_from(settings_data):
    """Preserve the agent's current auto-finalization (frontend submits it too)."""
    return {
        "autoFinalizationStatus": 1 if settings_data.get("autoFinalizationStatus") else 0,
        "autoFinalizationDuration": str(
            settings_data.get("autoFinalizationDuration") or "0.2"
        ).strip(),
    }


# ---------------------------------------------------------------------------
# High-level: set a paytable by name / reset to default
# ---------------------------------------------------------------------------
def set_agent_paytable(
    client,
    agent_id,
    currency_id,
    game_code,
    paytable_code,
    auto_finalization=None,
    max_payout=None,
):
    """
    SETUP: point `game_code`'s paytable at the one named `paytable_code`
    (e.g. "AutomationTest"). Fetches current settings, exact-matches the game,
    preserves all other fields, submits one row. Returns the update response.

    `max_payout` (optional) also caps the agent's payout for this game — see build_submit_row.
    """
    data = get_agent_game_settings(client, agent_id, currency_id, game_code=game_code)
    row = find_game_row(data, game_code=game_code)
    paytable = find_paytable_by_code(row, paytable_code)
    submit_row = build_submit_row(row, paytable, data.get("maxPayout"), max_payout)
    af = auto_finalization or _auto_finalization_from(data)
    logger.info(
        "Set paytable: agent=%s game=%s id=%s -> %s (payTableId=%s, isDefaultPaytable=%s)",
        agent_id, game_code, submit_row["id"], paytable_code,
        submit_row["payTableId"], submit_row["isDefaultPaytable"],
    )
    return update_agent_game_settings(client, agent_id, [submit_row], af)


def reset_agent_paytable_to_default(
    client,
    agent_id,
    currency_id,
    game_code,
    auto_finalization=None,
):
    """
    TEARDOWN: reset `game_code`'s paytable to the game default (isDefault==1).
    The default paytable id is discovered from payTableList — no hardcoding.
    Returns the update response.
    """
    data = get_agent_game_settings(client, agent_id, currency_id, game_code=game_code)
    row = find_game_row(data, game_code=game_code)
    paytable = find_default_paytable(row, prefer_rtp_level=row.get("rtpLevel"))
    # `False` clears any max-payout override a run set, so the cap never leaks into the next test.
    submit_row = build_submit_row(row, paytable, data.get("maxPayout"), max_payout=False)
    af = auto_finalization or _auto_finalization_from(data)
    logger.info(
        "Reset paytable to default: agent=%s game=%s id=%s -> %s (payTableId=%s)",
        agent_id, game_code, submit_row["id"],
        paytable.get("payTableCode"), submit_row["payTableId"],
    )
    return update_agent_game_settings(client, agent_id, [submit_row], af)


@contextmanager
def agent_paytable(client, agent_id, currency_id, game_code, paytable_code):
    """
    Context manager for tests: set the chosen paytable on enter, always reset
    to the game default on exit (even if the test body raises).

        with agent_paytable(client, "1", 18, "SL_33_ZAPPIN_ZEUS", "AutomationTest"):
            ...  # play the game
        # paytable restored to default here
    """
    set_agent_paytable(client, agent_id, currency_id, game_code, paytable_code)
    try:
        yield
    finally:
        reset_agent_paytable_to_default(client, agent_id, currency_id, game_code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    client = BoApiClient(
        token="<paste access token (ctoken)>",
        refresh_token="<paste refresh token (rtoken)>",
    )

    AGENT_ID = "1"
    CURRENCY_ID = 18
    GAME_CODE = "SL_33_ZAPPIN_ZEUS"
    PAYTABLE_NAME = "AutomationTest"

    # Option A: explicit setup / teardown
    set_agent_paytable(client, AGENT_ID, CURRENCY_ID, GAME_CODE, PAYTABLE_NAME)
    # ... play the game ...
    reset_agent_paytable_to_default(client, AGENT_ID, CURRENCY_ID, GAME_CODE)

    # Option B: context manager (auto reset on exit)
    # with agent_paytable(client, AGENT_ID, CURRENCY_ID, GAME_CODE, PAYTABLE_NAME):
    #     ...  # play
