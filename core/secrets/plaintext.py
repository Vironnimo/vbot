"""Conversion of the plaintext credential files into the Secret Store.

Before the Secret Store, credentials lay in plaintext: ``<data-dir>/.env`` and
one ``<data-dir>/oauth/<token id>.json`` per OAuth login. The conversion has two
steps, so a failed or rolled-back update never leaves an installation without
its credentials:

1. :func:`import_plaintext_credentials` copies every credential into the store,
   reads the written store back to verify it, and records each source file with
   a keyed fingerprint. It never changes a plaintext file and can repeat any
   number of times: while an import is pending, the store mirrors the files.
   An update runs it in the new version's verification start, which every
   update runs before it activates the new version; a failure there fails the
   verification, and the previous version keeps running on its plaintext files.
2. :func:`retire_plaintext_credentials` runs once the import is committed (the
   first normal start, or the explicit ``vbot data-store secrets convert``):
   it deletes each recorded file whose content is unchanged and whose entries
   the store still holds, then purges the plaintext copies from configuration
   backups and data snapshots. A file that changed since the import stays and
   is reported.

vBot never reads the plaintext files otherwise: a plaintext credential file
found outside a pending import is ignored with a warning and reported by
``doctor`` and the data-store status.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.config_validation import JsonDiagnostic, JsonValidationReport
from core.json_documents import matching_data_paths
from core.secrets.store import (
    CREDENTIALS,
    ENTRY_NAME_PATTERN,
    OAUTH_TOKENS,
    SecretStore,
    SecretStoreError,
    SecretTransaction,
    Section,
)
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

ENV_FILE_PATH = ".env"
OAUTH_TOKEN_PATTERN = "oauth/*.json"
#: The plaintext credential files of a data directory before the Secret Store.
PLAINTEXT_CREDENTIAL_PATTERNS: tuple[str, ...] = (ENV_FILE_PATH, OAUTH_TOKEN_PATTERN)
CONVERT_COMMAND = "vbot data-store secrets convert --yes"
# The file stem of a token file: ``<provider>-<connection>[--<account>]``.
_TOKEN_STEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_OAUTH_PREFIX = "oauth/"
_OAUTH_SUFFIX = ".json"

_LOGGER = get_logger("secrets")


@dataclass(frozen=True)
class PlaintextImport:
    """What one import copied into the store; names only, never values."""

    sources: tuple[str, ...]
    credentials: tuple[str, ...]
    oauth_tokens: tuple[str, ...]
    #: Entries an earlier pending import held that the files no longer hold.
    removed: tuple[str, ...] = ()
    #: Files or ``<file>:<name>`` entries left out, with the reason.
    skipped: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlaintextRetirement:
    """What retiring an import deleted and kept."""

    deleted: tuple[str, ...] = ()
    kept: Mapping[str, str] = field(default_factory=dict)
    backup_entries: int = 0
    snapshot_copies: int = 0
    backups_complete: bool = True


@dataclass
class _Parsed:
    contents: dict[str, bytes] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    oauth_tokens: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    #: Source files fully held by the store once imported.
    sources: list[str] = field(default_factory=list)


def plaintext_credential_files(data_dir: str | Path) -> tuple[str, ...]:
    """Every plaintext credential file of ``data_dir`` as a relative POSIX path.

    Raises ``OSError`` when the data directory cannot be listed.
    """
    return matching_data_paths(Path(data_dir), PLAINTEXT_CREDENTIAL_PATTERNS)


def import_plaintext_credentials(store: SecretStore) -> PlaintextImport | None:
    """Copy the plaintext credential files into ``store`` and verify the result.

    Returns ``None`` when the data directory holds no plaintext credential file.
    Never changes a plaintext file. Raises :class:`SecretStoreError` when the
    store cannot take the import (an unavailable store is never replaced) or
    does not read back what was written, and ``OSError`` when a file cannot be
    read.
    """
    data_dir = store.data_dir
    paths = plaintext_credential_files(data_dir)
    if not paths:
        return None
    parsed = _parse(data_dir, paths)

    def change(transaction: SecretTransaction) -> tuple[str, ...]:
        record = transaction.import_record or {}
        removed: list[str] = []
        if record.get("sources"):
            # An earlier import is still pending: the store mirrors the files.
            for name in record.get(CREDENTIALS, []):
                if name not in parsed.credentials and transaction.remove(CREDENTIALS, name):
                    removed.append(name)
            for name in record.get(OAUTH_TOKENS, []):
                if name not in parsed.oauth_tokens and transaction.remove(OAUTH_TOKENS, name):
                    removed.append(f"{_OAUTH_PREFIX}{name}")
        for name, value in parsed.credentials.items():
            transaction.set(CREDENTIALS, name, value)
        for name, value in parsed.oauth_tokens.items():
            transaction.set(OAUTH_TOKENS, name, value)
        current: dict[str, Any] = {
            "sources": {
                path: transaction.fingerprint(parsed.contents[path]) for path in parsed.sources
            },
            CREDENTIALS: sorted(parsed.credentials),
            OAUTH_TOKENS: sorted(parsed.oauth_tokens),
        }
        if record.get("backups_pending"):
            current["backups_pending"] = True
        previous = {key: value for key, value in record.items() if key != "imported_at"}
        if previous != current or "imported_at" not in record:
            current["imported_at"] = utc_now_timestamp()
            transaction.set_import_record(current)
        return tuple(removed)

    removed = store.update(change)
    _verify(store, parsed)
    result = PlaintextImport(
        sources=tuple(parsed.sources),
        credentials=tuple(sorted(parsed.credentials)),
        oauth_tokens=tuple(sorted(parsed.oauth_tokens)),
        removed=removed,
        skipped=dict(parsed.skipped),
    )
    _LOGGER.info(
        "Imported plaintext credentials into the Secret Store; the files stay until the "
        "import is committed (credentials=%d oauth_tokens=%d removed=%d files=%s)",
        len(result.credentials),
        len(result.oauth_tokens),
        len(result.removed),
        ",".join(result.sources) or "none",
    )
    if parsed.skipped:
        _LOGGER.warning(
            "Plaintext credentials left out of the import stay in their files (entries=%s)",
            ",".join(f"{item} ({reason})" for item, reason in sorted(parsed.skipped.items())),
        )
    if "LOG_LEVEL" in parsed.credentials:
        _LOGGER.warning(
            "LOG_LEVEL in .env no longer selects the log level; set it in the server's "
            "process environment instead"
        )
    return result


def retire_plaintext_credentials(store: SecretStore) -> PlaintextRetirement | None:
    """Delete the imported plaintext files and purge their copies from backups.

    Runs only for a recorded import, and only deletes a file whose content is
    unchanged since the import and whose every entry the store still holds.
    Returns ``None`` when no import is recorded. Raises
    :class:`SecretStoreError` when the store is not available.
    """
    record = store.import_record()
    if record is None:
        return None
    data_dir = store.data_dir
    deleted: list[str] = []
    kept: dict[str, str] = {}
    remaining: dict[str, str] = {}
    for path, recorded in sorted((record.get("sources") or {}).items()):
        if not _is_plaintext_path(path):
            continue
        target = data_dir.joinpath(*path.split("/"))
        try:
            content = target.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            kept[path] = f"cannot be read: {exc}"
            remaining[path] = recorded
            continue
        if store.fingerprint(content) != recorded:
            kept[path] = "changed after the import"
            continue
        problem = _source_problem(store, path, content)
        if problem is not None:
            kept[path] = problem
            continue
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            kept[path] = f"cannot be deleted: {exc}"
            remaining[path] = recorded
            continue
        deleted.append(path)
    _remove_empty_oauth_directory(data_dir)
    backup_entries, snapshot_copies, complete = _purge_copies(data_dir)
    replacement: dict[str, Any] | None = None
    if remaining:
        replacement = {
            "sources": remaining,
            CREDENTIALS: list(record.get(CREDENTIALS, [])),
            OAUTH_TOKENS: list(record.get(OAUTH_TOKENS, [])),
        }
        if "imported_at" in record:
            replacement["imported_at"] = record["imported_at"]
    if not complete:
        replacement = {**(replacement or {}), "backups_pending": True}
    store.update(lambda transaction: transaction.set_import_record(replacement))
    result = PlaintextRetirement(
        deleted=tuple(deleted),
        kept=kept,
        backup_entries=backup_entries,
        snapshot_copies=snapshot_copies,
        backups_complete=complete,
    )
    if deleted or backup_entries or snapshot_copies:
        _LOGGER.info(
            "Plaintext credential files removed after their import into the Secret Store "
            "(files=%s backup_entries=%d snapshot_copies=%d)",
            ",".join(deleted) or "none",
            backup_entries,
            snapshot_copies,
        )
    if kept:
        _LOGGER.warning(
            "Plaintext credential files kept; vBot ignores them. Import them with `%s` "
            "or delete them (files=%s)",
            CONVERT_COMMAND,
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
            "Secret Store. Import them with `%s`, which then deletes them (files=%s)",
            CONVERT_COMMAND,
            ",".join(ignored),
        )


def ignored_plaintext_files(store: SecretStore) -> tuple[str, ...]:
    """The plaintext credential files no pending import accounts for."""
    files = plaintext_credential_files(store.data_dir)
    if not files:
        return ()
    record = store.import_record() or {}
    pending = record.get("sources") or {}
    return tuple(path for path in files if path not in pending)


def secret_store_health(store: SecretStore) -> dict[str, Any]:
    """The store's status for operators, with why it degrades the data store, if it does."""
    status = store.status()
    projection = status.to_dict()
    try:
        ignored = list(ignored_plaintext_files(store))
    except OSError as exc:
        ignored = []
        projection["plaintext_check"] = str(exc)
    projection["plaintext_files"] = ignored
    reasons: list[str] = []
    if status.state == "unavailable":
        reasons.append(f"the Secret Store is unavailable: {status.reason}")
    if status.damaged:
        reasons.append("Secret Store entries do not decrypt: " + ", ".join(status.damaged))
    if ignored:
        reasons.append(
            "plaintext credential files are ignored: "
            + ", ".join(ignored)
            + f"; import them with `{CONVERT_COMMAND}`"
        )
    projection["degraded_reason"] = "; ".join(reasons) or None
    return projection


