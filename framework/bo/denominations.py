"""
Denominations in the Royaledge back-office: the bet ladder a game offers in one currency.
Read them, create one, edit one, and assign one — to the MASTER game, or to ONE agent's game row.

Three levels, and they are not interchangeable:

| Level | Endpoints | Field that decides |
|---|---|---|
| the denomination itself | `denomination/view/list` -> `denomination/update` | `betLevels`, `defaultBetLevel`, `name` — shared by every game of that SUB-CATEGORY + currency |
| master game | `game/view` -> `game/update/v2` | `currencyInfoList[].denominationId` — the default every agent inherits for that currency |
| agent game | `agentgamecurrency/view` -> `agentgamecurrency/update` | the row's `denominationId` **plus** `isDenomOverride` — one agent only |

**A denomination belongs to a (game sub-category, currency) pair, not to a game.** `game/view` gives
the sub-category (`gameSubCategory.id`: 1 = 3x3, 2 = 3x5); the currency id comes from the env config.
Every 3x3 game in TKN therefore shares the same list, which is why a new one must be named uniquely
per pair — `automation_denomination_name()` builds "automation test TKN 3x3".

⚠ **`denomination/update` is a FULL REPLACE, not a patch.** The body carries the whole list for that
pair, so every existing denomination must be sent back unchanged alongside the new or edited one.
Sending only the new entry would drop the rest — including the game defaults every other agent is
using. `update_denominations()` is the only function that posts it, and every caller here builds its
list from a fresh `denomination/view/list` read.

⚠ **The two endpoints spell the sub-category differently**: the list takes `gameSubCategoryId`, the
update takes `subCategoryId`, and both take the ids as STRINGS.

**`isDenomOverride` is what makes an agent row win.** `1` = this agent overrides the master default;
`0` = it follows the master game. Note it reads the OPPOSITE way round to the paytable flag beside it
(`isDefaultPaytable`: 1 = following the default) — see `agent_game_settings`.

Auth and token refresh are handled by `BoApiClient` (bo_api_client.py).
"""

import logging

from framework.bo.agent_game_settings import (
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    _auto_finalization_from,
    build_submit_row,
    find_current_paytable,
    find_game_row,
    get_agent_game_settings,
    update_agent_game_settings,
)
from framework.bo.update_game_paytable import (
    build_update_payload,
    get_game_detail,
    update_game,
)

logger = logging.getLogger(__name__)

# The agent row's denomination flag, reversed against `isDefaultPaytable` beside it.
DENOM_OVERRIDDEN = 1
DENOM_FOLLOWING_DEFAULT = 0

# The automation denomination this repo creates when a pair has none.
AUTOMATION_PREFIX = "automation test"
AUTOMATION_BET_LEVELS = (11, 22, 33)
AUTOMATION_DEFAULT_LEVEL = 1          # 1-BASED index into betLevels -> the 11 level

# The fields of a `currencyInfoList` entry worth reporting; the rest is not about denominations.
_CURRENCY_FIELDS = (
    "id", "code", "denominationId", "denomName",
    "minBet", "maxBet", "isDenomOverride", "isDefault",
)
# What `denomination/update` accepts per entry. `id` is added for an existing one and OMITTED for a
# new one — that omission is what makes the back office create it.
_SUBMIT_FIELDS = ("name", "isDefault", "defaultBetLevel", "remarks", "betLevels")


# ---------------------------------------------------------------------------
# THE DENOMINATION ITSELF — read
# ---------------------------------------------------------------------------
def get_denomination_list(client, sub_category_id, currency_id,
                          sort_by="name", sort_direction="desc"):
    """POST `denomination/view/list` -> the `data` object for one (sub-category, currency) pair:
    `{currency, gameSubCategory, denomType, denominations: [...]}`.

    Each denomination carries `{id, isDefault, name, defaultBetLevel, betLevels, totalLevel, minBet,
    maxBet, remarks, lastUpdated}`. Ids are sent as strings, as the back office expects.
    """
    body = client.post(
        "denomination/view/list",
        {
            "gameSubCategoryId": str(sub_category_id),
            "currencyId": str(currency_id),
            "sortBy": sort_by,
            "sortDirection": sort_direction,
        },
    )
    return body["data"]


