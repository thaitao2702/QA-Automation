/**
 * Scenario helpers used by more than one scenario group (port of the matching helpers of
 * scenario_engine.py). Kept apart from the scenario files so those can be ported independently.
 */
import { type Dict, get, py, pyRepr, pyRound, truthy } from "../py.js";
import { Bet, isFreespinTrigger } from "./bet.js";
import type { DriveContext } from "./drive.js";
import { playOutBonus } from "./flows.js";
import { GAME } from "./game.js";
import { requireButtons, Unreached } from "./result.js";

/** Try to open the denomination list; return whether it ACTUALLY opened. `HAS_BET_POPUP` records
 * what the doc says; this checks what the build does (they disagreed on Zeus). */
export async function openBetList(ctx: DriveContext, timeoutMs = 10000): Promise<boolean> {
  const opener = GAME.button("stake_up");
  if (!opener) return false;
  const before = new Set(await ctx.popups());
  await ctx.clickButton(opener, { optional: true, timeoutMs });
  const after = new Set(await ctx.popups());
  const label = GAME.popup("denominations");
  if (label && after.has(label)) return true;
  return [...after].some((x) => !before.has(x));     // a build may label it differently
}

/**
 * Close the denomination list and PROVE it closed. Click, then CHECK `visiblePopups` — ANY visible
 * popup counts (a label-only check answered "already closed" with the list on screen, a false green
 * on Chaos). If still up, fall back to `dismissPopups()` and report that it was needed.
 * facts: `closed` · `close_button_worked` · `needed_dismiss` · `popups_after`
 */
export async function closeBetList(ctx: DriveContext, timeoutMs = 8000): Promise<Dict> {
  const stillOpen = async () => (await ctx.popups()).length > 0;
  if (!(await stillOpen())) return { closed: true, close_button_worked: true, needed_dismiss: false, popups_after: [] };
  const clicked = await ctx.clickButton("bet_close", { optional: true, timeoutMs });
  const worked = clicked && !(await stillOpen());
  let neededDismiss = false;
  if (!worked) {
    ctx.mark("BET LIST: close control did not close it — falling back to dismissPopups()");
    await ctx.dismissPopups();
    neededDismiss = true;
  }
  const left = await ctx.popups();
  return { closed: !(await stillOpen()), close_button_worked: !!worked, needed_dismiss: neededDismiss, popups_after: left };
}

/** Observe one auto BASE spin; if it triggers a bonus, drain it (no settle — auto resumes on its
 * own). Appends every recorded round to `rounds`; returns the base bet. */
export async function autoNext(ctx: DriveContext, rounds: Bet[]): Promise<Bet> {
  const b = await ctx.observeBet();
  rounds.push(b);
  if (isFreespinTrigger(b)) {
    const fs = await playOutBonus(ctx, b, { settle: false });
    rounds.push(...fs.slice(1));
  }
  return b;
}

/** Clip time a recorded round's response ARRIVED: the end of its timeline entry. */
export function arrivedAt(ctx: DriveContext, b: Bet): number {
  for (let i = ctx.timeline.length - 1; i >= 0; i--) {
    const e = ctx.timeline[i];
    if (e.id === `${b.phase}-${b.seq}`) return e.end_ms / 1000;
  }
  return b.seq < ctx.roundStarts.length ? ctx.roundStarts[b.seq] : ctx.nowS();
}

/**
 * Buy the bonus: open the panel, pick a package, confirm, and return the purchase round. Returns
 * [bought, facts] (`bet` `packages` `popup_loaded` `package_picked` `price` `price_ratio`
 * `buy_clicked` `confirmed`). Raises `Unreached` if the build has no Buy Bonus, the panel never
 * opens, no package selects, or the purchase never fires.
 */
