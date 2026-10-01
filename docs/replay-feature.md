# Replay — what the bridge must expose, and how the cases run

**Cases:** CR_072 … CR_080 of `test-case-chaos.csv` (module *Replay*). Nothing here is automated yet;
this document is the design to build against.

---

## 1. The feature, as it behaves

* A round that wins big is recorded as a **replay record**. Only big wins produce one
  (Chaos: `BIG_WIN` starts at **6× the bet** — `ChaosRunner.WIN_TIERS`).
* The player opens the **Monumental Wins** panel from the trophy control (`monumentalWin`).
* The panel lists each record with **time, round id, bet, winnings**, and two buttons per row:
  **Replay** and **Link**.
* **Replay** opens the replay **in a new tab**. **Link** copies that same URL to the clipboard.
* A replay re-plays the round exactly: the balance before the bet, the symbols before and after, the
  win presentation, and the balance after.

---

## 2. Why replay does not fit the existing drive model

**Everything in this framework reads the server's BET response** — `awaitBetResponse` /
`awaitFreespinRound` are how a scenario knows what happened, and `stream:` asserts grade that
response (`docs/scenario-authoring-rules.md`, Rule 10).

**A replay places no bet.** There is no BET response, no balance the wallet moved, nothing in the
stream. So:

| | normal run | replay run |
|---|---|---|
| exact half (`stream:`) | the BET response | **the bridge's replay getters** (§3) |
| visible half (`video:`) | the clip | the clip, unchanged |

Without the getters in §3, every replay case is an AI-only case, which the rules forbid as the whole
of a case: "the bridge reports what the game thinks, not what is on screen" cuts both ways — an AI
reading a balance off pixels is the flaky half (`docs/ai-video-assertion-scoping`). **The getters are
what make these cases assertable at all.**

---

## 3. What the bridge must expose

Existing and usable today: `monumentalWin` (trophy) control, `monumentalWins` + `monumentalWins:close`
popup, the `replay` / `replayLanguage` popup labels, `IS_REPLAY_MODE` (runtime, `?mode=replay`),
`getSnapshot().balance`, `getAllButtonRects()`.

### 3.1 The Monumental Wins panel — `getMonumentalWinRows()`

Mirror `getHistoryRows()`, which already returns `{index, rect, clickable, data}`:

```jsonc
[{ "index": 0,
   "rect": {...}, "clickable": true,
   "data": { "time": "18-09-2026 10:23:46", "roundId": "…", "betAmount": 1.0, "payout": 12.5 },
   "replayRect": {...},      // the Replay button of THIS row
   "linkRect": {...} }]      // the Link (copy) button of THIS row
```

**Why per-row rects rather than `replay:row:<i>` control names:** the panel is a list, and a test has
to act on *the row it just created*, found by `roundId`. Index-addressed controls make that a
two-step guess. (If the build prefers control names, `monumentalWins:replay:<i>` /
`monumentalWins:link:<i>` are fine — then `data` must still carry `roundId`.)

### 3.2 The replay link — `getReplayLink(index)` → `string`

The URL the Replay button opens and the Link button copies.

**Do not make a test read the clipboard to learn it.** Clipboard reads need a browser permission
grant and are flaky headless; worse, a clipboard-only link means a failure cannot distinguish "copy
is broken" from "the link is wrong". With this getter, CR_080 asserts the two halves separately:
the link the game *would* copy (getter) and what actually *landed* in the clipboard (read).

### 3.3 Replay playback state — `getReplayState()`

The one call the balance cases stand on:

```jsonc
{ "active": true,              // this page is a replay (IS_REPLAY_MODE)
  "roundId": "…",
  "phase": "idle|spinning|win-presentation|finished",
  "stake": 1.0,
  "win": 12.5,
  "balanceBefore": 100.0,      // before the bet was taken
  "balanceAfterBet": 99.0,     //   = balanceBefore - stake  (what shows while the reels turn)
  "balanceAfter": 111.5,       //   = balanceAfterBet + win  (the round's final balance)
  "isBigWin": true,
  "isFreeSpin": false,
  "watchAgainAvailable": true }
```