def plaintext_credential_reports(data_dir: str | Path) -> list[JsonValidationReport]:
    """One ``doctor`` warning per plaintext credential file of ``data_dir``."""
    root = Path(data_dir)
    try:
        files = plaintext_credential_files(root)
    except OSError as exc:
        return [
            JsonValidationReport(
                root,
                True,
                (JsonDiagnostic("warning", "$", f"cannot check for plaintext credentials: {exc}"),),
            )
        ]
    if not files:
        return []
    store = SecretStore(root)
    try:
        pending = (store.import_record() or {}).get("sources") or {}
    except SecretStoreError:
        pending = {}
    reports: list[JsonValidationReport] = []
    for path in files:
        message = (
            "holds plaintext credentials already imported into the Secret Store; the next "
            "normal start deletes it"
            if path in pending
            else (
                "holds plaintext credentials that vBot no longer reads; import them with "
                f"`{CONVERT_COMMAND}` (which deletes the file) or delete it"
            )
        )
        reports.append(
            JsonValidationReport(
                root.joinpath(*path.split("/")),
                True,
                (JsonDiagnostic("warning", "$", message),),
            )
        )
    return reports


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse(data_dir: Path, paths: tuple[str, ...]) -> _Parsed:
    parsed = _Parsed()
    for path in paths:
        content = data_dir.joinpath(*path.split("/")).read_bytes()
        parsed.contents[path] = content
        if path == ENV_FILE_PATH:
            env = _parse_env(content)
            if env is None:
                parsed.skipped[path] = "not valid UTF-8"
                continue
            credentials, skipped = env
            parsed.credentials.update(credentials)
            parsed.skipped.update({f"{path}:{name}": reason for name, reason in skipped.items()})
            # A file with entries left out stays: deleting it would lose them.
            if not skipped:
                parsed.sources.append(path)
            continue
        token_id, problem = _parse_token(path, content)
        if token_id is None:
            parsed.skipped[path] = problem or "cannot be imported"
            continue
        parsed.oauth_tokens[token_id] = content.decode("utf-8")
        parsed.sources.append(path)
    return parsed


