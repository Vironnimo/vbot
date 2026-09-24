"""Launch and read the Playwright WebUI probe (``tests/e2e/perf/ui-probe.mjs``)."""

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
    """Reduce the probe's raw arrays to distributions for the report."""
    long_tasks = [float(value) for value in result.get("long_tasks_ms") or []]
    frame_gaps = [float(value) for value in result.get("frame_gaps_ms") or []]
    return {
        "status": "ok",
        "stop_reason": result.get("stop_reason"),
        "duration_ms": _round(result.get("duration_ms")),
        "frames": result.get("frames"),
        "mutations": result.get("mutations"),
        "long_tasks": {
            "count": len(long_tasks),
            "total_ms": round(sum(long_tasks), 1),
            "max_ms": round(max(long_tasks), 1) if long_tasks else None,
        },
        "frame_gaps": {
            "count": len(frame_gaps),
            "max_ms": round(max(frame_gaps), 1) if frame_gaps else None,
            "p95_ms": distribution(frame_gaps)["p95"],
        },
        "dom_marker_latency_ms": distribution(
            float(value) for value in result.get("marker_latencies_ms") or []
        ),
        "heap_used_mb": _round(result.get("heap_used_mb")),
        "dom_nodes": result.get("dom_nodes"),
    }


def _round(value: Any) -> float | None:
    return round(float(value), 1) if isinstance(value, int | float) else None


class UiProbe:
    """One headless browser watching one Session during the load phase."""

    def __init__(self, *, base_url: str, agent_id: str, log_path: Path, max_seconds: float) -> None:
        self._argv = [
            str(shutil.which("node") or "node"),
            str(PROBE_SCRIPT),
            "--url",
            base_url,
            "--agent",
            agent_id,
            "--max-seconds",
            str(int(max_seconds)),
        ]
        self._log_path = log_path
        self._process: subprocess.Popen[str] | None = None
        self._log_file: TextIO | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()

    def start(self) -> None:
        """Open the WebUI and return once the probe measures."""
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
        if message.get("event") != "ready":
            self.close()
            raise UiProbeError(f"UI probe did not start: {message}")

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
