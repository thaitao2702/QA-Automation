/**
 * Shared flows a scenario composes: draining a bonus, settling to base, proving a stop, skipping a
 * big-win celebration, answering the Extension offer, switching a paid feature. Port of the shared
 * helpers of scenario_engine.py.
 */
import type { HoldSpinGame } from "../profile/mechanisms.js";
import { type Dict, get, py, pyEq, pyRepr, pyRound, PyRuntimeError, pyStr, tryFloat, truthy } from "../py.js";
import { Bet, BetRejected, isFreespinTrigger, isTerminal } from "./bet.js";
import { answerExtensionOffer, awaitBetResponse, awaitBridge, readGameInfo, waitSpinClickable, launchLadder } from "./bridge.js";
import type { DriveContext } from "./drive.js";
import { GAME } from "./game.js";
import { evaluate, sleep } from "./page.js";

export const SPEED_JS = "() => { const b = window.__gameTestBridge; return b && b.getSpeedMode ? b.getSpeedMode() : 'UNKNOWN'; }";
export const AUTO_JS = "() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
  + " return { on: !!s.isAutoPlay, left: s.autoSpinsRemaining }; }";

/** Ordered labels of the `popup_shown` events in a bridge capture window. Diagnostic only. */
export function popupLabels(events: Dict[]): string[] {
  const out: string[] = [];
  for (const e of events ?? []) {
    if (get(e, "type") === "popup_shown") {
      const d = (get(e, "data") || {}) as Dict;
      out.push(pyStr(get(d, "label") || get(d, "name") || get(d, "popup") || "?"));
    }
  }
  return out;
}

/** Load the game WITHOUT Start until its launch payload carries the bet ladder `want` — evidence
 * instead of a timer, because a BO denomination change reaches the game server over Kafka after a
 * varying delay. Leaves the game loaded and NOT started. Returns [ready, seenLadder, loads]. */
export async function loadUntilLadder(ctx: DriveContext, want: number[], timeoutS: number, pollS: number): Promise<[boolean, number[], number]> {
  let seen: number[] = [];
  let loads = 0;
  const t0 = ctx.nowS();
  const same = (a: number[], b: number[]) => JSON.stringify([...a].sort((x, y) => x - y)) === JSON.stringify([...b].sort((x, y) => x - y));
  for (;;) {
    loads += 1;
    await ctx.loadGame();
    seen = launchLadder(await readGameInfo(ctx.page));
    if (!want.length || same(seen, want)) break;
    if (ctx.nowS() - t0 >= timeoutS) break;
    console.log(py`  ladder   : load ${loads} still serves ${seen} (want ${want}) -- retrying in ${pollS}s`);
    await sleep(pollS * 1000);
  }
  const ready = want.length > 0 && same(seen, want);
  console.log(py`  ladder   : launch payload ${seen} after ${loads} load(s), ${pyRound(ctx.nowS() - t0, 1)}s -- ${ready ? "new ladder" : "NEVER the new ladder"}`);
  return [ready, seen, loads];
}

/**
 * Prove the game STOPPED, and leave footage of it stopped. "It stopped" is a NEGATIVE claim, and a
 * negative needs time on the clock: WATCH for another bet for `watchMs` (nothing arriving is the
 * evidence), POLL the client's auto flag for up to `flagGraceMs` (it clears only after the stopping
 * round has presented), then HOLD for `dwellMs` so the still screen is recorded.
 */
export async function confirmStopped(ctx: DriveContext, opts: { watchMs?: number; dwellMs?: number; label?: string; flagGraceMs?: number } = {}): Promise<Dict> {
  const watchMs = opts.watchMs ?? 6000;
  const flagGraceMs = opts.flagGraceMs ?? 5000;
  const t0 = ctx.nowS();
  let extra: boolean;
  try {
    const raw = await awaitBetResponse(ctx.page, watchMs);
    extra = true;                                  // something bet again -> it did NOT stop
    ctx.record(Bet.parse(raw, ctx.events.length), "auto", t0);
  } catch (e) {
    if (e instanceof BetRejected) throw e;         // a bet WAS sent (and refused): never "stopped"
    extra = false;                                 // nothing bet in the window = stopped
  }
  let st: Dict = (await ctx.uiRead(AUTO_JS, {})) || {};
  let waited = 0;
  while (truthy(get(st, "on")) && waited < flagGraceMs / 1000) {
    await sleep(500);
    waited += 0.5;
    st = (await ctx.uiRead(AUTO_JS, {})) || {};
  }
  ctx.mark(`${opts.label ?? "STOPPED"} -- watching the game`);
  await ctx.dwell(opts.dwellMs ?? 3000);
  return {
    stopped: !extra && !truthy(get(st, "on")),
    extra_round_after_stop: extra,
    auto_flag_after: truthy(get(st, "on")),
    auto_flag_cleared_s: pyRound(waited, 1),
    auto_spins_left_after: get(st, "left"),
    stop_watch_s: pyRound(watchMs / 1000, 1),
  };
}

