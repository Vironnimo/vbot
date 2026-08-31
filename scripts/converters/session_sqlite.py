"""Standalone offline converter and cutover engine for legacy JSONL Sessions.

The application never imports this module. Every command works on an explicit
source and external work directory so conversion and recovery rehearsals can be
performed on copies without touching a configured vBot data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.sessions import SessionAddress
from core.sessions.format import (
    MAINTENANCE_GUARD_FILE_NAME,
    publish_ready_marker,
    read_session_store_marker,
)
from core.sessions.schema import APPLICATION_ID, SCHEMA_VERSION
from core.sessions.snapshots import create_snapshot
from core.sessions.store import (
    SessionStore,
    continuation_from_connection,
    messages_from_connection,
)
from scripts.converters.jsonl_sessions import (
    CaptureInventory,
    LegacySession,
    capture_inventory,
)

MANIFEST_VERSION = 3
MANIFEST_NAME = "conversion-manifest.json"
EXPORT_MANIFEST_NAME = "export-manifest.json"
ALLOWED_STAGES = (
    "captured",
    "converted",
    "install_preflight",
    "backup_publishing",
    "sources_relocating",
    "database_publishing",
    "marker_publishing",
    "runtime_verifying",
    "complete",
)
_TRANSITION_HOOK: Any = None
_RELOCATION_CHECKPOINT_BATCH_SIZE = 250


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"manifest is not an object: {path}")
    return value


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise SystemExit("unsupported conversion manifest version")
    stage = manifest.get("stage")
    if stage not in ALLOWED_STAGES:
        raise SystemExit("conversion manifest has an invalid stage")
    for field in ("source", "work_dir", "run_id", "sources"):
        if field not in manifest:
            raise SystemExit(f"conversion manifest is missing {field}")
    if not isinstance(manifest["sources"], list):
        raise SystemExit("conversion manifest sources are malformed")
    return manifest


def _resolve_outside(source: Path, external: Path) -> None:
    source_root = source.resolve()
    external_root = external.resolve()
    if external_root == source_root or external_root.is_relative_to(source_root):
        raise SystemExit("work and backup directories must be outside the source data directory")


def _invoke_transition(stage: str, boundary: str) -> None:
    if _TRANSITION_HOOK is not None:
        _TRANSITION_HOOK(stage, boundary)


def _set_stage(manifest_path: Path, manifest: dict[str, Any], stage: str) -> None:
    manifest["stage"] = stage
    manifest.setdefault("transitions", []).append(
        {"stage": stage, "boundary": "before", "at": _now()}
    )
    _invoke_transition(stage, "before")
    _write_json(manifest_path, manifest)
    _invoke_transition(stage, "after")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _address_payload(address: SessionAddress) -> list[str | None]:
    return [address.project_id, address.agent_id, address.session_id]


def _source_record(session: LegacySession, source: Path) -> dict[str, Any]:
    artifacts = [
        {
            "relative_path": artifact.relative_path,
            "kind": artifact.kind,
            "present": artifact.present,
            "sha256": artifact.sha256,
            "size": artifact.size,
            "mtime_ns": artifact.mtime_ns,
        }
        for artifact in session.captured_artifacts
    ]
    return {
        "relative_path": session.transcript.relative_to(source).as_posix(),
        "root_kind": session.root_kind,
        "address": _address_payload(session.address),
        "archived": session.archived,
        "generation_id": session.generation_id,
        "source_digest": session.source_digest,
        "message_count": len(session.messages),
        "artifacts": artifacts,
        "ignored_tails": [tail.__dict__ for tail in session.ignored_tails],
    }


def _inventory_payload(capture: CaptureInventory, source: Path) -> dict[str, Any]:
    sessions = list(capture.sessions)
    return {
        "source": str(source),
        "captured_at": _now(),
        "sources": [_source_record(session, source) for session in sessions],
        "evidence": {
            "orphan_sidecars": list(capture.orphan_sidecars),
            "unknown_files": list(capture.unknown_files),
            "rejected_paths": list(capture.rejected_paths),
            "skipped_sessions": list(capture.skipped_sessions),
        },
        "count": len(sessions),
    }


def _capture_source(source: Path) -> CaptureInventory:
    capture = capture_inventory(source)
    if capture.rejected_paths:
        raise SystemExit(
            "source contains rejected symlink or special paths: "
            + ", ".join(capture.rejected_paths)
        )
    return capture


def _same_capture(manifest: dict[str, Any], capture: CaptureInventory, source: Path) -> None:
    expected = {
        "sources": [_source_record(session, source) for session in capture.sessions],
        "evidence": {
            "orphan_sidecars": list(capture.orphan_sidecars),
            "unknown_files": list(capture.unknown_files),
            "rejected_paths": list(capture.rejected_paths),
            "skipped_sessions": list(capture.skipped_sessions),
        },
    }
    actual = {"sources": manifest["sources"], "evidence": manifest.get("evidence", {})}
    if actual != expected:
        raise RuntimeError("legacy Session sources changed since immutable capture")


def cmd_inventory(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    _resolve_outside(source, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    capture = _capture_source(source)
    output = work_dir / "inventory.json"
    _write_json(output, _inventory_payload(capture, source))
    print(f"inventory: {len(capture.sessions)} Sessions -> {output}")
    return 0


def _staged_database_path(work_dir: Path, run_id: str) -> Path:
    return work_dir / f".sessions-{run_id}.db"


def _created_at(session: LegacySession) -> str:
    if session.messages:
        return session.messages[0].timestamp
    transcript = session.captured_artifacts[0]
    timestamp = transcript.mtime_ns or 0
    return datetime.fromtimestamp(timestamp / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def _import_to_db(path: Path, sessions: tuple[LegacySession, ...]) -> None:
    store = SessionStore(path, _offline=True)
    try:
        store.prepare_offline_bulk_import()
        for session in sessions:
            store.import_generation(
                session.address,
                generation_id=session.generation_id,
                messages=session.messages,
                metadata=session.metadata,
                activity=session.activity,
                continuation=session.continuation,
                archived=session.archived,
                created_at=_created_at(session),
            )
        store.finish_offline_bulk_import()
        store.checkpoint()
    finally:
        store.close()
    _force_delete_journal(path)


def _force_delete_journal(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise RuntimeError("staged database did not enter DELETE journal mode")
        connection.commit()
    _fsync_file(path)


def _verify_db(path: Path, sessions: tuple[LegacySession, ...]) -> dict[str, Any]:
    return _verify_db_expected(
        path, {session.generation_id: len(session.messages) for session in sessions}
    )


def _verify_db_expected(path: Path, expected: dict[str, int]) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"staged database is missing: {path}")
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise RuntimeError("staged database schema version mismatch")
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
            raise RuntimeError("staged database application identity mismatch")
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise RuntimeError("staged database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("staged database foreign-key check failed")
        rows = connection.execute(
            "SELECT s.generation_id, s.message_count, COUNT(m.message_key) AS actual_count "
            "FROM sessions AS s LEFT JOIN messages AS m ON m.session_key = s.session_key "
            "GROUP BY s.session_key ORDER BY s.session_key"
        ).fetchall()
        if len(rows) != len(expected):
            raise RuntimeError("converted Session count does not match immutable capture")
        actual: dict[str, int] = {}
        for row in rows:
            count = int(row["actual_count"])
            if int(row["message_count"]) != count:
                raise RuntimeError(
                    "converted Session message counter does not match canonical rows"
                )
            actual[str(row["generation_id"])] = count
        if actual != expected:
            raise RuntimeError("converted Session message coverage does not match capture")
        return {
            "session_count": len(rows),
            "message_count": int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "sha256": _sha256(path),
        }


def cmd_dry_run(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    _resolve_outside(source, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    capture = _capture_source(source)
    run_id = uuid.uuid4().hex
    staged = _staged_database_path(work_dir, f"dryrun-{run_id}")
    try:
        _import_to_db(staged, capture.sessions)
        result = _verify_db(staged, capture.sessions)
        print(
            f"dry-run: verified {result['session_count']} Sessions and "
            f"{result['message_count']} Messages"
        )
        return 0
    finally:
        for path in (staged, Path(f"{staged}-wal"), Path(f"{staged}-shm")):
            path.unlink(missing_ok=True)


def cmd_convert(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    _resolve_outside(source, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / MANIFEST_NAME
    if manifest_path.exists():
        raise SystemExit(f"conversion manifest already exists: {manifest_path}; use resume")
    capture = _capture_source(source)
    run_id = uuid.uuid4().hex
    staged = _staged_database_path(work_dir, run_id)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "command": "legacy-jsonl-to-sqlite",
        "stage": "captured",
        "source": str(source),
        "work_dir": str(work_dir),
        "captured_at": _now(),
        "staged_db": staged.name,
        **_inventory_payload(capture, source),
        "transitions": [],
    }
    _write_json(manifest_path, manifest)
    try:
        _import_to_db(staged, capture.sessions)
        verification = _verify_db(staged, capture.sessions)
        database_id = _database_id(staged)
        manifest.update({"database_id": database_id, "database": verification})
        _set_stage(manifest_path, manifest, "converted")
    except Exception:
        # The immutable source and any partial staged output remain available for
        # forensic inspection; the manifest prevents a false successful resume.
        raise
    print(f"convert: staged {len(capture.sessions)} Sessions in {staged}; manifest {manifest_path}")
    return 0


def _database_id(path: Path) -> str:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'database_id'"
        ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise RuntimeError("converted database has no identity")
    return str(row[0])


def cmd_verify(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    database = Path(args.database).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = _validate_manifest(_load_json(manifest_path))
    capture = _capture_source(source)
    _same_capture(manifest, capture, source)
    result = _verify_db(database, capture.sessions)
    if manifest.get("staged_sha256") and manifest["staged_sha256"] != result["sha256"]:
        raise RuntimeError("staged database hash mismatch")
    if manifest.get("database_id") and manifest["database_id"] != _database_id(database):
        raise RuntimeError("staged database identity mismatch")
    print(
        f"verify: {result['session_count']} Sessions and "
        f"{result['message_count']} Messages verified"
    )
    return 0


def _server_is_stopped(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"", "*", "0.0.0.0"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.5):
            return False
    except OSError:
        return True


def _require_server_stopped(host: str, port: int) -> None:
    if not _server_is_stopped(host, port):
        raise RuntimeError(f"the exact vBot target is reachable at {host}:{port}; stop it first")


def _require_free_space(path: Path, bytes_needed: int) -> None:
    if shutil.disk_usage(path).free < max(bytes_needed * 2, 64 * 1024 * 1024):
        raise RuntimeError("insufficient free space for external backup and staged database")


def _ensure_maintenance_guard(source: Path, manifest: dict[str, Any]) -> None:
    """Block Runtime startup across every source-mutating install boundary."""

    path = source / MAINTENANCE_GUARD_FILE_NAME
    expected = {
        "operation": "session-sqlite-install",
        "run_id": str(manifest["run_id"]),
        "database_id": str(manifest["database_id"]),
    }
    if path.exists():
        if _load_json(path) != expected:
            raise RuntimeError("another Session-store maintenance operation owns the target")
        return
    _write_json(path, expected)


def _remove_maintenance_guard(source: Path) -> None:
    path = source / MAINTENANCE_GUARD_FILE_NAME
    path.unlink(missing_ok=True)
    _fsync_dir(source)


def _backup_external(
    backup_dir: Path, source: Path, sessions: tuple[LegacySession, ...], run_id: str
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    staging = backup_dir / f".{run_id}.backup.tmp"
    final = backup_dir / f"session-conversion-{run_id}"
    if final.exists():
        if _external_backup_matches(final, source, sessions):
            return final
        raise RuntimeError("external conversion backup already exists but does not match capture")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for session in sessions:
            for artifact in session.captured_artifacts:
                if not artifact.present:
                    continue
                target = staging / "legacy" / artifact.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(artifact.path, target)
                if target.stat().st_size != artifact.size or _sha256(target) != artifact.sha256:
                    raise RuntimeError(
                        f"legacy Session source changed while backing up: {artifact.relative_path}"
                    )
                _fsync_file(target)
        _write_json(
            staging / "backup-manifest.json",
            {
                "manifest_version": MANIFEST_VERSION,
                "source": str(source),
                "files": [
                    {
                        "relative_path": artifact.relative_path,
                        "sha256": artifact.sha256,
                        "size": artifact.size,
                    }
                    for session in sessions
                    for artifact in session.captured_artifacts
                    if artifact.present
                ],
            },
        )
        _fsync_dir(staging)
        os.replace(staging, final)
        _fsync_dir(backup_dir)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _external_backup_matches(
    bundle: Path, source: Path, sessions: tuple[LegacySession, ...]
) -> bool:
    """Accept an already-published bundle only when its immutable evidence matches."""

    manifest_path = bundle / "backup-manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    expected_files = [
        {
            "relative_path": artifact.relative_path,
            "sha256": artifact.sha256,
            "size": artifact.size,
        }
        for session in sessions
        for artifact in session.captured_artifacts
        if artifact.present
    ]
    if not isinstance(payload, dict) or payload.get("source") != str(source):
        return False
    if payload.get("files") != expected_files:
        return False
    for record in expected_files:
        path = bundle / "legacy" / Path(str(record["relative_path"]))
        try:
            if path.stat().st_size != record["size"] or _sha256(path) != record["sha256"]:
                return False
        except OSError:
            return False
    return True


def _relocate_sources(
    source: Path,
    backup_root: Path,
    sessions: tuple[LegacySession, ...] | None,
    manifest_path: Path,
    manifest: dict[str, Any],
    require_stopped: Callable[[], None],
) -> None:
    relocated: set[str] = set(manifest.get("relocated", []))
    if sessions is not None:
        artifacts = [
            {
                "relative_path": artifact.relative_path,
                "present": artifact.present,
                "sha256": artifact.sha256,
                "size": artifact.size,
            }
            for session in sessions
            for artifact in session.captured_artifacts
        ]
    else:
        artifacts = [artifact for record in manifest["sources"] for artifact in record["artifacts"]]
    pending_checkpoint = 0
    for artifact in artifacts:
        relative_path = str(artifact["relative_path"])
        artifact_size = artifact.get("size")
        if (
            isinstance(artifact_size, bool)
            or not isinstance(artifact_size, int)
            or artifact_size < 0
        ):
            raise RuntimeError(f"source manifest has an invalid size: {relative_path}")
        if not artifact["present"] or relative_path in relocated:
            continue
        if pending_checkpoint == 0:
            require_stopped()
        original = source / Path(relative_path)
        destination = backup_root / "relocated" / Path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if original.exists():
            if original.stat().st_size != artifact_size or _sha256(original) != artifact["sha256"]:
                raise RuntimeError(f"source changed before relocation: {relative_path}")
            os.replace(original, destination)
            _fsync_dir(destination.parent)
        elif (
            not destination.is_file()
            or destination.stat().st_size != artifact_size
            or _sha256(destination) != artifact["sha256"]
        ):
            raise RuntimeError(f"source disappeared before relocation: {relative_path}")
        relocated.add(relative_path)
        pending_checkpoint += 1
        if pending_checkpoint >= _RELOCATION_CHECKPOINT_BATCH_SIZE:
            manifest["relocated"] = sorted(relocated)
            _write_json(manifest_path, manifest)
            pending_checkpoint = 0
    if pending_checkpoint:
        manifest["relocated"] = sorted(relocated)
        _write_json(manifest_path, manifest)


def _publish_database(staged: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(staged, temporary)
        if _sha256(temporary) != _sha256(staged):
            raise RuntimeError("target database copy hash mismatch")
        _fsync_file(temporary)
        os.replace(temporary, target)
        _fsync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _install_runtime_verify(source: Path, expected: dict[str, int]) -> dict[str, Any]:
    target = source / "sessions.db"
    marker = read_session_store_marker(source)
    if marker is None or marker["state"] != "ready":
        raise RuntimeError("published Session-store marker is not ready")
    if _database_id(target) != str(marker["database_id"]):
        raise RuntimeError("published marker and database identity differ")
    store = SessionStore(target, _offline=True)
    try:
        result = _verify_db_expected(target, expected)
    finally:
        store.close()
    return result


def _create_live_snapshot(source: Path, target: Path, database_id: str) -> dict[str, Any]:
    store = SessionStore(target, _offline=True)
    try:
        snapshot = create_snapshot(
            source,
            target,
            store.backup,
            database_id=database_id,
            reason="conversion-install",
        )
        if snapshot is None:
            raise RuntimeError("live initial Session snapshot was not published")
        store.record_snapshot_checkpoint(Path(snapshot).name, store.snapshot_revisions())
    finally:
        store.close()
    return {"id": Path(snapshot).name, "directory": str(Path(snapshot).relative_to(source))}


def _install_from_manifest(
    manifest_path: Path, manifest: dict[str, Any], host: str, port: int, backup_dir: Path | None
) -> None:
    source = Path(str(manifest["source"])).resolve()
    work_dir = Path(str(manifest["work_dir"])).resolve()
    staged = work_dir / str(manifest["staged_db"])
    stage = str(manifest["stage"])
    if stage == "complete":
        _remove_maintenance_guard(source)
        return
    capture: CaptureInventory | None = None
    if stage in {"converted", "install_preflight", "backup_publishing"}:
        capture = _capture_source(source)
        _same_capture(manifest, capture, source)
        expected = {session.generation_id: len(session.messages) for session in capture.sessions}
    else:
        expected = {
            str(record["generation_id"]): int(record["message_count"])
            for record in manifest["sources"]
        }
    _require_server_stopped(host, port)
    target = source / "sessions.db"
    marker = source / "session-store.json"
    if backup_dir is None and manifest["stage"] == "converted":
        configured_backup = manifest.get("backup_dir")
        if not isinstance(configured_backup, str) or not configured_backup:
            raise RuntimeError("install requires an external backup directory")
        backup_dir = Path(configured_backup).resolve()
    if backup_dir is not None:
        _resolve_outside(source, backup_dir)
    if stage in {"converted", "install_preflight", "backup_publishing", "sources_relocating"} and (
        target.exists() or marker.exists()
    ):
        raise RuntimeError("install target already contains a database or current-format marker")
    _require_free_space((backup_dir or work_dir).parent, staged.stat().st_size)
    _verify_db_expected(staged, expected)
    if manifest.get("database", {}).get("sha256") != _sha256(staged):
        raise RuntimeError("staged database changed after conversion")

    if manifest["stage"] == "captured":
        raise RuntimeError("conversion is not complete; resume conversion before install")
    if manifest["stage"] == "converted":
        if backup_dir is None:
            raise RuntimeError("install requires an external backup directory")
        manifest["backup_dir"] = str(backup_dir)
        _write_json(manifest_path, manifest)
        _set_stage(manifest_path, manifest, "install_preflight")
    if manifest["stage"] == "install_preflight":
        if backup_dir is None:
            backup_dir = Path(str(manifest["backup_dir"]))
        if capture is None:
            raise RuntimeError("source capture is unavailable before external backup")
        backup_root = _backup_external(
            backup_dir, source, capture.sessions, str(manifest["run_id"])
        )
        manifest["backup_dir"] = str(backup_root)
        _set_stage(manifest_path, manifest, "backup_publishing")
    backup_root = Path(str(manifest["backup_dir"]))
    if manifest["stage"] == "backup_publishing":
        if not backup_root.is_dir():
            raise RuntimeError("external backup bundle is missing")
        _ensure_maintenance_guard(source, manifest)
        _set_stage(manifest_path, manifest, "sources_relocating")
    if manifest["stage"] == "sources_relocating":
        _ensure_maintenance_guard(source, manifest)
        _relocate_sources(
            source,
            backup_root,
            None if capture is None else capture.sessions,
            manifest_path,
            manifest,
            lambda: _require_server_stopped(host, port),
        )
        _set_stage(manifest_path, manifest, "database_publishing")
    if manifest["stage"] == "database_publishing":
        _ensure_maintenance_guard(source, manifest)
        _require_server_stopped(host, port)
        if not target.is_file() or _database_id(target) != str(manifest["database_id"]):
            _publish_database(staged, target)
        if _database_id(target) != str(manifest["database_id"]):
            raise RuntimeError("published database identity mismatch")
        _set_stage(manifest_path, manifest, "marker_publishing")
    if manifest["stage"] == "marker_publishing":
        _ensure_maintenance_guard(source, manifest)
        _require_server_stopped(host, port)
        publish_ready_marker(source, str(manifest["database_id"]))
        _set_stage(manifest_path, manifest, "runtime_verifying")
    if manifest["stage"] == "runtime_verifying":
        _ensure_maintenance_guard(source, manifest)
        _require_server_stopped(host, port)
        verification = _install_runtime_verify(source, expected)
        _require_server_stopped(host, port)
        manifest["live_snapshot"] = _create_live_snapshot(
            source, target, str(manifest["database_id"])
        )
        _require_server_stopped(host, port)
        manifest["installed_database"] = verification
        manifest["completed_at"] = _now()
        _set_stage(manifest_path, manifest, "complete")
        _remove_maintenance_guard(source)


def cmd_install(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = _validate_manifest(_load_json(manifest_path))
    expected_source = Path(str(manifest["source"])).resolve()
    if Path(args.source).expanduser().resolve() != expected_source:
        raise SystemExit("--source does not match the conversion manifest")
    expected_database = Path(str(manifest["work_dir"])).resolve() / str(manifest["staged_db"])
    if Path(args.database).expanduser().resolve() != expected_database:
        raise SystemExit("--database does not match the staged database in the manifest")
    backup_dir = Path(args.backup_dir).expanduser().resolve()
    _resolve_outside(expected_source, backup_dir)
    _install_from_manifest(manifest_path, manifest, str(args.host), int(args.port), backup_dir)
    print(f"install: complete; external evidence at {manifest['backup_dir']}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = _validate_manifest(_load_json(manifest_path))
    if manifest["stage"] == "complete":
        print(f"resume: already complete: {manifest_path}")
        return 0
    if manifest["stage"] == "captured":
        source = Path(str(manifest["source"])).resolve()
        capture = _capture_source(source)
        _same_capture(manifest, capture, source)
        staged = Path(str(manifest["work_dir"])).resolve() / str(manifest["staged_db"])
        for path in (staged, Path(f"{staged}-wal"), Path(f"{staged}-shm")):
            path.unlink(missing_ok=True)
        _import_to_db(staged, capture.sessions)
        manifest["database"] = _verify_db(staged, capture.sessions)
        manifest["database_id"] = _database_id(staged)
        _set_stage(manifest_path, manifest, "converted")
        print(f"resume: converted staged database at {staged}")
        return 0
    _install_from_manifest(manifest_path, manifest, str(args.host), int(args.port), None)
    print(f"resume: {manifest['stage']} at {manifest_path}")
    return 0


def _write_export_file(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256_bytes(data)


def cmd_export_jsonl(args: argparse.Namespace) -> int:
    database = Path(args.database).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise SystemExit("export-jsonl output directory already exists")
    if output == database.parent or output.is_relative_to(database.parent):
        raise SystemExit("export-jsonl output must be outside the database directory")
    output.mkdir(parents=True)
    exported: list[dict[str, Any]] = []
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM sessions ORDER BY session_key").fetchall()
        for row in rows:
            generation = str(row["generation_id"])
            scope = row["project_id"] or "global"
            session_root = (
                output / scope / str(row["agent_id"]) / str(row["session_id"]) / generation
            )
            messages = [
                message.to_dict()
                for message in messages_from_connection(connection, int(row["session_key"]))
            ]
            transcript = b"".join(
                (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
                for message in messages
            )
            transcript_path = session_root / f"{row['session_id']}.jsonl"
            files = [
                {
                    "path": transcript_path.relative_to(output).as_posix(),
                    "sha256": _write_export_file(transcript_path, transcript),
                }
            ]
            for suffix, key in (("meta.json", "metadata_json"), ("activity.json", "activity_json")):
                payload = str(row[key]).encode("utf-8")
                path = session_root / f"{row['session_id']}.{suffix}"
                files.append(
                    {
                        "path": path.relative_to(output).as_posix(),
                        "sha256": _write_export_file(path, payload),
                    }
                )
            continuation = continuation_from_connection(connection, int(row["session_key"]))
            if continuation:
                payload = b"".join(
                    (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                        "utf-8"
                    )
                    for record in continuation
                )
                path = session_root / f"{row['session_id']}.continuation.jsonl"
                files.append(
                    {
                        "path": path.relative_to(output).as_posix(),
                        "sha256": _write_export_file(path, payload),
                    }
                )
            exported.append(
                {
                    "generation_id": generation,
                    "address": [row["project_id"] or None, row["agent_id"], row["session_id"]],
                    "status": row["status"],
                    "relative_directory": session_root.relative_to(output).as_posix(),
                    "message_count": len(messages),
                    "files": files,
                }
            )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "database": str(database),
        "created_at": _now(),
        "complete": True,
        "generations": exported,
        "session_count": len(exported),
    }
    _write_json(output / EXPORT_MANIFEST_NAME, manifest)
    print(f"export-jsonl: exported {len(exported)} generations to {output}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("inventory", cmd_inventory), ("dry-run", cmd_dry_run)):
        command = subcommands.add_parser(name)
        command.add_argument("--source", required=True)
        command.add_argument("--work-dir", required=True)
        command.set_defaults(handler=handler)
    command = subcommands.add_parser("convert")
    command.add_argument("--source", required=True)
    command.add_argument("--work-dir", required=True)
    command.set_defaults(handler=cmd_convert)
    command = subcommands.add_parser("verify")
    command.add_argument("--source", required=True)
    command.add_argument("--database", required=True)
    command.add_argument("--manifest", required=True)
    command.set_defaults(handler=cmd_verify)
    command = subcommands.add_parser("install")
    command.add_argument("--source", required=True)
    command.add_argument("--database", required=True)
    command.add_argument("--manifest", required=True)
    command.add_argument("--backup-dir", required=True)
    command.add_argument("--host", required=True)
    command.add_argument("--port", required=True, type=int)
    command.set_defaults(handler=cmd_install)
    command = subcommands.add_parser("resume")
    command.add_argument("--manifest", required=True)
    command.add_argument("--host", required=True)
    command.add_argument("--port", required=True, type=int)
    command.set_defaults(handler=cmd_resume)
    command = subcommands.add_parser("export-jsonl")
    command.add_argument("--database", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(handler=cmd_export_jsonl)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return cast(int, args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
