# Joker: Where's My Banana — paytable fixtures

Forced paytables for deterministic runs. A suite selects one with `paytable: { ref: <stem> }` on a
run; the runner pushes it to BO before the game loads and resets to the game default afterwards.
It needs the `bo:` block in `games/joker/env.config.<env>.yml` — without it a `paytable:` run is
**skipped with an error**, never run unforced.

| file | what | used by |
|---|---|---|
| `base.json` | the game's real server paytable, copied from `docs/raw/paytable.json`. It is what `derive_paytable.py` derives from — and it is also referenced DIRECTLY by the Buy Bonus runs, which need a cap but no forced weights (see below). | `buy-bonus-lucky-spin` ×3 |
| `joker-fs-easy.json` | `FreeSpin!` raised to ~**30%** of BASE reels **1, 2, 3** and set to **0** on reels 4 and 5. A bonus arrives in a median of **3** spins instead of **236**, and always on exactly three reels. | `fs-complete` (JK-60 … JK-62, JK-68, JK-69) |

Keep `base.json` in step with the server:

```bash
cmp games/joker/docs/raw/paytable.json games/joker/paytables/base.json
```

If that differs, re-copy it and re-derive `joker-fs-easy` with the command below.

## Why a fixture at all — the unforced odds

`FreeSpin!` sits at 1–4% per cell across the five base reels, and the trigger needs it on **three**
of them. Measured over 200 000 simulated spins on `base.json`:

> **0.42% of base spins trigger — 1 in 236.**

A hunting run is therefore ~236 real spins at the live stake before the footage it exists for even
starts, and the failure is quiet: a run that spends its budget returns **unreached** and every case
under it reports `inconclusive`. That is the whole of Rule 17, in one number.

## How `joker-fs-easy` was made

```bash
python scripts/derive_paytable.py --game joker --base base --symbol "FreeSpin!" \
    --reels 0,1,2 --probability 0.30 --section BaseGame --zero-elsewhere --out joker-fs-easy
```

Exactly **10 numbers** change — the `FreeSpin!` weight on the five base reels of both
`ReelDistributions`. Nothing else in the file moves:

| reel | `base.json` | `joker-fs-easy.json` |
|---|---|---|
| 1 | 3 / 89 = 3.4% | **37 / 123 = 30.1%** |
| 2 | 1 / 75 = 1.3% | **32 / 106 = 30.2%** |
| 3 | 2 / 66 = 3.0% | **27 / 91 = 29.7%** |
| 4 | 3 / 71 = 4.2% | **0** |
| 5 | 1 / 55 = 1.8% | **0** |

Result, simulated over 400 000 spins of the derived file:

| | |
|---|---|
| trigger rate | **28.3% of spins** — 1 in 3.5 |
| spins to a trigger | median **3**, p95 **10**, worst of 113 000 triggers **43** |
| P(no trigger in 40 spins) | 1.6 × 10⁻⁶ |
| reels holding the scatter on a trigger | **always exactly 3** |

## Why 30%, and why the other two reels are zeroed

**30%, not 100%** — Rule 6. Weights are per cell, so a reel forced to 100% shows `FreeSpin!` in
**all three rows**, not once. On this server the award is counted off the grid, so a 100% force is
the fastest way to turn a 12-spin bonus into a very long one. 30% per cell makes a reel show the
symbol ~66% of the time, which is frequent without being a force, and the grid looks like real play.

**Reels 4 and 5 zeroed** — also Rule 6, and this is what `--zero-elsewhere` exists for.
`BaseGame.FreeSpins` awards by occurrence count:

| occurrences | free spins |
|---|---|
| 3 | **12** |
| 4 | 15 |
| 5 | 20 |

Leaving reels 4 and 5 at their real rate lets ~17% of triggers reach four or five reels — 15 or 20
free spins, three to eight extra rounds of animation on a run that needed none of them. With those
two reels at weight 0 the scatter can only ever occupy three reels, so **every** trigger is the
3-occurrence, shortest award.

## `--section BaseGame` matters

`FreeSpinGame` keeps its real 1.5–4.9% `FreeSpin!` weights, so a bonus does not retrigger itself and
actually ends. Raising it there too is the usual way to make `terminal_reached` hang. It also leaves
`WILD!` alone in the bonus, which is what JK-61 / JK-62 grade: a wild lands on an expanding reel in
~99.8% of 12-spin bonuses at the real weights, so the feature does not need forcing to show up.

## ⚠ One unknown this fixture cannot settle: is the award multiplied by *ways*?

Chaos ships the **same paytable schema** from the same server — `FreeSpins: [{Occurrences,
FreeSpins}]`, 3/4/5 → 12/15/20 — and a captured Chaos payload proved its award is
**`base(by reels) × ways`**, where ways is the product of the per-reel scatter counts
(`ChaosRunner.expected_fs_award`, confirmed at 48 free spins for counts `[1,2,0,2,0]`). Joker is a
20-payline game rather than a ways game, and `JokerBanana.FREESPIN_AWARDS` assumes the plain
`{3: 12}` reading — but **nothing captured on Joker proves either way** (`bet-response.md` §7.1: no
free-spin trigger has been captured at all).

