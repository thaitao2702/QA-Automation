/**
 * Bridge primitives (the split drive) — page-level, used by DriveContext and by the runner's boot.
 * Port of the bridge block of scenario_engine.py: `_await`, start/idle/spin/drain, the boot resume,
 * and the answers to what a reloaded session can be waiting on (Lucky Spin, Extension offer).
 *
 * THE BRIDGE REPORTS, THE FRAMEWORK DECIDES. Each step has exactly one authority; nothing here is
 * cross-checked against a second signal, because two judgements of one fact can disagree and the
 * tie-break would silently become the real contract.
 */
import type { Page } from "playwright";
import type { PendingResume } from "../profile/slot-game.js";
import { type Dict, get, py, pyEq, pyJson, pyRepr, pyRound, PyRuntimeError, truthy, tryFloat } from "../py.js";
import { Bet, isFreespinTrigger, isTerminal } from "./bet.js";
import { GAME } from "./game.js";
import { evaluate, TimeoutError, withTimeout } from "./page.js";

const AWAIT_JS = `async (t) => {
    const b = window.__gameTestBridge;
    try { return { ok: true, result: await __CALL__ }; }
    catch (e) {
        let reason = (e && e.reason) || (e && e.message) || String(e);
        let snap = null;
        try { snap = (e && e.snapshot) || (b.getSnapshot ? b.getSnapshot() : null); } catch (_) {}
        return { ok: false, error: reason, snapshot: snap };
    }
}`;

/** A snapshot read used ONLY to explain a failure -- never to decide one. */
const DIAG_JS = `() => { const b = window.__gameTestBridge;
    const s = b.getSnapshot ? b.getSnapshot() : {};
    return { app: s.appState, ready: !!s.sceneReady, popups: s.visiblePopups || [],
             busy: s.busyOps || [], fsMode: !!s.isFreeSpinMode,
             fsLeft: s.freeSpinsRemaining, autoLeft: s.autoSpinsRemaining }; }`;

export const GAME_INFO_JS = `() => { const b = window.__gameTestBridge;
    try { return b && b.getGameInfo ? b.getGameInfo() : null; } catch (e) { return null; } }`;

/** Which REQUIRED bridge functions this build is missing. Empty = the game can run the suite. */
export async function missingApi(page: Page): Promise<string[]> {
  return evaluate(page, GAME.apiCheckJs());
}

/** Await a bridge promise in the page (`b.<call>` with `t` = the timeout), surfacing a rejection
 * with its reason and a snapshot. */
export async function awaitBridge(page: Page, callExpr: string, timeoutMs: number, label: string): Promise<any> {
  let res: any;
  try {
    res = await withTimeout(evaluate(page, AWAIT_JS.replace("__CALL__", callExpr), timeoutMs), timeoutMs + 5000);
  } catch (e) {
    if (e instanceof TimeoutError) throw new PyRuntimeError(`${label} did not resolve within ${timeoutMs}ms`);
    throw new PyRuntimeError(`${label} evaluate failed: ${(e as Error).message}`);
  }
  if (!res || !res.ok) {
    const snap = res?.snapshot;
    const hint = snap ? `  snapshot: ${pyJson(snap).slice(0, 400)}` : "";
    throw new PyRuntimeError(`${label} rejected -> ${res?.error ?? null}  ${hint}`);
  }
  return res.result ?? null;
}

/** Snapshot rendered for an error message. Best-effort: diagnostics never mask the failure. */
export async function diag(page: Page): Promise<string> {
  let st: Dict;
  try {
    st = (await evaluate(page, DIAG_JS)) || {};
  } catch (e) {
    return `(snapshot unavailable: ${(e as Error).name})`;
  }
  return py`appState=${pyRepr(get(st, "app"))} sceneReady=${get(st, "ready")} popups=${get(st, "popups")} `
    + py`busy=${get(st, "busy")} freeSpinMode=${get(st, "fsMode")} `
    + py`freeSpinsRemaining=${get(st, "fsLeft")} autoSpinsRemaining=${get(st, "autoLeft")}`;
}