/** The control that STOPS a running auto-spin run, in this build's dialect: a dedicated main-screen
 * stop, then the control replacing spin during a run, then `auto`, then the popup's stop. */
export function autoStopButton(): string | null {
  return GAME.button("auto_stop_main") || GAME.button("auto_or_freespin") || GAME.button("auto") || GAME.button("auto_stop");
}

const BIG_WIN_POPUP_JS = `async (opts) => {
    const b = window.__gameTestBridge;
    if (!b || !b.getSnapshot) return "";
    for (let i = 0; i < opts.ticks; i++) {
        const s = b.getSnapshot() || {};
        const names = (s.visiblePopups || []).filter(n => (opts.ignore || []).indexOf(String(n)) < 0)
            .map(n => String(n).toLowerCase());
        if (names.some(n => n.indexOf('bigwin') >= 0)) return "popup";
        const busy = s.busyOps || [];
        if (busy.indexOf('bigWin') >= 0) return "busy";
        if (i > opts.grace && busy.length === 0 && names.length === 0) return "";
        await new Promise(r => setTimeout(r, 50));
    }
    return "";
}`;

const BIG_WIN_CONTROLS_JS = `() => {
    const b = window.__gameTestBridge;
    if (!b || !b.getAllButtonRects) return [];
    let all = {};
    try { all = b.getAllButtonRects() || {}; } catch (e) { return []; }
    const out = [];
    for (const name of Object.keys(all)) {
        const r = all[name];
        if (!r || !r.interactable || !r.visible) continue;
        const k = name.toLowerCase();
        const rank = k.indexOf('bigwin') >= 0 ? 0 : (k.indexOf('skip') >= 0 ? 1 : -1);
        if (rank >= 0) out.push({ name: name, rank: rank, x: r.centerX, y: r.centerY });
    }
    out.sort((a, c) => a.rank - c.rank);
    return out;
}`;

/** The payout/bet multiple at which this game starts a big-win celebration — read once from the
 * launch payload and cached on the context. null when the build reports no win bands. */
export async function bigWinThreshold(ctx: DriveContext): Promise<number | null> {
  if (ctx.bigWinThresholdCache === undefined) {
    let info: Dict;
    try {
      info = await readGameInfo(ctx.page);
    } catch {
      info = {};
    }
    ctx.bigWinThresholdCache = GAME.bigWinThreshold(info || {});
    if (ctx.bigWinThresholdCache === null) {
      console.log("  big win  : NO win bands in gameInfo (settings.winFormula / settings.general."
        + "winFormula) -- celebrations cannot be detected, so none will be skipped");
    } else {
      console.log(py`  big win  : celebrations start at ${ctx.bigWinThresholdCache}x the bet (from gameInfo winFormula)`);
    }
  }
  return ctx.bigWinThresholdCache;
}

/**
 * Cut short the big-win celebration this round is about to play. ⚠ WHY: TIME — minutes per run of
 * count-up nobody grades. It asks the RESPONSE first (`payout / baseStake >= threshold`), waits for
 * the banner or the end of the presentation, then TWO taps a second apart (cut the count-up, then
 * dismiss). Called from the round primitives, so no scenario can forget it. It NEVER fails the run.
 */
