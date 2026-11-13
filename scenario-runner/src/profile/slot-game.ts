/**
 * Game PROFILES — the per-game half of the scenario suite (port of scripts/game_profile.py).
 *
 * The engine holds the MECHANISM: how to drive a slot from its server BET response and grade it.
 * A profile holds the DIALECT: what a build calls its buttons and popups, which features it ships,
 * and how its response is shaped.
 *
 *     SlotGame            lifecycle, buttons, popups, money, autoplay, language (mechanism-free)
 *       CascadeGame       cluster-pays with cascading steps       -> MysticWishes
 *       HoldSpinGame      respins that HOLD symbols in place      -> Zeus, Dragon, DragonVip
 *       LineGame          one grid, paid along lines              -> ChaosRunner, JokerBanana
 *         WaysGame        one grid, paid by ways                  -> Grandma
 *
 * A new game subclasses whichever matches its bonus, declares only what it names differently, and
 * inherits every scenario and test case. See docs/game-bridge-contract.md.
 *
 * Nothing here talks to the browser: it is data plus pure functions over the response, so a
 * profile is unit-testable without a game running.
 *
 * Method names are the Python names in camelCase (`pending_resume` -> `pendingResume`); the
 * baseline tests rely on that mapping.
 */
import {
  type Dict, get, has, isDict, isList, or, pyEq, pyFloat, pyFormat, pyInt, pyRound, pyStr,
  quantizeDecimal, tryFloat, tryInt, truthy,
} from "../py.js";

function codes(list: Iterable<number>, meaning: string): Record<number, string> {
  const out: Record<number, string> = {};
  for (const c of list) out[c] = meaning;
  return out;
}

function range(start: number, stop: number): number[] {
  const out: number[] = [];
  for (let i = start; i < stop; i++) out.push(i);
  return out;
}

/** Negative `code`s the server returns with `status: 0` — the server API doc's "Errors you should
 * handle" (slots3x3 client guide, commit 56a55e18, read 2026-10-27). Used only to NAME a refusal in
 * an error message; nothing branches on the meaning. */
export const SERVER_ERRORS: Record<number, string> = {
  [-4]: "session invalid or taken over by a newer tab",
  [-6]: "payload could not be parsed",
  [-505]: "action not supported by this game",
  [-101]: "stake sent during free spins (send 0)",
  ...codes(range(-107, -101), "stake, min, max, denomination or lines invalid"),
  [-301]: "bet while in BuyBonus (call luckyspin)",
  [-2002]: "insufficient balance",
  [-2003]: "another operation still running",
  [-108]: "ante wrong or changed during free spins",
  [-109]: "ante not supported",
  [-110]: "ante during a promo",
  ...codes(range(-116, -110), "premium reel mismatch, disabled, promo, with ante, buy bonus or malformed"),
  ...codes([-200, -209], "promo completed or ended"),
  ...codes([-201, -202, -205], "promo cannot start, failed or invalid"),
  [-208]: "stake sent during a promo",
  [-210]: "another promo is active",
  ...codes([-303, -305], "package invalid, or free spins already pending"),
  [-309]: "not allowed in the current state",
  [-310]: "lucky spin already set",
  ...codes(range(-316, -310), "extension off, promo, max win, not eligible, package missing or wrong type"),
  [-1989]: "bets sent too fast",
  [-1990]: "earlier attempt failed; retry refused",
  [-1000]: "unknown action or game code",
  ...codes([-502, -504], "gateway error or no answer from the server"),
};

export interface ReplyError {
  status: number;
  code: number;
  msg: unknown;
  meaning: string;
}

export interface LaunchCaps {
  max_payout: number | null;
  max_multiplier: number | null;
}

/** What a reloaded session is waiting on (see `pendingResume`). */
export type PendingResume = "extension" | "lucky_spin" | "promo" | "freespins" | null;

/** A UI surface: (label, opener role or null if already on screen, closer role or null). */
export type UiSurface = readonly [string, string | null, string | null];

