# Zeus Slot Game — Test Bridge API Reference

> **Purpose:** Comprehensive reference for `ZeusTestBridge` — the E2E test automation bridge.
> All methods emulate real user interactions through the actual UI code path.
>
> **Companion doc:** [`bridge-applicability.md`](./bridge-applicability.md) — the
> applicability mechanism (`canDo`, `waitUntilApplicable`, hit-test) that
> guards every action against "the click fired but nothing happened" failures.
> Read it once; this reference assumes the mechanism is in place.

---

## 1. Design Philosophy

The bridge exposes three categories:

| Category | Purpose | Mutates State? |
|----------|---------|----------------|
| **Actions** | Emulate what a real user does (click spin, open popup, select bet) | Yes — through the same code path as a real click |
| **Queries** | Read-only state inspection (get balance, get reel state, check buttons) | No |
| **Applicability** | "Can I perform action X right now?" — preconditions, reasons, hit-test | No — read-only checks driving safe actions |

### Why UI-Path-Only Actions Matter

**Anti-pattern — direct internal calls:**
```
GameLogic.nextBet()  ← WRONG: skips UI label update, denomination popup sync
```

**Correct — UI path:**
```
GameplayScene.onNextBet() → GameController.onNextBet() → GameLogic.nextBet()
                          → updateBetLabel() → denominationPopup.scrollDenomination()
```

Direct internal calls skip validation logic, UI side-effects (sound, animations, button states), and can create game states that real users can never reach — leading to **false test assumptions**.

**Rule:** If a real user can't do it by clicking/touching the game, the bridge shouldn't do it either.

---

## 2. Actions (User Interaction Emulation)

