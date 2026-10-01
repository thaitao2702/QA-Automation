# Treasures of the Dragon — test-case coverage (DR-01 … DR-52)

Dragon is a **fork of Zeus**, and three facts decided how it was onboarded:

| | |
|---|---|
| Design doc | `games/dragon/docs/raw/Doc Zappin zeus.md` is **byte-identical** to Zeus's |
| Server paytable | `games/dragon/docs/raw/paytable.json` is **byte-identical** to Zeus's (and to `games/zeus/paytables/base.json`) |
| Bridge | a port of `ZeusBridgeV3`: the same BET response (§11), a slightly different dialect (§13) |

So these are Zeus's cases, not new ones. **DR-NN is ZZ-NN** of `test-case-zeus.xlsx`, on the same
scenario, the same invariants and the same forced paytables. No new scenario and no engine change
was needed. Dragon's whole game-specific surface is a `Dragon(Zeus)` profile that restates two
button names and the autoplay counts.

- Profile: `scripts/game_profile.py` → `Dragon(Zeus)`
- Bridge: `games/dragon/bridge-qa-api.md` (v3, `dragon-bridge-v3/2026-09-11`)
- Suites: `games/dragon/test-suites/dr-{core,autoplay,holdspin,input-sound-reconnect}.yml`
- Fixtures: `games/dragon/paytables/` (Zeus's, copied unchanged; its README says why that is sound)
- Config: `games/dragon/env.config.dev.yml` (**incomplete**, see below)

```
python tests/chaos/validate_suites.py --game dragon      # 50 cases, OK — no browser needed
python scripts/probe_bridge.py --game dragon --env dev   # once the env config is filled in
python scripts/run_scenario.py --game dragon --env dev --suite games/dragon/test-suites/dr-holdspin.yml
```

---

## ⚠ Before the first live run

Nothing here has run against a Dragon build yet. Three things stand between the suites and a run:

1. **Fill in `games/dragon/env.config.dev.yml`.** The Playsite game code, the dev build path and the
   BO game id are not known to this repo, so they are left empty on purpose. With Zeus's values the
   runner would launch Zeus, and the Dragon cases would grade the wrong game and could pass. With
   them empty, the runner fails loudly.
2. **Probe the build** (bridge §14). Check that `diagnose().rawHooksInstalled` is true and
   `gameInfoSource` is `'raw'`. Check that `turbo`, `auto` and `stopAuto` resolve to the controls
   actually on screen. Check that a resumed Hold & Spin plays out and `waitForSpinClickable` then
   resolves.
3. **Capture BET responses** into `games/dragon/fixtures/bet-responses/` and write
   `games/dragon/bet-response.md` (`docs/scenario-index.md` §6 step 2). The profile inherits Zeus's
   reading of the response on the strength of bridge §11 alone. One trigger round and one terminal
   round would confirm it. Zeus's own reading was corrected four times that way.

---

## What differs from Zeus, and what it changed

This is bridge §13, filtered to the differences that reach a case.

| Difference | Effect on the suites |
|---|---|
| Autoplay panel: `auto:count:{i}` / `auto:close` are canonical (Zeus: `package` / `cancel`, aliases here) | the two button names the profile overrides |
| `AUTO_COUNTS` published as `[10, 20, 30, 50, ∞]` (§9) | DR-14's count is backed by the doc. On Zeus it is still an open item |
| `stopAuto` is the only control that ends a run | none: `auto_stop_button()` already prefers `auto_stop_main` |
| Hidden or gated control → `null` (Zeus: `{visible:false}`) | a probe reads it as absent. The DR-19 / DR-38 prompts accept "hidden" as locked |
| `buyBonusSelectedPrice` = selected stake × package multiplier (Zeus: the package's price) | **DR-40 is tighter than ZZ-40**: the quoted total must equal the stake the server charged |
| `buyBonus:package:{i}` picks a STAKE, not a price tier | `price_ratio` is not the multiplier (open item 2). The prompt says a package pick may change the stake |
| `buyBonus:confirm` resolves only while the confirm sheet is up | none: `buy-bonus` already clicks `buy`, then `confirm` |
| Toasts (`turboNotify` / `autoNotify`) are listed in `visiblePopups` | prompts say a fading banner is expected. No case asserts on popups mid-run |
| The client treats `defaultBetLevel` as 1-based | DR-04 keeps the contract's 0-based reading on purpose (open item 1) |
| The main `sound` button toggles sound and music together | none: `sound-toggles` uses `settings:sound` / `settings:music` |
| Holding Spin engages turbo | the DR-46 prompt says faster spins during a hold are expected |
| Unlimited autoplay reports `Infinity` | none: nothing asserts `auto_spins_*` on an unlimited run |

One deliberate difference from Zeus's suite: every forced Hold & Spin run carries the loose
`max_win_x: 500` backstop. `zz-holdspin.yml` states that rule but sets it on only two of its four
forced runs.

---

## Automated — 56 of 59

| Case | Scenario | Suite |
|---|---|---|
| DR-01 UI at load · DR-04 opens at the default bet | boot-base (one run) | dr-core |
| DR-02 denominations = config | browse-denominations | dr-core |
| DR-03 bet +/- | change-bet | dr-core |
| DR-05 balance debited · DR-06 payout credited | base-win (one run) | dr-core |
| DR-07 insufficient balance | insufficient-balance | dr-core |
| DR-09 in-game History | spin-then-history | dr-core |
| DR-11 paytable UI · DR-52 tutorial | open-tutorial (one run, `combine: true`) | dr-core |
| DR-12 paytable payouts scale with bet | paytable-bet-scaling | dr-core |
| DR-51 multi-language | language-tour | dr-core |
| DR-53 reopening after a LOSING round restores its stake and no win | last-bet-restore (`outcome: lose`) | dr-lastbet |
| DR-54 reopening after a WINNING round restores its stake, win and winning line | last-bet-restore (`outcome: win`) | dr-lastbet |
| DR-55 reopening after a bonus TRIGGERED from the base game restores that round | last-bet-restore (`outcome: freespin_trigger`, `fs-trigger3-countdown`) | dr-lastbet |
| DR-56 play at bet X, change the bet to Y, reopen → the game shows X | last-bet-restore (`change_bet_after: true`) | dr-lastbet |
| DR-57 denomination changed on BO while a bonus is owed → the resumed bonus plays at the old bet · DR-58 after that bonus, the bet is the new denomination's default | bonus-resume-denomination-swap (one run, `fs-trigger3-countdown`; teardown `reset-denomination`) | dr-lastbet |
| DR-13 / 14 / 19 / 20 auto starts, stops at N, bet locked, count shown | auto-play (one run) | dr-autoplay |
| DR-15 stop at any win | auto-stop-at-win | dr-autoplay |
| DR-08 / 16 auto stops when the money runs out | auto-until-empty (one run) | dr-autoplay |
| DR-17 user stops auto | auto-user-stop | dr-autoplay |
| DR-18 reload cancels auto | auto-reload-cancels | dr-autoplay |
| DR-21 bet chosen before auto | auto-change-bet-first | dr-autoplay |
| DR-30 / 32 / 33 / 35 / 36 / 38 Hold & Spin, counting down | hold-spin-complete (`fs-trigger3-countdown`) | dr-holdspin |
| DR-34 a new coin resets the counter | hold-spin-complete (`fs-trigger3-reset`) | dr-holdspin |
| DR-31 9 coins in the base game | hold-spin-full-grid (`fs-trigger9`) | dr-holdspin |
| DR-37 9 coins during the feature | hold-spin-full-grid (`fs-trigger3-addcoin`) | dr-holdspin |
| DR-39 … 43 Buy Bonus | buy-bonus | dr-holdspin |
| DR-44 … 50 spin inputs | spin-inputs (one run) | dr-input-sound-reconnect |
| DR-22 turbo | turbo-modes | dr-input-sound-reconnect |
| DR-24 / 25 sound, music | sound-toggles (one run) | dr-input-sound-reconnect |
| DR-26 short disconnect | reconnect-brief | dr-input-sound-reconnect |
| DR-27 / 28 long disconnect, network switch | reconnect-long (one run) | dr-input-sound-reconnect |
| DR-29 second tab | multi-tab-session | dr-input-sound-reconnect |

Every case carries both a `stream:` half and an AI half (Rule 10), except DR-04. DR-04 is arithmetic
over the launch payload, and its screen is DR-01's still.

### Last Bet (DR-53 … DR-58) — CR_092 … CR_096

**Last Bet is a RESUME behaviour, not a panel**: the player reopens the game and it must come back on
the round they last played. DR-53 … DR-56 play a round, reopen, and check what came back
(`last-bet-restore`); DR-57/58 reopen while a bonus is still OWED, after the denomination changed on
BO, so the reopen RESUMES the bonus (`bonus-resume-denomination-swap`).

These are Dragon VIP's DV-67 … DV-73 on the same scenarios — see
`games/dragon-vip/test-suites/dv-lastbet.yml` for the same file with the paid-feature cases.

⚠ Every one gates on `info_captured`: `applyLastBet()` nulls `gameInfo.lastBet` at Start, so a live
read reports nothing on a session that restored perfectly (`games/dragon/bridge-qa-api.md`).
⚠ DR-57/58 write BO **on Dragon's own pair** — currency 18, not VIP's TKN — so the automation bet
ladder is created there on the first run. They declare `teardown: reset-denomination`, which is not
optional: a ladder left on 11/22/33 breaks every later bet assert in the suite.

## Not automated (3)

| Case | Why |
|---|---|
| DR-10 History on BO | manual, like ZZ-10: the BO bet-history endpoint is not known to this repo. DR-09 publishes the round's identity so the manual comparison is a copy-paste |
| DR-23 tab-switch mutes sound | not written yet, like ZZ-23: it needs a visibility change, with the sound getters polled from the backgrounded tab |
| DR-59 reopening after a PROMO round (CR_097) | needs a seeded promo account — promo exists only when the server sends `gameInfo.promo` |

---

## Open items

1. **Is `defaultBetLevel` 0-based or 1-based?** The client subtracts one (bridge §3), while the
   contract and `boot-base` read it as a 0-based index. So a FIRST session sends DR-04 red.
   Whether that red is a client defect depends on what the server means. Capture a first-session
   `getGameInfo()` and ask the server team. Returning sessions, which is most runs, open at
   `lastBet.baseStake` and are unaffected.
2. **Pin the Buy Bonus multiplier.** `buy-bonus` exposes the bet it read BEFORE the package pick,
   and a Dragon package is a stake, so `price_ratio` equals the multiplier only by coincidence.
   Two more facts would let DR-40 assert `paid_stake == stake × multiplier` exactly: the stake read
   after the pick, and `freeSpinPackages[i].price` from `getGameInfo()`. That small addition to
   `buy-bonus` would also settle Zeus's open question about what `price` is.
3. **`nextBet` is disabled at the top of the reachable ladder**, and the top depends on the balance.
   `open_bet_list` opens the list with `nextBet` only. So a session left at the highest affordable
   bet reports DR-02, and every other run that opens the list through `nextBet`, as unreached. The
   bridge doc's own advice ("use whichever of the two is enabled") would be a small, generic change
   to `open_bet_list`.
4. **`hold-spin-complete` and `hold-spin-full-grid` end on a bare `settle()`**, which is Zeus's open
   item too. DR-31, DR-36 and DR-37 claim "returns to base", but only the video checks it.
   `settle_to_base` would make that exact.
5. **There is no Dragon test matrix**, so the validator reports coverage as unknown. If QA issues
   one, check that its ids and rows still line up with ZZ-NN.
