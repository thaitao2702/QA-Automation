# Zeus Slot Game — Paytable Reference

> **Purpose:** How `paytable.json` is structured, how the server samples from
> it to build each board and bonus respin, and how to tune weights to force
> deterministic outcomes for testing or debugging.
>
> **Audience:** Math/server engineers tuning RNG weights, QA engineers
> authoring deterministic test paytables, and anyone reverse-engineering
> bonus probabilities.
>
> **Companion docs:**
> [`paytable.json`](./paytable.json) (default paytable).

---

## 1. File structure

The server's math config. Top-level keys: `BaseGame`, `FreeSpinGame`,
`Packages`, `SymbolMultipliers`.

### 1.1 `BaseGame`

Controls **base-game spins** (including whether/how a bonus triggers).

#### `Paylines: string[]`
Lookup table of payout strings (`"P1,P1,P1,200.00"` etc.). Not consumed
directly by the client — used by the server math.

#### `ReelDistributions[]` — RNG weights

The key knob for tuning trigger probability.

```
BaseGame.ReelDistributions = [
  { Id: 1, Weight: 30, Reels: [
      { Id: 1, Symbols: [ { Name, Weight }, … ] },   // column 0 weights
      { Id: 2, Symbols: [ … ] },                      // column 1 weights
      { Id: 3, Symbols: [ … ] },                      // column 2 weights
  ]},
  { Id: 2, Weight: 30, Reels: […] },
  { Id: 3, Weight: 40, Reels: […] },
]
```

**Two-level RNG**:

1. The server picks **which distribution set** by top-level `Weight`
   (30 / 30 / 40 → 100 total).
2. Within that set, for each of the 3 reels (columns), each of the reel's
   **3 cells** is drawn **independently** from the per-reel `Symbols`
   weighted pool.