export async function skipBigWin(ctx: DriveContext, bet: Bet | null, opts: { waitMs?: number; secondTapMs?: number; graceMs?: number } = {}): Promise<boolean> {
  const waitMs = opts.waitMs ?? 20000;
  const secondTapMs = opts.secondTapMs ?? 1000;
  const graceMs = opts.graceMs ?? 1500;
  if (bet === null || !ctx.skipBigWins) return false;
  const btn = GAME.button("big_win_skip");
  if (!btn) return false;
  const threshold = await bigWinThreshold(ctx);
  const raw = (bet.raw || {}) as Dict;
  const base = GAME.baseStake(raw);
  if (threshold === null) return false;            // already reported once
  if (!GAME.isBigWin(raw, threshold)) return false;

  const seen = await evaluate(ctx.page, BIG_WIN_POPUP_JS, { ticks: Math.max(1, Math.trunc(waitMs / 50)), grace: Math.max(1, Math.trunc(graceMs / 50)), ignore: GAME.promoPopups() });
  if (!seen) {
    ctx.mark(py`BIG WIN: ${bet.payout} qualified but no celebration appeared in ${waitMs}ms`);
    console.log(py`  big win  : ${bet.payout} is ${pyRound(bet.payout / Math.max(base, 1e-9), 1)}x -- qualified, but no popup or busy label appeared in ${waitMs}ms, so there was nothing to skip`);
    return false;
  }
  ctx.mark(py`BIG WIN: skipping the celebration (${bet.payout} on ${base}, seen as ${seen})`);
  let tap: string | null = null;
  // ⚠ NOT clickButton(optional): the rejection reason IS the diagnosis (unknown_control,
  // not_available, not_interactable, offscreen, blocked_by, nothing_hit / no_geometry).
  let why: string | null = null;
  let rect: any = null;
  try {
    rect = await awaitBridge(ctx.page, `b.waitForButtonClickable(${JSON.stringify(btn)}, undefined, 3000)`, 5000, `waitForButtonClickable(${btn})`);
  } catch (e) {
    rect = null;
    why = (e as Error).message;
  }
  if (rect) {
    await ctx.page.mouse.click(rect.centerX, rect.centerY);
    tap = btn;
  } else {
    let can: Dict = {};
    try {
      can = (await evaluate(ctx.page, "(n) => { const b = window.__gameTestBridge; return b && b.canDo ? b.canDo(n) : null; }", btn)) || {};
    } catch {
      can = {};
    }
    console.log(`  big win  : \`${btn}\` did not become clickable -- reason: ${why || "none given"}`);
    if (truthy(can)) console.log(py`             canDo(${pyRepr(btn)}) -> ${can}`);
    const found: Dict[] = (await evaluate(ctx.page, BIG_WIN_CONTROLS_JS)) || [];
    if (!found.length) {
      const state: Dict = (await ctx.uiRead(
        "(n) => { const b = window.__gameTestBridge;"
        + " const s = b && b.getSnapshot ? b.getSnapshot() : {};"
        + " let rects = {};"
        + " try { rects = (b && b.getAllButtonRects) ? (b.getAllButtonRects() || {}) : {}; }"
        + " catch (e) {}"
        + " const live = Object.keys(rects).filter(n => rects[n]"
        + "   && rects[n].interactable && rects[n].visible);"
        + " let rect = null;"
        + " try { rect = (b && b.getButtonRect) ? b.getButtonRect(n) : null; } catch (e) {}"
        + " const c = document.querySelector('canvas');"
        + " const cr = c ? c.getBoundingClientRect() : null;"
        + " return { live: live, all: Object.keys(rects), rect: rect,"
        + "          canvas: cr ? { width: cr.width, height: cr.height } : null,"
        + "          popups: s.visiblePopups || [], busy: s.busyOps || [] }; }", {}, btn)) || {};
      ctx.mark(py`BIG WIN: \`${btn}\` did not resolve; controls up = ${get(state, "live")}`);
      console.log(`  big win  : \`${btn}\` never became clickable while the celebration was up.`);
      console.log(py`             popups now : ${get(state, "popups")}`);
      console.log(py`             busy now   : ${get(state, "busy")}`);
      console.log(py`             CLICKABLE  : ${get(state, "live")}`);
      console.log(py`             resolving  : ${((get(state, "all") || []) as unknown[]).slice(0, 25)}`);
      console.log(py`             rect(${btn}) : ${get(state, "rect")}`);
      console.log(py`             canvas     : ${get(state, "canvas")}`);
      console.log(`             ^ if one of those dismisses the banner, set it as \`big_win_skip\` in the ${GAME.name} profile. `
        + "If the list is empty, the celebration is not skippable through the bridge -- a gap to report.");
      return false;
    }
    tap = pyStr(found[0].name);
    console.log(py`  big win  : \`${btn}\` did not resolve -- using \`${tap}\` instead (candidates: ${found.map((f) => f.name)}). ⚠ Fix \`big_win_skip\` in the profile`);
    await ctx.page.mouse.click(found[0].x as number, found[0].y as number);
    await ctx.dwell(secondTapMs);
    await ctx.page.mouse.click(found[0].x as number, found[0].y as number);
    console.log(py`  big win  : skipped via \`${tap}\` (${bet.payout} on ${base})`);
    return true;
  }
  await ctx.dwell(secondTapMs);
  if (rect) await ctx.page.mouse.click(rect.centerX, rect.centerY);   // the second tap dismisses
  console.log(py`  big win  : skipped via \`${tap}\` (${bet.payout} on ${base} = ${pyRound(bet.payout / Math.max(base, 1e-9), 1)}x)`);
  return true;
}

