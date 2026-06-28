"""Tests for the block-model prompt RPC handlers (the ``prompt.*`` surface).

These exercise the thin RPC edge in ``server/rpc/operations_methods.py`` directly:
each handler is called with a fake ``state`` whose ``runtime.system_prompts`` is a
real :class:`SystemPromptManager` wired with an in-memory block store + agent store.
The edge's job is validation + error mapping; the block logic lives in the manager,
so the assertions here check the RPC shapes, the field-allowlist guards, and the
``PromptError`` → ``invalid_request`` split, plus that the preview builds through the
full block path (extension blocks included).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.prompts import (
    BlockDefinition,
    LayoutEntry,
    PromptAgentStore,
    SystemPromptManager,
)
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
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
    thinking_effort: str | None = "high"
    memory_prompt_mode: str = "off"
    allowed_tools: tuple[str, ...] = ()
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
            "runtime.md": "## Runtime\nHost {host}",
            "tools.md": "## Tools\n{generated:tool_list}",
            "channels.md": "## Channels\n{generated:channel_list}",
            "skills.md": "## Skills\n{generated:skill_list}",
        }
        self._agent_fragments: dict[tuple[str, str], str] = {}

    def read_prompt_fragment(self, fragment_name: str) -> str:
        return self._fragments.get(fragment_name, "")

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        return self._agent_fragments.get((agent_id, fragment_name), "")


class StubTools:
    def prompt_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[JsonObject]:
        return [{"name": "read", "description": "Read a file"}]

    def provider_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[JsonObject]:
        return []


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str


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

    def write_layout(self, scope: str, entries: Sequence[LayoutEntry]) -> None:
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
) -> SystemPromptManager:
    return SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills(),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host="test-host",
        os_name="test-os",
        current_date=lambda: "2026-05-04",
        block_store=store or StubBlockStore(),
        agent_store=cast(PromptAgentStore, StubAgentStore(agents)) if agents is not None else None,
        block_definitions=block_definitions,
        loaded_extensions=loaded_extensions,
    )


def _state(manager: SystemPromptManager, *, runtime_extra: JsonObject | None = None) -> Any:
    runtime = SimpleNamespace(system_prompts=manager, **(runtime_extra or {}))
    return SimpleNamespace(runtime=runtime)


# --- prompt.list -------------------------------------------------------------


def test_list_returns_blocks_in_layout_order_with_scopes(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", name="Coder", custom_system_prompt_enabled=True)
    state = _state(_manager(tmp_path, agents=[agent]))

    result = _list_prompts(state, {})

    block_ids = [block["id"] for block in result["blocks"]]
    assert block_ids == [
        "core:soul",
        "memory:guidance",
        "core:runtime",
        "core:tools",
        "core:channels",
        "core:skills",
        "core:agent_body",
        "core:project_files",
    ]
    tools = next(block for block in result["blocks"] if block["id"] == "core:tools")
    assert tools["editable"] is True
    assert tools["source"] == "core"
    assert tools["rank"] == block_ids.index("core:tools")
    # ``scopes`` still returned (default + the enabled agent scope).
    assert result["scopes"] == [
        {"type": "default", "label": "Default"},
        {"type": "agent", "agent_id": "coder", "label": "Coder"},
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


def test_list_rejects_unsupported_field(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _list_prompts(state, {"bogus": 1})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_list_rejects_disabled_agent_scope(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", name="Coder", custom_system_prompt_enabled=False)
    state = _state(_manager(tmp_path, agents=[agent]))

    with pytest.raises(RpcError) as exc:
        _list_prompts(state, {"scope": {"type": "agent", "agent_id": "coder"}})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


# --- prompt.update / prompt.reset --------------------------------------------


def test_update_edits_block_by_id(tmp_path: Path) -> None:
    store = StubBlockStore()
    state = _state(_manager(tmp_path, store=store))

    result = _update_prompt(state, {"id": "core:tools", "content": "## Custom"})

    assert result["id"] == "core:tools"
    assert result["text"] == "## Custom"
    assert result["is_modified"] is True
    assert store.read_block_override("default", "core:tools") == "## Custom"


def test_update_rejects_non_string_content(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _update_prompt(state, {"id": "core:tools", "content": 5})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_update_rejects_data_block(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _update_prompt(state, {"id": "core:soul", "content": "nope"})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_reset_removes_override(tmp_path: Path) -> None:
    store = StubBlockStore(overrides={("default", "core:tools"): "custom"})
    state = _state(_manager(tmp_path, store=store))

    result = _reset_prompt(state, {"id": "core:tools"})

    assert result["is_modified"] is False
    assert store.read_block_override("default", "core:tools") is None


def test_reset_rejects_user_block(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "note"},
    )
    state = _state(_manager(tmp_path, store=store))

    with pytest.raises(RpcError) as exc:
        _reset_prompt(state, {"id": "user:note"})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


# --- prompt.set_layout -------------------------------------------------------


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


def test_set_layout_rejects_non_list(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _set_prompt_layout(state, {"layout": {"id": "core:tools"}})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


# --- prompt.create_block / prompt.remove_block -------------------------------


def test_create_block_rejects_bad_slug(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _create_prompt_block(state, {"slug": "../etc/passwd"})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_create_block_rejects_collision(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="user:note", source="user")]})
    state = _state(_manager(tmp_path, store=store))

    with pytest.raises(RpcError) as exc:
        _create_prompt_block(state, {"slug": "note"})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_create_block_creates_valid_block(tmp_path: Path) -> None:
    store = StubBlockStore()
    state = _state(_manager(tmp_path, store=store))

    result = _create_prompt_block(state, {"slug": "greeting", "content": "Hello."})

    assert result["id"] == "user:greeting"
    assert result["owner"] == "always"
    assert result["kind"] == "text"
    assert store.read_block_override("default", "user:greeting") == "Hello."
    assert any(entry.id == "user:greeting" for entry in store.read_layout("default"))


def test_create_block_rejects_negative_position(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _create_prompt_block(state, {"slug": "greeting", "position": -1})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_remove_block_rejects_non_user_id(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        _remove_prompt_block(state, {"id": "core:tools"})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_remove_block_deletes_custom_block(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "note"},
    )
    state = _state(_manager(tmp_path, store=store))

    result = _remove_prompt_block(state, {"id": "user:note"})

    assert store.read_block_override("default", "user:note") is None
    assert all(entry["id"] != "user:note" for entry in result["layout"])


# --- prompt.reset_layout -----------------------------------------------------


def test_reset_layout_restores_bundled_default(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="core:tools", enabled=False)]})
    state = _state(_manager(tmp_path, store=store))

    result = _reset_prompt_layout(state, {})

    persisted = [entry.id for entry in store.read_layout("default")]
    assert persisted[0] == "core:soul"
    assert "core:skills" in persisted
    assert [entry["id"] for entry in result["layout"]] == persisted


# --- prompt.preview (extension block visible) --------------------------------


@pytest.mark.asyncio
async def test_preview_includes_extension_block(tmp_path: Path) -> None:
    # An extension-contributed block now flows through the same build path the
    # preview uses, so it appears in the preview (the old append bug is gone).
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
    runtime_extra = {
        "agent_resolver": SimpleNamespace(resolve_agent=lambda _project, _id: agent),
        "projects": SimpleNamespace(find_by_cwd=lambda _cwd: None),
        "skills_for": lambda _project: StubSkills(),
    }
    state = _state(manager, runtime_extra=runtime_extra)

    result = await _preview_prompt(state, {"agent_id": "coder"})

    assert "EXTENSION-BLOCK-MARKER" in result["text"]
    assert isinstance(result["tokens"], int)
    assert result["estimated"] is True


@pytest.mark.asyncio
async def test_preview_rejects_unsupported_field(tmp_path: Path) -> None:
    state = _state(_manager(tmp_path))

    with pytest.raises(RpcError) as exc:
        await _preview_prompt(state, {"agent_id": "coder", "bogus": 1})
    assert exc.value.code == RPC_ERROR_INVALID_REQUEST
