# Skill: /run-test

```yaml
name: run-test
version: "2.0"
domain: [game-qa, test-execution, result-reporting, visual-ai-testing]
depends_on: [write-test, init-knowledge]
description: |
  Executes a YAML test suite against a game environment via the
  suite_runner.py subprocess, then parses summary.json to present
  color-coded verdicts (green/orange/red/pending), dual-validator
  sub-verdict decomposition, Gemini API cost accounting, and
  per-failure artifact paths. When failures occur, it runs an
  evidence-based investigation -- reading AI-response JSON, events/
  timeline, screenshots, the suite YAML, and game-knowledge.md -- to
  give grounded suggestions (wrong expectation, fragile step, infra,
  or possible game bug).

  Use when the user says "run tests," "execute the suite," "run
  the test suite," "check if tests pass," "test [game] on [env],"
  or "run suite-01" -- even if they don't say "run-test."
  Also triggers for "re-run the tests," "what failed?", "why did it
  fail?", "investigate the failure," "what's wrong with the test,"
  and "run it again on staging."

  Do NOT use for writing or editing tests (use /write-test).
  Do NOT use for onboarding a game (use /init-knowledge).
  Do NOT use for updating knowledge (use /update-knowledge).
```

**Usage:** `/run-test --game=<game-name> --suite=<suite-name> --env=<environment> [--test=<id>] [--group=<id>]`

**Flags:**
- `--game` (required) — game identifier (e.g., `zeus`)
- `--suite` (required) — suite filename stem (e.g., `suite-01-core`)
- `--env` (required) — environment name (e.g., `dev`, `staging`, `prod`)
- `--test` (optional) — run only the given test id(s), comma-separated (e.g. `ZZ-15` or `ZZ-15,ZZ-16`). The enclosing **group's setup/teardown still runs** around the selected test(s), so a test that depends on its group's paytable is set up correctly. IF the id is a **case** inside a `runs:` block (one execution → many cases), the whole run executes **once** and only that case's verdict is reported. Default: run all tests.
- `--group` (optional) — run only the given group id(s), comma-separated (e.g. `freespin`). Default: run all groups.

**Standalone CLI (no skill, identical execution):**
```bash
python framework/runner/suite_runner.py --game zeus --suite suite-01-core --env dev
# run a single test case (its group setup/teardown still runs):
python framework/runner/suite_runner.py --game zeus --suite suite-06 --env dev --test ZZ-15
# run a whole group:
python framework/runner/suite_runner.py --game zeus --suite suite-06 --env dev --group freespin
```
The skill adds result presentation only — execution is identical.

---

## Selecting What to Run (granularity)

A suite is structured as **suite → group(s) → run/test → case**, and you can target any of
these levels with the two filters (`--test` / `--group`). The smallest selectable unit is a
**case/test** — there is no per-step selection.

| Run by | Flags | Behavior |
|---|---|---|
| **Whole suite** | none | Every group, test, and run executes (default) |
| **Group(s)** | `--group <id>[,<id>…]` | Only those groups — their setup/teardown **plus all** their tests/runs |
| **Standalone test(s)** | `--test <id>[,<id>…]` | Only those tests; the enclosing **group's setup/teardown still runs** |
| **A run** | `--test <run-id>` | The whole run executes once; **all** its case verdicts are reported |
| **A single case** | `--test <case-id>` | The parent run still executes **once**, but only that case's verdict is reported (cases share one execution — a case can't run in isolation) |

**Semantics to know:**
- **`--test` accepts test ids, run ids, AND case ids** — a case id means "run the parent run,
  report just this case." (`framework/runner/suite_runner.py` → `_test_selected` / `_run_selected`.)
- **Both filters are comma-separated** (multi-select) and **compose with AND** — e.g.
  `--group freespin --test ZZ-30` runs only the `freespin` group and, within it, only `ZZ-30`.
- **A test/case selection never skips its group's setup/teardown**, so paytable / balance / state
  preconditions established at group level are still applied (run a single bonus case safely).
