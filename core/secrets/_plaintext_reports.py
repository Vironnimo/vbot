"""Operator diagnostics for plaintext credentials and quarantined Secret Stores."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config_validation import JsonDiagnostic, JsonValidationReport
from core.secrets._plaintext_files import (
    _find_plaintext,
    _inspect_for_report,
    _label,
    _pending_sources,
    _Source,
    plaintext_credential_files,
    quarantined_plaintext_files,
    quarantined_secret_stores,
)
from core.secrets.store import (
    SECRET_STORE_FILE_NAME,
    SecretStore,
    SecretStoreError,
)
from core.utils.file_status import stat_or_none

CONVERT_COMMAND = "vbot data-store secrets convert --yes"


def ignored_plaintext_files(store: SecretStore) -> tuple[str, ...]:
    """The plaintext credential files no pending import accounts for."""
    files = plaintext_credential_files(store.data_dir)
    if not files:
        return ()
    pending = _pending_sources(store.import_record() or {})
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
            pending = _pending_sources(store.import_record() or {})
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


def _ignored_sources(store: SecretStore) -> list[_Source]:
    found = _find_plaintext(store.data_dir)
    if not found:
        return []
    pending = _pending_sources(store.import_record() or {})
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
