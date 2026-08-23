"""Tests for the validated Skill Policy service."""

import json
from pathlib import Path

import pytest

from core.skills.policy import (
    POLICY_SCHEMA_VERSION,
    SkillPolicy,
    SkillPolicyError,
    SkillPolicyService,
)
from core.storage.storage import StorageManager


@pytest.fixture
def storage(tmp_path: Path) -> StorageManager:
    return StorageManager(data_dir=tmp_path / "data")


def policy_path(storage: StorageManager) -> Path:
    return storage.data_dir / "skills" / "policy.json"


class TestLoad:
    def test_missing_file_means_empty_policy(self, storage: StorageManager) -> None:
        service = SkillPolicyService(storage)

        assert service.load() == SkillPolicy()
        assert service.validation_diagnostics() == []

    def test_loads_valid_document(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": POLICY_SCHEMA_VERSION,
                    "disabled": ["deploy"],
                    "shared": {"main": {"deploy": ["two"], "review": ["two", "three"]}},
                }
            ),
            encoding="utf-8",
        )
        service = SkillPolicyService(storage)

        policy = service.load()

        assert policy.disabled == frozenset({"deploy"})
        assert policy.shared == {
            "main": {"deploy": frozenset({"two"}), "review": frozenset({"two", "three"})},
        }
        assert service.validation_diagnostics() == []

    def test_malformed_json_yields_diagnostics_and_empty_policy(
        self, storage: StorageManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        service = SkillPolicyService(storage)

        with caplog.at_level("WARNING", logger="vbot.skills"):
            policy = service.load()

        assert policy == SkillPolicy()
        assert service.validation_diagnostics()
        assert any("Cannot read skill policy" in message for message in caplog.messages)

    def test_unsupported_version_is_invalid(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "disabled": []}), encoding="utf-8")
        service = SkillPolicyService(storage)

        policy = service.load()

        assert policy == SkillPolicy()
        assert any("must be 2" in message for message in service.validation_diagnostics())

    def test_unknown_keys_warn_but_still_load(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": POLICY_SCHEMA_VERSION, "legacy_flag": True}),
            encoding="utf-8",
        )
        service = SkillPolicyService(storage)

        policy = service.load()

        assert policy == SkillPolicy()
        assert any("unknown key" in message for message in service.validation_diagnostics())

    def test_non_trigger_safe_name_is_ignored_with_warning(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": POLICY_SCHEMA_VERSION,
                    "disabled": ["bad name!", "good-name"],
                    "shared": {"owner": {"also bad!": ["two"]}},
                }
            ),
            encoding="utf-8",
        )
        service = SkillPolicyService(storage)

        policy = service.load()

        # Unusable names are dropped from the effective sets; their siblings load.
        assert policy.disabled == frozenset({"good-name"})
        assert policy.shared == {}
        messages = service.validation_diagnostics()
        assert sum("ignoring unusable skill name" in message for message in messages) == 2

    def test_non_string_receivers_are_shape_errors(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": POLICY_SCHEMA_VERSION,
                    "disabled": ["deploy"],
                    "shared": {"owner": {"deploy": [42]}},
                }
            ),
            encoding="utf-8",
        )
        service = SkillPolicyService(storage)

        policy = service.load()

        # A shape error invalidates the whole document (validated before consumed).
        assert policy == SkillPolicy()
        assert any("must be a string" in message for message in service.validation_diagnostics())


class TestMutations:
    def test_set_disabled_persists_atomically_and_toggles(self, storage: StorageManager) -> None:
        service = SkillPolicyService(storage)

        service.set_disabled("deploy", disabled=True)

        document = json.loads(policy_path(storage).read_text(encoding="utf-8"))
        assert document == {
            "version": POLICY_SCHEMA_VERSION,
            "disabled": ["deploy"],
            "shared": {},
        }
        assert service.load().disabled == frozenset({"deploy"})

        service.set_disabled("deploy", disabled=False)

        document = json.loads(policy_path(storage).read_text(encoding="utf-8"))
        assert document["disabled"] == []
        assert service.load() == SkillPolicy()

    def test_set_disabled_preserves_shared_state(self, storage: StorageManager) -> None:
        service = SkillPolicyService(storage)
        service.set_shared("main", "notes", shared=True, receivers=["two"])

        service.set_disabled("other", disabled=True)

        policy = service.load()
        assert policy.disabled == frozenset({"other"})
        assert policy.shared == {"main": {"notes": frozenset({"two"})}}

    def test_set_shared_groups_by_owner_and_drops_empty_owners(
        self, storage: StorageManager
    ) -> None:
        service = SkillPolicyService(storage)

        service.set_shared("main", "notes", shared=True, receivers=["two"])
        service.set_shared("two", "deploy", shared=True, receivers=["main"])

        policy = service.load()
        assert policy.shared == {
            "main": {"notes": frozenset({"two"})},
            "two": {"deploy": frozenset({"main"})},
        }

        service.set_shared("main", "notes", shared=False)

        policy = service.load()
        assert policy.shared == {"two": {"deploy": frozenset({"main"})}}

    def test_write_failure_raises_skill_policy_error(
        self, storage: StorageManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = SkillPolicyService(storage)

        def fail_write(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("core.skills.policy.atomic_write_text", fail_write)

        with pytest.raises(SkillPolicyError):
            service.set_disabled("deploy", disabled=True)
