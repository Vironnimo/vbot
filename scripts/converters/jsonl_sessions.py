"""Converter-only discovery and validation of legacy JSONL Session artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chat.messages import ChatMessage
from core.sessions import SessionAddress
from core.settings import is_valid_agent_id, is_valid_project_id

JsonObject = dict[str, Any]


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

    @property
    def source_digest(self) -> str:
        digest = hashlib.sha256()
        for path in _artifacts(self.transcript):
            digest.update(path.name.encode("utf-8"))
            if path.exists():
                digest.update(b"\0present\0")
                digest.update(path.read_bytes())
            else:
                digest.update(b"\0missing\0")
        return digest.hexdigest()

    @property
    def digest(self) -> str:
        return semantic_digest(
            self.address,
            self.messages,
            self.metadata,
            self.activity,
            self.continuation,
            self.archived,
        )


def inventory(data_dir: Path) -> list[LegacySession]:
    """Load all valid live and archived JSONL Sessions, rejecting ambiguity."""
    sources: list[tuple[Path, str | None, str, bool]] = []
    for transcript in (data_dir / "agents").glob("*/sessions/*.jsonl"):
        sources.append((transcript, None, transcript.parent.parent.name, False))
    for transcript in (data_dir / "projects").glob("*/agents/*/sessions/*.jsonl"):
        sources.append(
            (transcript, transcript.parents[3].name, transcript.parent.parent.name, False)
        )
    archive_root = data_dir / "archive" / "sessions"
    for transcript in (archive_root / "agents").glob("*/*.jsonl"):
        sources.append((transcript, None, transcript.parent.name, True))
    for transcript in (archive_root / "projects").glob("*/agents/*/*.jsonl"):
        sources.append(
            (transcript, transcript.parents[3].name, transcript.parent.parent.name, True)
        )
    # Additional two roots: archived agent and archived project trees.
    for transcript in (data_dir / "archive" / "agents").glob("*/agent/sessions/*.jsonl"):
        sources.append((transcript, None, transcript.parents[3].name, True))
    for transcript in (data_dir / "archive" / "projects").glob("*/agents/*/sessions/*.jsonl"):
        sources.append(
            (transcript, transcript.parent.parent.parent.name, transcript.parent.parent.name, True)
        )

    sessions: list[LegacySession] = []
    # Live addresses must be unique; archived may share an address across
    # multiple generations (plan 5). Deduplicate by file path, not address.
    seen_paths: set[Path] = set()
    seen_live: set[SessionAddress] = set()
    for transcript, project_id, agent_id, archived in sorted(
        sources, key=lambda item: (not item[3], str(item[0]))
    ):
        if transcript.name.endswith(".continuation.jsonl"):
            continue
        # Resolve the path to avoid symlink duplicates.
        try:
            resolved = transcript.resolve()
        except OSError:
            resolved = transcript
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        address = SessionAddress(project_id, agent_id, transcript.stem)
        _validate_address(address, transcript)
        if not archived:
            if address in seen_live:
                raise ValueError(f"duplicate live legacy Session source: {address}")
            seen_live.add(address)
        sessions.append(
            LegacySession(
                address=address,
                transcript=transcript,
                messages=tuple(_messages(transcript)),
                metadata=_object_sidecar(transcript.with_name(f"{transcript.stem}.meta.json")),
                activity=_object_sidecar(transcript.with_name(f"{transcript.stem}.activity.json")),
                continuation=tuple(
                    _continuation(transcript.with_name(f"{transcript.stem}.continuation.jsonl"))
                ),
                archived=archived,
            )
        )
    return sessions


def semantic_digest(
    address: SessionAddress,
    messages: tuple[ChatMessage, ...],
    metadata: JsonObject,
    activity: JsonObject,
    continuation: tuple[JsonObject, ...],
    archived: bool,
) -> str:
    payload = {
        "address": [address.project_id, address.agent_id, address.session_id],
        "messages": [message.to_dict() for message in messages],
        "metadata": metadata,
        "activity": activity,
        "continuation": continuation,
        "archived": archived,
    }
    digest = hashlib.sha256()
    digest.update(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    return digest.hexdigest()


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


def _messages(path: Path) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    data = path.read_bytes()
    ends_with_newline = data.endswith(b"\n") or data.endswith(b"\r")
    # Split without discarding empty tail handling via bytes.
    lines = data.splitlines()
    for line_number, line_bytes in enumerate(lines, start=1):
        if not line_bytes.strip():
            continue
        try:
            line = line_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            if line_number == len(lines) and not ends_with_newline:
                # Torn multibyte tail — ignore suffix as legacy loader did.
                break
            raise ValueError(f"invalid legacy Session message at {path}:{line_number}") from exc
        try:
            messages.append(ChatMessage.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            if line_number == len(lines) and not ends_with_newline:
                break
            raise ValueError(f"invalid legacy Session message at {path}:{line_number}") from exc
    return messages


def _object_sidecar(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid legacy sidecar: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"legacy sidecar must be an object: {path}")
    return data


def _continuation(path: Path) -> list[JsonObject]:
    if not path.exists():
        return []
    records: list[JsonObject] = []
    data = path.read_bytes()
    ends_with_newline = data.endswith(b"\n") or data.endswith(b"\r")
    lines = data.splitlines()
    for line_number, line_bytes in enumerate(lines, start=1):
        if not line_bytes.strip():
            continue
        try:
            line = line_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            if line_number == len(lines) and not ends_with_newline:
                break
            raise ValueError(f"invalid legacy continuation at {path}:{line_number}") from exc
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            if line_number == len(lines) and not ends_with_newline:
                break
            raise ValueError(f"invalid legacy continuation at {path}:{line_number}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"legacy continuation record must be an object: {path}:{line_number}")
        records.append(parsed)
    return records


def _artifacts(transcript: Path) -> tuple[Path, Path, Path, Path]:
    stem = transcript.stem
    return (
        transcript,
        transcript.with_name(f"{stem}.meta.json"),
        transcript.with_name(f"{stem}.activity.json"),
        transcript.with_name(f"{stem}.continuation.jsonl"),
    )