- **No `--case`, `--run`, or per-step flag** — cases and runs are both selected through `--test`
  (by their id); steps are never run individually.

To discover the selectable ids for a suite, read its YAML (the `id:` of each `group`, `test`,
`run`, and `case`) or the `{suite}.plan.json` plan file — there is no `--list` command.

---

## Expert Vocabulary Payload

**Test Execution & Orchestration:**
suite runner (suite_runner.py), subprocess invocation, exit code handling, pre-flight validation, per-environment configuration (env.config.{suffix}.yml), test timeout (test_timeout_ms), suite timeout (suite_timeout_ms), stop conditions (fail_fast, max_consecutive_failures, max_total_failures)

**Result Processing & Verdicts:**
summary.json, final_verdict, verdict classification (green/orange/red/pending/skip), dual-validator disagreement, sub-verdict decomposition (bridge_result + ai_result), cost accounting (gemini_image_calls, gemini_video_calls, estimated_cost_usd), stop_reason

**Artifact Management:**
results directory (`test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/`), per-test artifact directory, step screenshots (step-N.png), AI response JSON (step-N-ai.json), replay file (replay.html), relative path convention (always relative to qa-platform root)

**Failure Diagnosis:**
evidence-based investigation, artifact correlation (result.json + step-N-ai.json + events.jsonl + screenshots), expectation-vs-documentation cross-check (game-knowledge.md), fragile-step detection, confidence-ranked hypotheses, possible game-bug flagging, failure pattern matching, bridge/AI verdict cross-referencing, timeout detection (timed_out flag), pending AI processing, timing issue diagnosis

---

## Anti-Pattern Watchlist

### 1. Test Mutation
**Detection:** Any write to a file under `games/{game}/test-suites/` during this skill's execution.
**Why it's dangerous:** run-test is a read-only reporting skill. Modifying tests during execution creates confusion about what was actually tested.
**Resolution:** Never modify test YAML files. IF a test needs changes: report the issue and direct QA to use `/write-test`.

### 2. Retry on Success
**Detection:** Re-invoking suite_runner.py after it already exited with code 0 in the current session.
**Why it's dangerous:** Wastes Gemini API budget (real cost). Overwrites the previous results directory.
**Resolution:** IF runner succeeded: present results. Do not re-run unless QA explicitly requests it.

### 3. Truncated Error Output
**Detection:** Subprocess fails but only a summary or partial error message is shown. Full stderr is hidden or cut off.
**Why it's dangerous:** QA needs the full stack trace to diagnose runner failures. A truncated message sends them on a blind debugging path.
**Resolution:** Always show complete stdout and stderr on subprocess failure. Never truncate, summarize, or paraphrase error output.

### 4. Absolute Path Leak
**Detection:** Output contains absolute filesystem paths (e.g., `D:\Project\qa-platform\test-results\...`) instead of project-relative paths.
**Why it's dangerous:** Artifacts are shared across team members. Absolute paths break when the repo root differs.
**Resolution:** All artifact paths in output must be relative to the qa-platform root directory.

### 5. Stale Result Cache
**Detection:** Presenting results from a previous run's summary.json instead of the current run's output.
**Why it's dangerous:** QA sees outdated verdicts and makes decisions based on stale data.
**Resolution:** Always read summary.json from the directory matching today's date and the current suite name. IF the user re-runs: read the new summary, not the cached one.

### 6. Ungrounded Diagnosis
**Detection:** Stating a cause without having read the artifact that supports it, or asserting a definite **game bug** from indirect signals.
**Why it's dangerous:** A confident wrong cause sends QA down the wrong path.
**Resolution:** Diagnose only AFTER reading the evidence (Step 6). Cite the exact artifact each claim rests on and rank by confidence (high/medium/low). Test-side causes (wrong expectation, fragile step, mis-scoped AI prompt) may be stated when backed by cited evidence and paired with a concrete fix. A possible game bug must be presented as a flagged hypothesis for QA — never as fact.

