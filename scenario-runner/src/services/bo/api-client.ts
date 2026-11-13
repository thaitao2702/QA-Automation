/**
 * Royaledge backoffice API client with automatic token refresh (port of framework/bo/bo_api_client.py).
 *
 * Replicates the frontend base-query behaviour (src/components/fe_foundation/lib/api.tsx:175-338):
 *
 *   - Every request body carries a `traceId` (uuid4).
 *   - Token expiry is detected by the response body field `messageCode == "TOKEN_EXPIRED"` (NOT by
 *     HTTP 401).
 *   - On expiry: POST `auth/refresh-token` with {traceId, refreshToken}.
 *         success  -> status in ("success", "SC_OK"); new token at data.token
 *         store new token, retry the original request once.
 *         failure  -> raise AuthError (frontend logs the user out here).
 *   - Tokens map to the frontend storage keys: ctoken (access), rtoken (refresh).
 *
 * The body is serialised exactly as `requests` did (`json.dumps(payload, allow_nan=False)`: `", "` /
 * `": "` separators, non-ASCII escaped), so the bytes on the wire are the Python client's.
 */
import { now } from "../../engine/page.js";
import { type Dict, get, has, isDict, or, PyAttributeError, pyFloat, pyStr, pyTypeName, truthy } from "../../py.js";
import { jsonDumps, pyJsonLoads, pyJsonLoadsBytes } from "./compat.js";
import { getLogger } from "./log.js";

const logger = getLogger("framework.bo.bo_api_client");

export const API_BASE = "https://staging-boapi.royaledge.io/sas-web/api/v1";

// messageCode values the frontend treats specially
export const TOKEN_EXPIRED = "TOKEN_EXPIRED";
export const UNAUTHORIZED = "UNAUTHORIZED_USER";
export const SUCCESS_STATUSES: readonly string[] = ["success", "SC_OK"];

// The access token lives 20 minutes; the refresh token lives 7 days. Crucially,
// `auth/refresh-token` requires a VALID access token in the authorization header --
// refreshing an already-expired token is rejected as UNAUTHORIZED_USER, and sending no
// header at all is UNKNOWN_ERROR. So the token must be renewed BEFORE it lapses.
// Verified live against dev 2026-09-03.
export const REFRESH_SKEW_S = 120;

/** Raised when the session is invalid and cannot be refreshed (re-login needed). */
export class AuthError extends Error {
  override name = "AuthError";
}

/** Raised when an API call returns a non-success status. */
export class ApiError extends Error {
  override name = "ApiError";
}

/** `requests.HTTPError` from `raise_for_status()`: a 4xx / 5xx reply. */
export class HTTPError extends Error {
  override name = "HTTPError";

  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** `requests.InvalidJSONError`: the body could not be serialised (NaN / Infinity). */
export class InvalidJSONError extends Error {
  override name = "InvalidJSONError";
}

export type TokenRefreshCallback = (newToken: string, newRefreshToken: string) => void;

/** What the BO flow functions need from a client. */
export interface BoClientLike {
  post(endpoint: string, body?: Dict | null): Promise<Record<string, any>>;
}

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/** `binascii.a2b_base64(s)` in its default (non-strict) mode: characters outside the alphabet are
 * skipped, a completed pad sequence ends the input, and a dangling quad raises. */
function a2bBase64(s: string): Uint8Array {
  const out: number[] = [];
  let quadPos = 0;
  let leftchar = 0;
  let pads = 0;
  for (const ch of s) {
    if (ch === "=") {
      if (quadPos >= 2 && quadPos + ++pads >= 4) return Uint8Array.from(out);
      continue;
    }
    const v = B64.indexOf(ch);
    if (v < 0) continue;
    pads = 0;
    switch (quadPos) {
      case 0:
        quadPos = 1;
        leftchar = v;
        break;
      case 1:
        quadPos = 2;
        out.push(((leftchar << 2) | (v >> 4)) & 0xff);
        leftchar = v & 0x0f;
        break;
      case 2:
        quadPos = 3;
        out.push(((leftchar << 4) | (v >> 2)) & 0xff);
        leftchar = v & 0x03;
        break;
      default:
        quadPos = 0;
        out.push(((leftchar << 6) | v) & 0xff);
        leftchar = 0;
    }
  }
  if (quadPos !== 0) {
    const e = new Error(quadPos === 1
      ? `Invalid base64-encoded string: number of data characters (${Math.floor(out.length / 3) * 4 + 1}) cannot be 1 more than a multiple of 4`
      : "Incorrect padding");
    e.name = "Error";
    throw e;
  }
  return Uint8Array.from(out);
}

/** `base64.urlsafe_b64decode(s)` for a str: ASCII only, `-`/`_` read as `+`/`/`. */
export function urlsafeB64decode(s: string): Uint8Array {
  // eslint-disable-next-line no-control-regex
  if (/[^\x00-\x7f]/.test(s)) {
    const e = new Error("string argument should contain only ASCII characters");
    e.name = "ValueError";
    throw e;
  }
  return a2bBase64(s.replace(/-/g, "+").replace(/_/g, "/"));
}

/** `exp` (epoch seconds) out of a JWT, or null when it cannot be read.
 *
 * null means "cannot know", and every caller treats that as "do not refresh proactively" -- an
 * opaque token then falls back to the reactive path. */
export function jwtExpiry(token: string | null | undefined): number | null {
  const parts = String(or(token, "")).split(".");
  if (parts.length !== 3) return null;
  try {
    const p = parts[1];
    const pad = p + "=".repeat((4 - (p.length % 4)) % 4);
    const payload = pyJsonLoadsBytes(urlsafeB64decode(pad));
    if (!isDict(payload) || !has(payload, "exp")) return null;
    return pyFloat(payload.exp);
  } catch {
    // an unreadable token simply has no known expiry
    return null;
  }
}

/** `threading.Lock` for async callers: one holder at a time, in arrival order. Not re-entrant. */
class Mutex {
  private tail: Promise<void> = Promise.resolve();

