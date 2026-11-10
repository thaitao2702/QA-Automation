# Rule 9 — the max-win cap can END a bonus at any moment, including its first round

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

A run that forces an easy free-spin paytable also caps the payout (`max_win_x`, Rule 6) so the
feature cannot spin forever. When a round reaches the cap the server sets `hasReachedMaxWin`, **ends
that free-spin round, and clears the counter** — the bonus is over, the player has no free spins
left, and the game returns to base.

So a capped bonus is a genuinely finished bonus: `is_terminal` treats `hasReachedMaxWin` as the end,
`{terminal.free_spins} == 0` holds, and nothing is carried into the next session. The cap is safe to
set tight; what it costs you is LENGTH, which is the whole of this rule.

> Fixed backend-side 2026-09-05. This rule previously described capped spins as staying OWED on the
> server — compounding across runs and freezing the wallet. That was a backend defect, it has been
> fixed, and the workarounds it justified (holding `max_win_x` at 500 purely to avoid truncation)
> are no longer needed.

## What `max_win_x` multiplies (2026-09-04)

The cap is a multiple of the bet, and the bet is now READ FROM THE OPEN GAME. The runner opens the
game, reads the stake it is actually sitting at, resolves the cap against that, pushes it to BO,
then re-enters with the forced paytable in force. That costs one extra page load on the first run
of a session and nothing after it, because the page is already up.

It used to multiply `bo.reference_bet` in `env.config.*.yml` — a hand-maintained number that drifts
away from the game. Chaos had `reference_bet: 0.6` against a game that opens at `4.00`, so
`max_win_x: 500` resolved to a **300** cap: 75x the real bet rather than 500x, and low enough that
bonuses truncated on `hasReachedMaxWin`. `reference_bet` is now only the FALLBACK, for a game that
will not open or will not publish its bet.

Two forms, both meaning the same kind of thing:

```yaml
paytable: { ref: chaos-fs-easy, max_win_x: 500 }                # 500 x the live base bet
paytable: { ref: chaos-fs-easy, max_win_x: "max(500, 100 * bet)" }   # a function of it
paytable: { ref: chaos-fs-easy, max_payout: 2000 }              # absolute; skips the bet entirely
```

The expression is evaluated with no builtins and a four-name namespace (`bet`, `min`, `max`,
`round`), so a suite cannot smuggle code into the runner through a config file.

**`bet` is always the BASE stake**, never the ante-inflated one. A run that switches Ante Bet on
would otherwise get a 3x higher ceiling than the non-ante runs it is compared against, purely
because of the toggle.

That needs no arithmetic, because **`getSnapshot().betAmount` is already the base bet** — it does
not move when ante is toggled. Measured 2026-09-04: with ante ON the snapshot read `4.00` while the
server deducted `stake: 12.00` per spin. The first version of this reader "corrected" for ante by
dividing by the multiplier and turned a 4.00 bet into 1.33, capping the run at a third of what the
suite asked for. The bet display is the base; leave it alone.

None of this softens Rule 9: reading the real bet makes the multiple mean what it says, it does not
make a small multiple safe. `max_win_x: "10 * bet"` is still a cap that ends bonuses.

So a free-spin scenario must expect the bonus to be over at three points where it looks impossible:

| where | what it means |
|---|---|
| on the TRIGGER spin | the award is forfeited — there was never a bonus to play |
| on the FIRST free spin | the feature ended before the scenario could set anything up |
| on any later round | the drain ends early — the counter IS 0, but the bonus was shorter than the case may need |

None of these is a defect — it is the cap doing its job. Handle them, do not assert them away:

```python
if b.has_max_win or is_terminal(b):
    triggers_capped += 1
    await ctx.settle()          # back at base
    continue                    # ...and hunt another trigger
```

**Discard and retry, do not fail.** `freespin-zero-balance` loops: it hunts a trigger that is not
capped, plays one free spin, and if THAT caps it throws the attempt away and hunts again. It gives
up only when the spin budget runs out, and then says how many attempts it discarded
(`facts.attempts_discarded`) so a cap that is too tight for the paytable is visible rather than
looking like "the feature never triggers".

**A finished bonus satisfies the obvious asserts.** `terminal_reached` and
`{terminal.free_spins} == 0` are both true of a bonus that ended *before* the thing under test ever
happened. Add a fact that proves the behaviour actually occurred:

```yaml
- stream: "{facts.played_after_zero} == True"   # rounds really did play AFTER the wallet hit zero
- stream: "{facts.first_fs_capped} == False"    # ...and the graded attempt was not a truncated one
```

If the discards are frequent, raise the run's `max_win_x` — the cap is too tight for the paytable's
award size, and see Rule 6 for lowering the award instead.
