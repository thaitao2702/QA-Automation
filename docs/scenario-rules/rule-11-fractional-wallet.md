# Rule 11 — the wallet takes FRACTIONAL amounts; never round a balance to whole units

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

The Playsite wallet moves any amount and settles it to the cent — verified live to 0.001, with zero
drift over 16 deposit/withdraw pairs. For a long time the framework insisted otherwise:

```python
# WRONG -- this guard was fiction, and it blocked `freespin-zero-balance` for a whole session
if round(delta) != delta:
    raise ValueError("Playsite moves only whole units")
```

It did more than block things, it silently funded the wrong amount. `auto-until-empty` rounded its
target to a whole unit, so asking for "2 spins at 0.6" against a balance of 11840.99 funded **0.99**
— one spin, not two. Ask for the number you actually want:

```python
funded = round(self.fund_spins * bet, 2)      # exactly N bets
await ctx.set_balance(funded)
```
