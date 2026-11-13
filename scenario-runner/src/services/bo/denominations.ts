/**
 * Denominations in the Royaledge back-office: the bet ladder a game offers in one currency (port of
 * framework/bo/denominations.py). Read them, create one, edit one, and assign one — to the MASTER
 * game, or to ONE agent's game row.
 *
 * Three levels, and they are not interchangeable:
 *
 * | Level | Endpoints | Field that decides |
 * |---|---|---|
 * | the denomination itself | `denomination/view/list` -> `denomination/update` | `betLevels`, `defaultBetLevel`, `name` — shared by every game of that SUB-CATEGORY + currency |
 * | master game | `game/view` -> `game/update/v2` | `currencyInfoList[].denominationId` — the default every agent inherits for that currency |
 * | agent game | `agentgamecurrency/view` -> `agentgamecurrency/update` | the row's `denominationId` **plus** `isDenomOverride` — one agent only |
 *
 * **A denomination belongs to a (game sub-category, currency) pair, not to a game.** `game/view`
 * gives the sub-category (`gameSubCategory.id`: 1 = 3x3, 2 = 3x5); the currency id comes from the
 * env config. Every 3x3 game in TKN therefore shares the same list, which is why a new one must be
 * named uniquely per pair — `automationDenominationName()` builds "automation test TKN 3x3".
 *
 * ⚠ **`denomination/update` is a FULL REPLACE, not a patch.** The body carries the whole list for
 * that pair, so every existing denomination must be sent back unchanged alongside the new or edited
 * one. Sending only the new entry would drop the rest — including the game defaults every other
 * agent is using. `updateDenominations()` is the only function that posts it, and every caller here
 * builds its list from a fresh `denomination/view/list` read.
 *
 * ⚠ **The two endpoints spell the sub-category differently**: the list takes `gameSubCategoryId`,
 * the update takes `subCategoryId`, and both take the ids as STRINGS.
 *
 * **`isDenomOverride` is what makes an agent row win.** `1` = this agent overrides the master
 * default; `0` = it follows the master game. Note it reads the OPPOSITE way round to the paytable
 * flag beside it (`isDefaultPaytable`: 1 = following the default) — see agent-game-settings.ts.
 *
 * Auth and token refresh are handled by `BoApiClient` (api-client.ts).
 */
import {
  type Dict, get, or, PyRuntimeError, PyTuple, pyEq, pyInt, pyList, pyRepr, pyStr, PyValueError, truthy,
} from "../../py.js";
import {
  autoFinalizationFrom, buildSubmitRow, findCurrentPaytable, findGameRow, getAgentGameSettings, STATUS_ACTIVE,
  STATUS_INACTIVE, updateAgentGameSettings,
} from "./agent-game-settings.js";
import type { BoClientLike } from "./api-client.js";
import { casefold, excStr, item, pySplitWs, pyStrip } from "./compat.js";
import { getLogger } from "./log.js";
import { buildUpdatePayload, getGameDetail, sortedBy, updateGame } from "./update-game-paytable.js";

const logger = getLogger("framework.bo.denominations");

// The agent row's denomination flag, reversed against `isDefaultPaytable` beside it.
export const DENOM_OVERRIDDEN = 1;
export const DENOM_FOLLOWING_DEFAULT = 0;

// The automation denomination this repo creates when a pair has none.
export const AUTOMATION_PREFIX = "automation test";
export const AUTOMATION_BET_LEVELS: readonly number[] = [11, 22, 33];
export const AUTOMATION_DEFAULT_LEVEL = 1;          // 1-BASED index into betLevels -> the 11 level

// The fields of a `currencyInfoList` entry worth reporting; the rest is not about denominations.
const CURRENCY_FIELDS = [
  "id", "code", "denominationId", "denomName",
  "minBet", "maxBet", "isDenomOverride", "isDefault",
] as const;
// What `denomination/update` accepts per entry. `id` is added for an existing one and OMITTED for a
// new one — that omission is what makes the back office create it.
const SUBMIT_FIELDS = ["name", "isDefault", "defaultBetLevel", "remarks", "betLevels"] as const;

