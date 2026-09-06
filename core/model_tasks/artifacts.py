"""Shared blob + JSON-sidecar artifact persistence for task execution services.

Speech and image execution use the sidecar-backed :class:`TaskArtifactStore`.
Video and Music use :func:`write_generated_media_artifact` for caller-owned
exclusive files without a central sidecar. Both paths use compact typed ids
and preserve each task's own error type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.utils.errors import TaskError
from core.utils.ids import is_safe_id, new_id, write_id_file

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class StoredArtifact:
    """One persisted artifact: blob location plus sidecar metadata."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    file_path: Path
    metadata: JsonObject = field(default_factory=dict)


class TaskArtifactStore:
    """Blob + sidecar artifact storage for one task's artifact directory.

        *kind* names the task in error messages (``"speech"`` / ``"image"``);
        *error* is the task's configuration-error class used for every expected
    failure so callers keep their domain error contract.
    """

    def __init__(self, artifact_dir: str | Path, *, kind: str, error: type[TaskError]) -> None:
        self._artifact_dir = Path(artifact_dir)
        self._kind = kind
        self._error = error

    def write(
        self,
        payload: bytes,
        *,
        extension: str,
        media_type: str,
        extra_metadata: JsonObject | None = None,
    ) -> StoredArtifact:
        """Persist one blob and its sidecar; returns the stored artifact.

        Reserves the sidecar name, then writes the blob and complete metadata.
        Interrupted writes can leave invalid metadata or an orphaned blob;
        those names stay occupied and reads fail closed.
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
        metadata: JsonObject = {
            "id": artifact_id,
            "filename": filename,
            "media_type": media_type,
            "size_bytes": len(payload),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        return StoredArtifact(
            id=artifact_id,
            filename=filename,
            media_type=media_type,
            size_bytes=len(payload),
            file_path=file_path,
            metadata=metadata,
        )

    def read(self, artifact_id: str) -> StoredArtifact:
        """Load one artifact by id; raises the task's error for every failure."""
        label = self._kind.capitalize()
        if not is_safe_id(artifact_id):
            raise self._error(f"Invalid {self._kind} artifact id")
        metadata_path = self._artifact_dir / f"{artifact_id}.json"
        if not metadata_path.is_file():
            raise self._error(f"{label} artifact not found")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise self._error(f"{label} artifact metadata is unreadable") from exc

        filename = metadata.get("filename")
        media_type = metadata.get("media_type")
        size_bytes = metadata.get("size_bytes")
        if not isinstance(filename, str) or not isinstance(media_type, str):
            raise self._error(f"{label} artifact metadata is invalid")
        file_path = self._artifact_dir / filename
        if not file_path.is_file():
            raise self._error(f"{label} artifact file not found")
        return StoredArtifact(
            id=artifact_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes if isinstance(size_bytes, int) else file_path.stat().st_size,
            file_path=file_path,
            metadata=metadata if isinstance(metadata, dict) else {},
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
