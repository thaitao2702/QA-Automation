# Joker: Where's My Banana — Bridge API

The QA surface this build exposes on `window.__gameTestBridge`, for writing the game's
profile and test suites. Covers every public function, snapshot field, popup label, button
name, response shape and event.

- **What** a bridge must do in general → `game-bridge-contract.md` (the authority)
- **This file** → what *this* bridge actually does, its names, and its game-specific flows
- Source → `assets/joker-banana/scripts/test-bridge/JokerBananaBridgeV3.ts`

---

## Contents

1. [Activation & conventions](#1-activation--conventions)
2. [Boot recipe](#2-boot-recipe)
3. [Required functions](#3-required-functions)
4. [`canDo` — diagnostic actions](#4-cando--diagnostic-actions)
5. [`getSnapshot()` fields](#5-getsnapshot-fields)
6. [Popups (`visiblePopups` labels)](#6-popups-visiblepopups-labels)
7. [Buttons (name registry)](#7-buttons-name-registry)
8. [Server responses (the round stream)](#8-server-responses-the-round-stream)
9. [Optional getters & actions](#9-optional-getters--actions)
10. [Event capture](#10-event-capture)
11. [Capabilities](#11-capabilities)
12. [Game flows, step by step](#12-game-flows-step-by-step)
13. [Reason strings (glossary)](#13-reason-strings-glossary)
14. [Suggested profile mapping](#14-suggested-profile-mapping)
15. [Known limitations](#15-known-limitations)

---

## 1. Activation & conventions

**Activation.** Open the game with `?bridge=1` (or `#/...?bridge=1` in the hash). Without it
nothing is installed and the game is untouched. The bridge is installed at the very start of
loading, *before* the server connection, so it exists before Start is clicked.

```js
await page.waitForFunction(() => !!window.__gameTestBridge);
```

**Conventions that hold for every function:**

| Rule | Meaning |
|---|---|
| Never throws | Getters return `null` / `[]` / `0` / `'UNKNOWN'` instead of throwing. |
| Coordinates, not clicks | Rects are CSS pixels relative to the **viewport**. Click with `page.mouse.click(centerX, centerY)`. The bridge never clicks on your behalf. |
| `null` = not here | `getButtonRect(name)` → `null` means the control does not exist in this build **or** its popup is closed. Use it to probe for features. |
| Waits reject | Every `waitFor*` / `await*` rejects on timeout. None of them hang. |
| Raw payloads | Server responses are returned exactly as received: the full envelope, unmodified. |
| No round outcomes | Nothing in the snapshot tells you what a spin paid. Read the response. |

**`ButtonRect`**

```ts
{ x, y, width, height,     // CSS px, viewport-relative
  centerX, centerY,        // aim here
  visible,                 // active in hierarchy AND centre inside the canvas
  interactable }           // the control's own enabled flag (NOT "not covered")
```

`interactable: true` does not mean a click will land, because something may cover the
control. Use `waitForButtonClickable` when you need that guarantee.

---

## 2. Boot recipe

Run this on every page load, including reloads and language switches.

```
1. startGame()                         resolves at the Start click, nothing more
2. info = getGameInfo()                launch payload captured BEFORE the click
3. if info.pending owes a bonus:       pending.freeSpins > 0  OR  pending.gameState != 1
     if pending.gameState == 3:        ⚠ LUCKY SPIN resume, see §12.5
        click luckySpin:keep | luckySpin:spin
     loop r = awaitFreespinRound()     until r.result.freeSpins == 0 OR r.result.gameState == 1
4. waitForSpinClickable()              the game is at a clean, spinnable base
```

- `getGameInfo()` returns `{status, code, msg, timestamp?, result: GameInfo}`. Unwrap `result`.
- `pending` is the only resume signal. Never use `lastBet` to decide a resume: a max-win
  truncated `lastBet` still shows spins left that are forfeited.
- ⚠ **Two different "gameState 3"s.** In `pending.gameState` (ServerGameState), `3` =
  **LuckySpin** (waiting on the wheel choice). In a BET response `gameState` (GameState),
  `3` = **BuyBonus**. Base = `1`, Free = `2`, Promo = `4` in both.

---

## 3. Required functions

### `startGame(): Promise<void>`

Clicks Start and resolves **at the click**.

| | |
|---|---|
| Waits for | App state `ready` (the landing screen with the Start button), up to **120 s** |
| Resolves immediately | If the game has already started, or in REPLAY mode (which starts itself) |
| Rejects | `Error("[BridgeV3] wait timed out after 120000ms; last reason: app_state:<state>")` |
| Does NOT | wait for idle, play out a resumed bonus, dismiss popups, or skip resumed rounds |

Resumed free-spin rounds begin arriving the moment this resolves. They are buffered, so you
will not miss them (§8).

### `getGameInfo(): object | null`

The GAME_INFO envelope, **captured once before Start** and returned unchanged (deep copy)
for the whole session. Later re-fetches after a reconnect or promo never replace it.

Key fields of `result`:

```jsonc
{
  "currency": "…", "balance": 1234.5, "betlines": 20,
  "settings": {
    "minBet": 0.2, "maxBet": 100,
    "denominations": [0.2, 0.4, …],     // ascending; index i == bet:row:<i>
    "defaultBetLevel": 3,                // ⚠ ONE-BASED in this game (see below)
    "maxPayout": …, "maxMultiplier": …,
    "winFormula": [{ "name": "BIG", "min": …, "max": … }, …],
    "compliance": { "bonusBuyAllowed": true, "turboMode": true, "minSpinDurationSec": …, … }
  },
  "pending": {                           // the ONLY resume signal
    "freeSpins": 0, "gameState": 1,      // ServerGameState: 1 Base 2 Free 3 LuckySpin 4 Promo
    "stake": …, "luckyMultiplier": …, "tryLuckySpin": false,
    "accumulatedWinAmount": …, "roundId": …, "refId": …
  },
  "lastBet": { … } | null,               // context only, never control flow
  "freeSpinPackages": [{ "id", "name", "freeSpins", "price" }],
  "currentFreeSpinPackage": { … } | null,
  "luckyMultipliers": [1, 3, 12],        // the Lucky Spin wheel ladder
  "defaultLuckyMultiplier": …,
  "paytable": [{ "symbolId", "value": [] }],
  "promo": { … } | null, "needToStartPromo": false
}
```

⚠ **`defaultBetLevel` is 1-based here.** The game opens a *first* session at
`denominations[defaultBetLevel - 1]` (falling back to `denominations[0]` if the balance
cannot cover it). A returning session opens at `lastBet.baseStake`. Account for both in the
profile; the bridge reports the raw value.

### `getSnapshot(): object`

Synchronous, cheap, safe at any moment. Fields in §5.

### `getButtonRect(name): ButtonRect | null`

Answers immediately with no waiting. Names in §7.

| Result | Meaning |
|---|---|
| `null` | Unknown name, not in this build, compliance-disabled (`buyBonus`), or its popup is closed |
| `{visible:false}` | The control exists but is inactive or off-canvas |
| `{visible:true}` | On screen (may still be covered) |

### `waitForButtonClickable(name, stableMs = 250, timeoutMs = 30000): Promise<ButtonRect>`

Resolves when a click on `name` would really register: resolved, visible, interactable,
topmost under its centre in the Cocos tree, no DOM element over it, and all of that held
continuously for `stableMs`. Returns a freshly read rect.

Rejects with `Error("[BridgeV3] wait timed out after <ms>ms; last reason: <reason>")`
(reasons in §13). It also rejects, rather than hanging, for names that never resolve.

### `waitForSpinClickable(stableMs = 250, timeoutMs = 60000): Promise<ButtonRect>`

**The single "idle" authority.** Resolves only when **all** of these hold for `stableMs`,
checked in this order (the first failing one becomes the reject reason):

| # | Condition | Reason when failing |
|---|---|---|
| 1 | Game scene live (app state `playing`) | `scene_not_ready` |
| 2 | No modal popup open (toasts don't count) | `popup:<label>` |
| 3 | Mode is `NORMAL` (not free spin, lucky spin, promo) | `mode:<MODE>` |
| 4 | Not in autoplay | `mode:AUTOPLAY` |
| 5 | Round state `READY` | `state:<RoundState>` |
| 6 | No presentation running (reels, **wild expansion**, big win, free-spin outro, persistence coins, count-ups…) | `busy:<op,op>` |
| 7 | Not waiting for a response, no bet in progress, connected, denominations loaded | `waiting_response` · `bet_in_progress` · `spinning` · `connection:<state>` · `no_denominations` |
| 8 | Keyboard/spin lock released | `key_locked` |
| 9 | Spin button in mode NORMAL and state IDLE | `spin_mode:<X>` · `spin_state:<X>` |
| 10 | Spin button visible, enabled, hit-testable, no DOM overlay | see §13 |

⚠ **An insufficient balance does NOT block it.** Spin stays clickable, and the click shows
the out-of-money message (`visiblePopups: ['message']`, no BET sent). That is testable.

Use it before every spin, as the settle signal after a round, and as the boot assertion.

### `awaitBetResponse(timeoutMs = 15000): Promise<Envelope>`

The **next unconsumed** round response (§8), raw. It never clicks. A response that arrived
between your click and this call is returned, not skipped, so it is safe to click first
and then await.

Rejects with an **object** (not an Error):

```js
{ reason: 'bet_response_timeout', snapshot: {...}, errors: [/* last 3 error envelopes */] }
```

### `awaitFreespinRound(timeoutMs = 60000): Promise<Envelope>`

Same buffer and same cursor as `awaitBetResponse`, with a longer default. It is passive: the
client fires free spins itself, so **never click during free spins**. Loop until the round is
terminal:

```
terminal = r.result.freeSpins == 0  OR  r.result.gameState == 1   // max win can end early
```

---

## 4. `canDo` — diagnostic actions

`canDo(action): { ok: boolean, reason?: string }`. **Diagnostic only.** Use it to explain a
timeout, never to decide readiness.

| `action` | `ok` when | Typical reasons |
|---|---|---|
| `spin` | Same conditions as `waitForSpinClickable` (without the stability window) | see §3 |
| `changeBet` / `openBet` | NORMAL, not auto, READY, `betPlus` clickable | `mode:*` `state:*` + §13 |
| `openBuyBonus` | Buy button present and clickable, NORMAL | `unresolved` `mode:*` |
| `luckySpin` / `chooseLuckySpin` | Lucky Spin popup open and its buttons still live | `popup_not_open:luckySpin` · `lucky_spin_choice_made` |
| `openAutoplay` `openSettings` `openHistory` `openInfo` `openRecommend` | The opening **button** is clickable (never gated on the popup being open) | §13 |
| `stopAutoplay` | Autoplay running and `auto` clickable | `not_autoplaying` |
| `skipBigWin` | Round state `SHOW_BIG_WIN` | `no_bigwin` |
| *any button name* | That control is clickable right now | §13 |

Every action first requires `sceneReady` (otherwise `scene_not_ready`).

---

## 5. `getSnapshot()` fields

### Asserted: safe to use in test assertions

| Field | Type | Notes |
|---|---|---|
| `visiblePopups` | `string[]` | Modals **front-most first**, then toasts. `[]` when none. Labels in §6 |
| `visibleModals` | `string[]` | Modals only (the ones that block clicks) |
| `visibleNotifications` | `string[]` | Toasts only: `notify:turbo`, `notify:autoplay`, `notify:ante` |
| `isAutoPlay` | `boolean` | |
| `balance` | `number` | Model balance (the displayed value is `getBalance()`) |
| `betAmount` | `number` | Current stake = cost of the next spin (no Ante in this game) |
| `betIndex` | `number` | Index of `betAmount` in `denominations`, `-1` if not found |
| `denominations` | `number[]` | Ascending stake ladder |

### UI display values: what the counter shows, not game state

| Field | Notes |
|---|---|
| `autoSpinsRemaining` | Autoplay counter. `0` when off. **`Infinity`** for unlimited |
| `freeSpinsRemaining` | Free-spin counter on screen |

### Game-specific extras

| Field | Type | Notes |
|---|---|---|
| `isLuckySpinMode` | `boolean` | Mode `LUCKY_SPIN`: a bought or resumed bonus is before its first free spin |
| `luckySpinChoicePending` | `boolean` | Keep/Spin popup is up **and** still waiting for a choice |
| `isWildExpanding` | `boolean` | The expanding-wild animation is playing now |
| `isAnteBet` | `false` | This game has no Ante |
| `freeSpinPackageCount` | `number` | `gameInfo.freeSpinPackages.length` |
| `buyBonusPopupLoaded` | `boolean` | Buy Bonus popup instance exists |
| `buyBonusSelectedPrice` | `number` | Price the open Buy panel would charge (stake × package price); `0` when closed |

### Diagnostics: explain a refusal, never decide one

| Field | Values |
|---|---|
| `sceneReady` | `boolean` |
| `busyOps` | `string[]`: `round` `roundEnd` `freespinIntro` `buyBonusReveal` `wildExpanding` `bigwin` `persistence` `luckyWheel` `countUp` (see §10) |
| `isFreeSpinMode` | `true` in `FREE_SPIN` **or** `LUCKY_SPIN` |
| `mode` | `NONE` (before Start) · `NORMAL` · `FREE_SPIN` · `LUCKY_SPIN` · `PROMO` |
| `roundState` | `READY` · `SPINNING` · `SPIN_FINISHED` · `SHOW_WILD_EXPENDING` · `SHOW_WIN_LINE` · `SHOW_BIG_WIN` · `SHOW_FREE_SPIN` · `SHOW_FREE_SPIN_RESULT` |
| `spinType` | `normal` · `freespin` · `packagea` (bought bonus) · `promo` |
| `speedMode` | `NORMAL` · `TURBO` · `UNKNOWN` |
| `speedModeRaw` | `NORMAL` · `TURBO` · `SUPER_TURBO` (the toggle switches NORMAL↔SUPER_TURBO) |
| `isPromoMode` | `boolean` |
| `isSpinning` · `isBetInProgress` · `waitingResponse` | `boolean` |
| `connectionState` | `connected` · `connecting` · `reconnecting` · `disconnected` · `error` · … |
| `isLockKeyDown` · `isHoldingSpin` | `boolean` |
| `spinButtonMode` | `NORMAL` · `FREE_SPIN` · `AUTO` |
| `spinButtonState` | `IDLE` · `SPINNING` · `STOP` (click = stop reels now) · `UNACTIVE` |
| `minBet` · `maxBet` · `currency` | from game info |
| `gamePlayType` | `NORMAL` · `REPLAY` |
| `gameInfoCaptured` | `boolean`: `false` means `getGameInfo()` is a late read and `pending` is untrustworthy |
| `betLogLength` · `betCursor` · `betErrorCount` | response-buffer diagnostics |

If the snapshot itself fails it returns
`{ sceneReady:false, visiblePopups:[], busyOps:['snapshot_failed'], error }`.

---

## 6. Popups (`visiblePopups` labels)

Listed front-most first, which is the order `visiblePopups` reports them in.

| Label | What it is | Blocks spin | `dismissPopups()` closes it via |
|---|---|---|---|
| `message` | Generic message / error / out-of-money / reality check | ✔ | `hide()` (does **not** run the OK action, e.g. reload) |
| `disconnect` | Reconnecting overlay | ✔ | `PopupManager.hideDisconnect()` |
| `luckySpin` | **Lucky Spin wheel: Keep / Spin choice** | ✔ | ✘ **never**: it is a bonus decision (§12.5) |
| `buyBonus` | Buy Bonus panel (+ its confirm sheet) | ✔ | `clickClose()` |
| `information` | Paytable / tutorial (WebView) | ✔ | `hide()` |
| `gameLimit` | Game limits (opened from Settings) | ✔ | `hide()` + lock release |
| `setting` | Settings | ✔ | lock release + `hide()` |
| `autoplay` | Autoplay panel | ✔ | `hide()` |
| `denomination` | Stake list | ✔ | `hide()` |
| `historyDetail` | History ticket | ✔ | `hide()` |
| `history` | History list | ✔ | `hide()` |
| `monumentalWins` | Trophy / Monumental Wins | ✔ | `hide()` |
| `recommendJoin` | Recommendation "join" panel | ✔ | `hideJoin()` |
| `recommendDiscover` | Recommendation "discover" panel | ✔ | `hideDiscover()` |
| `promoMessage` | Promo message | ✔ | confirm button + hide |
| `promoReceive` | Promo completed / reward | ✔ | cancel (or confirm) button + hide |
| `promoList` | Promo list | ✔ | `hidePopupPromos()` |
| `replay` | Replay end dialog (not registered in this build, always absent) | ✔ | `hide()` |
| `replayLanguage` | Replay-mode language selector | ✔ | `hide()` |
| `notify:turbo` | Toast "Turbo spin enabled/disabled" | ✘ | `hideImmediate()` |
| `notify:autoplay` | Autoplay toast | ✘ | `hideImmediate()` |
| `notify:ante` | Ante toast (unused in this game) | ✘ | `hideImmediate()` |

The big win and the free-spin total win are **presentations, not popups**. They show up as
`busyOps` (`bigwin`, `roundEnd`) and `roundState` (`SHOW_BIG_WIN`, `SHOW_FREE_SPIN_RESULT`).
Both are skipped by clicking **anywhere** on the canvas (when turbo compliance allows); they
have no button rect.

### `dismissPopups(): Promise<string[]>`

Closes every open surface through its real close path, in two passes about 250 ms apart. It
returns the labels that were **observed** closed (`[]` if nothing was open). If it closed
something and the game is now fully idle, it also releases a stuck spin lock (the Game Limit
popup leaves one behind). Popups it could not close stay in `visiblePopups` and are logged.

---

## 7. Buttons (name registry)

Every name works with `getButtonRect`, `waitForButtonClickable`, and `canDo`.
**Popup buttons resolve only while their popup is open**; otherwise they return `null`.

### 7.1 Main screen

| Name | Control | Notes |
|---|---|---|
| `spin` | Spin button | IDLE → bets. **STOP state → stops the reels** (about 0.3 s after the spin starts). During autoplay and free spins it is a counter, not interactable |
| `turbo` | Turbo toggle | Check with `getSpeedMode()`. Disabled in promo when not allowed, and by compliance |
| `auto` | Autoplay | Idle → opens the `autoplay` panel. **While autoplay runs → stops it** |
| `betMinus` / `betPlus` | Stake − / + | **Both open the `denomination` list** (they do not step the stake). Disabled at min/max |
| `buyBonus` | Buy Bonus | `null` when compliance `bonusBuyAllowed=false`. Hidden during free spins |
| `anteBet` | — | Always `null` (no Ante) |
| `history` · `info` · `settings` | Footer buttons | Disabled unless round READY, not free spin, not autoplay |
| `sound` | Whichever of `soundOn`/`soundOff` is on screen | Toggles sound **and** music together |
| `soundOn` · `soundOff` | The two sound nodes | Only one is visible at a time |
| `monumentalWin` | Trophy | Opens `monumentalWins` |
| `recommend` | Recommendation side tag | Opens `recommendDiscover`. `null` if the panel failed to load |

### 7.2 Popup controls

| Popup | Names |
|---|---|
| `autoplay` | `autoplay:count:<i>` (i = 0..4 → **10, 20, 30, 50, ∞**; opens on index 0) · `autoplay:stopAtWin` · `autoplay:next` / `autoplay:back` (stake ±) · `autoplay:confirm` (idle only) · `autoplay:stop` (running only) · `autoplay:cancel` · `autoplay:close` |
| `denomination` | `denomination:row:<i>` (i = index in `denominations`, 0 = lowest) · `denomination:plus` · `denomination:minus` · `denomination:close` (the X) · `denomination:backdrop` (tap-outside area) |
| `setting` | `setting:turbo` · `setting:music` · `setting:sound` · `setting:turboTab` · `setting:history` · `setting:info` · `setting:gameLimit` · `setting:close` |
| `gameLimit` | `gameLimit:close` |
| `message` | `message:ok` · `message:cancel` · `message:close` (each only if shown for that message) |
| `information` | `information:close` · `information:webview` (prefer `getTutorialRect()`) |
| `history` | `history:row:<i>` (i = **rendered** row index) · `history:timeRange` · `history:close` |
| `historyDetail` | `historyDetail:prev` · `historyDetail:next` · `historyDetail:close` |
| `monumentalWins` | `monumentalWins:close` |
| `buyBonus` | `buyBonus:level:<i>` (i = 0..2, the stake shortcuts) · `buyBonus:next` / `buyBonus:prev` (stake ±) · `buyBonus:buy` = `buyBonus:package:0` (opens the confirm sheet; there is **one** package) · `buyBonus:confirm` (sheet only) · `buyBonus:cancel` (sheet's "No", or panel close when no sheet) · `buyBonus:close` |
| `luckySpin` | `luckySpin:keep` (keep the shown multiplier) · `luckySpin:spin` (spin the wheel for a new one). Both go dead as soon as either is clicked |
| `recommendDiscover` | `recommendDiscover:close` |
| `recommendJoin` | `recommendJoin:close` |
| `promoReceive` | `promoReceive:confirm` · `promoReceive:cancel` · `promoReceive:other` |
| `promoMessage` | `promoMessage:ok` |
| `promoList` | `promoList:close` |

While the Buy **confirm sheet** is open, the panel's own controls (`level`, `next`, `buy`,
`close`…) return `null`. Only `confirm` / `cancel` resolve.

### 7.3 Reference-dialect aliases (Mystic spellings)

| Alias | → Native |
|---|---|
| `stakeIncrease` / `stakeDecrease` | `betPlus` / `betMinus` |
| `bet:row:<i>` · `bet:plus` · `bet:minus` · `bet:close` | `denomination:*` |
| `auto:count:<i>` · `auto:confirm` · `auto:stop` · `auto:stopAtWin` | `autoplay:*` |
| `auto:close` · `auto:cancel` | `autoplay:cancel` |
| `settings:turbo` · `settings:music` · `settings:sound` · `settings:close` | `setting:*` |
| `info:close` | `information:close` |
| `trophy` | `monumentalWin` |
| `stopAuto` · `autoOrFreeSpin` | `auto` (the main-screen auto button stops a running autoplay) |

### `getAllButtonRects(): Record<string, ButtonRect>`

Every name that currently resolves, aliases included. Use it for probing and triage.

---

## 8. Server responses (the round stream)

One append-only buffer, **one shared cursor**. Each `awaitBetResponse` /
`awaitFreespinRound` call consumes exactly one record in arrival order. Buffering starts
before Start, so resumed rounds are never lost.

| Socket event | Enters the stream? | Why |
|---|---|---|
| `BET` | ✔ | Every base, free, promo and autoplay spin |
| `buyfreespin` (Buy Bonus) | ✔ | It is a real round: stake debited, spins awarded |
| `luckySpin` | ✘ side log only | A multiplier pick, not a round (no stake or gameState) |
| Any envelope with `status <= 0` | ✘ error log only | Errors are not rounds, so correlation cannot slip |

### Envelope (what the await functions return)

```jsonc
{ "status": 1, "code": 0, "msg": "", "timestamp": 1726…,
  "result": { /* SpinResult */ } }
```

### `result` fields (SpinResult)

| Field | Meaning |
|---|---|
| `roundId`, `id`, `refId` | identity |
| `gameState` | GameState: `1` Base · `2` Free · `3` BuyBonus · `4` Promo |
| `stake` | amount charged (**`0` for free spins**; the client sends stake 0 outside NORMAL mode) |
| `baseStake` | the bet level the round pays on |
| `payout`, `rawPayout` | win of this spin (after / before max-win cap) |
| `accumulatedWinAmount` | running total of the bonus |
| `totalFreeSpinPayout` | free-spin total |
| `balance` | **authoritative** post-round balance |
| `freeSpins` | **remaining** free spins (not a loop counter; see terminal rule) |
| `totalFreeSpin` | awarded total |
| `luckyMultiplier` | Lucky Spin multiplier applied to free-spin line wins |
| `scatterMultiplier` | "Banana/Power" scatter pay multiplier |
| `paylines[]` | `{payline, symbols[], payoutRate, winType}`; `payline: -1` = scatter/free-spin entry |
| `result.rawViews[]` | reels **before** wild expansion |
| `result.views[]` | reels **after** wild expansion (diff the two to assert expansion) |
| `betLines` | 20 |
| `hasReachedMaxWin`, `maxWin` | max-win cap hit |
| `promoResult` | promo counters when in a promo |
| `freeSpinPackage` | **Buy Bonus responses only**: `{id, name, freeSpins, price}` |

### Side channel: Lucky Spin response (`getBetLog().side`)

```jsonc
{ "status": 1, "result": { "playerId", "balance", "luckyMultiplier", "tryLuckySpin", "freeSpins" } }
```

The chosen multiplier also appears as `luckyMultiplier` on every free-spin BET response that
follows, so you normally do not need the side log.

### `getBetLog()`

```ts
{ cursor: number,             // next record awaitBetResponse will return
  records: BetRecord[],       // the round stream
  errors:  BetRecord[],       // status<=0 envelopes (seq < 0)
  side:    BetRecord[] }      // lucky spin picks
// BetRecord = { seq, t /* performance.now() */, kind: 'bet'|'buyBonus'|'luckySpin', result /* raw envelope */ }
```

`setBetLogging(false)` silences the per-response console line (on by default).

---

## 9. Optional getters & actions

| Function | Returns | Notes |
|---|---|---|
| `dismissPopups()` | `Promise<string[]>` | §6 |
| `getBetAmount()` | `number` | Cost of the next spin (= stake) |
| `getBetIndex()` | `number` | Index into `getDenominations()`, `-1` if the stake is off-ladder |
| `getBalance()` | `number` | **Displayed** balance (lags during count-ups). The authority is `result.balance` |
| `getDenominations()` | `number[]` | Ascending; index `i` ↔ `bet:row:<i>` |
| `getSpeedMode()` | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` | TURBO covers both fast tiers |
| `isSoundEnabled()` / `isMusicEnabled()` | `boolean` | Independent flags. The main `sound` button flips both; Settings toggles each |
| `getTutorialRect()` | `ButtonRect \| null` | The paytable **WebView** rect, `null` when closed. Scroll it with `page.mouse.wheel` (cross-origin, so the bridge cannot) |
| `getHistoryRows()` | `HistoryRow[]` | Rendered rows only: `{index, rect, clickable, data:{nodeName, roundId, betTime, betAmount, totalWinAmount, totalFreeSpins, balance, isBuyBonus, promoType}}` |
| `getHistoryDetailState()` | object | `{open, betIndex, betCount, hasPrev, hasNext, roundId, betId, bet, winning, balance}`. Loop on `hasNext`/`hasPrev` (they include the max-win summary page) |
| `getMultiplierLevels()` | `{baseGame, freeGame}` | `baseGame: [1]` (base lines pay x1), `freeGame: gameInfo.luckyMultipliers` (e.g. `[1,3,12]`) |
| `scrollBetRowIntoView(i)` | `void` | Scrolls the open denomination list to row `i` (0.1 s). No-op if closed |
| `setForcedResult(spinResult)` | `boolean` | Forces the **next single BET**. Pass the **inner** SpinResult, not the envelope. One-shot; `null` clears. Returns `false` in REPLAY. Does not affect Buy Bonus or a whole free-spin chain |
| `setDevModeResult(spinResult)` | `boolean` | Alias of `setForcedResult` |
| `holdSpinStart()` / `holdSpinEnd()` | `void` | Real press-and-hold on spin (hold-for-turbo). `holdSpinStart` **throws** if `canDo('spin')` is not ok |
| `getCapabilities()` | `Record<string, boolean>` | §11 |
| `startEventCapture(label)` / `endEventCapture(label)` | `void` / `Event[]` | §10 |
| `canDo(action)` | `{ok, reason?}` | §4 |
| `dispose()` | `void` | Removes every hook. Only for tear-down tooling |

---

## 10. Event capture

```js
bridge.startEventCapture('turbo-toggle');
/* drive the UI */
const events = bridge.endEventCapture('turbo-toggle');   // [] when nothing / unknown label
// Event = { t /* performance.now(), ms */, type, label?, data? }
```

Events are recorded **only while a capture window is open**.

| `type` | `label` / `data` | When |
|---|---|---|
| `capture_start` / `capture_end` | capture label | window edges |
| `start_clicked` | | `startGame()` issued the Start click |
| `popup_shown` / `popup_hidden` | popup **node name** (e.g. `SettingPopup`, `BuyBonusPopup`) | any BasePopup show/hide |
| `popup_dismissed` | bridge label (§6) | confirmed closed by `dismissPopups()` |
| `bet_response` | seq · `{kind}` | a round entered the stream |
| `bet_error` | seq · `{kind}` | an error envelope arrived |
| `lucky_spin_response` | seq | Lucky Spin pick arrived |
| `round_start` / `round_end` | | whole spin presentation (reels → wild → lines → big win → scatter) |
| `roundEnd_start` / `roundEnd_end` | | after the round; includes the **free-spin total-win outro** and promo-completed popup |
| `freespinIntro_start` / `_end` | | free-spin entrance animation |
| `buyBonusReveal_start` / `_end` | | fake 5-bonus spin after a purchase |
| **`wildExpanding_start`** / **`wildExpanding_end`** | `data: {reels: n}` | **expanding-wild animation** (live free spin *and* reload replay of the last bet) |
| `bigwin_start` / `bigwin_end` | | big/mega/ultra win presentation |
| `persistence_start` / `_end` | | coin-pile trail/coin animation |
| `luckyWheel_start` / `_end` | | Lucky Spin wheel spinning |
| `countUp_start` | `setBalance` / `setWinAmount` · `{seconds}` | a balance/win count-up began (no `_end`; it is time-based) |
| `busy_expired` | op | a persistence animation exceeded 10 s and was released (game bug signal) |
| `lock_released` | closed labels | `dismissPopups()` released a stranded spin lock |
| `forced_result_consumed` | | `setForcedResult` fired |
| `hold_spin_start` / `hold_spin_end` | | hold escape hatch used |

The windows can nest: `round_start … wildExpanding_start … wildExpanding_end … bigwin_start …`.

---

## 11. Capabilities

`getCapabilities()`: `false` means the dependent case should be *inconclusive*, never passed.

| Key | Value in this build |
|---|---|
| `HAS_TURBO` | ✔ (control present) |
| `HAS_ANTE_BET` | ✘ |
| `HAS_BUY_BONUS` | ✔ when the button resolves **and** `compliance.bonusBuyAllowed !== false` |
| `HAS_LUCKY_SPIN` | ✔ |
| `HAS_FREE_SPINS` | ✔ |
| `HAS_WILD_EXPANDING` | ✔ |
| `HAS_PERSISTENCE` | ✔ |
| `HAS_AUTOPLAY` · `HAS_AUTO_STOP_MAIN` | ✔ (`auto` doubles as stop) |
| `HAS_PROMO` | ✔ only when the session has a promo |
| `HAS_HISTORY` | ✔ |
| `HAS_RECOMMENDATION` | ✔ when the panel loaded |
| `HAS_TUTORIAL_WEBVIEW` | ✔ |
| `HAS_FORCED_RESULT` | ✔ (✘ in REPLAY) |
| `HAS_MULTIPLIER_LADDER` | ✔ |
| `HAS_BET_POPUP` | ✔ (stake ± open the list) |
| `HAS_NOTIFICATIONS` | ✔ (toasts are in `visiblePopups`) |
| `IS_REPLAY_MODE` | ✘ normally |

---

## 12. Game flows, step by step

### 12.1 Base spin

```js
const r0 = await b.waitForSpinClickable();
await page.mouse.click(r0.centerX, r0.centerY);
const env = await b.awaitBetResponse();          // env.result.stake == betAmount
await b.waitForSpinClickable();                  // settle: whole presentation finished
```

If `env.result.freeSpins > 0`, the spin **triggered free spins** → go to 12.3 (do not wait for
spin first; it will not resolve until the bonus is over).

### 12.2 Change stake

```
click betPlus (or betMinus)          → visiblePopups contains 'denomination'
scrollBetRowIntoView(i)              (optional, for rows below the fold)
waitForButtonClickable('bet:row:<i>') → click
getBetIndex() == i, getBetAmount() == denominations[i]
click 'bet:close'                    (or the popup may close itself on selection)
```

### 12.3 Free spins (and wild expanding)

The client plays them by itself, so **do not click**.

```js
let r;
do { r = (await b.awaitFreespinRound()).result; }
while (!(r.freeSpins === 0 || r.gameState === 1));
await b.waitForSpinClickable(undefined, 120000);  // total-win outro + return to base
```

- Wilds expand **only in free spins**. Assert from the response:
  `result.views` (after) differs from `result.rawViews` (before) on the expanded reels.
  Assert the animation with event capture: `wildExpanding_start{reels}` / `_end`.
- Retriggers add spins: `freeSpins` goes back up and the loop continues.
- Max win: `hasReachedMaxWin: true`, and the round can come back with `gameState 1` while
  `freeSpins > 0` (forfeited). That is why the terminal rule uses OR.

### 12.4 Autoplay

```
click auto                  → 'autoplay'
click autoplay:count:<i>    (0..4 = 10,20,30,50,∞; index 0 is preselected, so pick another to see a change)
[click autoplay:stopAtWin]
click autoplay:confirm      → popup closes, isAutoPlay true, the client starts betting
loop awaitBetResponse(90000) for each round (gaps between rounds are presentation time)
stop:  click auto           → isAutoPlay false, no further BET
```

`waitForSpinClickable` never resolves during autoplay (`mode:AUTOPLAY`).

### 12.5 Buy Bonus → Lucky Spin → free spins

```
1. waitForButtonClickable('buyBonus') → click          → 'buyBonus'
2. [click buyBonus:level:<i> | buyBonus:next/prev]     buyBonusSelectedPrice updates
3. click buyBonus:buy                                   → confirm sheet
4. click buyBonus:confirm                               → panel closes
      (not enough balance → 'message' opens instead, no request sent)
5. p = await awaitBetResponse()                         p.result.freeSpinPackage present,
                                                        balance debited, freeSpins = awarded
   busyOps: buyBonusReveal (fake 5-bonus spin, coin animation)
6. wait until visiblePopups includes 'luckySpin' AND luckySpinChoicePending
7. click luckySpin:keep   (keep the current multiplier)
      or luckySpin:spin   (spin the wheel; busyOps: luckyWheel)
   → Lucky Spin response lands in getBetLog().side
8. loop awaitFreespinRound() as in 12.3              r.luckyMultiplier = the chosen multiplier
```

⚠ The bridge never makes the step-7 choice, and `dismissPopups()` never closes `luckySpin`.
If a test stops after step 4, the game stays waiting for the choice. Teardown must click
`luckySpin:keep` and drain, or reload.

**Resume case.** `pending.gameState == 3` with `pending.freeSpins > 0` means the player left
at the wheel. After `startGame()` the `luckySpin` popup opens. Do step 7 then step 8.

### 12.6 Resume of a free-spin bonus (`pending.gameState == 2`)

Rounds start the moment Start is clicked. Loop `awaitFreespinRound()` to terminal, then
`waitForSpinClickable()` (§2).

### 12.7 Reload with a last bet

On a normal reload the game **replays the last round's win** (lines, and wild expansion if it
had one). `roundState` stays `SHOW_WIN_LINE` and `busyOps` may show `wildExpanding` until it
is done. `waitForSpinClickable` covers it. No BET is sent for this replay.

### 12.8 Turbo

```
click turbo → getSpeedMode(): 'NORMAL' ↔ 'TURBO'; visiblePopups gets 'notify:turbo' briefly
```

A toast never blocks `waitForSpinClickable`.

### 12.9 Insufficient balance

With `balance < betAmount`, `waitForSpinClickable` still resolves. Clicking spin opens
`message` (out of money) and **no BET is sent**. Its OK button reloads the page, so close it
with `dismissPopups()` if you want to stay on the page.

---

## 13. Reason strings (glossary)

| Reason | Meaning |
|---|---|
| `scene_not_ready` | Game not started or the main scene is not built |
| `popup:<label>` | A modal is open (§6) |
| `mode:<MODE>` / `mode:AUTOPLAY` | Not in base game / autoplay running |
| `state:<RoundState>` | Round still presenting |
| `busy:<ops>` | Tracked animation running (§10 names) |
| `waiting_response` · `bet_in_progress` · `spinning` | Request or round pipeline active |
| `connection:<state>` | Not connected (reconnect in progress) |
| `no_denominations` | Game info not processed yet |
| `key_locked` | Spin lock held (a popup is open or a transition is running) |
| `no_spin_button` · `spin_mode:<X>` · `spin_state:<X>` | Spin button not in NORMAL/IDLE |
| `unresolved` | Name unknown, absent in this build, or popup closed |
| `not_visible` | Inactive or centre outside the canvas |
| `button_disabled` | Control's own `interactable` is false |
| `inactive` · `no_transform` · `no_scene` · `no_hit` | Engine-level hit-test failures |
| `masked` | Scrolled out of a list's mask (use `scrollBetRowIntoView`) |
| `blocked_by:<node>` | Another Cocos node covers the centre |
| `dom_overlay:<id>` | An HTML element (e.g. the loading overlay) covers the canvas there |
| `pred_threw:<msg>` | Internal error while checking (report it) |
| `app_state:<state>` | `startGame` still waiting for the landing screen |
| `bet_response_timeout` | `awaitBetResponse` / `awaitFreespinRound` timed out |
| `popup_not_open:luckySpin` · `lucky_spin_choice_made` | `canDo('luckySpin')` |
| `not_autoplaying` · `no_bigwin` | `canDo('stopAutoplay')` / `canDo('skipBigWin')` |

---

## 14. Suggested profile mapping

Role → name for the framework's `BUTTONS` table. The native names are listed, and every
reference alias in §7.3 also works.

```python
BUTTONS = {
    # core
    "spin": "spin",
    "stop": "spin",                  # spin button in STOP state stops the reels
    "turbo": "turbo",
    # bet
    "stake_up": "betPlus",           # HAS_BET_POPUP = True (opens the list)
    "stake_down": "betMinus",
    "bet_row": "bet:row:{i}",
    "bet_plus": "bet:plus",
    "bet_minus": "bet:minus",
    "bet_close": "bet:close",
    # autoplay            AUTO_COUNTS = [10, 20, 30, 50, float("inf")]
    "auto": "auto",
    "auto_count": "auto:count:{i}",
    "auto_stop_at_win": "auto:stopAtWin",
    "auto_confirm": "auto:confirm",
    "auto_close": "auto:close",
    "auto_stop": "auto:stop",
    "auto_stop_main": "stopAuto",    # == auto
    "auto_or_freespin": "autoOrFreeSpin",
    # panels
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
    "trophy": "trophy",
    # features
    "ante": None,                    # no Ante in this game
    "buy_bonus": "buyBonus",
    "buy_package": "buyBonus:package:{i}",   # only i = 0 exists
    "buy_confirm": "buyBonus:confirm",
    "buy_cancel": "buyBonus:cancel",
    # game-specific roles
    "buy_level": "buyBonus:level:{i}",       # 0..2
    "lucky_keep": "luckySpin:keep",
    "lucky_spin": "luckySpin:spin",
    "recommend": "recommend",
}
```

---

## 15. Known limitations

- **Promo mode.** `waitForSpinClickable` refuses in `mode:PROMO`, because promo spins count as
  a bonus. To drive promo spins use `waitForButtonClickable('spin')` and `awaitBetResponse()`.
- **Lucky Spin choice** is always the test's to make (§12.5).
- **Big-win / total-win skip** has no rect. Click anywhere on the canvas. It is ignored when
  turbo is disabled by compliance, or in a promo that disallows turbo.
- **`getBalance()`** is the displayed value and lags during count-ups. Assert balance on
  `result.balance`.
- **`defaultBetLevel`** is 1-based in this game (§3).
- **REPLAY mode** (`?mode=replay`): no response stream, no forced results, and
  `replayLanguage` is permanently visible.
- Behaviour was derived from the code and typechecked. Confirm it against a real build with
  `python scripts/probe_bridge.py --game <joker> --env dev` before relying on the timing
  defaults.
