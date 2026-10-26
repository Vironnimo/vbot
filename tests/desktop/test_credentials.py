"""The Desktop's stored sign-ins: one device token per server origin (desktop.credentials)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from desktop.credentials import DEVICE_TOKENS_FILE_NAME, DeviceTokenStore

ORIGIN = "https://vbot.example.com"
OTHER_ORIGIN = "http://pi.lan:8420"
DEVICE_ID = "dev_0123456789abcdef"
TOKEN = f"vbt_{DEVICE_ID}_{'S' * 43}"
OTHER_DEVICE_ID = "dev_fedcba9876543210"


def _store(tmp_path: Path) -> DeviceTokenStore:
    return DeviceTokenStore(tmp_path / DEVICE_TOKENS_FILE_NAME)


def test_a_stored_sign_in_survives_a_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_device(ORIGIN, TOKEN, DEVICE_ID)

    stored = _store(tmp_path).get(ORIGIN)

    assert stored is not None
    assert (stored.token, stored.device_id) == (TOKEN, DEVICE_ID)
    assert TOKEN not in repr(stored)


def test_computer_registration_is_private_and_bound_to_one_pairing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_device(ORIGIN, TOKEN, DEVICE_ID)
    identity = store.computer_identity(ORIGIN, create=True)
    assert identity is not None
    assert identity.key not in repr(identity)
    assert _store(tmp_path).computer_identity(ORIGIN) == identity
    if sys.platform == "win32":
        assert identity.key not in (tmp_path / DEVICE_TOKENS_FILE_NAME).read_text(encoding="utf-8")
    store.save_device(ORIGIN, TOKEN, DEVICE_ID)
    assert store.computer_identity(ORIGIN, create=True) == identity
    store.save_device(ORIGIN, f"vbt_{OTHER_DEVICE_ID}_{'N' * 43}", OTHER_DEVICE_ID)
    replacement = store.computer_identity(ORIGIN, create=True)
    assert replacement is not None and replacement.computer_id != identity.computer_id
    store.remove(ORIGIN)
    assert store.computer_identity(ORIGIN) is None
    later = store.computer_identity(ORIGIN, create=True)
    assert later is not None and later.computer_id != replacement.computer_id
    assert _store(tmp_path).get(OTHER_ORIGIN) is None


@pytest.mark.parametrize("replace_on_unlock", [False, True])
def test_only_the_refused_token_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_on_unlock: bool
) -> None:
    store = _store(tmp_path)
    store.save_device(ORIGIN, TOKEN, DEVICE_ID)
    store.save_device(OTHER_ORIGIN, TOKEN, DEVICE_ID)

    assert store.drop_device(ORIGIN, f"vbt_{DEVICE_ID}_{'X' * 43}") is False
    replacement = f"vbt_{DEVICE_ID}_{'N' * 43}"
    if replace_on_unlock:
        original_lock = store._lock

        class ReplaceOnUnlock:
            pending = True

            def __enter__(self) -> None:
                original_lock.acquire()

            def __exit__(self, *_args: object) -> None:
                original_lock.release()
                if self.pending:
                    self.pending = False
                    # Model a successful pairing that acquires the lock as
                    # soon as the rejected request releases it.
                    store.save_device(ORIGIN, replacement, DEVICE_ID)

        monkeypatch.setattr(store, "_lock", ReplaceOnUnlock())
    assert store.drop_device(ORIGIN, TOKEN) is True

    current = _store(tmp_path).get(ORIGIN)
    assert (None if current is None else current.token) == (
        replacement if replace_on_unlock else None
    )
    other = _store(tmp_path).get(OTHER_ORIGIN)
    assert other is not None and other.token == TOKEN


def test_removing_a_sign_in_returns_what_was_stored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_device(ORIGIN, TOKEN, DEVICE_ID)
    store.save_device(OTHER_ORIGIN, TOKEN, DEVICE_ID)

    removed = store.remove(ORIGIN)

    assert removed is not None and removed.token == TOKEN
    assert store.remove(ORIGIN) is None
    assert _store(tmp_path).get(ORIGIN) is None
    assert _store(tmp_path).get(OTHER_ORIGIN) is not None


@pytest.mark.parametrize(
    ("token", "device_id"),
    [
        ("vbl_" + "L" * 43, DEVICE_ID),
        (TOKEN, OTHER_DEVICE_ID),
        (TOKEN + "x", DEVICE_ID),
    ],
    ids=["local-access-token", "other-device", "malformed"],
)
def test_only_a_device_token_of_its_device_is_stored(
    tmp_path: Path, token: str, device_id: str
) -> None:
    with pytest.raises(ValueError):
        _store(tmp_path).save_device(ORIGIN, token, device_id)

    assert not (tmp_path / DEVICE_TOKENS_FILE_NAME).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists only on Windows")
def test_on_windows_the_token_is_wrapped_for_the_user_and_its_origin(tmp_path: Path) -> None:
    _store(tmp_path).save_device(ORIGIN, TOKEN, DEVICE_ID)
    path = tmp_path / DEVICE_TOKENS_FILE_NAME

    text = path.read_text(encoding="utf-8")
    assert TOKEN not in text
    record = json.loads(text)["servers"][ORIGIN]
    assert "token" not in record and record["token_dpapi"]

    # A wrapped token copied to another origin does not open there.
    document = json.loads(text)
    document["servers"][OTHER_ORIGIN] = record
    path.write_text(json.dumps(document), encoding="utf-8")
    stored = _store(tmp_path).get(OTHER_ORIGIN)
    assert stored is not None and stored.token is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_elsewhere_the_token_file_is_owner_only(tmp_path: Path) -> None:
    _store(tmp_path).save_device(ORIGIN, TOKEN, DEVICE_ID)
    path = tmp_path / DEVICE_TOKENS_FILE_NAME

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["servers"][ORIGIN]["token"] == TOKEN


def test_fields_a_later_desktop_added_are_kept(tmp_path: Path) -> None:
    path = tmp_path / DEVICE_TOKENS_FILE_NAME
    path.write_text(
        json.dumps(
            {
                "format_version": 2,
                "future": {"kept": True},
                "servers": {OTHER_ORIGIN: {"future_field": 1}},
            }
        ),
        encoding="utf-8",
    )

    _store(tmp_path).save_device(ORIGIN, TOKEN, DEVICE_ID)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["format_version"] == 2
    assert document["future"] == {"kept": True}
    assert document["servers"][OTHER_ORIGIN] == {"future_field": 1}
    assert _store(tmp_path).get(ORIGIN) is not None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"servers": {ORIGIN: {"token": f"vbt_{OTHER_DEVICE_ID}_{'S' * 43}"}}}),
        json.dumps({"servers": {ORIGIN: {"token": TOKEN, "device_id": OTHER_DEVICE_ID}}}),
    ],
    ids=["damaged", "not-an-object", "token-without-device", "token-of-another-device"],
)
def test_an_unusable_sign_in_reads_as_signed_out(tmp_path: Path, content: str) -> None:
    (tmp_path / DEVICE_TOKENS_FILE_NAME).write_text(content, encoding="utf-8")
    store = _store(tmp_path)

    stored = store.get(ORIGIN)
    assert stored is None or stored.token is None

    store.save_device(ORIGIN, TOKEN, DEVICE_ID)
    stored = _store(tmp_path).get(ORIGIN)
    assert stored is not None and stored.token == TOKEN
