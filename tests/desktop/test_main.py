"""Tests for Desktop target configuration and local settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop import main as desktop_main


def test_parse_args_accepts_host_and_port() -> None:
    args = desktop_main.parse_args(["--host", "192.168.1.50", "--port", "9000"])

    assert args.host == "192.168.1.50"
    assert args.port == 9000


def test_resolve_target_uses_defaults_when_settings_are_missing(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    target = desktop_main.resolve_target([], settings_file=settings_file)

    assert target.host == desktop_main.DEFAULT_HOST
    assert target.port == desktop_main.DEFAULT_PORT
    assert target.url == "http://127.0.0.1:8420/"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "host": desktop_main.DEFAULT_HOST,
        "port": desktop_main.DEFAULT_PORT,
    }


def test_cli_args_override_saved_settings_per_field(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"host": "10.0.0.8", "port": 8765}), encoding="utf-8")

    host_override = desktop_main.resolve_target(
        ["--host", "localhost"], settings_file=settings_file
    )
    port_override = desktop_main.resolve_target(["--port", "9001"], settings_file=settings_file)

    assert host_override.host == "localhost"
    assert host_override.port == 8765
    assert host_override.url == "http://localhost:8765/"
    assert port_override.host == "localhost"
    assert port_override.port == 9001
    assert port_override.url == "http://localhost:9001/"


def test_settings_can_partially_override_defaults(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"host": "vbot.lan"}), encoding="utf-8")

    target = desktop_main.resolve_target([], settings_file=settings_file)

    assert target.host == "vbot.lan"
    assert target.port == desktop_main.DEFAULT_PORT
    assert target.url == "http://vbot.lan:8420/"


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_parse_args_rejects_invalid_ports(port: str) -> None:
    with pytest.raises(SystemExit):
        desktop_main.parse_args(["--port", port])


@pytest.mark.parametrize("port", [0, 65536, "not-a-port", None])
def test_resolve_target_rejects_invalid_settings_ports(tmp_path: Path, port: object) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"port": port}), encoding="utf-8")

    with pytest.raises(ValueError, match="settings.port"):
        desktop_main.resolve_target([], settings_file=settings_file)


def test_settings_file_lives_next_to_desktop_main() -> None:
    assert (
        desktop_main.settings_path()
        == Path(desktop_main.__file__).resolve().parent / "settings.json"
    )


def test_settings_writes_use_desktop_local_file_not_server_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_dir = tmp_path / "desktop"
    server_data_dir = tmp_path / "server-data"
    settings_file = desktop_main.settings_path(desktop_dir)
    monkeypatch.setenv("VBOT_DATA_DIR", str(server_data_dir))

    target = desktop_main.resolve_target(
        ["--host", "10.1.2.3", "--port", "8500"],
        settings_file=settings_file,
    )

    assert target.url == "http://10.1.2.3:8500/"
    assert settings_file.exists()
    assert not (server_data_dir / "settings.json").exists()
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "host": "10.1.2.3",
        "port": 8500,
    }


def test_desktop_main_does_not_import_server_or_core_business_logic() -> None:
    source = Path(desktop_main.__file__).read_text(encoding="utf-8")

    assert "from server" not in source
    assert "import server" not in source
    assert "from core" not in source
    assert "import core" not in source
