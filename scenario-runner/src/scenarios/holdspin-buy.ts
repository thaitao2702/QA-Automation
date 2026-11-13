/**
 * Hold & Spin, Buy Bonus, Sound and Premium Reel scenarios. Port of scenario_engine.py lines
 * 6035-6765: HoldSpinComplete, HoldSpinFullGrid, sample_purchase_reveal, BuyBonus, SoundToggles,
 * PremiumReelSpin. `purchase_bonus` lives in engine/shared (`purchaseBonus`), the paid-feature
 * helpers in engine/flows.
 */
import { type Bet, entersHoldspin, isTerminal } from "../engine/bet.js";
import type { DriveContext } from "../engine/drive.js";
import { featureOn, featureState, near, playOutFreespins, playOutHoldspin, roundCarries, settleToBase,
  spinAndFinish, StopSignal, switchFeature, watchControls } from "../engine/flows.js";
import { GAME } from "../engine/game.js";
import { sleep } from "../engine/page.js";
import { requireButtons, ScenarioResult, Unreached } from "../engine/result.js";
import { purchaseBonus } from "../engine/shared.js";
import { returnToBase } from "../engine/teardowns.js";
import type { HoldSpinGame } from "../profile/mechanisms.js";
import { type Dict, get, isList, or, py, PyAttributeError, pyEq, pyRound, pySorted, pyStr, r, sum,
  truthy } from "../py.js";
import { checkParams, param, type Params, Scenario, type ScenarioClass } from "./base.js";

/** The active profile seen as Hold & Spin — Python called these methods duck-typed on `GAME`. */
function holdSpin(): HoldSpinGame {
  return GAME as unknown as HoldSpinGame;
}

/**
 * `GAME._norm(sym)`. In Python `_norm` is a staticmethod of LineGame reached through the instance;
 * here it is a static on the class, so it is looked up on the profile's constructor. A profile
 * without it (a Hold & Spin game) raises AttributeError, as `GAME._norm` did.
 */
function gameNorm(sym: unknown): string {
  const fn = (GAME.constructor as unknown as { _norm?: (s: unknown) => string })._norm;
  if (typeof fn !== "function") {
    throw new PyAttributeError(`'${GAME.constructor.name}' object has no attribute '_norm'`);
  }
  return fn(sym);
}

export class HoldSpinComplete extends Scenario {    // ZZ-30/32/33/34/35/36/38 · DV-57/58 (ante) · DV-64/65 (premium)
  readonly name = "hold-spin-complete";
  readonly description = "Spin until Hold & Spin triggers, then play the WHOLE feature out and expose every "
    + "round. The client drives the respins, so this collects them passively. Exposes "
    + "trigger + rounds for the holdspin_* invariants (coins held, counter reset on a new "
    + "coin, count-down without one, correct end) and facts.trigger_coins / final_coins / "
    + "respins_played / coins_added / grid_filled / bonus_total / capped. Unreached if "
    + "no trigger inside the spin budget -- force one with a `paytable:` fixture rather "
    + "than raising the budget, since a natural trigger is rare.\n"
    + "The feature is drained by `play_out_holdspin`, so the end is read from the game "
    + "(Rule 2) and a max-win truncation is REPORTED as facts.capped rather than passing "
    + "as a clean finish (Rule 9) -- a capped feature satisfies holdspin_ends_correctly "
    + "for the wrong reason, so a case that needs the whole feature asserts `capped == "
    + "False` and the respin LENGTH it needs.\n"
    + "`feature` (\"ante\" | \"premium:<reelId>\") plays the Hold & Spin as a bet WITH a paid "
    + "spin feature: switched on before the hunt through `switch_feature`, and back off "
    + "once the feature has ended. Every hunting spin must carry it -- the run is "
    + "unreached the moment one does not, rather than hunting at the plain odds. Adds "
    + "facts.feature_on_trigger / trigger_stake / trigger_base_stake / trigger_multiplier "
    + "/ trigger_stake_matches / feature_multiplier_configured / respin_stakes / "
    + "respin_premium_ids / off_available_in_feature (the off control sampled through "
    + "the whole feature, never waited on) / feature_on_after_bonus / feature_restored. "
    + "Force the trigger in the feature's OWN reel section (Dragon VIP: "
    + "`ante-trigger3-countdown`, `premium-trigger3-countdown`), so the trigger itself "
    + "proves the server spun the feature's reels.";
  static readonly PARAMS = ["cap", "respin_cap", "settle", "feature"];

  readonly cap: number;                 // base spins to hunt a trigger through
  readonly respinCap: number;           // respins to drain before failing loudly (Rule 13)
  readonly settle: boolean;
  readonly feature: string | null;      // null | "ante" | "premium:<reelId>", on for the whole run
  private leftOn = false;               // this run switched the feature on and has not switched it off

