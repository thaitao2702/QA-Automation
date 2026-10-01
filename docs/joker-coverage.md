# Joker: Where's My Banana — test-case coverage (JK-01 … JK-69)

Joker is the **second line game** (Chaos was the first) and the first with **expanding wilds** and a
**Buy Bonus that raises a multiplier wheel**. Where a case is mechanism-free it follows Dragon's
numbering — **JK-NN is the same claim as DR-NN**, on the same scenario — so the four games' results
line up row for row. What is genuinely Joker's own starts at JK-60.

- Profile: `scripts/game_profile.py` → `JokerBanana(LineGame)`
- Bridge: `games/joker/docs/raw/bridge-api.md` (`JokerBananaBridgeV3`)
- Response contract: `games/joker/bet-response.md` — **computed from three captured payloads**, not
  read off a spec
- The feature: `games/joker/expanding-wild.md`
- Suites: `games/joker/test-suites/jk-{core,autoplay,input-sound-reconnect,lastbet,features}.yml`
- Fixtures: `games/joker/paytables/` — `base.json` + `joker-fs-easy.json` (its README has the
  derivation, the measured odds and one open question)
- Config: `games/joker/env.config.dev.yml` (**a template**, see below)

```
python tests/chaos/validate_suites.py --game joker        # 63 cases, OK — no browser needed
python scripts/probe_bridge.py --game joker --env dev     # once the env config is filled in
python scripts/run_scenario.py --game joker --env dev --suite games/joker/test-suites/jk-features.yml
```

---

## ⚠ Before the first live run

**Nothing here has run against a Joker build.** Three things stand between the suites and a run,
and the first is a hard blocker.

1. **Fill in `games/joker/env.config.dev.yml`.** Six values are not knowable from anything in this
   repo: the dev build path, the Playsite launch code and currency, and BO's `game_id`,
   `currency_id` and `game_code`. They are left empty and the `bo:` block is commented out on
   purpose — a `paytable:` run then fails with *"paytable requested but BO not configured"* and is
   SKIPPED, which is far better than running unforced and hunting a 1-in-236 trigger.
   ⚠ `bo.paytable_code` needs reading off BO, not guessing: Zeus spells it `AutomationTest` and
   Chaos `Automation_test`. Writing to a code this game does not carry leaves it on its real
   weights **while the run still reports the paytable was set**.
2. **Probe the build.** Check `getGameInfo()` is captured pre-Start and `isGameInfoCaptured()`
   answers. Check `betPlus` / `betMinus` really open the `denomination` list, that `auto` doubles
   as Stop during a run, and that `holdSpinStart()` works (it THROWS when `canDo('spin')` is not
   ok — see JK-46). Confirm `turbo` resolves rather than being compliance-disabled.
3. **Take the five captures `games/joker/bet-response.md` §7 lists.** Everything marked ⚠ below
   traces back to one of them.

---

## What is Joker's own, and what it changed

