# Skill: /write-test

```yaml
name: write-test
version: "2.0"
domain: [game-qa, test-authoring, visual-ai-testing, bridge-integration]
depends_on: [init-knowledge, bridge-api]
description: |
  Generates executable YAML test suites for slot games using bridge
  injection for deterministic state control, dual assertions (bridge
  query + Gemini visual AI), constrained output_schema prompts, and
  ref_assets from symbols.yml. Tests go through a one-by-one QA
  approval loop before final YAML is written.

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
step-based test structure, sequential step IDs (every step including setup/teardown requires `id`), setup/teardown isolation, self-contained test state, suite-level vs test-level lifecycle, test determinism via bridge injection, 12 supported action types only (navigate, wait_for_state, bridge_action, bridge_assert, capture, assert, script, go_offline, go_online, press_key, click, wait_ms)

**Bridge Integration:**
bridge action, bridge injection (setDevModeResult), bridge method validation, state query (getAppState, getBalance), dev mode reset (resetDevMode), API surface coverage, missing method flagging, bridge assert schema (method + expected only, no computed fields)

**AI Visual Assertions:**
dual assertion (bridge + AI), cost-aware mode selection (default image, video only when transition/animation must be observed), constrained prompt, output_schema, confidence gating (high/medium/low), ref_assets selection, reference screenshot (ref_screenshot for image mode), video trim (from_step/to_step for video mode), Gemini visual verification

**QA Workflow:**
approval loop (approve/change/skip), compact test presentation format, coverage report, mechanic coverage matrix, ambiguity flagging (`!! AMBIGUITY`), asset gap detection

**State & Determinism:**
reachable state enumeration, deterministic test path, idle state baseline, clean teardown contract, state injection before assertion

---

## Supported Action Types Reference

The suite runner accepts **exactly** these 12 action types. Using any other value causes a validation error.

| Action | Required params | Description |
|--------|-----------------|-------------|
| `navigate` | `url` or `url_script` | Navigate browser to URL |
| `wait_for_state` | `state`; optional `timeout_ms` (default 30000) | Wait until bridge reports the given app state |
| `bridge_action` | `name`; additional params forwarded to bridge | Call a bridge method (return value stored by runner) |
| `bridge_assert` | `method`, `expected` | Call bridge method and assert return equals expected |
| `capture` | none | Take a screenshot |
| `assert` | (requires `assert` block on the step) | Pure assertion step (no action) |
| `script` | `cmd` | Execute a shell command |
| `go_offline` | none | Set browser to offline mode |
| `go_online` | none | Restore browser online mode |
| `press_key` | `key` | Press a keyboard key |
| `click` | `selector` OR (`x` + `y`) | Click a CSS selector or canvas coordinates |
| `wait_ms` | `ms` (must be > 0) | Sleep for N milliseconds |

### Common Intent → Correct Action

| What you want to do | WRONG (invented) | CORRECT |
|---|---|---|
| Loop a bridge call until condition | `bridge_action_until` | Multiple `bridge_action` steps, or a `script` step |
| Read a bridge value without asserting | `bridge_query` | `bridge_action` (runner stores the return value) |
| Reload the game page | `reload` | `navigate` + `wait_for_state ready` + `click #start-button` + `wait_for_state idle` |
| Pause for N milliseconds | `wait` | `wait_ms` with `params: { ms: N }` |
| Wait until bridge returns a value | `wait_for_condition` | `wait_for_state` |
| Compare values across steps | `script` with `assert_equal(steps[...])` | Separate `bridge_assert` steps with literal expected values |
| Verify a bridge value is shown on screen | Bridge-only assertion | `bridge_assert` + `capture` + AI assertion with the concrete value in the prompt |

**NEVER invent action type names.** If none of the 12 actions fits, use `script` to run custom logic.

---

## Bridge Assert Schema

`assert.bridge` accepts **exactly 2 fields**:

```yaml
assert:
  bridge:
    method: getAppState      # string — bridge method name
    expected: "idle"          # any literal value — compared with equality
```

**No other fields are supported.** The following are all INVALID and will fail validation:
- `expected_type`, `expected_min_count`, `expected_path` — not real fields
- `expected_match`, `expected_relative`, `expected_formula` — not real fields