  constructor(p: Params = {}) {
    super();
    checkParams(HoldSpinComplete, p);
    this.cap = param(p, "cap", 200);
    this.respinCap = param(p, "respin_cap", 60);
    this.settle = param(p, "settle", true);
    this.feature = param(p, "feature", null);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    // The feature this run switched on goes back off whether the run ends or FAILS. A failed
    // drain (respin cap, timeout, rejected bet) used to skip the switch-off and hand the next
    // run a game still betting with Ante / Premium on. The failure can land INSIDE the bonus,
    // where the off control is locked, so the game is taken back to base first.
    this.leftOn = false;
    try {
      return await this.play(ctx);
    } catch (e) {
      if (this.leftOn) {
        ctx.mark(`${(this.feature as string).toUpperCase()}: the run failed -- back to base, then switch it OFF`);
        try {
          await returnToBase(ctx, 500, false);
          await switchFeature(ctx, this.feature as string, false);
        } catch (e2) {   // the original failure is the one to report
          const err = e2 as Error;
          console.log(`  hold-spin-complete: could not switch \`${this.feature}\` off after the failure: ${err?.name}: ${err?.message}`);
        }
      }
      throw e;
    }
  }

  private async play(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const feature = this.feature;
    const offRole = feature === "ante" ? "ante_off" : "premium_off";
    const facts: Dict = { feature };
    let turnedOn: unknown = false;

    const restore = async (): Promise<void> => {
      // Only what THIS run switched on, and only once the evidence is in (Rule 14).
      if (truthy(turnedOn)) {
        ctx.mark(`${(feature as string).toUpperCase()}: switch OFF again -- leaving the game as this run found it`);
        const off = await switchFeature(ctx, feature as string, false);
        this.leftOn = false;
        Object.assign(facts, { feature_restored: off.switched });
      }
    };

    if (truthy(feature)) {
      const sw = await switchFeature(ctx, feature as string, true);
      const st = await featureState(ctx);
      turnedOn = sw.clicked;
      this.leftOn = truthy(turnedOn);
      Object.assign(facts, {
        feature_switched_on: sw.switched,
        feature_multiplier_configured: feature === "ante" ? get(st, "anteMultiplier") : get(st, "premiumMultiplier"),
        feature_spin_cost: get(st, "spinCost"), feature_bet: get(st, "bet"),
      });
      if (!truthy(sw.switched)) {
        await restore();
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { round_windows: ctx.windows(), facts,
            reason: py`could not switch \`${feature}\` on -- every spin would be a plain bet` });
      }
    }
    let trigger: Bet | null = null;
    let spins = 0;
    for (let i = 0; i < this.cap; i++) {
      const b = await ctx.baseSpin();
      spins += 1;
      // `enters_holdspin`, NOT `is_freespin_trigger`: a base spin that lands the full grid pays
      // instantly and skips the respins, and `is_trigger` answers False to it by design.
      if (entersHoldspin(b)) {
        trigger = b;
        break;
      }
      await ctx.settle();
      if (truthy(feature) && !roundCarries(b, feature as string)) {
        // The forced trigger lives in the feature's own reel section, so a spin bet without
        // the feature would hunt at the plain odds for the whole budget. Stop and say so.
        await restore();
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { round_windows: ctx.windows(),
            facts: { ...facts, base_spins: spins },
            reason: py`spin ${spins} was not bet with \`${feature}\` (stake ${b.stake}, base ${GAME.baseStake((b.raw || {}) as Dict)}) although the client showed it on` });
      }
    }
    if (trigger === null) {
      await restore();
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { round_windows: ctx.windows(),
          facts: { ...facts, base_spins: spins },
          reason: py`no Hold & Spin trigger in ${spins} base spins` });
    }
    const g = holdSpin();
    const raw0 = (trigger.raw || {}) as Dict;
    ctx.mark(py`HOLD & SPIN TRIGGERED on ${g.coinCount(raw0)} coin(s)`);
    // A player must not be able to drop a paid feature out of a bonus it paid for. Sampled for
    // the whole drain in the background: a single probe at the trigger lands while the reels are
    // still turning, when the control is locked for an unrelated reason.
    const stop = new StopSignal();
    const watch = truthy(feature) ? watchControls(ctx, [offRole], stop) : null;
    let rounds: Bet[];
    let capped: boolean;
    try {
      // A full grid on the trigger skips the respins entirely (ZZ-31), a max win ends it on the
      // spot (Rule 9), and the drain fails loudly rather than returning a half-played feature
      // (Rule 13) -- all three live in the helper.
      [rounds, capped] = await playOutHoldspin(ctx, trigger, { cap: this.respinCap });
    } finally {
      stop.set();
    }
    const seen: Dict = watch ? await watch : {};
    if (truthy(this.settle)) await ctx.settle();
    const terminal = rounds[rounds.length - 1];
    const last = (terminal.raw || {}) as Dict;
    const respins = rounds.slice(1);
    const base0 = GAME.baseStake(raw0);
    const mult0 = feature === "ante" ? GAME.anteMultiplier(raw0)
      : truthy(feature) ? GAME.premiumReelMultiplier(raw0) : null;
    if (truthy(feature)) {
      Object.assign(facts, { feature_on_after_bonus: featureOn(await featureState(ctx), feature as string) });
    }
    Object.assign(facts, {
      base_spins: spins,
      trigger_coins: g.coinCount(raw0),
      final_coins: g.coinCount(last),
      coins_added: g.coinCount(last) - g.coinCount(raw0),
      respins_played: rounds.length - 1,
      respins_left: terminal.free_spins,
      grid_filled: g.gridFull(last),
      coin_total: g.coinTotal(last),
      bonus_total: pyRound(sum(rounds.map((b) => b.payout)), 4),
      // Rule 9: a truncated feature ends cleanly and passes every
      // obvious assert while showing almost nothing. Say so.
      capped,
      instant_finish: g.gridFull(raw0),
      trigger_stake: trigger.stake,
      trigger_base_stake: base0,
      // Gate every feature claim on this: the TRIGGER round was really bet with it.
      feature_on_trigger: truthy(feature) ? roundCarries(trigger, feature as string) : null,
      trigger_multiplier: mult0,
      trigger_stake_matches: truthy(mult0) ? near(trigger.stake, base0 * (mult0 as number)) : null,
      respin_stakes: pySorted(new Set(respins.map((b) => b.stake))),
      respin_premium_ids: pySorted(new Set(respins.map((b) => GAME.premiumReelId((b.raw || {}) as Dict)))),
      off_available_in_feature: truthy(feature) ? get(seen, offRole) : null,
      control_samples: get(seen, "samples"),
    });
    await restore();
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], { round_windows: ctx.windows(),
        marks: { trigger: trigger.seq, terminal: terminal.seq },
        facts });
  }
}

