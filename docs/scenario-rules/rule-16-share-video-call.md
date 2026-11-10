# Rule 16 — one clip, ONE prompt: share a video call across the cases that grade it

Part of the [scenario authoring rules](../scenario-authoring-rules.md) — the index lists every rule and when to read it.

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
