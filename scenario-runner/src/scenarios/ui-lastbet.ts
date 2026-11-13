/**
 * UI and Last Bet scenarios: Reels Glow, the feature-toggle popups (Mah_105-108), multi-language
 * (Mah_052-056), Last Bet (Mah_085/087, CR_092..CR_096) and the bonus-resume denomination swap.
 * Port of scenario_engine.py lines 4487-5682 (the toggle JS and `popupLabels` live in engine/flows).
 */
import { createHash } from "node:crypto";
import { getProfile } from "../profile/games.js";
import {
  type Dict, get, has, isDict, or, py, pyEq, pyFloat, pyInt, pyList, pyRound, pySorted, pyStr, PyRandom, PyTypeError,
  PyValueError, r, sum, tryFloat, truthy,
} from "../py.js";
import { Bet, isFreespinTrigger } from "../engine/bet.js";
import { clickStart, readGameInfo, waitSpinClickable } from "../engine/bridge.js";
import type { DriveContext } from "../engine/drive.js";
import {
  AUTO_JS, autoStopButton, loadUntilLadder, playOutBonus, playOutFreespins, playOutHoldspin, popupLabels, SPEED_JS,
} from "../engine/flows.js";
import { GAME, LANGUAGE_NAME, STRESS_LANGUAGE, SUPPORTED_LANGUAGES } from "../engine/game.js";
import { sleep } from "../engine/page.js";
import { ScenarioResult } from "../engine/result.js";
import { autoNext, openBetList } from "../engine/shared.js";
import { BadParams, checkParams, param, Scenario, type Params, type ScenarioClass } from "./base.js";

// ── local helpers ────────────────────────────────────────────────────────────────────

/** `_ladder_num` (scenario_engine.py:242): `round(float(v), 6)`, or null where float() refuses.
 * Not exported from engine/bridge.ts, so repeated here. */
function ladderNum(v: unknown): number | null {
  const f = tryFloat(v);
  return f === null ? null : pyRound(f, 6);
}

/** `LANGUAGE_NAME.get(code, code)`. */
function langName(code: unknown): unknown {
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(LANGUAGE_NAME, code) ? LANGUAGE_NAME[code] : code;
}

/** `code in LANGUAGE_NAME`. */
function isLanguage(code: unknown): boolean {
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(LANGUAGE_NAME, code);
}

/** `str(e)` for a caught exception. */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : pyStr(e);
}

/** `random.Random(seed)`'s seed as CPython reads it: an int as itself (a bool as 0/1), a str through
 * version-2 seeding (`int.from_bytes(a + sha512(a).digest())`). */
function pySeed(seed: unknown): bigint {
  if (typeof seed === "bigint") return seed;
  if (typeof seed === "boolean") return seed ? 1n : 0n;
  if (typeof seed === "number" && Number.isInteger(seed)) return BigInt(seed);
  if (typeof seed === "string") {
    const a = Buffer.from(seed, "utf8");
    const digest = createHash("sha512").update(a).digest();
    return BigInt(`0x${Buffer.concat([a, digest]).toString("hex")}`);
  }
  // Python accepts a float seed (it seeds from hash(float)); not reproduced here.
  throw new PyTypeError(py`seed ${seed} is not an int or a str -- only those seed the same draws as Python here`);
}

/** Both present and equal to 1e-6. `float()` on junk raises, as in the Python. */
function same(a: unknown, b: unknown): boolean {
  return a !== null && a !== undefined && b !== null && b !== undefined && Math.abs(pyFloat(a) - pyFloat(b)) < 1e-6;
}

/** `float(v)`, or null where Python's `except (TypeError, ValueError)` would catch. */
function num(v: unknown): number | null {
  return tryFloat(v);
}

/** Is `v` a value of `ladder`? */
function onLadder(v: unknown, ladder: number[]): boolean {
  return ladder.some((x) => same(v, x));
}

// ── Reels Glow ───────────────────────────────────────────────────────────────────────

export class ReelsGlow extends Scenario {    // Mah_041 / 042
  readonly name = "reels-glow";
  readonly description =
    "Reels Glow: spin and observe whether the Reels-Glow anticipation fires per the rule " +
    "(glows when the FreeSpin! symbol lands across the qualifying reels; NOT when the last " +
    "bonus is on the final reel). The forced BO paytable (run-level `paytable:`) makes the " +
    "FreeSpin! symbol land in the chosen reels; that usually TRIGGERS free spins, so this " +
    "captures the base spin (the glow) then drains any bonus so settle can't hang. " +
    "Video-graded per case (at: spin).";
  static readonly PARAMS = ["spins"];
  readonly spins: number;

  constructor(p: Params = {}) {
    super();
    checkParams(ReelsGlow, p);
    this.spins = param(p, "spins", 1);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    let last: Bet | null = null;
    for (let i = 0; i < this.spins; i++) {
      last = await ctx.baseSpin();                       // the glow plays during THIS base spin
      if (isFreespinTrigger(last)) {
        await playOutFreespins(ctx, last);               // forced FreeSpin! usually triggers FS -> drain it
      } else {
        await ctx.settle();
      }
    }
    // What LANDED, so a case can prove its forced paytable applied before grading a glow it can only
    // see on video. null on a profile that names no glow symbol or cannot read the grid -- which
    // reads as "cannot verify" rather than silently as zero.
    const raw = (last !== null ? or(last.raw, {}) : {}) as Dict;
    const g = GAME as any;
    const premium = g.SYM_GLOW_PREMIUM ?? null;
    const canRead = typeof g.reelsWith === "function";
    const scatterReels: number[] | null = canRead ? g.reelsWith(raw, g.SYM_SCATTER) : null;
    const premiumReels: number[] | null = premium !== null && canRead ? g.reelsWith(raw, premium) : null;
    return new ScenarioResult(this.name, true, last, last !== null ? [last] : [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: last !== null ? { spin: last.seq } : {},
        facts: {
          bonus_reels_hit: scatterReels !== null ? scatterReels.length : null,
          bonus_reels: scatterReels,
          p1_reels_hit: premiumReels !== null ? premiumReels.length : null,
          p1_reels: premiumReels,
          // 1-BASED reel numbers, as the design docs say them
          p1_on_reel2: premiumReels !== null ? premiumReels.includes(2) : null,
          p1_on_reel3: premiumReels !== null ? premiumReels.includes(3) : null,
        },
      });
  }
}

// ---------------------------------------------------------------------------
// Feature-toggle POPUPS (Mah_105-108). The subject of these cases is the on-screen notification, so
// the popup itself is a VIDEO claim; every toggle is MARKED so the timestamp-mode ACTION TIMELINE
// pins exactly when it happened. Where the bridge exposes the feature's own state (speed mode,
// isAutoPlay) it is captured as a FACT, so "the feature really toggled" stays an EXACT check and only
// "the notification appeared" is left to the AI. (SPEED_JS / AUTO_JS / popupLabels: engine/flows.)
// ---------------------------------------------------------------------------

export class ToggleTurbo extends Scenario {    // Mah_105
  readonly name = "toggle-turbo";
  readonly description =
    "Toggle Turbo ON and then back OFF from the main screen, dwelling after each click so " +
    "the 'Turbo Enabled' / 'Turbo Disabled' notification sits in frame long enough to be " +
    "read. Places no bet. Exposes facts.speed_after_on / speed_after_off — the bridge's " +
    "MODEL-truth speed mode, so the toggle itself is an exact check — plus the popup labels " +
    "the bridge saw. Unreached when the Turbo button never becomes clickable.";
  static readonly PARAMS = ["dwell_ms"];
  readonly dwellMs: number;      // hold after each toggle so the notification is recorded

  constructor(p: Params = {}) {
    super();
    checkParams(ToggleTurbo, p);
    this.dwellMs = param(p, "dwell_ms", 2000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    await ctx.startCapture("popups");
    const before = await ctx.uiRead(SPEED_JS, "UNKNOWN");
    ctx.mark("TURBO ON: click the Turbo button");
    const on = await ctx.clickButton(GAME.button("turbo") as string, { optional: true, timeoutMs: 15000, dwellMs: this.dwellMs });
    const afterOn = await ctx.uiRead(SPEED_JS, "UNKNOWN");
    ctx.mark("TURBO OFF: click the Turbo button a second time");
    const off = on
      ? await ctx.clickButton(GAME.button("turbo") as string, { optional: true, timeoutMs: 15000, dwellMs: this.dwellMs })
      : false;
    const afterOff = await ctx.uiRead(SPEED_JS, "UNKNOWN");
    const labels = popupLabels(await ctx.endCapture("popups"));
    if (!on) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { reason: "turbo button never became clickable" });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
      facts: {
        speed_before: before, speed_after_on: afterOn,
        speed_after_off: afterOff, toggled_off: off,
        // BOOLEANS, because a `stream:` assert interpolates the fact's VALUE straight into a Python
        // expression: comparing the raw modes gives `NORMAL != TURBO`, two bare names, and the
        // evaluator dies with NameError rather than answering. Compute the comparison here and
        // assert the answer. (Same reason `won_by_feature` exists alongside `won_via`.)
        speed_changed_on: !pyEq(afterOn, before),
        speed_restored_off: pyEq(afterOff, before),
        // tells a failed toggle apart from a build with no getter at all -- without it both look
        // like "the speed did not change"
        has_speed_getter: !pyEq(before, "UNKNOWN") && !pyEq(afterOn, "UNKNOWN"),
        popup_events: labels.join(",") || "none",
      },
    });
  }
}