export const SLOT_BUTTONS: Readonly<Record<string, string | null>> = {
  spin: "spin",
  stop: null,
  turbo: "turbo",
  auto: "auto",
  auto_stop_main: null,          // a main-screen stop, where the build has one
  ante: "anteBet",
  // A paid spin feature behind the Buy Bonus PANEL instead of a one-click toggle (Dragon VIP): the
  // panel row that switches it on -- confirmed with `buy_confirm` -- and the main-screen control
  // that switches it off. `premium_reel` is templated by the package's reelId, not a row index.
  ante_on: null,
  ante_off: null,
  premium_reel: null,
  premium_off: null,
  // The panel's own stake stepper. ⚠ it moves the GAME's stake on the click, with no confirm, and
  // closing the panel does not put it back -- re-read the bet after using it.
  buy_next: null,
  buy_prev: null,
  buy_bonus: "buyBonus",
  stake_up: "stakeIncrease",
  stake_down: "stakeDecrease",
  history: "history",
  info: "info",
  settings: "settings",
  sound: "sound",
  trophy: null,
  auto_confirm: "auto:confirm",
  auto_stop: "auto:stop",
  auto_stop_at_win: "auto:stopAtWin",
  auto_close: "auto:close",
  auto_count: "auto:count:{i}",
  bet_row: "bet:row:{i}",
  bet_plus: null,
  bet_minus: null,
  bet_close: "bet:close",
  settings_close: "setting:close",
  settings_sound: null,          // a sound toggle that lives INSIDE settings
  settings_music: null,
  settings_turbo: null,
  info_close: null,
  history_close: null,
  auto_or_freespin: null,        // the control that REPLACES spin during auto / free spins
  message_ok: "message:ok",
  detail_next: "historyDetail:next",
  detail_prev: "historyDetail:prev",
  buy_package: null,
  buy_confirm: null,
  buy_cancel: null,
  // The promo dialogs (Grandma bridge-api.md §7). ⚠ `promo_confirm` CLAIMS or DISCARDS a promo
  // whenever `promo_cancel` is also showing — only `clearPromoPopups` may press it, and only then.
  promo_cancel: null,
  promo_confirm: null,
  promo_list_close: null,
  promo_message_ok: null,
};

export const SLOT_POPUPS: Readonly<Record<string, string | null>> = {
  history: "history",
  history_detail: "historyDetail",
  information: "information",
  autoplay: "autoPlay",
  denominations: "denominations",
  settings: "setting",
  message: "message",
  big_win: "bigWin",
  total_win: null,
  buy_bonus: null,
  freespin_result: "freeSpinResult",
  promo: "promo",
  // The promo DIALOGS, when the bridge reports them one by one (Grandma). null = the game has none
  // the framework knows how to close, and promo handling is a no-op.
  promo_receive: null,           // new promo / discard / end-of-promo result — one dialog
  promo_list: null,
  promo_message: null,
  disconnect: "disconnect",
  ante_notify: null,             // the toast raised when Ante Bet is switched on / off
};

// ── Dialect translation tables (built once from SlotGame's DEFAULT names) ────────────
// `resolve()` turns a literal written in the reference build's dialect back into a ROLE, then out
// again through whichever profile is active. They describe the DEFAULTS, not any one profile.
const DEFAULT_BUTTON_ROLE: Record<string, string> = {};
const DEFAULT_BUTTON_PATTERNS: Array<[RegExp, string]> = [];
for (const [k, v] of Object.entries(SLOT_BUTTONS)) {
  if (v && !v.includes("{")) DEFAULT_BUTTON_ROLE[v] = k;
  if (v && v.includes("{i}")) {
    const esc = v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace("\\{i\\}", "(\\d+)");
    DEFAULT_BUTTON_PATTERNS.push([new RegExp(`^${esc}$`), k]);
  }
}
const DEFAULT_POPUP_ROLE: Record<string, string> = {};
for (const [k, v] of Object.entries(SLOT_POPUPS)) if (v) DEFAULT_POPUP_ROLE[v] = k;

/**
 * The contract ANY slot must satisfy to reuse this suite.
 *
 * Everything here is mechanism-independent. Prefer fixing the bridge over overriding: each
 * override is a permanent fork in the dialect, and a game that matches the defaults needs no
 * profile code beyond `name`.
 */
export class SlotGame {
  name = "slot";
  mechanism = "generic";

  // ── Paytable arithmetic ──────────────────────────────────────────────────────────
  // Every game that prints a paytable prints PER-LINE figures, so `paytable-bet-scaling` needs this
  // on any mechanism. LINE_DIVISOR is the explicit divisor; unset, it falls back to PAYLINES (Zeus's
  // design doc states the formula as `total bet / 5 x symbol multiplier`, 5 being the paylines).
  PAYLINES: number | null = null;        // null = server-configured (read getPaylineCount())
  LINE_DIVISOR: number | null = null;    // per-line bet = base bet / (LINE_DIVISOR or PAYLINES)

  // ── Server contract ──────────────────────────────────────────────────────────────
  BASE_STATE = 1;
  FREESPIN_STATE = 2;
  BUYBONUS_STATE = 3;
  PROMO_STATE = 4;