/** `getGameInfo()`, envelope peeled by the profile. `{}` when the build does not expose it. The
 * BRIDGE captured it at load, so it stays readable after Start. */
export async function readGameInfo(page: Page): Promise<Dict> {
  const info = GAME.gameInfo((await evaluate(page, GAME_INFO_JS)) || {});
  GAME.rememberLaunch(info);              // max-win settings for rounds parsed from here on
  return info;
}

function ladderNum(v: unknown): number | null {
  const f = tryFloat(v);
  return f === null ? null : pyRound(f, 6);
}

/** The bet ladder a load will run on: `settings.denominations` (or `settings.general.denominations`)
 * from the launch payload, as numbers. `[]` when the payload carries none. Readable BEFORE Start. */
export function launchLadder(info: Dict): number[] {
  const s = GAME.settings(info) || {};
  let d = get(s, "denominations");
  if (!Array.isArray(d)) {
    const general = get(s, "general");
    d = general && typeof general === "object" ? get(general, "denominations") : null;
  }
  return ((d as unknown[]) || []).map(ladderNum).filter((x): x is number => x !== null);
}

/** `startGame()`, whose contract is to resolve **the moment Start is clicked** — the single
 * authority for "has the game started". If it does not resolve, the bridge is wrong and the fix
 * belongs at the source (docs/game-bridge-contract.md §2). */
export async function clickStart(page: Page, timeoutMs = 60000): Promise<void> {
  try {
    await withTimeout(evaluate(page, "() => window.__gameTestBridge.startGame()"), timeoutMs);
  } catch (e) {
    if (e instanceof TimeoutError) {
      throw new PyRuntimeError(`startGame() did not resolve within ${timeoutMs}ms. Its contract is to resolve when `
        + "Start is CLICKED -- it must not wait for a clean idle or play out a resumed bonus "
        + `(docs/game-bridge-contract.md §2). Fix it at the source. Game state: ${await diag(page)}`);
    }
    throw new PyRuntimeError(`startGame() rejected: ${(e as Error).message}. Game state: ${await diag(page)}`);
  }
}

/** Assert the game is back to a clean, spinnable base. The authority is `waitForSpinClickable`,
 * and only that — its contract already covers no animation, no popup, not in a bonus. */
export async function assertIdle(page: Page, timeoutMs = 60000): Promise<void> {
  try {
    await waitSpinClickable(page, timeoutMs);
  } catch (e) {
    throw new PyRuntimeError(`game did not reach a clean, spinnable base state within ${timeoutMs}ms: ${(e as Error).message}`
      + `  Game state: ${await diag(page)}`);
  }
}

/**
 * Wait until a spin click would really send a bet. On a game with promo dialogs the wait runs in
 * slices and closes any promo dialog between them (`clearPromoPopups`): one can show at boot or
 * whenever the game returns to base (after free spins), and it blocks the spin button. This is
 * the one place that handles it, so every caller — boot, settle, the next spin — is covered.
 */
export async function waitSpinClickable(page: Page, timeoutMs: number): Promise<void> {
  if (!GAME.promoPopups().length) {
    await awaitBridge(page, "b.waitForSpinClickable(undefined, t)", timeoutMs, "waitForSpinClickable");
    return;
  }
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    await clearPromoPopups(page);
    const slice = Math.max(1, Math.min(PROMO_SLICE_MS, deadline - Date.now()));
    try {
      await awaitBridge(page, "b.waitForSpinClickable(undefined, t)", slice, "waitForSpinClickable");
      return;
    } catch (e) {
      if (Date.now() >= deadline) throw e;
    }
  }
}

// ── Promo dialogs ────────────────────────────────────────────────────────────────────
// The framework NEVER claims or discards a promo. `promo_confirm` is pressed only on a
// `promo_receive` dialog that shows no `promo_cancel` — the end-of-promo result, where confirm is
// the only way out (Grandma bridge-api.md §7).

const PROMO_SLICE_MS = 3000;

const POPUPS_JS = "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : null;"
  + " return (s && s.visiblePopups) || []; }";

const RECT_JS = "(n) => { const b = window.__gameTestBridge;"
  + " try { return b && b.getButtonRect ? b.getButtonRect(n) : null; } catch (e) { return null; } }";

