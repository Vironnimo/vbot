"""The Secret Store: credentials and OAuth tokens encrypted at rest.

One Generation 1 JSON document, ``<data-dir>/secrets.json`` (owner-only), holds
every entry sealed on its own with AES-256-GCM under the store's data key
(:mod:`core.secrets._crypto`); the key is protected per system
(:mod:`core.secrets._keys`). The document holds only ciphertext, so it is a
member of data snapshots and configuration backups like any other document.

A store is in one of three states:

- ``absent``: no document. Reads are empty; the first write creates the store
  and its key.
- ``available``: the document loads and the key opens its key check. An entry
  that does not authenticate is damaged: it reads as missing, is reported, and
  stays untouched until it is replaced or removed explicitly.
- ``unavailable``: the document cannot be read or validated, or its key is
  missing, wrong or cannot be opened. Reads are empty, every write is refused,
  and nothing is ever overwritten; only :meth:`SecretStore.reset`, an explicit
  operator command, moves the store to quarantine and starts a new one.

A read that fails with an I/O error (a scanner or backup tool holding the file,
a key file that cannot be read) is retried briefly; when it keeps failing, the
store reads as unavailable for that access only and the next access tries
again. A document that does not validate, or a key that is missing or wrong,
stays unavailable until the document changes.

Every write holds ``<data-dir>/secrets.lock``, an OS-owned file lock, around
its read-modify-write, so writers in different processes (the server, the
CLI, an update's verification start) never lose each other's changes.

Entry values are never logged.
"""

from __future__ import annotations

import copy
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import IO, Any, Final, Literal

from core.config_validation import (
    JsonValidationReport,
    add_error,
    read_json_file,
)
from core.json_documents import (
    JsonDocumentWriteError,
    document_change,
    write_json_document,
)
from core.secrets._crypto import (
    SealedEntryError,
    associated_data,
    fingerprint,
    key_check,
    key_matches,
    new_identifier,
    open_sealed,
    seal,
)
from core.secrets._document import (
    CREDENTIALS as CREDENTIALS,
)
from core.secrets._document import (
    ENTRY_NAME_PATTERN as ENTRY_NAME_PATTERN,
)
from core.secrets._document import (
    OAUTH_TOKENS as OAUTH_TOKENS,
)
from core.secrets._document import (
    PLAINTEXT_IMPORT_FIELD as PLAINTEXT_IMPORT_FIELD,
)
from core.secrets._document import (
    SECRET_STORE_FILE_NAME as SECRET_STORE_FILE_NAME,
)
from core.secrets._document import (
    SECRET_STORE_FORMAT as SECRET_STORE_FORMAT,
)
from core.secrets._document import (
    SECRET_STORE_FORMAT_VERSION as SECRET_STORE_FORMAT_VERSION,
)
from core.secrets._document import (
    SECRET_STORE_SHAPE as SECRET_STORE_SHAPE,
)
from core.secrets._document import (
    SECTIONS as SECTIONS,
)
from core.secrets._document import (
    Section as Section,
)
from core.secrets._document import (
    _decode_document,
)
from core.secrets._document import (
    validate_secret_store_data as validate_secret_store_data,
)
from core.secrets._keys import (
    KeyProtection,
    KeyUnavailableError,
    default_key_protections,
)
from core.utils.errors import StorageError
from core.utils.file_status import stat_or_none
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger

#: The quarantine child of a store that ``reset`` replaced.
QUARANTINE_NAME = "secret-store"
#: The inter-process write lock beside the document.
LOCK_FILE_NAME = "secrets.lock"

# A document modified this recently may change again within the same filesystem
# timestamp tick, so its stat stamp cannot prove it unchanged yet.
_RACY_WINDOW_NS = 3_000_000_000
# A read that fails with an I/O error is repeated after these delays; scanners
# and backup tools release a file within milliseconds.
_READ_RETRY_DELAYS = (0.05, 0.2)
# How long a write waits for another process's write to finish.
_WRITE_LOCK_TIMEOUT_SECONDS = 30.0
_WRITE_LOCK_POLL_SECONDS = 0.05
# Tests patch this seam instead of the process-wide ``time.sleep``.
_sleep = time.sleep