export class HoldSpinFullGrid extends Scenario {    // ZZ-31/37
  readonly name = "hold-spin-full-grid";
  readonly description = "The full-grid path: land every slot and the feature must pay out immediately "
    + "instead of spinning. Plays until the grid fills -- on the trigger itself (no "
    + "respin is fired at all) or during the respins -- and exposes facts.filled_on_"
    + "trigger / respins_played / final_coins / capped. Needs a forced outcome to be "
    + "practical: run it with the `fs-trigger9` paytable fixture. Unreached if the grid "
    + "never fills, which is the honest verdict for an unforced run -- and a MAX-WIN "
    + "truncation is one of the ways it can fail to fill, so the reason names it.";
  static readonly PARAMS = ["cap", "respin_cap"];

  readonly cap: number;                 // base spins to hunt a trigger through
  readonly respinCap: number;           // respins to drain before failing loudly (Rule 13)

  constructor(p: Params = {}) {
    super();
    checkParams(HoldSpinFullGrid, p);
    this.cap = param(p, "cap", 200);
    this.respinCap = param(p, "respin_cap", 60);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    let trigger: Bet | null = null;
    let spins = 0;
    for (let i = 0; i < this.cap; i++) {
      const b = await ctx.baseSpin();
      spins += 1;
      // `enters_holdspin`, NOT `is_freespin_trigger`: a base spin that lands the full grid pays
      // instantly and skips the respins, and `is_trigger` answers False to it by design.
      if (entersHoldspin(b)) {
        trigger = b;
        break;
      }
      await ctx.settle();
    }
    if (trigger === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { round_windows: ctx.windows(),
          facts: { base_spins: spins },
          reason: py`no Hold & Spin trigger in ${spins} base spins` });
    }
    const g = holdSpin();
    const raw0 = (trigger.raw || {}) as Dict;
    const filledOnTrigger = g.gridFull(raw0);
    ctx.mark(py`TRIGGER: ${g.coinCount(raw0)}/${g.GRID_SLOTS} coins`
      + (filledOnTrigger ? " -- FULL GRID, respins skipped" : ""));
    const [rounds, capped] = await playOutHoldspin(ctx, trigger, { cap: this.respinCap });
    if (g.gridFull((rounds[rounds.length - 1].raw || {}) as Dict) && !filledOnTrigger) {
      ctx.mark(py`GRID FILLED on respin ${rounds.length - 1}`);
    }
    await ctx.settle();
    const last = (rounds[rounds.length - 1].raw || {}) as Dict;
    const facts: Dict = {
      base_spins: spins, filled_on_trigger: filledOnTrigger,
      final_coins: g.coinCount(last), respins_played: rounds.length - 1,
      grid_filled: g.gridFull(last), coin_total: g.coinTotal(last),
      bonus_total: pyRound(sum(rounds.map((b) => b.payout)), 4), capped,
    };
    if (!g.gridFull(last)) {
      // Rule 9: a max-win truncation ends the feature wherever it lands, so "the grid never
      // filled" has two causes that look identical in the facts. Name the one that happened,
      // because the fixes are opposite: raise the run's cap, or force the coins harder.
      return new ScenarioResult(this.name, false, trigger, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], { round_windows: ctx.windows(), facts,
          reason: truthy(capped)
            ? py`the max-win cap truncated the feature at ${g.coinCount(last)}/${g.GRID_SLOTS} coins -- raise this run's max_win_x, the grid cannot fill under it`
            : py`grid never filled (${g.coinCount(last)}/${g.GRID_SLOTS}) -- force it with the fs-trigger9 paytable` });
    }
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], { round_windows: ctx.windows(),
        marks: { trigger: trigger.seq, terminal: rounds[rounds.length - 1].seq }, facts });
  }
}

