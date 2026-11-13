/**
 * DriveContext — what a scenario is handed. It drives the game through the bridge, records every
 * round as a replay EVENT and a TIMELINE entry, and keeps the raw responses for the asserts.
 * Port of `class DriveContext` in scenario_engine.py; method names are the Python names camelCased.
 *
 * UI observation (popups, history rows, screenshots) reads the SCREEN, which is legitimate only
 * because a UI case's subject IS the UI. Game LOGIC comes exclusively from the server BET response.
 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import type { Browser, BrowserContext, Page } from "playwright";
import { type Dict, get, pyFloat, pyRound, PyRuntimeError } from "../py.js";
import { Bet } from "./bet.js";
import { awaitBetResponse, awaitBridge, awaitFreespinRound, clickSpin, clickStart, GAME_INFO_JS, onPromoAction, startGame,
  waitSpinClickable } from "./bridge.js";
import { skipBigWin } from "./flows.js";
import { GAME, LANGUAGE_NAME } from "./game.js";
import { evaluate, now, sleep, waitForFunction, withTimeout } from "./page.js";
import type { Shot } from "./result.js";

/** The Playsite wallet, as a scenario needs it (implemented in services/wallet). */
export interface WalletLike {
  getBalance(): Promise<number>;
  setBalance(amount: number, opts?: { attempts?: number; pauseS?: number }): Promise<unknown>;
  deposit(amount: number): Promise<unknown>;
}

/** The BO helper, as a scenario needs it (implemented in services/bo). */
export interface BoLike {
  ensureAutomationDenomination(): Promise<Dict>;
  assignAgentDenomination(id: unknown): Promise<unknown>;
  resetAgentDenomination(opts: { denominationId: unknown }): Promise<unknown>;
  [k: string]: any;
}

export interface DriveOptions {
  spinTimeoutMs?: number;
  betTimeoutMs?: number;
  fsTimeoutMs?: number;
  autoTimeoutMs?: number;
  settlePadMs?: number;
  uiDwellMs?: number;
  stepDwellMs?: number;
  wallet?: WalletLike | null;
  browser?: Browser | null;
  url?: string | null;
  bo?: BoLike | null;
  relaunch?: ((lang: string | null) => Promise<string>) | null;
  language?: string | null;
  shotsDir?: string | null;
}

export interface TimelineEntry {
  id: string;
  action: string;
  description: string;
  start_ms: number;
  end_ms: number;
  result: unknown;
}

/** A JS arrow that DECLARES a parameter: `(n) => ...` or `async (opts) => ...`, but not `() =>`. */
const JS_TAKES_ARG = /^\s*(?:async\s*)?\(\s*[A-Za-z_$][\w$]*\s*(?:,|\))\s*=>|^\s*(?:async\s*)?\(\s*[A-Za-z_$][\w$]*\s*\)\s*=>/;

export class DriveContext {
  readonly spinTimeoutMs: number;
  readonly betTimeoutMs: number;
  readonly fsTimeoutMs: number;
  /** How long to wait for a round the CLIENT fires (auto-play): the gap between auto spins is the
   * client's presentation, not a server round-trip (14 s and 37 s gaps seen on a healthy Mystic run). */
  readonly autoTimeoutMs: number;
  readonly settlePadMs: number;
  /** After each UI-change click, hold this long so the change lands and the AI can see it. 2 s: the
   * montage samples ONE composite per second, so 2 s is already 2 frames. */
  readonly uiDwellMs: number;
  /** ...and this for an INTERMEDIATE step of a multi-step interaction, so it gets a frame too. */
  readonly stepDwellMs: number;

  readonly wallet: WalletLike | null;
  /** The wallet BEFORE this run first changed it, so a teardown can put it back exactly. */
  walletWas: number | null = null;
  readonly browser: Browser | null;
  /** The game URL this run is playing (kept current across a language relaunch). */
  url: string | null;
  readonly bo: BoLike | null;
  /** How to put the agent's denomination back after this run swapped it (see reset-denomination). */
  denomWas: Dict | null = null;
  private second: { context: BrowserContext; page: Page } | null = null;
  private readonly relaunch: ((lang: string | null) => Promise<string>) | null;
  /** Language the game is CURRENTLY loaded in. */
  language: string | null;
  private readonly shotsDir: string | null;
  readonly shots: Shot[] = [];

  private popupTask: Promise<void> | null = null;
  private popupSeen: string[] = [];
  private popupMax = 0;
  private popupSamples = 0;
  private popupStop = true;

