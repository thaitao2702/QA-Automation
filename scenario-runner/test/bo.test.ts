/**
 * The BO layer (src/services/bo/) replayed against what the Python answered
 * (baseline/make_bo_baseline.py -> baseline/bo.json): JWT expiry, the pure transforms, max-win
 * resolution, the runner glue, every HTTP flow (exact URL, headers, body text and timeout per
 * request, with `fetch` stubbed), BoHelper config / token cache / resolvePaytable, and the payTable
 * string each real fixture becomes.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PyTuple, PyValueError, isDict } from "../src/py.js";
import {
  agentPaytable, autoFinalizationFrom, buildSubmitRow, findCurrentPaytable, findDefaultPaytable, findGameRow,
  findPaytableByCode, getAgentCurrencies, getAgentGameSettings, isNan, iterGameRows, num, resetAgentPaytableToDefault,
  setAgentPaytable, updateAgentGameSettings, DEFAULT_AUTO_FINALIZATION,
} from "../src/services/bo/agent-game-settings.js";
import {
  API_BASE, ApiError, AuthError, BoApiClient, jwtExpiry, REFRESH_SKEW_S, SUCCESS_STATUSES, TOKEN_EXPIRED, UNAUTHORIZED,
} from "../src/services/bo/api-client.js";
import { pyJsonLoads } from "../src/services/bo/compat.js";
import {
  assignAgentDenomination, assignGameDenomination, automationDenominationName, AUTOMATION_BET_LEVELS,
  AUTOMATION_DEFAULT_LEVEL, AUTOMATION_PREFIX, createDenomination, DENOM_FOLLOWING_DEFAULT, DENOM_OVERRIDDEN,
  denominationNames, ensureAgentAutomationDenomination, ensureAutomationDenomination, findDenomination,
  findGameCurrency, gameSubCategoryId, getAgentDenomination, getDenominationList, listGameDenominations, normName,
  resetAgentDenominationToDefault, setCurrencyDenomination, toSubmitDenomination, updateDenomination,
  updateDenominations, validatedLevels,
} from "../src/services/bo/denominations.js";
import { BoHelper } from "../src/services/bo/helper.js";
import { addLogSink, type LogRecord } from "../src/services/bo/log.js";
import {
  BASE_BET_JS, BO_SETTLE_S, boReset, boSetup, type BoForSetup, maxWinNeedsBet, readBaseBet, resolveMaxPayout,
} from "../src/services/bo/setup.js";
import {
  asPaytableString, buildUpdatePayload, ensureGamePaytable, findGamePaytable, getGameDetail, setPaytable, updateGame,
  updateGamePaytable,
} from "../src/services/bo/update-game-paytable.js";
import { Diff, loadBaseline, plain, REPO } from "./baseline.js";

const B = loadBaseline<any>("bo.json");

// ── recording harness (the TS twin of the generator's monkeypatches) ──────────────────

const REASONS: Record<number, string> = { 200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error", 502: "Bad Gateway" };
const BO_ENV_KEYS = ["BO_TOKEN", "BO_REFRESH_TOKEN", "BO_API_BASE", "BO_GAME_ID", "BO_SAS_ENTITY_ID",
  "BO_AGENT_ID", "BO_CURRENCY_ID", "BO_GAME_CODE", "BO_REFERENCE_BET", "BO_PAYTABLE_CODE"];

const state = {
  clock: B.now as number,
  trace: 0,
  queue: [] as Array<[number, string]>,
  requests: [] as Array<Record<string, unknown>>,
  logs: [] as Array<[string, string, string]>,
  lastTimeoutMs: null as number | null,
  printed: [] as string[],
};

function reset(now: number = B.now, responses: Array<[number, string]> = []): void {
  state.clock = now;
  state.trace = 0;
  state.queue = responses.map((r) => [r[0], r[1]]);
  state.requests = [];
  state.logs = [];
  state.printed = [];
}

let removeSink: () => void = () => {};
const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  vi.spyOn(Date, "now").mockImplementation(() => state.clock * 1000);
  vi.spyOn(globalThis.crypto, "randomUUID").mockImplementation(() => {
    state.trace += 1;
    return `00000000-0000-4000-8000-${String(state.trace).padStart(12, "0")}` as any;
  });
  vi.spyOn(AbortSignal, "timeout").mockImplementation((ms: number) => {
    state.lastTimeoutMs = ms;
    return new AbortController().signal;
  });
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    state.requests.push({
      url,
      headers: { ...(init.headers as Record<string, string>) },
      body: init.body,
      timeout: state.lastTimeoutMs === null ? null : state.lastTimeoutMs / 1000,
    });
    const next = state.queue.shift();
    if (!next) throw new Error(`NoCannedResponse ${url}`);
    return new Response(next[1], { status: next[0], statusText: REASONS[next[0]] });
  });
  vi.spyOn(console, "log").mockImplementation((...args: unknown[]) => {
    state.printed.push(...args.map(String).join(" ").split("\n"));
  });
  vi.spyOn(console, "error").mockImplementation(() => {});
  removeSink = addLogSink((rec: LogRecord) => state.logs.push([rec.levelname, rec.name, rec.message]));
  for (const k of BO_ENV_KEYS) savedEnv[k] = process.env[k];
});

afterEach(() => {
  removeSink();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const k of BO_ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
});

/** `{"__float__": "nan"}` back to NaN — the generator tagged non-finite inputs that way. */
function revive(v: any): any {
  if (Array.isArray(v)) return v.map(revive);
  if (isDict(v)) {
    const keys = Object.keys(v);
    if (keys.length === 1 && keys[0] === "__float__") {
      const s = v.__float__;
      return s === "nan" ? NaN : s === "inf" ? Infinity : -Infinity;
    }
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v)) out[k] = revive(x);
    return out;
  }
  return v;
}