export class ToggleAuto extends Scenario {    // Mah_107
  readonly name = "toggle-auto";
  readonly description =
    "Start Auto Play (auto -> auto:count -> auto:confirm), let ONE auto spin play so it is " +
    "genuinely running, then STOP it from the main screen — dwelling after the start and " +
    "the stop so the 'Auto Play Enabled' / 'Auto Play Disabled' notification is recorded. " +
    "Exposes facts.auto_started / auto_stopped / spins_played (from the bridge's isAutoPlay " +
    "plus the collected responses), so the state change is an exact check and only the " +
    "notification is left to the AI. Unreached when auto never starts.";
  static readonly PARAMS = ["count_index", "spins_before_stop", "dwell_ms"];
  readonly countIndex: number;          // auto:count:0 == 10 spins
  readonly spinsBeforeStop: number;
  readonly dwellMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(ToggleAuto, p);
    this.countIndex = param(p, "count_index", 0);
    this.spinsBeforeStop = param(p, "spins_before_stop", 1);
    this.dwellMs = param(p, "dwell_ms", 2000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    await ctx.startCapture("popups");
    ctx.mark("AUTO PLAY ON: confirm the autoplay popup");
    const opened = await ctx.clickButton(GAME.button("auto") as string, { optional: true, timeoutMs: 15000,
      dwellMs: ctx.stepDwellMs });
    let confirmed = false;
    if (opened) {
      await ctx.clickButton(GAME.button("auto_count", { i: this.countIndex }) as string, { optional: true,
        timeoutMs: 10000, dwellMs: ctx.stepDwellMs });
      confirmed = await ctx.clickButton(GAME.button("auto_confirm") as string, { optional: true, timeoutMs: 10000,
        dwellMs: this.dwellMs });
    }
    const stOn: Dict = (await ctx.uiRead(AUTO_JS, {})) || {};
    const rounds: Bet[] = [];
    if (confirmed) {
      for (let i = 0; i < this.spinsBeforeStop; i++) {
        try {
          await autoNext(ctx, rounds);      // a real auto spin (client fires it; we observe)
        } catch {
          break;
        }
      }
    }
    ctx.mark("AUTO PLAY OFF: stop Auto Play from the main screen");
    const stopClick = confirmed
      ? await ctx.clickButton(autoStopButton() as string, { optional: true, timeoutMs: 20000, dwellMs: this.dwellMs })
      : false;
    // POLL for up to 5 s, do not sample once. The client turns auto off when it has finished the
    // spin that was running at the stop, which can outlast the 2 s dwell -- a single read then
    // reported "still on" for a stop that worked (same rule as `confirmStopped`).
    let stOff: Dict = {};
    for (let i = 0; i < 20; i++) {
      stOff = (await ctx.uiRead(AUTO_JS, {})) || {};
      if (!truthy(get(stOff, "on"))) break;
      await sleep(250);
    }
    let idle = false;
    try {
      await waitSpinClickable(ctx.page, ctx.spinTimeoutMs);
      idle = true;
    } catch {
      // not idle -- reported as returned_to_idle: false
    }
    const labels = popupLabels(await ctx.endCapture("popups"));
    const started = truthy(get(stOn, "on")) || rounds.length > 0;
    if (!started) {
      return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          reason: "auto play never started (auto/auto:confirm not clickable)",
        });
    }
    return new ScenarioResult(this.name, true, rounds.length ? rounds[rounds.length - 1] : null, rounds,
      ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: {
          auto_started: started, auto_stopped: !truthy(get(stOff, "on")),
          stop_clicked: stopClick, spins_played: rounds.length,
          returned_to_idle: idle,
          popup_events: labels.join(",") || "none",
        },
      });
  }
}

export class StackedPopups extends Scenario {    // Mah_108
  readonly name = "stacked-popups";
  readonly description =
    "Fire the main-screen feature toggles BACK-TO-BACK with no pause — each present " +
    "feature ON then OFF, repeated `cycles` times — so several notifications are requested " +
    "within a second or two of each other. Places NO bet and never starts Auto Play: the " +
    "case is about the notifications, and a spin would only add animation between them. " +
    "Which toggles are used is PROBED from the build (see _CANDIDATES): a feature this " +
    "build does not have is skipped instantly, because waiting on an absent button would " +
    "insert multi-second gaps into the very burst being tested. Every tap is MARKED, so the " +
    "ACTION TIMELINE — not a hard-coded prompt — tells the AI what was toggled and in what " +
    "order. A background poll samples the visible-popup list throughout; afterwards the game " +
    "is left IDLE so the notifications can auto-close. Every feature gets an EVEN number of " +
    "taps, so the burst restores itself; the screen is cleared at the end. Exposes " +
    "facts.features_used / toggles_landed / popup_order / max_concurrent_popups / " +
    "popup_samples / popups_cleared_after_idle; overlap and ORDER are themselves visual " +
    "claims, graded from the clip. Unreached when fewer than 3 toggles land (nothing " +
    "stacked, so there is nothing to judge).";
  static readonly PARAMS = ["idle_s", "click_timeout_ms", "min_toggles", "cycles"];
  readonly idleS: number;              // quiet tail: the notifications should auto-close here
  readonly clickTimeoutMs: number;     // SHORT: a burst must not stall on one dead button
  readonly minToggles: number;
  readonly cycles: number;             // ON/OFF rounds -- more notifications in one burst, and an
                                       // even tap count per feature (self-restoring)

  constructor(p: Params = {}) {
    super();
    checkParams(StackedPopups, p);
    this.idleS = param(p, "idle_s", 6.0);
    this.clickTimeoutMs = param(p, "click_timeout_ms", 4000);
    this.minToggles = param(p, "min_toggles", 3);
    this.cycles = param(p, "cycles", 2);
  }

  /** Main-screen toggles that raise their own enable/disable notification. PROBED, not assumed:
   * Mystic has no Ante Bet, but a build that gains one is picked up with no code change. */
  get CANDIDATES(): string[] {
    return GAME.FEATURE_TOGGLES.map((role) => GAME.button(role)).filter((n): n is string => truthy(n));
  }

  /** One burst click: NO dwell on purpose -- these taps must land back-to-back, which is the
   * situation the case is about. Every other multi-step interaction uses ctx.stepDwellMs so the
   * montage catches each step; here a dwell would destroy the thing being tested. */
  private async tap(ctx: DriveContext, name: string, timeoutMs: number | null = null): Promise<number> {
    ctx.mark(`TOGGLE: ${name}`);
    return (await ctx.clickButton(name, { optional: true, timeoutMs: or(timeoutMs, this.clickTimeoutMs),
      dwellMs: 0 })) ? 1 : 0;
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // PROBE first (instant, no waiting): burst only on features this build actually has.
    const present: string[] = [];
    for (const n of this.CANDIDATES) if (await ctx.hasButton(n)) present.push(n);
    await ctx.startCapture("popups");
    ctx.startPopupWatch();
    let landed = 0;
    ctx.mark(py`BURST: toggle ${present.join(", ") || "no"} feature(s) ON/OFF x${this.cycles}, `
      + "no pause between clicks");
    for (let c = 0; c < this.cycles; c++) {
      for (const name of [...present, ...present]) {   // first pass turns each ON, second turns each back OFF
        landed += await this.tap(ctx, name);
      }
    }
    ctx.mark("IDLE: no further input");
    await ctx.dwell(pyInt(this.idleS * 1000));
    const watch = await ctx.stopPopupWatch();
    const left = await ctx.popups();                       // read BEFORE cleaning up
    // A still of the settled screen. Asking "is this frame free of notifications?" of ONE
    // full-resolution image is a far easier question than "did it close during the tail?" of a
    // video, which an AI has answered wrongly on a clip that plainly showed it closing.
    await ctx.screenshot("after-idle");
    const labels = popupLabels(await ctx.endCapture("popups"));
    // every feature got an EVEN number of taps, so the burst restores itself -- but a click that
    // silently failed would leave Turbo on, so check the one feature whose state IS readable.
    if (present.includes("turbo") && pyEq(await ctx.uiRead(SPEED_JS, "UNKNOWN"), "TURBO")) {
      await ctx.clickButton(GAME.button("turbo") as string, { optional: true, timeoutMs: 10000, dwellMs: 0 });
    }
    await ctx.dismissPopups();                             // clean screen for the next run
    // ⚠ On Mystic these notifications are NOT reported in `visiblePopups` — probed live 2026-09-02:
    // toggling Turbo raises a visible toast while the snapshot stays `[]`. So the popup watch sees
    // nothing, and "no popups are left" is TRUE ON EVERY BUILD, including one where a notification
    // really is stuck. Reporting that as a pass is the vacuous-assert trap. When the watch never saw
    // a popup, say UNKNOWN (null) instead of true and let the case report unreached — the screen
    // claims then rest on the image/video asserts, which can actually see a toast.
    const order = watch.order as string[];
    const bridgeSeesPopups = (watch.max_concurrent as number) > 0 || truthy(order) || truthy(labels);
    const facts: Dict = {
      features_used: present.join(",") || "none", toggles_landed: landed,
      cycles: this.cycles, popup_order: order.join(",") || "none",
      max_concurrent_popups: watch.max_concurrent, popup_samples: watch.samples,
      notifications_visible_to_bridge: bridgeSeesPopups,
      popups_cleared_after_idle: bridgeSeesPopups ? !left.length : null,
      popups_left: left.join(",") || "none",
      popup_events: labels.join(",") || "none",
    };
    if (landed < this.minToggles) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          facts,
          reason: py`only ${landed} feature toggles landed (features present: ${facts.features_used}) -- nothing stacked`,
        });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], { facts, shots: ctx.shots });
  }
}