/** `x.get(k) or []`, iterated. */
function listOf(d: unknown, key: string): any[] {
  const v = get(d, key);
  return truthy(v) ? pyList(v) : [];
}

// ---------------------------------------------------------------------------
// THE DENOMINATION ITSELF — read
// ---------------------------------------------------------------------------

/** POST `denomination/view/list` -> the `data` object for one (sub-category, currency) pair:
 * `{currency, gameSubCategory, denomType, denominations: [...]}`.
 *
 * Each denomination carries `{id, isDefault, name, defaultBetLevel, betLevels, totalLevel, minBet,
 * maxBet, remarks, lastUpdated}`. Ids are sent as strings, as the back office expects. */
export async function getDenominationList(
  client: BoClientLike,
  subCategoryId: unknown,
  currencyId: unknown,
  sortBy = "name",
  sortDirection = "desc",
): Promise<any> {
  const body = await client.post("denomination/view/list", {
    gameSubCategoryId: pyStr(subCategoryId),
    currencyId: pyStr(currencyId),
    sortBy,
    sortDirection,
  });
  return item(body, "data");
}

/** A denomination name reduced to what a comparison should actually care about.
 *
 * Case and runs of whitespace are NOT meaningful here: the back office happily stores
 * "Automation Test TRY 3x5" for a ladder a person created by hand, and it collides with
 * "automation test TRY 3x5" on the server's own uniqueness check while comparing unequal in
 * Python. Matching exactly is what made `ensureAutomationDenomination` miss an existing ladder
 * and then fail its create with DENOMINATION_NAME_ALREADY_EXISTS. */
export function normName(value: unknown): string {
  return casefold(pySplitWs(pyStr(or(value, ""))).join(" "));
}

/** One denomination from a `getDenominationList()` result, by name or by id.
 *
 * The name match is EXACT first and then case/whitespace-insensitive (`normName`), so a ladder
 * someone created by hand is found rather than duplicated. Returns null when there is no match —
 * callers decide whether that means "create it" (the automation flow) or "fail" (an explicit
 * edit), so this does not raise. */
export function findDenomination(listData: unknown, opts: { name?: unknown; denominationId?: unknown } = {}): any {
  const name = opts.name ?? null;
  const denominationId = opts.denominationId ?? null;
  const entries = listOf(listData, "denominations");
  for (const d of entries) {
    if (name !== null && pyEq(get(d, "name"), name)) return d;
    if (denominationId !== null && pyEq(get(d, "id"), denominationId)) return d;
  }
  if (name === null) return null;
  const want = normName(name);
  for (const d of entries) {
    if (normName(get(d, "name")) === want) return d;
  }
  return null;
}

/** Every `(id, name)` in a listing — for an error message that shows what IS there. */
export function denominationNames(listData: unknown): PyTuple[] {
  return listOf(listData, "denominations").map((d) => new PyTuple([get(d, "id"), get(d, "name")]));
}

/** The name this repo's automation denomination must carry for this pair.
 *
 * A denomination name has to be unique across the game category, so the plain prefix cannot be
 * reused for two currencies or two sub-categories. The list response names both halves, so the
 * name is built from what the server itself reports: "automation test TKN 3x3". */
export function automationDenominationName(listData: unknown, prefix: unknown = AUTOMATION_PREFIX): string {
  return pyStrip(`${pyStr(prefix)} ${pyStr(get(listData, "currency"))} ${pyStr(get(listData, "gameSubCategory"))}`);
}

// ---------------------------------------------------------------------------
// THE DENOMINATION ITSELF — create and edit
// ---------------------------------------------------------------------------

/** Reduce a listed denomination to what `denomination/update` accepts, keeping its id.
 *
 * `totalLevel`, `minBet`, `maxBet` and `lastUpdated` are derived by the back office from
 * `betLevels`; sending them back is at best noise. */
export function toSubmitDenomination(entry: unknown): Dict {
  const out: Dict = {};
  for (const k of SUBMIT_FIELDS) out[k] = get(entry, k);
  if (get(entry, "id") !== null) out.id = item(entry, "id");
  return out;
}

