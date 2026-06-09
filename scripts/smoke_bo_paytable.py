"""
Smoke test: exercise the BO paytable helpers (BoHelper + agent_game_settings +
update_game_paytable) against a real back-office environment.

Stage 1 (always) — read-only discovery: validates BO_TOKEN/BO_REFRESH_TOKEN and
the agent/game/currency config by listing currencies, the agent's game-settings
row (+ its selectable paytables) and the master game's paytable list.

Stage 2 (only with --mutate) — the full deterministic-result flow:
    ensure_paytable -> select_agent_paytable -> update_paytable -> reset_agent_paytable

Usage:
    python scripts/smoke_bo_paytable.py --env dev
    python scripts/smoke_bo_paytable.py --env dev --mutate
    python scripts/smoke_bo_paytable.py --env dev --mutate --paytable-ref base

Config is resolved exactly like the runner: game-scoped ids (game_id, sas_entity_id,
agent_id, currency_id, game_code) from games/{game}/env.config.yml (bo: block);
endpoint + tokens (BO_API_BASE, BO_TOKEN, BO_REFRESH_TOKEN) from .env + .env.{suffix}.
Nothing here is hardcoded.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.bo.agent_game_settings import (
    find_default_paytable,
    find_game_row,
    get_agent_currencies,
    get_agent_game_settings,
)
from framework.bo.update_game_paytable import get_game_detail
from framework.helpers.bo import BoConfigError, BoHelper

logger = logging.getLogger("smoke_bo")

PAYTABLE_CODE = "AutomationTest"


def _hr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _discover(bo: BoHelper) -> None:
    """Read-only: prove tokens + config are valid before any mutation."""
    client = bo._client()  # builds the BoApiClient; raises BoConfigError if creds missing

    _hr("Config (resolved from env)")
    print(f"  api_base       : {bo._api_base or '(default staging)'}")
    print(f"  game_id        : {bo.game_id}")
    print(f"  sas_entity_id  : {bo.sas_entity_id}")
    print(f"  agent_id       : {bo.agent_id}")
    print(f"  currency_id    : {bo.currency_id}")
    print(f"  game_code      : {bo.game_code}")

    _hr("1) currency/list_by_agent  (validates token + agent_id)")
    currencies = get_agent_currencies(client, bo.agent_id)
    print(f"  {len(currencies)} currencies for agent {bo.agent_id}:")
    for c in currencies:
        marker = "  <-- BO_CURRENCY_ID" if c.get("id") == bo.currency_id else ""
        print(f"    id={c.get('id')!s:>5}  code={c.get('code')}{marker}")

    _hr("2) agentgamecurrency/view  (the game row + selectable paytables)")
    data = get_agent_game_settings(client, bo.agent_id, bo.currency_id, game_code=bo.game_code)
    row = find_game_row(data, game_code=bo.game_code)
    print(f"  row id={row.get('id')}  gameCode={row.get('gameCode')}  rtpLevel={row.get('rtpLevel')}")
    print(f"  payTableList ({len(row.get('payTableList') or [])}):")
    for p in row.get("payTableList") or []:
        is_auto = "  <-- AutomationTest" if p.get("payTableCode") == PAYTABLE_CODE else ""
        is_def = " [default]" if p.get("isDefault") == 1 else ""
        print(
            f"    payTableId={p.get('payTableId')!s:>5}  "
            f"code={p.get('payTableCode')}  rtp={p.get('rtpLevel')}{is_def}{is_auto}"
        )
    default_pt = find_default_paytable(row, prefer_rtp_level=row.get("rtpLevel"))
    print(f"  game-default paytable -> {default_pt.get('payTableCode')} (id={default_pt.get('payTableId')})")

    _hr("3) game/view  (master game paytable list)")
    game = get_game_detail(client, bo.game_id, bo.sas_entity_id)
    print(f"  game id={game.get('id')}  name={game.get('name')}")
    print(f"  payTableInfoList ({len(game.get('payTableInfoList') or [])}):")
    has_auto = False
    for p in game.get("payTableInfoList") or []:
        is_auto = ""
        if p.get("code") == PAYTABLE_CODE:
            is_auto = "  <-- AutomationTest exists"
            has_auto = True
        is_def = " [default]" if p.get("isDefault") == 1 else ""
        print(f"    id={p.get('id')!s:>5}  code={p.get('code')}  rtp={p.get('rtp')}{is_def}{is_auto}")
    if not has_auto:
        print(f"  (no '{PAYTABLE_CODE}' paytable yet — bo_paytable_ensure will create it)")

    print("\nDISCOVERY OK — tokens valid, config resolves, agent/game/currency found.")


async def _mutate(bo: BoHelper, paytable_ref: str | None) -> None:
    """The full ensure -> select -> update -> reset flow (real BO writes)."""
    _hr("4) ensure_paytable  (create AutomationTest on the master game if missing)")
    pid = await bo.ensure_paytable(PAYTABLE_CODE)
    print(f"  AutomationTest paytable id = {pid}")

    _hr("5) select_agent_paytable  (point the agent's game at AutomationTest)")
    resp = await bo.select_agent_paytable(PAYTABLE_CODE)
    print(f"  message: {resp.get('message')}")

    _hr("6) update_paytable  (set AutomationTest content for a group)")
    if paytable_ref:
        content = bo.resolve_paytable(ref=paytable_ref)
        print(f"  content from fixture games/{bo.game}/paytables/{paytable_ref}.json")
    else:
        # No fixture given: re-use the game's current AutomationTest content by
        # cloning the default — keeps the smoke test self-contained. To exercise a
        # real forced result, pass --paytable-ref <name>.
        client = bo._client()
        game = get_game_detail(client, bo.game_id, bo.sas_entity_id)
        src = next(
            (p for p in game.get("payTableInfoList") or [] if p.get("code") == PAYTABLE_CODE),
            None,
        )
        content = (src or {}).get("payTable")
        print("  no --paytable-ref given; re-submitting current AutomationTest content (no-op edit)")
    resp = await bo.update_paytable(PAYTABLE_CODE, content)
    print(f"  message: {resp.get('message')}")

    _hr("7) reset_agent_paytable  (teardown — back to the game default)")
    resp = await bo.reset_agent_paytable()
    print(f"  message: {resp.get('message')}")

    print("\nMUTATION FLOW OK — ensure / select / update / reset all succeeded.")


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    bo = BoHelper(game=args.game, env=args.env)

    try:
        _discover(bo)
        if args.mutate:
            await _mutate(bo, args.paytable_ref)
        else:
            print("\n(read-only run — pass --mutate to exercise ensure/select/update/reset)")
    except BoConfigError as exc:
        print(f"\nCONFIG ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("BO smoke test failed")
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the BO paytable helpers.")
    parser.add_argument("--game", default="zeus", help="Game identifier (default: zeus)")
    parser.add_argument("--env", default="dev", help="Environment (dev|staging|prod), default dev")
    parser.add_argument("--mutate", action="store_true", help="Run the ensure/select/update/reset writes")
    parser.add_argument("--paytable-ref", default=None, help="Fixture stem for the update step")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
