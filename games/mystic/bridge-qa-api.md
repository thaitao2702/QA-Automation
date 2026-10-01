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

`auto` is a **toggle**: when autoplay is off it opens the autoplay popup; when
autoplay is **running, clicking `auto` STOPS it** (calls `stopAutoplay()`). So
**stop autoplay with `auto`, not `auto:stop`** — see the note below.

**Autoplay popup:** `auto:confirm` · `auto:next` · `auto:back` ·
`auto:stopAtWin` · `auto:close` · `auto:count:0..4`

> ⚠ **`auto:stop` is effectively unreachable — don't use it.** It's the popup's
> internal stop button, shown only when the autoplay popup opens while autoplay
> is *already running*. But the only way to open that popup is the `auto`
> button, which — while autoplay runs — stops autoplay *instead of* opening the
> popup. So the popup never opens in that state and `auto:stop` resolves to
> `null`. **To stop autoplay, click `auto`.**

**Bet popup:** `bet:row:<i>` · `bet:close`

⚠ The bet list is a **scrolling, masked** list — only some rows are on screen,
and a clipped row **cannot be clicked** (Cocos clips input at a mask). Use:

| Function | Returns | Use |
|---|---|---|
| `waitForBetRowClickable(i, timeoutMs?)` | `Promise<ButtonRect>` | **The one you want.** Scrolls row `i` into view, waits until clickable, returns the rect. |
| `scrollBetRowIntoView(i, timeoutMs?)` | `Promise<boolean>` | Scroll only. `false` = it never became visible. |

```js
const row = await page.evaluate(i => window.__gameTestBridge.waitForBetRowClickable(i), 7);
await page.mouse.click(row.centerX, row.centerY);
```

`i` is the **denomination index** (0 = lowest bet), not the on-screen position.
Plain `waitForButtonClickable('bet:row:7')` still works, but only if that row
happens to already be scrolled into view.

### Scrolling the history list

The history popup is a **paginated, virtualized** list (rows recycle, and
reaching the bottom loads the next page from the server). Open it via the
`history` button, then:

| Function | Returns | Use |
|---|---|---|
| `scrollHistory(offsetFraction?, timeoutMs?)` | `Promise<number>` | Scroll to a fraction of the range: `0`=top (newest), `1`=bottom (oldest). Returns the fraction reached, or `-1` if the popup is closed / nothing to scroll. Default `1`, `1500`. |

```js
// pull more history rows (scroll to the bottom triggers the next page)
await page.evaluate(() => window.__gameTestBridge.scrollHistory(1));
```

> `scrollHistory(1)` may land **short of 1** — reaching the bottom appends a new
> page, extending the content. That's the pagination working, not a failure.
> Call it again to load further pages.

**Clicking a history row (to open its detail).** The list is **virtualized** —
only the on-screen rows exist as nodes, so rows aren't pre-named. Read the ones
currently rendered and click the one you want:

| Function | Returns | Use |
|---|---|---|
| `getHistoryRows()` | `Array<{ index, rect, clickable, data }>` | The rows rendered right now. `index` = data index (0 = newest); `rect` = clickable rect; `clickable` folds in the mask (an off-screen row is `false`); `data` = the raw history row (identify the bet by `data.betId` etc.). |

```js
// open history, then click a specific rendered row's detail
const rows = await page.evaluate(() => window.__gameTestBridge.getHistoryRows());
const target = rows.find(r => r.clickable);        // or match on r.index / r.data
await page.mouse.click(target.rect.centerX, target.rect.centerY);   // opens historyDetail
```

> To click a row that isn't rendered yet, `scrollHistory()` to bring it into
> view, then call `getHistoryRows()` again — the node set changes as you scroll.
> The bridge does **not** interpret `data`; the framework reads whatever fields
> it needs off the raw row.

### Navigating a round's detail (the free-spin chain)

Clicking a history row opens **`historyDetail`** — the per-round breakdown. It's
**paged**: one page per bet in the round, and **for a free-spin round each free
spin is its own bet**, so paging next/prev walks the whole free-spin chain
(plus a synthetic max-win page when the round hit max win). Two nav targets,
same coordinate-click model as every other button:

