# Session Summary 5 — Chaos Runner onboarded, and a naming question left open

> **Superseded in part by `session-summary-scenario-qa-6.md`.** §6 below is RESOLVED (the
> answer was the opposite of what it predicted) and the stall it describes had a different
> cause entirely — a popup-label typo, not the button names. Read doc 6 first.

Continues `session-summary-scenario-qa-4.md` (Zeus + the first live runs).
`session-summary-scenario-qa-3.md` is still the onboarding guide for a NEW GAME — **read that
first**.

This session: onboarded **Chaos Runner** end to end — knowledge docs, a third mechanism (`LineGame`),
profile, scenarios, 76 cases — then corrected almost all of it against **six real BET payloads and
the BO paytable export**. Six documented rules turned out to be wrong, including two that no amount
of re-reading the design doc would have caught.

It ended on one open question (§6). That is now **RESOLVED** — see §6: the answer was the opposite of what was expected, and the stall it was blamed for had a different cause entirely.

---

## 1. Where things stand

| | |
|---|---|
| Chaos cases | **76** across 5 suites, 72 of the 111 CSV rows |
| Scenarios | 43 total · Chaos adds **6**, reuses **26** from Mystic |
| Invariants | 26 · Chaos adds 10 |
| Captured payloads | **6** — `games/chaos/fixtures/bet-responses/` |
| Verification | 45 + 67 + 76 checks, all passing, no browser needed |
| Live run status | `browse-denominations` still blocked, but by a NEW defect — see §6 and `docs/chaos-bridge-defects.md` C1 |

**Not yet run end to end against the live game.** One scenario was driven far enough to expose the
bug in §6; nothing else has been executed.

---

## 2. What was built

### A third mechanism: `LineGame`

Chaos is neither cascade nor hold-and-spin. One grid per round, paid on fixed lines, plus a
pay-anywhere ways symbol. The mechanism's contribution is a **per-reel symbol census** (`grid` ·
`reel_counts` · `reels_with` · `ways`), because almost every rule this game states is a predicate
over it — "P1 only on reels 1/3/5", "2+ Wilds", "Bonus on 3 reels" — which makes them exact
`stream:` checks instead of AI reads.

### Two engine bugs fixed at the source

Both would have broken Chaos on its first run, and one was silently broken for Zeus already:

1. **The bridge returns the socket envelope.** `Bet.parse` read fields off the top level, where
   `payout` does not exist — so every round would have parsed as a zero-payout loss and
   `conservation` would have failed a game behaving perfectly. Fixed with `SlotGame.unwrap()`.
2. **The engine hardcoded the reference build's button names** at ~40 call sites instead of going
   through `GAME.button()`. Zeus's profile declared `nextBet` / `auto:package:{i}` that nothing ever
   read. Fixed with `SlotGame.resolve()`, which maps a literal in any dialect back to its role.

### Docs

| File | What |
|---|---|
| `games/chaos/bet-response.md` | the response contract, 9 sections, every rule tied to the invariant that enforces it |
| `games/chaos/docs/final/paytable.md` | rebuilt from the BO export; all 9 symbols, real rates |
| `games/chaos/docs/final/game-knowledge.md` | mechanics + a §5 recording every doc defect found |
| `docs/chaos-coverage.md` | the 111-case map |
| **`docs/scenario-index.md`** | **new** — the lookup table to read BEFORE writing a case for a new game |

---

## 3. What the real payloads corrected

Six captured responses plus `games/chaos/docs/raw/paytable.json` overturned six documented rules. This is the
section to read if you are onboarding another game: **every one of these was wrong in the design
doc, and four were wrong in my implementation of it.**

| Rule | The doc said | Reality |
|---|---|---|
| Free-spin award | "20, 15, 10" for 5/4/3 **symbols** | `base(REELS) × ways` — Bonus on 3 reels, ways 4 → **48**. Neither documented reading reproduces it |
| Trigger condition | 3+ symbols | 3+ **distinct reels** — two stacked on one reel plus one elsewhere does not trigger |
| Bet per line | `base bet / 5` in the worked examples | `/ betLines` = **20**; its "$40" payout does not hold |
| Ante multiplier | "increases the bet by 1.5x" | **×3**, and it multiplies the STAKE while leaving `baseStake` untouched |
| P2 3-of-a-kind | "Icon Multi P2 : 200" | **40**. 200 is P4's *5*-of-a-kind |
| Symbols | named strings | **numeric ids** — `5`=P1, `9`=Wild, `11`=Bonus |

