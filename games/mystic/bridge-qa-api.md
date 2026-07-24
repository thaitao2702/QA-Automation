# Test Bridge — QA Reference

Function signatures and when to use them. For *why* something behaves the way it
does, see [bridge-api.md](./bridge-api.md).

**Enable:** append `&bridge=1` to the game URL. Without it the bridge does not exist.

```js
await page.goto(`${GAME_URL}&bridge=1`);
await page.waitForFunction(() => window.__gameTestBridge);
const bridge = () => window.__gameTestBridge;   // always call inside page.evaluate
```

---

## Bootstrap

| Function | Returns | Use case |
|---|---|---|
| `waitForGameReady(stableMs?, timeoutMs?)` | `Promise<void>` | Wait until loading finishes and Start is **clickable**. Defaults `250`, `60000`. |
| `startGame()` | `Promise<void>` | Convenience: does the whole sequence below in one call. |

**The start sequence.** Wait for the ready state, read the button rect, click it:

```js
await page.evaluate(() => window.__gameTestBridge.waitForGameReady());

const start = await page.evaluate(() => window.__gameTestBridge.getButtonRect('startButton'));
await page.mouse.click(start.centerX, start.centerY);

await page.evaluate(() => window.__gameTestBridge.waitForDeterministicStart());
```

`startGame()` collapses that into one call, but it invokes the Start handler
**directly** instead of clicking — so it skips the real input pipeline. Use the
three-step form when you care that Start is genuinely clickable; use
`startGame()` when you just need a game to test something else in.

> **What `waitForGameReady()` waits for:** app state `ready` (loading + preload
> complete) **and** the Start button visible, interactable and not covered — the
> same check `isButtonClickable('startButton')` does, so the two can't disagree.
>
> ⚠ The button **pulses forever** (a 1.0↔1.1 scale tween). Its `width`/`height`
> oscillate, so don't assert on them — but the anchor is centred, so
> `centerX`/`centerY` never move and clicking is stable.
>
> ⚠ **Replay mode auto-starts** and never shows the button. This rejects
> straight away with `replay_mode_autostarts` instead of burning the timeout;
> `startGame()` skips both the wait and the click there.
>
> ⚠ The button only exists **before** the game starts — the loading screen is
> destroyed on click. Afterwards `getButtonRect('startButton')` returns `null`.

---

## Buttons

The bridge gives you **coordinates**, not click methods — you issue a real
`page.mouse.click()` so the test exercises the true input pipeline.

| Function | Returns | Use case |
|---|---|---|
| `waitForButtonClickable(name, stableMs?, timeoutMs?)` | `Promise<ButtonRect>` | **The one you want.** Waits until the control is clickable and returns its rect. Defaults: `250`, `30000`. |
| `getButtonRect(name)` | `ButtonRect \| null` | Read a control's position right now, without waiting. |
| `getAllButtonRects()` | `Record<string, ButtonRect>` | Debugging / snapshot of everything on screen. |
| `isButtonClickable(name)` | `boolean` | Assert a control is or isn't clickable. |

```js
const r = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('spin'));
await page.mouse.click(r.centerX, r.centerY);
```

**`ButtonRect`** → `{ x, y, width, height, centerX, centerY, visible, interactable }`

**Return meanings** — don't collapse them:
- `null` → bad name, or the owning popup is closed → your test is wrong
- `visible: false` → exists but off-screen/inactive → wait
- `visible: true` → on screen, but **may be covered** → use `isButtonClickable()`

### Button names

**Loading screen:** `startButton` — only before the game starts; `null` after.

**Main screen:** `spin` · `turbo` · `auto` · `anteBet` · `buyBonus` ·
`stakeDecrease` · `stakeIncrease` · `history` · `info` · `settings` ·
`monumentalWin` · `sound`

**Autoplay popup:** `auto:confirm` · `auto:stop` · `auto:next` · `auto:back` ·
`auto:stopAtWin` · `auto:close` · `auto:count:0..4`

**Bet popup:** `bet:row:<i>` · `bet:close`

**Settings popup:** `setting:turbo` · `setting:music` · `setting:sound` ·
`setting:turboTab` · `setting:history` · `setting:info` · `setting:gameLimit` ·
`setting:close`

