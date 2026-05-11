"""Tests for read-only log viewing utilities."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

from core.utils.log_viewer import (
    LogViewer,
    _build_snapshot_event,
    _LogSnapshot,
    parse_log_entries,
)


def test_parse_log_entries_groups_multiline_continuations() -> None:
    entries = parse_log_entries(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started",
                "Traceback (most recent call last):",
                '  File "server/app.py", line 10, in create_app',
                "2026-05-11 09:00:01 [WARN] vbot.server.app - Slow request",
            ]
        )
    )

    assert entries == [
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Server started",
            "continuation": (
                'Traceback (most recent call last):\n  File "server/app.py", line 10, in create_app'
            ),
        },
        {
            "timestamp": "2026-05-11 09:00:01",
            "level": "warn",
            "logger_name": "vbot.server.app",
            "message": "Slow request",
            "continuation": "",
        },
    ]


def test_parse_log_entries_keeps_orphan_lines_visible() -> None:
    entries = parse_log_entries("orphan line\n2026-05-11 09:00:00 [ERROR] vbot.core - Boom")

    assert entries == [
        {
            "timestamp": "",
            "level": "unknown",
            "logger_name": "",
            "message": "orphan line",
            "continuation": "",
        },
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "error",
            "logger_name": "vbot.core",
            "message": "Boom",
            "continuation": "",
        },
    ]


def test_parse_log_entries_filters_routine_websocket_noise_but_keeps_real_transport_logs() -> None:
    entries = parse_log_entries(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - connection open",
                "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
                "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
            ]
        )
    )

    assert entries == [
        {
            "timestamp": "2026-05-11 09:00:04",
            "level": "warn",
            "logger_name": "vbot.server.uvicorn",
            "message": "keepalive ping timeout",
            "continuation": "",
        },
        {
            "timestamp": "2026-05-11 09:00:05",
            "level": "error",
            "logger_name": "vbot.server.uvicorn",
            "message": "opening handshake failed",
            "continuation": "",
        },
        {
            "timestamp": "2026-05-11 09:00:06",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Ready",
            "continuation": "",
        },
    ]


def test_list_files_returns_newest_first_with_default_selection(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-09").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-11").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-10").write_text("", encoding="utf-8")
    (logs_dir / "subdir").mkdir()

    viewer = LogViewer(tmp_path)

    assert viewer.list_files() == {
        "files": ["2026-05-11", "2026-05-10", "2026-05-09"],
        "default_file": "2026-05-11",
    }


def test_read_file_returns_structured_entries(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Server started",
                "details line",
                "2026-05-11 09:00:01 [ERROR] vbot.core - Boom",
            ]
        ),
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)

    result = viewer.read_file("2026-05-11")

    assert result["file"] == "2026-05-11"
    assert result["entries"] == [
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Server started",
            "continuation": "details line",
        },
        {
            "timestamp": "2026-05-11 09:00:01",
            "level": "error",
            "logger_name": "vbot.core",
            "message": "Boom",
            "continuation": "",
        },
    ]
    assert isinstance(result["cursor"], str)
    assert result["cursor"]


def test_read_file_filters_persisted_websocket_noise(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - connection open",
                "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                "2026-05-11 09:00:05 [INFO] vbot.server.app - Ready",
            ]
        ),
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)

    result = viewer.read_file("2026-05-11")

    assert result["entries"] == [
        {
            "timestamp": "2026-05-11 09:00:04",
            "level": "warn",
            "logger_name": "vbot.server.uvicorn",
            "message": "keepalive ping timeout",
            "continuation": "",
        },
        {
            "timestamp": "2026-05-11 09:00:05",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Ready",
            "continuation": "",
        },
    ]


@pytest.mark.asyncio
async def test_subscribe_replays_entries_appended_after_read_handoff(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)
    initial = viewer.read_file("2026-05-11")
    cursor = initial["cursor"]

    log_file.write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
                "",
            ]
        ),
        encoding="utf-8",
    )

    async with aclosing(viewer.subscribe("2026-05-11", cursor=cursor)) as stream:
        event = await asyncio.wait_for(stream.__anext__(), timeout=1)

    assert event == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Failed",
                "continuation": "",
            }
        ],
    }


@pytest.mark.asyncio
async def test_subscribe_filters_routine_websocket_noise_from_handoff_append(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "2026-05-11"
    log_file.write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)
    initial = viewer.read_file("2026-05-11")
    cursor = initial["cursor"]

    log_file.write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - connection open",
                "2026-05-11 09:00:03 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                "",
            ]
        ),
        encoding="utf-8",
    )

    async with aclosing(viewer.subscribe("2026-05-11", cursor=cursor)) as stream:
        event = await asyncio.wait_for(stream.__anext__(), timeout=1)

    assert event == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:03",
                "level": "warn",
                "logger_name": "vbot.server.uvicorn",
                "message": "keepalive ping timeout",
                "continuation": "",
            }
        ],
    }


def test_build_snapshot_event_resets_using_filtered_entries() -> None:
    previous = _LogSnapshot(
        exists=True,
        size=10,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "info",
                "logger_name": "vbot.server.app",
                "message": "Ready",
                "continuation": "",
            }
        ],
    )
    current = _LogSnapshot(
        exists=True,
        size=25,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "warn",
                "logger_name": "vbot.server.uvicorn",
                "message": "keepalive ping timeout",
                "continuation": "",
            }
        ],
    )

    assert _build_snapshot_event("2026-05-11", previous, current) == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": current.entries,
    }


@pytest.mark.asyncio
async def test_subscribe_rejects_invalid_cursor(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\n",
        encoding="utf-8",
    )

    viewer = LogViewer(tmp_path)

    with pytest.raises(ValueError, match="invalid log cursor"):
        async with aclosing(viewer.subscribe("2026-05-11", cursor="missing")) as stream:
            await stream.__anext__()


def test_read_file_rejects_invalid_name(tmp_path: Path) -> None:
    viewer = LogViewer(tmp_path)

    with pytest.raises(ValueError, match="invalid log file name"):
        viewer.read_file("../2026-05-11")


def test_build_snapshot_event_appends_only_new_entries() -> None:
    previous = _LogSnapshot(
        exists=True,
        size=10,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "info",
                "logger_name": "vbot.server.app",
                "message": "Server started",
                "continuation": "",
            }
        ],
    )
    current = _LogSnapshot(
        exists=True,
        size=20,
        entries=[
            previous.entries[0],
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "warn",
                "logger_name": "vbot.server.app",
                "message": "Slow request",
                "continuation": "",
            },
        ],
    )

    assert _build_snapshot_event("2026-05-11", previous, current) == {
        "type": "append",
        "file": "2026-05-11",
        "entries": [
            {
                "timestamp": "2026-05-11 09:00:01",
                "level": "warn",
                "logger_name": "vbot.server.app",
                "message": "Slow request",
                "continuation": "",
            }
        ],
    }


def test_build_snapshot_event_resets_when_previous_tail_changes() -> None:
    previous = _LogSnapshot(
        exists=True,
        size=10,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Boom",
                "continuation": "",
            }
        ],
    )
    current = _LogSnapshot(
        exists=True,
        size=25,
        entries=[
            {
                "timestamp": "2026-05-11 09:00:00",
                "level": "error",
                "logger_name": "vbot.server.app",
                "message": "Boom",
                "continuation": "Traceback line",
            }
        ],
    )

    assert _build_snapshot_event("2026-05-11", previous, current) == {
        "type": "reset",
        "file": "2026-05-11",
        "entries": current.entries,
    }