Two more the docs never mentioned at all:

* **`payout` is CAPPED at `maxWin`; `rawPayout` is the gross.** A captured round earned 12, credited
  **10**, `hasReachedMaxWin: true`. My payout model ignored this and produced a **false red** on a
  round where the game was correct.
* **A scatter pays `multiplier × baseStake` with NO `/betLines` divisor** — unlike a line win.
* **Wild never lands on reel 1** (strip weight 0 in every distribution list).

### The method that found them

Nothing here came from reading harder. Each was found by **computing the documented rule and
comparing it to what the server actually paid** — see `tests/chaos/verify_against_fixtures.py`,
which runs every invariant against the real payload *and* against a mutated copy. Passing on real
data is only half a test: a rule that accepts everything also accepts the real thing.

---

## 4. Working practices worth keeping

**Never compare a payout formula against `payout`.** It is the capped figure. Use `rawPayout`.
This cost a false red, and the same shape will exist in any game with a max-win cap.

**A fact that does not exist resolves to the string `null` and the assert quietly PASSES.**
`tests/chaos/validate_suites.py` checks every `{facts.X}` against what its scenario actually
exposes. On first run it caught **43** of them. It is helper-aware — facts spread in with
`**confirm_stopped(...)` are invisible to a naive AST walk, and so are screenshots taken inside
`tour_ui()` / `_hover_ante()`.

**An `image:` assert needs its scenario to CAPTURE stills and RETURN them.** Miss either and the
assert sits at `pending` forever — it reads as "the AI never ran" rather than "there was nothing to
grade". The validator now fails the suite for both; it caught two more instances after the first was
reported.

**Favour `video:` over `image:`.** Mystic is ~97% video. Reach for a still only when ONE frame
answers the question AND the detail is too fine for a downsampled montage — text, glyphs, diacritics,
small payout digits. Anything that changes over time cannot be shown by a still.

**Check whether Mystic already does it.** Comparing scenario-by-scenario against Mystic deleted a
duplicate (`game-help` was `open-tutorial` behind the same `info` control) and moved two notification
cases onto `toggle-turbo` / `toggle-auto` — they had been sitting on `stacked-popups`, whose rapid
burst deliberately never dwells long enough for a notification to be readable. Both were found by
asking the question, not by a failing run. That comparison is now `docs/scenario-index.md`.

**Verify a doc's claim before repeating it.** Twice this session I acted on a stated premise and was
wrong: once on "the bridge follows the contract" (§6), once on a profile value that had changed on
disk between my reading it and reporting it. Read the artefact, not the claim about it.

---

## 5. Verifying without a browser

Four harnesses, all runnable with no game:

```
python tests/chaos/verify_against_fixtures.py   # 67 — invariants vs REAL payloads + mutations
python tests/chaos/verify_chaos.py              # 45 — synthetic streams, both directions
python tests/chaos/validate_suites.py           # every scenario/invariant/fact/param resolves
python tests/chaos/build_scenario_index.py      # regenerates the index AND fails on drift
```

Plus the two static self-checks in `docs/scenario-authoring-rules.md` (trigger handling inside every
loop; no `self.X` that nothing assigns). Both print nothing today.

---

## 6. ✅ RESOLVED (2026-09-02) — it was never the button names

Settled against the live dev build. Both halves of the question came back the opposite of what this
section predicted, and the stall had a third cause entirely. Full write-up:
**`docs/chaos-bridge-defects.md`**.

**The alias table exists, and the profile was already right.** `getButtonRect()` resolves the
reference spellings — `stakeIncrease`, `stakeDecrease`, `trophy` on the main screen, and
`bet:close` / `bet:plus` / `bet:minus` / `bet:row:{i}` while the denomination list is open — even
though `getAllButtonRects()` enumerates only the native `betPlus` / `monumentalWin` /
`denomination:*` names. **`getAllButtonRects()` is an inventory of canonical names, not of
resolvable ones**; reading it as the latter is what made this section suspect the profile. The six
roles in the table below must **NOT** be reverted. `bet:close` resolves in 0.6s and really does
close the list.

