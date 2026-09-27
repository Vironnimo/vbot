"""Smoke test of the maintainer-only tracked Model DB refresh script."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cli.server_management import CommandResult


def _load_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "refresh_model_db.py"
    spec = importlib.util.spec_from_file_location("refresh_model_db_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refresh_model_db = _load_script()


def test_refresh_publishes_into_this_checkouts_system_model_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: dict[str, Any] = {}

    def model_refresh(instance: Any, provider: str | None, **target: Any) -> CommandResult:
        refreshed.update(port=instance.port, provider=provider, **target)
        return CommandResult(ok=True, message="refreshed openai", instance=instance)

    monkeypatch.setattr(refresh_model_db, "_is_branch_checkout", lambda: True)
    monkeypatch.setattr(refresh_model_db, "model_refresh", model_refresh)

    code = refresh_model_db.main(["openai", "--port", "8421", "--data-dir", str(tmp_path)])

    assert code == 0
    assert refreshed == {
        "port": 8421,
        "provider": "openai",
        "target": "system",
        "expected_resources_dir": refresh_model_db.PROJECT_ROOT / "resources",
    }