// ---------------------------------------------------------------------------
// Multi-language (Mah_052-056). The language is baked into the LAUNCH URL, so every one of these is
// driven by re-launching, never by an in-game switch: the runner's `language:` on a run picks the
// language the game LOADS in, and ctx.setLanguage() re-launches mid-scenario. All three tour the
// same text-bearing surfaces (ctx.tourUi) so one clip covers every screen the case names, and all
// the verdicts are necessarily AI/video ones -- "the text is in Thai", "nothing overflows its frame"
// and "no glyph is clipped" are perception claims with no server or bridge equivalent.
// ---------------------------------------------------------------------------

/**
 * Resolve a QA-written language selection to an ordered list of codes + a human description.
 *
 * Testing all 16 languages across 6 surfaces is 96 AI calls and 16 re-launches, so a suite must be
 * able to say how much it wants to pay. Accepted `spec` forms:
 *
 *     null            -> [] (caller falls back to the single loaded language)
 *     "all"           -> every supported language
 *     "random"        -> one at random          | "random:4" -> four at random
 *     "th"            -> that one language
 *     ["th","my","ja"]-> exactly these, in this order
 *
 * RANDOM IS SEEDED, always. A sample that cannot be replayed is not a test: the run that fails must
 * be the run you can re-run. With no `seed` one is DRAWN and returned in the description (and put in
 * facts.seed), so a failure can be pinned by copying it back into the suite. `include` codes are
 * always present in a random sample — that is how a sweep keeps a known-good baseline (en) and the
 * stress language (my) in every draw.
 *
 * Raises ValueError on an unknown code, so a typo fails at build time and not as a bad token.
 */
export function selectLanguages(spec: unknown = null, seed: unknown = null, pool: unknown = null,
  include: unknown = null): [string[], string] {
  const codes = (truthy(pool) ? pyList(pool) : SUPPORTED_LANGUAGES.map(([c]) => c)) as string[];
  const keep = pyList(or(include, [])).filter((c) => truthy(c)) as string[];
  for (const c of keep) {
    if (!isLanguage(c)) throw new PyValueError(py`unknown language in include: '${c}'`);
  }
  const supported = () => pySorted(Object.keys(LANGUAGE_NAME)).join(", ");

  if (spec === null || spec === undefined || spec === "") return [[], "the language the run loaded in"];
  if (Array.isArray(spec)) {
    const picked = spec.map((c) => pyStr(c).trim());
    const bad = picked.filter((c) => !isLanguage(c));
    if (bad.length) {
      throw new PyValueError(`unknown language(s): ${bad.join(", ")} -- supported: ${supported()}`);
    }
    return [picked, `the ${picked.length} listed languages`];
  }
  const s = pyStr(spec).trim().toLowerCase();
  if (s === "all") return [codes, `all ${codes.length} supported languages`];
  if (s.startsWith("random")) {
    let n = 1;
    if (s.includes(":")) {
      try {
        n = pyInt(s.slice(s.indexOf(":") + 1));
      } catch (e) {
        if (e instanceof PyValueError) throw new PyValueError(py`bad random count in '${spec}' -- use e.g. 'random:3'`);
        throw e;
      }
    }
    if (seed === null || seed === undefined) {
      seed = new PyRandom(BigInt(Date.now())).randrange(1000000);   // drawn, then REPORTED so it can be pinned
    }
    const rest = codes.filter((c) => !keep.includes(c));
    n = Math.max(0, Math.min(n, codes.length));
    let picked = [...keep.slice(0, n), ...new PyRandom(pySeed(seed)).sample(rest, Math.max(0, n - keep.length))];
    const chosen = new Set(picked);
    picked = codes.filter((c) => chosen.has(c));          // stable order, not draw order
    return [picked, py`${picked.length} random of ${codes.length} (seed=${seed})`];
  }
  if (!isLanguage(s)) throw new PyValueError(py`unknown language '${spec}' -- supported: ${supported()}`);
  return [[s], `the single language ${s}`];
}

export class LanguageTour extends Scenario {    // Mah_052 / 054 / 055
  readonly name = "language-tour";
  readonly description =
    "Walk every text-bearing surface of the game in whatever language the run LOADED in " +
    "(set `language:` on the run): the base screen, the bet list, Settings, the Info / " +
    "Tutorial guide, the Auto Play panel and History, pausing on each. Places no bet. One " +
    "clip therefore holds every screen a language check has to look at — the translation " +
    "(052), the auto text scaling (054) and clipped/missing glyphs (055) — with each " +
    "surface MARKED so the ACTION TIMELINE names what is on screen when. Exposes " +
    "facts.language / language_name / surfaces / surface_count. Unreached if no surface " +
    "beyond the base screen opens.";
  static readonly PARAMS = ["dwell_ms", "languages", "seed", "include", "surfaces"];
  readonly dwellMs: number;
  readonly languages: unknown;     // null -> just the language the run loaded in (the cheap path)
  readonly seed: unknown;
  readonly include: unknown;
  readonly surfaces: any;          // null -> all six; a list trims the tour (the other cost lever)
  readonly codes: string[];
  readonly selection: string;

  constructor(p: Params = {}) {
    super();
    checkParams(LanguageTour, p);
    this.dwellMs = param(p, "dwell_ms", 2000);
    this.languages = param(p, "languages", null);
    this.seed = param(p, "seed", null);
    this.include = param(p, "include", null);
    this.surfaces = param(p, "surfaces", null);
    // Resolve at CONSTRUCTION so a bad code / bad spec fails the run immediately, before a browser is
    // driven or a token minted. `selectLanguages` raises ValueError; the runner reports it.
    [this.codes, this.selection] = selectLanguages(this.languages, this.seed, null, this.include);
    if (truthy(this.surfaces)) {
      // (validated against the MYSTIC profile's surfaces, as the Python does)
      const known = new Set(getProfile("mystic").UI_SURFACES.map(([label]) => label));
      const bad = pyList(this.surfaces).filter((s) => !known.has(s as string));
      if (bad.length) {
        throw new PyValueError(`unknown surface(s): ${bad.map(pyStr).join(", ")} -- known: ${pySorted([...known]).join(", ")}`);
      }
    }
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const codes = this.codes.length ? this.codes : [or(ctx.language, "en") as string];
    // every shot is labelled "<code> <name> - <surface>", so ONE image assert can use {shot.label}
    // to tell the model which language AND which screen it is looking at.
    const toured: string[] = [];
    const failed: string[] = [];
    const perLang = new Map<string, number>();
    let switches = 0;
    let prev: string | null = null;      // the language we switched FROM (null for the first tour)
    for (const code of codes) {
      if (code !== ctx.language) {
        const prevLang = ctx.language;
        try {
          await ctx.setLanguage(code);
        } catch (e) {                    // one dead language must not lose the others
          failed.push(code);
          console.log(`    ! language '${code}' failed to launch: ${errText(e)}`);
          continue;
        }
        if (truthy(prevLang)) {          // a real mid-session switch, not the first load
          prev = prevLang;
          switches += 1;
        }
      }
      const name = langName(code);
      ctx.mark(py`LANGUAGE ${code}: ${name}`
        + (truthy(prev) ? py` (switched from ${langName(prev)})` : ""));
      // The label carries the language, the screen AND what we switched from, because that is all
      // one image assert gets to work with: {shot.label} is the only per-shot text in the prompt.
      // Naming the previous language is what lets the stale-string question be "is any THAI still
      // here" instead of the much weaker "is any other language here".
      const tag = py`${code} ${name}` + (truthy(prev) ? py` (switched from ${prev} ${langName(prev)})` : "");
      const seen = await ctx.tourUi({ dwellMs: this.dwellMs, prefix: `${tag} - `, surfaces: this.surfaces });
      perLang.set(code, seen.length);
      if (seen.length) toured.push(code);
    }
    const surfacesEach = perLang.size ? Math.min(...perLang.values()) : 0;
    const facts: Dict = {
      language: codes[0], language_name: langName(codes[0]),
      languages: toured.join(",") || "none", language_count: toured.length,
      selection: this.selection, requested: codes.length,
      switches,                                    // mid-session language changes performed
      supported_count: SUPPORTED_LANGUAGES.length, // for "did we cover them ALL?"
      stress_language: STRESS_LANGUAGE,
      failed: failed.join(",") || "none",
      surfaces: [...perLang.entries()].map(([k, v]) => `${k}:${v}`).join(",") || "none",
      surface_count: surfacesEach, shot_count: ctx.shots.length,
    };
    if (!toured.length || surfacesEach < 2) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          facts, shots: ctx.shots,
          reason: !toured.length
            ? py`no language toured (failed: ${facts.failed})`
            : py`only ${surfacesEach} surface(s) opened per language (${facts.surfaces}) -- not enough on screen to judge `
              + "a translation",
        });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], { facts, shots: ctx.shots });
  }
}

export class LanguageSwitch extends Scenario {    // Mah_053
  readonly name = "language-switch";
  readonly description =
    "Change the language while the game is open and re-enter it: tour the UI in the FROM " +
    "language, then re-launch in the TO language and tour the SAME surfaces again. BOTH " +
    "ends are QA-settable per run — `params: {from: <code>, to: <code>}` — because which " +
    "pair leaks a stale string is exactly what a tester wants to vary; `from` defaults to " +
    "the run's `language:` (or whatever is loaded), `to` to Burmese. Shots are labelled " +
    "'BEFORE <surface>' / 'AFTER <surface>' so an image assert can grade either pass. The " +
    "failure this case is really about is a leftover untranslated string, visible only by " +
    "comparing the passes. Exposes facts.from_language / to_language / from_name / to_name " +
    "/ surfaces_before / surfaces_after / switched. Unreached if a re-launch fails.";
  static readonly PARAMS = ["to", "dwell_ms", "from", "from_"];
  readonly from_: any;
  readonly to: string;
  readonly dwellMs: number;

