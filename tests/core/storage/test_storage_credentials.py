"""Data-directory credentials: the Secret Store beside the process environment."""

import os
from pathlib import Path

import pytest

from core.secrets import SECRET_STORE_FILE_NAME
from core.storage import DataDirectoryLayout, StorageError, StorageManager


def test_stored_credentials_are_a_fallback_that_never_mutates_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    monkeypatch.delenv("DATA_ONLY", raising=False)
    monkeypatch.delenv("PLAINTEXT_ONLY", raising=False)
    storage = StorageManager(tmp_path)
    storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-from-data-dir")
    storage.set_data_dir_credential("DATA_ONLY", "from-data-dir")
    # A plaintext .env is no credential source.
    (tmp_path / ".env").write_text("PLAINTEXT_ONLY=ignored\n", encoding="utf-8")

    loaded = storage.load_environment()
    snapshot = storage.build_environment_snapshot()

    assert loaded == {"OPENROUTER_API_KEY": "sk-or-from-data-dir", "DATA_ONLY": "from-data-dir"}
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-process"
    assert "DATA_ONLY" not in os.environ
    assert snapshot["OPENROUTER_API_KEY"] == "sk-or-from-process"
    assert snapshot["DATA_ONLY"] == "from-data-dir"
    assert "PLAINTEXT_ONLY" not in snapshot
    stored = (tmp_path / SECRET_STORE_FILE_NAME).read_text(encoding="utf-8")
    assert "sk-or-from-data-dir" not in stored
    assert "from-data-dir" not in stored


def test_removing_an_absent_credential_reports_false_without_creating_a_store(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)

    assert storage.remove_data_dir_credential("OPENROUTER_API_KEY") is False
    assert not (tmp_path / SECRET_STORE_FILE_NAME).exists()


@pytest.mark.parametrize(
    "value",
    [
        '"literal-secret"',
        " secret with surrounding whitespace \t",
        r"literal\n\t\path=${NO_EXPANSION}",
        "first=second#literal",
        *[f"first{separator}SECOND_KEY=second" for separator in "\v\x85 "],
    ],
)
def test_credentials_round_trip_literal_values_through_other_mutations(
    tmp_path: Path, value: str
) -> None:
    storage = StorageManager(tmp_path)
    storage.set_data_dir_credential("EXISTING_KEY", "retained")

    storage.set_data_dir_credential("LITERAL_KEY", value)
    storage.set_data_dir_credential("OTHER_KEY", "temporary")
    assert storage.remove_data_dir_credential("OTHER_KEY") is True

    assert StorageManager(tmp_path).load_data_dir_credentials() == {
        "EXISTING_KEY": "retained",
        "LITERAL_KEY": value,
    }
    assert storage.remove_data_dir_credential("LITERAL_KEY") is True
    assert storage.load_data_dir_credentials() == {"EXISTING_KEY": "retained"}


def test_a_failed_credential_write_leaves_the_stored_credentials_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = StorageManager(tmp_path)
    storage.set_data_dir_credential("OPENROUTER_API_KEY", "old")
    store_path = tmp_path / SECRET_STORE_FILE_NAME
    before = store_path.read_bytes()
    original_replace = os.replace

    def fail_replace(source: Path, target: Path) -> None:
        # Only the store's replace fails; the data-store marker uses its own replace.
        if Path(target) == store_path:
            raise OSError("replace failed")
        return original_replace(source, target)

    monkeypatch.setattr("core.utils.atomic.os.replace", fail_replace)
    with pytest.raises(StorageError):
        storage.set_data_dir_credential("OPENROUTER_API_KEY", "new")
    monkeypatch.undo()

    assert store_path.read_bytes() == before
    assert storage.load_data_dir_credentials() == {"OPENROUTER_API_KEY": "old"}
    assert list(DataDirectoryLayout(tmp_path).atomic_temporary.iterdir()) == []


@pytest.mark.parametrize("operation", ["set", "remove"])
@pytest.mark.parametrize("key", ["", "1BAD", "BAD-NAME"])
def test_credential_mutations_reject_invalid_env_keys(
    tmp_path: Path, operation: str, key: str
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        if operation == "set":
            storage.set_data_dir_credential(key, "secret")
        else:
            storage.remove_data_dir_credential(key)


@pytest.mark.parametrize("value", ["", "line\nbreak", "line\rbreak"])
def test_set_data_dir_credential_rejects_invalid_value(tmp_path: Path, value: str) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential("OPENROUTER_API_KEY", value)