  F_GAME_STATE = "gameState";
  F_STAKE = "stake";
  F_BASE_STAKE = "baseStake";
  F_PAYOUT = "payout";
  F_BALANCE = "balance";
  F_ROUND_ID = "roundId";
  F_MAX_WIN = "hasReachedMaxWin";
  F_WINLOSE = "winlose";               // payout - stake
  // Paid spin features. Each round echoes what it was bet with, `0` on a plain spin: Ante Bet
  // (Chaos, Dragon VIP) and Premium Reel (Dragon VIP). Both multiply `stake`, never `baseStake`.
  F_ANTE_MULTIPLIER = "anteMultiplier";
  F_PREMIUM_REEL_ID = "premiumReelId";
  F_PREMIUM_REEL_MULTIPLIER = "premiumReelMultiplier";
  F_STEPS = "result";                  // the per-step array a round is made of
  // ...and `gameInfo.lastBet` calls that same array `steps` (Mystic). One parser reads both, or a
  // resumed round reports zero cascades.
  F_STEPS_ALIASES: readonly string[] = ["result", "steps"];
  // Free-spin counters. Some backends nest them (Zeus: freeSpinInfo.freespins); name the wrapper in
  // F_FS_CONTAINER, or leave null when they sit at the top level.
  F_FS_CONTAINER: string | null = null;
  F_FREE_SPINS = "freeSpins";          // REMAINING, not awarded
  // The same count inside `gameInfo.pending`. null = spelled like the BET response. A backend that
  // nests or re-cases the BET counter can still send `pending` flat (Zeus family).
  F_PENDING_FREE_SPINS: string | null = null;
  F_TOTAL_FREE_SPINS = "totalFreeSpin";
  // ...but `gameInfo.lastBet` spells the SAME count with a trailing 's' (Mystic). One parser has to
  // read both, or a resumed bonus reports "0 awarded" and every award assert passes on nothing.
  F_TOTAL_FREE_SPINS_ALIASES: readonly string[] = ["totalFreeSpin", "totalFreeSpins"];
  // Awarded BY THIS ROUND, where the backend reports it separately from the bonus total (Grandma:
  // `wonFreeSpins`). null = no such field, and `wonFreeSpins()` derives it.
  F_WON_FREE_SPINS: string | null = null;

  // ── gameInfo contract ────────────────────────────────────────────────────────────
  // The Buy Bonus package echoed on the round, where the backend sends one. ⚠ Its presence does NOT
  // mean "this round is a purchase" on every game — Grandma carries it on every free spin too.
  F_PACKAGE: string | null = null;

  // ── Win bands ────────────────────────────────────────────────────────────────────
  // `gameInfo.settings.winFormula` is the ladder the CLIENT uses to pick a celebration:
  // `[{name, min, max}, ...]`, min/max are MULTIPLES OF THE BET, not money.
  F_WIN_FORMULA = "winFormula";
  // The tiers that raise a full-screen celebration. For "will this round interrupt us?" the answer
  // is one number: where the FIRST of them starts.
  CELEBRATED_TIERS: readonly string[] = ["BIG", "MEGA", "ULTRA", "EPIC", "SUPER", "SENSATIONAL"];

  F_PENDING: string | null = "pending";
  // `pending`'s own spelling of the state field. null = spelled like the BET response. A backend
  // whose ROUND reports `state`/`nextState` can still send `pending.gameState` (Grandma).
  F_PENDING_GAME_STATE: string | null = null;
  F_LAST_BET: string | null = "lastBet";   // the last completed round, same shape as a BET response
  F_SETTINGS: string | null = "settings";
  F_DEFAULT_BET_LEVEL = "defaultBetLevel";

  // ── Client geometry ──────────────────────────────────────────────────────────────
  VIEWPORT = { width: 531, height: 919 };

  // ── Bridge button names: suite ROLE -> this build's name (null = no such control) ──
  BUTTONS: Readonly<Record<string, string | null>> = SLOT_BUTTONS;
  AUTO_COUNTS: readonly (number | null)[] = [10, 20, 30, 50, null];

  POPUPS: Readonly<Record<string, string | null>> = SLOT_POPUPS;

  // Controls that live INSIDE another panel rather than on the main screen: control NAME (as
  // BUTTONS maps it) -> ROLE of the button that opens its panel. Clicking such a control opens the
  // panel first. Grandma: {"settings:history": "settings", "settings:info": "settings"}.
  BUTTON_VIA: Readonly<Record<string, string>> = {};

  UI_SURFACES: readonly UiSurface[] = [
    ["base screen", null, null],
    ["bet list", "stake_up", "bet_close"],
    ["settings", "settings", "settings_close"],
    ["tutorial", "info", null],
    ["auto play panel", "auto", "auto_close"],
    ["history", "history", null],
  ];
  FEATURE_TOGGLES: readonly string[] = ["turbo", "ante", "sound"];

