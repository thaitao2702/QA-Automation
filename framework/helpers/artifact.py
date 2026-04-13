"""
Test artifact management for the QA platform.

Manages per-test output directories, screenshots, event logs, timeline data,
and result files following the naming convention:
  test-results/{game}/{env}/{suite}-{YYYY-MM-DD-HHmmss}/{test-id}/

All write operations are append-safe. The event log (events.jsonl) writes one
JSON object per line so partial writes are recoverable if the process terminates.

Public interface
----------------
ArtifactContext(run_dir, test_id)
    Create and manage a single test's output directory.

    test_dir: Path
        The test output directory (created on construction).

    save_screenshot(data: bytes) -> Path
        Save screenshot bytes with an auto-incremented step number.

    append_events(events: list[dict]) -> None
        Append events to events.jsonl (one JSON object per line).

    write_timeline(video_start_time, event_collection_start_time, steps) -> None
        Write timeline.json with step timestamps and video-event offset.

    write_result(bridge_result, ai_result, final_verdict, extra) -> None
        Write result.json with verdict fields.

    Path accessors:
        events_path, timeline_path, result_path, video_path, replay_path,
        screenshot_path(step), ai_result_path(step)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ArtifactContext:
    """
    Manages the output directory and files for a single test run.

    Parameters
    ----------
    run_dir:
        The suite run directory (e.g.
        ``test-results/{game}/{env}/{suite}-{YYYY-MM-DD-HHmmss}/``).
    test_id:
        Test identifier (e.g. ``'ZZ-01'``, ``'_setup'``).
    """

    def __init__(
        self,
        run_dir: Path,
        test_id: str,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._test_id = test_id
        self._screenshot_counter: int = 0

        # Create the output directory immediately
        self.test_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Directory path
    # ------------------------------------------------------------------

    @property
    def test_dir(self) -> Path:
        """
        Output directory for this test.

        Pattern: ``{run_dir}/{test_id}/``
        """
        return self._run_dir / self._test_id

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    def save_screenshot(self, data: bytes) -> Path:
        """
        Write screenshot bytes to the test directory with an auto-incremented
        step number.

        Returns
        -------
        Path
            The path where the screenshot was written (e.g. ``step-01.jpg``).
        """
        self._screenshot_counter += 1
        path = self.screenshot_path(self._screenshot_counter)
        path.write_bytes(data)
        logger.debug("Saved screenshot: %s", path)
        return path

    # ------------------------------------------------------------------
    # Event log (JSONL — append-safe)
    # ------------------------------------------------------------------

    def append_events(self, events: list[dict[str, Any]]) -> None:
        """
        Append *events* to ``events.jsonl``, one JSON object per line.

        Using append mode (``"a"``) and flushing after each write ensures that
        events are preserved even if the process terminates mid-run. Each line
        is independently parseable.

        Parameters
        ----------
        events:
            List of event dicts with ``t``, ``type``, and ``data`` fields.
        """
        with self.events_path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def write_timeline(
        self,
        video_start_time: float,
        event_collection_start_time: float,
        steps: list[dict[str, Any]],
    ) -> None:
        """
        Write ``timeline.json`` with step timestamps and video-event offset.

        The ``video_event_offset`` is the number of seconds between when video
        recording started and when event collection started. Used by the replay
        viewer and video trimming to align events with video timestamps.

        Parameters
        ----------
        video_start_time:
            Unix timestamp when video recording started.
        event_collection_start_time:
            Unix timestamp when ``startEventCollection()`` was called.
        steps:
            List of step timing dicts, each with ``id``, ``action``,
            ``start_ms`` and ``end_ms`` fields.
        """
        video_event_offset = event_collection_start_time - video_start_time
        timeline = {
            "video_start_time": video_start_time,
            "event_collection_start_time": event_collection_start_time,
            "video_event_offset": round(video_event_offset, 3),
            "steps": steps,
        }
        self.timeline_path.write_text(
            json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Result file
    # ------------------------------------------------------------------

    def write_result(
        self,
        bridge_result: str,
        ai_result: str,
        final_verdict: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """
        Write ``result.json`` with verdict fields.

        Parameters
        ----------
        bridge_result:
            ``'pass'``, ``'fail'``, or ``'skip'``.
        ai_result:
            ``'pass'``, ``'fail'``, ``'skip'``, or ``'pending'``.
        final_verdict:
            ``'green'``, ``'orange'``, ``'red'``, or ``'pending'``.
        extra:
            Optional additional fields to merge into the result document
            (e.g. ``{'error': None, 'duration_ms': 1234}``).
        """
        result: dict[str, Any] = {
            "bridge_result": bridge_result,
            "ai_result": ai_result,
            "final_verdict": final_verdict,
        }
        if extra:
            result.update(extra)
        self.result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Also generate a human-readable HTML view of the result
        self._write_result_html(result)

    def _write_result_html(self, result: dict[str, Any]) -> None:
        """Generate ``result.html`` — a human-readable view of result.json."""
        verdict = result.get("final_verdict", "skip")
        verdict_dot_colors = {
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
        dot_color = verdict_dot_colors.get(verdict, "#9ca3af")
        verdict_label = verdict_labels.get(verdict, verdict.title())

        test_id = _html_escape(str(result.get("test_id", "")))
        title = _html_escape(str(result.get("title", "")))
        duration_ms = result.get("duration_ms", 0)
        duration_s = duration_ms / 1000.0
        error = result.get("error")
        timed_out = result.get("timed_out", False)

        # Build key-value rows for all fields (skip final_verdict, rendered as dot)
        rows = ""
        field_order = [
            "test_id", "title", "bridge_result", "ai_result",
            "duration_ms", "timed_out", "error",
        ]
        rendered = {"final_verdict"}
        for key in field_order:
            if key in result:
                rendered.add(key)
                rows += self._result_html_row(key, result[key])
        # Render any remaining fields
        for key, val in result.items():
            if key not in rendered:
                rows += self._result_html_row(key, val)

        error_banner = ""
        if error:
            error_banner = (
                f'<div style="background:#fee2e2;border:1px solid #fca5a5;'
                f'border-radius:6px;padding:12px;margin-bottom:16px;'
                f'font-family:monospace;font-size:0.85rem;color:#991b1b;">'
                f"{_html_escape(str(error))}</div>"
            )

        timeout_badge = ""
        if timed_out:
            timeout_badge = (
                ' <span style="background:#fef3c7;color:#92400e;padding:2px 8px;'
                'border-radius:999px;font-size:0.75rem;font-weight:600;">TIMED OUT</span>'
            )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Result — {test_id}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 24px; color: #1f2937; background: #f9fafb; }}
  .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); max-width: 640px; margin: 0 auto; overflow: hidden; }}
  .header {{ padding: 16px 20px; display: flex; align-items: center; gap: 12px; }}
  .verdict-dot {{ width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
  .verdict-dot .label {{ color: #fff; font-weight: 700; font-size: 0.55rem; text-transform: uppercase; letter-spacing: 0.03em; }}
  .title-block {{ flex: 1; }}
  .title-block h1 {{ font-size: 1.1rem; margin: 0; }}
  .title-block .subtitle {{ font-size: 0.85rem; color: #6b7280; margin-top: 2px; }}
  .body {{ padding: 0 20px 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  tr {{ border-bottom: 1px solid #f3f4f6; }}
  tr:last-child {{ border-bottom: none; }}
  th {{ text-align: left; padding: 8px 12px; color: #6b7280; font-weight: 500; width: 140px; vertical-align: top; }}
  td {{ padding: 8px 12px; word-break: break-word; }}
  .pass {{ color: #065f46; }}
  .fail {{ color: #991b1b; }}
  .skip {{ color: #6b7280; }}
  .pending {{ color: #374151; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="verdict-dot" style="background:{dot_color};">
      <span class="label">{_html_escape(verdict_label)}</span>
    </div>
    <div class="title-block">
      <h1>{test_id}{timeout_badge}</h1>
      <div class="subtitle">{title} &mdash; {duration_s:.1f}s</div>
    </div>
  </div>
  <div class="body">
    {error_banner}
    <table>
      {rows}
    </table>
  </div>
</div>
</body>
</html>
"""
        result_html_path = self.test_dir / "result.html"
        result_html_path.write_text(html, encoding="utf-8")
        logger.debug("Saved result HTML: %s", result_html_path)

    @staticmethod
    def _result_html_row(key: str, value: Any) -> str:
        """Build a single <tr> for the result HTML table."""
        label = key.replace("_", " ").title()
        if isinstance(value, bool):
            display = "Yes" if value else "No"
        elif value is None:
            display = "&mdash;"
        else:
            display = _html_escape(str(value))

        # Add semantic CSS class for pass/fail/skip/pending values
        css = ""
        if isinstance(value, str) and value in ("pass", "fail", "skip", "pending"):
            css = f' class="{value}"'

        return f"<tr><th>{_html_escape(label)}</th><td{css}>{display}</td></tr>\n"

    # ------------------------------------------------------------------
    # Path accessors
    # ------------------------------------------------------------------

    @property
    def events_path(self) -> Path:
        """Path to the event log file (``events.jsonl``)."""
        return self.test_dir / "events.jsonl"

    @property
    def timeline_path(self) -> Path:
        """Path to the timeline file (``timeline.json``)."""
        return self.test_dir / "timeline.json"

    @property
    def result_path(self) -> Path:
        """Path to the result file (``result.json``)."""
        return self.test_dir / "result.json"

    @property
    def video_path(self) -> Path:
        """Path to the session video file (``session.webm``)."""
        return self.test_dir / "session.webm"

    @property
    def replay_path(self) -> Path:
        """Path to the replay viewer file (``replay.html``)."""
        return self.test_dir / "replay.html"

    def screenshot_path(self, step: int) -> Path:
        """Path for a screenshot at the given step number (e.g. ``step-03.jpg``)."""
        return self.test_dir / f"step-{step:02d}.jpg"

    def ai_result_path(self, step: int) -> Path:
        """Path for an AI result JSON file at the given step (e.g. ``step-02-ai.json``)."""
        return self.test_dir / f"step-{step:02d}-ai.json"


def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