/** Resolves when `visiblePopups` differs from `before`, or after ~3 s. */
const POPUPS_CHANGED_JS = `async (before) => {
    const b = window.__gameTestBridge;
    for (let i = 0; i < 60; i++) {
        const s = b && b.getSnapshot ? b.getSnapshot() : null;
        const now = JSON.stringify((s && s.visiblePopups) || []);
        if (now !== before) return true;
        await new Promise(r => setTimeout(r, 50));
    }
    return false;
}`;

const promoListeners = new WeakMap<Page, (msg: string) => void>();

/** Where `clearPromoPopups` reports what it closed on `page` (a DriveContext puts it on its
 * timeline). Without one it prints. */
export function onPromoAction(page: Page, fn: (msg: string) => void): void {
  promoListeners.set(page, fn);
}

/**
 * Close every promo dialog on screen without claiming or discarding anything. Closing one new
 * promo can bring up the next, so it loops until none is left. Returns what it did, one line each.
 * A no-op on a game without promo dialogs in its profile.
 */
export async function clearPromoPopups(page: Page): Promise<string[]> {
  const names = GAME.promoPopups();
  if (!names.length) return [];
  const done: string[] = [];
  for (let i = 0; i < 10; i++) {
    const up: string[] = ((await evaluate(page, POPUPS_JS)) || []).map(String);
    const dialog = up.find((n) => GAME.isPromoPopup(n));
    if (dialog === undefined) break;
    let control: string | null;
    let what: string;
    if (dialog === GAME.popup("promo_receive")) {
      const cancel = GAME.button("promo_cancel");
      // `cancel` resolves only while the dialog SHOWS it. When it does, it is the only button
      // pressed: `confirm` beside it would claim (or discard) the promo.
      if (cancel && (await evaluate(page, RECT_JS, cancel))) {
        control = cancel;
        what = "closed a promo dialog with cancel (not claimed)";
      } else {
        control = GAME.button("promo_confirm");
        what = "closed the end-of-promo result";
      }
    } else if (dialog === GAME.popup("promo_list")) {
      control = GAME.button("promo_list_close");
      what = "closed the promo list";
    } else {
      control = GAME.button("promo_message_ok");
      what = "closed a promo message";
    }
    const rect = control ? await evaluate(page, EXT_CLICKABLE_JS, control) : null;
    if (rect) {
      await page.mouse.click(rect.centerX, rect.centerY);
    } else {
      // dismissPopups() follows the same rule: cancel, else confirm on an end-of-promo result.
      await evaluate(page, "async () => { const b = window.__gameTestBridge; if (b && b.dismissPopups) await b.dismissPopups(); return true; }");
      what += " (via dismissPopups)";
    }
    const msg = `PROMO: ${what} [${dialog}]`;
    done.push(msg);
    const listener = promoListeners.get(page);
    if (listener) listener(msg);
    else console.log(`  ${msg}`);
    await evaluate(page, POPUPS_CHANGED_JS, JSON.stringify(up));
  }
  return done;
}

/**
 * Play out a promo the session is already IN (`pending.gameState == 4`): click spin until a round
 * leaves the promo state. Promo spins are clicked by the player, so nothing else will end it. A
 * free-spin bonus won inside the promo is drained on the way. Returns the raw responses.
 */
export async function playOutPromo(page: Page, timeoutMs: number, fsTimeoutMs: number, cap = 300): Promise<any[]> {
  const rounds: any[] = [];
  for (let i = 0; i < cap; i++) {
    await waitSpinClickable(page, timeoutMs);
    await clickSpin(page);
    const raw = await awaitBetResponse(page, timeoutMs);
    rounds.push(raw);
    let last = Bet.parse(raw, rounds.length - 1);
    if (isFreespinTrigger(last)) {
      const fs = await drainToBase(page, true, null, fsTimeoutMs, 500, false);
      rounds.push(...fs);
      if (fs.length) last = Bet.parse(fs[fs.length - 1], rounds.length - 1);
    }
    if (!pyEq(last.game_state, GAME.PROMO_STATE)) return rounds;
  }
  throw new PyRuntimeError(`promo play-out gave up after ${cap} spins with the session still in the promo state`);
}

