"""Tests for the POSIX server-lifetime process guardian."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX lifetime pipe contract")
def test_guardian_kills_child_group_when_server_pipe_closes(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,pathlib,time; "
        "pathlib.Path(os.environ['CHILD_PID_PATH']).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    environment = dict(os.environ)
    environment["CHILD_PID_PATH"] = str(pid_path)
    guardian = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.tools.process_guardian",
            "--lifetime-fd",
            str(read_fd),
            "--",
            sys.executable,
            "-c",
            child_code,
        ],
        env=environment,
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    try:
        for _ in range(50):
            if pid_path.exists():
                break
            time.sleep(0.1)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text(encoding="utf-8"))

        os.close(write_fd)
        guardian.wait(timeout=10)
        for _ in range(50):
            if not psutil.pid_exists(child_pid):
                break
            time.sleep(0.1)

        assert not psutil.pid_exists(child_pid)
    finally:
        if guardian.poll() is None:
            kill_process_group = os.killpg  # type: ignore[attr-defined]
            kill_process_group(guardian.pid, 9)
        if write_fd >= 0:
            with suppress(OSError):
                os.close(write_fd)
