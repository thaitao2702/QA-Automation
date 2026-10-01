# Session Summary 7 — four silent-pass bugs, deterministic paytables, and the AI results nobody could see

Continues `session-summary-scenario-qa-6.md`. `session-summary-scenario-qa-3.md` is still the
onboarding guide for a NEW GAME — **read that first**.

This session ran across both games. It started on Mystic (the popup cases), moved to Chaos (forced
paytables, prompt merges, Zeus alignment), and along the way turned up **four framework bugs that
made asserts unfailable or unpassable**, a **config fault that stalled every Chaos run before the
Start button**, and the fact that **image AI results were computed but never displayed anywhere**.

It ends with one real product defect found by the suite (§8) and no framework blockers.

---

## 1. Where things stand

| | |
|---|---|
| Chaos cases | **77** across 5 suites (was 78: two dead sound cases out, CR_035 in) |
| Mystic popup cases | Mah_105 / 107 / 108 in `mah-popups.yml`; **Mah_106 is N/A** — this build has no Ante Bet |
| Forced paytables | `chaos-fs-easy` (30%) · **`chaos-p1-ways` (30%)** · **`chaos-wild-rain` (15%)** |
| Authoring rules | **18** — Rule 6 rewritten, Rule 9 corrected, Rules 17 & 18 new |
| Chaos env | **fixed** — was stalling every run before Start (§5) |
| Live-verified | the language tour ran end to end and found a real i18n defect (§8) |

---

## 2. Four framework bugs — all of them silent

These were found while wiring CR_062/063, and every one of them made a written assert do something
other than what it says.

**A name with a DIGIT never bound.** `_TOKEN`'s field group was `[a-zA-Z_]+`, so `{facts.p1_ways}`
did not match, was left in the text verbatim, and the predicate then `eval`ed a set literal
(`{facts.p1_ways} >= 1`) and died — a permanent red no game state could fix. In a PROMPT the model
was shown the literal `{facts.p1_ways}` placeholder.

**An invariant name with a digit never resolved.** Same flaw in `run_stream_assert`'s regex, so
`p1_reels_only`, `p1_ways_payout` and `scatter_pay_matches_p1_ways` fell through to the inline
branch and `eval`ed as bare Python names → `NameError` → permanent red.

**The validator's own regex was narrower than the engine's**, so it silently SKIPPED those
references — a typo in one would have sailed through the check that file exists to make. It now
mirrors `_TOKEN` exactly.

**A container fact rendered as a quoted string.** `_expr_literal` fell through to `repr(str(val))`,
so `{facts.illegal_reels} == []` became `'[]' == []` — false for a clean audit as well as a dirty
one. Lists/tuples/dicts/sets now render as themselves.

> Blast radius was contained to the P1 cases; no Mystic or Zeus suite used digit-bearing names.
> The lesson worth keeping: **an identifier regex that forbids digits fails loudly in one place and
> silently in another.** If a new one is written, mirror it in `validate_suites.py` the same day.

---

## 3. Deterministic paytables — and the two ceilings

Rule 6 said the opposite of what the fixtures actually do; it described the old 91% `chaos-fs-easy`
and dismissed `--probability`. It has been rewritten around the real principle.

**Weights are per CELL.** A reel forced to 100% of a symbol shows it in EVERY visible row, and in a
ways game the outcome is the PRODUCT of the per-reel counts, so a force multiplies the very thing
you were keeping small:

| | 100% force | ~30% probability |
|---|---|---|
| Chaos free spins | ~27 ways → hundreds of spins | ways ~2.5 → the 12-spin minimum |
| Chaos P1 | 27 ways → 270x base, always capped | 1–2 ways → ~26x base, the ordinary win |

**A two-sided rule has a CEILING as well as a floor** (new in Rule 6). `raining-wild` needs BOTH a
2+ Wild result and a control with fewer than 2, because "the animation must not play below the
threshold" is half the rule. At 50% the control takes ~315 spins; at 100% it is impossible and the
run never reaches. 15% splits ~55/45 and gets both in a median of 3 spins.

**Never force a symbol onto a reel the rule under test forbids.** `chaos-wild-rain` leaves reel 0 at
weight 0 precisely because CR_059 asserts Wild never lands there.

| fixture | what | used by |
|---|---|---|
| `chaos-p1-ways` | P1 ~30% on base reels 0/2/4 | `p1-ways-win` — ~28% of spins win, in 1–4 spins |
| `chaos-wild-rain` | WILD! ~15% on base reels 1/2/3/4, reel 0 untouched | `raining-wild` — both halves in ~3 spins |

---

## 4. Cases that got sharper

**CR_063 now tests the documented formula ON SCREEN.** Design doc §4 gives
`Total Payout = Total Ways × Symbol Multiplier × Bet base`. `win_amount_text` was in the schema but
NOT in `expected` — the AI read it and nothing checked it. It is now compared numerically against
`{facts.p1_pay}`. Two setup changes were forced by that:

