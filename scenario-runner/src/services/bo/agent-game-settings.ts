/**
 * Update an agent's per-game paytable in the Royaledge backoffice (the /agent/game/settings/update/:id
 * page). Port of framework/bo/agent_game_settings.py.
 *
 * Mirrors the frontend flow (src/pages/Agent/GameSetting/GameSettingUpdate.tsx):
 *
 *   GET   agentgamecurrency/view     -> agent's game settings list (per currency)
 *       body: { page, pageSize, sortBy, sortDirection, filters: { gameCode }, agentId, currencyId }
 *       NOTE: filters.gameCode is a *partial* match, so the response returns the whole game family
 *       (..._VIP, ..._KR, ...) — we exact-match gameCode in code.
 *
 *   (pick the game row by exact gameCode, then pick a paytable from payTableList by its name =
 *    payTableCode, e.g. "AutomationTest" -> payTableId 362)
 *
 *   POST  agentgamecurrency/update   -> { gameSettings:[row], autoFinalization, agentId }
 *
 * Submit row shape (GameSettingUpdate.tsx:120-150):
 *   { id, status, maxPayoutCustom, override, payTableId, denominationId,
 *     rtpLevel, maxMultiplier, isDenomOverride, isDefaultPaytable }
 *
 * Paytable flag (PAYTABLE_DEFAULT_STATUS, reversed vs denom):
 *   isDefaultPaytable: 1 = following the game default, 0 = overridden
 *   -> derived from the chosen paytable's isDefault flag, so:
 *        AutomationTest (isDefault=0) -> 0   (your setup payload)
 *        the default paytable (isDefault=1) -> 1   (your teardown payload)
 *
 * Auth + token refresh is delegated to BoApiClient (api-client.ts).
 */
import { type Dict, get, isDict, or, PyTuple, pyEq, pyFloat, pyList, pyRepr, pyStr, PyValueError, truthy } from "../../py.js";
import type { BoClientLike } from "./api-client.js";
import { item, pyStrip } from "./compat.js";
import { getLogger } from "./log.js";

const logger = getLogger("framework.bo.agent_game_settings");

export const STATUS_ACTIVE = 1;
export const STATUS_INACTIVE = 0;

// PAYTABLE_DEFAULT_STATUS (GameSettingUpdate.tsx:69-72)
export const PAYTABLE_FOLLOWING_DEFAULT = 1; // DEFAULT
export const PAYTABLE_OVERRIDDEN = 0; // OVERRIDDEN

export const DEFAULT_AUTO_FINALIZATION: Readonly<Dict> = {
  autoFinalizationStatus: 1,
  autoFinalizationDuration: "0.2",
};

/** Mimic JS Number(): drop trailing .0 so 300000.0000 -> 300000 (int). */
export function num(value: unknown): number {
  const f = pyFloat(value);
  return Number.isInteger(f) ? f || 0 : f;
}

/** `row.get(k) or []` iterated: a list, or (for a dict) its keys, as Python's `for` would. */
function rowsOf(v: unknown): any[] {
  return truthy(v) ? pyList(v) : [];
}

// ---------------------------------------------------------------------------
// Low-level API
// ---------------------------------------------------------------------------

/** POST currency/list_by_agent -> list of {id, code, ...} for the agent. */
export async function getAgentCurrencies(client: BoClientLike, agentId: unknown): Promise<any> {
  const body = await client.post("currency/list_by_agent", { agentId: pyStr(agentId) });
  return or(get(body, "data"), []);
}

export interface AgentGameSettingsQuery {
  gameCode?: string | null;
  page?: number;
  pageSize?: number;
  sortBy?: string;
  sortDirection?: string;
}

/**
 * POST agentgamecurrency/view -> the `data` object:
 *   { gameSettings: [row, ...], maxPayout, total, autoFinalizationStatus, autoFinalizationDuration, ... }
 */
export async function getAgentGameSettings(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  opts: AgentGameSettingsQuery = {},
): Promise<any> {
  const request: Dict = {
    page: opts.page ?? 1,
    pageSize: opts.pageSize ?? 100,
    sortBy: opts.sortBy ?? "id",
    sortDirection: opts.sortDirection ?? "desc",
    agentId: pyStr(agentId),
    currencyId: currencyId ?? null,
  };
  if (truthy(opts.gameCode)) request.filters = { gameCode: opts.gameCode };
  const body = await client.post("agentgamecurrency/view", request);
  return item(body, "data");
}

