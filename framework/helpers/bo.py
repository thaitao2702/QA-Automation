"""
Back-office (BO) helper for the QA platform test runner.

Wraps the low-level BO package (``framework.bo``: ``BoApiClient``, the master-game
paytable flow ``update_game_paytable`` and the per-agent game-settings flow
``agent_game_settings``) so a test suite can drive deterministic results by
controlling which paytable the agent plays and what that paytable contains.

The deterministic-result workflow (see docs/bo-integration.md) has three layers:

  1. **Ensure** the named automation paytable (e.g. ``AutomationTest``) exists on
     the master game — ``ensure_paytable`` (suite setup, once).
  2. **Select** that paytable on the agent's game so spins use it —
     ``select_agent_paytable`` (suite setup). **Reset** to the game default in
     teardown — ``reset_agent_paytable``.
  3. **Update** the paytable's content per group of test cases (base game,
     free spin, …) so each group gets its forced/determined outcome —
     ``update_paytable``.

Configuration
-------------
BO config is split by scope:

* **Game-scoped** identifiers live in the per-environment game config
  ``games/{game}/env.config.{suffix}.yml`` (e.g. ``env.config.dev.yml``) under a
  top-level ``bo:`` block (committed with the game, not secret)::

      bo:
        game_id: 63           # master game (ensure/update paytable content)
        sas_entity_id: 1      # master game SAS entity
        agent_id: 1           # agent the suite plays on (select/reset)
        currency_id: 18       # currency id of the agent's game row (select/reset)
        game_code: SL_33_ZAPPIN_ZEUS   # exact game code (select/reset)

  Each environment has its own file (``--env staging`` -> ``env.config.stg.yml``).

* **Environment-scoped** values (endpoint + secrets) come from the per-environment
  .env files (base ``.env`` + ``.env.{suffix}`` overlay — see
  ``framework/env_files.py``)::

      BO_API_BASE        back-office API base URL for this environment
      BO_TOKEN           access token (ctoken)
      BO_REFRESH_TOKEN   refresh token (rtoken)

So ``--env dev`` reads ``.env.dev`` and ``--env staging`` reads ``.env.stg``.
A matching ``BO_GAME_ID`` / ``BO_SAS_ENTITY_ID`` / ``BO_AGENT_ID`` /
``BO_CURRENCY_ID`` / ``BO_GAME_CODE`` environment variable still overrides the
game config value, and a test step may override any of them via params.

When the access token rotates, the new token is written to a per-environment
``.bo_token.{suffix}.json`` cache at the repo root (gitignored) so it survives
across runs.

Public interface
----------------
BoHelper(game, env, base_dir=None)
    resolve_paytable(ref=None, inline=None) -> dict | list | str
    async ensure_paytable(paytable_code="AutomationTest", *, paytable_content=None,
                          rtp="MEDIUM", game_id=None, sas_entity_id=None) -> int
    async select_agent_paytable(paytable_code, *, agent_id=None, currency_id=None,
                                game_code=None) -> dict
    async reset_agent_paytable(*, agent_id=None, currency_id=None, game_code=None) -> dict
    async update_paytable(paytable_code, new_paytable, game_id=None, sas_entity_id=None) -> dict

Construction is cheap and never touches the network; the underlying client is built
lazily on the first BO call, and missing credentials raise only then (so a suite
without BO steps runs fine without BO config).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from functools import partial
from pathlib import Path
from typing import Any

from framework.bo.agent_game_settings import (
    reset_agent_paytable_to_default,
    set_agent_paytable,
)
from framework.bo.bo_api_client import ApiError, AuthError, BoApiClient
from framework.bo.denominations import (
    assign_agent_denomination,
    ensure_agent_automation_denomination,
    reset_agent_denomination_to_default,
)
from framework.bo.update_game_paytable import ensure_game_paytable, update_game_paytable
from framework.env_files import env_file_suffix, load_env_files, load_game_config

logger = logging.getLogger(__name__)

# Repo root: framework/helpers/bo.py -> framework -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class BoConfigError(RuntimeError):
    """Raised when BO is invoked but required config/credentials are missing."""


class BoHelper:
    """Expose BO paytable updates to the test runner."""

    def __init__(
        self,
        game: str,
        env: str,
        base_dir: Path | None = None,
    ) -> None:
        self.game = game
        self.env = env
        self.base_dir = Path(base_dir).resolve() if base_dir else _REPO_ROOT

        # Ensure the per-environment .env overlay is loaded (idempotent — the suite
        # runner also loads it; this makes standalone BoHelper usage self-sufficient).
        load_env_files(self.env, self.base_dir)

        # Game-scoped identifiers come from games/{game}/env.config.yml (bo: block);
        # a matching BO_* environment variable still overrides the file value.
        game_cfg = self._load_game_bo_config()
        # Master-game identifiers (ensure/update paytable content).
        self.game_id: Any = self._cfg("BO_GAME_ID", "game_id", game_cfg, int)
        self.sas_entity_id: Any = self._cfg("BO_SAS_ENTITY_ID", "sas_entity_id", game_cfg, int)
        # Per-agent identifiers (select/reset which paytable the agent plays).
        self.agent_id: str | None = self._cfg("BO_AGENT_ID", "agent_id", game_cfg, str)
        self.currency_id: Any = self._cfg("BO_CURRENCY_ID", "currency_id", game_cfg, int)
        self.game_code: str | None = self._cfg("BO_GAME_CODE", "game_code", game_cfg, str)
        # The bet a scenario is expected to play at, so a suite can express its max-win cap
        # as a MULTIPLE of the bet (`paytable: { max_win_x: 10 }`). The cap is applied before
        # the game loads, so the runner cannot read the real stake — this is the stand-in.
        self.reference_bet = self._cfg("BO_REFERENCE_BET", "reference_bet", game_cfg, float)
        # The automation paytable's `code` differs per game — Zeus's master game carries
        # "AutomationTest", Chaos's carries "Automation_test". Writing to a code the game
        # does not have does NOT fail loudly enough: the run reports a paytable was set while
        # the game keeps playing its real weights. Declare it per game; None keeps the
        # historical default so Zeus/Mystic are unaffected.
        self.paytable_code: str | None = self._cfg(
            "BO_PAYTABLE_CODE", "paytable_code", game_cfg, str)
        # Environment-scoped endpoint + secrets (from .env / .env.{suffix}).
        self._token: str = os.environ.get("BO_TOKEN", "").strip()
        self._refresh_token: str = os.environ.get("BO_REFRESH_TOKEN", "").strip()
        self._api_base: str | None = os.environ.get("BO_API_BASE", "").strip() or None

        # Per-environment token cache (rotated access token); newer than .env.
        self._token_cache_path = (
            self.base_dir / f".bo_token.{env_file_suffix(self.env)}.json"
        )
        self._load_token_cache()

        self._client_instance: BoApiClient | None = None

    # ------------------------------------------------------------------
    # Config / credential loading
    # ------------------------------------------------------------------

    @staticmethod
    def _int_env(name: str) -> int | None:
        """Read an integer environment variable; None if unset/invalid."""
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning("Ignoring non-integer %s=%r", name, raw)
            return None

    def _load_game_bo_config(self) -> dict[str, Any]:
        """
        Read the game-scoped ``bo:`` block from the per-environment game config
        (games/{game}/env.config.{suffix}.yml).

        Missing file/section -> empty dict (config may instead come from BO_*
        env vars).
        """
        try:
            config = load_game_config(self.game, self.env, self.base_dir)
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            logger.warning("Could not read BO config: %s", exc)
            return {}
        return dict(config.get("bo") or {})

    def _cfg(
        self,
        env_name: str,
        yaml_key: str,
        game_cfg: dict[str, Any],
        cast: type,
    ) -> Any:
        """
        Resolve one BO config value: BO_* env var (override) then env.config.yml
        ``bo:`` value. ``cast`` is ``int`` or ``str``. None when unset/invalid.
        """
        raw = os.environ.get(env_name, "").strip()
        value: Any = raw if raw else game_cfg.get(yaml_key)
        if value is None or value == "":
            return None
        if cast is int:
            try:
                return int(value)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-integer BO config %s=%r", yaml_key, value)
                return None
        return str(value)

    @staticmethod
    def _jwt_expiry(token: str) -> float | None:
        """`exp` (epoch seconds) out of a JWT, or None when it cannot be read."""
        parts = (token or "").split(".")
        if len(parts) != 3:
            return None
        try:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            return float(json.loads(base64.urlsafe_b64decode(pad))["exp"])
        except Exception:  # noqa: BLE001 -- an unreadable token simply has no known expiry
            return None

    def _load_token_cache(self) -> None:
        """Adopt the cached tokens, but NEVER a cached access token that is already dead.

        The cache exists so a token rotated mid-run survives into the next process. It used to
        override .env unconditionally, which quietly broke the opposite case: paste a fresh token
        into .env.{suffix} while a stale cache is on disk, and the stale one still wins. The run
        then authenticates with an expired token, tries to refresh it, and fails with
        UNAUTHORIZED_USER -- because `auth/refresh-token` requires a token that is still VALID --
        while the same fresh token works perfectly in Postman. Cost an hour on 2026-09-04.

        So the cache is only preferred while it is actually usable: a cached access token that has
        expired, or is older than the one in .env, is ignored and the .env value stands.
        """
        if not self._token_cache_path.exists():
            return
        try:
            cached = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Ignoring unreadable BO token cache %s: %s", self._token_cache_path, exc)
            return
        cached_token = str(cached.get("token") or "").strip()
        if cached_token:
            now = time.time()
            cached_exp = self._jwt_expiry(cached_token)
            env_exp = self._jwt_expiry(self._token)
            if cached_exp is not None and cached_exp <= now:
                logger.warning(
                    "Ignoring BO token cache %s: its access token expired %.0f min ago. "
                    "Using the token from the .env overlay instead.",
                    self._token_cache_path.name, (now - cached_exp) / 60)
            elif env_exp is not None and cached_exp is not None and env_exp > cached_exp:
                logger.info("Ignoring BO token cache %s: the .env token is newer.",
                            self._token_cache_path.name)
            else:
                self._token = cached_token
                # Only adopt the cached refresh token alongside the access token it was saved
                # with; pairing a cached refresh token with an .env access token mixes sessions.
                if cached.get("refresh_token"):
                    self._refresh_token = str(cached["refresh_token"]).strip()

    def _persist_token(self, new_token: str, new_refresh_token: str | None = None) -> None:
        """on_token_refresh callback — write the rotated tokens to the cache.

        The refresh token is written too. dev does not rotate it today, but if it ever
        starts to, caching the ACCESS token beside a stale REFRESH token would hand the
        next process a pair that cannot renew itself.
        """
        if new_refresh_token:
            self._refresh_token = new_refresh_token
        try:
            self._token_cache_path.write_text(
                json.dumps(
                    {"token": new_token, "refresh_token": self._refresh_token},
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Persisted rotated BO token to %s", self._token_cache_path.name)
        except OSError as exc:
            logger.warning("Could not persist rotated BO token: %s", exc)

    def clear_token_cache(self) -> None:
        """Delete the per-environment rotated-token cache file (if present)."""
        try:
            if self._token_cache_path.exists():
                self._token_cache_path.unlink()
                logger.info("Cleared BO token cache %s", self._token_cache_path.name)
        except OSError as exc:
            logger.warning("Could not clear BO token cache %s: %s", self._token_cache_path, exc)

    # ------------------------------------------------------------------
    # Client (lazy)
    # ------------------------------------------------------------------

    def _client(self) -> BoApiClient:
        """Build the BoApiClient on first use; raise a clear error if creds missing."""
        if self._client_instance is not None:
            return self._client_instance
        if not self._token or not self._refresh_token:
            raise BoConfigError(
                f"BO credentials missing — set BO_TOKEN and BO_REFRESH_TOKEN in "
                f".env.{env_file_suffix(self.env)} (grab ctoken/rtoken from browser DevTools)."
            )
        kwargs: dict[str, Any] = {
            "token": self._token,
            "refresh_token": self._refresh_token,
            "on_token_refresh": self._persist_token,
        }
        if self._api_base:
            kwargs["api_base"] = self._api_base
        self._client_instance = BoApiClient(**kwargs)
        return self._client_instance

    # ------------------------------------------------------------------
    # Paytable resolution
    # ------------------------------------------------------------------

    def _paytables_dir(self) -> Path:
        return self.base_dir / "games" / self.game / "paytables"

    def resolve_paytable(
        self,
        ref: str | None = None,
        inline: Any = None,
    ) -> Any:
        """
        Resolve the paytable content for a step.

        ``inline`` (dict/list/JSON string) takes precedence. Otherwise ``ref`` names
        a fixture file ``games/{game}/paytables/{ref}.json``. Raises ValueError if
        neither is usable.
        """
        if inline is not None:
            return inline
        if not ref:
            raise ValueError(
                "paytable update requires either 'paytable' (inline) or 'paytable_ref'"
            )
        # Allow ref with or without a .json extension.
        candidate = self._paytables_dir() / (ref if ref.endswith(".json") else f"{ref}.json")
        if not candidate.exists():
            available = sorted(p.stem for p in self._paytables_dir().glob("*.json"))
            raise ValueError(
                f"No paytable fixture {candidate.name!r} in {self._paytables_dir()}. "
                f"Available: {available}"
            )
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Paytable fixture {candidate.name!r} is not valid JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # Paytable update
    # ------------------------------------------------------------------

    async def update_paytable(
        self,
        paytable_code: str,
        new_paytable: Any,
        game_id: Any = None,
        sas_entity_id: Any = None,
    ) -> dict[str, Any]:
        """
        Update one paytable (matched by ``code``) on the configured game.

        ``game_id`` / ``sas_entity_id`` default to the env.config.yml bo: values.
        Runs the blocking BO HTTP flow in a worker thread. Propagates AuthError /
        ApiError / ValueError to the caller.
        """
        gid = game_id if game_id is not None else self.game_id
        seid = sas_entity_id if sas_entity_id is not None else self.sas_entity_id
        if gid is None or seid is None:
            raise BoConfigError(
                "BO game_id/sas_entity_id missing — set game_id/sas_entity_id under the "
                f"bo: block in games/{self.game}/env.config.{env_file_suffix(self.env)}.yml "
                "(or BO_GAME_ID/BO_SAS_ENTITY_ID env vars, or the step params)."
            )
        client = self._client()
        return await asyncio.to_thread(
            update_game_paytable,
            client,
            gid,
            seid,
            paytable_code,
            new_paytable,
        )

    # ------------------------------------------------------------------
    # Master-game paytable existence (ensure the automation paytable)
    # ------------------------------------------------------------------

    async def ensure_paytable(
        self,
        paytable_code: str = "AutomationTest",
        *,
        paytable_content: Any = None,
        rtp: str = "MEDIUM",
        game_id: Any = None,
        sas_entity_id: Any = None,
    ) -> int:
        """
        Ensure a paytable named ``paytable_code`` exists on the master game so it
        becomes selectable per-agent; return its server-side id.

        Idempotent: if the paytable already exists, no write happens. When the
        paytable is created and ``paytable_content`` is omitted, it is cloned from
        the game's default paytable. Run this once in suite setup.
        """
        gid = game_id if game_id is not None else self.game_id
        seid = sas_entity_id if sas_entity_id is not None else self.sas_entity_id
        if gid is None or seid is None:
            raise BoConfigError(
                "BO game_id/sas_entity_id missing — set game_id/sas_entity_id under the "
                f"bo: block in games/{self.game}/env.config.{env_file_suffix(self.env)}.yml "
                "(or BO_GAME_ID/BO_SAS_ENTITY_ID env vars, or the step params)."
            )
        client = self._client()
        return await asyncio.to_thread(
            ensure_game_paytable,
            client,
            gid,
            seid,
            paytable_code,
            paytable_content,
            rtp,
        )

    # ------------------------------------------------------------------
    # Per-agent paytable selection (which paytable the agent plays)
    # ------------------------------------------------------------------

    def _agent_target(
        self,
        agent_id: Any = None,
        currency_id: Any = None,
        game_code: str | None = None,
    ) -> tuple[str, Any, str]:
        """Resolve (agent_id, currency_id, game_code) from params then env."""
        aid = agent_id if agent_id is not None else self.agent_id
        cid = currency_id if currency_id is not None else self.currency_id
        code = game_code if game_code is not None else self.game_code
        missing = [
            name
            for name, val in (
                ("BO_AGENT_ID", aid),
                ("BO_CURRENCY_ID", cid),
                ("BO_GAME_CODE", code),
            )
            if val in (None, "")
        ]
        if missing:
            raise BoConfigError(
                f"BO agent config missing ({', '.join(missing)}) — set agent_id/currency_id/"
                f"game_code under the bo: block in "
                f"games/{self.game}/env.config.{env_file_suffix(self.env)}.yml (or the "
                f"matching BO_* env vars, or the step params)."
            )
        return str(aid), cid, str(code)

    async def select_agent_paytable(
        self,
        paytable_code: str,
        *,
        agent_id: Any = None,
        currency_id: Any = None,
        game_code: str | None = None,
        max_payout: Any = None,
    ) -> dict[str, Any]:
        """
        Point the agent's game at the named paytable (e.g. ``AutomationTest``) so
        spins use it. Setup step — preserves every other game-settings field.

        ``max_payout`` (optional) also caps the agent's payout for this game. A forced paytable
        can award a bonus that runs for hundreds of spins; the cap is what ends it — the server
        truncates at the cap and sets ``hasReachedMaxWin``, which the framework treats as terminal.
        The teardown clears the override, so a cap cannot leak into the next run.
        """
        aid, cid, code = self._agent_target(agent_id, currency_id, game_code)
        client = self._client()
        return await asyncio.to_thread(
            set_agent_paytable, client, aid, cid, code, paytable_code, None, max_payout
        )

    async def reset_agent_paytable(
        self,
        *,
        agent_id: Any = None,
        currency_id: Any = None,
        game_code: str | None = None,
    ) -> dict[str, Any]:
        """
        Reset the agent's game paytable back to the game default (auto-discovered).
        Teardown step — undoes ``select_agent_paytable``.
        """
        aid, cid, code = self._agent_target(agent_id, currency_id, game_code)
        client = self._client()
        return await asyncio.to_thread(
            reset_agent_paytable_to_default, client, aid, cid, code
        )

    # ------------------------------------------------------------------
    # Denominations — the BET LADDER the agent's game row offers.
    #
    # These are the paytable pair's twin: `ensure_automation_denomination` is SETUP and
    # `reset_agent_denomination` is its TEARDOWN, and a run that calls the first MUST declare the
    # second or the next run inherits a bet ladder of 11/22/33 and every bet assert in the suite
    # goes red for a reason nothing in that suite mentions.
    #
    # ⚠ The automation ladder is (11, 22, 33) ON PURPOSE (`AUTOMATION_BET_LEVELS`). Those values do
    # not occur in any real denomination, so a test can tell from ONE number whether a stake came
    # from the automation ladder or the game's own — which is the whole point of a
    # denomination-swap case. Do not "tidy" them into round numbers.
    #
    # ⚠ NEVER RUN LIVE as of 2026-09-16: `framework/bo/denominations.py` was written and tested
    # offline against captured payloads only. The first live run should read the list first.
    # ------------------------------------------------------------------
    async def ensure_automation_denomination(
        self,
        *,
        agent_id: Any = None,
        currency_id: Any = None,
        game_code: str | None = None,
        game_id: Any = None,
        sas_entity_id: Any = None,
        bet_levels: Any = None,
    ) -> dict[str, Any]:
        """SETUP: put this agent's game row on the automation bet ladder, creating it if needed.

        Returns ``{denomination_id, name, created, sub_category_id, previous_denomination_id}``.
        ``previous_denomination_id`` is what the row used BEFORE — hand it to
        ``reset_agent_denomination`` so the teardown restores exactly what was there.
        """
        aid, cid, code = self._agent_target(agent_id, currency_id, game_code)
        gid = game_id if game_id is not None else self.game_id
        seid = sas_entity_id if sas_entity_id is not None else self.sas_entity_id
        if gid is None or seid is None:
            raise BoConfigError(
                "BO game_id/sas_entity_id missing — set game_id/sas_entity_id under the "
                f"bo: block in games/{self.game}/env.config.{env_file_suffix(self.env)}.yml "
                "(or BO_GAME_ID/BO_SAS_ENTITY_ID env vars)."
            )
        client = self._client()
        kwargs: dict[str, Any] = {}
        if bet_levels is not None:
            kwargs["bet_levels"] = tuple(bet_levels)
        return await asyncio.to_thread(
            partial(ensure_agent_automation_denomination,
                    client, gid, seid, aid, cid, code, **kwargs)
        )

    async def assign_agent_denomination(
        self,
        denomination_id: Any,
        *,
        agent_id: Any = None,
        currency_id: Any = None,
        game_code: str | None = None,
    ) -> dict[str, Any]:
        """Point the agent's game row at ``denomination_id`` WITH the override on.

        The teardown half of a run whose row already overrode the denomination before the test:
        restoring that state means keeping ``isDenomOverride: 1``, because BO only accepts
        ``isDenomOverride: 0`` alongside the master game's id.
        """
        aid, cid, code = self._agent_target(agent_id, currency_id, game_code)
        client = self._client()
        return await asyncio.to_thread(
            partial(assign_agent_denomination, client, aid, cid, code, denomination_id)
        )

    async def reset_agent_denomination(
        self,
        *,
        denomination_id: Any = None,
        agent_id: Any = None,
        currency_id: Any = None,
        game_code: str | None = None,
    ) -> dict[str, Any]:
        """TEARDOWN: clear the row's denomination override so it follows the master game again.

        ⚠ ``denomination_id`` must be the MASTER game's denomination for this currency
        (``master_denomination_id`` from the setup). BO rejects `isDenomOverride: 0` sent with any
        other id — measured on dev 2026-09-18 — and leaves the row overridden.
        """
        aid, cid, code = self._agent_target(agent_id, currency_id, game_code)
        client = self._client()
        return await asyncio.to_thread(
            partial(reset_agent_denomination_to_default,
                    client, aid, cid, code, denomination_id=denomination_id)
        )


__all__ = ["BoHelper", "BoConfigError", "AuthError", "ApiError"]
