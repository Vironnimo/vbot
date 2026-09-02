"""Canonical offline converter discovery contracts."""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.format import MAINTENANCE_GUARD_FILE_NAME
from core.sessions.store import SessionStore
from scripts.converters import session_sqlite
from scripts.converters.jsonl_sessions import capture_inventory, inventory


def _write_transcript(path: Path, content: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ChatMessage.user(content).to_dict()) + "\n", encoding="utf-8")


def test_inventory_maps_archived_session_roots_to_their_project_and_agent(
    tmp_path: Path,
) -> None:
    roots = (
        tmp_path
        / "archive"
        / "sessions"
        / "projects"
        / "project-a"
        / "agents"
        / "agent-a"
        / "session-a.jsonl",
        tmp_path
        / "archive"
        / "projects"
        / "project-b"
        / "agents"
        / "agent-b"
        / "sessions"
        / "session-b.jsonl",
    )
    for index, root in enumerate(roots):
        _write_transcript(root, f"message-{index}")

    sessions = inventory(tmp_path)

    assert {session.address for session in sessions} == {
        SessionAddress("project-a", "agent-a", "session-a"),
        SessionAddress("project-b", "agent-b", "session-b"),
    }


@pytest.mark.parametrize(
    ("relative", "address", "archived"),
    [
        ("agents/identity/sessions/live.jsonl", SessionAddress(None, "identity", "live"), False),
        (
            "projects/project/agents/project-agent/sessions/live.jsonl",
            SessionAddress("project", "project-agent", "live"),
            False,
        ),
        (
            "archive/sessions/agents/identity/old.jsonl",
            SessionAddress(None, "identity", "old"),
            True,
        ),
        (
            "archive/sessions/projects/project/agents/project-agent/old.jsonl",
            SessionAddress("project", "project-agent", "old"),
            True,
        ),
        (
            "archive/agents/identity/agent/sessions/older.jsonl",
            SessionAddress(None, "identity", "older"),
            True,
        ),
        (
            "archive/projects/project/agents/project-agent/sessions/older.jsonl",
            SessionAddress("project", "project-agent", "older"),
            True,
        ),
    ],
)
def test_inventory_maps_every_legacy_root(
    tmp_path: Path, relative: str, address: SessionAddress, archived: bool
) -> None:
    transcript = tmp_path / relative
    _write_transcript(transcript, relative)

    sessions = inventory(tmp_path)

    assert [(session.address, session.archived) for session in sessions] == [(address, archived)]


def test_copied_rehearsal_converts_all_roots_and_reopens_current_database(
    tmp_path: Path,
) -> None:
    source = tmp_path / "copied-source"
    work = tmp_path / "copied-work"
    relative_paths = (
        "agents/identity/sessions/live.jsonl",
        "projects/project/agents/project-agent/sessions/live.jsonl",
        "archive/sessions/agents/identity/old.jsonl",
        "archive/sessions/projects/project/agents/project-agent/old.jsonl",
        "archive/agents/identity/agent/sessions/older.jsonl",
        "archive/projects/project/agents/project-agent/sessions/older.jsonl",
    )
    for index, relative in enumerate(relative_paths):
        transcript = source / relative
        _write_transcript(transcript, f"rehearsal-{index}-one")
        transcript.write_text(
            transcript.read_text(encoding="utf-8")
            + json.dumps(ChatMessage.user(f"rehearsal-{index}-two").to_dict())
            + "\n",
            encoding="utf-8",
        )
    torn = source / relative_paths[-1]
    with torn.open("ab") as handle:
        handle.write(b'{"role":"user","content":"\xe2')

    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]

    assert manifest["count"] == 6
    assert manifest["evidence"]["rejected_paths"] == []
    assert any(source["ignored_tails"] for source in manifest["sources"])
    assert (
        session_sqlite.main(
            [
                "verify",
                "--source",
                str(source),
                "--database",
                str(staged),
                "--manifest",
                str(manifest_path),
            ]
        )
        == 0
    )

    store = SessionStore(staged, _offline=True)
    sessions = ChatSessionManager(source, store=store)
    try:
        with sqlite3.connect(staged) as connection:
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 6
            assert (
                connection.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == 12
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM messages_fts_trigram_docsize").fetchone()[
                    0
                ]
                == 12
            )
        assert store.fts_health().state == "healthy"
        assert sessions.get(SessionAddress(None, "identity", "live")).load()[0].content == (
            "rehearsal-0-one"
        )
    finally:
        store.close()


