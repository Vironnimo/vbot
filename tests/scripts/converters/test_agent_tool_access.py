"""Tests for the explicit Identity Agent Tool-access converter."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest

_CONVERTER = import_module("scripts.converters.agent_tool_access")
AgentToolAccessConversionError = _CONVERTER.AgentToolAccessConversionError
convert_agent_tool_access = _CONVERTER.convert_agent_tool_access


def _write_agent(data_dir: Path, agent_id: str, **fields: object) -> Path:
    path = data_dir / "agents" / agent_id / "agent.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"id": agent_id, "name": agent_id, **fields}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        (["*"], {"mode": "all"}),
        ([], {"mode": "selected", "allowed": []}),
        (
            ["read", "memory", "session_read", "history", "skill_list", "read"],
            {"mode": "selected", "allowed": ["read"]},
        ),
    ],
)
def test_converter_maps_legacy_modes_without_changing_dry_run(
    tmp_path: Path,
    legacy: list[str],
    expected: dict[str, object],
) -> None:
    path = _write_agent(tmp_path, "main", allowed_tools=legacy, model="openai/test")
    before = path.read_text(encoding="utf-8")

    dry_run = convert_agent_tool_access(tmp_path)

    assert dry_run.planned == 1
    assert dry_run.converted == 0
    assert path.read_text(encoding="utf-8") == before

    applied = convert_agent_tool_access(tmp_path, apply=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert applied.converted == 1
    assert payload["tool_access"] == expected
    assert "allowed_tools" not in payload
    assert payload["model"] == "openai/test"


def test_converter_defaults_a_missing_legacy_field_to_all(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "main")

    convert_agent_tool_access(tmp_path, apply=True)

    assert json.loads(path.read_text(encoding="utf-8"))["tool_access"] == {"mode": "all"}


def test_converter_is_idempotent_for_current_policy(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "main", tool_access={"mode": "none"})
    before = path.read_text(encoding="utf-8")

    result = convert_agent_tool_access(tmp_path, apply=True)

    assert result.planned == 0
    assert result.converted == 0
    assert result.already_converted == 1
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "fields",
    [
        {"allowed_tools": ["*"], "tool_access": {"mode": "all"}},
        {"allowed_tools": ["*", "read"]},
        {"allowed_tools": "*"},
        {"allowed_tools": ["   "]},
    ],
)
def test_converter_preflights_every_agent_before_writing(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    valid_path = _write_agent(tmp_path, "a-valid", allowed_tools=["read"])
    invalid_path = _write_agent(tmp_path, "z-invalid", **fields)
    valid_before = valid_path.read_text(encoding="utf-8")
    invalid_before = invalid_path.read_text(encoding="utf-8")

    with pytest.raises(AgentToolAccessConversionError):
        convert_agent_tool_access(tmp_path, apply=True)

    assert valid_path.read_text(encoding="utf-8") == valid_before
    assert invalid_path.read_text(encoding="utf-8") == invalid_before


def test_converter_rejects_symlinked_agent_file(tmp_path: Path) -> None:
    target = _write_agent(tmp_path, "target", allowed_tools=["read"])
    linked_dir = tmp_path / "agents" / "linked"
    linked_dir.mkdir(parents=True)
    link = linked_dir / "agent.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    with pytest.raises(AgentToolAccessConversionError, match="symlinked Agent file"):
        convert_agent_tool_access(tmp_path)