export async function purchaseBonus(ctx: DriveContext, pkg = 0): Promise<[Bet, Dict]> {
  const facts: Dict = {};
  if (!GAME.HAS_BUY_BONUS) throw new Unreached("this build has no Buy Bonus");
  const [btnBuyBonus, btnPackage, btnConfirm] = requireButtons("buy_bonus", ["buy_package", pkg], "buy_confirm");
  // Not required: a build whose package row raises the confirm sheet itself has no separate Buy.
  const btnBuy = GAME.button("buy_purchase");

  const betBefore = await ctx.uiRead("() => { const b = window.__gameTestBridge; return b && b.getBetAmount ? b.getBetAmount() : null; }");
  Object.assign(facts, { bet: betBefore });

  ctx.mark("BUY BONUS: open the popup");
  if (!(await ctx.clickButton(btnBuyBonus, { optional: true, timeoutMs: 15000 }))) {
    throw new Unreached(`\`${btnBuyBonus}\` never became clickable`);
  }
  const state: Dict = (await ctx.uiRead("() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {};"
    + " return { count: s.freeSpinPackageCount, price: s.buyBonusSelectedPrice,"
    + "          loaded: s.buyBonusPopupLoaded }; }", {})) || {};
  Object.assign(facts, { packages: get(state, "count"), popup_loaded: truthy(get(state, "loaded")) });

  ctx.mark(`BUY BONUS: select package ${pkg}`);
  // A row below the fold reports `clipped_by_mask` and never becomes clickable. No-op elsewhere.
  await ctx.uiRead(`() => { const b = window.__gameTestBridge; if (b && b.scrollBuyFeatureIntoView) b.scrollBuyFeatureIntoView(${JSON.stringify(btnPackage)}); return true; }`);
  const picked = await ctx.clickButton(btnPackage, { optional: true, timeoutMs: 8000 });
  const priced = await ctx.uiRead("() => { const b = window.__gameTestBridge; const s = b && b.getSnapshot ? b.getSnapshot() : {}; return s.buyBonusSelectedPrice; }");
  Object.assign(facts, {
    package_picked: picked, price: priced,
    price_ratio: truthy(priced) && truthy(betBefore) ? pyRound(Number(priced) / Number(betBefore), 4) : null,
    buy_clicked: false, confirmed: false,
  });
  // 0 = nothing selected: confirming now would click an inert control and wait out the timeout.
  if (!truthy(priced)) throw new Unreached(py`no package selected after clicking \`${btnPackage}\` (buyBonusSelectedPrice=${pyRepr(priced ?? null)})`);

  // TWO CLICKS: `buy` raises a confirmation modal, `confirm` inside it purchases.
  let boughtClicked = false;
  if (btnBuy) {
    ctx.mark(`BUY BONUS: click \`${btnBuy}\` (raises the confirmation modal)`);
    boughtClicked = await ctx.clickButton(btnBuy, { optional: true, timeoutMs: 10000, dwellMs: ctx.stepDwellMs });
  }
  ctx.mark(`BUY BONUS: confirm with \`${btnConfirm}\``);
  const confirmed = await ctx.clickButton(btnConfirm, { optional: true, timeoutMs: 8000, dwellMs: ctx.stepDwellMs });
  Object.assign(facts, { buy_clicked: boughtClicked, confirmed: boughtClicked || confirmed });
  if (!(boughtClicked || confirmed)) {
    throw new Unreached(py`\`${btnBuy}\` and \`${btnConfirm}\` resolve but neither became clickable (price ${pyRepr(priced ?? null)})`);
  }
  // A purchase is a click-caused round trip: `betTimeoutMs`, not the auto-spin budget.
  // (As in the Python, a refused purchase reply is reported as unreached here, not as an error.)
  let bought: Bet;
  try {
    bought = await ctx.observeBet(ctx.betTimeoutMs);
  } catch (e) {
    const why = await ctx.uiRead("() => { const b = window.__gameTestBridge; return b && b.canDo ? b.canDo('confirmBuyBonus') : null; }");
    const u = new Unreached(py`no bet within ${ctx.betTimeoutMs}ms after confirm -- the purchase never fired (canDo ${pyRepr(why ?? null)}, price ${pyRepr(priced ?? null)})`);
    (u as Error & { cause?: unknown }).cause = e;
    throw u;
  }
  return [bought, facts];
}

/** Controls a resumed bonus must keep locked, watched in the page from the first frame. */
export const BUTTON_WATCH_NAMES = ["stake_up", "stake_down", "auto", "settings", "buy_bonus"] as const;

export const BUTTON_WATCH_JS = `(names) => {
    const b = window.__gameTestBridge;
    window.__fsWatch = { polls: 0, enabled: {} };
    if (window.__fsWatchTimer) clearInterval(window.__fsWatchTimer);
    window.__fsWatchTimer = setInterval(() => {
        const w = window.__fsWatch; if (!w || !b || !b.getButtonRect) return;
        w.polls += 1;
        for (const n of names) {
            let r = null;
            try { r = b.getButtonRect(n); } catch (e) { r = null; }
            if (r && r.interactable && r.visible) w.enabled[n] = (w.enabled[n] || 0) + 1;
        }
    }, 50);
    return true;
}`;

export const BUTTON_WATCH_READ_JS = `() => {
    if (window.__fsWatchTimer) { clearInterval(window.__fsWatchTimer); window.__fsWatchTimer = null; }
    const w = window.__fsWatch || { polls: 0, enabled: {} };
    return { polls: w.polls, enabled: w.enabled };
}`;
