# Session Summary 13 — Grandma Pets: first live runs, the fixes they forced, and new rules

Continues `session-summary-scenario-qa-12.md`. Onboarding a NEW game: read
`docs/game-bridge-contract.md` §12 and `session-summary-scenario-qa-3.md`.

Summary 12 ended with no trustworthy Grandma verdict. This session ran the Grandma suites live,
case by case, and fixed what each run exposed. Almost every failure was on OUR side (framework,
profile or suite). The bridge team fixed two real bridge defects (big-win skip, History rows).
There are two likely **game** findings (last-bet restore, see §7).

---

## 0. Start the next session here

```bash
python tests/chaos/validate_suites.py --game grandma      # OK
python tests/chaos/build_scenario_index.py                # in sync
```

> ⚠ **Still not in git: 115 paths**, last commit `bb01fa6`. Summaries 8–12 all said "commit
> first". It is now two sessions and a whole game larger. **Commit before anything else.**

> ⚠ **Most fixes below were verified offline only** (fake bridge in a real browser, re-grading a
> saved AI answer, or a read-only probe). The next full live run of the Grandma suites is the real
> check. §9 lists what has NOT been confirmed live.

---

## 1. Rules decided this session (the user's decisions — follow them)

| Rule | Where it is written |
|---|---|
| **AI prompts only ASK.** Never state the expected answer, the value to be read, the rule under test, a format example equal to the answer, or a measured result. | memory `feedback_prompt_no_answer_leak.md` |
| **Timeline mark text is part of the prompt** (timestamp mode appends it as the ACTION TIMELINE). Marks name the MOMENT only, never a value or "must match". | same memory; fixed in `last-bet-restore`, `bonus-resume-denomination-swap` |
| **Normal tests: name the moments, ask "same?".** Compare an AI-read value against `{facts.x}` only when QA genuinely inspects the response / gameInfo. Last-bet restore = `grid_same` / `bet_same` / … booleans. **Exception:** TC-086 compares reads to `pending_stake` and `new_default_bet`. | same memory |
| **Video by default; IMAGE for reading a static screen** (tutorial text/pictures, table digits, text in every language). The video montage shrinks frames and small text becomes unreadable. | `docs/scenario-assert-authoring.md` "Video or image"; memory `feedback_video_only_minimal_clip.md` |
| **Fix the product, not the test**: report bridge defects with evidence instead of routing around them. | existing memory; followed for G1/G3 |
| **Dragon's suites are the reference** for Grandma; update Grandma, not Dragon. | applied to `gm-autoplay.yml` |
| **`docs/game-bridge-contract.md` is the only bridge contract.** `cascade-game-bridge-contract.md` was deleted after its unique content was moved in. | §5 below |

---

## 2. Framework changes (`scripts/`, `framework/`)

| Change | File | Why |
|---|---|---|
| **Suite-start paytable baseline**: reset the BO paytable to the game default once before the first load, then wait `BO_SETTLE_S` (5 s). `bo_reset` now returns bool. | `run_scenario.py` | gm-core started on a leftover `gm-fs-easy` (shared BO agent); `base-win` hit free spins 10/24 spins and ran 17 min |
| **Facts + marks saved in `result.json`** | `run_scenario.py` | a failed run could not be diagnosed afterwards |
| **Extension offer wait is idle-based**, not a fixed 12 s: wait while `busyOps` or popups are up, conclude "no offer" after 4 s idle, 60 s cap; the FreeSpinResult popup counts as rest | `scenario_engine.py` `_EXT_WAIT_JS` | a last free spin with drama + glow presented ~18 s; the offer appeared after the window and the game sat on it |
| **`BUTTON_VIA`**: a control inside a panel is opened through it (`click_button`, hold, double-click via `_open_via`); `close_opener_if_left` closes the panel after | `game_profile.py`, `scenario_engine.py` | Grandma moved History + Info into Settings |
| **`close_history` closes a round detail first** (`detail_close`) | `scenario_engine.py` | the detail covers `history:close`; every close waited 8 s |
| **Wallet moves exact 3-decimal amounts** (withdraw rounds DOWN, deposit UP). Verified live: withdraw 0.005 exact. | `framework/helpers/wallet.py` | rounding to cents left 0.097 instead of 0.10 → "Oops" on the one funded spin |
| **Display amounts**: profile `DISPLAY_DECIMALS` / `DISPLAY_TRUNCATES` + `display_amount()`; Grandma = 2 decimals, CUT (0.195 → 0.19, 15069.237 → 15,069.23) | `game_profile.py` | an AI read of an amount was compared to the raw server value |
| `bonus-resume-denomination-swap`: **`load_until_ladder`** — load WITHOUT Start and reload until the launch payload carries the new ladder (30 s); otherwise play the bonus out and return **unreached** (propagation, not a game defect). New `ctx.load_game()`, `launch_ladder()`. New fact `bet_during_bonus_is_pending`. | `scenario_engine.py` | same 5 s wait gave 11.00 in one run and 0.10 in the next |
| `freespin-zero-balance`: **`reload_mid_bonus`** param (default True = old behaviour; False = play the bonus start→end, marks `trigger`/`terminal`) | `scenario_engine.py` | Grandma TC-010 requirement has no reload |
| `insufficient-balance`: **re-read the bet after the wallet reload** | `scenario_engine.py` | reload reset the bet to 0.10; the spin was affordable and no popup came |
| `base-win` / `spin-then-history`: display facts `win_payout_display`, `balance_after_win_display`, `<key>_payout_display` | `scenario_engine.py` | see Display amounts |
| Big-win skip diagnostic prints the control's rect + canvas size | `scenario_engine.py` | the bridge team asks for those numbers first |

