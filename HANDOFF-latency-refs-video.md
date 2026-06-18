# Handoff — Latency tuning, scoped reference `assets`, inline video assert

**Date:** 2026-06-15
**Branch:** `main` (everything below is **uncommitted** in the working tree, layered on top of the
prior session's uncommitted "vision agent" work — see `HANDOFF-vision-agent.md`)
**Continues:** `HANDOFF-vision-agent.md` (bridge-free vision agent). Read that first for the base
architecture (warm `claude` stream-json session, `do:`/`check:` surface, `agent_control.py`).

---

## 1. TL;DR — what this session did

1. **Traced the per-turn latency** and proved the **model decision is ~93% of every turn**; the test
   runner's overhead (screenshot + send + click + settle) is only ~7% (~450ms). The "7–10s/turn"
   feel is the decision itself growing/varying, not runner overhead.
2. **A — immediate `wait`:** a `wait` no longer sleeps; the decision round-trip already elapsed since
   the frame was captured, so we re-screenshot immediately. Settle only after `click`/`press_key`.
   Saves ~2–3s per wait turn. **Kept.**
3. **B — pre-warm pool (ping-pong):** built `PrewarmStreamPool` (background-spawn a replacement
   process, swap when booted) + self-contained prompts. **A/B-measured it: ~25% SLOWER** because
   accumulation is NOT the bottleneck (the CLI/API prompt-caches context; swaps defeat the cache).
   **Defaulted OFF** (`AGENT_PREWARM=0` → single warm session). Pool kept as opt-in only.
4. **#1 — restored `follow_up` prompt-stripping** for the single warm session (gated on a
   `stateless_turns` flag, so the pool still uses self-contained prompts).
5. **CLI-path fix:** `_resolve_cli()` now falls back to the **native install** `~/.local/bin/claude.exe`.
   Claude Code migrated off the npm global, the old `C:\Program Files\nodejs\claude.CMD` shim vanished,
   and every run was dying with `[WinError 2]`. This is why nothing ran until ~14:00.
6. **Inline video assert** (replaces the slow Read-tool-per-frame path that timed out at 300s):
   adaptive frame sampling + inline base64 frames + ref-aware image budget + **token-aware** budget.
7. **`assets:` feature** — per-test/group `assets: [symbols, ui_elements]` scopes which asset
   categories from `assets.yml` load (inline images **+ descriptions**) into BOTH the do/check agent
   prompt AND the AI video/image asserts.
8. **Per-image captions** — each image is now preceded by a text block describing it
   ("This image shows Coin (Hold & Spin) — …", "This is gameplay frame 7/56 …") instead of a legend.
9. **freespin-vision.yml**: removed the racy live `step-02` (ZZ-30 now rides on the video assert);
   added `assets: [symbols, ui_elements]` at the group level.

**C (an `AGENT_IMG_QUALITY` knob) was added then removed** at the user's request.

---

## 2. The big latency finding (so nobody re-derives it)

Per-turn flow (`AgentController.run`), measured from `timings.jsonl`:

| Segment | mean | share |
|---|---|---|
| screenshot (`page.screenshot` → jpeg) | ~237ms | 3.6% |
| send-prep (base64 + json + stdin) | ~41ms | 0.6% |
| **decision (CLI→API round-trip)** | **~6s** | **~93%** |
| parse + action (click ~35ms / wait 0) | ~15ms | 0.2% |
| settle (400ms, click/key only) | ~160ms avg | 2.4% |

- **The decision dominates.** Optimising screenshot/send/settle saves <0.7s/turn.
- **The pre-warm pool did NOT help** — the warm session's per-turn latency stays roughly flat as
  context accumulates (prompt caching), so swapping to fresh processes just pays cache-miss prefill.
  A/B on a fixed 11-turn step: single session warm-mean **5352ms** vs pool **6688ms**. Pool is off.
- **The two real levers** (not both done): (a) **eliminate `wait`-turn model calls** via local
  frame-diff settling — each `wait` is a full ~5s decision just to watch reels spin; (b) **cut output
  tokens** (reasoning length). A (immediate wait) was the cheap win actually shipped.
- **Today's API was brutally slow** (15–30s/decision, one 42s) — pure provider variance, not code.
  It amplified every fragility below.

