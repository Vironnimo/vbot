"""Tests for the canonical built-in read tool."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from zipfile import ZipFile

import pytest

import core.tools.read as read_module
import core.tools.read_extract as read_extract_module
from core.attachments import AttachmentTooLargeError
from core.model_tasks import SpeechError, SpeechTranscriptionResult
from core.tools import (
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
    FileReadState,
    ToolContext,
    ToolRegistry,
    is_tool_result_envelope,
    make_read_handler,
    register_read_tool,
)
from core.utils.paths import model_path


@dataclass(frozen=True)
class _FakeRecord:
    id: str
    filename: str
    media_type: str


class _FakeAttachmentStore:
    """Records ``store()`` calls; optionally raises to simulate rejection."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        media_type: str = "image/png",
        max_size_bytes: int = 20_971_520,
    ) -> None:
        self._error = error
        self._media_type = media_type
        self.max_size_bytes = max_size_bytes
        self.stored: list[tuple[str, bytes]] = []

    def ensure_within_limit(self, reported_size_bytes: int | None) -> None:
        if isinstance(self._error, AttachmentTooLargeError):
            raise self._error
        if reported_size_bytes is not None and reported_size_bytes > self.max_size_bytes:
            raise AttachmentTooLargeError(
                f"Attachment size {reported_size_bytes} exceeds limit {self.max_size_bytes}"
            )

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
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
    )


# The read handler is always async; mypy needs the precise awaitable-returning type
# (the registry's ``ToolHandler`` alias is a sync-or-async union that can't be awaited).
_ReadHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


def make_handler(
    store: Any = None,
    speech: Any = None,
    file_state: FileReadState | None = None,
    *,
    speech_max_size_bytes: int = 20_971_520,
) -> _ReadHandler:
    handler = make_read_handler(
        store or _FakeAttachmentStore(),
        speech or _FakeSpeech(),
        file_state or FileReadState(),
        speech_max_size_bytes=speech_max_size_bytes,
    )
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
        registry,
        attachment_store=_FakeAttachmentStore(),
        speech_service=_FakeSpeech(),
        file_state=FileReadState(),
        speech_max_size_bytes=20_971_520,
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
    assert "additionalProperties" not in parameters
    assert set(parameters["properties"]) == {"path", "offset", "limit"}
    assert parameters["properties"]["offset"]["oneOf"] == [
        {"type": "integer", "minimum": 1},
        {
            "type": "string",
            "pattern": r"^[1-9][0-9]*:[1-9][0-9]*$",
        },
    ]
    assert "line:character" in parameters["properties"]["offset"]["description"]
    assert "default 2000" in parameters["properties"]["limit"]["description"]
    assert "description" not in parameters["properties"]


@pytest.mark.asyncio
async def test_read_reads_relative_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"hello\nworkspace\n")

    result = await make_handler()(make_context(workspace), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "1| hello\n2| workspace\n"


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
    assert data["content"] == "1| repo copy\n"


@pytest.mark.asyncio
async def test_read_reads_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"absolute\npath\n")

    result = await make_handler()(make_context(workspace), {"path": str(target)})

    data = assert_success_envelope(result)
    assert data["content"] == "1| absolute\n2| path\n"


