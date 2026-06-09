# Backoffice API — QA Integration Guide

Python helpers for driving the Royaledge backoffice API from a QA / test
automation platform. They reproduce, server-side, exactly what the backoffice
frontend does — so the payloads and the token-refresh behaviour match the real
app.

> **In this framework you normally don't call these modules directly.** The test
> runner integrates them via `framework.helpers.bo.BoHelper` (driven by
> `bo_paytable_*` suite steps). This guide is the lower-level reference / example
> code that shows how the underlying functions work. The modules live in the
> `framework/bo/` package; import via `from framework.bo import ...`.

## Modules

The package `framework/bo/` contains:

| Module | Purpose |
|------|---------|
| `framework/bo/bo_api_client.py` | Auth + transport. One `BoApiClient` that POSTs to the API and **auto-refreshes the access token** when it expires. Every other module talks through it. |
| `framework/bo/agent_game_settings.py` | Set / reset a **per-agent** game paytable (the `/agent/game/settings/update/:id` page). This is the one you use for "set a paytable, play, reset to default". |
| `framework/bo/update_game_paytable.py` | Edit the **master game** paytable list (the `/game` update flow): edit a paytable's content, or **ensure a named paytable (e.g. "AutomationTest") exists** so it becomes selectable per-agent. |

For QA setup/teardown around playing a game on an agent, you almost always want
`agent_game_settings`.

```
bo_api_client.BoApiClient
        ▲                ▲
        │                │
agent_game_settings   update_game_paytable
```

---

## 1. Install

```bash
pip install requests
```

The modules ship as the `framework/bo/` package in this repo. Run from the repo
root (which is on `sys.path`) and import them via the package:

```python
from framework.bo import BoApiClient, set_agent_paytable, ensure_game_paytable
```

The package `__init__` re-exports the public functions, so a single
`from framework.bo import …` covers everything the examples below use.

---

## 2. Configuration

| Value | What it is | Where to get it |
|-------|-----------|-----------------|
| `token` (ctoken) | Access token (`Authorization: Bearer …`) | DevTools → Network → any API request → Request Headers → value after `Bearer ` |
| `refresh_token` (rtoken) | Refresh token | Login response, or captured from the app's storage |
| `api_base` | API base URL | `https://staging-boapi.royaledge.io/sas-web/api/v1` (the default in the code) |
| `currency_id` | Currency the agent's game row belongs to | `get_agent_currencies(client, agent_id)` |
| `agent_id` | The agent you'll play on | The `:id` in `/agent/game/settings/update/:id` |
| `game_code` | Exact game code | e.g. `SL_33_ZAPPIN_ZEUS` |

Recommended: keep secrets in environment variables, not in code.

```python
import os
from framework.bo import BoApiClient

client = BoApiClient(
    token=os.environ["BO_TOKEN"],
    refresh_token=os.environ["BO_REFRESH_TOKEN"],
    # api_base defaults to staging; override for other environments:
    api_base=os.environ.get("BO_API_BASE", "https://staging-boapi.royaledge.io/sas-web/api/v1"),
)
```

### Persisting the rotated token (optional)

When the access token expires, the client refreshes it transparently. To keep
the new token across test runs, pass an `on_token_refresh` callback:

```python
def save_token(new_token):
    with open(".bo_token", "w") as f:
        f.write(new_token)

client = BoApiClient(token=..., refresh_token=..., on_token_refresh=save_token)
```

---

## 3. `BoApiClient` (transport + auth)

```python
client = BoApiClient(
    token,                 # access token (ctoken)
    refresh_token,         # refresh token (rtoken)
    api_base=API_BASE,     # default = staging
    on_token_refresh=None, # callback(new_token) to persist rotation
    lang="en",
    timeout=90,
)

result = client.post(endpoint, body)   # -> parsed JSON dict
```

Behaviour (mirrors `src/components/fe_foundation/lib/api.tsx`):

