# Joker: Where's My Banana — BET response reference

The server's BET payload: the contract the whole framework drives on. Scenarios read game LOGIC from
here and nowhere else, so this is the file to check before writing an assert, an invariant, or
anything that counts a symbol.

* What the bridge must expose → `docs/game-bridge-contract.md`
* What *this* bridge does (functions, popups, buttons, snapshot) → `games/joker/docs/raw/bridge-api.md`
* The expanding-wild feature, in detail → `games/joker/expanding-wild.md`

## Captured samples

`fixtures/bet-responses/` (captured 2026-09-18):

| File | What it is |
|---|---|
| `base-win-three-lines.json` | base spin, **three** paying lines (rates 15 + 15 + 20), stake 0.6, paid 1.5 |
| `base-no-win.json` | base spin, no lines, paid 0, `winlose: -0.6` |
| `freespin-wild-expand-terminal.json` | the **last** free spin of a 12-spin bonus, with a **wild expansion on reel 4**, bonus total 39.6 |
| `buybonus-purchase.json` | the **Buy Bonus purchase** round (captured 2026-09-21): stake 400 at a bet of 4.00, 20 spins awarded, an EMPTY grid — see §9 |

Every rule below was computed from those captures, not read off a spec. **Two shapes are still
missing** and the rules that depend on them are marked ⚠ UNVERIFIED — see §7.

---

## 1. Envelope — and the double nesting

```jsonc
{ "status": 1, "code": 0, "msg": "", "timestamp": 1789727098075,
  "result": {                                  // <- the ROUND (this is what Bet.raw holds)
      "result": { "rawViews": [...], "views": [...] },   // <- the REELS, nested again
      "stake": 0.6, "payout": 1.5, "balance": 16110.25, ... } }
```

`result.result` holding the grid is the same double nesting Chaos has. A profile that reads
`payout` off the envelope finds nothing and every round parses as a zero-payout loss.

## 2. The reels — `rawViews` and `views`

Five entries, one per reel, `{id, value}` where `id` is the 1-based reel and `value` is that reel's
three cells **top to bottom**. So the grid is column-major: `views[i].value[row]`.

| | |
|---|---|
| `rawViews` | the reels **as they landed**, before any expansion |
| `views` | the reels **after** the expanding wild is applied — what the player ends up seeing |

**They are equal on every base spin** in the captures, and differ only where a wild expanded:

```
freespin-wild-expand-terminal, reel 4:   rawViews [2, 9, 8]  ->  views [9, 9, 9]
```

That diff IS the feature, and it is the only reliable way to assert it — see
`games/joker/expanding-wild.md`.

## 3. The money, verified

```
payout  = Σ payoutRate  ×  (stake / betLines)          # betLines is 20 (fixed)
winlose = payout − stake
```

`base-win-three-lines`: rates `15 + 15 + 20 = 50`, line bet `0.6 / 20 = 0.03`, so
`50 × 0.03 = 1.5` — exactly `payout`. `winlose` is `1.5 − 0.6 = 0.9`. Both hold on all three
captures.

⚠ **`multiplier` is NOT a payout factor.** The winning capture carries `multiplier: 2.5` while its
payout matches the formula **without** it (`1.5`, not `3.75`). Do not multiply by it, and do not
assert a win tier from it until someone says what it is for. `luckyMultiplier` is the free-spin
multiplier (§5); `scatterMultiplier` and `anteMultiplier` are `0` in every capture.

**`balance` is the authoritative post-round figure** — the framework's `conservation` invariant reads
it, never a client readout.

## 4. `paylines[]` — and what `winType` does NOT tell you

```jsonc
{ "symbols": [4, 9, 4, 8, 4], "payline": 5, "payoutRate": 15, "winType": "K-WILD!-K-P4-K" }
```

* `symbols` and `winType` describe **the whole line, all five reels** — not the winning run.
* The win is the **leading run from reel 1**: `K-WILD!-K` is three K's (the wild substitutes), and
  `P4` on reel 4 is where it stops. The 5-symbol string is decoration.
* **`payoutRate` is the authority for what was won.** Never infer the match length by counting
  segments in `winType`.

Symbol ids, read off the two together (the only mapping these captures prove):

