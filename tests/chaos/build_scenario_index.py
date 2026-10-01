"""Regenerate the data behind docs/scenario-index.md, straight from the engine and the suites.

The index is a lookup table people trust when onboarding a game, so it must not be written from
memory. Run this after adding a scenario or an invariant and reconcile any drift it reports.
"""
import ast, glob, io, json, os, re, sys

REPO = r"D:\Project\qa-platform"
sys.path.insert(0, os.path.join(REPO, "scripts"))
import scenario_engine as se  # noqa
import yaml

ENGINE = os.path.join(REPO, "scripts", "scenario_engine.py")
src = io.open(ENGINE, encoding="utf-8").read()
tree = ast.parse(src)

SPREAD = {"confirm_stopped": {"stopped", "extra_round_after_stop", "auto_flag_after",
                              "auto_spins_left_after", "stop_watch_s"}}
SHOT_CALL = "ctx.screenshot("
bodies = {n.name: (ast.get_source_segment(src, n) or "")
          for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
shooters = {n for n, b in bodies.items() if SHOT_CALL in b}
for _ in range(5):
    shooters |= {n for n, b in bodies.items() if any(h + "(" in b for h in shooters)}
shooters.add("tour_ui")

info = {}
for n in ast.walk(tree):
    if not isinstance(n, ast.ClassDef):
        continue
    nm = next((b.value.value for b in n.body if isinstance(b, ast.Assign)
               and getattr(b.targets[0], "id", None) == "name"
               and isinstance(b.value, ast.Constant)), None)
    if not nm or nm not in se.REGISTRY:
        continue
    seg = ast.get_source_segment(src, n) or ""
    facts = set()
    for d in ast.walk(n):
        if isinstance(d, ast.Dict):
            for k in d.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    facts.add(k.value)
    for h, keys in SPREAD.items():
        if h + "(" in seg:
            facts |= keys
    marks = set(re.findall(r'marks=\{["\']([a-z_]+)["\']', seg)) | set(
        re.findall(r'marks\[["\']([a-z_]+)["\']\]', seg))
    caps = sorted(set(re.findall(r"GAME\.(HAS_[A-Z_]+)", seg)))
    gamefields = sorted(set(re.findall(r"GAME\.([A-Za-z_]+)", seg)) - set(caps))
    ctor = {}
    for b in n.body:
        if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name == "__init__":
            args = [a.arg for a in b.args.args if a.arg != "self"]
            defs = [ast.unparse(d) for d in b.args.defaults]
            ctor = dict(zip(args[len(args) - len(defs):], defs))
    info[nm] = {
        "class": n.name,
        "desc": (se.REGISTRY[nm].description or "").strip(),
        "params": ctor,
        "facts": sorted(facts),
        "marks": sorted(marks),
        "shots": SHOT_CALL in seg or any(h + "(" in seg for h in shooters),
        "returns_shots": "shots=ctx.shots" in seg,
        "bets": ("base_spin(" in seg or "observe_bet(" in seg or "spin_and_finish(" in seg
                 or "play_out_freespins(" in seg),
        "caps": caps,
        "game_fields": gamefields,
        "confirm_stopped": "confirm_stopped(" in seg,
    }

# who uses what
use = {}
for game in ("mystic", "zeus", "dragon", "dragon-vip", "chaos"):
    for f in glob.glob(os.path.join(REPO, "games", game, "test-suites", "*.yml")):
        if os.path.basename(f) in ("test.yml", "temp.yml", "raw.yml"):
            continue
        d = yaml.safe_load(io.open(f, encoding="utf-8"))
        if not d or "runs" not in d:
            continue
        for e in d["runs"]:
            r = e.get("run", e)
            s = r.get("scenario")
            if not s:
                continue
            ids = [c["id"] for c in (r.get("cases") or [])]
            use.setdefault(s, {}).setdefault(game, []).extend(ids)

for k, v in info.items():
    v["used_by"] = use.get(k, {})

out = os.path.join(REPO, "tests", "chaos", "scenario-index-data.json")
io.open(out, "w", encoding="utf-8").write(json.dumps(info, indent=1))
print("wrote", out)
print("scenarios:", len(info))
print("unused   :", sorted(k for k in info if not info[k]["used_by"]))
print("capability guards seen:", sorted({c for v in info.values() for c in v["caps"]}))

# ---- reconcile against the doc ----------------------------------------------------------
doc_path = os.path.join(REPO, "docs", "scenario-index.md")
doc = io.open(doc_path, encoding="utf-8").read()
err = []
for name in se.REGISTRY:
    if "`%s`" % name not in doc:
        err.append("scenario %r is missing from docs/scenario-index.md" % name)
for name in se.INVARIANTS:
    if "`%s`" % name not in doc:
        err.append("invariant %r is missing from §5" % name)
KNOWN = {"return-to-base", "deposit-10000", "bet-response", "scenario-index",
         "game-bridge-contract", "scenario-authoring-rules", "scenario-assert-authoring",
         "hold-spin", "test-suites", "bet-responses"}
for m in set(re.findall(r"`([a-z0-9][a-z0-9-]{3,})`", doc)):
    if "-" in m and m not in se.REGISTRY and m not in se.INVARIANTS and m not in KNOWN:
        err.append("the doc names %r, which is neither a scenario nor an invariant" % m)
for line in doc.split("\n"):
    m = re.match(r"^\| `([a-z0-9-]+)` \|", line)
    if not m or m.group(1) not in info:
        continue
    for f in re.findall(r"`([a-z_]+(?:/[a-z_]+)*)`", line.split("|")[-2]):
        for part in f.split("/"):
            if part not in info[m.group(1)]["facts"] and not any(part in x for x in info[m.group(1)]["facts"]):
                err.append("%s: the doc quotes fact %r, which it does not expose" % (m.group(1), part))
# ---- the toolkit doc must name every public helper -------------------------------------
# docs/scenario-toolkit.md is what an author (or an agent) reads to learn what already exists. A
# helper missing from it gets re-implemented, usually without the rule it encodes -- the waste this
# check exists to prevent. Invariants are exempt: they are listed in scenario-index.md section 5.
tk_path = os.path.join(REPO, "docs", "scenario-toolkit.md")
tk = io.open(tk_path, encoding="utf-8").read() if os.path.exists(tk_path) else ""
if not tk:
    err.append("docs/scenario-toolkit.md is missing")
else:
    import ast as _ast
    _src = io.open(ENGINE, encoding="utf-8").read()
    _tree = _ast.parse(_src)
    public = set()
    for n in _tree.body:
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and not n.name.startswith("_"):
            public.add(n.name)
        if isinstance(n, _ast.ClassDef) and n.name == "DriveContext":
            public |= {m.name for m in n.body
                       if isinstance(m, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                       and not m.name.startswith("_")}
        if isinstance(n, _ast.ClassDef) and n.name == "Unreached":
            public.add(n.name)
    public -= set(se.INVARIANTS)
    # Identifier tokens from the WHOLE doc, not from backtick spans: pairing
    # backticks is parity-sensitive, and one fenced block or a backtick inside
    # an f-string flips every span after it -- that false-flagged ~75 helpers.
    # The doc is a curated reference, so any mention of a name documents it.
    spans = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", tk))
    for name in sorted(public):
        if name not in spans:
            err.append("helper %r is missing from docs/scenario-toolkit.md" % name)
head = "%d scenarios · %d invariants" % (len(se.REGISTRY), len(se.INVARIANTS))
if head not in doc:
    err.append("header count is stale — should read '%s'" % head)
print()
if err:
    print("DRIFT (%d):" % len(err))
    for e in sorted(set(err)):
        print("  !", e)
    sys.exit(1)
print("docs/scenario-index.md and docs/scenario-toolkit.md are in sync with the engine.")
