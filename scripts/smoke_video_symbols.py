"""
Smoke test: run LocalVideoHelper WITH the new symbol reference on a real trimmed
video, asking the model to name the symbols visible on each spin.

Verifies the symbol-reference update (symbols.yml descriptions prepended to every
request) works end-to-end against the local /analyze endpoint.

Usage:
    python scripts/smoke_video_symbols.py
    python scripts/smoke_video_symbols.py --video <path> --game zeus
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import os

from framework.env_files import load_env_files
from framework.helpers.local_ai import LocalVideoHelper
from framework.helpers.symbol_reference import load_symbol_reference

DEFAULT_VIDEO = (
    _REPO_ROOT
    / "test-results" / "zeus" / "dev"
    / "temp-zz15-16-04-2026-1542" / "ZZ-15" / "trimmed.webm"
)

PROMPT = (
    "This is a slot game capture that contains one or more spins on a 3x3 reel grid. "
    "Use the reference symbol/UI descriptions above to recognise symbols. "
    "For EACH spin shown in the video (in order), report the symbols visible on the "
    "grid. Be explicit: name each symbol using the reference asset names "
    "(e.g. Wild (Golden Eagle), Coin, Crow (P1), Pentagon/Ace (A), ...). "
    "Also report how many spins you observed."
)

OUTPUT_SCHEMA = {
    "spin_count": {"type": "integer"},
    "spins": {"type": "array"},  # each item: {"spin_index": int, "symbols": [str, ...]}
    "notes": {"type": "string"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default=str(DEFAULT_VIDEO))
    parser.add_argument("--game", default="zeus")
    parser.add_argument("--env", default="dev")
    args = parser.parse_args()

    load_env_files(args.env, _REPO_ROOT)
    endpoint = os.environ.get("LOCAL_AI_ENDPOINT", "http://192.168.1.35:9000/analyze")
    video = Path(args.video)

    ref = load_symbol_reference(args.game, _REPO_ROOT)

    print(f"Endpoint   : {endpoint}")
    print(f"Video      : {video}  ({video.stat().st_size} bytes)")
    print(f"Reference  : {len(ref.items)} symbol/UI descriptions prepended to the prompt")
    print("Calling LocalVideoHelper.analyse_video() with symbol reference ...\n")

    helper = LocalVideoHelper(endpoint, reference=ref)
    t0 = time.time()
    result = helper.analyse_video(video, None, PROMPT, OUTPUT_SCHEMA)
    elapsed = time.time() - t0

    print(f"--- VideoResult (in {elapsed:.1f}s) ---")
    print(f"success : {result.success}")
    print(f"reason  : {result.reason}")
    print(f"data    : {result.data}")
    print("\n--- Raw model text ---")
    print((result.raw_text or "")[:4000])
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
