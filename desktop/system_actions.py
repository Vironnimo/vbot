"""Validated Desktop host actions unavailable to an ordinary Web page.

The WebUI may be served by a remote HTTP vBot server, where browser origin
policy makes clipboard access unreliable. This module owns the narrow native
boundary for clipboard text and opening safe web links in the user's default
browser, including the new windows a page opens (:func:`guard_page_windows`).
It stays independent of pywebview and serializes process-global host state
because bridge calls execute on separate threads.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

_MAX_CLIPBOARD_TEXT_CHARACTERS = 8 * 1024 * 1024
_MAX_EXTERNAL_URL_CHARACTERS = 8192
_CLIPBOARD_COMMAND_TIMEOUT_SECONDS = 5.0
_WINDOWS_CLIPBOARD_RETRIES = 5
_WINDOWS_CLIPBOARD_RETRY_DELAY_SECONDS = 0.01
#: pywebview's backends open a page's new window (``target="_blank"``,
#: ``window.open``) with ``webbrowser.open`` from their own module.
_WEBVIEW_BACKEND_PREFIX = "webview.platforms."

logger = logging.getLogger("vbot.desktop.system_actions")


class DesktopSystemActions:
    """Thread-safe native clipboard and external-browser boundary."""

    def __init__(
        self,
        *,
        clipboard_writer: Callable[[str], None] | None = None,
        clipboard_reader: Callable[[], str] | None = None,
        external_url_opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._clipboard_writer = clipboard_writer or _write_system_clipboard
        self._clipboard_reader = clipboard_reader or _read_system_clipboard
        self._external_url_opener = external_url_opener or _open_external_url
        self._lock = threading.Lock()

    def set_clipboard_text(self, value: Any) -> None:
        """Replace the host clipboard with validated plain text."""
        text = _validated_clipboard_text(value)
        with self._lock:
            self._clipboard_writer(text)

    def get_clipboard_text(self) -> str:
        """Return validated plain text from the host clipboard."""
        with self._lock:
            text = self._clipboard_reader()
        return _validated_clipboard_text(text)

    def open_external_url(self, value: Any) -> None:
        """Open one validated HTTP(S) URL in the default browser."""
        url = _validated_external_url(value)
        with self._lock:
            opened = self._external_url_opener(url)
        if not opened:
            raise RuntimeError("The default browser could not open the link")


class _PageWindowBrowser:
    """Stands in for ``webbrowser`` in pywebview's backends: opens web links only."""

    def __init__(self, actions: DesktopSystemActions) -> None:
        self._actions = actions

    def open(self, url: Any, *_args: Any, **_kwargs: Any) -> bool:
        try:
            self._actions.open_external_url(str(url))
        except ValueError:
            scheme = urlsplit(str(url)).scheme if isinstance(url, str) else ""
            logger.warning("Desktop did not open a page's new window (scheme=%s)", scheme[:20])
            return False
        except RuntimeError:
            logger.warning("The default browser could not open a page's new window")
            return False
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(webbrowser, name)


def guard_page_windows(
    actions: DesktopSystemActions, modules: Mapping[str, Any] | None = None
) -> int:
    """Open every new window a page asks for through ``actions``, and nothing but web links.

    pywebview hands the URL of such a window to ``webbrowser.open``, which
    would also start a ``file:``, ``ms-settings:`` or any other registered
    handler. This replaces ``webbrowser`` in each pywebview backend module
    imported so far (``modules``, by default ``sys.modules``); call it once the
    window exists. Returns how many backend modules are guarded.
    """
    guarded = 0
    for name, module in list((sys.modules if modules is None else modules).items()):
        if not name.startswith(_WEBVIEW_BACKEND_PREFIX):
            continue
        current = getattr(module, "webbrowser", None)
        if current is webbrowser:
            module.webbrowser = _PageWindowBrowser(actions)  # type: ignore[attr-defined]
            current = module.webbrowser
        if isinstance(current, _PageWindowBrowser):
            guarded += 1
    return guarded


