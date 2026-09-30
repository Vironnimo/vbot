"""Contracts of the ``skill`` Tool: catalog, activation and package file reads."""

from pathlib import Path
from typing import Any

import pytest

from core.skills import SkillAuthoringService
from core.skills.skills import SkillRegistry
from core.tools import SKILL_TOOL_NAME, ToolContractError, tool_failure
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.skill import load_skill_content
from tests.core.tools.skill_test_support import SkillTool

GUIDE = "Read the evidence first.\n"
SCRIPT = "print('debugging')\n"


def debugging_skills(tmp_path: Path) -> Path:
    """A skills root holding ``debugging`` with one file in each resource directory."""
    skill_dir = tmp_path / "skills" / "debugging"
    for directory in ("scripts", "references", "assets"):
        (skill_dir / directory).mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text(SCRIPT, encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (skill_dir / "assets" / "checklist.txt").write_text("Check everything.\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: debugging\ndescription: Debug failures.\n---\n\n"
        "# Debugging\n\nInvestigate failures methodically.\n",
        encoding="utf-8",
    )
    return tmp_path / "skills"


def debugging_directory(tmp_path: Path) -> str:
    """The debugging Skill's directory as the activation payload reports it."""
    return (tmp_path / "skills" / "debugging").resolve().as_posix()


def debugging_tool(tmp_path: Path) -> SkillTool:
    return SkillTool(tmp_path, SkillRegistry.load(debugging_skills(tmp_path)))


def write_skill(root: Path, directory: str, document: str) -> None:
    (root / directory).mkdir(parents=True)
    (root / directory / "SKILL.md").write_text(document, encoding="utf-8")


class ActivationRecorder:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.activations: dict[str, str] = {}

    def __call__(self, name: str, content: str) -> bool:
        self.activations[name] = content
        return self.accept


def test_registration_exposes_one_tool_with_name_and_file_path(tmp_path: Path) -> None:
    [definition] = debugging_tool(tmp_path).tools.provider_definitions(["*"])
    parameters = definition["parameters"]

    assert definition["name"] == SKILL_TOOL_NAME
    assert set(parameters["properties"]) == {"name", "file_path"}
    assert parameters["properties"]["name"]["type"] == "string"
    assert parameters["properties"]["name"]["minLength"] == 1
    assert parameters["properties"]["file_path"]["type"] == "string"
    assert parameters["required"] == []
    assert "additionalProperties" not in parameters


def test_activation_separates_instructions_and_resource_files(tmp_path: Path) -> None:
    recorder = ActivationRecorder()

    result = debugging_tool(tmp_path).call({"name": "debugging"}, activation_hook=recorder)

    skill_directory = debugging_directory(tmp_path)
    data = result["data"]
    assert result["ok"] is True
    assert (data["name"], data["status"]) == ("debugging", "loaded")
    assert data["content"] == "# Debugging\n\nInvestigate failures methodically."
    assert data["resource_files"] == {
        "guidance": (
            f"Files of this Skill. Run a scripts/ file by its absolute path with "
            f"`{SHELL_MODEL_NAME}`; read another file with `skill` using this name and its "
            "relative file_path only when the instructions call for it."
        ),
        "files": [
            f"{skill_directory}/scripts/run.py",
            "references/guide.md",
            "assets/checklist.txt",
        ],
    }
    activation_content = recorder.activations["debugging"]
    assert activation_content.startswith('<skill_content name="debugging">')
    assert f"- {skill_directory}/scripts/run.py" in activation_content
    assert "- references/guide.md" in activation_content
    assert "- assets/checklist.txt" in activation_content
    assert data["content"] in activation_content


def test_env_requirements_add_environment_access_guidance(tmp_path: Path) -> None:
    write_skill(
        tmp_path / "skills",
        "provider-probe",
        "---\nname: provider-probe\ndescription: Probe provider APIs.\n"
        "metadata:\n  vbot:\n    requirements:\n      all:\n"
        "        - env: OPENAI_API_KEY\n        - env: OPENROUTER_API_KEY\n---\n\n"
        "# Provider Probe\n\nCall the provider API.\n",
    )
    registry = SkillRegistry.load(
        tmp_path / "skills",
        environment={"OPENAI_API_KEY": "available", "OPENROUTER_API_KEY": "available"},
    )

    tool = SkillTool(tmp_path, registry)
    result = tool.call({"name": "provider-probe"})

    assert result["data"]["content"] == "# Provider Probe\n\nCall the provider API."
    # The user sees the instructions and that credentials reach shell commands.
    assert tool.details({"name": "provider-probe"}, result) == [
        {
            "type": "text",
            "label": "content",
            "source": {"from": "result", "path": ["data", "content"]},
        },
        {
            "type": "notice",
            "level": "info",
            "text": "This Skill makes additional environment credentials available to shell "
            "commands.",
        },
    ]
    guidance = result["data"]["environment_access"]
    assert "Loading this Skill makes these additional environment credentials" in guidance
    assert "- `OPENAI_API_KEY`" in guidance
    assert "- `OPENROUTER_API_KEY`" in guidance
    assert f"`env_keys` array of every `{SHELL_MODEL_NAME}` call" in guidance
    assert "<environment_access>" not in guidance


def test_unknown_arguments_are_rejected_before_the_handler(tmp_path: Path) -> None:
    with pytest.raises(ToolContractError, match='"unexpected" is not a parameter'):
        debugging_tool(tmp_path).call({"name": "debugging", "unexpected": True})


def test_unknown_skill_rescans_once_then_fails(tmp_path: Path) -> None:
    # A name hand-dropped just before the call gets one rescan before the miss is final.
    refreshes: list[None] = []
    tool = SkillTool(
        tmp_path,
        SkillRegistry.load(debugging_skills(tmp_path)),
        lambda: refreshes.append(None),
    )

    result = tool.call({"name": "missing"})

    assert result == tool_failure(
        "skill_not_found", "Skill not found: missing. Available Skills: debugging."
    )
    assert len(refreshes) == 1


def test_rescan_makes_a_newly_dropped_skill_loadable(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    state = {"registry": SkillRegistry.load(skills_dir)}

    def refresh() -> None:
        debugging_skills(tmp_path)
        state["registry"] = SkillRegistry.load(skills_dir)

    tool = SkillTool(tmp_path, lambda _project_id, _agent_id: state["registry"], refresh)

    result = tool.call({"name": "debugging"})

    assert (result["data"]["name"], result["data"]["status"]) == ("debugging", "loaded")


def test_registry_is_resolved_per_project_and_only_identity_runs_pass_their_agent(
    tmp_path: Path,
) -> None:
    # A project run's config-agent slug must reach the resolver as None, so a
    # same-named identity Agent's private home never leaks past the project whitelist.
    global_registry = SkillRegistry.load(debugging_skills(tmp_path))
    write_skill(
        tmp_path / "project-skills",
        "proj-skill",
        "---\nname: proj-skill\ndescription: Project scoped.\n---\n\nBody.\n",
    )
    project_registry = SkillRegistry.load(tmp_path / "project-skills")
    calls: list[tuple[str | None, str | None]] = []

    def resolver(project_id: str | None, identity_agent_id: str | None) -> SkillRegistry:
        calls.append((project_id, identity_agent_id))
        return project_registry if project_id == "vbot" else global_registry

    tool = SkillTool(tmp_path, resolver)

    project_result = tool.call({"name": "proj-skill"}, project_id="vbot")
    identity_result = tool.call({"name": "proj-skill"})

    assert project_result["ok"] is True
    assert identity_result == tool_failure(
        "skill_not_found", "Skill not found: proj-skill. Available Skills: debugging."
    )
    # The identity miss resolves again after its one rescan.
    assert calls == [("vbot", None), (None, "coder"), (None, "coder")]


def test_unavailable_skill_fails_with_missing_requirements(tmp_path: Path) -> None:
    write_skill(
        tmp_path / "skills",
        "openai-helper",
        "---\nname: openai-helper\ndescription: Use OpenAI.\n"
        "metadata:\n  vbot:\n    requirements:\n      env: OPENAI_API_KEY\n---\n\n"
        "# OpenAI Helper\n",
    )
    registry = SkillRegistry.load(tmp_path / "skills", environment={})

    result = SkillTool(tmp_path, registry).call({"name": "openai-helper"})

    assert result == tool_failure(
        "skill_unavailable",
        "Skill 'openai-helper' is unavailable: missing environment variable 'OPENAI_API_KEY'",
    )


def test_already_active_skill_is_not_loaded_again(tmp_path: Path) -> None:
    tool = debugging_tool(tmp_path)
    result = tool.call({"name": "debugging"}, activation_hook=ActivationRecorder(accept=False))

    assert result["ok"] is True
    assert result["data"] == {
        "name": "debugging",
        "status": "already_active",
        "message": (
            "Skill 'debugging' is already active in this session; "
            "its instructions are already in context."
        ),
    }
    assert tool.details({"name": "debugging"}, result) == [
        {
            "type": "notice",
            "level": "info",
            "text": "The Skill was already active; it was not loaded again.",
        }
    ]


@pytest.mark.parametrize(
    ("arguments", "file_path"),
    [
        ({"name": "debugging", "file_path": "references/guide.md"}, "references/guide.md"),
        ({"name": "debugging", "file_path": "scripts/run.py"}, "scripts/run.py"),
        # The absolute script path from the activation's resource list.
        ({"name": "debugging", "file_path": "{directory}/scripts/run.py"}, "scripts/run.py"),
        ({"name": "debugging/references/guide.md"}, "references/guide.md"),
        ({"file_path": "debugging/references/guide.md"}, "references/guide.md"),
        (
            {"name": "debugging", "file_path": "debugging/references/guide.md"},
            "references/guide.md",
        ),
        ({"name": "debugging", "file_path": "./references/guide.md"}, "references/guide.md"),
        ({"skill": "debugging", "path": "references\\guide.md"}, "references/guide.md"),
    ],
)
def test_file_reads_return_the_named_file_without_activation(
    tmp_path: Path, arguments: dict[str, str], file_path: str
) -> None:
    tool = debugging_tool(tmp_path)
    directory = debugging_directory(tmp_path)
    recorder = ActivationRecorder()
    call: dict[str, object] = {
        key: value.format(directory=directory) for key, value in arguments.items()
    }

    result = tool.call(call, activation_hook=recorder)

    assert result["data"] == {
        "name": "debugging",
        "status": "file_loaded",
        "file_path": file_path,
        "content": {"references/guide.md": GUIDE, "scripts/run.py": SCRIPT}[file_path],
    }
    assert recorder.activations == {}
    assert directory not in str(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "Debugging"},
        {"name": "/debugging"},
        {"name": "$debugging"},
        {"name": "debugging/SKILL.md"},
        {"name": "skills/debugging"},
        {"skill": "debugging"},
        {"command": "/debugging"},
    ],
)
def test_skill_names_differing_only_in_form_load_the_skill(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    result = debugging_tool(tmp_path).call(arguments)

    assert (result["data"]["name"], result["data"]["status"]) == ("debugging", "loaded")
    assert "note" not in result["data"]


def test_skill_arguments_from_another_harness_are_noted(tmp_path: Path) -> None:
    result = debugging_tool(tmp_path).call({"skill": "debugging", "args": "the flaky test"})

    assert result["data"]["status"] == "loaded"
    assert result["data"]["note"] == (
        "Skills take no arguments; apply the loaded instructions to them yourself."
    )


def test_contradictory_skill_addresses_are_refused(tmp_path: Path) -> None:
    tool = debugging_tool(tmp_path)

    with pytest.raises(ToolContractError, match="Conflicting values for name"):
        tool.call({"name": "debugging", "skill": "other"})
    result = tool.call(
        {"name": "debugging/references/guide.md", "file_path": "assets/checklist.txt"}
    )

    assert result == tool_failure(
        "invalid_arguments",
        "name points to file 'references/guide.md' but file_path is 'assets/checklist.txt'; "
        'call skill with name "debugging" and the one file_path you mean.',
    )


@pytest.mark.parametrize(
    ("arguments", "allowed_skills", "code", "message"),
    [
        (
            {"file_path": "references/guide.md"},
            None,
            "invalid_arguments",
            "file_path needs the Skill's name: call skill with name and file_path.",
        ),
        (
            {"name": "debugging", "file_path": "references/missing.md"},
            None,
            "skill_read_error",
            "Skill 'debugging' file not found: references/missing.md",
        ),
        (
            {"name": "cdebugging"},
            None,
            "skill_not_found",
            'Skill not found: cdebugging. Did you mean "debugging"? Load it with name "debugging".',
        ),
        (
            {"name": "debug"},
            None,
            "skill_not_found",
            'Skill not found: debug. Did you mean "debugging"? Load it with name "debugging".',
        ),
        (
            {"name": "zzz"},
            None,
            "skill_not_found",
            "Skill not found: zzz. Available Skills: debugging.",
        ),
        # Disallowed Skills are neither resolved nor suggested.
        (
            {"name": "Debugging"},
            [],
            "skill_not_found",
            "Skill not found: Debugging. No Skills are available to you.",
        ),
    ],
)
def test_failed_lookups_explain_the_miss_without_activation(
    tmp_path: Path,
    arguments: dict[str, object],
    allowed_skills: list[str] | None,
    code: str,
    message: str,
) -> None:
    recorder = ActivationRecorder()

    result = debugging_tool(tmp_path).call(
        arguments, activation_hook=recorder, allowed_skills=allowed_skills
    )

    assert result == tool_failure(code, message)
    assert recorder.activations == {}


def test_names_matching_several_skills_in_form_are_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for directory, name in (("one", "deploy-app"), ("two", "deploy_app")):
        write_skill(root, directory, f"---\nname: {name}\ndescription: Deploy.\n---\n\nBody.\n")

    result = SkillTool(tmp_path, SkillRegistry.load(root)).call({"name": "Deploy-App"})

    assert result == tool_failure(
        "skill_not_found",
        'Skill not found: Deploy-App. Did you mean one of: "deploy-app", "deploy_app"?',
    )


@pytest.mark.parametrize(
    "relative",
    [
        "../outside",
        "/absolute",
        "references/../../outside",
        "C:/outside",
        ".vbot-install.json",
        ".git/config",
        "templates/NUL.txt",
    ],
)
def test_file_reads_stay_inside_the_package_and_skip_internal_files(
    tmp_path: Path, relative: str
) -> None:
    result = debugging_tool(tmp_path).call({"name": "debugging", "file_path": relative})

    assert result["ok"] is False


def test_skill_document_removed_after_loading_is_a_read_error(tmp_path: Path) -> None:
    skills_dir = debugging_skills(tmp_path)
    tool = SkillTool(tmp_path, SkillRegistry.load(skills_dir))
    (skills_dir / "debugging" / "SKILL.md").unlink()

    result = tool.call({"name": "debugging"})

    assert (result["ok"], result["error"]["code"]) == (False, "skill_read_error")


@pytest.mark.parametrize("arguments", [{}, {"name": "  "}])
def test_call_without_a_name_lists_the_live_grouped_catalog(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    write_skill(tmp_path / "agent", "mine", "---\nname: mine\ndescription: Mine.\n---\n\nBody.\n")
    registry = SkillRegistry.load(
        tmp_path / "agent", extra_dirs=[debugging_skills(tmp_path)], origins=["agent", "global"]
    )
    tool = SkillTool(tmp_path, registry)

    result = tool.call(arguments)

    # Same groups, order and labels as the System Prompt catalog.
    assert result["data"] == {
        "count": 2,
        "content": (
            "Your global skills:\n- debugging: Debug failures.\nYour own skills:\n- mine: Mine."
        ),
    }
    display = tool.tools.display_for_call(SKILL_TOOL_NAME, arguments, result=result)
    assert display["facts"] == [{"kind": "count", "value": 2, "unit": "results", "at_least": False}]


def test_agent_own_skill_loads_despite_an_empty_allowlist(tmp_path: Path) -> None:
    write_skill(
        tmp_path / "agent-skills",
        "private",
        "---\nname: private\ndescription: Agent only.\n---\n\nSecret steps.\n",
    )
    registry = SkillRegistry.load(tmp_path / "agent-skills", always_allowed=frozenset({"private"}))

    result = SkillTool(tmp_path, registry).call({"name": "private"}, allowed_skills=[])

    assert result["data"]["name"] == "private"


@pytest.mark.parametrize("name", ["Daily Review", "my.skill", "ümlaut", "long-" * 20])
def test_loaded_nontriggerable_name_is_addressable_through_dispatch(
    tmp_path: Path, name: str
) -> None:
    package = tmp_path / "skills" / "package"
    write_skill(
        package.parent,
        "package",
        f"---\nname: {name}\ndescription: A test Skill.\n---\nUse the sentinel procedure.",
    )
    (package / "reference.txt").write_text("Reference sentinel", encoding="utf-8")
    tool = SkillTool(tmp_path, SkillRegistry.load(package.parent))

    activated = tool.call({"name": name})
    reference = tool.call({"name": name, "file_path": "reference.txt"})
    denied = tool.call({"name": name}, allowed_skills=[])

    assert activated["data"]["name"] == name
    assert activated["data"]["content"] == "Use the sentinel procedure."
    assert reference["data"]["content"] == "Reference sentinel"
    assert denied["ok"] is False


def test_installed_package_exposes_nonconventional_resources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "templates").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: imported\ndescription: Imported guide\n---\n"
        "Read GUIDE.md and templates/example.md.\n"
    )
    (source / "GUIDE.md").write_text("The complete guide.")
    (source / "templates/example.md").write_text("Example text.")
    SkillAuthoringService().install(tmp_path / "skills", str(source))
    tool = SkillTool(tmp_path, SkillRegistry.load(tmp_path / "skills"))

    activated = tool.call({"name": "imported"})

    assert activated["data"]["resource_files"]["files"] == ["GUIDE.md", "templates/example.md"]
    for relative, text in [
        ("GUIDE.md", "The complete guide."),
        ("templates/example.md", "Example text."),
    ]:
        result = tool.call({"name": "imported", "file_path": relative})
        assert (result["data"]["status"], result["data"]["content"]) == ("file_loaded", text)


