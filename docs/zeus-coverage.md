# Zeus (Zappin Zeus) test-case coverage — ZZ-01 … ZZ-52

Source: `test-case.xlsx`. The old step-scripted suite (`games/zeus/test-suites/raw.yml`) is superseded
by scenario/assert suites, the same model Mystic runs.

- Engine: `scripts/scenario_engine.py` · Profile: `scripts/game_profile.py` (`Zeus(HoldSpinGame)`)
- Bridge: `games/zeus/bridge-api.md` (v3; it replaced `bridge-api-v2.md`) · Mechanism: `games/zeus/docs/raw/Doc Zappin zeus.md`
- **Response shape (captured, annotated): `games/zeus/bet-response.md`**
- Contract for new games: `docs/game-bridge-contract.md`
- **Bridge defects blocking these cases: `docs/zeus-bridge-defects.md`**

**Suites**: `zz-core.yml` · `zz-autoplay.yml` · `zz-holdspin.yml` · `zz-input-sound-reconnect.yml`

```
python scripts/run_scenario.py --game zeus --env dev --suite games/zeus/test-suites/zz-holdspin.yml
python tests/chaos/validate_suites.py --game zeus      # no browser: every reference resolves
```

---

## Rewritten against the authoring rules (2026-09-07)

The four suites were rewritten to `docs/scenario-authoring-rules.md` (18 rules) after those rules
were finished on Chaos. Nine things changed, and every one of them was a case that could pass while
proving less than it claimed:

| Rule | What was wrong | Now |
|---|---|---|
| **4** — proving a stop needs time on the clock | The auto cases asserted `rounds_played == auto_count` and nothing else. Counting N proves OUR loop ended, not that the client did. | Every auto case asserts `extra_round_after_stop == False` and `auto_flag_after == False`, and every prompt tells the model to judge the TAIL. The one exception is ZZ-08/16, where the out-of-money popup is what blocks the bet (Rule 15) — documented in place. |
| **10** — the bridge says what the game THINKS | ZZ-03, ZZ-05, ZZ-06, ZZ-13…ZZ-18, ZZ-21, ZZ-28, ZZ-30…ZZ-37, ZZ-39, ZZ-41…ZZ-43 asserted only the stream. A correct response rendered into a blank panel or a stale label passed all of them. | Every case carries an AI half. The three that do not are listed under "Not automated" with the reason. |
| **16** — one clip, one prompt | ZZ-19 ran a SECOND ten-spin auto-play purely to film a question the first clip already answered; ZZ-52 re-opened the same Info popup ZZ-11 had just filmed; ZZ-05 spun a `base-spin` to film the stake coming off, which ZZ-06's winning round already shows. Six Hold & Spin cases shared a clip with one `video:` between them. | Run-level `video:` / `image:` with `expected_by_case`. **Three** whole runs deleted; the five multi-case runs analyse their clip once. |
| **18** — screenfuls of ONE surface | ZZ-11 / ZZ-52 graded the Info guide per still, so "does it explain the bonus?" was honestly "no" on every page that does not. | `image: combine: true` — the screenfuls go to the AI as one document. |
| **17** — assert the LENGTH the claim needs | `holdspin_coins_held` / `_countdown` / `_reset_on_new_coin` all compare CONSECUTIVE rounds, so all three are vacuously true of a feature with no respins. | `respins_played >= 2` wherever the claim needs a sequence. |
| **6** — aim at the middle of the distribution | ZZ-34 ran on a p = 1 fixture that filled the grid on the first respin, which is the one case where no counter reset is due. It passed without testing anything. | New `fs-trigger3-reset` at p = 0.30 — see Forced outcomes. |
| **9** — the cap can end a bonus at any moment | No `max_win_x` at all, and no fact saying whether one had bitten. | A loose backstop cap plus `{facts.capped} == False` on every case that needs the whole feature. |
| — | ZZ-31 and ZZ-37 are two different situations, but both asserted only `grid_filled == True` — true on either path, so one of the two was always graded on the other's footage. | ZZ-31 asserts `filled_on_trigger == True` + `respins_played == 0`; ZZ-37 asserts the opposite of both, on its own fixture. |
| — | ZZ-22 asked "do the reels resolve faster?" of `toggle-turbo`, which **places no bet** — no reel ever turns in that clip. Its schema also carried two notification fields that no `expected` checked. | `turbo-modes`, which toggles Turbo on and SPINS. The unchecked fields are gone. |