* `max_win_x: 500`, not `10 * bet` — a capped round credits the CAP, so the screen would show the cap
  and the formula would be untestable from video. This paytable's largest possible win is 270x base,
  so 500 can never bite. (Every other Chaos run keeps the tight cap.)
* the scenario **prefers a scatter-only win** — a round that also paid a line shows the SUM, which
  proves nothing about either half. `{facts.scatter_only} == True` is asserted so a fallback is visible.
* `formula_ok` is computed in Python because an inline predicate is `eval`ed **without builtins** —
  there is no `abs()`, and `2 × 10.0 × 0.6` is `12.000000000000002`.

**`p1-ways-win` is one mode again.** `require_win` and the audit branch are gone; it always forces
and always demands a clean win. CR_062 rides on the same run, which is a real reduction in what it
demonstrates — it audits ~1–4 rounds on the FORCED paytable rather than 250 on the real strips. Its
anti-vacuity guard changed from `rounds_audited >= 20` (now unreachable) to
`{facts.p1_reels_seen} == [1, 3, 5]`, which proves P1 was actually observed, on the legal reels and
no others. **If CR_062 needs to be strong again it wants its own unforced run.**

**`random-multiplier` grades variation against the RESPONSE, not against `true`.** x1 carries 80% of
the base weight and 65% of the free-spin weight, so three base spins come back all-x1 about half the
time on a correct game. The scenario exposes `varied`; the suite writes
`values_vary: "{facts.varied}"` — the oracle-computes-expected pattern.

---

## 5. The Chaos env was stalling every run

Symptom: "stuck at the ready button", blamed on `language-tour`. It was neither.

The server said it over the game's own websocket: `{"code":-4,"msg":"Session is not exists or
expired"}`, with "Oops! Invalid request [-4]" on screen, `sceneReady: false`, `getGameInfo(): null`.
**The token pasted into `url:` had expired**, so the game never reached Start — on EVERY run, not
just the tour.

It looked like the language step because that is the one scenario that re-mints tokens: a fresh mint
resolves to `https://dev-play.royaledge.io`, whose build has **no `__gameTestBridge`**. The
bridge-enabled build is the local one.

```yaml
# games/chaos/env.config.dev.yml — fixed
base_url: "http://192.168.1.27:7456/web-mobile/sl_35_chaos_runner/index.html"
url: ""      # a pasted token expires, and language runs skip it anyway
```

`base_url` mints a fresh token and grafts its query onto the LOCAL build — a live session AND the
bridge. Verified through `se.start_game()` in three languages: resolved in 4.2s / 0.5s / 0.6s,
`sceneReady: true`, spin OK. Previously it hung for the full 60s timeout every time.

---

## 6. The AI results nobody could see

`language-tour` ran, the AI graded 18 stills, verdicts were correct — and the console printed
`stream=1/1` and nothing else, because **the per-case reporting loop only ever iterated
`c["video"]`**. `report.html` had the same omission. An image-graded run was indistinguishable from
one where the AI never ran.

Fixed in three places:

* **console** — `image=x/y` per case, then each assert, then the failing shots by name.
* **report.html** — one collapsible `<details>` section per image assert: the prompt, the expected
  map, and a per-shot toggle carrying the AI's answer, its mismatches, its prose reason **and the
  screenshot it graded**. Failing asserts and failing shots open by default; passing ones stay shut.
* **counts** — `grading N screenshot(s)` counted spec × shot, reporting 54 for a run that really
  makes 18 calls. Now deduped the way the grader is. The headline says "AI call(s)", not "video".

---

## 7. `combine: true` — screenfuls of one surface (Rule 18)

An `image:` assert grades one call per still, which is right for a tour of different surfaces and
**wrong for screenfuls of one scrolling surface**. Graded per still, "does this page show RTP
information?" is honestly "no" for every page that does not have it, so four of five shots failed an
assert the document satisfied. Surviving that forced a CONDITIONAL prompt — "if this page shows RTP,
answer about it" — which can never catch a section that is **missing altogether**, the thing the case
exists to check.

```yaml
image:
  combine: true        # every still of this surface -> ONE call
```

The first still goes as primary and the rest as `extra_images` (every provider already accepted
them); the prompt is auto-prefixed with the framing. Applied to the Chaos tutorial and Game Help
runs. **`language-tour` deliberately does NOT combine** — its stills are different surfaces AND
languages, and combining would lose the per-screen verdict.

---

## 8. What the suite found — hand these to the game team

**i18n: untranslated strings in every language.** `language-tour` ran live (3 languages × 6
surfaces). CR_064 orange on **10 of 18 screens**, and the model reached it independently in Burmese,
Hindi and Tagalog:

> "The language is mostly Burmese, but the 'Ante Bet' label is in English"
> "The game title 'Chaos Runner' is displayed…"

So "Ante Bet" and the title are not going through translation. CR_066 (overflow/collision) and
CR_067 (clipped glyphs, diacritics, descenders) are **green across all 18**, including Burmese.

