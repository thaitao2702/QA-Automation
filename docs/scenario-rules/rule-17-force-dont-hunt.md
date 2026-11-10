# Rule 17 — a run that NEEDS a free-spin trigger FORCES one; it never hunts

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

If a case cannot be graded without a bonus, the run declares a forced paytable. It does not spin the
base game hoping for one.

```yaml
- run:
    scenario: fs-complete
    paytable: { ref: chaos-fs-easy, max_win_x: 500 }   # forced: a bonus in a spin or two
```

**Hunting is not the cautious option, it is the expensive one.** A budget like
`params: { max_base: 300 }` is 300 real spins at the live stake, paid before the footage the run
exists for even starts. And the failure mode is quiet: a run that spends the budget without
triggering returns **unreached**, so every case under it reports `inconclusive` — excluded from the
fail counts, green-adjacent in the summary, and proving nothing. A suite full of those looks like
coverage and is not.

**Delete the budget, do not tune it.** The scenario keeps its own default (`fs-complete`: 400) as an
unreachable backstop for the case where the forced paytable did not take. Raising a hunt budget to
make a trigger more likely is the anti-pattern this rule exists to stop — forcing is both cheaper
and deterministic.

**Which fixture:** the smallest trigger that reaches the feature (Rule 6). For Chaos that is
`chaos-fs-easy` — FreeSpin! at 30% on base reels 0/1/2, the other two zeroed, `FreeSpinGame` left
alone so the bonus does not retrigger itself.

## Pair the forced paytable with a cap — and default it to 20x the bet

**A bonus that can FEED ITSELF has no natural end, and the cap is the only thing that bounds it.**
Three shapes do that, and any run that forces one MUST carry a cap:

| the bonus extends itself by | example |
|---|---|
| retriggering from inside free spins | a forced `FreeSpinGame` section keeps landing scatters, so the counter goes back up faster than it comes down |
| a symbol that RESETS the counter | Hold & Spin — every new Coin puts the respins back to 3 (`play_out_holdspin` raises at its cap for exactly this) |
| a bought bonus on an easy paytable | the purchase lands in the same runaway |

Without a cap such a run is bounded only by the scenario's spin budget: it plays for hundreds of
rounds, produces a clip nobody can grade, and ends by raising at the drain cap. With one, the server
truncates at the ceiling and sets `hasReachedMaxWin`, which the framework already treats as terminal
(Rule 9) — the round ends cleanly and clears the counter, so `{terminal.free_spins} == 0` and
`terminal_reached` both hold.

**The default is `max_win_x: 20`** — 20x the live base bet. It is long enough that a counter visibly
counts down and a multiplier gets sampled more than once, and short enough to bound a runaway and
keep the clip watchable. `10 * bet` was the old default and truncates too often: it regularly leaves
a one-round bonus, which passes on the stream and shows almost nothing on screen.

**Raise it when the SIZE of the feature is the subject, not its stake.** A Hold & Spin whose case is
"every respin plays and the total pays out", a max-win case, an award-value case — those are about
how big or how long the feature gets, and a tight ceiling changes the answer. Those runs keep a
LOOSE cap (the Zeus/Dragon Hold & Spin suites use `500`), which still bounds a runaway while leaving
the feature's own arithmetic intact.

```yaml
paytable: { ref: chaos-fs-easy, max_win_x: 20 }    # default: bounds a retriggering bonus
paytable: { ref: fs-trigger3-countdown, max_win_x: 500 }   # the feature's LENGTH is the case
```

⚠ **A cap is never the thing under test.** It shapes the run; the asserts are about the stake, the
counter or the money. If a case's verdict changes when you move the cap, the case is measuring the
cap.

**What a tight cap costs is LENGTH, and that is the thing to guard.** The cap can land on the
trigger spin or on free spin 1, which ends the feature before the case can observe anything:

| case wants to see | a 1-round bonus gives it |
|---|---|
| a free-spin counter COUNTING DOWN | one number, never decrementing |
| a multiplier sampled across the bonus | a single sample |
| "the bonus ran and then ended" | passes on the stream, shows almost nothing on screen |

So a run at a tight cap asserts the LENGTH it needs, rather than trusting it:

```yaml
- stream: "{facts.free_spins_played} >= 2"   # a countdown needs at least two rounds to be visible
```

A failure there is not a product bug — it says the cap is too tight for this paytable's award, and
the fix is to raise the multiple. Shape the award with the paytable, bound the runaway with the cap,
and let the assert tell you when the two are mismatched.