/**
 * POST agentgamecurrency/update with the exact submit payload. `gameSettings` is a list of
 * fully-built rows. Use this directly when you already know every field. traceId is injected
 * automatically by the client.
 */
export async function updateAgentGameSettings(
  client: BoClientLike,
  agentId: unknown,
  gameSettings: unknown[],
  autoFinalization: Dict | null = null,
): Promise<Record<string, any>> {
  const payload: Dict = {
    gameSettings,
    autoFinalization: truthy(autoFinalization) ? autoFinalization : { ...DEFAULT_AUTO_FINALIZATION },
    agentId: pyStr(agentId),
  };
  return client.post("agentgamecurrency/update", payload);
}

// ---------------------------------------------------------------------------
// Helpers: locate the game row and the paytable
// ---------------------------------------------------------------------------

/** gameSettings may be an array or an object/map — the row values. */
export function iterGameRows(settingsData: unknown): any[] {
  const gameSettings = or(get(settingsData, "gameSettings"), {});
  if (isDict(gameSettings)) return Object.values(gameSettings);
  return pyList(gameSettings);
}

/**
 * Find one game row by EXACT gameCode (preferred) or by row id.
 * The GET filter is partial, so exact-matching here is required.
 */
export function findGameRow(
  settingsData: unknown,
  opts: { gameCode?: unknown; gameSettingId?: unknown } = {},
): any {
  const gameCode = opts.gameCode ?? null;
  const gameSettingId = opts.gameSettingId ?? null;
  const rows = iterGameRows(settingsData);
  for (const row of rows) {
    if (gameCode !== null && pyEq(get(row, "gameCode"), gameCode)) return row;
    if (gameSettingId !== null && pyEq(get(row, "id"), gameSettingId)) return row;
  }
  const available = rows.map((r) => new PyTuple([get(r, "id"), get(r, "gameCode")]));
  throw new PyValueError(
    `Game row not found (gameCode=${pyRepr(gameCode)}, id=${pyStr(gameSettingId)}). `
    + `Available (id, gameCode): ${pyRepr(available)}`,
  );
}

/** Find a paytable in row.payTableList by its name (payTableCode). */
export function findPaytableByCode(row: unknown, paytableCode: unknown): any {
  for (const pt of rowsOf(get(row, "payTableList"))) {
    if (pyEq(get(pt, "payTableCode"), paytableCode)) return pt;
  }
  const available = rowsOf(get(row, "payTableList")).map((p) => get(p, "payTableCode"));
  throw new PyValueError(
    `Paytable ${pyRepr(paytableCode)} not found for game ${pyRepr(get(row, "gameCode"))}. `
    + `Available: ${pyRepr(available)}`,
  );
}

/**
 * Return the game-default paytable (isDefault == ACTIVE) from row.payTableList.
 * If several defaults exist, prefer one matching preferRtpLevel.
 */
export function findDefaultPaytable(row: unknown, preferRtpLevel: unknown = null): any {
  const defaults = rowsOf(get(row, "payTableList")).filter((p) => pyEq(get(p, "isDefault"), STATUS_ACTIVE));
  if (!defaults.length) {
    const available = rowsOf(get(row, "payTableList"))
      .map((p) => new PyTuple([get(p, "payTableId"), get(p, "payTableCode")]));
    throw new PyValueError(`No default paytable for game ${pyRepr(get(row, "gameCode"))}. Available: ${pyRepr(available)}`);
  }
  if (truthy(preferRtpLevel)) {
    for (const p of defaults) {
      if (pyEq(get(p, "rtpLevel"), preferRtpLevel)) return p;
    }
  }
  return defaults[0];
}

/**
 * The paytable the row is on RIGHT NOW — the payTableList entry whose payTableId matches.
 *
 * For a change that is not about the paytable (a denomination assignment), `buildSubmitRow` still
 * needs a paytable to rebuild the row from. Falls back to a synthetic entry carrying the row's own
 * ids, so a row whose list does not include its current paytable still round-trips unchanged
 * instead of being silently moved to another one.
 */