/**
 * Read the grid the CLIENT draws for a Buy Bonus purchase, while it is still on screen.
 *
 * ⚠ This exists because the purchase round carries NO reels. Grandma Pets sends five reels of -1
 * and draws a "you triggered it" teaser itself (`games/grandma/bet-response.md` §9), so the thing
 * the player is shown lives nowhere in the response. `getDisplayedGrid()` during the presentation
 * is the only exact record of it — everything else would be an AI reading pixels.
 *
 * Polls until the displayed grid is fully populated (no `-1`, which marks a cell still hidden or
 * masked), then reports, all prefixed `reveal_`: `reveal_sampled` (a complete grid was captured at
 * all — every other fact is meaningless without it), `reveal_stable`, `reveal_distinct_grids`,
 * `reveal_bonus_reels` (how many REELS carry the scatter — the award is keyed on reels, not
 * symbols), `reveal_max_bonus_per_reel`, `reveal_wilds`, `reveal_winning_run` (whether any symbol
 * reaches reels 1, 2 AND 3 — i.e. whether the teaser accidentally shows a winning combination on a
 * round that pays nothing) and `reveal_grid`.
 *
 * A build whose bridge has no `getDisplayedGrid()` reports `reveal_sampled: False` rather than
 * raising, so the surrounding scenario still runs and the case says "could not verify".
 */
export async function samplePurchaseReveal(ctx: DriveContext, timeoutMs = 8000): Promise<Dict> {
  const scatter = pyStr(or((GAME as any).SYM_SCATTER, ""));
  const wild = pyStr(or((GAME as any).SYM_WILD, ""));
  // ⚠ TAKE THE GRID THE TEASER SETTLES ON, NOT THE FIRST ONE THAT IS FULLY DRAWN. The reveal is
  // an animation: reels land in turn, and symbols can still be swapped in after every cell has a
  // value. Sampling the first frame with no `-1` catches it mid-flight and reports whatever was
  // on screen at that instant -- which reads exactly like a game that put the wrong symbols out.
  // So: keep sampling, and accept a grid only once it has been IDENTICAL for `stable_hits` polls.
  const deadline = ctx.nowS() + timeoutMs / 1000;
  const stableHits = 3;
  const seen: unknown[][][] = [];
  let grid: unknown[][] | null = null;
  let last: unknown[][] | null = null;
  let repeats = 0;
  while (ctx.nowS() < deadline) {
    const got = await ctx.uiRead("() => { const b = window.__gameTestBridge;"
      + " return b && b.getDisplayedGrid ? b.getDisplayedGrid() : null; }");
    const full = isList(got) && got.length > 0
      && got.every((reel) => isList(reel) && reel.length > 0)
      && got.every((reel) => (reel as unknown[]).every((c) => pyStr(c) !== "-1"));
    if (full) {
      const g = got as unknown[][];
      if (pyEq(g, last)) {
        repeats += 1;
      } else {
        repeats = 1;
        last = g;
        if (!seen.some((s) => pyEq(s, g))) seen.push(g);
      }
      if (repeats >= stableHits) {
        grid = g;
        break;
      }
    }
    await sleep(150);
  }
  // Settled or not, the last complete grid is better evidence than nothing -- and
  // `reveal_stable` says which it was, so a case never reads a mid-animation frame as final.
  const stable = grid !== null;
  if (grid === null) grid = last;

  if (grid === null) {
    return {
      reveal_sampled: false, reveal_stable: false, reveal_distinct_grids: 0,
      reveal_bonus_reels: null, reveal_max_bonus_per_reel: null,
      reveal_wilds: null, reveal_winning_run: null, reveal_grid: null,
    };
  }

  const norm = grid.map((reel) => reel.map((c) => gameNorm(c)));
  const wantScatter = gameNorm(scatter);
  const wantWild = gameNorm(wild);
  const perReel = norm.map((reel) => sum(reel.map((c) => (c === wantScatter ? 1 : 0))));

  // A ways win must start at reel 1 and run left to right with no gap, so "reels 1, 2 and 3 all
  // carry the same symbol" is exactly the condition for a paying combination to exist. The wild
  // substitutes, so it counts toward any symbol. The scatter is excluded: it is the thing the
  // teaser is MADE of, and it pays by reel count rather than by a run.
  const firstThree = norm.slice(0, 3);
  let winningRun = false;
  if (firstThree.length === 3) {
    const symbols = new Set(firstThree.flat());
    symbols.delete(wantScatter);
    symbols.delete(wantWild);
    winningRun = [...symbols].some((sym) => firstThree.every((reel) => reel.some((c) => c === sym || c === wantWild)))
      || firstThree.every((reel) => reel.includes(wantWild));
  }

  return {
    reveal_sampled: true,
    reveal_stable: stable,
    // how many DIFFERENT complete grids the teaser showed. > 1 means it was still changing
    // after every cell had a value -- the reason a single early sample cannot be trusted.
    reveal_distinct_grids: seen.length,
    reveal_bonus_reels: sum(perReel.map((n) => (n > 0 ? 1 : 0))),
    reveal_max_bonus_per_reel: perReel.length ? Math.max(...perReel) : 0,
    reveal_wilds: sum(norm.flatMap((reel) => reel.map((c) => (c === wantWild ? 1 : 0)))),
    reveal_winning_run: winningRun,
    reveal_grid: grid,
  };
}