**ZZ-05 + ZZ-06 are one run on `base-win`.** One winning round moves the balance TWICE — the stake
comes off as the reels start, the payout goes on as the win counts up — so the separate `base-spin`
run for the debit was paying for a real spin to film half of what the credit clip already contained.
`base-win` also exposes the readout's three values (`balance_before_spin`, `balance_while_spinning`,
`balance_after_win`), so each question names the two endpoints of the step it judges instead of only the
destination — the mistake that earned a correct NO on Mystic's Mah_008. The trade: `base-win` can
report unreached if nothing pays in 60 spins, which would make ZZ-05 inconclusive where `base-spin`
was near-certain to reach; on a 5-payline game that is remote, and it is the risk ZZ-06 already
carried alone. Chaos pairs CR_007 + CR_008 on one `base-win` for the same reason.

Also: ZZ-12 moved from "not automated" to automated (see below), ZZ-44/45 now assert
`click_rounds` / `key_rounds` (a double-fire looks like one spin on video and is two responses in
the stream), and the Hold & Spin drain moved into a shared `play_out_holdspin` helper that ends on
`is_terminal` (Rule 2), reports a max-win truncation (Rule 9) and **fails loudly at its cap** rather
than returning a half-played feature as though it had finished (Rule 13).

---

## What changed from `raw.yml`

| Old | New |
|---|---|
| `steps:` scripted per case — click, wait, assert | `scenario:` closed box + `assert:` per case |
| Hold & Spin rules via bridge analysers (`verifyHoldSpin*`) | **framework invariants** over the response stream — v2 removed those analysers by design |
| One case per run | one scenario play → many case verdicts |
| Zeus-only code | mechanism-free scenarios shared with Mystic; only the DIALECT is Zeus's |

Zeus is a **Hold & Spin** game, so it profiles as `HoldSpinGame`, not `CascadeGame`. Its whole feature
rule set is in the BET response (`coinPositions`, `coinCount`, `freeSpinInfo.freespins`), which makes
ZZ-30…ZZ-37 **exact `stream:` checks** rather than AI reads — the opposite of Mystic's multiplier
cases, which have no response field and stay probabilistic.

---

## Automated (✅) — 50 of 52

