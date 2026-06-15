# Handoff — Bridge-free Vision Agent for Game Testing

**Date:** 2026-06-14
**Branch:** `main` (all work below is **uncommitted** in the working tree)
**Author context:** built across one long session. This doc lets a new session continue seamlessly.

---

## 1. TL;DR — what this session built

A **bridge-free, vision-driven test layer**: tests are written as plain-English steps
(`do:` / `check:`), and **Claude drives the game by looking at screenshots and clicking pixel
coordinates** — no `window.__gameTestBridge`, no DOM. It works on any canvas game.

Under the hood, the agent runs through **one warm Claude process per test** (persistent
stream-json session), sends each **screenshot inline** (no Read tool), lets the model
**reason** before answering, and **strips redundant prompt text** on follow-up turns. Net: a
turn is ~**3s** on Sonnet, reliably correct.

Also: a **hybrid** Hold & Spin test (`freespin-vision.yml`) where the **bridge (BO API) only
forces the paytable** and **vision does all the gameplay + verification**, and a **replay
video/timeline alignment fix** so the recording includes the setup phase.

The earlier **Playsite URL-resolution work is already committed** (it's in HEAD); it's not part
of the uncommitted set below.

---

## 2. Architecture — how a `do:`/`check:` step actually runs

Four actors per test:

```
MAIN THREAD (asyncio loop)         — StepExecutor / AgentController.run(); owns Playwright Page
   │  await asyncio.to_thread(session.send, …)        (offload blocking I/O)
   ▼
WORKER THREAD                      — ClaudeStreamSession.send(): writes to stdin, blocks on a queue
   │  stdin ▲ │ pipe        queue ▲ (events)
   ▼        │ ▼                    │
claude PROCESS (1 per test)  ──▶  READER THREAD (daemon): drains stdout → queue
  `claude -p --input-format stream-json --output-format stream-json --verbose`
  (model runs REMOTELY via Anthropic API using local Claude Code auth — no API key)
```

**Per `do:` turn:** screenshot the canvas → send it (inline base64 image) to the warm process →
worker thread drains stdout events until the `result` event → parse the JSON action
`{action, x, y, reason}` → perform it (`page.mouse.click` / `keyboard.press` / `wait`) → settle
400ms → loop with a fresh screenshot until the model returns `done`/`fail` (or `max_steps`).

**A `check:`** is the same minus the click: screenshot(s) → `{passed, reason, confidence}` →
the step passes only if `passed == true`.

Key truths:
- It's **sequential**, not parallel. Threads exist only to keep the event loop free and give the
  pipe read a real timeout via the queue.
- **One warm process per test** handles every `do:` turn AND every `check:` — cold-start paid once.
- **`do:` "success" = the model said `done`** (not verified). The **`check:` steps are the real gate.**

---

## 3. Key decisions (and why)

| Decision | Why |
|---|---|
| **Bridge-free vision via the Claude Code CLI sub-agent** (local auth, no API key) | Works on any canvas game; consistent with the repo's existing `AI_PROVIDER=claude` pattern. |
| **One warm process per TEST** (not per-step, not per-suite) | Kills per-turn cold-start (~3–7s) without cross-test contamination or unbounded context. |
| **Hand-rolled stream-json subprocess** (not the Agent SDK) | No new dependency; matches the project's "shell out to `claude`" approach. |
| **Inline screenshots (base64 image block), no Read tool** | Removes a whole model round-trip per turn (~2–3× faster). |
| **Sonnet, reasoning ON** | A/B data: Haiku is ~3× slower AND less accurate on this canvas; reasoning is **free** on Sonnet (Sonnet reasons concisely, Haiku rambles). |
| **Strip goal/schema/history on warm follow-up turns** | Verified the session has conversation memory, so re-sending is redundant (~85% input cut on turns 2+). |
| **Hybrid for Hold & Spin** (`freespin-vision`): bridge=paytable injection only, vision=everything else | Vision can't force a deterministic result; the bridge BO API can. Everything observable is vision. |

---

## 4. Writing tests — the `do:` / `check:` surface

```yaml
setup:
  - id: setup-01
    action: navigate
    params: { url_script: "get_game_url" }
  - id: setup-02
    do: "Wait for the game to load, then tap Start to enter the game."

tests:
  - id: NL-01
    title: "Adjust the bet"
    steps:
      - id: step-01
        action: capture                  # 'before' reference for a ref: check
      - id: step-02
        do:    "Increase the bet by one step (tap the + button once)."
      - id: step-03
        check: "The bet is HIGHER than in the earlier reference screen."
        ref:   step-01
```

- **`do: "<goal>"`** → `agent_action` (vision control loop). Optional siblings: `max_steps`, `model`.
- **`check: "<claim>"`** → `agent_assert` (vision verdict). Optional `ref:` = an earlier `capture`
  step id (supplies a "before" image for before/after claims).
- These are **sugar** — equivalently `action: agent_action` + `params.goal`, or `action: agent_assert` + `params.check`.
- Works inside `tests:` AND `runs:`/`cases:`/`expected_by_case:` (the freespin demo uses runs).
- **Hybrid:** mix `do:`/`check:` with `bridge_action`/`bridge_assert`/`bo_paytable_*` freely.

---

## 5. Files changed (UNCOMMITTED — this is the commit set)

### New files
- **`framework/helpers/agent_control.py`** — the whole vision layer:
  - `ClaudeStreamSession` (warm per-test process: `start`/`send`/`close`, reader thread + queue, `_StreamError`).
  - `AgentController.run` (the `do:` loop), `_AgentDecider` (one-shot fallback), `AgentVerifier` (the `check:` engine).
  - `DecisionLog` (per-test `decisions.jsonl`).
  - Prompt builders `_build_control_prompt` (with `inline` + `follow_up`) and `_build_assert_prompt` (with `inline`).
  - Per-turn timing capture (`_build_timing`/`_emit_timing` → `timings.jsonl`).
- **`games/zeus/test-suites/agent-nl.yml`** — bridge-free NL suite (NL-01 bet, NL-02 spin, NL-03 auto-popup).
- **`games/zeus/test-suites/agent-poc.yml`** — first hybrid POC (agent clicks, bridge verifies).
- **`games/zeus/test-suites/freespin-vision.yml`** — hybrid Hold & Spin (BO paytable + vision + AI video assert).

### Modified files
- **`framework/runner/step_executor.py`** — `agent_action`/`agent_assert` handlers; `do:`/`check:` sugar
  (`_effective_action`); per-test session ownership (`_get_claude_session`, `aclose`, `_get_decision_log`);
  capture-path map for `ref:`; `import os`.
- **`framework/runner/suite_runner.py`** — `await executor.aclose()` after each test/run; **replay video fix**
  (records gained `test_start`; trim anchors at `test_start`, first test/run anchors at `video_start_time`;
  `video_event_offset` set accordingly; suite-setup events prepended only to the first test/run).
- **`framework/helpers/local_ai.py`** — `_build_schema_prompt(allow_reasoning=…)` (env-gated by `AGENT_REASONING`);
  `_extract_json` made tolerant of "reasoning-then-JSON" (last decodable `{...}`); `import os`.
- **`framework/helpers/claude_ai.py`** — `_run_agent` passes `allow_reasoning=True`.
- **`framework/schema_validator.py`** — `agent_action`/`agent_assert` in `SUPPORTED_ACTIONS`; `do:`/`check:` sugar accepted.
- **`framework/schema_reference.yml` / `.md`** — documented the new actions + sugar + `AGENT_STREAM`/`decisions.jsonl`.
- **`games/zeus/env.config.dev.yml`** — currently `url: ""` (dynamic fetch via Playsite). Has the `playsite:` block.

> Note: `scripts/get_game_url_playsite.py`, `scripts/get_url.py`, the stg/prod/local configs and `.env.*.example`
> (Playsite work) are **already committed** (no diff vs HEAD) — leave them.

---

## 6. Configuration & env knobs

| Env var | Default | Meaning |
|---|---|---|
| `AGENT_MODEL` | `sonnet` | Model for the `do:`/`check:` agent. **Keep Sonnet** (Haiku is slower + less accurate here). |
| `AGENT_REASONING` | on | `0`/`false`/`no`/`off` → strict JSON-only (no reasoning). On Sonnet, reasoning is free. |
| `AGENT_STREAM` | on | `0`/`false`/`no` → disable the warm session (one-shot `claude -p` per turn). |
| `AGENT_MAX_STEPS` | `8` | Default turn budget per `do:` (override per step with `max_steps:`). |
| `AGENT_SETTLE_MS` | `400` | Pause after each action before the next screenshot. |
| `CLAUDE_CLI_PATH` / `CLAUDE_CLI_TIMEOUT` | `claude` / `300` | CLI binary + per-turn timeout. |

Run a suite:
```bash
./.venv/Scripts/python.exe framework/runner/suite_runner.py --game zeus --suite agent-nl --env dev --test NL-01
# with overrides:
AGENT_MODEL=sonnet AGENT_REASONING=1 ./.venv/Scripts/python.exe framework/runner/suite_runner.py --game zeus --suite freespin-vision --env dev
```

---

## 7. Artifacts produced per test
- **`decisions.jsonl`** — EVERY decision (each `do:` turn + each `check:` verdict) **with the raw model response**. The audit log.
- **`agent/<step-id>/timings.jsonl`** + `frame-NN.jpg` — per-turn timing + screenshots for a `do:` step.
- **`session.webm`**, **`timeline.json`**, **`replay.html`** — replay (now includes the setup phase, see §9).
- Console also prints per-turn lines and green/red `check[step] PASS/FAIL`.

---

## 8. Performance findings (measured this session)

Per-turn `decision_ms` (warm), driving real Zeus:

| Stage | typical turn |
|---|---|
| Original (cold spawn + Read tool, per turn) | ~14–18s |
| + warm session (cold-start removed) | ~6–10s |
| + inline image + 400ms settle | **~3s** |

Model / reasoning A/B (NL-01, all GREEN unless noted):

| | reasoning | no reasoning |
|---|---|---|
| **Sonnet** | ~3.3s median ✅ | ~3.3s median ✅ (more variance) |
| **Haiku** | ~10.7s ❌ RED | ~8.6s ❌ RED (misreads the bonus, gets stuck) |

Conclusion: **Sonnet + reasoning** is the tuned default. Reasoning's earlier "slowness" was Haiku rambling, not reasoning itself.

---

## 9. Replay video/timeline fix (latest change)

**Problem:** the per-run video trim started at the run's `event_collection_start`, so the **setup
phase** (navigate + the vision `do:` start-game, ~30s) was **cut from the video** even though its
events showed in the replay console → the replay "started late" (at the freespin).

**Fix (in `suite_runner.py`):** anchor each clip at the test's `test_start` (executor start,
*before* its own setup) so **test-level setup** stays in; the **first** test/run anchors at
`video_start_time` so the one-time **suite setup** stays in. `video_event_offset` is set to
`test_start − anchor` (not zeroed), and suite-setup events are prepended only to the first test.