| Name | Moves to |
|---|---|
| `historyDetail:next` | next free spin / page |
| `historyDetail:prev` (alias `historyDetail:back`) | previous free spin / page |

Read the paging state to drive and bound the walk:

| Function | Returns | Use |
|---|---|---|
| `getHistoryDetailState()` | `{ open, betIndex, betCount, hasPrev, hasNext }` | `open` = detail popup showing. `betIndex` = current page (0-based). `betCount` = bets in the round (free spins included). `hasPrev`/`hasNext` = a page exists that way (read from the popup's own nav buttons). Returns `open:false` (empty fields) when closed. |

```js
// open a row's detail (see above), then walk every free spin forward
let s = await page.evaluate(() => window.__gameTestBridge.getHistoryDetailState());
while (s.hasNext) {
  const r = await page.evaluate(() => window.__gameTestBridge.getButtonRect('historyDetail:next'));
  await page.mouse.click(r.centerX, r.centerY);
  s = await page.evaluate(() => window.__gameTestBridge.getHistoryDetailState());
  // assert on this page here — betIndex has advanced
}
```

> ⚠ **Loop on `hasNext`/`hasPrev`, not on `betIndex` vs `betCount`.** A max-win
> round adds an extra page beyond `betCount`, so the real page count can be
> `betCount + 1`. The flags come straight from the popup's own nav buttons, which
> already fold in the ends and the max-win page.
>
> The nav buttons are **hidden at the ends** — on the first page `historyDetail:prev`
> is `visible:false` / not clickable; on the last, `historyDetail:next` is. That's
> the popup, not a bug: `isButtonClickable('historyDetail:next') === false` is your
> "no more free spins" signal, and it's exactly what `hasNext` reflects.

### Scrolling the tutorial (WebView — a FRAMEWORK job)

The tutorial (the **info** popup) is a `cc.WebView` whose guide page is served
**cross-origin** (from `staticUrl`). The browser blocks any in-page JS
(`evaluateJS`, `contentWindow.*`, synthetic events) from touching it — so the
**bridge cannot scroll it**. Only a real `page.mouse.wheel` from the framework
routes into a cross-origin iframe. The bridge just gives you *where* to wheel:

| Function | Returns | Use |
|---|---|---|
| `getTutorialRect()` | `ButtonRect \| null` | The WebView's on-screen rect. `null` if the info popup is closed. |

```js
// step top→bottom, pausing so an AI assertion catches each viewport
const r = await page.evaluate(() => window.__gameTestBridge.getTutorialRect());
await page.mouse.move(r.centerX, r.centerY);
const step = Math.round(r.height * 0.9);   // 90% → 10% overlap, nothing skipped
for (let i = 0; i < 12; i++) {             // fixed count — see note below
  await page.mouse.wheel(0, step);
  await page.waitForTimeout(500);
}
```

> **Bottom detection cross-origin is impossible** — neither the bridge nor
> Playwright can read a cross-origin iframe's scroll position. So use a **fixed
> step count**; over-wheeling past the end is harmless (the browser clamps at the
> bottom, and the AI just sees the last viewport repeated). Pick a count that
> covers your guide's length.
>
> ⚠ Pair with an image/video assertion if you need proof the content scrolled.

**Settings popup:** `setting:turbo` · `setting:music` · `setting:sound` ·
`setting:turboTab` · `setting:history` · `setting:info` · `setting:gameLimit` ·
`setting:close`

**Message dialog:** `message:ok` · `message:cancel` · `message:close`

**History detail:** `historyDetail:prev` · `historyDetail:next` — page a round's
detail; for a free-spin round they walk the free-spin chain (see *Navigating a
round's detail* above).

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

## Spinning a round (the API-first loop)

Round truth comes from the **server response**, not client state. This is the
portable part of the contract — the same shape across any game on this backend.

| Function | Returns | Use case |
|---|---|---|
| `waitForSpinClickable(stableMs?, timeoutMs?)` | `Promise<ButtonRect>` | **Start here.** Waits until clicking spin would actually send a bet. Defaults `250`, `60000`. |
| `isSpinClickable()` | `boolean` | Non-blocking assertion. |
| `getBetSeq()` | `number` | Marker — take it **before** clicking. |
| `waitForRoundComplete(sinceSeq?, timeoutMs?)` | `Promise<RoundSummary>` | Rides out the whole chain: free spins, retriggers, promo, max-win. Default `300000`. |
| `waitForBetResponse(sinceSeq, timeoutMs?)` | `Promise<BetRecord>` | One response at a time, if you need per-spin assertions. |
| `getBetLog()` / `getLastBetResponse()` | `BetRecord[]` / `BetRecord \| null` | Everything seen this session. |

```js
const seq = await page.evaluate(() => window.__gameTestBridge.getBetSeq());
const spin = await page.evaluate(() => window.__gameTestBridge.waitForSpinClickable());
await page.mouse.click(spin.centerX, spin.centerY);
const round = await page.evaluate(s => window.__gameTestBridge.waitForRoundComplete(s), seq);
// round.spins[], round.terminal, round.totalPayout, round.hasReachedMaxWin
```

That's the whole loop. `waitForRoundComplete` returns only when the chain has
ended **and** the spin button is clickable again — so it absorbs cascades,
big-win popups, the free-spin result popup and mode transitions without you
naming any of them.

### `BetRecord`

`{ seq, t, gameState, freeSpins, totalFreeSpin, payout, balance, hasReachedMaxWin, roundId, continues, result }`

`gameState`: `1`=BaseGame · `2`=FreeGame · `3`=BuyBonus · `4`=PromoGame

> ⚠ **`freeSpins` is REMAINING, not awarded — and it is not a loop variable.**
> A max-win truncates the chain server-side and returns `BaseGame` with
> `freeSpins` still **> 0** (those spins are forfeited). Terminating on
> `freeSpins === 0` hangs forever on every max-win round. Use `continues`.

`continues` is derived: `FreeGame`/`BuyBonus`, or `PromoGame` with `freeSpins > 0`.
It's the client's own rule — those are the only states from which another spin
is self-fired.

### `RoundSummary`

`{ spins[], terminal, totalPayout, hasReachedMaxWin, forfeitedFreeSpins }`

### What `waitForSpinClickable` actually checks

Gate C (the game's own guard, mirrored so it can't drift) then Gate A (the click
must land):

| Reason | Meaning |
|---|---|
| `spin_state:STOP` | mid-spin — clicking would **skip the animation**, not bet |
| `spin_state:UNACTIVE` / `SPINNING` | disabled |
| `spin_mode:AUTO` | autoplay running — state is idle but the click is a no-op |
| `spin_mode:FREE_SPIN` | free spins running |
| `lock_key_down` | **invisible** — a popup set it and didn't clear it |
| `blocked_by:<node>` | something covers the button |

> ⚠ It will **not** resolve during autoplay or free spins — correctly, since you
> can't manually spin then. Turn autoplay off, or ride the chain out with
> `waitForRoundComplete()` first.

> ⚠ **It does not check your balance.** The button stays clickable with
> insufficient funds; the game lets you click and *then* opens the
> insufficient-funds dialog, whose only button **reloads the page**. Assert
> `balance >= betAmount` yourself before long loops. If a bet never arrives,
> the wait's timeout reports why (`insufficient_balance`, `connection:...`,
> `popup:...`).

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
>
> **Captured real payloads + an annotated field table: [bet-response.md](./bet-response.md)**
> (`fixtures/bet-responses/*.json` are ready to pass straight to `setForcedResult`).

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

**Stop autoplay** — click `auto` again (NOT `auto:stop`)
```js
// while autoplay is running, in base game:
const a = await page.evaluate(() => window.__gameTestBridge.waitForButtonClickable('auto'));
await page.mouse.click(a.centerX, a.centerY);
// confirm: getSnapshot().isAutoPlay === false
```
> ⚠ `auto` is only clickable to stop autoplay **in base game**. During a
> free-spin / promo chain that autoplay triggered, the `auto` button is disabled
> — autoplay ends on its own when the bonus finishes; you can't stop it mid-bonus.

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
- **Never terminate a free-spin loop on `freeSpins === 0`** — max-win rounds end with spins still on the clock. Use `continues` / `waitForRoundComplete()`.
- **`waitForSpinClickable()` won't resolve during autoplay or free spins.** That's correct, not a hang — you can't manually spin then.
