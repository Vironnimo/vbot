"""Shared JSON configuration diagnostics, file loading, and shape primitives.

Domain modules own their persisted schemas. This module owns only the transport-
neutral mechanics those schemas share: stable diagnostics, JSON file handling,
and small reusable shape checks.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DiagnosticSeverity = Literal["error", "warning"]
JsonObject = dict[str, Any]
JsonValidator = Callable[[Any], list["JsonDiagnostic"]]


class JsonConfigValidationError(ValueError):
    """Raised when a JSON configuration file cannot be consumed safely."""


@dataclass(frozen=True)
class JsonDiagnostic:
    """One JSON configuration validation diagnostic."""

    severity: DiagnosticSeverity
    path: str
    message: str


@dataclass(frozen=True)
class JsonValidationReport:
    """Validation result for one JSON configuration file."""

    file_path: Path
    exists: bool
    diagnostics: tuple[JsonDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    @property
    def error_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.severity == "warning")


@dataclass(frozen=True)
class _ValidatedDocument:
    report: JsonValidationReport
    data: Any = None


def validate_json_file(
    file_path: str | Path,
    validator: JsonValidator,
    *,
    missing_ok: bool,
) -> JsonValidationReport:
    """Validate one JSON file without mutating or consuming its decoded value."""
    return _load_and_validate_json_file(Path(file_path), validator, missing_ok=missing_ok).report


def load_validated_json_file(
    file_path: str | Path,
    validator: JsonValidator,
    *,
    missing_ok: bool,
    missing_default: Any = None,
) -> Any:
    """Decode and validate one JSON file, raising a stable configuration error."""
    document = _load_and_validate_json_file(Path(file_path), validator, missing_ok=missing_ok)
    if not document.report.ok:
        details = "; ".join(format_report_diagnostics(document.report))
        raise JsonConfigValidationError(f"{document.report.file_path}: {details}")
    if not document.report.exists:
        return missing_default
    return document.data


def format_report_diagnostics(report: JsonValidationReport) -> list[str]:
    return [
        f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
        for diagnostic in report.diagnostics
    ]


def warn_unknown_keys(
    diagnostics: list[JsonDiagnostic],
    parent_path: str,
    data: Mapping[str, Any],
    known_keys: frozenset[str],
    label: str,
) -> None:
    for key in sorted(set(data) - known_keys):
        diagnostics.append(
            JsonDiagnostic(
                severity="warning",
                path=child_path(parent_path, key),
                message=f"unknown {label}: {key}",
            )
        )


def validate_required_fields(
    diagnostics: list[JsonDiagnostic],
    parent_path: str,
    data: Mapping[str, Any],
    required_fields: frozenset[str],
) -> None:
    for field in sorted(required_fields - set(data)):
        add_error(diagnostics, child_path(parent_path, field), "is required")


def validate_positive_integer(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, required: bool
) -> None:
    if value is None:
        if required:
            add_error(diagnostics, path, "is required")
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        add_error(diagnostics, path, "must be a positive integer")


def validate_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, required: bool
) -> None:
    if value is None:
        if required:
            add_error(diagnostics, path, "is required")
        return
    if not isinstance(value, str):
        add_error(diagnostics, path, "must be a string")


def validate_optional_string(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        add_error(diagnostics, path, "must be a string or null")


def validate_non_empty_string(
    diagnostics: list[JsonDiagnostic], path: str, value: Any, *, required: bool
) -> None:
    if value is None:
        if required:
            add_error(diagnostics, path, "is required")
        return
    if not isinstance(value, str) or not value.strip():
        add_error(diagnostics, path, "must be a non-empty string")


def validate_allowed_string(
    diagnostics: list[JsonDiagnostic],
    path: str,
    value: Any,
    allowed: frozenset[str],
) -> None:
    if value is None:
        add_error(diagnostics, path, "is required")
        return
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        add_error(diagnostics, path, f"must be one of: {choices}")


def validate_optional_allowed_string(
    diagnostics: list[JsonDiagnostic],
    path: str,
    value: Any,
    allowed: frozenset[str],
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        add_error(diagnostics, path, f"must be one of: {choices}")


def validate_optional_path_string(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is None or value == "":
        return
    if not isinstance(value, str):
        add_error(diagnostics, path, "must be a path string")


def validate_string_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is None:
        add_error(diagnostics, path, "is required")
        return
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            add_error(diagnostics, f"{path}[{index}]", "must be a string")


def validate_optional_string_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    """Validate an optional list whose present entries are non-empty strings."""
    if value is None:
        return
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must be a non-empty string")


def child_path(parent_path: str, key: str) -> str:
    if key.replace("_", "").isalnum():
        return f"{parent_path}.{key}"
    return f"{parent_path}[{key!r}]"


def add_error(diagnostics: list[JsonDiagnostic], path: str, message: str) -> None:
    diagnostics.append(error_diagnostic(path, message))


def error_diagnostic(path: str, message: str) -> JsonDiagnostic:
    return JsonDiagnostic(severity="error", path=path, message=message)


def _load_and_validate_json_file(
    file_path: Path,
    validator: JsonValidator,
    *,
    missing_ok: bool,
) -> _ValidatedDocument:
    if not file_path.exists():
        diagnostics: tuple[JsonDiagnostic, ...] = ()
        if not missing_ok:
            diagnostics = (error_diagnostic("$", "File does not exist"),)
        return _ValidatedDocument(
            report=JsonValidationReport(file_path=file_path, exists=False, diagnostics=diagnostics)
        )

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _ValidatedDocument(
            report=JsonValidationReport(
                file_path=file_path,
                exists=True,
                diagnostics=(
                    error_diagnostic(
                        "$",
                        f"Invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
                    ),
                ),
            )
        )
    except OSError as exc:
        return _ValidatedDocument(
            report=JsonValidationReport(
                file_path=file_path,
                exists=True,
                diagnostics=(error_diagnostic("$", f"Cannot read file: {exc}"),),
            )
        )

    return _ValidatedDocument(
        report=JsonValidationReport(
            file_path=file_path,
            exists=True,
            diagnostics=tuple(validator(data)),
        ),
        data=data,
    )
