/**
 * The games: one profile per build (port of the game half of scripts/game_profile.py).
 */
import { type Dict, get, has, isDict, isList, or, pyEq, pyFloat, pyInt, pyRound, pyStr, tryFloat, tryInt, truthy } from "../py.js";
import { CascadeGame, HoldSpinGame, LineGame, type WinTier, WaysGame } from "./mechanisms.js";
import { pyMin, SLOT_BUTTONS, SLOT_POPUPS, SlotGame, type UiSurface } from "./slot-game.js";

// =====================================================================================
// Mystic Wishes — the cascade reference
// =====================================================================================

/** Mystic Wishes (sl_77_mystic_wishes) — the cascade reference. Response shape and every
 * button/popup name are the defaults; see games/mystic/bet-response.md. */
export class MysticWishes extends CascadeGame {
  override name = "mystic";
  override HAS_ANTE_BET = false;        // no anteBet button in the build
  override HAS_BUY_BONUS = false;       // button exists but does nothing
  override HAS_LAST_BET_PANEL = false;  // no `lastBet` button
}

// =====================================================================================
// Zeus family — Hold & Spin
// =====================================================================================

const ZEUS_BUTTONS: Readonly<Record<string, string | null>> = {
  ...SLOT_BUTTONS,
  stop: "stop",
  auto_or_freespin: "autoOrFreeSpin",   // replaces spin during auto / free spins
  auto_stop_main: "stopAuto",
  ante: null,                   // no Ante Bet
  buy_bonus: "buyBonus",
  // BOTH open the bet-amount popup -- they are not steppers. The stepping lives inside it as
  // bet:plus / bet:minus, and a specific amount is picked with bet:row:<name>.
  stake_up: "nextBet",
  stake_down: "previousBet",
  trophy: "trophy",
  auto_count: "auto:package:{i}",       // 0..: the packages, in order
  auto_confirm: "auto:confirm",
  auto_stop: "auto:stop",
  auto_stop_at_win: "auto:stopAtWin",
  auto_close: "auto:cancel",
  // ⚠ bet:row:<i> resolves the child by NODE NAME equal to that string, not by index position.
  bet_row: "bet:row:{i}",
  bet_plus: "bet:plus",
  bet_minus: "bet:minus",
  bet_close: "bet:close",
  settings_close: "settings:close",
  settings_sound: "settings:sound",
  settings_music: "settings:music",
  settings_turbo: "settings:turbo",
  info_close: "info:close",
  history_close: "history:close",
  message_ok: "notification:yes",
  // bridge-api.md (v3) §9: the `historyDetail:` prefix publishes next / prev / close.
  detail_next: "historyDetail:next",
  detail_prev: "historyDetail:prev",
  detail_close: "historyDetail:close",
  history_row: "history:row:{i}",   // v3 addresses rows by name as well as by rect
  buy_package: "buyBonus:package:{i}",   // 0..2 = casual / bold / epic
  // BUYING IS TWO CLICKS on this build (live run 2026-09-10):
  //   `buyBonus:buy`      the panel's Buy button -- RAISES A CONFIRMATION MODAL
  //   `buyBonus:confirm`  the button inside that modal -- THE ACTUAL PURCHASE
  // The modal is a CHILD of the `buyBonus` popup, so `visiblePopups` shows only `buyBonus`.
  buy_confirm: "buyBonus:confirm",
  buy_purchase: "buyBonus:buy",
  buy_cancel: "buyBonus:cancel",
  buy_close: "buyBonus:close",
};

const ZEUS_POPUPS: Readonly<Record<string, string | null>> = {
  ...SLOT_POPUPS,
  history: "history",
  // ⚠ WAS null, AND THAT WAS THE 70-SECOND HANG: `spin-then-history` looked for this label to
  // decide "did the ticket open?". v3 §10 registers it.
  history_detail: "historyDetail",
  information: "info",
  autoplay: "auto",
  denominations: "bet",
  settings: "settings",
  message: "notification",
  big_win: "bigWin",
  total_win: "totalWin",
  buy_bonus: "buyBonus",
  freespin_result: "totalWin",
  promo: "promo",
  disconnect: "disconnect",
};

const ZEUS_SURFACES: readonly UiSurface[] = [
  ["base screen", null, null],
  ["bet list", "stake_up", "bet_close"],
  ["settings", "settings", null],
  ["tutorial", "info", null],
  ["auto play panel", "auto", "auto_close"],
  ["history", "history", null],
];

/**
 * Zeus / Zappin (SL_ZEUS) — the Hold & Spin reference. Bridge: games/zeus/bridge-api.md (v3).
 *
 * 3x3 grid, 5 paylines, Coin (symbol 13) is the special. 3+ Coins trigger Hold & Spin with 3
 * respins; every new Coin resets the counter to 3; the feature ends at 0 respins or a full grid of
 * 9, and 9 Coins in the base game skip the respins entirely. The bridge exposes NO round outcomes,
 * so the hold-spin rules live in the engine's invariants, computed from the responses.
 */
export class Zeus extends HoldSpinGame {
  override name = "zeus";

  // Only ONE field differs from the defaults: the free-spin counters are NESTED, and the remaining
  // count is spelled with a lowercase 's'.
  override F_FS_CONTAINER: string | null = "freeSpinInfo";   // ...and it is NULL on the round that ends the feature
  override F_FREE_SPINS = "freespins";                       // lowercase 's' -- not Mystic's freeSpins
  // ...but `gameInfo.pending` is FLAT and camelCase: `{refId, roundId, coinPositions, freeSpins,
  // stake}`. Reading it as `freespins` reported every owed Hold & Spin as "0 owed" (Dragon VIP
  // 2026-09-17, where only `gameState == 2` kept the resume from being missed).
  override F_PENDING_FREE_SPINS: string | null = "freeSpins";
  override F_STEPS = "result";        // {views: [...]} -- ONE grid, not a step array

  override GRID_SLOTS = 9;
  override TRIGGER_COUNT = 3;
  override RESPINS_ON_TRIGGER = 3;

  SPECIAL_SYMBOL = 13;                // COIN
  WILD_SYMBOL = 9;
  override PAYLINES: number | null = 5;
  WIN_TIERS: readonly WinTier[] = [["LOSE", 0], ["SLIM_WIN", 0], ["SMALL_WIN", 1], ["MEDIUM_WIN", 2.5],
    ["BIG_WIN", 10], ["MEGA_WIN", 25], ["ULTRA_WIN", 50]];

  override BUTTONS = ZEUS_BUTTONS;
  override POPUPS = ZEUS_POPUPS;
  override UI_SURFACES = ZEUS_SURFACES;
  override FEATURE_TOGGLES: readonly string[] = ["turbo", "sound"];