- Injects a unique `traceId` (uuid4) into **every** request body.
- Sends `Authorization: Bearer <token>` + JSON headers.
- **Token expiry is detected from the response body** (`messageCode == "TOKEN_EXPIRED"`),
  not the HTTP status. On expiry it calls `auth/refresh-token`, stores the new
  token, and retries the original request once.
- Success is `status in ("success", "SC_OK")`.

Exceptions:

| Exception | When | Suggested handling |
|-----------|------|--------------------|
| `AuthError` | Refresh failed / no refresh token / `UNAUTHORIZED_USER` | Re-login: fetch fresh tokens and rebuild the client. |
| `ApiError` | API returned a non-success `status` | Test/data error — inspect the message. |
| `requests.HTTPError` | Transport-level 4xx/5xx | Network/server problem. |

```python
from framework.bo import AuthError, ApiError

try:
    set_agent_paytable(client, "1", 18, "SL_33_ZAPPIN_ZEUS", "AutomationTest")
except AuthError:
    client = relogin()           # your function: get new tokens
except ApiError as e:
    raise AssertionError(f"Backoffice rejected the update: {e}")
```

---

## 4. Agent game-settings paytable (primary QA use case)

Flow: GET the agent's settings → exact-match the game by `gameCode` → pick a
paytable from its `payTableList` by **name** (`payTableCode`) → submit one row.
`isDefaultPaytable` is derived from the chosen paytable's `isDefault` flag
(named paytable → `0` overridden; the game default → `1`).

### Functions

```python
from framework.bo import (
    get_agent_currencies,
    set_agent_paytable,
    reset_agent_paytable_to_default,
    agent_paytable,            # context manager
)
```

| Function | Use |
|----------|-----|
| `get_agent_currencies(client, agent_id)` | List `{id, code, …}` to discover `currency_id`. |
| `set_agent_paytable(client, agent_id, currency_id, game_code, paytable_code)` | **Setup** — point the game at the named paytable. |
| `reset_agent_paytable_to_default(client, agent_id, currency_id, game_code)` | **Teardown** — reset to the game default (auto-discovered). |
| `agent_paytable(client, agent_id, currency_id, game_code, paytable_code)` | Context manager: set on enter, reset on exit. |

### Recommended pattern — context manager

```python
from framework.bo import BoApiClient
from framework.bo import agent_paytable

client = BoApiClient(token=..., refresh_token=...)

with agent_paytable(client, "1", 18, "SL_33_ZAPPIN_ZEUS", "AutomationTest"):
    play_game(...)          # paytable is "AutomationTest" while inside the block
# paytable is reset to the game default here — even if play_game() raised
```

### Explicit setup / teardown

```python
set_agent_paytable(client, "1", 18, "SL_33_ZAPPIN_ZEUS", "AutomationTest")
# ... run the test that plays the game ...
reset_agent_paytable_to_default(client, "1", 18, "SL_33_ZAPPIN_ZEUS")
```

### pytest fixture

```python
import os
import pytest
from framework.bo import BoApiClient
from framework.bo import set_agent_paytable, reset_agent_paytable_to_default

@pytest.fixture(scope="session")
def bo_client():
    return BoApiClient(
        token=os.environ["BO_TOKEN"],
        refresh_token=os.environ["BO_REFRESH_TOKEN"],
    )

@pytest.fixture
def zappin_zeus_paytable(bo_client):
    agent_id, currency_id, game_code = "1", 18, "SL_33_ZAPPIN_ZEUS"
    set_agent_paytable(bo_client, agent_id, currency_id, game_code, "AutomationTest")
    yield
    reset_agent_paytable_to_default(bo_client, agent_id, currency_id, game_code)

def test_play_with_automation_paytable(zappin_zeus_paytable):
    ...  # the agent's Zappin Zeus now uses the AutomationTest paytable
```

### What gets sent (verified against real payloads)

`set_agent_paytable(..., "SL_33_ZAPPIN_ZEUS", "AutomationTest")` →

