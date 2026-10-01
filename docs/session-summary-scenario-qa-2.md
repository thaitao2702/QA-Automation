# Session Summary 2 — Scenario QA: coverage build-out, shared video, video modes, BO paytables

Continuation of `docs/session-summary-scenario-qa.md`. That session built the response-driven
**scenario + assert** prototype (drive from the server BET response; grade with `stream:` exact / `video:`
AI tiers). THIS session scaled it: teardown/setup lifecycle, a big scenario library, shared-video
grading, AI video sampling modes, real input primitives, and BO-paytable forcing — targeting the
Mah_001–108 test cases (`test-case-mahjong.csv`). Now **45/108 automated**.

Everything still lives in the standalone `scripts/` prototype (NOT yet folded into `framework/runner`).

---

## 1. Files touched this session

| File | Role |
|---|---|
| `scripts/scenario_engine.py` | `DriveContext` (drive + input primitives), all scenarios + `REGISTRY`, invariants, `TEARDOWNS`, `resolve_window`/binding, `plan_insufficient`, FS helpers |
| `scripts/run_scenario.py` | the runner — lazy game load, per-run BO paytable setup/teardown, run/suite teardowns, shared-video `ai_calls()`, artifacts |
| `scripts/ai_video.py` | AI video pass — trim, montage, grade; `grade_schema` (shared), `_timeline_hint` (ACTION TIMELINE), group caching (one call per shared video) |
| `scripts/derive_paytable.py` | **new** — derive forced/probability paytable variants from a base |
| `framework/helpers/gemini_video.py` | montage builder — now parametric: `high_fps` (16fps) + `timestamp` (drawtext clock) |
| `framework/helpers/claude_ai.py` / `local_ai.py` | `analyse_video` gained `high_fps`/`timestamp` kwargs (parity) |
| `games/mystic/test-suites/mah-*.yml` | suites (see §6) |
| `games/mystic/paytables/` | **new** — `base.json`, `mystic-glow-2col.json`, `mystic-fs-easy.json`, `README.md` |
| `docs/mahjong-coverage.md` | **new** — full Mah_001–108 coverage map |

---

## 2. Core model (unchanged from session 1, extended)

- Drive game LOGIC from the **server BET response** (uniform across games); never client-state getters.
- **`stream:`** asserts → response engine (exact, Hard/red). **`video:`** → AI over the clip (Probabilistic/orange, never red). Unreached → inconclusive.
- A scenario is a closed box (`run(ctx) -> ScenarioResult`); one play → many case verdicts.
- **Binding**: `{res.*}` / `{trigger.*}` / `{terminal.*}` / `{rounds[i].*}` (raw response field, falls back to a computed `Bet` attr) and `{facts.*}` (scenario-exposed values). Inline stream predicates eval in a no-builtins sandbox.

---

## 3. Lifecycle: teardowns + setup (the big additions)

**Teardowns** — opt-in, NAMED (registry `TEARDOWNS`), mirror scenarios/invariants. Two hooks in a suite:
- **run-level** `teardown:` on a run → runs right after that run. A failure ABORTS the suite.
- **top-level** `teardown:` in the suite → runs once after ALL runs.

Named teardowns:
- **`return-to-base`** — response-driven: reads `ctx.last_bet`; if a bonus is still open (`free_spins>0 and game_state!=1`) it drains free spins to base, then settles. No-op if no bet / already base.
- **`deposit-10000`** — tops the wallet up +10000 (whole units) after a balance-draining test.

**Setup = paytable, before load.** A run may declare `paytable: { ref: <fixture> }`. The runner does the
Zeus-style `ensure → select → update` **before** the game loads (see §7), and `reset` after. Requires BO.
There is no other generic setup hook — setup that must precede load is expressed via `paytable:`.

---

## 4. Assert model additions

