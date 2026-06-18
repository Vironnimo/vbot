"""Tests for the Project entity, field validation, and serialization."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.projects.projects import (
    InvalidProjectIdError,
    Project,
    ProjectError,
    build_project,
    project_from_dict,
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
    assert project.auto_load == []


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
        auto_load=["AGENTS.md"],
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
        "auto_load",
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
    assert project.auto_load == []


def test_project_is_frozen(tmp_path: Path) -> None:
    project = build_project("vbot", "vBot", tmp_path)

    with pytest.raises(Exception):
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
