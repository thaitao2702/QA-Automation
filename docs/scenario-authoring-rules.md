# Scenario authoring rules

Rules for writing a **scenario** (the imperative code in `scripts/scenario_engine.py` that drives
the game). Read this before adding or editing one.

For writing test **cases** — the YAML `scenario:` + `assert:` layer — see
`docs/scenario-assert-authoring.md` instead. For what the bridge must provide, see
`docs/game-bridge-contract.md`.

---

## The toolkit — reuse these, do not re-implement them

**The full list is `docs/scenario-toolkit.md`** — every helper a scenario may call, grouped by job,
with signatures, the facts each returns, a scenario template, and the ones never to call. It is
checked against the engine by `tests/chaos/build_scenario_index.py`, so it cannot fall behind.

The ones you will reach for most:

| Job | Helper |
|---|---|
| one spin | `spin_and_finish(ctx)` — handles a trigger for you (Rule 1) |
| drain a bonus | `play_out_freespins` / `play_out_holdspin` — end on the game's terminal, raise at the cap |
| "it came back to base" | `settle_to_base(ctx)` — proves it, then holds the idle screen |
| "it stopped" | `confirm_stopped(ctx)` — watches, then holds (Rule 4) |
| controls | `require_buttons(...)` up front, then `ctx.click_button(name)` |
| a precondition failed | `raise Unreached(reason)` — one handler reports it |

---

## Rule 1 — every BET response can be a free-spin trigger

> **Whenever you read a bet response, ask: what if this spin just triggered free spins?**
> On any game with a free-spin system, *any* base spin can award a bonus — and the client then
> **auto-plays the whole bonus on its own**, with no click from the framework. Decide explicitly
> whether your scenario handles that. There is no safe default of ignoring it.

### Why ignoring it does not just lose data — it hangs the run

The moment a spin triggers, the client starts auto-playing free spins. The framework is not driving
them, but it is blocked by them:

`waitForSpinClickable` **must not resolve during free spins** (`docs/game-bridge-contract.md` §2).
So the next thing your scenario does — a spin, a settle, anything that waits for spin readiness —
waits for the entire bonus, and if the budget runs out first the run fails.

Worse, the bonus's responses go **unread**. A later drain then waits on `awaitFreespinRound` for
rounds that have already been emitted, times out, and can take the whole suite down with it.

### What it looks like when you get it wrong

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

### The check

```python
if is_freespin_trigger(b):        # awarded > 0, or the round is already in a bonus state
    ...
```

Never infer this from client state or a UI counter. `is_freespin_trigger` reads the **response**
through the game's profile, so it works on a game whose counters are nested or spelled differently.

### Then decide — three legitimate answers

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

### Put it inside the loop, not just at the end

`base-cluster-win` *did* call `play_out_freespins` — on the one follow-up spin after the loop. That
is why a class-level search for "does this scenario handle triggers?" said yes and the bug survived.

**Every loop body that spins needs its own handling.** A tail that handles it does not protect the
loop above it.

### Self-check

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

### Self-check 2 — an attribute nothing assigns

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

---

## Rule 2 — a bonus ends on `is_terminal`, never on the counter alone

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

---

## Rule 3 — never call `awaitFreespinRound` speculatively

Only await a free-spin round when a **response** has told you a bonus is open — a trigger you just
read, or `gameInfo.pending` at load. Waiting on a round that is not coming is not a slow no-op: it
burns the timeout and then raises, and in a teardown that used to abort whole suites.

If you need "get back to a clean base" without knowing the state, use `ensure_base(ctx)` — it
inspects the last response first and falls back to a reload rather than guessing.

---

## Rule 4 — proving something STOPPED needs time on the clock

> **A scenario that ends the moment the last round lands has not shown that anything stopped.**
> Whenever the claim is "…and then it stops" — auto-play finishing its count, the user halting it, a
> reload cancelling it, the balance running out — WATCH for a while, then HOLD. Use
> `confirm_stopped(ctx)`.

"It stopped" is a **negative** claim, and a negative is only worth anything if you waited. Two
separate things go wrong without the wait:

**Nothing is proved.** Having collected the N rounds you asked for says *your loop* ended, not that
the client did. A client that overruns its count keeps betting and no assert notices. `settle()`
does not save you: `waitForSpinClickable` is not supposed to resolve during autoplay, but its
fallback swallows the case where it does anyway.

**The AI has nothing to look at.** The clip ends on the last payout, so "did auto stop?" is being
asked of a video that merely *stopped*. Answering no to that is correct.

```python
stop = await confirm_stopped(ctx, watch_ms=6000, dwell_ms=3000, label="AUTO STOPPED")
# facts: stopped · extra_round_after_stop · auto_flag_after · auto_spins_left_after · stop_watch_s
return ScenarioResult(..., facts={..., **stop})
```

It watches `awaitBetResponse` for `watch_ms` — **nothing arriving is the evidence** — reads the
client's own auto flag, marks the moment, and dwells so the still screen is recorded. A round that
does arrive is recorded into the stream rather than discarded, so the proof of the defect is in
`events.jsonl`.

In the case, let the exact facts carry the claim and tell the AI to judge the tail:

```yaml
- stream: "{facts.extra_round_after_stop} == False"
- stream: "{facts.auto_flag_after} == False"
- video:
    prompt: >-
      ... The clip deliberately continues for about {facts.stop_watch_s} seconds AFTER the last
      spin and then holds still — during that tail the reels must not start again. A clip that
      simply ends is not evidence; judge the tail.
```

