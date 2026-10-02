"""web_fetch content: what a fetched response becomes. Pages keep their facts,
images become attachments, documents become text, binaries become notices, and
bot checks or script-only pages become failures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

import core.tools.read_extract as read_extract_module
from core.attachments import AttachmentTooLargeError
from core.tools._public_http import PublicResponse
from tests.core.tools.web_fetch_test_support import (
    assert_failure_envelope,
    assert_success_envelope,
    fetch,
    install_http_get,
    make_context,
    make_result,
    web_fetch_registry,
)
from tests.core.tools.web_fetch_test_support import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_test_support import stub_http_session as stub_http_session

_WALL_GUIDANCE = "Try another source, or a browser Tool if one is available."
_MAIN_CONTENT_NOTE = (
    "Only the main content is shown; find also searches navigation, sidebars and footers."
)


@dataclass(frozen=True)
class _FakeRecord:
    id: str
    filename: str
    media_type: str


class _FakeAttachmentStore:
    """Records ``store()`` calls; optionally raises to simulate rejection."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.stored: list[tuple[str, bytes]] = []

    def store(self, filename: str, data: bytes) -> _FakeRecord:
        if self._error is not None:
            raise self._error
        self.stored.append((filename, data))
        return _FakeRecord(id="att-web-1", filename=filename, media_type="image/png")


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


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content_type: str | None = "text/html; charset=utf-8",
    text: str = "",
    content: bytes | None = None,
) -> None:
    """Answer every request with one successful response at the requested URL."""
    headers = {"Content-Type": content_type} if content_type else {}
    install_http_get(
        monkeypatch,
        lambda url: make_result(headers=headers, text=text, content=content, url=url),
    )


@pytest.mark.asyncio
async def test_page_facts_survive_main_content_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = """<head><base href="https://example.com/docs/"><title>Reference</title></head>
    <body><nav><a href="/">Navigation</a></nav><main>
    <div class="thread-container">Thread fact</div><div id="download-container">Download fact</div>
    <article class="gdpr">GDPR fact</article>
    <ol start="3"><li>Install <pre><code class="language-sh">pip install useful
    echo `done`</code></pre>Then <b>start</b> it.<ul><li>Child A</li><li>Child B</li></ul></li></ol>
    <table><caption>Prices in EUR per month</caption><tr><th>Plan</th><th>Price</th></tr>
    <tr><td rowspan="2">Basic</td><td>12</td></tr><tr><td>15</td></tr></table>
    <a href="install">Install guide</a>
    <img data-src="chart.png">
    <img alt="Trend" src="data:image/png;base64,AAAA">
    <p style="display: none !important">Stale price 99</p>
    <div class="ad-container">Advertisement</div>
    </main><footer>License fact</footer></body>"""
    _serve(monkeypatch, text=html)
    registry, context = web_fetch_registry(), make_context(tmp_path)

    main = assert_success_envelope(
        await registry.dispatch(context, {"url": "https://example.com/start"})
    )
    page = assert_success_envelope(
        await registry.dispatch(context, {"ref": main["ref"], "scope": "page"})
    )

    for expected in (
        "Thread fact",
        "Download fact",
        "GDPR fact",
        "pip install useful",
        "echo `done`",
        "Child A",
        "Child B",
        "Prices in EUR per month",
        "rowspan=2",
        "https://example.com/docs/install",
        "https://example.com/docs/chart.png",
        "Trend",
    ):
        assert expected in main["content"]
    assert "3." in main["content"] and "Then **start** it." in main["content"]
    assert main["title"] == "Reference"
    assert main["note"] == _MAIN_CONTENT_NOTE
    assert "License fact" not in main["content"] and "License fact" in page["content"]
    for hidden in ("data:image", "Advertisement", "Stale price"):
        assert hidden not in page["content"]