export function findCurrentPaytable(row: unknown): any {
  for (const pt of rowsOf(get(row, "payTableList"))) {
    if (pyEq(get(pt, "payTableId"), get(row, "payTableId"))) return pt;
  }
  return {
    payTableId: get(row, "payTableId"),
    isDefault: pyEq(get(row, "isDefaultPaytable"), PAYTABLE_FOLLOWING_DEFAULT) ? STATUS_ACTIVE : STATUS_INACTIVE,
  };
}

/** `_is_nan`: true when the value does NOT convert with `float()` (not "is the float NaN"). */
export function isNan(value: unknown): boolean {
  try {
    pyFloat(value);
    return false;
  } catch (e) {
    if ((e as Error).name === "TypeError" || (e as Error).name === "ValueError") return true;
    throw e;
  }
}

export interface SubmitRowOptions {
  /** a number caps the payout; `false` clears the override; null/undefined preserves */
  maxPayout?: unknown;
  denominationId?: unknown;
  isDenomOverride?: unknown;
}

/**
 * Build one submit gameSettings row from a raw GET row + a chosen paytable, changing only the
 * paytable and preserving every other current value. Reproduces handleOnBeforeSubmit +
 * getInitialMaxPayout field-by-field.
 *
 * isDefaultPaytable is derived from the chosen paytable's isDefault flag.
 *
 * `maxPayout` OVERRIDES the agent's max payout for this game:
 *   * a number -> turn the override ON and cap the payout at that amount. A forced paytable can
 *     award a very long bonus (a Chaos free-spin run at a 40% scatter weight retriggers for
 *     hundreds of spins), and the cap is what makes such a run END: the server truncates at the
 *     cap and flags `hasReachedMaxWin`, which the framework already treats as terminal.
 *   * `false` -> clear the override, restoring the agent's default cap (used by the teardown, so
 *     a test's cap cannot leak into the next run).
 *   * null (default) -> preserve whatever the row already has.
 *
 * `denominationId` / `isDenomOverride` do the same job for the DENOMINATION (the callers are in
 * denominations.ts). null preserves the row's current value, which is what keeps a paytable change
 * from moving the denomination and vice versa. `isDenomOverride` truthy = this agent OVERRIDES the
 * master game's denomination (1); falsy = follow the master default (0) — the reverse reading of
 * `isDefaultPaytable` above.
 */
export function buildSubmitRow(row: unknown, paytable: unknown, maxPayoutDefault: unknown, opts: SubmitRowOptions = {}): Dict {
  const maxPayout = opts.maxPayout ?? null;
  const denominationId = opts.denominationId ?? null;
  const isDenomOverride = opts.isDenomOverride ?? null;
  // getInitialMaxPayout (GameSettingUpdate.tsx:287-303)
  let override: unknown = or(get(row, "override"), STATUS_INACTIVE);
  const custom = get(row, "maxPayoutCustom");
  let effectiveMax: unknown;
  if (pyEq(override, STATUS_INACTIVE) || !truthy(custom) || isNan(custom)) effectiveMax = maxPayoutDefault ?? null;
  else effectiveMax = custom;
  if (maxPayout === false) {                // teardown: back to the agent default
    override = STATUS_INACTIVE;
    effectiveMax = maxPayoutDefault ?? null;
  } else if (maxPayout !== null) {          // setup: cap this game for the run
    override = STATUS_ACTIVE;
    effectiveMax = maxPayout;
  }

  const isDefaultPaytable = pyEq(get(paytable, "isDefault"), STATUS_ACTIVE) ? PAYTABLE_FOLLOWING_DEFAULT : PAYTABLE_OVERRIDDEN;

  return {
    id: get(row, "id"),
    status: !truthy(get(row, "status")) ? STATUS_INACTIVE : STATUS_ACTIVE,
    // JS: maxPayoutCustom ? Number(...) : null
    maxPayoutCustom: truthy(effectiveMax) ? num(effectiveMax) : null,
    override: !truthy(override) ? STATUS_INACTIVE : STATUS_ACTIVE,
    payTableId: get(paytable, "payTableId"),
    // `denominationId` / `isDenomOverride` default to null = PRESERVE, so a paytable change never
    // moves the denomination, and a denomination change never moves the paytable.
    denominationId: denominationId === null ? get(row, "denominationId") : denominationId,
    rtpLevel: get(row, "rtpLevel"),
    // JS: maxMultiplier ? Number(...) : null  (0 -> null)
    maxMultiplier: truthy(get(row, "maxMultiplier")) ? num(item(row, "maxMultiplier")) : null,
    isDenomOverride: isDenomOverride === null
      ? get(row, "isDenomOverride")
      : (truthy(isDenomOverride) ? STATUS_ACTIVE : STATUS_INACTIVE),
    isDefaultPaytable,
  };
}

