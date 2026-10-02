"""``GET /api/oauth/callback``: where the browser comes back from an Extension's OAuth sign-in.

The authorization server redirects the user's browser here with the sign-in's
``state`` and its ``code`` (or ``error``). The route hands the query to the
Runtime's ``OAuthRedirects``, which completes only the sign-in waiting for that
``state``, once, and answers with a small page that sends the user back to vBot.
The page loads nothing and is never cached: its address carries the code.

The redirect URI is this server's loopback address, so it works for a browser
on the machine the server runs on. A server listening only on another address
publishes none; sign-in then falls back to pasting the redirected address.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any

from core.extensions.oauth_redirects import CALLBACK_PATH, OAuthRedirects
from server._bind import ServerBindState
from server._http_dependencies import Response

_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_RECEIVED = (
    "Sign-in received",
    "vBot received the sign-in response. You can close this tab and return to vBot.",
)
_UNKNOWN = (
    "Sign-in not found",
    "vBot is not waiting for this sign-in: it expired, finished already, or was "
    "cancelled. Start the sign-in again in vBot.",
)


def callback_url(server_bind: ServerBindState) -> str | None:
    """The redirect URI a browser on this machine reaches the server at, if any."""
    host = server_bind["listen_host"].removeprefix("[").removesuffix("]")
    if host in {"", "*", "0.0.0.0", "127.0.0.1"}:
        address = "127.0.0.1"
    elif host in {"::", "::1"}:
        address = "[::1]"
    elif host == "localhost":
        address = "localhost"
    else:
        return None
    return f"http://{address}:{server_bind['listen_port']}{CALLBACK_PATH}"


def _page(title: str, message: str, status_code: int) -> Response:
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
        "padding:0 1rem;line-height:1.5}</style></head>"
        f"<body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return Response(
        body, status_code=status_code, headers=_HEADERS, media_type="text/html; charset=utf-8"
    )


def oauth_callback_response(
    redirects: OAuthRedirects | None, query: Iterable[tuple[str, str]]
) -> Response:
    """Deliver the redirect *query* to the sign-in waiting for its state and answer the browser.

    A parameter given twice is refused (RFC 6749 section 3.1): which copy counts
    would otherwise depend on the parser.
    """
    params: dict[str, Any] = {}
    for key, value in query:
        if key in params:
            return _page(*_UNKNOWN, 400)
        params[key] = value
    if redirects is None or not redirects.deliver(params):
        return _page(*_UNKNOWN, 400)
    return _page(*_RECEIVED, 200)
