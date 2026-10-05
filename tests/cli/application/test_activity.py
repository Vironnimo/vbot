"""The tray's activity history records server and tray events once and keeps them bounded."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cli.application.activity import (
    JOURNAL_NAME,
    KEEP_ENTRIES,
    ActivityJournal,
    ActivityRecorder,
)
from cli.application.monitor import MonitorStatus

_NOW = datetime(2026, 10, 5, 12, 58, 29, tzinfo=UTC)
_EARLIER = "2026-10-05T09:31:05.000000Z"
_JUST_STARTED = "2026-10-05T12:58:10.000000Z"


def _journal(tmp_path: Path) -> ActivityJournal:
    return ActivityJournal(tmp_path / "logs" / JOURNAL_NAME, now=lambda: _NOW)


def test_journal_keeps_a_bounded_history_across_trays_and_skips_bad_lines(tmp_path: Path):
    journal = _journal(tmp_path)
    journal.record("update_finished", "vBot is already up to date", ref="upd_1")
    path = tmp_path / "logs" / JOURNAL_NAME
    with path.open("a", encoding="utf-8") as file:
        file.write('{"broken\n["not", "an", "entry"]\n')
    journal.record("server_crashed", "Server stopped unexpectedly", level="warning")

    # A later tray (the successor after an update) reads the same history.
    successor = _journal(tmp_path)
    assert [(entry.kind, entry.level) for entry in successor.entries()] == [
        ("update_finished", "info"),
        ("server_crashed", "warning"),
    ]
    # An entry naming what it reports is recorded only once, by any tray.
    successor.record("update_finished", "vBot is already up to date", ref="upd_1")
    assert len(successor.entries()) == 2

    for index in range(2 * KEEP_ENTRIES):
        successor.record("tray_started", f"vBot tray started {index}")
    assert len(path.read_text(encoding="utf-8").splitlines()) < 2 * KEEP_ENTRIES
    kept = _journal(tmp_path).entries()
    assert len(kept) == KEEP_ENTRIES
    assert kept[-1].text == f"vBot tray started {2 * KEEP_ENTRIES - 1}"


def _connected(started_at: str) -> MonitorStatus:
    return MonitorStatus("http://127.0.0.1:8420", "connected", True, "0.4.10", started_at)


@pytest.mark.parametrize(
    ("owns_server", "steps", "kinds"),
    [
        pytest.param(
            True,
            [("status", _connected(_EARLIER))],
            [("server_running", "info")],
            id="already-running",
        ),
        pytest.param(
            True,
            [
                ("status", _connected(_EARLIER)),
                ("event", "cli"),
                ("lost", 1012),
                ("status", MonitorStatus(connection="not_listening")),
                ("phase", "stopping"),
                ("status", MonitorStatus(connection="refused")),
                ("status", MonitorStatus(connection="not_listening")),
                ("phase", "starting"),
                ("status", _connected(_JUST_STARTED)),
            ],
            [("server_running", "info"), ("server_stopped", "info"), ("server_started", "info")],
            id="restart-from-the-command-line",
        ),
        pytest.param(
            True,
            [
                ("status", _connected(_EARLIER)),
                # A server without the stop announcement still gets the tray's own name.
                ("expect_stop", "tray_restart"),
                ("lost", 1012),
                ("status", MonitorStatus(connection="refused")),
                ("status", _connected(_JUST_STARTED)),
            ],
            [
                ("server_running", "info"),
                ("server_restarting", "info"),
                ("server_started", "info"),
            ],
            id="tray-restart",
        ),
        pytest.param(
            True,
            [
                ("status", _connected(_EARLIER)),
                ("lost", 1006),
                ("status", MonitorStatus(connection="not_listening")),
                ("phase", "stopping"),
                ("status", MonitorStatus(connection="refused")),
            ],
            [("server_running", "info"), ("server_crashed", "warning")],
            id="crash",
        ),
        pytest.param(
            True,
            [("status", _connected(_EARLIER)), ("lost", 1006), ("status", _connected(_EARLIER))],
            [("server_running", "info")],
            id="dropped-stream",
        ),
        pytest.param(
            True,
            [
                ("status", _connected(_EARLIER)),
                ("status", MonitorStatus(connection="unresponsive")),
                ("status", _connected(_EARLIER)),
            ],
            [
                ("server_running", "info"),
                ("server_unresponsive", "warning"),
                ("server_responding", "info"),
            ],
            id="busy-server",
        ),
        pytest.param(
            True,
            [
                ("status", MonitorStatus(connection="refused")),
                ("expect_start", None),
                ("status", MonitorStatus(connection="not_listening")),
                ("phase", "starting"),
                ("status", _connected(_JUST_STARTED)),
                ("failed", "Could not stop the server: test"),
            ],
            [("server_started", "info"), ("action_failed", "error")],
            id="tray-start",
        ),
        pytest.param(
            True,
            [("status", MonitorStatus(connection="rejected"))],
            [("port_occupied", "error")],
            id="port-occupied",
        ),
        pytest.param(
            False,
            [("status", _connected(_EARLIER)), ("lost", 1012), ("status", _connected(_EARLIER))],
            [("connected", "info"), ("disconnected", "warning"), ("connected", "info")],
            id="desktop-client",
        ),
    ],
)
def test_recorder_tells_what_happened_to_the_server(
    tmp_path: Path,
    owns_server: bool,
    steps: list[tuple[str, Any]],
    kinds: list[tuple[str, str]],
):
    recorder = ActivityRecorder(_journal(tmp_path), owns_server=owns_server, now=lambda: _NOW)
    for step, value in steps:
        if step == "status":
            recorder.status_changed(value)
        elif step == "event":
            recorder.event_received({"type": "server_stopping", "payload": {"initiator": value}})
        elif step == "lost":
            recorder.connection_lost(value)
        elif step == "expect_stop":
            recorder.expect_stop(value)
        elif step == "expect_start":
            recorder.expect_start()
        elif step == "failed":
            recorder.action_failed(value)
        else:
            assert recorder.phase() == value

    assert [(entry.kind, entry.level) for entry in recorder.journal.entries()] == kinds


def test_recorder_records_each_server_process_once_across_trays(tmp_path: Path):
    first = ActivityRecorder(_journal(tmp_path), owns_server=True, now=lambda: _NOW)
    first.status_changed(_connected(_EARLIER))
    assert first.running_since() == _EARLIER

    # The successor tray finds the same server process already recorded.
    successor = ActivityRecorder(_journal(tmp_path), owns_server=True, now=lambda: _NOW)
    successor.status_changed(_connected(_EARLIER))

    assert [entry.kind for entry in successor.journal.entries()] == ["server_running"]