/** `plain()` plus Python tuples as lists. */
function plainBo(v: unknown): unknown {
  if (v instanceof PyTuple) return v.items.map(plainBo);
  if (Array.isArray(v)) return v.map(plainBo);
  if (isDict(v)) {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v)) out[k] = plainBo(x);
    return out;
  }
  return plain(v);
}

/** Error text that is CPython's / PyYAML's own wording, which the port does not reproduce. */
function normMsg(s: unknown): unknown {
  if (typeof s !== "string") return s;
  return s
    .replace(/(is not valid JSON: )[\s\S]*$/, "$1<json error>")
    .replace(/(Ignoring unreadable BO token cache [^\n]*?\.json: )[\s\S]*$/, "$1<json error>")
    .replace(/(Failed to parse game config [^\n]*?\.yml: )[\s\S]*$/, "$1<yaml error>");
}

function outcomeOf(fn: () => unknown): Promise<Record<string, unknown>> {
  return Promise.resolve()
    .then(fn)
    .then((v) => ({ ok: plainBo(v) }), (e: Error) => {
      if (String(e?.message ?? "").startsWith("NoCannedResponse")) throw e;
      const rec: Record<string, unknown> = { err: e?.name ?? "Error", msg: normMsg(e?.message ?? String(e)) };
      if (rec.err === "JSONDecodeError") rec.msg = "<json error>";   // JavaScript's parser wording
      return rec;
    });
}

function pyOutcome(r: any): unknown {
  if ("err" in r) return { err: r.err, msg: r.err === "JSONDecodeError" ? "<json error>" : normMsg(r.msg) };
  return r;
}

const normLogs = (logs: Array<[string, string, string]>) => logs.map(([l, n, m]) => [l, n, normMsg(m)]);

/** An object (at any depth) with an integer-like key after a later-sorting one — the one shape a JS
 * object cannot keep in Python's order. */
function INTEGER_KEY_GAP(v: unknown): boolean {
  if (Array.isArray(v)) return v.some(INTEGER_KEY_GAP);
  if (!isDict(v)) return false;
  const keys = Object.keys(v);
  return keys.some((k) => /^(0|[1-9]\d*)$/.test(k)) || Object.values(v).some(INTEGER_KEY_GAP);
}

// ── constants ────────────────────────────────────────────────────────────────────────