export class BuyBonus extends Scenario {    // ZZ-39..ZZ-43
  readonly name = "buy-bonus";
  readonly description = "Buy the bonus and play it out: open the Buy Bonus popup, read each package's "
    + "price against the current bet, confirm one, then collect the whole feature. The "
    + "bought round enters Hold & Spin directly, so every holdspin_* invariant applies "
    + "to it unchanged -- which IS the case that buying behaves like a natural trigger. "
    + "Drives the WHOLE round: open, select, confirm, then every respin, then back to "
    + "a spinnable base game -- the last step proven by `settle_to_base`, not assumed. "
    + "Exposes facts.packages / price / bet / price_ratio / package_picked / confirmed / "
    + "trigger_coins / respins_played / bonus_rounds / bonus_ended / bonus_total / "
    + "capped / returned_to_base. Marks `trigger` and `terminal`, so a video spans the "
    + "whole thing with `at: [trigger, terminal]`. Unreached if the build has no Buy "
    + "Bonus, the popup never opens, no package selects, or the purchase never fires.\n"
    + "The bought feature is drained by `play_out_holdspin`, exactly as a natural one "
    + "is -- which is the point of ZZ-42/43: if buying took a different code path, the "
    + "same drain and the same invariants would show it.";
  static readonly PARAMS = ["package", "respin_cap", "sample_reveal"];

  readonly package: number;
  readonly respinCap: number;           // respins to drain before failing loudly (Rule 13)
  // Read the grid the CLIENT draws for the purchase, while it is on screen.
  // ⚠ Only a build whose purchase response carries no grid needs this. Grandma Pets sends five
  // reels of -1 and draws the "you triggered it" teaser itself (`games/grandma/bet-response.md`
  // §9), so the reveal exists nowhere in the response and `getDisplayedGrid()` sampled DURING the
  // presentation is the only exact record of what the player saw.
  readonly sampleReveal: boolean;

  constructor(p: Params = {}) {
    super();
    checkParams(BuyBonus, p);
    this.package = param(p, "package", 0);
    this.respinCap = param(p, "respin_cap", 60);
    this.sampleReveal = param(p, "sample_reveal", false);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const facts: Dict = {};          // filled as we go, so a failure reports what was known by then
    try {
      const [bought, buyFacts] = await purchaseBonus(ctx, this.package);
      Object.assign(facts, {
        bet: get(buyFacts, "bet"),
        packages: get(buyFacts, "packages"),
        popup_loaded: get(buyFacts, "popup_loaded"),
        package_picked: get(buyFacts, "package_picked"),
        price: get(buyFacts, "price"),
        price_ratio: get(buyFacts, "price_ratio"),
        buy_clicked: get(buyFacts, "buy_clicked"),
        confirmed: get(buyFacts, "confirmed"),
      });

      // The teaser plays between the purchase response and the first bonus round, so it has
      // to be read NOW -- after the drain there is nothing left to see.
      if (truthy(this.sampleReveal)) {
        const rev = await samplePurchaseReveal(ctx);
        // ⚠ Spelled out rather than `facts.update(rev)`: validate_suites.py reads the
        // exposed keys out of DICT LITERALS, so facts merged from a returned dict are
        // invisible to it and every correct `{facts.reveal_*}` assert is reported as
        // "not exposed" — which is the one error class that would otherwise reach a run.
        Object.assign(facts, {
          reveal_sampled: rev.reveal_sampled,
          reveal_stable: rev.reveal_stable,
          reveal_distinct_grids: rev.reveal_distinct_grids,
          reveal_bonus_reels: rev.reveal_bonus_reels,
          reveal_max_bonus_per_reel: rev.reveal_max_bonus_per_reel,
          reveal_wilds: rev.reveal_wilds,
          reveal_winning_run: rev.reveal_winning_run,
          reveal_grid: rev.reveal_grid,
        });
      }
      const braw = (bought.raw || {}) as Dict;
      Object.assign(facts, {
        package_spins: GAME.packageSpins(braw),
        awarded: GAME.wonFreeSpins(braw),
      });

      // ⚠ WHICH DRAIN. A bought bonus is whatever bonus this game HAS: Zeus and Dragon buy a
      // Hold & Spin, Grandma buys free spins. Calling `play_out_holdspin` on a free-spin game
      // counts coins that do not exist and ends on a full grid that never comes; calling
      // `play_out_freespins` on Hold & Spin misses a FULL GRID, which ends the feature with
      // respins still owed. `play_out_bonus` picks by `GAME.mechanism`, but it drops `capped`,
      // which ZZ-42/43 grade -- so hold & spin keeps its own call and everything else takes
      // the free-spin drain (which also answers the Free Spin Extension offer).
      const g = holdSpin();
      let rounds: Bet[];
      let capped: boolean;
      let terminal: Bet;
      let last: Dict;
      if (GAME.mechanism === "hold-spin") {
        ctx.mark(py`BOUGHT BONUS: ${g.coinCount(braw)} coin(s) on entry`);
        [rounds, capped] = await playOutHoldspin(ctx, bought, { cap: this.respinCap });
        terminal = rounds[rounds.length - 1];
        last = (terminal.raw || {}) as Dict;
        ctx.mark(py`BONUS COMPLETE: ${g.coinCount(last)}/${g.GRID_SLOTS} coin(s), `
          + py`${rounds.length - 1} respin(s), ${terminal.free_spins} respin(s) left`);
        Object.assign(facts, {
          trigger_coins: g.coinCount(braw),
          final_coins: g.coinCount(last),
          coins_added: g.coinCount(last) - g.coinCount(braw),
          grid_filled: g.gridFull(last),
          respins_played: rounds.length - 1,
          respins_left: terminal.free_spins,
        });
      } else {
        ctx.mark(py`BOUGHT BONUS: ${GAME.wonFreeSpins(braw)} free spin(s) awarded`);
        rounds = await playOutFreespins(ctx, bought, { cap: this.respinCap, settle: false });
        capped = truthy(rounds[rounds.length - 1].has_max_win);
        terminal = rounds[rounds.length - 1];
        last = (terminal.raw || {}) as Dict;
        ctx.mark(py`BONUS COMPLETE: ${rounds.length - 1} free spin(s) played, `
          + py`${terminal.free_spins} left on the counter`);
        Object.assign(facts, {
          fs_rounds: rounds.length - 1,
          respins_played: rounds.length - 1,
          respins_left: terminal.free_spins,
        });
      }
      // ...and back to base, proven and then held so the idle screen is on the clip (Rule 4).
      const back = await settleToBase(ctx, { label: "buy-bonus" });
      Object.assign(facts, {
        bonus_rounds: rounds.length,
        // the feature really ended, on the game's own terms -- true of a bonus
        // that stopped halfway and never handed back, without this. `grid_full`
        // is the Hold & Spin half of "ended" and is False on every other
        // mechanism, where `is_terminal` carries it alone.
        bonus_ended: truthy(isTerminal(terminal) || (GAME.mechanism === "hold-spin" && g.gridFull(last))),
        bonus_total: pyRound(sum(rounds.map((b) => b.payout)), 4),
        capped,
        paid_stake: bought.stake,
        // A bought bonus never inherits a Premium Reel (Dragon VIP client doc §6
        // rule 7). `0` on every build without the feature.
        bought_premium_id: GAME.premiumReelId(braw),
        respin_premium_ids: pySorted(new Set(rounds.slice(1).map((b) => GAME.premiumReelId((b.raw || {}) as Dict)))),
        ...back,
      });
      return new ScenarioResult(this.name, true, bought, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], { round_windows: ctx.windows(),
          // for a suite that wants a narrower clip; the buy-bonus cases
          // use the whole scenario, since the purchase flow precedes round 0.
          marks: { trigger: bought.seq, terminal: terminal.seq },
          facts });
    } catch (e) {
      if (e instanceof Unreached) {
        await ctx.dismissPopups();      // safe when nothing is open; runs on every failing path
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { facts, reason: e.message });
      }
      throw e;
    }
  }
}