**This is not only about auto-play.** The same shape applies to any "X no longer happens" claim: a
bonus that must not retrigger, a popup that must stay closed, a control that must stay disabled.
If the assertion is that nothing happens, the scenario has to spend time not-happening.

## Rule 5 — a scenario is finished only when the game is spinnable again

Do not return while a bonus, a celebration or a popup is still on screen. The next run inherits it,
and the failure surfaces somewhere that has nothing to do with the cause.

The framework now enforces this at both ends of every run (PREPARE / CLEANUP in `run_scenario.py`),
but that is a safety net, not permission to leave a mess: a scenario that ends mid-bonus makes the
next run pay for a drain or a page reload, and its own recorded video stops before the payout.

## Rule 6 — force the SMALLEST trigger that reaches the feature, not the biggest

A forced paytable exists to reach a feature **quickly and identically every run**. It is not there to
make the feature as big as possible — and the two pull in opposite directions, because in most games
the award scales with how hard the trigger was.

Chaos free spins:

| `FreeSpin!` on N distinct reels | award |
|---|---|
| 3 | **12 spins** |
| 4 | 15 spins |
| 5 | 20 spins |

Raising `FreeSpin!` on **all five** reels triggers ~every spin — and lands 4 or 5 reels most of the
time, so nearly every run sits through 15–20 free spins, each with its own animation. Target only
the **three** reels the minimum award needs.

### Make it FREQUENT (~30%), never FORCED (100%)

This is the part that is easy to get wrong, and getting it wrong is invisible until you read the
grid. Reel weights are **per cell**. A reel raised to 100% of one symbol does not show that symbol
once — it shows it in **every visible row**. And in a ways game the outcome is the PRODUCT of the
per-reel counts, so a 100% force multiplies the very thing you were trying to keep small:

| symbol per reel | Chaos free spins (award = base x ways) | Chaos P1 (pay = ways x 10 x bet) |
|---|---|---|
| 100% force → 3, 3, 3 | ~27 ways → a bonus of **hundreds of spins**, every run | 27 ways → **270x** base bet, always capped |
| **30%** → usually 1, 1, 1 | ways ~2.5 → the **12-spin minimum** | 1–2 ways → ~26x base bet, the ordinary win |

So both Chaos fixtures use a probability pass, not a force:

```
python scripts/derive_paytable.py --game chaos --base base --symbol "FreeSpin!" \
    --reels 0,1,2 --probability 0.30 --section BaseGame --out chaos-fs-easy
python scripts/derive_paytable.py --game chaos --base base --symbol "P1" \
    --reels 0,2,4 --probability 0.30 --section BaseGame --out chaos-p1-ways
```

At 30% per cell a reel shows the symbol ~66% of the time, so three reels line up on roughly a third
of spins.

**`--zero-elsewhere` pins the award to its minimum.** A probability pass raises the symbol on the
reels you name and leaves the others at their real rate — which is fine when the award does not
scale, and wasteful when it does. On Chaos ~17% of triggers still reach a fourth or fifth reel, and
each one costs the run three to eight extra rounds of animation. Adding `--zero-elsewhere` sets the
symbol to weight 0 on every reel you did NOT target, so it is not merely unlikely there but
impossible, and a trigger is always the 3-reel, shortest award (`joker-fs-easy` is derived that
way). Leave it off when the other reels' real rate is part of what the case observes.

**The price is a short hunt, and it is worth paying.** 30% does not guarantee the condition on spin
1, so **the run spins until it matches** under a budget — a handful of spins for an outcome that
looks like real play, instead of one spin for an outcome no player will ever see. Size the budget
from the odds and say so in the suite comment (Chaos P1: ~28% a spin, `max_spins: 40`, a
~1-in-a-million chance of missing).

Reach for a 100% force only when the symbol's COUNT does not scale the outcome — a fixed layout
check, say — and write down why.

### A two-sided rule puts a CEILING on the probability too

Some rules are thresholds, and testing one properly needs a result on BOTH sides of it. Chaos's
Raining Wild is the example: 2+ Wilds must play the animation, fewer must not — and without the
negative half an implementation that rained on every spin would pass perfectly.

For those, raising the symbol does not merely cost money, it deletes the case:

| `WILD!` per cell | P(2+ Wilds) | P(control, <2) | spins to get BOTH |
|---|---|---|---|
| **15%** | ~55% | ~45% | median 3 |
| 50% | 99.7% | 0.3% | ~315 |
| 100% force | 100% | **0** | never — unreached, every case inconclusive |

Aim at the MIDDLE of the distribution, not the top: pick the probability that maximises the rarer
side. And never force the symbol onto a reel the rule under test says it cannot occupy —
`chaos-wild-rain` leaves reel 0 at weight 0 precisely because CR_059 asserts Wild never lands
there.

**Only force the section you need.** `--section BaseGame` leaves `FreeSpinGame` at its real weights,
so the bonus does not retrigger itself and actually ends. Forcing the bonus section too is a common
way to make `terminal_reached` hang.

**The general rule.** Before forcing an outcome, ask what the case actually needs to observe. A case
about *"free spins play at stake 0"* needs a bonus, any bonus — so take the cheapest one. Only force
a bigger outcome when the SIZE is the thing under test (a max-win cap, a 5-reel award value), and
then say so in the suite comment, because the run cost is now deliberate rather than accidental.

## Rule 7 — a test that moves the money puts it back exactly

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

### Why the drift is a correctness problem, not just untidiness

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

## Rule 8 — a bonus plays ITSELF; tear the client down before acting on it

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

---

## Rule 9 — the max-win cap can END a bonus at any moment, including its first round

