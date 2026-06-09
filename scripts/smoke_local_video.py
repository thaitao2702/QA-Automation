"""
Smoke test: run LocalVideoHelper against a real trimmed video + the ZZ-15 prompt.

Mirrors what VideoAiProcessor does — calls analyse_video then compares the parsed
data against the step's `expected` map. Endpoint comes from LOCAL_AI_ENDPOINT
(falls back to the known dev endpoint).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.helpers.local_ai import LocalVideoHelper

ENDPOINT = os.environ.get("LOCAL_AI_ENDPOINT", "http://192.168.1.35:9000/analyze")
VIDEO = Path(
    r"D:\Project\qa-platform\test-results\zeus\dev\temp-zz15-15-04-2026-1500\ZZ-15\trimmed.webm"
)

PROMPT = (
    "Watch the auto-spin sequence. Verify: (1) multiple spins occurred in auto "
    "mode, (2) a win amount appeared on screen after a spin, (3) auto-spin stopped "
    "after the win, (4) the game returned to normal idle state with the spin "
    "button visible."
)
OUTPUT_SCHEMA = {
    "multiple_spins_occurred": {"type": "boolean"},
    "win_displayed_after_spin": {"type": "boolean"},
    "auto_stopped_after_win": {"type": "boolean"},
    "returned_to_idle": {"type": "boolean"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}
EXPECTED = {
    "multiple_spins_occurred": True,
    "win_displayed_after_spin": True,
    "auto_stopped_after_win": True,
    "returned_to_idle": True,
    "confidence": "high",
}


def main() -> int:
    print(f"Endpoint : {ENDPOINT}")
    print(f"Video    : {VIDEO}  ({VIDEO.stat().st_size} bytes)")
    print("Calling LocalVideoHelper.analyse_video() ...\n")

    helper = LocalVideoHelper(ENDPOINT)
    t0 = time.time()
    result = helper.analyse_video(VIDEO, "local", PROMPT, OUTPUT_SCHEMA)
    elapsed = time.time() - t0

    print(f"--- VideoResult (in {elapsed:.1f}s) ---")
    print(f"success : {result.success}")
    print(f"reason  : {result.reason}")
    print(f"data    : {result.data}")

    if result.success and result.data is not None:
        mismatches = []
        for key, exp in EXPECTED.items():
            act = result.data.get(key)
            if act != exp:
                mismatches.append(f"  {key}: got {act!r}, expected {exp!r}")
        passed = not mismatches
        print(f"\n--- Expected-field comparison ---")
        print(f"PASSED" if passed else "FAILED — mismatches:")
        for m in mismatches:
            print(m)

    print("\n--- Raw model text (result field) ---")
    print((result.raw_text or "")[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
