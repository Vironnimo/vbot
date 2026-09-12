"""Tests for project methods policy."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.projects.projects import PROJECT_DEFAULT_ALLOWED_TOOLS
from core.projects.scanners.opencode import OPENCODE_AGENTS_SUBPATH
from core.skills import SKILL_ORIGIN_BUNDLED, SKILL_ORIGIN_GLOBAL
from server.rpc.errors import RpcError
from server.rpc.project_methods import (
    _add_project,
    _set_project,
    _show_project,
)
from tests.server.rpc.project_methods_test_support import (
    _make_repo,
    _make_state,
    _write_agent,
)


# ---------------------------------------------------------------------------
# Tool / Skill Whitelist fields: add defaults, set, validation, team denials.
# ---------------------------------------------------------------------------
def test_add_returns_default_whitelist_fields(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")

    result = _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    assert result["project"]["allowed_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert result["project"]["skills_bundled_enabled"] == []
    assert result["project"]["skills_global_enabled"] == []
    assert result["project"]["skills_project_disabled"] == []


def test_set_changes_whitelist_fields(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(
        state,
        {
            "project_id": "vbot",
            "allowed_tools": ["read", "grep"],
            "skills_bundled_enabled": ["frontend-design"],
            "skills_global_enabled": ["pdf"],
            "skills_project_disabled": ["debugging"],
        },
    )

    assert result["project"]["allowed_tools"] == ["read", "grep"]
    assert result["project"]["skills_bundled_enabled"] == ["frontend-design"]
    assert result["project"]["skills_global_enabled"] == ["pdf"]
    assert result["project"]["skills_project_disabled"] == ["debugging"]


def test_set_allows_empty_allowed_tools(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(state, {"project_id": "vbot", "allowed_tools": []})

    assert result["project"]["allowed_tools"] == []


def test_set_rejects_non_string_tool_entry(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "allowed_tools": ["read", 7]})

    assert exc_info.value.code == "invalid_request"


def test_set_rejects_tool_wildcard(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "allowed_tools": ["read", "*"]})

    assert exc_info.value.code == "invalid_request"


def test_set_rejects_new_unregistered_project_tool(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(
            state,
            {"project_id": "vbot", "allowed_tools": ["read", "missing_extension_tool"]},
        )

    assert exc_info.value.code == "invalid_request"
    assert "missing_extension_tool" in exc_info.value.message


def test_set_rejects_registered_but_project_excluded_tool(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    with pytest.raises(RpcError) as exc_info:
        _set_project(state, {"project_id": "vbot", "allowed_tools": ["read", "memory"]})

    assert exc_info.value.code == "invalid_request"
    assert "memory" in exc_info.value.message


def test_set_accepts_registered_extension_tool(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    state.runtime.tools.names.add("extension_tool")
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})

    result = _set_project(
        state,
        {"project_id": "vbot", "allowed_tools": ["read", "extension_tool"]},
    )

    assert result["project"]["allowed_tools"] == ["read", "extension_tool"]


def test_set_preserves_existing_unavailable_tool_while_editing_known_tools(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})
    state.runtime.projects.update("vbot", allowed_tools=["read", "disabled_extension_tool"])

    result = _set_project(
        state,
        {
            "project_id": "vbot",
            "allowed_tools": ["read", "grep", "disabled_extension_tool"],
        },
    )

    assert result["project"]["allowed_tools"] == [
        "read",
        "grep",
        "disabled_extension_tool",
    ]


def test_show_reports_persisted_unavailable_tool_without_rejecting_project(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    _add_project(state, {"cwd": str(_make_repo(tmp_path, "vbot")), "display_name": "vBot"})
    state.runtime.projects.update("vbot", allowed_tools=["read", "disabled_extension_tool"])

    result = _show_project(state, {"project_id": "vbot"})

    assert result["project"]["allowed_tools"] == ["read", "disabled_extension_tool"]
    assert result["scan"]["report"] == {
        "clean": False,
        "findings": [
            {
                "type": "unavailable_tool",
                "detail": (
                    "Tool Whitelist entry 'disabled_extension_tool' is not a currently "
                    "registered Project tool. It remains stored but grants no access "
                    "unless the tool becomes available again."
                ),
                "agent_id": "",
                "source_path": None,
            }
        ],
    }


def test_scan_preview_includes_project_skill_pool(tmp_path: Path) -> None:
    # The scan response carries the editor's skill pool: the project's own skills,
    # plus the bundled and global opt-in pools with name collisions removed (project
    # wins) and global-home skills split out from bundled by origin.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    state.runtime.project_skill_names = lambda _project_id: frozenset({"refactoring", "glossary"})
    state.runtime.project_own_skills = lambda _project_id: [
        SimpleNamespace(name="refactoring", description="Refactor code safely."),
        SimpleNamespace(name="glossary", description="Maintain the glossary."),
    ]
    state.runtime.skills = SimpleNamespace(
        list_all=lambda: [
            SimpleNamespace(name="glossary", description="", origin=SKILL_ORIGIN_BUNDLED),
            SimpleNamespace(name="pdf", description="Work with PDFs.", origin=SKILL_ORIGIN_BUNDLED),
            SimpleNamespace(
                name="deploy", description="Deploy the app.", origin=SKILL_ORIGIN_GLOBAL
            ),
        ]
    )

    result = _show_project(state, {"project_id": "vbot"})

    # Each pool entry carries name + description so the editor's chips can show the
    # description on hover, like the tool pool.
    assert result["scan"]["skills"] == {
        "project": [
            {"name": "glossary", "description": "Maintain the glossary."},
            {"name": "refactoring", "description": "Refactor code safely."},
        ],
        # "glossary" is shadowed by the project skill of the same name, so it is not
        # offered again as a bundled opt-in.
        "bundled": [{"name": "pdf", "description": "Work with PDFs."}],
        # Global-home skills are a separate opt-in pool, split out by origin.
        "global": [{"name": "deploy", "description": "Deploy the app."}],
    }


def test_show_project_reloads_global_skills_from_disk(tmp_path: Path) -> None:
    # A show reloads the global skill registry so a skill hand-dropped into the global
    # skills folder surfaces in the editor pool without a server restart.
    state = _make_state(tmp_path)
    repo = _make_repo(tmp_path, "vbot", "builder.md")
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})
    reload_calls: list[bool] = []
    state.runtime.reload_skills = lambda: reload_calls.append(True)

    _show_project(state, {"project_id": "vbot"})

    assert reload_calls == [True]


def test_team_member_reports_denied_tools(tmp_path: Path) -> None:
    # An OpenCode agent denying task → the team response surfaces the mapped vBot
    # tool it turns off, so the editor can show it uses less than the ceiling.
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    repo.mkdir(parents=True)
    _write_agent(repo, "explorer.md", permission={"task": "deny", "edit": "deny"})
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    member = next(m for m in result["scan"]["team"] if m["agent_id"] == "explorer")
    assert member["denied_tools"] == ["edit", "subagent", "write"]


def test_team_member_reports_effective_repo_owned_agent_targets(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    repo = tmp_path / "repos" / "vbot"
    agents_dir = repo.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True)
    for name in ("builder", "reviewer"):
        _write_agent(repo, f"{name}.md")
    agents_dir.joinpath("orchestrator.md").write_text(
        (
            "---\nmodel: openai/gpt-5.2\npermission:\n  task:\n"
            '    "*": deny\n    reviewer: allow\n---\nBody.\n'
        ),
        encoding="utf-8",
    )
    _add_project(state, {"cwd": str(repo), "display_name": "vBot"})

    result = _show_project(state, {"project_id": "vbot"})

    members = {member["agent_id"]: member for member in result["scan"]["team"]}
    assert members["orchestrator"]["tools"] == {"subagent": {"allowed_agents": ["reviewer"]}}
    assert members["builder"]["tools"] == {
        "subagent": {"allowed_agents": ["orchestrator", "reviewer"]}
    }
