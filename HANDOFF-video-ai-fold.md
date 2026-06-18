# HANDOFF — video AI assert never produces a verdict for a standalone *test* (RESOLVED)

**Date:** 2026-06-18
**Status:** **RESOLVED.** Both the original "silent pending" bug *and* the standalone-test video bug are
fixed. The root cause was **not** a `BaseException`/`CancelledError` — see "RESOLUTION" below. Nothing
cancelled the `to_thread`.

---

## RESOLUTION (the hypothesis below was wrong — record the real cause)

**Root cause:** a plain `AttributeError`, raised **outside** the `try/except Exception` in `_process_test`.

`_process_test` did its cost/model bookkeeping with **direct** attribute access:
`self._gemini.cost_for_tokens(...)` (`video_ai_processor.py:496`) and `self._gemini.model` (`:502`, `:546`).
`ClaudeVideoHelper` had **neither** method (only `self._model` + `cost_summary`), despite its docstring
claiming "Interface-compatible with GeminiVideoHelper." So on the claude provider, the instant
`analyse_video` returned (~58s = the real subprocess runtime, *not* a cancel/timeout), line 496 raised
`AttributeError`. That `try` ends at `:456` — it only wraps reference resolution + the `to_thread` — so the
error propagated out of `_process_test`, `wait_all`'s `gather(return_exceptions=True)` **swallowed it
silently** (no traceback), `record.ai_result` was never set → stayed `pending` → safety net failed it, and
`step-N-ai.json` (written at `:511`, after the crash) never appeared. Every clue fits.

Why runs worked: `_process_run` already read those attrs **defensively** —
`getattr(self._gemini, "cost_for_tokens", None)` / `getattr(..., "model", "unknown")`. The 0.5fps/33-frames
detail was a red herring (fewer frames = faster = 58s).

The handoff's reasoning slipped here: "`except Exception` didn't catch it ⇒ must be a `BaseException`." But
the crash was **outside** the `try`, so `except Exception` never saw it — an ordinary `AttributeError`.

**Fix (two parts):**
1. Root cause — `framework/helpers/claude_ai.py`: added `model` property + `cost_for_tokens()` (returns 0.0;
   local Claude auth is unmetered) to `_ClaudeCliClient`, so both claude helpers are genuinely
   GeminiVideoHelper-compatible (as their docstrings claim).
2. Hardening — `framework/runner/video_ai_processor.py`: `_process_test` now resolves `cost_for_tokens`/`model`
   via `getattr` (mirrors `_process_run`), so post-analysis bookkeeping can never silently leave a test
   `pending` again.

**Tests added (all green):** `tests/test_claude_ai.py` — both helpers expose `model`/`cost_for_tokens`;
`tests/test_video_ai_processor.py::...::test_helper_without_cost_api_folds_to_verdict_not_pending` —
a claude-like helper (no `model`/`cost_for_tokens`) now folds to a real verdict + writes `step-N-ai.json`,
instead of the safety-net "did not complete". `python -m pytest tests/test_video_ai_processor.py -q` → 27 pass.

**Optional live confirmation (not yet run — needs fresh tokens):** `python framework/runner/suite_runner.py
--game zeus --suite raw --env dev --test ZZ-31` should now end `ai=pass`/`ai=fail` with real model output.

**New follow-up surfaced:** `tests/test_claude_ai.py` has **8 pre-existing failures** unrelated to this fix —
stale assertions left by the uncommitted inline-`Popen` refactor of `analyse_video` (they mock
`subprocess.run`, but the CLI now goes through `Popen`), plus `test_bad_fps_falls_back` (expects 2.0;
`_DEFAULT_FPS` is now 1.0). These need updating to the inline path.

---

## TL;DR for the next session

`--test ZZ-31` (a Hold & Spin test with a `mode: video` AI assert) ends as:

```
[ORANGE] ZZ-31 … bridge=pass ai=fail — "video AI did not complete (no verdict produced)"
log: "Test ZZ-31: video AI left pending — marking fail"
```

That `fail` is from the **new safety net** (good — no more silent `pending`). But the **claude video
call itself still isn't completing**: it samples frames, runs ~58s, then `_process_test`'s task ends with
the record still `pending` (so the safety net fails it). The same call inside a **run** (`_process_run`)
completes in ~95–110s and folds green. So something specific to the **standalone-test** path cancels the
`await asyncio.to_thread(self._gemini.analyse_video, …)`.

**Goal:** find what cancels (raises a `BaseException`, almost certainly `asyncio.CancelledError`) on the
test path, and make the video assert actually produce a green/red verdict.

---

## Evidence (from real runs, claude provider, Windows)

| Run | sampling → verdict | frames | outcome |
|---|---|---|---|
| `fs-trigger3-countdown` (a **run**) | 13:20:48 → 13:22:38 (**110s**) | 63 | folded **green** ("Run … video AI done: {…green}") |
| `fs-trigger3-addcoin` (a **run**) | 13:25:37 → 13:27:12 (**95s**) | ~51 | folded **green** |
| **ZZ-31** (a **test**) | 14:10:12 → 14:11:10 (**58s**) | 33 (0.5 fps) | **never folded** → safety net marked `fail` |