  /** Replay events (t is seconds since video start). A round's `seq` is `events.length`. */
  readonly events: Dict[] = [];
  /** RAW payloads for the replay's event inspector — kept OUT of `events` so seqs don't shift. */
  readonly apiEvents: Dict[] = [];
  readonly timeline: TimelineEntry[] = [];
  /** Video-relative start time per recorded round, indexed by seq. */
  readonly roundStarts: number[] = [];
  /** The last recorded BET response — server truth, read by teardowns. */
  lastBet: Bet | null = null;

  /** Set false in a scenario whose subject IS the big-win celebration. */
  skipBigWins = true;
  /** undefined = not read yet; null = the build reports no win bands. */
  bigWinThresholdCache: number | null | undefined = undefined;

  constructor(readonly page: Page, readonly videoStartTime: number, opts: DriveOptions = {}) {
    this.spinTimeoutMs = opts.spinTimeoutMs ?? 60000;
    this.betTimeoutMs = opts.betTimeoutMs ?? 15000;
    this.fsTimeoutMs = opts.fsTimeoutMs ?? 60000;
    this.autoTimeoutMs = opts.autoTimeoutMs ?? 90000;
    this.settlePadMs = opts.settlePadMs ?? 500;
    this.uiDwellMs = opts.uiDwellMs ?? 2000;
    this.stepDwellMs = opts.stepDwellMs ?? 1000;
    this.wallet = opts.wallet ?? null;
    this.browser = opts.browser ?? null;
    this.url = opts.url ?? null;
    this.bo = opts.bo ?? null;
    this.relaunch = opts.relaunch ?? null;
    this.language = opts.language ?? null;
    this.shotsDir = opts.shotsDir ?? null;
    // A promo dialog closed on this page goes on the run's timeline, so the report shows it.
    onPromoAction(page, (msg) => {
      console.log(`  ${msg}`);
      this.mark(msg);
    });
  }

  nowS(): number {
    return now() - this.videoStartTime;
  }

  /** Add a raw payload to the replay's event inspector. */
  logApi(kind: string, data: unknown, t: number | null = null): void {
    this.apiEvents.push({ t: pyRound(t === null ? this.nowS() : t, 3), type: kind, data });
  }

  /** Log the bridge's captured launch payload (raw) to the replay. Best-effort. */
  async captureGameInfo(when: string): Promise<void> {
    let raw: unknown;
    try {
      raw = await evaluate(this.page, GAME_INFO_JS);
    } catch {
      return;                     // a replay aid must never fail a run
    }
    if (raw) this.logApi("game_info", { when, gameInfo: raw });
  }

  /** Record a parsed round. Base + auto rounds come from awaitBetResponse; every other phase is a
   * free-spin round from awaitFreespinRound. */
  record(b: Bet, phase: string, t0: number): void {
    this.logApi(phase === "base" || phase === "auto" ? "bet_response" : "freespin_response",
      { seq: b.seq, phase, response: b.raw }, t0);
    b.phase = phase;
    this.lastBet = b;
    this.roundStarts.push(pyRound(t0, 3));
    this.events.push({ t: pyRound(t0, 3), type: "spin_result", data: b.row() });
    this.timeline.push({ id: `${phase}-${b.seq}`, action: phase, description: phase,
      start_ms: Math.trunc(t0 * 1000), end_ms: Math.trunc(this.nowS() * 1000), result: b.row() });
  }

  /** Per-round video sub-windows, indexed by seq: round i spans its own start to the next round's
   * start (the last runs to now, i.e. through settle). */
  windows(): Array<[number, number]> {
    const end = this.nowS();
    return this.roundStarts.map((s, i) => [s, pyRound(i + 1 < this.roundStarts.length ? this.roundStarts[i + 1] : end, 3)]);
  }

  /** One real base spin: wait clickable -> coordinate click -> await the response. */
  async baseSpin(): Promise<Bet> {
    await waitSpinClickable(this.page, this.spinTimeoutMs);
    const t0 = this.nowS();
    await clickSpin(this.page);
    const b = Bet.parse(await awaitBetResponse(this.page, this.betTimeoutMs), this.events.length);
    this.record(b, "base", t0);
    await skipBigWin(this, b);
    return b;
  }

  /** One auto-played free-spin round: passive await (no click). */
  async freeSpin(phase = "freespin"): Promise<Bet> {
    const t0 = this.nowS();
    const b = Bet.parse(await awaitFreespinRound(this.page, this.fsTimeoutMs), this.events.length);
    this.record(b, phase, t0);
    await skipBigWin(this, b);
    return b;
  }

