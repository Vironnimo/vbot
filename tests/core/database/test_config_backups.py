"""Configuration backups: what they hold, deduplication, retention and offline restore."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from core.database import (
    ConfigBackupPurge,
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    OperationLockBusyError,
    capture_config_backup,
    config_backup_root,
    data_store_status,
    describe_config_backup,
    list_config_backups,
    purge_config_backup_files,
    read_config_backup,
    read_maintenance,
    reserve_config_backup,
    restore_config_backup,
)
from core.database import _config_restore as config_restore_module
from core.database import config_backups as config_backups_module
from core.database.marker import acquire_operation_lock
from tests.core.database.database_test_support import write_document
from tests.directory_links import link_directory

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
# A configuration file that is no JSON document.
NOTE = "prompts/blocks/custom/notes.md"


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
        "secrets.json": '{"format_version": 1, "credentials": {"OPENAI_API_KEY": "sealed"}}\n',
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
    # Workspace files, Skills, blob sidecars, plaintext credential files and the
    # backups themselves stay out.
    _write(data_dir, ".env", "OPENAI_API_KEY=plaintext\n")
    _write(data_dir, "oauth/github-copilot-oauth.json", '{"format_version": 1}\n')
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
    _write(data_dir, NOTE, "KEY=one\n")
    first = _backup(data_dir)
    stored = _objects(data_dir)

    assert capture_config_backup(data_dir, reason="test", now=NOW) is None

    _write(data_dir, "settings.json", _settings("UTC"))
    second = _backup(data_dir, now=NOW + timedelta(minutes=5))

    assert [backup.backup_id for backup in list_config_backups(data_dir)] == [second, first]
    assert len(_objects(data_dir) - stored) == 1
    described = describe_config_backup(data_dir, first)
    assert described["files"]["settings.json"]["state"] == "differs"
    assert described["files"][NOTE]["state"] == "same"


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


def test_a_reserved_backup_outlives_retention_until_released_or_expired(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, deny_access: Callable[[Path], None]
) -> None:
    # Only the newest backup is retained, so each further backup drops the older one.
    monkeypatch.setattr(config_backups_module, "CONFIG_BACKUP_KEEP_LATEST", 1)

    def backed_up(timezone: str, at: datetime) -> str:
        _write(data_dir, "settings.json", _settings(timezone))
        return _backup(data_dir, now=at)

    def kept() -> set[str]:
        return {backup.backup_id for backup in list_config_backups(data_dir)}

    minute = timedelta(minutes=1)
    chosen = backed_up("Europe/Berlin", NOW)
    with pytest.raises(ValueError, match="no configuration backup"):
        reserve_config_backup(data_dir, "20260101T000000Z-00000000", now=NOW)
    reservation = reserve_config_backup(data_dir, chosen, now=NOW)
    # The server's last backup before it stops, between the check and the restore.
    backed_up("UTC", NOW + minute)
    assert chosen in kept()
    restore_config_backup(data_dir, chosen, now=NOW + 2 * minute)
    reservation.release()
    backed_up("Asia/Tokyo", NOW + 3 * minute)
    assert chosen not in kept()

    # A holder that died never releases its reservation; it expires.
    stale = backed_up("America/Chicago", NOW + 4 * minute)
    reserve_config_backup(data_dir, stale, now=NOW + 4 * minute)
    paris = backed_up("Europe/Paris", NOW + 5 * minute)
    assert stale in kept()
    expired = NOW + 4 * minute + config_backups_module.CONFIG_BACKUP_RESERVATION_TIME
    rome = backed_up("Europe/Rome", expired)
    assert stale not in kept()
    assert not any((config_backup_root(data_dir) / "reservations").iterdir())

    # A reservation that cannot be read may name any backup: retention waits for it.
    deny_access(reserve_config_backup(data_dir, paris, now=expired).path)
    backed_up("Europe/Madrid", expired + minute)
    assert rome in kept()


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


def test_restoring_a_whole_backup_leaves_newer_files_and_vanished_or_linked_folders_alone(
    data_dir: Path, tmp_path: Path
) -> None:
    _write(data_dir, "agents/nova/agent.json", '{"format_version": 1, "id": "nova"}\n')
    _write(data_dir, "agents/gone/agent.json", '{"format_version": 1, "id": "gone"}\n')
    _write(data_dir, "agents/linked/agent.json", '{"format_version": 1, "id": "linked"}\n')
    _write(data_dir, "secrets.json", '{"format_version": 1, "credentials": {"KEY": "old"}}\n')
    backup_id = _backup(data_dir)
    # Credentials changed after the backup: only a restore naming the store replaces it.
    newer_secrets = '{"format_version": 1, "credentials": {"KEY": "newer"}}\n'
    _write(data_dir, "secrets.json", newer_secrets)
    # A folder replaced by a link: backups never read through it, so a restore
    # must not write through it either.
    linked = data_dir / "agents" / "linked"
    (linked / "agent.json").unlink()
    linked.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "agent.json").write_text("outside\n", encoding="utf-8")
    link_directory(linked, outside)
    _write(data_dir, "agents/nova/agent.json", '{"format_version": 1, "id": "nova", "x": 1}\n')
    for path in sorted(data_dir.joinpath("agents", "gone").rglob("*"), reverse=True):
        path.unlink()
    data_dir.joinpath("agents", "gone").rmdir()
    _write(data_dir, "agents/new/agent.json", '{"format_version": 1, "id": "new"}\n')

    restored = restore_config_backup(data_dir, backup_id)

    assert restored.restored == ("agents/nova/agent.json",)
    assert set(restored.skipped) == {
        "agents/gone/agent.json",
        "agents/linked/agent.json",
        "secrets.json",
    }
    assert "name this file" in restored.skipped["secrets.json"]
    assert _read(data_dir, "secrets.json") == newer_secrets
    assert restored.created_after == ("agents/new/agent.json",)
    assert not data_dir.joinpath("agents", "gone").exists()
    assert data_dir.joinpath("agents", "new", "agent.json").exists()
    assert (outside / "agent.json").read_text(encoding="utf-8") == "outside\n"
    with pytest.raises(ValueError, match="folder no longer exists"):
        restore_config_backup(data_dir, backup_id, paths=["agents/gone/agent.json"])
    with pytest.raises(ValueError, match="is a link"):
        restore_config_backup(data_dir, backup_id, paths=["agents/linked/agent.json"])
    named = restore_config_backup(data_dir, backup_id, paths=["secrets.json"])
    assert named.restored == ("secrets.json",)
    assert "old" in _read(data_dir, "secrets.json")


def test_a_restore_verifies_every_copy_before_it_changes_anything(data_dir: Path) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, NOTE, "KEY=one\n")
    backup_id = _backup(data_dir)
    _write(data_dir, "settings.json", _settings("UTC"))
    _write(data_dir, NOTE, "KEY=two\n")
    note_object = read_config_backup(data_dir, backup_id).files[NOTE].sha256
    (config_backup_root(data_dir) / "objects" / note_object[:2] / note_object).write_bytes(b"x")

    with pytest.raises(DatabaseCorruptError, match="altered"):
        restore_config_backup(data_dir, backup_id)

    assert _read(data_dir, "settings.json") == _settings("UTC")
    assert len(list_config_backups(data_dir)) == 1


@pytest.mark.parametrize("changed", [False, True], ids=["unchanged", "changed"])
def test_a_backup_repairs_a_stored_copy_altered_to_the_same_size(
    data_dir: Path, changed: bool
) -> None:
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, NOTE, "KEY=one\n")
    first = _backup(data_dir)
    note_object = read_config_backup(data_dir, first).files[NOTE].sha256
    (config_backup_root(data_dir) / "objects" / note_object[:2] / note_object).write_bytes(
        b"KEY=bad\n"
    )
    if changed:
        _write(data_dir, "settings.json", _settings("UTC"))

    second = capture_config_backup(data_dir, reason="test", now=NOW + timedelta(minutes=5))

    assert (second is not None) is changed
    _write(data_dir, NOTE, "KEY=two\n")
    restore_config_backup(data_dir, first if second is None else second.backup_id, paths=[NOTE])
    assert _read(data_dir, NOTE) == "KEY=one\n"


@pytest.mark.parametrize("ending", ["repeated", "replaced"])
@pytest.mark.parametrize(
    ("wrote_every_file", "state_backed_up"),
    [(False, False), (True, False), (False, True)],
    ids=["mid-write", "after writing", "state already backed up"],
)
def test_an_interrupted_restore_holds_the_maintenance_guard_until_repeated_or_replaced(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrote_every_file: bool,
    state_backed_up: bool,
    ending: str,
) -> None:
    # Only the newest-backups rule holds the restored backup: the before-restore
    # backup would push it out, and the restore could never be repeated.
    monkeypatch.setattr(config_backups_module, "CONFIG_BACKUP_KEEP_LATEST", 2)
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    _write(data_dir, NOTE, "KEY=one\n")
    backup_id = _backup(data_dir)
    _write(data_dir, "settings.json", _settings("Asia/Tokyo"))
    other_id = _backup(data_dir, now=NOW + timedelta(minutes=5))
    if not state_backed_up:
        # A state no backup holds yet: the restore backs it up first.
        _write(data_dir, "settings.json", _settings("UTC"))
        _write(data_dir, NOTE, "KEY=two\n")
    real_write = config_restore_module.atomic_write_bytes

    def fail_on_settings(target: Path, *args: object, **kwargs: object) -> None:
        if target.name == "settings.json" and not wrote_every_file:
            raise OSError("injected write failure")
        real_write(target, *args, **kwargs)  # type: ignore[arg-type]
        if target.name == "settings.json":
            # The last file is written; the restore fails before it ends the guard.
            raise OSError("injected failure")

    with monkeypatch.context() as patched:
        patched.setattr(config_restore_module, "atomic_write_bytes", fail_on_settings)
        with pytest.raises(OSError, match="injected"):
            restore_config_backup(data_dir, backup_id, now=NOW + timedelta(minutes=10))
    # The backup that holds the replaced state, the undo point of the restore.
    undo_point = list_config_backups(data_dir)[0]
    if state_backed_up:
        assert undo_point.backup_id == other_id
    else:
        assert undo_point.reason == f"before restore {backup_id}"
    assert read_maintenance(data_dir) is not None
    with pytest.raises(DatabaseFormatError, match="maintenance"):
        capture_config_backup(data_dir, reason="test")
    # Restoring named files of another backup would keep the half-restored state.
    with pytest.raises(DatabaseFormatError, match=f"backup {backup_id} did not finish"):
        restore_config_backup(data_dir, other_id, paths=["settings.json"], check_only=True)
    assert restore_config_backup(data_dir, backup_id, check_only=True).interrupted
    assert restore_config_backup(data_dir, other_id, check_only=True).replaced == backup_id

    if ending == "repeated":
        repeated = restore_config_backup(data_dir, backup_id)

        assert read_maintenance(data_dir) is None
        assert repeated.restored == (() if wrote_every_file else ("settings.json",))
        assert repeated.before_restore == undo_point.backup_id
        assert _read(data_dir, NOTE) == "KEY=one\n"
        assert _read(data_dir, "settings.json") == _settings("Europe/Berlin")
        return
    # Once the restored backup is lost, its restore can no longer be repeated.
    (config_backup_root(data_dir) / "backups" / f"{backup_id}.json").unlink()
    with pytest.raises(DatabaseFormatError):
        reserve_config_backup(data_dir, backup_id)
    with pytest.raises(DatabaseFormatError):
        restore_config_backup(data_dir, backup_id)
    half_restored = {path: _read(data_dir, path) for path in ("settings.json", NOTE)}

    replaced = restore_config_backup(data_dir, other_id, now=NOW + timedelta(minutes=15))

    assert read_maintenance(data_dir) is None
    assert replaced.replaced == backup_id
    assert _read(data_dir, "settings.json") == _settings("Asia/Tokyo")
    assert _read(data_dir, NOTE) == "KEY=one\n"
    # The half-restored state is backed up first, like any state a restore replaces.
    assert replaced.before_restore is not None
    before = read_config_backup(data_dir, replaced.before_restore)
    assert {path: before.files[path].sha256 for path in half_restored} == {
        path: hashlib.sha256(text.encode()).hexdigest() for path, text in half_restored.items()
    }


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


def test_restore_holds_the_operation_lock_until_every_file_and_the_guard_are_settled(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(data_dir, NOTE, "KEY=old\n")
    _write(data_dir, "settings.json", _settings("Europe/Berlin"))
    backup_id = _backup(data_dir)
    _write(data_dir, NOTE, "KEY=new\n")
    _write(data_dir, "settings.json", _settings("UTC"))
    writing, finish = Event(), Event()
    real_write = config_restore_module.atomic_write_bytes

    def hold_last_write(target: Path, *args: object, **kwargs: object) -> None:
        if target == data_dir / "settings.json":
            writing.set()
            assert finish.wait(2)
        real_write(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_restore_module, "atomic_write_bytes", hold_last_write)
    monkeypatch.setattr("core.database.marker.OPERATION_LOCK_TIMEOUT_SECONDS", 0.1)
    with ThreadPoolExecutor(max_workers=1) as threads:
        restoring = threads.submit(restore_config_backup, data_dir, backup_id)
        try:
            assert writing.wait(2)
            guard = read_maintenance(data_dir)
            assert guard is not None
            with pytest.raises(DatabaseUnavailableError):
                restore_config_backup(data_dir, backup_id, paths=[NOTE])
            assert read_maintenance(data_dir) == guard
            with pytest.raises(DatabaseFormatError):
                capture_config_backup(data_dir, reason="during restore")
        finally:
            finish.set()
        restoring.result(timeout=2)
    assert read_maintenance(data_dir) is None
    assert _read(data_dir, "settings.json") == _settings("Europe/Berlin")


def test_purging_retired_files_rewrites_the_backups_and_deletes_their_contents(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Backups an older vBot took, when plaintext credentials were configuration files.
    with monkeypatch.context() as older:
        older.setattr(
            config_backups_module,
            "CONFIGURATION_FILE_PATTERNS",
            (*config_backups_module.CONFIGURATION_FILE_PATTERNS, ".env", "oauth/*.json"),
        )
        _write(data_dir, "settings.json", _settings("Europe/Berlin"))
        _write(data_dir, ".env", "OPENAI_API_KEY=plaintext\n")
        _write(data_dir, "oauth/github-copilot-oauth.json", '{"format_version": 1}\n')
        first = _backup(data_dir)
        _write(data_dir, ".env", "OPENAI_API_KEY=rotated\n")
        _write(data_dir, "settings.json", _settings("UTC"))
        second = _backup(data_dir, now=NOW + timedelta(minutes=5))
    kept_settings = {
        backup.backup_id: backup.files["settings.json"].sha256
        for backup in list_config_backups(data_dir)
    }

    with pytest.raises(ValueError, match="outside the configuration set"):
        purge_config_backup_files(data_dir, ("settings.json",))
    purge = purge_config_backup_files(data_dir, (".env", "oauth/*.json"))

    assert purge == ConfigBackupPurge(removed_entries=4, rewritten_backups=2, complete=True)
    backups = list_config_backups(data_dir)
    assert [backup.backup_id for backup in backups] == [second, first]
    assert all(sorted(backup.files) == ["settings.json"] for backup in backups)
    assert {backup.backup_id: backup.files["settings.json"].sha256 for backup in backups} == (
        kept_settings
    )
    assert _objects(data_dir) == set(kept_settings.values())
    restore_config_backup(data_dir, first)
    assert _read(data_dir, "settings.json") == _settings("Europe/Berlin")
    assert purge_config_backup_files(data_dir, (".env", "oauth/*.json")) == ConfigBackupPurge()


@pytest.mark.parametrize("redirected", ["root", "objects", "bucket"])
def test_backup_object_collection_never_follows_directory_links(
    data_dir: Path, tmp_path: Path, redirected: str
) -> None:
    root = config_backup_root(data_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    if redirected == "root":
        link, target = root, outside
        bucket = outside / "objects" / "ff"
    elif redirected == "objects":
        link, target = root / "objects", outside
        bucket = outside / "ff"
    else:
        link, target = root / "objects" / "ff", outside
        bucket = outside
    link.parent.mkdir(parents=True, exist_ok=True)
    bucket.mkdir(parents=True, exist_ok=True)
    operator_file = bucket / "operator-file.txt"
    operator_file.write_text("must survive", encoding="utf-8")
    link_directory(link, target)
    (root / "backups").mkdir(parents=True, exist_ok=True)

    purge_config_backup_files(data_dir, (".env", "oauth/*.json"))

    assert operator_file.read_text(encoding="utf-8") == "must survive"
