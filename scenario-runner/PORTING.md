# Porting the scenario runner to TypeScript

Python stays the runner until each game is cut over. This folder holds the port and the evidence
that it behaves the same.

## Scope

- **Ported:** `scripts/run_scenario.py`, `scripts/scenario_engine.py`, `scripts/game_profile.py`,
  `scripts/ai_video.py`, and the helpers they import: `framework/env_files.py`,
  `framework/helpers/{bo,wallet,claude_ai,gemini,gemini_video,local_ai,symbol_reference}.py`,
  `framework/bo/*`, `playsite/{wallet_client,playsite_client}.py`.
- **Not ported:** the legacy step runner (`framework/runner/`), and xlsx/pdf doc ingestion. Both stay
  in Python.
- **Unchanged:** suite YAML, fixtures, HTML report templates, and the `test-results/` layout.

## How "the same" is proved

`baseline/make_baseline.py` runs the PYTHON code on recorded inputs and writes what it answered.
`test/*.test.ts` replays the same inputs through the TS code and must match exactly. A raise is part
of the answer: `{"err": "ValueError"}` must come back as a thrown error with that `name`.

Regenerate the baseline only on purpose (`python scenario-runner/baseline/make_baseline.py` from
the repo root). A changed baseline means the Python behaviour changed, so the port has to follow it.

## Conventions

- **Method names** are the Python names in camelCase (`pending_resume` -> `pendingResume`). The
  baseline tests map names that way, so do not rename them. Constants keep their Python names
  (`F_GAME_STATE`, `HAS_ANTE_BET`).
- **Python `None` is `null`**, never `undefined`. Read dict fields with `get(d, k)` from `src/py.ts`.
  It returns `null` for a missing key, like `dict.get`.
- **Python semantics live in `src/py.ts`.** Use these helpers instead of the native JavaScript ones:
  - `truthy` / `or` for `x or y`. Python treats `0`, `""`, `[]` and `{}` as false.
  - `pyInt` / `pyFloat` for `int()` / `float()`. They raise on junk; `parseInt` would not.
    `tryInt` / `tryFloat` cover `except (TypeError, ValueError)`.
  - `pyRound` for `round()`: ties go to even, judged on the exact value of the number.
  - `sum` for builtin `sum()`, which is compensated in Python 3.12+. A Python `total += x` loop stays
    plain `+`.
  - `pyStr` / `pyRepr` for `str()` / `repr()`, which matter in prompts and violation text.
  - `pyEq` for `==` (`True == 1`, deep lists).
  - `quantizeDecimal` for `Decimal.quantize`.
- **Known, deliberate difference:** a JavaScript number cannot remember that JSON wrote it as
  `7.0`, so `pyStr(7.0)` is `"7"` where Python prints `"7.0"`. Nothing branches on it; it only
  changes the text of a prompt or a report. Baseline tests drop a trailing `.0` before comparing.

## Status

| Phase | Area | State |
|---|---|---|
| 0 | baseline: py-compat, profiles, invariants, exprs, cases | done |
| 1 | `src/py.ts` | done, matches CPython 3.13 (`py_compat.json`) |
| 1 | `src/profile/*` (7 games) | done, matches `profiles.json` |
| 1 | `src/engine/bet.ts`, `invariants.ts` (33) | done, matches `invariants.json` (151 streams, every profile) |
| 1 | `src/engine/expr.ts` (safe `stream:` evaluator) | done, matches `exprs.json` (432 expressions, incl. CPython error text) |
| 1 | `src/engine/tokens.ts`, `result.ts`, `src/runner/cases.ts` | done, matches `cases.json` (589 runs from all 57 suites) |
| 2 | `src/services/env.ts`, `playsite.ts`, `wallet.ts` | done (unit-tested: dotenv parsing, thousandth wallet step) |
| 2 | `src/services/bo/*` | done, matches `bo.json` (572 recorded calls, network stubbed) |
| 2 | `src/services/ai/*` (AI pass, Gemini / Claude CLI / local, ffmpeg) | done, matches `ai.json` (561 cases: prompts, grading, ffmpeg args, provider flows — all stubbed) |
| 3 | `src/engine/{bridge,drive,flows,shared,teardowns,page}.ts` | done |
| 3 | `src/scenarios/*` (50 scenarios) | done; names, descriptions, params and fact keys match the Python (`scenario_keys.json`) |
| 3 | end-to-end on the fake game (`test/fake-game/`) | 11 runs, outputs identical to the Python runner's (`compare.ts`) |
| 4 | `src/runner/{cli,urls,report,cases}.ts` | done; `--no-ai` runs work. Live runs not tried yet |
| 5 | `src/tools/validate-suites.ts` | done; same result as Python on all 7 games (xlsx matrices: coverage needs the Python tool) |
| 5 | `build_scenario_index` port, docs toolkit names, `/run-test` skill | todo — at cutover, with the docs moved to TS names |
| 6 | per-game cutover, Mystic first | todo |

