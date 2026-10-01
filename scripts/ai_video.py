#!/usr/bin/env python3
"""
AI video-assert execution for the scenario runner.

For each pending `video:` assert on a case: trim `session.webm` to the assert's sub-clip
window, run the configured AI provider over it, and grade — never producing `red` (AI is
probabilistic; a mismatch is `orange` for a human, the API holds the truth).

Reuses the framework's existing, interface-compatible video helpers (Gemini / Claude / local),
selected by AI_PROVIDER exactly as suite_runner does. The helper builds its own montage; we
only trim the window first.

Two assert forms (produced by run_scenario.build_cases):
  check:  {kind:"check", check:"<claim>"}                 -> AI returns passed + confidence
  read:   {kind:"read",  read:"<label>", equals|near}     -> AI extracts the value; WE compare
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_CHECK_SCHEMA = {
    "passed": {"type": "boolean"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "reason": {"type": "string"},
}
_READ_SCHEMA = {
    "value": {"type": "string"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}


def make_video_provider(env: str):
    """Load env + build the configured video AI helper (gemini | claude | local). None if unavailable."""
    try:
        from framework.env_files import load_env_files
        load_env_files(env)
    except Exception:
        pass
    provider = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
    try:
        if provider == "claude":
            from framework.helpers.claude_ai import ClaudeVideoHelper
            return ClaudeVideoHelper()
        if provider == "local":
            ep = os.environ.get("LOCAL_AI_ENDPOINT", "").strip()
            if not ep:
                return None
            from framework.helpers.local_ai import LocalVideoHelper
            return LocalVideoHelper(ep)
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            return None
        from framework.helpers.gemini_video import GeminiVideoHelper
        return GeminiVideoHelper(
            key,
            model=os.environ.get("GEMINI_VIDEO_MODEL") or None,
            resolution=os.environ.get("GEMINI_VIDEO_RESOLUTION") or None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ! AI provider init failed: {e}")
        return None


def trim_clip(session_webm: Path, window: Optional[tuple], dest: Path) -> bool:
    """ffmpeg trim to [start, end] seconds (window None -> whole video). Stream-copy, then
    fall back to a re-encode if the copy produced nothing (odd keyframes)."""
    if not session_webm.exists():
        return False

    def _run(reencode: bool) -> bool:
        cmd = ["ffmpeg", "-y"]
        if window:
            start, end = window
            cmd += ["-ss", f"{max(0.0, float(start)):.3f}", "-to", f"{float(end):.3f}"]
        cmd += ["-i", str(session_webm), "-an"]
        cmd += (["-c:v", "libvpx-vp9", "-b:v", "1M"] if reencode else ["-c", "copy"])
        cmd.append(str(dest))     # the output path (was missing -> ffmpeg "no output file")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception as e:  # noqa: BLE001
            print(f"  ! ffmpeg trim failed: {e}")
            return False
        return r.returncode == 0 and dest.exists()

    return _run(reencode=False) or _run(reencode=True)


def _to_num(x) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").replace(" ", "").replace("$", ""))
    except Exception:  # noqa: BLE001
        return None


def _values_match(actual, expected) -> bool:
    """Type-aware equality for raw.yml-style expected fields (bool / number / string)."""
    if isinstance(expected, bool):
        return bool(actual) == expected
    an, en = _to_num(actual), _to_num(expected)
    if an is not None and en is not None:
        return abs(an - en) < 1e-6
    return str(actual).strip().lower() == str(expected).strip().lower()


def _compare(got: str, equals, near) -> tuple:
    """(matched, detail). Numeric where possible, else string equality."""
    if equals is not None:
        gn, en = _to_num(got), _to_num(equals)
        if gn is not None and en is not None:
            return abs(gn - en) < 1e-6, f"read {got!r} vs expected {equals!r}"
        return got.strip() == str(equals).strip(), f"read {got!r} vs expected {equals!r}"
    if near is not None:
        gn, vn = _to_num(got), _to_num(near.get("value"))
        tol = float(near.get("tol", 0) or 0)
        if gn is not None and vn is not None:
            return abs(gn - vn) <= tol, f"read {got!r} vs {near.get('value')!r} +/-{tol}"
        return False, f"non-numeric near-compare: {got!r}"
    return True, "no expected value to compare"


def run_video_assert(provider, clip: Path, spec: dict) -> dict:
    """Analyse one trimmed clip for one video spec. Returns grade fields to merge into the spec.
    Status is only ever pass / orange (AI never yields red). Three forms:
      check  -> boolean claim         (passed + confidence)
      read   -> extract a value; WE compare to equals/near
      schema -> raw.yml style: full prompt + output_schema + expected field map."""
    kind = spec["kind"]
    if kind == "check":
        prompt, schema = spec["check"], dict(_CHECK_SCHEMA)
    elif kind == "read":
        prompt = (f"Look at {spec['read']}. Report the EXACT value shown there as text — "
                  f"digits and separators exactly as displayed, nothing else.")
        schema = dict(_READ_SCHEMA)
    else:  # schema
        prompt, schema = spec["prompt"], dict(spec.get("output_schema") or {})

    # always ask the model for a prose explanation of WHY it answered as it did
    schema.setdefault("reason", {"type": "string"})
    prompt = (prompt + "\n\nAlso set \"reason\" to a brief prose explanation of what you saw in the "
              "video and why you answered each field that way — especially explain anything that "
              "does NOT match what was described.")
    if spec.get("timeline_hint"):
        prompt = prompt + spec["timeline_hint"]        # clip-relative action times for timestamp mode

    res = provider.analyse_video(clip, None, prompt, schema,
                                 reference=spec.get("reference"),
                                 high_fps=bool(spec.get("high_fps")),
                                 timestamp=bool(spec.get("timestamp")))
    tokens = [getattr(res, "input_tokens", 0), getattr(res, "output_tokens", 0)]
    if not res.success or not res.data:
        return {"status": "orange", "confidence": "low", "reason": "",
                "note": f"AI error: {res.reason}", "tokens": tokens}

    data = res.data
    conf = str(data.get("confidence", "")).lower()
    confident = conf in ("high", "medium")
    reason = str(data.get("reason", "")).strip()

    def _note(ok: bool, detail: str) -> str:
        # on a miss, lead with the prose reason so the report explains WHY
        if ok:
            return detail or reason or "matched"
        return (detail + (" - " + reason if reason else "")).strip(" -")

    if kind == "check":
        passed = bool(data.get("passed"))
        ok = passed and confident
        return {"status": "pass" if ok else "orange", "passed": passed, "confidence": conf,
                "reason": reason, "note": _note(ok, "" if passed else "claim not satisfied"),
                "tokens": tokens}

    if kind == "read":
        got = str(data.get("value", "")).strip()
        matched, detail = _compare(got, spec.get("equals"), spec.get("near"))
        ok = matched and confident
        return {"status": "pass" if ok else "orange", "read": got,
                "expected": spec.get("equals", spec.get("near")), "matched": matched,
                "confidence": conf, "reason": reason, "note": _note(ok, detail), "tokens": tokens}

    # schema (raw.yml style): every expected field must match what the model reported
    return grade_schema(data, spec.get("expected"), reason, tokens)


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _field_ok(key: str, actual, expected) -> bool:
    """Match one expected field. `confidence` is a QUALITY GATE, not an exact value: the model's
    self-rating passes when it MEETS OR EXCEEDS the expected level (expected medium -> medium or high
    is fine, only low fails). All other fields use exact type-aware equality."""
    if key == "confidence":
        return _CONF_RANK.get(str(actual).strip().lower(), -1) >= _CONF_RANK.get(str(expected).strip().lower(), 0)
    # A LIST is "any of these": more than one answer is correct (a walking wild kicks a letter out
    # of its cell but throws a cat or dog at the screen — either is a passing kick).
    if isinstance(expected, list):
        return any(_values_match(actual, e) for e in expected)
    return _values_match(actual, expected)


def grade_schema(data: dict, expected, reason: str, tokens) -> dict:
    """Grade an AI `data` object against an `expected` field map (schema form). Split out so a
    SHARED video call can be graded against several cases' expected subsets without re-calling."""
    expected = expected or {}
    mismatches = [f"{k}: got {data.get(k)!r}, expected {'at least ' if k == 'confidence' else ''}{v!r}"
                  for k, v in expected.items() if not _field_ok(k, data.get(k), v)]
    ok = not mismatches
    reason = (reason or "").strip()
    note = (reason or "matched") if ok else ("; ".join(mismatches) + (" - " + reason if reason else "")).strip(" -")
    return {"status": "pass" if ok else "orange", "data": data, "mismatches": mismatches,
            "reason": reason, "note": note, "tokens": tokens}


