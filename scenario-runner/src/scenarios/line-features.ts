/**
 * Line-game feature scenarios: Ante Bet (toggle, and through a free-spin bonus), Chaos's P1 ways
 * win, Raining Wild, the random multiplier ladder, paytable bet scaling, and Joker's Buy Bonus with
 * its Lucky Spin wheel. Port of scenario_engine.py `_GRID_UNREADABLE` .. `BuyBonusLuckySpin`.
 *
 * Chaos- and Joker-only profile methods (`reelsWith`, `scatters`, `p1Ways`, `expandedReels`, ...)
 * are reached through `GAME as any`, where the Python duck-typed them.
 */
import { type Bet, isFreespinTrigger, isTerminal } from "../engine/bet.js";
import type { DriveContext } from "../engine/drive.js";
import {
  anteOffered, baseRoundsSince, near, playOutBonus, playOutFreespins, settleToBase, spinAndFinish, switchFeature,
} from "../engine/flows.js";
import { GAME } from "../engine/game.js";
import { requireButtons, ScenarioResult, Unreached } from "../engine/result.js";
import { closeBetList, openBetList } from "../engine/shared.js";
import { type Dict, get, or, py, pyFloat, pyRound, pySorted, sum, truthy, tryFloat } from "../py.js";
import { checkParams, param, type Params, Scenario, type ScenarioClass } from "./base.js";

const GRID_UNREADABLE = "the response carried no readable grid. The chaos profile reads "
  + "`result.result.views[].value` as numeric symbol ids (games/chaos/"
  + "bet-response.md §3); a round without one cannot be judged, so this reports "
  + "unreached rather than passing on an empty census.";

/** Python's `xs[i]`: a negative index counts from the end. */
function at<T>(xs: readonly T[], i: number): T {
  return xs.at(i) as T;
}

/** Python's `a % n`: the result takes the sign of `n`, not of `a`. */
function pyMod(a: number, n: number): number {
  return ((a % n) + n) % n;
}

/** Whatever the bridge will tell us about the ante control right now. Best-effort: the contract
 * lists no ante-specific getter, so the STAKE facts come from the RESPONSE and this is UI evidence
 * only (`getSnapshot` is documented safe to call at any time and never throws). */
function anteStateJs(): string {
  return "() => { const b = window.__gameTestBridge; if (!b) return null;"
    + " const s = b.getSnapshot ? b.getSnapshot() : {};"
    + " const r = b.getButtonRect ? b.getButtonRect('anteBet') : null;"
    + " return { present: !!r, visible: !!(r && r.visible), betAmount: s.betAmount,"
    + "          balance: s.balance, spinCost: s.spinCost,"
    + "          anteOn: s.isAnteBet === undefined ? null : !!s.isAnteBet }; }";
}

/** Hover the ante control so its info panel (normal stake vs ante stake) is on screen, and shoot
 * it. Returns whether the control was there to hover. A panel that only opens on hover cannot be
 * reached with `clickButton`, so this is a deliberate raw-pointer move against a rect the bridge
 * gave us — the one place in this file that bypasses the role helpers. */
async function hoverAnte(ctx: DriveContext, label: string, dwellMs: number): Promise<boolean> {
  const rect = await ctx.uiRead("() => { const b = window.__gameTestBridge;"
    + " return b && b.getButtonRect ? b.getButtonRect('anteBet') : null; }");
  if (!truthy(rect)) return false;
  ctx.mark(`ANTE INFO PANEL: ${label}`);
  await ctx.page.mouse.move(rect.centerX, rect.centerY);
  await ctx.dwell(dwellMs);
  await ctx.screenshot(label);
  return true;
}

export class AnteToggle extends Scenario {    // CR_049 / 050 / 051 / 054 / 109 · DV-53 / 54 / 55 / 56
  readonly name = "ante-toggle";
  readonly description = "Ante Bet in the base game: one spin with ante OFF, hover the control to raise "
    + "its info panel, switch ante ON, spin again at the SAME bet level, then switch "
    + "it back OFF.\n"
    + "The point of the case is the DIVERGENCE ante creates, and one play captures "
    + "both sides of it: `stake` (what the player pays) is multiplied, `baseStake` "
    + "(what payouts derive from) is not. facts.stake_off / stake_on / base_off / "
    + "base_on / ante_multiplier / ante_multiplier_setting (from gameInfo.result.settings) / "
    + "expected_ante_stake / stake_ratio / base_unchanged carry it exactly, and "
    + "`rounds` holds both spins so `ante_stake_multiplier` gets the ante/non-ante "
    + "pairing it needs to prove the base did not move.\n"
    + "Marks off/on; shots ante-off / ante-info-panel / ante-on. Unreached when this "
    + "build has no ante control.\n"
    + "Ante is switched through `switch_feature`, so a build that puts it behind the Buy "
    + "Bonus panel (Dragon VIP) runs the same play. There, while ante is ON, "
    + "facts.feature_panel_blocked_while_on / premium_blocked_while_on / "
    + "premium_block_reason probe what it excludes, and ante_toast_on_enable / "
    + "ante_toast_on_disable read the ante toast. `spin_after_off` spins once more after "
    + "ante goes back off: facts.stake_after_off / base_after_off / ante_off_spin_plain, "
    + "mark `after_off`.";
  static readonly PARAMS = ["hover_ms", "spin_after_off"];

  readonly hoverMs: number;
  readonly spinAfterOff: boolean;     // one more spin once ante is back off

