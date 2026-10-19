"""The Desktop's stored sign-ins: one device token per server origin.

A Desktop paired with a server in the access mode ``authenticated`` keeps the
device token it received in ``device-tokens.json`` beside the Desktop
settings, never in ``settings.json``. On Windows the token is wrapped with
DPAPI for the current Windows user (bound to its server origin); on other
platforms the file is owner-only (0600). Neither protects against another
process of the same user, which could read either store, so the Desktop adds no
keyring dependency.

Beside the token, a record keeps the device id and when it was paired. The
document carries ``format_version``; readers tolerate and writers keep fields
they do not know (earlier versions also kept ``webview_device_id``). Tokens never
appear in logs or in exceptions.

The tray of a Desktop Client reads this file through :class:`DeviceTokenStore`
to sign in as the Desktop (``cli/application/desktop.py``); only the Desktop
writes it.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import ctypes
import json
import logging
import os
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from desktop import _windows

logger = logging.getLogger("vbot.desktop.credentials")

DEVICE_TOKENS_FILE_NAME = "device-tokens.json"
FORMAT_VERSION = 1
_SERVERS_KEY = "servers"
_TOKEN_KEY = "token"
_DPAPI_TOKEN_KEY = "token_dpapi"
_DEVICE_ID_KEY = "device_id"
_PAIRED_AT_KEY = "paired_at"
DEVICE_TOKEN_PATTERN = re.compile(r"vbt_(dev_[0-9a-z]{16})_[A-Za-z0-9_-]{43}")
_DPAPI_UI_FORBIDDEN = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: never prompt, fail instead.


class CredentialStoreError(OSError):
    """The token store could not be written, or the token could not be protected."""


@dataclass(frozen=True)
class StoredSignIn:
    """What the Desktop keeps for one server origin (the token stays out of its repr)."""

    token: str | None = field(repr=False)
    device_id: str | None


class DeviceTokenStore:
    """Read and write ``device-tokens.json``; safe to share between threads.

    The Desktop is a single instance per config directory, so the decoded
    document is cached after the first read and every write replaces the file
    atomically.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._document: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def get(self, origin: str) -> StoredSignIn | None:
        """Return the stored sign-in of ``origin``, or ``None``."""
        with self._lock:
            record = self._servers().get(origin)
            return _stored(origin, record) if isinstance(record, dict) else None

    def save_device(self, origin: str, token: str, device_id: str) -> None:
        """Store a newly paired device token for ``origin`` (replacing an earlier one)."""
        match = DEVICE_TOKEN_PATTERN.fullmatch(token)
        if match is None or match.group(1) != device_id:
            raise ValueError("Not a device token of this device")
        protected = _protect(token, origin)

        def change(record: dict[str, Any]) -> None:
            record.pop(_TOKEN_KEY, None)
            record.pop(_DPAPI_TOKEN_KEY, None)
            record.update(protected)
            record[_DEVICE_ID_KEY] = device_id
            record[_PAIRED_AT_KEY] = datetime.now(UTC).isoformat(timespec="seconds")

        self._update(origin, change)

    def drop_device(self, origin: str, token: str) -> bool:
        """Remove the device token of ``origin`` if it still is ``token``; reports a removal."""
        with self._lock:
            record = self._servers().get(origin)
            stored = _stored(origin, record) if isinstance(record, dict) else None
            if stored is None or stored.token != token:
                return False
            document = copy.deepcopy(self._loaded(for_write=True))
            record = document[_SERVERS_KEY][origin]
            for key in (_TOKEN_KEY, _DPAPI_TOKEN_KEY, _DEVICE_ID_KEY, _PAIRED_AT_KEY):
                record.pop(key, None)
            if not record:
                del document[_SERVERS_KEY][origin]
            self._write(document)
            return True

    def remove(self, origin: str) -> StoredSignIn | None:
        """Forget everything stored for ``origin``; returns what was stored."""
        with self._lock:
            servers = self._servers()
            record = servers.get(origin)
            if not isinstance(record, dict):
                return None
            stored = _stored(origin, record)
            document = copy.deepcopy(self._loaded(for_write=True))
            del document[_SERVERS_KEY][origin]
            self._write(document)
            return stored

    # -- Internals ---------------------------------------------------------------

    def _update(self, origin: str, change: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            document = copy.deepcopy(self._loaded(for_write=True))
            servers = document.get(_SERVERS_KEY)
            if not isinstance(servers, dict):
                servers = document[_SERVERS_KEY] = {}
            record = servers.get(origin)
            if not isinstance(record, dict):
                record = servers[origin] = {}
            change(record)
            if not record:
                del servers[origin]
            self._write(document)

    def _servers(self) -> dict[str, Any]:
        servers = self._loaded().get(_SERVERS_KEY)
        return servers if isinstance(servers, dict) else {}

    def _loaded(self, *, for_write: bool = False) -> dict[str, Any]:
        if self._document is not None:
            return self._document
        try:
            text: str | None = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        except OSError as exc:
            # Possibly transient (a lock held by a scanner): read as signed out,
            # but never replace a file that could not be read.
            logger.warning("Desktop sign-ins could not be read (path=%s): %s", self._path, exc)
            if for_write:
                raise CredentialStoreError("The stored sign-ins could not be read") from None
            return _empty_document()
        document = _empty_document() if text is None else _parsed(text, self._path)
        self._document = document
        return document

    def _write(self, document: dict[str, Any]) -> None:
        document["format_version"] = document.get("format_version", FORMAT_VERSION)
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # mkstemp creates the file owner-only (0600) where modes exist.
            descriptor, temporary = tempfile.mkstemp(
                dir=directory, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                if sys.platform != "win32":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, self._path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError as exc:
            logger.error("Desktop sign-ins could not be saved (path=%s): %s", self._path, exc)
            raise CredentialStoreError("The sign-in could not be saved") from None
        self._document = document


def _empty_document() -> dict[str, Any]:
    return {"format_version": FORMAT_VERSION, _SERVERS_KEY: {}}


def _parsed(text: str, path: Path) -> dict[str, Any]:
    """Decode the document; a damaged one reads as empty and the next write replaces it."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        logger.warning("Desktop sign-ins are damaged and start empty (path=%s)", path)
        return _empty_document()
    return data


def _stored(origin: str, record: dict[str, Any]) -> StoredSignIn:
    device_id = record.get(_DEVICE_ID_KEY)
    token = _unprotect(record, origin)
    if token is not None:
        match = DEVICE_TOKEN_PATTERN.fullmatch(token)
        if match is None or match.group(1) != device_id:
            token = None
    return StoredSignIn(token=token, device_id=device_id if token is not None else None)


# -- Token protection ------------------------------------------------------------


def _protect(token: str, origin: str) -> dict[str, str]:
    """Return the record fields that keep ``token`` on this platform."""
    if sys.platform == "win32":
        blob = _dpapi(token.encode("ascii"), _entropy(origin), protect=True)
        return {_DPAPI_TOKEN_KEY: base64.b64encode(blob).decode("ascii")}
    return {_TOKEN_KEY: token}


def _unprotect(record: dict[str, Any], origin: str) -> str | None:
    """Return the stored token, or ``None`` when there is none this platform can open."""
    wrapped = record.get(_DPAPI_TOKEN_KEY)
    if isinstance(wrapped, str) and sys.platform == "win32":
        try:
            blob = base64.b64decode(wrapped.encode("ascii"), validate=True)
            return _dpapi(blob, _entropy(origin), protect=False).decode("ascii")
        except UnicodeError, binascii.Error, CredentialStoreError:
            logger.warning(
                "The stored sign-in for %s cannot be opened by this Windows user", origin
            )
            return None
    plain = record.get(_TOKEN_KEY)
    return plain if isinstance(plain, str) else None


def _entropy(origin: str) -> bytes:
    # Binds each wrapped token to its server origin.
    return f"vbot-desktop-device-token:{origin}".encode()


def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    """Wrap or unwrap ``data`` with DPAPI for the current Windows user."""
    if sys.platform != "win32":
        raise CredentialStoreError("Windows DPAPI is not available on this system")
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = (
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        )

    try:
        crypt32 = _windows.win32_library("crypt32")
        kernel32 = _windows.win32_library("kernel32")
    except OSError as exc:
        raise CredentialStoreError("Windows DPAPI could not be loaded") from exc
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char)))
    entropy_blob = DataBlob(
        len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char))
    )
    output_blob = DataBlob()
    if not function(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _DPAPI_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        action = "protect" if protect else "unprotect"
        raise CredentialStoreError(
            f"Windows DPAPI could not {action} the sign-in (error {_windows.win32_last_error()})"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
