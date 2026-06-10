# QA Workflow — From Game Docs to Passing Tests

How a QA engineer actually uses this platform to onboard a game and produce a running test suite.
This is the **practical playbook**; for the internals of the runner see
[framework-runner-guide.md](framework-runner-guide.md), and for the schema the auto-generated
`framework/schema_reference.md`.

The platform is AI-assisted: you supply the game's documentation, reference images, bridge API,
and test ideas, and three Claude **skills** do the heavy lifting:

| Skill | Command | Turns … | … into |
|-------|---------|---------|--------|
| **init-knowledge** | `/init-knowledge --game=<g>` | the raw game docs you dropped in `docs/raw/` | structured `game-knowledge.md` + `paytable.md` + `symbols.yml` |
| **write-test** | `/write-test --game=<g> --suite=<s>` | your test ideas (xlsx/csv/text/`--raw`) + the knowledge | an executable YAML suite + forced-result paytable fixtures |
| **run-test** | `/run-test --game=<g> --suite=<s> --env=<e>` | the YAML suite | a run with green/orange/red verdicts + replays + a failure diagnosis |

> The skills present findings and **wait for your confirmation** at each gate
> (mechanism detection, the test plan, each individual test). They never write knowledge or
> tests behind your back — you stay in the loop.

---

## The end-to-end flow

