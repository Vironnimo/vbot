"""Contracts of the Skill registry: discovery, metadata, requirements and origins."""

import logging
from pathlib import Path

import pytest

from core.settings import PROJECT_SOURCE_FORMATS
from core.skills.requirements import environment_requirement_names
from core.skills.skill_validator import MAX_SKILL_NAME_LENGTH
from core.skills.skills import (
    PROJECT_SKILLS_SUBPATHS,
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SkillRegistry,
    _logged_skill_warnings,
    find_skill_package_dir,
    format_skill_catalog_entries,
    load_project_skill_registry,
    project_skill_origin,
    project_skills_dir,
    scan_project_skill_names,
    skill_origin_sort_key,
)


def write_skill(
    root: Path,
    directory: str,
    document: str | None = None,
    *,
    name: str | None = None,
    description: str = "Use it.",
) -> Path:
    """Write ``root/directory/SKILL.md``; without a document, a minimal valid one."""
    skill_file = root / directory / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    if document is None:
        document = f"---\nname: {name or directory}\ndescription: {description}\n---\n\nUse it.\n"
    skill_file.write_text(document, encoding="utf-8")
    return skill_file


def requirements_document(name: str, requirements: str) -> str:
    return (
        f"---\nname: {name}\ndescription: Needs something.\n"
        f"metadata:\n  vbot:\n    requirements: {requirements}\n---\n"
    )


