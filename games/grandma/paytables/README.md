# Grandma Pets — forced paytables

Every fixture here is **derived**, never hand-edited, so it can be rebuilt when the base paytable
changes. The command that made each one is below; running it again must produce the same file.

`base.json` is a copy of `games/grandma/docs/raw/paytable.json` and is the derivation SOURCE only —
no run references it. Runs that need the game's real weights (Buy Bonus, Walking Wild) live in
`gm-real-weights.yml`, which pushes no paytable at all.

⚠ **`base.json` is not certainly the table the dev server runs.** Two captured wins disagree with
its pay values (`../bet-response.md` §3). That does not affect these fixtures, which only move REEL
WEIGHTS, and nothing in the suites compares a payout to this file — but it is why every forced run
carries a guard asserting what actually landed.

## What each one forces

| Fixture | Forces | Used by |
|---|---|---|
| `gm-fs-easy` | BONUS on reels 1–3 at **50%/cell**, none on 4–5 → **82% of spins trigger, always on exactly 3 reels**, and **98% of those carry 2+ BONUS on one reel** | A1, A2 |
| `gm-fs-4reel` | BONUS on reels 1–4 at 60% → **90% of triggers land on all 4 reels** | A3 |
| `gm-fs-5reel` | BONUS on all five at 60% → **88% of triggers land on all 5** | A4 |
| `gm-glow-bonus` | BONUS on reels 1–2 only → the glow condition every spin, and **a bonus can never trigger** (2 reels is below the 3 the award table needs) | G1 |
| `gm-glow-p1` | P1 on reels 1–2 at 60%, none on reel 3, **no WILD on reel 3**, and **no BONUS anywhere** | G2 |
| `gm-glow-negative` | P1 on reels 1 and 3, BONUS on reel 2, **no WILD on reel 2** | G3 |
| `gm-drama-bigwin` | P3 (orange cat) at 56% on every reel and **no BONUS anywhere** → a big win on nearly every spin, so the drama rolls at 70% instead of 4%, and no bonus interrupts the run | D1, D2 |

⚠ **Why the BONUS is zeroed in `gm-glow-p1`.** The anticipation fires on EITHER special symbol,
so a BONUS landing on two of the first four reels starts a sequence of its own — and that one keeps
going past reel 3, because a third BONUS anywhere would still pay. G2's whole claim is that the
sequence STOPS at reel 3 once the white cats can no longer connect, so a stray scatter pair would
fail the case for a reason that is not the game's fault. At the natural 1–3%/cell that was roughly
a one-in-ten flake per run.

⚠ **Why the wild is zeroed in the two glow negatives.** A ways win runs from reel 1 rightwards with
no gap, so "P1 on reels 1–2 and none on reel 3" *looks* like a broken chain — until a WILD lands on
reel 3, substitutes, and the chain is whole again. Without zeroing it, G2 and G3 would be testing
luck rather than the rule.

## Rebuilding them

```bash
cp games/grandma/docs/raw/paytable.json games/grandma/paytables/base.json

D=scripts/derive_paytable.py
python $D --game grandma --base base --symbol Scatter --reels 0,1,2   --probability 0.50 \
          --section BaseGame --zero-elsewhere --out gm-fs-easy
python $D --game grandma --base base --symbol Scatter --reels 0,1,2,3 --probability 0.60 \
          --section BaseGame --zero-elsewhere --out gm-fs-4reel
python $D --game grandma --base base --symbol Scatter --reels 0,1,2,3,4 --probability 0.60 \
          --section BaseGame --out gm-fs-5reel
python $D --game grandma --base base --symbol Scatter --reels 0,1     --probability 0.60 \
          --section BaseGame --zero-elsewhere --out gm-glow-bonus

# P1 glow: the run is broken at reel 3, and no wild may mend it
python $D --game grandma --base base       --symbol P1     --reels 0,1 --probability 0.60 \
          --section BaseGame --zero-elsewhere --out gm-glow-p1
python $D --game grandma --base gm-glow-p1 --symbol WILD_D --reels 2   --zero \
          --section BaseGame --out gm-glow-p1
python $D --game grandma --base gm-glow-p1 --symbol Scatter --reels 0,1,2,3,4 --zero \
          --section BaseGame --out gm-glow-p1

# the negatives: a P1 + BONUS mix, and a P1 pair that cannot connect through reel 2
python $D --game grandma --base base             --symbol P1      --reels 0,2   --probability 0.60 \
          --section BaseGame --zero-elsewhere --out gm-glow-negative
python $D --game grandma --base gm-glow-negative --symbol Scatter --reels 1     --probability 0.50 \
          --section BaseGame --zero-elsewhere --out gm-glow-negative
python $D --game grandma --base gm-glow-negative --symbol WILD_D  --reels 1     --zero \
          --section BaseGame --out gm-glow-negative

# drama: big wins nearly every spin, and nothing that can start a bonus
python $D --game grandma --base base            --symbol P3      --reels 0,1,2,3,4 --probability 0.55 \
          --section BaseGame --out gm-drama-bigwin
python $D --game grandma --base gm-drama-bigwin --symbol Scatter --reels 0,1,2,3,4 --zero \
          --section BaseGame --out gm-drama-bigwin
```

`--zero` was added for these: it sets a symbol to weight 0 on the **target** reels, the inverse of
`--zero-elsewhere`. `--probability 0` cannot say it — that mode takes an open interval, because 0
there would silently mean "unchanged" in the arithmetic that keeps the other symbols' ratios.

## ⚠ Why `gm-fs-easy` is 50% and not 30%

A1 does not want just any trigger. TC-103a's real claim is that the award counts REELS, so it needs
a trigger carrying **two BONUS on one reel** — the shape a ways-based count would get wrong. The
scenario hunts for exactly that (`arrive_when: {bonus_reels: 3, min_on_one_reel: 2}`) and **plays
out every trigger that does not match**, because leaving a bonus owed would poison the rounds after
it. On screen that looks like the run "not stopping": trigger, full bonus, extension declined, and
back to spinning.

At 30%/cell only 84% of triggers carried a doubled reel, so about one run in six paid for a whole
extra bonus before starting the one it graded. Density fixes it for nothing:

| scatter/cell | P(trigger) | P(2+ on a reel, given a trigger) | bonuses replayed |
|---|---|---|---|
| 30% | 44% | 84.1% | 0.19 |
| 40% | 66% | 93.7% | 0.07 |
| **50%** | **82%** | **98.1%** | **0.02** |

At 50% a matching trigger arrives in **1.2 spins on average**. Nothing about the case changes —
3 reels still awards 8 — only how long the run spends getting there. `facts.triggers_skipped`
records any that were played out.


## Only `BaseGame` is touched

No fixture changes `FreeSpinGame` or `Packages`. The bonus therefore plays at the game's **real**
free-spin weights, which is what the walking-wild cases need, and a forced trigger cannot make the
bonus itself unrepresentative. The one consequence: a bonus can still retrigger at its natural rate,
so every forced run carries a `max_win_x` cap.
