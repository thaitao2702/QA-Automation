# Treasures of the Dragon VIP — Bridge API

Reference for `window.__gameTestBridge` in Treasures of the Dragon VIP: every public function, every
control name, every utility, and the flows that need more than one click. Use it to write this game's
profile and its scenarios.

- Contract the bridge follows: `game-bridge-contract.md` (the function surface, boot sequence and data rules).
- Source: `assets/src/test/DragonVipBridgeV3.ts`, typed surface in `assets/src/test/IBridgeV3.ts`.
- Version string: `dragon-vip-bridge-v3/2026-09-14` (read it from `bridge.version`).

---

## 1. Getting the bridge

| | |
|---|---|
| Enable | Launch the game with `?bridge=1` (query string, or a query inside the hash). Without it nothing is installed. |
| Handle | `window.__gameTestBridge` |
| Install failure | `window.__gameTestBridgeError` holds the stack. A bridge that failed to install is different from one that was never requested. |
| When it appears | While the game is loading, before the game-info response lands. It is up before the Start button shows. |

```js
await page.waitForFunction(() => window.__gameTestBridge);
const version = await page.evaluate(() => window.__gameTestBridge.version);
```

**Coordinates.** Every rect (`ButtonRect`) is in CSS page pixels, the space `page.mouse.click(x, y)`
takes. The bridge never clicks for you: take `centerX` / `centerY` and issue a real mouse click.

```ts
interface ButtonRect {
  x: number; y: number; width: number; height: number;
  centerX: number; centerY: number;   // aim here
  visible: boolean;                   // false → numbers may be NaN; do not click
  interactable: boolean;
}
```

**Errors.**

| Call | Rejects with |
|---|---|
| `startGame`, `waitForSpinClickable`, `waitForButtonClickable` | `Error`. The message names the last blocking reason and includes a one-line snapshot. |
| `awaitBetResponse`, `awaitFreespinRound` | A plain object `{ reason: 'bet_response_timeout', snapshot }`, **not** an `Error`. |
| Every getter | Never throws. It returns `null`, `[]`, `0` or `'UNKNOWN'` instead. |

---

## 2. Boot sequence

```js
const b = () => window.__gameTestBridge;

await page.evaluate(() => b().startGame());          // resolves at the Start click
const info = await page.evaluate(() => b().getGameInfo());
const pending = info.pending ?? info.result?.pending;

if (pending && (pending.freeSpins > 0 || (pending.gameState ?? 1) !== 1)) {
  // resumed Hold & Spin: the client plays it by itself
  for (;;) {
    const r = await page.evaluate(() => b().awaitFreespinRound());
    if ((r.freeSpinInfo?.freespins ?? 0) === 0) break;   // see §7 for the terminal rule
  }
}
await page.evaluate(() => b().waitForSpinClickable());   // clean, spinnable base
```

- `startGame()` does not wait for idle, does not play the resume, and does not dismiss popups. Your
  framework does all three.
- `getGameInfo()` is the payload captured **before** Start. Its `pending` is still intact even though
  the resume has begun.

---

## 3. Required functions

### `startGame(): Promise<void>`
- **Does:** clicks Start and resolves the moment the click is issued.
- **Repeat calls:** once the game has started, it resolves immediately, including during the short gap
  before the loading screen hides. Calling it twice never clicks Start twice.
- **Before Start exists:** if you call it while the game is still loading, it waits for the Start
  handler and rejects after **60 s** if it never appears.
- **Replay mode (`?mode=replay`):** always resolves immediately.

### `getGameInfo(): unknown`
- **Returns:** a deep copy of the launch payload as the server sent it: the inner `result` object with
  `settings`, `pending`, `lastBet`, `balance`, `currency`, `freeSpinPackages` and `paytable`.
- **Rules:** it is never re-queried and never mutated. Every call returns the same data.
- **`isGameInfoCaptured(): boolean`:** `false` means no pre-Start copy was taken, so the function is
  returning a live read. That explains a missed resume.

> ⚠ **In this game `settings.defaultBetLevel` is 1-based.** The client opens a first session at
> `denominations[defaultBetLevel - 1]`. Returning sessions open at `lastBet.baseStake`.