  constructor(p: Params = {}) {
    super();
    checkParams(AnteToggle, p);
    this.hoverMs = param(p, "hover_ms", 1500);
    this.spinAfterOff = param(p, "spin_after_off", false);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    if (!GAME.HAS_ANTE_BET) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: `${GAME.name} has no Ante Bet feature (profile HAS_ANTE_BET=False)` });
    }
    if (!(await anteOffered(ctx))) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "this session offers no Ante Bet (no ante control, or the "
            + "snapshot's anteBetAvailable is false)" });
    }

    let before = or(await ctx.uiRead(anteStateJs(), {}), {}) as Dict;

    // ENSURE ANTE IS ACTUALLY OFF FIRST. The whole case is a comparison between an ante-off and
    // an ante-on spin, and it used to just assume the control started off. A previous run that
    // left ante ON inverted the entire play: the "baseline" spin was the ante one (stake 12.00),
    // the "ANTE ON" click turned it OFF, and the second spin cost 4.00. Every derived fact came
    // out backwards -- stake_ratio 0.33 against a multiplier of 3 -- and the case failed as if
    // the GAME had applied ante the wrong way round. Observed live 2026-09-04.
    //
    // `isAnteBet` is UI evidence, not the contract, so this is best-effort: when the bridge does
    // not publish it (null) there is nothing to correct and the run proceeds as before.
    if (truthy(get(before, "anteOn"))) {
      ctx.mark("ANTE was left ON by an earlier run -- switching it off for the baseline");
      await switchFeature(ctx, "ante", false, { dwellMs: 1000 });
      await ctx.dismissPopups();                  // the toggle raises its own notification
      before = or(await ctx.uiRead(anteStateJs(), {}), {}) as Dict;
      if (truthy(get(before, "anteOn"))) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
          { reason: "ante was ON at the start and would not switch off -- "
              + "the baseline spin would be an ante spin and every "
              + "comparison in this case would be inverted" });
      }
    }

    ctx.mark("ANTE OFF: baseline spin");
    // spinAndFinish handles the trigger case for us: if this spin awards free spins the bonus
    // is played out to its terminal round before we carry on (Rule 1).
    const [off, offRounds] = await spinAndFinish(ctx);
    await ctx.screenshot("ante-off");

    const hovered = await hoverAnte(ctx, "ante-info-panel", this.hoverMs);

    ctx.mark("ANTE ON");
    const onSwitch = await switchFeature(ctx, "ante", true, { optional: false });
    await ctx.screenshot("ante-on");
    const during = or(await ctx.uiRead(anteStateJs(), {}), {}) as Dict;
    // What ante EXCLUDES, probed while it is on -- instant reads, never a wait on a control that
    // should be locked. Only meaningful where ante shares the Buy Bonus panel (`ante_on`).
    const panelBlocked = truthy(GAME.button("ante_on")) ? !(await ctx.hasButton("buy_bonus")) : null;
    // ⚠ Ask the SESSION, not the profile. A build can ship Premium Reel while the launch payload
    // does not offer it (`settings.premiumReel` null -> `premiumReelAvailable` false,
    // `getPremiumReelOptions()` []): every canDo then answers `feature_unavailable:premiumReels`,
    // which says nothing about ante's exclusion. `premium_offered` separates "not on this
    // session" from "ante blocked it", so a case can gate instead of reading None as a failure.
    // Dragon VIP dev, 2026-09-16: offered false, and DV-55 failed on `None == True`.
    let premiumWhy: unknown = null;
    let premiumOffered: boolean | null = null;
    if (GAME.HAS_PREMIUM_REEL) {
      const premium = or(await ctx.uiRead(
        "() => { const b = window.__gameTestBridge; if (!b) return null;"
        + " const s = b.getSnapshot ? b.getSnapshot() : {};"
        + " const o = b.getPremiumReelOptions ? b.getPremiumReelOptions() : [];"
        + " const offered = !!s.premiumReelAvailable && o.length > 0;"
        + " return { offered: offered, ids: o.map(x => x.reelId),"
        + "          why: (offered && b.canDo) ? b.canDo('activatePremiumReels:' + o[0].reelId) : null }; }",
        {}), {}) as Dict;
      premiumOffered = truthy(get(premium, "offered"));
      premiumWhy = get(premium, "why");
    }

    const [on, onRounds] = await spinAndFinish(ctx);

    ctx.mark("ANTE OFF again");
    const offSwitch = await switchFeature(ctx, "ante", false);   // leave the game as we found it
    await ctx.dismissPopups();                          // the ante info popup, if it latched open
    const after = or(await ctx.uiRead(anteStateJs(), {}), {}) as Dict;
    let afterOff: Bet | null = null;
    let afterRounds: Bet[] = [];
    if (this.spinAfterOff) {
      ctx.mark("ANTE OFF: spin at the plain stake");
      [afterOff, afterRounds] = await spinAndFinish(ctx);
    }

    const baseOff = GAME.baseStake(off.raw || {});
    const baseOn = GAME.baseStake(on.raw || {});
    const mult = GAME.anteMultiplier(on.raw || {});
    // The multiplier the game was CONFIGURED with, from gameInfo.result.settings.anteMultiplier.
    // `mult` above is the one the ante ROUND reported, which is the right thing to grade the
    // stake against; this is the independent second opinion the AI is given so the clip can be
    // judged against the configuration rather than against a number derived from the same spin.
    // They should agree -- if they ever do not, that disagreement is itself the finding.
    const settingMult = await ctx.uiRead(`() => {
            const b = window.__gameTestBridge;
            const gi = b && b.getGameInfo ? b.getGameInfo() : null;
            const dig = (o, ks) => ks.reduce((a, k) => (a && a[k] != null) ? a[k] : null, o);
            const v = dig(gi, ['result', 'settings', 'anteMultiplier'])
                   ?? dig(gi, ['settings', 'anteMultiplier'])
                   ?? dig(gi, ['data', 'result', 'settings', 'anteMultiplier']);
            return (typeof v === 'number' && !isNaN(v)) ? v : null; }`, null);
    const popupsOn = onSwitch["popups"] as unknown[];
    const popupsOff = offSwitch["popups"] as unknown[];
    const facts: Dict = {
      stake_off: off.stake, stake_on: on.stake,
      base_off: baseOff, base_on: baseOn,
      // null when the server reported no multiplier — a case must not then assert a ratio.
      ante_multiplier: mult,
      // From gameInfo.result.settings — the CONFIGURED multiplier, read independently of the
      // round. null on a build that does not publish it; the prompt then falls back to
      // `ante_multiplier` and the two agree by construction.
      ante_multiplier_setting: settingMult,
      // What the bet SHOULD read on screen once ante is on: the plain stake times the
      // configured multiplier. Given to the AI so it checks the arithmetic against the
      // configuration, not just "the number went up".
      expected_ante_stake: (truthy(off.stake) && truthy(or(settingMult, mult))
        ? pyRound(off.stake * (or(settingMult, mult) as number), 2) : null),
      ante_engaged: GAME.isAnteRound(on.raw || {}),
      // The OBSERVED ratio. A case compares this against the multiplier the SERVER reported
      // for that round, never against a hardcoded 3 — it is BO-configurable (CR_055).
      stake_ratio: truthy(off.stake) ? pyRound(on.stake / off.stake, 6) : null,
      base_unchanged: Math.abs(baseOn - baseOff) < 1e-6,
      hovered,
      bet_amount_off: get(before, "betAmount"), bet_amount_on: get(during, "betAmount"),
      bet_amount_restored: get(after, "betAmount"),
      // Both spins may have dragged a bonus behind them; the case needs to know.
      bonus_rounds: offRounds.length + onRounds.length - 2,
      ante_switched_on: onSwitch["switched"],
      ante_switched_off: offSwitch["switched"],
      popups_on_enable: onSwitch["popups"],
      popups_on_disable: offSwitch["popups"],
      // The ante toast, where the profile names one. null = no label to look for.
      ante_toast_on_enable: (truthy(GAME.popup("ante_notify"))
        ? popupsOn.includes(GAME.popup("ante_notify")) : null),
      ante_toast_on_disable: (truthy(GAME.popup("ante_notify"))
        ? popupsOff.includes(GAME.popup("ante_notify")) : null),
      spin_cost_on: get(during, "spinCost"),
      feature_panel_blocked_while_on: panelBlocked,
      // Gate the two below on this. False = the SESSION ships no Premium Reel, so the refusal
      // is `feature_unavailable:premiumReels` and proves nothing about ante's exclusion.
      premium_offered: premiumOffered,
      premium_blocked_while_on: truthy(premiumWhy) ? !truthy(get(premiumWhy, "ok")) : null,
      premium_block_reason: get(or(premiumWhy, {}), "reason"),
      stake_after_off: afterOff !== null ? afterOff.stake : null,
      base_after_off: afterOff !== null ? GAME.baseStake(afterOff.raw || {}) : null,
      ante_off_spin_plain: (afterOff !== null
        ? (!GAME.isAnteRound(afterOff.raw || {}) && near(afterOff.stake, GAME.baseStake(afterOff.raw || {})))
        : null),
    };
    return new ScenarioResult(this.name, true, on, [...offRounds, ...onRounds, ...afterRounds],
      ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { off: off.seq, on: on.seq, ...(afterOff !== null ? { after_off: afterOff.seq } : {}) },
        facts, shots: ctx.shots,
      });
  }
}

