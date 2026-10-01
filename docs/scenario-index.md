# Scenario index — what exists, and what it works on

**Read this before writing a test case for a new game, and before writing a new scenario.**
50 scenarios · 33 invariants · 7 games (Mystic · Zeus · Dragon · Dragon VIP · Chaos · Joker · Grandma Pets).

The whole point of the scenario/dialect split is that **most cases are not game-specific**. Money,
bets, denominations, auto-play, history, reconnect, language, spin inputs and popups are all
mechanism-free: they read the response through the active profile, so they work on a new game with
no code at all — just a profile that names its buttons.

> **The rule for adding one.** Write a new scenario ONLY when the game has a mechanism, feature or
> control that no existing scenario reaches. Not when the same situation needs different asserts —
> that is a new *case* on an existing scenario. Zeus needed 4 new scenarios out of 52 cases; Chaos
> needed 6 out of 76; Dragon, a Zeus fork, needed none; Dragon VIP needed one, for Premium Reel. If you are writing a fifth, something
> is probably reusable.

| | |
|---|---|
| Scenario code | `scripts/scenario_engine.py` |
| How to write one | `docs/scenario-authoring-rules.md` |
| How to write a case | `docs/scenario-assert-authoring.md` |
| What the bridge must expose | `docs/game-bridge-contract.md` |
| Per-game response shape | `games/<game>/bet-response.md` |

---

## 1. How to use this file

1. **Find the row for what you need to test** in §3 (by feature) or §4 (the full table).
2. **Check the capability column.** A scenario guarded by a flag your profile sets `False` reports
   *precondition not met* → inconclusive. That is the correct outcome for a feature the game does
   not ship — not a failure, and not a reason to write a new scenario.
3. **Bind its facts** (§4) in your asserts. Every AI assert needs a hard `stream:` companion from
   the same run, or a scenario that quietly did nothing produces a green.
4. **Only if nothing fits**, write one — then add a row here and to §5.

---

## 2. Applicability at a glance

Three mechanisms exist so far. A scenario is **portable** unless this table says otherwise.

| Mechanism | Profile base class | Games | What is different about it |
|---|---|---|---|
| generic | `SlotGame` | all | lifecycle, money, bets, UI — no mechanism assumptions |
| cascade | `CascadeGame` | Mystic | wins clear and symbols fall in; a round is many steps |
| hold & spin | `HoldSpinGame` | Zeus, Dragon, Dragon VIP | respins that HOLD symbols; grid-full ends it |
| line / ways | `LineGame` | Chaos, Joker | one grid per round, fixed paylines (Joker: + expanding wilds) |

**35 of the 47 scenarios are mechanism-free.** Only these 12 are tied to one:

| Scenario | Mechanism | Why it cannot port |
|---|---|---|
| `base-cluster-win` · `freespin-cascade` | cascade | asserts a multiplier that climbs per cascade STEP |
| `hold-spin-complete` · `hold-spin-full-grid` · `buy-bonus` | hold & spin | coin positions, respin counter, grid-full |
| `p1-ways-win` · `raining-wild` · `random-multiplier` | line (Chaos) | symbols this game alone has |
| `ante-toggle` | any game WITH ante | gated on `HAS_ANTE_BET`. Ported to Dragon VIP by `switch_feature()`, which drives a panel flow where Chaos has a one-click toggle |
| `ante-freespins` | any game WITH ante, on free spins | gated on `HAS_ANTE_BET`, and still reads Chaos's scatter census. A Hold & Spin bet with ante is `hold-spin-complete` with `feature: ante` |
| `premium-reel-spin` | any game WITH Premium Reel | gated on `HAS_PREMIUM_REEL` (Dragon VIP) |
| `buy-bonus-lucky-spin` | any game whose Buy Bonus raises a multiplier WHEEL | Joker. `buy-bonus` cannot be used there: the wheel must be answered or the game hangs |

---

## 3. By feature — start here