Notes from phase 1:
- Suites must be parsed as **YAML 1.1** (`parse(text, { version: "1.1" })`), which is what PyYAML reads.
  YAML 1.2 would read `yes`/`no`/`on`/`off` as strings.
- `Bet` and `ScenarioResult` keep snake_case fields. They are a data contract: suites bind them and
  result files store them.
- A shared `video:` block's `group` was `id(block)` in Python and is a counter here. Only which specs
  share a group matters.
- `wild_expansion_is_uniform` lists violations in first-seen order. Python's order came from a set
  of strings, which changes on every run.

Run the tests: `cd scenario-runner && npx vitest run`. Typecheck: `npx tsc -p tsconfig.json --noEmit`.

## Python -> TypeScript map for scenario code

Scenario files live in `src/scenarios/<group>.ts`. Each exports `SCENARIOS: ScenarioClass[]`.

**Names.** Functions and methods use the Python name in camelCase. Options a Python call passed as
keywords become one options object: `ctx.click_button(n, optional=True, timeout_ms=8000, dwell_ms=0)`
becomes `ctx.clickButton(n, { optional: true, timeoutMs: 8000, dwellMs: 0 })`. Data that is a
contract keeps snake_case: `Bet` fields, `ScenarioResult` fields, and fact and mark names.

