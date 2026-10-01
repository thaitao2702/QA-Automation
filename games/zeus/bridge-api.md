# Zeus Slot — Test Bridge API (v3)

Surface reference for the QA framework. Implements `game-bridge-contract.md`.

Source: [`assets/src/test/ZeusBridgeV3.ts`](assets/src/test/ZeusBridgeV3.ts) ·
typed surface: [`assets/src/test/IBridgeV3.ts`](assets/src/test/IBridgeV3.ts) ·
install: [`assets/src/test/BridgeActivator.ts`](assets/src/test/BridgeActivator.ts)

> `bridge-api.md` documents the **v2** bridge and is superseded by this file.
> Its `startGame()`, `waitForDeterministicStart()` and `waitForStableGameState()`
> no longer exist — see [§12 Migrating from v2](#12-migrating-from-v2).

**The split.** The bridge exposes the *surface* — button rects, popup visibility,
readiness, and the raw server responses. The framework owns the *logic* — payouts,
win tiers, bonus math, every rule assertion — and computes it from the raw BET
response. **The bridge never tells you what a round did.**

**Single authority per fact.** Exactly one signal answers each question, and
nothing cross-checks anything. Two judgements of the same fact can disagree, and
whichever one breaks the tie quietly becomes the real contract — which is how a
broken bridge keeps reporting green.

| Question | The only answer |
|---|---|
| did the game start? | `startGame()` |
| is a bonus owed? | `getGameInfo().pending` |
| did a round happen, and what did it pay? | `awaitBetResponse()` / `awaitFreespinRound()` |
| is the game back to a clean idle? | `waitForSpinClickable()` |

Everything else in the snapshot is a **display value** or a **diagnostic**. Neither
decides anything. That distinction is enforced in §5 and it matters.

---

## 1. Access

| | |
|-|-|
| Gate | `?bridge=1` — query string **or** hash-routed `#/path?bridge=1` |
| Global | `window.__gameTestBridge` |
| Install failure | `window.__gameTestBridgeError` — a stack, present only on failure |
| Version | `bridge.version` → `'zeus-bridge-v3/<date>'` |
| Available from | as soon as `gameInfo` lands — **before** the Start button appears |
| Teardown | `BridgeActivator.deactivate()`, or `bridge.dispose()` |

Without `?bridge=1` nothing is constructed, no hook is installed, and the
production path costs nothing.

```ts
await page.waitForFunction(() => !!window.__gameTestBridge);
const version = await page.evaluate(() => window.__gameTestBridge.version);
```

⚠ **If the global never appears, read `window.__gameTestBridgeError` before
anything else.** Installation is wrapped in a try/catch precisely so a failure
leaves a cause behind. A bridge that failed to install must not look the same as
one that was never requested — that ambiguity is what made the previous build's
"the bridge is in the bundle but `window.__gameTestBridge` never appears" cost
days rather than minutes.

### Install order

| Hook | Called from | Does |
|---|---|---|
| `BridgeActivator.arm()` | `LoadingScene.onEnable()`, pre-boot | Polls until `GameLogic.gameInfo` exists, then installs. This is what makes the `gameInfo` capture reliable. |
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
const result = await bridge.awaitBetResponse();

// ---- a bonus the round triggered ----------------------------------------
while (!isTerminal(result)) {
    result = await bridge.awaitFreespinRound();  // passive — never click
}
await bridge.waitForSpinClickable();             // settled
```

`isTerminal` is **framework code**: `freeSpins === 0 || gameState === base`. Both,
because `hasReachedMaxWin` can truncate either way — a max-win round returns to
base with `freeSpins > 0` and those spins are forfeited, so a
`freeSpins === 0` loop would hang waiting for rounds that never arrive.

**The bridge returns coordinates, not click methods.** The framework issues a real
`page.mouse.click(centerX, centerY)`, so the true input pipeline is exercised.

---

## 3. REQUIRED — the suite does not run without these

### `startGame(): Promise<void>`

**Does.** Clicks the game's Start button and resolves the instant that click has
been issued.

**When.** Once per page load, and again after every reload, language switch, or
second tab session. Repeat calls are harmless.

**Does NOT** wait for the scene, wait for idle, play out a resumed bonus, or
dismiss popups. All of that is yours.

**Rejects** if no Start handler ever appears (60s), naming the app state it was
stuck in. It never hangs — a bridge that hangs here is indistinguishable, from
outside, from a dead server.

> ⚠ This is the one function whose semantics changed from v2. v2 awaited
> `waitForDeterministicStart()` internally, whose predicate required
> `autoSpinsRemaining === 0` while a fresh session reports `-1` — the promise
> never settled, and its popup-dismiss loop closed every popup a test opened,
> ~0.4s after it opened. Both are gone.

---

### `getGameInfo(): GameInfo`

**Does.** Returns the launch payload the bridge **captured at load**, unchanged,
for the life of the session.

**When.** Once, immediately after `startGame()` resolves. Read two things from it:

| Read | For |
|---|---|
| `pending` | *does this session owe a bonus?* — the **only** resume signal |
| `settings.denominations`, `settings.defaultBetLevel`, `currency`, `balance`, `settings.minBet/maxBet/maxPayout`, `settings.compliance`, `paytable`, `freeSpinPackages` | *what is configured?* |

**Shape.** The bridge returns the **inner** object — what the server put in
`response.result` — not the `{status, code, result}` envelope. Read `pending`
directly off it. (The framework unwraps either form, so the defensive
`info.result?.pending ?? info.pending` in §2 stays correct against both this
bridge and games that hand back the envelope.)

**Never throws.** Safe to call at any time.

> ⚠ **It is a captured COPY, and that is load-bearing.** `GameLogic` clears
> `gameInfo.pending.freeSpins` while it resumes a bonus. A live re-read taken
> after the Start click — which is exactly when you call this — reports "nothing
> owed" for a session that is mid-bonus. You would never call
> `awaitFreespinRound`, and those rounds would arrive as the first responses of
> whatever scenario ran first: `conservation` fails on a stake it never placed,
> and the balance-debit case grades a spin the player did not pay for.

> ⚠ **`pending` decides, always — never `lastBet`.** A max-win-truncated `lastBet`
> reads like a live bonus through the same parser (`freeSpins: 8`,
> `hasReachedMaxWin: true`) while `pending` correctly says nothing is owed. A boot
> driven off `lastBet` waits for 8 free-spin rounds that never arrive and fails a
> perfectly healthy session.

> ⚠ **`settings.defaultBetLevel` is an INDEX into `denominations`**, and it only
> applies to a player's **first** session. Every later session opens at
> `lastBet.baseStake`. So:
> `expected_open_bet = lastBet ? lastBet.baseStake : denominations[defaultBetLevel]`.
> An "opens at the default bet" case must **not** assert
> `betIndex === defaultBetLevel` — on a returning session that is a false failure
> against a game behaving correctly.

**Companion:** `isGameInfoCaptured(): boolean` — false means the capture window
was missed and you are reading a live clone. Check it when a resume goes missing.

---

### `getSnapshot(): GameSnapshot`

**Does.** Returns the current surface state — popups, autoplay, bet, balance.

**When.** In loops, and when explaining a failure. Synchronous, cheap (field reads
plus one pass over a static popup table), safe at **any** time including mid-spin,
mid-animation and mid-bonus, and it **never throws**.

**Carries no round outcomes.** Those come from the BET response, so the bridge
never has to be right about game logic.

See [§5](#5-gamesnapshot--field-by-field) for the field-by-field roles.

---

### `getButtonRect(name: string): ButtonRect | null`

**Does.** Where a named control is **right now**, without waiting.

**When.** Two distinct uses, and the second is why `null` matters as much as the
rect:

1. aim a real mouse click;
2. **probe whether a control exists in this build at all.**

**Three outcomes, never collapsed:**

| Return | Means |
|---|---|
| `null` | unknown name, **or** a popup-scoped control whose popup is closed |
| `{ visible: false, … }` | present, but inactive or off-canvas |
| `{ visible: true, … }` | on screen — may still be covered; ask `isButtonClickable()` |

**Never throws.** A throw here would turn "this build has no Ante button" into a
crashed run instead of one *unreached* scenario. Answers immediately — no waiting,
no retry.

```ts
// feature probe
const ante = await page.evaluate(() => window.__gameTestBridge.getButtonRect('anteBet'));
if (!ante) return unreached('no Ante control in this build');
```

Names: [§9](#9-button-name-registry).

---

### `waitForButtonClickable(name, stableMs = 250, timeoutMs = 30_000): Promise<ButtonRect>`

**Does.** Waits until the control can **genuinely** be clicked, then returns its
rect — re-read at resolve time, so you can never be handed a position the control
has since moved off.

**When.** Before every UI interaction: open a popup, pick a denomination, toggle a
setting, confirm autoplay.

Resolves only when a click would actually register: visible, hit-testable, not
covered by another layer, and stable for `stableMs`.

**Rejects** on timeout with the last blocking reason plus a snapshot line, and
**rejects immediately** for a name this build has no concept of — the answer will
not change, so burning 30s to say so only slows the run.

⚠ **Argument order is `stableMs` first**, unlike the removed v2
`waitForDeterministicStart`.

---

### `waitForSpinClickable(stableMs = 250, timeoutMs = 60_000): Promise<ButtonRect>`

**THE single readiness / settle authority.** Nothing cross-checks it, which is why
its contract is the strictest here.

**Three uses, all the same condition:**

1. before every spin — so the click is not swallowed;
2. the **settle signal** after a round — so the recorded video covers the whole
   animation;
3. the **boot idle assertion** — "the game is back to a clean, spinnable base".

Resolves only when **all** hold:

| Clause | Closes |
|---|---|
| state is `IDLE` | a bet on the wire, or a result still playing out |
| not autoplay, not free spins / bonus | the client is driving itself — a click would be swallowed or stack an extra bet |
| `busyOps` empty | **the settle guarantee.** The scene re-arms the spin button *before* the celebration finishes |
| no popup open | something is covering the button |
| visible + interactable + hit-test | the lock flag, and an overlay the flags know nothing about |

**Rejects** on timeout rather than resolving optimistically.

> ⚠ **Readiness is about the UI, not the wallet.** An insufficient balance does
> **not** hold this back: the spin button stays clickable and the client shows its
> out-of-money popup **on click**. That popup is the subject of ZZ-07 / Mah_009 /
> CR_009 — gating readiness on affordability would make the boot assertion demand
> the very thing those cases exist to disprove.

> ⚠ Do **not** cross-check this with a `busyOps` or `visiblePopups` poll of your
> own. Those fields are diagnostics; they explain a refusal, they never decide
> one. A second opinion that can disagree quietly becomes the real contract.

---

### `awaitBetResponse(timeoutMs = 15_000): Promise<BetResult>`

**Does.** Resolves with the **next** BET response, **raw and unmodified**.

**When.** Immediately after any click that sends a bet, and to observe rounds the
client fires during autoplay.

- **Does not click.** Only the framework can issue a real click.
- **Cannot miss a response that arrived between your click and this call** — the
  tap is live from construction and a buffered response is handed over
  synchronously.
- **Rejects** on timeout with `{ reason: 'bet_response_timeout', snapshot }`.

**One response consumed per round, in order.** A single shared cursor is what
makes "click, then await" race-free without a request marker — the Zeus protocol
carries no client bet id, so correlation is by arrival order. Skip or
double-consume one and every later correlation is off by one.

Dev-mode forced results arrive here too, indistinguishable from wire responses.

---

### `awaitFreespinRound(timeoutMs = 90_000): Promise<BetResult>`

**Does.** The next auto-played free-spin / Hold & Spin round, raw.

**When.** To play a bonus out without clicking — both mid-scenario (a bonus you
triggered) and at boot (a bonus this session resumed).

Mechanically identical to `awaitBetResponse` — same shared cursor, so rounds are
consumed once and in order — but **passive** by contract: never click during a
bonus, and neither does this. The longer default is because a bonus round includes
its own presentation.

**Termination is yours**, read off the response. Never loop on a remaining-count
alone.

---

## 4. Strongly recommended — `canDo(action)`

```ts
canDo(action: string): { ok: boolean, reason?: string }
```

**DIAGNOSTIC ONLY.** Readiness is always decided by `waitForSpinClickable` /
`waitForButtonClickable`, never by this. Its whole value is that when one of those
times out, `reason` turns "it never became clickable" into `popup_open:buyBonus`.

**Actions**

| Group | Actions |
|---|---|
| play | `spin` · `stopSpin` |
| bet | `openBetPopup` · `nextBet` · `previousBet` · `selectBet` |
| autoplay | `openAutoPopup` · `selectAutoSpinPackage` · `toggleAutoSpinStopAtWin` · `confirmAutoSpin` · `stopAutoSpin` |
| buy bonus | `openBuyBonusPopup` · `selectBuyBonusPackage` · `confirmBuyBonus` |
| toggles | `setTurbo` · `setSound` |
| panels | `openSettings` · `openInfo` · `openHistory` · `openTrophy` |

An unrecognised action returns `unknown_action:<name>` rather than a
plausible-looking `false` — a caller bug should not look like a game state.

**Reason vocabulary** — stable, machine-parsable, either a bare token or
`prefix:value`.

| Reason | Means |
|---|---|
| `scene_not_ready` | the gameplay scene is not up, or its input handlers are not attached |
| `state:<s>` | app state is not `idle` — a bet is on the wire or a result is presenting |
| `autoplay_running` / `free_spins_running` | the client is driving itself |
| `busy:<ops>` | a tracked animation is in flight (see `busyOps`) |
| `popup_open:<label>` | a popup is covering the main screen |
| `popup_not_open:<label>` | a popup-scoped control was asked for while its popup is closed |
| `spin_locked` | `GameplayScene._isLockSpinButton` |
| `not_spinning` | `stopSpin` while already idle |
| `auto_not_running` | `stopAutoSpin` with no run in progress |
| `no_packages` | buy bonus with an empty package list |
| `unknown_name` | no control of that name exists in this build |
| `unresolved:popup_closed_or_absent` | a known name that does not resolve right now |
| `not_visible` | inactive, no transform, or off-canvas |
| `button_disabled` | `interactable === false` |
| `clipped_by_mask` | scrolled out of an ancestor `Mask` — valid coordinates, unreachable |
| `blocked_by:<node>` | another input-consuming node covers the centre; `<node>` names it |
| `no_consumer_at_point` | nothing would receive the click there |
| `inactive_hierarchy` / `target_null` | the node is off, or the reference is null |

> ⚠ **A control that OPENS a popup is never gated on that popup being open.**
> v2 reported `popup_not_open:bet` for `nextBet` — a button whose entire job is to
> open the bet popup (defect **D2**). Fixed: `nextBet`, `previousBet` and
> `openBetPopup` all check main-screen preconditions instead.

---

## 5. `GameSnapshot` — field by field

Three roles, and **the split is the point**: a display value used as a decision
input is exactly what defect D1 was.

| Field | Type | Role | Use it for |
|---|---|---|---|
| `visiblePopups` | `string[]`, front-most first | **asserted** | every popup case; `[]` when none, never `null` |
| `isAutoPlay` | `boolean` | **asserted** | autoplay cases |
| `balance` · `betAmount` · `betIndex` | `number` | **asserted** | balance / bet cases (the dedicated getters are preferred) |
| `denominations` | `number[]` | **asserted** | denomination cases |
| `autoSpinsRemaining` | `number` | **UI display value** | *does the counter on screen say what it should?* — and nothing else |
| `freeSpinsRemaining` | `number` | **UI display value** | same |
| `sceneReady` | `boolean` | diagnostic | named in failure messages |
| `busyOps` | `string[]` | diagnostic | explains why spin is not yet clickable |
| `isFreeSpinMode` | `boolean` | diagnostic | explains a refusal during a bonus |
| `buyBonusPopupLoaded` | `boolean` | extra | the popup is lazy — false means it has never been opened |
| `freeSpinPackageCount` | `number` | extra | how many packages the server offered |
| `buyBonusSelectedPrice` | `number` | extra | price of the **checked** package, `0` when none is selected or the popup is closed |

> ⚠ **`autoSpinsRemaining` / `freeSpinsRemaining` are what the player can see,**
> not game state. That makes them the right subject for a display case and the
> wrong input for any decision. Game state comes from the BET response; resume
> state comes from `pending`. `autoSpinsRemaining` is normalised: the production
> field's `-1` sentinel ("never been set") is reported as `0`, and an unlimited
> run stores a large positive value, so clamping cannot hide one.

> ⚠ **`busyOps` values** are labels for wrapped animations:
> `transitionToHoldSpin` · `transitionToBaseGame` · `triggerBonus` ·
> `highlightCoinSymbols` · `bigWin.show` · `totalWin.show`. Diagnostic does not
> mean loose — a wrong `busyOps` sends someone chasing the wrong bug. It means
> the *authority* for readiness is `waitForSpinClickable`, and this is how the
> bridge explains itself when that call refuses.

**No `appState`.** It is implemented differently game to game, so nothing portable
can be built on it.

---

## 6. Optional getters

Missing → the dependent scenario is **unreached** (*inconclusive*), never a false
pass or a false failure.

| Function | Returns | When to use |
|---|---|---|
| `getBetAmount()` | `number` | check the bet actually changed after a bet interaction; size a balance drain. The **value**, not a ladder position, not a formatted string |
| `getBetIndex()` | `number` | verify a denomination pick landed on the row it clicked. Index into `getDenominations()`, `0` = lowest — the same index that addresses `bet:row:<i>` |
| `getDenominations()` | `number[]` | check the offered ladder against config; choose a bet by value rather than guessing an index. Ascending |
| `getBalance()` | `number` | size a drain to insufficient funds; compare displayed vs reported. ⚠ **displayed**, not the authority — post-payout balance comes from the BET response |
| `getSpeedMode()` | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` | make "Turbo really engaged" an exact check instead of asking an AI to judge spin duration from video |
| `isSoundEnabled()` / `isMusicEnabled()` | `boolean` | prove a mute toggle took effect — the alternative is an AI "hearing" a silent video. Reported independently, since the cases toggle them separately |
| `getTutorialRect()` | `ButtonRect \| null` | know **where to put the mouse** for the help WebView. `null` when the popup is closed |
| `scrollBetRowIntoView(i)` | `void` | reach denominations below the fold in the scrolling bet list |
| `getHistoryRows()` | `HistoryRow[]` | assert what History lists; get the rect of a row to click |
| `getHistoryDetailState()` | `HistoryDetailState` | walk a History detail ticket's pages |
| `getMultiplierLevels()` | `MultiplierLevels \| null` | compare reported multipliers against config |
| `dismissPopups()` | `Promise<string[]>` | teardown between runs; enter each surface from a known state |
| `startEventCapture(l)` / `endEventCapture(l)` | `void` / `BridgeEvent[]` | prove something too brief to poll actually happened |
| `setForcedResult(r)` / `setDevModeResult(grid)` | `void` | reach a specific outcome without spinning until it happens by chance |

### `getSpeedMode()`

Zeus models the speed toggle as a pair of Toggles named for the **icon** they
show, not the state they represent — reading the obvious field returns the exact
opposite of the truth. This returns the effective mode, and mirrors the game's own
`offTurboButton.isChecked || _isHoldSpinButton`.

`'UNKNOWN'` is returned when the scene is not up. **`'UNKNOWN'` is a valid
answer** — treat it as unanswerable, not as a failure. A confident wrong answer is
far worse.

### `getTutorialRect()`

Returns the rect of the **WebView itself**, not of the popup containing it — the
framework sizes each wheel step from `height` to about one screenful with a 10%
overlap, so a popup-sized rect leaves bands of the guide unscrolled between
frames.

> ⚠ **The bridge cannot scroll it, and neither can any injected JS.** The guide is
> served cross-origin from `staticUrl`, so the browser blocks it. Only a real
> `page.mouse.wheel` routes into that frame. Scroll *position* is unreadable for
> the same reason — there is no way to ask "am I at the bottom?" — so wheel a
> deliberately over-generous fixed number of steps and let the browser clamp.
> Over-wheeling is harmless: the last screenful just repeats. Proof that the
> pixels moved can only come from the video.

```ts
const t = await bridge.getTutorialRect();
if (!t) return unreached('tutorial not open');
for (let i = 0; i < 12; i++) {
    await page.mouse.move(t.centerX, t.centerY);
    await page.mouse.wheel(0, t.height * 0.9);
}
```

### `scrollBetRowIntoView(index)`

Accepts the **denomination index** (`0` = lowest) and centres that row in the
list's window. Without it only the rows that happen to be on screen are clickable,
and the high and low ends of the ladder cannot be tested at all.

Returns nothing and never throws — wrap it and ignore the result, then dwell so
the scrolled-to row lands in frame.

### `getHistoryRows()`

```ts
{ index: number, rect: ButtonRect | null, clickable: boolean, data: Record<string, string> }[]
```

Returns **only rows actually rendered**. History is virtualised, so this is a
subset — never a synthesised full list, because you click the rects you are given.
Scroll and re-read to reach more.

`rect` is in `getButtonRect` space. `clickable` is honest: mask-aware and
hit-tested. `data` carries the row's displayed values —
`time` · `date` · `betId` · `betAmount` · `payout` · `freeSpin` · `anteBet` ·
`promoType`, each present only when that label is actually shown.

### `getHistoryDetailState()`

```ts
{ open: boolean, betIndex: number, betCount: number, hasPrev: boolean, hasNext: boolean }
```

Zeus's detail ticket is a **two-view** popup, not a per-bet pager: view `0` is the
round summary, view `1` the respin list. `hasNext` mirrors the real Next control's
own visibility, which the popup enables only when a respin page exists.

**Loop on `hasNext` / `hasPrev`.** Never do arithmetic over `betCount` — the flags
are what make a round with an unusual page count page correctly.

### `getMultiplierLevels()`

Returns `null` on this build: the Zeus payload carries no `multiplierLevel`.

That is deliberate — `null` means *inconclusive*, not *no multipliers*. Inventing
a ladder would grade against fiction, and "+1 per step" is wrong on every game
checked so far. If the server starts sending `multiplierLevel`, this returns
`{ baseGame: number[], freeGame: number[] }` with no code change.

### `dismissPopups()`

Closes every open popup via its **real close path** and returns only the labels it
**observed** close, using the same names as `getSnapshot().visiblePopups`.

Safe to call when nothing is open. Call it without checking first.

> Verification is the point. A closer can throw on a null ref, or run without
> closing anything, or be a guarded no-op. Returning the *attempt* list would tell
> you the screen is clear when it is not, and your next wait then times out on a
> reason you were just told was resolved. Anything still up is `console.warn`ed and
> left out of the return value.

Two Zeus specifics it handles for you: the celebration popups are dismissed via
`skip()` not `hide()` (`hide()` strands the promise `show()` returned and the game
never reaches idle again), and `SettingPopup.hide()` never clears the input lock
its `show()` set — so a programmatic dismiss would otherwise leave every later
spin click silently swallowed.

### `startEventCapture(label)` / `endEventCapture(label)`

Opens a named recording window, and closes it returning everything captured.
Labelled and independent; `endEventCapture` returns `[]` rather than throwing for
an unknown label.

**When.** To prove something too brief to poll actually happened — a popup that
opens and closes in 300ms is missed by any sampling loop but shows up here.

```ts
await bridge.startEventCapture('turbo-toggle');
await page.mouse.click(t.centerX, t.centerY);
const events = await bridge.endEventCapture('turbo-toggle');
```

| Event `type` | `data` | Emitted when |
|---|---|---|
| `popup_shown` | `{ popup }` | a popup became visible |
| `popup_dismissed` | `{ popup }` | a popup stopped being visible |
| `bet_response` | `{ seq }` | a round response entered the buffer |
| `busy_start` / `busy_end` | `{ op }` | a tracked animation began / finished |

`t` is ms since that window opened, so ordering within a window is recoverable.
The popup watcher runs **only while a capture is open**, so an idle bridge costs
nothing.

### `setForcedResult(result | null)` / `setDevModeResult(grid)`

`setForcedResult` queues the **inner** result object (what the server puts in
`response.result`, not the socket envelope) for the next spin. `null` clears the
queue. `setDevModeResult(grid)` is the convenience form for a 3×3 grid —
`grid[col][row]`, col `0` = left reel, row `0` = top.

The forced round reaches `awaitBetResponse()` exactly like a wire response, which
is the point: assert against forced and real rounds identically.

> ⚠ **One round only.** A multi-spin sequence — Hold & Spin, Buy Bonus, a whole
> free-spin run — is server-authoritative and a client-side forced result cannot
> steer it. Those need a BO paytable fixture.

---

## 7. Diagnostics

| Member | Returns | When to use |
|---|---|---|
| `diagnose()` | `object` | **first**, on any hang. State, capture status, hooks, the scene's own start error, spin handler attachment, the bet cursor, the snapshot, every button rect, and the spin clickability breakdown — in one call |
| `getBetLog()` | `readonly BetRecord[]` | every response seen so far, raw. Does **not** move the cursor |
| `getUnconsumedBetCount()` | `number` | should be `0` between rounds. Non-zero means a caller skipped a response and the one-per-round invariant is broken |
| `isGameInfoCaptured()` | `boolean` | `false` explains a missed resume — you are reading a live clone, not the pre-Start capture |
| `getAllButtonRects()` | `Record<string, ButtonRect>` | everything resolvable right now — main buttons plus each open popup's controls |
| `isButtonClickable(name)` | `boolean` | synchronous counterpart to `waitForButtonClickable` |
| `isPopupVisible(label)` | `boolean` | one popup, by label |
| `getFreeSpinsRemaining()` / `getAutoSpinsRemaining()` | `number` | the same UI display values the snapshot carries |
| `hitTestDebug` | `boolean` field | set `true` to `console.warn` the blocker name and centre on every hit-test refusal. Off by default — cases that assert "cannot click" would otherwise spam |
| `logBetResponses` | `boolean` field | one expandable console line per round. On by default; set `false` during a long autoplay run |
| `dispose()` | `void` | detach the tap and every timer |

```ts
// the first thing to run when a wait times out
console.log(await page.evaluate(() => window.__gameTestBridge.diagnose()));
```

---

## 8. Types

```ts
interface ButtonRect {
    x: number; y: number; width: number; height: number;
    centerX: number; centerY: number;    // → page.mouse.click
    visible: boolean;                    // renderable AND centre on-canvas
    interactable: boolean;
}
```

Page-CSS pixels, origin at the top-left of the viewport, already including the
canvas element's offset and already divided by `devicePixelRatio`. **Not** Cocos
world units, **not** physical render pixels.

The rect is the node's **own** rect — contentSize × anchor × world matrix,
children excluded. Never a children-union box: a 160×160 button with a full-screen
highlight child would otherwise report a hit region covering the whole screen, and
every spin wait would hang on a false `blocked_by:`.

When `visible` is `false` the numeric fields may be `NaN` — do not click a
non-visible rect.

```ts
interface BetRecord { seq: number; t: number; result: unknown; }
interface CanDoResult { ok: boolean; reason?: string; }
interface BridgeEvent { t: number; type: string; data: Record<string, unknown>; }
type SpeedMode = 'NORMAL' | 'TURBO' | 'UNKNOWN';
```

---

## 9. Button name registry

Addressed by **name**. Popup-scoped names resolve **only while their popup is
open**; otherwise `getButtonRect` returns `null` and `waitForButtonClickable`
rejects.

> **The rule that makes all of them testable:** a control resolves only when
> clicking it would actually do its job. A control that resolves while inert is
> the worst case for a suite — the click lands, nothing happens, and the failure
> surfaces later somewhere unrelated.

### Main screen — always resolvable names

| Name | Does | Resolvable when |
|---|---|---|
| `spin` | send one bet | idle. ⚠ **an insufficient balance does not block it** — the out-of-money popup appears on click |
| `stop` | cut the current spin's animation short | while a spin is presenting |
| `autoOrFreeSpin` | the control that **replaces** spin during auto / free spins | during a run |
| `auto` | open the autoplay panel | idle |
| `stopAuto` | stop a running auto **from the main screen** | while a run is going |
| `buyBonus` | open the Buy Bonus panel | idle, with packages offered |
| `nextBet` / `previousBet` | **open the denomination list** (both do; Zeus has no inline step) | idle, not during autoplay |
| `turbo` | toggle fast play — resolves whichever of the on/off pair is on screen | any time it is togglable |
| `sound` | toggle audio — same on/off pair behaviour | any time |
| `settings` · `info` · `history` · `trophy` | open the matching panel | idle |
| `devMode` | open the dev panel | where the build has one |

### Popup-scoped names

| Prefix | Names |
|---|---|
| `auto:` | `package:{i}` (alias `count:{i}`) · `confirm` · `cancel` · `stop` · `next` · `prev` · `stopAtWin` |
| `buyBonus:` | `package:{0,1,2}` · `buy` · `confirm` · `cancel` · `close` · `next` · `prev` |
| `bet:` | `row:{i}` · `plus` · `minus` · `close` |
| `settings:` | `turbo` · `music` · `sound` · `turboTab` · `close` |
| `notification:` | `yes` · `no` · `close` |
| `info:` | `close` |
| `history:` | `row:{i}` · `close` |
| `historyDetail:` | `next` · `prev` · `close` |

`bet:row:{i}` and `auto:package:{i}` are indexed **by position** in
`getDenominations()` / the panel's own list — `0` is lowest — never by label text.

### Accepted aliases

| Alias | Resolves to |
|---|---|
| `stakeIncrease` / `stakeDecrease` | `nextBet` / `previousBet` |
| `auto:close` | `auto:cancel` |
| `message:ok` / `message:close` | `notification:yes` / `notification:close` |
| `auto:count:{i}` | `auto:package:{i}` |

**`anteBet` is deliberately not aliased.** Zeus has no Ante control, so the name
must resolve to `null` — your probe for "this build lacks the feature" — rather
than to something that half-works.

> ⚠ **Stopping a run must actually stop it.** Prefer `stopAuto`, then
> `autoOrFreeSpin`, then `auto`. After the click expect `isAutoPlay` to go false
> and no further BET to be sent. An auto-stop that resolves but does not stop
> leaves the client betting, and surfaces as a `waitForSpinClickable` timeout in
> the **next** run's teardown.

> ⚠ **Autoplay pacing.** The gap between two auto spins is the client's
> presentation, not a round trip — it can be tens of seconds behind a long
> celebration. Every round's response still arrives through `awaitBetResponse`,
> in order.

---

## 10. Popup label registry

Used by `getSnapshot().visiblePopups`, `isPopupVisible(label)` and
`dismissPopups()` — one catalogue, so the three can never disagree.

Listed **front-most first**, which is the order `visiblePopups` reports, so
`visiblePopups[0]` is the layer actually blocking a click.

| Label | Popup |
|---|---|
| `bigWin` | big / mega / ultra win celebration |
| `totalWin` | Hold & Spin final total |
| `disconnect` | connection-lost modal |
| `notification` | message / confirmation popup |
| `buyBonus` | Buy Bonus panel |
| `auto` | autoplay panel |
| `bet` | denomination list |
| `settings` | settings |
| `history` | History list |
| `historyDetail` | History detail ticket |
| `info` | help / paytable (WebView) |
| `trophy` | Monumental Wins |
| `luckySpin` · `gameLimit` · `floatPaytable` · `replay` | the remaining registered popups |
| `devMode` | dev panel |
| `promo` | promo (two nodes on a view, not one popup node) |

---

## 11. What the bridge deliberately does NOT do

**Drive the game to a deterministic start.** `startGame()` resolves at the click
and stops there. The bridge's half is answering *is spin clickable?*; the framework
asks until it is.

**Decide when a round is over.** `waitForSpinClickable` reports readiness — you
decide what to do with it. Anything that hid a mid-round state behind "ready"
would break the settle contract and clip animations out of the recorded video.

**Expose round outcomes.** No `getLastPayout`, no `getReelResult`, no
`getPaylines`, no Hold & Spin verifiers. Every one is readable off the raw
response, every game derives them differently, and a bridge that re-derives them
is a second source of truth that can disagree with the very response you assert
against.

**Scroll a cross-origin WebView.** Only a real `page.mouse.wheel` reaches it. The
bridge's job is to say *where*.

---

## 12. Migrating from v2

### Behaviour changes

| | v2 | v3 |
|---|---|---|
| `startGame()` | clicked Start, waited for the scene, drove to a clean idle | **clicks Start and resolves at that click** |
| `getGameInfo()` | live read | **pre-Start captured copy**, unchanged for the session |
| `getSpeedMode()` | `boolean` | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` |
| `dismissPopups()` | `string[]` | `Promise<string[]>` (verified after a frame) |
| `canDo('nextBet')` | `popup_not_open:bet` — inverted (**D2**) | main-screen preconditions |
| `canDo('spin')` | refused on `insufficient_balance` | affordability is not a readiness gate |
| unknown control name | timed out after 30s | `waitForButtonClickable` rejects immediately |

### Removed

| Removed | Do instead |
|---|---|
| `waitForDeterministicStart()` | read `getGameInfo().pending`, drain with `awaitFreespinRound()`, assert with `waitForSpinClickable()` |
| `waitForStableGameState()` | `waitForSpinClickable()` for idle; the response stream tells you about a bonus |
| `waitUntilApplicable()` | `waitForButtonClickable()` / `waitForSpinClickable()` |
| `getAppState()` | nothing — not portable across games, and no case reads it |
| `getCurrency()` · `getPaytable()` · `getFreeSpinPackages()` · `getDefaultStake()` | read them off `getGameInfo()` |
| `getDisplayBalance/Bet/Win()` | grade on-screen text from the video; assert values from the response |
| `skipBigWin()` · `skipTotalWin()` · `skipBonusTransition()` | `dismissPopups()` (it uses the same `skip()` paths) |
| `holdSpinStart()` / `holdSpinRelease()` | a real `mouse.down` → wait → `mouse.up`; no case used the bridge form |
| `startEventCollection()` · `stopEventCollection()` · `drainEvents()` | `startEventCapture(label)` / `endEventCapture(label)` |
| `getHistoryRowCount()` · `getRenderedHistoryRowIndices()` | `getHistoryRows()` |
| `diagnoseHitTest()` | `diagnose()` |

### New

`getHistoryRows()` · `getHistoryDetailState()` · `getMultiplierLevels()` ·
`scrollBetRowIntoView()` · `getTutorialRect()` · `isGameInfoCaptured()` ·
`diagnose()` · `dispose()` · `version` · `window.__gameTestBridgeError`

---

## 13. Checking a build

From the QA framework repo (not this one):

```bash
python scripts/probe_bridge.py --game zeus --env dev
```

Prints the REQUIRED-API check, the snapshot, whether `waitForSpinClickable`
resolves and how fast, the buttons the build actually exposes, and
profile-vs-reality. Reach for it first on any new build or any hang — it turns
"it hangs" into an exact answer in one run.

From the browser console, the same in one line:

```js
copy(JSON.stringify(window.__gameTestBridge.diagnose(), null, 2))
```
