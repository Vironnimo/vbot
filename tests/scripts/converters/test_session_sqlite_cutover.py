"""session sqlite cutover coverage."""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest

import scripts.converters._session_sqlite_values as conversion_values
from core.sessions.format import MAINTENANCE_GUARD_FILE_NAME
from scripts.converters import session_sqlite
from scripts.converters.jsonl_sessions import capture_inventory
from tests.scripts.converters.session_sqlite_helpers import _write_transcript


def test_install_relocates_only_a_copy_and_publishes_marker_last(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "external-backup"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest = json.loads((work / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    monkeypatch.setattr(session_sqlite, "_server_is_stopped", lambda _host, _port: True)

    assert (
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(work / session_sqlite.MANIFEST_NAME),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )
        == 0
    )

    assert not transcript.exists()
    assert (source / "sessions.db").is_file()
    marker = json.loads((source / "session-store.json").read_text(encoding="utf-8"))
    assert marker["state"] == "ready"
    assert not (source / MAINTENANCE_GUARD_FILE_NAME).exists()
    assert list(backup.rglob("one.jsonl"))
    with sqlite3.connect(source / "sessions.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_install_refuses_a_reachable_target_before_mutating_source(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "external-backup"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    monkeypatch.setattr(session_sqlite, "_server_is_stopped", lambda _host, _port: False)

    with pytest.raises(RuntimeError, match="target is reachable"):
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )
    assert transcript.is_file()
    assert not (source / "sessions.db").exists()
    assert not backup.exists()


def test_server_stop_probe_rejects_any_tcp_listener_even_without_healthy_http() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        assert session_sqlite._server_is_stopped("127.0.0.1", port) is False


def test_source_relocation_checkpoints_progress_in_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    manifest_path = tmp_path / "manifest.json"
    artifacts: list[dict[str, object]] = []
    for index in range(3):
        relative = f"agents/coder/sessions/{index}.jsonl"
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"message-{index}".encode()
        path.write_bytes(data)
        artifacts.append(
            {
                "relative_path": relative,
                "present": True,
                "sha256": conversion_values._sha256_bytes(data),
                "size": len(data),
            }
        )
    manifest: dict[str, object] = {"sources": [{"artifacts": artifacts}]}
    writes = 0
    stop_checks = 0
    real_write_json = session_sqlite._write_json

    def count_write(path: Path, payload: dict[str, object]) -> None:
        nonlocal writes
        writes += 1
        real_write_json(path, payload)

    monkeypatch.setattr(session_sqlite, "_RELOCATION_CHECKPOINT_BATCH_SIZE", 2)
    monkeypatch.setattr(session_sqlite, "_write_json", count_write)

    def count_stop_check() -> None:
        nonlocal stop_checks
        stop_checks += 1

    session_sqlite._relocate_sources(
        source,
        backup,
        None,
        manifest_path,
        manifest,
        count_stop_check,
    )

    assert writes == 2
    assert stop_checks == 2
    relocated = manifest["relocated"]
    assert isinstance(relocated, list)
    assert len(relocated) == 3
    assert not list(source.rglob("*.jsonl"))
    assert len(list((backup / "relocated").rglob("*.jsonl"))) == 3


def test_external_evidence_captures_and_backs_up_every_discovered_regular_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "backup"
    sessions_dir = source / "agents" / "coder" / "sessions"
    accepted = sessions_dir / "accepted.jsonl"
    skipped = sessions_dir / "skipped.jsonl"
    skipped_metadata = sessions_dir / "skipped.meta.json"
    orphan = sessions_dir / "orphan.meta.json"
    unknown = sessions_dir / "notes.bin"
    _write_transcript(accepted)
    skipped.write_text("not-json\n", encoding="utf-8")
    skipped_metadata.write_text('{"title":"evidence"}', encoding="utf-8")
    orphan.write_text('{"title":"orphan"}', encoding="utf-8")
    unknown.write_bytes(b"unknown evidence")

    capture = capture_inventory(source)
    artifacts = {artifact.relative_path: artifact for artifact in capture.artifacts}
    expected_classifications = {
        "agents/coder/sessions/accepted.jsonl": "accepted_source",
        "agents/coder/sessions/skipped.jsonl": "skipped_session",
        "agents/coder/sessions/skipped.meta.json": "skipped_session",
        "agents/coder/sessions/orphan.meta.json": "orphan_sidecar",
        "agents/coder/sessions/notes.bin": "unknown_file",
    }
    assert {path: artifact.classification for path, artifact in artifacts.items()} == (
        expected_classifications
    )
    for artifact in artifacts.values():
        assert artifact.size == artifact.path.stat().st_size
        assert artifact.sha256 == conversion_values._sha256(artifact.path)

    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    monkeypatch.setattr(session_sqlite, "_server_is_stopped", lambda _host, _port: True)
    assert (
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )
        == 0
    )

    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_root = Path(completed["backup_dir"])
    backup_manifest = json.loads((backup_root / "backup-manifest.json").read_text("utf-8"))
    records = {record["relative_path"]: record for record in backup_manifest["files"]}
    assert set(records) == set(expected_classifications)
    assert records["agents/coder/sessions/accepted.jsonl"]["disposition"] == "relocate"
    assert all(
        records[path]["disposition"] == "preserve"
        for path in expected_classifications
        if path != "agents/coder/sessions/accepted.jsonl"
    )
    assert not accepted.exists()
    assert skipped.is_file()
    assert skipped_metadata.is_file()
    assert orphan.is_file()
    assert unknown.is_file()
    assert all((backup_root / "legacy" / path).is_file() for path in expected_classifications)


def test_relocation_uses_atomic_copy_publish_without_cross_filesystem_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    manifest_path = tmp_path / "manifest.json"
    relative = "agents/coder/sessions/one.jsonl"
    original = source / relative
    original.parent.mkdir(parents=True)
    data = b"legacy"
    original.write_bytes(data)
    artifact = {
        "relative_path": relative,
        "present": True,
        "sha256": conversion_values._sha256_bytes(data),
        "size": len(data),
    }
    manifest: dict[str, object] = {"sources": [{"artifacts": [artifact]}]}
    real_replace = session_sqlite.os.replace

    def reject_direct_source_rename(source_path: Path, destination_path: Path) -> None:
        if Path(source_path) == original:
            raise AssertionError("relocation attempted a cross-filesystem source rename")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(session_sqlite.os, "replace", reject_direct_source_rename)
    session_sqlite._relocate_sources(source, backup, None, manifest_path, manifest, lambda: None)

    destination = backup / "relocated" / relative
    assert not original.exists()
    assert destination.read_bytes() == data
    assert manifest["relocated"] == [relative]


@pytest.mark.parametrize("resume_state", ["both_exist", "destination_only"])
def test_relocation_reconciles_published_destination_resume_states(
    tmp_path: Path, resume_state: str
) -> None:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    manifest_path = tmp_path / "manifest.json"
    relative = "agents/coder/sessions/one.jsonl"
    original = source / relative
    destination = backup / "relocated" / relative
    data = b"legacy"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(data)
    if resume_state == "both_exist":
        original.parent.mkdir(parents=True)
        original.write_bytes(data)
    artifact = {
        "relative_path": relative,
        "present": True,
        "sha256": conversion_values._sha256_bytes(data),
        "size": len(data),
    }
    manifest: dict[str, object] = {"sources": [{"artifacts": [artifact]}]}

    session_sqlite._relocate_sources(source, backup, None, manifest_path, manifest, lambda: None)

    assert not original.exists()
    assert destination.read_bytes() == data
    assert manifest["relocated"] == [relative]


def test_install_rejects_captured_stage_before_staged_database_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "backup"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript)
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    staged.unlink()
    manifest["stage"] = "captured"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        session_sqlite,
        "_server_is_stopped",
        lambda _host, _port: pytest.fail("captured install reached install preflight"),
    )

    with pytest.raises(RuntimeError, match="resume conversion before install"):
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )


def test_install_reports_missing_staged_database_actionably(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "backup"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript)
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    staged.unlink()

    with pytest.raises(RuntimeError, match="staged database is missing.*rerun convert"):
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )


def test_resume_requires_host_and_port_only_for_install_stages(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript)
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]

    with pytest.raises(SystemExit, match="requires both --host and --port"):
        session_sqlite.main(["resume", "--manifest", str(manifest_path)])

    staged.unlink()
    manifest["stage"] = "captured"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert session_sqlite.main(["resume", "--manifest", str(manifest_path)]) == 0
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["stage"] == "converted"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stage"] = "complete"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert session_sqlite.main(["resume", "--manifest", str(manifest_path)]) == 0


def test_resume_reconciles_after_source_relocation_boundary(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "external-backup"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0

    def interrupt(stage: str, boundary: str) -> None:
        if stage == "database_publishing" and boundary == "before":
            raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(session_sqlite, "_server_is_stopped", lambda _host, _port: True)
    monkeypatch.setattr(session_sqlite, "_TRANSITION_HOOK", interrupt)
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )
    interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert interrupted["stage"] == "sources_relocating"
    assert not transcript.exists()
    assert (source / MAINTENANCE_GUARD_FILE_NAME).is_file()

    monkeypatch.setattr(session_sqlite, "_TRANSITION_HOOK", None)
    assert (
        session_sqlite.main(
            [
                "resume",
                "--manifest",
                str(manifest_path),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )
        == 0
    )
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["stage"] == "complete"
    assert not (source / MAINTENANCE_GUARD_FILE_NAME).exists()
    assert (source / "sessions.db").is_file()
    assert (
        json.loads((source / "session-store.json").read_text(encoding="utf-8"))["state"] == "ready"
    )


@pytest.mark.parametrize(
    ("interrupted_stage", "interrupted_boundary"),
    [
        (stage, boundary)
        for stage in (
            "install_preflight",
            "backup_publishing",
            "sources_relocating",
            "database_publishing",
            "marker_publishing",
            "runtime_verifying",
            "complete",
        )
        for boundary in ("before", "after")
    ],
)
def test_install_resume_reconciles_every_state_boundary(
    tmp_path: Path, monkeypatch, interrupted_stage: str, interrupted_boundary: str
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    backup = tmp_path / "external-backup"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0

    def interrupt(stage: str, boundary: str) -> None:
        if stage == interrupted_stage and boundary == interrupted_boundary:
            raise RuntimeError(f"simulated interruption at {stage}")

    monkeypatch.setattr(session_sqlite, "_server_is_stopped", lambda _host, _port: True)
    monkeypatch.setattr(session_sqlite, "_TRANSITION_HOOK", interrupt)
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    with pytest.raises(RuntimeError, match=f"simulated interruption at {interrupted_stage}"):
        session_sqlite.main(
            [
                "install",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
                "--backup-dir",
                str(backup),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )

    monkeypatch.setattr(session_sqlite, "_TRANSITION_HOOK", None)
    assert (
        session_sqlite.main(
            [
                "resume",
                "--manifest",
                str(manifest_path),
                "--host",
                "127.0.0.1",
                "--port",
                "65530",
            ]
        )
        == 0
    )
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["stage"] == "complete"
    assert not transcript.exists()
    assert (source / "sessions.db").is_file()
    assert (
        json.loads((source / "session-store.json").read_text(encoding="utf-8"))["state"] == "ready"
    )
    assert list(backup.rglob("one.jsonl"))