/** Bet levels as a sorted list of numbers, with `defaultBetLevel` checked against them.
 *
 * `defaultBetLevel` is a **1-based index** into `betLevels` (the BO payload for [11, 22, 33] with a
 * default of 11 sends `1`). An index outside the ladder would leave the game opening at a level
 * that does not exist, so it is refused here rather than written. */
export function validatedLevels(betLevels: unknown, defaultBetLevel: unknown): any[] {
  const levels = sortedBy(truthy(betLevels) ? pyList(betLevels) : []);
  if (!levels.length) throw new PyValueError("bet_levels must not be empty");
  const lvl = pyInt(defaultBetLevel);
  if (!(lvl >= 1 && lvl <= levels.length)) {
    throw new PyValueError(
      `default_bet_level ${pyStr(defaultBetLevel)} is outside betLevels ${pyRepr(levels)} `
      + `(it is a 1-based index: 1 selects ${pyStr(levels[0])})`,
    );
  }
  return levels;
}

/** POST `denomination/update` with the COMPLETE list for this pair.
 *
 * ⚠ Full replace. `denominations` must contain every existing entry (via `toSubmitDenomination`)
 * plus whatever is being added or changed. Note the field is `subCategoryId` here, not the
 * `gameSubCategoryId` the list endpoint takes. */
export async function updateDenominations(
  client: BoClientLike,
  subCategoryId: unknown,
  currencyId: unknown,
  denominations: unknown[],
): Promise<Record<string, any>> {
  return client.post("denomination/update", {
    subCategoryId: pyStr(subCategoryId),
    currencyId: pyStr(currencyId),
    denominations,
  });
}

export interface CreateDenominationOptions {
  defaultBetLevel?: unknown;
  remarks?: unknown;
  verify?: boolean;
  listing?: unknown;
}

/** Create one denomination for a (sub-category, currency) pair and return its new id.
 *
 * Sends every existing denomination back unchanged plus the new one, which carries NO `id` — that
 * is what makes the back office create it. The id is then read back from a fresh list, because the
 * update response does not carry it.
 *
 * `listing` reuses a `getDenominationList()` result the caller already has, instead of fetching
 * the same list again. It must be the list for THIS pair — it is the one resubmitted, so a stale
 * or foreign one would write the wrong ladder back. */
export async function createDenomination(
  client: BoClientLike,
  subCategoryId: unknown,
  currencyId: unknown,
  name: unknown,
  betLevels: unknown,
  opts: CreateDenominationOptions = {},
): Promise<any> {
  const defaultBetLevel = opts.defaultBetLevel !== undefined ? opts.defaultBetLevel : 1;
  const remarks = opts.remarks !== undefined ? opts.remarks : "";
  const verify = opts.verify ?? true;
  const levels = validatedLevels(betLevels, defaultBetLevel);
  const listing = truthy(opts.listing) ? opts.listing : await getDenominationList(client, subCategoryId, currencyId);
  if (truthy(findDenomination(listing, { name }))) {
    throw new PyValueError(
      `A denomination named ${pyRepr(name)} already exists for `
      + `${pyStr(get(listing, "currency"))} ${pyStr(get(listing, "gameSubCategory"))} — names must be unique.`,
    );
  }
  const payload: Dict[] = listOf(listing, "denominations").map(toSubmitDenomination);
  payload.push({
    name,
    defaultBetLevel: pyInt(defaultBetLevel),
    remarks: or(remarks, ""),
    betLevels: levels,
  });
  logger.info(
    "Creating denomination %r for %s %s (levels %s, default level %s); resubmitting %d existing.",
    name, get(listing, "currency"), get(listing, "gameSubCategory"), levels, defaultBetLevel,
    payload.length - 1,
  );
  await updateDenominations(client, subCategoryId, currencyId, payload);
  if (!verify) return null;
  const created = findDenomination(await getDenominationList(client, subCategoryId, currencyId), { name });
  if (!truthy(created) || get(created, "id") === null) {
    throw new PyRuntimeError(
      `Created denomination ${pyRepr(name)} but could not read its id back from denomination/view/list.`,
    );
  }
  logger.info("Created denomination %r (id=%s).", name, item(created, "id"));
  return item(created, "id");
}

