"""
Reporter — aggregates test results and generates HTML reports.

Reads result.json files from a suite run directory, computes triple-validation
verdicts, writes summary.json, generates report.html, and generates replay.html
per test.

Public interface
----------------
Reporter(results_dir, template_dir)
    Read results and generate reports.

    generate() -> ReportSummary
        Run the full report generation pipeline. Returns a summary dataclass.

Standalone CLI
--------------
python framework/runner/reporter.py --results-dir test-results/zeus/dev/2026-04-07-suite-01

Triple-validation verdict model
--------------------------------
    bridge=pass, ai=pass   -> green
    bridge=pass, ai=fail   -> orange  (human review needed)
    bridge=fail, ai=pass   -> orange
    bridge=fail, ai=fail   -> red
    ai=pending             -> pending (video AI not yet processed)
    bridge=skip, ai=skip   -> skip
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------

_VERDICT_ORDER = {"red": 0, "orange": 1, "pending": 2, "skip": 3, "green": 4}

_VERDICT_CSS: dict[str, str] = {
    "green": "#10b981",
    "orange": "#f97316",
    "red": "#ef4444",
    "pending": "#6b7280",
    "skip": "#9ca3af",
}

_VERDICT_BG: dict[str, str] = {
    "green": "#d1fae5",
    "orange": "#ffedd5",
    "red": "#fee2e2",
    "pending": "#f3f4f6",
    "skip": "#f9fafb",
}


def _compute_verdict(bridge_result: str, ai_result: str) -> str:
    """Compute final_verdict from bridge_result + ai_result."""
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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TestResultEntry:
    """Parsed data for a single test from result.json."""

    test_id: str
    title: str
    bridge_result: str
    ai_result: str
    final_verdict: str
    error: str | None
    duration_ms: int
    timed_out: bool
    artifact_dir: Path
    screenshots: list[Path] = field(default_factory=list)
    ai_result_files: list[Path] = field(default_factory=list)


@dataclass
class ReportSummary:
    """Output of Reporter.generate()."""

    suite_dir: Path
    total: int
    counts: dict[str, int]
    tests: list[TestResultEntry]
    report_path: Path
    summary_path: Path


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


class Reporter:
    """
    Reads test result artifacts and generates HTML + JSON reports.

    Parameters
    ----------
    results_dir:
        The suite run directory (e.g. test-results/zeus/dev/2026-04-07-suite-01).
        Contains per-test subdirectories and optionally an existing summary.json.
    template_dir:
        Directory containing replay.html Jinja template. Defaults to
        framework/templates/.
    """

    def __init__(
        self,
        results_dir: Path,
        template_dir: Path | None = None,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.template_dir = template_dir or (
            Path(__file__).resolve().parent.parent / "templates"
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def generate(self) -> ReportSummary:
        """
        Run the full report generation pipeline.

        1. Discover all per-test result.json files.
        2. Re-compute verdicts from bridge_result + ai_result.
        3. Write summary.json.
        4. Generate report.html (suite-level).
        5. Generate replay.html per test.
        """
        entries = self._collect_results()
        counts = self._count_verdicts(entries)
        summary_path = self._write_summary(entries, counts)
        report_path = self._write_report_html(entries, counts)
        for entry in entries:
            self._write_replay_html(entry)

        logger.info(
            "Report generated: %d tests — %s",
            len(entries),
            report_path,
        )
        return ReportSummary(
            suite_dir=self.results_dir,
            total=len(entries),
            counts=counts,
            tests=entries,
            report_path=report_path,
            summary_path=summary_path,
        )

    # ------------------------------------------------------------------
    # Result collection
    # ------------------------------------------------------------------

    def _collect_results(self) -> list[TestResultEntry]:
        """Scan results_dir for per-test result.json files.

        A run's ``result.json`` (``kind: "run"``) expands into one entry per case.
        """
        entries: list[TestResultEntry] = []
        for result_file in sorted(self.results_dir.rglob("result.json")):
            # Skip the suite-level setup/teardown pseudo-tests
            parent = result_file.parent
            if parent.name.startswith("_"):
                continue
            entries.extend(self._parse_result(result_file))

        # Sort by test_id lexicographically
        entries.sort(key=lambda e: e.test_id)
        return entries

    def _parse_result(self, result_file: Path) -> list[TestResultEntry]:
        """Parse a result.json into one or more TestResultEntry.

        Single-test result.json → one entry. A run result.json (``kind: "run"``)
        → one entry per case, all sharing the run's artifact directory.
        """
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not parse %s: %s", result_file, exc)
            return []

        artifact_dir = result_file.parent
        screenshots = sorted(artifact_dir.glob("step-*.jpg"))
        ai_result_files = sorted(artifact_dir.glob("step-*-ai.json"))

        if data.get("kind") == "run":
            entries: list[TestResultEntry] = []
            for case in data.get("cases", []):
                bridge_result = case.get("bridge_result", "skip")
                ai_result = case.get("ai_result", "skip")
                entries.append(
                    TestResultEntry(
                        test_id=case.get("id", artifact_dir.name),
                        title=case.get("title", case.get("id", "")),
                        bridge_result=bridge_result,
                        ai_result=ai_result,
                        final_verdict=_compute_verdict(bridge_result, ai_result),
                        error=case.get("error"),
                        duration_ms=int(data.get("duration_ms", 0)),
                        timed_out=bool(data.get("timed_out", False)),
                        artifact_dir=artifact_dir,  # shared run dir
                        screenshots=screenshots,
                        ai_result_files=ai_result_files,
                    )
                )
            return entries

        test_id = data.get("test_id", artifact_dir.name)
        bridge_result = data.get("bridge_result", "skip")
        ai_result = data.get("ai_result", "skip")
        # Re-compute verdict from raw fields — authoritative source
        return [
            TestResultEntry(
                test_id=test_id,
                title=data.get("title", test_id),
                bridge_result=bridge_result,
                ai_result=ai_result,
                final_verdict=_compute_verdict(bridge_result, ai_result),
                error=data.get("error"),
                duration_ms=int(data.get("duration_ms", 0)),
                timed_out=bool(data.get("timed_out", False)),
                artifact_dir=artifact_dir,
                screenshots=screenshots,
                ai_result_files=ai_result_files,
            )
        ]

    # ------------------------------------------------------------------
    # Verdict counting
    # ------------------------------------------------------------------

    @staticmethod
    def _count_verdicts(entries: list[TestResultEntry]) -> dict[str, int]:
        counts = {"green": 0, "orange": 0, "red": 0, "pending": 0, "skip": 0}
        for e in entries:
            verdict = e.final_verdict
            counts[verdict] = counts.get(verdict, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # summary.json
    # ------------------------------------------------------------------

    def _write_summary(
        self,
        entries: list[TestResultEntry],
        counts: dict[str, int],
    ) -> Path:
        """Write or update summary.json in the results directory."""
        summary_path = self.results_dir / "summary.json"

        # Preserve existing cost data from a prior summary.json if present
        existing_costs: dict[str, Any] = {}
        if summary_path.exists():
            try:
                existing = json.loads(summary_path.read_text(encoding="utf-8"))
                existing_costs = existing.get("costs", {})
            except (json.JSONDecodeError, OSError):
                pass

        tests_list = [
            {
                "test_id": e.test_id,
                "title": e.title,
                "bridge_result": e.bridge_result,
                "ai_result": e.ai_result,
                "final_verdict": e.final_verdict,
                "duration_ms": e.duration_ms,
                "error": e.error,
                "timed_out": e.timed_out,
                "artifact_dir": str(e.artifact_dir),
            }
            for e in entries
        ]

        # Parse suite/game/env from directory path if not in existing summary
        dir_name = self.results_dir.name  # e.g. 2026-04-07-suite-01-core
        suite_name = dir_name[11:] if len(dir_name) > 11 else dir_name  # strip date prefix

        summary: dict[str, Any] = {
            "suite_dir": str(self.results_dir),
            "suite": suite_name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(entries),
            "counts": counts,
            "tests": tests_list,
            "costs": existing_costs,
        }

        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return summary_path

    # ------------------------------------------------------------------
    # report.html (suite-level)
    # ------------------------------------------------------------------

    def _write_report_html(
        self,
        entries: list[TestResultEntry],
        counts: dict[str, int],
    ) -> Path:
        """Generate suite-level report.html."""
        report_path = self.results_dir / "report.html"

        rows_html = ""
        for e in entries:
            verdict = e.final_verdict
            bg = _VERDICT_BG.get(verdict, "#fff")
            color = _VERDICT_CSS.get(verdict, "#000")

            # Inline screenshots for failures (orange/red)
            screenshots_html = ""
            if verdict in ("orange", "red") and e.screenshots:
                imgs = ""
                for ss in e.screenshots[:3]:  # limit to first 3
                    rel = ss.relative_to(self.results_dir)
                    imgs += (
                        f'<img src="{rel}" alt="{ss.name}" '
                        f'style="max-height:120px;margin:4px;border:1px solid #ccc;">'
                    )
                if imgs:
                    screenshots_html = f'<div class="screenshots">{imgs}</div>'

            # Replay link
            replay_path_rel = (
                str(e.artifact_dir.relative_to(self.results_dir) / "replay.html")
            )
            replay_link = f'<a href="{replay_path_rel}" target="_blank">Replay</a>'

            # Video link
            video_rel = str(e.artifact_dir.relative_to(self.results_dir) / "session.webm")
            video_link = f'<a href="{video_rel}" target="_blank">Video</a>'

            error_html = (
                f'<div class="error">{_html_escape(e.error)}</div>' if e.error else ""
            )
            detail = ""
            if verdict != "green":
                detail = (
                    f'<span class="detail">'
                    f"bridge={e.bridge_result} ai={e.ai_result}"
                    f"</span>"
                )

            duration_s = e.duration_ms / 1000.0
            rows_html += f"""