  /** Await the next server BET response WITHOUT clicking — for client-driven play. `phase` tags what
   * the round IS: a round the scenario caused by a gesture other than a click (a Space press) is a
   * paid BASE round and must be tagged "base", or `baseRoundsSince` will not count it. */
  async observeBet(timeoutMs: number | null = null, phase = "auto"): Promise<Bet> {
    const t0 = this.nowS();
    const b = Bet.parse(await awaitBetResponse(this.page, timeoutMs ?? this.autoTimeoutMs), this.events.length);
    this.record(b, phase, t0);
    await skipBigWin(this, b);
    return b;
  }

  /** Record the wallet as it is RIGHT NOW, once per run, before anything changes it. */
  private async rememberWallet(): Promise<void> {
    if (this.wallet === null || this.walletWas !== null) return;
    try {
      this.walletWas = pyFloat(await this.wallet.getBalance());
    } catch {
      this.walletWas = null;
    }
  }

  /** Set the player's wallet to an absolute amount via the Playsite wallet API. */
  async setBalance(amount: number, attempts = 6, pauseS = 1.0): Promise<void> {
    if (this.wallet === null) throw new PyRuntimeError("wallet not configured -- set PLAYSITE_API_URL/USER/PASS in .env");
    await this.rememberWallet();
    await this.wallet.setBalance(amount, { attempts, pauseS });
  }

  /** Deposit a whole-unit amount INTO the player's wallet. */
  async deposit(amount: number): Promise<void> {
    if (this.wallet === null) throw new PyRuntimeError("wallet not configured -- set PLAYSITE_API_URL/USER/PASS in .env");
    await this.rememberWallet();
    await this.wallet.deposit(amount);
  }

  /** TEAR THE GAME DOWN so it cannot play another round, without ending the run. The bonus lives on
   * the SERVER, so it survives and `reloadAndStart({url})` resumes it. */
  async stopClient(): Promise<void> {
    const t0 = this.nowS();
    await this.page.goto("about:blank", { waitUntil: "load" });
    this.step("stop", "client torn down (about:blank) so no further round can play", t0);
  }

  /** Reload (so the client re-reads the wallet) and re-enter the game. `url` navigates there instead
   * — needed after `stopClient()`. Returns the free-spin rounds the RELOAD itself resumed, recorded
   * under `phase`. `onReady` runs in the gap between the bridge appearing and Start. */
  async reloadAndStart(opts: { phase?: string; url?: string | null; onReady?: (() => Promise<void>) | null } = {}): Promise<Bet[]> {
    const phase = opts.phase ?? "freespin";
    if (opts.url) await this.page.goto(opts.url, { waitUntil: "networkidle" });
    else await this.page.reload({ waitUntil: "networkidle" });
    await waitForFunction(this.page, "() => !!window.__gameTestBridge", 60000);
    await this.captureGameInfo("reload");
    if (opts.onReady) await opts.onReady();
    const resumed: Bet[] = [];
    await startGame(this.page, 60000, async (raw) => {
      const t0 = this.nowS();
      const b = Bet.parse(raw, this.events.length);
      this.record(b, phase, t0);
      resumed.push(b);
    });
    return resumed;
  }

  /** Re-enter the game on a NEW launch token, for a run that ended the session ON PURPOSE. Call it
   * AFTER the evidence is in the result. Best-effort: false instead of raising. */
  async freshSession(why: string): Promise<boolean> {
    const t0 = this.nowS();
    try {
      if (this.relaunch !== null) this.url = await this.relaunch(this.language);
      else await this.reloadAndStart({ url: this.url });
    } catch (e) {
      console.log(`  ! fresh session after ${why} failed: ${(e as Error).name}: ${(e as Error).message}`);
      return false;
    }
    await this.captureGameInfo(`fresh session (${why})`);
    this.step("fresh_session", `re-entered the game on a new launch token (${why})`, t0);
    return true;
  }

  /**
   * Put the agent's game row on the automation bet ladder (11, 22, 33) in BO. BO only — takes effect
   * on the next load. ⚠ RULE 20: BO answering 200 means BO stored it, NOT that the game server has
   * it, so this WAITS `settleS` and the caller must still read the ladder back from the reopened
   * client. Records the previous denomination for `teardown: reset-denomination`, which the run MUST
   * declare. `{}` when BO is not configured.
   */
  async setAutomationDenomination(settleS = 5.0): Promise<Dict> {
    if (this.bo === null) return {};
    const info = await this.bo.ensureAutomationDenomination();
    if (this.denomWas === null) {
      const prev = get(info, "previous_denomination_id");
      const automation = get(info, "denomination_id");
      this.denomWas = {
        id: prev,
        override: get(info, "previous_is_denom_override"),
        master: get(info, "master_denomination_id"),
        // A row ALREADY on the automation ladder means an earlier run's teardown failed.
        leaked: prev !== null && prev === automation,
      };
    }
    if (settleS) await sleep(settleS * 1000);
    return info;
  }