### Boot, bets and money

| I need to test | Use | Notes |
|---|---|---|
| the game loads into a usable idle screen | `boot-base` | places no bet. For "opens at the default bet" assert `opens_at_expected_bet`, **never** `opens_at_default` — the default governs only a player's FIRST session, and every later one opens at `lastBet.baseStake` |
| the denomination list matches config | `browse-denominations` | needs `HAS_BET_POPUP`; scrolls to both ends |
| changing the bet | `change-bet` | `facts.picked_bet` / `landed_bet` — assert the bet that LANDED |
| one plain spin, balance debited and credited | `base-win`, not `base-spin` | one WINNING round shows BOTH moves of the balance, so the debit and the credit are one run (CR_007+CR_008, ZZ-05+ZZ-06). `base-spin` is the thinnest bet-placing box, but on a loss the credit half has nothing on screen |
| a WINNING spin specifically | `base-win` (any) · `base-cluster-win` (cascade) | `base-win` hunts a win on any mechanism; trim the assert with `at: win` — the clip spans every hunting spin |
| balance too low to bet | `insufficient-balance` | pair with `teardown: restore-balance` |

### Auto-play

| I need to test | Use |
|---|---|
| it runs and completes its count | `auto-play` |
| it stops on a win | `auto-stop-at-win` |
| the player stops it | `auto-user-stop` |
| a reload cancels it | `auto-reload-cancels` |
| it stops when the money runs out | `auto-until-empty` |
| it uses the bet chosen beforehand | `auto-change-bet-first` |
| the Auto Play notification | `toggle-auto` |

> Every one of these ends with `confirm_stopped()` — it watches for a further bet (**nothing
> arriving is the evidence**), then holds so the still screen is filmed. Assert
> `{facts.extra_round_after_stop} == False` and `{facts.auto_flag_after} == False`; a clip that
> merely ends proves nothing (Rule 4).

### Bonus rounds

| I need to test | Use | Mechanism |
|---|---|---|
| a free-spin round end to end | `fs-complete` | any |
| a cascade win inside free spins | `freespin-cascade` | cascade |
| free spins with an empty wallet | `freespin-zero-balance` | any |
| history after a bonus | `freespin-then-history` | any |
| Hold & Spin, normal and full-grid | `hold-spin-complete` · `hold-spin-full-grid` | hold & spin |
| buying the bonus | `buy-bonus` | needs `HAS_BUY_BONUS` |
| buying a bonus that then offers a MULTIPLIER WHEEL | `buy-bonus-lucky-spin` | Joker. It CLICKS the wheel (`keep` / `spin`) — the bridge never answers it and `dismissPopups()` never closes it, so anything else leaves the game waiting |
| a Hold & Spin bet WITH Ante Bet or a Premium Reel | `hold-spin-complete`, `feature: ante` / `"premium:<reelId>"` | hold & spin |

### Paid spin features

| I need to test | Use |
|---|---|
| Ante Bet charges base × multiplier, and switches off again | `ante-toggle` — a one-click toggle or a panel flow, both through `switch_feature`; `spin_after_off` adds the plain spin after |
| a bonus bet with ante | `ante-freespins` (free spins, Chaos) · `hold-spin-complete` with `feature: ante` (Hold & Spin) |
| every Premium Reel package: its stake, its echo, what it excludes | `premium-reel-spin` |
| a Hold & Spin bet with a Premium Reel | `hold-spin-complete` with `feature: "premium:<reelId>"` |

### Spin input, turbo, sound, popups

| I need to test | Use |
|---|---|
| button / Space / hold / double-click | `spin-inputs` — `facts.click_rounds`, `key_rounds` count PAID rounds only |
| turbo on/off and hold-to-turbo | `turbo-modes` — it SPINS, so "do the reels resolve faster" has something to time; `facts.speed_toggled` is exact via `getSpeedMode()` |
| the Turbo notification | `toggle-turbo` — places NO bet, so use it only where the notification is the subject, never for reel speed |
| sound / music toggles | `sound-toggles` — needs `HAS_SOUND_GETTERS` for the exact half |
| several notifications at once | `stacked-popups` — `facts.max_concurrent_popups` |
| an anticipation glow | `reels-glow` |

