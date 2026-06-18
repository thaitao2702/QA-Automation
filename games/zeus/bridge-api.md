# Zeus Slot Game — Test Bridge API Reference

> **Purpose:** Comprehensive reference for `ZeusTestBridge` — the E2E test automation bridge.
> All methods emulate real user interactions through the actual UI code path.

---

## 1. Design Philosophy

The bridge exposes two categories:

| Category | Purpose | Mutates State? |
|----------|---------|----------------|
| **Actions** | Emulate what a real user does (click spin, open popup, select bet) | Yes — through the same code path as a real click |
| **Queries** | Read-only state inspection (get balance, get reel state, check buttons) | No |

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

### Idempotency Guarantee

**Every bridge method that registers a hook, subscribes to an event, opens a connection, or starts background work is idempotent.** Calling it N times has the same effect as calling it once. The same applies to "stop / unregister" counterparts — they no-op if nothing is registered.

**Why this is a design principle, not an implementation detail:**

Test runners and harnesses commonly call setup methods more than once per test — page reloads, defensive re-initialisation, setup hooks that fire in both `beforeAll` and `beforeEach`, retry loops, etc. Without idempotency, a second call:

- builds a fresh closure and subscribes it, leaving the original closure **orphaned but still subscribed**;
- the orphaned subscription keeps firing on every event, so `spin_result`, `balance_change`, and other captured events are emitted **twice per game event**;
- analysers consuming those events (e.g. `verifyHoldSpinFreespinCountDown`) see adjacent duplicates and report **false violations**.

This is exactly the failure mode that surfaced in QA test runs and motivated bug fix #4 (see commit history). The doubling is silent — the bridge keeps "working" and tests keep "running", they just lie about what happened.

**Contract for new bridge methods:**

| Method shape | Idempotency requirement |
|---|---|
| `startX()` / `enableX()` / `openX()` that subscribes, polls, patches, or opens | Early-return if already active. Track activation with a single, well-named guard field. |
| `stopX()` / `disableX()` / `closeX()` | Early-return if not active. Clear the guard field on the way out. |
| `registerXHook()` / patching helpers (private) | Same guard pattern. Tracking the handler reference itself doubles as the guard (`if (this._xHandler) return;`). |
| Read-only queries | Naturally idempotent. No guard needed. |

**Examples currently honouring the contract:**
- `startEventCapture(label)` — keyed by label; calling with an existing label deliberately **resets** that label's buffer (documented behaviour, not a guard violation)
- `endEventCapture(label)` — returns `[]` if the label isn't active

**Trade-off you don't get to make:** "I'll just document that callers must call it once." Silent double-emission is too costly a footgun for the contract to depend on caller discipline. Add the guard.

---

## 2. Actions (User Interaction Emulation)

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
| **Popup names** | `'promo'`, `'notification'`, `'buyBonus'`, `'auto'`, `'bet'`, `'settings'`, `'history'`, `'info'`, `'bigWin'`, `'totalWin'`, `'devMode'`, `'disconnect'` |

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

### 2.1 `spin()`

Emulates user touching the spin button. **Synchronous — fires the click and returns immediately.** Does not wait for the spin to settle.

| | |
|-|-|
| **Emulates** | Touch `nodeSpinButton` (TOUCH_START → TOUCH_END) |
| **Precondition** | AppState = `idle`, no promo popup showing |
| **Returns** | `void` |

**Code path:**
```
GameplayScene.onSpinClicked()
  → GameplayScene.onSpinButtonReleased()
    → GameController.onSpinClicked()        [validates idle state]
      → GameController.spin()               [checks balance ≥ bet]
        → SlotGameApplication.spin(request) [SignalR 'bet' event]
```

**Why `spin()` does not await IDLE:** a single user click triggers a long state cascade (`BETTING → RESULT → [START_BONUS → IN_BONUS → … → END_BONUS] → IDLE`). Many of the intermediate states are observable game phases that a test may want to inspect, assert on, or capture events from — popups, bonus entry, big-win animations, etc. If `spin()` waited for IDLE internally, every test would race past those states and could never observe them.

Per the **action purity** principle: a bridge action emulates the *exact* user gesture and nothing more. A real user tap doesn't include "wait until the game settles" — that's a separate concern, owned by the wait helpers in §4.

**State flow (normal spin):** `idle` → `betting` → (server response) → `result` → (handler chain: spin animation → win lines → big win → transitions) → `idle`

**State flow (triggers Hold&Spin bonus):** `idle` → `betting` → `result` → `START_BONUS` → `IN_BONUS` → ... re-spins ... → `END_BONUS` → `idle` (can take 30-50s)

**How to compose with waits — pick what your test needs:**
```javascript
// "Spin and wait until the game returns to idle" — most common case.
// waitForSpinComplete() wraps spin() + waitForIdle() in one call.
await bridge.waitForSpinComplete(120_000);

// Spin and wait yourself with a custom timeout policy.
bridge.spin();
await bridge.waitForIdle(120_000);

// Spin and capture the entire event stream — see §5.
bridge.startEventCapture('spin');
bridge.spin();
await bridge.waitForIdle(120_000);
const events = bridge.endEventCapture('spin');
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

### 2.3 `openBetPopup()`

Emulates pressing the +/- bet button on the main screen. **The bet buttons no
longer change the bet inline** — both now open the bet-amount popup
(`DenominationsPopup`) that lists every denomination. This mirrors the
`openAutoPopup()` flow: open the popup here, then drive it with `selectBet()` /
`nextBet()` / `previousBet()`. Close it with `dismissPopups()` (no separate
confirm — each selection applies immediately via `setStake`).

| | |
|-|-|
| **Emulates** | Click `nextBetButton` or `previousBetButton` |
| **Precondition** | AppState = `idle`, no blocking popup, no big/total-win, not in bonus, not promo, a bet button interactable |

**Code path:**
```
GameplayScene.onClickChangeStake()
  → PopupManager.showCustomPopup({ name: PopupName.DENOMINATIONS, data: { denominations, currentStake } })
    → DenominationsPopup.show() → updateUI() → generateButtons()
