# Session Summary 10 — Last Bet on all five games, four framework bugs, and Joker onboarded

Continues `session-summary-scenario-qa-9.md`. `session-summary-scenario-qa-3.md` is still the
onboarding guide for a NEW GAME — read that first if you are adding one.

This session started by writing the **Last Bet** cases for Dragon VIP and ended with them on every
game, because the first thing it found was that the whole module had been read wrong: *Last Bet is
not a panel*. Four framework bugs fell out of running them, two of which were silently passing or
silently failing runs. A sixth game, **Joker: Where's My Banana**, was onboarded from its bridge doc
and paytable plus three captured payloads.

---

## 0. Start the next session here

```bash
python tests/chaos/validate_suites.py --game joker        # 10 cases, OK
python tests/chaos/validate_suites.py --game chaos        # 79 cases, OK
python tests/chaos/validate_suites.py --game dragon-vip   # 69 cases, OK
python tests/chaos/validate_suites.py --game dragon       # 56 cases, OK
python tests/chaos/validate_suites.py --game zeus         # 56 cases, OK
python tests/chaos/validate_suites.py --game mystic       # 3 known duplicate ids (Mah_012/044/045)
python tests/chaos/build_scenario_index.py                # 47 scenarios · 31 invariants, docs in sync
```

> ⚠ **Still not in git. 99 paths** modified or untracked, last commit `bb01fa6` — the same warning
> summaries 8 and 9 carried. The whole engine, every suite and all of `docs/` is in the working tree
> only. **Commit before anything else.**

**Blocked on a capture, not on code:** Joker has no fixture and no captured free-spin TRIGGER, so its
two bonus runs hunt instead of forcing (§5). That capture is the single thing gating the rest of it.

---

## 1. Last Bet is a RESUME behaviour, not a panel

The correction that shaped the session. Every Last Bet row begins *"Mở Last Bet"*, which had been
read as **open the Last Bet panel** — and no bridge exposes a `lastBet` control, so the cases were
parked as "not automatable" on Mystic and written against the History panel on Dragon VIP.

**"Mở Last Bet" means REOPEN THE GAME.** The player closes it and comes back; the session must show
the round they last played — stake, win, board — with the bet control on that round's stake rather
than the configured default. The client does it in `applyLastBet()` at Start, from
`getGameInfo().lastBet`. No button is needed, so the cases are automatable on every game.

⚠ **The trap that makes them green on nothing:** `applyLastBet()` NULLS `gameInfo.lastBet` at Start,
so a live read reports "no last bet" on a session that restored perfectly. `getGameInfo()` must
return the PRE-START capture, and `isGameInfoCaptured()` is what says it did.

### Two behaviours, two scenarios

| | reload with | driven by | scenario |
|---|---|---|---|
| RESTORE | nothing owed | `lastBet` | `last-bet-restore` |
| RESUME | a bonus still OWED | `pending` | `bonus-resume-denomination-swap` |

Conflating them is how a case grades the wrong screen, so each publishes the gate that proves which
one happened (`resumed_rounds == 0` / `is_pending_freespin == True`).

### Where the cases live now

| Game | Cases | Suite |
|---|---|---|
| Dragon VIP | DV-67 … DV-71, DV-72 not automated | `dv-lastbet.yml` |
| Dragon | DR-53 … DR-58, DR-59 not automated | `dr-lastbet.yml` |
| Zeus | ZZ-53 … ZZ-58, ZZ-59 not automated | `zz-lastbet.yml` |
| Chaos | CR_092, 093, 095, 096a/b | `cr-lastbet.yml` (moved out of `cr-ui-resilience.yml`) |
| Mystic | Mah_081, 082, 084, 085a/b | `mah-lastbet.yml` (Mah_087 stays in `mah-history-lastbet.yml`) |