---

## Behavioral Instructions

### Step 1 — Validate Inputs (Pre-flight)

1. Read the per-environment game config `games/{game}/env.config.{suffix}.yml`
   (the suffix maps the env name: `dev`→`dev`, `staging`→`stg`, `prod`→`prod`,
   `local`→`local`; so `--env staging` → `env.config.stg.yml`). Each file is
   self-contained — it holds only that one environment's config (no
   `environments:` wrapper).
   - IF the file for `{env}` does not exist: output `"games/{game}/env.config.{suffix}.yml not found. Run /init-knowledge --game={game} first, or check the --env value."` and STOP.
3. Check that `games/{game}/test-suites/{suite}.yml` exists.
   - IF missing: output `"Suite not found: games/{game}/test-suites/{suite}.yml. Run /write-test --game={game} --suite={suite} first."` and STOP.

### Step 2 — Invoke Suite Runner

4. Run the subprocess (append `--test`/`--group` only if the user provided them):
   ```bash
   python framework/runner/suite_runner.py --game {game} --suite {suite} --env {env} [--test {ids}] [--group {ids}]
   ```
   When the user asks to run a single test case (e.g. "run ZZ-15 from suite-06"), pass
   `--test ZZ-15`. The runner automatically also runs that test's group setup/teardown,
   so the test's paytable/precondition is established — do NOT try to run the group
   setup separately. IF `ZZ-15` is a **case** within a `runs:` entry (one execution, many
   cases), the runner runs the entire run once and reports only that case's verdict.
5. Display while running:
   ```
   Running {suite} for {game} on {env}{ , test {ids} / group {ids} if filtered }...
   (this may take several minutes -- video AI processing runs after all tests complete)
   ```
6. IF subprocess exits with non-zero code, classify the error:
   - IF stderr contains `Game config not found` or `env.config.` ... `not found`: report config error (missing per-environment game config file).
   - IF stderr contains `Suite file not found`: report missing suite file.
   - IF stderr contains `Suite validation failed`: show validation errors verbatim.
   - OTHERWISE: show full stdout and stderr:
     ```
     Suite runner failed (exit {code}):

     --- stdout ---
     {stdout}

     --- stderr ---
     {stderr}
     ```
7. Do NOT attempt to parse results if the subprocess failed. STOP.

### Step 3 — Read Summary