  /** Load the game and click Start. Nothing more: an owed bonus is left to the scenario. */
  async openGame(url: string | null = null): Promise<void> {
    const t0 = this.nowS();
    await this.loadGame(url);
    await clickStart(this.page);
    this.step("open", "game loaded and Start clicked (any owed bonus is left to the scenario)", t0);
  }

  /** Load the game and wait for the bridge — WITHOUT clicking Start (safe with a bonus owed). */
  async loadGame(url: string | null = null): Promise<void> {
    await this.page.goto((url || this.url) as string, { waitUntil: "networkidle" });
    await waitForFunction(this.page, "() => !!window.__gameTestBridge", 60000);
    await this.captureGameInfo("load");
  }

  /** Wait for the CLIENT to finish presenting the last round, so the recorded window covers the
   * whole animation. 'Spin clickable again' is the settle signal; a short pad leaves a clean tail. */
  async settle(): Promise<void> {
    const t0 = this.nowS();
    try {
      await waitSpinClickable(this.page, this.spinTimeoutMs);
    } catch {
      await sleep(3000);                       // fallback: heuristic settle pad
    }
    if (this.settlePadMs) await sleep(this.settlePadMs);
    this.timeline.push({ id: `settle-${this.timeline.length}`, action: "settle", description: "presentation settle",
      start_ms: Math.trunc(t0 * 1000), end_ms: Math.trunc(this.nowS() * 1000), result: null });
  }

  /** Read a config/UI value the scenario needs internally. */
  async eval(expr: string, ...arg: [unknown?]): Promise<any> {
    return evaluate(this.page, expr, ...arg);
  }

  /** Hold for `ms` (default the UI dwell) so the last change settles on screen. `0` skips. */
  async dwell(ms: number | null = null): Promise<void> {
    const d = ms === null ? this.uiDwellMs : ms;
    if (d && d > 0) await sleep(d);
  }

  /**
   * Wait for a named bridge button to be clickable, issue a REAL coordinate click, then DWELL. The
   * name is translated through the profile (`GAME.resolve`); a role that resolves to null means the
   * build has no such control. `optional` -> false instead of raising.
   */
  async clickButton(name: string, opts: { optional?: boolean; timeoutMs?: number | null; dwellMs?: number | null } = {}): Promise<boolean> {
    const optional = opts.optional ?? false;
    const t0 = this.nowS();
    const resolved = GAME.resolve(name);
    if (resolved === null) {
      if (optional) return false;
      throw new PyRuntimeError("this build has no such control (profile maps the role to None)");
    }
    const tmo = opts.timeoutMs ?? this.spinTimeoutMs;
    if (!(await this.openVia(resolved, tmo, optional))) return false;
    const call = `b.waitForButtonClickable(${JSON.stringify(resolved)}, undefined, ${Math.trunc(tmo)})`;
    let rect: any;
    try {
      rect = await awaitBridge(this.page, call, tmo + 2000, `waitForButtonClickable(${resolved})`);
    } catch (e) {
      if (optional) return false;
      throw e;
    }
    if (!rect) {
      if (optional) return false;
      throw new PyRuntimeError(`waitForButtonClickable('${resolved}') returned no rect`);
    }
    await this.page.mouse.click(rect.centerX, rect.centerY);
    await this.dwell(opts.dwellMs ?? null);
    this.timeline.push({ id: `click-${this.timeline.length}`, action: "click", description: `click ${resolved}`,
      start_ms: Math.trunc(t0 * 1000), end_ms: Math.trunc(this.nowS() * 1000), result: { button: resolved } });
    return true;
  }

  /** Scroll denomination row `i` into view, then DWELL so it lands in frame. */
  async scrollBetRow(i: number, dwellMs: number | null = null): Promise<void> {
    const t0 = this.nowS();
    try {
      await evaluate(this.page, "(i) => window.__gameTestBridge.scrollBetRowIntoView(i)", i);
    } catch {
      // best-effort
    }
    await this.dwell(dwellMs);
    this.timeline.push({ id: `scroll-${this.timeline.length}`, action: "scroll", description: `scroll bet row ${i} into view`,
      start_ms: Math.trunc(t0 * 1000), end_ms: Math.trunc(this.nowS() * 1000), result: { row: i } });
  }

  step(action: string, desc: string, t0: number, result: unknown = null): void {
    this.timeline.push({ id: `${action}-${this.timeline.length}`, action, description: desc,
      start_ms: Math.trunc(t0 * 1000), end_ms: Math.trunc(this.nowS() * 1000), result });
  }

  /** A labeled timeline MARKER now: in a timestamp-mode video assert it surfaces in the ACTION
   * TIMELINE, so the AI can pin WHEN a described action happened. */
  mark(label: string): void {
    this.step("mark", label, this.nowS());
  }

