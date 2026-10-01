# Session Summary — Response-Driven Scenario QA

A handoff for continuing in a new session. This session designed and built a **scenario +
assert** test framework that drives slot games from the **server BET response** (one source of
truth) and grades with **two tiers**: exact response-engine checks and probabilistic AI-video
checks. Target game: **Mystic** (7×7 cascading); the Mahjong test cases run on the Mystic package.

---

## 1. The core idea

- **One source of truth = the server bet response.** It's stable across every game on the same
  mechanism; per-game *client* implementations are not. So we infer game state (base / freespin /
  etc.) from the response, never from client-state getters.
- **Boundary:** the **bridge** exposes dumb, portable driving primitives; the **framework** owns
  all state inference and driving decisions. A new game ships a new bridge, touches zero framework.
- **Two assert tiers, never blended:**
  - `stream:` → checked by the **response engine** (exact, free). A violation is a real bug → **red** (Hard).
  - `video:`  → checked by **AI over the recorded clip** (perception). A mismatch → **orange** (Probabilistic), never auto-red.
  - Rule: AI can never produce red; a stream pass can never be AI-overridden. Unreached scenario → **inconclusive**.
- **Division of labor:** navigation engine + scenarios = engineers (per mechanism); invariant
  library = senior QA (per mechanism); scenarios + asserts = QA (per test).

## 2. The bridge contract (split primitives, implemented on the Mystic bridge)

Base spin (framework drives a REAL coordinate click):
`waitForSpinClickable(stableMs?, timeoutMs?=60000)` → rect → `getButtonRect('spin')` →
`page.mouse.click(centerX,centerY)` → `awaitBetResponse(timeoutMs?=15000)` (cursor-based,
returns the raw `GameBetResult`).
Free-spin phase (client auto-plays, no click): `awaitFreespinRound(timeoutMs?=60000)` — returns
every round incl. the terminal one. Both reject with `{reason, snapshot}`; consume ONE per round.
`waitForSpinClickable` also owns clearing celebrations (so it doubles as the "presentation done"
signal). Also used: `startGame`, `getButtonRect`, `getSnapshot`/`getGameInfo` (config only),
`scrollBetRowIntoView(i)`, auto buttons (`auto`, `auto:count:0..4`, `auto:confirm`), `history`.
`BetRecord` is now raw `{seq, t, result}`; the framework reads domain fields off `result`.

**Mystic response facts (confirmed from live captures):**
- `gameState`: 1=base, 2=freespin, 3=buybonus, 4=promo. The **trigger** spin already carries 2 (with stake>0).
- Free spins have `stake:0`; `result.balance` is authoritative (post-payout). Conservation: `balance == prev - stake + payout`.
- `freeSpins` = remaining (0 on terminal); `totalFreeSpin` constant; `roundId` spans a bonus; `result[]` = cascades (one response = one spin, cascades inside).
- `hasReachedMaxWin` truncates the chain (terminal can be gameState:1 with freeSpins>0). Terminate on `gameState==1 or freeSpins==0`.
- 7×7 has no forced-outcome paytable → YOLO (real RNG, spin-until-condition).
- A free spin is an AUTO-played round (`Bet.phase=='freespin'`), NOT `gameState==2` — the trigger is a paid base bet that also carries gameState 2, so invariants key off **phase**.

## 3. The scenario + assert model  (full spec: `docs/scenario-assert-authoring.md`)

- **Scenario = a closed, engineer-owned box.** Public contract = `name` + `description` + exposed
  outputs (`video`, `response`/`rounds`, `facts`, `marks`). Reach / start / stop / collect timing
  is hidden Python. `run(ctx) -> ScenarioResult`. Unreached-within-budget → inconclusive.
- **`stream:`** asserts — a named invariant (`conservation`, `stake_zero_in_freespins`,
  `freespins_monotonic`, `terminal_reached`) over the collected rounds, or an inline predicate
  (`"{res.stake} > 0"`, `"{facts.rounds_played} == {facts.auto_count}"`).
- **`video:`** asserts — three forms:
  - string → boolean `check` claim;
  - `{read, equals|near}` → AI extracts a value, the framework compares;
  - `{prompt, output_schema, expected}` → raw.yml style (AI judges boolean fields vs `expected`). **This is the main form the suites use.**
  - `at:` scopes the clip to a sub-window: a scenario **mark** (e.g. `win`, `big_cascade`, `spin`) or `trigger` / `terminal` / `res` / `rounds[i]`. Omit → whole-scenario clip.
- **Binding:** `{res.*}` (single round), `{trigger.*}` / `{terminal.*}` / `{rounds[i].*}` (multi-round),
  `{facts.*}` (scenario-exposed values). Reads the raw response field first, falls back to a computed `Bet` attribute (e.g. `cascades`).
- **AI prose reason:** every video assert auto-gets a `reason` field + a prompt tweak asking WHY;
  on a mismatch the reason is surfaced in `note`, `report.html` ("why: …"), and `result.json`. You never declare `reason`.
- **Integration:** this IS the existing `runs`/`cases` pipeline with two swaps (`scenario` replaces scripted `steps`; `stream`/`video` replace `bridge_assert`/`ai`). One scenario play → many case verdicts.

## 4. Files (all under `scripts/`, standalone — NOT yet folded into `framework/runner`)