| id | symbol | how it is known |
|---|---|---|
| 1 | A | `[1,9,1,8,4]` ↔ `A-WILD!-A-P4-K` |
| 4 | K | `[4,9,4,8,4]` ↔ `K-WILD!-K-P4-K` |
| 8 | P4 | same two lines |
| 9 | **WILD!** | same, and it is the id that expands in §2 |
| 12 | ⚠ unknown | appears mid-reel in the free spin (`[1,12,1]`). **Likely BONUS or BANANA** — the two symbols the help says a wild may not cover — but nothing in these captures proves which |

`payline` is the line index (5, 13, 15 here). The bridge doc notes `payline: -1` marks a
scatter / free-spin entry; no capture shows one yet.

## 5. Free spins

| field | on a free spin |
|---|---|
| `stake` | **0** — a free spin costs nothing, while `baseStake` stays 0.6 |
| `id` | **this spin** |
| `roundId` / `refId` | **the BONUS** — they stay fixed for every spin of it |
| `freeSpins` | **remaining**, not a loop counter |
| `totalFreeSpin` | awarded total (12 in the capture) |
| `totalFreeSpinPayout` / `accumulatedWinAmount` | the bonus's running total (both 39.6 on the terminal spin) |
| `luckyMultiplier` | **3** — the multiplier in force for the bonus (help: "all spins come with a x3 multiplier during the Free Spins") |

⚠ **`id` ≠ `roundId` is the tell that you are inside a bonus.** On both base captures all three ids
are equal; on the free spin `id: 940351469238878209` while `roundId`/`refId` are
`940350582730784769`. **A test that correlates rounds by `roundId` will see every free spin of a
bonus as the same round** — correlate by `id`, and use `roundId` to group a bonus.

## 6. State and the end of a bonus

`gameState`: `1` Base · `2` Free · `3` BuyBonus/LuckySpin · `4` Promo.

⚠ The terminal free spin in the capture reports **`gameState: 1` with `freeSpins: 0`** — it has
already returned to base. That is why the framework's terminal rule is `freeSpins == 0` **OR**
`gameState == base`, never the counter alone.

## 7. ⚠ What is NOT captured yet — do not guess these

1. **A real free-spin TRIGGER.** The payload supplied as "a bet that triggers free spins"
   (`base-no-win.json`) awards nothing: `freeSpins: 0`, `totalFreeSpin: 0`, `gameState: 1`,
   `paylines: []`, `winlose: -0.6`. Its `roundId` (`…476497453057`) is also **not** the bonus the
   free-spin capture belongs to (`…582730784769`), so the two are unrelated rounds. Either the award
   arrives on a later response, or that spin simply did not trigger. **Capture the round that awards
   the spins** before writing any trigger rule — `is_trigger` for this game depends on it.
   ⚠ It does NOT block the forced paytable, which the first write-up said it did. `joker-fs-easy`
   is derived from `paytables/base.json` and the trigger SYMBOL (`FreeSpin!`, which the paytable
   names), not from a captured response — see `games/joker/paytables/README.md`. What the capture
   is still owed for is the shape of the awarding round: which fields carry the count, and whether
   the award is multiplied by ways the way Chaos's is.
2. ~~A Buy Bonus round.~~ **CAPTURED 2026-09-21** — `buybonus-purchase.json`. It answered all
   three open questions and contradicted two assumptions; the whole of it is §9 below.
3. **A max-win truncation.** `hasReachedMaxWin: false` and `maxWin: 0` everywhere, so the cap's
   shape is unverified on this game.

Also unexplained, and worth asking the game team: `multiplier` (§3), `canBuyExtension`,
`extensionPhase`, `originalTotalFreeSpins*`, `extensionTotalFreeSpins*` — all zero/false in every
capture, so they describe a feature none of these rounds exercised.

## 9. The Buy Bonus purchase round

`fixtures/bet-responses/buybonus-purchase.json`, captured 2026-09-21 at a bet of **4.00**. It is the
round that lands after `buyBonus:confirm`, before the wheel and before any free spin.

