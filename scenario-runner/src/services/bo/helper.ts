/**
 * Back-office (BO) helper for the QA platform test runner (port of framework/helpers/bo.py).
 *
 * Wraps the low-level BO modules (`BoApiClient`, the master-game paytable flow
 * `updateGamePaytable` and the per-agent game-settings flow `agent-game-settings`) so a test suite
 * can drive deterministic results by controlling which paytable the agent plays and what that
 * paytable contains.
 *
 * The deterministic-result workflow (see docs/bo-integration.md) has three layers:
 *
 *   1. **Ensure** the named automation paytable (e.g. `AutomationTest`) exists on the master game —
 *      `ensurePaytable` (suite setup, once).
 *   2. **Select** that paytable on the agent's game so spins use it — `selectAgentPaytable` (suite
 *      setup). **Reset** to the game default in teardown — `resetAgentPaytable`.
 *   3. **Update** the paytable's content per group of test cases (base game, free spin, …) so each
 *      group gets its forced/determined outcome — `updatePaytable`.
 *
 * Configuration
 * -------------
 * * **Game-scoped** identifiers live in `games/{game}/env.config.{suffix}.yml` under a top-level
 *   `bo:` block (game_id, sas_entity_id, agent_id, currency_id, game_code, paytable_code,
 *   reference_bet). A matching `BO_GAME_ID` / `BO_SAS_ENTITY_ID` / `BO_AGENT_ID` /
 *   `BO_CURRENCY_ID` / `BO_GAME_CODE` / `BO_PAYTABLE_CODE` / `BO_REFERENCE_BET` environment variable
 *   overrides the file value, and a call may override any of them via its options.
 * * **Environment-scoped** values (endpoint + secrets) come from the per-environment .env files:
 *   `BO_API_BASE`, `BO_TOKEN` (ctoken), `BO_REFRESH_TOKEN` (rtoken).
 *
 * When the access token rotates, the new token is written to a per-environment
 * `.bo_token.{suffix}.json` cache at the repo root (gitignored) so it survives across runs. The file
 * is the same one the Python runner reads and writes, in the same format.
 *
 * Construction is cheap and never touches the network; the underlying client is built lazily on
 * the first BO call, and missing credentials raise only then (so a suite without BO steps runs fine
 * without BO config).
 */
import { existsSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import type { BoLike } from "../../engine/drive.js";
import { now } from "../../engine/page.js";
import { type Dict, get, isConversionError, isDict, or, PyAttributeError, pyInt, pyRepr, PyRuntimeError, pyStr, pyTypeName, PyValueError, truthy } from "../../py.js";
import { envFileSuffix, loadEnvFiles, loadGameConfig, REPO_ROOT } from "../env.js";
import { resetAgentPaytableToDefault, setAgentPaytable } from "./agent-game-settings.js";
import { ApiError, AuthError, BoApiClient, type BoApiClientOptions, jwtExpiry } from "./api-client.js";
import { excStr, JSONDecodeError, jsonDumps, pyJsonLoads, pyStrip, textModeNewlines } from "./compat.js";
import {
  assignAgentDenomination,
  ensureAgentAutomationDenomination,
  resetAgentDenominationToDefault,
} from "./denominations.js";
import { getLogger } from "./log.js";
import { ensureGamePaytable, sortedBy, updateGamePaytable } from "./update-game-paytable.js";

const logger = getLogger("framework.helpers.bo");

/** Raised when BO is invoked but required config/credentials are missing. */
export class BoConfigError extends PyRuntimeError {
  override name = "BoConfigError";
}

export { ApiError, AuthError };

type Cast = "int" | "str" | "float";

/** Per-agent target overrides (`agent_id=` / `currency_id=` / `game_code=` in Python). */
export interface AgentTarget {
  agentId?: unknown;
  currencyId?: unknown;
  gameCode?: string | null;
}

/** Expose BO paytable updates to the test runner. */
export class BoHelper implements BoLike {
  readonly game: string;
  readonly env: string;
  readonly baseDir: string;
  // Master-game identifiers (ensure/update paytable content).
  gameId: any;
  sasEntityId: any;
  // Per-agent identifiers (select/reset which paytable the agent plays).
  agentId: string | null;
  currencyId: any;
  gameCode: string | null;
  /** The bet a scenario is expected to play at, so a suite can express its max-win cap as a
   * MULTIPLE of the bet (`paytable: { max_win_x: 10 }`). The cap is applied before the game loads,
   * so the runner cannot read the real stake — this is the stand-in. (A STRING, as the Python read
   * it: `_cfg` casts only `int`.) */
  referenceBet: any;
  /** The automation paytable's `code` differs per game — Zeus's master game carries
   * "AutomationTest", Chaos's carries "Automation_test". Writing to a code the game does not have
   * does NOT fail loudly enough: the run reports a paytable was set while the game keeps playing
   * its real weights. Declare it per game; null keeps the historical default so Zeus/Mystic are
   * unaffected. */
  paytableCode: string | null;
  private token: string;
  private refreshToken: string;
  private readonly apiBase: string | null;
  private readonly tokenCachePath: string;
  private clientInstance: BoApiClient | null = null;

  constructor(game: string, env: string, baseDir: string | null = null) {
    this.game = game;
    this.env = env;
    this.baseDir = baseDir ? resolve(baseDir) : REPO_ROOT;

    // Ensure the per-environment .env overlay is loaded (idempotent — the suite runner also loads
    // it; this makes standalone BoHelper usage self-sufficient).
    loadEnvFiles(this.env, this.baseDir);

    // Game-scoped identifiers come from games/{game}/env.config.yml (bo: block); a matching BO_*
    // environment variable still overrides the file value.
    const gameCfg = this.loadGameBoConfig();
    this.gameId = this.cfg("BO_GAME_ID", "game_id", gameCfg, "int");
    this.sasEntityId = this.cfg("BO_SAS_ENTITY_ID", "sas_entity_id", gameCfg, "int");
    this.agentId = this.cfg("BO_AGENT_ID", "agent_id", gameCfg, "str");
    this.currencyId = this.cfg("BO_CURRENCY_ID", "currency_id", gameCfg, "int");
    this.gameCode = this.cfg("BO_GAME_CODE", "game_code", gameCfg, "str");
    this.referenceBet = this.cfg("BO_REFERENCE_BET", "reference_bet", gameCfg, "float");
    this.paytableCode = this.cfg("BO_PAYTABLE_CODE", "paytable_code", gameCfg, "str");
    // Environment-scoped endpoint + secrets (from .env / .env.{suffix}).
    this.token = pyStrip(process.env.BO_TOKEN ?? "");
    this.refreshToken = pyStrip(process.env.BO_REFRESH_TOKEN ?? "");
    this.apiBase = pyStrip(process.env.BO_API_BASE ?? "") || null;

    // Per-environment token cache (rotated access token); newer than .env.
    this.tokenCachePath = join(this.baseDir, `.bo_token.${envFileSuffix(this.env)}.json`);
    this.loadTokenCache();
  }

  // ------------------------------------------------------------------
  // Config / credential loading
  // ------------------------------------------------------------------

  /** Read an integer environment variable; null if unset/invalid. */
  static intEnv(name: string): number | null {
    const raw = pyStrip(process.env[name] ?? "");
    if (!raw) return null;
    try {
      return pyInt(raw);
    } catch (e) {
      if (!isConversionError(e)) throw e;
      logger.warning("Ignoring non-integer %s=%r", name, raw);
      return null;
    }
  }

  /** The game-scoped `bo:` block from games/{game}/env.config.{suffix}.yml. Missing file/section ->
   * empty (config may instead come from BO_* env vars). */
  private loadGameBoConfig(): Dict {
    let config: Record<string, any>;
    try {
      config = loadGameConfig(this.game, this.env, this.baseDir);
    } catch (exc) {
      if ((exc as Error).name === "FileNotFoundError") return {};
      if ((exc as Error).name === "ValueError") {
        logger.warning("Could not read BO config: %s", excStr(exc));
        return {};
      }
      throw exc;
    }
    const bo = or(get(config, "bo"), {});
    return isDict(bo) ? { ...bo } : (Object.fromEntries(bo as any) as Dict);
  }

  /** One BO config value: BO_* env var (override) then env.config.yml `bo:` value. `cast` "int"
   * converts; anything else is `str()` (so "float" is a string — as in the Python). null when
   * unset/invalid. */
  private cfg(envName: string, yamlKey: string, gameCfg: Dict, cast: Cast): any {
    const raw = pyStrip(process.env[envName] ?? "");
    const value: unknown = raw ? raw : get(gameCfg, yamlKey);
    if (value === null || value === "") return null;
    if (cast === "int") {
      try {
        return pyInt(value);
      } catch (e) {
        if (!isConversionError(e)) throw e;
        logger.warning("Ignoring non-integer BO config %s=%r", yamlKey, value);
        return null;
      }
    }
    return pyStr(value);
  }

  /** Adopt the cached tokens, but NEVER a cached access token that is already dead.
   *
   * The cache exists so a token rotated mid-run survives into the next process. It used to override
   * .env unconditionally, which quietly broke the opposite case: paste a fresh token into
   * .env.{suffix} while a stale cache is on disk, and the stale one still wins. The run then
   * authenticates with an expired token, tries to refresh it, and fails with UNAUTHORIZED_USER --
   * because `auth/refresh-token` requires a token that is still VALID -- while the same fresh token
   * works perfectly in Postman. Cost an hour on 2026-09-04.
   *
   * So the cache is only preferred while it is actually usable: a cached access token that has
   * expired, or is older than the one in .env, is ignored and the .env value stands. */
  private loadTokenCache(): void {
    if (!existsSync(this.tokenCachePath)) return;
    let cached: unknown;
    try {
      cached = pyJsonLoads(readFileSync(this.tokenCachePath, "utf-8"));
    } catch (exc) {
      logger.warning("Ignoring unreadable BO token cache %s: %s", this.tokenCachePath, excStr(exc));
      return;
    }
    if (!isDict(cached)) throw new PyAttributeError(`'${pyTypeName(cached)}' object has no attribute 'get'`);
    const cachedToken = pyStrip(pyStr(or(get(cached, "token"), "")));
    if (cachedToken) {
      const nowS = now();
      const cachedExp = jwtExpiry(cachedToken);
      const envExp = jwtExpiry(this.token);
      if (cachedExp !== null && cachedExp <= nowS) {
        logger.warning(
          "Ignoring BO token cache %s: its access token expired %.0f min ago. "
          + "Using the token from the .env overlay instead.",
          basename(this.tokenCachePath), (nowS - cachedExp) / 60);
      } else if (envExp !== null && cachedExp !== null && envExp > cachedExp) {
        logger.info("Ignoring BO token cache %s: the .env token is newer.", basename(this.tokenCachePath));
      } else {
        this.token = cachedToken;
        // Only adopt the cached refresh token alongside the access token it was saved with;
        // pairing a cached refresh token with an .env access token mixes sessions.
        if (truthy(get(cached, "refresh_token"))) this.refreshToken = pyStrip(pyStr(cached.refresh_token));
      }
    }
  }

  /** onTokenRefresh callback — write the rotated tokens to the cache.
   *
   * The refresh token is written too. dev does not rotate it today, but if it ever starts to,
   * caching the ACCESS token beside a stale REFRESH token would hand the next process a pair that
   * cannot renew itself. */
  persistToken(newToken: string, newRefreshToken: string | null = null): void {
    if (truthy(newRefreshToken)) this.refreshToken = newRefreshToken as string;
    try {
      writeFileSync(
        this.tokenCachePath,
        textModeNewlines(jsonDumps({ token: newToken, refresh_token: this.refreshToken }, { indent: 2 })),
        { encoding: "utf-8" },
      );
      logger.info("Persisted rotated BO token to %s", basename(this.tokenCachePath));
    } catch (exc) {
      logger.warning("Could not persist rotated BO token: %s", excStr(exc));
    }
  }

  /** Delete the per-environment rotated-token cache file (if present). */
  clearTokenCache(): void {
    try {
      if (existsSync(this.tokenCachePath)) {
        unlinkSync(this.tokenCachePath);
        logger.info("Cleared BO token cache %s", basename(this.tokenCachePath));
      }
    } catch (exc) {
      logger.warning("Could not clear BO token cache %s: %s", this.tokenCachePath, excStr(exc));
    }
  }

  // ------------------------------------------------------------------
  // Client (lazy)
  // ------------------------------------------------------------------

  /** Build the BoApiClient on first use; raise a clear error if creds missing. */
  private client(): BoApiClient {
    if (this.clientInstance !== null) return this.clientInstance;
    if (!this.token || !this.refreshToken) {
      throw new BoConfigError(
        `BO credentials missing — set BO_TOKEN and BO_REFRESH_TOKEN in `
        + `.env.${envFileSuffix(this.env)} (grab ctoken/rtoken from browser DevTools).`,
      );
    }
    const opts: BoApiClientOptions = { onTokenRefresh: (t, rt) => this.persistToken(t, rt) };
    if (this.apiBase) opts.apiBase = this.apiBase;
    this.clientInstance = new BoApiClient(this.token, this.refreshToken, opts);
    return this.clientInstance;
  }

  // ------------------------------------------------------------------
  // Paytable resolution
  // ------------------------------------------------------------------

  private paytablesDir(): string {
    return join(this.baseDir, "games", this.game, "paytables");
  }

  /**
   * Resolve the paytable content for a step.
   *
   * `inline` (dict/list/JSON string) takes precedence. Otherwise `ref` names a fixture file
   * `games/{game}/paytables/{ref}.json`. Raises ValueError if neither is usable.
   */
  resolvePaytable(opts: { ref?: string | null; inline?: unknown } = {}): any {
    const ref = opts.ref ?? null;
    const inline = opts.inline ?? null;
    if (inline !== null) return inline;
    if (!ref) throw new PyValueError("paytable update requires either 'paytable' (inline) or 'paytable_ref'");
    // Allow ref with or without a .json extension.
    const dir = this.paytablesDir();
    const candidate = resolve(dir, ref.endsWith(".json") ? ref : `${ref}.json`);
    const name = basename(candidate);
    if (!existsSync(candidate)) {
      throw new PyValueError(`No paytable fixture ${pyRepr(name)} in ${dir}. Available: ${pyRepr(fixtureStems(dir))}`);
    }
    const text = readFileSync(candidate, "utf-8");
    try {
      return pyJsonLoads(text);
    } catch (exc) {
      if (!(exc instanceof JSONDecodeError)) throw exc;
      throw new PyValueError(`Paytable fixture ${pyRepr(name)} is not valid JSON: ${excStr(exc)}`, { cause: exc });
    }
  }

  // ------------------------------------------------------------------
  // Paytable update
  // ------------------------------------------------------------------

  private masterTarget(gameId: unknown, sasEntityId: unknown, hint: string): [unknown, unknown] {
    const gid = gameId !== null && gameId !== undefined ? gameId : this.gameId;
    const seid = sasEntityId !== null && sasEntityId !== undefined ? sasEntityId : this.sasEntityId;
    if (gid === null || gid === undefined || seid === null || seid === undefined) {
      throw new BoConfigError(
        "BO game_id/sas_entity_id missing — set game_id/sas_entity_id under the "
        + `bo: block in games/${this.game}/env.config.${envFileSuffix(this.env)}.yml `
        + hint,
      );
    }
    return [gid, seid];
  }

  /**
   * Update one paytable (matched by `code`) on the configured game.
   *
   * `gameId` / `sasEntityId` default to the env.config.yml bo: values. Propagates AuthError /
   * ApiError / ValueError to the caller.
   */
  async updatePaytable(paytableCode: string, newPaytable: unknown, gameId: unknown = null, sasEntityId: unknown = null): Promise<Record<string, any>> {
    const [gid, seid] = this.masterTarget(gameId, sasEntityId, "(or BO_GAME_ID/BO_SAS_ENTITY_ID env vars, or the step params).");
    const client = this.client();
    return updateGamePaytable(client, gid, seid, paytableCode, newPaytable);
  }

  // ------------------------------------------------------------------
  // Master-game paytable existence (ensure the automation paytable)
  // ------------------------------------------------------------------

  /**
   * Ensure a paytable named `paytableCode` exists on the master game so it becomes selectable
   * per-agent; return its server-side id.
   *
   * Idempotent: if the paytable already exists, no write happens. When the paytable is created and
   * `paytableContent` is omitted, it is cloned from the game's default paytable. Run this once in
   * suite setup.
   */
  async ensurePaytable(
    paytableCode = "AutomationTest",
    opts: { paytableContent?: unknown; rtp?: unknown; gameId?: unknown; sasEntityId?: unknown } = {},
  ): Promise<any> {
    const [gid, seid] = this.masterTarget(opts.gameId, opts.sasEntityId, "(or BO_GAME_ID/BO_SAS_ENTITY_ID env vars, or the step params).");
    const client = this.client();
    return ensureGamePaytable(client, gid, seid, paytableCode, opts.paytableContent ?? null,
      opts.rtp !== undefined ? opts.rtp : "MEDIUM");
  }

  // ------------------------------------------------------------------
  // Per-agent paytable selection (which paytable the agent plays)
  // ------------------------------------------------------------------

  /** Resolve (agentId, currencyId, gameCode) from options then env. */
  private agentTarget(opts: AgentTarget = {}): [string, any, string] {
    const pick = (v: unknown, dflt: unknown) => (v !== null && v !== undefined ? v : dflt);
    const aid = pick(opts.agentId, this.agentId);
    const cid = pick(opts.currencyId, this.currencyId);
    const code = pick(opts.gameCode, this.gameCode);
    const missing = ([["BO_AGENT_ID", aid], ["BO_CURRENCY_ID", cid], ["BO_GAME_CODE", code]] as const)
      .filter(([, val]) => val === null || val === undefined || val === "")
      .map(([name]) => name);
    if (missing.length) {
      throw new BoConfigError(
        `BO agent config missing (${missing.join(", ")}) — set agent_id/currency_id/`
        + "game_code under the bo: block in "
        + `games/${this.game}/env.config.${envFileSuffix(this.env)}.yml (or the `
        + "matching BO_* env vars, or the step params).",
      );
    }
    return [pyStr(aid), cid, pyStr(code)];
  }

  /**
   * Point the agent's game at the named paytable (e.g. `AutomationTest`) so spins use it. Setup
   * step — preserves every other game-settings field.
   *
   * `maxPayout` (optional) also caps the agent's payout for this game. A forced paytable can award
   * a bonus that runs for hundreds of spins; the cap is what ends it — the server truncates at the
   * cap and sets `hasReachedMaxWin`, which the framework treats as terminal. The teardown clears
   * the override, so a cap cannot leak into the next run.
   */
  async selectAgentPaytable(paytableCode: string, opts: AgentTarget & { maxPayout?: unknown } = {}): Promise<Record<string, any>> {
    const [aid, cid, code] = this.agentTarget(opts);
    const client = this.client();
    return setAgentPaytable(client, aid, cid, code, paytableCode, null, opts.maxPayout ?? null);
  }

  /** Reset the agent's game paytable back to the game default (auto-discovered). Teardown step —
   * undoes `selectAgentPaytable`. */
  async resetAgentPaytable(opts: AgentTarget = {}): Promise<Record<string, any>> {
    const [aid, cid, code] = this.agentTarget(opts);
    const client = this.client();
    return resetAgentPaytableToDefault(client, aid, cid, code);
  }

  // ------------------------------------------------------------------
  // Denominations — the BET LADDER the agent's game row offers.
  //
  // These are the paytable pair's twin: `ensureAutomationDenomination` is SETUP and
  // `resetAgentDenomination` is its TEARDOWN, and a run that calls the first MUST declare the
  // second or the next run inherits a bet ladder of 11/22/33 and every bet assert in the suite
  // goes red for a reason nothing in that suite mentions.
  //
  // ⚠ The automation ladder is (11, 22, 33) ON PURPOSE (`AUTOMATION_BET_LEVELS`). Those values do
  // not occur in any real denomination, so a test can tell from ONE number whether a stake came
  // from the automation ladder or the game's own — which is the whole point of a
  // denomination-swap case. Do not "tidy" them into round numbers.
  //
  // ⚠ NEVER RUN LIVE as of 2026-09-16: `framework/bo/denominations.py` was written and tested
  // offline against captured payloads only. The first live run should read the list first.
  // ------------------------------------------------------------------

  /** SETUP: put this agent's game row on the automation bet ladder, creating it if needed.
   *
   * Returns `{denomination_id, name, created, sub_category_id, previous_denomination_id, ...}`.
   * `previous_denomination_id` is what the row used BEFORE — hand it to `resetAgentDenomination`
   * so the teardown restores exactly what was there. */
  async ensureAutomationDenomination(
    opts: AgentTarget & { gameId?: unknown; sasEntityId?: unknown; betLevels?: unknown } = {},
  ): Promise<Dict> {
    const [aid, cid, code] = this.agentTarget(opts);
    const [gid, seid] = this.masterTarget(opts.gameId, opts.sasEntityId, "(or BO_GAME_ID/BO_SAS_ENTITY_ID env vars).");
    const client = this.client();
    const kwargs: { betLevels?: unknown } = {};
    if (opts.betLevels !== null && opts.betLevels !== undefined) kwargs.betLevels = [...(opts.betLevels as Iterable<unknown>)];
    return ensureAgentAutomationDenomination(client, gid, seid, aid, cid, code, kwargs);
  }

  /** Point the agent's game row at `denominationId` WITH the override on.
   *
   * The teardown half of a run whose row already overrode the denomination before the test:
   * restoring that state means keeping `isDenomOverride: 1`, because BO only accepts
   * `isDenomOverride: 0` alongside the master game's id. */
  async assignAgentDenomination(denominationId: unknown, opts: AgentTarget = {}): Promise<Record<string, any>> {
    const [aid, cid, code] = this.agentTarget(opts);
    const client = this.client();
    return assignAgentDenomination(client, aid, cid, code, denominationId);
  }

  /** TEARDOWN: clear the row's denomination override so it follows the master game again.
   *
   * ⚠ `denominationId` must be the MASTER game's denomination for this currency
   * (`master_denomination_id` from the setup). BO rejects `isDenomOverride: 0` sent with any other
   * id — measured on dev 2026-09-18 — and leaves the row overridden. */
  async resetAgentDenomination(opts: AgentTarget & { denominationId?: unknown } = {}): Promise<Record<string, any>> {
    const [aid, cid, code] = this.agentTarget(opts);
    const client = this.client();
    return resetAgentDenominationToDefault(client, aid, cid, code, { denominationId: opts.denominationId ?? null });
  }
}

/** `sorted(p.stem for p in dir.glob("*.json"))` — case-insensitive on Windows, as pathlib is. */
function fixtureStems(dir: string): string[] {
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }
  const re = process.platform === "win32" ? /\.json$/i : /\.json$/;
  return sortedBy(names.filter((n) => re.test(n)).map((n) => {
    const dot = n.lastIndexOf(".");
    return dot > 0 ? n.slice(0, dot) : n;
  }));
}
