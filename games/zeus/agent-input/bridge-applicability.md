# Zeus Slot Game — Test Bridge Applicability Mechanism

> **Companion to** [`bridge-api.md`](./bridge-api.md). That doc lists what the
> bridge can do; this doc explains how the bridge knows **when** an action is
> safe to perform — and how tests use that knowledge to avoid false-assumption
> failures.

---

## 1. The Problem

Tests drive the game through `ZeusTestBridge` methods like `spin()`,
`confirmBuyBonus()`, and `nextBet()`. Each method emulates a real user click
through the production code path.

Without applicability checks, a test that calls `bridge.spin()` while:

- a popup covers the spin button
- the BigWin celebration is mid-animation
- the reels are still spinning from the previous bet
- the network is still waiting for a bet response
- the persistence column is mid-transition into bonus mode

…will silently no-op. The button's click handler ignores the call, the test
sees no state change, and the next assertion fails with a confusing error
message far from the actual cause.

**`button.interactable === true` is necessary but not sufficient.** The
button may be interactable but covered, mid-animation, or inside a state
where the game refuses to act on the click.

The bridge fixes this with a three-layer applicability mechanism plus a
quiescence wait helper.

---

## 2. Architecture

```
                                 canDo(action)
                                 ┌────────────────────────────────────┐
                                 │                                    │
  Layer A: Flag aggregation  ◄───┤  appState, popups, _isShowingBigWin,
  (cheap, always runs)           │  _isOnBonusGame, _isHoldSpinButton,
                                 │  _isReceiveBetResponse, etc.
                                 │                                    │
  Layer B: Animation tracker ◄───┤  _busyOps — populated by wrappers
  (cheap, always runs)           │  around transitionToHoldSpin,
                                 │  triggerBonus, BigWinPopup.show, …
                                 │                                    │
  Layer C: Cocos hit-test    ◄───┤  Walks scene graph from the topmost
  (click actions only)           │  input-consumer at button center;
                                 │  reports node name of blocker.
                                 └────────────────────────────────────┘
                                                  │
                                                  ▼
                                           Applicability
                                  { ok: boolean,
                                    reason?: string,
                                    hitTested?: boolean }
```

Each layer is independent and additive. Layer A catches the cases the game
already exposes as state (~80% of blockers). Layer B catches transitions
that have no flag. Layer C catches unknowns — including future overlays the
team hasn't added to the flag list yet.

---

## 3. The Contract

### 3.1 `Applicability`

```ts
interface Applicability {
    ok: boolean;
    reason?: string;        // short token, e.g. "popup:buyBonus"
    hitTested?: boolean;    // true if Layer C ran
}
```

`reason` is a **stable token** suitable for assertions and log greps. The
token vocabulary:

| Token | Meaning | Layer |
|-------|---------|-------|
| `scene_not_ready` | `GameplayScene.start()` not yet resolved | A |
| `state:<name>` | AppState is not the required state | A |
| `popup:<name1>,<name2>` | One or more blocking popups visible | A |
| `bigwin_animating` | BigWin celebration in progress | A |
| `totalwin_animating` | BonusResultPopup in progress | A |
| `bonus_in_progress` | Hold & Spin bonus active | A |
| `bet_pending` | Bet sent, response not yet received | A |
| `busy:<labels>` | One or more wrapped animations in flight | B |
| `button_disabled` | The button is not interactable | A |
| `insufficient_balance` | Balance < required cost | A |
| `promo_locks_bet` / `promo_locks_turbo` | Promo mode restriction | A |
| `autoPopup_not_open` / `buyBonusPopup_not_open` | Required popup not visible | A |
| `auto_not_running` / `hold_not_active` / `not_in_bonus` / `no_bigwin` / `not_spinning` | Action requires a state the game isn't in | A |
| `no_packages` / `devmode_unavailable` / `*_not_loaded` | Required resource not initialised | A |
| `blocked_by:<nodeName>` | Hit-test found a different input-consumer on top | C |
| `inactive_hierarchy` | Target node's parent chain is inactive | C |
| `no_consumer_at_point` | Nothing input-consuming at the button center | C |

