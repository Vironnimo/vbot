"""Offline discovery and immutable capture of legacy JSONL Session artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.chat.messages import ChatMessage
from core.sessions import SessionAddress
from core.settings import is_valid_agent_id, is_valid_project_id

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class CapturedArtifact:
    """One artifact read once into immutable bytes for the complete conversion."""

    path: Path
    relative_path: str
    kind: str
    present: bool
    data: bytes
    sha256: str
    size: int
    mtime_ns: int | None


@dataclass(frozen=True)
class IgnoredTail:
    """Evidence for a final unterminated record ignored by the legacy loader."""

    artifact: str
    offset: int
    size: int
    sha256: str


@dataclass(frozen=True)
class LegacyRoot:
    """Table-driven mapping for one supported physical legacy root."""

    name: str
    parts: tuple[str, ...]
    project_index: int | None
    agent_index: int
    archived: bool


LEGACY_ROOTS = (
    LegacyRoot("live_identity", ("agents", "*", "sessions", "*.jsonl"), None, 1, False),
    LegacyRoot(
        "live_project", ("projects", "*", "agents", "*", "sessions", "*.jsonl"), 1, 3, False
    ),
    LegacyRoot(
        "archive_sessions_identity",
        ("archive", "sessions", "agents", "*", "*.jsonl"),
        None,
        3,
        True,
    ),
    LegacyRoot(
        "archive_sessions_project",
        ("archive", "sessions", "projects", "*", "agents", "*", "*.jsonl"),
        3,
        5,
        True,
    ),
    LegacyRoot(
        "archive_identity",
        ("archive", "agents", "*", "agent", "sessions", "*.jsonl"),
        None,
        2,
        True,
    ),
    LegacyRoot(
        "archive_project",
        ("archive", "projects", "*", "agents", "*", "sessions", "*.jsonl"),
        2,
        4,
        True,
    ),
)


@dataclass(frozen=True)
class LegacySession:
    """Validated source payload for exactly one old-format Session."""

    address: SessionAddress
    transcript: Path
    messages: tuple[ChatMessage, ...]
    metadata: JsonObject
    activity: JsonObject
    continuation: tuple[JsonObject, ...]
    archived: bool
    root_kind: str
    generation_id: str
    source_digest: str
    captured_artifacts: tuple[CapturedArtifact, ...]
    ignored_tails: tuple[IgnoredTail, ...]


@dataclass(frozen=True)
class CaptureInventory:
    """Immutable source capture plus non-fatal discovery evidence."""

    sessions: tuple[LegacySession, ...]
    orphan_sidecars: tuple[str, ...]
    unknown_files: tuple[str, ...]
    rejected_paths: tuple[str, ...]
    skipped_sessions: tuple[JsonObject, ...]


def inventory(data_dir: Path) -> list[LegacySession]:
    """Return valid legacy Sessions from all six fixed roots."""
    return list(capture_inventory(data_dir).sessions)


def capture_inventory(data_dir: Path) -> CaptureInventory:
    """Capture each recognized source and collect evidence outside the schema."""
    root = Path(data_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"legacy Session source is not a directory: {root}")
    candidates: list[tuple[LegacyRoot, Path]] = []
    rejected: set[str] = set()
    unknown: set[str] = set()
    for specification in LEGACY_ROOTS:
        for path in _match_root(root, specification, rejected, unknown):
            candidates.append((specification, path))

    sessions: list[LegacySession] = []
    skipped_sessions: list[JsonObject] = []
    seen_paths: set[Path] = set()
    seen_live: set[SessionAddress] = set()
    for specification, transcript in sorted(
        candidates, key=lambda item: (item[0].archived, item[1].relative_to(root).as_posix())
    ):
        resolved = _safe_resolved_path(root, transcript)
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        relative_parts = transcript.relative_to(root).parts
        project_id = (
            relative_parts[specification.project_index]
            if specification.project_index is not None
            else None
        )
        agent_id = relative_parts[specification.agent_index]
        address = SessionAddress(project_id, agent_id, transcript.stem)
        try:
            _validate_address(address, transcript)
        except ValueError as exc:
            skipped_sessions.append(
                {
                    "relative_path": transcript.relative_to(root).as_posix(),
                    "reason": str(exc),
                }
            )
            continue
        if not specification.archived:
            if address in seen_live:
                skipped_sessions.append(
                    {
                        "relative_path": transcript.relative_to(root).as_posix(),
                        "reason": f"duplicate live legacy Session source: {address}",
                    }
                )
                continue
            seen_live.add(address)
        try:
            sessions.append(_capture_session(root, specification, transcript, address))
        except ValueError as exc:
            skipped_sessions.append(
                {
                    "relative_path": transcript.relative_to(root).as_posix(),
                    "reason": str(exc),
                }
            )

    known_artifacts = {
        artifact.relative_path
        for session in sessions
        for artifact in session.captured_artifacts
        if artifact.present
    }
    unknown.difference_update(known_artifacts)
    transcript_paths = {session.transcript for session in sessions}
    orphan_sidecars: set[str] = set()
    for path_text in sorted(unknown):
        path = root / Path(path_text)
        if _is_sidecar_name(path.name):
            transcript_name = path.name.split(".", 1)[0] + ".jsonl"
            if path.with_name(transcript_name) not in transcript_paths:
                orphan_sidecars.add(path_text)

    return CaptureInventory(
        sessions=tuple(sessions),
        orphan_sidecars=tuple(sorted(orphan_sidecars)),
        unknown_files=tuple(sorted(unknown - orphan_sidecars)),
        rejected_paths=tuple(sorted(rejected)),
        skipped_sessions=tuple(skipped_sessions),
    )


def _match_root(
    root: Path,
    specification: LegacyRoot,
    rejected: set[str],
    unknown: set[str],
) -> list[Path]:
    parents = [root]
    for component in specification.parts[:-1]:
        next_parents: list[Path] = []
        for parent in parents:
            if component == "*":
                next_parents.extend(
                    entry for entry in _directory_entries(parent, root, rejected) if entry.is_dir()
                )
                continue
            candidate = parent / component
            if _is_rejected_path(root, candidate, rejected):
                continue
            if candidate.is_dir():
                next_parents.append(candidate)
        parents = next_parents

    matches: list[Path] = []
    for parent in parents:
        for entry in _directory_entries(parent, root, rejected):
            if entry.is_dir():
                continue
            if entry.name.endswith(".jsonl") and not entry.name.endswith(".continuation.jsonl"):
                matches.append(entry)
            elif (
                entry.name.endswith(".jsonl")
                or _is_sidecar_name(entry.name)
                or entry.name.endswith((".json", ".db"))
            ):
                unknown.add(entry.relative_to(root).as_posix())
    return matches


def _directory_entries(path: Path, root: Path, rejected: set[str]) -> list[Path]:
    try:
        entries = list(os.scandir(path))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    result: list[Path] = []
    for entry in sorted(entries, key=lambda item: item.name):
        candidate = Path(entry.path)
        if entry.is_symlink():
            rejected.add(candidate.relative_to(root).as_posix())
            continue
        try:
            is_directory = entry.is_dir(follow_symlinks=False)
            is_regular_file = entry.is_file(follow_symlinks=False)
        except OSError:
            rejected.add(candidate.relative_to(root).as_posix())
            continue
        if not (is_directory or is_regular_file):
            rejected.add(candidate.relative_to(root).as_posix())
            continue
        result.append(candidate)
    return result


def _is_rejected_path(root: Path, path: Path, rejected: set[str]) -> bool:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink() or not (path.is_dir() or path.is_file()):
        rejected.add(path.relative_to(root).as_posix())
        return True
    if not path.resolve().is_relative_to(root):
        rejected.add(path.relative_to(root).as_posix())
        return True
    return stat.st_mode == 0


def _safe_resolved_path(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or any(
        part.is_symlink() for part in _path_components(root, path)
    ):
        raise ValueError(f"legacy Session path is not a safe regular path: {path}")
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"legacy Session transcript is not a regular file: {path}")
    return resolved


def _path_components(root: Path, path: Path) -> list[Path]:
    relative = path.relative_to(root)
    current = root
    components: list[Path] = []
    for part in relative.parts:
        current /= part
        components.append(current)
    return components


def _capture_session(
    root: Path, specification: LegacyRoot, transcript: Path, address: SessionAddress
) -> LegacySession:
    captures = [
        _capture_artifact(root, path, kind)
        for kind, path in zip(
            ("transcript", "metadata", "activity", "continuation"),
            _artifacts(transcript),
            strict=True,
        )
    ]
    messages, transcript_tail = _parse_messages(captures[0])
    metadata = _parse_object(captures[1])
    activity = _parse_object(captures[2])
    continuation, continuation_tail = _parse_continuation(captures[3])
    relative = transcript.relative_to(root).as_posix()
    source_digest = _source_digest(captures)
    generation_id = _generation_id(specification.name, relative, source_digest, 0)
    return LegacySession(
        address=address,
        transcript=transcript,
        messages=tuple(messages),
        metadata=metadata,
        activity=activity,
        continuation=tuple(continuation),
        archived=specification.archived,
        root_kind=specification.name,
        generation_id=generation_id,
        source_digest=source_digest,
        captured_artifacts=tuple(replace(artifact, data=b"") for artifact in captures),
        ignored_tails=(*transcript_tail, *continuation_tail),
    )


def _capture_artifact(root: Path, path: Path, kind: str) -> CapturedArtifact:
    relative = path.relative_to(root).as_posix()
    try:
        before = path.lstat()
    except FileNotFoundError:
        return CapturedArtifact(
            path, relative, kind, False, b"", hashlib.sha256(b"").hexdigest(), 0, None
        )
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"legacy Session artifact is not a regular file: {path}")
    try:
        with path.open("rb") as handle:
            data = handle.read()
            after = os.fstat(handle.fileno())
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"legacy Session artifact cannot be read: {path}") from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ValueError(f"legacy Session artifact changed during capture: {path}")
    return CapturedArtifact(
        path=path,
        relative_path=relative,
        kind=kind,
        present=True,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        mtime_ns=after.st_mtime_ns,
    )


def _parse_messages(
    artifact: CapturedArtifact,
) -> tuple[list[ChatMessage], tuple[IgnoredTail, ...]]:
    values, tails = _parse_json_lines(artifact, "message")
    messages: list[ChatMessage] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"legacy Session message must be an object: {artifact.path}")
        try:
            messages.append(ChatMessage.from_dict(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid legacy Session message: {artifact.path}") from exc
    return messages, tails


def _parse_continuation(
    artifact: CapturedArtifact,
) -> tuple[list[JsonObject], tuple[IgnoredTail, ...]]:
    values, tails = _parse_json_lines(artifact, "continuation")
    records: list[JsonObject] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"legacy continuation record must be an object: {artifact.path}")
        records.append(value)
    return records, tails


def _parse_json_lines(
    artifact: CapturedArtifact, label: str
) -> tuple[list[object], tuple[IgnoredTail, ...]]:
    if not artifact.present:
        return [], ()
    values: list[object] = []
    tails: list[IgnoredTail] = []
    offset = 0
    lines = artifact.data.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        end = offset + len(raw_line)
        line = raw_line.rstrip(b"\r\n")
        offset = end
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            is_final_unterminated = index == len(lines) - 1 and not raw_line.endswith(
                (b"\n", b"\r")
            )
            if is_final_unterminated:
                tails.append(
                    IgnoredTail(
                        artifact=artifact.relative_path,
                        offset=end - len(raw_line),
                        size=len(raw_line),
                        sha256=hashlib.sha256(raw_line).hexdigest(),
                    )
                )
                break
            raise ValueError(f"invalid legacy {label} record: {artifact.path}:{index + 1}") from exc
        values.append(value)
    return values, tuple(tails)


def _parse_object(artifact: CapturedArtifact) -> JsonObject:
    if not artifact.present:
        return {}
    try:
        value = json.loads(artifact.data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid legacy sidecar: {artifact.path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"legacy sidecar must be an object: {artifact.path}")
    return value


def _source_digest(captures: tuple[CapturedArtifact, ...] | list[CapturedArtifact]) -> str:
    digest = hashlib.sha256()
    for artifact in captures:
        digest.update(artifact.kind.encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifact.relative_path.encode("utf-8"))
        digest.update(b"\0present\0" if artifact.present else b"\0missing\0")
        digest.update(artifact.sha256.encode("ascii"))
    return digest.hexdigest()


def _generation_id(root_kind: str, relative_path: str, source_digest: str, ordinal: int) -> str:
    value = f"vbot-session-generation-v1\0{root_kind}\0{relative_path}\0{source_digest}\0{ordinal}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _validate_address(address: SessionAddress, path: Path) -> None:
    if not is_valid_agent_id(address.agent_id):
        raise ValueError(f"invalid legacy Agent id in {path}")
    if address.project_id is not None and not is_valid_project_id(address.project_id):
        raise ValueError(f"invalid legacy Project id in {path}")
    identifier = address.session_id
    if (
        not identifier
        or len(identifier) > 128
        or not identifier[0].isascii()
        or not identifier[0].isalnum()
        or any(
            not character.isascii() or not (character.isalnum() or character in "-_")
            for character in identifier
        )
    ):
        raise ValueError(f"invalid legacy Session id in {path}")


def _artifacts(transcript: Path) -> tuple[Path, Path, Path, Path]:
    stem = transcript.stem
    return (
        transcript,
        transcript.with_name(f"{stem}.meta.json"),
        transcript.with_name(f"{stem}.activity.json"),
        transcript.with_name(f"{stem}.continuation.jsonl"),
    )


def _is_sidecar_name(name: str) -> bool:
    return name.endswith((".meta.json", ".activity.json", ".continuation.jsonl"))