export interface UpdateDenominationOptions {
  name?: unknown;
  betLevels?: unknown;
  defaultBetLevel?: unknown;
  remarks?: unknown;
  isDefault?: unknown;
  verify?: boolean;
}

/** Edit one existing denomination, leaving every other one in the pair untouched.
 *
 * Only the arguments you pass are changed; the rest of the entry is resubmitted as the server
 * reports it. Returns the entry as it reads after the write (`verify: false` skips the read-back). */
export async function updateDenomination(
  client: BoClientLike,
  subCategoryId: unknown,
  currencyId: unknown,
  denominationId: unknown,
  opts: UpdateDenominationOptions = {},
): Promise<any> {
  const name = opts.name ?? null;
  const betLevels = opts.betLevels ?? null;
  const defaultBetLevel = opts.defaultBetLevel ?? null;
  const remarks = opts.remarks ?? null;
  const isDefault = opts.isDefault ?? null;
  const verify = opts.verify ?? true;
  const listing = await getDenominationList(client, subCategoryId, currencyId);
  const target = findDenomination(listing, { denominationId });
  if (!truthy(target)) {
    const available = listOf(listing, "denominations").map((d) => new PyTuple([get(d, "id"), get(d, "name")]));
    throw new PyValueError(
      `No denomination id ${pyStr(denominationId)} for ${pyStr(get(listing, "currency"))} `
      + `${pyStr(get(listing, "gameSubCategory"))}. Available (id, name): ${pyRepr(available)}`,
    );
  }
  let levels: unknown = betLevels === null ? get(target, "betLevels") : pyList(betLevels);
  const level = defaultBetLevel === null ? get(target, "defaultBetLevel") : defaultBetLevel;
  levels = validatedLevels(levels, level);

  const payload: Dict[] = [];
  for (const d of listOf(listing, "denominations")) {
    const entry = toSubmitDenomination(d);
    if (pyEq(get(d, "id"), denominationId)) {
      Object.assign(entry, {
        name: name === null ? get(target, "name") : name,
        betLevels: levels,
        defaultBetLevel: pyInt(level),
        remarks: remarks === null ? get(target, "remarks") : remarks,
        isDefault: isDefault === null ? get(target, "isDefault") : (truthy(isDefault) ? 1 : 0),
      });
    }
    payload.push(entry);
  }
  logger.info("Updating denomination %s (%r) for %s %s.", denominationId, get(target, "name"),
    get(listing, "currency"), get(listing, "gameSubCategory"));
  await updateDenominations(client, subCategoryId, currencyId, payload);
  if (!verify) return null;
  return findDenomination(await getDenominationList(client, subCategoryId, currencyId), { denominationId });
}

export interface EnsureAutomationOptions {
  betLevels?: unknown;
  defaultBetLevel?: unknown;
  prefix?: unknown;
  listing?: unknown;
}

/** Make sure this pair has the automation denomination, and return `[id, created]`.
 *
 * Idempotent, and deliberately so: an existing one is LEFT ALONE rather than rewritten, so a run
 * never disturbs a ladder another run (or a person) is already playing on. `created` says which
 * path was taken. */
