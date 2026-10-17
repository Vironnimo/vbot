"""Protection of a Secret Store's data key.

Each store has one random 256-bit data key. A :class:`KeyProtection` creates it
and makes it available again later:

- ``dpapi`` (Windows): the key is wrapped with DPAPI for the current user and
  kept, wrapped, in the store document itself. A copy of the data directory is
  useless on another computer or under another account.
- ``key_file`` (every other system): the key lives in a file outside the data
  directory, ``~/.config/vbot/keys/<store id>.key``, owner-only;
  ``VBOT_SECRET_KEY_FILE`` names the file instead (systemd credentials,
  containers). Backups of the data directory then hold no usable secret.

A new key file is always created in the default directory, which every lookup
searches whatever the caller's environment: an update's verification start
inherits the shell that ran ``vbot update``, and the service started later
must find the key all the same. A lookup also searches
``$XDG_CONFIG_HOME/vbot/keys`` (when absolute), where earlier builds created
keys. ``VBOT_SECRET_KEY_FILE`` stays explicit: with it, only that file is used.

A protection never overwrites an existing key file: the file is created
exclusively, and a key that cannot be found or opened makes the store
unavailable, never replaced.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from core.secrets._crypto import is_data_key, new_data_key, new_identifier
from core.utils import dpapi
from core.utils.file_status import exists_strict
from core.utils.owner_only import OWNER_ONLY_MODE, restrict_to_owner

KEY_FILE_ENVIRONMENT_VARIABLE = "VBOT_SECRET_KEY_FILE"
KEY_FILE_FORMAT_VERSION = 1
DPAPI_PROTECTION = "dpapi"
KEY_FILE_PROTECTION = "key_file"
_KEY_FILE_SUFFIX = ".key"


class KeyUnavailableError(Exception):
    """The data key of a store cannot be created, found, or opened.

    ``transient`` marks a key file that could not be checked or read (an I/O
    error), which a later attempt may not repeat. A missing, wrong or
    unopenable key is not transient.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class CreatedKey:
    """A new data key and the protection fields the store document records."""

    key: bytes = field(repr=False)
    key_id: str
    record: Mapping[str, str] = field(default_factory=dict)
    location: str | None = None


class KeyProtection(Protocol):
    """Creates a store's data key and opens it again."""

    @property
    def name(self) -> str:
        """The ``protection`` value recorded in the store document."""
        ...

    def create(self, store_id: str) -> CreatedKey:
        """Create (or adopt a provisioned) data key for a new store."""
        ...

    def load(self, store_id: str, key_id: str, record: Mapping[str, Any]) -> bytes:
        """Return the data key; raises :class:`KeyUnavailableError`."""
        ...

    def location(self, store_id: str) -> str | None:
        """Where the key lives, for operators; never the key itself."""
        ...

    def locations(self, store_id: str) -> tuple[str, ...]:
        """Every place a lookup of the store's key searches, in order."""
        ...

    def new_key_location(self) -> str:
        """Where the key of a new store is created, for operators."""
        ...


def default_key_protections() -> tuple[KeyProtection, ...]:
    """The protections of this system; the first one protects new stores."""
    if sys.platform == "win32":
        return (DpapiKeyProtection(), KeyFileProtection())
    return (KeyFileProtection(),)


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------


