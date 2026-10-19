"""The device store: device tokens, persistence, expiry and fail-closed loading."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.access.devices import (
    Device,
    DeviceNotFoundError,
    DeviceOrigin,
    DeviceStore,
    DeviceStoreUnavailableError,
    new_device,
    validate_device_store_file,
)

_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(minutes=1)


def _loaded(data_dir: Path) -> DeviceStore:
    store = DeviceStore(data_dir)
    store.load()
    return store


def test_device_tokens_authenticate_until_the_device_is_removed(tmp_path: Path) -> None:
    store = _loaded(tmp_path)
    device, token = new_device(
        name="  Julian's   phone ", kind="browser", approved_by="host", client="10.0.0.2", now=_NOW
    )
    store.add(device)
    other, other_token = new_device(
        name="Laptop", kind="browser", approved_by=device.id, client=None, now=_LATER, via="ticket"
    )
    store.add(other)
    secret = token[-43:]

    assert store.lookup(token, now=_NOW) == ("valid", device)
    assert device.name == "Julian's phone"
    assert token == f"vbt_{device.id}_{secret}"
    for forged in (
        f"vbt_{device.id}_{'A' * 43}",
        f"vbt_{other.id}_{secret}",
        token[:-1],
        f"Bearer {token}",
        "",
    ):
        assert store.lookup(forged, now=_NOW) == ("invalid", None)

    store.record_use(device.id, "10.0.0.3", now=_LATER)
    renamed = store.rename(device.id, "Phone")
    assert (renamed.last_used_at, renamed.last_client) == (_LATER, "10.0.0.3")
    # A fresh store reads exactly what was written; no token is ever stored. A
    # device without a recorded origin (written before ``via``) keeps none.
    reloaded = _loaded(tmp_path)
    assert reloaded.devices() == [renamed, other]
    assert (renamed.via, other.via, other.describe()["via"]) == (None, "ticket", "ticket")
    assert token not in (tmp_path / "access" / "devices.json").read_text(encoding="utf-8")

    assert store.remove([device.id, "dev_unknown"]) == [renamed]
    assert store.lookup(token, now=_NOW) == ("invalid", None)
    assert _loaded(tmp_path).devices() == [other]
    assert store.lookup(other_token, now=_NOW)[0] == "valid"
    # What a removed device approved but nobody claimed in time never becomes a device.
    late, _late_token = new_device(
        name="Tablet", kind="desktop", approved_by=device.id, client=None, now=_LATER
    )
    with pytest.raises(DeviceNotFoundError):
        store.add(late)
    assert _loaded(tmp_path).devices() == [other]


def test_devices_expire_after_ninety_idle_days_and_record_use_hourly(tmp_path: Path) -> None:
    store = _loaded(tmp_path)
    device, token = new_device(
        name="Tablet", kind="desktop", approved_by="host", client=None, now=_NOW
    )
    store.add(device)

    assert not store.record_use(device.id, "10.0.0.7", now=_NOW + timedelta(minutes=59))
    assert store.record_use(device.id, "10.0.0.8", now=_NOW + timedelta(hours=2))
    store.flush_usage()
    stored = _loaded(tmp_path).devices()[0]
    assert (stored.last_used_at, stored.last_client) == (_NOW + timedelta(hours=2), "10.0.0.8")

    later = _NOW + timedelta(hours=2, days=90)
    assert store.lookup(token, now=later)[0] == "valid"
    expired_at = later + timedelta(seconds=1)
    assert store.lookup(token, now=expired_at)[0] == "expired"
    assert [entry.id for entry in store.expired_devices(now=expired_at)] == [device.id]


@pytest.mark.parametrize("discover", ["load", "write"])
def test_a_store_that_fails_to_load_authenticates_nobody_and_is_never_overwritten(
    tmp_path: Path,
    discover: str,
) -> None:
    store = _loaded(tmp_path)
    device, token = new_device(
        name="Phone", kind="browser", approved_by="host", client=None, now=_NOW
    )
    store.add(device)
    path = tmp_path / "access" / "devices.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["devices"][0]["kind"] = "toaster"
    damaged = json.dumps(document)
    path.write_text(damaged, encoding="utf-8")

    if discover == "load":
        broken = _loaded(tmp_path)
    else:
        broken = store
        with pytest.raises(DeviceStoreUnavailableError):
            broken.rename(device.id, "Renamed")

    assert not broken.available
    assert "kind must be one of" in (broken.error or "")
    assert broken.lookup(token, now=_NOW) == ("invalid", None)
    with pytest.raises(DeviceStoreUnavailableError):
        broken.add(new_device(name="X", kind="cli", approved_by="host", client=None, now=_NOW)[0])
    with pytest.raises(DeviceStoreUnavailableError):
        broken.remove([device.id])
    assert path.read_text(encoding="utf-8") == damaged
    assert not validate_device_store_file(path).ok


@pytest.mark.parametrize("recovery", ["retry", "flush"])
def test_failed_removals_remain_pending_until_they_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    store = _loaded(tmp_path)
    device, token = new_device(
        name="Phone", kind="browser", approved_by="host", client=None, now=_NOW
    )
    store.add(device)

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("storage unavailable")

    with monkeypatch.context() as broken:
        broken.setattr("core.access.devices.write_json_document", fail_write)
        with pytest.raises(OSError):
            store.remove([device.id])
        assert store.lookup(token, now=_NOW)[0] == "invalid"
        assert store.removal_pending(device.id)
        with pytest.raises(OSError):
            store.remove([device.id])
    if recovery == "retry":
        assert store.remove([device.id]) == [device]
    else:
        store.flush_usage()
    assert not store.removal_pending(device.id)
    assert _loaded(tmp_path).lookup(token, now=_NOW)[0] == "invalid"


def test_ticket_browser_activity_renews_its_existing_issuer_chain(tmp_path: Path) -> None:
    store = _loaded(tmp_path)
    issuer, _token = new_device(
        name="Desktop", kind="desktop", approved_by="host", client=None, now=_NOW
    )
    store.add(issuer)
    browser, _token = new_device(
        name="Window", kind="browser", approved_by=issuer.id, client=None, now=_NOW, via="ticket"
    )
    store.add(browser)
    paired, _token = new_device(
        name="Independent", kind="cli", approved_by=browser.id, client=None, now=_NOW, via="pairing"
    )
    store.add(paired)
    later = _NOW + timedelta(days=89)
    assert store.record_use(browser.id, "10.0.0.9", now=later)
    store.flush_usage()
    loaded = _loaded(tmp_path)
    assert {device.id for device in loaded.expired_devices(now=_NOW + timedelta(days=91))} == {
        paired.id
    }
    used_issuer = loaded.get(issuer.id)
    assert used_issuer is not None and used_issuer.last_used_at == later
    # An explicitly spared browser remains usable after its issuer is gone.
    store.remove([issuer.id])
    assert store.record_use(browser.id, "10.0.0.9", now=later + timedelta(hours=2))


def test_ticket_browser_rebinding_preserves_independent_grants_and_refuses_cycles(
    tmp_path: Path,
) -> None:
    store = _loaded(tmp_path)

    def add(name: str, approver: str, via: DeviceOrigin) -> Device:
        device, _token = new_device(
            name=name, kind="browser", approved_by=approver, client=None, now=_NOW, via=via
        )
        store.add(device)
        return device

    first = add("First", "host", "pairing")
    second = add("Second", "host", "pairing")
    browser = add("Window", first.id, "ticket")
    child = add("Child", browser.id, "ticket")
    assert store.rebind_ticket_browser(first.id, second.id).approved_by == "host"
    assert store.rebind_ticket_browser(browser.id, browser.id).approved_by == first.id
    with pytest.raises(ValueError):
        store.rebind_ticket_browser(browser.id, child.id)
    store.remove([first.id])
    assert store.rebind_ticket_browser(browser.id, second.id).approved_by == second.id
    rebound = _loaded(tmp_path).get(browser.id)
    assert rebound is not None and rebound.approved_by == second.id
    store.remove([second.id])
    with pytest.raises(DeviceNotFoundError):
        store.rebind_ticket_browser(browser.id, second.id)


def test_the_store_keeps_fields_a_newer_vbot_added(tmp_path: Path) -> None:
    store = _loaded(tmp_path)
    device, _token = new_device(
        name="Phone", kind="browser", approved_by="host", client=None, now=_NOW
    )
    store.add(device)
    path = tmp_path / "access" / "devices.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["policy"] = {"future": True}
    document["devices"][0]["passkey"] = "abc"
    path.write_text(json.dumps(document), encoding="utf-8")

    reloaded = _loaded(tmp_path)
    reloaded.rename(device.id, "Renamed")

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["policy"] == {"future": True}
    assert written["devices"][0]["passkey"] == "abc"
    assert written["devices"][0]["name"] == "Renamed"
    assert validate_device_store_file(path).ok


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("Phone\x1b]0;owned\x07", id="escape-sequence"),
        pytest.param("Phone\x9b2J", id="c1-control"),
        pytest.param("Phone‮exe.gnp", id="bidi-override"),
        pytest.param("Ph​one", id="zero-width"),
        pytest.param("Phone\ud800", id="unpaired-surrogate"),
    ],
)
def test_new_names_refuse_hidden_characters_while_stored_ones_still_load(
    tmp_path: Path, name: str
) -> None:
    store = _loaded(tmp_path)
    with pytest.raises(ValueError, match="control"):
        new_device(name=name, kind="browser", approved_by="host", client=None, now=_NOW)
    device, _token = new_device(
        name="Phone  two", kind="browser", approved_by="host", client=None, now=_NOW
    )
    store.add(device)
    with pytest.raises(ValueError, match="control"):
        store.rename(device.id, name)

    # A name an earlier vBot accepted keeps its device signed in.
    path = tmp_path / "access" / "devices.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["devices"][0]["name"] = "Ph​one"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert device.name == "Phone two"
    assert [stored.name for stored in _loaded(tmp_path).devices()] == ["Ph​one"]
