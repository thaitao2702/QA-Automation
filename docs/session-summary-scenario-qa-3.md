# Session Summary 3 — The scenario engine, generalized: onboarding a NEW GAME

Continuation of `docs/session-summary-scenario-qa.md` (the response-driven prototype) and
`session-summary-scenario-qa-2.md` (scaling it across Mystic's 108 cases). **This session made the
suite multi-game**: the engine was split into MECHANISM and DIALECT, a second mechanism (Hold & Spin)
was added, and Zeus's 52 cases were rewritten onto it.

**Read this first if you are writing test cases for a new game.** It is the entry point; everything
else is linked from here.

---

## 1. The model in one page

Four ideas, and everything else follows from them.

**1. Drive from the SERVER RESPONSE, not client state.** A scenario clicks real buttons and reads the
BET response. Client-state getters are used only where the subject *is* the UI (a popup appeared, the
text is in Thai, turbo really engaged). This is what makes a test portable: the response contract is
the same shape across games; the UI never is.

**2. A scenario is a CLOSED BOX.** `run(ctx) -> ScenarioResult`. It knows how to *reach* a situation —
spin until a cascade win, drain a bonus, drop the network — and exposes what it reached. QA never
scripts clicks. One scenario play yields MANY case verdicts.

**3. Three assert kinds, three tiers.**

| Kind | Checked by | Failure |
|---|---|---|
| `stream:` | the response engine — exact | **red** (a real bug) |
| `video:` | AI over the recorded clip | **orange** (triage) — never red |
| `image:` | AI over a full-resolution screenshot, one call per shot | **orange** |

A scenario that could not reach its situation returns **unreached** → the case is **inconclusive**,
never a false pass. That distinction is the backbone of trusting the suite.

**4. MECHANISM vs DIALECT.** The engine is game-agnostic. A profile supplies what a build *calls*
things and which features it ships.

---

## 2. Where everything lives

| File | What |
|---|---|
| `scripts/scenario_engine.py` | the mechanism — `DriveContext` (drive primitives), 36 scenarios, 10 invariants, teardowns, binding |
| `scripts/game_profile.py` | the dialect — `SlotGame` → `CascadeGame` / `HoldSpinGame`, then per-game classes |
| `scripts/run_scenario.py` | the runner — URL/language/paytable setup, artifacts, verdicts |
| `scripts/ai_video.py` | the AI pass — clip trimming, montage, screenshot grading |
| `docs/game-bridge-contract.md` | **what a new game's bridge must expose** |
| `docs/scenario-assert-authoring.md` | the assert vocabulary spec |
| `docs/mahjong-coverage.md` · `docs/zeus-coverage.md` | worked coverage maps, 108 and 52 cases |
| `games/<game>/test-suites/*.yml` | the test cases |
| `games/mystic/bet-response.md` | an annotated real response + the rules derived from it |

Reference implementations: **Mystic** (cascade) and **Zeus** (hold & spin). When in doubt, copy the
closest suite and change the dialect.

---

## 3. Onboarding a new game — the flow

### Step 1 — check the bridge

Read `docs/game-bridge-contract.md`. Seven functions are REQUIRED; each optional one is
mapped to the cases it buys. `profile.api_check_js()` runs at load and names anything missing, so a
gap is reported up front instead of timing out later.

> If the build has no `window.__gameTestBridge` at all, stop. Nothing runs. Say so plainly rather
> than working around it — that has been the standing blocker on Mystic's dev build for weeks.

### Step 2 — capture real responses

Get one BET payload for each shape the game produces (a loss, a win, a bonus trigger, a bonus round).
Save them under `games/<game>/fixtures/bet-responses/` and write a `bet-response.md` like Mystic's.

**Do not skip this.** Every counting rule the engine applies is derived from those payloads. On
Mystic, the samples settled a question guesswork had got wrong twice (does a step paying two clusters
count once or twice?) and revealed that `multiplierLevel` is a config ladder, not `+1` per cascade —
which contradicted two test cases as written.

### Step 3 — write the profile

```python
# scripts/game_profile.py
class NewGame(HoldSpinGame):            # or CascadeGame
    name = "newgame"
    F_FS_CONTAINER = "freeSpinInfo"     # if the counters are nested
    F_FREE_SPINS = "freespins"
    BUTTONS = dict(SlotGame.BUTTONS, stake_up="nextBet", stake_down="previousBet")
    POPUPS  = dict(SlotGame.POPUPS, message="notification")
    HAS_BUY_BONUS = True
    HAS_SOUND_GETTERS = True

register(NewGame())
```

Capability flags matter: `HAS_ANTE_BET = False` makes the dependent case report *precondition not
met* (inconclusive) instead of failing. A feature the game does not have is not a defect.

If the game has no `gameState` field, override `game_state()` to derive one — Zeus reports its state
through the respin counter.

### Step 4 — map the test cases

Take the case list (xlsx/CSV) and, for each case, ask **"which existing scenario reaches that
situation?"** Most map to something that already exists: money, bets, autoplay, History, reconnect,
language, spin inputs are all mechanism-free.

Only write a new scenario when the *mechanism* is new. Zeus needed four
(`hold-spin-complete`, `hold-spin-full-grid`, `buy-bonus`, `sound-toggles`); everything else was
reuse.

### Step 5 — write the suites

Group by feature, one file each (`zz-core.yml`, `zz-holdspin.yml`, …). Header comment says what the
suite covers, what it forces, and what it deliberately leaves out.

### Step 6 — validate before running

The engine is verifiable without a browser. Every scenario constructs, every YAML resolves, every
invariant can be driven with synthetic response streams — see §6.

---

## 4. Writing a test case

```yaml
runs:
  - run:
      scenario: hold-spin-complete          # a registered closed box
      params: { cap: 200 }                  # constructor args (optional)
      paytable: { ref: fs-trigger3-countdown }   # BO forcing, BEFORE load (optional)
      language: th                          # re-mint the token in this language (optional)
      teardown: return-to-base              # after this run (optional)
      cases:
        - id: ZZ-30
          title: "3-8 Coins trigger Hold & Spin and award 3 respins"
          assert:
            - stream: holdspin_award                  # a named invariant
            - stream: "{facts.trigger_coins} >= 3"    # an inline predicate
            - video: { at: trigger, timestamp: true, prompt: ..., output_schema: ..., expected: ... }
      video:  { ... expected_by_case: { ZZ-30: {...} } }   # ONE AI call, graded per case
      image:  { ... expected_by_case: { ... } }            # ONE call PER SHOT
```

### Binding

`{res.*}` the key round · `{trigger.*}` / `{terminal.*}` · `{rounds[i].*}` · `{facts.*}` values the
scenario chose to expose · `{shot.label}` per-screenshot, in `image:` prompts only.

### The rule that matters most

**Every AI assert needs a hard companion that proves the run reached what the case claims to judge.**

```yaml
- stream: "{facts.language_count} == {facts.requested}"   # every language really launched
- image: { ... }                                          # ...now judge the text
```

Without it, a scenario that quietly did nothing produces a green case. Most of the debugging in
sessions 2–3 was closing exactly this kind of vacuous pass.

### Choosing a tier

- Response has the answer → `stream:`. Always prefer it. Zeus's whole Hold & Spin feature is exact
  because `coinPositions` / `coinCount` / `freespins` are all in the payload.
- Motion or timing → `video:` (`high_fps: true` for sub-second events, `timestamp: true` to get the
  ACTION TIMELINE lining marks up with a burned-in clock).
- Fine visual detail — text, glyphs, clipping → `image:`. A video assert grades a *downsampled
  montage*; a missing Burmese diacritic is simply not in those pixels.

---

## 5. The inventory today

**36 scenarios.** Base: `base-spin` `base-cluster-win` `boot-base` `change-bet`
`browse-denominations` `insufficient-balance`. Bonus: `fs-complete` `freespin-cascade`
`freespin-zero-balance` `reels-glow` · **hold & spin**: `hold-spin-complete` `hold-spin-full-grid`
`buy-bonus`. Auto: six variants. History: `spin-then-history` (hunts no-win / normal-win /
cascade-win in one pass) `freespin-then-history` `last-bet-before-rounds`. Input: `spin-inputs`
`turbo-modes` `toggle-turbo` `toggle-auto` `stacked-popups` `sound-toggles`. Resilience: four
reconnect/multi-tab. Language: `language-tour` `language-switch` `language-sweep`. `open-tutorial`.

**10 invariants.** `conservation` `freespins_monotonic` `stake_zero_in_freespins` `terminal_reached` ·
`holdspin_award` `holdspin_coins_held` `holdspin_reset_on_new_coin` `holdspin_countdown`
`holdspin_ends_correctly` `holdspin_payout_matches_coins`.

**2 teardowns.** `return-to-base` `deposit-10000`.

**2 profiles.** `mystic` (cascade) · `zeus` (hold & spin).

---

## 6. Verify without a browser

Everything below was how sessions 2–3 caught real bugs before any live run.

```python
# a fake page + injected responses drives a scenario end to end
se._await = fake_await                       # button rects
se.await_bet_response = fake_bet             # your scripted response stream
r = await se.HoldSpinComplete().run(ctx)
cases = rs.build_cases(run_doc, r)           # -> real verdicts
```

Always test the **failure** direction too. An invariant that never fires is worthless: feed it a
stream where a held coin vanishes, where the counter does not reset, where a language fails to
launch. Each should catch its own defect **and nothing else**.

Also check: every scenario constructs (`type(s)()`), every suite's scenarios and invariant names
resolve, and every `{facts.*}` a suite references is actually exposed. A typo'd fact silently becomes
the literal string `null` and the predicate quietly passes.

---

## 7. Traps worth knowing

- **`freeSpins` is REMAINING, not awarded**, and is not a loop variable. A max-win round returns to
  base with spins still on the clock. Terminate a drain on state.
- **Probe, never wait, for an absent control.** `waitForButtonClickable` on a button the build lacks
  burns its whole timeout — which destroyed the rapid-toggle burst in Mah_108 until `has_button()`
  (an instant `getButtonRect`) replaced it.
- **A fixed dwell is not a wait.** Opening a panel only *starts* its API fetch; dwell for the video,
  then poll for the content.
- **Dwell is 2000ms.** The montage samples one composite per second, so 2s is already two frames.
  Longer costs runtime and tokens on every dwell, and dwells multiply (languages × surfaces).
- **Random must be seeded**, and an unseeded draw must report the seed it picked. A sample you cannot
  replay is not a test.
- **DevMode forces ONE client-side spin.** Multi-round sequences (Hold & Spin, free spins) need a
  server-side BO paytable. If BO is unconfigured, SKIP the run — never run it unforced.
- **Verify a doc's claim before repeating it.** The contract listed `holdSpinStart/End` as buying
  Mah_022; the code never calls it. Grep first.

---

> **Continued in `docs/session-summary-scenario-qa-4.md`** — Zeus onboarded (Hold & Spin),
> plus the first live runs and what they corrected. Start there for current state.

## 8. State of play

| | |
|---|---|
| Mystic | 108 cases mapped, ~50 automated — `docs/mahjong-coverage.md` |
| Zeus | 52 cases mapped, 49 automated — `docs/zeus-coverage.md` |
| Suites | 112 cases across both games |

**Everything since mid-session-2 is LIVE-UNVERIFIED.** The deployed Mystic dev build ships without
the QA bridge (`window.__gameTestBridge` never appears with `&bridge=1`, polled 30s), so the work is
validated against fixtures, fake pages and synthetic streams. Zeus's suites were written from the v2
bridge doc, the mechanism doc and the old `raw.yml` — never run.

**First thing to do with a working build:** run one suite end to end and correct what reality
disagrees with. Known open questions are listed per game in the coverage docs — Zeus's `AUTO_COUNTS`,
its Buy Bonus price multiplier, and whether Mystic's history-detail pages per cascade step or per
cluster are all waiting on exactly that.