  constructor(p: Params = {}) {
    super();
    // `from` was a Python keyword, so it arrived through **kw: every key but `to` / `dwell_ms`, with
    // `from` popped first and `from_` only when `from` was falsy. Whatever is left is refused with
    // the Python's own message, before checkParams (which then has nothing left to refuse).
    const kw: Params = Object.fromEntries(Object.entries(p).filter(([k]) => k !== "to" && k !== "dwell_ms"));
    const pop = (k: string): unknown => {
      const v = Object.prototype.hasOwnProperty.call(kw, k) ? kw[k] : null;
      delete kw[k];
      return v;
    };
    const fromKw = pop("from");
    this.from_ = truthy(fromKw) ? fromKw : pop("from_");
    if (Object.keys(kw).length) throw new BadParams(`unexpected params: ${pySorted(Object.keys(kw)).join(", ")}`);
    checkParams(LanguageSwitch, p);
    const to = param(p, "to", STRESS_LANGUAGE);
    for (const code of [this.from_, to]) {
      if (truthy(code) && !isLanguage(code)) {
        throw new BadParams(py`unknown language '${code}' -- supported: ${pySorted(Object.keys(LANGUAGE_NAME)).join(", ")}`);
      }
    }
    if (truthy(this.from_) && pyEq(this.from_, to)) {
      throw new BadParams(py`from and to are both '${to}' -- a switch test needs two languages`);
    }
    this.to = to;
    this.dwellMs = param(p, "dwell_ms", 2000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    // an explicit `from` wins over whatever the page happens to be loaded in, so the run does not
    // silently depend on the language a PREVIOUS run left behind (it is sticky across runs).
    if (truthy(this.from_) && this.from_ !== ctx.language) {
      try {
        await ctx.setLanguage(this.from_);
      } catch (e) {
        return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
          [start, ctx.nowS()], {
            facts: { from_language: this.from_, to_language: this.to },
            reason: py`could not launch the FROM language '${this.from_}': ${errText(e)}`,
          });
      }
    }
    const first = or(ctx.language, "en") as string;
    ctx.mark(py`BEFORE: the game is loaded in ${langName(first)} (${first})`);
    const before = await ctx.tourUi({ dwellMs: this.dwellMs, prefix: "BEFORE " });
    try {
      await ctx.setLanguage(this.to);
    } catch (e) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          facts: { from_language: first, to_language: this.to },
          shots: ctx.shots,
          reason: py`could not re-launch in '${this.to}': ${errText(e)}`,
        });
    }
    ctx.mark(py`AFTER: re-entered in ${langName(this.to)} (${this.to})`);
    const after = await ctx.tourUi({ dwellMs: this.dwellMs, prefix: "AFTER " });
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events, [start, ctx.nowS()], {
      shots: ctx.shots,
      facts: {
        from_language: first, to_language: this.to,
        from_name: langName(first),
        to_name: langName(this.to),
        surfaces_before: before.join(",") || "none",
        surfaces_after: after.join(",") || "none",
        switched: true,
      },
    });
  }
}

export class LanguageSweep extends Scenario {    // Mah_056
  readonly name = "language-sweep";
  readonly description =
    "Re-launch the game in EVERY supported language in turn and pause on the base screen " +
    "in each, so one clip carries the whole set for a translation sweep. Each language is " +
    "MARKED with its code and name, so the ACTION TIMELINE tells the AI exactly which " +
    "language each stretch of the clip is supposed to be — without that it would have to " +
    "guess, and a wrong-language screen is precisely what this case looks for. This is the " +
    "longest scenario in the suite (one Playsite re-launch per language). Exposes " +
    "facts.languages / language_count / failed. Unreached if fewer than half re-launch.";
  static readonly PARAMS = ["languages", "dwell_ms"];
  readonly languages: string[];
  readonly dwellMs: number;

  constructor(p: Params = {}) {
    super();
    checkParams(LanguageSweep, p);
    const languages = param<unknown>(p, "languages", null);
    this.languages = (languages === null ? SUPPORTED_LANGUAGES.map(([c]) => c) : pyList(languages)) as string[];
    this.dwellMs = param(p, "dwell_ms", 2000);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const done: string[] = [];
    const failed: string[] = [];
    for (const code of this.languages) {
      try {
        if (code !== ctx.language) {             // already loaded in it -> don't pay for a re-launch
          await ctx.setLanguage(code);
        }
      } catch (e) {
        failed.push(code);
        console.log(py`    ! language '${code}' failed to launch: ${errText(e)}`);
        continue;
      }
      ctx.mark(py`LANGUAGE ${code}: ${langName(code)}`);
      await ctx.dwell(this.dwellMs);             // hold the base screen in this language
      await ctx.screenshot(py`${code} ${langName(code)}`);   // one still per language
      done.push(code);
    }
    const facts: Dict = {
      languages: done.join(",") || "none", language_count: done.length,
      failed: failed.join(",") || "none", requested: this.languages.length,
    };
    if (done.length * 2 < this.languages.length) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          facts, shots: ctx.shots,
          reason: py`only ${done.length}/${this.languages.length} languages launched (failed: ${facts.failed})`,
        });
    }
    return new ScenarioResult(this.name, true, null, [], ctx.timeline, ctx.events,
      [start, ctx.nowS()], { facts, shots: ctx.shots });
  }
}

// ── Last Bet ─────────────────────────────────────────────────────────────────────────

export class LastBetBeforeRounds extends Scenario {    // Mah_087 (+ the in-bonus half of Mah_085)
  readonly name = "last-bet-before-rounds";
  readonly description =
    "Trigger free spins and record the WHOLE bonus, because Last Bet shows ITSELF here: " +
    "per the spec it flashes for ~0.5s before each free-spin round, carrying the bet " +
    "type / stake / bet amount / win of the round that just finished. That auto-display " +
    "needs no button, which is why this case is automatable while the rest of the Last " +
    "Bet module is not (the bridge exposes no `lastBet` control to OPEN the panel). " +
    "Every free-spin round is MARKED so the ACTION TIMELINE lines each 0.5s window up " +
    "with the response that preceded it; grade with high_fps, since 0.5s is two frames " +
    "at the default sampling. Exposes facts.fs_rounds / fs_stake_zero / trigger_stake / " +
    "last_round_payout. Unreached if no bonus is reached within the spin budget.";
  static readonly PARAMS = ["cap"];
  readonly cap: number;        // base spins to try before giving up on a natural trigger

  constructor(p: Params = {}) {
    super();
    checkParams(LastBetBeforeRounds, p);
    this.cap = param(p, "cap", 60);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    let trigger: Bet | null = null;
    let spins = 0;
    for (let i = 0; i < this.cap; i++) {
      const b = await ctx.baseSpin();
      spins += 1;
      if (isFreespinTrigger(b)) {
        trigger = b;
        break;
      }
      await ctx.settle();
    }
    if (trigger === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          facts: { base_spins: spins },
          reason: `no free-spin trigger in ${spins} base spins`,
        });
    }
    ctx.mark("BONUS TRIGGERED");
    const rounds: Bet[] = [trigger];
    for (;;) {                        // drain the bonus, marking every round boundary
      const nxt = await ctx.freeSpin();
      rounds.push(nxt);
      ctx.mark(py`FREE SPIN ROUND ${rounds.length - 1} finished `
        + py`(stake ${nxt.stake}, win ${nxt.payout})`);
      if (!(nxt.free_spins > 0 && !pyEq(nxt.game_state, GAME.BASE_STATE))) break;
    }
    await ctx.settle();
    const fs = rounds.slice(1);
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { trigger: trigger.seq },
        facts: {
          base_spins: spins, fs_rounds: fs.length,
          trigger_stake: trigger.stake,
          fs_stake_zero: fs.every((x) => x.stake === 0),
          last_round_payout: rounds[rounds.length - 1].payout,
          bonus_total: pyRound(sum(fs.map((x) => x.payout)), 4),
        },
      });
  }
}

const DENOM_INFO_JS = `() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return { denoms: (s && s.denominations) || [],
                     betIndex: (b.getBetIndex ? b.getBetIndex() : -1),
                     betAmount: (b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)) }; }`;

const BET_AMOUNT_JS = `() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return (b.getBetAmount ? b.getBetAmount() : (s ? s.betAmount : null)); }`;

const LAST_BET_INFO_JS = `() => {
            const b = window.__gameTestBridge;
            const gi = b.getGameInfo ? b.getGameInfo() : null;
            const r = (gi && gi.result) ? gi.result : gi;      // inner object or envelope
            const s = b.getSnapshot ? b.getSnapshot() : {};
            let src = null;
            try { src = b.diagnose ? (b.diagnose() || {}).gameInfoSource : null; } catch (e) {}
            return { lastBet: r ? (r.lastBet || null) : null,
                     pending: r ? (r.pending || null) : null,
                     captured: b.isGameInfoCaptured ? !!b.isGameInfoCaptured() : null,
                     source: src,
                     betAmount: s.betAmount }; }`;

