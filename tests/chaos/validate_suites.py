"""Validate a game's suites WITHOUT a browser (session-summary-3 §6).

    python tests/chaos/validate_suites.py                # chaos, the default
    python tests/chaos/validate_suites.py --game zeus
    python tests/chaos/validate_suites.py --game mystic

Checks, in order of how quietly they fail in production:
  1. every `{facts.X}` a suite references is actually exposed by that run's scenario
     -- a typo'd fact resolves to the literal string "null" and the predicate QUIETLY PASSES,
        which is the single most dangerous failure mode in this framework
  2. every `scenario:` name resolves in scenario_engine.REGISTRY
  3. every named `stream:` invariant resolves in scenario_engine.INVARIANTS
  4. every `teardown:` name resolves in scenario_engine.TEARDOWNS, and every `params:` set is
     accepted by its scenario's constructor (the runner skips a run whose params it cannot apply)
  5. inline `stream:` predicates reference only bindable roots
  6. case ids are unique across the whole game and map to real matrix rows

WHY IT IS PER-GAME. Check 1 is resolved against the ACTIVE PROFILE, because a fact's existence can
depend on it (`GAME.mechanism` decides whether `spin-then-history` fans out a `cascade_win_*` set at
all). Running it as chaos and reading the result as if it covered Zeus would be the same class of
false green it exists to prevent -- so the profile, the suite directory and the case matrix all move
together, and nothing is hard-coded to one game any more.
"""
import ast, csv, io, os, re, sys, glob, argparse

REPO = r"D:\Project\qa-platform"
sys.path.insert(0, os.path.join(REPO, "scripts"))
import scenario_engine as se  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("pyyaml not installed")


# --------------------------------------------------------------------- per-game configuration
# `matrix`  the QA case list, for the "does this id exist / is it covered" report. A .csv is read
#           directly; an .xlsx needs openpyxl and is SKIPPED with a warning if it is missing, since
#           coverage reporting is a nicety and the six correctness checks are not.
# `prefix`  the id prefix a case is expected to carry, so a stray id from another game is caught.
# `skip`    files in the suite directory that are NOT part of the suite: scratch pads (every game
#           has one -- mystic/test.yml, zeus/temp.yml) which duplicate real ids on purpose while
#           someone iterates, and legacy `groups:`/`steps:` bridge-script suites kept for reference.
GAMES = {
    "chaos":  {"matrix": "test-case-chaos.csv",   "prefix": "CR_",
               "skip": {"test.yml", "temp.yml"}},
    "mystic": {"matrix": "test-case-mahjong.csv", "prefix": "Mah_",
               "skip": {"test.yml", "temp.yml", "raw.yml", "scenario-demo.yml"}},
    "zeus":   {"matrix": "test-case-zeus.xlsx",   "prefix": "ZZ-",
               # raw.yml / vocab.yml / freespin-runs.yml are the pre-scenario `groups:`+`steps:`
               # format and reference bridge analysers the v2 bridge dropped; they are history, not
               # suites, and the cases in them live in zz-holdspin.yml now.
               "skip": {"test.yml", "temp.yml", "raw.yml", "vocab.yml", "freespin-runs.yml"}},
    # A Zeus fork with the same design doc, so its cases MIRROR test-case-zeus.xlsx one for one
    # (DR-NN == ZZ-NN). It has no matrix of its own yet, so coverage is reported as unknown.
    "dragon": {"matrix": "test-case-dragon.xlsx", "prefix": "DR-",
               "skip": {"test.yml", "temp.yml"}},
    # Dragon plus Ante Bet and Premium Reel. DV-01..52 mirror DR-NN one for one; DV-53+ are the two
    # features Dragon does not have. No matrix of its own yet, so coverage is reported as unknown.
    "dragon-vip": {"matrix": "test-case-dragon-vip.xlsx", "prefix": "DV-",
                   "skip": {"test.yml", "temp.yml"}},
    # Joker: Where's My Banana — a LINE game like Chaos, plus expanding wilds, a Buy Bonus with a
    # Lucky Spin wheel, and a free-spin multiplier. No matrix of its own yet, so coverage is
    # reported as unknown and the JK- ids below are this repo's own numbering.
    "joker":  {"matrix": "test-case-joker.xlsx", "prefix": "JK-",
               "skip": {"test.yml", "temp.yml"}},
    # Grandma Pets — a 1024-WAYS game (the first in this repo), with a wild multiplier, walking
    # wilds in free spins, a Buy Bonus, a Free Spin Extension and the Cat vs Dog drama. Its matrix
    # is the CSV the game team supplied, so the ids here are TC-NNN from that file. Where one
    # matrix row needs more than one play-through (TC-103's 3/4/5-reel awards, TC-131 and TC-133's
    # two glow fixtures) the cases carry a letter suffix — TC-103a/b/c — because a case id is the
    # unit a verdict and `--test` address, and must be unique suite-wide.
    "grandma": {"matrix": "test-case-grandma.csv", "prefix": "TC-",
                "skip": {"test.yml", "temp.yml"}},
}

