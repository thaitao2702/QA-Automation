# Session Summary 6 — §6 resolved, a real blocker found, and four rules written down

Continues `session-summary-scenario-qa-5.md`, whose §6 was the open question this session started on.
`session-summary-scenario-qa-3.md` is still the onboarding guide for a NEW GAME — **read that first**.

This session: settled §6 (the answer was the opposite of what it predicted, twice over), root-caused
the real blocker behind it, fixed three silent-pass bugs, and wrote down four rules the suite had
been breaking. Four Chaos cases are now green against the live game for the first time.

It ends on **two things blocking further live work** (§7) — one of them just needs a token.

---

## 1. Where things stand

| | |
|---|---|
| Chaos cases | **78** across 5 suites |
| Offline harnesses | 67 + 45 + suite validation + index — all passing, no browser |
| Green on the LIVE game | CR_007, CR_008, CR_009, CR_012a, CR_012b, CR_020a |
| Blocked | anything with a `paytable:` (BO token expired) · every run's first click (defect C1) |
| New docs | **`docs/chaos-bridge-defects.md`** · Rules 7–10 in `scenario-authoring-rules.md` |

---

## 2. §6 of the last summary — resolved, and it was neither of the two guesses

**The alias table exists. The profile was already right.** `getButtonRect()` resolves the reference
spellings (`stakeIncrease`, `trophy`, and `bet:close` / `bet:plus` / `bet:row:{i}` while the list is
open) as well as this build's native `betPlus` / `monumentalWin` / `denomination:*`. `bet:close`
resolves in 0.6s and really closes the list. **Do not revert those six roles.**

> **`getAllButtonRects()` is an inventory of CANONICAL names, not of resolvable ones.** Reading it as
> the latter is the whole reason §6 suspected the profile. This is now written into the profile
> comment and into `docs/chaos-bridge-defects.md` C2.

**The ~70-second stall was a popup-LABEL typo.** `ChaosRunner.POPUPS` inherited the reference
`"denominations"`; this build's `visiblePopups` says `"denomination"` (and `"autoplay"`, not
`"autoPlay"`). Unlike button names, **popup labels are not aliased** — they are raw strings.

So `close_bet_list()`'s "is it still open?" guard compared `"denominations"` against
`["denomination"]`, concluded the list was already gone, and returned
`closed / close_button_worked / needed_dismiss = True` **having clicked nothing at all**. CR_004's
three asserts passed on a popup still on screen — a false green — and the next `settle()` then burned
its full 60s because a visible popup blocks `waitForSpinClickable`.

Fixed in `ChaosRunner.POPUPS` (labels verified live, one surface at a time) and hardened in
`close_bet_list()`: the guard no longer trusts the profile label — any visible popup counts — so a
label typo can never again report success without clicking.

---

## 3. ⚠ C1 — the blocker, root-caused and with a one-line fix

**The first click after boot is intermittently swallowed.** Not a naming problem, not a readiness
problem in the bridge's own terms:

```
document.elementFromPoint(379, 766)
  swallowed -> DIV#canvasContainer.canvas-container   (z-index: 1000, pointer-events: auto)
  working   -> CANVAS#GameCanvas
```

The click IS delivered — all five pointer events fire — but every one targets `DIV#canvasContainer`,
so it never reaches the Cocos input system. ~1s later the same point hit-tests to the canvas and the
identical click works. **Correlation is exact, 12/12 loads.**

`waitForButtonClickable` cannot see this: its hit-test runs in **Cocos node space**, and a **DOM**
element over the whole canvas is invisible to it. Nothing in the reason vocabulary can express it.

**The fix, verified:** `#canvasContainer { pointer-events: none }` → **0/10** swallowed vs **2/10**
unpatched. Full write-up, reproduction and the suggested bridge-side guard (`dom_overlay:<id>`) are
in `docs/chaos-bridge-defects.md` C1.

Deliberately NOT worked around in the framework: a blanket pre-click sleep would be paid by every
case and would turn this into invisible latency that hides the next genuinely inert control.

---

## 4. Three silent-pass bugs fixed

Each one PASSED while testing nothing. This is the section to read.

