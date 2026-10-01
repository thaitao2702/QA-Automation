# Zeus — bridge defects blocking automated QA

Found while running the scenario suite against the deployed dev build
(`http://192.168.1.27:7456/web-mobile/sl_33_zappin_zeus/`) on **2026-08-26/27**.
Bridge reference: `games/zeus/bridge-api.md` (v3). These defects were found against v2, which v3
replaced and which has since been removed; all four are fixed in v3. Reproductions below use only
public bridge calls.

> ## ⚠ ALL FOUR ARE FIXED IN BRIDGE v3 (`games/zeus/bridge-api.md`, read 2026-09-07)
>
> | | was | v3 |
> |---|---|---|
> | **D1** `startGame()` never resolves; popups flash shut | `waitForDeterministicStart()` awaited internally, predicate needed `autoSpinsRemaining === 0` while a fresh session reported `-1` | **removed.** `startGame()` clicks Start and resolves at that click; §11 says driving to a clean idle is the framework's job. `autoSpinsRemaining` is normalised so `-1` is reported as `0` |
> | **D2** `canDo('nextBet')` inverted | `popup_not_open:bet` for the control that OPENS that popup | main-screen preconditions (§12) |
> | **D3** no default bet level | `getDefaultStake()` undefined, `settings` carried only `compliance` | `settings.defaultBetLevel` is in `getGameInfo()` (§3) — **but read the first-session caveat below before writing the case** |
> | **D4** History rows not enumerable | no enumerator | `getHistoryRows()` + `getHistoryDetailState()` (§6) |
>
> The sections below are kept as the RECORD — the reproductions and the reasoning are what made the
> fixes precise, and D3's caveat and D4's framework fallout are still live guidance. Nothing here is
> an open ask.

**D1 was the blocker.** One value, one line to fix, and it made roughly half the test suite
impossible to run.

---

## D1 — `autoSpinsRemaining` is `-1` on a fresh session, so `startGame()` never resolves — ~~open~~ **FIXED (v3)**

**Severity: blocker.** Hangs `startGame()`, and makes **every popup close itself ~0.4s after opening**.

### What happens

`startGame()` internally awaits `waitForDeterministicStart()`. That routine never succeeds. Its own
rejection message lists every condition it checks:

```
[ZeusTestBridge] waitForDeterministicStart timed out after 5000ms.
Could not reach a clean base-game IDLE (appState=idle, freeSpinsRemaining=0,
autoSpinsRemaining=-1, isOnBonusGame=false, hasActivePromo=false, promo=false,
busyOps=[], visiblePopups=[]).
```

Every condition is already clean **except `autoSpinsRemaining = -1`**. On a fresh session that value
is `-1` while `isAutoPlay` is `false`; it is stable over time, and `getAutoSpinsRemaining()` returns
`-1` too. The predicate appears to require `0`, so it can never be satisfied.

Two consequences:

1. **`startGame()`'s promise never settles.** A plain `await page.evaluate(() => bridge.startGame())`
   hangs forever with no output. The game itself is fine throughout — `appState: "idle"`,
   `sceneReady: true`, `canDo('spin').ok === true`, `waitForSpinClickable` resolving in **0.3s**.
2. **Every popup flashes shut.** The deterministic-start loop keeps driving toward a clean idle,
   which includes dismissing popups — so anything opened is closed again ~0.4s later:

```
bet       opened +0.13s -> CLOSED ITSELF +0.42s
settings  opened +0.09s -> CLOSED ITSELF +0.44s
history   opened +0.10s -> CLOSED ITSELF +0.42s
auto      opened +0.10s -> CLOSED ITSELF +0.46s
```

### Proof it is this value

Run one autoplay and stop it — `autoSpinsRemaining` goes `-1 → 0`, and everything works:

```
after stop : isAutoPlay=False autoSpins=0
waitForDeterministicStart -> RESOLVED

bet       opened +0.14s and STAYED OPEN 4s
settings  opened +0.13s and STAYED OPEN 4s
history   opened +0.14s and STAYED OPEN 4s
```