  override HAS_ANTE_BET = false;
  override HAS_BUY_BONUS = true;
  override HAS_LAST_BET_PANEL = false;
  override HAS_HISTORY_DETAIL_NAV = true;   // v3 §9: historyDetail:next / prev; §6 getHistoryDetailState()
  override HAS_SOUND_GETTERS = true;        // isSoundEnabled()/isMusicEnabled() -> ZZ-24/25
  override HAS_DEV_MODE = true;             // setDevModeResult(grid)
  // v3 §10: `info` is "help / paytable (WebView)" and §6 documents getTutorialRect() for it; only a
  // real page.mouse.wheel scrolls a cross-origin frame.
  override HAS_TUTORIAL_WEBVIEW = true;
  // nextBet / previousBet BOTH open the bet-amount popup (confirmed 2026-08-27). The scenarios still
  // probe at runtime (open_bet_list) rather than trusting this flag.
  override HAS_BET_POPUP = true;

  /** One grid per round: `result.views`. Wrapped as a single step so `Bet.cascades == 1`. */
  override steps(raw: Dict): unknown[] {
    const r = get(raw, this.F_STEPS);
    return isDict(r) ? [r] : r === null ? [] : listOf(r);
  }

  /** Win tier by payout/stake. ⚠ assert against these (Interface.ts WIN_RANGE); Constants.ts
   * exports a second, DEAD table with different thresholds. */
  winTier(payout: number, stake: number): string {
    if (stake <= 0 || payout <= 0) return "LOSE";
    const x = payout / stake;
    let tier = "SLIM_WIN";
    for (const [name, lo] of this.WIN_TIERS) if (lo && x >= lo) tier = name;
    return tier;
  }
}

/** `list(r)` for a value Python would iterate: a list, a string (its characters) or a dict (its keys). */
function listOf(r: unknown): unknown[] {
  if (isList(r)) return [...r];
  if (typeof r === "string") return [...r];
  if (isDict(r)) return Object.keys(r);
  const e = new TypeError(`'${typeof r}' object is not iterable`);
  e.name = "TypeError";
  throw e;
}

// bridge §9: the CANONICAL autoplay names. Zeus's spellings still resolve as aliases, but
// getAllButtonRects() lists canonical names only.
const DRAGON_BUTTONS: Readonly<Record<string, string | null>> = {
  ...ZEUS_BUTTONS,
  auto_count: "auto:count:{i}",
  auto_close: "auto:close",         // a strip of the tap-outside backdrop -- no close X
};

/**
 * Treasures of the Dragon (`dragon`) — a fork of Zeus. Bridge: games/dragon/bridge-qa-api.md (v3).
 * The same game underneath, so response reading, the Hold & Spin parameters and every capability
 * flag are INHERITED; only the dialect §13 says differs is restated. §13 differences that reach the
 * framework without code here: a hidden control is `null` (Zeus: `{visible: false}`); `stopAuto` is
 * the ONLY control that ends a run; the Turbo / Auto toasts ARE in `visiblePopups`;
 * `buyBonusSelectedPrice` is the TOTAL price; unlimited autoplay reports `Infinity`.
 */
export class Dragon extends Zeus {
  override name = "dragon";

  override BUTTONS: Readonly<Record<string, string | null>> = DRAGON_BUTTONS;

  // bridge §9: the panel offers [10, 20, 30, 50, ∞] and OPENS on 10.
  override AUTO_COUNTS: readonly (number | null)[] = [10, 20, 30, 50, null];

  // ⚠ `settings.defaultBetLevel`: this client subtracts one everywhere it reads it (bridge §3), so a
  // FIRST session opens at `denominations[defaultBetLevel - 1]`. Deliberately NOT adapted here: if
  // the server means 0-based the client is wrong, and DR-04 should say so.
}

/**
 * Treasures of the Dragon VIP (`dragon-vip`) — Dragon plus Ante Bet and Premium Reel.
 * Bridge: games/dragon-vip/game-bridge-api.md; Premium Reel contract:
 * games/dragon-vip/docs/raw/client-integration.vi.md.
 *
 * NO one-click ante: Ante Bet and every Premium Reel package sit behind `buyBonus` (open the panel
 * -> ACTIVATE row -> confirm sheet), and the same main button switches the active one OFF. Ante and
 * a Premium Reel are mutually exclusive, and `buyBonus` is null while either is on.
 */
export class DragonVip extends Dragon {
  override name = "dragon-vip";

  override BUTTONS: Readonly<Record<string, string | null>> = {
    ...DRAGON_BUTTONS,
    stake_up: "stakeIncrease",
    stake_down: "stakeDecrease",
    auto_or_freespin: null,                    // bridge §10: not in this build
    buy_purchase: null,                        // the package row opens the confirm sheet (§6.3)
    ante: null,                                // no single ante control (§10)
    ante_on: "buyBonus:anteBet",
    ante_off: "anteBet:off",
    premium_reel: "buyBonus:premiumReels:{i}",  // {i} is the package's reelId, not a row index
    premium_off: "premiumReels:off",
    buy_next: "buyBonus:next",                 // moves the REAL stake on the click (§6.3)
    buy_prev: "buyBonus:prev",
  };

  override POPUPS: Readonly<Record<string, string | null>> = { ...ZEUS_POPUPS, ante_notify: "anteNotify" };

  // The feature panel carries the Ante Bet and Premium Reel rows, so it joins the text surfaces.
  override UI_SURFACES: readonly UiSurface[] = [...ZEUS_SURFACES, ["feature panel", "buy_bonus", "buy_close"]];

  override HAS_ANTE_BET = true;
  override HAS_PREMIUM_REEL = true;
}

// =====================================================================================
// Chaos Runner — the line reference
// =====================================================================================

/**
 * Chaos Runner (`chaos`) — the LINE reference. 5x3, ante bet, random multiplier, free spins.
 * Knowledge: games/chaos/docs/final/game-knowledge.md · paytable: docs/final/paytable.md · bridge:
 * games/chaos/docs/raw/test-bridge-api.md.
 *
 *   * base bet vs bet amount: Ante Bet multiplies what the player PAYS, never the payout base.
 *   * P1 ("Fuel") pays by WAYS on reels 1/3/5 only, while everything else pays by lines.
 *   * a random multiplier per round (x1/x2/x5/x10), which in free spins multiplies every payout.
 *
 * VERIFIED against real BET payloads (games/chaos/fixtures/bet-responses/): the bet object is
 * double-nested (envelope.result is the round, round.result holds {rawViews, views}); symbols are
 * NUMERIC IDS; the multiplier field is `randomMultiplier`; free spins are awarded by REEL count x
 * ways multiplier; paylines are server-reported with their own `payoutRate`.
 */
