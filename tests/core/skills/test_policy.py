"""Tests for the validated Skill Policy service."""

import json
from pathlib import Path

import pytest

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
                    "format_version": POLICY_FORMAT_VERSION,
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

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ({"version": 2, "disabled": []}, "persistence Generation 1"),
            ({"format_version": 2, "disabled": []}, "written by a newer vBot"),
        ],
    )
    def test_unsupported_version_is_invalid(
        self, storage: StorageManager, document: dict[str, object], message: str
    ) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        service = SkillPolicyService(storage)

        policy = service.load()

        assert policy == SkillPolicy()
        diagnostics = service.validation_diagnostics()
        assert any(message in item for item in diagnostics)

    def test_unknown_keys_warn_but_still_load(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"format_version": POLICY_FORMAT_VERSION, "legacy_flag": True}),
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
                    "format_version": POLICY_FORMAT_VERSION,
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

    def test_doctor_reports_entries_the_effective_policy_ignores(
        self, storage: StorageManager
    ) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "format_version": POLICY_FORMAT_VERSION,
                    "disabled": ["bad name!"],
                    "shared": {"owner": {"deploy": ["Not An Id"]}},
                }
            ),
            encoding="utf-8",
        )

        report = validate_skill_policy_file(path)

        assert report.ok
        assert [diagnostic.path for diagnostic in report.diagnostics] == [
            "$.disabled[0]",
            "$.shared.owner.deploy[0]",
        ]
        assert SkillPolicyService(storage).load() == SkillPolicy()

    def test_non_string_receivers_are_shape_errors(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "format_version": POLICY_FORMAT_VERSION,
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
    @pytest.mark.parametrize("operation", ["disable", "share"])
    @pytest.mark.parametrize(
        "original", [b"{broken", b'{"format_version": 999}', b'{"format_version":"\xff"}']
    )
    def test_mutation_preserves_invalid_existing_policy(
        self, storage: StorageManager, operation: str, original: bytes
    ) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
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

    def test_set_disabled_persists_atomically_and_toggles(self, storage: StorageManager) -> None:
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

    def test_mutation_keeps_unknown_fields_of_the_file(self, storage: StorageManager) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"format_version": POLICY_FORMAT_VERSION, "future": {"kept": True}}),
            encoding="utf-8",
        )
        service = SkillPolicyService(storage)

        service.set_disabled("deploy", disabled=True)

        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["future"] == {"kept": True}
        assert document["disabled"] == ["deploy"]

    def test_mutations_write_unusable_stored_entries_back_unchanged(
        self, storage: StorageManager
    ) -> None:
        path = policy_path(storage)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "format_version": POLICY_FORMAT_VERSION,
                    "disabled": ["bad name!", "old"],
                    "shared": {
                        "main": {"also bad!": ["two"], "notes": ["Not An Id", "two"]},
                        "other": {"deploy": []},
                    },
                }
            ),
            encoding="utf-8",
        )
        service = SkillPolicyService(storage)

        service.set_disabled("deploy", disabled=True)
        service.set_disabled("old", disabled=False)
        service.set_shared("main", "review", shared=True, receivers=["two"])

        document = json.loads(path.read_text(encoding="utf-8"))
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

        monkeypatch.setattr("core.json_documents.atomic_write_text", fail_write)

        with pytest.raises(SkillPolicyError):
            service.set_disabled("deploy", disabled=True)
