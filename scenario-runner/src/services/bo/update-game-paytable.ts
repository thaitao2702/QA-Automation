/**
 * Update a single paytable on a game in the Royaledge backoffice (port of
 * framework/bo/update_game_paytable.py).
 *
 * Mirrors the frontend flow (src/pages/GameVendor/Game/GameUpdate.tsx):
 *   POST game/view        -> get full game detail
 *   (transform)           -> shape into game/update/v2 body
 *   (modify one paytable) -> replace payTable JSON for the chosen `code`
 *   POST game/update/v2   -> submit
 *
 * Transform faithfully reproduces:
 *   - response->form (useEffect)          GameUpdate.tsx:152-222
 *   - form->submit (handleOnBeforeSubmit) GameUpdate.tsx:73-108
 *
 * Auth + automatic token refresh is delegated to BoApiClient (api-client.ts), which retries on
 * messageCode == "TOKEN_EXPIRED".
 */
import { type Dict, get, or, pyEq, pyInt, pyList, pyLt, pyRepr, PyRuntimeError, pyStr, PyValueError, truthy } from "../../py.js";
import type { BoClientLike } from "./api-client.js";
import { getOr, item, jsonDumps } from "./compat.js";
import { getLogger } from "./log.js";

const logger = getLogger("framework.bo.update_game_paytable");

export const STATUS_ACTIVE = 1; // STATUS.ACTIVE
export const STATUS_INACTIVE = 0; // STATUS.INACTIVE

/** POST game/view -> returns the `data` object (GameDetailType). */
export async function getGameDetail(client: BoClientLike, gameId: unknown, sasEntityId: unknown): Promise<any> {
  const body = await client.post("game/view", { gameId: gameId ?? null, sasEntityId: sasEntityId ?? null });
  return item(body, "data");
}

/** Return the payTableInfoList item whose name (`code`) matches, else null. */
export function findGamePaytable(gameData: unknown, code: unknown): any {
  for (const p of truthy(get(gameData, "payTableInfoList")) ? pyList(get(gameData, "payTableInfoList")) : []) {
    if (pyEq(get(p, "code"), code)) return p;
  }
  return null;
}

/** `sorted(xs, key=k)`: stable, ordered by Python's `<` (which raises on None / mixed kinds).
 *
 * The sort engine compares the element being placed against the one already placed — `a` below,
 * like CPython's `pivot < p` — and `a < b` is asked first, so a TypeError names the two kinds in the
 * order Python's message does. */
export function sortedBy<T>(xs: readonly T[], key: (x: T) => unknown = (x) => x): T[] {
  return [...xs].sort((a, b) => {
    const ka = key(a);
    const kb = key(b);
    if (pyLt(ka, kb)) return -1;
    if (pyLt(kb, ka)) return 1;
    return 0;
  });
}

/**
 * Transform a game/view `data` object into a game/update/v2 request body.
 * Replicates the two frontend transforms collapsed into one.
 */
export function buildUpdatePayload(gameData: any): Dict {
  const listOf = (k: string): any[] => (truthy(get(gameData, k)) ? pyList(get(gameData, k)) : []);
  // currencyInfoList -> only {id, denominationId, isDenomOverride}, sorted by id
  const currencyInfoList = sortedBy(
    listOf("currencyInfoList").map((c) => ({
      id: get(c, "id"),
      denominationId: get(c, "denominationId"),
      isDenomOverride: get(c, "isDenomOverride"),
    })),
    (c) => c.id,
  );

  // payTableInfoList -> {code, id, isDefault, payTable, rtp(scalar), status: ACTIVE}
  const payTableInfoList = listOf("payTableInfoList").map((p) => ({
    code: get(p, "code"),
    id: get(p, "id"),
    isDefault: get(p, "isDefault"),
    payTable: get(p, "payTable"), // JSON string, unchanged
    rtp: get(p, "rtp"), // HIGH | MEDIUM | LOW (already scalar)
    status: STATUS_ACTIVE,
  }));

  const rtp = get(gameData, "rtp");

  // Start from the full game object (frontend spreads `...gameData`), then
  // apply the renamed/added fields and the submit-time overrides.
  const payload: Dict = { ...gameData };
  Object.assign(payload, {
    id: pyInt(item(gameData, "id")),
    // renamed fields the form adds (form->submit keeps them via spread):
    categoryId: get(or(get(gameData, "gameCategory"), {}), "id"),
    subCategoryId: get(or(get(gameData, "gameSubCategory"), {}), "id"),
    maximumPayout: get(gameData, "maxPayout"),
    imageSquare: get(or(get(gameData, "assets"), {}), "thumbnailSquare"),
    imageLandScape: get(or(get(gameData, "assets"), {}), "thumbnailLandscape"),
    features: get(gameData, "feature"),
    themes: get(gameData, "theme"),
    labels: get(gameData, "labels"),
    tags: get(gameData, "tags"),
    languages: get(gameData, "languages"),
    hitrate: get(gameData, "hitrate") !== null ? pyStr(item(gameData, "hitrate")) : null,
    status: pyEq(get(gameData, "status"), STATUS_ACTIVE) ? STATUS_ACTIVE : 0,
    // submit-time overrides:
    rtp: truthy(rtp) ? pyInt(rtp) : null, // JS: rtp ? Number(rtp) : undefined
    currencyInfoList,
    payTableInfoList,
    // winFormula is round-tripped through a parser in the UI; the
    // original string is equivalent, so pass it through unchanged.
    winFormula: get(gameData, "winFormula"),
  });
  // transient UI-only keys the frontend deletes (no-ops if absent)
  for (const k of ["currencyIds", "currency", "rtpRange", "gameId", "currencies", "currencyStatus"]) {
    delete payload[k];
  }
  return payload;
}

