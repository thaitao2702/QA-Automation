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
3. **Bind its facts** in your asserts — the scenario's `description` in
   `scripts/scenario_engine.py` lists what it exposes. Every AI assert needs a hard `stream:`
   companion from the same run, or a scenario that quietly did nothing produces a green.
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

Every table has the same four columns. **Needs** is what the game must have for the scenario to
reach its situation — a mechanism (cascade, hold & spin), a feature (free spins, Buy Bonus, Ante
Bet, Premium Reel), or both. `any` means it runs on every game. A game without what a row needs gets
an *inconclusive*, not a fail.

### Boot, bets and money

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| the game loads into a usable idle screen | `boot-base` | any | places no bet. The default bet applies only to a player's FIRST session; every later session opens at the stake it last played |
| the denomination list matches config | `browse-denominations` | any — a bet list to see both ends | places no bet. Opens the bet list and scrolls to both ends; on a game without a list it steps the stake a few notches up and back instead, which shows neither end |
| changing the bet | `change-bet` | any | places no bet |
| one plain spin, balance debited and credited | `base-win`, not `base-spin` | any | one winning round shows both the debit and the credit in one run; on a losing `base-spin` the credit has nothing on screen |
| a winning spin | `base-win` | any | spins until it wins. The clip covers every spin it took, so point the video assert at the win (`at: win`) |
| a winning spin with cascades | `base-cluster-win` | cascade | |
| balance too low to bet | `insufficient-balance` | any | pair with `teardown: restore-balance` |

### Auto-play

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| it runs and completes its count | `auto-play` | any | |
| it stops on a win | `auto-stop-at-win` | any | |
| the player stops it | `auto-user-stop` | any | |
| a reload cancels it | `auto-reload-cancels` | any | |
| it stops when the money runs out | `auto-until-empty` | any | |
| it uses the bet chosen beforehand | `auto-change-bet-first` | any | |
| the Auto Play notification | `toggle-auto` | any | |

> Every one of these keeps watching for a while after auto-play ends and films the still screen,
> so a case can check that no further spin started — a clip that merely ends proves nothing
> ([Rule 4](scenario-rules/rule-04-prove-stopped.md)).

### Bonus rounds

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| a free-spin round end to end | `fs-complete` | free spins | |
| a cascade win inside free spins | `freespin-cascade` | cascade + free spins | |
| free spins with an empty wallet | `freespin-zero-balance` | free spins | |
| Hold & Spin, normal and full-grid | `hold-spin-complete` · `hold-spin-full-grid` | hold & spin | |
| buying the bonus | `buy-bonus` | Buy Bonus | plays the bought bonus out, free spins or Hold & Spin |
| buying a bonus that then offers a multiplier wheel | `buy-bonus-lucky-spin` | Buy Bonus + multiplier wheel | Joker. Answers the wheel the way a player does (keep or spin), then plays the bonus |

A bonus bet with Ante Bet or a Premium Reel is under **Paid spin features**; history after a bonus
is under **Text, language and panels**.

### Paid spin features

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| Ante Bet charges base × multiplier, and switches off again | `ante-toggle` | Ante Bet | switches ante on, spins, switches it off; `spin_after_off: true` adds one plain spin after |
| a free-spin bet with ante | `ante-freespins` | Ante Bet + free spins | Chaos |
| a Hold & Spin bet with ante | `hold-spin-complete`, `feature: ante` | hold & spin + Ante Bet | |
| every Premium Reel package: its stake, its echo, what it excludes | `premium-reel-spin` | Premium Reel | |
| a Hold & Spin bet with a Premium Reel | `hold-spin-complete`, `feature: "premium:<reelId>"` | hold & spin + Premium Reel | |

### Spin input, turbo, sound, popups

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| button / Space / hold / double-click | `spin-inputs` | any | |
| turbo on/off and hold-to-turbo | `turbo-modes` | any | spins, so "do the reels resolve faster" has something to compare |
| the Turbo notification | `toggle-turbo` | any | places NO bet, so use it only where the notification is the subject, never for reel speed |
| sound / music toggles | `sound-toggles` | any | the on/off state is checked exactly only on games that report it; elsewhere only on screen |
| several notifications at once | `stacked-popups` | any | |
| an anticipation glow | `reels-glow` | an anticipation glow | Mystic, Grandma |