def _norm_name(value):
    """A denomination name reduced to what a comparison should actually care about.

    Case and runs of whitespace are NOT meaningful here: the back office happily stores
    "Automation Test TRY 3x5" for a ladder a person created by hand, and it collides with
    "automation test TRY 3x5" on the server's own uniqueness check while comparing unequal in
    Python. Matching exactly is what made `ensure_automation_denomination` miss an existing ladder
    and then fail its create with DENOMINATION_NAME_ALREADY_EXISTS.
    """
    return " ".join(str(value or "").split()).casefold()


def find_denomination(list_data, *, name=None, denomination_id=None):
    """One denomination from a `get_denomination_list()` result, by name or by id.

    The name match is EXACT first and then case/whitespace-insensitive (`_norm_name`), so a ladder
    someone created by hand is found rather than duplicated. Returns `None` when there is no match —
    callers decide whether that means "create it" (the automation flow) or "fail" (an explicit
    edit), so this does not raise.
    """
    entries = list(list_data.get("denominations") or [])
    for d in entries:
        if name is not None and d.get("name") == name:
            return d
        if denomination_id is not None and d.get("id") == denomination_id:
            return d
    if name is None:
        return None
    want = _norm_name(name)
    for d in entries:
        if _norm_name(d.get("name")) == want:
            return d
    return None


def denomination_names(list_data):
    """Every `(id, name)` in a listing — for an error message that shows what IS there."""
    return [(d.get("id"), d.get("name")) for d in (list_data.get("denominations") or [])]


def automation_denomination_name(list_data, prefix=AUTOMATION_PREFIX):
    """The name this repo's automation denomination must carry for this pair.

    A denomination name has to be unique across the game category, so the plain prefix cannot be
    reused for two currencies or two sub-categories. The list response names both halves, so the
    name is built from what the server itself reports: "automation test TKN 3x3".
    """
    return f"{prefix} {list_data.get('currency')} {list_data.get('gameSubCategory')}".strip()


# ---------------------------------------------------------------------------
# THE DENOMINATION ITSELF — create and edit
# ---------------------------------------------------------------------------
def to_submit_denomination(entry):
    """Reduce a listed denomination to what `denomination/update` accepts, keeping its id.

    `totalLevel`, `minBet`, `maxBet` and `lastUpdated` are derived by the back office from
    `betLevels`; sending them back is at best noise.
    """
    out = {k: entry.get(k) for k in _SUBMIT_FIELDS}
    if entry.get("id") is not None:
        out["id"] = entry["id"]
    return out


def _validated_levels(bet_levels, default_bet_level):
    """Bet levels as a sorted list of numbers, with `default_bet_level` checked against them.

    `defaultBetLevel` is a **1-based index** into `betLevels` (the BO payload for [11, 22, 33] with a
    default of 11 sends `1`). An index outside the ladder would leave the game opening at a level
    that does not exist, so it is refused here rather than written.
    """
    levels = sorted(bet_levels or [])
    if not levels:
        raise ValueError("bet_levels must not be empty")
    if not 1 <= int(default_bet_level) <= len(levels):
        raise ValueError(
            f"default_bet_level {default_bet_level} is outside betLevels {levels} "
            f"(it is a 1-based index: 1 selects {levels[0]})"
        )
    return levels


def update_denominations(client, sub_category_id, currency_id, denominations):
    """POST `denomination/update` with the COMPLETE list for this pair.

    ⚠ Full replace. `denominations` must contain every existing entry (via `to_submit_denomination`)
    plus whatever is being added or changed. Note the field is `subCategoryId` here, not the
    `gameSubCategoryId` the list endpoint takes.
    """
    return client.post(
        "denomination/update",
        {
            "subCategoryId": str(sub_category_id),
            "currencyId": str(currency_id),
            "denominations": denominations,
        },
    )


def create_denomination(client, sub_category_id, currency_id, name, bet_levels,
                        default_bet_level=1, remarks="", verify=True, listing=None):
    """Create one denomination for a (sub-category, currency) pair and return its new id.

    Sends every existing denomination back unchanged plus the new one, which carries NO `id` — that
    is what makes the back office create it. The id is then read back from a fresh list, because the
    update response does not carry it.

    `listing` reuses a `get_denomination_list()` result the caller already has, instead of fetching
    the same list again. It must be the list for THIS pair — it is the one resubmitted, so a stale
    or foreign one would write the wrong ladder back.
    """
    levels = _validated_levels(bet_levels, default_bet_level)
    listing = listing or get_denomination_list(client, sub_category_id, currency_id)
    if find_denomination(listing, name=name):
        raise ValueError(
            f"A denomination named {name!r} already exists for "
            f"{listing.get('currency')} {listing.get('gameSubCategory')} — names must be unique."
        )
    payload = [to_submit_denomination(d) for d in (listing.get("denominations") or [])]
    payload.append({
        "name": name,
        "defaultBetLevel": int(default_bet_level),
        "remarks": remarks or "",
        "betLevels": levels,
    })
    logger.info(
        "Creating denomination %r for %s %s (levels %s, default level %s); resubmitting %d existing.",
        name, listing.get("currency"), listing.get("gameSubCategory"), levels, default_bet_level,
        len(payload) - 1,
    )
    update_denominations(client, sub_category_id, currency_id, payload)
    if not verify:
        return None
    created = find_denomination(get_denomination_list(client, sub_category_id, currency_id), name=name)
    if not created or created.get("id") is None:
        raise RuntimeError(
            f"Created denomination {name!r} but could not read its id back from denomination/view/list."
        )
    logger.info("Created denomination %r (id=%s).", name, created["id"])
    return created["id"]