```
┌─ ONE-TIME per game ───────────────────────────────────────────────────────────────┐
│                                                                                    │
│  1. Drop game docs        →  games/{game}/docs/raw/                                 │
│       • the game doc (PDF/Markdown/Excel describing mechanics)                      │
│       • base paytable     →  paytable.json                                          │
│       • paytable rules    →  paytable.md   (explains the params; refs paytable.json)│
│                                                                                    │
│  2. /init-knowledge       →  games/{game}/docs/final/game-knowledge.md             │
│                              games/{game}/docs/final/paytable.md                    │
│                              games/{game}/docs/assets/symbols.yml                   │
│                                                                                    │
│  3. Add reference images  →  games/{game}/docs/assets/symbols/*.jpg                 │
│       (paths MUST match the file: entries in symbols.yml)   docs/assets/ui/*.jpg    │
│                                                                                    │
│  4. Add the bridge API    →  games/{game}/bridge-api.md   (from the client team)    │
│                                                                                    │
│  5. Configure env         →  .env  (+ .env.{suffix})   root, shared secrets         │
│                              games/{game}/env.config.{suffix}.yml  per-environment  │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
┌─ PER SUITE (repeat) ──────────────────────────────────────────────────────────────┐
│                                                                                    │
│  6. Author test cases     →  xlsx / csv / text file / --raw description            │
│     /write-test           →  games/{game}/test-suites/{suite}.yml  (+ paytables)   │
│                                                                                    │
│  7. /run-test             →  test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/  │
│                              verdicts + replay.html + diagnosis of any failure      │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

Steps 1–5 are done **once** when a game is onboarded (and refreshed when the game changes).
Steps 6–7 repeat for every suite you write.

---

## Prerequisites — set up Python (one-time)

Install the Python dependencies (uses your system Python — no virtualenv activation needed
afterwards):

```bash
python -m pip install -r requirements.txt
```

Install the Playwright browser binary (once):

```bash
python -m playwright install chromium
```

Verify all imports resolve:

```bash
python -c "import playwright, google.genai, google.generativeai, requests, dotenv, yaml, PIL, imagehash, openpyxl, pdfplumber, csv; from playwright.async_api import async_playwright; print('All imports OK')"
```

That's it — no activation step needed for any future run. (`ffmpeg` must also be on your PATH; it
is a system dependency, not a pip package.)

---

## Stage 1 — Gather the game documentation (`docs/raw/`)

Everything the platform knows about a game starts as raw source material you place in:

```
games/{game}/docs/raw/
```

Drop in whatever the game/client team gave you — the platform reads **PDF, Excel (.xlsx),
images (.png/.jpg), and Markdown**. At minimum you want:

| File | What it is | Notes |
|------|-----------|-------|
| The **game doc** | The mechanics write-up (e.g. `Doc Zappin zeus.md`) — symbols, grid, paylines, bonus rules | The primary source `init-knowledge` reads to detect mechanics. |
| The **base paytable** | `paytable.json` — the game's baseline paytable / weight tables exactly as the server uses them | The authoritative payout + weight data. |
| The **paytable rules** | `paytable.md` — a human-written explanation of the paytable **parameters**, and **must reference** `paytable.json` | Explains what each field/weight means and *how the server samples a result from the weights*. This is what later lets the tooling tune weights to force a deterministic outcome. |

> **Why `paytable.md` must reference `paytable.json`:** the `.md` is the *rulebook* (what the
> parameters mean and how outcomes are drawn); the `.json` is the *data*. Tooling that forces a
> specific spin result (see Stage 6) reads the rules from the `.md` and the structure/weights
> from the `.json` together. A `.md` that explains params without pointing at the concrete
> base paytable leaves the generator guessing.

There is nothing to run in this stage — you are just assembling the source corpus. The quality of
everything downstream depends on it (the skills cite exact quotes from these files and refuse to
invent data that isn't here).

---

## Stage 2 — Generate game knowledge (`/init-knowledge`)

```
/init-knowledge --game=zeus
```

This ingests `docs/raw/`, detects **every** documented mechanic (shared ones like hold-and-spin /
free-spin / buy-bonus / auto-spin, plus custom ones), and classifies each as
**CONFIRMED / LIKELY / ABSENT / UNKNOWN** with an exact quote as evidence.

**It stops and shows you a detection report** — mechanisms, symbols, grid/paylines, and how each
mechanic compares to its canonical shared pattern (Matches / Deviates). **You confirm or correct
it.** Only after your response does it write the knowledge artifacts:

| Output | Purpose |
|--------|---------|
| `games/{game}/docs/final/game-knowledge.md` | The structured source of truth: overview, symbols, paylines, mechanics (with per-mechanic Matches/Deviates), limitations, source index. |
| `games/{game}/docs/final/paytable.md` | Symbol payouts, special/bonus pays, paytable rules — extracted from the raw docs (only created if a paytable was documented). |
| `games/{game}/docs/assets/symbols.yml` | The **asset manifest** — every symbol/UI element, each with a `file:` path and a `ref_asset` key (used later in test YAML). |
| `games/{game}/.docs-hash.json` | SHA-256 manifest of the source corpus, so `/update-knowledge` can detect when the raw docs drift. |

Review `game-knowledge.md` and `paytable.md` for accuracy before moving on — they feed test
generation directly, so a wrong value here becomes a wrong test later.

> Re-onboarding after the docs change: use `/update-knowledge` (it diffs against `.docs-hash.json`)
> rather than `/init-knowledge` again.

---

## Stage 3 — Add reference assets (`docs/assets/`)

`init-knowledge` writes the **manifest** (`symbols.yml`), but the actual reference **images** —
the cropped symbol and UI snapshots taken from the game client — are something **you** add. They
live under:

```
games/{game}/docs/assets/symbols/     # symbol crops:   Coin.jpg, Wild.jpg, Pentagon.jpg, …
games/{game}/docs/assets/ui/          # UI crops:       spin-button.jpg, free-spin-counter.jpg, …
```

**The file paths must exactly match the `file:` entries in `symbols.yml`.** That manifest is a
dict keyed by `ref_asset` name; each entry's `file:` is **relative to `docs/assets/`**:

```yaml
# games/zeus/docs/assets/symbols.yml  (excerpt)
symbols:
  coin:                                    # <- this key is the ref_asset used in test YAML
    file: symbols/Coin.jpg                 # <- must exist at docs/assets/symbols/Coin.jpg
    name: "Coin (Hold & Spin)"
    type: bonus
    description: "Bronze-gold coin … landing 3+ triggers Hold and Spin."
ui_elements:
  free_spin_counter:
    file: ui/free-spin-counter.jpg         # <- must exist at docs/assets/ui/free-spin-counter.jpg
    name: "Free Spin Counter"
    type: ui
    description: "Remaining re-spin count shown during the bonus."
```

**Why this matters:** at run time the framework builds a **symbol reference pack** from
`symbols.yml` and prepends those labelled images to *every* AI assertion, so the model has
ground-truth for what each symbol/UI element looks like. The rule is strict but quiet:

> **Only assets whose `file:` actually exists on disk are loaded into the AI reference pack.**
> A manifest entry pointing at a missing image is **silently skipped** — no error, just weaker AI
> assertions. (In the current Zeus manifest, `crow`/`cup`/`ring`/`time` are listed but carry a
> `# image not yet added` note — those won't contribute to AI grading until the jpg is dropped in.)

So: for any symbol/UI element a test will visually assert on, make sure its image is present and
its path matches the manifest. Snip the asset from the game client, save it to the right folder
under `docs/assets/`, and keep the filename identical to the manifest's `file:`.

