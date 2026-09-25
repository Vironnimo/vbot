"""Tests for the validated skill authoring write core."""

from __future__ import annotations

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
    return f"""---
name: {name}
description: {description}
---

{body}"""


def read_front_matter(skill_file: Path) -> Any:
    text = skill_file.read_text(encoding="utf-8")
    _, front, _ = text.split("---", 2)
    return yaml.safe_load(front)


def read_raw(path: Path) -> str:
    """Read text without newline translation (style-preserving read)."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


@pytest.fixture
def service() -> SkillAuthoringService:
    return SkillAuthoringService()


class TestCreate:
    def test_creates_skill_file_with_body(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        result = service.create(
            tmp_path, "demo", skill_document(body="# Demo\nSteps."), author="agent"
        )

        skill_file = tmp_path / "demo" / "SKILL.md"
        assert skill_file.is_file()
        assert result.name == "demo"
        assert result.operation == "create"
        assert result.path == skill_file
        assert "# Demo\nSteps." in skill_file.read_text(encoding="utf-8")

    def test_created_skill_loads_through_registry(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        registry = SkillRegistry.load(tmp_path)
        skill = registry.get("demo")
        assert skill.description == "Do a demo task."

    def test_rejects_duplicate(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError):
            service.create(tmp_path, "demo", skill_document(), author="agent")


class TestProvenance:
    def test_records_author_and_source(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(
            tmp_path, "demo", skill_document(), author="human", source="https://example.com/howto"
        )

        front = read_front_matter(tmp_path / "demo" / "SKILL.md")
        vbot = front["metadata"][REQUIREMENTS_METADATA_KEY]
        assert vbot[PROVENANCE_AUTHOR_KEY] == "human"
        assert vbot[PROVENANCE_SOURCE_KEY] == "https://example.com/howto"

    def test_provenance_coexists_with_requirements(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        content = """---
name: demo
description: Do a demo task.
metadata:
  vbot:
    requirements:
      all:
        - binary: git
---