A run that forces an easy free-spin paytable also caps the payout (`max_win_x`, Rule 6) so the
feature cannot spin forever. When a round reaches the cap the server sets `hasReachedMaxWin`, **ends
that free-spin round, and clears the counter** — the bonus is over, the player has no free spins
left, and the game returns to base.

So a capped bonus is a genuinely finished bonus: `is_terminal` treats `hasReachedMaxWin` as the end,
`{terminal.free_spins} == 0` holds, and nothing is carried into the next session. The cap is safe to
set tight; what it costs you is LENGTH, which is the whole of this rule.

> Fixed backend-side 2026-09-05. This rule previously described capped spins as staying OWED on the
> server — compounding across runs and freezing the wallet. That was a backend defect, it has been
> fixed, and the workarounds it justified (holding `max_win_x` at 500 purely to avoid truncation)
> are no longer needed.

### What `max_win_x` multiplies (2026-09-04)

The cap is a multiple of the bet, and the bet is now READ FROM THE OPEN GAME. The runner opens the
game, reads the stake it is actually sitting at, resolves the cap against that, pushes it to BO,
then re-enters with the forced paytable in force. That costs one extra page load on the first run
of a session and nothing after it, because the page is already up.

It used to multiply `bo.reference_bet` in `env.config.*.yml` — a hand-maintained number that drifts
away from the game. Chaos had `reference_bet: 0.6` against a game that opens at `4.00`, so
`max_win_x: 500` resolved to a **300** cap: 75x the real bet rather than 500x, and low enough that
bonuses truncated on `hasReachedMaxWin`. `reference_bet` is now only the FALLBACK, for a game that
will not open or will not publish its bet.

Two forms, both meaning the same kind of thing:

```yaml
paytable: { ref: chaos-fs-easy, max_win_x: 500 }                # 500 x the live base bet
paytable: { ref: chaos-fs-easy, max_win_x: "max(500, 100 * bet)" }   # a function of it
paytable: { ref: chaos-fs-easy, max_payout: 2000 }              # absolute; skips the bet entirely
```

The expression is evaluated with no builtins and a four-name namespace (`bet`, `min`, `max`,
`round`), so a suite cannot smuggle code into the runner through a config file.

**`bet` is always the BASE stake**, never the ante-inflated one. A run that switches Ante Bet on
would otherwise get a 3x higher ceiling than the non-ante runs it is compared against, purely
because of the toggle.

That needs no arithmetic, because **`getSnapshot().betAmount` is already the base bet** — it does
not move when ante is toggled. Measured 2026-09-04: with ante ON the snapshot read `4.00` while the
server deducted `stake: 12.00` per spin. The first version of this reader "corrected" for ante by
dividing by the multiplier and turned a 4.00 bet into 1.33, capping the run at a third of what the
suite asked for. The bet display is the base; leave it alone.

None of this softens Rule 9: reading the real bet makes the multiple mean what it says, it does not
make a small multiple safe. `max_win_x: "10 * bet"` is still a cap that ends bonuses.

So a free-spin scenario must expect the bonus to be over at three points where it looks impossible:

| where | what it means |
|---|---|
| on the TRIGGER spin | the award is forfeited — there was never a bonus to play |
| on the FIRST free spin | the feature ended before the scenario could set anything up |
| on any later round | the drain ends early — the counter IS 0, but the bonus was shorter than the case may need |

None of these is a defect — it is the cap doing its job. Handle them, do not assert them away:

```python
if b.has_max_win or is_terminal(b):
    triggers_capped += 1
    await ctx.settle()          # back at base
    continue                    # ...and hunt another trigger
```

**Discard and retry, do not fail.** `freespin-zero-balance` loops: it hunts a trigger that is not
capped, plays one free spin, and if THAT caps it throws the attempt away and hunts again. It gives
up only when the spin budget runs out, and then says how many attempts it discarded
(`facts.attempts_discarded`) so a cap that is too tight for the paytable is visible rather than
looking like "the feature never triggers".

**A finished bonus satisfies the obvious asserts.** `terminal_reached` and
`{terminal.free_spins} == 0` are both true of a bonus that ended *before* the thing under test ever
happened. Add a fact that proves the behaviour actually occurred:

```yaml
- stream: "{facts.played_after_zero} == True"   # rounds really did play AFTER the wallet hit zero
- stream: "{facts.first_fs_capped} == False"    # ...and the graded attempt was not a truncated one
```

If the discards are frequent, raise the run's `max_win_x` — the cap is too tight for the paytable's
award size, and see Rule 6 for lowering the award instead.

## Rule 10 — the bridge tells you what the game THINKS; only the AI tells you what the player SEES

A `stream:` assert reads the game's own account of itself: the BET response, and the getters behind
`getSnapshot()` / `getHistoryRows()` / `canDo()`. That is data the game **reports**. The screen is
rendered from it by a separate layer, and that layer can be wrong on its own:

* `isAutoPlay: false` while the auto button still shows a spinning counter;
* a correct `balance` in the response while the on-screen label still reads the old figure;
* `visiblePopups: []` while the popup is still painted mid-fade;
* the right `payout` in the stream while the win meter shows nothing, or the wrong amount;
* a round recorded in history with the right numbers, rendered into a blank or overlapping row.

Every one of those is a REAL defect, and every one of them passes a suite that only reads the
bridge. The response was right; the player saw something else. **A case that asserts only on the
stream is testing the server and the bridge, not the game.**

### So every case carries an AI assert