### Shared video + `expected_by_case` (one AI call, many cases)
A run can carry a run-level `video:` block with `expected_by_case: { CaseId: {field: expected, ...} }`.
`build_cases` expands it into a per-case spec sharing a `group` key; `ai_video.run_ai_pass` **calls the
model once per group** and re-grades the cached answer against each case's subset (`grade_schema`).
`ai_calls(cases)` reports the true (deduped) call count in the log. Used by Mah_044/045, 047/048/051,
049/050, 014/019, 057/059/060, and the reconnect brief/turbo/spin runs.

### `resolve_window` spans
`at:` accepts a 2-element `[start, end]` (e.g. `[win, reset]`, `[trigger, "rounds[1]"]`, `[fs_cascade, fs_reset]`)
→ clip from the first target's start to the second's end. Lets one shared clip span two moments.

### `confidence` is a GATE, not exact-match
`grade_schema` treats `confidence` as `actual >= expected` (`low<medium<high`). `expected: medium` passes
on `high`. Fixed a real bug where correct-but-high-confidence answers went orange.

### `params:` — scenario constructor arguments from the suite
`REGISTRY` holds pre-BUILT instances, so every scenario default (which language to switch to, how many
burst cycles, which surfaces) used to be frozen in Python. A run may now carry `params: {...}`; the
runner builds a FRESH instance via `type(scenario)(**params)`, leaving the registry one untouched. A
bad kwarg is a skipped run naming the offender, not a silent default. Used by Mah_053
(`params: {from: th, to: my}` — both ends of a language switch are QA's to pick, and the prompt reads
them back through `{facts.from_name}` / `{facts.to_name}` so changing the pair needs no prompt edit).
`LanguageSwitch` validates both codes against `SUPPORTED_LANGUAGES` and rejects from == to.

### Mah_052-056 are ONE run
Every language case asks a different question about the same artefact — a screen of text — so the
suite tours once, screenshots each surface, grades each screenshot ONCE and re-reads that answer per
case (`expected_by_case`, 8 boolean fields). Five cases for one pass, replacing four runs (46 calls,
overlapping coverage) with one that covers strictly more. Because `set_language()` re-enters the game
between languages, the tour performs REAL mid-session switches (`facts.switches`), which is what makes
Mah_053 genuine here rather than assumed; shots after a switch are labelled
`"<code> <name> (switched from <prev>) - <surface>"` so the stale-string question can name the
language it is hunting. Per-case `stream:` asserts enforce that the run actually covered what the case
claims: 053 needs `switches >= 1`, 054/055 need the stress language in the set (they are about layout
under the longest strings), and every case needs `language_count == requested` so a language that
failed to LAUNCH cannot shrink the run into a pass.
**Scope-independent by design:** each case asks "did all the languages in THIS run pass", and an
`image:` assert passes only if every screen of every language passes — so one bad language fails the
case at 3 languages or at 16, and the run can be sized to the time available. `facts.selection`
records what was covered, so a green verdict is always read together with its scope.
`language-switch` / `language-sweep` stay registered for ad-hoc single-pair or fast sweeps.

### Language SELECTION (`select_languages`) — full / sample / pinned set
`language-tour` takes `params: {languages, seed, include, surfaces}` and tours EACH selected language
over the chosen surfaces, labelling every shot `"<code> <name> - <surface>"` so one `image:` assert
covers the matrix and `{shot.label}` tells the model which language AND screen it is looking at.
Forms: `all` · `random:N` · `[th, my, ja]` · `th` · omitted (whatever the run loaded).
**Random is always SEEDED** — an unseeded draw picks a seed and reports it in `facts.selection`, so a
failing sample can be replayed exactly; `include:` pins a baseline (en) and the stress language (my)
into every draw. Cost is `languages x surfaces` AI calls, so `surfaces:` is the second lever
(`all` + 2 surfaces = 32 calls vs 96 for the full 16x6). Bad codes/surfaces raise at CONSTRUCTION —
before a browser is driven or a token minted. A language that fails to launch is recorded in
`facts.failed` and caught by `{facts.language_count} == {facts.requested}`, so a sweep cannot shrink
silently into a pass.