---

## 3. Inline video assert — how it works now

Old path: ffmpeg sampled the trimmed clip at 2fps → a one-shot `claude -p` **Read**s each frame from
disk → ~120+ sequential tool round-trips → **300s timeout**. Replaced with:

1. `_probe_duration` (ffprobe) → clip length.
2. **Token-aware + count-aware frame budget** (in `ClaudeVideoHelper.analyse_video`):
   `frame_budget = min(CLAUDE_AI_MAX_FRAMES, 100 − n_refs − 2, token_budget_frames)`,
   where `token_budget_frames = (CLAUDE_AI_TOKEN_BUDGET − n_refs*500 − 4000) / tokens_per_frame` and
   `tokens_per_frame = (w*h)/750` from `_probe_dimensions`.
3. `_adaptive_fps(duration, base_fps, frame_budget)` — keeps base 1fps for short clips; steps the
   interval up in whole seconds (1fps → 1/2s → 1/3s …) so the count fits the budget.
4. `_sample_frames` (clears stale frames first) → `frame-0001.jpg …`.
5. `_run_agent_inline` sends ONE stream-json `user` message: text prompt + **interleaved
   caption/image blocks** (refs first with their descriptions, then frames with chronological
   captions), via `_image_content_blocks`. Reads the `result` event. `communicate(timeout=600)`.

**Why both budgets:** the image-COUNT cap (100) is not enough — a long clip at 1fps can blow the
**context window** even under 100 images (`"Prompt is too long"` observed at ~85 images of 530×918 ≈
~57K tokens). The token budget (default 48000) caps frames by estimated tokens. Verified offline:
the failing 131s/21-ref case drops from **87 → 64 images (~39K tokens)**.

⚠️ **The token-aware fix is verified OFFLINE only.** The next live run died at BO auth (see §6) before
reaching the video. **Immediate next step:** confirm it live — either re-run the suite (needs BO
re-auth) or run the video analysis standalone on the existing clip
`test-results/zeus/dev/freespin-vision-15-06-2026-2041/fs-3coin-countdown-vision-run/trimmed-step-05.webm`
(BO-free; that is exactly the clip that produced "Prompt is too long").

---

## 4. The `assets:` feature (scoped reference pack)

Goal: a test/group declares which asset categories the AI should have as ground truth, loaded inline
with descriptions, for BOTH the agent loop and the AI asserts.

```yaml
groups:
  - id: fs-trigger3-countdown
    assets: [symbols, ui_elements]   # categories = top-level keys in assets.yml
    runs: [...]
```

- **Resolution:** test/run-level `assets:` overrides group-level (`effective_assets(group, test)`);
  absent → `None` (agent loop: no refs; AI asserts: the suite-wide full pack — backward compatible).
- **Loader:** `load_symbol_reference(game, base_dir, categories=None)` — `None` = full pack
  (`symbols`+`ui_elements`); a list loads only those categories. `ScopedReferenceCache` memoises.
- **Manifest:** `games/<game>/docs/assets/assets.yml` (confirmed it loads from there, NOT the legacy
  `symbols.yml` which also exists and is now dead). Image `file:` paths resolve relative to the
  assets dir (`symbols/*.jpg`, `ui/*.jpg`). zeus = 9 symbols + 13 ui = 22 entries, 2 missing on disk
  (`stop_button`, `big_win_popup`).
- **Wiring:** `suite_runner` resolves the scoped ref per test/run → `StepExecutor(reference=…)` AND
  `record.reference`. `StepExecutor` passes it to `AgentController`/`AgentVerifier`. The
  `VideoAiProcessor` calls `_apply_reference(record)` to point the video helper at the record's
  scoped ref (else its full `_full_reference`).
- **Agent injection:** refs are attached inline **once per warm session** (a `refs_sent` flag on the
  session; reset on a pool swap) — re-attaching every full turn would multiply images toward the
  100-image cap. Follow-up turns send only the screenshot.

### Per-image captions (this session's last change)
`_image_content_blocks(image_paths, captions)` (in `claude_ai.py`, shared by the agent session and
the video) interleaves `text(caption) → image` so each picture's description sits right before it.
- Reference caption: `"This image shows {label}"` where `label = "{name} — {description}"`
  (the internal manifest key and the word "Asset" were dropped — model-facing text is human-readable).
