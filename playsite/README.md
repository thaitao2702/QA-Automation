# Playsite automation client (Python)

A small Python client that exposes the Playsite flow your automation framework
needs:

```
login  ->  choose currency  ->  get game url
```

The Playsite web app is a thin front-end over the **GDP backend**. This client
talks to that same backend directly (no browser needed), so each step is one
HTTP call:

| Step              | Method on `PlaysiteClient` | Backend call                              |
| ----------------- | -------------------------- | ----------------------------------------- |
| login             | `login(user, password)`    | `POST /auth/login`                        |
| choose currency   | `get_currencies()`         | `GET  /user/getBalance` (player wallets)  |
| _(find game code)_| `get_game_list()`          | `GET  /game/list`                         |
| **get game url**  | `get_game_url(code, ccy)`  | `POST /game/openGame`                     |

## Install

```bash
pip install -r requirements.txt   # just `requests`
```

Drop `playsite_client.py` somewhere on your `PYTHONPATH` (or keep it next to
your tests and import it directly).

## Configure the backend URL

Resolution order: `base_url` argument → `PLAYSITE_API_URL` env var → built-in
default (`https://release.royaledge.io/api`).

```bash
export PLAYSITE_API_URL=https://dev.royaledge.io/api      # dev
export PLAYSITE_API_URL=https://release.royaledge.io/api  # release/QA
```

## Quick start

```python
from playsite_client import PlaysiteClient

client = PlaysiteClient()                       # or PlaysiteClient(base_url="https://dev.royaledge.io/api")

# 1. login
client.login("player1", "secret")               # token is stored on the client

# 2. choose currency  (currency is explicit — you pick the code)
codes = client.get_currency_codes()             # e.g. ["USD", "THB"]
currency = "USD"

# 3. get game url
url = client.get_game_url(game_code="MEGA888", currency=currency)
print(url)                                       # -> launchable vendor URL
```

One-liner for the whole flow:

```python
from playsite_client import login_and_get_game_url

url = login_and_get_game_url(
    username="player1", password="secret",
    game_code="MEGA888", currency="USD",
)
```

## API reference

### `PlaysiteClient(base_url=None, timeout=30, session=None)`
Create a client. Pass a `requests.Session` if you want shared connection
pooling / proxies in your framework.

### `login(username, password) -> dict`
Authenticates and stores `client.token` / `client.refresh_token` /
`client.username`. Returns the backend `data` object. Raises `PlaysiteError`
on bad credentials.

### `get_currencies() -> list[dict]`
The player's wallets, e.g. `[{"currency": "USD", "balance": 1000, "isDefault": 1}]`.
Use this to discover/validate currency codes. Requires `login()` first.

### `get_currency_codes() -> list[str]`
Convenience — just the codes, e.g. `["USD", "THB"]`.

### `get_game_list() -> list[dict]`
Game categories, each with a `gameList` of `{code, name, ...}`. Requires login.

### `get_game_codes() -> list[str]`
Convenience — a flat list of every game code.

### `get_game_url(game_code, currency, language="en") -> str`
Launches the game and returns its vendor URL. `currency` is **required and
explicit** — pass a code from `get_currency_codes()`. Raises `PlaysiteError`
on failure (see error codes below).

### `login_and_get_game_url(username, password, game_code, currency, language="en") -> str`
Convenience that runs `login()` then `get_game_url()`.

## Error handling

Every failure raises `PlaysiteError` with structured fields:

```python
from playsite_client import PlaysiteClient, PlaysiteError

client = PlaysiteClient()
try:
    client.login("player1", "secret")
    url = client.get_game_url("MEGA888", "USD")
except PlaysiteError as e:
    print(e.message)        # human-readable
    print(e.message_code)   # e.g. "SC_GAME_NOT_SUPPORTED_CURRENCY"
    print(e.payload)        # full JSON body for debugging
```

Common `message_code` values from the backend:

| Code                                | Meaning                                  |
| ----------------------------------- | ---------------------------------------- |
| `SC_GAME_NOT_SUPPORTED_CURRENCY`    | Game doesn't support the chosen currency |
| `SC_GAME_UNDER_MAINTENANCE`         | Game is under maintenance                |
| `SC_GAME_DEACTIVATED`               | Game has been deactivated                |
| `OPEN_GAME_FAILED`                  | Generic open-game failure                |
| `EXPIRED_TOKEN`                     | Access token expired (re-`login()`)      |

## CLI example

`example_get_game_url.py` is a runnable wrapper:

```bash
# Print the game URL (stdout = just the URL)
python example_get_game_url.py --user player1 --password secret --currency USD --game MEGA888

# Discover currencies (omit --currency)
python example_get_game_url.py --user player1 --password secret

# Discover game codes (omit --game)
python example_get_game_url.py --user player1 --password secret --currency USD
```

## Using it from pytest

```python
import os
import pytest
from playsite_client import PlaysiteClient

@pytest.fixture
def client():
    c = PlaysiteClient()  # reads PLAYSITE_API_URL
    c.login(os.environ["PLAYER_USER"], os.environ["PLAYER_PASS"])
    return c

def test_get_game_url(client):
    url = client.get_game_url(game_code="MEGA888", currency="USD")
    assert url.startswith("http")
```