### `image:` asserts — one AI call per SCREENSHOT (added for Mah_052-056)
A third assert kind beside `stream:`/`video:`. `ctx.screenshot(label)` writes a full-resolution PNG
to `<run>/_shots/` and registers it on `ScenarioResult.shots`; an `image:` assert grades ONE AI call
PER SHOT and passes only if EVERY shot passes, naming the ones that failed. `at:` filters shots by a
label substring (`at: AFTER`); a run-level `image:` block with `expected_by_case` re-grades one
answer per case (Mah_054/055 share 6 calls, not 12); `{shot.label}` in the prompt is replaced per
shot, so one assert can say something different about each screen (the language sweep names the
language each screen should be).
**Why not video for these:** a video assert grades a DOWNSAMPLED montage — a missing Burmese
diacritic or a two-pixel clip is not in those pixels — and one answer over six screens lets a single
bad surface vanish into the aggregate. Rule of thumb: `image:` when fine detail decides it (text,
glyphs, clipping), `video:` when motion or time does.

### Video sampling MODES (per `video:` assert)
- **`high_fps: true`** → montage samples at 16fps effective (4 composites/sec × 4 sub-frames) vs the
  default 4fps — for fast holds / sudden stops.
- **`timestamp: true`** → ffmpeg `drawtext` burns a clip-relative clock onto every sub-frame, AND
  `ai_video._timeline_hint` reads the run's `timeline.json` and appends an **ACTION TIMELINE** to the
  prompt (clip-relative times for `net`/`hold`/`key`/`dblclick`/`click`/`scroll`/`second_session`/`mark`
  events). So the AI correlates *when* we acted with the burned timestamps.
- **`ctx.mark(label)`** records a labeled timeline marker → shows in the ACTION TIMELINE as
  `"<label>: at t=Xs"`. `spin-inputs` marks each of its 5 inputs; the prompt references the labels
  instead of unanchored (a)–(e). Reconnect/multi-tab reference `net`/`second_session` events.
- Plumbing: YAML → `_video_spec` → `ai_video.run_video_assert` → `provider.analyse_video(..., high_fps=, timestamp=)`.
  (Fixed: `ai_video.py` was missing `import json`, which silently emptied every ACTION TIMELINE.)

---

## 5. `DriveContext` primitives (new this session)

- **UI dwell**: `ui_dwell_ms` (default 3000). `click_button`/`scroll_bet_row` DWELL ~3s after each UI
  change so it lands + is seen by the AI (`dwell_ms=` overrides; spins are NOT dwelled).
- **Input**: `hold_button(name, hold_ms, use_bridge=)` (real pointer down→hold→up, or bridge
  `holdSpinStart/End`), `double_click_button`, `hold_key`, `press_key`.
- **Network**: `set_offline(bool)` (Playwright/CDP) — records a `net` timeline step.
- **Multi-session**: `open_second_session()` / `close_second_session()` — fresh context, same account URL
  (needs `browser`+`url`, passed by the runner).