  private async rect(name: string, timeoutMs: number | null, optional: boolean): Promise<any> {
    const resolved = GAME.resolve(name);
    if (resolved === null) {
      if (optional) return null;
      throw new PyRuntimeError("this build has no such control (profile maps the role to None)");
    }
    const tmo = timeoutMs ?? this.spinTimeoutMs;
    if (!(await this.openVia(resolved, tmo, optional))) return null;
    const call = `b.waitForButtonClickable(${JSON.stringify(resolved)}, undefined, ${Math.trunc(tmo)})`;
    let rect: any;
    try {
      rect = await awaitBridge(this.page, call, tmo + 2000, `waitForButtonClickable(${resolved})`);
    } catch (e) {
      if (optional) return null;
      throw e;
    }
    if (!rect && !optional) throw new PyRuntimeError(`waitForButtonClickable('${resolved}') returned no rect`);
    return rect;
  }

  /** A control INSIDE a panel (profile `BUTTON_VIA`, e.g. Grandma's History inside Settings) is
   * reached by opening that panel first, when the control is not already on screen. */
  private async openVia(name: string, tmo: number, optional: boolean): Promise<boolean> {
    const opener = GAME.via(name);
    if (!opener || (await this.onScreen(name))) return true;
    const t0 = this.nowS();
    const openerRect = await this.rect(opener, tmo, optional);   // recursive: nested menus work
    if (openerRect === null || openerRect === undefined) return false;
    await this.page.mouse.click(openerRect.centerX, openerRect.centerY);
    this.step("click", `open ${GAME.resolve(opener)} to reach ${name}`, t0, { button: GAME.resolve(opener), to_reach: name });
    await this.dwell(this.stepDwellMs);
    return true;
  }

  /** Is the control clickable RIGHT NOW? An instant `getButtonRect` probe. */
  private async onScreen(name: string): Promise<boolean> {
    const r = await this.uiRead("(n) => { const b = window.__gameTestBridge; return b && b.getButtonRect ? b.getButtonRect(n) : null; }", null, name);
    return !!(r && r.interactable && r.visible);
  }

  /** Press-and-HOLD a button for `holdMs` with a REAL pointer hold. `useBridge` asks for the
   * bridge's own hold, probed for and silently replaced by the pointer hold when absent. */
  async holdButton(name: string, opts: { holdMs?: number; useBridge?: boolean; optional?: boolean; timeoutMs?: number | null } = {}): Promise<boolean> {
    const holdMs = opts.holdMs ?? 1000;
    const t0 = this.nowS();
    const rect = await this.rect(name, opts.timeoutMs ?? null, opts.optional ?? false);
    if (rect === null || rect === undefined) return false;
    const release = opts.useBridge ? await this.uiRead(
      "() => { const b = window.__gameTestBridge;"
      + " if (!b || typeof b.holdSpinStart !== 'function') return null;"
      + " if (typeof b.holdSpinRelease === 'function') return 'holdSpinRelease';"
      + " if (typeof b.holdSpinEnd === 'function') return 'holdSpinEnd';"
      + " return null; }") : null;
    if (opts.useBridge && !release) this.mark(`HOLD ${name}: no bridge hold on this build -- using a real pointer hold`);
    if (release) {
      await evaluate(this.page, "() => window.__gameTestBridge.holdSpinStart()");
      await sleep(holdMs);
      await evaluate(this.page, `() => window.__gameTestBridge.${release}()`);
    } else {
      await this.page.mouse.move(rect.centerX, rect.centerY);
      await this.page.mouse.down();
      await sleep(holdMs);
      await this.page.mouse.up();
    }
    await this.dwell();
    this.step("hold", `hold ${name} for ${holdMs}ms${release ? " (bridge)" : ""}`, t0, { button: name, hold_ms: holdMs });
    return true;
  }

  /** Double-click a button via real pointer events (double-click sudden-stop). */
  async doubleClickButton(name: string, opts: { optional?: boolean; timeoutMs?: number | null } = {}): Promise<boolean> {
    const t0 = this.nowS();
    const rect = await this.rect(name, opts.timeoutMs ?? null, opts.optional ?? false);
    if (rect === null || rect === undefined) return false;
    await this.page.mouse.dblclick(rect.centerX, rect.centerY);
    await this.dwell();
    this.step("dblclick", `double-click ${name}`, t0, { button: name });
    return true;
  }

  /** Press-and-HOLD a keyboard key (e.g. Space for hold-to-turbo). */
  async holdKey(key = " ", holdMs = 1000): Promise<void> {
    const t0 = this.nowS();
    await this.page.keyboard.down(key);
    await sleep(holdMs);
    await this.page.keyboard.up(key);
    await this.dwell();
    this.step("hold_key", `hold '${key}' for ${holdMs}ms`, t0, { key, hold_ms: holdMs });
  }

