# Back-office denominations — read, create, edit, assign

A **denomination** is the bet ladder a game offers in one currency: a list of bet levels, one of them
the level the game opens at. This page is the reference for `framework/bo/denominations.py`, which
drives the four back-office endpoints behind it.

`docs/bo-integration.md` is the parent doc (auth, the client, paytables); this one goes deep on
denominations only.

---

## 1. The model: three levels, three endpoints

The word "denomination" means three different things depending on which record you are changing, and
mixing them up is the commonest way to change nothing at all.

| Level | Endpoints | The field that decides | Who it affects |
|---|---|---|---|
| **the denomination itself** | `denomination/view/list` → `denomination/update` | `betLevels`, `defaultBetLevel`, `name` | every game of that sub-category + currency |
| **master game** | `game/view` → `game/update/v2` | `currencyInfoList[].denominationId` | every agent that does not override it |
| **agent game** | `agentgamecurrency/view` → `agentgamecurrency/update` | the row's `denominationId` **+** `isDenomOverride` | one agent |

**A denomination belongs to a (game sub-category, currency) pair, not to a game.** The pair key is
`gameSubCategory.id` from `game/view` (`1` = 3x3, `2` = 3x5) plus the currency id from the env
config. Every 3x3 game in TKN shares one list — which is why a new one needs a name unique to the
pair, not just to the game.

---

## 2. The endpoints, as captured

### `denomination/view/list` — what this pair offers

```jsonc
// request
{ "gameSubCategoryId": "2", "currencyId": "18",
  "sortBy": "name", "sortDirection": "desc" }

// response.data
{ "currency": "TRY", "gameSubCategory": "3x5", "denomType": "SLOTS",
  "denominations": [
    { "id": 1153, "isDefault": 1, "name": "Default TRY 3x5 1", "defaultBetLevel": 3,
      "betLevels": [1, 2, 4, 6, 8, 10, 20, 40, 60, 80, 100, 200, 400, 600, 800, 1000, 2000, 3000, 4000, 5000],
      "totalLevel": 20, "minBet": 1, "maxBet": 5000,
      "remarks": "Uploaded via CSV", "lastUpdated": "30-01-2026 15:30:21" },
    …
  ] }
```

### `denomination/update` — create or edit

```jsonc
{ "subCategoryId": "2", "currencyId": "18",
  "denominations": [
    { "id": 114,  "name": "denomTRY-2",        "isDefault": 0, "defaultBetLevel": 1, "remarks": "",  "betLevels": [6, 12, …] },
    { "id": 740,  "name": "denom TRY 3x5",     "isDefault": 0, "defaultBetLevel": 3, "remarks": null, "betLevels": [23, 33, 44, 66] },
    { "id": 1153, "name": "Default TRY 3x5 1", "isDefault": 1, "defaultBetLevel": 3, "remarks": "Uploaded via CSV", "betLevels": [1, 2, …] },
    {             "name": "automation test TRY 3x5", "defaultBetLevel": 1, "remarks": "", "betLevels": [11, 22, 33] }
  ] }
```

### `agentgamecurrency/update` — assign it to one agent's game

```jsonc
{ "gameSettings": [ { "id": 50362, "status": 0, "maxPayoutCustom": 300000, "override": 0,
                      "payTableId": 430, "denominationId": 1731, "rtpLevel": "MEDIUM",
                      "maxMultiplier": null, "isDenomOverride": 1, "isDefaultPaytable": 1 } ],
  "autoFinalization": { "autoFinalizationStatus": 1, "autoFinalizationDuration": "1" },
  "agentId": "1" }
```

`traceId` is added by `BoApiClient` on every call.

---

## 3. Four traps, each of which costs a bad write

**1. `denomination/update` is a FULL REPLACE, not a patch.** The body carries the whole list for the
pair, so every existing denomination must be resubmitted alongside the new or edited one. Send only
the new entry and the rest are dropped — including the defaults other agents are playing on. Every
function here rebuilds that list from a fresh `denomination/view/list`, and `update_denominations()`
is the only one that posts it.

**2. A NEW entry is the one with no `id`.** That omission is what makes the back office create it.
The update response does not return the new id, so it is read back from a second list call.

**3. The two endpoints spell the sub-category differently.** The list takes `gameSubCategoryId`; the
update takes `subCategoryId`. Both want ids as **strings**.

**4. `defaultBetLevel` is a 1-BASED INDEX into `betLevels`, not a bet value.** `[11, 22, 33]` with
`defaultBetLevel: 1` opens at **11**. An index outside the ladder would leave the game opening at a
level that does not exist, so `create_denomination` / `update_denomination` refuse it rather than
write it. (The same 1-based reading appears in the client: `settings.defaultBetLevel` — see
`docs/dragon-vip-coverage.md`.)

**5. Clearing `isDenomOverride` only works with the MASTER game's denomination id.** Measured on
dev 2026-09-18: `agentgamecurrency/update` with `isDenomOverride: 0` and any other `denominationId`
is rejected with `INVALID_UPDATE_REQUEST`, and the row is left overridden.