### `getSnapshot(): GameSnapshot`
Synchronous, cheap, safe at any time. See §5 for every field.

### `getButtonRect(name): ButtonRect | null`
- **Returns:** where the control is **right now**, without waiting.
- **Returns `null` when:**
  - the name is not a control of this build, or
  - the control cannot do its job right now (hidden, its popup closed, or the feature already on).
- **Never throws.** Use it to probe whether an optional feature exists.

### `waitForButtonClickable(name, stableMs = 250, timeoutMs = 30000): Promise<ButtonRect>`
- **Resolves** once the control has stayed clickable for `stableMs`, with its rect read again at
  resolve time. "Clickable" means:
  - it resolves;
  - it is visible;
  - it is interactable;
  - it is not covered by another layer;
  - it is not clipped by a scroll mask.
- **Rejects at once** for a name this build or session has no concept of. For example,
  `buyBonus:anteBet` in a session without Ante Bet (see §6.6).
- **Rejects on timeout** with the last blocking reason (§8).

### `waitForSpinClickable(stableMs = 250, timeoutMs = 60000): Promise<ButtonRect>`
The **only** readiness authority. It is used for three things:
1. before every spin;
2. as the settle signal after a round;
3. as the boot idle check.

It resolves only when all of these hold, checked in this order (the first failing one is the reason):

| # | Condition | Reason when it fails |
|---|---|---|
| 1 | Scene started and input handlers attached | `scene_not_ready` |
| 2 | App state is idle | `state:betting`, `state:result`, … |
| 3 | Not in autoplay | `autoplay_running` |
| 4 | Not in Hold & Spin | `free_spins_running` |
| 5 | Spin button not being held (hold-for-turbo) | `hold_spin_running` |
| 6 | No tracked animation running | `busy:<ops>` |
| 7 | No modal popup open (toasts do not count) | `popup_open:<label>` |
| 8 | No popup holding the input lock | `input_locked` |
| 9 | Spin clickable: IDLE state, NORMAL mode, interactable, hit-test passes | `button_disabled`, `blocked_by:<node>`, … |

An insufficient balance does **not** block it. The client shows its out-of-money popup on click.

### `awaitBetResponse(timeoutMs = 15000): Promise<unknown>`
- **Returns:** the next **raw** round response (the inner `result`), copied before the client touched it.
- **Race-safe:** a response that landed between your click and this call is handed over, not skipped.
- **Order:** it consumes exactly one response per call, in arrival order.
- **Covers:** normal spins, buy-bonus purchases, autoplay rounds and dev-mode forced results.

### `awaitFreespinRound(timeoutMs = 90000): Promise<unknown>`
- **Same mechanics** as `awaitBetResponse`: same shared cursor, a longer default timeout.
- **Passive:** it never clicks. Use it for Hold & Spin rounds the client fires itself, including a
  resume at boot.

---

## 4. Diagnostic: `canDo(action): { ok, reason? }`

Diagnostic only. Never gate a test on it, because readiness is decided by the two `waitFor…` calls. Its
value is turning a timeout into a named cause.

| Action | Checks |
|---|---|
| `spin` | main screen free (idle, no bonus/auto/busy/modal) + `spin` clickable |
| `stopSpin` | not idle + `stop` clickable |
| `nextBet` · `previousBet` · `openBetPopup` | main screen free + stake button clickable (these OPEN the list) |
| `selectBet` | `bet` popup open |
| `openAutoPopup` | main screen free + `auto` clickable |
| `selectAutoSpinPackage` · `toggleAutoSpinStopAtWin` · `confirmAutoSpin` | `auto` popup open + the control clickable |
| `stopAutoSpin` | autoplay running + `stopAuto` clickable |
| `openBuyBonusPopup` | main screen free, no feature on + `buyBonus` clickable |
| `selectBuyBonusPackage` · `confirmBuyBonus` | `buyBonus` popup open + the control clickable |
| `activateAnteBet` | offered, not on, no Premium Reel on → **next step** clickable (`buyBonus`, or `buyBonus:anteBet` once the panel is open) |
| `deactivateAnteBet` | Ante on + main screen free + `anteBet:off` clickable |
| `activatePremiumReels:<reelId>` | offered, package exists, not on, no Ante → next step clickable |
| `deactivatePremiumReels` | Premium Reel on + main screen free + `premiumReels:off` clickable |
| `confirmFeature` | `buyBonus` popup open + `buyBonus:confirm` clickable |
| `setTurbo` · `setSound` | the control clickable |
| `openSettings` · `openInfo` · `openHistory` · `openTrophy` | main screen free + the control clickable |

