"""Data-directory credentials: the ``.env`` file beside the process environment."""

import errno
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from core.storage import DataDirectoryLayout, StorageError, StorageManager
from core.utils.atomic import atomic_write_text

CREDENTIAL_MUTATION_CONTENTION_SECONDS = 0.1


THREAD_START_TIMEOUT_SECONDS = 10.0


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
        # Check serialization, not disk latency under the parallel suite's load.
        # The suite-wide pytest timeout still bounds a deadlock.
        first_future.result()
        second_future.result()


def test_environment_file_is_a_fallback_that_never_mutates_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-process")
    monkeypatch.delenv("DATA_ONLY", raising=False)
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-from-data-dir\nDATA_ONLY=from-data-dir\n",
        encoding="utf-8",
    )

    loaded = storage.load_environment()
    snapshot = storage.build_environment_snapshot()

    assert loaded == {"OPENROUTER_API_KEY": "sk-or-from-data-dir", "DATA_ONLY": "from-data-dir"}
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-process"
    assert "DATA_ONLY" not in os.environ
    assert snapshot["OPENROUTER_API_KEY"] == "sk-or-from-process"
    assert snapshot["DATA_ONLY"] == "from-data-dir"


def test_first_credential_is_added_to_the_seeded_environment_template(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    storage.set_data_dir_credential("OPENROUTER_API_KEY", "sk-or-test")

    environment_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-test\n" in environment_text
    assert "OPENAI_API_KEY=\n" in environment_text
    assert storage.load_environment()["OPENROUTER_API_KEY"] == "sk-or-test"


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("set", "# Provider keys\nOPENROUTER_API_KEY=new\nOTHER_KEY=value\n"),
        ("remove", "# Provider keys\nOTHER_KEY=value\n"),
    ],
)
def test_credential_mutation_rewrites_every_duplicate_and_keeps_other_lines(
    tmp_path: Path, operation: str, expected: str
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(
        "# Provider keys\nOPENROUTER_API_KEY=old\nOTHER_KEY=value\nOPENROUTER_API_KEY=duplicate\n",
        encoding="utf-8",
    )

    if operation == "set":
        storage.set_data_dir_credential("OPENROUTER_API_KEY", "new")
    else:
        assert storage.remove_data_dir_credential("OPENROUTER_API_KEY") is True

    assert (tmp_path / ".env").read_text(encoding="utf-8") == expected


def test_removing_an_absent_credential_reports_false_without_writing(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    assert storage.remove_data_dir_credential("OPENROUTER_API_KEY") is False
    (tmp_path / ".env").write_text("OTHER_KEY=value\n", encoding="utf-8")

    assert storage.remove_data_dir_credential("OPENROUTER_API_KEY") is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OTHER_KEY=value\n"


@pytest.mark.parametrize(
    "value",
    [
        '"literal-secret"',
        "'literal-secret'",
        '"leading-quote',
        "trailing-quote'",
        " secret with surrounding whitespace \t",
        ' "mixed\'quotes" ',
        r"literal\n\t\path=${NO_EXPANSION}",
        "first=second#literal",
        # Line boundaries for str.splitlines(), never for the dotenv file.
        *[f"first{separator}SECOND_KEY=second" for separator in "\v\x85 "],
    ],
)
def test_credentials_round_trip_literal_values_through_other_mutations(
    tmp_path: Path, value: str
) -> None:
    storage = StorageManager(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING_KEY=retained\n", encoding="utf-8")

    storage.set_data_dir_credential("LITERAL_KEY", value)
    storage.set_data_dir_credential("OTHER_KEY", "temporary")
    assert storage.remove_data_dir_credential("OTHER_KEY") is True

    assert storage.load_data_dir_credentials() == {
        "EXISTING_KEY": "retained",
        "LITERAL_KEY": value,
    }
    assert storage.remove_data_dir_credential("LITERAL_KEY") is True
    assert storage.load_data_dir_credentials() == {"EXISTING_KEY": "retained"}


@pytest.mark.parametrize(
    ("initial", "second", "expected"),
    [
        (
            "EXISTING_KEY=value\n",
            lambda storage: storage.set_data_dir_credential("SECOND_KEY", "second"),
            {"EXISTING_KEY": "value", "KEEP_KEY": "new", "SECOND_KEY": "second"},
        ),
        (
            "REMOVE_KEY=old\n",
            lambda storage: storage.remove_data_dir_credential("REMOVE_KEY"),
            {"KEEP_KEY": "new"},
        ),
    ],
    ids=["set-and-set", "set-and-remove"],
)
def test_concurrent_credential_mutations_preserve_both_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    initial: str,
    second: Callable[[StorageManager], object],
    expected: dict[str, str],
) -> None:
    storage = StorageManager(tmp_path)
    (tmp_path / ".env").write_text(initial, encoding="utf-8")

    run_overlapping_credential_mutations(
        monkeypatch,
        lambda: storage.set_data_dir_credential("KEEP_KEY", "new"),
        lambda: second(storage),
    )

    assert storage.load_data_dir_credentials() == expected


@pytest.mark.parametrize("failure", ["read", "replace"])
def test_set_data_dir_credential_preserves_env_it_cannot_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    storage = StorageManager(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("OPENROUTER_API_KEY=old\nOTHER_KEY=value\n", encoding="utf-8")
    replace_calls: list[tuple[Path, Path]] = []
    original_replace = os.replace
    original_exists = os.path.exists
    original_read_text = Path.read_text

    def fail_replace(source: Path, target: Path) -> None:
        replace_calls.append((source, target))
        # Only the .env replace fails; the data-store marker uses its own replace.
        if target == env_path:
            raise OSError("replace failed")
        return original_replace(source, target)

    def exists_unless_env(path: Any) -> bool:
        return Path(path) != env_path and original_exists(path)

    def refuse_env(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == env_path:
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return original_read_text(path, *args, **kwargs)

    if failure == "read":
        # A file whose check fails reads as missing (Python 3.14), but it is there.
        monkeypatch.setattr(os.path, "exists", exists_unless_env)
        monkeypatch.setattr(Path, "read_text", refuse_env)
    else:
        monkeypatch.setattr("core.utils.atomic.os.replace", fail_replace)

    with pytest.raises(StorageError):
        storage.set_data_dir_credential("OPENROUTER_API_KEY", "new")
    monkeypatch.undo()

    assert (failure == "replace") == any(target == env_path for _, target in replace_calls)
    assert env_path.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=old\nOTHER_KEY=value\n"
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
