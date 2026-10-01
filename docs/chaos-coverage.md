# Chaos Runner — coverage map (111 cases)

Which of `test-case-chaos.csv`'s 111 cases the suites automate, which scenario reaches each, and
what is deliberately left manual. Companion to `docs/mahjong-coverage.md` (108) and
`docs/zeus-coverage.md` (52).

| | |
|---|---|
| Cases in the CSV | **111** |
| Automated | **73** (66%) across 75 case entries |
| Every automated case has a hard `stream:` assert | **yes** — no case is AI-only |
| Not automated | **38** — reasons per case below |
| Suites | 5 · `games/chaos/test-suites/cr-*.yml` |
| Profile | `game_profile.ChaosRunner` (mechanism: **line**) |
| New scenarios | 7 · new invariants 8 |

**Read `docs/session-summary-scenario-qa-3.md` first.** This file assumes its model: scenarios are
closed boxes, `stream:` is exact and red, `video:`/`image:` are AI and orange, and a scenario that
cannot reach its situation returns *unreached* → **inconclusive**, never a false pass.

---

## 1. Two things that will decide whether a green here means anything

### 1.1 No BET response has ever been captured for this game — so the grid fields are UNVERIFIED

Onboarding step 2 ("capture real responses") has **not** been done. `games/chaos/fixtures/` does not
exist. The bridge doc pins the envelope shape and nothing else, so every field name in
`ChaosRunner` past that — `result.views[].value`, `multiplier`, `anteBet`, `anteMultiplier` — is
**modelled on the cascade reference, not verified**.

This is handled honestly rather than hopefully:

* The census helpers return `None`, never `0` or `[]`, when the grid cannot be read. An empty
  census would make "P1 never appears on reel 2" pass vacuously.
* Grid-dependent scenarios return **unreached** on `None` → the case is **inconclusive**.
* The invariants carry a `cannot verify …` violation as a backstop if one slips past the scenario.

So the failure mode is a wall of inconclusives, which is loud and correct — not a wall of greens,
which would be a lie. **Until a real response is captured, treat every `cr-features.yml` green as
unproven.** Capturing one loss, one win, one trigger and one free spin closes it.

### 1.2 The design doc is wrong in four places; the suites encode QA's corrections

`games/chaos/docs/final/game-knowledge.md` §5 has the full list. The four that reach test code:

| Rule | The doc says | QA confirmed | Where it bites |
|---|---|---|---|
| Free-spin award | 3/4/5 → **20/15/10** | 3/4/5 → **12/15/20** | `freespin_award`, CR_046 |
| Free-spin award, again | base 15 × ways multiplier (→ 60) | **not the award model at all** | same |
| Ante multiplier | "increases the bet by 1.5x" | **x3** — and BO-configurable | `ante_stake_multiplier`, CR_051 |
| Bet per line | worked examples use base/**5** | base/**20** | CR_015, `LINE_DIVISOR` |

Do not "correct" these back toward the doc. The doc's own worked payout (`200 × 0.2 = $40`) is
computed from the wrong divisor and does not hold.

---

## 2. What was built

### 2.1 The `LineGame` mechanism — new

Chaos is neither a cascade nor a hold-and-spin game: one grid per round, paid along fixed lines,
plus one ways symbol. `LineGame` adds the reading that makes such a game assertable — a **per-reel
symbol census** (`grid` · `reel_counts` · `reels_with` · `symbol_count` · `ways`). Almost every rule
Chaos states is a predicate over that census, which is why these cases are exact instead of visual.

### 2.2 Two engine bugs fixed at the source

Both would have broken Chaos on the first run, and one was silently broken for Zeus already:

1. **The bridge returns the socket envelope.** `awaitBetResponse()` is documented to hand back
   `{status, code, msg, timestamp, result}`, but `Bet.parse` reads fields off the top level — so
   every round would have parsed as a zero-payout loss and `conservation` would have failed a game
   that was behaving perfectly. Fixed with `SlotGame.unwrap()`, identity for profiles that already
   return the inner object.
2. **The engine hardcoded the reference build's button names.** ~40 call sites named
   `stakeIncrease` / `bet:close` / `auto:confirm` literally instead of going through `GAME.button()`.
   Chaos calls those `betPlus` / `denomination:close` / `autoplay:confirm`. **Zeus had the same
   latent bug** — its profile declared `nextBet` and `auto:package:{i}` that nothing ever read.
   Fixed with `SlotGame.resolve()`, which maps a literal in any dialect back to its role and out
   through the active profile. A role this build maps to `None` now answers "absent" instantly
   instead of burning a full `waitForButtonClickable` timeout.

### 2.3 New scenarios (7)

| Scenario | Reaches | Cases |
|---|---|---|
| `ante-toggle` | one spin ante-OFF, the hover info panel, one spin ante-ON at the same bet | CR_049/050/051/054/109 |
| `ante-freespins` | ante ON → trigger → whole bonus; probes the ante lock, tracks the frozen multiplier | CR_053/055/056 |
| `p1-ways-win` | hunts a P1 ways win **while auditing every spin** for P1 on an illegal reel | CR_045/062/063 |
| `raining-wild` | a 2+-Wild result **and** a contrasting <2-Wild one | CR_059/060/061 |
| `random-multiplier` | samples the multiplier in the base game and across a bonus | CR_057/058 |
| `paytable-bet-scaling` | shoots the paytable at two different bet levels | CR_014/015/016/017 |
| `game-help` | wheels the Dynamic Game Help, shooting each screenful | CR_070/098/099/107 |

### 2.4 New invariants (8)

`freespin_award` · `freespin_retrigger_adds` · `p1_reels_only` · `p1_ways_payout` ·
`multiplier_in_ladder` · `ante_stake_multiplier` · `ante_frozen_in_freespins` · `win_tier_bands`

All eight are driven in **both** directions by the synthetic-stream harness (37 checks: each must
catch its own defect and stay quiet otherwise). That harness found a real gap during development:
`ante_stake_multiplier` originally checked only the stake:base *ratio*, so a server that inflated
**both** kept a perfect 3:1 while silently tripling the payout base. It now also compares ante
rounds against non-ante rounds — which is why `ante-toggle` spins the same bet level both ways.

---

## 3. Coverage by module

| Module | Cases | Automated | Suite |
|---|---|---|---|
| Khởi tạo & Loading | 3 | 1 | `cr-core` |
| Bet & Denomination | 3 | 3 | `cr-core` |
| Balance | 5 | 5 | `cr-core`, `cr-auto-turbo` |
| History | 2 | 1 | `cr-core` |
| Paytable popup | 4 | 4 | `cr-core` |
| Last Bet | 6 | 4 | `cr-lastbet` |
| Auto Play | 8 | 8 | `cr-auto-turbo` |
| Turbo | 4 | 4 | `cr-auto-turbo` |
| Sound | 3 | 2 | `cr-ui-resilience` |
| Auto Reconnect | 8 | 5 | `cr-ui-resilience` |
| Cơ chế quay | 3 | 3 | `cr-auto-turbo` |
| Reel Glow | 2 | 2 | `cr-features` |
| Free Spin | 3 | 3 | `cr-features` |
| **Ante Bet** (incl. UI) | **8** | **7** | **`cr-ante`** |
| Random Multiplier | 2 | 2 | `cr-features` |
| Raining Wild | 3 | 3 | `cr-features` |
| Gun (P1) | 2 | 2 | `cr-features` |
| Multi-language | 4 | 4 | `cr-ui-resilience` |
| Tutorial | 4 | 3 | `cr-ui-resilience` |
| Replay | 9 | 0 | — |
| Promo | 3 | 0 | — |
| Compliance Setting | 8 | 0 | — |
| Last Bet | 6 | 3 | `cr-ui-resilience` |
| Dynamic Game Help | 10 | 3 | `cr-ui-resilience` |
| Popup Notification | 4 | 4 | `cr-auto-turbo`, `cr-ante` |

### The Ante Bet suite in detail (the feature this game turns on)

| Case | Scenario | Hard assert | AI assert |
|---|---|---|---|
| CR_049 | `ante-toggle` | control reached | `image:` control renders, no overlap |
| CR_050 | `ante-toggle` | hover happened | `image:` panel shows both stakes |
| CR_051 | `ante-toggle` | `ante_stake_multiplier`, `conservation`, ratio == server multiplier, **base unchanged** | `video:` bet amount rose, toggle changed |
| CR_053 | `ante-freespins` | ante not clickable in FS, `stake_zero_in_freespins` | `video:` ante/bet/auto locked |
| CR_054 | `ante-toggle` | bet amount restored | `video:` locked while spinning |
| CR_055 | `ante-freespins` | `ante_frozen_in_freespins`, ≥2 free spins | — (BO half manual) |
| CR_056 | `ante-freespins` | `ante_frozen_in_freespins`, `terminal_reached` | `video:` control returns after bonus |
| CR_109 | `ante-toggle` | control reached | `video:` enable/disable notifications |
| CR_052 | — | **not automated** — needs a build launched with Ante Bet = OFF | |

CR_055 and CR_056 are **half-automated and say so in their titles**: the run proves the value the
client holds does not drift across the bonus; it does not change BO config mid-bonus and reload.
That half stays manual. A green on these two is not the whole case.

---

## 4. The 38 not automated, and why

**Cannot be observed from inside the game (3)**
* `CR_001` `CR_002` — loading-bar behaviour. The bridge is only constructed *after*
  `GlobalContext.boot()` resolves, so by the time `window.__gameTestBridge` exists, loading is over.
  Needs a network-throttled Playwright capture outside the scenario engine.
* `CR_030` — audio muting on tab blur. The recording carries no audio and the bridge reports no
  mute state.

**Needs the BO / a second system (10)**
* `CR_013` (history on BO) · `CR_052` (Ante Bet = OFF build) ·
  `CR_084`–`CR_091` (each compliance flag changes what the build ships).
  `CR_090` (`bonusBuyAllowed = false`) is already this game's permanent state — `HAS_BUY_BONUS` is
  false and `buyBonus` always resolves to `null`.

**Needs seeded data (12)**
* `CR_072`–`CR_080` (Replay) — needs a replay URL for a specific past round. Buildable: the bridge
  exposes `IS_REPLAY_MODE` and the `replay` / `replayLanguage` popups. Needs a replay-link scenario
  plus a seeded round id.
* `CR_081`–`CR_083` (Promo) — `HAS_PROMO` is runtime-true only when the server sends
  `gameInfo.promo`. ⚠ The promo popups are **Promise-driven**: hiding one without firing its button
  hangs the game, so a promo scenario must click through, never `dismissPopups()`.
* `CR_097` (Last Bet of a Promo round) — same blocker.

**Not applicable to this game (1)**
* `CR_094` (reopening after a BOUGHT bonus) — Chaos Runner has no Buy Bonus, so there is no second
  way into the bonus to tell apart from CR_096a.

**Environment the harness cannot create (3)**
* `CR_035` (Wi-Fi A → Wi-Fi B handover) · `CR_038` (session expiry — wall-clock wait past the TTL) ·
  `CR_040` (slow/unstable network + duplicate-request detection — a CDP-level harness).

**Folded into one visual sweep (8)**
* `CR_100`–`CR_106` — each names one BO-toggled Game Help section. Rather than eight near-identical
  runs, `game-help` shoots every screenful once and `CR_099` enumerates what is actually present, to
  be read against the BO config in force. `CR_106` (the RTP value itself) needs the BO number to
  compare against.

---

## 5. Open questions for the first live run

1. **Capture a BET response.** Everything in §1.1 depends on it. Highest-value single action here.
2. **The autoplay count options are undocumented.** Neither source doc lists them; the bridge only
   exposes `autoplay:count:<i>` by index. The shared scenarios use index 4 as "infinite" from the
   reference build. Open the popup and record the real indices — until then a count-specific
   failure is a suspected mapping problem, not a game defect.
3. **Does the win tier measure against base bet or bet amount?** Undocumented, and the two differ by
   the ante multiplier, so every tier boundary moves under Ante Bet. `win_tier_bands` measures
   against the base stake; a violation on an ante round is this question surfacing, not necessarily
   a defect.
4. **The free-spin retrigger award amount.** Retrigger is confirmed to exist; how many spins it
   grants is documented nowhere. `freespin_retrigger_adds` asserts the *shape* only — tighten it
   once the number is known.
5. **`getGameInfo()` exists and carries `lastBet`; `isGameInfoCaptured()` does NOT.** Measured
   2026-09-18 on the first run of CR_092 — the Chaos bridge doc documents no `getGameInfo()` at all
   (it names `gameInfo.promo` and `gameInfo.betlines` in passing and nothing else), so **the doc is
   behind the build**:

   | | |
   |---|---|
   | `getGameInfo().lastBet` | present. `last_bet_present`, `stake_restored`, `restored_payout` and `bet_opens_at_last_stake` all passed — the restore itself behaves |
   | `isGameInfoCaptured()` | **missing** → `info_captured` reads `None`. **A bridge gap to file** |
   | `pending` | still unverified — CR_096a/b is the run that finds out |

   ⚠ The missing capture flag matters beyond a red assert: it is the only way to confirm
   `getGameInfo()` returns the PRE-START copy rather than a live read, and `applyLastBet()` nulls
   `lastBet` at Start on the Zeus family. On this build that cannot be confirmed — `last_bet_present`
   passing is evidence the read was not a post-Start one, but not proof. **Ask the Chaos team for
   `isGameInfoCaptured()`**, and until it lands the suite gates on `info_captured != False` (fails
   when the bridge SAYS the capture is missing, passes when it merely cannot answer).
6. **Payline count.** Not in either doc. `getPaylineCount()` reads `gameInfo.betlines` at runtime;
   the QA-confirmed `/20` divisor implies 20, but that was never confirmed directly.

---

## 6. Verifying without a browser

Both harnesses live in the session scratchpad and should be moved into the repo if this suite is
kept. They are how the work above was checked with no build available:

* **`verify_chaos.py`** — 37 checks driving every invariant with synthetic response streams in both
  directions, plus the unreadable-grid backstop and the envelope unwrap. All pass.
* **`validate_suites.py`** — parses all 5 suites and checks every `scenario:`, `stream:` invariant,
  `teardown:` and `params:` resolves, and that **every `{facts.X}` is actually exposed by that
  run's scenario**.

That last check is not optional bookkeeping. A typo'd fact resolves to the literal string `null` and
the predicate **quietly passes** — the exact vacuous green the framework exists to prevent. On first
run it caught **43** of them, every one in a reused shared scenario where the fact name had been
assumed rather than read. Two scenarios (`spin-inputs`, `turbo-modes`) turned out to expose no facts
at all, so their cases had no possible hard companion; both were given real ones
(`turbo-modes` now reads `getSpeedMode()` around each toggle, making "turbo really engaged" exact
for every game, not just Chaos).

Run both before trusting any change to these suites.
