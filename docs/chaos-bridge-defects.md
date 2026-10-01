# Chaos Runner — bridge defects blocking automated QA

Found while running the scenario suite against the deployed dev build
(`http://192.168.1.27:7456/web-mobile/sl_35_chaos_runner/`) on **2026-09-02**.
Bridge reference: `games/chaos/docs/raw/test-bridge-api.md`. Reproductions below use only public
bridge calls.

**C1 is the blocker, and it has a one-line fix.** A DOM element covers the game canvas for ~1s
after boot and eats the first click. Root cause and patch in C1 below.

---

## C1 — a DOM element (`#canvasContainer`) covers the game canvas and eats the first click

**Severity: blocker.** The first click after boot is intermittently swallowed (~20–50% of loads).
Every scenario whose first action is a UI click fails, and it presents as "this build has no such
control" rather than as a defect.

### Root cause

At the moment of the swallowed click, the topmost DOM element at the button's own coordinates is
**not the game canvas**:

```
document.elementFromPoint(379, 766)
  swallowed -> DIV#canvasContainer.canvas-container   (z-index: 1000, pointer-events: auto)
  working   -> CANVAS#GameCanvas

elementsFromPoint(379, 766) when swallowed, topmost first:
  ["DIV#canvasContainer.canvas-container",   <-- painted ABOVE its own child
   "CANVAS#GameCanvas",
   "DIV#Cocos3dGameContainer", "DIV#GameDiv", "BODY", "HTML"]
```

The click **is** delivered to the page — all five pointer events fire — but every one of them is
targeted at `DIV#canvasContainer`, so the event never reaches the Cocos input system and no button
sees it:

```
pointerdown -> DIV#canvasContainer.canvas-container
mousedown   -> DIV#canvasContainer.canvas-container
pointerup   -> DIV#canvasContainer.canvas-container
mouseup     -> DIV#canvasContainer.canvas-container
click       -> DIV#canvasContainer.canvas-container
```

~1s later the same point hit-tests to `CANVAS#GameCanvas` and an identical click works. This is a
transient state during the engine's initial resize/layout — `#canvasContainer` carries
`z-index: 1000` and `pointer-events: auto` and briefly paints above the canvas it contains.

**The correlation is exact — 12/12 loads:**

| topmost element at the click point | outcome |
|---|---|
| `CANVAS#GameCanvas` | click lands (10/10) |
| `DIV#canvasContainer` | click swallowed (2/2) |

### Why the bridge cannot currently see this

`waitForButtonClickable` reports the control fully ready at that exact moment:

```
getButtonRect('stakeIncrease') : {..., "visible": true, "interactable": true}
canDo('stakeIncrease')         : {"ok": true}
getSnapshot()                  : {"busyOps": [], "roundState": "READY",
                                  "isReadyToSpin": true, "sceneReady": true}
```

That is correct *as far as it can see*. The bridge's hit-test runs in **Cocos node space** — it
checks whether another Cocos node covers the button — so a **DOM** element sitting over the whole
canvas is invisible to it. Nothing in the reason vocabulary (`blocked_by:<node>`,
`clipped_by_mask`, `no_consumer_at_point`, …) can express "a div is on top of the canvas".

### The fix

**1. Primary — stop `#canvasContainer` intercepting input (game/engine side).**
It is the canvas's own wrapper; it should never be the hit-test target. Either

```css
#canvasContainer { pointer-events: none; }   /* the canvas itself keeps pointer-events: auto */
```

or correct the stacking so `#GameCanvas` is topmost once the game reports ready.

**Verified.** Injecting exactly that one rule before the first click and changing nothing else:

| | swallowed |
|---|---|
| unpatched | **2 / 10** loads |
| `#canvasContainer { pointer-events: none }` | **0 / 10** loads |

With the rule in place `elementFromPoint` returns `CANVAS#GameCanvas` on every load. This one line
removes the defect.

**2. Secondary — make the bridge's readiness signal honest (bridge side).**
`waitForButtonClickable` and `waitForSpinClickable` should also confirm, at the DOM level, that the
returned coordinates actually reach the canvas before resolving:

