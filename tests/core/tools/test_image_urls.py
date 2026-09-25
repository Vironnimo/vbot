"""analyze_image reads image URLs and data: URLs through the guarded public transport.

Every test dispatches through the production Tool path with a fake transport, so
no request reaches the network.
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_from_bytes

import pytest
from curl_cffi.requests.exceptions import CertificateVerifyError, DNSError, ReadTimeout

import core.tools._public_http as public_http
from core.attachments import AttachmentStore
from core.model_tasks import ImageUnderstandingRunContext
from core.tools._public_http import PublicResponse
from core.tools.image import ANALYZE_IMAGE_TOOL_NAME, register_analyze_image_tool
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.tools.web_fetch_helpers import (
    _StreamingSession,
    make_result,
)
from tests.core.tools.web_fetch_helpers import (
    stub_dns_resolution as stub_dns_resolution,
)
from tests.core.tools.web_fetch_helpers import (
    stub_http_session as stub_http_session,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"cat pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"dog pixels"


class _Service:
    def __init__(self) -> None:
        self.analyzed: tuple[Path, ...] | None = None
        self.contents: list[bytes] = []

    async def analyze(
        self,
        prompt: str,
        *,
        image_paths: tuple[Path, ...],
        run_context: ImageUnderstandingRunContext,
    ) -> object:
        self.analyzed = image_paths
        self.contents = [path.read_bytes() for path in image_paths]
        return SimpleNamespace(content="Two animals.")


class _Web:
    """Fake network seam: canned responses per URL, honoring the download limit."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.responses: dict[str, PublicResponse | Exception] = {}
        self.requested: list[tuple[str, int]] = []
        self.accept: list[str] = []

        async def http_get(session: object, url: str, max_bytes: int) -> PublicResponse:
            del session
            self.requested.append((url, max_bytes))
            response = self.responses.get(url, make_result(status_code=404, url=url))
            if isinstance(response, Exception):
                raise response
            if len(response.content) > max_bytes:
                raise public_http._ResponseTooLargeError(
                    f"response exceeds the {public_http._size_label(max_bytes)} download limit"
                )
            return response

        def session(**kwargs: Any) -> _StreamingSession:
            self.accept.append(kwargs["headers"]["Accept"])
            return _StreamingSession()

        async def no_sleep(*_args: object) -> None:
            return None

        monkeypatch.setattr(public_http, "_http_get", http_get)
        monkeypatch.setattr(public_http, "AsyncSession", session)
        monkeypatch.setattr(public_http, "sleep_for_retry", no_sleep)

    def image(self, url: str, data: bytes, content_type: str = "image/png") -> None:
        self.responses[url] = make_result(
            headers={"Content-Type": content_type}, url=url, content=data
        )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> _Web:
    return _Web(monkeypatch)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "cat.png").write_bytes(PNG)
    return tmp_path


def _context(root: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name=ANALYZE_IMAGE_TOOL_NAME,
        tool_call_index=0,
        workspace=root,
        vbot_root=root,
        data_root=root,
    )


class _Call:
    def __init__(self, root: Path, *, max_size_bytes: int = 20 * 1024 * 1024) -> None:
        self.service = _Service()
        self.store = AttachmentStore(root / "data", max_size_bytes=max_size_bytes)
        self.registry = ToolRegistry()
        register_analyze_image_tool(self.registry, self.service, attachment_store=self.store)
        self.context = _context(root)

    async def __call__(self, images: Any) -> dict[str, Any]:
        return await self.registry.dispatch(
            self.context, {"prompt": "What animals?", "images": images}
        )

    def stored(self) -> list[Path]:
        folder = self.store._attachments_dir
        return sorted(folder.glob("att_*.*")) if folder.exists() else []


@pytest.mark.asyncio
async def test_image_urls_and_local_files_are_analyzed_in_their_order(
    workspace: Path, web: _Web
) -> None:
    web.image("https://example.com/cat.png", PNG)
    web.image("https://example.com/img/dog.jpg", JPEG, "image/jpeg")
    call = _Call(workspace)

    result = await call(
        ["https://example.com/cat.png", "photos/cat.png", "<https:/example.com/img/dog.jpg>"]
    )

    assert result["data"] == {"analysis": "Two animals."}
    assert call.service.contents == [PNG, PNG, JPEG]
    assert call.service.analyzed is not None
    assert call.service.analyzed[1] == (workspace / "photos" / "cat.png").resolve()
    assert [url for url, _ in web.requested] == [
        "https://example.com/cat.png",
        "https://example.com/img/dog.jpg",
    ]
    assert {limit for _, limit in web.requested} == {call.store.max_size_bytes}
    assert all(accept.startswith("image/") for accept in web.accept)
    assert [image["filename"] for image in call.context.presentation_images] == [
        "cat.png",
        "cat.png",
        "dog.jpg",
    ]