`video:` (or `image:` where one frame answers it — Rule: prefer video) is **required**, not a
garnish. If a case has no `video:`/`image:` block, the reason must be written next to it and must be
that *there is nothing to see* — pure response arithmetic with no screen consequence.

### The division of labour

Do not replace one with the other. They answer different questions, and each is the stronger tool
for its own:

| question | assert | why |
|---|---|---|
| is the number exactly right? | `stream:` | the response carries it; an AI reads a downsampled montage |
| did the thing NOT happen? | `stream:` | absence over a watch window is evidence; an AI cannot prove a negative from frames |
| does the player SEE it? | `video:` | nothing in the response describes pixels |
| is the UI intact — no blank panel, stale label, overlap, clipping? | `video:` | the bridge has no opinion about layout |

The pattern is **both**: the stream pins the mechanism exactly, the clip proves the screen agrees.

```yaml
# CR_022 — the player stops auto-play
- stream: "{facts.auto_stopped} == True"          # it really stopped: no bet arrived in the watch
- stream: "{facts.extra_round_after_stop} == False"
- video:                                          # ...and the screen says so too
    prompt: >-
      Auto-play was running and the player pressed Stop. After that, does the screen show auto
      is NO LONGER running — no auto/remaining-spins counter, the spin button back to its normal
      idle look, and no further spin starting on its own?
```

### Watch for this failure mode

The temptation is to drop a `video:` because a `stream:` fact "already proves it, and more
strictly". It proves the *mechanism* more strictly. It says nothing about the render. Two cases in
this repo drifted apart on exactly that split before it was written down: Chaos's CR_020 / CR_022 /
CR_023 asserted only the stream (mechanics proven, UI never looked at), while Mystic's Mah_015 /
Mah_017 / Mah_018 asserted only the video (UI judged, mechanics assumed). Both halves are cheap —
the clip is already recorded, driving nothing extra — so there is no reason to own only one.

---

## Rule 11 — the wallet takes FRACTIONAL amounts; never round a balance to whole units

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

## Rule 12 — you cannot drain the wallet during a bonus; arrive broke instead

The server refuses to reduce the balance while a bonus is open, and for a while after recent play.
It reports `Insufficient balance` **against a balance that has not moved** — measured across 45
retries over 90s with the client already torn down. Retrying does not help, and there is a hard
floor besides: the wallet never reaches exactly 0.00, only 0.01.

So a test that needs a broke player must not take the money away. **Give the player exactly one bet
and let them spend it**, the way a real player goes broke:

```python
await ctx.set_balance(stake)      # exactly one bet -- lands with no bonus open
await ctx.reload_and_start()      # the client caches the balance; it must re-read it
b = await ctx.base_spin()         # the stake is debited HERE, server-side -- now broke
```

The debit happens inside the spin, so there is no wallet call to be refused and no race with the
auto-playing bonus. `freespin-zero-balance` hunts this way, discarding an attempt that does not
trigger, that caps, **or whose trigger paid enough to leave the player able to bet again**.

Judge "broke" at the moment the bonus BEGAN, not later: free spins pay in, so the balance climbs as
soon as the bonus wins anything. Asserting on the reload figure fails a perfectly good run.

```yaml
- stream: "{facts.broke_at_trigger} == True"    # balance after the triggering spin, below one stake
```

And when a wallet call is unavoidable, widen the retry — `set_balance(x, attempts=10, pause_s=2.0)`
— but ONLY where nothing can play while it waits.

## Rule 13 — never end a drain on a COUNT; ends are read from the game

Two different loops get this wrong in the same way.

**A free-spin drain must not be bounded by the award.** A free spin can win MORE free spins, so the
counter is not monotonic — it jumps back up on a retrigger. Draining until `is_terminal` handles
that correctly, but the safety cap does not: falling out of `for _ in range(cap)` used to return the
rounds as though the bonus had finished. Everything after that failed confusingly — `settle()` waits
on a spin button that cannot become ready, the run ends inside a live bonus, and the NEXT run's
teardown times out on `awaitFreespinRound`. **Fail loudly at the cap instead**, naming the retriggers
and the spins still owed.

**An out-of-money drain must not be bounded by the funding.** Funding N bets does not mean the wallet
is empty after N spins: an auto spin can win, and a trigger pays across a whole bonus. Read the
server's own balance after every round and stop when it can no longer cover a bet:

```python
final_balance = b.balance          # stake debited, payout credited
if final_balance is not None and final_balance < bet:
    below_bet = True               # the real end -- whenever it happens
    break
```

Then assert it, so "auto stopped" is known to be about MONEY and not something else:

```yaml
- stream: "{facts.balance_below_bet} == True"
```

**Corollary — for auto tests, prefer the INFINITE auto count.** Pick
`auto_count, i=len(GAME.AUTO_COUNTS) - 1` (`AUTO_COUNTS = (10, 20, 30, 50, None)`; `None` is
Infinity) whenever the subject is "auto stops because of X". A finite count can run out on its own
before X happens — and then the test passes for the wrong reason, or fails because the count was
smaller than the spins it needed. Choose a finite count only when the count IS the subject
(`auto-spin-count`) or when the test needs auto to END so the game goes idle
(`reconnect-during-auto` waits for a spinnable base, which never comes under Infinity).

## Rule 14 — never repair the game before the evidence is captured

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

## Rule 15 — a fact the client cannot clear is not a defect; assert what the player can see

When the balance runs out, Chaos stays in the auto STATE and raises the out-of-money popup. The popup
is what prevents any further bet; `isAutoPlay` only clears once it is dealt with. Verified live: no
further round was wagered, the screen was plainly idle, and the flag stayed true.

