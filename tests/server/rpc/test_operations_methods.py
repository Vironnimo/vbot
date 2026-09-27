"""Tests for the operations RPC handlers: logs, the prompt block editor and preview.

The prompt handlers run against a real :class:`SystemPromptManager` wired with an
in-memory block store and Agent store. The RPC edge validates, maps errors and keeps
prompt work off the Event Loop; the block logic itself lives in the manager.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.projects import ResolutionAgentNotFoundError
from core.projects.resolver import ConfigAgent
from core.prompts import (
    BlockDefinition,
    LayoutEntry,
    PromptAgentStore,
    SystemPromptManager,
)
from core.tools import ToolAccess, ToolRegistry
from core.utils.paths import model_path
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.methods import dispatch_rpc
from server.rpc.operations_methods import (
    _create_prompt_block,
    _list_prompts,
    _preview_prompt,
    _remove_prompt_block,
    _reset_prompt,
    _reset_prompt_layout,
    _set_prompt_layout,
    _update_prompt,
)

JsonObject = dict[str, Any]


# --- In-memory stubs ---------------------------------------------------------


@dataclass(frozen=True)
class StubAgent:
    id: str
    name: str
    model: str = "openai/gpt-5"
    workspace: str = ""
    root_project_id: str | None = None
    thinking_effort: str | None = "high"
    memory_prompt_mode: str = "off"
    tool_access: ToolAccess = ToolAccess(mode="all")
    allowed_skills: tuple[str, ...] = ()
    custom_system_prompt_enabled: bool = False


class StubAgentStore:
    def __init__(self, agents: list[StubAgent]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> StubAgent:
        return self._agents[agent_id]

    def list(self) -> list[StubAgent]:
        return list(self._agents.values())


class StubStorage:
    """Returns core fragment default texts so editable blocks carry text."""

    def __init__(self) -> None:
        self._fragments = {
            "identity_runtime.md": (
                "## Identity Environment\n"
                "Host {server_hostname}\n"
                "Version {vbot_version}\n"
                "Identity Workspace {identity_workspace}\n"
                "Root {vbot_root}\n"
                "Data {data_root}"
            ),
            "runtime.md": "## Runtime\nOS {operating_system}",
            "working_project.md": (
                "## Working Project\n"
                "Project {project_name}\n"
                "Project ID {project_id}\n"
                "Project Workspace {project_workspace}\n"
                "{project_files}"
            ),
            "tools.md": "## Tools\n{generated:tool_list}",
            "channels.md": "## Channels\n{generated:channel_list}",
            "skills.md": "## Skills\n{generated:skill_catalog}",
        }
        self._agent_fragments: dict[tuple[str, str], str] = {}

    def read_prompt_fragment(self, fragment_name: str) -> str:
        return self._fragments.get(fragment_name, "")

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        return self._agent_fragments.get((agent_id, fragment_name), "")


class StubTools:
    def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(
                name="read",
                internal=False,
                activation="configurable",
                constraints=(),
            )
        ]

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        profile_context: Any | None = None,
    ) -> list[JsonObject]:
        return [{"name": "read", "description": "Read a file"}]

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        profile_context: Any | None = None,
    ) -> list[JsonObject]:
        return [{"name": "read", "description": "Read a file", "parameters": {"type": "object"}}]


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str
    origin: str | None = None


class StubSkills:
    def __init__(self, skills: list[StubSkill] | None = None) -> None:
        self._skills = skills or []

    def filter_allowed(self, allowed_skills: list[str]) -> list[StubSkill]:
        if "*" in allowed_skills:
            return list(self._skills)
        return [skill for skill in self._skills if skill.name in allowed_skills]


class StubBlockStore:
    """In-memory read+write BlockStore using the manager's scope-key convention."""

    def __init__(
        self,
        *,
        layouts: dict[str, list[LayoutEntry]] | None = None,
        overrides: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._layouts = layouts or {}
        self._overrides = overrides or {}

    def read_layout(self, scope: str) -> list[LayoutEntry]:
        return list(self._layouts.get(scope, []))

    def read_block_override(self, scope: str, block_id: str) -> str | None:
        return self._overrides.get((scope, block_id))

    def write_layout(
        self, scope: str, entries: Sequence[LayoutEntry], *, reset: bool = False
    ) -> None:
        self._layouts[scope] = list(entries)

    def prune_layout(
        self, scope: str, entries: Sequence[LayoutEntry], known_ids: frozenset[str]
    ) -> None:
        self._layouts[scope] = [entry for entry in entries if entry.id in known_ids]

    def seed_agent_layout(
        self, scope: str, default_layout: Sequence[LayoutEntry], *, overwrite: bool = False
    ) -> None:
        if scope in self._layouts and not overwrite:
            return
        self._layouts[scope] = list(default_layout)

    def write_block_override(self, scope: str, block_id: str, content: str) -> None:
        self._overrides[(scope, block_id)] = content

    def remove_block_override(self, scope: str, block_id: str) -> bool:
        return self._overrides.pop((scope, block_id), None) is not None


def _manager(
    tmp_path: Path,
    *,
    store: StubBlockStore | None = None,
    agents: list[StubAgent] | None = None,
    block_definitions: Sequence[BlockDefinition] = (),
    loaded_extensions: Sequence[str] = (),
    tools: Any | None = None,
) -> SystemPromptManager:
    return SystemPromptManager(
        StubStorage(),
        tools if tools is not None else StubTools(),
        StubSkills(),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="test-host",
        operating_system="test-os",
        current_local_date=lambda: "2026-05-04",
        timezone_name=lambda: "Europe/Berlin",
        block_store=store or StubBlockStore(),
        agent_store=cast(PromptAgentStore, StubAgentStore(agents)) if agents is not None else None,
        block_definitions=block_definitions,
        loaded_extensions=loaded_extensions,
    )


def _state(manager: SystemPromptManager, *, runtime_extra: JsonObject | None = None) -> Any:
    runtime = SimpleNamespace(system_prompts=manager, **(runtime_extra or {}))
    return SimpleNamespace(runtime=runtime)


def _preview_state(
    manager: SystemPromptManager,
    agent: Any,
    *,
    projects: Any = None,
    skills_for: Any = None,
) -> Any:
    """State whose resolver serves one Agent and fails like the real one otherwise."""

    def resolve_agent(_project_id: str | None, agent_id: str) -> Any:
        if agent_id != agent.id:
            raise ResolutionAgentNotFoundError(f"agent not found: {agent_id}")
        return agent

    return _state(
        manager,
        runtime_extra={
            "agent_resolver": SimpleNamespace(resolve_agent=resolve_agent),
            "projects": projects if projects is not None else SimpleNamespace(),
            "skills_for": skills_for or (lambda _project, _agent=None: StubSkills()),
        },
    )


# --- log.list / log.read -----------------------------------------------------


def _log_state(tmp_path: Path) -> Any:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    for name in ("2026-05-09", "2026-05-11", "2026-05-10"):
        (logs_dir / name).write_text("", encoding="utf-8")
    (logs_dir / "2026-05-11").write_text(
        "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\ntrace line", encoding="utf-8"
    )
    return SimpleNamespace(runtime=SimpleNamespace(storage=SimpleNamespace(data_dir=tmp_path)))


@pytest.mark.asyncio
async def test_log_list_and_read_return_the_log_viewer_results(tmp_path: Path) -> None:
    state = _log_state(tmp_path)

    listed = await dispatch_rpc(state, {"method": "log.list", "params": {}})
    read = await dispatch_rpc(state, {"method": "log.read", "params": {"file": "2026-05-11"}})

    assert listed == {
        "ok": True,
        "result": {
            "files": ["2026-05-11", "2026-05-10", "2026-05-09"],
            "default_file": "2026-05-11",
        },
    }
    assert read["ok"] is True
    assert read["result"]["file"] == "2026-05-11"
    assert read["result"]["entries"] == [
        {
            "timestamp": "2026-05-11 09:00:00",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Ready",
            "continuation": "trace line",
            "raw": "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready\ntrace line",
        }
    ]
    # The cursor lets the log stream continue after this read.
    assert isinstance(read["result"]["cursor"], str)
    assert read["result"]["cursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "code"),
    [
        pytest.param("log.list", {"extra": True}, "invalid_request", id="list-with-params"),
        pytest.param("log.read", {}, "invalid_request", id="read-without-file"),
        pytest.param("log.read", {"file": "../2026-05-11"}, "invalid_request", id="path-escape"),
        pytest.param(
            "log.read", {"file": "2026-05-11", "extra": True}, "invalid_request", id="extra-field"
        ),
        pytest.param("log.read", {"file": "2026-05-12"}, "domain_error", id="missing-file"),
    ],
)
async def test_invalid_log_requests_are_rejected(
    tmp_path: Path, method: str, params: JsonObject, code: str
) -> None:
    response = await dispatch_rpc(_log_state(tmp_path), {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == code


# --- prompt.* block editor ---------------------------------------------------


def test_list_returns_blocks_in_layout_order_with_scopes(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", name="Coder", custom_system_prompt_enabled=True)
    state = _state(_manager(tmp_path, agents=[agent]))

    result = _list_prompts(state, {})

    block_ids = [block["id"] for block in result["blocks"]]
    assert block_ids == [
        "core:soul",
        "memory:guidance",
        "core:runtime",
        "core:identity_runtime",
        "core:tools",
        "core:tools_list",
        "core:channels",
        "core:skills",
        "core:skill_maintenance",
        "core:agent_body",
        "core:working_project",
    ]
    tools = next(block for block in result["blocks"] if block["id"] == "core:tools")
    assert tools["editable"] is True
    assert tools["source"] == "core"
    assert tools["rank"] == block_ids.index("core:tools")
    # ``scopes`` still returned (default + the enabled agent scope). The agent
    # scope carries has_customizations; here the stub store has no saved layout or
    # override for it, so it is False.
    assert result["scopes"] == [
        {"type": "default", "label": "Default"},
        {
            "type": "agent",
            "agent_id": "coder",
            "label": "Coder",
            "has_customizations": False,
        },
    ]


def test_list_agent_scope_includes_inheritance_flags(tmp_path: Path) -> None:
    store = StubBlockStore(overrides={("agent:coder", "core:tools"): "agent tools"})
    agent = StubAgent(id="coder", name="Coder", custom_system_prompt_enabled=True)
    state = _state(_manager(tmp_path, store=store, agents=[agent]))

    result = _list_prompts(state, {"scope": {"type": "agent", "agent_id": "coder"}})

    tools = next(block for block in result["blocks"] if block["id"] == "core:tools")
    assert tools["inheritance"] == "agent_override"
    assert tools["text"] == "agent tools"
    skills = next(block for block in result["blocks"] if block["id"] == "core:skills")
    assert skills["inheritance"] == "owner_default"


def test_update_edits_block_and_logs_only_a_real_change(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = StubBlockStore()
    state = _state(_manager(tmp_path, store=store))

    with caplog.at_level(logging.INFO, logger="vbot.server.rpc.prompts"):
        result = _update_prompt(state, {"id": "core:tools", "content": "## Custom"})
        _update_prompt(state, {"id": "core:tools", "content": "## Custom"})

    assert result["id"] == "core:tools"
    assert result["text"] == "## Custom"
    assert result["is_modified"] is True
    assert store.read_block_override("default", "core:tools") == "## Custom"
    # The unchanged second update does not log; the log never carries the content.
    prompt_logs = [
        record.getMessage() for record in caplog.records if record.name == "vbot.server.rpc.prompts"
    ]
    assert len(prompt_logs) == 1
    assert "core:tools" in prompt_logs[0]
    assert "## Custom" not in caplog.text


def test_reset_removes_override(tmp_path: Path) -> None:
    store = StubBlockStore(overrides={("default", "core:tools"): "custom"})
    state = _state(_manager(tmp_path, store=store))

    result = _reset_prompt(state, {"id": "core:tools"})

    assert result["is_modified"] is False
    assert store.read_block_override("default", "core:tools") is None


def test_set_layout_persists_and_prunes_inert_id(tmp_path: Path) -> None:
    store = StubBlockStore()
    state = _state(_manager(tmp_path, store=store))

    result = _set_prompt_layout(
        state,
        {
            "layout": [
                {"id": "core:skills", "enabled": False},
                {"id": "core:tools", "enabled": True},
                {"id": "extension:gone", "enabled": True},
            ]
        },
    )

    persisted = [entry.id for entry in store.read_layout("default")]
    assert persisted == ["core:skills", "core:tools"]  # inert id pruned
    assert [entry["id"] for entry in result["layout"]] == ["core:skills", "core:tools"]


def test_create_block_creates_valid_block(tmp_path: Path) -> None:
    store = StubBlockStore()
    state = _state(_manager(tmp_path, store=store))

    result = _create_prompt_block(state, {"slug": "greeting", "content": "Hello."})

    assert result["id"] == "user:greeting"
    assert result["owner"] == "always"
    assert result["kind"] == "text"
    assert store.read_block_override("default", "user:greeting") == "Hello."
    assert any(entry.id == "user:greeting" for entry in store.read_layout("default"))


def test_remove_block_deletes_custom_block(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "note"},
    )
    state = _state(_manager(tmp_path, store=store))

    result = _remove_prompt_block(state, {"id": "user:note"})

    assert store.read_block_override("default", "user:note") is None
    assert all(entry["id"] != "user:note" for entry in result["layout"])


def test_reset_layout_restores_bundled_default(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="core:tools", enabled=False)]})
    state = _state(_manager(tmp_path, store=store))

    result = _reset_prompt_layout(state, {})

    persisted = [entry.id for entry in store.read_layout("default")]
    assert persisted[0] == "core:soul"
    assert "core:skills" in persisted
    assert [entry["id"] for entry in result["layout"]] == persisted


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        pytest.param(_list_prompts, {"bogus": 1}, id="list-unsupported-field"),
        pytest.param(
            _list_prompts,
            {"scope": {"type": "agent", "agent_id": "coder"}},
            id="list-disabled-agent-scope",
        ),
        pytest.param(_update_prompt, {"id": "core:tools", "content": 5}, id="update-non-string"),
        pytest.param(
            _update_prompt, {"id": "core:soul", "content": "nope"}, id="update-data-block"
        ),
        pytest.param(_reset_prompt, {"id": "user:note"}, id="reset-user-block"),
        pytest.param(_set_prompt_layout, {"layout": {"id": "core:tools"}}, id="layout-not-a-list"),
        pytest.param(_create_prompt_block, {"slug": "../etc/passwd"}, id="create-bad-slug"),
        pytest.param(_create_prompt_block, {"slug": "note"}, id="create-collision"),
        pytest.param(
            _create_prompt_block,
            {"slug": "greeting", "position": -1},
            id="create-negative-position",
        ),
        pytest.param(_remove_prompt_block, {"id": "core:tools"}, id="remove-non-user-block"),
    ],
)
def test_prompt_editor_rejects_invalid_requests(
    tmp_path: Path, handler: Any, params: JsonObject
) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "note"},
    )
    agent = StubAgent(id="coder", name="Coder", custom_system_prompt_enabled=False)
    state = _state(_manager(tmp_path, store=store, agents=[agent]))

    with pytest.raises(RpcError) as exc:
        handler(state, params)
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("prompt.list", {}),
        ("prompt.update", {"id": "core:tools", "content": "test-owned text"}),
        ("prompt.reset", {"id": "core:tools"}),
        ("prompt.set_layout", {"layout": [{"id": "core:tools", "enabled": False}]}),
        ("prompt.create_block", {"slug": "new-block", "content": "test-owned text"}),
        ("prompt.remove_block", {"id": "user:existing"}),
        ("prompt.reset_layout", {}),
    ],
)
async def test_prompt_editor_rpc_keeps_event_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, params: JsonObject
) -> None:
    import server.rpc.operations_methods as methods

    manager = _manager(tmp_path)
    manager.create_block("existing", "test-owned text")
    state = _state(manager)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()

    def slow_manager(_state: Any) -> SystemPromptManager:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return manager

    monkeypatch.setattr(methods, "_prompt_manager", slow_manager)
    task = asyncio.create_task(dispatch_rpc(state, {"method": method, "params": params}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert result["ok"] is True, result


# --- prompt.preview ----------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_includes_extension_block_and_token_estimates(tmp_path: Path) -> None:
    # An extension-contributed block flows through the same build path as a Run.
    extension_block = BlockDefinition(
        id="extension:greeter",
        owner="extension:greeter",
        default_text="EXTENSION-BLOCK-MARKER",
    )
    agent = StubAgent(id="coder", name="Coder", workspace=str(tmp_path / "ws"))
    manager = _manager(
        tmp_path,
        agents=[agent],
        block_definitions=[extension_block],
        loaded_extensions=["greeter"],
    )
    state = _preview_state(manager, agent, projects=SimpleNamespace(find_by_cwd=lambda _cwd: None))

    result = await _preview_prompt(state, {"agent_id": "coder"})

    assert "EXTENSION-BLOCK-MARKER" in result["text"]
    assert result["tokens"] > 0
    # The provider tool-definition array is reported beside the prompt text.
    assert result["tool_count"] == 1
    assert result["tool_tokens"] > 0
    assert result["estimated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "rendered", "hidden"),
    [
        # Without a scope the preview shows the Agent's effective prompt.
        pytest.param({"agent_id": "coder"}, "AGENT-TOOLS", "DEFAULT-TOOLS", id="effective"),
        pytest.param(
            {"agent_id": "coder", "scope": {"type": "default"}},
            "DEFAULT-TOOLS",
            "AGENT-TOOLS",
            id="default-scope",
        ),
        # An Agent scope names its own identity Agent; no agent_id is needed.
        pytest.param(
            {"scope": {"type": "agent", "agent_id": "coder"}},
            "AGENT-TOOLS",
            "DEFAULT-TOOLS",
            id="agent-scope",
        ),
    ],
)
async def test_preview_renders_the_requested_scope_and_validates_it_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: JsonObject, rendered: str, hidden: str
) -> None:
    agent = StubAgent(
        id="coder", name="Coder", workspace=str(tmp_path / "ws"), custom_system_prompt_enabled=True
    )
    store = StubBlockStore(
        overrides={
            ("default", "core:tools"): "DEFAULT-TOOLS",
            ("agent:coder", "core:tools"): "AGENT-TOOLS",
        }
    )
    manager = _manager(tmp_path, store=store, agents=[agent])
    state = _preview_state(manager, agent, projects=SimpleNamespace(find_by_cwd=lambda _cwd: None))
    validate_scope = manager.validate_scope
    threads: list[int] = []

    def recording_validate_scope(scope: Any = None) -> Any:
        threads.append(threading.get_ident())
        return validate_scope(scope)

    monkeypatch.setattr(manager, "validate_scope", recording_validate_scope)

    result = await _preview_prompt(state, params)

    assert rendered in result["text"]
    assert hidden not in result["text"]
    # An explicit scope reads its Agent, so validation runs off the Event Loop.
    assert len(threads) == ("scope" in params)
    assert threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_preview_resolves_rooted_identity_skill_pool(tmp_path: Path) -> None:
    # A Rooted Identity Agent previews against its explicitly selected Project's
    # skill pool, matching live Run scope rather than the bare global registry.
    repo = tmp_path / "repo"
    repo.mkdir()
    identity_workspace = tmp_path / "identity"
    identity_workspace.mkdir()
    agent = StubAgent(
        id="coder",
        name="Coder",
        workspace=str(identity_workspace),
        root_project_id="vbot",
    )
    home_project = SimpleNamespace(
        project_id="vbot",
        display_name="vBot",
        cwd=str(repo),
        auto_load=(),
    )
    skills_for_calls: list[tuple[str | None, str | None]] = []

    def skills_for(project_id: str | None, agent_id: str | None = None) -> StubSkills:
        skills_for_calls.append((project_id, agent_id))
        return StubSkills()

    state = _preview_state(
        _manager(tmp_path, agents=[agent]),
        agent,
        projects=SimpleNamespace(get=lambda _project_id: home_project),
        skills_for=skills_for,
    )

    result = await _preview_prompt(state, {"agent_id": "coder"})

    assert skills_for_calls == [("vbot", "coder")]
    assert "## Identity Environment" in result["text"]
    assert f"Identity Workspace {model_path(identity_workspace)}" in result["text"]
    assert "## Working Project" in result["text"]
    assert f"Project Workspace {model_path(repo)}" in result["text"]


@pytest.mark.asyncio
async def test_preview_project_config_agent_renders_its_body_in_the_working_project(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = ConfigAgent(
        id="reviewer",
        name="Reviewer",
        model="openai/gpt-5",
        temperature=None,
        tool_access=ToolAccess(mode="all"),
        allowed_skills=["*"],
        tools={},
        body="Imported reviewer body",
        source_path=repo / ".opencode" / "agents" / "reviewer.md",
        source_format="opencode",
    )
    project = SimpleNamespace(
        project_id="vbot",
        display_name="vBot",
        cwd=str(repo),
        auto_load=(),
    )
    state = _preview_state(
        _manager(tmp_path, agents=[]),
        agent,
        projects=SimpleNamespace(get=lambda _project_id: project),
    )

    result = await _preview_prompt(state, {"agent_id": "reviewer@vbot"})

    # The preview matches a project-born Run: the imported body and only the
    # Working Project runtime, without the identity environment.
    assert "Imported reviewer body" in result["text"]
    assert "## Runtime" in result["text"]
    assert "## Working Project" in result["text"]
    assert "Project vBot" in result["text"]
    assert "Project ID vbot" in result["text"]
    assert f"Project Workspace {model_path(repo)}" in result["text"]
    assert "## Identity Environment" not in result["text"]
    assert "Host test-host" not in result["text"]
    assert "Identity Workspace" not in result["text"]
    assert f"Root {tmp_path / 'app'}" not in result["text"]
    assert f"Data {tmp_path / 'data'}" not in result["text"]


@pytest.mark.asyncio
async def test_preview_missing_rooted_project_cwd_maps_error_without_fallback(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        name="Coder",
        workspace=str(tmp_path / "workspace"),
        root_project_id="vbot",
    )
    missing_project = SimpleNamespace(
        project_id="vbot",
        cwd=str(tmp_path / "missing-repo"),
        auto_load=(),
    )
    state = _preview_state(
        _manager(tmp_path, agents=[agent]),
        agent,
        projects=SimpleNamespace(get=lambda _project_id: missing_project),
    )

    with pytest.raises(RpcError) as exc_info:
        await _preview_prompt(state, {"agent_id": "coder"})
    assert exc_info.value.code == "domain_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "code"),
    [
        pytest.param({"agent_id": "coder", "bogus": 1}, "invalid_request", id="unsupported-field"),
        pytest.param({}, "invalid_request", id="missing-agent-id"),
        pytest.param({"agent_id": "nobody"}, "agent_not_found", id="unknown-agent"),
        pytest.param(
            {"agent_id": "coder", "include_tools": None}, "invalid_request", id="null-inspection"
        ),
        pytest.param(
            {"agent_id": "coder", "include_tools": "true"},
            "invalid_request",
            id="string-inspection",
        ),
        pytest.param(
            {"agent_id": "coder", "include_tools": 1}, "invalid_request", id="number-inspection"
        ),
    ],
)
async def test_preview_rejects_invalid_requests(
    tmp_path: Path, params: JsonObject, code: str
) -> None:
    agent = StubAgent(id="coder", name="Coder")
    state = _preview_state(_manager(tmp_path, agents=[agent]), agent)

    response = await dispatch_rpc(state, {"method": "prompt.preview", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected", [("all", ["mcp_example"]), ("none", [])])
async def test_preview_inspects_only_effective_provider_definitions(
    tmp_path: Path, mode: str, expected: list[str]
) -> None:
    agent = StubAgent(
        id="coder", name="Coder", tool_access=ToolAccess(mode=mode, denied=("blocked",))
    )
    registry = ToolRegistry()
    schema = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["search", "read"]}},
        "additionalProperties": False,
    }
    for name in ("mcp_example", "blocked", "remote_detail"):
        registry.register(
            name, "TEST-DEFINITION", schema, lambda *_: {}, deferred=name == "remote_detail"
        )
    manager = _manager(tmp_path, agents=[agent], tools=registry)
    state = _preview_state(manager, agent)

    ordinary = await _preview_prompt(state, {"agent_id": "coder"})
    inspected = await _preview_prompt(state, {"agent_id": "coder", "include_tools": True})

    assert "tools" not in ordinary
    assert {key: value for key, value in inspected.items() if key != "tools"} == ordinary
    assert [entry["definition"]["name"] for entry in inspected["tools"]] == expected
    assert [
        entry["definition"] for entry in inspected["tools"]
    ] == manager.provider_tool_definitions(cast(Any, agent))
    assert all(entry["tokens"] > 0 for entry in inspected["tools"])