ERRORS, WARNINGS = [], []


# ---------------------------------------------------------------- facts, statically
# Helpers whose result is spread into `facts` with `**`. Their keys cannot be read statically
# from the calling scenario, so they are declared here — see docs/scenario-authoring-rules.md.
SPREAD_HELPERS = {
    "confirm_stopped": {"stopped", "extra_round_after_stop", "auto_flag_after",
                        "auto_flag_cleared_s", "auto_spins_left_after", "stop_watch_s"},
    "settle_to_base": {"returned_to_base", "celebration_shown", "celebration_auto_closed",
                       "popups_at_end", "base_ready_seconds", "bet_after_return",
                       "base_watch_s", "base_hold_s"},
}


# --------------------------------------------------------------- shot-producing scenarios
# An `image:` assert grades one AI call PER CAPTURED STILL. A scenario that captures none gives it
# nothing to grade — and the assert sits at `pending` forever rather than reporting. So: which
# scenarios can actually produce a shot?
#
# Capture is often INDIRECT — `tour_ui(shots=True)` and the local `_hover_ante()` both call
# `ctx.screenshot()` on the scenario's behalf — so a plain "does this class body call screenshot?"
# test false-positives on `language-tour` and `ante-freespins`. This resolves module-level helpers
# transitively.
SHOT_CALL = "ctx.screenshot("


def _shot_producing_helpers(tree, src):
    """Module-level functions that (transitively) take a screenshot."""
    bodies = {n.name: (ast.get_source_segment(src, n) or "")
              for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    shooters = {n for n, b in bodies.items() if SHOT_CALL in b}
    for _ in range(5):                       # transitive closure; depth 5 is ample
        grew = {n for n, b in bodies.items() if any(h + "(" in b for h in shooters)}
        if grew <= shooters:
            break
        shooters |= grew
    shooters.add("tour_ui")                  # a DriveContext method, not module level
    return shooters


def scenarios_with_shots(path):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    shooters = _shot_producing_helpers(tree, src)
    out = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.ClassDef):
            continue
        nm = next((b.value.value for b in n.body if isinstance(b, ast.Assign)
                   and getattr(b.targets[0], "id", None) == "name"
                   and isinstance(b.value, ast.Constant)), None)
        if not nm:
            continue
        seg = ast.get_source_segment(src, n) or ""
        captures = SHOT_CALL in seg or any(h + "(" in seg for h in shooters)
        returns = "shots=ctx.shots" in seg
        out[nm] = (captures, returns)
    return out