**Mystic's four were previously blocked on the panel reading** — the correction unblocked them.
**Mah_087 deliberately stayed put**: it is the one case in the module about the panel *drawing
itself* (it flashes ~0.5 s before each bonus round) and needs the high-fps clip.

---

## 2. Four framework bugs, found by running the cases

### 2.1 `drain_to_base` raised on SUCCESS — a stray `else`

```python
if not finished:
    raise RuntimeError(f"bonus drain gave up after {cap} rounds …")
else:
    raise RuntimeError(f"bonus did not reach a terminal round within {cap} rounds")   # ← fired when it DID
```

A leftover from a `for … else`. **Every load that resumed an owed bonus failed after the bonus had
played out correctly.** It surfaced as `bonus did not reach a terminal round within 500 rounds` on a
run that lasted seconds. Removed, with a comment so it cannot come back.

### 2.2 The pending free-spin counter was read with the wrong spelling

Zeus/Dragon/Dragon VIP nest the BET response counter as `freeSpinInfo.freespins` (lowercase), but
**`gameInfo.pending` spells it `freeSpins`** — so `pending_free_spins` always read **0** and the log
said `0 free spin(s) owed` while a bonus was plainly owed. The resume still ran (it also checks
`gameState`), but the number was evidence in DV-71. New profile field `F_PENDING_FREE_SPINS`, set on
Zeus, inherited by the family.

### 2.3 The denomination teardown restored the wrong id — and compounded

`reset-denomination` restored `previous_denomination_id`. **Measured on dev 2026-09-18: BO refuses
`isDenomOverride: 0` unless the id sent with it is the MASTER game's.**

| sent | result |
|---|---|
| `denominationId: 1734` (the automation ladder) + `isDenomOverride: 0` | **rejected** `INVALID_UPDATE_REQUEST` |
| `denominationId: 213` (master) + `isDenomOverride: 0` | accepted |

The failure compounds: a rejected reset leaves the automation ladder in force, so the NEXT run reads
"previous" as that ladder, sends the same invalid pair, and fails too — while every run in between
plays at 11/22/33 believing otherwise. **That is exactly what happened**: DV-71/73 went red on
`old_bet_on_new_ladder`, correctly, because the run started at bet 11.

Fixed: the setup now records the previous id, whether the override was on, AND the master id; the
teardown restores the override when the row genuinely had one, otherwise hands back to the master,
and **detects the leaked case so it self-heals**. Tested live from both a clean and a leaked start.
Written up as trap 5 in `docs/bo-denominations.md`.

### 2.4 The bet change hung for two minutes

`last-bet-restore`'s `_pick_other_denomination` clicked `bet:row:0` with no scroll, no `optional`,
and the default 60 s timeout — twice. Dragon's own doc says the row *"may be below the fold —
`scrollBetRowIntoView(i)` first"* and *"the current row is non-interactable"*, and `getBetIndex()`
can answer `-1`, so row 0 was often the row the game was already on. Now it picks the target **by
value** (never the current row), scrolls it into view, bounds every click, verifies the bet actually
moved, and marks the timeline.

---

## 3. Two rules, and a stricter validator

**Rule 20 — a BO write is not live when the API answers.** BO publishes the change on a **Kafka
event**; `200 OK` means BO stored it, not that the game server has it. So: write with the game
closed, WAIT (`set_automation_denomination(settle_s=5.0)`), then **prove** the reopened client has
it (`client_on_new_ladder`). The danger is that a stale read does not look like an error — it looks
like a PASS for "the bonus kept its old stake", which is the very claim under test.

**Rule 17 rewritten — when to cap, and the default is now `max_win_x: 20`.** A cap is REQUIRED
whenever the forced bonus can feed itself (retrigger in free spins, a counter-resetting symbol, a
bought bonus). `10 * bet` was the old default and truncates so often it leaves a one-round bonus;
20× is long enough to show a countdown and short enough to bound a runaway. Keep a LOOSE cap (500)
where the feature's own size or length is the subject.

