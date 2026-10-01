# Session Summary 11 — Joker built out to 62 cases, and seven framework bugs the live runs found

Continues `session-summary-scenario-qa-10.md`. `session-summary-scenario-qa-3.md` is still the
onboarding guide for a NEW GAME — read that first if you are adding one.

Summary 10 left Joker with 10 cases, no fixtures, and everything blocked on a capture. This session
unblocked it (the fixture never needed that capture), took it to **62 cases in 5 suites**, and then
**ran some of them** — which is where the value was. Seven framework bugs surfaced, five of them
found by a red assert on a game that was behaving correctly.

---

## 0. Start the next session here

```bash
python tests/chaos/validate_suites.py --game joker        # ⚠ 2 ERRORS — see §1
python tests/chaos/validate_suites.py --game chaos        # OK
python tests/chaos/validate_suites.py --game dragon       # OK
python tests/chaos/validate_suites.py --game dragon-vip   # OK
python tests/chaos/validate_suites.py --game zeus         # OK
python tests/chaos/validate_suites.py --game mystic       # 3 known duplicate ids (Mah_012/044/045)
python tests/chaos/build_scenario_index.py                # 47 scenarios · 32 invariants, docs in sync
```

> ⚠ **Still not in git. 102 paths**, last commit `bb01fa6` — the same warning summaries 8, 9 and 10
> carried, now one session older. **Commit before anything else.**

---

## 1. ⚠ FIX THIS FIRST — `jk-features.yml` does not validate

```
ERRORS (2):
  ! duplicate case id JK-08 (jk-autoplay.yml and jk-features.yml)
  ! duplicate case id JK-16 (jk-autoplay.yml and jk-features.yml)
```

The `wheel: spin` run (**JK-38**) in `jk-features.yml` was replaced with an `auto-until-empty` run
carrying JK-08 + JK-16, which already live in `jk-autoplay.yml`. It looks like a scratch paste that
was never taken back out. `games/joker/test-suites/` is untracked, so there is no git copy — JK-38
has to be rewritten (the run is one `buy-bonus-lucky-spin` with `params: { wheel: spin }`; its claim
and prompt are described in §4).

**Use `test.yml` for scratch runs.** `validate_suites.py` skips `test.yml` and `temp.yml` by design
(its `skip` set), which is exactly what that file is for.

---

## 2. The fixture that was never blocked

Summary 10 said Joker's forced paytable was blocked on capturing a free-spin trigger. **It was not.**
A fixture is derived from the base paytable and the trigger SYMBOL, both of which were already in
the repo. `games/joker/paytables/` now holds:

| file | what |
|---|---|
| `base.json` | the real server paytable, copied from `docs/raw/paytable.json` |
| `joker-fs-easy.json` | `FreeSpin!` at ~30% of base reels 1/2/3, **0 on reels 4/5** |

| | unforced | forced |
|---|---|---|
| trigger rate | 0.42% — **1 in 236 spins** | **28.3% — 1 in 3.5** |
| spins to a trigger | — | median 3, p95 10, worst of 113k 43 |
| scatter reels on a trigger | 3, 4 or 5 | **always exactly 3** |

Zeroing reels 4/5 needed a new `--zero-elsewhere` flag on `derive_paytable.py` (probability mode
left non-target reels alone). Default off; all three Chaos fixtures still re-derive byte-identically.

⚠ **One open question the fixture cannot settle.** Chaos ships the same paytable schema from the
same server and a captured payload proved its award is `base(by reels) × ways`
(`ChaosRunner.expected_fs_award`: counts `[1,2,0,2,0]` awarded **48**, not 12). Joker is a line game
and `FREESPIN_AWARDS` assumes the plain reading; nothing captured on Joker settles it. JK-69 is
written to hold under **both** (`>= 12` and `% 12 == 0`). The first live run settles it.

---

## 3. `max_win_x` is a multiple of the BET — and that is not always what you want

Verified against `_resolve_max_payout`: the bare-number path is literally `value = float(x) * bet`.

| written | live bet 0.60 | live bet 4.00 |
|---|---|---|
| `max_win_x: 20` | 12.0 | 80.0 |
| `max_win_x: "20 * bet"` | 12.0 | 80.0 |
| `max_payout: 20` | 20.0 | 20.0 |

So a bare number and the expression are the same thing; the **absolute** cap is a different key,
`max_payout:`. Rule 17's default of 20 is right for a natural bonus.