export class LastBetRestore extends Scenario {    // CR_092..CR_095 / DV-67..DV-70
  readonly name = "last-bet-restore";
  readonly description =
    "LAST BET IS A RESUME BEHAVIOUR, NOT A PANEL. Play one base round, RELOAD the " +
    "game as a player closing and reopening it, and check that the session comes " +
    "back showing the round they last played: the same stake, the same win, and the " +
    "bet control sitting at that stake rather than at the configured default.\n" +
    "Drives `reload_and_start()`, then reads `getGameInfo().lastBet` through the " +
    "profile's parser and compares it to the round actually played.\n" +
    "`result_hold_s` (3s) holds the SETTLED ROUND in frame before the reload: " +
    "`settle()` only waits for the game to be spinnable, so without it the clip cuts " +
    "from the win straight to the reload and the montage may never sample the result " +
    "the reopened screen is compared against.\n" +
    "GRADE IT WITH `video: at: [played, restored]`, not with a still. The clip then " +
    "runs from the last round's response -- so its WIN ANIMATION is in frame, which " +
    "a screenshot cannot hold -- through the reload and on to the restored screen " +
    "sitting ready. That turns the AI's job into a COMPARISON of two phases of one " +
    "clip (`is the bet / win / winning line the same after the reload as before it`) " +
    "rather than reading absolute values off a still, which is the reading the " +
    "bridge already owns exactly. `restored` is a MOMENT mark, so the span reaches " +
    "the end of the scenario; a bare `at: played` stops at the next response and " +
    "cuts the reload off. A `restored` screenshot is still taken for cases that want " +
    "a full-resolution look at the re-entry alone.\n" +
    "⚠ TWO TRAPS, both load-bearing, both from the bridge docs:\n" +
    "  * `getGameInfo()` must be the PRE-START CAPTURE. Right after the Start click " +
    "    `GameController.applyLastBet()` sets `gameInfo.lastBet = null`, so a LIVE " +
    "    read reports no last bet on a session that restored perfectly. " +
    "    `isGameInfoCaptured()` is the gate, published as facts.info_captured -- " +
    "    without it every comparison below is None == None and greens on nothing.\n" +
    "  * `lastBet` does NOT spell its fields like a BET response on every game " +
    "    (contract §5), which is why it is read through `Bet.parse` / `GAME.*` and " +
    "    never by raw key.\n" +
    "`outcome` picks which round to leave on record: 'win' | 'lose' | " +
    "'freespin_trigger' | 'any'. On 'win'/'lose'/'any' a bonus met while hunting is " +
    "DRAINED, so the round left on record is a plain base round; on " +
    "'freespin_trigger' the scenario deliberately leaves the terminal round of a " +
    "FINISHED bonus there instead. Either way the bonus is played out before the " +
    "reload, so `pending` is empty and the client RESTORES rather than resumes -- if " +
    "it ever resumes, this case is grading a screen it did not ask for, and " +
    "facts.resumed_rounds says so.\n" +
    "⚠ 'freespin_trigger' MEANS TRIGGERED FROM THE BASE GAME, NOT BOUGHT. The name is " +
    "explicit because a game with Buy Bonus has two ways into the same feature and " +
    "they do NOT cost the same: a trigger charges the base stake, a purchase charges " +
    "a price that is a multiple of it. A restore case that conflated them would " +
    "assert the wrong figure. The bought route is a separate outcome ('buy_bonus', " +
    "not implemented -- it needs the panel flow `buy-bonus` already drives).\n" +
    "⚠ ON 'freespin_trigger' THE STAKE IS READ OFF THE TRIGGER, not the terminal " +
    "round. Bonus rounds cost nothing, so their `stake` is 0 and `base_stake()` " +
    "falls back to it when `baseStake` is absent -- reading the terminal round would " +
    "publish 0 on a healthy game and fail every stake assert for the wrong reason. " +
    "The trigger is the round the player actually paid. `terminal_base_stake` " +
    "publishes the other figure so a captured BET response can settle which one " +
    "`lastBet` echoes.\n" +
    "`change_bet_after` is CR_095: after the round, pick a DIFFERENT bet amount in " +
    "the bet list (client-side, no spin), then reopen. A bet change sends nothing to " +
    "the server, so the record is still the round PLAYED and the game must come back " +
    "on that stake, not the one merely selected (facts.changed_bet_to vs " +
    "restored_bet_amount; bet_moved_after_round is the gate).\n" +
    "`swap_denomination` (Grandma TC-085): after the round, CLOSE the game, switch " +
    "the agent's bet levels in BO to the automation ladder (11/22/33, which does " +
    "not contain the bet just played), reopen once the game server serves it, and " +
    "check the game opens on the NEW list's DEFAULT bet (facts.new_default_bet / " +
    "bet_opens_at_new_default; played_bet_in_new_ladder must be False for the case " +
    "to mean anything). Unreached when BO is not configured or the new list never " +
    "arrives. Needs `teardown: reset-denomination`. A swap while a bonus is still " +
    "OWED makes the reload a RESUME -- that is `bonus-resume-denomination-swap`.\n" +
    "Exposes played_stake / played_base_stake / played_payout, restored_* for each, " +
    "stake_restored / payout_restored / bet_opens_at_last_stake, and the gates " +
    "info_captured / last_bet_present / resumed_rounds. On 'freespin_trigger' it " +
    "adds trigger_stake / trigger_base_stake / terminal_base_stake -- the trigger " +
    "names are `hold-spin-complete`'s and `last-bet-before-rounds`', deliberately: " +
    "one concept must not have two names across two scenarios.";
  static readonly PARAMS = ["outcome", "max_spins", "dwell_s", "change_bet_after", "result_hold_s",
    "swap_denomination", "settle_s", "propagation_timeout_s", "poll_s"];
  readonly outcome: string;          // which round to leave on record before reloading
  // Change the DENOMINATION LIST (the bet levels on offer) in BO while the game is closed, to the
  // automation ladder (11/22/33), which does not contain the bet just played. Grandma TC-085: when
  // the new list lacks the last round's bet, the game reopens on the new list's DEFAULT bet. Needs
  // `teardown: reset-denomination` — the ladder is shared state.
  readonly swapDenomination: boolean;
  readonly settleS: number;                  // wait after the BO write (Rule 20)
  readonly propagationTimeoutS: number;      // reopen until the new list is served
  readonly pollS: number;
  readonly maxSpins: number;
  // The restored screen has to SIT IN FRAME long enough to be compared against the round before the
  // reload, so the scenario settles, marks, and then holds this long.
  readonly dwellS: number;
  readonly changeBetAfter: boolean;
  // Seconds the SETTLED ROUND is held on screen before anything else happens. `settle()` only waits
  // until the game is spinnable again, which can be the moment the win presentation finishes -- so
  // without this the clip cuts from the win straight to the reload, and the AI montage (one
  // composite per second) may never sample the result it is asked to compare the reopened screen
  // against. This is the first phase of every prompt in the suite.
  readonly resultHoldS: number;

  constructor(p: Params = {}) {
    super();
    checkParams(LastBetRestore, p);
    this.outcome = param(p, "outcome", "any");
    this.swapDenomination = param(p, "swap_denomination", false);
    this.settleS = param(p, "settle_s", 5.0);
    this.propagationTimeoutS = param(p, "propagation_timeout_s", 30.0);
    this.pollS = param(p, "poll_s", 3.0);
    this.maxSpins = param(p, "max_spins", 60);
    this.dwellS = param(p, "dwell_s", 3);
    this.changeBetAfter = param(p, "change_bet_after", false);
    this.resultHoldS = param(p, "result_hold_s", 2.0);
  }

  private matches(b: Bet): boolean {
    if (this.outcome === "win") return b.payout > 0;
    if (this.outcome === "lose") return b.payout === 0;
    return true;
  }