---

## Stage 4 — Add the bridge API (`games/{game}/bridge-api.md`)

Tests drive and inspect the game through the **bridge** (`window.__gameTestBridge`) — methods like
`spin()`, `getAppState()`, `getBalance()`, `getFreeSpinsRemaining()`, `startGame()`,
`spinUntilBonus()`, the `verifyHoldSpin*` analysers, etc. The client team owns this surface and
documents it. Place their doc at:

```
games/{game}/bridge-api.md
```

`write-test` validates **every** bridge method name in a test against this file (and/or the bridge
surface described in `game-knowledge.md`). If a method isn't documented there, the skill **flags it
to you with a suggested signature** instead of inventing it — that's how phantom methods are kept
out of the suite.

> Keep this in sync with the client build. When the client team adds or renames a bridge method,
> update `bridge-api.md` first, then write tests that use it. If no bridge documentation exists in
> either `bridge-api.md` or `game-knowledge.md`, `write-test` refuses to run.

---

## Stage 5 — Configure the environment

Config is split in two places by **scope**: shared secrets in a root `.env` (+ per-env overlay),
and per-environment game settings in `games/{game}/env.config.{suffix}.yml`. The env name you pass
to `--env` selects the set via a suffix map:

| `--env` | `.env` overlay | game config file |
|---------|----------------|------------------|
| `dev` | `.env.dev` | `env.config.dev.yml` |
| `staging` | `.env.stg` | `env.config.stg.yml` |
| `prod` | `.env.prod` | `env.config.prod.yml` |
| `local` | `.env.local` | `env.config.local.yml` |

### 5a. Root `.env` (+ `.env.{suffix}`) — secrets & AI provider

The base `.env` holds cross-environment secrets; `.env.{suffix}` overlays environment-specific ones
(it overrides the base). Copy the `.env.example` / `.env.{suffix}.example` templates and fill in:

| Variable | Where | Purpose |
|----------|-------|---------|
| `GEMINI_API_KEY` | base `.env` | Gemini key for AI image/video assertions (required when `AI_PROVIDER=gemini`). |
| `AI_PROVIDER` | base `.env` | `gemini` (default) · `claude` (local Claude CLI sub-agent) · `local` (self-hosted endpoint). |
| `GEMINI_IMAGE_MODEL` / `GEMINI_VIDEO_MODEL` | base `.env` | Override AI models (defaults: `gemini-2.5-flash` / `gemini-3-flash-preview`). |
| `GEMINI_VIDEO_RESOLUTION` | base `.env` | `LOW` · `MEDIUM` (default) · `HIGH` — video frame token cost. |
| `LOCAL_AI_ENDPOINT` | base `.env` | HTTP `/analyze` endpoint, used when `AI_PROVIDER=local`. |
| `CLAUDE_AI_FPS` / `CLAUDE_MODEL` / `CLAUDE_CLI_TIMEOUT` | base `.env` | Claude-provider knobs (video sample rate, model, timeout). |
| `PLAYSITE_API_URL` / `PLAYSITE_USER` / `PLAYSITE_PASS` | `.env.{suffix}` | Playsite login used to resolve the game URL dynamically. |
| `BO_API_BASE` | `.env.{suffix}` | Back-office API base for paytable control (absent in prod). |
| `BO_TOKEN` / `BO_REFRESH_TOKEN` | `.env.{suffix}` | BO access / refresh tokens (grab from the BO DevTools session). |

### 5b. `games/{game}/env.config.{suffix}.yml` — per-environment game settings

Self-contained per environment (no shared base, no `environments:` wrapper). For `dev`:

```yaml
# games/zeus/env.config.dev.yml
url: "http://192.168.1.13:7456/.../index.html?data=eyJ..."   # if non-empty, used directly
url_script: "python scripts/get_game_url_playsite.py --env=dev"  # fallback when url: ""
browser_params: "?bridge=1&debug=1"   # appended to the URL (bridge=1 enables event capture)

playsite:                # used to obtain the play link
  game_code: sl_33_zappin_zeus
  currency: TRY          # omit to use the player's default wallet

bo:                      # back-office identifiers for bo_paytable_* steps (deterministic spins)
  game_id: 63
  sas_entity_id: 1
  agent_id: 1
  currency_id: 18
  game_code: SL_33_ZAPPIN_ZEUS
```

**Top-level launch keys:**