**`startGame()` hangs instead of rejecting.** `docs/game-bridge-contract.md` §2 requires it to
"reject if the Start control never becomes available, rather than hanging". Chaos's does not — which
is what turned an expired session into an opaque 60s stall (§5) instead of one line of error text.
It also "drives to clean idle" internally, which the contract forbids for the same reason (Zeus D1).

**`429 OPEN_GAME_FAILED` on rapid token mints.** Two `openGame` calls within a second are rate
limited; ~4s apart is fine. The tour has a page load and a six-surface walk between mints, so it
should not bite in practice — but a retry loop would hit it.

**Mystic's dev build has no QA bridge** (carried over from earlier this session): the game boots and
its socket connects, but `window.__gameTestBridge` never appears with `&bridge=1` set. Every Mystic
scenario fails at load. Needs a rebuild with the bridge included.

---

## 9. Mystic — the popup cases

`mah-popups.yml`: **Mah_105** (toggle-turbo), **Mah_107** (toggle-auto), **Mah_108**
(stacked-popups). The notification is a `video:` claim, but the feature's own state
(`getSpeedMode` / `isAutoPlay`) is a fact with a hard `stream:` assert — so a toggle that silently
no-ops goes RED, not orange.

**Mah_106 is N/A**: this build has no Ante Bet. There is no `toggle-ante` scenario; the run was
removed rather than left to report `inconclusive` forever.

`stacked-popups` **probes** which feature buttons exist (`ctx.has_button`, an instant `getButtonRect`)
and bursts only on those. Waiting on an absent button would inject multi-second gaps into the very
back-to-back burst the case is about. Each tap is marked `TOGGLE: <button>`, so the prompt names no
fixed sequence and the real ACTION TIMELINE tells the AI what happened.

New `DriveContext` primitives: `ui_read`, `has_button`, `popups`, `dismiss_popups`,
`start_capture` / `end_capture`, `start_popup_watch` / `stop_popup_watch`.

> ⚠ Watch for this class of break: `ToggleAnte` was deleted from the file while `REGISTRY` still
> referenced it. `py_compile` passes (valid syntax) but the module raises `NameError` on import, so
> **every scenario in every game** dies at startup. Grep the registry after deleting a class.

---

## 10. Gotchas worth carrying forward

* **A permanently-inconclusive run is worse than a documented gap.** `sound-toggles` returns
  unreached when a game has no sound getters, and `chaos.HAS_SOUND_GETTERS` is False — so CR_031/032
  reported `inconclusive` on every execution while occupying two rows in the report. Removed, with
  the bridge ask written down (Zeus automates them exactly, via `isSoundEnabled`/`isMusicEnabled`).
* **Zeus is the reference for shared cases.** `zz-input-sound-reconnect.yml` gave three alignments
  this session: assert `played_after_reconnect` as well as `recovered`; grade `returned_to_landing`
  (it was in the schema but not in `expected`, while the case title claims it); and CR_035 — from the
  CLIENT's side a Wi-Fi handover IS a drop and restore, so the same run answers it.
* **A schema field not in `expected` is read and never checked.** Three cases had one. Worth a grep
  before trusting a video/image assert.
* **Variable naming** — name for what it holds; a qualifier must disambiguate something real; `_count`
  for counts; no `_here` / `_now` suffixes. `win_base` was wrong because the stake does not change
  between rounds.

---

## 11. Where the files are

| | |
|---|---|
| Authoring rules | **`docs/scenario-authoring-rules.md`** — 18 rules; 6 rewritten, 9 corrected, 17 & 18 new |
| Bridge defects | `docs/chaos-bridge-defects.md` · `docs/game-bridge-contract.md` §2 (the `startGame` clause) |
| Chaos suites | `games/chaos/test-suites/cr-{core,ante,features,auto-turbo,ui-resilience}.yml` |
| Chaos paytables | `games/chaos/paytables/` + `README.md` (recipes for all three fixtures) |
| Mystic popups | `games/mystic/test-suites/mah-popups.yml` |
| Engine | `scripts/scenario_engine.py` — popup/UI primitives, `P1WaysWin`, `RainingWild`, `RandomMultiplier` |
| AI pass | `scripts/ai_video.py` — `combine`, multi-image calls, per-shot results |
| Runner + report | `scripts/run_scenario.py` — `_image_spec`, `ai_calls`, `write_report` |
| Validation | `tests/chaos/validate_suites.py` — passes; 77 cases, 71 CSV rows covered of 111 |

---

## 12. Open items

1. **Run the Chaos suites live** now the env is fixed — most of this session's work is verified
   offline (against real captured payloads and generated grids) but only `language-tour` has run
   against the game.
2. **Mystic needs a bridge-enabled build** before any of its suites can run at all.
3. **CR_062** is weaker than it was (§4). Decide whether it gets its own unforced run.
4. **Hand §8 to the game/bridge teams** — the i18n defect is a real finding; the `startGame()` hang
   costs an hour every time an environment goes stale.
5. `cr-ui-resilience.yml` still has three cases with no AI claim (CR_065, CR_098, the Last Bet trio),
   each with a written reason. CR_098 is BO-config-dependent; Last Bet is bridge-blocked.
