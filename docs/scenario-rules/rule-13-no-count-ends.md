# Rule 13 — never end a drain on a COUNT; ends are read from the game

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

Two different loops get this wrong in the same way.

**A free-spin drain must not be bounded by the award.** A free spin can win MORE free spins, so the
counter is not monotonic — it jumps back up on a retrigger. Draining until `is_terminal` handles
that correctly, but the safety cap does not: falling out of `for _ in range(cap)` used to return the
rounds as though the bonus had finished. Everything after that failed confusingly — `settle()` waits
on a spin button that cannot become ready, the run ends inside a live bonus, and the NEXT run's
teardown times out on `awaitFreespinRound`. **Fail loudly at the cap instead**, naming the retriggers
and the spins still owed.

**An out-of-money drain must not be bounded by the funding.** Funding N bets does not mean the wallet
is empty after N spins: an auto spin can win, and a trigger pays across a whole bonus. Read the
server's own balance after every round and stop when it can no longer cover a bet:

```python
final_balance = b.balance          # stake debited, payout credited
if final_balance is not None and final_balance < bet:
    below_bet = True               # the real end -- whenever it happens
    break
```

Then assert it, so "auto stopped" is known to be about MONEY and not something else:

```yaml
- stream: "{facts.balance_below_bet} == True"
```

**Corollary — for auto tests, prefer the INFINITE auto count.** Pick
`auto_count, i=len(GAME.AUTO_COUNTS) - 1` (`AUTO_COUNTS = (10, 20, 30, 50, None)`; `None` is
Infinity) whenever the subject is "auto stops because of X". A finite count can run out on its own
before X happens — and then the test passes for the wrong reason, or fails because the count was
smaller than the spins it needed. Choose a finite count only when the count IS the subject
(`auto-spin-count`) or when the test needs auto to END so the game goes idle
(`reconnect-during-auto` waits for a spinnable base, which never comes under Infinity).