@pytest.mark.asyncio
async def test_a_schemeless_address_stays_a_local_path(workspace: Path, web: _Web) -> None:
    call = _Call(workspace)

    result = await call(["example.com/cat.png"])

    assert result["error"]["code"] == "image_not_found"
    assert web.requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "data:image/png;base64," + base64.b64encode(PNG).decode(),
        # Line breaks, lost padding and the URL-safe alphabet from a copied value.
        "data:image/png;base64,"
        + "\n".join(
            base64.urlsafe_b64encode(PNG).decode().rstrip("=")[i : i + 8] for i in (0, 8, 16)
        ),
        "data:;base64," + quote_from_bytes(base64.b64encode(PNG)),
        "data:image/png," + quote_from_bytes(PNG),
        "![cat](data:image/png;base64," + base64.b64encode(PNG).decode() + ")",
    ],
)
async def test_data_urls_are_decoded(workspace: Path, web: _Web, address: str) -> None:
    call = _Call(workspace)

    result = await call([address])

    assert result["ok"] is True
    assert call.service.contents == [PNG]
    assert web.requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "http://127.0.0.1/cat.png",
            "127.0.0.1 is a private or local network address; only public internet addresses "
            "can be fetched.",
        ),
        (
            "http://localhost:8080/cat.png",
            "localhost is a private or local network address; only public internet "
            "addresses can be fetched.",
        ),
        (
            "http://[::ffff:10.0.0.5]/cat.png",
            "::ffff:10.0.0.5 is a private or local network address; only public internet "
            "addresses can be fetched.",
        ),
        (
            "https://user:secret@example.com/cat.png",
            "URLs containing a user name or password cannot be fetched; remove the "
            "credentials from the address.",
        ),
    ],
)
async def test_private_addresses_stay_unreachable(
    workspace: Path, web: _Web, url: str, message: str
) -> None:
    call = _Call(workspace)

    result = await call([url])

    assert result["error"] == {"code": "blocked_url", "message": message, "retryable": False}
    assert web.requested == []
    assert call.service.analyzed is None


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_not_followed(workspace: Path, web: _Web) -> None:
    web.responses["https://example.com/cat.png"] = make_result(
        status_code=302, headers={"Location": "http://192.168.1.10/cat.png"}
    )
    call = _Call(workspace)

    result = await call(["https://example.com/cat.png"])

    assert result["error"]["code"] == "blocked_url"
    assert result["error"]["message"] == (
        "https://example.com/cat.png redirected to an address that is not followed. "
        "192.168.1.10 is a private or local network address; only public internet addresses "
        "can be fetched."
    )
    assert [url for url, _ in web.requested] == ["https://example.com/cat.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "message", "retryable"),
    [
        (
            make_result(status_code=404, url="https://example.com/cat.png"),
            "image_not_found",
            "HTTP 404: there is no image at https://example.com/cat.png. Check the address; "
            "the image may have moved or been removed.",
            False,
        ),
        (
            make_result(status_code=403, url="https://example.com/cat.png"),
            "access_denied",
            "HTTP 403: example.com refused access to https://example.com/cat.png. The site "
            "blocks automated requests or requires a login, so repeating the request will not "
            "help. Try another source.",
            False,
        ),
        (
            ReadTimeout("Operation timed out"),
            "timeout",
            "example.com did not respond within 30 seconds. The site may be slow or down; try "
            "again later or use another source.",
            True,
        ),
        (
            DNSError("curl: (6) Could not resolve host: example.com"),
            "host_not_found",
            'Host "example.com" was not found (DNS lookup failed). Check the address for typos.',
            False,
        ),
        (
            CertificateVerifyError("curl: (60) SSL certificate problem: certificate has expired"),
            "tls_error",
            "The secure connection to example.com failed (SSL certificate problem: certificate "
            "has expired). The site's certificate or TLS setup is broken, so repeating the "
            "request will not help.",
            False,
        ),
        (
            make_result(
                headers={"Content-Type": "image/png"},
                url="https://example.com/cat.png",
                content=PNG * 10,
            ),
            "response_too_large",
            "https://example.com/cat.png: response exceeds the 64 bytes download limit. Try a "
            "smaller file or another source.",
            False,
        ),
    ],
)
async def test_download_failures_say_what_went_wrong(
    workspace: Path,
    web: _Web,
    response: PublicResponse | Exception,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    web.responses["https://example.com/cat.png"] = response
    call = _Call(workspace, max_size_bytes=64)

    result = await call(["https://example.com/cat.png"])

    assert result["error"]["code"] == code
    assert result["error"]["message"] == message
    assert result["error"]["retryable"] is retryable
    assert call.service.analyzed is None
    assert call.stored() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body", "message"),
    [
        (
            "text/html; charset=utf-8",
            b"<!DOCTYPE html><html><body><img src='/cat.png'></body></html>",
            "https://example.com/cat returned a web page, not an image. Pass the address of "
            "the image file itself, not of a page that shows it; the site may also show an "
            "access check instead of the image.",
        ),
        (
            "image/svg+xml",
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            "https://example.com/cat is an SVG drawing, which cannot be analyzed. Pass a PNG, "
            "JPEG, GIF or WebP image instead.",
        ),
        (
            "application/pdf",
            b"%PDF-1.7 catalogue",
            "https://example.com/cat returned application/pdf content, not an image. Pass an "
            "image file such as PNG, JPEG, GIF or WebP.",
        ),
        (
            "image/png",
            b"",
            "https://example.com/cat returned an empty response, not an image. Try another source.",
        ),
    ],
)
async def test_content_that_is_not_an_image_is_named(
    workspace: Path, web: _Web, content_type: str, body: bytes, message: str
) -> None:
    web.image("https://example.com/cat", body, content_type)
    call = _Call(workspace)

    result = await call(["https://example.com/cat"])

    assert result["error"] == {"code": "not_an_image", "message": message, "retryable": False}
    assert call.stored() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "code", "message"),
    [
        (
            "data:text/plain,hello",
            "not_an_image",
            "The data: URL holds text/plain content, not an image. Pass an image file such as "
            "PNG, JPEG, GIF or WebP.",
        ),
        (
            "data:image/png;base64",
            "invalid_arguments",
            "The data: URL is incomplete: it needs the form data:image/png;base64,<data>, "
            "with a comma before the data.",
        ),
        (
            "data:image/png;base64,iVBORw0KGgo*",
            "invalid_arguments",
            "The data: URL has damaged base64 data; it may have been cut off. Pass the "
            "complete data: URL, or the image file's path.",
        ),
        (
            "data:image/png;base64," + base64.b64encode(PNG * 10).decode(),
            "image_too_large",
            "The data: URL holds more than 64 bytes of data; an image can be at most 64 "
            "bytes. Pass a smaller image.",
        ),
    ],
)
async def test_unusable_data_urls_are_explained(
    workspace: Path, web: _Web, address: str, code: str, message: str
) -> None:
    call = _Call(workspace, max_size_bytes=64)

    result = await call([address])

    assert result["error"] == {"code": code, "message": message, "retryable": False}
    assert call.service.analyzed is None