  /**
   * Move the bet to a denomination that is NOT the one just played, and return what it landed on —
   * or null when this build would not move it. Non-fatal by design: `changed_bet_to` null reads as
   * "this half was not exercised" (the case gates on `bet_moved_after_round`) rather than as a
   * restore failure.
   *
   * ⚠ THREE WAYS THIS HANGS, all of them measured or documented, all handled here:
   * * **The row is below the fold.** `bet:row:<i>` is indexed by POSITION and the list scrolls;
   *   Dragon's bridge doc says "may be below the fold — `scrollBetRowIntoView(i)` first". Without
   *   the scroll the row never becomes clickable.
   * * **The row is the CURRENT one.** "The current row is non-interactable" — so a target chosen
   *   without knowing the current bet can be a row that can never be clicked. `getBetIndex()` may
   *   also answer `-1` (unknown), which an index-only choice reads as "not row 0". The target is
   *   therefore chosen by VALUE against the live bet amount, which is always readable.
   * * **A click that waits the default budget.** `clickButton` falls back to `spinTimeoutMs` (60 s)
   *   and RAISES when not optional. Two of those in a row is two minutes that looks like a hang.
   *   Every click here is `optional` with its own short budget.
   */
  private async pickOtherDenomination(ctx: DriveContext): Promise<number | null> {
    const info = await ctx.eval(DENOM_INFO_JS);
    const denoms = pyList(or(get(info, "denoms"), [])).map(pyFloat);
    if (denoms.length < 2) return null;
    const before0 = get(info, "betAmount");
    const before = before0 !== null ? pyFloat(before0) : null;
    const cur0 = get(info, "betIndex");
    const cur = typeof cur0 === "number" && Number.isInteger(cur0) && cur0 >= 0 && cur0 < denoms.length ? cur0 : null;
    // Pick by VALUE: the one thing that cannot pick the non-interactable current row.
    const found = denoms.findIndex((d, i) => i !== cur && (before === null || Math.abs(d - before) > 1e-9));
    const target = found >= 0 ? found : null;
    if (target === null) return null;
    const picked = denoms[target];
    if (await openBetList(ctx)) {
      console.log(py`  last-bet : bet list -- row ${target} (${picked}), was ${before}`);
      ctx.mark("BET LIST: pick a different bet");
      await ctx.scrollBetRow(target);               // the row may be below the fold
      await ctx.clickButton(GAME.button("bet_row", { i: target }) as string, { optional: true, timeoutMs: 15000 });
      await ctx.clickButton(GAME.button("bet_close") as string, { optional: true, timeoutMs: 8000 });
    } else {
      // No list on this build: the stake controls step the bet one denomination at a time.
      const steps = target - (or(cur, 0) as number);
      const role = steps > 0 ? "stakeIncrease" : "stakeDecrease";
      console.log(py`  last-bet : stepping ${steps > 0 ? "up" : "down"} ${Math.abs(steps)}x to ${picked}`);
      ctx.mark(`BET: step the stake ${steps > 0 ? "up" : "down"}`);
      for (let i = 0; i < Math.abs(steps); i++) {
        if (!(await ctx.clickButton(role, { optional: true, timeoutMs: 10000, dwellMs: ctx.stepDwellMs }))) break;
      }
    }
    await ctx.settle();
    const landed0 = await ctx.eval(BET_AMOUNT_JS);
    if (landed0 === null || landed0 === undefined) return null;
    const landed = pyFloat(landed0);
    // It has to have MOVED. Returning the unchanged bet would make `bet_moved_after_round` the only
    // thing standing between a silent no-op and a green.
    if (before !== null && Math.abs(landed - before) <= 1e-9) {
      console.log(py`  last-bet : BET DID NOT MOVE (still ${landed}) -- the change half is untested`);
      ctx.mark("BET CHANGE attempted");
      return null;
    }
    return landed;
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    const rounds: Bet[] = [];
    let played: Bet | null = null;
    let trigger: Bet | null = null;
    let bonusPayout: number | null = null;     // the WHOLE bonus's win -- see where it is set
    for (let i = 0; i < this.maxSpins; i++) {
      const b = await ctx.baseSpin();
      rounds.push(b);
      if (isFreespinTrigger(b)) {
        // Play it out either way -- a bonus still OWED at the reload makes the client resume, which
        // is a different behaviour from restoring a last bet.
        const fs = await playOutBonus(ctx, b);
        rounds.push(...fs.slice(1));
        if (this.outcome === "freespin_trigger") {
          // The terminal round of the finished bonus is what the session comes back showing, so it
          // is the round on record -- but NOT where the stake is read, and NOT where the WIN is read
          // either.
          //
          // THE BONUS'S WIN IS NOT ON ITS TERMINAL ROUND. Every round of a bonus reports only its
          // own payout, and the last one very often pays 0 -- the feature ends on whatever spin
          // takes the counter to zero, winning or not. Measured on Joker's captured terminal free
          // spin: `payout: 0` while the bonus paid 39.6 (`totalFreeSpinPayout` /
          // `accumulatedWinAmount`). So `played_payout` below is 0 on a game behaving perfectly, and
          // a case that asserted `played_payout > 0` went red for the wrong reason. Summing the
          // rounds is the mechanism-free way to say it: the server's own total field is spelled
          // differently per game (and does not exist on some), while every round's `payout` is
          // already parsed here.
          await ctx.settle();
          trigger = b;
          played = rounds[rounds.length - 1];
          // `fs.slice(1)`, NOT `fs`: `playOutBonus` returns [trigger, ...free spins...], and the
          // trigger is a PAID base round whose own line win is not part of what the bonus paid.
          // Excluding it matches the server's own field -- Joker spells it `totalFreeSpinPayout` /
          // `accumulatedWinAmount`, and the captured terminal free spin reads 39.6 there for a bonus
          // whose own terminal round paid 0.
          bonusPayout = pyRound(sum(fs.slice(1).map((x) => x.payout)), 4);
          break;
        }
        continue;
      }
      await ctx.settle();
      if (this.outcome !== "freespin_trigger" && this.matches(b)) {
        played = b;
        break;
      }
    }
    if (played === null) {
      return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          facts: { spins_played: rounds.length },
          reason: py`no '${this.outcome}' round in ${rounds.length} spins`,
        });
    }
    // THE STAKE COMES FROM THE ROUND THAT WAS PAID FOR. On a bonus that is the TRIGGER: its rounds
    // cost nothing, so a bonus round's `stake` is 0, and `baseStake()` falls back to `stake` when
    // `baseStake` is absent -- so reading it off the terminal round yields 0 on a game behaving
    // perfectly, and every stake assert goes red for the wrong reason. The trigger is the round the
    // player actually paid, and its base stake is the figure a restore shows.
    // ⚠ Whether a terminal bonus round carries its own `baseStake` is UNVERIFIED on Dragon VIP (no
    // BET response captured yet). Both figures are published so a case can say which it means, and
    // so capturing one response settles it without touching this code.
    // HOLD THE RESULT IN FRAME. The round is settled; give the clip a few seconds of it before the
    // bet change / reload, so phase 1 of the comparison has something to sample.
    // ⚠ MARK TEXT IS READ BY THE MODEL. In a timestamp-mode assert every mark is appended to the
    // prompt as the ACTION TIMELINE, so a mark that names the stake or the win, or says the screen
    // "must match", hands the AI the answer it is being asked to read. The marks here name the PHASE
    // only; the values go to the console for whoever is watching the run.
    if (truthy(this.resultHoldS)) {
      console.log(py`  last-bet : round settled -- stake ${played.stake}, win ${played.payout}`);
      ctx.mark("BEFORE: the round has finished; this is the game before it is closed");
      await sleep(this.resultHoldS * 1000);
    }
    const stakeSource = trigger !== null ? trigger : played;
    const playedBase = GAME.baseStake(or(stakeSource.raw, {}) as Dict);
    const terminalBase = GAME.baseStake(or(played.raw, {}) as Dict);
    let changedTo: number | null = null;
    if (truthy(this.changeBetAfter)) {
      changedTo = await this.pickOtherDenomination(ctx);
      console.log(py`  last-bet : bet moved to ${changedTo} after the round (played at ${playedBase})`);
      ctx.mark("BET CHANGED after the round");
    }
    console.log(py`  last-bet : reloading -- played stake ${played.stake}, win ${played.payout}`);
    let swap: Dict = {};
    let newLadder: number[] = [];
    let ladderReady: boolean | null = null;
    let resumed: Bet[];
    if (truthy(this.swapDenomination)) {
      // Close, change the bet levels on offer in BO, reopen only once the game server serves the
      // new list (evidence, not a timer -- see loadUntilLadder), then Start. Any bonus met while
      // hunting was already played out above, so nothing is owed: a RESTORE.
      ctx.mark("CLOSED: the game is closed");
      await ctx.stopClient();
      swap = await ctx.setAutomationDenomination(this.settleS);
      if (!truthy(swap)) {
        return new ScenarioResult(this.name, false, played, rounds, ctx.timeline, ctx.events,
          [start, ctx.nowS()], {
            round_windows: ctx.windows(),
            facts: { spins_played: rounds.length, swap_applied: false },
            reason: "BO is not configured, so the bet levels cannot be changed",
          });
      }
      newLadder = pyList(or(get(swap, "bet_levels"), [])).map(ladderNum).filter((x): x is number => x !== null);
      console.log(py`  last-bet : bet levels now ${newLadder} (default ${get(swap, "default_bet")})`);
      ctx.mark("BET LEVELS CHANGED while the game was closed");
      const [ready, seen, loads] = await loadUntilLadder(ctx, newLadder, this.propagationTimeoutS, this.pollS);
      ladderReady = ready;
      await clickStart(ctx.page);
      ctx.mark("REOPENED: the game is open again");
      resumed = [];
      if (!ladderReady) {
        return new ScenarioResult(this.name, false, played, rounds, ctx.timeline, ctx.events,
          [start, ctx.nowS()], {
            round_windows: ctx.windows(),
            facts: {
              spins_played: rounds.length, swap_applied: true,
              new_ladder: newLadder, client_ladder: seen,
              client_on_new_ladder: false, ladder_loads: loads,
            },
            reason: py`the game server never served the new bet levels ${newLadder} within `
              + py`${this.propagationTimeoutS}s of the BO change (${loads} load(s), `
              + py`still ${seen}) -- a BO -> game-server propagation problem, not a `
              + "game defect",
          });
      }
    } else {
      ctx.mark("CLOSE AND REOPEN the game");
      resumed = await ctx.reloadAndStart();
    }
    // THE PRE-START CAPTURE. `getGameInfo()` is the copy taken before Start precisely because Start
    // destroys `lastBet`; `isGameInfoCaptured()` says whether that copy exists at all.
    const info = await ctx.eval(LAST_BET_INFO_JS);
    // THE CLIP IS A COMPARISON, so the restored screen has to SIT IN FRAME long enough to be
    // compared against the round before the reload -- not merely appear. Settle first (the re-entry
    // is still drawing when `startGame` resolves), THEN mark the moment it is ready, THEN hold.
    // Marking before the settle would timestamp a loading screen, and the AI would be told to
    // compare against pixels that are still animating.
    await ctx.settle();
    const restoredAt = ctx.nowS();
    ctx.mark("AFTER: the reopened game is ready");
    await sleep(this.dwellS * 1000);
    await ctx.screenshot("restored");
    const raw = get(info, "lastBet");
    // Read it through the PROFILE, never by raw key: `lastBet` does not always spell its fields the
    // way a BET response does (contract §5), and the profile's aliases are what reconcile the two.
    // Parsed with a seq of -1 so it can never be mistaken for a round of the stream.
    const lb = isDict(raw) ? Bet.parse(raw, -1) : null;
    const restoredBase = isDict(raw) ? GAME.baseStake(raw) : null;
    const betAmount = get(info, "betAmount");
    const facts: Dict = {
      spins_played: rounds.length,
      // the round that was left on record
      played_stake: played.stake, played_base_stake: playedBase,
      // ⚠ THE ROUND'S OWN PAYOUT, which on `outcome: freespin_trigger` is the TERMINAL bonus round's
      // -- frequently 0, because a bonus ends on whatever spin empties the counter. Assert
      // `played_bonus_payout` instead on that outcome.
      played_payout: played.payout,
      // The WHOLE bonus's win (trigger + every free spin), or null when the outcome was not a bonus.
      // This is the figure a player would call "what the bonus paid".
      played_bonus_payout: bonusPayout,
      // On `outcome: freespin_trigger` these split: `played_base_stake` above is the TRIGGER's (the
      // round the player paid for), while this is the terminal bonus round's own figure -- 0 on a
      // game whose bonus rounds are free and carry no `baseStake`. Exposed, not asserted: capturing
      // one BET response says which one `lastBet` actually echoes.
      terminal_base_stake: terminalBase,
      // `trigger_*` are `hold-spin-complete`'s and `last-bet-before-rounds`' names for the same
      // concept -- the round that AWARDED the bonus. Do not invent a third spelling.
      trigger_stake: trigger !== null ? trigger.stake : null,
      trigger_base_stake: trigger !== null ? playedBase : null,
      // null unless `change_bet_after` asked for it. A case that grades the overwrite question MUST
      // gate on this being present AND different from played_base_stake: moving the bet to the
      // denomination already selected proves nothing.
      changed_bet_to: changedTo,
      bet_moved_after_round: changedTo !== null && !same(changedTo, playedBase),
      // The failure CR_095 is looking for, stated on its own so a case can assert it false: the game
      // reopened on the value SELECTED after the round instead of the one played.
      bet_opens_at_changed_value: same(betAmount, changedTo),
      // the gates. Without these the comparisons below are None == None, which is a green.
      info_captured: get(info, "captured"),
      game_info_source: get(info, "source"),
      last_bet_present: lb !== null,
      // a reload that RESUMED a bonus is not a restore -- this case would be grading the wrong
      // screen, and says so rather than passing quietly
      resumed_rounds: resumed.length,
      // what came back
      restored_stake: lb !== null ? lb.stake : null,
      restored_base_stake: restoredBase,
      restored_payout: lb !== null ? lb.payout : null,
      restored_bet_amount: betAmount,
      stake_restored: lb !== null ? same(restoredBase, playedBase) : false,
      payout_restored: same(lb !== null ? lb.payout : null, played.payout),
      // ...and the same question against the BONUS total. Which of the two a restored `lastBet`
      // echoes is UNVERIFIED -- the terminal round's own payout, or the whole feature's. Both are
      // published so a bonus case can assert "it came back with a win" without guessing, and so one
      // live run settles it for good.
      bonus_payout_restored: bonusPayout !== null ? same(lb !== null ? lb.payout : null, bonusPayout) : null,
      // `applyLastBet` puts the bet control back on the last stake -- the visible half of the same
      // rule, and the one a player notices first
      bet_opens_at_last_stake: same(betAmount, playedBase),
      // `swap_denomination` only (null otherwise). The rule: when the NEW bet levels do not contain
      // the last round's bet, the game reopens on the new list's DEFAULT bet.
      swap_applied: truthy(this.swapDenomination) ? truthy(swap) : null,
      new_ladder: newLadder.length ? newLadder : null,
      new_default_bet: truthy(swap) ? get(swap, "default_bet") : null,
      client_on_new_ladder: ladderReady,
      // the premise: the round was played at a bet the new list does NOT offer
      played_bet_in_new_ladder: newLadder.length ? newLadder.some((x) => same(playedBase, x)) : null,
      bet_opens_at_new_default: truthy(swap) ? same(betAmount, get(swap, "default_bet")) : null,
    };
    // THE WINDOW A CASE SHOULD ASK FOR IS `at: [played, restored]`.
    //   `played`   is a ROUND mark -- the clip opens on the last round's response, so its win
    //              presentation is in frame.
    //   `restored` is a MOMENT mark, which resolves from that moment to the END of the scenario (see
    //              `resolveWindow`), so spanning to it yields played-round-start -> scenario-end:
    //              the round, the reload, and the hold above.
    // A single `at: played` would stop at the next response and cut the reload off entirely.
    return new ScenarioResult(this.name, true, played, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        marks: { played: played.seq, restored: { t: pyRound(restoredAt, 3) } },
        facts, shots: ctx.shots,
      });
  }
}