export class AnteFreeSpins extends Scenario {    // CR_053 / 055 / 056 / 096
  readonly name = "ante-freespins";
  readonly description = "Turn Ante Bet ON, spin until it triggers free spins, then play the whole bonus "
    + "out. Answers the two questions the ante cases ask about a RUNNING bonus:\n"
    + "  * is the ante control LOCKED while the feature plays "
    + "(facts.ante_clickable_in_fs — probed instantly with getButtonRect, never waited "
    + "on, because waiting on a deliberately disabled control burns the whole timeout "
    + "inside the bonus);\n"
    + "  * is the ante multiplier FROZEN at its trigger value (facts.ante_frozen).\n"
    + "⚠ A free spin costs nothing, so the server may report `anteMultiplier: 0` on "
    + "every one — in which case the frozen rule is NOT answerable from the response "
    + "and facts.ante_verifiable_in_fs is False. A case must gate on that before "
    + "asserting ante_frozen, or 'no data' reads as 'frozen'. The ante-info panel is "
    + "shot mid-bonus (ante-info-in-fs) as the fallback evidence CR_055 actually asks "
    + "for.\n"
    + "The ante toggle is SWITCHED, not blind-clicked: `isAnteBet` is read first, so a "
    + "game that already loaded with ante ON is left alone instead of being toggled OFF "
    + "into a bonus nobody paid ante for, and the end-of-run restore only undoes a click "
    + "this run actually made. facts.ante_on_at_start records the state found at load and "
    + "facts.ante_engaged_at_trigger says whether the TRIGGER round really carried the "
    + "ante bet — gate every ante claim on that one.\n"
    + "Exposes rounds = [trigger, ...free spins..., terminal] for the shared free-spin "
    + "invariants. The BO half of CR_055/056 — changing config mid-bonus and reloading "
    + "— is NOT driven here.";
  static readonly PARAMS = ["max_base"];

  readonly maxBase: number;