`phase` is what lets a case say *when* a value must be on screen, which is exactly what CR_072/073/074
ask. The three balances are the replay's OWN data — the case then compares the screen against them,
rather than against a number the test computed and hoped was right.

⚠ **CR_072's arithmetic, stated in the CSV, is a consequence of these fields, not a separate rule:**
`balanceBefore == balanceAfter - win + stake`. Assert it on the getter (it is a cheap check that the
replay payload itself is coherent) AND read the screen for the same figure.

### 3.4 Replay controls

| Role | Name | Used by |
|---|---|---|
| watch again | `replay:watchAgain` | CR_079 |
| copy link (inside the replay) | `replay:copyLink` | CR_080 |
| the "Replay" label under Spin | text, not a control — AI reads it; optionally `getReplayLabel()` | CR_078 |

CR_077 needs no new control: `getAllButtonRects()` already reports each control's
`visible` / `interactable`, and the three return meanings the Chaos bridge documents (`null` = not in
this build / popup closed, `visible:false` = present but inactive, `visible:true` = on screen) are
enough to grade "disabled" honestly.

### 3.5 One framework primitive, not a bridge call

**Replay opens a NEW TAB.** The engine has `ctx.open_second_session()` (a second browser context) but
nothing that attaches to a tab the GAME opened. Two options, in preference order:

1. **Skip the tab**: read `getReplayLink()` and open that URL in a second browser context. Same
   URL, fully controlled, no popup handling. This is how CR_072-079 should run.
   ⚠ `ctx.open_second_session()` as it stands **cannot do this**: it takes no argument, always opens
   the run's own game URL, and then calls `start_game` (which resumes an owed bonus and asserts a
   spinnable base — neither is meaningful on a replay page). It needs a small change: accept a
   `url`, and skip the resume/idle assertion when the page is a replay. `startGame()` itself is safe
   — the bridge doc says it resolves immediately in replay mode.
