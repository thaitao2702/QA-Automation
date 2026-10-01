# Chaos Runner — paytables

Forced paytables for deterministic runs. A suite selects one with `paytable: { ref: <stem> }` on a
run; the runner pushes it to BO before the game loads and resets to the game default afterwards.

| file | what | used by |
|---|---|---|
| `base.json` | the game's real BO paytable, copied from `docs/raw/paytable.json`. **Never referenced by a suite** — it is the input `derive_paytable.py` derives from. | — |
| `chaos-fs-easy.json` | `FreeSpin!` raised to ~**30%** of BASE reels **0, 1, 2**; reels 3 and 4 left at their real ~3%. ~37% of spins trigger, and 83% of triggers take the **shortest** award: 3 reels = 12 free spins. ⚠ It is a probability pass, NOT a force — reels 3 and 4 are not zeroed, so a 4- or 5-reel award still arrives on ~17% of triggers. Add `--zero-elsewhere` when re-deriving if that matters. | `freespin-zero-balance` (CR_011), `fs-complete`, `random-multiplier`, `auto-stop-at-win` |
| `chaos-p1-ways.json` | `P1` raised to **~30%** of BASE reels **0, 2, 4** (the only reels it may occupy), other symbols kept. ~28% of spins are a P1 ways win, and the win is the small 1-2 way kind. | `p1-ways-win` (CR_062 / CR_063 / CR_045) |
| `chaos-wild-rain.json` | `WILD!` raised to **~15%** of BASE reels **1, 2, 3, 4**; reel 0 left at 0. ~55% of spins hold 2+ Wilds and ~45% hold fewer, so BOTH halves of the Raining Wild rule arrive within a few spins. | `raining-wild` (CR_059 / 059b / 060 / 061) |

## How `chaos-fs-easy` was made

    python scripts/derive_paytable.py --game chaos --base base --symbol "FreeSpin!" \
        --reels 0,1,2 --probability 0.30 --section BaseGame --out chaos-fs-easy

`--probability 0.30` sets `FreeSpin!` to 30% of each target reel's total weight and keeps the other
symbols' relative weights. Reels 3 and 4 are left at their real ~3%. All three `ReelDistributions`
are updated. Verify with the numbers in the file: reels 0/1/2 read 41/137, 38/126, 38/126 — 30% per
cell, against 2/98 in `base.json`.

Result: each of reels 0/1/2 shows `FreeSpin!` about 66% of the time, so ~37% of spins trigger —
roughly 1 in 2.7 — and 83% of those triggers take the 12-spin minimum. It is deliberately NOT
~100%; see below.

## Why only three reels

The trigger needs `FreeSpin!` on 3+ **distinct reels**, and the award scales with how many reels
hit:

| reels hit | free spins |
|---|---|
| 3 | **12** |
| 4 | 15 |
| 5 | 20 |

Making `FreeSpin!` frequent on all five reels triggers just as reliably, but then lands 4 or 5
reels most of the time — 15 to 20 free spins to sit through on every run, each with its own
animation. Raising only three keeps the award on the 12 row for ~83% of triggers: reels 3 and 4
are still at their real ~3%, so the other ~17% reach a fourth or fifth reel.

**To close that last 17%, re-derive with `--zero-elsewhere`** — it sets `FreeSpin!` to weight 0 on
the reels you did not target, making a 4- or 5-reel award impossible rather than merely unlikely.
This fixture predates the flag and is left as it is because its callers do not care which award
they get; `games/joker/paytables/joker-fs-easy.json` is derived the tighter way.

Forcing the *maximum* trigger is the easy mistake. Force the **minimum** that still reaches the
feature — see `docs/scenario-authoring-rules.md` Rule 6.

## How `chaos-p1-ways` was made

    python scripts/derive_paytable.py --game chaos --base base --symbol "P1" \
        --reels 0,2,4 --probability 0.30 --section BaseGame --out chaos-p1-ways

Same 30% treatment as `chaos-fs-easy`, on the three reels P1 is allowed to occupy. Reels 1 and 3 are
untouched: P1's weight there is already 0 in the real paytable, which is the reel restriction the
design doc states and CR_062 verifies against the UNFORCED strips.

**Why 30% and not a 100% force — this is the whole lesson of the fixture.** The design doc gives the
P1 win as a WAYS win: ways is the product of how many P1 land on each of reels 1, 3 and 5, paying
`ways x 10 x base bet`. Weights are per-cell, so a reel forced to 100% `P1` shows it in all three
rows, and the product explodes:

| P1 per reel | ways | gross |
|---|---|---|
| 100% force → 3, 3, 3 | **27** | 270x base bet — always capped, nothing like real play |
| 30% → usually 1, 1, 1 | **1** (mean 2.57) | ~26x base bet on average |

The 30% version is the win a player actually gets, and it matches the captured payload
`fixtures/bet-responses/p1-scatter-win-maxwin-capped.json` (2 ways, gross 12 at base 0.6).

The price is that the condition is no longer guaranteed on spin 1: all three reels carry P1 on ~28%
of spins, about 1 in 3.5, so **the run spins until it matches** under a budget (`max_spins: 40`, a
~1-in-a-million chance of missing). A short hunt for a realistic outcome beats a guaranteed
unrealistic one — Rule 6.

## `--section BaseGame` matters

`FreeSpinGame` keeps its real 2–4% weights, so a triggered bonus does not retrigger itself
repeatedly and the round actually ends. Raising it there too would make `terminal_reached` hang.

## How `chaos-wild-rain` was made

    python scripts/derive_paytable.py --game chaos --base base --symbol "WILD!" \
        --reels 1,2,3,4 --probability 0.15 --section BaseGame --out chaos-wild-rain

**Reel 0 is not a target, on purpose.** `WILD!` has weight 0 on reel 0 in every distribution of the
real paytable, and CR_059 asserts exactly that (`symbol_reel_restrictions`). Forcing Wilds there
would break the rule the case exists to check.

**15%, and this one has a CEILING as well as a floor.** `chaos-fs-easy` and `chaos-p1-ways` only
need their situation to arrive often; this scenario needs the situation AND its opposite — a result
with 2+ Wilds *and* a control result with fewer than 2, because "the animation does not play below
the threshold" is half the rule. Pushing Wilds higher destroys that half:

| Wild per cell | P(2+ Wilds) | P(control, <2) | spins to get BOTH |
|---|---|---|---|
| 2–6% (real strips) | uncommon | almost every spin | a long hunt for the rain |
| **15%** | **~55%** | **~45%** | **median 3, worst of 2000 runs 16** |
| 50% | 99.7% | 0.3% | ~315 |
| 100% force | 100% | **0** | never — the run cannot reach |

So the balance point is the middle of the distribution, not the top of it. Where a test judges a
threshold in both directions, pick the probability that maximises the RARER side.