describe("BO constants", () => {
  it("match the Python module constants", () => {
    const c = B.constants;
    expect({
      API_BASE, TOKEN_EXPIRED, UNAUTHORIZED, SUCCESS_STATUSES: [...SUCCESS_STATUSES], REFRESH_SKEW_S,
      DEFAULT_AUTO_FINALIZATION, AUTOMATION_PREFIX, AUTOMATION_BET_LEVELS: [...AUTOMATION_BET_LEVELS],
      AUTOMATION_DEFAULT_LEVEL, DENOM_OVERRIDDEN, DENOM_FOLLOWING_DEFAULT,
    }).toEqual(c);
    expect(BASE_BET_JS).toBe(B.glue.BASE_BET_JS);
    expect(BO_SETTLE_S).toBe(B.glue.BO_SETTLE_S);
  });
});

// ── JWT expiry ───────────────────────────────────────────────────────────────────────

describe("JWT expiry (BoHelper._jwt_expiry / BoApiClient._token_expiry)", () => {
  it(`matches ${B.jwt.length} tokens`, () => {
    const d = new Diff();
    for (const c of B.jwt) {
      d.expect(`${c.name} helper`, { ok: plain(jwtExpiry(c.token)) }, c.result);
      d.expect(`${c.name} client`, { ok: plain(new BoApiClient(c.token, "r").tokenExpiry()) }, c.client_result);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

// ── pure transforms ──────────────────────────────────────────────────────────────────

const PURE: Record<string, (a: any[], k: any) => unknown> = {
  "ags._num": (a) => num(a[0]),
  "ags._is_nan": (a) => isNan(a[0]),
  "ags.iter_game_rows": (a) => iterGameRows(a[0]),
  "ags.find_game_row": (a, k) => findGameRow(a[0], { gameCode: k.game_code, gameSettingId: k.game_setting_id }),
  "ags.find_paytable_by_code": (a) => findPaytableByCode(a[0], a[1]),
  "ags.find_default_paytable": (a) => findDefaultPaytable(a[0], a.length > 1 ? a[1] : null),
  "ags.find_current_paytable": (a) => findCurrentPaytable(a[0]),
  "ags.build_submit_row": (a, k) => buildSubmitRow(a[0], a[1], a[2], {
    maxPayout: a.length > 3 ? a[3] : k.max_payout, denominationId: k.denomination_id, isDenomOverride: k.is_denom_override,
  }),
  "ags._auto_finalization_from": (a) => autoFinalizationFrom(a[0]),
  "ugp.find_game_paytable": (a) => findGamePaytable(a[0], a[1]),
  "ugp.build_update_payload": (a) => buildUpdatePayload(a[0]),
  "ugp.set_paytable": (a) => setPaytable(a[0], a[1], a[2]),
  "ugp._as_paytable_string": (a) => asPaytableString(a[0]),
  "den._norm_name": (a) => normName(a[0]),
  "den.find_denomination": (a, k) => findDenomination(a[0], { name: k.name, denominationId: k.denomination_id }),
  "den.denomination_names": (a) => denominationNames(a[0]),
  "den.automation_denomination_name": (a) => (a.length > 1 ? automationDenominationName(a[0], a[1]) : automationDenominationName(a[0])),
  "den.to_submit_denomination": (a) => toSubmitDenomination(a[0]),
  "den._validated_levels": (a) => validatedLevels(a[0], a[1]),
  "den.list_game_denominations": (a) => listGameDenominations(a[0]),
  "den.game_sub_category_id": (a) => gameSubCategoryId(a[0]),
  "den.find_game_currency": (a, k) => findGameCurrency(a[0], { currencyCode: k.currency_code, currencyId: k.currency_id }),
  "den.set_currency_denomination": (a) => setCurrencyDenomination(a[0], a[1], a[2], a.length > 3 ? a[3] : null),
  "rs.max_win_needs_bet": (a) => maxWinNeedsBet(a[0]),
};

describe("pure transforms (agent_game_settings / update_game_paytable / denominations)", () => {
  it(`match ${B.pure.length} calls`, async () => {
    const d = new Diff();
    for (const [i, c] of B.pure.entries()) {
      const fn = PURE[c.fn];
      expect(fn, c.fn).toBeTypeOf("function");
      const got = await outcomeOf(() => fn(revive(c.args), revive(c.kwargs)));
      const where = `#${i} ${c.fn}(${JSON.stringify(c.args).slice(0, 120)} ${JSON.stringify(c.kwargs)})`;
      if (c.fn === "ugp._as_paytable_string" && INTEGER_KEY_GAP(c.args[0])) {
        // KNOWN GAP: a JS object lists integer-like keys first, ascending; Python keeps insertion
        // order. Same JSON object, different key order in the string.
        d.expect(`${where} (same JSON)`, JSON.parse((got as any).ok), JSON.parse(c.result.ok));
        d.expect(`${where} (order differs)`, (got as any).ok === c.result.ok, false);
        continue;
      }
      d.expect(where, got, pyOutcome(c.result));
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

// ── max-win resolution ───────────────────────────────────────────────────────────────

describe("_resolve_max_payout (max_payout / max_win_x incl. expressions)", () => {
  it(`matches ${B.max_payout.length} cases`, async () => {
    const d = new Diff();
    for (const c of B.max_payout) {
      const bo = c.no_bo ? null : { referenceBet: c.reference_bet };
      const got = await outcomeOf(() => resolveMaxPayout(bo, "zeus", revive(c.pt), revive(c.bet)));
      const where = `${JSON.stringify(c.pt)} bet=${JSON.stringify(c.bet)} ref=${JSON.stringify(c.reference_bet)}`;
      if (c.pt?.max_win_x === "bet.real") {
        // KNOWN GAP: pyEval has no attribute access, so `bet.real` (a float attribute in Python) is
        // refused with Python's own AttributeError wording instead of evaluating to the bet.
        d.expect(`${where} (python evaluated it)`, c.result, { ok: c.bet });
        d.expect(`${where} (ts refuses)`, (got as any).err, "RuntimeError");
        continue;
      }
      d.expect(where, got, pyOutcome(c.result));
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

// ── runner glue ──────────────────────────────────────────────────────────────────────

class FakeBo implements BoForSetup {
  paytableCode: string | null;
  referenceBet: unknown;
  readonly fail: string | null;
  readonly calls: unknown[] = [];

  constructor(spec: Record<string, any>) {
    this.paytableCode = spec.paytable_code ?? null;
    this.referenceBet = spec.reference_bet ?? null;
    this.fail = spec.fail ?? null;
  }

  resolvePaytable(opts: { ref?: string | null; inline?: unknown }): unknown {
    const ref = opts.ref ?? null;
    const inline = opts.inline ?? null;
    this.calls.push(["resolve_paytable", { ref, inline }]);
    if (inline !== null) return inline;
    if (ref === "missing") throw new PyValueError("No paytable fixture 'missing.json'");
    return { fixture: ref };
  }

  async ensurePaytable(code: string, opts: { paytableContent?: unknown }): Promise<unknown> {
    this.calls.push(["ensure_paytable", { code, paytable_content: opts.paytableContent ?? null }]);
    if (this.fail === "ensure") throw new ApiError("game/update/v2 failed: boom (X)");
    return 362;
  }

  async selectAgentPaytable(code: string, opts: { maxPayout?: unknown }): Promise<unknown> {
    this.calls.push(["select_agent_paytable", { code, max_payout: opts.maxPayout ?? null }]);
    return { status: "success" };
  }

  async updatePaytable(code: string, content: unknown): Promise<unknown> {
    this.calls.push(["update_paytable", { code, content }]);
    return { status: "success" };
  }

  async resetAgentPaytable(): Promise<unknown> {
    this.calls.push(["reset_agent_paytable", {}]);
    if (this.fail === "reset") throw new AuthError("Unauthorized — re-login required.");
    return { status: "success" };
  }
}

describe("runner glue: bo_setup / bo_reset / read_base_bet", () => {
  it(`bo_setup matches ${B.glue.setup.length} runs (calls, result, printed lines)`, async () => {
    const d = new Diff();
    for (const c of B.glue.setup) {
      reset();
      const bo = c.bo === null ? null : new FakeBo(c.bo);
      const got = await outcomeOf(() => boSetup(bo, "zeus", revive(c.pt), c.bet));
      const where = JSON.stringify(c.pt);
      d.expect(`${where} result`, got, pyOutcome(c.result));
      d.expect(`${where} calls`, bo ? plainBo(bo.calls) : null, c.calls);
      d.expect(`${where} printed`, state.printed, c.printed);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it(`bo_reset matches ${B.glue.reset.length} runs`, async () => {
    const d = new Diff();
    for (const c of B.glue.reset) {
      reset();
      const bo = c.bo === null ? null : new FakeBo(c.bo);
      const got = await outcomeOf(() => boReset(bo, c.label));
      d.expect(`${JSON.stringify(c.bo)} ${c.label} result`, got, c.result);
      d.expect(`${JSON.stringify(c.bo)} ${c.label} printed`, state.printed, c.printed);
      d.expect(`${JSON.stringify(c.bo)} ${c.label} calls`, bo ? plainBo(bo.calls) : null, c.calls);
    }
    expect(d.lines, d.report()).toEqual([]);
  });

  it(`read_base_bet matches ${B.glue.read_base_bet.length} page answers`, async () => {
    const d = new Diff();
    for (const c of B.glue.read_base_bet) {
      const seen: unknown[] = [];
      const page = {
        evaluate: async (fn: unknown) => {
          seen.push(fn);
          if (c.value === "__raise__") throw new Error("Execution context was destroyed");
          return c.value;
        },
      };
      const got = await outcomeOf(() => readBaseBet(page as any));
      d.expect(`${JSON.stringify(c.value)} result`, got, c.result);
      d.expect(`${JSON.stringify(c.value)} js`, seen.length === 1 && String(seen[0]) === B.glue.BASE_BET_JS, c.js_is_base_bet);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

// ── HTTP flows ───────────────────────────────────────────────────────────────────────

type Flow = (c: BoApiClient, a: any[], k: any) => Promise<unknown>;

const FLOWS: Record<string, Flow> = {
  post: (c, a) => c.post(a[0], a.length > 1 ? a[1] : null),
  "ags.get_agent_currencies": (c, a) => getAgentCurrencies(c, a[0]),
  "ags.get_agent_game_settings": (c, a, k) => getAgentGameSettings(c, a[0], a[1], {
    gameCode: k.game_code, page: k.page, pageSize: k.page_size, sortBy: k.sort_by, sortDirection: k.sort_direction,
  }),
  "ags.update_agent_game_settings": (c, a) => updateAgentGameSettings(c, a[0], a[1], a.length > 2 ? a[2] : null),
  "ags.set_agent_paytable": (c, a) => setAgentPaytable(c, a[0], a[1], a[2], a[3], a.length > 4 ? a[4] : null, a.length > 5 ? a[5] : null),
  "ags.reset_agent_paytable_to_default": (c, a) => resetAgentPaytableToDefault(c, a[0], a[1], a[2], a.length > 3 ? a[3] : null),
  "ags.agent_paytable": async (c, a, k) => {
    await agentPaytable(c, a[0], a[1], a[2], a[3], async () => {
      if (k.raise_in_body) throw new PyValueError("boom");
    });
    return "done";
  },
  "ugp.get_game_detail": (c, a) => getGameDetail(c, a[0], a[1]),
  "ugp.update_game": (c, a) => updateGame(c, a[0]),
  "ugp.update_game_paytable": (c, a) => updateGamePaytable(c, a[0], a[1], a[2], a[3]),
  "ugp.ensure_game_paytable": (c, a) => ensureGamePaytable(c, a[0], a[1], a[2], a.length > 3 ? a[3] : null, a.length > 4 ? a[4] : "MEDIUM"),
  "den.get_denomination_list": (c, a) => getDenominationList(c, a[0], a[1], ...(a.slice(2) as [string?, string?])),
  "den.update_denominations": (c, a) => updateDenominations(c, a[0], a[1], a[2]),
  "den.create_denomination": (c, a, k) => createDenomination(c, a[0], a[1], a[2], a[3], {
    defaultBetLevel: k.default_bet_level, remarks: k.remarks, verify: k.verify, listing: k.listing,
  }),
  "den.update_denomination": (c, a, k) => updateDenomination(c, a[0], a[1], a[2], {
    name: k.name, betLevels: k.bet_levels, defaultBetLevel: k.default_bet_level, remarks: k.remarks,
    isDefault: k.is_default, verify: k.verify,
  }),
  "den.ensure_automation_denomination": (c, a, k) => ensureAutomationDenomination(c, a[0], a[1], {
    betLevels: k.bet_levels, defaultBetLevel: k.default_bet_level, prefix: k.prefix, listing: k.listing,
  }),
  "den.assign_game_denomination": (c, a, k) => assignGameDenomination(c, a[0], a[1], a[2], {
    currencyCode: k.currency_code, currencyId: k.currency_id, isDenomOverride: k.is_denom_override, verify: k.verify,
  }),
  "den.get_agent_denomination": (c, a) => getAgentDenomination(c, a[0], a[1], a[2]),
  "den.assign_agent_denomination": (c, a, k) => assignAgentDenomination(c, a[0], a[1], a[2], a[3], {
    override: k.override, autoFinalization: k.auto_finalization,
  }),
  "den.reset_agent_denomination_to_default": (c, a, k) => resetAgentDenominationToDefault(c, a[0], a[1], a[2], {
    denominationId: k.denomination_id, autoFinalization: k.auto_finalization,
  }),
  "den.ensure_agent_automation_denomination": (c, a, k) => ensureAgentAutomationDenomination(c, a[0], a[1], a[2], a[3], a[4], {
    betLevels: k.bet_levels, defaultBetLevel: k.default_bet_level, prefix: k.prefix, autoFinalization: k.auto_finalization,
  }),
};

describe("HTTP flows (requests, refresh, retries, errors) with fetch stubbed", () => {
  it(`match ${B.flows.length} flows request-for-request`, async () => {
    const d = new Diff();
    for (const f of B.flows) {
      reset(B.now, f.responses);
      const refreshes: string[][] = [];
      const cl = f.client;
      const client = new BoApiClient(cl.token, cl.refresh_token, {
        apiBase: cl.api_base, lang: cl.lang, timeout: cl.timeout,
        onTokenRefresh: (t, rt) => refreshes.push([t, rt]),
      });
      const fn = FLOWS[f.fn];
      expect(fn, f.fn).toBeTypeOf("function");
      const got = await outcomeOf(() => fn(client, revive(f.args), revive(f.kwargs)));
      d.expect(`${f.name} result`, got, pyOutcome(f.result));
      d.expect(`${f.name} requests`, state.requests, f.requests);
      d.expect(`${f.name} unused replies`, state.queue.length, f.unused);
      d.expect(`${f.name} refreshes`, refreshes, f.refreshes);
      d.expect(`${f.name} logs`, normLogs(state.logs), normLogs(f.logs));
      d.expect(`${f.name} client after`, { token: client.token, refresh_token: client.refreshToken }, f.client_after);
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

// ── BoHelper ─────────────────────────────────────────────────────────────────────────

function normPaths(v: unknown, bases: string[]): unknown {
  if (typeof v === "string") {
    let s = v;
    let hit = false;
    for (const b of bases) {
      let i = s.toLowerCase().indexOf(b.toLowerCase());
      while (i >= 0) {
        s = s.slice(0, i) + "<BASE>" + s.slice(i + b.length);
        hit = true;
        i = s.toLowerCase().indexOf(b.toLowerCase());
      }
    }
    return hit ? s.replace(/\\/g, "/") : s;
  }
  if (Array.isArray(v)) return v.map((x) => normPaths(x, bases));
  if (isDict(v)) {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v)) out[k] = normPaths(x, bases);
    return out;
  }
  return v;
}

function helperCall(h: BoHelper, call: string, a: any[], k: any): unknown {
  const target = { agentId: k.agent_id, currencyId: k.currency_id, gameCode: k.game_code };
  switch (call) {
    case "select_agent_paytable": return h.selectAgentPaytable(a[0], { ...target, maxPayout: k.max_payout });
    case "reset_agent_paytable": return h.resetAgentPaytable(target);
    case "ensure_paytable": return h.ensurePaytable(a.length ? a[0] : undefined, {
      paytableContent: k.paytable_content, rtp: k.rtp, gameId: k.game_id, sasEntityId: k.sas_entity_id,
    });
    case "update_paytable": return h.updatePaytable(a[0], a[1], a.length > 2 ? a[2] : null, a.length > 3 ? a[3] : null);
    case "ensure_automation_denomination": return h.ensureAutomationDenomination({
      ...target, gameId: k.game_id, sasEntityId: k.sas_entity_id, betLevels: k.bet_levels,
    });
    case "assign_agent_denomination": return h.assignAgentDenomination(a[0], target);
    case "reset_agent_denomination": return h.resetAgentDenomination({ ...target, denominationId: k.denomination_id });
    case "resolve_paytable": return h.resolvePaytable({ ref: k.ref, inline: k.inline });
    case "persist_token": return h.persistToken(a[0], a.length > 1 ? a[1] : null);
    case "clear_token_cache": return h.clearTokenCache();
    case "_int_env": return BoHelper.intEnv(a[0]);
    default: throw new Error(`unmapped helper call ${call}`);
  }
}

describe("BoHelper: config, token cache, resolvePaytable and the BO methods", () => {
  it(`matches ${B.helper.length} helpers / ${B.helper.reduce((n: number, h: any) => n + h.actions.length, 0)} actions`, async () => {
    const d = new Diff();
    for (const spec of B.helper) {
      const tmp = mkdtempSync(join(tmpdir(), "bo-baseline-"));
      const bases = [...new Set([resolve(tmp), tmp, realpathSync.native(tmp)])].sort((x, y) => y.length - x.length);
      try {
        for (const [rel, text] of Object.entries(spec.files as Record<string, string>)) {
          const p = join(tmp, rel);
          mkdirSync(dirname(p), { recursive: true });
          writeFileSync(p, text, "utf-8");
        }
        for (const key of BO_ENV_KEYS) delete process.env[key];
        Object.assign(process.env, spec.environ);
        reset();
        let h: BoHelper | null = null;
        let construct: unknown;
        try {
          h = new BoHelper("zeus", spec.env, tmp);
          construct = { ok: true };
        } catch (e) {
          construct = { err: (e as Error).name, msg: (e as Error).message };
        }
        const rec: Record<string, unknown> = {
          name: spec.name, env: spec.env, files: spec.files, environ: spec.environ, construct,
          construct_logs: normLogs(state.logs), actions: [] as unknown[],
        };
        if (h) {
          const x = h as any;
          rec.attrs = {
            game_id: x.gameId, sas_entity_id: x.sasEntityId, agent_id: x.agentId, currency_id: x.currencyId,
            game_code: x.gameCode, reference_bet: x.referenceBet, paytable_code: x.paytableCode, token: x.token,
            refresh_token: x.refreshToken, api_base: x.apiBase, token_cache: basename(x.tokenCachePath),
          };
          for (const a of spec.actions) {
            reset(B.now, a.responses);
            const result = await outcomeOf(() => helperCall(h!, a.call, revive(a.args), revive(a.kwargs)));
            const cachePath = x.tokenCachePath as string;
            (rec.actions as unknown[]).push({
              call: a.call, args: a.args, kwargs: a.kwargs, responses: a.responses, result,
              requests: state.requests, unused: state.queue.length, logs: normLogs(state.logs),
              token_cache: existsSync(cachePath) ? readFileSync(cachePath, "utf-8") : null,
              client: x.clientInstance ? { token: x.clientInstance.token, refresh_token: x.clientInstance.refreshToken } : null,
            });
          }
        }
        const want = {
          ...spec,
          construct_logs: normLogs(spec.construct_logs),
          actions: spec.actions.map((a: any) => ({ ...a, result: pyOutcome(a.result), logs: normLogs(a.logs) })),
        };
        const got = normPaths(rec, bases) as Record<string, unknown>;
        for (const key of Object.keys(want)) d.expect(`${spec.name}.${key}`, got[key], (want as any)[key]);
      } finally {
        rmSync(tmp, { recursive: true, force: true });
      }
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});

// ── real fixtures -> payTable string ─────────────────────────────────────────────────

describe("paytable fixtures -> the payTable string sent to BO", () => {
  it(`matches ${B.fixtures.length} fixtures byte-for-byte`, () => {
    const d = new Diff();
    for (const f of B.fixtures) {
      const s = asPaytableString(pyJsonLoads(readFileSync(join(REPO, f.path), "utf-8")));
      d.expect(f.path, { sha256: createHash("sha256").update(s, "utf8").digest("hex"), length: [...s].length },
        { sha256: f.sha256, length: f.length });
    }
    expect(d.lines, d.report()).toEqual([]);
  });
});