@pytest.mark.asyncio
async def test_several_images_name_each_failure_and_store_nothing(
    workspace: Path, web: _Web
) -> None:
    web.image("https://example.com/cat.png", PNG)
    call = _Call(workspace)

    result = await call(
        [
            "https://example.com/cat.png",
            "https://example.com/gone.png",
            "photos/cat.png",
            "data:image/png;base64",
        ]
    )

    assert result["error"] == {
        "code": "image_download_failed",
        "message": "images[1]: HTTP 404: there is no image at https://example.com/gone.png. "
        "Check the address; the image may have moved or been removed.\n"
        "images[3]: The data: URL is incomplete: it needs the form "
        "data:image/png;base64,<data>, with a comma before the data.",
        "retryable": False,
    }
    assert call.service.analyzed is None
    assert call.stored() == []
    # The local image still appears in the row; the failed addresses do not.
    assert [image["filename"] for image in call.context.presentation_images] == ["cat.png"]


@pytest.mark.asyncio
async def test_a_missing_local_file_fails_before_any_download(workspace: Path, web: _Web) -> None:
    web.image("https://example.com/dog.png", PNG)
    call = _Call(workspace)

    result = await call(["https://example.com/dog.png", "photos/cat.jpg"])

    assert result["error"] == {
        "code": "image_not_found",
        "message": "No image at photos/cat.jpg (similar: photos/cat.png).\n"
        'If you meant that file, pass {"images": ["https://example.com/dog.png", '
        '"photos/cat.png"]}.',
        "retryable": False,
    }
    assert web.requested == []


@pytest.mark.asyncio
async def test_more_images_than_one_analysis_takes_fail_before_any_download(
    workspace: Path, web: _Web
) -> None:
    call = _Call(workspace)

    result = await call([f"https://example.com/{index}.png" for index in range(7)])

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "analyze_image takes at most 6 images per call; received 7. Split them "
        "across calls of up to 6 images each.",
        "retryable": False,
    }
    assert web.requested == []