export async function ensureAutomationDenomination(
  client: BoClientLike,
  subCategoryId: unknown,
  currencyId: unknown,
  opts: EnsureAutomationOptions = {},
): Promise<[any, boolean]> {
  const betLevels = opts.betLevels !== undefined ? opts.betLevels : AUTOMATION_BET_LEVELS;
  const defaultBetLevel = opts.defaultBetLevel !== undefined ? opts.defaultBetLevel : AUTOMATION_DEFAULT_LEVEL;
  const prefix = opts.prefix !== undefined ? opts.prefix : AUTOMATION_PREFIX;
  const listing = truthy(opts.listing) ? opts.listing : await getDenominationList(client, subCategoryId, currencyId);
  const name = automationDenominationName(listing, prefix);
  const existing = findDenomination(listing, { name });
  if (truthy(existing)) {
    logger.info("Denomination %r already exists (id=%s, levels %s).",
      name, get(existing, "id"), get(existing, "betLevels"));
    return [item(existing, "id"), false];
  }
  try {
    return [await createDenomination(client, subCategoryId, currencyId, name, betLevels,
      { defaultBetLevel, listing }), true];
  } catch (exc) {
    // ⚠ THE BACK OFFICE ENFORCES NAME UNIQUENESS MORE WIDELY THAN THIS LISTING SHOWS.
    // `denomination/view/list` is scoped to ONE (sub-category, currency) pair, so a name held
    // by a ladder outside that pair is invisible to the lookup above — and the create then
    // comes back DENOMINATION_NAME_ALREADY_EXISTS. A stale `listing` passed in by a caller
    // does the same thing. Either way the run used to die on an error that named no way out.
    //
    // So: re-read THIS pair fresh and look again. If the ladder is ours after all, use it —
    // that is the whole point of an idempotent ensure, and it is the common case (a ladder
    // created by an earlier run, or by hand under a different capitalisation).
    if (!excStr(exc).toUpperCase().includes("ALREADY_EXISTS")) throw exc;
    const fresh = await getDenominationList(client, subCategoryId, currencyId);
    const found = findDenomination(fresh, { name });
    if (truthy(found)) {
      logger.info("Denomination %r existed after all (id=%s, levels %s) -- the first listing "
        + "did not show it. Using it.", name, get(found, "id"), get(found, "betLevels"));
      return [item(found, "id"), false];
    }
    // Not in this pair, yet the name is taken. That is a name held elsewhere in the game
    // category, and no amount of retrying fixes it -- so say exactly what is here, which is
    // what a person needs to pick a free name or reuse the right ladder.
    throw new PyRuntimeError(
      `Cannot create the automation denomination ${pyRepr(name)} for `
      + `${pyStr(get(listing, "currency"))} ${pyStr(get(listing, "gameSubCategory"))}: the back office says the `
      + "NAME already exists, but no denomination with it is in this pair's list. Names are "
      + "unique across the game category, so it is held by another (sub-category, currency) "
      + `pair. This pair currently has: ${pyRepr(denominationNames(fresh))}. `
      + "Either point the run at one of those, or pass a different `prefix`.",
      { cause: exc },
    );
  }
}

// ---------------------------------------------------------------------------
// MASTER GAME — read and assign
// ---------------------------------------------------------------------------

function currencyFields(c: unknown): Dict {
  const out: Dict = {};
  for (const k of CURRENCY_FIELDS) out[k] = get(c, k);
  return out;
}

/** Every currency the master game offers, with the denomination assigned to it.
 *
 * `gameData` is the `data` object from `getGameDetail()`. Returns rows of
 * `{id, code, denominationId, denomName, minBet, maxBet, isDenomOverride, isDefault}` sorted by
 * currency code — `id` is the CURRENCY id (what `currencyInfoList` keys on), not the denomination id. */
export function listGameDenominations(gameData: unknown): Dict[] {
  const rows = listOf(gameData, "currencyInfoList").map(currencyFields);
  return sortedBy(rows, (r) => or(get(r, "code"), ""));
}

/** `gameSubCategory.id` — the pair key `denomination/view/list` needs (1 = 3x3, 2 = 3x5). */
export function gameSubCategoryId(gameData: unknown): any {
  const sub = or(get(gameData, "gameSubCategory"), {});
  if (get(sub, "id") === null) {
    throw new PyValueError(
      `Game ${pyRepr(get(gameData, "code"))} reports no gameSubCategory.id, so its denomination `
      + "list cannot be addressed.",
    );
  }
  return item(sub, "id");
}

/** One `currencyInfoList` entry, by currency code (e.g. "TKN") or by currency id.
 *
 * Raises ValueError listing what the game does offer — a typo'd currency must fail loudly rather
 * than silently assigning nothing. */
export function findGameCurrency(gameData: unknown, opts: { currencyCode?: unknown; currencyId?: unknown } = {}): any {
  const currencyCode = opts.currencyCode ?? null;
  const currencyId = opts.currencyId ?? null;
  if (currencyCode === null && currencyId === null) throw new PyValueError("pass currency_code or currency_id");
  for (const c of listOf(gameData, "currencyInfoList")) {
    if (currencyCode !== null && pyEq(get(c, "code"), currencyCode)) return c;
    if (currencyId !== null && pyEq(get(c, "id"), currencyId)) return c;
  }
  const available = sortedBy(
    listOf(gameData, "currencyInfoList").map((c) => [get(c, "code"), get(c, "id")]),
    (t) => t,
  ).map((t) => new PyTuple(t));
  throw new PyValueError(
    `Currency not found on game ${pyRepr(get(gameData, "code"))} `
    + `(code=${pyRepr(currencyCode)}, id=${pyStr(currencyId)}). Available (code, id): ${pyRepr(available)}`,
  );
}