  /** Tap a keyboard key once (e.g. Space to spin). */
  async pressKey(key = " "): Promise<void> {
    const t0 = this.nowS();
    await this.page.keyboard.press(key);
    this.step("key", `press '${key}'`, t0, { key });
  }

  /** Drop / restore the page's network — for disconnect/reconnect tests. */
  async setOffline(offline: boolean): Promise<void> {
    const t0 = this.nowS();
    await this.page.context().setOffline(offline);
    this.step("net", offline ? "network offline" : "network online", t0, { offline });
  }

  /** Open a SECOND browser session (fresh context, SAME account URL) and enter the game — for the
   * "same account on two tabs" test. */
  async openSecondSession(): Promise<Page> {
    if (this.browser === null || !this.url) {
      throw new PyRuntimeError("second session needs browser + url (runner must pass them to DriveContext)");
    }
    const t0 = this.nowS();
    const context = await this.browser.newContext({ viewport: GAME.VIEWPORT });
    const page2 = await context.newPage();
    await page2.goto(this.url, { waitUntil: "networkidle" });
    await waitForFunction(page2, "() => !!window.__gameTestBridge", 60000);
    await startGame(page2);
    this.second = { context, page: page2 };
    this.step("second_session", "open second session (same account, new context)", t0);
    return page2;
  }

  /** Close the second session if one was opened (idempotent). */
  async closeSecondSession(): Promise<void> {
    if (this.second !== null) {
      try {
        await this.second.context.close();
      } catch {
        // already gone
      }
      this.second = null;
    }
  }

  /**
   * Best-effort bridge read for a UI-only scenario: `fallback` when the call throws. ⚠ Pass `arg`
   * when the JS takes a parameter — a parameterised JS with no `arg` RAISES, because it would run
   * with `undefined` and answer confidently about a control named `undefined`.
   */
  async uiRead(js: string, fallback: any = null, arg: unknown = null): Promise<any> {
    if (arg === null && JS_TAKES_ARG.test(js)) {
      throw new PyRuntimeError("ui_read() was given JS that declares a parameter but no `arg` to fill it, so it "
        + "would run with `undefined`. Pass `arg=...`, or drop the parameter from the JS.\n"
        + `  js: ${js.slice(0, 120)}...`);
    }
    try {
      return await (arg !== null ? evaluate(this.page, js, arg) : evaluate(this.page, js));
    } catch {
      return fallback;
    }
  }

  /** Labels of the popups on screen right now (`getSnapshot().visiblePopups`). Promo dialogs are
   * left out unless `includePromo`: one can show at any idle moment, no scenario here is about it,
   * and `waitSpinClickable` closes it. */
  async popups(includePromo = false): Promise<string[]> {
    const all: string[] = (await this.uiRead("() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : null;"
      + " return (s && s.visiblePopups) || []; }", [])) || [];
    return includePromo ? all : all.filter((l) => !GAME.isPromoPopup(l));
  }

  /** Does this BUILD have the control at all? An instant probe — never burns a timeout. */
  async hasButton(name: string): Promise<boolean> {
    const resolved = GAME.resolve(name);
    if (resolved === null) return false;
    const rect = await this.uiRead(`() => { const b = window.__gameTestBridge; return b && b.getButtonRect ? b.getButtonRect(${JSON.stringify(resolved)}) : null; }`);
    return !!(rect && rect.visible);
  }

  /** Clear the screen; returns the labels the bridge VERIFIED closed. */
  async dismissPopups(): Promise<string[]> {
    return (await this.uiRead("() => { const b = window.__gameTestBridge; return b && b.dismissPopups ? b.dismissPopups() : []; }", [])) || [];
  }

  /** `getHistoryDetailState()` -> {open, betIndex, betCount, hasPrev, hasNext}. Drive on `hasNext`. */
  async historyDetailState(): Promise<Dict> {
    return (await this.uiRead("() => { const b = window.__gameTestBridge; return b && b.getHistoryDetailState ? b.getHistoryDetailState() : null; }")) || {};
  }

  /** Wait until the History list has RENDERED rows, then return them ([] on timeout, and [] at once
   * on a build with no `getHistoryRows`). */
  async waitForHistoryRows(timeoutMs = 10000, pollMs = 250): Promise<Dict[]> {
    if (!(await this.uiRead("() => { const b = window.__gameTestBridge; return !!(b && typeof b.getHistoryRows === 'function'); }", false))) return [];
    const deadline = now() + timeoutMs / 1000;
    let rows = await this.historyRows();
    while (!rows.length && now() < deadline) {
      await sleep(pollMs);
      rows = await this.historyRows();
    }
    return rows;
  }

