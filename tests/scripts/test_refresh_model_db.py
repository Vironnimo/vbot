"""Tests for the maintainer-only tracked Model DB refresh entry point."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def _load_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "refresh_model_db.py"
    spec = importlib.util.spec_from_file_location("refresh_model_db_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refresh_model_db = _load_script()


def test_main_targets_this_checkout_system_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    instance = ServerInstance(
        host="127.0.0.1",
        port=8421,
        data_dir=data_dir,
        url="http://127.0.0.1:8421",
        log_path=resolve_daily_log_path(data_dir),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(refresh_model_db, "_is_branch_checkout", lambda: True)
    monkeypatch.setattr(refresh_model_db, "_worktree_data_dir", lambda: data_dir)

    def fake_resolve_instance(**kwargs):
        captured["resolve"] = kwargs
        return instance

    def fake_model_refresh(
        selected_instance,
        provider,
        *,
        target,
        expected_resources_dir,
    ):
        captured["refresh"] = {
            "instance": selected_instance,
            "provider": provider,
            "target": target,
            "expected_resources_dir": expected_resources_dir,
        }
        return CommandResult(ok=True, message="refreshed openai", instance=instance)

    monkeypatch.setattr(refresh_model_db, "resolve_instance", fake_resolve_instance)
    monkeypatch.setattr(refresh_model_db, "model_refresh", fake_model_refresh)

    exit_code = refresh_model_db.main(["openai", "--port", "8421"])

    assert exit_code == 0
    assert captured["resolve"] == {
        "host": "127.0.0.1",
        "port": 8421,
        "data_dir": data_dir,
    }
    assert captured["refresh"] == {
        "instance": instance,
        "provider": "openai",
        "target": "system",
        "expected_resources_dir": refresh_model_db.PROJECT_ROOT / "resources",
    }


def test_main_rejects_detached_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_model_db, "_is_branch_checkout", lambda: False)

    exit_code = refresh_model_db.main([])

    assert exit_code == 2