Both readings agree the base is 12 under this fixture, because the scatter can only reach three
reels. They differ in the tail:

| ways on the trigger | share of triggers | award if plain | award if × ways |
|---|---|---|---|
| 1 | 30.5% | 12 | 12 |
| 2 | 38.7% | 12 | 24 |
| 3 – 4 | 22.3% | 12 | 36 – 48 |
| 6 + | 8.4% | 12 | 72 – 324 |

So the asserts in `jk-features.yml` are written to hold under **both** (`total_free_spin >= 12` and
a multiple of 12), and every forced run carries `max_win_x: 20` — the Rule 17 default — so the long
tail is bounded by the server truncating rather than by a run that will not end. **The first live
run settles it** — record `{terminal.total_free_spin}` against the ways on the trigger grid. If the
award does multiply, lower `--probability` to 0.20 (ways 1 on 49% of triggers, trigger rate still
11.6% — 1 in 8.6) and re-derive.

## What the cap costs, and what guards it

`max_win_x: 20` bounds the runaway above, and the price is LENGTH: the server can truncate the
bonus the moment the win reaches 20x the base bet, which on a lucky trigger is free spin 1 or 2.
The expanding wild is not forced — `FreeSpinGame` keeps its real `WILD!` weights — so a short bonus
can legitimately end without one ever landing on an expanding reel. Measured over 200 000
simulated bonuses on this fixture's (untouched) free-spin weights:

| free spins played | P(no wild on reels 2 & 4) | ...if only ONE of them expands |
|---|---|---|
| 2 | 35% | 55% |
| 4 | 12% | 31% |
| 6 | 4.3% | 17% |
| 12 (the full award) | **0.2%** | 2.9% |

That is why the runs assert the length rather than trusting it: `{facts.free_spins_played} >= 2`
where the case judges a rule over whatever rounds ran, and `>= 4` on JK-62, whose clip has to SHOW
an expansion. Per Rule 17 a red on either is not a game defect — it says the cap is too tight for
this award, and the fix is to raise the multiple. Nothing asserts `{facts.capped} == False`: at 20x
the cap is expected to bite, and grading it would make the cap the thing under test.

## Referencing `base.json` on purpose — a cap without a force

The Buy Bonus runs in `jk-features.yml` declare `paytable: { ref: base, max_win_x: "5 * 100 * bet" }`.
They want the CAP and none of the forcing, which looks like it should be written as a bare
`{ max_win_x: ... }` with no `ref` at all. **It must not be.**

A `paytable:` block with no `ref` and no `inline` resolves no content, and the runner then does:

| step | with no content |
|---|---|
| `ensure_paytable(code, paytable_content=None)` | **idempotent — no write when the paytable already exists** |
| `select_agent_paytable(code, max_payout=…)` | the agent plays that paytable, capped |
| `update_paytable(code, content)` | **skipped** (`if content is not None`) |

So the run re-selects the automation paytable holding **whatever the last forced run left in it** — in
`jk-features.yml` that is `joker-fs-easy`, pushed by the run above. The teardown resets the AGENT off
the automation paytable; it does not restore that paytable's CONTENT.

Here it happens to be harmless — `joker-fs-easy` only touches `BaseGame`, and a bought bonus does not
use those weights to enter — but that is luck, not design. `ref: base` pushes the real weights
explicitly, so a capped run is forced to the unforced paytable rather than inheriting one.

### Why the cap is `"5 * 100 * bet"` and not `20`

`max_win_x` multiplies the **live base bet**, and a Buy Bonus purchase costs **100× that bet**. The
Rule 17 default of `20` is therefore **0.20× the price the player just paid**:

| live bet | purchase price | `max_win_x: 20` | `"5 * 100 * bet"` |
|---|---|---|---|
| 0.60 | 60.00 | **12.00** — 0.2× the price | 300.00 — 5× the price |
| 4.00 | 400.00 | **80.00** — 0.2× the price | 2000.00 — 5× the price |

At `20` the bonus would truncate on essentially every run that won anything, and JK-66 ("plays EVERY
awarded spin") would go red on a correct build. A 100× purchase returns ~96× on average, so five
times the price sits well clear of an ordinary outcome and only bounds a genuine runaway.

The expression form is deliberate: `100 *` is the package multiplier and `5 *` is the headroom, so
either can be changed on its own with the reason visible. It resolves to exactly the same number as
the bare `500` would.

### Is the cap needed at all? — measured, not assumed

Rule 17 names "a bought bonus" as one of three shapes that MUST carry a cap. On this game the
bought bonus can only feed itself at REAL `FreeSpinGame` weights, because nothing forces that
section. Over 200 000 simulated 20-spin bought bonuses:

| | |
|---|---|
| retrigger chance per free spin | **0.33%** (1 in 305) |
| P(any retrigger in the 20 spins) | **6.6%** |
| rounds played | median 20 · p95 32 · p99 35 · longest seen 104 |
| P(reaching the scenario's 120-round drain cap) | **0 in 200 000** |

A long tail, not a runaway. The cap earns its place on the tail rather than on the danger: one run in
fifteen plays 32+ rounds, and six cases share one clip — a 32-round clip is materially harder for the
AI to grade than a 20-round one.
