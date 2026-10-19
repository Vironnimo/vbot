"""Conversion of the plaintext credential files into the Secret Store.

Before the Secret Store, credentials lay in plaintext: ``<data-dir>/.env`` and
one ``<data-dir>/oauth/<token id>.json`` per OAuth login. The conversion has two
steps, so a failed or rolled-back update never leaves an installation without
its credentials:

1. :func:`import_plaintext_credentials` copies the credentials into the store,
   reads the written store back to verify it, and records each source file
   with a keyed fingerprint of its content and of every value it put into the
   store. It never changes a plaintext file and can repeat any number of
   times. While an import is pending (an update's verification start runs it
   again after a rollback), the store mirrors the recorded files: a value a
   file changed replaces the one the import put there, and an entry a
   recorded file no longer holds is removed. Both happen only while the store
   still holds what the import put there: a value set since (WebUI, CLI) is
   never overwritten or removed, and a store value that already differed
   before the import is kept as well; the import reports both as conflicts.
   ``vbot data-store secrets convert`` imports without removing anything.
2. :func:`retire_plaintext_credentials` runs once the import is committed (the
   first normal start, or ``convert``): it deletes each recorded file whose
   content is unchanged and whose entries the store holds, then purges the
   plaintext copies from configuration backups, data snapshots and the JSON
   document quarantine. Before deletion, the record moves the files out of
   the mirrored sources into a separate retirement journal: an interrupted
   retire retries without allowing an import to remove the stored credentials.
   A file that cannot be deleted safely stays and is reported.

A plaintext file that is a link, or lies in an ``oauth`` directory that is a
link, is imported from the file it reaches, but vBot never deletes it or the
file it points to: it is reported until the operator deletes it.

vBot takes credentials from plaintext files only in an import: a plaintext
credential file outside a pending import is ignored with a warning, and
``doctor`` and the data-store status say what keeps it and what to do. Reports
name files, line numbers and credential names, never a value or the text of a
line that cannot be imported, which may be a pasted secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.secrets._plaintext_files import (
    ENV_FILE_PATH as ENV_FILE_PATH,
)
from core.secrets._plaintext_files import (
    OAUTH_DIRECTORY as OAUTH_DIRECTORY,
)
from core.secrets._plaintext_files import (
    OAUTH_TOKEN_PATTERN as OAUTH_TOKEN_PATTERN,
)
from core.secrets._plaintext_files import (
    PLAINTEXT_CREDENTIAL_PATTERNS as PLAINTEXT_CREDENTIAL_PATTERNS,
)
from core.secrets._plaintext_files import (
    _find_plaintext,
    _inspect,
    _label,
    _pending_sources,
    _recorded_sources,
    _section_of,
)
from core.secrets._plaintext_files import (
    plaintext_credential_files as plaintext_credential_files,
)
from core.secrets._plaintext_files import (
    quarantined_plaintext_files as quarantined_plaintext_files,
)
from core.secrets._plaintext_files import (
    quarantined_secret_stores as quarantined_secret_stores,
)
from core.secrets._plaintext_reports import (
    CONVERT_COMMAND as CONVERT_COMMAND,
)
from core.secrets._plaintext_reports import (
    _held_problem,
)
from core.secrets._plaintext_reports import (
    credential_file_reports as credential_file_reports,
)
from core.secrets._plaintext_reports import (
    ignored_plaintext_files as ignored_plaintext_files,
)
from core.secrets._plaintext_reports import (
    secret_store_health as secret_store_health,
)
from core.secrets.store import (
    CREDENTIALS,
    OAUTH_TOKENS,
    SecretEntryUnavailableError,
    SecretStore,
    SecretStoreError,
    SecretTransaction,
    Section,
)
from core.utils.file_status import is_link_status, stat_or_none
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

_LOGGER = get_logger("secrets")


@dataclass(frozen=True)
class PlaintextImport:
    """What one import did; names and line numbers only, never values."""

    #: The files recorded for the pending import.
    sources: tuple[str, ...]
    #: The entries the store now holds as the files do.
    credentials: tuple[str, ...]
    oauth_tokens: tuple[str, ...]
    #: Entries removed because a recorded file no longer holds them.
    removed: tuple[str, ...] = ()
    #: Files or ``<file>:<line>`` entries left out, with the reason.
    skipped: Mapping[str, str] = field(default_factory=dict)
    #: Entries the store keeps although a file holds another value: set or
    #: removed in the store since this import, or different before it.
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaintextRetirement:
    """What retiring an import deleted and kept."""

    deleted: tuple[str, ...] = ()
    kept: Mapping[str, str] = field(default_factory=dict)
    backup_entries: int = 0
    snapshot_copies: int = 0
    #: Plaintext copies deleted from the JSON document quarantine.
    quarantine_copies: int = 0
    backups_complete: bool = True


@dataclass
class _ImportOutcome:
    imported: dict[Section, dict[str, str]] = field(
        default_factory=lambda: {CREDENTIALS: {}, OAUTH_TOKENS: {}}, repr=False
    )
    sources: tuple[str, ...] = ()
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Purge:
    backup_entries: int = 0
    snapshot_copies: int = 0
    quarantine_copies: int = 0
    complete: bool = True


def import_plaintext_credentials(
    store: SecretStore, *, remove_missing: bool = True
) -> PlaintextImport | None:
    """Copy the plaintext credential files into ``store`` and verify the result.

    Returns ``None`` when there are neither plaintext files nor recorded sources
    to reconcile. An empty discovery still removes unchanged imported values of
    recorded sources when ``remove_missing`` is true.
    Never changes a plaintext file. A store value that differs from a file's
    is replaced only when this pending import put it there; otherwise the
    store keeps it and the entry is a conflict. With ``remove_missing`` (an
    update's verification start), an entry a recorded file no longer holds is
    removed while the store still holds the value this import put there;
    ``convert`` passes ``False`` and never removes an entry. Raises
    :class:`SecretStoreError` when the store cannot take the import (an
    unavailable store is never replaced) or does not read back what was
    written, and ``OSError`` when a file cannot be read.
    """
    data_dir = store.data_dir
    found = _find_plaintext(data_dir)
    if not found and (not remove_missing or not _recorded_sources(store.import_record() or {})):
        return None
    sources = [
        source
        for path, link in found.items()
        if (source := _inspect(data_dir, path, link)) is not None
    ]

    def change(transaction: SecretTransaction) -> _ImportOutcome:
        record = transaction.import_record or {}
        recorded = _recorded_sources(record)
        retiring = _recorded_sources(record, "retiring_sources")
        imported_before = _recorded_values(recorded)
        held: dict[Section, set[str]] = {CREDENTIALS: set(), OAUTH_TOKENS: set()}
        for source in sources:
            held[source.section].update(source.entries)
        # A file that no longer parses at all still holds its entries as far as
        # anyone can tell: they are never removed for it.
        unparsed = {source.path for source in sources if source.path in source.problems}
        outcome = _ImportOutcome()
        if remove_missing:
            for path, recorded_source in sorted(recorded.items()):
                if path in unparsed:
                    continue
                section = _section_of(path)
                for name, value_fingerprint in sorted(recorded_source["entries"].items()):
                    if name not in held[section] and _store_holds(
                        transaction, section, name, value_fingerprint
                    ):
                        transaction.remove(section, name)
                        outcome.removed.append(_label(section, name))
        recorded_now: dict[str, dict[str, Any]] = {}
        for source in sources:
            # A committed source waits only for deletion. Re-importing it would
            # let a later missing-source reconciliation remove its credentials.
            if source.path in retiring:
                continue
            section = source.section
            entries: dict[str, str] = {}
            for name, value in sorted(source.entries.items()):
                if _take(transaction, section, name, value, imported_before[section].get(name)):
                    entries[name] = transaction.fingerprint(value.encode("utf-8"))
                    outcome.imported[section][name] = value
                else:
                    outcome.conflicts.append(_label(section, name))
            # A file without anything importable is no source: nothing to mirror or delete.
            if source.content is not None and (source.entries or not source.problems):
                recorded_now[source.path] = {
                    "fingerprint": transaction.fingerprint(source.content),
                    "entries": entries,
                }
        if not found and recorded:
            # Normal startup must still retire plaintext copies in backups after
            # the last source disappeared during a rolled-back update.
            record = {**record, "backups_pending": True}
        _set_import_record(transaction, record, recorded_now)
        outcome.sources = tuple(sorted(recorded_now))
        return outcome

    outcome = store.update(change)
    _verify(store, outcome.imported)
    skipped = {item: reason for source in sources for item, reason in source.problems.items()}
    result = PlaintextImport(
        sources=outcome.sources,
        credentials=tuple(sorted(outcome.imported[CREDENTIALS])),
        oauth_tokens=tuple(sorted(outcome.imported[OAUTH_TOKENS])),
        removed=tuple(outcome.removed),
        skipped=skipped,
        conflicts=tuple(sorted(outcome.conflicts)),
    )
    _LOGGER.info(
        "Imported plaintext credentials into the Secret Store; the files stay until the "
        "import is committed (credentials=%d oauth_tokens=%d removed=%d files=%s)",
        len(result.credentials),
        len(result.oauth_tokens),
        len(result.removed),
        ",".join(result.sources) or "none",
    )
    if result.conflicts:
        _LOGGER.warning(
            "Plaintext credentials that differ from the Secret Store were not imported; the "
            "Secret Store keeps its values (entries=%s)",
            ",".join(result.conflicts),
        )
    if skipped:
        _LOGGER.warning(
            "Plaintext credentials left out of the import stay in their files (entries=%s)",
            ",".join(f"{item} ({reason})" for item, reason in sorted(skipped.items())),
        )
    if "LOG_LEVEL" in result.credentials:
        _LOGGER.warning(
            "LOG_LEVEL in .env no longer selects the log level; set it in the server's "
            "process environment instead"
        )
    return result


def retire_plaintext_credentials(store: SecretStore) -> PlaintextRetirement | None:
    """Delete the imported plaintext files and purge their copies from backups.

    Runs only for a recorded import, and only deletes a file whose content is
    unchanged since the import and whose every entry the store holds; never a
    link or the file it points to. Each file leaves the mirrored sources before
    deletion, but stays journaled until deleted so the next start retries.
    Returns ``None`` when no import is recorded. Raises
    :class:`SecretStoreError` when the store is not available.
    """
    record = store.import_record()
    if record is None:
        return None
    data_dir = store.data_dir
    found = _find_plaintext(data_dir)
    deleted: list[str] = []
    kept: dict[str, str] = {}
    # Sources that stay recorded: a file that cannot be read now is retried.
    stay: dict[str, dict[str, Any]] = {}
    retiring = _recorded_sources(record, "retiring_sources")
    retry_retiring: dict[str, dict[str, Any]] = {}
    deletable: dict[str, dict[str, Any]] = {}
    for path, recorded in sorted(_pending_sources(record).items()):
        if path not in found:
            continue
        try:
            source = _inspect(data_dir, path, found[path])
        except OSError as exc:
            kept[path] = f"cannot be read: {exc}"
            (retry_retiring if path in retiring else stay)[path] = recorded
            continue
        if source is None:
            continue
        if source.link is not None:
            kept[path] = f"{source.link}; vBot never deletes it or the file it points to"
            continue
        if source.content is None or store.fingerprint(source.content) != recorded["fingerprint"]:
            kept[path] = "changed after the import"
            continue
        problem = _held_problem(store, source)
        if problem is not None:
            kept[path] = problem
            continue
        deletable[path] = recorded
    # Persist the deletion journal before unlink: every crash point is retryable,
    # without giving an import authority to remove credentials of deleted files.
    store.update(
        lambda transaction: transaction.set_import_record(
            _narrowed_record(record, stay, {**retry_retiring, **deletable}, backups_pending=True)
        )
    )
    for path, recorded in deletable.items():
        try:
            data_dir.joinpath(*path.split("/")).unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            kept[path] = f"cannot be deleted: {exc}"
            # Unchanged and held by the store: the next start tries again.
            retry_retiring[path] = recorded
            continue
        deleted.append(path)
    _remove_empty_oauth_directory(data_dir)
    purge = _purge_copies(store)
    store.update(
        lambda transaction: transaction.set_import_record(
            _narrowed_record(record, stay, retry_retiring, backups_pending=not purge.complete)
        )
    )
    result = PlaintextRetirement(
        deleted=tuple(deleted),
        kept=kept,
        backup_entries=purge.backup_entries,
        snapshot_copies=purge.snapshot_copies,
        quarantine_copies=purge.quarantine_copies,
        backups_complete=purge.complete,
    )
    if deleted or purge.backup_entries or purge.snapshot_copies or purge.quarantine_copies:
        _LOGGER.info(
            "Plaintext credential files removed after their import into the Secret Store "
            "(files=%s backup_entries=%d snapshot_copies=%d quarantine_copies=%d)",
            ",".join(deleted) or "none",
            purge.backup_entries,
            purge.snapshot_copies,
            purge.quarantine_copies,
        )
    if kept:
        _LOGGER.warning(
            "Plaintext credential files kept: one that cannot be read or deleted now is tried "
            "again at the next start, and `vbot data-store secrets status` says what to do "
            "with the others (files=%s)",
            ",".join(f"{path} ({reason})" for path, reason in sorted(kept.items())),
        )
    return result


def settle_plaintext_credentials(
    store: SecretStore, *, mode: Literal["verification", "normal", "test"]
) -> None:
    """The startup step of the conversion for one Runtime start.

    ``verification`` (an update's start of the new version) imports into an
    absent store or one whose import is still pending, and raises when that
    fails, which fails the verification. ``normal`` retires a recorded import.
    Every mode warns about plaintext credential files that are ignored.
    """
    # Only an unconverted data directory imports: once a store holds credentials,
    # a plaintext file that appears later never overrides them.
    if mode == "verification" and (
        store.status().state == "absent" or (store.import_record() or {}).get("sources")
    ):
        import_plaintext_credentials(store)
        return
    if mode == "normal":
        try:
            retire_plaintext_credentials(store)
        except (SecretStoreError, OSError) as exc:
            _LOGGER.warning(
                "The plaintext credential files of the last import were not removed; "
                "the next start tries again: %s",
                exc,
            )
    try:
        ignored = ignored_plaintext_files(store)
    except OSError as exc:
        _LOGGER.warning(
            "The data directory could not be checked for plaintext credentials: %s", exc
        )
        return
    if ignored:
        _LOGGER.warning(
            "Plaintext credential files are ignored: vBot reads credentials only from the "
            "Secret Store. `vbot data-store secrets status` says what to do with each "
            "(files=%s)",
            ",".join(ignored),
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _recorded_values(recorded: Mapping[str, Mapping[str, Any]]) -> dict[Section, dict[str, str]]:
    """The value fingerprint the pending import put into the store, by section and name."""
    values: dict[Section, dict[str, str]] = {CREDENTIALS: {}, OAUTH_TOKENS: {}}
    for path, source in recorded.items():
        values[_section_of(path)].update(source["entries"])
    return values


def _set_import_record(
    transaction: SecretTransaction,
    record: Mapping[str, Any],
    sources: dict[str, dict[str, Any]],
) -> None:
    current: dict[str, Any] = {"sources": sources}
    retiring = _recorded_sources(record, "retiring_sources")
    if retiring:
        current["retiring_sources"] = retiring
    if record.get("backups_pending"):
        current["backups_pending"] = True
    elif not sources and not retiring:
        # Nothing to mirror, delete or purge: no record (an absent store stays absent).
        transaction.set_import_record(None)
        return
    previous = {key: value for key, value in record.items() if key != "imported_at"}
    if previous != current or "imported_at" not in record:
        current["imported_at"] = utc_now_timestamp()
        transaction.set_import_record(current)


def _narrowed_record(
    record: Mapping[str, Any],
    sources: dict[str, dict[str, Any]],
    retiring: dict[str, dict[str, Any]],
    *,
    backups_pending: bool,
) -> dict[str, Any] | None:
    if not sources and not retiring and not backups_pending:
        return None
    narrowed: dict[str, Any] = {"sources": dict(sources)}
    if retiring:
        narrowed["retiring_sources"] = dict(retiring)
    if backups_pending:
        narrowed["backups_pending"] = True
    if "imported_at" in record:
        narrowed["imported_at"] = record["imported_at"]
    return narrowed


def _store_holds(
    transaction: SecretTransaction, section: Section, name: str, value_fingerprint: str
) -> bool:
    """Whether the store still holds exactly the value the pending import put there."""
    try:
        current = transaction.get(section, name)
    except SecretEntryUnavailableError:
        return False
    return (
        current is not None
        and transaction.fingerprint(current.encode("utf-8")) == value_fingerprint
    )


def _take(
    transaction: SecretTransaction,
    section: Section,
    name: str,
    value: str,
    imported_fingerprint: str | None,
) -> bool:
    """Make the store hold ``value``; ``False`` when that would replace another's value.

    A store value is replaced only when the pending import put it there
    (``imported_fingerprint`` matches); a missing entry is added unless the
    import added it before and it was removed since. A damaged entry is
    replaced only explicitly.
    """
    try:
        current = transaction.get(section, name)
    except SecretEntryUnavailableError:
        return False
    if current == value:
        return True
    if imported_fingerprint is None:
        if current is not None:
            return False
    elif (
        current is None or transaction.fingerprint(current.encode("utf-8")) != imported_fingerprint
    ):
        return False
    transaction.set(section, name, value)
    return True


def _verify(store: SecretStore, imported: Mapping[Section, Mapping[str, str]]) -> None:
    """Read the written store back from disk and compare every imported entry."""
    store.reload()
    for section, expected in imported.items():
        for name, value in expected.items():
            if store.entry(section, name) != value:
                raise SecretStoreError(
                    f"the Secret Store does not read back the imported {section} entry {name}"
                )


def _remove_empty_oauth_directory(data_dir: Path) -> None:
    # A directory that still holds files, a link, or one that is absent stays as it is.
    directory = data_dir / OAUTH_DIRECTORY
    with suppress(OSError):
        status = stat_or_none(directory, follow_symlinks=False)
        if status is not None and not is_link_status(status):
            directory.rmdir()


def _purge_copies(store: SecretStore) -> _Purge:
    from core.database import (
        DatabaseError,
        purge_config_backup_files,
        remove_retired_document_copies,
    )

    data_dir = store.data_dir
    try:
        purge = purge_config_backup_files(data_dir, PLAINTEXT_CREDENTIAL_PATTERNS)
        copies = remove_retired_document_copies(data_dir, (OAUTH_TOKEN_PATTERN,))
        quarantined = _purge_quarantined_copies(store)
    except (DatabaseError, OSError) as exc:
        _LOGGER.warning(
            "Plaintext credential copies in configuration backups, data snapshots or the "
            "document quarantine were not purged; the next start tries again: %s",
            exc,
        )
        return _Purge(complete=False)
    if not purge.complete:
        _LOGGER.warning(
            "Plaintext credential copies stay in configuration backups while a backup "
            "manifest cannot be read; the next start tries again"
        )
    return _Purge(purge.removed_entries, copies, quarantined, purge.complete)


def _purge_quarantined_copies(store: SecretStore) -> int:
    """Delete the quarantined plaintext copies the store holds exactly; others stay reported."""
    from core.database import DOCUMENT_QUARANTINE_NAME, quarantine_batches

    removed = 0
    for batch in quarantine_batches(store.data_dir, DOCUMENT_QUARANTINE_NAME):
        for path, link in _find_plaintext(batch).items():
            if link is not None:
                continue
            source = _inspect(batch, path, None)
            if source is None or _held_problem(store, source) is not None:
                continue
            batch.joinpath(*path.split("/")).unlink(missing_ok=True)
            removed += 1
        _remove_empty_oauth_directory(batch)
    return removed
