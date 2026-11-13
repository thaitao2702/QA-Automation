# Grandma Pets — QA bridge API

The single reference for `window.__gameTestBridge` in Grandma Pets. It covers every function, the
button and popup names, and how to drive the four features other games do not have: Wild Multiplier,
Walking Wild, Buy Bonus and Free Spin Extension.

It implements `game-bridge-contract.md` (the function contract) and follows
`game-bridge-implementation-guide.md`.

| | |
|---|---|
| Source | `assets/game/scripts/testBridge/` (the typed surface is `IGameTestBridge` in `BridgeTypes.ts`) |
| Unit tests | `npx vitest run --config tests/vitest.config.ts tests/testBridge` |
| Typecheck | `npx tsc -p tsconfig.bridgecheck.json` |

## Contents

1. [Availability](#1-availability)
2. [Conventions](#2-conventions)
3. [Where this game differs from the contract](#3-where-this-game-differs-from-the-contract)
4. [REQUIRED functions](#4-required-functions)
5. [Diagnostics: canDo](#5-diagnostics-cando)
6. [OPTIONAL functions](#6-optional-functions)
7. [Buttons and popups](#7-buttons-and-popups)
8. [Features](#8-features): Wild Multiplier, Walking Wild, Buy Bonus, Free Spin Extension, Cat vs Dog drama
9. [Types](#9-types)
10. [Verifying a build](#10-verifying-a-build)

---

## 1. Availability

| | |
|---|---|
| Gate | The page URL contains `bridge=1` |
| Installed | When the app reaches `READY`: gameInfo loaded, main scene built, Start **not yet** clicked |
| Before that | `window.__gameTestBridge` is `undefined`. Poll for it before calling `startGame()`. |
| Replay mode (`?mode=replay`) | Installed; the game auto-starts, so `startGame()` resolves immediately |

From the moment it is installed, the bridge has captured `gameInfo` (so `pending` survives the Start
click) and is listening to every round response. It changes no gameplay. It only observes the
promises the scene already returns, to report what is still animating.

```python
page.goto(f"{url}&bridge=1")
page.wait_for_function("() => !!window.__gameTestBridge", timeout=60_000)
page.evaluate("() => window.__gameTestBridge.startGame()")
```

## 2. Conventions

- **Coordinates**: every `ButtonRect` is in CSS pixels, relative to the viewport. Click
  `centerX` / `centerY` with `page.mouse.click`. The bridge never clicks for you, except the Start
  button in `startGame()`.
- **Synchronous getters never throw.** When the scene is not built yet, they return an empty or
  neutral value (`null`, `[]`, `0`, `"UNKNOWN"`).
- **Waits poll every 50 ms.** A wait resolves only after its condition has held continuously for
  `stableMs`, and rejects on `timeoutMs`.
- **Rejections** are `Error`s whose message is `"<function> failed: <reason>"`. `reason` is the
  **last** blocking reason seen. Inside the page the error also carries `.reason` and `.snapshot`,
  but only the message survives `page.evaluate`. Read the reason from the message, or call
  `getSnapshot()` afterwards.
- **Round payloads** are the server's inner `result` object, raw. They are never reshaped or
  rounded, and never read from client state.

## 3. Where this game differs from the contract

| Topic | Behaviour |
|---|---|
| `settings` aliases | The server nests settings under `settings.general`. The bridge adds contract aliases next to it (`denominations`, `defaultBetLevel`, `minBet`, `maxBet`, `maxPayout`, `maxMultiplier`, `winFormula`). The raw payload is otherwise unchanged. |
| `defaultBetLevel` | **The server value is 1-based** (`denominations[level - 1]`). `settings.defaultBetLevel` is the **0-based index** the game really opens at on a first session. `settings.general.defaultBetLevel` keeps the raw value. |
| Round stream | `bet`, `buyfreespin` (Buy Bonus) and `luckySpin` responses share one stream and one cursor. Error responses (`status <= 0`) are not rounds. |
| Buy Bonus grid | The client replaces the bought round's grid with a teaser (`isFakeBuyBonusView`). The bridge hands over the **server's** payload, before that rewrite. |
| Extension replies | Buy / decline Free Spin Extension replies are **not rounds**. They have their own stream: `awaitExtensionResponse()`, full envelope. |
| Spin-button counters | `autoSpinsRemaining` / `freeSpinsRemaining` are the number shown on the spin button; `∞` is reported as `Infinity`. |
| Balance | `balance` / `getBalance()` is the displayed counter. The authority is the round payload's `balance`. |
| Stake buttons | `stakeIncrease` and `stakeDecrease` both **open the denomination list** (`HAS_BET_POPUP = True`); they do not step the stake. |
| Stopping autoplay | Use `auto`: it doubles as the main-screen stop. `auto:stop` exists only inside the panel. |
| Forced results | `setForcedResult` / `setDevModeResult` are **not implemented**. Use the dev-mode panel or a BO paytable fixture. |

---

## 4. REQUIRED functions

### `startGame()` → `Promise<void>`

Clicks the loading screen's Start button and resolves **at the click**.

| Case | Result |
|---|---|
| Game already started (or replay mode) | Resolves immediately. Repeat calls are harmless. |
| Start never becomes available within 60 s | Rejects: `start_not_available:<appState>` or `start_not_interactable` |
| Start button node gone | Rejects: `start_button_missing` |

It does not wait for idle, does not play out a resumed bonus, and does not dismiss popups. A resumed
free spin begins auto-playing the moment this resolves. Drain it with `awaitFreespinRound()`.

### `getGameInfo()` → `GameInfo | null`

The launch payload **captured before Start**. It is a copy: repeated calls return the same data for
the whole session, even after the resume has cleared `pending` server-side. Returns `null` only if
the game never delivered a gameInfo.

| Field | Meaning |
|---|---|
| `pending` | `{freeSpins, stake, gameState, currentAccumulatedWinAmount, anteMultiplier, canBuyExtension}`. **The only resume signal.** `gameState`: 1 base, 2 free game, 3 buy bonus (lucky spin pending), 4 promo. |
| `lastBet` | The last round, in round-payload shape. Context only, never a resume signal. |
| `balance`, `currency` | At load |
| `settings.general` | The raw server settings, untouched |
| `settings.denominations`, `settings.defaultBetLevel`, `settings.minBet` / `maxBet` / `maxPayout` / `maxMultiplier` / `winFormula` | Aliases (see [§3](#3-where-this-game-differs-from-the-contract)) |
| `settings.gameModeSetting` | Per-mode config: `baseGame`, `freeSpinGame`, `anteBetBaseGame`, `anteBetFreeSpinGame`, `packages[]` (Buy Bonus) |
| `extensionPackages` | Free Spin Extension offers: `{id, extType, name, freeSpins, price}`. `price` is a stake multiplier. |
| `multiplierLevel` | `{baseGame, freeGame}`: same as `getMultiplierLevels()` |

### `getSnapshot()` → `GameSnapshot`

The current surface state. Synchronous, cheap, safe mid-spin, never throws.

| Field | Type | Role | Meaning |
|---|---|---|---|
| `visiblePopups` | `string[]` | asserted | Front-most first, toasts included; `[]` when none. Names in [§7](#7-buttons-and-popups). |
| `isAutoPlay` | `boolean` | asserted | |
| `balance` | `number` | asserted | Displayed balance counter |
| `betAmount` | `number` | asserted | What the next spin costs (ante included) |
| `betIndex` | `number` | asserted | Index into `denominations`; `-1` if unknown |
| `denominations` | `number[]` | asserted | Ascending |
| `speedMode` | `"NORMAL" \| "TURBO" \| "UNKNOWN"` | asserted | |
| `isAnteBet` | `boolean` | asserted | |
| `autoSpinsRemaining` | `number` | UI value | Number on the spin button during autoplay; `Infinity` for ∞; `0` otherwise |
| `freeSpinsRemaining` | `number` | UI value | Number on the spin button during free spins; `0` otherwise |
| `buyBonusPopupLoaded` | `boolean` | UI value | The Buy Bonus popup has been instantiated this session |
| `buyBonusSelectedPrice` | `number \| null` | UI value | Confirm price, or package 0's price while the list is open |
| `freeSpinPackageCount` | `number` | UI value | Buy Bonus packages configured |
| `wildMultiplierShown` | `number \| null` | UI value | Multiplier label |
| `walkingWildCount` | `number` | UI value | Walking wilds on the grid |
| `extensionOfferOpen` | `boolean` | UI value | The extension offer panel is on screen |
| `sceneReady` / `gameStarted` | `boolean` | diagnostic | |
| `gameMode` | `string` | diagnostic | `NORMAL`, `FREE_SPIN`, `LUCKY_SPIN`, `PROMO` |
| `isFreeSpinMode` | `boolean` | diagnostic | |
| `busyOps` | `string[]` | diagnostic | Presentations still playing (labels below) |

**`busyOps` labels**:

- `reels`, `walkingWild`, `walkingWildExit`, `drama`, `angry`;
- `wildMultiplier`, `winWays`, `persistenceTrails`, `scatterHighlight`;
- `bigWin`, `freeSpinAward`, `extraFreeSpinAward`, `freeSpinResult`;
- `bonusTransition`, `luckySpin`, `roundReady`;
- `balanceCountUp`, `winCountUp`, `spinPayoutCountUp`.

### `getButtonRect(name)` → `ButtonRect | null`

Where the named control is right now. Answers immediately and never throws.

| Result | Meaning |
|---|---|
| `null` | Unknown name, the control does not exist in this build, its popup is closed, or it cannot do its job now (e.g. `stop` when nothing is spinning) |
| rect, `visible:false` | The control exists but its centre is outside the canvas |
| rect, `interactable:false` | The control is on screen but disabled |

`interactable` reflects the control's own state only. Coverage by another layer is checked by
`waitForButtonClickable`.

**Full-screen tap layers** — the tap-outside backdrops (`auto:close`, `bet:backdrop`,
`gameLimit:close`) and the presentation popups' skip overlays (`bigWin:skip`, `freeSpinAward:skip`,
`freeSpinResult:skip`) — are sized to the full design resolution, which overflows the canvas on most
viewports. Their `x/y/width/height` is the part of the layer that is **on the canvas**, and
`centerX/centerY` is a point inside that part which is not covered by the panel drawn over it
(edges first, centre last). Click the centre as usual.

### `waitForButtonClickable(name, stableMs = 300, timeoutMs = 15000)` → `Promise<ButtonRect>`

Resolves when a click on the control would really register. That means all of these hold for
`stableMs`:

- the control resolves;
- it is interactable;
- it is visible on the canvas;
- a hit-test at the aim point lands on it.

| Rejection reason | When |
|---|---|
| `unknown_control:<name>` | Immediately, for a name this build does not have |
| `not_available:<name>` | The control or its popup is not there |
| `not_interactable:<name>` | Disabled |
| `offscreen:<name>` | Centre outside the canvas |
| `blocked_by:<nodeName>` | Another layer would take the click |
| `nothing_hit` / `no_geometry:<name>` | Engine could not place it (report as a bridge defect) |

### `waitForSpinClickable(stableMs = 500, timeoutMs = 90000)` → `Promise<ButtonRect>`

The single idle authority. Use it before each spin, as the settle signal after each round, and as
the boot idle check. It resolves with the spin button's rect only when every condition below holds
for `stableMs`. Otherwise it rejects with the first failing reason, in this order:

| Reason | Meaning |
|---|---|
| `scene_not_ready`, `app_state:<s>` | Not in the game yet |
| `autoplay`, `free_spin`, `lucky_spin` | A run or bonus is in progress |
| `waiting_response`, `bet_in_progress`, `queued_rounds`, `round_state:<s>` | A round is still being processed |
| `busy:<ops>` | A presentation is still playing (`busyOps` labels) |
| `popup:<name>` | A popup is open. Toasts (`notify:*`) do not block. |
| `key_locked` | A popup left the spin key-lock set, so a click would be swallowed |
| `spin_state:<state>/mode:<mode>` | The spin button is not in IDLE / NORMAL |
| `not_interactable:spin`, `offscreen:spin`, `blocked_by:<node>` | The click checks from `waitForButtonClickable` |

An insufficient balance does **not** block it. Clicking spin then shows the out-of-money `MessagePopup`.

### `awaitBetResponse(timeoutMs = 30000)` → `Promise<BetResult>`

The next round response, in arrival order. The bridge listens from install time, so a response
that arrived between your click and this call is returned, not missed.

Round responses come from three socket events:

- `bet`: normal, free and promo spins;
- `buyfreespin`: the Buy Bonus purchase round;
- `luckySpin`.

Error responses (`status <= 0`) are never returned here; they appear in the event log as
`response_error`. Rejects: `timeout`.

### `awaitFreespinRound(timeoutMs = 90000)` → `Promise<BetResult>`

Same stream and **same cursor** as `awaitBetResponse`, with a longer default because a free spin
includes its own presentation. Passive: never clicks. Loop until the round is terminal:

```js
freeSpins == 0 || nextState == 1 /* base */
```

Check both, because max win can end the bonus with `freeSpins > 0`. A round with `freeSpins == 0`
and `nextState == 2` means the Free Spin Extension offer is open (see
[Free Spin Extension](#84-free-spin-extension)). Rejects: `timeout`.

---

## 5. Diagnostics: canDo

### `canDo(action)` → `{ ok: boolean, reason?: string }`

The same check as the waits, done once without stability. `action` is `"spin"` or any button name.
`reason` uses the same strings as the rejections above. It is for explaining a timeout only; it is
never a readiness signal.

---

## 6. OPTIONAL functions

| Function | Returns | Notes |
|---|---|---|
| `dismissPopups()` | `Promise<string[]>` | Closes every closable surface through its real close path. Returns the names verified closed, within ~1.5 s. See [§7](#7-buttons-and-popups) for what it skips. **Closing `BuyExtraFreeSpinPanel` declines the extension offer.** |
| `getBetAmount()` | `number` | Cost of the next spin; with Ante ON, `stake × anteMultiplier` |
| `getBetIndex()` | `number` | Index into `getDenominations()`, 0 = lowest; `-1` if unknown |
| `getDenominations()` | `number[]` | Ascending; index `i` is `bet:row:i` |
| `getBalance()` | `number` | Displayed balance |
| `getSpeedMode()` | `"NORMAL" \| "TURBO" \| "UNKNOWN"` | `UNKNOWN` only before the game logic exists |
| `isSoundEnabled()` / `isMusicEnabled()` | `boolean` | Independent flags |
| `getMultiplierLevels()` | `{baseGame: number[], freeGame: number[]}` | See [Wild Multiplier](#81-wild-multiplier) |
| `getTutorialRect()` | `ButtonRect \| null` | The Information WebView's own rect; `null` when closed. Scroll it with `page.mouse.wheel`. The bridge cannot scroll it (cross-origin). |
| `scrollBetRowIntoView(i)` | `void` | Scrolls the open denomination list so row `i` is centred. Silently does nothing when the list is closed. |
| `getHistoryRows()` | `HistoryRow[]` | Only the rows currently rendered (the list is virtualized), sorted by `index` — the list's own row id, so it is stable while scrolling. `data` is what that row renders: `roundId`, `betTime`, `betAmount`, `totalWinAmount`, `totalFreeSpins`, `anteMulti`, `isBuyBonus`, `promoType`. `rect` is the control that opens the round's detail (the row's `>` chevron, or the row itself when it has none), so clicking `rect.centerX/centerY` opens `HistoryDetail`. `clickable` is mask-aware: a row scrolled out of the viewport reports `false`. |
| `getHistoryDetailState()` | `HistoryDetailState` | `hasNext` / `hasPrev` follow the real navigation buttons (a declined-extension ticket hides Next). Loop on them; never do arithmetic on `betCount`. |
| `startEventCapture(label)` | `void` | Opens a capture window |
| `endEventCapture(label)` | `BridgeEvent[]` | Everything since `startEventCapture(label)`; `[]` for an unknown label |
| `getButtonNames()` | `string[]` | Every name this build knows. Indexed names (`bet:row:i`, `auto:count:i`, `buyBonus:package:i`) are expanded for the data loaded. |
| `getAllButtonRects()` | `{name: ButtonRect}` | Every name that resolves right now |

### Event types

The bridge checks every frame, so an event is recorded even if the popup or state it reports lasted
only one frame. Each event has `t` (epoch ms).

| `type` | `detail` |
|---|---|
| `popup_shown` / `popup_dismissed` | `{name}`: same names as `visiblePopups` |
| `mode_changed` | `{from, to}`: game mode |
| `autoplay_changed` | `{isAutoPlay}` |
| `speed_changed` | `{speedMode}` |
| `round_response` | `{event, seq}` |
| `response_error` | `{event, status, code}` |
| `extension_response` | `{seq, status}` |
| `drama_planned` | `{roundId, winnerFamily, fakes}` |
| `drama_started` / `drama_finished` | `{roundId}` / `{roundId, stopTaps}` |
| `angry_started` | `{roundId, pairs}` |

---

## 7. Buttons and popups

A popup's buttons resolve only while that popup is open; otherwise `getButtonRect` returns `null`.

### Main screen

| Name | Control | Notes |
|---|---|---|
| `start` | Loading screen Start | Only while `READY` |
| `spin` | Spin | `interactable` only when IDLE and not key-locked |
| `stop` | Spin in STOP state | Only while a spin can be cut short |
| `turbo` | Turbo toggle | Pair with `getSpeedMode()` |
| `auto` | Autoplay | Opens the panel when idle. **Stops a running autoplay.** |
| `stakeIncrease` / `stakeDecrease` | Stake arrows | Open the denomination list |
| `anteBet` | Ante Bet | `null` when ante is disabled for this operator |
| `buyBonus` | Buy Bonus | Disabled in free spins, autoplay, promo and with ante ON |
| `settings` | System button | Always on the main screen |
| `history` · `info` | System buttons | **Only on builds that show them on the main screen.** This build reaches them through Settings instead, so they resolve to `null` here — use `settings` → `settings:history` / `settings:info`. Probe with `getButtonRect`, don't assume. |
| `sound` (`soundOn` / `soundOff`) | Mute toggle | `sound` resolves whichever variant is showing |
| `trophy` | Monumental wins | |

### Popup buttons

| Popup (`visiblePopups` name) | Button names |
|---|---|
| `DenominationPopup` | `bet:row:{i}` (index into `getDenominations()`, 0 = lowest; the selected row reports `interactable:false`), `bet:plus`, `bet:minus`, `bet:close`, `bet:backdrop` |
| `AutoPlayPopup` | `auto:count:{i}` (`AUTO_COUNTS = [10, 20, 30, 50, ∞]`; the panel opens on index 0), `auto:stopAtWin`, `auto:confirm`, `auto:stop`, `auto:close` (tap-outside), `auto:bet:plus`, `auto:bet:minus` |
| `SettingPopup` | `settings:turbo`, `settings:music`, `settings:sound`, `settings:history`, `settings:info`, `settings:gameLimit`, `settings:close` |
| `MessagePopup` | `message:ok`, `message:cancel`, `message:close` |
| `HistoryPopup` | `history:close`, `history:time`, `history:customDate:close` |
| `HistoryDetail` | `historyDetail:next`, `historyDetail:prev`, `historyDetail:close` |
| `InformationPopup` | `info:close`. The guide is a WebView: use `getTutorialRect()`. |
| `MonumentalWinsPopup` / `PopupGameLimit` | `trophy:close` / `gameLimit:close` |
| `MultipleBuyBonusPopup` | `buyBonus:package:{i}`, `buyBonus:plus`, `buyBonus:minus`, `buyBonus:close`, `buyBonus:confirm`, `buyBonus:cancel` |
| `BigWinPopup` | `bigWin:skip` — tap the celebration to skip. **Two clicks close it**: the 1st ends the Big→Mega→Ultra escalation, the 2nd exits. It refuses skips for its first second, then for 2 s after the 1st click (`waitForButtonClickable` holds through both), and reports `interactable:false` all round when the round forbids skipping. |
| `GetFreeSpinPopup` | `freeSpinAward:skip` |
| `ExtraFreeSpinPopup` | none — the popup has no buttons; it plays out and closes itself |
| `FreeSpinResultPopup` | `freeSpinResult:skip`, `extension:confirm`, `extension:decline`, `extension:close` |
| `GetFreeSpinPopup` / `BigWinPopup` | `freeSpinAward:skip` / `bigWin:skip` (tap-outside overlays; `interactable:false` when the round forbids skipping) |
| `LuckySpinPopup` | `luckySpin:spin`, `luckySpin:keep` |
| `Promo:list` | `promo:list:close` (the list's X — its neighbour is *Discard all*, which is not exposed) |
| `Promo:receive` | `promo:receive:confirm`, `promo:receive:cancel`, `promo:receive:other` — see below; each resolves only when the dialog shows that button |
| `Promo:message` | `promo:message:ok` |

**`Promo:receive` is one dialog with three uses, and `confirm` means something different in each.**
To **close without side effects, click `promo:receive:cancel`**; if it is `null`, the dialog is an
end-of-promo result and `confirm` is its only (and safe) way out.

| Dialog | `confirm` | `cancel` | `other` |
|---|---|---|---|
| New promo received | **Claim — starts the promo** (promo spins follow) | Close (the next new promo may show) | Close all (only with 2+ new promos) |
| Discard a promo | **Discards it — irreversible** | Back | — |
| End-of-promo result | Back to game, or *More promo* (opens the list) | Back to game (when present) | — |

The end-of-promo result is **awaited by the round**: it must be closed by its button, never by
hiding the node. `dismissPopups()` follows the same rule — it presses `cancel`, else `confirm` on
an end-of-promo result, so it never claims or discards a promo.

### Other `visiblePopups` names

| Kind | Names | `dismissPopups()` |
|---|---|---|
| Presentation popups | `BigWinPopup`, `GetFreeSpinPopup`, `ExtraFreeSpinPopup`, `FreeSpinResultPopup` | Never force-closed; they end through their own skip / timeout (see their skip controls in the table above) |
| Sub-surfaces | `BuyExtraFreeSpinPanel` (extension offer), `MultipleBuyBonusPopup:confirm`, `HistoryPopup:customDate`, `SymbolInfo`, `Promo:list`, `Promo:receive`, `Promo:message` | Closed. `BuyExtraFreeSpinPanel` = a decline. |
| Toasts | `notify:turbo`, `notify:autoplay`, `notify:anteBet`, `notify:anteBetInfo` | Closed; never block spin |
| Not closable by the bridge | `DisconnectPopup`, `LuckySpinPopup` | Skipped |

**`Promo` is never reported.** The promo prefab is instantiated at load and stays active all session:
it is the container for the promo icon and HUD, not a popup. Only its inner popups appear in
`visiblePopups`, as `Promo:list` (the promo list), `Promo:receive` (the claim dialog) and
`Promo:message`. The promo icon and side notifications are HUD, so they are not reported at all.

---

## 8. Features

Everything here reports UI and config. Grade outcomes from the round payloads.

### 8.1 Wild Multiplier

**Rules:** every spin picks one multiplier. Every winning way containing at least one WILD (symbol 19)
is multiplied by it. The base game draws from 1x, 2x, 3x, 5x, 8x, 10x, 15x, 30x, 40x.

**`getMultiplierLevels()`** → `{ baseGame: number[], freeGame: number[] }`

The configured ladders, ascending, read from `settings.gameModeSetting.<mode>.nonStackingRandomWildMultiplier`.
Expected: base `[1, 2, 3, 5, 8, 10, 15, 30, 40]`, free `[2, 3, 5, 8, 10, 15, 30, 40]`.

**`getWildMultiplierState()`** → `WildMultiplierState`

| Field | Meaning |
|---|---|
| `shown` | Value on the multiplier label; `null` when blank. It resets to 1 when a spin starts. Also in the snapshot as `wildMultiplierShown`. |
| `candidates` | Ladder of the current mode (ante variants included when ante is ON) |
| `mode` | `"base"` or `"free"` |
| `isLuckyFixed` | A lucky-spin bonus holds one fixed multiplier for the whole run |

Busy op `wildMultiplier` covers the trail / fly-to-payout animation.

Payload fields to grade:

- `wildType.type == "NonStacking"`;
- `wildMultipliers[symbolId == 19].multiplierValue` must be in the ladder;
- each `wayDetails[*]` with `wildPositions` has `payout = base × multiplier`.

Suggested cases:

1. The multiplier is always in the base ladder.
2. Ways without a wild are not multiplied.
3. After the spin settles, `shown` equals the round's multiplier when a wild won.
4. `shown` resets to 1 on the next spin start.

### 8.2 Walking Wild

**Rules:** during free spins, WILDs (grandma) walk onto the reels and stop on a reel. On each following
free spin, a WILD may stay, move to another reel, or walk off. Each free spin also picks a multiplier
from 2x, 3x, 5x, 8x, 10x, 15x, 30x, 40x (**no 1x**).

**`getWalkingWildState()`** → `WalkingWildState`

| Field | Meaning |
|---|---|
| `enabled` | Feature switch in the client config |
| `onGrid` | Walking wilds on, or walking onto, the grid. Also in the snapshot as `walkingWildCount`. |
| `cells` | `{row, col}` each one stands on; row 0 = top, col 0 = reel 1 |
| `animating` | A walk-in, park or walk-off is playing |

Busy ops: `walkingWild`, `walkingWildExit`.

Suggested flow:

1. Trigger or buy free spins.
2. Loop `awaitFreespinRound()`.
3. Free spins are auto-played, so the spin waits do not apply. After each round, poll
   `getSnapshot().busyOps` until `reels` and `walkingWild` clear.
4. Compare `getWalkingWildState().cells` with the WILD cells in that round's `view`. After parking
   they should be the same set.
5. Assert every free-spin multiplier is in `getMultiplierLevels().freeGame` (never 1).

Walking wilds are presentation. The walk may visit other cells first; only the parked positions are
meaningful. Walking wilds that parked in a previous bonus may stay on the grid in base game until the
next spin walks them off.

### 8.3 Buy Bonus

**Rules ("Golden Walk"):**

- The price is 75× the selected bet.
- The purchase awards 1 BONUS symbol on each reel 1–5 and 15 free spins.
- The bonus spins follow the normal free spin rules.

**`getBuyBonusState()`** → `BuyBonusState`

| Field | Meaning |
|---|---|
| `available` | The main `buyBonus` button is enabled. It is disabled in free spins, autoplay and promo, and with Ante ON. |
| `open` / `confirmOpen` | Popup / confirm layer on screen |
| `stake`, `stakeIndex` | The popup's stake while open, otherwise the main stake |
| `packages[]` | `{index, id, name, freeSpins, buyMultiplier, price, affordable}`; `index` is `buyBonus:package:{index}` |
| `confirmPrice` | Price on the open confirm layer, else `null` |

The snapshot also carries `buyBonusPopupLoaded`, `buyBonusSelectedPrice` and `freeSpinPackageCount`.

Flow:

```
waitForButtonClickable('buyBonus')           -> click
getBuyBonusState()                           -> open, stake, packages[]
waitForButtonClickable('buyBonus:plus' | 'buyBonus:minus') -> click; changes the game's stake too
waitForButtonClickable('buyBonus:package:0') -> click (disabled when unaffordable)
getBuyBonusState()                           -> confirmOpen; assert confirmPrice == stake * 75
waitForButtonClickable('buyBonus:confirm')   -> click (or 'buyBonus:cancel')
awaitBetResponse()                           -> the purchase round (event buyfreespin): stake = 75 x bet, freeSpins = 15
awaitFreespinRound() ...                     -> until freeSpins == 0 OR nextState == 1
```

The purchase round carries the server's real grid. On screen, the client draws a teaser grid instead.

### 8.4 Free Spin Extension

**Rules:** after all free spins are completed, whether triggered by BONUS symbols or bought, the
player is offered a Free Spins Extension. The extended spins follow the same free spin rules.

**`getExtensionOfferState()`** → `ExtensionOfferState`

| Field | Meaning |
|---|---|
| `resultPopupOpen` | `FreeSpinResultPopup` on screen |
| `open` | The offer panel (`BuyExtraFreeSpinPanel`) on screen. Also in the snapshot as `extensionOfferOpen`. |
| `interactable` | Yes / No / Close accept clicks (false during the ~0.55 s fade-in and while buying) |
| `purchasing` | A purchase is in flight: No / Close are ignored |
| `showingResult` | The panel shows the summary of the previous purchase |
| `packages[]` | `{id, name, extraFreeSpins, price, currency}`; `price` is already multiplied by the stake |

**`awaitExtensionResponse(timeoutMs = 30000)`** → `Promise<{status, code, msg, result}>`

The next buy-extension reply, **envelope included**. This is its own stream, separate from rounds.

| Reply | Shape |
|---|---|
| Purchase granted | `status: 1`, `result.freeSpins` = spins added, `result.balance` = new balance |
| Decline (`extension:decline` / `extension:close` / `dismissPopups()` / autoplay / reload) | `status: 1` with a zeroed-out `result`. Not a purchase. |
| Failure | `status <= 0` |

Flow:

```
awaitFreespinRound()               -> last round: freeSpins == 0, nextState == 2 (window still open)
getExtensionOfferState()           -> poll until open && interactable
waitForButtonClickable('extension:confirm') -> click
awaitExtensionResponse()           -> {status: 1, result: {freeSpins, balance, ...}}
awaitFreespinRound() ...           -> the extension spins, same stream as before
                                      then the result popup reopens with showingResult = true,
                                      and a new offer may follow
```

To decline, click `extension:decline` or `extension:close`, or call `dismissPopups()`.

Edge cases the client implements, all worth a case:

- **No offer when max win is reached**, or when the server sends `canBuyExtension:false`.
- **Autoplay never shows the offer.** The client auto-declines (`reason: "on auto mode"`), and the
  result popup still appears without it.
- **Insufficient balance:** clicking `extension:confirm` shows a `MessagePopup`. Either button there
  declines and closes the result popup.
- **No/Close within the first 5 s:** the popup holds until that window ends, then exits.
- **Reload during the decision:** on the next boot the client auto-declines (`"cancelled purchase due
  to game reload"`). `getGameInfo().pending.canBuyExtension` shows whether the window was open.
- **In-flight purchase:** while `purchasing` is true, No/Close are ignored.

The client's own declines (autoplay, low balance, reload) also produce replies on
`awaitExtensionResponse()`. Collect them, or your next call gets a stale reply.

### 8.5 Cat vs Dog drama

**What it is:** a **presentation only** beat. It never changes the result or the payout. When a
round holds a cat (P1, P3, P4 = ids 5, 7, 8) or a dog (P2, P5 = ids 6, 18), the client may:

1. land the reels on a fake screen: the real result with fake symbols of the **losing** family
   inserted, including one right beside a real symbol of the **winning** family;
2. play the fight between that winner and the fake beside it;
3. remove every fake and drop the real symbols back, so the grid ends on the server's `view`.

The winning family is the one with more symbols on the bottom row (then the row above). Each fake
is a random member of the losing family (e.g. any of P1 / P3 / P4 when cats lose).

**Every losing-family symbol on the fight screen is a fake.** Inserting a fake does not cover a
cell: it pushes the rows above it up by one, and the reel's top symbol goes off-screen. A mandatory
planning rule puts enough fakes on each reel to push **every real** losing-family symbol off the
top; a fight that cannot do so is not chosen. So when the fakes vanish, the whole losing family
leaves the screen, and the winner's family stays. Then the pushed-out real symbols fall back in
(`evictedByReel`), so losing-family symbols from the real result reappear as part of it.

**Play rate:** the first matching condition owns the only roll.

| Round | Rate |
|---|---|
| Big win or higher (`winType >= 4`) | 70% |
| Free spin trigger (`wonFreeSpins > 0`) | 70% |
| Any other round | 4% |

**Angry** is the small sibling. When the drama did not play, real cat/dog neighbours on the same
row may glare at each other (20%). There are no fakes and nothing is removed. It never plays in free
spins or in the buy-bonus round.

**`getDramaState()`** → `DramaRoundState` for the latest round the reels landed on:

| Field | Meaning |
|---|---|
| `roundId`, `betId` | Match the round payload's `roundId` / `id` |
| `evaluated` | The client rolled for the drama. `false` when a walking wild or the buy-bonus teaser took the round. |
| `played` | The fight played |
| `skipReason` | `null` when played; otherwise one of `disabled`, `mode-excluded`, `no-cat-dog`, `roll-failed`, `no-valid-candidate`, `family-has-no-candidate`, `walking-wild`, `fake-buy-bonus` |
| `trigger`, `playRate` | Which roll applied: `bigWin` / `freeSpinTrigger` / `normal`, and its rate |
| `phase` | `none` → `fighting` → `done` |
| `winnerFamily`, `loserFamily`, `side` | `side` = which side of the winner the loser stands on |
| `winner` | `{reel, row, symbolId, symbolName, family}`: a real symbol of the result |
| `loser` | Same shape: always a fake, same row, neighbouring reel |
| `fakeCells` | Every fake inserted, the loser included: `{reel, row, symbolId, symbolName}` |
| `fakeCountByReel` | Fakes per reel |
| `evictedByReel` | Per reel, the real symbols the fakes pushed off the top (top-most first); they fall back in once the fakes go |
| `landingView` | `matrix[reel][row]` the reels landed on (with fakes) |
| `realView` | `matrix[reel][row]` of the server result |
| `stopTaps`, `spedUp` | Player taps during the drama: the 1st skips the fight, the 2nd speeds the rest up ×3 |
| `angry` | `{evaluated, played, skipReason, pairs[{row, leftReel, rightReel}]}` |

**`getDramaHistory(limit = 50)`** → the previous rounds' records, oldest first (the current round is
not included; up to 200 kept). Use it for rate checks over a run.

**`getDramaConfig()`** → the client's settings: `enabled`, `playInTurbo`, the three play rates,
`maxFakesPerColumn` (4), `cleanColumns`, `catSymbolIds`, `dogSymbolIds`, `angryEnabled`,
`angryPlayRate`, and the dev switches `devForceTrigger` / `devForceFamily`. A build with a dev
switch on is not a production build: fail the run.

**`getDisplayedGrid()`** → `matrix[reel][row]` of the symbol ids on screen right now; `-1` for a
hidden or masked cell. Use it to prove what the player actually saw.

Busy ops `drama` and `angry` cover the two animations; `waitForSpinClickable` waits for both.

Flow for one round:

```
click spin; r = awaitBetResponse()
waitForSpinClickable()               -> the drama (if any) has finished
d = getDramaState()                  -> d.roundId == r.roundId
if d.played: assert the invariants below
assert getDisplayedGrid() == r.view  -> the grid ends on the real result, drama or not
                                        (compare cells that are not -1; r.view is the grid after any
                                         wild expand, r.rawView the one before)
```

Invariants to assert on a played drama:

1. `realView` equals the round payload's `rawView` (or `view` when there is no `rawView`), and
   `payout` / `wayDetails` are what the server sent.
2. `winner` is a cell of `realView` holding a symbol of `winnerFamily`.
3. `loser` is on the winner's row, on reel `winner.reel ± 1` (per `side`), and its family is
   `loserFamily`, the opposite of `winnerFamily`.
4. Every `fakeCells[*].symbolId` belongs to `loserFamily`, and the loser is one of them.
5. **Every `loserFamily` cell of `landingView` is in `fakeCells`**: no real losing-family symbol
   is visible during the fight.
6. Every real `loserFamily` symbol of `realView` appears in `evictedByReel` of its reel, and none
   sits below the winner on the winner's own reel.
7. No reel carries more than `maxFakesPerColumn` fakes; `evictedByReel[reel].length == fakeCountByReel[reel]`.
8. `realView` holds at least one cat or dog. A drama never plays without one.
9. Never together with a walking wild: `skipReason == "walking-wild"` rounds have `played == false`.
10. `angry.played` is never true when `played` is true, nor in free spins or the buy-bonus round.

Rate checks over a run (`getDramaHistory()`): the roll happens only after the cat/dog check, so the
rolled rounds are those with `played == true` or `skipReason` in `roll-failed`, `no-valid-candidate`,
`family-has-no-candidate`. Among them, per `trigger`, the share with `played == true` should approach
`playRate`. `no-valid-candidate` / `family-has-no-candidate` rounds won the roll but found no legal
fight, so expect the observed rate slightly **below** the configured one. To test the fight itself deterministically, use a dev
build with `devForceTrigger` / `devForceFamily` set in `DramaConfig.ts`.

---

## 9. Types

```ts
interface ButtonRect { x: number; y: number; width: number; height: number; centerX: number; centerY: number; visible: boolean; interactable: boolean; }
interface CanDoResult { ok: boolean; reason?: string; }
type SpeedModeName = "NORMAL" | "TURBO" | "UNKNOWN";

interface HistoryRow { index: number; rect: ButtonRect; clickable: boolean; data: Record<string, unknown>; }
interface HistoryDetailState { open: boolean; betIndex: number; betCount: number; hasPrev: boolean; hasNext: boolean; }
interface BridgeEvent { type: string; t: number; detail?: unknown; }

interface WildMultiplierState { shown: number | null; candidates: number[]; mode: "base" | "free"; isLuckyFixed: boolean; }
interface WalkingWildState { enabled: boolean; onGrid: number; cells: { row: number; col: number }[]; animating: boolean; }
interface BuyBonusPackageState { index: number; id: number; name: string; freeSpins: number; buyMultiplier: number; price: number; affordable: boolean; }
interface BuyBonusState { available: boolean; open: boolean; confirmOpen: boolean; stake: number | null; stakeIndex: number; packages: BuyBonusPackageState[]; confirmPrice: number | null; }
interface ExtensionOfferPackage { id: number; name: string; extraFreeSpins: number; price: number; currency: string; }
interface ExtensionOfferState { resultPopupOpen: boolean; open: boolean; interactable: boolean; purchasing: boolean; showingResult: boolean; packages: ExtensionOfferPackage[]; }

interface DramaCell { reel: number; row: number; symbolId: number; symbolName: string; }
interface DramaFighter extends DramaCell { family: "CAT" | "DOG"; }
interface AngryState { evaluated: boolean; played: boolean; skipReason: string | null; pairs: { row: number; leftReel: number; rightReel: number }[]; }
interface DramaRoundState {
  roundId: string | null; betId: string | null; evaluated: boolean; played: boolean; skipReason: string | null;
  trigger: "bigWin" | "freeSpinTrigger" | "normal" | null; playRate: number | null; phase: "none" | "fighting" | "done";
  winnerFamily: "CAT" | "DOG" | null; loserFamily: "CAT" | "DOG" | null; side: "left" | "right" | null;
  winner: DramaFighter | null; loser: DramaFighter | null; fakeCells: DramaCell[]; fakeCountByReel: number[]; evictedByReel: number[][];
  landingView: number[][] | null; realView: number[][] | null; stopTaps: number; spedUp: boolean; angry: AngryState;
}
interface DramaConfigState {
  enabled: boolean; playInTurbo: boolean; bigWinPlayRate: number; freeSpinPlayRate: number; normalPlayRate: number;
  maxFakesPerColumn: number; cleanColumns: number; catSymbolIds: number[]; dogSymbolIds: number[];
  angryEnabled: boolean; angryPlayRate: number; devForceTrigger: boolean; devForceFamily: "CAT" | "DOG" | null;
}
```

`GameSnapshot` is listed field by field in [§4](#getsnapshot--gamesnapshot).

---

## 10. Verifying a build

1. Run the unit tests and the typecheck (commands at the top).
2. In a **built** game with `?bridge=1`, run `python scripts/probe_bridge.py --game grandma-pets --env dev`.
   Check that `waitForSpinClickable` resolves, `getAllButtonRects()` centres land inside the canvas,
   and each popup appears in `visiblePopups` and closes with `dismissPopups()`.
3. Boot paths: a clean session, a resumed free spin (`pending.freeSpins > 0`), a resumed bought
   bonus, and a reload during the extension decision.
