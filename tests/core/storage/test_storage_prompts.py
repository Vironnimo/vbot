"""Prompt fragments: user copies over bundled resources, and per-Agent editable copies."""

from collections.abc import Callable
from pathlib import Path

import pytest

from core.storage import StorageError, StorageManager

EDITABLE_FRAGMENTS = [
    "channels.md",
    "identity_runtime.md",
    "runtime.md",
    "skill_maintenance.md",
    "skills.md",
    "tools.md",
    "tools_list.md",
]


def create_prompt_resources(resources_dir: Path, *, include_compaction: bool = True) -> None:
    environment_template = resources_dir / "data-dir" / ".env.example"
    environment_template.parent.mkdir(parents=True)
    environment_template.write_text("# test environment\n", encoding="utf-8")
    prompts_dir = resources_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    prompt_names = [
        *EDITABLE_FRAGMENTS,
        # Backend-only brief fragments, always bundled (like compaction/handoff).
        "learn-intro.md",
        "skill-ladder.md",
    ]
    if include_compaction:
        prompt_names.append("compaction.md")

    for name in prompt_names:
        prompts_dir.joinpath(name).write_text(f"{name} bundled", encoding="utf-8")


@pytest.fixture
def storage(tmp_path: Path) -> StorageManager:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir)
    return StorageManager(tmp_path / "data", resources_dir=resources_dir)


@pytest.mark.parametrize(
    ("fragment_name", "expected"),
    [
        ("runtime.md", "custom runtime"),
        ("skills.md", "skills.md bundled"),
        ("skill_maintenance.md", "skill_maintenance.md bundled"),
        ("skill-ladder.md", "skill-ladder.md bundled"),
    ],
)
def test_read_prompt_fragment_prefers_the_user_copy_over_the_bundled_resource(
    storage: StorageManager, fragment_name: str, expected: str
) -> None:
    storage.ensure_directories()
    (storage.prompts_dir / "runtime.md").write_text("custom runtime", encoding="utf-8")

    assert storage.read_prompt_fragment(fragment_name) == expected


@pytest.mark.parametrize(
    ("fragment_name", "match", "user_copy_denied"),
    [
        ("../runtime.md", None, False),
        ("other.md", None, False),
        ("compaction.md", r"compaction\.md", False),
        # The bundled default never silently replaces a user copy it cannot read.
        ("runtime.md", r"runtime\.md", True),
    ],
    ids=["path-traversal", "unknown-name", "known-name-without-resource", "unreadable-user-copy"],
)
def test_read_prompt_fragment_rejects_unknown_missing_or_unreadable_fragments(
    tmp_path: Path,
    deny_access: Callable[[Path], None],
    fragment_name: str,
    match: str | None,
    user_copy_denied: bool,
) -> None:
    resources_dir = tmp_path / "resources"
    create_prompt_resources(resources_dir, include_compaction=False)
    storage = StorageManager(tmp_path / "data", resources_dir=resources_dir)
    if user_copy_denied:
        storage.ensure_directories()
        (storage.prompts_dir / fragment_name).write_text("custom", encoding="utf-8")
        deny_access(storage.prompts_dir)

    with pytest.raises(StorageError, match=match):
        storage.read_prompt_fragment(fragment_name)


def test_copy_agent_prompt_fragments_seeds_editable_defaults_and_keeps_agent_copies(
    storage: StorageManager,
    monkeypatch: pytest.MonkeyPatch,
    deny_access: Callable[[Path], None],
) -> None:
    storage.ensure_directories()
    # A hand-created data-dir copy overrides the bundled default and seeds the scope.
    (storage.prompts_dir / "runtime.md").write_text("custom default runtime", encoding="utf-8")
    agent_prompts_dir = storage.agent_prompts_dir("coder")
    agent_prompts_dir.mkdir(parents=True)
    (agent_prompts_dir / "skills.md").write_text("custom agent skills", encoding="utf-8")

    written_paths = storage.copy_agent_prompt_fragments("coder")

    assert sorted(path.name for path in written_paths) == [
        name for name in EDITABLE_FRAGMENTS if name != "skills.md"
    ]
    assert storage.read_agent_prompt_fragment("coder", "runtime.md") == "custom default runtime"
    assert storage.read_agent_prompt_fragment("coder", "skills.md") == "custom agent skills"
    assert not (agent_prompts_dir / "compaction.md").exists()

    # Copies that cannot be checked are neither replaced nor read as empty.
    deny_access(agent_prompts_dir)
    with pytest.raises(StorageError):
        storage.copy_agent_prompt_fragments("coder")
    with pytest.raises(StorageError):
        storage.read_agent_prompt_fragment("coder", "skills.md")
    monkeypatch.undo()
    assert storage.read_agent_prompt_fragment("coder", "skills.md") == "custom agent skills"


def test_read_missing_agent_prompt_fragment_returns_empty_string(storage: StorageManager) -> None:
    assert storage.read_agent_prompt_fragment("coder", "skills.md") == ""


@pytest.mark.parametrize(
    ("agent_id", "fragment_name"),
    [("../escape", "runtime.md"), ("coder", "../runtime.md"), ("coder", "compaction.md")],
    ids=["unsafe-agent-id", "unsafe-fragment-name", "non-editable-fragment"],
)
def test_agent_prompt_fragments_reject_unsafe_paths(
    storage: StorageManager, agent_id: str, fragment_name: str
) -> None:
    with pytest.raises(StorageError):
        storage.read_agent_prompt_fragment(agent_id, fragment_name)
