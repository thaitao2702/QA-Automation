# Rule 4 — proving something STOPPED needs time on the clock

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

> **A scenario that ends the moment the last round lands has not shown that anything stopped.**
> Whenever the claim is "…and then it stops" — auto-play finishing its count, the user halting it, a
> reload cancelling it, the balance running out — WATCH for a while, then HOLD. Use
> `confirm_stopped(ctx)`.

"It stopped" is a **negative** claim, and a negative is only worth anything if you waited. Two
separate things go wrong without the wait:

**Nothing is proved.** Having collected the N rounds you asked for says *your loop* ended, not that
the client did. A client that overruns its count keeps betting and no assert notices. `settle()`
does not save you: `waitForSpinClickable` is not supposed to resolve during autoplay, but its
fallback swallows the case where it does anyway.

**The AI has nothing to look at.** The clip ends on the last payout, so "did auto stop?" is being
asked of a video that merely *stopped*. Answering no to that is correct.

```python
stop = await confirm_stopped(ctx, watch_ms=6000, dwell_ms=3000, label="AUTO STOPPED")
# facts: stopped · extra_round_after_stop · auto_flag_after · auto_spins_left_after · stop_watch_s
return ScenarioResult(..., facts={..., **stop})
```

It watches `awaitBetResponse` for `watch_ms` — **nothing arriving is the evidence** — reads the
client's own auto flag, marks the moment, and dwells so the still screen is recorded. A round that
does arrive is recorded into the stream rather than discarded, so the proof of the defect is in
`events.jsonl`.

In the case, let the exact facts carry the claim and tell the AI to judge the tail:

```yaml
- stream: "{facts.extra_round_after_stop} == False"
- stream: "{facts.auto_flag_after} == False"
- video:
    prompt: >-
      ... The clip deliberately continues for about {facts.stop_watch_s} seconds AFTER the last
      spin and then holds still — during that tail the reels must not start again. A clip that
      simply ends is not evidence; judge the tail.
```

**This is not only about auto-play.** The same shape applies to any "X no longer happens" claim: a
bonus that must not retrigger, a popup that must stay closed, a control that must stay disabled.
If the assertion is that nothing happens, the scenario has to spend time not-happening.
