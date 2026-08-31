"""Canonical offline converter discovery contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import SessionAddress
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
    assert list(backup.rglob("one.jsonl"))
    with sqlite3.connect(source / "sessions.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


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
    assert (source / "sessions.db").is_file()
    assert (
        json.loads((source / "session-store.json").read_text(encoding="utf-8"))["state"] == "ready"
    )