| Case | Scenario | Graded by | Suite |
|---|---|---|---|
| ZZ-01 UI at load | boot-base | stream + **image** (a still, not a montage) | zz-core |
| **ZZ-04 opens at the default bet** | **boot-base** (same run as ZZ-01) | **stream** (the first-session rule) | zz-core |
| ZZ-02 denominations = config | browse-denominations | stream + video | zz-core |
| ZZ-03 bet +/- | change-bet | **stream** + video | zz-core |
| ZZ-05 balance debited | base-win (**same run as ZZ-06**) | **stream** (conservation) + shared video | zz-core |
| ZZ-06 payout credited | base-win | **stream** (conservation) + shared video | zz-core |
| ZZ-07 insufficient balance | insufficient-balance | stream + video | zz-core |
| ZZ-08 + ZZ-16 auto stops on empty | auto-until-empty | **stream** + shared video | zz-autoplay |
| ZZ-09 in-game History | spin-then-history (`detail_scroll_steps: 0`) | **stream** (rows + detail opened + panel closed) + video | zz-core |
| ZZ-11 paytable UI | open-tutorial | **stream** + **image** (`combine: true`, shared with ZZ-52) | zz-core |
| **ZZ-12 paytable payouts scale with bet** | **paytable-bet-scaling** | **stream** + **image** per bet level | zz-core |
| ZZ-13 auto starts | auto-play | **stream** + shared video | zz-autoplay |
| ZZ-14 auto stops after N | auto-play | **stream** (+ Rule 4 stop facts) + shared video | zz-autoplay |
| ZZ-15 stop at any win | auto-stop-at-win (`stop_on: base_win`) | **stream** + video | zz-autoplay |
| ZZ-17 user stop | auto-user-stop | **stream** (+ stop facts) + video | zz-autoplay |
| ZZ-18 reload cancels auto | auto-reload-cancels | **stream** (+ stop facts) + video | zz-autoplay |
| ZZ-19 bet locked in auto | auto-play (**same run as ZZ-13/14/20**) | **stream** (`stake_constant`) + shared video | zz-autoplay |
| ZZ-20 spin count shown | auto-play | **stream** + shared video | zz-autoplay |
| ZZ-21 bet chosen before auto | auto-change-bet-first | **stream** + video | zz-autoplay |
| ZZ-22 turbo | **turbo-modes** (it spins; `toggle-turbo` does not) | **stream** + video | zz-input-sound-reconnect |
| ZZ-24 sound off | sound-toggles | **stream** (isSoundEnabled) + shared video | zz-input-sound-reconnect |
| ZZ-25 music off | sound-toggles | **stream** (isMusicEnabled) + shared video | zz-input-sound-reconnect |
| ZZ-26 disconnect <20s | reconnect-brief | stream + video | zz-input-sound-reconnect |
| ZZ-27 disconnect >20s | reconnect-long | stream + video | zz-input-sound-reconnect |
| ZZ-28 network switch | reconnect-long (**same run as ZZ-27**) | stream + shared video | zz-input-sound-reconnect |
| ZZ-29 multi-tab login | multi-tab-session | stream + video | zz-input-sound-reconnect |
| **ZZ-30 trigger 3–8 coins → 3 respins** | hold-spin-complete | **stream** `holdspin_award` | zz-holdspin |
| **ZZ-31 9 coins → instant payout** | hold-spin-full-grid | **stream** + video | zz-holdspin |
| **ZZ-32 coins held** | hold-spin-complete | **stream** `holdspin_coins_held` | zz-holdspin |
| **ZZ-33 only empty slots respin** | hold-spin-complete | **stream** `holdspin_coins_held` | zz-holdspin |
| **ZZ-34 new coin resets counter to 3** | hold-spin-complete (`fs-trigger3-reset`) | **stream** `holdspin_reset_on_new_coin` + video | zz-holdspin |
| **ZZ-35 no coin → count down** | hold-spin-complete | **stream** `holdspin_countdown` | zz-holdspin |
| **ZZ-36 out of respins → total payout** | hold-spin-complete | **stream** `holdspin_ends_correctly` | zz-holdspin |
| **ZZ-37 9 coins in feature → stop** | hold-spin-full-grid (`fs-trigger3-addcoin`) | **stream** `holdspin_ends_correctly` + video | zz-holdspin |
| ZZ-38 controls locked in feature | hold-spin-complete | stream + video | zz-holdspin |
| ZZ-39 buy price follows bet | buy-bonus | **stream** + shared video | zz-holdspin |
| ZZ-40 price = bet × multiplier | buy-bonus | stream + video | zz-holdspin |
| ZZ-41 bought bonus starts on 3 coins | buy-bonus | **stream** + shared video | zz-holdspin |
| ZZ-42 bought bonus spins like a natural one | buy-bonus | **stream** (same invariants) + shared video | zz-holdspin |
| ZZ-43 bought bonus ends like a natural one | buy-bonus | **stream** + shared video | zz-holdspin |
| ZZ-44 … ZZ-50 spin inputs | spin-inputs | stream + video | zz-input-sound-reconnect |
| ZZ-51 multi-language | language-tour | **image** per screen | zz-core |
| ZZ-52 tutorial flow | open-tutorial (**same run as ZZ-11**) | **stream** + **image** (`combine: true`) | zz-core |
| ZZ-53 reopening after a LOSING round restores its stake and no win | last-bet-restore (`outcome: lose`) | **stream** + **video** | zz-lastbet |
| ZZ-54 reopening after a WINNING round restores its stake, win and winning line | last-bet-restore (`outcome: win`) | **stream** + **video** | zz-lastbet |
| ZZ-55 reopening after a bonus TRIGGERED from the base game restores that round | last-bet-restore (`outcome: freespin_trigger`, `fs-trigger3-countdown`) | **stream** + **video** | zz-lastbet |
| ZZ-56 play at bet X, change the bet to Y, reopen → the game shows X | last-bet-restore (`change_bet_after`) | **stream** + **video** | zz-lastbet |
| ZZ-57 denomination changed on BO while a bonus is owed → the resumed bonus plays at the old bet · ZZ-58 after it, the bet is the new denomination's default | bonus-resume-denomination-swap (one run, `fs-trigger3-countdown`; teardown `reset-denomination`) | **stream** + **video** | zz-lastbet |

