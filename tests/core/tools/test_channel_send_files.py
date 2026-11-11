"""channel_send with files: local paths in every spelling, and the files it refuses."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
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
            lambda _workspace: {"file_paths": json.dumps(["note.txt", "b.txt"])},
            None,
            [("note.txt", "text/plain"), ("b.txt", "text/plain")],
            id="json-file-list",
        ),
        pytest.param(
            lambda _workspace: {
                "attachments": [
                    {
                        "path": "chart.png",
                        "filename": "chart.png",
                        "type": "file",
                    }
                ]
            },
            None,
            [("chart.png", "image/png")],
            id="attachment-metadata",
        ),
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
                "attachments": [
                    {"media": "note.txt", "path": "note.txt", "name": ""},
                    {"path": "b.txt"},
                ]
            },
            None,
            [("note.txt", "text/plain"), ("b.txt", "text/plain")],
            id="attachment-objects",
        ),
        pytest.param(
            lambda _workspace: {"message": "Caption  \r\n\r\n\r\nMEDIA:note.txt\r\nKeep  this."},
            "Caption  \r\n\r\n\r\nKeep  this.",
            [("note.txt", "text/plain")],
            id="media-marker-keeps-surrounding-payload",
        ),
        pytest.param(
            lambda _workspace: {"message": "```\nMEDIA:note.txt\n```\nMEDIA:b.txt\nEnd"},
            "```\nMEDIA:note.txt\n```\nEnd",
            [("b.txt", "text/plain")],
            id="only-unquoted-marker-is-an-attachment",
        ),
        *(
            pytest.param(
                lambda _workspace, message=message: {"message": message},
                message,
                [],
                id=f"literal-media-{case}",
            )
            for case, message in (
                ("prose", "Print the literal MEDIA:note.txt in the documentation."),
                ("quoted", 'The marker is "MEDIA:note.txt".'),
                ("blockquote", "Quoted:\n> MEDIA:note.txt"),
                ("indented-code", "Example:\n    MEDIA:note.txt"),
                ("code-fence", "```text\nMEDIA:note.txt\n```"),
                ("long-fence", "~~~~\nMEDIA:note.txt\n~~~\nMEDIA:b.txt\n~~~~"),
                ("unclosed-fence", "```\nMEDIA:note.txt"),
            )
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

    delivered(envelope)
    [(channel_id, sent_message, target, options)] = tool.sent()
    assert (channel_id, sent_message, target) == ("tg-main", message, "111")
    assert [(item.filename, item.media_type) for item in options["files"] or []] == files
    assert [item.data for item in options["files"] or []] == [
        (tool.workspace / name).read_bytes() for name, _type in files
    ]
    assert all(name in envelope["data"]["content"] for name, _ in files)


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
    "arguments",
    [
        *(
            pytest.param({"attachments": [file_object]}, id=f"file-object-{case}")
            for case, file_object in (
                ("conflicting-paths", {"path": "note.txt", "url": "b.txt"}),
                ("conflicting-spellings", {"path": "note.txt", "Path": "b.txt"}),
                ("name", {"path": "note.txt", "name": "other.txt"}),
                ("filename", {"path": "note.txt", "filename": "other.txt"}),
                ("caption", {"path": "note.txt", "caption": "Read this"}),
                ("condition", {"path": "note.txt", "private": False}),
                ("voice", {"path": "chart.png", "type": "voice"}),
                ("document", {"path": "chart.png", "type": "document"}),
                ("mime-type", {"path": "chart.png", "mime_type": "text/plain"}),
            )
        ),
        *(
            pytest.param({"file_paths": [address]}, id=f"web-address-{index}")
            for index, address in enumerate(
                ["https://example.test/a.png", "/api/images/artifacts/abc", "data:image/png;x"]
            )
        ),
        pytest.param(
            {"file_paths": ["file://fileserver.example/share/note.txt"]},
            marks=pytest.mark.skipif(sys.platform == "win32", reason="a UNC path on Windows"),
            id="file-url-of-another-computer",
        ),
        pytest.param({"file_paths": ["shots"]}, id="folder"),
        pytest.param({"file_paths": ["missing.pdf"]}, id="missing-file"),
        pytest.param({"file_paths": ["two-kb.bin"]}, id="kilobytes-over-limit"),
        pytest.param(
            {
                "message": "Pick",
                "file_paths": ["note.txt"],
                "buttons": [[{"label": "OK", "data": "run:ok"}]],
            },
            id="buttons-with-files",
        ),
    ],
)
def test_files_that_cannot_be_sent_are_refused(tmp_path: Path, arguments: dict[str, Any]) -> None:
    tool = channel_send(tmp_path)
    _workspace_files(tool.workspace)
    (tool.workspace / "shots").mkdir()
    (tool.workspace / "missing.pdf.txt").write_bytes(b"x")
    (tool.workspace / "two-kb.bin").write_bytes(bytes(2048))

    result = refused(tool.call(arguments))

    if arguments.get("file_paths") == ["missing.pdf"]:
        assert (tool.workspace / "missing.pdf").as_posix() in result
        assert "missing.pdf.txt" in result
    assert tool.sent() == []


@pytest.mark.skipif(sys.platform == "win32", reason="backslash is a path separator on Windows")
def test_file_names_with_backslashes_are_not_silently_renamed(tmp_path: Path) -> None:
    tool = channel_send(tmp_path)
    name = "a\\b.txt"
    (tool.workspace / name).write_bytes(b"text")
    refused(tool.call({"attachments": [{"path": name, "filename": "b.txt"}]}))
    assert tool.sent() == []
    delivered(tool.call({"attachments": [{"path": name, "filename": name}]}))
    assert tool.sent()[0][3]["files"][0].filename == name


@pytest.mark.parametrize("message", ["MEDIA:C:/my docs/a.png", "MEDIA:", 'MEDIA:"unclosed.png'])
def test_unreadable_media_markers_are_not_sent_as_text(tmp_path: Path, message: str) -> None:
    tool = channel_send(tmp_path)
    refused(tool.call({"message": message}))
    assert tool.sent() == []


@pytest.mark.parametrize("operation", ["stat", "read_bytes"])
@pytest.mark.parametrize(
    "error", [PermissionError("localized reason"), OSError("localized reason"), TimeoutError()]
)
def test_unreadable_files_fail_before_any_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, error: OSError
) -> None:
    tool = channel_send(tmp_path)
    _workspace_files(tool.workspace)
    path = tool.workspace / "note.txt"
    original = getattr(Path, operation)

    def fail_for_file(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail_for_file)
    result = refused(tool.call({"file_paths": ["b.txt", "note.txt"]}))
    assert "localized reason" not in result
    assert path.as_posix() in result
    assert tool.sent() == []


@pytest.mark.parametrize(
    ("file_paths", "code"),
    [(["private.txt"], "permission_denied"), (["private"], "invalid_arguments")],
)
def test_files_the_rules_refuse_are_neither_sent_nor_suggested(
    tmp_path: Path, file_paths: list[str], code: str
) -> None:
    tool = channel_send(tmp_path)
    _workspace_files(tool.workspace)
    (tool.workspace / "private.txt").write_bytes(b"secret")
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"deny": ["Read({workdir}/private.txt)"]}),
        BoundaryContext(workdir=tool.workspace, home=tmp_path / "home", data_dir=tmp_path / "data"),
    )

    envelope = tool.call({"file_paths": file_paths}, permissions=boundary)

    assert envelope["error"]["code"] == code
    # A missing name never points at the refused file beside it.
    assert code == "permission_denied" or "private.txt" not in envelope["error"]["message"]
    assert tool.sent() == []