| sent | result |
|---|---|
| `denominationId: 1734` (the automation ladder) + `isDenomOverride: 0` | **rejected** |
| `denominationId: 213` (the master's, `game/view` → `currencyInfoList[].denominationId`) + `isDenomOverride: 0` | accepted |

So a teardown cannot simply put `previous_denomination_id` back. `ensure_agent_automation_denomination`
returns `previous_is_denom_override` and `master_denomination_id` alongside it, and the restore is:

* the row was overridden before the run → `assign_agent_denomination(..., previous_denomination_id)`,
  which keeps the override;
* otherwise → `reset_agent_denomination_to_default(..., denomination_id=master_denomination_id)`.

⚠ **This is also how a FAILED teardown compounds.** The rejected reset leaves the row on the
automation ladder, so the NEXT run reads `previous_denomination_id` as that ladder — its teardown then
sends the same invalid pair and fails too, and every run in between plays at 11/22/33 while believing
it is on the real ladder. `ensure_agent_automation_denomination` flags that case (`previous` equals the
automation id) so the teardown falls back to the master and the leak self-heals.

And one that is not a trap but reads like one: **`isDenomOverride` is reversed against the paytable
flag beside it.** `isDenomOverride: 1` = this agent overrides the master default; `isDefaultPaytable:
1` = this agent follows the default.

---

## 4. The functions

```python
from framework.bo import (
    BoApiClient, get_game_detail, game_sub_category_id,
    get_denomination_list, find_denomination, automation_denomination_name,
    create_denomination, update_denomination, ensure_automation_denomination,
    list_game_denominations, assign_game_denomination,
    get_agent_denomination, assign_agent_denomination, reset_agent_denomination_to_default,
    ensure_agent_automation_denomination,
)
```

| Function | Does |
|---|---|
| `get_denomination_list(client, sub_category_id, currency_id)` | the pair's list: `{currency, gameSubCategory, denomType, denominations[]}` |
| `find_denomination(listing, name=… \| denomination_id=…)` | one entry, or `None` — the caller decides what absence means |
| `automation_denomination_name(listing)` | `"automation test TKN 3x3"`, built from what the server reports |
| `create_denomination(client, sub, cur, name, bet_levels, default_bet_level=1)` | creates, returns the new id (read back). Refuses a duplicate name |
| `update_denomination(client, sub, cur, denom_id, bet_levels=…, name=…, …)` | edits ONE entry; the others are resubmitted untouched |
| `ensure_automation_denomination(client, sub, cur)` | `(id, created)` — idempotent; an existing one is **left alone** |
| `game_sub_category_id(game_data)` | the pair key from `game/view` |
| `list_game_denominations(game_data)` | every currency on the master game with its `denominationId` |
| `assign_game_denomination(client, game_id, sas_entity_id, denom_id, currency_code="TKN")` | master-game default; verifies by re-reading |
| `get_agent_denomination(client, agent_id, currency_id, game_code)` | what the agent's row uses now |
| `assign_agent_denomination(…, denom_id)` | one agent's row, `isDenomOverride: 1` |
| `reset_agent_denomination_to_default(…, denomination_id=None)` | clears the override (teardown) |
| `ensure_agent_automation_denomination(…)` | the whole setup in one call (below) |

Both assignments **preserve everything else on the record they touch**: the master write rebuilds the
same `game/update/v2` body a paytable edit uses and patches one `currencyInfoList` entry; the agent
write reuses `build_submit_row`, so the row keeps its paytable, max-payout override, rtp level and
status.

---

## 5. The one-shot automation flow

```python
info = ensure_agent_automation_denomination(
    client, game_id=283, sas_entity_id=1, agent_id="1", currency_id=31,
    game_code="SL_33_TREASURES_DRAGON_VIP",
)
# {'denomination_id': 1731, 'name': 'automation test TKN 3x3', 'created': True,
#  'sub_category_id': 1, 'previous_denomination_id': 213}

...                                        # play the game on 11 / 22 / 33

reset_agent_denomination_to_default(
    client, "1", 31, "SL_33_TREASURES_DRAGON_VIP",
    denomination_id=info["previous_denomination_id"],
)
```

```
game/view              -> the game's sub-category
denomination/view/list -> does "automation test <CUR> <SUBCAT>" exist?
denomination/update    -> ...if not, create it (resubmitting every existing entry)
denomination/view/list -> read the new id back
agentgamecurrency/*    -> assign it to the agent's row, isDenomOverride = 1
```

**Idempotent and non-destructive by design.** An existing automation denomination is returned as-is,
never rewritten, so one run cannot move the ladder another run is playing on. `created` says which
path it took; `previous_denomination_id` is what the teardown needs.

Defaults: `AUTOMATION_BET_LEVELS = (11, 22, 33)`, `AUTOMATION_DEFAULT_LEVEL = 1` (opens at 11),
`AUTOMATION_PREFIX = "automation test"` — all overridable per call.

---

## 6. Status and open questions

**Verified offline, not yet live.** Every function above was exercised against a stubbed client using
the captured payloads in §2: the create path resubmits ids 114/740/1153 untouched and appends an
id-less entry; the existing path posts no update at all; an edit leaves the other entries byte-equal;
the agent row keeps its paytable and cap; and the guards reject a bad default level, an empty ladder
and a duplicate name. **Nothing has run against the real back office** — `BO_TOKEN` in `.env.dev` was
expired for the whole session (§7 of the session summary).

| Open question | Why it matters |
|---|---|
| When an agent's `isDenomOverride` is cleared, does the BO also expect `denominationId` to be reset to the master value? | The teardown leaves the id in place (matching how the frontend treats untouched fields) and takes an explicit `denomination_id` when you want it rewritten. |
| Does `denomination/update` accept `isDefault` on a NEW entry? | The captured payload omits it, so `create_denomination` does too. Setting a new denomination as the pair's default is therefore not supported here yet. |
| Is `sortBy`/`sortDirection` ever significant? | The functions always read the full list, so ordering does not affect what is resubmitted. |

**First live check, when a token is available:**

```python
listing = get_denomination_list(client, 1, 31)        # Dragon VIP is 3x3, TKN
print(listing["currency"], listing["gameSubCategory"],
      [(d["id"], d["name"]) for d in listing["denominations"]])
```

Read first, and confirm the pair key and names look right before any write — the update endpoint
replaces the whole list.