Any other action returns `unknown_action:<action>`.

---

## 5. `getSnapshot()` fields

| Field | Type | Role | Meaning |
|---|---|---|---|
| `visiblePopups` | `string[]` | asserted | Every popup on screen, **front-most first**, toasts last. `[]` when none. Labels in §6.8. |
| `visibleNotifications` | `string[]` | asserted | The toast subset: `turboNotify`, `autoNotify`, `anteNotify`. |
| `isAutoPlay` | `boolean` | asserted | An autoplay run is on, including the last spin's presentation. |
| `balance` | `number` | asserted | Client balance. |
| `betAmount` | `number` | asserted | The selected **base** stake (the denomination at `betIndex`). |
| `betIndex` | `number` | asserted | 0-based index into `denominations`. The same index as `bet:row:<i>`. |
| `denominations` | `number[]` | asserted | The stake ladder, ascending. |
| `autoSpinsRemaining` | `number` | UI value | Auto counter on screen. `0` = no run, `Infinity` = unlimited. |
| `freeSpinsRemaining` | `number` | UI value | Hold & Spin counter on screen. |
| `sceneReady` | `boolean` | diagnostic | Game started and spin handlers attached. |
| `busyOps` | `string[]` | diagnostic | Tracked animations: `transitionToHoldSpin`, `transitionToBaseGame`, `triggerBonus`, `highlightCoinSymbols`, `persistenceRestart`, `bigWin.show`, `totalWin.show`, `anteBet.animation`, `premiumReels.animation`. |
| `isFreeSpinMode` | `boolean` | diagnostic | In Hold & Spin, or transitioning out of it. |
| `buyBonusPopupLoaded` | `boolean` | extra | The feature panel has been instantiated (it loads lazily). |
| `freeSpinPackageCount` | `number` | extra | `gameInfo.freeSpinPackages.length`. |
| `buyBonusSelectedPrice` | `number` | extra | Panel open: the confirm sheet's price, or stake × package price before a row is picked. `0` when closed. |
| `isAnteBet` | `boolean` | extra | Ante Bet is on for the next spin. |
| `anteBetAvailable` | `boolean` | extra | This session offers Ante Bet (`settings.anteMultiplier` set). |
| `anteMultiplier` | `number` | extra | `settings.anteMultiplier`, or `0`. |
| `isPremiumReel` | `boolean` | extra | A Premium Reel package is on and will be sent with the next bet. |
| `premiumReelAvailable` | `boolean` | extra | This session offers Premium Reels (`settings.premiumReel` enabled with options). |
| `premiumReelId` | `number` | extra | Active package `reelId` (opaque: `2`, `3`, `23`, …). `0` when none. |
| `premiumReelMultiplier` | `number` | extra | Active package multiplier, or `0`. |
| `spinCost` | `number` | extra | What the next spin debits: base stake × active feature multiplier, `0` during free spins. **A display value**: the BET response's `stake` is the authority. |

---

## 6. Controls

Every control name follows one rule: **a control resolves only when clicking it would do its job.**

| Situation | `getButtonRect` | `waitForButtonClickable` |
|---|---|---|
| Can do its job right now | rect | resolves |
| Exists, but not right now | `null` | keeps waiting, then times out |
| Not in this build / session | `null` | rejects at once |

### 6.1 Main screen