export class SoundToggles extends Scenario {    // ZZ-24/25
  readonly name = "sound-toggles";
  readonly description = "Toggle sound and music off and back on, reading the bridge's own getters "
    + "(isSoundEnabled / isMusicEnabled) around each click -- so 'the volume really "
    + "went to zero' is an EXACT check rather than something an AI has to hear. Opens "
    + "Settings if the toggles live there. Exposes facts.sound_before/after_off/"
    + "restored and the same for music. Unreached when the build exposes no sound "
    + "getters (the case cannot be answered without them).";
  static readonly PARAMS = ["via_settings"];

  static readonly _JS = "() => { const b = window.__gameTestBridge; return {"
    + " sound: b && b.isSoundEnabled ? b.isSoundEnabled() : null,"
    + " music: b && b.isMusicEnabled ? b.isMusicEnabled() : null }; }";

  readonly viaSettings: boolean;

  constructor(p: Params = {}) {
    super();
    checkParams(SoundToggles, p);
    this.viaSettings = param(p, "via_settings", true);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    if (!GAME.HAS_SOUND_GETTERS) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()],
        { reason: "no isSoundEnabled/isMusicEnabled getter -- the check would "
          + "be an AI guess at silence, so it is not attempted" });
    }
    const before = or(await ctx.uiRead(SoundToggles._JS, {}), {});
    // A settings-scoped toggle only resolves while the popup is open, and the main-screen one may
    // be COVERED by that popup -- so pick the control that matches where we are about to be.
    const inside = truthy(this.viaSettings) && truthy(GAME.button("settings")) && truthy(GAME.button("settings_sound"));
    const soundBtn = inside ? GAME.button("settings_sound") : GAME.button("sound");
    const musicBtn = inside ? GAME.button("settings_music") : null;
    if (truthy(this.viaSettings) && truthy(GAME.button("settings"))) {
      ctx.mark("SETTINGS: open");
      await ctx.clickButton(GAME.button("settings") as string, { optional: true, timeoutMs: 10000 });
    }
    ctx.mark("SOUND: toggle off");
    const toggled = await ctx.clickButton(soundBtn as string, { optional: true, timeoutMs: 10000 });
    if (truthy(musicBtn)) {                      // a build with a SEPARATE music control needs both moved
      ctx.mark("MUSIC: toggle off");
      await ctx.clickButton(musicBtn as string, { optional: true, timeoutMs: 10000 });
    }
    const afterOff = or(await ctx.uiRead(SoundToggles._JS, {}), {});
    ctx.mark("SOUND: toggle back on");
    await ctx.clickButton(soundBtn as string, { optional: true, timeoutMs: 10000 });
    if (truthy(musicBtn)) {
      ctx.mark("MUSIC: toggle back on");
      await ctx.clickButton(musicBtn as string, { optional: true, timeoutMs: 10000 });
    }
    const restored = or(await ctx.uiRead(SoundToggles._JS, {}), {});
    await ctx.dismissPopups();
    const facts: Dict = {
      toggled, sound_button: soundBtn, music_button: musicBtn,
      sound_before: get(before, "sound"), sound_after_off: get(afterOff, "sound"),
      sound_restored: get(restored, "sound"),
      music_before: get(before, "music"), music_after_off: get(afterOff, "music"),
      music_restored: get(restored, "music"),
    };
    if (!toggled) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { facts,
          reason: "the sound control never became clickable" });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], { facts });
  }
}