def update_denomination(client, sub_category_id, currency_id, denomination_id, *,
                        name=None, bet_levels=None, default_bet_level=None, remarks=None,
                        is_default=None, verify=True):
    """Edit one existing denomination, leaving every other one in the pair untouched.

    Only the arguments you pass are changed; the rest of the entry is resubmitted as the server
    reports it. Returns the entry as it reads after the write (`verify=False` skips the read-back).
    """
    listing = get_denomination_list(client, sub_category_id, currency_id)
    target = find_denomination(listing, denomination_id=denomination_id)
    if not target:
        available = [(d.get("id"), d.get("name")) for d in (listing.get("denominations") or [])]
        raise ValueError(
            f"No denomination id {denomination_id} for {listing.get('currency')} "
            f"{listing.get('gameSubCategory')}. Available (id, name): {available}"
        )
    levels = target.get("betLevels") if bet_levels is None else list(bet_levels)
    level = target.get("defaultBetLevel") if default_bet_level is None else default_bet_level
    levels = _validated_levels(levels, level)

    payload = []
    for d in (listing.get("denominations") or []):
        entry = to_submit_denomination(d)
        if d.get("id") == denomination_id:
            entry.update({
                "name": target.get("name") if name is None else name,
                "betLevels": levels,
                "defaultBetLevel": int(level),
                "remarks": target.get("remarks") if remarks is None else remarks,
                "isDefault": target.get("isDefault") if is_default is None else int(bool(is_default)),
            })
        payload.append(entry)
    logger.info("Updating denomination %s (%r) for %s %s.", denomination_id, target.get("name"),
                listing.get("currency"), listing.get("gameSubCategory"))
    update_denominations(client, sub_category_id, currency_id, payload)
    if not verify:
        return None
    return find_denomination(
        get_denomination_list(client, sub_category_id, currency_id), denomination_id=denomination_id
    )


def ensure_automation_denomination(client, sub_category_id, currency_id, *,
                                   bet_levels=AUTOMATION_BET_LEVELS,
                                   default_bet_level=AUTOMATION_DEFAULT_LEVEL,
                                   prefix=AUTOMATION_PREFIX,
                                   listing=None):
    """Make sure this pair has the automation denomination, and return `(id, created)`.

    Idempotent, and deliberately so: an existing one is LEFT ALONE rather than rewritten, so a run
    never disturbs a ladder another run (or a person) is already playing on. `created` says which
    path was taken.
    """
    listing = listing or get_denomination_list(client, sub_category_id, currency_id)
    name = automation_denomination_name(listing, prefix)
    existing = find_denomination(listing, name=name)
    if existing:
        logger.info("Denomination %r already exists (id=%s, levels %s).",
                    name, existing.get("id"), existing.get("betLevels"))
        return existing["id"], False
    try:
        return create_denomination(client, sub_category_id, currency_id, name, bet_levels,
                                   default_bet_level=default_bet_level, listing=listing), True
    except Exception as exc:                                   # noqa: BLE001 - re-raised below
        # ⚠ THE BACK OFFICE ENFORCES NAME UNIQUENESS MORE WIDELY THAN THIS LISTING SHOWS.
        # `denomination/view/list` is scoped to ONE (sub-category, currency) pair, so a name held
        # by a ladder outside that pair is invisible to the lookup above — and the create then
        # comes back DENOMINATION_NAME_ALREADY_EXISTS. A stale `listing` passed in by a caller
        # does the same thing. Either way the run used to die on an error that named no way out.
        #
        # So: re-read THIS pair fresh and look again. If the ladder is ours after all, use it —
        # that is the whole point of an idempotent ensure, and it is the common case (a ladder
        # created by an earlier run, or by hand under a different capitalisation).
        if "ALREADY_EXISTS" not in str(exc).upper():
            raise
        fresh = get_denomination_list(client, sub_category_id, currency_id)
        found = find_denomination(fresh, name=name)
        if found:
            logger.info("Denomination %r existed after all (id=%s, levels %s) -- the first listing "
                        "did not show it. Using it.", name, found.get("id"), found.get("betLevels"))
            return found["id"], False
        # Not in this pair, yet the name is taken. That is a name held elsewhere in the game
        # category, and no amount of retrying fixes it -- so say exactly what is here, which is
        # what a person needs to pick a free name or reuse the right ladder.
        raise RuntimeError(
            f"Cannot create the automation denomination {name!r} for "
            f"{listing.get('currency')} {listing.get('gameSubCategory')}: the back office says the "
            f"NAME already exists, but no denomination with it is in this pair's list. Names are "
            f"unique across the game category, so it is held by another (sub-category, currency) "
            f"pair. This pair currently has: {denomination_names(fresh)}. "
            f"Either point the run at one of those, or pass a different `prefix`."
        ) from exc