**`validate_suites.py` now checks `params:` against the scenario constructor.** The runner silently
SKIPS a run whose params it cannot apply, so a renamed parameter used to show up only as a run that
never happened. No existing suite was wrong; the check caught a stale `denomination_swap` in a
scratch file.

---

## 4. Engine additions

| | |
|---|---|
| `last-bet-restore` | play a round → reload → compare. `outcome: win \| lose \| freespin_trigger \| any`, `change_bet_after`, `result_hold_s` (3 s, holds the settled round in frame so the clip has something to compare against) |
| `bonus-resume-denomination-swap` | trigger → `stop_client()` → BO swap → `open_game()` → play the resumed bonus → settle. Each step visible in `run()` |
| `buy-bonus-lucky-spin` | Joker's Buy Bonus. **`buy-bonus` would hang it**: the purchase raises a wheel the bridge never answers and `dismissPopups()` never closes (§5) |
| `play_out_bonus(ctx, trigger, …)` | **the Rule 1 drain**: picks `play_out_holdspin` / `play_out_freespins` by `GAME.mechanism`. Nine call sites were reaching for `play_out_freespins` on Hold & Spin games, which misses a FULL GRID |
| `on_round=` on both drains | read evidence that only exists DURING a bonus (the bet readout per resumed round) |
| `ctx.set_automation_denomination()` · `ctx.open_game()` | split out of a three-in-one helper so the scenario reads as its steps |
| `teardown: reset-denomination` | re-raises on failure, stopping the suite — a ladder left on 11/22/33 makes later runs fail for reasons their own suite never mentions |
| invariants | `wild_expands_only_in_freespins` · `wild_expansion_is_uniform` · `freespin_multiplier_in_ladder` |

---

## 5. Joker: Where's My Banana — onboarded

5x3, 20 fixed paylines, expanding wilds, Buy Bonus with a Lucky Spin wheel. Built from
`games/joker/docs/raw/bridge-api.md`, `docs/raw/paytable.json` and **three captured payloads**.

**What the paytable settled** (the help only implied it):

```
BaseGame.EnableWildExpanding     false        FreeSpinGame  true
ExcludedWildExpandingSymbols     ["FreeSpin!", "Power!"]    ← the help's BONUS / BANANA
FreeSpins                        3/4/5 scatters → 12/15/20 spins, all ×3
Packages[0]                      20 spins, BuyMultiplier 100     (one package only)
MultiplierLuckySpin              ×1 (4950) · ×3 (3950, default) · ×12 (1100)
```

**What the captures settled** (`games/joker/bet-response.md`, computed not quoted):

* `payout = Σ payoutRate × (stake / betLines)` — 50 × 0.03 = 1.5 exactly
* `winlose = payout − stake`; free spins cost nothing; `balance` authoritative
* **the expansion is `views` vs `rawViews`** — reel 4 went `[2,9,8] → [9,9,9]`
* ⚠ **`multiplier` is NOT a payout factor**: the win reads `multiplier: 2.5` and the payout matches
  WITHOUT it. Exposed for reporting only; no invariant may multiply by it
* ⚠ **in free spins `id` ≠ `roundId`** — `id` is the spin, `roundId`/`refId` the BONUS. Anything
  correlating by `roundId` sees all 12 free spins as one round

Delivered: profile `JokerBanana(LineGame)`, three invariants (each tested against the real captures
AND a mutated copy), `buy-bonus-lucky-spin`, `jk-features.yml` (JK-60 … JK-69), registration in
`validate_suites.py`, and `games/joker/expanding-wild.md`.

⚠ **A mutation test caught a bug in one of my own invariants**: `wild_expansion_is_uniform` flagged
the wild that LANDED (unchanged in `rawViews`) as a violation and failed the real capture. A kept
cell is never wrong — it is the landing wild or a blocked symbol. The rule now checks what was
WRITTEN.

### ⚠ The blocker

