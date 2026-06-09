# Framework Runner — Technical Deep Dive

A step-by-step, function-by-function walkthrough of how a test suite is parsed, executed, and reported.

> **What changed since the last revision.** The runner gained four major capabilities:
> 1. **Multi-provider AI** — image/video assertions now run on Gemini (default), a Claude Code CLI sub-agent, or a self-hosted HTTP endpoint, selected by the `AI_PROVIDER` env var. The three providers are interface-compatible, so the rest of the pipeline is unchanged.
> 2. **Symbol reference pack** — labelled ground-truth symbol/UI images are loaded once from the game package and prepended to *every* AI request, so the model always has context for what it is looking at.
> 3. **Multi-environment config** — a per-environment `.env.{suffix}` overlay and `env.config.{suffix}.yml` replace the old single-file config. `--env dev|staging|prod|local` selects the set.
> 4. **Data-driven testing** — `groups` cluster tests under a shared setup/paytable, and `runs` play a scenario **once** while emitting a verdict **per case** (one bonus play → four case results), with AI output partitioned across cases via `expected_by_case`.

---

## Table of Contents

1. [Execution Overview](#1-execution-overview)
2. [Multi-Environment Configuration](#2-multi-environment-configuration)
3. [Phase 1 — Load & Validate](#3-phase-1--load--validate)
4. [Phase 2 — AI Provider Selection](#4-phase-2--ai-provider-selection)
5. [Phase 3 — Browser & Video Setup](#5-phase-3--browser--video-setup)
6. [Phase 4 — Suite Setup](#6-phase-4--suite-setup)
7. [Phase 5 — Group / Test / Run Loop](#7-phase-5--group--test--run-loop)
8. [Phase 6 — Single Test Execution](#8-phase-6--single-test-execution)
9. [Phase 7 — Run (Data-Driven) Execution](#9-phase-7--run-data-driven-execution)
10. [Phase 8 — Step Dispatch & Handlers](#10-phase-8--step-dispatch--handlers)
11. [Phase 9 — Bridge Communication](#11-phase-9--bridge-communication)
12. [Phase 10 — AI Image Assertion](#12-phase-10--ai-image-assertion)
13. [Phase 11 — Video Trimming & Copy](#13-phase-11--video-trimming--copy)
14. [Phase 12 — Batch Video AI Processing](#14-phase-12--batch-video-ai-processing)
15. [Phase 13 — Deferred Image AI Processing](#15-phase-13--deferred-image-ai-processing)
16. [Phase 14 — Summary & Reports](#16-phase-14--summary--reports)
17. [Phase 15 — Replay Viewer Generation](#17-phase-15--replay-viewer-generation)
18. [Timing Model](#18-timing-model)
19. [Verdict Computation](#19-verdict-computation)
20. [Symbol Reference & the Detection Package](#20-symbol-reference--the-detection-package)
21. [Error Handling](#21-error-handling)
22. [Complete Execution Flow Diagram](#22-complete-execution-flow-diagram)
23. [Back-Office Paytable Control](#23-back-office-paytable-control)

---

## 1. Execution Overview

Entry point: `framework/runner/suite_runner.py`

```bash
python framework/runner/suite_runner.py --game zeus --suite freespin --env dev
```

This invokes `SuiteRunner.run()` which orchestrates the entire pipeline:

```
Load env (.env + .env.{suffix}) → Load YAML → Validate → Resolve URL
→ Select AI provider → Load symbol reference → Launch browser + video
→ Suite Setup → Group/Test/Run Loop → Suite Teardown
→ Trim Videos → Batch Video AI → Deferred Image AI → Summary → Reports → Replays
```

**Group / Test / Run loop** — a suite is a tree of *groups*, each holding *tests* and/or *runs*:

- A **test** runs its own setup → steps → teardown and produces **one** verdict.
- A **run** is data-driven: it plays one scenario (setup → steps → teardown) **once** but declares N **cases**; each case gets its own verdict. Steps are tagged to the case(s) they prove with `case:`, and one AI call's output is split across cases with `expected_by_case`. This is how four free-spin assertions share a single bonus play instead of replaying it four times.
- A **group** wraps a set of tests/runs with a shared `setup`/`teardown` — typically a `bo_paytable_update` that forces one outcome for the whole group. Top-level ungrouped `tests:`/`runs:` execute first as an implicit group with no group setup/teardown.

**Filtered runs** — `--test <id[,id...]>` runs only those tests/cases and `--group <id[,id...]>` only those groups. Either way the **enclosing group's setup/teardown still runs** around the selected items, so a single test keeps its group's paytable/precondition intact:

```bash
python framework/runner/suite_runner.py --game zeus --suite freespin --env dev --test ZZ-30
python framework/runner/suite_runner.py --game zeus --suite freespin --env dev --group fs-trigger3-countdown
```

---

## 2. Multi-Environment Configuration

`framework/env_files.py` resolves all per-environment paths and loads the right `.env` overlay. The env name (`--env`) maps to a file **suffix**:

| `--env` value | `.env` overlay   | game config file                       |
|---------------|------------------|----------------------------------------|
| `dev`         | `.env.dev`       | `games/{game}/env.config.dev.yml`      |
| `staging` / `stg` | `.env.stg`   | `games/{game}/env.config.stg.yml`      |
| `prod`        | `.env.prod`      | `games/{game}/env.config.prod.yml`     |
| `local`       | `.env.local`     | `games/{game}/env.config.local.yml`    |

(any other env name uses itself as the suffix). `env_file_suffix(env)` does the mapping.

**`.env` loading** (`load_env_files(env, base_dir)`) is called from `SuiteRunner.__init__` before anything else reads config:

1. Load the shared base `.env` (holds cross-env secrets — e.g. `GEMINI_API_KEY`).
2. Overlay `.env.{suffix}` with `override=True` (environment-specific endpoints, BO tokens, Playsite credentials). Overlay values win.

**Game config** (`load_game_config(game, env)`) reads `games/{game}/env.config.{suffix}.yml`. Each file is **self-contained** — there is no shared base and no `environments:` wrapper. A representative file:

```yaml
# games/zeus/env.config.dev.yml
url: "http://192.168.1.13:7456/.../index.html?data=eyJ..."   # used directly if non-empty
url_script: "python scripts/get_game_url_playsite.py --env=dev"  # fallback when url == ""
browser_params: "?bridge=1&debug=1"

playsite:                 # injected as PLAYSITE_* for url_script
  game_code: sl_33_zappin_zeus
  currency: TRY           # omit to use the player's default wallet

bo:                       # back-office identifiers for bo_paytable_* steps (§23)
  game_id: 63
  sas_entity_id: 1
  agent_id: 1
  currency_id: 18
  game_code: SL_33_ZAPPIN_ZEUS

detection:                # standalone symbol-detection config (see §20 — not yet read by the runner)
  method: both
  phash_threshold: 10
  fallback: gemini
```

### Environment variables (`.env` / `.env.{suffix}`)

| Var | Scope | Purpose |
|-----|-------|---------|
| `GEMINI_API_KEY` | base | Gemini API key (image + video AI). Required for `AI_PROVIDER=gemini`. |
| `AI_PROVIDER` | base | `gemini` (default) \| `claude` \| `local` — selects the AI backend (§4). |
| `GEMINI_IMAGE_MODEL` | base | Image model override (default `gemini-2.5-flash`). |
| `GEMINI_VIDEO_MODEL` | base | Video model override (default `gemini-3-flash-preview`). |
| `GEMINI_VIDEO_RESOLUTION` | base | `LOW` \| `MEDIUM` (default) \| `HIGH` — frame token cost. |
| `LOCAL_AI_ENDPOINT` | base | HTTP `/analyze` endpoint, used when `AI_PROVIDER=local`. |
| `CLAUDE_AI_FPS` | base | Video frame-sampling rate for the Claude provider (default `2`). |
| `CLAUDE_CLI_PATH` / `CLAUDE_MODEL` / `CLAUDE_CLI_TIMEOUT` | base | Claude CLI executable / model (default `sonnet`) / per-call timeout (s). |
| `PLAYSITE_API_URL` / `PLAYSITE_USER` / `PLAYSITE_PASS` | overlay | Playsite login used by `url_script` to resolve the game URL. |
| `BO_API_BASE` | overlay | Back-office API base URL for the environment (absent in prod). |
| `BO_TOKEN` / `BO_REFRESH_TOKEN` | overlay | BO access / refresh tokens (grab from BO DevTools). |
| `BO_GAME_ID` / `BO_SAS_ENTITY_ID` / `BO_AGENT_ID` / `BO_CURRENCY_ID` / `BO_GAME_CODE` | overlay | Optional overrides of the game config `bo:` block. |

### Game package layout

```
games/zeus/
├── env.config.dev.yml / .stg.yml / .prod.yml / .local.yml   # per-env config
├── script/get_game_url.py                                   # game-specific login (legacy)
├── paytables/                                                # BO paytable fixtures (JSON)
│   ├── base.json
│   ├── fs-trigger3-countdown.json
│   └── fs-trigger3-addcoin.json ...
├── docs/assets/
│   ├── symbols.yml                                           # symbol + UI reference manifest
│   └── symbols/*.jpg, ui/*.jpg                               # ground-truth images (§20)
├── agent-input/                                              # authoring inputs (bridge-api, paytable)
└── test-suites/*.yml                                         # the suites
```

### URL resolution scripts

| Script | Role |
|--------|------|
| `scripts/get_game_url_playsite.py` | **Active** — game-agnostic Playsite login flow (`--env`, reads `PLAYSITE_*`). Prints only the URL to stdout; logs to stderr. Supports `--base-url` to swap scheme/host (e.g. local IP) while keeping remote-issued tokens. |
| `scripts/get_url.py` | Standalone generic resolver — mirrors `_resolve_game_url` for debugging (`--game`, `--env`). |
| `games/zeus/script/get_game_url.py` | Game-specific guest login (legacy). |

---

## 3. Phase 1 — Load & Validate

### `SuiteRunner.__init__(game, suite_name, env, base_dir)`

1. Auto-detects `base_dir` (repository root) if not provided.
2. Calls `load_env_files(env, base_dir)` — base `.env` then `.env.{suffix}` overlay.
3. Reads `GEMINI_API_KEY` from the now-populated environment.

### `SuiteRunner.run()` — first section

**Step 1: Find suite file**

```python
suite_path = self._find_suite_file()
# Searches games/{game}/test-suites/ for {suite}.yml, {suite}.yaml, or {suite}
```

**Step 2: Load and parse YAML** → `yaml.safe_load()` → dict.

**Step 3: Schema validation**

```python
from framework.schema_validator import validate_suite
errors = validate_suite(suite_doc, source=str(suite_path))
```

`validate_suite()` checks:

| Check | Requirement |
|-------|-------------|
| Root keys | `suite` (str), `game` (str), `version`, `config` (dict), and **at least one** of `tests` / `groups` / `runs` |
| Config | `test_timeout_ms` (int), `suite_timeout_ms` (int); `stop_conditions` (dict) optional |
| Group | `id` (str); optional `title`/`setup`/`teardown`; must contain `tests` and/or `runs` |
| Test | `id` (str, unique), `title` (str), `steps` (list, non-empty) |
| Run | `id` (str, unique), `cases` (non-empty; each `id`+`title`), `steps` (non-empty). Every declared case must be asserted by ≥1 step (`case:` tag or `expected_by_case`) |
| Setup/teardown steps | Each must have `id` (str) and a valid `action` |
| Step `action` | One of the 16 `SUPPORTED_ACTIONS` (§10) |
| Assert blocks | `bridge`: requires `method` + `expected`. `ai`: requires `mode` ∈ {`image`,`video`}, `prompt`, `output_schema`, plus `ref_screenshot` (image) or `video_trim` (video), and `expected` **or** (runs-only) `expected_by_case` |

Returns a list of `ValidationError`. If non-empty, `run()` raises `ValueError` with all errors — the suite does not execute.

**Step 4: Resolve game URL** — `_resolve_game_url()`:

```python
# 1. If env.config.{suffix}.yml has a non-empty `url:`, use it verbatim.
# 2. Otherwise run `url_script` with PLAYSITE_* injected from the playsite: block
#    + the .env overlay; capture stdout as the URL (non-zero exit → ValueError).
# 3. Append browser_params (e.g. ?bridge=1&debug=1), auto-picking ? or & separator.
```

**Step 5: Create output directory** — `_make_run_dir()`:

```
test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/
```

---

## 4. Phase 2 — AI Provider Selection

Before launching the browser, `run()` picks one AI backend. All three expose the **same** `analyse_image()` / `analyse_video()` / `cost_summary()` interface, so `VideoAiProcessor` and `StepExecutor` never know which is active.

```python
provider = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
local_endpoint = os.environ.get("LOCAL_AI_ENDPOINT", "").strip()

# Built ONCE; prepended to every AI image/video request (see §20).
symbol_reference = load_symbol_reference(self.game, self.base_dir)

if provider == "local" and local_endpoint:
    gemini_image        = LocalImageHelper(local_endpoint, reference=symbol_reference)
    gemini_video_helper = LocalVideoHelper(local_endpoint, reference=symbol_reference)
elif provider == "claude":
    gemini_image        = ClaudeImageHelper(reference=symbol_reference)
    gemini_video_helper = ClaudeVideoHelper(reference=symbol_reference)
elif self._api_key:                                  # "gemini" (default)
    gemini_video_helper = GeminiVideoHelper(self._api_key, model=video_model,
                                            resolution=video_resolution, reference=symbol_reference)
    gemini_image        = GeminiImageHelper(self._api_key, model=image_model, reference=symbol_reference)
else:
    logger.warning("AI provider unavailable — AI assertions will be skipped")
```

| Provider | Module | How it analyses | Cost |
|----------|--------|-----------------|------|
| **gemini** (default) | `helpers/gemini.py`, `helpers/gemini_video.py` | Direct `google.genai` API call. Image: `gemini-2.5-flash`; video: `gemini-3-flash-preview` (uploaded via Files API). | Per-token; tracked via `gemini_pricing.py`. |
| **claude** | `helpers/claude_ai.py` | Samples video to frames (ffmpeg), writes `ASSETS.md`, spawns `claude -p … --output-format json` with Read/Glob pre-approved; the sub-agent reads frames + emits JSON. | Free (local Claude auth). |
| **local** | `helpers/local_ai.py` | `POST {endpoint}/analyze` multipart (file + prompt); expects `{"result": "<json>"}`. Symbol descriptions sent as text (endpoint takes one file). | Free (self-hosted). |

The `cost_summary()` of every provider returns the same `gemini_*` keys (Claude/local report zeros) so the suite summary aggregates uniformly regardless of backend.

> **Note on the variable names.** `gemini_image` / `gemini_video_helper` are historical — they hold whichever provider was selected, not necessarily a Gemini object.

---

## 5. Phase 3 — Browser & Video Setup

```python
async with async_playwright() as pw:
    browser = await pw.chromium.launch(headless=True)
    video_tmp_dir = run_dir / "videos"
    context = await browser.new_context(
        viewport={"width": 531, "height": 919},
        record_video_dir=str(video_tmp_dir),
        record_video_size={"width": 531, "height": 919},
    )
    page = await context.new_page()
    video_start_time = time.time()  # Unix timestamp when recording begins
```

- Chromium launches headless; the context records a single WebM to `video_tmp_dir`.
- Viewport is 531×919 (mobile game dimensions).
- `video_start_time` anchors later timeline alignment.

**Bridge and network helpers:**

```python
bridge  = BridgeHelper(page)      # wraps window.__gameTestBridge
network = NetworkHelper(context)  # offline/online control
```

An optional `page.on("response", …)` logger records non-static API responses (filters `.js`, `.css`, images, etc.).

---

## 6. Phase 4 — Suite Setup

If the suite YAML has a top-level `setup` block:

```python
setup_artifact = ArtifactContext(run_dir=run_dir, test_id="_setup")
setup_executor = StepExecutor(bridge=bridge, network=network, artifact=setup_artifact,
                              gemini_image=gemini_image, page=page, game_url=self._game_url, bo=bo)
await _run_steps(setup_executor, suite_setup, "suite-setup")
```

A typical suite setup authenticates the BO, navigates, and starts the game:

```yaml
setup:
  - id: setup-01
    action: bo_paytable_ensure
    params: { paytable_code: AutomationTest }      # idempotent (§23)
  - id: setup-02
    action: bo_paytable_select
    params: { paytable_code: AutomationTest }
  - id: setup-03
    action: navigate
    params: { url_script: "get_game_url" }          # use the URL resolved at startup
  - id: setup-04
    action: wait_for_state
    params: { state: ready, timeout_ms: 30000 }
  - id: setup-05
    action: bridge_action
    params: { name: startGame, args: [] }           # enter GameplayScene
```

**`_run_steps()` helper** iterates steps calling `executor.execute(step)`, returning `(all_passed, first_error, pending_steps)`. On a step failure it stops the remaining steps but teardown still runs.

---

## 7. Phase 5 — Group / Test / Run Loop

The suite is flattened into an ordered list of groups; each group yields tests and runs. Pseudocode:

```python
for group in self._iter_groups():           # implicit group for top-level tests/runs
    await _run_steps(group_setup_executor, group.setup, f"{group.id}/setup")
    for item in group.tests_and_runs:
        if isinstance(item, Test):
            await self._run_one_test(record, ...)        # §8
        else:                                            # Run
            await self._run_one_run(run_record, ...)     # §9
        # write result(s), notify video processor, check stop conditions
    await _run_steps(group_teardown_executor, group.teardown, f"{group.id}/teardown")
```

Each test/run is wrapped in `asyncio.wait_for(..., timeout=test_timeout_ms/1000)`. On `TimeoutError` the record is marked `timed_out=True`, `final_verdict="red"`.

**After each item:**

1. **Write result** — `artifact.write_result()` (test, also emits `result.html`) or `artifact.write_run_result()` + `write_case_report()` per case (run).
2. **Notify video processor** — `notify_test_complete(record)` / `notify_run_complete(record)` if it has video AI steps.
3. **Check stop conditions:**

| Condition | Behavior |
|-----------|----------|
| `fail_fast: true` | Stop on first red verdict |
| `max_total_failures: N` | Stop after N total red verdicts |
| `max_consecutive_failures: N` | Stop after N consecutive red verdicts |

---

## 8. Phase 6 — Single Test Execution

### `SuiteRunner._run_one_test(record, page, context, bridge, network, gemini_image, run_dir)`

**Step 1 — Artifact context:** `ArtifactContext(run_dir, test_id)` creates `{run_dir}/{test_id}/`.

**Step 2 — Start event collection:**

```python
await bridge.start_event_collection()
record.event_collection_start = time.time()
```

Registers JavaScript hooks inside the game that capture:

| Event Type | Source |
|------------|--------|
| `state_change` | `ApplicationStateManager.onStateChanged` |
| `spin_result` | `GameLogic.betResult` setter |
| `balance_change` | After spin result |
| `button_state` | Sampled at state changes |
| `free_spin` | `GameLogic.freeSpins` change |
| `bonus_transition` | SpinState changes |
| `popup` | Popup `.active` changes |
| `turbo_change` | Turbo toggle |
| `error` | AppState.ERROR |

**Step 3 — Step executor:**

```python
executor = StepExecutor(bridge=bridge, network=network, artifact=artifact,
                        gemini_image=gemini_image, page=page, game_url=self._game_url,
                        test_start_time=time.time(), bo=bo)
```

`test_start_time` is the reference for all step `start_ms` / `end_ms` values.

**Step 4 — Test lifecycle:**

```python
await _run_steps(executor, test_doc.get("setup", []), f"{test_id}/setup")
await bridge.start_event_collection()          # re-register (setup may reload page)
record.event_collection_start = time.time()
all_ok, err, pending = _run_steps(executor, test_doc["steps"], test_id)
await _run_steps(executor, test_doc.get("teardown", []), f"{test_id}/teardown")  # always runs
```

**Step 5 — Final event drain:** `events = await bridge.drain_events()` → `artifact.append_events(events)`.

**Step 6 — Write timeline:** `artifact.write_timeline(video_start_time, event_collection_start, steps=executor.timeline)`, computing `video_event_offset = event_collection_start - video_start_time`.

**Step 7 — Compute verdict:**

```python
record.bridge_result = "pass" if all_ok else "fail"
record.ai_result     = "pending" if pending else ("pass" if all_ok else "skip")
record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)
record.duration_ms   = int((time.time() - test_start) * 1000)
```

---

## 9. Phase 7 — Run (Data-Driven) Execution

A **run** plays a scenario once and reports a verdict **per case**. It exists to avoid replaying an expensive scenario (e.g. a full Hold & Spin bonus) once per assertion.

**Authoring shape** (abridged from `games/zeus/test-suites/freespin-runs.yml`):

```yaml
runs:
  - id: fs-3coin-countdown-run
    cases:
      - { id: ZZ-30, title: "Trigger FS with 3 coins -> 3 free spins" }
      - { id: ZZ-32, title: "Coins held fixed across re-spins" }
      - { id: ZZ-33, title: "Only non-coin cells re-spin" }
      - { id: ZZ-35, title: "No new coin -> spin until FS runs out" }
    setup: [...]            # run once
    steps:
      - id: step-02         # tagged to ONE case
        case: ZZ-30
        assert: { bridge: { method: getFreeSpinsRemaining, expected: 3 } }

      - id: step-07         # a shared invariant — tagged to ALL cases
        case: [ZZ-30, ZZ-32, ZZ-33, ZZ-35]
        action: bridge_assert
        params: { method: verifyHoldSpinCoinsHeld, args: ["$events"], path: valid, expected: true }

      - id: step-10         # ONE video AI call → output split across cases
        assert:
          ai:
            mode: video
            video_trim: { from_step: step-01, to_step: step-04 }
            ref_assets: [coin, free_spin_counter, hold_spin_overlay]
            prompt: "...judging the BONUS phase only..."
            output_schema: { entered_bonus: {type: boolean}, coin_count: {type: number}, ... }
            expected_by_case:
              ZZ-30: { entered_bonus: true, coin_count: 3, confidence: high }
              ZZ-32: { coins_stay_locked: true, coins_moved_or_lost: false }
              ZZ-33: { only_noncoin_cells_spin: true, coin_cell_respun: false }
              ZZ-35: { counted_down_to_zero: true, ever_reset: false }
    teardown: [...]
```

**Semantics:**

- **`case:` tag** on a step routes that step's pass/fail to one or more cases. A bridge step with `case: ZZ-30` only affects ZZ-30's bridge verdict; an untagged action step (a spin, a wait) contributes to execution but no specific case.
- **`expected_by_case`** (AI block, runs-only) partitions a *single* model response across cases — one trim, one API call, four case verdicts. Each case is graded only against its own field subset.
- `artifact.write_run_result(cases, extra)` writes one `result.json` with `kind: "run"` and a `cases[]` array; `write_case_report(case, …)` emits a per-case `case-{id}.html`. The reporter later expands the run into one row per case.

---

## 10. Phase 8 — Step Dispatch & Handlers

### `StepExecutor.execute(step: dict) -> StepResult`

```python
start_ms = self._elapsed_ms()        # ms since test start
result   = await self._dispatch(step)  # route to handler
end_ms   = self._elapsed_ms()
```

After dispatch the executor:

1. Records a timeline entry `{id, action, description, start_ms, end_ms, result, saved?}`.
2. Emits a synthetic `"step"` event to `events.jsonl`.
3. Drains bridge events (except after `navigate`, which destroys bridge registration).
4. If the step has `save: <name>` **and** succeeded, stores `result.result` in the executor's per-test `vars` map.

### Step-level modifiers (apply to many actions)

| Modifier | Effect |
|----------|--------|
| `save: <name>` | Copy this step's return value into `vars[name]`, scoped setup → steps → teardown. Used to hand a captured event stream to an analyser. |
| `args: [...]` | Ordered positional args for `bridge_action` / `bridge_assert`. A string `"$name"` resolves to `vars[name]`; `"$$x"` escapes to literal `"$x"`. Resolved by `_resolve_args()`. |
| `path: <dotpath>` | (`bridge_assert`) drill into the returned object before comparing — `valid`, or `a.b.0` (int segment = list index). |
| `capture: true` | Take a screenshot during the step (supported by `navigate`, `wait_for_state`, `bridge_action`). |
| `case: <id|[ids]>` | (runs only) route this assert step's verdict to the named case(s). |

### The 16 supported actions

| Action | Handler | Key params | Behaviour |
|--------|---------|------------|-----------|
| `navigate` | `_handle_navigate` | `url` **or** `url_script` | `page.goto(url, wait_until="networkidle")`. `url_script` ⇒ use the startup-resolved `game_url`. Does **not** drain events after (reload kills the bridge). |
| `wait_for_state` | `_handle_wait_for_state` | `state`, `timeout_ms`(=30000) | Poll `getAppState()` every 200 ms until match/timeout. |
| `bridge_action` | `_handle_bridge_action` | `name`, `args`? / kwargs | With `args`: call `window.__gameTestBridge.{name}(...args)` directly, keep return value. Without: dispatch to the snake_case `BridgeHelper` wrapper, else `triggerAction()`. |
| `bridge_assert` | `_handle_bridge_assert` | `method`, `expected`, `args`?, `path`? | Call the bridge method (args or wrapper), optionally `path`-drill, compare `==`. |
| `capture` | `_handle_capture` | — | `page.screenshot(type="jpeg")` → `step-{NN}.jpg`. |
| `assert` | `_handle_assert` | `assert.{bridge,ai}` | Bridge block runs inline; AI block is **deferred** (marked `ai_pending`). |
| `script` | `_handle_script` | `cmd` | `page.evaluate()` with injected `steps`, `vars`, `bridge`, `assert()`, `assert_equal()`. Last resort. |
| `click` | `_handle_click` | `selector` **or** `x`+`y` | CSS selector or canvas coordinate click. |
| `press_key` | `_handle_press_key` | `key` | `keyboard.press(key)`. |
| `wait_ms` | `_handle_wait_ms` | `ms` | `asyncio.sleep(ms/1000)`. |
| `go_offline` / `go_online` | `_handle_go_offline` / `_on` | — | `network.go_offline()` / `go_online()`. |
| `bo_paytable_ensure` | `_handle_bo_paytable_ensure` | `paytable_code`?, `paytable_ref`?, `paytable`?, `rtp`? | Idempotently create the automation paytable on the master game (§23). |
| `bo_paytable_select` | `_handle_bo_paytable_select` | `paytable_code`, `agent_id`?, … | Point the agent's game at that paytable. |
| `bo_paytable_update` | `_handle_bo_paytable_update` | `paytable_code`, `paytable_ref`/`paytable` | Rewrite the paytable's content (per group). |
| `bo_paytable_reset` | `_handle_bo_paytable_reset` | `agent_id`?, … | Restore the agent's game default (teardown). |

> AI assertions are **deferred** — they don't run inline. This lets all bridge assertions finish first, lets the video be trimmed before analysis, and enables batch processing for cost efficiency.

---

## 11. Phase 9 — Bridge Communication

### `framework/helpers/bridge.py` — `BridgeHelper`

All bridge methods funnel through `_eval()`:

```python
async def _eval(self, expr: str) -> Any:
    exists = await self._page.evaluate("typeof window.__gameTestBridge !== 'undefined'")
    if not exists:
        raise BridgeMissingError()
    return await self._page.evaluate(expr)
```

**Action example — `spin()`** awaits a Promise that resolves when the game returns to idle. **Query example — `get_balance()`** returns `getBalance()`.

**`wait_for_state()`** polls `get_app_state()` every 200 ms, swallowing `BridgeMissingError` (bridge not yet registered during load) until match or `BridgeTimeoutError`.

**`drain_events()`** returns `[{t, type, data}, …]` accumulated since the last drain.

The `args`-form calls (`startEventCapture`, `endEventCapture`, `spinUntilBonus`, `filterBonusEvent`, `verifyHoldSpin*`, …) need no Python wrapper — the bridge JS method is invoked directly with `_resolve_args`-substituted arguments, and for analysers the bridge owns the comparison logic (the YAML only asserts a `path` into the result).

---

## 12. Phase 10 — AI Image Assertion

### `framework/helpers/gemini.py` — `GeminiImageHelper`

**`GeminiImageHelper(api_key, model=None, reference=None)`** — `model` defaults to `gemini-2.5-flash` (overridable via `GEMINI_IMAGE_MODEL`); `reference` is the `SymbolReference` pack (§20).

**`analyse_image(image_path, prompt, output_schema, extra_images=None) -> GeminiResult`:**

```python
client = genai.Client(api_key=self.api_key)        # google.genai
contents = [*self._reference_parts(),               # labelled ground-truth images first
            Image.open(image_path),
            *[Image.open(p) for p in (extra_images or [])],
            f"{prompt}\n\nRespond ONLY with valid JSON matching: {json.dumps(output_schema)}."]
response = client.models.generate_content(model=self._model, contents=contents)
```

**`GeminiResult`** carries `success`, `data`, `reason`, `input_tokens`, `output_tokens`, `raw_text`. `_parse_response()` accumulates token usage, strips any ```​json fence, and `json.loads()` the result. Failures return `success=False` (never raise).

**Cost tracking** — `cost_summary()` returns `{model, gemini_image_calls, gemini_input_tokens, gemini_output_tokens, estimated_cost_usd}`. Pricing comes from `gemini_pricing.py` (`gemini-2.5-flash`: $0.30 / $2.50 per 1M in/out). The Claude and local helpers expose the same method but report zeros.

---

## 13. Phase 11 — Video Trimming & Copy

After all tests complete and the context closes (finalizing the WebM):

```python
video_path = Path(await page.video.path())   # before closing
await context.close(); await browser.close()

if video_path and video_path.exists():
    for r in self._records:
        if r.artifact and r.artifact.test_dir.exists():
            shutil.copy2(video_path, r.artifact.video_path)   # {test_dir}/session.webm
    shutil.rmtree(video_path.parent)                          # clean shared videos/
```

Each test/run gets the full session video as `session.webm`; the `VideoAiProcessor` later trims it to the relevant window per video-AI step.

---

## 14. Phase 12 — Batch Video AI Processing

### `framework/runner/video_ai_processor.py` — `VideoAiProcessor`

**Construction** scans the suite for every step with `assert.ai.mode == "video"` and groups them by their `ref_assets` set (`_group_key()` = sorted, comma-joined). `run_test_ids` scopes the groups to the tests this run will actually execute (so a `--test`/`--group` filter still dispatches video AI instead of hanging in `pending`).

```python
VideoAiProcessor(game, suite_doc, base_dir,
                 gemini_video_helper=gemini_video_helper,
                 run_test_ids=self._compute_run_test_ids())
```

**Lifecycle:**

- `notify_test_complete(record)` / `notify_run_complete(record)` — mark an item done; when a group is fully complete it is queued.
- `wait_all()` (after suite teardown) — dispatches all ready groups and runs as asyncio tasks and awaits them.

**`_process_test(record, context_id)` — per video-AI step:**

```python
# 1. Resolve the trim window from timeline.json using video_trim {from_step, to_step},
#    adjusted by video_event_offset.
start_s, end_s = _resolve_trim_timestamps(timeline, from_step, to_step)

# 2. Trim with ffmpeg (stream copy, no re-encode):
#    ffmpeg -y -ss {start_s} -to {end_s} -i session.webm -c copy trimmed-{step}.webm
_trim_video(source, dest, start_s, end_s)

# 3. Analyse — the symbol reference is prepended inside the helper, so no per-group
#    asset upload is needed (create_cached_context is a no-op returning "inline").
result = gemini.analyse_video(trimmed_path, None, prompt, output_schema)

# 4. Compare each expected field; passed = (no mismatches).
# 5. Write step-{NN}-ai.json {passed, data, expected, mismatches, tokens}.
# 6. Update record.ai_result, recompute final_verdict, rewrite result.json.
```

**`_process_run(record)`** does the same trim+analyse once, then grades the response **per case** via `expected_by_case` (or a shared `expected`), writing per-case results into `step-{NN}-ai.json` and recomputing each case's verdict.

### `framework/helpers/gemini_video.py` — `GeminiVideoHelper`

**`GeminiVideoHelper(api_key, model=None, resolution=None, reference=None)`** — `model` defaults to `gemini-3-flash-preview`; `resolution` ∈ {LOW, MEDIUM(default), HIGH} sets media resolution (frame token cost). `analyse_video()` uploads via the Files API, polls until `ACTIVE`, prepends the reference pack, appends the video + prompt, and returns a `VideoResult`. Pricing: $0.50 / $3.00 per 1M in/out (~70 tokens/frame at low/medium). `create_cached_context()` / `release_context()` are retained as no-ops for interface compatibility.

---

## 15. Phase 13 — Deferred Image AI Processing

After video AI, pending **image** AI assertions run:

```python
await self._process_pending_image_ai(gemini_image)
```

For each test with a pending image step:

```python
screenshot_path = artifact.screenshot_path(ref_num)        # from ref_screenshot
result = await asyncio.to_thread(gemini_image.analyse_image, screenshot_path, prompt, output_schema)
mismatches = [f"{k}: got {actual.get(k)}, expected {v}" for k, v in expected.items() if actual.get(k) != v]
passed = not mismatches
# write step-{NN}-ai.json; update record.ai_result; rewrite result.json + result.html
```

(Runs through the *selected provider's* `analyse_image`, not necessarily Gemini.)

---

## 16. Phase 14 — Summary & Reports

```python
summary = self._build_summary(suite_start, stop_reason, gemini_image, gemini_video)
```

`_build_summary()` flattens runs into one entry per case, counts verdicts, and aggregates costs from each provider's `cost_summary()`:

```json
{
  "suite": "Free Spin (Hold & Spin) — runs demo",
  "game": "zeus", "env": "dev",
  "run_date": "2026-06-08 10:54:00", "duration_ms": 45000,
  "total": 4, "counts": { "green": 3, "orange": 1, "red": 0, "pending": 0, "skip": 0 },
  "stop_reason": null,
  "tests": [ { "test_id": "ZZ-30", ... }, ... ],
  "costs": {
    "gemini_image_calls": 0, "gemini_video_calls": 1,
    "gemini_input_tokens": 15000, "gemini_output_tokens": 2500,
    "estimated_cost_usd": 0.0125
  }
}
```

Written to `{run_dir}/summary.json`. HTML outputs:

| File | Generator | Contents |
|------|-----------|----------|
| `summary.html` | `SuiteRunner._write_summary_html()` | Human-readable suite overview with verdict dots. |
| `report.html` | `Reporter._write_report_html()` | Suite table: verdict, id, title, bridge/ai detail, duration, links to replay + video; failure screenshots inlined. Runs expand to one row per case. |
| `result.html` | `ArtifactContext.write_result()` | Per-test verdict card. |
| `case-{id}.html` | `ArtifactContext.write_case_report()` | Per-case card for runs. |

`Reporter.generate()` re-scans every `result.json` (skipping `_*` setup/teardown pseudo-dirs), re-computes verdicts, and (re)writes `summary.json`, `report.html`, and a `replay.html` per test.

---

## 17. Phase 15 — Replay Viewer Generation

Before generating replays, suite-setup events are prepended to each test's `events.jsonl` (timestamps adjusted by `video_event_offset`) so the replay shows navigation + game start before the test's own events.

**`Reporter._write_replay_html(entry, video_file="session.webm")`:**

1. Load `events.jsonl` and `timeline.json`.
2. Build a `replay_data` dict (test_id, title, final_verdict, bridge_result, ai_result, duration_ms, error, video_file, video_event_offset, steps[], events[]).
3. Read `framework/templates/replay.html` (the sole template) and replace `%%REPLAY_DATA%%` with the JSON.
4. Write `{test_dir}/replay.html`.

**Replay viewer features (client-side JS):**

| Feature | Description |
|---------|-------------|
| Video player | Seek bar, play/pause, speed (0.5×/1×/2×) |
| Event log | Color-coded badges per type, scrolls with video |
| Click-to-detail | Event → popup with full JSON + copy |
| Seek to event | Popup "Seek" jumps the video to that timestamp |
| Step timeline | Bottom bar of step segments, click to jump |
| Filter bar | Toggle event types on/off |
| Future toggle | Show/hide not-yet-reached events |

---

## 18. Timing Model

| Timestamp | When Set | Used By |
|-----------|----------|---------|
| `video_start_time` | Browser context creation | Timeline offset |
| `event_collection_start` | `bridge.start_event_collection()` | Event `t` reference |
| `test_start_time` | Start of `_run_one_test()` | Step `start_ms` / `end_ms` |

**Key formula:** `video_event_offset = event_collection_start - video_start_time`. In the replay viewer:

```javascript
videoTime = event.t + videoEventOffset
videoTime = step.start_ms / 1000 + videoEventOffset
```

---

## 19. Verdict Computation

```python
def _compute_verdict(bridge_result, ai_result):
    if bridge_result == "skip" and ai_result == "skip": return "skip"
    if ai_result == "pending":                          return "pending"
    if bridge_result == "pass" and ai_result in ("pass", "skip"): return "green"
    if bridge_result == "fail" and ai_result == "fail": return "red"
    if bridge_result == "fail" or ai_result == "fail":  return "orange"
    return "green"
```

| Bridge | AI | Verdict | Meaning |
|--------|----|---------|---------|
| pass | pass | green | Both agree — passed |
| pass | skip | green | Bridge passed, no AI assertion |
| fail | fail | red | Both agree — failed |
| pass | fail | orange | AI disagrees — human review |
| fail | pass | orange | Bridge disagrees — check implementation |
| any | pending | pending | Video AI not yet processed |
| skip | skip | skip | No assertions |

The identical function is duplicated in `reporter.py` and `video_ai_processor.py` to avoid cross-module side effects.

---

## 20. Symbol Reference & the Detection Package

### Symbol reference (`framework/helpers/symbol_reference.py`) — **active**

`load_symbol_reference(game, base_dir)` reads `games/{game}/docs/assets/symbols.yml`, iterating its `symbols:` and `ui_elements:` sections. For each entry whose `file:` exists, it adds `(image_path, "Asset '{key}' — {name}: {description}")` to a `SymbolReference`. Missing files are logged and skipped (never fatal).

The pack is built **once per run** and passed to whichever AI provider is active:

- Gemini / Claude: the labelled images are prepended as content **before** the screenshot/video, so the model has ground-truth references for every symbol and UI element.
- Local endpoint: `descriptions_text()` (header + labels) is prepended as text, since the endpoint takes a single media file.

`SymbolReference` exposes `header`, `items: list[(Path, str)]`, `is_empty`, and `descriptions_text()`.

### Detection package (`framework/detection/`) — **standalone utility**

A separate symbol-*identification* toolkit (distinct from the assertion path above). It is **not currently wired into `SuiteRunner` or `VideoAiProcessor`** (the `detection:` block in `env.config.*.yml` and `detection_method` in a suite's `config:` are presently unconsumed) — it is exercised by `tests/test_detection.py` and the `scripts/smoke_*` helpers, and is the substrate for future in-pipeline detection.

| Module | Class | Role |
|--------|-------|------|
| `phash.py` | `PhashDetector(manifest_path, threshold=10)` → `PhashResult{symbol, hamming_distance, confident}` | Pre-hashes all reference symbols (`imagehash.phash`); matches a crop by Hamming distance. ~1–2 ms, zero cost. |
| `gemini_detect.py` | `GeminiDetector(manifest_path, api_key)` → `GeminiDetectResult{symbol, confidence, raw_response}` | Sends a crop + all reference images to `gemini-2.5-flash` and asks which symbol it is. |
| `config.py` | `DetectionConfig(manifest_path, method="both", phash_threshold=10, api_key="")` → `DetectionResult{symbol, confidence, flagged_orange, …}` | Dispatcher. `both` runs pHash first; if confident, returns immediately, else falls back to Gemini. Disagreement sets `flagged_orange=True` (human review) and trusts Gemini's symbol. |

---

## 21. Error Handling

| Scenario | Handling |
|----------|----------|
| Bridge not registered | `BridgeMissingError` — caught in `wait_for_state()`, retried until timeout |
| Test/run timeout | `asyncio.wait_for()` → `TimeoutError` → `timed_out=True`, verdict red |
| AI provider unavailable | No provider configured (no key/endpoint) → warning logged, AI asserts skipped (not fatal) |
| AI call failure | Provider returns `success=False` result — never raises |
| ffmpeg missing/timeout | `_trim_video()` returns False; the step's AI result records the failure |
| Bridge method not found | Falls back to `triggerAction()` JS eval |
| Step exception | Caught in `execute()`, stored in `StepResult.error` |
| Event drain failure | Caught silently, does not fail the step |
| YAML validation error | All errors collected and raised before any test runs |
| Missing reference image | Logged and skipped; the reference pack is still built |

---

## 22. Complete Execution Flow Diagram

```
SuiteRunner.run()
│
├── load_env_files(env)          → .env + .env.{suffix} overlay
├── _find_suite_file()           → suite.yml path
├── _load_suite()                → yaml.safe_load()
├── validate_suite()             → check schema, stop if errors
├── _resolve_game_url()          → static url: or url_script + browser_params
├── Select AI provider           → gemini | claude | local  (interface-compatible)
├── load_symbol_reference()      → ground-truth pack for every AI request
├── BoHelper(game, env)          → lazy back-office client
├── VideoAiProcessor(...)        → group video-AI steps by ref_assets
├── _make_run_dir()              → test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/
│
├── Launch Playwright + video recording   → video_start_time = time.time()
│
├── Suite Setup (_run_steps)     → bo_paytable_ensure/select → navigate → wait ready → startGame
│
├── FOR EACH GROUP:
│   ├── group setup (_run_steps)  → e.g. bo_paytable_update (force this group's outcome)
│   │
│   ├── FOR EACH TEST:
│   │   └── _run_one_test()
│   │       ├── ArtifactContext → {test_id}/
│   │       ├── bridge.start_event_collection()
│   │       ├── StepExecutor.execute() per step:
│   │       │   ├── _dispatch() → handler (navigate / wait_for_state / bridge_action /
│   │       │   │                bridge_assert / capture / assert / script / click /
│   │       │   │                press_key / wait_ms / go_offline|online / bo_paytable_*)
│   │       │   ├── record timeline + emit "step" event
│   │       │   ├── apply save:/args:/path:/capture: modifiers
│   │       │   └── drain_events()
│   │       ├── write_timeline()
│   │       └── _compute_verdict(bridge, ai)
│   │
│   ├── FOR EACH RUN:
│   │   └── _run_one_run() → play once; grade per case (case: + expected_by_case)
│   │
│   ├── write_result()/write_run_result() + result.html / case-*.html
│   ├── notify_test_complete() / notify_run_complete()
│   ├── _check_stop_conditions()
│   └── group teardown (_run_steps)
│
├── Suite Teardown (_run_steps)   → bo_paytable_reset
│
├── Copy session video → each test dir as session.webm; rm shared videos/
│
├── await video_processor.wait_all()
│   └── per group → per test/run: trim (ffmpeg) → analyse_video() → write AI result
│       (symbol reference prepended inside the helper; cached context is a no-op)
│
├── await _process_pending_image_ai()   → screenshot → analyse_image() → update verdict
│
├── _build_summary() → summary.json + summary.html
├── Prepend setup events to each test's events.jsonl
├── Reporter → report.html + replay.html per test (from template)
│
└── Return summary dict → CLI prints results table + exit code
```

---

## 23. Back-Office Paytable Control

Bridge injection (`setDevModeResult`) forces outcomes **client-side**. When a test must validate the **real server result path** — the spin RNG resolved by the back-office against a controlled paytable — the runner drives the BO instead. This is how a suite gets *deterministic server results*.

### `framework/helpers/bo.py` — `BoHelper`

`BoHelper(game, env, base_dir)` is constructed once per run and passed to every `StepExecutor` (suite setup, each test, suite teardown). Construction is cheap and never touches the network; the underlying `BoApiClient` is built lazily on the first BO step, so suites without BO steps need no BO config.

It wraps three lower-level modules (auth + token refresh handled transparently):

| Module | Role |
|--------|------|
| `framework.bo.bo_api_client.BoApiClient` | Transport + automatic token refresh (`TOKEN_EXPIRED` → `auth/refresh-token` → retry). |
| `framework.bo.update_game_paytable` | Master-game paytable: `ensure_game_paytable` (create the automation paytable), `update_game_paytable` (edit its content). |
| `framework.bo.agent_game_settings` | Per-agent selection: `set_agent_paytable` / `reset_agent_paytable_to_default`. |

### The three-layer model

```
1. ENSURE   bo_paytable_ensure   → AutomationTest paytable exists on the master game
              (idempotent; suite setup, once — creating a paytable resubmits the game record)
2. SELECT   bo_paytable_select   → agent's game plays AutomationTest (real spins use it)
   RESET    bo_paytable_reset    → agent's game back on the game default (suite teardown)
3. UPDATE   bo_paytable_update   → rewrite AutomationTest's CONTENT per group of test cases
              (base game / free spin / …) so each group gets its forced result
```

**Group test cases by paytable.** Switch the paytable *content* once per group, not once per test — a content edit resubmits the whole game record and is slow. With `groups`, put the `bo_paytable_update` in the **group's** `setup` (see §7 / `freespin-runs.yml`), so the whole group shares one content switch.

### Configuration — split by scope

**Game-scoped** identifiers live in the per-environment game config `games/{game}/env.config.{suffix}.yml` under a top-level `bo:` block (committed with the game, not secret):

```yaml
# games/zeus/env.config.dev.yml
bo:
  game_id: 63            # master game (ensure, update)
  sas_entity_id: 1       # master game SAS entity (ensure, update)
  agent_id: 1            # agent the suite plays on (select, reset)
  currency_id: 18        # currency id of the agent's game row (select, reset)
  game_code: SL_33_ZAPPIN_ZEUS   # exact game code (select, reset)
```

**Environment-scoped** values (endpoint + secrets) live in the per-environment `.env.{suffix}` overlay:

| Var | Meaning |
|-----|---------|
| `BO_API_BASE` | BO API base URL for the environment |
| `BO_TOKEN` / `BO_REFRESH_TOKEN` | access (ctoken) / refresh (rtoken) tokens — grab from BO DevTools |

`BoHelper` resolves each game-scoped value as: `BO_*` env var (override) → `env.config.{suffix}.yml` `bo:` block. Any value is also overridable per step via params. The rotated access token is cached to `.bo_token.{suffix}.json` (gitignored) so it survives across runs; the cache is cleared after each suite run.

### Paytable content fixtures

`bo_paytable_update` / `bo_paytable_ensure` resolve content from `games/{game}/paytables/{ref}.json` via `paytable_ref`, or accept an inline `paytable` dict/list/JSON string. Create one fixture per group (e.g. `base.json`, `fs-trigger3-countdown.json`, `fs-trigger3-addcoin.json`). `BoHelper.resolve_paytable(ref, inline)` does the lookup.

> The BO Python modules live in the `framework/bo/` package. `framework.helpers.bo.BoHelper` imports them and exposes the `bo_paytable_*` steps; this section documents the integrated behaviour you call from YAML. See `docs/bo-integration.md` for the lower-level API.

---

*Source files: `suite_runner.py`, `step_executor.py`, `bridge.py`, `gemini.py`, `gemini_video.py`, `gemini_pricing.py`, `claude_ai.py`, `local_ai.py`, `symbol_reference.py`, `artifact.py`, `video_ai_processor.py`, `reporter.py`, `schema_validator.py`, `env_files.py`, `detection/{config,phash,gemini_detect}.py`, `helpers/bo.py`, `bo/{bo_api_client,update_game_paytable,agent_game_settings}.py`*
