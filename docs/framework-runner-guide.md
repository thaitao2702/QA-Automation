# Framework Runner Guide

How to execute test suites, process video AI assertions, and generate reports using the QA platform framework.

---

## Prerequisites

- **Python 3.12+** with dependencies installed (`pip install -r requirements.txt`)
- **Playwright browsers** installed (`python -m playwright install chromium`)
- **ffmpeg** on system PATH (required for video AI trimming)
- **GEMINI_API_KEY** environment variable set (required for AI assertions; bridge-only tests work without it)

---

## Quick Start

```bash
# Run a test suite
python framework/runner/suite_runner.py --game zeus --suite suite-01-core --env dev

# Re-process video AI results (without re-running tests)
python framework/runner/video_ai_processor.py --results-dir test-results/zeus/dev/2026-04-10-suite-01-core --game zeus

# Re-generate reports from existing results
python framework/runner/reporter.py --results-dir test-results/zeus/dev/2026-04-10-suite-01-core
```

---

## Suite Runner

The main entry point for test execution. Launches a browser, runs all tests in a YAML suite, collects results, and produces a summary.

### Usage

```bash
python framework/runner/suite_runner.py --game GAME --suite SUITE --env ENV [--base-dir DIR]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `--game` | Yes | Game identifier (e.g., `zeus`). Must match a directory under `games/`. |
| `--suite` | Yes | Suite file name without extension (e.g., `suite-01-core`). Looked up in `games/{game}/test-suites/`. |
| `--env` | Yes | Environment name (e.g., `dev`, `staging`, `prod`). Must match an entry in the game's `env.config.yml`. |
| `--base-dir` | No | Repository root. Auto-detected from script location if omitted. |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All tests passed (no red verdicts) |
| 1 | One or more red verdicts, or a startup error (missing config, validation failure) |

### What It Does

1. **Loads the suite YAML** from `games/{game}/test-suites/{suite}.yml`
2. **Validates** the YAML against the schema (reports errors and exits if invalid)
3. **Resolves the game URL** from `games/{game}/env.config.yml` using the specified environment
4. **Launches Playwright Chromium** with video recording enabled
5. **Runs suite-level setup** steps (typically: navigate to game URL, wait for idle state)
6. **For each test:**
   - Starts bridge event collection
   - Runs test setup → steps → teardown
   - Drains bridge events after each step
   - Saves `result.json` with bridge and AI verdicts
   - Enforces per-test timeout (kills context on expiry, saves partial artifacts)
   - Checks stop conditions after each test
7. **Runs suite-level teardown**
8. **Waits for video AI batches** to complete (if any tests had video assertions)
9. **Writes `summary.json`** with per-test verdicts and Gemini cost breakdown

### Execution Output

Results are saved to:
```
test-results/{game}/{env}/{YYYY-MM-DD}-{suite}/
  summary.json          # Per-test verdicts, counts, cost breakdown
  report.html           # Color-coded HTML report with screenshots
  {test-id}/
    result.json         # Individual test result (bridge + AI verdicts)
    timeline.json       # Step timestamps and video offset
    events.jsonl        # Bridge event log (one JSON per line)
    session.webm        # Full test video recording
    replay.html         # Synchronized video + event replay viewer
    step-01.png         # Screenshots (from capture/assert steps)
    step-01-ai.json     # Gemini AI response (if AI assertion ran)
    trimmed.webm        # Trimmed video segment (if video AI used)
