"""What a normally started Runtime wires: services, Tools, Skills, prompts, data directory."""

from __future__ import annotations

import asyncio
import os
import threading
import tomllib
from pathlib import Path
from typing import Any, cast

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
from core.runs import Run
from core.runtime._configuration import _VBOT_ROOT
from core.runtime.runtime import Runtime
from core.sessions import ChatSessionManager, SessionAddress
from core.skills.skills import SkillRegistry
from core.statistics import StatisticsIndex
from core.storage.layout import DATA_DIRECTORY_RELATIVE_PATHS
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.subagents.links import link_session
from core.tools.command_manager import CommandManager
from core.tools.live import LIVE_TOOL_NAMES
from core.tools.scratch_areas import ScratchAreas, ScratchFolder, ScratchUnavailableError
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import ToolNotFoundError, ToolRegistry
from core.tools.update_handoff import UpdateHandoffs
from core.utils.version import detect_vbot_version

CANONICAL_BUILTIN_TOOLS = [
    "analyze_image",
    "apply_patch",
    "background_tasks",
    "bash",
    "calendar",
    "classify",
    "cron",
    "edit",
    "generate_image",
    "generate_music",
    "generate_speech",
    "generate_video",
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

# Built-in Tools that only a Session grant or a Live call offers; they stay out of the catalog.
SESSION_GRANTED_BUILTIN_TOOLS = ["message_parent", "vbot_request"]
CANONICAL_REGISTERED_TOOLS = sorted(
    CANONICAL_BUILTIN_TOOLS
    + list(LIVE_TOOL_NAMES)
    + SESSION_GRANTED_BUILTIN_TOOLS
    + HOME_ASSISTANT_TOOLS
    + COMPUTER_USE_TOOLS
)

# Tools whose description opens with a sentence as short as a summary. Every
# other Tool an Agent can load on demand declares a summary.
FIRST_SENTENCE_SUMMARY_TOOLS = {"apply_patch", "background_tasks", "edit", "write"}

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


def test_loadable_tools_declare_a_summary_unless_their_first_sentence_serves(
    shared_runtime: Runtime,
) -> None:
    # Live Tools serve only the built-in Live Agents, which never load Tools on
    # demand; Session-granted Tools are always loaded.
    never_on_demand = (
        set(LIVE_TOOL_NAMES)
        | set(SESSION_GRANTED_BUILTIN_TOOLS)
        | _declared_hidden_session_tools(shared_runtime)
    )
    tools = [tool for tool in shared_runtime.tools.list_tools() if tool.name not in never_on_demand]

    assert sorted(tool.name for tool in tools if tool.summary is None) == sorted(
        FIRST_SENTENCE_SUMMARY_TOOLS
    )


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
        ("command_manager", CommandManager),
        ("skills", SkillRegistry),
        ("chat_sessions", ChatSessionManager),
        ("statistics_index", StatisticsIndex),
        ("system_prompts", SystemPromptManager),
        ("subagents", SubAgentCoordinator),
        ("embeddings", EmbeddingService),
        ("live_voice", LiveVoiceService),
        ("cron_service", CronService),
        ("chat_loop", ChatLoop),
        ("scratch_areas", ScratchAreas),
    ):
        assert isinstance(getattr(runtime, name), service_type), name
    assert runtime.agents.data_dir == runtime.storage.data_dir
    assert runtime.statistics_index.data_dir == runtime.storage.data_dir
    assert runtime.chat_loop._dependencies.scratch_areas is runtime.scratch_areas  # noqa: SLF001
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


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["run", "queue", "command", "terminal"])
async def test_scratch_retention_observes_each_live_resource_owner(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    session = runtime.chat_sessions.create("main")
    address = session.address
    folder = await runtime.scratch_areas.folder_for(address)
    marker = runtime.scratch_areas.state_root / folder.area.name / ".activity"
    os.utime(marker, (1, 1))
    run = Run(run_id="run_scratch", agent_id="main", session_id=address.session_id)
    monkeypatch.setattr(runtime.chat_runs, "active_runs", lambda: [run] if source == "run" else [])
    monkeypatch.setattr(
        runtime.chat_runs,
        "all_queued",
        lambda: [(address, cast(Any, None))] if source == "queue" else [],
    )
    for name, manager in (
        ("command", runtime.command_manager),
        ("terminal", runtime.terminal_manager),
    ):
        active: set[SessionAddress] = {address} if source == name else set()
        monkeypatch.setattr(manager, "busy_addresses", lambda active=active: active)

    assert await runtime.scratch_areas.sweep() == 0
    assert folder.own.is_dir()
    assert marker.stat().st_mtime > 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", ["parent", "child"])
async def test_scratch_retention_keeps_busy_tree_when_ancestry_is_unavailable(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    sessions = runtime.chat_sessions
    parent = sessions.create("main", "parent").address
    child = sessions.create("main", "child").address
    link_session(
        sessions,
        child,
        subagent_id="sub_scratch",
        parent=parent,
        run_id="run_parent",
        tool_call_id=None,
        tool_call_index=None,
    )
    folder = await runtime.scratch_areas.folder_for(child)
    marker = runtime.scratch_areas.state_root / folder.area.name / ".activity"
    os.utime(marker, (1, 1))
    run = Run(run_id="run_child", agent_id="main", session_id=child.session_id)
    monkeypatch.setattr(runtime.chat_runs, "active_runs", lambda: [run])
    original = sessions.get_metadata

    def unavailable_metadata(address: SessionAddress) -> dict[str, Any]:
        if address.session_id == unreadable:
            raise PermissionError("Session metadata unavailable")
        return original(address)

    with monkeypatch.context() as unavailable:
        unavailable.setattr(sessions, "get_metadata", unavailable_metadata)
        with pytest.raises(PermissionError):
            await runtime.scratch_areas.sweep()
        with pytest.raises(ScratchUnavailableError):
            await runtime.scratch_areas.folder_for(child)
        assert folder.own.is_dir()
        assert marker.stat().st_mtime == 1

    assert await runtime.scratch_areas.sweep() == 0
    assert marker.stat().st_mtime > 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_retirement", "during_rename"])
@pytest.mark.parametrize("starting_session", ["same", "child", "unrelated"])
async def test_scratch_retention_preserves_files_when_a_run_starts_during_sweep(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, phase: str, starting_session: str
) -> None:
    manager = runtime.chat_runs
    assert manager is not None
    address = runtime.chat_sessions.create("main").address
    admitted = address
    if starting_session != "same":
        admitted = runtime.chat_sessions.create("main").address
        if starting_session == "child":
            link_session(
                runtime.chat_sessions,
                admitted,
                subagent_id="sub_racing_sweep",
                parent=address,
                run_id="run_parent",
                tool_call_id=None,
                tool_call_index=None,
            )
    scratch = runtime.scratch_areas
    folder = await scratch.folder_for(address)
    payload = folder.own / "input.txt"
    payload.write_text("unfinished work", encoding="utf-8")
    marker = scratch.state_root / folder.area.name / ".activity"
    os.utime(marker, (1, 1))
    loop = asyncio.get_running_loop()
    paused = asyncio.Event()
    release_sweep = threading.Event()
    release_run = asyncio.Event()
    active_folder: asyncio.Future[ScratchFolder] = loop.create_future()

    def pause() -> None:
        loop.call_soon_threadsafe(paused.set)
        assert release_sweep.wait(2)

    if phase == "before_retirement":
        original_root_check = scratch._has_root

        def paused_root_check() -> bool:
            pause()
            return original_root_check()

        monkeypatch.setattr(scratch, "_has_root", paused_root_check)
    else:
        original_rename = Path.rename

        def paused_rename(path: Path, target: str | os.PathLike[str]) -> Path:
            renamed = original_rename(path, target)
            if path == folder.area:
                pause()
            return renamed

        monkeypatch.setattr(Path, "rename", paused_rename)

    async def executor(_run: Run) -> None:
        await release_run.wait()
        active_folder.set_result(await scratch.folder_for(admitted))

    sweep = asyncio.create_task(scratch.sweep())
    run: Run | None = None
    try:
        await paused.wait()
        run = await manager.start(admitted, executor)
        release_sweep.set()
        assert await sweep == (1 if starting_session == "unrelated" else 0)
        if starting_session != "unrelated":
            # Admission alone refreshes retention, before the Run calls any Tool.
            assert marker.stat().st_mtime > 1
        release_run.set()
        current = await active_folder
        if starting_session == "unrelated":
            assert not payload.exists()
            assert current.area != folder.area
        else:
            assert current.area == folder.area
            assert payload.read_text(encoding="utf-8") == "unfinished work"
            if starting_session == "same":
                assert current.incarnation == folder.incarnation
        assert not any(entry.name.startswith(".trash-") for entry in scratch.root.iterdir())
    finally:
        release_sweep.set()
        release_run.set()
        await sweep
        if run is not None:
            await run.wait()


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
