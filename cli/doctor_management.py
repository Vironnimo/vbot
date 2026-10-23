"""Read-only local configuration checks and actionable Doctor reports."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from cli._progress import Status, status_line
from cli._recovery import format_command
from cli._server_target import CommandResult, ServerInstance
from cli.formatting import inline_text
from core.config_validation import JsonDiagnostic, JsonValidationReport
from core.settings import validate_data_dir_config, validate_settings_file
from core.utils.config import DEFAULT_HOST, Config
from core.utils.file_status import stat_or_none
from core.utils.logging import resolve_daily_log_path

_TEXT_BUDGET = 10000
_SCOPE = (
    "Local JSON configuration; runtime, Provider connectivity and database integrity "
    "are not checked."
)
_RECOMMENDATIONS = {
    "unknown_field": (
        "This version does not use this field. Remove it if it is obsolete; "
        "keep it if another vBot version still needs it."
    ),
    "invalid_json": (
        "Repair the JSON syntax at the reported location, "
        "or restore a known-good copy of this file."
    ),
    "invalid_encoding": "Save this file as UTF-8, preserving its contents, then rerun Doctor.",
    "unreadable_file": (
        "Restore read access to this file and its parent directories, then rerun Doctor."
    ),
    "discovery_failed": (
        "Restore access to this path, then rerun Doctor "
        "to check the files that could not be inspected."
    ),
    "linked_path": (
        "Check the linked target separately; Doctor does not follow links in configuration bundles."
    ),
    "wrong_file_type": "Restore the expected file or directory at this path, then rerun Doctor.",
    "missing_data_dir": "Correct --data-dir to name an existing vBot data directory.",
    "missing_file": "Restore this required file from a known-good copy.",
    "validator_failed": (
        "Report this validator failure with the file path and vBot version; "
        "the file was not fully checked."
    ),
    "missing_validator": (
        "Restore the owning Extension with its config_documents validator, then rerun Doctor."
    ),
    "extension_validation_failed": (
        "Check the Extension path, manifest and offline validator; "
        "repair or update the Extension, then rerun Doctor."
    ),
}


def doctor_settings(
    data_dir: str | Path | None = None,
    *,
    json_output: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> CommandResult:
    """Validate settings.json without starting a server or changing files."""
    return _doctor("settings", data_dir, json_output=json_output, limit=limit, offset=offset)


def doctor_config(
    data_dir: str | Path | None = None,
    *,
    json_output: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> CommandResult:
    """Validate the durable JSON bundle, including Extension-owned documents."""
    return _doctor("config", data_dir, json_output=json_output, limit=limit, offset=offset)


def _doctor(
    check: str, data_dir: str | Path | None, *, json_output: bool, limit: int, offset: int
) -> CommandResult:
    root = _resolve_data_dir_without_loading_settings(data_dir)
    root_failure = _check_data_dir(root)
    reports = (
        (root_failure,)
        if root_failure
        else (
            (validate_settings_file(root / "settings.json"),)
            if check == "settings"
            else validate_data_dir_config(root)
        )
    )
    findings = [
        {
            "file": _relative_report_path(root, report.file_path),
            "severity": diagnostic.severity,
            "path": diagnostic.path,
            "code": diagnostic.code,
            "message": diagnostic.message,
            "recommendation": _recommendation(diagnostic),
        }
        for report in reports
        for diagnostic in report.diagnostics
    ]
    findings.sort(key=lambda item: (item["severity"] != "error", item["file"], item["path"]))
    errors = sum(report.error_count for report in reports)
    warnings = sum(report.warning_count for report in reports)
    status = "failed" if errors else "warning" if warnings else "ok"
    payload: dict[str, Any] = {
        "check": check,
        "status": status,
        "ok": not errors,
        "data_dir": str(root),
        "read_only": True,
        "scope": _SCOPE,
        "files_checked": sum(report.exists and report.file_path != root for report in reports),
        "valid_files": sum(report.exists and not report.diagnostics for report in reports),
        "missing_files": sum(not report.exists for report in reports),
        "settings_missing": any(
            report.file_path == root / "settings.json" and not report.exists and report.ok
            for report in reports
        ),
        "errors": errors,
        "warnings": warnings,
        "total_findings": len(findings),
        "offset": offset,
    }
    selected = findings[offset : offset + limit]
    if not json_output:
        # Leave room for header, continuation and summary. JSON retains full text.
        remaining = _TEXT_BUDGET - 2000
        selected = []
        for finding in findings[offset : offset + limit]:
            size = len(_format_finding(finding)) + 1
            if selected and size > remaining:
                break
            selected.append(finding)
            remaining -= size
    payload["findings"] = selected
    next_offset = offset + len(selected)
    payload["next_offset"] = next_offset if next_offset < len(findings) else None
    command: tuple[str, ...] = ("vbot", "doctor", check, "--data-dir", str(root))
    if payload["next_offset"] is not None:
        command += ("--offset", str(next_offset), "--limit", str(limit))
        if json_output:
            command += ("--json",)
        payload["next_command"] = format_command(command)
    else:
        payload["next_command"] = None
    message = json.dumps(payload, ensure_ascii=True) if json_output else _format_report(payload)
    return CommandResult(
        ok=not errors,
        message=message,
        instance=_doctor_instance(root),
        attention=(f"{warnings} configuration warning(s)",) if warnings else (),
    )


def _check_data_dir(root: Path) -> JsonValidationReport | None:
    try:
        status = stat_or_none(root)
        if status is None:
            diagnostic = JsonDiagnostic(
                "error", "$", "Data directory does not exist", "missing_data_dir"
            )
            return JsonValidationReport(root, False, (diagnostic,))
        if not stat.S_ISDIR(status.st_mode):
            diagnostic = JsonDiagnostic(
                "error", "$", "Data path is not a directory", "wrong_file_type"
            )
            return JsonValidationReport(root, True, (diagnostic,))
        next(root.iterdir(), None)
    except OSError:
        diagnostic = JsonDiagnostic(
            "error", "$", "Cannot inspect data directory", "discovery_failed"
        )
        return JsonValidationReport(root, True, (diagnostic,))
    return None


def _recommendation(diagnostic: JsonDiagnostic) -> str:
    if diagnostic.code in _RECOMMENDATIONS:
        return _RECOMMENDATIONS[diagnostic.code]
    if diagnostic.path.endswith(".format_version"):
        return (
            "Use a vBot version compatible with this document or its documented converter; "
            "do not change format_version by hand."
        )
    if diagnostic.severity == "error":
        return "Correct the reported field or follow the recovery instructions in this finding."
    return "Review the reported field and follow the guidance in this finding."


def _format_report(payload: dict[str, Any]) -> str:
    check = payload["check"]
    lines = [
        f"doctor {check}: {payload['status']}",
        f"data_dir: {inline_text(payload['data_dir'])}",
    ]
    if check == "settings":
        lines.append(f"file: {inline_text(str(Path(payload['data_dir']) / 'settings.json'))}")
    lines.extend(
        [
            f"scope: {_SCOPE}",
            "Note: Read-only check; no files were changed.",
            *(
                f"{key}: {payload[key]}"
                for key in ("files_checked", "valid_files", "errors", "warnings")
            ),
        ]
    )
    if payload["settings_missing"]:
        lines.append("settings.json: missing (defaults will be used)")
    total = payload["total_findings"]
    selected = payload["findings"]
    if total:
        first = payload["offset"] + 1 if selected else 0
        last = payload["offset"] + len(selected) if selected else 0
        lines.append(f"findings: {first}-{last} of {total} (errors first)")
        lines.extend(_format_finding(finding) for finding in selected)
    if payload["next_command"]:
        lines.append(f"Next: {inline_text(payload['next_command'])}")
    state: Status = (
        "error" if payload["errors"] else "warning" if payload["warnings"] else "success"
    )
    lines.append(
        status_line(
            state,
            f"Configuration check: {payload['errors']} error(s), {payload['warnings']} warning(s).",
        )
    )
    return "\n".join(lines)


def _format_finding(finding: dict[str, str]) -> str:
    def preview(value: str) -> str:
        text = inline_text(value)
        return (
            text if len(text) <= 1000 else text[:1000] + "... [preview; use --json for full text]"
        )

    return (
        f"{preview(finding['file'])}:\n"
        f"- {finding['severity']} {preview(finding['path'])}: {preview(finding['message'])}\n"
        f"  Recommendation: {preview(finding['recommendation'])}"
    )


def _resolve_data_dir_without_loading_settings(data_dir: str | Path | None) -> Path:
    sentinel = Path.cwd() / f".vbot-doctor-ignore-settings-{uuid4().hex}.json"
    config = Config(
        data_dir=Path(data_dir) if data_dir is not None else None, settings_path=sentinel
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


def _relative_report_path(data_dir: Path, file_path: Path) -> str:
    try:
        return file_path.relative_to(data_dir).as_posix()
    except ValueError:
        return str(file_path)