```

### Verdict Model

Each test receives a **final verdict** based on two validation layers:

| Bridge Result | AI Result | Final Verdict | Meaning |
|---------------|-----------|---------------|---------|
| pass | pass | **green** | Both checks agree — test passes |
| pass | fail | **orange** | Disagreement — human review needed |
| fail | pass | **orange** | Disagreement — human review needed |
| fail | fail | **red** | Both checks agree — test fails |
| pass | skip | **green** | No AI assertion defined — bridge-only pass |
| any | pending | **pending** | Video AI not yet processed |

### Stop Conditions

Configured per-suite in the YAML `config.stop_conditions` block:

| Condition | Type | Behavior |
|-----------|------|----------|
| `fail_fast` | bool | Stop the suite on the first red verdict |
| `max_consecutive_failures` | int | Stop after N consecutive red/orange verdicts |
| `max_total_failures` | int | Stop after N total red/orange verdicts |

When a stop condition triggers, remaining tests are skipped and the reason is recorded in `summary.json`.

### Timeout Enforcement

- **Per-test:** `config.test_timeout_ms` (default: 60,000ms). If a test exceeds this, the browser context is killed, partial artifacts are saved, and the test is marked `TIMEOUT`.
- **Per-suite:** `config.suite_timeout_ms` (default: 600,000ms). Acts as an overall wall-clock limit.

---

## Step Executor

Maps YAML step action types to Python helper calls. Used internally by the suite runner — not invoked directly.

### Supported Action Types

| Action | Parameters | Description |
|--------|-----------|-------------|
| `navigate` | `url` (string) | Navigate browser to URL |
| `wait_for_state` | `state` (string), `timeout_ms` (int, default 30000) | Poll bridge until app state matches |
| `bridge_action` | `name` (string), plus action-specific params | Call a named bridge action method |
| `bridge_assert` | `method` (string), `expected` (any) | Read a bridge query value and compare |
| `capture` | — | Take a screenshot, save to artifacts |
| `assert` | `bridge` and/or `ai` block | Run bridge and/or AI assertions (see below) |
| `script` | `cmd` (string) | Run a shell command via subprocess |
| `go_offline` | — | Set browser to offline mode |
| `go_online` | — | Restore browser connectivity |
| `press_key` | `key` (string) | Press a keyboard key (e.g., `Space`, `Enter`) |
| `click` | `selector` (string) or `x`/`y` (numbers) | Click an element or canvas coordinate |
| `wait_ms` | `ms` (int) | Fixed delay in milliseconds |

### Assert Block Structure

The `assert` action supports two validation layers that can be used independently or together:

```yaml
- id: step-03
  description: "Verify free spins granted"
  assert:
    bridge:
      method: getFreeSpinsRemaining     # Bridge query method name
      expected: 3                        # Expected return value
    ai:
      mode: image                        # "image" or "video"
      ref_screenshot: step-02            # Step ID of the reference screenshot
      ref_assets: [coin, scatter]        # Asset manifest keys for Gemini context
      prompt: "How many free spins are shown on screen?"
      output_schema:
        free_spins: { type: number }
        confidence: { type: string, enum: [high, medium, low] }
      expected:
        free_spins: 3
        confidence: high
```

**AI Image Mode** — Sends a screenshot + reference assets + prompt to Gemini. Best for end-state checks.

**AI Video Mode** — Trims the test recording between two steps and sends the clip to Gemini. Best for animation/transition verification. Processed in batches after test execution.

```yaml
ai:
  mode: video
  video_trim:
    from_step: step-01                   # Trim start
    to_step: step-03                     # Trim end
  ref_assets: [scatter, wild]
  prompt: "Did the reels spin and stop? What symbols landed?"
  output_schema:
    reels_spun: { type: boolean }
    result_grid: { type: array }
  expected:
    reels_spun: true
```

### After Each Step

The executor automatically:
1. Records wall-clock timestamps in the timeline (start/end milliseconds relative to test start)
2. Drains the bridge event buffer and appends events to `events.jsonl`

---

## Video AI Processor

Processes video-mode AI assertions using a streaming batch model. Tests are grouped by their shared `ref_assets` set — tests sharing the same assets reuse a single Gemini context cache, reducing API cost.

### Integrated Mode (automatic)

During suite execution, the processor runs automatically. When all tests in a ref_assets group complete, the batch is dispatched immediately — no waiting for the entire suite to finish.

### Standalone Mode (reprocessing)

Re-process video AI assertions on existing test results without re-running the suite:

```bash
python framework/runner/video_ai_processor.py \
  --results-dir test-results/zeus/dev/2026-04-10-suite-06-freespin \
  --game zeus \
  [--base-dir /path/to/qa-platform]
```

This is useful when:
- Gemini was unavailable during the original run (results show `ai_result: fail, reason: "Gemini API unavailable"`)
- You want to retry AI assertions with updated prompts
- Video processing was interrupted

### How Batching Works

1. At suite start: parse all test YAML, find tests with `mode: video`
2. Group by sorted `ref_assets` set (e.g., tests using `[scatter, wild]` share a group)
3. When all tests in a group complete:
   - Upload shared reference asset images **once** via Gemini context caching
   - For each test: trim video via ffmpeg → send to Gemini → update `result.json`
   - Release the cached context
4. Tests in different groups are processed independently

---

## Reporter

Aggregates test results and generates HTML reports with the replay viewer.

### Usage

```bash
python framework/runner/reporter.py \
  --results-dir test-results/zeus/dev/2026-04-10-suite-01-core \
  [--template-dir framework/templates/]