Crucial details:
- **No error / traceback** is logged between "Video assert (inline): 33 frame(s)" and the safety net.
- The runner's `_process_test` catches `except Exception` → marks `fail` with a reason. There is **no
  fail reason** here and the record stayed `pending` → therefore `analyse_video` raised a **`BaseException`**
  (which `except Exception` does NOT catch), e.g. `asyncio.CancelledError`. The to-thread coroutine was
  cancelled.
- 58s is NOT the claude subprocess timeout (`CLAUDE_CLI_TIMEOUT` default **600s**, `claude_ai.py:89,288`).
- ZZ-31's clip is 65.5s → adaptive sampler chose **0.5 fps → 33 frames** (runs used 1.0 fps). Possibly a
  red herring, but note it (the 0.5fps branch is the only obvious content difference).

Artifacts of a failing run: `test-results/zeus/dev/raw-18-06-2026-1408/ZZ-31/` — has
`trimmed-step-09.webm`, `trimmed-step-09-claude/frame-*.jpg` (frames WERE sampled), `result.json`
(`ai_result: pending` historically; now `fail` via safety net), and **no `step-9-ai.json`** (the per-step
AI JSON is written only after `analyse_video` returns — confirms it never returned).

---

## Reproduce

Prereqs (both expire ~hourly — refresh before each run):
1. **Game URL token** — paste a fresh launch URL into `games/zeus/env.config.dev.yml` → `url:` (the runner
   appends `&bridge=1&debug=1`). Game must reach READY + `startGame`.
2. **BO token** — `BO_TOKEN` / `BO_REFRESH_TOKEN` in `.env.dev`. ZZ-31 is in group `fs-trigger9`, whose
   group setup calls the back-office (`bo_paytable_ensure/select/update`). If stale you get
   `group 'fs-trigger9' setup failed: Unauthorized — re-login required` and never reach the video.

Command:
```
python framework/runner/suite_runner.py --game zeus --suite raw --env dev --test ZZ-31
```
Long-running (bonus play-out + ~1–2 min video AI). Run it in the background and read the task output file.
Watch for: `Video assert (inline): N frame(s)` → then either a real `ai=pass/fail` (FIXED) or
`Test ZZ-31: video AI left pending — marking fail` (still broken).

To compare with a **working** run path: `--group fs-trigger3-countdown` (folds green) or
`--group fs-trigger3-addcoin`.

---

## Hypotheses (ranked)

1. **The `to_thread(analyse_video)` is cancelled on the test path.** `wait_all` has no timeout and
   `gather(return_exceptions=True)` does not cancel children — so *who* cancels? Find it. Candidates: a
   stray `wait_for`/timeout somewhere in the test dispatch chain; the playwright/browser context closing
   while a thread is mid-flight; an event-loop teardown race specific to a single queued task.
2. **`analyse_video` itself propagates a `BaseException`.** Inspect `ClaudeVideoHelper.analyse_video`
   (`claude_ai.py:524`) and `_run_agent_inline` (`:429`, `proc.communicate(timeout=self._timeout)` at `:470`)
   for any path that could raise `KeyboardInterrupt`/`CancelledError`/`SystemExit` instead of returning a
   `VideoResult(success=False)`. (A normal failure returns success=False → `_process_test` would mark
   `fail`, not leave pending — so this would have to be a true BaseException.)
3. **Structural difference test-path vs run-path.** `_process_test` (`video_ai_processor.py:366`) vs
   `_process_run` (`:661`) call `analyse_video` near-identically (both `async with self._sem` + `to_thread`).
   Diff them carefully — the run path works, the test path doesn't, with the *same* helper and a *smaller*
   clip. The difference is the cancellation trigger, not the call itself.
4. **0.5 fps branch.** Only ZZ-31 hit 0.5 fps. Unlikely to cause a cancel, but rule it out.

---

## Concrete next steps

1. **Capture the exception type.** Temporarily widen the catch in `_process_test`
   (`video_ai_processor.py`, around line 444–456) to log a `BaseException` then re-raise:
   ```python
   try:
       reference = self._video_reference_for(record, ai_block)
       async with self._sem:
           gemini_result = await asyncio.to_thread(self._gemini.analyse_video, …)
   except Exception as exc:
       … existing fail …
       continue
   except BaseException as exc:            # TEMP DEBUG
       logger.error("ZZ-31 video to_thread aborted: %r", exc)   # expect CancelledError
       raise
   ```
   Re-run `--test ZZ-31`. Confirm it's `CancelledError`, and capture the time it fires (~58s?).
2. **Log analyse_video start/return + subprocess lifetime.** Add temp logs in `ClaudeVideoHelper.analyse_video`
   / `_run_agent_inline` (claude_ai.py) around the `Popen`/`communicate` to see whether the **subprocess is
   still running** when the cancel hits (external cancel) vs returned (internal raise).