  // ── How the CLIENT prints money ──────────────────────────────────────────────────
  // A server amount can carry more decimals than the screen shows (Grandma: a 0.195 win). An AI
  // that READS an amount must be compared with it AS DISPLAYED. `DISPLAY_TRUNCATES`: true = extra
  // digits are CUT (0.195 -> 0.19), false = rounded half-up (0.195 -> 0.20).
  DISPLAY_DECIMALS = 2;
  DISPLAY_TRUNCATES = false;

  /** `v` as the client prints it (a string, e.g. "0.19"), or null when `v` is not a number. */
  displayAmount(v: unknown): string | null {
    return quantizeDecimal(v, this.DISPLAY_DECIMALS, this.DISPLAY_TRUNCATES ? "down" : "half_up");
  }

  // ── Capabilities ─────────────────────────────────────────────────────────────────
  HAS_TURBO = true;
  HAS_ANTE_BET = false;
  HAS_PREMIUM_REEL = false;       // paid reel-upgrade packages, `premium-reel-spin`
  HAS_BUY_BONUS = false;
  HAS_AUTOPLAY = true;
  HAS_FREE_SPINS = true;
  HAS_LAST_BET_PANEL = false;
  HAS_TUTORIAL_WEBVIEW = true;
  HAS_SPEED_MODE_GETTER = true;
  HAS_HISTORY_DETAIL_NAV = true;
  HAS_SOUND_GETTERS = false;      // isSoundEnabled()/isMusicEnabled() -> the Sound cases
  // true  = the stake control OPENS a denomination list, and a bet is chosen by tapping a row.
  // false = the stake controls STEP the bet in place, with no list to open.
  HAS_BET_POPUP = true;
  HAS_DEV_MODE = false;           // setDevModeResult(grid) -> one forced spin

  // ── Dialect helpers ──────────────────────────────────────────────────────────────
  button(role: string, fmt?: Record<string, unknown>): string | null {
    const name = this.BUTTONS[role] ?? null;
    return name && fmt && Object.keys(fmt).length ? pyFormat(name, fmt) : name;
  }

  popup(role: string): string | null {
    return this.POPUPS[role] ?? null;
  }

  /** The `visiblePopups` names of this game's promo dialogs; [] when it has none. */
  promoPopups(): string[] {
    return ["promo_receive", "promo_list", "promo_message"]
      .map((r) => this.popup(r)).filter((n): n is string => !!n);
  }

  /** Is `label` one of the promo dialogs? They can show at any idle moment and are not what any
   * scenario is about, so popup readers leave them out and `clearPromoPopups` closes them. */
  isPromoPopup(label: unknown): boolean {
    return this.promoPopups().includes(String(label));
  }

  /** The ROLE of the panel opener a control lives behind (`BUTTON_VIA`), or null when the control
   * is on the main screen. `name` may be a role or any dialect; it is resolved first. */
  via(name: string): string | null {
    return this.BUTTON_VIA[or(this.resolve(name), name) as string] ?? null;
  }

  /**
   * Translate a button name written in ANY dialect into THIS build's name.
   *
   * The scenarios name the reference build's controls literally (`stakeIncrease`, `bet:close`,
   * `auto:count:4`). Those literals are the DEFAULT `BUTTONS` values, so they are role names in
   * disguise — this maps them back to the role and out again through the active profile. Accepts a
   * ROLE key, a DEFAULT name, or an INDEXED default. Returns null when the role maps to null (this
   * build has no such control). An unrecognised name passes through untouched.
   */
  resolve<T extends string | null | undefined>(name: T): string | null | T {
    if (!name) return name;
    if (has(this.BUTTONS, name)) return this.BUTTONS[name] ?? null;
    const role = DEFAULT_BUTTON_ROLE[name];
    if (role !== undefined) return this.BUTTONS[role] ?? null;
    for (const [pat, r] of DEFAULT_BUTTON_PATTERNS) {
      const m = pat.exec(name);
      if (m) {
        const mine = this.BUTTONS[r] ?? null;
        return mine ? pyFormat(mine, { i: m[1] }) : null;
      }
    }
    return name;
  }

  /** Same translation for popup LABELS (`bet` vs `denomination`, `auto` vs `autoplay`). */
  resolvePopup<T extends string | null | undefined>(label: T): string | null | T {
    if (!label) return label;
    if (has(this.POPUPS, label)) return this.POPUPS[label] ?? null;
    const role = DEFAULT_POPUP_ROLE[label];
    return role !== undefined ? (this.POPUPS[role] ?? null) : label;
  }