  constructor(p: Params = {}) {
    super();
    checkParams(AnteFreeSpins, p);
    this.maxBase = param(p, "max_base", 300);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const g = GAME as any;
    const start = ctx.nowS();
    if (!GAME.HAS_ANTE_BET || !(await ctx.hasButton("ante"))) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "no ante control on this build" });
    }

    // SWITCH ante on; do NOT blind-click it. `clickButton` toggles, so an unconditional click
    // against a game that already had ante ON turns it OFF and plays the whole "ante free spins"
    // case with no ante bet at all: the stake stays at the base, `ante_at_trigger` comes back
    // None, and CR_055/056 go red as if the GAME had dropped the ante mid-bonus. Observed live
    // 2026-09-04 (run 154942 -- green toggle at load, black one second after our click, and both
    // base spins deducted 4.00 instead of the 12.00 the info panel quotes). AnteToggle already
    // carries the mirror of this guard for the same reason.
    //
    // `isAnteBet` is UI evidence, not the contract, so this is best-effort: a bridge that does
    // not publish it answers null, and the click then goes ahead exactly as it used to.
    let state = or(await ctx.uiRead(anteStateJs(), {}), {}) as Dict;
    const anteOnAtStart = get(state, "anteOn");
    if (truthy(anteOnAtStart)) {
      ctx.mark("ANTE was already ON at load -- leaving it on rather than toggling it OFF");
    } else {
      ctx.mark("ANTE ON");
      await ctx.clickButton("ante");
      await ctx.dismissPopups();                  // the toggle raises its own notification
      state = or(await ctx.uiRead(anteStateJs(), {}), {}) as Dict;
      // Only a definite False is a failure. null means the bridge does not publish the flag,
      // which is the pre-existing blind-click behaviour and still worth running.
      if (get(state, "anteOn") === false) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          facts: { ante_on_at_start: anteOnAtStart },
          reason: "the ante control would not switch ON -- every base "
            + "spin would be a plain bet and the bonus would be "
            + "graded as an ante case nobody paid ante for",
        });
      }
    }
    // True only when WE turned it on, so the restore below cannot latch the game into ante-ON
    // and hand the next run the very state this guard exists to survive.
    const restoreNeeded = !truthy(anteOnAtStart);

    const before = ctx.events.length;
    let trigger: Bet | null = null;
    let spins = 0;
    for (let i = 0; i < this.maxBase; i++) {
      const b = await ctx.baseSpin();
      spins += 1;
      if (isFreespinTrigger(b)) {
        trigger = b;
        break;
      }
      await ctx.settle();
    }
    if (trigger === null) {
      if (restoreNeeded) await ctx.clickButton("ante", { optional: true });
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: { base_spins: spins, ante_on_at_start: anteOnAtStart },
        reason: py`no free-spin trigger in ${spins} ante spins — force one `
          + "with a `paytable:` fixture rather than raising the budget",
      });
    }

    // Say what the ROUND reported, never a hardcoded "ante ON": the old literal text asserted a
    // state nobody had read and stood over a trigger bet at the plain stake.
    ctx.mark(py`FREE SPINS TRIGGERED on Bonus across `
      + py`${(or(g.scatterReels(trigger.raw || {}), []) as unknown[]).length} reel(s), `
      + `ante ${GAME.isAnteRound(trigger.raw || {}) ? "ON" : "OFF"}`);
    // Probe, never WAIT: an instant getButtonRect. This is the CR_053 evidence.
    const anteClickable = await ctx.hasButton("ante");
    await hoverAnte(ctx, "ante-info-in-fs", 1500);

    // The bonus is the subject here, so drive it deliberately and keep every round.
    const rounds = await playOutFreespins(ctx, trigger);

    const fs = rounds.filter((b) => b.phase === "freespin");
    // Keep the nulls OUT of the drift set: `{None, None}` has one element and would otherwise
    // report a confident "frozen" on a stream that said nothing at all.
    const reported = fs.map((b) => GAME.anteMultiplier(b.raw || {})).filter((m): m is number => m !== null);
    if (restoreNeeded) {                                  // restore only what WE changed
      await ctx.clickButton("ante", { optional: true });
    }
    const facts: Dict = {
      base_spins: spins,
      ante_on_at_start: anteOnAtStart,
      // Did the TRIGGER round actually carry the ante bet? `isAnteRound` falls back to
      // stake > baseStake, so it answers even on a build that omits `anteMultiplier`. Gate on
      // this: without it a run that never engaged ante grades as an ante case.
      ante_engaged_at_trigger: GAME.isAnteRound(trigger.raw || {}),
      paid_spins: baseRoundsSince(ctx, before),
      trigger_scatters: g.scatters(trigger.raw || {}),
      trigger_reels: (or(g.scatterReels(trigger.raw || {}), []) as unknown[]).length,
      expected_award: g.expectedFsAward(trigger.raw || {}),
      awarded: trigger.total_free_spin,
      free_spins_played: fs.length,
      ante_clickable_in_fs: anteClickable,
      ante_at_trigger: GAME.anteMultiplier(trigger.raw || {}),
      ante_multipliers: pySorted(new Set(reported)),
      ante_reported_in_fs: reported.length,
      // Whether the frozen rule can be answered from the RESPONSE at all. Gate on this.
      ante_verifiable_in_fs: reported.length >= 2,
      ante_frozen: reported.length ? new Set(reported).size <= 1 : null,
      trigger_stake: trigger.stake,
      fs_stakes: pySorted(new Set(fs.map((b) => b.stake))),
      bonus_total: pyRound(sum(rounds.map((b) => b.payout)), 4),
      terminal_ok: isTerminal(rounds[rounds.length - 1]),
    };
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      marks: { trigger: trigger.seq, terminal: rounds[rounds.length - 1].seq },
      facts, shots: ctx.shots,
    });
  }
}

export class P1WaysWin extends Scenario {    // CR_045 / 062 / 063
  readonly name = "p1-ways-win";
  readonly description = "Symbol P1 (\"Fuel\", id 5): spin until a round pays a P1 WAYS WIN whose whole "
    + "payout is that win — P1 on reels 1, 3 and 5, and no payline win alongside it. "
    + "Needs the forced `chaos-p1-ways` paytable (~28% of spins qualify); on the real "
    + "strips this is ~1 in 1800 spins and the run would report inconclusive.\n"
    + "Why SCATTER-ONLY: the game shows one number, so on a round that also paid a line "
    + "the screen shows the SUM and an AI read of it proves nothing about either half. "
    + "When the P1 pay is the whole win the display is exactly "
    + "ways x 10 x baseStake, which is what makes the documented formula checkable "
    + "from the video.\n"
    + "Every round spun on the way is audited for P1 on a reel it must never occupy "
    + "(reels 2 and 4), free-spin rounds included.\n"
    + "Exposes facts.p1_ways / p1_reels / rounds_audited / paid_spins / illegal_reels; "
    + "the documented formula as symbol_multiplier / base_stake / p1_pay / line_pay / "
    + "formula_ok; and — because `payout` is CAPPED — both expected_gross vs "
    + "raw_payout AND expected_credited vs actual_payout. A case must not compare the "
    + "ways formula against the credited figure.";
  static readonly PARAMS = ["max_spins"];

  readonly maxSpins: number;

  constructor(p: Params = {}) {
    super();
    checkParams(P1WaysWin, p);
    this.maxSpins = param(p, "max_spins", 40);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const g = GAME as any;
    const start = ctx.nowS();
    const before = ctx.events.length;
    let win: Bet | null = null;
    let audited = 0;
    const illegal: Dict[] = [];
    const seen: number[] = [];
    let p1WinsSeen = 0;
    for (let i = 0; i < this.maxSpins; i++) {
      // One spin taken to a clean base; `chain` is [spin] or [trigger, ...bonus..., terminal].
      const [, chain] = await spinAndFinish(ctx);
      for (const r of chain) {
        const reels = g.reelsWith(r.raw || {}, g.SYM_P1) as number[] | null;
        if (reels === null) {
          return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
            { round_windows: ctx.windows(), reason: GRID_UNREADABLE });
        }
        audited += 1;
        seen.push(...reels);
        const bad = reels.filter((x) => !(g.P1_REELS as number[]).includes(x));
        if (bad.length) illegal.push({ seq: r.seq, phase: r.phase, reels: bad });
        const p1WaysCount = or(g.p1Ways(r.raw || {}), 0) as number;
        if (p1WaysCount > 0) {
          p1WinsSeen += 1;
          // Only a win with NO line pay alongside it will do (see the class docstring).
          // `<= 1e-9`, not `== 0`: the line win is a product/quotient, so a zero can
          // arrive as a float artefact.
          if (win === null && g.expectedLineWin(r.raw || {}) <= 1e-9) {
            win = r;
            ctx.mark(py`P1 WAYS WIN: ${p1WaysCount} way(s) on reels ${reels}`);
          }
        }
      }
      if (win !== null) break;
    }

