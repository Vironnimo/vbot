"""Local doctor checks for the vBot CLI."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from cli.server_management import CommandResult, ServerInstance
from core.settings import SettingsDiagnostic, validate_settings_file
from core.utils.config import Config
from core.utils.logging import resolve_daily_log_path
from server.main import DEFAULT_HOST


def doctor_settings(data_dir: str | Path | None = None) -> CommandResult:
    """Validate the target data-dir settings.json without requiring a server."""

    resolved_data_dir = _resolve_data_dir_without_loading_settings(data_dir)
    instance = _doctor_instance(resolved_data_dir)
    report = validate_settings_file(resolved_data_dir / "settings.json")
    return CommandResult(
        ok=report.ok,
        message=_format_settings_report(resolved_data_dir, report.exists, report.diagnostics),
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


def _format_settings_report(
    data_dir: Path,
    exists: bool,
    diagnostics: tuple[SettingsDiagnostic, ...],
) -> str:
    failed = any(diagnostic.severity == "error" for diagnostic in diagnostics)
    lines = [
        f"doctor settings: {'failed' if failed else 'ok'}",
        f"data_dir: {data_dir}",
        f"file: {data_dir / 'settings.json'}",
    ]
    if not exists:
        lines.append("status: missing (defaults will be used)")
        return "\n".join(lines)

    if not diagnostics:
        lines.append("status: valid")
        return "\n".join(lines)

    error_count = sum(1 for diagnostic in diagnostics if diagnostic.severity == "error")
    warning_count = sum(1 for diagnostic in diagnostics if diagnostic.severity == "warning")
    if error_count:
        lines.append(f"errors: {error_count}")
    if warning_count:
        lines.append(f"warnings: {warning_count}")
    lines.extend(_format_diagnostic(diagnostic) for diagnostic in diagnostics)
    return "\n".join(lines)


def _format_diagnostic(diagnostic: SettingsDiagnostic) -> str:
    return f"- {diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