/** Point one currency of a built `game/update/v2` payload at `denominationId`.
 *
 * `payload` comes from `buildUpdatePayload()`, whose `currencyInfoList` is already reduced to
 * `{id, denominationId, isDenomOverride}` — every other currency is passed through untouched, so
 * one assignment cannot disturb the other ~90 currencies on the game. */
export function setCurrencyDenomination(
  payload: Dict,
  currencyId: unknown,
  denominationId: unknown,
  isDenomOverride: unknown = null,
): Dict {
  for (const entry of listOf(payload, "currencyInfoList")) {
    if (pyEq(get(entry, "id"), currencyId)) {
      entry.denominationId = denominationId;
      if (isDenomOverride !== null && isDenomOverride !== undefined) {
        entry.isDenomOverride = truthy(isDenomOverride) ? STATUS_ACTIVE : STATUS_INACTIVE;
      }
      return payload;
    }
  }
  const available = sortedBy(listOf(payload, "currencyInfoList").map((e) => get(e, "id")));
  throw new PyValueError(`Currency id ${pyStr(currencyId)} not in this payload. Available ids: ${pyRepr(available)}`);
}

export interface AssignGameDenominationOptions {
  currencyCode?: unknown;
  currencyId?: unknown;
  isDenomOverride?: unknown;
  verify?: boolean;
}

/** Assign `denominationId` to one currency of the MASTER game. Every agent that does not override
 * it inherits the change.
 *
 * Reads the game, transforms it into the update body the same way a paytable edit does, patches
 * exactly one `currencyInfoList` entry, and submits. With `verify` (default) it re-reads
 * `game/view` and returns the currency row as the server now reports it — so the caller sees the
 * landed value, including `denomName`, rather than assuming the write took. */
export async function assignGameDenomination(
  client: BoClientLike,
  gameId: unknown,
  sasEntityId: unknown,
  denominationId: unknown,
  opts: AssignGameDenominationOptions = {},
): Promise<Dict | null> {
  const verify = opts.verify ?? true;
  const game = await getGameDetail(client, gameId, sasEntityId);
  const currency = findGameCurrency(game, { currencyCode: opts.currencyCode, currencyId: opts.currencyId });
  const payload = buildUpdatePayload(game);
  setCurrencyDenomination(payload, item(currency, "id"), denominationId, opts.isDenomOverride ?? null);
  logger.info(
    "Master game %s (%s): currency %s (id=%s) denomination %s -> %s",
    gameId, get(game, "code"), get(currency, "code"), item(currency, "id"),
    get(currency, "denominationId"), denominationId,
  );
  await updateGame(client, payload);
  if (!verify) return null;
  const after = findGameCurrency(await getGameDetail(client, gameId, sasEntityId), { currencyId: item(currency, "id") });
  if (!pyEq(get(after, "denominationId"), denominationId)) {
    throw new PyRuntimeError(
      `Assigned denomination ${pyStr(denominationId)} to ${pyStr(get(currency, "code"))} on game `
      + `${pyStr(gameId)}, but game/view still reports ${pyStr(get(after, "denominationId"))}.`,
    );
  }
  return currencyFields(after);
}

// ---------------------------------------------------------------------------
// AGENT GAME — read and assign
// ---------------------------------------------------------------------------

/** What this agent's game row currently uses: `{id, denominationId, isDenomOverride, ...}`.
 *
 * `id` here is the GAME SETTING row id — the one `agentgamecurrency/update` submits against. */
