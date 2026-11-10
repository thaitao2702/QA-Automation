#!/usr/bin/env python3
"""
Scenario runner — invokes a closed-box scenario in place of scripted `steps:`, runs the
per-case asserts, and emits the SAME artifacts as the existing run/case pipeline so replay
and reporting integrate unchanged (docs/scenario-assert-authoring.md §7).

Suite shape (QA-authored):

    runs:
      - run:
          scenario: fs-complete            # a registered closed box (scenario_engine.REGISTRY)
          cases:
            - id: MY-30
              title: "..."
              assert:
                - stream:  freespin_award          # response-engine invariant  (Hard)
                - stream:  "{terminal.freeSpins} == 0"
                - video: { at: big_cascade, check: "this round shows a 3-step cascade" }  # AI over a sub-clip

                - image: { at: AFTER, prompt: ..., output_schema: ..., expected: ... }  # AI per screenshot

One scenario play -> many case verdicts. Per case: any stream violation -> red;
scenario unreached -> inconclusive; video/image asserts are deferred (pending) — the
existing AI pipeline plugs in there. ONE session.webm is saved at the run root and shared
by every scenario (replays reference ../session.webm); per run: result.json (kind:run,
cases[]), events.jsonl, timeline.json, replay.html, _clips/ (trimmed AI clips),
_shots/ (screenshots for image: asserts).

`video:` grades a downsampled montage; `image:` grades full-resolution stills, ONE AI call
per captured shot, and passes only if every shot passes. Use image: for anything where fine
detail decides the answer (text, glyphs, clipping); video: for anything about motion or time.

URL resolution (env.config.<env>.yml): `base_url` (local build + a token minted from
`token_env`'s backend) > `url` (static, ignored for language runs) > `url_script`.

USAGE
    python scripts/run_scenario.py --env dev --suite games/mystic/test-suites/scenario-demo.yml
    python scripts/run_scenario.py --env dev --scenario fs-complete      # ad-hoc, default invariants
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys

# The scenario descriptions this prints contain characters the Windows console codepage cannot
# encode (a ⚠ in `ante-freespins`, box-drawing rules elsewhere). Printing one killed the whole run
# with `UnicodeEncodeError: 'charmap' codec can't encode character '⚠'` -- before any test had
# executed, and with nothing in the message to say it was only console encoding. Force UTF-8 and
# degrade unencodable characters rather than raising; the runner has no business failing over how a
# terminal renders a comment.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):        # not a reconfigurable stream (piped, redirected)
        pass
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario_engine as se   # noqa: E402
import ai_video                # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "framework" / "templates" / "replay.html"


def _game_config(game: str, env: str) -> dict:
    suffix = "stg" if env in ("staging", "stg") else env
    path = REPO / "games" / game / f"env.config.{suffix}.yml"
    if not path.exists():
        sys.exit(f"No game config at {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _with_params(url: str, cfg: dict) -> str:
    """Append the config's browser_params and guarantee bridge=1 (nothing is testable without it)."""
    for extra in (str(cfg.get("browser_params") or "").lstrip("?&"), ""):
        if extra and extra not in url:
            url += ("&" if "?" in url else "?") + extra
    return url if "bridge=1" in url else url + ("&" if "?" in url else "?") + "bridge=1"


def _run_url_script(cfg: dict, env: str, lang: str | None = None) -> str:
    """Run the config's `url_script` and return the launch URL it prints on stdout.

    Credentials come from the .env overlay of `token_env` (default: the run env). A local build has
    no backend of its own, so its config points `token_env: dev` at the environment that can actually
    mint a token."""
    script = cfg.get("url_script")
    if not script:
        raise RuntimeError("game config has no `url_script:` -- needed to mint a launch token")
    try:
        from framework.env_files import load_env_files
        load_env_files(cfg.get("token_env") or env)      # PLAYSITE_API_URL / USER / PASS
    except Exception:  # noqa: BLE001
        pass
    sub_env = os.environ.copy()
    for key, var in (("game_code", "PLAYSITE_GAME_CODE"), ("currency", "PLAYSITE_CURRENCY")):
        val = (cfg.get("playsite") or {}).get(key)
        if val not in (None, ""):
            sub_env[var] = str(val)
    if lang:
        sub_env["PLAYSITE_LANGUAGE"] = lang
    r = subprocess.run(script, shell=True, capture_output=True, text=True, cwd=str(REPO), env=sub_env)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"url_script failed{f' for language {lang!r}' if lang else ''} "
                           f"(exit {r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def resolve_url(game: str, env: str, override=None, lang: str | None = None) -> str:
    """The one URL resolver. In priority order:

    1. `--url` override — used as-is.
    2. `base_url` (a LOCAL build): mint a token via `url_script` and hang that token's query string
       off base_url. The client is local, the session is the backend's — see env.config.local.yml.
    3. `url` (a pasted launch URL) — but NOT for a language run: its token carries the language it
       was minted with, so reusing it would silently test the wrong one.
    4. `url_script` — resolve fresh from the backend.
    """
    if override:
        return _with_params(override, {})
    cfg = _game_config(game, env)
    base = str(cfg.get("base_url") or "").strip()
    if base:
        token_url = _run_url_script(cfg, env, lang)
        query = urlparse(token_url).query
        if not query:
            raise RuntimeError(f"url_script returned no query string to graft onto base_url: {token_url[:80]}")
        return _with_params(base + ("&" if "?" in base else "?") + query, cfg)
    static = str(cfg.get("url") or "").strip()
    if static and not lang:
        return _with_params(static, cfg)
    if cfg.get("url_script"):
        return _with_params(_run_url_script(cfg, env, lang), cfg)
    sys.exit(f"env.config for '{game}'/'{env}' has none of base_url / url / url_script. Pass --url.")


def resolve_url_for_language(game: str, env: str, lang: str) -> str:
    """A launch URL in `lang`. The language lives in the LAUNCH TOKEN — there is no in-game switch —
    so every language change re-mints the token and reloads. Works for a local build too: the token
    is minted in that language and grafted onto base_url."""
    return resolve_url(game, env, None, lang=lang)


def _norm_td(v) -> list:
    """Normalize a `teardown:` declaration to a list of registry names ([] = disabled).
    Accepts None, 'none'/'' (off), a single name, or a list of names."""
    if v is None:
        return []
    if isinstance(v, str):
        return [] if v.strip().lower() in ("", "none") else [v.strip()]
    if isinstance(v, (list, tuple)):
        out: list = []
        for x in v:
            out += _norm_td(x)
        return out
    return []


async def run_teardowns(names: list, ctx, label: str, processed: list, suite: bool = False) -> bool:
    """Run the named teardowns (from se.TEARDOWNS) in order on `ctx`. An unknown name is recorded and
    skipped; a teardown that raises is recorded and, for a run-level teardown, ABORTS the suite
    (returns True). Suite-end teardowns never abort (nothing is left to run) — they just record."""
    for td in names:
        fn = se.TEARDOWNS.get(td)
        if fn is None:
            print(f"  ! unknown teardown '{td}' -- skipping")
            processed.append({"name": f"{label}:{td}", "error": f"unknown teardown '{td}'"})
            continue
        try:
            print(f"  teardown[{td}] {'after all runs' if suite else 'after ' + label}...")
            await fn(ctx)
        except Exception as e:                                   # noqa: BLE001
            scope = "suite teardown" if suite else "run teardown"
            print(f"  !! {scope} '{td}' failed ({label}): {e}"
                  + ("" if suite else " -- aborting run"))
            processed.append({"name": f"{label}:{td}", "error": f"teardown failed: {e}"})
            if not suite:
                return True                                      # run-level failure aborts the suite
    return False


def compute_verdict(reached: bool, stream_results: list, has_ai: bool) -> str:
    if not reached:
        return "inconclusive"
    if any(not r["result"]["valid"] for r in stream_results):
        return "red"
    if has_ai:
        return "pending"                # AI video/image asserts deferred to the existing AI pipeline
    return "green"