2. **Prove the tab opens** (CR_080's "opens a new tab" half): needs a new helper —
   `ctx.expect_new_page()` wrapping Playwright's `context.expect_page()`. Small, and only this case
   needs it.

---

## 4. The runs

Three runs. Cases that grade **one playback** share one run and one `video:` block with
`expected_by_case` (Rule 16) — re-playing the same clip per case would re-analyse the same footage.

### Run 1 — `replay-base-big-win` → CR_072, 073, 074, 075, 077, 078, 079

**Setup.** A forced paytable that pays ≥ 6× on a base spin (Rule 17: force it, never hunt). The
record only exists if the round was a big win, so the forcing IS the precondition.

| # | Step | Why |
|---|---|---|
| 1 | spin until a round pays ≥ `BIG_WIN`× (forced, so: one spin) | creates the record; keep its `round_id`, `stake`, `payout`, and the balance before/after from the BET response |
| 2 | settle, let the big-win presentation finish | the record is written when the round completes |
| 3 | click `monumentalWin`, wait for the `monumentalWins` popup | the panel |
| 4 | `getMonumentalWinRows()`, find the row whose `data.roundId` == the round played | **anchors the whole run to the round we made**, not to "the top row" |
| 5 | assert the row's `betAmount` / `payout` against the BET response | the panel's own data is correct |
| 6 | `getReplayLink(i)`, then open it in a second context (§3.5 — needs the `open_second_session(url)` change) | the replay page, without touching the new-tab flow |
| 7 | on the replay page: `getReplayState()` before anything moves | `active == True`, `roundId` matches, the three balances are coherent |
| 8 | mark, then watch the playback to `phase == finished`, sampling `getSnapshot().balance` at each phase change | the three checkpoints CR_072/073/074 ask for |
| 9 | while it plays: `getAllButtonRects()` | CR_077's disabled/enabled census |
| 10 | at the end, click `replay:watchAgain`, watch it again to `finished` | CR_079 |

**Exact half** (`stream:`): row data == BET response; the three balances; the button census;
`watch_again_replayed == True` and the second pass's data identical to the first.
**Visible half** (one clip, `expected_by_case`): the balance readout at each of the three moments;
the big-win animation (CR_075); the "Replay" label under Spin (CR_078); nothing reacting to clicks on
the disabled controls (CR_075's "không thể thao tác sai").

⚠ **CR_072 wants a value shown ~0.5 s before the reels turn.** The montage samples one composite per
second, so 0.5 s can fall between two frames. Grade that one on a **high-fps clip** (as
`last-bet-before-rounds` does for its 0.5 s flash), or the case will fail on a correct build.

### Run 2 — `replay-freespin-big-win` → CR_076

Same scenario, different fixture: force a **free-spin trigger** whose bonus total is a big win. The
record is of the whole bonus, so the replay plays every free spin.

Extra asserts on top of Run 1's: `isFreeSpin == True`; the number of free-spin rounds replayed equals
the number the original bonus played; the total win matches the bonus total; the big win appears at
the round the original had it (the AI half — "does the big win happen at the right moment").

### Run 3 — `replay-copy-link` → CR_080

| # | Step |
|---|---|
| 1 | reach a replay (Run 1's steps 1-6) |
| 2 | click `replay:copyLink` (or the panel row's Link button) |
| 3 | read the clipboard (`navigator.clipboard.readText()`, permission granted at context creation) |
| 4 | assert it equals `getReplayLink(i)` |
| 5 | open the clipboard URL in a second session and assert `getReplayState().roundId` is the same round |

This is the one case that justifies `ctx.expect_new_page()` if you also want to prove the Replay
button opens a tab rather than navigating in place.

---

## 5. Case map

| Case | Run | Graded by |
|---|---|---|
| CR_072 balance before the reels turn | 1 | `getReplayState()` + high-fps clip |
| CR_073 balance when the reels stop | 1 | `getReplayState().balanceAfterBet` + clip |
| CR_074 balance when the replay ends | 1 | `getReplayState().balanceAfter` + clip |
| CR_075 big-win replay: animation, amount, no mis-clicks | 1 | button census + clip |
| CR_076 big win inside free spins | 2 | round count + total + clip |
| CR_077 button states in replay | 1 | `getAllButtonRects()` |
| CR_078 the "Replay" label under Spin | 1 | clip |
| CR_079 Watch Again | 1 | `replay:watchAgain` + second pass identical |
| CR_080 Copy Link | 3 | clipboard == `getReplayLink()`, and the link opens the same round |

---

## 6. Open questions — answer these before building

1. **Is a replay link authenticated or time-limited?** If it carries a token that expires, Run 3's
   "open it in another browser" half needs a fresh link each run, and a stale link must report
   *unreached*, not red.
2. **Does the replay page need the same launch flow** (Playsite token) as the game, or is the URL
   self-contained? This decides whether `open_second_session(link)` is enough.
3. **What exactly makes a record?** "Big win" is assumed to be the `BIG_WIN` tier (6× on Chaos).
   If the server decides by an absolute amount, or a different multiple, the forcing fixture changes.
4. **Is the record written on round completion or on presentation end?** Step 2 waits for the
   presentation; if the server writes earlier, the wait can be dropped.
5. **Which control is "Mount Win" in CR_077?** Read as the Monumental Wins (trophy) control; confirm
   against the build before asserting it is disabled.
6. **Does `getSnapshot()` report a balance at all in replay mode?** If the client zeroes it, the three
   balance cases must read the on-screen readout only (AI) with `getReplayState()` as the expectation
   — still assertable, but the exact half moves entirely into the getter.

---

## 7. What to build, in order

1. The bridge getters in §3 (`getMonumentalWinRows`, `getReplayLink`, `getReplayState`,
   `replay:watchAgain`, `replay:copyLink`).
2. Profile: `HAS_REPLAY_MODE` already exists; add the new roles to `ChaosRunner.BUTTONS` and a
   `HAS_MONUMENTAL_WINS` capability so a build without the panel reports *precondition not met*
   rather than red.
3. `ctx.open_second_session(url=None, start=True)` — take a URL, and let a replay page skip the
   resume-and-assert-idle path (§3.5).
4. Scenario `replay-big-win` (parameterised `source: base | freespin`) covering Runs 1 and 2, and
   `replay-copy-link` for Run 3.
5. Suite `games/chaos/test-suites/cr-replay.yml`, and rows in `docs/chaos-coverage.md`.
