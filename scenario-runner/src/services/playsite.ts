/**
 * Playsite client: login, wallets, deposit / withdraw, game URL (port of playsite/playsite_client.py
 * and playsite/wallet_client.py). Maps 1:1 to the backend the Playsite front-end calls:
 *
 *     login          POST {base}/auth/login        {username, password}
 *     getBalance     GET  {base}/user/getBalance    (player wallets)
 *     deposit        POST {base}/wallet/deposit     {amount, currency}
 *     withdraw       POST {base}/wallet/withdraw    {amount, currency}
 *     getGameUrl     POST {base}/game/openGame      {gameCode, currency, language}
 */

export const DEFAULT_BASE_URL = "https://release.royaledge.io/api";

export type Amount = number | string;

/** The backend returned a non-success response. */
export class PlaysiteError extends Error {
  override name = "PlaysiteError";

  constructor(message: string, readonly messageCode: string | null = null, readonly status: string | null = null,
    readonly payload: Record<string, any> | null = null) {
    super(message);
  }
}

export class PlaysiteClient {
  readonly baseUrl: string;
  token: string | null = null;
  refreshToken: string | null = null;
  username: string | null = null;

  constructor(baseUrl: string | null = null, readonly timeoutMs = 30000) {
    this.baseUrl = (baseUrl || process.env.PLAYSITE_API_URL || DEFAULT_BASE_URL).replace(/\/+$/, "");
  }

  private url(path: string): string {
    return `${this.baseUrl}/${path.replace(/^\/+/, "")}`;
  }

  private authHeaders(): Record<string, string> {
    if (!this.token) throw new PlaysiteError("Not logged in. Call login() first.");
    return { Authorization: `Bearer ${this.token}` };
  }

  protected async request(method: string, path: string, opts: { jsonBody?: unknown; auth?: boolean } = {}): Promise<Record<string, any>> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (opts.auth) Object.assign(headers, this.authHeaders());
    const resp = await fetch(this.url(path), {
      method,
      headers,
      body: opts.jsonBody === undefined ? undefined : JSON.stringify(opts.jsonBody),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      throw new PlaysiteError(`Non-JSON response from ${path} (HTTP ${resp.status}): ${text.slice(0, 200)}`, null, String(resp.status));
    }
  }

  /** Success is `status == "success"`, or — when there is no status — no `messageCode`. */
  protected static isSuccess(body: Record<string, any>): boolean {
    const status = body.status;
    if (status !== undefined && status !== null) return status === "success";
    return body.messageCode === undefined || body.messageCode === null;
  }

  protected fail(body: Record<string, any>, fallback: string): PlaysiteError {
    return new PlaysiteError(body.message || fallback, body.messageCode ?? null, body.status ?? null, body);
  }

  /** Authenticate and keep the tokens. */
  async login(username: string, password: string): Promise<Record<string, any>> {
    const body = await this.request("POST", "/auth/login", { jsonBody: { username, password } });
    const data = body.data || {};
    if (!data.accessToken) throw this.fail(body, "Login failed");
    this.token = data.accessToken;
    this.refreshToken = data.refreshToken ?? null;
    this.username = username;
    return data;
  }

  /** Every wallet the player holds: [{currency, balance, isDefault}, ...]. */
  async getBalance(): Promise<Array<Record<string, any>>> {
    const body = await this.request("GET", "/user/getBalance", { auth: true });
    if (!PlaysiteClient.isSuccess(body)) throw this.fail(body, "Failed to fetch balance");
    return (body.data || {}).balance || [];
  }

  /** The numeric balance for one currency, or null when the player has no such wallet. */
  async getBalanceFor(currency: string): Promise<number | null> {
    for (const w of await this.getBalance()) if (w.currency === currency) return w.balance ?? null;
    return null;
  }

  async deposit(amount: Amount, currency: string): Promise<Record<string, any>> {
    return this.walletOp("/wallet/deposit", amount, currency);
  }

  async withdraw(amount: Amount, currency: string): Promise<Record<string, any>> {
    return this.walletOp("/wallet/withdraw", amount, currency);
  }

  private async walletOp(path: string, amount: Amount, currency: string): Promise<Record<string, any>> {
    const body = await this.request("POST", path, { jsonBody: { amount, currency }, auth: true });
    if (!PlaysiteClient.isSuccess(body)) throw this.fail(body, `${path} failed`);
    return body.data || {};
  }

  /** Launch a game and return its vendor URL. */
  async getGameUrl(gameCode: string, currency: string, language = "en"): Promise<string> {
    const body = await this.request("POST", "/game/openGame", { jsonBody: { gameCode, currency, language }, auth: true });
    const url = (body.data || {}).gameUrl;
    if (!PlaysiteClient.isSuccess(body) || !url) throw this.fail(body, "Failed to get game URL");
    return url;
  }
}
