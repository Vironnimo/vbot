"""Give the installed Desktop shortcuts the AppUserModelID of the Desktop window.

A shortcut starts the stable ``vBot.GUI.exe``, while the window belongs to a
versioned ``vBot.Desktop.exe``. Only one shared AppUserModelID lets the taskbar
place that window on the pinned button. The installer writes it into the
shortcuts it creates, but updates do not run the installer and a pinned shortcut
is a copy of its own, so every Desktop launch completes what it finds.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

assert sys.platform == "win32"

_LOGGER = logging.getLogger("vbot.application.integration")

#: Keep equal to ``desktop._windows.DESKTOP_APP_ID`` and ``vbot.iss``.
DESKTOP_APP_ID = "vBot.Desktop"
_SHORTCUT_NAME = "vBot Desktop.lnk"
_SHORTCUT_ARGUMENTS = "desktop"
_SHORTCUT_FOLDERS = (
    Path("Microsoft", "Windows", "Start Menu", "Programs", "vBot"),
    Path("Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar"),
)
_PATH_CHARACTERS = 32768
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_STGM_READWRITE = 2
_VT_LPWSTR = 31
_SHCNE_UPDATEITEM = 0x2000
_SHCNF_PATHW = 0x0005
# COM method positions in each interface's vtable, IUnknown's three first.
_QUERY_INTERFACE, _RELEASE = 0, 2
_LINK_GET_PATH, _LINK_GET_ARGUMENTS = 3, 10
_FILE_LOAD, _FILE_SAVE = 5, 6
_STORE_GET_VALUE, _STORE_SET_VALUE, _STORE_COMMIT = 5, 6, 7


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("format", _Guid), ("identifier", wintypes.DWORD)]


class _PropVariant(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort * 3),
        ("text", ctypes.c_void_p),
        ("second_word", ctypes.c_void_p),
    ]


_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_Guid)]
_ole32.CLSIDFromString.restype = ctypes.HRESULT
_ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(_Guid),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_Guid),
    ctypes.POINTER(ctypes.c_void_p),
]
_ole32.CoCreateInstance.restype = ctypes.HRESULT
_ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PropVariant)]
_ole32.PropVariantClear.restype = ctypes.HRESULT
_shell32 = ctypes.WinDLL("shell32")
_shell32.SHChangeNotify.argtypes = [ctypes.c_long, wintypes.UINT, wintypes.LPCWSTR, ctypes.c_void_p]
_shell32.SHChangeNotify.restype = None


def _guid(text: str) -> _Guid:
    guid = _Guid()
    _ole32.CLSIDFromString(text, ctypes.byref(guid))
    return guid


_CLSID_SHELL_LINK = _guid("{00021401-0000-0000-C000-000000000046}")
_IID_SHELL_LINK = _guid("{000214F9-0000-0000-C000-000000000046}")
_IID_PERSIST_FILE = _guid("{0000010B-0000-0000-C000-000000000046}")
_IID_PROPERTY_STORE = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
_APP_USER_MODEL_ID_KEY = _PropertyKey(_guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)


def _vtable(interface: ctypes.c_void_p) -> Any:
    return ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents


def _method(interface: ctypes.c_void_p, index: int, *argtypes: Any) -> Callable[..., None]:
    """Bind one vtable method; a failing HRESULT raises ``OSError``."""
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    function = prototype(_vtable(interface)[index])
    return lambda *arguments: function(interface, *arguments)


def _release(interface: ctypes.c_void_p) -> None:
    prototype = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    prototype(_vtable(interface)[_RELEASE])(interface)


@contextmanager
def _query(interface: ctypes.c_void_p, iid: _Guid) -> Iterator[ctypes.c_void_p]:
    """Hold another COM interface of the same object."""
    other = ctypes.c_void_p()
    _method(interface, _QUERY_INTERFACE, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))(
        ctypes.byref(iid), ctypes.byref(other)
    )
    try:
        yield other
    finally:
        _release(other)


@contextmanager
def _shell_link() -> Iterator[ctypes.c_void_p]:
    """Hold a new shell link object."""
    link = ctypes.c_void_p()
    _ole32.CoCreateInstance(
        ctypes.byref(_CLSID_SHELL_LINK),
        None,
        _CLSCTX_INPROC_SERVER,
        ctypes.byref(_IID_SHELL_LINK),
        ctypes.byref(link),
    )
    try:
        yield link
    finally:
        _release(link)


def _starts_desktop(link: ctypes.c_void_p, root: Path) -> bool:
    target = ctypes.create_unicode_buffer(_PATH_CHARACTERS)
    arguments = ctypes.create_unicode_buffer(_PATH_CHARACTERS)
    get_path = _method(
        link, _LINK_GET_PATH, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    )
    get_path(target, _PATH_CHARACTERS, None, 0)
    _method(link, _LINK_GET_ARGUMENTS, wintypes.LPWSTR, ctypes.c_int)(arguments, _PATH_CHARACTERS)
    expected = os.path.normcase(os.path.realpath(root / "vBot.GUI.exe"))
    return (
        os.path.normcase(os.path.realpath(target.value)) == expected
        and arguments.value.strip() == _SHORTCUT_ARGUMENTS
    )


def _app_id(store: ctypes.c_void_p) -> str | None:
    value = _PropVariant()
    get_value = _method(
        store, _STORE_GET_VALUE, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)
    )
    get_value(ctypes.byref(_APP_USER_MODEL_ID_KEY), ctypes.byref(value))
    try:
        if value.kind == _VT_LPWSTR and value.text:
            return ctypes.wstring_at(value.text)
        return None
    finally:
        _ole32.PropVariantClear(ctypes.byref(value))


def _set_app_id(store: ctypes.c_void_p, app_id: str) -> None:
    text = ctypes.create_unicode_buffer(app_id)
    value = _PropVariant(kind=_VT_LPWSTR, text=ctypes.addressof(text))
    set_value = _method(
        store, _STORE_SET_VALUE, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)
    )
    set_value(ctypes.byref(_APP_USER_MODEL_ID_KEY), ctypes.byref(value))
    _method(store, _STORE_COMMIT)()


def _claim_shortcut(path: Path, root: Path) -> None:
    """Write the identity into a shortcut that starts this installation's Desktop."""
    with (
        _shell_link() as link,
        _query(link, _IID_PERSIST_FILE) as file,
        _query(link, _IID_PROPERTY_STORE) as store,
    ):
        _method(file, _FILE_LOAD, wintypes.LPCWSTR, wintypes.DWORD)(str(path), _STGM_READWRITE)
        if not _starts_desktop(link, root) or _app_id(store) == DESKTOP_APP_ID:
            return
        _set_app_id(store, DESKTOP_APP_ID)
        _method(file, _FILE_SAVE, wintypes.LPCWSTR, wintypes.BOOL)(str(path), True)
    _shell32.SHChangeNotify(_SHCNE_UPDATEITEM, _SHCNF_PATHW, str(path), None)
    _LOGGER.info("Desktop shortcut identity written (path=%s)", path)


def claim_desktop_shortcuts(root: Path) -> None:
    """Complete the Start menu and taskbar shortcuts of the installation at ``root``.

    A shortcut that cannot be read or written only keeps its own taskbar button,
    so a failure never stops the Desktop launch that called this.
    """
    profile = os.environ.get("APPDATA")
    if not profile:
        return
    initialized = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    try:
        for folder in _SHORTCUT_FOLDERS:
            path = Path(profile) / folder / _SHORTCUT_NAME
            if not path.is_file():
                continue
            try:
                _claim_shortcut(path, root)
            except OSError:
                _LOGGER.warning("Could not update the Desktop shortcut %s", path, exc_info=True)
    finally:
        if initialized >= 0:
            _ole32.CoUninitialize()
