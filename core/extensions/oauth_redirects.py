"""Browser redirects the server receives for the OAuth sign-ins of Extensions.

An Extension that sends the user's browser to an authorization server names
``callback_url`` as its redirect URI and waits for the browser to come back with
``expect(state)``. The server's ``GET /api/oauth/callback`` route passes the
query to ``deliver``. Only a pending, unexpired ``state`` matches, each one
once: the state is the sign-in's unguessable request value, so a stray or forged
request can neither complete nor cancel another sign-in.

``callback_url`` is ``None`` while no server receives redirects (no server, or
one that only listens on an address the browser on this machine cannot use as
a loopback redirect); a sign-in then needs another way back, such as the user
pasting the redirected address.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping

CALLBACK_PATH = "/api/oauth/callback"
# How long a sign-in may wait for the browser to come back.
DEFAULT_TTL_SECONDS = 600.0


def _settle(future: asyncio.Future[dict[str, str]], params: dict[str, str] | None) -> None:
    if future.done():
        return
    if params is None:
        future.cancel()
    else:
        future.set_result(params)


class OAuthRedirects:
    """Pending sign-ins by OAuth ``state``, completed by the server's callback route."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[asyncio.Future[dict[str, str]], float]] = {}
        self._callback_url: str | None = None

    @property
    def callback_url(self) -> str | None:
        """The redirect URI that reaches this server, or ``None`` without one."""
        return self._callback_url

    def bind(self, callback_url: str | None) -> None:
        """Set (or with ``None`` clear) the URL the server receives redirects at."""
        self._callback_url = callback_url

    def expect(self, state: str) -> asyncio.Future[dict[str, str]]:
        """A future for the redirect query carrying *state*; ``discard`` it when done."""
        if not state:
            raise ValueError("An OAuth redirect needs a state value")
        future: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        with self._lock:
            now = self._clock()
            for stale in [key for key, (_, deadline) in self._pending.items() if deadline < now]:
                pending, _ = self._pending.pop(stale)
                pending.get_loop().call_soon_threadsafe(_settle, pending, None)
            if state in self._pending:
                raise ValueError("This OAuth state is already awaited")
            self._pending[state] = (future, now + self._ttl)
        return future

    def discard(self, state: str) -> None:
        """Stop waiting for *state*; a later redirect carrying it matches nothing."""
        with self._lock:
            entry = self._pending.pop(state, None)
        if entry is not None:
            entry[0].get_loop().call_soon_threadsafe(_settle, entry[0], None)

    def deliver(self, params: Mapping[str, str]) -> bool:
        """Complete the sign-in awaiting ``params["state"]``; ``False`` when none matches."""
        state = params.get("state")
        if not state:
            return False
        with self._lock:
            entry = self._pending.pop(state, None)
            expired = entry is not None and entry[1] < self._clock()
        if entry is None:
            return False
        future = entry[0]
        future.get_loop().call_soon_threadsafe(_settle, future, None if expired else dict(params))
        return not expired
