# Rule 12 — you cannot drain the wallet during a bonus; arrive broke instead

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

The server refuses to reduce the balance while a bonus is open, and for a while after recent play.
It reports `Insufficient balance` **against a balance that has not moved** — measured across 45
retries over 90s with the client already torn down. Retrying does not help, and there is a hard
floor besides: the wallet never reaches exactly 0.00, only 0.01.

So a test that needs a broke player must not take the money away. **Give the player exactly one bet
and let them spend it**, the way a real player goes broke:

```python
await ctx.set_balance(stake)      # exactly one bet -- lands with no bonus open
await ctx.reload_and_start()      # the client caches the balance; it must re-read it
b = await ctx.base_spin()         # the stake is debited HERE, server-side -- now broke
```

The debit happens inside the spin, so there is no wallet call to be refused and no race with the
auto-playing bonus. `freespin-zero-balance` hunts this way, discarding an attempt that does not
trigger, that caps, **or whose trigger paid enough to leave the player able to bet again**.

Judge "broke" at the moment the bonus BEGAN, not later: free spins pay in, so the balance climbs as
soon as the bonus wins anything. Asserting on the reload figure fails a perfectly good run.

```yaml
- stream: "{facts.broke_at_trigger} == True"    # balance after the triggering spin, below one stake
```

And when a wallet call is unavoidable, widen the retry — `set_balance(x, attempts=10, pause_s=2.0)`
— but ONLY where nothing can play while it waits.
