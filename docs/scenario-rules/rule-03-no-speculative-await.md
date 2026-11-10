# Rule 3 — never call `awaitFreespinRound` speculatively

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

Only await a free-spin round when a **response** has told you a bonus is open — a trigger you just
read, or `gameInfo.pending` at load. Waiting on a round that is not coming is not a slow no-op: it
burns the timeout and then raises, and in a teardown that used to abort whole suites.

If you need "get back to a clean base" without knowing the state, use `ensure_base(ctx)` — it
inspects the last response first and falls back to a reload rather than guessing.