_LOGGER = get_logger("secrets")

SecretStoreState = Literal["absent", "available", "unavailable"]

#: Whether credential names ignore case, as environment-variable names do on Windows.
CREDENTIAL_NAMES_IGNORE_CASE: Final = sys.platform == "win32"
#: A credential name: an environment-variable name, since consumers resolve
#: credentials by it and Storage, the RPCs and the CLI accept only such names.
#: Stricter than ``ENTRY_NAME_PATTERN``, which also admits OAuth token ids.
CREDENTIAL_NAME_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,199}$")


def credential_name_key(name: str) -> str:
    """The name under which ``name`` is one credential on this system.

    Uppercase on Windows, where the process environment ignores case and
    ``os.environ`` reports names uppercased; ``name`` itself elsewhere.
    """
    return name.upper() if CREDENTIAL_NAMES_IGNORE_CASE else name


def fold_credential_names(entries: Mapping[str, str]) -> dict[str, str]:
    """One entry per credential of ``entries``, chosen the same way on every call.

    On Windows, names differing only in case are one credential: its
    all-uppercase spelling wins, otherwise the first spelling in sorted order.
    Elsewhere every name is its own credential.
    """
    if not CREDENTIAL_NAMES_IGNORE_CASE:
        return dict(entries)
    chosen: dict[str, str] = {}
    for name in sorted(entries):
        key = credential_name_key(name)
        current = chosen.get(key)
        if current is None or (name == key and current != key):
            chosen[key] = name
    return {name: entries[name] for name in sorted(chosen.values())}


class SecretStoreError(StorageError):
    """A Secret Store operation failed; nothing was changed."""


class SecretStoreUnavailableError(SecretStoreError):
    """The store cannot be read or opened; it is never written in this state."""


class SecretEntryUnavailableError(SecretStoreError):
    """One stored entry does not decrypt (damaged or altered)."""


@dataclass(frozen=True)
class SecretStoreStatus:
    """What an operator needs to know about a store; never a value."""

    state: SecretStoreState
    reason: str | None = None
    store_id: str | None = None
    protection: str | None = None
    key_location: str | None = None
    credentials: int = 0
    oauth_tokens: int = 0
    #: Entries that do not decrypt, as ``<section>/<name>``.
    damaged: tuple[str, ...] = ()
    #: Whether an import of plaintext credential files still waits to be finished.
    import_pending: bool = False
    #: Every place this process searches for the store's key, in order.
    key_locations: tuple[str, ...] = ()
    #: Where this process would create the key of a new store (``reset``).
    new_key_location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "store_id": self.store_id,
            "protection": self.protection,
            "key_location": self.key_location,
            "key_locations": list(self.key_locations),
            "new_key_location": self.new_key_location,
            "credentials": self.credentials,
            "oauth_tokens": self.oauth_tokens,
            "damaged": list(self.damaged),
            "import_pending": self.import_pending,
        }


@dataclass(frozen=True)
class SecretStoreReset:
    """The outcome of :meth:`SecretStore.reset`."""

    quarantine: Path | None
    status: SecretStoreStatus


@dataclass
class _View:
    """One loaded version of the document with its decrypted entries."""

    state: SecretStoreState
    stamp: tuple[int, int, int] | None = None
    reason: str | None = None
    document: dict[str, Any] | None = None
    key: bytes | None = field(default=None, repr=False)
    values: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    damaged: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: An I/O failure a later read may not repeat; such a view is never cached.
    transient: bool = False

    @property
    def store_id(self) -> str | None:
        return None if self.document is None else self.document.get("store_id")

    @property
    def key_record(self) -> dict[str, Any]:
        record = None if self.document is None else self.document.get("key")
        return record if isinstance(record, dict) else {}


