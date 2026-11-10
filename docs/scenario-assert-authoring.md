# Scenario + Assert Authoring (Layer 3)

How QA authors tests in the response-driven framework. QA references a **scenario**
(a reusable, reachable game situation) and writes **asserts** on it. QA never reads a
per-game bridge getter — only the portable scenario vocabulary and the server response
contract.

Three roles, three authors:

| Piece | Author | Scope | Why |
|---|---|---|---|
| **Scenario library** (closed box: `name` + `description` + exposed outputs) | framework eng | per **mechanism** | reach + start/stop/collect timing is imperative code, hidden — QA only sees the description and the outputs |
| **Invariant library** (`stream:` predicates) | senior QA / eng | per **mechanism** | game rules as exact predicates over the response stream, reused across every game on the mechanism |
| **Test cases** (`scenario:` + `assert:`) | QA | per **test** | pick coverage, bind API values, write the screen claims |

Two verifiers, never blended:

- **`stream:`** → checked by the **response engine** (exact, free). A violation is a real bug → **red** (Hard tier).
- **`video:`** → checked by **AI** against the recorded video (perception). A mismatch is "look here" → **orange** (Probabilistic tier), never an auto-red.

---

> **Writing the scenario itself** (the imperative code in `scripts/scenario_engine.py`) is a
> different job with its own rules — most importantly that **any bet response may be a free-spin
> trigger**, and ignoring one hangs the run. See `docs/scenario-authoring-rules.md`.

## 1. Scenario definition — a closed, engineer-owned box

A scenario is a named routine owned by framework engineers. It emulates **one situation** and,
when run, produces the outputs the asserts consume. Its **internals — how it reaches the
situation (YOLO / forced), when to start, when to stop, when to collect, the budget — are
closed.** They are deliberately *not* a declarative surface: expressing arbitrary start/stop/
collect logic in YAML recreates a programming language for exactly the complex cases where that
trap bites. Keep it in code.

The scenario's entire **public contract** is:

| Field | Meaning |
|---|---|
| `name` | the id QA references |
| `description` | the situation it emulates, in plain language — the spec QA reads to know what it does |
| exposed outputs | **`video`** (the recorded window → `video:` asserts) and **`response`** (the captured bet response(s) for the situation → `stream:` asserts and `{res.*}` binding) |

```yaml
# registered by engineers as code + this contract; QA never sees or constrains the internals
name:        fs-win-2-cascade
description: "Enter free spins, then land a cluster win with exactly 2 cascades."
exposes:     [video, response]
```

**Implementation (closed, Python).** Drives the game (real spins or forced), watches the response
stream, decides when the situation is reached and when to stop collecting, records the video
window, and returns `(video, response)`. If it can't reach the situation within its own budget it
returns **unreached** → the case is `inconclusive`, not a fail. QA neither sees nor constrains any
of this.

**What it exposes to asserts.** A simple scenario exposes one `response` (the key round) bound as
`{res.*}`; a whole-bonus scenario exposes an ordered `rounds` list (`{rounds[i].*}`,
`{trigger.*}`, `{terminal.*}`). The `description` states which.

**Video + marks.** The `video` is the full recorded span — bounded at the end by *settle* (the
box waits for the client to finish presenting, via `waitForSpinClickable`, before closing the
window), so the animation is always in frame. Beyond the whole-scenario clip, a scenario may
expose **marks**: named sub-clips its *internal logic* picked out — e.g. `big_cascade` = the
free-spin round that had the most cascades. QA never computes which round that is; the box
exposes it by name, and a `video:` assert targets it with `at:` (see §2). Every round also has an
automatic sub-clip addressable as `at: trigger | terminal | res | rounds[i]`.

> This is the same rule we applied to the navigation loop — imperative logic lives in code, once
> per mechanism — now applied to the scenario's timing. A scenario that needs a more intricate
> situation is a code change inside the box, never new declarative syntax.

