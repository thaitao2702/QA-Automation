# Zeus — BET response reference

The server's BET payload: the contract the scenario suite drives on. Scenarios read game LOGIC from
here and nowhere else, so this is the file to check before writing an assert, an invariant, or
anything that counts coins.

Real captured samples live in `fixtures/bet-responses/`:

| File | What it is |
|---|---|
| `holdspin-trigger.json` | base spin landing 3 Coins on reel 1 → Hold & Spin, 3 respins |
| `holdspin-terminal-full-grid.json` | the round that ENDS the feature: 9 Coins, pays 261 on a stake of 3 |

Mechanism: `docs/raw/Doc Zappin zeus.md` · Bridge: `bridge-api.md` (v3) · Profile:
`scripts/game_profile.py` → `Zeus(HoldSpinGame)` · Cases: `docs/zeus-coverage.md`

---

## 1. Envelope

```jsonc
{ "status": 1, "code": 0, "msg": "", "timestamp": 1787731633936,
  "result": { /* BetResult — everything below */ } }
```

`scenario_engine.Bet.parse()` receives the **inner** object, so `Bet.raw` is the `BetResult`.

## 2. `BetResult`

| Field | Type | Notes |
|---|---|---|
| `gameState` | int | **`1` = base · `2` = free game.** Present and authoritative — see §5 for the trap |
| `stake` | number | this bet's cost. **`0` during the feature** |
| `baseStake` | number | the round's original stake — stays `3` while `stake` is `0`. Coin face values are computed from THIS |
| `payout` / `rawPayout` | number | this round's win |
| `accumulatedWinAmount` | number | the round's running total — **the Coin Win Meter** at the end of the feature |
| `winlose` | number | `payout - stake` (trigger: `0 - 3 = -3`) |
| `balance` | number | **authoritative** post-payout balance. Never recompute it |
| `freeSpinInfo` | object \| **null** | `{ freespins, totalFreeSpin, accumulateWin }` — see §3 |
| `coinCount` | int | Coins on the grid |
| `coinPositions[]` | array | the held Coins — see §4 |
| `result.views[]` | array | the grid: 3 reels × 3 rows, `value` top→bottom, symbol IDs |
| `paylines[]` | array | `{ symbols, payline, payoutRate, winType }`. In the feature `winType: "FreeSpin"`, and `payoutRate` is the win as a multiple of stake (`261 / 3 = 87`) |
| `betLines` | int | `5` — the payline count, and the divisor in the paytable formula |
| `maxWin` | number | the max-win cap (`300000`) |
| `hasReachedMaxWin` | bool | truncates the chain server-side |
| `id` / `refId` / `roundId` | string | `id` is THIS bet; `roundId` is shared across the round's bets |
| `anteMultiplier` | number | `0` — the field exists although this build ships no Ante Bet |
| `jackpot`, `promoResult` | | unused in these samples |

## 3. `freeSpinInfo` — nested, and **nullable**

```jsonc
"freeSpinInfo": { "freespins": 3, "totalFreeSpin": 0, "accumulateWin": 0 }   // trigger
"freeSpinInfo": null                                                          // final round
```

Three things to know:

1. **`freespins` is lowercase-`s`** — not Mystic's `freeSpins`. The profile maps it
   (`F_FREE_SPINS = "freespins"`, `F_FS_CONTAINER = "freeSpinInfo"`).
2. **`totalFreeSpin` is `0` even on the trigger.** This build never reports an "awarded" figure, so
   anything keying off *awarded* would never fire — the trigger is recognised by
   `freespins > 0` / `gameState == 2`.
3. **The container is `null` on the round that ends the feature.** `profile.free_spins()` reads that
   as `(0, 0)`, which is exactly right: no respins left. Any code touching `freeSpinInfo.x` directly
   would throw on the most important round in the stream.

## 4. `coinPositions[]` — the feature's whole state

```jsonc
{ "row": 0, "col": 2, "coinValue": 150, "multiplier": 50, "type": 2 }
```

| Field | Meaning |
|---|---|
| `row` / `col` | grid position. **The occupied set may only ever GROW during the feature** |
| `multiplier` | the Coin's face multiplier, drawn from the paytable's `CoinPrize` weights |
| `coinValue` | **`multiplier × baseStake`** — verified on both samples (`50 × 3 = 150`, `2 × 3 = 6`) |
| `type` | `TYPE_COIN`: `0` EMPTY · `1` NORMAL · `2` MINOR (50×) · `3` MAJOR (100×) · `4` GRAND (500×) |

**The meter equals the coins.** In the terminal sample the nine `coinValue`s sum to exactly `261`,
which is the round's `payout` and `accumulatedWinAmount`. That identity is what
`holdspin_payout_matches_coins` asserts.

## 5. Rules the suite depends on

1. **A full grid ends the feature immediately.** The terminal sample has `coinCount: 9` and pays out
   with no further respin.
2. **Coins are held.** A position present in one round must be present in every later round of the
   same feature (`holdspin_coins_held`).
3. **A new Coin resets `freespins` to 3**; a respin that adds none decrements it by one
   (`holdspin_reset_on_new_coin`, `holdspin_countdown`).
4. **`stake` is `0` during the feature** while `baseStake` keeps the original.
5. **The meter is paid once, at the end** — intermediate rounds pay `0`. Grade against the terminal
   round's `accumulatedWinAmount`, never a sum of every round's `payout`: a trigger round that also
   won on a payline would double-count.

## 6. ⚠ The terminal round looks like a trigger

The round that ends the feature reports **`gameState: 1` (base), `freeSpinInfo: null`, and
`coinCount: 9`**.

Anything that decides "is this a bonus trigger?" by counting Coins will say **yes** — 9 ≥ 3 — and
send a scenario off to drain a bonus that has already finished. `HoldSpinGame.is_trigger()` therefore
checks **state and counters first**, and only falls back to the symbol count for a build that reports
neither:

```python
if awarded > 0 or rem > 0:            return True     # counters say yes
if self.is_bonus_state(game_state):   return True     # state says yes
if F_GAME_STATE in raw or F_FS_CONTAINER in raw: return False   # it reported; it said no
return coin_count >= TRIGGER_COUNT                    # last resort only
```

This was a real bug found by these samples, not a hypothetical.

## 7. Symbol IDs (`result.views[].value`)

| -1 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 13 |
|---|---|---|---|---|---|---|---|---|---|---|
| ERROR / COIN_EMPTY | A | J | Q | K | P1 | P2 | P3 | P4 | **WILD** | **COIN** |

The trigger sample's reel 1 is `[13, 13, 13]` — three Coins stacked on one reel, which is enough:
Coins pay from anywhere, not left-to-right.

## 8. Win tiers (× stake)

`LOSE 0` · `SLIM 0<x<1` · `SMALL 1–2.5` · `MEDIUM 2.5–10` · `BIG 10–25` · `MEGA 25–50` · `ULTRA ≥50`.
The terminal sample is `87×` → **ULTRA_WIN**.

⚠ `Constants.ts` exports a second, **dead** `WinRange` with different thresholds (`BIG: 20`,
`MEGA: 40`). Assert against `WIN_RANGE` in `Interface.ts` — `Zeus.win_tier()` implements that one.
