/**
 * Back-office (BO) API integration (port of framework/bo/__init__.py), used by the runner via
 * `BoHelper` (helper.ts):
 *
 * * `api-client`           — transport + automatic token refresh (`BoApiClient`).
 * * `agent-game-settings`  — per-agent paytable selection (set / reset / inspect).
 * * `update-game-paytable` — master-game paytable content (ensure / edit).
 * * `denominations`        — which denomination a game uses per currency: read, and assign on the
 *   master game or on one agent's row.
 * * `setup`                — the runner's glue: `boSetup`, `boReset`, `readBaseBet`, `maxWinNeedsBet`.
 *
 * Usage and the end-to-end deterministic-result flow are documented in docs/bo-integration.md.
 */
export { ApiError, AuthError, BoApiClient, HTTPError, jwtExpiry } from "./api-client.js";
export {
  agentPaytable,
  buildSubmitRow,
  findDefaultPaytable,
  findGameRow,
  findPaytableByCode,
  getAgentCurrencies,
  getAgentGameSettings,
  iterGameRows,
  resetAgentPaytableToDefault,
  setAgentPaytable,
  updateAgentGameSettings,
} from "./agent-game-settings.js";
export {
  assignAgentDenomination,
  assignGameDenomination,
  automationDenominationName,
  createDenomination,
  ensureAgentAutomationDenomination,
  ensureAutomationDenomination,
  findDenomination,
  findGameCurrency,
  gameSubCategoryId,
  getAgentDenomination,
  getDenominationList,
  listGameDenominations,
  resetAgentDenominationToDefault,
  setCurrencyDenomination,
  toSubmitDenomination,
  updateDenomination,
  updateDenominations,
} from "./denominations.js";
export {
  buildUpdatePayload,
  ensureGamePaytable,
  findGamePaytable,
  getGameDetail,
  setPaytable,
  updateGame,
  updateGamePaytable,
} from "./update-game-paytable.js";
export { BoConfigError, BoHelper } from "./helper.js";
export { BASE_BET_JS, BO_SETTLE_S, boReset, boSetup, maxWinNeedsBet, readBaseBet, resolveMaxPayout } from "./setup.js";
