# Scenario authoring rules

Rules for writing a **scenario** (the imperative code in `scripts/scenario_engine.py` that drives
the game). This page is the **index**: each rule below has a one-paragraph summary, the situations
that call for it, and a link to its own file with the full reasoning, real failures and code.
**Read the summaries, then open only the rules your change touches.**

For writing test **cases** — the YAML `scenario:` + `assert:` layer — see
`docs/scenario-assert-authoring.md` instead. For what the bridge must provide, see
`docs/game-bridge-contract.md`.

---

## The toolkit — reuse these, do not re-implement them

**The full list is `docs/scenario-toolkit.md`** — every helper a scenario may call, grouped by job,
with signatures, the facts each returns, a scenario template, and the ones never to call. It is
checked against the engine by `tests/chaos/build_scenario_index.py`, so it cannot fall behind.

The ones you will reach for most:

| Job | Helper |
|---|---|
| one spin | `spin_and_finish(ctx)` — handles a trigger for you (Rule 1) |
| drain a bonus | `play_out_freespins` / `play_out_holdspin` — end on the game's terminal, raise at the cap |
| "it came back to base" | `settle_to_base(ctx)` — proves it, then holds the idle screen |
| "it stopped" | `confirm_stopped(ctx)` — watches, then holds (Rule 4) |
| controls | `require_buttons(...)` up front, then `ctx.click_button(name)` |
| a precondition failed | `raise Unreached(reason)` — one handler reports it |

---

## The rules