# ---------------------------------------------------------------------------
# MASTER GAME — read and assign
# ---------------------------------------------------------------------------
def list_game_denominations(game_data):
    """Every currency the master game offers, with the denomination assigned to it.

    `game_data` is the `data` object from `get_game_detail()`. Returns rows of
    `{id, code, denominationId, denomName, minBet, maxBet, isDenomOverride, isDefault}` sorted by
    currency code — `id` is the CURRENCY id (what `currencyInfoList` keys on), not the denomination id.
    """
    rows = [
        {k: c.get(k) for k in _CURRENCY_FIELDS}
        for c in (game_data.get("currencyInfoList") or [])
    ]
    return sorted(rows, key=lambda r: (r.get("code") or ""))


def game_sub_category_id(game_data):
    """`gameSubCategory.id` — the pair key `denomination/view/list` needs (1 = 3x3, 2 = 3x5)."""
    sub = game_data.get("gameSubCategory") or {}
    if sub.get("id") is None:
        raise ValueError(
            f"Game {game_data.get('code')!r} reports no gameSubCategory.id, so its denomination "
            f"list cannot be addressed."
        )
    return sub["id"]


def find_game_currency(game_data, *, currency_code=None, currency_id=None):
    """One `currencyInfoList` entry, by currency code (e.g. "TKN") or by currency id.

    Raises `ValueError` listing what the game does offer — a typo'd currency must fail loudly rather
    than silently assigning nothing.
    """
    if currency_code is None and currency_id is None:
        raise ValueError("pass currency_code or currency_id")
    for c in game_data.get("currencyInfoList") or []:
        if currency_code is not None and c.get("code") == currency_code:
            return c
        if currency_id is not None and c.get("id") == currency_id:
            return c
    available = sorted(
        (c.get("code"), c.get("id")) for c in (game_data.get("currencyInfoList") or [])
    )
    raise ValueError(
        f"Currency not found on game {game_data.get('code')!r} "
        f"(code={currency_code!r}, id={currency_id}). Available (code, id): {available}"
    )


def set_currency_denomination(payload, currency_id, denomination_id, is_denom_override=None):
    """Point one currency of a built `game/update/v2` payload at `denomination_id`.

    `payload` comes from `build_update_payload()`, whose `currencyInfoList` is already reduced to
    `{id, denominationId, isDenomOverride}` — every other currency is passed through untouched, so
    one assignment cannot disturb the other ~90 currencies on the game.
    """
    for entry in payload.get("currencyInfoList") or []:
        if entry.get("id") == currency_id:
            entry["denominationId"] = denomination_id
            if is_denom_override is not None:
                entry["isDenomOverride"] = STATUS_ACTIVE if is_denom_override else STATUS_INACTIVE
            return payload
    available = sorted(e.get("id") for e in (payload.get("currencyInfoList") or []))
    raise ValueError(f"Currency id {currency_id} not in this payload. Available ids: {available}")


