# Chaos Runner — BET response reference

The server's BET payload: the contract the whole framework drives on. Scenarios read game LOGIC
from here and nowhere else, so this is the file to check before writing an assert, an invariant, or
anything that counts a symbol.

Real captured samples live in `fixtures/bet-responses/` (captured 2026-08-24):

| File | What it is |
|---|---|
| `base-win-two-lines.json` | base spin, **two** paying lines, multiplier x1, payout 0.6 on stake 0.6 |
| `base-win-multiplier-x2.json` | base spin, one line at rate 10, **multiplier x2**, payout 0.6 |
| `freespin-trigger.json` | base spin that **awarded 48 free spins** (`gameState: 2`), paid 0 |
| `ante-labelled-spin.json` | captured while Ante Bet was believed on — but reads as a PLAIN round (§7) |
| `ante-bet-wild-win.json` | **a real ante round**: `anteMultiplier: 3`, stake 1.8 on base 0.6, two Wilds substituting, paid 4.5 |
| `p1-scatter-win-maxwin-capped.json` | **a P1 scatter win, truncated by the cap**: 2 ways -> `rawPayout: 12`, `maxWin: 10`, credited **10**, `hasReachedMaxWin: true` |

Every rule below was checked against all six by `tests/chaos/verify_against_fixtures.py`
(67 checks, each rule run on the real payload *and* on a mutated copy).

---

## 1. Envelope — and the double nesting

```jsonc
{ "status": 1, "code": 0, "msg": "", "timestamp": 1787569124344,
  "result": {                    // <- the ROUND (this is what Bet.raw holds)
      "result": { "rawViews": null, "views": [ ... ] },   // <- the GRID, nested again
      "payout": 0.6, "stake": 0.6, ... } }
```

**Two `result` keys, one inside the other.** The bridge doc says `awaitBetResponse()` returns the
envelope verbatim, and it does — so `SlotGame.unwrap()` peels the outer one and `Bet.raw` becomes
the round. The round's *own* `result` is the grid container.

> This bit the framework once already: `Bet.parse` originally read fields off the top level, where
> `payout` does not exist, so every round parsed as a zero-payout loss and `conservation` failed a
> game that was behaving perfectly.

## 2. The round object

| Field | Type | Notes |
|---|---|---|
| `result` | object | `{rawViews, views}` — the grid, §3. `rawViews` was `null` in every sample |
| `gameState` | int | `1` = Base, `2` = FreeGame (confirmed by the trigger sample) |
| `stake` | number | what THIS bet cost |
| `baseStake` | number | **the payout base — every payout derives from this, never from `stake`.** Equal to `stake` except on an ante round (§7) |
| `rawPayout` | number | the win **before** the max-win cap |
| `payout` | number | what is actually CREDITED = `min(rawPayout, maxWin)`. Fully derivable — §5 |
| `accumulatedWinAmount` | number | equals `payout` in these single-round samples |
| `winlose` | number | `payout - stake`. Confirmed exact on all five, including the ante round |
| `balance` | number | **authoritative** post-payout balance. Use this, never arithmetic |
| `freeSpins` | int | remaining |
| `totalFreeSpin` | int | **awarded** — 48 in the trigger sample. See §6 |
| `totalFreeSpinPayout` | number | bonus running total (0 in the trigger) |
| `randomMultiplier` | int | the round's multiplier — **`randomMultiplier`, not `multiplier`** |
| `scatterMultiplier` | int | P1's `ways x 10`. Pays `x baseStake` with **no** `/betLines` — §5 |
| `anteMultiplier` | int | `0` = ante off; **`3`** on the confirmed ante round — §7 |
| `paylines` | array | the paying lines, §4 |
| `betLines` | int | **20** in every sample — this settles the open payline question |
| `maxWin` | number | **a hard cap on the credited payout, in currency.** It really truncates — §5 |
| `hasReachedMaxWin` | bool | `true` exactly when `rawPayout > maxWin` |
| `id` / `refId` / `roundId` | string | equal on plain rounds; the two-line sample has `id` ≠ `roundId` |
| `jackpot` | number | `0` |
| `promoResult` | null | |
| `betTime` / `processedTime` / `settledTime` | epoch ms | |

## 3. `result.views[]` — the grid

```jsonc
"views": [ { "id": 1, "value": [11, 1, 2] }, ... ]     // 5 entries = 5 REELS, 3 rows each
```

One entry per **reel** (`id` 1-5), `value` top→bottom. Grid is **5x3**, confirming QA.

**Symbols are NUMERIC IDS, not names.** This is the single biggest difference from the cascade
reference, which sends strings like `"FreeSpin!"`.

### Confirmed symbol ids