/** Preserve the agent's current auto-finalization (frontend submits it too). */
export function autoFinalizationFrom(settingsData: unknown): Dict {
  return {
    autoFinalizationStatus: truthy(get(settingsData, "autoFinalizationStatus")) ? 1 : 0,
    autoFinalizationDuration: pyStrip(pyStr(or(get(settingsData, "autoFinalizationDuration"), "0.2"))),
  };
}

// ---------------------------------------------------------------------------
// High-level: set a paytable by name / reset to default
// ---------------------------------------------------------------------------

/**
 * SETUP: point `gameCode`'s paytable at the one named `paytableCode` (e.g. "AutomationTest").
 * Fetches current settings, exact-matches the game, preserves all other fields, submits one row.
 * Returns the update response.
 *
 * `maxPayout` (optional) also caps the agent's payout for this game — see buildSubmitRow.
 */
export async function setAgentPaytable(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
  paytableCode: unknown,
  autoFinalization: Dict | null = null,
  maxPayout: unknown = null,
): Promise<Record<string, any>> {
  const data = await getAgentGameSettings(client, agentId, currencyId, { gameCode: gameCode as string });
  const row = findGameRow(data, { gameCode });
  const paytable = findPaytableByCode(row, paytableCode);
  const submitRow = buildSubmitRow(row, paytable, get(data, "maxPayout"), { maxPayout });
  const af = truthy(autoFinalization) ? autoFinalization : autoFinalizationFrom(data);
  logger.info(
    "Set paytable: agent=%s game=%s id=%s -> %s (payTableId=%s, isDefaultPaytable=%s)",
    agentId, gameCode, submitRow.id, paytableCode, submitRow.payTableId, submitRow.isDefaultPaytable,
  );
  return updateAgentGameSettings(client, agentId, [submitRow], af);
}

/**
 * TEARDOWN: reset `gameCode`'s paytable to the game default (isDefault==1). The default paytable id
 * is discovered from payTableList — no hardcoding. Returns the update response.
 */
export async function resetAgentPaytableToDefault(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
  autoFinalization: Dict | null = null,
): Promise<Record<string, any>> {
  const data = await getAgentGameSettings(client, agentId, currencyId, { gameCode: gameCode as string });
  const row = findGameRow(data, { gameCode });
  const paytable = findDefaultPaytable(row, get(row, "rtpLevel"));
  // `false` clears any max-payout override a run set, so the cap never leaks into the next test.
  const submitRow = buildSubmitRow(row, paytable, get(data, "maxPayout"), { maxPayout: false });
  const af = truthy(autoFinalization) ? autoFinalization : autoFinalizationFrom(data);
  logger.info(
    "Reset paytable to default: agent=%s game=%s id=%s -> %s (payTableId=%s)",
    agentId, gameCode, submitRow.id, get(paytable, "payTableCode"), submitRow.payTableId,
  );
  return updateAgentGameSettings(client, agentId, [submitRow], af);
}

/**
 * For tests: set the chosen paytable, run `body`, and always reset to the game default afterwards
 * (even if the body throws). The Python context manager `agent_paytable(...)`.
 *
 *     await agentPaytable(client, "1", 18, "SL_33_ZAPPIN_ZEUS", "AutomationTest", async () => {
 *       ... // play the game
 *     });
 *     // paytable restored to default here
 */
export async function agentPaytable<T>(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
  paytableCode: unknown,
  body: () => Promise<T>,
): Promise<T> {
  await setAgentPaytable(client, agentId, currencyId, gameCode, paytableCode);
  try {
    return await body();
  } finally {
    await resetAgentPaytableToDefault(client, agentId, currencyId, gameCode);
  }
}
