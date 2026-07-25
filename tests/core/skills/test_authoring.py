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


@pytest.fixture
def service() -> SkillAuthoringService:
    return SkillAuthoringService()


@pytest.fixture
def package_service(tmp_path: Path) -> SkillAuthoringService:
    return SkillAuthoringService(
        drafts_root=tmp_path / "temp" / "skill-drafts",
        archive_root=tmp_path / "archive",
    )


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

        with pytest.raises(SkillAuthoringError, match="already exists"):
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
        with pytest.raises(SkillAuthoringError, match="not found"):
            service.edit(tmp_path, "demo", skill_document(), author="agent")


class TestPatch:
    def test_applies_unique_replacement(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(body="# Demo\nold line"), author="agent")

        service.patch(tmp_path, "demo", "old line", "new line", author="agent")

        assert "new line" in (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_patch_not_found(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError, match="not found"):
            service.patch(tmp_path, "demo", "absent", "x", author="agent")

    def test_patch_not_unique(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(body="dup\ndup"), author="agent")

        with pytest.raises(SkillAuthoringError, match="not unique"):
            service.patch(tmp_path, "demo", "dup", "x", author="agent")

    def test_patch_identical_strings(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError, match="must differ"):
            service.patch(tmp_path, "demo", "same", "same", author="agent")


class TestDelete:
    def test_removes_skill_directory(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        service.delete(tmp_path, "demo")

        assert not (tmp_path / "demo").exists()

    def test_delete_missing_skill_fails(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        with pytest.raises(SkillAuthoringError, match="not found"):
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

        with pytest.raises(SkillAuthoringError, match="must live under"):
            service.write_file(tmp_path, "demo", "SKILL.md", "x")

    def test_remove_missing_file_fails(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        service.create(tmp_path, "demo", skill_document(), author="agent")

        with pytest.raises(SkillAuthoringError, match="not found"):
            service.remove_file(tmp_path, "demo", "scripts/absent.py")


class TestValidationRejection:
    def test_missing_name(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        content = "---\ndescription: Has no name.\n---\n\nbody\n"
        with pytest.raises(SkillAuthoringError) as exc:
            service.create(tmp_path, "demo", content, author="agent")
        assert any("name" in diagnostic for diagnostic in exc.value.diagnostics)
        assert not (tmp_path / "demo").exists()

    def test_missing_description(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        content = "---\nname: demo\n---\n\nbody\n"
        with pytest.raises(SkillAuthoringError) as exc:
            service.create(tmp_path, "demo", content, author="agent")
        assert any("description" in diagnostic for diagnostic in exc.value.diagnostics)

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
        with pytest.raises(SkillAuthoringError, match="unknown key"):
            service.create(tmp_path, "demo", content, author="agent")

    def test_missing_front_matter(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        with pytest.raises(SkillAuthoringError, match="front matter"):
            service.create(tmp_path, "demo", "# Just a body\n", author="agent")

    def test_invalid_yaml(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        content = "---\nname: [unclosed\n---\n\nbody\n"
        with pytest.raises(SkillAuthoringError, match="valid YAML"):
            service.create(tmp_path, "demo", content, author="agent")

    def test_name_must_match_directory(
        self, service: SkillAuthoringService, tmp_path: Path
    ) -> None:
        with pytest.raises(SkillAuthoringError, match="must match its directory"):
            service.create(tmp_path, "demo", skill_document(name="other"), author="agent")

    def test_unknown_author(self, service: SkillAuthoringService, tmp_path: Path) -> None:
        with pytest.raises(SkillAuthoringError, match="author"):
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

        with pytest.raises(SkillAuthoringError, match="protected"):
            service.create(bundled, "demo", skill_document(), author="agent")

    def test_refuses_target_under_protected_root(self, tmp_path: Path) -> None:
        resources = tmp_path / "resources"
        bundled = resources / "skills"
        bundled.mkdir(parents=True)
        service = SkillAuthoringService(protected_roots=[resources])

        with pytest.raises(SkillAuthoringError, match="protected"):
            service.create(bundled, "demo", skill_document(), author="agent")

    def test_allows_unprotected_target(self, tmp_path: Path) -> None:
        bundled = tmp_path / "resources" / "skills"
        bundled.mkdir(parents=True)
        service = SkillAuthoringService(protected_roots=[bundled])
        agent_home = tmp_path / "agents" / "main" / "skills"

        service.create(agent_home, "demo", skill_document(), author="agent")
        assert (agent_home / "demo" / "SKILL.md").is_file()


class TestPackageDraftLifecycle:
    def test_create_draft_is_isolated_until_commit(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="create",
            actor_id="main",
            author="agent",
        )

        package_service.put_draft_text(
            skills_root,
            draft.id,
            "SKILL.md",
            skill_document(),
            actor_id="main",
        )
        assert not (skills_root / "demo").exists()

        inspection = package_service.validate_draft(
            skills_root,
            draft.id,
            actor_id="main",
        )
        result = package_service.commit_draft(
            skills_root,
            draft.id,
            actor_id="main",
        )

        assert [item.path for item in inspection.files] == ["SKILL.md"]
        assert result.operation == "commit"
        assert SkillRegistry.load(skills_root).get("demo").description == "Do a demo task."
        assert not draft.path.exists()

    def test_update_draft_keeps_published_package_unchanged_until_commit(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        package_service.create(
            skills_root,
            "demo",
            skill_document(body="old body"),
            author="agent",
        )
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="update",
            actor_id="main",
            author="agent",
        )

        package_service.patch_draft_text(
            skills_root,
            draft.id,
            "SKILL.md",
            "old body",
            "new body",
            actor_id="main",
        )

        published = (skills_root / "demo" / "SKILL.md").read_text(encoding="utf-8")
        assert "old body" in published
        assert "new body" not in published

        package_service.commit_draft(skills_root, draft.id, actor_id="main")
        assert "new body" in (skills_root / "demo" / "SKILL.md").read_text(encoding="utf-8")

    def test_binary_asset_is_copied_byte_for_byte(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        source = tmp_path / "source.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="create",
            actor_id="main",
            author="agent",
        )
        package_service.put_draft_text(
            skills_root,
            draft.id,
            "SKILL.md",
            skill_document(),
            actor_id="main",
        )

        manifest_file = package_service.copy_draft_file(
            skills_root,
            draft.id,
            "assets/logo.png",
            source,
            actor_id="main",
        )
        inspection = package_service.validate_draft(
            skills_root,
            draft.id,
            actor_id="main",
        )
        package_service.commit_draft(skills_root, draft.id, actor_id="main")

        assert manifest_file.binary is True
        assert manifest_file.kind == "assets"
        assert [item.path for item in inspection.files] == ["SKILL.md", "assets/logo.png"]
        assert (skills_root / "demo" / "assets" / "logo.png").read_bytes() == source.read_bytes()

    def test_rejects_non_vbot_top_level_directory(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="create",
            actor_id="main",
            author="agent",
        )
        package_service.put_draft_text(
            skills_root,
            draft.id,
            "SKILL.md",
            skill_document(),
            actor_id="main",
        )
        foreign_metadata = draft.path / "agents" / "openai.yaml"
        foreign_metadata.parent.mkdir()
        foreign_metadata.write_text("interface: {}\n", encoding="utf-8")

        with pytest.raises(SkillAuthoringError) as exc:
            package_service.validate_draft(skills_root, draft.id, actor_id="main")

        assert any(
            "Unsupported top-level Skill package path: agents" in diagnostic
            for diagnostic in exc.value.diagnostics
        )
        assert not (skills_root / "demo").exists()

    def test_invalid_update_cannot_replace_published_skill(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        package_service.create(skills_root, "demo", skill_document(), author="agent")
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="update",
            actor_id="main",
            author="agent",
        )
        package_service.put_draft_text(
            skills_root,
            draft.id,
            "SKILL.md",
            "---\nname: demo\n---\n",
            actor_id="main",
        )

        with pytest.raises(SkillAuthoringError):
            package_service.commit_draft(skills_root, draft.id, actor_id="main")

        assert SkillRegistry.load(skills_root).get("demo").description == "Do a demo task."
        assert draft.path.exists()

    def test_draft_is_bound_to_actor_and_scope(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="create",
            actor_id="main",
            author="agent",
        )

        with pytest.raises(SkillAuthoringError, match="different agent"):
            package_service.inspect_draft(skills_root, draft.id, actor_id="other")
        with pytest.raises(SkillAuthoringError, match="different scope"):
            package_service.inspect_draft(tmp_path / "other-skills", draft.id, actor_id="main")

    def test_abort_discards_only_draft(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        package_service.create(skills_root, "demo", skill_document(), author="agent")
        draft = package_service.begin_draft(
            skills_root,
            "demo",
            mode="update",
            actor_id="main",
            author="agent",
        )

        package_service.abort_draft(skills_root, draft.id, actor_id="main")

        assert not draft.path.exists()
        assert (skills_root / "demo" / "SKILL.md").is_file()

    def test_archive_delete_is_unique_and_recoverable(
        self,
        package_service: SkillAuthoringService,
        tmp_path: Path,
    ) -> None:
        skills_root = tmp_path / "skills"
        package_service.create(skills_root, "demo", skill_document(), author="agent")

        result = package_service.archive_skill(
            skills_root,
            "demo",
            archive_namespace=("global",),
        )

        assert result.operation == "archive"
        assert not (skills_root / "demo").exists()
        assert (result.path / "SKILL.md").is_file()
        assert result.path.is_relative_to(tmp_path / "archive" / "skills" / "global" / "demo")