| Key | Meaning |
|-----|---------|
| `url` | A ready-to-use launch URL. **If non-empty it is used as-is** — the quickest path for a manual run: paste a fresh URL here. |
| `url_script` | Command run to resolve the URL when `url` is empty (Playsite login flow). Its stdout becomes the URL. |
| `browser_params` | Query string appended to the URL. **`?bridge=1` is what enables the test bridge + event capture**; `&debug=1` adds debug logging. |

**`playsite:` — used to obtain the play link** (injected as `PLAYSITE_*` for `url_script`):

| Key | Meaning |
|-----|---------|
| `game_code` | The game code of the game you want to test — identifies which game the Playsite login launches. |
| `currency` | The currency you want to test that game in (e.g. `TRY`). Omit to use the player's default wallet. |

**`bo:` — back-office identifiers** for the `bo_paytable_*` steps that force deterministic server
results (only present where deterministic spins are used — dev/stg/local, not prod):

| Key | Meaning |
|-----|---------|
| `game_id` | The game's ID in the back-office. |
| `sas_entity_id` | SAS entity — **fixed at `1`**. |
| `agent_id` | The ID of the agent you run the game on. |
| `currency_id` | The currency you run the game on (the BO currency ID). |
| `game_code` | The game's code in the back-office (e.g. `SL_33_ZAPPIN_ZEUS`). |

> **Quick start:** for a manual run, the fastest setup is to paste a working launch URL into
> `url:` in `env.config.dev.yml` and put your `GEMINI_API_KEY` in `.env`. The Playsite/BO config
> is only needed for dynamic URL resolution and server-side deterministic paytables.

---

## Stage 6 — Author test cases & generate the suite (`/write-test`)

You don't hand-write YAML. You express test **intent** and `write-test` converts it into a valid
suite through an approval loop. Provide intent in whichever form suits you:

| Input form | How |
|------------|-----|
| **Spreadsheet** (`.xlsx`) | Keep cases in a sheet (the repo's `test-case.xlsx` is the pattern). Tell the skill which file, or paste the rows. |
| **CSV / text file** | A file of one-test-per-row / per-paragraph descriptions. |
| **Inline text** | Pass the description directly with `--raw="…"`. |
| **From knowledge only** | Omit `--raw`; the skill generates coverage from `game-knowledge.md` + the shared-mechanism patterns, optionally narrowed with `--scope`. |

```
/write-test --game=zeus --suite=suite-02-freespin --scope="free spin"
/write-test --game=zeus --suite=suite-02-freespin --raw="Trigger free spins with 3 coins; verify counter shows 3 then counts to 0"
```

**What the skill does (and where it pauses for you):**

1. **Loads knowledge** — `game-knowledge.md`, `paytable.md`, `symbols.yml`, `bridge-api.md`, and
   `framework/schema_reference.md` (the authoritative action/schema reference).
2. **Plans the suite** — writes a `{suite}.plan.json`, groups tests by the paytable/forced outcome
   they need, and **shows you the whole plan to approve** (set, grouping, paytables).
3. **Per-test approval loop** — presents **one test at a time** (its steps, bridge + AI assertions,
   and paytable choice) and waits for `yes` / `change` / `skip` before writing it. This gate is
   mandatory — nothing is written until each test is individually confirmed.
4. **Writes the suite** → `games/{game}/test-suites/{suite}.yml`.
5. **Generates forced-result paytables** → for each scenario that needs a deterministic server
   spin, a sub-agent reads the **base paytable** and the **paytable rules** and tunes the weights to
   force the intended outcome, writing `games/{game}/paytables/{ref}.json`. Anything it can't derive
   from the documented rules is flagged `# PAYTABLE UNRESOLVED` rather than guessed.
6. **Coverage report** — what was written, skipped, which mechanics/bridge methods/assets are
   covered, and any ambiguities flagged.

**What a generated test contains (so you can read the YAML):** every test pairs a **bridge
assertion** (deterministic state check) with an **AI visual assertion** (what the player sees) —
e.g. the bridge confirms `getFreeSpinsRemaining == 3` while a Gemini video assertion confirms three
coins are locked on the grid. Determinism comes from either **bridge injection** (fast, client-side)
or **back-office paytables** (real server result path); the skill chooses per test and groups
paytable switches so they happen once per group, not once per test.

> If a test needs a bridge method that isn't in `bridge-api.md`, or references an asset key missing
> from `symbols.yml`, the skill flags it during the loop — fix the source (Stage 3 / Stage 4)
> rather than forcing the test through.

---

## Stage 7 — Run the suite (`/run-test`)

```
/run-test --game=zeus --suite=suite-02-freespin --env=dev
```

This is equivalent to running the runner directly (the skill adds result presentation on top):

```bash
python framework/runner/suite_runner.py --game zeus --suite suite-02-freespin --env dev
# one test (its group setup/teardown still runs, so its paytable is correct):
python framework/runner/suite_runner.py --game zeus --suite suite-02-freespin --env dev --test ZZ-30
# one group:
python framework/runner/suite_runner.py --game zeus --suite suite-02-freespin --env dev --group freespin
```

The runner launches a headless browser, records video, plays each test, then processes the AI
assertions and writes everything to:

```
test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/
```

**Verdicts** combine the two validators (bridge + AI):

| Verdict | Meaning |
|---------|---------|
| 🟢 **green** | bridge passed **and** AI passed (or no AI assertion) — fully passed |
| 🟠 **orange** | the two disagree — one passed, one failed → **human review** |
| 🔴 **red** | both failed (or a timeout/error) |
| ⏸ **pending** | video AI hadn't finished (re-run to resolve) |
| **skip** | no assertions in the test |

`run-test` prints a dashboard (per-test rows, counts, Gemini cost), and for each failure points you
to the artifacts: `result.json`, the AI response `step-N-ai.json`, screenshots `step-N.jpg`,
`events.jsonl` / `timeline.json`, and an interactive **`replay.html`** (video synced with the event
stream). On red/orange it offers an **evidence-based investigation** — reading those artifacts and
`game-knowledge.md` to tell you whether the cause is a wrong expectation, a fragile step, a
mis-scoped AI prompt, infra (token/bridge), or a possible game bug.

> `run-test` is **read-only** — it never edits your suite. To change a test, go back to
> `/write-test` (e.g. `--raw="<the fix>"`). An 🟠 orange is the signal to look at the replay: the
> bridge and the AI saw different things.

---

## Where everything lives

```
qa-platform/
├── .env, .env.dev, .env.stg, …          # secrets + AI provider (Stage 5a)
├── framework/                            # the runner + helpers (don't edit to write tests)
│   └── schema_reference.md / .yml        # authoritative action & schema reference
├── shared-mechanisms/                    # canonical patterns: hold-and-spin, free-spin, …
├── scripts/get_game_url_playsite.py      # URL resolver used by url_script
├── games/{game}/
│   ├── docs/raw/                         # 1. source docs: game doc + paytable.json + paytable.md
│   ├── docs/final/                       # 2. init-knowledge output: game-knowledge.md, paytable.md
│   ├── docs/assets/
│   │   ├── symbols.yml                   #    asset manifest (ref_asset keys + file: paths)
│   │   ├── symbols/*.jpg                 # 3. symbol reference images (match symbols.yml)
│   │   └── ui/*.jpg                      #    UI reference images
│   ├── bridge-api.md                     # 4. bridge method surface (from client team)
│   ├── env.config.{suffix}.yml           # 5. per-env game config (url, playsite, bo)
│   ├── paytables/                        # 6. forced-result fixtures generated by write-test
│   │   ├── base.json, paytable.md        #    base data + rules used to derive fixtures
│   │   └── {ref}.json
│   └── test-suites/
│       ├── {suite}.plan.json             #    write-test plan artifact
│       └── {suite}.yml                   # 6. the executable suite
└── test-results/{game}/{env}/{suite}-{DD-MM-YYYY-HHMM}/   # 7. run output + replay.html
```

## Command cheat sheet

```bash
# Onboard a game (after dropping docs into games/{game}/docs/raw/)
/init-knowledge --game=zeus

# (then add docs/assets images, bridge-api.md, and env config by hand)

# Author a suite from an idea
/write-test --game=zeus --suite=suite-02-freespin --scope="free spin"
/write-test --game=zeus --suite=suite-02-freespin --raw="<your test description>"

# Run it
/run-test  --game=zeus --suite=suite-02-freespin --env=dev
/run-test  --game=zeus --suite=suite-02-freespin --env=dev --test=ZZ-30   # one test/case
/run-test  --game=zeus --suite=suite-02-freespin --env=dev --group=freespin

# Refresh knowledge after the game docs change
/update-knowledge --game=zeus
```

---

*Related docs: [framework-runner-guide.md](framework-runner-guide.md) (runner internals),
`framework/schema_reference.md` (suite schema), [bo-integration.md](bo-integration.md)
(back-office paytable API).*
