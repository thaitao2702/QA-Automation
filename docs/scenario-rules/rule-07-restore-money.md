# Rule 7 — a test that moves the money puts it back exactly

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

Any scenario that changes the wallet (`ctx.set_balance` / `ctx.deposit`) declares
**`teardown: restore-balance`** in the suite:

```yaml
  - run:
      scenario: insufficient-balance
      teardown: restore-balance   # put the wallet back exactly as it was
```

`restore-balance` returns the wallet to the value it held before the run first touched it. The
context records that value on the first `set_balance`/`deposit`, reading it from the **wallet API**
rather than the game's on-screen balance — the client's figure can lag a change made moments
earlier, and restoring to a stale number is itself a drift. If the run never moved the money, the
teardown does nothing instead of inventing a balance.

**Do not use `deposit-10000`.** It is deprecated. It added a flat 10,000 to wherever the balance
happened to be, so the account walked further from its starting point on every run — and a run that
failed *before* it withdrew anything still paid the deposit. Observed on 2026-09-03 climbing
**9,950 → 69,968** across one morning.

## Why the drift is a correctness problem, not just untidiness

It makes `insufficient-balance` fail intermittently, in a way that reads as a wallet outage:

```
reached=False (could not set balance: Insufficient balance)
```

`set_balance` reads the wallet, then withdraws the difference. If the balance changes **between
those two calls** — an overlapping run, a session still spinning, a teardown from an aborted run —
the withdrawal is refused with exactly that message, and the failure names the wallet rather than
the race that caused it. A balance that always starts from the same place removes the condition.

The same rule applies to anything else a test moves and the next run inherits: the bet level, a
forced paytable, a language. Restore it, and restore it to what it *was* — not to a constant that
happens to be convenient.