**Cross-step value comparison is NOT supported.** The runner does not expose previous step results to subsequent steps. Do NOT use `script` with pseudo-code like `assert_equal(steps['step-02'].result, ...)` — the `script` action runs a raw shell command, not Python.

Instead, use one of these patterns:
- **`bridge_assert`** to check each value independently with a known literal `expected`.
- **`bridge_action`** to fetch a value, then **`capture`** + **AI assertion** asking the AI to confirm the specific value is displayed on screen.
- If you truly need server-side comparison, write a real Python script file and invoke it via `script: { cmd: "python scripts/my_check.py" }`.

---

## Anti-Pattern Watchlist

### 1. Phantom Bridge Method
**Detection:** A step uses a bridge method name that does not appear in `bridge-api.md`.
**Why it's dangerous:** Test will fail at runtime with "method not found." QA wastes a test cycle before discovering the gap.
**Resolution:** Validate every method against `bridge-api.md` before writing. IF missing: flag to QA with a suggested signature. Never silently invent methods.

### 2. Leaky Test State
**Detection:** A test's setup block assumes the game is already in a specific state (e.g., "in bonus") without explicitly establishing it via bridge injection.
**Why it's dangerous:** Test passes when run after a specific predecessor but fails in isolation or different order.
**Resolution:** Every test must reach its starting state from idle. Setup block must include bridge injection to configure the exact state needed.

### 3. Vague AI Prompt
**Detection:** AI assertion prompt is open-ended: "What do you see?", "Describe the screen," "What is happening?" No output_schema constraining the response.
**Why it's dangerous:** Gemini returns free-form prose. No structured field to compare against expected. Assertion is unjudgeable.
**Resolution:** Write closed-ended prompts with specific questions. Always include output_schema with typed fields and a confidence enum. Always include expected block matching schema.

### 4. Asset Overload
**Detection:** ref_assets contains all symbol keys from symbols.yml rather than only the ones relevant to the specific assertion.
**Why it's dangerous:** Floods the AI context with irrelevant reference images, reducing accuracy and increasing latency.
**Resolution:** Select only the ref_assets keys the AI needs to answer the specific prompt. "Is the wild symbol in the center?" needs only `wild`, not the entire symbol set.

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

### 9. Invented Action Type
**Detection:** A step uses an `action` value not in the 12 supported types (e.g., `bridge_action_until`, `bridge_query`, `reload`, `wait`, `wait_for_condition`).
**Why it's dangerous:** Suite runner rejects the entire suite at validation time. Zero tests execute.
**Resolution:** Consult the "Supported Action Types Reference" table above. Map intent to the correct action. If no action fits, use `script`.

### 10. Invented Bridge Assertion Field
**Detection:** `assert.bridge` contains fields other than `method` and `expected` (e.g., `expected_type`, `expected_relative`, `expected_formula`, `expected_match`, `expected_path`, `expected_min_count`).
**Why it's dangerous:** Suite runner requires `expected` — any other field name is silently ignored, causing a "required field 'expected' is missing" validation error.
**Resolution:** Use only `method` + `expected` with a literal value. For cross-step comparisons, use a `script` step to compute the value first.

### 11. Wrong Mode Selection (Image vs Video)
**Detection:** `mode: image` is used when the assertion requires observing a transition or animation (e.g., "did the reels spin?", "did the popup animate in?"). OR `mode: video` is used when a simple end-state screenshot would suffice (e.g., "is the balance displayed?", "is the popup visible?").
**Why it's dangerous:** Image when video is needed: AI sees only the end-state and can't verify the sequence occurred correctly. Video when image suffices: wastes Gemini API budget on unnecessary video processing.
**Resolution:** Apply the Rule 2 decision question: "Can the AI determine pass/fail from a single end-state screenshot?" If yes → image. If the assertion checks a transition, animation, or sequence of events → video.

