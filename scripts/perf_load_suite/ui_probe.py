"""Launch and read the Playwright WebUI probe (``tests/e2e/perf/ui-probe.mjs``).

The probe measures the load phase (Long Tasks, frame gaps, RPC calls and the
browser's own counters). With ``scroll_history`` it first measures a separate
scroll-through phase: it loads every older History page of the watched Session
the way scrolling to the top does, then returns to the bottom.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, TextIO

from core.utils.processes import subprocess_creation_flags
from scripts.perf_load_suite.metrics import distribution
from scripts.perf_load_suite.stack import PROJECT_ROOT, stop_process_tree

E2E_ROOT = PROJECT_ROOT / "tests" / "e2e"
PROBE_SCRIPT = E2E_ROOT / "perf" / "ui-probe.mjs"
PLAYWRIGHT_PACKAGE = E2E_ROOT / "node_modules" / "@playwright" / "test"
READY_TIMEOUT_SECONDS = 90.0
RESULT_TIMEOUT_SECONDS = 60.0
SELECT_TIMEOUT_SECONDS = 60.0
CHAT_VIEW = "chat"
PROFILE_FILE = "ui-profile.cpuprofile"
TOP_RPC_METHODS = 12
# Browser counters per phase: CDP ``Performance.getMetrics`` deltas plus the
# state after a forced garbage collection at the end of the phase.
BROWSER_DURATIONS = ("task_ms", "script_ms", "layout_ms", "recalc_style_ms")
BROWSER_COUNTS = ("layout_count", "recalc_style_count", "nodes", "layout_objects")
PROGRESS_EVENT = "progress"


class UiProbeError(RuntimeError):
    """The browser probe could not start or did not report a result."""


def ui_probe_unavailable_reason() -> str | None:
    """Why ``--ui`` cannot run here, or ``None`` when it can."""
    if shutil.which("node") is None:
        return "Node.js is not on PATH"
    if not PROBE_SCRIPT.is_file():
        return f"probe script missing: {PROBE_SCRIPT}"
    if not PLAYWRIGHT_PACKAGE.is_dir():
        return f"Playwright is not installed; run npm ci in {E2E_ROOT}"
    return None


def summarize_probe(result: dict[str, Any]) -> dict[str, Any]:
    """Reduce the probe's raw arrays to distributions for the report.

    ``rpc_calls`` keeps every ``/api/rpc`` method the browser called while
    measuring (``extensions.operation`` split by ``name/operation``) with its
    count, summed and maximum response time; ``rpc`` totals them.
    """
    page_gaps = [float(value) for value in result.get("page_frame_gaps_ms") or []]
    rpc_calls = _rpc_calls(result.get("rpc_calls"))
    duration_ms = _round(result.get("duration_ms"))
    rpc_count = sum(entry["count"] for entry in rpc_calls.values())
    summary: dict[str, Any] = {
        "status": "ok",
        "stop_reason": result.get("stop_reason"),
        "duration_ms": duration_ms,
        "frames": result.get("frames"),
        "mutations": result.get("mutations"),
        "long_tasks": _long_tasks(result.get("long_tasks_ms")),
        "frame_gaps": _frame_gaps(result.get("frame_gaps_ms")),
        "dom_marker_latency_ms": distribution(
            float(value) for value in result.get("marker_latencies_ms") or []
        ),
        "heap_used_mb": _round(result.get("heap_used_mb")),
        "dom_nodes": result.get("dom_nodes"),
        "rpc": {
            "count": rpc_count,
            "per_second": round(rpc_count / (duration_ms / 1000.0), 2) if duration_ms else None,
            "total_ms": round(sum(entry["total_ms"] for entry in rpc_calls.values()), 1),
            "failed": sum(entry["failed"] for entry in rpc_calls.values()),
        },
        "rpc_calls": rpc_calls,
        "browser": _browser(result.get("browser")),
    }
    if isinstance(result.get("scroll_through"), dict):
        summary["scroll_through"] = summarize_scroll_through(result["scroll_through"])
    if result.get("measured_frames", 1) > 1:
        summary["page_frame_gaps"] = {
            "count": len(page_gaps),
            "max_ms": round(max(page_gaps), 1) if page_gaps else None,
            "p95_ms": distribution(page_gaps)["p95"],
        }
        summary["page_dom_nodes"] = result.get("page_dom_nodes")
        summary["page_mutations"] = result.get("page_mutations")
    return summary


def summarize_scroll_through(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce the scroll-through phase to distributions for the report.

    ``page_ms`` runs from scrolling to the top until the second frame after the
    older page's items appeared; ``rpc_ms`` is that page's ``chat.history``
    request as the browser saw it. ``page_samples`` keeps every page in order,
    with the number of timeline items after it.
    """
    samples = [
        {
            "items": page.get("items"),
            "messages": page.get("messages"),
            "page_ms": _round(page.get("page_ms")),
            "rpc_ms": _round(page.get("rpc_ms")),
        }
        for page in raw.get("pages") or []
        if isinstance(page, dict)
    ]
    page_ms = [sample["page_ms"] for sample in samples if sample["page_ms"] is not None]
    rpc_ms = [sample["rpc_ms"] for sample in samples if sample["rpc_ms"] is not None]
    error = raw.get("error")
    return {
        "status": "failed" if error else "ok",
        "error": error,
        "duration_ms": _round(raw.get("duration_ms")),
        "pages": len(samples),
        "timeline_items": raw.get("timeline_items"),
        "user_messages": raw.get("user_messages"),
        "frames": raw.get("frames"),
        "page_ms": {
            **distribution(page_ms),
            "first": page_ms[0] if page_ms else None,
            "last": page_ms[-1] if page_ms else None,
        },
        "rpc_ms": distribution(rpc_ms),
        "long_tasks": _long_tasks(raw.get("long_tasks_ms")),
        "frame_gaps": _frame_gaps(raw.get("frame_gaps_ms")),
        "browser": _browser(raw.get("browser")),
        "page_samples": samples,
    }


