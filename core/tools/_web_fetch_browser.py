"""Local headless browser for pages direct reading cannot get.

web_fetch hands a page to it when the direct GET meets a bot check or a page
that shows its content only with JavaScript. It drives the Chrome, Edge or
Chromium installed on the server over the DevTools protocol; no browser is
downloaded and the user's own profile is never touched.

Safety matches the direct transport. The browser reaches the network only
through a private proxy on the loopback interface, and that proxy connects
every request, subresources and redirects included, to the public address it
validated itself (``resolve_public_address``), so neither a page nor a DNS
answer can steer the browser at a private or local address. Each fetch runs in
its own browser context with no cookies or storage from earlier fetches and
with downloads denied. One browser process serves concurrent fetches and exits
after a quiet period.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.tools._public_http import (
    SELF_SOLVING_CHECK_TITLES,
    PublicFetchError,
    PublicResponse,
    resolve_public_address,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.processes import (
    create_subprocess_exec,
    guarded_process_launch,
    kill_process_tree,
    kill_process_tree_async,
    subprocess_creation_flags,
    wait_for_exit,
)

_LOGGER = get_logger("tools.web_fetch.browser")

_MAX_PAGES = 3
_IDLE_SECONDS = 120.0
_START_SECONDS = 20.0
# One fetch: navigation, scripts and a passing browser check.
_FETCH_SECONDS = 35.0
_POLL_SECONDS = 0.5
# Characters of HTML taken from the page; extraction applies its own limit
# with a partial-content warning, this one only bounds the DevTools message.
_MAX_HTML_CHARS = 10_000_000
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_PROXY_HEAD_BYTES = 64 * 1024
_PROXY_HEAD_SECONDS = 30.0
_PROXY_CONNECT_SECONDS = 10.0
_BLOCK_MEMORY = 256

_PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})

_STATE_SCRIPT = (
    "({title: document.title, ready: document.readyState, "
    "length: document.body ? document.body.innerText.length : 0})"
)
_PAGE_SCRIPT = (
    "(() => { const navigation = performance.getEntriesByType('navigation')[0]; "
    "const root = document.documentElement; "
    "return {url: location.href, type: document.contentType, "
    "status: navigation && navigation.responseStatus || 0, "
    f"html: root ? root.outerHTML.slice(0, {_MAX_HTML_CHARS}) : ''}}; }})()"
)


class BrowserFetchError(Exception):
    """The local browser could not read the page; the message says why."""


@dataclass(frozen=True)
class LocalBrowser:
    """A Chromium-family browser installed on the server."""

    name: str
    path: str


def _browser_candidates() -> list[tuple[str, str]]:
    if sys.platform == "win32":
        roots = [
            os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        paths = [
            ("Google Chrome", ("Google", "Chrome", "Application", "chrome.exe")),
            ("Microsoft Edge", ("Microsoft", "Edge", "Application", "msedge.exe")),
            ("Chromium", ("Chromium", "Application", "chrome.exe")),
        ]
        return [(name, str(Path(root, *parts))) for name, parts in paths for root in roots if root]
    if sys.platform == "darwin":
        return [
            (name, f"/Applications/{name}.app/Contents/MacOS/{name}")
            for name in ("Google Chrome", "Microsoft Edge", "Chromium")
        ]
    found: list[tuple[str, str]] = []
    for name, command in (
        ("Google Chrome", "google-chrome-stable"),
        ("Google Chrome", "google-chrome"),
        ("Chromium", "chromium"),
        ("Chromium", "chromium-browser"),
        ("Microsoft Edge", "microsoft-edge-stable"),
        ("Microsoft Edge", "microsoft-edge"),
    ):
        if path := shutil.which(command):
            found.append((name, path))
    return found


def find_local_browser() -> LocalBrowser | None:
    """Return the first installed Chrome, Edge or Chromium, or ``None``."""
    for name, path in _browser_candidates():
        if os.path.isfile(path):
            return LocalBrowser(name, path)
    return None


# Connects the proxy to a validated address; tests replace it.
_open_upstream = asyncio.open_connection


class _GuardProxy:
    """HTTP proxy on the loopback interface that reaches only public addresses."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        # Hosts the proxy refused, with when and why (a public-fetch failure code).
        self._refused: dict[str, tuple[float, str]] = {}
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0, limit=_PROXY_HEAD_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]

    def refused_since(self, moment: float) -> tuple[str, str] | None:
        """Return the host last refused since ``moment`` (``time.monotonic``) and why.

        The reason is ``blocked_url`` (a private or local address) or
        ``host_not_found`` (the name does not resolve).
        """
        refusals = [(when, host, code) for host, (when, code) in self._refused.items()]
        latest = max((refusal for refusal in refusals if refusal[0] >= moment), default=None)
        return None if latest is None else (latest[1], latest[2])

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        upstream: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(_PROXY_HEAD_SECONDS):
                head = await reader.readuntil(b"\r\n\r\n")
            request_line, _, header_block = head.partition(b"\r\n")
            method, target, version = request_line.decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                host, port, forward = host.strip("[]"), int(port_text), b""
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise ValueError("not an absolute http URL")
                host, port = parts.hostname, parts.port or 80
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                forward = f"{method} {path} {version}\r\n".encode("latin-1") + _forward_headers(
                    header_block
                )
            try:
                address = await resolve_public_address(host, port)
            except PublicFetchError as error:
                self._remember_refusal(host, error.code)
                await _answer(
                    writer, "502 Bad Gateway" if error.code == "host_not_found" else "403 Forbidden"
                )
                return
            try:
                async with asyncio.timeout(_PROXY_CONNECT_SECONDS):
                    upstream_reader, upstream = await _open_upstream(address, port)
            except OSError, TimeoutError:
                await _answer(writer, "502 Bad Gateway")
                return
            self._writers.add(upstream)
            if forward:
                upstream.write(forward)
            else:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await _relay(reader, writer, upstream_reader, upstream)
        except ValueError, UnicodeDecodeError, asyncio.IncompleteReadError, TimeoutError:
            with contextlib.suppress(OSError):
                await _answer(writer, "400 Bad Request")
        except OSError, asyncio.LimitOverrunError:
            pass
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    self._writers.discard(stream)
                    stream.close()

    def _remember_refusal(self, host: str, code: str) -> None:
        self._refused.pop(host.lower(), None)
        self._refused[host.lower()] = (time.monotonic(), code)
        if len(self._refused) > _BLOCK_MEMORY:
            del self._refused[next(iter(self._refused))]


