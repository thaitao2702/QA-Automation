# Skill: /write-test

```yaml
name: write-test
version: "2.0"
domain: [game-qa, test-authoring, visual-ai-testing, bridge-integration]
depends_on: [init-knowledge, bridge-api]
description: |
  Generates executable YAML test suites for slot games using bridge
  injection and back-office paytable control for deterministic results,
  dual assertions (bridge query + Gemini visual AI, video-first), and
  constrained output_schema prompts. Tests go through a one-by-one QA
  approval loop before final YAML is written. Tests are presented for
  approval ONE AT A TIME and only written to YAML after each is
  individually confirmed — this loop is mandatory and is never skipped.

  Use when the user says "write tests," "create test cases,"
  "generate a test suite," "add tests for [mechanic]," "write YAML
  tests," or "test [game]" -- even if they don't say "write-test."
  Also triggers for "convert this test idea to YAML," "make this
  testable," and "I need tests for free spin / hold and spin / etc."

  Do NOT use for running tests (use /run-test).
  Do NOT use for onboarding a game (use /init-knowledge).
  Do NOT use for updating knowledge (use /update-knowledge).
```

**Usage:** `/write-test --game=<game-name> --suite=<suite-name> [--raw="<raw test description>"] [--scope="<topic filter>"]`

**Flags:**
- `--game` (required) — game name (e.g., `zeus`)
- `--suite` (required) — output filename stem (e.g., `suite-02-freespin`)
- `--raw` (optional) — QA-provided test description to refine into YAML steps (takes priority over generated tests)
- `--scope` (optional) — mechanic filter: `free spin`, `hold and spin`, `reconnect`, `buy bonus`, `auto spin`, `base game`, `core`

---

## Expert Vocabulary Payload

**Test Architecture:**
step-based test structure, sequential step IDs (every step including setup/teardown requires `id`), three lifecycle scopes (suite-level / group-level / test-level setup+teardown), test groups (`groups:` with once-per-group setup/teardown), single-test execution with group setup/teardown intact (`--test`/`--group`), unique suite-wide test ids, self-contained test state, test determinism via bridge injection AND back-office paytable control, paytable-grouped test cases (base game / free spin / etc.), deterministic server result, 21 supported action types only (navigate, wait_for_state, bridge_action, bridge_assert, capture, assert, script, go_offline, go_online, press_key, click, click_button, wait_ms, bo_paytable_ensure, bo_paytable_select, bo_paytable_update, bo_paytable_reset, wallet_balance, wallet_deposit, wallet_withdraw, wallet_set), server-side player-balance control via the wallet_* actions (force a balance to test balance-dependent rules; ALWAYS snapshot + restore the original in teardown)

**Bridge Integration:**
bridge action, real-coordinate button taps (`click_button` resolves a button's on-screen rectangle via the bridge's `getButtonRect`/`getAllButtonRects` geometry API and fires a genuine pointer event — the preferred way to press any on-screen button; method-invoking `bridge_action` is the last resort), tap-wrap pattern (`waitForButtonClickable` to wait the button is visible/enabled/un-covered → `click_button` → `wait_ms 300` settle so each action is a distinct, watchable moment in the recording), bridge injection (setDevModeResult), bridge method validation, state query (getAppState, getBalance), dev mode reset (resetDevMode), API surface coverage, missing method flagging, bridge assert schema (assert.bridge sub-block: method + expected only, no computed fields; the `bridge_assert` action additionally accepts an `args` list), saved variables (`save: <name>` on a step captures its result), bridge-owned deterministic assertions (`bridge_assert` with `args: ["$name"]` passes a saved value to a bridge function and compares its return to expected)

**AI Visual Assertions:**
dual assertion (bridge + AI), mode selection favoring video (image only when a single frame is unambiguous), game-context preamble (≤300 tokens), constrained prompt, output_schema, confidence gating (high/medium/low), reference screenshot (ref_screenshot for image mode), video trim (from_step/to_step for video mode), Gemini visual verification, saved-value prompt substitution (`{{name}}` injected into prompt/expected from an earlier `save:` for deterministic AI asserts)

**QA Workflow:**
grouped test plan artifact (`{suite}.plan.json`), case-vs-execution model, case consolidation into runs (one execution → many verdicts; fewest executions = minimal suite time), per-execution paytable decision + confirmation, plan-level overview gate (10a) then per-execution approval loop (approve/change/paytable/split/skip), compact execution presentation format, sub-agent paytable generation (from `base.json` + `paytable.md`), coverage report (cases + executions, replays saved), mechanic coverage matrix, ambiguity flagging (`!! AMBIGUITY`), asset gap detection

**State & Determinism:**
reachable state enumeration, deterministic test path, idle state baseline, clean teardown contract, state injection before assertion

---

## Supported Action Types Reference

**The definitive list of actions, their params, assert formats, and suite structure is in
`framework/schema_reference.md` + `framework/schema_reference.yml`** (auto-generated from the
framework source — read both in Step 1). Do **not** keep a copy of the action table here: always
use the action names and params from those files (they win on any conflict). For mapping a test
*intent* to the right action, use the table below.

*See "Deterministic Results via Back-Office Paytable" below for how to group test cases by paytable.*
*See "Saving Values and Asserting Them via the Bridge" below for the `save:` field + `bridge_assert` `args` pattern.*

### Common Intent → Correct Action

| What you want to do | WRONG (invented) | CORRECT |
|---|---|---|
| Press an on-screen game button (spin, auto, bet ±, buy bonus, popup confirm/cancel) | `bridge_action` invoking the method (`spin()`, `nextBet()`, `confirmBuyBonus()`); or a bare `click_button` with no readiness wait / settle | `waitForButtonClickable` → `click_button { name }` → `wait_ms 300` (see "Prefer real taps" below); `bridge_action` to invoke the method only as a last resort |
| Loop a bridge call until condition | `bridge_action_until` | Multiple `bridge_action` steps, or a `script` step |
| Read a bridge value without asserting | `bridge_query` | `bridge_action` (runner stores the return value) |
| Reload the game page | `reload` | `navigate` + `wait_for_state ready` + `click #start-button` + `wait_for_state idle` |
| Pause for N milliseconds | `wait` | `wait_ms` with `params: { ms: N }` |
| Assert on a value captured earlier | `script` with `assert_equal(steps[...])` | `save: <name>` on the producing step, then `bridge_assert` with `args: ["$name"]` calling a bridge function that judges it |
| Compare two captured values | `script` comparing `steps[...]` | `save:` both, then `bridge_assert` `args: ["$before", "$after"]` to a bridge function that compares them |
| Put a captured value into an AI prompt | hardcode it, or ask vaguely "is a value shown?" | `save: <name>`, then a `{{name}}` placeholder in the `ai` block's `prompt` / `expected` — replaced with the real value at run time |
| Verify a bridge value is shown on screen | Bridge-only assertion | `bridge_assert` + `capture` + AI assertion with the concrete value in the prompt |
| Force the player's server-side balance (e.g. test "can't bet when balance < bet") | a `setBalance` bridge call; `setDevModeResult` | `wallet_set { amount }` (reads current, applies the delta) — see "Manipulating Player Balance (Wallet)" |
| Restore the balance a test changed | a fixed reverse `wallet_deposit`/`withdraw` | snapshot with `wallet_balance save: orig` in setup, then `wallet_set { amount: "$orig" }` in the test's teardown |

**NEVER invent action type names.** If no supported action (see `schema_reference.yml`) fits, use `script` to run custom logic.

### Prefer real taps (`click_button`) over invoking the method

A real player presses buttons — they don't call `spin()`. **Emulate the tap.** The bridge's
on-screen geometry API (`getButtonRect` / `getAllButtonRects`, bridge-api.md §3.7) lets the
`click_button` action resolve a button's screen rectangle and fire a **genuine OS-level pointer
event** at its centre. That is the highest-fidelity gesture: it goes through the real canvas
hit-testing and the full UI path (sound, animation, button-state, input validation) exactly as a
player does — whereas invoking the method directly can skip those side-effects.

**Default to `click_button` for every on-screen button press.** Use the button `name`:

- **Main screen (always):** `spin`, `stop`, `auto`, `buyBonus`, `nextBet`, `previousBet`, `trophy`.
- **Popup elements (only while that popup is open):** bet — `bet:row:<i>`, `bet:plus`, `bet:minus`,
  `bet:close`; auto — `auto:package:<0..4>`, `auto:confirm`, `auto:cancel`, `auto:stop`, `auto:next`,
  `auto:prev`, `auto:stopAtWin`; buy-bonus — `buyBonus:package:<0..2>`, `buyBonus:buy`,
  `buyBonus:confirm`, `buyBonus:cancel`, `buyBonus:close`, `buyBonus:next`, `buyBonus:prev`.

**Wrap every tap: wait-clickable → tap → 300 ms settle.** A `click_button` only fires a pointer
event — it does not confirm the button is ready, and it does not block. Bracket each tap with three
steps:

1. **Before — wait until it's clickable.** `bridge_action { name: waitForButtonClickable, args: [<button>] }`
   (a §4 wait helper) polls until the button is visible, enabled, **and not covered by an overlay**
   (it folds in the hit-test) and then resolves, so the very next step can click it. Never tap a button
   you haven't waited for — it may be mid-animation, disabled, or behind a popup. (This is a *wait*,
   not the gesture — invoking it via `bridge_action` is correct; only the tap itself must be `click_button`.)
2. **Tap.** `click_button { name: <button> }`.
3. **After — settle 300 ms.** `wait_ms { ms: 300 }`. A short, deliberate pause so the screen is stable
   and **the moment is visible in the recording**. Without it, taps blur into one another and you lose
   track of what happened. This 300 ms is a *readability* pause — it does **not** replace waiting on the
   real outcome: if the tap starts something async (a spin, a popup, a transition), **also** add the
   right result wait (`wait_for_state`, `waitForSpinComplete`, …) after it.

```yaml
# wait until the spin button is genuinely clickable (visible, enabled, not covered)
- id: step-01
  action: bridge_action
  params: { name: waitForButtonClickable, args: [spin] }
# real tap on the spin button (resolved via getButtonRect)
- id: step-02
  action: click_button
  params: { name: spin }
# hold 300 ms so this tap is a distinct, watchable moment in the recording
- id: step-03
  action: wait_ms
  params: { ms: 300 }
# then wait for the actual result this tap started (the 300 ms pause does NOT cover it)
- id: step-04
  action: wait_for_state
  params: { state: idle, timeout_ms: 30000 }
```