**Nothing has been captured of a free-spin TRIGGER.** The payload supplied as one awards nothing
(`freeSpins: 0`, `totalFreeSpin: 0`, `gameState: 1`) and its `roundId` does not match the bonus the
free-spin capture belongs to. So:

* `games/joker/paytables/` is empty — no fixture can be derived without knowing what a trigger looks
  like, and `derive_paytable.py` needs the base paytable copied in first
* both bonus runs in `jk-features.yml` have `paytable:` COMMENTED OUT and hunt instead. That is a
  stopgap, not the design (Rule 17)

`games/joker/bet-response.md` §7 lists the five captures to take, in order.

---

## 6. Live findings worth keeping

| | |
|---|---|
| **Chaos has `getGameInfo()` with `lastBet`** — though its bridge doc documents neither. The restore works: `last_bet_present`, `stake_restored`, `restored_payout`, `bet_opens_at_last_stake` all passed |
| **Chaos lacks `isGameInfoCaptured()`** → `info_captured` reads `None`. **A bridge gap to file.** Its suites gate on `info_captured != False` (fails when the bridge SAYS the capture is missing, passes when it cannot answer); Mystic does the same, Zeus/Dragon/VIP keep `== True` because their docs promise the function |
| **The TKN 3x3 automation ladder now exists**: id **1734**, `[11, 22, 33]`. The master for that pair is **213** (`Test Upload 11`), agent row **50346** |
| **`denomination/update` rejected the create once** with `VALIDATION_FAILED` — it is a full replace, so an existing invalid ladder in the pair fails the whole request. `BoApiClient` now appends the server's error detail to the message |
| **Zeus and Dragon share one BO pair** (3x3, currency 18) and therefore one automation ladder. Safe back to back, **not side by side** — one suite's teardown can land while the other is mid-bonus |

---

## 7. Docs written or changed

| Doc | What |
|---|---|
| `docs/replay-feature.md` | **new** — CR_072 … CR_080 design: what the bridge must expose (`getMonumentalWinRows`, `getReplayLink`, `getReplayState`, `replay:watchAgain`), three runs, six open questions. Nothing built |
| `games/joker/bet-response.md` · `expanding-wild.md` | **new** — the response contract and the feature, from the captures |
| `docs/scenario-authoring-rules.md` | **Rule 20** new; **Rule 17** rewritten around when to cap and the 20× default |
| `docs/bo-denominations.md` | **trap 5** — clearing the override needs the MASTER id, and how a failed teardown compounds |
| `docs/scenario-index.md` · `scenario-toolkit.md` | the three new scenarios, three invariants, `play_out_bonus`, `open_game`, `set_automation_denomination`; counts refreshed |
| coverage docs | `zeus-coverage`, `dragon-coverage`, `chaos-coverage`, `mahjong-coverage`, `dragon-vip-coverage` — Last Bet rows and findings |

---

## 8. What to do next

1. **Commit.** 99 paths.
2. **Capture a Joker free-spin trigger** (and a paying free spin, and a Buy Bonus round). Everything
   else on that game is waiting on it; then derive `joker-fs-easy` and switch the two `paytable:`
   lines on.
3. **Write `jk-core.yml` / `jk-autoplay.yml`** — Dragon's case list on mechanism-free scenarios —
   and `docs/joker-coverage.md`. The feature suite is done.
4. **Run the Last Bet suites live**, in this order: Dragon VIP (the only one with a run behind it),
   then Zeus/Dragon (remember: not concurrently), then Chaos and Mystic. Watch for:
   * whether the bet readout is visible DURING a bonus — three cases assume it is, and the file says
     what to drop if it is not;
   * whether the forced paytable survives the denomination swap (both write the same agent row).
5. **File the two bridge gaps**: Chaos's missing `isGameInfoCaptured()`, and its undocumented
   `getGameInfo()`.
6. **Replay** (`docs/replay-feature.md`) is designed but needs bridge work before any case exists.