  /** Wait until `label` is in the visible-popup list. A null label answers false at once. */
  async waitForPopup(label: string | null, timeoutMs = 8000, pollMs = 200): Promise<boolean> {
    if (!label) return false;
    const deadline = now() + timeoutMs / 1000;
    while (now() < deadline) {
      if ((await this.popups()).includes(label)) return true;
      await sleep(pollMs);
    }
    return false;
  }

  /** Did the History DETAIL ticket open? `getHistoryDetailState().open` first, then the profile's
   * label, then ANY popup that was not up before — `visiblePopups` is a DIALECT. */
  async waitForHistoryDetail(timeoutMs = 10000, before: Set<string> | null = null, pollMs = 200): Promise<boolean> {
    const deadline = now() + timeoutMs / 1000;
    const label = GAME.popup("history_detail");
    const prior = before ?? new Set<string>();
    for (;;) {
      if ((await this.historyDetailState()).open) return true;
      const live = new Set(await this.popups());
      if (label && live.has(label)) return true;
      if ([...live].some((x) => !prior.has(x))) return true;
      if (now() >= deadline) return false;
      await sleep(pollMs);
    }
  }

  /** Real mouse wheel at (x, y), `steps` times, dwelling after each. ONE STEP IS ONE FULL PAGE by
   * default (`VIEWPORT.height * fraction`); `delta` overrides with explicit pixels. */
  async wheelAt(x: number, y: number, opts: { steps?: number; delta?: number | null; dwellMs?: number | null; label?: string; fraction?: number } = {}): Promise<number> {
    const steps = opts.steps ?? 6;
    const d = opts.delta ?? Math.trunc(GAME.VIEWPORT.height * (opts.fraction ?? 1.0));
    await this.page.mouse.move(x, y);
    for (let i = 0; i < steps; i++) {
      const t0 = this.nowS();
      await this.page.mouse.wheel(0, d);
      await this.dwell(opts.dwellMs ?? null);
      this.step("scroll", `scroll ${opts.label ?? "content"} ${i + 1}/${steps} (${d}px)`, t0, { delta: d, step: i + 1, steps });
    }
    return steps;
  }

  /** Wheel down the popup on screen, one full page per step (over the viewport centre). */
  async scrollPopup(opts: { steps?: number; dwellMs?: number | null; label?: string; fraction?: number; pagePx?: number | null } = {}): Promise<number> {
    return this.wheelAt(GAME.VIEWPORT.width / 2, GAME.VIEWPORT.height / 2, {
      steps: opts.steps ?? 6, dwellMs: opts.dwellMs ?? null, label: opts.label ?? "popup",
      delta: opts.pagePx ?? null, fraction: opts.fraction ?? 1.0,
    });
  }

  /** The History rows RENDERED right now ({index, rect, clickable, data}). [] when closed. */
  async historyRows(): Promise<Dict[]> {
    return (await this.uiRead("() => { const b = window.__gameTestBridge; return b && b.getHistoryRows ? b.getHistoryRows() : []; }", [])) || [];
  }

  /** Open a bridge EVENT capture window. */
  async startCapture(label = "popups"): Promise<void> {
    await this.uiRead(`() => { const b = window.__gameTestBridge; if (b && b.startEventCapture) b.startEventCapture(${JSON.stringify(label)}); return true; }`);
  }

  /** Close the capture window and return its events ([] if the bridge has no capture). */
  async endCapture(label = "popups"): Promise<Dict[]> {
    return (await this.uiRead(`() => { const b = window.__gameTestBridge; return (b && b.endEventCapture ? b.endEventCapture(${JSON.stringify(label)}) : []) || []; }`, [])) || [];
  }

  /** Sample the visible-popup list in the BACKGROUND while the scenario keeps clicking (Mah_108). */
  startPopupWatch(intervalS = 0.12): void {
    this.popupSeen = [];
    this.popupMax = 0;
    this.popupSamples = 0;
    this.popupStop = false;
    this.popupTask = (async () => {
      while (!this.popupStop) {
        const labels = await this.popups();
        this.popupSamples += 1;
        this.popupMax = Math.max(this.popupMax, labels.length);
        for (const l of labels) if (!this.popupSeen.includes(l)) this.popupSeen.push(l);   // first-appearance ORDER
        await sleep(intervalS * 1000);
      }
    })();
  }

