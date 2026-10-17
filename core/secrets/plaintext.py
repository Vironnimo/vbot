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
   document quarantine. The record forgets the files before they are deleted,
   so an interrupted retire never leaves a record naming a deleted file. A
   file that cannot be deleted safely stays and is reported.

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

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.config_validation import JsonDiagnostic, JsonValidationReport
from core.secrets.store import (
    CREDENTIALS,
    ENTRY_NAME_PATTERN,
    OAUTH_TOKENS,
    QUARANTINE_NAME,
    SECRET_STORE_FILE_NAME,
    SecretEntryUnavailableError,
    SecretStore,
    SecretStoreError,
    SecretTransaction,
    Section,
    credential_name_key,
)
from core.utils.file_status import is_link_status, stat_or_none
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

ENV_FILE_PATH = ".env"
OAUTH_DIRECTORY = "oauth"
OAUTH_TOKEN_PATTERN = "oauth/*.json"
#: The plaintext credential files of a data directory before the Secret Store.
PLAINTEXT_CREDENTIAL_PATTERNS: tuple[str, ...] = (ENV_FILE_PATH, OAUTH_TOKEN_PATTERN)
CONVERT_COMMAND = "vbot data-store secrets convert --yes"
# The file stem of a token file: ``<provider>-<connection>[--<account>]``.
_TOKEN_STEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_OAUTH_PREFIX = "oauth/"
_OAUTH_SUFFIX = ".json"
_EXTENDED_PATH_PREFIX = "\\\\?\\"

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
class _Source:
    """One plaintext credential file as it reads now."""

    path: str
    #: Why vBot never deletes the file (it is reached through a link), or ``None``.
    link: str | None = None
    content: bytes | None = field(default=None, repr=False)
    entries: dict[str, str] = field(default_factory=dict, repr=False)
    #: ``<file>`` or ``<file>:<line>`` left out, with the reason.
    problems: dict[str, str] = field(default_factory=dict)

    @property
    def section(self) -> Section:
        return _section_of(self.path)


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


def plaintext_credential_files(data_dir: str | Path) -> tuple[str, ...]:
    """Every plaintext credential file of ``data_dir`` as a relative POSIX path.

    A file reached through a link counts too. Raises ``OSError`` when the data
    directory cannot be listed.
    """
    return tuple(_find_plaintext(Path(data_dir)))


def quarantined_plaintext_files(data_dir: str | Path) -> tuple[str, ...]:
    """Plaintext credential copies in the JSON document quarantine, relative to ``data_dir``.

    An older vBot kept OAuth token files in data snapshots, so its set
    restores moved them to quarantine. Raises ``OSError`` when the quarantine
    cannot be listed.
    """
    from core.database import DOCUMENT_QUARANTINE_NAME, quarantine_batches

    root = Path(data_dir)
    found: list[str] = []
    for batch in quarantine_batches(root, DOCUMENT_QUARANTINE_NAME):
        prefix = batch.relative_to(root).as_posix()
        found.extend(f"{prefix}/{path}" for path in _find_plaintext(batch))
    return tuple(found)


def quarantined_secret_stores(data_dir: str | Path) -> tuple[str, ...]:
    """Secret Store documents in quarantine, newest batch first, relative to ``data_dir``.

    ``reset`` moves a store there, and an older vBot's set restore moved the
    live store there when its snapshot held none. Raises ``OSError`` when the
    quarantine cannot be listed.
    """
    from core.database import DOCUMENT_QUARANTINE_NAME, quarantine_batches

    root = Path(data_dir)
    found: list[tuple[str, str]] = []
    for name in (DOCUMENT_QUARANTINE_NAME, QUARANTINE_NAME):
        for batch in quarantine_batches(root, name):
            document = batch / SECRET_STORE_FILE_NAME
            status = stat_or_none(document, follow_symlinks=False)
            if status is not None and stat.S_ISREG(status.st_mode):
                found.append((batch.name, document.relative_to(root).as_posix()))
    return tuple(path for _batch, path in sorted(found, reverse=True))