**Run `name:` — what a suite calls one run.** A suite's `- run:` block uses a scenario; the same
scenario can appear in several runs (four `last-bet-restore` runs in `gm-lastbet.yml`). Give every
run a `name:` — unique in the suite, lower-case letters, digits, `-`, `_`, `.` (the validator
checks). It names the run's result folder, report section and replay chapter, and it is what
`--only` selects:

```bash
python scripts/run_scenario.py --game grandma --env dev \
  --suite games/grandma/test-suites/gm-lastbet.yml --only lastbet-win
```

`--only` also still takes a scenario name, which picks every run of that scenario, and both can be
mixed comma-separated. A run without a `name:` falls back to its scenario name; a repeat gets
`-2`, `-3`… so two runs never write into one folder.

---

## 2. Writing a `video:` prompt — describe the SCREEN, not the response

A `stream:` assert reads the server response. A `video:` assert reads the screen. **They do not show
the same number at the same time**, and the commonest way to write a broken prompt is to take a
response value and ask the AI to see it.

The traps — each a real failure, with the rules it taught — are in
**[assert-traps.md](assert-traps.md)**:

| Trap | Case | In one line |
|---|---|---|
| Balance | Mah_008 | the stake comes off at spin start, so `res.balance` is passed through, not risen to |
| Counting | Mah_043 | `cascades` counts grids evaluated (one more than visible events) — and never ask an AI to count |
| Inverse pair | ZZ-51 | two fields asking about one property in opposite directions give confident nonsense |
| Per-round window | ZZ-43 | a round's window ends at the next response, so `at:` on a bonus round can be milliseconds long |
| Generally | — | which response fields are ever on screen, and when |

**Read it before writing any assert, and add to it when a run teaches a new one.**

---

## 3. Assert vocabulary

QA writes asserts on a scenario. Each is either `stream:` (response engine) or `video:` (AI).

```yaml
# games/mystic/test-suites/*.yml   (QA-authored)
# A run plays ONE scenario, and every case under it gets its own verdict
# -> one scenario play, many verdicts (see §7 for the pipeline mapping).

- run:
    scenario: base-cluster-win          # closed box, played once
    cases:
      - id: MY-mult-base
        title: "Base multiplier ladder climbs per cascade"
        assert:
          - stream:  multiplier_ladder(base)      # named invariant, library
          - video: "the multiplier indicator counts up each cascade"
      - id: MY-06
        title: "Win amount shown equals payout"
        assert:
          - stream:  conservation                 # balance == prev - stake + payout
          - video: { read: "the win amount label", equals: "{res.payout}" }
```

### `stream:` — response-engine assertion (Hard)

- **Named invariant:** `stream: <name>(args)` runs a per-mechanism library function over the
  scenario's `rounds`/`res` and returns `{valid, violations}`. This is your existing analyser
  pattern (`verifyFreeSpinAward`, `verifyHoldSpin*`) — now plain Python over the captured
  response list, no bridge round-trip. Examples: `conservation`, `multiplier_ladder(base|free)`,
  `freespin_award`, `freespins_monotonic`, `stake_zero_in_freespins`.
- **Inline predicate:** `stream: "<expr over bound fields>"` for simple one-offs, e.g.
  `"{trigger.totalFreeSpin} == 8"`, `"{terminal.freeSpins} == 0"`.
- **Verdict:** any violation → **red**. Exact, deterministic, free. No screen involved.

> Some invariants are universal and default-on per mechanism (`conservation`,
> `freespins_monotonic`, `balance_never_negative`) — they run on every scenario's stream
> unless a case opts out.

### `video:` — AI video assertion (Probabilistic)

- **Boolean claim** (shorthand string): `video: "<visual claim>"` → AI returns
  `{passed, confidence}` on whether it happened. For animations, glow, counters, popups,
  layout. e.g. `video: "the reels glow effect plays"`.
