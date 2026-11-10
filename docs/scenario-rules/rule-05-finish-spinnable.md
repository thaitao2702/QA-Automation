# Rule 5 — a scenario is finished only when the game is spinnable again

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

Do not return while a bonus, a celebration or a popup is still on screen. The next run inherits it,
and the failure surfaces somewhere that has nothing to do with the cause.

The framework now enforces this at both ends of every run (PREPARE / CLEANUP in `run_scenario.py`),
but that is a safety net, not permission to leave a mess: a scenario that ends mid-bonus makes the
next run pay for a drain or a page reload, and its own recorded video stops before the payout.
