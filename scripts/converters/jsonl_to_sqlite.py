"""Explicit offline conversion from legacy JSONL Sessions to ``sessions.db``."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

# Converter scripts execute by path, so place this checkout ahead of any editable
# installation before importing the runtime-independent converter dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.chat.messages import ChatMessage
from core.sessions import SessionAddress
from core.sessions.store import SessionStore
from scripts.converters.jsonl_sessions import LegacySession, inventory, semantic_digest

CONVERSION_MARKER_NAME = "session-conversion.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    final = data_dir / "sessions.db"
    marker = data_dir / CONVERSION_MARKER_NAME
    if args.resume:
        return _resume(data_dir, final, marker)
    if final.exists() or marker.exists():
        parser.error("sessions.db or an incomplete conversion marker already exists; use --resume")
    run_id = uuid.uuid4().hex
    staging_root = data_dir / "artifacts" / "temp" / f"session-conversion-{run_id}"
    staging_db = staging_root / "sessions.db"
    if args.dry_run:
        sources = inventory(data_dir)
        _import(staging_db, sources)
        _verify_import(staging_db, sources)
        shutil.rmtree(staging_root, ignore_errors=True)
        print(f"validated {len(sources)} legacy Sessions")
        return 0
    staging_root.mkdir(parents=True, exist_ok=False)
    _write_marker(marker, {"run_id": run_id, "stage": "building", "staging": str(staging_db)})
    try:
        sources = inventory(data_dir)
        source_snapshot = _source_snapshot(sources)
        source_manifest = _source_manifest(data_dir, sources)
        _import(staging_db, sources)
        _verify_import(staging_db, sources)
        _require_unchanged_sources(data_dir, source_snapshot)
        _write_marker(
            marker,
            {
                "run_id": run_id,
                "stage": "ready_to_publish",
                "staging": str(staging_db),
                "source_snapshot": source_snapshot,
                "source_manifest": source_manifest,
            },
        )
        os.replace(staging_db, final)
        _write_marker(
            marker,
            {
                "run_id": run_id,
                "stage": "database_published",
                "source_snapshot": source_snapshot,
                "source_manifest": source_manifest,
            },
        )
        _require_unchanged_sources(data_dir, source_snapshot)
        _verify_import(final, sources)
        _relocate_sources(data_dir, run_id, sources)
        marker.unlink()
        shutil.rmtree(staging_root, ignore_errors=True)
    except Exception:
        raise
    print(f"converted {len(sources)} legacy Sessions")
    return 0


def _import(path: Path, sources: list[LegacySession]) -> None:
    store = SessionStore(path, _offline=True)
    try:
        for source in sources:
            created_at = (
                source.messages[0].timestamp
                if source.messages
                else datetime.fromtimestamp(source.transcript.stat().st_mtime, UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            store.create(source.address, created_at=str(created_at))
            store.append_messages(source.address, source.messages)
            if source.metadata:
                store.replace_metadata(source.address, source.metadata)
            if source.activity:
                store.replace_activity(source.address, source.activity)
            if source.continuation:
                store.append_continuation(source.address, source.continuation)
            if source.archived:
                store.archive(source.address)
        store.checkpoint()
    finally:
        store.close()


def _resume(data_dir: Path, final: Path, marker: Path) -> int:
    if not marker.exists():
        raise SystemExit("cannot resume: conversion marker does not exist")
    data = _read_marker(marker)
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise SystemExit("cannot resume: malformed conversion marker")
    stage = data.get("stage")
    if stage == "database_published":
        if not final.is_file():
            raise SystemExit("cannot resume: published Session database is missing")
        store = SessionStore(final, _offline=True)
        store.close()
        manifest = _read_source_manifest(data_dir, data.get("source_manifest"))
        _relocate_transcripts(data_dir, run_id, manifest)
        marker.unlink()
        print("resumed Session conversion")
        return 0
    if stage not in {"building", "ready_to_publish"} or final.exists():
        raise SystemExit("cannot resume: malformed or unsafe conversion state")
    staging = data.get("staging")
    if isinstance(staging, str):
        staging_path = Path(staging)
        if staging_path.parent.is_relative_to(data_dir / "artifacts" / "temp"):
            shutil.rmtree(staging_path.parent, ignore_errors=True)
    marker.unlink()
    return main([str(data_dir)])


def _read_marker(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("cannot resume: malformed conversion marker") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise SystemExit("cannot resume: malformed conversion marker")
    return value


def _write_marker(path: Path, value: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _relocate_sources(data_dir: Path, run_id: str, sources: list[LegacySession]) -> None:
    _relocate_transcripts(data_dir, run_id, [source.transcript for source in sources])


def _relocate_transcripts(data_dir: Path, run_id: str, transcripts: list[Path]) -> None:
    destination_root = data_dir / "archive" / f"jsonl-migration-{run_id}"
    for transcript in transcripts:
        for source_path in _artifacts(transcript):
            if not source_path.exists():
                destination = destination_root / source_path.relative_to(data_dir)
                if destination.exists():
                    continue
                if source_path == transcript:
                    raise RuntimeError(f"legacy Session source disappeared: {source_path}")
                continue
            relative = source_path.relative_to(data_dir)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_path, destination)
            _fsync_directory(source_path.parent)
            _fsync_directory(destination.parent)


def _artifacts(transcript: Path) -> tuple[Path, Path, Path, Path]:
    stem = transcript.stem
    return (
        transcript,
        transcript.with_name(f"{stem}.meta.json"),
        transcript.with_name(f"{stem}.activity.json"),
        transcript.with_name(f"{stem}.continuation.jsonl"),
    )


def _source_snapshot(sources: list[LegacySession]) -> str:
    payload = [
        {
            "address": [
                source.address.project_id,
                source.address.agent_id,
                source.address.session_id,
            ],
            "archived": source.archived,
            "digest": source.source_digest,
        }
        for source in sources
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_manifest(data_dir: Path, sources: list[LegacySession]) -> str:
    return json.dumps(
        [source.transcript.relative_to(data_dir).as_posix() for source in sources],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _read_source_manifest(data_dir: Path, payload: object) -> list[Path]:
    if not isinstance(payload, str):
        raise SystemExit("cannot resume: conversion source manifest is missing")
    try:
        values = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit("cannot resume: conversion source manifest is malformed") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise SystemExit("cannot resume: conversion source manifest is malformed")
    paths: list[Path] = []
    for value in values:
        path = (data_dir / value).resolve()
        if not path.is_relative_to(data_dir):
            raise SystemExit("cannot resume: conversion source path escapes the data directory")
        paths.append(path)
    return paths


def _require_unchanged_sources(data_dir: Path, expected: str) -> None:
    if _source_snapshot(inventory(data_dir)) != expected:
        raise RuntimeError("legacy Session sources changed during offline conversion")


def _verify_import(path: Path, sources: list[LegacySession]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        actual: list[str] = []
        rows = connection.execute(
            "SELECT * FROM sessions ORDER BY status = 'archived' DESC, "
            "project_id, agent_id, session_id"
        ).fetchall()
        for row in rows:
            messages = tuple(
                ChatMessage.from_dict(json.loads(message["message_json"]))
                for message in connection.execute(
                    "SELECT message_json FROM messages WHERE session_key = ? ORDER BY seq",
                    (row["session_key"],),
                )
            )
            continuation = tuple(
                json.loads(record["record_json"])
                for record in connection.execute(
                    "SELECT record_json FROM continuation_records "
                    "WHERE session_key = ? ORDER BY seq",
                    (row["session_key"],),
                )
            )
            actual.append(
                semantic_digest(
                    SessionAddress(
                        project_id=row["project_id"] or None,
                        agent_id=row["agent_id"],
                        session_id=row["session_id"],
                    ),
                    messages,
                    json.loads(row["metadata_json"]),
                    json.loads(row["activity_json"]),
                    continuation,
                    row["status"] == "archived",
                )
            )
    expected = [source.digest for source in sources]
    if sorted(actual) != sorted(expected):
        raise RuntimeError("converted Session database does not match legacy sources")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    sys.exit(main())
