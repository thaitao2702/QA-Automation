/**
 * Wallet helper (port of framework/helpers/wallet.py): read and move the player's Playsite balance.
 *
 * Endpoint + credentials come from the per-environment .env (`PLAYSITE_API_URL`, `PLAYSITE_USER`,
 * `PLAYSITE_PASS`); the default currency from the game's `playsite.currency`, overridden by
 * `PLAYSITE_CURRENCY`. Construction never touches the network: the client logs in on first use.
 */
import type { WalletLike } from "../engine/drive.js";
import { pyFloat, pyRound, pyStr } from "../py.js";
import { envFileSuffix, loadEnvFiles, loadGameConfig, REPO_ROOT } from "./env.js";
import { type Amount, PlaysiteClient, PlaysiteError } from "./playsite.js";

export class WalletConfigError extends Error {
  override name = "WalletConfigError";
}

/** `n` as an exact scaled integer (value x 10^scale), from its shortest decimal text. */
function scaled(n: number, scale: number): bigint {
  const s = Number.isInteger(n) ? String(n) : n.toFixed(20).replace(/0+$/, "");
  const lit = Math.abs(n) < 1e21 ? (Number.isInteger(n) ? s : String(n)) : String(n);
  const m = /^(-?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(lit);
  if (!m) throw new Error(`not a decimal: ${lit}`);
  const digits = BigInt((m[2] || "0") + (m[3] || ""));
  const exp = Number(m[4] || 0) - (m[3] || "").length + scale;
  const v = exp >= 0 ? digits * 10n ** BigInt(exp) : digits / 10n ** BigInt(-exp);
  return m[1] === "-" ? -v : v;
}

/** `Decimal(str(target)) - Decimal(str(current))`, quantized to 0.001 AWAY from overshooting:
 * a deposit rounds UP, a withdrawal rounds DOWN, so the balance lands at or just above target. */
function walletStep(target: number, current: number): number {
  const SCALE = 12;                      // enough to hold any shortest float repr exactly
  const diff = scaled(target, SCALE) - scaled(current, SCALE);
  const unit = 10n ** BigInt(SCALE - 3);
  let thousandths: bigint;
  if (diff > 0n) thousandths = (diff + unit - 1n) / unit;           // ROUND_CEILING
  else thousandths = -((-diff) / unit);                            // -(|diff| ROUND_FLOOR)
  return Number(thousandths) / 1000;
}

export class WalletHelper implements WalletLike {
  private readonly baseUrl: string | null;
  private readonly user: string | null;
  private readonly pass: string | null;
  private readonly defaultCurrency: string | null;
  private client: PlaysiteClient | null = null;
  private loggingIn: Promise<PlaysiteClient> | null = null;

  constructor(readonly game: string, readonly env: string, readonly baseDir: string = REPO_ROOT) {
    loadEnvFiles(env, baseDir);
    this.baseUrl = (process.env.PLAYSITE_API_URL || "").trim() || null;
    this.user = (process.env.PLAYSITE_USER || "").trim() || null;
    this.pass = (process.env.PLAYSITE_PASS || "").trim() || null;
    const envCurrency = (process.env.PLAYSITE_CURRENCY || "").trim();
    this.defaultCurrency = envCurrency || this.loadGameCurrency() || null;
  }

  private loadGameCurrency(): string | null {
    try {
      const cfg = loadGameConfig(this.game, this.env, this.baseDir);
      const cur = (cfg.playsite || {}).currency;
      return cur === null || cur === undefined || cur === "" ? null : pyStr(cur).trim();
    } catch {
      return null;
    }
  }

  private resolveCurrency(currency: string | null): string {
    const cur = (currency || this.defaultCurrency || "").trim();
    if (!cur) {
      throw new WalletConfigError("No currency for the wallet step — pass params.currency, or set "
        + `playsite.currency in games/${this.game}/env.config.${envFileSuffix(this.env)}.yml (or the PLAYSITE_CURRENCY env var).`);
    }
    return cur;
  }

  /** The logged-in client, built on first use (concurrent first uses log in once). */
  private async getClient(): Promise<PlaysiteClient> {
    if (this.client) return this.client;
    if (!this.loggingIn) {
      this.loggingIn = (async () => {
        if (!this.user || !this.pass) {
          throw new WalletConfigError("Playsite credentials missing — set PLAYSITE_USER and PLAYSITE_PASS "
            + `in .env.${envFileSuffix(this.env)} (root config).`);
        }
        const c = new PlaysiteClient(this.baseUrl);
        await c.login(this.user, this.pass);
        this.client = c;
        return c;
      })().finally(() => {
        this.loggingIn = null;
      });
    }
    return this.loggingIn;
  }

  /** Run `call`, logging in again once if the session has expired — the token outlives a short run
   * but not a long one, and it expires exactly where it hurts most: in `restore-balance`. */
  private async withLoginRetry<T>(call: (c: PlaysiteClient) => Promise<T>): Promise<T> {
    try {
      return await call(await this.getClient());
    } catch (e) {
      if (!(e instanceof PlaysiteError) || !e.message.toLowerCase().includes("expire")) throw e;
      this.client = null;
      return call(await this.getClient());
    }
  }

  /** The numeric balance for `currency` (or the default); every wallet when there is no currency. */
  async getBalance(currency: string | null = null): Promise<any> {
    const cur = (currency || this.defaultCurrency || "").trim();
    if (cur) return this.withLoginRetry((c) => c.getBalanceFor(cur));
    return this.withLoginRetry((c) => c.getBalance());
  }

  async deposit(amount: Amount, currency: string | null = null): Promise<Record<string, any>> {
    const cur = this.resolveCurrency(currency);
    return this.withLoginRetry((c) => c.deposit(amount, cur));
  }

  async withdraw(amount: Amount, currency: string | null = null): Promise<Record<string, any>> {
    const cur = this.resolveCurrency(currency);
    return this.withLoginRetry((c) => c.withdraw(amount, cur));
  }

  /**
   * Set the wallet to exactly `target` by moving the difference, retrying a refusal that is only
   * momentary: a RACE (the balance fell between the read and the op) or a TRANSIENT HOLD (the wallet
   * reports more than it will release while a round settles — measured live 2026-09-03). Each try
   * re-reads the balance. Moves in THOUSANDTHS, the wallet's own precision (a 0.075 win at a 0.10
   * stake leaves a third decimal; cents left `freespin-zero-balance` 0.003 short).
   */
  async setBalance(target: Amount, opts: { currency?: string | null; attempts?: number; pauseS?: number } = {}): Promise<Record<string, any>> {
    const currency = this.resolveCurrency(opts.currency ?? null);
    const attempts = opts.attempts ?? 6;
    const pauseS = opts.pauseS ?? 1.0;
    const targetN = pyFloat(target);
    let last: PlaysiteError | null = null;
    let firstRead: number | null = null;
    for (let attempt = 0; attempt < attempts; attempt++) {
      const current = await this.withLoginRetry((c) => c.getBalanceFor(currency));
      const currentF = current === null || current === undefined ? 0 : pyFloat(current);
      if (firstRead === null) firstRead = currentF;
      const delta = walletStep(targetN, currentF);
      if (delta === 0) return { currency, balance: currentF };
      // Playsite takes FRACTIONAL amounts as a plain JSON number, settled to the thousandth. An int
      // is sent when the delta is whole, so whole-unit moves keep posting what they always did.
      const amount = Number.isInteger(delta) ? Math.abs(delta) : pyRound(Math.abs(delta), 3);
      try {
        return await this.withLoginRetry((c) => (delta > 0 ? c.deposit(amount, currency) : c.withdraw(amount, currency)));
      } catch (e) {
        if (!(e instanceof PlaysiteError)) throw e;
        last = e;
        console.log(`wallet ${currency} ${delta > 0 ? "deposit" : "withdraw"} ${amount} -> ${target} refused (${e.message}); `
          + `balance read ${currentF}, attempt ${attempt + 1}/${attempts}`);
        if (attempt < attempts - 1) await new Promise((r) => setTimeout(r, pauseS * 1000));
      }
    }
    let nowBal: unknown;
    try {
      nowBal = await this.withLoginRetry((c) => c.getBalanceFor(currency));
    } catch {
      nowBal = "unreadable";
    }
    const moved = String(nowBal) !== String(firstRead);
    throw new PlaysiteError(`${last ? last.message : "wallet op failed"} after ${attempts} attempts over `
      + `~${(attempts * pauseS).toFixed(0)}s (target ${target} ${currency}: balance first read ${firstRead}, now ${nowBal} -- `
      + `${moved ? "balance moved, an unsettled race" : "balance UNCHANGED, a hold that did not clear"})`, last?.messageCode ?? null);
  }
}

export { walletStep as _walletStepForTests };