**Invoke the method via `bridge_action` only as a LAST RESORT** — when a real tap is impossible or
not what's being tested:

- **No on-screen button / pre-gameplay-scene** — e.g. `startGame` (the geometry API throws
  `GameplayScene is not yet initialised` until the game has started, so the splash "START GAME" button
  is not reachable by `click_button`; use `bridge_action startGame`).
- **State setup / injection** — `setDevModeResult`, the `bo_paytable_*` actions, `resetDevMode`.
- **Reading or asserting values** — these are not gestures: `bridge_action` to read (then `save:`) and
  `bridge_assert` to check stay as bridge calls.
- **Capture / analyser APIs** — `startEventCapture`, `endEventCapture`, the Hold & Spin verifiers, etc.
- **The button isn't there** — `getButtonRect` returns `null` (popup closed / unknown) or `visible:false`
  (`click_button` fails fast in that case); if the gesture genuinely can't be tapped, fall back to the
  method and note why.

Converting **gestures** to `click_button` does **not** change reads/asserts — only genuine button
presses move from `bridge_action <method>` to `click_button { name: <button> }`.

**Scope: the test's interaction steps.** The "tap it, don't call it" rule targets the gestures the
test actually exercises (the ones that should be faithful and watchable in the recording). In
`setup`/`teardown` you're only establishing a precondition fast and deterministically — invoking the
method there is the expected **state-setup** path, not something to convert: e.g. `bridge_action spin`
to *apply* a `setDevModeResult`, or composite helpers like `startAutoSpin` / `confirmBuyBonus`. Keep
those as `bridge_action`.

---

## Deterministic Results via Back-Office Paytable

Bridge injection (`setDevModeResult`) forces outcomes *client-side*. For results that
must be **server-authoritative** (the real spin RNG resolved by the back-office against a
controlled paytable), drive the back-office (BO) instead, using the four `bo_paytable_*`
actions. **Source of truth: `docs/framework-runner-guide.md` → §19 "Back-Office Paytable
Control"** (read it before writing a BO suite). The low-level BO API lives in the
`framework/bo/` package (reference docs: `docs/bo-integration.md`) — do not point tests or
users at it. You only write the YAML steps; the runner resolves BO credentials/config
automatically from the `--env` it runs under.

**The model has three layers — every layer you set up forwards you MUST tear down in
reverse, so a forced result never leaks past the deterministic tests:**

1. **Ensure** — the automation paytable (`AutomationTest`) must exist on the master game
   so it is selectable per-agent. `bo_paytable_ensure` (idempotent; run once in suite setup).
   Nothing to undo (it only creates a reusable paytable).
2. **Select** — point the agent's game at that paytable so spins use it.
   `bo_paytable_select` in suite setup. **Always pair it with a `bo_paytable_reset` step in
   the suite teardown** — this is the required "set the paytable back to default" step that
   runs once **after all deterministic tests are done**, putting the agent back on the
   game-default paytable. Omit it and the agent stays pinned to `AutomationTest` after the run.
3. **Update content per group** — rewrite the paytable's content so each *group* of test
   cases gets its forced outcome (`bo_paytable_update` in the **group setup**). When a single
   group/test forces a result the *rest of the suite* must not inherit, restore the expected
   baseline in **that group/test's teardown** with another `bo_paytable_update` (back to the
   group's paytable). **Do not use `bo_paytable_reset` mid-suite** — it *un-selects*
   `AutomationTest`, so a later group's `bo_paytable_update` would edit content the agent no
   longer points at, and its spins would silently fall back to the game default.
   `bo_paytable_reset` belongs **only in the suite teardown** (the final restore-to-default).

### Group test cases by paytable

When a suite spans multiple mechanics, the determined result for "base game" differs from
"free spin". **Order the tests by paytable group**, and switch the paytable content once
per group — NOT once per test (creating/editing a paytable resubmits the whole game record
and is slow; doing it per test wastes time and BO load).

```
Suite setup        : bo_paytable_ensure (AutomationTest)  →  bo_paytable_select (AutomationTest)
  Group "base-game" (e.g. 10 tests)
    group setup    : bo_paytable_update (code=AutomationTest, paytable_ref=base-game)
    tests          : real spins resolve to the base-game forced result
    group teardown : (optional cleanup for the group)
  Group "freespin" (e.g. 10 tests)
    group setup    : bo_paytable_update (code=AutomationTest, paytable_ref=free-spin)
    tests          : real spins resolve to the free-spin forced result
Suite teardown     : bo_paytable_reset  →  agent back on the game default paytable
```

**Use the suite's `groups:` construct for the *shared* paytable.** When a cluster of tests
share the same forced result, put the `bo_paytable_update` in the **group `setup`** so it
runs **once** for the whole group instead of repeating per test. The runner guarantees a
group's setup/teardown runs around its tests even when only one test is selected
(`--test ZZ-15`), so the paytable is always correct.

**A test may still set its own paytable when it genuinely needs a different one.** The group
default is just the common case — an individual test can put its own `bo_paytable_update`
(or `bo_paytable_select`) in its own `setup`, and restore the group's paytable in its own
`teardown`. Don't *redundantly* re-set the same paytable every test (that's wasteful — see
anti-pattern #15), but per-test paytable changes are perfectly fine when the forced result
truly differs from the group's.

### Paytable content fixtures

`bo_paytable_update` / `bo_paytable_ensure` resolve content from
`games/{game}/paytables/{ref}.json` via `paytable_ref`, or accept an inline `paytable`
dict. One fixture per forced result (e.g. `base-game.json`, `free-spin.json`). You do not
hand-write these — **Step 6b generates each fixture with a sub-agent** that reads the base
paytable (`games/{game}/paytables/base.json`) and the paytable rule reference
(`games/{game}/paytables/paytable.md`) and tunes the weights to force the intended outcome.
If a fixture can't be produced from the documented rules, it's flagged
`# PAYTABLE UNRESOLVED: {ref}` in the coverage report rather than guessed.

### Canonical suite skeleton with paytable groups

```yaml
setup:                                  # suite-level — runs ONCE before all groups
  - id: setup-bo-01
    action: bo_paytable_ensure
    params: { paytable_code: AutomationTest }
  - id: setup-bo-02
    action: bo_paytable_select
    params: { paytable_code: AutomationTest }
  - id: setup-01
    action: navigate
    params: { url_script: "get_game_url" }
  - id: setup-02
    action: wait_for_state
    params: { state: ready, timeout_ms: 30000 }
  - id: setup-03
    action: bridge_action
    params: { name: startGame }

teardown:                               # suite-level — runs ONCE after all groups
  - id: teardown-bo-01                   # REQUIRED for any BO-paytable suite:
    action: bo_paytable_reset           # set the paytable back to default once the
                                        # deterministic tests are done (un-pins AutomationTest)

groups:
  # ----- Group: base game — shared paytable set ONCE for the group -----
  - id: base-game
    title: "Base game tests"
    setup:
      - id: gsetup-01                    # group's shared paytable
        action: bo_paytable_update
        params: { paytable_code: AutomationTest, paytable_ref: base-game }
    teardown: []                         # optional group cleanup
    tests:
      - id: ZZ-10
        title: "Base game — spin resolves to forced base-game result"
        steps: [ ... ]
        teardown:                        # per-test: return to clean idle baseline
          - id: teardown-01
            action: wait_for_state
            params: { state: idle }
      - id: ZZ-11
        title: "Base game — a test needing a DIFFERENT forced result"
        setup:                           # per-test override of the group paytable
          - id: setup-01
            action: bo_paytable_update
            params: { paytable_code: AutomationTest, paytable_ref: base-game-bigwin }
        steps: [ ... ]
        teardown:                        # restore the group's paytable for the next test
          - id: teardown-01
            action: bo_paytable_update
            params: { paytable_code: AutomationTest, paytable_ref: base-game }
          - id: teardown-02
            action: wait_for_state
            params: { state: idle }
  # ----- Group: free spin — paytable switched once at the group boundary -----
  - id: freespin
    title: "Free spin tests"
    setup:
      - id: gsetup-01
        action: bo_paytable_update
        params: { paytable_code: AutomationTest, paytable_ref: free-spin }
    tests:
      - id: ZZ-20
        title: "Free spin trigger resolves from the server"
        steps: [ ... ]
        teardown: [ ... ]
```

Tests that need no server-determined result can still go under a top-level `tests:` list
(ungrouped) — they run first, with no group setup/teardown. A suite may use `tests:`,
`groups:`, or both.

**Bridge injection vs BO paytable — which to use?** Prefer **bridge injection** for fast,
isolated client-state tests. Use **BO paytable** when the test must validate the real
server result path (the spin is resolved by the BO, not stubbed) or when QA explicitly asks
for determined server results. They compose: a BO-paytable suite still uses bridge
assertions + AI visual assertions to judge each spin's outcome.

---

## Manipulating Player Balance (Wallet)

Some tests need to control the player's **real server-side balance** — usually to drive a
balance-dependent rule, e.g. *"the spin/bet is disabled when the balance is below the bet
amount"*, a low-balance warning, or an exact-funds boundary. Four `wallet_*` actions change
the Playsite wallet through the same back-end the front-end uses. You write only the YAML —
the runner reads `PLAYSITE_API_URL` / `PLAYSITE_USER` / `PLAYSITE_PASS` from the `.env.{suffix}`
overlay (the same credentials the game-URL login uses) and resolves the currency automatically.

| Action | Params | Result (`save:`-able) | Use |
|---|---|---|---|
| `wallet_balance` | `currency` (opt) | numeric balance for that currency (or the full wallet list if none) | Read / snapshot the balance |
| `wallet_set` | `amount` (target), `currency` (opt) | updated `{currency, balance}` | **Set the balance to an exact value** — reads current, applies only the delta |
| `wallet_deposit` | `amount`, `currency` (opt) | updated `{currency, balance}` | Add a known amount |
| `wallet_withdraw` | `amount`, `currency` (opt) | updated `{currency, balance}` | Remove a known amount |

- **`amount` accepts a `"$saved"` reference** (same `$name` rule as `bridge_assert` `args`), so a
  value captured by an earlier `wallet_balance` `save:` can be fed back in — this is how you
  restore the original balance.