export async function clickSpin(page: Page): Promise<void> {
  const rect = await evaluate(page, "() => window.__gameTestBridge.getButtonRect('spin')");
  if (!rect) throw new PyRuntimeError("getButtonRect('spin') returned null -- spin button unavailable");
  await page.mouse.click(rect.centerX, rect.centerY);
}

export async function awaitBetResponse(page: Page, timeoutMs: number): Promise<any> {
  return awaitBridge(page, "b.awaitBetResponse(t)", timeoutMs, "awaitBetResponse");
}

export async function awaitFreespinRound(page: Page, timeoutMs: number): Promise<any> {
  return awaitBridge(page, "b.awaitFreespinRound(t)", timeoutMs, "awaitFreespinRound");
}

/**
 * Play out an in-progress bonus if one is open, and optionally assert a clean base after.
 *
 * The one routine both entry points share — entering the game (`startGame`, evidence:
 * `gameInfo.pending`) and starting a run (`returnToBase`, evidence: the previous BET response).
 * `bonusOpen` is passed in rather than discovered, because **`awaitFreespinRound` must never be
 * called speculatively.** `onRound` is handed each raw response, so a caller can record them.
 */
export async function drainToBase(page: Page, bonusOpen: boolean, onRound: ((raw: any) => Promise<void>) | null = null,
  fsTimeoutMs = 60000, cap = 500, assertBase = true): Promise<any[]> {
  const rounds: any[] = [];
  if (bonusOpen) {
    let finished = false;
    for (let i = 0; i < cap; i++) {
      const raw = await awaitFreespinRound(page, fsTimeoutMs);
      rounds.push(raw);
      if (onRound) await onRound(raw);
      if (isTerminal(Bet.parse(raw, rounds.length - 1))) {
        finished = true;
        break;
      }
    }
    if (!finished) {
      // A bonus that outlives the budget (retriggers push the counter back up) must not be
      // reported as drained — that beats a waitForSpinClickable timeout naming the spin button.
      throw new PyRuntimeError(`bonus drain gave up after ${cap} rounds with free spins still owed `
        + `(${Bet.parse(rounds[rounds.length - 1], rounds.length - 1).free_spins} left on the counter)`);
    }
  }
  // ⚠ ANSWER THE OFFER BEFORE CHECKING FOR IDLE: a drained bonus can end on a Free Spin Extension
  // panel that locks the spin button, and no scenario is driving yet. No-op without the feature.
  if (rounds.length) await answerExtensionOffer(page, true);
  if (assertBase) await assertIdle(page, fsTimeoutMs);
  return rounds;
}

/** Answer the LUCKY SPIN wheel a reloaded session is parked on (`pending.gameState == 3`) by KEEPING
 * the multiplier it shows. Clicked, never dismissed: `dismissPopups()` leaves it open (Joker
 * bridge-api.md §12.5). Raises, naming the state, when the profile has no `lucky_keep` control. */
export async function answerLuckySpin(page: Page, timeoutMs = 30000): Promise<void> {
  const keep = GAME.button("lucky_keep");
  if (!keep) {
    throw new PyRuntimeError("this session is parked on the Lucky Spin wheel (gameInfo.pending.gameState == 3) and "
      + `profile '${GAME.name}' has no \`lucky_keep\` button to answer it. Add the role to the `
      + "profile, or clear the session, before running.");
  }
  const call = `b.waitForButtonClickable(${JSON.stringify(keep)}, undefined, ${Math.trunc(timeoutMs)})`;
  const rect = await awaitBridge(page, call, timeoutMs + 2000, `waitForButtonClickable(${keep})`);
  if (!rect) throw new PyRuntimeError(`waitForButtonClickable('${keep}') returned no rect`);
  await page.mouse.click(rect.centerX, rect.centerY);
}

export interface StartGameResult {
  game_info: Dict;
  game_info_raw: unknown;
  pending: Dict;
  pending_free_spins: number;
  pending_resume: PendingResume;
  resumed: any[];
  resumed_terminal: boolean | null;
}

