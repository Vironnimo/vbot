"""Web fetch: content behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import core.tools.read_extract as read_extract_module
import core.tools.web_fetch as web_fetch_module
from core.attachments import AttachmentTooLargeError
from core.tools.tools import is_tool_result_envelope
from core.tools.web_fetch import _FetchResult, extract_content
from tests.core.tools.web_fetch_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    install_http_get,
    make_context,
    make_result,
    web_fetch_arguments,
    web_fetch_handler,
)
from tests.core.tools.web_fetch_helpers import (
    stub_dns_resolution as stub_dns_resolution,
)
from tests.core.tools.web_fetch_helpers import (
    stub_http_session as stub_http_session,
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
        return _FakeRecord(id="att-web-1", filename=filename, media_type=self._media_type)


_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


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


def _minimal_docx(text: str) -> bytes:
    """Build a docx whose ``[Content_Types].xml`` makes it sniff as a Word file."""
    from io import BytesIO
    from zipfile import ZipFile

    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, "markdown"])
async def test_web_fetch_handler_html_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str | None,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/page"
    html = """
    <html>
      <head><title>Example Title</title></head>
      <body>
        <h1>Hello</h1>
        <p>World <a href="/docs">Docs</a></p>
      </body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url, output))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Hello" in content
    assert "World" in content
    assert "[Docs](https://example.com/docs)" in content
    assert "<h1>" not in content
    assert "<p>" not in content


@pytest.mark.asyncio
async def test_web_fetch_handler_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/raw"
    html = "<html><body><h1>Raw Heading</h1></body></html>"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "text/html"}, text=html, url=url
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments(url, "raw"),
    )

    data = assert_success_envelope(result)
    assert data["content"] == html