| File | Role |
|---|---|
| `scripts/scenario_engine.py` | `Scenario`/`ScenarioResult` contract, `DriveContext` (drive primitives + records replay events/timeline + video sub-windows), invariant library, `{...}`/`at:` resolution, all scenarios + `REGISTRY` |
| `scripts/run_scenario.py` | the runner — boots, drives each scenario, grades stream asserts, runs the AI pass, writes `result.json`/`events.jsonl`/`timeline.json`/`replay.html`/**one shared `session.webm`**/`summary.json`/**`report.html`** |
| `scripts/ai_video.py` | AI video-assert pass — ffmpeg-trims each assert's sub-clip, runs the provider (`GeminiVideoHelper`/`ClaudeVideoHelper` via `AI_PROVIDER`), grades to pass/orange (never red), captures the prose `reason` |
| `scripts/probe_mystic_split_drive.py` | the original probe proving response-only driving (kept for reference) |
| `docs/scenario-assert-authoring.md` | the authoring spec |

**Scenarios in `REGISTRY`:** `base-cluster-win`, `fs-complete`, `change-bet`, `boot-base`,
`base-spin`, `browse-denominations`, `auto-play`, `spin-then-history`, `insufficient-balance`.
**Invariants:** `conservation`, `stake_zero_in_freespins`, `freespins_monotonic`, `terminal_reached`.

## 5. How to run

```powershell
# whole suite:
python scripts/run_scenario.py --env dev --suite games/mystic/test-suites/mah-001-020.yml
# one scenario from a suite:
python scripts/run_scenario.py --env dev --suite games/mystic/test-suites/mah-001-020.yml --only change-bet
# ad-hoc single scenario (default invariants):
python scripts/run_scenario.py --env dev --scenario fs-complete
# flags: --game (default mystic) · --url "..." (override stale config URL) · --no-ai · --headless · --slow-mo
```
Output dir: `test-results/{game}/{env}/scenario-<ts>/` — open **`report.html`** (verdicts + AI
answers + "why" reasons); `session.webm` is one shared video at the run root (replays use `../session.webm`).

**Requirements:** Python 3.13 + Playwright + Chromium (present), ffmpeg (present), `GEMINI_API_KEY`
in `.env` (present, `AI_PROVIDER=gemini`). For `insufficient-balance`: `PLAYSITE_API_URL/USER/PASS`.

## 6. Mah_001–020 coverage (suite: `games/mystic/test-suites/mah-001-020.yml`)

| Automated | Scenario | Notes |
|---|---|---|
| Mah_001 UI at load | boot-base | video |
| Mah_004 denom = config | browse-denominations | scrolls to biggest+smallest, video |
| Mah_005 bet +/- | change-bet | video: displayed == picked |
| Mah_006 default bet | boot-base | video vs `{facts.default_stake}` |
| Mah_007 balance debit | base-spin | stream conservation + video |
| Mah_008 balance credit | base-cluster-win | stream conservation + video |
| Mah_009 insufficient balance | insufficient-balance | wallet_set low + reload; needs PLAYSITE_* |
| Mah_011 balance in FS | fs-complete | stream stake_zero_in_freespins + video (`at: trigger`) |
| Mah_012 in-game history | spin-then-history | spin → open history → AI: last row == spin |
| Mah_014 auto stops after N | auto-play | stream `{facts.rounds_played}=={facts.auto_count}` + video |
| Mah_019 buttons during auto | auto-play | video |

**Pending:** Mah_002/003 (loading timing — needs navigate-and-record boot), Mah_010 (auto+empty),
Mah_013 (BO history — external), Mah_015–018/020 (auto variants: stop-at-win / user-stop / reload /
change-bet-first).

## 7. Bugs fixed this session

- `ai_video.trim_clip` was missing the ffmpeg **output path** → every clip failed ("no output file")
  → 0 AI calls, all orange. Fixed (append `str(dest)`; fallback re-encode to vp9).
- `summary.json`/report weren't written when a run errored or the bridge check early-returned → now
  always written; per-run errors recorded and skipped, not fatal.
- Free-spin classification keyed off `gameState==2` misflagged the trigger; now keyed off `Bet.phase`.
- `{res.cascades}` (a computed field) didn't bind; binding now falls back to `Bet` attributes.
- Long-scenario video: `fs-complete`'s no-`at:` clip was ~160 s (≈ whole session) → scoped Mah_011 to `at: trigger`.
- Per-scenario `session.webm` copies (11× 200 MB) → one shared `session.webm` at the run root.

## 8. Open items / next steps

1. **Live validation needed** (couldn't test — dev server was down, `ERR_CONNECTION_REFUSED` at
   `192.168.1.27:7459`; get a fresh launch URL via `--url` or update `env.config.dev.yml`):
   - `auto-play`: assumes `awaitBetResponse` returns the client's auto-driven spins (user said yes).
   - `insufficient-balance`: needs `PLAYSITE_*`; reloads + restores balance.
   - `spin-then-history`: assumes the `history` button opens the panel with the latest round on top.
2. **`inconclusive` verdict** is honored in `run_scenario.py` but NOT yet in the framework's
   triplicated `_compute_verdict` (`suite_runner`/`reporter`/`video_ai_processor`) — the F5 item.
3. **Fold the prototype into `framework/runner/suite_runner.py`** (the real pipeline: multi-env, BO,
   Reporter, batch AI). `run_scenario.py` is the executable spec for that.
4. Build the remaining scenarios: auto variants, a navigate-and-record boot (loading), a history-detail reader.
5. Design decision to revisit: whether `confidence` belongs in `expected` (exact-match makes verdicts
   sensitive to the model's self-rating) vs. treating it as a gate (low → orange, else judge fields).

## 9. References

- Spec: `docs/scenario-assert-authoring.md` · Runner internals: `docs/framework-runner-guide.md`
- Bridge docs: `games/mystic/bridge-qa-api.md`, `games/zeus/bridge-api.md`
- Suites: `games/mystic/test-suites/{mah-001-020,scenario-demo,change-bet,test}.yml`
- Memory: `project_response_driven_driving.md` (kept current with the above)
