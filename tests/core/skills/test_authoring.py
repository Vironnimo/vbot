"""Tests for the validated skill authoring write core."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.skills.authoring import (
    PROVENANCE_AUTHOR_KEY,
    PROVENANCE_SOURCE_KEY,
    SkillAuthoringError,
    SkillAuthoringService,
)
from core.skills.requirements import REQUIREMENTS_METADATA_KEY
from core.skills.skills import SkillRegistry


def skill_document(
    name: str = "demo", description: str = "Do a demo task.", body: str = "# Demo\n"
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def read_front_matter(skill_file: Path) -> Any:
    _, front, _ = skill_file.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(front)


def _replace(old: str, new: str) -> Callable[[str], str]:
    def edit(text: str) -> str:
        assert "\r" not in text
        return text.replace(old, new)

    return edit


@pytest.fixture
def service() -> SkillAuthoringService:
    return SkillAuthoringService()


def test_create_writes_an_lf_document_the_registry_loads(
    service: SkillAuthoringService, tmp_path: Path
) -> None:
    result = service.create(tmp_path, "demo", skill_document(body="# Demo\nSteps."), author="agent")

    skill_file = tmp_path / "demo" / "SKILL.md"
    assert (result.name, result.operation, result.path) == ("demo", "create", skill_file)
    assert "# Demo\nSteps." in skill_file.read_text(encoding="utf-8")
    assert b"\r" not in skill_file.read_bytes()
    assert SkillRegistry.load(tmp_path).get("demo").description == "Do a demo task."
    with pytest.raises(SkillAuthoringError):
        service.create(tmp_path, "demo", skill_document(), author="agent")


def test_provenance_is_stamped_into_vbot_metadata_beside_requirements(
    service: SkillAuthoringService, tmp_path: Path
) -> None:
    content = (
        "---\nname: demo\ndescription: Do a demo task.\n"
        "metadata:\n  vbot:\n    requirements:\n      all:\n        - binary: git\n---\n\n# Demo\n"
    )

    service.create(tmp_path, "demo", content, author="human", source="https://example.com/howto")

    vbot = read_front_matter(tmp_path / "demo" / "SKILL.md")["metadata"][REQUIREMENTS_METADATA_KEY]
    assert vbot[PROVENANCE_AUTHOR_KEY] == "human"
    assert vbot[PROVENANCE_SOURCE_KEY] == "https://example.com/howto"
    # Requirements survive the stamp; catalog fields stay free of provenance.
    skill = SkillRegistry.load(tmp_path).get("demo")
    assert skill.requirements.required is not None
    assert (skill.name, skill.description) == ("demo", "Do a demo task.")


def test_edit_replaces_the_document(service: SkillAuthoringService, tmp_path: Path) -> None:
    service.create(tmp_path, "demo", skill_document(), author="agent")

    service.edit(
        tmp_path, "demo", skill_document(description="Updated.", body="# New\n"), author="human"
    )

    assert SkillRegistry.load(tmp_path).get("demo").description == "Updated."
    assert "# New" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")


def test_rewrite_applies_an_edit_of_the_lf_text(
    service: SkillAuthoringService, tmp_path: Path
) -> None:
    service.create(tmp_path, "demo", skill_document(body="# Demo\nold line"), author="agent")

    result = service.rewrite(
        tmp_path, "demo", "SKILL.md", _replace("old line", "new line"), author="agent"
    )

    assert result.operation == "rewrite"
    assert "new line" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")


def _refuse(_text: str) -> str:
    raise LookupError("no match")


@pytest.mark.parametrize(
    ("edit", "error", "match"),
    [
        (_refuse, LookupError, "no match"),
        (
            _replace("name: demo", "name: other"),
            SkillAuthoringError,
            "must match its directory name",
        ),
    ],
    ids=["edit-fails", "result-invalid"],
)
def test_failed_or_invalid_rewrite_writes_nothing(
    service: SkillAuthoringService,
    tmp_path: Path,
    edit: Callable[[str], str],
    error: type[Exception],
    match: str,
) -> None:
    service.create(tmp_path, "demo", skill_document(), author="agent")
    skill_file = tmp_path / "demo" / "SKILL.md"
    before = skill_file.read_bytes()

    with pytest.raises(error, match=match):
        service.rewrite(tmp_path, "demo", "SKILL.md", edit, author="agent")

    assert skill_file.read_bytes() == before


@pytest.mark.parametrize(
    ("relative", "operation", "marker"),
    [
        pytest.param(
            "SKILL.md",
            lambda service, root: service.edit(
                root, "demo", skill_document(description="Updated."), author="agent"
            ),
            "Updated.",
            id="edit",
        ),
        pytest.param(
            "SKILL.md",
            lambda service, root: service.rewrite(
                root, "demo", "SKILL.md", _replace("old line", "new line"), author="agent"
            ),
            "new line",
            id="rewrite-document",
        ),
        pytest.param(
            "references/notes.md",
            lambda service, root: service.rewrite(
                root,
                "demo",
                "references/notes.md",
                _replace("old line", "new line"),
                author="agent",
            ),
            "new line",
            id="rewrite-support-file",
        ),
        pytest.param(
            "references/notes.md",
            lambda service, root: service.write_file(
                root, "demo", "references/notes.md", "replacement\nnew line\n"
            ),
            "new line",
            id="write-support-file",
        ),
    ],
)
def test_changes_keep_the_crlf_style_of_an_existing_file(
    service: SkillAuthoringService,
    tmp_path: Path,
    relative: str,
    operation: Callable[[SkillAuthoringService, Path], object],
    marker: str,
) -> None:
    service.create(tmp_path, "demo", skill_document(body="# Demo\nold line\n"), author="agent")
    service.write_file(tmp_path, "demo", "references/notes.md", "notes\nold line\n")
    target = tmp_path / "demo" / relative
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))

    operation(service, tmp_path)

    written = target.read_bytes()
    assert marker.encode() in written
    assert b"\n" not in written.replace(b"\r\n", b"")
    text = service.read_text(tmp_path, "demo", relative)
    assert marker in text
    assert "\r" not in text


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param(
            lambda service, root: service.edit(
                root, "other", skill_document(name="other"), author="agent"
            ),
            "Skill 'other' not found",
            id="edit-missing-skill",
        ),
        pytest.param(
            lambda service, root: service.delete(root, "other"),
            "Skill 'other' not found",
            id="delete-missing-skill",
        ),
        pytest.param(
            lambda service, root: service.remove_file(root, "demo", "scripts/absent.py"),
            "Support file not found: scripts/absent.py",
            id="remove-missing-file",
        ),
        pytest.param(
            lambda service, root: service.rewrite(
                root, "demo", "references/none.md", str.upper, author="agent"
            ),
            "Skill file not found",
            id="rewrite-missing-file",
        ),
        pytest.param(
            lambda service, root: service.rewrite(
                root, "demo", "assets/logo.bin", str.upper, author="agent"
            ),
            "not UTF-8",
            id="rewrite-binary-file",
        ),
    ],
)
def test_changes_to_missing_or_binary_targets_fail(
    service: SkillAuthoringService,
    tmp_path: Path,
    operation: Callable[[SkillAuthoringService, Path], object],
    match: str,
) -> None:
    service.create(tmp_path, "demo", skill_document(), author="agent")
    (tmp_path / "demo" / "assets").mkdir()
    (tmp_path / "demo" / "assets" / "logo.bin").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(SkillAuthoringError, match=match):
        operation(service, tmp_path)


def test_delete_removes_the_skill_directory(service: SkillAuthoringService, tmp_path: Path) -> None:
    service.create(tmp_path, "demo", skill_document(), author="agent")

    service.delete(tmp_path, "demo")

    assert not (tmp_path / "demo").exists()


@pytest.mark.parametrize("action", ["delete", "write_file", "remove_file", "patch"])
@pytest.mark.parametrize("document_directory", [False, True])
def test_mutations_leave_non_package_directories_untouched(
    service: SkillAuthoringService, tmp_path: Path, action: str, document_directory: bool
) -> None:
    package = tmp_path / "demo"
    resource = package / "references" / "notes.md"
    resource.parent.mkdir(parents=True)
    resource.write_text("keep this file", encoding="utf-8")
    if document_directory:
        (package / "SKILL.md").mkdir()

    with pytest.raises(SkillAuthoringError):
        if action == "delete":
            service.delete(tmp_path, "demo")
        elif action == "write_file":
            service.write_file(tmp_path, "demo", "references/notes.md", "replacement")
        elif action == "remove_file":
            service.remove_file(tmp_path, "demo", "references/notes.md")
        else:
            service.rewrite(
                tmp_path, "demo", "references/notes.md", _replace("keep", "replace"), author="agent"
            )

    assert resource.read_text(encoding="utf-8") == "keep this file"


@pytest.mark.parametrize("directory", ["scripts", "references", "assets"])
def test_support_files_are_written_and_removed_under_resource_directories(
    service: SkillAuthoringService, tmp_path: Path, directory: str
) -> None:
    service.create(tmp_path, "demo", skill_document(), author="agent")
    resource = tmp_path / "demo" / directory / "file.txt"

    service.write_file(tmp_path, "demo", f"{directory}/file.txt", "content\n")
    assert resource.read_text(encoding="utf-8") == "content\n"

    service.remove_file(tmp_path, "demo", f"{directory}/file.txt")
    assert not resource.exists()


@pytest.mark.parametrize(
    ("content", "stored", "warnings"),
    [
        pytest.param(
            "---\ndescription: Has no name.\n---\n\nbody\n",
            "---\ndescription: Has no name.\nname: demo\n",
            ["Skill metadata missing name; using directory name 'demo'."],
            id="missing-name",
        ),
        pytest.param(
            "---\nname: demo\n---\n\nbody\n",
            "---\nname: demo\ndescription: body\n",
            ["Skill metadata missing description; using the first body text line."],
            id="missing-description",
        ),
        pytest.param(
            "# Demo\n\nRun it.\n",
            "---\nname: demo\ndescription: Run it.\n",
            [
                "SKILL.md has no complete YAML front matter; using the full file as instructions.",
                "Skill metadata missing name; using directory name 'demo'.",
                "Skill metadata missing description; using the first body text line.",
            ],
            id="missing-front-matter",
        ),
        pytest.param(
            "---\nname: demo\ndescription: [unclosed\n---\n\nbody\n",
            "---\nname: demo\ndescription: '[unclosed'\n",
            ["YAML front matter was read with the simple key: value fallback."],
            id="simple-key-value-fallback",
        ),
    ],
)
def test_lenient_metadata_is_stored_canonically(
    service: SkillAuthoringService,
    tmp_path: Path,
    content: str,
    stored: str,
    warnings: list[str],
) -> None:
    result = service.create(tmp_path, "demo", content, author="agent")

    body = content.split("---\n\n", 1)[-1]
    assert result.path.read_text(encoding="utf-8") == (
        f"{stored}metadata:\n  vbot:\n    author: agent\n---\n\n{body}"
    )
    assert result.warnings == warnings


@pytest.mark.parametrize(
    ("content", "author"),
    [
        pytest.param(skill_document(name="other"), "agent", id="name-differs-from-directory"),
        pytest.param(skill_document(), "robot", id="unknown-author"),
        pytest.param(
            "---\nname: demo\ndescription: Bad requirements.\n"
            "metadata:\n  vbot:\n    requirements:\n      bogus: true\n---\n\nbody\n",
            "agent",
            id="malformed-requirements",
        ),
    ],
)
def test_invalid_documents_are_rejected(
    service: SkillAuthoringService, tmp_path: Path, content: str, author: Any
) -> None:
    with pytest.raises(SkillAuthoringError):
        service.create(tmp_path, "demo", content, author=author)

    assert not (tmp_path / "demo").exists()


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "..", ".", "a\\b"])
def test_rejects_illegal_skill_names(
    service: SkillAuthoringService, tmp_path: Path, bad_name: str
) -> None:
    with pytest.raises(SkillAuthoringError):
        service.create(tmp_path, bad_name, skill_document(name=bad_name), author="agent")


@pytest.mark.parametrize(
    "bad_path",
    [
        "scripts/../../escape.py",
        "../outside.py",
        "/abs/path.py",
        "scripts/../SKILL.md",
        "SKILL.md",
        "other/data.txt",
    ],
)
def test_support_files_stay_inside_resource_directories(
    service: SkillAuthoringService, tmp_path: Path, bad_path: str
) -> None:
    service.create(tmp_path, "demo", skill_document(), author="agent")
    before = (tmp_path / "demo" / "SKILL.md").read_bytes()

    with pytest.raises(SkillAuthoringError):
        service.write_file(tmp_path, "demo", bad_path, "x")

    assert (tmp_path / "demo" / "SKILL.md").read_bytes() == before
    assert sorted(path.name for path in (tmp_path / "demo").iterdir()) == ["SKILL.md"]


def test_protected_roots_refuse_targets_at_or_under_them(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    bundled = resources / "skills"
    bundled.mkdir(parents=True)

    for protected in (bundled, resources):
        with pytest.raises(SkillAuthoringError):
            SkillAuthoringService(protected_roots=[protected]).create(
                bundled, "demo", skill_document(), author="agent"
            )

    agent_home = tmp_path / "agents" / "main" / "skills"
    SkillAuthoringService(protected_roots=[bundled]).create(
        agent_home, "demo", skill_document(), author="agent"
    )
    assert (agent_home / "demo" / "SKILL.md").is_file()
    assert not (bundled / "demo").exists()


@pytest.mark.parametrize("action", ["patch", "write_file", "remove_file"])
@pytest.mark.parametrize("directory_alias", [False, True])
def test_support_alias_cannot_bypass_document_validation(
    service: SkillAuthoringService, tmp_path: Path, action: str, directory_alias: bool
) -> None:
    service.create(tmp_path, "demo", skill_document(), author="human")
    document = tmp_path / "demo" / "SKILL.md"
    before = document.read_bytes()
    alias = document.parent / "references" / "alias.md"
    try:
        if directory_alias:
            alias = document.parent / "references" / "SKILL.md"
            alias.parent.symlink_to(document.parent, target_is_directory=True)
        else:
            alias.parent.mkdir()
            alias.symlink_to(document)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")
    relative = alias.relative_to(document.parent).as_posix()

    with pytest.raises(SkillAuthoringError):
        if action == "patch":
            service.rewrite(
                tmp_path, "demo", relative, _replace("name: demo", "name: other"), author="agent"
            )
        elif action == "write_file":
            service.write_file(tmp_path, "demo", relative, "invalid document")
        else:
            service.remove_file(tmp_path, "demo", relative)

    assert document.read_bytes() == before


def test_edit_rejects_linked_document_before_reading(
    service: SkillAuthoringService, tmp_path: Path
) -> None:
    source = tmp_path / "original.md"
    source.write_text("unrelated private data", encoding="utf-8")
    package = tmp_path / "demo"
    package.mkdir()
    document = package / "SKILL.md"
    try:
        document.symlink_to(source)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")

    with pytest.raises(SkillAuthoringError):
        service.edit(tmp_path, "demo", skill_document(), author="human")

    assert document.is_symlink()
    assert source.read_text(encoding="utf-8") == "unrelated private data"


def test_delete_does_not_follow_alias_to_another_package(
    service: SkillAuthoringService, tmp_path: Path
) -> None:
    service.create(tmp_path, "other", skill_document(name="other"), author="human")
    document = tmp_path / "other" / "SKILL.md"
    before = document.read_bytes()
    try:
        (tmp_path / "demo").symlink_to(document.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")

    with pytest.raises(SkillAuthoringError):
        service.delete(tmp_path, "demo")

    assert document.read_bytes() == before