<tr style="background:{bg}">
  <td><span style="color:{color};font-weight:bold">{_html_escape(verdict.upper())}</span></td>
  <td>{_html_escape(e.test_id)}</td>
  <td>{_html_escape(e.title)}</td>
  <td>{detail}</td>
  <td>{duration_s:.1f}s</td>
  <td>{replay_link} {video_link}</td>
</tr>
{f'<tr><td colspan="6">{error_html}{screenshots_html}</td></tr>' if (error_html or screenshots_html) else ''}
"""

        total = len(entries)
        green = counts.get("green", 0)
        orange = counts.get("orange", 0)
        red = counts.get("red", 0)
        pending = counts.get("pending", 0)

        dir_name = self.results_dir.name
        title = f"QA Report — {dir_name}"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_escape(title)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px; color: #1f2937; }}
  h1 {{ font-size: 1.25rem; margin-bottom: 8px; }}
  .summary {{ display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }}
  .badge {{ padding: 4px 12px; border-radius: 999px; font-weight: 600; font-size: 0.875rem; }}
  .green {{ background: #d1fae5; color: #065f46; }}
  .orange {{ background: #ffedd5; color: #9a3412; }}
  .red {{ background: #fee2e2; color: #991b1b; }}
  .pending {{ background: #f3f4f6; color: #374151; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  th {{ text-align: left; padding: 8px; background: #f9fafb; border-bottom: 2px solid #e5e7eb; }}
  td {{ padding: 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
  .detail {{ color: #6b7280; font-size: 0.8rem; }}
  .error {{ color: #991b1b; font-size: 0.8rem; margin: 4px 0; font-family: monospace; }}
  .screenshots {{ margin: 4px 0; }}
  a {{ color: #2563eb; }}
</style>
</head>
<body>
<h1>{_html_escape(title)}</h1>
<div class="summary">
  <span class="badge green">{green} green</span>
  <span class="badge orange">{orange} orange</span>
  <span class="badge red">{red} red</span>
  <span class="badge pending">{pending} pending</span>
  <span style="color:#6b7280;font-size:0.875rem;margin-top:4px">Total: {total}</span>
</div>
<table>
<thead>
  <tr>
    <th>Verdict</th><th>ID</th><th>Title</th><th>Detail</th><th>Duration</th><th>Links</th>
  </tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</body>
</html>
"""
        report_path.write_text(html, encoding="utf-8")
        return report_path

    # ------------------------------------------------------------------
    # replay.html (per-test)
    # ------------------------------------------------------------------

    def _write_replay_html(self, entry: TestResultEntry, video_file: str = "session.webm") -> None:
        """Generate replay.html for a single test."""
        replay_template = self.template_dir / "replay.html"
        if not replay_template.exists():
            logger.warning("replay.html template not found at %s — skipping replay generation", replay_template)
            return

        # Load events
        events: list[dict[str, Any]] = []
        events_path = entry.artifact_dir / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Load timeline
        timeline: dict[str, Any] = {}
        timeline_path = entry.artifact_dir / "timeline.json"
        if timeline_path.exists():
            try:
                timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Inline data for the template
        replay_data = {
            "test_id": entry.test_id,
            "title": entry.title,
            "final_verdict": entry.final_verdict,
            "bridge_result": entry.bridge_result,
            "ai_result": entry.ai_result,
            "duration_ms": entry.duration_ms,
            "error": entry.error,
            "video_file": video_file,
            "video_event_offset": timeline.get("video_event_offset", 0.0),
            "steps": timeline.get("steps", []),
            "events": events,
        }

        # Render template: replace %%REPLAY_DATA%% placeholder with JSON
        template_content = replay_template.read_text(encoding="utf-8")
        data_json = json.dumps(replay_data, ensure_ascii=False)
        rendered = template_content.replace("%%REPLAY_DATA%%", data_json)

        replay_out = entry.artifact_dir / "replay.html"
        replay_out.write_text(rendered, encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML escaping
# ---------------------------------------------------------------------------


def _html_escape(text: str | None) -> str:
    if not text:
        return ""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate HTML reports from a completed suite run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python framework/runner/reporter.py "
            "--results-dir test-results/zeus/dev/2026-04-07-suite-01"
        ),
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        metavar="DIR",
        help="Path to the suite run results directory",
    )
    parser.add_argument(
        "--template-dir",
        metavar="DIR",
        default=None,
        help="Directory containing replay.html template (default: framework/templates/)",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = _build_arg_parser()
    args = parser.parse_args()

    results_dir = Path(args.results_dir).resolve()
    if not results_dir.exists():
        logger.error("Results directory not found: %s", results_dir)
        sys.exit(1)

    template_dir = Path(args.template_dir).resolve() if args.template_dir else None
    reporter = Reporter(results_dir=results_dir, template_dir=template_dir)

    try:
        summary = reporter.generate()
    except Exception as exc:  # noqa: BLE001
        logger.error("Report generation failed: %s", exc)
        sys.exit(1)

    counts = summary.counts
    sys.stdout.write(
        f"Report: {summary.report_path}\n"
        f"  {counts.get('green',0)} green  "
        f"{counts.get('orange',0)} orange  "
        f"{counts.get('red',0)} red  "
        f"{counts.get('pending',0)} pending\n"
    )
    sys.stdout.flush()


if __name__ == "__main__":
    main()
