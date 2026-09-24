"""Optional py-spy flamegraph of the vBot server during a load phase."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sysconfig
from pathlib import Path
from typing import Any

STOP_TIMEOUT_SECONDS = 30.0
_SAMPLES_PATTERN = re.compile(r"Samples:\s*(\d+)\s+Errors:\s*(\d+)")


def find_py_spy() -> Path | None:
    """Locate py-spy installed for this interpreter (also per-user), then on ``PATH``."""
    executable = "py-spy.exe" if os.name == "nt" else "py-spy"
    scripts_dirs = [
        sysconfig.get_path("scripts"),
        sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")),
    ]
    for scripts_dir in scripts_dirs:
        if scripts_dir:
            candidate = Path(scripts_dir) / executable
            if candidate.is_file():
                return candidate
    found = shutil.which("py-spy")
    return Path(found) if found else None


def py_spy_unavailable_reason() -> str | None:
    """Why ``--profile`` cannot run here, or ``None`` when it can."""
    if find_py_spy() is None:
        return 'py-spy is not installed; run pip install -e ".[dev]"'
    return None


class PySpyRecorder:
    """Record a flamegraph of one process until :meth:`stop` is called.

    Sampling is non-blocking so the profiled server is never paused. py-spy
    writes its output when interrupted, so it runs in its own process group
    that receives Ctrl+Break (Windows) or SIGINT (POSIX) on stop.
    """

    def __init__(self, *, pid: int, output: Path, gil_only: bool, rate: int = 100) -> None:
        self._pid = pid
        self._output = output
        self._log_path = output.with_suffix(".log")
        self._gil_only = gil_only
        self._rate = rate
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        executable = find_py_spy()
        if executable is None:
            raise RuntimeError(py_spy_unavailable_reason())
        argv = [
            str(executable),
            "record",
            "--pid",
            str(self._pid),
            "--output",
            str(self._output),
            "--format",
            "flamegraph",
            "--rate",
            str(self._rate),
            "--nonblocking",
        ]
        if self._gil_only:
            argv.append("--gil")
        self._output.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("wb") as log_file:
            if os.name == "nt":
                # No CREATE_NO_WINDOW: Ctrl+Break needs the shared console.
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
                )
            else:
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

    def stop(self) -> dict[str, Any]:
        process = self._process
        if process is None:
            return {"status": "not_started"}
        self._process = None
        if process.poll() is None:
            interrupt = (
                signal.CTRL_BREAK_EVENT  # type: ignore[attr-defined]
                if os.name == "nt"
                else signal.SIGINT
            )
            process.send_signal(interrupt)
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        log = self._log_path.read_text(encoding="utf-8", errors="replace")
        samples = _SAMPLES_PATTERN.search(log)
        result: dict[str, Any] = {
            "status": "ok" if self._output.is_file() else "failed",
            "mode": "gil" if self._gil_only else "all",
            "flamegraph": self._output.name if self._output.is_file() else None,
            "samples": int(samples.group(1)) if samples else None,
            "errors": int(samples.group(2)) if samples else None,
        }
        if result["status"] == "failed":
            result["log_tail"] = log[-800:]
        return result
