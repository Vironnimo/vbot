"""Tests for the local skill metadata registry."""

import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.skills.requirements import environment_requirement_names
from core.skills.skills import (
    SkillMetadata,
    SkillRegistry,
    _logged_skill_warnings,
    _scan_skill_resources,
    project_skill_origin,
    skill_origin_sort_key,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def write_skill(skills_dir: Path, directory_name: str, metadata: str) -> Path:
    skill_dir = skills_dir / directory_name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(metadata, encoding="utf-8")
    return skill_file


def test_bundled_coding_agents_uses_interactive_terminal_contract() -> None:
    package = PROJECT_ROOT / "resources" / "skills" / "coding-agents"
    skill_text = (package / "SKILL.md").read_text(encoding="utf-8")
    references = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((package / "references").glob("*.md"))
    }
    reference_text = "\n".join(references.values())
    combined = f"{skill_text}\n{reference_text}"

    assert "terminal_beta" in skill_text
    assert "Terminal Sessions survive individual Runs" in skill_text
    assert "expected_screen_revision" in skill_text
    assert "terminal_id" in reference_text
    assert "machine-output invocation" in skill_text
    assert "permission checks" in skill_text
    assert "scrollback.next_request" in skill_text
    assert "Request at most 100 lines" in skill_text
    assert "raw `log_file` only for VT-level diagnostics" in skill_text
    assert "preserve the CLI or project default for every value they omit" in skill_text
    assert '"gpt-5.6-terra"' in references["codex.md"]
    assert 'model_reasoning_effort=\\"medium\\"' in references["codex.md"]
    assert '"--effort"' in references["claude-code.md"]
    assert '"medium"' in references["claude-code.md"]
    assert "/variants" in references["opencode.md"]
    assert "reasoningEffort" in references["opencode.md"]
    forbidden_invocations = (
        "claude" + " -p",
        "codex" + " exec",
        "opencode" + " run",
    )
    assert all(invocation not in combined for invocation in forbidden_invocations)

    registry = SkillRegistry.load(PROJECT_ROOT / "resources" / "skills")
    skill = registry.get("coding-agents")
    assert skill.requirements.empty
    assert registry.availability_for("coding-agents", ["coding-agents"]).state == "available"


class TestSkillMetadata:
    def test_fields(self) -> None:
        path = Path("/skills/coder/SKILL.md")
        skill = SkillMetadata(
            name="coder",
            description="Handle coding tasks.",
            path=path,
            license="MIT",
            compatibility={"vbot": ">=0.1"},
            metadata={"owner": "test"},
            allowed_tools=["read"],
        )

        assert skill.name == "coder"
        assert skill.description == "Handle coding tasks."
        assert skill.path == path
        assert skill.license == "MIT"
        assert skill.compatibility == {"vbot": ">=0.1"}
        assert skill.metadata == {"owner": "test"}
        assert skill.allowed_tools == ["read"]

    def test_frozen(self) -> None:
        skill = SkillMetadata(
            name="coder",
            description="Handle coding tasks.",
            path=Path("/skills/coder/SKILL.md"),
        )

        with pytest.raises(FrozenInstanceError):
            skill.name = "changed"  # type: ignore[misc]


