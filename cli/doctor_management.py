"""Local doctor checks for the vBot CLI."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from cli.server_management import CommandResult, ServerInstance
from core.settings import (
    JsonDiagnostic,
    JsonValidationReport,
    validate_data_dir_config,
    validate_settings_file,
)
from core.utils.config import DEFAULT_HOST, Config
from core.utils.logging import resolve_daily_log_path


def doctor_settings(data_dir: str | Path | None = None) -> CommandResult:
    """Validate the target data-dir settings.json without requiring a server."""

    resolved_data_dir = _resolve_data_dir_without_loading_settings(data_dir)
    instance = _doctor_instance(resolved_data_dir)
    report = validate_settings_file(resolved_data_dir / "settings.json")
    return CommandResult(
        ok=report.ok,
        message=_format_settings_report(resolved_data_dir, report),
        instance=instance,
    )


def doctor_config(data_dir: str | Path | None = None) -> CommandResult:
    """Validate all user-editable JSON configuration files in the data-dir."""

    resolved_data_dir = _resolve_data_dir_without_loading_settings(data_dir)
    instance = _doctor_instance(resolved_data_dir)
    reports = validate_data_dir_config(resolved_data_dir)
    ok = all(report.ok for report in reports)
    return CommandResult(
        ok=ok,
        message=_format_config_report(resolved_data_dir, reports),
        instance=instance,
    )


def _resolve_data_dir_without_loading_settings(data_dir: str | Path | None) -> Path:
    sentinel_settings_path = Path.cwd() / f".vbot-doctor-ignore-settings-{uuid4().hex}.json"
    config = Config(
        data_dir=Path(data_dir) if data_dir is not None else None,
        settings_path=sentinel_settings_path,
    )
    return config.data_dir.expanduser().resolve()


def _doctor_instance(data_dir: Path) -> ServerInstance:
    return ServerInstance(
        host=DEFAULT_HOST,
        port=0,
        data_dir=data_dir,
        url="local",
        log_path=resolve_daily_log_path(data_dir),
    )


def _format_settings_report(data_dir: Path, report: JsonValidationReport) -> str:
    failed = not report.ok
    lines = [
        f"doctor settings: {'failed' if failed else 'ok'}",
        f"data_dir: {data_dir}",
        f"file: {data_dir / 'settings.json'}",
    ]
    if not report.exists:
        lines.append("status: missing (defaults will be used)")
        return "\n".join(lines)

    if not report.diagnostics:
        lines.append("status: valid")
        return "\n".join(lines)

    lines.extend(_format_report_counts(report))
    lines.extend(_format_diagnostic(diagnostic) for diagnostic in report.diagnostics)
    return "\n".join(lines)


def _format_config_report(data_dir: Path, reports: tuple[JsonValidationReport, ...]) -> str:
    failed = any(not report.ok for report in reports)
    error_count = sum(report.error_count for report in reports)
    warning_count = sum(report.warning_count for report in reports)
    lines = [
        f"doctor config: {'failed' if failed else 'ok'}",
        f"data_dir: {data_dir}",
        f"files_checked: {len(reports)}",
    ]
    if error_count:
        lines.append(f"errors: {error_count}")
    if warning_count:
        lines.append(f"warnings: {warning_count}")

    for report in reports:
        relative_path = _relative_report_path(data_dir, report.file_path)
        if not report.exists:
            lines.append(f"{relative_path}: missing (defaults will be used)")
            continue
        if not report.diagnostics:
            lines.append(f"{relative_path}: valid")
            continue
        lines.append(f"{relative_path}:")
        lines.extend(_format_diagnostic(diagnostic) for diagnostic in report.diagnostics)
    return "\n".join(lines)


def _format_report_counts(report: JsonValidationReport) -> list[str]:
    lines: list[str] = []
    if report.error_count:
        lines.append(f"errors: {report.error_count}")
    if report.warning_count:
        lines.append(f"warnings: {report.warning_count}")
    return lines


def _format_diagnostic(diagnostic: JsonDiagnostic) -> str:
    return f"- {diagnostic.severity} {diagnostic.path}: {diagnostic.message}"


def _relative_report_path(data_dir: Path, file_path: Path) -> str:
    try:
        return file_path.relative_to(data_dir).as_posix()
    except ValueError:
        return str(file_path)
