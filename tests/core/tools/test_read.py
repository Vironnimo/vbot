"""Tests for the canonical built-in read tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.attachments import AttachmentTooLargeError
from core.model_tasks import SpeechError, SpeechTranscriptionResult
from core.tools import (
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
    ToolContext,
    ToolRegistry,
    is_tool_result_envelope,
    make_read_handler,
    register_read_tool,
)


@dataclass(frozen=True)
class _FakeRecord:
    id: str
    filename: str
    media_type: str


class _FakeAttachmentStore:
    """Records ``store()`` calls; optionally raises to simulate rejection."""

    def __init__(self, *, error: Exception | None = None, media_type: str = "image/png") -> None:
        self._error = error
        self._media_type = media_type
        self.stored: list[tuple[str, bytes]] = []

    def store(self, filename: str, data: bytes) -> _FakeRecord:
        if self._error is not None:
            raise self._error
        self.stored.append((filename, data))
        return _FakeRecord(id="att-123", filename=filename, media_type=self._media_type)


class _FakeSpeech:
    """Returns a fixed transcription; optionally raises a ``SpeechError``."""

    def __init__(self, *, text: str = "transcribed words", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[tuple[bytes, str, str]] = []

    async def transcribe(
        self, audio: bytes, *, filename: str, media_type: str
    ) -> SpeechTranscriptionResult:
        self.calls.append((audio, filename, media_type))
        if self._error is not None:
            raise self._error
        return SpeechTranscriptionResult(text=self._text)


def make_context(
    workspace: Path, tool_name: str = READ_TOOL_NAME, *, cwd: Path | None = None
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        app_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
    )


# The read handler is always async; mypy needs the precise awaitable-returning type
# (the registry's ``ToolHandler`` alias is a sync-or-async union that can't be awaited).
_ReadHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


def make_handler(store: Any = None, speech: Any = None) -> _ReadHandler:
    handler = make_read_handler(store or _FakeAttachmentStore(), speech or _FakeSpeech())
    return cast(_ReadHandler, handler)


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"content"}
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


def test_register_read_tool_exposes_provider_schema_without_description_property() -> None:
    registry = ToolRegistry()

    register_read_tool(
        registry, attachment_store=_FakeAttachmentStore(), speech_service=_FakeSpeech()
    )

    tool = registry.get("read")
    assert tool.name == READ_TOOL_NAME == "read"
    assert tool.parameters == READ_TOOL_PARAMETERS

    definitions = registry.provider_definitions(["read"])
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition) == {"name", "description", "parameters"}
    assert definition["name"] == "read"

    parameters = definition["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["path"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"path", "offset", "limit"}
    assert "description" not in parameters["properties"]


@pytest.mark.asyncio
async def test_read_reads_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\nworkspace\n")

    result = await make_handler()(make_context(workspace), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "hello\nworkspace\n"


@pytest.mark.asyncio
async def test_read_resolves_relative_path_against_cwd_not_workspace(tmp_path: Path) -> None:
    # A same-named file exists in both locations; with cwd set to the repo, the
    # relative path must read the repo copy, not the workspace copy.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"workspace copy\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("notes.txt").write_bytes(b"repo copy\n")

    result = await make_handler()(make_context(workspace, cwd=repo), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "repo copy\n"


@pytest.mark.asyncio
async def test_read_reads_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"absolute\npath\n")

    result = await make_handler()(make_context(workspace), {"path": str(target)})

    data = assert_success_envelope(result)
    assert data["content"] == "absolute\npath\n"


@pytest.mark.asyncio
async def test_read_text_file_does_not_create_attachment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"plain text\n")
    store = _FakeAttachmentStore()

    result = await make_handler(store=store)(make_context(workspace), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "plain text\n"
    assert store.stored == []


@pytest.mark.asyncio
async def test_read_returns_failure_envelope_for_missing_path_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await make_handler()(make_context(workspace), {})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "path" in error["message"]


@pytest.mark.asyncio
async def test_read_returns_failure_envelope_for_unknown_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\n")

    result = await make_handler()(
        make_context(workspace),
        {"path": "notes.txt", "description": "display-only label"},
    )

    error = assert_failure_envelope(result, "invalid_arguments")
    assert "description" in error["message"]


@pytest.mark.asyncio
async def test_read_returns_failure_envelope_for_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await make_handler()(make_context(workspace), {"path": "missing.txt"})

    error = assert_failure_envelope(result, "file_not_found")
    assert "missing.txt" in error["message"]


@pytest.mark.asyncio
async def test_read_returns_failure_envelope_for_directory_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("folder").mkdir()

    result = await make_handler()(make_context(workspace), {"path": "folder"})

    error = assert_failure_envelope(result, "not_a_file")
    assert "folder" in error["message"]


@pytest.mark.asyncio
async def test_read_returns_failure_envelope_for_read_time_filesystem_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"hello\n")

    def raise_permission_error(self: Path) -> bytes:
        raise PermissionError("access denied while reading")

    monkeypatch.setattr(Path, "read_bytes", raise_permission_error)

    result = await make_handler()(make_context(workspace), {"path": "notes.txt"})

    error = assert_failure_envelope(result, "file_read_error")
    assert str(target.resolve()) in error["message"]
    assert "access denied while reading" in error["message"]


@pytest.mark.asyncio
async def test_read_applies_line_offset_and_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\ntwo\nthree\nfour\n")

    result = await make_handler()(
        make_context(workspace), {"path": "notes.txt", "offset": 2, "limit": 2}
    )

    data = assert_success_envelope(result)
    assert data["content"] == "two\nthree\n[Showing lines 2-3 of 4. Use offset=4 to continue.]"


@pytest.mark.asyncio
async def test_read_returns_eof_notice_when_offset_is_past_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\ntwo\n")

    result = await make_handler()(make_context(workspace), {"path": "notes.txt", "offset": 5})

    data = assert_success_envelope(result)
    assert data["content"] == "[Offset 5 is beyond end of file (2 lines). Nothing to show.]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line_control", "message"),
    [
        ({"limit": 0}, "limit must be >= 1"),
        ({"limit": True}, "limit must be a positive integer"),
        ({"limit": 1.5}, "limit must be a positive integer"),
        ({"offset": 0}, "offset must be >= 1"),
        ({"offset": True}, "offset must be a positive integer"),
        ({"offset": 1.5}, "offset must be a positive integer"),
    ],
)
async def test_read_returns_failure_envelope_for_invalid_line_controls(
    tmp_path: Path,
    line_control: dict[str, object],
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\n")

    result = await make_handler()(make_context(workspace), {"path": "notes.txt", **line_control})

    error = assert_failure_envelope(result, "invalid_arguments")
    assert error["message"] == message


@pytest.mark.asyncio
async def test_read_accepts_integer_valued_float_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    handler = make_handler()

    result_float = await handler(make_context(workspace), {"path": "lines.txt", "offset": 2.0})
    result_int = await handler(make_context(workspace), {"path": "lines.txt", "offset": 2})

    assert result_float == result_int


@pytest.mark.asyncio
async def test_read_accepts_integer_valued_float_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    handler = make_handler()

    result_float = await handler(make_context(workspace), {"path": "lines.txt", "limit": 2.0})
    result_int = await handler(make_context(workspace), {"path": "lines.txt", "limit": 2})

    assert result_float == result_int


@pytest.mark.asyncio
async def test_read_default_limit_truncates_large_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lines = "".join(f"line{i}\n" for i in range(1, 2002))
    workspace.joinpath("big.txt").write_text(lines, encoding="utf-8")

    result = await make_handler()(make_context(workspace), {"path": "big.txt"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[Showing lines 1-2000 of 2001." in content
    assert "line2001" not in content


@pytest.mark.asyncio
async def test_read_byte_limit_truncates_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("huge.txt").write_bytes(("x" * 60000 + "\n").encode("utf-8"))

    result = await make_handler()(make_context(workspace), {"path": "huge.txt"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert len(content.encode("utf-8")) <= 50 * 1024 + 500
    assert "Output truncated at 50 KB" in content


@pytest.mark.asyncio
async def test_read_invalid_utf8_uses_replacement_character(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("invalid.txt").write_bytes(b"valid\xfftext")

    result = await make_handler()(make_context(workspace), {"path": "invalid.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "valid�text"


@pytest.mark.asyncio
async def test_read_empty_file_returns_empty_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("empty.txt").write_text("", encoding="utf-8")

    result = await make_handler()(make_context(workspace), {"path": "empty.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == ""


@pytest.mark.asyncio
async def test_read_audio_returns_transcription_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    audio_bytes = b"ID3\x04\x00mp3-frame-data"
    workspace.joinpath("voice.mp3").write_bytes(audio_bytes)
    speech = _FakeSpeech(text="hello from the recording")

    result = await make_handler(speech=speech)(make_context(workspace), {"path": "voice.mp3"})

    data = assert_success_envelope(result)
    assert data["content"] == "[Transcription of voice.mp3 (audio/mpeg)]:\nhello from the recording"
    assert speech.calls == [(audio_bytes, "voice.mp3", "audio/mpeg")]


@pytest.mark.asyncio
async def test_read_audio_maps_speech_error_to_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("voice.mp3").write_bytes(b"ID3\x04\x00mp3-frame-data")
    speech = _FakeSpeech(error=SpeechError("speech-to-text is not configured"))

    result = await make_handler(speech=speech)(make_context(workspace), {"path": "voice.mp3"})

    error = assert_failure_envelope(result, "transcription_failed")
    assert "speech-to-text is not configured" in error["message"]


@pytest.mark.asyncio
async def test_read_audio_empty_transcription_is_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("voice.mp3").write_bytes(b"ID3\x04\x00mp3-frame-data")
    speech = _FakeSpeech(text="   ")

    result = await make_handler(speech=speech)(make_context(workspace), {"path": "voice.mp3"})

    error = assert_failure_envelope(result, "transcription_failed")
    assert "voice.mp3" in error["message"]


@pytest.mark.asyncio
async def test_read_image_stores_attachment_and_emits_read_media_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    workspace.joinpath("diagram.png").write_bytes(image_bytes)
    store = _FakeAttachmentStore(media_type="image/png")

    result = await make_handler(store=store)(make_context(workspace), {"path": "diagram.png"})

    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert store.stored == [("diagram.png", image_bytes)]
    data = result["data"]
    assert isinstance(data, dict)
    assert "diagram.png" in data["content"]
    assert result["artifacts"] == [
        {
            "kind": "read_media",
            "attachment_id": "att-123",
            "filename": "diagram.png",
            "media_type": "image/png",
        }
    ]


@pytest.mark.asyncio
async def test_read_image_maps_attachment_error_to_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    store = _FakeAttachmentStore(error=AttachmentTooLargeError("Attachment size 9 exceeds limit 4"))

    result = await make_handler(store=store)(make_context(workspace), {"path": "diagram.png"})

    error = assert_failure_envelope(result, "attachment_error")
    assert "exceeds limit" in error["message"]


@pytest.mark.asyncio
async def test_read_video_returns_path_note_without_attachment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypisommp4-data")
    store = _FakeAttachmentStore()

    result = await make_handler(store=store)(make_context(workspace), {"path": "clip.mp4"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[Video: clip.mp4 (video/mp4)" in content
    assert "cannot view video" in content
    assert store.stored == []
