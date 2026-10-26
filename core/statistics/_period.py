"""The instance's optional measurement start, independent of the disposable index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    validate_format_version,
    warn_unknown_fields,
    write_json_document,
)
from core.utils.errors import VBotError
from core.utils.timestamps import parse_canonical_timestamp

_SHAPE = json_document({"started_at"})


class StatisticsPeriodError(VBotError):
    """The saved measurement start could not be read or changed."""


def _validate(data: Any) -> list[JsonDiagnostic]:
    if not isinstance(data, dict):
        return [error_diagnostic("$", "Expected a JSON object")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, 1):
        return diagnostics
    warn_unknown_fields(diagnostics, "$", data, _SHAPE)
    value = data.get("started_at")
    if value is not None:
        try:
            if not isinstance(value, str):
                raise ValueError("Expected a timestamp")
            parse_canonical_timestamp(value)
        except ValueError:
            add_error(diagnostics, "$.started_at", "Expected a canonical UTC timestamp or null")
    return diagnostics


_FORMAT = JsonDocumentFormat("Statistics measurement period", 1, _SHAPE, _validate)


def validate_statistics_period_file(path: str | Path) -> JsonValidationReport:
    return validate_json_file(path, _validate, missing_ok=True)


def read_period(path: Path) -> str | None:
    try:
        data = load_validated_json_file(path, _validate, missing_ok=True, missing_default={})
    except (JsonConfigValidationError, OSError) as error:
        raise StatisticsPeriodError(
            f"The Statistics measurement period could not be read: {error}"
        ) from error
    value: str | None = data.get("started_at")
    return value


def write_period(path: Path, started_at: str | None) -> None:
    try:
        write_json_document(path, {"started_at": started_at}, _FORMAT)
    except (JsonConfigValidationError, OSError) as error:
        raise StatisticsPeriodError(
            f"The Statistics measurement period could not be saved: {error}"
        ) from error