def _video_spec(d, result: "se.ScenarioResult") -> dict:
    """Turn a `video:` assert into a structured, interpolated spec for the deferred AI pass:
    kind check|read, prompt/read/expected with {res.*} bound in, and the `at:` sub-clip window."""
    def rt(s):
        return se.resolve_tokens(str(s), result) if result.reached else str(s)
    at = d.get("at") if isinstance(d, dict) else None
    each = d.get("each") if isinstance(d, dict) else None
    spec = {"at": at,
            # Which reference pictures prime this call. Unset -> the game's whole manifest.
            "asset_groups": (d.get("asset_groups") if isinstance(d, dict) else None),
            "assets": (d.get("assets") if isinstance(d, dict) else None),
            "window": se.resolve_window(result, at) if (result.reached and not each) else None,
            "status": "pending"}
    if each:
        # `each: <list mark>` -> ONE CLIP PER ROUND, each graded on its own; the assert passes only
        # if every clip passes (like an `image:` assert over its shots). See se.resolve_each.
        spec["each"] = each
        windows = se.resolve_each(result, each) if result.reached else []
        # `max_rounds: N` caps the AI calls (one per clip). The N are spread EVENLY over the list,
        # not the first N, so rounds from every bonus the run played stay represented. No
        # `max_rounds` -> every round is graded.
        cap = d.get("max_rounds")
        if isinstance(cap, int) and cap > 0 and len(windows) > cap:
            spec["rounds_available"] = len(windows)
            step = len(windows) / cap
            windows = [windows[int(k * step)] for k in range(cap)]
        spec["windows"] = windows
    if isinstance(d, dict):                              # video sampling modes (passed to the provider)
        if d.get("high_fps"):
            spec["high_fps"] = True
        if d.get("timestamp"):
            spec["timestamp"] = True
    if isinstance(d, str):
        spec.update(kind="check", check=rt(d))
    elif "prompt" in d or "output_schema" in d or "expected" in d:
        # raw.yml-style explicit form: full prompt + output_schema + expected field map
        spec.update(kind="schema", prompt=rt(d.get("prompt", "")),
                    output_schema=d.get("output_schema", {}),
                    expected={k: (rt(v) if isinstance(v, str) else v)
                              for k, v in (d.get("expected") or {}).items()})
    elif "check" in d:
        spec.update(kind="check", check=rt(d["check"]))
    else:
        spec.update(kind="read", read=rt(d.get("read", "")))
        if "equals" in d:
            spec["equals"] = rt(d["equals"])
        if "near" in d:
            spec["near"] = {"value": rt(d["near"].get("value", "")), "tol": d["near"].get("tol", 0)}
    return spec


def _shots_for(result: "se.ScenarioResult", at) -> list:
    """Which captured stills an `image:` assert covers. `at` is None/'all' (every shot) or a
    case-insensitive substring of the shot label ('AFTER', 'settings', 'my ')."""
    shots = result.shots or []
    if at in (None, "", "all"):
        return list(shots)
    key = str(at).strip().lower()
    return [s for s in shots if key in s["label"].lower()]


def _image_spec(d, result: "se.ScenarioResult") -> dict:
    """Turn an `image:` assert into a structured spec for the deferred AI pass. Unlike `video:`, this
    grades one AI call PER CAPTURED SHOT — the whole point is that each surface is judged on its own
    full-resolution still — and the assert passes only if every shot passes."""
    def rt(s):
        return se.resolve_tokens(str(s), result) if result.reached else str(s)
    at = d.get("at") if isinstance(d, dict) else None
    spec = {"at": at, "status": "pending", "kind": "schema",
            # `combine: true` -> every still goes to the AI in ONE call, as pages of one scrolling
            # surface. Without it each still is graded alone, which is right for a tour of DIFFERENT
            # surfaces and wrong for screenfuls of the same one.
            "combine": bool(isinstance(d, dict) and d.get("combine")),
            "shots": [{"label": s["label"], "path": s["path"], "file": s["file"]}
                      for s in _shots_for(result, at)]}
    if isinstance(d, str):
        spec.update(prompt=rt(d), output_schema={}, expected={})
    else:
        spec.update(prompt=rt(d.get("prompt", "")),
                    output_schema=d.get("output_schema", {}),
                    expected={k: (rt(v) if isinstance(v, str) else v)
                              for k, v in (d.get("expected") or {}).items()})
    return spec


async def bo_setup(bo, game: str, pt, bet=None) -> str:
    """BO paytable SETUP for a run: ensure the automation paytable exists, point this agent's game at
    it, and (if content is given) rewrite it to force a determined layout. `pt` is the run's
    `paytable:` value — a fixture-ref string, or a dict {code?, ref?, inline?, max_win_x?,
    max_payout?}. Returns the code.
    Raises if BO isn't configured (so the run is skipped rather than silently un-forced).

    MAX WIN. `max_win_x: 10` caps the agent's payout at 10x the bet -- the LIVE base stake the
    caller read off the open game and passed in as `bet`, falling back to `bo.reference_bet` when
    there was no game to read. `max_win_x` also takes an EXPRESSION over that bet ("10 * bet"), and
    `max_payout: <amount>` sets the cap directly. This is what makes a
    forced bonus END: an easy-trigger paytable can award free spins that retrigger for hundreds of
    rounds, and the server truncates at the cap and sets `hasReachedMaxWin` — which `is_terminal`
    already accepts. Without a cap those runs are bounded only by the scenario's spin budget.
    The teardown clears the override, so the cap never leaks into the next run."""
    if isinstance(pt, str):
        pt = {"ref": pt}
    if bo is None:
        raise RuntimeError("paytable requested but BO not configured -- set the bo: block in "
                           "games/{}/env.config.*.yml (or BO_* env vars)".format(game))
    # per-run `code:` > the game's bo.paytable_code > the historical default
    code = pt.get("code") or getattr(bo, "paytable_code", None) or "AutomationTest"
    ref, inline = pt.get("ref"), pt.get("inline")
    content = bo.resolve_paytable(ref=ref, inline=inline) if (ref or inline is not None) else None
    max_payout = _resolve_max_payout(bo, game, pt, bet=bet)
    await bo.ensure_paytable(code, paytable_content=content)     # idempotent create (clones default if no content)
    await bo.select_agent_paytable(code, max_payout=max_payout)   # this agent now plays `code`
    if content is not None:
        await bo.update_paytable(code, content)                   # force the exact forced layout
    print(f"  paytable[{code}] set" + (f" (ref={ref})" if ref else "")
          + (f", max win {max_payout}" if max_payout is not None else ""))
    return code


def max_win_needs_bet(pt) -> bool:
    """Does this run's `paytable:` block need a BET before its cap can be resolved? True only for
    `max_win_x` with no absolute `max_payout` overriding it.

    The runner asks this BEFORE touching BO, because a bet-derived cap has to be resolved against
    the bet the game is really sitting at — which means opening the game to read it first. An
    absolute `max_payout` needs nothing, and neither does a run with no cap, so those keep the
    cheap path: push the paytable, load once, go."""
    if not isinstance(pt, dict):
        return False
    return pt.get("max_payout") is None and pt.get("max_win_x") is not None


def _resolve_max_payout(bo, game: str, pt: dict, bet=None):
    """The run's max-win cap as an absolute amount, or None to leave the agent's default alone.

    `max_payout` wins if both are given. `max_win_x` is a MULTIPLE OF THE BET and takes two forms:

        max_win_x: 10                  ->  10 x bet
        max_win_x: "10 * bet"          ->  the same thing, written out
        max_win_x: "max(50, 10 * bet)" ->  a floor under the multiple

    The expression form is the "function of the current bet" a bare multiplier could not express.
    It is evaluated with NO builtins and a four-name namespace (`bet` plus `min`/`max`/`round`), so
    a suite cannot smuggle arbitrary code into the runner through a config file.

    WHICH BET. `bet` is the live BASE stake the caller read off the open game and passed in — the
    plain stake, never the ante-inflated one, so a run that switches Ante Bet on is capped against
    the bet the player started from and its cap matches the equivalent non-ante run. That needs no
    arithmetic: the bet display already shows the base and does not move when ante is toggled (see
    `_BASE_BET_JS`). `bo.reference_bet` from the game's env.config is
    the fallback for when there was no game to read, or the bet came back unreadable. Raising when
    neither is available is deliberate: a silently-unresolved multiplier would leave the bonus
    uncapped and the run would simply take a very long time instead of failing."""
    if pt.get("max_payout") is not None:
        return float(pt["max_payout"])
    x = pt.get("max_win_x")
    if x is None:
        return None
    if bet is None:
        bet = getattr(bo, "reference_bet", None)
    if not bet:
        raise RuntimeError(
            f"paytable.max_win_x={x!r} needs a bet to multiply. The live bet could not be read "
            f"from the game, and games/{game}/env.config.*.yml has no `reference_bet:` under its "
            f"bo: block to fall back on. Add the bet the scenario plays at (the game's opening "
            f"stake), or give an absolute `max_payout:`.")
    bet = float(bet)
    if isinstance(x, str):
        try:
            value = eval(x, {"__builtins__": {}},              # noqa: S307 - namespace above
                         {"bet": bet, "min": min, "max": max, "round": round})
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"paytable.max_win_x={x!r} is not an expression this runner can evaluate ({e}). "
                f"Use a number, or an expression over `bet` such as '10 * bet'.") from e
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(
                f"paytable.max_win_x={x!r} evaluated to {value!r}, which is not a number.")
    else:
        value = float(x) * bet
    return round(float(value), 2)