```jsonc
{ "freeSpins": 20, "luckyMultiplier": 0, "paylines": [],
  "id": "…219457", "refId": "…219457", "roundId": "…608065",
  "stake": 400, "payout": 0, "winlose": -400, "balance": 23948.65, "betLines": 20,
  "result": { "rawViews": [ {"id":1,"value":[-1,-1,-1]}, … ], "views": [ … same … ] },
  "freeSpinPackage": { "id": 0, "name": "packagea", "freeSpins": 20, "price": 100 } }
```

### ⚠ `totalFreeSpin` IS ABSENT — the award is in `freeSpins`

The key is **not present at all**. A natural trigger and every free spin of a natural bonus carry it
(the free-spin capture reads `totalFreeSpin: 12`); a purchase does not, and puts the award in
`freeSpins` — which on every other round means REMAINING.

That broke two things on the first live run:

| | |
|---|---|
| `{facts.awarded}` read **0**, so JK-63's `awarded == package_spins` failed as `0 == 20` | the framework, not the game |
| `is_trigger()` answered **False** on the one round that unarguably awards the bonus | same cause |

`JokerBanana.free_spins()` now folds it in, **gated on `freeSpinPackage` being present**. The gate
matters: `freeSpins` is remaining, so a blanket alias would report a mid-bonus round with 5 left as
"5 awarded" and make `is_trigger` true for every round of every bonus. `freeSpinPackage` appears
only on a purchase, which is exactly the round whose remaining count IS its award.

### ⚠ TWO DIFFERENT THINGS ARE CALLED "price"

| where | value here | what it is |
|---|---|---|
| `stake` on the round | **400** | MONEY — what was actually charged |
| `buyBonusSelectedPrice` (snapshot) | 400 | MONEY — what the panel quoted |
| `freeSpinPackage.price` | **100** | the **MULTIPLIER** — `Packages[0].BuyMultiplier`, not currency |

400 = 4.00 × 100, so the arithmetic holds and `price_ratio == 100` is the assert that expresses it.
Asserting `paid_stake == freeSpinPackage.price` would compare 400 against 100 and read as a defect.

### ⚠ THERE IS NO GRID — both views are `-1`

`rawViews` and `views` are five reels of `[-1, -1, -1]`, identical. The server sends **no reels at
all** for this round: the "fake 5-bonus spin" the client shows (`busyOps: buyBonusReveal`) is drawn
entirely client-side. Two consequences:

* the reveal can only ever be graded on the CLIP, never from the response (JK-39);
* `wild_expands_only_in_freespins` is safe on this round because `views == rawViews` — it reports no
  expansion rather than a spurious one on a paid round. That is luck rather than design, and worth
  knowing before anyone "improves" the invariant to compare against a symbol map.

### The id triplet takes a THIRD shape here

| round | `id` | `refId` | `roundId` |
|---|---|---|---|
| base spin | all three equal | | |
| free spin | the SPIN | the BONUS | the BONUS |
| **purchase** | **= `refId`** | | **DIFFERENT** |

So `id == refId != roundId` identifies a purchase. Nothing correlates on it yet; it is recorded so
that the next person writing a History or replay case does not assume two patterns exist.

### No `baseStake`, and no `hasReachedMaxWin`

`baseStake` is absent, so `base_stake()` falls back to `stake` and returns **400 — the price, not the
bet**. Anything that reads a "bet" off this round gets the purchase price. `hasReachedMaxWin` is
absent too, so the cap has nothing to say on the purchase round itself.

---

## 8. Writing the profile

What the above implies for `scripts/game_profile.py`, once a trigger is captured:

| | |
|---|---|
| base class | `LineGame` — fixed paylines, one grid per round (Chaos is the reference) |
| `F_STEPS` | `result` (single object → wrap it so `Bet.cascades == 1`) |
| `PAYLINES` / `LINE_DIVISOR` | 20, fixed — the divisor in §3's formula |
| grid | 5 reels × 3 rows, column-major |
| free-spin counters | flat: `freeSpins` (remaining), `totalFreeSpin` (awarded) — no nested container, unlike Zeus |
| `HAS_BUY_BONUS` | true (100× price, plus the Lucky Spin wheel) |
| capabilities | expanding wild, Lucky Spin wheel (`luckyMultipliers: [1, 3, 12]`) |
