# Game bridge contract

What a game's QA bridge (`window.__gameTestBridge`) must expose for the scenario suite to drive it,
and what each function buys in real test cases.

Derived by walking `scripts/scenario_engine.py` and every suite under `games/*/test-suites/`, not
from memory: **43 scenarios · 185 cases · 3 games** (Mystic · Zeus · Chaos). The case counts below
are what actually stops working when a function is missing.

- Function surface, boot sequence, data contracts, button/popup roles, cascade + Hold & Spin
  response shapes, where system buttons live (main screen or Settings menu), onboarding a new game
  (§12) → **this file**. It is the only bridge contract.
- Per-game response shapes → `games/<game>/bet-response.md`
- Onboarding walkthrough → `docs/session-summary-scenario-qa-3.md`

---

## 1. The division of labour

The single rule everything else follows: **the bridge reports, the framework decides.**

| | |
|---|---|
| **bridge** | captures `gameInfo` at load · `startGame()` resolves when Start is **clicked** · reports *is spin clickable?* · surfaces raw server responses |
| **framework** | reads that `gameInfo` · decides whether a resume is owed · plays it out from the API responses · asserts the game reached a clean idle |

The bridge must not own "drive the game to a deterministic start". It cannot see what a run is about
to assert, and if one of its internal conditions is unsatisfiable the promise simply never settles —
indistinguishable, from outside, from a dead server. That is `docs/zeus-bridge-defects.md` **D1**.

---

## 2. REQUIRED — the suite does not run without these

Checked at load by `profile.api_check_js()` (`SlotGame.REQUIRED_API`). A missing one aborts the run
naming exactly what is absent, rather than timing out somewhere later with no explanation.