@pytest.mark.asyncio
async def test_without_main_markup_navigation_and_footer_stay_in_the_page_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(
        monkeypatch,
        content_type=None,
        text=(
            "<nav>Many links</nav><p>Actual article</p>"
            '<img src="chart.png" alt="Sales chart"><footer>Terms</footer>'
        ),
    )
    registry, context = web_fetch_registry(), make_context(tmp_path)

    main = assert_success_envelope(
        await registry.dispatch(context, {"url": "https://example.com/", "output": "text"})
    )
    page = assert_success_envelope(
        await registry.dispatch(context, {"ref": main["ref"], "scope": "page"})
    )

    assert main["content"] == "Actual article\n\nSales chart"
    assert "Many links" in page["content"] and "Terms" in page["content"]
    # Text output drops link and image targets from both views.
    assert "chart.png" not in page["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "expected", "absent", "note"),
    [
        (
            "<html><head><title>Metadata Title</title><style>body { display: none; }</style>"
            "</head><body><script>console.log('hide me')</script><p>Visible Text</p>"
            "</body></html>",
            ["Visible Text"],
            ["console.log", "display: none"],
            None,
        ),
        (
            '<p>Read <a href="javascript:alert(1)">lowercase</a> and '
            '<a href="JaVaScRiPt:alert(1)">mixed case</a> and '
            '<a href="#section">fragment</a>.</p>',
            ["lowercase", "mixed case", "fragment"],
            ["javascript:", "alert(1)"],
            None,
        ),
        (
            "<pre><code>first<br>second `tick`</code></pre><svg><text>2026: 42 units</text></svg>",
            ["first\nsecond `tick`", "[Embedded svg: 2026: 42 units]"],
            [],
            "Embedded or interactive content appears as labels or links.",
        ),
        (
            "<div>" * 250 + "Important fact" + "</div>" * 250,
            ["Important fact"],
            [],
            "Complex HTML: text retained, layout and link targets unavailable.",
        ),
    ],
    ids=["scripts-and-styles", "javascript-links", "code-breaks-and-svg", "deep-nesting"],
)
async def test_html_becomes_readable_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    html: str,
    expected: list[str],
    absent: list[str],
    note: str | None,
) -> None:
    _serve(monkeypatch, text=html)

    data = assert_success_envelope(await fetch(tmp_path, {"url": "https://example.com/"}))

    for text in expected:
        assert text in data["content"]
    for text in absent:
        assert text not in data["content"]
    assert data.get("note") == note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "content_type", "body", "expected", "absent"),
    [
        (
            "https://example.com/report.pdf",
            "application/pdf",
            _minimal_pdf(["Hello PDF"]),
            [
                "[Extracted text from https://example.com/report.pdf (PDF document)]",
                "# Page 1",
                "Hello PDF",
            ],
            [],
        ),
        # The sniffed Word type drives detection when the URL has no extension.
        (
            "https://example.com/download",
            "application/octet-stream",
            _minimal_docx("Web doc body"),
            ["[Extracted text from https://example.com/download (Word document)]", "Web doc body"],
            [],
        ),
        (
            "https://example.com/scan.pdf",
            "application/pdf",
            _minimal_pdf([]),
            ["(no extractable text)"],
            [],
        ),
        (
            "https://example.com/broken.pdf",
            "application/pdf",
            b"%PDF-1.4 not really a pdf \x00 body",
            ["Binary content", "application/pdf"],
            [],
        ),
        (
            "https://example.com/installer.exe",
            "application/octet-stream",
            b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00garbage\x00bytes",
            ["Binary content", "application/octet-stream"],
            ["garbage"],
        ),
        # All bytes are ASCII, so the sniffer decodes it as text; the embedded NUL
        # still classifies it as binary.
        (
            "https://example.com/data.bin",
            None,
            b"\x01\x02\x00\x03\x04binary\x00payload",
            ["Binary content"],
            ["payload"],
        ),
        (
            "https://example.com/api/data",
            "application/json",
            b'{"recipe": "cake", "tasty": true}',
            ['{"recipe": "cake", "tasty": true}'],
            ["Binary content"],
        ),
    ],
    ids=["pdf", "docx-without-extension", "scanned-pdf", "malformed-pdf", "exe", "nul", "json"],
)
async def test_non_html_responses_become_text_or_a_binary_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    content_type: str | None,
    body: bytes,
    expected: list[str],
    absent: list[str],
) -> None:
    _serve(
        monkeypatch,
        content_type=content_type,
        text=body.decode("utf-8", errors="replace"),
        content=body,
    )

    data = assert_success_envelope(await fetch(tmp_path, {"url": url}))

    for text in expected:
        assert text in data["content"]
    for text in absent:
        assert text not in data["content"]


