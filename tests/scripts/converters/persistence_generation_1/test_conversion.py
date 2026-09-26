"""End-to-end tests of the Generation 1 conversion entry point: preflight, run, verify, install."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    DatabaseError,
    open_database,
    read_maintenance,
    read_marker,
)
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.store import SessionStore
from core.settings import validate_data_dir_config
from core.utils.server_control import server_control_claim
from scripts.converters.persistence_generation_1 import _install, conversion, sessions
from scripts.converters.persistence_generation_1.__main__ import main
from scripts.converters.persistence_generation_1._context import (
    ConversionContext,
    ConversionError,
)
from scripts.converters.persistence_generation_1._install import InstallError
from scripts.converters.persistence_generation_1._preflight import RefusedError
from scripts.converters.persistence_generation_1._verify import database_specs
from scripts.converters.persistence_generation_1.conversion import (
    AREAS,
    Area,
    ConversionFailedError,
    convert_data_directory,
)
from tests.scripts.converters.persistence_generation_1.legacy_schema_support import (
    LEGACY_DECISIONS_DDL,
    LEGACY_SWARM_DDL,
    create_legacy_database,
)
from tests.scripts.converters.persistence_generation_1.legacy_sessions_support import (
    LegacySessionStore,
)

BASE = SessionAddress(project_id=None, agent_id="main", session_id="base")
BRANCH = SessionAddress(project_id=None, agent_id="main", session_id="branch")
_TIMESTAMP = "2026-06-18T10:00:00Z"
_LOCK_FILE = "data-store.lock"
# A saved MCP result that no Tool Result returned: dropped and reported.
_MCP_RESULT = "mcp/content/results/res_000000000001.json"
_RENAMED_COUNTERS = {
    "turns_since_memory_review": 1,
    "model_steps_since_skill_review": 3,
    "generation": 0,
}
_DROPPED_COUNTERS = {"turns_since_memory_review": 2, "tool_calls_since_skill_review": 5}


def _write(root: Path, relative: str, value: Any) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def _legacy_data_dir(root: Path) -> dict[str, list[str]]:
    """A pre-Generation-1 data directory with every converted area; the Session history ids."""
    _write(root, "settings.json", {"server_port": 8500})
    _write(
        root,
        "agents/main/agent.json",
        {
            "id": "main",
            "name": "Main",
            "model": "",
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
            "tool_access": {"mode": "selected", "allowed": ["read", "grep", "glob"]},
        },
    )
    _write(root, "projects/p/project.json", {"project_id": "p", "display_name": "P", "cwd": "/p"})
    _write(
        root,
        "channels/tg/channel.json",
        {
            "id": "tg",
            "platform": "telegram",
            "agent_id": "main",
            "token_env_var": "TELEGRAM_BOT_TOKEN",
            "enabled": True,
        },
    )
    _write(root, "channels/tg/polling.json", {"version": 1, "last_update_id": 41})
    _write(root, "cron/jobs.json", [])
    _write(root, "mcp/connections.json", [{"id": "docs", "transport": "stdio", "command": "x"}])
    _write(root, _MCP_RESULT, {"owner": {"agent_id": "main"}, "payload": {"rows": [1]}})
    _write(root, "statistics/provider-usage/2026-09.jsonl", "not a sample\n")
    _write(root, "session-store.json", {"format_version": 1, "state": "ready"})
    _write(root, "session-snapshot-health.json", {"state": "healthy"})
    _write(root, "session-snapshots/20260924T212952Z/manifest.json", {})
    create_legacy_database(root / "decisions.db", LEGACY_DECISIONS_DDL)
    create_legacy_database(root / "extension-data/swarm/swarm.db", LEGACY_SWARM_DDL)
    with LegacySessionStore(root / "sessions.db") as legacy:
        base = legacy.session("base", minute=0)
        legacy.start_run(base, "run_1", minute=1)
        history = [
            legacy.user(base, "zebra question", minute=1, run_id="run_1"),
            # Both old Assistant Message shapes: a whole-turn-only estimate and a
            # line-only output-file reference.
            legacy.assistant(
                base,
                "An answer.\nanswer.md",
                minute=2,
                run_id="run_1",
                usage={"input_tokens": 12, "output_tokens": 3, "estimated": True},
                output_files=[{"path": "answer.md", "line_index": 1}],
            ),
        ]
        history.append(legacy.finish_run(base, "run_1", minute=3))
        branch = legacy.fork(base, "branch", minute=4)
        branch_history = [*history, legacy.user(branch, "a follow-up", minute=5)]
        # Retired Reflection counters: one renamed, and one dropped and reported. The
        # fork's report item is about metadata, so it explains no history difference.
        legacy.mutate_metadata(
            base, lambda metadata: metadata.update(reflection_counters=_RENAMED_COUNTERS)
        )
        legacy.mutate_metadata(
            branch, lambda metadata: metadata.update(reflection_counters=_DROPPED_COUNTERS)
        )
        # The source goes on after the fork; the fork keeps its view.
        history.append(legacy.user(base, "later", minute=6))
        history.append(legacy.note(base, "a note", minute=7))
    # Journal files that must never meet the new databases.
    (root / "sessions.db-wal").write_bytes(b"")
    (root / "channels.db-journal").write_bytes(b"")
    return {"base": history, "branch": branch_history}


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@contextmanager
def _sessions(data_dir: Path) -> Iterator[ChatSessionManager]:
    store = SessionStore(data_dir / "sessions.db")
    manager = ChatSessionManager(data_dir, store=store)
    try:
        yield manager
    finally:
        manager.close()
        store.close()


def _ids(manager: ChatSessionManager, address: SessionAddress) -> list[str]:
    return [message.id for message in manager.get(address).load_active()]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


def test_dry_run_verifies_everything_and_leaves_the_data_directory_unchanged(
    data_dir: Path,
) -> None:
    _legacy_data_dir(data_dir)
    before = _files(data_dir)

    report = convert_data_directory(data_dir, dry_run=True)

    after = _files(data_dir)
    after.pop(_LOCK_FILE, None)
    assert after == before
    assert report["result"] == "verified"
    assert report["areas"]["sessions"]["sessions"] == 2
    assert report["areas"]["sessions"]["usage_provenance_derived"] == 1
    assert report["areas"]["sessions"]["output_file_spans_derived"] == 1
    assert report["areas"]["sessions"]["reflection_counter_renamed"] == 1
    assert report["areas"]["sessions"]["reflection_counter_dropped"] == 1
    verification = report["verification"]
    assert set(verification["databases"]) == {
        "sessions",
        "decisions",
        "provider_usage",
        "channels",
        "ext.swarm.swarm",
    }
    assert verification["sessions"]["sessions_compared"] == 2
    assert verification["sessions"]["explained_differences"] == 0
    assert len(verification["sessions"]["loaded_through_the_application"]) == 2
    assert verification["json_documents"]["documents_with_errors"] == 0
    assert "sessions.db-wal" in report["install"]["moved_aside"]
    # The Telegram polling watermark names no bot and is dropped; the retired
    # grep and glob of the Agent are replaced; the retired Tool-call counter is
    # dropped.
    assert report["skipped_by_area"] == {
        "json_documents": 1,
        "provider_usage": 1,
        "mcp": 1,
        "channels": 1,
        "sessions": 1,
    }
    [counter] = [item for item in report["skipped"] if item["area"] == "sessions"]
    assert counter["changes_history"] is False
    assert report["areas"]["json_documents"]["retired_tool_names_converted"] == 1
    assert report["sizes"]["installed_bytes"] > 0
    assert read_maintenance(data_dir) is None


def test_install_registers_every_database_and_moves_replaced_files_aside(
    data_dir: Path,
) -> None:
    histories = _legacy_data_dir(data_dir)
    before = _files(data_dir)

    report = convert_data_directory(data_dir)

    assert report["result"] == "installed"
    backup = data_dir / "pre-generation-1"
    for relative in (
        "sessions.db",
        "sessions.db-wal",
        "channels.db-journal",
        "decisions.db",
        "extension-data/swarm/swarm.db",
        "session-store.json",
        "session-snapshot-health.json",
        "session-snapshots/20260924T212952Z/manifest.json",
        "agents/main/agent.json",
        "channels/tg/channel.json",
        "channels/tg/polling.json",
        "statistics/provider-usage/2026-09.jsonl",
        "mcp/connections.json",
        _MCP_RESULT,
    ):
        assert (backup / relative).read_bytes() == before[relative], relative
    for relative in (
        "sessions.db-wal",
        "channels.db-journal",
        "session-store.json",
        "mcp/connections.json",
        _MCP_RESULT,
    ):
        assert not (data_dir / relative).exists()
    assert not (data_dir / "generation-1-staging").exists()
    assert read_maintenance(data_dir) is None
    saved = json.loads((backup / "conversion-report.json").read_text(encoding="utf-8"))
    assert saved["result"] == "installed"

    marker = read_marker(data_dir)
    assert marker is not None
    assert set(marker.databases) == set(report["verification"]["databases"])
    for spec in database_specs(data_dir).values():
        open_database(spec).close()
    with _sessions(data_dir) as manager:
        assert _ids(manager, BASE) == histories["base"]
        assert _ids(manager, BRANCH) == histories["branch"]
        assert manager.get_metadata(BASE)["reflection_counters"] == {
            "turns_since_memory_review": 1,
            "iterations_since_skill_review": 3,
            "generation": 0,
        }
        assert manager.get_metadata(BRANCH)["reflection_counters"] == {
            "turns_since_memory_review": 2
        }
    assert all(report.ok for report in validate_data_dir_config(data_dir))
    agent = json.loads((data_dir / "agents/main/agent.json").read_text(encoding="utf-8"))
    assert agent["tool_access"]["allowed"] == ["read", "search_files"]
    connections = data_dir / "extension-data/mcp/connections.json"
    assert json.loads(connections.read_text(encoding="utf-8"))["connections"][0]["id"] == "docs"


@pytest.mark.parametrize("mode", [0o600, 0o400])
@pytest.mark.parametrize("dry_run", [False, True])
def test_conversion_preserves_oauth_and_relocated_document_permissions(
    data_dir: Path, mode: int, dry_run: bool
) -> None:
    _write(data_dir, "settings.json", {})
    documents = {
        "oauth/github-copilot-oauth.json": {"access_token": "secret-token"},
        "mcp/connections.json": [],
    }
    originals = {}
    for relative, content in documents.items():
        _write(data_dir, relative, content)
        path = data_dir / relative
        path.chmod(mode)
        originals[relative] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))

    report = convert_data_directory(data_dir, dry_run=dry_run)

    assert report["result"] == ("verified" if dry_run else "installed")
    assert not (data_dir / "generation-1-staging").exists()
    assert read_maintenance(data_dir) is None
    for relative, (original_bytes, original_mode) in originals.items():
        if dry_run:
            untouched = data_dir / relative
        else:
            untouched = data_dir / "pre-generation-1" / relative
            target = f"extension-data/{relative}" if relative.startswith("mcp/") else relative
            installed = data_dir / target
            assert stat.S_IMODE(installed.stat().st_mode) == original_mode
            assert json.loads(installed.read_text(encoding="utf-8"))["format_version"] == 1
        assert untouched.read_bytes() == original_bytes
        assert stat.S_IMODE(untouched.stat().st_mode) == original_mode


def test_the_cli_prints_a_summary_and_writes_the_report(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_data_dir(data_dir)
    report_path = tmp_path / "report.json"

    assert main([str(data_dir), "--dry-run", "--report", str(report_path)]) == 0

    assert json.loads(report_path.read_text(encoding="utf-8"))["result"] == "verified"
    assert str(data_dir) in capsys.readouterr().out


def test_a_running_server_is_refused(data_dir: Path) -> None:
    _legacy_data_dir(data_dir)
    before = _files(data_dir)

    with server_control_claim(data_dir, 8420), pytest.raises(RefusedError, match="8420"):
        convert_data_directory(data_dir)

    after = _files(data_dir)
    assert {name for name in after if name.startswith("runtime/")} == {"runtime/server-8420.lock"}
    assert {name: value for name, value in after.items() if not name.startswith("runtime/")} == (
        before
    )


def test_an_already_converted_data_directory_is_refused(data_dir: Path) -> None:
    _legacy_data_dir(data_dir)
    convert_data_directory(data_dir)

    with pytest.raises(RefusedError, match="already Generation 1"):
        convert_data_directory(data_dir, dry_run=True)


def test_an_unsupported_source_is_refused_before_anything_changes(data_dir: Path) -> None:
    _legacy_data_dir(data_dir)
    with closing(sqlite3.connect(data_dir / "decisions.db")) as connection:
        connection.execute("PRAGMA application_id = 42")
    before = _files(data_dir)

    with pytest.raises(RefusedError, match="unsupported source"):
        convert_data_directory(data_dir)

    assert _files(data_dir) == before


def test_a_failing_area_leaves_the_source_untouched_and_releases_the_guard(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_data_dir(data_dir)
    before = _files(data_dir)

    def fail(context: ConversionContext) -> None:
        raise ConversionError("broken source")

    monkeypatch.setattr(conversion, "AREAS", (*AREAS, Area("broken", fail)))

    with pytest.raises(ConversionFailedError, match="broken source"):
        convert_data_directory(data_dir)

    after = _files(data_dir)
    after.pop(_LOCK_FILE, None)
    assert after == before
    assert read_maintenance(data_dir) is None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE entries SET superseded_at_seq = seq WHERE entry_id = :last",
        "DELETE FROM sessions WHERE session_id = 'branch'",
    ],
    ids=["an entry hidden", "the Session missing"],
)
def test_a_session_whose_history_changed_unreported_fails_verification(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    histories = _legacy_data_dir(data_dir)

    def change_the_branch(context: ConversionContext) -> None:
        sessions.convert(context)
        # The fork has a report item, but one about its metadata.
        assert [
            item.changes_history
            for item in context.report.skipped
            if item.area == sessions.AREA and "/main/branch " in item.item
        ] == [False]
        with closing(sqlite3.connect(context.staging / "sessions.db")) as connection:
            connection.execute(change, {"last": histories["branch"][-1]})
            connection.commit()

    areas = tuple(
        Area(area.name, change_the_branch) if area.name == sessions.AREA else area for area in AREAS
    )
    monkeypatch.setattr(conversion, "AREAS", areas)

    with pytest.raises(ConversionFailedError, match="differ from their source history"):
        convert_data_directory(data_dir, dry_run=True)


def test_a_reported_history_change_explains_the_difference(data_dir: Path) -> None:
    histories = _legacy_data_dir(data_dir)
    # The source's last Message, a note written after the fork, lost its content:
    # the converter drops it.
    with closing(sqlite3.connect(data_dir / "sessions.db")) as connection:
        connection.execute(
            "UPDATE messages SET content = NULL WHERE message_id = ?", (histories["base"][-1],)
        )
        connection.commit()

    report = convert_data_directory(data_dir, dry_run=True)

    [dropped] = [
        item
        for item in report["skipped"]
        if item["area"] == "sessions" and histories["base"][-1] in item["reason"]
    ]
    assert dropped["changes_history"] is True
    assert "/main/base " in dropped["item"]
    check = report["verification"]["sessions"]
    assert check["explained_differences"] == 1
    assert check["explained_examples"][0].startswith(dropped["item"])


def test_sources_changed_during_the_conversion_are_not_installed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_data_dir(data_dir)

    def touch_a_retired_file(context: ConversionContext) -> None:
        polling = data_dir / "channels/tg/polling.json"
        polling.write_text('{"version": 1, "last_update_id": 42}', encoding="utf-8")

    monkeypatch.setattr(conversion, "AREAS", (*AREAS, Area("writer", touch_a_retired_file)))

    with pytest.raises(ConversionFailedError, match="changed while they were converted"):
        convert_data_directory(data_dir)

    assert not (data_dir / "pre-generation-1").exists()
    assert read_marker(data_dir) is None
    assert read_maintenance(data_dir) is None


@pytest.mark.parametrize("phase", ["moving files aside", "installing staged files"])
def test_an_interrupted_install_keeps_vbot_out_and_finishes_when_run_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    histories = _legacy_data_dir(data_dir)
    moved_aside = len(convert_data_directory(data_dir, dry_run=True)["install"]["moved_aside"])
    failing_move = 3 if phase == "moving files aside" else moved_aside + 3
    moves = 0
    real_move = _install._move

    def move_then_fail(source: Path, target: Path, relative: str) -> None:
        nonlocal moves
        moves += 1
        if moves == failing_move:
            raise InstallError(f"{relative} could not be moved: simulated crash")
        real_move(source, target, relative)

    monkeypatch.setattr(_install, "_move", move_then_fail)
    with pytest.raises(InstallError, match="run again"):
        convert_data_directory(data_dir)

    assert read_maintenance(data_dir) is not None
    with pytest.raises(DatabaseError):
        open_database(database_specs(data_dir)["sessions"])
    with pytest.raises(RefusedError, match="interrupted install"):
        convert_data_directory(data_dir, dry_run=True)

    monkeypatch.setattr(_install, "_move", real_move)
    report = convert_data_directory(data_dir)

    assert report["result"] == "installed"
    assert report["resumed"] is True
    assert read_maintenance(data_dir) is None
    assert not (data_dir / "generation-1-staging").exists()
    with _sessions(data_dir) as manager:
        assert _ids(manager, BRANCH) == histories["branch"]


def test_an_install_interrupted_after_registration_finishes_when_run_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_data_dir(data_dir)

    def fail_the_final_check(data_dir: Path, registered: Any) -> None:
        raise InstallError("simulated crash before the final check")

    monkeypatch.setattr(_install, "_check_registered", fail_the_final_check)
    with pytest.raises(InstallError):
        convert_data_directory(data_dir)
    assert read_marker(data_dir) is not None
    assert read_maintenance(data_dir) is not None

    monkeypatch.undo()
    report = convert_data_directory(data_dir)

    assert report["result"] == "installed"
    assert read_maintenance(data_dir) is None
    for spec in database_specs(data_dir).values():
        open_database(spec).close()


def test_a_leftover_backup_directory_is_refused(data_dir: Path) -> None:
    _legacy_data_dir(data_dir)
    (data_dir / "pre-generation-1").mkdir()

    with pytest.raises(RefusedError, match="pre-generation-1"):
        convert_data_directory(data_dir)


def test_a_directory_that_is_not_a_data_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RefusedError, match="vBot data directory"):
        convert_data_directory(tmp_path)
    assert os.listdir(tmp_path) == []