def _validated_clipboard_text(value: Any) -> str:
    """Accept bounded plain text at the native clipboard boundary."""
    if not isinstance(value, str):
        raise ValueError("Clipboard content must be plain text")
    if len(value) > _MAX_CLIPBOARD_TEXT_CHARACTERS:
        raise ValueError("Clipboard content exceeds the Desktop size limit")
    return value


def _validated_external_url(value: Any) -> str:
    """Accept an absolute HTTP(S) URL and reject executable/local schemes."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("External link must be an absolute HTTP(S) URL")
    if len(value) > _MAX_EXTERNAL_URL_CHARACTERS:
        raise ValueError("External link exceeds the Desktop size limit")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("External link must be an absolute HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("External link must be an absolute HTTP(S) URL")
    return value


def _open_external_url(url: str) -> bool:
    """Launch a validated URL in the host's default browser."""
    return webbrowser.open(url, new=2, autoraise=True)


def _write_system_clipboard(text: str) -> None:
    """Write host clipboard text without relying on the page's origin policy."""
    if sys.platform == "win32":
        write_windows_clipboard(text)
        return
    command = _clipboard_command(write=True)
    subprocess.run(
        command,
        input=text,
        text=True,
        encoding="utf-8",
        check=True,
        capture_output=True,
        timeout=_CLIPBOARD_COMMAND_TIMEOUT_SECONDS,
    )


def _read_system_clipboard() -> str:
    """Read host clipboard text without relying on the page's origin policy."""
    if sys.platform == "win32":
        return read_windows_clipboard()
    command = _clipboard_command(write=False)
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        check=True,
        capture_output=True,
        timeout=_CLIPBOARD_COMMAND_TIMEOUT_SECONDS,
    )
    return result.stdout


def _clipboard_command(*, write: bool) -> list[str]:
    """Resolve an installed native clipboard helper for macOS/Linux."""
    if sys.platform == "darwin":
        return ["pbcopy" if write else "pbpaste"]

    candidates = (
        (["wl-copy"], ["wl-paste", "--no-newline"]),
        (["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]),
        (["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]),
    )
    for write_command, read_command in candidates:
        command = write_command if write else read_command
        if shutil.which(command[0]):
            return command
    raise RuntimeError("No supported system clipboard helper is installed")


def _windows_clipboard_libraries() -> tuple[Any, Any, Any]:
    """Load and type the small Win32 clipboard API surface lazily."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    return ctypes, user32, kernel32


def _open_windows_clipboard(ctypes: Any, user32: Any) -> None:
    """Open the shared Windows clipboard with a short bounded retry."""
    for attempt in range(_WINDOWS_CLIPBOARD_RETRIES):
        if user32.OpenClipboard(None):
            return
        if attempt + 1 < _WINDOWS_CLIPBOARD_RETRIES:
            time.sleep(_WINDOWS_CLIPBOARD_RETRY_DELAY_SECONDS)
    raise ctypes.WinError(ctypes.get_last_error())


def write_windows_clipboard(text: str) -> None:
    """Publish UTF-16 text through the native Windows clipboard API."""
    ctypes, user32, kernel32 = _windows_clipboard_libraries()
    clipboard_format_unicode_text = 13
    global_memory_moveable = 0x0002
    encoded = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(global_memory_moveable, len(encoded))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    ownership_transferred = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)

        _open_windows_clipboard(ctypes, user32)
        try:
            if not user32.EmptyClipboard():
                raise ctypes.WinError(ctypes.get_last_error())
            if not user32.SetClipboardData(clipboard_format_unicode_text, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            ownership_transferred = True
        finally:
            user32.CloseClipboard()
    finally:
        if not ownership_transferred:
            kernel32.GlobalFree(handle)


def read_windows_clipboard() -> str:
    """Read UTF-16 text through the native Windows clipboard API."""
    ctypes, user32, kernel32 = _windows_clipboard_libraries()
    clipboard_format_unicode_text = 13
    if not user32.IsClipboardFormatAvailable(clipboard_format_unicode_text):
        return ""

    _open_windows_clipboard(ctypes, user32)
    try:
        handle = user32.GetClipboardData(clipboard_format_unicode_text)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return cast("str", ctypes.wstring_at(pointer))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