```

> **Source:** `GameplayScene.ts:1165`, `denominations/DenominationsPopup.ts:45`

---

### 2.3.1 `nextBet()`

Emulates tapping the **'+' button inside the open bet-amount popup** (one step
up). Requires `openBetPopup()` first — throws `betPopup_not_open` otherwise.

| | |
|-|-|
| **Emulates** | Click `+` in `DenominationsPopup` |
| **Precondition** | Bet-amount popup is open |

**Code path:**
```
DenominationsPopup.onClickBtnPlus()
  → handleOnClickedBtnPlus()  [idx = min(idx+1, len-1)]
    → GameController.setStake(denominations[idx]) → GameLogic.onSelectedBet(idx)
```

> **Source:** `denominations/DenominationsPopup.ts:82,155`, `GameController.ts:213`

---

### 2.3.2 `previousBet()`

Emulates tapping the **'−' button inside the open bet-amount popup** (one step
down). Requires `openBetPopup()` first.

| | |
|-|-|
| **Emulates** | Click `−` in `DenominationsPopup` |
| **Precondition** | Bet-amount popup is open |

**Code path:**
```
DenominationsPopup.onClickBtnMinus()
  → handleOnClickedBtnMinus()  [idx = max(idx-1, 0)]
    → GameController.setStake(denominations[idx]) → GameLogic.onSelectedBet(idx)
```

> **Source:** `denominations/DenominationsPopup.ts:78,142`, `GameController.ts:213`

---

### 2.3.3 `selectBet(index: number)`

Emulates tapping a **specific denomination row inside the open bet-amount
popup**. Requires `openBetPopup()` first.

| | |
|-|-|
| **Emulates** | Tap a denomination in `DenominationsPopup` |
| **Precondition** | Bet-amount popup is open; valid index, denomination ≤ balance |

**Code path:**
```
DenominationsPopup.onClickMoney(evt, String(index))
  → GameController.setStake(denominations[index])
    → GameLogic.onSelectedBet(index)  [validates denomination ≤ balance]
```

**Parameter:** `index` — zero-based index into `GameLogic.denominations` array.

> **Source:** `denominations/DenominationsPopup.ts:86`, `GameController.ts:213`

**Typical flow:**
```javascript
bridge.openBetPopup();      // press the +/- bet button → popup opens
bridge.selectBet(3);        // tap the 4th denomination
// or: bridge.nextBet() / bridge.previousBet() to step ±1
bridge.dismissPopups();     // close the popup
```

---

### 2.6 `openAutoPopup()`

Emulates user clicking the auto button to open the auto-spin configuration popup.

| | |
|-|-|
| **Emulates** | Click `autoButton` |
| **Precondition** | `autoButton.interactable = true`, AppState = `idle`, no blocking popups, not in bonus |

**Code path:**
```
GameplayScene.onAutoClicked()
  → PopupManager.showCustomPopup({ name: PopupName.AUTOPLAY, data: { denominations, currentStake, isAuto } })
    → AutoPopup.show()  [popup becomes visible, ready for tap-by-tap interaction]
```

**Important — `AutoPopup` is fetched dynamically, not stored on the scene.** Earlier bridge code assumed `scene.autoPopup` existed as a field; it does not. The popup lives inside `PopupManager` and is retrieved by name (`PopupName.AUTOPLAY`). The bridge's `_getAutoPopup()` helper does the lookup; tests do not need to touch this directly — `openAutoPopup()` → granular methods below all handle it.

> **Source:** `GameplayScene.ts:890-906`, `AutoPopup.ts`

---

### 2.7 Auto-spin flow — overview

Once the popup is open (§2.6), the user has three tap-by-tap interactions before confirming:

1. Pick a package count (`10` / `20` / `30` / `50` / `Infinity`) → §2.7.1
2. Optionally toggle "stop at any win" → §2.7.2
3. Tap Confirm → §2.7.3

Each step is exposed as its own bridge action so a test can assert intermediate popup state between taps. For the common case where you just want to start auto-spin with known values in one line, §2.7.4 `startAutoSpin(count, stopAtWin)` composes the three.

**Auto-spin runtime behaviour — no IDLE between spins:**
```
IDLE → spin() → BETTING → (server) → RESULT → maybeHandleNextSpin()
                                                  ↓
                                        numberAutoSpin > 0?
                                       YES → spin() → BETTING → RESULT → ... (loop)
                                       NO  → transitionTo(IDLE)  ← only once, at the end