def scenario_facts(path):
    """name -> set of fact keys, read from the `facts={...}` / facts.update({...}) literals."""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef)):
            continue
        name = None
        for st in node.body:
            if isinstance(st, ast.Assign):
                for t in st.targets:
                    if isinstance(t, ast.Name) and t.id == "name" and isinstance(st.value, ast.Constant):
                        name = st.value.value
        if not name:
            continue
        keys = set()
        for sub in ast.walk(node):
            # facts={...} keyword, or a `facts = {...}` local, or facts.update({...})
            if isinstance(sub, ast.Dict):
                for k in sub.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
        # Facts spread in with `**` are INVISIBLE to the walk above — the keys live in the helper's
        # return value, not in any literal here. Without this, a scenario that does
        # `facts={..., **confirm_stopped(...)}` looks like it exposes nothing of the sort, and every
        # correct Rule 4 assert gets reported as an error (and, worse, a TYPO in one of those five
        # names would never be caught). Each helper below has a documented, fixed return contract.
        src_seg = ast.get_source_segment(src, node) or ""
        for helper, contributed in SPREAD_HELPERS.items():
            if helper + "(" in src_seg:
                keys |= contributed
        # Facts built with an f-string key -- `facts[f"{key}_stake"]` -- are invisible the same way:
        # the key is a JoinedStr, not a string literal. A scenario that fans facts out per outcome
        # declares its prefixes in FACT_PREFIXES; here the SUFFIXES are read from the source and
        # crossed with them. That keeps the check honest: an unknown prefix or a misspelled suffix
        # still has no match and is still reported.
        suffixes = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                for k in sub.keys:
                    if isinstance(k, ast.JoinedStr):
                        tail = "".join(v.value for v in k.values
                                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
                        if tail.startswith("_"):
                            suffixes.add(tail)
        for prefix in getattr(getattr(se, node.name, None), "FACT_PREFIXES", ()) or ():
            keys |= {prefix + suf for suf in suffixes}
        out[name] = keys
    return out


ENGINE = os.path.join(REPO, "scripts", "scenario_engine.py")
FACTS: dict = {}      # filled in main(), once the profile is set
SHOTS: dict = {}

# Facts every scenario gets for free from ScenarioResult / the runner.
UNIVERSAL = set()
# Mirrors scenario_engine._TOKEN: names may carry digits after the first character
# (`{facts.p1_ways}`). A narrower pattern here silently SKIPS those references, so a typo
# in one would sail through the very check this file exists to make.
TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)(?:\[(\d+)\])?\.([a-zA-Z_][a-zA-Z_0-9]*)\}")
BINDABLE_ROOTS = {"res", "trigger", "terminal", "rounds", "facts", "prev", "shot"}
BET_FIELDS = {f.name for f in __import__("dataclasses").fields(se.Bet)} | {"row"}


def read_matrix(name):
    """Case ids from the QA matrix. Returns (ids, note) -- `note` is non-empty when the matrix
    could not be read, in which case coverage is reported as unknown rather than as zero."""
    path = os.path.join(REPO, name)
    if not os.path.exists(path):
        return set(), "%s not found" % name
    if name.lower().endswith(".csv"):
        with io.open(path, encoding="utf-8-sig") as f:
            return {r["ID"].strip() for r in csv.DictReader(f) if r.get("ID")}, ""
    try:
        import openpyxl
    except ImportError:
        return set(), "%s needs openpyxl to read (pip install openpyxl)" % name
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    rows = ws.iter_rows(min_row=2, max_col=1, values_only=True)
    return {str(r[0]).strip() for r in rows if r and r[0]}, ""


def walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_strings(v)


