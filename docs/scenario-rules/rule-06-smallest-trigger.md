# Rule 6 — force the SMALLEST trigger that reaches the feature, not the biggest

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

A forced paytable exists to reach a feature **quickly and identically every run**. It is not there to
make the feature as big as possible — and the two pull in opposite directions, because in most games
the award scales with how hard the trigger was.

Chaos free spins:

| `FreeSpin!` on N distinct reels | award |
|---|---|
| 3 | **12 spins** |
| 4 | 15 spins |
| 5 | 20 spins |

Raising `FreeSpin!` on **all five** reels triggers ~every spin — and lands 4 or 5 reels most of the
time, so nearly every run sits through 15–20 free spins, each with its own animation. Target only
the **three** reels the minimum award needs.

## Make it FREQUENT (~30%), never FORCED (100%)

This is the part that is easy to get wrong, and getting it wrong is invisible until you read the
grid. Reel weights are **per cell**. A reel raised to 100% of one symbol does not show that symbol
once — it shows it in **every visible row**. And in a ways game the outcome is the PRODUCT of the
per-reel counts, so a 100% force multiplies the very thing you were trying to keep small:

| symbol per reel | Chaos free spins (award = base x ways) | Chaos P1 (pay = ways x 10 x bet) |
|---|---|---|
| 100% force → 3, 3, 3 | ~27 ways → a bonus of **hundreds of spins**, every run | 27 ways → **270x** base bet, always capped |
| **30%** → usually 1, 1, 1 | ways ~2.5 → the **12-spin minimum** | 1–2 ways → ~26x base bet, the ordinary win |

So both Chaos fixtures use a probability pass, not a force:

```
python scripts/derive_paytable.py --game chaos --base base --symbol "FreeSpin!" \
    --reels 0,1,2 --probability 0.30 --section BaseGame --out chaos-fs-easy
python scripts/derive_paytable.py --game chaos --base base --symbol "P1" \
    --reels 0,2,4 --probability 0.30 --section BaseGame --out chaos-p1-ways
```

At 30% per cell a reel shows the symbol ~66% of the time, so three reels line up on roughly a third
of spins.

**`--zero-elsewhere` pins the award to its minimum.** A probability pass raises the symbol on the
reels you name and leaves the others at their real rate — which is fine when the award does not
scale, and wasteful when it does. On Chaos ~17% of triggers still reach a fourth or fifth reel, and
each one costs the run three to eight extra rounds of animation. Adding `--zero-elsewhere` sets the
symbol to weight 0 on every reel you did NOT target, so it is not merely unlikely there but
impossible, and a trigger is always the 3-reel, shortest award (`joker-fs-easy` is derived that
way). Leave it off when the other reels' real rate is part of what the case observes.

**The price is a short hunt, and it is worth paying.** 30% does not guarantee the condition on spin
1, so **the run spins until it matches** under a budget — a handful of spins for an outcome that
looks like real play, instead of one spin for an outcome no player will ever see. Size the budget
from the odds and say so in the suite comment (Chaos P1: ~28% a spin, `max_spins: 40`, a
~1-in-a-million chance of missing).

Reach for a 100% force only when the symbol's COUNT does not scale the outcome — a fixed layout
check, say — and write down why.

## A two-sided rule puts a CEILING on the probability too

Some rules are thresholds, and testing one properly needs a result on BOTH sides of it. Chaos's
Raining Wild is the example: 2+ Wilds must play the animation, fewer must not — and without the
negative half an implementation that rained on every spin would pass perfectly.

For those, raising the symbol does not merely cost money, it deletes the case:

| `WILD!` per cell | P(2+ Wilds) | P(control, <2) | spins to get BOTH |
|---|---|---|---|
| **15%** | ~55% | ~45% | median 3 |
| 50% | 99.7% | 0.3% | ~315 |
| 100% force | 100% | **0** | never — unreached, every case inconclusive |

Aim at the MIDDLE of the distribution, not the top: pick the probability that maximises the rarer
side. And never force the symbol onto a reel the rule under test says it cannot occupy —
`chaos-wild-rain` leaves reel 0 at weight 0 precisely because CR_059 asserts Wild never lands
there.

**Only force the section you need.** `--section BaseGame` leaves `FreeSpinGame` at its real weights,
so the bonus does not retrigger itself and actually ends. Forcing the bonus section too is a common
way to make `terminal_reached` hang.

**The general rule.** Before forcing an outcome, ask what the case actually needs to observe. A case
about *"free spins play at stake 0"* needs a bonus, any bonus — so take the cheapest one. Only force
a bigger outcome when the SIZE is the thing under test (a max-win cap, a 5-reel award value), and
then say so in the suite comment, because the run cost is now deliberate rather than accidental.
