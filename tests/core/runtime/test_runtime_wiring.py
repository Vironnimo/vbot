"""What a normally started Runtime wires: services, Tools, Skills, prompts, data directory."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from core.agents.agents import AgentStore
from core.automation import CronService
from core.chat import ChatLoop
from core.model_tasks import EmbeddingService
from core.model_tasks.live import LiveVoiceService
from core.models.models import ModelRegistry
from core.prompts import LayoutEntry, SystemPromptManager
from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import ProviderRegistry
from core.runtime._configuration import _VBOT_ROOT
from core.runtime.runtime import Runtime
from core.sessions import ChatSessionManager
from core.skills.skills import SkillRegistry
from core.statistics import StatisticsIndex
from core.storage.layout import DATA_DIRECTORY_RELATIVE_PATHS
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import ToolNotFoundError, ToolRegistry
from core.tools.update_handoff import UpdateHandoffs
from core.utils.version import detect_vbot_version

CANONICAL_BUILTIN_TOOLS = [
    "analyze_image",
    "apply_patch",
    "bash",
    "calendar",
    "classify",
    "cron",
    "edit",
    "generate_music",
    "generate_video",
    "image_generation",
    "memory",
    "project",
    "read",
    "search_files",
    "session_search",
    "skill",
    "skill_manage",
    "status",
    "subagent",
    "terminal",
    "text_to_speech",
    "web_fetch",
    "web_search",
    "write",
]

# The four Home Assistant Tools ship as a bundled Extension and are always
# registered (readiness only hides them from model-facing surfaces until the
# token is set), so they are part of the registered inventory even without a
# token, but absent from provider definitions, which filter on readiness.
HOME_ASSISTANT_TOOLS = ["ha_call_service", "ha_get_state", "ha_list_entities", "ha_list_services"]

COMPUTER_USE_TOOLS = ["computer", "computer_apps", "computer_batch"]

# Built-in Tools that only a Session grant offers; they stay out of the catalog.
SESSION_GRANTED_BUILTIN_TOOLS = ["message_parent"]
CANONICAL_REGISTERED_TOOLS = sorted(
    CANONICAL_BUILTIN_TOOLS
    + SESSION_GRANTED_BUILTIN_TOOLS
    + HOME_ASSISTANT_TOOLS
    + COMPUTER_USE_TOOLS
)

BUNDLED_SKILLS = [
    "coding-agents",
    "computer-use",
    "free-models",
    "home-assistant",
    "pdf",
    "playwright-cli",
    "skill-writing",
    "vbot-docs",
    "weather",
]


def _declared_hidden_session_tools(runtime: Runtime) -> set[str]:
    registry = runtime.extensions
    assert registry is not None
    return {
        declaration.name
        for record in registry.records()
        if record.status == "loaded"
        for declaration in record.declarations.tools
        if declaration.session_scoped
    }


def test_start_registers_each_builtin_tool_once_with_a_result_contract(
    shared_runtime: Runtime,
) -> None:
    tools = shared_runtime.tools
    hidden_session_tools = _declared_hidden_session_tools(shared_runtime)
    registered = [tool.name for tool in tools.list_tools()]

    assert [name for name in registered if name not in hidden_session_tools] == (
        CANONICAL_REGISTERED_TOOLS
    )
    assert hidden_session_tools <= set(registered)
    # Session-scoped Tools stay out of the catalog.
    assert not (hidden_session_tools | set(SESSION_GRANTED_BUILTIN_TOOLS)) & {
        tool.name for tool in tools.list_tools(include_catalog_hidden=False)
    }
    for tool in tools.list_tools():
        if tool.name in hidden_session_tools:
            continue
        assert tool.result_schema is not None
        assert len(tool.contract.schema_fingerprint) == 64


def test_builtin_provider_definitions_expose_model_visible_metadata_only(
    shared_runtime: Runtime,
) -> None:
    definitions = {
        definition["name"]: definition for definition in shared_runtime.tools.provider_definitions()
    }

    assert sorted(definitions) == [name for name in CANONICAL_BUILTIN_TOOLS if name != "classify"]
    for tool_name, definition in definitions.items():
        tool = shared_runtime.tools.get(tool_name)
        assert definition == {
            "name": tool_name,
            "description": tool.description,
            "parameters": tool.parameters,
        }


def test_runtime_wires_its_services_to_the_data_directory(shared_runtime: Runtime) -> None:
    runtime = shared_runtime
    for name, service_type in (
        ("storage", StorageManager),
        ("agents", AgentStore),
        ("providers", ProviderRegistry),
        ("models", ModelRegistry),
        ("provider_credentials", ProviderCredentialResolver),
        ("tools", ToolRegistry),
        ("update_handoffs", UpdateHandoffs),
        ("terminal_manager", TerminalManager),
        ("skills", SkillRegistry),
        ("chat_sessions", ChatSessionManager),
        ("statistics_index", StatisticsIndex),
        ("system_prompts", SystemPromptManager),
        ("subagents", SubAgentCoordinator),
        ("embeddings", EmbeddingService),
        ("live_voice", LiveVoiceService),
        ("cron_service", CronService),
        ("chat_loop", ChatLoop),
    ):
        assert isinstance(getattr(runtime, name), service_type), name
    assert runtime.agents.data_dir == runtime.storage.data_dir
    assert runtime.statistics_index.data_dir == runtime.storage.data_dir
    # Image understanding availability is answered by the Runtime's image service.
    availability = runtime.chat_loop._dependencies.image_understanding_available  # noqa: SLF001
    assert getattr(availability, "__self__", None) is runtime._image  # noqa: SLF001


def test_start_prepares_the_canonical_data_directory(shared_runtime: Runtime) -> None:
    data_dir = shared_runtime.storage.data_dir
    for directory_name in DATA_DIRECTORY_RELATIVE_PATHS:
        assert (data_dir / directory_name).is_dir()
    assert (data_dir / ".env").is_file()
    assert (data_dir / "settings.json").is_file()
    assert shared_runtime.storage.layout.sessions_db_path.is_file()
    for legacy_name in (
        ".tmp",
        "attachments",
        "images",
        "speech",
        "models",
        "debug",
        "temp",
        "provider-usage",
    ):
        assert not (data_dir / legacy_name).exists()
    # Startup must NOT seed fragment copies into the data dir: a seeded copy would
    # shadow the bundled resource forever and freeze prompt defaults at first-run
    # state. Bundled fragments are read live; only a hand-created copy overrides.
    assert not (data_dir / "prompts" / "runtime.md").exists()


def test_start_bootstraps_main_agent_when_data_dir_is_empty(shared_runtime: Runtime) -> None:
    agents = shared_runtime.agents.list()
    assert [agent.id for agent in agents] == ["main"]
    main_agent = agents[0]
    assert main_agent.name == "Main"
    # The bootstrap creates no Session; the first message to main creates one.
    assert main_agent.current_session_id == ""


def test_start_creates_the_hidden_librarian_that_can_only_maintain_skills(
    shared_runtime: Runtime,
) -> None:
    librarian = shared_runtime.agents.librarian()

    assert librarian is not None and librarian.name == "Librarian"
    prompts = shared_runtime.system_prompts
    assert [definition["name"] for definition in prompts.provider_tool_definitions(librarian)] == [
        "skill",
        "skill_manage",
    ]
    # It is in no other Agent's System Prompt or Tool definitions (the checkout path aside).
    main = shared_runtime.agents.get("main")
    prompt = prompts.build_system_prompt(main).replace(_VBOT_ROOT.as_posix(), "<vbot root>")
    assert "librarian" not in prompt.casefold()
    assert "librarian" not in str(prompts.provider_tool_definitions(main)).casefold()


def test_start_loads_the_bundled_skills_without_diagnostics(shared_runtime: Runtime) -> None:
    assert [skill.name for skill in shared_runtime.skills.list_all()] == BUNDLED_SKILLS
    assert shared_runtime.skills.invalid_diagnostics() == []


def test_playwright_replaces_archived_browser_extension(shared_runtime: Runtime) -> None:
    skill = shared_runtime.skills.get("playwright-cli")
    assert (
        skill.path.parent == Path(__file__).resolve().parents[3] / "resources/skills/playwright-cli"
    )
    assert skill.requirements.empty
    assert shared_runtime.extensions is not None
    assert not any(record.name == "browser_use" for record in shared_runtime.extensions.records())
    with pytest.raises(KeyError):
        shared_runtime.skills.get("browser-use")
    with pytest.raises(ToolNotFoundError):
        shared_runtime.tools.get("browser")


def test_system_prompt_reports_the_single_source_vbot_version(shared_runtime: Runtime) -> None:
    with (_VBOT_ROOT / "pyproject.toml").open("rb") as handle:
        expected = tomllib.load(handle)["project"]["version"]

    assert detect_vbot_version() == expected
    prompt = shared_runtime.system_prompts.build_system_prompt(shared_runtime.agents.get("main"))
    assert f"vBot version: `{expected}`" in prompt


def test_system_prompt_blocks_persist_through_the_runtime_storage(runtime: Runtime) -> None:
    """The prompt manager reads and writes blocks through the real StorageManager.

    Default-scope writes use the storage scope ``None``; each write shapes the
    very next build, so the manager is not on an empty fallback block store.
    """
    manager = runtime.system_prompts
    agent = runtime.agents.get("main")
    baseline = manager.build_system_prompt(agent)
    assert "## Tool Call Style" in baseline
    assert "## Available Skills" in baseline

    runtime.storage.write_block_override(
        None, "core:tools", "## PERSISTED-OVERRIDE-MARKER\n{generated:tool_list}"
    )
    runtime.storage.write_block_layout(
        None,
        [
            LayoutEntry(id="core:runtime", enabled=True, source="core"),
            LayoutEntry(id="core:tools", enabled=True, source="core"),
            LayoutEntry(id="core:skills", enabled=False, source="core"),
        ],
    )
    persisted = manager.build_system_prompt(agent)
    assert "## PERSISTED-OVERRIDE-MARKER" in persisted
    assert "## Tool Call Style" not in persisted
    assert "## Available Skills" not in persisted

    manager.update_block("core:tools", "## FACADE-TOOLS-MARKER\n{generated:tool_list}")
    assert runtime.storage.read_block_override(None, "core:tools") == (
        "## FACADE-TOOLS-MARKER\n{generated:tool_list}"
    )
    assert "## FACADE-TOOLS-MARKER" in manager.build_system_prompt(agent)
    # An id whose contributor is gone is pruned, not an error.
    manager.set_layout(
        [
            {"id": "core:runtime", "enabled": True},
            {"id": "core:tools", "enabled": True},
            {"id": "core:skills", "enabled": True},
            {"id": "extension:gone", "enabled": True},
        ]
    )
    assert "extension:gone" not in {entry.id for entry in runtime.storage.read_block_layout(None)}
    assert "## Available Skills" in manager.build_system_prompt(agent)

    manager.create_block("greeting", "Hello from a custom block.")
    assert runtime.storage.read_block_override(None, "user:greeting") is not None
    assert "Hello from a custom block." in manager.build_system_prompt(agent)
    manager.remove_block("user:greeting")
    assert runtime.storage.read_block_override(None, "user:greeting") is None
    assert "Hello from a custom block." not in manager.build_system_prompt(agent)
