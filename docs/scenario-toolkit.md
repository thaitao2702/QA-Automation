# Scenario toolkit — what to use when writing a scenario

Every helper in `scripts/scenario_engine.py` a scenario may call, grouped by the job it does. **Read
this before writing a scenario, and reuse before you re-implement** — each helper encodes a rule
that is easy to get wrong and hard to notice when you do (`docs/scenario-authoring-rules.md`).

`python tests/chaos/build_scenario_index.py` fails if a public helper exists in the engine but is
missing here, so this list is complete. Invariants (`stream:` predicates) are listed in
`docs/scenario-index.md` §5, not here.

---

## 0. The shape of a scenario

```python
class MyScenario(Scenario):
    name = "my-scenario"
    description = "What situation it reaches and which facts it exposes."

    async def run(self, ctx: DriveContext) -> ScenarioResult:
        start = ctx.now_s()
        facts: dict = {}                     # created FIRST, filled as you go
        try:
            if not GAME.HAS_SOMETHING:
                raise Unreached("this build has no Something")
            btn_open, btn_ok = require_buttons("open_role", "ok_role")   # every control, up front

            ctx.mark("STEP: open the panel")                              # every step a prompt names
            if not await ctx.click_button(btn_open, optional=True):
                raise Unreached(f"`{btn_open}` never became clickable")

            spin, rounds = await spin_and_finish(ctx)                    # any spin may trigger a bonus
            facts.update({"payout": spin.payout})                         # dict literal, not facts["x"]=

            stop = await settle_to_base(ctx, label="my-scenario")         # prove + film the return
            facts.update({**stop})
            return ScenarioResult(self.name, True, spin, rounds, ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), round_windows=ctx.windows(),
                                  marks={"trigger": rounds[0].seq, "terminal": rounds[-1].seq},
                                  facts=facts)
        except Unreached as e:
            await ctx.dismiss_popups()
            return ScenarioResult(self.name, False, None, [], ctx.timeline, ctx.events,
                                  (start, ctx.now_s()), facts=facts, reason=str(e))
```

Then add it to `REGISTRY`, run the two self-checks in `docs/scenario-authoring-rules.md`, and add a
row to `docs/scenario-index.md`.

---

## 1. Driving rounds

| Helper | Use it when |
|---|---|
| `spin_and_finish(ctx) -> (spin, rounds)` | **The default for one spin.** Clicks, and if the spin triggered a bonus plays it out; otherwise settles. `rounds` is `[spin]` or `[trigger, …, terminal]`. |
| `ctx.base_spin() -> Bet` | One spin you click, when you need the round before deciding what to do. **You** must then handle a trigger (Rule 1). |
| `finish_round(ctx, b) -> rounds` | You already have the round (the client fired it, or a gesture sits between response and settle). Same trigger handling as `spin_and_finish`. |
| `ctx.observe_bet(timeout_ms=None, phase="auto") -> Bet` | A round the **client** fires — auto-play, a key press, a purchase. No click. Defaults to `auto_timeout_ms` (90 s); pass `bet_timeout_ms` for a click-caused round trip such as a purchase. Tag `phase="base"` for a paid base round. |
| `ctx.free_spin(phase="freespin") -> Bet` | One auto-played bonus round. Only when a response **said** a bonus is open (Rule 3). |
| `play_out_bonus(ctx, trigger, cap=None, settle=True, on_round=None) -> rounds` | **The Rule 1 drain: a trigger you did not ask for.** Picks `play_out_holdspin` or `play_out_freespins` by `GAME.mechanism`, so a mechanism-free scenario stays correct on a Hold & Spin game — calling `play_out_freespins` directly there misses a FULL GRID, which ends the feature with respins still on the counter, and the run then settles against a spin button that never becomes ready. Returns `[trigger, …rounds]`; `cap` defaults per mechanism (60 respins / 500 free spins); `settle` behaves the same either way. It drops `capped` — a scenario that GRADES the feature calls `play_out_holdspin` itself. |
| `play_out_freespins(ctx, trigger, cap=500, settle=True, on_round=None) -> rounds` | Drain a free-spin bonus to its terminal. Ends on `is_terminal`; **raises** at `cap` instead of returning a half bonus (Rule 13). `settle=False` when auto-play resumes on its own. `on_round(bet)` is awaited as each round arrives, for evidence that only exists DURING the bonus. **Not for Hold & Spin**: it misses a full grid. |
| `play_out_holdspin(ctx, trigger, cap=60, mark=True, on_round=None) -> (rounds, capped)` | Drain a Hold & Spin to its end — **the Hold & Spin counterpart of `play_out_freespins`; pick with `GAME.mechanism == "hold-spin"`**. Ends on `is_terminal` **or a full grid** (which ends the feature with respins still owed); returns at once, awaiting nothing, if the trigger already filled the grid or hit max win. `rounds` is `[trigger, …respins]`; `capped` is True when max win cut the feature short (reported, never asserted away). **Raises** at `cap` rather than returning a half feature. `mark=True` puts `RESPIN n: coins held, respins left` on the ACTION TIMELINE. `on_round(bet)` is awaited as each respin arrives, for evidence that only exists DURING the feature (e.g. the bet readout). ⚠ **Does not settle** — follow it with `ctx.settle()` or `settle_to_base()`. |
| `ctx.settle()` | Let the last round's presentation finish so the clip covers it. Falls back silently after 60 s — **use `settle_to_base` when "it came back" is part of the claim.** |