export class PremiumReelSpin extends Scenario {    // DV-59 / 60 / 61 / 62 / 63
  readonly name = "premium-reel-spin";
  readonly description = "Premium Reel in the base game: a plain baseline spin, then for EACH package the "
    + "session offers (`getPremiumReelOptions()`, narrowed by `reel_ids`) -- switch it "
    + "on through `switch_feature`, probe what it excludes, spin, switch it off -- then a "
    + "final plain spin at the same bet level. One play puts plain and premium rounds "
    + "side by side, which is what `premium_reel_stake` needs to prove the payout base "
    + "never moved.\n"
    + "Exposes facts.packages_configured / configured_reel_ids / configured_multipliers; "
    + "packages_tested / reel_ids_tested / multipliers_tested / stakes; the per-package "
    + "verdicts rolled up as all_engaged / all_stakes_match / all_ids_echoed / "
    + "all_multipliers_echoed / all_bases_unchanged / all_spin_costs_match / "
    + "all_switched_off (each False on an empty list, never vacuously True); what a "
    + "package excludes, probed while it was ON, as panel_blocked_while_on / "
    + "ante_blocked_while_on / ante_block_reasons; distinct_ids_played; and the plain "
    + "spins as plain_stake / plain_base / after_stake / after_premium_id / "
    + "off_spin_plain. Marks `plain` and `after`. Unreached when the profile has no "
    + "Premium Reel or the session offers no package.";
  static readonly PARAMS = ["reel_ids"];

  readonly reelIds: unknown[] | null;   // null -> every package gameInfo configures

