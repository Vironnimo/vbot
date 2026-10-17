"""The Secret Store: sealed entries, tamper detection, and stores that cannot be opened."""

from __future__ import annotations

import base64
import json
import logging
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from core.secrets import (
    CREDENTIALS,
    OAUTH_TOKENS,
    KeyFileProtection,
    SecretEntryUnavailableError,
    SecretStoreError,
    SecretStoreUnavailableError,
)
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
    key_file = Path(str(old_status.key_location))
    hidden = key_file.rename(tmp_path / "hidden.key")
    original = store.path.read_bytes()
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


def test_concurrent_writers_keep_every_entry(tmp_path: Path) -> None:
    data_directory(tmp_path)
    store = open_store(tmp_path)
    names = [f"KEY_{index}" for index in range(24)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda name: store.set_entry(CREDENTIALS, name, f"value-{name}"), names))

    assert open_store(tmp_path).entries(CREDENTIALS) == {name: f"value-{name}" for name in names}
