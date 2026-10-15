"""Offline Extension document validation without importing registration entry points."""

from __future__ import annotations

import importlib
import json
import sys
import types
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from core.config_validation import JsonDiagnostic, JsonValidationReport
from core.extensions._loading import _discover_extension_paths
from core.utils.config import VBOT_ROOT
from core.utils.file_status import is_file_strict


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
        for candidate in _discover_extension_paths(root) or ():
            if candidate.name in seen:
                continue
            seen.add(candidate.name)
            manifest = candidate.root_path / "extension.json"
            if candidate.root_path == candidate.entry_path:
                continue
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                reports.append(_failure(manifest, str(error)))
                continue
            declarations = raw.get("config_documents", {}) if isinstance(raw, dict) else None
            try:
                validators = _declarations(declarations)
            except ValueError as error:
                reports.append(_failure(manifest, str(error)))
                continue
            state = data_dir / "extension-data" / candidate.name
            for pattern, entry in validators.items():
                try:
                    paths = [path for path in sorted(state.glob(pattern)) if is_file_strict(path)]
                except OSError as error:
                    reports.append(_failure(state, str(error)))
                    continue
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
                            reports.append(_failure(path, f"Extension validator failed: {error}"))
                except Exception as error:
                    reports.extend(
                        _failure(path, f"Cannot load Extension validator: {error}")
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
    return value


def _failure(path: Path, message: str) -> JsonValidationReport:
    return JsonValidationReport(path, True, (JsonDiagnostic("error", "$", message),))