**Status:** verified **offline** (alignment math proven for both suite-setup and test-level-setup
cases). **NOT yet verified on a live run** — blocked by the token issue (§10).

---

## 10. What's LEFT / open items

1. **COMMIT THE WORK.** Everything in §5 is uncommitted on `main`. Recommend a feature branch,
   grouped commits (e.g. `vision-agent core`, `warm session`, `inline + reasoning + strip`,
   `decision logging`, `freespin-vision`, `replay video fix`). Ask the user before committing.
2. **Token issue (recurring blocker).** The dynamic Playsite URL (`url: ""`) sometimes returns a
   token the game **rejects** → *"Session ended" / "Unauthorized"* → setup fails. Either paste a
   fresh `url:` in `env.config.dev.yml`, or investigate `scripts/get_game_url_playsite.py` /
   Playsite creds in `.env.dev`. **Verify the replay fix and re-confirm `freespin-vision` once the token works.**
3. **`freespin-vision` final confirmation.** Last good run was 3 GREEN / 1 ORANGE; the ORANGE was a
   too-strict `check:` wording (bonus result coins remain on the reels) — **already fixed in the file**
   but not re-run to confirm 4 GREEN.
4. **Latency ceiling.** ~3s/turn is mostly image-processing + API round-trip + CLI overhead. Going
   lower likely needs the **Anthropic Messages API** (no CLI process) — discussed, not built.