### Text, language and panels

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| every surface in N languages | `language-tour` | any | the one place `image:` beats `video:` |
| the language survives a relaunch | `language-switch` | any | |
| many languages, shallow | `language-sweep` | any | |
| the tutorial / info / game-help guide | `open-tutorial` | any | pages it and shoots each screen |
| a paytable at two bet levels | `paytable-bet-scaling` | any | |
| history rows and detail paging | `spin-then-history` | any | |
| history after a bonus | `freespin-then-history` | free spins | |
| **Last Bet — the game reopens showing the round last played** | `last-bet-restore` | any; `outcome: freespin_trigger` needs free spins | plays a round, RELOADS, and checks the session comes back on that round's stake and win. This is what "Last Bet" means: a resume behaviour, not a panel. `outcome:` names WHICH round to leave on record — `win` · `lose` · `any` · `freespin_trigger` (a bonus reached from the base game, **not** a bought one: the two charge different amounts, so never call either merely "bonus") |
| **a bonus RESUMED after the denomination changed on BO** — the bonus keeps its stake, the base game after it takes the new ladder's default | `bonus-resume-denomination-swap` | free spins or hold & spin | trigger, close at once (bonus still owed), swap to the automation ladder (11, 22, 33), reopen. A RESUME, not a restore. **Needs `teardown: reset-denomination`** |
| Last Bet auto-displayed before each bonus round | `last-bet-before-rounds` | free spins + a Last Bet display between rounds | Mystic's spec. It does NOT reload, so it cannot answer the re-entry question above |

### Resilience

| I need to test | Use | Needs | Notes |
|---|---|---|---|
| a short disconnect | `reconnect-brief` | any | |
| a long one that ends the session | `reconnect-long` | any | |
| a disconnect during auto-play | `reconnect-during-auto` | any | |
| the same account in two tabs | `multi-tab-session` | any | |

---

## 4. The full table

`bets` = places a real bet, so **Rule 1 applies** (any spin can trigger a bonus) and the run
contributes to `conservation`. `shots` = captures stills, so an `image:` assert has something to
grade. `cap` = the profile flag that gates it.

| Scenario | bets | shots | cap | Used by |
|---|:--:|:--:|---|---|
| `ante-freespins` | ● | ● | `ANTE_BET` | chaos |
| `ante-toggle` | ● | ● | `ANTE_BET` | chaos, dragon-vip |
| `auto-change-bet-first` | | | | all 5 |
| `auto-play` | ● | | | all 5 |
| `auto-reload-cancels` | | | | all 5 |
| `auto-stop-at-win` | | | | all 5 |
| `auto-until-empty` | ● | | | all 5 |
| `auto-user-stop` | | | | all 5 |
| `base-cluster-win` | ● | | | mystic |
| `base-spin` | ● | | | mystic |
| `base-win` | ● | | | chaos, dragon, dragon-vip, zeus |
| `bonus-resume-denomination-swap` | ● | ● | | all 5 |
| `buy-bonus-lucky-spin` | ● | ● | `BUY_BONUS` | joker |
| `boot-base` | | ● | | all 5 |
| `browse-denominations` | | | `BET_POPUP` | all 5 |
| `buy-bonus` | ● | | `BUY_BONUS` | dragon, dragon-vip, zeus, grandma |
| `change-bet` | | | | all 5 |
| `freespin-cascade` | ● | | | mystic |
| `freespin-then-history` | ● | | | mystic |
| `freespin-zero-balance` | ● | | | chaos, mystic |
| `drama` | ● | | | grandma |
| `freespin-reload` | ● | | | grandma |
| `fs-complete` | ● | | | chaos, mystic, grandma |
| `hold-spin-complete` | ● | | | dragon, dragon-vip, zeus |
| `hold-spin-full-grid` | ● | | | dragon, dragon-vip, zeus |
| `insufficient-balance` | | | | all 5 |
| `language-sweep` | | ● | | *unused* |
| `language-switch` | | ● | | chaos |
| `language-tour` | | ● | | all 5 |
| `last-bet-before-rounds` | ● | | | chaos, dragon-vip, mystic |
| `last-bet-restore` | ● | ● | | all 5 |
| `multi-tab-session` | ● | | | all 5 |
| `open-tutorial` | | ● | `TUTORIAL_WEBVIEW` | all 5 |
| `p1-ways-win` | ● | | | chaos |
| `paytable-bet-scaling` | | ● | | chaos, dragon, dragon-vip, zeus |
| `premium-reel-spin` | ● | | `PREMIUM_REEL` | dragon-vip |
| `raining-wild` | ● | | | chaos |
| `random-multiplier` | ● | | | chaos |
| `reconnect-brief` | ● | | | all 5 |
| `reconnect-during-auto` | ● | | | chaos, mystic |
| `reconnect-long` | ● | | | all 5 |
| `reels-glow` | ● | | | mystic, grandma |
| `sound-toggles` | | | `SOUND_GETTERS` | dragon, dragon-vip, zeus |
| `walking-wild` | ● | | | grandma |
| `spin-inputs` | ● | | | all 5 |
| `spin-then-history` | ● | | | all 5 |
| `stacked-popups` | | ● | | chaos, mystic |
| `toggle-auto` | | | | chaos, mystic |
| `toggle-turbo` | | | | chaos, mystic |
| `turbo-modes` | ● | | | all 5 |

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