@pytest.mark.asyncio
async def test_text_output_of_markdown_keeps_link_text_without_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unclosed brackets made link collapsing quadratic: past the test timeout.
    unclosed = "[a" * 100_000 + "[](https://a" * 20_000
    links = "See [the docs](https://example.com/docs) and ![chart](https://example.com/c.png)."
    page = f"{links}\n{unclosed}"
    _serve(monkeypatch, content_type="text/markdown; charset=utf-8", text=page)

    data = assert_success_envelope(
        await fetch(tmp_path, {"url": "https://example.com/readme.md", "output": "text"})
    )

    assert data["content"].startswith("See the docs and chart.\n[a[a")


@pytest.mark.asyncio
async def test_notebook_is_recognized_by_the_final_url_after_a_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_url = "https://example.com/download"
    final_url = "https://example.com/notebook.ipynb"
    notebook = json.dumps(
        {
            "cells": [
                {"cell_type": "code", "source": ["print('hello')\n"]},
                {"cell_type": "markdown", "source": ["# Title\n"]},
            ]
        }
    )

    def responder(url: str) -> PublicResponse:
        if url == start_url:
            return make_result(status_code=302, headers={"Location": final_url}, url=url)
        return make_result(headers={"Content-Type": "application/json"}, text=notebook, url=url)

    install_http_get(monkeypatch, responder)

    data = assert_success_envelope(await fetch(tmp_path, {"url": start_url}))

    assert f"[Extracted text from {final_url} (Jupyter notebook)]" in data["content"]
    assert "print('hello')" in data["content"]
    assert "# Title" in data["content"]


