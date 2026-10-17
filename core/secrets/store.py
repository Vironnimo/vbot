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

Entry values are never logged.
"""

from __future__ import annotations

import copy
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Final, Literal

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    read_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    document_change,
    json_document,
    json_map,
    json_object,
    validate_format_version,
    warn_unknown_fields,
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
from core.secrets._keys import (
    KeyProtection,
    KeyUnavailableError,
    default_key_protections,
)
from core.utils.errors import StorageError
from core.utils.file_status import stat_or_none
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger

Section = Literal["credentials", "oauth_tokens"]

SECRET_STORE_FILE_NAME = "secrets.json"
SECRET_STORE_FORMAT_VERSION = 1
#: Credentials by environment-variable name (API keys, tokens, Extension secrets).
CREDENTIALS: Final = "credentials"
#: OAuth token documents of Provider Connections by token id.
OAUTH_TOKENS: Final = "oauth_tokens"
SECTIONS: tuple[Section, ...] = (CREDENTIALS, OAUTH_TOKENS)
#: The record of an import of plaintext credential files (``core.secrets.plaintext``).
PLAINTEXT_IMPORT_FIELD = "plaintext_import"
#: The quarantine child of a store that ``reset`` replaced.
QUARANTINE_NAME = "secret-store"

ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")
_STORE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A document modified this recently may change again within the same filesystem
# timestamp tick, so its stat stamp cannot prove it unchanged yet.
_RACY_WINDOW_NS = 3_000_000_000

_LOGGER = get_logger("secrets")

SecretStoreState = Literal["absent", "available", "unavailable"]

_KEY_SHAPE = json_object({"id", "protection", "check", "wrapped"})
_IMPORT_SHAPE = json_object(
    {"imported_at", "sources", "credentials", "oauth_tokens", "backups_pending"}
)
SECRET_STORE_SHAPE = json_document(
    {"store_id", "key", CREDENTIALS, OAUTH_TOKENS, PLAINTEXT_IMPORT_FIELD},
    {
        "key": _KEY_SHAPE,
        CREDENTIALS: json_map(),
        OAUTH_TOKENS: json_map(),
        PLAINTEXT_IMPORT_FIELD: _IMPORT_SHAPE,
    },
)


class SecretStoreError(StorageError):
    """A Secret Store operation failed; nothing was changed."""


class SecretStoreUnavailableError(SecretStoreError):
    """The store cannot be read or opened; it is never written in this state."""


class SecretEntryUnavailableError(SecretStoreError):
    """One stored entry does not decrypt (damaged or altered)."""


def validate_secret_store_data(data: Any) -> list[JsonDiagnostic]:
    """Validate the structure of a decoded store document (no decryption)."""
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, SECRET_STORE_FORMAT_VERSION):
        return diagnostics
    warn_unknown_fields(diagnostics, "$", data, SECRET_STORE_SHAPE, label="Secret Store field")
    store_id = data.get("store_id")
    if not isinstance(store_id, str) or _STORE_ID_PATTERN.fullmatch(store_id) is None:
        add_error(diagnostics, "$.store_id", "must be a store identifier")
    key = data.get("key")
    if not isinstance(key, dict):
        add_error(diagnostics, "$.key", "must be an object")
    else:
        for name in ("id", "protection", "check"):
            if not isinstance(key.get(name), str) or not key[name]:
                add_error(diagnostics, f"$.key.{name}", "must be a non-empty string")
        if "wrapped" in key and not isinstance(key["wrapped"], str):
            add_error(diagnostics, "$.key.wrapped", "must be a string")
    for section in SECTIONS:
        entries = data.get(section, {})
        if not isinstance(entries, dict):
            add_error(diagnostics, f"$.{section}", "must be an object")
            continue
        for name, value in entries.items():
            if ENTRY_NAME_PATTERN.fullmatch(name) is None:
                add_error(diagnostics, f"$.{section}", f"has an invalid entry name: {name!r}")
            if not isinstance(value, str):
                add_error(diagnostics, f"$.{section}.{name}", "must be a sealed string")
    record = data.get(PLAINTEXT_IMPORT_FIELD)
    if record is not None:
        _validate_import_record(diagnostics, record)
    return diagnostics


def _validate_import_record(diagnostics: list[JsonDiagnostic], record: Any) -> None:
    path = f"$.{PLAINTEXT_IMPORT_FIELD}"
    if not isinstance(record, dict):
        add_error(diagnostics, path, "must be an object")
        return
    sources = record.get("sources", {})
    if not isinstance(sources, dict) or any(
        not isinstance(value, str) for value in sources.values()
    ):
        add_error(diagnostics, f"{path}.sources", "must map file paths to fingerprints")
    for name in (CREDENTIALS, OAUTH_TOKENS):
        value = record.get(name, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            add_error(diagnostics, f"{path}.{name}", "must be a list of names")
    if not isinstance(record.get("backups_pending", False), bool):
        add_error(diagnostics, f"{path}.backups_pending", "must be a boolean")


SECRET_STORE_FORMAT = JsonDocumentFormat(
    name="Secret Store",
    version=SECRET_STORE_FORMAT_VERSION,
    shape=SECRET_STORE_SHAPE,
    validate=validate_secret_store_data,
    sort_keys=True,
)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "store_id": self.store_id,
            "protection": self.protection,
            "key_location": self.key_location,
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
    the document first. ``protections`` are the key protections this process
    can open; the first one protects a new store.
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
            if isinstance(protection, str) and isinstance(store_id, str):
                found = self._protection(protection)
                location = None if found is None else found.location(store_id)
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

        Creates the store when it is absent. Raises
        :class:`SecretStoreUnavailableError` while it is unavailable and
        :class:`SecretStoreError` when the document cannot be written; the
        document on disk is then unchanged.
        """
        # The snapshot freeze first: a waiting change keeps the locks it holds.
        with document_change(self.path), self._lock:
            view = self._load()
            if view.state == "unavailable":
                raise SecretStoreUnavailableError(_unavailable_message(view))
            if view.state == "absent":
                # Checked before a key is created: no key file is left behind for
                # a store that cannot be written.
                self._require_data_dir()
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
        """
        with document_change(self.path), self._lock:
            quarantine: Path | None = None
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
        try:
            status = stat_or_none(self.path)
        except OSError as exc:
            return _View("unavailable", reason=f"secrets.json cannot be checked: {exc}")
        if status is None:
            return _View("absent")
        stamp = _stamp(status)
        report, data = read_json_file(self.path, validate_secret_store_data, missing_ok=True)
        if not report.exists:
            return _View("absent")
        if not report.ok:
            problems = "; ".join(
                f"{diagnostic.path}: {diagnostic.message}"
                for diagnostic in report.diagnostics
                if diagnostic.severity == "error"
            )
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
    return (
        f"the Secret Store is unavailable ({view.reason}); restore its key or start a new "
        "store with `vbot data-store secrets reset --yes`"
    )