Derived by pairing `paylines[].symbols` against the same entry's `winType` — e.g.
`symbols: [1,1,1,2,1]` with `winType: "A-A-A-J-A"` pins `1 = A` and `2 = J`:

| Id | Symbol | Evidence |
|---|---|---|
| `1` | A | `A-A-A-J-A`, `J-J-J-A-K` |
| `2` | J | `A-A-A-J-A`, `J-J-J-J-P2`, `J-J-J-A-K` |
| `4` | K | `J-J-J-A-K` |
| `6` | P2 ("can") | `J-J-J-J-P2`, `P2-WILD!-P2-WILD!-P3` |
| `7` | P3 ("Mask") | `P2-WILD!-P2-WILD!-P3` |
| `9` | **WILD!** | `P2-WILD!-P2-WILD!-P3` |
| `11` | **FreeSpin!** (Bonus / scatter) | `A-A-A-FreeSpin!-A`, and it is what triggers the bonus |

| `5` | **P1** ("Fuel") | see below — confirmed by a P1 scatter win |

**P1 = `5`, confirmed three ways over a single round** (`p1-scatter-win-maxwin-capped.json`):

* id `5` landed on reels **[1, 3, 5]** and nowhere else — P1's documented restriction, and the reel
  strips give P1 weight `0` on reels 2 and 4;
* its ways count `2 x 1 x 1 = 2`, times the 10x multiplier, equals the round's reported
  `scatterMultiplier` of **20**, exactly;
* and `rawPayout` was **12** = `2 ways x 10 x baseStake 0.6`.

The earlier hypothesis — P1 = 5, because P2 = 6 and P3 = 7 run sequentially — was right, and the
same reasoning puts **P4 at `8`** and **Q at `3`**, the two ids still unnamed. Those two remain
*inference only*: any win line naming them settles it.

## 4. `paylines[]` — the paying lines

```jsonc
{ "symbols": [1,1,1,2,1], "payline": 1, "payoutRate": 10, "winType": "A-A-A-J-A" }
```

- `symbols` / `winType` list the **whole line**, not just the winning run. `A-A-A-J-A` pays for the
  leading three A's; the `J` in position 4 is what stops it.
- `payoutRate` is the symbol's multiplier for that match count, and it comes straight from the
  BO paytable (`docs/raw/paytable.json` -> `SymbolMultipliers`). **All five observed rates match
  the export exactly** — 3xA = 10, 3xJ = 4, 4xJ = 10, 4xP2 = 150. That is what makes
  `payline_rate_matches_paytable` a real BO-to-balance check. Full table in
  [docs/final/paytable.md](docs/final/paytable.md).
- `payline` is the line index (1, 10, 13, 19 observed — consistent with 20 lines).
- **Wild substitution is visible here.** `symbols: [6,9,6,9,7]` /
  `winType: "P2-WILD!-P2-WILD!-P3"` pays rate 150 — the two Wilds (`9`) stand in for P2 to
  make four of a kind, and the P3 in position 5 stops the run. This confirms the documented
  rule that Wild substitutes for anything except Bonus and P1.

### The synthetic free-spin entry

The trigger round reports:

```jsonc
{ "symbols": [-1,-1,-1], "payline": -1, "payoutRate": 1, "winType": "FreeSpin" }
```

**`payline: -1` marks a pseudo-line.** There are two kinds, and they behave differently:

| `winType` | What it means | Does its `payoutRate` pay? |
|---|---|---|
| `"FreeSpin"` | the bonus was triggered | **No** — rate was `1`, the round paid `0` |
| `"Scatter"` | a P1 ways win | **Yes** — rate `20`, and the round paid `20 x baseStake` |

So a pseudo-line cannot simply be summed with the real ones, nor simply discarded.
`GAME.win_lines()` filters `payline == -1` out of the LINE total, and the scatter half is taken
from the `scatterMultiplier` field instead — which carries the same value and is unambiguous.

Note the `symbols` array has exactly **3** entries on the FreeSpin trigger, matching the 3 reels
that held a Bonus — corroborating the reel-based award rule in §6.

## 5. Payout — fully derivable, in two parts, then capped

```
lineWin    = randomMultiplier x SUM(payoutRate over real paylines) x baseStake / betLines
scatterWin = scatterMultiplier x baseStake            <- note: NO /betLines
rawPayout  = lineWin + scatterWin
payout     = min(rawPayout, maxWin)                   <- what actually reaches the balance
```

**Exact on all six samples.** Three things here were each learned the hard way:

| Sample | mult | lines | scatter | raw | cap | credited |
|---|---|---|---|---|---|---|
| `base-win-two-lines` | x1 | `1 x (10+10) x 0.6/20` = 0.6 | 0 | 0.6 | 600 | **0.6** ✓ |
| `base-win-multiplier-x2` | x2 | `2 x 10 x 0.6/20` = 0.6 | 0 | 0.6 | 600 | **0.6** ✓ |
| `ante-labelled-spin` | x2 | `2 x 4 x 0.6/20` = 0.24 | 0 | 0.24 | 10 | **0.24** ✓ |
| `freespin-trigger` | x1 | 0 | 0 | 0 | 600 | **0** ✓ |
| `ante-bet-wild-win` | x1 | `1 x 150 x 0.6/20` = 4.5 — note **0.6**, not the 1.8 staked | 0 | 4.5 | 10 | **4.5** ✓ |
| `p1-scatter-win-maxwin-capped` | x1 | 0 | `20 x 0.6` = **12** | 12 | **10** | **10** ✓ truncated |

1. **A scatter pays on the WHOLE bet, with no `/betLines`.** `20 x 0.6 = 12` is the round's
   `rawPayout`; dividing by `betLines` would have given `0.6`. This matches the design doc's
   "Scatter winnings: base bet x Multiplier" and its P1 worked example — both of which turn out to
   be correct, unlike its line arithmetic.
2. **`maxWin` is a real, currency-denominated cap**, and `payout` is the truncated value while
   `rawPayout` is the gross. The P1 round earned 12 against a cap of 10 and credited **10**, with
   `hasReachedMaxWin: true`. Only `payout` reaches the balance; `winlose` (9.4) uses the capped
   figure too.
3. **The `/20` divisor is the server's own `betLines`**, and the base is `baseStake` — never the
   ante-inflated `stake` (§7).

> ⚠️ **Unconfirmed:** whether `randomMultiplier` also multiplies the *scatter* half. The only
> captured scatter win had `randomMultiplier: 1`, so the two readings are indistinguishable. The
> profile deliberately does **not** apply it, so a future scatter win on a x2+ spin will surface the
> question as a violation instead of hiding it.

Asserted by `payout_matches_paylines` (the arithmetic), `payline_rate_matches_paytable` (the rates
against BO config), `scatter_pay_matches_p1_ways` (the scatter multiplier against the grid) and
`max_win_cap_applied` (the truncation and its flag).

## 6. Free spins — the award rule ⚠ **both documented readings are wrong**

```
reels = number of distinct REELS holding a Bonus symbol   (>= 3 to trigger)
base  = 12 / 15 / 20   for 3 / 4 / 5 reels
ways  = product of the per-reel Bonus counts
award = base x ways
```

From `freespin-trigger.json` — Bonus (`11`) per reel: **`[1, 2, 0, 2, 0]`**

| | |
|---|---|
| Reels holding a Bonus | 1, 2, 4 → **3 reels** → base **12** |
| Ways multiplier | 1 x 2 x 2 = **4** |
| **Award** | 12 x 4 = **48** |
| **Server awarded** | **48** ✓ |

Nothing else reproduces it:

| Reading | Gives | |
|---|---|---|
| **base-by-reels x ways** | **48** | ✓ the only match |
| By total symbol count (5 Bonuses → 20) | 20 | ✗ |
| Design doc's "Base Free Spins (3 reels): 15" x ways | 60 | ✗ |
| Base without the ways multiplier | 12 | ✗ |

So QA's "12, 15 or 20" was **right**, but it keys on **reels**, not on symbols — and the design
doc's ways-multiplier section was right in *shape* while wrong in its base (12, not 15). Its worked
example (`15 x 4 = 60`) should read `12 x 4 = 48`.

This also redefines the trigger condition: **3+ distinct reels**, not 3+ symbols. A grid with two
Bonuses stacked on one reel and one on another does **not** trigger. `base-win-two-lines` is a live
example — Bonus on reels 1 and 4 only, no trigger.

## 7. Ante Bet — **CONFIRMED**

`ante-bet-wild-win.json` is the first payload with ante genuinely engaged, and it settles the rule
the design doc contradicts itself about:

```
anteMultiplier : 3          <- QA's x3 is right; the doc's "increases the bet by 1.5x" is wrong
baseStake      : 0.6        <- UNCHANGED
stake          : 1.8        = baseStake x anteMultiplier
payout         : 4.5        = 1 x 150 x 0.6/20   <- derived from baseStake
winlose        : 2.7        = 4.5 - 1.8
```

**Ante multiplies what the player pays. It does not move the payout base.** That is the single most
confusable rule in this game, and the payload proves it decisively:

| Which base does the payout use? | Calculation | Result | |
|---|---|---|---|
| `baseStake` 0.6 — the payout base, unchanged | `1 x 150 x 0.6/20` | **4.5** | ✓ what the server paid |
| `stake` 1.8 — the ante-inflated deduction | `1 x 150 x 1.8/20` | 13.5 | ✗ |

So a correct implementation charges 3x and pays the same as it would have at the plain bet. The
player is buying a better *chance* (more Wilds — and note this very round landed three of them),
not a bigger *payout per line*.

`anteMultiplier: 0` reliably means ante off: `ante-labelled-spin.json` reports `0` with
`stake == baseStake`, so despite its name it is a plain round — captured before the toggle took
effect on the stake.

Asserted by `ante_stake_multiplier`, which now catches a wrong ratio, the doc's 1.5x, **and** an
implementation that inflates the payout base along with the stake.

## 8. What enforces each rule

Every rule above is an invariant in `scripts/scenario_engine.py`, driven by
`tests/chaos/verify_against_fixtures.py` (67 checks) and `tests/chaos/verify_chaos.py` (45). Each
is run on the real payload **and** on a mutated copy, so none of them can pass by accepting
everything.

| Contract rule | Invariant | Also catches |
|---|---|---|
| §5 credited = lines + scatter, capped | `payout_matches_paylines` | ignored/doubled multiplier, wrong divisor, phantom line |
| §4 the rate itself is the BO rate | `payline_rate_matches_paytable` | a self-consistent round paying a rate the paytable never had |
| §5 scatter = P1 ways x 10 | `scatter_pay_matches_p1_ways` | a miscounted ways, checked against the grid |
| §5 the max-win cap | `max_win_cap_applied` | credit above the cap; a mis-set `hasReachedMaxWin` either way |
| §5 gross covers the P1 win | `p1_ways_payout` | a ways win missing from `rawPayout` |
| §6 award = base(reels) x ways | `freespin_award` | both of the doc's readings, and an off-by-one |
| §6 retrigger adds on the same terms | `freespin_retrigger_adds` | a counter that rose with no Bonus on the grid |
| §7 ante multiplies stake, not base | `ante_stake_multiplier` | the doc's 1.5x, and an inflated payout base |
| §3 reel restrictions | `symbol_reel_restrictions` | P1 off reels 1/3/5; **Wild on reel 1** |
| §2 `winlose` = payout - stake | `winlose_consistent` | a stake that does not reconcile |
| ladder x1/x2/x5/x10 | `multiplier_in_ladder` | an off-ladder value |

**Two traps this contract sets for a test author**, both of which caught this suite during
development and are worth restating:

1. **Never compare a payout formula against `payout`.** It is the CAPPED figure. `p1_ways_payout`
   originally did, and produced a false red on the captured P1 round — the game grossed 12, credited
   10, and was behaving perfectly. Compare against `rawPayout`.
2. **A scatter has no `/betLines` divisor.** Applying the line formula to a scatter win
   under-reports it 20-fold.

## 9. Open questions

**Resolved since the first capture:** P1's id (`5`), how a scatter pays, and what `maxWin` is —
it is a currency cap that genuinely truncates, *not* an ante side effect. The earlier "`maxWin`
dropped 600 -> 10" note is explained: it is a BO config value, it changed between sessions, and a
later round hit it. Whether the drop to 10 was intentional is still worth asking the game team —
a 60x smaller cap is a big deal, and it is now demonstrably truncating real wins (a 12 paid as 10).

1. **Does `randomMultiplier` apply to the scatter half of a win?** See §5. Needs one P1 win on a
   x2-or-better spin.
2. **Ids `3` and `8`** = **Q** and **P4**. Strongly inferred (the ids run A=1, J=2, Q=3, K=4, then
   P1=5, P2=6, P3=7, P4=8, WILD=9, FreeSpin=11), but not yet named by a `winType`.
3. **Free-spin rounds themselves.** Still no in-bonus round captured, so `stake: 0` per free spin
   and the counter decrement remain assumed. **This is now the biggest single gap.** The export has
   already answered two parts of it: `FreeSpinGame.FreeSpins` is identical to the base table, so a
   **retrigger awards on the same terms**, and the in-bonus multiplier weights are much richer
   (x10 at weight 40 vs 5).
4. **Win tiers** — which base the tier compares against is still unconfirmed, and the ante round
   makes it concrete: it paid 4.5, which is **7.5x** `baseStake` (a Big Win) but **2.5x** the 1.8
   actually staked (a Medium Win).
5. **`scatterMultiplier` when P1 does not win.** It reads `0` on every non-P1 round, which is
   consistent, but no round has yet shown a scatter win *alongside* a line win — so whether the two
   halves genuinely sum is inferred from the formula rather than observed.
