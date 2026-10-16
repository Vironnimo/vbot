"""The tray tells how its server process ended when it bypassed its shutdown."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from cli.application.server_exit import ServerExitWatch
from cli.server_management import ServerInstance, resolve_instance
from core.utils.logging import resolve_daily_log_path
from core.utils.server_control import create_server_control, process_started

_STARTED = "2026-10-16T13:00:00.000000Z"


def _target(tmp_path: Path) -> ServerInstance:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return resolve_instance(host="127.0.0.1", port=8420, data_dir=data_dir)


def _logged_fields(target: ServerInstance) -> dict[str, str]:
    lines = resolve_daily_log_path(target.data_dir).read_text(encoding="utf-8").splitlines()
    [line] = [line for line in lines if " vbot.application.server_exit - " in line]
    assert "[WARN]" in line
    return dict(re.findall(r"(\w+)=([^\s)]+)", line))


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows keeps a non-child's exit code")
def test_report_names_the_ended_process_with_its_exit_code_and_exit_time(tmp_path: Path):
    target = _target(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(7)"],
        stdin=subprocess.PIPE,
    )
    try:
        create_server_control(
            target.data_dir,
            target.port,
            pid=child.pid,
            process_create_time=process_started(psutil.Process(child.pid)),
            token="t",
        )
        watch = ServerExitWatch(lambda: target)
        watch.track(_STARTED)
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
    finally:
        child.kill()

    end = watch.report()

    assert (end.pid, end.exit_code) == (child.pid, 7)
    # The exit time the operating system kept bounds the uptime.
    assert end.ended_at is not None and end.ended_at <= datetime.now(UTC)
    assert end.uptime == end.ended_at - datetime(2026, 10, 16, 13, tzinfo=UTC)
    fields = _logged_fields(target)
    assert (fields["pid"], fields["exit_code"]) == (str(child.pid), "7")


def test_a_reused_pid_is_never_taken_for_the_server(tmp_path: Path):
    target = _target(tmp_path)
    # The record's pid now names another process: this one, created at another time.
    create_server_control(
        target.data_dir,
        target.port,
        pid=os.getpid(),
        process_create_time=process_started(psutil.Process()) - 60,
        token="t",
    )
    now = datetime(2026, 10, 16, 13, 5, tzinfo=UTC)
    watch = ServerExitWatch(lambda: target, now=lambda: now)
    watch.track(_STARTED)

    end = watch.report()

    assert (end.pid, end.exit_code) == (None, None)
    # The start time comes from the server's hello; the uptime runs until the tray noticed.
    assert end.uptime == timedelta(minutes=5)
    fields = _logged_fields(target)
    assert (fields["pid"], fields["exit_code"], fields["uptime"]) == ("unknown", "unknown", "5m00s")