---

## 3. Grandma suite changes

**All six suites:** every run has a `HOW:` block in plain language, one action per line
("click the auto button", not `auto`), derived from the scenario code — no assumptions.

| Suite / case | Change |
|---|---|
| gm-core **TC-005** | AI reads the bet (`bet_text`), compared to `{facts.bet_amount}` |
| gm-core **TC-006 / TC-007** | aligned to the other games: `conservation` + `{res.stake}/{res.payout} > 0`; removed arithmetic on derived balances (it only re-checked rounding). TC-007 AI reads compared to the `_display` facts. `base-win` clip is now `at: win` (the winning spin only) |
| gm-core **TC-008** | removed `{facts.mode} == 'message'` (mode is only 'wallet' or 'bet' — could never pass) |
| gm-core **TC-010** | `reload_mid_bonus: false`; asserts `fs_rounds_played >= 1`, `reloaded_mid_bonus == False` |
| gm-core **TC-011** | detail win compared to `normal_win_payout_display` |
| gm-core **TC-054 / TC-056** (tutorial) | **image** assert, `combine: true` (all screenfuls as one document); new `pages_differ`, `images_in_place` |
| gm-core **TC-051–053** (language) | `languages: [en, my, zh, ru]` (was ONE language), **image** per-shot, Zeus's proven question set, Burmese-toured check; removed `failed == 0` ('none' == 0 never passes) |
| gm-lastbet **TC-082** | `outcome: lose` (was `no_win`, which silently meant "any"); `dwell_s: 2` |
| gm-lastbet **TC-082 / 083 / 085** | prompts compare BEFORE vs AFTER: `grid_same`, `bet_same` (085: `bet_same_as_changed`), `balance_same`, `win_same`, (083) `win_marking_same` |
| gm-lastbet **TC-086** | requirement-based: clip `at: [resume_first, restored]` (trigger spin is never shown); AI reads bet during bonus → `{facts.pending_stake}`, after → `{facts.new_default_bet}`; streams `bet_during_bonus_is_pending`, `bet_after_bonus_is_new_default` |
| gm-autoplay (aligned to Dragon `dr-autoplay.yml`) | TC-021/028 `auto_spins_at_start >= auto_count - 1` and `< via_count`; TC-027 DR-19 checks + AI `unlocked_after` (the watcher was never started); TC-023 `stop_on: base_win`, stream only; TC-009 + `extra_round_after_stop`; TC-029 `{res.stake} == {facts.picked_bet}`, retitled "bet chosen before it was confirmed" |
| **every prompt** | ~20 answer leaks removed (values, "should", format examples equal to the answer, rule-under-test framing) |
| `test.yml` | now holds the image `open-tutorial` run (mirrors gm-core) |

---

## 4. Grandma profile (`scripts/game_profile.py`, class `Grandma`)

`history` → `settings:history`, `info` → `settings:info`, `BUTTON_VIA` for both;
`detail_close` → `historyDetail:close`; `DISPLAY_TRUNCATES = True`.

---

## 5. Documentation

