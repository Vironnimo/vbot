"""What the page in the Desktop window may do through the bridge beyond a web page.

The installation's own server may use native operations. Other selected
servers need a native confirmation for protected operations. During a real
bridge call the document credential binds this policy to the invoking page.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from desktop._windows import url_origin
from desktop.server_address import parse_server_address

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
        self._server_list_origins: set[str] = set()
        self._sharing_status_origins: set[str] = set()
        self._document: ContextVar[tuple[str | None, Callable[[], bool]] | None] = ContextVar(
            "desktop_document", default=None
        )

    @contextmanager
    def document(self, origin: str | None, current: Callable[[], bool]) -> Iterator[None]:
        token = self._document.set((origin, current))
        try:
            yield
        finally:
            self._document.reset(token)

    @property
    def origin(self) -> str | None:
        return self._page_origin()

    def list_servers(self) -> None:
        self._ask_once(
            self._server_list_origins,
            "Show remembered servers",
            "{page} asks to read the addresses and sign-in status of the servers saved "
            "on this Desktop. Allow this until vBot Desktop closes?",
        )

    def read_sharing_status(self, server_url: str) -> None:
        if not server_url:
            raise PageActionDeclined("No selected server", error_code="computer_server_changed")
        self.computer_sharing(server_url, [])
        self._ask_once(
            self._sharing_status_origins,
            "Show computer sharing settings",
            "{page} asks to read this computer's name, installed native Extensions, "
            "available capabilities and sharing status. Allow this until vBot Desktop closes?",
        )

    def may_publish_sharing(self, server_url: str) -> bool:
        origin = url_origin(server_url)
        with self._lock:
            return origin is not None and origin in self._sharing_status_origins

    def select_server(self, server: str) -> None:
        self._ask(
            "Connect to a server",
            "{page} asks to switch vBot Desktop to {server}. Remember it and connect?",
            server=server,
        )

    def configure_dictation(self) -> None:
        self._ask(
            "Change Desktop dictation",
            "{page} asks to change the system-wide dictation "
            "shortcut or its settings. Dictation sends recordings to this server and "
            "pastes its transcript into the focused app. Allow this change?",
        )

    def configure_voice(self) -> None:
        self._ask(
            "Change Voice settings",
            "{page} asks to change wake phrase listening "
            "or how voice recordings and commands are handled. Allow this change?",
        )

    def configure_microphone(self) -> None:
        self._ask(
            "Change Desktop microphone",
            "{page} asks to change the microphone or "
            "echo settings used by Voice and dictation. Allow this change?",
        )

    def configure_hotkey(self) -> None:
        self._ask(
            "Change Live voice shortcut",
            "{page} asks to change the global Live voice keyboard shortcut. Allow this change?",
        )

    def delete_model(self, model: Any) -> None:
        self._ask(
            "Delete a wake word model",
            "{page} asks to delete the installed wake word "
            "model named {server} from this computer. Delete it?",
            server=_shown(model),
        )

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
            "can connect to it when selected. Add it?",
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
        self._ask_once(
            self._clipboard_origins,
            "Paste from the clipboard",
            "{page} asks to read the clipboard to paste it. Allow its pages to read the "
            "clipboard until vBot Desktop closes?",
        )

    def _ask_once(self, grants: set[str], title: str, message: str) -> None:
        origin = self._page_origin()
        if origin is None:
            raise PageActionDeclined(f"{title}: no loaded server page")
        with self._lock:
            if origin in grants:
                return
        self._ask(title, message, origin=origin)
        with self._lock:
            grants.add(origin)

    def write_clipboard(self) -> None:
        """Approve replacement of the host clipboard for this loaded page."""
        self._ask(
            "Copy to the clipboard",
            "{page} asks to replace the text on your clipboard. Allow this copy?",
        )

    def enable_voice(self) -> None:
        """Approve starting native Voice listening for this loaded page."""
        self._ask(
            "Start Voice listening",
            "{page} asks to start vBot Desktop's microphone listening. "
            "Voice recordings go to the connected server. Allow listening?",
        )

    def import_model(self, filename: Any) -> None:
        """Let the page install a wake word model; raises when declined."""
        self._ask(
            "Install a wake word model",
            "{page} asks to install the wake word model “{server}”. vBot Desktop runs it on "
            "what the microphone hears. Install it?",
            server=_shown(filename),
        )

    def computer_sharing(
        self, server_url: str, capabilities: list[str], renamed_to: str | None = None
    ) -> None:
        """Approve this server's native capabilities, never an intervening page's."""
        expected = parse_server_address(server_url.rstrip("/"), None).origin
        if self._page_origin() != expected:
            raise PageActionDeclined(
                "The selected server changed", error_code="computer_server_changed"
            )
        if capabilities:
            names = "; ".join(_shown(name) for name in capabilities)
            self._ask(
                "Share this computer",
                "{page} asks to use {server} on this computer while vBot Desktop is open. "
                "Allow this?",
                server=names,
                origin=expected,
                changed_code="computer_server_changed",
            )
        if renamed_to is not None:
            self._ask(
                "Rename this computer",
                "{page} asks to name this computer “{server}” for all its servers. Allow this?",
                server=_shown(renamed_to),
                origin=expected,
                changed_code="computer_server_changed",
            )
        if self._page_origin() != expected:
            raise PageActionDeclined(
                "The selected server changed", error_code="computer_server_changed"
            )

    def reload_desktop_extensions(self, server_url: str) -> None:
        """A remote page cannot reload native code without a local dialog."""
        self.computer_sharing(server_url, [])
        origin = parse_server_address(server_url.rstrip("/"), None).origin
        self._ask(
            "Reload Desktop Extensions",
            "{page} asks to reload Extensions installed on this computer. Updated Extensions "
            "keep their existing sharing permissions. Changed Extensions stop their current "
            "work; new Extensions still need sharing permission. Reload them?",
            origin=origin,
            changed_code="computer_server_changed",
        )
        self.computer_sharing(server_url, [])

    def _page_origin(self) -> str | None:
        """The calling page's origin; ``None`` when the window is between two pages."""
        origin = url_origin(self._page_url())
        document = self._document.get()
        if document is not None:
            expected, current = document
            return origin if origin == expected and current() else None
        with self._lock:
            return origin if origin == self._loaded_origin else None

    def _ask(
        self,
        title: str,
        message: str,
        *,
        server: str = "",
        origin: str | None = None,
        changed_code: str = PAGE_ACTION_DECLINED,
    ) -> None:
        current = self._page_origin()
        if current is None or origin is not None and current != origin:
            raise PageActionDeclined(f"{title}: no loaded server page", error_code=changed_code)
        origin = current
        if self._own_server(origin) and self._page_origin() == origin:
            return
        page = f"The page of {origin}"
        try:
            allowed = self._confirm(title, message.format(page=page, server=server))
        except Exception:
            logger.warning("The Desktop could not ask before a page action", exc_info=True)
            allowed = False
        if self._page_origin() != origin:
            raise PageActionDeclined(
                f"{title}: the server page changed",
                error_code=changed_code,
            )
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
