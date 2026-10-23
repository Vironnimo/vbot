"""Offline Extension document validation without importing registration entry points."""

from __future__ import annotations

import importlib
import json
import sys
import types
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from core.config_validation import JsonDiagnostic, JsonValidationReport, discover_json_documents
from core.extensions._loading import _discover_extension_paths
from core.utils.config import VBOT_ROOT
from core.utils.file_status import exists_strict, is_dir_strict


def validate_extension_documents(
    data_dir: Path, directories: list[Path]
) -> tuple[JsonValidationReport, ...]:
    """Run manifest-declared validators for installed owners, even when disabled.

    ``config_documents`` maps owner-state-relative glob patterns to
    ``module:function`` validators. Validation modules load under a temporary
    package with the Extension's path; its ``__init__`` and ``register`` never run.
    Shadowing follows runtime root order. Validators are trusted Extension code,
    expected to read only, and receive the absolute document path.
    """
    reports: list[JsonValidationReport] = []
    seen: set[str] = set()
    roots = [data_dir / "extensions", *directories, VBOT_ROOT / "resources" / "extensions"]
    for root in roots:
        try:
            if not is_dir_strict(root):
                if root in directories or exists_strict(root):
                    reports.append(
                        _failure(
                            root, "Configured Extension directory is missing or is not a directory"
                        )
                    )
                continue
            # Runtime discovery logs unavailable roots and returns None. Doctor
            # must also put that failure in its report and exit status.
            list(root.iterdir())
        except OSError:
            reports.append(_failure(root, "Cannot inspect Extension directory"))
            continue
        candidates = _discover_extension_paths(root)
        if candidates is None:
            reports.append(_failure(root, "Cannot inspect Extension directory"))
            continue
        for candidate in candidates:
            if candidate.name in seen:
                continue
            seen.add(candidate.name)
            if candidate.error:
                reports.append(
                    _failure(candidate.entry_path, "Cannot inspect Extension entry point")
                )
                continue
            manifest = candidate.root_path / "extension.json"
            if candidate.root_path == candidate.entry_path:
                continue
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except OSError, ValueError, RecursionError:
                reports.append(
                    _failure(manifest, "Cannot read Extension manifest as a JSON object")
                )
                continue
            declarations = raw.get("config_documents", {}) if isinstance(raw, dict) else None
            try:
                validators = _declarations(declarations)
            except ValueError as error:
                reports.append(_failure(manifest, str(error)))
                continue
            state = data_dir / "extension-data" / candidate.name
            for pattern, entry in validators.items():
                paths, failures = discover_json_documents(state, pattern)
                reports.extend(failures)
                if not paths:
                    continue
                namespace = "_vbot_config_" + uuid.uuid4().hex
                package = types.ModuleType(namespace)
                package.__path__ = [str(candidate.root_path)]
                sys.modules[namespace] = package
                try:
                    module_name, function = entry.split(":")
                    validator = getattr(
                        importlib.import_module(f"{namespace}.{module_name}"), function
                    )
                    for path in paths:
                        try:
                            report = validator(path)
                            if (
                                not isinstance(report, JsonValidationReport)
                                or report.file_path != path
                            ):
                                raise ValueError(
                                    "Validator must return a JsonValidationReport for its document"
                                )
                            reports.append(report)
                        except Exception as error:
                            reports.append(
                                _failure(
                                    path, f"Extension validator failed ({type(error).__name__})"
                                )
                            )
                except Exception as error:
                    reports.extend(
                        _failure(path, f"Cannot load Extension validator ({type(error).__name__})")
                        for path in paths
                    )
                finally:
                    for name in tuple(sys.modules):
                        if name == namespace or name.startswith(namespace + "."):
                            sys.modules.pop(name, None)
    return tuple(reports)


def _declarations(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(
            "config_documents must map relative file patterns to module:function validators"
        )
    for pattern, entry in value.items():
        if not isinstance(pattern, str) or not pattern or "\\" in pattern or ":" in pattern:
            raise ValueError("config_documents needs owner-state-relative file patterns")
        path = PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "config_documents patterns must stay inside the Extension state directory"
            )
        if not isinstance(entry, str) or entry.count(":") != 1:
            raise ValueError("config_documents validators use module:function")
        module, function = entry.split(":")
        if (
            not module
            or not all(part.isidentifier() for part in module.split("."))
            or not function.isidentifier()
        ):
            raise ValueError("config_documents validators use module:function")
        if module.split(".")[0].casefold() == "__init__":
            raise ValueError("config_documents validators cannot load the Extension's __init__")
    return value


def _failure(path: Path, message: str) -> JsonValidationReport:
    return JsonValidationReport(
        path, True, (JsonDiagnostic("error", "$", message, code="extension_validation_failed"),)
    )
