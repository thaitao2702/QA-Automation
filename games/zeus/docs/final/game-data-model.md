# Zeus Slot Game — Data Model Reference

> **Purpose:** How the 3×3 game grid is indexed, the shape of every server
> response the client consumes, and the spin state machine that classifies
> each round.
>
> **Audience:** Engineers reading bridge events, debugging spin outcomes,
> or building tools that inspect game state.
>
> **Companion docs:**
> [`paytable.md`](./paytable.md) (RNG config and weight tuning),
> [`bridge-api.md`](./bridge-api.md) (bridge methods & events),
> [`bridge-applicability.md`](./bridge-applicability.md) (action gating).

---

## 1. Grid coordinate system

### 1.1 Dimensions

- **3 columns × 3 rows = 9 cells.**
- **5 paylines.** Defined by [`Constants.ts:120-126`](assets/src/constants/Constants.ts#L120) as `PayLinesPosition: Map<number, number[]>` where each entry is `[rowInCol0, rowInCol1, rowInCol2]`.

| Payline | Rows | Visual |
|---|---|---|
| 1 | `[1, 1, 1]` | middle horizontal |
| 2 | `[0, 0, 0]` | **top** horizontal |
| 3 | `[2, 2, 2]` | **bottom** horizontal |
| 4 | `[0, 1, 2]` | top-left → bottom-right (`\`) |
| 5 | `[2, 1, 0]` | bottom-left → top-right (`/`) |

### 1.2 Axes and indexing — **row 0 = top, col 0 = left**

Verified across three independent position-calculation sites; all subtract
`row` from a constant, inverting the index relative to Cocos's Y-up axis:

| Site | Formula | Effect |
|---|---|---|
| [HoldSpinView.ts:285](assets/src/views/HoldSpinView.ts#L285) | `(col - 1) * w, (1 - row) * h` | row 0 → +h, row 2 → −h |
| [SlotMachineView.ts:749](assets/src/views/SlotMachineView.ts#L749) | `(col - 1) * w, (1 - row) * h` | same |
| [SlotMachineView.ts:90](assets/src/views/SlotMachineView.ts#L90) | `(col - floor(cols/2)) * w, (rows - row) * h` | same direction |

Cross-confirmed by the payline layout in §1.1 — payline 2 = `[0,0,0]` is the
**top** horizontal, the canonical "row 0 = top" indicator.

### 1.3 The locked-in mental model

```
                col 0       col 1       col 2
              (LEFT reel) (MID reel)  (RIGHT reel)
            ┌───────────┬───────────┬───────────┐
row 0  TOP  │  value[0] │  value[0] │  value[0] │   ← reel.value[0]
            ├───────────┼───────────┼───────────┤
row 1  MID  │  value[1] │  value[1] │  value[1] │
            ├───────────┼───────────┼───────────┤
row 2  BOT  │  value[2] │  value[2] │  value[2] │
            └───────────┴───────────┴───────────┘
```

- `col`: 0 (left) → 2 (right).
- `row`: 0 (top) → 2 (bottom).
- A **reel** = one column. Its three drawn symbols fill that column top-to-bottom.
- Visualized as x/y axes: x = `col`, y = `row` with the y-axis growing **downward** in index.

> **Server convention:** The data-flow chain (`mappingResultByBet` in
> [GameLogic.ts:205-251](assets/src/managers/GameLogic.ts#L205)) does not flip
> the matrix — the server is assumed to use the same `value[0]=top`
> convention. Worth a one-line confirmation with the math/server team if a
> coordinate mismatch is ever suspected.

---

## 2. Symbols

From [`Constants.ts`](assets/src/constants/Constants.ts) `SlotSymbol` enum.

| Symbol | ID | Notes |
|---|---|---|
| `ERROR` | -1 | invalid / hidden cell during early bonus respins |
| `A` | 1 | low |
| `J` | 2 | low (weight 0 in current paytable — effectively disabled) |
| `Q` | 3 | low |
| `K` | 4 | low |
| `P1` | 5 | premium — Athena |
| `P2` | 6 | premium — Helmet |
| `P3` | 7 | premium — Zeus |
| `P4` | 8 | premium — Shield |
| `WILD` | 9 | Golden Eagle — substitutes for all except COIN |
| `COIN` | 13 | Hold & Spin trigger; collects coin prize during bonus |
| `COIN_EMPTY` | -1 | empty slot visual during bonus respins |

`reel.value[row]` carries these IDs server-side.

---

## 3. Server response shapes

### 3.1 `BetResult` — top-level server response per spin

Returned from the SignalR `bet` / `buyfreespin` events. Stored on
`GameLogic.betResult` by the setter at
[GameLogic.ts:127](assets/src/managers/GameLogic.ts#L127).

| Field | Type | Meaning |
|---|---|---|
| `id`, `roundId`, `refId` | `string` | round identifiers |
| `playType` | `string` | `'NORMAL'` / `'buyBonus'` / etc. |
| `stake` | `number` | bet amount (0 during bonus respins) |
| `payout` | `number` | this spin's payout |
| `winlose` | `number` | `payout − stake` |
| `balance` | `number` | balance after settlement |
| `result.views` | `[{ id, value: number[] }, …]` | 3 reels × 3 symbol IDs — the board (`value[0]` = top of column) |
| `paylines` | `PayLines[]` | winning lines (see §3.4) |
| `coinPositions` | `CoinPositions[]` | coin cells (see §3.3) |
| `coinCount` | `number` | server's coin count (== `coinPositions.length`) |
| `freeSpinInfo` | `FreeSpinInfo \| null` | bonus state (see §3.5) |
| `betTime` / `processedTime` / `settledTime` | `number` | server timestamps |
| `betLines` | `number` | 5 |

### 3.2 `SpinResult` — client-derived view of a `BetResult`

Built by [`mappingResultByBet`](assets/src/managers/GameLogic.ts#L205) and
stored on `GameLogic.spinResult`. The bridge surfaces this in snapshots and
the `spin_result` event.

| Field | Source | Meaning |
|---|---|---|
| `state` | client (`getStateGame`) | one of `SpinState` (see §4) |
| `reels` | server `result.views[].value` | `number[][]` copy of the board |
| `paylines` | server, filtered `payline ≥ 0` | active winning lines |
| `coinPositions` | server, `mappingTypeCoin()` enriched | coin cells with tier `type` set |
| `coinCount` | server | total coins on board |
| `freeSpinInfo`, `freespins` | server | respins remaining etc. |
| `payout`, `stake`, `winlose`, `rawPayout` | server | |
| `typeWin` | client | `LOSE` / `SLIM` / `SMALL` / `MEDIUM` / `BIG_WIN` / `MEGA_WIN` / `ULTRA_WIN` |
| `persistenceInfo` | client (`getPersistenceInfo`) | 8-tier visual progression |
| `infoResultBonus` | client (`getInfoResultBonus`) | total-win popup data on bonus end |
| `promoResult` | server | promo round data (if applicable) |
| `hasReachedMaxWin` | server | max-payout cap reached |

### 3.3 `CoinPositions`

[`Interface.ts:78-84`](assets/src/constants/Interface.ts#L78)

```ts
interface CoinPositions {
    row: number;        // 0 (top) — 2 (bottom)
    col: number;        // 0 (left) — 2 (right)
    coinValue: number;  // cash value (= stake × multiplier)
    type: TYPE_COIN;    // tier — see below
    multiplier?: number;
}
```

`type` is derived client-side in `mappingTypeCoin()`
([GameLogic.ts:165-187](assets/src/managers/GameLogic.ts#L165)) by looking up
the coin's `multiplier` in the **coin paytable's multiplier list**
(see [`paytable.md` §1.4](./paytable.md)):

| `TYPE_COIN` | Value | Derivation |
|---|---|---|
| `EMPTY` | 0 | no coin |
| `NORMAL` | 1 | multiplier not in special list (default) |
| `MINOR` | 2 | multiplier at index 0 of coin paytable values |
| `MAJOR` | 3 | multiplier at index 1 |
| `GRAND` | 4 | multiplier at index 2 |

`mappingTypeCoin` also sorts the array by `(row, col)` ascending — so a coin
list arrives top-to-bottom, left-to-right.

### 3.4 `PayLines`

[`Interface.ts`](assets/src/constants/Interface.ts) — winning combinations
matched against `PayLinesPosition`.

```ts
interface PayLines {
    payline: number;      // 1–5; or -1 for the bonus "FreeSpin" placeholder
    symbols: number[];    // symbols along this line, e.g. [6, 6, 9]
    payoutRate: number;   // multiplier (×bet/5)
    winType: string;      // e.g. "P2-P2-WILD!"
}
```

### 3.5 `FreeSpinInfo`

[`Interface.ts:158-162`](assets/src/constants/Interface.ts#L158)

```ts
interface FreeSpinInfo {
    freespins: number;       // respins remaining; 3 → 0
    totalFreespin: number;   // cumulative free spins granted
    accumulateWin: number;   // running bonus payout total
}
```

`null` outside a bonus and on the terminal respin (signals "bonus ended,
`payout` is the final total").

---

## 4. SpinState machine

[`Interface.ts:149-156`](assets/src/constants/Interface.ts#L149) (numeric
enum; the bridge surfaces it as a string name via `getSpinState()`).

```
NORMAL ── 3+ coins land ──► START_BONUS ──► IN_BONUS ⇄ (re-spins)
                                                │
                                                ▼
                                            END_BONUS ──► NORMAL
```

Computed client-side by `getStateGame()`
([GameLogic.ts:335-362](assets/src/managers/GameLogic.ts#L335)) from
**current vs previous** `freeSpinInfo`:

| Previous | Current | → SpinState |
|---|---|---|
| no free spins | `freespins > 0` | `START_BONUS` |
| no free spins | `coinCount == 9` | `START_BONUS` (instant 9-coin trigger) |
| had free spins | `freeSpinInfo` null / `freespins == 0` / `coinCount ≥ 9` | `END_BONUS` |
| had free spins | ongoing | `IN_BONUS` |
| neither | normal | `NORMAL` |

> The server doesn't send `spinState` — it's a client-side derivation. The
> `spin_result` event includes it so consumers can segment a bonus episode
> from the event timeline without correlating two streams.

`PROMO` and `START_GAME` are separate flows (game-info pending state, promo
free-spins). They are not part of the Hold & Spin bonus described above.

---

## 5. Cross-references

| Doc | Topic |
|---|---|
| [`paytable.md`](./paytable.md) | RNG config, weight tuning, deterministic outcomes |
| [`bridge-api.md`](./bridge-api.md) | Bridge actions, queries, event types |
| [`bridge-api.md` §5](./bridge-api.md) | `spin_result` payload, bonus-observation recipe |
| [`bridge-applicability.md`](./bridge-applicability.md) | Action gating (`canDo`, `waitUntilApplicable`) |
| [`assets/src/constants/Constants.ts`](assets/src/constants/Constants.ts) | `SlotSymbol`, `PayLinesPosition`, `GameType` |
| [`assets/src/constants/Interface.ts`](assets/src/constants/Interface.ts) | `BetResult`, `SpinResult`, `CoinPositions`, `FreeSpinInfo`, `SpinState`, `TYPE_COIN` |
| [`assets/src/managers/GameLogic.ts`](assets/src/managers/GameLogic.ts) | `mappingResultByBet`, `getStateGame`, `mappingTypeCoin` |
| [`assets/src/views/SlotMachineView.ts`](assets/src/views/SlotMachineView.ts) / [`HoldSpinView.ts`](assets/src/views/HoldSpinView.ts) | Grid rendering — row/col convention |