class TestSkillRegistryLoad:
    def test_loads_skill_metadata_from_yaml_front_matter(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill_file = write_skill(
            skills_dir,
            "agent-cli",
            """---
name: agent-cli
description: Delegate coding tasks to an external CLI.
license: MIT
compatibility:
  vbot: ">=0.1"
metadata:
  owner: tests
allowed-tools:
  - read
---

# Agent CLI
""",
        )

        registry = SkillRegistry.load(skills_dir)
        skill = registry.get("agent-cli")

        assert skill.name == "agent-cli"
        assert skill.description == "Delegate coding tasks to an external CLI."
        assert skill.path == skill_file.resolve()
        assert skill.license == "MIT"
        assert skill.compatibility == {"vbot": ">=0.1"}
        assert skill.metadata == {"owner": "tests"}
        assert skill.allowed_tools == ["read"]
        assert skill.requirements.empty

    def test_loads_folded_multiline_description(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "research",
            """---
name: research
description: >
  Find source material for a task.
  Summarize the relevant facts.
---

# Research
""",
        )

        registry = SkillRegistry.load(skills_dir)
        skill = registry.get("research")

        assert skill.description == "Find source material for a task. Summarize the relevant facts."

        def test_loads_vbot_requirement_metadata(self, tmp_path: Path) -> None:
            skills_dir = tmp_path / "skills"
            write_skill(
                skills_dir,
                "compile",
                """---
name: compile
description: Compile native code.
metadata:
    vbot:
        requirements:
            all:
                - env: C_COMPILER
                - any:
                        - binary: gcc
                        - binary: clang
            optional:
                - binary: jq
---
""",
            )

            registry = SkillRegistry.load(skills_dir, environment={"C_COMPILER": "clang"})
            skill = registry.get("compile")

            assert skill.metadata["vbot"]["requirements"]["all"]
            assert not skill.requirements.empty
            assert environment_requirement_names(skill.requirements) == ("C_COMPILER",)

        def test_unavailable_when_required_env_missing(self, tmp_path: Path) -> None:
            skills_dir = tmp_path / "skills"
            write_skill(
                skills_dir,
                "openai-helper",
                """---
name: openai-helper
description: Use OpenAI.
metadata:
    vbot:
        requirements:
            env: OPENAI_API_KEY
---
""",
            )

            registry = SkillRegistry.load(skills_dir, environment={})
            availability = registry.availability_for("openai-helper", ["*"])

            assert availability.state == "unavailable"
            assert availability.missing == ("missing environment variable 'OPENAI_API_KEY'",)
            assert registry.filter_allowed(["*"]) == []

        def test_any_requirement_accepts_present_alternative(self, tmp_path: Path) -> None:
            skills_dir = tmp_path / "skills"
            write_skill(
                skills_dir,
                "provider-flex",
                """---
name: provider-flex
description: Use any supported provider token.
metadata:
    vbot:
        requirements:
            any:
                - env: OPENAI_API_KEY
                - env: ANTHROPIC_API_KEY
---
""",
            )

            registry = SkillRegistry.load(skills_dir, environment={"ANTHROPIC_API_KEY": "set"})

            assert registry.availability_for("provider-flex", ["*"]).state == "available"
            assert [skill.name for skill in registry.filter_allowed(["*"])] == ["provider-flex"]

        def test_optional_requirement_does_not_make_skill_unavailable(self, tmp_path: Path) -> None:
            skills_dir = tmp_path / "skills"
            write_skill(
                skills_dir,
                "fast-json",
                """---
name: fast-json
description: Handle JSON.
metadata:
    vbot:
        requirements:
            optional:
                - binary: vbot-definitely-missing-jq
---
""",
            )

            registry = SkillRegistry.load(skills_dir, environment={})
            availability = registry.availability_for("fast-json", ["*"])

            assert availability.state == "available"
            assert availability.optional_missing == ("missing binary 'vbot-definitely-missing-jq'",)
            assert [skill.name for skill in registry.filter_allowed(["*"])] == ["fast-json"]

        def test_skill_dependency_respects_agent_allowlist(self, tmp_path: Path) -> None:
            skills_dir = tmp_path / "skills"
            write_skill(skills_dir, "helper", "---\nname: helper\ndescription: Helper.\n---\n")
            write_skill(
                skills_dir,
                "main-task",
                """---
name: main-task
description: Main task.
metadata:
    vbot:
        requirements:
            skill: helper
---
""",
            )

            registry = SkillRegistry.load(skills_dir, environment={})

            assert registry.availability_for("main-task", ["main-task"]).state == "unavailable"
            assert [skill.name for skill in registry.filter_allowed(["main-task"])] == []
            assert [skill.name for skill in registry.filter_allowed(["helper", "main-task"])] == [
                "helper",
                "main-task",
            ]

        def test_invalid_vbot_requirements_rejects_skill(self, tmp_path: Path) -> None:
            skills_dir = tmp_path / "skills"
            write_skill(
                skills_dir,
                "provider-only",
                """---
name: provider-only
description: Invalid because provider checks are not supported.
metadata:
    vbot:
        requirements:
            provider: openai
---
""",
            )

            registry = SkillRegistry.load(skills_dir, environment={})

            assert registry.list_all() == []
            assert "unknown key(s): provider" in registry.invalid_diagnostics()[0].warnings[0]

    def test_missing_skills_directory_loads_empty_registry(self, tmp_path: Path) -> None:
        registry = SkillRegistry.load(tmp_path / "missing-skills")

        assert registry.list_all() == []
        assert registry.diagnostics() == []

    def test_ignores_non_skill_directories_and_files(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "README.md").write_text("not a skill", encoding="utf-8")
        (skills_dir / "empty-dir").mkdir()

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        assert registry.diagnostics() == []

    def test_duplicate_skill_name_first_found_wins_with_diagnostic(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        first_file = write_skill(
            skills_dir,
            "first",
            """---
name: duplicate
description: First skill.
---
""",
        )
        second_file = write_skill(
            skills_dir,
            "second",
            """---
name: duplicate
description: Second skill.
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.get("duplicate").path == first_file.resolve()
        invalid = registry.invalid_diagnostics()
        assert invalid[0].path == second_file.resolve()
        assert "Duplicate skill name 'duplicate' rejected" in invalid[0].warnings[-1]

    def test_missing_front_matter_is_preserved_as_invalid_diagnostic(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill_file = write_skill(skills_dir, "broken", "# Broken\n")

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        invalid = registry.invalid_diagnostics()
        assert invalid[0].path == skill_file.resolve()
        assert "missing front matter" in invalid[0].warnings[0]

    def test_missing_required_metadata_is_preserved_as_invalid_diagnostic(
        self, tmp_path: Path
    ) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "broken",
            """---
name: broken
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        invalid = registry.invalid_diagnostics()
        assert invalid[0].name == "broken"
        assert "missing description" in invalid[0].warnings[0]

    def test_loadable_skill_with_warning_remains_available(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "directory-name",
            """---
name: metadata-name
description: Loadable with a warning.
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.get("metadata-name").description == "Loadable with a warning."
        assert registry.warnings_for("metadata-name") == [
            "Skill name 'metadata-name' does not match directory name 'directory-name'."
        ]
        assert registry.invalid_diagnostics() == []

    def test_malformed_yaml_fallback_loads_with_warning(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "careful",
            """---
name: careful
description: Use mode: careful
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.get("careful").description == "Use mode: careful"
        assert registry.warnings_for("careful") == [
            "YAML front matter was repaired by quoting scalar values with colons."
        ]
        assert registry.invalid_diagnostics() == []

    def test_invalid_yaml_is_preserved_as_invalid_diagnostic(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "broken-yaml",
            """---
name: broken-yaml
description: [unterminated
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        invalid = registry.invalid_diagnostics()
        assert invalid[0].name == "broken-yaml"
        assert "Invalid YAML front matter" in invalid[0].warnings[0]

    def test_extra_scan_directories_are_loaded_after_primary_dir(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        extra_dir = tmp_path / "extra-skills"
        primary_file = write_skill(
            skills_dir,
            "primary",
            "---\nname: shared\ndescription: Primary.\n---\n",
        )
        write_skill(extra_dir, "extra", "---\nname: extra\ndescription: Extra.\n---\n")
        duplicate_file = write_skill(
            extra_dir,
            "shared-duplicate",
            "---\nname: shared\ndescription: Duplicate.\n---\n",
        )

        registry = SkillRegistry.load(skills_dir, extra_dirs=[extra_dir])

        assert [skill.name for skill in registry.list_all()] == ["extra", "shared"]
        assert registry.get("shared").path == primary_file.resolve()
        invalid = registry.invalid_diagnostics()
        assert invalid[0].path == duplicate_file.resolve()
        assert "Duplicate skill name 'shared' rejected" in invalid[0].warnings[-1]


class TestSkillDiagnosticLogging:
    """The DEBUG log for a skill diagnostic names its file and never floods.

    Registries reload on every project run / reload, so the same diagnostic would
    otherwise be logged on each scan. It must carry the skill's path (so the user
    can find the offending file) and be emitted once per process.
    """

    def _write_malformed_skill(self, skills_dir: Path) -> Path:
        # ``description: Use mode: careful`` has an unquoted colon-space value, so
        # the loader repairs it and records the MALFORMED_YAML_FALLBACK_WARNING.
        return write_skill(
            skills_dir,
            "careful",
            "---\nname: careful\ndescription: Use mode: careful\n---\n",
        )

    def test_debug_log_names_the_skill_file_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _logged_skill_warnings.clear()
        skills_dir = tmp_path / "skills"
        skill_file = self._write_malformed_skill(skills_dir)
        caplog.set_level(logging.DEBUG, logger="vbot.skills")

        SkillRegistry.load(skills_dir)

        records = [record for record in caplog.records if record.name == "vbot.skills"]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        message = records[0].getMessage()
        assert "careful" in message
        assert str(skill_file.resolve()) in message
        assert "metadata diagnostic" in message

    def test_same_diagnostic_is_logged_once_per_process(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _logged_skill_warnings.clear()
        skills_dir = tmp_path / "skills"
        self._write_malformed_skill(skills_dir)
        caplog.set_level(logging.DEBUG, logger="vbot.skills")

        SkillRegistry.load(skills_dir)
        SkillRegistry.load(skills_dir)

        records = [record for record in caplog.records if record.name == "vbot.skills"]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG


class TestSkillRegistryGet:
    def test_get_missing_skill_raises_key_error(self, tmp_path: Path) -> None:
        registry = SkillRegistry.load(tmp_path / "missing-skills")

        with pytest.raises(KeyError, match="Skill not found: missing"):
            registry.get("missing")


class TestSkillRegistryListAll:
    def test_list_all_returns_skills_sorted_by_name(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(skills_dir, "z-dir", "---\nname: zeta\ndescription: Last.\n---\n")
        write_skill(skills_dir, "a-dir", "---\nname: alpha\ndescription: First.\n---\n")

        registry = SkillRegistry.load(skills_dir)
        skills = registry.list_all()

        assert [skill.name for skill in skills] == ["alpha", "zeta"]


class TestSkillRegistryFilterAllowed:
    def test_wildcard_allows_all_skills(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        skills = registry.filter_allowed(["*"])

        assert [skill.name for skill in skills] == ["agent-cli", "research"]

    def test_empty_allowlist_allows_no_skills(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        assert registry.filter_allowed([]) == []

    def test_explicit_allowlist_filters_by_name(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        skills = registry.filter_allowed(["research"])

        assert [skill.name for skill in skills] == ["research"]

    def test_unknown_allowed_skill_is_ignored(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        skills = registry.filter_allowed(["missing", "agent-cli"])

        assert [skill.name for skill in skills] == ["agent-cli"]


class TestScanSkillResources:
    def test_returns_resource_directory_relative_paths(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "coder"
        (skill_dir / "scripts" / "nested").mkdir(parents=True)
        (skill_dir / "references").mkdir()
        (skill_dir / "assets").mkdir()
        (skill_dir / "scripts" / "run.py").write_text("", encoding="utf-8")
        (skill_dir / "scripts" / "nested" / "helper.py").write_text("", encoding="utf-8")
        (skill_dir / "references" / "guide.md").write_text("", encoding="utf-8")
        (skill_dir / "assets" / "template.html").write_text("", encoding="utf-8")
        (skill_dir / "notes" / "ignored.md").parent.mkdir()
        (skill_dir / "notes" / "ignored.md").write_text("", encoding="utf-8")

        resources = _scan_skill_resources(skill_dir)

        assert resources == [
            "scripts/nested/helper.py",
            "scripts/run.py",
            "references/guide.md",
            "assets/template.html",
        ]

    def test_returns_empty_list_when_resource_directories_are_missing(self, tmp_path: Path) -> None:
        assert _scan_skill_resources(tmp_path / "skills" / "coder") == []


def registry_with_two_skills(tmp_path: Path) -> SkillRegistry:
    skills_dir = tmp_path / "skills"
    write_skill(
        skills_dir,
        "agent-cli",
        """---
name: agent-cli
description: Delegate coding tasks.
---
""",
    )
    write_skill(
        skills_dir,
        "research",
        """---
name: research
description: Find source material.
---
""",
    )
    return SkillRegistry.load(skills_dir)


class TestSkillOrigin:
    def test_load_records_origin_from_scan_roots(self, tmp_path: Path) -> None:
        write_skill(tmp_path / "agent", "a", "---\nname: a\ndescription: A.\n---\n")
        write_skill(tmp_path / "global", "g", "---\nname: g\ndescription: G.\n---\n")

        registry = SkillRegistry.load(
            tmp_path / "agent", extra_dirs=[tmp_path / "global"], origins=["agent", "global"]
        )

        assert registry.get("a").origin == "agent"
        assert registry.get("g").origin == "global"

    def test_load_without_origins_leaves_origin_none(self, tmp_path: Path) -> None:
        write_skill(tmp_path / "s", "x", "---\nname: x\ndescription: X.\n---\n")

        registry = SkillRegistry.load(tmp_path / "s")

        assert registry.get("x").origin is None

    def test_project_skill_origin_carries_display_name(self) -> None:
        assert project_skill_origin("Acme") == "project:Acme"

    def test_skill_origin_sort_key_orders_bundled_global_project_agent(self) -> None:
        origins = [None, "agent", "project:Zeta", "global", "bundled", "project:Alpha"]

        assert sorted(origins, key=skill_origin_sort_key) == [
            "bundled",
            "global",
            "project:Alpha",
            "project:Zeta",
            "agent",
            None,
        ]


class TestProjectSkillLocations:
    def test_subpaths_cover_exactly_the_canonical_source_formats(self) -> None:
        # The dict keys are the core.settings vocabulary — asserted here so the two
        # can never drift (a new format must land in both places).
        from core.settings import PROJECT_SOURCE_FORMATS
        from core.skills.skills import PROJECT_SKILLS_SUBPATHS

        assert set(PROJECT_SKILLS_SUBPATHS) == set(PROJECT_SOURCE_FORMATS)

    def test_project_skills_dir_resolves_per_format(self, tmp_path: Path) -> None:
        from core.skills.skills import project_skills_dir

        assert project_skills_dir(tmp_path, "opencode") == tmp_path / ".opencode" / "skills"
        assert project_skills_dir(tmp_path, "claude") == tmp_path / ".claude" / "skills"

    def test_project_skills_dir_rejects_unknown_format(self, tmp_path: Path) -> None:
        from core.skills.skills import project_skills_dir

        with pytest.raises(KeyError):
            project_skills_dir(tmp_path, "cursor")

    def test_scan_project_skill_names_reads_the_chosen_format_only(self, tmp_path: Path) -> None:
        from core.skills.skills import scan_project_skill_names

        write_skill(
            tmp_path / ".opencode" / "skills", "deploy", "---\nname: deploy\ndescription: D.\n---\n"
        )
        write_skill(
            tmp_path / ".claude" / "skills", "review", "---\nname: review\ndescription: R.\n---\n"
        )

        assert scan_project_skill_names(tmp_path, "opencode") == frozenset({"deploy"})
        assert scan_project_skill_names(tmp_path, "claude") == frozenset({"review"})

    def test_load_project_skill_registry_uses_format_directory(self, tmp_path: Path) -> None:
        from core.skills.skills import load_project_skill_registry

        write_skill(
            tmp_path / ".claude" / "skills", "review", "---\nname: review\ndescription: R.\n---\n"
        )
        write_skill(tmp_path / "bundled", "teach", "---\nname: teach\ndescription: T.\n---\n")

        registry = load_project_skill_registry(tmp_path, "claude", [tmp_path / "bundled"])

        assert {skill.name for skill in registry.list_all()} == {"review", "teach"}