export class ChaosRunner extends LineGame {
  override name = "chaos";

  override REELS = 5;
  override ROWS = 3;
  override PAYLINES: number | null = 20;   // the samples all report `betLines: 20`; read per round anyway
  override LINE_DIVISOR: number | null = 20;   // fallback only — `lineBetOf()` prefers the round's betLines

  // Symbols — NUMERIC IDS, each read off a real payload (`symbols: [1,1,1,2,1]` with
  // `winType: "A-A-A-J-A"` pins 1=A and 2=J). Ids seen but not yet named: 3, 7, 8.
  SYMBOLS: Readonly<Record<number, string>> = { 1: "A", 2: "J", 4: "K", 5: "P1", 6: "P2", 7: "P3", 9: "WILD!", 11: "FreeSpin!" };
  override SYM_SCATTER: string | number | null = 11;   // UI name "Bonus"
  override SYM_WILD: string | number | null = 9;       // confirmed by winType "P2-WILD!-P2-WILD!-P3"
  // 5 = P1, CONFIRMED by a captured P1 win: id 5 on reels [1, 3, 5] only; its ways (2x1x1) times 10
  // equals `scatterMultiplier` 20; `rawPayout` 12 = 2 ways x 10 x baseStake 0.6.
  SYM_P1 = 5;                       // UI name "Fuel"
  P1_REELS: readonly number[] = [1, 3, 5];   // 1-based
  P1_MULTIPLIER = 10.0;             // 10x bet per way

  // Features — read off the BO paytable export (games/chaos/docs/raw/paytable.json), DEFAULT config.
  override MULTIPLIER_VALUES: readonly number[] = [1, 2, 5, 10];
  // Selection weights per 1000. Free spins skew far richer than the base game.
  MULTIPLIER_WEIGHTS_BASE: Readonly<Record<number, number>> = { 1: 800, 2: 165, 5: 30, 10: 5 };
  MULTIPLIER_WEIGHTS_FREE: Readonly<Record<number, number>> = { 1: 650, 2: 250, 5: 60, 10: 40 };
  RAINING_WILD_MIN_WILDS = 2;       // 2+ Wilds -> 100%; fewer -> 0%
  SCATTERS_TO_TRIGGER = 3;          // ...distinct REELS holding a Bonus, not total symbols
  // "Occurrences" means REELS: a trigger with 5 Bonus SYMBOLS across 3 reels awarded 48 = the
  // Occurrences-3 entry (12) times the ways multiplier (1*2*2 = 4). See expectedFsAward().
  FS_BASE_BY_REELS: Readonly<Record<number, number>> = { 3: 12, 4: 15, 5: 20 };
  // `FreeSpinGame.FreeSpins` is the IDENTICAL table, so a retrigger awards on the same terms.
  FS_RETRIGGER_BY_REELS: Readonly<Record<number, number>> = { 3: 12, 4: 15, 5: 20 };
  ANTE_MULTIPLIER = 3.0;
  ANTE_DISTRIBUTION_LIST = 3;
  // Complete symbol paytable, indexed by MATCH COUNT 1..5. Every rate observed live matched it.
  SYMBOL_PAYS: Readonly<Record<string, readonly number[]>> = {
    J: [0, 0, 4, 10, 30],
    Q: [0, 0, 6, 20, 50],
    K: [0, 0, 8, 25, 100],
    A: [0, 0, 10, 50, 150],
    P4: [0, 0, 15, 75, 200],
    P3: [0, 0, 20, 100, 250],
    P2: [0, 0, 40, 150, 300],
    P1: [0, 0, 10, 0, 0],      // ways/scatter symbol: only the 3-of-a-kind entry pays
    "WILD!": [0, 0, 0, 0, 0],  // substitutes; never pays on its own
  };
  // P1 is configured as a SCATTER — it pays by occurrence, anywhere, not along a payline.
  P1_SCATTER_OCCURRENCES = 3;
  // Reels P1 can occupy, from the reel strips: weight 0 on reels 2 and 4 in every list.
  P1_REELS_FROM_CONFIG: readonly number[] = [1, 3, 5];
  // Wild's weight is 0 on REEL 1 in every list — an absolute rule neither design doc mentions.
  WILD_FORBIDDEN_REELS: readonly number[] = [1];
  override WIN_TIERS: readonly WinTier[] = [["SLIM_WIN", 0], ["SMALL_WIN", 1], ["MEDIUM_WIN", 2.5],
    ["BIG_WIN", 6], ["MEGA_WIN", 15], ["ULTRA_WIN", 40]];

  override F_STEPS = "result";               // the INNER {rawViews, views} object — one grid per round
  override F_MULTIPLIER = "randomMultiplier";
  F_SCATTER_MULTIPLIER = "scatterMultiplier";
  override F_ANTE_MULTIPLIER = "anteMultiplier";
  F_PAYLINES = "paylines";          // [{symbols, payline, payoutRate, winType}]
  F_BET_LINES = "betLines";         // 20 in every sample
  override F_WINLOSE = "winlose";   // payout - stake

  // VERIFIED LIVE 2026-09-02: the bridge keeps an ALIAS TABLE — its native names (`betPlus`,
  // `denomination:close`, ...) AND the reference spellings both resolve. ⚠ `getAllButtonRects()`
  // lists NATIVE names only; a reference name missing from it can still resolve.
  override BUTTONS: Readonly<Record<string, string | null>> = {
    ...SLOT_BUTTONS,
    buy_bonus: null,       // NO Buy Bonus in this build: skip the control instead of probing it
    bet_plus: "bet:plus",
    bet_minus: "bet:minus",
    trophy: "trophy",      // the Monumental Wins panel; native `monumentalWin`, alias `trophy`
  };
  override AUTO_COUNTS: readonly (number | null)[] = [10, 20, 30, 50, null];

  // Popup LABELS are NOT aliased by the bridge: `visiblePopups` reports raw strings, so these must
  // match exactly (confirmed live 2026-09-02). `denominations` -> `denomination` cost a FALSE GREEN:
  // `close_bet_list` concluded the list was already gone and never clicked close.
  override POPUPS: Readonly<Record<string, string | null>> = {
    ...SLOT_POPUPS,
    denominations: "denomination",   // NOT the reference plural — verified live
    autoplay: "autoplay",            // NOT the reference camelCase `autoPlay` — verified live
    big_win: null,                   // big win is a tap-anywhere Canvas listener, not a popup
    total_win: null,
    buy_bonus: null,                 // no Buy Bonus in this build
    freespin_result: null,           // the skip control is a BUTTON (`skip:freespinResult`)
    // Split across promoMessage / promoReceive / promoList; give it a label only when a scenario
    // needs one, and verify it live first.
    promo: null,
  };
  override UI_SURFACES: readonly UiSurface[] = [
    ["base screen", null, null],
    ["bet list", "stake_up", "bet_close"],
    ["settings", "settings", "settings_close"],
    ["tutorial", "info", null],
    ["auto play panel", "auto", "auto_close"],
    ["history", "history", null],
  ];
  override FEATURE_TOGGLES: readonly string[] = ["turbo", "ante", "sound"];

