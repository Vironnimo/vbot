"""The JSON document set of data snapshots: capture, verification and set restore."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    DatabaseCorruptError,
    DatabaseUnavailableError,
    list_data_snapshots,
    open_database,
    read_maintenance,
    read_verified_manifest,
    restore_data_snapshot,
    snapshot_summaries,
)
from core.database import _documents as documents_module
from core.database.recovery import quarantine_root
from core.database.snapshots import (
    SNAPSHOT_MANIFEST_NAME,
    member_restore_candidates,
    snapshot_inventory,
)
from tests.core.database.database_test_support import (
    add_note,
    notes_spec,
    snapshot_with_notes,
    stored_bodies,
)

_DOCUMENTS = {
    "settings.json": '{"format_version": 1, "theme": "dark"}\n',
    "agents/main/agent.json": '{"format_version": 1, "id": "main"}\n',
    "oauth/provider.json": '{"format_version": 1, "access_token": "secret"}\n',
}


def _write(data_dir: Path, relative: str, text: str) -> Path:
    path = data_dir.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _with_documents(data_dir: Path) -> Path:
    for relative, text in _DOCUMENTS.items():
        _write(data_dir, relative, text)
    return snapshot_with_notes(data_dir, "saved")


def _manifest(snapshot: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (snapshot / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    return payload


def _rewrite_manifest(snapshot: Path, change: Callable[[dict[str, Any]], None]) -> None:
    payload = _manifest(snapshot)
    change(payload)
    (snapshot / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")


def test_a_snapshot_holds_every_durable_document_and_nothing_else(data_dir: Path) -> None:
    _write(data_dir, "agents/main/notes.txt", "not a document")
    _write(data_dir, "agents/.main.json", "{}")
    _write(data_dir, ".settings.json.tmp", "{}")
    _write(data_dir, "workspaces/main/project.json", "{}")

    snapshot = _with_documents(data_dir)

    documents = _manifest(snapshot)["documents"]
    assert sorted(documents) == sorted(_DOCUMENTS)
    for relative, text in _DOCUMENTS.items():
        copy = snapshot / "documents" / relative
        assert copy.read_text(encoding="utf-8") == text
        assert documents[relative]["file_size"] == copy.stat().st_size
        assert len(documents[relative]["sha256"]) == 64
    summary = snapshot_summaries(data_dir)[0]
    assert summary["documents"] == {
        "count": 3,
        "file_size": sum(len(text.encode()) for text in _DOCUMENTS.values()),
    }
    assert "secret" not in json.dumps(summary)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_document_copy_keeps_its_permissions(data_dir: Path) -> None:
    token = _write(data_dir, "oauth/provider.json", '{"format_version": 1}\n')
    token.chmod(0o600)

    snapshot = snapshot_with_notes(data_dir, "saved")

    assert stat.S_IMODE((snapshot / "documents" / "oauth" / "provider.json").stat().st_mode) == (
        0o600
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symbolic links unavailable")
def test_a_linked_document_is_not_a_member(data_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = data_dir / "settings.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating symbolic links is not permitted")

    snapshot = snapshot_with_notes(data_dir, "saved")

    assert _manifest(snapshot)["documents"] == {}


def test_a_tampered_document_fails_the_snapshot_but_not_its_database_members(
    data_dir: Path,
) -> None:
    snapshot = _with_documents(data_dir)
    copy = snapshot / "documents" / "settings.json"
    copy.write_bytes(copy.read_bytes().replace(b"dark", b"pale"))
    database_id = _manifest(snapshot)["members"]["notes"]["database_id"]

    assert read_verified_manifest(data_dir, snapshot) is None
    assert list_data_snapshots(data_dir) == []
    # Corruption auto-restore needs only the affected database member.
    assert [
        path
        for path, _manifest, _member in member_restore_candidates(
            data_dir, "notes", database_id=database_id, spec=notes_spec(data_dir)
        )
    ] == [snapshot]
    with pytest.raises(DatabaseCorruptError, match="settings.json hash mismatch"):
        restore_data_snapshot(data_dir, snapshot, names=(), documents=True)


def test_a_missing_document_copy_hides_the_snapshot_from_status(data_dir: Path) -> None:
    snapshot = _with_documents(data_dir)
    (snapshot / "documents" / "agents" / "main" / "agent.json").unlink()

    assert snapshot_inventory(data_dir) == []
    assert read_verified_manifest(data_dir, snapshot) is None


@pytest.mark.parametrize(
    "path",
    [
        "../settings.json",
        "agents/main/../agent.json",
        "agents/.main/agent.json",
        "/settings.json",
        "C:/settings.json",
        "Settings.json",
        "x\\y",
    ],
)
def test_a_manifest_with_an_invalid_document_path_is_not_a_snapshot(
    data_dir: Path, path: str
) -> None:
    snapshot = _with_documents(data_dir)
    _rewrite_manifest(
        snapshot,
        lambda payload: payload["documents"].update({path: {"file_size": 1, "sha256": "0" * 64}}),
    )

    assert list_data_snapshots(data_dir) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload.pop("documents"),
        lambda payload: payload["documents"]["settings.json"].pop("sha256"),
    ],
    ids=["document-set", "document-field"],
)
def test_a_manifest_without_its_document_set_is_not_a_snapshot(
    data_dir: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    snapshot = _with_documents(data_dir)
    _rewrite_manifest(snapshot, change)

    assert list_data_snapshots(data_dir) == []
    assert snapshot_inventory(data_dir) == []


def test_an_older_vbot_ignores_documents_and_fields_a_newer_one_added(data_dir: Path) -> None:
    snapshot = _with_documents(data_dir)
    future = _write(data_dir, "future/state.json", '{"format_version": 1}\n')

    def newer(payload: dict[str, Any]) -> None:
        payload["documents"]["settings.json"]["encoding"] = "utf-8"
        # A document kind this vBot does not know; its copy is not even there.
        payload["documents"]["future/state.json"] = {"file_size": 1, "sha256": "0" * 64}

    _rewrite_manifest(snapshot, newer)
    _write(data_dir, "settings.json", '{"format_version": 1, "theme": "light"}\n')

    assert list_data_snapshots(data_dir) == [snapshot]
    assert snapshot_summaries(data_dir)[0]["documents"]["count"] == len(_DOCUMENTS)
    result = restore_data_snapshot(data_dir, snapshot, names=(), documents=True)

    assert result.documents is not None
    assert result.documents.restored == ("settings.json",)
    assert result.documents.removed == ()
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == _DOCUMENTS["settings.json"]
    assert future.read_text(encoding="utf-8") == '{"format_version": 1}\n'


def test_the_document_set_is_restored_as_one_unit(data_dir: Path) -> None:
    snapshot = _with_documents(data_dir)
    database = open_database(notes_spec(data_dir))
    add_note(database, "later")
    database.close()
    _write(data_dir, "settings.json", '{"format_version": 1, "theme": "light"}\n')
    (data_dir / "agents" / "main" / "agent.json").unlink()
    extra = _write(data_dir, "channels/new/channel.json", '{"format_version": 1}\n')

    plan = restore_data_snapshot(data_dir, snapshot, names=(), documents=True, check_only=True)
    assert plan.databases == ()
    assert plan.documents is not None
    assert plan.documents.restored == ("agents/main/agent.json", "settings.json")
    assert plan.documents.removed == ("channels/new/channel.json",)

    result = restore_data_snapshot(data_dir, snapshot, names=(), documents=True)

    for relative, text in _DOCUMENTS.items():
        assert data_dir.joinpath(*relative.split("/")).read_text(encoding="utf-8") == text
    assert not extra.exists()
    assert result.documents is not None
    assert result.documents.quarantine is not None
    assert result.documents.quarantine.parent == quarantine_root(data_dir) / "json-documents"
    kept = result.documents.quarantine
    assert "light" in (kept / "settings.json").read_text(encoding="utf-8")
    assert (kept / "channels" / "new" / "channel.json").is_file()
    assert not (kept / "oauth").exists()
    # Databases were not selected.
    assert stored_bodies(notes_spec(data_dir)) == ["saved", "later"]
    assert read_maintenance(data_dir) is None


def test_an_unchanged_document_set_moves_nothing(data_dir: Path) -> None:
    snapshot = _with_documents(data_dir)

    result = restore_data_snapshot(data_dir, snapshot, names=(), documents=True)

    assert result.documents is not None
    assert not result.documents.changed
    assert not quarantine_root(data_dir).exists()


def test_a_failed_document_quarantine_rolls_back_and_keeps_the_guard(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _with_documents(data_dir)
    changed = '{"format_version": 1, "theme": "light"}\n'
    _write(data_dir, "settings.json", changed)
    _write(data_dir, "oauth/provider.json", '{"format_version": 1, "access_token": "new"}\n')
    real_replace = documents_module.os.replace
    moves: list[Path] = []

    def fail_second_quarantine(source: str | Path, destination: str | Path) -> None:
        if quarantine_root(data_dir) in Path(destination).parents:
            moves.append(Path(destination))
            if len(moves) == 2:
                raise OSError("injected quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(documents_module.os, "replace", fail_second_quarantine)
    with pytest.raises(DatabaseUnavailableError, match="could not be quarantined"):
        restore_data_snapshot(data_dir, snapshot, names=(), documents=True)

    assert (data_dir / "settings.json").read_text(encoding="utf-8") == changed
    assert "new" in (data_dir / "oauth" / "provider.json").read_text(encoding="utf-8")
    assert read_maintenance(data_dir) is not None
    assert not [path for path in data_dir.rglob("*.tmp") if ".restore." in path.name], (
        "staged copies are removed"
    )

    monkeypatch.setattr(documents_module.os, "replace", real_replace)
    restore_data_snapshot(data_dir, snapshot, names=(), documents=True)
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == _DOCUMENTS["settings.json"]
    assert read_maintenance(data_dir) is None


def test_selecting_nothing_is_refused(data_dir: Path) -> None:
    snapshot = _with_documents(data_dir)

    with pytest.raises(ValueError, match="no snapshot member selected"):
        restore_data_snapshot(data_dir, snapshot, names=())