| # | Rule | Summary | Read it when |
|---|---|---|---|
| 1 | [every BET response can be a free-spin trigger](scenario-rules/rule-01-freespin-trigger.md) | Any base spin can trigger free spins, which the client then auto-plays. Check every response with `is_freespin_trigger` inside every spinning loop, and play the bonus out (`play_out_freespins` / `spin_and_finish`) — ignoring it hangs the run. Includes two static self-checks. | writing any loop that spins; a run hung on `waitForSpinClickable`; adding a scenario (run the self-checks) |
| 2 | [a bonus ends on `is_terminal`, never on the counter alone](scenario-rules/rule-02-is-terminal.md) | A bonus ends when `is_terminal(b)` says so — never on `freeSpins == 0` or `gameState == base` alone, because a max-win truncation leaves spins on the counter. | writing a free-spin or Hold & Spin drain loop |
| 3 | [never call `awaitFreespinRound` speculatively](scenario-rules/rule-03-no-speculative-await.md) | Only await `awaitFreespinRound` when a response said a bonus is open. Otherwise it burns the timeout and raises; use `ensure_base(ctx)` when the state is unknown. | a scenario or teardown waits on free-spin rounds; getting back to base from an unknown state |
| 4 | [proving something STOPPED needs time on the clock](scenario-rules/rule-04-prove-stopped.md) | "It stopped" is a negative claim: watch, then hold, with `confirm_stopped(ctx)`, and expose its facts so the stream and the AI can judge the tail. | any "…and then it stops / no longer happens" claim — auto-play ending, a popup staying closed, a control staying disabled |
| 5 | [a scenario is finished only when the game is spinnable again](scenario-rules/rule-05-finish-spinnable.md) | Return only when the game is spinnable again — no bonus, celebration or popup left on screen for the next run to inherit. | finishing any scenario; a run fails in PREPARE/CLEANUP or because of the previous run |
| 6 | [force the SMALLEST trigger that reaches the feature, not the biggest](scenario-rules/rule-06-smallest-trigger.md) | Force the smallest trigger that reaches the feature. Use a ~30% probability pass, not a 100% force (weights are per cell, ways multiply), `--zero-elsewhere` to pin the minimum award, and force only the section you need. Two-sided rules need a probability in the middle. | deriving or choosing a forced paytable; a bonus runs far too long; a threshold rule needs both sides |
| 7 | [a test that moves the money puts it back exactly](scenario-rules/rule-07-restore-money.md) | A run that moves the wallet declares `teardown: restore-balance`, which restores the exact pre-run balance read from the wallet API. Never `deposit-10000`. | a scenario calls `set_balance` / `deposit`, or changes anything the next run inherits (bet level, paytable, language) |
| 8 | [a bonus plays ITSELF; tear the client down before acting on it](scenario-rules/rule-08-stop-client-mid-bonus.md) | The client auto-plays a bonus, so out-of-band work races it. Call `ctx.stop_client()` first, then `reload_and_start(url=ctx.url)` — not a bare reload. | doing anything mid-bonus: a wallet call, a BO change, a second session |
| 9 | [the max-win cap can END a bonus at any moment, including its first round](scenario-rules/rule-09-max-win-cap.md) | `hasReachedMaxWin` can end a bonus on the trigger, on free spin 1, or any time later. Discard the attempt and hunt again, and assert a fact proving the behaviour actually happened. Also: `max_win_x` multiplies the live BASE bet. | writing a free-spin scenario under a forced paytable with a cap; setting `max_win_x`; a bonus ends earlier than expected |
| 10 | [the bridge tells you what the game THINKS; only the AI tells you what the player SEES](scenario-rules/rule-10-bridge-vs-screen.md) | The bridge reports what the game thinks; only the AI sees what the player sees. Every case carries a `video:`/`image:` assert alongside its `stream:` asserts, unless there is genuinely nothing to see. | writing any case's asserts; tempted to drop a `video:` because a `stream:` fact already proves it |
| 11 | [the wallet takes FRACTIONAL amounts; never round a balance to whole units](scenario-rules/rule-11-fractional-wallet.md) | The wallet settles fractional amounts to the cent. Fund the exact figure you want; never round a balance to whole units. | computing a `set_balance` / funding target |
| 12 | [you cannot drain the wallet during a bonus; arrive broke instead](scenario-rules/rule-12-arrive-broke.md) | The server refuses to drain the wallet during or right after a bonus. For a broke player, fund exactly one bet and let the spin spend it; judge "broke" at the trigger. | a test needs a player with no money, especially around a bonus |
| 13 | [never end a drain on a COUNT; ends are read from the game](scenario-rules/rule-13-no-count-ends.md) | No loop ends on a count. A free-spin drain fails loudly at its cap (retriggers push the counter up); an out-of-money drain stops when the server balance falls below one bet. Auto tests prefer the INFINITE auto count. | writing a drain or out-of-money loop; choosing an auto-play count |
| 14 | [never repair the game before the evidence is captured](scenario-rules/rule-14-no-repair-before-evidence.md) | Never repair the game (e.g. click auto:stop) inside the scenario — the clip then shows OUR fix and the AI grades it. Leave the broken state, label the tail, and let cleanup repair. | a scenario is tempted to tidy up a misbehaving game before returning |
| 15 | [a fact the client cannot clear is not a defect; assert what the player can see](scenario-rules/rule-15-player-visible-facts.md) | An internal flag the client cannot clear is not a defect. Assert what the player can see or do, not the flag. | asserting on an internal client flag (e.g. `isAutoPlay`) that disagrees with what is on screen |
| 16 | [one clip, ONE prompt: share a video call across the cases that grade it](scenario-rules/rule-16-share-video-call.md) | Cases in one run that grade the same clip share ONE run-level `video:` block with `expected_by_case`. The prompt describes the whole clip in order, with distinct field names. | several cases in one run grade the same footage; reducing AI calls |
| 17 | [a run that NEEDS a free-spin trigger FORCES one; it never hunts](scenario-rules/rule-17-force-dont-hunt.md) | A run that needs a bonus forces one with a paytable and carries no hunt budget. Pair it with a cap — default `max_win_x: 20`, looser when the feature's size or length is the subject — and assert the length the case needs. | a case needs a free-spin / Hold & Spin / bought bonus; choosing `max_win_x`; a run comes back `inconclusive` from hunting |
| 18 | [screenfuls of ONE surface go to the AI together (`combine: true`)](scenario-rules/rule-18-combine-screenfuls.md) | Screenfuls of one scrolling surface (help, tutorial, long history) go to the AI together with `image: combine: true`; different surfaces or languages stay one call per still. | writing an `image:` assert over several stills |
| 19 | [a field that says NOTHING is not a value; never do arithmetic on a sentinel](scenario-rules/rule-19-sentinel-fields.md) | A field the server uses to say nothing (`-1`, `0`, `null`) is not a value. Carry the last reported figure instead of doing arithmetic on it, expose raw values as facts, and gate cases on whether the field was answerable. | asserting on a response field that may be unreported on some rounds (e.g. `balance: -1` on respins) |
| 20 | [a BO write is not live when the API answers; wait for it, then PROVE it arrived](scenario-rules/rule-20-bo-write-propagation.md) | A BO `200 OK` means BO stored the change, not that the game has it (Kafka lag). Write with the game closed, wait, reopen, then prove the client loaded the new config with a gating fact. | a scenario writes BO (denomination, paytable, flags) and reopens the game |
| 21 | [a round's clip ends when the NEXT round starts, which is before its presentation plays](scenario-rules/rule-21-round-clip-window.md) | A round's clip ends when the next round starts — for an auto-played round that is before its presentation plays. For a claim about a banner, teaser or celebration, span one round further: `at: [trigger, "rounds[1]"]`. | a `video:` claim is about something drawn after a response (award banner, Buy Bonus teaser, big-win celebration) |

---

## Quick checklist for a new scenario

- [ ] Every `base_spin()` / `observe_bet()` result is tested with `is_freespin_trigger` — **or** the
      scenario documents why a trigger is impossible here (e.g. it places no bet).
- [ ] That test is inside every loop that spins, not only after the loop.
- [ ] Bonus drains terminate on `is_terminal`, capped, never on `freeSpins == 0` alone.
- [ ] `awaitFreespinRound` is only reached when a response said a bonus is open.
- [ ] The scenario returns with the game spinnable.
- [ ] Any "…and then it stops" claim goes through `confirm_stopped()` — watched, then held.
- [ ] Facts that count rounds count the right ones — `base_rounds_since()` where the claim is "one
      input caused one spin", so a drained bonus does not inflate the number.
- [ ] A forced paytable reaches the feature with the SMALLEST outcome that works (Rule 6): a
      ~30% probability pass, NOT a 100% force, wherever the outcome scales with the symbol COUNT —
      weights are per cell, so a forced reel fills every row and multiplies a ways result. Accept
      the short hunt, and force only the section you need so the bonus still ends.
- [ ] A scenario that moves the wallet declares `teardown: restore-balance` (Rule 7) —
      never `deposit-10000`, which leaves the balance drifting.
- [ ] Anything done mid-bonus calls `ctx.stop_client()` first (Rule 8) — the feature
      auto-plays, so an API call races it — and comes back with
      `reload_and_start(url=ctx.url)`, not a bare reload.
- [ ] A free-spin scenario handles `has_max_win` on the trigger AND on the first free
      spin (Rule 9) by discarding the attempt and hunting again, not by failing.
- [ ] The case asserts a fact proving the behaviour HAPPENED — a bonus that ended early
      passes `terminal_reached` and `{terminal.free_spins} == 0` on its own.
- [ ] EVERY case has a `video:` (or `image:`) assert (Rule 10) — the bridge reports what the
      game thinks, not what is on screen. No AI assert means the reason is written down and
      is "there is nothing to see".
- [ ] ...and it does not REPLACE the `stream:` asserts: exact values and "it did not happen"
      stay on the stream, the clip proves the screen agrees.
- [ ] A wallet target is the EXACT figure wanted (Rule 11) — never rounded to whole units.
- [ ] A test needing a broke player FUNDS ONE BET AND SPENDS IT (Rule 12); it does not try to
      drain the wallet during a bonus, which the server refuses.
- [ ] A run whose forced bonus can EXTEND ITSELF (retrigger in free spins, a counter-resetting
      symbol, a bought bonus) carries a cap, defaulting to `max_win_x: 20` (Rule 17) — and a
      LOOSER one where the feature's own size or length is the subject.
- [ ] A run at a tight `max_win_x` asserts the bonus LENGTH it needs (Rule 9/17) — a cap can end
      the feature on the trigger or on free spin 1, which passes the obvious asserts showing nothing.
- [ ] A run that NEEDS a bonus FORCES one with a paytable (Rule 17) and carries no hunt budget —
      hunting costs real spins and ends in `inconclusive`, which reads like coverage and is not.
- [ ] No loop ends on a COUNT (Rule 13): a free-spin drain fails loudly at its cap, and an
      out-of-money drain stops on the server balance falling below one bet.
- [ ] An auto test whose subject is "auto stops because of X" uses the INFINITE auto count
      (Rule 13) — a finite one can run out on its own and pass for the wrong reason.
- [ ] Nothing repairs the game before the asserts are graded (Rule 14) — a tidy-up click inside
      the scenario ends up in the clip and the AI grades OUR fix, not the game's behaviour.
- [ ] An internal flag is asserted only where the player could act on it (Rule 15).
- [ ] No assert does arithmetic on a field the server uses to say NOTHING (Rule 19) — a `-1`
      balance on a bonus round means "not reported", and reading it literally reds a feature
      whose money is exactly right. Expose the raw value as a fact and gate the case on
      whether the field was answerable.
- [ ] A `video:` claim about a PRESENTATION (an award banner, a teaser, a celebration) spans
      one round further than the round it belongs to (Rule 21) — `at: trigger` ends at the
      response, before the animation plays, and the AI correctly answers "no".
- [ ] Cases in one run that grade the SAME clip share ONE run-level `video:` block with
      `expected_by_case` (Rule 16) — not one `video:` assert each, which re-analyses the same
      footage per case. Merged prompts describe the whole clip and use distinct field names.
- [ ] An `image:` assert over screenfuls of ONE scrolling surface sets `combine: true` (Rule 18) —
      per-still grading cannot answer "is this section anywhere in the document?", and the
      conditional prompt it forces can never catch a section that is missing entirely.
- [ ] A scenario that writes BO mid-run WAITS before reopening and then asserts the game
      actually loaded the change (Rule 20) — BO answers before the game server has the
      event, and a stale read greens the very claim the case is making.
- [ ] The two self-checks in [Rule 1](scenario-rules/rule-01-freespin-trigger.md) print nothing.
