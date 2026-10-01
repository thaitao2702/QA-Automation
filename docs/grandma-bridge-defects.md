# Grandma Pets — bridge defects blocking automated QA

Found while running the scenario suite against the deployed dev build
(`http://192.168.1.27:7456/web-mobile/sl_45_grandma_pets/index.html`) on **2026-09-24**.
Bridge reference: `games/grandma/bridge-api.md`. Reproductions below use only public bridge calls.

| | Defect | Severity | Status |
|---|---|---|---|
| **G1** | `bigWin:skip` resolves but its centre is outside the canvas, so it can never be clicked | blocker (cost, not correctness) | **fixed** by the bridge team |
| **G2** | bridge-api.md's main-screen table lists `history` and `info`, which are no longer on the main screen — they live inside Settings | doc defect | **open** |
| **G3** | `getHistoryRows()` returns `[]` while the History panel shows rows, so no row can be clicked and History detail is untestable | blocker (TC-011 and every History case) | **open** |

---

## G1 — `bigWin:skip` is permanently `offscreen`, so the big-win celebration cannot be skipped

**Severity: blocker for run time.** Not a correctness defect — no test grades the celebration — but
every BIG/MEGA/ULTRA win plays out in full, on every round of every run, on all seven suites. In a
free-spin bonus several rounds in a row can celebrate.

### What happens

The control **exists** in this build and **resolves**, but `waitForButtonClickable` rejects it as
`offscreen`, which `getButtonRect` (§ *Where the named control is right now*) defines as
**"the control exists but its centre is outside the canvas"**. Because the centre never moves onto
the canvas, the rejection is permanent, not a race — we poll for 3 s with the celebration fully up.

### Verbatim, from a live run

```
big win  : `bigWin:skip` did not become clickable -- reason:
           waitForButtonClickable(bigWin:skip) rejected -> offscreen:bigWin:skip
           canDo('bigWin:skip') -> {'ok': False, 'reason': 'offscreen:bigWin:skip'}
big win  : `bigWin:skip` never became clickable while the celebration was up.
           popups now : ['BigWinPopup']
           busy now   : ['bigWin', 'balanceCountUp']
           CLICKABLE  : ['turbo']
           resolving  : ['spin', 'turbo', 'auto', 'stakeIncrease', 'stakeDecrease',
                         'buyBonus', 'settings', 'bigWin:skip']
```

Snapshot at that moment: `sceneReady: true`, `gameStarted: true`,
`visiblePopups: ["BigWinPopup"]`, `busyOps: ["bigWin", "balanceCountUp"]`,
`isFreeSpinMode: false`, `gameMode: "NORMAL"`, `betAmount: 0.1`, `betIndex: 0`.

The three facts that matter, together:

1. **`bigWin:skip` is in `resolving`** — that list is the keys of `getAllButtonRects()`, i.e. every
   name that resolves right now. So this is not `unknown_control`, and the name is right.
2. **It is not in `CLICKABLE`** — that list is the same dict filtered to `interactable && visible`.
   So the bridge is reporting `visible: false` for it, consistent with `offscreen`.
3. **The moment is correct** — `BigWinPopup` is in `visiblePopups` and `bigWin` is in `busyOps`.
   The celebration is on screen while the control is being asked for.

### This violates the bridge's own build check

§10 *Verifying a build*, step 2:

> Check that `waitForSpinClickable` resolves, **`getAllButtonRects()` centres land inside the
> canvas**, and each popup appears in `visiblePopups` and closes with `dismissPopups()`.

`bigWin:skip` is returned by `getAllButtonRects()` with a centre outside the canvas, so this build
fails that check for that control.

### Our best hypothesis (for you to confirm — we cannot see the scene graph)

`bigWin:skip` looks like a **full-screen tap-to-skip layer** rather than a discrete button, and the
bridge is returning the centre of a node that is not laid out in canvas space (a backdrop sized or
anchored outside the visible area, or parented to an off-canvas container).

The reason we think so: the sibling control has already been special-cased for exactly this.
§ *`getButtonRect`* says —

> Tap-outside controls (`auto:close`, `bet:backdrop`, `gameLimit:close`, **`freeSpinResult:skip`**)
> return a small rect on an uncovered edge point, not the backdrop's centre.

`freeSpinResult:skip` is the same kind of control on the sibling popup and it **is** in that list.
`bigWin:skip` is **not**. If the two are implemented the same way in the scene and only one got the
edge-point treatment, that alone explains the result.

### What we need

1. **The intended geometry of `bigWin:skip`** — is it a discrete button, or a full-screen tap layer?
2. If it is a tap layer: **give it the same treatment as `freeSpinResult:skip`** (a small rect on an
   uncovered on-canvas point), so `waitForButtonClickable` can pass.
3. If it is a discrete button: **why is its centre outside the canvas** during `BigWinPopup`?

