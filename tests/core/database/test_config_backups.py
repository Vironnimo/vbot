"""Configuration backups: what they hold, deduplication, retention and offline restore."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.database import (
    DatabaseCorruptError,
    DatabaseFormatError,
    OperationLockBusyError,
    capture_config_backup,
    config_backup_root,
    data_store_status,
    describe_config_backup,
    list_config_backups,
    read_config_backup,
    read_maintenance,
    restore_config_backup,
)
from core.database import config_backups as config_backups_module
from core.database.marker import acquire_operation_lock
from tests.core.database.database_test_support import write_document

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _settings(value: str) -> str:
    return json.dumps({"format_version": 1, "timezone": value}) + "\n"


def _write(data_dir: Path, relative: str, text: str) -> Path:
    return write_document(data_dir, relative, text)


def _read(data_dir: Path, relative: str) -> str:
    return data_dir.joinpath(*relative.split("/")).read_text(encoding="utf-8")


def _objects(data_dir: Path) -> set[str]:
    objects = config_backup_root(data_dir) / "objects"
    return {path.name for path in objects.rglob("*") if path.is_file()}


def _backup(data_dir: Path, *, now: datetime = NOW, reason: str = "test") -> str:
    backup = capture_config_backup(data_dir, reason=reason, now=now)
    assert backup is not None
    return backup.backup_id


def test_a_backup_holds_the_configuration_files_and_nothing_else(data_dir: Path) -> None:
    configuration = {
        "settings.json": _settings("Europe/Berlin"),
        ".env": "OPENAI_API_KEY=secret\n",
        "agents/nova/agent.json": '{"format_version": 1, "id": "nova"}\n',
        "agents/nova/workspace/SOUL.md": "# Soul\n",
        "agents/nova/workspace/USER.md": "- likes tea\n",
        "agents/nova/workspace/MEMORY.md": "- remembers\n",
        "agents/nova/prompts/blocks/custom/notes.md": "agent block\n",
        "prompts/blocks/custom/notes.md": "default block\n",
        "cron/jobs.json": '{"format_version": 1, "jobs": []}\n',
    }
    for relative, text in configuration.items():
        _write(data_dir, relative, text)
    # Workspace files, Skills, blob sidecars and the backups themselves stay out.
    _write(data_dir, "agents/nova/workspace/notes.txt", "scratch\n")
    _write(data_dir, "agents/nova/skills/demo/SKILL.md", "skill\n")
    _write(data_dir, "artifacts/attachments/att_1.json", '{"format_version": 1}\n')

    backup_id = _backup(data_dir)

    backup = read_config_backup(data_dir, backup_id)
    assert sorted(backup.files) == sorted(configuration)
    assert backup.reason == "test"
    assert backup.damaged() == ()
    described = describe_config_backup(data_dir, backup_id)
    assert {item["state"] for item in described["files"].values()} == {"same"}
    assert described["created_after"] == []
    assert len(_objects(data_dir)) == len(set(configuration.values()))
    manifest = json.loads(
        (config_backup_root(data_dir) / "backups" / f"{backup_id}.json").read_text("utf-8")
    )
    assert manifest["manifest_version"] == 1
    summary = data_store_status(data_dir)["config_backups"]
    assert summary["count"] == 1
    assert summary["latest"]["backup_id"] == backup_id


def test_only_a_changed_configuration_is_backed_up_and_unchanged_contents_are_shared(
    data_dir: Path,
) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, ".env", "KEY=one\n")
    first = _backup(data_dir)
    stored = _objects(data_dir)

    assert capture_config_backup(data_dir, reason="test", now=NOW) is None

    _write(data_dir, "settings.json", _settings("UTC"))
    second = _backup(data_dir, now=NOW + timedelta(minutes=5))

    assert [backup.backup_id for backup in list_config_backups(data_dir)] == [second, first]
    assert len(_objects(data_dir) - stored) == 1
    described = describe_config_backup(data_dir, first)
    assert described["files"]["settings.json"]["state"] == "differs"
    assert described["files"][".env"]["state"] == "same"


def test_a_damaged_json_document_is_marked_and_never_restored(data_dir: Path) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    good = _backup(data_dir)
    # What a power loss can leave behind: a zero-filled or truncated document.
    data_dir.joinpath("settings.json").write_bytes(b"\0" * 32)
    damaged = _backup(data_dir, now=NOW + timedelta(minutes=5))

    assert read_config_backup(data_dir, damaged).damaged() == ("settings.json",)
    assert data_store_status(data_dir)["config_backups"]["latest"]["damaged"] == ["settings.json"]
    with pytest.raises(ValueError, match="damaged"):
        restore_config_backup(data_dir, damaged, paths=["settings.json"])
    assert restore_config_backup(data_dir, damaged).skipped.keys() == {"settings.json"}

    restore_config_backup(data_dir, good, paths=["settings.json"])

    assert _read(data_dir, "settings.json") == _settings("Europe/Berlin")


def test_retention_keeps_the_newest_backups_and_the_newest_of_each_hour_day_and_week(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_backups_module, "CONFIG_BACKUP_KEEP_LATEST", 2)
    ages = {
        "older than a year": timedelta(weeks=60),
        "week, earlier": timedelta(weeks=10, hours=2),
        "week, newest": timedelta(weeks=10),
        "day, earlier": timedelta(days=20, hours=2),
        "day, newest": timedelta(days=20),
        "hour, earlier": timedelta(hours=5, minutes=30),
        "hour, newest": timedelta(hours=5, minutes=10),
        "latest, second": timedelta(minutes=10),
    }
    ids: dict[str, str] = {}
    for label, age in sorted(ages.items(), key=lambda item: item[1], reverse=True):
        _write(data_dir, "settings.json", _settings(label))
        ids[label] = _backup(data_dir, now=NOW - age)
    # A newer vBot backed up a file this vBot does not know; its content must stay.
    manifest = config_backup_root(data_dir) / "backups" / f"{ids['latest, second']}.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    future = hashlib.sha256(b"future").hexdigest()
    payload["files"]["future/config.json"] = {"sha256": future, "file_size": 6, "mode": 0o600}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    write_document(config_backup_root(data_dir), f"objects/{future[:2]}/{future}", "future")
    _write(data_dir, "settings.json", _settings("latest"))
    ids["latest"] = _backup(data_dir, now=NOW)

    kept = {backup.backup_id for backup in list_config_backups(data_dir)}

    assert kept == {
        ids[label]
        for label in ("week, newest", "day, newest", "hour, newest", "latest, second", "latest")
    }
    # Contents only dropped backups held are removed with them.
    assert _objects(data_dir) == {
        digest for backup in list_config_backups(data_dir) for digest in backup.objects()
    }
    assert future in _objects(data_dir)


def test_a_restore_replaces_the_named_files_after_backing_up_the_state_it_replaces(
    data_dir: Path,
) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, "agents/nova/agent.json", '{"format_version": 1, "id": "nova"}\n')
    earlier = _backup(data_dir)
    _write(data_dir, "settings.json", _settings("UTC"))
    _write(data_dir, "agents/nova/agent.json", '{"format_version": 1, "id": "nova", "x": 1}\n')

    plan = restore_config_backup(data_dir, earlier, paths=["settings.json"], check_only=True)
    assert plan.restored == ("settings.json",) and plan.before_restore is None
    assert _read(data_dir, "settings.json") == _settings("UTC")
    # A Windows path names the same file.
    restored = restore_config_backup(
        data_dir, earlier, paths=["settings.json", "agents\\nova\\agent.json"]
    )

    assert restored.restored == ("agents/nova/agent.json", "settings.json")
    assert _read(data_dir, "settings.json") == _settings("Europe/Berlin")
    assert read_maintenance(data_dir) is None
    assert restored.before_restore is not None
    before = read_config_backup(data_dir, restored.before_restore)
    assert before.reason == f"before restore {earlier}"

    restore_config_backup(data_dir, before.backup_id, paths=["settings.json"])

    assert _read(data_dir, "settings.json") == _settings("UTC")
    with pytest.raises(ValueError, match="not in configuration backup"):
        restore_config_backup(data_dir, earlier, paths=["cron/jobs.json"])


def test_restoring_a_whole_backup_leaves_newer_files_and_vanished_folders_alone(
    data_dir: Path,
) -> None:
    _write(data_dir, "agents/nova/agent.json", '{"format_version": 1, "id": "nova"}\n')
    _write(data_dir, "agents/gone/agent.json", '{"format_version": 1, "id": "gone"}\n')
    backup_id = _backup(data_dir)
    _write(data_dir, "agents/nova/agent.json", '{"format_version": 1, "id": "nova", "x": 1}\n')
    for path in sorted(data_dir.joinpath("agents", "gone").rglob("*"), reverse=True):
        path.unlink()
    data_dir.joinpath("agents", "gone").rmdir()
    _write(data_dir, "agents/new/agent.json", '{"format_version": 1, "id": "new"}\n')

    restored = restore_config_backup(data_dir, backup_id)

    assert restored.restored == ("agents/nova/agent.json",)
    assert set(restored.skipped) == {"agents/gone/agent.json"}
    assert restored.created_after == ("agents/new/agent.json",)
    assert not data_dir.joinpath("agents", "gone").exists()
    assert data_dir.joinpath("agents", "new", "agent.json").exists()
    with pytest.raises(ValueError, match="folder no longer exists"):
        restore_config_backup(data_dir, backup_id, paths=["agents/gone/agent.json"])


def test_a_restore_verifies_every_copy_before_it_changes_anything(data_dir: Path) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, ".env", "KEY=one\n")
    backup_id = _backup(data_dir)
    _write(data_dir, "settings.json", _settings("UTC"))
    _write(data_dir, ".env", "KEY=two\n")
    env_object = read_config_backup(data_dir, backup_id).files[".env"].sha256
    (config_backup_root(data_dir) / "objects" / env_object[:2] / env_object).write_bytes(b"x")

    with pytest.raises(DatabaseCorruptError, match="altered"):
        restore_config_backup(data_dir, backup_id)

    assert _read(data_dir, "settings.json") == _settings("UTC")
    assert len(list_config_backups(data_dir)) == 1


@pytest.mark.parametrize("changed", [False, True], ids=["unchanged", "changed"])
def test_a_backup_repairs_a_stored_copy_altered_to_the_same_size(
    data_dir: Path, changed: bool
) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, ".env", "KEY=one\n")
    first = _backup(data_dir)
    env_object = read_config_backup(data_dir, first).files[".env"].sha256
    (config_backup_root(data_dir) / "objects" / env_object[:2] / env_object).write_bytes(
        b"KEY=bad\n"
    )
    if changed:
        _write(data_dir, "settings.json", _settings("UTC"))

    second = capture_config_backup(data_dir, reason="test", now=NOW + timedelta(minutes=5))

    assert (second is not None) is changed
    _write(data_dir, ".env", "KEY=two\n")
    restore_config_backup(data_dir, first if second is None else second.backup_id, paths=[".env"])
    assert _read(data_dir, ".env") == "KEY=one\n"


@pytest.mark.parametrize("wrote_every_file", [False, True], ids=["mid-write", "after writing"])
def test_an_interrupted_restore_holds_the_maintenance_guard_until_it_is_repeated(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, wrote_every_file: bool
) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, ".env", "KEY=one\n")
    backup_id = _backup(data_dir)
    _write(data_dir, "settings.json", _settings("Asia/Tokyo"))
    other_id = _backup(data_dir, now=NOW + timedelta(minutes=5))
    _write(data_dir, "settings.json", _settings("UTC"))
    _write(data_dir, ".env", "KEY=two\n")
    real_write = config_backups_module.atomic_write_bytes

    def fail_on_settings(target: Path, *args: object, **kwargs: object) -> None:
        if target.name == "settings.json" and not wrote_every_file:
            raise OSError("injected write failure")
        real_write(target, *args, **kwargs)  # type: ignore[arg-type]
        if target.name == "settings.json":
            # The last file is written; the restore fails before it ends the guard.
            raise OSError("injected failure")

    with monkeypatch.context() as patched:
        patched.setattr(config_backups_module, "atomic_write_bytes", fail_on_settings)
        with pytest.raises(OSError, match="injected"):
            restore_config_backup(data_dir, backup_id)
    first_before = list_config_backups(data_dir)[0]
    assert first_before.reason == f"before restore {backup_id}"
    assert read_maintenance(data_dir) is not None
    with pytest.raises(DatabaseFormatError, match="maintenance"):
        capture_config_backup(data_dir, reason="test")
    with pytest.raises(DatabaseFormatError, match=f"backup {backup_id} did not finish"):
        restore_config_backup(data_dir, other_id, check_only=True)
    assert restore_config_backup(data_dir, backup_id, check_only=True).interrupted

    repeated = restore_config_backup(data_dir, backup_id)

    assert read_maintenance(data_dir) is None
    assert repeated.restored == (() if wrote_every_file else ("settings.json",))
    assert repeated.before_restore == first_before.backup_id
    assert _read(data_dir, ".env") == "KEY=one\n"
    assert _read(data_dir, "settings.json") == _settings("Europe/Berlin")


def test_a_backup_does_not_wait_long_for_another_data_store_operation(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    monkeypatch.setattr(config_backups_module, "CONFIG_BACKUP_LOCK_SECONDS", 0.1)
    lock = acquire_operation_lock(data_dir)
    assert lock is not None
    try:
        with pytest.raises(OperationLockBusyError):
            capture_config_backup(data_dir, reason="test")
    finally:
        lock.release()

    assert capture_config_backup(data_dir, reason="test") is not None