```

The game **does not transition to IDLE between auto-spins**. `waitForIdle()` resolves only after the *last* spin completes. Observe individual spin results via event collection — see §5.

---

### 2.7.1 `selectAutoSpinPackage(count?: number)`

Emulates user tapping one of the package-count buttons inside the open popup.

| | |
|-|-|
| **Emulates** | Tap a package button (10 / 20 / 30 / 50 / ∞) |
| **Parameter** | `count` — one of `10`, `20`, `30`, `50`, `Infinity`. **Default `10`** (the smallest package). |
| **Precondition** | Auto popup is open |
| **Returns** | `void` |

**Code path:**
```
AutoPopup.clickNumAuto(event, data)
  → sets _numberAuto = parseInt(data)              [Infinity → encoded as '999999999']
  → runs the check-indicator tween to the chosen button
```

> **Source:** `AutoPopup.ts:103-123`

---

### 2.7.2 `toggleAutoSpinStopAtWin(enabled: boolean)`

Emulates user tapping the "stop at any win" toggle.

| | |
|-|-|
| **Emulates** | Tap `toggleStopAtWin` |
| **Parameter** | `enabled` — desired toggle state |
| **Precondition** | Auto popup is open |
| **Returns** | `void` |

**Code path:**
```
toggleStopAtWin.isChecked = enabled
  → AutoPopup.onToggleStopAtWin(toggleStopAtWin)
    → _isStopAtWin = toggle.isChecked
```

The setter-then-handler sequence mirrors the production dispatch. Setting `isChecked` alone is **not** sufficient — the popup's internal `_isStopAtWin` flag is only written from inside the handler, so the bridge fires both.

> **Source:** `AutoPopup.ts:148-151`

---

### 2.7.3 `confirmAutoSpin()`

Emulates user tapping the Confirm button, locking in whichever package and stop-at-win values are currently selected.

| | |
|-|-|
| **Emulates** | Tap `btnConfirm` |
| **Precondition** | Auto popup is open |
| **Returns** | `void` |

**Code path:**
```
AutoPopup.clickConfrim(event, data)              [production typo preserved]
  → close()                                       [hides popup, fires telemetry]
  → gameController.startAutoplay({ numOfAuto, isStopAtWin, stake })
    → GameLogic.numberAutoSpin = count
    → GameLogic.isStopByWin = stopAtWin
    → if (count > 0 && idle) → spin()             [immediately triggers first spin]
```

After confirm, the popup closes and the first auto-spin fires — there is no IDLE phase between confirm and the first spin.

> **Source:** `AutoPopup.ts:84-101`, `GameController.onConfirmAuto`

---

### 2.7.4 `startAutoSpin(count: number, stopAtWin: boolean, delayMs?: number)`

Convenience composite — select the package, set the stop-at-win toggle, and confirm in one call. **Async** because each inner step is separated by `delayMs` so the package-select tween plays out and the event recorder captures each action as a distinct frame.

```javascript
await bridge.startAutoSpin(20, true);          // 500 ms between each inner step (default)
await bridge.startAutoSpin(20, true, 1000);    // slower pacing — easier replay watching
await bridge.startAutoSpin(20, true, 0);       // fire all three in same tick — fastest, no animation
```

Equivalent to:

```javascript
bridge.selectAutoSpinPackage(count);
await sleep(delayMs);
bridge.toggleAutoSpinStopAtWin(stopAtWin);
await sleep(delayMs);
bridge.confirmAutoSpin();
```

| | |
|-|-|
| **Precondition** | Auto popup is open |
| **Packages** | `10`, `20`, `30`, `50`, `Infinity` |
| **`delayMs`** | Default `500` ms — matches `AutoPopup`'s package-select tween duration. Pass `0` to skip all delays. |
| **Returns** | `Promise<void>` |

Use this when you want auto-spin running with known values **and** want each step to be replay-distinguishable. Use the granular methods (§2.7.1–§2.7.3) when your test needs to assert intermediate popup state or set its own pacing.

**Stop-at-win behaviour:** if enabled AND a spin has payout > 0 → auto-spin stops at the end of that spin.

**Observing individual spin results during auto-spin** (works for either composite or step-by-step):

```javascript
bridge.startEventCapture('auto');
bridge.openAutoPopup();
await bridge.startAutoSpin(10, false);
await bridge.waitForIdle(600_000);
const events = bridge.endEventCapture('auto');

const spinResults = events.filter(e => e.type === 'spin_result');
// spinResults.length === 10 (one per spin)
```

> **Source:** `ZeusTestBridge.startAutoSpin` (composes §2.7.1–§2.7.3 with paced delays)

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

### 2.10 Buy-bonus flow — overview

Once the popup is open (§2.9), the user can:

1. Pick a package toggle (Casual / Bold / Epic — three preset bet denominations) → §2.10.1
2. Optionally fine-tune the bet with the increase/decrease arrows → §2.10.2 / §2.10.3
3. Tap Buy → confirmation sub-popup appears → tap Confirm → purchase fires → §2.10.4

Each step is exposed as its own bridge action so a test can assert intermediate popup state between taps. For the common case where you just want to buy a specific package, §2.10.4 `confirmBuyBonus(packageIndex)` composes the four taps into one call with replay-friendly delays between each step.

---

### 2.10.1 `selectBuyBonusPackage(index?: number)`

Emulates user tapping one of the three package toggles inside the open buy-bonus popup.

| | |
|-|-|
| **Emulates** | Tap `packageCasual` / `packageBold` / `packageEpic` |
| **Parameter** | `index` — `0` = Casual (cheapest), `1` = Bold, `2` = Epic (most expensive). **Default `0`** — the cheapest package. |
| **Precondition** | Buy-bonus popup is open |
| **Returns** | `void` |

**Code path:**
```
BuyBousPopup.chosePackage(event, String(index))
  → handleSelectedBet(index)
    → GameplayScene.onSelectedBet(index)
      → GameLogic.onSelectedBet     [updates bet denomination]