⚠ **It is wrong for a bought bonus.** A purchase costs **100× the bet**, so `max_win_x: 20` is
**0.20× the price the player just paid** — the bonus truncates on essentially every run that wins
anything. The buy runs use `"5 * 100 * bet"`, which self-documents as five times the price (`100 *`
is the package multiplier, `5 *` the headroom).

⚠ **A `paytable:` block with only a cap and no `ref` pushes no weights at all.** `ensure_paytable`
is idempotent and `update_paytable` is skipped when there is no content, so the run re-selects the
automation paytable holding **whatever the last forced run left in it**. The teardown resets the
AGENT off that paytable; it does not restore the paytable's CONTENT. That is why the buy runs carry
`ref: base` — pushing the real weights explicitly rather than inheriting `joker-fs-easy`.

Measured, so the cap is sized rather than guessed (200k simulated bought bonuses, real FS weights):
retrigger **0.33%/spin**, P(any retrigger in 20) **6.6%**, rounds median 20 · p95 32 · p99 35 ·
longest 104, and **0 of 200 000** reached the scenario's 120-round drain cap. A long tail, not a
runaway.

---

## 4. The suites — 62 cases in 5 files

Numbering follows Dragon's, so **JK-NN is the same claim as DR-NN** wherever Dragon has one.
JK-30/32/33/34/35/36 are Chaos/Mystic cases Dragon never ported; they sit in the 30s because that
block is Dragon's Hold & Spin, which this game does not have.

| suite | cases |
|---|---|
| `jk-core.yml` | JK-01…12, 34, 35, 36, 51, 52 — boot, bet, money, History, paytable, tutorial, language, zero-balance free spins, the BONUS History ticket |
| `jk-autoplay.yml` | JK-08, 13…21, 31 |
| `jk-input-sound-reconnect.yml` | JK-22, 24…30, 32, 32b, 33, 44…50 |
| `jk-lastbet.yml` | JK-53…58 — `last-bet-restore` ×4 + `bonus-resume-denomination-swap` |
| `jk-features.yml` | JK-37, 39, 60…69 — expanding wild, free-spin multiplier, Buy Bonus (**JK-38 missing, §1**) |

**Scenario coverage: 32 of 47.** The other 15 are checked-and-inapplicable (hold & spin, cascade,
Chaos symbols, ante, premium reel, Mystic's glow, plus `base-spin` / `language-sweep` /
`last-bet-before-rounds` as redundant) — the table is in `docs/joker-coverage.md`, so nobody
re-derives it.

**One run per feature, not one per case.** `jk-features.yml` is 4 runs for 13 cases. The expanding
wild and the free-spin multiplier were two identical `fs-complete` runs — same fixture, same cap,
same teardown — split only because the claims wanted different assert sets. The asserts were always
per-case; only the RUN was duplicated, and with it a second BO push, a second bonus and a second AI
call for the same footage (Rule 16).

---

## 5. ⚠ The Buy Bonus flow is not what §12.5 reads like

Corrected by the game team mid-session, then confirmed by a capture:

```
confirm → BUY request → trigger-shaped response → A REEL SPIN, presented as a free-spin
TRIGGER (busyOps: buyBonusReveal) → the LUCKY SPIN wheel → the free spins, with the chosen
multiplier displayed DIRECTLY ABOVE THE REELS for the whole bonus
```

The purchase **does not jump into free spins**. The old prompt said *"a panel opens, the purchase is
confirmed, a wheel offers a multiplier… then the free spins play out"* — describing a screen the
player never sees. JK-39 now grades that reveal, and the Buy Bonus clip walks all five steps in
order.

The bridge calls the reveal a *"fake **5**-bonus spin"*, and `BaseGame.FreeSpins` awards 20 spins for
5 occurrences — exactly `Packages[0].NoOfSpins`. The reveal shows the grid that would *naturally*
have awarded what was bought, which is why it can be shown as a real trigger.

### What the purchase capture settled

`fixtures/bet-responses/buybonus-purchase.json` (stake 400 at a bet of 4.00):

| | |
|---|---|
| ⚠ **`totalFreeSpin` is absent** | the award is in `freeSpins: 20`. See §6.3 |
| ⚠ **there is no grid** | `rawViews` and `views` are both five reels of `[-1,-1,-1]` — the reveal is drawn entirely client-side, so it can only ever be graded on the CLIP |
| ⚠ **two things are called "price"** | `stake` / `buyBonusSelectedPrice` are MONEY (400); `freeSpinPackage.price` is **100 — the multiplier**. Asserting `paid_stake == freeSpinPackage.price` compares 400 to 100 and reads as a defect |
| the id triplet takes a THIRD shape | base: all three equal · free spin: `id` is the SPIN, `roundId`/`refId` the BONUS · **purchase: `id == refId ≠ roundId`** |
| no `baseStake` | so `base_stake()` returns **400, the price** |

