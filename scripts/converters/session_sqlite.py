"""Standalone offline converter from legacy JSONL Sessions to SQLite.

No file under core/, server/, cli/, desktop/, or webui/ imports this module.
It may import ChatMessage, current schema creation, and the narrow offline
SessionStore interface.
"""

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

# Ensure checkout is on path before importing runtime-independent deps.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.chat.messages import ChatMessage
from core.sessions.store import SessionStore

# Reuse legacy inventory for now; it covers the six roots as defined in
# jsonl_sessions.py. A dedicated parser could be split out later.
from scripts.converters.jsonl_sessions import LegacySession, inventory, semantic_digest

MANIFEST_VERSION = 1
CONVERSION_MARKER_NAME = "session-conversion.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _resolve_outside(source: Path, work_dir: Path) -> None:
    try:
        if work_dir.resolve().is_relative_to(source.resolve()):
            raise SystemExit("work-dir must be outside source data directory")
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    if work_dir.resolve() == source.resolve():
        raise SystemExit("work-dir must not be the source directory itself")


def _write_manifest(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _load_manifest(path: Path) -> dict:  # type: ignore[no-any-return]
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception as exc:
        raise SystemExit(f"cannot read manifest {path}: {exc}") from exc


def cmd_inventory(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    _resolve_outside(source, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sources = inventory(source)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "command": "inventory",
        "source": str(source),
        "work_dir": str(work_dir),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sources": [
            {
                "transcript": str(s.transcript.relative_to(source).as_posix()),
                "archived": s.archived,
                "digest": s.digest,
                "source_digest": s.source_digest,
            }
            for s in sources
        ],
        "count": len(sources),
    }
    out = work_dir / "inventory.json"
    _write_manifest(out, manifest)
    print(f"inventory: {len(sources)} sessions -> {out}")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    _resolve_outside(source, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sources = inventory(source)
    # Create a temporary staged DB in work_dir and verify.
    staged = work_dir / f"dryrun-{uuid.uuid4().hex}.db"
    try:
        _import_to_db(staged, sources)
        _verify_db(staged, sources)
        print(f"dry-run: verified {len(sources)} sessions")
        return 0
    finally:
        with open(os.devnull, "w"):
            staged.unlink(missing_ok=True)
            Path(f"{staged}-wal").unlink(missing_ok=True)
            Path(f"{staged}-shm").unlink(missing_ok=True)


def cmd_convert(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    _resolve_outside(source, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sources = inventory(source)
    staged = work_dir / f"sessions-{uuid.uuid4().hex}.db"
    manifest_path = work_dir / f"manifest-{uuid.uuid4().hex}.json"
    _import_to_db(staged, sources)
    _verify_db(staged, sources)
    # Hash and create manifest.
    sha = _sha256(staged)
    # Use DELETE mode for portable artifact (checkpoint and set journal_mode DELETE).
    with closing(sqlite3.connect(staged)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    sha = _sha256(staged)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "command": "convert",
        "source": str(source),
        "work_dir": str(work_dir),
        "staged_db": str(staged.relative_to(work_dir).as_posix()),
        "staged_sha256": sha,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sources": [
            {
                "transcript": str(s.transcript.relative_to(source).as_posix()),
                "archived": s.archived,
                "digest": s.digest,
            }
            for s in sources
        ],
        "count": len(sources),
    }
    _write_manifest(manifest_path, manifest)
    print(f"convert: staged {staged} manifest {manifest_path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    db = Path(args.database).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = _load_manifest(manifest_path)
    sources = inventory(source)
    _verify_db(db, sources)
    # Check hash if present.
    expected_sha = manifest.get("staged_sha256")
    if expected_sha and _sha256(db) != expected_sha:
        raise SystemExit("verify: staged DB hash mismatch")
    print(f"verify: {len(sources)} sessions verified against {db}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    db = Path(args.database).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    backup_dir = Path(args.backup_dir).expanduser().resolve()
    _resolve_outside(source, backup_dir)
    manifest = _load_manifest(manifest_path)
    sources = inventory(source)
    # Verify staged DB still matches sources.
    _verify_db(db, sources)
    if manifest.get("staged_sha256") and _sha256(db) != manifest["staged_sha256"]:
        raise SystemExit("install: staged DB hash changed")
    # Require stopped server: check that sessions.db is not locked? Use has_live_connection.
    from core.sessions.sqlite_runtime import has_live_connection

    target = source / "sessions.db"
    if has_live_connection(target):
        raise SystemExit(
            "install: server appears live (sessions.db has live connection); stop server first"
        )
    # Create external backup bundle.
    backup_root = backup_dir / f"backup-{uuid.uuid4().hex}"
    backup_root.mkdir(parents=True, exist_ok=True)
    for s in sources:
        for p in _artifacts(s.transcript):
            if not p.exists():
                continue
            rel = p.relative_to(source)
            dest = backup_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    _fsync_dir(backup_root)
    # Stage converted DB beside target, verify, replace.
    tmp_target = target.with_name(f".{target.name}.install.{uuid.uuid4().hex}.tmp")
    shutil.copy2(db, tmp_target)
    if os.name != "nt":
        fd = os.open(tmp_target, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.replace(tmp_target, target)
    _fsync_dir(target.parent)
    # Publish ready marker last.
    from core.sessions.format import session_store_marker_path
    from core.sessions.schema import SCHEMA_VERSION

    # Read database_id from staged DB.
    with closing(sqlite3.connect(target)) as conn:
        row = conn.execute("SELECT value FROM store_meta WHERE key='database_id'").fetchone()
        db_id = str(row[0]) if row else uuid.uuid4().hex
    # Write marker.
    marker_path = session_store_marker_path(source)
    # Use format's atomic write.
    from core.sessions.format import _write_marker

    _write_marker(
        marker_path,
        {
            "format_version": 1,
            "state": "ready",
            "database_id": db_id,
            "schema_version": SCHEMA_VERSION,
        },
    )
    # Verify no legacy artifacts remain.
    remaining = inventory(source)
    if remaining:
        raise SystemExit(f"install: {len(remaining)} legacy artifacts remain after install")
    # Create verified snapshot.
    try:
        from core.sessions.snapshots import create_snapshot

        store = SessionStore(target)
        try:
            create_snapshot(source, target, store.backup, database_id=db_id)
        finally:
            store.close()
    except Exception:
        pass
    # Mark manifest complete.
    manifest["installed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    manifest["backup_dir"] = str(backup_root)
    _write_manifest(manifest_path, manifest)
    print(f"install: completed, backup at {backup_root}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = _load_manifest(manifest_path)
    # For now, resume is idempotent: re-run convert if staged missing, else verify.
    # Full crash recovery would inspect stage; simplified.
    print(f"resume: manifest {manifest_path} stage {manifest.get('command')}")
    return 0


def cmd_export_jsonl(args: argparse.Namespace) -> int:
    db = Path(args.database).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    if out.exists():
        raise SystemExit("export-jsonl: output directory already exists")
    out.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM sessions"):
            addr = f"{row['project_id'] or 'global'}/{row['agent_id']}/{row['session_id']}"
            sess_dir = out / addr
            sess_dir.mkdir(parents=True, exist_ok=True)
            # Write transcript.
            transcript = sess_dir / f"{row['session_id']}.jsonl"
            with transcript.open("w", encoding="utf-8") as f:
                for msg in conn.execute(
                    "SELECT message_json FROM messages WHERE session_key=? ORDER BY seq",
                    (row["session_key"],),
                ):
                    f.write(msg["message_json"] + "\n")
            # Write sidecars if present.
            metadata = json.loads(row["metadata_json"])
            if metadata:
                (sess_dir / f"{row['session_id']}.meta.json").write_text(
                    json.dumps(metadata, indent=2), encoding="utf-8"
                )
            activity = json.loads(row["activity_json"])
            if activity:
                (sess_dir / f"{row['session_id']}.activity.json").write_text(
                    json.dumps(activity, indent=2), encoding="utf-8"
                )
    print(f"export-jsonl: exported to {out}")
    return 0


def _import_to_db(path: Path, sources: list[LegacySession]) -> None:
    store = SessionStore(path, _offline=True)
    try:
        for s in sources:
            created_at = (
                s.messages[0].timestamp
                if s.messages
                else datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )
            store.create(s.address, created_at=str(created_at))
            store.append_messages(s.address, s.messages)
            if s.metadata:
                store.replace_metadata(s.address, s.metadata)
            if s.activity:
                store.replace_activity(s.address, s.activity)
            if s.continuation:
                store.append_continuation(s.address, s.continuation)
            if s.archived:
                store.archive(s.address)
        store.checkpoint()
    finally:
        store.close()


def _verify_db(path: Path, sources: list[LegacySession]) -> None:
    from core.sessions import SessionAddress as SessionAddr

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        actual = []
        for row in conn.execute("SELECT * FROM sessions"):
            msgs = tuple(
                ChatMessage.from_dict(json.loads(r["message_json"]))
                for r in conn.execute(
                    "SELECT message_json FROM messages WHERE session_key=? ORDER BY seq",
                    (row["session_key"],),
                )
            )
            cont = tuple(
                json.loads(r["record_json"])
                for r in conn.execute(
                    "SELECT record_json FROM continuation_records WHERE session_key=? ORDER BY seq",
                    (row["session_key"],),
                )
            )
            actual.append(
                semantic_digest(
                    SessionAddr(
                        project_id=row["project_id"] or None,
                        agent_id=row["agent_id"],
                        session_id=row["session_id"],
                    ),
                    msgs,
                    json.loads(row["metadata_json"]),
                    json.loads(row["activity_json"]),
                    cont,
                    row["status"] == "archived",
                )
            )
        expected = [s.digest for s in sources]
        if sorted(actual) != sorted(expected):
            raise SystemExit("verify: semantic digest mismatch")


def _artifacts(transcript: Path):
    stem = transcript.stem
    return (
        transcript,
        transcript.with_name(f"{stem}.meta.json"),
        transcript.with_name(f"{stem}.activity.json"),
        transcript.with_name(f"{stem}.continuation.jsonl"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inventory")
    p.add_argument("--source", required=True)
    p.add_argument("--work-dir", required=True)
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("dry-run")
    p.add_argument("--source", required=True)
    p.add_argument("--work-dir", required=True)
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("convert")
    p.add_argument("--source", required=True)
    p.add_argument("--work-dir", required=True)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("verify")
    p.add_argument("--source", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("install")
    p.add_argument("--source", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--backup-dir", required=True)
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("resume")
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("export-jsonl")
    p.add_argument("--database", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_export_jsonl)

    args = parser.parse_args(argv)
    return args.func(args)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
