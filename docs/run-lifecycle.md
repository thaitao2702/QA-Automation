# The life of a test run

What actually happens when you run a suite, in order, with the reasons behind the ordering. Read
this when a run fails somewhere that is not your scenario, when you want to know what the framework
does for you versus what your scenario must do itself, or before changing `run_scenario.py`.

```bash
python scripts/run_scenario.py --game mystic --env dev --suite games/mystic/test-suites/mah-001-020.yml
```

Related: [scenario-authoring-rules.md](scenario-authoring-rules.md) (writing the scenario code) ·
[scenario-assert-authoring.md](scenario-assert-authoring.md) (writing the cases) ·
[game-bridge-contract.md](game-bridge-contract.md) (what the bridge must provide).

---

## Once, before any run

| | |
|---|---|
| Load the suite | `runs:` list, plus suite-level `teardown:` and `assert_base:` |
| Launch | **one** browser, **one** page, **one** video recording for the entire suite |
| Resolve the URL | pasted `url`, or minted via `url_script`; `bridge=1` is force-appended |

Everything below reuses that page. That is why "clean" cannot be assumed at a run boundary — the
previous run's state is still on screen.

---

## Per run

```
1. RESOLVE    scenario from REGISTRY; apply params:; build a FRESH DriveContext

2. SETUP      language: → mint a launch URL      ─┐ no page interaction:
              paytable: → bo_setup()              ┘ both are LOAD-TIME settings

3. PREPARE    needs a load?  → goto + bridge check + start_game()
              otherwise      → ensure_base(ctx, prev_last_bet)

4. RUN        result = scenario.run(ctx)
              → events.jsonl + timeline.json, stream asserts graded now,
                video asserts queued for the AI pass

5. RESET      bo_reset() if this run forced a paytable

6. TEARDOWN   the suite's declared extras (restore-balance, …)

7. CLEANUP    ensure_base(ctx, None)

8. HANDOFF    prev_last_bet = ctx.last_bet  →  the next run's PREPARE
```

Steps 4-8 run **whether the scenario passed, returned unreached, or raised**. The `except` around
`scenario.run()` records the error and falls through rather than skipping ahead.

### 2. Setup happens before the page opens

`language:` and `paytable:` are both **load-time** settings — the language is baked into the launch
token, and the client reads the paytable when it boots. Neither can be applied to an already-open
game. So the language step only *mints* a URL and `bo_setup` only talks to the back office; nothing
navigates yet.

That is what lets step 3 open the game **once** with both already in force. Applying them after
opening would mean a load followed by a reload.

### 3. Prepare — one load covers every reason to need one

```python
needs_load = (not game_loaded) or needs_lang or has_pt or prev_pt
```

First run, a language change, a paytable to pick up, or a previous forced paytable to clear — any of
them, one load. Otherwise the page stays as it is and `ensure_base` establishes the base in place.

**Both paths play out an owed bonus**, and they differ only in the evidence:

| path | how it knows a bonus is open |
|---|---|
| loaded | `getGameInfo().pending` — the bridge's captured launch payload |
| not loaded | the previous run's last BET response (`prev_last_bet`) |

There is no live "am I in a bonus?" signal mid-suite: `pending` is the load-time capture, and
`isFreeSpinMode` is diagnostic-only by the contract. So the framework acts on a **response**, never
on a guess — `awaitFreespinRound` is never called speculatively.

### 4. Run — what the framework does NOT do for you

The run-boundary machinery handles **leftovers between runs**. A spin that triggers free spins
**inside** your scenario is still your scenario's job — see
[scenario-authoring-rules.md](scenario-authoring-rules.md) Rule 1. No amount of prepare/cleanup
catches that, because the hang happens on the next spin of the same run.

### 6 before 7 — teardown, then check

`restore-balance` puts the wallet back where the run found it and reloads the page. Running it
first means the game is already
clean by the time CLEANUP looks. The other order paid a full `waitForSpinClickable` timeout to
discover a state the teardown was about to fix one line later.

### 7. Cleanup — repair always, assert on request

| | when | cost when there is nothing to do |
|---|---|---|
| **drain an unfinished bonus** | always | **zero** — evidence-driven; no response saying "bonus open" means no bridge call |
| **assert a clean, spinnable base** | only `assert_base: true` | a real `waitForSpinClickable` |
| **reload + `startGame()`** | only if either of the above failed | a page load |

Draining is **repair, not an assertion**: the damage it prevents is silent and lands on a *later*
run, as unconsumed free-spin rounds arriving in the next scenario's stream. So it is not optional.

Asserting is opt-in because a scenario may legitimately end unable to satisfy it —
`insufficient-balance` finishes with the out-of-money popup open, which blocks the spin button.

```yaml
assert_base: true          # suite-level default
runs:
  - run:
      scenario: base-spin
      assert_base: false   # per-run override
```

> **Known gap with `assert_base` off (the default):** if a run ends mid-bonus *without that response
> being recorded* — it raised before `_record` — then `prev_last_bet` looks terminal, nothing is
> drained, and nothing notices. `assert_base: true` is what closes it.

### 8. Handoff

Each run builds a fresh `DriveContext`, so `last_bet` starts empty. Passing this run's last response
forward is the only reason the next PREPARE can tell that a bonus was left open.

---

## After all runs

| | |
|---|---|
| Suite teardown | once, on the last run's context — skipped if a run-level teardown aborted |
| Video | one `session.webm` at the run root; every scenario references sub-clips of it, no per-run copies |
| **AI pass** | now, not during the runs: trim each assert's sub-clip, analyse, grade — `pending` becomes green or orange |
| Report | per-case verdicts, HTML + JSON summary |

The AI pass is deferred so the browser session is not held open while models are called, and so one
clip can be analysed **once** and graded against several cases (`expected_by_case`) instead of once
per case. `--no-ai` skips it and leaves video asserts pending.

---

## Failure severity by stage

Not every failure is equally bad, and the runner reflects that:

| Stage fails | Consequence |
|---|---|
| unknown scenario / bad `params:` | skip this run |
| language or paytable setup | skip this run (paytable reset first) |
| PREPARE — load, or `ensure_base` | skip this run |
| `scenario.run()` | run recorded as **errored**, steps 5-8 still run, suite continues |
| `bo_reset` | logged only — best-effort, but the forced paytable stays active and `prev_pt` forces a reload next run |
| declared teardown | **abort the suite** |
| CLEANUP | **abort the suite** — and only after the reload fallback also failed, so the page is unrecoverable |
| bridge missing a REQUIRED function | **stop before any run** — reported by name |

The principle: one scenario that cannot get a clean start is not a reason to stop, but a page that
cannot be recovered is — every later run would be grading a broken session.
