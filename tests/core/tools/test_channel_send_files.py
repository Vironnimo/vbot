"""channel_send with files: local paths in every spelling, and the files it refuses."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.core.tools.channel_send_test_support import (
    channel_send,
    delivered,
    refused,
)

PNG = b"\x89PNG\r\n\x1a\nchart"


def _workspace_files(workspace: Path) -> None:
    (workspace / "note.txt").write_bytes(b"hello")
    (workspace / "b.txt").write_bytes(b"b")
    (workspace / "chart.png").write_bytes(PNG)


@pytest.mark.parametrize(
    ("arguments", "message", "files"),
    [
        pytest.param(
            lambda _workspace: {"file_paths": ["note.txt"]},
            None,
            [("note.txt", "text/plain")],
            id="relative-path",
        ),
        pytest.param(
            lambda workspace: {"message": "caption", "file_paths": [str(workspace / "chart.png")]},
            "caption",
            [("chart.png", "image/png")],
            id="absolute-path-with-caption",
        ),
        pytest.param(
            lambda workspace: {"file_paths": [(workspace / "note.txt").as_uri()]},
            None,
            [("note.txt", "text/plain")],
            id="file-uri",
        ),
        pytest.param(
            lambda _workspace: {"message": "Here it is\nMEDIA:chart.png"},
            "Here it is",
            [("chart.png", "image/png")],
            id="media-marker",
        ),
        pytest.param(
            lambda _workspace: {"message": 'MEDIA:"chart.png"'},
            None,
            [("chart.png", "image/png")],
            id="media-marker-alone",
        ),
        pytest.param(
            lambda _workspace: {
                "attachments": [{"media": "note.txt", "name": "A"}, {"path": "b.txt"}]
            },
            None,
            [("note.txt", "text/plain"), ("b.txt", "text/plain")],
            id="attachment-objects",
        ),
        pytest.param(
            lambda _workspace: {"file": "note.txt", "caption": "One file"},
            "One file",
            [("note.txt", "text/plain")],
            id="single-file",
        ),
    ],
)
def test_files_reach_the_channel(
    tmp_path: Path,
    arguments: Callable[[Path], dict[str, Any]],
    message: str | None,
    files: list[tuple[str, str]],
) -> None:
    tool = channel_send(tmp_path)
    _workspace_files(tool.workspace)

    envelope = tool.call(arguments(tool.workspace))

    assert delivered(envelope) == {"channel_id": "tg-main", "platform_target": "111"}
    [(channel_id, sent_message, target, options)] = tool.sent()
    assert (channel_id, sent_message, target) == ("tg-main", message, "111")
    assert [(item.filename, item.media_type) for item in options["files"]] == files
    assert [item.data for item in options["files"]] == [
        (tool.workspace / name).read_bytes() for name, _type in files
    ]


def test_relative_paths_start_in_the_working_directory(tmp_path: Path) -> None:
    tool = channel_send(tmp_path)
    (tool.workspace / "note.txt").write_text("from workspace", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "note.txt").write_text("from repo", encoding="utf-8")

    envelope = tool.call({"file_paths": ["note.txt"]}, cwd=repo)

    delivered(envelope)
    assert [item.data for item in tool.sent()[0][3]["files"]] == [b"from repo"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        *(
            pytest.param(
                {"file_paths": [address]},
                f'channel_send was not run: file_paths "{address}" is a web address, not a file '
                "on this computer. Send the file's local path, or put the link in message.",
                id=f"web-address-{index}",
            )
            for index, address in enumerate(
                ["https://example.test/a.png", "/api/images/artifacts/abc", "data:image/png;x"]
            )
        ),
        pytest.param(
            {"file_paths": ["file://fileserver.example/share/note.txt"]},
            'channel_send was not run: file_paths "file://fileserver.example/share/note.txt" is '
            "a web address, not a file on this computer. Send the file's local path, or put "
            "the link in message.",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="a UNC path on Windows"),
            id="file-url-of-another-computer",
        ),
        pytest.param(
            {"file_paths": ["shots"]},
            'channel_send was not run: file_paths "shots" is a folder; list the files in it one '
            "by one.",
            id="folder",
        ),
        pytest.param(
            {"file_paths": ["missing.pdf"]},
            'channel_send was not run: file_paths "missing.pdf" does not exist '
            '(<missing path>). Files with similar names there: "missing.pdf.txt".',
            id="missing-file",
        ),
        pytest.param(
            {"file_paths": ["two-kb.bin"]},
            'channel_send was not run: file_paths "two-kb.bin" is 2 KB; files sent through a '
            "Channel may be at most 1000 bytes.",
            id="kilobytes-over-limit",
        ),
        pytest.param(
            {
                "message": "Pick",
                "file_paths": ["note.txt"],
                "buttons": [[{"label": "OK", "data": "run:ok"}]],
            },
            "channel_send was not run: buttons cannot go with files in one message. Send the "
            'files first, then the buttons, in two calls: {"channel_id":"tg-main","message":'
            '"Pick","file_paths":["note.txt"]} then {"channel_id":"tg-main","message":"Pick",'
            '"buttons":"<the buttons from this call>"}',
            id="buttons-with-files",
        ),
    ],
)
def test_files_that_cannot_be_sent_are_refused(
    tmp_path: Path, arguments: dict[str, Any], message: str
) -> None:
    tool = channel_send(tmp_path)
    _workspace_files(tool.workspace)
    (tool.workspace / "shots").mkdir()
    (tool.workspace / "missing.pdf.txt").write_bytes(b"x")
    (tool.workspace / "two-kb.bin").write_bytes(bytes(2048))

    envelope = tool.call(arguments)

    missing_path = str(tool.workspace / "missing.pdf")
    assert refused(envelope) == message.replace("<missing path>", missing_path)
    assert tool.sent() == []