def test_loads_skill_metadata_from_yaml_front_matter(tmp_path: Path) -> None:
    skill_file = write_skill(
        tmp_path,
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

    registry = SkillRegistry.load(tmp_path)
    skill = registry.get("agent-cli")

    assert skill.name == "agent-cli"
    assert skill.description == "Delegate coding tasks to an external CLI."
    assert skill.path == skill_file.resolve()
    assert skill.license == "MIT"
    assert skill.compatibility == {"vbot": ">=0.1"}
    assert skill.metadata == {"owner": "tests"}
    assert skill.allowed_tools == ["read"]
    assert skill.requirements.empty
    assert skill.origin is None
    assert registry.warnings_for("agent-cli") == []


MISSING_NAME = "Skill metadata missing name; using directory name 'broken'."
BODY_DESCRIPTION = "Skill metadata missing description; using the first body text line."
LONG_NAME = "a" * (MAX_SKILL_NAME_LENGTH + 1)


@pytest.mark.parametrize(
    ("directory", "document", "name", "description", "warnings"),
    [
        pytest.param(
            "research",
            "---\nname: research\ndescription: >\n  Find source material for a task.\n"
            "  Summarize the relevant facts.\n---\n",
            "research",
            "Find source material for a task. Summarize the relevant facts.",
            [],
            id="folded-description",
        ),
        pytest.param(
            "deploy",
            "﻿---\nname: deploy\ndescription: Deploy it.\n---\n\nRun it.\n",
            "deploy",
            "Deploy it.",
            [],
            id="leading-bom",
        ),
        pytest.param(
            "broken",
            "# Broken\n\nRun the repair steps.\n",
            "broken",
            "Run the repair steps.",
            [
                "SKILL.md has no complete YAML front matter; using the full file as instructions.",
                MISSING_NAME,
                BODY_DESCRIPTION,
            ],
            id="no-front-matter",
        ),
        pytest.param(
            "broken",
            "---\n- not\n- a mapping\n---\n\nDo the useful thing.\n",
            "broken",
            "Do the useful thing.",
            [
                "Invalid YAML front matter in {path}: expected a mapping; "
                "using directory and body fallbacks.",
                MISSING_NAME,
                BODY_DESCRIPTION,
            ],
            id="non-mapping-front-matter",
        ),
        pytest.param(
            "broken",
            "---\nname: broken\n---\n",
            "broken",
            "",
            [
                "Skill metadata missing description and no body text line was available; "
                "using an empty description."
            ],
            id="no-description-or-body",
        ),
        pytest.param(
            "directory-name",
            "---\nname: metadata-name\ndescription: Loadable with a warning.\n---\n",
            "metadata-name",
            "Loadable with a warning.",
            ["Skill name 'metadata-name' does not match directory name 'directory-name'."],
            id="name-differs-from-directory",
        ),
        pytest.param(
            LONG_NAME,
            f"---\nname: {LONG_NAME}\ndescription: Useful.\n---\n",
            LONG_NAME,
            "Useful.",
            [f"Skill name '{LONG_NAME}' is longer than {MAX_SKILL_NAME_LENGTH} characters."],
            id="oversized-name",
        ),
        pytest.param(
            "careful",
            "---\nname: careful\ndescription: Use mode: careful\n---\n",
            "careful",
            "Use mode: careful",
            ["YAML front matter was repaired by quoting scalar values with colons."],
            id="colon-values-repaired",
        ),
        pytest.param(
            "broken-yaml",
            "---\nname: broken-yaml\ndescription: Use mode: careful\nbroken: [unterminated\n---\n",
            "broken-yaml",
            "Use mode: careful",
            ["YAML front matter was read with the simple key: value fallback."],
            id="simple-key-value-fallback",
        ),
        pytest.param(
            "broken-yaml",
            "---\nname: broken-yaml\ndescription: [unterminated\n---\n",
            "broken-yaml",
            "[unterminated",
            ["YAML front matter was read with the simple key: value fallback."],
            id="unrepairable-yaml",
        ),
    ],
)
def test_imperfect_metadata_loads_with_fallbacks_and_warnings(
    tmp_path: Path,
    directory: str,
    document: str,
    name: str,
    description: str,
    warnings: list[str],
) -> None:
    write_skill(tmp_path, directory, document)

    registry = SkillRegistry.load(tmp_path)
    skill = registry.get(name)

    assert skill.description == description
    assert registry.warnings_for(name) == [warning.format(path=skill.path) for warning in warnings]
    assert registry.invalid_diagnostics() == []


@pytest.mark.parametrize(
    ("requirements", "warning"),
    [
        ("{42: value}", "metadata.vbot.requirements has unknown key(s): 42"),
        (
            "{42: value, unknown: value}",
            "metadata.vbot.requirements has unknown key(s): 42, unknown",
        ),
        (
            "{all: [{42: value, unknown: value}]}",
            "metadata.vbot.requirements.all[0] has unknown key(s): 42, unknown",
        ),
        ("{provider: openai}", "metadata.vbot.requirements has unknown key(s): provider"),
    ],
)
def test_invalid_requirements_reject_only_their_package(
    tmp_path: Path, requirements: str, warning: str
) -> None:
    write_skill(tmp_path, "broken", requirements_document("broken", requirements))
    write_skill(tmp_path, "healthy")

    registry = SkillRegistry.load(tmp_path)

    assert [skill.name for skill in registry.list_all()] == ["healthy"]
    [invalid] = registry.invalid_diagnostics()
    assert (invalid.name, invalid.loadable, invalid.warnings) == ("broken", False, [warning])


def test_requirement_metadata_is_kept_and_names_its_environment_variables(
    tmp_path: Path,
) -> None:
    write_skill(
        tmp_path,
        "compile",
        requirements_document(
            "compile",
            "{all: [{env: C_COMPILER}, {any: [{binary: gcc}, {binary: clang}]}], "
            "optional: [{binary: jq}]}",
        ),
    )

    skill = SkillRegistry.load(tmp_path, environment={"C_COMPILER": "clang"}).get("compile")

    assert skill.metadata["vbot"]["requirements"]["all"]
    assert not skill.requirements.empty
    assert environment_requirement_names(skill.requirements) == ("C_COMPILER",)


@pytest.mark.parametrize(
    ("requirements", "environment", "state", "missing", "optional_missing"),
    [
        pytest.param(
            "{env: OPENAI_API_KEY}",
            {},
            "unavailable",
            ("missing environment variable 'OPENAI_API_KEY'",),
            (),
            id="required-env-missing",
        ),
        pytest.param(
            "{any: [{env: OPENAI_API_KEY}, {env: ANTHROPIC_API_KEY}]}",
            {"ANTHROPIC_API_KEY": "set"},
            "available",
            (),
            (),
            id="any-alternative-present",
        ),
        pytest.param(
            "{optional: [{binary: vbot-definitely-missing-jq}]}",
            {},
            "available",
            (),
            ("missing binary 'vbot-definitely-missing-jq'",),
            id="optional-binary-missing",
        ),
    ],
)
def test_requirements_decide_availability(
    tmp_path: Path,
    requirements: str,
    environment: dict[str, str],
    state: str,
    missing: tuple[str, ...],
    optional_missing: tuple[str, ...],
) -> None:
    write_skill(tmp_path, "needs", requirements_document("needs", requirements))

    registry = SkillRegistry.load(tmp_path, environment=environment)
    availability = registry.availability_for("needs", ["*"])

    assert (availability.state, availability.missing, availability.optional_missing) == (
        state,
        missing,
        optional_missing,
    )
    listed = [skill.name for skill in registry.filter_allowed(["*"])]
    assert listed == ([] if state == "unavailable" else ["needs"])


def test_skill_dependency_respects_agent_allowlist(tmp_path: Path) -> None:
    write_skill(tmp_path, "helper")
    write_skill(tmp_path, "main-task", requirements_document("main-task", "{skill: helper}"))

    registry = SkillRegistry.load(tmp_path, environment={})

    assert registry.availability_for("main-task", ["main-task"]).state == "unavailable"
    assert registry.filter_allowed(["main-task"]) == []
    assert [skill.name for skill in registry.filter_allowed(["helper", "main-task"])] == [
        "helper",
        "main-task",
    ]


@pytest.mark.parametrize(
    ("ignore_case", "first", "first_state", "second", "second_state"),
    [
        # Windows reports process environment names upper-cased; the declared
        # mixed-case name still matches, and the later process value wins.
        (
            True,
            {"GITHUB_TOKEN": "set"},
            "available",
            {"GitHub_Token": "fallback", "GITHUB_TOKEN": ""},
            "unavailable",
        ),
        (False, {"GITHUB_TOKEN": "set"}, "unavailable", {"GitHub_Token": "set"}, "available"),
    ],
    ids=["windows", "posix"],
)
def test_env_requirement_name_case_follows_the_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_case: bool,
    first: dict[str, str],
    first_state: str,
    second: dict[str, str],
    second_state: str,
) -> None:
    monkeypatch.setattr("core.skills.skills._ENVIRONMENT_NAMES_IGNORE_CASE", ignore_case)
    write_skill(
        tmp_path, "github-helper", requirements_document("github-helper", "{env: GitHub_Token}")
    )

    registry = SkillRegistry.load(tmp_path, environment=first)
    assert registry.availability_for("github-helper", ["*"]).state == first_state

    registry.reload_environment(second)
    assert registry.availability_for("github-helper", ["*"]).state == second_state