    const facts: Dict = {
      rounds_audited: audited,
      paid_spins: baseRoundsSince(ctx, before),
      illegal_reels: illegal,
      p1_reels_seen: pySorted(new Set(seen)),
      allowed_reels: [...g.P1_REELS],
      p1_wins_seen: p1WinsSeen,
    };
    if (win === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(), facts,
        reason: py`no scatter-only P1 ways win in ${audited} round(s) `
          + py`(${p1WinsSeen} P1 win(s) seen, all with a line win `
          + "alongside). Check the run declares "
          + "`paytable: { ref: chaos-p1-ways }`, then raise max_spins",
      });
    }

    const raw = (win.raw || {}) as Dict;
    const p1WaysCount = g.p1Ways(raw);
    const baseStake = GAME.baseStake(raw);
    const cap = win.max_win !== null ? win.max_win : GAME.maxWinCap(raw);
    const left = GAME.maxWinLeft(raw, cap);            // the cap counts the whole round
    const grossRaw = get(raw, GAME.F_RAW_PAYOUT);
    const gross = grossRaw !== null ? pyFloat(grossRaw) : null;
    Object.assign(facts, {
      p1_ways: p1WaysCount,
      p1_reels: g.reelsWith(raw, g.SYM_P1),
      base_stake: baseStake,
      scatter_multiplier: g.scatterMultiplier(raw),
      // GROSS, before the cap — this is what the ways formula predicts.
      expected_gross: pyRound(p1WaysCount * g.P1_MULTIPLIER * baseStake, 6),
      raw_payout: gross,
      // CREDITED, after the cap. These DIVERGE on a capped round.
      actual_payout: win.payout,
      expected_credited: g.expectedPayout(raw),
      max_win: cap,
      capped: gross !== null && left !== null && gross > left + 1e-9,
      // The DOCUMENTED formula, split out so a case can check the figure the player
      // actually sees: design doc §4 gives
      //   Total Payout = Total Ways x Symbol Multiplier x Bet base
      // `p1_pay` is that product; `line_pay` is anything the paylines added on top
      // (0 here by construction, and asserted by the suite so it stays that way).
      symbol_multiplier: g.P1_MULTIPLIER,
      p1_pay: pyRound(g.expectedScatterWin(raw), 6),
      line_pay: pyRound(g.expectedLineWin(raw), 6),
      scatter_only: g.expectedLineWin(raw) <= 1e-9,
      // Computed HERE, not in the suite: an inline `stream:` predicate is eval'd
      // without builtins, so it has no `abs()` and cannot compare floats safely
      // (2 x 10.0 x 0.6 is 12.000000000000002, which != 12.0).
      formula_ok: Math.abs(g.expectedScatterWin(raw)
        - (or(p1WaysCount, 0) as number) * g.P1_MULTIPLIER
        * (or(baseStake, 0) as number)) < 1e-6,
    });
    return new ScenarioResult(this.name, true, win, [win], ctx.timeline, ctx.events, [start, ctx.nowS()], {
      round_windows: ctx.windows(),
      marks: { win: win.seq }, facts, shots: ctx.shots,
    });
  }
}

export class RainingWild extends Scenario {    // CR_059 / 060 / 061
  readonly name = "raining-wild";
  readonly description = "Raining Wild: spin until a result carries 2+ Wilds (id 9) — the documented 100% "
    + "trigger — and capture that spin's animation window. Also records a CONTRASTING "
    + "spin with fewer than 2 Wilds (0%, the animation must NOT play), so the rule can "
    + "be judged both ways instead of only confirming the positive. Without the "
    + "negative, an implementation that played the animation on EVERY spin would pass.\n"
    + "The animation is VISUAL ONLY (game-knowledge.md §4): it must never change the "
    + "outcome, so facts.payout_at_rain is exposed for a conservation check rather than "
    + "any claim that the effect paid something.\n"
    + "Exposes facts.wilds / wild_reels / expected_raining / control_wilds; marks rain "
    + "and norain.";
  static readonly PARAMS = ["max_spins"];

  readonly maxSpins: number;

  constructor(p: Params = {}) {
    super();
    checkParams(RainingWild, p);
    this.maxSpins = param(p, "max_spins", 200);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const g = GAME as any;
    const start = ctx.nowS();
    let rain: Bet | null = null;
    let norain: Bet | null = null;
    for (let i = 0; i < this.maxSpins; i++) {
      const [b] = await spinAndFinish(ctx);
      const n = g.wilds(b.raw || {}) as number | null;
      if (n === null) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
          { round_windows: ctx.windows(), reason: GRID_UNREADABLE });
      }
      if (n >= g.RAINING_WILD_MIN_WILDS && rain === null) {
        rain = b;
        ctx.mark(py`RAINING WILD expected: ${n} Wild(s) in the result`);
      } else if (n < g.RAINING_WILD_MIN_WILDS && norain === null) {
        norain = b;
        ctx.mark(py`NO RAINING WILD expected: only ${n} Wild(s)`);
      }
      if (rain !== null && norain !== null) break;
    }
    if (rain === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: { control_wilds: norain !== null ? g.wilds(norain.raw || {}) : null,
          has_control: norain !== null },
        reason: py`no result with ${g.RAINING_WILD_MIN_WILDS}+ Wilds in `
          + py`${this.maxSpins} spins — force one with a \`paytable:\` `
          + "fixture. Note the ante distribution list raises the Wild "
          + "weight 5-10x, so ante ON reaches this much faster.",
      });
    }
    const rounds = [norain, rain].filter((b): b is Bet => b !== null);
    const facts: Dict = {
      wilds: g.wilds(rain.raw || {}),
      wild_reels: g.reelsWith(rain.raw || {}, g.SYM_WILD),
      expected_raining: g.rainingWildExpected(rain.raw || {}),
      min_wilds: g.RAINING_WILD_MIN_WILDS,
      payout_at_rain: rain.payout,
      scatters_at_rain: g.scatters(rain.raw || {}),
      control_wilds: norain !== null ? g.wilds(norain.raw || {}) : null,
      has_control: norain !== null,
    };
    const marks: Dict = { rain: rain.seq };
    if (norain !== null) marks.norain = norain.seq;
    return new ScenarioResult(this.name, true, rain, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()],
      { round_windows: ctx.windows(), marks, facts });
  }
}