# The live BASE stake, read off an OPEN game.
#
# `getSnapshot().betAmount` IS ALREADY THE BASE BET -- it does not move when Ante Bet is switched
# on. Measured live 2026-09-04: with ante ON the snapshot read 4.00 while the server deducted
# `stake: 12.00` per spin (balance 12080.79 -> 12068.79 -> 12056.79), i.e. betAmount == baseStake
# and the ante multiplier lives only in what is charged. This code first divided betAmount by the
# ante multiplier to "recover" the base and produced 1.33 from a 4.00 bet, capping the run at a
# third of what the suite asked for. Do not reintroduce that: the bet display is the base, and
# `bet` in a `max_win_x` expression is the base, so the two already agree.
_BASE_BET_JS = """() => {
    const b = window.__gameTestBridge;
    if (!b || !b.getSnapshot) return null;
    const s = b.getSnapshot() || {};
    const shown = Number(s.betAmount);
    return (isFinite(shown) && shown > 0) ? shown : null; }"""


async def read_base_bet(page):
    """The game's current BASE bet, or None when it cannot be read. Never raises: an unreadable bet
    is a reason to fall back on `reference_bet`, not to fail the run."""
    try:
        v = await page.evaluate(_BASE_BET_JS)
    except Exception:  # noqa: BLE001
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


async def bo_reset(bo, label: str = "") -> bool:
    """BO paytable TEARDOWN: reset the agent's game paytable to the game default (best-effort).
    Returns True when the reset went through."""
    if bo is None:
        return False
    try:
        await bo.reset_agent_paytable()
        print(f"  paytable reset{(' after ' + label) if label else ''}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ! paytable reset failed{(' after ' + label) if label else ''}: {e}")
        return False


# Seconds for a BO change to reach the game server. BO answers 200 when IT has stored the change;
# the game server gets it later over Kafka (Rule 20), the same delay the denomination swap waits.
BO_SETTLE_S = 5.0


def ai_calls(cases: list) -> int:
    """Number of ACTUAL AI calls for a run's cases. Video specs sharing a `group` (a shared
    `expected_by_case` block) collapse to one call; ungrouped specs count 1 each. IMAGE specs cost
    one call PER SHOT — that is the point of them — deduped the same way by (group, shot)."""
    seen, n = set(), 0
    for c in cases:
        for spec in c.get("video", []):
            g = spec.get("group")
            if g is not None:
                if g in seen:
                    continue
                seen.add(g)
            # an `each:` spec is one call PER ROUND it covers
            n += len(spec.get("windows") or []) if "each" in spec else 1
        for i, spec in enumerate(c.get("image", [])):
            g = spec.get("group") or f"{c['id']}-img{i}"
            if spec.get("combine"):          # every still in one call, so it costs one
                if (g, "__combined__") not in seen:
                    seen.add((g, "__combined__"))
                    n += 1
                continue
            for shot in spec.get("shots", []):
                key = (g, shot["file"])
                if key in seen:
                    continue
                seen.add(key)
                n += 1
    return n


def build_cases(run_doc: dict, result: "se.ScenarioResult") -> list:
    """Stream asserts run now (Hard); video asserts are structured for the deferred AI pass.

    A run may also carry a SHARED `video:` block with `expected_by_case` (raw.yml style): ONE AI
    call over one clip, graded against several cases' expected subsets. It expands into a per-case
    spec that shares a `group` key, so the AI pass analyses the clip once and reuses the answer."""
    cases, case_map = [], {}
    for case in run_doc.get("cases", []):
        stream_results, video_specs, image_specs = [], [], []
        for a in case.get("assert", []):
            if "stream" in a:
                r = se.run_stream_assert(a["stream"], result) if result.reached else {"valid": False, "violations": ["scenario unreached"]}
                stream_results.append({"spec": a["stream"], "result": r})
            elif "video" in a:
                video_specs.append(_video_spec(a["video"], result))
            elif "image" in a:
                image_specs.append(_image_spec(a["image"], result))
        c = {"id": case.get("id"), "title": case.get("title", ""),
             "stream": stream_results, "video": video_specs, "image": image_specs}
        cases.append(c)
        case_map[c["id"]] = c

    # Shared blocks: `video:` (one) and/or `videos:` (a list) — each its own clip(s) and prompt,
    # graded per case through `expected_by_case`. A list, because one run can need SEVERAL
    # questions about DIFFERENT moments (walking-wild: grandmas entering, stand-ins knocked off),
    # each on its own short clips, rather than everything asked of one long recording.
    blocks = [run_doc.get("video")] + list(run_doc.get("videos") or [])
    for shared in blocks:
        if not (isinstance(shared, dict) and shared.get("expected_by_case")):
            continue
        base = _video_spec({k: v for k, v in shared.items() if k != "expected_by_case"}, result)
        group = f"shared-{id(shared)}"
        for cid, exp in shared["expected_by_case"].items():
            c = case_map.get(cid)
            if c is None:
                continue
            spec = dict(base)
            spec["group"] = group          # same clip+prompt -> AI called once, reused across cases
            spec["expected"] = {k: (se.resolve_tokens(str(v), result) if (isinstance(v, str) and result.reached) else v)
                                for k, v in (exp or {}).items()}
            c["video"].append(spec)

    shared_img = run_doc.get("image")
    if isinstance(shared_img, dict) and shared_img.get("expected_by_case"):
        base = _image_spec({k: v for k, v in shared_img.items() if k != "expected_by_case"}, result)
        group = f"shared-img-{id(shared_img)}"
        for cid, exp in shared_img["expected_by_case"].items():
            c = case_map.get(cid)
            if c is None:
                continue
            spec = dict(base)
            spec["group"] = group      # same shot+prompt -> analysed once, re-graded per case
            spec["expected"] = {k: (se.resolve_tokens(str(v), result) if (isinstance(v, str) and result.reached) else v)
                                for k, v in (exp or {}).items()}
            c["image"].append(spec)

    for c in cases:
        c["verdict"] = compute_verdict(result.reached, c["stream"],
                                       bool(c["video"]) or bool(c.get("image")))
    return cases


