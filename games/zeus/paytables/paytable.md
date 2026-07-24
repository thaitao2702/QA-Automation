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
> [`game-data-model.md`](./game-data-model.md) (grid, symbols, server response shapes),
> [`bridge-api.md`](./bridge-api.md) (observing outcomes via the bridge).

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

A **per-empty-cell coin-landing probability**, looked up by the board's
current coin count, that the server rolls **independently for every empty
cell** on each respin.

- **Outer key** = the **trigger coin count** — how many coins were on the
  board when this bonus started. Fixed for the whole bonus; it selects
  which row is active.
- **Inner key** = the **current coin count on the board** — the state at
  the start of this respin. As coins accumulate, the active inner key
  advances (`3 → 4 → …`).
- **Value** = a weight; `p = value / TotalCoinWeight` (typically out of
  `1000`) is the probability that **one empty cell** turns into a new coin
  on this respin. **Every** empty (non-coin) cell is rolled
  **independently** with this same `p`.

There is **no "next coin count" and no implicit "9" gap** — the value is a
per-cell probability, not a weight on a particular resulting total.

For the active trigger row, on a respin with `current` coins on the board
there are `9 − current` empty cells, each an independent trial at
`p = CoinWeight[trigger][current] / TotalCoinWeight`:

  `p (one empty cell gets a coin)      = CoinWeight[trigger][current] / TotalCoinWeight`
  `Expected new coins this respin      = (9 − current) × p`
  `P(no new coin — counter ticks down) = (1 − p) ^ (9 − current)`
  `P(instant fill to 9)                = p ^ (9 − current)`

```
CoinWeight = {
  "3": {              // bonus triggered with 3 coins…
    "3": 210,         //   board at 3: each of the 6 empty cells fills with p = 210/1000 = 21%
    "4": 200,         //   board at 4: each of the 5 empty cells, p = 20%
    "5": 120,         //   board at 5: p = 12%
    "6": 120,         //   board at 6: p = 12%
    "7": 100,         //   board at 7: p = 10%
    "8":  75,         //   board at 8 (1 empty cell): p = 7.5% it fills to 9
  },
  "4": { "4": 240, "5": 250, … },   // bonus triggered with 4 coins
  …
  "8": { "8": 750 },                 // triggered at 8: at 8 coins, p = 75% the last cell fills
}
TotalCoinWeight = 1000
```

> **Coin count and positions both fall out of the per-cell rolls.** Each
> empty cell is its own Bernoulli trial at probability `p`; how many new
> coins land, and where, is simply which of those independent rolls
> succeed — there is no separate server choice.

> **Inner keys `< trigger` are never used.** The board never holds fewer
> coins than it triggered with, so those states never occur; they carry
> weight `0` (or are absent) and are never looked up.

Each `CoinDistribution` entry has its own top-level `Weight`, and one
distribution is picked at the start of the bonus by that weight.

> **Source:** the per-empty-cell probability model above is confirmed by
> live dev runs — `FreeSpinGame.CoinWeight` all-`0` counts down, all-`1000`
> fills to 9 on the first respin. `Packages.CoinDistributions` uses the
> identical JSON shape and the same per-cell model.

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

Because `CoinWeight[trigger][current]` is the **per-empty-cell** coin
probability (§1.2), you steer the bonus by setting `p` to 0, 1, or
in-between — the **same value in every inner cell**, since the board walks
through the inner keys `trigger → … → 8` as coins land:

| Desired bonus behaviour | `CoinWeight` setting (active trigger row) |
|---|---|
| **No new coin — counter counts down to 0** | Set **every** inner cell to `0` → `p = 0` → no empty cell ever fills → the board stays at the trigger count → the free-spin counter decrements each respin until it hits 0. |
| **New coin on every respin — instant fill to 9** | Set **every** inner cell to `TotalCoinWeight` (e.g. `1000`) → `p = 1` → every empty cell fills on the first respin → the board jumps straight to 9 and the bonus ends. |
| **Bonus runs out of respins → payout** | Same as "no new coin" — all inner cells `0`. |
| **Gradual / realistic accumulation** | Set each inner cell between `0` and `TotalCoinWeight`; higher `p` = coins land faster. `p` may differ per current-count row (e.g. taper as the board fills, like the shipped values). |

> **This is the reverse of a "next-count distribution".** All-zero here is
> **count-down**, not fill-to-9; and all-`TotalCoinWeight` is **instant
> fill**, not "stay". To fill to 9 without touching respins, trigger with 9
> coins via `BaseGame.ReelDistributions` (§3.1) instead.

Inner cells with key `< trigger` are unreachable (the board never holds
fewer coins than it triggered with) and can be left `0`.

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
| `respin-no-coin.json` | `FreeSpinGame.CoinWeight` all cells `0` → `p = 0` → guaranteed count-down |
| `respin-add-coin.json` | `FreeSpinGame.CoinWeight` all cells `1000` → `p = 1` → instant fill to 9 |
| `coin-grand.json` | `CoinPrize` collapsed to `Multiplier: 500` → all coins land as GRAND |

Naming is illustrative — pick whatever convention fits the deployment
pipeline. The principle is: one file per behavioural axis you want to pin.

---

## 4. Mechanics notes

### 4.1 Per-respin coin count

The number of new coins on a respin is **how many of the `9 − current`
independent per-cell rolls succeed**, each at
`p = CoinWeight[trigger][current] / TotalCoinWeight` — expected
`(9 − current) × p`. Their positions are whichever empty cells hit. Both
the count and the placement are emergent from the per-cell rolls, not a
separate server draw.

### 4.2 Reaching 9 coins

Reaching 9 is not a special roll — it is just the case where every
remaining empty cell happened to hit on the same respin
(`P = p ^ (9 − current)`).

**Worked example** — bonus triggered with 8 coins, board currently at 8
(one empty cell, the only reachable state for trigger=8):

```json
"8": { "3": 0, "4": 0, "5": 0, "6": 0, "7": 0, "8": 750 }
```

`p = 750 / 1000 = 0.75`. With a single empty cell:

- 750 / 1000 = 75% → that cell fills → **9 coins, bonus ends**
- 250 / 1000 = 25% → no new coin → counter decrements

(Only when exactly one cell is empty does the per-cell probability line up
with a simple stay-vs-fill split; with more empty cells each is rolled
independently, so more than one coin can land at once.)

### 4.3 `ReelDistributionThreshold`

Whatever this rule gates is mooted by the neutralisation pattern in §3.1:
set every `ReelDistributions[].Weight` to the same value and any
threshold-based gating between sets becomes a no-op.

---