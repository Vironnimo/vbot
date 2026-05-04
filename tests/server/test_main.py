"""Tests for server startup argument and port handling."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.utils.config import Config
from server import main as server_main
from server.main import DEFAULT_PORT, main, parse_args, resolve_port


def test_parse_args_accepts_data_dir_and_port() -> None:
    args = parse_args(["--data-dir", "dev-data", "--port", "9000"])

    assert args.data_dir == "dev-data"
    assert args.port == 9000


def test_resolve_port_priority_explicit_then_environment_then_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"server_port": 8500}), encoding="utf-8")
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")
    config = Config(data_dir=tmp_path)

    assert resolve_port(config, 8700) == 8700
    assert resolve_port(config) == 8600


def test_resolve_port_uses_settings_then_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

    assert resolve_port(Config(data_dir=tmp_path)) == 8500
    assert resolve_port(Config(data_dir=tmp_path / "missing")) == DEFAULT_PORT


def test_resolve_port_ignores_ambient_port_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    monkeypatch.setenv("PORT", "8600")
    monkeypatch.setenv("SERVER_PORT", "8700")

    assert resolve_port(Config(data_dir=tmp_path)) == DEFAULT_PORT


def test_resolve_port_accepts_port_keys_from_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    monkeypatch.setenv("PORT", "8600")
    monkeypatch.setenv("SERVER_PORT", "8800")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"SERVER_PORT": 8700}), encoding="utf-8")

    assert resolve_port(Config(data_dir=tmp_path)) == 8700


def test_main_starts_uvicorn_with_configured_app(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(app, *, host: str, port: int, log_level: str) -> None:
        calls.append({"app": app, "host": host, "port": port, "log_level": log_level})

    monkeypatch.setattr(server_main, "uvicorn", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(server_main, "create_app", lambda *, config: {"config": config})

    main(["--data-dir", str(tmp_path / "data"), "--port", "8765"])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8765
    assert calls[0]["log_level"] == "info"
