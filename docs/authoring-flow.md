# Readable test authoring (`.flow.yml`)

Write test cases the way a QA tester thinks about the game — *wait for load, click
start, spin, wait for the result, check the balance* — instead of low-level
`action:`/`params:` blocks. A QA author writes a **`{suite}.flow.yml`** of one-line
verbs; the compiler expands them into today's runnable **`{suite}.yml`** (validated
by the same `schema_validator`, run by the same runner). The bridge is still the
deterministic engine underneath — verbs compile to bridge calls, **not** to the AI
vision agent.

```
games/zeus/test-suites/sample.flow.yml   <- you write this (verbs)
        │  python -m framework.authoring.compile --game zeus --suite sample
        ▼
games/zeus/test-suites/sample.yml        <- GENERATED, runnable, schema-valid
```

> The compiled `.yml` carries a `GENERATED - DO NOT EDIT` header. Edit the
> `.flow.yml` and recompile; never hand-edit the `.yml` (CI can catch drift with
> `--check`).

See a full worked example: [games/zeus/test-suites/sample.flow.yml](../games/zeus/test-suites/sample.flow.yml)
→ [sample.yml](../games/zeus/test-suites/sample.yml).

---

## The readable file

Top-level `suite` / `game` / `version` / `config` are copied through unchanged.
`setup` / `teardown` / a test's `steps` are **lists of verb strings**:

```yaml
suite: "My suite"
game: zeus
version: "1.0"
config: { test_timeout_ms: 120000, suite_timeout_ms: 1200000 }

setup:
  - start the game            # one verb -> navigate + wait ready + startGame + wait idle

tests:
  - id: ZZ-05
    title: "Balance Deduction on Bet"
    setup:
      - force grid "no-win"
    steps:
      - remember balance as before
      - take a screenshot as before_shot
      - spin and wait for result
      - expect last payout is 0
      - take a screenshot as after_shot
      - check that the balance decreased between before_shot and after_shot
    teardown:
      - reset forced grid
```

### Variables (any word you choose)

`take a screenshot as <name>` and `remember <thing> as <name>` capture a variable
under **any word you pick**. Reference it later:

- in an image check, by name: `check that the bet increased between before and after`
- in a before/after assert: `expect balance decreased from before`

Names are per test/run (they live as long as the runner's `vars` do). Re-using a
name in the same test is an error.

### `@case` — per-case verdicts in a run

A `run:` is one execution that produces several case verdicts. Tag a verb's
assertion with the case(s) it proves using a trailing `@case`:

```yaml
runs:
  - id: fs-3coin-run
    cases:
      - { id: ZZ-30, title: "Trigger free spin" }
      - { id: ZZ-35, title: "Returns to base game" }
    steps:
      - spin through the whole bonus
      - expect free spins remaining is 0   @case ZZ-30
      - take a screenshot as endshot
      - check that endshot shows the bonus has ended   @case ZZ-35
```

`@case A, B` tags two cases. `@case` only goes on a verb that makes an assertion,
and the id must be one of the run's `cases`.

### `raw:` — escape hatch

Anything not (yet) a verb — an **AI-video** assert (bespoke prompt /
`expected_by_case`) or a **back-office paytable** op — is embedded verbatim as a
today-style step under `raw:`. Inside a `raw:` block you may reference a verb's
output by `@name` (a name you captured, or attached with a trailing `as <name>`):

```yaml
    steps:
      - spin through the whole bonus as bonus_spin
      - take a screenshot as endshot
      - raw:
          action: assert
          assert:
            ai:
              mode: video
              video_trim: { from_step: "@bonus_spin", to_step: "@endshot" }
              prompt: "Did the coins stay locked through the re-spins?"
              output_schema: { coins_stay_locked: { type: boolean } }
              expected_by_case: { ZZ-32: { coins_stay_locked: true } }
```

`groups:` and `runs:` are first-class: their `setup`/`steps`/`teardown` are verb
lists; only the `cases:` list stays structured.

---

## Verb catalog (core)

The authoritative source is [framework/authoring/vocab/core.yml](../framework/authoring/vocab/core.yml).
Highlights (the hard-won rules are baked into the expansions):

| Verb | Compiles to |
|---|---|
| `start the game` | navigate → wait `ready` → `startGame` → wait `idle` |
| `reload the game` | navigate → wait `ready` → `startGame` (popup-close in teardown) |
| `spin and wait for result` | `spin` → wait `idle` |
| `wait until idle` / `wait for state <s>` / `wait <n> ms` | `wait_for_state` / `wait_ms` |
| `increase bet` / `decrease bet` | `nextBet` / `previousBet` (never `selectBet`) |
| `enable turbo` / `disable turbo` | `setTurbo` |
| `open the auto popup` | `openAutoPopup` |
| `start <n> auto spins[, stop on any win]` | `startAutoSpin` (needs a prior open-auto-popup) |
| `stop auto spin` | `stopAutoSpin` |
| `take a screenshot as <name>` | `capture` (aliased to `<name>`) |
| `remember balance\|bet index\|last payout as <name>` | bridge getter + `save: <name>` |
| `expect bet index\|last payout\|free spins remaining is <n>` | `bridge_assert` getter `== n` |
| `expect game state is <state>` | `bridge_assert getAppState == <state>` |
| `expect the spin button is enabled\|disabled`, `expect turbo is on\|off`, … | `bridge_assert` boolean getter |
| `expect balance decreased\|increased from <name>` | getBalance + a `script` comparison |

### Per-game verbs

Each game adds its own verbs in `games/<game>/test-suites/vocab.yml` — forced
result grids, the long bonus spin, and asset-aware AI image checks. A per-game
pattern identical to a core one **overrides** it. See
[games/zeus/test-suites/vocab.yml](../games/zeus/test-suites/vocab.yml):
`force grid "no-win"`, `force grid "3 coins top row"`, `reset forced grid`,
`spin through the whole bonus`, `check that the bet increased between <a> and <b>`, …

---

## CLI

```bash
# compile games/zeus/test-suites/test.flow.yml -> test.yml
python -m framework.authoring.compile --game zeus --suite test

# verify the .yml is up to date with its source (CI guard) — exit 1 on drift, no write
python -m framework.authoring.compile --game zeus --suite test --check
```

Then run the compiled suite the usual way:
```bash
python framework/runner/suite_runner.py --game zeus --suite test --env dev
```

If a verb line matches nothing, the compiler fails loudly with the offending line
and the nearest patterns — it never silently drops or guesses. If the compiled
suite would fail schema validation, nothing is written.

---

## Adding a verb

Add an entry to the core or per-game `vocab.yml`:

```yaml
verbs:
  - pattern: "expect bet index is {n:int}"
    expands:
      - { action: bridge_assert, params: { method: getBetIndex, expected: "{n}" } }
```

Slot types: `int`, `ident` (a new variable name — any word), `ref` (an existing
name), `str` (a `"quoted"` literal), `bool`. In `expands`, `{name}` substitutes the
captured value (typed when it is the whole value), and `@{name}` resolves a captured
name to its emitted step id (for `ref_screenshot`, `video_trim`, …). Set
`capture_alias: "{slot}"` on a verb whose emitted step should be aliased under the
author's word.

Matching is whitespace-normalized and case-insensitive on literal words; the
most-literal pattern wins, and an equal-specificity tie is reported as ambiguous.