| Python | TypeScript (module) |
|---|---|
| `GAME`, `set_profile`, `BASE_STATE` / `FREESPIN_STATE` / `VIEWPORT` | `GAME` (`engine/game`); use `GAME.BASE_STATE`, `GAME.FREESPIN_STATE`, `GAME.VIEWPORT` |
| `SUPPORTED_LANGUAGES`, `LANGUAGE_NAME`, `STRESS_LANGUAGE` | same names (`engine/game`) |
| `Bet`, `BetRejected`, `is_terminal`, `cluster_wins`, `win_clusters`, `win_steps`, `is_freespin_trigger`, `enters_holdspin` | `Bet`, `BetRejected`, `isTerminal`, `clusterWins`, `winClusters`, `winSteps`, `isFreespinTrigger`, `entersHoldspin` (`engine/bet`) |
| `_step_multipliers` | `stepMultipliers` (`engine/invariants`) |
| `ScenarioResult(name, reached, res, rounds, timeline, events, (start, end), round_windows=, marks=, facts=, reason=, shots=)` | `new ScenarioResult(name, reached, res, rounds, timeline, events, [start, end], { round_windows, marks, facts, reason, shots })` (`engine/result`) |
| `Unreached`, `require_buttons("a", ("b", 0))` | `Unreached`, `requireButtons("a", ["b", 0])` (`engine/result`) |
| `Scenario` base, `__init__(self, x=1)` | `Scenario` + `static PARAMS = ["x"]`, `constructor(p: Params = {})` with `checkParams(Cls, p)` and `param(p, "x", 1)` (`scenarios/base`) |
| `missing_api`, `_await`, `_diag`, `read_game_info`, `launch_ladder`, `click_start`, `assert_idle`, `wait_spin_clickable`, `click_spin`, `await_bet_response`, `await_freespin_round`, `drain_to_base`, `answer_lucky_spin`, `start_game`, `answer_extension_offer`, `_GAME_INFO_JS` | `missingApi`, `awaitBridge`, `diag`, `readGameInfo`, `launchLadder`, `clickStart`, `assertIdle`, `waitSpinClickable`, `clickSpin`, `awaitBetResponse`, `awaitFreespinRound`, `drainToBase`, `answerLuckySpin`, `startGame`, `answerExtensionOffer`, `GAME_INFO_JS` (`engine/bridge`) |
| `load_until_ladder`, `confirm_stopped`, `auto_stop_button`, `big_win_threshold`, `skip_big_win`, `decline_extension_offer`, `play_out_freespins`, `spin_and_finish` (returns `(b, rounds)`), `finish_round`, `base_rounds_since`, `settle_to_base`, `play_out_holdspin` (returns `(rounds, capped)`), `play_out_bonus`, `_feature_state`, `_feature_on`, `_round_carries`, `_near`, `switch_feature`, `_ante_offered`, `_watch_controls`, `asyncio.Event`, `_SPEED_JS`, `_AUTO_JS`, `_popup_labels` | `loadUntilLadder`, `confirmStopped`, `autoStopButton`, `bigWinThreshold`, `skipBigWin`, `declineExtensionOffer`, `playOutFreespins`, `spinAndFinish` (returns `[b, rounds]`), `finishRound`, `baseRoundsSince`, `settleToBase`, `playOutHoldspin` (returns `[rounds, capped]`), `playOutBonus`, `featureState`, `featureOn`, `roundCarries`, `near`, `switchFeature`, `anteOffered`, `watchControls`, `StopSignal`, `SPEED_JS`, `AUTO_JS`, `popupLabels` (`engine/flows`) |
| `open_bet_list`, `close_bet_list`, `_auto_next`, `_arrived_at`, `purchase_bonus` (returns `(bought, facts)`), `_BUTTON_WATCH_NAMES` / `_JS` / `_READ_JS` | `openBetList`, `closeBetList`, `autoNext`, `arrivedAt`, `purchaseBonus` (returns `[bought, facts]`), `BUTTON_WATCH_NAMES` / `BUTTON_WATCH_JS` / `BUTTON_WATCH_READ_JS` (`engine/shared`) |
| `bridge_is_live`, `return_to_base`, `stop_autoplay`, `ensure_base`, `restore_balance`, `reset_denomination`, `TEARDOWNS` | `bridgeIsLive`, `returnToBase`, `stopAutoplay`, `ensureBase`, `restoreBalance`, `resetDenomination`, `TEARDOWNS` (`engine/teardowns`) |
| `ctx.<attr>`: `page`, `events`, `timeline`, `shots`, `language`, `last_bet`, `api_events`, `round_starts`, `spin_timeout_ms`, `bet_timeout_ms`, `fs_timeout_ms`, `auto_timeout_ms`, `settle_pad_ms`, `ui_dwell_ms`, `step_dwell_ms`, `skip_big_wins`, `_big_win_threshold`, `_wallet`, `_wallet_was`, `_bo`, `_url` / `url`, `_browser`, `_denom_was` | `page`, `events`, `timeline`, `shots`, `language`, `lastBet`, `apiEvents`, `roundStarts`, `spinTimeoutMs`, `betTimeoutMs`, `fsTimeoutMs`, `autoTimeoutMs`, `settlePadMs`, `uiDwellMs`, `stepDwellMs`, `skipBigWins`, `bigWinThresholdCache`, `wallet`, `walletWas`, `bo`, `url`, `browser`, `denomWas` (`engine/drive`) |
| `ctx._record(b, phase, t0)`, `ctx._step(a, d, t0, r)`, `ctx._rect` / `_open_via` / `_on_screen` | `ctx.record(...)`, `ctx.step(...)`; the rest are private, so use `clickButton` / `holdButton` / `hasButton` |
| `ctx.reload_and_start(phase=, url=, on_ready=)` | `ctx.reloadAndStart({ phase, url, onReady })` |
| `ctx.ui_read(js, default, arg=x)` | `ctx.uiRead(js, fallback, x)`. Pass `arg` exactly when the JS takes a parameter |
| `page.evaluate(js, arg)` with a JS string | `evaluate(page, js, arg)` (`engine/page`). **Never** call `page.evaluate("...")` with a string; see `engine/page.ts` |
| `page.wait_for_function(js, timeout=)` | `waitForFunction(page, js, timeoutMs)` (`engine/page`) |
| `asyncio.sleep(s)`, `time.time()`, `asyncio.wait_for(p, s)` | `sleep(s * 1000)`, `now()`, `withTimeout(p, s * 1000)` (`engine/page`) |
| `asyncio.gather` / `ensure_future` | `Promise.all` / a promise you keep and await later |
| `random.Random(seed).sample(xs, k)`, `random.randrange(n)` | `new PyRandom(seed).sample(xs, k)`, `new PyRandom(Date.now()).randrange(n)` (`py`) |
| `print(...)` | `console.log(...)` with the same text. Use the `py` template tag where Python interpolated values |
| `f"{x}"` / `f"{x!r}"` | `` py`${x}` `` / `` py`${r(x)}` `` (`py`) |
| `x or y`, `bool(x)`, `round(x, n)`, `int(x)` / `float(x)`, `sum(xs)`, `==` on JSON values, `sorted(xs)`, `d.get(k)` | `or(x, y)`, `truthy(x)`, `pyRound(x, n)`, `pyInt` / `pyFloat` (or `tryInt` / `tryFloat` for `except (TypeError, ValueError)`), `sum(xs)`, `pyEq(a, b)`, `pySorted(xs)`, `get(d, k)` (`py`) |
| Profile methods `GAME.display_amount(v)`, `GAME.button(role, i=n)` | `GAME.displayAmount(v)`, `GAME.button(role, { i: n })` |