| Fact | Where it came from | Effect on the suites |
|---|---|---|
| Every response carries the reels TWICE — `rawViews` as they landed, `views` after expansion | the captures | the expanding wild is asserted by DIFFING them, never from pixels. Three invariants rest on it |
| `BaseGame.EnableWildExpanding: false` · `FreeSpinGame: true` | `paytable.json` | `wild_expands_only_in_freespins` has a config-level authority behind it, not a help-page claim |
| ⚠ **`multiplier` is NOT a payout factor** — the captured win reads 2.5 and credited 1.5, not 3.75 | the captures | `F_MULTIPLIER` is deliberately unmapped, and Chaos's `payout_matches_paylines` **must not be pointed at this game** (below) |
| `payout = Σ payoutRate × (stake / betLines)`, betLines fixed at 20 | the captures | `joker_payout_matches_paylines`, and **JK-12's divisor is 20, not Dragon's 5** |
| ⚠ inside a bonus `id ≠ roundId` — `id` is the spin, `roundId` the BONUS | the captures | anything correlating rounds by `roundId` sees all 12 free spins as one round. JK-09 is scoped to a BASE round for that reason |
| Buy Bonus raises a Lucky Spin wheel the bridge never answers, and `dismissPopups()` never closes | bridge §12.5 | `buy-bonus` would HANG the game. `buy-bonus-lucky-spin` exists for this and clicks the choice |
| ⚠ **A purchase does not jump into free spins** — the response is trigger-shaped and the client SPINS THE REELS and shows a free-spin trigger (`buyBonusReveal`, a "fake 5-bonus spin") before the bonus starts; the wheel comes after THAT, and the chosen multiplier then sits directly above the reels for the whole bonus | the game team, 2026-09-21; corroborated by bridge §10 / §12.5 | JK-39 is that presentation. The Buy Bonus clip is now one run-level video across five cases, and its steps are described in order — the old prompt skipped the reveal entirely and described a screen the player never sees |
| 5 scatters → 20 free spins, and `Packages[0].NoOfSpins` is 20 | `paytable.json` | why the reveal can be shown as a REAL trigger: the fake grid is the one that would naturally have awarded what was bought |
| `defaultBetLevel` is 1-BASED | bridge §3 and §15, stated outright | JK-04 keeps the contract's 0-based reading on purpose (open item 1) |
| Both stake controls OPEN the denomination list | bridge §7.1 | every bet change is a row picked from a list, as on Dragon |
| The main `sound` button flips sound AND music together; Settings separates them | bridge §7.1, §9 | `sound-toggles` uses the Settings controls — that separation is the only reason JK-24 and JK-25 are two claims |
| A reload REPLAYS the last round's win, wild expansion included, with no BET | bridge §12.7 | the JK-18 prompt says a single win presentation after a reload is expected and is not a spin |
| The out-of-money popup's OK button RELOADS THE PAGE | bridge §12.9 | nothing presses it; `dismissPopups()` hides it without running the OK action |
| Toasts are listed in `visiblePopups` but never block spin readiness | bridge §3, §5 | prompts say a fading banner is expected; no case asserts on popups mid-run |

### Two corrections this onboarding made to shared code

**`HAS_SOUND_GETTERS` was `False`**, with the note "bridge §9 lists no
isSoundEnabled()/isMusicEnabled()". §9 lists **both**. `sound-toggles` returns *unreached* on a
False flag, so JK-24/25 would have been permanently inconclusive — green-adjacent, proving nothing
— rather than failing. Corrected in the profile.

**`docs/scenario-index.md` claimed `payout_matches_paylines` "will work on the next line game
unchanged".** Joker IS that game, and it does not, in two independent ways:

* `expected_payout`, `expected_line_win`, `win_lines`, `scatter_multiplier` and `bet_lines` live on
  `ChaosRunner`, not on `LineGame` — pointing it here raises `AttributeError` mid-run;
* its formula multiplies by `multiplier`, which this game's own captures disprove.

`joker_payout_matches_paylines` is the version that holds here (verified against all three captures
and five mutations, including both of those traps). ⚠ **`validate_suites.py` checks that an
invariant NAME resolves, not that the profile supports it**, so neither failure would surface until
a live run.

---

## Automated — 63 cases in 5 suites