class SecretTransaction:
    """A change of one store, applied by :meth:`SecretStore.update` as one write."""

    def __init__(
        self,
        document: dict[str, Any],
        key: bytes | None,
        values: dict[str, dict[str, str]],
        create: Callable[[dict[str, Any]], bytes] | None = None,
    ) -> None:
        self._document = document
        self._key = key
        self._values = values
        self._create = create
        self.changed = False
        #: Whether this transaction created the store (and its key).
        self.created = False

    def ensure_created(self) -> None:
        """Create the store now if it is absent; a store is created only on a change."""
        self._require_key()

    def names(self, section: Section) -> tuple[str, ...]:
        """Every stored name of ``section``, damaged entries included."""
        return tuple(sorted(self._entries(section)))

    def get(self, section: Section, name: str) -> str | None:
        """The value of one entry; :class:`SecretEntryUnavailableError` when damaged."""
        if name not in self._entries(section):
            return None
        value = self._values.get(section, {}).get(name)
        if value is None:
            raise SecretEntryUnavailableError(f"the stored {section} entry {name} does not decrypt")
        return value

    def set(self, section: Section, name: str, value: str) -> bool:
        """Store ``value`` under ``name``; whether the stored value changed."""
        _check_name(name)
        if not isinstance(value, str):
            raise TypeError("a secret value must be a string")
        entries = self._entries(section)
        if name in entries and self._values.get(section, {}).get(name) == value:
            return False
        key = self._require_key()
        entries[name] = seal(key, value.encode("utf-8"), self._aad(section, name))
        self._values.setdefault(section, {})[name] = value
        self.changed = True
        return True

    def remove(self, section: Section, name: str) -> bool:
        """Remove one entry, damaged or not; whether it existed."""
        entries = self._entries(section)
        if name not in entries:
            return False
        del entries[name]
        self._values.get(section, {}).pop(name, None)
        self.changed = True
        return True

    @property
    def import_record(self) -> dict[str, Any] | None:
        record = self._document.get(PLAINTEXT_IMPORT_FIELD)
        return copy.deepcopy(record) if isinstance(record, dict) else None

    def set_import_record(self, record: Mapping[str, Any] | None) -> None:
        current = self._document.get(PLAINTEXT_IMPORT_FIELD)
        if record is None:
            if current is not None:
                del self._document[PLAINTEXT_IMPORT_FIELD]
                self.changed = True
            return
        if current != record:
            self._require_key()
            self._document[PLAINTEXT_IMPORT_FIELD] = copy.deepcopy(dict(record))
            self.changed = True

    def fingerprint(self, content: bytes) -> str:
        """A keyed digest of plaintext content; creates the store when absent."""
        return fingerprint(self._require_key(), content)

    @property
    def document(self) -> dict[str, Any]:
        return self._document

    def _require_key(self) -> bytes:
        if self._key is None:
            if self._create is None:
                raise SecretStoreError("the Secret Store has no key")
            self._key = self._create(self._document)
            self.created = True
            self.changed = True
        return self._key

    def _entries(self, section: Section) -> dict[str, Any]:
        if section not in SECTIONS:
            raise ValueError(f"unknown Secret Store section: {section}")
        entries = self._document.setdefault(section, {})
        assert isinstance(entries, dict)
        return entries

    def _aad(self, section: str, name: str) -> bytes:
        key_record = self._document["key"]
        return associated_data(self._document["store_id"], key_record["id"], section, name)


