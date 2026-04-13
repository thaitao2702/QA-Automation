"""
Video AI streaming batch processor for the QA platform.

Groups tests by their sorted ref_assets set. When all tests in a group
complete, the processor uploads the shared reference assets via Gemini context
caching, trims each test's video with ffmpeg, sends trimmed video to Gemini,
and updates result.json with the AI verdict.

Public interface
----------------
VideoAiProcessor(game, suite_doc, base_dir, gemini_video_helper)
    Streaming batch manager. Call notify_test_complete() as tests finish.

    notify_test_complete(record) -> None
        Mark a test as complete. Dispatches the batch if all tests in the
        group are now done.

    wait_all() -> None
        Await all in-flight batch tasks. Call after suite execution finishes.

Standalone CLI
--------------
python framework/runner/video_ai_processor.py \\
    --results-dir test-results/zeus/dev/2026-04-07-suite-06 \\
    --game zeus

Handles Gemini failures gracefully — marks test as ai_result=fail with reason.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from framework.runner.suite_runner import TestRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict calculation (duplicated from suite_runner to avoid import side-effects)
# ---------------------------------------------------------------------------


def _compute_verdict(bridge_result: str, ai_result: str) -> str:
    """
    Compute triple-validation final verdict.

    Intentionally standalone (not imported from suite_runner) to avoid triggering
    Playwright imports when video_ai_processor is used as a standalone CLI tool.
    """
    if bridge_result == "skip" and ai_result == "skip":
        return "skip"
    if ai_result == "pending":
        return "pending"
    if bridge_result == "pass" and ai_result in ("pass", "skip"):
        return "green"
    if bridge_result == "fail" and ai_result == "fail":
        return "red"
    if bridge_result == "fail" or ai_result == "fail":
        return "orange"
    return "green"


# Ensure repo root on path when run standalone
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# ffmpeg trimming
# ---------------------------------------------------------------------------


def _trim_video(
    source: Path,
    dest: Path,
    start_s: float,
    end_s: float,
) -> bool:
    """
    Trim *source* video to the [start_s, end_s] window and write *dest*.

    Uses ``-c copy`` for fast stream copy (no re-encode).

    Returns True on success, False if ffmpeg fails or is not found.
    """
    if not source.exists():
        logger.error("Video source not found for trimming: %s", source)
        return False

    cmd = [
        "ffmpeg",
        "-y",           # overwrite output
        "-ss", str(start_s),
        "-to", str(end_s),
        "-i", str(source),
        "-c", "copy",
        str(dest),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error("ffmpeg failed (exit %d): %s", result.returncode, result.stderr[-500:])
            return False
        return True
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found. Install from https://ffmpeg.org/download.html "
            "or via your OS package manager."
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out trimming %s", source)
        return False


def _resolve_trim_timestamps(
    timeline: dict[str, Any],
    from_step_id: str,
    to_step_id: str,
) -> tuple[float, float] | None:
    """
    Resolve video trim start/end seconds from step IDs in timeline.json.

    The timeline contains step offsets in milliseconds relative to video_start_time.
    video_event_offset is the gap between video recording start and event collection start.

    Returns (start_s, end_s) or None if step IDs not found.
    """
    steps = timeline.get("steps", [])
    step_map: dict[str, dict[str, Any]] = {s["id"]: s for s in steps}

    from_step = step_map.get(from_step_id)
    to_step = step_map.get(to_step_id)

    if from_step is None:
        logger.warning("Trim from_step '%s' not found in timeline", from_step_id)
        return None
    if to_step is None:
        logger.warning("Trim to_step '%s' not found in timeline", to_step_id)
        return None

    # start_ms / end_ms are relative to test_start (executor._test_start_time).
    # video_event_offset = event_collection_start - video_start
    # So step timestamps are already aligned with video_event_offset added.
    video_event_offset_s = float(timeline.get("video_event_offset", 0.0))

    start_s = from_step["start_ms"] / 1000.0 + video_event_offset_s
    end_s = to_step["end_ms"] / 1000.0 + video_event_offset_s

    # Guard against negative / reversed values
    start_s = max(0.0, start_s)
    if end_s <= start_s:
        logger.warning(
            "Trim end (%ss) <= start (%ss) for steps %s→%s; using start+30s fallback",
            end_s,
            start_s,
            from_step_id,
            to_step_id,
        )
        end_s = start_s + 30.0

    return start_s, end_s


# ---------------------------------------------------------------------------
# Batch grouping
# ---------------------------------------------------------------------------


def _group_key(ref_assets: list[str]) -> str:
    """Stable string key for a ref_assets set (sorted, comma-joined)."""
    return ",".join(sorted(ref_assets))


def _extract_video_tests(suite_doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """
    Scan suite_doc and return a mapping of group_key -> list of test info dicts.

    Each test info dict has: test_id, steps (the video AI assert steps).
    """
    groups: dict[str, list[dict[str, Any]]] = {}

    for test in suite_doc.get("tests", []):
        test_id = test.get("id", "")
        video_steps = []

        for step in test.get("steps", []):
            assert_block = step.get("assert", {})
            ai_block = assert_block.get("ai", {})
            if ai_block.get("mode") == "video":
                ref_assets = ai_block.get("ref_assets", [])
                video_steps.append({"step": step, "ref_assets": ref_assets})

        for vs in video_steps:
            key = _group_key(vs["ref_assets"])
            if key not in groups:
                groups[key] = []
            groups[key].append({"test_id": test_id, "ref_assets": vs["ref_assets"]})

    return groups


# ---------------------------------------------------------------------------
# Asset manifest loading
# ---------------------------------------------------------------------------


def _load_asset_paths(
    game: str,
    ref_assets: list[str],
    base_dir: Path,
) -> list[Path]:
    """
    Resolve ref_asset keys to image file paths via the game's symbols.yml manifest.

    Returns a list of resolved image Paths. Missing assets are logged and skipped.
    """
    manifest_path = base_dir / "games" / game / "docs" / "assets" / "symbols.yml"
    if not manifest_path.exists():
        logger.warning("Asset manifest not found: %s", manifest_path)
        return []

    import yaml  # lazy — standard lib not available at module level without install

    with manifest_path.open("r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)

    asset_dir = manifest_path.parent

    # Combine symbols and ui_elements into one lookup
    all_assets: dict[str, Any] = {}
    all_assets.update(manifest.get("symbols", {}))
    all_assets.update(manifest.get("ui_elements", {}))

    paths: list[Path] = []
    for key in ref_assets:
        entry = all_assets.get(key)
        if entry is None:
            logger.warning("ref_asset '%s' not found in manifest", key)
            continue
        file_rel = entry.get("file", "")
        if not file_rel:
            logger.warning("ref_asset '%s' has no 'file' field in manifest", key)
            continue
        img_path = asset_dir / file_rel
        if not img_path.exists():
            logger.warning("ref_asset '%s' image not found: %s", key, img_path)
            continue
        paths.append(img_path)

    return paths


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class VideoAiProcessor:
    """
    Manages streaming batch video AI processing.

    Groups tests by shared ref_assets. When all tests in a group complete
    test execution, dispatches Gemini processing for the group asynchronously.

    Parameters
    ----------
    game:
        Game identifier (e.g. "zeus").
    suite_doc:
        Parsed suite YAML document (used to discover video AI test groups).
    base_dir:
        qa-platform repository root.
    gemini_video_helper:
        GeminiVideoHelper instance. If None, video AI is skipped gracefully.
    """

    def __init__(
        self,
        game: str,
        suite_doc: dict[str, Any],
        base_dir: Path,
        gemini_video_helper: Any | None = None,
    ) -> None:
        self._game = game
        self._suite_doc = suite_doc
        self._base_dir = base_dir
        self._gemini = gemini_video_helper

        # group_key -> set of test_ids expected in this group
        self._groups = _extract_video_tests(suite_doc)
        # group_key -> set of completed test_ids (with their records)
        self._completed: dict[str, dict[str, Any]] = {k: {} for k in self._groups}
        # Asyncio tasks for in-flight batches
        self._tasks: list[asyncio.Task[None]] = []
        self._ready_groups: list[tuple[str, dict[str, Any]]] = []

    async def notify_test_complete(self, record: "TestRecord") -> None:
        """
        Mark a test as complete and dispatch its batch if the group is ready.

        Parameters
        ----------
        record:
            Completed TestRecord from SuiteRunner.
        """
        test_id = record.test_id
        for key, test_list in self._groups.items():
            group_ids = {t["test_id"] for t in test_list}
            if test_id in group_ids:
                self._completed[key][test_id] = record
                # Check if all tests in this group are done
                if set(self._completed[key].keys()) == group_ids:
                    logger.info(
                        "Video AI group '%s' complete (%d tests) — dispatching batch",
                        key,
                        len(group_ids),
                    )
                    self._ready_groups.append((key, dict(self._completed[key])))

    async def wait_all(self) -> None:
        """Process all ready groups and await completion."""
        # Dispatch now (video files are copied by this point)
        for key, records in self._ready_groups:
            task = asyncio.create_task(
                self._process_group(key, records)
            )
            self._tasks.append(task)
        self._ready_groups.clear()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    async def _process_group(
        self,
        group_key: str,
        records: dict[str, Any],
    ) -> None:
        """
        Process a completed batch group:
        1. Upload shared ref_assets via Gemini context caching.
        2. For each test: trim video, send to Gemini, update result.json.
        3. Release cached context.
        """
        if self._gemini is None:
            logger.warning(
                "Video AI skipped for group '%s': GeminiVideoHelper not available", group_key
            )
            for record in records.values():
                self._update_ai_result(record, "fail", "GeminiVideoHelper not configured")
            return

        # Determine ref_assets from first record's group entry
        test_list = self._groups.get(group_key, [])
        if not test_list:
            return
        ref_assets = test_list[0]["ref_assets"]

        # Load asset image paths
        asset_paths = _load_asset_paths(self._game, ref_assets, self._base_dir)
        if not asset_paths:
            logger.warning(
                "No valid asset images for group '%s' — marking tests as fail", group_key
            )
            for record in records.values():
                self._update_ai_result(record, "fail", "No ref_assets found on disk")
            return

        # Upload shared context once
        context_id: str | None = None
        try:
            context_id = await asyncio.to_thread(
                self._gemini.create_cached_context, asset_paths
            )
            logger.info("Gemini context created: %s", context_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to create Gemini cached context: %s", exc)
            for record in records.values():
                self._update_ai_result(record, "fail", f"Gemini context creation failed: {exc}")
            return

        try:
            # Process each test in the group
            for record in records.values():
                await self._process_test(record, context_id)
        finally:
            # Always release context to avoid orphaned cache charges
            try:
                await asyncio.to_thread(self._gemini.release_context, context_id)
                logger.info("Gemini context released: %s", context_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to release Gemini context %s: %s", context_id, exc)

    async def _process_test(self, record: Any, context_id: str) -> None:
        """Trim video for one test and send to Gemini for analysis."""
        artifact = record.artifact
        if artifact is None:
            logger.warning("No artifact for test %s — skipping video AI", record.test_id)
            return

        # Load timeline
        timeline_path = artifact.timeline_path
        if not timeline_path.exists():
            self._update_ai_result(record, "fail", "timeline.json not found")
            return

        with timeline_path.open("r", encoding="utf-8") as fh:
            timeline = json.load(fh)

        # Find all video AI assert steps in this test
        test_doc = record.test_doc
        video_ai_steps = [
            step for step in test_doc.get("steps", [])
            if step.get("assert", {}).get("ai", {}).get("mode") == "video"
        ]

        if not video_ai_steps:
            return

        video_path = artifact.video_path

        for step in video_ai_steps:
            ai_block = step["assert"]["ai"]
            video_trim = ai_block.get("video_trim", {})
            from_step = video_trim.get("from_step", "")
            to_step = video_trim.get("to_step", "")
            prompt = ai_block.get("prompt", "")
            output_schema = ai_block.get("output_schema", {})
            expected = ai_block.get("expected", {})
            step_id = step.get("id", "")

            # Resolve trim timestamps
            timestamps = _resolve_trim_timestamps(timeline, from_step, to_step)
            if timestamps is None:
                self._update_ai_result(
                    record, "fail",
                    f"Could not resolve trim timestamps for step {step_id}"
                )
                continue

            start_s, end_s = timestamps
            trimmed_path = artifact.test_dir / "trimmed.webm"

            # Trim video
            trim_ok = await asyncio.to_thread(
                _trim_video, video_path, trimmed_path, start_s, end_s
            )
            if not trim_ok:
                self._update_ai_result(
                    record, "fail",
                    f"ffmpeg trimming failed for step {step_id}"
                )
                continue

            # Send to Gemini
            try:
                gemini_result = await asyncio.to_thread(
                    self._gemini.analyse_video,
                    trimmed_path,
                    context_id,
                    prompt,
                    output_schema,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Gemini video analysis failed for %s: %s", record.test_id, exc)
                self._update_ai_result(record, "fail", f"Gemini API error: {exc}")
                continue

            if not gemini_result.success:
                ai_data = {
                    "passed": False,
                    "reason": gemini_result.reason,
                    "raw_text": gemini_result.raw_text,
                }
            else:
                # Check expected fields
                actual_data = gemini_result.data or {}
                mismatches = []
                for key, exp_val in (expected or {}).items():
                    act_val = actual_data.get(key)
                    if act_val != exp_val:
                        mismatches.append(f"{key}: got {act_val!r}, expected {exp_val!r}")

                passed = len(mismatches) == 0
                ai_data = {
                    "passed": passed,
                    "data": actual_data,
                    "expected": expected,
                    "mismatches": mismatches,
                    "input_tokens": gemini_result.input_tokens,
                    "output_tokens": gemini_result.output_tokens,
                }

            # Persist step AI result JSON
            step_num = self._extract_step_number(step_id)
            if step_num is not None:
                ai_path = artifact.ai_result_path(step_num)
                ai_path.write_text(
                    json.dumps(ai_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Update record ai_result
            if ai_data.get("passed"):
                record.ai_result = "pass"
            else:
                record.ai_result = "fail"
                if not record.error:
                    record.error = ai_data.get("reason") or "; ".join(
                        ai_data.get("mismatches", [])
                    )

        # Recompute final verdict now that AI result is known
        record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)

        # Rewrite result.json with updated AI result
        if artifact:
            artifact.write_result(
                bridge_result=record.bridge_result,
                ai_result=record.ai_result,
                final_verdict=record.final_verdict,
                extra={
                    "error": record.error,
                    "duration_ms": record.duration_ms,
                    "timed_out": record.timed_out,
                    "test_id": record.test_id,
                    "title": record.test_doc.get("title", ""),
                },
            )
            logger.info(
                "Video AI result for %s: ai=%s verdict=%s",
                record.test_id,
                record.ai_result,
                record.final_verdict,
            )

    @staticmethod
    def _update_ai_result(record: Any, ai_result: str, reason: str) -> None:
        """Update record.ai_result and rewrite result.json."""
        record.ai_result = ai_result
        if not record.error and reason:
            record.error = reason

        record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)

        artifact = record.artifact
        if artifact:
            artifact.write_result(
                bridge_result=record.bridge_result,
                ai_result=record.ai_result,
                final_verdict=record.final_verdict,
                extra={
                    "error": record.error,
                    "duration_ms": record.duration_ms,
                    "timed_out": getattr(record, "timed_out", False),
                    "test_id": record.test_id,
                    "title": record.test_doc.get("title", ""),
                },
            )

    @staticmethod
    def _extract_step_number(step_id: str) -> int | None:
        """Extract integer from step id like 'step-02' -> 2."""
        if not step_id:
            return None
        parts = step_id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])
        return None


# ---------------------------------------------------------------------------
# Standalone CLI for reprocessing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reprocess video AI assertions for a completed suite run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python framework/runner/video_ai_processor.py "
            "--results-dir test-results/zeus/dev/2026-04-07-suite-06 --game zeus"
        ),
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        metavar="DIR",
        help="Path to the suite run results directory containing per-test subdirs",
    )
    parser.add_argument(
        "--game",
        required=True,
        metavar="GAME",
        help="Game identifier (e.g. zeus)",
    )
    parser.add_argument(
        "--base-dir",
        metavar="DIR",
        default=None,
        help="qa-platform repository root (default: auto-detected)",
    )
    return parser


async def _reprocess_async(args: argparse.Namespace) -> int:
    """
    Standalone reprocessing: scan a results directory for pending video AI tests
    and rerun Gemini analysis on them.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    base_dir = Path(args.base_dir).resolve() if args.base_dir else _REPO_ROOT
    results_dir = Path(args.results_dir).resolve()

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable not set")
        return 1

    # Find all result.json files with ai_result=pending
    pending_tests: list[Path] = []
    for result_file in sorted(results_dir.rglob("result.json")):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            if data.get("ai_result") == "pending":
                pending_tests.append(result_file)
        except (json.JSONDecodeError, OSError):
            pass

    if not pending_tests:
        logger.info("No pending video AI tests found in %s", results_dir)
        return 0

    logger.info("Found %d pending video AI test(s)", len(pending_tests))

    from framework.helpers.gemini_video import GeminiVideoHelper

    gemini = GeminiVideoHelper(api_key)

    # Build minimal stub records from result.json + timeline.json
    processed = 0
    failed = 0
    for result_file in pending_tests:
        test_dir = result_file.parent
        try:
            result_data = json.loads(result_file.read_text(encoding="utf-8"))
            timeline_path = test_dir / "timeline.json"
            if not timeline_path.exists():
                logger.warning("No timeline.json in %s — skipping", test_dir)
                failed += 1
                continue
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read artifacts in %s: %s", test_dir, exc)
            failed += 1
            continue

        logger.info("Reprocessing: %s", test_dir.name)

        # We need the suite YAML to get the step prompts/schemas.
        # Parse test_id and suite name from dir name: {date}-{suite}-{test_id}
        dir_name = test_dir.name
        test_id = result_data.get("test_id", "")
        # Suite name extraction: strip leading date and trailing test_id
        # Pattern: 2026-04-07-suite-06-freespin-ZZ-30 -> suite = "suite-06-freespin"
        suite_match = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)-[A-Z]+-\d+$", dir_name)
        suite_name = suite_match.group(1) if suite_match else ""

        suite_path = base_dir / "games" / args.game / "test-suites" / f"{suite_name}.yml"
        if not suite_path.exists():
            logger.warning("Suite file not found: %s — skipping", suite_path)
            failed += 1
            continue

        import yaml
        with suite_path.open("r", encoding="utf-8") as fh:
            suite_doc = yaml.safe_load(fh)

        # Find the test in the suite
        test_doc = next(
            (t for t in suite_doc.get("tests", []) if t.get("id") == test_id),
            None,
        )
        if test_doc is None:
            logger.warning("Test %s not found in suite %s — skipping", test_id, suite_name)
            failed += 1
            continue

        # Build a minimal stub record
        from framework.helpers.artifact import ArtifactContext

        # Derive artifact params from directory path and result
        # test_dir: base_dir/test-results/{game}/{env}/{date-suite-testid}
        # We need env; parse from path
        parts = test_dir.parts
        try:
            results_idx = next(i for i, p in enumerate(parts) if p == "test-results")
            env = parts[results_idx + 2]
        except (StopIteration, IndexError):
            env = "unknown"

        # Re-create ArtifactContext pointing at the existing test_dir
        # run_dir is the parent of the test directory
        artifact = ArtifactContext(
            run_dir=test_dir.parent,
            test_id=test_id,
        )

        # Stub record-like object
        class _StubRecord:
            pass

        stub = _StubRecord()
        stub.test_id = test_id  # type: ignore[attr-defined]
        stub.test_doc = test_doc  # type: ignore[attr-defined]
        stub.artifact = artifact  # type: ignore[attr-defined]
        stub.bridge_result = result_data.get("bridge_result", "skip")  # type: ignore[attr-defined]
        stub.ai_result = "pending"  # type: ignore[attr-defined]
        stub.final_verdict = "pending"  # type: ignore[attr-defined]
        stub.error = result_data.get("error")  # type: ignore[attr-defined]
        stub.duration_ms = result_data.get("duration_ms", 0)  # type: ignore[attr-defined]
        stub.timed_out = result_data.get("timed_out", False)  # type: ignore[attr-defined]

        # Find ref_assets for context upload
        video_steps = [
            step for step in test_doc.get("steps", [])
            if step.get("assert", {}).get("ai", {}).get("mode") == "video"
        ]
        if not video_steps:
            logger.info("No video AI steps found in test %s — skipping", test_id)
            continue

        ref_assets = video_steps[0]["assert"]["ai"].get("ref_assets", [])
        asset_paths = _load_asset_paths(args.game, ref_assets, base_dir)

        if not asset_paths:
            logger.warning("No asset images for test %s — marking fail", test_id)
            VideoAiProcessor._update_ai_result(stub, "fail", "No ref_assets found")
            failed += 1
            continue

        try:
            context_id = gemini.create_cached_context(asset_paths)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to create context for %s: %s", test_id, exc)
            VideoAiProcessor._update_ai_result(stub, "fail", f"Context creation failed: {exc}")
            failed += 1
            continue

        processor = VideoAiProcessor(
            game=args.game,
            suite_doc=suite_doc,
            base_dir=base_dir,
            gemini_video_helper=gemini,
        )
        try:
            await processor._process_test(stub, context_id)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Processing failed for %s: %s", test_id, exc)
            VideoAiProcessor._update_ai_result(stub, "fail", str(exc))
            failed += 1
        finally:
            try:
                gemini.release_context(context_id)
            except Exception:  # noqa: BLE001
                pass

    logger.info("Reprocessing complete: %d processed, %d failed", processed, failed)
    return 0 if failed == 0 else 1


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_reprocess_async(args)))


if __name__ == "__main__":
    main()
