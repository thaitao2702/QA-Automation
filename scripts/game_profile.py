#!/usr/bin/env python3
"""
Game PROFILES — the per-game half of the scenario suite.

`scenario_engine.py` holds the MECHANISM: how to drive a slot from its server BET response and grade
it. This module holds the DIALECT: what a build calls its buttons and popups, which features it
ships, and how its response is shaped.

    SlotGame            lifecycle, buttons, popups, money, autoplay, language  (mechanism-free)
      CascadeGame       cluster-pays with cascading steps        -> MysticWishes
      HoldSpinGame      respins that HOLD symbols in place       -> Zeus

A new game subclasses whichever matches its bonus, declares only what it names differently, and
inherits every scenario and test case. See docs/game-bridge-contract.md.

Nothing here talks to the browser: it is data plus pure functions over the response, so a profile is
unit-testable without a game running.
"""
from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation


def _codes(codes, meaning: str) -> dict:
    return {c: meaning for c in codes}


# Negative `code`s the server returns with `status: 0` — the server API doc's "Errors you should
# handle" (slots3x3 client guide, commit 56a55e18, read 2026-10-27). Shared by every engine. Used only
# to NAME a refusal in an error message; nothing branches on the meaning.
SERVER_ERRORS: dict = {
    -4: "session invalid or taken over by a newer tab",
    -6: "payload could not be parsed",
    -505: "action not supported by this game",
    -101: "stake sent during free spins (send 0)",
    **_codes(range(-107, -101), "stake, min, max, denomination or lines invalid"),
    -301: "bet while in BuyBonus (call luckyspin)",
    -2002: "insufficient balance",
    -2003: "another operation still running",
    -108: "ante wrong or changed during free spins",
    -109: "ante not supported",
    -110: "ante during a promo",
    **_codes(range(-116, -110), "premium reel mismatch, disabled, promo, with ante, buy bonus or malformed"),
    **_codes((-200, -209), "promo completed or ended"),
    **_codes((-201, -202, -205), "promo cannot start, failed or invalid"),
    -208: "stake sent during a promo",
    -210: "another promo is active",
    **_codes((-303, -305), "package invalid, or free spins already pending"),
    -309: "not allowed in the current state",
    -310: "lucky spin already set",
    **_codes(range(-316, -310), "extension off, promo, max win, not eligible, package missing or wrong type"),
    -1989: "bets sent too fast",
    -1990: "earlier attempt failed; retry refused",
    -1000: "unknown action or game code",
    **_codes((-502, -504), "gateway error or no answer from the server"),
}