### The Hold & Spin invariants

Live in `scripts/scenario_engine.py`, computed from the response stream. Each was tested against a
deliberately broken stream and catches only its own defect:

| Invariant | Catches |
|---|---|
| `holdspin_award` | trigger paid the wrong number of respins |
| `holdspin_coins_held` | a landed coin was re-spun away |
| `holdspin_reset_on_new_coin` | a new coin did not reset the counter |
| `holdspin_countdown` | the counter did not decrement without a new coin |
| `holdspin_ends_correctly` | ended early, or kept spinning past 0 respins / a full grid |
| `holdspin_payout_matches_coins` | the meter paid something other than the coins collected |

### Forced outcomes

`zz-holdspin.yml` forces each path with a BO paytable fixture from `games/zeus/paytables/`:

| Fixture | `FreeSpinGame.CoinWeight` | Reaches | Cases |
|---|---|---|---|
| `fs-trigger3-countdown` | all `0` (p = 0) | trigger on 3, no new coin, 3→2→1→0 | ZZ-30/32/33/35/36/38 |
| **`fs-trigger3-reset`** (new) | all `300` (p = 0.30) | a coin lands, grid does **not** fill → the counter must reset | ZZ-34 |
| `fs-trigger3-addcoin` | all `1000` (p = 1) | every empty cell fills at once → 9 **during** the feature | ZZ-37 |
| `fs-trigger9` | all `0`, COIN on all 3 base reels | 9 on the **trigger**; no respin fired | ZZ-31 |
| *(none)* | the game's real weights | ZZ-39..43 force nothing — **buying is itself the deterministic entry**, and forcing the respin weights would make the run less representative, not more | ZZ-39..43 |

**DevMode is not usable** for these: `setDevModeResult` forces one client-side spin, and a Hold &
Spin is a server-driven sequence. If BO is unconfigured the run is **skipped with an error**, never
run unforced.

> **ZZ-34 was vacuous before `fs-trigger3-reset`.** It ran on `fs-trigger3-addcoin`, where p = 1
> fills all six empty cells on the first respin — and a round that fills the grid is the one round
> `holdspin_reset_on_new_coin` deliberately skips, because a full grid ends the feature and no reset
> is due. So the invariant passed without the reset ever happening, and `coins_added >= 1` passed on
> the six coins that ended the bonus. At p = 0.30 a respin adds at least one coin 88% of the time
> and fills the grid outright 0.07% of the time. Arithmetic and the fixture table:
> `games/zeus/paytables/paytable.md` §3.4.

> **`max_win_x` is LOOSE here (500), not the usual tight `10 * bet`.** A tight cap truncates the
> feature mid-respin, and a truncated Hold & Spin ends with respins owed and the grid unfilled —
> exactly what `holdspin_ends_correctly` reports as a violation. That would be a red blaming the
> game for the cap. The feature is bounded anyway (9 coins max; the drain fails loudly at 60
> respins), so every case that needs the whole feature asserts `{facts.capped} == False` instead,
> which names the cap when it does bite.

---

## Not automated (2)

| Case | Why |
|---|---|
| ZZ-10 History on BO | The BO bet-history endpoint is not known to this repo and BO auth is currently rejected. ZZ-09 exposes the round's identity (`facts.normal_win_round_id` / `_stake` / `_payout`) so the manual comparison is a copy-paste. |

