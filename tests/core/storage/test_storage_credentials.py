"""Tests for storage credentials."""

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.storage import (
    DATA_DIRECTORY_RELATIVE_PATHS,
    DataDirectoryLayout,
    StorageError,
    StorageManager,
)
from core.utils.atomic import atomic_write_text

CREDENTIAL_MUTATION_CONTENTION_SECONDS = 0.1


THREAD_START_TIMEOUT_SECONDS = 1.0


THREAD_RESULT_TIMEOUT_SECONDS = 2.0


def run_overlapping_credential_mutations(
    monkeypatch: pytest.MonkeyPatch,
    first_mutation: Callable[[], object],
    second_mutation: Callable[[], object],
) -> None:
    """Force the second mutation to contend while the first write is pending."""
    first_write_started = threading.Event()
    second_mutation_started = threading.Event()
    second_mutation_finished = threading.Event()
    write_count = 0
    write_count_lock = threading.Lock()

    def delayed_atomic_write_text(
        target_path: Path,
        text: str,
        *,
        data_dir: Path | None = None,
        encoding: str = "utf-8",
    ) -> None:
        nonlocal write_count
        with write_count_lock:
            write_count += 1
            current_write = write_count
        if current_write == 1:
            first_write_started.set()
            assert second_mutation_started.wait(THREAD_START_TIMEOUT_SECONDS)
            second_mutation_finished.wait(CREDENTIAL_MUTATION_CONTENTION_SECONDS)
        atomic_write_text(target_path, text, data_dir=data_dir, encoding=encoding)

    def run_second_mutation() -> object:
        second_mutation_started.set()
        try:
            return second_mutation()
        finally:
            second_mutation_finished.set()

    monkeypatch.setattr(
        "core.storage.storage.atomic_write_text",
        delayed_atomic_write_text,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_mutation)
        assert first_write_started.wait(THREAD_START_TIMEOUT_SECONDS)
        second_future = executor.submit(run_second_mutation)
        first_future.result(timeout=THREAD_RESULT_TIMEOUT_SECONDS)
        second_future.result(timeout=THREAD_RESULT_TIMEOUT_SECONDS)


class ConfigWithDataDir:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def get(self, key: str, default=None):
        return default


class ConfigWithValues:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def test_ensure_directories_creates_canonical_structure(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.ensure_directories()

    assert tmp_path.is_dir()
    assert all((tmp_path / directory).is_dir() for directory in DATA_DIRECTORY_RELATIVE_PATHS)


def test_load_environment_reads_data_dir_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-data-dir\n", encoding="utf-8")

    loaded = storage.load_environment()

    assert loaded == {"OPENROUTER_API_KEY": "sk-or-from-data-dir"}
    assert "OPENROUTER_API_KEY" not in os.environ


def test_load_environment_does_not_overwrite_existing_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-data-dir\n", encoding="utf-8")

    loaded = storage.load_environment()

    assert loaded == {"OPENROUTER_API_KEY": "sk-or-from-data-dir"}
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-process"


def test_build_environment_snapshot_prefers_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    monkeypatch.setenv("PROCESS_ONLY", "from-process")
    monkeypatch.delenv("DATA_ONLY", raising=False)
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\nDATA_ONLY=from-data-dir\n",
        encoding="utf-8",
    )

    snapshot = storage.build_environment_snapshot()

    assert snapshot["OPENROUTER_API_KEY"] == "sk-or-from-process"
    assert snapshot["PROCESS_ONLY"] == "from-process"
    assert snapshot["DATA_ONLY"] == "from-data-dir"


def test_set_data_dir_credential_writes_new_env_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-test")

    environment_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-test\n" in environment_text
    assert "OPENAI_API_KEY=\n" in environment_text
    assert storage.load_environment()["OPENROUTER_API_KEY"] == "sk-or-test"


def test_set_data_dir_credential_replaces_existing_key_and_preserves_other_lines(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "# Provider keys\nOPENROUTER_API_KEY=old\nOTHER_KEY=value\nOPENROUTER_API_KEY=duplicate\n",
        encoding="utf-8",
    )

    storage.set_data_dir_credential("OPENROUTER_API_KEY", "new")

    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "# Provider keys\nOPENROUTER_API_KEY=new\nOTHER_KEY=value\n"
    )


