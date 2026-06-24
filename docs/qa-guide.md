# QA Guide — Understand the Platform, Then Use It

**Read this first.** It explains *what every piece is* and *how the whole thing fits together*, in
plain language, before you touch a single file. Once the concepts click, the day-to-day commands are
easy.

- New here and want the ideas? → read top to bottom.
- Just need the commands and exact field names? → see the dense playbook
  [qa-workflow.md](qa-workflow.md) and the auto-generated `framework/schema_reference.md`.

---

## Table of contents

1. [The big idea (in one minute)](#1-the-big-idea-in-one-minute)
2. [Core concepts — the vocabulary](#2-core-concepts--the-vocabulary)
3. [The heart of it: two referees (bridge + AI)](#3-the-heart-of-it-two-referees-bridge--ai)
4. [The test file, built up piece by piece](#4-the-test-file-built-up-piece-by-piece)
5. [Group vs Test vs Run — when to use which](#5-group-vs-test-vs-run--when-to-use-which)
6. [Forcing a result (why tests aren't random)](#6-forcing-a-result-why-tests-arent-random)
7. [The full flow, stage by stage](#7-the-full-flow-stage-by-stage)
8. [Reading the results](#8-reading-the-results)
9. [Glossary & cheat sheet](#9-glossary--cheat-sheet)

---

## 1. The big idea (in one minute)

This platform is a robot QA tester for slot games. You give it:

- **The game's documentation** (how the game works), and
- **Your test ideas** (what you want to check).

It then **opens the real game in a browser, plays it, records a video, and checks the results** — and
it checks them **two ways at once**: by asking the game's own code for the facts, *and* by looking at
the screen with an AI that judges what a player would see. When both agree, you trust the result.
When they disagree, it flags it for a human.

Three assistants (we call them **skills**) do the heavy lifting, and **each one pauses for your
approval** before writing anything:

| Skill | What you say | What you get back |
|-------|--------------|-------------------|
| `/init-knowledge` | "Here are the game docs." | A clean, structured **knowledge file** about the game + an **asset list**. |
| `/write-test` | "Test these scenarios." | A ready-to-run **test suite** (a YAML file). |
| `/run-test` | "Run this suite." | **Verdicts** (pass/fail), a **replay video**, and a **diagnosis** of any failure. |

That's the whole platform: **understand the game → write tests → run tests → read results.**

---

## 2. Core concepts — the vocabulary

These are the words you'll see everywhere. Learn these and the rest is detail.

### The building blocks of a test file

| Term | Plain meaning | Analogy |
|------|---------------|---------|
| **Suite** | One file containing all the tests for one game. | A test *plan* document. |
| **Test** | One scenario you want to check. Gets **one verdict**. | One line item: "bet goes up when I press +". |
| **Step** | One thing inside a test — either *do something* or *check something*. | One instruction in a recipe. |
| **Action** | A step that **does** something (spin, click, navigate, wait). | "Stir the pot." |
| **Assert** (a "check") | A step that **verifies** something is true. | "Taste it — is it salty enough?" |
| **Setup** | Steps that run **before** to prepare. | "Preheat the oven." |
| **Teardown** | Steps that run **after** to clean up. | "Wash the dishes." |

### The grouping concepts

| Term | Plain meaning |
|------|---------------|
| **Group** | A bundle of tests that share an **expensive preparation** (e.g. the same forced odds). The group's setup/teardown runs **once for the whole bundle**, not once per test. |
| **Run** | **One play-through that answers several questions at once.** Instead of playing a bonus round five times to check five things, you play it **once** and read off five verdicts. |
| **Case** | One question (one verdict) **inside a run**. A run has many cases. |

> **Test vs Run in one line:** a **Test** = one play → one verdict. A **Run** = one play → many
> verdicts (one per case). More on this in [section 5](#5-group-vs-test-vs-run--when-to-use-which).

### The "how do we know it's correct?" concepts

| Term | Plain meaning |
|------|---------------|
| **The bridge** | The game's built-in **test remote-control** (`window.__gameTestBridge`). It lets tests press buttons and **ask the game's code for facts** ("how many free spins are left?"). |
| **Bridge assert** | A check that asks the bridge for an exact fact. **Deterministic** — the same input always gives the same answer. |
| **AI assert** | A check where an AI **looks at the screen (image) or the recorded clip (video)** and judges what a player would see. Good for visuals the raw numbers can't fully prove. |
| **Reference assets** | Labelled screenshots of each symbol/button you give the AI so it knows what a "Coin" or the "Spin button" *looks* like. |
| **Verdict** | The final result of a test/case after combining both checks: 🟢 green / 🟠 orange / 🔴 red / ⏸ pending / skip. |

### The "make it repeatable" concepts

| Term | Plain meaning |
|------|---------------|
| **Forcing a result** | Real slots are random. To test "what happens on a 3-coin trigger," you must **force** that outcome so the test is repeatable. (Two methods — see [section 6](#6-forcing-a-result-why-tests-arent-random).) |
| **Variable (`save` / `{{name}}`)** | Capture a value during the test (e.g. the balance) and **reuse it later** in a check. |

---

## 3. The heart of it: two referees (bridge + AI)

This is the single most important idea in the platform, so it gets its own section.

Every meaningful test is judged by **two independent referees**:

- **Referee 1 — the bridge (the game's brain).** Asks the code directly. Exact and instant.
  *"`getFreeSpinsRemaining` → 3."* It can't be fooled, but it only knows numbers — it can't tell you
  the coins *look* locked on screen.
- **Referee 2 — the AI (a player's eyes).** Watches the screen or the video clip and judges like a
  human. *"Yes, three coins are glowing and stay put through the re-spins."* Great for visuals, but
  judgement isn't perfectly exact.

The platform combines their two opinions into one **verdict**:

| Verdict | What happened | What to do |
|---------|---------------|------------|
| 🟢 **green** | Both agree it passed (or there was no AI check). | Trust it. ✅ |
| 🟠 **orange** | **They disagree** — one passed, one failed. | **A human looks at the replay.** This is the valuable signal. 👀 |
| 🔴 **red** | Both failed (or the test errored/timed out). | A real failure — investigate. ❌ |
| ⏸ **pending** | The video AI hadn't finished yet. | Re-run to resolve. |
| **skip** | The test had no checks. | Nothing to judge. |

> **Why two referees?** The bridge proves the *math* is right; the AI proves the *player sees* the
> right thing. A bug can hide in the gap between them — e.g. the code says "win = 10" but the screen
> shows nothing. **Orange is the platform telling you to go look.**

---

## 4. The test file, built up piece by piece

A suite is a `.yml` (YAML) file. Don't worry about memorizing it — `/write-test` and the readable
`.flow.yml` format (see [authoring-flow.md](authoring-flow.md)) generate it for you. But you should be
able to *read* one. We'll build it up from nothing.

### 4.1 The skeleton (every suite has this)

```yaml
suite: "My first suite"     # a human title
game: zeus                  # which game
version: "1.0"
config:
  test_timeout_ms: 120000   # give up on a single test after 2 min
  suite_timeout_ms: 1200000 # give up on the whole suite after 20 min
  video: true               # record video (needed for video AI checks)

tests:
  - ...                     # your tests go here
```

### 4.2 A test = an id, a title, and a list of steps

```yaml
tests:
  - id: ZZ-03                       # unique id (used to run just this test)
    title: "Increase/Decrease Bet"  # what it proves
    steps:
      - id: step-01
        action: bridge_action       # DO: press the "increase bet" button
        params: { name: nextBet }
      - id: step-02
        action: assert              # CHECK: did the bet index become 1?
        assert:
          bridge: { method: getBetIndex, expected: 1 }
```

Read it as: **"Press increase-bet, then check the bet index is now 1."** Every step has an `id`. A
step either **does** something (`action:`) or **checks** something (`assert:`).

### 4.3 Adding the two referees to one check

A single `assert` step can carry **both** a bridge check and an AI check — that's the dual referee in
action:

```yaml
      - id: step-05
        assert:
          bridge:                              # Referee 1: the math
            method: getFreeSpinsRemaining
            expected: 3
          ai:                                  # Referee 2: the eyes
            mode: image
            ref_screenshot: step-04            # which screenshot to look at
            assets: [ ui_elements.free_spin_counter ]   # show the AI what to look for
            prompt: "Does the free-spin counter show 3?"
            output_schema: { shows_three: { type: boolean } }
            expected: { shows_three: true }
```

- **`mode: image`** judges a single screenshot (taken by an earlier `capture` step).
- **`mode: video`** judges a *clip* of the recording between two steps (`video_trim: { from_step,
  to_step }`) — use it for things that happen over time, like "the coins stayed locked through the
  re-spins."
- **`assets:`** (or `asset_groups:`) tells the AI which reference images to look at, so it isn't
  guessing what a "Coin" looks like.

### 4.4 Remembering a value and checking it later

Sometimes the value you want to check is only known at run time (e.g. the payout of a spin). **Save**
it, then assert on it later. A `"$name"` in a check's `args` resolves to the saved value:

```yaml
      - id: step-05
        action: bridge_action
        params: { name: getLastPayout }
        save: payout                       # remember this number as "payout"
      - id: step-06
        action: bridge_assert
        params:
          method: inRange                  # bridge.inRange(<payout>, 0, 100)
          args: [ "$payout", 0, 100 ]      # "$payout" = the saved value
          expected: true
```

You can also drop a saved value into an **AI** prompt with `{{name}}` — e.g.
`prompt: "Is the bet shown as {{bet}}?"` — so the AI confirms a *known* value instead of a fuzzy one.

---

## 5. Group vs Test vs Run — when to use which

These three are the structure of a suite, and mixing them up is the most common confusion. Here's the
mental model.

### Test vs Run at a glance

A **test** and a **run** execute *identically* (setup → steps → teardown, same actions and asserts).
The only difference is **how many verdicts come out**:

| | **Test** | **Run** |
|---|---|---|
| Play-throughs | one | one |
| Verdicts produced | **1** | **many** (one per `case`) |
| Reported as | one line | one line **per case** |

So a run is *not* a different kind of check — it's a way to **harvest several verdicts from one
play-through**. Use it when several checks all ride on the *same expensive execution* (e.g. trigger a
bonus once, then judge six things about it) instead of replaying that execution once per check.

### Test — one play, one verdict

The default. Use it for an independent scenario.

```yaml
tests:
  - id: ZZ-03
    title: "Bet increases"
    steps: [ ... ]
```

### Group — tests that share an expensive setup

Some tests need the same costly preparation first (most often: **forcing the game's odds** to a
particular outcome). Putting them in a **group** means that setup runs **once** for all of them, not
once per test — much faster.

```yaml
groups:
  - id: freespin-tests
    setup:    [ ... force the odds to "free-spin trigger" ... ]   # runs ONCE
    teardown: [ ... restore the odds ... ]                        # runs ONCE
    tests:
      - id: ZZ-30
        title: "Free spins start"
        steps: [ ... ]
      - id: ZZ-31
        title: "Counter counts down"
        steps: [ ... ]
```

> Running a single test with `--test ZZ-30` **still runs its group's setup/teardown** — so the odds
> are correct even when you run just one test.

### Run — one play, MANY verdicts (cases)

This is the clever one. Some scenarios are **expensive to reach** (e.g. you must trigger and play out
a whole bonus round, which takes a minute+). You don't want to do that five times to answer five
questions. A **run** plays it **once** and reads off a verdict for each **case**:

```yaml
runs:
  - id: fs-3coin-run
    title: "Play one bonus, check several things about it"
    cases:                                   # each case = one verdict line in the report
      - { id: ZZ-30, title: "Free spins triggered" }
      - { id: ZZ-32, title: "Coins stayed locked" }
      - { id: ZZ-35, title: "Returned to base game" }
    steps:
      - id: step-01
        action: bridge_action                # plain step (no case tag): just play the bonus
        params: { name: spinUntilBonus }
      - id: step-02
        case: ZZ-30                           # this check proves CASE ZZ-30
        assert: { bridge: { method: getFreeSpinsRemaining, expected: 3 } }
      - id: step-10                           # ONE video AI call → verdicts for several cases
        assert:
          ai:
            mode: video
            video_trim: { from_step: step-01, to_step: step-09 }
            output_schema: { coins_held: {type: boolean}, returned_to_base: {type: boolean} }
            expected_by_case:                 # split the AI's answer across cases
              ZZ-32: { coins_held: true }
              ZZ-35: { returned_to_base: true }
```

Key rules for runs:

- **Tag a check with `case:`** (one id, or a list) to say which case it proves.
- A **plain, untagged step that fails is fatal** — it means the play-through broke, so *every* case
  goes red (we couldn't gather the evidence).
- For an AI check, **`expected_by_case`** splits one model call's answer across multiple cases — one
  call, many verdicts.
- Each case shows up as its **own line** in the report, all sharing the run's one video.
- Each case still gets the **dual referee** (bridge + AI) and its own 🟢/🟠/🔴 verdict — same judging
  as a test, just attributed per case.

| You have… | Use a… |
|-----------|--------|
| An independent scenario | **Test** |
| Several tests needing the same costly prep | **Group** (of tests) |
| Several checks about the *same single expensive play* | **Run** (with cases) |

---

## 6. Forcing a result (why tests aren't random)

Real slots are random — so "test what happens on a winning spin" is impossible unless you **force**
the outcome. The platform forces results two ways; you usually don't choose manually (`/write-test`
picks for you), but you should understand the difference:

| Method | How it works | Best for |
|--------|--------------|----------|
| **Bridge injection** | Tell the game client "make the *next* spin land exactly this." Fast, client-side. | A single forced spin. |
| **Back-office (BO) paytable** | Change the **server's** odds so spins are guaranteed to behave a certain way, then restore it after. Real server path. | Multi-spin sequences (bonuses, Hold & Spin) where the server decides each spin. |

Because the BO method is expensive (it changes server state), tests that need it are bundled into a
**group** so the switch happens once. That's the main reason groups exist.

The deterministic data itself lives in **paytable fixtures** (`games/{game}/paytables/{name}.json`),
which `/write-test` generates from the game's documented paytable rules.

---

## 7. The full flow, stage by stage

```
ONE-TIME per game                          EVERY suite
────────────────────                       ────────────────
1. Drop game docs in docs/raw/      ┐      4. Write tests:
2. /init-knowledge  → knowledge     │           /write-test  OR  a .flow.yml + compile
3. Add asset images, bridge-api.md, │      5. /run-test → verdicts + replay + diagnosis
   and env config                   ┘      (repeat 4–5 per suite)
```

### Stage 1 — Gather the game docs (`docs/raw/`)
Drop everything the game team gave you into `games/{game}/docs/raw/`: the **game design doc**, the
**base paytable** (`paytable.json`), and the **paytable rules** (`paytable.md`). The quality of
everything downstream depends on this corpus — the skills quote it and refuse to invent data.

### Stage 2 — Understand the game (`/init-knowledge`)
```bash
/init-knowledge --game=zeus
```
Reads `docs/raw/`, detects every mechanic, and **shows you a report to confirm**. Only after your OK
does it write:

| Artifact | What it is |
|----------|-----------|
| `docs/final/game-knowledge.md` | The clean, structured source of truth about the game. |
| `docs/final/paytable.md` | Symbol payouts & paytable rules (if documented). |
| `docs/assets/assets.yml` | The **asset manifest** — every symbol/UI element, grouped (`symbols:`, `ui_elements:`, `ui:`), each with a `file:` path. |

> Re-onboarding after the docs change? Use `/update-knowledge` (it detects what drifted).

### Stage 3 — Add the reference images & APIs (by hand)
- **Asset images:** drop the cropped symbol/UI screenshots under `docs/assets/symbols/` and
  `docs/assets/ui/` so their paths match the `file:` entries in `assets.yml`. These teach the AI what
  each symbol looks like. *Only images that exist on disk are used* — a missing one is silently
  skipped (weaker AI checks).
- **Bridge API:** put the client team's `bridge-api.md` at `games/{game}/bridge-api.md`. `/write-test`
  validates every bridge method against it and refuses to invent methods.
- **Environment config:** secrets in `.env` (+ `.env.{env}`), and per-environment game settings
  (the launch URL, playsite login, back-office ids) in `games/{game}/env.config.{env}.yml`. The
  fastest start: paste a working launch URL into `url:` and set your AI provider key.

### Stage 4 — Write the tests
Either describe your intent and let `/write-test` generate the suite, or hand-write a readable
`{suite}.flow.yml` and compile it (`python -m framework.authoring.compile --game {game} --suite
{suite}`; see [authoring-flow.md](authoring-flow.md)). Output:
`games/{game}/test-suites/{suite}.yml` (+ paytable fixtures).

### Stage 5 — Run the tests (`/run-test`)
This launches a headless browser, records video, plays each test, runs both referees, and writes
everything to `test-results/{game}/{env}/{suite}-{timestamp}/`.

Every run needs `--game`, `--suite`, and `--env`. What you add on top selects **how much** to run:

| To run… | Add… | What happens |
|---------|------|--------------|
| **The whole suite** | *(nothing)* | Every test, group, and run executes. |
| **One group** | `--group <group-id>` | All tests + runs in that group, with the group's setup/teardown once. |
| **Several groups** | `--group g1,g2` | Comma-separated list of group ids. |
| **One test** | `--test <test-id>` | Just that test — **but its group's setup/teardown still runs around it**, so its forced odds are correct. |
| **Several tests** | `--test ZZ-03,ZZ-05` | Comma-separated list of test ids. |
| **One whole run** | `--test <run-id>` | The run executes once; **all** its cases are reported. |
| **One case of a run** | `--test <case-id>` | The whole run still executes (cases share one play-through), but **only that case's verdict** is reported. |
| **Tests inside one group** | `--group <g> --test <id>` | The filters combine — narrow to the group, then to the test(s) within it. |

```bash
# whole suite
/run-test --game=zeus --suite=freespin --env=dev

# by group
/run-test --game=zeus --suite=freespin --env=dev --group=fs-trigger3-countdown
/run-test --game=zeus --suite=freespin --env=dev --group=g1,g2          # several groups

# by test
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-03
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-03,ZZ-05     # several tests

# by run / by case (a case id runs the whole run, reports that case)
/run-test --game=zeus --suite=freespin --env=dev --test=fs-3coin-run    # whole run, all cases
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-32           # one case of that run
```

> `/run-test` is the skill (adds result presentation). The bare runner takes the **same flags**:
> `python framework/runner/suite_runner.py --game zeus --suite freespin --env dev [--group … | --test …]`.
> Test, run, and case ids are all **unique across the suite**, so `--test <id>` is never ambiguous.

---

## 8. Reading the results

`/run-test` prints a dashboard (one row per test/case, counts, AI cost). For each test you get an
artifact folder containing:

| File | What it shows |
|------|---------------|
| `result.json` | The verdict and both referees' outcomes. |
| `step-N-ai.json` | The AI's exact answer for an AI check. |
| `step-N.jpg` | A screenshot at that step. |
| `events.jsonl` / `timeline.json` | The raw event stream and step timing. |
| **`replay.html`** | An **interactive replay** — the video synced with the event log. **Start here.** |

When a test is 🟠 **orange** or 🔴 **red**, `/run-test` offers an **evidence-based diagnosis**: it
reads those artifacts plus `game-knowledge.md` and tells you whether the cause is a wrong expectation,
a fragile step, a mis-scoped AI prompt, an infrastructure issue (token/bridge), or a possible **game
bug**.

> `/run-test` is **read-only** — it never edits your suite. To change a test, go back to your
> `.flow.yml` (or `/write-test`). An **orange is your cue to open `replay.html`**: the two referees
> saw different things, and your eyes settle it.

---

## 9. Glossary & cheat sheet

### One-line glossary

- **Suite** — the file of tests for one game.
- **Test** — one scenario → one verdict.
- **Run** — one play-through → many verdicts (cases).
- **Case** — one verdict inside a run.
- **Group** — tests sharing one expensive setup (run once).
- **Step** — one line in a test: an **action** (do) or an **assert** (check).
- **Bridge** — the game's test remote-control; bridge checks are exact/deterministic.
- **AI assert** — a screen/video check judged by AI (`image` or `video`).
- **Reference assets** — labelled images that teach the AI each symbol/button.
- **Forcing** — making a random game produce a fixed outcome so a test is repeatable.
- **Verdict** — 🟢 green / 🟠 orange (disagree → look) / 🔴 red / ⏸ pending / skip.

### Where things live

```
games/{game}/
├── docs/raw/                  # 1. source docs you drop in
├── docs/final/                # 2. /init-knowledge output: game-knowledge.md, paytable.md
├── docs/assets/
│   ├── assets.yml             #    asset manifest (groups: symbols / ui_elements / ui)
│   ├── symbols/*.jpg          # 3. symbol reference images
│   └── ui/*.jpg               #    UI reference images
├── bridge-api.md              # 4. the bridge methods (from the client team)
├── env.config.{env}.yml       # 5. per-env config (launch url, playsite, back-office)
├── paytables/{name}.json      #    forced-result fixtures (from /write-test)
└── test-suites/
    ├── vocab.yml              #    per-game verbs for .flow.yml authoring
    ├── {suite}.flow.yml       #    readable verb source (optional)
    └── {suite}.yml            #    the runnable suite
test-results/{game}/{env}/{suite}-{timestamp}/   # run output + replay.html
```

### Command cheat sheet

```bash
# 1. Understand the game (after dropping docs in docs/raw/)
/init-knowledge --game=zeus
/update-knowledge --game=zeus            # after the docs change

# 2. Write a suite — pick one path
/write-test --game=zeus --suite=freespin --raw="<describe your tests>"
python -m framework.authoring.compile --game zeus --suite sample   # compile a .flow.yml

# 3. Run it
/run-test --game=zeus --suite=freespin --env=dev
/run-test --game=zeus --suite=freespin --env=dev --test=ZZ-30      # one test/case
/run-test --game=zeus --suite=freespin --env=dev --group=freespin-tests
```

---

*Next steps: the dense playbook [qa-workflow.md](qa-workflow.md) (every option, exact setup),
[authoring-flow.md](authoring-flow.md) (the full `.flow.yml` verb catalog),
`framework/schema_reference.md` (every action & field), and
[bo-integration.md](bo-integration.md) (the back-office paytable API).*