def _parse_env(content: bytes) -> tuple[dict[str, str], dict[str, str]] | None:
    """The credentials of a ``.env`` file and the entries left out, by name.

    ``None`` when the file is not UTF-8, which vBot never read either.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    credentials: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for name, value in _parse_env_text(text).items():
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            skipped[name] = "not a credential name"
        elif value:
            # Blank placeholders (the old .env template) hold no credential.
            credentials[name] = value
    return credentials, skipped


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines exactly as vBot read ``.env`` before the Secret Store.

    CR, LF and CRLF end a line; Unicode line separators are part of a value.
    Blank lines, comments and lines without ``=`` are ignored, values may hold
    further ``=``, and one pair of matching outer quotes is removed. There is
    no expansion, escape decoding or command substitution. A later line wins.
    """
    values: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


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


def _verify(store: SecretStore, parsed: _Parsed) -> None:
    """Read the written store back from disk and compare every imported entry."""
    store.reload()
    imported: tuple[tuple[Section, dict[str, str]], ...] = (
        (CREDENTIALS, parsed.credentials),
        (OAUTH_TOKENS, parsed.oauth_tokens),
    )
    for section, expected in imported:
        for name, value in expected.items():
            if store.entry(section, name) != value:
                raise SecretStoreError(
                    f"the Secret Store does not read back the imported {section} entry {name}"
                )