/**
 * Enter the game and leave it in a clean, spinnable base state, resuming any owed bonus.
 *
 *   1. `startGame()` — "the game started" (resolves at the Start click)
 *   2. `getGameInfo()` — "does this session owe something", and WHAT (`pendingResume`)
 *   3. `awaitFreespinRound()` — each resumed round, until one is terminal
 *   4. `waitForSpinClickable()` — "back to a clean idle"
 *
 * Only an auto-playing bonus is drained; the Lucky Spin wheel and the Extension offer wait on a
 * click (server API doc §06 reload order). `onFreespin` is given each resumed round's RAW response.
 */
export async function startGame(page: Page, timeoutMs = 60000, onFreespin: ((raw: any) => Promise<void>) | null = null,
  fsTimeoutMs = 60000, cap = 500): Promise<StartGameResult> {
  await clickStart(page, timeoutMs);
  const infoRaw = (await evaluate(page, GAME_INFO_JS)) || {};
  const info = GAME.gameInfo(infoRaw);
  GAME.rememberLaunch(info);                               // max-win settings for this session
  const owed = GAME.pendingFreeSpins(info);
  const resume = GAME.pendingResume(info);
  const state = get(GAME.pending(info), GAME.F_PENDING_GAME_STATE ?? GAME.F_GAME_STATE);
  let bonusOpen = resume === "freespins";
  if (resume === "freespins") {
    console.log(py`  gameInfo.pending: ${owed} free spin(s) owed (gameState=${pyRepr(state)}) -- the client is auto-playing them; playing out from the BET responses before any scenario runs.`);
  } else if (resume === "lucky_spin") {
    console.log("  gameInfo.pending: a bought bonus is parked on the LUCKY SPIN wheel "
      + "(gameState=3) -- keeping the shown multiplier, then playing the bonus out.");
    await answerLuckySpin(page, timeoutMs);
    bonusOpen = true;                 // the wheel is answered: the bought spins play themselves
  } else if (resume === "extension") {
    console.log("  gameInfo.pending: the last free spin ended on the EXTENSION offer "
      + "(canBuyExtension) -- declining it before the idle check.");
    await answerExtensionOffer(page, true);
  } else if (resume === "promo") {
    console.log("  gameInfo.pending: a free-game PROMO is running (gameState=4) -- clicking its "
      + "spins until it ends, before any scenario runs.");
    const promo = await playOutPromo(page, timeoutMs, fsTimeoutMs);
    console.log(`  played out ${promo.length} promo round(s) at load`);
  }
  if (GAME.promoOffered(info)) {
    console.log("  gameInfo.needToStartPromo: the client will OFFER a promo. The framework never "
      + "accepts one; the idle check below closes its dialog with cancel.");
  }
  const resumed = await drainToBase(page, bonusOpen, onFreespin, fsTimeoutMs, cap);
  if (bonusOpen) console.log(`  played out ${resumed.length} resumed free-spin round(s) at load`);
  return {
    game_info: info, game_info_raw: infoRaw, pending: GAME.pending(info), pending_free_spins: owed,
    pending_resume: resume, resumed, resumed_terminal: resumed.length ? true : null,
  };
}

// ── Free Spin Extension offer ────────────────────────────────────────────────────────
// ⚠ NOT A FIXED WINDOW. The offer is raised only after the LAST free spin has finished PRESENTING
// (a drama + reel-glow last spin took ~18 s, then the TOTAL WIN count-up, then the offer). So: keep
// waiting while ANYTHING is animating (`busyOps`) or any popup is up, and conclude "no offer" only
// after `idle` ticks of the game genuinely at rest. `max` is the hard stop.
const EXT_WAIT_JS = `async (opts) => {
    const b = window.__gameTestBridge;
    if (!b) return null;
    const seen = () => {
        const s = b.getExtensionOfferState ? b.getExtensionOfferState() : null;
        if (s && s.open) return s;
        const snap = b.getSnapshot ? b.getSnapshot() : {};
        if (snap && snap.extensionOfferOpen) return Object.assign({}, s || {}, {open: true});
        const names = (snap && snap.visiblePopups) || [];
        if (names.some(n => String(n).indexOf('BuyExtraFreeSpin') >= 0))
            return Object.assign({}, s || {}, {open: true});
        return null;
    };
    let idle = 0;
    for (let i = 0; i < opts.max; i++) {
        const hit = seen(); if (hit) return hit;
        const snap = b.getSnapshot ? b.getSnapshot() : {};
        const busy = (snap && snap.busyOps) || [];
        const popups = ((snap && snap.visiblePopups) || [])
            .filter(n => String(n).indexOf('notify:') !== 0
                         && String(n).indexOf('FreeSpinResult') < 0
                         && (opts.ignore || []).indexOf(String(n)) < 0);
        idle = (busy.length || popups.length) ? 0 : idle + 1;
        if (idle >= opts.idle) return {};
        await new Promise(r => setTimeout(r, 50));
    }
    return {};
}`;

