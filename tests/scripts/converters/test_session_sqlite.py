"""session sqlite main coverage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scripts.converters._session_sqlite_values as conversion_values
from core.chat import ChatMessage
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.store import SessionStore
from scripts.converters import session_sqlite
from scripts.converters.jsonl_sessions import capture_inventory, inventory
from tests.scripts.converters.session_sqlite_helpers import _write_transcript


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
    assert capture.sessions[0].ignored_tails[0].sha256 == conversion_values._sha256_bytes(torn)
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


def test_fsync_file_flushes_on_the_current_platform(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"durable")
    calls: list[int] = []
    real_fsync = session_sqlite.os.fsync

    def observe(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(session_sqlite.os, "fsync", observe)
    conversion_values._fsync_file(artifact)
    assert len(calls) == 1


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
