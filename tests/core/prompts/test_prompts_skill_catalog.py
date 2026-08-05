"""Prompt Skill catalog and explicit Project Context rendering tests."""

from core.utils.paths import model_path

from .prompts_test_support import (
    MEMORY_PROMPT_MODE_OFF,
    Path,
    PinnedSkillCatalog,
    StubSkill,
    StubSkills,
    _agent,
    _manager,
)


def test_build_system_prompt_skill_registry_override_scopes_skills_block(tmp_path: Path) -> None:
    global_skills = StubSkills([StubSkill("global-skill", "Global only.")])
    project_skills = StubSkills([StubSkill("project-skill", "Project only.")])
    manager = _manager(tmp_path, skills=global_skills)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent, skill_registry=project_skills)

    assert "project-skill" in prompt
    assert "global-skill" not in prompt


def test_skill_catalog_groups_skills_by_origin(tmp_path: Path) -> None:
    skills = StubSkills(
        [
            StubSkill("own-skill", "Mine.", origin="agent"),
            StubSkill("bundled-skill", "Shipped.", origin="bundled"),
            StubSkill("proj-skill", "From the repo.", origin="project:Acme"),
        ]
    )
    manager = _manager(tmp_path, skills=skills)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert '<skill_group label="Bundled skills">' in prompt
    assert '<skill_group label="Your own skills">' in prompt
    assert "Skills from project" in prompt and "Acme" in prompt
    # Plan order: bundled, then project, then the agent's own.
    assert prompt.index("Bundled skills") < prompt.index("Acme") < prompt.index("Your own skills")
    # The catalog stays path-free.
    assert "/skills/" not in prompt


def test_render_project_skills_lists_names_descriptions_and_paths(tmp_path: Path) -> None:
    from types import SimpleNamespace

    manager = _manager(tmp_path)
    deploy_path = Path("/repo/.opencode/skills/deploy/SKILL.md")
    skills = [
        SimpleNamespace(name="deploy", description="Ship it.", path=deploy_path),
        SimpleNamespace(
            name="audit",
            description="Check it.",
            path=Path("/repo/.opencode/skills/audit/SKILL.md"),
        ),
    ]

    rendered = manager.render_project_skills("vBot", skills)

    assert "Skills from project 'vBot'" in rendered
    assert "load one by name with the `skill` Tool" in rendered
    assert f"- deploy: Ship it. ({model_path(deploy_path)})" in rendered
    # Sorted by name: audit before deploy.
    assert rendered.index("audit") < rendered.index("deploy")


def test_render_project_skills_empty_is_blank(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert manager.render_project_skills("vBot", []) == ""


def test_render_skill_catalog_snapshots_text(tmp_path: Path) -> None:
    skills = StubSkills([StubSkill("alpha", "First.", origin="bundled")])
    manager = _manager(tmp_path, skills=skills)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    snapshot = manager.render_skill_catalog(agent)
    assert "<name>alpha</name>" in snapshot.catalog_text

    empty = manager.render_skill_catalog(agent, skill_registry=StubSkills([]))
    assert "<name>" not in empty.catalog_text


def test_build_system_prompt_pins_catalog_over_live_registry(tmp_path: Path) -> None:
    # A skill written after the session pinned its catalog must not appear: the
    # pinned snapshot text wins over the (now richer) live registry.
    live = StubSkills([StubSkill("new-skill", "Written later.", origin="agent")])
    manager = _manager(tmp_path, skills=live)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    pinned = PinnedSkillCatalog(
        catalog_text="<available_skills>PINNED-CATALOG</available_skills>",
    )

    prompt = manager.build_system_prompt(agent, skill_catalog=pinned)

    assert "PINNED-CATALOG" in prompt
    assert "new-skill" not in prompt
