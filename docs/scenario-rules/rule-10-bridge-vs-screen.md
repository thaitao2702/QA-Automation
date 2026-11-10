# Rule 10 — the bridge tells you what the game THINKS; only the AI tells you what the player SEES

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

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

## So every case carries an AI assert

`video:` (or `image:` where one frame answers it — Rule: prefer video) is **required**, not a
garnish. If a case has no `video:`/`image:` block, the reason must be written next to it and must be
that *there is nothing to see* — pure response arithmetic with no screen consequence.

## The division of labour

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

## Watch for this failure mode

The temptation is to drop a `video:` because a `stream:` fact "already proves it, and more
strictly". It proves the *mechanism* more strictly. It says nothing about the render. Two cases in
this repo drifted apart on exactly that split before it was written down: Chaos's CR_020 / CR_022 /
CR_023 asserted only the stream (mechanics proven, UI never looked at), while Mystic's Mah_015 /
Mah_017 / Mah_018 asserted only the video (UI judged, mechanics assumed). Both halves are cheap —
the clip is already recorded, driving nothing extra — so there is no reason to own only one.
