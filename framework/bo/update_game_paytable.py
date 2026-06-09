"""
Update a single paytable on a game in the Royaledge backoffice.

Mirrors the frontend flow (src/pages/GameVendor/Game/GameUpdate.tsx):
  POST game/view        -> get full game detail
  (transform)           -> shape into game/update/v2 body
  (modify one paytable) -> replace payTable JSON for the chosen `code`
  POST game/update/v2   -> submit

Transform faithfully reproduces:
  - response->form (useEffect)          GameUpdate.tsx:152-222
  - form->submit (handleOnBeforeSubmit) GameUpdate.tsx:73-108

Auth + automatic token refresh is delegated to BoApiClient
(bo_api_client.py), which retries on messageCode == "TOKEN_EXPIRED".

Requirements: pip install requests
"""

import json
import logging

from framework.bo.bo_api_client import BoApiClient

logger = logging.getLogger(__name__)

STATUS_ACTIVE = 1  # STATUS.ACTIVE
STATUS_INACTIVE = 0  # STATUS.INACTIVE


def get_game_detail(client, game_id, sas_entity_id):
    """POST game/view -> returns the `data` object (GameDetailType)."""
    body = client.post(
        "game/view", {"gameId": game_id, "sasEntityId": sas_entity_id}
    )
    return body["data"]


def find_game_paytable(game_data, code):
    """Return the payTableInfoList item whose name (`code`) matches, else None."""
    for p in game_data.get("payTableInfoList") or []:
        if p.get("code") == code:
            return p
    return None


def build_update_payload(game_data):
    """
    Transform a game/view `data` object into a game/update/v2 request body.
    Replicates the two frontend transforms collapsed into one.
    """
    # currencyInfoList -> only {id, denominationId, isDenomOverride}, sorted by id
    currency_info_list = sorted(
        (
            {
                "id": c.get("id"),
                "denominationId": c.get("denominationId"),
                "isDenomOverride": c.get("isDenomOverride"),
            }
            for c in (game_data.get("currencyInfoList") or [])
        ),
        key=lambda c: c["id"],
    )

    # payTableInfoList -> {code, id, isDefault, payTable, rtp(scalar), status: ACTIVE}
    pay_table_info_list = [
        {
            "code": p.get("code"),
            "id": p.get("id"),
            "isDefault": p.get("isDefault"),
            "payTable": p.get("payTable"),  # JSON string, unchanged
            "rtp": p.get("rtp"),  # HIGH | MEDIUM | LOW (already scalar)
            "status": STATUS_ACTIVE,
        }
        for p in (game_data.get("payTableInfoList") or [])
    ]

    rtp = game_data.get("rtp")

    # Start from the full game object (frontend spreads `...gameData`), then
    # apply the renamed/added fields and the submit-time overrides.
    payload = dict(game_data)
    payload.update(
        {
            "id": int(game_data["id"]),
            # renamed fields the form adds (form->submit keeps them via spread):
            "categoryId": (game_data.get("gameCategory") or {}).get("id"),
            "subCategoryId": (game_data.get("gameSubCategory") or {}).get("id"),
            "maximumPayout": game_data.get("maxPayout"),
            "imageSquare": (game_data.get("assets") or {}).get("thumbnailSquare"),
            "imageLandScape": (game_data.get("assets") or {}).get("thumbnailLandscape"),
            "features": game_data.get("feature"),
            "themes": game_data.get("theme"),
            "labels": game_data.get("labels"),
            "tags": game_data.get("tags"),
            "languages": game_data.get("languages"),
            "hitrate": (
                str(game_data["hitrate"])
                if game_data.get("hitrate") is not None
                else None
            ),
            "status": STATUS_ACTIVE if game_data.get("status") == STATUS_ACTIVE else 0,
            # submit-time overrides:
            "rtp": int(rtp) if rtp else None,  # JS: rtp ? Number(rtp) : undefined
            "currencyInfoList": currency_info_list,
            "payTableInfoList": pay_table_info_list,
            # winFormula is round-tripped through a parser in the UI; the
            # original string is equivalent, so pass it through unchanged.
            "winFormula": game_data.get("winFormula"),
        }
    )
    # transient UI-only keys the frontend deletes (no-ops if absent)
    for k in (
        "currencyIds",
        "currency",
        "rtpRange",
        "gameId",
        "currencies",
        "currencyStatus",
    ):
        payload.pop(k, None)
    return payload