| Doc | What |
|---|---|
| `docs/game-bridge-contract.md` | §5 `winFormula`; §6 full BET response + **cascade step** + **Hold & Spin fields** (moved in); §7 `dismissPopups` must NOT force-close presentation popups; §8 **Presentation skips** (`big_win_skip`, on-canvas rect rule), **role registry + popup labels** (moved in), **"Main screen (legacy UI) or Settings menu (new UI)"** — a table to fill for History / Info / Turbo / Sound / Music per game, bridge + profile rules, `BUTTON_VIA`; §10 row 6; §11 `probe_big_win`; **§12 Onboarding a new game** (moved in) |
| `docs/cascade-game-bridge-contract.md` | **deleted** (was not in git). All 12 references repointed |
| `docs/scenario-assert-authoring.md` | **"Video or image — which assert to write"** |
| `docs/scenario-toolkit.md` | `load_game` / `load_until_ladder` / `launch_ladder`, `close_opener_if_left` |
| `docs/grandma-bridge-defects.md` | G1 fixed; G2 open (doc); G3 written (now fixed); screenshot `docs/grandma-g3-history-rows.png` |

---

## 6. Bridge defects (`docs/grandma-bridge-defects.md`)

| | Defect | Status |
|---|---|---|
| G1 | `bigWin:skip` resolved but its centre was off-canvas → never clickable | **fixed** by the bridge team |
| G2 | bridge-api.md main-screen table still lists `history` / `info` (they are inside Settings) | **open** — doc only; send to game team |
| G3 | `getHistoryRows()` returned `[]` while History showed rows | **fixed** by the bridge team (TC-011 green after) |

---

## 7. Likely GAME findings — confirm, then report to the game team

- **Last bet does not restore the view.** Reopening keeps the stake, but the grid is different and
  the win amount / win line are gone. Checked on video frames (TC-082, TC-083). The matrix
  requires "correct view, win line, win amount and stake". Stream checks pass because the server's
  `lastBet` is right; it is the client that does not redraw it.

---

## 8. Open items

1. **Commit** (115 paths).
2. **TC-047** still has `{facts.space} == True` (a mark used as a fact → permanent red). Fix:
   delete it (`{facts.key_rounds} == 1` is on the next line).
3. **Validator gap**: `validate_suites.py` merges `marks={...}` keys into the facts set, so a mark
   used as `{facts.x}` passes validation. Its error text ("would quietly PASS") is also stale —
   such an assert now fails (`None == True`).
4. **TC-085 expected behaviour — needs the user's decision.** The game reopens on the PLAYED stake
   (0.10). The suite asserts it opens on the CHANGED bet (`bet_opens_at_changed_value == True`,
   AI `bet_same_as_changed: true`) — the opposite of the scenario's design (CR_095: assert False).
   The matrix says "default bet level of the denom list". In the runs so far 0.10 was both played
   stake and default, so the run cannot tell them apart; play at a non-default bet once decided.
5. **`paytable-bet-scaling` (TC-013/014/055)** is still a video assert; by the new rule it should be
   image (other games use image). Not changed yet.
6. **TC-051** matrix = "language in sync BETWEEN games" — not covered (needs a second game).
7. **Runs of the same scenario share one result folder** (e.g. three `last-bet-restore` runs):
   `result.json` / `timeline.json` / `replay.html` of earlier runs are overwritten. Give each run its
   own folder.
8. Summary 12 carry-over: verify `bo.paytable_code` for Grandma (unset → falls back to
   `AutomationTest`); the grid read + compare (P7); three bridge additions (`getReelGlowState()`,
   Shake/Swirl, Energy Trail).
9. Report §7 and G2 to the game team.

---

## 9. Not yet confirmed on a live run

Suite-start paytable reset; idle-based extension wait; `load_until_ladder` (TC-086);
`reload_mid_bonus: false` (TC-010); `insufficient-balance` bet re-read (TC-008); detail-first
`close_history`; `BUTTON_VIA` for `info` (open-tutorial, paytable-bet-scaling); image tutorial and
language runs; Dragon-aligned gm-autoplay; display-amount comparisons (TC-007, TC-011);
`base-win` `at: win`; report layout below.

Confirmed live this session: `BUTTON_VIA` for History (TC-011 green), wallet 3-decimal moves,
G1 and G3 bridge fixes.

---

## 10. Report (`scripts/run_scenario.py`, `scripts/report_template*.html`, replay templates)

- Orange verdict label **"Partly Failed"** in a real orange (was a pale-yellow "Review").
- Scenario description removed from the report.
- Each case collapses to one line (id — title — status) with a toggle chevron on the right.
- **▶ AI video** button in the run's footer next to Watch replay / View result JSON — opens the
  montage the AI graded; one button per distinct clip.
