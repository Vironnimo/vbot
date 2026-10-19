"""Durable packaged-install records reject malformed or ambiguous facts."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from cli.application import state
from cli.application.state import (
    ApplicationError,
    Installation,
    Operation,
    discover,
    exclusive,
    load_installation,
    load_operation,
)
from core.access.config import access_config


def _server_install(root: Path) -> Installation:
    return Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))


def test_desktop_client_round_trip_has_no_server_data(tmp_path: Path):
    install = Installation(tmp_path, "desktop-client", None, None, None)
    install.save()

    loaded = load_installation(tmp_path)
    assert loaded.install_shape == "desktop-client"
    assert loaded.owns_server is False
    assert (loaded.server_host, loaded.server_port, loaded.server_data_directory) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "install_shape": "desktop-client"},
        {
            "schema_version": 1,
            "install_shape": "desktop-client",
            "server_host": "127.0.0.1",
            "server_port": 8420,
            "server_data_directory": "C:/data",
        },
        {
            "schema_version": 1,
            "install_shape": "server",
            "server_host": "127.0.0.1",
            "server_port": True,
            "server_data_directory": "relative-data",
        },
        # Every installer records the channel URL and the release signing key.
        {
            "schema_version": 1,
            "install_shape": "desktop-client",
            "release_url": state.CHANNEL_URLS["release"],
        },
        {
            "schema_version": 1,
            "install_shape": "desktop-client",
            "release_url": "https://example.invalid/releases/latest",
            "release_public_key": "",
        },
    ],
    ids=["schema", "client_target", "server_target", "no_key", "unknown_channel"],
)
def test_installation_rejects_malformed_schema_and_targets(
    tmp_path: Path, payload: dict[str, object]
):
    (tmp_path / "application.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ApplicationError):
        load_installation(tmp_path)


def test_active_version_cannot_escape_versions_directory(tmp_path: Path):
    install = _server_install(tmp_path)
    install.save()
    (tmp_path / "active-version").write_text("../outside\n", encoding="ascii")

    with pytest.raises(ApplicationError):
        install.version()


def test_operation_round_trip_and_invalid_phase_or_boolean_rejection(tmp_path: Path):
    install = _server_install(tmp_path)
    install.save()
    operation = Operation(id="upd_example", previous_version="rel_old", candidate_version="rel_new")
    operation.save(install)

    assert load_operation(install, operation.id) == operation
    path = tmp_path / "operations" / f"{operation.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["phase"] = "invented"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ApplicationError, match="phase"):
        load_operation(install, operation.id)

    payload["phase"] = "queued"
    payload["restart"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ApplicationError, match="policy"):
        load_operation(install, operation.id)


def test_version_labels_round_trip_without_changing_protocol_one_fields(tmp_path):
    install = _server_install(tmp_path)
    operation = Operation(
        id="upd_labels", previous_label="1.0 (aaaaaaaa)", target_label="1.1 (bbbbbbbb)"
    )
    operation.save(install)
    saved = json.loads((tmp_path / "operations" / "upd_labels.json").read_text(encoding="utf-8"))
    assert "previous_label" not in saved and "target_label" not in saved
    assert load_operation(install, operation.id) == operation
    assert len(state.operations(install)) == 1


def test_os_lock_rejects_an_overlapping_operation(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with exclusive(tmp_path):
            entered.set()
            release.wait(2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert entered.wait(2)
    try:
        with (
            pytest.raises(ApplicationError, match="Another application operation"),
            exclusive(tmp_path),
        ):
            pass
    finally:
        release.set()
        holder.join(timeout=2)
    assert not holder.is_alive()


def test_discovery_does_not_treat_the_shell_cwd_as_an_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _server_install(tmp_path).save()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VBOT_INSTALL_ROOT", raising=False)

    assert discover() is None
    assert os.getcwd() == str(tmp_path)
    explicit = state.discover(tmp_path)
    assert explicit is not None
    assert explicit.root == tmp_path


def test_discovery_ignores_an_installation_ancestor_of_development_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_root = tmp_path / "packaged"
    _server_install(install_root).save()
    source_module = install_root / "development" / "source" / "cli" / "application" / "state.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(state, "__file__", str(source_module))
    monkeypatch.delenv("VBOT_INSTALL_ROOT", raising=False)

    assert state.discover() is None


def test_source_discovery_ignores_inherited_native_install_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_root = tmp_path / "packaged"
    _server_install(install_root).save()
    source_module = tmp_path / "checkout" / "cli" / "application" / "state.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(state, "__file__", str(source_module))
    monkeypatch.setenv("VBOT_INSTALL_ROOT", str(install_root))

    assert state.discover() is None


def test_packaged_module_uses_its_recorded_install_over_foreign_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    own_root = tmp_path / "own"
    foreign_root = tmp_path / "foreign"
    _server_install(own_root).save()
    _server_install(foreign_root).save()
    source_module = (
        own_root / "versions" / "rel_current" / "app" / "cli" / "application" / "state.py"
    )
    source_module.parent.mkdir(parents=True)
    source_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(state, "__file__", str(source_module))
    monkeypatch.setenv("VBOT_INSTALL_ROOT", str(foreign_root))

    discovered = state.discover()

    assert discovered is not None
    assert discovered.root == own_root


@pytest.mark.parametrize("unavailable", [False, True])
def test_native_bootstrap_uses_its_recorded_install_over_foreign_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
):
    own_root = tmp_path / "own"
    foreign_root = tmp_path / "foreign"
    _server_install(own_root).save()
    _server_install(foreign_root).save()
    monkeypatch.setattr(
        state, "__file__", str(tmp_path / "source" / "cli" / "application" / "state.py")
    )
    monkeypatch.setattr(sys, "executable", str(own_root / "vBot.exe"))
    monkeypatch.setenv("VBOT_INSTALL_ROOT", str(foreign_root))
    if unavailable:
        original = os.stat

        def stat(path, *args, **kwargs) -> os.stat_result:
            if path == own_root / "application.json":
                raise PermissionError("record metadata unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
        with pytest.raises(ApplicationError, match="Cannot check application record"):
            state.discover()
        return

    discovered = state.discover()

    assert discovered is not None
    assert discovered.root == own_root


def test_temporary_native_payload_accepts_explicit_install_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_root = tmp_path / "destination"
    _server_install(install_root).save()
    monkeypatch.setattr(
        state, "__file__", str(tmp_path / "payload" / "app" / "cli" / "application" / "state.py")
    )
    monkeypatch.setattr(
        sys, "executable", str(tmp_path / "payload" / "runtime" / "vBot.Python.exe")
    )
    monkeypatch.setenv("VBOT_INSTALL_ROOT", str(install_root))

    discovered = state.discover()

    assert discovered is not None
    assert discovered.root == install_root


def test_the_server_access_mode_round_trips_and_older_records_infer_it(tmp_path: Path):
    _server_install(tmp_path).save()
    older = load_installation(tmp_path)
    path = tmp_path / "application.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["added_by_a_newer_version"] = {"kept": True}
    path.write_text(json.dumps(record), encoding="utf-8")
    selected = access_config(
        "authenticated", public_url="https://vbot.example.org", trusted_proxies=["127.0.0.1"]
    )

    replace(load_installation(tmp_path), server_access=selected).save()
    loaded = load_installation(tmp_path)

    # A record written before access modes keeps the mode its bind address implies.
    assert older.server_access is None and older.access == access_config("local")
    assert replace(older, server_host="0.0.0.0").access == access_config("lan")
    assert loaded.access == selected
    assert json.loads(path.read_text(encoding="utf-8"))["added_by_a_newer_version"] == {
        "kept": True
    }


@pytest.mark.parametrize(
    ("shape", "host", "recorded"),
    [
        pytest.param("desktop-client", None, {"mode": "local"}, id="client-with-access"),
        pytest.param("server", "0.0.0.0", {"mode": "local"}, id="local-on-the-network"),
        pytest.param("server", "127.0.0.1", {"mode": "authenticated"}, id="no-public-url"),
        pytest.param("server", "127.0.0.1", {"mode": "lan", "allow_networks": "x"}, id="shape"),
    ],
)
def test_installation_rejects_an_invalid_recorded_access_mode(
    tmp_path: Path, shape: str, host: str | None, recorded: dict[str, object]
):
    if shape == "server":
        replace(_server_install(tmp_path), server_host=host).save()
    else:
        Installation(tmp_path, shape, None, None, None).save()
    path = tmp_path / "application.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["server_access"] = recorded
    path.write_text(json.dumps(record), encoding="utf-8")

    # The server would otherwise run with less protection than recorded.
    with pytest.raises(ApplicationError):
        load_installation(tmp_path)
