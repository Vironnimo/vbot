"""Shared fakes, dispatch helpers, and document builders for the read Tool tests."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from core.attachments import AttachmentTooLargeError
from core.model_tasks import SpeechTranscriptionResult
from core.tools import (
    READ_TOOL_NAME,
    ChangeTracker,
    FileReadState,
    ToolContext,
    ToolRegistry,
    is_tool_result_envelope,
    register_read_tool,
)
from core.tools.model_names import SHELL_MODEL_NAME

SPEECH_LIMIT = 20_971_520


@dataclass(frozen=True)
class _Record:
    id: str
    filename: str
    media_type: str


class FakeAttachmentStore:
    """Records ``store()`` calls and enforces a size limit like the real store."""

    def __init__(self, *, max_size_bytes: int = 20_971_520) -> None:
        self.max_size_bytes = max_size_bytes
        self.stored: list[tuple[str, bytes]] = []

    def ensure_within_limit(self, reported_size_bytes: int | None) -> None:
        if reported_size_bytes is not None and reported_size_bytes > self.max_size_bytes:
            raise AttachmentTooLargeError(
                f"Attachment size {reported_size_bytes} exceeds limit {self.max_size_bytes}"
            )

    def store(self, filename: str, data: bytes) -> _Record:
        self.stored.append((filename, data))
        return _Record(id="att-123", filename=filename, media_type="image/png")


class FakeSpeech:
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
    workspace: Path,
    tool_name: str = READ_TOOL_NAME,
    *,
    cwd: Path | None = None,
    change_tracker: ChangeTracker | None = None,
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
        change_tracker=change_tracker,
    )


def read_registry(
    *,
    store: Any = None,
    speech: Any = None,
    file_state: FileReadState | None = None,
    speech_max_size_bytes: int = SPEECH_LIMIT,
) -> ToolRegistry:
    registry = ToolRegistry()
    register_read_tool(
        registry,
        attachment_store=store or FakeAttachmentStore(),
        speech_service=speech or FakeSpeech(),
        file_state=file_state or FileReadState(),
        speech_max_size_bytes=speech_max_size_bytes,
    )
    return registry


async def read(
    root: Path,
    arguments: dict[str, Any],
    *,
    context: ToolContext | None = None,
    **services: Any,
) -> dict[str, Any]:
    """Dispatch one read call as Chat does: normalizer, contract, then handler."""
    result = await read_registry(**services).dispatch(context or make_context(root), arguments)
    assert is_tool_result_envelope(result) is True
    assert result["artifacts"] == []
    return result


def content(result: dict[str, Any]) -> str:
    assert result["ok"] is True, result
    assert result["error"] is None
    assert set(result["data"]) == {"content"}
    return str(result["data"]["content"])


def failure(result: dict[str, Any], code: str) -> str:
    assert result["ok"] is False, result
    assert result["data"] is None
    assert result["error"]["code"] == code
    assert result["error"]["message"]
    return str(result["error"]["message"])


def binary_notice(label: str) -> str:
    return (
        f"[{label} is a binary file, so it is not shown as text. If {SHELL_MODEL_NAME} is "
        "available, a command for this file type can inspect it.]"
    )


def docx_bytes(body_xml: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body_xml}</w:body></w:document>",
        )
    return buffer.getvalue()


def xlsx_bytes(*, worksheet_target: str | None = "worksheets/sheet1.xml") -> bytes:
    """A one-sheet workbook; ``worksheet_target=None`` omits the workbook parts."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{main}"><si><t>Name</t></si><si><t>Age</t></si></sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="str"><v>Bob</v></c><c r="B2"><v>42</v></c></row>'
            "</sheetData></worksheet>",
        )
        if worksheet_target is not None:
            archive.writestr(
                "xl/workbook.xml",
                f'<workbook xmlns="{main}" xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships"><sheets>'
                '<sheet name="People" sheetId="1" r:id="rId1"/></sheets></workbook>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                f'relationships"><Relationship Id="rId1" Target="{worksheet_target}"/>'
                "</Relationships>",
            )
    return buffer.getvalue()


def ipynb_bytes() -> bytes:
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Title\n", "intro"]},
            {"cell_type": "code", "source": "print('hi')"},
        ]
    }
    return json.dumps(notebook).encode("utf-8")


def pdf_bytes(lines: list[str], *, compressed: bool = False) -> bytes:
    """A minimal single-page PDF drawing ``lines``; no lines means no text layer.

    Cross-reference offsets come from the real byte layout so pypdf reads the file
    directly. ``compressed`` Flate-encodes the page content stream.
    """
    operators = b"BT /F1 24 Tf 72 720 Td "
    for line in lines:
        operators += b"(" + line.encode("latin-1") + b") Tj 0 -28 Td "
    operators += b"ET"
    stream = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(operators), operators)
    if compressed:
        packed = zlib.compress(operators)
        stream = b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (
            len(packed),
            packed,
        )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        stream,
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