@pytest.mark.asyncio
async def test_document_text_over_the_extraction_limit_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_extract_module, "_MAX_DOCUMENT_EXTRACTED_BYTES", 128)
    _serve(
        monkeypatch,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=_minimal_docx("x" * 512),
    )

    result = await fetch(tmp_path, {"url": "https://example.com/large.docx"})

    error = assert_failure_envelope(result, "document_too_large")
    assert error["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, "raw"])
async def test_images_are_stored_and_shown_whatever_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str | None
) -> None:
    url = "https://example.com/photo.png"
    store = _FakeAttachmentStore()
    _serve(monkeypatch, content_type="image/png", content=_PNG_BYTES)
    arguments = {"url": url} if output is None else {"url": url, "output": output}

    result = await fetch(tmp_path, arguments, attachment_store=store)

    assert result["ok"] is True
    assert result["data"] == {"content": f"Fetched image photo.png (image/png) from {url}."}
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
async def test_image_rejected_by_the_attachment_store_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeAttachmentStore(error=AttachmentTooLargeError("Attachment size 99 exceeds 4"))
    _serve(monkeypatch, content_type="image/png", content=_PNG_BYTES)

    result = await fetch(tmp_path, {"url": "https://example.com/huge.png"}, attachment_store=store)

    error = assert_failure_envelope(result, "attachment_error")
    assert error["message"] == "Attachment size 99 exceeds 4"
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_image_without_an_attachment_store_says_it_could_not_be_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.com/photo.png"
    _serve(monkeypatch, content_type="image/png", content=_PNG_BYTES)

    data = assert_success_envelope(await fetch(tmp_path, {"url": url}))

    assert data["content"] == (
        f"[Image at {url} could not be loaded (no attachment store available).]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "html", "code", "message"),
    [
        (
            "https://www.reddit.com/r/Python/",
            "<html><head><title>Reddit - Prove your humanity</title></head><body>"
            "<h1>Prove your humanity</h1><p>Complete the challenge below and let us know "
            "you're a real person.</p></body></html>",
            "access_denied",
            "Blocked by a bot check at https://www.reddit.com/r/Python/; no readable content.",
        ),
        (
            "https://example.com/guarded",
            "<html><head><title>Just a moment...</title></head>"
            "<body><p>Verifying you are human.</p></body></html>",
            "access_denied",
            "Blocked by a bot check at https://example.com/guarded; no readable content.",
        ),
        (
            "https://example.com/denied",
            "<title>Access denied</title><p>Verify you are human</p>",
            "access_denied",
            "Blocked by a bot check at https://example.com/denied; no readable content.",
        ),
        (
            "https://example.com/app",
            "<div id='root'></div><script>runApp()</script>",
            "no_content",
            "https://example.com/app has no readable content without JavaScript, or shows an "
            "access check instead.",
        ),
        (
            "https://example.com/app",
            "﻿ \n<!-- app -->\n<div id='root'></div><script>runApp()</script>",
            "no_content",
            "https://example.com/app has no readable content without JavaScript, or shows an "
            "access check instead.",
        ),
        (
            "https://example.com/app",
            "<h1>Please enable JavaScript</h1>",
            "no_content",
            "https://example.com/app has no readable content without JavaScript, or shows an "
            "access check instead.",
        ),
    ],
    ids=["reddit-challenge", "challenge-title", "access-check", "script-only", "bom", "js-wall"],
)
async def test_bot_checks_and_script_only_pages_are_not_retryable_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, html: str, code: str, message: str
) -> None:
    # No content type: the markup alone identifies these as HTML.
    _serve(monkeypatch, content_type=None, text=html)

    error = assert_failure_envelope(await fetch(tmp_path, {"url": url}), code)

    assert error["message"] == f"{message} {_WALL_GUIDANCE}"
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_reddit_login_redirect_is_a_login_wall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = "https://old.reddit.com/r/Python/"
    login_url = "https://old.reddit.com/login/?reason=lor2&dest=https%3A%2F%2Fold.reddit.com%2Fr%2FPython%2F"

    def responder(request_url: str) -> PublicResponse:
        if request_url == requested:
            return make_result(status_code=302, headers={"Location": login_url}, url=request_url)
        return make_result(
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<html><head><title>Welcome to Reddit</title></head>"
            "<body><p>Log in or sign up to personalize your feed.</p></body></html>",
            url=login_url,
        )

    install_http_get(monkeypatch, responder)

    error = assert_failure_envelope(await fetch(tmp_path, {"url": requested}), "access_denied")

    assert error["message"] == (
        f"Blocked by a login wall at {login_url}; no readable content. {_WALL_GUIDANCE}"
    )
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_raw_output_returns_a_challenge_page_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = "<html><head><title>Reddit - Prove your humanity</title></head></html>"
    _serve(monkeypatch, text=html)

    result = await fetch(tmp_path, {"url": "https://www.reddit.com/r/Python/", "output": "raw"})

    assert assert_success_envelope(result)["content"] == html


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "html", "expected"),
    [
        (
            "https://example.com/essay",
            "<html><head><title>Just a moment of joy</title></head>"
            "<body><p>An essay about patience.</p></body></html>",
            "An essay about patience.",
        ),
        (
            "https://example.com/login/",
            "<html><head><title>Sign in</title></head>"
            "<body><p>Welcome back. Enter your credentials.</p></body></html>",
            "Welcome back.",
        ),
        (
            "https://x.com/ThePSF/status/2090064027216998893",
            "<html><head>"
            '<title>Python Software Foundation on X: "#PyPI runs on zero cost" / X</title>'
            '<meta property="og:description" content="#PyPI runs on zero cost" /></head><body>'
            '<a href="/i/jf/onboarding/web?mode=login">Log in</a>'
            '<a href="/i/jf/onboarding/web?mode=signup">Sign up</a>'
            "<p>#PyPI runs on zero cost</p></body></html>",
            "#PyPI runs on zero cost",
        ),
        (
            "https://example.com/help",
            "<title>Access denied</title><article>How to diagnose an access denied "
            "error in your application.</article>",
            "How to diagnose an access denied error",
        ),
        (
            "https://example.com/help",
            "<h1>How to enable JavaScript</h1><p>Open browser settings to enable JavaScript.</p>",
            "Open browser settings to enable JavaScript.",
        ),
    ],
    ids=["similar-title", "login-path-elsewhere", "post-with-login-links", "article", "howto"],
)
async def test_pages_that_only_mention_logins_or_checks_keep_their_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, html: str, expected: str
) -> None:
    _serve(monkeypatch, content_type=None, text=html)

    data = assert_success_envelope(await fetch(tmp_path, {"url": url}))

    assert expected in data["content"]
