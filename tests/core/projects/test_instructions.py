"""Contracts of the main instruction file and the folder instructions of a Project."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.projects import FolderInstructions, ProjectStore


def _write(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Rules of {name}.", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("files", "main"),
    [
        (("CLAUDE.md", "AGENTS.md", "GEMINI.md"), "AGENTS.md"),
        (("GEMINI.md", "CLAUDE.md"), "CLAUDE.md"),
        ((".claude/CLAUDE.md", ".github/copilot-instructions.md"), ".claude/CLAUDE.md"),
        ((".github/copilot-instructions.md",), ".github/copilot-instructions.md"),
        ((), None),
    ],
)
def test_project_loads_its_main_instruction_file_first_then_additional_files(
    tmp_path: Path, files: tuple[str, ...], main: str | None
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in files:
        _write(repo, name)
    projects = ProjectStore(tmp_path / "data")
    # An additional entry naming the main file again loads it once.
    project = projects.create("repo", "Repo", repo, auto_load=["docs/style.md", *files[:1]])

    expected = [main] if main is not None else []
    assert project.instruction_files == [
        *expected,
        *(name for name in ["docs/style.md", *files[:1]] if name not in expected),
    ]
    off = projects.update("repo", auto_instructions=False)
    assert off.instruction_files == ["docs/style.md", *files[:1]]


def test_main_instruction_file_follows_the_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "CLAUDE.md")
    project = ProjectStore(tmp_path / "data").create("repo", "Repo", repo)
    assert project.instruction_files == ["CLAUDE.md"]

    _write(repo, "AGENTS.md")
    assert project.instruction_files == ["AGENTS.md"]


def test_folder_instructions_govern_paths_below_their_folder(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "AGENTS.md")
    outer = _write(repo, "app/AGENTS.md")
    # A folder holding several conventions contributes its first one only.
    inner = _write(repo, "app/ui/CLAUDE.md")
    _write(repo, "app/ui/GEMINI.md")
    loaded = _write(repo, "docs/AGENTS.md")
    _write(repo, "node_modules/pkg/AGENTS.md")
    _write(repo, ".claude/CLAUDE.md")
    _write(repo, "app/ui/widget.py")
    finder = FolderInstructions.for_project(repo, ["AGENTS.md", "docs/AGENTS.md"])

    # Outermost first; the repository root's file is the main instruction file.
    assert finder.files_for([repo / "app/ui/widget.py"]) == [outer, inner]
    # A folder path counts as the folder itself.
    assert finder.files_for([repo / "app/ui"]) == [outer, inner]
    # Files the Agent already has are left out: shown before, read by this batch
    # or loaded by the System Prompt.
    assert finder.files_for([repo / "app/ui/widget.py"], seen=[outer]) == [inner]
    assert finder.files_for([repo / "app/ui/CLAUDE.md"]) == [outer]
    assert finder.files_for([loaded.parent / "guide.md"]) == []
    # A main instruction file location is repository-wide, whichever main file loads.
    assert finder.files_for([repo / ".claude/skills/s/SKILL.md"]) == []
    # Package folders and paths outside the repository contribute nothing.
    assert finder.files_for([repo / "node_modules/pkg/index.js"]) == []
    assert finder.files_for([tmp_path / "elsewhere.py", repo / "README.md"]) == []
    # Several paths of one batch share their folders' files.
    assert finder.files_for([repo / "app/a.py", repo / "app/ui/b.py"]) == [outer, inner]