def assign_game_denomination(
    client,
    game_id,
    sas_entity_id,
    denomination_id,
    *,
    currency_code=None,
    currency_id=None,
    is_denom_override=None,
    verify=True,
):
    """Assign `denomination_id` to one currency of the MASTER game. Every agent that does not
    override it inherits the change.

    Reads the game, transforms it into the update body the same way a paytable edit does, patches
    exactly one `currencyInfoList` entry, and submits. With `verify=True` (default) it re-reads
    `game/view` and returns the currency row as the server now reports it — so the caller sees the
    landed value, including `denomName`, rather than assuming the write took.
    """
    game = get_game_detail(client, game_id, sas_entity_id)
    currency = find_game_currency(game, currency_code=currency_code, currency_id=currency_id)
    payload = build_update_payload(game)
    set_currency_denomination(payload, currency["id"], denomination_id, is_denom_override)
    logger.info(
        "Master game %s (%s): currency %s (id=%s) denomination %s -> %s",
        game_id, game.get("code"), currency.get("code"), currency["id"],
        currency.get("denominationId"), denomination_id,
    )
    update_game(client, payload)
    if not verify:
        return None
    after = find_game_currency(
        get_game_detail(client, game_id, sas_entity_id), currency_id=currency["id"]
    )
    if after.get("denominationId") != denomination_id:
        raise RuntimeError(
            f"Assigned denomination {denomination_id} to {currency.get('code')} on game "
            f"{game_id}, but game/view still reports {after.get('denominationId')}."
        )
    return {k: after.get(k) for k in _CURRENCY_FIELDS}


# ---------------------------------------------------------------------------
# AGENT GAME — read and assign
# ---------------------------------------------------------------------------
def get_agent_denomination(client, agent_id, currency_id, game_code):
    """What this agent's game row currently uses: `{id, denominationId, isDenomOverride, ...}`.

    `id` here is the GAME SETTING row id — the one `agentgamecurrency/update` submits against.
    """
    data = get_agent_game_settings(client, agent_id, currency_id, game_code=game_code)
    row = find_game_row(data, game_code=game_code)
    return {
        "id": row.get("id"),
        "gameCode": row.get("gameCode"),
        "denominationId": row.get("denominationId"),
        "isDenomOverride": row.get("isDenomOverride"),
        "payTableId": row.get("payTableId"),
    }


def assign_agent_denomination(
    client,
    agent_id,
    currency_id,
    game_code,
    denomination_id,
    *,
    override=True,
    auto_finalization=None,
):
    """Assign `denomination_id` to ONE agent's game row, and mark it as an override.

    Everything else on the row — the paytable it is on, its max-payout override, rtp level, status —
    is preserved, exactly as `set_agent_paytable` preserves the denomination when it changes the
    paytable. `override=False` writes the id while leaving the row following the master default,
    which is rarely what you want.
    """
    data = get_agent_game_settings(client, agent_id, currency_id, game_code=game_code)
    row = find_game_row(data, game_code=game_code)
    submit_row = build_submit_row(
        row,
        find_current_paytable(row),          # keep the paytable the row is already on
        data.get("maxPayout"),
        denomination_id=denomination_id,
        is_denom_override=DENOM_OVERRIDDEN if override else DENOM_FOLLOWING_DEFAULT,
    )
    af = auto_finalization or _auto_finalization_from(data)
    logger.info(
        "Agent %s game %s (row %s): denomination %s -> %s (isDenomOverride=%s)",
        agent_id, game_code, submit_row["id"], row.get("denominationId"),
        denomination_id, submit_row["isDenomOverride"],
    )
    return update_agent_game_settings(client, agent_id, [submit_row], af)


def reset_agent_denomination_to_default(
    client,
    agent_id,
    currency_id,
    game_code,
    *,
    denomination_id=None,
    auto_finalization=None,
):
    """TEARDOWN: put the agent's game row back to following the master game's denomination.

    Clears `isDenomOverride`. `denomination_id` optionally also rewrites the id — pass the master
    game's value for this currency (`find_game_currency(...)["denominationId"]`) when you want the
    row to read exactly as it did before the test.

    ⚠ MEASURED on dev 2026-09-18: BO REQUIRES the id sent with `isDenomOverride: 0` to be the
    MASTER game's denomination for this currency. Clearing the override while the row still points at
    another ladder (say the automation one) is rejected with `INVALID_UPDATE_REQUEST`, and the row is
    left overridden. Pass `denomination_id=find_game_currency(...)["denominationId"]`; passing
    `None` keeps whatever the row has, which fails for the same reason whenever that is not the
    master's.
    """
    data = get_agent_game_settings(client, agent_id, currency_id, game_code=game_code)
    row = find_game_row(data, game_code=game_code)
    submit_row = build_submit_row(
        row,
        find_current_paytable(row),
        data.get("maxPayout"),
        denomination_id=denomination_id,
        is_denom_override=DENOM_FOLLOWING_DEFAULT,
    )
    af = auto_finalization or _auto_finalization_from(data)
    logger.info(
        "Agent %s game %s (row %s): denomination override cleared (id now %s)",
        agent_id, game_code, submit_row["id"], submit_row["denominationId"],
    )
    return update_agent_game_settings(client, agent_id, [submit_row], af)