export class RandomMultiplier extends Scenario {    // CR_057 / 058
  readonly name = "random-multiplier";
  readonly description = "The random multiplier ladder (x1/x2/x5/x10) across BOTH modes: sample the base "
    + "game, then reach a bonus and sample every free spin in it. The BO export gives "
    + "the weights — x10 is weight 5 in the base game but 40 in free spins — so an "
    + "all-x1 bonus is unusual rather than normal.\n"
    + "Also reads the configured ladder straight from the bridge "
    + "(getMultiplierLevels) as facts.configured_ladder, so 'the values shown are the "
    + "values configured' is an EXACT check rather than an AI read of the on-screen "
    + "bar. Exposes facts.base_multipliers / fs_multipliers / all_in_ladder / "
    + "reached_freespins.\n"
    + "`base_spins` is the MINIMUM number of base rounds sampled: the run cannot choose "
    + "which spin triggers, so when the bonus arrives first it goes back to the base "
    + "game afterwards until the count is met. facts.base_spins counts every base "
    + "round, base_spins_after_bonus the ones played after it; fs_spins counts every "
    + "free spin played and fs_multiplier_count the ones that reported a multiplier.";
  static readonly PARAMS = ["base_spins", "max_base"];

  // MINIMUM base rounds in the sample. A trigger cannot be put off, so when it arrives
  // before this many base rounds the run plays more base spins AFTER the bonus. `max_base`
  // caps the paid spins either way.
  readonly baseSpins: number;
  readonly maxBase: number;

  constructor(p: Params = {}) {
    super();
    checkParams(RandomMultiplier, p);
    this.baseSpins = param(p, "base_spins", 3);
    this.maxBase = param(p, "max_base", 40);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const g = GAME as any;
    const start = ctx.nowS();
    const configured = await ctx.uiRead(
      "() => { const b = window.__gameTestBridge;"
      + " return b && b.getMultiplierLevels ? b.getMultiplierLevels() : null; }");

    ctx.mark("BASE GAME multiplier sampling");
    const rounds: Bet[] = [];
    let trigger: Bet | null = null;
    let baseBefore: number | null = null;     // base rounds played before the bonus
    const baseCount = (): number => rounds.filter((x) => x.phase === "base" || x.phase === "auto").length;
    // One loop, one budget: sample the base game until a bonus has turned up AND at least
    // `base_spins` base rounds are in. spinAndFinish returns the whole chain, so a trigger
    // contributes its free spins to the sample instead of being handled separately.
    for (let i = 0; i < this.maxBase; i++) {
      const [b, chain] = await spinAndFinish(ctx);
      rounds.push(...chain);
      if (trigger === null && isFreespinTrigger(b)) {
        trigger = b;
        baseBefore = baseCount();
        ctx.mark("FREE SPINS: multiplier sampling continues inside the bonus");
        if (baseBefore < this.baseSpins) {
          ctx.mark(py`BASE GAME again: ${this.baseSpins - baseBefore} more base spin(s) to reach ${this.baseSpins}`);
        }
      }
      if (trigger !== null && baseCount() >= this.baseSpins) break;
    }
    if (baseBefore === null) baseBefore = baseCount();

    const mults = (bs: Bet[]): number[] =>
      bs.map((x) => g.multiplier(x.raw || {}) as number | null).filter((m): m is number => m !== null);

    const baseM = mults(rounds.filter((b) => b.phase === "base" || b.phase === "auto"));
    const fsM = mults(rounds.filter((b) => b.phase === "freespin"));
    const ladder = (g.MULTIPLIER_VALUES as unknown[]).map(pyFloat);
    const observed = [...baseM, ...fsM];
    const distinct = pySorted(new Set(observed));
    const facts: Dict = {
      base_multipliers: baseM, fs_multipliers: fsM,
      ladder: [...g.MULTIPLIER_VALUES],
      configured_ladder: truthy(configured) ? get(or(configured, {}), "values") : null,
      base_spins: baseCount(),
      base_spins_after_bonus: baseCount() - baseBefore,
      // every free spin PLAYED (what the prompt describes), and separately the ones that
      // reported a multiplier (what "it appears in free spins" needs)
      fs_spins: rounds.filter((b) => b.phase === "freespin").length,
      fs_multiplier_count: fsM.length,
      reported_any: observed.length > 0,
      all_in_ladder: observed.length > 0 && observed.every((m) => ladder.some((x) => x === m)),
      reached_freespins: trigger !== null,
      // "the multiplier is RANDOM, not fixed" -- the ORACLE half. The response says
      // whether this run actually varied; the AI is then asked whether the SCREEN agrees,
      // graded against `varied` rather than against a hardcoded true. It has to work that
      // way: x1 carries 80% of the base weight and 65% of the free-spin weight, so three
      // base spins come back all-x1 about half the time on a perfectly correct game.
      multipliers_seen: distinct,
      distinct_multipliers: distinct.length,
      varied: distinct.length > 1,
    };
    if (!observed.length) {
      // Distinguish "the game never showed one" from "we cannot read it": the latter is a
      // profile gap, and calling it a pass would certify a field nobody has verified.
      return new ScenarioResult(this.name, false, trigger, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(), facts,
        reason: "no round reported `randomMultiplier` — the field is "
          + "documented in games/chaos/bet-response.md §2; if it is "
          + "genuinely absent the profile needs correcting",
      });
    }
    const marks: Dict = trigger !== null ? { trigger: trigger.seq } : {};
    return new ScenarioResult(this.name, true, trigger ?? (rounds.length ? rounds[rounds.length - 1] : null), rounds,
      ctx.timeline, ctx.events, [start, ctx.nowS()],
      { round_windows: ctx.windows(), marks, facts });
  }
}