### 13. Vague AI Expected Values
**Detection:** AI assertion asks "Is a bet amount displayed?" or "Is a balance shown?" without specifying the concrete expected value, even though the test knows the exact value (from bridge data, game config, or setup injection).
**Why it's dangerous:** The AI confirms *something* is displayed, but not that the *correct* value is displayed. A broken UI showing "999999" instead of "0.20" would pass. The AI assertion becomes useless validation theater.
**Resolution:** When the test knows the expected value, embed it in the prompt and expected block. Instead of `"Is a bet amount displayed?"` → `"Is the bet amount displayed as 0.20?"` with `expected: { bet_value: 0.20 }`. The AI assertion must verify the **same specific value** that the bridge assertion checks.

### 14. Script for Cross-Step Comparison
**Detection:** A `script` step contains pseudo-code referencing `steps['step-02'].result` or similar cross-step value access (e.g., `assert_equal(steps[...], steps[...])`).
**Why it's dangerous:** The `script` action runs a raw **shell command** via `subprocess.run(cmd, shell=True)`. It has no access to step results, no Python evaluation context, and no `assert_equal` function. On Windows it fails with "'assert_equal' is not recognized"; on Unix it fails with "command not found."
**Resolution:** Never use `script` for cross-step value comparison. Use `bridge_assert` with a known literal `expected` value for each check independently. If you need to visually confirm a bridge value on screen, use `bridge_action` → `capture` → AI assertion with the concrete value in the prompt.

### 7. Ambiguity Burial
**Detection:** A value in a step (e.g., re-spin count, state string, trigger threshold) is assumed without documentation support and no `!! AMBIGUITY` flag.
**Why it's dangerous:** Test encodes a guess as a fact. Fails against the real game, and nobody knows which value to fix.
**Resolution:** IF docs are unclear about a value: use the most common default for the mechanic, flag with `!! AMBIGUITY: {what's unclear}. Assumed: {value}. Confirm with game dev.`

### 12. Nested File Structure
**Detection:** The generated YAML file nests `game`, `tests`, `setup`, `teardown`, or `config` inside a `suite:` object instead of placing them at the root level. OR `suite` is written as a dict/object instead of a plain string.
**Why it's dangerous:** The suite runner validates top-level schema immediately. A nested structure fails with "required field 'game' is missing" and "expected str, got dict" — zero tests execute, the entire run is wasted.
**Resolution:** `suite` is always a string title. `game`, `version`, `config`, `setup`, `teardown`, `tests` are always root-level keys. Refer to the Suite File Schema in Step 6.

---

## Behavioral Instructions

### Step 1 — Load Knowledge Inputs

1. Read these required files:
   - `games/{game}/game-knowledge.md` — game overview, symbols, mechanics, bridge API summary
   - `games/{game}/bridge-api.md` — bridge method names and signatures
   - `games/{game}/docs/assets/symbols.yml` — symbol keys, types, image paths
2. Load shared mechanism files based on `--scope` (or all if no scope):
   - `shared-mechanisms/hold-and-spin.md`
   - `shared-mechanisms/free-spin.md`
   - `shared-mechanisms/buy-bonus.md`
   - `shared-mechanisms/reconnect.md`
   - `shared-mechanisms/auto-spin.md`
3. IF `game-knowledge.md` does not exist: output `"Run /init-knowledge --game={game} first."` and STOP.
4. IF `bridge-api.md` does not exist: output `"Bridge API documentation required. Ask the game developer to add it."` and STOP.

### Step 2 — Determine Scope and Test Sources

5. IF `--scope` provided: filter to only the matching mechanic sections and shared mechanism file.
6. IF no `--scope`: generate tests covering all confirmed mechanics in game-knowledge.md.
7. IF `--raw` provided: use QA's raw description as the primary test case. Refine into YAML steps using knowledge files for bridge method names, state transitions, and assertions. Do not replace QA's intent — only structure it.
8. IF no `--raw`: generate from knowledge files and canonical patterns in each shared mechanism's "Canonical Test Patterns" section. Adapt patterns to use concrete bridge methods from `bridge-api.md` and add AI visual assertions using ref_assets from `symbols.yml`. Every canonical pattern must be augmented with at least one AI visual assertion that verifies the visible outcome of the action — do not output bridge-only tests.

### Step 3 — Plan the Test Set