```

The three toggles are pre-mapped to three different denominations from `denominations[]` based on its length — see `DataToggle` in `BuyBousPopup.ts:11-20` for the mapping.

> **Source:** `BuyBousPopup.ts:174-177`

---

### 2.10.2 `increaseBuyBonusBet()`

Emulates user tapping the "next bet" arrow inside the buy-bonus popup.

| | |
|-|-|
| **Emulates** | Tap `nextBetButton` |
| **Precondition** | Buy-bonus popup is open (no-op at max bet — button auto-disabled) |
| **Returns** | `void` |

**Code path:**
```
BuyBousPopup.onNextBetClicked()
  → handleNextBet()
    → GameplayScene.onNextBet → GameController.onNextBet → GameLogic.nextBet
```

> **Source:** `BuyBousPopup.ts:179-182`

---

### 2.10.3 `decreaseBuyBonusBet()`

Emulates user tapping the "previous bet" arrow inside the buy-bonus popup.

| | |
|-|-|
| **Emulates** | Tap `previousBetButton` |
| **Precondition** | Buy-bonus popup is open (no-op at min bet — button auto-disabled) |
| **Returns** | `void` |

**Code path:**
```
BuyBousPopup.onPreviousBetClicked()
  → handlePreviousBet()
    → GameplayScene.onPreviousBet → GameController.onPreviousBet → GameLogic.previousBet
```

> **Source:** `BuyBousPopup.ts:184-187`

---

### 2.10.4 `confirmBuyBonus(packageIndex: number, delayMs?: number)`

Convenience composite — select the package, tap Buy (which surfaces the confirmation sub-popup), then tap Confirm. **Async** because each step is separated by `delayMs` so the event recorder captures each tap distinctly and the confirmation sub-popup is briefly visible.

```javascript
await bridge.confirmBuyBonus(0);             // Casual package, 500ms between steps (default)
await bridge.confirmBuyBonus(2, 1000);       // Epic package, slower pacing
await bridge.confirmBuyBonus(1, 0);          // Bold package, no delays — same-tick fire
```

Equivalent to:

```javascript
bridge.selectBuyBonusPackage(packageIndex);
await sleep(delayMs);
// internal: BuyBousPopup.showPopupConfirm()    [tap "Buy" → sub-popup visible]
await sleep(delayMs);
// internal: BuyBousPopup.onConfirmClicked()    [tap "Confirm"]
```

| | |
|-|-|
| **Precondition** | Buy-bonus popup is open, AppState = `idle`, balance ≥ bet × package price |
| **`packageIndex`** | `0` = Casual, `1` = Bold, `2` = Epic |
| **`delayMs`** | Default `500` ms. Pass `0` to skip all delays. |
| **Returns** | `Promise<void>` — resolves when bonus mode completes and returns to `idle` |

Use this when you want to buy a specific package and care about replay distinguishability. Use the granular methods (§2.10.1–§2.10.3) when your test needs to assert intermediate popup state, or pre-arrange the bet manually before confirming.

**Code path:**
```
BuyBousPopup.chosePackage(event, String(packageIndex))    [select tile]
  → handleSelectedBet(packageIndex)
─── await delayMs ───
BuyBousPopup.showPopupConfirm()                            [tap Buy → sub-popup]
─── await delayMs ───
BuyBousPopup.onConfirmClicked()                            [tap Confirm]
  → close()
  → onConfirmBuyBonus(0)
    → GameplayScene.onConfirmBuyBonus
      → GameController.onConfirmBuyBonus
        → SlotGameApplication.buyBonus(request)  [SignalR 'buyfreespin' event]
        → GameLogic.bonusGameType = BONUS_TOTAL_WIN
```

**State flow:** `idle` → `betting` → `result` → `START_BONUS` → `IN_BONUS` (re-spins) → `END_BONUS` → `idle`

> **Source:** `ZeusTestBridge.confirmBuyBonus` (composes §2.10.1 + Buy + Confirm with paced delays)

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

Injects a 3x3 reel result for the **next** spin via DevModeManager. Can be called multiple times to queue results for consecutive spins (e.g. auto-spin).

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

**Queuing multiple results for auto-spin:**
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
// Remaining spins hit the real server
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

### 2.22 `skipTotalWin()`

Dismisses the bonus total-win popup that appears between `END_BONUS` and `IDLE` after a Hold & Spin or Buy Bonus round. Optional — the popup auto-dismisses on its own; this is a speedup.

| | |
|-|-|
| **Emulates** | Auto-dismiss equivalent — calls `BonusResultPopup.skip()` directly (not the user-tap `clickSkip()` path) |
| **Precondition** | `isShowingTotalWin === true` (reason token `no_totalwin` otherwise) |
| **Returns** | `void` — fire-and-forget; the underlying `skip()` is async but not awaited |

**Code path:**
```
ZeusTestBridge.skipTotalWin()
  → popupTotalWin.skip()
    → _currentState.cancel()      [animation teardown]
    → finish()
      → hide()
      → resolves popup.show() Promise
        → handler chain proceeds
          → transitionToBaseGame()
            → AppState.IDLE
```

**When to use it:**
```javascript
// Fast path — skip the animation tail and proceed.
await bridge.waitForBonusTotalWinPopup();
bridge.skipTotalWin();
await bridge.waitForIdle();