def import_plaintext_credentials(
    store: SecretStore, *, remove_missing: bool = True
) -> PlaintextImport | None:
    """Copy the plaintext credential files into ``store`` and verify the result.

    Returns ``None`` when the data directory holds no plaintext credential file.
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
    if not found:
        return None
    sources = [
        source
        for path, link in found.items()
        if (source := _inspect(data_dir, path, link)) is not None
    ]

    def change(transaction: SecretTransaction) -> _ImportOutcome:
        record = transaction.import_record or {}
        recorded = _recorded_sources(record)
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
    link or the file it points to. Each file leaves the record before it is
    deleted. Returns ``None`` when no import is recorded. Raises
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
    deletable: dict[str, dict[str, Any]] = {}
    for path, recorded in sorted(_recorded_sources(record).items()):
        if path not in found:
            continue
        try:
            source = _inspect(data_dir, path, found[path])
        except OSError as exc:
            kept[path] = f"cannot be read: {exc}"
            stay[path] = recorded
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
    # The record forgets the files before they are deleted: an interrupted retire
    # never leaves a record naming a deleted file, whose entries a later import
    # would remove as gone. The record stays until the purge below is done.
    store.update(
        lambda transaction: transaction.set_import_record(
            _narrowed_record(record, stay, backups_pending=True)
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
            stay[path] = recorded
            continue
        deleted.append(path)
    _remove_empty_oauth_directory(data_dir)
    purge = _purge_copies(store)
    store.update(
        lambda transaction: transaction.set_import_record(
            _narrowed_record(record, stay, backups_pending=not purge.complete)
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


def ignored_plaintext_files(store: SecretStore) -> tuple[str, ...]:
    """The plaintext credential files no pending import accounts for."""
    files = plaintext_credential_files(store.data_dir)
    if not files:
        return ()
    pending = _recorded_sources(store.import_record() or {})
    return tuple(path for path in files if path not in pending)


def secret_store_health(store: SecretStore) -> dict[str, Any]:
    """The store's status for operators, with why it degrades the data store, if it does.

    Every reason names what to do. Besides the store's own state, it reports
    ignored plaintext credential files (``plaintext_files``), plaintext copies
    in the document quarantine (``plaintext_copies``), and a store found in
    quarantine while ``secrets.json`` is missing (``quarantined_stores``).
    """
    status = store.status()
    projection = status.to_dict()
    reasons: list[str] = []
    if status.state == "unavailable":
        reasons.append(f"the Secret Store is unavailable: {status.reason}")
    if status.damaged:
        reasons.append("Secret Store entries do not decrypt: " + ", ".join(status.damaged))
    ignored: list[_Source] = []
    copies: tuple[str, ...] = ()
    stores: tuple[str, ...] = ()
    try:
        ignored = _ignored_sources(store)
        copies = quarantined_plaintext_files(store.data_dir)
        if status.state == "absent":
            stores = quarantined_secret_stores(store.data_dir)
    except OSError as exc:
        projection["plaintext_check"] = str(exc)
    projection["plaintext_files"] = [source.path for source in ignored]
    projection["plaintext_copies"] = list(copies)
    projection["quarantined_stores"] = list(stores)
    if ignored:
        reasons.append(
            "plaintext credential files are ignored: "
            + "; ".join(f"{source.path} {_remedy(store, source)}" for source in ignored)
        )
    if copies:
        reasons.append(
            "plaintext credential copies lie in the document quarantine: "
            + ", ".join(copies)
            + "; vBot never reads them, delete them once they are no longer needed"
        )
    if stores:
        reasons.append(_quarantined_store_reason(stores))
    projection["degraded_reason"] = "; ".join(reasons) or None
    return projection


def credential_file_reports(data_dir: str | Path) -> list[JsonValidationReport]:
    """``doctor`` warnings about credentials outside the live Secret Store.

    One per plaintext credential file, one per plaintext copy in the document
    quarantine, and one for a store found in quarantine while ``secrets.json``
    is missing.
    """
    root = Path(data_dir)
    try:
        found = _find_plaintext(root)
        copies = quarantined_plaintext_files(root)
        live = stat_or_none(root / SECRET_STORE_FILE_NAME, follow_symlinks=False)
        stores = () if live is not None else quarantined_secret_stores(root)
    except OSError as exc:
        return [
            JsonValidationReport(
                root,
                True,
                (JsonDiagnostic("warning", "$", f"cannot check for plaintext credentials: {exc}"),),
            )
        ]
    reports: list[JsonValidationReport] = []
    if found:
        store = SecretStore(root)
        try:
            pending = _recorded_sources(store.import_record() or {})
        except SecretStoreError:
            pending = {}
        for path, link in found.items():
            if path in pending:
                message = "holds plaintext credentials already imported into the Secret Store; " + (
                    f"it {link} and vBot never deletes it: delete it once the next normal "
                    "start has committed the import"
                    if link is not None
                    else "the next normal start deletes it"
                )
            else:
                source = _inspect_for_report(root, path, link)
                if source is None:
                    continue
                message = (
                    f"holds plaintext credentials that vBot no longer reads; it "
                    f"{_remedy(store, source)}"
                )
            reports.append(_warning(root.joinpath(*path.split("/")), message))
    for path in copies:
        reports.append(
            _warning(
                root.joinpath(*path.split("/")),
                "a plaintext credential copy that a restore moved to quarantine; vBot never "
                "reads it: delete it once it is no longer needed",
            )
        )
    if stores:
        reports.append(_warning(root / SECRET_STORE_FILE_NAME, _quarantined_store_reason(stores)))
    return reports


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_plaintext(root: Path) -> dict[str, str | None]:
    """Every plaintext credential file below ``root``, by relative path, in order.

    The value says why vBot never deletes the file (it is reached through a
    symbolic link or junction), or is ``None``. Raises ``OSError`` when a
    directory cannot be listed.
    """
    found: dict[str, str | None] = {}
    env = root / ENV_FILE_PATH
    status = stat_or_none(env, follow_symlinks=False)
    if status is not None:
        if is_link_status(status):
            found[ENV_FILE_PATH] = _link_reason(env)
        elif stat.S_ISREG(status.st_mode):
            found[ENV_FILE_PATH] = None
    directory = root / OAUTH_DIRECTORY
    status = stat_or_none(directory, follow_symlinks=False)
    if status is None:
        return found
    directory_link = _link_reason(directory) if is_link_status(status) else None
    try:
        with os.scandir(directory) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if entry.name.endswith(_OAUTH_SUFFIX) and not entry.name.startswith(".")
            )
    except FileNotFoundError, NotADirectoryError:
        return found
    for name in names:
        entry_path = directory / name
        status = stat_or_none(entry_path, follow_symlinks=False)
        if status is None:
            continue
        path = f"{_OAUTH_PREFIX}{name}"
        if is_link_status(status):
            found[path] = _link_reason(entry_path)
        elif stat.S_ISREG(status.st_mode):
            found[path] = (
                None
                if directory_link is None
                else f"lies in the oauth directory, which {directory_link}"
            )
    return found


def _link_reason(path: Path) -> str:
    try:
        target = os.readlink(path)
    except OSError, ValueError:
        return "is a link"
    # Windows reports the extended-length form of a junction or symbolic link target.
    return f"is a link to {target.removeprefix(_EXTENDED_PATH_PREFIX)}"


def _inspect(root: Path, path: str, link: str | None) -> _Source | None:
    """Read and parse one plaintext file; ``None`` when it is gone.

    Raises ``OSError`` when the file cannot be read.
    """
    target = root.joinpath(*path.split("/"))
    source = _Source(path, link=link)
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        if stat_or_none(target, follow_symlinks=False) is None:
            return None
        source.problems[path] = "its link points to nothing"
        return source
    source.content = content
    if path == ENV_FILE_PATH:
        parsed = _parse_env(content)
        if parsed is None:
            source.problems[path] = "not valid UTF-8"
            return source
        source.entries, skipped = parsed
        source.problems.update({f"{path}:{line}": reason for line, reason in skipped.items()})
        return source
    token_id, problem = _parse_token(path, content)
    if token_id is None:
        source.problems[path] = problem or "cannot be imported"
    else:
        source.entries[token_id] = content.decode("utf-8")
    return source


def _inspect_for_report(root: Path, path: str, link: str | None) -> _Source | None:
    """:func:`_inspect` for reports: a file that cannot be read is a problem."""
    try:
        return _inspect(root, path, link)
    except OSError as exc:
        return _Source(path, link=link, problems={path: f"cannot be read: {exc}"})


def _ignored_sources(store: SecretStore) -> list[_Source]:
    found = _find_plaintext(store.data_dir)
    if not found:
        return []
    pending = _recorded_sources(store.import_record() or {})
    return [
        source
        for path, link in found.items()
        if path not in pending
        and (source := _inspect_for_report(store.data_dir, path, link)) is not None
    ]


def _remedy(store: SecretStore, source: _Source) -> str:
    """What keeps an ignored plaintext file and what the operator can do about it."""
    details: list[str] = []
    if source.problems:
        details.append(_describe_problems(source))
    differing = _differing(store, source)
    if differing:
        details.append(
            "differs from the Secret Store, which keeps its values: " + ", ".join(differing)
        )
    described = f"({'; '.join(details)})" if details else "(not imported)"
    if source.link is not None:
        if details or store.status().state != "available":
            return (
                f"{source.link} {described}; vBot never deletes it or the file it points to: "
                "delete the link once nothing needs it"
            )
        return (
            f"{source.link}; its credentials are in the Secret Store, and vBot never deletes "
            "it or the file it points to: delete the link"
        )
    if source.problems:
        return (
            f"{described}: fix or remove what cannot be imported and run "
            f"`{CONVERT_COMMAND}`, or delete the file"
        )
    if differing:
        return f"{described}: delete the file, or remove those entries and run `{CONVERT_COMMAND}`"
    return f"{described}: import it with `{CONVERT_COMMAND}`, which then deletes it"


def _describe_problems(source: _Source) -> str:
    described: list[str] = []
    for item, reason in sorted(source.problems.items(), key=lambda pair: _problem_order(pair[0])):
        _path, _separator, line = item.partition(":")
        described.append(f"line {line}: {reason}" if line else reason)
    return "; ".join(described)


def _problem_order(item: str) -> tuple[int, str]:
    _path, _separator, line = item.partition(":")
    return (int(line) if line.isdigit() else 0, item)


def _differing(store: SecretStore, source: _Source) -> list[str]:
    """The entries of ``source`` the available store holds with another value, or not at all."""
    if store.status().state != "available":
        return []
    differing: list[str] = []
    for name, value in sorted(source.entries.items()):
        try:
            stored = store.entry(source.section, name)
        except SecretStoreError:
            stored = None
        if stored != value:
            differing.append(_label(source.section, name))
    return differing


def _held_problem(store: SecretStore, source: _Source) -> str | None:
    """Why the store does not hold every entry of ``source``, if it does not."""
    if source.problems:
        return _describe_problems(source)
    if store.status().state != "available":
        return "the Secret Store is not available"
    differing = _differing(store, source)
    if differing:
        return "differs from the Secret Store (" + ", ".join(differing) + ")"
    return None


def _quarantined_store_reason(stores: tuple[str, ...]) -> str:
    return (
        "secrets.json is missing, but a Secret Store lies in quarantine ("
        + ", ".join(stores)
        + "): if it holds your credentials, stop the server and move it back to "
        "<data-dir>/secrets.json (the newest is listed first)"
    )


def _warning(path: Path, message: str) -> JsonValidationReport:
    return JsonValidationReport(path, True, (JsonDiagnostic("warning", "$", message),))


def _recorded_sources(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The record's plaintext source files as ``{path: {"fingerprint", "entries"}}``.

    A source of the first record format holds only its content fingerprint
    and records no entries: none of its values counts as put there by the
    import. A path that names no plaintext credential file is left out.
    """
    sources = record.get("sources")
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(sources, dict):
        return result
    for path, value in sources.items():
        if not _is_plaintext_path(path):
            continue
        if isinstance(value, str):
            result[path] = {"fingerprint": value, "entries": {}}
        elif isinstance(value, dict) and isinstance(value.get("fingerprint"), str):
            entries = value.get("entries")
            result[path] = {
                "fingerprint": value["fingerprint"],
                "entries": {
                    name: digest
                    for name, digest in (entries if isinstance(entries, dict) else {}).items()
                    if isinstance(name, str) and isinstance(digest, str)
                },
            }
    return result


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
    if record.get("backups_pending"):
        current["backups_pending"] = True
    elif not sources:
        # Nothing to mirror, delete or purge: no record (an absent store stays absent).
        transaction.set_import_record(None)
        return
    previous = {key: value for key, value in record.items() if key != "imported_at"}
    if previous != current or "imported_at" not in record:
        current["imported_at"] = utc_now_timestamp()
        transaction.set_import_record(current)


