# Rule 8 — a bonus plays ITSELF; tear the client down before acting on it

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

The client **auto-plays** a free-spin feature. Nothing pauses between rounds while a scenario does
something else, so any out-of-band work races the remaining spins:

```python
# WRONG -- the bonus keeps spinning during the API round trip
await ctx.set_balance(0)          # Playsite call, hundreds of ms
await ctx.reload_and_start()      # ...bonus may already be over: nothing left to resume
```

`set_balance` is a network call to the wallet API. While it is in flight the feature can finish, or
hit the max-win cap (Rule 9), so by the time the balance is zero there is no pending bonus at all.
The reload then resumes nothing and the case grades a state it never created — and it does so
*quietly*, because "no free spins left" looks identical to "the bonus completed normally".

**Stop the client first.**

```python
await ctx.stop_client()                       # navigates to about:blank -- no round can play
await ctx.set_balance(0)                      # now lands against a known state
resumed = await ctx.reload_and_start(url=ctx.url)
```

`stop_client()` destroys the game so no further round is possible. The bonus lives on the SERVER
(`gameInfo.pending`), so it survives being disconnected and the reload resumes it — which is exactly
what `freespin-zero-balance` is testing. The page object and the video recording are untouched.

**After `stop_client()` you must navigate, not reload.** The current page is `about:blank`; a plain
`reload_and_start()` would re-load that. Pass the game URL: `reload_and_start(url=ctx.url)`.

This applies to anything that must land against a known mid-bonus state — zeroing the wallet,
changing a BO setting, opening a second session. A `sleep` is not a substitute: it does not stop the
spins, it just makes the race longer.