class DpapiKeyProtection:
    """Wraps the data key with DPAPI for the current Windows user."""

    @property
    def name(self) -> str:
        return DPAPI_PROTECTION

    def create(self, store_id: str) -> CreatedKey:
        key = new_data_key()
        key_id = new_identifier()
        wrapped = _dpapi_protect(key, _dpapi_entropy(store_id, key_id))
        return CreatedKey(
            key=key,
            key_id=key_id,
            record={"wrapped": base64.b64encode(wrapped).decode("ascii")},
            location=self.location(store_id),
        )

    def load(self, store_id: str, key_id: str, record: Mapping[str, Any]) -> bytes:
        wrapped = record.get("wrapped")
        if not isinstance(wrapped, str):
            raise KeyUnavailableError("the store records no DPAPI-wrapped key")
        try:
            blob = base64.b64decode(wrapped.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise KeyUnavailableError("the DPAPI-wrapped key is not valid base64") from exc
        key = _dpapi_unprotect(blob, _dpapi_entropy(store_id, key_id))
        if not is_data_key(key):
            raise KeyUnavailableError("the DPAPI-wrapped key has an unexpected length")
        return key

    def location(self, store_id: str) -> str | None:
        return _dpapi_location()

    def locations(self, store_id: str) -> tuple[str, ...]:
        return (_dpapi_location(),)

    def new_key_location(self) -> str:
        return _dpapi_location()


def _dpapi_location() -> str:
    """The key location of this Windows account; another account cannot unwrap the key."""
    account = _windows_account()
    holder = "the current user" if account is None else f"the user {account}"
    return f"Windows DPAPI of {holder}, wrapped in secrets.json"


@functools.cache
def _windows_account() -> str | None:
    """This process's Windows account as ``DOMAIN\\user``, or ``None`` when unknown."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    from typing import cast

    native = cast(Any, ctypes)
    try:
        secur32 = native.WinDLL("secur32", use_last_error=True)
        function = secur32.GetUserNameExW
        function.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        function.restype = wintypes.BOOLEAN
        size = wintypes.ULONG(256)
        buffer = ctypes.create_unicode_buffer(size.value)
        # 2: NameSamCompatible.
        if not function(2, buffer, ctypes.byref(size)):
            return None
    except OSError, AttributeError:
        return None
    return buffer.value or None


def _dpapi_entropy(store_id: str, key_id: str) -> bytes:
    return f"vbot-secret-store:{store_id}:{key_id}".encode()


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
    if not dpapi.available():
        raise KeyUnavailableError("Windows DPAPI is not available on this system")
    return _dpapi_call(data, entropy, protect=True)


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    if not dpapi.available():
        raise KeyUnavailableError(
            "the key is protected by Windows DPAPI, which is not available on this system"
        )
    return _dpapi_call(data, entropy, protect=False)


def _dpapi_call(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    try:
        return dpapi.protect(data, entropy) if protect else dpapi.unprotect(data, entropy)
    except dpapi.DpapiError as exc:
        action = "wrap" if protect else "unwrap"
        raise KeyUnavailableError(
            f"Windows DPAPI could not {action} the key (error {exc.code}); a key wrapped for "
            "another Windows account or computer cannot be opened here"
        ) from exc


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------


class KeyFileProtection:
    """Keeps the data key in an owner-only file outside the data directory.

    ``path`` names the key file directly (as ``VBOT_SECRET_KEY_FILE`` does);
    ``directories`` replace the key directories, the first one taking new keys.
    Without either, both are read from the process environment at each call.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        directories: Sequence[Path] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._path = path
        self._directories = tuple(directories) if directories is not None else None
        self._environ = environ

    @property
    def name(self) -> str:
        return KEY_FILE_PROTECTION

    def create(self, store_id: str) -> CreatedKey:
        explicit = self._explicit_path()
        if explicit is not None and _exists(explicit):
            # A provisioned key (systemd credential, container secret) is adopted.
            return _adopt(explicit)
        # Never where the caller's environment points: the first directory is the
        # default one, which every lookup searches.
        target = explicit if explicit is not None else self._candidates(store_id)[0]
        key = new_data_key()
        key_id = new_identifier()
        try:
            _write_key_file(target, key_id, key)
        except FileExistsError as exc:
            if explicit is None:
                raise KeyUnavailableError(
                    f"a key file already exists at {target}; it is never overwritten"
                ) from exc
            # Another store created the named key file meanwhile; it is shared.
            return _adopt(explicit)
        return CreatedKey(key=key, key_id=key_id, location=str(target))

    def load(self, store_id: str, key_id: str, record: Mapping[str, Any]) -> bytes:
        candidates = self._candidates(store_id)
        mismatches: list[str] = []
        for candidate in candidates:
            if not _exists(candidate):
                continue
            found_id, key = _read_key_file(candidate)
            if found_id == key_id:
                return key
            mismatches.append(f"{candidate} holds key {found_id}")
        if mismatches:
            raise KeyUnavailableError(
                f"no key file holds the store's key {key_id}: " + "; ".join(mismatches)
            )
        looked = ", ".join(str(candidate) for candidate in candidates)
        raise KeyUnavailableError(f"the key file is missing (looked for {looked})")

    def location(self, store_id: str) -> str | None:
        candidates = self._candidates(store_id)
        for candidate in candidates:
            try:
                if exists_strict(candidate):
                    return str(candidate)
            except OSError:
                return str(candidate)
        return str(candidates[0])

    def locations(self, store_id: str) -> tuple[str, ...]:
        return tuple(str(candidate) for candidate in self._candidates(store_id))

    def new_key_location(self) -> str:
        explicit = self._explicit_path()
        return str(explicit if explicit is not None else self._key_directories()[0])

    def _explicit_path(self) -> Path | None:
        if self._path is not None:
            return self._path
        if self._directories is not None:
            return None
        value = self._env().get(KEY_FILE_ENVIRONMENT_VARIABLE, "")
        return Path(value).expanduser() if value else None

    def _candidates(self, store_id: str) -> list[Path]:
        explicit = self._explicit_path()
        if explicit is not None:
            return [explicit]
        name = f"{store_id}{_KEY_FILE_SUFFIX}"
        return [directory / name for directory in self._key_directories()]

    def _key_directories(self) -> list[Path]:
        if self._directories is not None:
            return list(self._directories)
        # The default directory comes first and takes every new key: a service
        # unit and an interactive shell may disagree about XDG_CONFIG_HOME, and
        # the key must be found by both.
        directories = [Path.home() / ".config" / "vbot" / "keys"]
        configured = self._env().get("XDG_CONFIG_HOME", "")
        # The XDG specification ignores a relative value. Earlier builds created
        # keys there, so a lookup still searches it.
        if configured and Path(configured).is_absolute():
            legacy = Path(configured) / "vbot" / "keys"
            if legacy not in directories:
                directories.append(legacy)
        return directories

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ


def _adopt(path: Path) -> CreatedKey:
    key_id, key = _read_key_file(path)
    return CreatedKey(key=key, key_id=key_id, location=str(path))


def _exists(path: Path) -> bool:
    try:
        return exists_strict(path)
    except OSError as exc:
        raise KeyUnavailableError(
            f"the key file {path} cannot be checked right now: {exc}", transient=True
        ) from exc


def _read_key_file(path: Path) -> tuple[str, bytes]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KeyUnavailableError(f"the key file {path} is missing") from exc
    except UnicodeError as exc:
        raise KeyUnavailableError(f"the key file {path} cannot be read: {exc}") from exc
    except OSError as exc:
        raise KeyUnavailableError(
            f"the key file {path} cannot be read right now: {exc}", transient=True
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise KeyUnavailableError(f"the key file {path} is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format_version") != KEY_FILE_FORMAT_VERSION
        or not isinstance(payload.get("key_id"), str)
        or not payload["key_id"]
        or not isinstance(payload.get("key"), str)
    ):
        raise KeyUnavailableError(f"the key file {path} is not a vBot secret key file")
    try:
        key = base64.b64decode(payload["key"].encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise KeyUnavailableError(f"the key file {path} holds no valid key") from exc
    if not is_data_key(key):
        raise KeyUnavailableError(f"the key file {path} holds a key of the wrong length")
    return payload["key_id"], key


def _write_key_file(path: Path, key_id: str, key: bytes) -> None:
    """Create ``path`` holding the key; ``FileExistsError`` when anything is there.

    The key is written owner-only into a staging file beside the target and
    published with a hard link, which never replaces an entry: two stores
    creating the same file never overwrite each other's key, and a reader
    never sees a partial key. Where hard links are unsupported, the target is
    created exclusively and written in place.
    """
    payload = {
        "format_version": KEY_FILE_FORMAT_VERSION,
        "key_id": key_id,
        "key": base64.b64encode(key).decode("ascii"),
    }
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        _write_new(staging, data)
        try:
            os.link(staging, path)
        except FileExistsError:
            raise
        except OSError:
            # No hard links on this filesystem: create the target itself exclusively.
            _write_new(path, data)
        finally:
            with suppress(OSError):
                staging.unlink()
        if os.name == "posix":
            _fsync_directory(path.parent)
    except FileExistsError:
        raise
    except OSError as exc:
        raise KeyUnavailableError(f"the key file {path} cannot be written: {exc}") from exc


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively, owner-only before any content, and write ``data``."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, OWNER_ONLY_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            restrict_to_owner(path)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise


def _fsync_directory(path: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