/** `answerExtensionOffer` for a scenario: works out from the terminal round whether to expect an
 * offer, and marks the timeline either way ("none was raised" vs "we looked too early"). */
export async function declineExtensionOffer(ctx: DriveContext, terminal: Bet | null = null): Promise<boolean> {
  if (!GAME.button("extension_decline")) return false;
  let expect = true;
  if (terminal !== null) {
    const raw = (terminal.raw || {}) as Dict;
    const windowOpen = terminal.free_spins === 0 && pyEq(GAME.gameState(raw), GAME.FREESPIN_STATE);
    expect = windowOpen || truthy(get(raw, "canBuyExtension"));
  }
  const answered = await answerExtensionOffer(ctx.page, expect);
  ctx.mark(answered ? "EXTENSION OFFER: declined" : "EXTENSION OFFER: none was raised");
  return answered;
}

/**
 * Wait for a triggered free-spin bonus to FINISH: consume every auto-played round until the bonus
 * ends (`isTerminal` — both halves, since hasReachedMaxWin can truncate it), answer an Extension
 * offer, then settle. Returns [trigger, ...free spins..., terminal]. Never calls
 * waitForSpinClickable while the bonus runs. ⚠ Running out of `cap` RAISES, naming the retriggers.
 */
export async function playOutFreespins(ctx: DriveContext, trigger: Bet, opts: { cap?: number; settle?: boolean; onRound?: ((b: Bet) => Promise<void>) | null } = {}): Promise<Bet[]> {
  const cap = opts.cap ?? 500;
  const rounds = [trigger];
  let retriggers = 0;
  let prevLeft: number | null = null;
  let finished = false;
  for (let i = 0; i < cap; i++) {
    const b = await ctx.freeSpin();
    rounds.push(b);
    if (opts.onRound) await opts.onRound(b);
    if (prevLeft !== null && b.free_spins > prevLeft) retriggers += 1;
    prevLeft = b.free_spins;
    if (isTerminal(b)) {
      finished = true;
      break;
    }
  }
  if (!finished) {
    throw new PyRuntimeError(`free-spin drain gave up after ${cap} rounds with the bonus still running `
      + `(${retriggers} retrigger(s) seen; ${rounds[rounds.length - 1].free_spins} spin(s) still on the counter). `
      + "Raise `cap` if the paytable legitimately awards this many, or make the award smaller.");
  }
  await declineExtensionOffer(ctx, rounds.length ? rounds[rounds.length - 1] : null);
  if (opts.settle ?? true) await ctx.settle();
  return rounds;
}

/** One base spin, played through to a clean base. Returns [spin, rounds]. Any base spin can trigger
 * free spins, so a scenario is finished only when the game is back at a spinnable base. */
export async function spinAndFinish(ctx: DriveContext): Promise<[Bet, Bet[]]> {
  const b = await ctx.baseSpin();
  return [b, await finishRound(ctx, b)];
}

/** Take an ALREADY-OBSERVED round to a clean base; returns the chain it produced. */
export async function finishRound(ctx: DriveContext, b: Bet): Promise<Bet[]> {
  if (isFreespinTrigger(b)) return playOutBonus(ctx, b);
  await ctx.settle();
  return [b];
}

/** Paid BASE rounds recorded since event index `before` — "one input started exactly one spin". */
export function baseRoundsSince(ctx: DriveContext, before: number): number {
  return ctx.events.slice(before).filter((e) => get(e, "type") === "spin_result" && get(get(e, "data") || {}, "phase") === "base").length;
}