So `-1` is an uninitialised sentinel that only becomes `0` after an autoplay run has happened.

### Repro

```js
// fresh session, autoplay never used
bridge.getSnapshot().autoSpinsRemaining   // -1   <-- expected 0
bridge.getAutoSpinsRemaining()            // -1
await bridge.waitForDeterministicStart(5000, 400)   // rejects, naming autoSpinsRemaining=-1
// now run and stop one autoplay, then:
bridge.getSnapshot().autoSpinsRemaining   // 0
await bridge.waitForDeterministicStart(5000, 400)   // RESOLVES
```

### Suggested fix

**Preferred — `startGame()` should not be doing this at all.** Under the agreed split (see
`docs/game-bridge-contract.md` §5), `startGame()` **resolves at the moment Start is
clicked**. Resuming an owed bonus and reaching a clean idle are the QA framework's job: it reads the
bridge's captured `gameInfo`, plays any pending free spins out from the BET responses, and then asks
the bridge *is spin clickable?* until it is. Dropping `waitForDeterministicStart` from `startGame`
removes this whole failure mode — a predicate the bridge cannot satisfy can no longer swallow the
promise, and the framework can always report which condition never cleared.

**Minimal, if the current shape is kept:** initialise `autoSpinsRemaining` to **`0`** when no
autoplay is running, or make the deterministic-start predicate accept **`<= 0`**. The second is
safer if `-1` is deliberate elsewhere (e.g. a "no run" sentinel distinct from "run finished").
Either one unhangs `startGame()`, but neither stops the next unsatisfiable condition from doing the
same thing — which is why the first fix is the real one.

The framework already tolerates a lost promise: it races `startGame()` against a poll and continues
once the scene is ready and either spin is clickable or `isFreeSpinMode` is set, printing the defect
each time. That is a stopgap, not a substitute for the fix.

### What it unblocks

The flashing popups, the hung `startGame()`, and every popup-based case: bet amount (ZZ-02, ZZ-03),
tutorial (ZZ-52), paytable (ZZ-11), History (ZZ-09), settings/sound (ZZ-24, ZZ-25), autoplay
(ZZ-13…ZZ-21), Buy Bonus (ZZ-39…ZZ-43).

---

## D2 — `canDo('nextBet')` is inverted — ~~open~~ **FIXED (v3)**

**Severity: minor, but it misleads.**

`nextBet` and `previousBet` are main-screen controls that **open** the bet-amount popup (confirmed:
clicking `nextBet` yields `visiblePopups: ['bet']` with all 20 `bet:row:<i>` plus `bet:plus`,
`bet:minus`, `bet:close`). But applicability reports the opposite:

```js
bridge.canDo('nextBet')                  // { ok: false, reason: 'popup_not_open:bet' }
await bridge.waitUntilApplicable('nextBet', undefined, 3000)
// rejects: "Last blocking reason: 'popup_not_open:bet'"
```

A control that opens a popup is being gated on that popup already being open. Any test that gates on
`canDo` before clicking will wrongly conclude the bet cannot be changed — which is exactly what our
first investigation concluded, incorrectly.

**Fix:** treat `nextBet` / `previousBet` as main-screen actions; the popup-scoped gate belongs on
`bet:plus` / `bet:minus` / `bet:row:<i>`.

---

## D3 — no default bet level is exposed — ~~open~~ **FIXED (v3), with a caveat that matters more than the fix**

`settings.defaultBetLevel` is now in `getGameInfo()`. But **do not assert
`betIndex == defaultBetLevel`** — both `docs/game-bridge-contract.md` §5 and `bridge-api.md` §3 say
it governs only a player's **first** session; every later session opens at `lastBet.baseStake`. The
obvious assert therefore fails a correct game on nearly every run.

The rule is:

    expected_open_bet = lastBet ? lastBet.baseStake : denominations[defaultBetLevel]

