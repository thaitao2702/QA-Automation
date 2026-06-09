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

from framework.env_files import env_file_suffix, load_env_files, load_game_config
from framework.helpers.artifact import ArtifactContext, _safe_id
from framework.helpers.bo import BoHelper
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


# Verdict severity ranking for rolling up a run's many cases into one summary.
_VERDICT_RANK = {"green": 0, "skip": 1, "pending": 2, "orange": 3, "red": 4}


class CaseVerdict:
    """One test case's verdict within a run (one execution, many cases)."""

    def __init__(self, case_id: str, title: str) -> None:
        self.case_id = case_id
        self.title = title
        self.bridge_result: str = "skip"
        self.ai_result: str = "skip"
        self.final_verdict: str = "pending"
        self.error: str | None = None
        self.step_ids: list[str] = []
        self.ai_pending_steps: list[dict[str, Any]] = []  # AI steps serving this case
        # Aggregation accumulators for non-AI (bridge) asserts.
        self._bridge_seen: bool = False
        self._bridge_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "title": self.title,
            "bridge_result": self.bridge_result,
            "ai_result": self.ai_result,
            "final_verdict": self.final_verdict,
            "error": self.error,
            "step_ids": list(self.step_ids),
        }


class RunRecord:
    """
    Mutable state for one RUN — a single execution producing verdicts for many
    test cases. Shares ONE artifact/timeline/video. Exposes ``test_id``/``test_doc``
    and rolled-up verdict properties so the shared post-execution loops (video
    trim, setup-event prepend, replay, stop conditions) treat it like a TestRecord.
    """

    def __init__(self, run_doc: dict[str, Any], run_idx: int) -> None:
        self.run_doc = run_doc
        self.run_idx = run_idx
        self.run_id: str = run_doc.get("id", f"run-{run_idx}")
        self.cases: dict[str, CaseVerdict] = {}
        for c in run_doc.get("cases", []):
            cid = c.get("id")
            if isinstance(cid, str):
                self.cases[cid] = CaseVerdict(cid, c.get("title", ""))
        self.error: str | None = None
        self.duration_ms: int = 0
        self.timed_out: bool = False
        self.artifact: ArtifactContext | None = None
        self.video_start_time: float = 0.0
        self.event_collection_start: float = 0.0
        self.timeline_steps: list[dict[str, Any]] = []
        self.untagged_failure: bool = False
        self.untagged_first_error: str | None = None

    # -- TestRecord-compatible accessors (shared post-execution loops) ------

    @property
    def test_id(self) -> str:
        return self.run_id

    @property
    def test_doc(self) -> dict[str, Any]:
        return self.run_doc

    @property
    def ai_pending_steps(self) -> list[dict[str, Any]]:
        """Distinct AI steps across all cases (decides whether to defer to video AI)."""
        steps: list[dict[str, Any]] = []
        seen: set[int] = set()
        for cv in self.cases.values():
            for s in cv.ai_pending_steps:
                if id(s) not in seen:
                    seen.add(id(s))
                    steps.append(s)
        return steps

    @property
    def final_verdict(self) -> str:
        return self.worst_verdict

    @property
    def worst_verdict(self) -> str:
        if not self.cases:
            return "skip"
        worst = "green"
        for cv in self.cases.values():
            if _VERDICT_RANK.get(cv.final_verdict, 0) > _VERDICT_RANK.get(worst, 0):
                worst = cv.final_verdict
        return worst

    @property
    def bridge_result(self) -> str:
        results = {cv.bridge_result for cv in self.cases.values()}
        if "fail" in results:
            return "fail"
        if "pass" in results:
            return "pass"
        return "skip"

    @property
    def ai_result(self) -> str:
        results = {cv.ai_result for cv in self.cases.values()}
        if "pending" in results:
            return "pending"
        if "fail" in results:
            return "fail"
        if "pass" in results:
            return "pass"
        return "skip"

    def cases_as_dicts(self) -> list[dict[str, Any]]:
        return [cv.to_dict() for cv in self.cases.values()]

    def mark_all_cases_failed(self, error: str | None) -> None:
        for cv in self.cases.values():
            cv.bridge_result = "fail"
            cv.ai_result = "skip"
            cv.final_verdict = "red"
            if cv.error is None:
                cv.error = error


def _step_case_ids(step: dict[str, Any]) -> list[str]:
    """Case ids a step asserts — from its ``case`` tag and any ``expected_by_case``."""
    ids: list[str] = []
    tag = step.get("case")
    if isinstance(tag, str):
        ids.append(tag)
    elif isinstance(tag, list):
        ids.extend(c for c in tag if isinstance(c, str))
    assert_block = step.get("assert")
    if isinstance(assert_block, dict):
        ai = assert_block.get("ai")
        if isinstance(ai, dict) and isinstance(ai.get("expected_by_case"), dict):
            for cid in ai["expected_by_case"]:
                if isinstance(cid, str) and cid not in ids:
                    ids.append(cid)
    return ids


def _attribute_step_to_cases(record: "RunRecord", step: dict[str, Any], result: Any) -> None:
    """Fold one executed step's outcome into the case(s) it serves (run fan-out)."""
    case_ids = _step_case_ids(step)
    if not case_ids:
        # Plain/untagged step — its failure breaks the data the cases depend on.
        if not result.success:
            record.untagged_failure = True
            if record.untagged_first_error is None:
                record.untagged_first_error = result.error
        return

    for cid in case_ids:
        cv = record.cases.get(cid)
        if cv is None:
            continue
        cv.step_ids.append(step.get("id", ""))
        if result.ai_pending:
            cv.ai_pending_steps.append(step)
            cv.ai_result = "pending"
        else:
            cv._bridge_seen = True
            cv._bridge_ok = cv._bridge_ok and result.success
            if not result.success and cv.error is None:
                cv.error = result.error


