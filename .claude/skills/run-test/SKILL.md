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
  sub-verdict decomposition, Gemini API cost accounting, per-failure
  artifact paths, and pattern-matched diagnostic next steps.

  Use when the user says "run tests," "execute the suite," "run
  the test suite," "check if tests pass," "test [game] on [env],"
  or "run suite-01" -- even if they don't say "run-test."
  Also triggers for "re-run the tests," "what failed?", and
  "run it again on staging."

  Do NOT use for writing or editing tests (use /write-test).
  Do NOT use for onboarding a game (use /init-knowledge).
  Do NOT use for updating knowledge (use /update-knowledge).
```

**Usage:** `/run-test --game=<game-name> --suite=<suite-name> --env=<environment>`

**Flags:**
- `--game` (required) — game identifier (e.g., `zeus`)
- `--suite` (required) — suite filename stem (e.g., `suite-01-core`)
- `--env` (required) — environment name (e.g., `dev`, `staging`, `prod`)

**Standalone CLI (no skill, identical execution):**
```bash
python framework/runner/suite_runner.py --game zeus --suite suite-01-core --env dev
```
The skill adds result presentation only — execution is identical.

---

## Expert Vocabulary Payload

**Test Execution & Orchestration:**
suite runner (suite_runner.py), subprocess invocation, exit code handling, pre-flight validation, environment configuration (env.config.yml), test timeout (test_timeout_ms), suite timeout (suite_timeout_ms), stop conditions (fail_fast, max_consecutive_failures, max_total_failures)

**Result Processing & Verdicts:**
summary.json, final_verdict, verdict classification (green/orange/red/pending/skip), dual-validator disagreement, sub-verdict decomposition (bridge_result + ai_result), cost accounting (gemini_image_calls, gemini_video_calls, estimated_cost_usd), stop_reason

**Artifact Management:**
results directory (`test-results/{game}/{env}/{date}-{suite}/`), per-test artifact directory, step screenshots (step-N.png), AI response JSON (step-N-ai.json), replay file (replay.html), relative path convention (always relative to qa-platform root)

**Failure Diagnosis:**
failure pattern matching, bridge/AI verdict cross-referencing, timeout detection (timed_out flag), pending AI processing, bridge API regression, timing issue diagnosis

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

### 6. Premature Diagnosis
**Detection:** Stating the root cause of a failure ("the bridge API is broken") instead of pointing to specific artifacts for QA to investigate.
**Why it's dangerous:** The skill doesn't have enough context to diagnose game logic bugs. Confident wrong diagnoses send QA down the wrong path.
**Resolution:** Use the failure pattern table to suggest investigation steps and artifact locations. Never assert a root cause — suggest where to look.

---

## Behavioral Instructions

### Step 1 — Validate Inputs (Pre-flight)

1. Read `games/{game}/env.config.yml`.
   - IF file does not exist: output `"games/{game}/env.config.yml not found. Run /init-knowledge --game={game} first."` and STOP.
2. Check that `{env}` appears under the `environments:` key.
   - IF environment not found: output `"Environment '{env}' not defined in env.config.yml. Available: {list}."` and STOP.
3. Check that `games/{game}/test-suites/{suite}.yml` exists.
   - IF missing: output `"Suite not found: games/{game}/test-suites/{suite}.yml. Run /write-test --game={game} --suite={suite} first."` and STOP.

### Step 2 — Invoke Suite Runner

4. Run the subprocess:
   ```bash
   python framework/runner/suite_runner.py --game {game} --suite {suite} --env {env}
   ```
5. Display while running:
   ```
   Running {suite} for {game} on {env}...
   (this may take several minutes -- video AI processing runs after all tests complete)
   ```
6. IF subprocess exits with non-zero code, classify the error:
   - IF stderr contains `env.config.yml not found` or `Environment '...' not in env.config.yml`: report config error.
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

8. After exit code 0, read `test-results/{game}/{env}/{YYYY-MM-DD}-{suite}/summary.json` (where `YYYY-MM-DD` is today's date).
9. IF summary.json not found:
   ```
   Suite runner reported success but summary.json was not found at:
     test-results/{game}/{env}/{today}-{suite}/summary.json

   Check the runner logs above for details.
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
Artifacts: test-results/{game}/{env}/{date}-{suite}/
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

  Screenshots: test-results/{game}/{env}/{date}-{suite}/{test_id}/
  Replay:      test-results/{game}/{env}/{date}-{suite}/{test_id}/replay.html

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

### Step 6 — Overall Next Steps

17. Output the appropriate summary based on verdict distribution:

- **All green:**
  ```
  All tests passed. Run /write-test --game={game} --suite=<next-suite> to continue coverage.
  ```

- **Any orange:**
  ```
  {N} test(s) need human review (orange verdict).
  Review artifact screenshots and AI response JSON listed above.
  If game behavior is correct, the AI prompt or expected output may need adjustment.
  Update with /write-test --game={game} --suite={suite} --raw="..."
  ```

- **Any red:**
  ```
  {N} test(s) failed (red verdict).
  Fix failures listed above before expanding test coverage.
  Common causes: bridge method returning wrong value, game state bug, or test setup issue.
  ```

- **Any pending:**
  ```
  {N} test(s) have pending AI results (video processing incomplete).
  Re-run /run-test --game={game} --suite={suite} --env={env} after processing completes,
  or check runner logs for video_ai_processor status.
  ```

---

## Examples

### Example 1: BAD vs GOOD — Failure Reporting

**BAD:**
```
[red] ZZ-05 - Hold and spin re-spin counter

The bridge API is broken. The re-spin count should be 3 but the bridge returned 0.
This is a game bug.
```
Premature diagnosis (anti-pattern #6). Asserts root cause without evidence. "The bridge API is broken" could be wrong — it might be a test setup issue, a timing race, or a legitimate game behavior change.

**GOOD:**
```
[red] ZZ-05 -- Hold and spin re-spin counter -- bridge=fail ai=fail
  bridge: fail (getHoldAndSpinState returned { respins_remaining: 0 }, expected: 3)
  ai:     fail
  error:  none
  duration: 4200ms

  Screenshots: test-results/zeus/dev/2026-04-10-suite-01/ZZ-05/
  Replay:      test-results/zeus/dev/2026-04-10-suite-01/ZZ-05/replay.html

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

  Screenshots: test-results/zeus/dev/2026-04-10-suite-02/ZZ-08/
  Replay:      test-results/zeus/dev/2026-04-10-suite-02/ZZ-08/replay.html

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
- "Run it again on [env]"
- "How many tests passed?"
- "Show me the test results"
- "Run the free spin suite"
- "Execute tests and show failures"
- "What's the Gemini cost for this run?"