const RESUME_INFO_JS = `() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {};
            return { captured: b.isGameInfoCaptured ? !!b.isGameInfoCaptured() : null,
                     denoms: s.denominations || (b.getDenominations ? b.getDenominations() : []) || [] }; }`;

const RESUME_BET_JS = `() => {
                const s = window.__gameTestBridge.getSnapshot(); return s ? s.betAmount : null; }`;

const AFTER_BONUS_JS = `() => { const b = window.__gameTestBridge;
            const s = b.getSnapshot ? b.getSnapshot() : {}; return { betAmount: s.betAmount }; }`;

export class BonusResumeDenominationSwap extends Scenario {    // CR_096 / DV-71 + DV-73
  readonly name = "bonus-resume-denomination-swap";
  readonly description =
    "A bonus is TRIGGERED, the game is CLOSED while that bonus is still owed, the " +
    "agent's bet ladder is changed on BO, and the game is REOPENED. Two claims, one " +
    "run: (1) the RESUMED bonus keeps the stake it was triggered at -- the server " +
    "froze it in `gameInfo.pending` -- and (2) once it ends, the base game opens on " +
    "the NEW ladder's default bet, because the old stake does not exist there.\n" +
    "This is a RESUME, not a restore (`last-bet-restore`): the reload happens with a " +
    "bonus owed, so `pending` drives it. The swap goes to the automation ladder " +
    "(11, 22, 33), which shares no value with any real ladder -- that is what makes " +
    "'the old stake is not on the new ladder' true by construction, and " +
    "facts.old_bet_on_new_ladder is the gate that proves it.\n" +
    "Sequence, each step visible in `run`: base spins until a trigger -> " +
    "`stop_client()` -> `set_automation_denomination()` (BO write + a Rule 20 " +
    "settle wait) -> `open_game()` " +
    "(load + Start, no play-out) -> read `pending` -> play the resumed bonus with " +
    "`play_out_holdspin` / `play_out_freespins`, reading the bet readout on every " +
    "round through `on_round` (facts.resume_bets) -> " +
    "settle -> mark `restored` -> hold -> shoot `after_bonus`.\n" +
    "Marks: `trigger` (round), `resume_first` / `resume_last` (rounds), `restored` " +
    "(moment). Grade both claims on ONE clip with `at: [trigger, restored]` and a " +
    "phase-scoped prompt.\n" +
    "Force the trigger with a paytable fixture; unreached if none arrives in " +
    "`max_spins`, or when BO is not configured. REQUIRES " +
    "`teardown: reset-denomination`.\n" +
    "Exposes trigger_stake / trigger_base_stake, the swap (swap_applied, " +
    "new_ladder, new_default_bet, client_ladder, client_on_new_ladder, " +
    "old_bet_on_new_ladder), the resume (info_captured, is_pending_freespin, " +
    "pending_stake, pending_stake_is_trigger, resumed_rounds, resume_bets, " +
    "bet_during_bonus_is_old, bet_during_bonus_on_new_ladder) and the base game " +
    "after it (bet_after_bonus, bet_after_bonus_is_new_default, " +
    "bet_after_bonus_on_new_ladder).";
  static readonly PARAMS = ["max_spins", "dwell_s", "resume_cap", "settle_s", "propagation_timeout_s", "poll_s"];
  // after the first `settle_s`, reload (without Start) every `poll_s` until the launch payload
  // carries the new ladder, for up to `propagation_timeout_s`
  readonly propagationTimeoutS: number;
  readonly pollS: number;
  readonly maxSpins: number;     // base spins to find the trigger; a fixture makes it spin 1
  readonly dwellS: number;       // hold on the base game after the bonus, so it is filmed
  readonly resumeCap: number;    // most resumed rounds to take before calling the bonus stuck
  // Rule 20: seconds to let the BO change reach the game server before the game is reopened.
  // `client_on_new_ladder` is what PROVES it arrived; this only gives it time to.
  readonly settleS: number;

  constructor(p: Params = {}) {
    super();
    checkParams(BonusResumeDenominationSwap, p);
    this.propagationTimeoutS = param(p, "propagation_timeout_s", 30.0);
    this.pollS = param(p, "poll_s", 3.0);
    this.maxSpins = param(p, "max_spins", 20);
    this.dwellS = param(p, "dwell_s", 3);
    this.resumeCap = param(p, "resume_cap", 60);
    this.settleS = param(p, "settle_s", 5.0);
  }

