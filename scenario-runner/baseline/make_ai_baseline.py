#!/usr/bin/env python3
"""Record what the PYTHON AI pass answers, so the TypeScript port can be held to it.

Covers scripts/ai_video.py and framework/helpers/{gemini, gemini_video, gemini_pricing, claude_ai,
local_ai, symbol_reference}.py. Writes scenario-runner/baseline/ai.json; scenario-runner/test/
ai.test.ts replays the same inputs through src/services/ai/ and must match.

Run from the repo root:  python scenario-runner/baseline/make_ai_baseline.py

No AI provider and no network is ever called: the Gemini client, `requests.post`,
`subprocess.run` / `Popen` (ffmpeg, ffprobe, the claude CLI) and `time.sleep` are stood in for by
recorders, and sockets are blocked. Every case records its INPUT next to its output (the stand-ins
are driven by data in the input), so the TS test is generic: it reads both.

Paths under the scratch dir and the repo root are written as <TMP> / <ROOT>. A raise is recorded
as {"err": "<ExceptionType>", "msg": str(exc)}.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest import mock

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _no_network(*_a, **_k):
    raise RuntimeError("the AI baseline must not touch the network")


socket.socket.connect = _no_network          # belt and braces: every provider is stubbed below
socket.create_connection = _no_network

import framework.env_files as env_files  # noqa: E402

env_files.load_env_files = lambda env, base_dir=None: []     # never read the real .env files

import ai_video  # noqa: E402
import requests  # noqa: E402
from google import genai  # noqa: E402
from PIL import Image  # noqa: E402

from framework.helpers import claude_ai, gemini, gemini_pricing, gemini_video, local_ai  # noqa: E402
from framework.helpers import symbol_reference as sr  # noqa: E402

TMP = os.path.realpath(tempfile.mkdtemp(prefix="aibase-"))
BASE = Path(ROOT)

# ── serialisation ────────────────────────────────────────────────────────────────────


def plain(v):
    """JSON-safe copy: sets sorted, tuples listed, non-finite floats tagged, Paths as str."""
    if isinstance(v, float) and not math.isfinite(v):
        return {"__float__": repr(v)}
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, (set, frozenset)):
        return sorted((plain(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    return v


def _variants(p):
    return {p, p.replace("\\", "\\\\"), p.replace("\\", "/")}


_SUBS = sorted([(v, "<TMP>") for v in _variants(TMP)] + [(v, "<ROOT>") for v in _variants(ROOT)],
               key=lambda s: len(s[0]), reverse=True)


def norm(v):
    """Machine paths -> <TMP> / <ROOT> (also in their repr-escaped and forward-slash spellings)."""
    if isinstance(v, str):
        for a, b in _SUBS:
            v = v.replace(a, b)
        return v
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    return v


def rec(fn, *args, **kwargs):
    try:
        return {"ok": norm(plain(fn(*args, **kwargs)))}
    except Exception as e:  # noqa: BLE001 -- the exception TYPE (and text) is the recorded answer
        return {"err": type(e).__name__, "msg": norm(str(e))}


def tmp(*parts):
    p = os.path.join(TMP, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def real(s):
    """'<TMP>/a/b' / '<ROOT>/a/b' -> a real path."""
    if isinstance(s, str):
        for k, v in (("<TMP>", TMP), ("<ROOT>", ROOT)):
            if s.startswith(k):
                return v + s[len(k):].replace("/", os.sep)
    return s


def write(name, data):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        fh.write("\n")
    print(f"wrote {os.path.relpath(path, ROOT)}")


# ── environment ──────────────────────────────────────────────────────────────────────

ENV_KEYS = ["AI_PROVIDER", "AI_CONCURRENCY", "GEMINI_API_KEY", "GEMINI_VIDEO_MODEL",
            "GEMINI_VIDEO_RESOLUTION", "GEMINI_IMAGE_MODEL", "CLAUDE_AI_FPS", "CLAUDE_AI_MAX_FRAMES",
            "CLAUDE_AI_TOKEN_BUDGET", "CLAUDE_CLI_PATH", "CLAUDE_MODEL", "CLAUDE_CLI_TIMEOUT",
            "LOCAL_AI_ENDPOINT", "AGENT_REASONING", "AGENT_MODEL", "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT", "BASELINE_OTHER"]


@contextlib.contextmanager
def env(values=None):
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in (values or {}).items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def lines(buf):
    return norm(buf.getvalue().splitlines())


# ── fixture files (created identically by the TS test) ───────────────────────────────

C = chr
FILES = {
    # stills (real image bytes, copied from the repo)
    "img/shot.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/A.png"},
    "img/extra1.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/J.png"},
    "img/extra2.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/K.png"},
    "img/photo.jpg": {"copy": "<ROOT>/games/zeus/docs/assets/symbols/Coin.jpg"},
    "img/bad.png": {"text": "this is not an image"},
    # a synthetic asset manifest exercising every branch of the loader
    "games/synth/docs/assets/assets.yml": {"text": "\n".join([
        "meta: just a scalar",
        "version: 3",
        "symbols:",
        "  alpha:",
        "    file: sym/a.png",
        "    name: Alpha",
        "    description: \"first one " + C(0x2014) + " with a dash\"",
        "  beta:",
        "    file: sym/missing.png",
        "    name: Beta",
        "  gamma:",
        "    name: NoFile",
        "    description: no file field",
        "  delta: not-a-dict",
        "  eps:",
        "    file: ./sym/./e.png",
        "    description: \"\"",
        "  dup:",
        "    file: sym/a.png",
        "    name: Duplicate of alpha",
        "  bad:",
        "    file: sym/bad.png",
        "    name: Broken image",
        "ui_elements: {}",
        "ui:",
        "  screen:",
        "    file: ui/screen.png",
        "    name: Whole screen",
        "    description: the base game",
        "  dup:",
        "    file: ui/screen.png",
        "    name: Screen again",
        "",
    ])},
    "games/synth/docs/assets/sym/a.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/P1.png"},
    "games/synth/docs/assets/sym/e.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/P2.png"},
    "games/synth/docs/assets/sym/bad.png": {"text": "garbage bytes"},
    "games/synth/docs/assets/ui/screen.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/P3.png"},
    "games/legacy/docs/assets/symbols.yml": {"text": "symbols:\n  one:\n    file: o.png\n    name: One\n"},
    "games/legacy/docs/assets/o.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/Q.png"},
    "games/badyaml/docs/assets/assets.yml": {"text": "symbols: [unclosed\n"},
    "games/listroot/docs/assets/assets.yml": {"text": "- a\n- b\n"},
    "games/emptyfile/docs/assets/assets.yml": {"text": ""},
    "games/intname/docs/assets/assets.yml": {"text": "symbols:\n  x:\n    file: x.png\n    name: 7\n"},
    "games/intname/docs/assets/x.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/Q.png"},
    "games/nodesc/docs/assets/assets.yml": {"text": "symbols:\n  x:\n    file: x.png\n    description: 42\n"},
    "games/nodesc/docs/assets/x.png": {"copy": "<ROOT>/games/grandma/docs/assets/symbols/Q.png"},
    "session.webm": {"text": "not really a video"},
}


def make_files():
    for rel, spec in FILES.items():
        p = tmp(*rel.split("/"))
        if "copy" in spec:
            shutil.copyfile(real(spec["copy"]), p)
        else:
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(spec["text"])


# ── stand-ins ────────────────────────────────────────────────────────────────────────

def _capture_missing_exe():
    try:
        subprocess.run(["definitely-not-a-program-aibase"], capture_output=True)
    except FileNotFoundError as e:
        return e
    raise SystemExit("expected FileNotFoundError")


MISSING_EXE = _capture_missing_exe()


def ref_from(spec):
    """A reference pack described in a case input: {game, base, groups?, assets?, full?}."""
    if spec is None:
        return None
    base = Path(real(spec.get("base", "<ROOT>")))
    if spec.get("full"):
        return sr.load_symbol_reference(spec["game"], base, spec.get("categories"))
    return sr.load_reference_selection(spec["game"], base, spec.get("groups"), spec.get("assets"))


def ref_summary(ref):
    if ref is None:
        return None
    return norm({"header": ref.header, "items": [[str(p), label] for p, label in ref.items],
                 "is_empty": ref.is_empty})


def program(cmd):
    name = os.path.basename(str(cmd[0])).lower()
    for p in ("ffmpeg", "ffprobe"):
        if name.startswith(p):
            return p
    return "cli"


def hashed_input(text):
    """The stdin a call got: a stream-json user message has each image's data replaced by its
    sha256, anything else is kept as text."""
    if text is None:
        return None
    try:
        msg = json.loads(text.strip())
    except ValueError:
        return norm(text)
    if not (isinstance(msg, dict) and msg.get("type") == "user"):
        return norm(text)
    for block in msg["message"]["content"]:
        if block.get("type") == "image":
            data = block["source"]["data"]
            block["source"]["data"] = hashlib.sha256(data.encode("ascii")).hexdigest()
    return norm(msg)


class Procs:
    """subprocess.run / Popen stand-in. `script` maps program -> list of actions, consumed in order
    (the last one repeats). An action: {"raise": "timeout"|"missing"} or {"rc", "stdout",
    "stderr", "create": bool, "frames": n, "if_drawtext": action, "fail_names": [...]}."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.lock = threading.Lock()

    def _next(self, prog):
        acts = self.script.get(prog) or [{}]
        return acts.pop(0) if len(acts) > 1 else acts[0]

    def _record(self, cmd, kw, input_text):
        envd = kw.get("env")
        r = {"cmd": norm([str(c) for c in cmd]), "timeout": kw.get("timeout"),
             "cwd": os.path.basename(kw["cwd"]) if kw.get("cwd") else None,
             "input": hashed_input(input_text)}
        if envd is not None:
            r["env"] = {k: (k in envd) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BASELINE_OTHER")}
        with self.lock:
            self.calls.append(r)

    def _act(self, cmd, timeout):
        prog = program(cmd)
        with self.lock:
            act = self._next(prog)
        if act.get("if_drawtext") is not None and any("drawtext" in str(c) for c in cmd):
            act = act["if_drawtext"]
        last = str(cmd[-1])
        if os.path.basename(last) in (act.get("fail_names") or []):
            act = {"rc": 1, "stderr": "forced failure"}
        if act.get("raise") == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if act.get("raise") == "missing":
            raise MISSING_EXE
        if act.get("create"):
            with open(last, "wb") as fh:
                fh.write(b"output")
        for i in range(act.get("frames", 0)):
            with open(os.path.join(os.path.dirname(last), "frame-%04d.jpg" % (i + 1)), "wb") as fh:
                fh.write(b"frame-%d" % (i + 1))
        return act.get("rc", 0), act.get("stdout", ""), act.get("stderr", "")

    def run(self, cmd, *_a, **kw):
        self._record(cmd, kw, kw.get("input"))
        rc, out, err = self._act(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def popen(self, cmd, **kw):
        procs = self

        class _P:
            def communicate(self, input=None, timeout=None):  # noqa: A002
                procs._record(cmd, dict(kw, timeout=timeout), input)
                rc, out, err = procs._act(cmd, timeout)
                self.returncode = rc
                return out, err

            def kill(self):
                pass

        return _P()

    @contextlib.contextmanager
    def active(self):
        with mock.patch.object(subprocess, "run", self.run), mock.patch.object(subprocess, "Popen", self.popen):
            yield self


class FakeFile:
    def __init__(self, name, uri, mime_type, state):
        self.name, self.uri, self.mime_type = name, uri, mime_type
        self.state = types.SimpleNamespace(name=state) if state else None


def part_of(x):
    if isinstance(x, str):
        return {"text": norm(x)}
    if isinstance(x, Image.Image):
        mime = ("image/jpeg" if x.format == "JPEG" and x.mode in ("1", "L", "RGB", "RGBX", "CMYK")
                else "image/png")
        return {"image": os.path.basename(x.filename), "mime": mime}
    if isinstance(x, FakeFile):
        return {"file": {"uri": x.uri, "mime_type": x.mime_type}}
    return {"other": type(x).__name__}


class FakeGenai:
    """google.genai.Client stand-in driven by {states: [...], response: {...} | {"raise": msg}}."""

    def __init__(self, script):
        self.script = script or {}
        self.log = []
        self.states = list(self.script.get("states") or ["ACTIVE"])

    def client(self, api_key=None):
        fake = self
        fake.log.append({"op": "client", "api_key": api_key})

        class _Files:
            def upload(self, file=None):
                fake.log.append({"op": "upload", "file": os.path.basename(str(file))})
                st = fake.states.pop(0) if len(fake.states) > 1 else fake.states[0]
                mime = "video/mp4" if str(file).endswith(".mp4") else "video/webm"
                return FakeFile("files/abc", "https://example.invalid/files/abc", mime, st)

            def get(self, name=None):
                fake.log.append({"op": "get", "name": name})
                st = fake.states.pop(0) if len(fake.states) > 1 else fake.states[0]
                return FakeFile(name, "https://example.invalid/files/abc", "video/mp4", st)

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                res = getattr(config, "media_resolution", None) if config is not None else None
                fake.log.append({"op": "generate", "model": model, "contents": [part_of(c) for c in contents],
                                 "config": getattr(res, "value", res)})
                r = fake.script.get("response") or {}
                if "raise" in r:
                    raise RuntimeError(r["raise"])
                usage = r.get("usage")
                um = None if usage is None else types.SimpleNamespace(
                    prompt_token_count=usage.get("prompt"), candidates_token_count=usage.get("candidates"),
                    thoughts_token_count=usage.get("thoughts"))
                return types.SimpleNamespace(text=r.get("text"), usage_metadata=um)

        c = types.SimpleNamespace(files=_Files(), models=_Models())
        return c


def result_of(res):
    return norm(plain({k: getattr(res, k) for k in ("success", "data", "reason", "input_tokens",
                                                    "output_tokens", "raw_text")}))


def make_answer(a):
    if a.get("raise"):
        raise RuntimeError(a["raise"])
    ns = types.SimpleNamespace(success=a.get("success", True), data=copy.deepcopy(a.get("data")),
                               reason=a.get("reason"))
    for k in ("input_tokens", "output_tokens"):
        if k in a:
            setattr(ns, k, a[k])
    return ns


class StubVideo:
    def __init__(self, answers, default):
        self.answers, self.default, self.calls = answers or {}, default, []
        self.lock = threading.Lock()

    def analyse_video(self, clip, context_id, prompt, output_schema, ref_images=None, reference=None,
                      high_fps=False, timestamp=False):
        name = os.path.basename(str(clip))
        with self.lock:
            self.calls.append(norm({"clip": name, "context_id": context_id, "prompt": prompt,
                                    "schema": copy.deepcopy(output_schema), "reference": ref_summary(reference),
                                    "high_fps": high_fps, "timestamp": timestamp}))
        return make_answer(self.answers.get(name, self.default))


class StubImage:
    def __init__(self, answers, default):
        self.answers, self.default, self.calls = answers or {}, default, []
        self.lock = threading.Lock()

    def analyse_image(self, image_path, prompt, output_schema, extra_images=None, reference=None):
        name = os.path.basename(str(image_path))
        with self.lock:
            self.calls.append(norm({"shot": name, "prompt": prompt, "schema": copy.deepcopy(output_schema),
                                    "extras": None if extra_images is None else [os.path.basename(str(p)) for p in extra_images]}))
        return make_answer(self.answers.get(name, self.default))


# ── grading ──────────────────────────────────────────────────────────────────────────

NBSP, ZWSP, EM = C(0xa0), C(0x200b), C(0x2014)
ARABIC_123 = C(0x661) + C(0x662) + C(0x663)
THAI_12 = C(0xe51) + C(0xe52)
FULL_12 = C(0xff11) + C(0xff12)

TO_NUM = ["12", "12.50", "$1,234.50", " 1 234 ", "1,2,3", "abc", "", None, True, False, 7, 7.5, 1e16,
          "1e3", ARABIC_123, THAI_12, FULL_12, "1" + NBSP + "234", NBSP + "12" + NBSP, "nan", "inf", "-inf",
          "1_000", "1__0", "$-5", "5%", [1], {"a": 1}, "0x10", "  ", "+3", C(0x2212) + "3", "1.0",
          C(0xb2), "\x1c5", C(0xfeff) + "1", "12" + C(0x85), "infinity", " -0.5 ", 0.1, 1e-7, 123456789012345678]

VALUES_MATCH = [
    [True, True], ["true", True], ["false", True], [0, False], ["", False], [None, False], [[], False],
    ["yes", False], ["12", 12], ["12.0", 12], ["$12", "12"], ["1,200", 1200], [12.0000001, 12],
    [12.00001, 12], ["abc", "ABC "], [" Foo", "foo"], ["Foo", "bar"], [None, None], ["None", None],
    [None, "none"], [1, True], ["1", "1.0"], ["nan", "nan"], ["inf", "inf"], ["x", 1], [[1], [1]],
    [{"a": 1}, {"a": 1}], [C(0x391) + C(0x3a3), C(0x3b1) + C(0x3c2)], ["12" + NBSP, "12"],
    [ARABIC_123, 123], ["\x1cabc\x1f", "abc"], [" abc" + C(0x85), "ABC"], [True, 1], [False, 0],
    [7.5, "7.50"], ["1e3", 1000], ["$ 1 000", "1000"],
]

FIELD_OK = [
    ["confidence", "high", "medium"], ["confidence", "medium", "medium"], ["confidence", "low", "medium"],
    ["confidence", " HIGH ", "high"], ["confidence", "unknown", "low"], ["confidence", "low", "bogus"],
    ["confidence", None, None], ["confidence", "medium", None], ["confidence", "Medium", "HIGH"],
    ["symbol", "cat", ["dog", "Cat"]], ["n", 3, [1, 2]], ["n", 2, [1, "2"]], ["x", "a", []], ["x", 5, 5],
    ["flag", "", [True, False]], ["x", None, [None]],
]

COMPARE = [
    ["12", "12", None], ["12.00", "12", None], ["$1,234.50", "1234.5", None], ["abc", "abc", None],
    ["abc ", " abc", None], ["ABC", "abc", None], ["", "", None], ["x", None, {"value": "10", "tol": 0.5}],
    ["10.4", None, {"value": "10", "tol": 0.5}], ["10.6", None, {"value": "10", "tol": 0.5}],
    ["10", None, {"value": "10"}], ["10", None, {"value": "10", "tol": None}],
    ["10", None, {"value": "10", "tol": "1"}], ["10", None, {"value": "10", "tol": "x"}],
    ["10", None, {"value": "abc", "tol": 1}], ["10", None, None], ["10", None, {"value": 10, "tol": 2.0}],
    ["10", None, "notadict"], ["it's", "it's", None], ["a" + NBSP, "b", None], ["9.95", None, {"value": "10", "tol": 0.05}],
    ["1", "1.0000001", None], ["x", "", None],
]

GRADE = [
    [{"symbol": "cat", "count": "3", "confidence": "high"}, {"symbol": "Cat", "count": 3}, "saw a cat", [10, 2]],
    [{"symbol": "dog", "count": "4", "confidence": "low"}, {"symbol": "cat", "count": 3, "confidence": "medium"},
     " because ", [1, 1]],
    [{}, {"a": 1, "b": True, "c": None}, "", [0, 0]],
    [{"a": 1}, None, None, [0, 0]],
    [{"a": 1}, {}, "all fine", [5, 5]],
    [{"kick": "dog"}, {"kick": ["cat", "dog"]}, "kicked", [0, 0]],
    [{"kick": "cow"}, {"kick": ["cat", "dog"]}, "", [0, 0]],
    [{"v": "1" + NBSP + "234", "e": C(0x1f600), "q": "it's \"x\"", "n": {"x": [1, 2.5]}, "f": 0.1 + 0.2,
      "t": 1e-7, "z": ZWSP}, {"v": "1234", "e": "x", "q": "y", "n": {"x": [1]}, "f": 0.3, "t": 0, "z": ""},
     "odd values", [0, 0]],
    [{"a": 1}, ["a"], "", [0, 0]],
    [["not", "a", "dict"], {"a": 1}, "", [0, 0]],
    [{"a": True}, {"a": "True"}, "-", [0, 0]],
    [{"confidence": "medium"}, {"confidence": "high"}, " - - ", [0, 0]],
    [{"n": 1.0}, {"n": "1"}, None, [0, 0]],
]

VERDICTS = [
    {"verdict": "red", "video": [{"status": "orange"}]}, {"verdict": "inconclusive"},
    {"verdict": "pending", "video": [{"status": "pass"}], "image": [{"status": "pending"}]},
    {"verdict": "pending", "video": [{"status": "pass"}, {"status": "orange"}]}, {"verdict": "pending"},
    {"verdict": "green", "image": [{"status": "pass"}]}, {}, {"verdict": "pending", "video": [{}]},
    {"verdict": "orange", "video": [{"status": "pass"}]},
]

WORKERS = [
    [{}, 4], [{"AI_CONCURRENCY": "8"}, 4], [{"AI_CONCURRENCY": " 2 "}, 4], [{"AI_CONCURRENCY": "0"}, 4],
    [{"AI_CONCURRENCY": "-1"}, 4], [{"AI_CONCURRENCY": "abc"}, 4], [{"AI_CONCURRENCY": "2.5"}, 4],
    [{"AI_PROVIDER": "local"}, 4], [{"AI_PROVIDER": " LOCAL "}, 4],
    [{"AI_PROVIDER": "local", "AI_CONCURRENCY": "3"}, 4], [{"AI_CONCURRENCY": C(0x663)}, 4], [{}, 6],
    [{"AI_CONCURRENCY": ""}, 2],
]


READ_NUMBER = ["$1,250.00", "50", "x10", "-3.5 credits", "", None, "no digits", 12.5, "1.", "0.20 USD"]


def _rows(*vals):
    return [{"data": {"p": v}} for v in vals]


COMPARE_SHOTS = [
    ({"field": "p", "ratio": "5.0"}, _rows("$10.00", "$50.00")),
    ({"field": "p", "ratio": "5.0", "tol": 0.02}, _rows("$10.00", "$50.90")),
    ({"field": "p", "ratio": "5", "tol": 0.02}, _rows("10", "10")),
    ({"field": "p", "ratio": "2.5"}, _rows("$8.00", "$20.00")),
    ({"field": "p", "ratio": "2"}, _rows("0", "4")),
    ({"field": "p", "ratio": "2"}, _rows(None, "4")),
    ({"field": "p", "ratio": "2"}, _rows("4")),
    ({"field": "p", "ratio": "{facts.bet_ratio}"}, _rows("1", "2")),
    ({"field": "p", "ratio": "3.333333", "tol": 0.01}, [{"data": {"p": "0.60"}}, {}]),
    ({"field": "p", "ratio": "3.333333", "tol": 0.01}, _rows("0.60", "2.00")),
]


def grading():
    return {
        "read_number": [{"in": x, "out": rec(ai_video.read_number, x)} for x in READ_NUMBER],
        "compare_shots": [{"in": [c, r], "out": rec(ai_video.compare_shots, c, r)} for c, r in COMPARE_SHOTS],
        "to_num": [{"in": x, "out": rec(ai_video._to_num, x)} for x in TO_NUM],
        "values_match": [{"in": p, "out": rec(ai_video._values_match, *p)} for p in VALUES_MATCH],
        "field_ok": [{"in": p, "out": rec(ai_video._field_ok, *p)} for p in FIELD_OK],
        "compare": [{"in": p, "out": rec(ai_video._compare, *p)} for p in COMPARE],
        "grade_schema": [{"in": copy.deepcopy(p), "out": rec(ai_video.grade_schema, *copy.deepcopy(p))} for p in GRADE],
        "recompute_verdict": [{"in": v, "out": rec(ai_video.recompute_verdict, copy.deepcopy(v))} for v in VERDICTS],
        "ai_workers": [{"env": e, "default": d, "out": _with_env(e, ai_video.ai_workers, d)} for e, d in WORKERS],
    }


def _with_env(e, fn, *a):
    with env(e):
        return rec(fn, *a)


# ── single asserts (run_video_assert / run_image_assert) ─────────────────────────────

OK = {"success": True, "input_tokens": 100, "output_tokens": 20}

VIDEO_ASSERTS = [
    {"spec": {"kind": "check", "check": "The reels spin"}, "answer": dict(OK, data={"passed": True, "confidence": "high", "reason": "reels spun"})},
    {"spec": {"kind": "check", "check": "A popup shows"}, "answer": dict(OK, data={"passed": False, "confidence": "medium", "reason": "no popup " + EM + " only reels"})},
    {"spec": {"kind": "check", "check": "x"}, "answer": dict(OK, data={"passed": True, "confidence": "low", "reason": "unsure"})},
    {"spec": {"kind": "check", "check": "x"}, "answer": dict(OK, data={"passed": True, "confidence": "high"})},
    {"spec": {"kind": "check", "check": "x"}, "answer": dict(OK, data={"passed": "false", "confidence": "High"})},
    {"spec": {"kind": "check", "check": "Held 2s", "timestamp": True, "high_fps": True,
              "timeline_hint": "\n\nACTION TIMELINE (clip-relative seconds, matching the burned-in timestamps):\n- hold: pressed at t=0.50s, released at t=2.50s"},
     "answer": dict(OK, data={"passed": True, "confidence": "medium", "reason": ""})},
    {"spec": {"kind": "read", "read": "the WIN label", "equals": "1234.5"}, "answer": dict(OK, data={"value": " 1,234.50 ", "confidence": "high", "reason": "read it"})},
    {"spec": {"kind": "read", "read": "the WIN label", "equals": "13"}, "answer": dict(OK, data={"value": "12", "confidence": "high", "reason": "blurry"})},
    {"spec": {"kind": "read", "read": "balance", "near": {"value": "100", "tol": 0.5}}, "answer": dict(OK, data={"value": "$100.40", "confidence": "medium"})},
    {"spec": {"kind": "read", "read": "balance", "near": {"value": "100", "tol": 2}}, "answer": dict(OK, data={"value": "n/a", "confidence": "high", "reason": "hidden"})},
    {"spec": {"kind": "read", "read": "balance"}, "answer": dict(OK, data={"value": "7", "confidence": "high"})},
    {"spec": {"kind": "read", "read": "balance", "equals": "None"}, "answer": dict(OK, data={"value": None, "confidence": "high"})},
    {"spec": {"kind": "read", "read": "balance", "equals": ""}, "answer": dict(OK, data={"confidence": "low"})},
    {"spec": {"kind": "read", "read": "x", "equals": "abc"}, "answer": dict(OK, data={"value": "ABC", "confidence": "high"})},
    {"spec": {"kind": "schema", "prompt": "Which symbol?", "output_schema": {"symbol": {"type": "string", "enum": ["cat", "dog"]}, "count": {"type": "integer"}},
              "expected": {"symbol": "cat", "count": 3, "confidence": "medium"}},
     "answer": dict(OK, data={"symbol": "Cat", "count": "3", "confidence": "high", "reason": "three cats"})},
    {"spec": {"kind": "schema", "prompt": "No schema", "expected": {"ok": True}}, "answer": dict(OK, data={"ok": "yes", "reason": "fine"})},
    {"spec": {"kind": "schema", "prompt": "Own reason", "output_schema": {"reason": {"type": "string", "description": "why"}, "a": {"type": "boolean"}}, "expected": {"a": False}},
     "answer": dict(OK, data={"a": 0, "reason": "zero"})},
    {"spec": {"kind": "schema", "prompt": "Pairs", "output_schema": [["a", {"type": "string"}]], "expected": {}}, "answer": dict(OK, data={"a": "x"})},
    {"spec": {"kind": "schema", "prompt": "Bad schema", "output_schema": "abc", "expected": {}}, "answer": dict(OK, data={"a": "x"})},
    {"spec": {"kind": "check", "check": "fails"}, "answer": {"success": False, "data": None, "reason": "boom", "input_tokens": 3, "output_tokens": 0}},
    {"spec": {"kind": "check", "check": "empty"}, "answer": dict(OK, data={})},
    {"spec": {"kind": "check", "check": "none"}, "answer": {"success": True, "data": None, "reason": None}},
    {"spec": {"kind": "check", "check": "list"}, "answer": dict(OK, data=[1, 2])},
    {"spec": {"kind": "check", "check": "string"}, "answer": dict(OK, data="passed")},
    {"spec": {"kind": "check", "check": "no tokens"}, "answer": {"success": True, "data": {"passed": True, "confidence": "high"}, "reason": None}},
    {"spec": {"check": "no kind"}, "answer": dict(OK, data={"passed": True})},
    {"spec": {"kind": "check"}, "answer": dict(OK, data={"passed": True})},
    {"spec": {"kind": "check", "check": "with ref"}, "reference": {"game": "zeus", "groups": ["symbols"], "assets": None},
     "answer": dict(OK, data={"passed": True, "confidence": "high"})},
    {"spec": {"kind": "check", "check": "hint not str", "timeline_hint": 5}, "answer": dict(OK, data={"passed": True})},
    {"spec": {"kind": "check", "check": None}, "answer": dict(OK, data={"passed": True})},
    {"spec": {"kind": "schema", "prompt": "conf", "expected": {"confidence": "high"}}, "answer": dict(OK, data={"confidence": "HIGH ", "reason": " r "})},
]

IMAGE_ASSERTS = [
    {"spec": {"prompt": "Is this {shot.label}? ({shot.label})", "output_schema": {"lang": {"type": "string"}}, "expected": {"lang": "th"}},
     "shot": "shot.png", "label": "th Thai", "extras": None, "answer": dict(OK, data={"lang": "TH", "reason": "thai script"})},
    {"spec": {"prompt": "Is there an RTP section?", "output_schema": {"rtp": {"type": "boolean"}}, "expected": {"rtp": True}},
     "shot": "shot.png", "label": "help p1, help p2, help p3", "extras": ["extra1.png", "extra2.png"],
     "answer": dict(OK, data={"rtp": True, "reason": "page 3"})},
    {"spec": {"prompt": "One page", "expected": {"x": 1}}, "shot": "shot.png", "label": "solo", "extras": [],
     "answer": dict(OK, data={"x": "2"})},
    {"spec": {"prompt": "fails"}, "shot": "shot.png", "label": "", "extras": None, "answer": {"success": False, "data": None, "reason": "quota"}},
    {"spec": {"prompt": "has reason", "output_schema": {"reason": {"type": "string"}, "ok": {"type": "boolean"}}, "expected": {}},
     "shot": "shot.png", "label": "x", "extras": None, "answer": {"success": True, "data": {"ok": True}, "reason": None}},
    {"spec": {"output_schema": {}}, "shot": "shot.png", "label": "x", "extras": None, "answer": dict(OK, data={"ok": True})},
    {"spec": {"prompt": "list data", "expected": {"a": 1}}, "shot": "shot.png", "label": "x", "extras": None, "answer": dict(OK, data=[1])},
    {"spec": {"prompt": "dollar $& label {shot.label}"}, "shot": "shot.png", "label": "$1 $&", "extras": None, "answer": dict(OK, data={"a": 1})},
]


def single_asserts():
    out_v = []
    for case in VIDEO_ASSERTS:
        stub = StubVideo({}, case["answer"])
        spec = copy.deepcopy(case["spec"])
        if case.get("reference"):
            spec["reference"] = ref_from(case["reference"])
        r = rec(ai_video.run_video_assert, stub, Path(tmp("va", "clip.webm")), spec)
        out_v.append({"in": case, "call": stub.calls[0] if stub.calls else None, "out": r})
    out_i = []
    for case in IMAGE_ASSERTS:
        stub = StubImage({}, case["answer"])
        extras = None if case["extras"] is None else [Path(tmp("img", e)) for e in case["extras"]]
        r = rec(ai_video.run_image_assert, stub, Path(tmp("img", case["shot"])), copy.deepcopy(case["spec"]),
                case["label"], extras)
        out_i.append({"in": case, "call": stub.calls[0] if stub.calls else None, "out": r})
    return {"video_assert": out_v, "image_assert": out_i}


# ── timeline hints ───────────────────────────────────────────────────────────────────

TIMELINE = [
    {"action": "hold", "description": "hold spin", "start_ms": 1500, "end_ms": 3250},
    {"action": "click", "description": "click bet+", "start_ms": 4000},
    {"action": "net", "start_ms": 500, "end_ms": 900},
    {"action": "wait", "description": "ignored", "start_ms": 2000},
    {"action": "hold_key", "description": "space", "start_ms": 900, "end_ms": 1100},
    {"action": "dblclick", "description": "late", "start_ms": 10000},
    {"action": "mark", "description": "tie", "start_ms": 1125},
    {"action": "key", "description": "no time"},
    {"action": "scroll", "description": "bool time", "start_ms": True},
    {"action": "second_session", "description": 7, "start_ms": 2062.5},
    {"description": "no action", "start_ms": 1200},
]

TIMELINE_CASES = [
    {"timeline": TIMELINE, "window": None}, {"timeline": TIMELINE, "window": [1.0, 5.0]},
    {"timeline": TIMELINE, "window": [0, 0.5]}, {"timeline": TIMELINE, "window": [2.0, 3.0]},
    {"timeline": TIMELINE, "window": ["1.5", "4"]}, {"timeline": TIMELINE, "window": [100, 200]},
    {"timeline": None, "window": [1.0, 5.0]}, {"raw": "not json {", "window": [1.0, 5.0]},
    {"timeline": [{"action": "click", "start_ms": None}], "window": [0, 9]},
    {"timeline": {"a": 1}, "window": [0, 9]}, {"timeline": [], "window": []},
    {"timeline": 5, "window": [0, 9]}, {"timeline": [{"action": "click", "start_ms": "100"}], "window": [0, 9]},
    {"timeline": [{"action": "hold", "start_ms": 0, "end_ms": 0}], "window": [0, 1]},
]


def timeline_hints():
    out = []
    for n, case in enumerate(TIMELINE_CASES):
        run_dir = tmp("tl", str(n), "x")
        run_dir = os.path.dirname(run_dir)
        if case.get("raw") is not None:
            with open(os.path.join(run_dir, "timeline.json"), "w", encoding="utf-8") as fh:
                fh.write(case["raw"])
        elif case.get("timeline") is not None:
            with open(os.path.join(run_dir, "timeline.json"), "w", encoding="utf-8") as fh:
                json.dump(case["timeline"], fh)
        out.append({"in": case, "dir": f"tl/{n}", "out": rec(ai_video._timeline_hint, Path(run_dir), case["window"])})
    return out


# ── ffmpeg: trim, montage, frame sampling ────────────────────────────────────────────

TRIMS = [
    {"session": "missing.webm", "window": None, "procs": {}},
    {"session": "session.webm", "window": None, "procs": {"ffmpeg": [{"create": True}]}},
    {"session": "session.webm", "window": [1.0625, 3.5], "procs": {"ffmpeg": [{}, {"create": True}]}},
    {"session": "session.webm", "window": [-2, 1e21], "procs": {"ffmpeg": [{"create": True}]}},
    {"session": "session.webm", "window": ["0.5", "2"], "procs": {"ffmpeg": [{"rc": 1}, {"rc": 1}]}},
    {"session": "session.webm", "window": [0.0005, 0.0015], "procs": {"ffmpeg": [{"raise": "timeout"}, {"raise": "missing"}]}},
    {"session": "session.webm", "window": [1, 2, 3], "procs": {}},
    {"session": "session.webm", "window": [], "procs": {"ffmpeg": [{"create": True}]}},
    {"session": "session.webm", "window": ["x", 2], "procs": {}},
]


def trims():
    out = []
    for n, case in enumerate(TRIMS):
        procs = Procs(case["procs"])
        dest = Path(tmp("trim", str(n), "clip.webm"))
        with procs.active(), captured() as buf:
            r = rec(ai_video.trim_clip, Path(tmp(case["session"])), case["window"], dest)
        out.append({"in": case, "dir": f"trim/{n}", "out": r, "calls": procs.calls, "stdout": lines(buf)})
    return out


MONTAGES = [
    {"source": "img/shot.png", "fps": 4, "timestamp": False, "procs": {"ffmpeg": [{"create": True}]}},
    {"source": "img/shot.png", "fps": 16, "timestamp": True, "procs": {"ffmpeg": [{"rc": 1, "stderr": "x" * 600}]}},
    {"source": "img/shot.png", "fps": 4, "timestamp": False, "procs": {"ffmpeg": [{"raise": "missing"}]}},
    {"source": "img/shot.png", "fps": 4, "timestamp": False, "procs": {"ffmpeg": [{"raise": "timeout"}]}},
    {"source": "img/shot.png", "fps": 4, "timestamp": False, "procs": {"ffmpeg": [{}]}},
    {"source": "nope.webm", "fps": 4, "timestamp": False, "procs": {}},
]


def montage():
    out = {
        "constants": {"cols": gemini_video._MONTAGE_COLS, "interval": gemini_video._MONTAGE_INTERVAL_S,
                      "sample_fps": gemini_video._MONTAGE_SAMPLE_FPS, "padding": gemini_video._MONTAGE_PADDING_PX,
                      "timeout": gemini_video._FFMPEG_TIMEOUT, "high_fps": gemini_video._MONTAGE_HIGH_FPS},
        "font_file": gemini_video._font_file(),
        "drawtext": gemini_video._drawtext_ts(),
        "filters": [{"in": [f, t], "out": gemini_video._montage_filter(f, t)} for f, t in
                    [(4, False), (4, True), (16, False), (16, True), (8, False), (3, True)]],
        "preambles": [{"in": [f, t], "out": gemini_video._montage_preamble(f, t)} for f, t in
                      [(4, False), (4, True), (16, False), (16, True), (3, False), (5, False), (1, True)]],
        "builds": [],
    }
    for n, case in enumerate(MONTAGES):
        procs = Procs(case["procs"])
        dest = Path(tmp("montage", str(n), "out-montage.mp4"))
        with procs.active():
            r = rec(gemini_video._build_montage, Path(tmp(*case["source"].split("/"))), dest, case["fps"], case["timestamp"])
        out["builds"].append({"in": case, "dir": f"montage/{n}", "out": r, "calls": procs.calls})
    return out


# ── Gemini helpers (fake client) ─────────────────────────────────────────────────────

CHECK = {"passed": {"type": "boolean"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
         "reason": {"type": "string"}}
ODD_SCHEMA = {"n": {"type": "integer"}, "x": {"type": "array"}, "s": "notadict", "e": {"type": "string", "enum": []},
              "u": {"type": "string", "enum": "abc"}, "t": {}, "f": {"type": "number"}, "b": {"type": "boolean"},
              "nul": {"type": None}, "v": {"type": "string", "description": "gi" + C(0xe1) + " tr" + C(0x1ecb) + " " + EM + " " + C(0x1f600)}}

RESP_OK = {"text": "```json\n{\"passed\": true, \"confidence\": \"high\", \"reason\": \"ok\"}\n```",
           "usage": {"prompt": 1200, "candidates": 30, "thoughts": 70}}

GEMINI_VIDEO = [
    {"clip": "c.webm", "prompt": "Do the reels spin?", "schema": CHECK, "opts": {},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": RESP_OK}},
    {"clip": "c.webm", "prompt": "Timed", "schema": CHECK, "opts": {"timestamp": True},
     "procs": {"ffmpeg": [{"if_drawtext": {"rc": 1}, "create": True}]}, "genai": {"response": RESP_OK}},
    {"clip": "c.webm", "prompt": "Fast", "schema": CHECK, "opts": {"timestamp": True, "high_fps": True},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": RESP_OK}},
    {"clip": "c.webm", "prompt": "Raw clip", "schema": ODD_SCHEMA, "opts": {"timestamp": True},
     "procs": {"ffmpeg": [{"rc": 1}]}, "genai": {"response": {"text": "{\"n\": 1}", "usage": {"prompt": 5}}}},
    {"clip": "c.webm", "prompt": "Processing", "schema": CHECK, "opts": {},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"states": ["PROCESSING", None, "ACTIVE"], "response": RESP_OK}},
    {"clip": "c.webm", "prompt": "Not JSON", "schema": CHECK, "opts": {},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": {"text": "I think the reels " + EM + " spin " * 60, "usage": {"prompt": 10, "candidates": 3}}}},
    {"clip": "c.webm", "prompt": "Bad resolution", "schema": CHECK, "opts": {}, "resolution": "ultra",
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": RESP_OK}},
    {"clip": "c.webm", "prompt": "With refs", "schema": CHECK,
     "opts": {"reference": {"game": "synth", "base": "<TMP>", "groups": ["symbols"], "assets": ["ui.screen"]},
              "ref_images": ["<TMP>/img/photo.jpg", "<TMP>/img/nothere.png"]},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": RESP_OK}},
    {"clip": "c.webm", "prompt": "Raises", "schema": CHECK, "opts": {}, "model": "gemini-2.5-flash", "resolution": "low",
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": {"raise": "quota exceeded"}}},
    {"clip": "c.webm", "prompt": "No usage", "schema": CHECK, "opts": {},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": {"text": "{\"passed\": false}", "usage": None}}},
    {"clip": "c.webm", "prompt": "No text", "schema": {}, "opts": {},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": {"text": None, "usage": {"prompt": None, "candidates": 4, "thoughts": None}}}},
    {"clip": "c.webm", "prompt": "Constructor ref", "schema": CHECK, "opts": {}, "ctor_reference": {"game": "synth", "base": "<TMP>", "groups": ["ui"], "assets": None},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": {"text": "[1, 2]", "usage": {"prompt": 1, "candidates": 1}}}},
    {"clip": "c.webm", "prompt": "Empty ref", "schema": CHECK, "opts": {"reference": {"game": "synth", "base": "<TMP>", "groups": [], "assets": []}},
     "procs": {"ffmpeg": [{"create": True}]}, "genai": {"response": RESP_OK}},
]

GEMINI_IMAGE = [
    {"image": "img/shot.png", "prompt": "What symbol?", "schema": ODD_SCHEMA, "extras": None,
     "genai": {"response": {"text": "  {\"n\": 3, \"v\": \"x\"}  ", "usage": {"prompt": 500, "candidates": 20, "thoughts": 5}}}},
    {"image": "img/shot.png", "prompt": "Pages", "schema": CHECK, "extras": ["img/extra1.png", "img/photo.jpg"],
     "genai": {"response": RESP_OK}},
    {"image": "img/shot.png", "prompt": "Refs", "schema": CHECK, "extras": None,
     "reference": {"game": "synth", "base": "<TMP>", "groups": ["symbols"], "assets": None}, "genai": {"response": RESP_OK}},
    {"image": "img/bad.png", "prompt": "Bad", "schema": CHECK, "extras": None, "genai": {"response": RESP_OK}},
    {"image": "img/missing.png", "prompt": "Missing", "schema": CHECK, "extras": None, "genai": {"response": RESP_OK}},
    {"image": "img/shot.png", "prompt": "Extra missing", "schema": CHECK, "extras": ["img/missing.png"], "genai": {"response": RESP_OK}},
    {"image": "img/shot.png", "prompt": "Bad JSON", "schema": CHECK, "extras": None,
     "genai": {"response": {"text": "{\"passed\": tru}", "usage": {"prompt": 7, "candidates": 1}}}},
    {"image": "img/shot.png", "prompt": "Raises", "schema": CHECK, "extras": None, "model": "gemini-3-flash-preview",
     "genai": {"response": {"raise": "500 INTERNAL"}}},
]


def gemini_flows():
    video = []
    for n, case in enumerate(GEMINI_VIDEO):
        fake = FakeGenai(case["genai"])
        procs = Procs(case["procs"])
        sleeps = []
        clip = Path(tmp("gv", str(n), case["clip"]))
        clip.write_bytes(b"clip")
        helper = gemini_video.GeminiVideoHelper("key-" + str(n), model=case.get("model"), resolution=case.get("resolution"),
                                                reference=ref_from(case.get("ctor_reference")))
        o = case["opts"]
        with procs.active(), mock.patch.object(genai, "Client", fake.client), \
                mock.patch.object(gemini_video, "time", types.SimpleNamespace(sleep=sleeps.append)):
            res = helper.analyse_video(clip, None, case["prompt"], copy.deepcopy(case["schema"]),
                                       ref_images=[Path(real(p)) for p in o["ref_images"]] if o.get("ref_images") else None,
                                       reference=ref_from(o.get("reference")), high_fps=o.get("high_fps", False),
                                       timestamp=o.get("timestamp", False))
        video.append({"in": case, "dir": f"gv/{n}", "result": result_of(res), "log": norm(fake.log), "calls": procs.calls,
                      "sleeps": sleeps, "cost": plain(helper.cost_summary()), "estimated": helper.estimated_cost_usd,
                      "model": helper.model, "per_call": helper.cost_for_tokens(1000, 100),
                      "tokens": [helper.total_input_tokens, helper.total_output_tokens]})
    image = []
    for n, case in enumerate(GEMINI_IMAGE):
        fake = FakeGenai(case["genai"])
        helper = gemini.GeminiImageHelper("img-key", model=case.get("model"))
        with mock.patch.object(genai, "Client", fake.client):
            res = helper.analyse_image(Path(real("<TMP>/" + case["image"])), case["prompt"], copy.deepcopy(case["schema"]),
                                       extra_images=[Path(real("<TMP>/" + e)) for e in case["extras"]] if case["extras"] else None,
                                       reference=ref_from(case.get("reference")))
        image.append({"in": case, "result": result_of(res), "log": norm(fake.log), "cost": plain(helper.cost_summary()),
                      "estimated": helper.estimated_cost_usd, "calls": helper.call_count,
                      "tokens": [helper.total_input_tokens, helper.total_output_tokens]})
        helper.reset()
        image[-1]["after_reset"] = plain(helper.cost_summary())
    # cumulative cost over several calls on ONE helper
    fake = FakeGenai({"response": RESP_OK})
    procs = Procs({"ffmpeg": [{"create": True}]})
    helper = gemini_video.GeminiVideoHelper("k")
    clip = Path(tmp("gv", "cum", "c.webm"))
    clip.write_bytes(b"clip")
    with procs.active(), mock.patch.object(genai, "Client", fake.client):
        for _ in range(3):
            helper.analyse_video(clip, None, "again", dict(CHECK))
    cumulative = {"cost": plain(helper.cost_summary()), "estimated": helper.estimated_cost_usd}
    pricing = {
        "supported": gemini_pricing.SUPPORTED_MODELS,
        "models": [{"in": m, "out": {"model": p.model, "input_cost_per_1m": p.input_cost_per_1m,
                                      "output_cost_per_1m": p.output_cost_per_1m, "input_per_token": p.input_per_token,
                                      "output_per_token": p.output_per_token}}
                   for m in ("gemini-2.5-flash", "gemini-3-flash-preview", "unknown", "")
                   for p in [gemini_pricing.get_pricing(m)]],
        "per_call": [{"in": [m, i, o], "out": gemini_video.GeminiVideoHelper("k", model=m).cost_for_tokens(i, o)}
                     for m, i, o in [("gemini-3-flash-preview", 1000, 100), ("gemini-2.5-flash", 123456, 7890),
                                     ("x", 1, 1), ("gemini-3-flash-preview", 0, 0), ("gemini-2.5-flash", 3, 333333)]],
    }
    return {"gemini_video": video, "gemini_image": image, "gemini_cumulative": cumulative, "pricing": pricing}


# ── text parsing: fences, json.loads, extract_json, schema prompts ───────────────────

FENCES = ["```json\n{\"a\":1}\n```", "```\n{\"a\":1}```", "  {\"a\":1}  ", "```json{\"a\":1}```", "```JSON\n{}\n```",
          "```json\n{\"a\":1}\n``` trailing", "text ```json {} ```", "``````", "```", "```json\n```",
          "\n\n```json\n\n  {\"x\": [1,\n2]}\n\n```\n", "```json" + NBSP + "{}```", "```json\x1c{}```"]

JSON_LOADS = ['', ' ', 'x', '{', '{"a"', '{"a":', '{"a":1', '{"a":1,', '{"a":1,}', '{"a" 1}', '{a:1}', '[1,]', '[1 2]', '[',
              '"abc', '"a\\x"', '"a\\u12"', '"a\\u12g4"', '"\\ud83d\\ude00"', '"a\x01b"', '{"a":1}x', '{"a":1} ', 'NaN',
              '-Infinity', 'Infinity', '-', '01', '1.', '1e', '-0', '1.5e3', '[1,2', '{"a":1 "b":2}', '{"a":[1,2}', 'tru',
              'nul', '"\\u123"', '"\\u1234', '{"a":1,"a":2}', C(0xfeff) + '{}', '{"a":}', '{,}', '[,]', '{"a":1,,}', '"\\',
              '"\\u', '"ab\\', '"\\ud83d\\u12"', '"\\ud83d\\uZZZZ"', '{"a"\n:\n1,\n}', '[1,\n]', '"\x1f"', '\n\n  x',
              '"a\tb"', '{"a":1, }', '[1, ]', '[1 , ]', '{"a": [1, 2.5, -0, 1e400, true, null, "x\\u00e9"]}',
              ' {"a":"\\ud83d\\ude00", "b": "\\n\\t\\/\\\\\\"\\b\\f\\r"} ', '{"__proto__": 1, "x": 2}', '{"1": "a", "b": 2}',
              '[1e5, 1E-2, -1.5e+3, 0.0, 12345678901234567890]', '{"k": "' + C(0xe9) + C(0x1f600) + '"} ' + C(0x1f600),
              C(0x1f600) + ' 1', '"' + C(0x1f600) + '\x02"', 'true false', '[true,false,null]', '{"a": {"b": {"c": []}}}',
              '-1', '-01', '1.5e', '1.e5', '2E+', '[-Infinity, NaN]', '{"a": NaN}', '"\\/"', '{"a" : 1 , "b" : [ ] }',
              '\r\n\t {} \r\n']

EXTRACT = ['', '   ', '{"a":1}', 'reasoning... {"a": 1} more {"b": 2}', 'x {"a": {"b": 1}} y', '{bad} {"ok": true}',
           '<think>{"x":1}</think>\n```json\n{"passed": true}\n```', '[1,2]', '{"a":1', 'no json here', '{"a":1}{"b":2}',
           '{"a": "}"} {"c": 3}', C(0xe9) + ' {"k": "' + C(0xe9) + '"}', C(0x1f600) + C(0x1f600) + ' {"e": 1} {',
           '"just a string"', '{"a": NaN}', '  {"nested": {"deep": {"x": 1}}} trailing {"last": [1, {"y": 2}]}',
           '{{"a": 1}}', '{"a": 1}\n\nSo the answer: {"passed": false, "reason": "x"}']

SCHEMA_PROMPTS = [
    ["Is it spinning?", CHECK, False, None], ["Is it spinning?", CHECK, True, None], ["p", {}, True, "0"],
    ["p", ODD_SCHEMA, True, " False "], ["p", ODD_SCHEMA, True, "no"], ["p", CHECK, True, "OFF"], ["p", CHECK, True, "1"],
    ["p", CHECK, True, "yes"], ["p", CHECK, False, "1"], ["multi\nline " + EM, ODD_SCHEMA, False, None],
    ["p", {"e": {"type": "string", "enum": {"0": "zero"}}}, True, None],
]


def text_parsing():
    def loads(s):
        return rec(json.loads, s)

    sp = []
    for prompt, schema, allow, reasoning in SCHEMA_PROMPTS:
        with env({} if reasoning is None else {"AGENT_REASONING": reasoning}):
            sp.append({"in": [prompt, schema, allow, reasoning], "out": rec(local_ai._build_schema_prompt, prompt, copy.deepcopy(schema), allow)})
    return {
        "fence": [{"in": s, "out": gemini._strip_markdown_fence(s), "video": gemini_video._strip_markdown_fence(s)} for s in FENCES],
        "json_loads": [{"in": s, "out": loads(s)} for s in JSON_LOADS],
        "extract_json": [{"in": s, "out": rec(local_ai._extract_json, s)} for s in EXTRACT],
        "schema_prompt": sp,
    }


# ── local endpoint (fake requests.post) ──────────────────────────────────────────────

LOCAL = [
    {"kind": "video", "file": "img/shot.png", "prompt": "Spin?", "schema": CHECK,
     "http": {"status": 200, "reason": "OK", "body": json.dumps({"media_url": "u", "result": "{\"passed\": true, \"confidence\": \"high\"}"})}},
    {"kind": "image", "file": "img/shot.png", "prompt": "Which?", "schema": ODD_SCHEMA, "extras": ["img/extra1.png"],
     "http": {"status": 200, "reason": "OK", "body": json.dumps({"result": "<think>hmm {\"n\": 0}</think> final: {\"n\": 2, \"v\": \"" + EM + "\"}"})}},
    {"kind": "image", "file": "img/shot.png", "prompt": "No JSON", "schema": CHECK,
     "http": {"status": 200, "reason": "OK", "body": json.dumps({"result": ("I could not decide " + C(0xe9) + " ") * 20})}},
    {"kind": "video", "file": "img/shot.png", "prompt": "500", "schema": CHECK, "http": {"status": 500, "reason": "Internal Server Error", "body": "oops"}},
    {"kind": "image", "file": "img/shot.png", "prompt": "404", "schema": CHECK, "http": {"status": 404, "reason": "Not Found", "body": ""}},
    {"kind": "video", "file": "img/shot.png", "prompt": "list", "schema": CHECK, "http": {"status": 200, "reason": "OK", "body": "[1, 2]"}},
    {"kind": "image", "file": "img/shot.png", "prompt": "null", "schema": CHECK, "http": {"status": 200, "reason": "OK", "body": "{\"result\": null}"}},
    {"kind": "image", "file": "img/shot.png", "prompt": "invalid body", "schema": CHECK, "http": {"status": 200, "reason": "OK", "body": "<html>"}},
    {"kind": "video", "file": "img/shot.png", "prompt": "refs", "schema": CHECK, "ref_images": ["img/extra1.png"],
     "reference": {"game": "synth", "base": "<TMP>", "groups": ["symbols", "ui"], "assets": None},
     "http": {"status": 200, "reason": "OK", "body": json.dumps({"result": "{\"passed\": false}"})}},
    {"kind": "image", "file": "img/missing.png", "prompt": "missing", "schema": CHECK, "http": {"status": 200, "reason": "OK", "body": "{}"}},
    {"kind": "image", "file": "img/shot.png", "prompt": "ctor ref", "schema": CHECK, "ctor_reference": {"game": "legacy", "base": "<TMP>", "full": True},
     "http": {"status": 302, "reason": "Found", "body": json.dumps({"result": "{\"passed\": true}"})}},
]


def local_flows():
    out = []
    for case in LOCAL:
        posts = []
        h = case["http"]

        def post(url, data=None, files=None, timeout=None, _h=h, _posts=posts):
            name, fh, mime = files["file"]
            _posts.append({"url": url, "prompt": norm(data["prompt"]), "file": [name, mime, len(fh.read())], "timeout": timeout})
            r = requests.models.Response()
            r.status_code, r.reason, r.url, r._content = _h["status"], _h["reason"], url, _h["body"].encode("utf-8")
            r.encoding = "utf-8"
            return r

        endpoint = "http://127.0.0.1:9/analyze"
        cls = local_ai.LocalVideoHelper if case["kind"] == "video" else local_ai.LocalImageHelper
        helper = cls(endpoint, reference=ref_from(case.get("ctor_reference")))
        path = Path(real("<TMP>/" + case["file"]))
        with mock.patch.object(requests, "post", post):
            if case["kind"] == "video":
                res = helper.analyse_video(path, None, case["prompt"], copy.deepcopy(case["schema"]),
                                           ref_images=[Path(real("<TMP>/" + p)) for p in case.get("ref_images", [])] or None,
                                           reference=ref_from(case.get("reference")))
            else:
                res = helper.analyse_image(path, case["prompt"], copy.deepcopy(case["schema"]),
                                           extra_images=[Path(real("<TMP>/" + p)) for p in case.get("extras", [])] or None,
                                           reference=ref_from(case.get("reference")))
        out.append({"in": case, "endpoint": endpoint, "result": result_of(res), "posts": posts,
                    "cost": plain(helper.cost_summary()), "calls": helper.call_count})
    return out


# ── Claude CLI (fake subprocess) ─────────────────────────────────────────────────────

ENVELOPE_OK = json.dumps({"type": "result", "is_error": False,
                          "result": "Looking at it... the reels spin.\n\n{\"passed\": true, \"confidence\": \"high\", \"reason\": \"spun\"}"})


def stream(*events, junk=True):
    out = ["not json", ""] if junk else []
    out += [json.dumps(e) for e in events]
    return "\r\n".join(out) + "\r\n"


CLAUDE_CONFIGS = [
    {}, {"CLAUDE_AI_FPS": "2", "CLAUDE_AI_MAX_FRAMES": "50.7", "CLAUDE_CLI_PATH": " /opt/claude ", "CLAUDE_MODEL": " opus ",
         "CLAUDE_CLI_TIMEOUT": "90"},
    {"CLAUDE_AI_FPS": "abc"}, {"CLAUDE_AI_MAX_FRAMES": "inf"}, {"CLAUDE_CLI_TIMEOUT": "nan"}, {"CLAUDE_MODEL": "   "},
    {"CLAUDE_AI_FPS": " 0.5 ", "CLAUDE_CLI_TIMEOUT": "1e2"}, {"CLAUDE_CLI_PATH": "nonexistent-cli-aibase"},
]

ADAPTIVE = [[None, 1, 95], [0, 1, 95], [-5, 1, 95], [50, 1, 95], [95, 1, 95], [95.5, 1, 95], [190, 1, 95], [191, 1, 95],
            [500, 1, 95], [130.5, 2.0, 50], [10, 1, 0], [10, 1, -1], [300, 0.5, 95], ["nan", 1, 95], ["inf", 1, 95], [1e-9, 1, 95]]

MEDIA = ["a.PNG", "b.jpg", "c.JPEG", "d.gif", "e.webp", "f.bmp", "g", "h.tar.png", "dir.png/i", ".png"]

CLAUDE_IMAGE = [
    {"image": "shot.png", "prompt": "Is the paytable open?", "schema": CHECK, "env": {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "BASELINE_OTHER": "x"},
     "procs": {"cli": [{"stdout": ENVELOPE_OK}]}},
    {"image": "shot.png", "prompt": "Pages", "schema": ODD_SCHEMA, "extras": ["extra1.png", "extra2.png"],
     "reference": {"game": "synth", "base": "<TMP>", "groups": ["symbols"], "assets": None},
     "procs": {"cli": [{"stdout": json.dumps({"result": "{\"n\": 1}"})}]}},
    {"image": "shot.png", "prompt": "Error", "schema": CHECK, "procs": {"cli": [{"stdout": json.dumps({"is_error": True, "result": "rate limited"})}]}},
    {"image": "shot.png", "prompt": "Error no result", "schema": CHECK, "procs": {"cli": [{"stdout": json.dumps({"is_error": True, "result": None, "type": "result"})}]}},
    {"image": "shot.png", "prompt": "Plain text", "schema": CHECK, "procs": {"cli": [{"stdout": "  just words  \n", "stderr": "warn"}]}},
    {"image": "shot.png", "prompt": "Exit 2", "schema": CHECK, "procs": {"cli": [{"rc": 2, "stdout": "", "stderr": "e" * 400}]}},
    {"image": "shot.png", "prompt": "List envelope", "schema": CHECK, "procs": {"cli": [{"stdout": "[1]"}]}},
    {"image": "shot.png", "prompt": "No result key", "schema": CHECK, "procs": {"cli": [{"stdout": "{\"type\": \"result\"}"}]}},
    {"image": "shot.png", "prompt": "Timeout", "schema": CHECK, "procs": {"cli": [{"raise": "timeout"}]}},
    {"image": "missing.png", "prompt": "Missing", "schema": CHECK, "procs": {}},
    {"image": "shot.png", "prompt": "Strict", "schema": CHECK, "env": {"AGENT_REASONING": "0", "CLAUDE_MODEL": "haiku", "CLAUDE_CLI_TIMEOUT": "30"},
     "procs": {"cli": [{"stdout": ENVELOPE_OK}]}},
    {"image": "shot.png", "prompt": "Missing extra", "schema": CHECK, "extras": ["nothere.png"], "procs": {}},
    {"image": "shot.png", "prompt": "Empty ref", "schema": CHECK, "reference": {"game": "synth", "base": "<TMP>", "groups": [], "assets": []},
     "procs": {"cli": [{"stdout": json.dumps({"result": "no json"})}]}},
    {"image": "shot.png", "prompt": "Missing CLI", "schema": CHECK, "env": {"CLAUDE_CLI_PATH": "nonexistent-cli-aibase"}, "procs": {"cli": [{"raise": "missing"}]}},
]

STREAM_OK = stream({"type": "system", "subtype": "init"}, {"type": "assistant"},
                   {"type": "result", "is_error": False, "result": "Frames show spinning.\n{\"passed\": true, \"confidence\": \"medium\"}"})

CLAUDE_VIDEO = [
    {"prompt": "Do the reels spin?", "schema": CHECK, "probe": ["1280x720\n", "12.0\n"], "frames": 4, "stale": 2,
     "env": {"CLAUDECODE": "1"}, "cli": [{"stdout": STREAM_OK}]},
    {"prompt": "Long clip with refs", "schema": CHECK, "probe": ["1920x1080\n", "400.25\n"], "frames": 3,
     "reference": {"game": "synth", "base": "<TMP>", "groups": ["symbols", "ui"], "assets": None}, "cli": [{"stdout": STREAM_OK}]},
    {"prompt": "Probe fails", "schema": CHECK, "probe_actions": [{"rc": 1, "stderr": "bad"}, {"rc": 1, "stderr": "bad"}], "frames": 2,
     "cli": [{"stdout": STREAM_OK}]},
    {"prompt": "No ffprobe", "schema": CHECK, "probe_actions": [{"raise": "missing"}, {"raise": "timeout"}], "frames": 1,
     "cli": [{"stdout": STREAM_OK}]},
    {"prompt": "Odd probe text", "schema": CHECK, "probe": ["1280\n", "N/A\n"], "frames": 1, "cli": [{"stdout": STREAM_OK}]},
    {"prompt": "Sampling fails", "schema": CHECK, "probe": ["1280x720\n", "10\n"], "ffmpeg": [{"rc": 1, "stderr": "nope"}], "cli": []},
    {"prompt": "No frames", "schema": CHECK, "probe": ["1280x720\n", "10\n"], "frames": 0, "cli": []},
    {"prompt": "Sampling timeout", "schema": CHECK, "probe": ["1280x720\n", "10\n"], "ffmpeg": [{"raise": "timeout"}], "cli": []},
    {"prompt": "Stream error", "schema": CHECK, "probe": ["640x360\n", "5\n"], "frames": 2,
     "cli": [{"stdout": stream({"type": "result", "is_error": True, "result": "Prompt is too long"})}]},
    {"prompt": "No result event", "schema": CHECK, "probe": ["640x360\n", "5\n"], "frames": 2,
     "cli": [{"stdout": stream({"type": "assistant", "text": "x" * 300}) + "y" * 400}]},
    {"prompt": "Timeout", "schema": CHECK, "probe": ["640x360\n", "5\n"], "frames": 2, "cli": [{"raise": "timeout"}]},
    {"prompt": "Small budget", "schema": ODD_SCHEMA, "probe": ["1280x720\n", "60\n"], "frames": 2,
     "env": {"CLAUDE_AI_TOKEN_BUDGET": "5000", "CLAUDE_AI_FPS": "2", "CLAUDE_AI_MAX_FRAMES": "30"}, "cli": [{"stdout": STREAM_OK}]},
    {"prompt": "Line separator", "schema": CHECK, "probe": ["640x360\n", "5\n"], "frames": 1,
     "cli": [{"stdout": stream({"type": "result", "result": "split" + C(0x2028) + "here {\"passed\": true}"},
                               {"type": "result", "result": "{\"passed\": false, \"reason\": \"second\"}"}, junk=False)}]},
    {"prompt": "Result is_error false string", "schema": CHECK, "probe": ["640x360\n", "5\n"], "frames": 1,
     "cli": [{"stdout": stream({"type": "result", "is_error": "", "result": None})}]},
    {"prompt": "Huge frames", "schema": CHECK, "probe": ["7680x4320\n", "30\n"], "frames": 1,
     "env": {"CLAUDE_AI_TOKEN_BUDGET": "abc"}, "cli": [{"stdout": STREAM_OK}]},
]


def claude_flows():
    configs = []
    for e in CLAUDE_CONFIGS:
        with env(e):
            def mk():
                v = claude_ai.ClaudeVideoHelper()
                i = claude_ai.ClaudeImageHelper()
                return {"attrs": {k: getattr(v, k) for k in ("_fps", "_max_frames", "_cli_path", "_model", "_timeout")},
                        "model": v.model, "cost_for_tokens": v.cost_for_tokens(5, 5), "video_cost": v.cost_summary(),
                        "image_cost": i.cost_summary(), "context": v.create_cached_context([]),
                        "active": v.active_context_count, "resolve_cli": norm(v._resolve_cli())}
            configs.append({"env": e, "out": rec(mk)})

    with env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "BASELINE_OTHER": "y"}):
        sub_env = {k: (k in claude_ai._claude_subprocess_env()) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BASELINE_OTHER")}

    synth_ref = ref_from({"game": "synth", "base": "<TMP>", "groups": ["symbols", "ui"], "assets": None})
    with env():
        img = claude_ai.ClaudeImageHelper()
        vid = claude_ai.ClaudeVideoHelper()
        vid_ref = claude_ai.ClaudeVideoHelper(reference=synth_ref)
        prompts = {
            "image": [
                {"in": ["Task A", [], None], "out": img._build_image_prompt("Task A", [], None)},
                {"in": ["Task B", ["screenshot-extra-1.png", "screenshot-extra-2.png"], "synth"],
                 "out": norm(img._build_image_prompt("Task B", ["screenshot-extra-1.png", "screenshot-extra-2.png"], synth_ref))},
                {"in": ["Task C", ["screenshot-extra-1.png"], "empty"],
                 "out": img._build_image_prompt("Task C", ["screenshot-extra-1.png"], sr.SymbolReference(header="h"))},
            ],
            "inline": [{"in": [p, n_refs, n], "out": vid._build_inline_video_prompt(p, [(Path("x"), "l")] * n_refs, n)}
                       for p, n_refs, n in [("Spin?", 0, 12), ("Spin?\nmulti", 3, 1), ("", 1, 0)]],
            "read_tool": [vid._build_video_prompt("Old path"), vid_ref._build_video_prompt("Old path with assets")],
        }
        assets_file = vid_ref._write_assets_file(Path(tmp("assets", "work", "x")).parent)
        assets = {"path": norm(str(assets_file)), "text": norm(Path(assets_file).read_bytes().decode("utf-8")),
                  "none": vid._write_assets_file(Path(tmp("assets", "none", "x")).parent)}

    image_flows = []
    for n, case in enumerate(CLAUDE_IMAGE):
        d = tmp("ci", str(n), "x")
        d = os.path.dirname(d)
        if case["image"] != "missing.png":
            shutil.copyfile(real("<TMP>/img/shot.png"), os.path.join(d, case["image"]))
        for e in case.get("extras", []):
            if e != "nothere.png":
                shutil.copyfile(real("<TMP>/img/extra1.png"), os.path.join(d, e))
        procs = Procs(case["procs"])
        with env(case.get("env")), procs.active():
            helper = claude_ai.ClaudeImageHelper()
            res = helper.analyse_image(Path(d) / case["image"], case["prompt"], copy.deepcopy(case["schema"]),
                                       extra_images=[Path(d) / e for e in case.get("extras", [])] or None,
                                       reference=ref_from(case.get("reference")))
        staged = sorted(os.listdir(os.path.join(d, Path(case["image"]).stem + "-claude"))) if os.path.isdir(
            os.path.join(d, Path(case["image"]).stem + "-claude")) else None
        image_flows.append({"in": case, "dir": f"ci/{n}", "result": result_of(res), "calls": procs.calls,
                            "staged": staged, "cost": plain(helper.cost_summary())})

    video_flows = []
    for n, case in enumerate(CLAUDE_VIDEO):
        d = os.path.dirname(tmp("cv", str(n), "x"))
        clip = Path(d) / "clip.webm"
        work = Path(d) / "clip-claude"
        work.mkdir(exist_ok=True)
        for i in range(case.get("stale", 0)):
            (work / ("frame-%04d.jpg" % (90 + i))).write_bytes(b"stale")
        probe = case.get("probe_actions") or [{"stdout": s} for s in case["probe"]]
        script = {"ffprobe": probe, "ffmpeg": case.get("ffmpeg") or [{"frames": case.get("frames", 0)}], "cli": case["cli"] or [{}]}
        procs = Procs(script)
        with env(case.get("env")), procs.active():
            helper = claude_ai.ClaudeVideoHelper()
            res = helper.analyse_video(clip, None, case["prompt"], copy.deepcopy(case["schema"]),
                                       reference=ref_from(case.get("reference")))
        frames = sorted(os.listdir(work))
        video_flows.append({"in": case, "dir": f"cv/{n}", "result": result_of(res), "calls": procs.calls,
                            "frames": frames, "cost": plain(helper.cost_summary())})

    return {
        "configs": configs, "subprocess_env": sub_env,
        "adaptive_fps": [{"in": a, "out": rec(claude_ai._adaptive_fps, float(a[0]) if isinstance(a[0], str) else a[0], a[1], a[2])} for a in ADAPTIVE],
        "media_type": [{"in": m, "out": claude_ai._media_type(Path(m))} for m in MEDIA],
        "prompts": prompts, "assets_file": assets, "image": image_flows, "video": video_flows,
    }


# ── symbol references ────────────────────────────────────────────────────────────────

SYMBOL_LOADS = [
    ["grandma", "<ROOT>", None], ["zeus", "<ROOT>", None], ["chaos", "<ROOT>", None], ["mystic", "<ROOT>", None],
    ["nonexistent", "<ROOT>", None], ["zeus", "<ROOT>", ["ui"]], ["zeus", "<ROOT>", ["ui_elements"]], ["zeus", "<ROOT>", ["bogus"]],
    ["zeus", "<ROOT>", []], ["synth", "<TMP>", None], ["synth", "<TMP>", ["ui", "symbols"]], ["legacy", "<TMP>", None],
    ["badyaml", "<TMP>", None], ["listroot", "<TMP>", None], ["emptyfile", "<TMP>", None], ["intname", "<TMP>", None],
    ["nodesc", "<TMP>", None],
]

SELECTIONS = [
    ["zeus", "<ROOT>", ["symbols"], None], ["zeus", "<ROOT>", None, ["symbols.wild", "ui.base_game", "bogus.x", "noqual", "symbols.ace"]],
    ["zeus", "<ROOT>", ["symbols"], ["symbols.wild"]], ["grandma", "<ROOT>", [], ["symbols.a"]], ["zeus", "<ROOT>", None, None],
    ["zeus", "<ROOT>", "symbols", None], ["zeus", "<ROOT>", [], []], ["synth", "<TMP>", ["symbols", "ui", "nope"], ["ui.dup", "symbols.gamma", "x.y.z"]],
    ["synth", "<TMP>", None, ["symbols.alpha", "symbols.dup", "symbols.eps"]], ["nonexistent", "<ROOT>", ["symbols"], None],
    ["synth", "<TMP>", None, [5]], ["synth", "<TMP>", None, [["symbols.alpha"]]], ["intname", "<TMP>", ["symbols"], None],
]

EFFECTIVE = [
    [None, None, None], [{"asset_groups": ["a"]}, None, None], [{"asset_groups": ["a"]}, {"assets": ["b.c"]}, None],
    [{"asset_groups": ["a"]}, {"assets": ["b.c"]}, {"asset_groups": [], "ref_assets": ["x"]}],
    [None, None, {"ref_assets": ["wild", "screen", "nope", "dup"]}], [None, {"ref_assets": ["nope"]}, None],
    [{"asset_groups": "notalist"}, None, None], [None, "notadict", {"assets": None}], [None, None, {"ref_assets": []}],
]


def symbols():
    def summary(ref):
        return ref_summary(ref) | {"descriptions": norm(ref.descriptions_text())}

    loads = [{"in": c, "out": rec(lambda g, b, cats: summary(sr.load_symbol_reference(g, Path(real(b)), cats)), *c)} for c in SYMBOL_LOADS]
    sels = [{"in": c, "out": rec(lambda g, b, gr, it: summary(sr.load_reference_selection(g, Path(real(b)), gr, it)), *c)} for c in SELECTIONS]
    indexes = [{"in": [g, b], "out": rec(lambda g, b: sr.build_asset_index(g, Path(real(b))), g, b)}
               for g, b in [["zeus", "<ROOT>"], ["synth", "<TMP>"], ["nonexistent", "<ROOT>"], ["badyaml", "<TMP>"]]]
    synth_index = sr.build_asset_index("synth", Path(TMP))
    effective = [{"in": c, "out": rec(sr.effective_selection, *copy.deepcopy(c), synth_index)} for c in EFFECTIVE]
    effective.append({"in": [None, None, {"ref_assets": ["wild"]}], "no_index": True,
                      "out": rec(sr.effective_selection, None, None, {"ref_assets": ["wild"]}, None)})
    ordered = sr._ordered_groups({"ui": set(), "extra": set(), "symbols": set(), "ui_elements": set()})
    cache = sr.ScopedReferenceCache("synth", Path(TMP))
    a = cache.for_selection(["symbols"], None)
    b = cache.for_selection(["symbols"], None)
    c = cache.for_selection(None, None)
    d = cache.scoped(None)
    e = cache.for_selection([], None)
    f = cache.for_selection(None, [])
    cache_out = {"same": a is b, "full_same": c is d, "distinct": e is not f, "full": summary(c), "groups": summary(a)}
    return {"loads": loads, "selections": sels, "indexes": indexes, "effective": effective, "ordered": ordered,
            "cache": cache_out, "default_sections": sr._DEFAULT_SECTIONS}


REFERENCE_FOR = [
    [None, {}], ["zeus", {}], ["zeus", {"asset_groups": []}], ["zeus", {"asset_groups": [], "assets": []}], ["zeus", {"assets": []}],
    ["zeus", {"assets": [], "asset_groups": ["ui"]}], ["zeus", {"asset_groups": ["symbols"]}], ["zeus", {"assets": ["symbols.wild"]}],
    ["nonexistent", {}], ["zeus", {"asset_groups": None, "assets": None}], ["zeus", {"asset_groups": [], "assets": None}],
    ["", {}], ["zeus", {"assets": ["ui_elements.spin_button", "ui.free_spin"]}],
]


def reference_for():
    out = []
    for game, spec in REFERENCE_FOR:
        with captured() as buf:
            r = rec(lambda: ref_summary(ai_video._reference_for(game, spec)))
        out.append({"in": [game, spec], "out": r, "stdout": lines(buf)})
    return out


# ── provider selection ───────────────────────────────────────────────────────────────

PROVIDER_ENVS = [
    {}, {"GEMINI_API_KEY": "  k1  "}, {"GEMINI_API_KEY": "k", "GEMINI_VIDEO_MODEL": "gemini-2.5-flash", "GEMINI_VIDEO_RESOLUTION": "low"},
    {"GEMINI_API_KEY": "k", "GEMINI_VIDEO_MODEL": "", "GEMINI_VIDEO_RESOLUTION": ""}, {"AI_PROVIDER": " Claude "},
    {"AI_PROVIDER": "claude", "CLAUDE_AI_MAX_FRAMES": "inf"}, {"AI_PROVIDER": "local"},
    {"AI_PROVIDER": "local", "LOCAL_AI_ENDPOINT": " http://h:9000/analyze "}, {"AI_PROVIDER": "openai", "GEMINI_API_KEY": "k"},
    {"AI_PROVIDER": "", "GEMINI_API_KEY": "k"}, {"AI_PROVIDER": "gemini", "GEMINI_API_KEY": "   "},
    {"AI_PROVIDER": "GEMINI", "GEMINI_API_KEY": "k", "GEMINI_VIDEO_RESOLUTION": "High", "GEMINI_IMAGE_MODEL": "ignored"},
]

ATTRS = ["api_key", "_model", "_resolution", "_fps", "_max_frames", "_cli_path", "_timeout", "_endpoint"]


def describe(p):
    if p is None:
        return None
    return {"class": type(p).__name__, "attrs": {k: getattr(p, k) for k in ATTRS if hasattr(p, k)},
            "model": getattr(p, "model", None), "cost": plain(p.cost_summary())}


def providers():
    out = []
    for e in PROVIDER_ENVS:
        with env(e), captured() as buf:
            v = describe(ai_video.make_video_provider("dev"))
            i = describe(ai_video.make_image_provider("dev"))
        out.append({"env": e, "video": v, "image": i, "stdout": lines(buf)})
    return out


# ── the whole pass (run_ai) ──────────────────────────────────────────────────────────

def shot(run, name, label):
    return {"label": label, "path": f"<TMP>/{run}/shots/{name}", "file": name}


SCHEMA_SPEC = {"kind": "schema", "prompt": "Count the cats", "output_schema": {"cats": {"type": "integer"}},
               "status": "pending", "at": None, "asset_groups": None, "assets": None}

RUN_AI_RUNS = [
    {"dir": "run1", "timeline": TIMELINE, "cases": [
        {"id": "c1", "title": "one", "stream": [], "verdict": "pending", "video": [
            {"kind": "check", "check": "Reels spin", "window": [1.0, 3.0], "status": "pending", "asset_groups": [], "assets": None},
            {"kind": "read", "read": "WIN", "equals": "12.5", "window": [2.0, 4.0], "status": "pending", "asset_groups": ["ui"], "assets": None},
        ], "image": [{"kind": "schema", "status": "pending", "combine": False, "prompt": "Language is {shot.label}?",
                      "output_schema": {"ok": {"type": "boolean"}}, "expected": {"ok": True},
                      "shots": [shot("run1", "a.png", "th Thai"), shot("run1", "b.png", "my Burmese")]}]},
        {"id": "c2", "title": "two", "stream": [], "verdict": "pending", "video": [
            dict(SCHEMA_SPEC, group="shared-1", window=[0, 5], timestamp=True, expected={"cats": 3})],
         "image": [{"kind": "schema", "status": "pending", "combine": True, "prompt": "RTP present?", "output_schema": {},
                    "expected": {"rtp": True}, "shots": [shot("run1", "p1.png", "help 1"), shot("run1", "p2.png", "help 2"),
                                                         shot("run1", "p3.png", "help 3")]}]},
        {"id": "c3", "title": "three", "stream": [], "verdict": "pending", "video": [
            dict(SCHEMA_SPEC, group="shared-1", window=[0, 5], timestamp=True, expected={"cats": 4})],
         "image": [{"kind": "schema", "status": "pending", "combine": False, "group": "shared-img-1", "prompt": "Clipped?",
                    "output_schema": {}, "expected": {"clipped": False}, "shots": [shot("run1", "a.png", "th Thai"), shot("run1", "c.png", "vi")]},
                   {"kind": "schema", "status": "pending", "combine": False, "group": "shared-img-1", "prompt": "Clipped?",
                    "output_schema": {}, "expected": {"clipped": True}, "shots": [shot("run1", "a.png", "th Thai"), shot("run1", "c.png", "vi")]}]},
        {"id": "c4", "title": "each", "stream": [], "verdict": "pending", "video": [
            dict(SCHEMA_SPEC, group="shared-2", each="bonus_rounds", timestamp=True, window=None,
                 windows=[{"seq": 0, "window": [0, 2]}, {"seq": 3, "window": [5, 7]}], expected={"cats": 1})]},
        {"id": "c5", "title": "each shared", "stream": [], "verdict": "pending", "video": [
            dict(SCHEMA_SPEC, group="shared-2", each="bonus_rounds", timestamp=True, window=None,
                 windows=[{"seq": 0, "window": [0, 2]}, {"seq": 3, "window": [5, 7]}], expected={"cats": 2})]},
        {"id": "c6", "title": "each empty", "stream": [], "verdict": "pending", "video": [
            dict(SCHEMA_SPEC, each=["a", "b"], windows=[], window=None, expected={})]},
        {"id": "c7", "title": "not pending", "stream": [], "verdict": "pending", "video": [
            {"kind": "check", "check": "done", "status": "pass", "window": None}],
         "image": [{"kind": "schema", "status": "pending", "prompt": "x", "shots": []}]},
        {"id": "c8", "title": "red stays red", "stream": [], "verdict": "red", "video": [
            {"kind": "check", "check": "still graded", "status": "pending", "window": [0, 1], "high_fps": True}]},
        {"id": "c9", "title": "errors", "stream": [], "verdict": "pending", "video": [
            {"kind": "check", "check": "fails", "status": "pending", "window": [0, 1]},
            {"kind": "check", "check": "raises", "status": "pending", "window": [0, 1]},
            {"check": "no kind", "status": "pending", "window": [0, 1]}]},
        {"id": 10, "title": "numeric id", "stream": [], "verdict": "pending", "video": [
            {"kind": "check", "check": "id 10", "status": "pending", "window": None, "assets": ["symbols.wild"]}]},
    ]},
    {"dir": "run2", "timeline": None, "cases": [
        {"id": "d1", "title": "trim fails", "stream": [], "verdict": "pending", "video": [
            {"kind": "check", "check": "never seen", "status": "pending", "window": [0, 1]}]},
        {"id": "d2", "title": "images", "stream": [], "verdict": "pending", "image": [
            {"kind": "schema", "status": "pending", "prompt": "Bad shot?", "expected": {"ok": True},
             "shots": [shot("run2", "x.png", "x"), shot("run2", "y.png", "y")]}]},
        {"id": "d3", "title": "nothing pending", "stream": [], "verdict": "green", "video": [], "image": []},
    ]},
    {"dir": "run3", "timeline": None, "cases": [
        {"id": "e1", "title": "only done", "stream": [], "verdict": "orange", "video": [{"kind": "check", "status": "orange"}]},
    ]},
]

ANSWERS_VIDEO = {
    "c1-0.webm": dict(OK, data={"passed": True, "confidence": "high", "reason": "spun"}),
    "c1-1.webm": dict(OK, data={"value": "12.50", "confidence": "high"}),
    "c2-0.webm": dict(OK, data={"cats": 3, "reason": "three"}),
    "c4-0-r0.webm": dict(OK, data={"cats": 1}),
    "c4-0-r3.webm": dict(OK, data={"cats": 2, "reason": "two"}),
    "c8-0.webm": dict(OK, data={"passed": True, "confidence": "high"}),
    "c9-0.webm": {"success": False, "data": None, "reason": "model overloaded"},
    "c9-1.webm": {"raise": "connection reset"},
}
ANSWERS_IMAGE = {
    "a.png": dict(OK, data={"ok": True, "clipped": False, "reason": "fine"}),
    "b.png": dict(OK, data={"ok": False, "reason": "wrong script"}),
    "p1.png": dict(OK, data={"rtp": True}),
    "c.png": dict(OK, data={"clipped": True, "ok": True}),
    "x.png": {"raise": "image exploded"},
    "y.png": {"success": False, "data": None, "reason": "nope"},
}

RUN_AI = [
    {"name": "both", "video": True, "image": True, "workers": 1, "game": "zeus", "ffmpeg_fail": ["d1-0.webm"], "env": {}},
    {"name": "image only", "video": False, "image": True, "workers": 1, "game": None, "ffmpeg_fail": [], "env": {}},
    {"name": "video only, default workers", "video": True, "image": False, "workers": None, "game": "grandma",
     "ffmpeg_fail": [], "env": {"AI_CONCURRENCY": "3"}, "sorted_calls": True},
]


def materialise(run_spec, root_name):
    """The cases of one run, with <TMP> paths made real (shots) and its timeline written."""
    run_dir = os.path.dirname(tmp(root_name, run_spec["dir"], "x"))
    if run_spec["timeline"] is not None:
        with open(os.path.join(run_dir, "timeline.json"), "w", encoding="utf-8") as fh:
            json.dump(run_spec["timeline"], fh)
    cases = copy.deepcopy(run_spec["cases"])
    for c in cases:
        for sp in c.get("image", []):
            for sh in sp.get("shots", []):
                sh["path"] = real(sh["path"].replace("<TMP>/", f"<TMP>/{root_name}/"))
    return Path(run_dir), cases


def run_ai():
    out = []
    for n, scen in enumerate(RUN_AI):
        root_name = f"runai{n}"
        runs = [materialise(r, root_name) for r in RUN_AI_RUNS]
        session = Path(tmp(root_name, "session.webm"))
        session.write_bytes(b"session")
        vstub = StubVideo(ANSWERS_VIDEO, dict(OK, data={"passed": True, "confidence": "medium", "reason": "default"}))
        istub = StubImage(ANSWERS_IMAGE, dict(OK, data={"ok": True}))
        procs = Procs({"ffmpeg": [{"create": True, "fail_names": scen["ffmpeg_fail"]}]})
        with env(scen["env"]), procs.active(), captured() as buf:
            r = rec(ai_video.run_ai, vstub if scen["video"] else None, istub if scen["image"] else None, session, runs,
                    game=scen["game"], workers=scen["workers"])
        vcalls, icalls, pcalls = vstub.calls, istub.calls, procs.calls
        if scen.get("sorted_calls"):
            vcalls = sorted(vcalls, key=lambda c: c["clip"])
            pcalls = sorted(pcalls, key=lambda c: json.dumps(c["cmd"]))
        out.append({"in": scen, "root": root_name, "out": r, "cases": norm(plain([c for _, cs in runs for c in cs])),
                    "video_calls": vcalls, "image_calls": icalls, "ffmpeg": pcalls, "stdout": lines(buf),
                    "clips_dirs": [os.path.isdir(os.path.join(str(d), "_clips")) for d, _ in runs]})
    return {"runs": RUN_AI_RUNS, "answers_video": ANSWERS_VIDEO, "answers_image": ANSWERS_IMAGE, "scenarios": out}


# ── main ─────────────────────────────────────────────────────────────────────────────

def main():
    make_files()
    try:
        with env():
            data = {
                "env_keys": ENV_KEYS, "files": FILES, "grading": grading(),
                **single_asserts(), "timeline_hint": timeline_hints(), "trim": trims(), "montage": montage(),
                **gemini_flows(), "text": text_parsing(), "local": local_flows(), "claude": claude_flows(),
                "symbols": symbols(), "reference_for": reference_for(), "providers": providers(), "run_ai": run_ai(),
                "missing_exe": str(MISSING_EXE),
            }
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    write("ai.json", data)
    counts = {k: (len(v) if isinstance(v, list) else sum(len(x) for x in v.values() if isinstance(x, list)) if isinstance(v, dict) else 1)
              for k, v in data.items()}
    print("cases:", sum(counts.values()), counts)


if __name__ == "__main__":
    main()
