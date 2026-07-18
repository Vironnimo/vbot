"""Tests for the explicit Agent target-policy converter."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PROJECT_ROOT / "scripts" / "converters" / "add_agent_allowed_agents.py"


def _load_converter():
    spec = importlib.util.spec_from_file_location("add_agent_allowed_agents", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.convert


def test_convert_adds_wildcard_and_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "agents" / "coder" / "agent.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"id": "coder", "allowed_tools": ["*"]}), encoding="utf-8")

    convert = _load_converter()
    first = convert(tmp_path)
    second = convert(tmp_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert first == (1, 0)
    assert second == (0, 1)
    assert data["allowed_agents"] == ["*"]


def test_convert_preserves_existing_target_policy(tmp_path: Path) -> None:
    config_path = tmp_path / "agents" / "coder" / "agent.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"id": "coder", "allowed_agents": ["worker"]}), encoding="utf-8"
    )

    result = _load_converter()(tmp_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert result == (0, 1)
    assert data["allowed_agents"] == ["worker"]