def _forward_headers(header_block: bytes) -> bytes:
    """Drop proxy headers and ask the site to close after one response."""
    lines = [
        line
        for line in header_block.split(b"\r\n")
        if line
        and line.split(b":", 1)[0].strip().lower()
        not in {b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"}
    ]
    return b"\r\n".join([*lines, b"Connection: close", b"", b""])


async def _answer(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()


async def _relay(
    client_reader: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream: asyncio.StreamWriter,
) -> None:
    async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError):
            while data := await source.read(65536):
                target.write(data)
                await target.drain()

    tasks = [
        asyncio.create_task(copy(client_reader, upstream)),
        asyncio.create_task(copy(upstream_reader, client)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _DevToolsError(Exception):
    """A DevTools command failed or the browser connection closed."""


class _DevTools:
    """Minimal DevTools protocol client over the browser's WebSocket."""

    def __init__(self, socket: Any) -> None:
        self._socket = socket
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._reader = asyncio.create_task(self._read(), name="web-fetch-browser-devtools")

    @property
    def closed(self) -> bool:
        return self._reader.done()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        if self.closed:
            raise _DevToolsError("the browser connection is closed")
        message_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        try:
            await self._socket.send(json.dumps(message))
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._pending.pop(message_id, None)

    def events(self, session: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._events[session] = queue
        return queue

    def forget(self, session: str) -> None:
        self._events.pop(session, None)

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.close()
        await asyncio.gather(self._reader, return_exceptions=True)

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(_DevToolsError(str(error.get("message", error))))
                    else:
                        future.set_result(message.get("result", {}))
                elif (queue := self._events.get(message.get("sessionId", ""))) is not None:
                    queue.put_nowait(message)
        except Exception as error:
            _LOGGER.debug("Browser DevTools connection ended: %s", error)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(_DevToolsError("the browser connection closed"))
            for queue in self._events.values():
                queue.put_nowait({"method": "Inspector.detached"})


@dataclass
class _Running:
    process: asyncio.subprocess.Process
    devtools: _DevTools
    proxy: _GuardProxy
    profile: Path
    user_agent: str

    def alive(self) -> bool:
        return self.process.returncode is None and not self.devtools.closed


class PageBrowser:
    """Render pages in the server's own headless Chrome, Edge or Chromium.

    The browser starts with the first fetch and exits after ``idle_seconds``
    without one. ``profile_root`` holds the throwaway browser profile; leftovers
    of a killed server are removed at the next start.
    """

    def __init__(
        self,
        profile_root: Path,
        *,
        find: Callable[[], LocalBrowser | None] = find_local_browser,
        idle_seconds: float = _IDLE_SECONDS,
    ) -> None:
        self._profile_root = profile_root
        self._find = find
        self._idle_seconds = idle_seconds
        self._lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(_MAX_PAGES)
        self._running: _Running | None = None
        self._active = 0
        self._idle: asyncio.TimerHandle | None = None
        self._expiry: asyncio.Task[None] | None = None

    def available(self) -> bool:
        return self._find() is not None

    async def fetch(self, url: str) -> PublicResponse:
        """Render ``url`` and return its HTML; raise ``BrowserFetchError`` otherwise."""
        self._active += 1
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        try:
            async with self._pages:
                running = await self._ensure_running()
                try:
                    async with asyncio.timeout(_FETCH_SECONDS + 10):
                        return await _render(running, url)
                except TimeoutError as error:
                    raise BrowserFetchError(
                        "the browser did not finish loading the page"
                    ) from error
                except _DevToolsError as error:
                    raise BrowserFetchError(f"the browser failed ({error})") from error
        finally:
            self._active -= 1
            if self._active == 0 and self._running is not None:
                self._idle = asyncio.get_running_loop().call_later(self._idle_seconds, self._expire)

    async def aclose(self) -> None:
        """Stop the browser and remove its profile."""
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        async with self._lock:
            await self._stop()

    def close(self) -> None:
        """Kill the browser without the Event Loop (synchronous Runtime shutdown)."""
        running, self._running = self._running, None
        if running is None:
            return
        if running.process.returncode is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                kill_process_tree(running.process)
        shutil.rmtree(running.profile, ignore_errors=True)

    def _expire(self) -> None:
        self._idle = None
        if self._active == 0:
            self._expiry = asyncio.create_task(self.aclose(), name="web-fetch-browser-expiry")

    async def _ensure_running(self) -> _Running:
        async with self._lock:
            if self._running is not None and self._running.alive():
                return self._running
            await self._stop()
            self._running = await self._start()
            return self._running

    async def _start(self) -> _Running:
        browser = self._find()
        if browser is None:
            raise BrowserFetchError("no Chrome, Edge or Chromium is installed on the server")
        profile = self._profile_root / new_id("brw")
        await asyncio.to_thread(_prepare_profile, self._profile_root, profile)
        proxy = _GuardProxy()
        await proxy.start()
        process: asyncio.subprocess.Process | None = None
        try:
            process = await _launch(browser, profile, proxy.port)
            devtools = await _connect(process, profile)
            version = await devtools.call("Browser.getVersion")
        except BaseException as error:
            await proxy.aclose()
            if process is not None and process.returncode is None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    await kill_process_tree_async(process)
            await asyncio.to_thread(shutil.rmtree, profile, True)
            if isinstance(error, (OSError, TimeoutError, _DevToolsError)):
                _LOGGER.warning("Local browser %s did not start: %s", browser.name, error)
                raise BrowserFetchError(f"{browser.name} did not start ({error})") from error
            raise
        _LOGGER.debug("Local browser started: %s", browser.name)
        user_agent = str(version.get("userAgent", "")).replace("HeadlessChrome", "Chrome")
        return _Running(process, devtools, proxy, profile, user_agent)

    async def _stop(self) -> None:
        running, self._running = self._running, None
        if running is None:
            return
        with contextlib.suppress(_DevToolsError, TimeoutError, OSError):
            await running.devtools.call("Browser.close", timeout=3)
        await running.devtools.aclose()
        try:
            async with asyncio.timeout(5):
                await wait_for_exit(running.process)
        except TimeoutError:
            with contextlib.suppress(OSError, ProcessLookupError):
                await kill_process_tree_async(running.process)
        await running.proxy.aclose()
        await asyncio.to_thread(shutil.rmtree, running.profile, True)
        _LOGGER.debug("Local browser stopped")


def _prepare_profile(root: Path, profile: Path) -> None:
    """Remove profiles a killed server left behind, then create a fresh one."""
    with contextlib.suppress(FileNotFoundError):
        for entry in root.iterdir():
            shutil.rmtree(entry, ignore_errors=True)
    profile.mkdir(parents=True)


async def _launch(
    browser: LocalBrowser, profile: Path, proxy_port: int
) -> asyncio.subprocess.Process:
    argv = [
        browser.path,
        "--headless=new",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        # Without this, loopback addresses would bypass the guard proxy.
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--mute-audio",
        "--window-size=1366,900",
        "about:blank",
    ]
    if sys.platform == "win32":
        return await create_subprocess_exec(
            *argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess_creation_flags(new_process_group=True),
        )
    if os.geteuid() == 0:
        # Chromium refuses to run its sandbox as root (containers, root services).
        argv.insert(-1, "--no-sandbox")
    launch = guarded_process_launch(argv, env=dict(os.environ))
    return await create_subprocess_exec(
        *launch.argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=launch.pass_fds,
    )


async def _connect(process: asyncio.subprocess.Process, profile: Path) -> _DevTools:
    """Wait for the DevTools endpoint the browser announces in its profile."""
    from websockets.asyncio.client import connect

    port_file = profile / "DevToolsActivePort"
    async with asyncio.timeout(_START_SECONDS):
        while True:
            if process.returncode is not None:
                raise OSError(f"the browser exited with code {process.returncode}")
            with contextlib.suppress(OSError):
                lines = (await asyncio.to_thread(port_file.read_text, "utf-8")).splitlines()
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    break
            await asyncio.sleep(0.05)
        socket = await connect(
            f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}",
            max_size=_MAX_MESSAGE_BYTES,
            ping_interval=None,
            proxy=None,
            # The DevTools endpoint is plain ws:// on the loopback interface.
            ssl=None,
        )
    return _DevTools(socket)


async def _render(running: _Running, url: str) -> PublicResponse:
    devtools = running.devtools
    started = time.monotonic()
    context = (await devtools.call("Target.createBrowserContext", {"disposeOnDetach": True}))[
        "browserContextId"
    ]
    session: str | None = None
    try:
        await devtools.call(
            "Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": context}
        )
        target = await devtools.call(
            "Target.createTarget", {"url": "about:blank", "browserContextId": context}
        )
        session = (
            await devtools.call(
                "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
            )
        )["sessionId"]
        events = devtools.events(session)
        for method, params in (
            ("Page.enable", {}),
            ("Page.setLifecycleEventsEnabled", {"enabled": True}),
            ("Emulation.setUserAgentOverride", {"userAgent": running.user_agent}),
        ):
            await devtools.call(method, params, session=session)
        navigation = await devtools.call(
            "Page.navigate", {"url": url}, session=session, timeout=_FETCH_SECONDS
        )
        if error := navigation.get("errorText"):
            raise BrowserFetchError(
                _navigation_failure(str(error), running.proxy.refused_since(started))
            )
        await _settle(devtools, session, events, navigation["frameId"], started + _FETCH_SECONDS)
        page = await _evaluate(devtools, session, _PAGE_SCRIPT)
    finally:
        if session is not None:
            devtools.forget(session)
        with contextlib.suppress(_DevToolsError, TimeoutError):
            await devtools.call("Target.disposeBrowserContext", {"browserContextId": context})
    return _page_response(page)


async def _settle(
    devtools: _DevTools,
    session: str,
    events: asyncio.Queue[dict[str, Any]],
    frame: str,
    deadline: float,
) -> None:
    """Wait until the page stopped loading and changing, or the deadline passed.

    A page counts as settled once it is loaded, its text length held for one
    check after the network went almost idle (or for four checks without that
    signal), and it does not show a self-solving browser check.
    """
    idle = False
    stable = 0
    length = -1
    next_check = time.monotonic() + _POLL_SECONDS
    while (now := time.monotonic()) < deadline:
        if now >= next_check:
            next_check = now + _POLL_SECONDS
            state = await _evaluate(devtools, session, _STATE_SCRIPT)
            # A self-solving check page: keep waiting instead of settling on it.
            if str(state.get("title", "")).strip().lower() in SELF_SOLVING_CHECK_TITLES:
                stable, length = 0, -1
                continue
            current = state.get("length", 0)
            stable = stable + 1 if state.get("ready") == "complete" and current == length else 0
            length = current
            if stable >= (1 if idle else 4):
                return
            continue
        try:
            async with asyncio.timeout(min(next_check, deadline) - now):
                event = await events.get()
        except TimeoutError:
            continue
        method = event.get("method")
        if method in {"Inspector.detached", "Inspector.targetCrashed"}:
            raise BrowserFetchError("the browser tab closed or crashed while loading the page")
        params = event.get("params", {})
        if method == "Page.lifecycleEvent" and params.get("frameId") == frame:
            if params.get("name") == "init":
                idle, stable = False, 0
            elif params.get("name") in {"networkAlmostIdle", "networkIdle"}:
                idle = True


async def _evaluate(devtools: _DevTools, session: str, expression: str) -> dict[str, Any]:
    reply = await devtools.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session=session,
    )
    value = reply.get("result", {}).get("value")
    if reply.get("exceptionDetails") or not isinstance(value, dict):
        raise BrowserFetchError("the browser could not read the page")
    return value


def _navigation_failure(error: str, refused: tuple[str, str] | None) -> str:
    if refused is not None and refused[1] == "host_not_found":
        return f"the page leads to {refused[0]}, which could not be found (DNS lookup failed)"
    if refused is not None:
        return "the page leads to a private or local network address, which is not followed"
    if "ERR_ABORTED" in error:
        return "the address starts a download or was cancelled instead of showing a page"
    if "CERT" in error or "SSL" in error:
        return "it could not set up a secure connection to the site"
    if "TIMED_OUT" in error:
        return "the site did not answer in time"
    return f"the browser could not open the page ({error.removeprefix('net::')})"


def _page_response(page: dict[str, Any]) -> PublicResponse:
    final_url = str(page.get("url", ""))
    if urlsplit(final_url).scheme not in {"http", "https"}:
        raise BrowserFetchError("the browser ended on an error page instead of the site")
    status = page.get("status") or 200
    if not isinstance(status, int) or status >= 400:
        raise BrowserFetchError(f"the site answered HTTP {status} in the browser too")
    media_type = str(page.get("type", "")).lower()
    if media_type not in _PAGE_TYPES:
        raise BrowserFetchError(f"the browser opened a {media_type or 'non-page'} file")
    html = str(page.get("html", ""))
    return PublicResponse(
        status_code=status,
        headers={"content-type": "text/html; charset=utf-8"},
        text=html,
        url=final_url,
        content=html.encode("utf-8", "replace"),
    )


__all__ = ["BrowserFetchError", "LocalBrowser", "PageBrowser", "find_local_browser"]