---

## 11. Known gotchas
- **`Failed to drain bridge events: window.__gameTestBridge is not registered`** — harmless noise on
  vision suites (the runner reflexively polls the in-game bridge, which vision tests don't use). Could
  be silenced for vision suites.
- **Transient Chromium error** `Page.screenshot: Protocol error (Page.captureScreenshot)` seen once —
  retried fine; not code-related.
- **`cli_api_ms` is cumulative-per-session** in streaming mode — use `decision_ms` for per-turn latency.
- **Haiku rambles** under reasoning mode → slow + inaccurate. Don't use Haiku for the agent here.
- The one-shot **fallback** (warm session dead, or `AGENT_STREAM=0`) still uses the **Read tool** +
  full prompt + history (a fresh process has no memory) — by design.

---

## 12. Quick start for the next session
1. Read this doc + `framework/helpers/agent_control.py` (the heart of it).
2. Make sure the game loads: refresh the token (paste a fresh `url:` in `games/zeus/env.config.dev.yml`,
   or fix Playsite creds in `.env.dev`).
3. Smoke test: `./.venv/Scripts/python.exe framework/runner/suite_runner.py --game zeus --suite agent-nl --env dev --test NL-01` → expect GREEN, ~3s/turn.
4. Then confirm the replay fix + `freespin-vision`, and commit (see §10.1).
