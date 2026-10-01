# Session Summary 8 — the Zeus rewrite, bridge v3, and a bought bonus driven end to end

Continues `session-summary-scenario-qa-7.md`. `session-summary-scenario-qa-3.md` is still the
onboarding guide for a NEW GAME — read that first if you are adding one.

This session was almost all Zeus. The four suites were rewritten against the 18 authoring rules, the
framework was aligned with the game's **bridge v3**, and five bugs were chased through live runs —
ending with **buy-bonus fully green, end to end, twice**. It also produced
`docs/scenario-toolkit.md`, a drift-checked reference of every helper a scenario may call.

---

## 0. Start the next session here

```bash
python tests/chaos/validate_suites.py --game zeus     # 50 cases, OK
python tests/chaos/validate_suites.py --game chaos    # 77 cases, OK
python tests/chaos/validate_suites.py --game mystic   # 3 known duplicate-id errors (§11)
python tests/chaos/build_scenario_index.py            # index AND toolkit doc in sync
python -m pytest tests/ -q --ignore=tests/test_detection.py   # baseline: 48 failed / 511 passed
```

> ⚠ **The framework is not in git.** `scripts/scenario_engine.py`, `scripts/game_profile.py`,
> `scripts/run_scenario.py`, `scripts/ai_video.py` and both `tests/chaos/*.py` checkers are all
> UNTRACKED. This session lost five classes to a bad edit and got them back only through VS Code's
> Timeline (§9). **Commit before doing anything else.**

- **Writing scenario code?** Read `docs/scenario-toolkit.md` first — §0 is a template.
- **`games/zeus/test-suites/temp.yml` is what the user runs.** It is a scratch copy of whichever run
  is under test (currently `multi-tab-session` / ZZ-29). When a fix lands in a real suite, sync it
  into `temp.yml` too — twice this session a fix "didn't work" because it was only in the suite.
- **The validators are per-game.** Run all three; a clean Chaos run says nothing about Zeus.

---

## 1. Where things stand

| | |
|---|---|
| Zeus | **50 cases**, 50 of 52 matrix rows. Not automated: ZZ-10 (History on BO — manual), ZZ-23 (tab-switch mutes sound — no scenario yet) |
| Chaos | 77 cases, 71 of 111 — unchanged this session |
| Mystic | 58 cases (55 unique ids), 54 of 108 — **3 duplicate case ids** (§11) |
| Engine | 43 scenarios · 26 invariants · 86 public helpers, every one documented in `scenario-toolkit.md` |
| pytest | 48 failed / 511 passed — **pre-existing**: event-loop isolation (they pass one file at a time) and a subprocess `framework` path; `test_detection.py` needs `imagehash`. Nothing here imports the engine. |
| Git | nothing committed this session; last commit `bb01fa6` |
| Dev env | `192.168.1.27:7456` serves `/web-mobile/<game>/index.html` again — it 404'd at session start |

---

## 2. Live-verified this session

| Scenario | Cases | Result |
|---|---|---|
| `buy-bonus` | ZZ-39…43 | **green, all five — twice** (10-09 18:10, 11-09 09:19). One intermittent failure between them (§11.2) |
| `turbo-modes` | ZZ-22 | green |
| `reconnect-brief` | ZZ-26 | green |
| `reconnect-long` | ZZ-27, ZZ-28 | green |
| `multi-tab-session` | ZZ-29 | green |
| `spin-inputs` | ZZ-44…50 | **orange, all seven — not a defect.** Gemini's video API returned `500 INTERNAL` on the one shared call, so every case sharing it went orange. Re-run. |

**Fixed but not re-run since:** `spin-then-history` (ZZ-09), `hold-spin-full-grid` (ZZ-31, ZZ-37),
`language-tour` (ZZ-51 has a new prompt), `hold-spin-complete`, all of `zz-core.yml` and
`zz-autoplay.yml`.

---

## 3. The Zeus suites, rewritten against the 18 rules

Every change was a case that could pass while proving less than it claimed. Full detail in
`docs/zeus-coverage.md` ("Rewritten against the authoring rules").