> **D4 is fixed in the build** (`docs/zeus-bridge-defects.md`): `getHistoryRows()` is implemented to
> the contract, so ZZ-09 now opens the newest row's detail ticket and asserts it exactly
> (`normal_win_history_rows >= 1`, `normal_win_detail_opened == True`) instead of grading the panel
> from video alone. The old per-game bridge docs (`bridge-api-v2.md`, `bridge-api-v21.md`) were STALE
> on this — they listed History rows as unresolvable — and v3 has since replaced them. Check `docs/game-bridge-contract.md` and probe
> the live bridge before concluding a getter is missing.
| ZZ-23 tab-switch mutes sound | Needs a browser visibility change with the sound getters polled from the backgrounded context. Small scenario, not yet written — and deliberately not written as an AI-only case, since "is it silent?" is what a video cannot answer and Zeus is the one build that can answer it exactly. |

> **ZZ-04 is now automated** (it was listed here). Bridge v3 puts `settings.defaultBetLevel` in
> `getGameInfo()`, closing D3 — but the case asserts `opens_at_expected_bet`, not
> `bet_index == default_bet_level`: the default governs only a player's FIRST session, and every
> later one opens at `lastBet.baseStake`, so the obvious assert fails a correct game on nearly every
> run. `boot-base` applies the documented rule and publishes which branch it took.

> **ZZ-12 is now automated** (it was listed here). It never needed a symbol-select affordance:
> `paytable-bet-scaling` shoots the SAME table at two bet levels, which is the case as written. It
> was unavailable to Zeus only because `line_bet()` lived on the `LineGame` profile — it is on
> `SlotGame` now, and Zeus's divisor resolves from `PAYLINES = 5`, exactly the formula the design
> doc states (`total bet / 5 x symbol multiplier`).

---

## First LIVE run (2026-08-26) — what it found

`python scripts/run_scenario.py --game zeus --env dev --suite .../zz-core.yml`

| Finding | Status |
|---|---|
| **`startGame()` never resolves.** Its promise is lost — the game reaches `appState: idle`, `sceneReady: true`, `canDo('spin').ok`, and `waitForSpinClickable` resolves in 0.3s, but the promise never settles. A bare `await page.evaluate(startGame())` hung the whole suite with no output. | **BRIDGE DEFECT — fix at source.** The runner now races the promise against a readiness poll (`se.start_game`) and continues with a loud warning, so a lost promise costs one poll interval instead of the run. |
| ~~Nothing opens the bet popup~~ — **superseded 2026-08-27.** `nextBet` DOES open it, with all 20 `bet:row:<i>` plus `bet:plus`/`bet:minus`/`bet:close`. The first probe polled 1.5s after the click and the popup had already closed itself. The real cause is **D1** below. | ZZ-02 / ZZ-03 remain **inconclusive** until D1 is fixed — the panel is only on screen ~0.3s. |
| **No default bet level is reported.** `getDefaultStake()` is undefined and `gameInfo.settings` carries only `compliance`. | ZZ-04 removed from the suite. The default-stake probe has since been dropped from `boot-base` entirely, so no game asserts on it. |
| **Cascade vocabulary applied to a payline game.** `base-cluster-win` requires `cascades >= 2`, which Zeus never produces — it spun its whole budget. The same flaw made `spin-then-history` burn 80 spins matching nothing, because its outcome hunt counted *cluster wins* (always 0 here). | Fixed. New `base-win` scenario (any paying round, mechanism-agnostic) now backs ZZ-06; `base-cluster-win` refuses a non-cascade profile immediately with a pointer to it; and `win_steps()` replaces `cluster_wins()` wherever the unit means "paying steps" — cascade steps on a cascade game, one-per-paying-round elsewhere. `cascade_win` is dropped from the default outcome hunt on a single-grid game. |
| `base-spin` / ZZ-05, `base-win` / ZZ-06 | ✅ **green** — `conservation` holds on live Zeus data; `base-win` found a paying round in 3 spins |
| `boot-base` / ZZ-01, `spin-then-history` / ZZ-09, `open-tutorial` / ZZ-11 + ZZ-52 | reach and record; graded once an AI provider is configured |

