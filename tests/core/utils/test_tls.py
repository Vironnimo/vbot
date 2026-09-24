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


def _reset_shared_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wait out any prewarm thread a bootstrapped Runtime left building in this process.
    tls.shared_ssl_context()
    monkeypatch.setattr(tls, "_context", None)


def test_shared_context_is_one_verifying_context() -> None:
    context = tls.shared_ssl_context()

    assert tls.shared_ssl_context() is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


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


def test_prewarm_builds_off_the_calling_thread_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_shared_context(monkeypatch)
    built = threading.Event()
    build_threads: list[str] = []

    def build() -> ssl.SSLContext:
        build_threads.append(threading.current_thread().name)
        built.set()
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(tls.httpx, "create_ssl_context", build)

    tls.prewarm_shared_ssl_context()
    assert built.wait(timeout=5)
    context = tls.shared_ssl_context()
    tls.prewarm_shared_ssl_context()

    assert build_threads == ["vbot-tls-prewarm"]
    assert tls.shared_ssl_context() is context


def test_every_server_httpx_client_uses_an_explicit_tls_context() -> None:
    """A bare httpx client re-parses the CA bundle, blocking the Event Loop per client."""
    missing: list[str] = []
    for package in ("core", "server"):
        for source_path in (_REPO_ROOT / package).rglob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            if "httpx." not in source:
                continue
            tree = ast.parse(source, filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                target = node.func.value
                if not (isinstance(target, ast.Name) and target.id == "httpx"):
                    continue
                if node.func.attr not in _HTTPX_CONSTRUCTORS:
                    continue
                keywords = {keyword.arg for keyword in node.keywords}
                if not keywords & {"verify", "transport"}:
                    relative = source_path.relative_to(_REPO_ROOT).as_posix()
                    missing.append(f"{relative}:{node.lineno}")

    assert missing == []