| Case | Scenario | Suite |
|---|---|---|
| JK-01 UI at load · JK-04 opens at the default bet | boot-base (one run) | jk-core |
| JK-02 denominations = config | browse-denominations | jk-core |
| JK-03 bet +/− | change-bet | jk-core |
| JK-05 stake debited · JK-06 payout credited | base-win (one run) | jk-core |
| JK-07 insufficient balance | insufficient-balance | jk-core |
| JK-09 in-game History | spin-then-history | jk-core |
| JK-11 paytable UI · JK-52 tutorial flow | open-tutorial (one run) | jk-core |
| JK-12 payouts scale with the bet | paytable-bet-scaling | jk-core |
| JK-51 multi-language | language-tour | jk-core |
| JK-34 the language survives a relaunch | language-switch | jk-core |
| JK-35 free spins on a zero balance | freespin-zero-balance, forced | jk-core |
| JK-36 the History ticket of a BONUS pages every spin | freespin-then-history, forced | jk-core |
| JK-13 starts · JK-14 stops at count · JK-19 bet locked · JK-20 counter | auto-play (one run) | jk-autoplay |
| JK-15 stop at any win | auto-stop-at-win | jk-autoplay |
| JK-08 + JK-16 stops when the money runs out | auto-until-empty (one run) | jk-autoplay |
| JK-17 user stops · JK-18 reload cancels · JK-21 bet chosen first | three runs | jk-autoplay |
| JK-31 the Auto Play notification | toggle-auto | jk-autoplay |
| JK-44 … JK-50 every spin input | spin-inputs (one run, seven verdicts) | jk-input-sound-reconnect |
| JK-22 turbo | turbo-modes | jk-input-sound-reconnect |
| JK-24 sound off · JK-25 music off | sound-toggles (one run) | jk-input-sound-reconnect |
| JK-26 short drop · JK-27 + JK-28 long drop · JK-29 second tab | three runs | jk-input-sound-reconnect |
| JK-30 the Turbo notification | toggle-turbo | jk-input-sound-reconnect |
| JK-32 + JK-32b queued notifications | stacked-popups | jk-input-sound-reconnect |
| JK-33 disconnect DURING auto-play | reconnect-during-auto | jk-input-sound-reconnect |
| **JK-53 … JK-56 reopening restores the round last played** | last-bet-restore × 4 | **jk-lastbet** |
| **JK-57 + JK-58 a bonus resumed after a BO denomination swap** | bonus-resume-denomination-swap | **jk-lastbet** |
| **JK-60 · JK-61 · JK-62 expanding wild + JK-68 · JK-69 free-spin multiplier and award** | fs-complete, forced — ONE run, one clip, five verdicts | **jk-features** |
| **JK-39 + JK-63 … JK-67 Buy Bonus: the reveal spin, the wheel, the bonus** (`wheel: keep`, one clip) | buy-bonus-lucky-spin | **jk-features** |
| **JK-37 the 100x price follows the stake** (`level: 2`) | buy-bonus-lucky-spin | **jk-features** |
| **JK-38 SPINNING the wheel sets the multiplier** (`wheel: spin`) | buy-bonus-lucky-spin | **jk-features** |

### One run per feature, not one per case

`jk-features.yml` is **4 runs for 13 cases**. The expanding wild and the free-spin multiplier
were two identical `fs-complete` runs until 2026-09-21 — same fixture, same cap, same teardown —
split only because the two claims wanted different assert sets. The asserts were always
per-case; only the RUN was duplicated, and with it a second forced-paytable push, a second bonus
and a second AI call for the same footage. Merged, one bonus now answers all five (Rule 16).
The two runs that could NOT merge say why where they sit: `level: 2` buys at a different stake,
and `wheel: spin` takes the wheel's other branch — §7.2 says both wheel buttons "go dead as soon
as either is clicked", so `keep` and `spin` are mutually exclusive one-shot paths.

### A note on the numbering

JK-NN = DR-NN wherever Dragon has the same claim. **JK-30, JK-32, JK-33 and JK-34 … JK-36 are
not in Dragon's list** — they are cases Chaos and Mystic carry that Dragon never ported. They
sit in the 30s because that block is Dragon's **Hold & Spin**, a mechanism this game does not
have, so the range is free and the mapping is not disturbed anywhere it exists.

### Determinism

