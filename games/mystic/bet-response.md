# Mystic — BET response reference

The server's BET payload: the contract the whole framework drives on. Scenarios read game LOGIC from
here and nowhere else (`docs/session-summary-scenario-qa.md`), so this is the file to check before
writing an assert, an invariant, or anything that counts cascades.

Real captured samples live in `fixtures/bet-responses/`:

| File | What it is |
|---|---|
| `base-cluster-win.json` | base spin, 5 winning cascade steps, payout 81 on stake 4 |
| `freespin-trigger.json` | base spin that AWARDED 8 free spins (already `gameState: 2`) |
| `freespin-round.json` | one free spin of that bonus (`stake: 0`, same `roundId`) |

They are the payload shape `setForcedResult()` takes — **strip the envelope first**, it wants the
inner `result` object (`bridge-qa-api.md` §Actions without a button).

---

## 1. Envelope

```jsonc
{ "status": 1, "code": 0, "msg": "", "timestamp": 1787037370904,
  "result": { /* GameBetResult — everything below */ } }
```

`scenario_engine.Bet.parse()` receives the **inner** object, so `Bet.raw` is the `GameBetResult`.

## 2. `GameBetResult`

| Field | Type | Notes |
|---|---|---|
| `result` | array | the **cascade steps** — see §3. `Bet.cascades = len(result)` |
| `gameState` | int | `1`=Base · `2`=FreeGame · `3`=BuyBonus · `4`=Promo |
| `stake` | number | what THIS bet cost. **`0` in free spins** |
| `baseStake` | number | the round's original stake — stays `4` even when `stake` is `0` |
| `payout` | number | this bet's total win = **sum of every step's `payout`** (verified on all three samples) |
| `rawPayout` | number | equals `payout` in these samples |
| `accumulatedWinAmount` | number | running total across the ROUND (trigger 3 → FS round 12.6) |
| `winlose` | number | net for the bet: `payout - stake` (trigger: 3 − 4 = −1) |
| `balance` | number | **authoritative** post-payout balance. Use this, not arithmetic |
| `freeSpins` | int | free spins **REMAINING** — not awarded. ⚠ see the max-win trap below |
| `totalFreeSpin` | int | free spins **awarded** for the bonus (8 in the sample) |
| `totalFreeSpinPayout` | number | bonus running total |
| `id` | string | **this bet's** id — unique per free spin |
| `roundId` | string | **the round's** id — SHARED by the trigger and every free spin in it |
| `hasReachedMaxWin` | bool | truncates the chain server-side |
| `maxWin`, `jackpot`, `betLines`, `paylines`, `refId`, `promoResult` | | unused / null in these samples |
| `betTime`, `processedTime`, `settledTime` | epoch ms | |

> **`id` vs `roundId`.** In `freespin-trigger.json` and `freespin-round.json` the `roundId` is the
> same (`917790755957768193`) while `id` differs. A History ROW is one round; its detail ticket holds
> one bet per `id`. That is exactly why paging the detail walks the free-spin chain.
> In a plain base round (`base-cluster-win.json`) `id == roundId`.

## 3. `result[]` — the cascade steps

One entry per cascade step, in play order:

```jsonc
{ "views":   [ { "id": 1, "value": ["B","A","A","D","D","D","P1"] }, ... ],  // 7 columns x 7 rows
  "winnings": [ { "symbol": "D", "type": "X", "multiplier": 0.25,
                  "clusterSize": 6,
                  "positions": [ {"x":1,"y":1}, ... ],
                  "payout": 1.00 } ],
  "multiplierLevel": 1,
  "payout": 1.00 }                                        // step total = sum of its winnings
```

- **`views[]`** — 7 columns, each `value` 7 symbols top→bottom. Symbols are STRING names:
  `A B C D P1 P2 P3` and `FreeSpin!` (scatter). `positions` are `{x: column, y: row}`.
- **`winnings[]`** — one entry per winning CLUSTER. A step can pay more than one cluster at once
  (step 2 of `base-cluster-win.json` pays `B` and `A` together).
- **`payout`** — the step's total. **`0` marks a non-winning step.**
- **`multiplierLevel`** — the cascade multiplier in force for that step (§5).

### The terminal step

**The chain always ends with a non-winning step** (`payout: 0`, `winnings: []`) — that is what stops
the cascade. Confirmed in all three samples:

| Sample | step payouts |
|---|---|
| base-cluster-win | `1.0, 18.4, 48, 4.0, 9.6, **0**` |
| freespin-trigger | `3.0, **0**` |
| freespin-round | `9.6, **0**` |

So for a round that wins, `winning steps == len(result) - 1`. A LOSING spin is a single step with
`payout: 0` → zero winning steps.

## 4. Counting cluster wins (`scenario_engine.cluster_wins`)

**A "cluster win" for scroll purposes = a winning cascade STEP, not a `winnings[]` entry.** The
history detail renders one grid per step, and a step that pays two clusters is still one grid.
`base-cluster-win.json` therefore counts **5**, not 6.

```python
cluster_wins(bet) == len([s for s in bet.raw["result"] if s["payout"] > 0])
```

This is what makes the history-detail scroll deterministic: no win → no scroll; N winning steps → N
pages (`spin-then-history`, `freespin-then-history`).

## 5. `multiplierLevel` — ⚠ it is a LADDER, not `+1` per cascade

| Sample | levels across the steps |
|---|---|
| base-cluster-win (base game) | `1 → 2 → 3 → 5 → 8 → 10` |
| freespin-trigger / freespin-round (free game) | `1 → 2` / `2 → 4` |

Base game climbs `1,2,3,5,8,10` — **not** `1,2,3,4,5`. The values come from the game's own config;
read them with the bridge's `getMultiplierLevels()` → `{baseGame[], freeGame[]}` rather than assuming
an increment.

Two consequences worth acting on:

1. **Mah_044 / Mah_049 say "multiplier +1 per cascade".** That does not match this data. Confirm the
   intended rule against `getMultiplierLevels()` before trusting those cases' expected results.
2. **Mah_043/044/045/048/049/050 can be promoted from AI video to exact `stream:` asserts** — the
   multiplier is right here in the response, per step. `docs/mahjong-coverage.md` already flags this
   as the promotion to make "if the game later exposes the multiplier in the bet response". It does.

## 6. Traps

- **`freeSpins` is REMAINING and is not a loop variable.** A max-win round returns to `gameState: 1`
  with `freeSpins > 0` (those spins are forfeited), so terminating a drain on `freeSpins == 0` hangs
  forever. Use the bridge's derived `continues`, or `game_state != 1`, as `play_out_freespins` does.
- **The trigger response already reports `gameState: 2`** (`freespin-trigger.json`) even though it is
  the paid base spin — `stake: 4`, and `freeSpins == totalFreeSpin == 8`. `is_freespin_trigger()`
  keys off exactly that.
- **Use `balance`, never recompute it.** It is the authoritative post-payout figure; the `conservation`
  invariant checks `balance == prev - stake + payout` against it.
- **Free-spin bets are `stake: 0`** while `baseStake` keeps the original — the `stake_zero_in_freespins`
  invariant and Mah_085 both rest on this.
