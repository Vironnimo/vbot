"""The Permission Boundary of a Run: built from its Agent, carried to every Tool Call."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import core.chat._run_permissions as run_permissions
from core.permissions import (
    BoundaryContext,
    PermissionBoundary,
    PermissionRules,
    normalize_permission_rules,
)
from core.projects import AgentOverrides
from core.sessions import SUBAGENT_PARENT_META_KEY, SessionAddress, is_prompt_block_change_note
from core.tools import ToolContext, ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    history,
)
from tests.core.chat.chat_loop_tools_test_support import final, tool_turn
from tests.core.prompts.prompts_test_support import _agent, _manager


@dataclass(frozen=True)
class _RuleAgent(StubAgent):
    permission_rules: PermissionRules | None = None


class _ScratchFolders:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def folder_for(self, address: SessionAddress) -> Any:
        area = self.root / address.session_id
        own = area / address.session_id
        return SimpleNamespace(
            own=own, area=area, tmp=own / "tmp", recreated=False, incarnation="1"
        )


def _runtime(tmp_path: Path, rules: dict[str, Any] | None) -> tuple[Any, list[ToolContext]]:
    seen: list[ToolContext] = []

    def probe(context: ToolContext, _arguments: Any) -> Any:
        seen.append(context)
        return tool_success({})

    tools = ToolRegistry()
    tools.register("probe", "Probe.", {"type": "object"}, probe)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = _RuleAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        workspace=workspace,
        permission_rules=normalize_permission_rules(rules) if rules is not None else None,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path / "data",
        agent=agent,
        adapter=StubAdapter([tool_turn(("call_1", "probe")), final("Done")]),
        tools=tools,
    )
    runtime.scratch_areas = _ScratchFolders(tmp_path / "scratch")
    runtime.chat_sessions.create("coder", session_id="s1")
    return runtime, seen


def _system_prompt(runtime: Any) -> str:
    return str(runtime.adapter.requests[0]["messages"][0]["content"])


@pytest.mark.asyncio
async def test_a_restricted_run_checks_tool_accesses_against_its_expanded_boundary(
    tmp_path: Path,
) -> None:
    runtime, seen = _runtime(
        tmp_path,
        {"read": "work_area", "write": "scratch", "deny": ["Read({workdir}/secrets/**)"]},
    )

    await build_chat_loop(runtime).send("coder", "Work", session_id="s1")

    [context] = seen
    boundary = context.permissions
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch" / "s1"
    assert boundary.restricted
    # {workdir} is the Session's working directory; the Scratch Area is writable.
    assert boundary.check_path(workspace / "notes.md", "read") is None
    assert boundary.check_path(scratch / "s1" / "draft.md", "write") is None
    refused = boundary.check_path(workspace / "notes.md", "write")
    assert refused is not None and refused.reason == "not_permitted"
    denied = boundary.check_path(workspace / "secrets" / "key", "read")
    assert denied is not None and denied.rule == f"Read({workspace.as_posix()}/secrets/**)"
    outside = boundary.check_path(tmp_path / "elsewhere.md", "read")
    assert outside is not None and outside.reason == "not_permitted"
    # The Agent's own configuration is never readable.
    own_config = boundary.check_path(tmp_path / "data" / "agents" / "coder" / "agent.json", "read")
    assert own_config is not None and own_config.reason == "protected_location"

    # The System Prompt names the boundary in the Model's path form.
    prompt = _system_prompt(runtime)
    assert "## Permission rules" in prompt
    assert f"`{workspace.as_posix()}`" in prompt
    assert f"`Read({workspace.as_posix()}/secrets/**)`" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rules",
    [
        pytest.param(None, id="no-rules"),
        pytest.param(
            {
                "read": "anywhere",
                "write": "anywhere",
                "commands": "any",
                "web": "any",
                "allow": [],
                "deny": [],
            },
            id="explicit-full-access",
        ),
    ],
)
async def test_a_run_without_rules_gets_full_access_without_building_a_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: dict[str, object] | None
) -> None:
    runtime, seen = _runtime(tmp_path, rules)

    def unexpected(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("an unrestricted Run built a boundary")

    monkeypatch.setattr(run_permissions, "installation_dirs", unexpected)
    monkeypatch.setattr(run_permissions, "_skill_dirs", unexpected)
    monkeypatch.setattr(run_permissions, "_agent_config", unexpected)

    await build_chat_loop(runtime).send("coder", "Work", session_id="s1")

    [context] = seen
    assert context.permissions is PermissionBoundary.UNRESTRICTED
    assert "Permission rules" not in _system_prompt(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_from", ["parent", "cron"])
async def test_an_inherited_ceiling_bounds_an_agent_without_rules(
    tmp_path: Path, inherited_from: str
) -> None:
    runtime, seen = _runtime(tmp_path, None)
    address = SessionAddress(None, "coder", "s1")
    creator = PermissionBoundary.build(
        normalize_permission_rules({"write": "scratch"}),
        BoundaryContext(workdir=tmp_path / "creator", home=tmp_path, data_dir=tmp_path / "data"),
    )
    ceiling = creator.ceiling(source=cast(Any, inherited_from))
    assert ceiling is not None
    loop = build_chat_loop(runtime)

    if inherited_from == "parent":

        def link(metadata: dict[str, Any]) -> None:
            metadata[SUBAGENT_PARENT_META_KEY] = {
                "id": "sub_aaaaaaaaaaaa",
                "agent_id": "lead",
                "session_id": "parent",
                "run_id": "parent-run",
                "tool_call_id": None,
                "tool_call_index": None,
                "project_id": None,
            }
            stored = AgentOverrides(permission_ceiling=ceiling.to_ceiling_dict())
            metadata.update(stored.session_metadata())

        runtime.chat_sessions.mutate_metadata(address, link)
        await loop.send("coder", "Work", session_id="s1")
    else:
        run = await loop.start_run("coder", "Work", session_id="s1", permission_ceiling=ceiling)
        await run.wait()

    [context] = seen
    assert [layer.source for layer in context.permissions.layers] == [inherited_from]
    refused = context.permissions.check_path(tmp_path / "workspace" / "notes.md", "write")
    assert refused is not None and refused.source == inherited_from


@pytest.mark.asyncio
async def test_the_permissions_block_is_pinned_and_a_change_is_announced_once(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = _agent(workspace)
    adapter = StubAdapter([final(str(index)) for index in range(4)])
    runtime: Any = StubRuntime(data_dir=tmp_path / "data", agent=cast(Any, agent), adapter=adapter)
    runtime.system_prompts = _manager(tmp_path, tools=runtime.tools)
    runtime.chat_sessions.create(agent.id, session_id="s1")
    loop = build_chat_loop(runtime)

    await loop.send(agent.id, "First", session_id="s1")
    runtime.agents._agent = replace(
        agent, permission_rules=normalize_permission_rules({"write": "work_area"})
    )
    await loop.send(agent.id, "Second", session_id="s1")
    await loop.send(agent.id, "Third", session_id="s1")
    runtime.chat_sessions.create(agent.id, session_id="s2")
    await loop.send(agent.id, "Fresh", session_id="s2")

    *systems, fresh = [str(request["messages"][0]["content"]) for request in adapter.requests]
    # A new prompt epoch shows the block in the System Prompt itself.
    assert "## Permission rules" in fresh
    # Full access shows no block, and the epoch's System Prompt stays byte-identical.
    assert "Permission rules" not in systems[0]
    assert systems == [systems[0]] * 3
    notes = [
        message
        for message in history(runtime, "s1", agent.id)
        if is_prompt_block_change_note(message)
    ]
    assert len(notes) == 1
    assert "Permission rules" in str(notes[0].content)
    assert workspace.as_posix() in str(notes[0].content)
