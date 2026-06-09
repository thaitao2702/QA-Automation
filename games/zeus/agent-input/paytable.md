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

## 0. TL;DR — The picture-book version

> Skim this first. If you understand it, you understand 80% of the rest.
> The numbered sections below are the precise reference.

### The marble-bag rule

Each wheel of the slot machine has its own **bag of marbles**, and every
marble is painted with a picture (`A`, `COIN`, `WILD`, `P1`, …). To fill a
square, the game **reaches into that wheel's bag and grabs one marble** —
whichever picture it pulled out is what lands on that square. It does this
**3 times per wheel** (once for each row), and each grab is independent
(it puts the marble back before grabbing the next).

Some marbles are more common than others. The number next to each picture
in `paytable.json` (called **`Weight`**) is *how many marbles of that
color are in the bag*. More weight = more marbles = more likely to be
picked. Weight `0` means "this picture isn't in the bag at all — it can
never appear."

### Two completely separate marble-bag configs

`paytable.json` has **two separate bag configurations**, used in different
moments. Changing one does **not** affect the other.

| Config | Used when | Controls |
|---|---|---|
| `BaseGame.ReelDistributions` | normal spins | whether and how the bonus **triggers** |
| `FreeSpinGame.CoinDistributions` | inside the bonus | what coins land on each **re-spin** |
| `Packages[].CoinDistributions` | Buy Bonus | same as FreeSpinGame, but per package |

> **Important:** to make a setup behave a certain way for both the trigger
> and the bonus, you usually need to tune both `BaseGame` and `FreeSpinGame`
> (or the relevant `Packages` entry).

### Weight collapsing — making outcomes deterministic

You convert a random outcome into a guaranteed one by **putting only one
color of marble in the bag**. Set the picture you want to a positive
weight, set **every other** picture to `0`. Now every grab is guaranteed
to be that picture.

For a whole wheel, this means **all 3 squares in that wheel become the
same picture** — because the 3 grabs share one bag. So if you make COIN
the only marble in a wheel:

| Wheels collapsed to COIN-only | Coins on the board |
|---|---|
| 1 wheel | **exactly 3 coins** (one full column) |
| 2 wheels | **exactly 6 coins** |
| 3 wheels | **exactly 9 coins** (full board) |

You **cannot** force exactly 4, 5, 7, or 8 coins, because each wheel only
has one bag shared by its 3 squares — you can only collapse a whole column
at a time.

A different table — `FreeSpinGame.CoinDistributions.CoinWeight` — runs a
**weighted Yes/No flip** on each respin: "does at least one new coin
appear this respin?" The weights live in a `[triggerCount][currentCount]`
table; the probability of "yes" is `weight / TotalCoinWeight`. Collapse to
**0** to force "no new coin" every respin (counter counts down). Collapse
to **`TotalCoinWeight`** to force "new coin every respin" (counter resets
each time). The **number** of coins that land when "yes" wins is **server-
random**, not weight-controlled. See §3.2.

### Coin jackpot tier — a separate marble bag

When a coin lands, **another** weighted draw decides its **multiplier**
(`CoinPrize`). The multiplier determines whether the coin reads as
`NORMAL` / `MINOR` / `MAJOR` / `GRAND` in the bridge's `coinPositions`.
Collapse the `CoinPrize` bag to force a specific tier.

### The one-line cheat sheet

> **Trigger the bonus:** raise COIN weight in `BaseGame.ReelDistributions`.
> **Force exact 3 / 6 / 9 coins:** collapse 1 / 2 / 3 reels to COIN-only.
> **Control respins:** collapse `FreeSpinGame.CoinDistributions.CoinWeight`.
> **Force a coin tier:** collapse `CoinPrize` to one `Multiplier`.

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

A **weighted Yes/No table** the server consults on every respin to answer:
*does at least one new coin land this respin?* It is **not** a state-
transition matrix.

- **Outer key** = the **trigger coin count** — how many coins were on the
  board when this bonus started. Fixed for the whole bonus.
- **Inner key** = the **current coin count** on the board right now (rises
  from the trigger count upward as new coins land).
- **Value** = the weight, out of `TotalCoinWeight` (typically `1000`),
  that **a new coin appears** this respin.

  `P(new coin) = value / TotalCoinWeight`
  `P(no new coin, counter decrements) = 1 − P(new coin)`