| Rule | Fix |
|---|---|
| 4 — a stop needs time on the clock | every auto case asserts `extra_round_after_stop` + `auto_flag_after` and the prompt judges the tail (ZZ-08/16 skip the flag — Rule 15) |
| 10 — the bridge is not the screen | every case has an AI half; the two without one say why |
| 16 — one clip, one prompt | run-level `video:`/`image:` with `expected_by_case`; **three whole runs deleted** (ZZ-19's second auto-play, ZZ-52's second tutorial, ZZ-05's `base-spin`) |
| 18 — one scrolling surface | Info/Paytable/Tutorial graded as one document with `combine: true` |
| 17 — assert the length | `respins_played >= 2` wherever an invariant compares consecutive rounds |
| 6 — aim at the middle | **ZZ-34 was vacuous**: its p=1 fixture filled the grid on respin 1, the one case where no reset is due. New `fs-trigger3-reset` at p=0.30 |
| 9 — the cap | loose `max_win_x: 500` + `{facts.capped} == False`; a tight cap truncates a Hold & Spin into a shape `holdspin_ends_correctly` calls a violation |

Also: **ZZ-04 automated** (the first-session rule, §5) · **ZZ-12 automated** (`paytable-bet-scaling`
was portable in name only — `line_bet()` moved from `LineGame` to `SlotGame`) · **ZZ-05 + ZZ-06
merged** on `base-win`, whose winning round shows both moves of the balance · **ZZ-22** moved to
`turbo-modes` (`toggle-turbo` places no bet — no reel ever turned) · **ZZ-31 / ZZ-37** split onto
their own fixtures (both used to assert only `grid_filled`, true on either path).

Fixture map (`zz-holdspin.yml`):

| Fixture | Respin CoinWeight | Path | Cases |
|---|---|---|---|
| `fs-trigger3-countdown` | all 0 | 3-coin trigger, no new coin, 3→2→1→0 | ZZ-30/32/33/35/36/38 |
| `fs-trigger3-reset` (new) | all 300 (p=0.30) | a coin lands, grid does not fill → counter resets | ZZ-34 |
| `fs-trigger3-addcoin` | all 1000 | every empty cell fills → 9 during the feature | ZZ-37 |
| `fs-trigger9` | all 0, COIN on 3 base reels | 9 on the trigger, no respin | ZZ-31 |
| *(none)* | real weights | **buying is itself deterministic** — forcing it made ZZ-42's reset rule vacuous | ZZ-39…43 |

---

## 4. Engine changes

All documented in `docs/scenario-toolkit.md`. The new or reworked pieces:

| Helper | What it is for |
|---|---|
| `play_out_holdspin(ctx, trigger)` | Drain a Hold & Spin: ends on `is_terminal` or a full grid, reports `capped`, **raises** at its cap (was three copies, all ending on `free_spins == 0`) |
| `enters_holdspin(b)` | A trigger **or** 9 coins on a base spin — the instant win `is_trigger` rightly answers False to |
| `settle_to_base(ctx)` | Prove a finished feature handed back to base: film the celebration, clear it, wait (`spin_timeout_ms`, in 5 s slices), watch for a stray bet, **hold — on the failure path too** |
| `close_history(ctx)` | Close History and prove it, logging every step to console and timeline |
| `ctx.wait_for_history_detail()` | Did the ticket open? Asks `getHistoryDetailState().open` first |
| `require_buttons(*roles)` | Resolve every control up front; raise naming **all** missing roles at once |
| `Unreached(reason)` | Raise for a precondition; one `except Unreached` reports it. **Never bare `Exception`** |
| `base-win` facts | `balance_before_spin` / `balance_while_spinning` / `balance_after_win` (renamed from `win_balance` to match `base-cluster-win`) |
| `boot-base` facts | `expected_open_bet` / `opens_at_expected_bet` / `first_session` / `last_bet_stake` |

Smaller: `wait_for_popup(None)` returns at once · `wait_for_history_rows` returns `[]` at once with no
getter · `hold_button(use_bridge=True)` probes and falls back (it called `holdSpinEnd`, a name the API
never had) · dead `getGameSettings()` fallbacks removed.

---

## 5. Bridge v3 (`games/zeus/bridge-api.md`)

**D1–D4 are all fixed in the build** (`docs/zeus-bridge-defects.md` keeps the record). `startGame()`
now resolves at the click; `settings.defaultBetLevel` exists; `getHistoryRows()` and
`getHistoryDetailState()` are implemented.

Profile corrections: `history_detail` popup = `historyDetail` (it was `None` — the cause of the 70 s
hang, §6a) · `detail_next/prev/close` + `HAS_HISTORY_DETAIL_NAV = True` · **`HAS_TUTORIAL_WEBVIEW =
True`** (Info is a cross-origin WebView; `False` sent the tutorial down a scroll path that cannot move
it) · `buy_purchase` / `buy_close` / `history_row` added.

Also: `getSpeedMode()` now returns `'NORMAL' | 'TURBO' | 'UNKNOWN'`, not a boolean ·
`holdSpinStart/Release` removed · `scripts/check_bridge.py` reported a healthy v3 build as broken
(it needed the removed `getAppState()`); fixed and marked superseded by `scripts/probe_bridge.py`.

> **The first-session rule.** `defaultBetLevel` applies only to a player's FIRST session; every later
> one opens at `lastBet.baseStake`. Never assert `bet_index == default_bet_level` — ZZ-04 asserts
> `opens_at_expected_bet`.

> The older `games/zeus/bridge-api-v2.md` and `bridge-api-v21.md` are **gone** — v3 replaced them,
> and being untracked they are not recoverable. They were already stale (neither mentioned
> `getHistoryRows`), which is why a claim taken from one sent the History diagnosis the wrong way.
> `bridge-api.md` is the only Zeus bridge doc now; trust it, the contract, and a live probe.

---

## 6. Five bugs chased through live runs

**a. `spin-then-history` sat on the detail ticket for 70 s.** The scenario decided whether the ticket
opened by looking for `GAME.popup("history_detail")` in `visiblePopups` — `None` on Zeus, so False
forever. It skipped the paging, polled 10 s, then waited **60 s** (`spin_timeout_ms`) inside
`except: pass`. *`visiblePopups` is a dialect; ask the contract's own field.*

**b. `hold-spin-full-grid` won with 9 coins and kept spinning.** A 9-coin base win reports base state
and no respins, so `is_trigger` correctly says False — and the hunting loop's only stop condition was
`is_trigger`. `enters_holdspin` fixes the loop, and `_holdspin_rounds` too (it sliced an instant win
to zero rounds, so every invariant would have gone red).

**c. `buy-bonus`, in order:**
1. **Buying is two clicks.** `buyBonus:buy` raises a confirmation modal; `buyBonus:confirm` inside it
   purchases. The modal is a *child* of the `buyBonus` popup, so `visiblePopups` never shows it.
   `canDo('confirmBuyBonus')` answers about the *action*, not the control.
2. The purchase waited on `observe_bet()`'s **90 s** auto-play default → now `bet_timeout_ms`.
3. `UnboundLocalError` mid-bonus: `facts` was built after branches that wrote to it.
4. **The clip was 4 ms long.** The purchase and first respin arrive 4 ms apart, so `at: trigger`
   covered nothing. Now no `at:` — the whole scenario is the case.
5. It closed on the win animation: `settle_to_base` waited 15 s but the end presentation takes
   ~21 s, and it didn't hold on failure. Now `spin_timeout_ms`, slice polling, hold on both paths.
6. No paytable: the purchase is deterministic, and forcing respin weights only silenced a rule.

**d. ZZ-51: 12 false alarms out of 24.** `text_fits` (pass = true) and `text_clipped` (pass = false)
are one property asked twice in opposite directions. `text_fits` came back true on all 24;
`text_clipped` true on 12, **every one** with prose denying clipping. Rebuilt on the Chaos shape.

**e. My own drift check was broken twice** — a `\b` regex landed as a literal backspace byte, then
backtick pairing flipped after a code fence. Only a negative test (remove one helper, expect exactly
one flag) exposed it.

---

## 7. Validator (`tests/chaos/validate_suites.py`)

Now `--game chaos|zeus|mystic` (it was hard-coded to Chaos, so Mystic and Zeus had never been
checked). Reads `.xlsx` matrices; expands merged ids (`Mah_014_019`). `SPREAD_HELPERS` knows
`settle_to_base`, and `confirm_stopped` now lists all 6 of its facts (`auto_flag_cleared_s` was
missing). First run across all three games found **5 Mystic defects**: two silent passes fixed
(`extra_spin_after_stop` → `extra_round_after_stop`; `{trigger.totalFreeSpin}` →
`total_free_spin`), three duplicate ids left for a decision.

---

## 8. Docs

| Doc | Change |
|---|---|
| `scenario-toolkit.md` | **new** — every helper by job, a template, the don't-call list, traps. `build_scenario_index.py` fails if a public helper is missing |
| `scenario-assert-authoring.md` §2 | two new traps: **inverse-pair** (ZZ-51) and **per-round window** (the 4 ms clip) |
| `scenario-authoring-rules.md` | the stale toolkit tables → a pointer to the toolkit doc |
| `zeus-coverage.md` | rewrite record, fixture map, ZZ-04/12 automated |
| `zeus-bridge-defects.md` | D1–D4 marked fixed, with the framework bugs each exposed |
| `scenario-index.md` | rows updated; per-game validation note |
| `games/zeus/paytables/paytable.md` | the four shipped fixtures, and why p=0.30 |
| `README.md` | toolkit doc, summaries 5–8 |

---

## 9. The incident, and how to edit this repo safely

A replacement sliced from `close_history` to `class SpinThenHistory`. Both anchors were unique — but
**five classes lived between them** (`ChangeBet`, `BrowseDenominations`, `BootBase`, `BaseSpin`,
`AutoPlay`). Restored from VS Code's Timeline, because the file is untracked.

The method used since, with no further damage:

1. **Bound a replacement by AST line numbers** (`node.lineno`…`node.end_lineno`), never by the text
   between two anchors.
2. **SHA-256 everything outside the range** before and after; both must match.
3. **Don't pass code through a Bash heredoc.** It mangles backslashes and backticks here even when
   quoted. Write it with the Write tool to the scratchpad, splice it with a short Python step.
4. **Read the bytes back** (`cat -A`, scan for control bytes).
5. **Re-run the validators** — they import the module, so a missing class fails loudly.

---

## 10. Gotchas worth carrying forward

- **`facts.update({...})`, never `facts["x"] = …`** — the validator reads fact names from dict
  literals; a subscript store makes correct asserts read "not exposed". Hit twice.
- **Build `facts` first**, before any branch can raise or return.
- **A helper whose facts are spread with `**` needs a `SPREAD_HELPERS` entry.**
- **`marks` map a name to a round seq, not a moment.** A bonus's rounds can arrive milliseconds apart,
  so a single-round `at:` can be empty. For a whole feature, omit `at:`.
- **Never ask one property twice in opposite directions** in a schema.
- **Read the AI's `reason` before believing its boolean** — and a field that never varies tests nothing.
- **An inline `stream:` predicate has no builtins and no tolerance** (`9972.60 + 0.24 != 9972.84`).
  Float identities belong in Python — `conservation` already checks the balance to 1e-6.
- **One transient provider error oranges every case sharing the call.**
- **A per-game bridge doc can be two revisions behind** — probe the live build.

---

## 11. Open items, most important first

1. **Commit the framework** — the six core files are untracked (§0).
2. **`buy-bonus` intermittent** (11-09 09:17, between two greens): all four clicks landed, confirm
   **1.36 s after Buy**, then no bet; `canDo` returned `None`, meaning the call threw. Most likely
   confirm landed while the modal was still animating open — a control that resolves while inert.
   Needs a second failure before changing anything; a wait for confirm to be *stably* clickable is
   the likely fix.
3. **Is `price` really the price?** `buyBonusSelectedPrice` read **60** while the purchase round's
   stake was **180** at bet 3. If 60 is the package *multiplier*, then `paid_stake == bet × price`
   holds and `price_ratio` (price ÷ bet) is dividing a multiplier. Check before pinning ZZ-40.
4. **Re-run** `spin-inputs` (Gemini 500), then everything in §2's "not re-run" list. Consider a retry
   on 5xx for shared AI calls.
5. **`hold-spin-complete` / `hold-spin-full-grid` still end on a bare `settle()`** — ZZ-36 and ZZ-31
   claim "returns to base" but only the video checks it. Move them to `settle_to_base`.
6. **Mystic:** 3 duplicate case ids (`Mah_044`, `Mah_045`, `Mah_012` — which suite owns each is a
   decision) · `mah-language.yml` has the inverse-pair trap (Mah_054 asserts `text_fits: true` **and**
   `text_overflow: false`; the fix is Chaos's `scaled_to_fit`, not deleting it) ·
   `mah-freespin-balance.yml` uses `at: trigger` on a free-spin trigger · Mah_007/008 could merge the
   way ZZ-05/06 did.
7. `close_bet_list` has no step logging; `Unreached` / `require_buttons` are only in `buy-bonus`.
8. `plan_insufficient` still withdraws `floor(balance)` on a "whole units only" premise Rule 11 calls
   false.
9. Confirm Zeus's `AUTO_COUNTS` (still the shared default) against `auto:package:<i>`.
10. The runner resolved `max_win_x` against `bet[live] 4.0` while the re-entered session opened at 3 —
    the first-session rule again. Harmless with a loose cap.
11. **Bridge asks:** document `buyBonus:buy` vs `buyBonus:confirm`; confirm Zeus publishes a
    `historyDetail` label and `getHistoryDetailState()`.
12. Chaos live runs are still pending from summary 7.

---

## 12. Where the files are

| | |
|---|---|
| Engine / profile | `scripts/scenario_engine.py` · `scripts/game_profile.py` |
| Runner / AI pass | `scripts/run_scenario.py` · `scripts/ai_video.py` |
| Checkers | `tests/chaos/validate_suites.py --game <g>` · `tests/chaos/build_scenario_index.py` |
| Build probe | `scripts/probe_bridge.py --game zeus --env dev` |
| Zeus suites | `games/zeus/test-suites/zz-{core,autoplay,holdspin,input-sound-reconnect}.yml` · `temp.yml` (scratch) |
| Zeus fixtures | `games/zeus/paytables/` · `games/zeus/fixtures/bet-responses/` |
| Zeus bridge | `games/zeus/bridge-api.md` (v3, current) |
| Run output | `test-results/zeus/dev/scenario-<date>/<scenario>/` — `result.json` holds each spec's `at` and `window`; `events.jsonl` the round timings |
| Reference docs | `scenario-toolkit.md` · `scenario-authoring-rules.md` · `scenario-assert-authoring.md` · `scenario-index.md` · `game-bridge-contract.md` · `zeus-coverage.md` · `zeus-bridge-defects.md` |
