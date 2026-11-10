# Rule 20 — a BO write is not live when the API answers; wait for it, then PROVE it arrived

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

> **`200 OK` from the back office means BO STORED the change, not that the game server has it.**
> BO publishes the change on a **Kafka event** and the game server applies it when the event is
> delivered. Nothing in the HTTP response is a promise about that. A scenario that writes BO and
> reopens the game in the same breath can load the OLD configuration.

This bites on **any out-of-band write the NEXT LOAD has to see** — the agent's denomination, its
paytable, a compliance flag. The denomination is where it was measured (Dragon VIP,
`bonus-resume-denomination-swap`), and it is the same lag already recorded for
`bo_paytable_update`, which needed a further reload before the forced paytable took effect.

## Why this one is worse than a flaky timeout

A late denomination does not look like an error. It looks like a PASS:

| | what the case wants to prove | what a stale read produces |
|---|---|---|
| DV-71 | the resumed bonus keeps the OLD bet | old bet on screen — **greens for the wrong reason** |
| DV-73 | after the bonus, the bet is the NEW ladder's default | old bet on screen — reds a correct game |

So the failure mode is not "the run errored". It is a **green on evidence the run never actually
created**, which is exactly what this document exists to prevent. A propagation lag is
indistinguishable from the behaviour under test unless the scenario checks.

## The rule, in three parts

1. **Write BO with the game CLOSED** (Rule 8's reasoning: the client must not be playing rounds
   against a configuration that is mid-change).
2. **Wait before reopening.** `ctx.set_automation_denomination(settle_s=5.0)` sleeps after the
   write; 5 s is the figure in use for Dragon VIP dev. It is a *settling* wait, not a retry loop,
   and it is deliberately a named argument so a scenario can state it rather than inherit it.
3. **PROVE the new configuration is the one the client loaded.** The wait is a guess; the check is
   evidence. Read the value back from the reopened client and publish it as a fact:

```python
"client_ladder": client_ladder,                       # what the reopened client offers
"client_on_new_ladder": bool(new_ladder) and client_ladder == new_ladder,
```

A case then **gates on that fact** before grading anything else:

```yaml
- stream: "{facts.swap_applied} == True"          # BO accepted the write
- stream: "{facts.client_on_new_ladder} == True"  # ...and the GAME is running it
```

Without the second line the first proves only that BO is happy, and every assert after it is
describing a session that never saw the change.

## What a stale read means, and what it does not

`client_on_new_ladder == False` after the wait is **a propagation problem, not a game defect**.
Raise the wait, or check the event pipeline — do not read it as "the game ignored the new
denomination", and do not chase it by loosening the case. If the lag turns out to vary, the honest
shape is retry-then-verify (reopen once more after a further wait) rather than a longer blind sleep:
a sleep long enough for the worst case is dead time on every run, and still not a guarantee.

## Do not "fix" it by asserting the old value

The tempting patch, once a stale read is seen, is to accept either ladder. That deletes the case:
the whole point of swapping to the automation ladder (11, 22, 33 — values no real ladder uses) is
that ONE number says which configuration the client loaded. Keep the assert exact and make the
propagation visible instead.
