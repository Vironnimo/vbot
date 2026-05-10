"""Tests for system prompt assembly."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.agents.agents import Agent, AgentError, SkillPromptMetadata, SystemPromptManager


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str


class StubStorage:
    def __init__(self, fragments: dict[str, str]) -> None:
        self._fragments = fragments

    def read_prompt_fragment(self, fragment_name: str) -> str:
        return self._fragments[fragment_name]


class StubTools:
    def __init__(self) -> None:
        self.prompt_allowlist: list[str] | None = None
        self.provider_allowlist: list[str] | None = None

    def prompt_definitions(
        self, allowed_tools: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        self.prompt_allowlist = list(allowed_tools) if allowed_tools is not None else None
        tools = [
            {"name": "read_file", "description": "Read a workspace file"},
            {"name": "shell", "description": "Run a shell command"},
        ]
        return _filter_by_allowlist(tools, allowed_tools)

    def provider_definitions(
        self, allowed_tools: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        self.provider_allowlist = list(allowed_tools) if allowed_tools is not None else None
        tools = [
            {
                "name": "read_file",
                "description": "Read a workspace file",
                "parameters": {"type": "object"},
            },
            {
                "name": "shell",
                "description": "Run a shell command",
                "parameters": {"type": "object"},
            },
        ]
        return _filter_by_allowlist(tools, allowed_tools)


class StubSkills:
    def __init__(self, skills: list[StubSkill]) -> None:
        self._skills = skills
        self.allowlist: list[str] | None = None

    def filter_allowed(self, allowed_skills: list[str]) -> list[SkillPromptMetadata]:
        self.allowlist = allowed_skills
        if "*" in allowed_skills:
            return list(self._skills)
        return [skill for skill in self._skills if skill.name in allowed_skills]


@pytest.fixture
def fragments() -> dict[str, str]:
    return {
        "system.md": (
            "App {app_version}\n"
            "{runtime}\n"
            "{tools}\n"
            "{skills}\n"
            "{include:SOUL.md}\n"
            "{include:IDENTITY.md}\n"
            "{include:AGENTS.md}\n"
            "{include:USER.md}"
        ),
        "runtime.md": (
            "## Runtime\n"
            "- Host: {host}\n"
            "- OS: {os}\n"
            "- You are powered by the model {model}\n"
            "- Your Workspace (HOME, your CWD for tools, where you and your files live): "
            "{agent_workspace}\n"
            "- App Path: {app_dir}\n"
            "- Data Path: All app data (sessions, workspaces, skills, configs, etc.) "
            "lives here: {data_root}\n"
            "- Thinking level: {thinking_effort}\n"
            "- Date: {current_date}"
        ),
        "tools.md": "## Available Tools\n{tool_list}",
        "skills.md": "## Available Skills\n{skill_list}",
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    (directory / "SOUL.md").write_text("Soul text", encoding="utf-8")
    (directory / "IDENTITY.md").write_text("Identity text", encoding="utf-8")
    (directory / "AGENTS.md").write_text("Agents text", encoding="utf-8")
    (directory / "USER.md").write_text("User text", encoding="utf-8")
    return directory


def test_build_system_prompt_replaces_all_placeholders_and_includes_workspace_files(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    skills = StubSkills(
        [
            StubSkill("agent-cli", "Delegate coding tasks"),
            StubSkill("news", "Fetch news"),
        ]
    )
    manager = SystemPromptManager(
        StubStorage(fragments),
        tools,
        skills,
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host="test-host",
        os_name="test-os",
        current_date=lambda: "2026-05-04",
    )
    agent = _agent(
        workspace,
        allowed_tools=["read_file"],
        allowed_skills=["agent-cli"],
    )

    prompt = manager.build_system_prompt(agent)

    assert "App 0.1.0" in prompt
    assert "- Host: test-host" in prompt
    assert "- OS: test-os" in prompt
    assert "- You are powered by the model openai/gpt-5.2" in prompt
    assert f"{workspace}" in prompt
    assert str((tmp_path / "app").resolve()) in prompt
    assert str((tmp_path / "data").resolve()) in prompt
    assert "- Thinking level: high" in prompt
    assert "- Date: 2026-05-04" in prompt
    assert "- read_file: Read a workspace file" in prompt
    assert "shell" not in prompt
    assert "<name>agent-cli</name>" in prompt
    assert "<description>Delegate coding tasks</description>" in prompt
    assert "<path>" not in prompt
    assert "<location>" not in prompt
    assert "news" not in prompt
    assert "Soul text" in prompt
    assert "Identity text" in prompt
    assert "Agents text" in prompt
    assert "User text" in prompt
    assert "{" not in prompt
    assert tools.prompt_allowlist == ["read_file"]
    assert skills.allowlist == ["agent-cli"]


def test_provider_tool_definitions_use_same_agent_allowlist(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    manager = SystemPromptManager(
        StubStorage(fragments),
        tools,
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, allowed_tools=["read_file"])

    definitions = manager.provider_tool_definitions(agent)

    assert definitions == [
        {
            "name": "read_file",
            "description": "Read a workspace file",
            "parameters": {"type": "object"},
        }
    ]
    assert tools.provider_allowlist == ["read_file"]


def test_empty_tool_and_skill_allowlists_emit_empty_sections(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([StubSkill("agent-cli", "Delegate coding tasks")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        current_date=lambda: "2026-05-04",
    )
    agent = _agent(workspace, allowed_tools=[], allowed_skills=[])

    prompt = manager.build_system_prompt(agent)

    assert "- read_file" not in prompt
    assert "<available_skills>\n</available_skills>" in prompt
    assert "agent-cli" not in prompt


def test_skill_xml_escapes_metadata(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([StubSkill("a&b", "Use <danger>")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, allowed_skills=["*"])

    prompt = manager.build_system_prompt(agent)

    assert "<name>a&amp;b</name>" in prompt
    assert "<description>Use &lt;danger&gt;</description>" in prompt


def test_unsafe_workspace_include_raises_error(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    fragments["system.md"] = "{include:../secret.md}"
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    with pytest.raises(AgentError, match="Unsafe workspace include"):
        manager.build_system_prompt(_agent(workspace))


def _agent(
    workspace: Path,
    *,
    allowed_tools: list[str] | None = None,
    allowed_skills: list[str] | None = None,
) -> Agent:
    return Agent(
        id="coder",
        name="Coder Agent",
        model="openai/gpt-5.2",
        fallback_model="",
        workspace=str(workspace),
        temperature=0.1,
        thinking_effort="high",
        allowed_tools=["*"] if allowed_tools is None else allowed_tools,
        allowed_skills=["*"] if allowed_skills is None else allowed_skills,
        created_at="2026-05-03T12:00:00Z",
        updated_at="2026-05-03T12:00:00Z",
    )


def _filter_by_allowlist(
    definitions: list[dict[str, Any]],
    allowlist: Sequence[str] | None,
) -> list[dict[str, Any]]:
    if allowlist is None or "*" in allowlist:
        return definitions
    return [definition for definition in definitions if definition["name"] in allowlist]
