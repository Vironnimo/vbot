"""Tests for the validated Skill Policy service."""

import json
import logging
from pathlib import Path

import pytest

from core.database import older_format_hint
from core.skills.policy import (
    POLICY_FORMAT_VERSION,
    SkillPolicy,
    SkillPolicyError,
    SkillPolicyService,
    validate_skill_policy_file,
)
from core.storage.storage import StorageManager


@pytest.fixture
def storage(tmp_path: Path) -> StorageManager:
    return StorageManager(data_dir=tmp_path / "data")


def policy_path(storage: StorageManager) -> Path:
    return storage.data_dir / "skills" / "policy.json"


def write_policy(storage: StorageManager, document: object) -> Path:
    path = policy_path(storage)
    path.parent.mkdir(parents=True)
    if isinstance(document, bytes):
        path.write_bytes(document)
    else:
        path.write_text(json.dumps(document), encoding="utf-8")
    return path


class TestLoad:
    def test_missing_file_means_empty_policy(self, storage: StorageManager) -> None:
        service = SkillPolicyService(storage)

        assert service.load() == SkillPolicy()
        assert service.validation_diagnostics() == []

    def test_loads_valid_document_as_written(self, storage: StorageManager) -> None:
        # The policy knows no roster: owners and Skills that no longer exist stay
        # as written; staleness is resolved where the live Agents are known.
        write_policy(
            storage,
            {
                "format_version": POLICY_FORMAT_VERSION,
                "disabled": ["deploy"],
                "shared": {
                    "ghost-agent": {"deploy": ["two"]},
                    "main": {"vanished-skill": ["two"], "review": ["two", "three"]},
                },
            },
        )
        service = SkillPolicyService(storage)

        policy = service.load()

        assert policy.disabled == frozenset({"deploy"})
        assert policy.shared == {
            "ghost-agent": {"deploy": frozenset({"two"})},
            "main": {
                "vanished-skill": frozenset({"two"}),
                "review": frozenset({"two", "three"}),
            },
        }
        assert service.validation_diagnostics() == []

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            pytest.param(b"{not json", "Cannot read skill policy", id="malformed-json"),
            pytest.param({"version": 2, "disabled": []}, older_format_hint(), id="generation-1"),
            pytest.param(
                {"format_version": 2, "disabled": []}, "written by a newer vBot", id="newer"
            ),
            pytest.param(
                {
                    "format_version": POLICY_FORMAT_VERSION,
                    "disabled": ["deploy"],
                    "shared": {"owner": {"deploy": [42]}},
                },
                "must be a string",
                id="non-string-receiver",
            ),
        ],
    )
    def test_invalid_document_is_ignored_as_a_whole_with_diagnostics(
        self,
        storage: StorageManager,
        caplog: pytest.LogCaptureFixture,
        document: object,
        message: str,
    ) -> None:
        path = write_policy(storage, document)
        service = SkillPolicyService(storage)

        with caplog.at_level("WARNING", logger="vbot.skills"):
            policy = service.load()

        assert policy == SkillPolicy()
        assert any(message in item for item in service.validation_diagnostics())
        assert any(str(path) in logged for logged in caplog.messages)

    def test_an_invalid_file_is_logged_once_per_version_and_when_it_is_usable_again(
        self, storage: StorageManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Every registry rebuild re-reads the policy; the log reports changes only.
        path = write_policy(storage, {"format_version": 2, "disabled": []})
        service = SkillPolicyService(storage)

        def levels_of_loads() -> list[int]:
            caplog.clear()
            with caplog.at_level("INFO", logger="vbot.skills"):
                service.load()
                service.load()
            return [record.levelno for record in caplog.records]

        assert levels_of_loads() == [logging.WARNING]
        path.write_text(json.dumps({"format_version": 2, "disabled": ["x"]}), encoding="utf-8")
        assert levels_of_loads() == [logging.WARNING]
        path.write_text(json.dumps({"format_version": POLICY_FORMAT_VERSION}), encoding="utf-8")
        assert levels_of_loads() == [logging.INFO]

    def test_unknown_keys_warn_but_still_load(self, storage: StorageManager) -> None:
        write_policy(storage, {"format_version": POLICY_FORMAT_VERSION, "legacy_flag": True})
        service = SkillPolicyService(storage)

        assert service.load() == SkillPolicy()
        assert any("unknown key" in message for message in service.validation_diagnostics())

    def test_unusable_names_are_left_out_with_warnings(self, storage: StorageManager) -> None:
        path = write_policy(
            storage,
            {
                "format_version": POLICY_FORMAT_VERSION,
                "disabled": ["bad name!", "good-name"],
                "shared": {"owner": {"also bad!": ["two"], "deploy": ["Not An Id"]}},
            },
        )
        service = SkillPolicyService(storage)

        policy = service.load()

        # Unusable entries drop out of the effective sets; their siblings load.
        assert policy.disabled == frozenset({"good-name"})
        assert policy.shared == {}
        messages = service.validation_diagnostics()
        assert sum("ignoring unusable skill name" in message for message in messages) == 2
        # The doctor check reports the same entries as warnings of a usable file.
        report = validate_skill_policy_file(path)
        assert report.ok
        assert [diagnostic.path for diagnostic in report.diagnostics] == [
            "$.disabled[0]",
            "$.shared.owner['also bad!']",
            "$.shared.owner.deploy[0]",
        ]


class TestMutations:
    @pytest.mark.parametrize("operation", ["disable", "share"])
    @pytest.mark.parametrize(
        "original", [b"{broken", b'{"format_version": 999}', b'{"format_version":"\xff"}']
    )
    def test_mutation_preserves_invalid_existing_policy(
        self, storage: StorageManager, operation: str, original: bytes
    ) -> None:
        path = write_policy(storage, original)
        service = SkillPolicyService(storage)

        assert service.load() == SkillPolicy()
        with pytest.raises(SkillPolicyError):
            if operation == "disable":
                service.set_disabled("deploy", disabled=True)
            else:
                service.set_shared("main", "deploy", shared=True, receivers=["two"])
        assert path.read_bytes() == original

    @pytest.mark.parametrize("name", ["bad name", "deploy\n", "x" * 65])
    def test_mutation_rejects_skill_names_that_cannot_round_trip(
        self, storage: StorageManager, name: str
    ) -> None:
        service = SkillPolicyService(storage)
        with pytest.raises(SkillPolicyError):
            service.set_disabled(name, disabled=True)
        with pytest.raises(SkillPolicyError):
            service.set_shared("main", name, shared=True, receivers=["two"])
        assert not policy_path(storage).exists()

    @pytest.mark.parametrize("receivers", [[], ["bad name"], ["two\n"], ["main"]])
    def test_share_rejects_receivers_that_cannot_round_trip(
        self, storage: StorageManager, receivers: list[str]
    ) -> None:
        service = SkillPolicyService(storage)
        with pytest.raises(SkillPolicyError):
            service.set_shared("main", "deploy", shared=True, receivers=receivers)
        assert not policy_path(storage).exists()

    def test_set_disabled_persists_and_toggles(self, storage: StorageManager) -> None:
        service = SkillPolicyService(storage)

        service.set_disabled("deploy", disabled=True)

        document = json.loads(policy_path(storage).read_text(encoding="utf-8"))
        assert document == {
            "format_version": POLICY_FORMAT_VERSION,
            "disabled": ["deploy"],
            "shared": {},
        }
        assert service.load().disabled == frozenset({"deploy"})

        service.set_disabled("deploy", disabled=False)

        document = json.loads(policy_path(storage).read_text(encoding="utf-8"))
        assert document["disabled"] == []
        assert service.load() == SkillPolicy()

    def test_mutations_write_unknown_fields_and_unusable_entries_back_unchanged(
        self, storage: StorageManager
    ) -> None:
        path = write_policy(
            storage,
            {
                "format_version": POLICY_FORMAT_VERSION,
                "future": {"kept": True},
                "disabled": ["bad name!", "old"],
                "shared": {
                    "main": {"also bad!": ["two"], "notes": ["Not An Id", "two"]},
                    "other": {"deploy": []},
                },
            },
        )
        service = SkillPolicyService(storage)

        service.set_disabled("deploy", disabled=True)
        service.set_disabled("old", disabled=False)
        service.set_shared("main", "review", shared=True, receivers=["two"])

        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["future"] == {"kept": True}
        assert document["disabled"] == ["bad name!", "deploy"]
        assert document["shared"] == {
            "main": {
                "also bad!": ["two"],
                "notes": ["Not An Id", "two"],
                "review": ["two"],
            },
            "other": {"deploy": []},
        }
        # The effective policy still leaves the unusable entries out.
        assert service.load() == SkillPolicy(
            disabled=frozenset({"deploy"}),
            shared={"main": {"notes": frozenset({"two"}), "review": frozenset({"two"})}},
        )

    def test_sharing_groups_by_owner_beside_disabled_names(self, storage: StorageManager) -> None:
        service = SkillPolicyService(storage)

        service.set_shared("main", "notes", shared=True, receivers=["two"])
        service.set_shared("two", "deploy", shared=True, receivers=["main"])
        service.set_disabled("other", disabled=True)

        policy = service.load()
        assert policy.disabled == frozenset({"other"})
        assert policy.shared == {
            "main": {"notes": frozenset({"two"})},
            "two": {"deploy": frozenset({"main"})},
        }

        service.set_shared("main", "notes", shared=False)

        # An owner without shared Skills is dropped.
        assert service.load().shared == {"two": {"deploy": frozenset({"main"})}}

    def test_write_failure_raises_skill_policy_error(
        self, storage: StorageManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = SkillPolicyService(storage)

        def fail_write(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("core.json_documents.atomic_write_text", fail_write)

        with pytest.raises(SkillPolicyError):
            service.set_disabled("deploy", disabled=True)
