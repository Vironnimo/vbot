"""Tests for the Skill manager RPC handlers (inventory / set_disabled / share)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.skill_methods import (
    _skill_inspect,
    _skill_inventory,
    _skill_set_disabled,
    _skill_share,
    method_handlers,
)


def _skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nUse it.\n"


class _ManagerRuntime:
    """Records invalidation/reload calls; inventory comes from a canned answer."""

    def __init__(self, tmp_path: Path) -> None:
        self._root = tmp_path
        self.inventory: dict[str, Any] = {"skills": [], "stale_shared": []}
        self.reload_calls = 0
        self.invalidated: list[str | None] = []
        self.policy_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.agents = SimpleNamespace(exists=lambda agent_id: agent_id in ("builder", "reviewer"))
        self.published: list[str] = []
        self.skill_policy = SimpleNamespace(
            set_disabled=self.set_disabled,
            set_shared=self.set_shared,
        )

    def skill_inventory(self) -> dict[str, Any]:
        return self.inventory

    async def reload_skills_async(self) -> None:
        self.reload_calls += 1

    def invalidate_agent_skills(self, agent_id: str | None) -> None:
        self.invalidated.append(agent_id)

    def set_disabled(self, name: str, *, disabled: bool) -> None:
        self.policy_calls.append(("set_disabled", (name, disabled)))

    def set_shared(
        self, agent_id: str, name: str, *, shared: bool, receivers: list[str] | None = None
    ) -> None:
        self.policy_calls.append(("set_shared", (agent_id, name, shared, receivers or [])))

    def agent_owns_private_skill(self, agent_id: str, name: str) -> bool:
        return agent_id == "builder" and name == "deploy"


def _state(tmp_path: Path) -> Any:
    runtime = _ManagerRuntime(tmp_path)
    return SimpleNamespace(
        runtime=runtime,
        event_bus=SimpleNamespace(
            publish=lambda event_type, payload=None: runtime.published.append(payload["kind"])
        ),
    )


class TestInventory:
    @pytest.mark.asyncio
    async def test_returns_the_runtime_inventory_unchanged(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.runtime.inventory = {"skills": [{"name": "x"}], "stale_shared": []}

        result = await _skill_inventory(state, {})

        assert result == {"skills": [{"name": "x"}], "stale_shared": []}

    @pytest.mark.asyncio
    async def test_rejects_params(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_inventory(state, {"scope": "global"})

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST


class TestSetDisabled:
    @pytest.mark.asyncio
    async def test_known_name_persists_reloads_and_publishes(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.runtime.inventory["skills"] = [{"name": "deploy"}]

        result = await _skill_set_disabled(state, {"name": "deploy", "disabled": True})

        assert result == {"name": "deploy", "disabled": True}
        assert state.runtime.policy_calls == [("set_disabled", ("deploy", True))]
        assert state.runtime.reload_calls == 1
        assert state.runtime.invalidated == []
        assert state.runtime.published == ["skills"]

    @pytest.mark.asyncio
    async def test_unknown_name_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_set_disabled(state, {"name": "ghost", "disabled": True})

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST
        assert state.runtime.reload_calls == 0

    @pytest.mark.asyncio
    async def test_non_boolean_disabled_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.runtime.inventory["skills"] = [{"name": "deploy"}]

        with pytest.raises(RpcError):
            await _skill_set_disabled(state, {"name": "deploy", "disabled": "yes"})


class TestShare:
    @pytest.mark.asyncio
    async def test_valid_owner_and_skill_persists_invalidates_and_publishes(
        self, tmp_path: Path
    ) -> None:
        state = _state(tmp_path)

        result = await _skill_share(
            state,
            {"agent_id": "builder", "name": "deploy", "shared": True, "receivers": ["reviewer"]},
        )

        assert result == {
            "agent_id": "builder",
            "name": "deploy",
            "shared": True,
            "receivers": ["reviewer"],
        }
        assert state.runtime.policy_calls == [
            ("set_shared", ("builder", "deploy", True, ["reviewer"]))
        ]
        assert state.runtime.invalidated == [None]
        assert state.runtime.reload_calls == 0
        assert state.runtime.published == ["skills"]

    @pytest.mark.asyncio
    async def test_unknown_agent_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_share(
                state,
                {"agent_id": "ghost", "name": "deploy", "shared": True, "receivers": ["reviewer"]},
            )

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_agent_not_owning_the_skill_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_share(
                state,
                {"agent_id": "builder", "name": "notes", "shared": True, "receivers": ["reviewer"]},
            )

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_unshare_passes_false_through(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        await _skill_share(state, {"agent_id": "builder", "name": "deploy", "shared": False})

        assert state.runtime.policy_calls == [("set_shared", ("builder", "deploy", False, []))]

    @pytest.mark.asyncio
    async def test_unknown_receiver_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_share(
                state,
                {"agent_id": "builder", "name": "deploy", "shared": True, "receivers": ["ghost"]},
            )

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_self_receiver_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_share(
                state,
                {"agent_id": "builder", "name": "deploy", "shared": True, "receivers": ["builder"]},
            )

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_empty_receivers_when_sharing_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            await _skill_share(
                state, {"agent_id": "builder", "name": "deploy", "shared": True, "receivers": []}
            )

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST


def test_manager_methods_are_registered() -> None:
    handlers = method_handlers()

    assert {
        "skill.inventory",
        "skill.set_disabled",
        "skill.share",
    } <= set(handlers)


@pytest.mark.asyncio
async def test_inspection_reads_exact_id_off_the_event_loop(tmp_path: Path) -> None:
    import threading

    state = _state(tmp_path)
    caller_thread = threading.get_ident()

    def inspect(entry_id: str) -> dict[str, str]:
        assert threading.get_ident() != caller_thread
        assert entry_id == "opaque-id"
        return {"id": entry_id, "content": "inspection-sentinel"}

    state.runtime.inspect_skill = inspect
    assert await _skill_inspect(state, {"id": "opaque-id"}) == {
        "id": "opaque-id",
        "content": "inspection-sentinel",
    }


@pytest.mark.asyncio
async def test_inspection_maps_a_vanished_package_to_invalid_request(tmp_path: Path) -> None:
    state = _state(tmp_path)

    def inspect(entry_id: str) -> None:
        raise ValueError(entry_id)

    state.runtime.inspect_skill = inspect
    with pytest.raises(RpcError) as error:
        await _skill_inspect(state, {"id": "missing-id"})
    assert error.value.code == RPC_ERROR_INVALID_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["skill.set_disabled", "skill.share"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_policy_rpc_offloads_io_and_applies_even_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, cancel: bool
) -> None:
    import asyncio
    import threading

    from server.rpc.methods import dispatch_rpc

    state = _state(tmp_path)
    runtime = state.runtime
    runtime.inventory = {"skills": [{"name": "deploy"}]}
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    policy_method = "set_disabled" if method == "skill.set_disabled" else "set_shared"
    original = getattr(runtime.skill_policy, policy_method)

    def check_worker(function: Any) -> Any:
        def checked(*args: Any, **kwargs: Any) -> Any:
            assert threading.get_ident() != loop_thread
            return function(*args, **kwargs)

        return checked

    monkeypatch.setattr(runtime, "skill_inventory", check_worker(runtime.skill_inventory))
    monkeypatch.setattr(
        runtime, "agent_owns_private_skill", check_worker(runtime.agent_owns_private_skill)
    )

    def slow_write(*args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return original(*args, **kwargs)

    async def reload_skills() -> None:
        assert threading.get_ident() == loop_thread
        assert runtime.policy_calls
        runtime.reload_calls += 1

    monkeypatch.setattr(runtime.skill_policy, policy_method, slow_write)
    monkeypatch.setattr(runtime, "reload_skills_async", reload_skills)
    params = (
        {"name": "deploy", "disabled": True}
        if method == "skill.set_disabled"
        else {"agent_id": "builder", "name": "deploy", "shared": True, "receivers": ["reviewer"]}
    )
    task = asyncio.create_task(dispatch_rpc(state, {"method": method, "params": params}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task)["ok"] is True
    assert runtime.policy_calls
    assert runtime.published == ["skills"]
    assert runtime.reload_calls == (1 if method == "skill.set_disabled" else 0)
    assert runtime.invalidated == ([] if method == "skill.set_disabled" else [None])


@pytest.mark.asyncio
async def test_skill_mutations_remain_serialized_through_cancelled_reload(tmp_path: Path) -> None:
    import asyncio

    from server.rpc.methods import dispatch_rpc

    state = _state(tmp_path)
    runtime = state.runtime
    runtime.inventory = {"skills": [{"name": "deploy"}]}
    reload_entered = asyncio.Event()
    release_reload = asyncio.Event()

    async def reload_skills() -> None:
        reload_entered.set()
        await release_reload.wait()
        runtime.reload_calls += 1

    runtime.reload_skills_async = reload_skills
    request = {"method": "skill.set_disabled", "params": {"name": "deploy", "disabled": True}}
    first = asyncio.create_task(dispatch_rpc(state, request))
    second = None
    try:
        await asyncio.wait_for(reload_entered.wait(), 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(
            dispatch_rpc(state, {**request, "params": {"name": "deploy", "disabled": False}})
        )
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        assert len(runtime.policy_calls) == 1
    finally:
        release_reload.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            assert (await second)["ok"] is True
    assert runtime.policy_calls[-1] == ("set_disabled", ("deploy", False))
    assert runtime.reload_calls == 2
    assert runtime.published == ["skills", "skills"]