  async run<T>(fn: () => Promise<T>): Promise<T> {
    const prev = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((r) => {
      release = r;
    });
    await prev;
    try {
      return await fn();
    } finally {
      release();
    }
  }
}

/** `result.get(...)` on a reply that is not a JSON object raised AttributeError in Python. */
function asReply(result: unknown): Record<string, any> {
  if (!isDict(result)) throw new PyAttributeError(`'${pyTypeName(result)}' object has no attribute 'get'`);
  return result;
}

export interface BoApiClientOptions {
  apiBase?: string;
  onTokenRefresh?: TokenRefreshCallback | null;
  lang?: string;
  /** seconds, as `requests` took it */
  timeout?: number;
}

/**
 * Thin client that handles auth + transparent token refresh.
 *
 *     const client = new BoApiClient("<ctoken>", "<rtoken>");
 *     const data = await client.post("game/view", { gameId: 123, sasEntityId: 1 });
 *
 * Pass `onTokenRefresh(newToken, newRefreshToken)` to persist the rotated token (e.g. write it back
 * to a file / DB) so it survives across runs.
 */
export class BoApiClient implements BoClientLike {
  token: string;
  refreshToken: string;
  readonly apiBase: string;
  readonly onTokenRefresh: TokenRefreshCallback | null;
  readonly lang: string;
  readonly timeout: number;
  // serialize refreshes so concurrent calls don't refresh multiple times
  // (mirrors the mutex in the frontend base query)
  private readonly refreshLock = new Mutex();

  constructor(token: string, refreshToken: string, opts: BoApiClientOptions = {}) {
    this.token = token;
    this.refreshToken = refreshToken;
    this.apiBase = (opts.apiBase ?? API_BASE).replace(/\/+$/, "");
    this.onTokenRefresh = opts.onTokenRefresh ?? null;
    this.lang = opts.lang ?? "en";
    this.timeout = opts.timeout ?? 90;
  }

  // ---- internals -------------------------------------------------------
  private headers(): Record<string, string> {
    return {
      authorization: `Bearer ${this.token}`,
      Accept: "*/*;",
      "Content-Type": "application/json; charset=UTF-8",
      Lang: this.lang,
    };
  }

  /** Single POST: inject traceId, send, return parsed JSON. */
  private async rawPost(endpoint: string, body: Dict | null | undefined): Promise<unknown> {
    const payload: Dict = { ...(truthy(body) ? (body as Dict) : {}) };
    const traceId = globalThis.crypto.randomUUID();      // drawn even when the body has one, as setdefault did
    if (!has(payload, "traceId")) payload.traceId = traceId;
    const url = `${this.apiBase}/${endpoint.replace(/^\/+/, "")}`;
    let text: string;
    try {
      text = jsonDumps(payload, { allowNan: false });
    } catch (e) {
      if ((e as Error).name === "ValueError") throw new InvalidJSONError((e as Error).message);
      throw e;
    }
    const resp = await fetch(url, {
      method: "POST",
      headers: this.headers(),
      body: text,
      signal: AbortSignal.timeout(this.timeout * 1000),
    });
    // raise_for_status()
    const where = resp.url || url;
    if (resp.status >= 400 && resp.status < 500) {
      throw new HTTPError(`${resp.status} Client Error: ${resp.statusText} for url: ${where}`, resp.status);
    }
    if (resp.status >= 500 && resp.status < 600) {
      throw new HTTPError(`${resp.status} Server Error: ${resp.statusText} for url: ${where}`, resp.status);
    }
    // resp.json(): the reply decoded as UTF-8 (what `requests` picks for application/json)
    return pyJsonLoads(await resp.text());
  }