  constructor(p: Params = {}) {
    super();
    checkParams(PremiumReelSpin, p);
    this.reelIds = param(p, "reel_ids", null);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const facts: Dict = {};
    try {
      if (!GAME.HAS_PREMIUM_REEL) {
        throw new Unreached(`${GAME.name} has no Premium Reel (profile HAS_PREMIUM_REEL=False)`);
      }
      requireButtons("buy_bonus", "buy_confirm", "premium_off", ["premium_reel", 0]);
      const options: unknown[] = or(await ctx.uiRead("() => { const b = window.__gameTestBridge;"
        + " return b && b.getPremiumReelOptions ? b.getPremiumReelOptions() : []; }", []), []);
      // Compared as TEXT: the bridge sends numbers (2, 23) and a suite may quote them ("2"),
      // which used to match nothing and report the package as not offered.
      const asked = this.reelIds === null ? null : new Set(this.reelIds.map((x) => pyStr(x)));
      const wanted = options.filter((o) => asked === null || asked.has(pyStr(get(o, "reelId"))));
      Object.assign(facts, {
        packages_configured: options.length,
        configured_reel_ids: options.map((o) => get(o, "reelId")),
        configured_multipliers: options.map((o) => get(o, "multiplier")),
      });
      if (!wanted.length) {
        // `[]` means the SESSION does not ship the feature: `settings.premiumReel` is null, so
        // the client hides it and the server would reject a premium bet (-112). That is a BO /
        // launch-config question, not a game defect -- say so, rather than reporting it as one.
        throw new Unreached(py`no Premium Reel package to test (offered ${r(options)}, asked for `
          + py`${r(this.reelIds)}). An empty list means this launch has `
          + "`settings.premiumReel: null` -- enable the feature for this player "
          + "and currency on BO first");
      }

      // Start from a PLAIN spin. A package or Ante left on by an earlier run would make the
      // baseline itself a feature spin and invert every comparison (AnteToggle's lesson).
      let st = await featureState(ctx);
      if (truthy(get(st, "premium"))) {
        ctx.mark("a Premium Reel was left ON by an earlier run -- switching it off first");
        await switchFeature(ctx, py`premium:${get(st, "premiumId")}`, false);
      }
      if (truthy(get(st, "ante"))) {
        ctx.mark("ANTE was left ON by an earlier run -- switching it off first");
        await switchFeature(ctx, "ante", false);
      }

      ctx.mark("PLAIN SPIN: no package");
      const [plain, rounds] = await spinAndFinish(ctx);
      const plainBase = GAME.baseStake((plain.raw || {}) as Dict);

      const tested: Array<Record<string, any>> = [];
      let firstSpin: Bet | null = null;
      for (const opt of wanted) {
        const rid = get(opt, "reelId");
        const mult = or(get(opt, "multiplier"), 0);
        const feature = py`premium:${rid}`;
        ctx.mark(py`PREMIUM REEL ${rid} (x${mult}): switch ON`);
        const sw = await switchFeature(ctx, feature, true);
        if (!truthy(sw.switched)) {
          // Spinning now would grade a plain spin as a premium one. Record it and move on.
          tested.push({ reel_id: rid, multiplier: mult, engaged: false });
          continue;
        }
        st = await featureState(ctx);
        // What a package EXCLUDES, probed while it is on: instant reads, never a wait on a
        // control that should be locked.
        const panelOpen = await ctx.hasButton("buy_bonus");
        const anteWhy = or(await ctx.uiRead("() => { const b = window.__gameTestBridge;"
          + " return b && b.canDo ? b.canDo('activateAnteBet') : null; }"), {});
        ctx.mark(py`PREMIUM REEL ${rid}: spin`);
        const [spin, chain] = await spinAndFinish(ctx);
        rounds.push(...chain);
        firstSpin = firstSpin ?? spin;
        const raw = (spin.raw || {}) as Dict;
        ctx.mark(py`PREMIUM REEL ${rid}: switch OFF with one tap`);
        const off = await switchFeature(ctx, feature, false);
        tested.push({
          reel_id: rid, multiplier: mult, engaged: true,
          spin_cost: get(st, "spinCost"), bet: or(get(st, "bet"), 0),
          stake: spin.stake, base: GAME.baseStake(raw),
          echoed_id: GAME.premiumReelId(raw),
          echoed_multiplier: GAME.premiumReelMultiplier(raw),
          panel_open_while_on: panelOpen,
          ante_allowed_while_on: truthy(get(anteWhy, "ok")),
          ante_block_reason: get(anteWhy, "reason"),
          switched_off: off.switched,
          bonus_rounds: chain.length - 1,
        });
      }

      ctx.mark("PLAIN SPIN again: every package switched off");
      const [after, chain] = await spinAndFinish(ctx);
      rounds.push(...chain);
      const araw = (after.raw || {}) as Dict;
      const on = tested.filter((t) => truthy(t.engaged));
      Object.assign(facts, {
        packages_tested: on.length,
        reel_ids_tested: on.map((t) => t.reel_id),
        multipliers_tested: on.map((t) => t.multiplier),
        stakes: on.map((t) => t.stake),
        all_engaged: on.length === wanted.length,
        // Every rollup is False on an empty list: "no package switched on" must never read
        // as "every package passed".
        all_stakes_match: on.length > 0 && on.every((t) => near(t.stake, t.base * t.multiplier)),
        all_ids_echoed: on.length > 0 && on.every((t) => pyEq(t.echoed_id, t.reel_id)),
        all_multipliers_echoed: on.length > 0 && on.every((t) => near(t.echoed_multiplier, t.multiplier, 1e-9)),
        all_bases_unchanged: on.length > 0 && on.every((t) => near(t.base, plainBase)),
        all_spin_costs_match: on.length > 0 && on.every((t) => near(t.spin_cost, t.bet * t.multiplier)),
        panel_blocked_while_on: on.length > 0 && !on.some((t) => truthy(t.panel_open_while_on)),
        ante_blocked_while_on: on.length > 0 && !on.some((t) => truthy(t.ante_allowed_while_on)),
        ante_block_reasons: pySorted(new Set(on.map((t) => pyStr(t.ante_block_reason)))),
        all_switched_off: on.length > 0 && on.every((t) => truthy(t.switched_off)),
        distinct_ids_played: new Set(on.map((t) => t.echoed_id)).size,
        plain_stake: plain.stake,
        plain_base: plainBase,
        after_stake: after.stake,
        after_premium_id: GAME.premiumReelId(araw),
        off_spin_plain: GAME.premiumReelId(araw) === 0 && near(after.stake, GAME.baseStake(araw)),
        bonus_rounds: sum(on.map((t) => t.bonus_rounds)),
      });
      // Leave no package on for the next run -- only now, with every fact taken (Rule 14).
      st = await featureState(ctx);
      if (truthy(get(st, "premium"))) {
        ctx.mark("CLEANUP: a package is still ON -- switching it off for the next run");
        await switchFeature(ctx, py`premium:${get(st, "premiumId")}`, false);
      }
      return new ScenarioResult(this.name, true, firstSpin ?? plain, rounds, ctx.timeline,
        ctx.events, [start, ctx.nowS()], { round_windows: ctx.windows(),
          marks: { plain: plain.seq, after: after.seq }, facts });
    } catch (e) {
      if (e instanceof Unreached) {
        await ctx.dismissPopups();
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], { facts, reason: e.message });
      }
      throw e;
    }
  }
}

export const SCENARIOS: ScenarioClass[] = [
  HoldSpinComplete, HoldSpinFullGrid, BuyBonus, SoundToggles, PremiumReelSpin,
];