- Current screen: `"This is the CURRENT, LIVE game screen — act on THIS (it is not a reference):"`.
- Video frame: `"This is gameplay frame i/N (chronological order)"`.
- The master-prompt note (`_reference_legend`) was reframed: references are the **expected appearance
  to judge against**, not just recognition hints.

---

## 5. Files changed this session (UNCOMMITTED)

- **`framework/helpers/agent_control.py`** — A (immediate wait: `_perform` wait no-op + settle only
  on click/key); `PrewarmStreamPool` (opt-in); `stateless_turns` flag + `follow_up` restore in
  `_decide`; `reference` on `AgentController`/`AgentVerifier`; `_reference_legend`/`_reference_items`;
  inline-caption attach (once-per-session `refs_sent`); imports `_image_content_blocks`;
  `ClaudeStreamSession.send`/`PrewarmStreamPool.send` gained a `captions` arg.
- **`framework/helpers/claude_ai.py`** — `_resolve_cli` native `~/.local/bin` fallback; `_media_type`,
  `_image_content_blocks`, `_run_agent_inline`; `_probe_duration`, `_probe_dimensions`, `_adaptive_fps`;
  token+count-aware budget in `analyse_video`; `_build_inline_video_prompt`; default changes
  (`_DEFAULT_FPS=1`, `_DEFAULT_MAX_FRAMES=95`, `_HARD_IMAGE_CAP=100`, `_DEFAULT_TOKEN_BUDGET=48000`,
  `_DEFAULT_TIMEOUT=600`).
- **`framework/helpers/symbol_reference.py`** — `load_symbol_reference(categories=…)`,
  `effective_assets`, `ScopedReferenceCache`; label format dropped `Asset '{key}' —`.
- **`framework/runner/step_executor.py`** — `reference` ctor param threaded to
  `AgentController`/`AgentVerifier`; `AGENT_PREWARM` session-type selection; `_decide` turn_index.