class SecretStore:
    """Owns ``<data-dir>/secrets.json``: one instance per data directory and process.

    Thread-safe: every read and write holds one lock, and every write re-reads
    the document first while it holds the inter-process write lock.
    ``protections`` are the key protections this process can open; the first
    one protects a new store.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        protections: Sequence[KeyProtection] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.path = self.data_dir / SECRET_STORE_FILE_NAME
        self._protections = tuple(
            protections if protections is not None else default_key_protections()
        )
        if not self._protections:
            raise ValueError("a Secret Store needs at least one key protection")
        self._lock = RLock()
        self._view: _View | None = None
        self._keys: dict[tuple[str, str, str, str], bytes] = {}
        self._conditions = LoggedConditions()
        # The inter-process write lock, re-entered by a write inside ``reset``.
        self._write_lock_depth = 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def status(self) -> SecretStoreStatus:
        """The store's state, counts and damaged entries."""
        with self._lock:
            view = self._current()
            record = None if view.document is None else view.document.get(PLAINTEXT_IMPORT_FIELD)
            protection = view.key_record.get("protection")
            store_id = view.store_id
            location = None
            locations: tuple[str, ...] = ()
            if isinstance(protection, str) and isinstance(store_id, str):
                found = self._protection(protection)
                if found is not None:
                    location = found.location(store_id)
                    locations = found.locations(store_id)
            return SecretStoreStatus(
                state=view.state,
                reason=view.reason,
                store_id=store_id if isinstance(store_id, str) else None,
                protection=protection if isinstance(protection, str) else None,
                key_location=location,
                credentials=len(_section(view.document, CREDENTIALS)),
                oauth_tokens=len(_section(view.document, OAUTH_TOKENS)),
                damaged=tuple(
                    f"{section}/{name}"
                    for section in SECTIONS
                    for name in view.damaged.get(section, ())
                ),
                import_pending=bool(isinstance(record, dict) and record.get("sources")),
                key_locations=locations,
                new_key_location=self._protections[0].new_key_location(),
            )

    def entries(self, section: Section) -> dict[str, str]:
        """Every entry of ``section`` that decrypts; empty while unavailable."""
        with self._lock:
            return dict(self._current().values.get(section, {}))

    def entry(self, section: Section, name: str) -> str | None:
        """One entry, or ``None`` when it is not stored.

        Raises :class:`SecretStoreUnavailableError` while the store is
        unavailable and :class:`SecretEntryUnavailableError` for a damaged entry.
        """
        with self._lock:
            view = self._current()
            if view.state == "unavailable":
                raise SecretStoreUnavailableError(_unavailable_message(view))
            if name not in _section(view.document, section):
                return None
            value = view.values.get(section, {}).get(name)
            if value is None:
                raise SecretEntryUnavailableError(
                    f"the stored {section} entry {name} does not decrypt"
                )
            return value

    def entry_names(self, section: Section) -> tuple[str, ...]:
        """Every stored name of ``section``, damaged entries included.

        Names need no key: they are listed while the key is unavailable, as
        long as the document itself loads.
        """
        with self._lock:
            return tuple(sorted(_section(self._current().document, section)))

    def import_record(self) -> dict[str, Any] | None:
        """The pending plaintext import record, if the document holds one."""
        with self._lock:
            document = self._current().document
            record = None if document is None else document.get(PLAINTEXT_IMPORT_FIELD)
            return copy.deepcopy(record) if isinstance(record, dict) else None

    def fingerprint(self, content: bytes) -> str:
        """A keyed digest of ``content`` under this store's key; reveals nothing without it.

        Raises :class:`SecretStoreUnavailableError` unless the store is available.
        """
        with self._lock:
            view = self._current()
            if view.state != "available" or view.key is None:
                raise SecretStoreUnavailableError(_unavailable_message(view))
            return fingerprint(view.key, content)

    def reload(self) -> SecretStoreStatus:
        """Forget the cached version and read the document again."""
        with self._lock:
            self._view = None
            return self.status()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set_entry(self, section: Section, name: str, value: str) -> bool:
        """Store one entry; whether the stored value changed."""
        return self.update(lambda transaction: transaction.set(section, name, value))

    def remove_entry(self, section: Section, name: str) -> bool:
        """Remove one entry; whether it existed. An absent store stays absent."""
        return self.update(lambda transaction: transaction.remove(section, name))

    def update[Result](self, change: Callable[[SecretTransaction], Result]) -> Result:
        """Apply ``change`` to a fresh copy of the store and write it once.

        Creates the store when it is absent. Holds the inter-process write lock
        from the read to the write, so a concurrent writer in another process
        waits and then sees this change. Raises
        :class:`SecretStoreUnavailableError` while it is unavailable and
        :class:`SecretStoreError` when the document cannot be written; the
        document on disk is then unchanged.
        """
        # The snapshot freeze first: a waiting change keeps the locks it holds.
        # Checked before a key is created: no key file is left behind for a store
        # that cannot be written.
        with document_change(self.path), self._lock, self._write_lock():
            view = self._load()
            if view.state == "unavailable":
                raise SecretStoreUnavailableError(_unavailable_message(view))
            if view.state == "absent":
                transaction = SecretTransaction(
                    {CREDENTIALS: {}, OAUTH_TOKENS: {}}, None, {}, create=self._create_key
                )
            else:
                assert view.document is not None and view.key is not None
                transaction = SecretTransaction(
                    copy.deepcopy(view.document),
                    view.key,
                    {section: dict(view.values.get(section, {})) for section in SECTIONS},
                )
            result = change(transaction)
            if transaction.changed:
                document = transaction.document
                if transaction.created:
                    self._require_absent()
                self._write(document)
                if transaction.created:
                    _LOGGER.info(
                        "Secret Store created (store=%s protection=%s)",
                        document["store_id"],
                        document["key"]["protection"],
                    )
            return result

    def reset(self) -> SecretStoreReset:
        """Move the current document to quarantine and start a new empty store.

        The explicit operator command for a store whose key is lost: nothing is
        deleted, and the old store opens again once moved back with its key.
        Refuses, under the write lock, a store that this process opens and one
        that cannot be read right now (an I/O error is no lost key).
        """
        with document_change(self.path), self._lock, self._write_lock():
            quarantine: Path | None = None
            current = self._load()
            if current.state == "available":
                raise SecretStoreError(
                    "the Secret Store opens; reset only replaces a store whose key is lost or wrong"
                )
            if current.transient:
                raise SecretStoreError(
                    f"the Secret Store cannot be checked right now ({current.reason}); try "
                    "again, a reset only replaces a store whose key is lost or wrong"
                )
            try:
                present = stat_or_none(self.path) is not None
            except OSError as exc:
                raise SecretStoreError(f"{self.path} cannot be checked: {exc}") from exc
            if present:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                quarantine = (
                    self.data_dir
                    / "quarantine"
                    / QUARANTINE_NAME
                    / f"{stamp}-{uuid.uuid4().hex[:8]}"
                )
                try:
                    quarantine.mkdir(parents=True)
                    os.replace(self.path, quarantine / SECRET_STORE_FILE_NAME)
                except OSError as exc:
                    raise SecretStoreError(
                        f"the Secret Store could not be moved to quarantine: {exc}"
                    ) from exc
                _LOGGER.warning(
                    "Secret Store moved to quarantine by an explicit reset (quarantine=%s)",
                    quarantine,
                )
            self._view = None
            self.update(lambda transaction: transaction.ensure_created())
            status = self.status()
            _LOGGER.info(
                "Secret Store created by an explicit reset (store=%s protection=%s)",
                status.store_id,
                status.protection,
            )
            return SecretStoreReset(quarantine=quarantine, status=status)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _current(self) -> _View:
        view = self._view
        if view is not None and view.stamp is not None:
            try:
                stamp = _stamp(stat_or_none(self.path))
            except OSError:
                stamp = None
            if stamp == view.stamp and time.time_ns() - stamp[2] >= _RACY_WINDOW_NS:
                return view
        return self._load()

    def _load(self) -> _View:
        view = self._read()
        self._view = view
        self._report(view)
        return view

    def _read(self) -> _View:
        """Read the document; an I/O failure is retried briefly and never cached."""
        view = self._read_once()
        for delay in _READ_RETRY_DELAYS:
            if not view.transient:
                break
            _sleep(delay)
            view = self._read_once()
        return view

    def _read_once(self) -> _View:
        try:
            with self.path.open("rb") as handle:
                stamp = _stamp(os.fstat(handle.fileno()))
                raw = handle.read()
        except FileNotFoundError, NotADirectoryError:
            return _View("absent")
        except OSError as exc:
            return _View(
                "unavailable",
                reason=f"secrets.json cannot be read right now: {exc}",
                transient=True,
            )
        data, problems = _decode_document(raw)
        if problems:
            return _View(
                "unavailable",
                stamp=stamp,
                reason=f"secrets.json failed to load: {problems}",
                document=data if isinstance(data, dict) else None,
            )
        document: dict[str, Any] = data
        key_record = document["key"]
        protection = self._protection(key_record["protection"])
        if protection is None:
            return _View(
                "unavailable",
                stamp=stamp,
                reason=(
                    f"the key protection {key_record['protection']!r} is not available on "
                    "this system"
                ),
                document=document,
            )
        try:
            key = self._open_key(protection, document["store_id"], key_record)
        except KeyUnavailableError as exc:
            if exc.transient:
                return _View("unavailable", reason=str(exc), document=document, transient=True)
            return _View("unavailable", stamp=stamp, reason=str(exc), document=document)
        if not key_matches(key, document["store_id"], key_record["id"], key_record["check"]):
            self._keys.clear()
            return _View(
                "unavailable",
                stamp=stamp,
                reason="the key does not open this store (a different key was found)",
                document=document,
            )
        values: dict[str, dict[str, str]] = {}
        damaged: dict[str, tuple[str, ...]] = {}
        for section in SECTIONS:
            opened: dict[str, str] = {}
            broken: list[str] = []
            for name, sealed in _section(document, section).items():
                aad = associated_data(document["store_id"], key_record["id"], section, name)
                try:
                    opened[name] = open_sealed(key, sealed, aad).decode("utf-8")
                except SealedEntryError, UnicodeDecodeError:
                    broken.append(name)
            values[section] = opened
            damaged[section] = tuple(sorted(broken))
        return _View(
            "available",
            stamp=stamp,
            document=document,
            key=key,
            values=values,
            damaged=damaged,
        )

    def _open_key(
        self, protection: KeyProtection, store_id: str, record: Mapping[str, Any]
    ) -> bytes:
        cache_key = (protection.name, store_id, str(record["id"]), str(record.get("wrapped", "")))
        key = self._keys.get(cache_key)
        if key is None:
            key = protection.load(store_id, str(record["id"]), record)
            self._keys[cache_key] = key
        return key

    def _protection(self, name: object) -> KeyProtection | None:
        return next((item for item in self._protections if item.name == name), None)

    def _create_key(self, document: dict[str, Any]) -> bytes:
        """Give an absent store its identity and key; the caller writes the document."""
        store_id = new_identifier()
        protection = self._protections[0]
        try:
            created = protection.create(store_id)
        except KeyUnavailableError as exc:
            raise SecretStoreError(f"the Secret Store key could not be created: {exc}") from exc
        document["store_id"] = store_id
        document["key"] = {
            "id": created.key_id,
            "protection": protection.name,
            "check": key_check(created.key, store_id, created.key_id),
            **dict(created.record),
        }
        cache_key = (protection.name, store_id, created.key_id, created.record.get("wrapped", ""))
        self._keys[cache_key] = created.key
        return created.key

    def _require_data_dir(self) -> None:
        # Never create the data directory here: a root without its marker would
        # be refused by the next start.
        if not self.data_dir.is_dir():
            raise SecretStoreError(f"the data directory {self.data_dir} does not exist")

    def _require_absent(self) -> None:
        """A new store never replaces a document, whatever an earlier read reported."""
        try:
            present = stat_or_none(self.path) is not None
        except OSError as exc:
            raise SecretStoreError(f"{self.path} cannot be checked: {exc}") from exc
        if present:
            raise SecretStoreError(
                "secrets.json appeared while a new Secret Store was being created; nothing "
                "was written"
            )

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Hold ``secrets.lock`` across processes; re-entrant within this store.

        The caller holds ``self._lock``, which guards the depth.
        """
        if self._write_lock_depth:
            self._write_lock_depth += 1
            try:
                yield
            finally:
                self._write_lock_depth -= 1
            return
        self._require_data_dir()
        handle = _acquire_write_lock(self.data_dir / LOCK_FILE_NAME)
        self._write_lock_depth = 1
        try:
            yield
        finally:
            self._write_lock_depth = 0
            _release_write_lock(handle)

    def _write(self, document: Mapping[str, Any]) -> None:
        try:
            self._require_data_dir()
            write_json_document(
                self.path,
                document,
                SECRET_STORE_FORMAT,
                data_dir=self.data_dir,
                mode=0o600,
            )
        except JsonDocumentWriteError as exc:
            raise SecretStoreUnavailableError(str(exc)) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise SecretStoreError(f"secrets.json could not be written: {exc}") from exc
        finally:
            # The next read loads the version on disk, written or not.
            self._view = None

    def _report(self, view: _View) -> None:
        if view.state == "unavailable":
            if view.transient:
                if self._conditions.started("state", view.reason):
                    _LOGGER.warning(
                        "Secret Store cannot be read right now; every access tries again "
                        "(store=%s reason=%s)",
                        self.path,
                        view.reason,
                    )
                return
            if self._conditions.started("state", view.reason):
                _LOGGER.warning(
                    "Secret Store unavailable; the credentials it holds cannot be used until "
                    "its key is available again and the server restarts, or `vbot data-store "
                    "secrets reset --yes` starts a new store (store=%s reason=%s)",
                    self.path,
                    view.reason,
                )
            return
        if self._conditions.ended("state"):
            _LOGGER.info("Secret Store available again (store=%s)", self.path)
        damaged = tuple(
            f"{section}/{name}" for section in SECTIONS for name in view.damaged.get(section, ())
        )
        if damaged:
            if self._conditions.started("damaged", damaged):
                _LOGGER.warning(
                    "Secret Store entries do not decrypt and are treated as missing until "
                    "replaced (store=%s entries=%s)",
                    self.path,
                    ",".join(damaged),
                )
        elif self._conditions.ended("damaged"):
            _LOGGER.info("Secret Store entries decrypt again (store=%s)", self.path)


def validate_secret_store_file(path: str | Path) -> JsonValidationReport:
    """Validate ``secrets.json`` and check that its key opens it, for ``doctor``."""
    store_path = Path(path)
    report, _data = read_json_file(store_path, validate_secret_store_data, missing_ok=False)
    if not report.ok or not report.exists:
        return report
    status = SecretStore(store_path.parent).status()
    diagnostics = list(report.diagnostics)
    if status.state == "unavailable":
        add_error(
            diagnostics,
            "$.key",
            f"the store cannot be opened: {status.reason}. Restore its key, or start a new "
            "store with `vbot data-store secrets reset --yes`",
        )
    for damaged in status.damaged:
        section, _, name = damaged.partition("/")
        add_error(diagnostics, f"$.{section}.{name}", "does not decrypt; set it again")
    return JsonValidationReport(store_path, True, tuple(diagnostics))


def _acquire_write_lock(path: Path) -> IO[bytes]:
    """Take the OS-owned lock on byte 0 of ``path``, waiting for another writer.

    The operating system releases it when a process ends, so a crashed writer
    never leaves the store locked.
    """
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise SecretStoreError(
            f"the Secret Store write lock {path} cannot be opened: {exc}"
        ) from exc
    deadline = time.monotonic() + _WRITE_LOCK_TIMEOUT_SECONDS
    try:
        # Windows can lock bytes beyond EOF. Writing a sentinel first races
        # another writer that has already locked byte 0 of the empty file.
        while True:
            try:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise SecretStoreError(
                        "another process has been writing the Secret Store for "
                        f"{_WRITE_LOCK_TIMEOUT_SECONDS:.0f} s; nothing was changed"
                    ) from exc
                _sleep(_WRITE_LOCK_POLL_SECONDS)
    except BaseException:
        handle.close()
        raise


def _release_write_lock(handle: IO[bytes]) -> None:
    with suppress(OSError):
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _section(document: Mapping[str, Any] | None, section: str) -> dict[str, Any]:
    if document is None:
        return {}
    entries = document.get(section)
    return entries if isinstance(entries, dict) else {}


def _stamp(status: os.stat_result | None) -> tuple[int, int, int] | None:
    if status is None:
        return None
    return (status.st_ino, status.st_size, status.st_mtime_ns)


def _check_name(name: str) -> None:
    if not isinstance(name, str) or ENTRY_NAME_PATTERN.fullmatch(name) is None:
        raise SecretStoreError(f"invalid Secret Store entry name: {name!r}")


def _unavailable_message(view: _View) -> str:
    if view.state == "absent":
        return "the Secret Store does not exist yet"
    if view.transient:
        return f"the Secret Store cannot be read right now ({view.reason}); try again"
    return (
        f"the Secret Store is unavailable ({view.reason}); restore its key or start a new "
        "store with `vbot data-store secrets reset --yes`"
    )
