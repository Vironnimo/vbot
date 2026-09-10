"""Tests for the skill mutation RPC handlers (global + per-agent scopes)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.skills.authoring import SkillAuthoringService
from core.skills.skills import SkillRegistry
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.skill_methods import (
    _skill_create,
    _skill_delete,
    _skill_read,
    _skill_remove_file,
    _skill_update,
    _skill_write_file,
    method_handlers,
)


def _skill_md(
    name: str = "demo", description: str = "Do a demo task.", body: str = "# Demo\n"
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _SkillRuntime:
    def __init__(self, root: Path, known_agents: set[str]) -> None:
        self._root = root
        self.global_skills_dir = root / "skills"
        self.skill_authoring = SkillAuthoringService(
            protected_roots=[root / "resources" / "skills"]
        )
        self.reload_calls = 0
        self.invalidated: list[str] = []
        # The identity-store probe the agent-scope validation consults: private
        # skill homes are identity-only, so only known agents accept writes.
        self.agents = SimpleNamespace(exists=lambda agent_id: agent_id in known_agents)

    def agent_skills_dir(self, agent_id: str) -> Path:
        return self._root / "agents" / agent_id / "skills"

    async def reload_skills_async(self) -> None:
        self.reload_calls += 1

    def invalidate_agent_skills(self, agent_id: str) -> None:
        self.invalidated.append(agent_id)


def _state(tmp_path: Path, known_agents: set[str] | None = None) -> Any:
    known = known_agents if known_agents is not None else {"builder"}
    return SimpleNamespace(runtime=_SkillRuntime(tmp_path, known))


@pytest.mark.asyncio
async def test_create_global_writes_and_reloads(tmp_path: Path) -> None:
    state = _state(tmp_path)

    result = await _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})

    assert result["name"] == "demo"
    assert (state.runtime.global_skills_dir / "demo" / "SKILL.md").is_file()
    assert state.runtime.reload_calls == 1
    assert state.runtime.invalidated == []


@pytest.mark.asyncio
async def test_create_agent_writes_and_invalidates(tmp_path: Path) -> None:
    state = _state(tmp_path)

    await _skill_create(state, {"scope": "agent:builder", "name": "demo", "content": _skill_md()})

    assert (state.runtime.agent_skills_dir("builder") / "demo" / "SKILL.md").is_file()
    assert state.runtime.invalidated == ["builder"]
    assert state.runtime.reload_calls == 0


@pytest.mark.asyncio
async def test_create_records_human_provenance(tmp_path: Path) -> None:
    state = _state(tmp_path)

    await _skill_create(
        state, {"scope": "global", "name": "demo", "content": _skill_md(), "source": "wiki"}
    )

    skill = SkillRegistry.load(state.runtime.global_skills_dir).get("demo")
    assert skill.metadata["vbot"]["author"] == "human"
    assert skill.metadata["vbot"]["source"] == "wiki"


@pytest.mark.asyncio
async def test_update_rewrites_skill(tmp_path: Path) -> None:
    state = _state(tmp_path)
    await _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})

    await _skill_update(
        state, {"scope": "global", "name": "demo", "content": _skill_md(description="Updated.")}
    )

    assert SkillRegistry.load(state.runtime.global_skills_dir).get("demo").description == "Updated."


@pytest.mark.asyncio
async def test_delete_removes_skill(tmp_path: Path) -> None:
    state = _state(tmp_path)
    await _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})

    await _skill_delete(state, {"scope": "global", "name": "demo"})

    assert not (state.runtime.global_skills_dir / "demo").exists()


@pytest.mark.asyncio
async def test_write_and_remove_support_file(tmp_path: Path) -> None:
    state = _state(tmp_path)
    await _skill_create(state, {"scope": "agent:builder", "name": "demo", "content": _skill_md()})

    await _skill_write_file(
        state,
        {"scope": "agent:builder", "name": "demo", "path": "scripts/run.py", "content": "x = 1\n"},
    )
    resource = state.runtime.agent_skills_dir("builder") / "demo" / "scripts" / "run.py"
    assert resource.is_file()

    await _skill_remove_file(
        state, {"scope": "agent:builder", "name": "demo", "path": "scripts/run.py"}
    )
    assert not resource.exists()


@pytest.mark.asyncio
async def test_project_scope_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError) as exc:
        await _skill_create(
            state, {"scope": "project:vbot", "name": "demo", "content": _skill_md()}
        )

    assert exc.value.code == RPC_ERROR_INVALID_REQUEST
    assert "scope" in exc.value.message


@pytest.mark.asyncio
async def test_invalid_agent_scope_id_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError) as exc:
        await _skill_create(
            state, {"scope": "agent:../escape", "name": "demo", "content": _skill_md()}
        )

    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


@pytest.mark.asyncio
async def test_unknown_agent_scope_is_rejected(tmp_path: Path) -> None:
    # Private skill homes are identity-only. A well-formed id that names no stored
    # identity agent (e.g. a project-team slug) must be refused — a write would
    # create a stray ``agents/<id>/skills`` home that no agent owns.
    state = _state(tmp_path, known_agents={"builder"})

    with pytest.raises(RpcError) as exc:
        await _skill_create(state, {"scope": "agent:ghost", "name": "demo", "content": _skill_md()})

    assert exc.value.code == RPC_ERROR_INVALID_REQUEST
    assert "ghost" in exc.value.message
    assert not state.runtime.agent_skills_dir("ghost").exists()


@pytest.mark.asyncio
async def test_missing_description_uses_body_and_reloads(tmp_path: Path) -> None:
    state = _state(tmp_path)

    result = await _skill_create(
        state,
        {"scope": "global", "name": "demo", "content": "---\nname: demo\n---\n\nbody\n"},
    )

    assert result["name"] == "demo"
    assert result["operation"] == "create"
    assert result["warnings"]
    assert SkillRegistry.load(state.runtime.global_skills_dir).get("demo").description == "body"
    assert state.runtime.reload_calls == 1


@pytest.mark.asyncio
async def test_missing_scope_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError):
        await _skill_create(state, {"name": "demo", "content": _skill_md()})


@pytest.mark.asyncio
async def test_read_returns_scope_skills_with_content(tmp_path: Path) -> None:
    state = _state(tmp_path)
    await _skill_create(
        state, {"scope": "global", "name": "demo", "content": _skill_md(body="# Demo\nGo.")}
    )

    result = await _skill_read(state, {"scope": "global"})

    assert [skill["name"] for skill in result["skills"]] == ["demo"]
    entry = result["skills"][0]
    assert entry["description"] == "Do a demo task."
    assert "# Demo\nGo." in entry["content"]


@pytest.mark.asyncio
async def test_read_empty_scope_returns_no_skills(tmp_path: Path) -> None:
    state = _state(tmp_path)

    assert (await _skill_read(state, {"scope": "agent:builder"}))["skills"] == []


def test_method_handlers_registered() -> None:
    handlers = method_handlers()
    assert set(handlers) == {
        "skill.read",
        "skill.create",
        "skill.update",
        "skill.delete",
        "skill.write_file",
        "skill.remove_file",
        "skill.inventory",
        "skill.inspect",
        "skill.set_disabled",
        "skill.share",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "owner_method", "params"),
    [
        ("skill.create", "create", {"name": "new", "content": _skill_md("new")}),
        ("skill.update", "edit", {"name": "demo", "content": _skill_md()}),
        ("skill.delete", "delete", {"name": "demo"}),
        ("skill.write_file", "write_file", {"name": "demo", "path": "scripts/a.py", "content": ""}),
        ("skill.remove_file", "remove_file", {"name": "demo", "path": "scripts/a.py"}),
        ("skill.read", None, {}),
    ],
)
async def test_skill_rpc_io_yields_and_refreshes_on_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    owner_method: str | None,
    params: dict[str, Any],
) -> None:
    import asyncio
    import threading

    import server.rpc.skill_methods as methods
    from server.rpc.methods import dispatch_rpc

    state = _state(tmp_path)
    await _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})
    await _skill_write_file(
        state, {"scope": "global", "name": "demo", "path": "scripts/a.py", "content": ""}
    )
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    refreshed: list[bool] = []
    owner = state.runtime.skill_authoring if owner_method else methods
    name = owner_method or "_read_skills"
    original = getattr(owner, name)

    def slow_io(*args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return original(*args, **kwargs)

    async def reload_skills() -> None:
        assert threading.get_ident() == loop_thread
        refreshed.append(True)

    monkeypatch.setattr(owner, name, slow_io)
    monkeypatch.setattr(state.runtime, "reload_skills_async", reload_skills)
    task = asyncio.create_task(
        dispatch_rpc(state, {"method": method, "params": {"scope": "global", **params}})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert result["ok"] is True, result
    assert bool(refreshed) == (method != "skill.read")