## 2. Reading a round — never hand-write these

| Helper | Answers |
|---|---|
| `is_freespin_trigger(b) -> bool` | Did this round award a bonus? Read through the profile, so nested or differently spelled counters work. |
| `enters_holdspin(b) -> bool` | Does a Hold & Spin **start** here — a trigger, **or** 9 coins on a base spin (an instant win `is_trigger` deliberately answers False to). Use in hold-spin hunting loops. |
| `is_terminal(b) -> bool` | Did the bonus end? `freeSpins == 0` **or** base state (both halves — Rule 2). `None` counts as terminal. |
| `win_steps(b) -> int` | Paying steps: cascade steps that paid on a cascade game, else 1 for any win. The unit a history ticket pages by. |
| `cluster_wins(b)` / `win_clusters(b)` | Cascade-only: steps that paid / clusters across them. |
| `base_rounds_since(ctx, before) -> int` | Paid **base** rounds since event index `before`. For "one input caused one spin" — a drained bonus must not inflate the count. |

## 3. Ending a feature, and proving a negative

Both **watch, then hold** (Rule 4): a clip that ends the instant the last round lands proves nothing.

| Helper | Use it when | Facts it returns (spread with `**`) |
|---|---|---|
| `settle_to_base(ctx, label="", dwell_ms=2000, timeout_ms=None, watch_ms=5000, hold_ms=3000, slice_ms=5000)` | A feature finished and the claim includes **"…and the game came back to base."** Films the celebration first, clears it if it does not close itself, waits (default `spin_timeout_ms`) for a spinnable base, watches for a stray bet, then holds — **on the failure path too**. | `returned_to_base` `celebration_shown` `celebration_auto_closed` `popups_at_end` `base_ready_seconds` `bet_after_return` `base_watch_s` `base_hold_s` |
| `confirm_stopped(ctx, watch_ms=6000, dwell_ms=3000, label="STOPPED", flag_grace_ms=5000)` | **Auto-play** stopped (count reached, user stop, win, out of money, reload). Watches for a further bet, polls the client's auto flag, marks, holds. | `stopped` `extra_round_after_stop` `auto_flag_after` `auto_flag_cleared_s` `auto_spins_left_after` `stop_watch_s` |

