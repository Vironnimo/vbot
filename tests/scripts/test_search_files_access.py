"""Search capability consolidation never silently widens old policies."""

import json
from pathlib import Path

import pytest

from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.search_files import register_search_files_tool
from core.tools.tools import ToolRegistry
from scripts.converters.search_files_access import (
    convert_ceiling,
    convert_policy,
    convert_search_access,
)


def test_both_search_grants_can_be_consolidated():
    assert convert_policy({"mode": "selected", "allowed": ["read", "grep", "glob"]}) == {
        "mode": "selected",
        "allowed": ["read", "search_files"],
    }
    assert convert_ceiling(["read", "glob", "grep"]) == ["read", "search_files"]
    assert convert_policy({"mode": "all", "denied": ["grep", "glob"]}) == {
        "mode": "all",
        "denied": ["search_files"],
    }


@pytest.mark.parametrize(
    "policy", [{"mode": "selected", "allowed": ["grep"]}, {"mode": "all", "denied": ["glob"]}]
)
def test_mixed_policies_require_explicit_choice(policy):
    with pytest.raises(ValueError, match="Mixed"):
        convert_policy(policy)


def test_project_ceiling_cannot_be_widened():
    with pytest.raises(ValueError, match="explicit"):
        convert_ceiling(["*"])
    with pytest.raises(ValueError, match="ceiling"):
        convert_ceiling(["read", "glob"])
    with pytest.raises(ValueError, match="ceiling"):
        convert_policy({"mode": "selected", "allowed": ["grep", "glob"]}, ceiling=["grep"])


def test_default_project_ceiling_uses_the_registered_search_capability(tmp_path):
    from core.projects.projects import build_project

    project = build_project("test", "Test", tmp_path)
    assert "search_files" in project.allowed_tools
    assert not {"glob", "grep"}.intersection(project.allowed_tools)


def test_conversion_updates_project_overrides_after_complete_preflight(tmp_path):
    path = tmp_path / "projects/p/project.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "allowed_tools": ["read", "grep", "glob"],
                "overrides": {
                    "worker": {"tool_access": {"mode": "selected", "allowed": ["glob", "grep"]}}
                },
            }
        )
    )
    original = path.read_bytes()
    assert convert_search_access(tmp_path) == [str(path)]
    assert path.read_bytes() == original
    convert_search_access(tmp_path, apply=True)
    result = json.loads(path.read_text())
    assert result["allowed_tools"] == ["read", "search_files"]
    assert result["overrides"]["worker"]["tool_access"]["allowed"] == ["search_files"]


def test_preflight_failure_writes_nothing(tmp_path: Path):
    a = tmp_path / "agents/a/agent.json"
    b = tmp_path / "agents/b/agent.json"
    for file, allowed in ((a, ["grep", "glob"]), (b, ["grep"])):
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({"tool_access": {"mode": "selected", "allowed": allowed}}))
    original = a.read_bytes()
    with pytest.raises(ValueError, match="Mixed"):
        convert_search_access(tmp_path, apply=True)
    assert a.read_bytes() == original


def test_legacy_denial_also_blocks_union_before_manual_conversion():
    registry = ToolRegistry()
    register_search_files_tool(registry)
    tool = registry.get("search_files")
    for name in ("grep", "glob"):
        result = resolve_tool_access(ToolAccess(mode="all", denied=(name,)), [tool], "off")
        assert "search_files" not in result.allowed_tools
