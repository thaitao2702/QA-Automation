# Rule 19 — a field that says NOTHING is not a value; never do arithmetic on a sentinel

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

> **Before asserting on a response field, ask what the server sends when it has nothing to say.**
> A backend that means *"unchanged"* or *"not applicable"* has to put SOMETHING in the field, and what
> it puts is usually a number — `-1`, `0`, sometimes `null`. Read literally, that number walks straight
> into arithmetic and produces a red on a game that did nothing wrong.

Dragon VIP's Hold & Spin, measured 2026-09-16:

| round | phase | stake | payout | `balance` |
|---|---|---|---|---|
| 0 | trigger | 10.50 | 0 | 14283.04 |
| 1 | respin | 0 | 0 | **-1** |
| 2 | respin | 0 | 0 | **-1** |
| 3 | terminal | 0 | 56.00 | 14339.04 |

The money is exactly right — `14283.04 + 56.00 == 14339.04`, and a respin costs nothing. The respins
simply do not report a balance, and the server says so with `-1`. `conservation` compared each round
against the previous one and reported two violations of a feature that behaved perfectly:

```
round 1: balance -1.0 != 14283.04 - 0.0 + 0.0
round 3: balance 14339.04 != -1.0 - 0.0 + 56.0
```

Note the second line: one sentinel poisons the NEXT comparison too, so a single unread field reds a
whole feature.

## The fix is to carry, not to skip

`conservation` now treats a non-positive balance as *not reported*: it carries that round's stake and
payout forward and compares against the last figure the server actually stated. A real wallet cannot
be negative, so `-1` can only mean "nothing to say".

**Carrying is not the same as skipping**, and the difference is the whole rule. Skipping would let a
bonus pay whatever it liked in the unreported rounds; carrying means the next round that DOES report a
balance has to account for every stake and payout in between. The check stays as strong, and it now
answers the question the case is really asking: *did the feature's money add up end to end?*

## What to do in a new scenario

* **Expose the raw values as facts** (`respin_stakes`, `respin_premium_ids`), so the sentinel is
  visible rather than silently normalised away. A fact that reads `[-1, -1]` is how the next person
  finds this in one look.
* **Gate a case on a fact that says the field was answerable**, exactly as `ante_verifiable_in_fs`
  gates the ante-frozen rule (Rule 9's sibling problem). "No data" must never read as "correct".
* **Do not hard-code the sentinel into a profile field reader.** `-1` is this backend's spelling;
  another may send `0` or omit the key. The reader answers "did the server report one?", and the
  invariant decides what to do about it.
* **Write it down in `games/<game>/bet-response.md`** when you capture the payload. This one is in no
  doc — Zeus documents a `-1` sentinel only for `autoSpinsRemaining` ("never been set"), which is a
  different field on a different call.