  /**
   * `{status, code, msg, meaning}` when `raw` is a REJECTED server reply, else null.
   *
   * The server answers a refused action with HTTP 200 and an envelope that has NO `result`:
   * `{status: 0, code: <negative>, msg, timestamp}` (server API doc §01). Read as a round, one parses
   * as stake 0 / payout 0 / balance 0 — a phantom round every check would grade as real.
   * Detection is narrow on purpose: no round object carries `status` or `code`, so a dict that has
   * both and is not a success (`status == 1`, `code >= 0`) is the envelope of a refusal.
   */
  replyError(raw: unknown): ReplyError | null {
    if (!isDict(raw) || !has(raw, "status") || !has(raw, "code")) return null;
    const status = tryInt(get(raw, "status"));
    const code = tryInt(or(get(raw, "code"), 0));
    if (status === null || code === null) return null;
    if (status === 1 && code >= 0) return null;
    return { status, code, msg: or(get(raw, "msg"), ""), meaning: SERVER_ERRORS[code] ?? "not in the server API doc" };
  }

  /**
   * The BET object the engine should read fields from. Some bridges hand back the whole socket
   * ENVELOPE (`{status, code, msg, timestamp, result}`) — Chaos's does. Reading `payout` off an
   * envelope finds nothing and every round parses as a zero-payout loss, so it is peeled here.
   * Detection is narrow: transport fields AND a dict `result`. A cascade round also has `result`,
   * but it is a LIST and there is no `status`/`timestamp`, so it is never mistaken for one.
   */
  unwrap(raw: unknown): Dict {
    if (!isDict(raw)) return (or(raw, {}) as Dict);
    const inner = get(raw, "result");
    if (isDict(inner) && (has(raw, "status") || has(raw, "code")) && has(raw, "timestamp")) return inner;
    return raw;
  }

  /** True while the client self-fires the next round (any non-base state). */
  isBonusState(gameState: unknown): boolean {
    return gameState !== null && gameState !== undefined && !pyEq(gameState, this.BASE_STATE);
  }

  // ── Response reading ─────────────────────────────────────────────────────────────

  /** The free-spin counter container: the nested wrapper, or the round itself. A nested container
   * may be present-but-NULL (Zeus: `freeSpinInfo: null` on the round that ends the feature) — that
   * reads as {} -> (0, 0), which is exactly right. */
  protected fsSource(raw: Dict): Dict {
    const src = this.F_FS_CONTAINER ? or(get(raw, this.F_FS_CONTAINER), {}) : raw;
    return isDict(src) ? src : {};
  }

  /** [remaining, awarded] — wherever this backend keeps them. */
  freeSpins(raw: Dict): [number, number] {
    const src = this.fsSource(raw);
    let awarded = 0;
    for (const key of [this.F_TOTAL_FREE_SPINS, ...this.F_TOTAL_FREE_SPINS_ALIASES]) {
      if (get(src, key) !== null) {                 // profile's own spelling wins; aliases fill in
        awarded = pyInt(or(get(src, key), 0));
        break;
      }
    }
    return [pyInt(or(get(src, this.F_FREE_SPINS), 0)), awarded];
  }

  /** Free spins the bought PACKAGE promises, as the round echoes it — null when this backend sends
   * no package block. What was advertised, as opposed to what `wonFreeSpins` says was handed out. */
  packageSpins(raw: Dict): number | null {
    if (!this.F_PACKAGE) return null;
    const pkg = get(raw, this.F_PACKAGE);
    if (!isDict(pkg)) return null;
    const n = tryInt(or(or(get(pkg, this.F_FREE_SPINS), get(pkg, "freeSpins")), 0));
    return n === null ? null : (n || null);
  }

  /**
   * How many free spins THIS round awarded — 0 on a round that awarded none. Distinct from both
   * `freeSpins()` counters: `remaining` counts down and `awarded` is the bonus TOTAL, which every
   * round of the bonus keeps reporting. A backend that reports it names the field in
   * `F_WON_FREE_SPINS`; otherwise it is the bonus total on the round that started the bonus.
   */
  wonFreeSpins(raw: Dict): number {
    if (this.F_WON_FREE_SPINS) {
      const src = this.F_FS_CONTAINER ? or(get(raw, this.F_FS_CONTAINER), {}) : raw;
      if (isDict(src)) return tryInt(or(get(src, this.F_WON_FREE_SPINS), 0), 0);
      return 0;
    }
    const [, awarded] = this.freeSpins(raw);
    return this.isTrigger(raw) ? awarded : 0;
  }

  // ── gameInfo reading ─────────────────────────────────────────────────────────────

  /** `getGameInfo()`, envelope peeled. Identity for a bridge that already returns the inner object. */
  gameInfo(raw: unknown): Dict {
    return isDict(raw) ? this.unwrap(raw) : {};
  }

  /** The `pending` block: what this session will resume into. `{}` when the backend has none. */
  pending(info: unknown): Dict {
    if (!this.F_PENDING || !isDict(info)) return {};
    return or(get(info, this.F_PENDING), {}) as Dict;
  }

