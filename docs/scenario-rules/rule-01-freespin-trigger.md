# Rule 1 — every BET response can be a free-spin trigger

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

> **Whenever you read a bet response, ask: what if this spin just triggered free spins?**
> On any game with a free-spin system, *any* base spin can award a bonus — and the client then
> **auto-plays the whole bonus on its own**, with no click from the framework. Decide explicitly
> whether your scenario handles that. There is no safe default of ignoring it.

## Why ignoring it does not just lose data — it hangs the run

The moment a spin triggers, the client starts auto-playing free spins. The framework is not driving
them, but it is blocked by them:

`waitForSpinClickable` **must not resolve during free spins** (`docs/game-bridge-contract.md` §2).
So the next thing your scenario does — a spin, a settle, anything that waits for spin readiness —
waits for the entire bonus, and if the budget runs out first the run fails.

Worse, the bonus's responses go **unread**. A later drain then waits on `awaitFreespinRound` for
rounds that have already been emitted, times out, and can take the whole suite down with it.

## What it looks like when you get it wrong

Real failure, `base-cluster-win`:

```
base-cluster-win  ERROR: RuntimeError: waitForSpinClickable rejected ->
  [Bridge] waitForSpinClickable() timed out after 60000ms.
  Last blocking reason: 'spin_state:STOP'.
  Snapshot: { ..., "freeSpinsLeft": 4, "totalFreeSpins": 8,
              "gameMode": "FREE_SPIN", "isFreeSpinMode": true,
              "roundState": "SPINNING", "busyOps": ["spinResult"] }
```

Read the snapshot, not just the message: **`gameMode: "FREE_SPIN"` with 4 of 8 spins left.** The
bridge was right to refuse — the game was mid-bonus. The scenario asked at the wrong time.

The bug was a hunting loop whose win test excluded bonus rounds but whose loop body did nothing
about them:

```python
for _ in range(self.max_spins):
    b = await ctx.base_spin()
    if b.game_state == BASE_STATE and b.payout > 0 and b.cascades >= 2:   # a trigger fails this...
        win = b
        break
                                                # ...so the loop spins again, mid-bonus. Hang.
```

A cascade landed the trigger, the response came back `gameState: 2`, the win test was `False`, and
the next iteration called `base_spin()` while free spins were still playing.

## The check

```python
if is_freespin_trigger(b):        # awarded > 0, or the round is already in a bonus state
    ...
```

Never infer this from client state or a UI counter. `is_freespin_trigger` reads the **response**
through the game's profile, so it works on a game whose counters are nested or spelled differently.

## Then decide — three legitimate answers

**a) Play it out and carry on** — the usual answer for a loop hunting a specific outcome.

```python
for _ in range(self.max_spins):
    b = await ctx.base_spin()
    if is_freespin_trigger(b):
        await play_out_freespins(ctx, b)     # drains to terminal, then settles
        continue                             # not the outcome we want -- keep hunting
    if b.payout > 0 and b.cascades >= 2:
        win = b
        break
```

**b) Play it out and keep the rounds** — when the bonus is part of what the case grades.

```python
b, rounds = await spin_and_finish(ctx)       # one spin, taken to a clean base
# rounds == [spin] normally, or [trigger, ...free spins..., terminal] if it triggered
```

Use `finish_round(ctx, b)` for a round you already have — one the client fired itself (a Space
press, an auto spin), or where a gesture has to happen between the response and the settle.

**c) The bonus IS the subject** — `fs-complete`, `freespin-cascade`, `hold-spin-complete`. These
already drive it deliberately; follow their shape.

What is **not** an option: reading the response, ignoring the trigger, and spinning again.

## Put it inside the loop, not just at the end

`base-cluster-win` *did* call `play_out_freespins` — on the one follow-up spin after the loop. That
is why a class-level search for "does this scenario handle triggers?" said yes and the bug survived.

**Every loop body that spins needs its own handling.** A tail that handles it does not protect the
loop above it.

## Self-check

```python
# a loop that spins but does not handle a trigger IN THE LOOP BODY
python - <<'PY'
import ast, io
src = io.open("scripts/scenario_engine.py", encoding="utf-8").read(); tree = ast.parse(src)
H = ("is_freespin_trigger","play_out_freespins","finish_round","spin_and_finish","free_spin")
for n in ast.walk(tree):
    if isinstance(n, (ast.For, ast.While)):
        body = "\n".join(ast.get_source_segment(src, s) or "" for s in n.body)
        if ("base_spin(" in body or "observe_bet(" in body) and not any(h in body for h in H):
            print("  !! line", n.lineno)
PY
```

Expected output: nothing. Run it after adding a scenario.

## Self-check 2 — an attribute nothing assigns

A scenario that reads `self.watch_ms` without a constructor that sets it raises `AttributeError`
only when that line is reached — which for a tail like `confirm_stopped` means after the whole run
has already played. Adding a knob to one scenario and forgetting its `__init__` in another is an
easy slip; this catches it statically.

```python
python - <<'PY'
import ast, io
src = io.open("scripts/scenario_engine.py", encoding="utf-8").read(); tree = ast.parse(src)
for n in ast.walk(tree):
    if not isinstance(n, ast.ClassDef): continue
    if not any(isinstance(b, ast.Assign) and getattr(b.targets[0], "id", None) == "name" for b in n.body):
        continue
    defined = set()
    for b in n.body:
        if isinstance(b, ast.Assign):      defined |= {getattr(t, "id", None) for t in b.targets}
        elif isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name): defined.add(b.target.id)
        elif isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):          defined.add(b.name)
    init_set, reads = set(), set()
    for m in n.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for x in ast.walk(m):
                if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self":
                    if m.name == "__init__" and isinstance(x.ctx, ast.Store): init_set.add(x.attr)
                    elif isinstance(x.ctx, ast.Load):                          reads.add(x.attr)
    miss = sorted(reads - init_set - defined)
    if miss: print(f"  !! {n.name}: reads self.{miss} but nothing assigns it")
PY
```

Expected output: nothing.