/**
 * Take a FINISHED feature all the way back to a spinnable base game, PROVE it got there, and leave
 * footage either way. The end of a feature is a PRESENTATION (21 s measured on a Zeus bought bonus),
 * so the budget is the spin budget and the loop clears celebrations that appear meanwhile. WATCH for
 * a further bet, then HOLD — on the failure path too, because the stuck screen IS the evidence.
 */
export async function settleToBase(ctx: DriveContext, opts: { label?: string; dwellMs?: number; timeoutMs?: number | null; watchMs?: number; holdMs?: number; sliceMs?: number } = {}): Promise<Dict> {
  const label = opts.label ?? "";
  const watchMs = opts.watchMs ?? 5000;
  const holdMs = opts.holdMs ?? 3000;
  const sliceMs = opts.sliceMs ?? 5000;
  const tag = label ? `settle-to-base [${label}]` : "settle-to-base";
  const budget = opts.timeoutMs ?? ctx.spinTimeoutMs;
  const t0 = ctx.nowS();
  const shown = [...(await ctx.popups())];
  if (shown.length) ctx.mark(`FEATURE OVER: celebration on screen -- ${shown.join(", ")}`);
  await ctx.dwell(opts.dwellMs ?? 2000);
  let reached = false;
  let dismissed = false;
  const seen = [...shown];
  const deadline = ctx.nowS() + budget / 1000;
  for (;;) {
    try {
      await waitSpinClickable(ctx.page, sliceMs);
      reached = true;
      break;
    } catch {
      const pops = [...(await ctx.popups())];
      for (const l of pops) if (!seen.includes(l)) seen.push(l);
      if (pops.length) {
        ctx.mark(`CELEBRATION: clearing ${pops.join(", ")}`);
        await ctx.dismissPopups();
        dismissed = true;
      }
      if (ctx.nowS() >= deadline) break;
    }
  }
  const readyS = pyRound(ctx.nowS() - t0, 2);
  const left = [...(await ctx.popups())];
  let extra = false;
  if (reached) {
    try {
      const raw = await awaitBetResponse(ctx.page, watchMs);
      extra = true;
      ctx.record(Bet.parse(raw, ctx.events.length), "base", ctx.nowS());
    } catch (e) {
      if (e instanceof BetRejected) throw e;
      extra = false;
    }
    ctx.mark("BACK AT BASE -- watching the game");
  } else {
    console.log(py`  !! ${tag}: the game was still not spinnable after ${readyS}s` + (left.length ? `; popup(s) up: ${left.join(", ")}` : " (screen clear)"));
    ctx.mark(py`DID NOT RETURN TO BASE after ${readyS}s` + (left.length ? ` -- ${left.join(", ")} still up` : " -- screen clear, still busy"));
  }
  await ctx.dwell(holdMs);
  return {
    returned_to_base: reached && !extra,
    celebration_shown: seen.length > 0,
    celebration_auto_closed: seen.length > 0 && !dismissed,
    popups_at_end: left.join(",") || "none",
    base_ready_seconds: readyS,
    bet_after_return: extra,
    base_watch_s: pyRound(watchMs / 1000, 1),
    base_hold_s: pyRound(holdMs / 1000, 1),
  };
}

/**
 * Drain a triggered Hold & Spin to its END. Returns [rounds, capped]. The end is `isTerminal` plus
 * this mechanism's own `gridFull`; `hasReachedMaxWin` ends the round wherever it lands (reported as
 * `capped`); running out of `cap` RAISES. A full grid on the trigger fires no respin at all.
 */
export async function playOutHoldspin(ctx: DriveContext, trigger: Bet, opts: { cap?: number; mark?: boolean; onRound?: ((b: Bet) => Promise<void>) | null } = {}): Promise<[Bet[], boolean]> {
  const g = GAME as unknown as HoldSpinGame;
  const cap = opts.cap ?? 60;
  const mark = opts.mark ?? true;
  const raw0 = (trigger.raw || {}) as Dict;
  const rounds = [trigger];
  let capped = trigger.has_max_win;
  if (g.gridFull(raw0) || capped) return [rounds, capped];
  for (let i = 0; i < cap; i++) {
    const nxt = await ctx.freeSpin();
    rounds.push(nxt);
    if (opts.onRound) await opts.onRound(nxt);
    const raw = (nxt.raw || {}) as Dict;
    if (mark) ctx.mark(`RESPIN ${rounds.length - 1}: ${g.coinCount(raw)}/${g.GRID_SLOTS} coin(s) held, ${nxt.free_spins} respin(s) left`);
    if (nxt.has_max_win) {
      capped = true;
      ctx.mark("MAX WIN reached -- the feature ends here (Rule 9), respins forfeited");
      return [rounds, capped];
    }
    if (isTerminal(nxt) || g.gridFull(raw)) return [rounds, capped];
  }
  const last = rounds[rounds.length - 1];
  throw new PyRuntimeError(`Hold & Spin drain gave up after ${cap} respin(s) with the feature still running `
    + `(${g.coinCount((last.raw || {}) as Dict)}/${g.GRID_SLOTS} coins, ${last.free_spins} respin(s) still on the counter). `
    + "Every new coin resets the counter, so a paytable that keeps landing them can outlast any cap -- lower the coin "
    + "weight (Rule 6) rather than raising this.");
}

