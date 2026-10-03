"""Tests for the Project entity, field validation, and serialization."""

from __future__ import annotations

import os
import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from core.projects.projects import (
    OVERRIDE_FIELDS,
    PROJECT_DEFAULT_ALLOWED_TOOLS,
    InvalidProjectIdError,
    ProjectError,
    build_project,
    project_from_dict,
    seed_default_auto_load,
)
from core.utils.timestamps import is_canonical_timestamp

_TIMESTAMP = "2026-06-18T10:00:00Z"


def test_build_project_fills_every_optional_field_with_its_default(tmp_path: Path) -> None:
    # A blank display name falls back to the Project id.
    payload = build_project("scratch", "   ", tmp_path).to_dict()
    created_at = payload.pop("created_at")
    updated_at = payload.pop("updated_at")

    assert payload == {
        "project_id": "scratch",
        "display_name": "scratch",
        "cwd": str(Path(os.path.realpath(tmp_path))),
        "default_agent": "",
        "default_model": "",
        "default_temperature": None,
        "default_thinking_effort": None,
        # No default_top_p: an unset one is not written.
        "source_format": "opencode",
        "auto_load": [],
        "allowed_tools": list(PROJECT_DEFAULT_ALLOWED_TOOLS),
        "skills_bundled_enabled": [],
        "skills_global_enabled": [],
        "skills_project_disabled": [],
        "overrides": {},
    }
    assert is_canonical_timestamp(created_at)
    assert updated_at == created_at
    # The base Tool Whitelist uses the successor of the retired edit Tool.
    assert "apply_patch" in PROJECT_DEFAULT_ALLOWED_TOOLS
    assert "edit" not in PROJECT_DEFAULT_ALLOWED_TOOLS


def test_build_project_keeps_every_explicit_field_through_a_round_trip(tmp_path: Path) -> None:
    fields: dict[str, Any] = {
        "default_agent": "orchestrator",
        "default_model": "openai/gpt-5",
        # 0.0 is the sampling floor and "" the explicit Provider default; neither is unset.
        "default_temperature": 0.0,
        "default_thinking_effort": "",
        "default_top_p": 0.0,
        "source_format": "claude",
        "auto_load": ["AGENTS.md"],
        "allowed_tools": ["read", "grep"],
        "skills_bundled_enabled": ["frontend-design"],
        "skills_global_enabled": ["pdf", "deploy"],
        "skills_project_disabled": ["debugging"],
        "overrides": {
            "builder": {"model": "openai/gpt-5", "temperature": 0.4, "thinking_effort": "high"},
            "planner": {"model": "anthropic/claude-sonnet-4"},
            "floor": {"temperature": 0.0, "top_p": 0.0, "thinking_effort": ""},
            "reviewer": {
                "tool_access": {
                    "mode": "selected",
                    "allowed": ["read"],
                    "denied": ["session_read"],
                }
            },
            # An entry holding only fields this vBot does not model loads as ``{}``.
            "future": {},
        },
        "created_at": _TIMESTAMP,
        "updated_at": "2026-06-18T11:00:00Z",
    }

    project = build_project("vbot", "vBot", tmp_path, **fields)

    assert project.to_dict() == {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": str(Path(os.path.realpath(tmp_path))),
        **fields,
    }
    assert project_from_dict(project.to_dict()) == project


def test_explicit_empty_tool_whitelist_is_kept(tmp_path: Path) -> None:
    # [] turns every Tool off; only an absent field seeds the base list.
    stored = {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/srv/repos/vbot",
        "allowed_tools": [],
        "created_at": _TIMESTAMP,
        "updated_at": _TIMESTAMP,
    }

    assert build_project("vbot", "vBot", tmp_path, allowed_tools=[]).allowed_tools == []
    assert project_from_dict(stored).allowed_tools == []