`wild_expands_only_in_freespins` is safe on this round only because `views == rawViews`. That is
luck, not design — worth knowing before anyone "improves" it to compare against a symbol map.

---

## 6. Seven framework bugs, five found by a red assert on a healthy game

### 6.1 `HAS_SOUND_GETTERS = False` on Joker — wrong
Its comment said *"bridge §9 lists no isSoundEnabled()/isMusicEnabled()"*. §9 lists **both**.
`sound-toggles` returns *unreached* on a False flag, so JK-24/25 would have been permanently
inconclusive — green-adjacent, proving nothing — rather than failing.

### 6.2 `payout_matches_paylines` is Chaos-only, and the index doc said otherwise
`docs/scenario-index.md` claimed it *"will work on the next line game unchanged"*. Joker IS that
game, and it fails two ways: `expected_payout` / `win_lines` / `bet_lines` live on `ChaosRunner`,
not `LineGame` (so it raises `AttributeError` mid-run), and its formula multiplies by `multiplier`,
which Joker's captures disprove (reads 2.5, credited 1.5 not 3.75).

Wrote **`joker_payout_matches_paylines`** — `Σ payoutRate × (baseStake / betLines)` — tested against
all captures and five mutations (the `multiplier` trap, Dragon's divisor, an inflated rate, a
one-cent error, a credited no-win). It skips free spins on purpose (whether `luckyMultiplier`
multiplies a winning free spin is unverified) and **fails rather than passing when everything was
skipped**.

> ⚠ **`validate_suites.py` checks that an invariant NAME resolves, not that the profile supports
> it.** Neither failure would surface until a live run. Check the helpers exist on the profile
> before reusing a mechanism invariant.

### 6.3 A bought round's award was read as 0
`totalFreeSpin` is absent on a purchase; the award is in `freeSpins`. `{facts.awarded}` read **0**
(JK-63 failed `0 == 20`) and **`is_trigger()` answered False** on the one round that unarguably
awards the bonus. `JokerBanana.free_spins()` now folds it in, **gated on `freeSpinPackage`** — a
blanket alias would report a mid-bonus round with 5 left as "5 awarded".

### 6.4 `played_payout` is the wrong figure for a bonus
`last-bet-restore` sets `played` to the TERMINAL round, and a bonus ends on whatever spin empties
the counter — winning or not. Joker's captured terminal free spin reads `payout: 0` while the bonus
paid **39.6** (`totalFreeSpinPayout`). So `{facts.played_payout} > 0` went red on a healthy game.

New facts **`played_bonus_payout`** (summed over `fs[1:]` — free spins only, matching the server's
own total) and **`bonus_payout_restored`**. The scenario already did this for STAKE, with a comment
explaining why; payout never got the equivalent.

### 6.5 …and then `played_bonus_payout > 0` was a flake
A bonus that loses every free spin pays 0, and that is ordinary. Unlike `outcome: win` — where
`_matches()` hunts until `payout > 0`, so JK-54 may assert it — `outcome: freespin_trigger` breaks
out on the TRIGGER and takes whatever follows. **The case is the restore, not the size of the
bonus.** The win is now asserted as "whatever it was, it came back", which holds at zero.

All other payout asserts were audited: JK-15, JK-09, JK-53 and JK-54 are all hunt-guaranteed.

### 6.6 `DENOMINATION_NAME_ALREADY_EXISTS` — lookup and create disagreed about scope
`denomination/view/list` is scoped to ONE (sub-category, currency) pair; BO enforces name uniqueness
across the game CATEGORY. The lookup found nothing, the create collided. Exact matching made it
worse — a hand-made `"Automation Test TRY 3x5"` collides on BO's check while comparing unequal in
Python.

`find_denomination` now matches exact first, then case/whitespace-normalised;
`ensure_automation_denomination` **self-heals** on an `ALREADY_EXISTS` failure by re-reading the
pair, and if the name really is held elsewhere it raises **listing the names that pair does have**.

### 6.7 A failed scenario took the whole suite with it
`bonus-resume-denomination-swap` calls `stop_client()` **on purpose**. When the BO write above
failed, the run raised before reopening, leaving no page — and `return-to-base` then drove a bridge
that was not there:

```
Cannot read properties of undefined (reading 'awaitFreespinRound')
```

