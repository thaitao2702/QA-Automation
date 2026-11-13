/**
 * Teardown library — optional, NAMED cleanup steps a suite opts into (`teardown:` on a run, or once
 * at suite level), plus the framework's own between-runs repair (`ensureBase`). Response-driven: a
 * teardown reads ONLY the last server response (`ctx.lastBet`), never a per-game client getter.
 */
import { get, PyRuntimeError, truthy } from "../py.js";
import { Bet, isTerminal } from "./bet.js";
import { drainToBase } from "./bridge.js";
import type { DriveContext } from "./drive.js";
import { AUTO_JS, autoStopButton } from "./flows.js";
import { evaluate } from "./page.js";

/** Is there still a page with a bridge on it? A scenario may legitimately end with the client torn
 * down; driving that anyway turns a scenario error into a misleading teardown error that aborts the
 * suite. */
export async function bridgeIsLive(ctx: DriveContext): Promise<boolean> {
  if (!ctx.page) return false;
  try {
    return !!(await evaluate(ctx.page, "() => !!window.__gameTestBridge"));
  } catch {
    return false;                 // a closed page/context raises; that IS the answer
  }
}

/** Return the game to a normal, spinnable BASE so the next run starts clean. The authority between
 * runs is the last BET response this session recorded. Rounds are recorded under phase `teardown`. */
export async function returnToBase(ctx: DriveContext, cap = 500, assertBase = true): Promise<void> {
  if (!(await bridgeIsLive(ctx))) {
    console.log("  return-to-base: no live game to return (the client was torn down and not "
      + "reopened) -- nothing to do");
    return;
  }
  await drainToBase(ctx.page, !isTerminal(ctx.lastBet), async (raw) => {
    const t0 = ctx.nowS();
    ctx.record(Bet.parse(raw, ctx.events.length), "teardown", t0);
  }, ctx.fsTimeoutMs, cap, assertBase);
}

/** Stop a running auto-play. Returns whether one WAS running. Repair, evidence-driven. */
export async function stopAutoplay(ctx: DriveContext): Promise<boolean> {
  const st = (await ctx.uiRead(AUTO_JS, {})) || {};
  if (!truthy(get(st, "on"))) return false;
  const btn = autoStopButton();
  if (btn) await ctx.clickButton(btn, { optional: true, dwellMs: 1500 });
  return true;
}

/**
 * Leave the game fit for the next run; called before and after every run. Draining an unfinished
 * bonus is unconditional REPAIR; asserting a spinnable base happens only when the suite asks
 * (`assert_base`). On failure, reload + `startGame()` — the definitive reset. `carryLast` hands over
 * the PREVIOUS run's last BET response. Returns "clean", "drained" or "reloaded(<reason>)".
 */
export async function ensureBase(ctx: DriveContext, carryLast: Bet | null = null, label = "", assertBase = false): Promise<string> {
  if (carryLast !== null && ctx.lastBet === null) ctx.lastBet = carryLast;
  const drained = !isTerminal(carryLast);
  try {
    if (await stopAutoplay(ctx)) console.log(`  ${label || "ensure-base"}: auto-play was still running -- stopped it`);
    if ((await ctx.popups()).length) {
      const closed = await ctx.dismissPopups();
      console.log(`  ${label || "ensure-base"}: closed leftover popup(s): ${closed.length ? `[${closed.map((c) => `'${c}'`).join(", ")}]` : "none"}`);
    }
    await returnToBase(ctx, 500, assertBase);
    return drained ? "drained" : "clean";
  } catch (e) {
    const why = `${(e as Error).name}: ${String((e as Error).message).slice(0, 120)}`;
    console.log(`  !! ${label || "ensure-base"}: could not reach a clean base (${why}) -- reloading`);
    await ctx.reloadAndStart();
    return `reloaded(${why})`;
  }
}

/** Put the wallet back to exactly what it was before this run touched it, then reload. Restores
 * only when the run actually changed the wallet. */
export async function restoreBalance(ctx: DriveContext): Promise<void> {
  if (ctx.walletWas === null) {
    console.log("  restore-balance: the run never changed the wallet -- nothing to restore");
    return;
  }
  await ctx.setBalance(ctx.walletWas);
  console.log(`  restore-balance: wallet back to ${ctx.walletWas}`);
  try {
    await ctx.reloadAndStart();
  } catch {
    // the next PREPARE checks the base for itself
  }
}

/** DEPRECATED: top the wallet up by 10000 and reload. Prefer `restore-balance`. */
export async function deposit10000(ctx: DriveContext): Promise<void> {
  await ctx.deposit(10000);
  try {
    await ctx.reloadAndStart();
  } catch {
    // the next PREPARE checks the base for itself
  }
}

/** `teardown: return-to-base` as a suite declares it — REPAIR only, never an assertion. */
async function teardownReturnToBase(ctx: DriveContext): Promise<void> {
  await returnToBase(ctx, 500, false);
}

/**
 * `teardown: reset-denomination` — the MANDATORY partner of `ctx.setAutomationDenomination`. A
 * ladder left on 11/22/33 makes every later run produce FALSE REDS, so a failure here RE-RAISES and
 * aborts the suite on purpose. BO accepts `isDenomOverride: 0` only with the MASTER id, so a row
 * overridden before the run is restored WITH its override, anything else goes back to the master.
 */
export async function resetDenomination(ctx: DriveContext): Promise<void> {
  if (ctx.denomWas === null) {
    console.log("  reset-denomination: the run never swapped the denomination -- nothing to reset");
    return;
  }
  if (ctx.bo === null) {
    throw new PyRuntimeError("reset-denomination: BO is not configured, so the automation bet ladder (11/22/33) is "
      + "STILL IN FORCE on the agent's game row. Reset it in BO before running anything else.");
  }
  const was = ctx.denomWas;
  const keepOverride = truthy(get(was, "override")) && !truthy(get(was, "leaked"));
  const target = keepOverride ? get(was, "id") : get(was, "master");
  try {
    if (keepOverride) await ctx.bo.assignAgentDenomination(target);
    else await ctx.bo.resetAgentDenomination({ denominationId: target });
  } catch (e) {
    console.log(`  !! reset-denomination FAILED (${(e as Error).message}) -- the automation bet ladder (11/22/33) is `
      + "STILL IN FORCE. Every later run's bet asserts would be measured against it, so the "
      + `suite stops here. Put the agent's row back on denomination ${target} in BO `
      + `(${keepOverride ? "override ON" : "override OFF"}), then re-run.`);
    throw e;
  }
  console.log(`  reset-denomination: agent back on denomination ${target}`
    + (keepOverride ? " (override kept, as it was before the run)" : "")
    + (truthy(get(was, "leaked")) ? " -- an earlier run had leaked the automation ladder" : ""));
  ctx.denomWas = null;
}

export const TEARDOWNS: Readonly<Record<string, (ctx: DriveContext) => Promise<void>>> = {
  "return-to-base": teardownReturnToBase,
  "restore-balance": restoreBalance,
  "reset-denomination": resetDenomination,
  "deposit-10000": deposit10000,
};