- **`currency`** defaults to `playsite.currency` in the game's `env.config.{suffix}.yml` (or the
  `PLAYSITE_CURRENCY` env var); pass it on a step only to override.
- **These are server calls — slow (login + HTTP).** Use them ONLY when the test genuinely needs
  server-side balance control. Never to fake a result that bridge injection could establish.

### ALWAYS restore the pre-test balance (mandatory)

Changing the balance is a side effect that **leaks into every later test** if not undone — a
later test could start from a balance you forced to 0 and fail in isolation or a different order
(leaky state, anti-pattern #2, but server-side and invisible until something downstream breaks).
**Any test that changes the balance MUST restore it to its pre-test value in that same test's
teardown.** Use `wallet_balance` to snapshot the original and `wallet_set` to restore it:
`wallet_set` reads the current balance and applies only the delta, so it lands back on the
original **even if the test changed the balance in between** — a fixed reverse `deposit`/`withdraw`
would not.

**Scope matters:** saved variables are **test-scoped** (one executor for that test's
setup → steps → teardown). Snapshot the original in the **test's own `setup`** and restore in the
**test's own `teardown`** — a `wallet_balance` saved in a *group* setup is NOT visible to a test's
teardown (different executor). So balance save/restore lives at the **test (or run) level**, never
group level.

```yaml
- id: ZZ-40
  title: "Spin/bet is disabled when the balance is below the bet amount"
  setup:
    - id: setup-01                       # snapshot the real balance FIRST (test-scoped var)
      action: wallet_balance
      save: orig_balance
    - id: setup-02                       # force the balance below the minimum bet
      action: wallet_set
      params: { amount: 0 }
    - id: setup-03                       # reload so the client reads the new balance
      action: navigate
      params: { url_script: "get_game_url" }
    - id: setup-04
      action: wait_for_state
      params: { state: ready, timeout_ms: 30000 }
    - id: setup-05
      action: bridge_action
      params: { name: startGame }
  steps:
    - id: step-01                        # bridge: the spin/bet must be disabled
      assert:
        bridge:
          method: isSpinEnabled
          expected: false
    - id: step-02
      action: capture
    - id: step-03                        # AI: the player SEES that betting is blocked
      assert:
        ai:
          mode: image
          ref_screenshot: step-02
          assets: [ui_elements.spin_button, ui_elements.balance_label]
          prompt: "This is a slot game at rest on the base game; the balance is 0, below the minimum bet, so betting must be blocked. Is the spin button visibly disabled / greyed out, and does the balance read 0?"
          output_schema:
            spin_disabled: { type: boolean }
            balance_zero: { type: boolean }
            confidence: { type: string, enum: [high, medium, low] }
          expected: { spin_disabled: true, balance_zero: true, confidence: high }
  teardown:
    - id: teardown-01                    # MANDATORY: restore the exact pre-test balance
      action: wallet_set
      params: { amount: "$orig_balance" }
    - id: teardown-02
      action: wait_for_state
      params: { state: idle }
```

To force a *specific* sub-bet balance instead of 0 (e.g. just under the minimum bet), use
`wallet_set { amount: 0.10 }`. The reload (`navigate → ready → startGame`) after forcing the
balance matters: the client reads the wallet at load, so a mid-session change only shows after a
refresh.

---

## Bridge Assert Schema

The `assert.bridge` **sub-block** (inside an `assert:` block) accepts **exactly 2 fields**:

```yaml
assert:
  bridge:
    method: getAppState      # string — bridge method name
    expected: "idle"          # any literal value — compared with equality
```

**No other fields are supported in `assert.bridge`.** The following are all INVALID and will fail validation:
- `expected_type`, `expected_min_count`, `expected_path` — not real fields
- `expected_match`, `expected_relative`, `expected_formula` — not real fields

The standalone **`bridge_assert` action** is the same equality check but additionally
accepts an optional `args` list (see next section) — that is the only place arguments
(including saved-variable references) are accepted.

---

## Runs — One Execution, Many Test-Case Verdicts

When several test cases share the **same execution** and differ only in what they assert
(e.g. enter a free-spin bonus once, then check entry count, coins-held, non-coin spin, and
countdown), use a `runs:` entry instead of repeating the bonus per case. A run executes one
`setup → steps → teardown` and emits a verdict per declared **case**, sharing one
video/timeline/events. See `framework/schema_reference.md` → "Runs" for the full schema.

Key rules:
- Declare `cases: [{id, title}, ...]`. Case ids are unique suite-wide (selectable via `--test`).
- Tag an assert step with `case:` (a case id or a list — a shared analyser feeds several cases).
- For AI asserts, use `expected_by_case: {<case-id>: {<expected subset>}}` so ONE model call
  is partitioned across cases by field (one call, many verdicts) — not one call per case.
- Every declared case must be covered by a `case:` tag or an `expected_by_case` key.
- A plain (untagged) step failing is fatal — all cases go red (the run couldn't produce data).

```yaml
runs:
  - id: fs-3coin-run
    cases:
      - { id: ZZ-32, title: "Coins held" }
      - { id: ZZ-33, title: "Only non-coin cells re-spin" }
    setup: [ ... ]
    steps:
      - id: step-01
        action: bridge_action
        params: { name: spinUntilBonus }          # plain step — no case
      - id: step-07
        case: [ZZ-32, ZZ-33]                       # shared analyser -> two cases
        action: bridge_assert
        params: { method: verifyHoldSpinCoinsHeld, args: ["$events"], path: valid, expected: true }
      - id: step-10                                 # ONE video AI call -> per-case verdicts
        assert:
          ai:
            mode: video
            video_trim: { from_step: step-01, to_step: step-09 }
            output_schema: { coins_held: {type: boolean}, only_noncoin_cells_spin: {type: boolean}, confidence: {type: string} }
            expected_by_case:
              ZZ-32: { coins_held: true, confidence: high }
              ZZ-33: { only_noncoin_cells_spin: true, confidence: high }
    teardown: [ ... ]
```

Normal `tests:` are unchanged — use a run only when cases genuinely share one execution.

## Saving Values and Asserting Them via the Bridge

When an assertion depends on a value produced earlier in the test (a payout, a balance,
a reel grid), capture it and let the **bridge** judge it. This keeps the verdict
deterministic — the comparison logic lives in the game bridge, not in the test runner —
and avoids `script`/JS entirely.

**1. Save** — add `save: <name>` to any result-producing step (typically `bridge_action`).
It stores that step's return value into a per-test variable (scoped to one test:
setup → steps → teardown).

**2. Assert via the bridge** — use the `bridge_assert` action with an `args` list. Each
element is a literal, **except** a string `"$name"` which resolves to the saved variable.
A `$name` may appear at any position; use `"$$"` to escape a literal leading `$`. The
named bridge function receives the resolved arguments and its return is compared to
`expected`.

```yaml
- id: step-01
  action: bridge_action
  params: { name: getBalance }
  save: balanceBefore                 # vars['balanceBefore'] = result

- id: step-05
  action: bridge_action
  params: { name: getLastPayout }
  save: payout                        # vars['payout'] = result

# Single saved value → bridge function judges it
- id: step-06
  action: bridge_assert
  params:
    method: isWinningPayout           # bridge.isWinningPayout(payout)
    args: ["$payout"]
    expected: true

# Saved value + literals, var at any position
- id: step-07
  action: bridge_assert
  params:
    method: isPayoutInRange           # bridge.isPayoutInRange(payout, 0, 100)
    args: ["$payout", 0, 100]
    expected: true

# Compare two saved values via a bridge function
- id: step-08
  action: bridge_assert
  params:
    method: balanceDecreased          # bridge.balanceDecreased(balanceBefore, balanceAfter)
    args: ["$balanceBefore", "$balanceAfter"]
    expected: true
```

**Requirements & caveats:**
- The bridge must actually expose the assertion function (`isWinningPayout`,
  `isPayoutInRange`, etc.). Validate it against the bridge API surface like any other
  bridge method (anti-pattern #1). If it is missing, flag it to QA with a suggested
  signature — do not invent it silently.
- A `"$name"` that was never saved is a hard error at runtime ("variable 'name' … was not
  saved by an earlier step"), not a silent pass.
- `args` values are passed exactly as JSON — numbers, strings, booleans, arrays, and
  objects (e.g. a saved 3×3 grid) all serialise correctly.
- With **no** `args`, `bridge_assert` calls the plain getter (e.g. `isSpinEnabled`) — the
  existing behavior is unchanged.

**3. Inject a saved value into an AI prompt (`{{name}}`)** — when the captured value also
appears on screen, embed it in the AI assertion so the model confirms a **known** value
instead of judging a fuzzy one. A `{{name}}` token in an `ai` block's `prompt` (and in any
string inside `expected` / `expected_by_case`) is replaced **at run time** with the value an
earlier step saved via `save: name`. This makes AI asserts deterministic and is the way to
satisfy anti-pattern #13 when the value is only known at run time (read from the bridge), not
a literal you can hardcode while authoring.

```yaml
- id: step-02
  action: bridge_action
  params: { name: getDisplayBet }       # the formatted string the player sees, e.g. "1.00"
  save: bet
- id: step-03
  action: capture
- id: step-04
  assert:
    ai:
      mode: image
      ref_screenshot: step-03
      prompt: "Does the bet label show {{bet}}?"   # -> "Does the bet label show 1.00?"
      output_schema: { matches: { type: boolean }, confidence: { type: string, enum: [high, medium, low] } }
      expected: { matches: true, confidence: high }
```

- **Pick the display-shaped saved value.** Save `getDisplayBet` (formatted `"1.00"`) for a
  label check, not `getBetAmount` (numeric `1`) — the token becomes `str(value)`, so save
  whatever matches what the player sees.
- A `{{name}}` whose variable was never saved **fails the step at execution** (not at the
  deferred AI stage) — same hard-error contract as a bridge `"$name"`.
- Works in `mode: image` and `mode: video`, and in an `agent_assert` `check:` claim.
- Still pair it with the bridge: `bridge_assert` checks state, the AI checks what the player
  **sees** (Rule 8 / anti-pattern #13).

---

## Anti-Pattern Watchlist

### 1. Invented / Phantom Element (bridge method, action type, or assert field)
**Detection:** A step uses something not in the authoritative reference — any of:
- a **bridge method** not in the bridge API surface (`bridge-api.md` / game-knowledge.md), OR
- an **action type** not in `schema_reference.{md,yml}` → Supported Action Types (e.g. `bridge_action_until`, `bridge_query`, `reload`, `wait`, `wait_for_condition`), OR
- an **`assert.bridge` field** other than `method` + `expected` (e.g. `expected_type`, `expected_relative`, `expected_formula`, `expected_match`, `expected_path`, `expected_min_count`).

**Why it's dangerous:** Each fails at validation or runtime — the suite is rejected or the test errors ("method not found" / unknown action / "required field 'expected' is missing") and a whole test cycle is wasted.
**Resolution:** Validate every bridge method against the bridge API surface, every `action` against `schema_reference.yml` → Supported Action Types, and keep `assert.bridge` to `method` + `expected` only. IF a bridge method is genuinely needed but missing: flag it to QA with a suggested signature — never invent it silently. To assert on a captured value, `save:` it and pass it to a bridge function via the `bridge_assert` action's `args: ["$name"]` (see "Saving Values and Asserting Them via the Bridge"). If no action fits, use `script` (last resort).

### 2. Leaky Test State
**Detection:** A test's setup block assumes the game is already in a specific state (e.g., "in bonus") without explicitly establishing it via bridge injection.
**Why it's dangerous:** Test passes when run after a specific predecessor but fails in isolation or different order.
**Resolution:** Every test must reach its starting state from idle. Setup block must include bridge injection to configure the exact state needed.

### 3. Vague AI Prompt
**Detection:** AI assertion prompt is open-ended: "What do you see?", "Describe the screen," "What is happening?" No output_schema constraining the response. OR the prompt jumps straight to the question with **no game-context preamble**, so the model has to guess what game/scene it is looking at.
**Why it's dangerous:** Gemini returns free-form prose. No structured field to compare against expected. Assertion is unjudgeable. Without context, the model also misreads the scene (e.g. judges a base spin as a bonus).
**Resolution:** Begin the prompt with a short **game-context preamble** (≤ 300 tokens — game/theme + what is happening in this clip/screenshot), THEN ask closed-ended question(s) (Rule 4). Always include output_schema with typed fields and a confidence enum, and an expected block matching the schema.

### 4. Asset Overload
**Detection:** an AI assert loads the whole pack (`asset_groups: [symbols, ui_elements]`, or no selection at all) when only one or two assets are relevant to the prompt.
**Why it's dangerous:** Floods the AI context with irrelevant reference images, reducing accuracy and increasing latency/cost.
**Resolution:** Select the **smallest** group/item set the AI needs via `assets: ["{group}.{name}"]` (or `asset_groups:` for a whole section). "Is the wild symbol in the center?" needs only `assets: [symbols.wild]`, not the entire symbol set. References are now validated at suite load, so a typo (`symbols.wld`) fails fast.

### 5. Silent Skip
**Detection:** A test or assertion is dropped from the output without flagging the reason to QA.
**Why it's dangerous:** QA believes the mechanic is covered when it isn't. Gaps appear only at review time.
**Resolution:** Every omission must be flagged with a reason and presented in the approval loop (Step 5) or coverage report (Step 7).

### 6. Schema/Expected Mismatch
**Detection:** The `expected` block contains fields not present in `output_schema`, or vice versa.
**Why it's dangerous:** Runtime assertion engine can't match the response to expectations. Silent pass or cryptic error.
**Resolution:** Every field in `expected` must exist in `output_schema` with matching type. Validate before writing.

### 8. Bridge-Only Test
**Detection:** A test has one or more bridge assertions but zero AI visual assertions. The test verifies state programmatically but never confirms what the player actually sees.
**Why it's dangerous:** Bridge state can be correct while the UI is broken — invisible symbols, missing animations, wrong labels, overlapping elements. A bridge-only test gives false confidence that the player experience is correct.
**Resolution:** For every bridge assertion, ask "does this state change have a visible effect?" If yes, add an AI assertion. If the test truly has no visual component (rare), document why with a comment: `# No AI assertion: {reason}`.

### 11. Wrong Mode Selection (defaulting to image)
**Detection:** `mode: image` is used for an assertion that involves a transition, animation, or sequence (e.g., "did the reels spin?", "did the popup animate in?", "did auto-spin run?"), or where it is not certain a single frame is unambiguous. Image is the *default choice* in the suite rather than the deliberate, confident exception.
**Why it's dangerous:** A single end-state frame can't verify the sequence occurred correctly — a frozen reel and a settled reel look identical, a missing animation is invisible. Image gives false confidence; video is more precise because it sees how the game reached the state.
**Resolution:** Default to `mode: video`. Choose `mode: image` only when you are confident one screenshot fully and unambiguously captures the assertion and nothing about the transition matters (static popup text, a value at rest the bridge already confirmed). When in doubt, use video.

### 13. Vague AI Expected Values
**Detection:** AI assertion asks "Is a bet amount displayed?" or "Is a balance shown?" without specifying the concrete expected value, even though the test knows the exact value (from bridge data, game config, or setup injection).
**Why it's dangerous:** The AI confirms *something* is displayed, but not that the *correct* value is displayed. A broken UI showing "999999" instead of "0.20" would pass. The AI assertion becomes useless validation theater.
**Resolution:** When the test knows the expected value, embed it in the prompt and expected block. Instead of `"Is a bet amount displayed?"` → `"Is the bet amount displayed as 0.20?"` with `expected: { bet_value: 0.20 }`. The AI assertion must verify the **same specific value** that the bridge assertion checks. **When the value is only known at run time** (read by an earlier step rather than a literal you know while authoring), save it and inject it with a `{{name}}` placeholder — e.g. `save: bet` then `prompt: "Is the bet shown as {{bet}}?"` (see "Saving Values …" §3). Never leave the prompt vague when the value can be pinned, literally or via `{{name}}`.

### 14. Script for Cross-Step Comparison (last resort)
**Detection:** A `script` step asserts across steps with JS, e.g. `assert(steps['step-05'].result > 0, ...)` or `assert_equal(steps[...], steps[...])`.
**Why it's discouraged:** `script` runs JavaScript in the browser page (`page.evaluate`) with `steps`, `vars`, `bridge`, `assert(cond,msg)`, and `assert_equal(a,b)` injected — so this *works*, but it buries verdict logic as ad-hoc JS in the test file, where it is non-obvious, unvalidated, and easy to get subtly wrong (JS truthiness, `==` vs `===`, async). The framework's deterministic path keeps that logic in the game bridge instead.
**Resolution:** Prefer `save:` + the `bridge_assert` action with `args: ["$name"]` calling a bridge function that owns the comparison (see "Saving Values and Asserting Them via the Bridge"). Use `bridge_assert` with a literal `expected` for independent checks. To confirm a value on screen, use `bridge_action` → `capture` → AI assertion with the concrete value. Reach for `script` only when no bridge function fits and the logic is genuinely test-specific — it is a last resort, not the default.

### 15. Redundant Paytable Switching
**Detection:** A `bo_paytable_update` (or `bo_paytable_ensure`/`bo_paytable_select`) step **re-sets the *same* paytable** in every test's setup for a cluster of tests that share one forced outcome — instead of setting it once in the group's `setup`. (A per-test `bo_paytable_update` that sets a genuinely *different* forced result for that test is NOT this anti-pattern — that's legitimate.)
**Why it's dangerous:** Editing a paytable resubmits the entire master-game record on the BO — slow and load-heavy. Re-setting the identical paytable per test multiplies that cost for no benefit and can hit rate limits or token-refresh churn.
**Resolution:** Put the **shared** paytable in the **group `setup`** (runs once for the group). `bo_paytable_ensure` + `bo_paytable_select` go in **suite** setup (run once); `bo_paytable_reset` goes in **suite** teardown. Only add a per-test `bo_paytable_update` when that test needs a *different* forced result than its group — and restore the group's paytable in that test's teardown. See "Deterministic Results via Back-Office Paytable".

### 16. BO Paytable When Bridge Injection Suffices
**Detection:** A test uses the `bo_paytable_*` actions to force an outcome that `setDevModeResult` (bridge injection) could establish client-side, with no requirement to exercise the real server result path.
**Why it's dangerous:** BO calls are slow (network + token refresh + full game-record resubmit on content edits) and require BO credentials. Using them where bridge injection works makes the suite slower, flakier, and dependent on BO availability for no added coverage.
**Resolution:** Default to **bridge injection** for client-state determinism. Reach for `bo_paytable_*` only when the test must validate the **server-authoritative** result (the spin resolved by the BO, not stubbed) or QA explicitly asks for determined server results.

### 7. Ambiguity Burial
**Detection:** A value in a step (e.g., re-spin count, state string, trigger threshold) is assumed without documentation support and no `!! AMBIGUITY` flag.
**Why it's dangerous:** Test encodes a guess as a fact. Fails against the real game, and nobody knows which value to fix.
**Resolution:** IF docs are unclear about a value: use the most common default for the mechanic, flag with `!! AMBIGUITY: {what's unclear}. Assumed: {value}. Confirm with game dev.`

### 17. Batched or Skipped Approval Loop
**Detection:** In the **Step 5 per-test detail loop**, multiple (or all) tests' **full detail** (steps/assertions) are presented in a single message, or the YAML is written before each test received an individual `yes`/`skip`. The plan file jumps from all-`planned` straight to a written suite with no per-test status updates in between. (The Step 3 / 10a **plan overview** — all tests as short one-line summaries for plan-level approval — is separate and expected; it is NOT this anti-pattern.)
**Why it's dangerous:** QA loses the chance to correct each test (paytable choice, assertions, ambiguity flags) before it is encoded. Errors then surface only at run time, wasting a full test cycle, and the QA's intent is silently overridden.
**Resolution:** Present exactly ONE test, wait, update its plan status, then move to the next (Step 5). Treat skipping the loop as a defect even under user time-pressure. An ambiguous "just write it / go ahead / don't bother" is NOT a waiver — confirm explicitly ("skip per-test approval and write all N at once, or review one by one?") and only batch on an explicit yes. A message about scope or sources (e.g. "ignore the old suite") never waives the loop.

### 12. Nested File Structure
**Detection:** The generated YAML file nests `game`, `tests`, `setup`, `teardown`, or `config` inside a `suite:` object instead of placing them at the root level. OR `suite` is written as a dict/object instead of a plain string.
**Why it's dangerous:** The suite runner validates top-level schema immediately. A nested structure fails with "required field 'game' is missing" and "expected str, got dict" — zero tests execute, the entire run is wasted.
**Resolution:** `suite` is always a string title. `game`, `version`, `config`, `setup`, `teardown`, `tests` are always root-level keys. Refer to `framework/schema_reference.md` / `framework/schema_reference.yml` (read in Step 6).

### 18. Unrestored Player Balance
**Detection:** A test changes the player balance with `wallet_set` / `wallet_deposit` / `wallet_withdraw` but does **not** restore the original value in its own teardown — or it "restores" with a fixed reverse `deposit`/`withdraw` instead of snapshotting the original and `wallet_set`ting it back. Also: the snapshot `wallet_balance save:` lives in a *group* setup (out of scope for the test's teardown vars) rather than the test's own setup.
**Why it's dangerous:** The forced balance (e.g. 0) leaks into every later test — they start from the wrong balance and fail in isolation or a different order (the server-side, invisible form of leaky state, anti-pattern #2). A fixed reverse op also fails to restore if the test itself changed the balance in between (spent on a bet, won a payout).
**Resolution:** Snapshot the real balance with `wallet_balance save: orig_balance` in the **test's own `setup`**, and restore it with `wallet_set { amount: "$orig_balance" }` in the **test's own `teardown`** (test-scoped vars — never group level). `wallet_set` reads the current balance and applies the delta, so it always lands back on the original. See "Manipulating Player Balance (Wallet)".

---

## Behavioral Instructions

### Step 1 — Load Knowledge Inputs

1. **Load the processed knowledge from `games/{game}/docs/final/`** — the structured output of
   /init-knowledge and the authoritative description of the game:
   - `games/{game}/docs/final/game-knowledge.md` — game overview, symbols, states, the bridge
     surface, and the mechanics (shared + custom, each shared mechanic carrying its
     Matches/Deviates comparison). **This is the primary source.**
   - `games/{game}/docs/final/paytable.md` **if present** — symbol payouts, special/bonus pays,
     and the paytable rules. Two uses: (1) embed these concrete values in bridge + AI assertions;
     (2) it is the reference for **forcing a deterministic bet/spin result** — which paytable
     values to set so a spin resolves to a known outcome (the basis for the forced-result
     paytables driven through the `bo_paytable_*` flow / generated in Step 6b).
2. Also read:
   - `games/{game}/docs/assets/assets.yml` — asset manifest. Top-level sections  are **groups**; each key under them is an **item**. Reference an AI assert with `asset_groups: [<group>]` (whole section) or `assets: ["<group>.<name>"]` (specific items). The bare-name `ref_assets` field is deprecated.
   - `framework/schema_reference.md` and `framework/schema_reference.yml` — **auto-generated**
     reference of supported actions, params, assert formats, and suite structure. Source of truth
     for what the framework accepts; never hardcode action types or field names.
   - `games/{game}/bridge-api.md` **if present** — bridge method signatures (game-knowledge.md
     may also describe the bridge surface).
3. **Load only the shared-mechanism files relevant to THIS game — never load all of them.**
   Read the confirmed mechanics in game-knowledge.md §4, and for each confirmed **shared**
   mechanic read its `shared-mechanisms/{name}.md` for the canonical pattern. `--scope` narrows
   further (load only the scoped mechanic). Do NOT load shared-mechanism files for mechanics this
   game does not have; **custom** mechanics have no shared file — use game-knowledge.md's description.
4. IF `games/{game}/docs/final/game-knowledge.md` is missing: output `"No game knowledge found. Run /init-knowledge --game={game} first to generate games/{game}/docs/final/game-knowledge.md."` and STOP.
5. IF `framework/schema_reference.md` does not exist: output `"Schema reference not found. Run: python scripts/generate_schema_reference.py"` and STOP.
6. Bridge method names are validated against the bridge API surface (game-knowledge.md and/or `games/{game}/bridge-api.md`). IF no bridge API documentation is available in either place: output `"Bridge API documentation required — document the bridge surface in games/{game}/docs/final/game-knowledge.md or add games/{game}/bridge-api.md."` and STOP.

### Step 2 — Determine Scope and Test Sources

5. IF `--scope` provided: filter to only the matching mechanic sections and shared mechanism file.
6. IF no `--scope`: generate tests covering all confirmed mechanics in game-knowledge.md.
7. IF `--raw` provided: use QA's raw description as the primary test case. Refine into YAML steps using knowledge files for bridge method names, state transitions, and assertions. Do not replace QA's intent — only structure it.
8. IF no `--raw`: generate from knowledge files and canonical patterns in each shared mechanism's "Canonical Test Patterns" section. Adapt patterns to use concrete bridge methods from the bridge API surface and add AI visual assertions that verify the visible outcome. Every canonical pattern must be augmented with at least one AI visual assertion — do not output bridge-only tests.

### Step 3 — Plan the Test Set (write the plan artifact)

**Vocabulary (used from here on).**
- **case** = one desired verdict (what used to be called a standalone "test"); has a suite-unique
  id (e.g. `ZZ-30`), a title, and assertions.
- **execution** = one `setup → steps → teardown`. Either a **run** (≥2 cases proven by the SAME
  execution → a `runs:` block) or a **standalone test** (a single case with its own execution →
  a `tests:` entry).
- **Goal of planning: the FEWEST executions** (minimal suite wall-clock) while every case keeps an
  independent verdict. Consolidating cases that share one execution is the whole point of 10a.

9. **Build the raw set of cases first** and **write it to a real file** —
   `games/{game}/test-suites/{suite}.plan.json` — so every later step (the plan review, the
   approval loop, the final YAML write, the paytable generation) reads a concrete artifact
   instead of in-memory state. At this stage just **enumerate every case** (id, title, source,
   mechanism, bridge_methods, ai_assertions) — grouping, paytable, and **run consolidation** are
   decided and confirmed in 10a. The plan file uses this schema (each group, and `ungrouped`, holds
   both `runs:` and `tests:`; the run/group/paytable fields are populated in 10a):

   ```json
   {
     "suite": "<human-readable title>",
     "game": "{game}",
     "groups": [
       {
         "id": "fs-trigger3",
         "title": "Free spin — 3-coin trigger",
         "paytable": { "ref": "fs-trigger3", "forced_outcome": "<what this group's paytable forces, e.g. 3-coin Hold&Spin bonus>" },
         "runs": [
           {
             "id": "fs-3coin-run",
             "title": "One bonus play -> 3 verdicts",
             "paytable_ref": "fs-trigger3",
             "needs_own_paytable": false,
             "shared_flow": ["spinUntilBonus", "playOut", "waitIdle", "endCapture", "filterEvents", "AI(video)"],
             "cases": [
               { "id": "ZZ-30", "title": "3 free spins granted",
                 "asserted_by": ["step-02 bridge getFreeSpins==3"], "status": "planned" },
               { "id": "ZZ-32", "title": "coins held across respins",
                 "asserted_by": ["step-05 analyser", "step-07 AI coins_held"], "status": "planned" }
             ]
           }
         ],
         "tests": [
           {
             "id": "ZZ-31",
             "title": "9-coin trigger -> 9 spins",
             "source": "<canonical pattern name or --raw>",
             "mechanism": "<shared mechanism or none>",
             "bridge_methods": ["spinUntilBonus", "getFreeSpins"],
             "ai_assertions": [{ "mode": "video", "target": "counter" }],
             "needs_own_paytable": false,
             "paytable_ref": "fs-trigger3",
             "own_execution_reason": "different trigger count — cannot share the 3-coin run",
             "status": "planned"
           }
         ]
       }
     ],
     "ungrouped": [
       { "id": "ZZ-01", "title": "...", "...": "...", "needs_own_paytable": false, "paytable_ref": null, "status": "planned" }
     ]
   }
   ```

   **Run fields:** `id`, `title`, `paytable_ref`, `needs_own_paytable`, `shared_flow` (the step
   outline all cases share), `cases[]` — each case `{ id, title, asserted_by[], status }` where
   `asserted_by` names the step/assert that produces that case's verdict (a `case:` tag, or an
   `expected_by_case` field for a partitioned AI call). **Standalone test fields:** `id`, `title`,
   `source`, `mechanism`, `bridge_methods`, `ai_assertions`, `needs_own_paytable`, `paytable_ref`,
   `own_execution_reason` (why it can't join a run), `status` (`planned` | `approved` | `skipped`).
   Group fields: `id`, `title`, `paytable.ref` + `paytable.forced_outcome`. **Case ids are unique
   suite-wide** (selectable via `--test`). This file is the **source of truth** for Steps 5–6;
   update it as the plan changes (do not keep a separate mental copy).

10. Validate every bridge method against the bridge API surface (anti-pattern #1). Flag any test requiring a missing method — do not silently drop it.

10a. **Group by feature + paytable, consolidate cases into runs, show the whole plan, then update the plan file.**

   **(a) Group + decide paytable.** Cluster cases that share a forced server result under one
   group (by feature / paytable). For each case ask: *does it need a server-determined result* (a
   real spin resolved by the BO against a controlled paytable), or is bridge injection enough?
   - **Bridge-injection only** → `ungrouped`, `needs_own_paytable: false`, `paytable_ref: null`.
   - **Shares a forced outcome with other cases** → that group; `paytable_ref` = the group's paytable.
   - **Needs a forced outcome different from its group's** → `needs_own_paytable: true` with a
     distinct `paytable_ref` (e.g. `base-game-bigwin`), applied in its own setup and restored in
     its teardown (see anti-pattern #15).

   Order groups so each is contiguous. Suite setup gets one `bo_paytable_ensure` +
   `bo_paytable_select`; each group's setup gets one `bo_paytable_update`; suite teardown gets one
   `bo_paytable_reset`. If every case uses bridge injection only, skip the BO layer and write only
   `ungrouped`.

   **(a2) Consolidate cases into runs — the time-saver.** Within each group, **merge cases that can
   be proven from ONE `setup → steps → teardown`** (same paytable + same reachable-state path,
   differing only in *what they assert*) into a single **run**. The classic case: enter a bonus
   once, then read entry-count, coins-held, non-coin respin, and countdown — four verdicts, one
   bonus play. **Prefer a run whenever ≥2 cases share an execution** — the saving is largest for
   slow executions (bonus plays, long sequences). Keep a case as a **standalone test** only when its
   execution genuinely differs (different forced outcome/bet/trigger, or its assertion would disturb
   the shared timeline the others depend on) — record `own_execution_reason`. In a run, steps that
   **all** cases depend on stay **untagged** (an untagged plain step failing fails the whole run —
   the data couldn't be produced); each case-specific check carries a `case:` tag (or an
   `expected_by_case` field for one partitioned AI call). Record runs/tests in the plan file (schema
   in item 9).

   **(b) Show the WHOLE plan to the user** — the deterministic tree below (group → paytable →
   execution → step flow → which case each assert proves). This is a **PLAN-level gate**: the user
   approves the set, grouping, paytables, and **run consolidation**, or asks to change it. (The
   detailed, per-execution review happens later in Step 5 — this overview does NOT replace it.)

   ```
   === Test Plan for {game} / {suite} ===
   Goal: fewest executions — cases sharing one setup→steps→teardown are merged into one run.

   GROUP {group-id}   paytable: {ref}  (forced: {forced_outcome})

     RUN {run-id}  (1 execution -> {K} verdicts)
       flow: {step-01} -> {step-02} -> … -> {step-NN}
       {ZZ-a} "{title}"   <= {step-02 bridge getX==3}
       {ZZ-b} "{title}"   <= {step-05 analyser} + {step-07 AI <field>}

     TEST {ZZ-c} "{title}"  — own execution ({own_execution_reason, e.g. different trigger/bet})
       flow: {step-01} -> {step-02} -> assert {…}

   UNGROUPED (bridge injection)
     RUN/TEST … (same shape)

   Summary: {C} cases -> {E} executions ({C-E} replays saved).
   Approve, or change (merge/split a run, move a case to/from a run, regroup, change a paytable or forced outcome, adjust coverage).
   ```

   **(c) Wait for the user.** Apply their changes (merge/split a run, move a case, regroup, change
   paytables/forced outcomes, adjust coverage) and **update the plan file** so it reflects the
   approved runs/tests before moving on to Step 4/5.

### Step 4 — AI Reasoning Rules (Apply to Every Test)

Apply all 7 rules when constructing each test case.

**Rule 1 — Enumerate Reachable States.**
Before writing assertions for any action step, enumerate all possible outcomes in your thinking (not in YAML output). The test must use bridge injection to make the path deterministic. IF no injection method exists for this state: flag to QA.

**Rule 2 — Video vs Image Mode.**

**Default to `mode: video`** — video is more precise: it sees the whole sequence (how the game reached the end-state), not just a single frame, so it catches missing animations, wrong transitions, and "frozen vs running" cases an end-state screenshot cannot. Use `mode: image` **only when you are confident a single screenshot fully and unambiguously captures the assertion** and nothing about *how* the game got there matters.

**Decision process — ask this question for each AI assertion:**
> "Am I certain one end-state screenshot is enough to judge pass/fail, with no dependence on the transition that produced it?"
> - **YES, confident → `mode: image`** with `ref_screenshot`
> - **Any doubt → `mode: video`** with `video_trim`

**Use `mode: video` (default) when:**
- The assertion involves any **transition, animation, or sequence** (reels spinning, popup appearing/closing, bonus entry, free-spin trigger, auto-spin run)
- The assertion checks **whether something happened**, not just the final frame
- The end-state alone could be **ambiguous** without seeing how the game got there

**Use `mode: image` (only when confident) when:**
- The assertion is a stable, self-evident end-state that a single frame captures completely (e.g. a static popup's text content, a counter value at rest, a balance/bet label) **and** the bridge assertion already confirmed the action succeeded — the AI only re-confirms the visible value
- A before/after comparison of two discrete screenshots is genuinely sufficient (`ref_screenshot: [step-01, step-05]`)

| `mode: video` (default) | `mode: image` (only when confident) |
|---|---|
| Reels spin animation played correctly | Static popup text content (paytable, history records) at rest |
| Bonus entry transition (idle → bonus screen) | Balance/bet/win value displayed and stable |
| Auto-spin sequence (start → spins → stop) | Counter value at rest after the action settled |
| Free spin trigger animation | Before/after balance comparison (two discrete screenshots) |
| Symbol landing / win presentation | Game loaded, all UI elements present |

Video mode uses `video_trim` instead of `ref_screenshot`:
```yaml
# mode: image — single screenshot
assert:
  ai:
    mode: image
    ref_screenshot: step-03
    prompt: "..."
    output_schema: { ... }
    expected: { ... }

# mode: video — when transition/animation must be observed
assert:
  ai:
    mode: video
    video_trim: { from_step: step-01, to_step: step-05 }
    prompt: "..."
    output_schema: { ... }
    expected: { ... }
```

**Rule 4 — Constrained Prompts with Concrete Values.**
Every AI assertion must have:
- A short **game-context preamble** at the start of the `prompt` (**≤ 300 tokens**, ~200 words): one or two sentences naming the game/theme and describing what is happening in this clip/screenshot — the phase or mechanic under test and what the player just did or what should occur. This grounds the model so it reads the visuals in context (e.g. it knows it is watching a Hold & Spin bonus, not an ordinary base spin). Draw it from `game-knowledge.md` §1 (Game Overview) + the test's intent. Keep it tight — it is *context*, not the question.
- A specific, closed-ended `prompt` question (not "describe what you see"), following the preamble
- An `output_schema` with typed fields constraining the model's response
- An `expected` block matching the schema
- A `confidence` field with enum `[high, medium, low]`; `low` = assertion fails

**Prompt shape:** `"{1–2 sentence game + scene context}. {Closed-ended question(s) referencing the concrete expected values}."` The context preamble orients the model (under 300 tokens — enough to set the scene, never a wall of text); the question carries the actual assertion.

**Concrete value requirement:** When the test knows a specific value (from bridge data, game config, or test setup), the AI prompt and expected block MUST include that exact value. Do NOT ask vague questions like "Is a bet amount displayed?" when you know the bet should be 0.20. Instead ask "Is the bet amount displayed as 0.20?" with `expected: { bet_value: 0.20 }`.

This applies to:
- **Denomination/bet values:** If bridge returns denominations `[0.20, 0.40, 1.00]`, the AI assertion must check for the specific value (e.g., "Does the bet label show 0.20?")
- **Balance values:** If initial balance is known (e.g., 10000), ask "Is the balance displayed as 10,000?" not "Is a balance displayed?"
- **Win amounts:** If bridge reports a win of 5.00, ask "Does the win display show 5.00?"
- **Counts and counters:** If free spins remaining is 10, ask "Does the counter show 10?" not "Is a counter visible?"

The AI assertion is the visual validator — it must confirm the **exact same value** the bridge reported, not just that *some* value exists.

**Rule 5 — Setup/Teardown Scope (shared once vs per-test).**
A test must never depend on a *previous test's* leftover state — but expensive preconditions that a **group** of tests share may be established **once** rather than per test. Split setup/teardown into two scopes:

- **Shared, once per group → suite-level `setup`/`teardown`.** The BO paytable is the canonical case: for a base-game group of 6 tests, `bo_paytable_ensure` + `bo_paytable_select` (and the group's `bo_paytable_update`) run **once** in suite setup, and `bo_paytable_reset` runs **once** in suite teardown — NOT per test (re-setting/resetting the paytable every test is wasteful and slow; see anti-pattern #15). Anything that is identical for every test in the group and costly to repeat belongs here.
- **Per-test → test-level `setup`/`teardown`.** Each test still owns the state unique to it: bring the game to the exact state it asserts on (bridge injection if used) in its `setup`, and return to a clean baseline in its `teardown` — e.g. `wait_for_state idle`, `resetDevMode`, close any popup it opened. Every test must leave the game in the same clean idle baseline the next test expects, so order-independence holds even though the shared paytable persists across the group. **If the test changed the player's server-side balance, restoring it here is mandatory** — snapshot it with `wallet_balance save: orig` in this test's setup and put it back with `wallet_set { amount: "$orig" }` in this teardown (test-scoped vars; see "Manipulating Player Balance (Wallet)" and anti-pattern #18).

**Every step in setup and teardown (both suite-level and test-level) MUST have an `id` field** — use the format `setup-01`, `setup-02`, ... and `teardown-01`, `teardown-02`, ... The runner validates `id` on ALL steps including setup/teardown and will reject the entire suite if any are missing.

**Rule 6 — Bridge Method Validation.**
IF a test needs a method not in `bridge-api.md`:
- Do not silently drop the assertion
- Flag it in the approval loop with a suggested signature
- Ask QA: skip this test or add to bridge-api.md first?

**Rule 8 — Mandatory Dual Assertion.**
Every test MUST include at least one AI visual assertion alongside its bridge assertions. The only exception is when the assertion is purely internal state with no visual manifestation (e.g., internal RNG seed, server-side balance ledger). For every bridge assertion, ask: "Does this state change have a visible effect on screen?" If yes, add an AI assertion that verifies the visual outcome. Apply Rule 2 to choose the mode — **default to `mode: video`**, use `mode: image` only when you are confident a single screenshot unambiguously captures the assertion. A test with bridge assertions but zero AI assertions is incomplete — treat it as a defect.

**Rule 7 — Flag Ambiguities.**
IF docs are unclear about a behavior: use the most common default for the mechanic. Add flag above the step:
```
!! AMBIGUITY: {what's unclear}. Assumed: {value}. Confirm with game dev.
```

### Step 5 — Present Executions One by One (Approval Loop)

> The overall plan (set, grouping, paytables, run consolidation) was already approved in Step 3
> (10a). Step 5 is the **detailed** gate: confirm each **execution**'s steps/assertions (a run with
> its cases, or a standalone test) ONE AT A TIME before its YAML is written.

11. Present each **execution** (a run, or a standalone test) in compact readable format, ONE AT A
    TIME. Do NOT output raw YAML at this stage. **Show its paytable**: which group/forced outcome it
    uses and whether it needs its **own** paytable. For a **run**, also show all its cases and which
    step/assert proves each (the `case:` / `expected_by_case` mapping the QA must confirm).

**Standalone test** (one execution → one verdict):
```
-------------------------------------
TEST {id} - {title}
-------------------------------------
GROUP    -> {group id}  (forced outcome: {group forced_outcome})
PAYTABLE -> {paytable_ref}   [own paytable: yes/no]
            {why this test needs its own paytable, if needs_own_paytable=true}
SETUP    -> {bridge injections and state setup}
STEP {n} -> {action description}
            assert (bridge) {method} = {expected}
            assert (AI/{mode}) {screenshot ref} -> "{prompt summary}"
TEARDOWN -> {teardown actions}
-------------------------------------
Approve? (yes / change: <describe fix> / paytable: <describe paytable change> / skip)
```

**Run** (one execution → many case verdicts):
```
-------------------------------------
RUN {run-id} - {title}   ({K} cases, 1 execution)
-------------------------------------
GROUP    -> {group id}  (forced outcome: {group forced_outcome})
PAYTABLE -> {paytable_ref}   [own paytable: yes/no]
CASES    -> {ZZ-a} "{title}";  {ZZ-b} "{title}";  ...
SETUP    -> {shared setup}
STEP {n} -> {action}                              [plain — every case depends on it]
STEP {n} -> {action}   case: {ZZ-a}              assert (bridge) {method} = {expected}
STEP {n} -> {action}   case: [{ZZ-a},{ZZ-b}]     assert (bridge analyser) {method} path:{p} = {expected}
STEP {n} -> AI/{mode} (one call) expected_by_case:
              {ZZ-a}: {expected subset}
              {ZZ-b}: {expected subset}
TEARDOWN -> {shared teardown}
-------------------------------------
Approve? (yes / change: <describe fix> / paytable: <...> / split: <case-id to break into its own test> / skip[: <case-id> to drop one case, or the whole run])
```

   - For **ungrouped** (bridge-injection only) executions, show `PAYTABLE -> none (bridge injection)`.
   - For a run, confirm that every case is covered by a `case:` tag or an `expected_by_case` key,
     and that only steps **all** cases need are left untagged (an untagged failure fails all cases).
   - If the agent judges an execution needs its own paytable, say so and explain the forced outcome.

12. IF a bridge method is missing, show before the test:
    ```
    !!  {id} needs {method} but this is not in bridge-api.md.
        Suggest: expose {method}({params}): {return_type}
        Skip this test, or add to bridge-api.md first?
    ```
13. IF an asset is missing from assets.yml:
    ```
    !!  {id} references ref_asset '{key}' not in assets.yml.
        Add '{key}' to games/{game}/docs/assets/assets.yml before running.
    ```
14. QA responses: `yes` (approved) | `change: <description>` (revise and re-present) | `paytable: <description>` (revise the paytable decision — group / own paytable / forced outcome — and re-present) | `skip` (exclude from final YAML). For a **run**, also: `split: <case-id>` (break that case out into its own standalone test and re-present both) | `skip: <case-id>` (drop just that case from the run, keeping the others).
15. **After each response, update the plan file** (`{suite}.plan.json`): set the execution's
    `status` (for a run, each **case**'s `status`) to `approved` or `skipped`, and write back any
    changed `paytable_ref` / `needs_own_paytable` / group assignment / run↔test moves. The plan
    file must always reflect the latest confirmed state — Step 6 reads it, not your memory.
16. Present exactly ONE execution (run or standalone test) per message and STOP. Do not present,
    mention, or pre-write the next one until the current is resolved (`yes`/`change`/`paytable`/
    `split`/`skip`) and the plan is updated. An ambiguous "just write it / go ahead" does NOT waive
    this loop — confirm explicitly before any batch (see anti-pattern #17). When QA types `done` or
    `complete`, or after all executions are resolved: proceed to Step 6.

### Step 6 — Write Final YAML

17. Write the final file to `games/{game}/test-suites/{suite}.yml`, conforming to the
    authoritative schema in `framework/schema_reference.md` **and**
    `framework/schema_reference.yml` (read both first — see below).

#### Suite File Schema — read the reference, don't hardcode

**The authoritative suite schema lives in `framework/schema_reference.md` and
`framework/schema_reference.yml`** (both auto-generated from the framework source). Read
**both** before writing the YAML — they define the top-level keys and their order, the
`config` block, the `groups` / `tests` / `runs` objects, every action's params, and the
`assert.bridge` / `assert.ai` formats (incl. `save:`, `bridge_assert` `args`/`path`, and
`runs:` `cases` / `case` / `expected_by_case`). Build the YAML to match them exactly.

Do **not** rely on a hardcoded copy of the schema in this skill: when the framework changes,
only those two files change (regenerate with `python scripts/generate_schema_reference.py`),
and they **win on any conflict**.

- `schema_reference.md` — human-readable: structure, notes, defaults, examples.
- `schema_reference.yml` — machine-readable: exact field names, types, required/optional, enums.

**Most common validation failures to double-check** (the reference is authoritative; these
just waste a whole run most often):
- `suite` is a top-level **string** title — never a dict; never nest `game`/`tests`/`config` under it.
- `game`, `version`, `config` are **root-level**; the suite has `tests:` and/or `groups:` (and/or `runs:`) at root — at least one.
- **Every step has an `id`** — suite/group/test setup, teardown, and main steps alike.
- **Test and run-case ids are unique suite-wide** so `--test <id>` can select one.
- Every `action` value and every `assert` field exists in `schema_reference.yml` — no invented names.

18. **Read the plan file** (`games/{game}/test-suites/{suite}.plan.json`) and build the YAML
    from it — include only **approved** items (a run is included if ≥1 of its cases is approved;
    drop skipped cases). Map the plan onto the schema:
    - each plan **run** → a `runs:` entry (its `cases`, the shared `setup`/`steps`/`teardown`,
      each case-specific assert tagged with `case:`, and one partitioned AI assert via
      `expected_by_case`) — see the **Runs** section for the exact shape;
    - each plan **standalone test** → a `tests:` entry;
    - each plan **group** → a `groups[]` entry whose group `setup` sets the group's `paytable.ref`
      via `bo_paytable_update` (the group holds its `runs:` and/or `tests:`);
    - any run/test with `needs_own_paytable: true` → a `bo_paytable_update` (its `paytable_ref`) in
      its own `setup` and a restore in its `teardown`;
    - `ungrouped` plan runs/tests → the top-level `runs:` / `tests:` lists.

    Apply these rules:
    - Exact bridge method names from the bridge API surface
    - Exact symbol/UI keys from `assets.yml`
    - Every execution (run or test) has its own `setup` and `teardown`
    - Sequential step IDs: `step-01`, `step-02`, ...
    - In a run: only steps **all** cases depend on are untagged; every other assert carries a
      `case:` tag (or `expected_by_case`); **every declared case is covered** by one of these
    - `confidence: high` required in all AI `expected` / `expected_by_case` blocks
    - Every field in `expected` / `expected_by_case` must exist in `output_schema`
19. **Pre-write validation checklist** — cross-check against `framework/schema_reference.md`:
    - [ ] File has `suite` as a top-level **string** (not a dict/object)
    - [ ] File has all root required fields from `schema_reference.md` → Suite File Structure
    - [ ] `config` has all required fields from `schema_reference.md` → Config block
    - [ ] Suite-level `setup` and `teardown` are at root level (not nested under `suite`)
    - [ ] The suite has `tests`, `groups`, and/or `runs` at root level (at least one; not nested under `suite`)
    - [ ] Each `group` has a unique `id` and a non-empty `tests`/`runs` (per schema_reference.md → Group object)
    - [ ] **Every** test id AND run-case id is unique across the whole suite (ungrouped + all groups)
    - [ ] In each `run`: every declared `case` is covered by a `case:` tag or an `expected_by_case` key; only all-case steps are untagged (per schema_reference.md → Runs)
    - [ ] Every step has an `id` field — including suite/group/test/run setup and teardown (per schema_reference.md → Step object)
    - [ ] Every `action` value is listed in `schema_reference.md` → Supported Action Types
    - [ ] Every action's required params are present (per schema_reference.md)
    - [ ] On-screen button presses use `click_button` (not method-invoking `bridge_action`, which is a documented last resort), and each tap is wrapped: `waitForButtonClickable` before → `click_button` → `wait_ms 300` after (plus a result wait like `wait_for_state` when the tap starts something async)
    - [ ] Any BO-paytable (deterministic) suite restores the default in teardown — a `bo_paytable_reset` in the **suite** teardown, and a baseline-restoring `bo_paytable_update` in any group/test that forced its own result
    - [ ] Any test that changes the player balance (`wallet_set`/`wallet_deposit`/`wallet_withdraw`) snapshots it with `wallet_balance save:` in its **own setup** and restores it with `wallet_set { amount: "$orig" }` in its **own teardown** (anti-pattern #18)
    - [ ] Every `assert.bridge` has exactly the fields listed in schema_reference.md → assert.bridge
    - [ ] Every `assert.ai` has the mode-specific required fields from schema_reference.md → assert.ai
    - [ ] No invented action types or assert fields beyond what schema_reference.md defines

### Step 6b — Generate Forced-Result Paytables (sub-agent)

After the suite YAML is written, generate the actual paytable fixture file for every
`paytable_ref` the suite uses, so `bo_paytable_update` has real content to upload.

19a. From the plan file, collect the **distinct** `paytable_ref` values referenced by
   approved groups/runs/tests (skip `null`/ungrouped — those use bridge injection, no paytable).
   For each distinct ref, the fixture lives at `games/{game}/paytables/{paytable_ref}.json`.
   Skip refs whose file already exists unless QA asked to regenerate.

19b. **Spawn a sub-agent (Agent tool) per distinct `paytable_ref`** to generate its fixture.
   Give each sub-agent these inputs and instructions:
   - Read `games/{game}/paytables/base.json` — the base paytable to start from (structure +
     baseline weights).
   - Read `games/{game}/paytables/paytable.md` — the paytable rule reference: how the server
     samples each board/respin from the `Weight` values, and **how to tune those weights to
     force a deterministic outcome**.
   - Read the plan-file entry for this `paytable_ref` (the group's `paytable.forced_outcome`,
     or the test's required result) so the sub-agent knows exactly what outcome to force.
   - Task: produce a valid paytable JSON (same shape as `base.json`) whose weights
     deterministically yield the intended outcome (e.g. guaranteed free-spin trigger,
     forced big win, no-win baseline), following the rules in `paytable.md`. Write it to
     `games/{game}/paytables/{paytable_ref}.json`.
   Run the sub-agents in parallel (one per ref). If a sub-agent cannot satisfy the forced
   outcome from the documented rules, it must say so — flag that ref in the coverage report
   (`# PAYTABLE UNRESOLVED: {ref} — {reason}`) rather than emit a guess.

19c. After the sub-agents finish, verify each referenced
   `games/{game}/paytables/{paytable_ref}.json` now exists and is valid JSON. Any still
   missing is reported as `# PAYTABLE MISSING: {ref}` in the coverage report.

### Step 7 — Output Coverage Report

20. After writing YAML, output:

```
=== write-test coverage report for {game} / {suite} ===

Cases written:    {C}   (across {E} executions: {R} runs + {T} standalone tests)
Cases skipped:    {N}
Total planned:    {C}
Replays saved:    {C - E}   (cases consolidated into shared-execution runs)

Mechanics covered:
  [V] {mechanic} -- {N} case(s)

Mechanics NOT covered (from game-knowledge.md):
  [ ] {mechanic} -- reason: {no scope / missing bridge methods / explicit skip}

Bridge methods used:
  {method} -- {N} test(s)

Bridge methods in bridge-api.md NOT used:
  {method}

Missing bridge methods flagged:
  {method} -- needed by {test_id}, suggestion logged

Assets referenced:
  {asset_key} -- {N} time(s)

Assets in assets.yml NOT referenced:
  {asset_key}

Assertion coverage (per case):
  Dual (bridge + AI):  {N} case(s)
  Bridge-only:         {N} case(s)  [!! if > 0, review for missing AI assertions]
  AI-only:             {N} case(s)

Consolidation:
  {run_id} -> {K} cases in 1 execution: {ZZ-a}, {ZZ-b}, ...
  Standalone tests (own execution): {N}

Paytable groups / fixtures:
  {group_id} -> paytables/{ref}.json  ({N} execution(s))  [generated / pre-existing / UNRESOLVED]
  Per-execution own paytables:
    {run_or_test_id} -> paytables/{ref}.json  [generated / pre-existing / UNRESOLVED]
  Ungrouped (bridge-injection only): {N} execution(s)

Ambiguities flagged: {N}
  - {id}: {brief description}

Artifacts:
  Plan file: games/{game}/test-suites/{suite}.plan.json
  Suite:     games/{game}/test-suites/{suite}.yml
  Paytables: games/{game}/paytables/{ref}.json (one per forced result)

Next steps:
  1. Run /run-test --game={game} --suite={suite} --env=dev
  2. Review/adjust any generated paytables flagged UNRESOLVED in games/{game}/paytables/
  3. Add missing reference images to games/{game}/docs/assets/symbols/
  4. Address flagged missing bridge methods with game dev
```

---

## Examples

### Example 1: BAD vs GOOD — AI Assertion Prompt

**BAD:**
```yaml
- id: step-03
  assert:
    ai:
      mode: image
      ref_screenshot: step-02
      asset_groups: [symbols, ui_elements]   # loads the ENTIRE pack — overload (anti-pattern #4)
      prompt: "What do you see on the screen?"
```
Vague prompt (anti-pattern #3). All assets loaded (anti-pattern #4). No output_schema. No expected. Gemini returns free-form text — unjudgeable.

**ALSO BAD — vague values (anti-pattern #13):**
```yaml
- id: step-03
  description: "Verify bet label shows valid value"
  assert:
    ai:
      mode: image
      ref_screenshot: step-02
      assets: [ui_elements.bet_label]
      prompt: "Is a bet amount displayed on the bet label? Does the game show a valid numeric bet value?"
      output_schema:
        bet_displayed: { type: boolean }
        valid_numeric: { type: boolean }
        confidence: { type: string, enum: [high, medium, low] }
      expected: { bet_displayed: true, valid_numeric: true, confidence: high }
```
Has structure but checks only "is *some* bet displayed?" — does not verify the *correct* value. A broken UI showing "999.99" instead of "0.20" would pass.

**GOOD:**
```yaml
- id: step-03
  description: "Verify 3 scatter symbols triggered free spin"
  assert:
    ai:
      mode: image
      ref_screenshot: step-02
      assets: [symbols.scatter, ui_elements.free_spin_popup]
      prompt: "This is a slot game; the player just finished a base-game spin that should have landed scatters and triggered the free spin bonus. Is the free spin bonus popup visible? How many scatter symbols are on the grid?"
      output_schema:
        popup_visible: { type: boolean }
        scatter_count: { type: number }
        confidence: { type: string, enum: [high, medium, low] }
      expected: { popup_visible: true, scatter_count: 3, confidence: high }
```
Closed-ended prompt. Only relevant assets. Typed schema. Confidence gated. Clear pass/fail.

### Example 4: BAD vs GOOD — Denomination/Bet Verification

**BAD — uses script for cross-step comparison (anti-pattern #14):**
```yaml
- id: step-01
  action: bridge_action
  params: { name: getGameInfo }
- id: step-02
  action: bridge_action
  params: { name: getDenominations }
- id: step-03
  action: script
  params:
    cmd: "assert_equal(steps['step-02'].result, steps['step-01'].result.settings.denominations)"
```
This *runs* (script is JS in the browser with `steps` injected), but it buries the verdict in ad-hoc JS — fragile and unvalidated (anti-pattern #14). Prefer `save:` the values and let a bridge function judge them via `bridge_assert` `args: ["$name"]`, or assert each value against a literal `expected` as in the GOOD example below.

**ALSO BAD — vague AI check (anti-pattern #13):**
```yaml
- id: step-03
  assert:
    ai:
      mode: image
      ref_screenshot: step-02
      assets: [ui_elements.bet_label]
      prompt: "Is a bet amount displayed?"
      output_schema:
        bet_displayed: { type: boolean }
        confidence: { type: string, enum: [high, medium, low] }
      expected: { bet_displayed: true, confidence: high }
```
Confirms *something* is displayed, not that the *right* value is shown.

**GOOD — concrete value verification:**
```yaml
# Step 1: Get denominations from bridge (value is known from game config)
- id: step-01
  description: "Get denomination list from bridge"
  action: bridge_action
  params: { name: getDenominations }

# Step 2: Assert the default bet via bridge (first denomination)
- id: step-02
  description: "Verify default bet is the first denomination"
  assert:
    bridge:
      method: getBetAmount
      expected: 0.20

# Step 3: Capture the bet display
- id: step-03
  description: "Capture bet display for visual check"
  action: capture

# Step 4: AI confirms the SPECIFIC bet value on screen
- id: step-04
  description: "Verify bet label shows 0.20"
  assert:
    ai:
      mode: image
      ref_screenshot: step-03
      assets: [ui_elements.bet_label]
      prompt: "This is a slot game at rest on the base-game screen; the bet selector should show the default bet. What is the exact bet amount shown on the bet label? Read the numeric value."
      output_schema:
        bet_value: { type: number }
        confidence: { type: string, enum: [high, medium, low] }
      expected: { bet_value: 0.20, confidence: high }
```
Bridge asserts the exact value (0.20). AI independently confirms the same value is displayed on screen. No cross-step comparison needed — each assertion checks against a known literal value.

### Example 2: BAD vs GOOD — Test Setup

**BAD:**
```yaml
- id: ZZ-05
  title: "Verify hold and spin re-spin counter"
  steps:
    - id: step-01
      description: "Check re-spin counter shows 3"
      assert:
        bridge:
          method: getRespinCount
          expected: 3
```
No setup block — assumes game is already in hold-and-spin state (anti-pattern #2). `getRespinCount` not validated against bridge-api.md. If previous test didn't trigger bonus, this fails.

**GOOD:**
```yaml
- id: ZZ-05
  title: "Verify hold and spin re-spin counter"
  setup:
    - id: setup-01
      action: bridge_action
      params: { name: "setDevModeResult", result: "hold_and_spin_trigger", coin_count: 6 }
    - id: setup-02
      action: bridge_action
      params: { name: "spin" }
    - id: setup-03
      action: wait_for_state
      params: { state: "hold_and_spin", timeout_ms: 10000 }
  steps:
    - id: step-01
      description: "Capture hold-and-spin screen"
      action: capture
      capture: true
    - id: step-02
      description: "Assert re-spin counter shows 3"
      assert:
        bridge:
          method: getHoldAndSpinState
          expected: { respins_remaining: 3 }
        ai:
          mode: image
          ref_screenshot: step-01
          assets: [ui_elements.respin_counter]
          prompt: "This is a slot game in the Hold & Spin bonus, just triggered; the re-spin counter should read 3. Is the game in hold-and-spin mode? What number is displayed on the re-spin counter?"
          output_schema:
            in_hold_and_spin: { type: boolean }
            counter_value: { type: number }
            confidence: { type: string, enum: [high, medium, low] }
          expected: { in_hold_and_spin: true, counter_value: 3, confidence: high }
  teardown:
    - id: teardown-01
      action: bridge_action
      params: { name: "resetDevMode" }
```
Self-contained: injects hold-and-spin state from idle. Dual assertion (bridge + AI). Uses `mode: image` because the assertion checks a stable end-state (counter value) — bridge already verified the setup succeeded. Clean teardown. Every setup/teardown step has an `id`.

### Example 3: Handling a Missing Bridge Method

**BAD (silent skip):**
Test for buy bonus simply doesn't appear in the suite. QA assumes it's covered elsewhere. Coverage gap discovered weeks later.

**GOOD (flagged to QA):**
```
!!  ZZ-12 needs purchaseBonus but this is not in bridge-api.md.
    Suggest: expose purchaseBonus({ package: string }): { success: boolean, cost: number }
    Skip this test, or add to bridge-api.md first?

-------------------------------------
TEST ZZ-12 - Buy bonus package purchase
-------------------------------------
SETUP    -> setDevModeResult(balance: 50000)
STEP 1   -> bridge: purchaseBonus({ package: "100x" })   [!! METHOD MISSING]
STEP 2   -> wait_for_state: bonus_active
            assert (bridge) getAppState = "bonus"
            assert (AI/video) step-1..step-2 -> "Did the buy bonus animation play and transition to bonus game?"
TEARDOWN -> resetDevMode
-------------------------------------
Approve? (yes / change: <describe fix> / skip)
```
Missing method is visible. QA decides whether to skip or request the method. Coverage report will record the gap.

---

## Questions This Skill Answers

- "Write tests for [game]"
- "Create a test suite for [mechanic]"
- "Generate YAML test cases"
- "Add tests for free spin / hold and spin / buy bonus"
- "Convert this test idea into YAML steps"
- "I have a test case, make it executable"
- "Write tests that use the bridge API"
- "Generate visual AI assertions for [game]"
- "Test the reconnect flow for [game]"
- "Create a base game test suite"
- "What tests should we write for [mechanic]?"
- "Make this test description into proper YAML"
- "I need tests for the new game we onboarded"
