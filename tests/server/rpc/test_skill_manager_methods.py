"""Tests for the Skill manager RPC handlers (inventory / set_disabled / share)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.skill_methods import (
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
        self.agents = SimpleNamespace(exists=lambda agent_id: agent_id == "builder")
        self.published: list[str] = []
        self.skill_policy = SimpleNamespace(
            set_disabled=self.set_disabled,
            set_shared=self.set_shared,
        )

    def skill_inventory(self) -> dict[str, Any]:
        return self.inventory

    def reload_skills(self) -> None:
        self.reload_calls += 1

    def invalidate_agent_skills(self, agent_id: str | None) -> None:
        self.invalidated.append(agent_id)

    def set_disabled(self, name: str, *, disabled: bool) -> None:
        self.policy_calls.append(("set_disabled", (name, disabled)))

    def set_shared(self, agent_id: str, name: str, *, shared: bool) -> None:
        self.policy_calls.append(("set_shared", (agent_id, name, shared)))

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
    def test_returns_the_runtime_inventory_unchanged(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.runtime.inventory = {"skills": [{"name": "x"}], "stale_shared": []}

        result = _skill_inventory(state, {})

        assert result == {"skills": [{"name": "x"}], "stale_shared": []}

    def test_rejects_params(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            _skill_inventory(state, {"scope": "global"})

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST


class TestSetDisabled:
    def test_known_name_persists_reloads_and_publishes(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.runtime.inventory["skills"] = [{"name": "deploy"}]

        result = _skill_set_disabled(state, {"name": "deploy", "disabled": True})

        assert result == {"name": "deploy", "disabled": True}
        assert state.runtime.policy_calls == [("set_disabled", ("deploy", True))]
        assert state.runtime.reload_calls == 1
        assert state.runtime.invalidated == []
        assert state.runtime.published == ["skills"]

    def test_unknown_name_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            _skill_set_disabled(state, {"name": "ghost", "disabled": True})

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST
        assert state.runtime.reload_calls == 0

    def test_non_boolean_disabled_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.runtime.inventory["skills"] = [{"name": "deploy"}]

        with pytest.raises(RpcError):
            _skill_set_disabled(state, {"name": "deploy", "disabled": "yes"})


class TestShare:
    def test_valid_owner_and_skill_persists_invalidates_and_publishes(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        result = _skill_share(state, {"agent_id": "builder", "name": "deploy", "shared": True})

        assert result == {"agent_id": "builder", "name": "deploy", "shared": True}
        assert state.runtime.policy_calls == [("set_shared", ("builder", "deploy", True))]
        assert state.runtime.invalidated == [None]
        assert state.runtime.reload_calls == 0
        assert state.runtime.published == ["skills"]

    def test_unknown_agent_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            _skill_share(state, {"agent_id": "ghost", "name": "deploy", "shared": True})

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST

    def test_agent_not_owning_the_skill_is_invalid_request(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        with pytest.raises(RpcError) as excinfo:
            _skill_share(state, {"agent_id": "builder", "name": "notes", "shared": True})

        assert excinfo.value.code == RPC_ERROR_INVALID_REQUEST

    def test_unshare_passes_false_through(self, tmp_path: Path) -> None:
        state = _state(tmp_path)

        _skill_share(state, {"agent_id": "builder", "name": "deploy", "shared": False})

        assert state.runtime.policy_calls == [("set_shared", ("builder", "deploy", False))]


def test_manager_methods_are_registered() -> None:
    handlers = method_handlers()

    assert {
        "skill.inventory",
        "skill.set_disabled",
        "skill.share",
    } <= set(handlers)
