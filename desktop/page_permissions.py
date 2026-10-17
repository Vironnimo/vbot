"""What the page in the Desktop window may do through the bridge beyond a web page.

Every server the Desktop connects to serves the page in its one window, and
that page reaches the bridge (:mod:`desktop.bridge`). A server's page is
trusted only as far as the user chose it:

- The page of the installation's own server (the one the Desktop signs in to
  with the local access token) is trusted like the Desktop itself: that server
  already runs with the user's rights on this computer.
- Any other server's page needs the user's native confirmation to add or
  remove a remembered server (selecting one that is not remembered adds it),
  to read the clipboard (asked once per server origin while the Desktop runs)
  and to install a wake word model.

Connecting, pairing and forgetting a sign-in are no page's business at all:
only the Desktop's own connection screen may call them
(:attr:`desktop.connection.ConnectionController.screen_key`). The microphone
is granted without a prompt only to the window's server, which the user added
(on the connection screen, on the command line, or through one of these
confirmations).

The page's origin is the window's top-level origin, read at the call. It
counts only while it is also the origin of the page that last finished loading
(:meth:`PagePermissions.page_loaded`): a page that calls the bridge and at once
navigates the window to the own server is not taken for that server's page,
since the window's URL changes when the next document commits, well before it
has loaded. A page in between is named "A page" and is asked for.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from collections.abc import Callable
from typing import Any

from desktop._windows import url_origin

logger = logging.getLogger("vbot.desktop.page_permissions")

PAGE_ACTION_DECLINED = "declined"
#: The bridge method may be called only by the Desktop's connection screen.
CONNECTION_SCREEN_ONLY = "connection_screen_only"
_SHOWN_TEXT_LIMIT = 80
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class PageActionDeclined(Exception):  # noqa: N818 - names the outcome
    """The user, or the policy, did not let the page do this; ``error_code`` says which."""

    def __init__(self, message: str, *, error_code: str = PAGE_ACTION_DECLINED) -> None:
        super().__init__(message)
        self.error_code = error_code


class PagePermissions:
    """Ask the user before another server's page acts on this Desktop; thread-safe.

    ``page_url`` returns the window's top-level URL (``None`` before there is
    one), ``own_server`` whether an origin is the installation's own server,
    ``confirm`` shows a native yes/no dialog and returns the answer.
    """

    def __init__(
        self,
        *,
        page_url: Callable[[], str | None],
        own_server: Callable[[str], bool],
        confirm: Callable[[str, str], bool],
    ) -> None:
        self._page_url = page_url
        self._own_server = own_server
        self._confirm = confirm
        self._lock = threading.Lock()
        self._loaded_origin: str | None = None
        self._clipboard_origins: set[str] = set()

    def page_loaded(self) -> None:
        """Note the origin of the page that finished loading (pywebview ``loaded``)."""
        origin = url_origin(self._page_url())
        with self._lock:
            self._loaded_origin = origin

    def add_server(self, server: str) -> None:
        """Let the page remember ``server`` (its display form); raises when declined."""
        self._ask(
            "Add a server",
            "{page} asks to add {server} to vBot Desktop's servers. vBot Desktop then "
            "connects to it when you or a page select it, and lets its pages use the "
            "microphone. Add it?",
            server=server,
        )

    def remove_server(self, server: str) -> None:
        """Let the page forget ``server`` and this Desktop's sign-in there; raises when declined."""
        self._ask(
            "Remove a server",
            "{page} asks to remove {server} from vBot Desktop's servers, together with "
            "this Desktop's sign-in there. Remove it?",
            server=server,
        )

    def read_clipboard(self) -> None:
        """Let the page read the clipboard; asked once per origin while the Desktop runs."""
        origin = self._page_origin()
        if origin is not None and self._own_server(origin):
            return
        with self._lock:
            if origin is not None and origin in self._clipboard_origins:
                return
        self._ask(
            "Paste from the clipboard",
            "{page} asks to read the clipboard to paste it. Allow its pages to read the "
            "clipboard until vBot Desktop closes?",
            origin=origin,
        )
        if origin is not None:
            with self._lock:
                self._clipboard_origins.add(origin)

    def import_model(self, filename: Any) -> None:
        """Let the page install a wake word model; raises when declined."""
        self._ask(
            "Install a wake word model",
            "{page} asks to install the wake word model “{server}”. vBot Desktop runs it on "
            "what the microphone hears. Install it?",
            server=_shown(filename),
        )

    def _page_origin(self) -> str | None:
        """The calling page's origin; ``None`` when the window is between two pages."""
        origin = url_origin(self._page_url())
        with self._lock:
            return origin if origin == self._loaded_origin else None

    def _ask(self, title: str, message: str, *, server: str = "", **known: Any) -> None:
        origin = known["origin"] if "origin" in known else self._page_origin()
        if origin is not None and self._own_server(origin):
            return
        page = f"The page of {origin}" if origin is not None else "A page"
        try:
            allowed = self._confirm(title, message.format(page=page, server=server))
        except Exception:
            logger.warning("The Desktop could not ask before a page action", exc_info=True)
            allowed = False
        if not allowed:
            logger.info("A page action was declined (%s, origin=%s)", title, origin)
            raise PageActionDeclined(f"{title}: declined")


def _shown(value: Any) -> str:
    """Page-supplied text for a dialog: one line, no hidden characters, bounded."""
    text = value if isinstance(value, str) else ""
    text = " ".join(
        "".join(
            " " if unicodedata.category(character) in _HIDDEN_CATEGORIES else character
            for character in text
        ).split()
    )
    if len(text) > _SHOWN_TEXT_LIMIT:
        text = text[: _SHOWN_TEXT_LIMIT - 1] + "…"
    return text or "unnamed"