**Rules for a scenario port.**
- Port faithfully: same steps, same order, same timeouts, same facts (names AND values), marks,
  timeline labels, `reason` text and printed lines. Port Python behaviour you think is a bug as it
  is, and list it in your report.
- Keep the comments that explain WHY. They carry knowledge from live runs. Docstrings can be
  shortened, but keep each ⚠ point.
- Catch errors only where the Python does. `except Exception` becomes a bare `catch`, but a
  `BetRejected` re-raise stays (`if (e instanceof BetRejected) throw e;`). `except Unreached`
  becomes `if (e instanceof Unreached)`.
- Python `facts.update({...})` / dict literals become object literals with the same keys.

## Python behaviour carried over unchanged, flagged for review

These are ported exactly as the Python does them, so the two runners agree. Each one should be
fixed in BOTH runners or neither, on purpose. Python line numbers refer to scripts/scenario_engine.py
at e757494.

| # | Where | What |
|---|---|---|
| 1 | `purchase_bonus` 6316-6323 | A refused purchase reply (`BetRejected`) is caught by `except Exception` and reported as Unreached, "the purchase never fired", instead of an error naming the server code. |
| 2 | `HoldSpinComplete` 6108-6113 | **FIXED 2026-11-11 (both runners):** on failure the run returns to base, then switches the feature off, then re-raises. `restore()` is not in a `finally`. If the drain raises, the Ante / Premium feature the run switched on stays on for the next run. |
| 3 | `sample_purchase_reveal` 6390 | `GAME._norm` exists only on line/ways games. On Zeus/Dragon with `sample_reveal: true` it raises AttributeError, and the run errors. |
| 4 | `PremiumReelSpin` 6642 | **FIXED 2026-11-11 (both runners):** ids compared as text. `reel_ids` is matched with `==`, so string ids in a suite never match int ids from the bridge, and the run reports "no package". |
| 5 | `RandomMultiplier` 7284-7309 | **FIXED 2026-11-11 (both runners):** `base_spins` is a minimum; base spins are topped up after an early bonus (fact `base_spins_after_bonus`). The `base_spins` param is stored but never read. |
| 6 | `RandomMultiplier` 7323 | **FIXED 2026-11-11 (both runners):** `fs_spins` = every free spin played; `fs_multiplier_count` = the ones with a multiplier (CR_058 asserts it). `fs_spins` counts free spins that reported a multiplier, not free spins played. |
| 7 | `PaytableBetScaling._pick_bet` 7371-7379 | **FIXED 2026-11-11 (both runners):** the bet is read back after each pick (unreached if it did not land), new fact `bet_ratio`, and image asserts gained `compare_shots: {field, ratio, tol}`; the 5 paytable suites no longer hand the AI the bet values. Returns True once the list opens; the row click and close results are ignored. `bet_a` / `bet_b` come from the list either way, which is a possible false green. |
| 8 | `PaytableBetScaling` 7421 | **FIXED 2026-11-11 (both runners):** `divisor` uses `LINE_DIVISOR or PAYLINES`, as `line_bet()` does. Divisor is `PAYLINES or LINE_DIVISOR`, but `line_bet()` uses `LINE_DIVISOR or PAYLINES`. Same answer for every current profile. |
| 9 | `P1WaysWin` 7137-7156 | Keeps auditing the chain after the win is found, so an unreadable grid later in the chain turns a found win into unreached. |
| 10 | `RainingWild` 7232-7255 | Counts Wilds on the base spin only, not on the drained bonus. Possibly intended. |
| 11 | `CatDogDrama` 8237-8251 | **FIXED 2026-11-11 (both runners):** autoplay is stopped once enough dramas are seen; a count the game does not offer is Unreached. Autoplay mode: once enough dramas are seen the loop breaks but never stops autoplay, so `confirm_stopped` watches a run still going. If `autoplay` is not in `AUTO_COUNTS` it falls back to the first count but still waits for `autoplay` rounds, so it can time out as an error rather than Unreached. |
| 12 | `WalkingWild` 7972-7973 | `per_round` is never reset between bonuses, so the first free spin of the second and later bonuses is graded, against the comment "only from the second free spin on". |
| 13 | `FreeSpinReload` / `WalkingWild` / `CatDogDrama` | Hunting base spins and Rule-1 drained bonuses are left out of `rounds`, so a balance check across rounds sees gaps. Possibly intended. |
| 14 | `CatDogDrama` 8213 | **FIXED 2026-11-11 (both runners):** a null `reel` is treated like a missing one. `int(winner.get("reel", -9))` raises when `reel` is present but null. |
| 15 | `AutoChangeBetFirst` 3692-3698 | A `count` not in {10, 20, 30, 50} selects index 0 (10 spins) but still waits for `count` spins, so count=15 times out on the 11th. |
| 16 | `SpinInputs` 3981-4012 | Free-spin rounds caught during the hold, and the ones drained after, are left out of `rounds`; a round-based check sees gaps. |
| 17 | `ReconnectBrief` 4101, 4117 | The first spin does not drain a trigger; after the reconnect only the trigger goes into `rounds`, not the bonus it played. |
| 18 | `ReconnectDuringAuto` 4180-4197 | Uses `observe_bet`, not `_auto_next`, so a bonus triggered during auto is never drained. |
| 19 | `AutoUntilEmpty` 3796, `FreespinZeroBalance` 4349 | `balance is None` can never be true (`Bet.balance` is `float(x or 0)`), so a reply without a balance reads as "broke". |
| 20 | `FreespinZeroBalance` 4327, 4388 | With `reload_mid_bonus: false` it drains with `play_out_freespins` on any mechanism (Hold & Spin needs `play_out_bonus`), and rounds resumed by the hunt's reload are dropped. |
| 21 | `OpenTutorial` 3881 | `scroll_back` writes "TUTORIAL TOP AGAIN" even when nothing scrolled (no WebView). |
| 22 | `InsufficientBalance` | The description says it restores the wallet/bet; it does not, a teardown does. |
| 23 | `AutoStopAtWin` | `spins_to_win = len(rounds)` also counts the spins of discarded attempts. |
| 24 | `AutoPlay` 2974 | **False green.** It reads `_BUTTON_WATCH_READ_JS` but never starts `_BUTTON_WATCH_JS`, so `buttons_polled_during_auto` is always 0 and `buttons_enabled_during_auto` always `[]`. |
| 25 | `FreespinThenHistory` 3286 | `reached_last_page` is hard-coded True whenever `page_back` is on (the default). |
| 26 | `FreespinThenHistory` 3241 | Detects the detail with `wait_for_popup(GAME.popup("history_detail"))`, which is False at once where that popup is None, so the case is always unreached there. `SpinThenHistory` uses `wait_for_history_detail`. |
| 27 | `ChangeBet` 2690 | Closes the list with a raw `bet_close` click and never checks that it closed (the hang `close_bet_list` exists to prevent). `_BET_CLOSE_ASK` is unused. |
| 28 | `BaseClusterWin` / `FreeSpinComplete` / `FreeSpinCascade` / `FreespinThenHistory` | Drain with `play_out_freespins`, not `play_out_bonus`, so a full-grid Hold & Spin trigger could wait out the timeout. |
| 29 | `LanguageTour` 4843 | `surfaces` is checked against the MYSTIC profile's surfaces, so Dragon VIP's "feature panel" and Joker's "buy bonus panel" are refused. |
| 30 | `BonusResumeDenominationSwap` 5558 | When the new ladder never arrives it calls `play_out_freespins` without checking `pending`, on every mechanism. |
| 31 | `LastBetBeforeRounds` 5033-5039 | Drains with an uncapped `while True` and no Extension-offer answer before `settle()`. |
| 32 | `swap_applied` fact | Means `bool(swap)` in LastBetRestore (5427) but `bool(swap.get("denomination_id"))` in BonusResumeDenominationSwap (5633). |
| 33 | `StackedPopups` 4731 | Tests `"turbo" in present`, where `present` holds button names, not roles; it works because every profile names it `turbo`. |
| 34 | `LastBetRestore` 5294 / 5369 | `restored_base_stake` is read from the raw `lastBet` while `lb` is unwrapped by `Bet.parse`; they disagree if `lastBet` arrives in an envelope. |
| 35 | `framework/helpers/bo.py` 135 / 204 | `_cfg` only casts `int`, so `reference_bet` stays a string: `reference_bet: 0` is truthy and silently caps the payout at 0.0. |
| 36 | `framework/bo/denominations.py` 590-615 | If the currency is not on the master game, `find_game_currency` raises AFTER the agent row was switched to the automation ladder: the row stays overridden and no teardown info is returned. |
| 37 | `framework/bo/update_game_paytable.py` 104 | `int(rtp)` raises on a decimal RTP like "96.5", so every paytable update for such a game aborts. |
| 38 | `framework/helpers/bo.py` 244 | A token cache that is valid JSON but not an object (e.g. `[1]`) crashes the BoHelper constructor. |
| 39 | `framework/bo/agent_game_settings.py` 222 | `_is_nan` means "not convertible"; `maxPayoutCustom: "nan"` is accepted and becomes NaN. |