`browse-denominations` was initially passing its three `stream:` asserts **without ever opening a
list** — the denominations came from config, so they were true regardless. It now returns unreached
when the list cannot be shown, which is what turned that vacuous green into an honest inconclusive.

## Corrections from the first captured responses (2026-08-26)

Two real payloads (a trigger and a terminal full-grid round) contradicted assumptions taken from the
bridge doc. All four are fixed in the profile / invariants:

| Assumed | Actually |
|---|---|
| no `gameState` field; derive it from the counters | **`gameState` is present** (`2` trigger, `1` terminal) — the derivation is gone |
| `totalFreespin` | **`totalFreeSpin`** (the shared default) — and it is `0` even on the trigger, so nothing may key off "awarded" |
| no `baseStake`; reuse `stake` | **`baseStake` is present** and keeps the original while `stake` is `0` |
| a trigger can be recognised by counting Coins | the **terminal** round carries 9 Coins with base state and no respins — counting alone calls it a trigger. `is_trigger()` now checks state/counters first |

Also hardened: `freeSpinInfo` is **null** on the terminal round (reads as 0 remaining, which is
correct), and `holdspin_payout_matches_coins` now grades against `accumulatedWinAmount` instead of
summing every round's payout — summing would double-count a trigger that also won on a payline.

Verified: both captured payloads parse correctly and all six hold-spin invariants pass on the real
stream, on a synthetic count-down, and on an add-coin stream; each broken variant still trips its own
invariant.

## Open items

1. **Confirm `AUTO_COUNTS`.** The Zeus profile still carries the default `10/20/30/50/unlimited`.
   Zeus uses `auto:package:<i>`, and its packages may differ — if they do, ZZ-14 asserts the wrong
   number of spins. Read the live list and correct the profile.
2. **Confirm the Buy Bonus price multiplier.** ZZ-40 asserts only `price_ratio > 0` because the
   multiplier is server config. Read `facts.price_ratio` from one live run, then pin it.
3. ~~`getSpeedMode()` returns a boolean here~~ — **v3 returns Mystic's `'NORMAL' | 'TURBO' |
   `'UNKNOWN'` string** (bridge-api.md §12). ZZ-22 was unaffected because it asserts
   `facts.speed_toggled`, the COMPARISON computed in the scenario, rather than a mode name — which
   is the general lesson: assert the derived answer, not the raw dialect. `'UNKNOWN'` is a valid
   answer meaning *unanswerable*, and `has_speed_getter` keeps it distinct from a real failed
   toggle.
4. **Everything is LIVE-UNVERIFIED.** Built against the v2 bridge doc, the mechanism doc and the old
   suite; validated against synthetic streams and fixtures. The first live run is the real test.
   Two REAL responses have since been captured (`games/zeus/bet-response.md`) and corrected four
   things the docs alone had got wrong — see below.
5. **Win tiers** (`SLIM`/`SMALL`/`MEDIUM`/`BIG`/`MEGA`/`ULTRA`) are in the profile as
   `Zeus.win_tier()` but no case asserts them yet — the xlsx has no tier case. Worth adding, since
   the celebration shown for a win is exactly the kind of thing that regresses silently.
   ⚠ Assert against `Interface.ts` `WIN_RANGE`; `Constants.ts` exports a second, dead table.

### Last Bet — reopening the game (ZZ-53 … ZZ-58, from CR_092 … CR_096)

**Last Bet is a RESUME behaviour, not a panel**: the player reopens the game and it must come back on
the round they last played. The restore cases play a round, reopen, and check what came back
(`last-bet-restore`); the resume case reopens while a bonus is still OWED, after the denomination
changed on BO, so the reopen RESUMES it (`bonus-resume-denomination-swap`).

⚠ Every case gates on `info_captured`: `applyLastBet()` nulls `gameInfo.lastBet` at Start, so a live
read reports nothing on a session that restored perfectly.
⚠ The resume cases write BO and declare `teardown: reset-denomination`, which is not optional — a
ladder left on 11/22/33 breaks every later bet assert in the suite.
