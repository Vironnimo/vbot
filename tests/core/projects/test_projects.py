"""Tests for the Project entity, field validation, and serialization."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.projects.projects import (
    PROJECT_DEFAULT_ALLOWED_TOOLS,
    InvalidProjectIdError,
    Project,
    ProjectError,
    build_project,
    project_from_dict,
    seed_default_auto_load,
)


def test_build_project_creates_entity_with_normalized_cwd(tmp_path: Path) -> None:
    project = build_project("vbot", "vBot", tmp_path, default_agent="orchestrator")

    assert project.project_id == "vbot"
    assert project.display_name == "vBot"
    assert project.cwd == str(Path(os.path.realpath(tmp_path)))
    assert project.default_agent == "orchestrator"
    assert project.created_at.endswith("Z")
    assert project.updated_at == project.created_at


def test_build_project_minimal_just_cwd_defaults_optionals(tmp_path: Path) -> None:
    project = build_project("scratch", "Scratch", tmp_path)

    assert project.default_agent == ""
    assert project.default_model == ""
    assert project.default_temperature is None
    assert project.default_thinking_effort is None
    assert project.auto_load == []
    # An unspecified Tool Whitelist falls back to the base list; skill lists empty.
    assert project.allowed_tools == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert project.skills_bundled_enabled == []
    assert project.skills_project_disabled == []
    assert project.model_overrides == {}


def test_build_project_keeps_explicit_empty_allowed_tools(tmp_path: Path) -> None:
    # [] is a real value (every tool off), distinct from None (seed the base list).
    project = build_project("vbot", "vBot", tmp_path, allowed_tools=[])

    assert project.allowed_tools == []


def test_build_project_accepts_whitelist_fields(tmp_path: Path) -> None:
    project = build_project(
        "vbot",
        "vBot",
        tmp_path,
        allowed_tools=["read", "grep"],
        skills_bundled_enabled=["frontend-design"],
        skills_project_disabled=["debugging"],
    )

    assert project.allowed_tools == ["read", "grep"]
    assert project.skills_bundled_enabled == ["frontend-design"]
    assert project.skills_project_disabled == ["debugging"]


def test_build_project_rejects_non_string_allowed_tool(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, allowed_tools=["read", 7])  # type: ignore[list-item]


def test_build_project_rejects_non_list_skills_bundled(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, skills_bundled_enabled="frontend")  # type: ignore[arg-type]


def test_build_project_round_trips_skills_global_enabled(tmp_path: Path) -> None:
    project = build_project("vbot", "vBot", tmp_path, skills_global_enabled=["pdf", "deploy"])

    assert project.skills_global_enabled == ["pdf", "deploy"]
    assert project.to_dict()["skills_global_enabled"] == ["pdf", "deploy"]
    assert project_from_dict(project.to_dict()).skills_global_enabled == ["pdf", "deploy"]


def test_build_project_rejects_non_list_skills_global(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, skills_global_enabled="pdf")  # type: ignore[arg-type]


def test_build_project_accepts_model_overrides(tmp_path: Path) -> None:
    project = build_project(
        "vbot",
        "vBot",
        tmp_path,
        model_overrides={"builder": "openai/gpt-5", "planner": "anthropic/claude-sonnet-4"},
    )

    assert project.model_overrides == {
        "builder": "openai/gpt-5",
        "planner": "anthropic/claude-sonnet-4",
    }


def test_build_project_rejects_non_dict_model_overrides(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, model_overrides=["builder"])  # type: ignore[arg-type]


def test_build_project_rejects_empty_model_override_key(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, model_overrides={"  ": "openai/gpt-5"})


def test_build_project_rejects_empty_model_override_value(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, model_overrides={"builder": "  "})


def test_build_project_accepts_default_temperature_and_thinking(tmp_path: Path) -> None:
    project = build_project(
        "vbot",
        "vBot",
        tmp_path,
        default_temperature=0.0,
        default_thinking_effort="medium",
    )

    # 0.0 is a real value (the chain's floor), not "unset".
    assert project.default_temperature == 0.0
    assert project.default_thinking_effort == "medium"


def test_build_project_accepts_empty_thinking_effort_as_provider_default(tmp_path: Path) -> None:
    # "" is the explicit "provider default" value, distinct from None ("no default").
    project = build_project("vbot", "vBot", tmp_path, default_thinking_effort="")

    assert project.default_thinking_effort == ""


def test_build_project_rejects_temperature_out_of_range(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, default_temperature=3.0)


def test_build_project_rejects_unknown_thinking_effort(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, default_thinking_effort="ultra")


def test_build_project_rejects_invalid_project_id(tmp_path: Path) -> None:
    with pytest.raises(InvalidProjectIdError):
        build_project("bad/slug", "Bad", tmp_path)


def test_build_project_rejects_empty_display_name(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "   ", tmp_path)


def test_build_project_rejects_empty_cwd() -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", "   ")


def test_build_project_rejects_non_string_auto_load_entry(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        build_project("vbot", "vBot", tmp_path, auto_load=["AGENTS.md", 7])  # type: ignore[list-item]


def test_seed_default_auto_load_seeds_agents_file_into_empty() -> None:
    assert seed_default_auto_load(None) == ["AGENTS.md"]
    assert seed_default_auto_load([]) == ["AGENTS.md"]


def test_seed_default_auto_load_prepends_before_user_files() -> None:
    assert seed_default_auto_load(["CONTEXT.md"]) == ["AGENTS.md", "CONTEXT.md"]


def test_seed_default_auto_load_is_idempotent_case_insensitive() -> None:
    # An already-named AGENTS.md (any case) is not duplicated; the user's spelling
    # and ordering survive untouched. A path-qualified agents.md is a different file,
    # so the root convention is still seeded ahead of it.
    assert seed_default_auto_load(["AGENTS.md", "CONTEXT.md"]) == ["AGENTS.md", "CONTEXT.md"]
    assert seed_default_auto_load(["agents.md"]) == ["agents.md"]
    assert seed_default_auto_load(["docs/agents.md"]) == ["AGENTS.md", "docs/agents.md"]


def test_build_project_preserves_explicit_timestamps(tmp_path: Path) -> None:
    project = build_project(
        "vbot",
        "vBot",
        tmp_path,
        created_at="2026-06-18T10:00:00Z",
        updated_at="2026-06-18T11:00:00Z",
    )

    assert project.created_at == "2026-06-18T10:00:00Z"
    assert project.updated_at == "2026-06-18T11:00:00Z"


def test_to_dict_round_trips_through_project_from_dict(tmp_path: Path) -> None:
    project = build_project(
        "vbot",
        "vBot",
        tmp_path,
        default_agent="orchestrator",
        default_model="openai/gpt-5",
        default_temperature=0.4,
        default_thinking_effort="high",
        auto_load=["AGENTS.md"],
        allowed_tools=["read", "grep"],
        skills_bundled_enabled=["frontend-design"],
        skills_project_disabled=["debugging"],
        model_overrides={"builder": "openai/gpt-mini"},
    )

    restored = project_from_dict(project.to_dict())

    assert restored == project


def test_to_dict_has_stable_field_set(tmp_path: Path) -> None:
    project = build_project("vbot", "vBot", tmp_path)

    assert set(project.to_dict()) == {
        "project_id",
        "display_name",
        "cwd",
        "default_agent",
        "default_model",
        "default_temperature",
        "default_thinking_effort",
        "auto_load",
        "allowed_tools",
        "skills_bundled_enabled",
        "skills_global_enabled",
        "skills_project_disabled",
        "model_overrides",
        "created_at",
        "updated_at",
    }


def test_project_from_dict_defaults_optional_fields() -> None:
    data = {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/srv/repos/vbot",
        "created_at": "2026-06-18T10:00:00Z",
        "updated_at": "2026-06-18T10:00:00Z",
    }

    project = project_from_dict(data)

    assert project.default_agent == ""
    assert project.default_model == ""
    assert project.default_temperature is None
    assert project.default_thinking_effort is None
    assert project.auto_load == []
    # An old project.json without the whitelist fields loads at the same defaults a
    # new project is seeded with: base tool list, empty skill lists (decision 10).
    assert project.allowed_tools == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert project.skills_bundled_enabled == []
    assert project.skills_project_disabled == []
    # An old project.json without model_overrides loads at the empty map.
    assert project.model_overrides == {}


def test_project_from_dict_preserves_explicit_empty_allowed_tools() -> None:
    # A persisted empty Tool Whitelist (user turned every tool off) must survive a
    # reload — only an *absent* field falls back to the base list.
    data = {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/srv/repos/vbot",
        "allowed_tools": [],
        "created_at": "2026-06-18T10:00:00Z",
        "updated_at": "2026-06-18T10:00:00Z",
    }

    project = project_from_dict(data)

    assert project.allowed_tools == []


def test_project_is_frozen(tmp_path: Path) -> None:
    project = build_project("vbot", "vBot", tmp_path)

    with pytest.raises(FrozenInstanceError):
        project.display_name = "changed"  # type: ignore[misc]


def test_auto_load_is_copied_not_aliased(tmp_path: Path) -> None:
    source = ["AGENTS.md"]
    project = build_project("vbot", "vBot", tmp_path, auto_load=source)
    source.append("PROJECT.md")

    assert project.auto_load == ["AGENTS.md"]


def test_project_construction_is_a_plain_dataclass() -> None:
    project = Project(
        project_id="vbot",
        display_name="vBot",
        cwd="/srv/repos/vbot",
        created_at="2026-06-18T10:00:00Z",
        updated_at="2026-06-18T10:00:00Z",
    )

    assert project.auto_load == []