def test_concurrent_credential_sets_preserve_both_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("EXISTING_KEY=value\n", encoding="utf-8")

    run_overlapping_credential_mutations(
        monkeypatch,
        lambda: storage.set_data_dir_credential("FIRST_KEY", "first"),
        lambda: storage.set_data_dir_credential("SECOND_KEY", "second"),
    )

    assert storage.load_data_dir_credentials() == {
        "EXISTING_KEY": "value",
        "FIRST_KEY": "first",
        "SECOND_KEY": "second",
    }


def test_concurrent_credential_set_and_remove_preserve_both_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("REMOVE_KEY=old\n", encoding="utf-8")

    run_overlapping_credential_mutations(
        monkeypatch,
        lambda: storage.set_data_dir_credential("KEEP_KEY", "new"),
        lambda: storage.remove_data_dir_credential("REMOVE_KEY"),
    )

    assert storage.load_data_dir_credentials() == {"KEEP_KEY": "new"}


def test_set_data_dir_credential_preserves_env_when_atomic_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("OPENROUTER_API_KEY=old\nOTHER_KEY=value\n", encoding="utf-8")
    replace_calls: list[tuple[Path, Path]] = []
    import os as _os_module

    _original_replace = _os_module.replace

    def fail_replace(source: Path, target: Path) -> None:
        replace_calls.append((source, target))
        # Only fail the .env atomic replace; the session marker uses a
        # separate ``os.replace`` path and must not be affected.
        if target == env_path:
            raise OSError("replace failed")
        # Fall back to the real replace for marker and other files.
        return _original_replace(source, target)

    monkeypatch.setattr("core.utils.atomic.os.replace", fail_replace)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential("OPENROUTER_API_KEY", "new")

    assert any(target == env_path for _, target in replace_calls)
    assert env_path.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=old\nOTHER_KEY=value\n"
    assert list(DataDirectoryLayout(tmp_path).atomic_temporary.iterdir()) == []


@pytest.mark.parametrize("key", ["", "1BAD", "BAD-NAME", "BAD NAME"])
def test_set_data_dir_credential_rejects_invalid_env_key(tmp_path: Path, key: str) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential(key, "secret")


@pytest.mark.parametrize("value", ["", "line\nbreak", "line\rbreak"])
def test_set_data_dir_credential_rejects_invalid_value(tmp_path: Path, value: str) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential("OPENROUTER_API_KEY", value)


def test_remove_data_dir_credential_removes_key_and_preserves_other_lines(
    tmp_path: Path,
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "# Provider keys\nOPENROUTER_API_KEY=old\nOTHER_KEY=value\nOPENROUTER_API_KEY=duplicate\n",
        encoding="utf-8",
    )

    removed = storage.remove_data_dir_credential("OPENROUTER_API_KEY")

    assert removed is True
    assert (tmp_path / ".env").read_text(encoding="utf-8") == ("# Provider keys\nOTHER_KEY=value\n")


def test_remove_data_dir_credential_returns_false_for_missing_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text("OTHER_KEY=value\n", encoding="utf-8")

    removed = storage.remove_data_dir_credential("OPENROUTER_API_KEY")

    assert removed is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OTHER_KEY=value\n"


def test_remove_data_dir_credential_returns_false_without_env_file(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    assert storage.remove_data_dir_credential("OPENROUTER_API_KEY") is False


@pytest.mark.parametrize("key", ["", "1BAD", "BAD-NAME", "BAD NAME"])
def test_remove_data_dir_credential_rejects_invalid_env_key(tmp_path: Path, key: str) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.remove_data_dir_credential(key)


def test_resolves_data_dir_from_config_attribute(tmp_path: Path) -> None:
    data_dir = tmp_path / "configured"
    storage = StorageManager(config=ConfigWithDataDir(data_dir))

    assert storage.data_dir == data_dir


def test_resolves_data_dir_from_config_value(tmp_path: Path) -> None:
    data_dir = tmp_path / "from-value"
    storage = StorageManager(config=ConfigWithValues({"DATA_DIR": str(data_dir)}))

    assert storage.data_dir == data_dir