@pytest.mark.asyncio
async def test_read_text_file_does_not_create_attachment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"plain text\n")
    store = _FakeAttachmentStore()

    result = await make_handler(store=store)(make_context(workspace), {"path": "notes.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == "1| plain text\n"
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
async def test_read_suggests_ranked_similar_files_for_missing_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    same_stem = workspace / "settings.yaml"
    same_stem.write_text("yaml", encoding="utf-8")
    typo = workspace / "settngs.txt"
    typo.write_text("typo", encoding="utf-8")
    unrelated = workspace / "release-notes.md"
    unrelated.write_text("notes", encoding="utf-8")
    workspace.joinpath("settings.txt.backup").mkdir()

    result = await make_handler()(make_context(workspace), {"path": "settings.txt"})

    error = assert_failure_envelope(result, "file_not_found")
    message = error["message"]
    assert "Similar files:" in message
    assert model_path(same_stem) in message
    assert model_path(typo) in message
    assert model_path(unrelated) not in message
    assert "settings.txt.backup" not in message
    assert message.index(model_path(same_stem)) < message.index(model_path(typo))


@pytest.mark.asyncio
async def test_read_bounds_similar_file_suggestions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(8):
        workspace.joinpath(f"settings-{index}.txt").write_text("candidate", encoding="utf-8")

    result = await make_handler()(make_context(workspace), {"path": "settings.txt"})

    error = assert_failure_envelope(result, "file_not_found")
    suggestions = [line for line in error["message"].splitlines() if line.startswith("- ")]
    assert len(suggestions) == 5


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

    def raise_permission_error(self: Path, *args: object, **kwargs: object) -> Any:
        raise PermissionError("access denied while reading")

    monkeypatch.setattr(Path, "open", raise_permission_error)

    result = await make_handler()(make_context(workspace), {"path": "notes.txt"})

    error = assert_failure_envelope(result, "file_read_error")
    assert model_path(target.resolve()) in error["message"]
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
    assert data["content"] == "2| two\n3| three\n[Showing lines 2-3. Use offset=4 to continue.]"


@pytest.mark.asyncio
async def test_read_numbers_lines_compactly_including_blanks_and_multi_digit(
    tmp_path: Path,
) -> None:
    # The gutter is unpadded ``N| ``, numbers every line (blanks included), and
    # rolls cleanly from single- to multi-digit. Its separator space makes source
    # punctuation visually distinct from the gutter.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = "".join(f"line{i}\n" for i in range(1, 12))  # 11 lines, no trailing blank
    # write_bytes (not write_text) so LF survives on Windows for exact assertions.
    workspace.joinpath("code.txt").write_bytes(f"start\n\n{body}".encode())

    result = await make_handler()(make_context(workspace), {"path": "code.txt"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    lines = content.split("\n")
    assert lines[0] == "1| start"
    assert lines[1] == "2| "  # a blank source line keeps its number and separator
    assert lines[2] == "3| line1"
    assert "13| line11" in lines  # single- to multi-digit transition is seamless


@pytest.mark.asyncio
async def test_read_separator_distinguishes_gutter_from_leading_pipe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("table.md").write_bytes(
        b"| Variant | Focus |\n|---|---|\n| lite | iteration |\n"
    )

    result = await make_handler()(make_context(workspace), {"path": "table.md"})

    data = assert_success_envelope(result)
    assert data["content"] == ("1| | Variant | Focus |\n2| |---|---|\n3| | lite | iteration |\n")


@pytest.mark.asyncio
async def test_read_line_gutter_uses_file_absolute_numbers_with_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"a\nb\nc\nd\ne\n")

    result = await make_handler()(
        make_context(workspace), {"path": "notes.txt", "offset": 3, "limit": 2}
    )

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    # Numbering reflects the true file position, not the page position.
    assert content.startswith("3| c\n4| d\n")


@pytest.mark.asyncio
async def test_read_accepts_in_line_offset_without_advertising_another_parameter(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"one\nsecond\nthird\n")

    result = await make_handler()(make_context(workspace), {"path": "notes.txt", "offset": "2:3"})

    data = assert_success_envelope(result)
    assert data["content"] == "2:3| cond\n3| third\n"


@pytest.mark.asyncio
async def test_read_emits_and_accepts_in_line_continuation_after_byte_truncation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = "x" * 60_000 + "\nsecond\n"
    workspace.joinpath("minified.txt").write_bytes(source.encode("utf-8"))
    handler = make_handler()

    first = await handler(make_context(workspace), {"path": "minified.txt"})
    first_content = assert_success_envelope(first)["content"]
    assert isinstance(first_content, str)
    match = re.search(r"Use offset=(1:\d+) to continue", first_content)
    assert match is not None
    continuation_offset = match.group(1)
    continuation_character = int(continuation_offset.split(":", maxsplit=1)[1])
    first_visible = first_content.split("\n\n[Showing", maxsplit=1)[0]
    assert first_visible == f"1| {source[: continuation_character - 1]}"

    continued = await handler(
        make_context(workspace),
        {"path": "minified.txt", "offset": continuation_offset},
    )
    continued_content = assert_success_envelope(continued)["content"]
    assert isinstance(continued_content, str)
    remaining_first_line = source[continuation_character - 1 : source.index("\n")]
    assert continued_content == f"{continuation_offset}| {remaining_first_line}\n2| second\n"


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
        ({"limit": True}, "limit must be an integer"),
        ({"limit": 1.5}, "limit must be an integer"),
        ({"offset": 0}, "offset must be >= 1"),
        ({"offset": True}, "offset must be an integer"),
        ({"offset": 1.5}, "offset must be an integer"),
        ({"offset": "2:0"}, "offset character must be >= 1"),
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
async def test_read_rejects_integer_valued_float_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    handler = make_handler()

    result_float = await handler(make_context(workspace), {"path": "lines.txt", "offset": 2.0})
    error = assert_failure_envelope(result_float, "invalid_arguments")
    assert error["message"] == "offset must be an integer"


@pytest.mark.asyncio
async def test_read_rejects_integer_valued_float_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    handler = make_handler()

    result_float = await handler(make_context(workspace), {"path": "lines.txt", "limit": 2.0})
    error = assert_failure_envelope(result_float, "invalid_arguments")
    assert error["message"] == "limit must be an integer"


@pytest.mark.asyncio
async def test_read_rejects_string_encoded_offset_and_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    handler = make_handler()

    result_string = await handler(
        make_context(workspace), {"path": "lines.txt", "offset": "2", "limit": "1"}
    )
    error = assert_failure_envelope(result_string, "invalid_arguments")
    assert error["message"] == "offset must be an integer"


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
    assert "[Showing lines 1-2000. Use offset=2001 to continue.]" in content
    assert "of 2001" not in content
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
    assert data["content"] == "1| valid�text"


@pytest.mark.asyncio
async def test_read_strips_utf8_bom(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("bom.txt").write_bytes(b"\xef\xbb\xbfhello\nworld\n")

    result = await make_handler()(make_context(workspace), {"path": "bom.txt"})

    data = assert_success_envelope(result)
    # The BOM is stripped, so line 1 has no phantom leading character.
    assert data["content"] == "1| hello\n2| world\n"


@pytest.mark.asyncio
async def test_streaming_text_matches_byte_renderer_across_chunked_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    raw = b"\xef\xbb\xbf" + "alpha\r\nbeta\rgamma\u2028delta".encode()
    workspace.joinpath("line-endings.txt").write_bytes(raw)
    monkeypatch.setattr(read_module, "_TEXT_STREAM_CHUNK_CHARACTERS", 2)

    result = await make_handler()(make_context(workspace), {"path": "line-endings.txt"})

    data = assert_success_envelope(result)
    assert data["content"] == read_module.render_text_file(raw)


@pytest.mark.asyncio
async def test_read_returns_binary_notice_for_nul_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Bytes that decode as valid UTF-8 but contain an embedded NUL — still binary.
    workspace.joinpath("data.bin").write_bytes(b"\x7fELF\x00\x00\x01payload")

    result = await make_handler()(make_context(workspace), {"path": "data.bin"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary file" in content
    assert "data.bin" in content


@pytest.mark.asyncio
async def test_read_text_binary_and_video_paths_never_use_full_file_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("large.txt").write_text(
        "first\n" + "x" * 200_000,
        encoding="utf-8",
    )
    workspace.joinpath("data.bin").write_bytes(b"\x7fELF\x00payload")
    workspace.joinpath("clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypisommp4-data")

    def unexpected_full_read(_resolved: Path, _max_bytes: int) -> bytes:
        raise AssertionError("notice/text branch attempted a full-file read")

    monkeypatch.setattr(read_module, "_read_file_bytes_with_limit", unexpected_full_read)
    handler = make_handler()

    text_result = await handler(make_context(workspace), {"path": "large.txt", "limit": 1})
    binary_result = await handler(make_context(workspace), {"path": "data.bin"})
    video_result = await handler(make_context(workspace), {"path": "clip.mp4"})

    text_content = str(assert_success_envelope(text_result)["content"])
    assert text_content.startswith("1| first")
    assert text_content.endswith("[Showing lines 1-1. Use offset=2 to continue.]")
    assert "Binary file" in str(assert_success_envelope(binary_result)["content"])
    assert "Video: clip.mp4" in str(assert_success_envelope(video_result)["content"])


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
async def test_read_audio_rejects_oversized_file_before_transcription(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("voice.mp3").write_bytes(b"ID3" + b"x" * 20)
    speech = _FakeSpeech()

    result = await make_handler(speech=speech, speech_max_size_bytes=8)(
        make_context(workspace), {"path": "voice.mp3"}
    )

    error = assert_failure_envelope(result, "audio_too_large")
    assert "exceeds limit 8" in error["message"]
    assert speech.calls == []


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
    store = _FakeAttachmentStore(max_size_bytes=4)

    result = await make_handler(store=store)(make_context(workspace), {"path": "diagram.png"})

    error = assert_failure_envelope(result, "attachment_error")
    assert "exceeds limit" in error["message"]
    assert store.stored == []


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


@pytest.mark.asyncio
async def test_read_extracts_docx_as_text_without_gutter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document_xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Report body</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(workspace / "report.docx", "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    result = await make_handler()(make_context(workspace), {"path": "report.docx"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert content == "[Extracted text from report.docx (Word document)]:\nReport body"
    # A rendering, not editable source: no `N|` gutter.
    assert "1|" not in content


@pytest.mark.asyncio
async def test_read_reports_document_extraction_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document_xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>" + "x" * 512 + "</w:t></w:r></w:p></w:body></w:document>"
    )
    target = workspace / "large.docx"
    with ZipFile(target, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    monkeypatch.setattr(read_extract_module, "_MAX_DOCUMENT_EXTRACTED_BYTES", 128)

    def unexpected_extract(_data: bytes, _kind: str) -> str:
        raise AssertionError("oversized document was materialized before preflight")

    monkeypatch.setattr(read_module, "extract_document_text", unexpected_extract)

    result = await make_handler()(make_context(workspace), {"path": "large.docx"})

    error = assert_failure_envelope(result, "document_too_large")
    assert "128 MB extraction limit" in error["message"]


@pytest.mark.asyncio
async def test_read_extracts_ipynb_instead_of_dumping_raw_json(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    notebook = {"cells": [{"cell_type": "code", "source": "print('hi')"}]}
    workspace.joinpath("nb.ipynb").write_text(json.dumps(notebook), encoding="utf-8")

    result = await make_handler()(make_context(workspace), {"path": "nb.ipynb"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert content.startswith("[Extracted text from nb.ipynb (Jupyter notebook)]:")
    assert "# Cell 1 [code]\nprint('hi')" in content
    assert '"cells"' not in content


@pytest.mark.asyncio
async def test_read_malformed_docx_falls_back_to_binary_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A .docx that is not a valid zip: extraction fails, binary check takes over.
    workspace.joinpath("broken.docx").write_bytes(b"PK\x03\x04 not really a zip \x00 body")

    result = await make_handler()(make_context(workspace), {"path": "broken.docx"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[Binary file: broken.docx" in content


def _minimal_pdf(lines: list[str]) -> bytes:
    """Build a minimal single-page PDF drawing ``lines`` (empty → no text layer)."""
    operators = b"BT /F1 24 Tf 72 720 Td "
    for line in lines:
        operators += b"(" + line.encode("latin-1") + b") Tj 0 -28 Td "
    operators += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(operators), operators),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref_position = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    pdf += b"startxref\n%d\n%%%%EOF" % xref_position
    return bytes(pdf)


@pytest.mark.asyncio
async def test_read_extracts_pdf_as_text_with_page_headers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("paper.pdf").write_bytes(_minimal_pdf(["Hello PDF"]))

    result = await make_handler()(make_context(workspace), {"path": "paper.pdf"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert content.startswith("[Extracted text from paper.pdf (PDF document)]:")
    assert "# Page 1" in content
    assert "Hello PDF" in content
    # A rendering, not editable source: no `N|` gutter.
    assert "1|" not in content


@pytest.mark.asyncio
async def test_read_scanned_pdf_without_text_layer_reports_no_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("scan.pdf").write_bytes(_minimal_pdf([]))

    result = await make_handler()(make_context(workspace), {"path": "scan.pdf"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert content == "[Extracted text from scan.pdf (PDF document)]:\n(no extractable text)"


@pytest.mark.asyncio
async def test_read_malformed_pdf_falls_back_to_binary_notice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A .pdf that pypdf cannot parse: extraction fails, binary check takes over.
    workspace.joinpath("broken.pdf").write_bytes(b"%PDF-1.4 not really a pdf \x00 body")

    result = await make_handler()(make_context(workspace), {"path": "broken.pdf"})

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[Binary file: broken.pdf" in content


@pytest.mark.asyncio
async def test_read_records_stamp_so_write_edit_guard_passes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_bytes(b"content\n")
    file_state = FileReadState()

    await make_handler(file_state=file_state)(make_context(workspace), {"path": "notes.txt"})

    # The read stamped the file for this session, so the guard no longer flags it.
    assert file_state.check_stale("session-1", target.resolve()) is None
    # Sanity: a registry that never saw the read would block the write.
    assert FileReadState().check_stale("session-1", target.resolve()) is not None
