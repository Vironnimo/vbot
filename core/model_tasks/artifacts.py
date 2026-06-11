"""Shared blob + JSON-sidecar artifact persistence for task execution services.

Speech and image execution persist results the same way: one binary blob and
one JSON metadata sidecar per artifact under a task-specific directory, with
``uuid4().hex`` ids. :class:`TaskArtifactStore` owns that layout — id
generation and validation, the write order (blob before sidecar), and the
fail-closed read path — while each task keeps its own artifact dataclass and
error type.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.utils.errors import TaskError

JsonObject = dict[str, Any]
_ARTIFACT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


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

        Writes the blob before the sidecar and has no rollback wrapper —
        interrupted writes can leave an orphaned blob (fail-closed on read).
        """
        artifact_id = uuid4().hex
        filename = f"{artifact_id}.{extension}"
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
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
        if not isinstance(artifact_id, str) or _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
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