def main(game):
    global FACTS, SHOTS
    se.set_profile(game)                       # BEFORE the scan: fact fan-out can be profile-shaped
    FACTS = scenario_facts(ENGINE)
    SHOTS = scenarios_with_shots(ENGINE)

    cfg = GAMES[game]
    matrix_ids, matrix_note = read_matrix(cfg["matrix"])
    if matrix_note:
        WARNINGS.append("coverage not reported: " + matrix_note)

    suites = [f for f in sorted(glob.glob(os.path.join(REPO, "games", game, "test-suites", "*.yml")))
              if os.path.basename(f) not in cfg["skip"]]
    if not suites:
        sys.exit("no %s suites found" % game)

    seen_ids, covered = {}, set()
    total_cases = 0

    for path in suites:
        short = os.path.basename(path)
        doc = yaml.safe_load(io.open(path, encoding="utf-8"))
        if not doc or "runs" not in doc:
            ERRORS.append("%s: no `runs:` key" % short)
            continue
        run_names: dict = {}
        for i, entry in enumerate(doc["runs"]):
            run = entry.get("run") if "run" in entry else entry
            sname = run.get("scenario")
            where = "%s run[%d] (%s)" % (short, i, sname)

            # (1b) run `name:` — what `--only` selects and what the result folder is called. Must be
            # unique in the suite (a repeat would make `--only` pick two runs) and folder-safe.
            rname = run.get("name")
            if rname is not None:
                if not (isinstance(rname, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", rname)):
                    ERRORS.append("%s: run name %r must be lower-case letters, digits, '-', '_' or "
                                  "'.' (it becomes a folder name)" % (where, rname))
                elif rname in run_names:
                    ERRORS.append("%s: run name '%s' is already used by run[%d] -- names must be "
                                  "unique in a suite" % (where, rname, run_names[rname]))
                else:
                    run_names[rname] = i

            # (2) scenario resolves
            # `video.at` is either one target or a SPAN OF EXACTLY TWO (resolve_window takes
            # at[0] and at[1]). A 3-element list is not a wider span: it falls through to
            # `at in result.marks`, which raises TypeError on an unhashable list -- at run time,
            # after the run has already played.
            # A field the prompt ASKS FOR that no case grades. Sometimes deliberate — a discovery
            # field records what a green run saw without being able to fail it (joker's
            # `multiplier_text`) — so this WARNS rather than errors. But it is also what an
            # oversight looks like: the model is asked to read a value, spends tokens on it, and
            # the answer is thrown away. Grade it, or say in a comment why it is only recorded.
            # Every SHARED video block of the run: `video:` (one) and `videos:` (a list, one block
            # per question about a different moment). Each gets the same checks.
            if run.get("videos") is not None and not isinstance(run.get("videos"), list):
                ERRORS.append("%s: `videos:` must be a LIST of video blocks" % where)
            blocks = ([("video", run["video"])] if isinstance(run.get("video"), dict) else []) + \
                     [("videos[%d]" % k, b) for k, b in enumerate(run.get("videos") or [])
                      if isinstance(b, dict)]
            for label, vid in blocks:
                # A `{facts.x}` in the PROMPT that the scenario does not expose renders the literal
                # "null" into the text the model reads — so it is told the bet is null and asked to
                # grade the screen against that. Same silent class as the assert version, and
                # invisible on review because the prompt still reads perfectly well in the file.
                if vid.get("prompt") and sname in FACTS:
                    known = FACTS.get(sname, set()) | UNIVERSAL
                    for tok in sorted(set(re.findall(r"\{facts\.([a-z_0-9]+)\}", vid["prompt"]))):
                        if tok not in known:
                            ERRORS.append("%s %s: the prompt uses '{facts.%s}', which scenario '%s' "
                                          "does not expose -- it would render as the literal 'null'"
                                          % (where, label, tok, sname))
                if vid.get("expected_by_case"):
                    asked = set((vid.get("output_schema") or {}).keys()) - {"notes", "confidence", "reason"}
                    graded = {k for exp in vid["expected_by_case"].values() for k in (exp or {})}
                    if asked - graded:
                        WARNINGS.append("%s %s: video asks for %s, which no case grades -- grade it, "
                                        "or note why it is discovery-only"
                                        % (where, label, ", ".join(sorted(asked - graded))))
                at = vid.get("at")
                if isinstance(at, (list, tuple)) and len(at) != 2:
                    ERRORS.append("%s %s: video.at is a list of %d -- a span must be exactly 2 "
                                  "targets (got %r)" % (where, label, len(at), list(at)))
                # `each:` = one clip per round of a LIST mark. It replaces `at:` (the windows come
                # from the mark), so both at once means one of them is silently ignored.
                if vid.get("each") is not None:
                    if not isinstance(vid["each"], str):
                        ERRORS.append("%s %s: video.each must be ONE mark name (got %r)"
                                      % (where, label, vid["each"]))
                    if vid.get("at") is not None:
                        ERRORS.append("%s %s: video has both `each:` and `at:` -- `each` takes its "
                                      "clips from the mark, so `at` would be ignored" % (where, label))
                mr = vid.get("max_rounds")
                if mr is not None:
                    if vid.get("each") is None:
                        ERRORS.append("%s %s: `max_rounds` only applies to an `each:` block -- "
                                      "here it would be ignored" % (where, label))
                    if not (isinstance(mr, int) and not isinstance(mr, bool) and mr > 0):
                        ERRORS.append("%s %s: `max_rounds` must be a positive whole number (got %r)"
                                      % (where, label, mr))
            if sname not in se.REGISTRY:
                ERRORS.append("%s: unknown scenario '%s'" % (where, sname))
                continue
            exposed = FACTS.get(sname, set()) | UNIVERSAL

            # (4) teardown resolves
            td = run.get("teardown")
            for t in ([td] if isinstance(td, str) else (td or [])):
                if t and t not in se.TEARDOWNS:
                    ERRORS.append("%s: unknown teardown '%s'" % (where, t))

            # (4b) params are accepted by the scenario. The runner builds `type(scenario)(**params)`
            # and SKIPS the run on a TypeError, so a renamed or removed parameter otherwise shows up
            # only at runtime, as a run that silently never happened.
            params = run.get("params") or {}
            if params:
                try:
                    type(se.REGISTRY[sname])(**params)
                except TypeError as e:
                    ERRORS.append("%s: bad params %s -- %s" % (where, sorted(params), e))

            for case in run.get("cases") or []:
                cid = case.get("id")
                total_cases += 1
                if cid in seen_ids:
                    ERRORS.append("duplicate case id %s (%s and %s)" % (cid, seen_ids[cid], short))
                seen_ids[cid] = short
                # A suite id may be a MERGE of matrix rows ("Mah_014_019" covers Mah_014 AND
                # Mah_019) or carry a variant suffix ("CR_020a"), so it is expanded into every
                # matrix row it claims -- otherwise a legitimately merged case reads as an id
                # nobody wrote, and the coverage number quietly undercounts.
                claims = [cfg["prefix"] + n for n in re.findall(r"\d+", str(cid))]
                covered |= {c for c in claims if c in matrix_ids}
                if not str(cid).startswith(cfg["prefix"]):
                    WARNINGS.append("%s: case id %s is not a %s id" % (short, cid, cfg["prefix"]))
                elif matrix_ids and not any(c in matrix_ids for c in claims):
                    WARNINGS.append("%s: case id %s has no row in %s" % (short, cid, cfg["matrix"]))

                for a in case.get("assert") or []:
                    # (7) an `image:` assert needs stills to grade — and the scenario must both
                    # CAPTURE them and RETURN them via `shots=ctx.shots`. Miss either and the
                    # assert silently stays `pending`: it reads as "the AI never ran" rather than
                    # "this assert had nothing to look at".
                    if isinstance(a, dict) and "image" in a:
                        captures, returns = SHOTS.get(sname, (False, False))
                        if not captures:
                            ERRORS.append(
                                "%s / %s: `image:` assert but scenario '%s' captures NO "
                                "screenshots — add ctx.screenshot(...) to it, or use `video:`"
                                % (where, cid, sname))
                        elif not returns:
                            ERRORS.append(
                                "%s / %s: scenario '%s' captures stills but never returns them "
                                "(ScenarioResult(..., shots=ctx.shots)) — the image assert would "
                                "grade nothing" % (where, cid, sname))
                    # (3) named invariants
                    if isinstance(a, dict) and isinstance(a.get("stream"), str):
                        s = a["stream"].strip()
                        if re.fullmatch(r"[a-z_][a-z0-9_]*(\(.*\))?", s):
                            iname = s.split("(")[0]
                            if iname not in se.INVARIANTS:
                                ERRORS.append("%s / %s: unknown invariant '%s'" % (where, cid, iname))
                    # (1)+(5) every bound token
                    for text in walk_strings(a):
                        for root, idx, field in TOKEN.findall(text):
                            if root not in BINDABLE_ROOTS:
                                ERRORS.append("%s / %s: '{%s.%s}' — '%s' is not a bindable root %s"
                                              % (where, cid, root, field, root, sorted(BINDABLE_ROOTS)))
                            elif root == "facts":
                                if field not in exposed:
                                    ERRORS.append(
                                        "%s / %s: '{facts.%s}' is NOT exposed by scenario '%s' "
                                        "— it would resolve to the literal 'null' and the assert "
                                        "would quietly PASS. Exposed: %s"
                                        % (where, cid, field, sname, sorted(exposed)))
                            elif root in ("res", "trigger", "terminal", "rounds"):
                                if field not in BET_FIELDS:
                                    ERRORS.append("%s / %s: '{%s.%s}' is not a Bet field %s"
                                                  % (where, cid, root, field, sorted(BET_FIELDS)))

    print("game     : %s" % game)
    print("suites   : %d" % len(suites))
    print("cases    : %d  (unique ids: %d)" % (total_cases, len(seen_ids)))
    print("matrix   : %d rows covered of %d (%s)" % (len(covered), len(matrix_ids), cfg["matrix"]))
    print()
    if WARNINGS:
        print("WARNINGS (%d):" % len(WARNINGS))
        for w in WARNINGS:
            print("  ~", w)
        print()
    if ERRORS:
        print("ERRORS (%d):" % len(ERRORS))
        for e in ERRORS:
            print("  !", e)
        sys.exit(1)
    print("OK — every scenario, invariant, teardown and {facts.*} reference resolves.")


ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--game", default="chaos", choices=sorted(GAMES),
                help="which game's suites to validate (default: chaos)")
main(ap.parse_args().game)