def _narrowed_record(
    record: Mapping[str, Any], sources: dict[str, dict[str, Any]], *, backups_pending: bool
) -> dict[str, Any] | None:
    if not sources and not backups_pending:
        return None
    narrowed: dict[str, Any] = {"sources": dict(sources)}
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


def _parse_env(content: bytes) -> tuple[dict[str, str], dict[int, str]] | None:
    """The credentials of a ``.env`` file and the lines left out, by line number.

    ``None`` when the file is not UTF-8, which vBot never read either. Names
    take their credential spelling (uppercase on Windows, where a later line
    wins over another spelling as it did in the process environment).
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    values: dict[str, str] = {}
    skipped: dict[int, str] = {}
    for line, name, value in _parse_env_lines(text):
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            skipped[line] = "not a credential name"
        else:
            values[credential_name_key(name)] = value
    # Blank placeholders (the old .env template) hold no credential.
    return {name: value for name, value in values.items() if value}, skipped


def _parse_env_lines(text: str) -> Iterator[tuple[int, str, str]]:
    """Parse ``KEY=VALUE`` lines exactly as vBot read ``.env`` before the Secret Store.

    Yields the line number, key and value of each assignment. CR, LF and CRLF
    end a line; Unicode line separators are part of a value. Blank lines,
    comments and lines without ``=`` are ignored, values may hold further
    ``=``, and one pair of matching outer quotes is removed. There is no
    expansion, escape decoding or command substitution. A later line wins.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            yield number, key, value