- **Read + compare** (object): AI *extracts* a value, the **framework** compares it to a bound
  API value — AI stays pure perception, the framework owns the equality:
  ```yaml
  - video:
      read:   "the win amount label"
      equals: "{res.payout}"          # exact; or  near: { value: "{res.payout}", tol: 0.001 }
  ```
- **`at:` — target a sub-clip.** By default a `video:` assert runs on the whole-scenario clip.
  `at:` trims it to one round: a scenario **mark** (`at: big_cascade`), or `trigger` / `terminal`
  / `res` / `rounds[i]`. The scenario picks *what* `big_cascade` means internally; QA just names it.
  ```yaml
  - video: { at: big_cascade, check: "symbols clear and new ones fall in for each cascade step" }
  ```
  ⚠ **A single-round `at:` usually ends before the presentation does.** A round's window runs from
  its own start to the NEXT round's start, and for an AUTO-played round (a free spin) that start is
  recorded when the framework begins *waiting* — which is immediately after the previous response.
  The response lands before the award animation, the bonus teaser or the big-win banner plays, so
  `at: trigger` yields a clip that stops mid-animation. When the claim is about a presentation,
  span one round further (`at: [trigger, rounds[1]]`), and say in the prompt that the extra round
  is there for framing and is not the subject.
- **`each:` — one short clip PER ROUND, each graded on its own.** `each:` names a LIST mark (every
  round where something happened — walking-wild's `wild_enter_rounds`, `stand_in_kick_rounds`).
  The framework cuts one clip per listed round, makes one AI call per clip (in parallel), and the
  assert passes only if **every** clip passes; the note names the rounds that did not. Use it when
  the claim is about a sub-second animation that recurs across a long feature: asked about a
  2-minute bonus in one go, the model loses the detail and cannot say which round went wrong. An
  empty list grades orange ("no round of this kind happened"), never green. `each:` replaces `at:`
  — the validator rejects both on one block. Sampled at the default 4 fps unless `high_fps: true`.
  **`max_rounds: N`** caps the calls: only N of the listed rounds are clipped, spread EVENLY over
  the list (not the first N), so every bonus the run played stays represented; the report says
  "sampled from M rounds". No `max_rounds` → every round is graded.
- **`videos:` — several shared blocks on one run.** A run-level `video:` holds ONE prompt over ONE
  clip. When a run needs questions about DIFFERENT moments (the whole bonus, the rounds a wild
  enters, the rounds a stand-in is knocked off), list them under `videos:`; each block has its own
  `at:`/`each:`, prompt and `expected_by_case`, and `video:` may stay alongside it.
  ```yaml
  videos:
    - each: wild_enter_rounds          # one clip per free spin where a grandma was added
      assets: [symbols.wild]
      prompt: >-
        ... This clip is ONE free spin. (1) grandma_enters: ... (2) enters_from: one of left, right, ...
      output_schema: { grandma_enters: { type: boolean }, enters_from: { type: string } }
      expected_by_case:
        TC-153: { grandma_enters: true, enters_from: left, confidence: medium }
  ```
- **`assets:` / `asset_groups:` — the pictures the model is shown.** A prompt that names a symbol
  ("the orange tabby", "the BONUS paw") is guesswork unless the model has seen it: these prime the
  call from `games/<game>/docs/assets/assets.yml`. `asset_groups: [symbols]` sends a whole group,
  `assets: [symbols.p1, symbols.scatter]` exactly those, an **explicit empty list sends none** (for
  a prompt about a counter or a button), and omitting the field sends the game's whole manifest.
  ⚠ Scope it. Grandma Pets has three cats and two dogs, so a drama prompt needs all five and a
  walking-wild prompt needs only the grandma — sending eleven pictures to answer "did she walk?"
  costs tokens and dilutes attention.
- **Verdict:** match → pass; mismatch → **orange** (triage); low confidence → **orange**.
  Never red. A read-mismatch can be a display bug *or* an AI misread — the API holds the truth,
  so a human looks. Read after the counter settles (mid-count-up legitimately differs).

Under the hood a `video:` compiles to your existing AI image/video assert
(`prompt` + `output_schema` + `expected`), with `{...}` interpolated and `read/equals` wrapped
as extract-then-compare.

### Video or image — which assert to write

**Default to `video:`.** Most claims are about something that happens: reels spinning, a walk, a
fight, a counter going down, a toast appearing, a popup closing. Only a clip shows motion, order and
timing, and the minimal-clip rule keeps it cheap.

**Use `image:` when the claim is about READING a static screen at full resolution**: text you have
to read (spelling, translation, text cut off), small numbers in a table, or whether pictures on a
page are drawn and in the right place. A video assert does not see full frames. It sees a
**montage** that samples about one frame per second and packs several frames side by side, so each
one is shrunk, and small text and table digits stop being readable. A still does not have that
problem.

| The claim is about… | Assert | Examples |
|---|---|---|
| something that moves or changes over time | `video:` | spins, walking wild, drama, counters, auto-play, toasts, reconnect |
| a value on the main screen (bet, balance, win) | the **response** (`stream:`) first; `video:` only for what is shown | TC-006/007 balance, TC-005 bet |
| reading a static page of text and pictures | **`image:`** | tutorial / info guide (TC-054, TC-056) |
| small numbers in a table | **`image:`** | paytable values at two bets (`paytable-bet-scaling`) |
| text in every language fitting its box | **`image:`** | `language-tour` |

If one case needs both (read a page *and* watch it animate), split it: `image:` for the reading,
`video:` for the motion, or a `stream:` fact where the bridge already knows the answer.

**How to write an `image:` assert:**

- The scenario must **capture the screenshots** (`ctx.screenshot("<label>")`); an `image:` assert
  grades the stills the scenario took, nothing else. `open-tutorial` shoots every screenful
  (`tutorial-01` … `tutorial-11`), and `paytable-bet-scaling` shoots `paytable-bet-a/b`.
- **`combine: true`** sends all the stills in **one** call, to be read as one document. Use it for
  one surface split across screenfuls (the tutorial). **Without it, each still is graded on its
  own and the assert passes only if every still passes**, which is right for a tour of *different*
  surfaces (`language-tour`).
- `at:` narrows which stills are graded: any shot whose label contains that text, ignoring case
  (`at: settings`, `at: paytable-bet-b`).
- Reference pictures (`assets:` / `asset_groups:`) are only attached to `video:` calls, so do not
  rely on them in an `image:` prompt; describe the symbol in words instead.
- Everything else is the same as `video:`: one `prompt` + `output_schema` + `expected_by_case`,
  the prompt must never contain the answer, and a mismatch is **orange**, never red.

```yaml
- run:
    scenario: open-tutorial
    image:
      combine: true
      prompt: >-
        These screenshots are the in-game guide, one screenful at a time from top to bottom.
        Read them as ONE document. (1) text_readable: … (2) images_in_place: …
      output_schema: { text_readable: { type: boolean }, images_in_place: { type: boolean } }
      expected_by_case:
        TC-054: { text_readable: true, confidence: medium }
        TC-056: { images_in_place: true, confidence: medium }
```

---

## 4. Binding — `{...}` interpolation

Resolved at runtime from the scenario's captured responses (the stable server contract, never a
bridge getter):