### 3.2 `ApplicableAction`

All 21 bridge actions have an applicability rule. Action names match the
bridge method names one-to-one:

```ts
type ApplicableAction =
    | 'spin' | 'stopSpin'
    | 'nextBet' | 'previousBet' | 'selectBet'
    | 'openAutoPopup' | 'startAutoSpin' | 'stopAutoSpin'
    | 'openBuyBonusPopup' | 'confirmBuyBonus'
    | 'setTurbo' | 'setSound'
    | 'openSettings' | 'openInfo' | 'openHistory'
    | 'holdSpinStart' | 'holdSpinRelease'
    | 'skipBonusTransition' | 'skipBigWin'
    | 'openDevMode' | 'openTrophy';
```

### 3.3 Universal preconditions

Two checks apply to **every** action, evaluated before the per-action gate:

1. `sceneReady` must be true (`GameplayScene.start()` resolved). Reason
   token: `scene_not_ready`.
2. `busyOps` must be empty (no bridge-tracked animation in flight). Reason
   token: `busy:<labels>`.

---

## 4. API Surface

### 4.1 `canDo(action)` — synchronous query

```ts
bridge.canDo('spin');
// → { ok: false, reason: 'popup:buyBonus' }
```

Returns the applicability **right now**, without waiting. Tests can use
this for assertions, gating, or diagnostics.

### 4.2 `waitUntilApplicable(action, stableMs?, timeoutMs?)` — quiescence wait

```ts
await bridge.waitUntilApplicable('spin');           // defaults: 250 ms stable, 30 s timeout
await bridge.waitUntilApplicable('spin', 500);      // require 500 ms continuous ok
```

Polls `canDo(action)` every 50 ms; resolves only when `ok` has stayed `true`
continuously for `stableMs`. Filters flicker during transition tails (e.g.
AppState flips to `idle` while `popupBigWin.show()` is still resolving for
~100 ms).

Timeout error message includes the last blocking reason — invaluable for
diagnosing why a wait stalled.

```
Error: [ZeusTestBridge] waitUntilApplicable('spin') timed out after 30000ms.
       Last blocking reason: 'busy:transitionToHoldSpin'
```

### 4.3 Action methods self-assert

All 21 action methods call `_assertApplicable(action)` first. Calling an
action when not applicable throws synchronously with the blocking reason:

```ts
bridge.spin();
// throws: [ZeusTestBridge] spin() not applicable: popup:buyBonus

bridge.confirmBuyBonus(0);
// throws: [ZeusTestBridge] confirmBuyBonus() not applicable: insufficient_balance
```

This converts a Playwright-side "click fired but nothing happened" into a
fast, named failure with a stack trace pointing at the offending test line.

### 4.4 `is*Enabled()` queries

The legacy boolean queries (`isSpinEnabled`, `isBuyBonusEnabled`,
`isAutoEnabled`, `isNextBetEnabled`, `isPreviousBetEnabled`) now delegate
to `canDo(action).ok`. They reflect the **full** applicability picture, not
just the button's `interactable` flag.

### 4.5 `getSnapshot()` — applicability fields

`GameSnapshot` carries 10 applicability-input fields alongside the
gameplay state:

```ts
{
    // existing gameplay fields…
    isPopupVisible: boolean;
    visiblePopups: string[];          // ['buyBonus', 'bigWin']
    isShowingBigWin: boolean;
    isShowingTotalWin: boolean;
    isOnBonusGame: boolean;
    isHoldSpinActive: boolean;
    isLockSpinButton: boolean;
    isBetPending: boolean;
    sceneReady: boolean;
    busyOps: string[];                // ['transitionToHoldSpin']
}
```