  async run(ctx: DriveContext): Promise<ScenarioResult> {
    const start = ctx.nowS();
    if (ctx.bo === null) {
      return new ScenarioResult(this.name, false, null, [], ctx.timeline, ctx.events,
        [start, ctx.nowS()], { reason: "BO not configured -- this case changes the denomination on BO" });
    }
    const rounds: Bet[] = [];
    let trigger: Bet | null = null;
    for (let i = 0; i < this.maxSpins; i++) {
      const b = await ctx.baseSpin();
      rounds.push(b);
      if (isFreespinTrigger(b)) {
        trigger = b;
        break;
      }
      await ctx.settle();
    }
    if (trigger === null) {
      return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events,
        [start, ctx.nowS()], {
          round_windows: ctx.windows(),
          facts: { base_spins: rounds.length },
          reason: `no bonus trigger in ${rounds.length} base spins`,
        });
    }
    const triggerBase = GAME.baseStake(or(trigger.raw, {}) as Dict);
    // NO settle, NO drain, and deliberately NO HOLD here: the bonus must still be OWED when the game
    // closes, or the reopen has nothing to resume and the case silently becomes a restore.
    // `last-bet-restore` holds the settled round in frame before ITS reload (`result_hold_s`) so the
    // AI can sample it -- do not copy that here. The client auto-plays the feature, so every second
    // this waits is a respin the server takes off `pending`, and enough of them turn the resume into
    // a restore. Phase 1 of this clip is short on purpose.
    // ⚠ Marks are appended to the prompt as the ACTION TIMELINE: name the MOMENT only, never a
    // stake, a ladder or what the screen "should" show. The values go to the console.
    console.log(py`  denom-swap: bonus triggered at stake ${triggerBase} -- closing with it owed`);
    ctx.mark("CLOSED: free spins were won and the game was closed at once, before that spin "
      + "was shown");
    // 1. Close the game while the bonus is still owed. The client auto-plays a bonus, so it has to
    //    be gone before BO is touched, or the BO call races those rounds.
    await ctx.stopClient();

    // 2. Change the denomination in BO while nothing is open, and give the change time to reach the
    //    game server (Rule 20 -- BO answers before the game server has the event).
    const swap = await ctx.setAutomationDenomination(this.settleS);
    console.log(py`  denom-swap: ladder now ${r(get(swap, "name"))} ${get(swap, "bet_levels")} `
      + py`(default ${get(swap, "default_bet")}) -- waited ${this.settleS}s`);
    ctx.mark("BET LEVELS CHANGED while the game was closed");

    // 3. Reopen ONLY once the game server serves the new ladder — evidence, not a timer.
    //
    // ⚠ WHY NOT JUST WAIT. BO answering 200 means BO stored the change; the game server gets it
    // later, over Kafka, and how much later varies. Two runs with the SAME 5 s wait split: one
    // reopened on the new ladder (bet 11.00 after the bonus), the other on the OLD one (0.10), and
    // the second read exactly like a game that ignores the new denomination. So: load WITHOUT Start
    // — safe, the owed bonus only plays once Start is clicked — read the ladder from the launch
    // payload, and reload until it is the new one. A load that never shows it within
    // `propagation_timeout_s` is a BO -> game-server propagation problem, not a game defect, and the
    // run says so instead of failing the case.
    const want = pyList(or(get(swap, "bet_levels"), [])).map(ladderNum).filter((x): x is number => x !== null);
    const [ladderReady, seen, loads] = await loadUntilLadder(ctx, want, this.propagationTimeoutS, this.pollS);
    await clickStart(ctx.page);
    ctx.mark("REOPENED: the game is open again");
    if (!ladderReady) {
      // Still play the owed bonus out (and answer its extension offer), so the run does not leave
      // the next one mid-bonus -- then report the propagation failure as UNREACHED.
      await playOutFreespins(ctx, trigger, { cap: this.resumeCap, settle: true });
      return new ScenarioResult(this.name, false, null, rounds, ctx.timeline, ctx.events, [start, ctx.nowS()], {
        round_windows: ctx.windows(),
        facts: {
          swap_applied: truthy(swap), new_ladder: want, client_ladder: seen,
          client_on_new_ladder: false, ladder_loads: loads,
          trigger_base_stake: triggerBase,
        },
        reason: py`the game server never served the new bet ladder ${want} within `
          + py`${this.propagationTimeoutS}s of the BO change (${loads} load(s), still `
          + py`${seen}) -- a BO -> game-server propagation problem, not a game defect`,
      });
    }
    const gi = await readGameInfo(ctx.page);     // the PRE-START capture, so `pending` is intact
    const pending = or(GAME.pending(gi), {}) as Dict;
    const isPendingFreespin = GAME.pendingIsBonus(gi);
    const info = await ctx.eval(RESUME_INFO_JS);

    // 4. Play the resumed bonus out, reading the bet readout as each round arrives (after the drain
    //    the bonus is over and the readout may have moved on). Only when `pending` says a bonus is
    //    owed: waiting on a round that is not coming hangs.
    const resumeBets: unknown[] = [];
    const readBet = async (_round: Bet): Promise<void> => {
      resumeBets.push(await ctx.eval(RESUME_BET_JS));
    };

    let resumed: Bet[] = [];
    let resumeCapped = false;
    if (isPendingFreespin) {
      // Both helpers return [trigger, ...rounds]; the trigger is already in `rounds`.
      let chain: Bet[];
      if (GAME.mechanism === "hold-spin") {
        [chain, resumeCapped] = await playOutHoldspin(ctx, trigger, { cap: this.resumeCap, onRound: readBet });
      } else {
        chain = await playOutFreespins(ctx, trigger, { cap: this.resumeCap, settle: false, onRound: readBet });
      }
      resumed = chain.slice(1);
    }
    rounds.push(...resumed);

    // 5. Back to the base game, then film it.
    await ctx.settle();
    const after = await ctx.eval(AFTER_BONUS_JS);
    const restoredAt = ctx.nowS();
    ctx.mark("BONUS OVER: back in the base game");
    await sleep(this.dwellS * 1000);
    await ctx.screenshot("after_bonus");

    const newLadder = pyList(or(get(swap, "bet_levels"), [])).map(num).filter((x): x is number => x !== null);
    const clientLadder = pyList(or(get(info, "denoms"), [])).map(num).filter((x): x is number => x !== null);
    const newDefault = num(get(swap, "default_bet"));
    const pendingStake = num(has(pending, GAME.F_STAKE) ? get(pending, GAME.F_STAKE) : get(pending, "stake"));
    const bets = resumeBets.map(num);
    const betAfter = num(get(after, "betAmount"));
    const marks: Dict = { trigger: trigger.seq, restored: { t: pyRound(restoredAt, 3) } };
    if (resumed.length) Object.assign(marks, { resume_first: resumed[0].seq, resume_last: resumed[resumed.length - 1].seq });
    const facts: Dict = {
      base_spins: rounds.length - resumed.length,
      trigger_stake: trigger.stake,
      trigger_base_stake: triggerBase,
      // ── the swap. `swap_applied` is the gate: without it this is a plain resume.
      swap_applied: truthy(get(swap, "denomination_id")),
      swap_denomination_id: get(swap, "denomination_id"),
      swap_denomination_name: get(swap, "name"),
      swap_created_ladder: get(swap, "created"),
      previous_denomination_id: get(swap, "previous_denomination_id"),
      new_ladder: newLadder,
      new_default_bet: newDefault,
      new_default_level: get(swap, "default_bet_level"),
      // what the REOPENED client offers -- proves the new ladder actually reached it
      client_ladder: clientLadder,
      client_on_new_ladder: newLadder.length > 0 && pyEq(clientLadder, newLadder),
      // THE PREMISE of the case: the old stake is not a value of the new ladder. If it were, "the
      // resumed bonus kept its stake" and "the new ladder applied" would look identical.
      old_bet_on_new_ladder: onLadder(triggerBase, newLadder),
      // ── the resume. `is_pending_freespin` is the gate: false means the bonus had already ended (or
      // never opened) before the close, so nothing was resumed.
      info_captured: get(info, "captured"),
      is_pending_freespin: truthy(isPendingFreespin),
      pending_free_spins: GAME.pendingFreeSpins(gi),
      pending_stake: pendingStake,
      pending_stake_is_trigger: same(pendingStake, triggerBase),
      resumed_rounds: resumed.length,
      // the resumed feature was cut short by the max-win cap (Hold & Spin only)
      resume_capped: resumeCapped,
      resume_bets: bets,
      // the client's bet readout during EVERY resumed round was the triggering stake...
      bet_during_bonus_is_old: bets.length > 0 && bets.every((v) => same(v, triggerBase)),
      // ...and the stake the server FROZE for the resume (`gameInfo.pending`), which is the figure
      // the requirement names: "during Free Spin, the stake is the frozen one"
      bet_during_bonus_is_pending: bets.length > 0 && pendingStake !== null
        && bets.every((v) => same(v, pendingStake)),
      // ...and never a value of the new ladder (stated separately: a readout that shows a third
      // figure fails the first and passes this one, and that is worth knowing)
      bet_during_bonus_on_new_ladder: bets.some((v) => onLadder(v, newLadder)),
      // ── the base game after the bonus
      bet_after_bonus: betAfter,
      bet_after_bonus_is_new_default: same(betAfter, newDefault),
      bet_after_bonus_on_new_ladder: onLadder(betAfter, newLadder),
    };
    return new ScenarioResult(this.name, true, trigger, rounds, ctx.timeline, ctx.events,
      [start, ctx.nowS()], { round_windows: ctx.windows(), marks, facts, shots: ctx.shots });
  }
}

export const SCENARIOS: ScenarioClass[] = [
  ReelsGlow,
  ToggleTurbo,
  ToggleAuto,
  StackedPopups,
  LanguageTour,
  LanguageSwitch,
  LanguageSweep,
  LastBetBeforeRounds,
  LastBetRestore,
  BonusResumeDenominationSwap,
];