const EXT_CLICKABLE_JS = `async (name) => {
    const b = window.__gameTestBridge;
    if (!b || !b.getButtonRect) return null;
    for (let i = 0; i < 40; i++) {
        let r = null;
        try { r = b.getButtonRect(name); } catch (e) { r = null; }
        if (r && r.interactable && r.visible) return r;
        await new Promise(res => setTimeout(res, 50));
    }
    return null;
}`;

const EXT_GONE_JS = `async () => {
    const b = window.__gameTestBridge;
    for (let i = 0; i < 160; i++) {
        const s = b && b.getExtensionOfferState ? b.getExtensionOfferState() : null;
        const snap = b && b.getSnapshot ? b.getSnapshot() : {};
        const names = (snap && snap.visiblePopups) || [];
        const up = (s && s.open) || (snap && snap.extensionOfferOpen)
                   || names.some(n => String(n).indexOf('BuyExtraFreeSpin') >= 0);
        if (!up) return true;
        await new Promise(r => setTimeout(r, 50));
    }
    return false;
}`;

const EXT_REPLY_JS = `async () => {
    const b = window.__gameTestBridge;
    if (!b || !b.awaitExtensionResponse) return null;
    try { return await b.awaitExtensionResponse(4000); } catch (e) { return null; }
}`;

/**
 * Answer the FREE SPIN EXTENSION offer on `page`, and leave the screen clear behind it. Page-level
 * on purpose: the offer can be raised where no scenario is driving (a resumed bonus plays out inside
 * `startGame`). Waits for it (it fades in after the result popup), reads two signals, declines (the
 * popup HOLDS up to 5 s), collects the reply, then clears the result popup. Returns true when an
 * offer was answered. A no-op on every game without `extension_decline`.
 */
export async function answerExtensionOffer(page: Page, expect = true): Promise<boolean> {
  const decline = GAME.button("extension_decline");
  if (!decline) return false;
  const state: Dict = (await evaluate(page, EXT_WAIT_JS, { max: 1200, idle: expect ? 80 : 20, ignore: GAME.promoPopups() })) || {};
  let answered = false;
  if (truthy(get(state, "open"))) {
    for (const name of [decline, GAME.button("extension_close")]) {
      if (!name) continue;
      const rect = await evaluate(page, EXT_CLICKABLE_JS, name);
      if (rect) {
        await page.mouse.click(rect.centerX, rect.centerY);
        answered = true;
        break;
      }
    }
    if (!answered) {
      // Documented as a decline in its own right, not a dismissal.
      await evaluate(page, "async () => { const b = window.__gameTestBridge; if (b && b.dismissPopups) await b.dismissPopups(); return true; }");
      answered = true;
    }
    await evaluate(page, EXT_GONE_JS);
  }
  await evaluate(page, EXT_REPLY_JS);
  const skip = GAME.button("freespin_result_skip");
  if (skip) {
    const names: unknown[] = (await evaluate(page,
      "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {}; return (s && s.visiblePopups) || []; }")) || [];
    if (names.some((n) => String(n).toLowerCase().includes("freespinresult"))) {
      const rect = await evaluate(page, EXT_CLICKABLE_JS, skip);
      if (rect) await page.mouse.click(rect.centerX, rect.centerY);
    }
  }
  return answered;
}