`run_teardowns` treats a raising teardown as fatal, so **one failed BO call aborted the suite** and
reported a JS TypeError as the cause. New helper **`bridge_is_live(ctx)`**; `return_to_base` checks
it and no-ops. Tested against live bridge / page without one / destroyed context / no page.

---

## 7. ⚠ The Lucky Spin default, and a config that disagrees with itself

A `wheel: keep` run landed **x12**. The framework clicked the right control (`luckySpin:keep`, and a
name that did not resolve would raise `Unreached`, never fall through to `spin`).

The paytable flags `IsDefault: true` on **both x3 and x12** (x1 is the only false one, despite the
largest weight at 49.5%). The game team's position is that **the default is x3**, so that flag is
the thing to chase. JK-64 now asserts `{facts.lucky_multiplier} == 3`, with both candidates written
into the case:

1. the paytable pushed to BO still carries `IsDefault: true` on x12 — **it does, as of this
   writing**; check `MultiplierLuckySpin` in `games/joker/paytables/base.json`, which IS the file
   the runs push. Then the config is wrong and the client is innocent;
2. the config is right and the client opens the wheel on a weighted draw. Then it is a client bug.

**If the paytable was updated on BO rather than in the repo**, re-export and re-derive:

```bash
cmp games/joker/docs/raw/paytable.json games/joker/paytables/base.json
cp  games/joker/docs/raw/paytable.json games/joker/paytables/base.json
python scripts/derive_paytable.py --game joker --base base --symbol "FreeSpin!" \
    --reels 0,1,2 --probability 0.30 --section BaseGame --zero-elsewhere --out joker-fs-easy
```

---

## 8. Two bridge gaps to file

| | |
|---|---|
| **`isGameInfoCaptured()` is not in Joker's bridge doc** | so `facts.info_captured` reads `None` = "cannot say", not "no capture". All six Last Bet cases gate on `!= False` (the Chaos/Mystic compromise) instead of `== True`. When it lands, tighten six lines |
| **Chaos's missing `isGameInfoCaptured()` and undocumented `getGameInfo()`** | still open from summary 10 §8.5 |

---

## 9. ⚠ Nothing in `games/joker/env.config.dev.yml` is verified

It was filled in by hand into a copy of Dragon VIP's file. The prose has been corrected to describe
Joker; **every id is still exactly as entered**. A wrong `bo.game_id` or `game_code` does not fail
loudly — the run reports the paytable was set while the game keeps its REAL weights.

* ⚠ **`bo.paytable_code` is not set**, so the runner falls back to `AutomationTest` — Zeus's
  spelling. Chaos carries `Automation_test`. Writing to a code the game does not carry is the
  failure that still reports success. **Read the real list off BO and pin it.**
* ⚠ **`currency_id: 18` is the pair Zeus and Dragon run on.** If Joker shares it, JK-57/58's
  automation ladder is shared with them — safe back to back, **not** side by side.

---

## 10. What to do next

1. **Commit.** 102 paths.
2. **Restore JK-38** and take the scratch run out of `jk-features.yml` (§1). Use `test.yml`.
3. **Verify the env config against BO** (§9) — `game_id`, `game_code`, `paytable_code`, and which
   (sub-category, currency) pair Joker is on.
4. **Settle the Lucky Spin default** (§7): check the x12 `IsDefault` flag, re-export and re-derive
   if it was changed on BO only.
5. **Run the suites** in this order — each one settles something the next depends on:
   * `jk-core.yml` → does the build boot, and is `defaultBetLevel` really 1-based (JK-04)?
   * `jk-features.yml` → settles the ways question (§2) and the wheel default (§7)
   * `jk-lastbet.yml` → settles which win a restored `lastBet` echoes (§6.4), then tighten JK-55
6. **Capture the three shapes still missing** — `bet-response.md` §7 lists them: a natural free-spin
   TRIGGER, a PAYING free spin (which settles whether `luckyMultiplier` multiplies the payout, and
   turns `joker_payout_matches_paylines`'s free-spin skip into a second formula), and a max-win
   truncation.
7. **Then the cases that are designed but not written** — `docs/joker-coverage.md` has each with
   what it needs. The most interesting is **the Lucky Spin resume**: `pending.gameState == 3` with
   spins owed means the player closed the game AT THE WHEEL, and no other game in this repo has a
   resume that blocks on a player decision. ⚠ `dismissPopups()` never closes `luckySpin`, so a run
   that reopens into it and walks away hangs the game AND poisons the next run.
