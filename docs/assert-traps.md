# Assert traps — real failures and the rules they taught

Asserts that looked right and failed on a real run — or passed when they should not have. Each trap
records what was asked, why it went wrong, and the rules that keep it from happening again. Read
this before writing an assert; add a new trap here whenever a run teaches one.

So far every trap is in a `video:` assert, for one root cause: a `stream:` assert reads the server
response, a `video:` assert reads the screen, and **they do not show the same number at the same
time**.

| Trap | Case | In one line |
|---|---|---|
| [Balance](#the-balance-trap-mah_008-a-real-failure) | Mah_008 | the stake comes off at spin start, so `res.balance` is passed through, not risen to |
| [Counting](#the-counting-trap-mah_043-a-real-failure) | Mah_043 | `cascades` counts grids evaluated (one more than visible events) — and never ask an AI to count |
| [Inverse pair](#the-inverse-pair-trap-zz-51-a-real-failure--12-false-alarms-in-one-run) | ZZ-51 | two fields asking about one property in opposite directions give confident nonsense |
| [Per-round window](#the-per-round-window-trap-zz-43-a-real-failure--a-4-millisecond-clip) | ZZ-43 | a round's window ends at the next response, so `at:` on a bonus round can be milliseconds long |
| [Generally](#generally-a-response-value-is-a-claim-about-the-server) | — | which response fields are ever on screen, and when |

---

## The balance trap (Mah_008, a real failure)

The prompt asked:

> "This clip is one winning base spin that paid 0.24, leaving a balance of 9972.84. Once the win
> finishes counting up, does the balance readout **rise to 9972.84**?"

The AI answered **no**, correctly, and explained itself:

> "The balance readout stays at 9,972.60 until it eventually updates to 9,972.24 at the very end.
> The specific intermediate value of 9,972.84 is never displayed."

Nothing was wrong with the game. **The bet is deducted at spin START**, so the readout passes
through four values in one clip:

| | value | when |
|---|---|---|
| `balance_before_spin` | 9973.20 | before the spin |
| `balance_while_spinning` | 9972.60 | stake deducted the moment the spin starts |
| `balance_after_win` (`res.balance`) | 9972.84 | win credited at the end of the count-up |
| `balance_after_next_bet` | 9972.24 | the next spin takes its own stake |

`res.balance` is the value the balance **passes through**, not one it rises to and holds — and on a
scenario that spins again (`base-cluster-win` plays a follow-up round), the next deduction lands
inside the same clip.

### The rules that follow

1. **State that the stake comes off first.** A prompt about a win must say the balance already reads
   `balance - payout` before the credit, or the AI is looking for a rise that never happens.
2. **Name the two endpoints of the step you are judging**, not just the destination:
   "does it go UP by {payout}, from {balance_while_spinning} to {balance_after_win}?"
3. **Say what to ignore.** If a later deduction or a new round is inside the clip, name it as
   expected: *"the balance ending near {balance_after_next_bet} is expected and is NOT a failure."*
4. **Judge one transition per question.** "Does the final balance equal X" invites the AI to grade
   whatever happens to be on screen last, which on a multi-round clip is a different round.
5. **Bind facts, not literals.** `{facts.balance_while_spinning}` is computed from the response the
   run actually got. A number typed into the prompt is wrong on the next run.

## The counting trap (Mah_043, a real failure)

Same shape, different field. The prompt said *"CASCADED 2 times"* and asked whether the cascade
"repeated more than once". The AI said no — and worked out why on its own:

> "Only one set of symbols clears and is replaced, so the cascade does not repeat more than once,
> despite the prompt's mention of 2 cascades (which likely includes the initial spin or the final
> non-winning evaluation)."

It was right. **`cascades` is `len(result)` — grids EVALUATED, not clear-and-drop events seen.** The
chain ends with a step that pays nothing, and that step clears nothing and drops nothing in:

| steps in `result` | what a viewer sees |
|---|---|
| 6 (5 paying, 1 not) | **5** clear-and-drop events |
| 2 (1 paying, 1 not) | **1** clear-and-drop event |

So `visible events == cascades - 1 == cluster_wins()`. Saying "cascaded {cascades} times" overstates
it by one, every time.

**And do not ask an AI to count at all.** Counting fast repeated events on video is what it is worst
at, and the response already knows the answer exactly. Put the count in `stream:` and leave the video
the question it can answer:

```yaml
- stream: "{facts.cascade_events} >= 1"        # the count -- exact, free
- video: ... "does that behaviour happen at all — not how many times"
```

## The inverse-pair trap (ZZ-51, a real failure — 12 false alarms in one run)

The first two traps are about asking for a value the screen never shows. This one is about asking
the same question **twice, in opposite directions**, and it produces confident nonsense.

ZZ-51's schema asked four questions with alternating polarity:

| # | field | question | passes when |
|---|---|---|---|
| 1 | `text_in_language` | is the text in the language named? | TRUE |
| 2 | `untranslated_text` | is any text in a DIFFERENT language? | FALSE |
| 3 | `text_fits` | does the text fit rather than overflowing? | TRUE |
| 4 | `text_clipped` | is any text clipped, or any marks missing? | FALSE |

Across 24 graded screens, **`text_fits` came back `true` 24 times** — it never varied, so that
assert could not fail — and **`text_clipped` came back `true` on 12**, every one of which carried
prose explicitly denying it:

> "All text fits within its designated areas without overflowing or clipping, and no characters or
> marks appear to be missing." — returned alongside `text_clipped: true`

The model read every screen correctly and mis-assigned one boolean. Twelve false alarms, zero real
findings, and half the case was vacuous into the bargain.

**Why (3) and (4) failed when (1) and (2) did not.** Fields 1 and 2 are *semantically distinct
questions* — "is it the named language" and "is some OTHER language present" — so the model reasons
about each separately, and it got even the subtle case right (a Burmese screen with `COIN` left in
English: `in_language: false`, `untranslated: true`). Fields 3 and 4 are **one physical property
asked twice**. Having concluded "the layout is fine", the model must emit `true` then `false` back
to back for what it experiences as the same question — and it does not flip.

### The rules that follow

1. **Never let two fields cover one property.** Not "does it fit" *and* "is it clipped". Pick
   distinct defects: `text_overflows` (a boundary), `text_collides` (two elements), `glyphs_clipped`
   (a character cut at the line), `diacritics_intact` (marks above/below).
2. **Mixed polarity is fine; duplicated properties are not.** Chaos's CR_064/066/067 mix positive
   and negative fields freely and ran green across 18 screens — because no two of them ask about the
   same thing. The rule is about redundancy, not direction.
3. **Make every field name state the direction its question asks.** `text_overflows` asked as "does
   any text overflow" is unambiguous; `text_fits` asked as "is any text clipped" is a trap.
4. **Split compound questions.** "is any text clipped, OR are any characters or marks missing" is
   two defects in one boolean, and on Burmese the second is the hard one. Three fields, three
   answers.
5. **Group the questions by topic** (LANGUAGE / LAYOUT / GLYPHS). It gives the model somewhere to
   put each judgement instead of carrying one impression across all of them.
6. **Read the `reason` before believing a boolean.** The contradiction was visible in the very first
   failing shot. A field whose prose disagrees with its value is a prompt bug, not a game defect —
   and a field that returns the same value on every shot is not testing anything.

---

## The per-round window trap (ZZ-43, a real failure — a 4-millisecond clip)

`at:` trims the clip to one round's window, and a round's window is **from its own response to the
NEXT round's response** — which measures when the SERVER answered, not how long the CLIENT
animated. On a feature whose rounds are pushed back to back, those are wildly different things.

Real timings from a bought Hold & Spin (video-relative seconds):

| round | t | window |
|---|---|---|
| #0 the purchase | 60.984 | **0.004 s** |
| #1 respin | 60.988 | 7.680 s |
| #2 respin | 68.668 | 4.858 s |
| #3 respin | 73.526 | 4.468 s |
| #4 terminal | 77.994 | 21.460 s → end of scenario |

The purchase response and the first respin arrived **4 milliseconds apart**, so `at: trigger`
produced a four-millisecond clip and the case was graded on essentially nothing. Meanwhile the
feature the case is about ran for 17 seconds and the whole scenario for 38.

### The rules that follow

1. **A case about a whole feature spans it: `at: [trigger, terminal]`.** The span runs from the
   first target's start to the second's end, and the LAST round's window runs to the end of the
   scenario — so it picks up the settle, the celebration and any hold as well.
2. **Never point a single-round `at:` at a bonus round.** Free spins and Hold & Spin respins are
   client-driven; the server can emit several within one animation. A single-round window is only
   meaningful where the framework CLICKED the round and waited for it (a base spin).
3. **If the clip looks empty, print the window before blaming the AI.** It is in `result.json` on
   the spec (`"at"` and `"window"`), and `events.jsonl` has the round timings that produced it.
   A four-millisecond window is obvious the moment you look and invisible if you do not.
4. **The tail belongs to the last round.** That is what makes "and then it returned to base" or
   "and then it stopped" gradeable — see Rule 4 in `docs/scenario-authoring-rules.md`. A scenario
   that ends at the last response leaves nothing after it to judge.

---

## Generally: a response value is a claim about the SERVER

Before putting one in a prompt, ask **when, if ever, that exact number is on screen**:

| response field | on screen? |
|---|---|
| `payout` | yes — in the WIN counter, during the count-up |
| `balance` | briefly, between the win credit and the next stake deduction |
| `stake` | yes — the bet display, stable |
| `freeSpins` | as a counter, and it may lag the response by an animation |
| `multiplierLevel` | as the multiplier badge, one value per cascade step |
| `cascades` (`len(result)`) | **no** — it counts grids evaluated; the viewer sees `cascades - 1` clear-and-drop events |

If the answer is "briefly" or "not directly", either describe the transition around it (rules 1-3)
or leave it to `stream:` and ask the video something it can actually see.