def test_roots_without_skill_packages_load_empty(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    (plain / "empty-dir").mkdir(parents=True)
    (plain / "README.md").write_text("not a skill", encoding="utf-8")

    for root in (tmp_path / "missing", plain):
        registry = SkillRegistry.load(root)
        assert registry.list_all() == []
        assert registry.diagnostics() == []


def test_unreadable_skill_root_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    original_iterdir = Path.iterdir

    def fail_target_iterdir(path: Path):
        if path == skills_dir:
            raise PermissionError("denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_target_iterdir)

    registry = SkillRegistry.load(skills_dir)

    assert registry.list_all() == []
    [invalid] = registry.invalid_diagnostics()
    assert "Cannot scan skill directory" in invalid.warnings[0]


def test_roots_load_in_order_with_origins_and_first_found_name_wins(tmp_path: Path) -> None:
    own, global_dir, package = tmp_path / "own", tmp_path / "global", tmp_path / "package"
    kept = write_skill(own, "first", name="shared", description="Own copy.")
    same_root_duplicate = write_skill(own, "second", name="shared")
    write_skill(global_dir, "extra")
    later_root_duplicate = write_skill(global_dir, "shared", description="Global copy.")
    # A root that is itself a package contributes exactly that one package.
    write_skill(tmp_path, "package", name="deploy")
    write_skill(package / "scripts", "inner")

    registry = SkillRegistry.load(
        own,
        extra_dirs=[global_dir, package],
        origins=[SKILL_ORIGIN_AGENT, SKILL_ORIGIN_GLOBAL, SKILL_ORIGIN_AGENT],
    )

    assert [skill.name for skill in registry.list_all()] == ["deploy", "extra", "shared"]
    assert registry.get("shared").path == kept.resolve()
    assert registry.get("shared").origin == SKILL_ORIGIN_AGENT
    assert registry.get("extra").origin == SKILL_ORIGIN_GLOBAL
    assert registry.get("deploy").origin == SKILL_ORIGIN_AGENT
    invalid = registry.invalid_diagnostics()
    assert {diagnostic.path for diagnostic in invalid} == {
        same_root_duplicate.resolve(),
        later_root_duplicate.resolve(),
    }
    assert all("Duplicate skill name 'shared' rejected" in item.warnings[-1] for item in invalid)


def test_metadata_diagnostic_is_logged_once_per_process_with_its_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Registries reload on every run; the DEBUG record names the file once.
    _logged_skill_warnings.clear()
    skill_file = write_skill(
        tmp_path, "careful", "---\nname: careful\ndescription: Use mode: careful\n---\n"
    )
    caplog.set_level(logging.DEBUG, logger="vbot.skills")

    SkillRegistry.load(tmp_path)
    SkillRegistry.load(tmp_path)

    [record] = [record for record in caplog.records if record.name == "vbot.skills"]
    assert record.levelno == logging.DEBUG
    message = record.getMessage()
    assert "careful" in message
    assert str(skill_file.resolve()) in message
    assert "metadata diagnostic" in message


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (["*"], ["agent-cli", "research"]),
        ([], []),
        (["research"], ["research"]),
        (["missing", "agent-cli"], ["agent-cli"]),
    ],
)
def test_allowlist_filters_skills_sorted_by_name(
    tmp_path: Path, allowed: list[str], expected: list[str]
) -> None:
    write_skill(tmp_path, "z-dir", name="research")
    write_skill(tmp_path, "a-dir", name="agent-cli")

    registry = SkillRegistry.load(tmp_path)

    assert [skill.name for skill in registry.list_all()] == ["agent-cli", "research"]
    assert [skill.name for skill in registry.filter_allowed(allowed)] == expected