// Assert on the displayed total before dismissing.
await bridge.waitForBonusTotalWinPopup();
expect(bridge.getDisplayWin()).toMatch(/^[0-9.,]+$/);
bridge.skipTotalWin();
await bridge.waitForIdle();

// Or skip entirely — the popup auto-dismisses, just give waitForIdle a long enough timeout.
await bridge.waitForIdle(120_000);
```

**Auto-dismiss vs user-tap:** the popup ships a "press anywhere to continue" overlay (`nodeSkip`) that fires `BonusResultPopup.clickSkip()` → `finish()` directly (no `cancel()`). `skipTotalWin()` currently uses the auto-dismiss path; both reach the same terminal state.

> **Source:** `ZeusTestBridge.ts:683-692`, `BonusResultPopup.ts:122` (`skip`), `BonusResultPopup.ts:129` (`clickSkip`)

---

## 3. Queries (Read-Only State Inspection)

### 3.1 Game State

| Method | Returns | Source |
|--------|---------|--------|
| `getAppState()` | `string` — `'idle'`, `'betting'`, `'result'`, `'error'`, etc. | `ApplicationStateManager.getCurrentState()` |V
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

| Method | Returns | What It Checks |
|--------|---------|----------------|
| `isSpinEnabled()` | `boolean` | `spinButton.interactable` AND AppState is `idle` |
| `isBuyBonusEnabled()` | `boolean` | `buyBonusButton.interactable` (disabled during promo, auto, free spin) |
| `isAutoEnabled()` | `boolean` | `autoButton.interactable` |
| `isNextBetEnabled()` | `boolean` | `nextBetButton.interactable` (disabled at max bet, during promo) |
| `isPreviousBetEnabled()` | `boolean` | `previousBetButton.interactable` (disabled at min bet, during promo) |
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

`getSnapshot()` returns a single object containing every commonly-read field — useful for "give me everything in one call" assertions.

| Field | Type | Meaning |
|---|---|---|
| `appState` | `string` | Current AppState (`'idle'`, `'betting'`, `'result'`, …) |
| `spinState` | `string` | Bonus-flow state (`'NORMAL'`, `'START_BONUS'`, `'IN_BONUS'`, `'END_BONUS'`, `'PROMO'`) |
| `balance` / `betAmount` / `betIndex` | `number` | Money & bet selection |
| `denominations` | `number[]` | Available bet levels |
| `freeSpins` | `number` | Remaining free spins (`> 0` means inside a bonus) |
| `autoSpins` | `number` | Remaining auto-spins (`Infinity` for continuous, `0` when not running) |
| `lastPayout` | `number` | Payout of the most recent spin |
| `lastWinType` | `string` | `'LOSE'` / `'SLIM_WIN'` / `'SMALL_WIN'` / `'MEDIUM_WIN'` / `'BIG_WIN'` / `'MEGA_WIN'` / `'ULTRA_WIN'` |
| `turbo` / `sound` / `music` | `boolean` | Toggle states |
| `promo` | `boolean` | Promotional game active |
| `reelResult` | `number[][]` | 3×3 grid of symbol IDs from the last spin |
| `coinPositions` | `CoinPositions[]` | Coins on the bonus board |
| `isPopupVisible` | `boolean` | Any modal popup is currently blocking the game |
| `visiblePopups` | `string[]` | Names of every popup currently visible — e.g. `['buyBonus']` |
| `isShowingBigWin` / `isShowingTotalWin` | `boolean` | Win-celebration popup is animating |
| `isOnBonusGame` | `boolean` | Hold & Spin bonus mode active |
| `isHoldSpinActive` | `boolean` | User is long-pressing the spin button |
| `isBetPending` | `boolean` | Bet sent, server response not yet received |
| `sceneReady` | `boolean` | Gameplay scene initialisation complete |

The snapshot is computed on every call — it's a fresh read, not a cached value. Cheap to call.

---

### 3.7 On-screen geometry — `getButtonRect()` / `getAllButtonRects()`

For tests that prefer **real coordinate clicks** (`page.mouse.click(x, y)`) over invoking bridge actions, these return an element's on-screen rectangle in **DOM/CSS page pixels** — the coordinate space Playwright's mouse uses.

```ts
getButtonRect(name: string): ButtonRect | null
getAllButtonRects(): Record<string, ButtonRect>

interface ButtonRect {
    x: number; y: number; width: number; height: number;   // page CSS px, top-left origin
    centerX: number; centerY: number;                      // pass these to page.mouse.click
    visible: boolean;       // node renderable AND center lands on-canvas
    interactable: boolean;  // underlying Button/Toggle interactable (best-effort)
}
```

The bridge runs inside the page, so it converts the element's Cocos world-space bounding box all the way to page CSS pixels (`UITransform.getBoundingBoxToWorld()` → `Camera.worldToScreen()` → ÷ `devicePixelRatio` + flip Y → add `#GameCanvas` offset). The camera is resolved from the element's own ancestor `Canvas`, so gameplay elements (rendered by the `game_play` prefab's camera) map correctly.

**Names** — main-screen buttons (always): `spin`, `stop`, `auto`, `buyBonus`, `nextBet`, `previousBet`, `trophy`. Popup elements (only while that popup is open):

