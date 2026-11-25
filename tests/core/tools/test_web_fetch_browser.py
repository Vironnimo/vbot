"""web_fetch's local browser: when it steps in, what its outcome does to the result,
and the guard proxy that keeps the browser on public addresses. No browser runs here."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx

import core.tools._public_http as public_http
from core.tools import _web_fetch_browser as browser_module
from core.tools._public_http import PublicResponse
from core.tools._web_fetch_browser import BrowserFetchError
from tests.core.tools.web_fetch_test_support import (
    fetch,
    install_http_get,
    make_context,
    make_result,
    web_fetch_registry,
)
from tests.core.tools.web_fetch_test_support import stub_dns_resolution as stub_dns_resolution
from tests.core.tools.web_fetch_test_support import stub_http_session as stub_http_session

URL = "https://example.com/article"
TAVILY = "https://api.tavily.com/extract"
_ARTICLE = "<html><head><title>Rendered</title></head><body><main><p>{}</p></main></body></html>"
_FIREWALL_BLOCK = (
    "<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. "
    "Please consult with your administrator.<br><br>Your support ID is: 1234567890</body></html>"
)


class FakeBrowser:
    """PageBrowser stand-in that returns one canned page or failure."""

    def __init__(self, outcome: str | BrowserFetchError, *, installed: bool = True) -> None:
        self.outcome = outcome
        self.installed = installed
        self.urls: list[str] = []

    def available(self) -> bool:
        return self.installed

    async def fetch(self, url: str, **_options: Any) -> PublicResponse:
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
        make_result(
            headers={"content-type": "text/html"},
            text="<html><body><div id='root'></div></body></html>",
            url=URL,
        ),
        make_result(status_code=302, headers={"Location": URL}, url=URL),
        make_result(
            headers={"content-type": "text/html"},
            text="<html><head><meta http-equiv='refresh' content='0; URL=/new'></head>"
            "<body>Page moved</body></html>",
            url=URL,
        ),
        make_result(
            headers={"content-type": "text/html"},
            text="<html><head><title>Redirecting to: /new</title></head><body>"
            "<a href='/new'>Redirecting from <code>/old</code> to <code>/new</code></a>"
            "</body></html>",
            url=URL,
        ),
    ],
    ids=[
        "javascript-shell",
        "redirect-loop",
        "meta-refresh",
        "redirect-link",
    ],
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
async def test_redirect_page_names_its_destination_without_hiding_readable_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "next?tab=docs"
    head = f'<base href="/guide/"><meta http-equiv="Refresh" content="0;Url={target}">'
    body = "<p>Documentation about redirects.</p>"

    def serve(url: str) -> PublicResponse:
        return make_result(
            headers={"content-type": "text/html"},
            text=f"<html><head>{head}</head><body>{body}</body></html>",
            url=url,
        )

    install_http_get(monkeypatch, serve)
    result = await fetch(tmp_path, {"url": URL})
    assert result["error"]["code"] == "no_content"
    assert "https://example.com/guide/next?tab=docs" in result["error"]["message"]
    raw = await fetch(tmp_path, {"url": URL, "output": "raw"})
    assert raw["ok"] and head in raw["data"]["content"]
    head = head.replace("0;Url=", "60;Url=")
    link = "<a href='/new'>Redirecting from <code>/old</code> to <code>/new</code></a>"
    body += link
    article = await fetch(tmp_path, {"url": URL})
    assert article["ok"] and "Documentation about redirects." in article["data"]["content"]
    body = link
    plain = await fetch(tmp_path, {"url": URL, "output": "text"})
    assert plain["error"]["code"] == "no_content"
    assert "https://example.com/new" in plain["error"]["message"]
    with respx.mock() as router:
        target_url = "https://example.com/new"
        stub = f"[Redirecting from `/old` to `{target_url}`]({target_url})"
        router.post(TAVILY).respond(json={"results": [{"url": URL, "raw_content": stub}]})
        service = await fetch(
            tmp_path,
            {"url": URL, "output": "text"},
            **_options(FakeBrowser("", installed=False), provider="tavily"),
        )
    assert service["error"]["code"] == "no_content"
    assert "tavily" in service["error"]["message"] and target_url in service["error"]["message"]


@pytest.mark.asyncio
async def test_failed_browser_keeps_the_direct_failure_and_the_service_still_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_http_get(monkeypatch, lambda url: make_result(text="", url=url))
    browser = FakeBrowser(BrowserFetchError("the browser did not start"))

    failed = await fetch(tmp_path, {"url": URL}, **_options(browser))
    with respx.mock() as router:
        route = router.post(TAVILY).respond(
            json={"results": [{"url": URL, "raw_content": "Service text"}]}
        )
        recovered = await fetch(tmp_path, {"url": URL}, **_options(browser, provider="tavily"))

    assert failed["error"]["code"] == "no_content"
    assert "The local browser could not read it either" in failed["error"]["message"]
    assert "the browser did not start" in failed["error"]["message"]
    assert recovered["ok"] and recovered["data"]["content"] == "Service text"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_rendered_static_app_path_expires_and_honors_access_checks(tmp_path, monkeypatch):
    now = [1.0]
    monkeypatch.setattr("core.tools.web_fetch._route_clock", lambda: now[0])
    shell = "<script type='module' src='/bundle.js'></script><div id='root'></div>"
    requests = []

    def answer(url):
        requests.append(url)
        return make_result(headers={"content-type": "text/html"}, text=shell, url=url)

    install_http_get(monkeypatch, answer)
    browser = FakeBrowser(_ARTICLE.format("Public app content"))
    tool = web_fetch_registry(**_options(browser))
    context = make_context(tmp_path)

    async def get(path):
        return await tool.dispatch(context, {"url": f"https://example.com{path}"})

    assert (await get("/docs/a"))["ok"]
    assert (await get("/docs/b"))["ok"] and len(requests) == 1
    assert (await get("/news/a"))["ok"] and len(requests) == 2
    assert (await get("/docs/a?print=1"))["ok"] and len(requests) == 3
    now[0] += 301
    assert (await get("/docs/a"))["ok"] and len(requests) == 4
    browser.outcome = browser_module.BrowserAccessError("the site answered HTTP 403 in the browser")
    assert (await get("/docs/c"))["error"]["code"] == "access_denied"
    assert len(requests) == 4
    browser.outcome = _ARTICLE.format("Public app content")
    assert (await get("/docs/a"))["ok"] and len(requests) == 5

    events = asyncio.Queue()
    events.put_nowait(
        {
            "method": "Network.responseReceived",
            "params": {
                "frameId": "main",
                "type": "Document",
                "response": {
                    "url": URL,
                    "status": 429,
                    "headers": {"Retry-After": "7200", "X-RateLimit-Global": "true"},
                },
            },
        }
    )
    with pytest.raises(browser_module.BrowserAccessError) as limited:
        await browser_module._settle(AsyncMock(), "session", events, "main", float("inf"))
    browser.outcome = limited.value
    assert (await get("/docs/b"))["error"]["code"] == "rate_limited"
    assert (await get("/news/b"))["error"]["code"] == "rate_limited"
    assert len(requests) == 5


@pytest.mark.asyncio
async def test_browser_tries_installed_candidates_and_remembers_the_working_one(
    tmp_path, monkeypatch
):
    first = browser_module.LocalBrowser("First", "first")
    second = browser_module.LocalBrowser("Second", "second")
    browser = browser_module.PageBrowser(tmp_path, find=lambda: [first, second])
    launched = []
    process = SimpleNamespace(returncode=0)

    async def launch(candidate, profile, port):
        launched.append(candidate)
        if candidate == first:
            raise OSError("cannot start")
        return process

    monkeypatch.setattr(browser_module, "_launch", launch)
    monkeypatch.setattr(browser_module, "_connect", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(browser_module, "wait_for_exit", AsyncMock())
    monkeypatch.setattr(browser_module, "_render", AsyncMock(return_value=make_result(url=URL)))
    try:
        await browser.fetch(URL)
        await browser.aclose()
        await browser.fetch(URL)
        assert launched == [first, second, second]
    finally:
        await browser.aclose()


@pytest.mark.asyncio
async def test_browser_launch_keeps_the_sandbox_even_as_root(tmp_path, monkeypatch):
    started = AsyncMock()
    monkeypatch.setattr(browser_module.sys, "platform", "linux")
    monkeypatch.setattr(browser_module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(browser_module, "create_subprocess_exec", started)
    monkeypatch.setattr(
        browser_module,
        "guarded_process_launch",
        lambda argv, **_env: SimpleNamespace(argv=argv, pass_fds=()),
    )
    await browser_module._launch(
        browser_module.LocalBrowser("Chromium", "chromium"), tmp_path, 1234
    )
    args = started.await_args.args
    assert "--no-sandbox" not in args
    assert "--proxy-server=http://127.0.0.1:1234" in args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "answer", "arguments", "settings", "installed"),
    [
        (URL, {"status_code": 403}, {}, {"browser": False}, True),
        (URL, {"status_code": 403}, {}, {}, False),
        (URL, {"status_code": 403}, {"output": "raw"}, {}, True),
        (URL, {"status_code": 404}, {}, {}, True),
        (URL, {"status_code": 403}, {}, {}, True),
        (URL, {"status_code": 400, "text": "<title>Just a moment...</title>"}, {}, {}, True),
        (
            URL,
            {
                "headers": {"content-type": "text/html"},
                "text": "<title>Ihr Browser wird geprüft – reCAPTCHA</title>"
                "<p>Ihr Browser wird geprüft, bevor Sie auf example.com zugreifen können…</p>",
            },
            {},
            {},
            True,
        ),
        ("http://127.0.0.1/", {}, {}, {}, True),
        # The browser shares this machine's address, which the firewall blocks.
        (URL, {"headers": {"content-type": "text/html"}, "text": _FIREWALL_BLOCK}, {}, {}, True),
    ],
    ids=[
        "turned-off",
        "not-installed",
        "raw",
        "missing-page",
        "forbidden",
        "challenge",
        "captcha",
        "private-address",
        "firewall",
    ],
)
async def test_browser_stays_out_where_it_cannot_help_or_is_not_wanted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    answer: dict[str, Any],
    arguments: dict[str, Any],
    settings: dict[str, Any],
    installed: bool,
) -> None:
    install_http_get(monkeypatch, lambda target: make_result(**answer, url=target))
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
    request: bytes, follow_up: bytes = b"", *, paused_host: str | None = None
) -> tuple[bytes, tuple[str, str] | None]:
    """Send a request through a fresh proxy; return its answer and its last refusal."""
    proxy = browser_module._GuardProxy()
    if paused_host:
        proxy.pause_checks.append(lambda host: host == paused_host)
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
        (b"CONNECT example.com.:443 HTTP/1.1\r\n\r\n", 429, "rate_limited"),
    ],
    ids=["loopback", "localhost", "private-http", "ipv6-loopback", "unresolvable", "paused-host"],
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

    answer, refused = await _through_proxy(request_head, paused_host="example.com")

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