  /** Free spins OWED at load. > 0 means the client will auto-play a bonus as soon as START is
   * clicked, so the framework must drain those rounds before a scenario places its first bet. */
  pendingFreeSpins(info: unknown): number {
    return this._pendingCount(this.pending(info));
  }

  _pendingCount(p: Dict): number {
    return pyInt(or(get(p, this.F_PENDING_FREE_SPINS ?? this.F_FREE_SPINS), 0));
  }

  /** True when `pending` describes an unfinished bonus the client will AUTO-PLAY after Start. Only
   * the `freespins` kind of `pendingResume` — the wheel, an extension offer or a promo wait on the
   * PLAYER, and no free-spin round will come. */
  pendingIsBonus(info: unknown): boolean {
    return this.pendingResume(info) === "freespins";
  }

  // Reload order the server API doc prescribes (§06): draw `lastBet`, apply `pending`, then
  //   pending.gameState == 3     -> show Lucky Spin          (WildExpanding, Ways)
  //   pending.canBuyExtension    -> show the Extension popup (Default, WildExpanding, Ways)
  //   needToStartPromo           -> offer the promo          (every engine)
  F_PENDING_CAN_EXTEND = "canBuyExtension";
  F_NEED_TO_START_PROMO = "needToStartPromo";

  /**
   * What a reloaded session is WAITING ON, from `gameInfo.pending`:
   *
   *     "extension"  the last free spin ended on the Extension offer (`canBuyExtension`)
   *     "lucky_spin" a bought bonus is parked on the Lucky Spin wheel (`gameState == 3`)
   *     "promo"      a free-game promo is running (`gameState == 4`); its spins are CLICKED
   *     "freespins"  an unfinished bonus the client auto-plays as soon as Start is clicked
   *     null         nothing owed
   *
   * Only "freespins" may be drained with `awaitFreespinRound`; the other three wait on the player.
   * Order matters — an extension offer is reported in the free-game state with `freeSpins: 0`, so it
   * is checked before the counter/state rule would call it a bonus.
   */
  pendingResume(info: unknown): PendingResume {
    const p = this.pending(info);
    if (!truthy(p)) return null;
    const state = get(p, this.F_PENDING_GAME_STATE ?? this.F_GAME_STATE);
    if (truthy(get(p, this.F_PENDING_CAN_EXTEND))) return "extension";
    if (pyEq(state, this.BUYBONUS_STATE)) return "lucky_spin";
    if (pyEq(state, this.PROMO_STATE)) return "promo";
    if (this._pendingCount(p) || this.isBonusState(state)) return "freespins";
    return null;
  }

  /** `gameInfo.needToStartPromo`: the client will OFFER a free-game promo after load. */
  promoOffered(info: unknown): boolean {
    return isDict(info) && truthy(get(info, this.F_NEED_TO_START_PROMO));
  }

  /** `gameInfo.lastBet` — the last completed round, in BET-response shape. */
  lastBet(info: unknown): Dict {
    if (!this.F_LAST_BET || !isDict(info)) return {};
    return or(get(info, this.F_LAST_BET), {}) as Dict;
  }

  settings(info: unknown): Dict {
    if (!this.F_SETTINGS || !isDict(info)) return {};
    return or(get(info, this.F_SETTINGS), {}) as Dict;
  }

  /** The configured opening bet level (an index into `settings.denominations`), or null where the
   * backend does not report one. */
  defaultBetLevel(info: unknown): unknown {
    return get(this.settings(info), this.F_DEFAULT_BET_LEVEL);
  }

  /** The win bands, or `[]` where the backend sends none. ⚠ Looked for in BOTH `settings` and
   * `settings.general`: no bands means no threshold, and the big-win skip silently stops. */
  winFormula(info: unknown): unknown[] {
    const s = this.settings(info);
    let v = get(s, this.F_WIN_FORMULA);
    if (!isList(v)) {
      const general = get(s, "general");
      if (isDict(general)) v = get(general, this.F_WIN_FORMULA);
    }
    return isList(v) ? v : [];
  }

  /** The payout/bet multiple at which a big-win celebration starts, or null when this build
   * reports no bands. ONE number: the lowest floor of the celebrated tiers. ⚠ Read from the
   * LAUNCH PAYLOAD, never hard-coded. null means "cannot say", not "no big win". */
  bigWinThreshold(info: unknown): number | null {
    const floors: number[] = [];
    for (const band of this.winFormula(info)) {
      if (!isDict(band)) continue;
      if (this.CELEBRATED_TIERS.includes(pyStr(or(get(band, "name"), "")).trim().toUpperCase())) {
        const f = tryFloat(get(band, "min"));
        if (f !== null) floors.push(f);
      }
    }
    return floors.length ? Math.min(...floors) : null;
  }

