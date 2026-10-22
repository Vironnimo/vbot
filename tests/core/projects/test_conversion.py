"""The conversion of ``project.json`` format version 1 to version 2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.database import write_bootstrap_marker
from core.projects import PROJECT_FORMAT_VERSION
from core.projects.conversion import convert_project_documents, pending_project_documents
from core.projects.store import ProjectStore

# Before the instruction settings changed meaning (2026-10-20) and after it.
_OLD = "2026-10-01T10:00:00.000000Z"
_NEW = "2026-10-21T10:00:00.000000Z"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    write_bootstrap_marker(directory)
    return directory


def _write_v1(data_dir: Path, repo: Path, **fields: Any) -> Path:
    path = data_dir / "projects" / "repo" / "project.json"
    path.parent.mkdir(parents=True)
    document = {
        "format_version": 1,
        "project_id": "repo",
        "cwd": str(repo),
        "allowed_tools": ["read"],
        "created_at": _OLD,
        **fields,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _sources(*switches: tuple[str, bool]) -> list[dict[str, Any]]:
    return [{"id": "claude.agents", "enabled": True}] + [
        {"id": source_id, "enabled": enabled} for source_id, enabled in switches
    ]


@pytest.mark.parametrize(
    ("files", "fields", "auto_load", "auto_instructions", "loaded"),
    [
        # The seeded AGENTS.md plus the CLAUDE.md Source the user switched on.
        pytest.param(
            ["AGENTS.md", "CLAUDE.md"],
            {"auto_load": ["AGENTS.md"], "sources": _sources(("claude.instructions", True))},
            ["AGENTS.md", "CLAUDE.md"],
            True,
            ["AGENTS.md", "CLAUDE.md"],
            id="seeded-file-and-instruction-source",
        ),
        # AGENTS.md removed from auto_load, the instruction Sources left off.
        pytest.param(
            ["AGENTS.md", "CLAUDE.md"],
            {"auto_load": [], "sources": _sources(("claude.instructions", False))},
            [],
            False,
            [],
            id="nothing-loaded",
        ),
        # Without AGENTS.md the Source's file was the main instruction file.
        pytest.param(
            ["CLAUDE.md", "GEMINI.md"],
            {
                "auto_load": ["AGENTS.md"],
                "sources": _sources(("claude.instructions", False), ("gemini.instructions", True)),
            },
            ["AGENTS.md", "GEMINI.md"],
            False,
            ["AGENTS.md", "GEMINI.md"],
            id="main-file-off-other-source-on",
        ),
        pytest.param(
            ["CLAUDE.md", ".claude/CLAUDE.md"],
            {"auto_load": ["AGENTS.md"], "sources": _sources(("claude.instructions", True))},
            ["AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md"],
            True,
            ["CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md"],
            id="source-with-two-files",
        ),
        # Re-saved by a vBot that already had the new meaning: the user added the file.
        pytest.param(
            ["AGENTS.md", "CLAUDE.md"],
            {
                "auto_load": ["AGENTS.md", "CLAUDE.md"],
                "sources": _sources(("claude.instructions", True)),
            },
            ["AGENTS.md", "CLAUDE.md"],
            True,
            ["AGENTS.md", "CLAUDE.md"],
            id="file-already-listed",
        ),
        # Switched off with the new meaning: the user's later choice stays.
        pytest.param(
            ["AGENTS.md", "CLAUDE.md"],
            {
                "auto_load": [],
                "auto_instructions": False,
                "sources": _sources(("claude.instructions", True)),
            },
            [],
            False,
            [],
            id="switch-already-written",
        ),
        # Created with the new meaning: nothing to keep.
        pytest.param(
            ["AGENTS.md"],
            {"auto_load": [], "sources": _sources(), "created_at": _NEW},
            [],
            True,
            ["AGENTS.md"],
            id="created-after-the-change",
        ),
        # Created before it, in a repository with AGENTS.md only, the seed removed.
        pytest.param(
            ["AGENTS.md"],
            {"auto_load": [], "sources": _sources()},
            [],
            False,
            [],
            id="seed-removed-without-instruction-sources",
        ),
        pytest.param(
            [],
            {"auto_load": [], "sources": _sources(("claude.instructions", True))},
            [],
            True,
            [],
            id="no-instruction-file-yet",
        ),
    ],
)
def test_conversion_keeps_the_instruction_files_a_project_loaded(
    data_dir: Path,
    tmp_path: Path,
    files: list[str],
    fields: dict[str, Any],
    auto_load: list[str],
    auto_instructions: bool,
    loaded: list[str],
) -> None:
    repo = tmp_path / "repo"
    for name in files:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(name, encoding="utf-8")
    path = _write_v1(data_dir, repo, future_field={"keep": True}, **fields)

    assert pending_project_documents(data_dir) == ("projects/repo/project.json",)
    result = convert_project_documents(data_dir)

    assert result.converted == ("projects/repo/project.json",)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["format_version"] == PROJECT_FORMAT_VERSION
    assert stored["future_field"] == {"keep": True}
    assert [source["id"] for source in stored["sources"]] == ["claude.agents"]
    project = ProjectStore(data_dir).get("repo")
    assert project.auto_load == auto_load
    assert project.auto_instructions is auto_instructions
    assert project.instruction_files == loaded


def test_conversion_changes_only_documents_of_version_1(data_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("a", encoding="utf-8")
    path = _write_v1(data_dir, repo, auto_load=[])
    convert_project_documents(data_dir)
    converted = path.read_bytes()
    unreadable = data_dir / "projects" / "broken" / "project.json"
    unreadable.parent.mkdir()
    unreadable.write_text("{not json", encoding="utf-8")

    # A second pass, also over a document a newer vBot wrote, changes nothing.
    newer = data_dir / "projects" / "newer" / "project.json"
    newer.parent.mkdir()
    newer.write_text(json.dumps({"format_version": PROJECT_FORMAT_VERSION + 1}), encoding="utf-8")
    assert pending_project_documents(data_dir) == ()
    result = convert_project_documents(data_dir)

    assert result.converted == ()
    assert result.invalid == ("projects/broken/project.json",)
    assert path.read_bytes() == converted
    assert unreadable.read_text(encoding="utf-8") == "{not json"
    assert json.loads(newer.read_text(encoding="utf-8")) == {
        "format_version": PROJECT_FORMAT_VERSION + 1
    }
