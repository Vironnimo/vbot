"""Tests for the validated skill authoring write core."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.skills.authoring import (
    HUMAN_WRITER,
    PROVENANCE_AUTHOR_KEY,
    PROVENANCE_SOURCE_KEY,
    SkillAuthoringError,
    SkillAuthoringService,
    SkillWriter,
)
from core.skills.requirements import REQUIREMENTS_METADATA_KEY
from core.skills.skills import SkillRegistry
from tests.directory_links import link_directory


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


AGENT = SkillWriter(actor="agent")


@pytest.fixture
def service() -> SkillAuthoringService:
    return SkillAuthoringService()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A writable Skill home; its history and archive live beside it in ``tmp_path``."""
    return tmp_path / "skills"


def test_create_writes_an_lf_document_the_registry_loads(
    service: SkillAuthoringService, root: Path
) -> None:
    result = service.create(root, "demo", skill_document(body="# Demo\nSteps."), writer=AGENT)

    skill_file = root / "demo" / "SKILL.md"
    assert (result.name, result.operation, result.path) == ("demo", "create", skill_file)
    assert "# Demo\nSteps." in skill_file.read_text(encoding="utf-8")
    assert b"\r" not in skill_file.read_bytes()
    assert SkillRegistry.load(root).get("demo").description == "Do a demo task."
    with pytest.raises(SkillAuthoringError):
        service.create(root, "demo", skill_document(), writer=AGENT)


def test_provenance_is_stamped_into_vbot_metadata_beside_requirements(
    service: SkillAuthoringService, root: Path
) -> None:
    content = (
        "---\nname: demo\ndescription: Do a demo task.\n"
        "metadata:\n  vbot:\n    requirements:\n      all:\n        - binary: git\n---\n\n# Demo\n"
    )

    service.create(root, "demo", content, writer=HUMAN_WRITER, source="https://example.com/howto")

    vbot = read_front_matter(root / "demo" / "SKILL.md")["metadata"][REQUIREMENTS_METADATA_KEY]
    assert vbot[PROVENANCE_AUTHOR_KEY] == "human"
    assert vbot[PROVENANCE_SOURCE_KEY] == "https://example.com/howto"
    # Requirements survive the stamp; catalog fields stay free of provenance.
    skill = SkillRegistry.load(root).get("demo")
    assert skill.requirements.required is not None
    assert (skill.name, skill.description) == ("demo", "Do a demo task.")


@pytest.mark.parametrize("utf8", [True, False], ids=["text-document", "non-utf8-document"])
def test_edit_replaces_the_document(service: SkillAuthoringService, root: Path, utf8: bool) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    skill_file = root / "demo" / "SKILL.md"
    if not utf8:
        skill_file.write_bytes(b"\xff\xfe---\r\nname: demo\r\n")
    before = skill_file.read_text(encoding="utf-8") if utf8 else None

    result = service.edit(
        root, "demo", skill_document(description="Updated.", body="# New\n"), writer=HUMAN_WRITER
    )

    assert SkillRegistry.load(root).get("demo").description == "Updated."
    assert "# New" in skill_file.read_text(encoding="utf-8")
    [change] = result.changes
    assert (change.change, change.before) == ("updated", before)


def test_rewrite_applies_an_edit_of_the_lf_text(service: SkillAuthoringService, root: Path) -> None:
    service.create(root, "demo", skill_document(body="# Demo\nold line"), writer=AGENT)

    result = service.rewrite(
        root, "demo", "SKILL.md", _replace("old line", "new line"), writer=AGENT
    )

    assert result.operation == "rewrite"
    assert "new line" in (root / "demo" / "SKILL.md").read_text(encoding="utf-8")


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
    root: Path,
    edit: Callable[[str], str],
    error: type[Exception],
    match: str,
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    skill_file = root / "demo" / "SKILL.md"
    before = skill_file.read_bytes()

    with pytest.raises(error, match=match):
        service.rewrite(root, "demo", "SKILL.md", edit, writer=AGENT)

    assert skill_file.read_bytes() == before