def test_excluded_names_hide_every_copy_of_the_skill(tmp_path: Path) -> None:
    global_dir, bundled_dir = tmp_path / "global", tmp_path / "bundled"
    write_skill(global_dir, "deploy", description="Global copy.")
    write_skill(global_dir, "review")
    write_skill(bundled_dir, "deploy", description="Bundled copy.")

    def load(excluded_names: set[str] | None = None) -> SkillRegistry:
        return SkillRegistry.load(
            global_dir,
            extra_dirs=[bundled_dir],
            origins=[SKILL_ORIGIN_GLOBAL, SKILL_ORIGIN_BUNDLED],
            excluded_names=excluded_names,
        )

    registry = load({"deploy", "ghost"})

    assert [skill.name for skill in registry.list_all()] == ["review"]
    assert [skill.name for skill in registry.filter_allowed(["*"])] == ["review"]
    assert registry.availability_for("deploy", ["*"]).state == "invalid"
    with pytest.raises(KeyError, match="deploy"):
        registry.get("deploy")
    [excluded] = registry.excluded_skills()
    assert (excluded.name, excluded.origin, excluded.description) == (
        "deploy",
        SKILL_ORIGIN_GLOBAL,
        "Global copy.",
    )
    assert load().excluded_skills() == []


def test_catalog_groups_skills_by_origin_in_display_order() -> None:
    from types import SimpleNamespace

    origins = [None, "agent", project_skill_origin("Zeta"), "global", "bundled"]
    assert sorted([*origins, project_skill_origin("Alpha")], key=skill_origin_sort_key) == [
        "bundled",
        "global",
        "project:Alpha",
        "project:Zeta",
        "agent",
        None,
    ]
    skills = [
        SimpleNamespace(name="own", description="Mine.", origin="agent"),
        SimpleNamespace(name="deploy", description="Ship it.\n  Then verify.", origin="bundled"),
        SimpleNamespace(name="loose", description="No origin.", origin=None),
    ]

    assert format_skill_catalog_entries(skills) == (
        "Bundled skills:\n- deploy: Ship it. Then verify.\n"
        "Your own skills:\n- own: Mine.\n"
        "Skills:\n- loose: No origin."
    )


def test_project_skill_directories_follow_the_source_format(tmp_path: Path) -> None:
    # The subpaths are keyed by the core.settings vocabulary; a new format must land in both.
    assert set(PROJECT_SKILLS_SUBPATHS) == set(PROJECT_SOURCE_FORMATS)
    assert project_skills_dir(tmp_path, "opencode") == tmp_path / ".opencode" / "skills"
    assert project_skills_dir(tmp_path, "claude") == tmp_path / ".claude" / "skills"
    with pytest.raises(KeyError):
        project_skills_dir(tmp_path, "cursor")

    write_skill(tmp_path / ".opencode" / "skills", "deploy")
    write_skill(tmp_path / ".claude" / "skills", "review")
    write_skill(tmp_path / "bundled", "teach")

    assert scan_project_skill_names(tmp_path, "opencode") == frozenset({"deploy"})
    assert scan_project_skill_names(tmp_path, "claude") == frozenset({"review"})
    registry = load_project_skill_registry(tmp_path, "claude", [tmp_path / "bundled"])
    assert {skill.name for skill in registry.list_all()} == {"review", "teach"}


def test_find_skill_package_dir_resolves_only_existing_packages(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_skill(home, "notes")

    assert find_skill_package_dir(home, "notes") == home / "notes"
    assert find_skill_package_dir(home, "vanished-skill") is None
    assert find_skill_package_dir(tmp_path / "gone", "anything") is None