8. After exit code 0, locate `summary.json`. The runner prints its path on the final log
   lines as `Summary written: {path}` — use that. It is written **last** (after all video/
   image AI), so its presence means the run actually finished. The run directory is
   `test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/`, stamped with the run's **start
   time** — do NOT reconstruct it from the date alone (you don't know the minute). Read the
   path from the `Summary written:` line, or pick the newest `{suite}-*` directory under
   `test-results/{game}/{env}/`. Refer to this directory's name as `{run_dir}` below.
9. IF summary.json not found (no `Summary written:` line, and no matching directory):
   ```
   Suite runner reported success but summary.json was not found under:
     test-results/{game}/{env}/{suite}-*

   Check the runner logs above (look for the "Summary written:" line).
   ```
   STOP.

### Step 4 — Present Results

10. Output the results dashboard:

```
{suite_title} -- {game} -- {env} -- {run_date}
--------------------------------------------------
{per-test rows}
--------------------------------------------------
{N} green  {N} orange  {N} red  {N} pending
Cost: {gemini_image_calls} image calls, {gemini_video_calls} video calls, ~${estimated_cost_usd:.4f}
Artifacts: test-results/{game}/{env}/{run_dir}/
```

11. Per-test row format by verdict:

| Verdict | Row format |
|---------|------------|
| green | `[green]  {test_id}  {title}` |
| orange or red (no error) | `[{verdict}]  {test_id}  {title}  -- bridge={bridge_result} ai={ai_result}` |
| orange or red (with error) | `[{verdict}]  {test_id}  {title}  -- bridge={bridge_result} ai={ai_result}  ERROR: {error}` |
| pending | `[pending]  {test_id}  {title}` |
| skip | `[skip]  {test_id}  {title}` |

12. Verdict definitions:

| Verdict | Meaning |
|---------|---------|
| green | bridge=pass AND ai=pass -- fully passed |
| orange | one validator passed, one failed -- human review needed |
| red | both validators failed (or bridge failed with timeout/error) |
| pending | AI video processing not yet complete |
| skip | no assertions in this test |

13. IF `summary.costs.gemini_image_calls` and `gemini_video_calls` are both 0: omit the Cost line.
14. IF `summary.stop_reason` is non-null: append `Stopped early: {stop_reason}` after the count line.

### Step 5 — Failure Details (orange and red only)

15. IF any orange or red tests exist, output:

```
=== Failure Details ===

[{verdict}] {test_id} -- {title}
  bridge: {bridge_result}
  ai:     {ai_result}
  error:  {error or "none"}
  duration: {duration_ms}ms{" (TIMED OUT)" if timed_out}

  Screenshots: test-results/{game}/{env}/{run_dir}/{test_id}/
  Replay:      test-results/{game}/{env}/{run_dir}/{test_id}/replay.html

  Suggested next steps:
  {pattern-matched suggestion from table below}
```

16. Match failure pattern to diagnostic suggestion:

| Pattern | Suggested next step |
|---------|---------------------|
| bridge=fail, ai=fail or ai=skip | Check bridge assertion: `{error}`. Review screenshot at the step where bridge failed. |
| bridge=pass, ai=fail | AI disagreed with bridge. Review `step-N-ai.json` in artifact directory for AI response detail. Check screenshots. |
| bridge=fail, ai=pass | Bridge failed but AI sees a passing state -- possible bridge API regression or timing issue. |
| timed_out=true | Test exceeded timeout. Check if game loaded (screenshot step-01.png). Consider increasing `test_timeout_ms` in suite YAML. |
| ai_result=pending | Video AI processing still running or failed silently. Re-run suite or check `video_ai_processor` logs. |

### Step 6 — Investigate and Diagnose Failures (evidence-based)

Run this whenever the finished run has **red or orange** results (skip if all green / pending-only). The goal: read the actual artifacts and context, then tell the user *what went wrong* and *what to do* — the expectation may be wrong, a step may be fragile, the AI assert may be mis-scoped, it may be infra, or it may be a real game bug. Stay **read-only** (anti-pattern #1): suggest changes via `/write-test`, never edit the suite here.

17. **Bare review, then ask permission to investigate.** Steps 4–5 already gave the bare review (dashboard + per-failure artifact pointers) — that stands on its own. Do NOT start reading artifacts yet. First ask the user whether to investigate:

    ```
    {N} red / {N} orange need attention: {failing test ids}.
    Want me to investigate? I'll read the AI responses, events, screenshots, the test YAML,
    and game-knowledge.md to suggest what went wrong -- test expectation, fragile step, infra,
    or possible game bug. [yes / no]
    ```

    **STOP and wait for the user.** Run items 18–20 ONLY if they agree. EXCEPTION: if the user already asked to investigate (e.g. invoked with "why did it fail?" / "investigate the failures"), skip the question and go straight to item 18. If they decline, skip to Step 7.

18. **Gather evidence** for each red/orange test *before forming any opinion* — this is what makes the diagnosis grounded rather than a guess (anti-pattern #6). Read what's relevant:
    - `{run_dir}/{test_id}/result.json` — bridge/ai/final verdict, `error`, `duration_ms`, `timed_out`.
    - `{run_dir}/{test_id}/step-N-ai.json` — the AI's structured output (expected vs actual) and its reasoning, for AI disagreements.
    - `{run_dir}/{test_id}/events.jsonl` + `timeline.json` — the bridge event stream and per-step timings (reveals races and what the bridge actually saw).
    - `{run_dir}/{test_id}/step-N.jpg` — view the screenshot at the failing step (you are multimodal — actually look at it).
    - The runner stdout/stderr captured in Step 2 — the live error/traceback.
    - The failing test in `games/{game}/test-suites/{suite}.yml` — its steps, asserts, and expected values.
    - `games/{game}/docs/final/game-knowledge.md` (+ `docs/final/paytable.md`, and the relevant `shared-mechanisms/*.md`) — to check whether the test's expected value matches the **documented** game behaviour.

19. **Correlate the evidence into a most-likely cause.** Cross-reference bridge vs AI vs screenshot vs events vs documented behaviour, then map to a category and form a hypothesis with a confidence level:

    | Signal | Likely cause | Suggested fix |
    |--------|--------------|---------------|
    | YAML expected value ≠ behaviour documented in game-knowledge.md (bridge=fail) | Wrong test expectation | Correct the assert's expected value to the documented/observed one. |
    | bridge=pass, ai=fail; AI JSON judged the wrong phase or read a fast-changing value | Fragile AI assert (scope / transient read) | Scope the AI prompt to the right phase; move exact/transient values to a bridge assert. |
    | bridge=fail, ai=pass; screenshot shows the correct end state | Early/fragile bridge read or timing race | Wait for the proper state (`wait_for_state` / `waitForIdle`) before asserting; avoid arbitrary sleeps. |
    | timed_out=true; step-01 screenshot shows game not loaded/stuck | Load or timing failure | Confirm game load + bridge registration; raise `test_timeout_ms` only if the step is legitimately slow. |
    | error has `<bridge not registered>` / `Unauthorized` / token | Infra, not test logic | Refresh the env/BO token; confirm `?bridge=1` reaches READY. |
    | bridge=fail AND screenshot/events confirm wrong behaviour | Possible real game bug | Flag for QA with the evidence — present it, do not assert it. |
    | ai_result=pending | Video AI didn't finish | Re-run, or check `video_ai_processor` logs; not a test failure per se. |

    Common **fragile-step signatures** (usually "the step needs to change," not "the game is broken"): arbitrary sleeps instead of waiting for a state; asserting a value mid-animation that the bridge resolves; AI prompts not scoped to the bonus/phase; `script`/`cmd` shortcuts; popups not cleared in teardown.

20. **Present the diagnosis** for each red/orange test, citing the exact artifact each claim rests on:

    ```
    === Investigation: {test_id} ({verdict}) ===
    Evidence:
      - result.json: bridge={bridge}, ai={ai}, error={error}
      - step-N-ai.json: expected {X}, got {Y} -- "{AI reasoning excerpt}"   (if AI involved)
      - screenshot step-N.jpg: {what you actually see at the failing step}
      - game-knowledge.md: {documented behaviour for this mechanic, if relevant}

    Most likely cause: {category} ({confidence: high|medium|low})
      because: {the specific evidence above that supports it}

    Suggested fix: {concrete action}.
      - IF the expectation/step/prompt needs changing: say exactly what to change, then
        /write-test --game={game} --suite={suite} --raw="{the change}".
      - IF infra: state the infra fix (token / bridge / env).
      - IF a possible game bug: flag for QA with the evidence -- do not assert it.

    What would confirm it: {a check or re-run that would prove/disprove the hypothesis}
    ```
    Order failures most-actionable first. IF the evidence is genuinely insufficient, say so and name the artifact that would help — never invent a cause.

### Step 7 — Closing

21. Close out based on the run (red/orange next steps are owned by Step 6 — don't repeat generic advice here):

- **All green** (Step 6 was skipped):
  ```
  All tests passed. Run /write-test --game={game} --suite=<next-suite> to continue coverage.
  ```

- **Any pending:**
  ```
  {N} test(s) have pending AI results (video processing incomplete).
  Re-run /run-test --game={game} --suite={suite} --env={env} after it completes,
  or check runner logs for video_ai_processor status.
  ```

- **Red / orange:** the per-failure suggestions from Step 6 are the next steps. IF the user
  declined the investigation, point them to the Step 5 artifact paths and offer to investigate
  later — do not re-emit generic advice.

---

## Examples

### Example 1: BAD vs GOOD — Failure Reporting

**BAD:**
```
[red] ZZ-05 - Hold and spin re-spin counter

The bridge API is broken. The re-spin count should be 3 but the bridge returned 0.
This is a game bug.
```
Ungrounded diagnosis (anti-pattern #6). Asserts root cause without reading any artifact. "The bridge API is broken" could be wrong — it might be a test setup issue, a timing race, or a legitimate game behavior change. Step 6 fixes this by reading the evidence first, then suggesting a cause with cited support.

**GOOD:**
```
[red] ZZ-05 -- Hold and spin re-spin counter -- bridge=fail ai=fail
  bridge: fail (getHoldAndSpinState returned { respins_remaining: 0 }, expected: 3)
  ai:     fail
  error:  none
  duration: 4200ms

  Screenshots: test-results/zeus/dev/suite-01-10-04-2026-1432/ZZ-05/
  Replay:      test-results/zeus/dev/suite-01-10-04-2026-1432/ZZ-05/replay.html

  Suggested next steps:
  Check bridge assertion: getHoldAndSpinState returned 0 respins. Review screenshot
  at the step where bridge failed. Both validators disagree -- check if the game
  actually entered hold-and-spin state (step-01.png).
```
Points to artifacts. Suggests where to look. Does not assert the cause.

### Example 2: BAD vs GOOD — Subprocess Failure

**BAD:**
```
Suite runner failed. There was an error with the configuration.
Please check your setup and try again.
```
Truncated output (anti-pattern #3). No stdout/stderr. QA has no information to diagnose.

**GOOD:**
```
Suite runner failed (exit 1):

--- stdout ---
Loading suite: games/zeus/test-suites/suite-01-core.yml
Validating bridge connection...

--- stderr ---
Traceback (most recent call last):
  File "framework/runner/suite_runner.py", line 142, in connect_bridge
    self.ws = websocket.connect(bridge_url, timeout=5)
ConnectionRefusedError: [Errno 111] Connection refused

Bridge connection failed. Is the game running on dev?
```
Full output preserved. QA can see exactly what happened and where.

### Example 3: Orange Verdict — Dual-Validator Disagreement

**Scenario:** bridge says pass (balance decreased correctly), AI says fail (bonus popup not visible).

**BAD:**
```
[orange] ZZ-08 - Buy bonus purchase
AI failed but bridge passed. The test probably needs fixing.
```
Vague. No artifact paths. "Probably" is hedging. QA doesn't know where to look.

**GOOD:**
```
[orange] ZZ-08 -- Buy bonus purchase -- bridge=pass ai=fail
  bridge: pass
  ai:     fail
  error:  none
  duration: 6100ms

  Screenshots: test-results/zeus/dev/suite-02-10-04-2026-1505/ZZ-08/
  Replay:      test-results/zeus/dev/suite-02-10-04-2026-1505/ZZ-08/replay.html

  Suggested next steps:
  AI disagreed with bridge. Review step-02-ai.json in artifact directory
  for AI response detail. Check screenshots -- the bonus popup may have
  appeared after the screenshot was captured (timing issue), or the
  ref_asset for the popup may need updating.
```
Points to the exact JSON file with the AI's reasoning. Suggests two likely causes without asserting either.

---

## Questions This Skill Answers

- "Run tests for [game]"
- "Execute the test suite"
- "Run suite-01 on dev"
- "Check if the tests pass"
- "Test [game] on staging"
- "Re-run the tests"
- "What failed in the last run?"
- "Why did [test] fail?"
- "Investigate the failures and tell me what's wrong"
- "Is this a test problem or a game bug?"
- "Run it again on [env]"
- "How many tests passed?"
- "Show me the test results"
- "Run the free spin suite"
- "Execute tests and show failures"
- "What's the Gemini cost for this run?"