def test_capture_reads_torn_utf8_tail_once_and_records_evidence(tmp_path: Path) -> None:
    transcript = tmp_path / "agents" / "coder" / "sessions" / "one.jsonl"
    transcript.parent.mkdir(parents=True)
    complete = json.dumps(ChatMessage.user("complete").to_dict()).encode("utf-8") + b"\n"
    torn = b'{"role":"user","content":"\xe2\x82'
    transcript.write_bytes(complete + torn)

    capture = capture_inventory(tmp_path)

    assert len(capture.sessions) == 1
    assert capture.sessions[0].messages[0].content == "complete"
    assert capture.sessions[0].ignored_tails[0].size == len(torn)
    assert capture.sessions[0].ignored_tails[0].sha256 == session_sqlite._sha256_bytes(torn)
    assert all(artifact.data == b"" for artifact in capture.sessions[0].captured_artifacts)


def test_capture_skips_one_malformed_session_without_losing_valid_sessions(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "agents" / "coder" / "sessions" / "valid.jsonl"
    malformed = tmp_path / "agents" / "coder" / "sessions" / "malformed.jsonl"
    _write_transcript(valid, "keep me")
    malformed.write_text("not-json\n", encoding="utf-8")

    capture = capture_inventory(tmp_path)

    assert [session.address.session_id for session in capture.sessions] == ["valid"]
    assert capture.skipped_sessions == (
        {
            "relative_path": "agents/coder/sessions/malformed.jsonl",
            "reason": f"invalid legacy message record: {malformed}:1",
        },
    )


def test_capture_skips_message_validation_errors_without_losing_valid_sessions(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "agents" / "coder" / "sessions" / "valid.jsonl"
    invalid = tmp_path / "agents" / "coder" / "sessions" / "invalid.jsonl"
    _write_transcript(valid, "keep me")
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text(
        json.dumps(
            {
                "id": "invalid-message",
                "role": "user",
                "timestamp": "2026-09-02T12:00:00Z",
                "content": [{"type": "unknown", "text": "broken"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    capture = capture_inventory(tmp_path)

    assert [session.address.session_id for session in capture.sessions] == ["valid"]
    assert capture.skipped_sessions == (
        {
            "relative_path": "agents/coder/sessions/invalid.jsonl",
            "reason": f"invalid legacy Session message: {invalid}",
        },
    )


def test_converter_preserves_legacy_file_mention_blocks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    transcript = source / "agents" / "coder" / "sessions" / "mention.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    message = ChatMessage.user(
        [
            TextBlock(type="text", text="Review this file"),
            FileMentionBlock(
                type="file_mention",
                path="src/app.py",
                status="inlined",
                text="print('hello')",
                size_bytes=14,
            ),
        ]
    )
    transcript.write_text(json.dumps(message.to_dict()) + "\n", encoding="utf-8")

    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest = json.loads((work / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))
    store = SessionStore(work / manifest["staged_db"], _offline=True)
    sessions = ChatSessionManager(source, store=store)
    try:
        loaded = sessions.get(SessionAddress(None, "coder", "mention")).load()
    finally:
        store.close()

    assert loaded == [message]


def test_convert_is_deterministic_and_preserves_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work_a = tmp_path / "work-a"
    work_b = tmp_path / "work-b"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    transcript.with_name("one.meta.json").write_text('{"title":"one"}', encoding="utf-8")

    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work_a)]) == 0
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work_b)]) == 0

    manifest_a = json.loads((work_a / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest_b = json.loads((work_b / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest_a["sources"] == manifest_b["sources"]
    assert manifest_a["sources"][0]["generation_id"]
    assert transcript.exists()
    assert not (source / "sessions.db").exists()


def test_convert_preserves_session_activity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    activity = {
        "latest_completion": {
            "run_id": "run-current",
            "status": "completed",
            "timestamp": "2026-08-31T12:00:00+00:00",
        },
        "read_run_id": "run-previous",
    }
    transcript.with_name("one.activity.json").write_text(json.dumps(activity), encoding="utf-8")

    assert session_sqlite.MANIFEST_VERSION == 1
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest = json.loads((work / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]

    store = SessionStore(staged, _offline=True)
    sessions = ChatSessionManager(source, store=store)
    try:
        address = SessionAddress(None, "coder", "one")
        assert store.activity(address) == activity
        assert sessions.list_completion_activity("coder") == [
            {
                "id": "one",
                "latest_completion_run_id": "run-current",
                "has_unread_completion": True,
                "unread_run_id": "run-current",
                "unread_run_status": "completed",
                "unread_run_at": "2026-08-31T12:00:00+00:00",
            }
        ]
    finally:
        store.close()


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


def test_fsync_file_flushes_on_the_current_platform(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"durable")
    calls: list[int] = []
    real_fsync = session_sqlite.os.fsync

    def observe(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(session_sqlite.os, "fsync", observe)
    session_sqlite._fsync_file(artifact)
    assert len(calls) == 1


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
                "sha256": session_sqlite._sha256_bytes(data),
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


def test_export_is_generation_collision_safe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    output = tmp_path / "export"
    transcript = source / "agents" / "coder" / "sessions" / "one.jsonl"
    _write_transcript(transcript, "hello")
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest = json.loads((work / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))

    assert (
        session_sqlite.main(
            [
                "export-jsonl",
                "--database",
                str(work / manifest["staged_db"]),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    export_manifest = json.loads(
        (output / session_sqlite.EXPORT_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert export_manifest["complete"] is True
    assert export_manifest["session_count"] == 1
    assert len(list(output.rglob("*.jsonl"))) == 1


def test_export_reconstructs_projected_and_residual_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    output = tmp_path / "export"
    transcript = source / "agents" / "coder" / "sessions" / "metadata.jsonl"
    _write_transcript(transcript)
    metadata = {
        "title": "Projected title",
        "auto_title": "Projected auto title",
        "source_channel_id": "channel-1",
        "platform": "telegram",
        "platform_conv_id": "conversation-1",
        "is_subagent_session": True,
        "subagent_parent": {"session_id": "parent"},
        "fork_source": {"session_id": "source"},
        "run_kinds": ["user", "subagent"],
        "compaction_policy": {"strategy": "summary"},
        "residual": {"nested": [1, 2, 3]},
    }
    transcript.with_name("metadata.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest = json.loads((work / session_sqlite.MANIFEST_NAME).read_text(encoding="utf-8"))

    assert (
        session_sqlite.main(
            [
                "export-jsonl",
                "--database",
                str(work / manifest["staged_db"]),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    exported_metadata = json.loads(next(output.rglob("metadata.meta.json")).read_text("utf-8"))
    assert exported_metadata == metadata


def test_verify_checks_every_generation_semantic_and_manifest_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    transcript = source / "agents" / "coder" / "sessions" / "semantic.jsonl"
    _write_transcript(transcript, "canonical")
    transcript.with_name("semantic.meta.json").write_text(
        json.dumps({"title": "Projected", "residual": {"keep": True}}), encoding="utf-8"
    )
    transcript.with_name("semantic.activity.json").write_text(
        json.dumps({"latest_completion": {"run_id": "run-1"}}), encoding="utf-8"
    )
    continuation = (
        {
            "version": 1,
            "type": "run_started",
            "run_id": "run-1",
            "origin_run_id": "run-1",
            "checkpoint_id": "checkpoint-1",
            "timestamp": "2026-09-02T12:00:00Z",
            "request": {"prompt": "continue"},
        },
        {
            "version": 1,
            "type": "stream_delta",
            "run_id": "run-1",
            "step": 0,
            "timestamp": "2026-09-02T12:00:01Z",
            "content_delta": "partial",
        },
        {
            "version": 1,
            "type": "tool_result",
            "run_id": "run-1",
            "tool_call_id": "call-1",
            "name": "read_file",
            "timestamp": "2026-09-02T12:00:02Z",
            "ok": True,
        },
        {
            "version": 1,
            "type": "run_interrupted",
            "run_id": "run-1",
            "timestamp": "2026-09-02T12:00:03Z",
            "cause": "internal",
        },
    )
    transcript.with_name("semantic.continuation.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in continuation), encoding="utf-8"
    )
    assert session_sqlite.main(["convert", "--source", str(source), "--work-dir", str(work)]) == 0
    manifest_path = work / session_sqlite.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    staged = work / manifest["staged_db"]
    verify_args = [
        "verify",
        "--source",
        str(source),
        "--database",
        str(staged),
        "--manifest",
        str(manifest_path),
    ]
    assert session_sqlite.main(verify_args) == 0
    original_database = staged.read_bytes()
    corruptions = (
        ("address", "UPDATE sessions SET session_id = 'changed'"),
        ("lifecycle", "UPDATE sessions SET status = 'archived'"),
        ("Messages", "UPDATE messages SET content = 'changed'"),
        ("metadata", "UPDATE sessions SET title = 'changed'"),
        ("activity", "UPDATE sessions SET activity_json = '{}'"),
        ("Continuation", "UPDATE continuation_steps SET content = 'changed'"),
    )
    for mismatch, statement in corruptions:
        staged.write_bytes(original_database)
        with sqlite3.connect(staged) as connection:
            connection.execute(statement)
            connection.commit()
        with pytest.raises(RuntimeError, match=mismatch):
            session_sqlite.main(verify_args)

    staged.write_bytes(original_database)
    manifest["database"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="database hash mismatch"):
        session_sqlite.main(verify_args)


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
        assert artifact.sha256 == session_sqlite._sha256(artifact.path)

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
        "sha256": session_sqlite._sha256_bytes(data),
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
        "sha256": session_sqlite._sha256_bytes(data),
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