def recompute_verdict(case: dict) -> str:
    """After the AI pass, fold video AND image results into the case verdict. Hard tiers win."""
    if case["verdict"] in ("inconclusive", "red"):   # stream/reach already decided it
        return case["verdict"]
    ai = list(case.get("video", [])) + list(case.get("image", []))
    if any(v.get("status") == "pending" for v in ai):
        return "pending"
    if any(v.get("status") == "orange" for v in ai):
        return "orange"
    return "green"


def _timeline_hint(run_dir: Path, window) -> str:
    """For a timestamp-mode assert, build a clip-relative ACTION TIMELINE from this run's
    timeline.json (holds / key-holds / double-clicks / clicks / network events that fall in the
    clip window) so the model can line the burned-in timestamps up with when we acted."""
    if not window:
        return ""
    ws, we = float(window[0]), float(window[1])
    try:
        steps = json.loads((run_dir / "timeline.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    notes = []
    for s in steps:
        act = s.get("action")
        if act not in ("hold", "hold_key", "dblclick", "click", "key", "net", "scroll",
                       "second_session", "mark"):
            continue
        a = s.get("start_ms", 0) / 1000.0
        b = s.get("end_ms", s.get("start_ms", 0)) / 1000.0
        if b < ws or a > we:                                   # outside this clip
            continue
        ra, rb = max(0.0, a - ws), max(0.0, b - ws)
        desc = s.get("description", act)
        notes.append(f"{desc}: pressed at t={ra:.2f}s, released at t={rb:.2f}s"
                     if act in ("hold", "hold_key") else f"{desc}: at t={ra:.2f}s")
    if not notes:
        return ""
    return ("\n\nACTION TIMELINE (clip-relative seconds, matching the burned-in timestamps):\n- "
            + "\n- ".join(notes))


def make_image_provider(env: str):
    """Build the configured STILL-IMAGE AI helper (gemini | claude | local). None if unavailable.
    Mirrors make_video_provider — same env vars, the image-side helper of the same family."""
    try:
        from framework.env_files import load_env_files
        load_env_files(env)
    except Exception:  # noqa: BLE001
        pass
    provider = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
    try:
        if provider == "claude":
            from framework.helpers.claude_ai import ClaudeImageHelper
            return ClaudeImageHelper()
        if provider == "local":
            ep = os.environ.get("LOCAL_AI_ENDPOINT", "").strip()
            if not ep:
                return None
            from framework.helpers.local_ai import LocalImageHelper
            return LocalImageHelper(ep)
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            return None
        from framework.helpers.gemini import GeminiImageHelper
        return GeminiImageHelper(key)
    except Exception as e:  # noqa: BLE001
        print(f"  ! AI image provider init failed: {e}")
        return None


def run_image_assert(provider, shot: Path, spec: dict, label: str = "", extras=None) -> dict:
    """Analyse a still (or a SET of stills) against one image spec. Same schema-grading contract as
    the video path (grade_schema), so an image assert is orange-only too — never red.

    `{shot.label}` in the prompt is replaced with this shot's label. One assert covers many shots, so
    this is how a prompt says something different about each — e.g. a language sweep whose shots are
    labelled "th Thai", "my Burmese", ... tells the model which language THIS screen should be in.

    `extras` sends further stills alongside the first, in ONE call — for a SCROLLING surface, where
    the screenfuls are pages of a single document rather than separate surfaces. Asking each page in
    isolation cannot answer "is there an RTP section?": the honest answer for page 2 is "no" even
    when page 5 has one, so every page but one reports a failure the surface does not have."""
    prompt = spec["prompt"].replace("{shot.label}", label)
    schema = dict(spec.get("output_schema") or {})
    schema.setdefault("reason", {"type": "string"})
    if extras:
        prompt = (f"You are given {len(extras) + 1} screenshots of ONE screen, in order, captured as "
                  f"it was scrolled from top to bottom: {label}. They are consecutive parts of the "
                  f"SAME surface, not separate screens. Answer about the surface AS A WHOLE — if "
                  f"something appears on any one of them it is present; only say it is absent if it "
                  f"is on none of them.\n\n" + prompt)
    prompt = (prompt + "\n\nAlso set \"reason\" to a brief prose explanation of what you saw in the "
              "image and why you answered each field that way — especially explain anything that "
              "does NOT match what was described.")
    res = provider.analyse_image(shot, prompt, schema, extra_images=list(extras) if extras else None)
    tokens = [getattr(res, "input_tokens", 0), getattr(res, "output_tokens", 0)]
    if not res.success or not res.data:
        return {"status": "orange", "confidence": "low", "reason": "",
                "note": f"AI error: {res.reason}", "tokens": tokens}
    return grade_schema(res.data, spec.get("expected"), str(res.data.get("reason", "")).strip(), tokens)


def ai_workers(default: int = 4) -> int:
    """How many AI calls run at once: `AI_CONCURRENCY`, else 4 — or 1 for a `local` provider, which
    is usually one model on one GPU and gains nothing from queueing more requests at it."""
    raw = os.environ.get("AI_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 1 if os.environ.get("AI_PROVIDER", "gemini").strip().lower() == "local" else default


def _answer_of(fut) -> tuple:
    """(data, reason) of a finished shared call; (None, None) if it failed or raised."""
    try:
        r = fut.result()
    except Exception:  # noqa: BLE001
        return None, None
    return r.get("data"), r.get("reason")


def _own_result(fut) -> dict:
    """The result dict of a call THIS spec made (a copy: shared callers grade from the same one)."""
    try:
        return dict(fut.result())
    except Exception as e:  # noqa: BLE001 -- one bad call must not sink the pass
        return {"status": "orange", "confidence": "low", "reason": "",
                "note": f"AI error: {type(e).__name__}: {e}", "tokens": [0, 0]}


def _regrade(fut, expected) -> dict:
    """Grade a spec from ANOTHER spec's call (same group / same still): no second AI call."""
    data, reason = _answer_of(fut)
    if data is None:
        return {"status": "orange", "note": "shared AI call failed", "tokens": [0, 0]}
    return grade_schema(data, expected, reason, [0, 0])


def _plan_image(pool, provider, cases: list) -> list:
    """Submit every pending `image:` call of ONE run; return what `_finish_image` needs.

    ONE AI call per captured SHOT, so each surface is judged on its own full-resolution still
    rather than as one summary of six. Calls are keyed per (group, shot) so two cases sharing a
    tour (e.g. text-scaling and clipped-glyphs over the same screens) cost one call, not two."""
    calls: dict = {}                     # (group, file) -> future; scoped to this run
    plan = []
    for c in cases:
        for i, spec in enumerate(c.get("image", [])):
            if spec.get("status") != "pending":
                continue
            shots = spec.get("shots") or []
            if not shots:
                spec.update({"status": "orange", "note": "no screenshots were captured for this assert"})
                continue
            grp = spec.get("group") or f"{c['id']}-img{i}"
            if spec.get("combine"):
                # ONE call over every still of a scrolling surface (see run_image_assert).
                key = (grp, "__combined__")
                own = key not in calls
                if own:
                    calls[key] = pool.submit(run_image_assert, provider, Path(shots[0]["path"]), spec,
                                             ", ".join(sh["label"] for sh in shots),
                                             [Path(sh["path"]) for sh in shots[1:]])
                plan.append((spec, "combined", [(None, calls[key], own)]))
                continue
            parts = []
            for shot in shots:
                key = (grp, shot["file"])
                own = key not in calls
                if own:
                    calls[key] = pool.submit(run_image_assert, provider, Path(shot["path"]), spec,
                                             shot["label"])
                parts.append((shot, calls[key], own))
            plan.append((spec, "shots", parts))
    return plan


def _finish_image(plan: list, cases: list) -> None:
    """Fold the finished image calls into their specs. The assert passes only if EVERY shot passes,
    and the note names the ones that didn't — otherwise a single bad surface would disappear into
    an aggregate answer."""
    for spec, mode, parts in plan:
        results = [(_own_result(fut) if own else _regrade(fut, spec.get("expected")), shot)
                   for shot, fut, own in parts]
        if mode == "combined":
            r = results[0][0]
            shots = spec.get("shots") or []
            r["shot"] = f"{len(shots)} screen(s) graded together"
            r["files"] = [sh.get("file") for sh in shots]
            spec["shot_results"] = [r]
            spec["status"] = r["status"]
            spec["note"] = r.get("note") or ""
            spec["tokens"] = r.get("tokens", [0, 0])
            continue
        rows, failed = [], []
        for r, shot in results:
            r["shot"] = shot["label"]
            r["file"] = shot.get("file")         # so the report can show the still it graded
            rows.append(r)
            if r["status"] != "pass":
                failed.append(f"{shot['label']}: {r.get('note', '')}")
        spec["shot_results"] = rows
        spec["status"] = "pass" if not failed else "orange"
        spec["note"] = ("all %d screens matched" % len(rows)) if not failed else " | ".join(failed)
        spec["tokens"] = [sum(r.get("tokens", [0, 0])[0] for r in rows),
                          sum(r.get("tokens", [0, 0])[1] for r in rows)]
    for c in cases:
        c["verdict"] = recompute_verdict(c)


def _reference_for(game: str | None, spec: dict):
    """The symbol/UI reference pack primed into one AI call — the pictures that let a model say
    "the orange tabby", not "a cat".

    ⚠ Without it a prompt naming a symbol is asking the model to guess. Grandma Pets has THREE
    cats and TWO dogs; nothing in a video says which is P3 unless the manifest shows it.

    Scope comes from the assert (`assets:` / `asset_groups:` on the `video:` block); with neither,
    the game's whole manifest is sent. `None` when the game has no manifest, which is not an error
    — most prompts describe what they mean in words.
    """
    if not game:
        return None
    try:
        from framework.helpers.symbol_reference import (load_reference_selection,
                                                        load_symbol_reference)
        base = Path(__file__).resolve().parent.parent
        groups, items = spec.get("asset_groups"), spec.get("assets")
        # An EXPLICIT empty list means "send no pictures" — for a prompt that names no symbol at
        # all (a counter, a button state). Distinct from the field being absent, which means "send
        # the lot". Without the distinction every such call carries the whole manifest for nothing.
        if groups == [] and items in (None, []):
            return None
        if items == [] and groups is None:
            return None
        if groups or items:
            return load_reference_selection(game, base, groups, items)
        return load_symbol_reference(game, base)
    except Exception as e:  # noqa: BLE001
        print(f"  ! asset reference unavailable for {game}: {e}")
        return None


def _video_job(provider, session_webm: Path, clip: Path, spec: dict, game: str | None) -> dict:
    """Trim one assert's sub-clip and analyse it. Runs on a worker thread, so it only READS `spec`:
    the reference pack goes on a private copy (it is not JSON-serialisable and must stay out of
    result.json anyway)."""
    if not trim_clip(session_webm, spec.get("window"), clip):
        return {"status": "orange", "note": "clip trim failed"}
    call = dict(spec)
    call["reference"] = _reference_for(game, spec)
    return run_video_assert(provider, clip, call)


def _plan_video(pool, provider, session_webm: Path, run_dir: Path, cases: list,
                game: str | None) -> list:
    """Submit every pending `video:` call of ONE run; return the plan `_finish_video` grades.

    Plan entries: `(spec, "one", [(None, future, own)])` for a single clip, and
    `(spec, "each", [(window, future, own), ...])` for an `each:` spec — one clip per round. A spec
    in a `group` already submitted makes no call of its own: it is graded from that call's answer
    (per clip, for `each`)."""
    clips = run_dir / "_clips"
    clips.mkdir(exist_ok=True)
    calls: dict = {}                     # (group, clip index) -> future; scoped to this run
    plan = []
    for c in cases:
        for i, spec in enumerate(c.get("video", [])):
            if spec.get("status") != "pending":
                continue
            grp = spec.get("group")
            if "each" in spec:
                parts = []
                for k, w in enumerate(spec.get("windows") or []):
                    key = (grp, k) if grp is not None else None
                    if key is not None and key in calls:
                        fut, clip_name = calls[key]
                        parts.append((dict(w, clip=clip_name), fut, False))
                        continue
                    job = dict(spec, window=w["window"])
                    if spec.get("timestamp"):
                        job["timeline_hint"] = _timeline_hint(run_dir, w["window"])
                    # named after the FIRST case that makes the call; sharers link to the same file
                    clip_name = f"{c['id']}-{i}-r{w['seq']}.webm"
                    fut = pool.submit(_video_job, provider, session_webm, clips / clip_name,
                                      job, game)
                    if key is not None:
                        calls[key] = (fut, clip_name)
                    parts.append((dict(w, clip=clip_name), fut, True))
                plan.append((spec, "each", parts))
                continue
            if spec.get("timestamp") and "timeline_hint" not in spec:   # annotate action times once
                spec["timeline_hint"] = _timeline_hint(run_dir, spec.get("window"))
            key = (grp, 0) if grp is not None else None
            if key is not None and key in calls:
                plan.append((spec, "one", [(None, calls[key][0], False)]))
                continue
            fut = pool.submit(_video_job, provider, session_webm, clips / f"{c['id']}-{i}.webm",
                              spec, game)
            if key is not None:
                calls[key] = (fut, None)
            plan.append((spec, "one", [(None, fut, True)]))
    return plan


def _finish_video(plan: list, cases: list) -> None:
    for spec, mode, parts in plan:
        if mode == "one":
            _, fut, own = parts[0]
            spec.update(_own_result(fut) if own else _regrade(fut, spec.get("expected")))
            continue
        # `each`: every clip graded on its own; the assert passes only if EVERY clip passes, and
        # the note names the rounds that did not — the reason to split the recording at all.
        if not parts:
            spec.update({"status": "orange", "clip_results": [], "tokens": [0, 0],
                         "note": f"no round of this kind happened (`{spec.get('each')}` is empty)"
                                 " — nothing to grade"})
            continue
        rows, failed = [], []
        for w, fut, own in parts:
            r = _own_result(fut) if own else _regrade(fut, spec.get("expected"))
            r["seq"], r["window"], r["clip"] = w["seq"], w["window"], w.get("clip")
            rows.append(r)
            if r.get("status") != "pass":
                failed.append(f"round {w['seq']}: {r.get('note', '')}")
        spec["clip_results"] = rows
        spec["status"] = "pass" if not failed else "orange"
        spec["note"] = (f"all {len(rows)} clips matched" if not failed else " | ".join(failed))
        spec["tokens"] = [sum(r.get("tokens", [0, 0])[0] for r in rows),
                          sum(r.get("tokens", [0, 0])[1] for r in rows)]
    for c in cases:
        c["verdict"] = recompute_verdict(c)


def run_ai(video_provider, image_provider, session_webm: Path, runs: list,
           game: str | None = None, workers: int | None = None) -> None:
    """Grade every pending AI assert (video AND image) of EVERY run, with the calls in parallel.

    `runs` = [(run_dir, cases)]. The calls are independent — each is one clip or one still and one
    prompt — so they go on ONE pool shared by the whole suite, `workers` (default `ai_workers()`) at
    a time. Before this each call waited for the previous one, and a suite of ~40 calls at 20-60 s
    each spent most of its wall-clock idle on the network. Grading stays sequential and in run
    order once the answers are in, so results and reports read exactly as before.

    `session_webm` is the single shared session video at the run root (not per-run copies).
    `game` names the asset manifest whose pictures prime each video call (see `_reference_for`)."""
    from concurrent.futures import ThreadPoolExecutor
    workers = workers or ai_workers()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai") as pool:
        plans = []
        for run_dir, cases in runs:
            vplan = (_plan_video(pool, video_provider, session_webm, run_dir, cases, game)
                     if video_provider is not None else [])
            iplan = _plan_image(pool, image_provider, cases) if image_provider is not None else []
            plans.append((cases, vplan, iplan))
        # both plans are (spec, mode, [(item, future, own), ...]); count the calls really made
        n = sum(own for _, vp, ip in plans for plan in (vp, ip)
                for _, _, parts in plan for *_, own in parts)
        if n:
            print(f"  {n} AI call(s) running, {min(workers, n)} at a time...")
        for cases, vplan, iplan in plans:            # .result() blocks until each answer is in
            if vplan:
                _finish_video(vplan, cases)
            if iplan:
                _finish_image(iplan, cases)
