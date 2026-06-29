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


def _skill_md(name: str = "demo", description: str = "Do a demo task.", body: str = "# Demo\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _SkillRuntime:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.global_skills_dir = root / "skills"
        self.skill_authoring = SkillAuthoringService(
            protected_roots=[root / "resources" / "skills"]
        )
        self.reload_calls = 0
        self.invalidated: list[str] = []

    def agent_skills_dir(self, agent_id: str) -> Path:
        return self._root / "agents" / agent_id / "skills"

    def reload_skills(self) -> None:
        self.reload_calls += 1

    def invalidate_agent_skills(self, agent_id: str) -> None:
        self.invalidated.append(agent_id)


def _state(tmp_path: Path) -> Any:
    return SimpleNamespace(runtime=_SkillRuntime(tmp_path))


def test_create_global_writes_and_reloads(tmp_path: Path) -> None:
    state = _state(tmp_path)

    result = _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})

    assert result["name"] == "demo"
    assert (state.runtime.global_skills_dir / "demo" / "SKILL.md").is_file()
    assert state.runtime.reload_calls == 1
    assert state.runtime.invalidated == []


def test_create_agent_writes_and_invalidates(tmp_path: Path) -> None:
    state = _state(tmp_path)

    _skill_create(state, {"scope": "agent:builder", "name": "demo", "content": _skill_md()})

    assert (state.runtime.agent_skills_dir("builder") / "demo" / "SKILL.md").is_file()
    assert state.runtime.invalidated == ["builder"]
    assert state.runtime.reload_calls == 0


def test_create_records_human_provenance(tmp_path: Path) -> None:
    state = _state(tmp_path)

    _skill_create(
        state, {"scope": "global", "name": "demo", "content": _skill_md(), "source": "wiki"}
    )

    skill = SkillRegistry.load(state.runtime.global_skills_dir).get("demo")
    assert skill.metadata["vbot"]["author"] == "human"
    assert skill.metadata["vbot"]["source"] == "wiki"


def test_update_rewrites_skill(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})

    _skill_update(
        state, {"scope": "global", "name": "demo", "content": _skill_md(description="Updated.")}
    )

    assert SkillRegistry.load(state.runtime.global_skills_dir).get("demo").description == "Updated."


def test_delete_removes_skill(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md()})

    _skill_delete(state, {"scope": "global", "name": "demo"})

    assert not (state.runtime.global_skills_dir / "demo").exists()


def test_write_and_remove_support_file(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _skill_create(state, {"scope": "agent:builder", "name": "demo", "content": _skill_md()})

    _skill_write_file(
        state,
        {"scope": "agent:builder", "name": "demo", "path": "scripts/run.py", "content": "x = 1\n"},
    )
    resource = state.runtime.agent_skills_dir("builder") / "demo" / "scripts" / "run.py"
    assert resource.is_file()

    _skill_remove_file(
        state, {"scope": "agent:builder", "name": "demo", "path": "scripts/run.py"}
    )
    assert not resource.exists()


def test_project_scope_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError) as exc:
        _skill_create(state, {"scope": "project:vbot", "name": "demo", "content": _skill_md()})

    assert exc.value.code == RPC_ERROR_INVALID_REQUEST
    assert "scope" in exc.value.message


def test_invalid_agent_scope_id_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError) as exc:
        _skill_create(state, {"scope": "agent:../escape", "name": "demo", "content": _skill_md()})

    assert exc.value.code == RPC_ERROR_INVALID_REQUEST


def test_bad_content_returns_authoring_diagnostics(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError) as exc:
        _skill_create(
            state,
            {"scope": "global", "name": "demo", "content": "---\nname: demo\n---\n\nbody\n"},
        )

    assert exc.value.code == RPC_ERROR_INVALID_REQUEST
    assert "description" in exc.value.message
    # A rejected write does not invalidate anything.
    assert state.runtime.reload_calls == 0


def test_missing_scope_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(RpcError):
        _skill_create(state, {"name": "demo", "content": _skill_md()})


def test_read_returns_scope_skills_with_content(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _skill_create(state, {"scope": "global", "name": "demo", "content": _skill_md(body="# Demo\nGo.")})

    result = _skill_read(state, {"scope": "global"})

    assert [skill["name"] for skill in result["skills"]] == ["demo"]
    entry = result["skills"][0]
    assert entry["description"] == "Do a demo task."
    assert "# Demo\nGo." in entry["content"]


def test_read_empty_scope_returns_no_skills(tmp_path: Path) -> None:
    state = _state(tmp_path)

    assert _skill_read(state, {"scope": "agent:builder"})["skills"] == []


def test_method_handlers_registered() -> None:
    handlers = method_handlers()
    assert set(handlers) == {
        "skill.read",
        "skill.create",
        "skill.update",
        "skill.delete",
        "skill.write_file",
        "skill.remove_file",
    }