## Checking the two runners against each other

```
URL="file:///D:/Project/qa-platform/scenario-runner/test/fake-game/index.html"
python scripts/run_scenario.py --game fake --env fake --url "$URL" --suite scenario-runner/test/fake-game/suite.yml --headless --no-ai
cd scenario-runner && npx tsx src/runner/cli.ts --game fake --env fake --url "$URL" --suite ../scenario-runner/test/fake-game/suite.yml --headless --no-ai
npx tsx test/fake-game/compare.ts ../test-results/fake/fake/<python-run> ../test-results/fake/fake/<ts-run>
```

`--game fake --env fake` has no game config and no `.env.fake`, so BO and the wallet are never
reached. The profile falls back to Mystic's. The fake game (`test/fake-game/index.html`) is a
scripted bridge with a repeating script of outcomes, and it keeps `conservation` true.
| 40 | `scripts/ai_video.py` 107 / 175 | **A string answer `"false"` counts as true**, for `passed` and against a boolean expected. |
| 41 | `scripts/ai_video.py` 164 | The model's confidence is not stripped, so `" high"` grades orange. |
| 42 | `framework/helpers/claude_ai.py` 424 | **Windows:** the Claude CLI call uses cp1252, so "—" arrives corrupted and any Thai/Vietnamese/"→" in a prompt raises, failing that image assert every time. (The TS port sends UTF-8.) |
| 43 | `framework/helpers/gemini_video.py` 355 | `_upload_and_wait` loops forever if Gemini reports the file FAILED. |
| 44 | `scripts/ai_video.py` 251 | A timeline step with a null/string `start_ms` raises and aborts the whole AI pass. |
| 45 | `framework/helpers/claude_ai.py` 498 | `splitlines()` splits on U+2028 inside JSON, which can drop or swap a result event. |
| 46 | `scripts/ai_video.py` 159 | A successful call with empty data gets the note "AI error: None". |
| 47 | `scripts/ai_video.py` 120 | `equals` is case-sensitive while schema grading is case-insensitive. |
| 48 | `framework/helpers/symbol_reference.py` 227 | `asset_groups: "symbols"` as a string is iterated per character, giving an empty reference pack. |
| 49 | `scripts/ai_video.py` 330 | `isdigit()` accepts "²", which `int()` then rejects. |
| 50 | `framework/helpers/local_ai.py` 295 / 378 | Sends `video/mp4` for webm clips and `image/png` for every image. |
| 51 | `framework/helpers/claude_ai.py` 714 / 855 | The cost summary names the model "claude" while `.model` is "sonnet". |
| 52 | 16 fps montage preamble | Rounds 0.0625 to "0.062" next to stamps that read "+0.188s". |

Known porting differences in the AI pass:
- Images are sent as their original bytes with the type taken from the file header. The Python
  re-encoded every image with PIL, keeping JPEG and making everything else PNG.
- The Claude CLI is started through `cmd.exe` on Windows.
- The local endpoint's 300 s timeout covers the whole request, not each socket read.