  override HAS_TURBO = true;
  override HAS_ANTE_BET = true;
  override HAS_BUY_BONUS = false;        // `buyBonusNode` is unwired; the doc states it outright
  override HAS_AUTOPLAY = true;
  override HAS_FREE_SPINS = true;
  override HAS_LAST_BET_PANEL = false;   // no `lastBet` control in the bridge's list
  override HAS_TUTORIAL_WEBVIEW = true;  // cross-origin: the framework wheels it, the bridge locates it
  override HAS_SPEED_MODE_GETTER = true;
  override HAS_HISTORY_DETAIL_NAV = true;
  override HAS_SOUND_GETTERS = false;    // no isSoundEnabled()/isMusicEnabled() in the bridge doc
  override HAS_DEV_MODE = false;         // forcing is setForcedResult() (one-shot)
  HAS_FORCED_RESULT = true;
  HAS_MULTIPLIER_LADDER = true;
  HAS_REPLAY_MODE = true;

  /** One grid per round: `result` is a single object, wrapped so `Bet.cascades == 1`. */
  override steps(raw: Dict): unknown[] {
    const r = get(raw, this.F_STEPS);
    return isDict(r) ? [r] : r === null ? [] : listOf(r);
  }

  /** Total Bonus/FreeSpin symbols on the grid. null when unreadable. */
  scatters(raw: Dict): number | null {
    return this.symbolCount(raw, this.SYM_SCATTER);
  }

  /** Total Wilds on the grid — the Raining Wild gate. null when unreadable. */
  wilds(raw: Dict): number | null {
    return this.symbolCount(raw, this.SYM_WILD);
  }

  /** P1's ways count = product of its counts on reels 1, 3 and 5. */
  p1Ways(raw: Dict): number | null {
    return this.ways(raw, this.SYM_P1, this.P1_REELS);
  }

  /** 1-based reel numbers holding at least one Bonus symbol. null when unreadable. */
  scatterReels(raw: Dict): number[] | null {
    return this.reelsWith(raw, this.SYM_SCATTER);
  }

  /** The Bonus ways multiplier: product of the per-reel Bonus counts over the reels that have one.
   * null when unreadable, 0 when no Bonus landed. */
  scatterWays(raw: Dict): number | null {
    const counts = this.reelCounts(raw, this.SYM_SCATTER);
    if (counts === null) return null;
    const hits = counts.filter((n) => n > 0);
    if (!hits.length) return 0;
    let ways = 1;
    for (const n of hits) ways *= n;
    return ways;
  }

  /**
   * Free spins this round SHOULD award. null when unreadable; 0 when the Bonus did not reach enough
   * reels. **Reel-based, then multiplied by ways**: reels = distinct reels holding a Bonus (>= 3);
   * base = 12 / 15 / 20 for 3 / 4 / 5 reels; award = base x product of the per-reel counts.
   * Confirmed sample: counts [1,2,0,2,0] -> 3 reels, ways 4, base 12 -> 48, what the server awarded.
   */
  expectedFsAward(raw: Dict): number | null {
    const reels = this.scatterReels(raw);
    if (reels === null) return null;
    if (reels.length < this.SCATTERS_TO_TRIGGER) return 0;
    const base = this.FS_BASE_BY_REELS[reels.length];
    if (base === undefined) return 0;
    return base * (or(this.scatterWays(raw), 1) as number);
  }

  /** Paylines in force for THIS round, server-reported. Falls back to the configured count. */
  betLines(raw: Dict): number {
    const v = get(raw, this.F_BET_LINES);
    const fallback = (): number => pyInt(or(this.PAYLINES, this.LINE_DIVISOR));
    if (!truthy(v)) return fallback();
    const n = tryInt(v);
    return n === null ? fallback() : n;
  }

  /** Per-line bet for this round: base stake / the round's own betLines. */
  lineBetOf(raw: Dict): number {
    const n = this.betLines(raw);
    return n ? this.baseStake(raw) / n : 0;
  }

  /** The paying lines, EXCLUDING the synthetic scatter/free-spin entry (`payline: -1`), whose
   * `payoutRate` is NOT a line payout — counting it would invent a win. */
  winLines(raw: Dict): Dict[] {
    const out: Dict[] = [];
    for (const p of (or(get(raw, this.F_PAYLINES), []) as unknown[])) {
      if (isDict(p) && !pyEq(get(p, "payline"), -1)) out.push(p);
    }
    return out;
  }

  /**
   * The `payoutRate` a reported payline SHOULD carry, from the BO symbol table. The win is the
   * LEADING RUN of `winType` (`J-J-J-A-K`), so the rate is `SYMBOL_PAYS[leader][run_length]`; WILD!
   * substitutes for the leader. null when underivable (no winType, an unpriced leader, an all-wild
   * line) — callers treat null as *not checked*, never as agreement.
   */
  expectedLineRate(payline: unknown): number | null {
    const names = pyStr(or(get(or(payline, {}), "winType"), "")).split("-");
    if (!names.length || !names[0]) return null;
    const wild = this.SYM_WILD ? pyStr(this.SYMBOLS[this.SYM_WILD as number] ?? "WILD!") : "WILD!";
    const leader = names.find((n) => n !== wild);
    if (leader === undefined || !has(this.SYMBOL_PAYS, leader)) return null;
    let run = 0;
    for (const n of names) {
      if (n === leader || n === wild) run += 1;
      else break;
    }
    const table = this.SYMBOL_PAYS[leader];
    return run >= 1 && run <= table.length ? table[run - 1] : null;
  }

  /** 1-based reels holding a Wild. null when unreadable. */
  wildReels(raw: Dict): number[] | null {
    return this.reelsWith(raw, this.SYM_WILD);
  }

  /** The round's scatter multiplier — P1's `ways x 10`. 0 when no P1 win. */
  scatterMultiplier(raw: Dict): number {
    return tryFloat(or(get(raw, this.F_SCATTER_MULTIPLIER), 0), 0);
  }

