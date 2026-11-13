#!/usr/bin/env python3
"""Every string key of every dict literal inside each scenario class of scripts/scenario_engine.py,
plus each class's name/description/__init__ defaults. The TS port must expose the same fact names
(test/scenario-keys.test.ts compares). Run from the repo root; writes baseline/scenario_keys.json."""
import ast
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
src = open(os.path.join(ROOT, "scripts", "scenario_engine.py"), encoding="utf-8").read()
tree = ast.parse(src)
out = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.ClassDef):
        continue
    name = desc = None
    for st in node.body:
        if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name):
            if st.targets[0].id == "name" and isinstance(st.value, ast.Constant):
                name = st.value.value
            if st.targets[0].id == "description":
                try:
                    desc = ast.literal_eval(st.value)
                except ValueError:
                    desc = None
    if not name:
        continue
    keys = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for k in sub.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    params = {}
    for st in node.body:
        if isinstance(st, ast.FunctionDef) and st.name == "__init__":
            args = st.args.args[1:]
            defaults = st.args.defaults
            pad = [None] * (len(args) - len(defaults)) + list(defaults)
            for a, d in zip(args, pad):
                try:
                    params[a.arg] = ast.literal_eval(d) if d is not None else "<required>"
                except ValueError:
                    params[a.arg] = "<expr>"
            if st.args.kwarg is not None:
                params["**" + st.args.kwarg.arg] = "<kwargs>"
    out[name] = {"class": node.name, "keys": sorted(keys), "description": desc, "params": params}
with open(os.path.join(ROOT, "scenario-runner", "baseline", "scenario_keys.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1, default=list)
print(f"{len(out)} scenarios")
