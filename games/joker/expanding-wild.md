# Joker: Where's My Banana — the Expanding Wild, and the multipliers around it

How the feature behaves, what the server says about it, and how to assert it without reading pixels.

* The payload it lives in → `games/joker/bet-response.md`
* The bridge's own state for it → `games/joker/docs/raw/bridge-api.md` (§5 snapshot, §10 events)

---

## 1. The feature, as the game help states it

> **Expanding Wild** — During the Free Spins, WILD symbols landing on **Reels 2 and 4** expand to
> cover the entire reel **except BONUS and BANANA symbols**, unlocking greater win potential.
> All spins come with a **x3 multiplier** during the Free Spins.

> **Buy Bonus** — 20 Free Spins plus a multiplier wheel awarding **x1, x3 or x12**; or skip the
> wheel and start at the default **x3**. The cost is **100x the selected bet**.

The default is **x3** on both paths: `MultiplierLuckySpin` marks it `IsDefault`, and every
`BaseGame.FreeSpins[]` entry gives a natural trigger `Multiplier: 3`. So `wheel: keep` keeps x3,
and JK-64 asserts it.

⚠ **A live run on 2026-09-21 kept the wheel and got x12.** The framework clicked the right
control (`luckySpin:keep`, bridge §7.2), so that is a DEFECT to chase rather than expected
behaviour — see JK-64 for the two candidates and what each would mean.

Four claims worth separating, because each is asserted differently:

| # | claim | where it is decided |
|---|---|---|
| 1 | a wild expands only **in free spins** | the response — `rawViews` vs `views` |
| 2 | only on **reels 2 and 4** | the response — which reel index differs |
| 3 | it covers the reel **except BONUS and BANANA** | the response — the cells that did NOT become wild |
| 4 | free spins carry a **x3** multiplier (x1/x3/x12 when bought) | the response — `luckyMultiplier` |

---

## 2. The one fact that makes this assertable: `rawViews` vs `views`

Every BET response carries the reels **twice**:

```jsonc
"rawViews": [ …, { "id": 4, "value": [2, 9, 8] }, … ],   // as they landed
"views":    [ …, { "id": 4, "value": [9, 9, 9] }, … ]    // after the expansion
```

* **equal** → no expansion happened this round
* **different on reel `id`** → a wild expanded there, and the diff says exactly which cells changed

Measured on `fixtures/bet-responses/freespin-wild-expand-terminal.json`: reel **4** went
`[2, 9, 8] → [9, 9, 9]`, and every other reel was untouched. Both base captures show `rawViews ==
views` throughout.

**This is the whole assertion surface.** The bridge also exposes the *animation*
(`isWildExpanding`, `busyOps: ["wildExpanding"]`, `roundState: SHOW_WILD_EXPENDING`), but those say
"a wild is expanding right now", not "it expanded correctly" — they are for waiting and for the
video's phase, never for grading the result.

⚠ **Symbol 9 is WILD!** — proved by `winType` (`K-WILD!-K-P4-K` against `symbols [4,9,4,8,4]`), and
it is the value the whole reel took. Do not hard-code `9` in a scenario: read it through the profile.

---

## 3. The invariants this supports

Sketches for `scripts/scenario_engine.py`, in the shape of the existing `stream:` library. None are
written yet — each names the capture it needs first.

### `wild_expands_only_in_freespins`
For every round where `views != rawViews`, that round is a free spin (`stake == 0`, or
`gameState == 2`). A base round whose views differ is the defect.
**Ready to write** — the three captures already satisfy it.

### `wild_expands_on_allowed_reels`
Every reel that differs is one of the allowed ids. ⚠ **The allowed set is UNCONFIRMED.** The help
says "Reels 2 and 4"; the capture expanded reel `id: 4`. Whether the help counts reels 1-based (ids
2 and 4) or 0-based (ids 3 and 5) is not settled by one sample that happens to fit both readings
under the 1-based reading only. **Capture an expansion on the OTHER reel before writing this**, or
it will encode a guess.

### `wild_expansion_covers_reel_except_blocked`
On an expanded reel, every cell equals the wild id **except** cells that held a BONUS or BANANA in
`rawViews`, which keep their symbol. ⚠ Needs a capture where a bonus/banana shares the reel with an
expanding wild — the one sample expanded a reel with none, so it proves only the simple case.
Symbol `12` (seen mid-reel as `[1, 12, 1]`) is the prime suspect for one of the two, but nothing
proves which.

### `freespin_multiplier_is_three`
`luckyMultiplier == 3` on every free spin of a naturally triggered bonus; one of `1, 3, 12` after a
Buy Bonus wheel (`getGameInfo().luckyMultipliers` publishes the ladder). The capture reads `3`.
⚠ The payout arithmetic in `bet-response.md` §3 held on a free spin that paid **0**, so **how the
multiplier enters a winning free spin's payout is unverified** — capture a paying free spin before
asserting `payout = Σ rate × line_bet × luckyMultiplier`.

---

## 4. How a scenario should reach it

1. **Force the bonus.** Expanding wilds happen only in free spins, so the run needs a trigger, and
   Rule 17 says force one with a paytable rather than hunting. ⚠ No fixture exists for this game yet
   (`games/joker/paytables/` is empty) and **no capture shows what a trigger even looks like** —
   that is the first blocker, not the fixture.
2. **Cap it** — a bonus that can retrigger needs `max_win_x`, default `20` (Rule 17).
3. **Collect every free spin**, and for each one diff `rawViews` against `views`. `play_out_bonus`
   dispatches to `play_out_freespins` for a line game; its `on_round` hook is where the diff goes, so
   the evidence is gathered *during* the bonus rather than reconstructed afterwards.
4. **Grade the pixels separately.** The stream proves the reel became wild; only the clip proves the
   player saw it expand. Scope the prompt to the free-spin phase — the base game before it has no
   expansion and an unscoped prompt reports "no expansion seen" from the wrong footage.

Facts worth publishing from such a scenario: `expanded_rounds`, `expanded_reel_ids`,
`blocked_cells_kept`, `lucky_multiplier`, `fs_rounds`.

---

## 5. What to capture next, in order

| # | capture | unblocks |
|---|---|---|
| 1 | the round that **awards** free spins | the trigger rule, and every scenario that forces a bonus |
| 2 | an expansion on the **other** allowed reel | `wild_expands_on_allowed_reels` |
| 3 | an expansion on a reel holding **BONUS or BANANA** | the "except" half, and the id of symbol 12 |
| 4 | a **paying** free spin | how `luckyMultiplier` enters the payout |
| 5 | a **Buy Bonus** round + its Lucky Spin pick | the 100x price, the x1/x3/x12 ladder |

Until 1 and 4 exist, this game has no assertable bonus arithmetic — only the expansion diff, which is
already solid.