`boot-base` applies it and publishes `expected_open_bet` / `opens_at_expected_bet` / `first_session`
/ `last_bet_stake`, so a failure says which branch it took. ZZ-04 binds those. The older
`opens_at_default` fact still does the naive comparison and is deliberately left alone — no suite
asserts it, and the contract's changelog records the decision not to redefine a published fact.

**Severity when it was open: low — blocked one case.**

`getDefaultStake()` is `undefined`, and `getGameInfo().settings` contains only `compliance`:

```json
{ "compliance": { "autoPlay": true, "minSpinDurationSec": null, "turboMode": true,
                  "oneClickPerSpin": false, "realityCheckEnabled": false,
                  "realityCheckIntervalMin": null, "maxSessionTimeMin": null,
                  "bonusBuyAllowed": true } }
```

Nothing reports a configured default bet level, so "the game opens at the default bet" (ZZ-04) has
nothing to compare against. Observed: opens at `betAmount 3`, `betIndex 2`, of 20 denominations.

**Fix (if you want ZZ-04 automated):** expose `getDefaultStake()`, or include `defaultBetLevel` in
`gameInfo.settings`.

---

## D4 — History rows are not enumerable — ~~open~~ **FIXED in the build (confirmed 2026-09-07)**

~~`bridge-api-v2.md` §10 "Not resolvable" lists History rows and date pickers as having no
enumerator.~~ **The build now implements `getHistoryRows()` to `docs/game-bridge-contract.md`**: the
rows come back, the framework can click one, and the detail ticket opens. ZZ-09 asserts
`normal_win_history_rows >= 1` and `normal_win_detail_opened == True` accordingly.

⚠ **`games/zeus/bridge-api-v2.md` and `bridge-api-v21.md` were STALE on this point** (both since
removed — `bridge-api.md`, v3, replaced them) — neither
mentioned `getHistoryRows` or `getHistoryDetailState` at all. Believing the per-game doc over the
contract cost a session here: it is what made "the detail never opens" look plausible when the
detail was on screen. **Check the contract and probe the live bridge before concluding a getter is
missing.**

### The framework bug this exposed (fixed 2026-09-07)

Zeus's profile carries no `history_detail` popup label, and `spin-then-history` decided whether the
ticket had opened by asking `visiblePopups` for exactly that label. `None in [...]` is False forever,
so the scenario concluded the detail had NOT opened while it was plainly on screen — it skipped the
paging, reported `detail_opened: False`, then spent 10s finishing the poll and 60s in a
`wait_spin_clickable` budgeted at the SPIN timeout inside a bare `except: pass`. **~70 seconds of
apparent hang, with nothing changing on screen and no error naming any of it.**

`visiblePopups` is a DIALECT, and a dialect that disagrees is indistinguishable from a feature that
is missing. The open test is now `DriveContext.wait_for_history_detail`, which asks
`getHistoryDetailState().open` FIRST — the field the contract defines for exactly this — and falls
back to the popup label, then to "any popup that was not up before the row was clicked".

**Still worth asking for:** confirm whether Zeus publishes a `historyDetail` popup label and
implements `getHistoryDetailState()`. The framework no longer needs either, but signal 3 is a
heuristic, and the contract asks for both.

---

## Not a defect — worth knowing

- **`getAllButtonRects()` lists `nextBet` / `previousBet` with `visible: true, interactable: true`
  while the bet popup is closed.** That is fine, but combined with D2 it is confusing: geometry says
  clickable, applicability says blocked, and the truth is "clickable, and it opens the popup".
- **Response shape is confirmed good.** `coinPositions` / `coinCount` / `freeSpinInfo` carry the whole
  Hold & Spin rule set, so those cases grade exactly with no AI. See `games/zeus/bet-response.md`.
  One quirk documented there: `freeSpinInfo` is **`null`** on the round that ends the feature, and
  that round reports `gameState: 1` with a full grid of 9 coins.
