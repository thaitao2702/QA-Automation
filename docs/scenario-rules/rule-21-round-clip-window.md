# Rule 21 — a round's clip ends when the NEXT round starts, which is before its presentation plays

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

> **`at: trigger` does not contain the trigger's animation.** A round's video window runs from that
> round's start to the next round's start, and an AUTO-played round's start is recorded the moment
> the framework begins WAITING for it — immediately after the previous response. The response lands
> before the award banner, the bonus teaser or the big-win celebration plays, so a single-round clip
> ends mid-animation. When the claim is about a PRESENTATION, span one round further.

This is `DriveContext.windows()`:

```python
def windows(self) -> list:
    """Round i spans from its own spin start to the next round's start (the last round runs to
    now, i.e. through settle)."""
```

For two CLICKED spins that is right: round *i* runs from its click to round *i+1*'s click, and
everything the client drew in between — the win count-up, the celebration — is inside it. The
trouble is the auto-played round, where `free_spin()` records `t0` and *then* awaits:

```python
async def free_spin(self, phase: str = "freespin") -> Bet:
    t0 = self.now_s()                    # <- recorded BEFORE the response, and the drain calls
    b = Bet.parse(await await_freespin_round(...))   #    this the instant the trigger lands
```

A drain calls `free_spin()` the instant the triggering response arrives, so free spin 1's "start" is
effectively the trigger's response time. The trigger's window therefore closes at its own response —
before a single frame of the award animation.

## What it looks like when you get it wrong

The run is green, the suite validates, and the clip is two seconds of reels stopping. Grandma Pets'
Buy Bonus is the worked example: the purchase response carries no grid at all, the client then plays
a fake trigger spin and announces 15 free spins, and `at: trigger` captured none of it. The AI was
asked "does the game announce how many free spins were won?" about footage that ended before the
announcement — and answering **no** to that is correct, so the case reds and the game is innocent.

## The fix

Span to the round after the one the presentation belongs to, and tell the AI the extra round is
framing:

```yaml
video:
  at: [trigger, "rounds[1]"]            # the purchase AND the first free spin
  prompt: >-
    ... Judge ONLY the fake spin and the announcement — the free spin at the end of the clip is
    there so the announcement is fully visible, and is not the subject.
```

The last round of a scenario needs none of this: its window runs to `now`, so it already covers
settle. `at: terminal` and `at: [trigger, terminal]` are safe for the same reason.

## Two traps in writing the span

* **Quote the index.** `at: [trigger, rounds[1]]` is a YAML **parse error** — inside a flow
  sequence, the `[` opens a nested list. It must be `at: [trigger, "rounds[1]"]`.
* **A span is exactly TWO targets.** `resolve_window` takes `at[0]`'s start and `at[1]`'s end; a
  three-element list is not a wider span. It falls through to `at in result.marks`, which raises
  `TypeError: unhashable type: 'list'` **at run time**, after the bonus has already played.
  `validate_suites.py` now catches this before a run starts.

## Which claims this applies to

Anything the client draws AFTER the response: free-spin award banners, Buy Bonus teasers, big-win
celebrations, a counter that counts up, a popup that opens on a win. A claim about the REELS of that
round is unaffected — those are drawn before the response is processed.