```json
{
  "gameSettings": [{
    "id": 6677, "status": 1, "maxPayoutCustom": 300000, "override": 0,
    "payTableId": 362, "denominationId": 1160, "rtpLevel": "MEDIUM",
    "maxMultiplier": null, "isDenomOverride": 0, "isDefaultPaytable": 0
  }],
  "autoFinalization": { "autoFinalizationStatus": 1, "autoFinalizationDuration": "0.2" },
  "agentId": "1",
  "traceId": "<uuid auto-injected>"
}
```

`reset_agent_paytable_to_default(..., "SL_33_ZAPPIN_ZEUS")` → same row with
`payTableId: 237` and `isDefaultPaytable: 1`.

Every non-paytable field (`maxPayoutCustom`, `override`, `denominationId`,
`rtpLevel`, `maxMultiplier`, `isDenomOverride`, `status`) is preserved from the
current GET response. `autoFinalization` is preserved from the GET response too.

### Low-level escape hatch

If you need full control over the exact payload, build it yourself:

```python
from framework.bo import update_agent_game_settings

update_agent_game_settings(client, "1", [{
    "id": 6677, "status": 1, "maxPayoutCustom": 300000, "override": 0,
    "payTableId": 362, "denominationId": 1160, "rtpLevel": "MEDIUM",
    "maxMultiplier": None, "isDenomOverride": 0, "isDefaultPaytable": 0,
}], auto_finalization={"autoFinalizationStatus": 1, "autoFinalizationDuration": "0.2"})
```

---

## 5. Master game paytable (definition edit)

Use `update_game_paytable.py` only when you need to change the **content** of a
paytable on the master game record (not the per-agent selection).

```python
from framework.bo import update_game_paytable

update_game_paytable(
    client,
    game_id=12345,
    sas_entity_id=1,
    paytable_code="DEFAULT",          # the paytable's `code`
    new_paytable={"symbols": []},     # dict (JSON-encoded for you) or a JSON string
)
```

Flow: GET `game/view` → transform to the `game/update/v2` shape → replace the
`payTable` JSON for the matching `code` → POST `game/update/v2`. Other building
blocks if you need them: `get_game_detail`, `build_update_payload`,
`set_paytable`, `update_game`.

### 5.1 Setup flow — ensure an "AutomationTest" paytable exists

Before an agent can be pointed at the `AutomationTest` paytable (Section 4),
that paytable must exist in the **master game's** paytable list. Use
`ensure_game_paytable` for an idempotent setup:

```
get game by id (game/view)
        │
        ▼
payTableInfoList has a paytable named "AutomationTest"?
        │
   yes ─┴─ no
   │        │
   │        ▼
   │   create it: append a new payTableInfoList item (no id)
   │   → POST game/update/v2
   │        │
   │        ▼
   │   get game again (game/view) to read the server-assigned id
   │        │
   ▼        ▼
return the paytable id
```

```python
from framework.bo import ensure_game_paytable

paytable_id = ensure_game_paytable(
    client,
    game_id=63,                 # the master game id (gameId in the agent response)
    sas_entity_id=1,
    code="AutomationTest",      # paytable name
    paytable_content=None,      # optional; None -> clone the game's default paytable
    rtp="MEDIUM",               # HIGH | MEDIUM | LOW (only used when creating)
)
# paytable_id now exists on the game and is selectable per-agent by name.
```

Behaviour:

- **Already exists** → returns the existing paytable's `id`, no write.
- **Missing** → appends a new `payTableInfoList` item
  `{ code, payTable, rtp, isDefault: 0, status: 1 }` (no `id`, so the backend
  creates it), POSTs `game/update/v2`, then re-fetches `game/view` and returns
  the new server-assigned `id`.
- **`paytable_content`**: the `payTable` JSON for a *newly created* paytable
  (dict/list or JSON string). If omitted, it's cloned from the game's default
  paytable (or the first existing one) so the new paytable is valid. Pass an
  explicit value when the automation needs specific reel/symbol data.