# Demo
"""
        service.create(tmp_path, "demo", content, author="agent", source="folder:/tmp/x")

        registry = SkillRegistry.load(tmp_path)
        skill = registry.get("demo")
        # Requirements survive the provenance stamp and still parse.
        assert skill.requirements.required is not None
        front = read_front_matter(tmp_path / "demo" / "SKILL.md")
        vbot = front["metadata"][REQUIREMENTS_METADATA_KEY]
        assert vbot[PROVENANCE_AUTHOR_KEY] == "agent"
        assert "requirements" in vbot

    def test_provenance_never_leaks_into_catalog_fields(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent", source="x")

        registry = SkillRegistry.load(tmp_path)
        skill = registry.get("demo")
        # Catalog-facing fields are name/description only; provenance lives in metadata.
        assert skill.name == "demo"
        assert PROVENANCE_AUTHOR_KEY not in skill.name
        assert PROVENANCE_AUTHOR_KEY not in skill.description


class TestEdit:
    def test_rewrites_existing_skill(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        service.edit(
            tmp_path, "demo", skill_document(description="Updated.", body="# New\n"), author="human"
        )

        registry = SkillRegistry.load(tmp_path)
        assert registry.get("demo").description == "Updated."
        assert "# New" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_edit_missing_skill_fails(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        with pytest.raises(SkillAuthoringError):
            service.edit(tmp_path, "demo", skill_document(), author="agent")

    def test_edit_preserves_crlf_style(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")
        skill_file = tmp_path / "demo" / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8").replace("\n", "\r\n"),
            encoding="utf-8",
            newline="",
        )

        service.edit(tmp_path, "demo", skill_document(description="Updated."), author="agent")

        text = read_raw(skill_file)
        assert "Updated." in text
        assert "\r\n" in text
        assert "\n" not in text.replace("\r\n", "")


def _replace(old: str, new: str):
    def edit(text: str) -> str:
        assert "\r" not in text
        return text.replace(old, new)

    return edit


class TestRewrite:
    def test_rewrites_skill_md_through_the_edit(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(body="# Demo\nold line"), author="agent")

        result = service.rewrite(
            tmp_path, "demo", "SKILL.md", _replace("old line", "new line"), author="agent"
        )

        assert result.operation == "rewrite"
        assert "new line" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_create_writes_lf(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        assert b"\r" not in (tmp_path / "demo" / "SKILL.md").read_bytes()

    def test_failed_edit_writes_nothing(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")
        skill_file = tmp_path / "demo" / "SKILL.md"
        before = skill_file.read_bytes()

        def refuse(_text: str) -> str:
            raise LookupError("no match")

        with pytest.raises(LookupError):
            service.rewrite(tmp_path, "demo", "SKILL.md", refuse, author="agent")

        assert skill_file.read_bytes() == before

    def test_rewritten_document_is_validated(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")
        skill_file = tmp_path / "demo" / "SKILL.md"
        before = skill_file.read_bytes()

        with pytest.raises(SkillAuthoringError, match="must match its directory name"):
            service.rewrite(
                tmp_path, "demo", "SKILL.md", _replace("name: demo", "name: other"), author="agent"
            )

        assert skill_file.read_bytes() == before

    def test_rewrite_on_crlf_file_preserves_style(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(body="# Demo\nold line\n"), author="agent")
        skill_file = tmp_path / "demo" / "SKILL.md"
        skill_file.write_bytes(skill_file.read_bytes().replace(b"\n", b"\r\n"))

        service.rewrite(
            tmp_path, "demo", "SKILL.md", _replace("old line", "new line"), author="agent"
        )

        text = read_raw(skill_file)
        assert "new line" in text
        assert "\n" not in text.replace("\r\n", "")

    def test_rewrite_crlf_support_file(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")
        resource = tmp_path / "demo" / "references" / "notes.md"
        service.write_file(tmp_path, "demo", "references/notes.md", "notes\n")
        resource.write_bytes(b"notes\r\nmore\r\n")

        service.rewrite(
            tmp_path,
            "demo",
            "references/notes.md",
            _replace("notes", "ideas"),
            author="agent",
        )

        assert resource.read_bytes() == b"ideas\r\nmore\r\n"
        assert service.read_text(tmp_path, "demo", "references/notes.md") == "ideas\nmore\n"

    def test_rewrite_missing_or_binary_file_fails(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")
        (tmp_path / "demo" / "assets").mkdir()
        (tmp_path / "demo" / "assets" / "logo.bin").write_bytes(b"\xff\xfe\x00")

        with pytest.raises(SkillAuthoringError, match="Skill file not found"):
            service.rewrite(tmp_path, "demo", "references/none.md", str.upper, author="agent")
        with pytest.raises(SkillAuthoringError, match="not UTF-8"):
            service.rewrite(tmp_path, "demo", "assets/logo.bin", str.upper, author="agent")


class TestDelete:
    def test_removes_skill_directory(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        service.delete(tmp_path, "demo")

        assert not (tmp_path / "demo").exists()

    def test_delete_missing_skill_fails(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        with pytest.raises(SkillAuthoringError):
            service.delete(tmp_path, "demo")


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


class TestSupportFiles:
    def test_write_and_remove_under_scripts(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        service.write_file(tmp_path, "demo", "scripts/run.py", "print('hi')\n")
        resource = tmp_path / "demo" / "scripts" / "run.py"
        assert resource.read_text(encoding="utf-8") == "print('hi')\n"

        service.remove_file(tmp_path, "demo", "scripts/run.py")
        assert not resource.exists()

    def test_write_under_references(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        service.write_file(tmp_path, "demo", "references/notes.md", "notes\n")
        assert (tmp_path / "demo" / "references" / "notes.md").is_file()

    def test_write_under_assets(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        service.write_file(tmp_path, "demo", "assets/template.html", "<html></html>\n")
        assert (tmp_path / "demo" / "assets" / "template.html").is_file()

    def test_rejects_file_outside_resource_dirs(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError):
            service.write_file(tmp_path, "demo", "SKILL.md", "x")

    def test_remove_missing_file_fails(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError):
            service.remove_file(tmp_path, "demo", "scripts/absent.py")

    def test_write_file_preserves_existing_crlf_style(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")
        service.write_file(tmp_path, "demo", "references/notes.md", "notes\n")
        resource = tmp_path / "demo" / "references" / "notes.md"
        resource.write_text(
            resource.read_text(encoding="utf-8").replace("\n", "\r\n"),
            encoding="utf-8",
            newline="",
        )

        service.write_file(tmp_path, "demo", "references/notes.md", "replacement\n")

        assert read_raw(resource) == "replacement\r\n"


class TestLenientMetadataAuthoring:
    def test_missing_name_uses_directory_name(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        content = "---\ndescription: Has no name.\n---\n\nbody\n"
        result = service.create(tmp_path, "demo", content, author="agent")

        assert read_front_matter(result.path)["name"] == "demo"
        assert result.warnings == ["Skill metadata missing name; using directory name 'demo'."]

    def test_missing_description_uses_body(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        content = "---\nname: demo\n---\n\nbody\n"
        result = service.create(tmp_path, "demo", content, author="agent")

        assert read_front_matter(result.path)["description"] == "body"
        assert result.warnings == [
            "Skill metadata missing description; using the first body text line."
        ]

    def test_missing_front_matter_is_canonicalized(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        result = service.create(tmp_path, "demo", "# Demo\n\nRun it.\n", author="agent")

        fields = read_front_matter(result.path)
        assert fields["name"] == "demo"
        assert fields["description"] == "Run it."
        assert result.path.read_text(encoding="utf-8").endswith("# Demo\n\nRun it.\n")

    def test_malformed_requirements(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        content = """---
