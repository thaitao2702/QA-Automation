"""
Suite runner — entry point for QA platform test execution.

CLI usage
---------
python framework/runner/suite_runner.py --game zeus --suite suite-01 --env dev

Execution model
---------------
1. Load env.config.yml, run url_script to get game URL.
2. Validate suite YAML via SchemaValidator.
3. Launch Playwright browser with video recording enabled.
4. Run suite-level setup steps.
5. For each test:
   a. Start bridge event collection, record video_event_offset.
   b. Run test setup → steps → teardown via StepExecutor.
   c. Drain final events, write timeline.json.
   d. Save result.json (ai_result="pending" for video assertions).
   e. Enforce test_timeout_ms — on timeout kill context, save partial artifacts.
   f. Check stop conditions after each verdict.
6. Run suite-level teardown.
7. Notify VideoAiProcessor batches; wait for all video AI to complete.
8. Write summary.json with cost breakdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Ensure qa-platform root is on sys.path when invoked directly
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env from repo root (GEMINI_API_KEY, etc.)
_dotenv_path = _REPO_ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path)

from framework.helpers.artifact import ArtifactContext
from framework.helpers.bridge import BridgeHelper
from framework.helpers.gemini import GeminiImageHelper
from framework.helpers.network import NetworkHelper
from framework.runner.reporter import Reporter, TestResultEntry
from framework.runner.step_executor import StepExecutor, UnsupportedActionError
from framework.runner.video_ai_processor import VideoAiProcessor
from framework.schema_validator import validate_suite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TEST_TIMEOUT_MS = 60_000
_DEFAULT_SUITE_TIMEOUT_MS = 600_000
_VIDEO_DIR = "videos"


# ---------------------------------------------------------------------------
# Test result data
# ---------------------------------------------------------------------------


class TestRecord:
    """Mutable record for a single test's execution state."""

    def __init__(self, test_doc: dict[str, Any], test_idx: int) -> None:
        self.test_doc = test_doc
        self.test_idx = test_idx
        self.test_id: str = test_doc.get("id", f"test-{test_idx}")
        self.bridge_result: str = "skip"
        self.ai_result: str = "skip"
        self.final_verdict: str = "pending"
        self.error: str | None = None
        self.duration_ms: int = 0
        self.timed_out: bool = False
        self.ai_pending_steps: list[dict[str, Any]] = []  # video AI steps for batch
        self.artifact: ArtifactContext | None = None
        self.video_start_time: float = 0.0
        self.event_collection_start: float = 0.0
        self.timeline_steps: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _resolve_game_url(game: str, env: str, base_dir: Path) -> str:
    """
    Load env.config.yml and resolve the game URL for the given environment.

    Mirrors the logic in scripts/get_url.py but imported directly to avoid
    the subprocess overhead when running inline.
    """
    config_path = base_dir / "games" / game / "env.config.yml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"env.config.yml not found: {config_path}. "
            f"Expected at games/{game}/env.config.yml"
        )

    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    environments = config.get("environments", {})
    if env not in environments:
        available = ", ".join(sorted(environments.keys()))
        raise KeyError(
            f"Environment '{env}' not in env.config.yml. "
            f"Available: {available}"
        )

    env_entry = environments[env]
    browser_params = env_entry.get("browser_params", "")

    # Static URL
    if "url" in env_entry:
        base_url = str(env_entry["url"]).strip()
        if browser_params:
            sep = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{sep}{browser_params.lstrip('?&')}"
        return base_url

    # url_script
    if "url_script" in env_entry:
        result = subprocess.run(
            env_entry["url_script"],
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(base_dir),
        )
        if result.returncode != 0:
            raise ValueError(
                f"url_script for '{env}' failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        base_url = result.stdout.strip()
        if not base_url:
            raise ValueError(f"url_script for '{env}' produced no output")
        if browser_params:
            sep = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{sep}{browser_params.lstrip('?&')}"
        return base_url

    raise ValueError(
        f"Environment '{env}' in env.config.yml has neither 'url' nor 'url_script'"
    )


# ---------------------------------------------------------------------------
# Stop condition evaluation
# ---------------------------------------------------------------------------


def _check_stop_conditions(
    stop_conditions: dict[str, Any],
    records: list[TestRecord],
    current_verdict: str,
) -> str | None:
    """
    Evaluate stop conditions after a test completes.

    Returns a human-readable reason string if the suite should stop, or None
    to continue.
    """
    if not stop_conditions:
        return None

    fail_fast = stop_conditions.get("fail_fast", False)
    max_consecutive = stop_conditions.get("max_consecutive_failures")
    max_total = stop_conditions.get("max_total_failures")

    if fail_fast and current_verdict == "red":
        return "fail_fast: stopped on first red verdict"

    # Count total failures (red only — orange is human-review, not hard fail)
    total_red = sum(1 for r in records if r.final_verdict == "red")
    if max_total is not None and total_red >= max_total:
        return f"max_total_failures ({max_total}) reached"

    # Count consecutive failures from the tail
    if max_consecutive is not None:
        consecutive = 0
        for r in reversed(records):
            if r.final_verdict == "red":
                consecutive += 1
            else:
                break
        if consecutive >= max_consecutive:
            return f"max_consecutive_failures ({max_consecutive}) reached"

    return None


# ---------------------------------------------------------------------------
# Verdict calculation
# ---------------------------------------------------------------------------


def _compute_verdict(bridge_result: str, ai_result: str) -> str:
    """
    Compute triple-validation final verdict.

    Rules:
      both pass                  -> green
      ai = pending               -> pending
      one fails, one passes      -> orange
      both fail                  -> red
      bridge = skip              -> skip
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
    # bridge=pass, ai=skip
    return "green"


# ---------------------------------------------------------------------------
# Step sequence runner
# ---------------------------------------------------------------------------


async def _run_steps(
    executor: StepExecutor,
    steps: list[dict[str, Any]],
    context_label: str,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """
    Run a list of steps through executor.

    Returns (all_passed, first_error, pending_video_steps).
    """
    all_passed = True
    first_error: str | None = None
    pending_steps: list[dict[str, Any]] = []

    for step in steps:
        try:
            result = await executor.execute(step)
        except UnsupportedActionError as exc:
            logger.error("[%s] Unsupported action in step %s: %s", context_label, step.get("id"), exc)
            all_passed = False
            first_error = str(exc)
            break

        if result.ai_pending:
            pending_steps.append(step)

        if not result.success:
            all_passed = False
            first_error = first_error or result.error
            # Continue running remaining steps unless it's a hard bridge fail
            # (we want teardown to still run)

    return all_passed, first_error, pending_steps


# ---------------------------------------------------------------------------
# Main runner class
# ---------------------------------------------------------------------------


class SuiteRunner:
    """
    Orchestrates full test suite execution.

    Parameters
    ----------
    game:
        Game identifier (e.g. "zeus").
    suite_name:
        Suite file stem (e.g. "suite-01-core").
    env:
        Environment name (e.g. "dev", "staging").
    base_dir:
        qa-platform repository root. Defaults to the repo root derived from
        this file's location.
    gemini_api_key:
        Gemini API key string. Falls back to GEMINI_API_KEY env var.
    """

    def __init__(
        self,
        game: str,
        suite_name: str,
        env: str,
        base_dir: Path | None = None,
        gemini_api_key: str | None = None,
    ) -> None:
        self.game = game
        self.suite_name = suite_name
        self.env = env
        self.base_dir = base_dir or _REPO_ROOT
        self._api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY", "")

        self._suite_doc: dict[str, Any] = {}
        self._game_url: str = ""
        self._records: list[TestRecord] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        """
        Execute the suite and return the summary dict.

        Writes summary.json to the results directory.
        """
        # Load and validate suite YAML
        suite_path = self._find_suite_file()
        self._suite_doc = self._load_suite(suite_path)

        # Resolve game URL
        self._game_url = _resolve_game_url(self.game, self.env, self.base_dir)
        logger.info("Game URL: %s", self._game_url)

        # Build video AI processor (must happen before suite execution)
        gemini_video_helper = None
        if self._api_key:
            from framework.helpers.gemini_video import GeminiVideoHelper
            gemini_video_helper = GeminiVideoHelper(self._api_key)

        video_processor = VideoAiProcessor(
            game=self.game,
            suite_doc=self._suite_doc,
            base_dir=self.base_dir,
            gemini_video_helper=gemini_video_helper,
        )

        # Gemini image helper (shared across all tests)
        gemini_image: GeminiImageHelper | None = None
        if self._api_key:
            gemini_image = GeminiImageHelper(self._api_key)

        run_dir = self._make_run_dir(
            self.base_dir, self.game, self.env, self.suite_name
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        suite_start = time.time()

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            # Create a browser context with video recording
            video_tmp_dir = run_dir / _VIDEO_DIR
            context_options: dict[str, Any] = {
                "viewport": {"width": 531, "height": 919},
                "record_video_dir": str(video_tmp_dir),
                "record_video_size": {"width": 531, "height": 919},
            }
            context = await browser.new_context(**context_options)
            page = await context.new_page()
            video_start_time = time.time()  # video recording starts with context

            bridge = BridgeHelper(page)
            network = NetworkHelper(context)

            # Log network requests/responses (game API calls)
            _STATIC_EXT = {
                ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                ".webp", ".woff", ".woff2", ".ttf", ".ico", ".map",
                ".mp3", ".ogg", ".wav", ".webm", ".bin", ".plist",
                ".atlas", ".json",  # game asset json (not API json)
            }

            def _on_response(response: Any) -> None:
                url = response.url
                path = url.split("?")[0]
                # Skip static assets by extension
                ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.split("/")[-1] else ""
                if ext in _STATIC_EXT:
                    return
                # Skip data: URLs and blob: URLs
                if url.startswith("data:") or url.startswith("blob:"):
                    return

                status = response.status
                method = response.request.method
                short = path.split("/")[-1] if "/" in path else path
                # Include content length if available
                size = response.headers.get("content-length", "")
                size_str = f" ({size}B)" if size else ""
                color = "\033[32m" if 200 <= status < 400 else "\033[31m"
                logger.info(
                    "  \033[36m↔\033[0m \033[2m%s %s%d\033[0m %s%s",
                    method, color, status, short, size_str,
                )

            page.on("response", _on_response)

            # Run suite-level setup
            suite_setup = self._suite_doc.get("setup", [])
            if suite_setup:
                logger.info("\033[1m\033[36m▶ Suite Setup\033[0m (%d steps)", len(suite_setup))
                setup_artifact = ArtifactContext(
                    run_dir=run_dir,
                    test_id="_setup",
                )
                setup_executor = StepExecutor(
                    bridge=bridge,
                    network=network,
                    artifact=setup_artifact,
                    gemini_image=gemini_image,
                    page=page,
                    game_url=self._game_url,
                )
                await _run_steps(setup_executor, suite_setup, "suite-setup")

            # Execute each test
            config = self._suite_doc.get("config", {})
            test_timeout_ms = int(config.get("test_timeout_ms", _DEFAULT_TEST_TIMEOUT_MS))
            stop_conditions = config.get("stop_conditions") or {}
            stop_reason: str | None = None

            tests = self._suite_doc.get("tests", [])
            for idx, test_doc in enumerate(tests):
                record = TestRecord(test_doc, idx)
                record.video_start_time = video_start_time

                try:
                    await asyncio.wait_for(
                        self._run_one_test(
                            record=record,
                            page=page,
                            context=context,
                            bridge=bridge,
                            network=network,
                            gemini_image=gemini_image,
                            run_dir=run_dir,
                        ),
                        timeout=test_timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    record.timed_out = True
                    record.bridge_result = "fail"
                    record.ai_result = "skip"
                    record.final_verdict = "red"
                    record.error = f"Test timed out after {test_timeout_ms}ms"
                    logger.warning("Test %s timed out", record.test_id)
                    # Drain any remaining events before saving
                    try:
                        if record.artifact:
                            events = await bridge.drain_events()
                            if events:
                                record.artifact.append_events(events)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    record.bridge_result = "fail"
                    record.ai_result = "skip"
                    record.final_verdict = "red"
                    record.error = str(exc)
                    logger.error("Test %s failed with exception: %s", record.test_id, exc)

                # Write result.json (ai_result may be "pending" for video tests)
                if record.artifact:
                    record.artifact.write_result(
                        bridge_result=record.bridge_result,
                        ai_result=record.ai_result,
                        final_verdict=record.final_verdict,
                        extra={
                            "error": record.error,
                            "duration_ms": record.duration_ms,
                            "timed_out": record.timed_out,
                            "test_id": record.test_id,
                            "title": test_doc.get("title", ""),
                        },
                    )

                self._records.append(record)

                # Notify video AI processor that this test is done
                if record.ai_pending_steps and record.artifact:
                    await video_processor.notify_test_complete(record)

                # Check stop conditions
                stop_reason = _check_stop_conditions(
                    stop_conditions, self._records, record.final_verdict
                )
                if stop_reason:
                    logger.warning("Suite stopping: %s", stop_reason)
                    break

            # Run suite teardown
            suite_teardown = self._suite_doc.get("teardown", [])
            if suite_teardown:
                logger.info("\033[1m\033[36m▶ Suite Teardown\033[0m")
                teardown_artifact = ArtifactContext(
                    run_dir=run_dir,
                    test_id="_teardown",
                )
                teardown_executor = StepExecutor(
                    bridge=bridge,
                    network=network,
                    artifact=teardown_artifact,
                    gemini_image=gemini_image,
                    page=page,
                    game_url=self._game_url,
                )
                await _run_steps(teardown_executor, suite_teardown, "suite-teardown")

            # Get video path before closing context (finalises the file)
            video_path: Path | None = None
            try:
                if page.video:
                    video_path = Path(await page.video.path())
            except Exception:  # noqa: BLE001
                pass

            await context.close()
            await browser.close()

        # Trim the session video per test (each test gets only its portion)
        if video_path and video_path.exists():
            for r in self._records:
                if not (r.artifact and r.artifact.test_dir.exists()):
                    continue
                dest = r.artifact.video_path
                # Trim exactly at event_collection_start (no buffer)
                # so video t=0 aligns with event t=0 (offset=0)
                start_s = r.event_collection_start - video_start_time
                end_s = start_s + (r.duration_ms / 1000.0)
                trimmed = False
                try:
                    proc = subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-ss", f"{start_s:.3f}",
                            "-to", f"{end_s:.3f}",
                            "-i", str(video_path),
                            "-c", "copy",
                            str(dest),
                        ],
                        capture_output=True,
                        timeout=30,
                    )
                    if proc.returncode == 0:
                        trimmed = True
                        logger.debug("Trimmed video for %s: %.1fs-%.1fs", r.test_id, start_s, end_s)
                    else:
                        shutil.copy2(video_path, dest)
                        logger.debug("ffmpeg trim failed for %s, copied full video", r.test_id)
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    shutil.copy2(video_path, dest)
                    logger.debug("ffmpeg not available, copied full video for %s", r.test_id)

                # Update timeline: trimmed video starts at event_collection_start,
                # so video_event_offset = 0 (video t=0 == event t=0)
                if trimmed and r.artifact:
                    tl_path = r.artifact.timeline_path
                    if tl_path.exists():
                        try:
                            tl = json.loads(tl_path.read_text(encoding="utf-8"))
                            tl["video_event_offset"] = 0.0
                            tl_path.write_text(
                                json.dumps(tl, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                        except (json.JSONDecodeError, OSError):
                            pass

            # Remove the shared videos directory
            video_dir = video_path.parent
            try:
                shutil.rmtree(video_dir)
                logger.debug("Removed shared video dir: %s", video_dir)
            except Exception:  # noqa: BLE001
                pass

        # Wait for all video AI batches to complete
        await video_processor.wait_all()

        # Process deferred image AI assertions (non-blocking during test execution)
        if gemini_image:
            await self._process_pending_image_ai(gemini_image)

        # Write summary.json
        summary = self._build_summary(
            suite_start=suite_start,
            stop_reason=stop_reason,
            gemini_image=gemini_image,
            gemini_video=gemini_video_helper,
        )
        summary_path = run_dir / "summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Summary written: %s", summary_path)

        # Generate human-readable summary.html alongside summary.json
        _write_summary_html(summary, summary_path.parent / "summary.html")

        # Prepend global setup events to each test's events.jsonl
        # so the replay console shows the full context (navigate, startGame, etc.)
        # Setup event timestamps are relative to setup start (≈ video start).
        # Test event timestamps are relative to event_collection_start.
        # The replay viewer adds video_event_offset to all event.t values.
        # So setup events need negative t values: t_setup - video_event_offset
        setup_events_path = run_dir / "_setup" / "events.jsonl"
        setup_events: list[dict[str, Any]] = []
        if setup_events_path.exists():
            for line in setup_events_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        setup_events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        if setup_events:
            for r in self._records:
                if not (r.artifact and r.artifact.test_dir.exists()):
                    continue
                # Read this test's video_event_offset from timeline.json
                tl_path = r.artifact.timeline_path
                offset = 0.0
                if tl_path.exists():
                    try:
                        tl = json.loads(tl_path.read_text(encoding="utf-8"))
                        offset = tl.get("video_event_offset", 0.0)
                    except (json.JSONDecodeError, OSError):
                        pass

                # Adjust setup event timestamps so replay viewer maps them correctly
                # replay videoTime = event.t + offset → for setup at video t=0..N,
                # we need event.t = video_t - offset
                adjusted_lines = []
                for ev in setup_events:
                    adj = dict(ev)
                    adj["t"] = round(ev["t"] - offset, 3)
                    adjusted_lines.append(json.dumps(adj, ensure_ascii=False))

                test_events_path = r.artifact.events_path
                existing = ""
                if test_events_path.exists():
                    existing = test_events_path.read_text(encoding="utf-8").strip()
                merged = "\n".join(adjusted_lines)
                if existing:
                    merged += "\n" + existing
                test_events_path.write_text(merged + "\n", encoding="utf-8")

        # Generate replay.html for each test
        reporter = Reporter(run_dir)
        for r in self._records:
            if r.artifact and r.artifact.test_dir.exists():
                entry = TestResultEntry(
                    test_id=r.test_id,
                    title=r.test_doc.get("title", ""),
                    bridge_result=r.bridge_result,
                    ai_result=r.ai_result,
                    final_verdict=r.final_verdict,
                    error=r.error,
                    duration_ms=int(r.duration_ms),
                    timed_out=r.timed_out,
                    artifact_dir=r.artifact.test_dir,
                )
                reporter._write_replay_html(entry, video_file="session.webm")

        return summary

    # ------------------------------------------------------------------
    # Single test execution
    # ------------------------------------------------------------------

    async def _run_one_test(
        self,
        record: TestRecord,
        page: Any,
        context: Any,
        bridge: BridgeHelper,
        network: NetworkHelper,
        gemini_image: GeminiImageHelper | None,
        run_dir: Path,
    ) -> None:
        """Execute a single test: setup → steps → teardown."""
        test_doc = record.test_doc
        test_id = record.test_id
        logger.info("\033[1m\033[33m▶ %s\033[0m — %s", test_id, test_doc.get("title", ""))

        artifact = ArtifactContext(
            run_dir=run_dir,
            test_id=test_id,
        )
        record.artifact = artifact

        # Video start time is passed from the suite runner (context creation time)
        # — set by caller before invoking this method.

        # Start bridge event collection
        try:
            await bridge.start_event_collection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not start event collection for %s: %s", test_id, exc)
        record.event_collection_start = time.time()

        test_start = time.time()
        executor = StepExecutor(
            bridge=bridge,
            network=network,
            artifact=artifact,
            gemini_image=gemini_image,
            page=page,
            test_start_time=test_start,
            game_url=self._game_url,
        )

        bridge_passed = True
        ai_passed = True
        ai_pending = False
        first_error: str | None = None

        # Test-level setup
        test_setup = test_doc.get("setup", [])
        if test_setup:
            ok, err, _ = await _run_steps(executor, test_setup, f"{test_id}/setup")
            if not ok and first_error is None:
                first_error = err

        # Re-start event collection after setup (setup navigates and reloads
        # the page, which destroys any prior event collection registration).
        try:
            await bridge.start_event_collection()
        except Exception:  # noqa: BLE001
            pass
        record.event_collection_start = time.time()

        # Main steps
        steps = test_doc.get("steps", [])
        all_ok, err, pending_steps = await _run_steps(executor, steps, test_id)
        if not all_ok and first_error is None:
            first_error = err
        if pending_steps:
            record.ai_pending_steps = pending_steps
            ai_pending = True

        # Determine bridge and AI results from step outcomes
        for step_result in executor.timeline:
            pass  # timeline already recorded inside executor

        # Check bridge assertion results from executor timeline
        # Re-evaluate: did any step fail bridge assertions?
        # We rely on all_ok: if any step returned success=False the test fails bridge
        bridge_passed = all_ok

        # Test-level teardown (always runs)
        test_teardown = test_doc.get("teardown", [])
        if test_teardown:
            await _run_steps(executor, test_teardown, f"{test_id}/teardown")

        # Drain final events
        try:
            events = await bridge.drain_events()
            if events:
                artifact.append_events(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Final event drain failed for %s: %s", test_id, exc)

        # Stop event collection
        try:
            await bridge.stop_event_collection()
        except Exception:  # noqa: BLE001
            pass

        # Write timeline.json
        artifact.write_timeline(
            video_start_time=record.video_start_time,
            event_collection_start_time=record.event_collection_start,
            steps=executor.timeline,
        )
        record.timeline_steps = executor.timeline

        # Compute results
        record.bridge_result = "pass" if bridge_passed else "fail"
        record.ai_result = "pending" if ai_pending else ("pass" if ai_passed else "fail")
        record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)
        record.error = first_error
        record.duration_ms = int((time.time() - test_start) * 1000)

    # ------------------------------------------------------------------
    # Deferred image AI processing
    # ------------------------------------------------------------------

    async def _process_pending_image_ai(
        self, gemini_image: GeminiImageHelper
    ) -> None:
        """
        Process all deferred image AI assertions after tests complete.

        For each test record with pending AI steps, run the Gemini image
        analysis, write step-N-ai.json, and update the record's ai_result
        and final_verdict.
        """
        import asyncio

        tasks: list[tuple[TestRecord, dict[str, Any]]] = []
        for record in self._records:
            if record.ai_result != "pending" or not record.ai_pending_steps:
                continue
            for step in record.ai_pending_steps:
                ai_block = step.get("assert", {}).get("ai", {})
                if ai_block.get("mode") == "image":
                    tasks.append((record, step))

        if not tasks:
            return

        logger.info("\033[1m\033[35m▶ AI Processing\033[0m %d deferred image assertion(s)", len(tasks))

        for record, step in tasks:
            artifact = record.artifact
            if not artifact:
                continue

            ai_block = step["assert"]["ai"]
            ref_screenshot = ai_block.get("ref_screenshot", "")
            prompt = ai_block.get("prompt", "")
            output_schema = ai_block.get("output_schema", {})
            expected = ai_block.get("expected", {})

            # Normalize ref_screenshot to a list
            ref_ids = ref_screenshot if isinstance(ref_screenshot, list) else [ref_screenshot]

            # Resolve each ref to a screenshot path
            screenshot_paths: list[Path] = []
            missing: list[str] = []
            for rid in ref_ids:
                found = None
                capture_index = 0
                for tl_entry in record.timeline_steps:
                    if tl_entry.get("action") == "capture":
                        capture_index += 1
                        if tl_entry.get("id") == rid:
                            found = artifact.screenshot_path(capture_index)
                            break
                # Fallback: try direct number extraction
                if (found is None or not found.exists()) and isinstance(rid, str) and rid.startswith("step-"):
                    try:
                        ref_num = int(rid.split("-")[1])
                        found = artifact.screenshot_path(ref_num)
                    except (ValueError, IndexError):
                        pass
                if found and found.exists():
                    screenshot_paths.append(found)
                else:
                    missing.append(str(rid))

            if missing:
                record.ai_result = "fail"
                record.error = record.error or f"AI: screenshot(s) not found: {', '.join(missing)}"
                record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)
                continue

            # Run Gemini API — pass first image (or multiple for comparison)
            screenshot_path = screenshot_paths[0]
            gemini_result = await asyncio.to_thread(
                gemini_image.analyse_image, screenshot_path, prompt, output_schema,
                extra_images=screenshot_paths[1:] if len(screenshot_paths) > 1 else None,
            )

            if not gemini_result.success:
                ai_data = {
                    "passed": False,
                    "reason": gemini_result.reason,
                    "raw_text": gemini_result.raw_text,
                }
                record.ai_result = "fail"
                record.error = record.error or f"AI image assert failed: {gemini_result.reason}"
            else:
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
                record.ai_result = "pass" if passed else "fail"
                if not passed:
                    record.error = record.error or f"AI image assert failed: {'; '.join(mismatches)}"

            # Write step-N-ai.json
            step_id = step.get("id", "")
            step_num = None
            if step_id.startswith("step-"):
                try:
                    step_num = int(step_id.split("-")[1])
                except (ValueError, IndexError):
                    pass
            if step_num is not None:
                ai_path = artifact.ai_result_path(step_num)
                ai_path.write_text(
                    json.dumps(ai_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Update verdict
            record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)

            # Rewrite result.json with updated ai_result
            if artifact:
                artifact.write_result(
                    bridge_result=record.bridge_result,
                    ai_result=record.ai_result,
                    final_verdict=record.final_verdict,
                    extra={
                        "error": record.error,
                        "duration_ms": record.duration_ms,
                        "timed_out": record.timed_out,
                    },
                )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _build_summary(
        self,
        suite_start: float,
        stop_reason: str | None,
        gemini_image: GeminiImageHelper | None,
        gemini_video: Any | None,
    ) -> dict[str, Any]:
        """Assemble summary.json content."""
        verdicts = [r.final_verdict for r in self._records]
        counts = {
            "green": verdicts.count("green"),
            "orange": verdicts.count("orange"),
            "red": verdicts.count("red"),
            "pending": verdicts.count("pending"),
            "skip": verdicts.count("skip"),
        }

        tests_summary = []
        for r in self._records:
            tests_summary.append(
                {
                    "test_id": r.test_id,
                    "title": r.test_doc.get("title", ""),
                    "bridge_result": r.bridge_result,
                    "ai_result": r.ai_result,
                    "final_verdict": r.final_verdict,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                    "timed_out": r.timed_out,
                    "artifact_dir": str(r.artifact.test_dir) if r.artifact else None,
                }
            )

        # Cost breakdown — accumulate from both image and video helpers
        img_cost = gemini_image.cost_summary() if gemini_image else {}
        vid_cost = gemini_video.cost_summary() if gemini_video else {}

        costs: dict[str, Any] = {
            "gemini_image_calls": img_cost.get("gemini_image_calls", 0),
            "gemini_video_calls": vid_cost.get("gemini_video_calls", 0),
            "gemini_input_tokens": (
                img_cost.get("gemini_input_tokens", 0)
                + vid_cost.get("gemini_input_tokens", 0)
            ),
            "gemini_output_tokens": (
                img_cost.get("gemini_output_tokens", 0)
                + vid_cost.get("gemini_output_tokens", 0)
            ),
            "estimated_cost_usd": round(
                img_cost.get("estimated_cost_usd", 0.0)
                + vid_cost.get("estimated_cost_usd", 0.0),
                6,
            ),
        }

        return {
            "suite": self._suite_doc.get("suite", self.suite_name),
            "game": self.game,
            "env": self.env,
            "run_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": int((time.time() - suite_start) * 1000),
            "total": len(self._records),
            "counts": counts,
            "stop_reason": stop_reason,
            "tests": tests_summary,
            "costs": costs,
        }

    # ------------------------------------------------------------------
    # File loading helpers
    # ------------------------------------------------------------------

    def _find_suite_file(self) -> Path:
        """Locate the suite YAML file under games/{game}/test-suites/."""
        suite_dir = self.base_dir / "games" / self.game / "test-suites"
        # Accept with or without .yml extension
        candidates = [
            suite_dir / f"{self.suite_name}.yml",
            suite_dir / f"{self.suite_name}.yaml",
            suite_dir / self.suite_name,
        ]
        for p in candidates:
            if p.exists():
                return p
        raise FileNotFoundError(
            f"Suite file not found for '{self.suite_name}' under {suite_dir}"
        )

    def _load_suite(self, suite_path: Path) -> dict[str, Any]:
        """Load and validate a suite YAML file."""
        with suite_path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)

        errors = validate_suite(doc, source=suite_path.name)
        if errors:
            msg = f"Suite validation failed ({len(errors)} error(s)):\n" + "\n".join(
                f"  {e}" for e in errors
            )
            raise ValueError(msg)

        return doc

    @staticmethod
    def _make_run_dir(base_dir: Path, game: str, env: str, suite_name: str) -> Path:
        """Create and return the suite run directory with a datetime stamp."""
        stamp = datetime.now().strftime("%d-%m-%Y-%H%M")
        return (
            base_dir
            / "test-results"
            / game
            / env
            / f"{suite_name}-{stamp}"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _html_esc(text: str | None) -> str:
    """Escape HTML special characters."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _write_summary_html(summary: dict[str, Any], out_path: Path) -> None:
    """Generate a human-readable summary.html from the summary dict."""
    suite = _html_esc(str(summary.get("suite", "")))
    game = _html_esc(str(summary.get("game", "")))
    env = _html_esc(str(summary.get("env", "")))
    run_date = _html_esc(str(summary.get("run_date", "")))
    duration_ms = summary.get("duration_ms", 0)
    duration_s = duration_ms / 1000.0
    stop_reason = summary.get("stop_reason")
    counts = summary.get("counts", {})
    tests = summary.get("tests", [])
    costs = summary.get("costs", {})

    dot_colors = {
        "green": "#10b981",
        "orange": "#f97316",
        "red": "#ef4444",
        "pending": "#6b7280",
        "skip": "#9ca3af",
    }
    verdict_labels = {
        "green": "Pass",
        "orange": "Suspicious",
        "red": "Fail",
        "pending": "Pending",
        "skip": "Skip",
    }

    # Summary dots
    dots_html = ""
    for v in ("green", "orange", "red", "pending", "skip"):
        c = counts.get(v, 0)
        if c == 0:
            continue
        color = dot_colors[v]
        label = verdict_labels[v]
        dots_html += (
            f'<div class="count-item">'
            f'<span class="dot" style="background:{color};"></span>'
            f'<span class="count-label">{c} {label}</span>'
            f'</div>'
        )

    # Test rows
    rows_html = ""
    for t in tests:
        verdict = t.get("final_verdict", "skip")
        color = dot_colors.get(verdict, "#9ca3af")
        label = verdict_labels.get(verdict, verdict.title())
        tid = _html_esc(str(t.get("test_id", "")))
        title = _html_esc(str(t.get("title", "")))
        d_ms = t.get("duration_ms", 0)
        d_s = d_ms / 1000.0
        error = t.get("error")
        timed_out = t.get("timed_out", False)
        bridge = _html_esc(str(t.get("bridge_result", "")))
        ai = _html_esc(str(t.get("ai_result", "")))

        timeout_badge = ""
        if timed_out:
            timeout_badge = (
                ' <span style="background:#fef3c7;color:#92400e;padding:1px 6px;'
                'border-radius:999px;font-size:0.7rem;font-weight:600;">TIMEOUT</span>'
            )

        error_html = ""
        if error:
            error_html = (
                f'<div style="color:#991b1b;font-family:monospace;font-size:0.8rem;'
                f'margin-top:4px;">{_html_esc(str(error))}</div>'
            )

        rows_html += f"""<tr>
  <td><span class="dot-sm" style="background:{color};" title="{_html_esc(label)}"></span></td>
  <td>{tid}{timeout_badge}</td>
  <td>{title}</td>
  <td>{bridge}</td>
  <td>{ai}</td>
  <td>{d_s:.1f}s</td>
</tr>
"""
        if error_html:
            rows_html += f'<tr><td></td><td colspan="5">{error_html}</td></tr>\n'

    # Cost section
    cost_html = ""
    total_calls = costs.get("gemini_image_calls", 0) + costs.get("gemini_video_calls", 0)
    if total_calls > 0:
        cost_usd = costs.get("estimated_cost_usd", 0.0)
        cost_html = (
            f'<div class="cost">'
            f'{total_calls} Gemini calls '
            f'({costs.get("gemini_image_calls", 0)} image, '
            f'{costs.get("gemini_video_calls", 0)} video) '
            f'&mdash; ~${cost_usd:.6f}'
            f'</div>'
        )

    stop_html = ""
    if stop_reason:
        stop_html = (
            f'<div style="background:#fef3c7;border:1px solid #fcd34d;'
            f'border-radius:6px;padding:10px;margin-bottom:16px;'
            f'font-size:0.85rem;color:#92400e;">'
            f'Stopped early: {_html_esc(str(stop_reason))}</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Summary — {suite}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 24px; color: #1f2937; background: #f9fafb; }}
  .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); max-width: 800px; margin: 0 auto; overflow: hidden; }}
  .header {{ padding: 20px; border-bottom: 1px solid #e5e7eb; }}
  .header h1 {{ font-size: 1.2rem; margin: 0 0 4px; }}
  .header .meta {{ font-size: 0.85rem; color: #6b7280; }}
  .counts {{ display: flex; gap: 16px; padding: 16px 20px; flex-wrap: wrap; border-bottom: 1px solid #f3f4f6; }}
  .count-item {{ display: flex; align-items: center; gap: 6px; }}
  .dot {{ width: 14px; height: 14px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
  .dot-sm {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
  .count-label {{ font-size: 0.875rem; font-weight: 600; }}
  .body {{ padding: 0 20px 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 12px; }}
  th {{ text-align: left; padding: 8px 10px; color: #6b7280; font-weight: 500; border-bottom: 2px solid #e5e7eb; font-size: 0.8rem; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
  .cost {{ font-size: 0.8rem; color: #6b7280; padding: 12px 20px; border-top: 1px solid #f3f4f6; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>{suite}</h1>
    <div class="meta">{game} &mdash; {env} &mdash; {run_date} &mdash; {duration_s:.1f}s &mdash; {len(tests)} tests</div>
  </div>
  <div class="counts">{dots_html}</div>
  <div class="body">
    {stop_html}
    <table>
      <thead>
        <tr><th></th><th>ID</th><th>Title</th><th>Bridge</th><th>AI</th><th>Duration</th></tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
  {cost_html}
</div>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    logger.info("Summary HTML written: %s", out_path)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a QA test suite against a game environment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python framework/runner/suite_runner.py --game zeus --suite suite-01-core --env dev\n"
            "  python framework/runner/suite_runner.py --game zeus --suite suite-06-freespin --env staging"
        ),
    )
    parser.add_argument("--game", required=True, metavar="GAME", help="Game identifier")
    parser.add_argument("--suite", required=True, metavar="SUITE", help="Suite file name (without .yml)")
    parser.add_argument("--env", required=True, metavar="ENV", help="Environment (dev|staging|prod)")
    parser.add_argument(
        "--base-dir",
        metavar="DIR",
        default=None,
        help="qa-platform repository root (default: auto-detected)",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    base_dir = Path(args.base_dir).resolve() if args.base_dir else None
    runner = SuiteRunner(
        game=args.game,
        suite_name=args.suite,
        env=args.env,
        base_dir=base_dir,
    )

    try:
        summary = await runner.run()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    # Write compact results table to stdout (CLI user-facing output)
    def _out(line: str = "") -> None:
        sys.stdout.write(line + "\n")

    counts = summary.get("counts", {})
    costs = summary.get("costs", {})
    _out(f"\n{summary['suite']} — {summary['game']} — {summary['env']} — {summary['run_date']}")
    _out("-" * 60)
    for test in summary.get("tests", []):
        verdict = test["final_verdict"].upper()
        extra = ""
        if verdict != "GREEN":
            extra += f"  — bridge={test['bridge_result']} ai={test['ai_result']}"
        if test["error"]:
            extra += f"  ERROR: {test['error']}"
        _out(f"[{verdict:7s}] {test['test_id']:10s} {test['title']}{extra}")
    _out("-" * 60)
    _out(
        f"{counts.get('green', 0)} green  "
        f"{counts.get('orange', 0)} orange  "
        f"{counts.get('red', 0)} red  "
        f"{counts.get('pending', 0)} pending"
    )
    total_calls = costs.get("gemini_image_calls", 0) + costs.get("gemini_video_calls", 0)
    cost_usd = costs.get("estimated_cost_usd", 0.0)
    if total_calls > 0:
        _out(f"Cost: {total_calls} Gemini calls, ~${cost_usd:.6f}")

    if summary.get("stop_reason"):
        _out(f"Stopped early: {summary['stop_reason']}")

    return 0 if counts.get("red", 0) == 0 else 1


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
