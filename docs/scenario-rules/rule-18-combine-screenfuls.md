# Rule 18 — screenfuls of ONE surface go to the AI together (`combine: true`)

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

An `image:` assert grades **one AI call per captured still**, and that is right when the stills are
different surfaces: the base screen, the bet list, Settings, each in its own language. Each answer
belongs to one screen, and a bad screen cannot hide inside an aggregate.

It is wrong when the stills are **screenfuls of a single scrolling surface** — a help document, a
tutorial guide, a long history list — because then no single still can answer the question.

## The failure it prevents

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

## The fix

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

## Which to use

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
