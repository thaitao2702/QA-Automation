# Chaos Runner — Test Bridge API

In-game E2E test bridge, built to `bridge-guide-v2.md`.

Open the game with **`?bridge=1`** (query string or hash-routed, e.g.
`#/play?bridge=1`). The bridge then lives on `window.__gameTestBridge`.
Without the flag nothing is constructed and no production method is patched.

```
assets/game/scripts/testBridge/
  BridgeActivator.ts        ?bridge=1 gate -> window.__gameTestBridge
  TestBridgeBase.ts         abstract contract + shared types
  EventCollector.ts         timestamped UI-event buffer + pure FILTER/ASSERT
  ChaosRunnerTestBridge.ts  the implementation
```

Wiring: one line in [LoadingView.ts](../assets/game/scripts/views/loading/LoadingView.ts) —
`BridgeActivator.activate(() => this.onClickStart(null))`, right after
`GlobalContext.boot()` resolves (the bridge's access point is `GlobalContext`,
which is only populated then).

---

## Division of labour

The bridge exposes the **surface** (control rects, popup visibility, a thin
snapshot) plus the **raw server responses**. It never computes what a round did —
payouts, win tiers, free-spin math and every rule assertion are the QA
framework's job, derived from `awaitBetResponse()`'s raw `BetResponse`.

`awaitBetResponse()` returns the response envelope **verbatim**:
`{ status, code, msg, timestamp, result: SpinResult }` — see
[ResponseModels.ts](../assets/game/scripts/models/ResponseModels.ts).

---

## REQUIRED

| Method | Notes |
|---|---|
| `startGame(): Promise<void>` | waits for `AppState.READY`, clicks Start, waits for `PLAYING` + scene, patches busy hooks, then drives to clean idle |
| `getSnapshot(): GameSnapshot` | thin — surface + applicability inputs only |
| `getButtonRect(name): ButtonRect \| null` | `null` (never throws) for an absent control or a closed popup |
| `getAllButtonRects(): Record<string, ButtonRect>` | main controls + whatever the open popups expose |
| `waitForButtonClickable(name, stableMs?, timeoutMs?)` | resolves with a **freshly read** rect |
| `waitForSpinClickable(stableMs?, timeoutMs?)` | also the **settle signal** — see below |
| `awaitBetResponse(timeoutMs = 15000)` | next raw response past the shared cursor; does not click |
| `awaitFreespinRound(timeoutMs = 60000)` | same mechanism, passive — the client fires each free spin itself |

### The settle contract

`waitForSpinClickable()` resolves only when all of these hold continuously for
`stableMs` (default 250 ms):

`sceneReady` · `roundState === READY` · `mode === NORMAL` · `!isAutoPlay` ·
`isReadyToSpin` · `!isLockKeyDown` · `busyOps` empty · no visible popup ·
spin button interactable **and not covered** (hit-test).

`busyOps` is the part that makes it a settle signal rather than a readiness
probe: `GameController.processSpinResult` is wrapped, so `round` stays in
`busyOps` for the whole 19-phase pipeline. That matters because the free-spin
outro flips `gameMode` back to `NORMAL` and `roundState` to `READY` *while the
transition animation is still running* — the mode check alone would settle early.

It never resolves during autoplay or free spins.

---

## OPTIONAL

`dismissPopups()` · `isPopupVisible(label)` · `canDo(action)` ·
`waitUntilApplicable(action)` · `waitForStableGameState()` ·
`waitForDeterministicStart()` · `isButtonClickable(name)` · `getSpeedMode()` ·
`getHistoryRows()` · `getHistoryDetailState()` · `getTutorialRect()` ·
`scrollBetRowIntoView(index)` · `holdSpinStart()` / `holdSpinEnd()` ·
`skipBigWin()` · `setForcedResult(result | null)` ·
`startEventCapture(label)` / `endEventCapture(label)` / `stopEventCollection()`

### `getTutorialRect()`

The tutorial is a **cross-origin** `WebView` served from the static host, so
injected JS cannot reach inside it. The bridge reports its page rect only — the
framework does the wheeling.

### `setForcedResult(result)`

Pass the **inner** `SpinResult`, not the socket envelope. Implemented by wrapping
whatever `IGameAdapter` is installed and overriding `consumeNextResult()`, so the
injected round travels the game's existing forced-result path
(`SlotGameApplication.requestBet` → `onBetSuccess` → the shared bet fan-out) and
is captured by the tap like any other response. One-shot.

---

## CAPABILITIES

`getCapabilities()` returns:

| Flag | Value here | Why |
|---|---|---|
| `HAS_TURBO` | true | `btn_turbo` |
| `HAS_ANTE_BET` | true | `btn_anteBet` |
| `HAS_BUY_BONUS` | **false** | no buy-bonus control exists — `buyBonusNode` is unwired and `ControlView.handleClickBuyBonus()` is empty |
| `HAS_FREE_SPINS` | true | |
| `HAS_AUTOPLAY` | true | `btn_autoplay` |
| `HAS_PROMO` | runtime | true only when the server sent `gameInfo.promo` |
| `HAS_HISTORY` | true | |
| `HAS_TUTORIAL_WEBVIEW` | true | |
| `HAS_FORCED_RESULT` | true | |
| `HAS_MULTIPLIER_LADDER` | true | |
| `IS_REPLAY_MODE` | runtime | `?mode=replay` |