So `auto-until-empty` asserts that nothing more was wagered and that the money really ran out — and
deliberately does **not** assert `auto_flag_after`, while `auto-stop-at-win` (no popup) still does.
Before asserting an internal flag, ask what the player can actually do: if the game cannot bet, the
behaviour is correct whatever the flag says.

## Rule 16 — one clip, ONE prompt: share a video call across the cases that grade it

Every `video:` assert is an upload plus an analysis. When several cases in a run grade the **same**
clip — none of them narrowing it with `at:` — writing one `video:` block per case sends that clip
once per case. `turbo-modes` did this: four cases, four uploads, four analyses of identical footage,
for prompts that differed only in which moment they asked about.

Put a **run-level `video:` block with `expected_by_case`** instead. It is analysed ONCE and graded
against each case's own expected subset, so a failure still lands on the case it belongs to
(`build_cases`, `scripts/run_scenario.py`). Four calls became one, and CR_029 still failed on its own
field while the other three passed.

```yaml
  - run:
      scenario: turbo-modes
      video:                       # RUN level -- sibling of `scenario:` and `cases:`
        timestamp: true            # the ACTION TIMELINE is how the model tells the steps apart
        high_fps: true
        prompt: >-
          ONE recording of four behaviours, in this order -- (A) ... (B) ... (C) ... (D) ...
          Answer each of the following about the moment it names, independently:
          (1) turbo_faster: ...  (2) button_state_changed: ...  (3) turbo_on_during_mouse_hold: ...
        output_schema: { turbo_faster: { type: boolean }, ... }
        expected_by_case:
          CR_026: { turbo_faster: true, button_state_changed: true, confidence: medium }
          CR_027: { turbo_on_during_mouse_hold: true, confidence: low }
      cases:
        - id: CR_026
          assert:
            - stream: "{facts.speed_toggled} == True"
            # The screen half is the run-level `video:` above.
```

Three things make or break the merge:

* **The prompt must describe the WHOLE clip, in order, and say the answers are independent.** A
  merged prompt is no longer "look at this moment" — it is "here is a sequence, answer about the
  part each question names". Mark the steps in the scenario (`ctx.mark`) and set `timestamp: true`
  so the model can line the questions up with them.
* **Field names must not collide.** Both hold cases asked `turbo_on_during_hold`; in one shared
  schema that is a single answer covering two different gestures. Rename them apart
  (`turbo_on_during_mouse_hold` / `turbo_on_during_key_hold`) or the cases cannot disagree.
* **Per-case strictness survives.** Each case is graded only against its own fields, and
  `confidence` is a MINIMUM, not an exact value (expected `medium` passes on medium or high). So a
  clearly-visible case can still demand a firmer read than a genuinely hard-to-see one off the same
  answer — merging does not level them down.