  /** Would THIS round raise a big-win celebration? `payout / baseStake >= threshold` — ⚠
   * `baseStake`, not `stake`: a free spin costs 0. A hair of tolerance, because `0.6 / 0.1` is
   * 5.999999999999999 and a bare `>=` would call the 6x round a medium win. */
  isBigWin(raw: Dict, threshold: unknown): boolean {
    if (threshold === null || threshold === undefined) return false;
    const base = this.baseStake(raw);
    if (!base) return false;
    const payout = tryFloat(or(get(raw, this.F_PAYOUT), 0));
    if (payout === null) return false;
    return payout > 0 && payout / base >= pyFloat(threshold) - 1e-9;
  }

  // ── Max-win cap ──────────────────────────────────────────────────────────────────
  // Server API doc, "Max-win cap" (every engine):
  //     maxWin = min(MaxPayout, baseStake x MaxMultiplier)       a 0 in either = that half unset
  // counted over the WHOLE round (`accumulatedWinAmount`, which INCLUDES the current spin), so the
  // spin that crosses it pays only what is left. `rawPayout` is the win before the cap, `payout`
  // after it. Mystic's capped lastBet: accumulated 10.00 == maxPayout, payout 2.00, rawPayout 11.20
  // -> 8.00 already won, 2.00 left, 2.00 credited. The reply's own `maxWin` is 0 on WildExpanding
  // and Cascading (Joker, Mystic) and on Grandma's purchase round, so the cap falls back to the
  // launch settings, kept by `rememberLaunch`.
  F_RAW_PAYOUT = "rawPayout";
  F_ACCUMULATED = "accumulatedWinAmount";
  F_MAX_WIN_CAP = "maxWin";
  F_MAX_PAYOUT = "maxPayout";          // gameInfo.settings (Ways: settings.general)
  F_MAX_MULTIPLIER = "maxMultiplier";
  launch_caps: LaunchCaps | null = null;   // from the latest gameInfo

  /** Keep the max-win settings of the launch payload just read. A payload with no settings block
   * leaves the previous copy alone. Called on EVERY load: a `max_win_x` paytable changes
   * `maxPayout`, and the next load is where it shows. Each Bet freezes its cap at parse time. */
  rememberLaunch(info: unknown): void {
    const s = this.settings(info);
    if (!truthy(s)) return;
    const g = get(s, "general");
    const general = isDict(g) ? g : {};
    const num = (key: string): number | null => {
      let v = get(s, key);
      if (v === null) v = get(general, key);
      const f = tryFloat(v);
      return f !== null && f > 0 ? f : null;
    };
    this.launch_caps = { max_payout: num(this.F_MAX_PAYOUT), max_multiplier: num(this.F_MAX_MULTIPLIER) };
  }

  /** The round's max-win cap in currency, or null when neither the reply nor the launch settings
   * set one. The reply's own `maxWin` wins when it is filled (> 0). */
  maxWinCap(raw: Dict): number | null {
    const v = tryFloat(or(get(raw, this.F_MAX_WIN_CAP), 0), 0);
    if (v > 0) return v;
    const caps = this.launch_caps ?? { max_payout: null, max_multiplier: null };
    const candidates: number[] = [];
    if (truthy(caps.max_payout)) candidates.push(caps.max_payout as number);
    const base = this.baseStake(raw);
    if (truthy(caps.max_multiplier) && base > 0) candidates.push(pyRound(base * (caps.max_multiplier as number), 10));
    return candidates.length ? Math.min(...candidates) : null;
  }

  /** What the round had already won BEFORE this spin: `accumulatedWinAmount - payout`. 0 where the
   * reply carries no running total, which is right for a single-spin round. */
  roundWonBefore(raw: Dict): number {
    const acc = get(raw, this.F_ACCUMULATED);
    if (acc === null) return 0;
    const a = tryFloat(acc);
    const p = tryFloat(or(get(raw, this.F_PAYOUT), 0));
    if (a === null || p === null) return 0;
    return pyMax(a - p, 0);
  }

  /** What THIS spin may still credit under the cap (`cap - roundWonBefore`, never below 0), or null
   * when there is no cap. `cap` defaults to `maxWinCap(raw)`; pass the one a Bet froze. */
  maxWinLeft(raw: Dict, cap: number | null = null): number | null {
    if (cap === null) cap = this.maxWinCap(raw);
    if (cap === null) return null;
    return pyMax(pyRound(cap - this.roundWonBefore(raw), 10), 0);
  }

  /** The state this backend reports. */
  gameState(raw: Dict): unknown {
    return get(raw, this.F_GAME_STATE);
  }

  /** The per-step array a round is made of. Cascade rounds have many; a single-grid round has one,
   * so `Bet.cascades` stays meaningful across mechanisms. */
  steps(raw: Dict): unknown[] {
    let v = get(raw, this.F_STEPS);
    if (v === null) {
      for (const key of this.F_STEPS_ALIASES) {        // profile's own spelling wins; aliases fill in
        if (get(raw, key) !== null) {
          v = get(raw, key);
          break;
        }
      }
    }
    if (isList(v)) return v;
    return v === null ? [] : [v];
  }

