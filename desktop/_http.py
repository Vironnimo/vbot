"""Desktop-owned HTTP initialization shared by server probes and speech clients.

Keep the existing httpx verification policy with ``trust_env=False``: its
certificate bundle, without proxy or certificate overrides from the environment.
Only the verified TLS context is shared; callers still own their connections,
timeouts, retries and cancellation. Desktop never imports the server's TLS owner.
"""

from __future__ import annotations

import importlib
import logging
import math
import ssl
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

_lock = threading.Lock()
_context: ssl.SSLContext | None = None
_logger = logging.getLogger("vbot.desktop.http")


def shared_ssl_context() -> ssl.SSLContext:
    """Return one verified context, including when clients start concurrently."""
    global _context
    context = _context
    if context is not None:
        return context
    with _lock:
        if _context is None:
            _context = httpx.create_ssl_context(trust_env=False)
        return _context


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """The HTTP retry hint, as seconds or an HTTP date; invalid hints are ignored."""
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(raw)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            seconds = (deadline - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError, OverflowError:
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def prewarm_outbound_http() -> None:
    """Prepare HTTP while the window starts, before a dictation opens its client."""
    threading.Thread(target=_prewarm, name="vbot-desktop-http-prewarm", daemon=True).start()


def _prewarm() -> None:
    try:
        shared_ssl_context()
        # httpx otherwise imports these on the first client and async request.
        importlib.import_module("httpcore")
        importlib.import_module("anyio._backends._asyncio")
    except Exception:
        # A later client still reports the actual failure and may retry creation.
        _logger.warning("Desktop HTTP preparation failed", exc_info=True)
