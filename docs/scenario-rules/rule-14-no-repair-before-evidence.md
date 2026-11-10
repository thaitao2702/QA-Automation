# Rule 14 — never repair the game before the evidence is captured

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

`auto-stop-at-win` used to click auto:stop when the game failed to stop itself. The verdict stayed
honest — `stopped` is read before any click — but **the CLIP LIED**. The video window runs to the end
of the scenario, so the footage showed auto halting, because the framework halted it, and the AI
answered "yes, auto stopped" about a game that had just failed the behaviour under test. Stream red,
video green, on the same run.

Repair belongs in cleanup, which runs after every assert is graded: `ensure_base` already calls
`stop_autoplay`. Inside a scenario, let the broken state stand and film it — and label the tail for
what it is:

```python
ctx.mark("IDLE TAIL -- base game sitting still after the win" if stopped else
         "TAIL -- auto did NOT stop at the win; still running")
```