/** Drain WHATEVER BONUS THIS GAME HAS, chosen by mechanism — the "I just need it over" door for
 * Rule 1 ("any spin can trigger a bonus"). A scenario that GRADES a Hold & Spin calls
 * `playOutHoldspin` itself. */
export async function playOutBonus(ctx: DriveContext, trigger: Bet, opts: { cap?: number | null; settle?: boolean; onRound?: ((b: Bet) => Promise<void>) | null } = {}): Promise<Bet[]> {
  const settle = opts.settle ?? true;
  if (GAME.mechanism === "hold-spin") {
    const [rounds] = await playOutHoldspin(ctx, trigger, { cap: opts.cap ?? 60, onRound: opts.onRound ?? null });
    if (settle) await ctx.settle();
    return rounds;
  }
  return playOutFreespins(ctx, trigger, { cap: opts.cap ?? 500, settle, onRound: opts.onRound ?? null });
}

// ── Paid spin features — Ante Bet and Premium Reel ───────────────────────────────────
// Both multiply what a spin COSTS and leave the payout base alone. They differ in how a player
// reaches them (a one-click `ante` toggle on Chaos; Dragon VIP's Buy Bonus panel), and
// `switchFeature` is the one place that difference lives.
const FEATURE_STATE_JS = "() => { const b = window.__gameTestBridge; if (!b || !b.getSnapshot) return {};"
  + " const s = b.getSnapshot();"
  + " return { ante: s.isAnteBet === undefined ? null : !!s.isAnteBet,"
  + "          premium: s.isPremiumReel === undefined ? null : !!s.isPremiumReel,"
  + "          premiumId: s.premiumReelId, premiumMultiplier: s.premiumReelMultiplier,"
  + "          anteMultiplier: s.anteMultiplier, anteAvailable: s.anteBetAvailable,"
  + "          spinCost: s.spinCost, bet: s.betAmount }; }";

/** The client's own account of its paid features. UI evidence only. */
export async function featureState(ctx: DriveContext): Promise<Dict> {
  return (await ctx.uiRead(FEATURE_STATE_JS, {})) || {};
}

/** Whether `state` shows `feature` ("ante" | "premium:<reelId>") on. null when the bridge does not
 * publish the flag, so "unknown" never reads as "off". */
export function featureOn(state: Dict, feature: string): boolean | null {
  if (feature === "ante") return (get(state, "ante") as boolean | null);
  if (get(state, "premium") === null) return null;
  return truthy(get(state, "premium")) && pyStr(get(state, "premiumId")) === feature.split(":").slice(1).join(":");
}

/** Was this ROUND bet with `feature`? From the response, never from the client. */
export function roundCarries(b: Bet, feature: string): boolean {
  const raw = (b.raw || {}) as Dict;
  if (feature === "ante") return GAME.isAnteRound(raw);
  return String(GAME.premiumReelId(raw)) === feature.split(":").slice(1).join(":");
}

/** Money equality to the cent. null on either side is never equal. */
export function near(a: unknown, b: unknown, tol = 0.005): boolean {
  if (a === null || a === undefined || b === null || b === undefined) return false;
  const x = tryFloat(a);
  const y = tryFloat(b);
  return x !== null && y !== null && Math.abs(x - y) <= tol;
}

/**
 * Switch a paid spin feature ON or OFF: "ante", or "premium:<reelId>". SWITCHES, never
 * blind-toggles: a feature already in the wanted state is left alone. Samples `visiblePopups` for
 * ~2.5 s after the last click so a feature toast is caught. Returns {switched, clicked, popups}.
 */