**Message dialog:** `message:ok` · `message:cancel` · `message:close`

> Popup buttons resolve **only while that popup is open** — otherwise `null`.
> `auto:count:` index → spins: `0`=10, `1`=20, `2`=30, `3`=50, `4`=Infinity.
> `bet:row:<i>` is the **denomination index** (0 = lowest bet). The currently
> selected row reports `interactable: false` — that's correct, not a bug.

### The message dialog

`MessagePopup` is the **generic** alert/confirm used for every message in the
game — there's no dedicated popup per error. So **which buttons exist depends on
who opened it**:

| Name | Live when | Default |
|---|---|---|
| `message:ok` | caller passed `isShowOk` | **shown** |
| `message:cancel` | caller passed `isShowCancel` | hidden |
| `message:close` | caller passed `isShowClose` | hidden |

Expect `null` from the two that aren't live — that's the dialog being configured,
not a broken test. ⚠ `message:close` runs the **cancel** callback, not a neutral
dismiss.

**Insufficient balance** is the one you'll hit most. Spinning without enough
balance in base game opens this dialog with `title: "oops"`, `msg: "not_enough_money"`,
and **only `message:ok` live**:

```js
// after a spin attempt with balance < stake
const s = await page.evaluate(() => window.__gameTestBridge.getSnapshot());
expect(s.visiblePopups).toContain('message');
```

⚠ **`message:ok` reloads the page.** It's the only exit, so the player cannot
dismiss this dialog and keep playing — clicking it ends the session. Autoplay is
stopped on the way out. It cannot fire during free spins (they bet 0).

⚠ **`dismissPopups()` is not the player flow here.** It closes the dialog via
`hide()`, skipping the reload — fine for clearing the screen, wrong if you're
testing the real insufficient-funds behaviour. Click `message:ok` for that.

---

## State

| Function | Returns | Use case |
|---|---|---|
| `getSnapshot()` | `GameSnapshot` | Assert on game state. **Dump this first when a test fails** — it explains why. |

Key `GameSnapshot` fields:

| Field | Use |
|---|---|
| `balance`, `betAmount`, `betIndex`, `denominations` | money assertions (`betIndex` is the row index `bet:row:<i>` takes; `-1` = unknown) |
| `freeSpinsLeft`, `totalFreeSpins`, `autoSpinsRemaining` | spin-counter assertions (`autoSpinsRemaining` may be `Infinity` — the ∞ option) |
| `winType`, `lastPayout`, `lastAccumulatedWin` | win assertions. ⚠ small win is `'SMALL WIN'` **with a space** |
| `reelResult` | final grid as symbol IDs, `[column][row]`. `null` before any spin |
| `musicOn`, `soundOn` | sound-settings assertions |
| `roundState` | **the readiness signal.** `READY` = new spin accepted. Also `'SPINNING'` = round in flight |
| `visiblePopups`, `isPopupVisible` | what's on screen |
| `isBetInProgress`, `waitingResponse` | is a bet in flight |
| `isAutoPlay`, `isFreeSpinMode` | current mode |
| `hasActivePromo` | **use this** for "is a promo live" (server truth) |
| `isPlayingPromo` | per-spin UI flag — **don't** use it as an existence check |
| `busyOps` | animations currently running |
| `appState` | diagnostics only — **never gate on it** |

### Individual getters

`getSnapshot()` covers most assertions. These exist for the cases it can't:

| Function | Returns | Note |
|---|---|---|
| `getBalance()` / `getBetAmount()` / `getLastPayout()` | `number` | **NaN** when not yet known — never a silent `0` (0 is a legal value). |
| `getBetIndex()` | `number` | `-1` when unknown. |
| `getDefaultStake()` | `number` | **The default bet amount** — what the game snaps to. Use this for "opens at default bet". |
| `getDefaultBetIndex()` | `number` | 0-based — pass to `bet:row:<i>` to click the default row. |
| `getDefaultBetLevel()` | `number` | ⚠ The server's **1-based** config. **Not an index** — off by one. |
| `isAtDefaultBet()` | `boolean` | `betAmount === defaultStake`. |
| `getReelResult()` | `number[][] \| null` | `null` before any spin — never `[]`, so a symbol assertion can't pass vacuously. |
| `getAutoSpinsRemaining()` | `number` | `0` when autoplay is off. Can be `Infinity`. |
| `getSpeedMode()` | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` | Model truth, not the button's look — they differ mid-hold. |
| `getWinType()` | `string` | ⚠ small win is `'SMALL WIN'` **with a space**. |
| `isMusicOn()` / `isSoundOn()` | `boolean` | Sound-manager truth, not the UI toggle. |
| `getDisplayBalance()` / `getDisplayWin()` | `string` | **The on-screen text.** Lags the number while counters animate. |

> **Display strings vs numbers.** `getDisplayWin()` reads the *animating counter*;
> `getSnapshot().lastPayout` reads the *model*. During a win count-up they
> disagree, and the model is right. Only assert on display strings after the UI
> has settled — and prefer the numbers everywhere else.

### Server session payload (`gameInfo`)

| Function | Returns | Use |
|---|---|---|
| `getGameInfo()` | object \| `null` | Everything the server sent for this session. |
| `getGameSettings()` | object \| `null` | Bet limits, denominations, max payout, compliance. |
| `getPendingRound()` | object \| `null` | An **interrupted round being resumed** — why a fresh load can start mid-bonus. |
| `hasPendingFreeSpins()` | `boolean` | Resumed into an unfinished bonus. |
| `getPaytable()` | array \| `null` | `[{symbolId, value[]}]`, symbol IDs 1–8. |
| `getLastBet()` | object \| `null` | Previous round. |
| `getPromoState()` | object \| `null` | The promo record (server truth). |
| `getMultiplierLevels()` | object \| `null` | `{baseGame[], freeGame[]}` wheel values. |

> **These return copies, not live objects.** Mutating what you get back does
> nothing to the game — and the copy will *not* update when the game changes.
> Re-read the getter instead of holding one object across spins.
>
> All return `null` before the payload lands. `startGame()` waits for it, so
> they're populated for any properly bootstrapped test.
>
> They are **not** in `getSnapshot()` on purpose — too deep to copy at 20Hz.

---

## Gates

| Function | Returns | Use case |
|---|---|---|
| `canDo(action)` | `Applicability` | Ask *why* an action isn't possible, instead of waiting and timing out. |
| `isSpinEnabled()` | `boolean` | Shorthand for `canDo('spin').ok`. |

**`Applicability`** → `{ ok: boolean, reason?: string }`

**Actions:** `spin` · `stopNow` · `selectBet` · `openAutoPlay` · `toggleTurbo` ·
`openSetting` · `openHistory` · `skipBigWin` · `skipFreeSpinResult` · `forceSkip`

**`reason` is machine-parsable `prefix:value`** — branch on it:

| Reason | Meaning |
|---|---|
| `scene_not_ready` | called before `startGame()` finished |
| `round:<name>` | a round is in flight, e.g. `round:SPINNING` |
| `popup:<label>` | a popup is blocking, e.g. `popup:bigWin` |
| `busy:<ops>` | an animation is running |
| `blocked_by:<node>` | something is physically covering the button |
| `button_disabled` | the control's own `interactable` is false |
| `bet_in_flight` | a bet is in progress |
| `unresolved` | bad name / popup closed |

```js
const c = await page.evaluate(() => window.__gameTestBridge.canDo('spin'));
if (!c.ok && c.reason.startsWith('popup:')) {
  await page.evaluate(() => window.__gameTestBridge.dismissPopups());
}
```

---

## Waits

All waits reject with the **last blocking reason + a full snapshot** — never a
bare timeout. None of them are sleeps: they return as soon as the condition holds.

| Function | Returns | Use case |
|---|---|---|
| `waitForStableGameState(timeoutMs?, stableMs?)` | `Promise<'ready' \| 'freespin'>` | **Default choice after a spin.** Works in any mode; tells you where you landed. Defaults `120000`, `750`. |
| `waitForDeterministicStart(timeoutMs?, stableMs?)` | `Promise<void>` | Force a clean base game (clears popups, plays out promos). Already called by `startGame()`. Defaults `180000`, `750`. |
| `waitUntilApplicable(action, stableMs?, timeoutMs?)` | `Promise<void>` | Wait until an action becomes legal. Defaults `250`, `30000`. |
| `waitForRoundReady(timeoutMs?, stableMs?)` | `Promise<void>` | ⚠ **Base game only** — hangs during free spins. Defaults `60000`, `250`. |
| `waitForSpinComplete(timeoutMs?)` | `Promise<void>` | Spin and wait for it to finish, without clicking. Default `60000`. |

> Rule of thumb: **`waitForStableGameState()` unless you know you're in base game.**

### Bet rows — scroll helpers (added 2026-07-21)

The denominations list sits under a RECT `cc.Mask`, and Cocos clips **input** at a mask,
not just rendering. In the current layout all 20 denominations render at once in two
columns, so ordinary `waitForButtonClickable("bet:row:<i>")` is enough and these helpers
are not needed — reach for them only if a layout appears where rows are genuinely
scrolled out of view.

| Function | Returns | Use case |
|---|---|---|
| `waitForBetRowClickable(i)` | rect | **Use this for any bet row.** Scrolls row `i` into view, then waits for clickable. |
| `scrollBetRowIntoView(i)` | `boolean` | Scroll only; reports whether the row was observed to become visible. |

`i` is the **denomination index** (0 = lowest bet) — the same space as `bet:row:<i>`, not an
on-screen position.

```js
const row = await page.evaluate(i => window.__gameTestBridge.waitForBetRowClickable(i), 7);
await page.mouse.click(row.centerX, row.centerY);
```

Two traps:
- **The current bet's row is deliberately `interactable: false`** — `waitForBetRowClickable(<current>)`
  will time out. Check `getBetIndex()` first and pick a different row.
- `stakeDecrease` / `stakeIncrease` need no scroll handling, but they move the stake one step at a
  time, so they cannot reach a distant row without selecting every row in between.

**The same build made the hit test mask-aware** (`_isPointInsideAncestorMasks`): a node consumes a
touch only if the point falls inside *every* ancestor mask, so a clipped node is now neither a hit
nor a blocker. Before this, a clipped row reported `visible: true` / `isButtonClickable: true` and a
click landed on whatever was really at those coordinates. This changes hit-test results **everywhere**,
not just the bet list — it removes false positives, but any test that asserted a control was clickable
while it sat under a mask will now correctly fail.

---

## Popups

| Function | Returns | Use case |
|---|---|---|
| `isPopupVisible(label)` | `boolean` | Assert a popup is/isn't showing. |
| `dismissPopups()` | `Promise<PopupLabel[]>` | Clear the screen. **`await` it.** Returns only what it *verified* closed. |

**Labels:** `bigWin` · `getFreeSpin` · `extraFreeSpin` · `freeSpinResult` ·
`autoPlay` · `denominations` · `setting` · `information` · `history` ·
`historyDetail` · `monumentalWins` · `message` · `disconnect` · `promo` ·
`gameLimit` · `replayLanguage`

> `dismissPopups()` is async because popups fade out over 0.2s. If a label is
> missing from the returned array, it did **not** close — check the console warning.

---

## Actions without a button

Only for things a player can't do with one click.

| Function | Returns | Use case |
|---|---|---|
| `holdSpinStart()` / `holdSpinEnd()` | `void` | Hold-to-turbo (a click can't express a 240ms/760ms hold). |
| `forceSkip()` | `void` | Stop-now: cut a spin animation short. |
| `skipCelebration(label, timeoutMs?)` | `Promise<boolean>` | Skip `'bigWin'` or `'freeSpinResult'`. Default `5000`. |
| `setForcedResult(result \| null)` | `void` | **Deterministic spins.** Force the next spin's result; `null` clears. |

> `skipCelebration()` returns `false` when the game legitimately refuses to skip
> (compliance can disable it). Check the result — don't assume it worked.

> `setForcedResult()` is **one-shot** — it applies to the next spin only, then
> clears itself. When it fires, **nothing is sent to the server at all**.
>
> The payload is the **inner `result` object** of a captured `BET` socket message
> — strip the `{status, code, msg, timestamp, result}` envelope first, because the
> game re-wraps it. Pass the whole message and the *game* throws.
>
> Symbols in `views[].value` are **string names** (`"A"`,`"B"`,`"C"`,`"D"`,
> `"P1"`,`"P2"`,`"P3"`, and `"FreeSpin!"` for scatter), not numbers. A typo maps
> to `undefined` and fails **silently** — the symbol just renders broken.
>
> **Full payload reference, annotated real sample, symbol table, win/free-spin
> recipes and a grid helper: [bridge-api.md §9](./bridge-api.md).**

```js
const forced = capturedSocketMessage.result;   // ← strip one level
await page.evaluate(r => window.__gameTestBridge.setForcedResult(r), forced);
```

```js
await page.evaluate(r => window.__gameTestBridge.setForcedResult(r), myRawResult);
const spin = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('spin'));
await page.mouse.click(spin.centerX, spin.centerY);
```

---

## Events

| Function | Returns | Use case |
|---|---|---|
| `startEventCapture(label)` | `void` | Begin recording a named window. |
| `endEventCapture(label)` | `CollectedEvent[]` | Stop and get the events. Returns `[]` for an unknown label. |
| `stopEventCollection()` | `void` | Teardown. |

**`CollectedEvent`** → `{ t: number, type: string, data: object }` (`t` = seconds
since that capture started)

**Event types:** `state_change` · `round_state` · `spin_request` · `spin_result` ·
`balance_change` · `popup_shown` · `popup_dismissed` · `free_spin_start` ·
`free_spin_end` · `busy_start` · `busy_end` · `error`

### Rule verifiers

Pure functions — run them in Node on captured events, no browser needed.

| Function | Returns | Use case |
|---|---|---|
| `MysticWishesTestBridge.verifyFreeSpinAward(events)` | `VerifyReport` | Free spins awarded match scatter count (3→8, 4→10, 5→12, 6→14, 7→16). |
| `MysticWishesTestBridge.verifyBalanceMonotonicity(events)` | `VerifyReport` | Balance never goes negative/NaN. |

**`VerifyReport`** → `{ valid: boolean, violations: { rule, detail, at? }[] }`

---

## Recipes

**Start the game with a real click (instead of `startGame()`)**
```js
await page.evaluate(() => window.__gameTestBridge.waitForGameReady());
const start = await page.evaluate(() => window.__gameTestBridge.getButtonRect('startButton'));
await page.mouse.click(start.centerX, start.centerY);
await page.evaluate(() => window.__gameTestBridge.waitForDeterministicStart());
```

**Spin once**
```js
await page.evaluate(() => window.__gameTestBridge.startGame());
const r = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('spin'));
await page.mouse.click(r.centerX, r.centerY);
const mode = await page.evaluate(() => window.__gameTestBridge.waitForStableGameState());
```

**Change bet**
```js
const open = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('stakeIncrease'));
await page.mouse.click(open.centerX, open.centerY);
const row = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('bet:row:3'));
await page.mouse.click(row.centerX, row.centerY);
```

**Start autoplay for 20 spins**
```js
const a = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('auto'));
await page.mouse.click(a.centerX, a.centerY);
const n = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('auto:count:1'));
await page.mouse.click(n.centerX, n.centerY);
const ok = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('auto:confirm'));
await page.mouse.click(ok.centerX, ok.centerY);
```

**Assert a modal blocks spin**
```js
const c = await page.evaluate(() => window.__gameTestBridge.canDo('spin'));
expect(c.ok).toBe(false);
expect(c.reason).toBe('popup:setting');
```

**Debug any failure**
```js
console.log(await page.evaluate(() => window.__gameTestBridge.getSnapshot()));
```

---

## Gotchas

- **`await` `dismissPopups()`** — it's async.
- **Use `waitForStableGameState()`, not `waitForRoundReady()`**, unless you know you're in base game.
- **Use `hasActivePromo`**, not `isPlayingPromo`, to ask whether a promo is live.
- **Never gate on `appState`** — it reaches `playing` and stays there forever.
- **`buyBonus` is not supported by this game** — the button exists but does nothing.
- **Check `skipCelebration()`'s return value** — it can legitimately refuse.
- **`message:ok` reloads the page** on the insufficient-balance dialog — it ends the session, so don't click it mid-test unless that's the point.