```js
const el = document.elementFromPoint(rect.centerX, rect.centerY);
if (el !== gameCanvas && !gameCanvas.contains(el)) {
    // keep waiting; reject with a new reason, e.g. `dom_overlay:<id>`
}
```

Worth adding regardless of (1): it converts any future full-canvas DOM overlay from a silent
swallowed click into a named, diagnosable rejection. Suggested addition to the reason vocabulary:
**`dom_overlay:<element id>`**.

### Reproduction

Load the game with `?bridge=1`, `startGame()`, `await waitForSpinClickable()`, then immediately:

```js
const r = await bridge.waitForButtonClickable('stakeIncrease');
document.elementFromPoint(r.centerX, r.centerY);   // DIV#canvasContainer on a bad load
```

Click `r.centerX/centerY` with a real mouse event: on a bad load nothing happens, and a second
identical click ~2s later works. Script: `scripts/probe_bridge.py` for the general picture; the
overlay probe used here is in the session scratchpad and is ~40 lines of `elementFromPoint` logging.

### Impact on the suite

`browse-denominations` fails outright, and the failure text blamed the wrong thing — the run
concluded "no control on this build opens the denomination popup", which is false (`stakeIncrease`
opens it fine, on the second click). Any scenario whose first action is a click is affected.

**Not worked around in the framework.** A blanket pre-click retry or sleep would be paid by all 185
cases and would convert this into invisible latency, so the next genuinely-inert control would look
like the same benign slowness.

---

## C2 — the reference dialect and the native dialect BOTH resolve, but `getAllButtonRects()` lists only the native names

**Severity: documentation / discoverability.** Not blocking; recorded so it is not re-litigated.

The bridge keeps an alias table. `getButtonRect()` resolves the reference spellings
(`stakeIncrease`, `stakeDecrease`, `trophy`, and `bet:close` / `bet:plus` / `bet:minus` /
`bet:row:{i}` while the denomination list is open) as well as the native ones from
`test-bridge-api.md` (`betPlus`, `betMinus`, `monumentalWin`, `denomination:*`). Verified live
2026-09-02: `bet:close` returns a rect and closing by it actually dismisses the list, in 0.6s.

But `getAllButtonRects()` enumerates **only the native names**. Reading it as an inventory of
resolvable names is what led `docs/session-summary-scenario-qa-5.md` §6 to suspect the profile's
reference spellings were wrong. They are not, and they must not be reverted.

**BRIDGE ASK (minor).** Either include the aliases in `getAllButtonRects()`, or state in
`test-bridge-api.md` that it lists canonical names only and that aliases resolve but are not
enumerated.

---

## C3 — popup labels are not the reference labels (framework-side; already fixed)

Not a bridge defect — recorded because of what it cost. `getSnapshot().visiblePopups` reports this
build's own strings, and unlike button names these are **not** aliased:

| role | reference label | this build |
|---|---|---|
| `denominations` | `denominations` | **`denomination`** |
| `autoplay` | `autoPlay` | **`autoplay`** |

`settings` (`setting`), `history`, `historyDetail` and `information` do match.

The `denominations` mismatch produced a **false green**, not a miss. `close_bet_list()` guarded on
the profile's label, compared it against `["denomination"]`, concluded the list was already closed,
and returned `closed / close_button_worked / needed_dismiss = True` **without ever clicking the
close control** — so CR_004's three asserts all passed with the popup still on screen, and the next
`settle()` then burned its full 60s because a visible popup blocks `waitForSpinClickable`. That is
the ~70s stall reported in `docs/session-summary-scenario-qa-5.md` §6, which had been attributed to
the button naming in C2.

Fixed 2026-09-02 in `scripts/game_profile.py` (`ChaosRunner.POPUPS`) and hardened in
`scripts/scenario_engine.py`: `close_bet_list`'s "still open?" check no longer trusts the profile
label — any visible popup counts, so a label typo can never again report success without clicking.