# ---------------------------------------------------------------------------
# The whole flow, in one call
# ---------------------------------------------------------------------------
def ensure_agent_automation_denomination(
    client,
    game_id,
    sas_entity_id,
    agent_id,
    currency_id,
    game_code,
    *,
    bet_levels=AUTOMATION_BET_LEVELS,
    default_bet_level=AUTOMATION_DEFAULT_LEVEL,
    prefix=AUTOMATION_PREFIX,
    auto_finalization=None,
):
    """SETUP: put this agent's game on a known bet ladder, creating it if the pair has none.

        game/view              -> the game's sub-category (the denomination pair key)
        denomination/view/list -> does "automation test <CUR> <SUBCAT>" exist?
        denomination/update    -> ...if not, create it (resubmitting every existing entry)
        denomination/view/list -> read the new id back
        agentgamecurrency/*    -> assign it to the agent's row with isDenomOverride = 1

    Returns `{denomination_id, name, created, sub_category_id, previous_denomination_id,
    previous_is_denom_override, master_denomination_id, bet_levels, default_bet_level,
    default_bet}`.

    ⚠ A TEARDOWN NEEDS ALL THREE OF THOSE, not just `previous_denomination_id`. Measured on dev
    2026-09-18: **BO refuses to clear `isDenomOverride` unless the id sent with it is the MASTER
    game's denomination** — `denominationId: <automation> + isDenomOverride: 0` comes back
    `INVALID_UPDATE_REQUEST`. So the restore is:

      * previous override ON, and its id is not this automation ladder ->
        `assign_agent_denomination(..., previous_denomination_id)` (keeps the override);
      * otherwise -> `reset_agent_denomination_to_default(..., denomination_id=master_denomination_id)`.

    The second branch is also what rescues a row a FAILED teardown left on the automation ladder:
    `previous_denomination_id` then reads as the automation id itself, and restoring that would be
    both rejected and wrong.

    `bet_levels` / `default_bet_level` are read back from BO rather than assumed from the arguments:
    an EXISTING automation ladder is left alone (see `ensure_automation_denomination`), so it may not
    be the one this call would have created. `default_bet` resolves the 1-based `default_bet_level`
    to the bet value a game should open at on this ladder.
    """
    game = get_game_detail(client, game_id, sas_entity_id)
    sub_category_id = game_sub_category_id(game)
    listing = get_denomination_list(client, sub_category_id, currency_id)
    name = automation_denomination_name(listing, prefix)
    denomination_id, created = ensure_automation_denomination(
        client, sub_category_id, currency_id,
        bet_levels=bet_levels, default_bet_level=default_bet_level, prefix=prefix,
        listing=listing,                      # already fetched above; do not read it again
    )
    before = get_agent_denomination(client, agent_id, currency_id, game_code)
    assign_agent_denomination(client, agent_id, currency_id, game_code, denomination_id,
                              auto_finalization=auto_finalization)
    logger.info(
        "Agent %s game %s now on denomination %r (id=%s, %s); it was on %s.",
        agent_id, game_code, name, denomination_id, "created" if created else "existing",
        before.get("denominationId"),
    )
    # Read the ladder back: after a create the listing above is stale, and an existing ladder may
    # differ from the defaults this call was given.
    entry = find_denomination(get_denomination_list(client, sub_category_id, currency_id),
                              denomination_id=denomination_id) or {}
    levels = list(entry.get("betLevels") or [])
    level = entry.get("defaultBetLevel")
    default_bet = (levels[int(level) - 1]
                   if levels and level is not None and 1 <= int(level) <= len(levels) else None)
    return {
        "denomination_id": denomination_id,
        "name": name,
        "created": created,
        "sub_category_id": sub_category_id,
        "previous_denomination_id": before.get("denominationId"),
        "previous_is_denom_override": before.get("isDenomOverride"),
        # The only id BO accepts alongside `isDenomOverride: 0` (see the docstring).
        "master_denomination_id": (find_game_currency(game, currency_id=currency_id) or {}).get(
            "denominationId"),
        "bet_levels": levels,
        "default_bet_level": level,
        "default_bet": default_bet,
    }