@pytest.mark.parametrize(
    ("relative", "operation", "marker"),
    [
        pytest.param(
            "SKILL.md",
            lambda service, root: service.edit(
                root, "demo", skill_document(description="Updated."), writer=AGENT
            ),
            "Updated.",
            id="edit",
        ),
        pytest.param(
            "SKILL.md",
            lambda service, root: service.rewrite(
                root, "demo", "SKILL.md", _replace("old line", "new line"), writer=AGENT
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
                writer=AGENT,
            ),
            "new line",
            id="rewrite-support-file",
        ),
        pytest.param(
            "references/notes.md",
            lambda service, root: service.write_file(
                root, "demo", "references/notes.md", "replacement\nnew line\n", writer=AGENT
            ),
            "new line",
            id="write-support-file",
        ),
    ],
)
def test_changes_keep_the_crlf_style_of_an_existing_file(
    service: SkillAuthoringService,
    root: Path,
    relative: str,
    operation: Callable[[SkillAuthoringService, Path], object],
    marker: str,
) -> None:
    service.create(root, "demo", skill_document(body="# Demo\nold line\n"), writer=AGENT)
    service.write_file(root, "demo", "references/notes.md", "notes\nold line\n", writer=AGENT)
    target = root / "demo" / relative
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))

    operation(service, root)

    written = target.read_bytes()
    assert marker.encode() in written
    assert b"\n" not in written.replace(b"\r\n", b"")
    text = service.read_text(root, "demo", relative)
    assert marker in text
    assert "\r" not in text


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param(
            lambda service, root: service.edit(
                root, "other", skill_document(name="other"), writer=AGENT
            ),
            "Skill 'other' not found",
            id="edit-missing-skill",
        ),
        pytest.param(
            lambda service, root: service.delete(root, "other", writer=HUMAN_WRITER),
            "Skill 'other' not found",
            id="delete-missing-skill",
        ),
        pytest.param(
            lambda service, root: service.remove_file(
                root, "demo", "scripts/absent.py", writer=AGENT
            ),
            "Support file not found: scripts/absent.py",
            id="remove-missing-file",
        ),
        pytest.param(
            lambda service, root: service.rewrite(
                root, "demo", "references/none.md", str.upper, writer=AGENT
            ),
            "Skill file not found",
            id="rewrite-missing-file",
        ),
        pytest.param(
            lambda service, root: service.rewrite(
                root, "demo", "assets/logo.bin", str.upper, writer=AGENT
            ),
            "not UTF-8",
            id="rewrite-binary-file",
        ),
    ],
)
def test_changes_to_missing_or_binary_targets_fail(
    service: SkillAuthoringService,
    root: Path,
    operation: Callable[[SkillAuthoringService, Path], object],
    match: str,
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    (root / "demo" / "assets").mkdir()
    (root / "demo" / "assets" / "logo.bin").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(SkillAuthoringError, match=match):
        operation(service, root)


def test_delete_archives_the_skill_directory(
    service: SkillAuthoringService, root: Path, tmp_path: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    service.write_file(root, "demo", "references/notes.md", "notes\n", writer=AGENT)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("private", encoding="utf-8")
    link_directory(root / "demo" / "assets", outside)

    result = service.delete(root, "demo", writer=HUMAN_WRITER)

    # A linked folder moves as a link: what it points at is neither read nor moved.
    assert [(change.path, change.change) for change in result.changes] == [
        ("SKILL.md", "deleted"),
        ("references/notes.md", "deleted"),
    ]
    assert not (root / "demo").exists()
    archived = tmp_path / "skill-archive" / str(result.archive_id)
    assert result.path == archived
    assert (archived / "references" / "notes.md").read_text(encoding="utf-8") == "notes\n"
    assert (outside / "private.md").read_text(encoding="utf-8") == "private"
    [entry] = service.archived(root)
    assert (entry.archive_id, entry.name, entry.reason, entry.archived_by, entry.origin) == (
        result.archive_id,
        "demo",
        "deleted",
        "human",
        "agent",
    )


@pytest.mark.parametrize("action", ["delete", "write_file", "remove_file", "patch"])
@pytest.mark.parametrize("document_directory", [False, True])
def test_mutations_leave_non_package_directories_untouched(
    service: SkillAuthoringService, root: Path, action: str, document_directory: bool
) -> None:
    package = root / "demo"
    resource = package / "references" / "notes.md"
    resource.parent.mkdir(parents=True)
    resource.write_text("keep this file", encoding="utf-8")
    if document_directory:
        (package / "SKILL.md").mkdir()

    with pytest.raises(SkillAuthoringError):
        if action == "delete":
            service.delete(root, "demo", writer=HUMAN_WRITER)
        elif action == "write_file":
            service.write_file(root, "demo", "references/notes.md", "replacement", writer=AGENT)
        elif action == "remove_file":
            service.remove_file(root, "demo", "references/notes.md", writer=AGENT)
        else:
            service.rewrite(
                root, "demo", "references/notes.md", _replace("keep", "replace"), writer=AGENT
            )

    assert resource.read_text(encoding="utf-8") == "keep this file"


@pytest.mark.parametrize("directory", ["scripts", "references", "assets"])
def test_support_files_are_written_and_removed_under_resource_directories(
    service: SkillAuthoringService, root: Path, directory: str
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    resource = root / "demo" / directory / "file.txt"

    service.write_file(root, "demo", f"{directory}/file.txt", "content\n", writer=AGENT)
    assert resource.read_text(encoding="utf-8") == "content\n"

    service.remove_file(root, "demo", f"{directory}/file.txt", writer=AGENT)
    assert not resource.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows maps the name to a device")
def test_an_existing_support_file_under_a_windows_reserved_name_stays_writable(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    resource = root / "demo" / "scripts" / "aux.py"
    resource.parent.mkdir()
    resource.write_text("old\n", encoding="utf-8")

    service.write_file(root, "demo", "scripts/aux.py", "new\n", writer=AGENT)

    assert resource.read_text(encoding="utf-8") == "new\n"


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
    root: Path,
    content: str,
    stored: str,
    warnings: list[str],
) -> None:
    result = service.create(root, "demo", content, writer=AGENT)

    body = content.split("---\n\n", 1)[-1]
    assert result.path.read_text(encoding="utf-8") == (
        f"{stored}metadata:\n  vbot:\n    author: agent\n---\n\n{body}"
    )
    assert result.warnings == warnings


@pytest.mark.parametrize(
    ("content", "actor"),
    [
        pytest.param(skill_document(name="other"), "agent", id="name-differs-from-directory"),
        pytest.param(skill_document(), "robot", id="unknown-writer"),
        pytest.param(
            "---\nname: demo\ndescription: Bad requirements.\n"
            "metadata:\n  vbot:\n    requirements:\n      bogus: true\n---\n\nbody\n",
            "agent",
            id="malformed-requirements",
        ),
    ],
)
def test_invalid_documents_are_rejected(
    service: SkillAuthoringService, root: Path, content: str, actor: Any
) -> None:
    with pytest.raises(SkillAuthoringError):
        service.create(root, "demo", content, writer=SkillWriter(actor=actor))

    assert not (root / "demo").exists()


# Names Windows reserves for devices are refused on every platform.
@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "..", ".", "a\\b", "con", "NUL", "com0"])
def test_rejects_illegal_skill_names(
    service: SkillAuthoringService, root: Path, bad_name: str
) -> None:
    with pytest.raises(SkillAuthoringError):
        service.create(root, bad_name, skill_document(name=bad_name), writer=AGENT)

    assert not root.exists()


@pytest.mark.parametrize(
    "bad_path",
    [
        "scripts/../../escape.py",
        "../outside.py",
        "/abs/path.py",
        "scripts/../SKILL.md",
        "SKILL.md",
        "other/data.txt",
        # New names Windows cannot store are refused on every platform.
        "scripts/nul.py",
        "assets/con/x.txt",
        "scripts/a:b.py",
        "references/notes.",
    ],
)
def test_support_files_stay_inside_resource_directories(
    service: SkillAuthoringService, root: Path, bad_path: str
) -> None:
    service.create(root, "demo", skill_document(), writer=AGENT)
    before = (root / "demo" / "SKILL.md").read_bytes()

    with pytest.raises(SkillAuthoringError):
        service.write_file(root, "demo", bad_path, "x", writer=AGENT)

    assert (root / "demo" / "SKILL.md").read_bytes() == before
    assert sorted(path.name for path in (root / "demo").iterdir()) == ["SKILL.md"]


def test_protected_roots_refuse_targets_at_or_under_them(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    bundled = resources / "skills"
    bundled.mkdir(parents=True)

    for protected in (bundled, resources):
        with pytest.raises(SkillAuthoringError):
            SkillAuthoringService(protected_roots=[protected]).create(
                bundled, "demo", skill_document(), writer=AGENT
            )

    agent_home = tmp_path / "agents" / "main" / "skills"
    SkillAuthoringService(protected_roots=[bundled]).create(
        agent_home, "demo", skill_document(), writer=AGENT
    )
    assert (agent_home / "demo" / "SKILL.md").is_file()
    assert not (bundled / "demo").exists()


@pytest.mark.parametrize("action", ["patch", "write_file", "remove_file"])
@pytest.mark.parametrize("directory_alias", [False, True])
def test_support_alias_cannot_bypass_document_validation(
    service: SkillAuthoringService, root: Path, action: str, directory_alias: bool
) -> None:
    service.create(root, "demo", skill_document(), writer=HUMAN_WRITER)
    document = root / "demo" / "SKILL.md"
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
                root, "demo", relative, _replace("name: demo", "name: other"), writer=AGENT
            )
        elif action == "write_file":
            service.write_file(root, "demo", relative, "invalid document", writer=AGENT)
        else:
            service.remove_file(root, "demo", relative, writer=AGENT)

    assert document.read_bytes() == before


def test_edit_rejects_linked_document_before_reading(
    service: SkillAuthoringService, root: Path, tmp_path: Path
) -> None:
    source = tmp_path / "original.md"
    source.write_text("unrelated private data", encoding="utf-8")
    package = root / "demo"
    package.mkdir(parents=True)
    document = package / "SKILL.md"
    try:
        document.symlink_to(source)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")

    with pytest.raises(SkillAuthoringError):
        service.edit(root, "demo", skill_document(), writer=HUMAN_WRITER)

    assert document.is_symlink()
    assert source.read_text(encoding="utf-8") == "unrelated private data"


def test_delete_does_not_follow_alias_to_another_package(
    service: SkillAuthoringService, root: Path
) -> None:
    service.create(root, "other", skill_document(name="other"), writer=HUMAN_WRITER)
    document = root / "other" / "SKILL.md"
    before = document.read_bytes()
    try:
        (root / "demo").symlink_to(document.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")

    with pytest.raises(SkillAuthoringError):
        service.delete(root, "demo", writer=HUMAN_WRITER)

    assert document.read_bytes() == before