export async function getAgentDenomination(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
): Promise<Dict> {
  const data = await getAgentGameSettings(client, agentId, currencyId, { gameCode: gameCode as string });
  const row = findGameRow(data, { gameCode });
  return {
    id: get(row, "id"),
    gameCode: get(row, "gameCode"),
    denominationId: get(row, "denominationId"),
    isDenomOverride: get(row, "isDenomOverride"),
    payTableId: get(row, "payTableId"),
  };
}

/** Assign `denominationId` to ONE agent's game row, and mark it as an override.
 *
 * Everything else on the row — the paytable it is on, its max-payout override, rtp level, status —
 * is preserved, exactly as `setAgentPaytable` preserves the denomination when it changes the
 * paytable. `override: false` writes the id while leaving the row following the master default,
 * which is rarely what you want. */
export async function assignAgentDenomination(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
  denominationId: unknown,
  opts: { override?: unknown; autoFinalization?: Dict | null } = {},
): Promise<Record<string, any>> {
  const override = opts.override !== undefined ? opts.override : true;
  const data = await getAgentGameSettings(client, agentId, currencyId, { gameCode: gameCode as string });
  const row = findGameRow(data, { gameCode });
  const submitRow = buildSubmitRow(
    row,
    findCurrentPaytable(row),          // keep the paytable the row is already on
    get(data, "maxPayout"),
    { denominationId, isDenomOverride: truthy(override) ? DENOM_OVERRIDDEN : DENOM_FOLLOWING_DEFAULT },
  );
  const af = truthy(opts.autoFinalization) ? (opts.autoFinalization as Dict) : autoFinalizationFrom(data);
  logger.info(
    "Agent %s game %s (row %s): denomination %s -> %s (isDenomOverride=%s)",
    agentId, gameCode, submitRow.id, get(row, "denominationId"),
    denominationId, submitRow.isDenomOverride,
  );
  return updateAgentGameSettings(client, agentId, [submitRow], af);
}

/** TEARDOWN: put the agent's game row back to following the master game's denomination.
 *
 * Clears `isDenomOverride`. `denominationId` optionally also rewrites the id — pass the master
 * game's value for this currency (`findGameCurrency(...).denominationId`) when you want the row to
 * read exactly as it did before the test.
 *
 * ⚠ MEASURED on dev 2026-09-18: BO REQUIRES the id sent with `isDenomOverride: 0` to be the
 * MASTER game's denomination for this currency. Clearing the override while the row still points at
 * another ladder (say the automation one) is rejected with `INVALID_UPDATE_REQUEST`, and the row is
 * left overridden. Pass `denominationId: findGameCurrency(...).denominationId`; passing null keeps
 * whatever the row has, which fails for the same reason whenever that is not the master's. */
export async function resetAgentDenominationToDefault(
  client: BoClientLike,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
  opts: { denominationId?: unknown; autoFinalization?: Dict | null } = {},
): Promise<Record<string, any>> {
  const data = await getAgentGameSettings(client, agentId, currencyId, { gameCode: gameCode as string });
  const row = findGameRow(data, { gameCode });
  const submitRow = buildSubmitRow(
    row,
    findCurrentPaytable(row),
    get(data, "maxPayout"),
    { denominationId: opts.denominationId ?? null, isDenomOverride: DENOM_FOLLOWING_DEFAULT },
  );
  const af = truthy(opts.autoFinalization) ? (opts.autoFinalization as Dict) : autoFinalizationFrom(data);
  logger.info(
    "Agent %s game %s (row %s): denomination override cleared (id now %s)",
    agentId, gameCode, submitRow.id, submitRow.denominationId,
  );
  return updateAgentGameSettings(client, agentId, [submitRow], af);
}

// ---------------------------------------------------------------------------
// The whole flow, in one call
// ---------------------------------------------------------------------------

export interface EnsureAgentAutomationOptions {
  betLevels?: unknown;
  defaultBetLevel?: unknown;
  prefix?: unknown;
  autoFinalization?: Dict | null;
}