export class PaytableBetScaling extends Scenario {    // CR_014 / 015 / 016 / 017 / 070
  readonly name = "paytable-bet-scaling";
  readonly description = "The paytable popup and its bet scaling: open the info/paytable surface and shoot "
    + "it at bet level A, close, change the bet to level B, reopen and shoot it again. "
    + "Two full-resolution stills of the SAME table at two bets is what an `image:` "
    + "assert needs to check that the printed payouts moved with the bet (CR_015) — a "
    + "video assert grades a downsampled montage, where paytable digits are not in the "
    + "pixels.\n"
    + "Places NO bet, so Rule 1 does not apply: no response is read and no trigger is "
    + "possible. Exposes facts.bet_a / bet_b (the bet the GAME showed after each "
    + "pick, read back -- unreached when it is not the bet asked for) / bet_ratio "
    + "(bet_b / bet_a, for an image assert's `compare_shots`) / line_bet_a / line_bet_b "
    + "/ divisor and the shots paytable-bet-a / paytable-bet-b.\n"
    + "Per-line bet is `baseStake / betLines` (20). The design doc's `/5` worked "
    + "examples are wrong — see game-knowledge.md §5 item 4.";
  static readonly PARAMS = ["index_a", "index_b"];

  readonly indexA: number;
  readonly indexB: number;

  constructor(p: Params = {}) {
    super();
    checkParams(PaytableBetScaling, p);
    this.indexA = param(p, "index_a", 0);
    this.indexB = param(p, "index_b", 3);
  }

  /** Select denomination `index`. Returns [opened, bet]: whether the list actually opened —
   * `openBetList` checks `visiblePopups` rather than assuming the click landed — and the bet the
   * game shows AFTERWARDS, read back. The row click is optional, so the read-back is what proves
   * it landed: without it a missed click left both shots at one bet while the facts still named
   * two. */
  private async pickBet(ctx: DriveContext, index: number): Promise<[boolean, unknown]> {
    if (!(await openBetList(ctx))) return [false, null];
    await ctx.scrollBetRow(index, 0);
    // Templated role: resolve it WITH the index first — clickButton takes a finished name.
    await ctx.clickButton(GAME.button("bet_row", { i: index }) as string, { optional: true, timeoutMs: 15000 });
    // Same stall risk as browse-denominations: a close that silently does nothing leaves the
    // popup up, and the next settle then waits its whole timeout. closeBetList proves it went.
    await closeBetList(ctx);
    const bet = await ctx.eval("() => { const b = window.__gameTestBridge;"
      + " const s = b.getSnapshot ? b.getSnapshot() : null;"
      + " return b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null); }");
    return [true, bet];
  }

  private async shootPaytable(ctx: DriveContext, label: string): Promise<boolean> {
    if (!(await ctx.clickButton("info", { optional: true, timeoutMs: 10000 }))) return false;
    await ctx.screenshot(label);
    await ctx.dismissPopups();
    return true;
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const denoms = or(await ctx.eval("() => { const b = window.__gameTestBridge;"
      + " const s = b.getSnapshot ? b.getSnapshot() : null;"
      + " return s ? s.denominations : null; }"), []) as unknown[];
    if (denoms.length < 2) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { reason: "fewer than two denominations — cannot compare two bet levels" });
    }
    const a = Math.min(this.indexA, denoms.length - 1);
    let bI = Math.min(this.indexB, denoms.length - 1);
    if (a === bI) bI = pyMod(a + 1, denoms.length);

    ctx.mark(py`BET LEVEL A = ${at(denoms, a)}`);
    const [openedA, shownA] = await this.pickBet(ctx, a);
    const shotA = await this.shootPaytable(ctx, "paytable-bet-a");

    ctx.mark(py`BET LEVEL B = ${at(denoms, bI)}`);
    const [openedB, shownB] = await this.pickBet(ctx, bI);
    const shotB = await this.shootPaytable(ctx, "paytable-bet-b");

    const landed = (shown: unknown, want: unknown): boolean => {
      if (shown === null || shown === undefined) return false;
      const s = tryFloat(shown);
      const w = tryFloat(want);
      return s !== null && w !== null && Math.abs(s - w) < 1e-9;
    };

    if (!(openedA && openedB)) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        shots: ctx.shots,
        reason: "the denomination list never opened — cannot compare two "
          + "bet levels (open_bet_list checked visiblePopups)",
      });
    }
    if (!(shotA && shotB)) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        shots: ctx.shots,
        reason: "the info / paytable surface never opened on this build",
      });
    }
    if (!(landed(shownA, at(denoms, a)) && landed(shownB, at(denoms, bI)))) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
        shots: ctx.shots,
        facts: { bet_a: shownA, bet_b: shownB, bet_a_asked: at(denoms, a), bet_b_asked: at(denoms, bI) },
        reason: py`the bet did not change as asked: picked ${at(denoms, a)} then ${at(denoms, bI)}, the game showed ${shownA} then ${shownB} — the two paytable shots are not at the bets they claim`,
      });
    }
    const facts: Dict = {
      bet_a: shownA, bet_b: shownB,
      bet_ratio: pyFloat(shownA) ? pyRound(pyFloat(shownB) / pyFloat(shownA), 6) : null,
      index_a: a, index_b: bI,
      divisor: or(GAME.LINE_DIVISOR, GAME.PAYLINES),   // the order lineBet() divides by
      line_bet_a: GAME.lineBet(pyFloat(at(denoms, a))),
      line_bet_b: GAME.lineBet(pyFloat(at(denoms, bI))),
      denom_count: denoms.length,
    };
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
      { facts, shots: ctx.shots });
  }
}