These are the raw inputs `canDo` derives from — surfaced so tests can also
make their own custom predicates.

---

## 5. Layer B: Animation Tracker

Some long-running animations have no flag — they are inline
`await Utils.runAnimAsync(...)` chains. The bridge wraps them at runtime,
without modifying production code, via `_patchBusyHooks()` called from
`startGame()` once after `sceneReady`.

Currently wrapped:

| Production method | `busyOps` label | What it covers |
|---|---|---|
| `GameplayScene.transitionToHoldSpin` | `transitionToHoldSpin` | Bonus enter transition (~1.5 s) |
| `GameplayScene.transitionToBaseGame` | `transitionToBaseGame` | Bonus exit transition |
| `GameplayScene.triggerBonus` | `triggerBonus` | Persistence column bonus-trigger anim |
| `GameplayScene.HighlightCoinSymbols` | `HighlightCoinSymbols` | Coin highlight before bonus |
| `BigWinPopup.show` | `bigWin.show` | Big/Mega/Ultra celebration |
| `BonusResultPopup.show` | `totalWin.show` | Hold & Spin final total popup |

**Mechanism.** Each wrap rebinds the host method to a closure that calls
`_trackBusy(label, originalBound(...args))`. `_trackBusy` adds the label to
`_busyOps` on entry, removes it on resolve/reject. Production code is
untouched.

**Idempotency.** Guarded by `_busyHooksPatched`. Safe across multiple
`startGame()` calls.

**Adding a new wrap.** When a flake points at a long-running animation
that has no flag, append one line to `_patchBusyHooks()`:

```ts
wrap(scene, 'someAsyncMethod', 'someAsyncMethod');
```

`canDo()` will then return `{ ok: false, reason: 'busy:someAsyncMethod' }`
while it runs.

---

## 6. Layer C: Cocos Hit-Test

For click-style actions (`spin`, `holdSpinStart`), the bridge verifies a
synthetic touch at the button's screen-space center would actually land on
that button.

### 6.1 Walk semantics

Cocos UI render order:

- Children render on top of parents.
- Within the same parent, later siblings render on top of earlier siblings.

The walk is a DFS from `director.getScene()` in **reverse-sibling order**.
The deepest matching node wins.

### 6.2 Input-consumer filter

A naïve walk would report any UITransform-overlapping node as a blocker —
including decorative Sprites and Labels. Cocos actually passes touches
through nodes that don't consume input. The walk filters to nodes carrying:

- `Button`
- `Toggle`
- `EditBox`
- `BlockInputEvents`
- `ScrollView`

…and skips everything else.

### 6.3 Asymmetric target handling

This game wires the spin button with raw node listeners
(`nodeSpinButton.on(Node.EventType.TOUCH_START, ...)`) rather than a
`Button` component. There is no public Cocos API to enumerate listeners,
so `_hitTestNode` handles the target asymmetrically:

| Case | Verdict |
|---|---|
| Topmost consumer is the target or its ancestor/descendant | `ok` |
| No consumer at the point, and target is itself a consumer | `no_consumer_at_point` |
| No consumer at the point, and target is **not** a consumer (raw listener) | `ok` (we can't prove failure) |
| Topmost consumer is a different node | `blocked_by:<nodeName>` |

The last case is the meaningful one for tests — `nodeName` is the
production node name and pinpoints exactly which overlay is blocking.

### 6.4 Live debugging

```ts
bridge.hitTestDebug = true;
```

Enables a `console.warn` on every blocked hit-test:

```
[ZeusTestBridge] hit-test blocked
  target:  'nodeSpinButton'
  blocker: 'BuyBonusPopup'
  center:  { x: 540, y: -480 }
```

Off by default so tests that intentionally check "can't click" scenarios
don't generate noise.

---

## 7. `diagnoseHitTest()` — Runtime Verification

The hit-test rests on assumptions about Cocos render order, the component
list considered input-consuming, and the structure of the live scene.
`diagnoseHitTest()` is the verification tool:

```ts
const report = bridge.diagnoseHitTest();
```

Returns:

```ts
{
    timestamp: 1731234567890,
    sceneReady: true,
    appState: 'idle',
    visiblePopups: ['buyBonus'],
    buttons: {
        spin:        { result: 'blocked', target: 'nodeSpinButton',  interactable: true,  blocker: 'BuyBonusPopup',     center: { x: 540, y: -480 } },
        stop:        { result: 'inactive', target: 'stopButton',     interactable: false                                                            },
        auto:        { result: 'ok',      target: 'autoButton',     interactable: true,                                center: { x: 320, y: -500 } },
        buyBonus:    { result: 'blocked', target: 'buyBonusButton', interactable: false, blocker: 'BuyBonusPopup',     center: { x: 760, y: -500 } },
        nextBet:     { result: 'ok',      target: 'nextBetButton',  interactable: true,                                center: { x: 220, y: -700 } },
        previousBet: { result: 'ok',      ...                                                                                                       },
        trophy:      { result: 'ok',      ...                                                                                                       }
    }
}
```

### 7.1 Result tokens

| Token | Meaning |
|---|---|
| `ok` | Click would land on target |
| `blocked` | Another input-consumer is on top (`blocker` names it) |
| `inactive` | `target.node.activeInHierarchy === false` |
| `no_uitransform` | Target has no UITransform — can't hit-test |
| `no_target` | Bridge reference is `null` (popup not yet loaded, etc.) |
| `target_null` / `inactive_hierarchy` / `no_consumer_at_point` | Passthroughs from internal hit-test |

### 7.2 Verification recipes

Run these from a Playwright test to verify the mechanism matches reality:

```ts
// 1. Baseline — idle scene, no popup. Every clickable button is 'ok'.
await bridge.startGame();
const r1 = bridge.diagnoseHitTest();
expect(r1.buttons.spin.result).toBe('ok');
expect(r1.buttons.buyBonus.result).toBe('ok');

// 2. Popup blocking — open buy bonus, spin must be 'blocked' by it.
bridge.openBuyBonusPopup();
const r2 = bridge.diagnoseHitTest();
expect(r2.buttons.spin.result).toBe('blocked');
expect(r2.buttons.spin.blocker).toMatch(/BuyBonus/i);   // tune to actual node name

// 3. Animation in flight — busyOps reports it.
//    Trigger a natural spin that lands a bonus; during transition:
expect(bridge.getSnapshot().busyOps).toContain('transitionToHoldSpin');

// 4. Bonus mode — buy bonus button is 'inactive' during Hold & Spin.
await bridge.confirmBuyBonus(0);
// while still inside the bonus:
expect(bridge.diagnoseHitTest().buttons.buyBonus.result).toBe('inactive');

// 5. Live debug — see hit-test misses in the test log.
bridge.hitTestDebug = true;
```

### 7.3 What to look for

| Observation | Diagnosis | Fix |
|---|---|---|
| `result: 'blocked'`, `blocker` is a decorative sprite name | Layer C false positive — Sprite incorrectly treated as input-consumer | Inspect that node; if it does carry one of the filtered components, that's why. Otherwise file a bug. |
| `bridge.spin()` fails silently but `result: 'ok'` | Layer C false negative — raw-listener button blocked by something undetectable | Widen `canDo()`'s Layer A flag checks; don't lean on hit-test for that path. |
| `result: 'blocked'`, `blocker: 'Canvas'` or scene root | Walk is finding a parent because of an unexpected component | Check the node — possibly an old `BlockInputEvents` left active. |
| Test breaks after engine upgrade | Cocos render order / component class changed | Re-verify Layer C; the walk lives entirely in `_findTopmostConsumerAt`. |

---

## 8. Test Patterns

### 8.1 Recommended call sequence

```ts
await bridge.waitUntilApplicable('spin');   // quiescence
await bridge.spin();                         // self-asserts
```

The wait absorbs transition tails; the self-assert throws a named error if
state changed between the wait and the call.

### 8.2 Negative assertions

```ts
expect(bridge.canDo('spin')).toEqual({ ok: false, reason: 'popup:buyBonus' });
```

`reason` is a stable string token, safe to grep and assert on.

### 8.3 Custom predicates

Build one-off predicates from snapshot fields without extending `canDo`:

```ts
await bridge.waitUntilApplicable('spin');
const s = bridge.getSnapshot();
if (s.lastWinType !== 'LOSE') {
    /* … */
}
```

For wait-on-anything semantics, `waitUntilApplicable` only knows the 21
actions; for arbitrary predicates, hand-roll a poll loop on `getSnapshot()`.

### 8.4 Debugging a hung test

When `waitUntilApplicable` times out, the error message includes the last
blocking reason. Common diagnostic flow:

1. Read the reason token: `busy:bigWin.show` → an animation is in flight.
2. Did the celebration complete? Check `popupBigWin.node.active` in the
   console; if `false`, the wrap leaked. Check `bridge.getSnapshot().busyOps`.
3. Reason `state:result`? The handler chain stalled before reaching IDLE.
   Inspect `gameController.handlers` to see which handler is still running.
4. Reason `bet_pending`? Network never delivered the bet response. Check
   `connectionManager` state and the SignalR transport.

---

## 9. Known Limitations

1. **Raw `node.on(TOUCH_START)` listeners.** Layer C cannot detect these.
   The asymmetric target rule (§6.3) papers over the common case but won't
   catch a custom overlay that uses raw listeners. If such an overlay ever
   gets added, add a `BlockInputEvents` component to it OR widen
   `_isInputConsumer`.

2. **No automatic flag discovery.** When a new gameplay flag is added to
   `GameplayScene` (e.g. some future `_isShowingTutorial`), the bridge
   won't see it until someone adds it to the snapshot + `canDo` switch.
   This is documented drift — Layer C is the partial safety net.

3. **No event-driven re-eval.** `waitUntilApplicable` polls every 50 ms.
   For most transitions this is imperceptible, but tests that target
   millisecond-level windows of applicability may need a tighter loop or
   an event hook.

4. **Z-order via render priority.** The walk assumes Cocos default sibling
   ordering. Nodes that override `priority` or `Layers` may render in an
   order the walk doesn't reflect. Falsifiable via `diagnoseHitTest`.

5. **Multi-camera scenes.** The walk uses world-space coordinates from
   `UITransform.getBoundingBoxToWorld()` which is correct for single-canvas
   UI but does not project through multiple cameras. Not currently an
   issue — this game uses a single UI canvas.

---

## 10. Extending the Mechanism

### Add a new action gate

1. Add the action name to `ApplicableAction` in `TestBridgeBase.ts`.
2. Add a `case '<name>': { … }` branch in `canDo()` in `ZeusTestBridge.ts`.
3. Call `this._assertApplicable('<name>')` at the top of the action method.

### Add a new busy-op wrap

1. Append a `wrap(host, methodName, label)` line in `_patchBusyHooks()`.
2. The label appears in `getSnapshot().busyOps` and the `busy:<label>`
   reason token automatically.

### Add a new applicability input

1. Add the field to `GameSnapshot` in `TestBridgeBase.ts`.
2. Populate it in `ZeusTestBridge.getSnapshot()`.
3. Consume it from the relevant `canDo` branches with a new reason token.
4. Document the token in §3.1.

---

## Cross-References

- [`bridge-api.md`](./bridge-api.md) — full action / query reference
- [`assets/src/test/TestBridgeBase.ts`](assets/src/test/TestBridgeBase.ts) —
  abstract bridge contract
- [`assets/src/test/ZeusTestBridge.ts`](assets/src/test/ZeusTestBridge.ts) —
  concrete implementation