> **All actions self-assert.** Every method in this section calls
> `_assertApplicable(action)` before executing. If the action is not safe to
> perform right now, it throws synchronously:
>
> ```
> [ZeusTestBridge] spin() not applicable: popup:buyBonus
> [ZeusTestBridge] confirmBuyBonus() not applicable: insufficient_balance
> [ZeusTestBridge] nextBet() not applicable: state:result
> ```
>
> The **Precondition** field in each entry below describes what `canDo(action)`
> verifies. The reason token after the colon is stable and grep-safe — see the
> [token vocabulary](./bridge-applicability.md#3-the-contract) in the companion
> doc.
>
> **Recommended test pattern:**
>
> ```ts
> await bridge.waitUntilApplicable('spin');   // quiescence (250 ms stable by default)
> await bridge.spin();                         // throws if state changed between
> ```

### 1.1 `startGame()`

Emulates user clicking the Start button on the loading screen.

| | |
|-|-|
| **Emulates** | Touch `startButton` on `LoadingScene` |
| **Precondition** | AppState = `ready` (bridge is registered, Start button is visible) |
| **Returns** | `Promise<void>` — resolves when `GameplayScene.start()` fully completes |

**Code path:**
```
LoadingScene.onStartClicked()
  → SlotGame node.active = true → GameplayScene.start()
    → prepare()                  [spinButton.interactable = false]
    → await initialize()         [GameController → prepareGame → transitionTo(IDLE)]
    → await lazyLoadAssets()     [popup initialization]
    → setupHandlers()            [touch/click handlers registered]
    → sceneReady resolves        [bridge waits on this]
  → SoundManager.setupBgAudio()
```

**Why not just `waitForIdle()`:** `transitionTo(IDLE)` fires during `initialize()` — before `lazyLoadAssets()` and `setupHandlers()` complete. Waiting only for IDLE would resolve too early, leaving popups uninitialized and touch handlers unregistered. `startGame()` waits for both IDLE and `GameplayScene.sceneReady`.

**State flow:** `ready` → `idle` (during initialize) → scene fully ready (after setupHandlers)

> **Source:** `LoadingScene.ts:230-235`, `GameplayScene.ts:114-120`

---

### 1.2 `dismissPopups()`

Dismisses all currently visible popups. Returns an array of dismissed popup names.

| | |
|-|-|
| **Returns** | `string[]` — names of popups that were dismissed |
| **Popup names** | `'promo'`, `'notification'`, `'buyBonus'`, `'auto'`, `'settings'`, `'bigWin'`, `'totalWin'`, `'devMode'`, `'disconnect'` |

Covers all popup types that can block the game:
- **Promo popups** — new promo started, promo receive, promo countdown, promo list
- **Notification** — error messages, session timeout, disconnect alerts
- **Buy bonus / Auto** — purchase and auto-spin configuration popups
- **Settings / Info / History** — tabbed settings popup
- **Big win / Total win** — celebration popups
- **Dev mode** — developer tools panel
- **Disconnect** — connection lost overlay

---

### 1.3 `isPopupVisible()`

Returns `true` if any popup is currently blocking the game.

| | |
|-|-|
| **Returns** | `boolean` |

Useful for test assertions and waiting:
```javascript
// Wait for all popups to close before proceeding
await page.waitForFunction(() => !window.__gameTestBridge.isPopupVisible());
```

---

### 2.1 `spin(timeoutMs?: number)`

Emulates user touching the spin button.

| | |
|-|-|
| **Emulates** | Touch `nodeSpinButton` (TOUCH_START → TOUCH_END) |
| **Precondition** | AppState = `idle`, no promo popup showing |
| **Parameter** | `timeoutMs` — max wait for IDLE (default `60000`). Increase for spins that trigger bonus rounds. |
| **Returns** | `Promise<void>` — resolves when state returns to `idle` |

**Code path:**
```
GameplayScene.onSpinClicked()
  → GameplayScene.onSpinButtonReleased()
    → GameController.onSpinClicked()        [validates idle state]
      → GameController.spin()               [checks balance ≥ bet]
        → SlotGameApplication.spin(request) [SignalR 'bet' event]
```

**State flow (normal spin):** `idle` → `betting` → (server response) → `result` → (handler chain: spin animation → win lines → big win → transitions) → `idle`

**State flow (triggers Hold&Spin bonus):** `idle` → `betting` → `result` → `START_BONUS` → `IN_BONUS` → ... re-spins ... → `END_BONUS` → `idle` (can take 30-50s)

**Timeout guidance:**
```javascript
// Normal spin — default 60s is sufficient
await bridge.spin();

// Spin that may trigger Hold&Spin bonus — use longer timeout
await bridge.spin(120_000);
```

> **Source:** `GameplayScene.ts:868-908`, `GameController.ts:451-468`

---

### 2.2 `stopSpin()`

Emulates user touching stop button to skip reel animation during result.

| | |
|-|-|
| **Emulates** | Touch `stopButton` during result animation |
| **Precondition** | AppState = `result`, turbo allowed |

**Code path:**
```
GameplayScene.onStopClicked()
  → SlotMachineView.stopFast()
```

> **Source:** `GameplayScene.ts:472-479`, `GameController.ts:471-477`

---

### 2.3 `nextBet()`

Emulates user clicking the next bet button.

| | |
|-|-|
| **Emulates** | Click `nextBetButton` |
| **Precondition** | AppState = `idle`, not in promo/auto/free-spin, not at max bet |

**Code path:**
```
GameplayScene.onNextBet()
  → GameController.onNextBet()
    → GameLogic.nextBet()
  → updateBetLabel()  [syncs all UI: bet label, denomination popup, buy bonus popup]
```

> **Source:** `GameplayScene.ts:944-948`, `GameController.ts:542`

---

### 2.4 `previousBet()`

Emulates user clicking the previous bet button.

| | |
|-|-|
| **Emulates** | Click `previousBetButton` |
| **Precondition** | Same as `nextBet()` but not at min bet |

**Code path:**
```
GameplayScene.onPreviousBet()
  → GameController.onPreviousBet()
    → GameLogic.previousBet()
  → updateBetLabel()
```

> **Source:** `GameplayScene.ts:958-962`, `GameController.ts:544`

---

### 2.5 `selectBet(index: number)`

Emulates user opening denomination popup and selecting a specific denomination.

| | |
|-|-|
| **Emulates** | Click denomination in `DenominationsPopup` |
| **Precondition** | Valid index, denomination ≤ balance |

**Code path:**
```
GameplayScene.onSelectedBet(index)
  → GameController.onSelectedBet(index)
    → GameLogic.onSelectedBet(index)  [validates denomination ≤ balance]
  → updateBetLabel()
```

**Parameter:** `index` — zero-based index into `GameLogic.denominations` array.

> **Source:** `GameplayScene.ts:951-955`, `GameController.ts:543`

---

### 2.6 `openAutoPopup()`

Emulates user clicking the auto button to open the auto-spin configuration popup.

| | |
|-|-|
| **Emulates** | Click `autoButton` |
| **Precondition** | `autoButton.interactable = true` |

**Code path:**
```
GameplayScene.onAutoClicked()
  → autoPopup.open()  [popup becomes visible]
```

> **Source:** `GameplayScene.ts:982-990`

---

### 2.7 `startAutoSpin(count: number, stopByWin: boolean)`

Emulates user selecting a package in the auto popup and clicking confirm.

| | |
|-|-|
| **Emulates** | Select package → click confirm in `AutoPopup` |
| **Precondition** | AppState = `idle`, auto popup open (call `openAutoPopup()` first) |
| **Packages** | `10, 20, 30, 50, Infinity` |

**Code path:**
```
AutoPopup.onConfirmClicked()       [bridge sets packageAuto & toggleStopByWin, then calls this]
  → close()                        [hides popup, fires telemetry]
  → onConfirm(count, stopByWin)
    → GameplayScene.onConfirmAuto(count, stopByWin)
      → GameController.onConfirmAuto(count, stopByWin)
        → GameLogic.numberAutoSpin = count
        → GameLogic.isStopByWin = stopByWin
        → if (count > 0 && idle) → spin()  [immediately triggers first spin]
```

**Stop-by-win behavior:** If enabled AND a spin has payout > 0 → auto-spin stops.

**Auto-spin state flow — no IDLE between spins:**
```
IDLE → spin() → BETTING → (server) → RESULT → maybeHandleNextSpin()
                                                  ↓
                                        numberAutoSpin > 0?
                                       YES → spin() → BETTING → RESULT → ... (loop)
                                       NO  → transitionTo(IDLE)  ← only once, at the end
```

The game **does not transition to IDLE between auto-spins**. The cycle is `BETTING → RESULT → BETTING → RESULT → ...` until `numberAutoSpin` reaches 0. `waitForIdle()` resolves only after the **last** auto-spin completes.

**Observing individual spin results during auto-spin:**

Use event collection to capture each spin result:
```javascript
await page.evaluate(() => window.__gameTestBridge.startEventCollection());
await page.evaluate(() => {
    window.__gameTestBridge.openAutoPopup();
    window.__gameTestBridge.startAutoSpin(10, false);
});
await page.evaluate(() => window.__gameTestBridge.waitForIdle());
const events = await page.evaluate(() => window.__gameTestBridge.drainEvents());

// Each spin produces a spin_result event
const spinResults = events.filter(e => e.type === 'spin_result');
// spinResults.length === 10
// Each: { reels, paylines, payout, stake, typeWin, freeSpinInfo, coinPositions }

// Balance changes are also captured per-spin
const balanceChanges = events.filter(e => e.type === 'balance_change');
// Each: { balance, previous }

await page.evaluate(() => window.__gameTestBridge.stopEventCollection());
```

> **Source:** `GameplayScene.ts:993-998`, `GameController.ts:427-437,503-511`, `AutoPopup.ts:79-83`

---

### 2.8 `stopAutoSpin()`

Emulates user clicking the stop auto button.

| | |
|-|-|
| **Emulates** | Click `stopAutoButton` |

**Code path:**
```
GameplayScene.onStopAuto()
  → GameController.onConfirmAuto(0, false)
    → GameLogic.numberAutoSpin = 0
  → updateSpinButton()
```

> **Source:** `GameplayScene.ts:1000-1007`

---

### 2.9 `openBuyBonusPopup()`

Emulates user clicking the buy bonus button.

| | |
|-|-|
| **Emulates** | Click `buyBonusButton` |
| **Precondition** | AppState = `idle`, `buyBonusButton.interactable = true` |

**Code path:**
```
GameplayScene.onBuyBonusClicked()
  → buyBonusPopup.open()
```

> **Source:** `GameplayScene.ts:965-976`

---

### 2.10 `confirmBuyBonus(packageIndex: number)`

Emulates user clicking buy → confirm in the buy bonus popup.

| | |
|-|-|
| **Emulates** | Click `BuyButton` → click `confirmButton` in `BuyBousPopup` |
| **Precondition** | AppState = `idle`, balance ≥ bet x package price |
| **Returns** | `Promise<void>` — resolves when bonus mode completes and returns to `idle` |

**Code path:**
```
BuyBousPopup.onConfirmClicked()
  → GameplayScene.onConfirmBuyBonus(packageIndex)
    → GameController.onConfirmBuyBonus(packageIndex)
      → SlotGameApplication.buyBonus(request)  [SignalR 'buyfreespin' event]
      → GameLogic.bonusGameType = BONUS_TOTAL_WIN
```

**State flow:** `idle` → `betting` → `result` → `START_BONUS` → `IN_BONUS` (re-spins) → `END_BONUS` → `idle`

> **Source:** `GameplayScene.ts:978-980`, `GameController.ts:491-501`, `BuyBousPopup.ts:189-192`

---

### 2.11 `setTurbo(enabled: boolean)`

Emulates user clicking the turbo toggle.

| | |
|-|-|
| **Emulates** | Click `onTurboButton` (off) or `offTurboButton` (on) |
| **Note** | During promo mode, turbo may be forced off and toggles become non-interactable |

**Code path:**
```
if (enabled):
  offTurboButton.isChecked = true   [turbo ON]
else:
  onTurboButton.isChecked = true    [turbo OFF]
```

**Naming note:** The toggle names are inverted — `offTurboButton.isChecked = true` means turbo IS active. `onTurboButton` is the "turbo off" state.

> **Source:** `GameplayScene.ts:187-194`, `GameplayScene.ts:1009-1033`

---

### 2.12 `setSound(enabled: boolean)`

Emulates user clicking the sound on/off button.

| | |
|-|-|
| **Emulates** | Click `onSoundButton` (mute) or `offSoundButton` (unmute) |

**Code path:**
```
if (enabled):
  GameController.onClickOnOffSound(true)
  GameController.onClickOnOffMusic(true)
  onSoundButton.node.active = true
  offSoundButton.node.active = false
else:
  GameController.onClickOnOffSound(false)
  GameController.onClickOnOffMusic(false)
  onSoundButton.node.active = false
  offSoundButton.node.active = true
```

**Note:** Sound toggle controls BOTH sound effects and music simultaneously.

> **Source:** `GameplayScene.ts:199-214`, `GameController.ts:479-489`

---

### 2.13 `openSettings()`

Emulates user clicking the settings button.

| | |
|-|-|
| **Emulates** | Click `settingButton` |

**Code path:**
```
GameplayScene.onSettingClicked()
  → SettingPopup.open(TypePopup.SETTING, { turbo, sound, music })
```

> **Source:** `GameplayScene.ts:1074-1078`

---

### 2.14 `openInfo()`

Emulates user clicking the info/paytable button. Opens the SettingPopup on the **paytable/game info tab** (page index 2).

| | |
|-|-|
| **Emulates** | Click `infoButton` |
| **Note** | Settings, History, Info, and Game Limit share the same `SettingPopup` — this opens it to the paytable tab |

**Code path:**
```
GameplayScene.onInfoClicked()
  → openSettingPopup(TypePopup.GENERAL_INFO)
    → SettingPopup.open(GENERAL_INFO)   [_indexPage = 2 → paytable tab]
```

**SettingPopup tabs:**
| TypePopup | Page | Content |
|-----------|------|---------|
| `SETTING` | 0 | Turbo, sound, music toggles |
| `HISTORY` | 1 | Bet history list |
| `GENERAL_INFO` | 2 | **Paytable / game rules** |
| `GAME_LIMIT` | 3 | Game limits |

> **Source:** `GameplayScene.ts:1090-1094`, `SettingPopup.ts:129-157`

---

### 2.15 `openHistory()`

Emulates user clicking the history button.

| | |
|-|-|
| **Emulates** | Click `historyButton` |

**Code path:**
```
GameplayScene.onHistoryClicked()
  → SettingPopup.open(TypePopup.HISTORY)
```

> **Source:** `GameplayScene.ts:1087-1091`

---

### 2.16 `holdSpinStart()` / `holdSpinRelease()`

Emulates user holding the space bar or long-pressing the spin button during bonus mode.

| | |
|-|-|
| **Emulates** | Hold spacebar / long-press spin button |
| **Precondition** | In bonus mode (Hold&Spin) |

**`holdSpinStart()` code path:**
```
onKeyDown(SPACE) / onSpinClicked()
  → after 1s hold → processHoldSpin()
    → offTurboButton.isChecked = true  [forces turbo on]
    → GameController.onSpinClicked()
    → schedule(countTimeHoldSpinButton, 0.1)  [continuous re-spins]
```

**`holdSpinRelease()` code path:**
```
onSpinButtonReleased()
  → restores turbo state to before hold
  → _isHoldSpinButton = false
  → unschedule(countTimeHoldSpinButton)
```

> **Source:** `GameplayScene.ts:821-902`

---

### 2.17 `skipBonusTransition()`

Emulates user clicking skip during Hold&Spin entry animation.

| | |
|-|-|
| **Emulates** | Click `nodeSkip` in `HoldSpinView` |

**Code path:**
```
HoldSpinView.clickSkip()
  → resolves pending Promise
  → plays exit animation
  → hides transition overlay
```

> **Source:** `HoldSpinView.ts:208-215`

---

### 2.18 `skipBigWin()`

Emulates user pressing space during big win celebration popup.

| | |
|-|-|
| **Emulates** | Press SPACE during big win animation |

**Code path:**
```
BigWinPopup.skip()
  → skips celebration animation
```

> **Source:** `GameplayScene.ts:830`

---

### 2.18a `skipTotalWin()`

Dismisses the **bonus total-win popup** ("press anywhere to continue") shown after a Hold & Spin or Buy Bonus ends. The popup blocks the IDLE transition — until it is dismissed, `waitForIdle()` will time out.

| | |
|-|-|
| **Emulates** | Tap anywhere on `BonusResultPopup` |
| **Precondition** | `canDo('skipTotalWin').ok` — popup currently visible (`snapshot.isShowingTotalWin === true`). Throws `[ZeusTestBridge] skipTotalWin() not applicable: no_totalwin` otherwise. |
| **Returns** | `void` (calls `BonusResultPopup.skip()` fire-and-forget so the caller can immediately `waitForIdle()`) |

**Code path:**
```
BonusResultPopup.skip()
  → finish()
    → hide()  [node.active = false; promise resolves;
               handler chain proceeds to transitionToBaseGame()]
```

> **Required post-bonus sequence:** see §4 — the right order is
> `waitForTotalWinPopup()` → `skipTotalWin()` → `waitForIdle()`. Calling
> `waitForIdle()` directly after the bonus deadlocks until the popup is
> dismissed.

> **Source:** `assets/src/views/BonusResultPopup.ts` — `skip()`, `finish()`, `hide()`.

---

### 2.19 `openDevMode()`

Emulates user clicking the dev mode button (only available in dev environment).

| | |
|-|-|
| **Emulates** | Click `devModeButton` |
| **Precondition** | URL contains `env=dev` |

**Code path:**
```
GameplayScene.onDevModeClicked()
  → devModePopup.node.active = true
```

> **Source:** `GameplayScene.ts:1093-1096`

---

### 2.20 `setDevModeResult(grid: number[][])`

Injects a 3x3 reel result for the **next** spin via DevModeManager.

> **⚠️ Use ONLY for single-result tests.**
>
> DevMode is intended for tests that need to force **one specific spin
> outcome** — e.g. asserting that a particular reel layout triggers the
> expected win, bonus entry, or UI behaviour for that single spin.
>
> **Do NOT use DevMode for tests that depend on subsequent spin results.**
> Once the queued DevMode entries are consumed, every later spin (and every
> Hold & Spin re-spin past what was queued) falls back to the real server
> API. The test then mixes client-injected and server-driven results within
> the same run, and the two paths produce **different** payout, balance,
> bonus-progression, and persistence data that no longer reconciles. Any
> assertion that compares "this spin" to "the next spin" becomes unreliable.
>
> For multi-spin tests — bonus sequences, auto-spin loops, balance tracking
> across many spins — use the **real server with a tuned paytable**. The server keeps the entire data flow
> consistent across the sequence.

| | |
|-|-|
| **Emulates** | Configure 3x3 grid in dev mode panel and save |
| **Parameter** | `grid` — 3×3 array of symbol IDs, indexed as `grid[col][row]`. Column 0 = left reel, row 0 = top. (See Section 7 for symbol IDs) |
| **Precondition** | URL contains `env=dev` |

**Code path:**
```
DevModeManager.add()
  → creates entry "{roundId}-0" with random BetResult
DevModeManager.edit(key, reelData)
  → overwrites grid with your values
  → handleEditData(key):
    ├─ if coins >= 3 → setDataBonus() → generates full bonus sequence
    │   entries: "{roundId}-0" (START_BONUS), "{roundId}-1" (first re-spin), ...
    └─ else → calculatePayLines() + calculateBalance() (normal spin)
```

**Consumption:** Each `spin()` call takes the **first** Map entry, uses it as the result (no server call, 300ms fake delay), then deletes it. When the Map is empty, spins fall back to real server calls.

**Queuing multiple results (exploratory / manual use only — NOT for automated tests):**

> Documented for completeness; this contradicts the single-result
> constraint above. Use only for manual demo or debugging sessions where
> you want a scripted sequence to replay by hand. Automated tests that
> need multiple specific results should drive a tuned **server paytable**
> instead.

```javascript
// Each call adds a separate round to the queue
bridge.setDevModeResult([[1,3,4], [5,6,7], [8,1,3]]);    // spin 1: normal
bridge.setDevModeResult([[9,9,9], [1,1,1], [3,3,3]]);    // spin 2: all wilds
bridge.setDevModeResult([[13,13,1],[13,1,1], [3,3,3]]);   // spin 3: 4 coins → bonus
// bonus re-spin entries ("3-1", "3-2", ...) are auto-generated

// Map order: "1-0", "2-0", "3-0", "3-1", "3-2", ...
// Auto-spin consumes entries in order
bridge.openAutoPopup();
bridge.startAutoSpin(10, false);
// Spins 1-3 (+ bonus re-spins) use dev mode results
// Remaining spins hit the real server — RESULTS WILL DIVERGE from the queued ones.
```

**Bonus auto-generation:** When the injected grid has 3+ COIN symbols (id 13), `handleEditData()` automatically generates the full Hold&Spin bonus sequence:
- Entry `"{roundId}-0"` → `START_BONUS` (the triggering spin)
- Entry `"{roundId}-1"` → first re-spin (non-coin positions cleared to -1)
- Entry `"{roundId}-2+"` → recursive re-spins with 70% chance of adding a new coin each time
- Bonus ends when `freespins = 0` or grid reaches 9 coins

**Balance note:** DevMode results are client-side only — balance changes are calculated locally. Balance may desync when returning to real server spins.

> **Source:** `DevModeManager.ts`, `SlotGameApplication.ts:186-198`

---

### 2.21 `openTrophy()`

Emulates user clicking the trophy button.

| | |
|-|-|
| **Emulates** | Click `trophyButton` |
| **Precondition** | `trophyButton.interactable = true` (only after mega/ultra wins) |

**Code path:**
```
GameplayScene.onTrophyButtonClicked()
  → TrophyComponent.show()
```

> **Source:** `GameplayScene.ts:609-612`

---

### 2.22 `spinUntilBonus(options?)`

Spins repeatedly until a Hold & Spin bonus is triggered. Resolves the **moment** bonus mode is entered — **does not** wait for the bonus to play out — leaving the game inside Hold & Spin so the caller can inspect / interact with it.

| | |
|-|-|
| **Emulates** | Press spin repeatedly until the next response triggers Hold & Spin |
| **Precondition** | `canDo('spin').ok` (re-checked before each spin via `waitUntilApplicable('spin')`) |
| **Parameter** | `options?: SpinUntilOptions` — `{ maxSpins?, timeoutMs?, perSpinTimeoutMs? }` |
| **Returns** | `Promise<SpinUntilResult>` — `{ spins: number, matched: boolean }` |

**Options & defaults:**

| Option | Default | Meaning |
|---|---|---|
| `maxSpins` | `500` | Maximum spins before giving up |
| `timeoutMs` | `300_000` (5 min) | Wall-clock budget for the whole loop |
| `perSpinTimeoutMs` | `120_000` (2 min) | Per-spin race timeout before declaring the spin stuck |

**Return shape:**

| Result | Meaning |
|---|---|
| `{ spins: N, matched: true }` | Bonus triggered on spin `N` (1-indexed) |
| `{ spins: 0, matched: true }` | Game was already in bonus when called — no-op |
| `{ spins: N, matched: false }` | Gave up at `maxSpins` or `timeoutMs` without triggering |

**Why this is needed (vs. `spin()`):** the bridge's regular `spin()` blocks until `AppState.IDLE` returns — which, for a bonus-triggering spin, means *after* the entire Hold & Spin has played out (~30–50s). That swallows the trigger moment. `spinUntilBonus` fires `onSpinClicked()` directly and races bonus-entry against spin-complete via a custom `_raceBonusOrSpinComplete` poll, so it can resolve mid-RESULT processing the instant `_isOnBonusGame` flips true.

**Code path:**
```
loop until matched or limit hit:
  await waitUntilApplicable('spin')        ← popup/animation gates
  collector.emit(SPIN_REQUEST, ...)        ← same telemetry as spin()
  gameController.onSpinClicked()           ← fire, do NOT await IDLE
  await _raceBonusOrSpinComplete(perSpinTimeoutMs)
       → 'bonus'    : _isOnBonusGame === true   → return { matched: true }
       → 'complete' : state cycled IDLE→BETTING→RESULT→IDLE without bonus → loop
```

`_raceBonusOrSpinComplete` is a single polling promise (50 ms interval) that resolves whichever outcome lands first — avoids the dangling-promise pitfall of `Promise.race([bonusEntered, backToIdle])`.

**State flow (per spin in the loop):**
`idle` → `betting` → `result`
- If `_isOnBonusGame` flips true during `result` handlers → `spinUntilBonus` resolves with `matched: true`; the bonus continues to auto-spin in the background.
- Otherwise the spin returns to `idle` and the loop fires the next one.

**Usage:**

```ts
// Trigger a bonus, then assert what we entered with
const r = await bridge.spinUntilBonus({ maxSpins: 100 });
expect(r.matched).toBe(true);

const snap = bridge.getSnapshot();
expect(snap.isOnBonusGame).toBe(true);
expect(snap.spinState).toBe('START_BONUS');
expect(snap.coinPositions.length).toBeGreaterThanOrEqual(3);
expect(snap.freeSpins).toBe(3);

// Optional: let the background bonus play out before returning
await bridge.waitForIdle(120_000);
```

**Common pitfalls:**

| Pitfall | Why / fix |
|---|---|
| Loop times out though the server clearly returned a bonus | The bonus may have been entered via a path that doesn't flip `_isOnBonusGame` (e.g. **promo-triggered** Hold & Spin — `TransitionSpinStateHandler` has no `PROMO` branch). Assert via `spin_result` event with `spinState === 'START_BONUS'` instead. |
| Bonus completed before you could read state | `getSnapshot()` is synchronous and runs in the same JS microtask as the resolution — read it *immediately* after `await spinUntilBonus()`, before any other `await`. For race-proof reads, capture the `spin_result` event from `drainEvents()` instead. |
| `matched: false` with low spin count | Server paytable likely has low COIN weight — boost `BaseGame.ReelDistributions[*].Reels[*].COIN` to make triggers frequent, or collapse a column to COIN-exclusive for guaranteed triggers. See [`paytable.md` §3.1](./paytable.md). |

**Related:** `spinUntil(predicate, options?)` — generic version that lets the spin complete fully then evaluates a predicate against the snapshot. Use it for non-bonus conditions (e.g. balance threshold, specific win type). It does NOT have the racing behaviour above; each spin runs to IDLE before the predicate is checked.

> **Source:** `assets/src/test/ZeusTestBridge.ts` — `spinUntilBonus`, `_raceBonusOrSpinComplete`; `assets/src/test/TestBridgeBase.ts` — `SpinUntilOptions`, `SpinUntilResult`.

---

### 2.23 `startEventCapture(label)` / `endEventCapture(label)`

Generic, label-keyed event recorder — runs alongside `startEventCollection()` / `drainEvents()` **without touching the main collector**. Use this for the **interactive** test pattern where the test inspects state, takes screenshots, or interacts with the game mid-scenario and stops recording when ready.

| | |
|-|-|
| **Emulates** | n/a — recorder lifecycle, not a user action |
| **Parameter** | `label: string` — caller-chosen key. Any string; the same value is required to end the capture. |
| **Precondition** | none (`start` with an existing label resets that label's buffer; `end` is idempotent — returns `[]` if no capture is active under the label) |
| **Returns** | `start`: `void`; `end`: `CollectedEvent[]` containing every bridge-emitted event from start to end |

**Three-layer architecture.** The capture primitive is the bottom layer of a deliberate naming convention QA can rely on:

| Layer | Methods | Concern |
|---|---|---|
| Capture | `start/endEventCapture(label)` | Generic — collects every bridge-emitted event under a named buffer |
| Filter | `filterTo*(events)` — see §3.8 | Pure functions that slice a captured array into a meaningful window (one bonus, one Buy Bonus round, one spin, …) |
| Assert | `verify*(events)` — see §3.9 | Pure invariant checks returning structured violation reports |

**Independence guarantee.** Capture buffers are owned by the bridge in a `Map<label, buffer>`. The main `_collector` is **never bypassed, never drained, never reset by any capture**. Replay / telemetry / any other consumer of `startEventCollection()` / `drainEvents()` continues receiving every event exactly as before.

**Concurrent labels.** Any number of captures can run at once. Each one has its own buffer and its own `t = 0` baseline. Useful for nested or overlapping observation windows.

**Scope.** Events emitted by the **bridge** (`spin_request`, `spin_result`, `balance_change`, `turbo_change`, `free_spin`, `socket_send`, `socket_recv`) are mirrored into all active captures. Events emitted by `EventCollector`'s own internal hooks (`state_change`, `button_state`, `bonus_transition`, `error`) are captured only by the main collector and don't appear in named-capture buffers. The bonus analysers in §3.9 don't need them.

**Code path:**
```
startEventCapture(label):
  _eventCaptures.set(label, { events: [], startTime: now() })

bridge emits event (via the private _emit wrapper):
  _collector.emit(...)                       ← main collector, unchanged
  for each (label, cap) in _eventCaptures:
    cap.events.push({ t: now - cap.startTime, type, data })

endEventCapture(label):
  cap = _eventCaptures.get(label)
  if !cap: return []
  _eventCaptures.delete(label)
  return cap.events
```

**Usage (interactive bonus pattern):**

```ts
bridge.startEventCapture('bonus');
await bridge.spinUntilBonus({ maxSpins: 50 });

// Inspect, screenshot, click, interact freely — the capture keeps
// collecting events in the background.
console.log(bridge.getSnapshot().coinPositions);
bridge.skipBonusTransition();
await bridge.waitForIdle();

const raw = bridge.endEventCapture('bonus');
const events = bridge.filterBonusEvent(raw);   // see §3.8
// → pass to verifyHoldSpin* analysers in §3.9
```

> **Source:** `assets/src/test/ZeusTestBridge.ts` — `startEventCapture`, `endEventCapture`, `_emit` wrapper.

---

### 2.24 `captureHoldSpinBonus(options?)`

Convenience wrapper for the **wait-for-end** case — one call triggers a bonus, waits for it to fully resolve, and returns the sliced events. Internally composes the §2.23 primitives with `spinUntilBonus()`, `waitForIdle()`, and `filterBonusEvent()` under the label `'captureHoldSpinBonus'`.

| | |
|-|-|
| **Emulates** | Press spin repeatedly until a bonus triggers, then wait for the bonus to fully play out |
| **Precondition** | `canDo('spin').ok` (checked by the underlying `spinUntilBonus`) |
| **Parameter** | `options?: SpinUntilOptions` — `{ maxSpins?, timeoutMs?, perSpinTimeoutMs? }`, same defaults as `spinUntilBonus` |
| **Returns** | `Promise<HoldSpinBonusCapture>` — `{ triggered, spinsToTrigger, events, respinCount }` |

**Return shape:**

| Field | Meaning |
|---|---|
| `triggered` | `true` if a bonus actually fired within the budget; `false` if `maxSpins`/`timeoutMs` ran out |
| `spinsToTrigger` | Number of spins it took before the bonus triggered |
| `events` | The `spin_result` events from `START_BONUS` through `END_BONUS`, inclusive |
| `respinCount` | `events.filter(e => e.type === 'spin_result').length` |

**Use this when:** the test only wants to assert on a complete bonus (the three §3.9 analysers). For mid-bonus interaction, use the §2.23 `startEventCapture` / `endEventCapture` pair directly.

**Internally:** `startEventCapture('captureHoldSpinBonus')` → `spinUntilBonus(options)` → `waitForIdle()` → `endEventCapture('captureHoldSpinBonus')` → `filterBonusEvent(events)`. Every step is a named primitive you can also call directly.

**Cleanup guarantee:** if `spinUntilBonus` fails or any step throws, the capture is ended before the error propagates — never leaves a dangling active session.

**Usage:**

```ts
const capture = await bridge.captureHoldSpinBonus({ maxSpins: 50 });
expect(capture.triggered).toBe(true);

const events = capture.events;
expect(bridge.verifyHoldSpinCoinsHeld(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinResetOnNewCoin(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinCountDown(events).valid).toBe(true);
```

> **Source:** `assets/src/test/ZeusTestBridge.ts` — `captureHoldSpinBonus`; `assets/src/test/TestBridgeBase.ts` — `HoldSpinBonusCapture`.

---

## 3. Queries (Read-Only State Inspection)

### 3.1 Game State

| Method | Returns | Source |
|--------|---------|--------|
| `getAppState()` | `string` — `'idle'`, `'betting'`, `'result'`, `'error'`, etc. | `ApplicationStateManager.getCurrentState()` |
| `getSpinState()` | `string` — `'NORMAL'`, `'START_BONUS'`, `'IN_BONUS'`, `'END_BONUS'`, `'START_GAME'`, `'PROMO'` (enum name, not numeric) | `SpinState[spinResult.state]` |
| `getBalance()` | `number` — current player balance | `GameLogic.balance` |
| `getBetAmount()` | `number` — current bet amount | `GameLogic.currentBetAmount` |
| `getBetIndex()` | `number` — index into denominations array | `GameLogic.currentIndexBet` |
| `getDenominations()` | `number[]` — all available bet levels | `GameLogic.denominations` |
| `getFreeSpinsRemaining()` | `number` — remaining free spins in bonus | `GameLogic.freeSpins` |
| `getAutoSpinsRemaining()` | `number` — remaining auto spins (`Infinity` for continuous) | `GameLogic.numberAutoSpin` |
| `getLastPayout()` | `number` — payout from last completed spin | `GameLogic.betResult?.payout` |
| `getLastWinType()` | `string` — `'LOSE'`, `'SLIM_WIN'`, `'SMALL_WIN'`, `'MEDIUM_WIN'`, `'BIG_WIN'`, `'MEGA_WIN'`, `'ULTRA_WIN'` | `GameLogic.spinResult.typeWin` |
| `isTurboActive()` | `boolean` — is turbo mode on | `offTurboButton.isChecked` |
| `isSoundEnabled()` | `boolean` | `SoundManager.isSoundEnabled()` |
| `isMusicEnabled()` | `boolean` | `SoundManager.isMusicEnabled()` |
| `isPromoMode()` | `boolean` — is promotional game active | `GameLogic.isPromoMode` |

### 3.2 Reel State

| Method | Returns | Source |
|--------|---------|--------|
| `getReelResult()` | `number[][]` — 3x3 grid of symbol IDs from server response | `GameLogic.betResult.result.views` |
| `getCoinPositions()` | `CoinPositions[]` — coins on grid with `{row, col, coinValue, type}` | `GameLogic.spinResult.coinPositions` |
| `getPaylines()` | `PayLines[]` — winning lines with `{symbols, payline, payoutRate, winType}` | `GameLogic.betResult.paylines` |

### 3.3 Button Interactability

> All `is*Enabled()` methods delegate to `canDo(action).ok` — they reflect the
> **full** applicability picture (popups, big-win celebrations, bonus mode,
> bet-pending, hit-test), not just the button's `interactable` flag.
> For the underlying blocker reason, call `canDo(action)` directly.

| Method | Returns | Equivalent to |
|--------|---------|---------------|
| `isSpinEnabled()` | `boolean` | `canDo('spin').ok` |
| `isBuyBonusEnabled()` | `boolean` | `canDo('openBuyBonusPopup').ok` |
| `isAutoEnabled()` | `boolean` | `canDo('openAutoPopup').ok` |
| `isNextBetEnabled()` | `boolean` | `canDo('nextBet').ok` |
| `isPreviousBetEnabled()` | `boolean` | `canDo('previousBet').ok` |
| `isStopAutoVisible()` | `boolean` | `stopAutoButton.isChecked` (visible when auto-spin is running) |

### 3.4 Game Info

| Method | Returns | Source |
|--------|---------|--------|
| `getGameInfo()` | `GameInfo` — full server game config including settings, pending state, paytable | `GameLogic.gameInfo` |
| `getCurrency()` | `string` — e.g., `'USD'`, `'EUR'` | `GameLogic.gameInfo.currency` |
| `getPaytable()` | `Paytable[]` — symbol payout rates | `GameLogic.gameInfo.paytable` |
| `getFreeSpinPackages()` | `FreeSpinPackage[]` — available bonus packages with `{name, freeSpins, price}` | `GameLogic.gameInfo.freeSpinPackages` |

### 3.5 UI Display Values

These return what the player actually sees on screen — the formatted label text, not raw numbers.

| Method | Returns | Source |
|--------|---------|--------|
| `getDisplayBalance()` | `string` — formatted balance (e.g., `"1,000.00"`) | `balanceLabel.string` |
| `getDisplayBet()` | `string` — formatted bet (e.g., `"1.00"`) | `betLabel.string` |
| `getDisplayWin()` | `string` — formatted win (e.g., `"25.50"`) | `winLabel.string` |

### 3.6 Snapshot

`getSnapshot()` returns a full state dump covering both gameplay state and
applicability inputs:

**Gameplay fields**
`appState`, `spinState`, `balance`, `betAmount`, `betIndex`, `denominations`,
`freeSpins`, `autoSpins`, `lastPayout`, `lastWinType`, `turbo`, `sound`,
`music`, `promo`, `reelResult`, `coinPositions`

**Applicability fields** (raw inputs `canDo()` reads)

| Field | Type | Meaning |
|---|---|---|
| `isPopupVisible` | `boolean` | Any modal popup is blocking the scene |
| `visiblePopups` | `string[]` | Names of currently visible popups, e.g. `['buyBonus', 'bigWin']` |
| `isShowingBigWin` | `boolean` | BigWinPopup celebration in progress |
| `isShowingTotalWin` | `boolean` | BonusResultPopup in progress |
| `isOnBonusGame` | `boolean` | Hold & Spin bonus mode active |
| `isHoldSpinActive` | `boolean` | User is long-pressing spin (hold-spin loop) |
| `isLockSpinButton` | `boolean` | `GameplayScene._isLockSpinButton` |
| `isBetPending` | `boolean` | Bet sent, response not yet received |
| `sceneReady` | `boolean` | `GameplayScene.start()` resolved |
| `busyOps` | `string[]` | Bridge-tracked animations in flight, e.g. `['transitionToHoldSpin']` |

### 3.7 Applicability Queries

| Method | Returns | Purpose |
|---|---|---|
| `canDo(action)` | `Applicability` | `{ ok, reason?, hitTested? }` — "can I do this right now?" Synchronous, no waiting. |
| `diagnoseHitTest()` | `{ timestamp, sceneReady, appState, visiblePopups, buttons }` | Per-button hit-test report covering spin, stop, auto, buyBonus, nextBet, previousBet, trophy. See [bridge-applicability.md §7](./bridge-applicability.md#7-diagnosehittest--runtime-verification). |
| `hitTestDebug` (property) | `boolean` | When `true`, logs `console.warn` on every blocked hit-test. Off by default. |

**Example:**

```ts
const result = bridge.canDo('spin');
// { ok: false, reason: 'popup:buyBonus' }
//   or
// { ok: true, hitTested: true }

const report = bridge.diagnoseHitTest();
// report.buttons.spin === { result: 'blocked', target: 'nodeSpinButton',
//                           blocker: 'BuyBonusPopup', center: {x, y} }
```

For the full reason-token vocabulary, action list, and recipes,
see [`bridge-applicability.md`](./bridge-applicability.md).

### 3.8 Event filters

Pure functions that slice a captured event array into a meaningful window. The middle layer of the **capture / filter / assert** convention (see §2.23). Filters take an events array, return a (possibly empty) events array — no side effects, no buffer touching.

| Method | Returns | Purpose |
|---|---|---|
| `filterBonusEvent(events)` | `CollectedEvent[]` | Pull the bonus (free-spin) `spin_result` events out of a captured array — the contiguous run from `START_BONUS` through `END_BONUS`. Returns `[]` if no `START_BONUS` is present. Output is the **canonical input** to the three §3.9 verifiers. |

**Usage:**

```ts
bridge.startEventCapture('bonus');
await bridge.spinUntilBonus({ maxSpins: 50 });
await bridge.waitForIdle();
const raw = bridge.endEventCapture('bonus');

// Bonus events only — ready for the §3.9 verifiers:
const events = bridge.filterBonusEvent(raw);
expect(bridge.verifyHoldSpinCoinsHeld(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinResetOnNewCoin(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinCountDown(events).valid).toBe(true);
```

**Adding more filters** is a one-method-per-scenario extension — the capture layer doesn't change. Likely future additions: `filterBuyBonusEvent(events)`, `filterSingleSpinEvent(events, roundId)`.

> **Source:** `assets/src/test/ZeusTestBridge.ts` — `filterBonusEvent`.

### 3.9 Hold & Spin bonus invariant analysers

> **Required for every free-spin test.** Any test that exercises the
> Hold & Spin (free-spin) bonus — whether the bonus is triggered naturally
> from the base game, purchased via Buy Bonus, or entered through any
> other path — **must run all three of these analysers** on the captured
> bonus events to assert that the free-spin mechanic itself is working.
>
> A free-spin test that does not run these checks is not actually testing
> the free-spin behaviour — it's only confirming that *something* happened.
> The three invariants below are what *defines* the Hold & Spin mechanic;
> failing any one of them means the bonus is broken.

Three pure analysers — the top layer of the **capture / filter / assert** convention (§2.23). **No side effects** — they never touch the event collector, never drain anything.

**Input contract.** All three methods expect `events` to be the output of [`filterBonusEvent(...)`](#38-event-filters) — a bonus episode slice (the contiguous `spin_result` events from `START_BONUS` through `END_BONUS`). The §2.24 `captureHoldSpinBonus()` wrapper already returns its `events` field in this exact shape, so its output can be passed straight in. For the interactive pattern, run `bridge.filterBonusEvent(raw)` on whatever `endEventCapture()` returned and pass the result.

| Method | Asserts the rule that… | Returns |
|---|---|---|
| `verifyHoldSpinCoinsHeld(events)` | **Coins stay put.** Every coin present in event `N` is still at the same `(col, row)` in event `N+1` of the bonus. Detects `missing` (coin disappeared) and `tier_changed` (multiplier mutated). | `CoinHoldCheckResult` |
| `verifyHoldSpinFreespinResetOnNewCoin(events)` | **A new coin resets the counter to 3.** When a re-spin lands new coins (`coinCount` increased), `freeSpinInfo.freespins` is `3` in that event. Pairs where the bonus has ended (`freeSpinInfo === null`) are skipped. | `FreespinResetCheckResult` |
| `verifyHoldSpinFreespinCountDown(events)` | **No new coin → counter decrements by 1.** When a re-spin lands no new coin (`coinCount` unchanged), `freespins` equals the previous event's `freespins − 1`. Same exit clause. | `FreespinCountDownCheckResult` |

Together they cover the **three core invariants of the Hold & Spin
mechanic** — coin persistence, counter reset on success, counter
decrement on failure. Any free-spin test should run all three; a passing
result on all three is what proves the mechanic is intact for that
bonus run.

**Shared result shape:**

```ts
{
    valid: boolean;            // true iff bonusFound && violations.length === 0
    bonusFound: boolean;       // false if no START_BONUS event in input
    respinCount: number;       // spin_result events in the bonus episode
    violations: Violation[];   // per-rule shape, each with a human-readable `detail`
}
```

Every violation carries a `detail` string ready to print as an assertion failure message — e.g. `"Coin at (col=0,row=1) present in respin 2 (IN_BONUS) but missing in respin 3 (IN_BONUS)"`, `"New coin landed in respin 3 (IN_BONUS): coinCount 5→7; expected freespins=3, got 2"`, `"No new coin in respin 4 (IN_BONUS): coinCount stayed at 5; expected freespins=1, got 2"`.

#### Canonical recipes

**Recipe A — wait-for-end (simple):**

```ts
const capture = await bridge.captureHoldSpinBonus({ maxSpins: 50 });
expect(capture.triggered).toBe(true);

const events = capture.events;
expect(bridge.verifyHoldSpinCoinsHeld(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinResetOnNewCoin(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinCountDown(events).valid).toBe(true);
```

**Recipe B — interactive (capture / filter / assert manually):**

> **Important:** the bonus's total-win popup blocks `IDLE`. The post-bonus
> sequence must be `waitForTotalWinPopup → skipTotalWin → waitForIdle`,
> NOT `waitForIdle` alone. See §4 for details.

```ts
bridge.startEventCapture('bonus');
await bridge.spinUntilBonus({ maxSpins: 50 });
// ... inspect, screenshot, interact freely ...

await bridge.waitForTotalWinPopup();   // bonus has ended; popup is up
bridge.skipTotalWin();                  // dismiss it
await bridge.waitForIdle();             // now IDLE fires

const raw = bridge.endEventCapture('bonus');
const events = bridge.filterBonusEvent(raw);

expect(bridge.verifyHoldSpinCoinsHeld(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinResetOnNewCoin(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinCountDown(events).valid).toBe(true);
```

**Python / Playwright end-to-end:**

```python
capture = page.evaluate(
    "(opts) => window.__gameTestBridge.captureHoldSpinBonus(opts)",
    {"maxSpins": 50},
)
assert capture["triggered"], f"No bonus in {capture['spinsToTrigger']} spins"

events = capture["events"]
for fn in ("verifyHoldSpinCoinsHeld",
           "verifyHoldSpinFreespinResetOnNewCoin",
           "verifyHoldSpinFreespinCountDown"):
    result = page.evaluate(
        f"(evs) => window.__gameTestBridge.{fn}(evs)", events
    )
    assert result["valid"], f"{fn} failed:\n" + "\n".join(
        v["detail"] for v in result["violations"]
    )
```

> **Source:** `assets/src/test/ZeusTestBridge.ts` — `verifyHoldSpinCoinsHeld`, `verifyHoldSpinFreespinResetOnNewCoin`, `verifyHoldSpinFreespinCountDown`, `_extractBonusEpisode`; `assets/src/test/TestBridgeBase.ts` — `CoinHoldCheckResult`, `FreespinResetCheckResult`, `FreespinCountDownCheckResult` and their `*Violation` interfaces.

---

## 4. Wait Helpers

| Method | Behavior |
|--------|----------|
| `waitForState(state: string, timeoutMs?: number)` | Resolves when AppState matches the target. Rejects on timeout (default 30s). Uses `ApplicationStateManager.onStateChanged` callback. |
| `waitForIdle(timeoutMs?: number)` | Shorthand for `waitForState('idle')`. Most commonly used after actions. |
| `waitForSpinComplete(timeoutMs?: number)` | Calls `spin(timeoutMs)`. Default 60s. Single call for the most common test operation. |
| `waitForUISettle(delayMs?: number)` | Waits for UI animations to complete after IDLE. Default 1000ms. See timing note below. |
| `waitUntilApplicable(action, stableMs?, timeoutMs?)` | Resolves when `canDo(action).ok` has stayed true continuously for `stableMs` (default 250 ms). Polls every 50 ms. Default `timeoutMs` is 30 000. Timeout error includes the last blocking reason. **Use this before every action** — it absorbs transition tails that flicker between IDLE-flip and animation-complete. |
| `waitForTotalWinPopup(timeoutMs?: number)` | Resolves the moment the bonus total-win popup appears (`snapshot.isShowingTotalWin === true`). Polls every 50 ms. Default 60 000 ms. **Use this after a Hold & Spin / Buy Bonus, before `waitForIdle()`** — the popup blocks IDLE. See "Required post-bonus sequence" below. |

### Required post-bonus sequence — `waitForIdle()` alone DEADLOCKS

After a Hold & Spin or Buy Bonus, the `BonusResultPopup` ("press anywhere to continue") sits between the `END_BONUS` `spin_result` event and the `IDLE` state. The handler chain runs:

```
END_BONUS spin_result → showResultHoldSpin (coin animation)
                       → showPopupTotalWinBonusGame  ← awaits popup dismiss
                       → transitionToBaseGame
                       → IDLE
```

Until the popup is dismissed, `appState` stays at `'result'` and `IDLE` never fires. A test that calls `waitForIdle()` directly after the bonus will time out.

**Correct sequence:**

```ts
await bridge.spinUntilBonus({ maxSpins: 50 });
// ... interactive: inspect, screenshot, run verifiers ...

await bridge.waitForTotalWinPopup();   // popup appears, appState still 'result'
//   At this point you can also assert getDisplayWin() / read totals from the popup.
bridge.skipTotalWin();                  // dismiss it (or call dismissPopups())
await bridge.waitForIdle();             // now IDLE can fire
```

**Why §2.24 `captureHoldSpinBonus()` doesn't need this manually:** its internal `waitForIdle()` is reached because the popup self-dismisses after its animation (`BonusResultPopup.show()` calls `this.skip()` at the end). If your environment has a longer popup hold or the auto-dismiss is disabled, prefer the explicit sequence above.

**`waitUntilApplicable` vs `waitForIdle`:**

`waitForIdle` only watches `AppState`. `waitUntilApplicable` watches the full
applicability picture — popups, big-win celebration, busy ops, hit-test, etc.
For tests that want the safest possible "screen is fully ready" gate, prefer:

```ts
await bridge.waitUntilApplicable('spin');
await bridge.spin();
```

See [`bridge-applicability.md`](./bridge-applicability.md#42-waituntilapplicableaction-stablems-timeoutms--quiescence-wait)
for the polling cadence and timeout semantics.

**Important — IDLE fires before UI animations complete:**

`transitionTo(IDLE)` means "game logic is ready for next action" — **not** "screen is visually idle". For non-big wins, the handler chain awaits reel stop and payline highlight, but the **win counter animation** and **persistence effects** are fire-and-forget:

```
handleSpinResult()
  ├─ await SpinHandler              ← reel animation (awaited ✅)
  ├─ await WinLinesHandler          ← payline highlight (awaited ✅)
  ├─ BigWinHandler:
  │   ├─ BIG/MEGA/ULTRA → await showBigWin()   (awaited ✅)
  │   └─ SLIM/SMALL/MEDIUM → showBigWin()      (fire-and-forget ⚠️)
  ├─ await TransitionSpinStateHandler
  └─ maybeHandleNextSpin() → transitionTo(IDLE)
                              ↑ IDLE fires here
                              ↓ win counter still animating (~500-1000ms)
```

This is **by design** — during auto-spin, the game doesn't wait for small win animations to keep the pace fast. For big wins, it does wait because the celebration popup needs user interaction.

**When to use `waitForUISettle()`:**

Call it after `waitForIdle()` when your test needs the screen to visually match (e.g. screenshots, reading `getDisplayWin()`):

```javascript
await bridge.spin();                    // resolves at IDLE
await bridge.waitForUISettle();         // wait for win animation to finish
const win = bridge.getDisplayWin();     // now reads the final displayed value
```

Don't use it between auto-spins — the game handles that automatically. Only needed for the **last** spin before visual assertions.

**Important — `waitForIdle()` during auto-spin and Hold&Spin:**

The game does **not** transition to IDLE between consecutive spins in these modes:
- **Auto-spin:** `BETTING → RESULT → BETTING → RESULT → ...` — IDLE only after the last spin
- **Hold&Spin bonus:** `BETTING → RESULT → BETTING → RESULT → ...` — IDLE only after bonus ends
- **Free spins (pending):** same continuous loop

`waitForIdle()` will resolve only when the entire sequence completes. To observe individual spins within these modes, use event collection (Section 5).

---

## 5. Event Collection (for Replay Viewer)

### 5.1 `startEventCollection()`

Starts capturing game events with timestamps. Registers hooks on:
- `ApplicationStateManager.onStateChanged` → `state_change` events
- Every `AppState.RESULT` transition → `spin_result` events (one per spin **and per bonus respin**)
- Button interactability changes → `button_state` events

### 5.2 `stopEventCollection()`

Stops capturing events. Hooks are unregistered.

### 5.3 `drainEvents()`

Returns all collected events since last drain, then clears the buffer.

**Returns:** `CollectedEvent[]`

```typescript
interface CollectedEvent {
    t: number;        // seconds since collection start, 3 decimal places
    type: string;     // event category (see table below)
    data: Record<string, any>;
}
```

### Event Types

| Type | Hook Point | Payload |
|------|-----------|---------|
| `state_change` | `ApplicationStateManager.onStateChanged` | `{ from, to }` |
| `spin_request` | Bridge `spin()` action | `{ stake, betIndex, devMode }` |
| `spin_result` | Every `AppState.RESULT` transition (fires once per spin **and per bonus respin**) | `{ spinState, reels, paylines, payout, stake, typeWin, freeSpinInfo, coinCount, coinPositions }` |
| `balance_change` | After spin result | `{ balance, previous }` |
| `socket_send` | `SignalRConnection.sendEvent` | `{ event }` |
| `socket_recv` | `SignalRConnection.subscribeEvent` | `{ event, status }` |
| `button_state` | Sampled at state changes | `{ spin, auto, buyBonus, nextBet, previousBet }` |
| `free_spin` | `GameLogic.freeSpins` change | `{ remaining }` |
| `bonus_transition` | SpinState changes | `{ spinState, coinCount }` |
| `popup` | Popup `.active` changes | `{ name, active }` |
| `turbo_change` | Turbo toggle | `{ turboActive }` |
| `error` | AppState.ERROR | `{ message }` |

### 5.4 `spin_result` payload (detail)

| Field | Type | Source | Meaning |
|-------|------|--------|---------|
| `spinState` | `string` | client-computed (`getStateGame`) | `NORMAL` \| `START_BONUS` \| `IN_BONUS` \| `END_BONUS` \| `PROMO` — phase of this result |
| `reels` | `number[][]` | server | Final 3×3 board (symbol IDs) |
| `paylines` | `PayLines[]` | server | Winning lines (empty during bonus) |
| `payout` | `number` | server | Payout for this spin/respin |
| `stake` | `number` | server | Stake (0 during bonus respins) |
| `typeWin` | `string` | client | `LOSE`…`ULTRA_WIN` |
| `freeSpinInfo` | `{ freespins, totalFreespin, accumulateWin }` \| `null` | server | Respins remaining; `null` once the bonus has ended |
| `coinCount` | `number` | `coinPositions.length` | Coins on the board (0–9) |
| `coinPositions` | `CoinPositions[]` | server (type enriched client-side) | Coin cells with `{ row, col, coinValue, type, multiplier }` |

### 5.5 Observing a Hold & Spin (Free Spin) bonus

The bonus plays as an automatic loop and `spin()` blocks until it fully ends,
so you cannot step it. Instead, capture the `spin_result` timeline — one event
fires per respin — and reconstruct the bonus afterwards. `spinState` segments
the episode: the run from `START_BONUS` through `END_BONUS`.

```javascript
bridge.startEventCollection();

// With coins boosted server-side, spin until a bonus occurs and resolves.
let sawBonus = false;
while (!sawBonus) {
    await bridge.spin();                       // blocks through the whole bonus
    const events = bridge.drainEvents();
    const results = events.filter(e => e.type === 'spin_result');
    const start = results.findIndex(e => e.data.spinState === 'START_BONUS');
    if (start === -1) continue;                // normal spin, try again

    const end = results.findIndex((e, i) => i >= start && e.data.spinState === 'END_BONUS');
    const bonus = results.slice(start, end + 1).map(e => e.data);
    sawBonus = true;

    // Per-respin facts for ZZ-30 → ZZ-37 assertions:
    //   bonus[0].coinCount            → trigger coin count (ZZ-30: 3–8, ZZ-31: 9)
    //   bonus[i].freeSpinInfo.freespins → respins remaining
    //   coinCount up + freespins===3  → new coin reset (ZZ-34)
    //   coinCount same + freespins-1  → count-down (ZZ-35)
    //   last.freeSpinInfo===null      → ended; coinCount<9 = out-of-spins (ZZ-36), ===9 = early end (ZZ-37)
    //   coinPositions[N] ⊆ coinPositions[N+1] → coins held (ZZ-32)
}
bridge.stopEventCollection();
```

For **ZZ-38** (controls disabled during the bonus), don't use the blocking
`spin()` — use `spinUntilBonus()` to pause at bonus entry, assert, then resume:

```javascript
await bridge.spinUntilBonus();                 // stops with isOnBonusGame === true
// canDo returns { ok: false, reason: 'bonus_in_progress' } for these:
['nextBet', 'previousBet', 'selectBet', 'openAutoPopup'].forEach(a =>
    console.assert(bridge.canDo(a).ok === false));
await bridge.waitForIdle();                     // let the bonus finish
```

> The 9-coin cases (**ZZ-31** trigger-with-9, **ZZ-37** fill-to-9) are rare
> under random respins. Since the trigger lever is server-side config, prefer
> a **server-forced** 9-coin scenario over waiting on RNG.

---

## 6. State Machine Reference

### Application State Machine

```
INITIAL → CONNECTING → LOADING → READY
                                   ↓
                                  IDLE ⇄ BETTING ⇄ RESULT
                                   ↕        ↕         ↕
                                 PROMO    ERROR     ERROR
                                   ↕        ↕
                                 IDLE   RECONNECTING → IDLE
```

**Valid transitions** (from `ApplicationStateManager.ts:11-22`):

| From | To |
|------|----|
| `initial` | `connecting`, `error` |
| `connecting` | `loading`, `error`, `idle` |
| `loading` | `ready`, `error` |
| `ready` | `idle`, `betting`, `error`, `promo` |
| `idle` | `betting`, `error`, `promo` |
| `betting` | `result`, `error`, `promo`, `idle` |
| `result` | `idle`, `betting`, `error`, `promo` |
| `error` | `connecting`, `reconnecting`, `idle` |
| `reconnecting` | `idle`, `error` |
| `promo` | `idle`, `error` |

### Spin State Machine (Bonus Flow)

```
NORMAL ──(3+ coins land)──→ START_BONUS
                                ↓
                            IN_BONUS ←─(re-spins with coins remaining)
                                ↓
                           END_BONUS ──→ NORMAL
```

| SpinState | Meaning |
|-----------|---------|
| `NORMAL` | Regular base game spin |
| `START_BONUS` | First spin that triggered Hold&Spin |
| `IN_BONUS` | Re-spins during Hold&Spin |
| `END_BONUS` | Final Hold&Spin spin (no re-spins left or grid full) |
| `PROMO` | Promotional game mode spin |

---

## 7. Symbol IDs

From `Constants.ts:133-146` — `SlotSymbol` enum:

| Symbol | ID | Name | Description |
|--------|----|------|-------------|
| `ERROR` | -1 | Error | Invalid/error symbol |
| `A` | 1 | A | Ace card symbol |
| `J` | 2 | J | Jack card symbol (disabled in config, weight 0) |
| `Q` | 3 | Q | Queen card symbol |
| `K` | 4 | K | King card symbol |
| `P1` | 5 | P1 | Premium 1 — Athena |
| `P2` | 6 | P2 | Premium 2 — Helmet |
| `P3` | 7 | P3 | Premium 3 — Zeus |
| `P4` | 8 | P4 | Premium 4 — Shield |
| `WILD` | 9 | Wild | Golden Eagle — substitutes for all except Coin |
| `COIN` | 13 | Coin | Hold&Spin trigger — 3+ coins start bonus |
| `COIN_EMPTY` | -1 | CoinEmpty | Empty coin slot in bonus mode |

### Coin Types (in Hold&Spin)

| Type | ID | Meaning |
|------|----|---------|
| `EMPTY` | 0 | No coin |
| `NORMAL` | 1 | Regular coin |
| `MINOR` | 2 | Minor jackpot coin |
| `MAJOR` | 3 | Major jackpot coin |
| `GRAND` | 4 | Grand jackpot coin |

---

## 8. Win Type Thresholds

From `Interface.ts:139-147` — multiplier of bet amount:

| Win Type | Multiplier Range | Visual |
|----------|-----------------|--------|
| `LOSE` | payout = 0 | No animation |
| `SLIM_WIN` | 0 < x < 1 | Minimal highlight |
| `SMALL_WIN` | 1 ≤ x < 2.5 | Win line animation |
| `MEDIUM_WIN` | 2.5 ≤ x < 10 | Enhanced win animation |
| `BIG_WIN` | 10 ≤ x < 25 | Big win popup |
| `MEGA_WIN` | 25 ≤ x < 50 | Mega win popup + trophy |
| `ULTRA_WIN` | x ≥ 50 | Ultra win popup + trophy |

---

## 9. Game Configuration

From `Constants.ts:8-13`:

| Config | Value |
|--------|-------|
| Grid size | 3 rows x 3 columns |
| Pay lines | 5 |
| Decimal places | 2 |
| Auto-spin packages | `[10, 20, 30, 50, Infinity]` |

---

## 10. Bridge Registration & Activation

| | |
|-|-|
| **Activation** | URL parameter `?bridge=1` |
| **Registration** | `window.__gameTestBridge = bridgeInstance` |
| **Called during** | `LoadingScene.onGameReady()` at `AppState.READY` — before the user clicks Start |
| **Production safety** | Zero overhead when parameter absent — bridge code never instantiated |

### Activation Timing

The bridge is registered at `AppState.READY`, which is after:
- `GlobalContext.boot()` completes (connection established, game info received)
- All game assets are loaded
- The Start button is shown

This means:
- Playwright can detect `window.__gameTestBridge` before clicking Start
- Query methods that only need `GameLogic` (e.g., `getGameInfo()`, `getDenominations()`, `getBalance()`) work immediately
- Action methods and UI queries that need `GameplayScene` (e.g., `spin()`, `isSpinEnabled()`, `getDisplayBalance()`) require clicking Start first — they throw `"GameplayScene is not yet initialised"` if called too early

### Access from Playwright/test framework:
```javascript
// Wait for bridge to be available (at READY state, before Start click)
await page.waitForFunction(() => window.__gameTestBridge);

// Query game info before clicking Start
const info = await page.evaluate(() => window.__gameTestBridge.getGameInfo());
const balance = await page.evaluate(() => window.__gameTestBridge.getBalance());

// Start the game — clicks Start button and waits for idle
await page.evaluate(() => window.__gameTestBridge.startGame());

// Full bridge API is now available
await page.evaluate(() => window.__gameTestBridge.spin());
const snapshot = await page.evaluate(() => window.__gameTestBridge.getSnapshot());
```

---

## 11. What NOT to Expose

These internal methods must **never** be callable from tests:

| Internal Method | Why Not |
|-----------------|---------|
| `GameLogic.nextBet()` | Skips UI label update, denomination popup sync |
| `GameLogic.numberAutoSpin = X` | Skips UI button state update, doesn't trigger first spin |
| `ApplicationStateManager.transitionTo()` | Bypasses validation, creates impossible game states |
| `SlotGameApplication.spin()` | Skips idle check, balance check, UI deactivation sequence |
| `GameLogic.betResult = X` | Skips state transition, entire handler chain, all animations |
| `GameLogic._balance = X` | Balance is server-authoritative — direct set will desync |
| `GamePlayScene.onBetting()` | Internal callback, not a user action |
| `GameController.spin()` (private) | Skips the idle + promo popup validation in `onSpinClicked()` |

**The only exception:** `setDevModeResult()` injects fake spin outcomes, but it uses the same `DevModeManager` mechanism that real developers use through the DevMode UI panel.

---

## 12. Key Interfaces

### BetResult (server response)

```typescript
interface BetResult {
    paylines: PayLines[];
    id: string;
    roundId: string;
    stake: number;
    payout: number;
    winlose: number;
    coinPositions: CoinPositions[];
    coinCount: number;
    result: { views: { id: number, value: number[] }[] };
    balance: number;
    freeSpinInfo: { freespins: number, totalFreespin: number, accumulateWin: number };
}
```

### SpinResult (processed for display)

```typescript
interface SpinResult {
    state: SpinState;
    reels: number[][];
    paylines: PayLines[];
    coinPositions: CoinPositions[];
    coinCount: number;
    freespins: number;
    typeWin: WIN_TYPE_STATE;
    payout: number;
    stake: number;
    persistenceInfo: { nextLevel: number, type: TypePersistence };
}
```

### CoinPositions

```typescript
interface CoinPositions {
    row: number;     // 0-2
    col: number;     // 0-2
    coinValue: number;
    type: TYPE_COIN; // EMPTY=0, NORMAL=1, MINOR=2, MAJOR=3, GRAND=4
    multiplier?: number;
}
```

### PayLines

```typescript
interface PayLines {
    symbols: number[];
    payline: number;
    payoutRate: number;
    winType: string;
}
```

### GameInfo (server config)

```typescript
interface GameInfo {
    settings: {
        defaultBetLevel: number;
        minBet: number;
        maxBet: number;
        maxPayout: number;
        denominations: number[];
        maxMultiplier: number;
        sessionTimeOut: number;
    };
    pending?: {
        coinPositions: CoinPositions[];
        freeSpins: number;
        stake: number;
    };
    denominations: number[];
    username: string;
    currency: string;
    balance: number;
    betlines: number;
    freeSpinPackages: FreeSpinPackage[];
    paytable: Paytable[];
    promo?: PlayerPromoState;
}
```

---

## 13. Source File Map

| File | Role |
|------|------|
| `assets/src/views/GameplayScene.ts` | All button handlers, event bindings (L165-218), UI state management |
| `assets/src/controllers/GameController.ts` | Public action methods, handler chain orchestration |
| `assets/src/managers/GlobalContext.ts` | Singleton accessors for all managers |
| `assets/src/managers/ApplicationStateManager.ts` | State machine with validated transitions |
| `assets/src/managers/GameLogic.ts` | Game state: balance, bet, freeSpins, spinResult |
| `assets/src/controllers/SlotGameApplication.ts` | SignalR server communication |
| `assets/src/constants/Constants.ts` | AppState, SlotSymbol, GameConfig enums |
| `assets/src/constants/Interface.ts` | All TypeScript interfaces |
| `assets/src/views/BuyBousPopup.ts` | Buy bonus popup interaction flow |
| `assets/submodules/src/views/AutoPopup.ts` | Auto-spin popup interaction flow |
| `assets/src/views/HoldSpinView.ts` | Hold&Spin bonus game interaction |
| `assets/src/views/DevMode/DevModeManager.ts` | Dev mode result injection |
| `assets/submodules/src/views/DenominationsPopup.ts` | Bet denomination selection popup |
| `assets/submodules/src/views/SettingPopup.ts` | Settings/info/history popup |
| `assets/src/networks/SignalRConnection.ts` | WebSocket connection (MessagePack protocol) |
| `assets/src/test/BridgeActivator.ts` | Bridge activation gated by `?bridge=1` URL param |
| `assets/src/test/ZeusTestBridge.ts` | Concrete bridge implementation — actions, queries, `canDo`, hit-test, busy-op wrappers |
| `assets/src/test/TestBridgeBase.ts` | Abstract bridge interface contract — `GameSnapshot`, `Applicability`, `ApplicableAction` |
| `assets/src/test/EventCollector.ts` | Timestamped event capture for replay viewer |
| `assets/src/views/LoadingScene.ts` | Calls `BridgeActivator.activate()` at `AppState.READY` |
| `bridge-applicability.md` | Companion doc — applicability mechanism, reason vocabulary, `diagnoseHitTest` recipes |
| `game-data-model.md` | Companion doc — grid coordinates (row 0 = top, col 0 = left), `BetResult` / `SpinResult` / `CoinPositions` shapes, SpinState machine |
| `paytable.md` | Companion doc — `paytable.json` structure (`BaseGame`, `FreeSpinGame`, `Packages`, `SymbolMultipliers`), reel sampling model, weight-collapsing for deterministic outcomes |
| `paytable.json` | Live paytable math config consumed by the server |