/** SETUP: put this agent's game on a known bet ladder, creating it if the pair has none.
 *
 *     game/view              -> the game's sub-category (the denomination pair key)
 *     denomination/view/list -> does "automation test <CUR> <SUBCAT>" exist?
 *     denomination/update    -> ...if not, create it (resubmitting every existing entry)
 *     denomination/view/list -> read the new id back
 *     agentgamecurrency/*    -> assign it to the agent's row with isDenomOverride = 1
 *
 * Returns `{denomination_id, name, created, sub_category_id, previous_denomination_id,
 * previous_is_denom_override, master_denomination_id, bet_levels, default_bet_level, default_bet}`.
 *
 * ⚠ A TEARDOWN NEEDS ALL THREE OF THOSE, not just `previous_denomination_id`. Measured on dev
 * 2026-09-18: **BO refuses to clear `isDenomOverride` unless the id sent with it is the MASTER
 * game's denomination** — `denominationId: <automation> + isDenomOverride: 0` comes back
 * `INVALID_UPDATE_REQUEST`. So the restore is:
 *
 *   * previous override ON, and its id is not this automation ladder ->
 *     `assignAgentDenomination(..., previous_denomination_id)` (keeps the override);
 *   * otherwise -> `resetAgentDenominationToDefault(..., { denominationId: master_denomination_id })`.
 *
 * The second branch is also what rescues a row a FAILED teardown left on the automation ladder:
 * `previous_denomination_id` then reads as the automation id itself, and restoring that would be
 * both rejected and wrong.
 *
 * `bet_levels` / `default_bet_level` are read back from BO rather than assumed from the arguments:
 * an EXISTING automation ladder is left alone (see `ensureAutomationDenomination`), so it may not
 * be the one this call would have created. `default_bet` resolves the 1-based `default_bet_level`
 * to the bet value a game should open at on this ladder. */
export async function ensureAgentAutomationDenomination(
  client: BoClientLike,
  gameId: unknown,
  sasEntityId: unknown,
  agentId: unknown,
  currencyId: unknown,
  gameCode: unknown,
  opts: EnsureAgentAutomationOptions = {},
): Promise<Dict> {
  const betLevels = opts.betLevels !== undefined ? opts.betLevels : AUTOMATION_BET_LEVELS;
  const defaultBetLevel = opts.defaultBetLevel !== undefined ? opts.defaultBetLevel : AUTOMATION_DEFAULT_LEVEL;
  const prefix = opts.prefix !== undefined ? opts.prefix : AUTOMATION_PREFIX;
  const game = await getGameDetail(client, gameId, sasEntityId);
  const subCategoryId = gameSubCategoryId(game);
  const listing = await getDenominationList(client, subCategoryId, currencyId);
  const name = automationDenominationName(listing, prefix);
  const [denominationId, created] = await ensureAutomationDenomination(client, subCategoryId, currencyId, {
    betLevels, defaultBetLevel, prefix,
    listing,                      // already fetched above; do not read it again
  });
  const before = await getAgentDenomination(client, agentId, currencyId, gameCode);
  await assignAgentDenomination(client, agentId, currencyId, gameCode, denominationId,
    { autoFinalization: opts.autoFinalization ?? null });
  logger.info(
    "Agent %s game %s now on denomination %r (id=%s, %s); it was on %s.",
    agentId, gameCode, name, denominationId, created ? "created" : "existing",
    get(before, "denominationId"),
  );
  // Read the ladder back: after a create the listing above is stale, and an existing ladder may
  // differ from the defaults this call was given.
  const entry = or(findDenomination(await getDenominationList(client, subCategoryId, currencyId),
    { denominationId }), {});
  const levels: any[] = truthy(get(entry, "betLevels")) ? pyList(get(entry, "betLevels")) : [];
  const level = get(entry, "defaultBetLevel");
  const defaultBet = levels.length && level !== null && pyInt(level) >= 1 && pyInt(level) <= levels.length
    ? levels[pyInt(level) - 1] : null;
  const masterCurrency = or(findGameCurrency(game, { currencyId }), {});
  return {
    denomination_id: denominationId,
    name,
    created,
    sub_category_id: subCategoryId,
    previous_denomination_id: get(before, "denominationId"),
    previous_is_denom_override: get(before, "isDenomOverride"),
    // The only id BO accepts alongside `isDenomOverride: 0` (see the docstring).
    master_denomination_id: get(masterCurrency, "denominationId"),
    bet_levels: levels,
    default_bet_level: level,
    default_bet: defaultBet,
  };
}