def write_replay(run_dir: Path, run_id: str, title: str, verdict: str, result: "se.ScenarioResult",
                 duration_ms: int, sections: list | None = None, section: dict | None = None,
                 api_events: list | None = None) -> None:
    """`sections` = every run's span in the shared session video (the seek bar's chapters);
    `section` = this run's own, which the replay opens on and highlights."""
    if not TEMPLATE.exists():
        return
    replay_data = {
        "test_id": run_id, "title": title, "final_verdict": verdict,
        "bridge_result": verdict, "ai_result": "pending", "duration_ms": duration_ms,
        "error": result.reason, "video_file": "../session.webm", "video_event_offset": 0,
        "steps": result.timeline,
        # the stream's spin_result rows + the raw payloads (bet / free-spin responses, gameInfo)
        "events": sorted(list(result.events) + list(api_events or []), key=lambda e: e.get("t", 0)),
        "sections": sections or [],
        "current_section": sections.index(section) if sections and section in sections else None,
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace("%%REPLAY_DATA%%", json.dumps(replay_data))
    (run_dir / "replay.html").write_text(html, encoding="utf-8")


def write_session_replay(root: Path, suite: str, processed: list, sections: list) -> bool:
    """ONE replay of the whole session at the run root (`replay.html`, next to session.webm): every
    run's steps and events on the one timeline, the runs as seek-bar chapters. A per-run replay shows
    only its own run's events; this is the view for following the suite end to end.

    Steps and events are tagged with their run (`2·auto-0`, `"run": "auto-play"`) because round
    numbers restart in every run. Returns False when there is nothing to show."""
    if not TEMPLATE.exists() or not (root / "session.webm").exists():
        return False
    runs = [p for p in processed if "run_dir" in p]
    if not runs:
        return False
    steps, events = [], []
    for k, p in enumerate(runs, 1):
        for s in p["result"].timeline:
            steps.append({**s, "id": f"{k}·{s.get('id')}",
                          "description": f"{p['name']}: {s.get('description') or s.get('action')}"})
        for e in list(p["result"].events) + list(p.get("api_events") or []):
            data = e.get("data")
            events.append({**e, "data": {"run": p["name"], **data} if isinstance(data, dict) else data})
    order = ["red", "error", "inconclusive", "orange", "pending", "green"]
    verdict = min((p.get("verdict") or "pending" for p in runs),
                  key=lambda v: order.index(v) if v in order else len(order))
    t0 = sections[0]["start_s"] if sections else 0
    t1 = sections[-1]["end_s"] if sections and sections[-1]["end_s"] else t0
    replay_data = {
        "test_id": suite, "title": f"{suite} · full session", "final_verdict": verdict,
        "bridge_result": verdict, "ai_result": "—", "duration_ms": int((t1 - t0) * 1000),
        "error": None, "video_file": "session.webm", "video_event_offset": 0,
        "steps": sorted(steps, key=lambda s: s.get("start_ms", 0)),
        "events": sorted(events, key=lambda e: e.get("t", 0)),
        "sections": sections, "current_section": None,
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace("%%REPLAY_DATA%%", json.dumps(replay_data))
    (root / "replay.html").write_text(html, encoding="utf-8")
    return True


_BADGE = {"green": "#2e7d32", "orange": "#ef6c00", "red": "#c62828",
          "pending": "#616161", "inconclusive": "#6a1b9a", "error": "#b71c1c"}


def write_report(root: Path, suite: str, processed: list, ai_cost: dict) -> Path:
    """Human-readable report.html for the whole run: every case, its verdict, the stream
    results and each video assert's prompt + the AI's actual answer + clip window."""
    import html as _h

    def esc(s):
        return _h.escape(str(s))

    def badge(v):
        tone = {"pass": "green", "fail": "red"}.get(v, v)
        tone = tone if tone in _BADGE else "pending"
        label = {"green": "Passed", "red": "Failed", "orange": "Partly Failed"}.get(v, v)
        return f'<span class="badge {tone}">{esc(label)}</span>'

    counts: dict = {}
    sections = []
    for p in processed:
        if "run_dir" not in p:
            sections.append(f'<section class="scenario" data-status="error"><h2>{esc(p["name"])} {badge("error")}</h2>'
                            f'<div class="mm">{esc(p.get("error"))}</div></section>')
            continue
        name, cases, reached = p["name"], p["cases"], p["result"].reached
        run_v = ("inconclusive" if not reached
                 else "red" if any(c["verdict"] == "red" for c in cases)
                 else "orange" if any(c["verdict"] == "orange" for c in cases)
                 else "pending" if any(c["verdict"] == "pending" for c in cases) else "green")
        case_html = []
        # The clip each AI video assert was graded on. `ai_video.run_ai` writes it as
        # `_clips/<case>-<i>.webm`, and the provider re-encodes it into `<case>-<i>-montage.mp4`,
        # which is what the model actually receives -- so the montage is linked first, the raw clip
        # only when no montage was built. ⚠ Cases SHARING one AI call (`expected_by_case`) trim
        # and upload once, under the FIRST case's id; the others reuse that file via `group`.
        clips_dir = Path(p["run_dir"]) / "_clips"
        group_clip: dict = {}

        def _ai_clip(case_id: str, i: int, group):
            for fn in (f"{case_id}-{i}-montage.mp4", f"{case_id}-{i}.webm"):
                if (clips_dir / fn).is_file():
                    if group is not None:
                        group_clip.setdefault(group, fn)
                    return fn
            return group_clip.get(group) if group is not None else None

        clips_seen: list = []            # (file, first case id that used it), in case order
        for c in cases:
            counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
            for i, v in enumerate(c.get("video") or []):
                fn = _ai_clip(c["id"], i, v.get("group"))
                if fn and fn not in (f for f, _ in clips_seen):
                    clips_seen.append((fn, c["id"]))
            # Collapsed to one line -- id, title, verdict, and a chevron on the right showing
            # whether it is open. Everything else opens on click.
            parts = [f'<summary><span class="case-title">{esc(c["id"])} — '
                     f'{esc(c.get("title", ""))}</span> {badge(c["verdict"])}'
                     f'<span class="chev" aria-hidden="true"></span></summary>']
            for s in c["stream"]:
                ok = s["result"]["valid"]
                viol = "" if ok else " · " + esc("; ".join(s["result"]["violations"]))
                parts.append(f'<div class="a {"ok" if ok else "bad"}">stream · '
                             f'<code>{esc(s["spec"])}</code> → {"pass" if ok else "FAIL"}{viol}</div>')
            for v in c["video"]:
                if "each" in v:
                    # ONE CLIP PER ROUND: a summary line, then a row per clip with its round, its
                    # answer and a link to the montage the model saw.
                    rows = v.get("clip_results") or []
                    n_ok = sum(1 for r in rows if r.get("status") == "pass")

                    def _clip_link(r):
                        # `clip` is the file the call was made on (shared across cases); the
                        # provider's montage of it, which is what the model saw, is preferred
                        raw = r.get("clip") or ""
                        for fn in (raw.replace(".webm", "-montage.mp4"), raw):
                            if fn and (clips_dir / fn).is_file():
                                return (f' <a href="{esc(name)}/_clips/{esc(fn)}" '
                                        f'target="_blank">▶ clip</a>')
                        return ""
                    row_html = "".join(
                        f'<div class="ans">round {esc(r.get("seq"))} {badge(r.get("status", "?"))} '
                        f'clip {esc(r.get("window"))}{_clip_link(r)} — AI answered: '
                        f'<code>{esc(json.dumps(r.get("data")))}</code>'
                        + (f'<div class="mm">{esc(r.get("note"))}</div>'
                           if r.get("status") != "pass" and r.get("note") else "")
                        + '</div>' for r in rows)
                    parts.append(
                        '<div class="a v">'
                        f'video[each: {esc(v.get("each"))}] {badge(v.get("status", "?"))} '
                        f'{n_ok}/{len(rows)} clips'
                        + (f' (sampled from {esc(v["rounds_available"])} rounds, '
                           f'max_rounds={len(v.get("windows") or [])})'
                           if v.get("rounds_available") else "")
                        + f'<div class="prompt">{esc(v.get("prompt"))}</div>'
                        + (f'<div class="ans">expected: <code>{esc(json.dumps(v.get("expected")))}</code></div>'
                           if v.get("expected") else "")
                        + (f'<div class="mm">{esc(v.get("note"))}</div>'
                           if v.get("status") != "pass" and v.get("note") else "")
                        + row_html + '</div>')
                    continue
                answer = v.get("read") if v.get("kind") == "read" else v.get("data")
                parts.append(
                    '<div class="a v">'
                    f'video[{esc(v.get("at") or "all")}] {badge(v.get("status", "?"))} '
                    f'clip {esc(v.get("window"))}'
                    f'<div class="prompt">{esc(v.get("prompt") or v.get("check") or v.get("read"))}</div>'
                    f'<div class="ans">AI answered: <code>{esc(json.dumps(answer))}</code></div>'
                    + (f'<div class="ans">expected: <code>{esc(json.dumps(v.get("expected")))}</code></div>'
                       if v.get("expected") else "")
                    + (f'<div class="mm">mismatches: {esc("; ".join(v.get("mismatches", [])))}</div>'
                       if v.get("mismatches") else "")
                    + (f'<div class="why">why: {esc(v.get("reason"))}</div>' if v.get("reason") else "")
                    + '</div>')
            # IMAGE asserts, one collapsible section each. They were absent from the report
            # entirely, so an image-graded run (language-tour, game help) showed only its stream
            # line and looked as though the AI had never run. Collapsed because one assert can
            # carry 18+ shots; the summary line alone says pass/fail, and expanding shows the
            # prompt, what the AI answered per screen, and the screenshot it answered about.
            for v in c.get("image", []):
                shots = v.get("shots") or []
                results = v.get("shot_results") or []
                # `shot_results` is built in the same order as `shots`; `file` is carried on the
                # result now, and the zip is the fallback for result.json written before that.
                paired = []
                for i, res_i in enumerate(results):
                    f = res_i.get("file") or (shots[i].get("file") if i < len(shots) else None)
                    paired.append((res_i, f))
                n_ok = sum(1 for x in results if x.get("status") == "pass")
                bad_n = len(results) - n_ok
                head = (f'image · {len(results) or len(shots)} shot(s) · '
                        f'{n_ok} passed, {bad_n} failed')
                rows = []
                for res_i, fname in paired:
                    ok = res_i.get("status") == "pass"
                    why = res_i.get("note") or ""
                    src = f'{name}/_shots/{fname}' if fname else None
                    rows.append(
                        f'<details class="shot {"ok" if ok else "bad"}"{"" if ok else " open"}>'
                        f'<summary>{badge(res_i.get("status", "?"))} '
                        f'<b>{esc(res_i.get("shot", "?"))}</b>'
                        + (f' — {esc(why[:160])}' if not ok and why else "")
                        + '</summary>'
                        + (f'<div class="ans">AI answered: '
                           f'<code>{esc(json.dumps(res_i.get("data")))}</code></div>'
                           if res_i.get("data") is not None else "")
                        + (f'<div class="mm">mismatches: '
                           f'{esc("; ".join(res_i.get("mismatches", [])))}</div>'
                           if res_i.get("mismatches") else "")
                        + (f'<div class="why">why: {esc(res_i.get("reason"))}</div>'
                           if res_i.get("reason") else "")
                        + "".join(
                            f'<a href="{esc(f"{name}/_shots/{fn}")}" target="_blank">'
                            f'<img class="shotimg" src="{esc(f"{name}/_shots/{fn}")}" '
                            f'loading="lazy"></a>'
                            for fn in (res_i.get("files") or ([fname] if fname else [])))
                        + '</details>')
                parts.append(
                    '<div class="a i">'
                    f'<details{"" if v.get("status") == "pass" else " open"}>'
                    f'<summary>{badge(v.get("status", "?"))} {esc(head)}</summary>'
                    f'<div class="prompt">{esc(v.get("prompt") or "")}</div>'
                    + (f'<div class="ans">expected: '
                       f'<code>{esc(json.dumps(v.get("expected")))}</code></div>'
                       if v.get("expected") else "")
                    + (f'<div class="mm">{esc(v.get("note"))}</div>'
                       if v.get("status") != "pass" and v.get("note") else "")
                    + '<div class="shots">' + "".join(rows) + '</div>'
                    + '</details></div>')
            case_html.append(f'<div class="case" data-status="{esc(c["verdict"])}">'
                             f'<details class="case-body">' + "".join(parts) + "</details></div>")
        # The run's own row: replay and the AI clip(s) side by side (the two videos), then result
        # JSON. One button per DISTINCT clip — cases sharing one AI call share one button; only
        # several distinct clips get the case id.
        ai_links = "".join(
            f'<a class="ai" href="{esc(f"{name}/_clips/{fn}")}" target="_blank" '
            f'title="The clip the AI graded">'
            f'{esc("▶ AI video" + (f" · {cid}" if len(clips_seen) > 1 else ""))}</a>'
            for fn, cid in clips_seen)
        sections.append(
            f'<section class="scenario" data-status="{run_v}"><h2>{esc(name)} {badge(run_v)}</h2>'
            + "".join(case_html)
            + f'<footer class="artifacts"><a href="{esc(name)}/replay.html">▶ Watch replay</a>'
              f'{ai_links}<a href="{esc(name)}/result.json">View result JSON ↗</a></footer></section>')

    tally = " ".join(f"{badge(k)}×{n}" for k, n in counts.items()) or "no cases"
    cost = (f'{ai_cost.get("gemini_video_calls", 0)} AI call(s), '
            f'{ai_cost.get("gemini_input_tokens", 0) + ai_cost.get("gemini_output_tokens", 0)} tokens, '
            f'${ai_cost.get("estimated_cost_usd", 0)}') if ai_cost else "AI not run"
    total = sum(counts.values())
    errors = sum(1 for p in processed if "run_dir" not in p)
    passed = counts.get("green", 0)
    rate = round(100 * passed / total) if total else 0
    metrics = "".join(
        f'<div class="metric"><span>{label}</span><strong>{value}</strong><small>{hint}</small></div>'
        for label, value, hint in [
            ("Test cases", total, f"Across {len(processed)} scenarios"),
            ("Passed", passed, f"{rate}% of all cases" if total else "No cases recorded"),
            ("Needs attention", counts.get("red", 0) + counts.get("orange", 0), "Failed or review required"),
            ("Run errors", errors, "Scenario execution errors"),
        ]
    )
    template = Path(__file__).with_name("report_template.html").read_text(encoding="utf-8")
    session = ""
    if (root / "replay.html").exists():   # written by write_session_replay just before this
        session = ('<a class="session-replay" href="replay.html" '
                   'title="Every run of this suite on one timeline">'
                   '▶ Watch full session replay</a>')
    replacements = {
        "SUITE": esc(suite), "METRICS": metrics, "TALLY": tally, "SESSION": session,
        "COST": esc(cost), "SECTIONS": "".join(sections),
        "COUNT": str(len(processed)),
    }
    # A single substitution keeps placeholder-like user content literal.
    doc = re.sub(r"%%([A-Z]+)%%", lambda m: replacements[m.group(1)], template)
    out = root / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default="mystic", help="game package under games/ (env.config + output dir)")
    ap.add_argument("--env", default="dev")
    ap.add_argument("--url", default=None)
    ap.add_argument("--suite", default=None, help="path to a runs/scenario/cases YAML")
    ap.add_argument("--scenario", default=None, help="ad-hoc: run this scenario with default invariants")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--slow-mo", type=int, default=0)
    ap.add_argument("--no-ai", action="store_true", help="skip video (AI) asserts, leave them pending")
    ap.add_argument("--only", default=None,
                    help="run only these runs from the suite, by run `name:` (or by scenario name, "
                         "which picks every run of it); comma-separated")
    ap.add_argument("--teardown", default=None, help="run-level teardown name for the ad-hoc --scenario path")
    args = ap.parse_args()

    # Build the list of runs (from --suite, or a one-off from --scenario) + the suite-end teardown
    suite_teardown: list = []
    if args.suite:
        doc = yaml.safe_load(Path(args.suite).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            sys.exit(f"ERROR: {args.suite} is not a YAML mapping (expected a top-level `runs:` key).")
        raw_runs = doc.get("runs") or []
        # A bare `-` left behind while editing is a valid YAML list item whose value is null, and
        # `runs:` then holds a None the loader used to call `.get()` on -- an AttributeError deep in
        # main() that named neither the file nor the line. Say which entry is empty instead.
        empty = [i + 1 for i, r in enumerate(raw_runs) if r is None]
        if empty:
            sys.exit(f"ERROR: {args.suite} has an EMPTY entry under `runs:` "
                     f"(item {', '.join(map(str, empty))} of {len(raw_runs)}) -- usually a stray `-` "
                     f"with nothing under it. Delete the bare dash or give it a `run:` block.")
        bad = [i + 1 for i, r in enumerate(raw_runs) if not isinstance(r, dict)]
        if bad:
            sys.exit(f"ERROR: {args.suite}: entry {', '.join(map(str, bad))} under `runs:` is not a "
                     f"mapping -- each item must be a `- run:` block.")
        runs = [r.get("run", r) for r in raw_runs]
        suite_teardown = _norm_td(doc.get("teardown"))          # top-level: runs once, after all runs
        # Opt-in "the game must be usable before/after this run" check. OFF by default: it is a real
        # waitForSpinClickable, and a scenario may legitimately end unable to satisfy it
        # (insufficient-balance ends with the out-of-money popup open, which blocks the spin
        # button). Draining an unfinished bonus is NOT part of this and always happens -- that is
        # repair, not an assertion. Set at suite level, overridable per run.
        suite_assert_base = bool(doc.get("assert_base", False))
    elif args.scenario:
        suite_assert_base = False
        runs = [{"scenario": args.scenario, "teardown": args.teardown, "cases": [{
            "id": f"{args.scenario}-default", "title": "default invariants",
            "assert": [{"stream": "conservation"}, {"stream": "stake_zero_in_freespins"},
                       {"stream": "terminal_reached"}]}]}]
    else:
        sys.exit("pass --suite <yaml> or --scenario <name>")

    if args.only:                       # keep only the named run(s)
        # A RUN NAME picks exactly that run; a SCENARIO NAME still works and picks every run of
        # that scenario (the old behaviour). Comma-separated, either kind, mixed.
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        runs = [r for r in runs if r.get("name") in want or r.get("scenario") in want]
        if not runs:
            names = [r.get("name") or r.get("scenario") for r in
                     ([x.get("run", x) for x in (doc.get("runs") or [])] if args.suite else [])]
            sys.exit(f"--only {args.only}: no run with that name (or scenario) in the suite. "
                     f"Run names: {', '.join(n for n in names if n)}")

    se.set_profile(args.game)          # per-game dialect: buttons, popups, states, capabilities
    url = resolve_url(args.game, args.env, args.url)
    tag = time.strftime("%d-%m-%Y-%H%M%S")
    root = REPO / "test-results" / args.game / args.env / f"scenario-{tag}"
    root.mkdir(parents=True, exist_ok=True)
    print(f"Scenario runner | {len(runs)} run(s) | {url[:70]}...")

    summary = {"suite": args.suite or f"ad-hoc:{args.scenario}", "runs": []}

    # optional Playsite wallet (for insufficient-balance / balance scenarios); None if unconfigured
    try:
        from framework.helpers.wallet import WalletHelper
        wallet = WalletHelper(args.game, args.env)
    except Exception as e:                       # noqa: BLE001
        wallet = None
        print(f"(wallet unavailable: {e})")

    # optional BO helper (for runs with a `paytable:` — e.g. Reels Glow forced layouts); None if unconfigured
    try:
        from framework.helpers.bo import BoHelper
        bo = BoHelper(args.game, args.env)
    except Exception as e:                       # noqa: BLE001
        bo = None
        print(f"(bo unavailable: {e})")

    # ⚠ START FROM A KNOWN PAYTABLE. The agent's paytable is SHARED state: a manual BO change, a
    # run on another machine, or anyone on the same agent can leave a forced fixture selected, and
    # every run here without a `paytable:` then plays on it. The runner resets after each forced run
    # but never checked what it INHERITED: on 2026-09-30 gm-core started on a leftover gm-fs-easy,
    # and `base-win` hit free spins on 10 of 24 base spins (every one awarding exactly 8 — the
    # fixture's fingerprint) and took 17 minutes to find one plain win. So: reset to the game's
    # default once, before the first page load, and give the game server time to get it.
    if bo is not None and await bo_reset(bo, label="whatever was left selected (suite-start baseline)"):
        await asyncio.sleep(BO_SETTLE_S)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, slow_mo=args.slow_mo)
        video_dir = root / "_video"
        context = await browser.new_context(viewport=se.VIEWPORT,
                                             record_video_dir=str(video_dir),
                                             record_video_size=se.VIEWPORT)
        page = await context.new_page()
        video_start = time.time()
        vpath = None
        processed = []                    # per run: driven + stream-graded; AI runs after the session
        # Each run's span in the session video -> chapters on every replay's seek bar. A run starts
        # at its SETUP and ends where the next one starts (its teardown + cleanup are part of it);
        # the last ends before the suite-end teardown. Verdict defaults to "error" and is
        # overwritten once the run is graded, so a skipped run still shows as its own chapter.
        sections = []

        def open_section(name: str) -> dict:
            now = round(time.time() - video_start, 2)
            if sections and sections[-1]["end_s"] is None:
                sections[-1]["end_s"] = now
            sec = {"name": name, "start_s": now, "end_s": None, "verdict": "error"}
            sections.append(sec)
            return sec

        def close_sections() -> None:
            if sections and sections[-1]["end_s"] is None:
                sections[-1]["end_s"] = round(time.time() - video_start, 2)

        try:
            last_ctx = None          # ctx of the last executed run -> used by the suite-end teardown
            prev_last_bet = None     # previous run's last BET response -> lets the PREPARE step tell
                                     # whether that run ended inside a bonus (a fresh ctx per run
                                     # would otherwise start with last_bet=None and drain nothing)
            aborted = False          # a run-level teardown that can't reach base stops the suite
            game_loaded = False      # has the initial navigate + startGame happened yet? (lazy)
            prev_pt = False          # did the previous run force a paytable (needs clearing on reload)?
            cur_url, cur_lang = url, None    # URL/language the page is CURRENTLY loaded with

            async def relaunch(lang: str) -> str:
                """Re-enter the game in `lang` (mints a fresh launch URL). Handed to every
                DriveContext so a scenario can switch language mid-run; also used by the runner for a
                run-level `language:`. Language lives in the launch token, so this is a real reload."""
                nonlocal cur_url, cur_lang, game_loaded
                new_url = resolve_url_for_language(args.game, args.env, lang)
                await page.goto(new_url, wait_until="networkidle")
                await page.wait_for_function("() => !!window.__gameTestBridge", timeout=30000)
                await se.start_game(page)
                cur_url, cur_lang, game_loaded = new_url, lang, True
                return new_url

            used_names: set = set()
            for run_doc in runs:
                scen_name = run_doc["scenario"]
                # The RUN's name labels everything this run produces -- its result folder, its
                # report section, its replay chapter. It used to be the scenario name, so two runs
                # of one scenario (four `last-bet-restore` runs in gm-lastbet) wrote into ONE folder
                # and each overwrote the last. A suite gives every run a `name:`; without one the
                # scenario name is used, and a repeat gets a -2, -3... so nothing is overwritten.
                name = str(run_doc.get("name") or scen_name)
                if name in used_names:
                    k = 2
                    while f"{name}-{k}" in used_names:
                        k += 1
                    name = f"{name}-{k}"
                used_names.add(name)
                section = open_section(name)
                scenario = se.REGISTRY.get(scen_name)
                if scenario is None:
                    print(f"  ! unknown scenario '{scen_name}' -- skipping")
                    processed.append({"name": name, "error": "unknown scenario"})
                    continue
                # `params:` — build a FRESH instance with the run's arguments, so a scenario's
                # constructor defaults (which language to switch to, how many cycles, which
                # surfaces) are QA-settable per run instead of frozen in the registry instance.
                params = run_doc.get("params") or {}
                if params:
                    try:
                        scenario = type(scenario)(**params)
                    except TypeError as e:                       # unknown/bad kwarg -> name it
                        print(f"  ! bad params for '{name}': {e} -- skipping")
                        processed.append({"name": name, "error": f"bad params: {e}"})
                        continue
                print(f"\n=== run: {name} (scenario: {scen_name}) ==="
                      + (f"\n  params: {params}" if params else "")
                      + f"\n  {scenario.description}")
                run_dir = root / name
                (run_dir / "_shots").mkdir(parents=True, exist_ok=True)   # image: asserts write here
                # `bo=` lets a scenario change BO config MID-RUN, which the run-level `paytable:`
                # cannot: that is applied before the game opens. `last-bet-restore`'s denomination
                # swap needs the change to land while the game is CLOSED, between two loads.
                ctx = se.DriveContext(page, video_start, wallet=wallet, browser=browser, url=cur_url,
                                      bo=bo, relaunch=relaunch, language=cur_lang,
                                      shots_dir=run_dir / "_shots")
                has_pt = bool(run_doc.get("paytable"))
                pt_active = False
                want_assert_base = bool(run_doc.get("assert_base", suite_assert_base))
                # ── SETUP: everything that must be true BEFORE the game loads ───────────────────
                # Language and paytable are both LOAD-TIME settings: the language is baked into the
                # launch token, and the client reads the paytable when it boots. Neither can be
                # applied to an already-open game, so both are settled here, with NO page
                # interaction, and the single LOAD below opens the game once with both in force.
                # (Applying them after opening would mean loading the game and then reloading it —
                # two page loads for a state one load could have started in.)
                lang = run_doc.get("language")
                needs_lang = bool(lang and lang != cur_lang)
                target_url = cur_url
                if needs_lang:
                    try:
                        print(f"  language[{lang}] minting a launch URL...")
                        target_url = resolve_url_for_language(args.game, args.env, lang)
                    except Exception as e:  # noqa: BLE001
                        print(f"  !! language setup failed for '{name}': {e} -- skipping run")
                        processed.append({"name": name, "error": f"language setup failed: {e}"})
                        continue
                if has_pt:
                    # A BET-DERIVED CAP HAS TO SEE THE BET. `max_win_x` is a multiple of the stake,
                    # and BO is written before the client boots, so the cap used to be resolved
                    # against `reference_bet` in env.config — a hand-maintained number that drifts.
                    # It was 0.6 on a game that opens at 4.00, which turned `max_win_x: 500` into a
                    # 300 cap: 75x the real bet, not 500x, and low enough that the bonus truncated
                    # on hasReachedMaxWin. So: open the game, read the stake it is actually sitting
                    # at, resolve the cap against THAT, and let the load below re-enter with the
                    # forced paytable in force. The extra open costs one page load and only on the
                    # FIRST run of a session — after that the page is already up and the bet is
                    # read from it for free. `reference_bet` stays as the fallback for a game that
                    # will not open or will not publish its bet.
                    live_bet = None
                    if max_win_needs_bet(run_doc["paytable"]):
                        if not game_loaded:
                            try:
                                await page.goto(target_url, wait_until="networkidle")
                                await page.wait_for_function(
                                    "() => !!window.__gameTestBridge", timeout=30000)
                                await se.start_game(page)   # READY -> IDLE, so the bet is settled
                            except Exception as e:  # noqa: BLE001
                                # Not fatal: the real load below re-runs this and reports properly.
                                print(f"  ! bet probe could not open the game: {e}")
                        live_bet = await read_base_bet(page)
                        print(f"  bet[live] {live_bet}" if live_bet is not None else
                              "  ! bet[live] unreadable -- falling back to bo.reference_bet")
                    try:
                        await bo_setup(bo, args.game, run_doc["paytable"], bet=live_bet)
                        pt_active = True
                    except Exception as e:  # noqa: BLE001
                        print(f"  !! paytable setup failed for '{name}': {e} -- skipping run")
                        processed.append({"name": name, "error": f"paytable setup failed: {e}"})
                        await bo_reset(bo)          # undo any partial select
                        prev_pt = False
                        continue
                # ── PREPARE: start from a clean, spinnable base ────────────────────────────────
                # ONE load covers every reason a run needs one — first run, a language change, a
                # paytable to pick up, or a previous forced paytable to clear — so a run never pays
                # for two. (The exception is the bet probe above, which opens the game to read the
                # stake a bet-derived cap needs; that is a second load, and only on the first run of
                # a session.) When none applies the page stays as it is and ensure_base establishes the
                # base in place. Both paths end on the same assertion, so either satisfies PREPARE.
                needs_load = (not game_loaded) or needs_lang or has_pt or prev_pt
                if needs_load:
                    try:
                        await page.goto(target_url, wait_until="networkidle")
                        await page.wait_for_function("() => !!window.__gameTestBridge", timeout=30000)
                        if not game_loaded:                  # required-API check, once per session
                            miss = await se.missing_api(page)
                            if miss:
                                print(f"ERROR: bridge missing {miss} -- no runs executed")
                                if pt_active:
                                    await bo_reset(bo, label=name)
                                break
                        # Logged at LOAD DONE (bridge up, before Start) -- the moment the launch
                        # payload exists -- not after start_game, which first plays out any owed bonus.
                        await ctx.capture_game_info("prepare load")

                        async def _log_resumed(raw: dict) -> None:   # each resumed round AS it arrives
                            ctx.log_api("freespin_response", {"phase": "resumed at load",
                                                              "response": raw})

                        await se.start_game(page, on_freespin=_log_resumed)  # resumes an owed bonus,
                                                                             # then asserts base
                        cur_url, game_loaded = target_url, True
                        if needs_lang:
                            cur_lang = lang
                        ctx.language, ctx._url = cur_lang, cur_url
                    except Exception as e:  # noqa: BLE001
                        print(f"  !! prepare[{name}] load failed: {e} -- skipping run")
                        processed.append({"name": name, "error": f"prepare failed (load): {e}"})
                        if pt_active:
                            await bo_reset(bo, label=name)   # don't leave the forced paytable active
                        prev_pt = False
                        continue
                else:
                    try:
                        state = await se.ensure_base(ctx, prev_last_bet, f"prepare[{name}]",
                                                     assert_base=want_assert_base)
                        if state != "clean":
                            print(f"  prepare[{name}]: {state}")
                    except Exception as e:  # noqa: BLE001
                        print(f"  !! prepare failed for '{name}': {e} -- skipping run")
                        processed.append({"name": name, "error": f"prepare failed: {e}"})
                        continue
                try:
                    t0 = time.time()
                    result = await scenario.run(ctx)
                    if result is None:
                        # A scenario that falls off the end returns None; without this the runner
                        # dies with a bare AttributeError naming nothing useful.
                        raise RuntimeError(f"scenario '{name}' returned None -- its run() fell "
                                           f"through without a ScenarioResult")
                    duration_ms = int((time.time() - t0) * 1000)
                    print(f"  reached={result.reached}"
                          + (f" ({result.reason})" if result.reason else "")
                          + f" | rounds={len(result.rounds)}")
                    cases = build_cases(run_doc, result)
                    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as f:
                        for e in result.events:
                            f.write(json.dumps(e) + "\n")
                    (run_dir / "timeline.json").write_text(json.dumps(result.timeline, indent=2), encoding="utf-8")
                    processed.append({"name": name, "run_dir": run_dir, "result": result,
                                      "cases": cases, "description": scenario.description,
                                      "duration_ms": duration_ms, "section": section,
                                      "api_events": ctx.api_events})
                except Exception as e:                          # one run failing must not sink the suite
                    print(f"  ! run '{name}' errored: {type(e).__name__}: {e}")
                    processed.append({"name": name, "error": f"{type(e).__name__}: {e}"})
                if pt_active:                       # TEARDOWN: reset the agent paytable to default
                    await bo_reset(bo, label=name)
                prev_pt = has_pt
                last_ctx = ctx
                # run-level teardown FIRST: the declared work (deposit-10000 tops the wallet up
                # and reloads, ...). Doing it before the check means a teardown that reloads has
                # already left the game clean, instead of the check paying a timeout to discover a
                # state the teardown was about to fix anyway.
                aborted = await run_teardowns(_norm_td(run_doc.get("teardown")), ctx, name, processed)
                if aborted:
                    break
                # CLEANUP: always repairs (drains an unfinished bonus -- free when there is none);
                # asserts the game is usable only when the suite asked for it (assert_base).
                try:
                    state = await se.ensure_base(ctx, None, f"cleanup[{name}]",
                                                 assert_base=want_assert_base)
                    if state != "clean":
                        print(f"  cleanup[{name}]: {state}")
                except Exception as e:  # noqa: BLE001
                    print(f"  !! cleanup failed for '{name}': {e} -- aborting suite")
                    processed.append({"name": name, "error": f"cleanup failed: {e}"})
                    break
                prev_last_bet = ctx.last_bet     # hand this run's server truth to the next PREPARE
            close_sections()
            # suite-end teardown: once, after ALL runs (skipped if a run-level teardown aborted)
            if not aborted and last_ctx is not None:
                await run_teardowns(suite_teardown, last_ctx, "suite", processed, suite=True)
            try:
                vpath = await page.video.path()
            except Exception:
                vpath = None
        except Exception as e:                                      # boot/session error must not skip summary
            print(f"\nsession error: {type(e).__name__}: {e}")
            close_sections()
        finally:
            await context.close(); await browser.close()

    # save ONE session video at the run root; every scenario references it (no per-run copies)
    session_path = root / "session.webm"
    if vpath and Path(vpath).exists():
        shutil.copy2(vpath, session_path)
        shutil.rmtree(Path(vpath).parent, ignore_errors=True)

    # AI video pass: trim each assert's sub-clip, analyse, grade -> pending becomes green/orange
    provider = None if args.no_ai else ai_video.make_video_provider(args.env)
    img_provider = None if args.no_ai else ai_video.make_image_provider(args.env)
    n_video = sum(ai_calls(p["cases"]) for p in processed if "run_dir" in p)
    if args.no_ai:
        print(f"\nAI: disabled (--no-ai) -- {n_video} video assert(s) stay pending")
    elif provider is None:
        print(f"\nAI: no provider available -- {n_video} video assert(s) stay pending "
              f"(check GEMINI_API_KEY / AI_PROVIDER in .env)")
    else:
        print(f"\nAI: {getattr(provider, 'model', type(provider).__name__)} -- "
              f"grading {n_video} AI call(s)")   # video clips AND image shots; ai_calls counts both

    graded = [p for p in processed if "run_dir" in p]
    for p in graded:
        cases = p["cases"]
        n_v = sum(1 for c in cases for v in c.get("video", []) if v.get("status") == "pending")
        # DEDUPED, like the grader itself: an `image:` block shared via `expected_by_case`
        # analyses each still once and re-grades the cached answer per case. Counting spec x shot
        # reported 54 for a 3-case tour of 18 screens that really makes 18 calls.
        n_shots = len({(sp.get("group") or f"{c['id']}-img{i}",
                        "__combined__" if sp.get("combine") else sh["file"])
                       for c in cases for i, sp in enumerate(c.get("image", []))
                       for sh in sp.get("shots", [])})
        n_img = sum(len(c.get("image", [])) for c in cases)
        if provider is not None and n_v:
            print(f"  {p['name']}: {n_v} video assert(s)")
        # An `image:` assert whose scenario captured nothing has zero shots -- the pass still runs
        # over it, to flag "no screenshots were captured" instead of leaving it PENDING forever,
        # which reads as "the AI did not run" rather than "this assert had nothing to grade".
        if img_provider is not None and n_img:
            if n_shots:
                print(f"  {p['name']}: {n_shots} screenshot(s)")
            else:
                print(f"  {p['name']}: {n_img} image assert(s) but the scenario captured NO "
                      f"screenshots -- add ctx.screenshot(...) to it, or use a `video:` assert")
    # ONE parallel pass over every run's AI calls (AI_CONCURRENCY at a time; see ai_video.run_ai).
    # `game` is what lets the model SEE the symbols: it resolves the asset manifest each video
    # assert is primed with (scoped per assert by `assets:` / `asset_groups:`).
    if provider is not None or img_provider is not None:
        t_ai = time.time()
        ai_video.run_ai(provider, img_provider, session_path,
                        [(p["run_dir"], p["cases"]) for p in graded], game=args.game)
        print(f"  AI pass took {time.time() - t_ai:.0f}s")

    for p in processed:
        if "run_dir" not in p:
            summary["runs"].append({"scenario": p["name"], "error": p.get("error"), "cases": []})
            print(f"{p['name']:<20} ERROR: {p.get('error')}")
            continue
        cases, result = p["cases"], p["result"]
        # `facts` and `marks` are saved so a verdict can be diagnosed AFTER the run. They were
        # computed and thrown away: when TC-086 went red, which ladder the reopened client had
        # loaded (`client_ladder`) was known during the run and unrecoverable afterwards.
        # `default=str` because a fact may hold a value json cannot encode as-is.
        (p["run_dir"] / "result.json").write_text(json.dumps({
            "kind": "run", "scenario": p["name"], "description": p["description"],
            "reached": result.reached, "reason": result.reason,
            "video_window": result.video_window, "shots": result.shots,
            "facts": getattr(result, "facts", None) or {},
            "marks": getattr(result, "marks", None) or {},
            "cases": cases,
        }, indent=2, default=str), encoding="utf-8")
        run_verdict = ("inconclusive" if not result.reached
                       else "red" if any(c["verdict"] == "red" for c in cases)
                       else "orange" if any(c["verdict"] == "orange" for c in cases)
                       else "pending" if any(c["verdict"] == "pending" for c in cases)
                       else "green")
        p["verdict"] = p["section"]["verdict"] = run_verdict
        summary["runs"].append({"scenario": p["name"], "reached": result.reached,
                                "cases": [{"id": c["id"], "verdict": c["verdict"]} for c in cases]})
        print(f"{p['name']:<20} {run_verdict}")
        for c in cases:
            vids, imgs = c["video"], c.get("image", [])
            vstat = f" video={sum(1 for v in vids if v.get('status') == 'pass')}/{len(vids)}" if vids else ""
            # IMAGE results were computed and folded into the verdict but never printed, so an
            # image-only run (language-tour, game help) reported "stream=1/1" and nothing else --
            # indistinguishable from "the AI never ran".
            istat = f" image={sum(1 for v in imgs if v.get('status') == 'pass')}/{len(imgs)}" if imgs else ""
            print(f"    {c['id']:<16} {c['verdict']:<12} "
                  f"stream={sum(1 for s in c['stream'] if s['result']['valid'])}/{len(c['stream'])}"
                  f"{vstat}{istat}")
            for v in vids:                                   # show each AI video assert's outcome
                answer = v.get("read") if v.get("kind") == "read" else v.get("data")
                detail = v.get("note") or (json.dumps(answer) if answer is not None else "")
                where = f"each: {v['each']}" if "each" in v else (v.get("at") or "all")
                print(f"        video[{where}] {v.get('status', '?'):<8} {detail}")
            for v in imgs:                                   # ...and each image assert's, per shot
                shot_results = v.get("shot_results") or []
                bad = [r for r in shot_results if r.get("status") != "pass"]
                print(f"        image[{len(shot_results)} shot(s)] {v.get('status', '?'):<8} "
                      f"{v.get('note') or ''}")
                for r in bad[:8]:                            # name the screens that actually failed
                    print(f"            {r.get('shot', '?')}: {r.get('note') or r.get('data')}")
                if len(bad) > 8:
                    print(f"            ... and {len(bad) - 8} more failing shot(s)")

    # Replays last: every one shows ALL runs as seek-bar chapters, coloured by verdict, so every
    # run has to be graded first.
    # gameInfo is SESSION state: a run that never reloads still plays on the payload of the last
    # load, which happened in an earlier run. So every replay carries every load's gameInfo (tagged
    # with the run that loaded it), not only its own -- otherwise most replays show none at all.
    session_info = [(k, {**e, "data": {**e["data"], "run": p["name"]}})
                    for k, p in enumerate(processed) if "run_dir" in p
                    for e in p.get("api_events") or [] if e["type"] == "game_info"]
    for k, p in enumerate(processed):
        if "run_dir" in p:
            own = p.get("api_events") or []
            others = [e for j, e in session_info if j != k]
            write_replay(p["run_dir"], p["name"], f"{p['name']} scenario", p["verdict"], p["result"],
                         p["duration_ms"], sections, p["section"], own + others)
    write_session_replay(root, summary["suite"], processed, sections)

    if provider is not None and hasattr(provider, "cost_summary"):
        summary["ai_cost"] = provider.cost_summary()

    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = write_report(root, summary["suite"], processed, summary.get("ai_cost", {}))
    print(f"\nreport  -> {report}")
    print(f"summary -> {root / 'summary.json'}")
    reds = sum(1 for r in summary["runs"] for c in r.get("cases", []) if c["verdict"] == "red")
    errs = sum(1 for r in summary["runs"] if r.get("error"))
    return 1 if (reds or errs) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