export class BuyBonusLuckySpin extends Scenario {    // Joker JK-63..JK-67 (games/joker/test-suites/jk-features.yml)
  readonly name = "buy-bonus-lucky-spin";
  readonly description = "Buy the bonus, answer the LUCKY SPIN WHEEL, and play the bought free spins out. "
    + "The game-specific half of Buy Bonus on Joker: the purchase awards the spins AND "
    + "raises a wheel offering x1 / x3 / x12, and the bridge never makes that choice "
    + "(bridge-api.md 12.5). `dismissPopups()` deliberately never closes `luckySpin`, "
    + "so a scenario that buys and walks away leaves the game waiting forever -- which "
    + "is why `buy-bonus` cannot be used here and this exists.\n"
    + "`wheel`: 'keep' takes the multiplier already shown (the default, x3), 'spin' "
    + "spins for a new one. Either way the choice is CLICKED, never dismissed.\n"
    + "Sequence: buyBonus -> [buy_level] -> buy_purchase -> buy_confirm -> the purchase "
    + "round (carries `freeSpinPackage`) -> wait for the wheel -> keep/spin -> drain the "
    + "free spins -> settle to base.\n"
    + "Exposes price / price_ratio / paid_stake / package_spins, the wheel "
    + "(wheel_shown, wheel_choice, lucky_multiplier), and the bonus (fs_rounds, "
    + "bonus_total, expanded_rounds, expanded_reels, capped).";
  static readonly PARAMS = ["wheel", "level", "respin_cap", "wheel_timeout_ms"];

  readonly wheel: string;               // 'keep' | 'spin'
  readonly level: number | null;        // buyBonus:level:<i> shortcut, or null to take the panel's stake
  readonly respinCap: number;
  readonly wheelTimeoutMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(BuyBonusLuckySpin, p);
    this.wheel = param(p, "wheel", "keep");
    this.level = param<number | null>(p, "level", null);
    this.respinCap = param(p, "respin_cap", 120);
    this.wheelTimeoutMs = param(p, "wheel_timeout_ms", 30000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const g = GAME as any;
    const start = ctx.nowS();
    const facts: Dict = {};
    try {
      if (!GAME.HAS_BUY_BONUS) throw new Unreached("this build has no Buy Bonus");
      const [btnBuy, btnPurchase, btnConfirm] = requireButtons("buy_bonus", "buy_purchase", "buy_confirm");

      ctx.mark("BUY BONUS: open the panel");
      if (!(await ctx.clickButton(btnBuy, { optional: true, timeoutMs: 15000 }))) {
        throw new Unreached(`\`${btnBuy}\` never became clickable`);
      }
      if (this.level !== null) {
        await ctx.clickButton(GAME.button("buy_level", { i: this.level }) as string, { optional: true,
          timeoutMs: 8000 });
      }
      const info = await ctx.eval(`() => { const b = window.__gameTestBridge;
                const s = b.getSnapshot ? b.getSnapshot() : {};
                return { price: s.buyBonusSelectedPrice, bet: s.betAmount,
                         balance: s.balance }; }`);
      const price = get(info, "price");
      const bet = get(info, "bet");
      Object.assign(facts, {
        price, bet,
        price_ratio: truthy(price) && truthy(bet) ? pyFloat(price) / pyFloat(bet) : null,
        balance_before: get(info, "balance"),
      });

      ctx.mark(py`BUY BONUS: purchase at ${price} (bet ${bet})`);
      await ctx.clickButton(btnPurchase, { optional: true, timeoutMs: 10000 });
      if (!(await ctx.clickButton(btnConfirm, { optional: true, timeoutMs: 10000 }))) {
        throw new Unreached("the Buy Bonus confirm sheet never offered `buy_confirm`");
      }

      const bought = await ctx.observeBet(ctx.betTimeoutMs, "base");
      const pkg = or(get(bought.raw || {}, "freeSpinPackage"), {});
      Object.assign(facts, {
        paid_stake: bought.stake, package_spins: get(pkg, "freeSpins"),
        package_name: get(pkg, "name"), awarded: bought.total_free_spin,
      });

      // ── the wheel. THE STEP THAT MUST NOT BE SKIPPED ───────────────────────────────
      const label = GAME.popup("lucky_spin");
      const shown = truthy(label) ? await ctx.waitForPopup(label, this.wheelTimeoutMs) : false;
      Object.assign(facts, { wheel_shown: truthy(shown) });
      if (shown) {
        const role = this.wheel === "spin" ? "lucky_spin" : "lucky_keep";
        ctx.mark(py`LUCKY SPIN: choosing ${this.wheel}`);
        Object.assign(facts, { wheel_choice: this.wheel });
        if (!(await ctx.clickButton(GAME.button(role) as string, { optional: true, timeoutMs: 10000 }))) {
          throw new Unreached(`the Lucky Spin wheel offered no \`${role}\` -- the game is now `
            + "waiting on a choice nobody can make");
        }
      } else {
        // Not fatal: report it. A build that skips the wheel still owes the free spins.
        Object.assign(facts, { wheel_choice: null });
        ctx.mark("LUCKY SPIN: no wheel appeared within the timeout -- continuing to the spins");
      }

      const rounds: Bet[] = [bought];
      const fs = await playOutBonus(ctx, bought, { cap: this.respinCap, settle: false });
      rounds.push(...fs.slice(1));
      const spins = rounds.slice(1);
      const expanded: Array<[number, unknown[]]> = spins
        .filter((r) => truthy(g.expandedReels(r.raw || {})))
        .map((r): [number, unknown[]] => [r.seq, g.expandedReels(r.raw || {}) as unknown[]]);
      const mults = new Set(spins.map((r) => g.luckyMultiplier(r.raw || {}) as number | null)
        .filter((m): m is number => m !== null));
      Object.assign(facts, {
        fs_rounds: spins.length,
        bonus_total: pyRound(sum(spins.map((r) => r.payout)), 4),
        capped: rounds.some((r) => r.has_max_win),
        expanded_rounds: expanded.length,
        expanded_reels: pySorted(new Set(expanded.flatMap(([, ids]) => ids))),
        lucky_multiplier: mults.size === 1 ? pySorted(mults)[0] : pySorted(mults),
        multiplier_constant: mults.size <= 1,
      });
      const stop = await settleToBase(ctx, { label: "buy-bonus-lucky-spin" });
      Object.assign(facts, stop);
      return new ScenarioResult(this.name, true, bought, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { bought: bought.seq, terminal: rounds.length ? rounds[rounds.length - 1].seq : bought.seq },
        facts, shots: ctx.shots,
      });
    } catch (e) {
      if (!(e instanceof Unreached)) throw e;
      await ctx.dismissPopups();
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()],
        { facts, reason: e.message });
    }
  }
}

export const SCENARIOS: ScenarioClass[] = [
  AnteToggle, AnteFreeSpins, P1WaysWin, RainingWild, RandomMultiplier, PaytableBetScaling, BuyBonusLuckySpin,
];
