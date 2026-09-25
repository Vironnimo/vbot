"""Shared blob + JSON-sidecar artifact persistence for task execution services.

Speech execution uses the sidecar-backed :class:`TaskArtifactStore`; image
execution owns its separate caller-directory writer. Video and Music use
:func:`write_generated_media_artifact` for caller-owned
exclusive files without a central sidecar. Both paths use compact typed ids
and preserve each task's own error type.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    validate_non_empty_string,
    warn_unknown_keys,
)
from core.json_documents import (
    json_document,
    render_json_document,
    strip_unknown_fields,
    validate_format_version,
)
from core.utils.atomic import atomic_write_text
from core.utils.errors import TaskError
from core.utils.ids import is_safe_id, new_id, write_id_file

TASK_ARTIFACT_FORMAT_VERSION = 1
TASK_ARTIFACT_SHAPE = json_document({"id", "filename", "media_type", "size_bytes"})


def validate_task_artifact_metadata_data(data: Any) -> list[JsonDiagnostic]:
    """Validate one decoded artifact sidecar (``artifacts/speech/<id>.json``)."""

    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, TASK_ARTIFACT_FORMAT_VERSION):
        return diagnostics
    warn_unknown_keys(diagnostics, "$", data, TASK_ARTIFACT_SHAPE.fields, "artifact metadata field")
    for name in ("id", "filename", "media_type"):
        validate_non_empty_string(diagnostics, f"$.{name}", data.get(name), required=True)
    size_bytes = data.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        add_error(diagnostics, "$.size_bytes", "must be a non-negative integer")
    return diagnostics


def validate_task_artifact_metadata_file(path: str | Path) -> JsonValidationReport:
    """Validate one artifact sidecar without reading its blob."""

    return validate_json_file(path, validate_task_artifact_metadata_data, missing_ok=False)


@dataclass(frozen=True)
class StoredArtifact:
    """One persisted artifact: blob location plus sidecar metadata."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    file_path: Path


class TaskArtifactStore:
    """Blob + sidecar artifact storage for one task's artifact directory.

    *kind* names the task in error messages (currently ``"speech"``);
    *error* is the task's configuration-error class used for every expected
    failure so callers keep their domain error contract.
    """

    def __init__(self, artifact_dir: str | Path, *, kind: str, error: type[TaskError]) -> None:
        self._artifact_dir = Path(artifact_dir)
        self._kind = kind
        self._error = error

    def write(self, payload: bytes, *, extension: str, media_type: str) -> StoredArtifact:
        """Persist one blob and its sidecar; returns the stored artifact.

        Reserves the sidecar name, then writes the blob and complete metadata.
        Interrupted writes can leave invalid metadata or an orphaned blob;
        those names stay occupied and reads fail closed. A sidecar is written
        once and never rewritten.
        """
        self._artifact_dir.mkdir(parents=True, exist_ok=True)

        def claim(candidate: str) -> bool:
            metadata_path = self._artifact_dir / f"{candidate}.json"
            try:
                with metadata_path.open("x", encoding="utf-8"):
                    pass
            except FileExistsError:
                return False
            if any(path != metadata_path for path in self._artifact_dir.glob(f"{candidate}.*")):
                metadata_path.unlink()
                return False
            return True

        artifact_id = new_id("aud" if self._kind == "speech" else "img", claim=claim)
        filename = f"{artifact_id}.{extension}"
        file_path = self._artifact_dir / filename
        metadata_path = self._artifact_dir / f"{artifact_id}.json"
        file_path.write_bytes(payload)
        metadata = {
            "id": artifact_id,
            "filename": filename,
            "media_type": media_type,
            "size_bytes": len(payload),
        }
        atomic_write_text(
            metadata_path,
            render_json_document(metadata, version=TASK_ARTIFACT_FORMAT_VERSION, sort_keys=True),
        )
        return StoredArtifact(
            id=artifact_id,
            filename=filename,
            media_type=media_type,
            size_bytes=len(payload),
            file_path=file_path,
        )

    def read(self, artifact_id: str) -> StoredArtifact:
        """Load one artifact by id; raises the task's error for every failure."""
        label = self._kind.capitalize()
        if not is_safe_id(artifact_id):
            raise self._error(f"Invalid {self._kind} artifact id")
        metadata_path = self._artifact_dir / f"{artifact_id}.json"
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise self._error(f"{label} artifact not found")
        try:
            data = load_validated_json_file(
                metadata_path, validate_task_artifact_metadata_data, missing_ok=False
            )
        except (OSError, JsonConfigValidationError) as exc:
            # The cause names the file and its diagnostics; the message stays generic.
            raise self._error(f"{label} artifact metadata is unreadable") from exc

        metadata = strip_unknown_fields(data, TASK_ARTIFACT_SHAPE)
        filename = metadata["filename"]
        prefix = f"{artifact_id}."
        if (
            metadata["id"] != artifact_id
            or not filename.startswith(prefix)
            or not is_safe_id(filename[len(prefix) :])
            or filename == metadata_path.name
        ):
            raise self._error(f"{label} artifact metadata is invalid")
        file_path = self._artifact_dir / filename
        try:
            if (
                file_path.is_symlink()
                or file_path.resolve().parent != self._artifact_dir.resolve()
                or not file_path.is_file()
            ):
                raise self._error(f"{label} artifact file not found")
        except OSError as exc:
            raise self._error(f"{label} artifact file is unreadable") from exc
        return StoredArtifact(
            id=artifact_id,
            filename=filename,
            media_type=metadata["media_type"],
            size_bytes=metadata["size_bytes"],
            file_path=file_path,
        )


@dataclass(frozen=True)
class GeneratedMediaArtifact:
    """Generated media persisted in a caller-owned working directory."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    file_path: Path


def write_generated_media_artifact(
    payload: bytes,
    *,
    output_dir: str | Path,
    extension: str,
    media_type: str,
    error: type[TaskError],
) -> GeneratedMediaArtifact:
    """Write generated media exclusively, never overwriting an existing file."""

    destination = Path(output_dir)
    try:
        prefix = "vid" if media_type.startswith("video/") else "mus"
        file_path = write_id_file(destination, prefix, f".{extension}", payload)
        artifact_id = file_path.stem
        filename = file_path.name
        return GeneratedMediaArtifact(
            id=artifact_id,
            filename=filename,
            media_type=media_type,
            size_bytes=len(payload),
            file_path=destination / filename,
        )
    except OSError as exc:
        raise error(str(exc)) from exc