```
CoinWeight = {
  "3": {              // bonus triggered by exactly 3 coins
    "3": 210,         //   currently 3 on screen → 210/1000 = 21% chance new coin lands
    "4": 200,         //   currently 4 → 20%
    "5": 120,         //   currently 5 → 12%
    "6": 120,         //   currently 6 → 12%
    "7": 100,         //   currently 7 → 10%
    "8":  75,         //   currently 8 → 7.5% (next coin would fill to 9)
  },
  "4": { "4": 240, "5": 250, … },   // bonus triggered by 4 coins
  …
  "8": { "8": 750 },                 // bonus triggered by 8 coins
}
TotalCoinWeight = 1000
```

> **Note — count of coins is server-random.** When the flip says "yes,
> new coin," the **number** of coins added that respin (1, 2, …) is
> decided by separate server logic that is not exposed through
> `CoinWeight`. `CoinWeight` only controls the binary appears-or-not flip.

> **Unreachable cells.** Entries where `current < trigger` (e.g.
> `["5"]["3"]`) are unreachable, since coins only accumulate during a
> bonus. The JSON carries them at weight `0` as a no-op.

Each `CoinDistribution` entry has its own top-level `Weight`, and one
distribution is picked at the start of the bonus by that weight.

> **Source:** semantics confirmed for `Packages.CoinDistributions`.
> `FreeSpinGame.CoinDistributions` uses the identical JSON shape and is
> expected to follow the same Yes/No-with-server-random-count model.

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

Because `CoinWeight` is a Yes/No coin-flip per respin (not a transition
matrix), saturate or zero the relevant cells:

| Desired bonus behaviour | `CoinWeight` collapse |
|---|---|
| **No new coin per respin** — counter counts down to 0 | For every `[trigger][current]` cell, set the value to `0` → `P(new coin) = 0` → counter decrements every respin → bonus ends by running out of spins. |
| **New coin on every respin** — counter resets each time | For every reachable `[trigger][current]` cell, set the value to `TotalCoinWeight` → `P(new coin) = 1` → at least one new coin lands every respin → counter resets to 3. |
| **Bonus runs out of respins → payout** | Same as "no new coin" — guarantee count-down. |
| **Fill to 9 (instant-end)** | Not fully forceable from `CoinWeight` alone, because the *number* of coins per "yes" is server-random. The reliable path is to trigger with 9 coins via `BaseGame.ReelDistributions` (§3.1), which short-circuits the respin loop. |

`[trigger][current]` cells where `current < trigger` are unreachable and
can be left alone.

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

## 4. Open items to confirm with the math/server team

1. Does the **server use `value[0] = top`** in its `result.views[col].value`
   arrays? See [`game-data-model.md` §1.2](./game-data-model.md).
2. Does base-game generation allow **multiple COIN per reel**? §3.1
   assumes yes (a COIN-exclusive reel yields 3 coins). If capped at
   1/reel, the granularity becomes 0/1/2/3 instead, and the bonus
   produces the higher counts rather than the trigger spin.
3. **Count of new coins when `CoinWeight` rolls "yes."** `CoinWeight`
   controls the binary appears-or-not flip per respin; how many coins
   actually land (1, 2, …) is server-controlled and not exposed by this
   config. Worth confirming whether the count is uniform, weighted, or
   capped (e.g. always +1 per "yes").
4. **9-coin fill path during a bonus.** Since `CoinWeight`'s inner keys
   top out at `"8"`, the transition from 8 → 9 (the instant-end fill)
   must be driven by the same "yes → server picks a count" logic, where
   the picked count happens to be 1 from current=8. Worth confirming
   with the math team.
5. `ReelDistributionThreshold` rule — does it gate the set-pick by some
   condition, or is the distribution purely weight-based?

---

## 5. Cross-references

| Doc | Topic |
|---|---|
| [`game-data-model.md`](./game-data-model.md) | Grid coordinates, server response shapes, SpinState |
| [`bridge-api.md`](./bridge-api.md) | Bridge actions, queries, event types |
| [`bridge-api.md` §5](./bridge-api.md) | `spin_result` payload, bonus-observation recipe |
| [`bridge-applicability.md`](./bridge-applicability.md) | Action gating (`canDo`, `waitUntilApplicable`) |
| [`paytable.json`](./paytable.json) | Live paytable file described in §1 |
| [`assets/src/views/DevMode/DevModeManager.ts`](assets/src/views/DevMode/DevModeManager.ts) | Client-side reel generator (`generateReelData`) — confirms the sampling model |
| [`assets/src/managers/GameLogic.ts`](assets/src/managers/GameLogic.ts) | `mappingTypeCoin` — coin tier classification from `CoinPrize` multipliers |
