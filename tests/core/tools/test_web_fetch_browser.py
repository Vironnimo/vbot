"""web_fetch's local browser: when it steps in, what its outcome does to the result,
and the guard proxy that keeps the browser on public addresses. No browser runs here."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import respx

import core.tools._public_http as public_http
from core.tools import _web_fetch_browser as browser_module
from core.tools._public_http import PublicResponse
from core.tools._web_fetch_browser import BrowserFetchError
from tests.core.tools.web_fetch_test_support import (
    fetch,
    install_http_get,
    make_result,
)
from tests.core.tools.web_fetch_test_support import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_test_support import stub_http_session as stub_http_session

URL = "https://example.com/article"
TAVILY = "https://api.tavily.com/extract"
_ARTICLE = "<html><head><title>Rendered</title></head><body><main><p>{}</p></main></body></html>"


class FakeBrowser:
    """PageBrowser stand-in that returns one canned page or failure."""

    def __init__(self, outcome: str | BrowserFetchError, *, installed: bool = True) -> None:
        self.outcome = outcome
        self.installed = installed
        self.urls: list[str] = []

    def available(self) -> bool:
        return self.installed

    async def fetch(self, url: str) -> PublicResponse:
        self.urls.append(url)
        if isinstance(self.outcome, BrowserFetchError):
            raise self.outcome
        return make_result(headers={"content-type": "text/html"}, text=self.outcome, url=url)


def _options(fake: FakeBrowser, **settings: Any) -> dict[str, Any]:
    return {
        "browser": fake,
        "credential_resolver": lambda _variable: "fixture-key",
        "settings_loader": lambda: {"provider": "direct", "mode": "fallback", **settings},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direct",
    [
        make_result(status_code=403, url=URL),
        make_result(
            headers={"content-type": "text/html"},
            text="<html><body><div id='root'></div></body></html>",
            url=URL,
        ),
        make_result(status_code=302, headers={"Location": URL}, url=URL),
    ],
    ids=["bot-check", "javascript-shell", "redirect-loop"],
)
async def test_browser_reads_what_direct_reading_could_not_before_any_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct: PublicResponse
) -> None:
    install_http_get(monkeypatch, lambda _url: direct)
    browser = FakeBrowser(_ARTICLE.format("Rendered article text"))
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TAVILY).respond(json={})
        result = await fetch(tmp_path, {"url": URL}, **_options(browser, provider="tavily"))

    assert result["ok"], result
    assert "Rendered article text" in result["data"]["content"]
    assert browser.urls == [URL]
    assert route.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (BrowserFetchError("the site answered HTTP 403 in the browser too"), "HTTP 403"),
        (
            "<html><head><title>Just a moment...</title></head><body>Wait</body></html>",
            "it showed a bot check or login wall",
        ),
    ],
    ids=["browser-error", "browser-wall"],
)
async def test_failed_browser_keeps_the_direct_failure_and_the_service_still_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: Any, reason: str
) -> None:
    install_http_get(monkeypatch, lambda url: make_result(status_code=403, url=url))
    browser = FakeBrowser(outcome)

    failed = await fetch(tmp_path, {"url": URL}, **_options(browser))
    with respx.mock() as router:
        route = router.post(TAVILY).respond(
            json={"results": [{"url": URL, "raw_content": "Service text"}]}
        )
        recovered = await fetch(tmp_path, {"url": URL}, **_options(browser, provider="tavily"))

    assert failed["error"]["code"] == "access_denied"
    assert "The local browser could not read it either" in failed["error"]["message"]
    assert reason in failed["error"]["message"]
    assert recovered["ok"] and recovered["data"]["content"] == "Service text"
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "status", "arguments", "settings", "installed"),
    [
        (URL, 403, {}, {"browser": False}, True),
        (URL, 403, {}, {}, False),
        (URL, 403, {"output": "raw"}, {}, True),
        (URL, 404, {}, {}, True),
        ("http://127.0.0.1/", 200, {}, {}, True),
    ],
    ids=["turned-off", "not-installed", "raw", "missing-page", "private-address"],
)
async def test_browser_stays_out_where_it_cannot_help_or_is_not_wanted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    status: int,
    arguments: dict[str, Any],
    settings: dict[str, Any],
    installed: bool,
) -> None:
    install_http_get(monkeypatch, lambda target: make_result(status_code=status, url=target))
    browser = FakeBrowser(_ARTICLE.format("Rendered"), installed=installed)

    result = await fetch(tmp_path, {"url": url, **arguments}, **_options(browser, **settings))

    assert not result["ok"]
    assert "local browser" not in result["error"]["message"]
    assert browser.urls == []


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, bytes]]:
    """Record where the proxy connects and what it sends; answer like a site."""
    connections: list[tuple[str, int, bytes]] = []

    async def open_upstream(
        address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        async def site(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            server.close()
            connections.append((address, port, await reader.read(4096)))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(site, "127.0.0.1", 0)
        site_port = server.sockets[0].getsockname()[1]
        return await asyncio.open_connection("127.0.0.1", site_port)

    monkeypatch.setattr(browser_module, "_open_upstream", open_upstream)
    return connections


async def _through_proxy(
    request: bytes, follow_up: bytes = b""
) -> tuple[bytes, tuple[str, str] | None]:
    """Send a request through a fresh proxy; return its answer and its last refusal."""
    proxy = browser_module._GuardProxy()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(request)
        if follow_up:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(follow_up)
        await writer.drain()
        async with asyncio.timeout(5):
            answer = await reader.read(4096)
        writer.close()
        return answer, proxy.refused_since(0)
    finally:
        await proxy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_head", "status", "reason"),
    [
        (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n", 403, "blocked_url"),
        (b"CONNECT localhost:8421 HTTP/1.1\r\n\r\n", 403, "blocked_url"),
        (
            b"GET http://192.168.1.1/admin HTTP/1.1\r\nHost: 192.168.1.1\r\n\r\n",
            403,
            "blocked_url",
        ),
        (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", 403, "blocked_url"),
        # A bot check's challenge host that does not resolve is not a private address.
        (b"CONNECT validate.bot-check.invalid:443 HTTP/1.1\r\n\r\n", 502, "host_not_found"),
    ],
    ids=["loopback", "localhost", "private-http", "ipv6-loopback", "unresolvable"],
)
async def test_guard_proxy_refuses_private_local_and_unresolvable_addresses(
    upstream: list[tuple[str, int, bytes]],
    monkeypatch: pytest.MonkeyPatch,
    request_head: bytes,
    status: int,
    reason: str,
) -> None:
    resolve = public_http._resolve_host_addresses

    async def resolve_or_fail(host: str, port: int) -> list[public_http.IpAddress]:
        if host.endswith(".invalid"):
            raise public_http._HostNotFoundError(public_http._host_not_found(host))
        return await resolve(host, port)

    monkeypatch.setattr(public_http, "_resolve_host_addresses", resolve_or_fail)

    answer, refused = await _through_proxy(request_head)

    assert answer.startswith(f"HTTP/1.1 {status}".encode())
    assert refused is not None and refused[1] == reason
    assert upstream == []


@pytest.mark.asyncio
async def test_guard_proxy_tunnels_to_the_address_it_validated(
    upstream: list[tuple[str, int, bytes]],
) -> None:
    answer, _ = await _through_proxy(
        b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
        follow_up=b"tls bytes",
    )

    assert answer.endswith(b"ok")
    assert upstream == [("93.184.216.34", 443, b"tls bytes")]


@pytest.mark.asyncio
async def test_guard_proxy_forwards_plain_http_in_origin_form_and_closes(
    upstream: list[tuple[str, int, bytes]],
) -> None:
    answer, _ = await _through_proxy(
        b"GET http://example.com/page?q=1 HTTP/1.1\r\nHost: example.com\r\n"
        b"Proxy-Connection: keep-alive\r\n\r\n"
    )

    assert answer.endswith(b"ok")
    [(address, port, sent)] = upstream
    assert (address, port) == ("93.184.216.34", 80)
    assert sent.startswith(b"GET /page?q=1 HTTP/1.1\r\n")
    assert b"Proxy-Connection" not in sent and b"Connection: close" in sent
