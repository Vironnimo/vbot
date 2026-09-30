"""The read Tool: definition, paths, directories, text windows, media, and documents."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import pytest

import core.tools._read_text as read_text_module
import core.tools.read as read_module
import core.tools.read_extract as read_extract_module
from core.model_tasks import SpeechError
from core.sessions import SessionAddress
from core.tools import (
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
    ChangeTracker,
    FileReadState,
    ToolContractError,
    ToolRegistry,
)
from core.tools.file_state import StaleReason
from core.tools.search_files import register_search_files_tool
from tests.core.tools.read_test_support import (
    FakeAttachmentStore,
    FakeSpeech,
    binary_notice,
    content,
    docx_bytes,
    failure,
    ipynb_bytes,
    make_context,
    pdf_bytes,
    read,
    read_registry,
    xlsx_bytes,
)


def test_register_read_tool_exposes_provider_schema_without_description_property() -> None:
    registry = read_registry()

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
    assert parameters["properties"]["offset"]["type"] == "integer"
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in parameters["properties"].values()
    )
    assert "description" not in parameters["properties"]

    ranged_display = registry.display_for_call(
        "read", {"path": "notes.txt", "offset": 170, "limit": 111}
    )
    default_display = registry.display_for_call("read", {"path": "notes.txt"})
    continued_display = registry.display_for_call(
        "read", {"path": "notes.txt", "offset": "170:42", "limit": 10}
    )
    dialect_display = registry.display_for_call(
        "read", {"file_path": "notes.txt", "start_line": 5, "end_line": 9}
    )
    assert ranged_display["facts"] == [{"kind": "line_range", "start": 170, "end": 280}]
    assert default_display["facts"] == []
    assert continued_display["facts"] == [{"kind": "line_range", "start": 170, "end": 179}]
    assert dialect_display["facts"] == [{"kind": "line_range", "start": 5, "end": 9}]
    assert dialect_display["primary"][0]["value"] == "notes.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_repo", "path", "expected"),
    [
        (False, "notes.txt", "1| workspace copy\n"),
        # With a working directory set, a relative path reads its copy, not the workspace's.
        (True, "notes.txt", "1| repo copy\n"),
        (False, "outside.txt", "1| absolute\n2| path\n"),
    ],
)
async def test_paths_resolve_from_the_working_directory_or_are_absolute(
    tmp_path: Path, in_repo: bool, path: str, expected: str
) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    workspace.mkdir()
    repo.mkdir()
    workspace.joinpath("notes.txt").write_bytes(b"workspace copy\n")
    repo.joinpath("notes.txt").write_bytes(b"repo copy\n")
    tmp_path.joinpath("outside.txt").write_bytes(b"absolute\npath\n")
    if path == "outside.txt":
        path = str(tmp_path / path)
    store = FakeAttachmentStore()
    context = make_context(workspace, cwd=repo if in_repo else None)

    result = await read(workspace, {"path": path}, context=context, store=store)

    assert content(result) == expected
    assert store.stored == []


@pytest.mark.asyncio
async def test_read_rejects_unknown_argument_before_reading(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello\n")
    file_state = FileReadState()

    with pytest.raises(ToolContractError, match='"encoding" is not a parameter'):
        await read(tmp_path, {"path": "notes.txt", "encoding": "latin-1"}, file_state=file_state)

    assert file_state.check_stale("session-1", target.resolve()) is StaleReason.NEVER_READ


@pytest.mark.asyncio
async def test_a_missing_file_suggests_ranked_similar_paths(tmp_path: Path) -> None:
    ranked = tmp_path / "ranked"
    ranked.mkdir()
    for name in ["settings.yaml", "settngs.txt", "release-notes.md"]:
        ranked.joinpath(name).write_text(name, encoding="utf-8")
    ranked.joinpath("settings.txt.backup").mkdir()
    many = tmp_path / "many"
    many.mkdir()
    for index in range(8):
        many.joinpath(f"settings-{index}.txt").write_text("candidate", encoding="utf-8")

    for folder in ("tests", "tests/deep", ".hidden", "node_modules"):
        tmp_path.joinpath(folder).mkdir()
        tmp_path.joinpath(folder, "corpus.py").write_text("x", encoding="utf-8")

    message = failure(await read(tmp_path, {"path": "ranked/settings.txt"}), "file_not_found")
    bounded = failure(await read(tmp_path, {"path": "many/settings.txt"}), "file_not_found")
    elsewhere = failure(await read(tmp_path, {"path": "corpus.py"}), "file_not_found")

    # Same stem first, then files or directories beside it; never unrelated names
    # or the absolute path.
    assert message == (
        "File not found: ranked/settings.txt (similar: ranked/settings.yaml, "
        "ranked/settings.txt.backup, ranked/settngs.txt)."
    )
    assert bounded.count("settings-") == 5
    # The same name in other folders, shallow ones first; hidden and dependency
    # folders are not searched.
    assert (
        elsewhere == "File not found: corpus.py (similar: tests/corpus.py, tests/deep/corpus.py)."
    )


@pytest.mark.asyncio
async def test_a_missing_file_without_similar_names_names_the_listing_call(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    tmp_path.joinpath("docs/zeta.md").write_text("z", encoding="utf-8")

    in_existing = await read(tmp_path, {"path": "docs/alpha.txt"})
    in_missing = await read(tmp_path, {"path": "gone/alpha.txt"})

    assert failure(in_existing, "file_not_found") == (
        'File not found: docs/alpha.txt. read(path="docs") lists its directory.'
    )
    assert failure(in_missing, "file_not_found") == (
        "File not found: gone/alpha.txt. Its directory gone does not exist."
    )


@pytest.mark.asyncio
async def test_a_directory_is_listed_and_paged_without_being_stamped(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    (folder / "Sub").mkdir(parents=True)
    for name in ["b.txt", "a.py", ".hidden"]:
        folder.joinpath(name).write_text(name, encoding="utf-8")
    many = tmp_path / "many"
    many.mkdir()
    for index in range(1, 8):
        many.joinpath(f"f{index}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    file_state = FileReadState()

    listing = await read(tmp_path, {"path": "folder"}, file_state=file_state)
    first = await read(tmp_path, {"path": "many", "limit": 3})
    last = await read(tmp_path, {"path": "many", "offset": -2})
    nothing = await read(tmp_path, {"path": "empty"})

    assert content(listing) == "Directory folder/ (4 entries):\n.hidden\na.py\nb.txt\nSub/"
    assert file_state.check_stale("session-1", folder.resolve()) is StaleReason.NEVER_READ
    assert content(first) == (
        "Directory many/ (7 entries):\nf1.txt\nf2.txt\nf3.txt\n"
        "[Showing entries 1-3 of 7. Use offset=4 to continue.]"
    )
    assert content(last) == "Directory many/ (7 entries):\nf6.txt\nf7.txt"
    assert content(nothing) == "empty/ is an empty directory."


@pytest.mark.asyncio
async def test_a_read_time_filesystem_error_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.joinpath("notes.txt").write_bytes(b"hello\n")

    def raise_permission_error(self: Path, *args: object, **kwargs: object) -> Any:
        raise PermissionError("access denied while reading")

    monkeypatch.setattr(Path, "open", raise_permission_error)

    result = await read(tmp_path, {"path": "notes.txt"})

    assert failure(result, "file_read_error") == (
        "Could not read notes.txt: access denied while reading."
    )


_ELEVEN = "".join(f"line{i}\n" for i in range(1, 12))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        # The unpadded gutter numbers every line, blanks included, and rolls from
        # single to multiple digits.
        (
            "code.txt",
            f"start\n\n{_ELEVEN}".encode(),
            "1| start\n2| \n" + "".join(f"{i + 2}| line{i}\n" for i in range(1, 12)),
        ),
        # The separator space tells the gutter from source text starting with a pipe.
        (
            "table.md",
            b"| Variant | Focus |\n|---|---|\n",
            "1| | Variant | Focus |\n2| |---|---|\n",
        ),
        ("invalid.txt", b"valid\xfftext", "1| valid�text"),
        ("bom.txt", b"\xef\xbb\xbfhello\nworld\n", "1| hello\n2| world\n"),
        ("empty.txt", b"", "[empty.txt is empty.]"),
        ("data.bin", b"\x7fELF\x00\x00\x01payload", binary_notice("data.bin")),
        # Text that starts like a media file is still text.
        *[
            ("notes.md", f"{first}\nsecond\n".encode(), f"1| {first}\n2| second\n")
            for first in ["ID3 tags", "OggS notes", "fLaC header", "GIF8 frames", "GIF89a version"]
        ],
    ],
    ids=[
        "numbering",
        "leading_pipe",
        "invalid_utf8",
        "bom",
        "empty",
        "nul_byte",
        *["id3", "ogg", "flac", "gif8", "gif89a"],
    ],
)
async def test_text_files_are_shown_with_a_line_number_gutter(
    tmp_path: Path, name: str, raw: bytes, expected: str
) -> None:
    tmp_path.joinpath(name).write_bytes(raw)
    speech = FakeSpeech()
    context = make_context(tmp_path)

    result = await read(tmp_path, {"path": name}, context=context, speech=speech)

    assert content(result) == expected
    assert speech.calls == []
    assert context.result_media == []


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\f", "\v", "\x1c", "\x85", " ", " "])
async def test_line_numbers_break_only_at_lf_crlf_and_cr_like_search_files(
    tmp_path: Path, separator: str
) -> None:
    raw = f"import os\n{separator}\ndef foo():\r\n    pass\nx = 1\n".encode()
    tmp_path.joinpath("ff.py").write_bytes(raw)
    search = ToolRegistry()
    register_search_files_tool(search)

    whole = content(await read(tmp_path, {"path": "ff.py"}))
    line = content(await read(tmp_path, {"path": "ff.py", "offset": 4, "limit": 1}))
    found = await search.dispatch(make_context(tmp_path, "search_files"), {"args": ["pass"]})

    assert whole == f"1| import os\n2| {separator}\n3| def foo():\n4|     pass\n5| x = 1\n"
    assert line.startswith("4|     pass\n")
    assert found["data"]["content"] == "ff.py:4:    pass"
    assert read_module.render_text_file(raw) == whole


@pytest.mark.asyncio
async def test_streaming_text_matches_byte_renderer_across_chunked_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"\xef\xbb\xbf" + "alpha\r\nbeta\rgamma delta".encode()
    tmp_path.joinpath("line-endings.txt").write_bytes(raw)
    monkeypatch.setattr(read_text_module, "_TEXT_STREAM_CHUNK_CHARACTERS", 2)

    result = await read(tmp_path, {"path": "line-endings.txt"})

    assert content(result) == read_module.render_text_file(raw)


_LONG = "".join(f"line{i}\n" for i in range(1, 2002))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "window", "expected"),
    [
        # Numbers are the file's own, and the hint names the next call.
        (
            "one\ntwo\nthree\nfour\n",
            {"offset": 2, "limit": 2},
            "2| two\n3| three\n[Showing lines 2-3 of 4. Use offset=4 to continue.]",
        ),
        (
            "one\ntwo\n",
            {"offset": 5},
            "[Offset 5 is beyond end of file (2 lines). Nothing to show.]",
        ),
        # Without a limit, a read stops after 2000 lines.
        (
            _LONG,
            {},
            "".join(f"{i}| line{i}\n" for i in range(1, 2001))
            + "[Showing lines 1-2000 of 2001. Use offset=2001 to continue.]",
        ),
    ],
    ids=["offset_and_limit", "past_the_end", "default_limit"],
)
async def test_line_windows_name_the_call_that_continues_them(
    tmp_path: Path, raw: str, window: dict[str, int], expected: str
) -> None:
    tmp_path.joinpath("notes.txt").write_bytes(raw.encode())

    result = await read(tmp_path, {"path": "notes.txt", **window})

    assert content(result) == expected


@pytest.mark.asyncio
async def test_a_line_cut_at_the_byte_limit_continues_at_its_character(tmp_path: Path) -> None:
    source = "x" * 60_000 + "\nsecond\n"
    tmp_path.joinpath("minified.txt").write_bytes(source.encode("utf-8"))

    first = content(await read(tmp_path, {"path": "minified.txt"}))
    match = re.search(r'Use offset="(1:\d+)" to continue', first)
    assert match is not None
    offset = match.group(1)
    character = int(offset.split(":", maxsplit=1)[1])
    assert len(first.encode("utf-8")) <= 50 * 1024 + 500
    assert "Output truncated at 50 KB" in first
    assert first.split("\n\n[", maxsplit=1)[0] == f"1| {source[: character - 1]}"

    continued = content(await read(tmp_path, {"path": "minified.txt", "offset": offset}))

    assert continued == f"{offset}| {source[character - 1 : source.index(chr(10))]}\n2| second\n"


@pytest.mark.asyncio
async def test_read_never_records_change_tracker_stats(tmp_path: Path) -> None:
    tmp_path.joinpath("notes.txt").write_bytes(b"alpha\nbeta\ngamma\n")
    tracker = ChangeTracker()
    context = make_context(tmp_path, change_tracker=tracker)

    content(await read(tmp_path, {"path": "notes.txt"}, context=context))

    run = (SessionAddress(context.project_id, context.agent_id, context.session_id), "run-1")
    assert tracker.peek_run_stats(run) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("notes.txt", b"content\n"),
        ("voice.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"x" * 20),
        ("diagram.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00"),
        ("report.docx", docx_bytes("<w:p><w:r><w:t>Report</w:t></w:r></w:p>")),
        ("data.bin", b"\x7fELF\x00payload"),
    ],
    ids=["text", "audio", "image", "document", "binary"],
)
async def test_a_successful_read_stamps_the_file_for_the_write_guard(
    tmp_path: Path, name: str, raw: bytes
) -> None:
    target = tmp_path / name
    target.write_bytes(raw)
    file_state = FileReadState()

    assert (await read(tmp_path, {"path": name}, file_state=file_state))["ok"] is True

    assert file_state.check_stale("session-1", target.resolve()) is None
    assert file_state.check_stale("session-2", target.resolve()) is StaleReason.NEVER_READ


@pytest.mark.asyncio
async def test_read_stamp_predates_bytes_so_a_concurrent_write_forces_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"before\n")
    file_state = FileReadState()
    render = read_module.render_text_path

    def render_after_external_write(resolved: Path, arguments: dict[str, Any]) -> str:
        target.write_bytes(b"written during the read\n")
        return render(resolved, arguments)

    monkeypatch.setattr(read_module, "render_text_path", render_after_external_write)

    result = await read(tmp_path, {"path": "notes.txt"}, file_state=file_state)

    assert result["ok"] is True
    assert file_state.check_stale("session-1", target.resolve()) is StaleReason.MODIFIED


_AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"x" * 20
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
_MP4 = b"\x00\x00\x00\x18ftypisommp4-data"


@pytest.mark.asyncio
async def test_an_image_passes_its_pixels_in_memory_without_creating_files(tmp_path: Path) -> None:
    tmp_path.joinpath("diagram.png").write_bytes(_PNG)
    store = FakeAttachmentStore()
    context = make_context(tmp_path)
    before = set(tmp_path.rglob("*"))

    result = await read(tmp_path, {"path": "diagram.png"}, context=context, store=store)

    assert "diagram.png" in content(result)
    assert store.stored == []
    assert set(tmp_path.rglob("*")) == before
    assert base64.b64decode(context.result_media[0]["base64"]) == _PNG
    assert context.presentation_media == [
        {"path": str(tmp_path / "diagram.png"), "kind": "image", "filename": "diagram.png"}
    ]


@pytest.mark.asyncio
async def test_audio_is_transcribed(tmp_path: Path) -> None:
    tmp_path.joinpath("voice.mp3").write_bytes(_AUDIO)
    speech = FakeSpeech(text="hello from the recording")

    result = await read(tmp_path, {"path": "voice.mp3"}, speech=speech)

    assert content(result) == "[Transcription of voice.mp3 (audio/mpeg)]:\nhello from the recording"
    assert speech.calls == [(_AUDIO, "voice.mp3", "audio/mpeg")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "services", "code", "message"),
    [
        ("voice.mp3", _AUDIO, {"speech_max_size_bytes": 8}, "audio_too_large", "exceeds limit 8"),
        (
            "voice.mp3",
            _AUDIO,
            {"speech": FakeSpeech(error=SpeechError("speech-to-text is not configured"))},
            "transcription_failed",
            "speech-to-text is not configured",
        ),
        (
            "voice.mp3",
            _AUDIO,
            {"speech": FakeSpeech(text="   ")},
            "transcription_failed",
            "voice.mp3",
        ),
        (
            "pic.png",
            _PNG,
            {"store": FakeAttachmentStore(max_size_bytes=4)},
            "attachment_error",
            "exceeds limit",
        ),
    ],
    ids=["audio_too_large", "speech_error", "empty_transcription", "image_too_large"],
)
async def test_failed_read_does_not_stamp_the_file(
    tmp_path: Path, name: str, raw: bytes, services: dict[str, Any], code: str, message: str
) -> None:
    target = tmp_path / name
    target.write_bytes(raw)
    file_state = FileReadState()
    services = {"speech": FakeSpeech(), **services}

    result = await read(tmp_path, {"path": name}, file_state=file_state, **services)

    assert message in failure(result, code)
    assert file_state.check_stale("session-1", target.resolve()) is StaleReason.NEVER_READ
    if code == "audio_too_large":
        assert services["speech"].calls == []


_DOCX = docx_bytes(
    "<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> World</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Line</w:t><w:tab/><w:t>Two</w:t></w:r></w:p>"
)
_SHEET = "Name\tAge\nBob\t42"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        # A rendering, not editable source: no line number gutter. Extensions match
        # in any case.
        ("report.DOCX", _DOCX, "(Word document)]:\nHello World\nLine\tTwo"),
        (
            "nb.ipynb",
            ipynb_bytes(),
            "(Jupyter notebook)]:\n# Cell 1 [markdown]\n# Title\nintro\n\n# Cell 2 [code]\n"
            "print('hi')",
        ),
        (
            "paper.pdf",
            pdf_bytes(["Hello PDF", "Second line"]),
            "(PDF document)]:\n# Page 1\nHello PDF\nSecond line",
        ),
        # A file without an extension is recognized by its content.
        ("download", pdf_bytes(["Hello PDF"]), "(PDF document)]:\n# Page 1\nHello PDF"),
        # A scanned PDF has no text layer.
        ("scan.pdf", pdf_bytes([]), "(PDF document)]:\n(no extractable text)"),
        # Worksheet relationship targets resolve relative to the workbook part or the root.
        *[
            (
                "people.xlsx",
                xlsx_bytes(worksheet_target=target),
                f"(Excel spreadsheet)]:\n# Sheet: People\n{_SHEET}",
            )
            for target in [
                "worksheets/sheet1.xml",
                "/xl/worksheets/sheet1.xml",
                "./worksheets/sheet1.xml",
                "worksheets/../worksheets/sheet1.xml",
                "../xl/worksheets/sheet1.xml",
            ]
        ],
        # Without a workbook part, sheets are named after their files.
        (
            "people.xlsx",
            xlsx_bytes(worksheet_target=None),
            f"(Excel spreadsheet)]:\n# Sheet: sheet1\n{_SHEET}",
        ),
    ],
    ids=[
        "docx",
        "ipynb",
        "pdf",
        "pdf_without_extension",
        "scanned_pdf",
        *[f"xlsx_target_{index}" for index in range(5)],
        "xlsx_without_workbook",
    ],
)
async def test_documents_are_read_as_their_extracted_text(
    tmp_path: Path, name: str, raw: bytes, expected: str
) -> None:
    tmp_path.joinpath(name).write_bytes(raw)

    result = await read(tmp_path, {"path": name})

    assert content(result) == f"[Extracted text from {name} {expected}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("clip.mp4", _MP4, "[clip.mp4 is a video (video/mp4); this model cannot view video.]"),
        # A document the extractor cannot parse falls back to the ordinary file checks.
        ("broken.docx", b"PK\x03\x04 not really a zip \x00 body", binary_notice("broken.docx")),
        ("broken.pdf", b"%PDF-1.4 not really a pdf \x00 body", binary_notice("broken.pdf")),
        ("broken.ipynb", b"{not valid json", "1| {not valid json"),
    ],
    ids=["video", "broken_docx", "broken_pdf", "broken_ipynb"],
)
async def test_files_without_a_readable_form_get_a_note(
    tmp_path: Path, name: str, raw: bytes, expected: str
) -> None:
    tmp_path.joinpath(name).write_bytes(raw)
    store = FakeAttachmentStore()

    result = await read(tmp_path, {"path": name}, store=store)

    assert content(result) == expected
    assert store.stored == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw", "headroom"),
    [
        # The file itself exceeds the budget: rejected before it is read in full.
        ("large.docx", docx_bytes("<w:p><w:r><w:t>" + "x" * 512 + "</w:t></w:r></w:p>"), None),
        # Small files whose content expands beyond the budget during extraction.
        ("packed.docx", docx_bytes("<w:p><w:r><w:t>" + "x" * 8192 + "</w:t></w:r></w:p>"), 64),
        ("packed.pdf", pdf_bytes(["x" * 4096], compressed=True), 64),
    ],
    ids=["docx_file", "docx_content", "pdf_stream"],
)
async def test_documents_beyond_the_extraction_budget_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, raw: bytes, headroom: int | None
) -> None:
    tmp_path.joinpath(name).write_bytes(raw)
    budget = 128 if headroom is None else len(raw) + headroom
    monkeypatch.setattr(read_extract_module, "_MAX_DOCUMENT_EXTRACTED_BYTES", budget)
    if headroom is None:

        def unexpected_extract(_data: bytes, _kind: str) -> str:
            raise AssertionError("oversized document was materialized before preflight")

        monkeypatch.setattr(read_module, "extract_document_text", unexpected_extract)

    result = await read(tmp_path, {"path": name})

    assert "128 MB extraction limit" in failure(result, "document_too_large")


@pytest.mark.asyncio
async def test_text_binary_and_video_reads_never_load_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.joinpath("large.txt").write_text("first\n" + "x" * 200_000, encoding="utf-8")
    tmp_path.joinpath("data.bin").write_bytes(b"\x7fELF\x00payload")
    tmp_path.joinpath("clip.mp4").write_bytes(_MP4)

    def unexpected_full_read(_resolved: Path, _max_bytes: int) -> bytes:
        raise AssertionError("notice/text branch attempted a full-file read")

    monkeypatch.setattr(read_module, "_read_file_bytes_with_limit", unexpected_full_read)

    text = content(await read(tmp_path, {"path": "large.txt", "limit": 1}))
    binary = content(await read(tmp_path, {"path": "data.bin"}))
    video = content(await read(tmp_path, {"path": "clip.mp4"}))

    assert text.startswith("1| first")
    assert text.endswith("[Showing lines 1-1 of 2. Use offset=2 to continue.]")
    assert binary == binary_notice("data.bin")
    assert video.startswith("[clip.mp4 is a video")