> The `game_id` here is the **master game id** (the `gameId` field in the agent
> `agentgamecurrency/view` response — e.g. `63` for `SL_33_ZAPPIN_ZEUS`), not the
> `id` of the agent game-settings row (`6677`).

### 5.2 End-to-end QA setup (master + agent)

Combine both layers: ensure the paytable exists on the game, then select it on
the agent and reset on teardown.

```python
from framework.bo import BoApiClient
from framework.bo import ensure_game_paytable
from framework.bo import set_agent_paytable, reset_agent_paytable_to_default

client = BoApiClient(token=..., refresh_token=...)

GAME_ID = 63                    # master game id
AGENT_ID = "1"
CURRENCY_ID = 18
GAME_CODE = "SL_33_ZAPPIN_ZEUS"
PAYTABLE = "AutomationTest"

# --- setup ---
ensure_game_paytable(client, GAME_ID, sas_entity_id=1, code=PAYTABLE)   # 1) master
set_agent_paytable(client, AGENT_ID, CURRENCY_ID, GAME_CODE, PAYTABLE)  # 2) agent

# ... play the game ...

# --- teardown ---
reset_agent_paytable_to_default(client, AGENT_ID, CURRENCY_ID, GAME_CODE)
```

As a pytest fixture:

```python
@pytest.fixture
def automation_paytable(bo_client):
    ensure_game_paytable(bo_client, 63, sas_entity_id=1, code="AutomationTest")
    set_agent_paytable(bo_client, "1", 18, "SL_33_ZAPPIN_ZEUS", "AutomationTest")
    yield
    reset_agent_paytable_to_default(bo_client, "1", 18, "SL_33_ZAPPIN_ZEUS")
```

---

## 6. Endpoint reference

| Operation | Endpoint | Method | Module |
|-----------|----------|--------|--------|
| List agent currencies | `currency/list_by_agent` | POST | agent_game_settings |
| Get agent game settings | `agentgamecurrency/view` | POST | agent_game_settings |
| Update agent game settings | `agentgamecurrency/update` | POST | agent_game_settings |
| Get game detail | `game/view` | POST | update_game_paytable |
| Update master game / create paytable | `game/update/v2` | POST | update_game_paytable |
| Refresh token | `auth/refresh-token` | POST | bo_api_client (automatic) |

All are POST against `…/sas-web/api/v1/`. `traceId` is added automatically.

---

## 7. Notes / caveats

- **`gameCode` filter is partial.** `agentgamecurrency/view` with
  `filters.gameCode = "SL_33_ZAPPIN_ZEUS"` returns the whole family
  (`…_VIP`, `…_KR`, `…_MK`, …). The helpers exact-match `gameCode`, so you must
  pass the precise code.
- **Teardown resets to the *game* default**, i.e. the paytable with
  `isDefault == 1` (preferring the current `rtpLevel`), not the value that was
  set before your test. If you need to restore the exact prior selection,
  snapshot `payTableId` / `isDefaultPaytable` during setup and submit those via
  the low-level function.
- **`isDefaultPaytable` is reversed vs the denom flag**: `1` = following the
  game default, `0` = overridden.
- **Creating a paytable resubmits the whole game.** `ensure_game_paytable` adds
  the new paytable by POSTing the full `game/update/v2` payload (this is how the
  frontend does it). It's idempotent — if the paytable already exists it does no
  write — but the create path rewrites the game record. Run it as a one-time
  setup, not inside a tight per-test loop.
- **New paytable content is cloned** from the game's default paytable unless you
  pass `paytable_content`. If your automation needs specific reel/symbol data,
  pass it explicitly.
- **Token expiry is body-driven** (`messageCode == "TOKEN_EXPIRED"`), HTTP 200.
  The client handles it. If your environment instead returns a real HTTP 401,
  tell the maintainer to add a status-code branch.
- **`Browser-Fingerprint` header is not sent.** The frontend sends one; if the
  backend enforces it you'll get auth failures despite valid tokens — capture
  the header value from a real browser request and add it to `_headers()` in
  `bo_api_client.py`.
```