### Also worth fixing while you are in here (documentation, same control)

- **`bigWin:skip` appears in no §7 control table.** We only have the name because the game team gave
  it to us in conversation. `BigWinPopup` is listed in *Other `visiblePopups` names* as a
  presentation popup that "ends through its own skip / timeout" — with no control named.
  `FreeSpinResultPopup`, one row up in the §7 table, does list `freeSpinResult:skip`.
- Please add a `BigWinPopup` row to the §7 control table naming its skip control.

### Reproducing it without our suite

```bash
python scripts/probe_big_win.py --game grandma --env dev
```

⚠ It bets. It spins at the current stake until a round pays ≥ the big-win threshold
(6× the bet, read from `gameInfo.settings.winFormula`), then reports the bands, whether the build
knows the control, what surfaced at 50 ms sampling, and whether two taps clear it.

---

## Not a defect: what we checked on our side first

Recorded so this does not get re-litigated.

| Ruled out | How |
|---|---|
| Wrong control name | `bigWin:skip` is in `getAllButtonRects()` keys — the build has it |
| Asking too early / too late | `BigWinPopup` visible and `bigWin` in `busyOps` at the moment of the call |
| A transient animation | `waitForButtonClickable(..., stableMs=300, timeoutMs=3000)` polled for 3 s |
| Asking about `undefined` | the page call passes the name explicitly; `ui_read` now raises on parameterised JS with no `arg` |
| Covered by another layer | that is `blocked_by:<node>`, a different rejection — we got `offscreen` |

---

## G2 — the main-screen button table lists `history` and `info`, which are inside Settings

**Severity: doc defect.** The bridge itself is right; the table that describes it is out of date.

`bridge-api.md` §7 lists `history · info · settings` under **Main screen**. On the current build
there is no History or guide button on the main screen: the player opens **Settings** and taps
**History** / **Info** there. The same doc's popup table already has it right: `SettingPopup` →
`settings:history`, `settings:info`.

What it cost: a profile built from the main-screen table mapped `history` → `history` and
`info` → `info`, and three runs failed on it in one suite (2026-09-29):

| Run | Failure |
|---|---|
| `spin-then-history` (TC-011) | `waitForButtonClickable(history) rejected -> not_available:history` |
| `paytable-bet-scaling` (TC-013/014/055) | unreached: "the info / paytable surface never opened" |
| `open-tutorial` (TC-054/056) | unreached: "info/tutorial button not clickable" |

**Ask:** remove `history` and `info` from the main-screen table (or mark them "inside Settings").

**Our side is already handled:** the Grandma profile maps `history` → `settings:history`,
`info` → `settings:info`, and declares `BUTTON_VIA` so every click opens Settings first
(`docs/game-bridge-contract.md` §8 "Main screen (legacy UI) or Settings menu (new UI)").

---

## G3 — `getHistoryRows()` returns no rows while the History panel shows them

**Severity: blocker for every History case** (TC-011, and any case that opens a round's detail).

### What happens

Opened the way a player does on this build (Settings → History), the panel renders its rows, but
`getHistoryRows()` returns an **empty array** for as long as it is open. It does not throw; it
simply finds nothing. With no rows there is no rect to click, so the round detail can never be
opened and nothing in it can be checked.

### Evidence — live dev build, 2026-09-30, no spins placed

Polled while `HistoryPopup` was open:

```
t~1s  -> getHistoryRows(): length 0   visiblePopups: ["HistoryPopup"]
t~3s  -> length 0
t~6s  -> length 0
t~10s -> length 0
```

Screenshot at t~10s (`docs/grandma-g3-history-rows.png`): **8 rows rendered**, Time / Round ID /
Bet / Winnings with a `>` on each row. The top row is round `948820874994122753`, exactly the round
the test had just played. So the game recorded it correctly; only the bridge cannot see it.

In the suite run (`test-results/grandma/dev/scenario-30-09-2026-095704`): History opened (44.6 s),
the framework polled `getHistoryRows()` for its full 10 s, got `[]`, and closed the panel —
`normal_win_history_rows = 0`, `normal_win_detail_opened = False`.

### Likely cause (for you to confirm)

The History panel is reached through Settings on this build and has a new layout (a table with a
`>` chevron per row). The row enumerator probably still looks for the old panel's node names or
path, so it matches nothing in the new one.

### What we need

1. `getHistoryRows()` to return the rendered rows of the History panel as it is opened on this
   build (Settings → History): `{index, rect, clickable, data}` per row, as bridge-api.md §6 says.
2. `rect` must point at what opens the round's detail (the row, or its `>` chevron).
3. Please check `getHistoryDetailState()` / `historyDetail:next|prev|close` against the same new
   layout, since they are the next step after a row is clicked.

### Reproduce

Open the game, tap Settings, tap History, wait for the rows to appear, then in the console:
`window.__gameTestBridge.getHistoryRows()` → `[]`.