class SlotGame:
    """The contract ANY slot must satisfy to reuse this suite.

    Everything here is mechanism-independent. Prefer fixing the bridge over overriding: each override
    is a permanent fork in the dialect, and a game that matches the defaults needs no profile code
    beyond `name`.
    """

    name: str = "slot"
    mechanism: str = "generic"

    # ── Paytable arithmetic ──────────────────────────────────────────────────────────────
    # Every game that prints a paytable prints PER-LINE figures, so `paytable-bet-scaling` needs
    # this arithmetic on any mechanism — it lived on LineGame, which made a scenario the index
    # calls portable crash with AttributeError on Zeus. LINE_DIVISOR is the explicit divisor; where
    # a game does not set one it falls back to PAYLINES, which is the same number for Zeus (its
    # design doc states the formula as `total bet / 5 x symbol multiplier`, 5 being the paylines).
    PAYLINES: int | None = None        # None = server-configured (read getPaylineCount())
    LINE_DIVISOR: int | None = None    # per-line bet = base bet / (LINE_DIVISOR or PAYLINES)

    # ── Server contract ──────────────────────────────────────────────────────────────────
    BASE_STATE: int = 1
    FREESPIN_STATE: int = 2
    BUYBONUS_STATE: int = 3
    PROMO_STATE: int = 4

    F_GAME_STATE = "gameState"
    F_STAKE = "stake"
    F_BASE_STAKE = "baseStake"
    F_PAYOUT = "payout"
    F_BALANCE = "balance"
    F_ROUND_ID = "roundId"
    F_MAX_WIN = "hasReachedMaxWin"
    F_WINLOSE = "winlose"               # payout - stake
    # Paid spin features. Each round echoes what it was bet with, `0` on a plain spin: Ante Bet
    # (Chaos, Dragon VIP) and Premium Reel (Dragon VIP, games/dragon-vip/docs/raw/
    # client-integration.vi.md §4). Both multiply `stake` and leave `baseStake` alone.
    F_ANTE_MULTIPLIER = "anteMultiplier"
    F_PREMIUM_REEL_ID = "premiumReelId"
    F_PREMIUM_REEL_MULTIPLIER = "premiumReelMultiplier"
    F_STEPS = "result"                  # the per-step array a round is made of
    # ...and `gameInfo.lastBet` calls that same array `steps` (Mystic). Second alias, same reason as
    # the free-spin one below: one parser reads both, or a resumed round reports zero cascades.
    F_STEPS_ALIASES: tuple = ("result", "steps")
    # Free-spin counters. Some backends nest them (Zeus: freeSpinInfo.freespins); name the wrapper
    # in F_FS_CONTAINER, or leave None when they sit at the top level.
    F_FS_CONTAINER = None
    F_FREE_SPINS = "freeSpins"          # REMAINING, not awarded
    # The same count inside `gameInfo.pending`. None = spelled like the BET response. A backend
    # that nests or re-cases the BET counter can still send `pending` flat (Zeus family).
    F_PENDING_FREE_SPINS = None
    F_TOTAL_FREE_SPINS = "totalFreeSpin"
    # ...but `gameInfo.lastBet` spells the SAME count with a trailing 's' (Mystic: the BET response
    # says `totalFreeSpin`, gameInfo's copy of it says `totalFreeSpins`). One parser has to read
    # both, or a resumed bonus reports "0 awarded" and every award assert silently passes on nothing.
    F_TOTAL_FREE_SPINS_ALIASES: tuple = ("totalFreeSpin", "totalFreeSpins")
    # Awarded BY THIS ROUND, where the backend reports it separately from the bonus total (Grandma:
    # `wonFreeSpins`). None = this backend has no such field, and the default below derives it.
    F_WON_FREE_SPINS = None

    # ── gameInfo contract ────────────────────────────────────────────────────────────────
    # `getGameInfo()` is the state a RELOAD resumes into, and it must be read BEFORE START: the
    # client auto-plays any owed bonus the moment START is clicked, so after that the evidence of
    # what was owed is gone. `pending.freeSpins > 0` is the authoritative "this session will resume
    # a bonus" signal. Set F_PENDING to None for a backend that has no such block.
    # The Buy Bonus package echoed on the round, where the backend sends one. None = no such block.
    # ⚠ Its presence does NOT mean "this round is a purchase" on every game — Grandma carries it on
    # the purchase AND on every free spin of the bonus it bought.
    F_PACKAGE = None

    # ── Win bands ────────────────────────────────────────────────────────────────────────
    # `gameInfo.settings.winFormula` is the ladder the CLIENT uses to decide which celebration to
    # play: `[{name, min, max}, ...]` where min/max are MULTIPLES OF THE BET, not money. Mystic
    # ships SLIM 0-1, SMALL 1-2.5, MEDIUM 2.5-6, BIG 6-15, MEGA 15-40, ULTRA 40+.
    F_WIN_FORMULA = "winFormula"
    # The tiers that raise a full-screen celebration. They differ only in length and artwork, so
    # for "will this round interrupt us?" the answer is one number: where the FIRST of them starts.
    CELEBRATED_TIERS: tuple = ("BIG", "MEGA", "ULTRA", "EPIC", "SUPER", "SENSATIONAL")

    F_PENDING = "pending"
    # `pending`'s own spelling of the state field. None = spelled like the BET response. A backend
    # whose ROUND reports `state`/`nextState` can still send `pending.gameState` (Grandma), and
    # reading the round's spelling there returns None — which would make `pending_is_bonus` fall
    # back to the counter alone and miss a max-win resume.
    F_PENDING_GAME_STATE = None
    F_LAST_BET = "lastBet"              # the last completed round, same shape as a BET response
    F_SETTINGS = "settings"
    F_DEFAULT_BET_LEVEL = "defaultBetLevel"

    # ── Client geometry ──────────────────────────────────────────────────────────────────
    VIEWPORT = {"width": 531, "height": 919}

    # ── Bridge button names: suite ROLE -> this build's name (None = no such control) ────
    BUTTONS: dict = {
        "spin": "spin",
        "stop": None,
        "turbo": "turbo",
        "auto": "auto",
        "auto_stop_main": None,          # a main-screen stop, where the build has one
        "ante": "anteBet",
        # A paid spin feature that sits behind the Buy Bonus PANEL instead of a one-click toggle
        # (Dragon VIP): the panel row that switches it on -- confirmed with `buy_confirm` -- and the
        # main-screen control that switches it off again. `premium_reel` is templated by the
        # package's reelId, not by a row index. None on a build without them.
        "ante_on": None,
        "ante_off": None,
        "premium_reel": None,
        "premium_off": None,
        # The panel's own stake stepper. ⚠ it moves the GAME's stake on the click, with no confirm,
        # and closing the panel does not put it back -- re-read the bet after using it.
        "buy_next": None,
        "buy_prev": None,
        "buy_bonus": "buyBonus",
        "stake_up": "stakeIncrease",
        "stake_down": "stakeDecrease",
        "history": "history",
        "info": "info",
        "settings": "settings",
        "sound": "sound",
        "trophy": None,
        "auto_confirm": "auto:confirm",
        "auto_stop": "auto:stop",
        "auto_stop_at_win": "auto:stopAtWin",
        "auto_close": "auto:close",
        "auto_count": "auto:count:{i}",
        "bet_row": "bet:row:{i}",
        "bet_plus": None,
        "bet_minus": None,
        "bet_close": "bet:close",
        "settings_close": "setting:close",
        "settings_sound": None,          # a sound toggle that lives INSIDE settings
        "settings_music": None,
        "settings_turbo": None,
        "info_close": None,
        "history_close": None,
        "auto_or_freespin": None,        # the control that REPLACES spin during auto / free spins
        "message_ok": "message:ok",
        "detail_next": "historyDetail:next",
        "detail_prev": "historyDetail:prev",
        "buy_package": None,
        "buy_confirm": None,
        "buy_cancel": None,
    }
    AUTO_COUNTS: tuple = (10, 20, 30, 50, None)

    POPUPS: dict = {
        "history": "history",
        "history_detail": "historyDetail",
        "information": "information",
        "autoplay": "autoPlay",
        "denominations": "denominations",
        "settings": "setting",
        "message": "message",
        "big_win": "bigWin",
        "total_win": None,
        "buy_bonus": None,
        "freespin_result": "freeSpinResult",
        "promo": "promo",
        "disconnect": "disconnect",
        "ante_notify": None,             # the toast raised when Ante Bet is switched on / off
    }

    # Controls that live INSIDE another panel rather than on the main screen: control NAME (as
    # BUTTONS maps it) -> ROLE of the button that opens the panel it lives in. Clicking such a
    # control opens the panel first when the control is not already on screen, so a scenario
    # still just clicks `history` / `info`. Grandma Pets: History and the guide moved into
    # Settings -> {"settings:history": "settings", "settings:info": "settings"}. Empty = every
    # control is on the main screen. See docs/game-bridge-contract.md §8 "Main screen (legacy UI) or Settings menu (new UI)".
    BUTTON_VIA: dict = {}

    # (label, opener role or None if already on screen, closer role or None)
    UI_SURFACES: tuple = (
        ("base screen", None, None),
        ("bet list", "stake_up", "bet_close"),
        ("settings", "settings", "settings_close"),
        ("tutorial", "info", None),
        ("auto play panel", "auto", "auto_close"),
        ("history", "history", None),
    )
    FEATURE_TOGGLES: tuple = ("turbo", "ante", "sound")

    # ── How the CLIENT prints money ──────────────────────────────────────────────────────
    # A server amount can carry more decimals than the screen shows (Grandma: a 0.195 win, a
    # 15069.237 balance). An AI that READS an amount must be compared with the amount AS DISPLAYED,
    # not the raw figure, or a correct screen fails on formatting alone (TC-011: read "0.19",
    # compared with 0.195). `DISPLAY_TRUNCATES`: True = extra digits are CUT (0.195 -> 0.19),
    # False = rounded half-up (0.195 -> 0.20). Confirm on the live build before setting it.
    DISPLAY_DECIMALS: int = 2
    DISPLAY_TRUNCATES: bool = False

    def display_amount(self, v):
        """`v` as the client prints it (a string, e.g. "0.19"), or None when `v` is not a number."""
        try:
            d = Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            return None
        q = Decimal(1).scaleb(-self.DISPLAY_DECIMALS)
        return str(d.quantize(q, rounding=ROUND_DOWN if self.DISPLAY_TRUNCATES else ROUND_HALF_UP))

    # ── Capabilities ─────────────────────────────────────────────────────────────────────
    HAS_TURBO: bool = True
    HAS_ANTE_BET: bool = False
    HAS_PREMIUM_REEL: bool = False       # paid reel-upgrade packages, `premium-reel-spin`
    HAS_BUY_BONUS: bool = False
    HAS_AUTOPLAY: bool = True
    HAS_FREE_SPINS: bool = True
    HAS_LAST_BET_PANEL: bool = False
    HAS_TUTORIAL_WEBVIEW: bool = True
    HAS_SPEED_MODE_GETTER: bool = True
    HAS_HISTORY_DETAIL_NAV: bool = True
    HAS_SOUND_GETTERS: bool = False      # isSoundEnabled()/isMusicEnabled() -> the Sound cases
    # True  = the stake control OPENS a denomination list, and a bet is chosen by tapping a row.
    # False = the stake controls STEP the bet in place, with no list to open (Zeus: nextBet /
    #         previousBet). Scenarios branch on this instead of assuming the reference flow.
    HAS_BET_POPUP: bool = True
    HAS_DEV_MODE: bool = False           # setDevModeResult(grid) -> one forced spin

    # ── Dialect helpers ──────────────────────────────────────────────────────────────────
    def button(self, role: str, **fmt) -> str | None:
        name = self.BUTTONS.get(role)
        return name.format(**fmt) if (name and fmt) else name

    def popup(self, role: str) -> str | None:
        return self.POPUPS.get(role)

    def via(self, name: str) -> str | None:
        """The ROLE of the panel opener a control lives behind (`BUTTON_VIA`), or None when the
        control is on the main screen. `name` may be a role or any dialect; it is resolved first."""
        return self.BUTTON_VIA.get(self.resolve(name) or name)

    def resolve(self, name: str) -> str | None:
        """Translate a button name written in ANY dialect into THIS build's name.

        The scenarios were written against the reference build and name its controls literally
        (`stakeIncrease`, `bet:close`, `auto:count:4`). Those literals are simply the DEFAULT
        `BUTTONS` values, so they are really role names in disguise — this maps them back to the
        role and out again through the active profile. Three inputs are accepted:

          * a ROLE key           -> `stake_up`      -> this build's name
          * a DEFAULT name       -> `stakeIncrease` -> this build's name (Chaos: `betPlus`)
          * an INDEXED default   -> `auto:count:4`  -> `autoplay:count:4` on Chaos

        Returns `None` when the role resolves to `None` (this build has no such control), which the
        callers already treat as "absent". An unrecognised name passes through untouched, so a
        game-specific control a scenario names directly still works.

        Without this, every hardcoded literal in the engine silently addressed the reference
        build's controls on EVERY game — a name a different build does not have, which
        `waitForButtonClickable` then burns its whole timeout on.
        """
        if not name:
            return name
        if name in self.BUTTONS:                       # already a role
            return self.BUTTONS[name]
        role = _DEFAULT_BUTTON_ROLE.get(name)
        if role is not None:
            return self.BUTTONS.get(role)
        for pat, (role, tmpl) in _DEFAULT_BUTTON_PATTERNS.items():
            m = pat.match(name)
            if m:
                mine = self.BUTTONS.get(role)
                return mine.format(i=m.group(1)) if mine else None
        return name                                    # unknown -> the caller meant it literally

    def resolve_popup(self, label: str) -> str | None:
        """Same translation for popup LABELS (`bet` vs `denomination`, `auto` vs `autoplay`)."""
        if not label:
            return label
        if label in self.POPUPS:
            return self.POPUPS[label]
        role = _DEFAULT_POPUP_ROLE.get(label)
        return self.POPUPS.get(role) if role is not None else label

    def reply_error(self, raw) -> dict | None:
        """`{status, code, msg, meaning}` when `raw` is a REJECTED server reply, else None.

        The server answers a refused action with HTTP 200 and an envelope that has NO `result`:
        `{status: 0, code: <negative>, msg, timestamp}` (server API doc §01). Grandma, Dragon and
        Joker document that their bridges keep these out of the round stream; Zeus, Dragon VIP,
        Mystic and Chaos do not say. Read as a round, one parses as stake 0 / payout 0 /
        balance 0 — a phantom round every check would then grade as real. So the parser refuses it.

        Detection is narrow on purpose: no round object carries `status` or `code` (checked on
        every captured fixture), so a dict that has both and is not a success (`status == 1`,
        `code >= 0`) is the envelope of a refusal."""
        if not isinstance(raw, dict) or "status" not in raw or "code" not in raw:
            return None
        try:
            status = int(raw.get("status"))
            code = int(raw.get("code") or 0)
        except (TypeError, ValueError):
            return None
        if status == 1 and code >= 0:
            return None
        return {"status": status, "code": code, "msg": raw.get("msg") or "",
                "meaning": SERVER_ERRORS.get(code, "not in the server API doc")}

    def unwrap(self, raw: dict) -> dict:
        """The BET object the engine should read fields from.

        Most bridges hand back the inner result. Some hand back the whole socket ENVELOPE
        (`{status, code, msg, timestamp, result}`) — Chaos's does, explicitly. Reading `payout` off
        an envelope finds nothing and every round silently parses as a zero-payout loss, so the
        envelope is peeled here, once, for every profile.

        Detection is deliberately narrow: an envelope has the transport fields AND a dict `result`.
        A cascade game's inner object also has a `result` key, but it is a LIST of steps and it
        carries no `status`/`timestamp`, so it is never mistaken for an envelope.
        """
        if not isinstance(raw, dict):
            return raw or {}
        inner = raw.get("result")
        if isinstance(inner, dict) and ("status" in raw or "code" in raw) and "timestamp" in raw:
            return inner
        return raw

    def is_bonus_state(self, game_state) -> bool:
        """True while the client self-fires the next round (any non-base state)."""
        return game_state is not None and game_state != self.BASE_STATE

    # ── Response reading ─────────────────────────────────────────────────────────────────
    def free_spins(self, raw: dict) -> tuple:
        """(remaining, awarded) — wherever this backend keeps them.

        A nested container may be present-but-NULL: Zeus sends `freeSpinInfo: null` on the round that
        ends the feature. `or {}` turns that into (0, 0), which is exactly right — no respins left —
        so the terminating condition reads naturally instead of needing a null special case."""
        src = (raw.get(self.F_FS_CONTAINER) or {}) if self.F_FS_CONTAINER else raw
        if not isinstance(src, dict):
            src = {}
        awarded = 0
        for key in (self.F_TOTAL_FREE_SPINS, *self.F_TOTAL_FREE_SPINS_ALIASES):
            if src.get(key) is not None:                 # profile's own spelling wins; aliases fill in
                awarded = int(src.get(key) or 0)
                break
        return int(src.get(self.F_FREE_SPINS) or 0), awarded

    def package_spins(self, raw: dict):
        """Free spins the bought PACKAGE promises, as the round echoes it — `None` when this backend
        sends no package block. Distinct from `won_free_spins`: this is what was advertised, that is
        what the server actually handed out, and a case exists to check they agree."""
        if not self.F_PACKAGE:
            return None
        pkg = raw.get(self.F_PACKAGE)
        if not isinstance(pkg, dict):
            return None
        try:
            return int(pkg.get(self.F_FREE_SPINS) or pkg.get("freeSpins") or 0) or None
        except (TypeError, ValueError):
            return None

    def won_free_spins(self, raw: dict) -> int:
        """How many free spins THIS round awarded — 0 on a round that awarded none.

        Distinct from both counters `free_spins()` returns: `remaining` counts down through a bonus
        and `awarded` is the bonus TOTAL, which every round of the bonus keeps reporting. Only this
        answers "did this round hand out spins", which is what `is_trigger` and every award assert
        actually ask.

        A backend that reports it outright names the field in `F_WON_FREE_SPINS`. Otherwise it is
        derived the way the older profiles already behaved: the bonus total on the round that
        started the bonus, 0 on every round inside it."""
        if self.F_WON_FREE_SPINS:
            src = (raw.get(self.F_FS_CONTAINER) or {}) if self.F_FS_CONTAINER else raw
            if isinstance(src, dict):
                try:
                    return int(src.get(self.F_WON_FREE_SPINS) or 0)
                except (TypeError, ValueError):
                    return 0
            return 0
        _, awarded = self.free_spins(raw)
        return awarded if self.is_trigger(raw) else 0

    # ── gameInfo reading ─────────────────────────────────────────────────────────────────
    def game_info(self, raw: dict) -> dict:
        """`getGameInfo()`, envelope peeled. Identity for a bridge that already returns the inner
        object, so it is safe to call on whatever this build hands back."""
        return self.unwrap(raw) if isinstance(raw, dict) else {}

    def pending(self, info: dict) -> dict:
        """The `pending` block: what this session will resume into. `{}` when the backend has none."""
        if not self.F_PENDING or not isinstance(info, dict):
            return {}
        return info.get(self.F_PENDING) or {}

    def pending_free_spins(self, info: dict) -> int:
        """Free spins OWED at load. > 0 means the client will auto-play a bonus as soon as START is
        clicked, with no spin click from us — so the framework must drain those rounds before a
        scenario places its first bet, or they land in that scenario's stream as phantom rounds."""
        return self._pending_count(self.pending(info))

    def _pending_count(self, p: dict) -> int:
        return int(p.get(self.F_PENDING_FREE_SPINS or self.F_FREE_SPINS) or 0)

    def pending_is_bonus(self, info: dict) -> bool:
        """True when `pending` describes an unfinished bonus the client will AUTO-PLAY after Start,
        by counter OR by state. Both are checked for the same reason the drain loop checks both:
        `hasReachedMaxWin` can leave a non-base state with the counter already at 0, and a max-win
        truncation leaves the counter above 0 with the state already back at base.

        Only the `freespins` kind of `pending_resume` — a session waiting on the Lucky Spin wheel,
        an extension offer or a promo is waiting on the PLAYER, and no free-spin round will come."""
        return self.pending_resume(info) == "freespins"

    # Reload order the server API doc prescribes (§06): draw `lastBet`, apply `pending`, then
    #   pending.gameState == 3     -> show Lucky Spin          (WildExpanding, Ways)
    #   pending.canBuyExtension    -> show the Extension popup (Default, WildExpanding, Ways)
    #   needToStartPromo           -> offer the promo          (every engine)
    F_PENDING_CAN_EXTEND = "canBuyExtension"
    F_NEED_TO_START_PROMO = "needToStartPromo"

    def pending_resume(self, info: dict) -> str | None:
        """What a reloaded session is WAITING ON, from `gameInfo.pending`:

            "extension"  the last free spin ended on the Extension offer (`canBuyExtension`)
            "lucky_spin" a bought bonus is parked on the Lucky Spin wheel (`gameState == 3`)
            "promo"      a free-game promo is running (`gameState == 4`); its spins are CLICKED
            "freespins"  an unfinished bonus the client auto-plays as soon as Start is clicked
            None         nothing owed

        Only "freespins" may be drained with `awaitFreespinRound`. The other three wait on the
        player: draining them waits for rounds that never come, and the boot fails on a healthy
        session. Order matters — an extension offer is reported in the free-game state with
        `freeSpins: 0`, so it is checked before the counter/state rule would call it a bonus."""
        p = self.pending(info)
        if not p:
            return None
        state = p.get(self.F_PENDING_GAME_STATE or self.F_GAME_STATE)
        if p.get(self.F_PENDING_CAN_EXTEND):
            return "extension"
        if state == self.BUYBONUS_STATE:
            return "lucky_spin"
        if state == self.PROMO_STATE:
            return "promo"
        if self._pending_count(p) or self.is_bonus_state(state):
            return "freespins"
        return None

    def promo_offered(self, info: dict) -> bool:
        """`gameInfo.needToStartPromo`: the client will OFFER a free-game promo after load."""
        return bool(isinstance(info, dict) and info.get(self.F_NEED_TO_START_PROMO))

    def last_bet(self, info: dict) -> dict:
        """`gameInfo.lastBet` — the last completed round, in BET-response shape. Parseable by the
        same `Bet.parse` the live stream uses (see F_TOTAL_FREE_SPINS_ALIASES)."""
        if not self.F_LAST_BET or not isinstance(info, dict):
            return {}
        return info.get(self.F_LAST_BET) or {}

    def settings(self, info: dict) -> dict:
        if not self.F_SETTINGS or not isinstance(info, dict):
            return {}
        return info.get(self.F_SETTINGS) or {}

    def default_bet_level(self, info: dict):
        """The configured opening bet level, or None where the backend does not report one
        (Zeus: defect D3). An index into `settings.denominations`."""
        return self.settings(info).get(self.F_DEFAULT_BET_LEVEL)

    def win_formula(self, info: dict) -> list:
        """The win bands, or `[]` where the backend sends none.

        ⚠ Looked for in BOTH places. The bridge adds contract aliases next to the raw payload, so
        `settings.winFormula` is the documented home — but the server nests its settings under
        `settings.general`, and a build whose alias is missing would report no bands at all. That
        failure is silent: no bands means no threshold, which means the big-win skip quietly stops
        happening and every celebration plays out in full.
        """
        s = self.settings(info)
        v = s.get(self.F_WIN_FORMULA)
        if not isinstance(v, list):
            general = s.get("general")
            if isinstance(general, dict):
                v = general.get(self.F_WIN_FORMULA)
        return v if isinstance(v, list) else []

    def big_win_threshold(self, info: dict):
        """The payout/bet multiple at which the game starts playing a big-win celebration, or
        `None` when this build reports no bands.

        ONE number, deliberately. BIG, MEGA and ULTRA are the same interruption with different
        artwork and length, so a test that only wants to know "is a celebration about to eat ten
        seconds?" needs the lowest of their floors and nothing else.

        ⚠ Read from the LAUNCH PAYLOAD, never hard-coded: the bands are operator configuration and
        a game may ship different ones. `None` means "cannot say", and a caller must treat it as
        such rather than as "no big win" — the difference is a skip that silently stops happening.
        """
        floors = []
        for band in self.win_formula(info):
            if not isinstance(band, dict):
                continue
            if str(band.get("name") or "").strip().upper() in self.CELEBRATED_TIERS:
                try:
                    floors.append(float(band.get("min")))
                except (TypeError, ValueError):
                    continue
        return min(floors) if floors else None

    def is_big_win(self, raw: dict, threshold) -> bool:
        """Would THIS round raise a big-win celebration?

        `payout / baseStake >= threshold`. ⚠ `baseStake`, not `stake`: a free spin costs 0, and
        dividing by it would either raise or call every winning free spin a big win — and free
        spins are exactly where these celebrations cost the most time.
        """
        if threshold is None:
            return False
        base = self.base_stake(raw)
        if not base:
            return False
        try:
            payout = float(raw.get(self.F_PAYOUT) or 0)
        except (TypeError, ValueError):
            return False
        # ⚠ A HAIR OF TOLERANCE. A payout exactly ON the boundary is a big win, and `0.6 / 0.1`
        # is 5.999999999999999 in binary — so a bare `>=` calls the 6x round a medium win, the
        # celebration plays, and the skip that was supposed to save the time never fires.
        return payout > 0 and (payout / base) >= float(threshold) - 1e-9

    # ── Max-win cap ──────────────────────────────────────────────────────────────────────
    # Server API doc, "Max-win cap" (every engine):
    #     maxWin = min(MaxPayout, baseStake x MaxMultiplier)       a 0 in either = that half unset
    # counted over the WHOLE round (`accumulatedWinAmount`, which INCLUDES the current spin), so
    # the spin that crosses it pays only what is left. `rawPayout` is the win before the cap,
    # `payout` after it. Proved on the captures: Mystic's capped lastBet has accumulated 10.00 ==
    # maxPayout, payout 2.00, rawPayout 11.20 -> 8.00 already won, 2.00 left, 2.00 credited.
    # The reply's own `maxWin` is 0 on WildExpanding and Cascading (Joker, Mystic) and on Grandma's
    # purchase round, so the cap falls back to the launch settings, kept by `remember_launch`.
    F_RAW_PAYOUT = "rawPayout"
    F_ACCUMULATED = "accumulatedWinAmount"
    F_MAX_WIN_CAP = "maxWin"
    F_MAX_PAYOUT = "maxPayout"          # gameInfo.settings (Ways: settings.general)
    F_MAX_MULTIPLIER = "maxMultiplier"
    launch_caps: dict | None = None     # {max_payout, max_multiplier} from the latest gameInfo

    def remember_launch(self, info: dict) -> None:
        """Keep the max-win settings of the launch payload just read, so a round can be measured
        against the cap the server applied. A payload with no settings block (a bridge that could
        not answer) leaves the previous copy alone rather than wiping it.

        Called on EVERY load: a run that pushes a `max_win_x` paytable changes `maxPayout`, and the
        next load is where that change becomes visible. Each `Bet` freezes the cap at parse time,
        so a round recorded before a reload keeps the cap it was played under."""
        s = self.settings(info)
        if not s:
            return
        general = s.get("general") if isinstance(s.get("general"), dict) else {}

        def num(key):
            v = s.get(key)
            if v is None:
                v = general.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None

        self.launch_caps = {"max_payout": num(self.F_MAX_PAYOUT),
                            "max_multiplier": num(self.F_MAX_MULTIPLIER)}

    def max_win_cap(self, raw: dict):
        """The round's max-win cap in currency, or `None` when neither the reply nor the launch
        settings set one. The reply's own `maxWin` wins when it is filled (> 0)."""
        try:
            v = float(raw.get(self.F_MAX_WIN_CAP) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
        caps = self.launch_caps or {}
        candidates = []
        if caps.get("max_payout"):
            candidates.append(caps["max_payout"])
        base = self.base_stake(raw)
        if caps.get("max_multiplier") and base > 0:
            candidates.append(round(base * caps["max_multiplier"], 10))
        return min(candidates) if candidates else None

    def round_won_before(self, raw: dict) -> float:
        """What the round had already won BEFORE this spin: `accumulatedWinAmount - payout`.
        `0` where the reply carries no running total, which is right for a single-spin round."""
        try:
            acc = raw.get(self.F_ACCUMULATED)
            if acc is None:
                return 0.0
            return max(float(acc) - float(raw.get(self.F_PAYOUT) or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def max_win_left(self, raw: dict, cap=None):
        """What THIS spin may still credit under the cap (`cap - round_won_before`, never below 0),
        or `None` when there is no cap. `cap` defaults to `max_win_cap(raw)`; pass the one a `Bet`
        froze at parse time."""
        if cap is None:
            cap = self.max_win_cap(raw)
        if cap is None:
            return None
        return max(round(cap - self.round_won_before(raw), 10), 0.0)

    def game_state(self, raw: dict):
        """The state this backend reports. A build without an explicit field DERIVES one, so the
        engine's only question — base or bonus? — always has an answer."""
        return raw.get(self.F_GAME_STATE)

    def steps(self, raw: dict) -> list:
        """The per-step array a round is made of. Cascade rounds have many; a single-grid round has
        one, so `Bet.cascades` stays meaningful across mechanisms."""
        v = raw.get(self.F_STEPS)
        if v is None:
            for key in self.F_STEPS_ALIASES:         # profile's own spelling wins; aliases fill in
                if raw.get(key) is not None:
                    v = raw.get(key)
                    break
        if isinstance(v, list):
            return v
        return [] if v is None else [v]

    def is_trigger(self, raw: dict) -> bool:
        """Did THIS round award the bonus? From the response, never from client state."""
        _, awarded = self.free_spins(raw)
        return awarded > 0 or self.is_bonus_state(self.game_state(raw))

    # ── Paid spin features (Ante Bet, Premium Reel) ──────────────────────────────────────
    # Mechanism-free: both are a server contract, not a reel mechanism, which is what lets
    # `ante-toggle` and `ante_stake_multiplier` run on a Hold & Spin game as well as on Chaos.
    def base_stake(self, raw: dict) -> float:
        """The payout base for the round. Under Ante Bet or a Premium Reel this is NOT what was
        deducted: the player pays `stake`, and every payout still derives from this."""
        v = raw.get(self.F_BASE_STAKE)
        if v is None:
            v = raw.get(self.F_STAKE)
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def ante_multiplier(self, raw: dict):
        """The ante multiplier THIS round was bet at, or `None` for a round bet without it (the server
        sends `0`), so a stream with no ante round never asserts against a configured default."""
        try:
            f = float(raw.get(self.F_ANTE_MULTIPLIER))
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def is_ante_round(self, raw: dict) -> bool:
        """Bet with Ante on: a non-zero ante multiplier, or, for a build that omits the field, a stake
        above the payout base. That fallback misreads a Premium Reel spin, which inflates the stake
        too, so a round that echoes a package is never taken for ante."""
        if self.ante_multiplier(raw) is not None:
            return True
        if self.premium_reel_id(raw):
            return False
        try:
            return float(raw.get(self.F_STAKE) or 0) > self.base_stake(raw) + 1e-9
        except (TypeError, ValueError):
            return False

    def premium_reel_id(self, raw: dict) -> int:
        """The Premium Reel package this round was bet with, `0` for a plain round. Opaque: `23` is a
        package id, not "reels 2 and 3" (client-integration §1)."""
        try:
            return int(raw.get(self.F_PREMIUM_REEL_ID) or 0)
        except (TypeError, ValueError):
            return 0

    def premium_reel_multiplier(self, raw: dict) -> float:
        """The package's stake multiplier as this round echoed it, `0.0` for a plain round."""
        try:
            return float(raw.get(self.F_PREMIUM_REEL_MULTIPLIER) or 0)
        except (TypeError, ValueError):
            return 0.0

    # Mechanism hooks — meaningful only on the subclass that owns them.
    def cluster_wins(self, steps: list, payout: float = 0.0) -> int:
        return 0

    def win_clusters(self, steps: list) -> int:
        return 0

    # ── Bridge contract ──────────────────────────────────────────────────────────────────
    # `getGameInfo` joined this list with the resume contract (docs/game-bridge-contract.md): without
    # it the framework cannot see `pending`, so a session that owes free spins replays them into the
    # first scenario's stream with nothing to warn anyone. A missing REQUIRED function aborts the
    # suite by name, which is the only honest outcome — silent corruption is not.
    REQUIRED_API: tuple = (
        "startGame", "waitForSpinClickable", "getButtonRect", "waitForButtonClickable",
        "awaitBetResponse", "awaitFreespinRound", "getSnapshot", "getGameInfo",
    )
    # Strongly recommended, not required: `canDo(action)` is the bridge's own "would this click do
    # anything?" answer and is what the idle assertion prefers. Absent, the engine falls back to
    # getButtonRect('spin').interactable, which is one layer lower and cannot explain a refusal.
    RECOMMENDED_API: tuple = ("canDo",)
    OPTIONAL_API: dict = {
        "dismissPopups": "clean teardown between runs; popup cases",
        "getHistoryRows": "History rows + the row to click",
        "getHistoryDetailState": "free-spin sub-ticket paging",
        "getSpeedMode": "hard check that Turbo really toggled",
        "getTutorialRect": "where to wheel a cross-origin tutorial WebView",
        "startEventCapture/endEventCapture": "popup + animation-window diagnostics",
        "getGameInfo/getDenominations": "denominations, currency, default bet level, pending",
        "setForcedResult/setDevModeResult": "one deterministic spin",
    }

    def line_bet(self, base_stake: float) -> float:
        """Per-line bet — the figure a paytable actually prints against each symbol row.

        Derived from the BASE stake, not the deducted amount: an ante bet multiplies what the player
        pays without moving the payout base (Rule 9). `LINE_DIVISOR` wins where a game states one;
        otherwise the payline count is the divisor, which is what a game's design doc means when it
        writes `total bet / <paylines> x symbol multiplier`. Returns 0.0 when neither is known,
        rather than dividing by nothing.
        """
        div = self.LINE_DIVISOR or self.PAYLINES
        return base_stake / div if div else 0.0

    def api_check_js(self) -> str:
        req = ", ".join(f"'{f}'" for f in self.REQUIRED_API)
        return ("() => { const b = window.__gameTestBridge;"
                " if (!b) return ['__gameTestBridge'];"
                f" return [{req}].filter(f => typeof b[f] !== 'function'); }}")


# ── Dialect translation tables (built once from SlotGame's DEFAULT names) ────────────────
# `SlotGame.resolve()` uses these to turn a literal written in the reference build's dialect
# back into a ROLE, then out again through whichever profile is active. Built here rather than
# inside the class because they describe the DEFAULTS, not any one profile.
_DEFAULT_BUTTON_ROLE: dict = {
    v: k for k, v in SlotGame.BUTTONS.items() if v and "{" not in v
}
# Indexed controls (`auto:count:{i}`, `bet:row:{i}`) need a pattern rather than a literal match.
_DEFAULT_BUTTON_PATTERNS: dict = {
    re.compile("^" + re.escape(v).replace(r"\{i\}", r"(\d+)") + "$"): (k, v)
    for k, v in SlotGame.BUTTONS.items() if v and "{i}" in v
}
_DEFAULT_POPUP_ROLE: dict = {v: k for k, v in SlotGame.POPUPS.items() if v}


# =====================================================================================
# Mechanism: cluster-pays with cascading steps
# =====================================================================================
class CascadeGame(SlotGame):
    """Wins clear, symbols fall in, the round continues until a step pays nothing."""

    mechanism = "cascade"

    F_STEP_PAYOUT = "payout"
    F_STEP_WINNINGS = "winnings"
    F_STEP_GRID = "views"
    F_STEP_MULTIPLIER = "multiplierLevel"

    def cluster_wins(self, steps: list, payout: float = 0.0) -> int:
        """How many cascade steps PAID — the unit the history detail pages by (one grid per step, so
        a step paying several clusters still counts once) and the unit the multiplier ladder climbs.
        The chain ends with a non-winning step, so a winning round has `len(steps) - 1`."""
        typed = [s for s in (steps or []) if isinstance(s, dict)]
        if not typed:
            return 0
        if any(self.F_STEP_PAYOUT in s for s in typed):
            n = 0
            for s in typed:
                try:
                    if float(s.get(self.F_STEP_PAYOUT) or 0) > 0:
                        n += 1
                except (TypeError, ValueError):
                    pass
            return n
        if any(isinstance(s.get(self.F_STEP_WINNINGS), list) for s in typed):
            return sum(1 for s in typed if s.get(self.F_STEP_WINNINGS))
        return max(0, len(typed) - 1) if payout > 0 else 0

    def win_clusters(self, steps: list) -> int:
        """Total winning CLUSTERS (every `winnings[]` entry). Exceeds cluster_wins whenever one step
        paid several clusters at once. Diagnostic only."""
        n = 0
        for s in steps or []:
            if isinstance(s, dict) and isinstance(s.get(self.F_STEP_WINNINGS), list):
                n += len(s[self.F_STEP_WINNINGS])
        return n

    def multiplier_ladder(self, steps: list) -> list:
        """Per-step multiplier as the server reported it. ⚠ a LADDER from game config (Mystic base
        game runs 1,2,3,5,8,10) — never assume `+1` per cascade."""
        return [s.get(self.F_STEP_MULTIPLIER) for s in steps or []
                if isinstance(s, dict) and self.F_STEP_MULTIPLIER in s]


# =====================================================================================
# Mechanism: Hold & Spin (respins that hold symbols)
# =====================================================================================
class HoldSpinGame(SlotGame):
    """N special symbols trigger a respin feature: the triggering symbols are HELD, only the empty
    positions respin, a new symbol RESETS the respin counter, and the feature ends when the counter
    hits zero or the grid fills.

    The whole rule set is checkable from the response — held positions, symbol count, respins
    remaining — so this mechanism's cases are HARD `stream:` asserts, not AI reads.
    """

    mechanism = "hold-spin"

    # Feature parameters
    TRIGGER_COUNT: int = 3        # special symbols needed to trigger
    RESPINS_ON_TRIGGER: int = 3   # respins awarded, and the value the counter RESETS to
    GRID_SLOTS: int = 9           # a full grid ends the feature immediately

    # Response fields carrying the feature's state
    F_COIN_POSITIONS = "coinPositions"   # [{row, col, coinValue, type, multiplier?}]
    F_COIN_COUNT = "coinCount"
    F_COIN_VALUE = "coinValue"
    F_COIN_TYPE = "type"

    HAS_FREE_SPINS = True

    def coin_positions(self, raw: dict) -> set:
        """The held positions as {(row, col)} — the set that must only ever GROW during the feature."""
        out = set()
        for c in raw.get(self.F_COIN_POSITIONS) or []:
            if isinstance(c, dict) and c.get("row") is not None and c.get("col") is not None:
                out.add((int(c["row"]), int(c["col"])))
        return out

    def coin_count(self, raw: dict) -> int:
        """How many special symbols are on the grid. Prefers the server's own count and falls back to
        the position list, so a build that reports only one of the two still works."""
        n = raw.get(self.F_COIN_COUNT)
        if isinstance(n, (int, float)):
            return int(n)
        return len(self.coin_positions(raw))

    def coin_total(self, raw: dict) -> float:
        """Summed face value of the held symbols — the feature's minimum payout so far."""
        total = 0.0
        for c in raw.get(self.F_COIN_POSITIONS) or []:
            if isinstance(c, dict):
                try:
                    total += float(c.get(self.F_COIN_VALUE) or 0)
                except (TypeError, ValueError):
                    pass
        return round(total, 6)

    def grid_full(self, raw: dict) -> bool:
        return self.coin_count(raw) >= self.GRID_SLOTS

    def is_trigger(self, raw: dict) -> bool:
        """Did THIS round award the feature?

        State and counters first; the symbol count is only a FALLBACK for a build that reports
        neither. That order matters: the round which ENDS the feature carries a full grid of
        specials (9 coins) while already reporting base state and no respins — counting symbols
        alone would call that a trigger and send a scenario off to drain a bonus that just finished.
        """
        rem, awarded = self.free_spins(raw)
        if awarded > 0 or rem > 0:
            return True
        if self.is_bonus_state(self.game_state(raw)):
            return True
        if self.F_GAME_STATE in raw or (self.F_FS_CONTAINER and self.F_FS_CONTAINER in raw):
            return False                # the build DID report; it said no
        return self.coin_count(raw) >= self.TRIGGER_COUNT


# =====================================================================================
# Mechanism: line / ways evaluation on a fixed grid
# =====================================================================================
class LineGame(SlotGame):
    """One grid per round, paid along fixed LINES (left-to-right from reel 1) — plus, in some games,
    a WAYS symbol that pays from any reel.

    Nothing cascades and nothing is held, so the round IS the grid. That makes the readable unit a
    per-reel symbol census: how many of symbol X landed on reel R. Almost every rule a line game
    states — "this symbol only appears on reels 1, 3, 5", "2+ scatters glow the rest", "3 scatters
    award N free spins" — is a predicate over that census, so it is checkable EXACTLY from the
    response instead of being read off the screen.

    `grid()` returns `None` (never an empty grid) when the response does not carry a readable one.
    That distinction matters: an empty census would make every "symbol X is absent" rule pass
    vacuously, so callers must treat `None` as *cannot verify* and report the case unreached.
    """

    mechanism = "line"

    # ── Geometry ─────────────────────────────────────────────────────────────────────────
    REELS: int = 5
    ROWS: int = 3
    # PAYLINES / LINE_DIVISOR are declared on SlotGame (a paytable is printed per line on every
    # mechanism); a line game merely defaults the divisor to the usual 20.
    LINE_DIVISOR: int | None = 20

    # ── Response fields carrying the grid ────────────────────────────────────────────────
    F_STEP_GRID = "views"              # [{id, value: [symbol, ...]}] — one entry per REEL
    F_STEP_WINNINGS = "winnings"
    F_MULTIPLIER = "multiplier"        # the round's random multiplier

    # ── Symbol names (string ids, as the cascade reference uses) ─────────────────────────
    SYM_WILD: str = "Wild"
    SYM_SCATTER: str = "FreeSpin"
    MULTIPLIER_VALUES: tuple = ()      # the configured ladder, if the game has one
    WIN_TIERS: tuple = ()              # (name, lower bound as a multiple of bet), ascending

    # ── Grid reading ─────────────────────────────────────────────────────────────────────
    @staticmethod
    def _norm(sym) -> str:
        """Symbols compare loosely, and across TYPES.

        A grid cell may be a numeric id (Chaos sends `11`) or a name (the cascade reference sends
        `FreeSpin!`), and a profile names its symbols in whichever form that build uses. Both sides
        are folded to the same string, so `11` matches `11` and `FreeSpin!` matches `FreeSpin`."""
        return str(sym).strip().rstrip("!").lower()

    def grid(self, raw: dict):
        """The round's grid as a list of REELS, each a list of symbol names top->bottom.

        `None` when the response carries no readable grid — an UNVERIFIED field name, a build that
        does not send one, or a shape this profile has not been taught. Never `[]`.
        """
        for step in self.steps(raw) or []:
            if not isinstance(step, dict):
                continue
            views = step.get(self.F_STEP_GRID)
            if not isinstance(views, list) or not views:
                continue
            reels = []
            for col in views:
                if isinstance(col, dict) and isinstance(col.get("value"), list):
                    reels.append([str(s) for s in col["value"]])
                elif isinstance(col, list):
                    reels.append([str(s) for s in col])
            if reels:
                return reels
        return None

    def reel_counts(self, raw: dict, symbol):
        """How many `symbol` landed on each reel, as a list indexed by reel (0 = reel 1).

        `None` when the grid is unreadable (see `grid()`) OR when `symbol` is `None` — a profile
        uses `None` for a symbol whose id this game has not revealed yet. Counting an unknown
        symbol would return all-zeros, and "it never appeared on an illegal reel" would then pass
        for a symbol nobody can identify."""
        if symbol is None:
            return None
        reels = self.grid(raw)
        if reels is None:
            return None
        want = self._norm(symbol)
        return [sum(1 for s in reel if self._norm(s) == want) for reel in reels]

    def symbol_count(self, raw: dict, symbol: str):
        """Total `symbol` on the grid, or `None` when the grid is unreadable."""
        counts = self.reel_counts(raw, symbol)
        return None if counts is None else sum(counts)

    def reels_with(self, raw: dict, symbol: str):
        """1-BASED reel numbers carrying `symbol` (so it reads like the design docs, which say
        'reels 1, 3, 5'). `None` when the grid is unreadable."""
        counts = self.reel_counts(raw, symbol)
        return None if counts is None else [i + 1 for i, n in enumerate(counts) if n > 0]

    def ways(self, raw: dict, symbol: str, reels: tuple):
        """The WAYS count for a symbol that pays across specific reels: the product of its per-reel
        counts. Zero when any required reel is missing it. `None` when the grid is unreadable."""
        counts = self.reel_counts(raw, symbol)
        if counts is None:
            return None
        total = 1
        for r in reels:
            if r - 1 >= len(counts):
                return 0
            total *= counts[r - 1]
        return total

    def multiplier(self, raw: dict):
        """The round's random multiplier, or `None` when the response does not report one."""
        v = raw.get(self.F_MULTIPLIER)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None


    def win_tier(self, payout: float, stake: float) -> str:
        """Win tier by payout/stake against this game's configured bands."""
        if not self.WIN_TIERS or stake <= 0 or payout <= 0:
            return "LOSE"
        x, tier = payout / stake, self.WIN_TIERS[0][0]
        for name, lo in self.WIN_TIERS:
            if x >= lo:
                tier = name
        return tier


class WaysGame(LineGame):
    """One grid per round, paid by WAYS: every left-to-right run of a symbol on adjacent reels
    starting at reel 1 pays, once per combination of the cells involved.

    It subclasses LineGame for one reason — the readable unit is the same PER-REEL SYMBOL CENSUS
    (`reel_counts`, `reels_with`, `ways`), and a ways game is the mechanism that census was really
    describing: "how many of symbol X on reel R" IS the ways count once multiplied across reels.
    What differs is the arithmetic on top, so the line-specific defaults are cleared here rather
    than inherited by accident:

    * there are no paylines and no per-line divisor. A ways payout divides by nothing — it is
      `symbol pay x ways x BASE STAKE` — so `PAYLINES` / `LINE_DIVISOR` stay None and any scenario
      that needs them reports unreached instead of dividing by a number this mechanism has no
      meaning for;
    * the round carries ONE grid at the top level rather than an array of steps, so `steps()`
      returns the round itself and `Bet.cascades` stays 1.

    `WAYS_TOTAL` is the headline number the game advertises (1024 = rows ** reels for a full mask)
    and is reported, never assumed: a game with a ragged reel mask has fewer.
    """

    mechanism = "ways"

    PAYLINES: int | None = None
    LINE_DIVISOR: int | None = None

    ROWS: int = 4
    WAYS_TOTAL: int | None = None

    def steps(self, raw: dict) -> list:
        """The round IS the step. A ways response has no step array — the grid sits at the top
        level — so the round is wrapped to keep `Bet.cascades == 1` and `grid()` unchanged."""
        return [raw] if isinstance(raw, dict) else []

    def ways_total(self) -> int | None:
        """The advertised ways count, from the geometry this profile declares."""
        if self.WAYS_TOTAL is not None:
            return self.WAYS_TOTAL
        if self.REELS and self.ROWS:
            return self.ROWS ** self.REELS
        return None


# =====================================================================================
# Games
# =====================================================================================
class MysticWishes(CascadeGame):
    """Mystic Wishes (sl_77_mystic_wishes) — the cascade reference. Response shape and every
    button/popup name are the defaults; see games/mystic/bet-response.md."""

    name = "mystic"
    HAS_ANTE_BET = False        # no anteBet button in the build
    HAS_BUY_BONUS = False       # button exists but does nothing
    HAS_LAST_BET_PANEL = False  # no `lastBet` button


class Zeus(HoldSpinGame):
    """Zeus / Zappin (SL_ZEUS) — the Hold & Spin reference. Bridge: games/zeus/bridge-api.md (v3).

    3x3 grid, 5 paylines, Coin (symbol 13) is the special. 3+ Coins trigger Hold & Spin with 3
    respins; every new Coin resets the counter to 3; the feature ends at 0 respins or a full grid of
    9, and 9 Coins in the base game skip the respins entirely.

    The bridge deliberately exposes NO round outcomes — it dropped `verifyHoldSpin*` in v2 — so the
    hold-spin rules live in the engine's invariants, computed from the responses.
    """

    name = "zeus"

    # Confirmed against captured payloads (games/zeus/bet-response.md). Only ONE field differs from
    # the defaults: the free-spin counters are NESTED, and the remaining count is spelled with a
    # lowercase 's'. `gameState`, `baseStake` and `totalFreeSpin` are all present and standard.
    F_FS_CONTAINER = "freeSpinInfo"     # ...and it is NULL on the round that ends the feature
    F_FREE_SPINS = "freespins"          # lowercase 's' -- not Mystic's freeSpins
    # ...but `gameInfo.pending` is FLAT and camelCase: `{refId, roundId, coinPositions, freeSpins,
    # stake}` (bridge-api.md, and Dragon's / Dragon VIP's). Reading it as `freespins` reported every
    # owed Hold & Spin as "0 free spin(s) owed" -- measured on Dragon VIP 2026-09-17, where only
    # `gameState == 2` kept the resume from being missed.
    F_PENDING_FREE_SPINS = "freeSpins"
    F_STEPS = "result"                  # {views: [...]} -- ONE grid, not a step array

    GRID_SLOTS = 9
    TRIGGER_COUNT = 3
    RESPINS_ON_TRIGGER = 3

    SPECIAL_SYMBOL = 13                 # COIN
    WILD_SYMBOL = 9
    PAYLINES = 5
    WIN_TIERS = (("LOSE", 0), ("SLIM_WIN", 0), ("SMALL_WIN", 1), ("MEDIUM_WIN", 2.5),
                 ("BIG_WIN", 10), ("MEGA_WIN", 25), ("ULTRA_WIN", 50))

    # Names checked against games/zeus/bridge-api.md (v3) §9, the complete button registry.
    BUTTONS = dict(SlotGame.BUTTONS, **{
        "stop": "stop",
        "auto_or_freespin": "autoOrFreeSpin",   # replaces spin during auto / free spins
        "auto_stop_main": "stopAuto",
        "ante": None,                   # no Ante Bet
        "buy_bonus": "buyBonus",
        # BOTH open the bet-amount popup -- they are not steppers. The stepping lives inside it as
        # bet:plus / bet:minus, and a specific amount is picked with bet:row:<name>.
        "stake_up": "nextBet",
        "stake_down": "previousBet",
        "trophy": "trophy",
        "auto_count": "auto:package:{i}",       # 0..: the packages, in order
        "auto_confirm": "auto:confirm",
        "auto_stop": "auto:stop",
        "auto_stop_at_win": "auto:stopAtWin",
        "auto_close": "auto:cancel",
        # ⚠ bet:row:<i> resolves the child by NODE NAME equal to that string, not by index
        # position. If the rows are not named "0","1",... this needs the real names.
        "bet_row": "bet:row:{i}",
        "bet_plus": "bet:plus",
        "bet_minus": "bet:minus",
        "bet_close": "bet:close",
        "settings_close": "settings:close",
        "settings_sound": "settings:sound",
        "settings_music": "settings:music",
        "settings_turbo": "settings:turbo",
        "info_close": "info:close",
        "history_close": "history:close",
        "message_ok": "notification:yes",
        # bridge-api.md (v3) §9: the `historyDetail:` prefix publishes next / prev / close. v2
        # had no detail nav at all, and leaving these None made `spin-then-history` unable to
        # page a ticket on the one build that can.
        "detail_next": "historyDetail:next",
        "detail_prev": "historyDetail:prev",
        "detail_close": "historyDetail:close",
        "history_row": "history:row:{i}",   # v3 addresses rows by name as well as by rect
        "buy_package": "buyBonus:package:{i}",   # 0..2 = casual / bold / epic
        # BUYING IS TWO CLICKS on this build, established from a live run 2026-09-10:
        #   `buyBonus:buy`      the panel's Buy button -- RAISES A CONFIRMATION MODAL
        #   `buyBonus:confirm`  the button inside that modal -- THE ACTUAL PURCHASE
        # v3 §9 lists both under the prefix without distinguishing them, and canDo's vocabulary
        # (`openBuyBonusPopup` / `selectBuyBonusPackage` / `confirmBuyBonus`) names the ACTION, not
        # the control -- it answered ok:True while neither had been clicked. An earlier reading took
        # `confirm` for the purchase; it is scoped to a modal that does not exist until Buy is
        # pressed, so it burned its whole timeout and the purchase never fired.
        # The modal is a CHILD of the `buyBonus` popup, not a registered label of its own, so
        # `visiblePopups` shows only `buyBonus` while it is up.
        "buy_confirm": "buyBonus:confirm",
        "buy_purchase": "buyBonus:buy",
        "buy_cancel": "buyBonus:cancel",
        "buy_close": "buyBonus:close",
    })

    POPUPS = dict(SlotGame.POPUPS, **{
        "history": "history",
        # ⚠ WAS None, AND THAT WAS THE 70-SECOND HANG. v3 §10 registers `historyDetail`, and
        # `spin-then-history` used to decide "did the ticket open?" by looking for this label in
        # visiblePopups -- so with None the answer was False forever, the ticket was on screen,
        # the paging was skipped, and the wait behind it ran to a spin-sized timeout. The
        # scenario no longer depends on the label alone (it asks getHistoryDetailState().open
        # first), but the label is real and belongs here.
        "history_detail": "historyDetail",
        "information": "info",
        "autoplay": "auto",
        "denominations": "bet",
        "settings": "settings",
        "message": "notification",
        "big_win": "bigWin",
        "total_win": "totalWin",
        "buy_bonus": "buyBonus",
        "freespin_result": "totalWin",
        "promo": "promo",
        "disconnect": "disconnect",
    })

    UI_SURFACES = (
        ("base screen", None, None),
        ("bet list", "stake_up", "bet_close"),
        ("settings", "settings", None),
        ("tutorial", "info", None),
        ("auto play panel", "auto", "auto_close"),
        ("history", "history", None),
    )
    FEATURE_TOGGLES = ("turbo", "sound")

    HAS_ANTE_BET = False
    HAS_BUY_BONUS = True
    HAS_LAST_BET_PANEL = False
    HAS_HISTORY_DETAIL_NAV = True       # v3 §9: historyDetail:next / prev; §6 getHistoryDetailState()
    HAS_SOUND_GETTERS = True            # isSoundEnabled()/isMusicEnabled() -> ZZ-24/25
    HAS_DEV_MODE = True                 # setDevModeResult(grid)
    # v3 §10 lists `info` as "help / paytable (WebView)" and §6 documents getTutorialRect() for it,
    # including the cross-origin caveat: only a real page.mouse.wheel scrolls it and the scroll
    # position cannot be read back. False sent `open-tutorial` down its scroll_popup fallback,
    # which cannot move a cross-origin frame -- the guide would not have scrolled at all.
    HAS_TUTORIAL_WEBVIEW = True
    # bridge-api.md v3 §9 (and v2 §10 before it): nextBet / previousBet BOTH open the bet-amount popup. CONFIRMED
    # 2026-08-27 -- the popup opens with all 20 bet:row:<i> plus bet:plus / bet:minus / bet:close.
    # It then CLOSES ITSELF after ~0.4s, which is defect D1 (see docs/zeus-bridge-defects.md): every
    # popup does, because startGame()'s deterministic-start loop never finishes and keeps dismissing
    # them. Not a bet-popup problem. The scenarios still probe at runtime (open_bet_list) rather than
    # trusting this flag, so they report honestly whichever way a given build behaves.
    HAS_BET_POPUP = True

    def steps(self, raw: dict) -> list:
        """One grid per round: `result.views`. Wrapped as a single step so `Bet.cascades == 1`."""
        r = raw.get(self.F_STEPS)
        return [r] if isinstance(r, dict) else ([] if r is None else list(r))

    def win_tier(self, payout: float, stake: float) -> str:
        """Win tier by payout/stake. ⚠ assert against these (Interface.ts WIN_RANGE); Constants.ts
        exports a second, DEAD table with different thresholds."""
        if stake <= 0 or payout <= 0:
            return "LOSE"
        x, tier = payout / stake, "SLIM_WIN"
        for name, lo in self.WIN_TIERS:
            if lo and x >= lo:
                tier = name
        return tier


class Dragon(Zeus):
    """Treasures of the Dragon (`dragon`) — a fork of Zeus. Bridge: games/dragon/bridge-qa-api.md (v3).

    The same game underneath: the design doc and the server paytable in games/dragon/docs/raw/ are
    byte-identical to Zeus's, and the bridge's §11 describes the same BET response (nested
    `freeSpinInfo.freespins`, `coinPositions` / `coinCount`, `gameState`, `baseStake`). So response
    reading, the Hold & Spin parameters and every capability flag are INHERITED; this class restates
    only the dialect the bridge doc's §13 ("Differences from Zeus v3") says is different.

    §13 differences that reach the framework without a line of code here, and so are easy to miss:
      * a hidden or gated control is `null` (Zeus: `{visible: false}`) — `buyBonus` during Hold &
        Spin, `auto` during a run, `stopAuto` with no run — so `has_button` reads it as absent;
      * `stopAuto` is the ONLY control that ends a run (`autoOrFreeSpin` only fast-stops the reels).
        `auto_stop_button()` prefers `auto_stop_main`, which is `stopAuto`, so it is already right;
      * the Turbo / Auto toasts ARE reported in `visiblePopups` (`turboNotify` / `autoNotify`, ~2.4s);
      * `buyBonusSelectedPrice` is the TOTAL price (selected stake x package price multiplier), and
        `buyBonus:package:{i}` picks a STAKE, not a price tier;
      * unlimited autoplay reports `autoSpinsRemaining` as `Infinity`.
    """

    name = "dragon"

    BUTTONS = dict(Zeus.BUTTONS, **{
        # bridge §9: the CANONICAL autoplay names. Zeus's `auto:package:{i}` / `auto:cancel` still
        # resolve here as aliases, but getAllButtonRects() lists canonical names only, so the
        # canonical form is the one a probe can confirm.
        "auto_count": "auto:count:{i}",
        "auto_close": "auto:close",         # a strip of the tap-outside backdrop -- no close X
    })

    # bridge §9: the panel offers [10, 20, 30, 50, ∞] and OPENS on 10. Stated, not inherited, so the
    # value traces to the doc -- Zeus's copy is still the unconfirmed shared default.
    AUTO_COUNTS = (10, 20, 30, 50, None)

    # ⚠ `settings.defaultBetLevel`: the contract calls it a 0-based index and `boot-base` reads it
    # that way, but this client subtracts one everywhere it reads it (bridge §3), so a FIRST session
    # opens at `denominations[defaultBetLevel - 1]`. Deliberately NOT adapted here: if the server
    # means 0-based the client is wrong, and DR-04 should say so. Returning sessions open at
    # `lastBet.baseStake` and never take that branch.


class DragonVip(Dragon):
    """Treasures of the Dragon VIP (`dragon-vip`) — Dragon plus Ante Bet and Premium Reel.
    Bridge: games/dragon-vip/game-bridge-api.md (`dragon-vip-bridge-v3/2026-09-14`); the Premium
    Reel server contract: games/dragon-vip/docs/raw/client-integration.vi.md.

    The base game, the Hold & Spin and the BET response are Dragon's, so response reading and the
    Hold & Spin parameters are inherited. The server paytable is Dragon's plus the `AnteBet*` and
    `PremiumReel_*` sections, with DIFFERENT base-game weights — which is why games/dragon-vip/
    paytables/ is derived from this game's own paytable rather than copied from Dragon.

    What differs, and where it reaches the framework:
      * NO one-click ante. Ante Bet and every Premium Reel package sit behind `buyBonus` (open the
        panel -> ACTIVATE row -> confirm sheet), and the same main button switches the active one
        OFF (`anteBet:off` / `premiumReels:off`, bridge §6.3). `switch_feature` drives both flows;
        `ante` is None so nothing mistakes the panel for a toggle;
      * Ante Bet and a Premium Reel are mutually exclusive, and `buyBonus` is null while either is on;
      * a Buy Bonus PACKAGE ROW raises the confirm sheet itself — no `buyBonus:buy` in between;
      * the stake controls are canonically `stakeIncrease` / `stakeDecrease` (Dragon's `nextBet` /
        `previousBet` still resolve, as aliases), and there is no `autoOrFreeSpin`;
      * `anteNotify` is a third toast beside `turboNotify` / `autoNotify`, listed in `visiblePopups`.

    ⚠ No BET response has been captured from this build. `anteMultiplier` (bridge §9's recipe) and
    `premiumReelId` / `premiumReelMultiplier` (client-integration §4) are the DOCUMENTED names. Capture
    one ante round and one premium round into games/dragon-vip/fixtures/bet-responses/ before reading
    a red on them as a game defect.
    """

    name = "dragon-vip"

    BUTTONS = dict(Dragon.BUTTONS, **{
        "stake_up": "stakeIncrease",
        "stake_down": "stakeDecrease",
        "auto_or_freespin": None,                    # bridge §10: not in this build
        "buy_purchase": None,                        # the package row opens the confirm sheet (§6.3)
        "ante": None,                                # no single ante control (§10)
        "ante_on": "buyBonus:anteBet",
        "ante_off": "anteBet:off",
        "premium_reel": "buyBonus:premiumReels:{i}",  # {i} is the package's reelId, not a row index
        "premium_off": "premiumReels:off",
        "buy_next": "buyBonus:next",                 # moves the REAL stake on the click (§6.3)
        "buy_prev": "buyBonus:prev",
    })

    POPUPS = dict(Dragon.POPUPS, **{"ante_notify": "anteNotify"})

    # The feature panel carries the Ante Bet and Premium Reel rows plain Dragon does not have, so it
    # joins the text surfaces `language-tour` shoots.
    UI_SURFACES = Dragon.UI_SURFACES + (("feature panel", "buy_bonus", "buy_close"),)

    HAS_ANTE_BET = True
    HAS_PREMIUM_REEL = True


class ChaosRunner(LineGame):
    """Chaos Runner (`chaos`) — the LINE reference. 5x3, ante bet, random multiplier, free spins.

    Knowledge: games/chaos/docs/final/game-knowledge.md · paytable: docs/final/paytable.md ·
    bridge: games/chaos/docs/raw/test-bridge-api.md.

    What makes this game its own mechanism rather than a Zeus/Mystic variant:
      * **base bet vs bet amount.** Ante Bet multiplies what the player PAYS and what the UI SHOWS,
        but every payout still derives from the unchanged base bet. So `stake` and the payout base
        legitimately diverge, and an ante case asserts exactly that divergence.
      * **P1 ("Fuel") pays by WAYS** on reels 1/3/5 only, while everything else pays by lines.
      * **A random multiplier per round** (x1/x2/x5/x10), which in free spins multiplies every
        free-spin payout.

    **VERIFIED against 4 real BET payloads** (2026-08-24) — see `games/chaos/bet-response.md` and
    `games/chaos/fixtures/bet-responses/`. What those samples corrected, versus what was modelled
    from the design doc:

      * The bet object is **double-nested**: `envelope.result` is the round, and `round.result`
        holds `{rawViews, views}`. `unwrap()` peels the envelope; `steps()` takes the inner object.
      * **Symbols are NUMERIC IDS**, not names (`11` is the Bonus/FreeSpin scatter).
      * The multiplier field is **`randomMultiplier`**, not `multiplier`.
      * **Free spins are awarded by REEL count x ways multiplier**, not by total scatter count —
        the single biggest correction; see `expected_fs_award()`.
      * Paylines are **server-reported** (`betLines: 20`) and each win carries its own `payoutRate`,
        which makes the payout exactly derivable — see `expected_payout()`.
      * **Ante Bet multiplies the STAKE, never the payout base** — confirmed by a 5th payload
        (`anteMultiplier: 3`, `baseStake: 0.6`, `stake: 1.8`, paying `150 x 0.6/20 = 4.5`).

    Still unverified: **P1's symbol id** (it has never appeared in a payload — see `SYM_P1`), and
    everything about a round played INSIDE the bonus (no free-spin round has been captured). Those
    readers return `None` so the dependent case goes inconclusive rather than passing on a guess.
    """

    name = "chaos"

    # ── Geometry (QA-confirmed 5x3; betLines confirmed 20 by the samples) ────────────────
    REELS = 5
    ROWS = 3
    PAYLINES = 20              # the samples all report `betLines: 20`; read per round anyway
    LINE_DIVISOR = 20          # fallback only — `line_bet_of()` prefers the round's own betLines

    # ── Symbols — NUMERIC IDS (confirmed from paylines[].symbols vs winType) ─────────────
    # Every id below was read off a real payload: `symbols: [1,1,1,2,1]` paired with
    # `winType: "A-A-A-J-A"` pins 1=A and 2=J. Ids seen on the reels but not yet pinned to a
    # name: 3, 7, 8. NOTHING here is inferred from position in the design doc's table.
    SYMBOLS: dict = {1: "A", 2: "J", 4: "K", 5: "P1", 6: "P2", 7: "P3", 9: "WILD!", 11: "FreeSpin!"}
    SYM_SCATTER = 11                 # UI name "Bonus"
    SYM_WILD = 9                     # confirmed by winType "P2-WILD!-P2-WILD!-P3"
    # **5 = P1, CONFIRMED** by a captured P1 win, three ways over the same round:
    #   * id 5 landed on reels [1, 3, 5] and nowhere else — P1's documented restriction;
    #   * its ways count (2 x 1 x 1 = 2) times the 10x multiplier equals the round's reported
    #     `scatterMultiplier` of 20, exactly;
    #   * and `rawPayout` was 12 = 2 ways x 10 x baseStake 0.6.
    # (The earlier hypothesis "P1 = 5, because P2 = 6 and P3 = 7 run sequentially" was right.)
    SYM_P1 = 5                       # UI name "Fuel"
    P1_REELS: tuple = (1, 3, 5)      # 1-based; documented, not yet observed
    P1_MULTIPLIER: float = 10.0      # 10x bet per way (documented)

    # ── Features — all of the following now read off the BO paytable export ─────────────
    # games/chaos/docs/raw/paytable.json. Values here are the DEFAULT config; a run that forces a
    # different paytable must re-read it rather than trust these.
    MULTIPLIER_VALUES = (1, 2, 5, 10)
    # Selection weights per 1000. Free spins skew far richer than the base game — worth knowing
    # before calling an all-x1 bonus a defect.
    MULTIPLIER_WEIGHTS_BASE: dict = {1: 800, 2: 165, 5: 30, 10: 5}
    MULTIPLIER_WEIGHTS_FREE: dict = {1: 650, 2: 250, 5: 60, 10: 40}
    RAINING_WILD_MIN_WILDS = 2       # 2+ Wilds -> 100%; fewer -> 0%
    SCATTERS_TO_TRIGGER = 3          # ...distinct REELS holding a Bonus, not total symbols
    # `BaseGame.FreeSpins` = [{Occurrences: 3, FreeSpins: 12}, {4: 15}, {5: 20}]. "Occurrences"
    # means REELS: the captured trigger had 5 Bonus SYMBOLS across 3 reels and awarded 48, which is
    # the Occurrences-3 entry (12) times the ways multiplier (1*2*2 = 4). By symbols it would have
    # been the Occurrences-5 entry, 20. See expected_fs_award().
    FS_BASE_BY_REELS: dict = {3: 12, 4: 15, 5: 20}
    # `FreeSpinGame.FreeSpins` is the IDENTICAL table, so a retrigger awards on the same terms —
    # which answers what the design doc left blank.
    FS_RETRIGGER_BY_REELS: dict = {3: 12, 4: 15, 5: 20}
    # `BaseGame.AnteBet` = {Multiplier: 3, DistributionListId: 3}. Confirmed by a live ante round.
    ANTE_MULTIPLIER: float = 3.0
    ANTE_DISTRIBUTION_LIST: int = 3
    # Complete symbol paytable, indexed by MATCH COUNT 1..5 (`SymbolMultipliers[].Value`).
    # Every rate observed in a live payline matched this table exactly.
    SYMBOL_PAYS: dict = {
        "J":  [0, 0, 4, 10, 30],
        "Q":  [0, 0, 6, 20, 50],
        "K":  [0, 0, 8, 25, 100],
        "A":  [0, 0, 10, 50, 150],
        "P4": [0, 0, 15, 75, 200],
        "P3": [0, 0, 20, 100, 250],
        "P2": [0, 0, 40, 150, 300],
        "P1": [0, 0, 10, 0, 0],      # ways/scatter symbol: only the 3-of-a-kind entry pays
        "WILD!": [0, 0, 0, 0, 0],    # substitutes; never pays on its own
    }
    # `BaseGame.Scatters` = [{Symbol: "P1", Occurrences: 3, Multiplier: 10}]. So P1 is configured as
    # a SCATTER — it pays by occurrence, anywhere, not along a payline. Confirmed structurally: no
    # entry in the 7148-row payline table starts with P1 (nor with FreeSpin!).
    P1_SCATTER_OCCURRENCES: int = 3
    # Reels P1 can occupy, taken from the reel strips: its weight is literally 0 on reels 2 and 4
    # in every distribution list.
    P1_REELS_FROM_CONFIG: tuple = (1, 3, 5)
    # Wild's weight is 0 on REEL 1 in every list, base game and free game alike — an absolute rule
    # neither design doc mentions.
    WILD_FORBIDDEN_REELS: tuple = (1,)
    WIN_TIERS = (("SLIM_WIN", 0), ("SMALL_WIN", 1), ("MEDIUM_WIN", 2.5),
                 ("BIG_WIN", 6), ("MEGA_WIN", 15), ("ULTRA_WIN", 40))

    # ── Server contract (VERIFIED against games/chaos/fixtures/bet-responses/) ───────────
    F_STEPS = "result"               # the INNER {rawViews, views} object — one grid per round
    F_MULTIPLIER = "randomMultiplier"
    F_SCATTER_MULTIPLIER = "scatterMultiplier"
    F_ANTE_MULTIPLIER = "anteMultiplier"
    F_PAYLINES = "paylines"          # [{symbols, payline, payoutRate, winType}]
    F_BET_LINES = "betLines"         # 20 in every sample
    F_WINLOSE = "winlose"            # payout - stake
    # rawPayout / accumulatedWinAmount / maxWin: inherited from SlotGame (every engine sends them)

    # ── Bridge dialect ───────────────────────────────────────────────────────────────────
    # VERIFIED LIVE 2026-09-02 (scripts/probe_bridge.py --game chaos --env dev, plus a probe that
    # opened the denomination list): the bridge keeps an ALIAS TABLE. Its native names are the ones
    # in games/chaos/docs/raw/test-bridge-api.md (`betPlus`, `denomination:close`, `monumentalWin`,
    # `soundOn`), and those are what `getAllButtonRects()` enumerates — but `getButtonRect()` ALSO
    # resolves the reference spellings (`stakeIncrease`, `stakeDecrease`, `trophy`, and, while the
    # list is open, `bet:close` / `bet:plus` / `bet:minus` / `bet:row:{i}`). Both dialects work, so
    # the reference names below are correct and must NOT be reverted.
    #
    # ⚠ `getAllButtonRects()` is an inventory of NATIVE names, not of resolvable ones. A reference
    # name being absent from it does not mean the bridge cannot resolve it — reading it that way is
    # what made docs/session-summary-scenario-qa-5.md §6 suspect these names in the first place.
    # (The ~70s stall that prompted that suspicion was a POPUP-LABEL typo; see POPUPS below.)
    BUTTONS = dict(SlotGame.BUTTONS, **{
        # This build ships NO Buy Bonus (HAS_BUY_BONUS below, and the game doc states it outright).
        # `None` makes the framework skip the control instantly instead of probing one that can
        # never resolve — the contract's "does not exist in this build" path.
        "buy_bonus": None,
        # Declared in the contract's Bet table but `None` in the reference defaults, because no
        # Mystic case drives them. Chaos has the in-list steppers, so name them.
        "bet_plus": "bet:plus",
        "bet_minus": "bet:minus",
        # The Monumental Wins panel fills the contract's `trophy` role. Its native name IS
        # `monumentalWin`, but the alias table resolves `trophy` too — confirmed live 2026-09-02.
        "trophy": "trophy",
    })
    AUTO_COUNTS = (10, 20, 30, 50, None)

    # Popup LABELS are not standardised by docs/game-bridge-contract.md (§4 only requires
    # `visiblePopups` to be a front-most-first string[]), and — unlike BUTTON names — they are NOT
    # aliased by the bridge: `visiblePopups` reports this build's raw strings, so the profile has to
    # match them exactly. CONFIRMED against `getSnapshot().visiblePopups` on the live dev build
    # (2026-09-02) by opening each surface; the reference spellings were wrong for two of them.
    #
    # `denominations` -> `denomination` cost a FALSE GREEN, not just a miss: `close_bet_list`'s
    # "is it still open?" guard compared the reference label against ["denomination"], concluded the
    # list was already gone, and returned closed/close_button_worked/needed_dismiss = True WITHOUT
    # EVER CLICKING the close control. CR_004's three asserts all passed with the popup still on
    # screen, and the next settle() then burned its whole 60s because a visible popup blocks
    # waitForSpinClickable. That is the ~70s stall recorded in docs/session-summary-scenario-qa-5.md
    # §6 — a popup-label typo, not the button-naming question that section was chasing.
    POPUPS = dict(SlotGame.POPUPS, **{
        "denominations": "denomination",   # NOT the reference plural — verified live
        "autoplay": "autoplay",            # NOT the reference camelCase `autoPlay` — verified live
        "big_win": None,      # big win is a tap-anywhere Canvas listener, not a popup
        "total_win": None,
        "buy_bonus": None,    # no Buy Bonus in this build
        # Not in this build's label list (games/chaos/docs/raw/test-bridge-api.md, "Popup labels"):
        # there is no `freeSpinResult` popup — the skip control is a BUTTON (`skip:freespinResult`).
        "freespin_result": None,
        # The build splits promo across `promoMessage` / `promoReceive` / `promoList`; there is no
        # single `promo` label. Left as None (= "no such popup") rather than guessing one of the
        # three, which is the mistake that produced the denomination bug above. Nothing reads this
        # role today; give it a real label only when a scenario needs one, and verify it live first.
        "promo": None,
    })
    UI_SURFACES = (
        ("base screen", None, None),
        ("bet list", "stake_up", "bet_close"),
        ("settings", "settings", "settings_close"),
        ("tutorial", "info", None),
        ("auto play panel", "auto", "auto_close"),
        ("history", "history", None),
    )
    FEATURE_TOGGLES = ("turbo", "ante", "sound")

    # ── Capabilities (test-bridge-api.md, CAPABILITIES) ──────────────────────────────────
    HAS_TURBO = True
    HAS_ANTE_BET = True
    HAS_BUY_BONUS = False        # `buyBonusNode` is unwired; the doc states it outright
    HAS_AUTOPLAY = True
    HAS_FREE_SPINS = True
    HAS_LAST_BET_PANEL = False   # no `lastBet` control in the bridge's list
    HAS_TUTORIAL_WEBVIEW = True  # cross-origin: the framework wheels it, the bridge only locates it
    HAS_SPEED_MODE_GETTER = True
    HAS_HISTORY_DETAIL_NAV = True
    HAS_SOUND_GETTERS = False    # no isSoundEnabled()/isMusicEnabled() in the bridge doc
    HAS_DEV_MODE = False         # forcing is setForcedResult() (one-shot), not setDevModeResult()
    HAS_FORCED_RESULT = True
    HAS_MULTIPLIER_LADDER = True
    HAS_REPLAY_MODE = True

    # ── Response reading ─────────────────────────────────────────────────────────────────
    def steps(self, raw: dict) -> list:
        """One grid per round: `result` is a single object, wrapped so `Bet.cascades == 1`."""
        r = raw.get(self.F_STEPS)
        return [r] if isinstance(r, dict) else ([] if r is None else list(r))

    def scatters(self, raw: dict):
        """Total Bonus/FreeSpin symbols on the grid — the free-spin trigger count.
        `None` when the grid is unreadable."""
        return self.symbol_count(raw, self.SYM_SCATTER)

    def wilds(self, raw: dict):
        """Total Wilds on the grid — the Raining Wild gate. `None` when unreadable."""
        return self.symbol_count(raw, self.SYM_WILD)

    def p1_ways(self, raw: dict):
        """P1's ways count = product of its counts on reels 1, 3 and 5."""
        return self.ways(raw, self.SYM_P1, self.P1_REELS)

    def scatter_reels(self, raw: dict):
        """1-based reel numbers holding at least one Bonus symbol. `None` when unreadable."""
        return self.reels_with(raw, self.SYM_SCATTER)

    def scatter_ways(self, raw: dict):
        """The Bonus ways multiplier: the product of the per-reel Bonus counts, over the reels that
        have one. `None` when unreadable, `0` when no Bonus landed."""
        counts = self.reel_counts(raw, self.SYM_SCATTER)
        if counts is None:
            return None
        hits = [n for n in counts if n > 0]
        if not hits:
            return 0
        ways = 1
        for n in hits:
            ways *= n
        return ways

    def expected_fs_award(self, raw: dict):
        """Free spins this round SHOULD award. `None` when the grid is unreadable; `0` when the
        Bonus did not reach enough reels.

        **The award is REEL-based, then multiplied by ways** — this is the rule the real payload
        settled, and it is neither of the two models the design doc offers:

            reels = how many distinct reels hold a Bonus   (>= 3 to trigger)
            base  = 12 / 15 / 20   for 3 / 4 / 5 reels
            ways  = product of the per-reel Bonus counts
            award = base x ways

        Confirmed sample: Bonus counts per reel `[1,2,0,2,0]` -> 3 reels, ways 1*2*2 = 4,
        base 12 -> **48 free spins**, which is exactly what the server awarded.

        Why the doc's readings are both wrong: by TOTAL SYMBOL count that grid holds 5 Bonuses and
        would award 20; by the doc's own "Base Free Spins (3 reels): 15" it would award 15 x 4 = 60.
        The server said 48. Only base-by-reels x ways reproduces it. (QA's "12, 15 or 20" was
        right — it just keys on REELS, not on symbols.)"""
        reels = self.scatter_reels(raw)
        if reels is None:
            return None
        if len(reels) < self.SCATTERS_TO_TRIGGER:
            return 0
        base = self.FS_BASE_BY_REELS.get(len(reels))
        if base is None:
            return 0
        return base * (self.scatter_ways(raw) or 1)

    def bet_lines(self, raw: dict) -> int:
        """Paylines in force for THIS round, server-reported. Falls back to the configured count."""
        v = raw.get(self.F_BET_LINES)
        try:
            return int(v) if v else int(self.PAYLINES or self.LINE_DIVISOR)
        except (TypeError, ValueError):
            return int(self.PAYLINES or self.LINE_DIVISOR)

    def line_bet_of(self, raw: dict) -> float:
        """Per-line bet for this round: base stake / the round's own betLines."""
        n = self.bet_lines(raw)
        return self.base_stake(raw) / n if n else 0.0

    def win_lines(self, raw: dict) -> list:
        """The paying lines, EXCLUDING the synthetic scatter/free-spin entry.

        A free-spin trigger is reported as a pseudo-line `{payline: -1, symbols: [-1,-1,-1],
        winType: "FreeSpin"}` whose `payoutRate` (1 in the sample) is NOT a line payout — the round
        paid 0. Counting it would invent a win, so it is filtered out by `payline == -1`."""
        out = []
        for p in raw.get(self.F_PAYLINES) or []:
            if isinstance(p, dict) and p.get("payline") != -1:
                out.append(p)
        return out

    def expected_line_rate(self, payline: dict):
        """The `payoutRate` a reported payline SHOULD carry, from the BO symbol table.

        A payline names its whole line (`winType: "J-J-J-A-K"`), and the win is the LEADING RUN —
        so the rate is `SYMBOL_PAYS[leader][run_length]`. WILD! substitutes for the leader, which is
        how `P2-WILD!-P2-WILD!-P3` scores as four P2 (rate 150).

        `None` when the rate cannot be derived: no `winType`, a leader with no paytable entry, or a
        line led by WILD! alone (WILD! pays nothing itself, so a run of pure wilds has no leader to
        price). Callers must treat `None` as *not checked*, never as agreement."""
        names = str((payline or {}).get("winType") or "").split("-")
        if not names or not names[0]:
            return None
        wild = str(self.SYMBOLS.get(self.SYM_WILD, "WILD!")) if self.SYM_WILD else "WILD!"
        leader = next((n for n in names if n != wild), None)
        if leader is None or leader not in self.SYMBOL_PAYS:
            return None
        run = 0
        for n in names:
            if n == leader or n == wild:
                run += 1
            else:
                break
        table = self.SYMBOL_PAYS[leader]
        return table[run - 1] if 1 <= run <= len(table) else None

    def wild_reels(self, raw: dict):
        """1-based reels holding a Wild. `None` when the grid is unreadable."""
        return self.reels_with(raw, self.SYM_WILD)

    def scatter_multiplier(self, raw: dict) -> float:
        """The round's scatter multiplier — P1's `ways x 10`. `0` when no P1 win."""
        try:
            return float(raw.get(self.F_SCATTER_MULTIPLIER) or 0)
        except (TypeError, ValueError):
            return 0.0

    def expected_line_win(self, raw: dict) -> float:
        """The LINE half of the win: `randomMultiplier x SUM(payoutRate) x baseStake / betLines`."""
        rate = 0.0
        for p in self.win_lines(raw):
            try:
                rate += float(p.get("payoutRate") or 0)
            except (TypeError, ValueError):
                pass
        mult = self.multiplier(raw)
        return rate * self.line_bet_of(raw) * (1.0 if mult is None else mult)

    def expected_scatter_win(self, raw: dict) -> float:
        """The SCATTER half: `scatterMultiplier x baseStake`.

        Note the divisor is absent — a scatter pays on the WHOLE bet, not per line. Confirmed by a
        captured P1 win: `scatterMultiplier 20 x baseStake 0.6 = 12`, which is exactly the round's
        `rawPayout` (dividing by `betLines` would have given 0.6). This matches the design doc's
        "Scatter winnings: base bet x Multiplier" and its P1 worked example, both of which turn out
        to be right.

        ⚠ Whether `randomMultiplier` also applies to the scatter half is **unconfirmed** — the only
        captured scatter win had `randomMultiplier: 1`, so the two readings are indistinguishable.
        It is deliberately NOT applied here, so a future scatter win on a x2+ spin will surface the
        question as a violation rather than hiding it."""
        return self.scatter_multiplier(raw) * self.base_stake(raw)

    def expected_raw_payout(self, raw: dict):
        """The round's win BEFORE the max-win cap: lines + scatter. `None` when the round carries
        neither a payline array nor a scatter multiplier to derive from."""
        if raw.get(self.F_PAYLINES) is None and self.F_SCATTER_MULTIPLIER not in raw:
            return None
        return round(self.expected_line_win(raw) + self.expected_scatter_win(raw), 10)

    def expected_payout(self, raw: dict):
        """What this round SHOULD have CREDITED — the raw win, truncated at the max-win cap.

            lineWin    = randomMultiplier x SUM(payoutRate) x baseStake / betLines
            scatterWin = scatterMultiplier x baseStake          <- no divisor; see above
            rawPayout  = lineWin + scatterWin
            payout     = min(rawPayout, cap left in the round)  <- SlotGame.max_win_left

        The cap counts the WHOLE round, so a free spin late in a bonus may credit less than
        `maxWin` itself; on a base spin (nothing won before it) the two are the same number.

        Verified exact on all six captured samples. The cap is not theoretical: one captured round
        earned `rawPayout: 12` against `maxWin: 10` and credited **10**, with `hasReachedMaxWin`
        true — so `payout` and `rawPayout` genuinely diverge and only `payout` reaches the balance.
        An earlier version of this method modelled lines only and produced a FALSE RED on that
        round, which is why both halves and the cap are all here."""
        raw_win = self.expected_raw_payout(raw)
        if raw_win is None:
            return None
        left = self.max_win_left(raw)
        return round(min(raw_win, left), 10) if left is not None else raw_win

    def raining_wild_expected(self, raw: dict):
        """Whether Raining Wild MUST play for this result (2+ Wilds -> 100%, else 0%).
        `None` when the grid is unreadable."""
        n = self.wilds(raw)
        return None if n is None else n >= self.RAINING_WILD_MIN_WILDS

    def ante_multiplier(self, raw: dict):
        """The ante multiplier THIS round was bet at, or `None` when the round was not an ante bet.

        **CONFIRMED** by a real ante payload: `anteMultiplier: 3`, `baseStake: 0.6`,
        `stake: 1.8` — so `stake == baseStake x anteMultiplier`, and `0` really does mean ante off.
        That payload also settles the question the design doc muddles: **it paid 4.5, which is
        `150 x 0.6/20`, derived from the UNCHANGED baseStake**. Deriving it from the inflated stake
        would have paid 13.5. Ante multiplies what the player pays; it never moves the payout base.

        Still returns `None` rather than the configured default when the field is 0, so a stream
        with no ante round reports that instead of asserting against a number nobody bet at."""
        v = raw.get(self.F_ANTE_MULTIPLIER)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def is_ante_round(self, raw: dict) -> bool:
        """Whether this round was bet with Ante Bet on, by either signal the contract could use:
        a non-zero `anteMultiplier`, or a deducted stake above the payout base."""
        if self.ante_multiplier(raw) is not None:
            return True
        try:
            return float(raw.get(self.F_STAKE) or 0) > float(self.base_stake(raw)) + 1e-9
        except (TypeError, ValueError):
            return False

    def base_stake(self, raw: dict) -> float:
        """The payout base for the round. Under Ante Bet this is NOT what was deducted: the player
        pays `stake` (= base x ante multiplier) while every payout still derives from this."""
        v = raw.get(self.F_BASE_STAKE)
        if v is None:
            v = raw.get(self.F_STAKE)
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def is_trigger(self, raw: dict) -> bool:
        """A round awarded the bonus when the server says so, or when the Bonus symbol reached
        enough distinct REELS (the counters may only appear on the next response)."""
        rem, awarded = self.free_spins(raw)
        if awarded > 0 or self.is_bonus_state(self.game_state(raw)):
            return True
        reels = self.scatter_reels(raw)
        return bool(reels is not None and len(reels) >= self.SCATTERS_TO_TRIGGER)


class JokerBanana(LineGame):
    """Joker: Where's My Banana (`joker`) — 5x3, 20 fixed paylines, expanding wilds, Buy Bonus.

    Bridge: `games/joker/docs/raw/bridge-api.md` (JokerBananaBridgeV3). Response shape and the
    arithmetic below: `games/joker/bet-response.md`, computed from three captured payloads in
    `games/joker/fixtures/bet-responses/` — not read off a spec. The feature itself:
    `games/joker/expanding-wild.md`.

    What makes this game its own profile rather than a Chaos clone:

      * **Expanding wilds.** Every response carries the reels TWICE — `rawViews` as they landed and
        `views` after expansion — so the feature is asserted by DIFFING them, never from pixels.
        `games/joker/docs/raw/paytable.json` settles the rule the help only implies:
        `BaseGame.EnableWildExpanding: false`, `FreeSpinGame.EnableWildExpanding: true`.
      * **The blocked symbols are `FreeSpin!` and `Power!`** (`ExcludedWildExpandingSymbols`) — the
        help calls them BONUS and BANANA. An expanding reel keeps those cells and overwrites the rest.
      * **Buy Bonus is a two-step purchase**: the round is bought, and THEN a Lucky Spin wheel offers
        x1 / x3 / x12 (`MultiplierLuckySpin`, default x3). The bridge never makes that choice and
        `dismissPopups()` never closes the wheel, so a scenario that stops after the purchase HANGS
        the game (bridge §12.5). `HAS_LUCKY_SPIN` gates the scenario that drives it.
      * **Free spins carry a multiplier** (`luckyMultiplier`), 3 by default on a natural trigger.

    ⚠ `multiplier` is NOT a payout factor here. The captured win reads `multiplier: 2.5` while its
    payout matches `Σ payoutRate x (stake / betLines)` WITHOUT it (1.5, not 3.75). `F_MULTIPLIER` is
    therefore deliberately NOT mapped to it — see `random_multiplier` below.
    """

    name = "joker"

    # ── Response contract (VERIFIED against games/joker/fixtures/bet-responses/) ──────────
    F_STEPS = "result"                  # {rawViews, views} -- ONE grid, not a step array
    F_STEP_GRID = "views"               # the reels AFTER expansion: what the player ends on
    F_RAW_GRID = "rawViews"             # ...and as they landed. The pair IS the feature.
    F_FREE_SPINS = "freeSpins"          # REMAINING (flat, not nested like Zeus)
    F_TOTAL_FREE_SPINS = "totalFreeSpin"
    F_LUCKY_MULTIPLIER = "luckyMultiplier"

    PAYLINES = 20                       # fixed; `betLines` echoes it on every round
    LINE_DIVISOR = 20                   # payout = sum(payoutRate) x (stake / 20)
    REELS = 5
    ROWS = 3

    SYM_WILD = "WILD!"
    SYM_SCATTER = "FreeSpin!"           # the free-spin trigger symbol
    SYM_POWER = "Power!"                # the scatter-pay symbol (help: BANANA)
    # Neither may be covered by an expanding wild (paytable ExcludedWildExpandingSymbols).
    WILD_EXPAND_BLOCKED = ("FreeSpin!", "Power!")
    # Reels the help says a wild expands on. ⚠ UNCONFIRMED against a capture: the one sample
    # expanded reel id 4, which fits this reading, but no capture shows the other reel.
    WILD_EXPAND_REELS = (2, 4)

    # paytable.json FreeSpins: 3/4/5 scatters -> 12/15/20 spins, all at multiplier 3.
    FREESPIN_AWARDS = {3: 12, 4: 15, 5: 20}
    FREESPIN_MULTIPLIER = 3
    # Packages[0]: one package, 20 spins at 100x the stake.
    BUY_BONUS_SPINS = 20
    BUY_BONUS_PRICE_X = 100
    # MultiplierLuckySpin: the wheel's ladder, x3 the default.
    LUCKY_MULTIPLIERS = (1, 3, 12)
    LUCKY_MULTIPLIER_DEFAULT = 3

    WIN_TIERS = (("SLIM_WIN", 0), ("SMALL_WIN", 1), ("MEDIUM_WIN", 2.5),
                 ("BIG_WIN", 10), ("MEGA_WIN", 25), ("ULTRA_WIN", 50))

    # ── Dialect (bridge-api.md §14 gives this table; §7.3 aliases the Mystic spellings) ───
    BUTTONS = dict(LineGame.BUTTONS, **{
        "spin": "spin",
        "stop": "spin",                  # the spin button in STOP state
        "turbo": "turbo",
        "stake_up": "betPlus",           # BOTH open the denomination list
        "stake_down": "betMinus",
        "bet_row": "bet:row:{i}",
        "bet_close": "bet:close",
        "auto": "auto",
        "auto_count": "auto:count:{i}",
        "auto_stop_at_win": "auto:stopAtWin",
        "auto_confirm": "auto:confirm",
        "auto_close": "auto:close",
        "auto_stop": "auto:stop",
        "auto_stop_main": "auto",        # `auto` doubles as stop while a run is going
        "history": "history",
        "history_close": "history:close",
        "detail_next": "historyDetail:next",
        "detail_prev": "historyDetail:prev",
        "info": "info",
        "info_close": "info:close",
        "settings": "settings",
        "settings_close": "settings:close",
        "sound": "sound",
        "settings_sound": "settings:sound",
        "settings_music": "settings:music",
        "settings_turbo": "settings:turbo",
        "message_ok": "message:ok",
        "trophy": "monumentalWin",
        "ante": None,                    # no Ante in this game
        "buy_bonus": "buyBonus",
        "buy_package": "buyBonus:package:{i}",   # only i = 0 exists
        "buy_purchase": "buyBonus:buy",
        "buy_confirm": "buyBonus:confirm",
        "buy_cancel": "buyBonus:cancel",
        # game-specific
        "buy_level": "buyBonus:level:{i}",       # 0..2 stake shortcuts
        "lucky_keep": "luckySpin:keep",
        "lucky_spin": "luckySpin:spin",
    })

    POPUPS = dict(LineGame.POPUPS, **{
        "message": "message",
        "disconnect": "disconnect",
        "denominations": "denomination",
        "autoplay": "autoplay",
        "settings": "setting",
        "history": "history",
        "history_detail": "historyDetail",
        "information": "information",
        "game_limit": "gameLimit",
        "buy_bonus": "buyBonus",
        "lucky_spin": "luckySpin",       # ⚠ dismissPopups() never closes this one
        "trophy": "monumentalWins",
        "promo": None,                   # split across promoMessage / promoReceive / promoList
        "big_win": None,                 # a presentation, not a popup (busyOps: bigwin)
        "total_win": None,               # likewise (roundState SHOW_FREE_SPIN_RESULT)
        "turbo_notify": "notify:turbo",
        "auto_notify": "notify:autoplay",
    })

    AUTO_COUNTS = (10, 20, 30, 50, None)

    UI_SURFACES = (
        ("base screen", None, None),
        ("bet list", "stake_up", "bet_close"),
        ("settings", "settings", None),
        ("tutorial", "info", None),
        ("auto play panel", "auto", "auto_close"),
        ("history", "history", None),
        ("buy bonus panel", "buy_bonus", "buy_cancel"),
    )
    FEATURE_TOGGLES = ("turbo", "sound")

    HAS_ANTE_BET = False
    HAS_BUY_BONUS = True
    HAS_LUCKY_SPIN = True
    HAS_WILD_EXPANDING = True
    HAS_BET_POPUP = True
    HAS_HISTORY_DETAIL_NAV = True
    HAS_TUTORIAL_WEBVIEW = True
    HAS_LAST_BET_PANEL = False
    # ⚠ WAS False with the note "bridge §9 lists no isSoundEnabled()/isMusicEnabled()". It does:
    # `games/joker/docs/raw/bridge-api.md` §9 lists both, as INDEPENDENT flags -- the main `sound`
    # button flips the pair, Settings toggles each. `sound-toggles` returns UNREACHED when this is
    # False, so the mistake made JK-24/25 permanently inconclusive rather than failing loudly.
    HAS_SOUND_GETTERS = True
    HAS_DEV_MODE = False                 # forcing is setForcedResult(), like Chaos
    HAS_FORCED_RESULT = True
    HAS_MULTIPLIER_LADDER = True

    # ── Reading the response ──────────────────────────────────────────────────────────────
    def steps(self, raw: dict) -> list:
        """One grid per round: `result` is a single object, wrapped so `Bet.cascades == 1`."""
        r = raw.get(self.F_STEPS)
        return [r] if isinstance(r, dict) else ([] if r is None else list(r))

    def free_spins(self, raw: dict) -> tuple:
        """(remaining, awarded) — with the BOUGHT round's own spelling folded in.

        ⚠ A Buy Bonus purchase does NOT report `totalFreeSpin` at all. Captured live 2026-09-21
        (`fixtures/bet-responses/buybonus-purchase.json`): the round carries `freeSpins: 20` and
        `freeSpinPackage {freeSpins: 20, price: 100}`, and no `totalFreeSpin` key. A natural trigger
        and every free spin of a natural bonus DO carry it, so the base reader is right for them and
        wrong only here — it returned `awarded == 0`, which made

          * `{facts.awarded}` read 0, so `awarded == package_spins` failed as `0 == 20`; and
          * `is_trigger()` answer **False** on the one round that unarguably awarded the bonus.

        The fallback is deliberately gated on `freeSpinPackage` rather than added as a blanket
        `F_TOTAL_FREE_SPINS_ALIASES` entry: `freeSpins` is REMAINING, so treating it as the award
        everywhere would report a mid-bonus free spin with 5 left as "5 awarded" and make
        `is_trigger` true for every round of every bonus. `freeSpinPackage` is present only on a
        purchase, which is exactly the round whose remaining count IS its award.
        """
        remaining, awarded = super().free_spins(raw)
        if not awarded and isinstance(raw, dict) and raw.get("freeSpinPackage"):
            awarded = remaining
        return remaining, awarded

    def raw_reels(self, raw: dict) -> list:
        """The reels AS THEY LANDED (`result.rawViews`), before any wild expansion."""
        step = (raw.get(self.F_STEPS) or {}) if isinstance(raw.get(self.F_STEPS), dict) else {}
        return list(step.get(self.F_RAW_GRID) or [])

    def reels(self, raw: dict) -> list:
        """The reels AFTER expansion (`result.views`) — what the player ends the round looking at."""
        step = (raw.get(self.F_STEPS) or {}) if isinstance(raw.get(self.F_STEPS), dict) else {}
        return list(step.get(self.F_STEP_GRID) or [])

    def expanded_reels(self, raw: dict) -> list:
        """Reel ids where `views` differs from `rawViews` — i.e. where a wild expanded.

        The whole assertion surface for the feature (`games/joker/expanding-wild.md`). Empty on
        every base round of the captures, and `[4]` on the free spin that expanded.
        """
        out = []
        for before, after in zip(self.raw_reels(raw), self.reels(raw)):
            if before.get("value") != after.get("value"):
                out.append(before.get("id"))
        return out

    def lucky_multiplier(self, raw: dict):
        """The free-spin multiplier this round was played at, or `None` when the field is absent.

        `0` means "not a free spin" on this backend (every base capture reads 0), so it is returned
        as None rather than as a multiplier of zero — Rule 19: a field saying NOTHING is not a value.
        """
        v = raw.get(self.F_LUCKY_MULTIPLIER)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def random_multiplier(self, raw: dict):
        """⚠ NOT the payout multiplier. `multiplier` is present (2.5 on the captured win) and the
        payout matches the line arithmetic WITHOUT it. Exposed for reporting only; no invariant may
        multiply by it until the game team says what it is."""
        return raw.get("multiplier")

class Grandma(WaysGame):
    """Grandma Pets (sl_45_grandma_pets) — 1024 ways on a 5x4 grid, with a per-round WILD
    MULTIPLIER, WALKING WILDS in free spins, a Buy Bonus, a Free Spin Extension and the Cat vs Dog
    drama.

    Sources: `games/grandma/bridge-api.md` (the bridge surface) and `games/grandma/bet-response.md`
    (the payload, verified against five real captures).

    ⚠ `games/grandma/docs/raw/paytable.json` is NOT a source for pay VALUES — it is not certainly
    the table a given session runs on. Nothing in this profile reads a pay rate from it; the
    response reports `symbolMultiplier` per win and that is what the arithmetic uses.

    Four things are spelled differently from every other game in this repo, and each one fails
    SILENTLY rather than loudly if it is left at the default:
    """

    name = "grandma"

    # ── Geometry ─────────────────────────────────────────────────────────────────────────
    REELS = 5
    ROWS = 4
    WAYS_TOTAL = 1024                   # the rules page's own number: 4 ** 5, full mask

    # The paytable prints each symbol against the TOTAL bet ("Payout = Total Bet x Icon
    # Multiplier"), so the divisor is 1. `WaysGame` leaves it None because a ways game has no
    # per-LINE bet at all; this says the figure on the paytable screen is the whole stake, which is
    # what `paytable-bet-scaling` needs to compare two bet levels.
    LINE_DIVISOR = 1

    # ── (1) The grid field is SINGULAR ───────────────────────────────────────────────────
    # `view` / `rawView`, not the plural `views` every other build sends. A profile that inherits
    # "views" reads nothing and every symbol census silently answers "no grid".
    F_STEP_GRID = "view"
    F_RAW_GRID = "rawView"

    # ── (2) There is no `gameState` ──────────────────────────────────────────────────────
    # The round reports `state` (where it was PLAYED) and `nextState` (where it goes). The engine's
    # question is always "does the bonus continue?", and the bridge answers it with `nextState`
    # (bridge-api.md §4: drain until `freeSpins == 0 || nextState == 1`).
    #
    # ⚠ Mapping this to `state` would break the Buy Bonus outright: the purchase round is PLAYED in
    # base (`state: 1`) and hands over to the bonus (`nextState: 2`), so `is_terminal` would call it
    # terminal and the drain would stop before a single free spin ran.
    F_GAME_STATE = "nextState"
    # `pending` keeps the ordinary spelling, so it needs saying separately.
    F_PENDING_GAME_STATE = "gameState"

    # ── (3) The award is its own field ───────────────────────────────────────────────────
    # `freeSpins` is REMAINING, `totalFreeSpin` is the bonus TOTAL, and `wonFreeSpins` is what THIS
    # round handed out. The three are all present on every round (bet-response.md §6), which is why
    # `is_trigger` below can be exact instead of inferred.
    F_FREE_SPINS = "freeSpins"
    F_TOTAL_FREE_SPINS = "totalFreeSpin"
    F_WON_FREE_SPINS = "wonFreeSpins"

    # ── (4) A purchase keeps its `baseStake` ─────────────────────────────────────────────
    # `stake` is the money charged (75 x bet on a purchase, 0 on a free spin) and `baseStake` is the
    # real bet on EVERY round, purchase included. So the inherited `base_stake()` is already right
    # here, unlike Joker's, where the field is absent on a purchase and the price leaks through.
    F_STAKE = "stake"
    F_BASE_STAKE = "baseStake"

    # ── The per-way payout fields ────────────────────────────────────────────────────────
    F_WAY_DETAILS = "wayDetails"
    F_TOTAL_WAYS = "totalWays"
    F_WAY_COUNT = "wayCount"
    F_WAY_SYMBOL = "symbolId"
    F_WAY_SYMBOL_MULTIPLIER = "symbolMultiplier"
    F_WAY_WILD_MULTIPLIER = "totalWildMultiplier"
    F_WAY_TOTAL_MULTIPLIER = "totalMultiplier"
    F_WAY_PAYOUT = "payout"
    F_WAY_WILD_POSITIONS = "wildPositions"
    F_WILD_MULTIPLIERS = "wildMultipliers"

    # ── Buy Bonus / package ──────────────────────────────────────────────────────────────
    # ⚠ Present on the purchase AND on every free spin of the bonus it bought, so it can never
    # answer "was this a purchase" — `is_purchase()` below uses `subType` for that.
    F_PACKAGE = "package"
    F_SUB_TYPE = "subType"              # 5 on the purchase, 0 on everything else
    F_MISC_TYPE = "miscType"            # "BuyBonusA" on the purchase, "NA" otherwise
    BUY_MULTIPLIER = 75

    # ── Symbols — numeric ids (bet-response.md §3, docs/assets/assets.yml) ───────────────
    SYM_WILD = 19
    SYM_SCATTER = 11
    # The premium the anticipation glow keys on besides the scatter: P1, the white cat. An ordinary
    # ways symbol with a high pay rate -- so a win needs it on reels 1, 2 and 3 with no gap, which
    # is what makes a broken run a testable negative.
    SYM_GLOW_PREMIUM = 5
    CAT_SYMBOLS: tuple = (5, 7, 8)      # white / orange / grey cat
    DOG_SYMBOLS: tuple = (6, 18)        # pug / dachshund
    # 3, 4 or 5 REELS holding a scatter award these. Counted by REEL: several on one reel count
    # once (docs/raw/2.jpg), which is the rule TC-103 exists to check.
    FREESPIN_AWARDS: dict = {3: 8, 4: 12, 5: 15}
    # The wild multiplier ladders, per mode. Read them live with `getMultiplierLevels()`; these are
    # what the client is expected to offer (bridge-api.md §8.1). Free spins never draw 1x.
    MULTIPLIER_VALUES: tuple = (1, 2, 3, 5, 8, 10, 15, 30, 40)
    FREE_MULTIPLIER_VALUES: tuple = (2, 3, 5, 8, 10, 15, 30, 40)

    # ── Capabilities ─────────────────────────────────────────────────────────────────────
    HAS_BUY_BONUS = True
    HAS_FREE_SPINS = True
    HAS_ANTE_BET = False                # AnteBetSetting.Enabled is false, and `anteBet` is null
    HAS_PREMIUM_REEL = False
    HAS_LAST_BET_PANEL = False
    HAS_SOUND_GETTERS = True            # bridge §6 lists isSoundEnabled() and isMusicEnabled()
    HAS_SPEED_MODE_GETTER = True
    HAS_HISTORY_DETAIL_NAV = True
    HAS_TUTORIAL_WEBVIEW = True
    HAS_BET_POPUP = True                # the stake arrows OPEN the denomination list (bridge §3)
    HAS_DEV_MODE = False                # ⚠ setForcedResult / setDevModeResult are NOT implemented

    BUTTONS = dict(SlotGame.BUTTONS, **{
        "stop": "stop",
        # bridge §3: `auto` doubles as the main-screen stop; `auto:stop` exists only in the panel.
        "auto_stop_main": "auto",
        "ante": None,                   # null while ante is disabled for the operator
        "buy_bonus": "buyBonus",
        "buy_package": "buyBonus:package:{i}",
        "buy_confirm": "buyBonus:confirm",
        "buy_cancel": "buyBonus:cancel",
        "bet_plus": "bet:plus",
        "bet_minus": "bet:minus",
        "settings_close": "settings:close",
        "settings_sound": "settings:sound",
        "settings_music": "settings:music",
        "settings_turbo": "settings:turbo",
        # ⚠ History and the guide are NOT on the main screen on this build: they live inside the
        # Settings panel (bridge-api.md §7 `SettingPopup`; its main-screen table still lists
        # `history` / `info` and is out of date — clicking those fails `not_available:history`).
        # BUTTON_VIA below makes a click on either open Settings first.
        "history": "settings:history",
        "info": "settings:info",
        "info_close": "info:close",
        "history_close": "history:close",
        # The round detail opens ON TOP of the History list and covers `history:close`, so it has
        # to be closed first (bridge §7 `HistoryDetail`). Unmapped, every close waited 8 s for a
        # covered button and fell back to dismissPopups() (2026-09-30).
        "detail_close": "historyDetail:close",
        # The Free Spin Extension offer. ⚠ Closing it is a DECLINE, not a dismissal (bridge §8.4),
        # and every bonus in this game ends on it: until it is answered the spin button never frees
        # up, so the drain has to click one of these.
        "extension_confirm": "extension:confirm",
        "extension_decline": "extension:decline",
        "extension_close": "extension:close",
        "freespin_result_skip": "freeSpinResult:skip",
        # ⚠ NOT in bridge-api.md §7, which lists `BigWinPopup` only as a presentation popup that
        # "ends through its own skip / timeout" without naming the control. The name below is what
        # the game team says it is; `skip_big_win()` reports `unknown_control` loudly rather than
        # silently doing nothing if this build does not have it.
        "big_win_skip": "bigWin:skip",
    })

    # History and the guide are reached through Settings (see `history` / `info` above).
    BUTTON_VIA = {"settings:history": "settings", "settings:info": "settings"}

    # Money is printed with 2 decimals and the rest CUT, not rounded. Seen twice on dev: a server
    # balance of 15069.237 shown as 15,069.23 (2026-09-29), and a 0.195 win shown as 0.19 in the
    # History detail (2026-09-30). Rounding would have shown 15,069.24 and 0.20.
    DISPLAY_DECIMALS = 2
    DISPLAY_TRUNCATES = True

    POPUPS = dict(SlotGame.POPUPS, **{
        "history": "HistoryPopup",
        "history_detail": "HistoryDetail",
        "information": "InformationPopup",
        "autoplay": "AutoPlayPopup",
        "denominations": "DenominationPopup",
        "settings": "SettingPopup",
        "message": "MessagePopup",
        "big_win": "BigWinPopup",
        "buy_bonus": "MultipleBuyBonusPopup",
        "freespin_result": "FreeSpinResultPopup",
        "disconnect": "DisconnectPopup",
        "promo": None,
        # Grandma's own surfaces.
        "get_free_spin": "GetFreeSpinPopup",
        "extra_free_spin": "ExtraFreeSpinPopup",
        "extension_offer": "BuyExtraFreeSpinPanel",
        "lucky_spin": "LuckySpinPopup",
        "trophy": "MonumentalWinsPopup",
        "game_limit": "PopupGameLimit",
    })

    # ── Response reading ─────────────────────────────────────────────────────────────────
    def is_trigger(self, raw: dict) -> bool:
        """Did THIS round award free spins? Exact here, because the server says so outright.

        ⚠ The inherited definition — "awarded > 0 OR the state is a bonus state" — is wrong on this
        game twice over. `totalFreeSpin` keeps reporting the bonus total on every round INSIDE the
        bonus, and `nextState` is 2 for all of them, so every free spin would answer True and any
        "the trigger awarded N" assert would grade the wrong round."""
        return self.won_free_spins(raw) > 0

    def is_purchase(self, raw: dict) -> bool:
        """Was this round a Buy Bonus purchase?

        ⚠ NOT `package is not None`: `package` is carried by the purchase AND by every free spin of
        the bonus it bought, so that test is true for the whole bonus. `subType` is 5 only on the
        purchase itself, with `miscType` as the second witness."""
        if raw.get(self.F_SUB_TYPE) == 5:
            return True
        return str(raw.get(self.F_MISC_TYPE) or "").startswith("BuyBonus")

    def bonus_reels(self, raw: dict):
        """1-based reel numbers holding a BONUS (scatter). `None` when the grid is unreadable.

        This is the count the award table is keyed on — REELS, not symbols."""
        return self.reels_with(raw, self.SYM_SCATTER)

    def expected_award(self, raw: dict):
        """Free spins this round SHOULD award, from the number of reels holding a BONUS.

        `None` when the grid cannot be read, so a caller reports unreached rather than comparing
        against a guess. `0` for a grid with too few reels — that is a real answer, not a gap."""
        reels = self.bonus_reels(raw)
        if reels is None:
            return None
        return self.FREESPIN_AWARDS.get(len(reels), 0)

    def max_scatters_on_a_reel(self, raw: dict):
        """The most BONUS symbols on any single reel. `None` when the grid is unreadable.

        `>= 2` is what makes a trigger interesting: it is the case a ways-based award count would
        get wrong, and the award must be unchanged by it."""
        counts = self.reel_counts(raw, self.SYM_SCATTER)
        return None if counts is None else (max(counts) if counts else 0)

    def wild_cells(self, raw: dict):
        """Every cell holding a WILD, as `{row, col}` 0-based (col 0 = reel 1, row 0 = top) — the
        shape `getWalkingWildState().cells` uses, so the two compare directly. `None` when the grid
        is unreadable."""
        reels = self.grid(raw)
        if reels is None:
            return None
        want = self._norm(self.SYM_WILD)
        return [{"row": row, "col": col}
                for col, reel in enumerate(reels)
                for row, sym in enumerate(reel) if self._norm(sym) == want]

    def way_details(self, raw: dict) -> list:
        """The per-way breakdown. `[]` on a losing round — the server omits the entries rather than
        sending zeros."""
        v = raw.get(self.F_WAY_DETAILS)
        return v if isinstance(v, list) else []

    def way_payout(self, way: dict, base_stake: float):
        """What one `wayDetails` entry SHOULD pay:

            symbolMultiplier x totalWildMultiplier x wayCount x baseStake

        Verified exact against all 8 entries of the five captures (bet-response.md §4). `None` when
        the entry is missing a field, so a malformed entry is reported rather than scored as 0.

        ⚠ `baseStake`, never `stake`: a free spin costs 0 and still pays off the real bet."""
        try:
            sym = float(way.get(self.F_WAY_SYMBOL_MULTIPLIER))
            wild = float(way.get(self.F_WAY_WILD_MULTIPLIER, 1) or 1)
            count = float(way.get(self.F_WAY_COUNT))
        except (TypeError, ValueError):
            return None
        return sym * wild * count * float(base_stake or 0)

    def wild_multiplier(self, raw: dict):
        """The wild multiplier drawn for this round, or `None` when the round reports none.

        ⚠ `None` is the normal answer on a round that won nothing: `wildType` and `wildMultipliers`
        are both null there, even with wilds sitting on the grid (bet-response.md §4). So "every
        round draws a multiplier in the ladder" is NOT assertable — only winning rounds are."""
        entries = raw.get(self.F_WILD_MULTIPLIERS)
        if not isinstance(entries, list):
            return None
        for e in entries:
            if isinstance(e, dict) and e.get("symbolId") == self.SYM_WILD:
                try:
                    return float(e.get("multiplierValue"))
                except (TypeError, ValueError):
                    return None
        return None

    def family_of(self, symbol_id):
        """`"CAT"`, `"DOG"` or `None` — the drama's two families (bridge §8.5)."""
        try:
            sid = int(symbol_id)
        except (TypeError, ValueError):
            return None
        if sid in self.CAT_SYMBOLS:
            return "CAT"
        if sid in self.DOG_SYMBOLS:
            return "DOG"
        return None


_REGISTRY: dict = {}


def register(profile: SlotGame) -> SlotGame:
    _REGISTRY[profile.name] = profile
    return profile


def get(name: str) -> SlotGame:
    """Profile by game name; falls back to the cascade reference so an unprofiled game still runs on
    the defaults (and says so, rather than crashing)."""
    if name not in _REGISTRY:
        print(f"(no game profile for '{name}' -- using the '{MysticWishes.name}' defaults)")
        return _REGISTRY[MysticWishes.name]
    return _REGISTRY[name]


register(MysticWishes())
register(Zeus())
register(Dragon())
register(DragonVip())
register(ChaosRunner())
register(JokerBanana())
register(Grandma())