  /** The LINE half of the win: `randomMultiplier x SUM(payoutRate) x baseStake / betLines`. */
  expectedLineWin(raw: Dict): number {
    let rate = 0;
    for (const p of this.winLines(raw)) {
      const f = tryFloat(or(get(p, "payoutRate"), 0));
      if (f !== null) rate += f;
    }
    const mult = this.multiplier(raw);
    return rate * this.lineBetOf(raw) * (mult === null ? 1 : mult);
  }

  /** The SCATTER half: `scatterMultiplier x baseStake` — no divisor, a scatter pays on the WHOLE
   * bet. ⚠ Whether `randomMultiplier` also applies is unconfirmed; it is deliberately NOT applied,
   * so a scatter win on a x2+ spin surfaces the question as a violation. */
  expectedScatterWin(raw: Dict): number {
    return this.scatterMultiplier(raw) * this.baseStake(raw);
  }

  /** The round's win BEFORE the max-win cap: lines + scatter. null when there is nothing to derive from. */
  expectedRawPayout(raw: Dict): number | null {
    if (get(raw, this.F_PAYLINES) === null && !has(raw, this.F_SCATTER_MULTIPLIER)) return null;
    return pyRound(this.expectedLineWin(raw) + this.expectedScatterWin(raw), 10);
  }

  /** What this round SHOULD have CREDITED — the raw win, truncated at what is LEFT of the round's
   * max-win cap (`maxWinLeft`). Verified exact on all captured samples, including one that earned
   * `rawPayout: 12` against `maxWin: 10` and credited 10. */
  expectedPayout(raw: Dict): number | null {
    const rawWin = this.expectedRawPayout(raw);
    if (rawWin === null) return null;
    const left = this.maxWinLeft(raw);
    return left !== null ? pyRound(pyMin(rawWin, left), 10) : rawWin;
  }

  /** Whether Raining Wild MUST play for this result (2+ Wilds -> 100%, else 0%). null when unreadable. */
  rainingWildExpected(raw: Dict): boolean | null {
    const n = this.wilds(raw);
    return n === null ? null : n >= this.RAINING_WILD_MIN_WILDS;
  }

  /** CONFIRMED by a real ante payload: `anteMultiplier: 3, baseStake: 0.6, stake: 1.8`, paying
   * `150 x 0.6/20 = 4.5` off the UNCHANGED base. null when the field is 0 (ante off). */
  override anteMultiplier(raw: Dict): number | null {
    const f = tryFloat(get(raw, this.F_ANTE_MULTIPLIER));
    return f !== null && f > 0 ? f : null;
  }

  /** Bet with Ante on, by either signal: a non-zero `anteMultiplier`, or a stake above the base. */
  override isAnteRound(raw: Dict): boolean {
    if (this.anteMultiplier(raw) !== null) return true;
    const stake = tryFloat(or(get(raw, this.F_STAKE), 0));
    return stake !== null && stake > this.baseStake(raw) + 1e-9;
  }

  override baseStake(raw: Dict): number {
    let v = get(raw, this.F_BASE_STAKE);
    if (v === null) v = get(raw, this.F_STAKE);
    return tryFloat(or(v, 0), 0);
  }

  /** A round awarded the bonus when the server says so, or when the Bonus reached enough distinct
   * REELS (the counters may only appear on the next response). */
  override isTrigger(raw: Dict): boolean {
    const [, awarded] = this.freeSpins(raw);
    if (awarded > 0 || this.isBonusState(this.gameState(raw))) return true;
    const reels = this.scatterReels(raw);
    return reels !== null && reels.length >= this.SCATTERS_TO_TRIGGER;
  }
}

// =====================================================================================
// Joker: Where's My Banana — expanding wilds
// =====================================================================================

/**
 * Joker: Where's My Banana (`joker`) — 5x3, 20 fixed paylines, expanding wilds, Buy Bonus. Bridge:
 * games/joker/docs/raw/bridge-api.md. Shape and arithmetic: games/joker/bet-response.md.
 *
 *   * Expanding wilds: every response carries the reels TWICE — `rawViews` as landed and `views`
 *     after expansion — so the feature is asserted by DIFFING them. Base game: no expansion.
 *   * The blocked symbols are `FreeSpin!` and `Power!` (help: BONUS and BANANA).
 *   * Buy Bonus is two steps: the round is bought, THEN a Lucky Spin wheel offers x1 / x3 / x12.
 *     The bridge never makes that choice and `dismissPopups()` never closes the wheel.
 *   * Free spins carry a multiplier (`luckyMultiplier`), 3 by default on a natural trigger.
 *
 * ⚠ `multiplier` is NOT a payout factor here — see `randomMultiplier`.
 */
export class JokerBanana extends LineGame {
  override name = "joker";

  override F_STEPS = "result";               // {rawViews, views} -- ONE grid, not a step array
  override F_STEP_GRID = "views";            // the reels AFTER expansion: what the player ends on
  F_RAW_GRID = "rawViews";                   // ...and as they landed. The pair IS the feature.
  override F_FREE_SPINS = "freeSpins";       // REMAINING (flat, not nested like Zeus)
  override F_TOTAL_FREE_SPINS = "totalFreeSpin";
  F_LUCKY_MULTIPLIER = "luckyMultiplier";

  override PAYLINES: number | null = 20;     // fixed; `betLines` echoes it on every round
  override LINE_DIVISOR: number | null = 20; // payout = sum(payoutRate) x (stake / 20)
  override REELS = 5;
  override ROWS = 3;

  override SYM_WILD: string | number | null = "WILD!";
  override SYM_SCATTER: string | number | null = "FreeSpin!";   // the free-spin trigger symbol
  SYM_POWER = "Power!";                      // the scatter-pay symbol (help: BANANA)
  WILD_EXPAND_BLOCKED: readonly string[] = ["FreeSpin!", "Power!"];
  // ⚠ UNCONFIRMED against a capture: the one sample expanded reel id 4.
  WILD_EXPAND_REELS: readonly number[] = [2, 4];

  FREESPIN_AWARDS: Readonly<Record<number, number>> = { 3: 12, 4: 15, 5: 20 };
  FREESPIN_MULTIPLIER = 3;
  BUY_BONUS_SPINS = 20;
  BUY_BONUS_PRICE_X = 100;
  LUCKY_MULTIPLIERS: readonly number[] = [1, 3, 12];
  LUCKY_MULTIPLIER_DEFAULT = 3;

  override WIN_TIERS: readonly WinTier[] = [["SLIM_WIN", 0], ["SMALL_WIN", 1], ["MEDIUM_WIN", 2.5],
    ["BIG_WIN", 10], ["MEGA_WIN", 25], ["ULTRA_WIN", 50]];

