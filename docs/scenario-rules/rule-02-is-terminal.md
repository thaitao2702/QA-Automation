# Rule 2 — a bonus ends on `is_terminal`, never on the counter alone

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

```python
if is_terminal(b):     # b is None, or freeSpins == 0, or gameState == base
    break
```

Both halves are load-bearing:

- **`freeSpins == 0` alone misses a max-win truncation.** A capped round returns to base with spins
  still on the counter — they are forfeited, and a drain waiting for them never ends. Real payload:
  `games/mystic/fixtures/game-info/maxwin-truncated.json` has `freeSpins: 8, gameState: 1,
  hasReachedMaxWin: true`.
- **`gameState == base` alone misses** a bonus whose counter emptied on a round still reported in
  the bonus state.

Never hand-write the condition. `is_terminal()` is the one definition; Hold & Spin's own terminal
rule (`grid_full`) is separate and deliberate.