### What a scenario exposes, and where each name works

| name | what it is | as a `{...}` token | as a `video:` target |
|---|---|---|---|
| `res` | the single key round a one-round scenario reached (the winning spin) | `{res.payout}` | `at: res` |
| `rounds` | every collected round, in order (a whole bonus) | `{rounds[2].payout}` | `at: rounds[2]` |
| `trigger` | **always `rounds[0]`** — the first collected round, usually the one that started the bonus | `{trigger.totalFreeSpin}` | `at: trigger` |
| `terminal` | **always `rounds[-1]`** — the last collected round, the one that ended it | `{terminal.freeSpins}` | `at: terminal` |
| `facts` | values the scenario computed itself (`cascade_events`, `speed_after_on`); its `description` lists them | `{facts.cascade_events}` | — |
| mark | a round the scenario's logic picked out by name (`big_cascade`) | — | `at: big_cascade` |
| list mark | every round where something happened (`wild_enter_rounds`) | — | `each: wild_enter_rounds` — one clip per round, all must pass |
| span | from the first target's start to the second target's end | — | `at: [trigger, terminal]` |
| *(nothing)* / `all` | the whole scenario | — | the default when `at:` is omitted |

`trigger` and `terminal` are positions in `rounds`, not marks: a scenario mark that happens to be
named `trigger` (buy-bonus sets one to the purchase) is **not** what `at: trigger` resolves to. If
`rounds` is empty they do not exist, and a token using them is left unresolved. There is no
`{prev.*}` — for "the balance before the scenario", the scenario must expose it as a fact.