| Popup | Names |
|---|---|
| Bet (denominations) | `bet:row:<i>` (per denomination), `bet:plus`, `bet:minus`, `bet:close` |
| Auto-spin | `auto:package:<0..4>` (10/20/30/50/∞), `auto:confirm`, `auto:cancel`, `auto:stop`, `auto:next`, `auto:prev`, `auto:stopAtWin` |
| Buy-bonus | `buyBonus:package:<0..2>`, `buyBonus:buy`, `buyBonus:confirm`, `buyBonus:cancel`, `buyBonus:close`, `buyBonus:next`, `buyBonus:prev` |

`getAllButtonRects()` returns the seven main buttons plus the element rects of whichever popups are currently open.

**Return semantics:** `null` ⇒ unknown name **or** the owning popup is closed (element not present). A returned rect with `visible: false` ⇒ the element exists but isn't currently clickable (inactive, no transform, or off-canvas) — its numeric fields are best-effort; do **not** click it.

```javascript
// Always confirm the path is clear first — a valid rect can still be covered.
const diag = await page.evaluate(() => window.__gameTestBridge.diagnoseHitTest());
expect(diag.buttons.spin.result).toBe('ok');

const rect = await page.evaluate(n => window.__gameTestBridge.getButtonRect(n), 'spin');
expect(rect?.visible).toBe(true);
await page.mouse.click(rect.centerX, rect.centerY);   // real OS-level pointer event
```

`diagnoseHitTest()` answers "is the path clear?" (Cocos-space blocker check); `getButtonRect()` answers "where on the page is it?" (DOM geometry). Use both. Like all scene queries, these throw `GameplayScene is not yet initialised` if called before `startGame()`.

**Waiting for clickable** — `waitForButtonClickable(name, stableMs?, timeoutMs?)` (a wait helper, §4) polls until the element is **fully clickable** — visible, interactable, *and not covered by an overlay* — then **resolves with the rect**. It folds in the hit-test, so you don't need a separate `diagnoseHitTest()` gate. (`isButtonClickable(name)` is the synchronous form.)

```javascript
const rect = await page.evaluate(
  n => window.__gameTestBridge.waitForButtonClickable(n), 'spin');
await page.mouse.click(rect.centerX, rect.centerY);   // guaranteed on target, not an overlay
```

---

## 4. Wait Helpers

The bridge ships a handful of intuitive waits, each named after the player-facing condition the test is waiting on — not the internal state machine.

| Method | Behavior |
|--------|----------|
| `waitForSpinComplete(timeoutMs?: number)` | Fires a spin and waits for the entire animation, payout, transitions, and any triggered bonus to settle. Default 60 s — bump to 120 s+ when the spin may trigger a Hold & Spin bonus. |
| `waitForIdle(timeoutMs?: number)` | Waits until the game is sitting at IDLE — no animation running, ready for the next player action. Default 30 s. Most commonly used after composite actions like `confirmBuyBonus()` or `startAutoSpin()` to wait for the whole round to complete. |
| `waitForUISettle(delayMs?: number)` | Fixed timer (default 1000 ms). Some animations (win counter ticking up, persistence-column effects) keep running for a beat after IDLE. Call this after `waitForIdle()` when you need the screen to visually match the state — e.g. before reading `getDisplayWin()`. |
| `waitForBonusTotalWinPopup(timeoutMs?: number)` | Waits for the bonus total-win popup to appear. Default 60 s. Optional — the popup auto-dismisses. Use it only when you want to assert on the popup-visible state or call `skipTotalWin()` ahead of the auto-dismiss. |
| `waitForButtonClickable(name, stableMs?, timeoutMs?)` | Polls until the element is **fully clickable** — visible, interactable, **and not covered by any overlay** (hit-test lands on it) — continuously for `stableMs` (default 250 ms), then **resolves with that `ButtonRect`** so you can click it immediately. Default timeout 30 s; rejects with the last blocking reason. See §3.7 for valid names. The synchronous form is `isButtonClickable(name)`. |
| `spinUntilBonus(options?)` | Spins repeatedly until a Hold & Spin bonus is triggered, then resolves the **moment** bonus mode is entered (before the bonus plays out). Leaves the game inside the bonus. See §4.1. |

### Important — `waitForIdle()` during auto-spin and Hold & Spin

The game does **not** transition to IDLE between consecutive spins in these modes:
- **Auto-spin:** `BETTING → RESULT → BETTING → RESULT → ...` — IDLE only after the last spin
- **Hold & Spin bonus:** same continuous loop — IDLE only after the bonus ends
- **Free spins (pending):** same continuous loop

`waitForIdle()` will resolve only when the entire sequence completes. To observe individual spins within these modes, use the event capture API in §5.

### 4.1 `spinUntilBonus(options?)`

Spins repeatedly until a Hold & Spin bonus is triggered. **Resolves the moment bonus mode is entered** (before the bonus plays out) so the caller can interact with the bonus — call `skipBonusTransition()`, capture the entry frame, or just `waitForIdle(120_000)` to let it run.

```javascript
const res = await bridge.spinUntilBonus({ maxSpins: 50 });
// res = { spins: 17, matched: true }     ← matched=false if maxSpins ran out
expect(res.matched).toBe(true);
expect(bridge.getSpinState()).toBe('START_BONUS');
```

| Option | Default | Meaning |
|---|---|---|
| `maxSpins` | 500 | Maximum spins to attempt before giving up |
| `timeoutMs` | 300 000 (5 min) | Wall-clock budget for the whole loop |
| `perSpinTimeoutMs` | 120 000 (2 min) | Per-spin completion timeout |