---

## Control names

**Main screen** — `spin`, `turbo`, `auto`, `betMinus`, `betPlus`, `anteBet`,
`anteBetInfoClose`, `history`, `info`, `settings`, `soundOn`, `soundOff`,
`monumentalWin`, `skip:freespinHit`, `skip:freespinResult`.
(`buyBonus` always resolves to `null`.)

**Popup-namespaced** — resolve **only while that popup is open**:

- `autoplay:` `confirm` `stop` `cancel` `next` `back` `stopAtWin` `close` `count:<i>`
- `denomination:` `close` `minus` `plus` `row:<i>` — rows are matched **by name**, not child order (the popup generates them in reverse)
- `setting:` `turbo` `music` `sound` `turboTab` `history` `info` `gameLimit` `close`
- `message:` `ok` `cancel` `close`
- `gameLimit:close`, `information:` `close` `webview`
- `history:` `close` `timeRange` `row:<i>` (virtual list — only rendered rows exist)
- `historyDetail:` `prev` `next` `close`
- `monumentalWins:close`
- `promoReceive:` `confirm` `cancel` `other`, `promoMessage:ok`, `promoList:close`

Three return meanings are kept distinct:
`null` = bad name / popup closed · `visible:false` = present but inactive or
off-canvas · `visible:true` = on screen (may still be covered — check
clickability).

Big-win skip is a **tap-anywhere Canvas listener**, not a button, so it has no
rect: use `skipBigWin()`.

---

## Popup labels

`message` `disconnect` `information` `gameLimit` `setting` `autoplay`
`denomination` `historyDetail` `history` `monumentalWins` `promoMessage`
`promoReceive` `promoList` `replay` `replayLanguage`
— listed front-most modal first, which is also `dismissPopups()`'s order.

`isPopupVisible()` treats a popup whose `hide()` has been called as **not
visible**: `BasePopup.hide()` keeps the node active for a 0.2 s tween, so the
`_isHide` flag is what says "already committed to closing". Input is still
blocked during that fade, but the spin gate catches that through the hit-test
(`blocked_by:<node>`), not through popup visibility.

The promo popups (`promoList` / `promoReceive` / `promoMessage`) are
**Promise-driven**: their awaiting flow only resolves when a specific button
fires. Their dismissers emit that button's `CLICK` before hiding — hiding alone
would strand the flow and hang the game. They close on a tween, so a single
`dismissPopups()` pass can legitimately miss them; `waitForDeterministicStart()`
loops with a 300 ms gap, and the "could not close" warning only fires from the
second consecutive failed attempt.

---

## Reason vocabulary

Every wait rejects with the last blocking reason, as `prefix:value`:

`scene_not_ready` · `app_state:<x>` · `state:<roundState>` · `mode:<gameMode>` ·
`mode:AUTOPLAY` · `not_ready_to_spin` · `key_locked` · `busy:<ops>` ·
`popup:<label>` · `unresolved` · `not_visible` · `button_disabled` ·
`blocked_by:<node>` · `clipped_by_mask` · `no_consumer_at_point` ·
`inactive_hierarchy` · `self_advancing:<mode>`

Skip-style actions (`skipBigWin`, `skip:freespinHit`, `skip:freespinResult`,
`stopSpin`) are exempt from the busy gate — interrupting the animation
`busyOps` holds is exactly their purpose.

---

## Event capture (UI / timing only)

`startEventCapture(label)` … `endEventCapture(label)` returns `UIEvent[]`.
Emitted types: `popup_shown`, `popup_hidden`, `popup_dismissed`, `round_start`,
`round_end`, `bigwin_start`, `bigwin_end`, `bet_response`, `hold_spin_start`,
`hold_spin_end`, `start_clicked`, `forced_result_consumed`, `capture_start`,
`capture_end`.

Hooks, never polls: `BasePopup.prototype.show/hide` and the wrapped async
methods. Every original is stored and restored by `stopEventCollection()`.
`EventCollector.filterByType/filterByLabel/windowMs/hasOrder` are pure functions
over the array.

**Game-rule verification is not here** — the framework asserts payout /
free-spin / balance rules over the raw responses.

---

## Game-specific extensions (NOT core bridge API)

- `getMultiplierLevels(): { values: number[] }` — the configured lucky-multiplier
  ladder (`LUCKY_MULTIPLIER_VALUES`) shown by `MulitplierBarView`. A line game
  without a ladder omits this and the dependent case goes inconclusive.
- `getPaylineCount(): number | null` — `gameInfo.betlines` for this session.

Both expose a **configured fact**, never a per-spin interpretation.

---

## Verify in a real BUILD (not editor preview)

Static types prove structure; only a running, **minified** build proves the
structure matches reality.

1. `getAllButtonRects()` — every visible button's centre inside the canvas.
2. Real spin click → `awaitBetResponse()` resolves with the raw response; one per
   spin, in arrival order; forced results captured too.
3. `waitForSpinClickable()` rides out a big-win celebration and a free-spin
   outro, and does not resolve during autoplay / free spins.
4. Open each popup → it appears in `visiblePopups`; `dismissPopups()` returns its
   label **and it is gone**.
5. `getHistoryRows()` returns rows **in a build** (it uses the imported
   `ItemHistoryComponent` type; a string `getComponent` would return `[]`).
6. Coordinate sanity: multi-camera resolution and the Y flip.
