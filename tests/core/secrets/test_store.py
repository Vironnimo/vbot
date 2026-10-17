"""The Secret Store: sealed entries, tamper detection, and stores that cannot be opened."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from core.secrets import (
    CREDENTIALS,
    LOCK_FILE_NAME,
    OAUTH_TOKENS,
    KeyFileProtection,
    SecretEntryUnavailableError,
    SecretStoreError,
    SecretStoreUnavailableError,
)
from core.secrets import store as store_module
from tests.core.secrets.secrets_test_support import (
    TOKEN_TEXT,
    data_directory,
    key_directory,
    open_store,
)


def _document(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


def test_entries_round_trip_sealed_in_an_owner_only_document(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="vbot")
    data_directory(tmp_path)
    store = open_store(tmp_path)
    assert store.status().state == "absent"

    assert store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai-value") is True
    assert store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai-value") is False
    assert store.set_entry(CREDENTIALS, "SAME_VALUE", "sk-openai-value") is True
    assert store.set_entry(OAUTH_TOKENS, "github-copilot-oauth", TOKEN_TEXT) is True

    restarted = open_store(tmp_path)
    assert restarted.entries(CREDENTIALS) == {
        "OPENAI_API_KEY": "sk-openai-value",
        "SAME_VALUE": "sk-openai-value",
    }
    assert restarted.entry(OAUTH_TOKENS, "github-copilot-oauth") == TOKEN_TEXT
    assert restarted.entry(CREDENTIALS, "ANTHROPIC_API_KEY") is None
    status = restarted.status()
    assert (status.state, status.protection, status.credentials, status.oauth_tokens) == (
        "available",
        "key_file",
        2,
        1,
    )
    key_file = key_directory(tmp_path) / f"{status.store_id}.key"
    assert status.key_location == str(key_file)

    raw = store.path.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document["format_version"] == 1
    assert "wrapped" not in document["key"]
    # A fresh nonce for every entry: equal values never show as equal ciphertext.
    assert document[CREDENTIALS]["OPENAI_API_KEY"] != document[CREDENTIALS]["SAME_VALUE"]
    for value in ("sk-openai-value", "token-value", "refresh"):
        assert value not in raw
        assert value not in caplog.text
    if sys.platform != "win32":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    assert restarted.remove_entry(CREDENTIALS, "SAME_VALUE") is True
    assert restarted.remove_entry(CREDENTIALS, "SAME_VALUE") is False
    assert store.entry_names(CREDENTIALS) == ("OPENAI_API_KEY",)


def test_a_store_is_created_only_by_a_valid_write_into_its_data_directory(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path)

    # The data directory itself is never created: its marker would be missing.
    with pytest.raises(SecretStoreError, match="does not exist"):
        store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "value")
    assert not (tmp_path / "data").exists()
    data_directory(tmp_path)
    assert store.remove_entry(CREDENTIALS, "OPENAI_API_KEY") is False
    with pytest.raises(SecretStoreError, match="invalid Secret Store entry name"):
        store.set_entry(CREDENTIALS, "NOT A NAME", "value")

    assert store.entries(CREDENTIALS) == {}
    assert not store.path.exists()
    assert not key_directory(tmp_path).exists()


@pytest.mark.parametrize("tamper", ["flipped byte", "copied from another name"])
def test_an_entry_that_does_not_authenticate_reads_as_missing_until_replaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, tamper: str
) -> None:
    data_directory(tmp_path)
    store = open_store(tmp_path)
    store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    store.set_entry(CREDENTIALS, "ANTHROPIC_API_KEY", "sk-anthropic")
    document = _document(store.path)
    entries = document[CREDENTIALS]
    if tamper == "flipped byte":
        sealed = bytearray(base64.b64decode(entries["OPENAI_API_KEY"]))
        sealed[-1] ^= 0x01
        entries["OPENAI_API_KEY"] = base64.b64encode(bytes(sealed)).decode("ascii")
    else:
        # A valid ciphertext of this store, bound to another name.
        entries["OPENAI_API_KEY"] = entries["ANTHROPIC_API_KEY"]
    store.path.write_text(json.dumps(document), encoding="utf-8")
    damaged = entries["OPENAI_API_KEY"]

    restarted = open_store(tmp_path)

    assert restarted.status().state == "available"
    assert restarted.status().damaged == ("credentials/OPENAI_API_KEY",)
    assert restarted.entries(CREDENTIALS) == {"ANTHROPIC_API_KEY": "sk-anthropic"}
    assert restarted.entry_names(CREDENTIALS) == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    with pytest.raises(SecretEntryUnavailableError):
        restarted.entry(CREDENTIALS, "OPENAI_API_KEY")
    restarted.set_entry(CREDENTIALS, "OTHER_KEY", "other")
    assert _document(store.path)[CREDENTIALS]["OPENAI_API_KEY"] == damaged
    assert _warnings(caplog) == [
        "Secret Store entries do not decrypt and are treated as missing until replaced "
        f"(store={store.path} entries=credentials/OPENAI_API_KEY)"
    ]

    restarted.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-replaced")

    assert restarted.status().damaged == ()
    assert open_store(tmp_path).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-replaced"


@pytest.mark.parametrize(
    ("problem", "reason"),
    [
        ("missing key", "the key file is missing"),
        ("another key", "no key file holds the store's key"),
        ("wrong key", "a different key was found"),
        ("foreign protection", "is not available on this system"),
        ("damaged document", "secrets.json failed to load"),
    ],
)
def test_a_store_that_cannot_be_opened_is_never_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, problem: str, reason: str
) -> None:
    data_directory(tmp_path)
    store = open_store(tmp_path)
    store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    key_file = Path(str(store.status().key_location))
    document = _document(store.path)
    if problem == "missing key":
        key_file.unlink()
    elif problem == "another key":
        # Another store's key in its place, as after restoring the wrong key file.
        key_file.unlink()
        KeyFileProtection(path=key_file).create("another-store")
    elif problem == "wrong key":
        payload = json.loads(key_file.read_text(encoding="utf-8"))
        payload["key"] = base64.b64encode(bytes(32)).decode("ascii")
        key_file.write_text(json.dumps(payload), encoding="utf-8")
    elif problem == "foreign protection":
        # A data directory copied from a Windows computer.
        document["key"]["protection"] = "dpapi"
        store.path.write_text(json.dumps(document), encoding="utf-8")
    else:
        store.path.write_text(json.dumps(document)[:-20], encoding="utf-8")
    store_bytes = store.path.read_bytes()
    key_bytes = key_file.read_bytes() if key_file.exists() else None

    restarted = open_store(tmp_path)

    status = restarted.status()
    assert status.state == "unavailable"
    assert reason in str(status.reason)
    assert restarted.entries(CREDENTIALS) == {}
    with pytest.raises(SecretStoreUnavailableError, match="secrets reset --yes"):
        restarted.entry(CREDENTIALS, "OPENAI_API_KEY")
    with pytest.raises(SecretStoreUnavailableError):
        restarted.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-new")
    with pytest.raises(SecretStoreUnavailableError):
        restarted.remove_entry(CREDENTIALS, "OPENAI_API_KEY")
    assert store.path.read_bytes() == store_bytes
    assert (key_file.read_bytes() if key_file.exists() else None) == key_bytes
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "vbot data-store secrets reset --yes" in warnings[0]
    assert "sk-openai" not in caplog.text


def test_a_reset_quarantines_the_store_which_its_key_still_opens(tmp_path: Path) -> None:
    data_dir = data_directory(tmp_path)
    store = open_store(tmp_path)
    store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    old_status = store.status()
    original = store.path.read_bytes()
    # A store this process opens is never replaced, whatever an earlier check said.
    with pytest.raises(SecretStoreError, match="reset only replaces"):
        store.reset()
    assert store.path.read_bytes() == original
    key_file = Path(str(old_status.key_location))
    hidden = key_file.rename(tmp_path / "hidden.key")
    unavailable = open_store(tmp_path)
    assert unavailable.status().state == "unavailable"

    reset = unavailable.reset()

    assert reset.quarantine is not None
    assert reset.quarantine.parent == data_dir / "quarantine" / "secret-store"
    assert (reset.quarantine / "secrets.json").read_bytes() == original
    assert reset.status.state == "available"
    assert reset.status.credentials == 0
    assert reset.status.store_id != old_status.store_id
    unavailable.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-entered-again")
    hidden.rename(key_file)
    assert open_store(tmp_path, reset.quarantine).entry(CREDENTIALS, "OPENAI_API_KEY") == (
        "sk-openai"
    )
    assert open_store(tmp_path).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-entered-again"


def test_concurrent_writers_keep_every_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = data_directory(tmp_path)
    # Separate instances share nothing in memory, as writers in separate processes.
    stores = [open_store(tmp_path) for _writer in range(3)]
    names = [f"KEY_{index}" for index in range(24)]

    if sys.platform == "win32":
        import msvcrt

        # Another first writer can hold byte 0 while the lock file is still empty.
        with (data_dir / LOCK_FILE_NAME).open("w+b") as held_empty:
            msvcrt.locking(held_empty.fileno(), msvcrt.LK_NBLCK, 1)
            waits = []

            def release_first_writer(delay: float) -> None:
                waits.append(delay)
                msvcrt.locking(held_empty.fileno(), msvcrt.LK_UNLCK, 1)

            with monkeypatch.context() as patch:
                patch.setattr(store_module, "_sleep", release_first_writer)
                stores[0].set_entry(CREDENTIALS, names[0], f"value-{names[0]}")
            assert len(waits) == 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: stores[index % len(stores)].set_entry(
                    CREDENTIALS, names[index], f"value-{names[index]}"
                ),
                range(len(names)),
            )
        )

    assert open_store(tmp_path).entries(CREDENTIALS) == {name: f"value-{name}" for name in names}

    # A writer that holds the lock too long makes a write fail, never overwrite.
    monkeypatch.setattr(store_module, "_WRITE_LOCK_TIMEOUT_SECONDS", 0.2)
    written = stores[0].path.read_bytes()
    held = store_module._acquire_write_lock(data_dir / LOCK_FILE_NAME)
    try:
        with pytest.raises(SecretStoreError, match="another process has been writing"):
            stores[1].set_entry(CREDENTIALS, "LATE_KEY", "late")
    finally:
        store_module._release_write_lock(held)
    assert stores[0].path.read_bytes() == written
    stores[1].set_entry(CREDENTIALS, "LATE_KEY", "late")
    assert stores[2].entry(CREDENTIALS, "LATE_KEY") == "late"


@pytest.mark.parametrize("failing", ["document", "key file"])
def test_an_io_error_while_reading_is_retried_and_never_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    data_directory(tmp_path)
    store = open_store(tmp_path)
    store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    key_file = Path(str(store.status().key_location))
    # Older than the racy window, so an unchanged document may be served from memory.
    os.utime(store.path, (time.time() - 60, time.time() - 60))
    written = store.path.read_bytes()
    target = store.path if failing == "document" else key_file
    failures = {"left": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(store_module, "_sleep", sleeps.append)
    real_open = Path.open

    def busy_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == target and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(13, "The file is in use by another process", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", busy_open)

    # A scanner that holds the file briefly is waited out.
    failures["left"] = 1
    started = open_store(tmp_path)
    assert started.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
    assert sleeps

    # One that holds it longer makes this access fail, never a write or a reset.
    failures["left"] = 1_000
    busy = open_store(tmp_path)
    status = busy.status()
    assert (status.state, busy.entries(CREDENTIALS)) == ("unavailable", {})
    assert "cannot be read right now" in str(status.reason)
    with pytest.raises(SecretStoreUnavailableError, match="cannot be read right now"):
        busy.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-new")
    with pytest.raises(SecretStoreError, match="cannot be checked right now"):
        busy.reset()

    # Once the file is released, the same process reads it again: nothing was remembered.
    failures["left"] = 0
    assert store.path.read_bytes() == written
    assert busy.status().state == "available"
    assert busy.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