  /** Stop the background poll -> {order, max_concurrent, samples}. */
  async stopPopupWatch(): Promise<Dict> {
    this.popupStop = true;
    if (this.popupTask !== null) {
      try {
        await withTimeout(this.popupTask, 2000);
      } catch {
        // abandoned: the loop sees popupStop on its next tick
      }
      this.popupTask = null;
    }
    return { order: [...this.popupSeen], max_concurrent: this.popupMax, samples: this.popupSamples };
  }

  /** Re-enter the game in language `code` (the language lives in the launch token, so this is a real
   * reload via the runner's `relaunch`). Returns the new launch URL. */
  async setLanguage(code: string): Promise<string> {
    if (this.relaunch === null) {
      throw new PyRuntimeError("language switching needs the runner's relaunch callback "
        + "(run scenarios through run_scenario.py, and set `language:` on the run)");
    }
    const t0 = this.nowS();
    const url = await this.relaunch(code);
    await this.captureGameInfo(`relaunch (${code})`);
    this.language = code;
    this.url = url;
    this.step("language", `relaunch in ${LANGUAGE_NAME[code] ?? code} (${code})`, t0, { language: code });
    return url;
  }

  /** Capture the viewport to a PNG and register it as a named SHOT (full resolution — the right
   * evidence for a text check). Returns the file path, or null without a shots dir. */
  async screenshot(label: string): Promise<string | null> {
    if (this.shotsDir === null) return null;
    mkdirSync(this.shotsDir, { recursive: true });
    const safe = label.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "shot";
    const file = `${String(this.shots.length).padStart(2, "0")}-${safe}.png`;
    const path = join(this.shotsDir, file);
    const t0 = this.nowS();
    try {
      await this.page.screenshot({ path });
    } catch (e) {
      this.step("shot", `screenshot FAILED for ${label}: ${(e as Error).message}`, t0, { label });
      return null;
    }
    this.shots.push({ label, file, path });
    this.step("shot", `screenshot: ${label}`, t0, { label, file });
    return path;
  }

  /** The text-bearing surfaces from the profile, with roles resolved to this build's names. */
  get UI_SURFACES(): Array<[string, string | null, string | null]> {
    return GAME.UI_SURFACES.map(([label, op, cl]) => [label, op ? GAME.button(op) : null, cl ? GAME.button(cl) : null]);
  }

  /** Walk the text-bearing surfaces and pause on each (and, with `shots`, capture each as a still
   * named "<prefix>surface"). `surfaces` tours only those. Returns those reached. */
  async tourUi(opts: { dwellMs?: number; shots?: boolean; prefix?: string; surfaces?: string[] | null } = {}): Promise<string[]> {
    const dwellMs = opts.dwellMs ?? 2000;
    const shots = opts.shots ?? true;
    const prefix = opts.prefix ?? "";
    const wanted = opts.surfaces?.length ? new Set(opts.surfaces) : null;
    const seen: string[] = [];
    for (const [label, opener, closer] of this.UI_SURFACES) {
      if (wanted !== null && !wanted.has(label)) continue;
      this.mark(`SURFACE: ${label}`);
      if (opener === null) await this.dwell(dwellMs);
      else if (!(await this.clickButton(opener, { optional: true, timeoutMs: 8000, dwellMs }))) continue;
      seen.push(label);
      if (shots) await this.screenshot(`${prefix}${label}`);
      if (opener !== null) {
        if (closer) await this.clickButton(closer, { optional: true, timeoutMs: 5000, dwellMs: this.stepDwellMs });
        await this.dismissPopups();
      }
    }
    return seen;
  }

  /** On-screen rect of the tutorial WebView (`getTutorialRect`), or null. */
  async tutorialRect(): Promise<Dict | null> {
    return this.uiRead("() => { const b = window.__gameTestBridge; return b && b.getTutorialRect ? b.getTutorialRect() : null; }");
  }

  /** Wheel through the cross-origin tutorial WebView one viewport at a time (only a real wheel
   * reaches it, and its scroll position cannot be read back). Returns the wheels issued. */
  async scrollTutorial(opts: { steps?: number; fraction?: number; dwellMs?: number; up?: boolean } = {}): Promise<number> {
    const steps = opts.steps ?? 12;
    const rect = await this.tutorialRect();
    if (!rect) return 0;
    await this.page.mouse.move(rect.centerX as number, rect.centerY as number);
    const delta = Math.trunc((rect.height as number) * (opts.fraction ?? 0.9)) * (opts.up ? -1 : 1);
    for (let i = 0; i < steps; i++) {
      const t0 = this.nowS();
      await this.page.mouse.wheel(0, delta);
      await this.dwell(opts.dwellMs ?? 2000);
      this.step("scroll", `tutorial scroll ${opts.up ? "up" : "down"} ${i + 1}/${steps}`, t0, { delta, step: i + 1 });
    }
    return steps;
  }
}