| bug | what it did | fix |
|---|---|---|
| popup label (§2) | reported the list closed without clicking | label verified live; guard no longer trusts it |
| `won_payout` on a free-spin stop | a free-spin TRIGGER pays 0 — the money arrives across the drained bonus. `auto-stop-at-win` published the trigger's `0.0` as "the win", so `{facts.won_payout} > 0` failed a textbook stop-at-win | sums the whole event; `won_via` / `won_by_feature` record the shape |
| `at: win` on a feature stop | clipped a free-spin stop to the trigger spin, which shows no win at all. The AI reported `win_shown: false` and **was right** | marks `win` AND `win_end`; the case spans `at: [win, win_end]` |

Two more of the same family, found by asking rather than by a failing run:

* **`payline_rate_matches_paytable` fails on a scatter-only win.** Chaos's P1 pays anywhere, so a
  round can satisfy `payout > 0` while reporting no `paylines` at all; the invariant then correctly
  refuses to pass on an empty set. It made CR_008 a coin flip. **The check has since been removed
  entirely at the maintainer's direction — the BO paytable changes and is not maintained against the
  suite, so it would go red on a config change rather than on a defect.** Everything CR_008 asserts
  now derives from the server's own reported numbers.
* **`confirm_stopped` sampled the auto flag once**, and the client clears `isAutoPlay` only after it
  finishes presenting the round that stopped auto. A big win's count-up outlasted the watch window,
  so CR_020 failed on `auto_flag_after` in a suite run and passed 7/7 in isolation. A direct probe
  proved the bridge is fine (flag false immediately, stable 30s). Now POLLED for up to 5s, with
  `auto_flag_cleared_s` exposed so a genuinely stuck flag still fails and a slow one is visible.

---

## 5. The wallet no longer drifts

`deposit-10000` added a flat 10,000 to wherever the balance happened to be — and a run that failed
*before* withdrawing anything still paid the deposit. Observed climbing **9,950 → 69,968 in one
morning**.

That drift is not untidiness, it is what made `insufficient-balance` fail as
`could not set balance: Insufficient balance`: `set_balance` reads the wallet then withdraws the
difference, so a balance that moves in between is refused, and the message names the wallet rather
than the race.

