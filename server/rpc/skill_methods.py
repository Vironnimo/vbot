"""Skill mutation RPC handlers.

Write data-dir skill scopes for the UI/accessors: ``global`` (the user-curated
``<data_dir>/skills``) or ``agent:<agent_id>`` (a chosen agent's private home).
Never the project/repo scope — those are repo files authored with the ordinary
file tools. All writes go through the one validated authoring service with
``author="human"`` provenance, then the matching scoped invalidation so the change
is live without a restart. Validation failures surface the authoring diagnostics as
an ``invalid_request`` error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from core.settings import is_valid_agent_id
from core.skills import SkillAuthoringError, SkillRegistry, SkillWriteResult
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _optional_string, _required_string

JsonObject = dict[str, Any]

_GLOBAL_SCOPE = "global"
_AGENT_SCOPE_PREFIX = "agent:"
_HUMAN_AUTHOR = "human"


def _validated_scope(params: JsonObject) -> str:
    """Return the request's ``scope``, rejecting anything but global / agent:<id>.

    The ``agent:<id>`` id becomes a filesystem path segment, so it is validated with
    the canonical (traversal-safe) agent-id rule before any path is built. A project
    or repo scope is rejected here — v1 write surfaces never target the repo.
    """
    scope = _required_string(params, "scope")
    if scope == _GLOBAL_SCOPE:
        return scope
    if scope.startswith(_AGENT_SCOPE_PREFIX):
        agent_id = scope[len(_AGENT_SCOPE_PREFIX) :]
        if not is_valid_agent_id(agent_id):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"invalid agent scope id: {agent_id!r}")
        return scope
    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        f"unsupported skill scope: {scope!r} (use 'global' or 'agent:<id>')",
    )


def _scope_root(state: Any, scope: str) -> Path:
    if scope == _GLOBAL_SCOPE:
        return cast(Path, state.runtime.global_skills_dir)
    return cast(Path, state.runtime.agent_skills_dir(scope[len(_AGENT_SCOPE_PREFIX) :]))


def _invalidate_scope(state: Any, scope: str) -> None:
    if scope == _GLOBAL_SCOPE:
        # A global write changes the shared pool every project/agent registry layers
        # over, so reload the whole registry (which also drops those caches).
        state.runtime.reload_skills()
    else:
        state.runtime.invalidate_agent_skills(scope[len(_AGENT_SCOPE_PREFIX) :])


def _write(state: Any, scope: str, write: Callable[[Path], SkillWriteResult]) -> JsonObject:
    """Run one authoring write, map its diagnostics to an RpcError, then invalidate."""
    try:
        result = write(_scope_root(state, scope))
    except SkillAuthoringError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "; ".join(exc.diagnostics)) from exc
    except OSError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    _invalidate_scope(state, scope)
    return {"name": result.name, "operation": result.operation, "warnings": list(result.warnings)}


def _skill_read(state: Any, params: JsonObject) -> JsonObject:
    """Return the editable skills of one scope, each with its full ``SKILL.md`` text.

    Scans only the scope's own directory (the data-dir global pool or an agent's
    private home), so bundled and project skills never appear — those are not
    editable here. The content lets the UI view and pre-fill an edit form.
    """
    scope = _validated_scope(params)
    registry = SkillRegistry.load(_scope_root(state, scope))
    skills: list[JsonObject] = []
    for skill in registry.list_all():
        try:
            content = skill.path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        skills.append({"name": skill.name, "description": skill.description, "content": content})
    return {"skills": skills}


def _skill_create(state: Any, params: JsonObject) -> JsonObject:
    scope = _validated_scope(params)
    name = _required_string(params, "name")
    content = _required_string(params, "content")
    source = _optional_string(params, "source")
    return _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.create(
            root, name, content, author=_HUMAN_AUTHOR, source=source
        ),
    )


def _skill_update(state: Any, params: JsonObject) -> JsonObject:
    scope = _validated_scope(params)
    name = _required_string(params, "name")
    content = _required_string(params, "content")
    source = _optional_string(params, "source")
    return _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.edit(
            root, name, content, author=_HUMAN_AUTHOR, source=source
        ),
    )


def _skill_delete(state: Any, params: JsonObject) -> JsonObject:
    scope = _validated_scope(params)
    name = _required_string(params, "name")
    return _write(state, scope, lambda root: state.runtime.skill_authoring.delete(root, name))


def _skill_write_file(state: Any, params: JsonObject) -> JsonObject:
    scope = _validated_scope(params)
    name = _required_string(params, "name")
    path = _required_string(params, "path")
    content = params.get("content")
    if not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    return _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.write_file(root, name, path, content),
    )


def _skill_remove_file(state: Any, params: JsonObject) -> JsonObject:
    scope = _validated_scope(params)
    name = _required_string(params, "name")
    path = _required_string(params, "path")
    return _write(
        state, scope, lambda root: state.runtime.skill_authoring.remove_file(root, name, path)
    )


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the skill mutation RPC handlers."""
    return {
        "skill.read": _skill_read,
        "skill.create": _skill_create,
        "skill.update": _skill_update,
        "skill.delete": _skill_delete,
        "skill.write_file": _skill_write_file,
        "skill.remove_file": _skill_remove_file,
    }
