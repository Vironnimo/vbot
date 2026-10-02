"""Tests for the process-wide outbound TLS context."""

from __future__ import annotations

import ast
import ssl
import threading
from pathlib import Path

import pytest

from core.utils import tls

_REPO_ROOT = Path(__file__).parents[3]
_HTTPX_CONSTRUCTORS = frozenset({"AsyncClient", "Client", "AsyncHTTPTransport", "HTTPTransport"})
# Everything that runs inside the server process, bundled Extensions included.
_SERVER_SOURCE_ROOTS = ("core", "server", "resources/extensions")


def _server_modules(marker: str) -> list[tuple[str, ast.Module]]:
    modules: list[tuple[str, ast.Module]] = []
    for package in _SERVER_SOURCE_ROOTS:
        for source_path in (_REPO_ROOT / package).rglob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            if marker not in source:
                continue
            relative = source_path.relative_to(_REPO_ROOT).as_posix()
            modules.append((relative, ast.parse(source, filename=str(source_path))))
    return modules


def _reset_shared_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wait out any prewarm thread a bootstrapped Runtime left building in this process.
    tls.shared_ssl_context()
    monkeypatch.setattr(tls, "_context", None)


def test_shared_context_is_one_verifying_context() -> None:
    context = tls.shared_ssl_context()

    assert tls.shared_ssl_context() is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # The operating system's trusted CAs count as well as the certifi bundle.
    system = ssl.create_default_context()
    trusted = context.get_ca_certs(binary_form=True)
    assert all(ca in trusted for ca in system.get_ca_certs(binary_form=True))


def test_concurrent_first_use_builds_the_context_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_shared_context(monkeypatch)
    release = threading.Event()
    builds: list[ssl.SSLContext] = []

    def slow_build() -> ssl.SSLContext:
        release.wait(timeout=5)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        builds.append(context)
        return context

    monkeypatch.setattr(tls.httpx, "create_ssl_context", slow_build)
    results: list[ssl.SSLContext] = []
    threads = [
        threading.Thread(target=lambda: results.append(tls.shared_ssl_context())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(builds) == 1
    assert results == [builds[0]] * 8


def test_prewarm_builds_and_imports_off_the_calling_thread_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_shared_context(monkeypatch)
    build_threads: list[str] = []
    imports: list[tuple[str, str]] = []
    done = threading.Event()

    def build() -> ssl.SSLContext:
        build_threads.append(threading.current_thread().name)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def import_module(name: str) -> None:
        imports.append((threading.current_thread().name, name))
        if len(imports) == len(tls._LAZY_TRANSPORT_MODULES):  # noqa: SLF001 - the prewarm list.
            done.set()

    monkeypatch.setattr(tls.httpx, "create_ssl_context", build)
    monkeypatch.setattr(tls.importlib, "import_module", import_module)

    tls.prewarm_outbound_http()
    assert done.wait(timeout=5)
    context = tls.shared_ssl_context()

    assert build_threads == ["vbot-http-prewarm"]
    assert imports == [
        ("vbot-http-prewarm", "httpcore"),
        ("vbot-http-prewarm", "anyio._backends._asyncio"),
    ]
    assert tls.shared_ssl_context() is context


def test_every_server_httpx_client_uses_an_explicit_tls_context() -> None:
    """A bare httpx client re-parses the CA bundle, blocking the Event Loop per client.

    ``httpx2`` (the MCP SDK's client) would instead verify through the system trust
    store, unlike every other outbound client.
    """
    missing: list[str] = []
    for relative, tree in _server_modules("httpx"):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id in {"httpx", "httpx2"}):
                continue
            if node.func.attr not in _HTTPX_CONSTRUCTORS:
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            if not keywords & {"verify", "transport"}:
                missing.append(f"{relative}:{node.lineno}")

    assert missing == []


def test_every_server_websocket_connection_passes_a_tls_context() -> None:
    """websockets builds a fresh default context per ``wss://`` connection without ``ssl``."""
    missing: list[str] = []
    for relative, tree in _server_modules("websockets.asyncio.client"):
        connect_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "websockets.asyncio.client"
            for alias in node.names
            if alias.name == "connect"
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in connect_names:
                continue
            if "ssl" not in {keyword.arg for keyword in node.keywords}:
                missing.append(f"{relative}:{node.lineno}")

    assert missing == []
