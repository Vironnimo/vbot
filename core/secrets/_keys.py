"""Protection of a Secret Store's data key.

Each store has one random 256-bit data key. A :class:`KeyProtection` creates it
and makes it available again later:

- ``dpapi`` (Windows): the key is wrapped with DPAPI for the current user and
  kept, wrapped, in the store document itself. A copy of the data directory is
  useless on another computer or under another account.
- ``key_file`` (every other system): the key lives in a file outside the data
  directory, ``$XDG_CONFIG_HOME/vbot/keys/<store id>.key`` (default
  ``~/.config/vbot/keys``), owner-only; ``VBOT_SECRET_KEY_FILE`` names the file
  instead (systemd credentials, containers). Backups of the data directory then
  hold no usable secret.

A protection never overwrites an existing key file: a key that cannot be found
or opened makes the store unavailable, never replaced.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core.secrets._crypto import is_data_key, new_data_key, new_identifier
from core.utils import dpapi
from core.utils.atomic import atomic_write_text
from core.utils.file_status import exists_strict

KEY_FILE_ENVIRONMENT_VARIABLE = "VBOT_SECRET_KEY_FILE"
KEY_FILE_FORMAT_VERSION = 1
DPAPI_PROTECTION = "dpapi"
KEY_FILE_PROTECTION = "key_file"
_KEY_FILE_SUFFIX = ".key"


class KeyUnavailableError(Exception):
    """The data key of a store cannot be created, found, or opened."""


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
        return "Windows DPAPI (current user), wrapped in secrets.json"


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
    ``directories`` replace the default key directories. Without either, both
    are read from the process environment at each call.
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
        if explicit is not None:
            if _exists(explicit):
                # A provisioned key (systemd credential, container secret) is adopted.
                key_id, key = _read_key_file(explicit)
                return CreatedKey(key=key, key_id=key_id, location=str(explicit))
            target = explicit
        else:
            target = self._candidates(store_id)[0]
            if _exists(target):
                raise KeyUnavailableError(
                    f"a key file already exists at {target}; it is never overwritten"
                )
        key = new_data_key()
        key_id = new_identifier()
        _write_key_file(target, key_id, key)
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
        directories: list[Path] = []
        configured = self._env().get("XDG_CONFIG_HOME", "")
        # The XDG specification ignores a relative value.
        if configured and Path(configured).is_absolute():
            directories.append(Path(configured) / "vbot" / "keys")
        # Also the default location: a service unit and an interactive shell may
        # disagree about XDG_CONFIG_HOME, and the key must be found by both.
        default = Path.home() / ".config" / "vbot" / "keys"
        if default not in directories:
            directories.append(default)
        return directories

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ


def _exists(path: Path) -> bool:
    try:
        return exists_strict(path)
    except OSError as exc:
        raise KeyUnavailableError(f"the key file {path} cannot be checked: {exc}") from exc


def _read_key_file(path: Path) -> tuple[str, bytes]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise KeyUnavailableError(f"the key file {path} cannot be read: {exc}") from exc
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
    payload = {
        "format_version": KEY_FILE_FORMAT_VERSION,
        "key_id": key_id,
        "key": base64.b64encode(key).decode("ascii"),
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, indent=2) + "\n", mode=0o600)
    except OSError as exc:
        raise KeyUnavailableError(f"the key file {path} cannot be written: {exc}") from exc