  /** POST auth/refresh-token, update this.token. Raise AuthError on failure. */
  private async refresh(): Promise<string> {
    if (!truthy(this.refreshToken)) throw new AuthError("No refresh token available — re-login required.");
    logger.info("Access token expired; refreshing.");
    const result = asReply(await this.rawPost("auth/refresh-token", { refreshToken: this.refreshToken }));
    if (!SUCCESS_STATUSES.includes(get(result, "status") as string)) {
      throw new AuthError(`Token refresh failed: ${pyStr(get(result, "message"))} (${pyStr(get(result, "messageCode"))})`);
    }
    const data = or(get(result, "data"), {});
    if (!isDict(data)) throw new PyAttributeError(`'${pyTypeName(data)}' object has no attribute 'get'`);
    const newToken = get(data, "token");
    if (!truthy(newToken)) throw new AuthError("Token refresh response missing data.token.");
    this.token = newToken as string;
    // dev returns the SAME refreshToken today, but honour a rotated one if it ever
    // starts sending a new one -- otherwise the next refresh would present a dead token.
    const newRefresh = get(data, "refreshToken");
    if (truthy(newRefresh)) this.refreshToken = newRefresh as string;
    if (this.onTokenRefresh) this.onTokenRefresh(newToken as string, this.refreshToken);
    logger.info("Access token refreshed (valid %.0fs).", (or(this.tokenExpiry(), 0) as number) - now());
    return newToken as string;
  }

  // ---- proactive expiry handling ---------------------------------------
  /** `exp` (epoch seconds) from the access-token JWT, or null if it is not readable. */
  tokenExpiry(): number | null {
    return jwtExpiry(this.token);
  }

  /** Refresh while the current token is still VALID, which is the only time it works.
   *
   * Without this the client only ever refreshed reactively, on a TOKEN_EXPIRED reply -- by which
   * point the access token had already lapsed and `auth/refresh-token` refused it. The refresh path
   * could therefore never succeed, and every run longer than the 20-minute token lifetime died
   * mid-suite needing a hand-pasted token. */
  private async ensureFreshToken(): Promise<void> {
    const exp = this.tokenExpiry();
    if (exp === null || now() < exp - REFRESH_SKEW_S) return;
    await this.refreshLock.run(async () => {
      const exp2 = this.tokenExpiry();          // another caller may have just renewed it
      if (exp2 !== null && now() >= exp2 - REFRESH_SKEW_S) {
        logger.info("Access token expires in %.0fs; refreshing ahead of use.", exp2 - now());
        await this.refresh();
      }
    });
  }

  // ---- public API ------------------------------------------------------
  /**
   * POST to an endpoint and return the parsed JSON body.
   *
   * On messageCode == TOKEN_EXPIRED, refreshes the access token (once) and retries the original
   * request. Raises ApiError on any other non-success status, AuthError when the session can't be
   * recovered.
   */
  async post(endpoint: string, body: Dict | null = null): Promise<Record<string, any>> {
    await this.ensureFreshToken();
    let result = asReply(await this.rawPost(endpoint, body));

    if (get(result, "messageCode") === TOKEN_EXPIRED) {
      result = await this.refreshLock.run(async () => {
        // re-check after acquiring the lock: another caller may have
        // already refreshed while we waited.
        const retry = asReply(await this.rawPost(endpoint, body));
        if (get(retry, "messageCode") !== TOKEN_EXPIRED) return retry;
        await this.refresh();
        return asReply(await this.rawPost(endpoint, body));
      });
    }

    const messageCode = get(result, "messageCode");
    if (messageCode === UNAUTHORIZED) throw new AuthError("Unauthorized — re-login required.");

    if (!SUCCESS_STATUSES.includes(get(result, "status") as string)) {
      // The summary alone ("One or more validation errors occurred.") does not say WHICH
      // field failed, so carry whatever else the server sent -- that is where the detail is.
      const detail: Dict = {};
      for (const [k, v] of Object.entries(result)) {
        if (!["status", "message", "messageCode", "traceId"].includes(k) && truthy(v)) detail[k] = v;
      }
      const detailText = truthy(detail)
        ? ` -- ${[...jsonDumps(detail, { ensureAscii: false })].slice(0, 2000).join("")}`
        : "";
      throw new ApiError(`${endpoint} failed: ${pyStr(get(result, "message"))} (${pyStr(messageCode)})${detailText}`);
    }
    return result;
  }
}