  override BUTTONS: Readonly<Record<string, string | null>> = {
    ...SLOT_BUTTONS,
    spin: "spin",
    stop: "spin",                  // the spin button in STOP state
    turbo: "turbo",
    stake_up: "betPlus",           // BOTH open the denomination list
    stake_down: "betMinus",
    bet_row: "bet:row:{i}",
    bet_close: "bet:close",
    auto: "auto",
    auto_count: "auto:count:{i}",
    auto_stop_at_win: "auto:stopAtWin",
    auto_confirm: "auto:confirm",
    auto_close: "auto:close",
    auto_stop: "auto:stop",
    auto_stop_main: "auto",        // `auto` doubles as stop while a run is going
    history: "history",
    history_close: "history:close",
    detail_next: "historyDetail:next",
    detail_prev: "historyDetail:prev",
    info: "info",
    info_close: "info:close",
    settings: "settings",
    settings_close: "settings:close",
    sound: "sound",
    settings_sound: "settings:sound",
    settings_music: "settings:music",
    settings_turbo: "settings:turbo",
    message_ok: "message:ok",
    trophy: "monumentalWin",
    ante: null,                    // no Ante in this game
    buy_bonus: "buyBonus",
    buy_package: "buyBonus:package:{i}",   // only i = 0 exists
    buy_purchase: "buyBonus:buy",
    buy_confirm: "buyBonus:confirm",
    buy_cancel: "buyBonus:cancel",
    // game-specific
    buy_level: "buyBonus:level:{i}",       // 0..2 stake shortcuts
    lucky_keep: "luckySpin:keep",
    lucky_spin: "luckySpin:spin",
  };

  override POPUPS: Readonly<Record<string, string | null>> = {
    ...SLOT_POPUPS,
    message: "message",
    disconnect: "disconnect",
    denominations: "denomination",
    autoplay: "autoplay",
    settings: "setting",
    history: "history",
    history_detail: "historyDetail",
    information: "information",
    game_limit: "gameLimit",
    buy_bonus: "buyBonus",
    lucky_spin: "luckySpin",       // ⚠ dismissPopups() never closes this one
    trophy: "monumentalWins",
    promo: null,                   // split across promoMessage / promoReceive / promoList
    big_win: null,                 // a presentation, not a popup (busyOps: bigwin)
    total_win: null,               // likewise (roundState SHOW_FREE_SPIN_RESULT)
    turbo_notify: "notify:turbo",
    auto_notify: "notify:autoplay",
  };

  override AUTO_COUNTS: readonly (number | null)[] = [10, 20, 30, 50, null];

  override UI_SURFACES: readonly UiSurface[] = [
    ["base screen", null, null],
    ["bet list", "stake_up", "bet_close"],
    ["settings", "settings", null],
    ["tutorial", "info", null],
    ["auto play panel", "auto", "auto_close"],
    ["history", "history", null],
    ["buy bonus panel", "buy_bonus", "buy_cancel"],
  ];
  override FEATURE_TOGGLES: readonly string[] = ["turbo", "sound"];

  override HAS_ANTE_BET = false;
  override HAS_BUY_BONUS = true;
  HAS_LUCKY_SPIN = true;
  HAS_WILD_EXPANDING = true;
  override HAS_BET_POPUP = true;
  override HAS_HISTORY_DETAIL_NAV = true;
  override HAS_TUTORIAL_WEBVIEW = true;
  override HAS_LAST_BET_PANEL = false;
  // bridge §9 lists isSoundEnabled()/isMusicEnabled() as INDEPENDENT flags.
  override HAS_SOUND_GETTERS = true;
  override HAS_DEV_MODE = false;          // forcing is setForcedResult(), like Chaos
  HAS_FORCED_RESULT = true;
  HAS_MULTIPLIER_LADDER = true;

  /** One grid per round: `result` is a single object, wrapped so `Bet.cascades == 1`. */
  override steps(raw: Dict): unknown[] {
    const r = get(raw, this.F_STEPS);
    return isDict(r) ? [r] : r === null ? [] : listOf(r);
  }

  /** [remaining, awarded] — with the BOUGHT round's own spelling folded in. ⚠ A Buy Bonus purchase
   * reports `freeSpins: 20` and `freeSpinPackage`, and NO `totalFreeSpin` (captured 2026-09-21). The
   * fallback is gated on `freeSpinPackage`: `freeSpins` is REMAINING, so using it as the award
   * everywhere would call every round of every bonus a trigger. */
  override freeSpins(raw: Dict): [number, number] {
    const [remaining, awarded0] = super.freeSpins(raw);
    let awarded = awarded0;
    if (!awarded && isDict(raw) && truthy(get(raw, "freeSpinPackage"))) awarded = remaining;
    return [remaining, awarded];
  }

  private stepObject(raw: Dict): Dict {
    const s = get(raw, this.F_STEPS);
    return isDict(s) ? s : {};
  }

  /** The reels AS THEY LANDED (`result.rawViews`), before any wild expansion. */
  rawReels(raw: Dict): unknown[] {
    return [...(or(get(this.stepObject(raw), this.F_RAW_GRID), []) as unknown[])];
  }

  /** The reels AFTER expansion (`result.views`) — what the player ends the round looking at. */
  reels(raw: Dict): unknown[] {
    return [...(or(get(this.stepObject(raw), this.F_STEP_GRID), []) as unknown[])];
  }

  /** Reel ids where `views` differs from `rawViews` — i.e. where a wild expanded. Empty on every
   * base round of the captures, `[4]` on the free spin that expanded. */
  expandedReels(raw: Dict): unknown[] {
    const out: unknown[] = [];
    const before = this.rawReels(raw);
    const after = this.reels(raw);
    for (let i = 0; i < Math.min(before.length, after.length); i++) {
      if (!pyEq(get(before[i], "value"), get(after[i], "value"))) out.push(get(before[i], "id"));
    }
    return out;
  }

  /** The free-spin multiplier this round was played at, or null when absent. `0` means "not a free
   * spin" on this backend, so it reads as null — a field saying NOTHING is not a value. */
  luckyMultiplier(raw: Dict): number | null {
    const f = tryFloat(get(raw, this.F_LUCKY_MULTIPLIER));
    return f !== null && f > 0 ? f : null;
  }

  /** ⚠ NOT the payout multiplier: the captured win reads `multiplier: 2.5` and its payout matches the
   * line arithmetic WITHOUT it. Reporting only; no invariant may multiply by it. */
  randomMultiplier(raw: Dict): unknown {
    return get(raw, "multiplier");
  }
}

// =====================================================================================
// Grandma Pets — ways
// =====================================================================================