9. Build an internal test plan (not shown to QA yet):

   | Field | Value |
   |-------|-------|
   | Test ID | Sequential within suite (e.g., `ZZ-30`, `ZZ-31` — derive prefix from game name) |
   | Title | Descriptive test name |
   | Source | Which canonical pattern or `--raw` input it derives from |
   | Bridge methods | Which methods it requires |
   | AI assertions | Which visual assertions to include — mode (image/video), target element, ref_assets keys |
   | Mechanism | Which shared mechanism applies (if any) |

10. Validate every bridge method against `bridge-api.md`. Flag any test requiring a missing method — do not silently drop it.

### Step 4 — AI Reasoning Rules (Apply to Every Test)

Apply all 7 rules when constructing each test case.

**Rule 1 — Enumerate Reachable States.**
Before writing assertions for any action step, enumerate all possible outcomes in your thinking (not in YAML output). The test must use bridge injection to make the path deterministic. IF no injection method exists for this state: flag to QA.

**Rule 2 — Image vs Video Mode.**

**Default to `mode: image`** — video calls are expensive (Gemini API cost). Only use video when a screenshot genuinely cannot capture the information needed to judge pass/fail.

**Decision process — ask this question for each AI assertion:**
> "Can the AI determine pass/fail from a single screenshot of the end-state?"
> - **YES → `mode: image`** with `ref_screenshot`
> - **NO → `mode: video`** with `video_trim`

**Use `mode: image` when:**
- The end-state screenshot fully captures the assertion (UI loaded, popup visible, counter value, symbol on grid, balance displayed, bet amount shown)
- The bridge assertion already verifies the action succeeded — the AI only needs to confirm the visual end-state matches
- Before/after comparison where two separate screenshots are sufficient (use `ref_screenshot: [step-01, step-05]`)