| Name | Does | Resolvable when |
|---|---|---|
| `spin` | Sends one bet. | Always resolves while on screen. `interactable` only when idle (IDLE state, NORMAL mode), no popup lock, not in Hold & Spin. Not blocked by balance. |
| `stop` | Cuts the current spin's reels short (same button, STOP state). | Only while the spin button shows STOP. Never shows in a no-turbo promo or when compliance disallows turbo. |
| `turbo` | Toggles speed mode and raises the `turboNotify` toast. Check the effect with `getSpeedMode()`. | Visible (compliance `turboMode`) and interactable. |
| `auto` | Idle: opens the autoplay panel. During a run: **stops the run**. | Visible (compliance `autoPlay`) and interactable. |
| `stopAuto` | Same button as `auto`, named for its stop job only. | Only while autoplay is running. |
| `stakeDecrease` / `stakeIncrease` | **Both open the denomination list** (neither steps the stake). | Idle, not in auto/bonus/promo. Decrease is disabled at `minBet`, increase at `maxBet`. |
| `buyBonus` | Opens the feature panel: Buy Bonus, Ante Bet and Premium Reels (§6.3). | Idle, not in auto/bonus/promo, compliance `bonusBuyAllowed`, and **no feature currently on**. |
| `history` · `info` · `settings` · `trophy` | Opens the matching panel. | Idle, not in auto/bonus. |
| `sound` | The visible one of the sound on/off pair. Clicking "on" turns **both** sound and music off; clicking "off" turns both on. | Always while visible. |

### 6.2 Aliases (accepted anywhere a name is)

| Alias | Resolves to |
|---|---|
| `nextBet` / `previousBet` | `stakeIncrease` / `stakeDecrease` |
| `monumentalWin` | `trophy` |
| `anteBet:on` | `buyBonus:anteBet` |
| `premiumReels:<reelId>` | `buyBonus:premiumReels:<reelId>` |
| `buyBonus:buy:<i>` | `buyBonus:package:<i>` |
| `auto:cancel` / `auto:back` | `auto:close` / `auto:prev` |
| `message:ok` / `message:cancel` / `message:close` | `notification:yes` / `:no` / `:close` |
| `settings:turboTab` | `settings:turbo` |

### 6.3 Feature panel: Buy Bonus, Ante Bet, Premium Reels

This build has **no dedicated Ante Bet or Premium Reel button** on the main screen. All three features
sit behind `buyBonus`, which opens a list of rows. Each row has its own action button, and a confirm
sheet follows. While Ante or a Premium Reel is on, the same main button turns it **off** instead of
opening the panel.

| Name | Does | Resolvable when |
|---|---|---|
| `buyBonus:package:<i>` | BUY row for `gameInfo.freeSpinPackages[i]` → opens the confirm sheet. | Panel open, sheet not shown. |
| `buyBonus:anteBet` | ACTIVATE row for Ante Bet → opens the confirm sheet. | Panel open, sheet not shown, Ante **off**, no Premium Reel on. |
| `buyBonus:premiumReels:<reelId>` | ACTIVATE row for that Premium Reel package → opens the confirm sheet. | Panel open, sheet not shown, Premium Reel **off**, Ante not on, `reelId` listed in `settings.premiumReel.options`. |
| `buyBonus:confirm` | Confirms: buys the package, or turns the feature on. Closes the panel. | Only while the confirm sheet is shown. |
| `buyBonus:cancel` | Back from the sheet to the list. | Only while the confirm sheet is shown. |
| `buyBonus:close` | Closes the panel. | Panel open, sheet not shown. |
| `buyBonus:next` / `buyBonus:prev` | Steps the panel's stake. **This changes the game's selected stake immediately.** | Panel open, sheet not shown. |
| `anteBet:off` | Turns Ante Bet off: the main `buyBonus` button, with no panel or confirm. | Ante on, and the button is interactable. |
| `premiumReels:off` | Turns the Premium Reel off: the same button, with no panel or confirm. | Premium Reel on (and Ante not on), and the button is interactable. |

Things to know about the panel:
- **Scrolling:** the list scrolls, so a row below the fold reports `clipped_by_mask`. Call
  `scrollBuyFeatureIntoView(name)` first, then wait on it.
- **Insufficient balance:** clicking a row whose price is above the balance shows a `notification`
  popup (out of money) **instead of** the confirm sheet.