def test_load_skill_content_resolves_base_dir_and_lists_package_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "deploy"
    files = [
        "scripts/ship.py",
        "scripts/nested/helper.py",
        "scripts/__pycache__/ship.pyc",
        "references/guide.md",
        "assets/template.html",
        "notes/guide.md",
    ]
    for relative in files:
        (skill_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (skill_dir / relative).write_text("", encoding="utf-8")
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: deploy\ndescription: Ship it.\n---\n\n"
        "Run `python {baseDir}/scripts/ship.py` to deploy.\n",
        encoding="utf-8",
    )

    result = load_skill_content("deploy", skill_file)

    directory = skill_dir.resolve().as_posix()
    assert result["content"] == f"Run `python {directory}/scripts/ship.py` to deploy."
    assert result["resource_files"]["files"] == [
        f"{directory}/scripts/nested/helper.py",
        f"{directory}/scripts/ship.py",
        "references/guide.md",
        "assets/template.html",
        "notes/guide.md",
    ]


def test_load_skill_content_keeps_the_wrapper_out_of_the_content(tmp_path: Path) -> None:
    # Without front matter the whole document is the instructions.
    skill_file = tmp_path / "skills" / "unsafe" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# Deploy\n\nRun the deploy steps.\n", encoding="utf-8")

    result: dict[str, Any] = load_skill_content('bad" name><tag', skill_file)

    assert result["content"] == "# Deploy\n\nRun the deploy steps."
    assert "resource_files" not in result
    assert "environment_access" not in result
    assert result["activation_content"] == (
        '<skill_content name="bad&quot; name&gt;&lt;tag">\n'
        "# Deploy\n\nRun the deploy steps.\n</skill_content>"
    )
