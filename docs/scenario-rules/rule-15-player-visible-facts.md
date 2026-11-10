# Rule 15 — a fact the client cannot clear is not a defect; assert what the player can see

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

When the balance runs out, Chaos stays in the auto STATE and raises the out-of-money popup. The popup
is what prevents any further bet; `isAutoPlay` only clears once it is dealt with. Verified live: no
further round was wagered, the screen was plainly idle, and the flag stayed true.

So `auto-until-empty` asserts that nothing more was wagered and that the money really ran out — and
deliberately does **not** assert `auto_flag_after`, while `auto-stop-at-win` (no popup) still does.
Before asserting an internal flag, ask what the player can actually do: if the game cannot bet, the
behaviour is correct whatever the flag says.
