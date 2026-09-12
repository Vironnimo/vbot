"""Tests for storage prompts."""

from pathlib import Path

import pytest

from core.storage import (
    StorageError,
    StorageManager,
)


def create_prompt_resources(resources_dir: Path, *, include_compaction: bool = True) -> None:
    environment_template = resources_dir / "data-dir" / ".env.example"
    environment_template.parent.mkdir(parents=True)
    environment_template.write_text("# test environment\n", encoding="utf-8")
    prompts_dir = resources_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    prompt_names = [
        "identity_runtime.md",
        "runtime.md",
        "working_project.md",
        "tools.md",
        "tools_list.md",
        "channels.md",
        "skills.md",
        "skill_maintenance.md",
        # Backend-only fragments, always bundled (like compaction/handoff/learn).
        "reflect-memory.md",
        "reflect-skill.md",
        "reflect.md",
    ]
    if include_compaction:
        prompt_names.append("compaction.md")

    for name in prompt_names:
        prompts_dir.joinpath(name).write_text(f"{name} bundled", encoding="utf-8")


def test_read_prompt_fragment_prefers_user_copy(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()
    (data_dir / "prompts" / "runtime.md").write_text("custom runtime", encoding="utf-8")

    assert storage.read_prompt_fragment("runtime.md") == "custom runtime"


def test_read_prompt_fragment_falls_back_to_bundled_resource(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    assert storage.read_prompt_fragment("skills.md") == "skills.md bundled"


def test_read_prompt_fragment_skill_maintenance_resolves_bundled_resource(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    assert storage.read_prompt_fragment("skill_maintenance.md") == "skill_maintenance.md bundled"


@pytest.mark.parametrize("fragment_name", ["reflect-memory.md", "reflect-skill.md", "reflect.md"])
def test_read_prompt_fragment_reflect_resolves_bundled_resource(
    tmp_path: Path, fragment_name: str
) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    assert storage.read_prompt_fragment(fragment_name) == f"{fragment_name} bundled"


def test_read_prompt_fragment_compaction_name_passes_allowlist_check(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir, include_compaction=False)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    with pytest.raises(StorageError, match="compaction\\.md"):
        storage.read_prompt_fragment("compaction.md")


def test_read_prompt_fragment_rejects_path_traversal(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.read_prompt_fragment("../runtime.md")


def test_read_prompt_fragment_rejects_unknown_names(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.read_prompt_fragment("other.md")


def test_copy_agent_prompt_fragments_seeds_editable_defaults_only(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    storage.ensure_directories()
    # A hand-created data-dir copy overrides the bundled default and seeds the scope.
    (data_dir / "prompts" / "runtime.md").write_text("custom default runtime", encoding="utf-8")

    written_paths = storage.copy_agent_prompt_fragments("coder")

    assert sorted(path.name for path in written_paths) == [
        "channels.md",
        "identity_runtime.md",
        "runtime.md",
        "skill_maintenance.md",
        "skills.md",
        "tools.md",
        "tools_list.md",
    ]
    assert storage.read_agent_prompt_fragment("coder", "runtime.md") == "custom default runtime"
    assert not (data_dir / "agents" / "coder" / "prompts" / "compaction.md").exists()


def test_copy_agent_prompt_fragments_preserves_existing_files(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    create_prompt_resources(resources_dir)
    storage = StorageManager(data_dir, resources_dir=resources_dir)
    agent_prompts_dir = data_dir / "agents" / "coder" / "prompts"
    agent_prompts_dir.mkdir(parents=True)
    (agent_prompts_dir / "runtime.md").write_text("custom agent runtime", encoding="utf-8")

    storage.copy_agent_prompt_fragments("coder")

    assert storage.read_agent_prompt_fragment("coder", "runtime.md") == "custom agent runtime"


def test_read_missing_agent_prompt_fragment_returns_empty_string(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)

    assert storage.read_agent_prompt_fragment("coder", "skills.md") == ""


@pytest.mark.parametrize(
    ("agent_id", "fragment_name", "_message"),
    [
        ("../escape", "runtime.md", "Unsafe agent id"),
        ("coder", "../runtime.md", "Unsafe Agent prompt fragment name"),
        ("coder", "compaction.md", "Unknown Agent prompt fragment"),
    ],
)
def test_agent_prompt_fragments_reject_unsafe_paths(
    tmp_path: Path,
    agent_id: str,
    fragment_name: str,
    _message: str,
) -> None:
    storage = StorageManager(tmp_path / "data")

    with pytest.raises(StorageError):
        storage.read_agent_prompt_fragment(agent_id, fragment_name)