### The fields of a round (`res.`, `trigger.`, `terminal.`, `rounds[i].`)

A token reads the **raw server field** first (`payout`, `balance`, `freeSpins`, `totalFreeSpin`, …
— whatever the game's BET response calls it). Only if the response has no such field does it fall
back to the computed fields below, which every game has under the same name:

| field | meaning |
|---|---|
| `seq` | the round's position in the run |
| `phase` | `base` / `trigger` (a round the framework paid for and clicked) or `freespin` (an automatic round) |
| `game_state` | the profile's game state (Mystic: 1 base, 2 free spins, 3 buy bonus, 4 promo) |
| `free_spins` | free spins remaining |
| `total_free_spin` | free spins awarded in total |
| `won_free_spins` | free spins awarded by THIS round — 0 inside a bonus, where `total_free_spin` still reports the total |
| `stake` | what this round charged — 0 in free spins, the price on a bought bonus |
| `base_stake` | the bet payouts are computed from — differs from `stake` under Ante Bet, Premium Reel, a bought bonus and free spins |
| `payout` | what this round paid |
| `balance` | the balance after this round |
| `has_max_win` | the round hit the max-win cap (the bonus ends early, remaining spins forfeited) |
| `cascades` | grids evaluated, `len(result)` — the viewer sees **one fewer** clear-and-drop ([assert-traps.md](assert-traps.md), the counting trap) |
| `round_id` | the server's round id |

Numeric compares: `equals` (exact) or `near: { value, tol }`. Interpolation works in both
`stream:` inline predicates and `video:` prompts/`equals`.

---

## 5. Verdict & confidence

A **run** plays one scenario; each **case** under it gets its own verdict from *its* asserts (so
one scenario play yields many verdicts). Per case, combine the tier-tagged results:

```
scenario unreached (budget exhausted)      -> inconclusive   (excluded from fail counts)
any stream violation                       -> red            (Hard: real bug)
all stream pass, any video mismatch/low    -> orange         (Probabilistic: QA triage)
video not yet processed (batch AI)         -> pending
all pass                                   -> green
```

**Rules that must hold:** a `video:` result can never produce red; a `stream:` pass can never
be overridden by AI. That separation is what keeps the suite trusted — hard failures stay
actionable, soft flakiness stays labeled.

---

## 6. Worked examples (Mystic)

**Displayed win == API (the label-read case)**
```yaml
- case: MY-06
  scenario: base-cluster-win
  assert:
    - stream:  conservation                          # balance == prev - stake + payout  (Hard)
    - video: { read: "the win amount label", equals: "{res.payout}" }   # AI reads, fw compares
    - video: "the balance counter animates upward by the win"
```

**Free-spin award + multiplier + presentation (multi-round)**
```yaml
- case: MY-fs-award
  scenario: fs-complete
  assert:
    - stream:  freespin_award                        # scatter-reel count -> awarded spins (Hard)
    - stream:  multiplier_ladder(free)               # free-game ladder, per cascade (Hard)
    - stream:  stake_zero_in_freespins               # every FS round stake == 0 (Hard)
    - stream:  "{terminal.freeSpins} == 0"           # ended cleanly (Hard)
    - video: "the free-spin intro shows {trigger.totalFreeSpin} spins"   # AI, video
    - video: "the free-spin counter decrements to 0 across the bonus"     # AI, video
```

**Reels Glow (oracle computes expected, AI confirms visual)**
```yaml
- case: MY-glow
  scenario: base-bonus-symbol-spread            # arrive_when: bonus symbol on >=2 reels in 1..6
  assert:
    - video:
        check:  "the reels glow effect plays"
        expect: "{res.expected_glow}"           # engine derived it from result[].views
```

---

## 7. What this reuses (implementation is an evolution, not a rewrite)

| New concept | Built on existing |
|---|---|
| scenario (closed box) | the response-driven `drive()` engine, with its start/stop/collect timing kept internal — exposed only via `name` + `description` |
| exposed `video` | current Playwright recording + `timeline.json` + `video_trim` window |
| `stream: <invariant>` | current analyser pattern (`verifyFreeSpinAward`, `verifyHoldSpin*`) as pure Python over the captured response list |
| `video:` | current AI image/video assert (`prompt`/`output_schema`/`expected`) + `{...}` interpolation + read/compare wrapper |
| `inconclusive` | the verdict tier the earlier framework plan already called for (F5) |

QA stops writing `bridge_action` / `bridge_assert` steps against per-game getters. QA writes
`scenario:` + `assert:`. The imperative navigation lives in the scenario library (per mechanism),
the game rules live in the invariant library (per mechanism), and both are portable across every
game that shares the mechanism.

---

## 8. Integration with the existing run / case / replay / report pipeline

The new model **is** the existing `runs` / `cases` structure with two substitutions — so every
downstream artifact is produced by machinery you already have:

| Existing `runs` model | New model |
|---|---|
| a `run` plays a scenario **once** | same — the scenario is now the closed box (replaces scripted `steps:`) |
| `cases:` each get a verdict | same — each `case.id` is a QA test-matrix ID, each is one verdict |
| `bridge_assert` (analyser, `path: valid`) | `stream:` — an invariant over the exposed `response` |
| `ai` video + `expected_by_case` | `video:` — AI over the exposed `video`, with `{res.*}` bound in |

So **one scenario play → many case verdicts** — exactly "each run asserts many verdict."

**Artifacts, reused as-is** (see the runner guide §16–17):
- `result.json` (`kind: "run"`, `cases[]`) via `write_run_result()`; `case-{id}.html` via `write_case_report()`.
- `session.webm` — the scenario's exposed video.
- `events.jsonl` + `timeline.json`, `replay.html` — the per-case/run replay viewer.
- `summary.json` + `summary.html` + `report.html` — the existing Reporter, one row per case.

**What the closed box must emit for the replay** (not just the video): the `video`, the
`response` stream (rendered as replay **events**), and a `timeline` of the spins it drove. The
video alone is a recording; the events + timeline are what make the replay scrub-synced and
clickable, exactly as today. So a scenario returns `(video, response, timeline, reached?)`.

**Verdict mapping** onto the existing enum (per case; the run aggregates): `red` / `orange` /
`green` / `pending` as in §4, plus **`inconclusive`** when the scenario returns unreached.

**The one code change** the pipeline needs: add `inconclusive` to the verdict enum + summary
counts (excluded from failure totals) — the triplicated `_compute_verdict` in `suite_runner` /
`reporter` / `video_ai_processor` (runner guide §19). This is the F5 item from the framework
plan. Everything else — `write_run_result`, `write_case_report`, the replay template, the
Reporter — is untouched.