@pytest.mark.parametrize(
    ("project_id", "cwd", "fields", "error", "message"),
    [
        pytest.param(
            "bad/slug",
            None,
            {},
            InvalidProjectIdError,
            "Project id must be 1-64 characters",
            id="project-id",
        ),
        pytest.param("vbot", "   ", {}, ProjectError, "cwd must be a non-empty path", id="cwd"),
        pytest.param(
            "vbot",
            None,
            {"default_temperature": 3.0},
            ProjectError,
            "default_temperature must be between 0 and 2",
            id="default-temperature",
        ),
        pytest.param(
            "vbot",
            None,
            {"default_top_p": 1.5},
            ProjectError,
            "default_top_p must be between 0 and 1",
            id="default-top-p",
        ),
        pytest.param(
            "vbot",
            None,
            {"default_thinking_effort": "ultra"},
            ProjectError,
            "default_thinking_effort must be one of",
            id="default-thinking-effort",
        ),
        pytest.param(
            "vbot",
            None,
            {"source_format": "cursor"},
            ProjectError,
            "source_format must be one of: opencode, claude",
            id="source-format",
        ),
        pytest.param(
            "vbot",
            None,
            {"auto_load": ["AGENTS.md", 7]},
            ProjectError,
            "auto_load entries must be non-empty strings",
            id="auto-load-entry",
        ),
        pytest.param(
            "vbot",
            None,
            {"allowed_tools": ["read", 7]},
            ProjectError,
            "allowed_tools entries must be non-empty strings",
            id="tool-entry",
        ),
        pytest.param(
            "vbot",
            None,
            {"allowed_tools": ["read", "*"]},
            ProjectError,
            "allowed_tools cannot contain the all-tools wildcard '*' for a Project",
            id="tool-wildcard",
        ),
        pytest.param(
            "vbot",
            None,
            {"skills_bundled_enabled": "frontend"},
            ProjectError,
            "skills_bundled_enabled must be a list of strings",
            id="bundled-skills",
        ),
        pytest.param(
            "vbot",
            None,
            {"skills_global_enabled": "pdf"},
            ProjectError,
            "skills_global_enabled must be a list of strings",
            id="global-skills",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": ["builder"]},
            ProjectError,
            "overrides must be an object",
            id="overrides",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": {"  ": {"model": "openai/gpt-5"}}},
            ProjectError,
            "overrides keys must be non-empty agent id strings",
            id="override-key",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": {"builder": "openai/gpt-5"}},
            ProjectError,
            "overrides['builder'] must be an object",
            id="override-value",
        ),
        # Building a Project is strict; raw file validation only warns.
        pytest.param(
            "vbot",
            None,
            {"overrides": {"builder": {"nope": "x"}}},
            ProjectError,
            "overrides['builder'] has unknown fields: nope",
            id="override-unknown-field",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": {"builder": {"model": "  "}}},
            ProjectError,
            "overrides['builder'].model must be a non-empty model string",
            id="override-model",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": {"builder": {"temperature": 3.0}}},
            ProjectError,
            "overrides['builder'].temperature must be between 0 and 2",
            id="override-temperature",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": {"builder": {"top_p": 1.5}}},
            ProjectError,
            "overrides['builder'].top_p must be between 0 and 1",
            id="override-top-p",
        ),
        pytest.param(
            "vbot",
            None,
            {"overrides": {"builder": {"thinking_effort": "ultra"}}},
            ProjectError,
            "overrides['builder'].thinking_effort must be one of",
            id="override-thinking-effort",
        ),
        pytest.param(
            "vbot",
            None,
            {
                "allowed_tools": ["read"],
                "overrides": {
                    "reviewer": {"tool_access": {"mode": "selected", "allowed": ["bash"]}}
                },
            },
            ProjectError,
            "overrides['reviewer'].tool_access.allowed contains Tools outside the Project "
            "Tool Whitelist: bash",
            id="override-tool-outside-ceiling",
        ),
    ],
)
def test_build_project_rejects_invalid_fields(
    tmp_path: Path,
    project_id: str,
    cwd: str | None,
    fields: dict[str, Any],
    error: type[ProjectError],
    message: str,
) -> None:
    with pytest.raises(error, match=re.escape(message)):
        build_project(project_id, "vBot", tmp_path if cwd is None else cwd, **fields)


def test_override_fields_constant_contains_all_overridable_fields() -> None:
    assert (
        frozenset(
            {"model", "temperature", "top_p", "thinking_effort", "compaction_policy", "tool_access"}
        )
        == OVERRIDE_FIELDS
    )


@pytest.mark.parametrize(
    ("auto_load", "seeded"),
    [
        (None, ["AGENTS.md"]),
        (["CONTEXT.md"], ["AGENTS.md", "CONTEXT.md"]),
        # An already-named AGENTS.md (any case) is not duplicated; the user's spelling
        # and ordering survive untouched.
        (["AGENTS.md", "CONTEXT.md"], ["AGENTS.md", "CONTEXT.md"]),
        (["agents.md"], ["agents.md"]),
        # A path-qualified agents.md is a different file.
        (["docs/agents.md"], ["AGENTS.md", "docs/agents.md"]),
    ],
)
def test_seed_default_auto_load_prepends_agents_file_once(
    auto_load: list[str] | None, seeded: list[str]
) -> None:
    assert seed_default_auto_load(auto_load) == seeded


def test_project_from_dict_defaults_optional_fields() -> None:
    stored = {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/srv/repos/vbot",
        "allowed_tools": ["read_file"],
        "created_at": _TIMESTAMP,
        "updated_at": _TIMESTAMP,
    }

    assert project_from_dict(stored).to_dict() == {
        **stored,
        "default_agent": "",
        "default_model": "",
        "default_temperature": None,
        "default_thinking_effort": None,
        # An old project.json without these fields loads at their defaults.
        "source_format": "opencode",
        "auto_load": [],
        "skills_bundled_enabled": [],
        "skills_global_enabled": [],
        "skills_project_disabled": [],
        "overrides": {},
    }


def test_project_is_frozen_and_copies_its_inputs(tmp_path: Path) -> None:
    source = ["AGENTS.md"]
    project = build_project("vbot", "vBot", tmp_path, auto_load=source)
    source.append("PROJECT.md")

    assert project.auto_load == ["AGENTS.md"]
    with pytest.raises(FrozenInstanceError):
        project.display_name = "changed"  # type: ignore[misc]