### Text, language and panels

| I need to test | Use |
|---|---|
| every surface in N languages | `language-tour` — the one place `image:` beats `video:` |
| the language survives a relaunch | `language-switch` |
| many languages, shallow | `language-sweep` |
| the tutorial / info / game-help guide | `open-tutorial` — pages it and shoots each screen |
| a paytable at two bet levels | `paytable-bet-scaling` — portable since `line_bet()` moved to `SlotGame`; the divisor is `LINE_DIVISOR` or, failing that, the payline count |
| history rows and detail paging | `spin-then-history` · `freespin-then-history` |
| **Last Bet — the game reopens showing the round last played** | `last-bet-restore` — plays a round, RELOADS, and checks the session comes back on that round's stake and win. This is what "Last Bet" means: a resume behaviour, not a panel. `outcome:` names WHICH round to leave on record — `win` · `lose` · `any` · `freespin_trigger` (a bonus reached from the base game, **not** a bought one: the two charge different amounts, so never call either merely "bonus") |
| **a bonus RESUMED after the denomination changed on BO** — the bonus keeps its stake, the base game after it takes the new ladder's default | `bonus-resume-denomination-swap` — trigger, close at once (bonus still owed), swap to the automation ladder (11, 22, 33), reopen. A RESUME, not a restore. **Needs `teardown: reset-denomination`** |
| Last Bet auto-displayed before each bonus round | `last-bet-before-rounds` — only where the build flashes it between rounds (Mystic's spec). It does NOT reload, so it cannot answer the re-entry question above |

### Resilience

| I need to test | Use |
|---|---|
| a short disconnect | `reconnect-brief` |
| a long one that ends the session | `reconnect-long` |
| a disconnect during auto-play | `reconnect-during-auto` |
| the same account in two tabs | `multi-tab-session` |

---

## 4. The full table

`bets` = places a real bet, so **Rule 1 applies** (any spin can trigger a bonus) and the run
contributes to `conservation`. `shots` = captures stills, so an `image:` assert has something to
grade. `cap` = the profile flag that gates it.

| Scenario | bets | shots | cap | Used by | Key facts |
|---|:--:|:--:|---|---|---|
| `ante-freespins` | ● | ● | `ANTE_BET` | chaos | `ante_clickable_in_fs` `ante_frozen` `ante_verifiable_in_fs` `expected_award` |
| `ante-toggle` | ● | ● | `ANTE_BET` | chaos, dragon-vip | `stake_off/on` `base_off/on` `base_unchanged` `stake_ratio` `ante_engaged` `ante_switched_on/off` `feature_panel_blocked_while_on` `premium_blocked_while_on` `ante_off_spin_plain` |
| `auto-change-bet-first` | | | | all 5 | `picked_bet` `picked_index` `auto_count` |
| `auto-play` | ● | | | all 5 | `auto_count` `rounds_played` `completed` `stake_constant` + stop facts |
| `auto-reload-cancels` | | | | all 5 | `auto_cancelled` `spins_before_reload` + stop facts |
| `auto-stop-at-win` | | | | all 5 | `won_payout` `spins_to_win` `auto_stopped` + stop facts |
| `auto-until-empty` | ● | | | all 5 | `stopped_on_empty` `spins_played` `funded_balance` + stop facts |
| `auto-user-stop` | | | | all 5 | `spins_before_stop` `auto_stopped` + stop facts |
| `base-cluster-win` | ● | | | mystic | `cascades` `multipliers` `multiplier_climbed` `balance_after_win` — **cascade only** |
| `base-spin` | ● | | | mystic | `spin` |
| `base-win` | ● | | | chaos, dragon, dragon-vip, zeus | `win_payout` `win_stake` `spins_to_win` · the readout's three values: `balance_before_spin` `balance_while_spinning` `balance_after_win` |
| `bonus-resume-denomination-swap` | ● | ● | | all 5 | `trigger_base_stake` `swap_applied` `new_ladder` `new_default_bet` `client_on_new_ladder` `old_bet_on_new_ladder` `is_pending_freespin` `pending_stake_is_trigger` `resumed_rounds` `resume_bets` `bet_during_bonus_is_old` `bet_during_bonus_on_new_ladder` `bet_after_bonus` `bet_after_bonus_is_new_default` `bet_after_bonus_on_new_ladder` **(needs `teardown: reset-denomination`)** |
| `buy-bonus-lucky-spin` | ● | ● | `BUY_BONUS` | joker | `price` `price_ratio` `paid_stake` `package_spins` `awarded` · the wheel: `wheel_shown` `wheel_choice` `lucky_multiplier` `multiplier_constant` · the bonus: `fs_rounds` `bonus_total` `expanded_rounds` `expanded_reels` `capped` (the feature's return to base is spread in from the shared settle helper) |
| `boot-base` | | ● | | all 5 | `balance` `denom_count` `default_bet_level` · the first-session rule: `expected_open_bet` `opens_at_expected_bet` `first_session` `last_bet_stake` |
| `browse-denominations` | | | `BET_POPUP` | all 5 | `denom_count` `min_bet` `max_bet` `list_shown` `closed` |
| `buy-bonus` | ● | | `BUY_BONUS` | dragon, dragon-vip, zeus, grandma | `price` `price_ratio` `paid_stake` `packages` `confirmed` `capped` `bought_premium_id` `package_spins` `awarded` · Hold & Spin only: `trigger_coins` `final_coins` `coins_added` `grid_filled` · free spins: `fs_rounds` · with `sample_reveal: true`: `reveal_sampled` `reveal_stable` `reveal_distinct_grids` `reveal_bonus_reels` `reveal_max_bonus_per_reel` `reveal_wilds` `reveal_winning_run` `reveal_grid`. **The drain is chosen by `GAME.mechanism`** — Hold & Spin keeps its own drain (it grades `capped`), every other mechanism takes the free-spin drain, which also declines a Free Spin Extension |
| `change-bet` | | | | all 5 | `picked_bet` `landed_bet` `bet_changed` |
| `freespin-cascade` | ● | | | mystic | `fs_cascade` `fs_reset` — **cascade only** |
| `freespin-then-history` | ● | | | mystic | `bet_count` `expected_pages` `paged_back` |
| `freespin-zero-balance` | ● | | | chaos, mystic | `fs_rounds_resumed` `broke_at_trigger` |
| `drama` | ● | | | grandma | `rounds_played` `dramas_played` `rounds_roll_failed` `skip_reasons` `fights_cat_vs_dog` `fights_same_row_adjacent` `fights_vertical` `fights_per_round_max` `rounds_with_multiple_candidates` `winner_is_real_cell` `winner_family_matches_rule` `loser_is_fake` `fakes_all_loser_family` `fakes_within_column_cap` `real_losers_survive` `winner_survives_on_grid` `grid_matches_view_rounds` `drama_animation_rounds` `fakes_when_not_played` `skipped_mode_excluded` `min_drama_ms` `dev_switch_on` `play_in_turbo_config` `speed_mode` `autoplay_ran` · marks: first_drama, last_drama. **Stops at N DRAMAS, not N spins** — the roll is 70% on a big win and 4% otherwise, so a spin count can end with nothing to grade. ⚠ FAILS on a build with a drama dev switch set |
| `freespin-reload` | ● | | | grandma | `source` `awarded` `rounds_before_reload` `free_spins_before_reload` `pending_free_spins` `pending_game_state` `pending_is_bonus` `resumed_rounds` `first_resumed_free_spins` `buttons_polled` `buttons_flashed` `buttons_ever_enabled` `fs_rounds` · marks: reload, resume. `source: buy` reaches the bonus by purchase. ⚠ Unreached if the bonus ENDS before the requested number of rounds — a reload with nothing owed proves nothing |
| `fs-complete` | ● | | | chaos, mystic, grandma | `free_spins_played` `capped` `total_rounds` `fs_rounds` `counter_matches_rounds` `counter_readings` `counter_offsets` `triggers_skipped` `trigger_bonus_reels` `trigger_max_bonus_per_reel` · otherwise bind `{trigger.*}` / `{terminal.*}`. `arrive_when: {bonus_reels, min_on_one_reel}` hunts a trigger of a PARTICULAR SHAPE and plays out the ones that do not match |
| `hold-spin-complete` | ● | | | dragon, dragon-vip, zeus | `trigger_coins` `respins_played` `coins_added` `coin_total` `capped` · with `feature`: `feature_on_trigger` `trigger_stake_matches` `off_available_in_feature` — **hold & spin** |
| `hold-spin-full-grid` | ● | | | dragon, dragon-vip, zeus | `filled_on_trigger` `grid_filled` `capped` — **hold & spin** |
| `insufficient-balance` | | | | all 5 | `spin_balance` `chosen_bet` `min_bet` `mode` |
| `language-sweep` | | ● | | *unused* | `language_count` `failed` |
| `language-switch` | | ● | | chaos | `from_language` `to_language` `switched` |
| `language-tour` | | ● | | all 5 | `language_count` `requested` `surfaces` `shot_count` |
| `last-bet-before-rounds` | ● | | | chaos, dragon-vip, mystic | `trigger_stake` `fs_stake_zero` `fs_rounds` |
| `last-bet-restore` | ● | ● | | all 5 | `played_stake` `played_base_stake` `played_payout` `restored_stake` `restored_base_stake` `restored_payout` `stake_restored` `payout_restored` `bet_opens_at_last_stake` `changed_bet_to` `bet_moved_after_round` · on `outcome: freespin_trigger`, `trigger_stake` `trigger_base_stake` `terminal_base_stake` (the stake is read off the TRIGGER — a bonus round's own stake is 0) · on `change_bet_after: true`, `changed_bet_to` `bet_moved_after_round` `bet_opens_at_changed_value` · the gates: `info_captured` `last_bet_present` `resumed_rounds` |
| `multi-tab-session` | ● | | | all 5 | `second_opened` `wait_s` |
| `open-tutorial` | | ● | `TUTORIAL_WEBVIEW` | all 5 | `tutorial_open` `scroll_steps` `shot_count` `is_webview` |
| `p1-ways-win` | ● | | | chaos | `p1_ways` `illegal_reels` `expected_gross` `capped` — **line** |
| `paytable-bet-scaling` | | ● | | chaos, dragon, dragon-vip, zeus | `bet_a` `bet_b` `line_bet_a/b` `divisor` |
| `premium-reel-spin` | ● | | `PREMIUM_REEL` | dragon-vip | `packages_tested` `all_stakes_match` `all_ids_echoed` `all_bases_unchanged` `panel_blocked_while_on` `ante_blocked_while_on` `off_spin_plain` |
| `raining-wild` | ● | | | chaos | `wilds` `expected_raining` `control_wilds` — **line** |
| `random-multiplier` | ● | | | chaos | `base_multipliers` `fs_multipliers` `all_in_ladder` — **line** |
| `reconnect-brief` | ● | | | all 5 | `recovered` `offline_s` `played_after_reconnect` |
| `reconnect-during-auto` | ● | | | chaos, mystic | `recovered` `offline_s` |
| `reconnect-long` | ● | | | all 5 | `offline_s` |
| `reels-glow` | ● | | | mystic, grandma | `bonus_reels_hit` `bonus_reels` `p1_reels_hit` `p1_reels` `p1_on_reel2` `p1_on_reel3` · marks: spin. The facts are the FIXTURE GUARD: the glow itself has no bridge signal on any game, so they prove what landed before the video is asked what glowed |
| `sound-toggles` | | | `SOUND_GETTERS` | dragon, dragon-vip, zeus | `sound_before/after_off` `music_after_off` `toggled` |
| `walking-wild` | ● | | | grandma | `fs_rounds` `wild_counts` `wild_cells_match_view_rounds` `grid_matches_view_rounds` `walk_started_in_base` `fs_rounds_with_wilds` `multi_wild_rounds` `more_wild_rounds` `fewer_wild_rounds` `moved_wild_rounds` `wild_change_rounds` `walk_animation_rounds` `walk_overlapped_spin_rounds` `stand_in_rounds` `exit_animation_rounds` `wild_wins_use_parked_cells` `walk_finished_before_next_round` `min_walk_ms` `bonuses_played` `transitions_seen` `speed_mode` `stop_clicks` · marks: base_before, base_after, new_wild, more_wilds, fewer_wilds, moved_wild, multi_wild. Takes base spins BEFORE and AFTER on purpose — "a wild walks on only in free spins" needs the base game in the same recording |
| `spin-inputs` | ● | | | all 5 | `click_rounds` `key_rounds` `hold_done` `dblclick_done` |
| `spin-then-history` | ● | | | all 5 | `outcome_count` `outcomes_found` `missing` |
| `stacked-popups` | | ● | | chaos, mystic | `toggles_landed` `max_concurrent_popups` `popup_order` |
| `toggle-auto` | | | | chaos, mystic | `auto_started` `stop_clicked` `spins_played` `returned_to_idle` |
| `toggle-turbo` | | | | chaos, mystic | `speed_before` `speed_after_on` `speed_changed_on` `toggled_off` |
| `turbo-modes` | ● | | | all 5 | `speed_toggled` `has_speed_getter` `mouse_hold_done` `key_hold_done` |

---

## 5. Invariants — the `stream:` library

Named predicates over the response stream. **Portable ones first; the rest are gated by mechanism.**

| Invariant | Applies to | Checks |
|---|---|---|
| `conservation` | **any** | `balance == prev - stake + payout` |
| `terminal_reached` | **any** | the collected stream actually reached a terminal round |
| `stake_zero_in_freespins` | any with free spins | a free spin never costs the player |
| `freespins_monotonic` | any with free spins | the counter only counts down, or rises on a retrigger |
| `cascade_multiplier_climbs` · `cascade_multiplier_resets` | cascade | the ladder rises within a round, resets between |
| `holdspin_award` · `holdspin_coins_held` · `holdspin_reset_on_new_coin` · `holdspin_countdown` · `holdspin_ends_correctly` · `holdspin_payout_matches_coins` | hold & spin | the six Hold & Spin rules |
| `payout_matches_paylines` | line | credited == lines + scatter, then capped |
| `payline_rate_matches_paytable` | line + a BO paytable export | each `payoutRate` matches the configured table |
| `max_win_cap_applied` | any reporting `rawPayout` + `maxWin` | credit == `min(raw, cap)`, flag set honestly |
| `winlose_consistent` | any reporting `winlose` | `winlose == payout - stake` |
| `symbol_reel_restrictions` | any with reel-restricted symbols | a symbol never lands where its strip weight is 0 |
| `freespin_award` · `freespin_retrigger_adds` | Chaos rules | award = base(reels) × ways |
| `p1_reels_only` · `p1_ways_payout` · `scatter_pay_matches_p1_ways` | Chaos | the P1 ways scatter |
| `multiplier_in_ladder` | any with a multiplier ladder | every value is a configured one |
| `ante_stake_multiplier` · `ante_frozen_in_freespins` | any with ante | stake × N, payout base UNCHANGED |
| `premium_reel_stake` · `premium_reel_frozen_in_freespins` | any with Premium Reel | stake = base × package multiplier, base UNCHANGED; a bonus keeps exactly the package that opened it |
| `win_tier_bands` | any with win tiers | the tier matches the configured band |
| `wild_expands_only_in_freespins` | any with expanding wilds (Joker) | `views` may differ from `rawViews` ONLY on a round that cost nothing — the paytable enables expansion in `FreeSpinGame` and not in `BaseGame` |
| `wild_expansion_is_uniform` | any with expanding wilds (Joker) | an expanded reel was overwritten with ONE symbol, and that symbol had landed on it. Cells that kept their symbol are never a violation — that is the landing wild, or a blocked symbol |
| `freespin_multiplier_in_ladder` | any with a free-spin multiplier (Joker) | every free spin reports a multiplier the game offers (`LUCKY_MULTIPLIERS`); rounds reporting none are skipped, not failed |
| `ways_payout_matches` | **ways** (Grandma) | every `wayDetails` entry credited `symbolMultiplier × totalWildMultiplier × wayCount × baseStake`, and the round credited their sum — exact on all 8 entries of the five captures. ⚠ `baseStake`, never `stake`: a free spin costs 0 and still pays off the bet, so free spins are graded too, not skipped. ⚠ One symbol can produce SEVERAL entries (a reel holding both the symbol and a wild splits them), so the total is summed over entries, never one per symbol. Checks the response against ITSELF, never against the paytable file. Max-win truncations are skipped; a stream where everything was skipped FAILS rather than passing vacuously |
| `joker_payout_matches_paylines` | Joker | a PAID round credited `Σ payoutRate × (baseStake / betLines)` — exact on both captured base rounds. **No `multiplier` term** (see below). Free spins, max-win truncations and rounds with no paylines array are skipped, and a stream where everything was skipped FAILS rather than passing vacuously |

Several of the "Chaos" ones are only Chaos-shaped in their constants — `max_win_cap_applied`,
`winlose_consistent` and `symbol_reel_restrictions` read everything through `GAME.*` and should
work on another line game unchanged.

⚠ **`payout_matches_paylines` IS NOT ONE OF THEM**, and this paragraph used to say it was.
Measured against Joker 2026-09-20, it fails a line game that is not Chaos in two independent ways:

* `expected_payout`, `expected_line_win`, `win_lines`, `scatter_multiplier` and `bet_lines` are
  defined on `ChaosRunner`, **not** on `LineGame` — so pointing it at Joker raises
  `AttributeError` mid-run rather than returning a verdict;
* its formula multiplies by `GAME.multiplier(raw)`. On Joker that field is present and is **not**
  a payout factor: the captured win reads `multiplier: 2.5` and credited 1.5, not 3.75
  (`games/joker/bet-response.md` §3). A ported version would be red on a correct game.

`joker_payout_matches_paylines` is that game's own version. **`validate_suites.py` checks that an
invariant NAME resolves, not that the profile supports it**, so neither failure is caught before
a live run — check the helpers exist on the profile before reusing a mechanism invariant.

---

## 6. Onboarding a new game — the shortest path

1. **Write the profile** (`scripts/game_profile.py`): subclass the matching mechanism, declare the
   button/popup names and the `HAS_*` flags. Most games need nothing else.
2. **Capture BET responses** — one per shape (loss, win, trigger, bonus round) into
   `games/<game>/fixtures/bet-responses/`, and write `games/<game>/bet-response.md`. **Do not skip
   this.** Every counting rule is derived from those payloads; guessing a field name produces a
   suite that passes against nothing.
3. **Map each case to a row in §3.** Most will land on an existing scenario.
4. **Write the suites.** Group by feature, one file each.
5. **Only now**, write scenarios for what genuinely has no home — and update this file.

### What each game actually needed

| Game | Cases | New scenarios | Why |
|---|---|---|---|
| Mystic | 108 | — (the reference) | |
| Zeus | 52 | 4 | Hold & Spin (×2), Buy Bonus, sound getters |
| Chaos | 76 | 6 | Ante Bet (×2), P1 ways scatter, Raining Wild, random multiplier, two-bet paytable |
| Dragon | 52 | 0 | a Zeus fork — same design doc, paytable and response. A `Dragon(Zeus)` profile restating two button names carries Zeus's 50 cases over unchanged (`docs/dragon-coverage.md`) |
| Joker | 10 so far | 1 | A line game like Chaos, but its Buy Bonus raises a Lucky Spin WHEEL the bridge never answers — `buy-bonus` would hang the game, so `buy-bonus-lucky-spin` clicks the choice. Expanding wilds and the free-spin multiplier needed **no** scenario: they are invariants over `rawViews` vs `views` and `luckyMultiplier` (`docs/joker-coverage.md`) |
| Dragon VIP | 64 | 1 | Dragon plus Ante Bet and Premium Reel. Ante reused `ante-toggle` once `switch_feature` absorbed its panel flow, and a Hold & Spin bet with a feature reused `hold-spin-complete` (`feature:`). Only Premium Reel's base-game play needed `premium-reel-spin` (`docs/dragon-vip-coverage.md`) |

Chaos reuses **26** scenarios unchanged. Some of its early work turned out to be duplication and
was removed once compared against Mystic properly:

* a Chaos-specific *game-help* scenario **(deleted)** — Chaos's Dynamic Game Help sits behind the
  same `info` control as the tutorial and pages the same way, so `open-tutorial` already covered it.
  Its one real advantage, a `scroll_popup` fallback for builds with no WebView, was folded into
  `open-tutorial` and now benefits Zeus too.
* its Turbo and Auto notification cases were sitting on `stacked-popups`, whose rapid burst
  deliberately never dwells long enough for a notification to be read. They belong on
  `toggle-turbo` / `toggle-auto`, which pause after each click for exactly that reason.

Both were found by asking "does Mystic already do this?" rather than by a failing run — which is
what this index is for.

---

## 7. Keeping this file honest

The tables above are generated from the engine and the suites, not written from memory — and a
checker enforces that they stay that way:

```
python tests/chaos/build_scenario_index.py           # regenerates the data AND fails on drift
python tests/chaos/validate_suites.py                # chaos, the default
python tests/chaos/validate_suites.py --game zeus    # ...and every other game, one at a time
python tests/chaos/validate_suites.py --game mystic
python tests/chaos/validate_suites.py --game dragon
python tests/chaos/validate_suites.py --game dragon-vip
```

`validate_suites.py` is **per-game and must be run per-game**. It resolves `{facts.*}` against the
ACTIVE PROFILE, because a fact's existence can depend on it — `spin-then-history` fans out its
`cascade_win_*` facts only on a cascade mechanism. Running it as chaos and reading the result as
though it covered Zeus is the same class of false green it exists to prevent. Running it across all
three the first time found four silent-pass bugs that had never been checked.

`build_scenario_index.py` re-reads `scenario_engine.py` and every suite, writes
`tests/chaos/scenario-index-data.json`, then reconciles this file against it. It exits non-zero and
names the problem when:

* a scenario or invariant exists in the engine but is missing from these tables;
* the doc names a scenario or invariant that no longer exists (a rename, or something deleted);
* a table row quotes a `fact` its scenario does not actually expose;
* the header counts are stale.

That last group matters more than it looks. This file is a lookup table people trust while
onboarding a game, so a stale row does not merely mislead — it sends someone to write a new
scenario for something that already exists, which is the exact waste it is meant to prevent.

When you add a scenario: add its row to §4, list it under the right heading in §3, and if it is
mechanism-bound add it to the §2 exception table. When you add an invariant, add it to §5 with the
mechanism it applies to — an invariant filed under the wrong mechanism is worse than a missing one,
because it will be asserted on a game it cannot judge.