name: demo
description: Bad requirements.
metadata:
  vbot:
    requirements:
      bogus: true
---

body
"""
        with pytest.raises(SkillAuthoringError):
            service.create(tmp_path, "demo", content, author="agent")

    def test_invalid_yaml_uses_simple_key_value_fallback(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        content = "---\nname: demo\ndescription: [unclosed\n---\n\nbody\n"
        result = service.create(tmp_path, "demo", content, author="agent")

        assert read_front_matter(result.path)["description"] == "[unclosed"
        assert result.warnings == [
            "YAML front matter was read with the simple key: value fallback."
        ]


class TestValidationRejection:
    def test_name_must_match_directory(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        with pytest.raises(SkillAuthoringError):
            service.create(tmp_path, "demo", skill_document(name="other"), author="agent")

    def test_unknown_author(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        with pytest.raises(SkillAuthoringError):
            service.create(tmp_path, "demo", skill_document(), author="robot")  # type: ignore[arg-type]


class TestPathTraversalRejection:
    @pytest.mark.parametrize("bad_name", ["../escape", "a/b", "..", ".", "a\\b"])
    def test_rejects_illegal_skill_names(
        self, service: SkillAuthoringService, tmp_path: Path, bad_name: str
    ) -> None:
        with pytest.raises(SkillAuthoringError):
            service.create(tmp_path, bad_name, skill_document(name=bad_name), author="agent")

    @pytest.mark.parametrize(
        "bad_path",
        ["scripts/../../escape.py", "../outside.py", "/abs/path.py", "scripts/../SKILL.md"],
    )
    def test_rejects_illegal_support_paths(
        self, service: SkillAuthoringService, tmp_path: Path, bad_path: str
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError):
            service.write_file(tmp_path, "demo", bad_path, "x")


class TestProtectedRootRefusal:
    def test_refuses_target_at_protected_root(self, tmp_path: Path) -> None:
        bundled = tmp_path / "resources" / "skills"
        bundled.mkdir(parents=True)
        service = SkillAuthoringService(protected_roots=[bundled])

        with pytest.raises(SkillAuthoringError):
            service.create(bundled, "demo", skill_document(), author="agent")

    def test_refuses_target_under_protected_root(self, tmp_path: Path) -> None:
        resources = tmp_path / "resources"
        bundled = resources / "skills"
        bundled.mkdir(parents=True)
        service = SkillAuthoringService(protected_roots=[resources])

        with pytest.raises(SkillAuthoringError):
            service.create(bundled, "demo", skill_document(), author="agent")

    def test_allows_unprotected_target(self, tmp_path: Path) -> None:
        bundled = tmp_path / "resources" / "skills"
        bundled.mkdir(parents=True)
        service = SkillAuthoringService(protected_roots=[bundled])
        agent_home = tmp_path / "agents" / "main" / "skills"

        service.create(agent_home, "demo", skill_document(), author="agent")
        assert (agent_home / "demo" / "SKILL.md").is_file()


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

    with pytest.raises(SkillAuthoringError):
        if action == "patch":
            service.rewrite(
                tmp_path,
                "demo",
                alias.relative_to(document.parent).as_posix(),
                _replace("name: demo", "name: other"),
                author="agent",
            )
        elif action == "write_file":
            service.write_file(
                tmp_path, "demo", alias.relative_to(document.parent).as_posix(), "invalid document"
            )
        else:
            service.remove_file(tmp_path, "demo", alias.relative_to(document.parent).as_posix())

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