def _long_tasks(raw: Any) -> dict[str, Any]:
    durations = [float(value) for value in raw or []]
    return {
        "count": len(durations),
        "total_ms": round(sum(durations), 1),
        "max_ms": round(max(durations), 1) if durations else None,
    }


def _frame_gaps(raw: Any) -> dict[str, Any]:
    gaps = [float(value) for value in raw or []]
    return {
        "count": len(gaps),
        "max_ms": round(max(gaps), 1) if gaps else None,
        "p95_ms": distribution(gaps)["p95"],
    }


def _browser(raw: Any) -> dict[str, Any] | None:
    """The browser's own counters for one phase (durations in ms)."""
    if not isinstance(raw, dict):
        return None
    browser: dict[str, Any] = {name: _round(raw.get(name)) for name in BROWSER_DURATIONS}
    for name in BROWSER_COUNTS:
        value = raw.get(name)
        browser[name] = round(value) if isinstance(value, int | float) else None
    browser["js_heap_mb"] = _round(raw.get("js_heap_mb"))
    return browser


def top_rpc_methods(rpc_calls: dict[str, Any], limit: int = TOP_RPC_METHODS) -> list[str]:
    """Method names, most called first."""
    return sorted(
        rpc_calls,
        key=lambda name: (-int(rpc_calls[name].get("count") or 0), name),
    )[:limit]


def _rpc_calls(raw: Any) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for method, entry in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(entry, dict):
            continue
        calls[str(method)] = {
            "count": int(entry.get("count") or 0),
            "total_ms": round(float(entry.get("total_ms") or 0.0), 1),
            "max_ms": round(float(entry.get("max_ms") or 0.0), 1),
            "failed": int(entry.get("failed") or 0),
        }
    return {method: calls[method] for method in top_rpc_methods(calls, limit=len(calls))}


def _round(value: Any) -> float | None:
    return round(float(value), 1) if isinstance(value, int | float) else None


class UiProbe:
    """One headless browser watching one WebUI view during the load phase.

    ``view`` is ``chat`` (``agent_id``'s current Session) or an Extension page
    route such as ``extension:swarm:swarms``. With ``profile_path`` the probe
    also writes a CPU profile of the page's main-frame JavaScript there. With
    ``scroll_history`` (chat view) :meth:`start` first scrolls through the
    Session's whole History, which is measured as its own phase.
    """

    def __init__(
        self,
        *,
        base_url: str,
        view: str = CHAT_VIEW,
        agent_id: str | None = None,
        log_path: Path,
        max_seconds: float,
        profile_path: Path | None = None,
        scroll_history: bool = False,
    ) -> None:
        self._argv = [
            str(shutil.which("node") or "node"),
            str(PROBE_SCRIPT),
            "--url",
            base_url,
            "--view",
            view,
            *(["--agent", agent_id] if agent_id else []),
            "--max-seconds",
            str(int(max_seconds)),
            *(["--profile", str(profile_path)] if profile_path else []),
            *(["--scroll-history"] if scroll_history else []),
        ]
        self.profile_path = profile_path
        self._log_path = log_path
        self._process: subprocess.Popen[str] | None = None
        self._log_file: TextIO | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()

    def start(self) -> None:
        """Open the WebUI and return once the probe measures the load phase.

        A scroll-through reports each loaded page; the wait for the next
        message restarts with every report.
        """
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_path.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            self._argv,
            cwd=E2E_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log_file,
            text=True,
            encoding="utf-8",
            # Own process group: a console Ctrl+C reaches only the harness,
            # which then asks the probe for its result or closes it.
            creationflags=subprocess_creation_flags(new_process_group=True),
            start_new_session=os.name != "nt",
        )
        threading.Thread(target=self._read_stdout, name="ui-probe-reader", daemon=True).start()
        message = self._next_message(READY_TIMEOUT_SECONDS)
        while message.get("event") == PROGRESS_EVENT:
            message = self._next_message(READY_TIMEOUT_SECONDS)
        if message.get("event") != "ready":
            self.close()
            raise UiProbeError(f"UI probe did not start: {message}")

    def select_run(self) -> str | None:
        """Select the newest run on the Swarm page; return why that failed, if it did."""
        process = self._process
        if process is None or process.stdin is None:
            return "UI probe is not running"
        process.stdin.write("select-run\n")
        process.stdin.flush()
        message = self._next_message(SELECT_TIMEOUT_SECONDS)
        if message.get("event") == "selected":
            return None
        return str(message.get("message") or message)

    def stop(self) -> dict[str, Any]:
        """Ask the probe to finish and return its summarized measurements."""
        process = self._process
        if process is None or process.stdin is None:
            raise UiProbeError("UI probe is not running")
        try:
            process.stdin.write("stop\n")
            process.stdin.flush()
            message = self._next_message(RESULT_TIMEOUT_SECONDS)
        finally:
            self.close()
        if message.get("event") != "result":
            raise UiProbeError(f"UI probe failed: {message}")
        return summarize_probe(message)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                stop_process_tree(process.pid)
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _next_message(self, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"event": "timeout"}
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return {"event": "timeout"}
            if line is None:
                return {"event": "exited", "log": self._log_tail()}
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                return message

    def _log_tail(self) -> str:
        try:
            return self._log_path.read_text(encoding="utf-8", errors="replace")[-800:]
        except OSError:
            return ""