def set_paytable(payload, code, new_paytable):
    """
    Replace the `payTable` content of exactly one paytable, matched by `code`.
    `new_paytable` may be a dict/list (will be JSON-encoded) or an already
    JSON-encoded string.
    """
    new_value = (
        new_paytable
        if isinstance(new_paytable, str)
        else json.dumps(new_paytable, separators=(",", ":"))
    )
    matched = False
    for item in payload.get("payTableInfoList", []):
        if item.get("code") == code:
            item["payTable"] = new_value
            matched = True
            break
    if not matched:
        available = [p.get("code") for p in payload.get("payTableInfoList", [])]
        raise ValueError(f"No paytable with code={code!r}. Available: {available}")
    return payload


def update_game(client, payload):
    """POST game/update/v2 with the transformed body."""
    return client.post("game/update/v2", payload)


def _as_paytable_string(value):
    """Accept a dict/list (JSON-encode it) or an already JSON-encoded string."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def ensure_game_paytable(
    client,
    game_id,
    sas_entity_id,
    code,
    paytable_content=None,
    rtp="MEDIUM",
):
    """
    Make sure a paytable named `code` (e.g. "AutomationTest") exists on the game,
    and return its server-side id (payTableId).

    Flow:
      1. GET game/view by game id.
      2. If payTableInfoList already has a paytable with name == `code`,
         return its id.
      3. Otherwise create it (append a new payTableInfoList item with no id and
         POST game/update/v2), then GET game/view again to read back the
         server-assigned id.

    `paytable_content` is the payTable JSON for a newly created paytable
    (dict/list or JSON string). If omitted, it is cloned from the game's default
    paytable (or the first existing one) so the new paytable is valid.
    """
    game = get_game_detail(client, game_id, sas_entity_id)

    existing = find_game_paytable(game, code)
    if existing:
        logger.info("Paytable %r already exists (id=%s).", code, existing.get("id"))
        return existing.get("id")

    # --- create a new paytable -------------------------------------------
    existing_list = game.get("payTableInfoList") or []
    if paytable_content is None:
        if not existing_list:
            raise ValueError(
                f"Cannot create paytable {code!r}: no existing paytable to clone "
                f"content from, and no paytable_content provided."
            )
        source = next(
            (p for p in existing_list if p.get("isDefault") == STATUS_ACTIVE),
            existing_list[0],
        )
        paytable_content = source.get("payTable")

    payload = build_update_payload(game)
    payload["payTableInfoList"].append(
        {
            "code": code,
            "payTable": _as_paytable_string(paytable_content),
            "rtp": rtp,
            "isDefault": STATUS_INACTIVE,  # new paytable is not the game default
            "status": STATUS_ACTIVE,
            # no `id` -> backend creates a new paytable
        }
    )
    logger.info("Creating paytable %r on game %s.", code, game_id)
    update_game(client, payload)

    # re-fetch to read back the server-assigned id
    game = get_game_detail(client, game_id, sas_entity_id)
    created = find_game_paytable(game, code)
    if not created or created.get("id") is None:
        raise RuntimeError(
            f"Created paytable {code!r} but could not read its id back from game/view."
        )
    logger.info("Created paytable %r (id=%s).", code, created.get("id"))
    return created.get("id")


# ---- High-level one-shot ----
def update_game_paytable(
    client,
    game_id,
    sas_entity_id,
    paytable_code,
    new_paytable,
):
    """
    Get game -> transform to update shape -> change one paytable -> submit.
    Token refresh is handled transparently by the client.
    Returns the game/update/v2 response.
    """
    game_data = get_game_detail(client, game_id, sas_entity_id)
    payload = build_update_payload(game_data)
    set_paytable(payload, paytable_code, new_paytable)
    return update_game(client, payload)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # --- Fill these in (grab tokens from browser DevTools) ---
    client = BoApiClient(
        token="<paste access token (ctoken)>",
        refresh_token="<paste refresh token (rtoken)>",
    )

    result = update_game_paytable(
        client,
        game_id=12345,
        sas_entity_id=1,
        paytable_code="DEFAULT",  # the paytable's `code`
        new_paytable={"symbols": []},  # dict or JSON string
    )
    logging.getLogger(__name__).info("%s", result.get("message"))