> **Granularity:** weights are **per reel**, not per cell. All 3 cells in a
> column share the same weighted pool. There is no per-`(col,row)` weight
> in this schema. Confirmed by the client's own grid generator
> [`DevModeManager.generateReelData()`](assets/src/views/DevMode/DevModeManager.ts#L38)
> which draws 3 independent symbols per reel.

Other fields:
- `ReelDistributionThreshold` — selection rule across the 3 sets.
- `EnableWildExpanding` — wild-expansion flag (currently `false`).

#### `CoinPrize[]`

Weighted draw of the **multiplier** a coin lands with. Drives the
NORMAL/MINOR/MAJOR/GRAND classification (see §1.4).

```
CoinPrize = [
  { Multiplier: 1,   Weight: 2625 },
  { Multiplier: 2,   Weight: 2500 },
  …
  { Multiplier: 500, Weight: 25 },
]
```

### 1.2 `FreeSpinGame`

Controls **Hold & Spin respins** — what happens *after* a bonus is triggered.
**Changing `BaseGame.ReelDistributions` does not affect respins.**

#### `CoinDistributions[].CoinWeight`

A **weighted distribution over the next coin count** the server samples
on every respin.

- **Outer key** = the **trigger coin count** — how many coins were on
  the board when this bonus started. Fixed for the whole bonus.
- **Inner key** = a candidate **next coin count** — the resulting count
  on the board after this respin.
- **Value** = the weight of that next-count outcome, out of
  `TotalCoinWeight` (typically `1000`).
- **Implicit "9" key** = `TotalCoinWeight − sum(inner weights)`. The
  residual weight maps to "next = 9 → instant fill, bonus ends." The
  structure carries inner keys only up to `"8"`; the 9-outcome is
  encoded exclusively in this gap and never appears in the JSON.

For the active trigger row, with current count `current`:

  `P(stay at current)   = CoinWeight[trigger][current]      / TotalCoinWeight`
  `P(next = N | N > current)
                        = CoinWeight[trigger][N]            / TotalCoinWeight`
  `P(fill to 9)         = (TotalCoinWeight − sum(inner))    / TotalCoinWeight`
  `P(any new coin)      = 1 − P(stay at current)`

```
CoinWeight = {
  "3": {              // bonus triggered with 3 coins
    "3": 210,         //   21% — next = 3 (stay, no new coin)
    "4": 200,         //   20% — next = 4 (1 new coin)
    "5": 120,         //   12% — next = 5
    "6": 120,         //   12% — next = 6
    "7": 100,         //   10% — next = 7
    "8":  75,         //   7.5% — next = 8
  },                  //   implicit gap: 175 → 17.5% fill to 9
  "4": { "4": 240, "5": 250, … },   // bonus triggered with 4 coins
  …
  "8": { "8": 750 },                 // 75% stay at 8, 25% fill to 9
}
TotalCoinWeight = 1000
```

> **Placement of new coins is server-random.** When the sampled
> `next > current`, the **positions** of the `next − current` new coins
> across the empty cells are decided by the server and not exposed
> through this config. Treat the placement as random.

> **Unreachable cells.** Entries where the inner key is less than
> `trigger` (e.g. `["5"]["3"]`) carry weight `0`. They represent
> impossible "next < trigger" outcomes, since coins only accumulate
> during a bonus.

Each `CoinDistribution` entry has its own top-level `Weight`, and one
distribution is picked at the start of the bonus by that weight.

> **Source:** semantics confirmed for `Packages.CoinDistributions`.
> `FreeSpinGame.CoinDistributions` uses the identical JSON shape and is
> expected to follow the same next-count-distribution model.

#### `CoinPrize[]`

Bonus-context version of §1.1's `CoinPrize`. Different weights — bonus
multipliers skew higher.

### 1.3 `Packages` — Buy Bonus

Each entry is a purchasable bonus package.

| Field | Meaning |
|---|---|
| `Id` | package identifier |
| `Price` | bet multiplier (total cost = `bet × Price`; current package: 60) |
| `NoOfFreeSpins` | starting respins (3) |
| `Scatters` | reserved scatter count (currently `0`) |
| `Name` | identifier (`"packagea"`) |
| `CoinDistributions` | same shape as `FreeSpinGame.CoinDistributions` — drives this package's respins. The bonus's **trigger coin count** (returned in the initial Buy Bonus response, typically 3) picks which `CoinWeight` outer-key row the server uses for the duration of that bonus. |
| `CoinPrize` | bonus-context multiplier weights |

### 1.4 `SymbolMultipliers`

Per-symbol payout values, indexed by **match count** (for 3-of-a-kind):

```
{ Name: "WILD!", Value: [0, 0, 250] }   // 3-WILD! pays 250×
{ Name: "P1",    Value: [0, 0, 200] }
…
{ Name: "COIN",  Value: [50, 100, 500] }  // the COIN paytable
```

For non-COIN symbols `Value[0]` and `Value[1]` are 0 (no 1- or 2-of-a-kind
payout). `COIN`'s `Value` is the special "jackpot tier" list used by
`mappingTypeCoin()` to classify each coin:

| `coin.multiplier` | Matched against | → `type` |
|---|---|---|
| `50` | `Value[0]` | `MINOR` |
| `100` | `Value[1]` | `MAJOR` |
| `500` | `Value[2]` | `GRAND` |
| anything else | not in the list | `NORMAL` |

So if you tune `BaseGame.CoinPrize` (or `FreeSpinGame.CoinPrize`) to force a
particular multiplier, that coin's `type` in `coinPositions` will reflect
the matching tier.

---

## 2. Reel sampling model (how a base-game board is drawn)

Step-by-step the server (or `DevModeManager` client-side) builds a board:

1. **Pick a `ReelDistribution` set** from `BaseGame.ReelDistributions[]`
   weighted by each set's top-level `Weight`.
2. **For each of the 3 reels** in that set:
   a. Read the reel's `Symbols` weight pool.
   b. Draw a symbol weighted-random — independently — **3 times**, one
      per row.
3. Assemble `result.views[col].value = [topSymbol, midSymbol, botSymbol]`.
4. The server then scans for COINs to populate `coinPositions` and runs
   payline matching for `paylines`.

Key consequence: **the 3 cells in a reel are i.i.d. draws from one pool.**

---

## 3. Weight collapsing for deterministic outcomes

To convert weighted randomness into a fixed outcome: set **one** weight
positive in a competing list and **every other weight to 0**. Wherever the
server draws weighted-random, a single non-zero weight = guaranteed
outcome.

### 3.1 `BaseGame` — controlling the trigger

Per-reel collapse gives **column-granular** determinism, because the 3
cells in a reel share one pool:

| Per-reel COIN setting | Each cell in that column | Coins in column |
|---|---|---|
| `COIN: 0` (all others positive) | never COIN | **0** (deterministic) |
| `COIN: N` (all others `0`) | always COIN | **3** (deterministic) |
| anything in between | i.i.d. chance | 0–3 (probabilistic) |

Summing across the 3 reels, the **deterministic grid totals** are:

| Reels with COIN-exclusive | Total coins | Outcome |
|---|---|---|
| 0 | **0** | no trigger |
| 1 | **3** | minimal-coin trigger |
| 2 | **6** | mid-range trigger |
| 3 | **9** | full-board instant-fill trigger |

**Counts 4, 5, 7, 8 are not weight-forceable** without per-`(col,row)`
weights, which this schema doesn't expose.

When using a 1-column-COIN setup, the deterministic coin positions are
exactly:

```
{ col: 0, row: 0 }   // top-left
{ col: 0, row: 1 }   // middle-left
{ col: 0, row: 2 }   // bottom-left
```

(Or whichever reel `Id` you collapsed — `Reels[Id=1]` is column 0.)

Apply the collapse to **all 3 `ReelDistributions` sets** (or set all
top-level `Weight` to the same value as the collapsed set) so the
distribution-set pick doesn't reintroduce variance.

### 3.2 `FreeSpinGame` / `Packages` — controlling respins

Because `CoinWeight` is a distribution over next coin counts (§2), every
respin's outcome is determined by routing weight into a single
inner-key slot — or into the implicit "9" gap:

| Desired bonus behaviour | `CoinWeight` collapse |
|---|---|
| **No new coin per respin** — counter counts down to 0 | In the active trigger row, set `inner[trigger] = TotalCoinWeight` and every other inner cell to `0` → `P(next = trigger) = 1` → board stays at the trigger count → counter decrements every respin → bonus ends by running out of spins. |
| **New coin on every respin** — counter resets each time | In the active trigger row, set `inner[trigger] = 0` (no "stay" outcome) and route `TotalCoinWeight` into one reachable inner key `N > trigger` (or distribute across several) → every respin advances → counter resets. To avoid early fill, keep weight off the implicit "9" gap by ensuring `sum(inner) = TotalCoinWeight`. |
| **Bonus runs out of respins → payout** | Same as "no new coin" — guarantee count-down. |
| **Fill to 9 (instant-end)** | In the active trigger row, set every inner cell to `0` while keeping `TotalCoinWeight > 0`. All weight collapses into the implicit "9" outcome (§2) → every respin instantly fills. Alternatively, trigger with 9 coins via `BaseGame.ReelDistributions` (§3.1) to short-circuit the respin loop entirely. |

Inner cells with key `< trigger` carry weight `0` in the shipped JSON
(unreachable "next < trigger" outcomes) and can be left alone.

If `Packages` is in play (Buy Bonus), apply the same collapse to each
package's `CoinDistributions[].CoinWeight`. The bonus's trigger count
selects which outer row the server uses for the duration of that bonus.

### 3.3 `CoinPrize` — controlling jackpot tier

To force a specific `type` (MINOR/MAJOR/GRAND) on every coin: in the
relevant `CoinPrize` (BaseGame, FreeSpinGame, or a Package), set the
target `Multiplier` weight positive and zero the rest. Combined with
`SymbolMultipliers.COIN.Value`, this pins `coin.type` predictably.

| Goal | `CoinPrize` collapse |
|---|---|
| All coins land as `MINOR` | leave only `Multiplier: 50` positive |
| All coins land as `MAJOR` | leave only `Multiplier: 100` positive |
| All coins land as `GRAND` | leave only `Multiplier: 500` positive |
| All coins land as `NORMAL` | leave only a multiplier *not* in `SymbolMultipliers.COIN.Value` (e.g. `1`, `2`, `3`) |

### 3.4 Practical layout — dedicated variant files

When automating multiple scenarios it's cleaner to maintain a small set
of named variant paytables and swap them per scenario, rather than mutate
one file ad-hoc.

| File | What it forces |
|---|---|
| `coins-3.json` | 1 column COIN-exclusive → trigger with exactly 3 coins |
| `coins-6.json` | 2 columns COIN-exclusive → trigger with exactly 6 coins |
| `coins-9.json` | all 3 columns COIN-exclusive → instant 9-coin fill |
| `respin-no-coin.json` | `FreeSpinGame.CoinWeight` collapsed to "stay" → guaranteed count-down |
| `respin-add-coin.json` | `FreeSpinGame.CoinWeight` collapsed to "+1" → new coin on every respin |
| `coin-grand.json` | `CoinPrize` collapsed to `Multiplier: 500` → all coins land as GRAND |

Naming is illustrative — pick whatever convention fits the deployment
pipeline. The principle is: one file per behavioural axis you want to pin.

---

## 4. Mechanics notes

### 4.1 Per-respin coin count

When a respin lands at least one new coin, the exact number is
server-decided and not exposed by this config. Treat as random for
test-generation purposes.

### 4.2 9-coin fill is deterministic from `CoinWeight`

The 8 → 9 instant-fill transition is encoded as the **weight gap** between
`sum(CoinWeight[trigger]_inner)` and `TotalCoinWeight`; there is no
separate server-side roll for it.

**Worked example** — bonus triggered with 8 coins, current count 8 (the
only reachable cell for trigger=8):

```json
"8": { "3": 0, "4": 0, "5": 0, "6": 0, "7": 0, "8": 750 }
```

`TotalCoinWeight = 1000`. Inner weights sum to 750, so 250 weight is
implicitly allocated to the "fill to 9" outcome:

- 750 / 1000 = 75% → no new coin, counter decrements
- 250 / 1000 = 25% → fill to 9, bonus ends instantly

### 4.3 `ReelDistributionThreshold`

Whatever this rule gates is mooted by the neutralisation pattern in §3.1:
set every `ReelDistributions[].Weight` to the same value and any
threshold-based gating between sets becomes a no-op.

---