```

### What It Produces

**`summary.json`** — Machine-readable results:
```json
{
  "suite": "Core Game Functionality",
  "game": "zeus",
  "env": "dev",
  "run_date": "2026-04-10",
  "duration_ms": 45200,
  "total": 9,
  "counts": { "green": 7, "orange": 1, "red": 1, "pending": 0, "skip": 0 },
  "tests": [ ... ],
  "costs": {
    "gemini_image_calls": 12,
    "gemini_video_calls": 0,
    "gemini_input_tokens": 45000,
    "gemini_output_tokens": 3200,
    "estimated_cost_usd": 0.08
  }
}
```

**`report.html`** — Color-coded HTML report with:
- Per-test verdict badges (green/orange/red/pending)
- Inline screenshots for orange and red tests
- Links to session video and replay viewer per test
- Overall pass/fail counts and cost summary

**`replay.html`** (per test) — Synchronized video + event viewer:
- Left panel (60%): HTML5 video player with speed controls (0.5x/1x/2x)
- Right panel (40%): Scrollable event log with type filters and color-coded badges
- Bottom: Clickable step timeline segments
- Fully self-contained — open in any browser, no server needed

---

## Environment Configuration

Each game has an `env.config.yml` in `games/{game}/`:

```yaml
environments:
  dev:
    url: "https://dev.example.com/games/zeus"    # Static URL (option A)
    browser_params: "?bridge=1&debug=1"           # Appended as query string
  staging:
    url_script: "python scripts/get_url.py --env=staging"  # Dynamic URL (option B)
    browser_params: "?bridge=1"
  prod:
    url_script: "python scripts/get_url.py --env=prod"
    browser_params: ""

detection:
  method: both              # phash | gemini | both
  phash_threshold: 10       # Hamming distance threshold
  fallback: gemini          # Fallback when phash confidence is low
```

**URL resolution:** Each environment provides either a static `url` or a `url_script` command whose stdout is the URL. The `browser_params` string is appended as a query string.

**Bridge activation:** Dev and staging environments should include `?bridge=1` in `browser_params` to activate the game's test bridge.

---

## YAML Test Suite Format

```yaml
suite: "Suite Display Name"
game: zeus
version: "1.0"

config:
  detection_method: both
  video: true
  test_timeout_ms: 60000
  suite_timeout_ms: 600000
  stop_conditions:
    fail_fast: false
    max_consecutive_failures: 3
    max_total_failures: 10

# Runs once before all tests
setup:
  - action: navigate
    params: { url_script: "get_game_url" }
  - action: wait_for_state
    params: { state: "idle", timeout_ms: 15000 }

# Runs once after all tests
teardown: []

tests:
  - id: ZZ-01
    title: "Game loads and reaches idle state"
    description: "Verify the game initializes correctly"

    setup:
      - action: bridge_action
        params: { name: "setDevModeResult", grid: [[1,3,4],[3,4,1],[4,1,3]] }

    steps:
      - id: step-01
        action: bridge_action
        params: { name: "spin" }

      - id: step-02
        action: wait_for_state
        params: { state: "idle", timeout_ms: 10000 }
        capture: true

      - id: step-03
        description: "Verify balance decreased"
        assert:
          bridge:
            method: getBalance
            expected: 999

    teardown:
      - action: bridge_action
        params: { name: "resetDevMode" }
```

### Schema Validation

Suite files are validated automatically before execution. You can also validate manually:

```python
import yaml
from framework.schema_validator import validate_suite

with open("games/zeus/test-suites/suite-01-core.yml") as f:
    doc = yaml.safe_load(f)

errors = validate_suite(doc, source="suite-01-core.yml")
if errors:
    for e in errors:
        print(e)  # "[suite-01-core.yml] tests[0].steps[1].action: unsupported action 'fly'"
else:
    print("Valid!")
```

---

## Troubleshooting

### Bridge not found
```
BridgeNotFoundError: Game test bridge not registered at window.__gameTestBridge
```
Ensure the game URL includes `?bridge=1` in the environment's `browser_params`.

### ffmpeg not found
```
FileNotFoundError: ffmpeg not found on PATH
```
Install ffmpeg and verify with `ffmpeg -version`. Video AI assertions require ffmpeg for trimming.

### Gemini API errors
```
ai_result: fail, reason: "Gemini API unavailable"
```
Check that `GEMINI_API_KEY` is set. Re-process failed video assertions with:
```bash
python framework/runner/video_ai_processor.py --results-dir <path> --game <game>
```

### Test timeout
```
final_verdict: TIMEOUT
```
Increase `test_timeout_ms` in the suite config, or check if the game is stuck in a non-idle state. Partial artifacts (video, events up to timeout) are preserved.

### Schema validation errors
```
[suite-01.yml] $.config.test_timeout_ms: required field missing
```
Check the suite YAML against the format documented above. All required fields must be present.
