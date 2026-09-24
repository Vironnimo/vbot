"""Process-wide TLS verification context for outbound HTTP clients.

Constructing an ``httpx`` client without an explicit ``verify`` context builds
a fresh ``ssl.SSLContext`` and parses the complete CA bundle: 150-250 ms of
blocking work per client on Windows. Provider Adapters, OAuth refreshes, and
HTTP-calling Tools construct clients on the Event Loop, so under concurrent
Runs that work serialized every Agent behind it.

Every outbound ``httpx`` client and transport in the server therefore passes
``verify=shared_ssl_context()``. The context carries exactly httpx's default
verification (certifi bundle, or ``SSL_CERT_FILE``/``SSL_CERT_DIR`` from the
environment) and is built once per process. ``ssl.SSLContext`` is safe to
share between clients and threads; httpcore only (re)sets the HTTP/1.1 ALPN
list on it before each handshake, which is idempotent while no client enables
HTTP/2.
"""

from __future__ import annotations

import ssl
import threading

import httpx

_lock = threading.Lock()
_context: ssl.SSLContext | None = None


def shared_ssl_context() -> ssl.SSLContext:
    """Return the process-wide verified client context, building it on first use."""
    global _context
    context = _context
    if context is not None:
        return context
    with _lock:
        if _context is None:
            _context = httpx.create_ssl_context()
        return _context


def prewarm_shared_ssl_context() -> None:
    """Build the shared context on a daemon thread unless it already exists.

    CA parsing releases the GIL, so warming at startup keeps the first outbound
    request from paying the build cost on the Event Loop.
    """
    if _context is not None:
        return
    threading.Thread(
        target=shared_ssl_context,
        name="vbot-tls-prewarm",
        daemon=True,
    ).start()