/**
 * Grandma Pets (sl_45_grandma_pets) — 1024 ways on a 5x4 grid, with a per-round WILD MULTIPLIER,
 * WALKING WILDS in free spins, a Buy Bonus, a Free Spin Extension and the Cat vs Dog drama.
 * Sources: games/grandma/bridge-api.md and games/grandma/bet-response.md.
 *
 * ⚠ games/grandma/docs/raw/paytable.json is NOT a source for pay VALUES; the response reports
 * `symbolMultiplier` per win and that is what the arithmetic uses. Four things are spelled
 * differently from every other game, and each fails SILENTLY if left at the default.
 */
export class Grandma extends WaysGame {
  override name = "grandma";

  override REELS = 5;
  override ROWS = 4;
  override WAYS_TOTAL: number | null = 1024;   // the rules page's own number: 4 ** 5, full mask

  // The paytable prints each symbol against the TOTAL bet, so the divisor is 1.
  override LINE_DIVISOR: number | null = 1;

  // (1) The grid field is SINGULAR: `view` / `rawView`, not `views`.
  override F_STEP_GRID = "view";
  F_RAW_GRID = "rawView";

  // (2) There is no `gameState`: `state` (where it was PLAYED) and `nextState` (where it goes). ⚠
  // Mapping this to `state` breaks the Buy Bonus: the purchase is PLAYED in base (`state: 1`) and
  // hands over to the bonus (`nextState: 2`).
  override F_GAME_STATE = "nextState";
  // `pending` keeps the ordinary spelling.
  override F_PENDING_GAME_STATE: string | null = "gameState";

  // (3) The award is its own field: `freeSpins` REMAINING, `totalFreeSpin` the bonus TOTAL,
  // `wonFreeSpins` what THIS round handed out.
  override F_FREE_SPINS = "freeSpins";
  override F_TOTAL_FREE_SPINS = "totalFreeSpin";
  override F_WON_FREE_SPINS: string | null = "wonFreeSpins";

  // (4) A purchase keeps its `baseStake`: `stake` is the money charged (75 x bet on a purchase, 0 on
  // a free spin), `baseStake` the real bet on EVERY round.
  override F_STAKE = "stake";
  override F_BASE_STAKE = "baseStake";

  F_WAY_DETAILS = "wayDetails";
  F_TOTAL_WAYS = "totalWays";
  F_WAY_COUNT = "wayCount";
  F_WAY_SYMBOL = "symbolId";
  F_WAY_SYMBOL_MULTIPLIER = "symbolMultiplier";
  F_WAY_WILD_MULTIPLIER = "totalWildMultiplier";
  F_WAY_TOTAL_MULTIPLIER = "totalMultiplier";
  F_WAY_PAYOUT = "payout";
  F_WAY_WILD_POSITIONS = "wildPositions";
  F_WILD_MULTIPLIERS = "wildMultipliers";

  // ⚠ `package` is on the purchase AND every free spin of the bonus it bought, so it can never
  // answer "was this a purchase" — `isPurchase()` uses `subType` for that.
  override F_PACKAGE: string | null = "package";
  F_SUB_TYPE = "subType";              // 5 on the purchase, 0 on everything else
  F_MISC_TYPE = "miscType";            // "BuyBonusA" on the purchase, "NA" otherwise
  BUY_MULTIPLIER = 75;

  override SYM_WILD: string | number | null = 19;
  override SYM_SCATTER: string | number | null = 11;
  // The premium the anticipation glow keys on besides the scatter: P1, the white cat.
  SYM_GLOW_PREMIUM = 5;
  CAT_SYMBOLS: readonly number[] = [5, 7, 8];      // white / orange / grey cat
  DOG_SYMBOLS: readonly number[] = [6, 18];        // pug / dachshund
  // 3, 4 or 5 REELS holding a scatter award these. Counted by REEL (TC-103).
  FREESPIN_AWARDS: Readonly<Record<number, number>> = { 3: 8, 4: 12, 5: 15 };
  // The wild multiplier ladders, per mode (bridge-api.md §8.1). Free spins never draw 1x.
  override MULTIPLIER_VALUES: readonly number[] = [1, 2, 3, 5, 8, 10, 15, 30, 40];
  FREE_MULTIPLIER_VALUES: readonly number[] = [2, 3, 5, 8, 10, 15, 30, 40];

  override HAS_BUY_BONUS = true;
  override HAS_FREE_SPINS = true;
  override HAS_ANTE_BET = false;              // AnteBetSetting.Enabled is false, and `anteBet` is null
  override HAS_PREMIUM_REEL = false;
  override HAS_LAST_BET_PANEL = false;
  override HAS_SOUND_GETTERS = true;          // bridge §6 lists isSoundEnabled() and isMusicEnabled()
  override HAS_SPEED_MODE_GETTER = true;
  override HAS_HISTORY_DETAIL_NAV = true;
  override HAS_TUTORIAL_WEBVIEW = true;
  override HAS_BET_POPUP = true;              // the stake arrows OPEN the denomination list (bridge §3)
  override HAS_DEV_MODE = false;              // ⚠ setForcedResult / setDevModeResult are NOT implemented

  override BUTTONS: Readonly<Record<string, string | null>> = {
    ...SLOT_BUTTONS,
    stop: "stop",
    auto_stop_main: "auto",        // bridge §3: `auto` doubles as the main-screen stop
    ante: null,                    // null while ante is disabled for the operator
    buy_bonus: "buyBonus",
    buy_package: "buyBonus:package:{i}",
    buy_confirm: "buyBonus:confirm",
    buy_cancel: "buyBonus:cancel",
    bet_plus: "bet:plus",
    bet_minus: "bet:minus",
    settings_close: "settings:close",
    settings_sound: "settings:sound",
    settings_music: "settings:music",
    settings_turbo: "settings:turbo",
    // ⚠ History and the guide live INSIDE the Settings panel on this build (bridge §7
    // `SettingPopup`); BUTTON_VIA makes a click on either open Settings first.
    history: "settings:history",
    info: "settings:info",
    info_close: "info:close",
    history_close: "history:close",
    // The round detail opens ON TOP of the History list and covers `history:close`.
    detail_close: "historyDetail:close",
    // The Free Spin Extension offer. ⚠ Closing it is a DECLINE (bridge §8.4); until it is answered
    // the spin button never frees up.
    extension_confirm: "extension:confirm",
    extension_decline: "extension:decline",
    extension_close: "extension:close",
    freespin_result_skip: "freeSpinResult:skip",
    // ⚠ NOT in bridge-api.md §7; the name is what the game team says it is. `skip_big_win()`
    // reports `unknown_control` loudly if this build does not have it.
    big_win_skip: "bigWin:skip",
    // The promo dialogs (bridge §7). `cancel` closes with no side effect; `confirm` is the claim
    // (or an irreversible discard) whenever `cancel` is showing too.
    promo_cancel: "promo:receive:cancel",
    promo_confirm: "promo:receive:confirm",
    promo_list_close: "promo:list:close",
    promo_message_ok: "promo:message:ok",
  };

