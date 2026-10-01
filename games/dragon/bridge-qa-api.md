# Treasures of the Dragon — Test Bridge API (v3)

Surface reference for the QA framework. Implements `game-bridge-contract.md`.

Source: [`assets/src/test/DragonBridgeV3.ts`](assets/src/test/DragonBridgeV3.ts) ·
typed surface: [`assets/src/test/IBridgeV3.ts`](assets/src/test/IBridgeV3.ts) ·
install: [`assets/src/test/BridgeActivator.ts`](assets/src/test/BridgeActivator.ts)

This client is a fork of Zeus and the bridge is a port of `ZeusBridgeV3`. A Zeus
profile is the right starting point — but read [§13](#13-differences-from-zeus-v3)
first: several names, gates and payload details differ, and every difference is
one that would otherwise make a case silently inconclusive or falsely red.

**The split.** The bridge exposes the *surface* — button rects, popup visibility,
readiness, and the raw server responses. The framework owns the *logic* — payouts,
win tiers, Hold & Spin math, every rule assertion — and computes it from the raw
BET response. **The bridge never tells you what a round did.**

**Single authority per fact.** Exactly one signal answers each question, and
nothing cross-checks anything.

| Question | The only answer |
|---|---|
| did the game start? | `startGame()` |
| is a bonus owed? | `getGameInfo().pending` |
| did a round happen, and what did it pay? | `awaitBetResponse()` / `awaitFreespinRound()` |
| is the game back to a clean idle? | `waitForSpinClickable()` |

Everything else in the snapshot is a **display value** or a **diagnostic**. Neither
decides anything.

---

## Contents

1. [Access](#1-access)
2. [The core loop](#2-the-core-loop)
3. [REQUIRED functions](#3-required--the-suite-does-not-run-without-these)
4. [`canDo(action)`](#4-strongly-recommended--candoaction)
5. [`GameSnapshot` field by field](#5-gamesnapshot--field-by-field)
6. [Optional getters](#6-optional-getters)
7. [Diagnostics](#7-diagnostics)
8. [Types](#8-types)
9. [Button name registry](#9-button-name-registry)
10. [Popup label registry](#10-popup-label-registry)
11. [The BET response, as this bridge delivers it](#11-the-bet-response-as-this-bridge-delivers-it)
12. [What the bridge deliberately does NOT do](#12-what-the-bridge-deliberately-does-not-do)
13. [Differences from Zeus v3](#13-differences-from-zeus-v3)
14. [Checking a build](#14-checking-a-build)

---

## 1. Access

| | |
|-|-|
| Gate | `?bridge=1` — query string **or** hash-routed `#/path?bridge=1` |
| Global | `window.__gameTestBridge` |
| Install failure | `window.__gameTestBridgeError` — a stack, present only on failure |
| Version | `bridge.version` → `'dragon-bridge-v3/2026-09-11'` |
| Available from | as soon as `GameLogic` exists — **before** the game-info response lands, and before the Start button appears |
| Teardown | `BridgeActivator.deactivate()`, or `bridge.dispose()` |

Without `?bridge=1` nothing is constructed, no hook is installed, and the
production path costs nothing.

```ts
await page.goto(`${GAME_URL}&bridge=1`);
await page.waitForFunction(() => !!window.__gameTestBridge);
const version = await page.evaluate(() => window.__gameTestBridge.version);
```

Every call runs inside the page:

```ts
const r = await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
await page.mouse.click(r.centerX, r.centerY);
```

⚠ **If the global never appears, read `window.__gameTestBridgeError` before
anything else.** Installation is wrapped in a try/catch precisely so a failure
leaves a cause behind. A bridge that failed to install must not look the same as
one that was never requested.

### Install order

| Hook | Called from | Does |
|---|---|---|
| `BridgeActivator.arm()` | `LoadingScene.onEnable()`, just before `GlobalContext.boot()` | Polls every 20 ms until `GameLogic` and the state manager exist, then installs — early enough to see the launch payload exactly as the server sent it. |
| `BridgeActivator.activate(startGameFn)` | `LoadingScene.onGameReady()` (`AppState.READY`), and the REPLAY branch | Installs if `arm()` has not, and registers the Start handler. |

Both are idempotent and both no-op without `?bridge=1`.

---

## 2. The core loop

```ts
// ---- boot, once per page load -------------------------------------------
await bridge.startGame();                  // resolves at the Start CLICK. Nothing more.

const info = await bridge.getGameInfo();   // the CAPTURED launch payload
const pending = info?.result?.pending ?? info?.pending;

if (pending?.freeSpins > 0) {              // pending is the ONLY resume signal
    for (;;) {
        const round = await bridge.awaitFreespinRound();
        if (isTerminal(round)) break;      // YOUR rule, read off the response
    }
}

await bridge.waitForSpinClickable();       // the clean-idle assertion

// ---- one round ----------------------------------------------------------
const r = await bridge.waitForSpinClickable();
await page.mouse.click(r.centerX, r.centerY);   // a REAL mouse event
let result = await bridge.awaitBetResponse();

// ---- a Hold & Spin the round triggered ----------------------------------
while (!isTerminal(result)) {
    result = await bridge.awaitFreespinRound();  // passive — never click
}
await bridge.waitForSpinClickable();             // settled, celebration included
```

`isTerminal` is **framework code**. In this game the respin counter is
`freeSpinInfo.freespins` (see [§11](#11-the-bet-response-as-this-bridge-delivers-it));
terminate on "counter is 0 **or** the round is back in base", because
`hasReachedMaxWin` can end a bonus with respins still on the counter — those are
forfeited, and a counter-only loop would hang waiting for rounds that never come.

**The bridge returns coordinates, not click methods.** The framework issues a real
`page.mouse.click(centerX, centerY)`, so the true input pipeline is exercised.

---

## 3. REQUIRED — the suite does not run without these

### `startGame(): Promise<void>`

**Does.** Clicks the game's Start button (calls `LoadingScene.onStartClicked()`,
exactly what the button does) and resolves the instant that click is issued.

**When.** Once per page load, and again after every reload, language switch, or
second tab session. Repeat calls are harmless — it resolves immediately once the
gameplay scene exists. In REPLAY mode (which auto-starts) it always resolves
immediately.

**Does NOT** wait for the scene, wait for idle, play out a resumed bonus, or
dismiss popups. All of that is yours.

**Rejects** after 60 s if no Start handler ever appears, naming the app state it
was stuck in. It never hangs.

---

### `getGameInfo(): GameInfo`

**Does.** Returns the launch payload the bridge **captured before Start**,
unchanged, for the life of the session. A fresh deep copy on every call, so you
cannot mutate the captured one.

**When.** Once, immediately after `startGame()` resolves.

| Read | For |
|---|---|
| `pending` | *does this session owe a Hold & Spin?* — the **only** resume signal |
| `lastBet` | context about the previous round — **never** control flow |
| `settings.denominations` · `settings.defaultBetLevel` · `settings.minBet/maxBet/maxPayout/maxMultiplier` · `settings.winFormula` · `settings.compliance` | *what is configured?* |
| `currency` · `balance` · `paytable` · `freeSpinPackages` · `promo` · `needToStartPromo` | session facts |

**Shape.** The **inner** object — what the server put in `response.result` — not
the `{status, code, result}` envelope. (The defensive
`info.result?.pending ?? info.pending` in §2 is correct against both forms.)

`pending` in this game is
`{ refId, roundId, coinPositions[], freeSpins, stake, extraFreeSpins? }`.

**Never throws.** Safe at any time.

> ⚠ **It is a captured COPY, and that is load-bearing.** On the first round of a
> resume, `GameLogic` sets `gameInfo.pending.freeSpins = 0`, and right after
> Start `GameController.applyLastBet()` sets `gameInfo.lastBet = null`. A live read
> after the Start click — which is exactly when you call this — would report
> "nothing owed" and no `lastBet`.

> ⚠ **It is the SERVER's payload.** The capture is taken inside the `gameInfo`
> setter, before `GameLogic` annotates it (the setter adds a `type` field to every
> coin in `pending.coinPositions`). `diagnose().gameInfoSource` says which you got:
> `'raw'` (the normal case) or `'live'` (a fallback copy of the stored object,
> taken only if the hook missed the payload).

> ⚠ **`pending` decides, always — never `lastBet`.** A max-win-truncated `lastBet`
> reads like a live bonus through the same parser while `pending` correctly says
> nothing is owed.

> ⚠ **`settings.defaultBetLevel` — this client treats it as 1-BASED.** Everywhere
> it is read, the client subtracts one: `GameLogic.gameInfo` setter
> (`defaultBetLevel - 1`), `GameController.processEndPromo()` (`- 1`), and replay
> writes it back as `index + 1`. So on a **first** session this client opens at
> `denominations[defaultBetLevel - 1]`, not `denominations[defaultBetLevel]` as
> the contract's Mystic fixture implies. Every later session opens at
> `lastBet.baseStake ?? lastBet.stake` (`applyLastBet`). Confirm against a real
> first-session fixture before writing an "opens at the default bet" case — if
> the server means 0-based, that is a client defect worth filing.

**Companion:** `isGameInfoCaptured(): boolean` — false means no pre-Start capture
happened (REPLAY, or a bridge installed after Start) and you are reading a live
clone. Check it when a resume goes missing.

---

### `getSnapshot(): GameSnapshot`

**Does.** The current surface state — popups, toasts, autoplay, bet, balance.

**When.** In loops, and when explaining a failure. Synchronous, cheap (field reads
plus one pass over a static popup table), safe at **any** time including mid-spin,
mid-animation and mid-bonus, and it **never throws**. Carries no round outcomes.

Field roles: [§5](#5-gamesnapshot--field-by-field).

---

### `getButtonRect(name: string): ButtonRect | null`

**Does.** Where a named control is **right now**, without waiting.

**When.** To aim a real mouse click, and to **probe whether a control exists** in
this build at all.

| Return | Means |
|---|---|
| `null` | unknown name · **or** the control is hidden right now · **or** its popup is closed · **or** it is gated off because a click would not do its job (e.g. `stopAuto` with no run) |
| `{ visible: false, … }` | resolved, but its centre is off-canvas or no UI camera renders it |
| `{ visible: true, … }` | on screen — may still be covered or disabled; ask `isButtonClickable()` |

> ⚠ **Stricter than Zeus.** Zeus returned `{visible:false}` for an inactive node;
> this bridge returns `null` (contract §8: *cannot do its job right now → null*).
> A control hidden by compliance (`autoPlay`, `turboMode`, `bonusBuyAllowed`
> off) or by game phase (`buyBonus` during Hold & Spin) therefore reads as
> absent, and a probe reports the case *unreached*.

**Never throws.** Answers immediately.

```ts
const ante = await page.evaluate(() => window.__gameTestBridge.getButtonRect('anteBet'));
if (!ante) return unreached('no Ante control in this build');   // always null here
```

Names: [§9](#9-button-name-registry).

---

### `waitForButtonClickable(name, stableMs = 250, timeoutMs = 30_000): Promise<ButtonRect>`

**Does.** Waits until the control can **genuinely** be clicked — resolved, visible,
interactable, hit-testable (not covered, not clipped by a mask) — and has stayed
so for `stableMs`. Returns the rect re-read at resolve time.

**When.** Before every UI interaction.

**Rejects**
- **immediately** for a name this build has no concept of
  (`no such control in this build`);
- on timeout, with the last blocking reason (see [§4](#4-strongly-recommended--candoaction))
  and a snapshot line.

A **known** name that is merely hidden or whose popup is closed keeps waiting
(reason `unresolved:hidden_or_popup_closed`) — open the popup first.

⚠ Argument order is `stableMs` first.

---

### `waitForSpinClickable(stableMs = 250, timeoutMs = 60_000): Promise<ButtonRect>`

**THE single readiness / settle authority.** Nothing cross-checks it.

Three uses, all the same condition: before every spin; the **settle signal** after
a round (so the video covers the whole presentation); the **boot idle assertion**.

Resolves only when **all** hold:

| Clause | Reason on refusal | Closes |
|---|---|---|
| scene ready (spin handlers attached) | `scene_not_ready` | clicking a button with no listener |
| app state is `idle` | `state:<s>` | a bet on the wire, or a result still presenting |
| no autoplay run | `autoplay_running` | the client is driving itself (unlimited runs included) |
| not in Hold & Spin | `free_spins_running` | same |
| spin not held down | `hold_spin_running` | hold-to-spin auto-fires rounds while held |
| `busyOps` empty | `busy:<ops>` | **the settle guarantee** — spin re-arms *before* celebrations and the post-bonus persistence restart finish |
| no **modal** popup open | `popup_open:<label>` | something covering the button. Toasts are ignored — they take no input |
| spin control clickable | `button_disabled` / `blocked_by:<node>` / … | the PLAY face is up, the lock is off, nothing is on top |

**Rejects** on timeout rather than resolving optimistically.

> ⚠ **Readiness is about the UI, not the wallet.** An insufficient balance does
> **not** hold this back: spin stays clickable and the client shows its
> `not_enough_money` popup **on click**. That popup is the subject of the
> insufficient-balance cases.

> ⚠ Do **not** cross-check this with your own `busyOps` / `visiblePopups` poll.
> Those fields explain a refusal; they never decide one.

---

### `awaitBetResponse(timeoutMs = 15_000): Promise<BetResult>`

**Does.** Resolves with the **next** round response, **raw and unmodified**.

**When.** Immediately after any click that sends a bet, and to observe rounds the
client fires itself during autoplay.

- **Does not click.**
- **Cannot miss a response that arrived between your click and this call** — the
  tap is live from construction and a buffered response is handed over
  synchronously.
- **One response consumed per round, in order**, via one shared cursor with
  `awaitFreespinRound`. The protocol carries no client bet id, so correlation is by
  arrival order. Skip or double-consume one and every later correlation is off by
  one — `getUnconsumedBetCount()` should be `0` between rounds.
- **Rejects** on timeout with `{ reason: 'bet_response_timeout', snapshot }`.

**What counts as a round:** every response that reaches `AppState.RESULT` — BET,
**Buy Bonus** (`buyfreespin` goes through the same handler), Hold & Spin respins,
dev-mode forced results, and replay playback. Error responses (`status != 1`) are
**not** rounds and never enter the buffer; neither does the `lastBet` the client
re-renders at boot.

Raw means **before the client touched it** — see
[§11](#11-the-bet-response-as-this-bridge-delivers-it).

---

### `awaitFreespinRound(timeoutMs = 90_000): Promise<BetResult>`

**Does.** The next auto-played **Hold & Spin respin**, raw.

**When.** To play a bonus out without clicking — mid-scenario (a bonus you
triggered or bought) and at boot (a bonus this session resumed; the client starts
firing those rounds the moment Start is clicked).

Same shared cursor as `awaitBetResponse`; **passive** by contract — never click
during a bonus. The longer default covers each respin's own presentation.

**Termination is yours**, read off the response.

---

## 4. Strongly recommended — `canDo(action)`

```ts
canDo(action: string): { ok: boolean, reason?: string }
```

**DIAGNOSTIC ONLY.** Readiness is decided by `waitForSpinClickable` /
`waitForButtonClickable`, never by this. Its value is that when one of those
times out, `reason` explains why.

| Group | Actions |
|---|---|
| play | `spin` · `stopSpin` |
| bet | `openBetPopup` · `nextBet` · `previousBet` · `selectBet` |
| autoplay | `openAutoPopup` · `selectAutoSpinPackage` · `toggleAutoSpinStopAtWin` · `confirmAutoSpin` · `stopAutoSpin` |
| buy bonus | `openBuyBonusPopup` · `selectBuyBonusPackage` · `confirmBuyBonus` |
| toggles | `setTurbo` · `setSound` |
| panels | `openSettings` · `openInfo` · `openHistory` · `openTrophy` |

An unrecognised action returns `unknown_action:<name>`.

> ⚠ A control that OPENS a popup is never gated on that popup being open (defect
> D2). `nextBet` / `previousBet` / `openBetPopup` check main-screen preconditions.

### Reason vocabulary

Stable, machine-parsable: a bare token or `prefix:value`. Shared by `canDo`,
`waitForSpinClickable` and `waitForButtonClickable` rejections.

| Reason | Means |
|---|---|
| `scene_not_ready` | the gameplay scene is not up, or its input handlers are not attached yet |
| `state:<s>` | app state is not `idle` (`betting`, `result`, `error`, `promo`, …) |
| `autoplay_running` / `free_spins_running` | the client is driving itself |
| `hold_spin_running` | the spin button is being held (hold-to-spin) |
| `busy:<ops>` | a tracked presentation is in flight — see `busyOps` in §5 |
| `popup_open:<label>` | a modal popup covers the main screen |
| `popup_not_open:<label>` | a popup-scoped action was asked for while its popup is closed |
| `not_spinning` | `stopSpin` while already idle |
| `auto_not_running` | `stopAutoSpin` with no run in progress |
| `no_packages` | buy bonus with an empty `freeSpinPackages` |
| `unknown_name` | no control of that name exists in this build |
| `unresolved:hidden_or_popup_closed` | a known name that does not resolve right now |
| `not_visible` | resolved, but the centre is off-canvas / no camera |
| `button_disabled` | `interactable === false` (for `spin`: also "PLAY face not up" or "spin locked") |
| `clipped_by_mask` | scrolled out of an ancestor `Mask` — valid coordinates, unreachable |
| `blocked_by:<node>` | another input-consuming node covers the target; `<node>` names it |
| `no_consumer_at_point` | nothing would receive the click there |
| `inactive_hierarchy` / `target_null` | the node is off, or the reference is null |
| `predicate_threw:<msg>` | a readiness read threw — a bridge bug, report it |

---

## 5. `GameSnapshot` — field by field

| Field | Type | Role | Use it for |
|---|---|---|---|
| `visiblePopups` | `string[]`, modals front-most first, then toasts | **asserted** | every popup case; `[]` when none, never `null`. **Includes toasts** |
| `visibleNotifications` | `string[]` | **asserted** | the toast subset (`turboNotify`, `autoNotify`) under its own key |
| `isAutoPlay` | `boolean` | **asserted** | autoplay cases — `true` for unlimited runs too |
| `balance` · `betAmount` · `betIndex` | `number` | **asserted** | balance / bet cases (the dedicated getters are equivalent) |
| `denominations` | `number[]` | **asserted** | denomination cases |
| `autoSpinsRemaining` | `number` | **UI display value** | *does the counter show what it should?* `0` = no run; `Infinity` = unlimited (∞) |
| `freeSpinsRemaining` | `number` | **UI display value** | the respin counter |
| `sceneReady` | `boolean` | diagnostic | named in failure messages |
| `busyOps` | `string[]` | diagnostic | explains why spin is not yet clickable |
| `isFreeSpinMode` | `boolean` | diagnostic | explains a refusal during Hold & Spin |
| `buyBonusPopupLoaded` | `boolean` | extra | the Buy Bonus popup has been instantiated |
| `freeSpinPackageCount` | `number` | extra | how many packages the server offered |
| `buyBonusSelectedPrice` | `number` | extra | what **Buy** would charge right now — `currentBet × freeSpinPackages[0].price` — while the popup is open; `0` when closed |

> ⚠ **Toasts are popups for assertion purposes.** Toggling Turbo or Autoplay
> raises a ~2.4 s banner. It is reported in `visiblePopups` (and
> `visibleNotifications`) for exactly as long as it is visible — judged by
> opacity, because the banner node stays active after it fades. So
> "popups cleared after idle" catches a stuck banner. Toasts never hold back
> `waitForSpinClickable`, since they take no input.

> ⚠ **`autoSpinsRemaining` / `freeSpinsRemaining` are what the player sees**, not
> game state. Game state comes from the BET response; resume state from `pending`.
> The production auto counter starts at `-1` ("never set") — reported as `0`.

> **`busyOps` labels:** `transitionToHoldSpin` · `transitionToBaseGame` ·
> `triggerBonus` · `highlightCoinSymbols` · `persistenceRestart` · `bigWin.show` ·
> `totalWin.show`. `persistenceRestart` is the one that matters most after a
> bonus: the client fires it without awaiting and goes `idle` while it plays.

**No `appState`** — not portable across games.

---

## 6. Optional getters

Missing → the dependent scenario is **unreached** (*inconclusive*), never a false
pass. All are present on this build.

| Function | Returns | Use |
|---|---|---|
| `getBetAmount()` | `number` | the stake the next spin costs — the **value**, not an index, not a formatted string |
| `getBetIndex()` | `number` | index into `getDenominations()`, `0` = lowest — the index `bet:row:<i>` addresses |
| `getDenominations()` | `number[]` | the configured ladder, ascending (a copy) |
| `getBalance()` | `number` | the balance the client holds now. ⚠ **displayed**, not the authority — the post-round balance is in the BET response. Between click and response it is already debited locally |
| `getSpeedMode()` | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` | exact "Turbo engaged" check |
| `isSoundEnabled()` / `isMusicEnabled()` | `boolean` | prove a mute toggle took effect; reported independently |
| `getTutorialRect()` | `ButtonRect \| null` | where to aim the mouse wheel for the help WebView |
| `scrollBetRowIntoView(i)` | `void` | reach a denomination row below the fold |
| `getHistoryRows()` | `HistoryRow[]` | what History lists right now, and where to click a row |
| `getHistoryDetailState()` | `HistoryDetailState` | page a History detail ticket |
| `getMultiplierLevels()` | `MultiplierLevels \| null` | multipliers vs config — `null` on this build |
| `dismissPopups()` | `Promise<string[]>` | teardown between runs |
| `startEventCapture(l)` / `endEventCapture(l)` | `void` / `BridgeEvent[]` | prove something too brief to poll happened |
| `setForcedResult(r)` / `setDevModeResult(grid)` | `void` | force the next single spin |

### `getSpeedMode()`

The turbo pair is named for the **icon**, not the state: `offTurboButton.isChecked`
means turbo **is on**. This mirrors the game's own
`offTurboButton.isChecked || _isHoldSpinButton` — holding spin engages turbo too.
`'UNKNOWN'` before the scene is up; treat it as unanswerable, not a failure.

### `isSoundEnabled()` / `isMusicEnabled()`

⚠ **The main-screen `sound` button toggles BOTH** — its handler calls
`onClickOnOffSound` and `onClickOnOffMusic` together. To test them independently,
use `settings:sound` and `settings:music`.

### `getTutorialRect()`

The rect of the **WebView itself** (`InformationPopup.webView`), not the popup —
size each wheel step from `height`. `null` when the info popup is closed.

> ⚠ **The bridge cannot scroll it, and neither can injected JS** — the guide is
> served cross-origin from `staticUrl`. Only a real `page.mouse.wheel` reaches it,
> and scroll position is unreadable, so wheel a generous fixed number of steps.

```ts
const t = await bridge.getTutorialRect();
if (!t) return unreached('tutorial not open');
for (let i = 0; i < 12; i++) {
    await page.mouse.move(t.centerX, t.centerY);
    await page.mouse.wheel(0, t.height * 0.9);
}
```

> ⚠ The client **destroys** the info popup whenever a message popup is shown
> (`PopupManager.hideHelp()`), so an error popup mid-scroll closes the tutorial.

### `scrollBetRowIntoView(index)`

Takes the **denomination index** (`0` = lowest) and centres that row in the list's
window (rows are named by index, so this is independent of display order — the
list shows the ladder highest-first). Returns nothing, never throws; dwell ~150 ms
after it (the scroll animates over 100 ms), then `waitForButtonClickable('bet:row:<i>')`.

### `getHistoryRows()`

```ts
{ index: number, rect: ButtonRect | null, clickable: boolean, data: Record<string, string> }[]
```

**Only rows actually rendered** — History is virtualised. `rect` is in
`getButtonRect` space; `clickable` is mask-aware and hit-tested. `data` keys, each
present only when that label is shown: `time` · `date` · `betId` · `betAmount` ·
`payout` · `freeSpin` · `anteBet` · `promoType`.

### `getHistoryDetailState()`

```ts
{ open: boolean, betIndex: number, betCount: number, hasPrev: boolean, hasNext: boolean }
```

The ticket is a **two-view** popup, not a per-bet pager:

| | view 0 — `view-normal` | view 1 — `view-respin` |
|---|---|---|
| shows | the triggering spin | the list of Hold & Spin respins |
| `betIndex` | `0` | `1` |
| `hasNext` | `true` when the round has respins (Next lives on this view) | `false` |
| `hasPrev` | `false` | `true` — **except** buy-bonus and free-spin rounds, where Prev **closes the ticket** instead of paging back |

`betCount` is the number of bets in the round. **Loop on `hasNext` / `hasPrev`**;
never do arithmetic over `betCount`. Buy-bonus rounds open directly on view 1.

### `getMultiplierLevels()`

`null` on this build — the payload carries no `multiplierLevel`. `null` means
*inconclusive*. If the server starts sending `multiplierLevel`, this returns
`{ baseGame, freeGame }` with no code change.

### `dismissPopups()`

Closes every open popup (and toast) via its **real close path** and returns only
the labels it **observed** close, using the `visiblePopups` names. Safe when
nothing is open; call it without checking. Anything left up is `console.warn`ed
and omitted from the return.

Each close gets a settle window before it is judged — **600 ms** normally (popups
fade over a 0.2 s tween), **4 s** for `bigWin` / `totalWin` (a skipped
celebration still plays its exit). Plan for the call to take that long.

What it does for each popup, and why:

| Label | Closed by | Why not the obvious call |
|---|---|---|
| `bigWin` / `totalWin` | `skip()` | `hide()` strands the promise the result chain awaits; the game never idles. ⚠ `bigWin.skip()` is a no-op during its first 0.8 s and when turbo is disallowed — it may report stuck |
| `notification` | its **OK** button (`clickYes`), else **No**, else `hideMessage()` | the OK action is what takes the client out of `error` (`recoverStuckSpin()` + refetch). Hiding it leaves spin disabled for the next run |
| `settings` | `hideCustomPopup` **+ release the input lock** | `SettingPopup.hide()` never releases the lock its `show()` took |
| `buyBonus` | `BuyBousPopup.close()` | it is scene-owned, not PopupManager-owned |
| `turboNotify` / `autoNotify` | `hideImmediate()` | the game's own "cut the banner short" |
| everything else | `PopupManager.hideCustomPopup(name)` / `hideDisconnect()` / promo `hidePopupReceive` + `hidePopupPromos` | |

> ⚠ **Pressing OK on a message popup is what a player would do — including on
> errors whose OK returns to the lobby** (session timeout, lobby-class error codes,
> "session ended"). On those, `dismissPopups()` navigates the page away. That
> session is dead either way; reload rather than dismiss.

### `startEventCapture(label)` / `endEventCapture(label)`

Labelled, independent recording windows. `endEventCapture` returns `[]` rather
than throwing for an unknown label.

| Event `type` | `data` | Emitted when |
|---|---|---|
| `popup_shown` | `{ popup }` | a popup or toast became visible |
| `popup_dismissed` | `{ popup }` | a popup or toast stopped being visible |
| `bet_response` | `{ seq }` | a round response entered the buffer |
| `busy_start` / `busy_end` | `{ op }` | a tracked presentation began / finished |

`t` is ms since that window opened. The popup watcher polls every 50 ms and runs
**only while a capture is open**.

```ts
await bridge.startEventCapture('turbo-toggle');
await page.mouse.click(t.centerX, t.centerY);
const events = await bridge.endEventCapture('turbo-toggle');
// → [{ t: 212.4, type: 'popup_shown', data: { popup: 'turboNotify' } }, …]
```

### `setForcedResult(result | null)` / `setDevModeResult(grid)`

Both go through the client's dev-mode queue (`GlobalContext.getDevModeManager()`).
`SlotGameApplication.requestBet` checks that queue **first**: with an entry queued
it never contacts the server, assigns the entry as the round result and goes to
`result` — so the forced round reaches `awaitBetResponse()` exactly like a wire
response.

| Call | Effect |
|---|---|
| `setForcedResult(inner)` | queue this **inner** result object (the shape of `response.result`) for the next spin. Any follow-up entries the dev manager auto-generated for that round are dropped, so exactly one spin is forced |
| `setForcedResult(null)` | clear the **whole** queue — including entries queued from the dev-mode UI |
| `setDevModeResult(grid)` | queue a 3×3 grid; the dev manager computes paylines and payout for it. `grid[col][row]`, col `0` = left reel, row `0` = top. Throws unless it is 3 columns of 3 |

> ⚠ **A forced round never reaches the server** — the server balance is not
> debited and History will not list it. Use forced rounds for presentation/UI
> cases, not for balance conservation or History cases.

> ⚠ **One round only.** A Hold & Spin sequence or a Buy Bonus is
> server-authoritative. (`setDevModeResult` with a grid that triggers Hold & Spin
> may make the dev manager queue follow-up respins of its own; those are served on
> the next spins.)

---

## 7. Diagnostics

| Member | Returns | Use |
|---|---|---|
| `diagnose()` | `object` | **first**, on any hang — see below |
| `getBetLog()` | `readonly BetRecord[]` | every response seen, raw. Does **not** move the cursor |
| `getUnconsumedBetCount()` | `number` | should be `0` between rounds |
| `isGameInfoCaptured()` | `boolean` | `false` explains a missed resume |
| `getAllButtonRects()` | `Record<string, ButtonRect>` | everything resolvable right now |
| `isButtonClickable(name)` | `boolean` | synchronous counterpart to `waitForButtonClickable` |
| `isPopupVisible(label)` | `boolean` | one popup or toast, by label |
| `getFreeSpinsRemaining()` / `getAutoSpinsRemaining()` | `number` | the snapshot's display values |
| `hitTestDebug` | `boolean` field | `true` → `console.warn` the blocker on every hit-test refusal. Off by default |
| `logBetResponses` | `boolean` field | one console line per round. On by default |
| `dispose()` | `void` | detach the tap, the setter hooks and every timer |

`diagnose()` returns:

| Key | Means |
|---|---|
| `version` · `appState` · `gameStarted` | where the client is |
| `gameInfoCaptured` · `gameInfoSource` | `'raw'` / `'live'` / `null` — see §3 |
| `rawHooksInstalled` | both `GameLogic` setter hooks are in; if `false`, responses are post-mutation copies |
| `rawResponsesRecorded` | how many buffered rounds came from the raw hook (should equal `betLogLength`) |
| `sceneReady` · `busyHooksPatched` · `spinHandlerAttached` | readiness plumbing |
| `betLogLength` · `betCursor` · `unconsumed` | the response buffer |
| `snapshot` · `buttons` · `spinClickable` | the surface, every rect, and why spin is / is not clickable |

```js
copy(JSON.stringify(window.__gameTestBridge.diagnose(), null, 2))
```

---

## 8. Types

```ts
interface ButtonRect {
    x: number; y: number; width: number; height: number;
    centerX: number; centerY: number;    // → page.mouse.click
    visible: boolean;                    // centre on-canvas AND a camera renders it
    interactable: boolean;
}

interface GameSnapshot {
    visiblePopups: string[];  visibleNotifications: string[];
    isAutoPlay: boolean;  balance: number;  betAmount: number;  betIndex: number;
    denominations: number[];
    autoSpinsRemaining: number;  freeSpinsRemaining: number;
    sceneReady: boolean;  busyOps: string[];  isFreeSpinMode: boolean;
    buyBonusPopupLoaded: boolean;  freeSpinPackageCount: number;  buyBonusSelectedPrice: number;
}

interface HistoryRow { index: number; rect: ButtonRect | null; clickable: boolean; data: Record<string, string>; }
interface HistoryDetailState { open: boolean; betIndex: number; betCount: number; hasPrev: boolean; hasNext: boolean; }
interface MultiplierLevels { baseGame: number[]; freeGame: number[]; }
interface BetRecord { seq: number; t: number; result: unknown; }
interface CanDoResult { ok: boolean; reason?: string; }
interface BridgeEvent { t: number; type: string; data: Record<string, unknown>; }
type SpeedMode = 'NORMAL' | 'TURBO' | 'UNKNOWN';
```

`ButtonRect` is in page-CSS pixels — origin at the viewport's top-left, canvas
offset included, already divided by `devicePixelRatio`. The rect is the node's
**own** rect (children excluded). When `visible` is `false` the numbers may be
`NaN` — do not click it.

---

## 9. Button name registry

Addressed by **name** — this game's own names; map your roles onto them in the
profile's `BUTTONS` table. Popup-scoped names resolve **only while their popup is
open**.

> **The rule:** a control resolves only when clicking it would actually do its
> job. Hidden, gated, or its popup closed → `getButtonRect` is `null` and
> `waitForButtonClickable` keeps waiting until its timeout. Not a name this build
> knows → `null`, and `waitForButtonClickable` rejects at once.

### Main screen

| Name | Suggested role | Does | Resolves when |
|---|---|---|---|
| `spin` | `spin` | send one bet | whenever the scene is up; **interactable** only with the PLAY face showing, the spin toggle armed, and the spin lock off. ⚠ an insufficient balance does **not** block it |
| `stop` | `stop` | cut the current spin's reels short | only while the STOP face shows (a spin presenting); interactable unless locked, in a bonus, or turbo is disallowed (promo `allowTurbo: false`, compliance `turboMode: false`) |
| `autoOrFreeSpin` | `auto_or_freespin` | the counter control that **replaces** spin during autoplay / Hold & Spin — fast-stops the current reels | while that control is shown. ⚠ it does **not** end an autoplay run |
| `auto` | `auto` | open the autoplay panel | only when **no** run is going, and autoplay is allowed by compliance |
| `stopAuto` | `auto_stop_main` | stop a running autoplay from the main screen | only **while a run is going** |
| `buyBonus` | `buy_bonus` | open the Buy Bonus panel | idle base game, compliance `bonusBuyAllowed` — hidden during Hold & Spin |
| `nextBet` / `previousBet` | `stake_up` / `stake_down` | **both open the denomination list** (`HAS_BET_POPUP = true`) — neither steps the bet inline | idle, not during autoplay or a promo. ⚠ see below |
| `turbo` | `turbo` | toggle fast play — resolves the visible, **unchecked** member of the on/off pair, the one whose click changes state | when turbo is togglable (not in a promo that forbids it, not compliance-hidden) |
| `sound` | `sound` | toggle **sound AND music together** — resolves whichever of the on/off pair is shown | any time |
| `settings` · `info` · `history` · `trophy` | `settings` · `info` · `history` · `trophy` | open the matching panel (`trophy` = Monumental Wins) | idle |

> ⚠ **`nextBet` is disabled at the top of the reachable ladder, `previousBet` at
> the bottom** — even though both only open the list. "Top" is balance-aware:
> `nextBet` disables when the next denomination exceeds the balance. To open the
> list from either end, use whichever of the two is enabled.

Not in this build (always `null`): `anteBet`, `devMode`, `startButton`.

### Popup-scoped names

| Prefix | Name | Does / notes |
|---|---|---|
| `auto:` | `count:{i}` (alias `package:{i}`) | pick spin count `i` of **`[10, 20, 30, 50, ∞]`**. ⚠ the panel opens on `10` (index 0), so selecting 0 is a no-op — detour via another index. ⚠ picks are ignored for 0.5 s after the previous one (the check-mark tween); dwell between clicks. `null` if the panel was opened during a run |
| | `confirm` | start the run and close the panel. Only shown when no run is going |
| | `stop` | stop the run from inside the panel. Only shown if the panel opened during a run — unreachable in practice, because `auto` does not resolve during a run. Use `stopAuto` |
| | `next` / `prev` | step the **stake** up/down inside the panel (it calls `setStake` — this changes the real bet) |
| | `stopAtWin` | arm "stop on any win" |
| | `close` (alias `cancel`) | dismiss without starting. ⚠ this panel has **no close X** — its only dismiss is the full-screen tap-outside backdrop, so this rect is a strip along the **top 6 %** of that backdrop, where no panel sits |
| `buyBonus:` | `package:{0,1,2}` | pick one of three **stakes** (the packages are stake shortcuts, not price tiers). Hidden while the confirm sheet is up |
| | `next` / `prev` | step the stake inside the panel |
| | `buy` | open the confirm sheet (does **not** purchase) |
| | `confirm` | purchase and close — only while the confirm sheet is up. Balance is checked here; short funds show `not_enough_money` |
| | `cancel` | close the confirm sheet — only while it is up |
| | `close` | close the panel |
| `bet:` | `row:{i}` | select denomination index `i` (`0` = lowest). May be below the fold — `scrollBetRowIntoView(i)` first. The current row is non-interactable |
| | `plus` / `minus` | step the selection within the open list |
| | `close` | close, keeping the selection. (The tap-outside backdrop shares this handler; the bridge picks the small X) |
| `settings:` | `turbo` · `music` · `sound` | toggle each **independently** (alias `turboTab` → `turbo`). `turbo` is disabled when turbo is not allowed |
| | `history` · `info` | ⚠ close settings **and** open that panel |
| | `gameLimit` | close settings and open game limits |
| | `close` | close settings (also releases the input lock) |
| `notification:` | `yes` · `no` · `close` | the message popup's buttons — present only when that popup shows them (`yes` is the usual OK / Continue) |
| `info:` | `close` | close the help / paytable |
| `trophy:` | `close` | close Monumental Wins |
| `history:` | `row:{i}` | a **rendered** History row by data index — read `getHistoryRows()` first |
| | `close` | close History |
| `historyDetail:` | `next` | go to the respin list (only while `hasNext`) |
| | `prev` | back to the triggering spin — **or close the ticket** on buy-bonus / free-spin rounds (see §6) |
| | `close` | close the ticket |

`bet:row:{i}` and `auto:count:{i}` are indexed **by position**, never by label
text.

### Accepted aliases

| Alias | Resolves to |
|---|---|
| `stakeIncrease` / `stakeDecrease` | `nextBet` / `previousBet` |
| `auto:cancel` | `auto:close` |
| `auto:package:{i}` | `auto:count:{i}` |
| `message:ok` / `message:close` | `notification:yes` / `notification:close` |
| `settings:turboTab` | `settings:turbo` |

> ⚠ **Stopping a run must actually stop it.** In this game the only main-screen
> control that ends a run is **`stopAuto`**. `autoOrFreeSpin` only speeds up the
> current reels, and `auto` does not resolve during a run. After clicking
> `stopAuto`, expect `isAutoPlay` to go `false` and no further BET. The round
> already on the wire still completes and still arrives through
> `awaitBetResponse`.

> ⚠ **Autoplay pacing** is the client's presentation, not a round trip — tens of
> seconds behind a big win. Every round still arrives through `awaitBetResponse`,
> in order.

---

## 10. Popup label registry

Used by `getSnapshot().visiblePopups`, `visibleNotifications`,
`isPopupVisible(label)`, `dismissPopups()` and the event-capture `popup` field —
one catalogue, so they can never disagree. Listed in `visiblePopups` order:
modals front-most first, then toasts.

| Label | Kind | Popup |
|---|---|---|
| `bigWin` | modal | big / mega / ultra win celebration |
| `totalWin` | modal | Hold & Spin total-win result |
| `disconnect` | modal | reconnecting / connection-lost |
| `notification` | modal | message popup — errors, `not_enough_money`, reality check, session ended |
| `buyBonus` | modal | Buy Bonus panel (its confirm sheet included) |
| `auto` | modal | autoplay panel |
| `bet` | modal | denomination list |
| `settings` | modal | settings |
| `historyDetail` | modal | History detail ticket (opens over the list) |
| `history` | modal | History list |
| `info` | modal | help / paytable (WebView) |
| `trophy` | modal | Monumental Wins |
| `gameLimit` | modal | game limits |
| `promo` | modal | promo receive / list / message dialogs |
| `turboNotify` | **toast** | the turbo on / off banner (`GameplayScene.notifyTurbo`) |
| `autoNotify` | **toast** | the autoplay on / off banner (`GameplayScene.notifyAuto`) |

Not reported: `replay` (REPLAY mode only, never driven by the suite).

---

## 11. The BET response, as this bridge delivers it

The bridge returns the **inner** result object (`response.result`), captured
**before the client mutates it**. That matters here, because the client's
`betResult` setter rewrites the object in place:

| Field | Server sends | Client rewrites to (and a naive tap would hand you) |
|---|---|---|
| `result.views[].value[]` | may contain **`-1`** placeholders | random display symbols |
| `paylines[].symbols` | as sent | re-derived from the (rewritten) grid |
| `balance` | as sent (may be `< 0` = "not supplied") | `localBalance + winlose` |

So assert on what the server said: a `-1` in `views` is real payload.

Fields the framework reduces a round to (type: `BetResult` in
[`assets/src/constants/Interface.ts`](assets/src/constants/Interface.ts)):

| Field | Notes |
|---|---|
| `stake` | **`0` on every Hold & Spin respin** |
| `payout` · `rawPayout` · `winlose` · `balance` | `balance` is authoritative — never recompute it |
| `freeSpinInfo` | `{ freespins, totalFreespin, accumulateWin }` or `null`. `freespins` is **REMAINING** respins, not awarded |
| `coinCount` · `coinPositions[]` | `{ row, col, coinValue, multiplier }` — 9 coins ends the bonus immediately |
| `paylines[]` | `{ symbols, payline, payoutRate, winType }` |
| `accumulatedWinAmount` · `hasReachedMaxWin` | max win truncates a bonus with respins left |
| `gameState` | present when the server sends it (`1` base · `2` free · `3` buy bonus · `4` promo) |
| `promoResult` | during promo play |
| `id` · `refId` · `roundId` · `betTime` · `processedTime` · `settledTime` | identity / timing |

---

## 12. What the bridge deliberately does NOT do

**Drive the game to a deterministic start.** `startGame()` resolves at the click.

**Decide when a round is over.** `waitForSpinClickable` reports readiness; you
decide what it means.

**Expose round outcomes.** No payout, win tier, grid or Hold & Spin getters —
every one is readable off the raw response, and a bridge that re-derived them
would be a second source of truth.

**Scroll a cross-origin WebView.** It says *where*; `page.mouse.wheel` does the
scrolling.

**Hold the spin button.** Hold-to-spin cases drive a real
`mouse.down` → wait → `mouse.up`. There is no `holdSpinStart()`.

---

## 13. Differences from Zeus v3

For anyone porting a Zeus profile or suite:

| | Zeus v3 | Dragon v3 |
|---|---|---|
| response payload | read at `RESULT`, after the client rewrote it | captured **before** the client touches it (§11) |
| `getGameInfo()` | copy of the stored object | copy of the **server's** payload (`gameInfoSource: 'raw'`) |
| `settings.defaultBetLevel` | 0-based index (contract fixture) | **client treats it as 1-based** (§3) |
| toasts | not reported | `turboNotify` / `autoNotify` in `visiblePopups` + `visibleNotifications` |
| unlimited autoplay | large positive count | `Infinity` — `isAutoPlay` stays `true`, `autoSpinsRemaining` is `Infinity` |
| hidden control | `{ visible: false }` | **`null`** |
| `stop` | the stop Toggle | the spin touch node while its STOP face shows |
| `auto` during a run | resolves | `null` — use `stopAuto` |
| `stopAuto` with no run | resolves | `null` |
| auto-stop fallback | `stopAuto` → `autoOrFreeSpin` → `auto` | **`stopAuto` only** — `autoOrFreeSpin` does not stop a run |
| auto panel | `auto:package:{i}`, `auto:cancel` | `auto:count:{i}` (alias `package`), `auto:close` = backdrop strip (alias `cancel`) |
| `settings:*` rows | the Toggle fields | the tab Buttons, by handler; adds `history` · `info` · `gameLimit` |
| Buy Bonus `confirm` / `cancel` | always resolve while the panel is open | only while the confirm sheet is up |
| `buyBonusSelectedPrice` | price of the checked package | stake × package price |
| `notification` dismiss | `hideMessage()` | presses **OK** (§6) |
| `dismissPopups()` settle | one frame | 600 ms / 4 s celebrations |
| `waitForSpinClickable` | — | also refuses `hold_spin_running`; ignores toasts |
| `busyOps` | — | adds `persistenceRestart` |
| help popup | `BPopupName.HELP` | `BPopupName.INFORMATION` |
| History detail | `PopupName.HISTORY_DETAIL` | registered as `'HistoryDetail'`; Prev can close the ticket |
| `devMode` button / popup | present | absent |
| `trophy:close` | — | present |
| `luckySpin` · `floatPaytable` · `replay` labels | reported | absent |

---

## 14. Checking a build

From the QA framework repo (a `games/<dragon>` profile is needed first — start
from Zeus and apply §13):

```bash
python scripts/probe_bridge.py --game <dragon> --env dev
```

It prints the REQUIRED-API check, the snapshot, whether `waitForSpinClickable`
resolves and how fast, the buttons the build exposes, and profile-vs-reality.

First things to confirm on a new build:

1. `diagnose().rawHooksInstalled === true` and `gameInfoSource === 'raw'`.
2. `turbo`, `auto`, `stopAuto` resolve to the controls actually on screen — the
   on/off toggle pairs are the least certain part of the registry.
3. A resumed Hold & Spin (`pending.freeSpins > 0`) plays out through
   `awaitFreespinRound` and `waitForSpinClickable` then resolves.
4. A big win: `waitForSpinClickable` rides out `bigWin.show` and
   `persistenceRestart`.

From the browser console:

```js
copy(JSON.stringify(window.__gameTestBridge.diagnose(), null, 2))
```