def _finalize_case_verdicts(record: "RunRecord", run_fatal_error: str | None) -> None:
    """Compute each case's bridge/ai/final verdict after the run's steps complete."""
    if run_fatal_error:
        record.mark_all_cases_failed(run_fatal_error)
        return
    for cv in record.cases.values():
        if not cv._bridge_seen:
            cv.bridge_result = "skip"
        else:
            cv.bridge_result = "pass" if cv._bridge_ok else "fail"
        cv.ai_result = "pending" if cv.ai_pending_steps else "skip"
        cv.final_verdict = _compute_verdict(cv.bridge_result, cv.ai_result)


def _match_expected(
    data: dict[str, Any], expected: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Compare an AI output dict against an expected subset → (passed, mismatches)."""
    mismatches: list[str] = []
    for key, exp_val in (expected or {}).items():
        if data.get(key) != exp_val:
            mismatches.append(f"{key}: got {data.get(key)!r}, expected {exp_val!r}")
    return (len(mismatches) == 0, mismatches)


def _step_case_bindings(ai_block: dict[str, Any], step: dict[str, Any]) -> dict[str, dict]:
    """Map each case an AI step serves to the expected subset it is judged against.

    ``expected_by_case`` when present; otherwise the single ``expected`` applied to
    every id in the step's ``case`` tag.
    """
    ebc = ai_block.get("expected_by_case")
    if isinstance(ebc, dict):
        return {cid: (sub if isinstance(sub, dict) else {}) for cid, sub in ebc.items()}
    expected = ai_block.get("expected", {})
    tag = step.get("case")
    if isinstance(tag, str):
        cids = [tag]
    elif isinstance(tag, list):
        cids = [c for c in tag if isinstance(c, str)]
    else:
        cids = []
    return {cid: expected for cid in cids}


def _step_number(step_id: str) -> int | None:
    """Extract the integer from a step id like 'step-02' -> 2."""
    if not step_id or not step_id.startswith("step-"):
        return None
    try:
        return int(step_id.split("-")[1])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _url_script_env(env_entry: dict) -> dict:
    """Build the environment for a ``url_script`` subprocess.

    Starts from the current environment (which already holds root-config values
    loaded from the .env overlay — PLAYSITE_API_URL / PLAYSITE_USER / ...) and
    overlays the game-specific ``playsite:`` block from the game config so the
    resolver script receives the game's code/currency without them being baked
    into the url_script command line.
    """
    sub_env = os.environ.copy()
    playsite = env_entry.get("playsite") or {}
    mapping = {
        "game_code": "PLAYSITE_GAME_CODE",
        "currency": "PLAYSITE_CURRENCY",
        "language": "PLAYSITE_LANGUAGE",
    }
    for key, var in mapping.items():
        value = playsite.get(key)
        if value not in (None, ""):
            sub_env[var] = str(value)
    return sub_env


def _resolve_game_url(game: str, env: str, base_dir: Path) -> str:
    """
    Load the per-environment game config and resolve the game URL.

    Game config is split per environment (env.config.{suffix}.yml); the loaded
    file IS the environment's config (no ``environments:`` wrapper). Mirrors the
    logic in scripts/get_url.py but imported directly to avoid the subprocess
    overhead when running inline.
    """
    env_entry = load_game_config(game, env, base_dir)
    browser_params = env_entry.get("browser_params", "")

    # Static URL — preferred when a non-empty 'url' is provided. An empty or
    # absent 'url' falls through to 'url_script' below, so a config can keep
    # both keys: paste a fresh URL into 'url' for a quick manual run, or clear
    # it to let 'url_script' resolve the URL dynamically.
    static_url = str(env_entry.get("url") or "").strip()
    if static_url:
        base_url = static_url
        if browser_params:
            sep = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{sep}{browser_params.lstrip('?&')}"
        return base_url

    # url_script — fallback that resolves the URL by running a shell command.
    if env_entry.get("url_script"):
        result = subprocess.run(
            env_entry["url_script"],
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(base_dir),
            env=_url_script_env(env_entry),
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
        f"Game config for '{game}'/'{env}' has neither 'url' nor 'url_script'"
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
    on_step: Any = None,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """
    Run a list of steps through executor.

    Returns (all_passed, first_error, pending_video_steps).

    If *on_step* is given it is called as ``on_step(step, result)`` after each
    step executes — used by runs to fan a step's outcome out to its test cases.
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

        if on_step is not None:
            on_step(step, result)

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
        test_filter: set[str] | None = None,
        group_filter: set[str] | None = None,
    ) -> None:
        self.game = game
        self.suite_name = suite_name
        self.env = env
        self.base_dir = base_dir or _REPO_ROOT
        # Load the per-environment .env overlay (.env + .env.{suffix}) before
        # reading any environment-derived config below.
        load_env_files(self.env, self.base_dir)
        self._api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY", "")

        # Optional run filters. When set, only matching tests/groups execute, but
        # the enclosing group's setup/teardown still runs around the selected tests.
        self._test_filter = test_filter
        self._group_filter = group_filter

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

        # Read AI model configuration from environment
        image_model = os.environ.get("GEMINI_IMAGE_MODEL")
        video_model = os.environ.get("GEMINI_VIDEO_MODEL")
        video_resolution = os.environ.get("GEMINI_VIDEO_RESOLUTION")

        # Select AI provider: "gemini" (default), "local" HTTP endpoint, or
        # "claude" (frame-sampling Claude Code CLI sub-agent). The local/claude
        # helpers are interface-compatible with the Gemini helpers, so the rest of
        # the runner (VideoAiProcessor, StepExecutor) is unchanged.
        provider = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
        local_endpoint = os.environ.get("LOCAL_AI_ENDPOINT", "").strip()

        # Build the symbol/UI reference pack ONCE from the game's assets.yml (or legacy symbols.yml). Its
        # labeled ground-truth images are prepended to every AI image/video request.
        from framework.helpers.symbol_reference import load_symbol_reference
        symbol_reference = load_symbol_reference(self.game, self.base_dir)

        gemini_video_helper: Any | None = None
        gemini_image: Any | None = None  # holds a local helper when provider=local
        if provider == "local" and local_endpoint:
            from framework.helpers.local_ai import LocalImageHelper, LocalVideoHelper
            gemini_video_helper = LocalVideoHelper(local_endpoint, reference=symbol_reference)
            gemini_image = LocalImageHelper(local_endpoint, reference=symbol_reference)
            logger.info("AI provider: local (%s)", local_endpoint)
        elif provider == "claude":
            from framework.helpers.claude_ai import ClaudeImageHelper, ClaudeVideoHelper
            gemini_video_helper = ClaudeVideoHelper(reference=symbol_reference)
            gemini_image = ClaudeImageHelper(reference=symbol_reference)
            logger.info(
                "AI provider: claude (cli, fps=%s)",
                os.environ.get("CLAUDE_AI_FPS", "2"),
            )
        elif self._api_key:
            from framework.helpers.gemini_video import GeminiVideoHelper
            gemini_video_helper = GeminiVideoHelper(
                self._api_key,
                model=video_model,
                resolution=video_resolution,
                reference=symbol_reference,
            )
            gemini_image = GeminiImageHelper(
                self._api_key, model=image_model, reference=symbol_reference
            )
            logger.info("AI provider: gemini (image=%s, video=%s)", image_model, video_model)
        else:
            logger.warning(
                "AI provider unavailable: provider=%s, no GEMINI_API_KEY and no "
                "LOCAL_AI_ENDPOINT — AI assertions will be skipped",
                provider,
            )

        # Back-office helper for bo_paytable_update steps. Construction is cheap and
        # never touches the network; credentials are validated lazily when a BO step
        # actually runs, so suites without BO steps need no BO config.
        bo = BoHelper(self.game, self.env, self.base_dir)

        # Build video AI processor (must happen before suite execution)
        video_processor = VideoAiProcessor(
            game=self.game,
            suite_doc=self._suite_doc,
            base_dir=self.base_dir,
            gemini_video_helper=gemini_video_helper,
            # Scope batch groups to the tests this run will actually execute, so a
            # --test/--group filter still dispatches video AI instead of hanging
            # in 'pending' waiting for unselected tests in the same ref_assets group.
            run_test_ids=self._compute_run_test_ids(),
        )

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

            # Resolve config / timeouts (shared by all groups)
            config = self._suite_doc.get("config", {})
            test_timeout_ms = int(config.get("test_timeout_ms", _DEFAULT_TEST_TIMEOUT_MS))
            stop_conditions = config.get("stop_conditions") or {}
            stop_reason: str | None = None

            # Nested lifecycle helper — runs a setup/teardown step sequence on the
            # shared page/context. Used for suite-level AND group-level setup/teardown.
            # Returns (ok, first_error); callers decide how to act on a failure.
            async def _lifecycle(
                steps: list[dict[str, Any]], label: str, header: str | None = None
            ) -> tuple[bool, str | None]:
                if not steps:
                    return True, None
                if header:
                    logger.info(header, len(steps))
                artifact = ArtifactContext(run_dir=run_dir, test_id=label)
                executor = StepExecutor(
                    bridge=bridge,
                    network=network,
                    artifact=artifact,
                    gemini_image=gemini_image,
                    page=page,
                    game_url=self._game_url,
                    bo=bo,
                )
                ok, err, _ = await _run_steps(executor, steps, label)
                return ok, err

            # Nested per-test runner — timeout handling + result writing.
            async def _execute_test(test_doc: dict[str, Any], idx: int) -> TestRecord:
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
                            bo=bo,
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
                if record.ai_pending_steps and record.artifact:
                    await video_processor.notify_test_complete(record)
                return record

            # Nested per-run runner — one execution, many case verdicts.
            async def _execute_run(run_doc: dict[str, Any], idx: int) -> RunRecord:
                record = RunRecord(run_doc, idx)
                record.video_start_time = video_start_time
                try:
                    await asyncio.wait_for(
                        self._run_one_run(
                            record=record,
                            page=page,
                            context=context,
                            bridge=bridge,
                            network=network,
                            gemini_image=gemini_image,
                            run_dir=run_dir,
                            bo=bo,
                        ),
                        timeout=test_timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    record.timed_out = True
                    record.error = f"Run timed out after {test_timeout_ms}ms"
                    record.mark_all_cases_failed(record.error)
                    logger.warning("Run %s timed out", record.run_id)
                    try:
                        if record.artifact:
                            events = await bridge.drain_events()
                            if events:
                                record.artifact.append_events(events)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    record.error = str(exc)
                    record.mark_all_cases_failed(record.error)
                    logger.error("Run %s failed with exception: %s", record.run_id, exc)

                if record.artifact:
                    record.artifact.write_run_result(
                        cases=record.cases_as_dicts(),
                        extra={
                            "run_id": record.run_id,
                            "title": run_doc.get("title", ""),
                            "duration_ms": record.duration_ms,
                            "timed_out": record.timed_out,
                            "error": record.error,
                        },
                    )
                self._records.append(record)
                # Defer AI assertions for runs (resolved by the video AI processor).
                # Skip when the run failed fatally (record.error set): the execution
                # never produced valid footage, so analysing the clip would waste an
                # AI call (and could hang on a garbage clip) — cases are already red.
                notify_run = getattr(video_processor, "notify_run_complete", None)
                if notify_run and record.error is None and record.ai_pending_steps and record.artifact:
                    await notify_run(record)
                return record

            # Run suite-level setup (once, before any group). A suite-setup failure
            # is fatal: the precondition for EVERY test/run is broken, so none are
            # executed — each is marked red with the setup error instead.
            suite_setup_ok, suite_setup_err = await _lifecycle(
                self._suite_doc.get("setup", []),
                "_setup",
                header="\033[1m\033[36m▶ Suite Setup\033[0m (%d steps)",
            )
            if not suite_setup_ok:
                stop_reason = f"suite setup failed: {suite_setup_err}"
                logger.error(
                    "\033[31mSuite setup failed — skipping all tests/runs: %s\033[0m",
                    suite_setup_err,
                )

            # Iterate groups. Top-level ``tests:`` form an implicit ungrouped group
            # (no group setup/teardown); explicit ``groups:`` follow in order. The
            # --test / --group filters select which tests run, but the enclosing
            # group's setup/teardown ALWAYS runs around any selected test — so a
            # single test can be run with its group's paytable setup intact.
            selected_total = 0
            global_idx = 0
            for group in self._build_groups():
                gid = group["id"]
                if self._group_filter is not None and gid not in self._group_filter:
                    continue
                selected = [t for t in group["tests"] if self._test_selected(t)]
                selected_runs = [rn for rn in group.get("runs", []) if self._run_selected(rn)]
                if not selected and not selected_runs:
                    continue
                selected_total += len(selected) + len(selected_runs)
                label = gid or "ungrouped"

                # Suite setup failed → mark this group's selected tests/runs red and
                # skip its setup/teardown entirely (nothing to set up against).
                if not suite_setup_ok:
                    for test_doc in selected:
                        self._mark_skipped(test_doc, global_idx, stop_reason, is_run=False)
                        global_idx += 1
                    for run_doc in selected_runs:
                        self._mark_skipped(run_doc, global_idx, stop_reason, is_run=True)
                        global_idx += 1
                    continue

                # Group setup (once, before the group's selected tests/runs).
                group_setup_ok, group_setup_err = await _lifecycle(
                    group["setup"],
                    f"_group_{label}_setup",
                    header=f"\033[1m\033[36m▶ Group Setup\033[0m [{label}] (%d steps)",
                )
                if not group_setup_ok:
                    # Group precondition broken → mark its tests/runs red, skip them,
                    # but still attempt group teardown (best-effort cleanup).
                    reason = f"group '{label}' setup failed: {group_setup_err}"
                    logger.error(
                        "\033[31mGroup '%s' setup failed — skipping its %d test(s)/run(s)\033[0m",
                        label, len(selected) + len(selected_runs),
                    )
                    for test_doc in selected:
                        self._mark_skipped(test_doc, global_idx, reason, is_run=False)
                        global_idx += 1
                    for run_doc in selected_runs:
                        self._mark_skipped(run_doc, global_idx, reason, is_run=True)
                        global_idx += 1
                    t_ok, t_err = await _lifecycle(
                        group["teardown"], f"_group_{label}_teardown"
                    )
                    if not t_ok:
                        logger.warning("Group '%s' teardown failed (cleanup): %s", label, t_err)
                    continue

                stop_reason = None
                for test_doc in selected:
                    record = await _execute_test(test_doc, global_idx)
                    global_idx += 1
                    stop_reason = _check_stop_conditions(
                        stop_conditions, self._records, record.final_verdict
                    )
                    if stop_reason:
                        logger.warning("Suite stopping: %s", stop_reason)
                        break

                # Runs (one execution, many case verdicts) after the group's tests
                if not stop_reason:
                    for run_doc in selected_runs:
                        record = await _execute_run(run_doc, global_idx)
                        global_idx += 1
                        stop_reason = _check_stop_conditions(
                            stop_conditions, self._records, record.final_verdict
                        )
                        if stop_reason:
                            logger.warning("Suite stopping: %s", stop_reason)
                            break

                # Group teardown (once — runs even when stopping, to clean up state
                # such as the group's paytable selection). Warn-only on failure.
                t_ok, t_err = await _lifecycle(
                    group["teardown"],
                    f"_group_{label}_teardown",
                    header=f"\033[1m\033[36m▶ Group Teardown\033[0m [{label}] (%d steps)",
                )
                if not t_ok:
                    logger.warning("Group '%s' teardown failed (cleanup): %s", label, t_err)

                if stop_reason:
                    break

            if self._test_filter is not None and selected_total == 0:
                logger.warning("No tests matched --test filter %s", sorted(self._test_filter))

            # Run suite teardown (once, after all groups). Warn-only on failure.
            suite_td_ok, suite_td_err = await _lifecycle(
                self._suite_doc.get("teardown", []),
                "_teardown",
                header="\033[1m\033[36m▶ Suite Teardown\033[0m (%d steps)",
            )
            if not suite_td_ok:
                logger.warning("Suite teardown failed (cleanup): %s", suite_td_err)

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
            await self._process_pending_run_image_ai(gemini_image)

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

                # A run has many cases sharing this replay — give each its own
                # report card (linked from the summary) that links back here.
                if isinstance(r, RunRecord):
                    for cv in r.cases.values():
                        r.artifact.write_case_report(cv.to_dict())

        return summary

    # ------------------------------------------------------------------
    # Group / filter helpers
    # ------------------------------------------------------------------

    def _build_groups(self) -> list[dict[str, Any]]:
        """
        Normalize the suite into an ordered list of group dicts
        ``{id, title, setup, teardown, tests}``.

        Top-level ``tests:`` become an implicit ungrouped group (id=None, no
        setup/teardown), listed first; explicit ``groups:`` follow in document
        order. Each group's setup/teardown runs once around its tests.
        """
        groups: list[dict[str, Any]] = []
        flat = self._suite_doc.get("tests") or []
        flat_runs = self._suite_doc.get("runs") or []
        if flat or flat_runs:
            groups.append(
                {"id": None, "title": "", "setup": [], "teardown": [],
                 "tests": flat, "runs": flat_runs}
            )
        for g in self._suite_doc.get("groups") or []:
            groups.append(
                {
                    "id": g.get("id"),
                    "title": g.get("title", ""),
                    "setup": g.get("setup") or [],
                    "teardown": g.get("teardown") or [],
                    "tests": g.get("tests") or [],
                    "runs": g.get("runs") or [],
                }
            )
        return groups

    def _test_selected(self, test_doc: dict[str, Any]) -> bool:
        """True if the test passes the --test id filter (None filter = all tests)."""
        if self._test_filter is None:
            return True
        return test_doc.get("id") in self._test_filter

    def _run_selected(self, run_doc: dict[str, Any]) -> bool:
        """True if a run is selected by --test (its run id OR any of its case ids).

        A run always executes as a whole — selecting a single case still runs the
        whole run (cases share one execution), but the case verdict is reported.
        """
        if self._test_filter is None:
            return True
        if run_doc.get("id") in self._test_filter:
            return True
        return any(c.get("id") in self._test_filter for c in run_doc.get("cases", []))

    def _mark_skipped(
        self, doc: dict[str, Any], idx: int, reason: str, is_run: bool
    ) -> "TestRecord | RunRecord":
        """Record a test/run as failed (red) WITHOUT executing it.

        Used when a suite/group setup failure means the dependent tests/runs cannot
        be trusted to run — they appear as failures in the summary (no artifact,
        since no steps ran), rather than being silently skipped.
        """
        if is_run:
            run_rec = RunRecord(doc, idx)
            run_rec.error = reason
            run_rec.mark_all_cases_failed(reason)
            self._records.append(run_rec)
            logger.error("\033[31m✗ %s skipped — %s\033[0m", run_rec.run_id, reason)
            return run_rec
        rec = TestRecord(doc, idx)
        rec.bridge_result = "fail"
        rec.ai_result = "skip"
        rec.final_verdict = "red"
        rec.error = reason
        self._records.append(rec)
        logger.error("\033[31m✗ %s skipped — %s\033[0m", rec.test_id, reason)
        return rec

    def _compute_run_test_ids(self) -> set[str]:
        """Ids of tests that will actually execute under the current --test/--group filters.

        Mirrors the execution loop's selection (``_build_groups`` + ``_group_filter``
        + ``_test_selected``) so the VideoAiProcessor only waits on tests that will
        run — otherwise a filtered run leaves video AI 'pending' indefinitely.
        """
        ids: set[str] = set()
        for group in self._build_groups():
            if self._group_filter is not None and group["id"] not in self._group_filter:
                continue
            for test_doc in group["tests"]:
                if self._test_selected(test_doc):
                    tid = test_doc.get("id")
                    if isinstance(tid, str):
                        ids.add(tid)
        return ids

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
        bo: Any = None,
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
            bo=bo,
        )

        bridge_passed = True
        ai_passed = True
        ai_pending = False
        first_error: str | None = None
        setup_failed = False

        # Test-level setup — a setup failure is fatal: skip the main steps and fail.
        test_setup = test_doc.get("setup", [])
        if test_setup:
            ok, err, _ = await _run_steps(executor, test_setup, f"{test_id}/setup")
            if not ok:
                setup_failed = True
                first_error = err
                logger.error(
                    "\033[31m[%s] setup failed — skipping steps: %s\033[0m", test_id, err
                )

        if not setup_failed:
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

            # We rely on all_ok: if any step returned success=False the test fails bridge
            bridge_passed = all_ok
        else:
            bridge_passed = False

        # Test-level teardown (always runs — warn-only; never flips the verdict)
        test_teardown = test_doc.get("teardown", [])
        if test_teardown:
            t_ok, t_err, _ = await _run_steps(executor, test_teardown, f"{test_id}/teardown")
            if not t_ok:
                logger.warning(
                    "[%s] teardown failed (cleanup, verdict unaffected): %s", test_id, t_err
                )

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

        # Compute results. A setup failure is a hard infra failure (like a timeout):
        # red directly, not via _compute_verdict (which would yield orange).
        if setup_failed:
            record.bridge_result = "fail"
            record.ai_result = "skip"
            record.final_verdict = "red"
        else:
            record.bridge_result = "pass" if bridge_passed else "fail"
            record.ai_result = "pending" if ai_pending else ("pass" if ai_passed else "fail")
            record.final_verdict = _compute_verdict(record.bridge_result, record.ai_result)
        record.error = first_error
        record.duration_ms = int((time.time() - test_start) * 1000)

    # ------------------------------------------------------------------
    # Single run execution (one execution, many case verdicts)
    # ------------------------------------------------------------------

    async def _run_one_run(
        self,
        *,
        record: "RunRecord",
        page: Any,
        context: Any,
        bridge: BridgeHelper,
        network: NetworkHelper,
        gemini_image: Any,
        run_dir: Path,
        bo: Any = None,
    ) -> None:
        """Execute a single run: setup → steps → teardown, fanning each tagged
        assert's outcome out to its case(s). Shares ONE artifact/timeline/video."""
        run_doc = record.run_doc
        run_id = record.run_id
        n_cases = len(record.cases)
        logger.info(
            "\033[1m\033[33m▶ %s\033[0m — %s  \033[2m(%d case%s)\033[0m",
            run_id, run_doc.get("title", ""), n_cases, "" if n_cases == 1 else "s",
        )

        artifact = ArtifactContext(run_dir=run_dir, test_id=run_id)
        record.artifact = artifact

        try:
            await bridge.start_event_collection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not start event collection for %s: %s", run_id, exc)
        record.event_collection_start = time.time()

        run_start = time.time()
        executor = StepExecutor(
            bridge=bridge,
            network=network,
            artifact=artifact,
            gemini_image=gemini_image,
            page=page,
            test_start_time=run_start,
            game_url=self._game_url,
            bo=bo,
        )

        # Run-level setup — a setup failure is fatal for the whole run: skip the
        # main steps entirely (running them would only cascade, e.g. '$events' not saved).
        setup_error: str | None = None
        run_setup = run_doc.get("setup", [])
        if run_setup:
            ok, err, _ = await _run_steps(executor, run_setup, f"{run_id}/setup")
            if not ok:
                setup_error = err
                logger.error(
                    "\033[31m[%s] run setup failed — skipping steps: %s\033[0m", run_id, err
                )

        if setup_error is None:
            # Re-start event collection after setup (setup may reload the page).
            try:
                await bridge.start_event_collection()
            except Exception:  # noqa: BLE001
                pass
            record.event_collection_start = time.time()

            # Main steps — fan each step's result out to its case(s).
            def _on_step(step: dict[str, Any], result: Any) -> None:
                _attribute_step_to_cases(record, step, result)

            steps = run_doc.get("steps", [])
            await _run_steps(executor, steps, run_id, on_step=_on_step)

        # Run-level teardown (always runs — warn-only; never flips a verdict)
        run_teardown = run_doc.get("teardown", [])
        if run_teardown:
            t_ok, t_err, _ = await _run_steps(executor, run_teardown, f"{run_id}/teardown")
            if not t_ok:
                logger.warning(
                    "[%s] teardown failed (cleanup, verdict unaffected): %s", run_id, t_err
                )

        # Drain final events
        try:
            events = await bridge.drain_events()
            if events:
                artifact.append_events(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Final event drain failed for %s: %s", run_id, exc)

        try:
            await bridge.stop_event_collection()
        except Exception:  # noqa: BLE001
            pass

        artifact.write_timeline(
            video_start_time=record.video_start_time,
            event_collection_start_time=record.event_collection_start,
            steps=executor.timeline,
        )
        record.timeline_steps = executor.timeline

        # A failed setup or a failed untagged/plain step means the run could not
        # produce the data the cases rely on → all cases red.
        run_fatal = setup_error or (
            record.untagged_first_error if record.untagged_failure else None
        )
        record.error = run_fatal
        _finalize_case_verdicts(record, run_fatal)
        record.duration_ms = int((time.time() - run_start) * 1000)

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
            if isinstance(record, RunRecord):
                continue  # runs handled by _process_pending_run_image_ai
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

            request_info = {
                "prompt": prompt,
                "output_schema": output_schema,
                "expected": expected,
                "screenshots": [str(p.name) for p in screenshot_paths],
            }

            if not gemini_result.success:
                ai_data = {
                    "passed": False,
                    "reason": gemini_result.reason,
                    "request": request_info,
                    "raw_response": gemini_result.raw_text,
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
                    "request": request_info,
                    "raw_response": gemini_result.raw_text,
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

    async def _process_pending_run_image_ai(
        self, gemini_image: GeminiImageHelper
    ) -> None:
        """Resolve deferred IMAGE AI asserts inside runs, attributing each to its case(s).

        One screenshot analysis (one Gemini call per assert step) is evaluated per
        case via ``expected_by_case`` (or the single ``expected`` for the step's
        ``case`` tag), so one call can settle several cases.
        """
        import asyncio

        for record in self._records:
            if not isinstance(record, RunRecord) or not record.artifact:
                continue
            image_steps = [
                s for s in record.run_doc.get("steps", [])
                if s.get("assert", {}).get("ai", {}).get("mode") == "image"
            ]
            if not image_steps:
                continue
            artifact = record.artifact
            changed = False

            for step in image_steps:
                ai_block = step["assert"]["ai"]
                prompt = ai_block.get("prompt", "")
                output_schema = ai_block.get("output_schema", {})
                bindings = _step_case_bindings(ai_block, step)
                if not bindings:
                    continue

                paths, missing = self._resolve_ref_screenshots(
                    record, artifact, ai_block.get("ref_screenshot", "")
                )
                if missing:
                    for cid in bindings:
                        self._set_run_case_ai(record, cid, "fail", f"screenshot(s) not found: {', '.join(missing)}")
                    changed = True
                    continue

                gemini_result = await asyncio.to_thread(
                    gemini_image.analyse_image, paths[0], prompt, output_schema,
                    extra_images=paths[1:] if len(paths) > 1 else None,
                )

                per_case: dict[str, Any] = {}
                if not gemini_result.success:
                    for cid in bindings:
                        self._set_run_case_ai(record, cid, "fail", gemini_result.reason)
                        per_case[cid] = {"passed": False, "reason": gemini_result.reason}
                else:
                    data = gemini_result.data or {}
                    for cid, subset in bindings.items():
                        passed, mismatches = _match_expected(data, subset)
                        self._set_run_case_ai(
                            record, cid, "pass" if passed else "fail",
                            "; ".join(mismatches) if mismatches else None,
                        )
                        per_case[cid] = {"passed": passed, "mismatches": mismatches}
                changed = True

                step_num = _step_number(step.get("id", ""))
                if step_num is not None:
                    artifact.ai_result_path(step_num).write_text(
                        json.dumps(
                            {
                                "data": gemini_result.data if gemini_result.success else None,
                                "expected_by_case": bindings,
                                "per_case": per_case,
                                "raw_response": gemini_result.raw_text,
                            },
                            indent=2, ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )

            if changed:
                for cv in record.cases.values():
                    cv.final_verdict = _compute_verdict(cv.bridge_result, cv.ai_result)
                artifact.write_run_result(
                    cases=record.cases_as_dicts(),
                    extra={
                        "run_id": record.run_id,
                        "title": record.run_doc.get("title", ""),
                        "duration_ms": record.duration_ms,
                        "timed_out": record.timed_out,
                        "error": record.error,
                    },
                )

    @staticmethod
    def _set_run_case_ai(record: "RunRecord", case_id: str, ai_result: str, error: str | None) -> None:
        cv = record.cases.get(case_id)
        if cv is None:
            return
        cv.ai_result = ai_result
        if ai_result == "fail" and error and not cv.error:
            cv.error = error

    def _resolve_ref_screenshots(
        self, record: Any, artifact: Any, ref_screenshot: Any
    ) -> tuple[list[Path], list[str]]:
        """Resolve a step's ref_screenshot id(s) to screenshot paths on disk."""
        ref_ids = ref_screenshot if isinstance(ref_screenshot, list) else [ref_screenshot]
        paths: list[Path] = []
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
            if (found is None or not found.exists()) and isinstance(rid, str) and rid.startswith("step-"):
                try:
                    found = artifact.screenshot_path(int(rid.split("-")[1]))
                except (ValueError, IndexError):
                    pass
            if found and found.exists():
                paths.append(found)
            else:
                missing.append(str(rid))
        return paths, missing

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
        """Assemble summary.json content.

        Each run is flattened into one entry per case (sharing the run's
        artifact_dir + a ``run_id`` field), so cases count as tests in the totals.
        """
        tests_summary: list[dict[str, Any]] = []
        for r in self._records:
            if isinstance(r, RunRecord):
                artifact_dir = str(r.artifact.test_dir) if r.artifact else None
                folder = Path(artifact_dir).name if artifact_dir else None
                for cv in r.cases.values():
                    tests_summary.append(
                        {
                            "test_id": cv.case_id,
                            "title": cv.title,
                            "bridge_result": cv.bridge_result,
                            "ai_result": cv.ai_result,
                            "final_verdict": cv.final_verdict,
                            "duration_ms": r.duration_ms,
                            "error": cv.error,
                            "timed_out": r.timed_out,
                            "artifact_dir": artifact_dir,
                            "run_id": r.run_id,
                            # Per-case report (a run case) + the shared replay viewer.
                            "report": (
                                f"{folder}/case-{_safe_id(cv.case_id)}.html"
                                if folder else None
                            ),
                            "replay": f"{folder}/replay.html" if folder else None,
                        }
                    )
            else:
                artifact_dir = str(r.artifact.test_dir) if r.artifact else None
                folder = Path(artifact_dir).name if artifact_dir else None
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
                        "artifact_dir": artifact_dir,
                        # Single test: result.html is its report; replay is alongside.
                        "report": f"{folder}/result.html" if folder else None,
                        "replay": f"{folder}/replay.html" if folder else None,
                    }
                )

        verdicts = [t["final_verdict"] for t in tests_summary]
        counts = {
            "green": verdicts.count("green"),
            "orange": verdicts.count("orange"),
            "red": verdicts.count("red"),
            "pending": verdicts.count("pending"),
            "skip": verdicts.count("skip"),
        }

        # Cost breakdown — per-model pricing
        # Image: Gemini 2.5 Flash ($0.30/1M in, $2.50/1M out)
        # Video: Gemini 3 Flash Preview ($0.50/1M in, $3.00/1M out)
        img_cost = gemini_image.cost_summary() if gemini_image else {}
        vid_cost = gemini_video.cost_summary() if gemini_video else {}

        costs: dict[str, Any] = {
            "image": {
                "model": img_cost.get("model", "gemini-2.5-flash"),
                "calls": img_cost.get("gemini_image_calls", 0),
                "input_tokens": img_cost.get("gemini_input_tokens", 0),
                "output_tokens": img_cost.get("gemini_output_tokens", 0),
                "cost_usd": round(img_cost.get("estimated_cost_usd", 0.0), 6),
            },
            "video": {
                "model": vid_cost.get("model", "gemini-3-flash-preview"),
                "resolution": vid_cost.get("resolution", "MEDIUM"),
                "calls": vid_cost.get("gemini_video_calls", 0),
                "input_tokens": vid_cost.get("gemini_input_tokens", 0),
                "output_tokens": vid_cost.get("gemini_output_tokens", 0),
                "cost_usd": round(vid_cost.get("estimated_cost_usd", 0.0), 6),
            },
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
            "total": len(tests_summary),
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

    def clear_bo_token_cache(self) -> None:
        """Delete this env's rotated BO token cache (``.bo_token.{suffix}.json``).

        Called after every suite run (pass or fail) so the next run re-authenticates
        from the base ``.env.{suffix}`` tokens rather than reusing a possibly-stale
        rotated token. Computes the path directly (no BoHelper needed), so it runs
        even when the suite failed before BO was used.
        """
        cache_path = self.base_dir / f".bo_token.{env_file_suffix(self.env)}.json"
        try:
            if cache_path.exists():
                cache_path.unlink()
                logger.info("Cleared BO token cache %s", cache_path.name)
        except OSError as exc:  # noqa: BLE001
            logger.warning("Could not clear BO token cache %s: %s", cache_path, exc)

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

        # Link the ID to this case/test's report file (summary -> report -> replay).
        report = t.get("report")
        tid_cell = f'<a href="{_html_esc(str(report))}">{tid}</a>' if report else tid

        error_html = ""
        if error:
            error_html = (
                f'<div style="color:#991b1b;font-family:monospace;font-size:0.8rem;'
                f'margin-top:4px;">{_html_esc(str(error))}</div>'
            )

        rows_html += f"""<tr>
  <td><span class="dot-sm" style="background:{color};" title="{_html_esc(label)}"></span></td>
  <td>{tid_cell}{timeout_badge}</td>
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
        img = costs.get("image", {})
        vid = costs.get("video", {})
        lines = [f'<div class="cost">']
        lines.append(
            f'<strong>AI Cost:</strong> ~${cost_usd:.6f} total '
            f'({total_calls} calls: {costs.get("gemini_image_calls", 0)} image, '
            f'{costs.get("gemini_video_calls", 0)} video)'
        )
        if img.get("calls", 0) > 0:
            lines.append(
                f'<br><span style="color:#6b7280;font-size:0.75rem;">'
                f'Image ({img.get("model", "gemini-2.5-flash")}): '
                f'{img.get("calls", 0)} calls, '
                f'{img.get("input_tokens", 0)} in / {img.get("output_tokens", 0)} out tokens, '
                f'${img.get("cost_usd", 0.0):.6f}'
                f'</span>'
            )
        if vid.get("calls", 0) > 0:
            lines.append(
                f'<br><span style="color:#6b7280;font-size:0.75rem;">'
                f'Video ({vid.get("model", "gemini-3-flash-preview")}): '
                f'{vid.get("calls", 0)} calls, '
                f'{vid.get("input_tokens", 0)} in / {vid.get("output_tokens", 0)} out tokens, '
                f'${vid.get("cost_usd", 0.0):.6f}'
                f'</span>'
            )
        lines.append('</div>')
        cost_html = "\n".join(lines)

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
            "  python framework/runner/suite_runner.py --game zeus --suite suite-06-freespin --env staging\n"
            "  # run a single test (its group setup/teardown still runs):\n"
            "  python framework/runner/suite_runner.py --game zeus --suite suite-06 --env dev --test ZZ-15\n"
            "  # run a whole group:\n"
            "  python framework/runner/suite_runner.py --game zeus --suite suite-06 --env dev --group freespin"
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
    parser.add_argument(
        "--test",
        metavar="ID",
        default=None,
        help=(
            "Run only these test id(s), comma-separated (e.g. ZZ-15 or ZZ-15,ZZ-16). "
            "The enclosing group's setup/teardown still runs. Default: run all tests."
        ),
    )
    parser.add_argument(
        "--group",
        metavar="ID",
        default=None,
        help="Run only these group id(s), comma-separated. Default: run all groups.",
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

    def _split_ids(raw: str | None) -> set[str] | None:
        if not raw:
            return None
        ids = {part.strip() for part in raw.split(",") if part.strip()}
        return ids or None

    runner = SuiteRunner(
        game=args.game,
        suite_name=args.suite,
        env=args.env,
        base_dir=base_dir,
        test_filter=_split_ids(args.test),
        group_filter=_split_ids(args.group),
    )

    try:
        summary = await runner.run()
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        # Always clear the rotated BO token cache after a run (pass or fail) so the
        # next run re-authenticates with fresh tokens.
        runner.clear_bo_token_cache()

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
        img = costs.get("image", {})
        vid = costs.get("video", {})
        if img.get("calls", 0) > 0:
            _out(
                f"  Image ({img.get('model', 'gemini-2.5-flash')}): "
                f"{img['calls']} calls, "
                f"{img.get('input_tokens', 0)} in / {img.get('output_tokens', 0)} out, "
                f"${img.get('cost_usd', 0.0):.6f}"
            )
        if vid.get("calls", 0) > 0:
            _out(
                f"  Video ({vid.get('model', 'gemini-3-flash-preview')}): "
                f"{vid['calls']} calls, "
                f"{vid.get('input_tokens', 0)} in / {vid.get('output_tokens', 0)} out, "
                f"${vid.get('cost_usd', 0.0):.6f}"
            )

    if summary.get("stop_reason"):
        _out(f"Stopped early: {summary['stop_reason']}")

    return 0 if counts.get("red", 0) == 0 else 1


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