/** Accept a dict/list (JSON-encode it) or an already JSON-encoded string. */
export function asPaytableString(value: unknown): string {
  if (typeof value === "string") return value;
  return jsonDumps(value, { separators: [",", ":"] });
}

/**
 * Replace the `payTable` content of exactly one paytable, matched by `code`. `newPaytable` may be a
 * dict/list (will be JSON-encoded) or an already JSON-encoded string.
 */
export function setPaytable(payload: Dict, code: unknown, newPaytable: unknown): Dict {
  const newValue = asPaytableString(newPaytable);
  let matched = false;
  for (const entry of pyList(getOr(payload, "payTableInfoList", []))) {
    if (pyEq(get(entry, "code"), code)) {
      (entry as Dict).payTable = newValue;
      matched = true;
      break;
    }
  }
  if (!matched) {
    const available = pyList(getOr(payload, "payTableInfoList", [])).map((p) => get(p, "code"));
    throw new PyValueError(`No paytable with code=${pyRepr(code)}. Available: ${pyRepr(available)}`);
  }
  return payload;
}

/** POST game/update/v2 with the transformed body. */
export async function updateGame(client: BoClientLike, payload: Dict): Promise<Record<string, any>> {
  return client.post("game/update/v2", payload);
}

/**
 * Make sure a paytable named `code` (e.g. "AutomationTest") exists on the game, and return its
 * server-side id (payTableId).
 *
 * Flow:
 *   1. GET game/view by game id.
 *   2. If payTableInfoList already has a paytable with name == `code`, return its id.
 *   3. Otherwise create it (append a new payTableInfoList item with no id and POST game/update/v2),
 *      then GET game/view again to read back the server-assigned id.
 *
 * `paytableContent` is the payTable JSON for a newly created paytable (dict/list or JSON string).
 * If omitted, it is cloned from the game's default paytable (or the first existing one) so the new
 * paytable is valid.
 */
export async function ensureGamePaytable(
  client: BoClientLike,
  gameId: unknown,
  sasEntityId: unknown,
  code: unknown,
  paytableContent: unknown = null,
  rtp: unknown = "MEDIUM",
): Promise<any> {
  let game = await getGameDetail(client, gameId, sasEntityId);

  const existing = findGamePaytable(game, code);
  if (truthy(existing)) {
    logger.info("Paytable %r already exists (id=%s).", code, get(existing, "id"));
    return get(existing, "id");
  }

  // --- create a new paytable -------------------------------------------
  const existingList: any[] = truthy(get(game, "payTableInfoList")) ? pyList(get(game, "payTableInfoList")) : [];
  let content: unknown = paytableContent ?? null;
  if (content === null) {
    if (!existingList.length) {
      throw new PyValueError(
        `Cannot create paytable ${pyRepr(code)}: no existing paytable to clone `
        + "content from, and no paytable_content provided.",
      );
    }
    const source = existingList.find((p) => pyEq(get(p, "isDefault"), STATUS_ACTIVE)) ?? existingList[0];
    content = get(source, "payTable");
  }

  const payload = buildUpdatePayload(game);
  (payload.payTableInfoList as Dict[]).push({
    code,
    payTable: asPaytableString(content),
    rtp,
    isDefault: STATUS_INACTIVE, // new paytable is not the game default
    status: STATUS_ACTIVE,
    // no `id` -> backend creates a new paytable
  });
  logger.info("Creating paytable %r on game %s.", code, gameId);
  await updateGame(client, payload);

  // re-fetch to read back the server-assigned id
  game = await getGameDetail(client, gameId, sasEntityId);
  const created = findGamePaytable(game, code);
  if (!truthy(created) || get(created, "id") === null) {
    throw new PyRuntimeError(`Created paytable ${pyRepr(code)} but could not read its id back from game/view.`);
  }
  logger.info("Created paytable %r (id=%s).", code, get(created, "id"));
  return get(created, "id");
}

// ---- High-level one-shot ----

/**
 * Get game -> transform to update shape -> change one paytable -> submit. Token refresh is handled
 * transparently by the client. Returns the game/update/v2 response.
 */
export async function updateGamePaytable(
  client: BoClientLike,
  gameId: unknown,
  sasEntityId: unknown,
  paytableCode: unknown,
  newPaytable: unknown,
): Promise<Record<string, any>> {
  const gameData = await getGameDetail(client, gameId, sasEntityId);
  const payload = buildUpdatePayload(gameData);
  setPaytable(payload, paytableCode, newPaytable);
  return updateGame(client, payload);
}