def _source_problem(store: SecretStore, path: str, content: bytes) -> str | None:
    """Why the store does not hold every entry of one source file, if it does not."""
    if path == ENV_FILE_PATH:
        env = _parse_env(content)
        if env is None:
            return "not valid UTF-8"
        expected, skipped = env
        if skipped:
            return "holds entries that cannot be imported"
        section: Section = CREDENTIALS
    else:
        token_id, problem = _parse_token(path, content)
        if token_id is None:
            return problem
        expected = {token_id: content.decode("utf-8")}
        section = OAUTH_TOKENS
    for name, value in expected.items():
        try:
            stored = store.entry(section, name)
        except SecretStoreError:
            stored = None
        if stored != value:
            return f"differs from the Secret Store ({name})"
    return None


def _is_plaintext_path(path: str) -> bool:
    """Whether a recorded source names a plaintext credential file; never another file."""
    if path == ENV_FILE_PATH:
        return True
    if not path.startswith(_OAUTH_PREFIX) or not path.endswith(_OAUTH_SUFFIX):
        return False
    stem = path.removeprefix(_OAUTH_PREFIX).removesuffix(_OAUTH_SUFFIX)
    return _TOKEN_STEM_PATTERN.fullmatch(stem) is not None


def _remove_empty_oauth_directory(data_dir: Path) -> None:
    # A directory that still holds files (or is absent) stays as it is.
    with suppress(OSError):
        (data_dir / "oauth").rmdir()


def _purge_copies(data_dir: Path) -> tuple[int, int, bool]:
    from core.database import (
        DatabaseError,
        purge_config_backup_files,
        remove_retired_document_copies,
    )

    try:
        purge = purge_config_backup_files(data_dir, PLAINTEXT_CREDENTIAL_PATTERNS)
        copies = remove_retired_document_copies(data_dir, (OAUTH_TOKEN_PATTERN,))
    except (DatabaseError, OSError) as exc:
        _LOGGER.warning(
            "Plaintext credential copies in configuration backups or data snapshots were "
            "not purged; the next start tries again: %s",
            exc,
        )
        return 0, 0, False
    if not purge.complete:
        _LOGGER.warning(
            "Plaintext credential copies stay in configuration backups while a backup "
            "manifest cannot be read; the next start tries again"
        )
    return purge.removed_entries, copies, purge.complete