**New `restore-balance` teardown** puts the wallet back to exactly what it was, reading the
pre-change value from the wallet API (not the game's on-screen figure, which lags). If a run never
moved the money it does nothing. All 9 teardowns across chaos/mystic/zeus switched;
`deposit-10000` is deprecated but kept. CR_009 then went green first time, wallet `10000.21` →
`10000.21`.

---

## 6. Four rules written down

`docs/scenario-authoring-rules.md` now has Rules 7–10. Each was learned the hard way this session.

**Rule 7 — a test that moves the money puts it back exactly.** `teardown: restore-balance`, never
`deposit-10000`.

**Rule 8 — a bonus plays ITSELF; tear the client down before acting on it.** The client auto-plays a
feature, so `set_balance` races the remaining spins: the bonus can finish, or hit the cap, while the
API call is in flight, and the reload then resumes nothing. New `ctx.stop_client()` navigates to
`about:blank` so no round can play; the bonus is server-side and survives. Come back with
`reload_and_start(url=ctx.url)` — a plain reload would re-load `about:blank`. A sleep is not a
substitute; it only makes the race longer.

**Rule 9 — the max-win cap can END a bonus at any moment, including its first round.** The server
sets `hasReachedMaxWin` and returns to base with spins still on the counter, forfeited. Handle it at
three points (trigger / first free spin / any later round) by **discarding the attempt and hunting
again**, not by failing. And note that a bonus which ended early still satisfies `terminal_reached`
and `{terminal.free_spins} == 0` — so assert a fact proving the behaviour actually happened.

**Rule 10 — the bridge tells you what the game THINKS; only the AI tells you what the player SEES.**
A `stream:` assert reads the game's own account of itself; the screen is rendered by a separate layer
and can be wrong on its own (`isAutoPlay: false` while the auto counter still spins; a correct
`balance` behind a stale label; a history row with right numbers rendered blank). **A `video:` /
`image:` assert is required**; if a case has none, the reason is written next to it and must be
"there is nothing to see". It does not replace the stream asserts — exact values and "it did not
happen" stay there, because an AI cannot prove a negative from downsampled frames.

> Chaos and Mystic had drifted to opposite halves of this: Chaos's CR_020/022/023 asserted only the
> stream, Mystic's Mah_015/017/018 only the video. Six Chaos cases gained a video assert
> (CR_019, CR_020a, CR_020b, CR_022, CR_023, CR_025); CR_010 and CR_041 keep none, with the reason
> written in place.

---

## 7. ⚠ Pick these up first

### a) The BO token is expired — this blocks every forced-paytable case

```
paytable setup failed: Unauthorized — re-login required
```

`BO_TOKEN` **and** `BO_REFRESH_TOKEN` in `.env.dev` are both dead, so the automatic refresh cannot
recover. **Also delete `.bo_token.dev.json` at the repo root** — that cache overrides the env vars,
so a new token will not take effect while the stale one sits there.

Unverified until it is back:

* **CR_011** `freespin-zero-balance` — rewritten this session (observation window, cap discard/retry,
  `stop_client` before zeroing) and **never run live**.
* **CR_020b** — the free-spin half of stop-at-win, **never run live**.
* Anything else carrying `paytable:` (reels-glow, the ante free-spin runs).

### b) C1 — file it with the bridge team

`docs/chaos-bridge-defects.md` C1 is written to be handed over as-is: root cause, exact reproduction,
the 12/12 correlation table, the verified one-line fix, and a suggested `dom_overlay:<id>` reason so
a future overlay fails loudly instead of silently. Until it is fixed, any case whose first action is
a click is a coin flip — that is what killed the last `auto-stop-at-win` suite run
(`waitForButtonClickable(auto:stopAtWin)` timed out because the `auto` click that opens the panel was
swallowed).

### Also open

* **The dev launch token expires within hours.** Mint one with
  `python scripts/get_game_url_playsite.py --env=dev --game sl_35_chaos_runner` and splice the `data=`
  parameter onto the LAN build URL, keeping `&bridge=1&debug=1` — without it the bridge never
  registers.
* **The Chaos free-spin paytable is now 30%, not 91%.** `chaos-fs-easy.json` was regenerated with
  `--probability 0.30` on BaseGame reels 0/1/2. Chaos awards `base(reels) × WAYS`, so filling three
  reels meant ~27 ways and a bonus of hundreds of spins every run; at 30% a reel usually contributes
  one symbol, ways average ~2.5, and the award stays near the 12-spin minimum. Trigger rate ~37%
  (about 1 in 2.7 spins). The same reasoning applies to any game whose award scales with the symbol
  COUNT rather than just the trigger.
* **`spin-then-history` is already mechanism-aware** — `win_steps()` splits cascade vs single-grid and
  `cascade_win` is dropped when `GAME.mechanism != "cascade"`. Chaos's detail does not page, so its
  run passes `detail_scroll_steps: 0`. If this build's detail turns out to page, raise it and assert
  `facts.<outcome>_scrolls`.
* **The validator now understands f-string facts.** `_inspect` writes `f"{key}_stake"`, which a static
  walk cannot see, so twelve correct asserts were being reported as "not exposed". A scenario
  declares `FACT_PREFIXES` and `validate_suites.py` crosses them with the suffixes it finds in the
  source — verified to still catch a typo (`no_win_staek` → 2 warnings).
* **Chaos case ids suffix with a LETTER, not a word.** `validate_suites.py` maps an id to its CSV row
  by stripping a single trailing lowercase letter, so `CR_012a` works and Mystic's `Mah_012-nowin`
  style would not.

---

## 8. Where the files are

| | |
|---|---|
| Bridge defects (hand to the bridge team) | **`docs/chaos-bridge-defects.md`** |
| Authoring rules (read before writing a case) | **`docs/scenario-authoring-rules.md`** — Rules 7–10 are new |
| Scenario lookup | `docs/scenario-index.md` (regenerated by `tests/chaos/build_scenario_index.py`) |
| Profile | `scripts/game_profile.py` — `ChaosRunner.POPUPS` / `BUTTONS` carry the live-verified dialect |
| Engine | `scripts/scenario_engine.py` — `stop_client`, `restore_balance`, `AutoStopAtWin.stop_on`, `confirm_stopped` polling |
| Suites | `games/chaos/test-suites/cr-{core,ante,features,auto-turbo,ui-resilience}.yml` |
| Forced paytable | `games/chaos/paytables/chaos-fs-easy.json` (30%) |
| Harnesses | `tests/chaos/` — all four pass with no browser |