- **Ante row with no Ante:** a session without Ante still renders an Ante row, but the bridge reports
  `buyBonus:anteBet` as absent there. Rely on the bridge, not the row's presence.
- **Mutual exclusion:** Ante Bet and a Premium Reel can never both be on (client integration doc §6.5).
  The panel also cannot be opened while either is on.

### 6.4 Autoplay panel (label `auto`)

| Name | Does | Resolvable when |
|---|---|---|
| `auto:count:<i>` | Selects a spin count: `0→10  1→20  2→30  3→50  4→∞`. The panel opens on 10. | Panel open and no run active. |
| `auto:stopAtWin` | Toggles "stop on any win" (a Toggle). | Panel open. |
| `auto:next` / `auto:prev` | Steps the stake **(changes the game's stake)**. | Panel open, not at the end of the ladder. |
| `auto:confirm` | Starts the run and closes the panel. | Panel open, no run active, panel not blocked (free spins / promo). |
| `auto:stop` | Stops a running auto from inside the panel. | Panel open during a run. |
| `auto:close` | Dismisses without starting. This panel has **no X**: the rect is the top strip of the full-screen backdrop. | Panel open. |

### 6.5 Denomination list (label `bet`)

| Name | Does | Resolvable when |
|---|---|---|
| `bet:row:<i>` | Selects `denominations[i]`, addressed by index, not by label. The currently selected row reports `interactable: false`. | List open. Use `scrollBetRowIntoView(i)` for rows below the fold. |
| `bet:plus` / `bet:minus` | Steps the selection within the list. | List open. |
| `bet:close` | Closes the list, keeping the selection. | List open. |

### 6.6 Other panels

| Name | Does |
|---|---|
| `settings:turbo` · `settings:music` · `settings:sound` | Toggle turbo / music / sound **independently** (unlike the main `sound` button). |
| `settings:history` · `settings:info` · `settings:gameLimit` | Open that panel and close settings. |
| `settings:close` | Close settings (also releases the input lock). |
| `notification:yes` · `notification:no` | The message popup's buttons, each present only when that message shows it. |
| `notification:close` | The circle close. ⚠ It runs the **cancel** callback, not a neutral dismiss. |
| `info:close` · `trophy:close` · `history:close` | Close that panel. |
| `history:row:<i>` | A rendered History row; clicking it opens the detail ticket. |
| `historyDetail:next` · `historyDetail:prev` · `historyDetail:close` | Page and close the detail ticket (see `getHistoryDetailState`). |

### 6.7 Session-scoped names

These reject at once in `waitForButtonClickable` when the session does not offer the feature. That
counts as *unreached*, not a failure.

| Name | Exists only when |
|---|---|
| `buyBonus:anteBet`, `anteBet:on`, `anteBet:off` | `settings.anteMultiplier` is set |
| `premiumReels:off` | `settings.premiumReel` is enabled with options |
| `buyBonus:premiumReels:<reelId>`, `premiumReels:<reelId>` | that `reelId` is in `settings.premiumReel.options` |

Before the game-info response has arrived, every name counts as known.

### 6.8 Popup labels (`visiblePopups` / `dismissPopups`)

Front-most first:

`bigWin` · `totalWin` · `disconnect` · `notification` · `buyBonus` · `auto` · `bet` · `settings` ·
`historyDetail` · `history` · `info` · `trophy` · `gameLimit` · `promo`

Toasts (listed last, never block input): `turboNotify` · `autoNotify` · `anteNotify`

---

## 7. Optional and utility functions

### Contract optional functions

| Function | Returns | Notes |
|---|---|---|
| `dismissPopups()` | `Promise<string[]>` | Closes every open popup through its **real** close path and returns only the labels it saw close. Celebrations are skipped rather than hidden, and get up to 4 s to exit; other popups get 0.6 s. A message popup is closed through its OK/No button, so its action runs. Logs anything stuck; releases the input lock when nothing is stuck. |
| `getBetAmount()` | `number` | Base stake. For the amount actually debited, use `snapshot.spinCost`. |
| `getBetIndex()` | `number` | 0-based index into `getDenominations()`. |
| `getDenominations()` | `number[]` | Ascending ladder. |
| `getBalance()` | `number` | Displayed balance. The BET response's `balance` is the authority. |
| `getSpeedMode()` | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` | `'UNKNOWN'` only before game logic exists. |
| `isSoundEnabled()` / `isMusicEnabled()` | `boolean` | Independent of each other. |
| `getTutorialRect()` | `ButtonRect \| null` | The info popup's WebView rect; `null` when closed. The bridge never scrolls it: use `page.mouse.wheel` at its centre. |
| `scrollBetRowIntoView(i)` | `void` | Centres denomination row `i` in the open list. Non-fatal. |
| `getHistoryRows()` | `HistoryRow[]` | Rendered rows only: `{ index, rect, clickable, data }`. `data` keys: `time`, `date`, `betId`, `betAmount`, `payout`, `freeSpin`, `anteBet`, `promoType` (only those shown). |
| `getHistoryDetailState()` | `{open, betIndex, betCount, hasPrev, hasNext}` | A two-view ticket: view 0 is the triggering spin, view 1 the respin list. `hasPrev` is false on buy-bonus / free-spin rounds, where Prev closes the ticket. Loop on the flags, not on `betCount`. |
| `getMultiplierLevels()` | `null` | This game sends no multiplier ladder. |
| `startEventCapture(label)` / `endEventCapture(label)` | `void` / `BridgeEvent[]` | Events: `popup_shown {popup}`, `popup_dismissed {popup}`, `bet_response {seq}`, `busy_start {op}`, `busy_end {op}`. `t` is ms since the window opened. An unknown label returns `[]`. |
| `setForcedResult(result \| null)` | `void` | Queues an inner BET result for **the next spin only**; `null` clears the queue. The spin never reaches the server, so balance and history reflect only what you forced. Throws if dev mode is unavailable. |
| `setDevModeResult(grid)` | `void` | Forces a 3×3 grid (`grid[col][row]`, col 0 = left reel, row 0 = top). Dev mode recomputes paylines and payout. Next spin only. |

### Game-specific extras

| Function | Returns | Notes |
|---|---|---|
| `getPremiumReelOptions()` | `{reelId, multiplier}[]` | From `settings.premiumReel.options`; `[]` when the feature is off. Treat `reelId` as opaque. |
| `getBuyFeatures()` | `BuyFeatureRow[]` | Panel rows as rendered: `{ index, id, kind: 'package'\|'anteBet'\|'premiumReels', control, packageIndex?, reelId?, priceMulti, price, rect, clickable }`. `control` is the name to wait on. `[]` when the panel is closed. |
| `scrollBuyFeatureIntoView(name)` | `void` | Scrolls the panel list so that control's row is in view. Non-fatal. |
| `isPopupVisible(label)` | `boolean` | One label from §6.8. |
| `getFreeSpinsRemaining()` / `getAutoSpinsRemaining()` | `number` | Same values as the snapshot fields. |

### Diagnostics

| Function | Returns | Notes |
|---|---|---|
| `getAllButtonRects()` | `Record<string, ButtonRect>` | Every name that resolves right now. |
| `isButtonClickable(name)` | `boolean` | The synchronous version of `waitForButtonClickable`. |
| `getBetLog()` | `BetRecord[]` | Every response so far, `{seq, t, result}`. Does not move the cursor. |
| `getUnconsumedBetCount()` | `number` | Responses buffered but not yet awaited. Should be `0` at the end of a clean run. |
| `diagnose()` | `object` | Everything in one object: state, capture status, spin button state/mode/lock, feature state and panel rows, bet cursor, snapshot, all rects. **Attach it to every failure.** |
| `logBetResponses` | `boolean` property | Default `true`: one console line per round. |
| `hitTestDebug` | `boolean` property | Default `false`: `console.warn` whenever a hit-test is blocked. |
| `dispose()` | `void` | Detaches hooks and timers. Teardown only. |

---

## 8. Reason tokens

These appear in wait timeouts and in `canDo`.

| Token | Meaning |
|---|---|
| `scene_not_ready` | Game not started, or spin handlers not attached yet. |
| `state:<appState>` | Not idle (`betting`, `result`, `promo`, `error`, …). |
| `autoplay_running` / `free_spins_running` / `hold_spin_running` | The client is driving itself. |
| `busy:<ops>` | A tracked animation is still playing (§5 `busyOps`). |
| `popup_open:<label>` / `popup_not_open:<label>` | A modal is covering the screen / the control's popup is closed. |
| `input_locked` | A popup took the key lock and it is still held; spin clicks would be swallowed. |
| `unknown_name` | Not a control of this build or session. |
| `unresolved:hidden_or_popup_closed` | A known control that cannot do its job right now. |
| `not_visible` · `button_disabled` | Off-canvas · not interactable. |
| `blocked_by:<nodeName>` | Another input-consuming layer sits on top. |
| `clipped_by_mask` | Scrolled out of its list; scroll it into view. |
| `no_consumer_at_point` · `inactive_hierarchy` · `target_null` | Hit-test could not reach the node. |
| `ante_bet_active` · `ante_bet_not_active` · `premium_reels_active` · `premium_reels_not_active` | Feature state blocks the action. |
| `feature_unavailable:anteBet` · `feature_unavailable:premiumReels` · `unknown_package:<reelId>` | The session does not offer it. |
| `not_spinning` · `auto_not_running` · `unknown_action:<action>` | Wrong moment / caller bug. |

---

## 9. Recipes

A shared helper for the recipes:

```js
const click = async (name) => {
  const r = await page.evaluate(n => window.__gameTestBridge.waitForButtonClickable(n), name);
  await page.mouse.click(r.centerX, r.centerY);
};
```

### Base spin
```js
const r = await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
await page.mouse.click(r.centerX, r.centerY);
const round = await page.evaluate(() => window.__gameTestBridge.awaitBetResponse());
await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());   // settled
```

A round that triggers Hold & Spin: keep calling `awaitFreespinRound()` until the round is terminal
(below), then `waitForSpinClickable()`.

**Terminal free-spin round.** This client reads the counter from `freeSpinInfo.freespins`.
- The round is terminal when that is `0` or absent, or when `gameState` is back to base (`1`) where the
  server sends it.
- A max-win round (`hasReachedMaxWin`) can end the bonus with spins left.

### Activate Ante Bet, then spin
```js
const s = await page.evaluate(() => window.__gameTestBridge.getSnapshot());
if (!s.anteBetAvailable) return unreached();