**Limitation:** does not catch **promo-triggered** Hold & Spin (the production code has no PROMO branch in the bonus transition handler, so the bonus flag never flips). For promo-mode testing, check `bridge.isPromoMode()` first.

> **Source:** `ZeusTestBridge.spinUntilBonus`

---

## 5. Event Capture & Analysis

For tests that need to record the game's event stream — to inspect what happened during a spin, or to feed a captured run into the Hold & Spin invariant analysers — the bridge ships a **named capture** API. You name a buffer, start it, drive game actions, stop it, and get back a list of `CollectedEvent`s.

```ts
interface CollectedEvent {
    t: number;                          // seconds since the capture started
    type: string;                       // event category — see below
    data: Record<string, any>;          // payload — shape depends on type
}
```

The two event types you'll typically care about:

| `event.type` | When it fires | Payload |
|---|---|---|
| `spin_result` | Server returned a bet/buy-bonus result | `{ reels, paylines, payout, stake, typeWin, freeSpinInfo, coinPositions, coinCount, spinState }` — the full per-spin outcome. Bonus analysers consume this. |
| `balance_change` | After each spin result | `{ balance, previous }` |

Other categories (`spin_request`, `turbo_change`, `free_spin`) exist but rarely matter for assertion-level testing.

### 5.1 Named captures — `startEventCapture` / `endEventCapture`

The named-capture API is private to your test — independent of any internal collector the game runs for replay/telemetry. Multiple captures can run concurrently under different labels.

| Method | Returns |
|--------|---------|
| `startEventCapture(label: string): void` | Begin a buffer keyed by `label`. Multiple labels can be active concurrently. Calling with an existing label deliberately **resets** that label's buffer. |
| `endEventCapture(label: string): CollectedEvent[]` | Stop the named capture and return everything it collected. Returns `[]` if no capture under that label is active (idempotent). |

```javascript
// Capture every event during one spin.
bridge.startEventCapture('one-spin');
bridge.spin();
await bridge.waitForIdle();
const events = bridge.endEventCapture('one-spin');
// events: spin_request, spin_result, balance_change, … each scoped to this capture
```

### 5.2 Bonus capture & filter

Built on top of named captures, these compose into the bonus-test pipeline. Pure analysers (§5.3) consume the events these produce.

#### `filterBonusEvent(events: CollectedEvent[]): CollectedEvent[]`

Pure function — given a captured event array, returns the contiguous `spin_result` slice from `START_BONUS` through `END_BONUS`, inclusive. Returns `[]` if no `START_BONUS` event is present in the input. This is the **bonus episode** the three `verifyHoldSpin*` analysers expect.

```javascript
bridge.startEventCapture('bonus');
await bridge.spinUntilBonus({ maxSpins: 50 });
await bridge.waitForIdle(120_000);
const raw = bridge.endEventCapture('bonus');
const episode = bridge.filterBonusEvent(raw);
// episode: [START_BONUS, IN_BONUS×N, END_BONUS]
```

#### `captureHoldSpinBonus(options?: SpinUntilOptions): Promise<HoldSpinBonusCapture>`

Convenience wrapper that composes the full bonus-recorder pipeline under the internal label `'captureHoldSpinBonus'`:

1. `startEventCapture('captureHoldSpinBonus')`
2. `await spinUntilBonus(options)` — fire spins until bonus mode entered
3. `await waitForIdle(options.timeoutMs ?? 5*60_000)` — let the bonus play out
4. `endEventCapture('captureHoldSpinBonus')` — collect raw events
5. `filterBonusEvent(raw)` — slice to the episode
6. Return `{ triggered, spinsToTrigger, events, respinCount }`

Returns a `HoldSpinBonusCapture` (see §12). If the spin-loop never triggered a bonus within the budget, returns `{ triggered: false, spinsToTrigger: N, events: [], respinCount: 0 }`. If anything throws inside the pipeline, the named capture is cleaned up before the error propagates — no leaked captures.

```javascript
// Drive a bonus from start to finish, return the episode for analysis.
const capture = await bridge.captureHoldSpinBonus({ maxSpins: 50 });
expect(capture.triggered).toBe(true);
expect(capture.respinCount).toBeGreaterThan(0);
```

Use `captureHoldSpinBonus` for the **wait-for-end** case. Use the `startEventCapture` / `spinUntilBonus` / `endEventCapture` triplet directly when you need to interact with the bonus mid-flight.

> **Source:** `ZeusTestBridge.ts:1605` (filter), `ZeusTestBridge.ts:1616` (capture)

### 5.3 Hold & Spin invariant analysers

Three pure functions that take a bonus episode (the output of `filterBonusEvent` or `captureHoldSpinBonus().events`) and return a structured violation report. All three share `BonusInvariantBase` — `{ valid, bonusFound, respinCount, violations }`. `valid === true` iff `bonusFound && violations.length === 0`. Full type definitions in §12.

| Method | Invariant |
|--------|-----------|
| `verifyHoldSpinCoinsHeld(events)` | Every coin present in event N must still be at the same `(col, row)` in event N+1. New coins may be added. Violation `kind`: `'missing'` (coin disappeared) or `'tier_changed'` (multiplier flipped). |
| `verifyHoldSpinFreespinResetOnNewCoin(events)` | When a re-spin lands new coins (`toCoinCount > fromCoinCount`), `freeSpinInfo.freespins` must reset to **3** in the next event. |
| `verifyHoldSpinFreespinCountDown(events)` | When a re-spin lands **no** new coin (`toCoinCount === fromCoinCount`), `freespins` must decrement by exactly **1**. |

