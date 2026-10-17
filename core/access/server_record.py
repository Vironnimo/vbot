"""The access configuration a data directory records for its server.

A server keeps its access configuration and bind address in the owner-only
Generation 1 document ``<data-dir>/access/server.json``. A start without access
flags (``vbot server start|restart``, a restart the server scheduled itself)
serves exactly what it records; without the record the mode would follow the
bind address, and an ``authenticated`` server would come back without login.
Explicit access flags replace the record deliberately. A record that cannot be
read refuses the start: the server never guesses a mode for a data directory
that chose one.

A packaged installation's own server runs with its installation record and
mirrors it here (replacing a record it cannot read), so that the data directory
never serves a weaker mode under another launcher or port.

The document is outside data snapshots and configuration backups: a restore
must never bring back a weaker mode the host replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.access.config import (
    AccessConfig,
    AccessConfigError,
    access_config_from_record,
    check_bind_host,
)
from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    format_report_diagnostics,
    read_json_file,
    validate_json_file,
    warn_unknown_keys,
)
from core.json_documents import (
    DURABLE_DOCUMENTS,
    JsonDocumentFormat,
    json_document,
    strip_unknown_fields,
    validate_format_version,
    write_json_document,
)

SERVER_ACCESS_FORMAT_VERSION = 1
SERVER_ACCESS_PATH = DURABLE_DOCUMENTS["access_server"]

_FIELDS = frozenset(
    (
        "host",
        "mode",
        "allow_networks",
        "allow_hosts",
        "public_url",
        "trusted_proxies",
        "tls_cert",
        "tls_key",
    )
)
SERVER_ACCESS_SHAPE = json_document(_FIELDS)


@dataclass(frozen=True)
class RecordedServerAccess:
    """The access configuration and bind address a server of the data directory serves."""

    access: AccessConfig
    host: str


def server_access_path(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser() / SERVER_ACCESS_PATH


def read_server_access(data_dir: str | Path) -> RecordedServerAccess | None:
    """The recorded configuration; ``None`` when the data directory records none.

    Raises :class:`AccessConfigError` when a record exists but cannot be read or
    no longer validates: the server must not start without knowing its mode.
    """
    path = server_access_path(data_dir)
    report, data = read_json_file(path, _validate_document, missing_ok=True)
    if not report.exists:
        return None
    if not report.ok or not isinstance(data, dict):
        details = "; ".join(format_report_diagnostics(report))
        raise AccessConfigError(
            f"the access configuration this data directory records cannot be read ({path}: "
            f"{details}); repair the file, or remove it and start the server with the "
            "access options it should serve"
        )
    return _parse(strip_unknown_fields(data, SERVER_ACCESS_SHAPE))


def write_server_access(data_dir: str | Path, recorded: RecordedServerAccess) -> None:
    """Record the configuration (blocking), replacing what the file holds, even if unreadable.

    The caller has decided what the data directory serves: a server refuses an
    unreadable record before it gets here, unless an installation record is the
    authority. Raises ``OSError`` or ``ValueError`` on failure.
    """
    path = server_access_path(data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json_document(
        path,
        {"host": recorded.host, **recorded.access.to_record()},
        SERVER_ACCESS_FORMAT,
        owner_only=True,
        reset=True,
    )


def validate_server_access_file(path: str | Path) -> JsonValidationReport:
    """Validate the recorded server access configuration without loading it."""
    return validate_json_file(path, _validate_document, missing_ok=True)


def _validate_document(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        add_error(diagnostics, "$", "must be a JSON object")
        return diagnostics
    if not validate_format_version(diagnostics, data, SERVER_ACCESS_FORMAT_VERSION):
        return diagnostics
    warn_unknown_keys(diagnostics, "$", data, SERVER_ACCESS_SHAPE.fields, "field")
    try:
        _parse(strip_unknown_fields(data, SERVER_ACCESS_SHAPE))
    except AccessConfigError as exc:
        add_error(diagnostics, "$", str(exc))
    return diagnostics


def _parse(document: dict[str, Any]) -> RecordedServerAccess:
    host = document.get("host")
    if not isinstance(host, str) or not host.strip():
        raise AccessConfigError("the recorded bind address must be a non-empty string")
    access = access_config_from_record(document)
    check_bind_host(access, host)
    return RecordedServerAccess(access=access, host=host)


SERVER_ACCESS_FORMAT = JsonDocumentFormat(
    name="Server access",
    version=SERVER_ACCESS_FORMAT_VERSION,
    shape=SERVER_ACCESS_SHAPE,
    validate=_validate_document,
)