@pytest.mark.asyncio
async def test_web_fetch_handler_text_output_removes_link_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page_url = "https://example.com/links"
    link_url = "https://target.example/resource"
    html = f"""
    <html>
      <body>
        <p>Read <a href="{link_url}">Visible Link</a> now.</p>
      </body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "text/html"}, text=html, url=page_url
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments(page_url, "text"),
    )

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Visible Link" in content
    assert link_url not in content


@pytest.mark.asyncio
async def test_web_fetch_handler_image_url_stores_attachment_and_emits_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/photo.png"
    store = _FakeAttachmentStore(media_type="image/png")

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "image/png"},
            content=_PNG_BYTES,
            url=url,
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments(url),
        attachment_store=store,
    )

    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    data = result["data"]
    assert isinstance(data, dict)
    assert "photo.png" in data["content"]
    assert result["artifacts"] == [
        {
            "kind": "read_media",
            "attachment_id": "att-web-1",
            "filename": "photo.png",
            "media_type": "image/png",
        }
    ]
    # The exact fetched bytes were handed to the store under the URL's filename.
    assert store.stored == [("photo.png", _PNG_BYTES)]


@pytest.mark.asyncio
async def test_web_fetch_handler_image_url_shown_with_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/photo.png"
    store = _FakeAttachmentStore(media_type="image/png")

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "image/png"}, content=_PNG_BYTES, url=url
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace), web_fetch_arguments(url, "raw"), attachment_store=store
    )

    assert result["ok"] is True
    artifacts = result["artifacts"]
    assert isinstance(artifacts, list)
    assert artifacts[0]["kind"] == "read_media"


@pytest.mark.asyncio
async def test_web_fetch_handler_image_attachment_error_maps_to_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/huge.png"
    store = _FakeAttachmentStore(
        error=AttachmentTooLargeError("Attachment size 99 exceeds limit 4")
    )

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "image/png"}, content=_PNG_BYTES, url=url
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments(url),
        attachment_store=store,
    )

    error = assert_failure_envelope(result, "attachment_error")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_handler_image_without_store_returns_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/photo.png"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "image/png"}, content=_PNG_BYTES, url=url
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "could not be loaded" in content


@pytest.mark.asyncio
async def test_web_fetch_handler_binary_content_returns_notice_not_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/installer.exe"
    exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00garbage\x00bytes"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/octet-stream"},
            content=exe_bytes,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary content" in content
    assert "application/octet-stream" in content
    # The decoded binary body is never surfaced as text.
    assert "garbage" not in content


@pytest.mark.asyncio
async def test_web_fetch_handler_binary_detected_by_nul_without_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/data.bin"
    # All bytes are ASCII, so the sniffer decodes it as text/plain; the embedded
    # NUL is what still classifies it as binary.
    blob = b"\x01\x02\x00\x03\x04binary\x00payload"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(status_code=200, headers={}, content=blob, url=url),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary content" in content


@pytest.mark.asyncio
async def test_web_fetch_extracts_pdf_as_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/report.pdf"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=_minimal_pdf(["Hello PDF"]),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert f"[Extracted text from {url} (PDF document)]" in content
    assert "# Page 1" in content
    assert "Hello PDF" in content


@pytest.mark.asyncio
async def test_web_fetch_extracts_docx_recognized_by_media_type_without_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A download URL with no usable extension: the sniffed OOXML type must drive
    # detection, not the URL path.
    url = "https://example.com/download"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/octet-stream"},
            content=_minimal_docx("Web doc body"),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert f"[Extracted text from {url} (Word document)]" in content
    assert "Web doc body" in content


@pytest.mark.asyncio
async def test_web_fetch_rejects_document_expansion_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/large.docx"
    monkeypatch.setattr(read_extract_module, "_MAX_DOCUMENT_EXTRACTED_BYTES", 128)

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={
                "Content-Type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            },
            content=_minimal_docx("x" * 512),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "document_too_large")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_scanned_pdf_reports_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/scan.pdf"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=_minimal_pdf([]),
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "(no extractable text)" in content


@pytest.mark.asyncio
async def test_web_fetch_malformed_pdf_returns_binary_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/broken.pdf"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.4 not really a pdf \x00 body",
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "Binary content" in content
    assert "application/pdf" in content


@pytest.mark.asyncio
async def test_web_fetch_handler_non_html_json_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/api/data"
    body = '{"recipe": "cake", "tasty": true}'

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text=body,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    assert data["content"] == body


def test_extract_content_strips_scripts_and_styles() -> None:
    html = """
    <html>
      <head>
        <title>Metadata Title</title>
        <style>body { display: none; }</style>
      </head>
      <body>
        <script>console.log('hide me')</script>
        <p>Visible Text</p>
      </body>
    </html>
    """

    text, metadata = extract_content(html, "https://example.com")

    assert "Visible Text" in text
    assert "console.log" not in text
    assert "display: none" not in text
    assert metadata["title"] == "Metadata Title"


def test_extract_content_skips_javascript_links_case_insensitively() -> None:
    html = """
    <html>
      <body>
        <p>Read <a href="javascript:alert(1)">lowercase</a> and
        <a href="JaVaScRiPt:alert(1)">mixed case</a> and
        <a href="#section">fragment</a>.</p>
      </body>
    </html>
    """

    text, _metadata = extract_content(html, "https://example.com")

    assert "lowercase" in text
    assert "mixed case" in text
    assert "fragment" in text
    assert "javascript:" not in text
    assert "alert(1)" not in text


def test_format_output_clamps_negative_reduction_to_zero() -> None:
    """Markdown link targets can expand text beyond raw HTML size (B3)."""
    from core.tools.web_fetch import _format_output

    # raw_size=100, clean_size=120 → reduction would be -20% without clamping.
    output = _format_output(
        "https://example.com/page",
        {"title": "Test"},
        "content",
        raw_size=100,
        clean_size=120,
    )
    assert "(-" not in output
    assert "0% reduced" in output


def test_build_truncated_output_reports_delivered_size_not_full_size(
    tmp_path: Path,
) -> None:
    """Content-Size must reflect the post-truncation text size (B3)."""
    from core.tools.web_fetch import _build_truncated_output

    # Build text large enough to require truncation.
    large_text = "A" * 200_000
    output = _build_truncated_output(
        "https://example.com/page",
        {"title": "Big Page"},
        large_text,
        raw_size=300_000,
    )

    assert len(output.encode("utf-8")) <= web_fetch_module._MAX_URL_BYTES
    # The Content-Size line must not claim 200,000 bytes when the agent
    # receives ~100 KB. Parse the delivered size from the header.
    content_size_line = [line for line in output.split("\n") if line.startswith("Content-Size:")][0]
    # Extract the "-> X bytes" portion.
    delivered_part = content_size_line.split("->")[1]
    delivered_size_str = delivered_part.split("bytes")[0].strip().replace(",", "")
    delivered_size = int(delivered_size_str)
    assert delivered_size < 200_000, (
        f"Content-Size should report truncated size, got {delivered_size}"
    )
    assert "[... content truncated ...]" in output


@pytest.mark.asyncio
async def test_web_fetch_uses_final_url_for_notebook_detection_after_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redirect to a .ipynb URL must be detected as a notebook (B4)."""
    import json

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    start_url = "https://example.com/download"
    final_url = "https://example.com/notebook.ipynb"

    notebook_content = json.dumps(
        {
            "cells": [
                {"cell_type": "code", "source": ["print('hello')\n"]},
                {"cell_type": "markdown", "source": ["# Title\n"]},
            ]
        }
    ).encode("utf-8")

    def responder(url: str) -> _FetchResult:
        if url == start_url:
            return make_result(
                status_code=302,
                headers={"Location": final_url},
                url=start_url,
            )
        return make_result(
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=notebook_content,
            text=notebook_content.decode("utf-8"),
            url=final_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(start_url))

    data = assert_success_envelope(result)
    content = data["content"]
    assert isinstance(content, str)
    assert "[Extracted text from" in content
    assert "Jupyter notebook" in content
    assert "print('hello')" in content
    assert "# Title" in content