await click('buyBonus');
await page.evaluate(() => window.__gameTestBridge.scrollBuyFeatureIntoView('buyBonus:anteBet'));
await click('buyBonus:anteBet');
await click('buyBonus:confirm');

// the reels play the ante animation; spin stays locked until it ends (busy:anteBet.animation)
const r = await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
// snapshot: isAnteBet === true, spinCost === betAmount * anteMultiplier
await page.mouse.click(r.centerX, r.centerY);
const round = await page.evaluate(() => window.__gameTestBridge.awaitBetResponse());
// response: anteMultiplier > 0; what the player paid diverges from baseStake
```

### Activate a Premium Reel, then spin
```js
const opts = await page.evaluate(() => window.__gameTestBridge.getPremiumReelOptions());
if (opts.length === 0) return unreached();
const { reelId, multiplier } = opts[0];

await click('buyBonus');
await page.evaluate(n => window.__gameTestBridge.scrollBuyFeatureIntoView(n), `buyBonus:premiumReels:${reelId}`);
await click(`buyBonus:premiumReels:${reelId}`);
await click('buyBonus:confirm');

const r = await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
// snapshot: isPremiumReel, premiumReelId === reelId, spinCost === betAmount * multiplier
await page.mouse.click(r.centerX, r.centerY);
const round = await page.evaluate(() => window.__gameTestBridge.awaitBetResponse());
// response: premiumReelId === reelId, premiumReelMultiplier === multiplier,
//           stake === baseStake * multiplier (client integration doc §4)
```

### Deactivate a feature
```js
await click('anteBet:off');        // or 'premiumReels:off' — one click, no confirm
await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
// snapshot: isAnteBet === false / isPremiumReel === false; the next response echoes 0
```

### Mutual exclusion
With Ante on:
- `getButtonRect('buyBonus')` is `null` (that click would deactivate Ante instead).
- The panel cannot be opened, so `buyBonus:premiumReels:<id>` is unreachable.
- `canDo('activatePremiumReels:<id>')` reports `ante_bet_active`.

### Buy Bonus
```js
await click('buyBonus');
await click('buyBonus:package:0');
await click('buyBonus:confirm');
// the purchase response, then the Hold & Spin rounds the client plays itself
const buy = await page.evaluate(() => window.__gameTestBridge.awaitBetResponse());
// drain with awaitFreespinRound() until terminal, then waitForSpinClickable()
```

### Autoplay run and stop
```js
await click('auto');
await click('auto:count:1');            // 20 spins
await click('auto:confirm');
const first = await page.evaluate(() => window.__gameTestBridge.awaitBetResponse(90000));
await click('stopAuto');                // the auto button stops the run
// snapshot.isAutoPlay → false; no further BET beyond the round already in flight
await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
```

### Pick a denomination
```js
await click('stakeIncrease');           // opens the list
await page.evaluate(i => window.__gameTestBridge.scrollBetRowIntoView(i), 5);
await click('bet:row:5');
await click('bet:close');
// getBetIndex() === 5, getBetAmount() === getDenominations()[5]
```

### Teardown between runs
```js
await page.evaluate(() => window.__gameTestBridge.dismissPopups());
await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());   // else reload + startGame
```

---

## 10. Suggested profile mapping

| Role | Name | Notes |
|---|---|---|
| `spin` / `stop` / `turbo` | `spin` / `stop` / `turbo` | |
| `stake_up` / `stake_down` | `stakeIncrease` / `stakeDecrease` | `HAS_BET_POPUP = True`: both open the list. |
| `bet_row:<i>` / `bet_plus` / `bet_minus` / `bet_close` | `bet:row:{i}` / `bet:plus` / `bet:minus` / `bet:close` | |
| `auto` | `auto` | Also stops a running auto. |
| `auto_count:<i>` | `auto:count:{i}` | `AUTO_COUNTS = [10, 20, 30, 50, inf]`. |
| `auto_stop_at_win` / `auto_confirm` / `auto_close` / `auto_stop` | `auto:stopAtWin` / `auto:confirm` / `auto:close` / `auto:stop` | |
| `auto_stop_main` | `stopAuto` | |
| `auto_or_freespin` | — | Not in this build. |
| `history` / `history_close` | `history` / `history:close` | |
| `detail_next` / `detail_prev` | `historyDetail:next` / `historyDetail:prev` | |
| `info` / `info_close` | `info` / `info:close` | Tutorial is a WebView: use `getTutorialRect()`. |
| `settings` / `settings_close` | `settings` / `settings:close` | |
| `sound` / `settings_sound` / `settings_music` / `settings_turbo` | `sound` / `settings:sound` / `settings:music` / `settings:turbo` | |
| `message_ok` | `notification:yes` | |
| `trophy` | `trophy` | |
| `buy_bonus` / `buy_package:<i>` / `buy_confirm` / `buy_cancel` | `buyBonus` / `buyBonus:package:{i}` / `buyBonus:confirm` / `buyBonus:cancel` | |
| `ante` | **no single control** | Activate: `buyBonus` → `buyBonus:anteBet` → `buyBonus:confirm`. Deactivate: `anteBet:off`. Scenarios that treat `ante` as a one-click toggle need a flow for this game. |
| `premium_reel:<reelId>` (new) | `buyBonus:premiumReels:{reelId}` | Activate as for ante; deactivate with `premiumReels:off`. |

---

## 11. What the bridge does not do

- **Decide outcomes.** It computes no payout, win tier or free-spin count. Read them from the raw BET
  response.
- **Drive the game to idle inside `startGame()`**, or play out a resumed bonus. That is framework work.
- **Scroll the tutorial WebView.** It is cross-origin; wheel the mouse at `getTutorialRect()`.
- **Gate spin on balance.** An insufficient balance is a game rule you assert, not a readiness condition.