  override BUTTON_VIA: Readonly<Record<string, string>> = { "settings:history": "settings", "settings:info": "settings" };

  // Money is printed with 2 decimals and the rest CUT: 15069.237 shown as 15,069.23, a 0.195 win as 0.19.
  override DISPLAY_DECIMALS = 2;
  override DISPLAY_TRUNCATES = true;

  override POPUPS: Readonly<Record<string, string | null>> = {
    ...SLOT_POPUPS,
    history: "HistoryPopup",
    history_detail: "HistoryDetail",
    information: "InformationPopup",
    autoplay: "AutoPlayPopup",
    denominations: "DenominationPopup",
    settings: "SettingPopup",
    message: "MessagePopup",
    big_win: "BigWinPopup",
    buy_bonus: "MultipleBuyBonusPopup",
    freespin_result: "FreeSpinResultPopup",
    disconnect: "DisconnectPopup",
    // `Promo` itself is never reported (it is the HUD container); only its inner dialogs are.
    promo: null,
    promo_receive: "Promo:receive",
    promo_list: "Promo:list",
    promo_message: "Promo:message",
    get_free_spin: "GetFreeSpinPopup",
    extra_free_spin: "ExtraFreeSpinPopup",
    extension_offer: "BuyExtraFreeSpinPanel",
    lucky_spin: "LuckySpinPopup",
    trophy: "MonumentalWinsPopup",
    game_limit: "PopupGameLimit",
  };

  /** Did THIS round award free spins? Exact here, because the server says so outright. ⚠ The
   * inherited rule is wrong twice: `totalFreeSpin` and `nextState: 2` stay on every free spin. */
  override isTrigger(raw: Dict): boolean {
    return this.wonFreeSpins(raw) > 0;
  }

  /** Was this round a Buy Bonus purchase? ⚠ NOT `package is not None` — `subType` is 5 only on
   * the purchase itself, with `miscType` as the second witness. */
  isPurchase(raw: Dict): boolean {
    if (pyEq(get(raw, this.F_SUB_TYPE), 5)) return true;
    return pyStr(or(get(raw, this.F_MISC_TYPE), "")).startsWith("BuyBonus");
  }

  /** 1-based reel numbers holding a BONUS (scatter) — the count the award table is keyed on. */
  bonusReels(raw: Dict): number[] | null {
    return this.reelsWith(raw, this.SYM_SCATTER);
  }

  /** Free spins this round SHOULD award, from the number of reels holding a BONUS. null when the
   * grid cannot be read; 0 for too few reels — a real answer, not a gap. */
  expectedAward(raw: Dict): number | null {
    const reels = this.bonusReels(raw);
    if (reels === null) return null;
    return this.FREESPIN_AWARDS[reels.length] ?? 0;
  }

  /** The most BONUS symbols on any single reel. `>= 2` is the case a ways-based award count would
   * get wrong. null when unreadable. */
  maxScattersOnAReel(raw: Dict): number | null {
    const counts = this.reelCounts(raw, this.SYM_SCATTER);
    return counts === null ? null : counts.length ? Math.max(...counts) : 0;
  }

  /** Every cell holding a WILD, as `{row, col}` 0-based (col 0 = reel 1, row 0 = top) — the shape
   * `getWalkingWildState().cells` uses. null when unreadable. */
  wildCells(raw: Dict): Array<{ row: number; col: number }> | null {
    const reels = this.grid(raw);
    if (reels === null) return null;
    const want = LineGame._norm(this.SYM_WILD);
    const out: Array<{ row: number; col: number }> = [];
    reels.forEach((reel, col) => reel.forEach((sym, row) => {
      if (LineGame._norm(sym) === want) out.push({ row, col });
    }));
    return out;
  }

  /** The per-way breakdown. `[]` on a losing round — the server omits the entries. */
  wayDetails(raw: Dict): unknown[] {
    const v = get(raw, this.F_WAY_DETAILS);
    return isList(v) ? v : [];
  }

  /** What one `wayDetails` entry SHOULD pay: `symbolMultiplier x totalWildMultiplier x wayCount x
   * baseStake`. Verified exact against every captured entry. null when a field is missing. ⚠
   * `baseStake`, never `stake`: a free spin costs 0 and still pays off the real bet. */
  wayPayout(way: Dict, baseStake: unknown): number | null {
    const sym = tryFloat(get(way, this.F_WAY_SYMBOL_MULTIPLIER));
    if (sym === null) return null;
    const wild = tryFloat(or(get(way, this.F_WAY_WILD_MULTIPLIER), 1));
    if (wild === null) return null;
    const count = tryFloat(get(way, this.F_WAY_COUNT));
    if (count === null) return null;
    return sym * wild * count * pyFloat(or(baseStake, 0));
  }

  /** The wild multiplier drawn for this round, or null when the round reports none. ⚠ null is the
   * normal answer on a losing round, so "every round draws one" is NOT assertable. */
  wildMultiplier(raw: Dict): number | null {
    const entries = get(raw, this.F_WILD_MULTIPLIERS);
    if (!isList(entries)) return null;
    for (const e of entries) {
      if (isDict(e) && pyEq(get(e, "symbolId"), this.SYM_WILD)) return tryFloat(get(e, "multiplierValue"));
    }
    return null;
  }

  /** "CAT", "DOG" or null — the drama's two families (bridge §8.5). */
  familyOf(symbolId: unknown): "CAT" | "DOG" | null {
    const sid = tryInt(symbolId);
    if (sid === null) return null;
    if (this.CAT_SYMBOLS.includes(sid)) return "CAT";
    if (this.DOG_SYMBOLS.includes(sid)) return "DOG";
    return null;
  }
}

// =====================================================================================
// Registry
// =====================================================================================

const REGISTRY = new Map<string, SlotGame>();

export function register<T extends SlotGame>(profile: T): T {
  REGISTRY.set(profile.name, profile);
  return profile;
}

/** Profile by game name; falls back to the cascade reference so an unprofiled game still runs on
 * the defaults (and says so, rather than crashing). */
export function getProfile(name: string): SlotGame {
  const p = REGISTRY.get(name);
  if (p) return p;
  console.log(`(no game profile for '${name}' -- using the 'mystic' defaults)`);
  return REGISTRY.get("mystic") as SlotGame;
}

export function profileNames(): string[] {
  return [...REGISTRY.keys()];
}

register(new MysticWishes());
register(new Zeus());
register(new Dragon());
register(new DragonVip());
register(new ChaosRunner());
register(new JokerBanana());
register(new Grandma());