| Function | Returns | Cases lost if missing |
|---|---|---|
| [`startGame()`](#startgame) | `Promise<void>` | **all 185** |
| [`getGameInfo()`](#getgameinfo) | `GameInfo` | **all 185** |
| [`getSnapshot()`](#getsnapshot) | `GameSnapshot` | **all 185** |
| [`getButtonRect(name)`](#getbuttonrect) | `ButtonRect \| null` | **all 185** |
| [`awaitFreespinRound(timeoutMs?)`](#awaitfreespinround) | `Promise<BetResult>` | **all 185** |
| [`waitForSpinClickable(stableMs?, timeoutMs?)`](#waitforspinclickable) | `Promise<ButtonRect>` | 131 |
| [`awaitBetResponse(timeoutMs?)`](#awaitbetresponse) | `Promise<BetResult>` | 122 |
| [`waitForButtonClickable(name, stableMs?, timeoutMs?)`](#waitforbuttonclickable) | `Promise<ButtonRect>` | 90 |

The first five read "all 185" because they sit on the boot path: every run pays for them before its
scenario starts.

**`ButtonRect`** → `{ x, y, width, height, centerX, centerY, visible, interactable }`, in CSS pixels
relative to the viewport. The bridge returns **coordinates, not click methods** — the framework
issues a real `page.mouse.click(centerX, centerY)`, so the true input pipeline is exercised.

---

<a id="startgame"></a>
#### `startGame()` → `Promise<void>`

**Does.** Clicks the game's Start button and resolves as soon as that click has been issued.

**Framework uses it to** enter the game — once per page load, and again after every reload
(`reload_and_start`, a language switch, the second multi-tab session).

**Bridge must**
- resolve **at the moment Start is clicked** — not when the game is idle, not when a bonus finished;
- **not** wait for any deterministic or clean state internally;
- **not** play out a resumed bonus, and **not** run a loop that dismisses popups;
- **reject** if the Start control never becomes available, rather than hanging;
- resolve immediately if the game has already started, so a repeat call is harmless.

**Framework will** await it with a timeout and, on timeout, fail the run with a snapshot attached.
It does not poll for a second opinion — see §3.

> This is the one function whose semantics changed. A bridge that drives to a clean idle in here
> cannot fail safely: Zeus's `waitForDeterministicStart` requires `autoSpinsRemaining === 0`, a fresh
> session reports `-1`, and the promise never settles (D1).

---

<a id="getgameinfo"></a>
#### `getGameInfo()` → `GameInfo`

**Does.** Returns the launch payload the bridge captured when the game loaded (§5).

**Framework uses it to** answer two questions: *does this session owe a bonus?* (`pending`) and
*what is configured?* (`settings.denominations`, `defaultBetLevel`, currency, limits).

**Bridge must**
- **capture the payload at load, before Start is clicked**;
- return **that captured copy, unchanged**, for the life of the session — never re-query the server,
  never mutate it as the game plays;
- include at minimum `pending`, `lastBet`, `settings` (with `denominations[]` and `defaultBetLevel`),
  `currency`, `balance`;
- be safe to call at any time, and never throw.

**Framework will** call it once, immediately after `startGame()` resolves, and treat `pending` as the
sole resume signal. Either the raw `{status, code, result:{…}}` envelope or the inner object is
accepted — it unwraps whichever it gets.

> If the bridge re-queries the server instead of returning its captured copy, `pending` will already
> have been cleared by the resume, the framework will never learn that a bonus is playing, and those
> rounds land in the first scenario's stream.

---

<a id="getsnapshot"></a>
#### `getSnapshot()` → `GameSnapshot`

**Does.** Returns the current surface state of the client — popups, autoplay, bet, balance (§4).

**Framework uses it to** assert popup-related cases, record autoplay facts, read the current bet and
balance, and explain failures.

**Bridge must**
- be **synchronous and cheap** — it is called in loops;
- be safe to call at **any** time, including mid-spin, mid-animation and mid-bonus, and **never throw**;
- list `visiblePopups` **front-most first**, and `[]` rather than `null` when none are open;
- carry no round outcomes — those come from the BET response, so the bridge never has to be right
  about game logic.

**Framework will** treat some fields as assertable, some as UI display values, and some as
diagnostics only. That split is in §4, and it matters.

---

<a id="getbuttonrect"></a>
#### `getButtonRect(name)` → `ButtonRect | null`

**Does.** Returns where a named control is on screen **right now**, without waiting.

**Framework uses it to** (a) aim a real mouse click, and (b) **probe whether a control exists in this
build at all** — that second use is why the `null` behaviour matters as much as the rect.

**Bridge must**
- return `null` when no control of that name exists in this build, and **never throw** — a throw
  turns "this build has no Ante button" into a crashed run;
- give `centerX` / `centerY` in CSS pixels relative to the viewport, so `page.mouse.click` lands;
- report `visible` and `interactable` honestly;
- answer **immediately** — no waiting, no retry loop.

**Framework will** call it for optional features it is not sure exist, and read `null` as "this build
does not have it" — the scenario then reports *unreached*, never a failure.

---

<a id="waitforbuttonclickable"></a>
#### `waitForButtonClickable(name, stableMs?, timeoutMs?)` → `Promise<ButtonRect>`

**Does.** Waits until a named control can genuinely be clicked, then returns its rect.

**Framework uses it to** drive every UI interaction: open a popup, pick a denomination, toggle a
setting, confirm autoplay.

**Bridge must**
- resolve **only when a click would actually register** — the control is visible, hit-testable, not
  covered by another layer, and has been stable for `stableMs`;
- **reject on timeout**, with a reason where one is available;
- **reject** — not hang, not resolve — for a control that does not exist in this build.

**Framework will** click the returned `centerX`/`centerY` with a real mouse event, then dwell so the
resulting UI change lands on screen for the recorded video.

---

<a id="waitforspinclickable"></a>
#### `waitForSpinClickable(stableMs?, timeoutMs?)` → `Promise<ButtonRect>`

**Does.** Waits until the spin button is genuinely clickable — that is, the UI is ready to accept a
click on it.

**Framework uses it for three things**, which is why its contract is the strictest here:
1. **before every spin** — so the click is not swallowed;
2. as the **settle signal** after a round, so the recorded video covers the whole animation;
3. as the **boot idle assertion** (§3 step 4) — the single authority for "the game is back to a
   clean, spinnable base".

**Bridge must** resolve only when **all** of these hold, and must not resolve early:
- no animation is running — including the win count-up and any big-win / total-win celebration;
- no popup is blocking the button;
- the button is visible and hit-testable;
- the client is **not** in autoplay, and **not** in free spins or any bonus.

It must **reject on timeout** rather than resolve optimistically.

⚠ **Readiness is about the UI, not the wallet.** An insufficient balance must **not** hold this back:
the spin button stays clickable, and the client shows its out-of-money popup *on click*. That popup
is a game rule the tests assert (Mah_009 / CR_009 / ZZ-07), not a readiness condition — a bridge that
withheld readiness on affordability would make those cases untestable, because the boot assertion
would be demanding the very thing they exist to disprove.

**Framework will** treat this as the sole authority for readiness. Nothing cross-checks it — no
`busyOps` poll, no `visiblePopups` poll. If it resolves while a celebration is still playing, the
video clips the animation and the AI asserts grade the wrong frames, and nothing else in the system
will catch that.

---

<a id="awaitbetresponse"></a>
#### `awaitBetResponse(timeoutMs?)` → `Promise<BetResult>`

**Does.** Resolves with the **next** BET response from the server, raw and unmodified (§6).

**Framework uses it to** capture the result of a spin it has just clicked, and to observe rounds the
client fires itself during autoplay.

**Bridge must**
- return the server payload **unmodified** — no reshaping, no derived fields, no rounding;
- **not click anything** — it only listens;
- **not miss a response that arrives between the click and this call.** The framework clicks first
  and calls this immediately after, so the bridge has to be listening from the moment it is
  installed and hand over a response that has already landed, rather than waiting for the *next* one
  after the call;
- **reject on timeout**.

**Framework will** parse the payload through the game's profile. It never reads client state for
round outcomes.

---

<a id="awaitfreespinround"></a>
#### `awaitFreespinRound(timeoutMs?)` → `Promise<BetResult>`

**Does.** Resolves with the next auto-played free-spin round's response.

**Framework uses it to** play a bonus out without clicking — both mid-scenario (a bonus it triggered)
and at boot (a bonus this session resumed, §3 step 3).

**Bridge must**
- be **passive** — the framework never clicks during a bonus, and neither may this;
- resolve **once per round, in order**;
- carry the same no-missed-response guarantee as `awaitBetResponse`. At boot this matters most: the
  rounds start arriving the instant Start is clicked, before the framework has even read `gameInfo`;
- return the raw payload;
- **reject on timeout**.

**Framework will** loop on it until a round is terminal — `freeSpins == 0 OR gameState == base`,
both, because `hasReachedMaxWin` can truncate either way.

---

### Strongly recommended

<a id="cando"></a>
#### `canDo(action)` → `{ ok: boolean, reason?: string }`

**Does.** Reports whether an action is currently possible, and if not, why.

**Framework uses it as a diagnostic only.** Readiness is always decided by `waitForSpinClickable` /
`waitForButtonClickable`, never by `canDo`. Its value is that when one of those times out, `reason`
turns "it never became clickable" into `popup_not_open:bet`.

**Bridge must**
- return a **stable, machine-readable** `reason` string;
- not gate a control that **opens** a popup on that popup already being open — Zeus reports
  `canDo('nextBet') → {ok:false, reason:'popup_not_open:bet'}` for a button whose whole job is to
  open the bet popup (D2).

---

## 3. Boot sequence

Every entry point — first load, `reload_and_start`, the second multi-tab session — runs this, in
`scenario_engine.start_game()`:

```
1. startGame()                  -> resolves at the Start CLICK; nothing else is expected of it
2. getGameInfo()                -> the bridge's CAPTURED copy; the click has not invalidated it
3. what is pending waiting on?  (profile.pending_resume, server API doc §06 reload order)
     freespins   an unfinished bonus the client auto-plays:
       awaitFreespinRound() ... -> play it out from the API side until a round is TERMINAL:
                                   freeSpins == 0 OR gameState == base
                                   (both -- hasReachedMaxWin can truncate either way)
     lucky_spin  pending.gameState == 3: keep the wheel's multiplier, then drain as above
     extension   pending.canBuyExtension: decline the offer; nothing to drain
     promo       pending.gameState == 4: CLICK its spins until a round leaves state 4
4. waitForSpinClickable()       -> the game is back to a clean, spinnable base
```

Only `freespins` may be drained. The other three wait on the PLAYER, so `awaitFreespinRound` would
wait for rounds that never come and fail the boot of a healthy session. A running promo is played
out by clicking spin (test accounts only — it spends the player's promo).

**Promo dialogs** (games whose profile names them, e.g. Grandma `Promo:receive` / `Promo:list` /
`Promo:message`): the framework **never claims or discards a promo**. `waitForSpinClickable` is
waited in slices, and between slices `clearPromoPopups` closes any promo dialog — `cancel` on the
claim dialog, `confirm` only on the end-of-promo result (no `cancel` shown), the list's close, the
message's OK. Because every boot, settle and spin goes through that wait, a dialog at boot or on the
return to base after free spins is handled in one place. Popup readers (`ctx.popups()`, the
extension-offer and big-win waits) leave promo dialogs out.

**Every step has exactly one authority.** Nothing is cross-checked against a second signal. Two
judgements of the same fact can disagree, and whichever one is allowed to break the tie quietly
becomes the real contract — which is how a broken bridge keeps reporting green. So:

| Step | Sole authority | If it does not answer |
|---|---|---|
| the game started | `startGame()` | **fail the run.** No fallback poll, no "it looks started anyway" |
| a bonus is owed | `getGameInfo().pending` | treated as clean — so a bridge that cannot report it must say so at boot, which is why it is REQUIRED |
| each resumed round | `awaitFreespinRound()` | **fail the run** |
| back to a clean idle | `waitForSpinClickable()` | **fail the run** |

Step 4 in particular re-checks nothing: `waitForSpinClickable`'s own contract already means
no animation running, no popup blocking, hit-testable, and never during autoplay or free spins.
Asserting `busyOps` / `visiblePopups` / `isFreeSpinMode` alongside it would be a second opinion on
one question. Those fields stay in the snapshot for **diagnostics** — they explain a refusal, they
never decide one.

**Why step 3 exists.** `pending.freeSpins > 0` means the player left mid-bonus and the client
auto-plays those spins the moment Start is clicked — no spin click from the suite, no scenario asking
for them. Unhandled, they arrive as the first responses of whatever scenario runs first: its
`base_spin()` returns a free-spin round, `conservation` fails on a stake it never placed, and the
balance-debit case grades a spin the player did not pay for.

**What if `pending` under-reports?** Step 4 still protects the run. `waitForSpinClickable` may not
resolve during free spins, so a bonus the framework did not know about would block the boot until it
finished rather than leaking into the first scenario. The rounds would go unrecorded — a lost
measurement, not a wrong verdict. That is the safety margin the single-authority design buys, and it
is why step 4 is an assertion rather than a poll that could be satisfied early.

### Every run, not just the first

The four steps above run at **page load**. A suite reuses one page for all its runs, so a clean base
has to be established for each one as well — before and after:

```
PREPARE (before every run)   CLEANUP (after every run)
  return-to-base               return-to-base
  waitForSpinClickable         waitForSpinClickable
  -> else reload + startGame   -> else reload + startGame
```

`return-to-base` **never calls `awaitFreespinRound` blind**. It inspects the last BET response first
and drains only when that response says a bonus is still open — waiting on a free-spin round that is
not coming is the exact failure that used to abort suites. Between runs there is no live `pending` to
consult (it is the load-time capture), so the previous run's last response *is* the signal, and the
runner carries it forward across the fresh per-run context.

The reload is the fallback, not the mechanism: it is the only guaranteed reset, and it costs a page
load. It is what recovers the case where a bonus really did play out unobserved — the rounds are
gone, so nothing can drain them, and only a reload gets back to a known state.

This makes a clean base the framework's invariant rather than something each suite author has to
remember with `teardown: return-to-base`. Declared teardowns are now for extras only (topping the
wallet back up, for instance).

Resumed rounds are real server results, so `start_game()` hands them to the caller through an
`on_freespin` callback rather than swallowing them — `freespin-zero-balance` (Mah_011) asserts
`stake == 0` across exactly those rounds.

---

## 4. `getSnapshot()` — the fields the suite reads

Thin: surface state and applicability inputs. **No round outcomes** — those come from the BET
response, so the bridge never has to be right about game logic.

| Field | Type | Used for | Role |
|---|---|---|---|
| `visiblePopups` | `string[]`, front-most first | every popup case; `open_bet_list` probes it to prove a list really opened | **asserted** |
| ⚠ …**including notifications / toasts** | | a transient banner ("Turbo Spin Enabled") is a popup for this purpose — see below | **asserted** |
| `isAutoPlay` | `boolean` | autoplay cases | **asserted** |
| `balance` · `betAmount` · `betIndex` | `number` | balance / bet cases (the dedicated getters are preferred where present) | **asserted** |
| `denominations` | `number[]` | denomination cases | **asserted** |
| `autoSpinsRemaining` · `freeSpinsRemaining` | `number` | **the value currently shown in the UI** — so they answer "does the counter on screen say what it should?" (ZZ-20) and nothing else | **UI display value** |
| `sceneReady` | `boolean` | named in failure messages | diagnostic |
| `busyOps` | `string[]` | explains why spin is not yet clickable | diagnostic |
| `isFreeSpinMode` | `boolean` | explains a refusal during a bonus | diagnostic |

**`autoSpinsRemaining` / `freeSpinsRemaining` are UI values, not game state.** They report what the
player can see, which makes them the right subject for a display case and the wrong input for any
decision. Game state comes from the BET response (§6) and the resume state from `pending` (§5).
Zeus's `autoSpinsRemaining: -1` on a fresh session is a case in point: as a display value "nothing
to show" is defensible, and gating a deterministic start on it was the mistake (D1).

**No `appState`.** It is implemented differently game to game, so nothing portable can be built on
it and the suite does not read it.

**`visiblePopups` must include transient notifications.** Probed on Mystic, 2026-09-02: toggling
Turbo raises a visible "Turbo Spin Enabled" banner while `visiblePopups` stays `[]` for the whole
8 seconds it is on screen. The cost is not a missing measurement — it is a **silently passing test**:
`popups_cleared_after_idle` is computed as "nothing left in `visiblePopups`", which is then true on
every build, including one where a notification really is stuck (Mah_108). A control-state field that
never reports the thing under test turns its assert into a no-op.

If a build genuinely distinguishes modal popups from toasts, expose the toasts under their own key
and say so — the suite can read either. What it cannot do is tell "nothing is showing" apart from
"this bridge does not report what is showing".

**Diagnostic** means the field appears in error output but never decides pass/fail — §3. That is not
permission to report it loosely: a wrong `busyOps` sends someone chasing the wrong bug. It means the
*authority* for readiness is `waitForSpinClickable`, so these are how the bridge explains itself when
that call refuses.

Game-specific extras the profiles read where present: `buyBonusSelectedPrice`,
`buyBonusPopupLoaded`, `freeSpinPackageCount`.

**Paid-feature state — the fields a feature case asserts** (§8 Features). A build that ships Ante Bet
or Premium Reel reports both *whether the session offers it* and *whether it is on right now*, because
those two answers send a case to opposite verdicts: not offered → **unreached**, offered but not
engaged → **failure**.

| Field | Type | Role | Meaning |
|---|---|---|---|
| `anteBetAvailable` · `premiumReelAvailable` | `boolean` | **asserted** | this SESSION offers the feature (`settings.anteMultiplier` / `settings.premiumReel` are set) |
| `isAnteBet` · `isPremiumReel` | `boolean` | **asserted** | it is on for the next spin |
| `anteMultiplier` · `premiumReelMultiplier` | `number` | **asserted** | the configured multiplier, `0` when not engaged |
| `premiumReelId` | `number` | **asserted** | the active package id, `0` when none. Opaque |
| `spinCost` | `number` | UI display value | what the next spin will cost: base stake × the active feature's multiplier. The BET response's `stake` is the authority |

These replace "did a toast appear?" as the way to prove a feature engaged — see §8.

---

## 5. `getGameInfo()` — the state a session resumes into

```jsonc
{ "pending": { "freeSpins": 0, "gameState": 1,      // <-- what this session will auto-play
               "stake": 0.6, "accumulatedWinAmount": 0.6, "roundId": null, "refId": null },
  "lastBet": { /* the last completed round, in BET-response shape */ },
  "settings": { "minBet": 0.2, "maxBet": 100, "defaultBetLevel": 3,
                "denominations": [0.2, 0.4, …], "winFormula": [ … ], "compliance": { … } },
  "multiplierLevel": { "baseGame": [1,2,3,5,8,10], "freeGame": [2,4,6,10,16,20] },
  "paytable": [ … ], "currency": "TKN", "balance": 9919.38 }
```

Live sample: `games/mystic/fixtures/game-info/clean-base.json`.

- **`pending.freeSpins > 0`** (or `pending.gameState != base`) → the framework plays the bonus out at
  boot. Both are checked, because `hasReachedMaxWin` can leave a non-base state with the counter at
  0, or the counter above 0 with the state already back at base.
- **`settings.defaultBetLevel`** is an INDEX into `denominations`. It is what makes "the game opens at
  the default bet" gradeable — `boot-base` exposes `default_bet_level` / `default_bet_amount` /
  `opens_at_default`, gated by `default_bet_known`, so a build that omits it reports *unknown* rather
  than a confident pass on a missing value. Zeus omits it: defect D3.
- **`settings.winFormula`** is the win-band table, and it is what arms the big-win skip (§8). Each
  entry names a tier and the range that reaches it, **as a multiple of the bet, not as money**.
  Verified payload (`games/mystic/fixtures/game-info/clean-base.json`):

  ```jsonc
  [{ "name": "SLIM",  "min": 0,   "max": 1    },   // not celebrated — no banner, nothing to skip
   { "name": "SMALL", "min": 1,   "max": 2.5  },
   { "name": "MEDIUM","min": 2.5, "max": 6    },
   { "name": "BIG",   "min": 6,   "max": 15   },   // <-- lowest celebrated floor = the threshold
   { "name": "MEGA",  "min": 15,  "max": 40   },
   { "name": "ULTRA", "min": 40,  "max": null }]   // open-ended top band: `max` may be null
  ```

  The framework takes the lowest `min` among the **celebrated** tiers (BIG / MEGA / ULTRA / EPIC /
  SUPER / SENSATIONAL) as the threshold — one number, because those three are the same interruption
  with different artwork — then asks each response whether `payout / baseStake` reaches it, so a
  round that cannot celebrate never polls for a banner. ⚠ **`baseStake`, not `stake`:** a free spin
  costs 0, and dividing by that would call every winning free spin a big win — free spins being
  exactly where these celebrations cost the most time.
  Accept it at `settings.winFormula` or `settings.general.winFormula`; the framework reads both.
  ⚠ **Omit it and every celebration plays out in full**, silently: with no bands there is no
  threshold, the skip is never armed, and the only symptom is a slower run. A build that omits it
  says so once per run in the log rather than pretending the skip is working.

### `pending` is the ONLY resume signal — never `lastBet`

Real payload (`games/mystic/fixtures/game-info/maxwin-truncated.json`), a session whose last round
hit the max-win cap:

```jsonc
"pending": { "freeSpins": 0,  "gameState": 1 },        // nothing owed
"lastBet": { "freeSpins": 8,  "totalFreeSpins": 10,    // counter still says 8 left...
             "gameState": 1,  "hasReachedMaxWin": true,
             "rawPayout": 11.20, "payout": 2.00,       // ...because max win truncated the bonus
             "accumulatedWinAmount": 10.00 },          // == settings.maxPayout, exactly
```

The 8 remaining free spins are **forfeited**, and `pending` says so. `lastBet` does not: read
through the same parser it looks like a live bonus (`is_trigger(lastBet) == True`). A framework that
decided the resume from `lastBet` would wait for 8 free-spin rounds that never arrive and fail the
boot of a perfectly healthy session.

So: **`pending` decides, always.** `lastBet` is context — useful for reporting the previous round,
never for control flow.

It also confirms **`settings.maxPayout` is an absolute cap on `accumulatedWinAmount`**, not a
multiplier (`maxMultiplier` is `0` here). That cap is what makes `hasReachedMaxWin` explainable.

### `defaultBetLevel` applies to a FIRST session only

`settings.defaultBetLevel` is an **index into `denominations`** — proved by `clean-base.json`, where
it is `3` and `3` is not itself a valid denomination (`[0.2, 0.4, 0.6, 0.8, …]`), so it cannot be a
value.

But it is only what the game opens at **the first time a player opens it**. On every later session
the game opens at the player's **last bet**. The two fixtures show both halves:

| Fixture | `defaultBetLevel` → | `lastBet.baseStake` | Game should open at |
|---|---|---|---|
| `clean-base.json` | 3 → **0.8** | **0.6** | **0.6** — the last bet, *not* the default |
| `maxwin-truncated.json` | 3 → **4** | **4** | 4 — the two agree here by coincidence |

So the expected opening bet is:

```
expected_open_bet = lastBet is present ? lastBet.baseStake
                                       : denominations[settings.defaultBetLevel]
```

⚠ **An "opens at the default bet" case must not assert `betIndex == defaultBetLevel`.** On any
returning session that is a false failure against a game behaving correctly — `clean-base.json` is
exactly such a session. `maxwin-truncated.json` would have passed the naive check and hidden the bug,
which is why one fixture is not enough to confirm a rule.

> ⚠ **`gameInfo.lastBet` does not spell fields the way the BET response does.** Mystic sends the step
> array as `steps` (BET: `result`) and the awarded count as `totalFreeSpins` (BET: `totalFreeSpin`).
> The profile carries `F_STEPS_ALIASES` / `F_TOTAL_FREE_SPINS_ALIASES` so one parser reads both;
> without them a resumed round reports zero cascades and zero awarded, and every award assert passes
> on nothing. **Check both spellings when onboarding a game** — the profile's own name always wins.

---

## 6. The BET response

Scenarios drive game LOGIC from this and nothing else; client-state getters are used only where the
subject *is* the UI (popups, language, speed mode). The cascade step shape and the Hold & Spin
fields are at the end of this section. A full response (Mystic, a cascade game):

```jsonc
{ "gameState": 1,            // 1 base · 2 free game · 3 buy bonus · 4 promo
  "stake": 4,                // 0 during free spins
  "baseStake": 4,            // the round's original stake, kept through the bonus
  "payout": 81,              // this bet's win = sum of every step's payout
  "balance": 13173.25,       // AUTHORITATIVE post-payout balance — never recompute it
  "freeSpins": 0,            // REMAINING, not awarded
  "totalFreeSpin": 0,        // awarded for the bonus
  "roundId": "917785612927369217",   // SHARED by a trigger and all its free spins
  "id": "917785612927369217",        // this BET — unique per free spin
  "hasReachedMaxWin": false,
  "result": [ /* cascade steps */ ] }
```

The fields the engine reduces every response to:

`gameState` · `stake` · `baseStake` · `payout` · `balance` (authoritative — never recomputed) ·
`freeSpins` (**remaining**, not awarded) · `totalFreeSpin` (awarded) · `roundId` ·
`hasReachedMaxWin` · the step array.

Two rules the whole suite leans on:

1. **Free-spin bets are `stake: 0`**, with `baseStake` keeping the original.
2. **`freeSpins` is REMAINING and is not a loop variable.** A max-win round returns to base with
   `freeSpins > 0` (those spins are forfeited), so a drain terminates on
   `freeSpins == 0 OR gameState == base`.
3. **A field the backend uses to say NOTHING must be recognisable as such.** Dragon VIP's Hold & Spin
   respins send `balance: -1` and only the terminal round carries the real figure; the money is
   correct end to end (14283.04 → -1 → -1 → 14339.04, and 14283.04 + 56.00 = 14339.04). The framework
   now treats a non-positive balance as *not reported* and makes the next reported round account for
   every carried stake and payout (`conservation`, `docs/scenario-authoring-rules.md` Rule 19). What
   a bridge must not do is send a sentinel that could pass for a real value — a balance of `0` on a
   round that did not report one is indistinguishable from a player who is broke. **Prefer omitting
   the field, or documenting the sentinel in `games/<game>/bet-response.md`.**
4. **A refused bet is not a round.** The server answers a refusal with HTTP 200 and
   `{status: 0, code: <negative>, msg}` with no `result`. A bridge should keep these out of the
   round stream (Grandma, Dragon and Joker document that they do); if one gets through anyway,
   `Bet.parse` raises `BetRejected` naming the code instead of grading a stake-0 / payout-0 round.
5. **The max-win cap counts the whole round.** `cap = maxWin`, or where the reply leaves it `0`
   (WildExpanding, Cascading: Joker, Mystic) `min(settings.maxPayout, baseStake ×
   settings.maxMultiplier)`, a `0` in either meaning unset. The spin that crosses it credits only
   what is left: `payout = min(rawPayout, cap − (accumulatedWinAmount − payout))`, and
   `accumulatedWinAmount` includes the current spin. `max_win_cap_applied` checks exactly this on
   every profile.

**Paid features in the response.** Where a build ships Ante Bet or Premium Reel, every round bet with
one echoes it — `anteMultiplier`, or `premiumReelId` + `premiumReelMultiplier` — and `0` means the
round was a plain spin. `stake` is what the player PAID (base × multiplier); `baseStake` is what the
payouts derive from, and it must not move when a feature is switched on. Inside a bonus the echo is
frozen to whatever opened it, so a case can prove the feature was not silently dropped mid-feature.

### Cascade games — one step

```jsonc
{ "views":    [ {"id":1,"value":["B","A", …7 symbols… ]}, …7 columns… ],
  "winnings": [ {"symbol":"D","clusterSize":6,"multiplier":0.25,
                 "positions":[{"x":1,"y":1}, …],"payout":1.00} ],
  "multiplierLevel": 1,
  "payout": 1.00 }              // 0 marks a non-winning step
```

- **The chain ends with a non-winning step.** A step that pays nothing stops the cascade, so a
  winning round has `len(result) - 1` winning steps. `cluster_wins()` counts steps with
  `payout > 0`, and that count drives the history-detail scroll (no win → no scroll).
- **A step may pay several clusters at once** and still counts once — the detail renders one grid
  per step. `win_clusters()` reports the other number for diagnostics.
- **`payout` == the sum of its steps' payouts.**
- **`multiplierLevel` is a LADDER from config**, not `+1` per cascade — Mystic's base game runs
  `1, 2, 3, 5, 8, 10`. Read the expected sequence from `getMultiplierLevels()`.

### Hold & Spin games — the extra fields

A `HoldSpinGame` additionally needs, per round:

| Field | Contract |
|---|---|
| `coinPositions[]` | `{row, col, coinValue, ...}` — the HELD positions. This set may only ever GROW during the feature; a position that disappears is the defect `holdspin_coins_held` catches |
| `coinCount` | how many specials are on the grid (falls back to `len(coinPositions)`) |
| free-spin counter | respins REMAINING. Nest it under `F_FS_CONTAINER` if the backend does (Zeus: `freeSpinInfo.freespins`) |

Feature parameters live on the profile: `TRIGGER_COUNT` (specials needed), `RESPINS_ON_TRIGGER` (the
award, and the value a new special RESETS the counter to), `GRID_SLOTS` (a full grid ends it). With
those, six invariants grade the whole feature exactly — award, coins held, reset on a new symbol,
count-down without one, correct end, and payout matching the symbols collected. **No AI is involved
in any of it**, which is the payoff for a mechanism whose rules are all in the payload.

A game with no `gameState` field derives one in `profile.game_state()` — Zeus reports the state via
its respin counter, so `playType` (which names the product, not the round) is deliberately not used.

---

## 7. OPTIONAL — each one buys specific cases

Missing → the dependent scenario returns **unreached** (case = *inconclusive*), never a false pass.
So none of these can break a run by being absent; each one only unlocks work.

| Function | Returns | Cases | Which |
|---|---|---|---|
| [`dismissPopups()`](#dismisspopups) | `Promise<string[]>` | 43 | teardown for every popup-opening scenario |
| [`getBetAmount()`](#getbetamount) | `number` | 16 | CR_005/009/010/021, Mah_005/009/010_016, ZZ-03/07/08/16/39/40/41 |
| [`getSpeedMode()`](#getspeedmode) | `'NORMAL' \| 'TURBO' \| 'UNKNOWN'` | 13 | CR_026-029/108/110/111, Mah_021-023/105/108, ZZ-22 |
| [`getTutorialRect()`](#gettutorialrect) | `ButtonRect \| null` | 12 | CR_068-071/098/099/107, Mah_057/059/060, ZZ-11/52 |
| [`getBetIndex()`](#getbetindex) | `number` | 8 | CR_004/005, Mah_004/005/020, ZZ-02/03/21 |
| [`startEventCapture(label)` / `endEventCapture(label)`](#eventcapture) | — / `Event[]` | 7 | CR_108/110/111, Mah_105/107/108, ZZ-22 |
| [`scrollBetRowIntoView(i)`](#scrollbetrowintoview) | — | 7 | CR_004/014-017, Mah_004, ZZ-02 |
| [`getHistoryRows()`](#gethistoryrows) | `Row[]` | 6 | CR_012, Mah_012 (×4), ZZ-09 |
| [`isSoundEnabled()` / `isMusicEnabled()`](#soundgetters) | `boolean` | 4 | CR_031/032, ZZ-24/25 |
| [`getBalance()`](#getbalance) | `number` | 3 | CR_009, Mah_009, ZZ-07 |
| [`getDenominations()`](#getdenominations) | `number[]` | 3 | CR_003, Mah_001, ZZ-01 |
| [`getMultiplierLevels()`](#getmultiplierlevels) | `{baseGame[], freeGame[]}` | 2 | CR_057/058 |
| [`getHistoryDetailState()`](#gethistorydetailstate) | detail state | 1 | Mah_012-fs |
| [`setForcedResult` / `setDevModeResult`](#forcedresult) | — | — | deterministic single spins |
| `getPremiumReelOptions()` | `{reelId, multiplier}[]` | 9 | DV-59…65, DV-53/55 — the packages this SESSION offers, with the multiplier each charges. `[]` when the feature is off, which is how a case reports *unreached* instead of failing. The framework never hard-codes a `reelId`: it drives whatever this returns |
| `getBuyFeatures()` · `scrollBuyFeatureIntoView(name)` | rows / — | 8 | DV-39…43, DV-53…55 — the feature panel's rows **as rendered** (`{index, id, kind, control, priceMulti, price, rect, clickable}`), and a way to bring one into view before waiting on it. A row below the fold reports `clipped_by_mask` and never becomes clickable, so without the scroll helper the lower rows of a System-B panel (§8) are unreachable |

---

<a id="dismisspopups"></a>
#### `dismissPopups()` → `Promise<string[]>`

**Does.** Closes every popup currently open and returns the labels of the ones it closed.

**Framework uses it as teardown** between runs, so the next scenario starts on a clean screen instead
of inheriting whatever the last one left open. It is also called mid-scenario before opening a new
surface, so each surface is entered from a known state.

**Bridge must**
- close *all* open popups, not just the front-most;
- return the labels it actually closed (`[]` when there was nothing), using the same names as
  `getSnapshot().visiblePopups`;
- be safe to call when nothing is open;
- ⚠ **leave PRESENTATION popups alone** — see below.

**⚠ Presentation popups are the one exception.** A big-win banner, a free-spin award, an
end-of-bonus result: these are not surfaces the player opened, they are the round being shown. They
must end through **their own skip control** (§8) or their own timeout, and `dismissPopups()` must
**not** force-close them. Two reasons, both load-bearing:

- it runs **mid-scenario**, before opening a surface — sweeping a celebration away there cuts the
  recorded video that the AI asserts grade, and the frames they need are simply gone;
- it runs in **teardown**, where it would hide the popup without running the *bonus-result → back to
  base* handler behind it, leaving the next run's `waitForSpinClickable` to hang on a game that
  never resumed.

Grandma is the reference here: `BigWinPopup`, `GetFreeSpinPopup`, `ExtraFreeSpinPopup` and
`FreeSpinResultPopup` are documented as never force-closed. Zeus v3 went the other way and retired
`skipBigWin()` *into* `dismissPopups()` — §10 row 6 reverses that.

**Framework will** call it without checking first, and ignore the return except for logging. It
skips a celebration through `big_win_skip`, never through this.

---

<a id="getbetamount"></a>
#### `getBetAmount()` → `number`

**Does.** Returns the stake one spin currently costs, as a currency value.

**Framework uses it to** check the bet actually changed after a bet interaction, and to size a
balance drain for the insufficient-funds cases.

**Bridge must** return the amount the player would pay for the next spin — the value, not a ladder
position, and not a formatted string.

**Framework will** prefer this over `getSnapshot().betAmount` where both exist. Pair it with
`getBetIndex()`: one without the other leaves the bet cases half-answerable.

---

<a id="getbetindex"></a>
#### `getBetIndex()` → `number`

**Does.** Returns which rung of the stake ladder is currently selected.

**Framework uses it to** verify a denomination pick landed on the row it clicked, and to know where
it is starting from when stepping the bet.

**Bridge must** return an index into the same `denominations[]` array the bridge reports, where `0`
is the lowest bet — the same index that addresses `bet:row:<i>`.

---

<a id="getdenominations"></a>
#### `getDenominations()` → `number[]`

**Does.** Returns the configured stake ladder.

**Framework uses it to** check the offered denominations match config, and to choose a specific bet
to click by value rather than by guessing an index.

**Bridge must** return the values ascending, where index `i` is the row addressed by `bet:row:<i>`.

**Framework will** accept this or `getGameInfo().settings.denominations` — either satisfies these
cases, and `getGameInfo()` is required anyway, so this getter is pure convenience.

---

<a id="getbalance"></a>
#### `getBalance()` → `number`

**Does.** Returns the player's balance as the client currently shows it.

**Framework uses it to** size a drain down to an insufficient-funds state, and to check the displayed
balance against what the server reported.

**Bridge must** report what is on screen now.

> This is the **displayed** balance, not the authority. The post-payout balance always comes from the
> BET response (§6) — same distinction as the UI counters in §4.

---

<a id="getspeedmode"></a>
#### `getSpeedMode()` → `'NORMAL' | 'TURBO' | 'UNKNOWN'`

**Does.** Returns the client's current spin-speed mode.

**Framework uses it to** make "Turbo really engaged" an exact check instead of asking an AI to judge
spin duration from the video.

**Bridge must** report the model's own state, and return `'UNKNOWN'` when it genuinely cannot tell —
that is a valid answer, and the framework treats it as unanswerable rather than as a failure. A
confident wrong answer is far worse than `'UNKNOWN'`.

---

<a id="soundgetters"></a>
#### `isSoundEnabled()` / `isMusicEnabled()` → `boolean`

**Does.** Return whether sound / music is currently on.

**Framework uses them to** prove a mute toggle actually took effect — otherwise the case depends on
an AI "hearing" a silent video, which it cannot.

**Bridge must** report the two independently, since the cases toggle them separately.

---

<a id="gettutorialrect"></a>
#### `getTutorialRect()` → `ButtonRect | null`

**Does.** Returns where the tutorial / paytable WebView sits on screen, or `null` when it is not open.

**Framework uses it to** know **where to put the mouse**. The tutorial is a `cc.WebView` served
cross-origin, so the browser blocks the bridge — and any injected JS — from scrolling it. Only a real
`page.mouse.wheel` routes into that frame, which is why scrolling it is framework work by design
(§8). From the rect the framework takes `centerX`/`centerY` to aim, and `height` to size each wheel
step to roughly one screenful with a 10% overlap, so no band of the guide falls between two frames.

**Bridge must**
- return the rect of the **WebView itself**, not of the popup that contains it;
- return `null` when the tutorial is not open — the scenario then records 0 wheels instead of
  scrolling something else by accident.

> Scroll **position** is also unreadable cross-origin — there is no way to ask "am I at the bottom?".
> So the framework wheels a fixed, deliberately over-generous number of steps and lets the browser
> clamp at the end. Over-wheeling is harmless: the last screenful just repeats. Proof that the pixels
> moved can only come from the video, which is why these 12 cases are AI-graded.

A build whose help is a native popup rather than a WebView (Zeus) can omit this — the framework falls
back to scrolling the popup directly.

---

<a id="scrollbetrowintoview"></a>
#### `scrollBetRowIntoView(i)` → —

**Does.** Scrolls the denomination list so that row `i` is on screen.

**Framework uses it to** reach denominations that exist but are masked or below the fold in a
scrolling bet list — without it, only the visible rows are clickable and the high/low ends of the
ladder cannot be tested.

**Bridge must** accept the denomination index (`0` = lowest) and bring that row fully into view.

**Framework will** ignore the return value entirely and wrap the call in a try/except, so returning
nothing is fine and a failure is non-fatal. It then dwells so the scrolled-to row lands in frame.

---

<a id="gethistoryrows"></a>
#### `getHistoryRows()` → `{index, rect, clickable, data}[]`

**Does.** Returns the History rows rendered right now.

**Framework uses it to** assert what the History panel lists, and to get the rect of the row it needs
to click to open a detail ticket.

**Bridge must**
- return **only rows actually rendered**. On a virtualized list this is a subset — never synthesise a
  full list, because the framework clicks the rects it is given;
- give `rect` in the same coordinate space as `getButtonRect`;
- mark `clickable` honestly, and put the row's displayed values in `data`.

---

<a id="gethistorydetailstate"></a>
#### `getHistoryDetailState()` → `{open, betIndex, betCount, hasPrev, hasNext}`

**Does.** Returns the state of the open History detail ticket, including whether it can page forward
or back.

**Framework uses it to** walk the free-spin sub-tickets of one round, one bet at a time.

**Bridge must** make `hasNext` / `hasPrev` reflect the popup's **real** navigation, including any
synthetic page (such as a max-win summary). The framework loops on those flags and never does
arithmetic over `betCount`, precisely so a round with an unusual page count still pages correctly.

---

<a id="eventcapture"></a>
#### `startEventCapture(label)` / `endEventCapture(label)` → — / `Event[]`

**Does.** `startEventCapture` begins recording the bridge's own events (`popup_shown`,
`popup_dismissed`, …) under a label; `endEventCapture` stops and returns everything captured under it.

**Framework uses them to** prove something too brief to poll actually happened — a popup that opens
and closes in 300ms would be missed by any sampling loop, but shows up in the capture.

**Bridge must**
- support labelled, non-overlapping capture windows;
- return `[]` rather than throwing when there is nothing, or when capture is unsupported;
- timestamp events so their order is recoverable.

**Framework will** open a window, drive the interaction, close it, and read the events as facts.

---

<a id="getmultiplierlevels"></a>
#### `getMultiplierLevels()` → `{baseGame: number[], freeGame: number[]}`

**Does.** Returns the configured multiplier ladders, e.g.
`{baseGame:[1,2,3,5,8,10], freeGame:[2,4,6,10,16,20]}`.

**Framework uses it to** compare the multipliers a round actually reported against config, instead of
assuming the ladder climbs by `+1` per cascade — which is wrong on every game checked so far.

**Bridge must** return the configured sequences in order, base and free-spin separately.

---

<a id="forcedresult"></a>
#### `setForcedResult(result)` / `setDevModeResult(grid)` → —

**Does.** Forces the outcome of the next single spin, client-side.

**Framework uses it to** reach a specific outcome without spinning until it happens by chance.

**Bridge must** apply the forced result to **the next spin only**, and clear it afterwards.

> **One round only.** A multi-spin sequence — Hold & Spin, Buy Bonus, a whole free-spin run — is
> server-authoritative, and a client-side forced result cannot steer it. Those need a BO paytable
> fixture instead.

---

**Not used by any case:** `holdSpinStart()` / `holdSpinEnd()`. They exist as an escape hatch in
`DriveContext.hold_button(use_bridge=True)` for builds where Cocos ignores a synthetic pointer hold;
no scenario passes that flag, and the hold-input cases (Mah_022, CR_027/028/042) all drive a real
`mouse.down` → wait → `mouse.up`. Do not treat them as required.

---

## 8. Buttons — what each control must DO

`getButtonRect(name)` and `waitForButtonClickable(name)` address controls by **name**. This section
says what the framework will do with each one and what it must achieve, so a bridge can be
implemented against behaviour rather than against a list of strings.

Names below are the reference dialect (Mystic). A build that spells them differently maps them in
its profile's `BUTTONS` table — the framework addresses controls by ROLE, never by literal. Each
game's actual mapping is its profile class in `scripts/game_profile.py` (§12).

**The roles the suite drives** (reference names; set a role to `None` only if the build genuinely
lacks the control — see "Main screen or Settings menu" below before doing that for a system button):

```
spin · turbo · auto · ante · buy_bonus · stake_up · stake_down · history · info · settings · sound
auto_confirm · auto_stop · auto_stop_at_win · auto_close · auto_count:{i}
bet_row:{i} · bet_close · settings_close · message_ok
settings_sound · settings_music · settings_turbo · history_close · info_close
detail_next · detail_prev
```

`auto_count:{i}` indexes the profile's `AUTO_COUNTS` (default `10, 20, 30, 50, unlimited`).
`bet_row:{i}` is the **denomination index** (0 = lowest), not the on-screen position.

**Popup labels** (`getSnapshot().visiblePopups`, mapped in the profile's `POPUPS`):
`history · historyDetail · information · autoPlay · denominations · setting · message · bigWin ·
freeSpinResult · promo`.

### The rule that makes all of them testable

> **A control must be resolvable only when clicking it would actually do its job.**
>
> - Cannot do its job right now → `getButtonRect` returns `null` **and** `waitForButtonClickable`
>   **rejects**. Not "resolves to a rect that does nothing".
> - Does not exist in this build → `getButtonRect` returns `null`, and must **not throw**. That is
>   how the suite probes for optional features.
>
> A control that resolves while inert is the worst case for a test suite: the click lands, nothing
> happens, and the failure surfaces later somewhere unrelated. Two real examples of the cost —
> `canDo('nextBet')` reporting `popup_not_open:bet` for the button whose whole job is to OPEN that
> popup (D2), and an auto-stop that resolves mid-run but does not stop the run, which surfaces as
> a `waitForSpinClickable` timeout in the *next* run's teardown.

**Fail fast, not silently.** The framework treats a rejection as a real failure and reports it with
the control's name. It cannot detect a click that was accepted and ignored — that is precisely why
the resolvability rule above is the bridge's responsibility.

### Core play

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `spin` | `spin` | Send one bet. | Idle and affordable-or-not — **an insufficient balance must NOT block it**; the client shows its out-of-money popup on click, and tests assert that. Not during autoplay, free spins, or an unfinished presentation. |
| `stop` | `stop` | Cut the current spin's animation short. Optional. | While a spin is presenting. |
| `turbo` | `turbo` | Toggle fast play. | Any time it is togglable; pair with `getSpeedMode()` so the effect is checkable. |

### Presentation skips — the controls that buy back run time

A BIG / MEGA / ULTRA celebration takes the screen for several seconds of count-up, and inside a
free-spin bonus it can fire on several rounds in a row. Nothing in the suite grades the celebration
— `waitForSpinClickable` must not resolve through one (§2), so every second of it is dead run time,
on every round, on every suite. The celebration is skippable by the player, so the framework does
what the player does.

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `big_win_skip` | `bigWin:skip` | Cut the big-win celebration short. **Tap 1** ends the count-up and shows the total; **tap 2** dismisses the banner. | Only while the celebration is on screen — i.e. while its popup is in `visiblePopups` **or** its label is in `busyOps`. `null` before and after. |
| `freespin_result_skip` | `freeSpinResult:skip` | Dismiss the end-of-bonus result / total-win panel. On builds where this panel is what actually runs the *bonus-result → back to base* handler, it must run it — hiding the panel without resuming the game leaves the next `waitForSpinClickable` hanging. | Only while that panel is up. |

**Two taps, ~1 s apart.** That is what the client's own control expects. A single tap leaves the
banner sitting there and the next `waitForSpinClickable` blocks on it anyway, so a bridge must not
collapse the two into one.

#### ⚠ The rect must be a point that is actually ON the canvas

A celebration skip is usually a **full-screen tap layer**, not a discrete button — and the centre of
a full-screen node is very often not where the bridge thinks it is. When `getButtonRect` returns a
centre outside the canvas, `waitForButtonClickable` rejects `offscreen` **forever**: the centre never
moves, so this is not a race that a longer timeout fixes.

> Measured on Grandma Pets dev, 2026-09-24 (`docs/grandma-bridge-defects.md` **G1**): `bigWin:skip`
> was in `getAllButtonRects()` keys — the build had it, the name was right, and `BigWinPopup` was in
> `visiblePopups` with `bigWin` in `busyOps` at the moment of the call — yet it rejected
> `offscreen:bigWin:skip` through 3 s of polling. Every celebration in every Grandma run played out
> in full. **Fixed by returning an on-canvas point instead of the backdrop's centre.**

**So: a tap-outside or full-screen control must return a small rect on an uncovered point INSIDE the
canvas, not the backdrop's centre.** This is the same treatment `auto:close` and `bet:backdrop`
already need, and it is the single most likely thing to be wrong about a newly added skip control.
§11's build check exists to catch it: **every `getAllButtonRects()` centre must land inside the
canvas** — including the ones that only resolve mid-presentation, which a probe run at idle never
sees.

#### Why a named control, and not a method

Three of the four bridges in this repo shipped a different shape for this, and only one of them is
drivable. The contract requires the **named control**:

| Shape | Shipped by | Why it is not enough |
|---|---|---|
| **`bigWin:skip` control** | Grandma | ✅ **This is the contract.** Addressed by role like every other control, so the profile maps the build's spelling; the resolvability rule gives a real rejection with a reason instead of a silent no-op; and the framework clicks real coordinates — the player's actual gesture. |
| `skipBigWin()` method | Chaos (`framework/helpers/bridge.py`) | Fire-and-forget: it throws when not applicable but reports nothing about **when the screen is clear**, so the framework is back to guessing with a sleep. It also bypasses the gesture, so it proves nothing about whether a player could skip it. |
| `canDo('skipBigWin')` action | Mystic | Says *whether* it could be skipped but gives no rect to click. Fine as a **diagnostic alongside** the control — `canDo` is exactly how the framework reports the reason today — but it cannot drive anything on its own. |
| `dismissPopups()` closes it | Zeus v3 (retired `skipBigWin()` into it) | ⚠ **Do not do this.** `dismissPopups()` is the teardown broom: it runs between runs and before opening a surface. A celebration that it force-closes can be swept away mid-round, which cuts the recorded video the AI asserts grade — and it leaves a scenario whose subject *is* the celebration with no way to let one play. Grandma has this right: presentation popups (`BigWinPopup`, `FreeSpinResultPopup`, …) are **never** force-closed by `dismissPopups()`; they end through their own skip or timeout. |

**Bridge must** also make the celebration visible while it runs: its popup in `visiblePopups`, or
its label in `busyOps` (`bigWin`, `balanceCountUp`). The framework waits for one of those before it
reaches for the skip, because a round can qualify for a celebration several seconds before the
banner appears — a ways game presents `winWays`, then `wildMultiplier`, then the count-up, and only
then the banner, 6.5 s after the response on a measured Grandma round. With neither signal there is
nothing to wait for and the skip cannot be armed at all.

**Framework will** ask the RESPONSE first — `payout / baseStake >= threshold`, with the threshold
read from `settings.winFormula` (§5) — so a round that cannot celebrate never waits and never polls.
This is an optimisation, not an assertion: **a missing or broken skip never fails a run.** It makes
runs slower and nothing else, which is precisely why it has to be checked at build time rather than
discovered from a red suite.

Set `ctx.skip_big_wins = False` in a scenario whose subject IS the celebration.

### Bet

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `stake_up` / `stake_down` | `stakeIncrease` / `stakeDecrease` | Change the stake by one ladder step, **or** open the denomination list — either is fine, but say which in the profile (`HAS_BET_POPUP`). The suite probes at runtime and reports honestly. | Not mid-spin, not during autoplay. |
| `bet_row:<i>` | `bet:row:{i}` | Select denomination **index `i`** of `getDenominations()` — `0` = lowest. Indexed by POSITION in that array, not by label text. | Only while the denomination list is open. |
| `bet_plus` / `bet_minus` | `bet:plus` / `bet:minus` | Step the stake within the open list. | Only while it is open. |
| `bet_close` | `bet:close` | Close the list, keeping the selection. | Only while it is open. |

### Auto-play

This group is where inert controls hurt most, because a failure leaves the client **still betting**.

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `auto` | `auto` | Open the autoplay panel **when idle**. Where the same control also STOPS a running auto (Mystic does), it must genuinely stop it — see below. | Idle, or during a run if it doubles as the stop. |
| `auto_count:<i>` | `auto:count:{i}` | Select spin-count **index `i`** of the profile's `AUTO_COUNTS`; the last entry is unlimited. Must change what the panel shows. ⚠ The panel opens on index 0, so selecting index 0 is a no-op — scenarios detour via another index to make the change visible. | Only while the panel is open. |
| `auto_stop_at_win` | `auto:stopAtWin` | Arm "stop on any win" for the run about to start. | Only while the panel is open. |
| `auto_confirm` | `auto:confirm` | Start the run with the selected count, and close the panel. | Only while the panel is open. |
| `auto_close` | `auto:close` | Dismiss the panel without starting. | Only while the panel is open. |
| `auto_stop` | `auto:stop` | Stop a running auto **from inside the panel**. | Only while the panel is open. ⚠ On Mystic the panel is CLOSED during a run, so this is not the main-screen stop. |
| `auto_stop_main` | *(none on Mystic; Zeus: `stopAuto`)* | Stop a running auto **from the main screen**. | Only while a run is going. |
| `auto_or_freespin` | *(none on Mystic; Zeus: `autoOrFreeSpin`)* | The control that REPLACES spin during auto / free spins. | During a run. |

**Stopping a run must actually stop it.** `auto_stop_button()` picks, in order:
`auto_stop_main` → `auto_or_freespin` → `auto` → `auto_stop`. After the click the framework expects
`isAutoPlay` to go false and no further BET to be sent. If a build has no working main-screen stop,
it must expose one — the popup-scoped `auto:stop` is unreachable while the panel is closed, and the
run cannot be halted at all.

> Verified on Mystic: during a run, `auto:stop`, `stopAuto`, `autoOrFreeSpin` and `auto:close` all
> resolve to `null` (the panel is closed) and `spin` is `interactable: false`. Only `auto` resolves,
> and clicking it stops the run — which is why it is last in the preference order but the one that
> works there.

**Pacing.** The gap between two auto spins is the client's presentation, not a server round-trip.
Measured on Mystic: typically 4-9s, but 14.3s and 37.1s in a sample of eight. The framework waits
`auto_timeout_ms` (90s) for each, so a bridge does not need to hurry — but it must deliver **every**
round's response through `awaitBetResponse`, in order, including the ones separated by a long
celebration.

### Panels and toggles

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `history` / `history_close` | `history` | Open / close the History panel. Pair with `getHistoryRows()` to make rows assertable. | Idle. |
| `detail_next` / `detail_prev` | `historyDetail:next/prev` | Page a History detail ticket. Must agree with `getHistoryDetailState().hasNext/hasPrev`. | Only while a detail ticket is open. |
| `info` / `info_close` | `info` | Open / close the tutorial or paytable. Pair with `getTutorialRect()` where it is a WebView (§8 forbids the bridge scrolling it). | Idle. |
| `settings` / `settings_close` | `settings` | Open / close settings. | Idle. |
| `sound` · `settings_sound` · `settings_music` | `sound`, `settings:*` | Toggle audio, on the main screen or inside settings — whichever this build has. Pair with `isSoundEnabled()` / `isMusicEnabled()`. | Wherever the control lives. |
| `message_ok` | `message:ok` | Dismiss a notification / confirmation popup. | Only while one is showing. |
| `trophy` | `trophy` | Open the trophy / leaderboard panel, where the build has one. | Idle. |

### Main screen (legacy UI) or Settings menu (new UI) — check every system button

Games ship two layouts, and **you cannot tell which one from the name alone**:

- **Legacy UI:** History, Info, Sound (and often Turbo) are buttons on the **main screen**.
- **New UI:** some or all of them moved **inside the Settings menu**. The player taps Settings,
  then History / Info / Music / Sound / Turbo in the panel.

Some builds have **both**: Grandma Pets keeps Turbo and a mute toggle on the main screen, *and*
has Turbo / Sound / Music inside Settings, while History and Info exist **only** inside Settings.

**When onboarding a game, fill this table for its build before writing the profile.** Check each
control in both places on the live build (method below), and record every name that resolves:

| Control | Profile role(s) | Legacy name (main screen) | New-UI name (inside Settings) | Grandma Pets |
|---|---|---|---|---|
| History | `history` | `history` | `settings:history` | **menu only** |
| Info / guide | `info` | `info` | `settings:info` | **menu only** |
| Turbo | `turbo` · `settings_turbo` | `turbo` | `settings:turbo` | both |
| Sound | `sound` · `settings_sound` | `sound` | `settings:sound` | both |
| Music | `settings_music` | *(no main-screen role)* | `settings:music` | menu only |

**How to check, on the live build, at idle:**

1. `getButtonRect('<legacy name>')` → a rect means it is on the main screen; `null` means it is not.
2. Click `settings`, then `getButtonRect('settings:<control>')` → a rect means it is in the menu.
3. Cross-check the game's bridge doc: its **main-screen** table and its **`SettingPopup`** table.
   ⚠ Trust the probe over the doc. Grandma's bridge doc still listed `history · info` on the main
   screen after they had moved (`docs/grandma-bridge-defects.md` G2), and a profile built from that
   table failed three runs with `not_available:history`.

**Bridge side — how to expose them:**

- Name an in-menu control by its panel: `settings:history`, `settings:info`, `settings:sound`,
  `settings:music`, `settings:turbo`.
- It must resolve **only while that panel is open** (the resolvability rule at the top of §8).
- The bridge doc lists it under **that panel's** buttons, not the main screen. A control present in
  both places gets **both** names, one in each table.

**Profile side — how to map them:**

| The control lives… | Profile |
|---|---|
| on the main screen only | nothing to do; the defaults (`history`, `info`, `turbo`, `sound`) are right |
| in the menu only (History / Info on Grandma) | map the ROLE to the in-menu name, and declare where it lives: `BUTTONS["history"] = "settings:history"` plus `BUTTON_VIA = {"settings:history": "settings"}` |
| in both places (Turbo / Sound on Grandma) | keep the main-screen role (`turbo`, `sound`) and ALSO map the settings role (`settings_turbo = "settings:turbo"`, `settings_sound = "settings:sound"`); scenarios that test the menu use the `settings_*` role |
| Music (menu only on every build so far) | `settings_music = "settings:music"` |

The menu panel also needs its close control and popup label, `settings_close` and
`POPUPS["settings"]` (Grandma: `settings:close`, `SettingPopup`), so the framework can close it.

```python
class Grandma(WaysGame):
    BUTTONS = dict(SlotGame.BUTTONS, **{
        "history": "settings:history",          # menu only -> the in-menu name
        "info": "settings:info",
        "settings_turbo": "settings:turbo",     # in both places -> settings role as well
        "settings_sound": "settings:sound",
        "settings_music": "settings:music",
        "settings_close": "settings:close",
    })
    POPUPS = dict(SlotGame.POPUPS, settings="SettingPopup")
    # control NAME -> ROLE of the button that opens the panel it lives in
    BUTTON_VIA = {"settings:history": "settings", "settings:info": "settings"}
```

**What the framework does with `BUTTON_VIA`:**

| Moment | Behaviour |
|---|---|
| a scenario clicks `history` | if `settings:history` is not on screen, click `settings` first (timeline: *"open settings to reach settings:history"*), then `settings:history`. If Settings is already open, click straight through. |
| hold / double-click | the same, since every gesture goes through the same step |
| nested menus | an opener may itself be in `BUTTON_VIA`; it is resolved recursively |
| closing History | after `history:close`, if the game went back to the Settings panel instead of the main screen, `close_opener_if_left` closes it with `settings_close` (falling back to `dismissPopups()`). That counts as the close **working**, not a stuck popup. |

No scenario needs to change: they keep clicking `history` / `info`.

**Three rules, each one a failure we have hit:**

- **Key `BUTTON_VIA` by the resolved NAME, not the role**: `"settings:history"`, not `"history"`.
  It is looked up after the dialect is translated.
- **Never map a moved control's role to `None`.** `None` means "this build has no History" and
  quietly skips its cases. The control exists, one tap further in.
- **Symptoms of a wrong profile:** `waitForButtonClickable(history) rejected -> not_available:history`;
  `open-tutorial` / `paytable-bet-scaling` unreached with *"info … never opened"*.

### Features — TWO UI systems, one set of roles

A paid feature (Ante Bet, Premium Reel, Buy Bonus) reaches the player in one of two shapes, and a
bridge must say which one this build is. **Neither is the default** — a scenario asks the profile,
never the screen.

| | System A — a control per feature | System B — one feature panel |
|---|---|---|
| Shipped by | Chaos (`anteBet` toggle) | Dragon VIP |
| On | one click on the feature's own control | **three** clicks: open the panel → the feature's ACTIVATE row → confirm |
| Off | one click on the same control | **one** click on the main button, no panel, no confirm |
| Main feature button | opens Buy Bonus only | opens the panel — **or** switches the active feature off (below) |
| Roles the profile names | `ante` | `ante_on` + `ante_off`, `premium_reel:<reelId>` + `premium_off` |

The framework drives both through one helper (`switch_feature`), so a shared case does not care which
system it is on. What it cannot survive is a profile that names an `ante` toggle for a build that has
none: the click lands on nothing and every ante comparison is then made between two plain spins.

#### System B — the controls

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `buy_bonus` | `buyBonus` | Open the feature panel — **or** switch off whichever feature is on. | Idle, `compliance.bonusBuyAllowed`, **and no feature currently on** (see the three behaviours below). |
| `buy_package:<i>` | `buyBonus:package:{i}` | The BUY row for free-spin package `i` → raises the confirm sheet. | Panel open on the LIST, and `stake × price ≤ balance`. |
| `ante_on` | `buyBonus:anteBet` | The ACTIVATE row for Ante Bet → raises the confirm sheet. | Panel open on the list, Ante offered and OFF, no Premium Reel on, affordable. |
| `premium_reel:<reelId>` | `buyBonus:premiumReels:{i}` | The ACTIVATE row for that package → raises the confirm sheet. `reelId` is **opaque** (`2`, `3`, `23`) and comes from `getPremiumReelOptions()` — never a row index, never hard-coded. | Panel open on the list, that package offered and OFF, Ante not on, affordable. |
| `buy_confirm` / `buy_cancel` | `buyBonus:confirm` / `:cancel` | Accept / go back. Confirm activates the feature (or buys the package) **and closes the whole panel**. | **Only while the confirm sheet is up.** |
| `buy_close` | `buyBonus:close` | Close the panel. | Only while the LIST is shown — it is hidden under the confirm sheet. |
| `buy_next` / `buy_prev` | `buyBonus:next` / `:prev` | Step the panel's stake. ⚠ **Changes the game's real stake immediately**, on the click, with no confirm — and closing the panel does not put it back. Re-read `betAmount` after using it. | Only while the list is shown. |
| `ante_off` | `anteBet:off` | Switch Ante Bet off. One click, no panel, no confirm. | Ante is on. |
| `premium_off` | `premiumReels:off` | Switch the active Premium Reel off. One click. | A Premium Reel is on, Ante not on. |

**The main button has three behaviours.** This is what most often breaks a ported scenario:

| State when `buy_bonus` is clicked | Result |
|---|---|
| Ante Bet on | switches Ante **off**; the panel does not open |
| a Premium Reel on | switches that package **off**; the panel does not open |
| neither on | the panel opens |

**Mutual exclusion.** Ante Bet and a Premium Reel can never both be on, and while either is on the
panel cannot be opened — so the other feature's row is unreachable until the first is switched off. A
case that asserts the exclusion reads it from the bridge (`getButtonRect('buyBonus')` and
`canDo('activatePremiumReels:<id>')`), never by clicking to see.

**What the bridge must get right for System B**

- **Gate the rows; never let one resolve while inert.** The panel itself disables nothing: a row for a
  feature that is already on toggles it *off*, and an unaffordable row still looks clickable because
  the game checks the price only after the click. The general rule at the top of §8 is what makes this
  testable — the framework must never need a "click it and see" fallback.
- **Session availability is separate from row presence.** `anteBetAvailable` / `premiumReelAvailable`
  say whether this session offers each feature, and a panel may render an Ante row in a session
  without Ante, where the click does nothing. Report availability honestly: a session that does not
  offer a feature makes its cases *unreached*, and a build that cannot say so turns that into a
  failure. Measured on Dragon VIP dev, 2026-09-16: `settings.premiumReel` null →
  `premiumReelAvailable` false, `getPremiumReelOptions()` `[]`, and every
  `canDo('activatePremiumReels:*')` answering `feature_unavailable:premiumReels`.
- **Insufficient balance opens the message popup**, not the confirm sheet, and the panel stays on the
  list. Expose it as the `message` popup with its OK control, as any other notification.
- **A feature animation follows the confirm**, with the UI locked. `waitForSpinClickable()` must not
  resolve through it, and `busyOps` should name it (`anteBet.animation`, `premiumReels.animation`).
- **Closing is animated (~0.2 s)**, so the panel node lingers briefly after a close. Readiness is
  `waitForSpinClickable` / `waitForButtonClickable`, never the popup label disappearing.
- **A feature toast is NOT part of the contract.** Dragon VIP raises none when Ante is switched on,
  even though `anteNotify` appears in its toast label list. Whether a build announces a feature with a
  banner is a product decision; what every build must report is the STATE — `isAnteBet`,
  `isPremiumReel`, `premiumReelId`, `premiumReelMultiplier`, `spinCost`. Cases assert those.

#### Both systems

| Role | Reference name | Must achieve | Resolvable when |
|---|---|---|---|
| `ante` | `anteBet` | **System A only.** Toggle Ante Bet. Must change what the player PAYS while leaving the payout base (`baseStake`) alone — a test asserts exactly that divergence. | Idle only; **must not** be usable during a bonus. |
| `buy_confirm` | `buyBonus:confirm` | Purchase the selected package. Must produce a real bonus, indistinguishable in the response stream from a naturally triggered one. | System A: while the Buy Bonus panel is open. System B: while the confirm sheet is up. |

Whichever system a build ships, a feature that is ON must stay on for the whole bonus it paid for: its
off control must not resolve mid-feature, and the response must keep echoing the feature
(`anteMultiplier`, or `premiumReelId` / `premiumReelMultiplier`) on every round of it.

### Controls the profile declares but no case drives yet

`auto_close` · `bet_plus` · `bet_minus` · `buy_cancel` · `buy_next` · `buy_prev` · `history_close` ·
`info_close` · `message_ok` · `settings_close` · `settings_turbo` · `stop` · `trophy`

Implement them if the build has them — several are needed for cases not yet automated — but nothing
currently fails without them. `getButtonRect` must still return `null` rather than throwing for any
the build lacks.

---

## 9. Three things the bridge must NOT do

**Drive the game to a deterministic start.** §1 and §3. `startGame()` resolves at the click and stops
there; the bridge's half is answering *is spin clickable?*, and the framework asks until it is.

**Decide when a round is over.** `waitForSpinClickable` reports readiness; the framework decides what
to do with it. Anything that hides a mid-round state behind "ready" breaks the settle contract and
clips animations out of the recorded video.

**Scroll a cross-origin WebView.** The tutorial is served from `staticUrl`, so the browser blocks
injected JS from touching it. Only a real `page.mouse.wheel` from the framework reaches it. The
bridge's job is to say *where*: `getTutorialRect()`.

Related: the bridge deliberately exposes **no round outcomes**. Zeus's v2 dropped its
`verifyHoldSpin*` analysers for exactly this reason — the Hold & Spin rules live in the framework's
invariants, computed from the response stream.

---

## 10. What changes for an existing bridge

Measured against Zeus's v2 bridge (`games/zeus/bridge-api-v2.md`), which is the closest thing to a
current implementation. Three changes, one addition:

| # | Change | Today | Required |
|---|---|---|---|
| 1 | **`startGame()` semantics** | "Clicks Start, waits for scene, drives to a clean base-game idle" — it awaits `waitForDeterministicStart()` internally | Click Start and resolve **at that click**. Do not wait for idle. Do not play out a resumed bonus. Do not dismiss popups on a loop. |
| 2 | **`getGameInfo()` becomes REQUIRED and must be captured** | present, but returns a thin `settings` and is not specified as captured | Capture the launch payload at load; return that copy unchanged. Must include `pending` and `settings.defaultBetLevel`. |
| 3 | **`waitForSpinClickable()` is now the single idle authority** | already exists with the right contract | Unchanged in shape — but it must be *right*, because nothing second-guesses it. It may not resolve during autoplay, free spins, a win count-up, a celebration, or behind a popup. |
| 5 | **Controls must be resolvable only when they work** (§8) | not specified | `getButtonRect` → `null` and `waitForButtonClickable` → reject whenever a click would not do the control's job. A control that resolves while inert makes the failure surface in an unrelated later run. Most urgent for the auto-play group: a stop that does not stop leaves the client betting. |
| 4 | *(addition)* `canDo(action).reason` | exists; `nextBet` is inverted (D2) | Keep, as a diagnostic. Fix the inversion: a control that OPENS a popup must not be gated on that popup being open. |
| 6 | *(addition)* **a named `big_win_skip` control** (§8) | Zeus retired `skipBigWin()` into `dismissPopups()`; Mystic exposes `canDo('skipBigWin')` with no control; Chaos has a `skipBigWin()` method | Expose it as a **control**, resolvable only while the celebration is on screen, returning an **on-canvas** point (G1). Keep `canDo('skipBigWin')` as the diagnostic beside it. Stop letting `dismissPopups()` force-close presentation popups — it is the teardown broom and it runs mid-video. Costs run time only: nothing fails without it. |

Change 1 also retires the failure behind **D1**: with no `waitForDeterministicStart` inside
`startGame`, an unsatisfiable predicate can no longer swallow the promise, and the popup-dismissal
loop that closes every popup ~0.4s after it opens goes away with it. `autoSpinsRemaining` returning
`-1` on a fresh session is still worth fixing (it is reported in diagnostics), but it stops being a
blocker.

**What does NOT change:** the BET response, the button and popup name registries, `getButtonRect` /
`waitForButtonClickable`, `awaitBetResponse` / `awaitFreespinRound`, and every optional getter in §7.
Mystic's verified suite exercises all of that as-is.

---

## 11. How to check a build

```bash
python scripts/probe_bridge.py --game <game> --env dev
```

Prints the REQUIRED-API check, the snapshot, whether `waitForSpinClickable` resolves and how fast,
the buttons the build actually exposes, and profile-vs-reality. Reach for it first on any new game or
any hang — it turns "it hangs" into an exact answer in one run.

⚠ **It runs at idle, so it cannot see a control that only resolves mid-presentation.** Every
`getAllButtonRects()` centre must land inside the canvas — including `big_win_skip` and
`freespin_result_skip`, which exist only while their celebration is up. That gap is how G1 shipped:
the probe was clean, and the control was unclickable in every run for a week. Check those two while
a celebration is actually playing:

```bash
python scripts/probe_big_win.py --game <game> --env dev   # ⚠ it bets
```

It spins until a round reaches the threshold, then reports the bands it read, whether the build
knows the control, what surfaced at 50 ms sampling, and whether two taps actually clear the banner.

### Queued framework changes

Held until the bridge side of this contract is agreed and implemented. Recorded here so nothing is
lost between the two halves of the work:

| # | Change | Why |
|---|---|---|
| 1 | Drop `appState` from the `_diag()` failure string | §4 — not portable across games, so it should not appear even in diagnostics. |
| 2 | Drop the `getGameSettings()` fallback from `boot-base` and `browse-denominations`, and from `SlotGame.OPTIONAL_API` | §7 — removed from the contract. Both JS reads already prefer `getGameInfo().settings`, so this is dead-code removal, not a behaviour change. |
| 3 | Move `auto-reload-cancels` to a video-only assert in `zz-autoplay.yml` (ZZ-18) and `cr-auto-turbo.yml` (CR_023) | `facts.auto_cancelled` is unfalsifiable now that `reload_and_start` asserts idle internally — it would have raised first. Mystic's Mah_018 already asserts by video and needs no change. |
| 4 | ~~Rewrite the boot mock tests~~ — **done**: 6 paths, each contract violation now failing the run rather than falling back | Matches the single-authority design. |

**Deliberately NOT changing:** `boot-base`'s `opens_at_default` fact, by decision — it is latent (no
suite asserts it) and stays as-is. Anyone who later writes a "game opens at the default bet" case
(Mah_006 / ZZ-04) must use the first-session rule in §5 rather than this fact, which compares
`betIndex` to `defaultBetLevel` and so only holds for a player's very first session.

**No longer needed:** `insufficient-balance` required no change. An insufficient balance leaves the
spin button clickable — the client shows its out-of-money popup on click — so the boot idle assertion
passes and the scenario runs as written. §2's readiness wording was corrected to say so.

### Status, 2026-08-28

**Mystic is the verified reference.** Its suite was validated against a live game, and the behaviour
this contract describes is the behaviour those cases already exercise — the only delta to the
architecture in §1/§3 is **game start and resume**, which is what changed here. Everything else in
its ~50 cases (drive, settle, invariants, popups, denominations, History, language) stands as
written, and Zeus and Chaos are modelled on it.

So the scope of what is NEW and therefore not yet re-confirmed live is narrow:

| Area | State |
|---|---|
| Mystic case suite | **verified** — unchanged by this work |
| `startGame()` resolving at the click | contract change; bridges must follow |
| `getGameInfo().pending` resume at boot | **new** — verified against a mock bridge + recorded fixtures |
| deterministic-idle assertion | **new** — same |

The resume path was driven through 7 branches on a mock bridge: clean session · bonus played to
terminal · max-win truncation mid-resume · client-plays-internally fallthrough · lost promise
escaping via `isFreeSpinMode` · popup-never-clears · celebration-still-presenting. Response parsing
was regression-checked against every recorded fixture in `games/*/fixtures/bet-responses/`.

Live re-confirmation of the resume path is still outstanding, because neither dev build currently
serves a bridge:

| Game | Bridge on the dev build today | Notes |
|---|---|---|
| Zeus | **not installing** on `192.168.1.27:7456` | The bridge code IS in the bundle (`assets/main/index.1caf5.js`) and `BridgeActivator.activate()` is called from `LoadingScene.onGameReady()`, but `window.__gameTestBridge` never appears. Defects D1–D4 also open. |
| Mystic | **absent** from the build probed | Does not affect the cases already verified; it blocks re-running them against the new boot path. |
| Chaos | not probed | |

---

## 12. Onboarding a new game — the profile

A new game inherits every scenario and test case once its bridge implements §2 and a profile says
what the build calls things. The profile is the only per-game code:

```python
# scripts/game_profile.py
class LuckyCascade(CascadeGame):      # or HoldSpinGame / LineGame / WaysGame — the BONUS mechanism
    name = "lucky"
    HAS_ANTE_BET = True                                        # capability
    BUTTONS = dict(CascadeGame.BUTTONS, ante="anteWager")      # dialect (roles: §8)
    POPUPS  = dict(CascadeGame.POPUPS, history_detail="ticketDetail")
    BUTTON_VIA = {}                                            # §8 "Main screen or Settings menu"
    BASE_STATE, FREESPIN_STATE = 0, 5                          # server constants

register(LuckyCascade())
```

Then `python scripts/run_scenario.py --game lucky --suite ...`. The runner calls
`se.set_profile(args.game)`, and every scenario reads names through the profile.

**Onboarding order:**

1. Implement §2 on the bridge; check it with `python scripts/probe_bridge.py --game <game> --env dev` (§11).
2. Fill the **system-button table** in §8 "Main screen (legacy UI) or Settings menu (new UI)" for
   History, Info, Turbo, Sound and Music, by probing the live build. Map `BUTTONS` and
   `BUTTON_VIA` from it.
3. Map the remaining roles (§8) and popup labels, and capture a few BET responses into
   `games/<game>/bet-response.md` (§6).
4. Declare the capabilities below.

**Prefer fixing the bridge over overriding.** Each override is a permanent fork in the dialect; a
game that matches the defaults needs no profile code at all beyond `name`.

**Capabilities.** Declare what the build ships. `False` makes dependent cases report *precondition
not met* (inconclusive) rather than failing, which is the honest verdict for an absent feature:

`HAS_TURBO · HAS_ANTE_BET · HAS_PREMIUM_REEL · HAS_BUY_BONUS · HAS_AUTOPLAY · HAS_FREE_SPINS ·
HAS_LAST_BET_PANEL · HAS_TUTORIAL_WEBVIEW · HAS_SPEED_MODE_GETTER · HAS_HISTORY_DETAIL_NAV ·
HAS_SOUND_GETTERS · HAS_BET_POPUP · HAS_DEV_MODE`

**What you inherit.** With REQUIRED + `dismissPopups` + `getHistoryRows`: base spins, balance
debit/credit, denominations, bet changes, autoplay (all its variants), free-spin completion,
insufficient balance, History, reconnect/multi-tab, spin inputs, turbo, feature-toggle popups and
the multi-language matrix. Add `getHistoryDetailState` + `historyDetail:next/prev` for free-spin
ticket paging, `getSpeedMode` for an exact turbo check, and `getTutorialRect` for the tutorial
cases. Reference coverage maps: `docs/mahjong-coverage.md`, `docs/zeus-coverage.md`,
`docs/grandma-coverage.md`.
