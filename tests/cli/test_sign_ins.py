"""The CLI's stored Device sign-ins: round trip, protection on disk, and damaged files."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from cli.sign_ins import PendingPairing, SignIn, SignInStore, SignInStoreError

ORIGIN = "https://vbot.example.com"
TOKEN = "vbt_dev7_" + "t" * 43
SECRET = "poll-secret-" + "s" * 30


def test_a_sign_in_round_trips_and_stays_readable_by_its_owner_only(tmp_path: Path) -> None:
    store = SignInStore(tmp_path / "vbot" / "sign-ins.json")
    pairing = PendingPairing(ORIGIN, "pr_1", SECRET, "K7QM-4XPA", "CLI on desk", 1.8e9, 2.0)

    store.save_pending(pairing)
    resumed = store.pending(ORIGIN)
    store.save_sign_in(SignIn(ORIGIN, TOKEN, "dev7", "CLI on desk", "2026-10-17T10:00:00Z"))
    signed_in = store.sign_in(ORIGIN)

    assert resumed == pairing
    assert signed_in is not None and (signed_in.token, signed_in.device_id) == (TOKEN, "dev7")
    assert store.pending(ORIGIN) is None, "the sign-in replaces the finished pairing"
    assert store.sign_in("https://vbot.example.com:8443") is None
    assert TOKEN not in repr(signed_in) and SECRET not in repr(resumed)
    text = store.path.read_text(encoding="utf-8")
    if sys.platform == "win32":
        assert TOKEN not in text and '"protection": "dpapi"' in text
    else:
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.forget(ORIGIN) and not store.forget(ORIGIN)
    assert store.sign_in(ORIGIN) is None


def test_a_token_this_account_cannot_open_signs_in_nothing(tmp_path: Path) -> None:
    store = SignInStore(tmp_path / "sign-ins.json")
    store.save_sign_in(SignIn(ORIGIN, TOKEN, "dev7", "CLI on desk", "2026-10-17T10:00:00Z"))
    document = json.loads(store.path.read_text(encoding="utf-8"))
    token = document["servers"][ORIGIN]["sign_in"]["token"]
    token["value"] = "QUJD" if token["protection"] == "dpapi" else "not a token\n"
    store.path.write_text(json.dumps(document), encoding="utf-8")

    assert store.sign_in(ORIGIN) is None
    assert store.forget(ORIGIN)


def test_a_damaged_file_is_reported_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "sign-ins.json"
    path.write_text("{not json", encoding="utf-8")
    store = SignInStore(path)

    with pytest.raises(SignInStoreError, match="sign-ins.json"):
        store.sign_in(ORIGIN)
    with pytest.raises(SignInStoreError):
        store.save_sign_in(SignIn(ORIGIN, TOKEN, "dev7", "CLI on desk", "2026-10-17T10:00:00Z"))
    assert path.read_text(encoding="utf-8") == "{not json"