- **UI / popup observation** (added for Mah_105–108): `ui_read(js, default)` (best-effort bridge read
  that returns `default` instead of failing the run), `popups()` / `dismiss_popups()`,
  `start_capture()` / `end_capture()` (bridge event window → `popup_shown` labels), and
  `start_popup_watch()` / `stop_popup_watch()` (background poll of the visible-popup list →
  `{order, max_concurrent, samples}`, for a rapid burst where clicking can't stop to look).
  These read the SCREEN — legitimate only because the subject of a popup case IS the UI and no bet
  is placed; game logic still comes only from the response.
- **Wallet**: `set_balance` / `deposit` (Playsite). **BO**: paytable ops via `framework.helpers.bo.BoHelper`.
- **FS helpers**: `is_freespin_trigger(bet)`, `play_out_freespins(ctx, trigger, settle=)` — used by
  fs-complete, freespin-cascade, spin-then-history, auto-play, reels-glow, reconnect-brief (drain on trigger).

---

## 6. Scenario REGISTRY (30) + suites

**base**: base-spin, base-cluster-win (now finds a ≥2-cascade win THEN plays one more spin → exposes
marks `win`+`reset`), boot-base, change-bet, browse-denominations, spin-then-history (plays out a bonus
before opening History).
**free spins**: fs-complete, freespin-cascade (guarantees a cascade WIN inside FS followed by another FS →
marks `fs_cascade`+`fs_reset`), freespin-zero-balance (trigger FS → zero wallet → reload → drain; needs PLAYSITE_*+BO).
**auto**: auto-play (drains a bonus mid-auto), auto-stop-at-win, auto-user-stop, auto-reload-cancels,
auto-change-bet-first, auto-until-empty (funds a deterministic drain via whole-unit wallet math).
**balance/UI**: insufficient-balance (wallet-first whole-unit withdraw below min bet; else raise the stake
above the balance; skips if already-insufficient), open-tutorial (opens the Info guide and wheels it
top→bottom a screenful at a time — the tutorial is a cross-origin WebView, so only a real
`page.mouse.wheel` reaches it and its scroll position can't be read back; `ctx.scroll_tutorial`).
**input**: spin-inputs (035–040, marked), turbo-modes (021–023).
**resilience**: reconnect-brief (027/029/033; plays a round after reconnect + `stream:{facts.played_after_reconnect}`),
reconnect-long (028), reconnect-during-auto (031), multi-tab-session (030).
**reels-glow** (041; forced FreeSpin! layout, drains the FS it triggers).
**language** (052-056): language-tour (walks base screen / bet list / settings / tutorial / autoplay /
history in one clip), language-switch (tour, relaunch in a 2nd language, tour again -- the leftover
old-language string is only visible by comparing the two), language-sweep (all 16 languages, one
clip). The language lives in the Playsite LAUNCH TOKEN, not the client: a run declares
`language: <code>` and the runner mints a URL via `url_script` (`PLAYSITE_LANGUAGE`) and re-enters
before the scenario; `ctx.set_language()` does the same mid-run. Verdicts are necessarily video-only
-- "the text is Thai" has no server or bridge equivalent -- so the `stream:` asserts pin the language
actually loaded and the surface count, stopping a tour that opened nothing from passing as
"all translated". Language is STICKY across runs.
**popups**: toggle-turbo (105), toggle-auto (107), stacked-popups (108) — no Ante scenario, this build
has none (106) — the notification is a `video:` claim, but the feature's own state
(`getSpeedMode` / `isAutoPlay`) is a fact with a hard `stream:` assert, so a toggle that silently no-ops
goes RED, not orange. `stacked-popups` PROBES which feature buttons exist (`ctx.has_button`, an instant
`getButtonRect`) and bursts only on those: waiting on an absent button would inject multi-second gaps
into the very back-to-back burst the case is about. Its prompt names no fixed sequence — each tap is
marked `TOGGLE: <button>`, so the real ACTION TIMELINE tells the AI what happened and in what order.

Suites: `mah-001-020.yml`, `mah-basegame-freespin.yml`, `mah-autoplay.yml`, `mah-tutorial.yml`,
`mah-spin-turbo.yml`, `mah-reconnect.yml`, `mah-freespin-balance.yml`, `mah-reels-glow.yml`,
`mah-popups.yml`, `mah-language.yml`.

---

## 7. BO paytable forcing (setup/teardown)

- **`derive_paytable.py`** — two modes on a base paytable (standard `<section>.ReelDistributions[].Reels[].Symbols[{Name,Weight}]`):
  - EXCLUSIVE (default): force a symbol to 100% of target reels (others → 0). Deterministic layout.
  - `--probability p`: set the symbol to fraction `p` of each target reel (keep others). Frequent, not forced.
- **Fixtures** (`games/mystic/paytables/`, symbol = `FreeSpin!`, section `BaseGame`):
  `mystic-glow-2col.json` (FreeSpin! forced into reels 0,1 → Reels Glow); `mystic-fs-easy.json`
  (FreeSpin! ~30% on reels 0,1,2 → FS triggers often, game still plays — used by `freespin-zero-balance`).
- **Runner wiring**: a run with `paytable:` → `bo_setup` (`ensure/select/update` via `BoHelper`, IDs from
  `games/mystic/env.config.dev.yml` `bo:` block) → **game load** → scenario → `bo_reset`. If BO isn't
  configured the run is **skipped with an error** (never silently un-forced).
- **Before-load timing (lazy load)**: the runner no longer boots the game up-front. Per run it does
  paytable SETUP first, then the FIRST run does the initial `goto + startGame` (loads WITH the paytable);
  later runs reload only when the paytable changed or a previous forced one must clear. So the paytable is
  active before the game reads its config — matches the Zeus `bo_paytable_update` setup step.

### ⚠ BO auth is the current blocker (config, not code)
`.env.dev` (global) supplies `BO_API_BASE` / `BO_TOKEN` / `BO_REFRESH_TOKEN`; the per-game `env.config`
holds only the non-secret IDs. Verified the env-load path, IDs, and no stale `.bo_token` cache. But the
dev BO (`https://dev-boapi.royaledge.io`) rejects the current token on EVERY endpoint and EVERY header
variant with `UNAUTHORIZED_USER: "Unauthorized user found"`, even on an exact browser-request replay —
and the **refresh token is rejected too**. The JWT is a valid, unexpired token for user `thaiadmin`
(`id:"-"`), so it's either issued for a different environment/portal than dev, or the server-side session
was invalidated. **Next step: log into the dev BO fresh and paste new `ctoken`/`rtoken` into `.env.dev`,
then confirm with `python scripts/smoke_bo_paytable.py --game mystic --env dev`.** Until then, `paytable:`
runs (reels-glow, freespin-zero-balance) skip. Everything else about the BO path is verified.

---

## 8. Coverage — 53/108 (see `docs/mahjong-coverage.md`)

Automated: Mah_001, 004–012, 014–020, 043–051, 057/059/060, 021–023, 027–035…040, 030/031/033, 041,
105/107/108 (`mah-popups.yml`) and 052-056 (`mah-language.yml`) — both added after this
session's write-up.
**Mah_106 (Ante Bet popup) is N/A — Mystic has no Ante Bet**, so its precondition can't be met; the
no scenario or run exists for it.
Still open (54): Sound (024–026), Tutorial detail (058/061–063), Replay (064–073),
Promo (074–080), Last Bet (081–087 — **no `lastBet` bridge button**), Game Help (088–096),
Compliance (097–104), loading timing (002/003 manual), BO history (013),
Mah_032 (30–45 min idle — impractical), Mah_034 (slow-network — needs CDP throttling).

---

## 8b. `--env local` — local client, real backend
`games/mystic/env.config.local.yml` uses **`base_url`** instead of `url`: the runner mints a normal
launch token via `url_script` (credentials from `token_env: dev`), takes that URL's QUERY STRING and
hangs it off `base_url`, then adds `browser_params` + `bridge=1`. So the CLIENT is the locally served
build and the SERVER is dev — test local client changes with no deploy. Language runs work here too
(the token is minted in that language, then grafted on). Resolution order is now
`base_url` > `url` (skipped for language runs — its token has a fixed language) > `url_script`,
all through one `resolve_url(game, env, override, lang)`.

## 9. How to run

```powershell
python scripts/run_scenario.py --env dev --suite games/mystic/test-suites/<suite>.yml
python scripts/run_scenario.py --env dev --scenario <name> --url "http://.../index.html?data=..."   # ad-hoc
# flags: --game (mystic) · --only a,b · --no-ai · --headless · --slow-mo · --teardown <name> (ad-hoc)
python scripts/smoke_bo_paytable.py --game mystic --env dev            # BO auth/config read-only check
python scripts/derive_paytable.py --game mystic --base base --symbol "FreeSpin!" --reels 0,1,2 --probability 0.30 --out mystic-fs-easy
```
Output: `test-results/{game}/{env}/scenario-<ts>/` → open **`report.html`**; one shared `session.webm`.
Needs Python 3.13 + Playwright/Chromium + ffmpeg; `GEMINI_API_KEY`/`AI_PROVIDER` in `.env` (Gemini is
**geo-blocked** here → video asserts return orange/null until a supported region/provider); `PLAYSITE_*`
for wallet scenarios; `BO_*` for `paytable:` runs.

---

## 10. Open items / next steps

1. **Unblock BO** — fresh dev `ctoken`/`rtoken` in `.env.dev` (§7); then reels-glow + freespin-zero-balance run.
2. **Live validation — BLOCKED on a bridge-less build.** As of 2026-08-07 the dev game server is back
   up (`http://192.168.1.27:7459/.../sl_77_mystic_wishes/`, HTTP 200) and the launch URL in
   `env.config.dev.yml` is valid — the game boots and its SignalR socket connects to
   `dev-play.royaledge.io`. But **`window.__gameTestBridge` never appears**, with `&bridge=1` present
   on the URL and after polling 30s (no `*[Bb]ridge*` global at all; only `polyfills`/`system` bundles
   load). So the deployed build was compiled without the QA test bridge. Every scenario fails at load
   with `Page.wait_for_function: Timeout 30000ms exceeded`. **Needs a game-side rebuild/redeploy with
   the bridge included**; until then nothing (old or new) can be validated live.
   Then: input/turbo/reconnect/multi-tab/popups are all still LIVE-UNVERIFIED (Cocos may ignore
   synthetic holds → `hold_button(use_bridge=True)` fallback).
3. **AI provider** — Gemini geo-blocked; switch `AI_PROVIDER=claude` (+ANTHROPIC key) or a supported region.
4. **Next batches** (bridge/infra): Last Bet needs a `lastBet` button; Sound needs a volume getter; Replay
   needs replay-open buttons; Promo/Compliance/i18n need BO/locale provisioning; Mah_034 needs CDP throttling.
5. **Fold into `framework/runner/suite_runner.py`** — the prototype is the executable spec (session-1 item 3).
6. **`inconclusive` verdict** still not in the framework's triplicated `_compute_verdict` (session-1 F5).

## 10b. Reusable across games — `CascadeGame` profile
The suite is now split into MECHANISM (`scripts/scenario_engine.py` — scenarios, invariants, drive
primitives; game-agnostic) and DIALECT (`scripts/cascade_game.py` — a `CascadeGame` profile holding
button/popup names, gameState constants, viewport, capabilities and the response field map). The
runner calls `se.set_profile(--game)`; a new cluster-pays cascade slot subclasses `CascadeGame`,
declares only what it names differently, and inherits every scenario and case. Capability flags
(`HAS_ANTE_BET`, `HAS_BUY_BONUS`, `HAS_LAST_BET_PANEL`, ...) make a missing feature report
*precondition not met* (inconclusive) instead of failing. `profile.api_check_js()` is checked at load
and names any missing REQUIRED bridge function up front.
**The contract other games implement: `docs/game-bridge-contract.md`** — 7 required functions,
~11 optional ones each mapped to the cases they buy, the button/popup roles, and the BET-response
rules the engine depends on.

> **Continued in `docs/session-summary-scenario-qa-3.md`** — the engine was split into
> mechanism + per-game profile, a second mechanism (Hold & Spin) was added, and Zeus was
> onboarded. Start there if you are writing test cases for a NEW GAME.

## 11. References
- Session 1: `docs/session-summary-scenario-qa.md` · Coverage: `docs/mahjong-coverage.md`
- Spec: `docs/scenario-assert-authoring.md` · Paytables: `games/mystic/paytables/README.md`
- Bridge contract (all games): `docs/game-bridge-contract.md` · Profiles: `scripts/cascade_game.py`
- Mystic bridge: `games/mystic/bridge-qa-api.md` · Response shape: `games/mystic/bet-response.md` · Memory: `project_response_driven_driving.md`