def _parse_token(path: str, content: bytes) -> tuple[str | None, str | None]:
    # The token document format belongs to the Provider domain; imported at call
    # time, since the Provider token store builds on this package.
    from core.providers.token_store import validate_oauth_token_data

    stem = path.removeprefix(_OAUTH_PREFIX).removesuffix(_OAUTH_SUFFIX)
    if _TOKEN_STEM_PATTERN.fullmatch(stem) is None or ENTRY_NAME_PATTERN.fullmatch(stem) is None:
        return None, "not an OAuth token file name"
    try:
        data = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None, "not valid JSON"
    errors = [item for item in validate_oauth_token_data(data) if item.severity == "error"]
    if errors:
        return None, "does not load as an OAuth token: " + "; ".join(
            f"{item.path}: {item.message}" for item in errors
        )
    return stem, None


def _verify(store: SecretStore, imported: Mapping[Section, Mapping[str, str]]) -> None:
    """Read the written store back from disk and compare every imported entry."""
    store.reload()
    for section, expected in imported.items():
        for name, value in expected.items():
            if store.entry(section, name) != value:
                raise SecretStoreError(
                    f"the Secret Store does not read back the imported {section} entry {name}"
                )


def _section_of(path: str) -> Section:
    return CREDENTIALS if path == ENV_FILE_PATH else OAUTH_TOKENS


def _label(section: Section, name: str) -> str:
    """How reports name an entry: a credential by name, a token as ``oauth/<token id>``."""
    return name if section == CREDENTIALS else f"{_OAUTH_PREFIX}{name}"


def _is_plaintext_path(path: object) -> bool:
    """Whether a recorded source names a plaintext credential file; never another file."""
    if path == ENV_FILE_PATH:
        return True
    if not isinstance(path, str):
        return False
    if not path.startswith(_OAUTH_PREFIX) or not path.endswith(_OAUTH_SUFFIX):
        return False
    stem = path.removeprefix(_OAUTH_PREFIX).removesuffix(_OAUTH_SUFFIX)
    return _TOKEN_STEM_PATTERN.fullmatch(stem) is not None


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
