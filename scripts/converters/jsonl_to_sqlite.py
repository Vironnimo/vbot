"""Explicit offline conversion from legacy JSONL Sessions to ``sessions.db``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

# Converter scripts execute by path, so place this checkout ahead of any editable
# installation before importing the runtime-independent converter dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.sessions import SessionStore
from core.sessions.store import CONVERSION_MARKER_NAME
from scripts.converters.jsonl_sessions import LegacySession, inventory


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
    sources = inventory(data_dir)
    run_id = uuid.uuid4().hex
    staging_root = data_dir / "artifacts" / "temp" / f"session-conversion-{run_id}"
    staging_db = staging_root / "sessions.db"
    if args.dry_run:
        _import(staging_db, sources)
        shutil.rmtree(staging_root, ignore_errors=True)
        print(f"validated {len(sources)} legacy Sessions")
        return 0
    staging_root.mkdir(parents=True, exist_ok=False)
    _write_marker(marker, {"run_id": run_id, "stage": "building", "staging": str(staging_db)})
    try:
        _import(staging_db, sources)
        _write_marker(
            marker, {"run_id": run_id, "stage": "ready_to_publish", "staging": str(staging_db)}
        )
        os.replace(staging_db, final)
        _write_marker(marker, {"run_id": run_id, "stage": "database_published"})
        _relocate_sources(data_dir, run_id, sources)
        marker.unlink()
        shutil.rmtree(staging_root, ignore_errors=True)
    except Exception:
        raise
    print(f"converted {len(sources)} legacy Sessions")
    return 0


def _import(path: Path, sources: list[LegacySession]) -> None:
    store = SessionStore(path)
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
        store = SessionStore(final, allow_conversion_marker=True)
        store.close()
        _relocate_sources(data_dir, run_id, inventory(data_dir))
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


def _relocate_sources(data_dir: Path, run_id: str, sources: list[LegacySession]) -> None:
    destination_root = data_dir / "archive" / f"jsonl-migration-{run_id}"
    for source in sources:
        for source_path in _artifacts(source.transcript):
            if not source_path.exists():
                continue
            relative = source_path.relative_to(data_dir)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_path, destination)


def _artifacts(transcript: Path) -> tuple[Path, Path, Path, Path]:
    stem = transcript.stem
    return (
        transcript,
        transcript.with_name(f"{stem}.meta.json"),
        transcript.with_name(f"{stem}.activity.json"),
        transcript.with_name(f"{stem}.continuation.jsonl"),
    )


if __name__ == "__main__":
    sys.exit(main())