**Use `mode: video` when:**
- The assertion requires observing a **transition or animation** (reels spinning, popup appearing and closing, bonus entry sequence)
- The assertion checks **whether something happened** rather than the end-state (did the reels spin? did the popup animate in?)
- The end-state alone is **ambiguous** without seeing how the game got there (e.g., auto-spin running — a screenshot can't distinguish "spinning" from "frozen")

| `mode: image` examples | `mode: video` examples |
|---|---|
| Game loaded, all UI elements present | Reels spin animation played correctly |
| Popup content (paytable, history records) | Bonus entry transition (idle → bonus screen) |
| Balance/bet/win value displayed | Auto-spin sequence (start → spins → stop) |
| Symbol visible on grid after spin | Free spin trigger animation |
| Before/after balance comparison (two screenshots) | Popup open → interact → close sequence |

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

**Rule 3 — Select ref_assets from Manifest.**
Include only the symbol/UI keys from `symbols.yml` directly relevant to the assertion. IF a needed asset is missing: add `# ASSET MISSING: {key}` comment and note in coverage report.

**Rule 4 — Constrained Prompts with Concrete Values.**
Every AI assertion must have:
- A specific, closed-ended `prompt` (not "describe what you see")
- An `output_schema` with typed fields constraining Gemini's response
- An `expected` block matching the schema
- A `confidence` field with enum `[high, medium, low]`; `low` = assertion fails

**Concrete value requirement:** When the test knows a specific value (from bridge data, game config, or test setup), the AI prompt and expected block MUST include that exact value. Do NOT ask vague questions like "Is a bet amount displayed?" when you know the bet should be 0.20. Instead ask "Is the bet amount displayed as 0.20?" with `expected: { bet_value: 0.20 }`.

This applies to:
- **Denomination/bet values:** If bridge returns denominations `[0.20, 0.40, 1.00]`, the AI assertion must check for the specific value (e.g., "Does the bet label show 0.20?")
- **Balance values:** If initial balance is known (e.g., 10000), ask "Is the balance displayed as 10,000?" not "Is a balance displayed?"
- **Win amounts:** If bridge reports a win of 5.00, ask "Does the win display show 5.00?"
- **Counts and counters:** If free spins remaining is 10, ask "Does the counter show 10?" not "Is a counter visible?"

The AI assertion is the visual validator — it must confirm the **exact same value** the bridge reported, not just that *some* value exists.

**Rule 5 — Self-Contained Setup.**
Every test's `setup` block must establish its own state from idle via bridge injection. Every test's `teardown` must reset injected state. No test may depend on a predecessor's state. **Every step in setup and teardown (both suite-level and test-level) MUST have an `id` field** — use the format `setup-01`, `setup-02`, ... and `teardown-01`, `teardown-02`, ... The runner validates `id` on ALL steps including setup/teardown and will reject the entire suite if any are missing.

**Rule 6 — Bridge Method Validation.**
IF a test needs a method not in `bridge-api.md`:
- Do not silently drop the assertion
- Flag it in the approval loop with a suggested signature
- Ask QA: skip this test or add to bridge-api.md first?

**Rule 8 — Mandatory Dual Assertion.**
Every test MUST include at least one AI visual assertion alongside its bridge assertions. The only exception is when the assertion is purely internal state with no visual manifestation (e.g., internal RNG seed, server-side balance ledger). For every bridge assertion, ask: "Does this state change have a visible effect on screen?" If yes, add an AI assertion that verifies the visual outcome. Apply Rule 2 to decide `mode: image` vs `mode: video` — default image, use video only when the assertion requires observing a transition or animation. Select ref_assets from `symbols.yml` relevant to the visual check. A test with bridge assertions but zero AI assertions is incomplete — treat it as a defect.

**Rule 7 — Flag Ambiguities.**
IF docs are unclear about a behavior: use the most common default for the mechanic. Add flag above the step:
```
!! AMBIGUITY: {what's unclear}. Assumed: {value}. Confirm with game dev.
```

### Step 5 — Present Tests One by One (Approval Loop)

11. Present each test in compact readable format. Do NOT output raw YAML at this stage.

```
-------------------------------------
TEST {id} - {title}
-------------------------------------
SETUP    -> {bridge injections and state setup}
STEP {n} -> {action description}
            assert (bridge) {method} = {expected}
            assert (AI/{mode}) {screenshot ref} -> "{prompt summary}"
TEARDOWN -> {teardown actions}
-------------------------------------
Approve? (yes / change: <describe fix> / skip)
```

12. IF a bridge method is missing, show before the test:
    ```
    !!  {id} needs {method} but this is not in bridge-api.md.
        Suggest: expose {method}({params}): {return_type}
        Skip this test, or add to bridge-api.md first?
    ```
13. IF an asset is missing from symbols.yml:
    ```
    !!  {id} references ref_asset '{key}' not in symbols.yml.
        Add '{key}' to games/{game}/docs/assets/symbols.yml before running.
    ```
14. QA responses: `yes` (approved, next test) | `change: <description>` (revise and re-present) | `skip` (exclude from final YAML).
15. Proceed through all tests one at a time, waiting for QA after each.
16. When QA types `done` or `complete`, or after all tests processed: proceed to Step 6.

### Step 6 — Write Final YAML

17. Write the final file to `games/{game}/test-suites/{suite}.yml` using the **Suite File Schema** below.

#### Suite File Schema

The suite runner validates the file's top-level structure before executing any tests. The file MUST have these root-level keys in this order:

```yaml
# Header comment (optional — suite description, generation date, source info)

suite: "Human-readable suite title"   # MUST be a string, NOT a dict/object
game: {game}                          # game identifier — matches the folder name
version: "1.0"                        # schema version string

config:
  detection_method: both              # "both" | "phash" | "gemini"
  video: true                         # enable video recording for video-mode assertions
  test_timeout_ms: 60000              # per-test timeout (default 60s)
  suite_timeout_ms: 600000            # total suite timeout (default 10min)
  stop_conditions:
    fail_fast: false                  # stop on first failure
    max_consecutive_failures: 3       # stop after N consecutive failures
    max_total_failures: 10            # stop after N total failures

setup:                                # suite-level setup (runs once before all tests)
  - action: navigate
    params: { url_script: "get_game_url" }
  - action: wait_for_state                # wait for bridge to register at READY
    params: { state: "ready", timeout_ms: 30000 }
  - action: bridge_action                 # click Start button via bridge — waits for
    params: { name: "startGame" }         # GameplayScene.sceneReady (not just IDLE)

teardown: []                          # suite-level teardown (runs after EACH test)
                                      # Use [] if tests handle their own teardown

tests:                                # list of test objects
  - id: ZZ-01
    title: "..."
    ...
```

**Critical rules:**
- `suite` is a **string** (title), not a nested object. NEVER nest `game`, `tests`, or other fields under `suite`.
- `game`, `version`, `config`, `setup`, `teardown`, `tests` are ALL at the **root level** of the file.
- `config` is required — the runner uses it for timeout and stop condition enforcement.
- Suite-level `setup`/`teardown` steps do NOT need `id` fields (only test-level setup/teardown steps do).
- Test-level `setup`/`teardown` steps MUST have `id` fields.

18. Include only approved tests. Apply these rules:
    - Exact bridge method names from `bridge-api.md`
    - Exact symbol/UI keys from `symbols.yml`
    - Every test has its own `setup` and `teardown`
    - Sequential step IDs: `step-01`, `step-02`, ...
    - `confidence: high` required in all AI `expected` blocks
    - Every field in `expected` must exist in `output_schema`
19. **Pre-write validation checklist** — verify ALL of these before writing:
    - [ ] File has `suite` as a top-level **string** (not a dict/object)
    - [ ] File has top-level `game`, `version`, `config` fields
    - [ ] `config` has `detection_method`, `video`, `test_timeout_ms`, `suite_timeout_ms`, `stop_conditions`
    - [ ] Suite-level `setup` and `teardown` are at root level (not nested under `suite`)
    - [ ] `tests` is at root level (not nested under `suite`)
    - [ ] Every step (in `steps`, `setup`, AND `teardown`) has an `id` field
    - [ ] Every `action` value is one of the 12 supported types (see reference table)
    - [ ] Every `assert.bridge` has exactly `method` + `expected` (no invented fields)
    - [ ] Every `assert.ai` has `mode`, `ref_screenshot`/`video_trim`, `prompt`, `output_schema`, `expected`
    - [ ] Suite-level `setup` and `teardown` steps also have `id` fields
    - [ ] No invented action types (`bridge_action_until`, `bridge_query`, `reload`, `wait`, `wait_for_condition`)
    - [ ] No invented bridge fields (`expected_type`, `expected_relative`, `expected_formula`, `expected_match`, `expected_path`, `expected_min_count`)

### Step 7 — Output Coverage Report

20. After writing YAML, output:

```
=== write-test coverage report for {game} / {suite} ===

Tests written:    {N}
Tests skipped:    {N}
Total planned:    {N}

Mechanics covered:
  [V] {mechanic} -- {N} test(s)

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

Assets in symbols.yml NOT referenced:
  {asset_key}

Assertion coverage:
  Dual (bridge + AI):  {N} test(s)
  Bridge-only:         {N} test(s)  [!! if > 0, review for missing AI assertions]
  AI-only:             {N} test(s)

Ambiguities flagged: {N}
  - {id}: {brief description}

Next steps:
  1. Run /run-test --game={game} --suite={suite} --env=dev
  2. Add missing reference images to games/{game}/docs/assets/symbols/
  3. Address flagged missing bridge methods with game dev
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
      ref_assets: [wild, scatter, coin, dragon, phoenix, jade, card_a, card_k, card_q, card_j]
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
      ref_assets: [bet_label]
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
      ref_assets: [scatter, free_spin_popup]
      prompt: "Is the free spin bonus popup visible? How many scatter symbols are on the grid?"
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
`script` runs a shell command — `assert_equal` is not a shell executable. The runner has no cross-step value access. This fails on every platform.

**ALSO BAD — vague AI check (anti-pattern #13):**
```yaml
- id: step-03
  assert:
    ai:
      mode: image
      ref_screenshot: step-02
      ref_assets: [bet_label]
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
      ref_assets: [bet_label]
      prompt: "What is the exact bet amount shown on the bet label? Read the numeric value."
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
          ref_assets: [respin_counter]
          prompt: "Is the game in hold-and-spin mode? What number is displayed on the re-spin counter?"
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
