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


class TestPatch:
    def test_applies_unique_replacement(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(body="# Demo\nold line"), author="agent")

        service.patch(tmp_path, "demo", "old line", "new line", author="agent")

        assert "new line" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_patch_not_found(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError, match="file_path"):
            service.patch(tmp_path, "demo", "absent", "x", author="agent")

    def test_patch_not_unique(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(body="dup\ndup"), author="agent")

        with pytest.raises(SkillAuthoringError, match=r"line\(s\) 9, 10"):
            service.patch(tmp_path, "demo", "dup", "x", author="agent")

    def test_patch_identical_strings(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError):
            service.patch(tmp_path, "demo", "same", "same", author="agent")

    def test_patch_tolerates_crlf_match_on_lf_file(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(body="# Demo\nold line\n"), author="agent")

        service.patch(tmp_path, "demo", "old line\r\n", "new line\r\n", author="agent")

        assert "new line" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_patch_on_crlf_file_preserves_style(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(body="# Demo\nold line\n"), author="agent")
        skill_file = tmp_path / "demo" / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8").replace("\n", "\r\n"),
            encoding="utf-8",
            newline="",
        )

        service.patch(tmp_path, "demo", "old line", "new line", author="agent")

        text = read_raw(skill_file)
        assert "new line" in text
        assert "\r\n" in text
        assert "\n" not in text.replace("\r\n", "")

    def test_patch_crlf_support_file_with_lf_match(
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

        service.patch(
            tmp_path,
            "demo",
            "notes",
            "ideas",
            author="agent",
            relative_path="references/notes.md",
        )

        text = read_raw(resource)
        assert "ideas" in text
        assert "\r\n" in text
        assert "\n" not in text.replace("\r\n", "")


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