3. **Find the canceller.** Grep the dispatch path for any `wait_for` / `asyncio.timeout` / task cancellation
   that could reach the video task. Key sites: `suite_runner.py:1194` `await video_processor.wait_all()`
   (no timeout today), `notify_test_complete` (`:903`), the `async with async_playwright()` teardown after
   wait_all + summary (`suite_runner.run()`).
4. **Mitigation regardless of root cause:** give the video call its own explicit budget so it fails cleanly
   instead of being cancelled — e.g. wrap the `to_thread` in `asyncio.wait_for(…, timeout=CLAUDE_CLI_TIMEOUT)`
   inside the existing `try` so a slow/stuck call yields a `fail` reason, and ensure the claude subprocess is
   killed on timeout.
5. Re-run; success = ZZ-31's step-09 ends `ai=pass`/`ai=fail` with real model output (not the safety-net
   "did not complete" message).

---

## Key files & symbols

- `framework/runner/video_ai_processor.py`
  - `wait_all` (line ~321) — dispatches `_process_test`/`_process_run` tasks, `gather(return_exceptions=True)`,
    then the **safety net** loop.
  - `_fail_if_still_pending` (~346) — the safety net (test record → `_update_ai_result(fail)`; run record →
    `_fail_run_cases_ai`).
  - `_process_test` (~366) — per-step loop; the `to_thread(analyse_video)` is at ~444 inside `try/except
    Exception` (~453). **This is where the BaseException escapes.**
  - `_process_run` (~661) — the **working** run equivalent; `to_thread(analyse_video)` at ~723.
  - `_extract_video_test_ids` (~207) — NEW discovery (replaced `_extract_video_tests`/`_group_key`, both
    removed along with `_load_asset_paths` and `_process_group`).
- `framework/helpers/claude_ai.py` — `ClaudeVideoHelper.analyse_video` (~524), `_run_agent_inline` (~429),
  `proc.communicate(timeout=self._timeout)` (~470), `_DEFAULT_TIMEOUT=600` (~89), env `CLAUDE_CLI_TIMEOUT`.
- `framework/runner/suite_runner.py` — `notify_test_complete` (~903), `notify_run_complete` (~964),
  `await video_processor.wait_all()` (~1194), then summary; all inside `async with async_playwright()`.

---

## What was already fixed this session (so don't re-do it)

In `framework/runner/video_ai_processor.py` (and `tests/test_video_ai_processor.py`):
1. **Dropped `ref_assets` batch-grouping entirely.** It only existed to share a now-no-op Gemini context
   cache and keyed off the deprecated `ref_assets` field (after migrating video asserts to `assets:`, every
   standalone video test collapsed to an empty group key `''`). Now: `_extract_video_test_ids` → a set of
   test ids; `notify_test_complete` queues the record; `wait_all` dispatches each test/run **independently**.
   Removed `_group_key`, `_extract_video_tests`, `_load_asset_paths`, `_process_group`, and the
   `ref_assets`/`create_cached_context` logic in `_process_run`.
2. **Hardened the fold (no silent pending):** the `wait_all` safety net; reference resolution moved **inside**
   the try in `_process_test`; explicit gemini-None check at the top of `_process_test`.
3. Tests updated: `TestExtractVideoTestIds`, `TestVideoAiProcessorDispatch` (per-test dispatch +
   `test_safety_net_fails_unresolved_pending`). `python -m pytest tests/test_video_ai_processor.py -q` → **26 pass**.

Earlier in the session (also in tree, uncommitted): the claude CLI nested-session fix
(`framework/helpers/claude_ai.py` `_claude_subprocess_env()` strips `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT`
so the `claude` CLI runs from inside a Claude Code session); scoped AI references (`asset_groups`/`assets`,
`framework/helpers/symbol_reference.py` + validator); the `click_button` action; and the raw.yml migrations.

---

## Secondary OPEN item (separate from the framework bug)

`games/zeus/paytables/fs-trigger9.json` does **not** force "9 coins on the trigger spin." The live event
stream shows the trigger landing **3** coins (`START_BONUS coinCount: 3`) and respins filling to 9 — i.e. it
behaves like the add-coin variant, with respins (so step-02 `waitForBonusTotalWinPopup` waited ~24s). ZZ-31's
*content* premise (9-on-trigger, **skip** respins, immediate payout) therefore can't pass even once the video
folds. Either fix the fixture to land 9 at once, or revisit whether the mechanic supports it and rewrite ZZ-31.

---

## Reproduce-the-working-path sanity (optional)

These all passed green this session (so the broader pipeline is healthy — the bug is isolated to the
single-test video path): group `core` ZZ-03 (bet-popup taps), `balance-run` (spins via `click_button`),
`fs-trigger3-countdown` (6/6), `fs-trigger3-addcoin` (2/2).