**Exit clause** (all three): pairs where the bonus has ended (`freeSpinInfo === null` on the trailing event) are skipped. The analysers correctly stop checking at `END_BONUS`.

**Mandatory pattern.** A free-spin test that doesn't run **all three** of these isn't actually testing the Hold & Spin mechanic — it's testing whether the bonus ran at all. Run all three or run none.

```javascript
// Recipe A — convenience wrapper (most tests).
const capture = await bridge.captureHoldSpinBonus({ maxSpins: 50 });
expect(capture.triggered).toBe(true);

expect(bridge.verifyHoldSpinCoinsHeld(capture.events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinResetOnNewCoin(capture.events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinCountDown(capture.events).valid).toBe(true);

// Recipe B — interactive (if you need to act during the bonus).
bridge.startEventCapture('bonus');
await bridge.spinUntilBonus({ maxSpins: 50 });
// … interact with the bonus here …
await bridge.waitForIdle(120_000);
const raw = bridge.endEventCapture('bonus');
const events = bridge.filterBonusEvent(raw);

expect(bridge.verifyHoldSpinCoinsHeld(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinResetOnNewCoin(events).valid).toBe(true);
expect(bridge.verifyHoldSpinFreespinCountDown(events).valid).toBe(true);
```

**Reading violations.** When `valid === false`, the `violations` array lists each pair that failed, naming the bonus respin indices (`fromRespinIndex`, `toRespinIndex`), the spin states, and (where applicable) the coin or freespin counts involved. `detail` is a human-readable summary suitable for a failed assertion message.

> **Source:** `ZeusTestBridge.ts:1652` (CoinsHeld), `ZeusTestBridge.ts:1690` (FreespinReset), `ZeusTestBridge.ts:1722` (FreespinCountDown)

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

### Spin-loop options & result (§4.1)

```typescript
interface SpinUntilOptions {
    /** Maximum number of spins to attempt before giving up. Default 500. */
    maxSpins?: number;
    /** Wall-clock budget for the whole loop in ms. Default 300_000 (5 min). */
    timeoutMs?: number;
    /** Per-spin completion timeout in ms. Default 60_000 (spinUntil) / 120_000 (spinUntilBonus). */
    perSpinTimeoutMs?: number;
}

interface SpinUntilResult {
    /** How many spins were performed (including the matching one). */
    spins: number;
    /** True if the condition was met; false if maxSpins or timeoutMs was hit. */
    matched: boolean;
}
```

### Hold & Spin bonus capture (§5.2)

```typescript
interface HoldSpinBonusCapture {
    /** True if a bonus actually triggered within the budget; false if maxSpins/timeoutMs ran out. */
    triggered: boolean;
    /** How many spins were performed before the bonus triggered (0 if already in bonus). */
    spinsToTrigger: number;
    /** Contiguous `spin_result` events from START_BONUS through END_BONUS, inclusive. */
    events: CollectedEvent[];
    /** Count of `spin_result` events in `events`. */
    respinCount: number;
}
```

### Bonus invariant base (§5.3)

Every `verifyHoldSpin*` analyser returns a result extending this base:

```typescript
interface BonusInvariantBase {
    /** True iff `bonusFound` and `violations.length === 0`. */
    valid: boolean;
    /** False when no `START_BONUS` event was found in the input. */
    bonusFound: boolean;
    /** Number of `spin_result` events in the bonus episode. */
    respinCount: number;
}
```

### Coin-hold invariant — `verifyHoldSpinCoinsHeld`

```typescript
interface CoinHoldViolation {
    fromRespinIndex: number;
    toRespinIndex: number;
    fromSpinState: string;
    toSpinState: string;
    kind: 'missing' | 'tier_changed';
    coin: { col: number; row: number; multiplier?: number };
    detail: string;
}

interface CoinHoldCheckResult extends BonusInvariantBase {
    violations: CoinHoldViolation[];
}
```

### Freespin-reset invariant — `verifyHoldSpinFreespinResetOnNewCoin`

```typescript
interface FreespinResetViolation {
    fromRespinIndex: number;
    toRespinIndex: number;
    fromSpinState: string;
    toSpinState: string;
    fromCoinCount: number;
    toCoinCount: number;
    actualFreespins: number;
    expectedFreespins: 3;
    detail: string;
}

interface FreespinResetCheckResult extends BonusInvariantBase {
    violations: FreespinResetViolation[];
}
```

### Freespin-countdown invariant — `verifyHoldSpinFreespinCountDown`

```typescript
interface FreespinCountDownViolation {
    fromRespinIndex: number;
    toRespinIndex: number;
    fromSpinState: string;
    toSpinState: string;
    coinCount: number;
    fromFreespins: number;
    actualFreespins: number;
    expectedFreespins: number;
    detail: string;
}

interface FreespinCountDownCheckResult extends BonusInvariantBase {
    violations: FreespinCountDownViolation[];
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
| `assets/src/test/ZeusTestBridge.ts` | Concrete bridge implementation |
| `assets/src/test/TestBridgeBase.ts` | Abstract bridge interface contract |
| `assets/src/test/EventCollector.ts` | Timestamped event capture for replay viewer |
| `assets/src/views/LoadingScene.ts` | Calls `BridgeActivator.activate()` at `AppState.READY` |
