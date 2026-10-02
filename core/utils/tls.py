"""Process-wide TLS verification context for outbound HTTP clients.

Constructing an ``httpx`` client without an explicit ``verify`` context builds
a fresh ``ssl.SSLContext`` and parses the complete CA bundle: 150-250 ms of
blocking work per client on Windows. Provider Adapters, OAuth refreshes, and
HTTP-calling Tools construct clients on the Event Loop, so under concurrent
Runs that work serialized every Agent behind it.

Every outbound ``httpx`` client and transport in the server therefore passes
``verify=shared_ssl_context()``, and so do the ``httpx2`` clients of the MCP
SDK. The context carries httpx's default verification (certifi bundle, or
``SSL_CERT_FILE``/``SSL_CERT_DIR`` from the environment) plus the operating
system's trusted CAs (``load_default_certs``: the Windows ROOT and CA stores,
OpenSSL's default paths elsewhere), so servers behind a private CA the system
trusts verify too. It is built once per process; the system store adds about
25 ms to that build and nothing per handshake. ``truststore`` (httpx2's
default) is not used: it switches the shared context to unverified for the
duration of each handshake, which races across threads.

``ssl.SSLContext`` is safe to share between clients and threads;
httpcore and httpcore2 only (re)set the HTTP/1.1 ALPN list on it before each
handshake, which is idempotent while no client enables HTTP/2.

httpx also imports its connection stack lazily: the first client imports
httpcore, h11 and anyio, and the first request anyio's asyncio backend. On a
cold start that took 380 ms on the Event Loop at the first Run, so the startup
prewarm imports them too.
"""

from __future__ import annotations

import importlib
import ssl
import sys
import threading

import httpx

_lock = threading.Lock()
_context: ssl.SSLContext | None = None
# What httpx imports on its first client and first request.
_LAZY_TRANSPORT_MODULES = ("httpcore", "anyio._backends._asyncio")


def shared_ssl_context() -> ssl.SSLContext:
    """Return the process-wide verified client context, building it on first use."""
    global _context
    context = _context
    if context is not None:
        return context
    with _lock:
        if _context is None:
            context = httpx.create_ssl_context()
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
            _context = context
        return _context


def prewarm_outbound_http() -> None:
    """Build the shared context and import httpx's lazy modules on a daemon thread.

    CA parsing and import file access release the GIL, so warming at startup
    keeps the first outbound client and request from paying either on the Event
    Loop. A client created while the thread still runs waits only for the rest.
    """
    if _context is not None and all(module in sys.modules for module in _LAZY_TRANSPORT_MODULES):
        return
    threading.Thread(
        target=_prewarm,
        name="vbot-http-prewarm",
        daemon=True,
    ).start()


def _prewarm() -> None:
    shared_ssl_context()
    for module in _LAZY_TRANSPORT_MODULES:
        importlib.import_module(module)