export async function switchFeature(ctx: DriveContext, feature: string, on: boolean, opts: { optional?: boolean; dwellMs?: number | null } = {}): Promise<Dict> {
  const optional = opts.optional ?? true;
  const before = await featureState(ctx);
  if (featureOn(before, feature) === on) return { switched: true, clicked: false, popups: [] };
  const premiumId = feature === "ante" ? null : feature.split(":").slice(1).join(":");
  let clicked = false;
  if (feature === "ante" && GAME.button("ante")) {
    ctx.mark(`ANTE: toggle ${on ? "ON" : "OFF"}`);
    clicked = await ctx.clickButton("ante", { optional, dwellMs: 0 });
  } else if (on) {
    const row = premiumId === null ? GAME.button("ante_on") : GAME.button("premium_reel", { i: premiumId });
    if (row && GAME.button("buy_bonus") && GAME.button("buy_confirm")) {
      ctx.mark(`${feature.toUpperCase()}: open the feature panel`);
      if (await ctx.clickButton("buy_bonus", { optional: true, timeoutMs: 15000, dwellMs: ctx.stepDwellMs })) {
        // A row below the fold reports `clipped_by_mask` and never becomes clickable.
        await ctx.uiRead(`() => { const b = window.__gameTestBridge; if (b && b.scrollBuyFeatureIntoView) b.scrollBuyFeatureIntoView(${JSON.stringify(row)}); return true; }`);
        ctx.mark(`${feature.toUpperCase()}: ACTIVATE row \`${row}\``);
        if (await ctx.clickButton(row, { optional: true, timeoutMs: 8000, dwellMs: ctx.stepDwellMs })) {
          ctx.mark(`${feature.toUpperCase()}: confirm`);
          clicked = await ctx.clickButton("buy_confirm", { optional: true, timeoutMs: 8000, dwellMs: 0 });
        }
      }
      if (!clicked) await ctx.dismissPopups();     // never leave a half-used panel over the reels
    }
  } else {
    const off = premiumId === null ? GAME.button("ante_off") : GAME.button("premium_off");
    if (off) {
      ctx.mark(`${feature.toUpperCase()}: switch OFF`);
      clicked = await ctx.clickButton(off, { optional, timeoutMs: 8000, dwellMs: 0 });
    }
  }
  const seen: string[] = [];
  if (clicked) {
    for (let i = 0; i < 10; i++) {
      for (const l of await ctx.popups()) if (!seen.includes(l)) seen.push(l);
      await sleep(250);
    }
    await ctx.dwell(opts.dwellMs ?? null);
  }
  return { switched: clicked ? featureOn(await featureState(ctx), feature) === on : false, clicked, popups: seen };
}

/** Does this session offer Ante Bet at all? A toggle build by the control's presence; a panel build
 * by the snapshot (its ACTIVATE row only resolves while the panel is open). */
export async function anteOffered(ctx: DriveContext): Promise<boolean> {
  if (GAME.button("ante")) return ctx.hasButton("ante");
  if (!GAME.button("ante_on")) return false;
  return truthy(get(await featureState(ctx), "anteAvailable"));
}

/** A one-shot flag a watcher loop can wait on (Python's asyncio.Event). */
export class StopSignal {
  private set_ = false;
  private waiters: Array<() => void> = [];

  set(): void {
    this.set_ = true;
    for (const w of this.waiters.splice(0)) w();
  }

  isSet(): boolean {
    return this.set_;
  }

  /** Resolves when set, or after `ms`. */
  wait(ms: number): Promise<void> {
    if (this.set_) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(resolve, ms);
      this.waiters.push(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

/** Probe `roles` with instant `hasButton` reads every 0.5 s until `stop` is set. Returns
 * {role: true if it was EVER available} plus `samples` — for "this control stays locked for the
 * whole feature", which one probe at the trigger cannot prove. */
export async function watchControls(ctx: DriveContext, roles: string[], stop: StopSignal): Promise<Dict> {
  const seen: Dict = Object.fromEntries(roles.map((r) => [r, false]));
  let samples = 0;
  while (!stop.isSet()) {
    for (const r of roles) if (!seen[r] && (await ctx.hasButton(r))) seen[r] = true;
    samples += 1;
    await stop.wait(500);
  }
  return { ...seen, samples };
}