| `answer_extension_offer(page, expect=True)` · `decline_extension_offer(ctx, terminal=None)` | A game offers to SELL more free spins when a bonus ends (Grandma Pets' Free Spin Extension). ⚠ The offer holds the spin button until it is answered. Both shared drains call it already: `play_out_freespins` (the ctx wrapper, which marks the timeline) and `drain_to_base` (the page-level one, which covers the BOOT resume and the `return-to-base` teardown). ⚠ The page-level form exists because the offer can be raised where no scenario is driving: a resumed bonus plays out inside `start_game`, which calls `assert_idle` itself — a scenario answering it afterwards is already too late. Four things it handles that a bare click does not: the panel may legitimately not be there (max win, `canBuyExtension: false`, or autoplay declined for us); the panel HOLDS for up to 5 s after the click, so it waits for it to actually go; the decline produces a reply on its own stream, which it collects so a later `awaitExtensionResponse` is not handed a stale one; and the result popup behind it is a presentation surface `dismissPopups()` never force-closes, so it is skipped explicitly. | returns `True` when an offer was answered by us; a no-op on every profile with no `extension_decline` button |
| `skip_big_win(ctx, bet)` · `big_win_threshold(ctx)` | **Time, not a claim.** A BIG/MEGA/ULTRA win holds the screen for seconds of count-up, and inside a bonus it can happen round after round — minutes per run for a presentation nothing here grades. ⚠ It asks the RESPONSE first (`payout / baseStake >= threshold`, the threshold read from `gameInfo.settings.winFormula`, never guessed), so a round that cannot be a big win never waits; polling every round would cost more than it saves. Two taps a second apart, which is what the control expects — one cuts the count-up, the second dismisses the banner. ⚠ Called from the ROUND PRIMITIVES — `ctx.base_spin()`, `ctx.free_spin()`, `ctx.observe_bet()` — so every round the framework observes is covered and no scenario has to remember. It sat in `finish_round` / `play_out_freespins` first and missed every scenario that drives with `ctx.base_spin()` directly (`drama`, `walking-wild`'s base spins, `reels-glow`, both hunt loops). Set `ctx.skip_big_wins = False` in a scenario whose subject IS the celebration. **It never fails a run**: no control, or no celebration, is marked on the timeline and left alone. | returns True when one was skipped |
| `purchase_bonus(ctx, package=0)` | Buying the bonus is part of the path, not the subject — `freespin-reload` and `walking-wild` both start from a bought bonus. Opens the panel, picks the package, handles the TWO-CLICK confirm (a build that raises a modal, and one that buys straight from the panel), and returns the purchase round. Raises `Unreached` on every way it can fail. | returns `(bought, facts)` — `bet` `packages` `popup_loaded` `package_picked` `price` `price_ratio` `buy_clicked` `confirmed` |
| `watch_presentation(ctx, grid=False, wilds=False)` · `take_presentation_trace(ctx, stop=False)` | A presentation is made of things true for a few frames — a wild mid-walk, a stand-in about to be knocked off, a control that flickers enabled during a reload. ⚠ Polling from Python cannot see them: every `await` is a gap, and the gaps are where the evidence is. These sample `busyOps` (optionally the displayed grid and walking wilds) every 50 ms **inside the page**; `take_` returns everything since the last take and clears, so consecutive calls slice the recording per round. | a list of `{t, busy[], wilds?, cells?, animating?, grid?}` samples |
| `sample_purchase_reveal(ctx, timeout_ms=8000)` | A Buy Bonus purchase whose response carries **no grid** (Grandma Pets sends five reels of `-1` and draws the "you triggered it" teaser client-side). Polls `getDisplayedGrid()` until every cell is revealed, so what the player was shown is recorded EXACTLY instead of being read off pixels by the AI. Call it between the purchase response and the first bonus round — afterwards there is nothing left to see. | `reveal_sampled` `reveal_stable` `reveal_distinct_grids` `reveal_bonus_reels` `reveal_max_bonus_per_reel` `reveal_wilds` `reveal_winning_run` `reveal_grid` — ⚠ gate a case on `reveal_stable`: a teaser still animating reports whatever was on screen at that instant, which reads exactly like a game drawing the wrong symbols |

Quote `base_watch_s` / `stop_watch_s` in the prompt so the model judges the tail, not the cut.

⚠ `sample_purchase_reveal` returns a dict; spread its keys into `facts` **as a dict literal**, not
`facts.update(rev)` — `validate_suites.py` reads exposed facts out of literals, and merged keys are
invisible to it, so every correct `{facts.reveal_*}` assert is reported as "not exposed".

## 4. Controls

| Helper | Use it when |
|---|---|
| `require_buttons(*roles) -> list` | **Resolve every control you will click, up front.** A role is a string or `(role, index)` for a templated name. Raises `Unreached` naming **every** missing role at once. Past it, clicks need no `if name:` guard. |
| `ctx.click_button(name, optional=False, timeout_ms=None, dwell_ms=None) -> bool` | Wait for clickable, real coordinate click, then dwell. Accepts a role or a build name. `optional=True` returns False instead of raising. |
| `ctx.has_button(name) -> bool` | Is the control in this build at all? Instant probe — for "does this build have feature X". |
| `ctx.hold_button(name, hold_ms=1000, use_bridge=False, …)` | Press-and-hold (hold-to-turbo, hold-to-autospin). `use_bridge` is best-effort and falls back to a real pointer hold — Zeus v3 removed the bridge hold. |
| `ctx.double_click_button(name, …)` | Double-click (sudden stop). |
| `ctx.press_key(key)` / `ctx.hold_key(key, hold_ms)` | Tap / hold a key (Space to spin, hold for turbo). |
| `switch_feature(ctx, feature, on, optional=True, dwell_ms=None) -> dict` | Switch a paid spin feature — `"ante"` or `"premium:<reelId>"` — ON or OFF, whatever the build's flow: a one-click `ante` toggle (Chaos), or Dragon VIP's Buy Bonus panel → ACTIVATE row → `buy_confirm` and a one-click off. **Switches, never blind-toggles**: a feature already in the wanted state is left alone. Returns `{switched, clicked, popups}`; `popups` is sampled for ~2.5 s after the last click, so a feature toast is caught. |
| `auto_stop_button() -> name` | The control that **stops** auto-play in this build's dialect (a main-screen stop, or the `auto` toggle). Never assume. |
| `ctx.scroll_bet_row(i, dwell_ms)` | Bring denomination row `i` into view before clicking it. |

## 5. Popups and panels

| Helper | Use it when |
|---|---|
| `ctx.popups() -> list` | Labels on screen now (`visiblePopups`). ⚠ a dialect: a modal can be a child of another popup and never appear here. |
| `ctx.dismiss_popups() -> list` | Close everything. Returns only what the bridge **verified** closed. Safe when nothing is open. |
| `ctx.wait_for_popup(label, timeout_ms=8000) -> bool` | Wait for a popup label. Returns False at once for `label=None` (the profile says the build has none). |
| `open_bet_list(ctx) -> bool` | Open the denomination list and report whether it **actually** opened. False is a real answer — raise `Unreached`. |
| `close_bet_list(ctx) -> facts` | Close it and **prove** it closed (close control → check → `dismissPopups()` fallback). Facts: `closed` `close_button_worked` `needed_dismiss` `popups_after`. |
| `close_history(ctx, timeout_ms=8000, label="") -> facts` | The same for the History panel, logging every step to console and timeline. Facts: `closed` `close_button_worked` `needed_dismiss` `popups_after` `close_seconds` `spin_ready_seconds`. On a build where History lives inside a menu it also closes that menu (below). |
| `close_opener_if_left(ctx, name) -> bool` | After closing a surface reached **through a panel** (profile `BUTTON_VIA`, e.g. Grandma's History inside Settings), close that panel too if the game went back to it instead of the main screen. Uses `<opener role>_close`, falling back to `dismissPopups()`. `True` when a panel was left open and closed here; a no-op for a main-screen control. Opening is automatic: `ctx.click_button` / `hold_button` / `double_click_button` open the panel first whenever a `BUTTON_VIA` control is not on screen. See `docs/game-bridge-contract.md` §8 "Main screen (legacy UI) or Settings menu (new UI)". |
| `ctx.history_rows() -> list` | History rows rendered **now** (virtualised — a subset). `[]` if closed or no getter. |
| `ctx.wait_for_history_rows(timeout_ms=10000) -> list` | Wait for rows to render after opening. Returns `[]` at once if the bridge has no `getHistoryRows`. |
| `ctx.wait_for_history_detail(timeout_ms=10000, before=None) -> bool` | Did the detail ticket open? Asks `getHistoryDetailState().open` first, then the popup label, then "any new popup since `before`". |
| `ctx.history_detail_state() -> dict` | `{open, betIndex, betCount, hasPrev, hasNext}` — page a ticket on `hasNext`, never on `betCount`. |
| `ctx.scroll_popup(steps, …)` / `ctx.wheel_at(x, y, steps, …)` | Wheel a popup / a point, one full page per step, dwelling so each lands on the clip. |
| `ctx.tutorial_rect()` / `ctx.scroll_tutorial(steps, …)` | The cross-origin help WebView: only a real wheel scrolls it, and its position cannot be read back. |
| `ctx.tour_ui(dwell_ms, shots, prefix, surfaces)` / `ctx.UI_SURFACES` | Walk every text surface (base, bet list, settings, tutorial, autoplay, history), shooting each. |
| `ctx.start_capture(label)` / `ctx.end_capture(label)` | Record bridge events (`popup_shown` …) for something too brief to poll. |
| `ctx.start_popup_watch(interval_s)` / `ctx.stop_popup_watch()` | Sample `visiblePopups` in the background during a burst → `{order, max_concurrent, samples}`. |

## 6. Reading bridge state

| Helper | Use it when |
|---|---|
| `ctx.ui_read(js, default)` | Best-effort read for a UI value (bet, price, a flag). Returns `default` if the call throws. |
| `ctx.eval(expr)` | A config/UI value the scenario needs internally, where a throw should propagate. |

Decisions come from the **BET response**, never a client getter. Use these for UI values and
diagnostics only.

## 7. Recording for the AI

| Helper | Use it when |
|---|---|
| `ctx.mark(label)` | Pin a labelled moment. With `timestamp: true` it becomes an ACTION TIMELINE entry. **Mark every step a prompt refers to.** |
| `ctx.dwell(ms=2000)` | Hold so a change is on screen. 2 s is enough — the montage samples one frame a second. |
| `ctx.screenshot(label)` | A still for an `image:` assert. Return it with `shots=ctx.shots`, or the assert grades nothing. |
| `ctx.windows()` | Per-round clip windows — pass as `round_windows=` so `at:` can bind. |
| `ctx.now_s()` | Video-relative seconds, for `start` and the scenario window. |

`marks` map a name to a **round seq**, not a moment. A round's window runs from its response to the
next round's response, so on a bonus whose rounds arrive milliseconds apart a single-round `at:` is
nearly empty. For a whole feature, omit `at:` or span `[trigger, terminal]`
(`docs/scenario-assert-authoring.md`, *the per-round window trap*).

## 8. Money, network, sessions, language

| Helper | Use it when |
|---|---|
| `ctx.set_balance(amount, attempts, pause_s)` | Set the wallet to an **exact** figure (fractional amounts are fine — Rule 11). Declare `teardown: restore-balance` (Rule 7). |
| `ctx.deposit(amount)` | Add to the wallet. Same teardown rule. |
| `ctx.stop_client()` | Tear the game down so no round can play — **before** any out-of-band action mid-bonus (Rule 8). |
| `ctx.reload_and_start(phase, url)` | Reload and re-enter, **playing out any owed bonus** and waiting for the base game. After `stop_client()` pass `url=ctx.url`; a bare reload re-loads `about:blank`. When the scenario itself needs to watch the resumed bonus, use `open_game` instead. |
| `ctx.open_game(url=None)` | Load the game and click Start — **nothing more**. No bonus play-out, no wait for base. Use it when the scenario watches a resumed bonus itself: read `pending` with `read_game_info`, then take the rounds with `ctx.free_spin()` (only if `pending` says one is owed). `url` defaults to `ctx.url`, which is also what is needed after `stop_client()`. |
| `ctx.set_automation_denomination(settle_s=5.0) -> dict` | **BO only**: put the agent's game row on the automation bet ladder (11, 22, 33). The game is not touched, and a denomination takes effect on the next load, so call it between `stop_client()` and `open_game()`. ⚠ **Rule 20**: BO answering `200` is not the game server having the change — it arrives on a Kafka event — so this waits `settle_s` after the write, and the caller must still read the ladder back from the reopened client and assert it (`client_on_new_ladder`). The unreal values mean one number says which ladder a stake came from. Returns `{denomination_id, name, created, sub_category_id, previous_denomination_id, bet_levels, default_bet_level, default_bet}`, or `{}` when BO is not configured. **Declare `teardown: reset-denomination`** (Rule 7): the ladder is shared state, and leaving 11/22/33 in place breaks every later bet assert in the suite. |
| `await ctx.fresh_session(why) -> bool` | Re-enter the game on a NEW launch token (runner `relaunch`; falls back to reloading `ctx.url`). For a scenario that ends the session **on purpose** — `reconnect-long`, `multi-tab-session` — so the NEXT run does not inherit a dead client ("session expired", auto frozen). Call it **after** building the `ScenarioResult`, so the recorded window ends on the popup that is the case. Logs the new `game_info`. Best-effort: `False` on failure, never raises. |
| `ctx.log_api(kind, data, t=None)` · `await ctx.capture_game_info(when)` | Add a RAW payload to the replay's event inspector (`ctx.api_events`, merged into `replay.html` by the runner — kept out of `ctx.events` because a round's `seq` is `len(events)`). `_record` already logs every round as `bet_response` (base / auto) or `freespin_response` (every other phase), and `load_game` / `reload_and_start` / a language relaunch log `game_info` (the bridge's raw `getGameInfo()`, read before Start where possible). Call these yourself only for a payload no helper records. Best-effort: a failed read never fails the run. |
| `ctx.load_game(url=None)` · `load_until_ladder(ctx, want, timeout_s, poll_s) -> (ready, seen, loads)` · `launch_ladder(info)` | Load the game **without** clicking Start (safe with a bonus owed — `pending` only plays after Start), and read the bet ladder the load will run on from the launch payload (`settings.denominations`, or `settings.general.denominations`). `load_until_ladder` reloads every `poll_s` until that ladder is `want`, for up to `timeout_s`, and leaves the game loaded but NOT started — follow it with `click_start(ctx.page)`. ⚠ **Use it after `set_automation_denomination`, not a longer sleep**: the Kafka delay varies, and two runs with the same 5 s wait split — one reopened on the new ladder, one on the old, and the old one reads exactly like a game ignoring the new denomination. `ready == False` is a propagation problem, not a game defect: report it as unreached. |
| `reset_denomination(ctx)` | The teardown behind `teardown: reset-denomination`. Clears the override and rewrites the id to what the row held before. No-ops when this run never swapped (`_denom_was`), exactly as `restore-balance` does. |
| `ctx.url` | The game URL this run is playing (kept current across a language relaunch). |
| `ctx.set_offline(offline)` | Drop / restore the network (reconnect cases). |
| `ctx.open_second_session()` / `ctx.close_second_session()` | Same account in a second browser session. |
| `ctx.set_language(code)` | Re-enter in another language; returns the new URL. |
| `select_languages(spec, seed, pool, include)` | Turn a suite's `languages:` param (`all`, a list, `random:N`) into codes. |
| `spin_balance_needs_stake(balance, min_bet)` / `plan_insufficient(balance, min_bet)` | Internal to `insufficient-balance`. ⚠ `plan_insufficient` still withdraws `floor(balance)` on a "whole units only" premise that Rule 11 says is false. |

## 9. Failure handling

| Helper | Use it when |
|---|---|
| `Unreached(reason)` | A precondition this scenario cannot get past. Raise it; one `except Unreached` at the end of `run` clears the screen and returns `reached=False` with the message as the reason. **Catch this type only — never bare `Exception`**, or a real bug is filed as "the build does not support this". Keep the message to one line with the value that identifies the failure. |

## 10. Do not call these from a scenario

| Group | Names | Why |
|---|---|---|
| Framework-owned, run boundaries | `start_game` `click_start` `assert_idle` `drain_to_base` `return_to_base` `ensure_base` `stop_autoplay` `restore_balance` `deposit_10000` `missing_api` `read_game_info` `set_profile` `bridge_is_live` | Run before and after every run (`docs/run-lifecycle.md`). A scenario handles its own bonus and leaves the game spinnable; it does not invoke repair. `deposit_10000` is deprecated — use `restore-balance`. **`bridge_is_live(ctx)`** answers whether there is still a page with a bridge on it: a scenario may legitimately end with the client torn down (`bonus-resume-denomination-swap` calls `stop_client()` so a reopen can prove the resume), and if it then raises before reopening, every teardown that drives the bridge fails with `Cannot read properties of undefined` — which `run_teardowns` treats as fatal, losing every later run. `return_to_base` checks it and no-ops. |
| Runner-side | `resolve_window` `resolve_each` `resolve_tokens` `eval_inline_stream` `run_stream_assert` | Turn a suite's `at:` / `each:` / `{facts.*}` / `stream:` into results. |
| Bridge primitives | `wait_spin_clickable` `click_spin` `await_bet_response` `await_freespin_round` | For **helpers** that need a raw call (`confirm_stopped`, `settle_to_base`). A scenario uses the `ctx.*` wrappers, which record the round. |

---

## Traps the helpers already handle — keep them handled

| If you write it by hand | You will get |
|---|---|
| a spin loop without `is_freespin_trigger` / `enters_holdspin` in the body | a hang mid-bonus, or an instant 9-coin win the loop never notices (Rule 1) |
| a drain that ends on `freeSpins == 0` | a max-win truncation that never ends (Rule 2) |
| a drain that falls out of `for _ in range(cap)` | a half-played bonus reported as finished (Rule 13) |
| `ctx.settle()` where the claim is "it came back" | a silent 3 s fallback and a popup left for the next run |
| `observe_bet()` for a clicked purchase | a 90 s wait on a bet that never fired |
| `facts["x"] = …` | a fact the validator cannot see, so its asserts read as "not exposed" |
| `except Exception:` around the whole run | your own bugs reported as "unreached" |
| the facts dict built after the first `raise` / `return` | `UnboundLocalError`, or failures that report nothing |

## Adding a helper

Add it to the engine, not inside one scenario, when it is **game-agnostic** and encodes a rule.
Read the response through `GAME.*`, take `ctx` first, return facts as a dict. If it returns facts a
scenario spreads with `**`, add its contract to `SPREAD_HELPERS` in `tests/chaos/validate_suites.py`,
or every assert on those facts is reported "not exposed". Then add a row here —
`build_scenario_index.py` fails until you do.