**When NOT to merge.** Keep separate `video:` asserts where the cases grade genuinely different
footage — that is what `at:` is for, and a narrower clip is cheaper AND easier to judge than the
whole recording (Rule 10's point about scoping). Merge only what already shares one clip.

`image:` has the same run-level `expected_by_case` form, for the same reason.

## Rule 17 — a run that NEEDS a free-spin trigger FORCES one; it never hunts

If a case cannot be graded without a bonus, the run declares a forced paytable. It does not spin the
base game hoping for one.

```yaml
- run:
    scenario: fs-complete
    paytable: { ref: chaos-fs-easy, max_win_x: 500 }   # forced: a bonus in a spin or two
```

**Hunting is not the cautious option, it is the expensive one.** A budget like
`params: { max_base: 300 }` is 300 real spins at the live stake, paid before the footage the run
exists for even starts. And the failure mode is quiet: a run that spends the budget without
triggering returns **unreached**, so every case under it reports `inconclusive` — excluded from the
fail counts, green-adjacent in the summary, and proving nothing. A suite full of those looks like
coverage and is not.

**Delete the budget, do not tune it.** The scenario keeps its own default (`fs-complete`: 400) as an
unreachable backstop for the case where the forced paytable did not take. Raising a hunt budget to
make a trigger more likely is the anti-pattern this rule exists to stop — forcing is both cheaper
and deterministic.

**Which fixture:** the smallest trigger that reaches the feature (Rule 6). For Chaos that is
`chaos-fs-easy` — FreeSpin! at 30% on base reels 0/1/2, the other two zeroed, `FreeSpinGame` left
alone so the bonus does not retrigger itself.

### Pair the forced paytable with a cap — and default it to 20x the bet

**A bonus that can FEED ITSELF has no natural end, and the cap is the only thing that bounds it.**
Three shapes do that, and any run that forces one MUST carry a cap:

| the bonus extends itself by | example |
|---|---|
| retriggering from inside free spins | a forced `FreeSpinGame` section keeps landing scatters, so the counter goes back up faster than it comes down |
| a symbol that RESETS the counter | Hold & Spin — every new Coin puts the respins back to 3 (`play_out_holdspin` raises at its cap for exactly this) |
| a bought bonus on an easy paytable | the purchase lands in the same runaway |

Without a cap such a run is bounded only by the scenario's spin budget: it plays for hundreds of
rounds, produces a clip nobody can grade, and ends by raising at the drain cap. With one, the server
truncates at the ceiling and sets `hasReachedMaxWin`, which the framework already treats as terminal
(Rule 9) — the round ends cleanly and clears the counter, so `{terminal.free_spins} == 0` and
`terminal_reached` both hold.

**The default is `max_win_x: 20`** — 20x the live base bet. It is long enough that a counter visibly
counts down and a multiplier gets sampled more than once, and short enough to bound a runaway and
keep the clip watchable. `10 * bet` was the old default and truncates too often: it regularly leaves
a one-round bonus, which passes on the stream and shows almost nothing on screen.

**Raise it when the SIZE of the feature is the subject, not its stake.** A Hold & Spin whose case is
"every respin plays and the total pays out", a max-win case, an award-value case — those are about
how big or how long the feature gets, and a tight ceiling changes the answer. Those runs keep a
LOOSE cap (the Zeus/Dragon Hold & Spin suites use `500`), which still bounds a runaway while leaving
the feature's own arithmetic intact.

```yaml
paytable: { ref: chaos-fs-easy, max_win_x: 20 }    # default: bounds a retriggering bonus
paytable: { ref: fs-trigger3-countdown, max_win_x: 500 }   # the feature's LENGTH is the case
```

⚠ **A cap is never the thing under test.** It shapes the run; the asserts are about the stake, the
counter or the money. If a case's verdict changes when you move the cap, the case is measuring the
cap.

**What a tight cap costs is LENGTH, and that is the thing to guard.** The cap can land on the
trigger spin or on free spin 1, which ends the feature before the case can observe anything:

| case wants to see | a 1-round bonus gives it |
|---|---|
| a free-spin counter COUNTING DOWN | one number, never decrementing |
| a multiplier sampled across the bonus | a single sample |
| "the bonus ran and then ended" | passes on the stream, shows almost nothing on screen |

So a run at a tight cap asserts the LENGTH it needs, rather than trusting it:

```yaml
- stream: "{facts.free_spins_played} >= 2"   # a countdown needs at least two rounds to be visible
```

A failure there is not a product bug — it says the cap is too tight for this paytable's award, and
the fix is to raise the multiple. Shape the award with the paytable, bound the runaway with the cap,
and let the assert tell you when the two are mismatched.

## Rule 18 — screenfuls of ONE surface go to the AI together (`combine: true`)

An `image:` assert grades **one AI call per captured still**, and that is right when the stills are
different surfaces: the base screen, the bet list, Settings, each in its own language. Each answer
belongs to one screen, and a bad screen cannot hide inside an aggregate.

It is wrong when the stills are **screenfuls of a single scrolling surface** — a help document, a
tutorial guide, a long history list — because then no single still can answer the question.

### The failure it prevents

Chaos's Dynamic Game Help is one document, shot once per scroll step. CR_107 asks whether the RTP
section is filled in. Graded per still:

| still | "does this page show RTP information?" | verdict |
|---|---|---|
| help page 1 (Max Win) | no — honestly | **fails** `rtp_on_page: true` |
| help page 2 (Game Limits) | no — honestly | **fails** |
| help page 4 (RTP) | yes | passes |

Four of five shots fail an assert the document satisfies. The model is not wrong; the question was
put to the wrong thing. Worse is what it forces on the prompt: to survive per-still grading it has
to be written conditionally — *"if this page shows RTP, answer about it; if not, set `rtp_on_page`
false and leave the rest"* — and a conditional question **can never catch a section that is missing
altogether**, which is exactly what the case exists to check. The assert quietly degrades from
"the RTP section is present and filled" to "wherever RTP appears, it is filled".

### The fix

```yaml
image:
  combine: true          # every still of this surface -> ONE call
  prompt: >-
    These screenshots are the Game Help, captured as it was scrolled from top to bottom. Read them
    as ONE document and answer about the whole of it — a section counts as present if it appears on
    any screenful. ...
```

The framework sends the first still plus the rest as `extra_images` (every provider already accepts
them) and prefixes the prompt with the framing: *N screenshots of ONE screen, in order, consecutive
parts of the same surface; if something appears on any of them it is present; only say it is absent
if it is on none of them.* One answer, graded per case exactly as `expected_by_case` does elsewhere.

`sections_present` becomes a real inventory of the document, `buy_bonus_rtp_present: false` becomes a
statement about the whole guide rather than about page 3, and CR_070 could finally assert
`paytable_on_page: true` — it had been reduced to expecting nothing but `confidence`, because no
nominated page was guaranteed to hold the paytable.

### Which to use

| the stills are | use | why |
|---|---|---|
| different surfaces or different languages | the default, one call per still | a bad screen must not average out against good ones |
| screenfuls of one scrolling surface | `combine: true` | "is X anywhere in this?" is unanswerable from one screenful |

It is opt-in for that reason: combining a language tour would throw away the per-screen verdict that
makes "Burmese base screen has an English label" traceable to a screen.

**It is also cheaper**, but that is the side effect, not the point. Chaos's help went from 5 calls to
1 and the tutorial from 5 to 1 — the reason to do it is that the questions became answerable.

**Orthogonal to Rule 16.** That rule shares one analysis across CASES; this one shares it across
SHOTS. They compose: the Game Help run uses both, so three cases over five screenfuls cost a single
call.

## Rule 19 — a field that says NOTHING is not a value; never do arithmetic on a sentinel

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

### The fix is to carry, not to skip

`conservation` now treats a non-positive balance as *not reported*: it carries that round's stake and
payout forward and compares against the last figure the server actually stated. A real wallet cannot
be negative, so `-1` can only mean "nothing to say".

**Carrying is not the same as skipping**, and the difference is the whole rule. Skipping would let a
bonus pay whatever it liked in the unreported rounds; carrying means the next round that DOES report a
balance has to account for every stake and payout in between. The check stays as strong, and it now
answers the question the case is really asking: *did the feature's money add up end to end?*

### What to do in a new scenario

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

## Rule 20 — a BO write is not live when the API answers; wait for it, then PROVE it arrived

> **`200 OK` from the back office means BO STORED the change, not that the game server has it.**
> BO publishes the change on a **Kafka event** and the game server applies it when the event is
> delivered. Nothing in the HTTP response is a promise about that. A scenario that writes BO and
> reopens the game in the same breath can load the OLD configuration.

This bites on **any out-of-band write the NEXT LOAD has to see** — the agent's denomination, its
paytable, a compliance flag. The denomination is where it was measured (Dragon VIP,
`bonus-resume-denomination-swap`), and it is the same lag already recorded for
`bo_paytable_update`, which needed a further reload before the forced paytable took effect.

### Why this one is worse than a flaky timeout

A late denomination does not look like an error. It looks like a PASS:

| | what the case wants to prove | what a stale read produces |
|---|---|---|
| DV-71 | the resumed bonus keeps the OLD bet | old bet on screen — **greens for the wrong reason** |
| DV-73 | after the bonus, the bet is the NEW ladder's default | old bet on screen — reds a correct game |

So the failure mode is not "the run errored". It is a **green on evidence the run never actually
created**, which is exactly what this document exists to prevent. A propagation lag is
indistinguishable from the behaviour under test unless the scenario checks.

### The rule, in three parts

1. **Write BO with the game CLOSED** (Rule 8's reasoning: the client must not be playing rounds
   against a configuration that is mid-change).
2. **Wait before reopening.** `ctx.set_automation_denomination(settle_s=5.0)` sleeps after the
   write; 5 s is the figure in use for Dragon VIP dev. It is a *settling* wait, not a retry loop,
   and it is deliberately a named argument so a scenario can state it rather than inherit it.
3. **PROVE the new configuration is the one the client loaded.** The wait is a guess; the check is
   evidence. Read the value back from the reopened client and publish it as a fact:

```python
"client_ladder": client_ladder,                       # what the reopened client offers
"client_on_new_ladder": bool(new_ladder) and client_ladder == new_ladder,
```

A case then **gates on that fact** before grading anything else:

```yaml
- stream: "{facts.swap_applied} == True"          # BO accepted the write
- stream: "{facts.client_on_new_ladder} == True"  # ...and the GAME is running it
```

Without the second line the first proves only that BO is happy, and every assert after it is
describing a session that never saw the change.

### What a stale read means, and what it does not

`client_on_new_ladder == False` after the wait is **a propagation problem, not a game defect**.
Raise the wait, or check the event pipeline — do not read it as "the game ignored the new
denomination", and do not chase it by loosening the case. If the lag turns out to vary, the honest
shape is retry-then-verify (reopen once more after a further wait) rather than a longer blind sleep:
a sleep long enough for the worst case is dead time on every run, and still not a guarantee.

### Do not "fix" it by asserting the old value

The tempting patch, once a stale read is seen, is to accept either ladder. That deletes the case:
the whole point of swapping to the automation ladder (11, 22, 33 — values no real ladder uses) is
that ONE number says which configuration the client loaded. Keep the assert exact and make the
propagation visible instead.

## Rule 21 — a round's clip ends when the NEXT round starts, which is before its presentation plays

> **`at: trigger` does not contain the trigger's animation.** A round's video window runs from that
> round's start to the next round's start, and an AUTO-played round's start is recorded the moment
> the framework begins WAITING for it — immediately after the previous response. The response lands
> before the award banner, the bonus teaser or the big-win celebration plays, so a single-round clip
> ends mid-animation. When the claim is about a PRESENTATION, span one round further.

This is `DriveContext.windows()`:

```python
def windows(self) -> list:
    """Round i spans from its own spin start to the next round's start (the last round runs to
    now, i.e. through settle)."""
```

For two CLICKED spins that is right: round *i* runs from its click to round *i+1*'s click, and
everything the client drew in between — the win count-up, the celebration — is inside it. The
trouble is the auto-played round, where `free_spin()` records `t0` and *then* awaits:

```python
async def free_spin(self, phase: str = "freespin") -> Bet:
    t0 = self.now_s()                    # <- recorded BEFORE the response, and the drain calls
    b = Bet.parse(await await_freespin_round(...))   #    this the instant the trigger lands
```

A drain calls `free_spin()` the instant the triggering response arrives, so free spin 1's "start" is
effectively the trigger's response time. The trigger's window therefore closes at its own response —
before a single frame of the award animation.

### What it looks like when you get it wrong

The run is green, the suite validates, and the clip is two seconds of reels stopping. Grandma Pets'
Buy Bonus is the worked example: the purchase response carries no grid at all, the client then plays
a fake trigger spin and announces 15 free spins, and `at: trigger` captured none of it. The AI was
asked "does the game announce how many free spins were won?" about footage that ended before the
announcement — and answering **no** to that is correct, so the case reds and the game is innocent.

### The fix

Span to the round after the one the presentation belongs to, and tell the AI the extra round is
framing:

```yaml
video:
  at: [trigger, "rounds[1]"]            # the purchase AND the first free spin
  prompt: >-
    ... Judge ONLY the fake spin and the announcement — the free spin at the end of the clip is
    there so the announcement is fully visible, and is not the subject.
```

The last round of a scenario needs none of this: its window runs to `now`, so it already covers
settle. `at: terminal` and `at: [trigger, terminal]` are safe for the same reason.

### Two traps in writing the span

* **Quote the index.** `at: [trigger, rounds[1]]` is a YAML **parse error** — inside a flow
  sequence, the `[` opens a nested list. It must be `at: [trigger, "rounds[1]"]`.
* **A span is exactly TWO targets.** `resolve_window` takes `at[0]`'s start and `at[1]`'s end; a
  three-element list is not a wider span. It falls through to `at in result.marks`, which raises
  `TypeError: unhashable type: 'list'` **at run time**, after the bonus has already played.
  `validate_suites.py` now catches this before a run starts.

### Which claims this applies to

Anything the client draws AFTER the response: free-spin award banners, Buy Bonus teasers, big-win
celebrations, a counter that counts up, a popup that opens on a win. A claim about the REELS of that
round is unaffected — those are drawn before the response is processed.

## Quick checklist for a new scenario

- [ ] Every `base_spin()` / `observe_bet()` result is tested with `is_freespin_trigger` — **or** the
      scenario documents why a trigger is impossible here (e.g. it places no bet).
- [ ] That test is inside every loop that spins, not only after the loop.
- [ ] Bonus drains terminate on `is_terminal`, capped, never on `freeSpins == 0` alone.
- [ ] `awaitFreespinRound` is only reached when a response said a bonus is open.
- [ ] The scenario returns with the game spinnable.
- [ ] Any "…and then it stops" claim goes through `confirm_stopped()` — watched, then held.
- [ ] Facts that count rounds count the right ones — `base_rounds_since()` where the claim is "one
      input caused one spin", so a drained bonus does not inflate the number.
- [ ] A forced paytable reaches the feature with the SMALLEST outcome that works (Rule 6): a
      ~30% probability pass, NOT a 100% force, wherever the outcome scales with the symbol COUNT —
      weights are per cell, so a forced reel fills every row and multiplies a ways result. Accept
      the short hunt, and force only the section you need so the bonus still ends.
- [ ] A scenario that moves the wallet declares `teardown: restore-balance` (Rule 7) —
      never `deposit-10000`, which leaves the balance drifting.
- [ ] Anything done mid-bonus calls `ctx.stop_client()` first (Rule 8) — the feature
      auto-plays, so an API call races it — and comes back with
      `reload_and_start(url=ctx.url)`, not a bare reload.
- [ ] A free-spin scenario handles `has_max_win` on the trigger AND on the first free
      spin (Rule 9) by discarding the attempt and hunting again, not by failing.
- [ ] The case asserts a fact proving the behaviour HAPPENED — a bonus that ended early
      passes `terminal_reached` and `{terminal.free_spins} == 0` on its own.
- [ ] EVERY case has a `video:` (or `image:`) assert (Rule 10) — the bridge reports what the
      game thinks, not what is on screen. No AI assert means the reason is written down and
      is "there is nothing to see".
- [ ] ...and it does not REPLACE the `stream:` asserts: exact values and "it did not happen"
      stay on the stream, the clip proves the screen agrees.
- [ ] A wallet target is the EXACT figure wanted (Rule 11) — never rounded to whole units.
- [ ] A test needing a broke player FUNDS ONE BET AND SPENDS IT (Rule 12); it does not try to
      drain the wallet during a bonus, which the server refuses.
- [ ] A run whose forced bonus can EXTEND ITSELF (retrigger in free spins, a counter-resetting
      symbol, a bought bonus) carries a cap, defaulting to `max_win_x: 20` (Rule 17) — and a
      LOOSER one where the feature's own size or length is the subject.
- [ ] A run at a tight `max_win_x` asserts the bonus LENGTH it needs (Rule 9/17) — a cap can end
      the feature on the trigger or on free spin 1, which passes the obvious asserts showing nothing.
- [ ] A run that NEEDS a bonus FORCES one with a paytable (Rule 17) and carries no hunt budget —
      hunting costs real spins and ends in `inconclusive`, which reads like coverage and is not.
- [ ] No loop ends on a COUNT (Rule 13): a free-spin drain fails loudly at its cap, and an
      out-of-money drain stops on the server balance falling below one bet.
- [ ] An auto test whose subject is "auto stops because of X" uses the INFINITE auto count
      (Rule 13) — a finite one can run out on its own and pass for the wrong reason.
- [ ] Nothing repairs the game before the asserts are graded (Rule 14) — a tidy-up click inside
      the scenario ends up in the clip and the AI grades OUR fix, not the game's behaviour.
- [ ] An internal flag is asserted only where the player could act on it (Rule 15).
- [ ] No assert does arithmetic on a field the server uses to say NOTHING (Rule 19) — a `-1`
      balance on a bonus round means "not reported", and reading it literally reds a feature
      whose money is exactly right. Expose the raw value as a fact and gate the case on
      whether the field was answerable.
- [ ] A `video:` claim about a PRESENTATION (an award banner, a teaser, a celebration) spans
      one round further than the round it belongs to (Rule 21) — `at: trigger` ends at the
      response, before the animation plays, and the AI correctly answers "no".
- [ ] Cases in one run that grade the SAME clip share ONE run-level `video:` block with
      `expected_by_case` (Rule 16) — not one `video:` assert each, which re-analyses the same
      footage per case. Merged prompts describe the whole clip and use distinct field names.
- [ ] An `image:` assert over screenfuls of ONE scrolling surface sets `combine: true` (Rule 18) —
      per-still grading cannot answer "is this section anywhere in the document?", and the
      conditional prompt it forces can never catch a section that is missing entirely.
- [ ] A scenario that writes BO mid-run WAITS before reopening and then asserts the game
      actually loaded the change (Rule 20) — BO answers before the game server has the
      event, and a stale read greens the very claim the case is making.
- [ ] The self-check above prints nothing.