- **`framework/runner/suite_runner.py`** — `ScopedReferenceCache` + `effective_assets` per test/run;
  `reference` threaded through `_execute_test`/`_execute_run`/`_run_one_test`/`_run_one_run` →
  `StepExecutor`; `record.reference`. (Plus the prior session's replay-video fix + `executor.aclose()`.)
- **`framework/runner/video_ai_processor.py`** — `_full_reference` capture + `_apply_reference(record)`
  before each `analyse_video`.
- **`framework/schema_validator.py`** — `_validate_assets` at test/run/group level.
- **`games/zeus/test-suites/freespin-vision.yml`** — removed `step-02`; added `assets:` at group level.
- **`games/zeus/test-suites/agent-latency.yml`** — NEW throwaway suite (BO-free long spin) for latency
  A/B. Can be deleted.
- A temp `C:\Users\PC\AppData\Local\Temp\vidtest\` dir from an isolated test (harmless, deletable).

---

## 6. Recurring blockers (all environmental, NOT code)

1. **BO API auth expires** (`bo_paytable_*` → `"Unauthorized — re-login required"`) ~every few hours →
   suite setup fails → all cases RED. Re-auth back-office (done twice today). The game launch token is
   separate and was usually fine. The LAST run (20:53) died here.
2. **Playsite game-launch token** (`url: ""` dynamic fetch) sometimes rejected → paste a fresh `url:`
   in `games/zeus/env.config.dev.yml` or fix Playsite creds.
3. **`window.__gameTestBridge` never registers** on the current dev build — fatal for the bridge-based
   `freespin-runs.yml`, harmless noise for vision suites. (This is *why* the vision approach exists.)
4. **CLI path** — fixed this session (`_resolve_cli` native fallback). No longer a blocker.

---

## 7. Known fragilities of `freespin-vision` (why it rarely goes all-GREEN)

These are independent; on a slow-API day they rarely all align:

- **`step-01` live bonus-catch is RACY.** The forced 3-coin countdown bonus auto-plays out in a few
  seconds; with 15–30s decisions the agent often `wait`s "to confirm" and the bonus is gone before it
  says `done` (passed 1 of 2 runs). Same fragility we removed from `step-02`. Possible fix: give the
  **`do:` loop an action-first framing** (decide `done` the instant the condition is visible) while
  keeping "judge against references" for `check:`/video asserts. NOT done.
- **`confidence: high` gate flaps.** The video returns the correct gameplay verdict but rates itself
  `high` one run, `medium` the next on identical input → `confidence` mismatch → ORANGE. The user
  **wants** this gate (medium/low ⇒ a human QA rechecks), so it's by-design noise, not a bug.
- **Video "Prompt is too long"** — fixed via the token-aware budget (§3), pending a live confirm.

Net: the vision gameplay (do/check) and the inline video assert both work; ZZ-32/33/35 have gone
GREEN, ZZ-30 rides the video. A fully GREEN run needs step-01 to win its race AND confidence to land
`high`.

---

## 8. Config / env knobs (current defaults)

| Var | Default | Meaning |
|---|---|---|
| `AGENT_MODEL` | sonnet | agent do/check model (keep sonnet) |
| `AGENT_REASONING` | on | reasoning before JSON |
| `AGENT_STREAM` | on | warm session (0 → one-shot per turn) |
| `AGENT_PREWARM` | **0 (off)** | 1 → opt into the (slower) pre-warm pool; only caps context on very long single steps |
| `AGENT_PREWARM_READY_MS` | 9500 | standby boot time before promotion |
| `AGENT_MAX_STEPS` | 8 | default `do:` turn budget |
| `AGENT_SETTLE_MS` | 400 | pause after click/key (not after wait) |
| `CLAUDE_AI_FPS` | **1** | base video sample fps (auto-reduced for long clips) |
| `CLAUDE_AI_MAX_CONCURRENCY` | **2** | max concurrent claude AI-assert processes (shared by at-step image + video asserts); gemini/local unbounded |
| `CLAUDE_AI_MAX_FRAMES` | 95 | image-count side of the frame budget |
| `CLAUDE_AI_TOKEN_BUDGET` | 48000 | token side of the frame budget ("too long" guard) |
| `CLAUDE_CLI_TIMEOUT` | **600** | per-call CLI timeout (video inline-prefills many frames) |
| `CLAUDE_CLI_PATH` / `CLAUDE_MODEL` | claude / sonnet | CLI binary (auto-falls back to `~/.local/bin/claude.exe`) |

Run:
```bash
./.venv/Scripts/python.exe framework/runner/suite_runner.py --game zeus --suite freespin-vision --env dev
# latency A/B (BO-free):
$env:AGENT_PREWARM='0'; ... --suite agent-latency --env dev
```

---

## 9. Quick start for the next session

1. Read `HANDOFF-vision-agent.md` then this doc. Heart of it: `agent_control.py` + the inline-video
   path in `claude_ai.py`.
2. **Re-auth BO** (and refresh the game `url:` if needed) so a full suite run can reach gameplay.
3. **Confirm the token-aware video fix live** (the one thing not yet verified end-to-end) — full
   suite, or the standalone video harness on `…/freespin-vision-15-06-2026-2041/…/trimmed-step-05.webm`.
4. Decide on the two fragilities in §7: action-first `do:` framing for step-01; whether to soften the
   confidence gate (the user said keep it strict).
5. **Commit.** Everything is uncommitted on `main`. Recommend a feature branch with grouped commits
   (latency/A + pool-opt-in, CLI fallback, inline-video + budgets, `assets` feature, captions). Ask
   the user before committing.

---

## 10. Gotchas

- Pre-warm pool default is OFF on purpose (measured slower). Don't "re-enable for speed."
- Refs are attached **once per session** — don't move the attach to every turn (100-image cap).
- The video budget is BOTH count- and token-bounded; a count-only cap re-introduces "Prompt is too
  long" on long clips.
- `assets.yml` is authoritative; the sibling `symbols.yml` is dead (loader prefers `assets.yml`).
- `confidence: high` in `expected_by_case` is compared with strict equality — `medium` ⇒ ORANGE by
  design (user's choice). Don't "fix" it without asking.
- Today's API latency (15–30s) is provider variance; don't attribute slow runs to the code.