**The ~70s stall was a popup-LABEL typo.** `ChaosRunner.POPUPS` inherited the reference
`"denominations"`, but this build's `visiblePopups` says `"denomination"` (and `"autoplay"`, not
`"autoPlay"`). Popup labels are raw strings — unlike button names they are **not** aliased. So
`close_bet_list()`'s "is it still open?" guard compared `"denominations"` against
`["denomination"]`, concluded the list was already gone, and returned
`closed / close_button_worked / needed_dismiss = True` **having clicked nothing at all**. CR_004's
three asserts passed on a popup still on screen — a false green — and the next `settle()` then
burned its full 60s because a visible popup blocks `waitForSpinClickable`.

Fixed: `ChaosRunner.POPUPS` now carries the verified labels, and `close_bet_list()` no longer trusts
the profile label (any visible popup counts), so a label typo can never again report success without
clicking. Verified live: the list opens, scrolls both ends, and closes via `bet:close` with no
leftover popup.

**What is still blocking — a NEW defect (C1).** With that fixed, `browse-denominations` still fails,
for an unrelated reason: **the first click after boot is silently swallowed.**
`waitForButtonClickable` resolves and returns a rect, `canDo` is ok, `interactable: true`,
`roundState: READY` — and the click does nothing. The second click always works. It is a race, not a
fixed delay (isolated, 6 loads each: 0s delay → 3/6 land, 1s delay → 6/6), and it is why the run
concluded "no control on this build opens the denomination popup". It does; on the second click.
Deliberately **not** worked around with a sleep — see `docs/chaos-bridge-defects.md` C1 for why and
for the BRIDGE ASK.

> The stale `_BET_DRIVE_ASK` string that asserted nothing opens the bet popup has been corrected to
> describe what the run observed rather than that wrong conclusion.


### Also open

* **`getGameInfo()` is absent from the Chaos bridge doc** but is REQUIRED on the boot path
  (`docs/game-bridge-contract.md` §2). Without it the framework cannot see `pending`, so a session
  resuming mid-bonus leaks its free-spin rounds into the first scenario's stream and `conservation`
  fails on a stake nobody placed.
* **No control is documented that OPENS the denomination popup.** `betPlus` appears to (the list
  opens), but it is listed as a main-screen stepper. If it does not, set `HAS_BET_POPUP = False` so
  the scenario takes the stepper branch deliberately instead of discovering it by timeout.
* **No in-bonus round has been captured.** `stake: 0` per free spin, the counter decrement and the
  retrigger amount are still assumed. Biggest remaining evidence gap.
* **Does `randomMultiplier` apply to the scatter half of a win?** The only captured scatter win had
  ×1, so the two readings are indistinguishable. The profile deliberately does NOT apply it, so a
  future scatter win on a ×2 spin surfaces the question as a violation rather than hiding it.
* **Symbol ids `3` and `8`** are Q and P4 in some order — inferred, not named by a `winType`.
* **Win-tier reference base.** The captured ante round paid 7.5× `baseStake` but 2.5× the deducted
  stake — different tiers. `win_tier_bands` is deliberately NOT asserted on any ante case until this
  is settled.
* **`language-sweep`** is registered but used by no suite. Possibly dead.

---

## 7. Where the files are

| | |
|---|---|
| Profile | `scripts/game_profile.py` — `LineGame`, `ChaosRunner` |
| Scenarios | `scripts/scenario_engine.py` — the Chaos block is commented as such |
| Suites | `games/chaos/test-suites/cr-{core,ante,features,auto-turbo,ui-resilience}.yml` |
| Payloads | `games/chaos/fixtures/bet-responses/` · documented in `games/chaos/bet-response.md` |
| BO paytable | `games/chaos/docs/raw/paytable.json` |
| Harnesses | `tests/chaos/` |
| **Read before writing a case** | **`docs/scenario-index.md`** |