  /** Did THIS round award the bonus? From the response, never from client state. */
  isTrigger(raw: Dict): boolean {
    const [, awarded] = this.freeSpins(raw);
    return awarded > 0 || this.isBonusState(this.gameState(raw));
  }

  // ── Paid spin features (Ante Bet, Premium Reel) ──────────────────────────────────

  /** The payout base for the round. Under Ante Bet or a Premium Reel this is NOT what was deducted:
   * the player pays `stake`, and every payout still derives from this. */
  baseStake(raw: Dict): number {
    let v = get(raw, this.F_BASE_STAKE);
    if (v === null) v = get(raw, this.F_STAKE);
    return tryFloat(or(v, 0), 0);
  }

  /** The ante multiplier THIS round was bet at, or null for a round bet without it (the server
   * sends `0`). */
  anteMultiplier(raw: Dict): number | null {
    const f = tryFloat(get(raw, this.F_ANTE_MULTIPLIER));
    return f !== null && f > 0 ? f : null;
  }

  /** Bet with Ante on: a non-zero ante multiplier, or, for a build that omits the field, a stake
   * above the payout base. That fallback misreads a Premium Reel spin, so a round that echoes a
   * package is never taken for ante. */
  isAnteRound(raw: Dict): boolean {
    if (this.anteMultiplier(raw) !== null) return true;
    if (this.premiumReelId(raw)) return false;
    const stake = tryFloat(or(get(raw, this.F_STAKE), 0));
    return stake !== null && stake > this.baseStake(raw) + 1e-9;
  }

  /** The Premium Reel package this round was bet with, 0 for a plain round. Opaque: `23` is a
   * package id, not "reels 2 and 3". */
  premiumReelId(raw: Dict): number {
    return tryInt(or(get(raw, this.F_PREMIUM_REEL_ID), 0), 0);
  }

  /** The package's stake multiplier as this round echoed it, 0 for a plain round. */
  premiumReelMultiplier(raw: Dict): number {
    return tryFloat(or(get(raw, this.F_PREMIUM_REEL_MULTIPLIER), 0), 0);
  }

  // Mechanism hooks — meaningful only on the subclass that owns them.
  clusterWins(_steps: unknown[], _payout = 0): number {
    return 0;
  }

  winClusters(_steps: unknown[]): number {
    return 0;
  }

  // ── Bridge contract ──────────────────────────────────────────────────────────────
  // A missing REQUIRED function aborts the suite by name: silent corruption is not an option.
  REQUIRED_API: readonly string[] = [
    "startGame", "waitForSpinClickable", "getButtonRect", "waitForButtonClickable",
    "awaitBetResponse", "awaitFreespinRound", "getSnapshot", "getGameInfo",
  ];
  // Strongly recommended: `canDo(action)` is the bridge's own "would this click do anything?".
  RECOMMENDED_API: readonly string[] = ["canDo"];
  OPTIONAL_API: Readonly<Record<string, string>> = {
    dismissPopups: "clean teardown between runs; popup cases",
    getHistoryRows: "History rows + the row to click",
    getHistoryDetailState: "free-spin sub-ticket paging",
    getSpeedMode: "hard check that Turbo really toggled",
    getTutorialRect: "where to wheel a cross-origin tutorial WebView",
    "startEventCapture/endEventCapture": "popup + animation-window diagnostics",
    "getGameInfo/getDenominations": "denominations, currency, default bet level, pending",
    "setForcedResult/setDevModeResult": "one deterministic spin",
  };

  /** Per-line bet — the figure a paytable prints against each symbol row. From the BASE stake: an
   * ante multiplies what the player pays without moving the payout base. 0 when no divisor is known. */
  lineBet(baseStake: number): number {
    const div = this.LINE_DIVISOR || this.PAYLINES;
    return div ? baseStake / div : 0;
  }

  apiCheckJs(): string {
    const req = this.REQUIRED_API.map((f) => `'${f}'`).join(", ");
    return "() => { const b = window.__gameTestBridge;"
      + " if (!b) return ['__gameTestBridge'];"
      + ` return [${req}].filter(f => typeof b[f] !== 'function'); }`;
  }
}

/** Python's two-argument `max(a, b)`: returns `a` unless `b > a` (so NaN in `a` survives). */
export function pyMax(a: number, b: number): number {
  return b > a ? b : a;
}

/** Python's two-argument `min(a, b)`: returns `a` unless `b < a`. */
export function pyMin(a: number, b: number): number {
  return b < a ? b : a;
}