The two natural-trigger runs declare `paytable: { ref: joker-fs-easy, max_win_x: 20 }`. Unforced,
a bonus is **1 in 236 base spins**; forced, it is **1 in 3.5**, and always on exactly three reels —
the shortest award the game has. `max_win_x: 20` is 20× the live base bet (not 20 units of
currency), the Rule 17 default; what it costs is LENGTH, which the runs guard with
`{facts.free_spins_played} >= 2`, and `>= 4` on JK-62 where the clip has to SHOW an expansion.
The numbers and the one open question are in `games/joker/paytables/README.md`.

JK-35, JK-36 and JK-55 … JK-58 force the same fixture for the same reason — none of them can be
graded without a bonus, and unforced that is 1 in 236 spins. **JK-55 and JK-57/58 are the one
place in these suites that keeps a LOOSE `max_win_x: 500`**, and Rule 17 is why rather than an
exception to it: a tight ceiling would change the answer. Bridge §3 states the mechanism —
*"a max-win truncated `lastBet` still shows spins left that are forfeited"* — so a cap that bit
would make a finished bonus read as a live one, `resumed_rounds == 0` would fail on a healthy
client, and the case would be measuring the cap.

The Buy Bonus runs force nothing — buying IS the deterministic entry.

---

## Not automated, and what each needs

| | |
|---|---|
| **JK-10** History on BO | MANUAL, as DR-10 / ZZ-10 — the BO bet-history endpoint is not known to this repo. JK-09 publishes the round's identity so the comparison is a copy-paste |
| **JK-23** tab-switch mutes all sound | needs a browser visibility change with the sound getters polled from the backgrounded context. Deliberately not an AI-only case: "is it silent?" is what a video cannot answer and this build CAN answer exactly |
| **JK-59 — reopening after a PROMO round** | needs a SEEDED PROMO ACCOUNT; `HAS_PROMO` is true "only when the session has a promo" (bridge §11), so no local setup reaches it. ⚠ When written: `waitForSpinClickable` REFUSES in `mode:PROMO` (§15), so promo spins need `waitForButtonClickable('spin')` instead, and the promo popups close through their own buttons — hiding one without firing its button hangs a promise-driven popup |
| **The LUCKY SPIN resume** — Joker's own third resume shape | `pending.gameState == 3` with spins owed means the player closed the game AT THE WHEEL, and on reopen `luckySpin` comes up waiting for a choice (§12.5). **No other game in this repo has a resume that blocks on a player decision**, and ⚠ `dismissPopups()` never closes it — a run that reopens into it and walks away hangs the game AND poisons the next run. Neither existing scenario covers it: `bonus-resume-denomination-swap` closes after a TRIGGER (gameState 2), `last-bet-restore` plays everything out first. Needs a new scenario reusing `buy-bonus-lucky-spin`'s wheel helper |
| **CR_094 — reopening after a BOUGHT bonus** | JK-55 covers the TRIGGERED route. The bought one is not interchangeable: a trigger charges the base stake, a purchase charges 100× it, so the record should carry the PRICE. The right shape is a third `last-bet-restore` outcome, `buy_bonus`, delegating to the same helper so the wheel is answered rather than abandoned |
| **Cancelling a Buy Bonus purchase** | `buyBonus:cancel` is the confirm sheet's "No" (bridge §7.2) — the last point before a 100× debit. The claim is that nothing is charged and no bonus awarded. `buy-bonus-lucky-spin` always clicks confirm; needs a `confirm: false` path |
| **Buying with too little balance** | §12.5 step 4: "not enough balance → 'message' opens instead, no request sent". JK-07 covers the same refusal on a plain spin; this is the 100×-price version, and the likelier one to be hit. ⚠ Rule 7: it moves the money, so `teardown: restore-balance` |
| **The Buy button DURING free spins** | §7.1: `buyBonus` is "Hidden during free spins". `getButtonRect('buyBonus')` returning `null` mid-bonus is the whole test — an `on_round=` read on an existing drain, not a new run |

---

## Deliberately NOT applicable — 15 of the 47 scenarios

Checked against the registry, not assumed. Nothing here is an oversight:

| Scenario(s) | Why not |
|---|---|
| `hold-spin-complete` · `hold-spin-full-grid` · `buy-bonus` | **hold & spin**. `buy-bonus` is the trap of the three: it would open the purchase and walk away from the Lucky Spin wheel, which HANGS the game and poisons the next run. `buy-bonus-lucky-spin` exists for exactly that |
| `base-cluster-win` · `freespin-cascade` | **cascade** — they assert a multiplier that climbs per cascade STEP, and a Joker round is one grid |
| `p1-ways-win` · `raining-wild` · `random-multiplier` | Chaos's own symbols. ⚠ `random-multiplier` is the tempting one — `HAS_MULTIPLIER_LADDER` is true here — but this game's `multiplier` field is **not a payout factor** at all, so the case would be about a number that does nothing |
| `ante-toggle` · `ante-freespins` | `HAS_ANTE_BET` is false; `anteBet` always returns `null` (bridge §7.1) |
| `premium-reel-spin` | `HAS_PREMIUM_REEL` is false (Dragon VIP only) |
| `reels-glow` | Mystic's anticipation-glow feature |
| `last-bet-before-rounds` | Mystic's spec: a Last Bet panel that FLASHES between bonus rounds. `HAS_LAST_BET_PANEL` is false here and no such surface is in the bridge's popup list |
| `base-spin` | the thinnest bet-placing box, and redundant: JK-05/06 use `base-win`, which shows BOTH moves of the balance (`base-spin` on a loss has nothing to show for the credit half) |
| `language-sweep` | used by no game. `language-tour` (JK-51) covers four languages across every surface, and `language-switch` (JK-34) covers the relaunch — a shallow many-language pass adds nothing neither already has |

---

## Open items

1. **`defaultBetLevel` 1-based vs 0-based.** Bridge §3 and §15 both say the client reads it 1-based;
   the contract and `boot-base` read it 0-based. Only a FIRST session takes that branch. JK-04 keeps
   the contract's reading, so a red with `first_session == True` and `bet_amount` one denomination
   below `default_bet_amount` is the predicted off-by-one — a client defect if the server means
   0-based. Settling it needs a real first-session fixture.
2. ⚠ **Is the free-spin award multiplied by *ways*?** Chaos ships the same paytable schema from the
   same server, and a captured Chaos payload proved its award is `base(by reels) × ways`
   (`ChaosRunner.expected_fs_award`: counts `[1,2,0,2,0]` awarded 48, not 12). Joker is a line game
   and `FREESPIN_AWARDS` assumes the plain reading, but **nothing captured on Joker settles it**.
   JK-69 is written to hold under both. The first live run settles it.
3. **Which reels a wild may expand on.** `WILD_EXPAND_REELS = (2, 4)` records the help's claim, and
   NOTHING asserts it: the one capture expanded reel id 4, which fits both a 1-based and an
   off-by-one reading. It is also why JK-67 does not assert `{facts.expanded_rounds} >= 1` — if only
   one reel expands, that assert is a ~1-in-100 red on a correct game.
4. **The blocked symbols.** `ExcludedWildExpandingSymbols` is `["FreeSpin!", "Power!"]`, and
   `wild_expansion_is_uniform` covers only the structural half (an expansion writes ONE symbol).
   Asserting that the cells left alone really are those two needs the symbol id map from
   `getGameInfo().paytable` plus a capture where one shares a reel with an expanding wild.
5. **How the multiplier enters a WINNING free spin's payout.** The only captured free spin paid 0,
   so `payout = Σ rate × line_bet × luckyMultiplier` is a guess —
   `joker_payout_matches_paylines` SKIPS free spins for exactly this reason. One paying free spin
   settles it and turns the skip into a second formula.
6. **`canBuyExtension` / `extensionPhase` / `originalTotalFreeSpins*`** are zero or false in every
   capture. They describe a feature none of these rounds exercised; worth asking the game team
   whether it ships.
