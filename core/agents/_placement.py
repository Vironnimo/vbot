"""Placement validation, owner proof and delegation rules owned by AgentStore."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.config_validation import JsonDiagnostic, add_error, error_diagnostic
from core.settings import is_valid_agent_id
from core.tools.availability import subagent_allowed_agents
from core.utils.timestamps import parse_timestamp

from . import _delegation
from ._types import Agent, AgentError, AgentPlacement, AgentPlacementError

if TYPE_CHECKING:
    from .agents import AgentStore


def validate_placement(value: Any, *, agent_id: Any, builtin: Any) -> list[JsonDiagnostic]:
    """Unknown lock fields invalidate the document instead of opening access."""
    if not isinstance(value, dict):
        return [error_diagnostic("$.placement", "must be an object")]
    diagnostics: list[JsonDiagnostic] = []
    if set(value) - {"kind", "owner_id", "owner_binding"}:
        add_error(diagnostics, "$.placement", "contains an unknown field")
    kind = value.get("kind")
    if kind not in ("helper", "private"):
        add_error(diagnostics, "$.placement", "kind must be helper or private")
    elif kind == "helper":
        if "owner_id" in value or "owner_binding" in value:
            add_error(diagnostics, "$.placement", "a Helper has no owner fields")
    else:
        owner = value.get("owner_id")
        if not isinstance(owner, str) or not is_valid_agent_id(owner) or owner == agent_id:
            add_error(diagnostics, "$.placement", "owner_id must name another Identity Agent")
        try:
            binding = value.get("owner_binding")
            if not isinstance(binding, str):
                raise ValueError("missing owner binding")
            parse_timestamp(binding)
        except TypeError, ValueError:
            add_error(diagnostics, "$.placement", "owner_binding must be a creation timestamp")
    if builtin is not None:
        add_error(diagnostics, "$.placement", "a built-in Agent cannot have placement")
    return diagnostics


def verified_owner(agent: Agent | None, find: Callable[[str], Agent | None]) -> Agent | None:
    placement = agent.placement if agent else None
    if placement is None or placement.kind != "private" or placement.owner_id is None:
        return None
    owner = find(placement.owner_id)
    if (
        owner is not None
        and owner.builtin is None
        and owner.placement is None
        and owner.created_at == placement.owner_binding
    ):
        return owner
    return None


def resolved(agent: Agent, find: Callable[[str], Agent | None]) -> dict[str, Any]:
    placement = agent.placement
    if placement is None:
        return {"kind": "chat"}
    if placement.kind == "helper":
        return {"kind": "helper"}
    return {
        "kind": "private",
        "owner_id": placement.owner_id,
        "orphaned": verified_owner(agent, find) is None,
    }


def target_allowed(
    caller: Agent | None,
    target: Agent | None,
    find: Callable[[str], Agent | None],
    *,
    listed: bool,
    wildcard: bool,
) -> bool:
    if target is None:
        return listed or wildcard
    if caller is not None and caller.id == target.id:
        return True
    target_owner = verified_owner(target, find)
    caller_owner = verified_owner(caller, find)
    if target.placement is not None and target.placement.kind == "private":
        return (
            target_owner is not None
            and caller is not None
            and (
                caller.id == target_owner.id
                or (caller_owner is not None and caller_owner.id == target_owner.id)
            )
        )
    if caller_owner is not None and target.id == caller_owner.id:
        return False
    if target.placement is not None:
        return listed
    return listed or wildcard


def private_agents(owner_id: str, roster: Iterable[Agent]) -> list[Agent]:
    identities = {agent.id: agent for agent in roster}
    return [
        agent
        for agent in identities.values()
        if (owner := verified_owner(agent, identities.get)) is not None and owner.id == owner_id
    ]


def automatic_targets(caller_id: str, roster: Iterable[Agent]) -> list[str]:
    identities = {agent.id: agent for agent in roster}
    caller = identities.get(caller_id)
    if caller is None:
        return []
    owner = caller if caller.placement is None else verified_owner(caller, identities.get)
    if owner is None:
        return []
    return [
        agent.id for agent in private_agents(owner.id, identities.values()) if agent.id != caller_id
    ]


def transition(
    store: AgentStore, agent_id: str, placement: AgentPlacement | None, previous: Agent | None
) -> AgentPlacement | None:
    if placement is not None and not isinstance(placement, AgentPlacement):
        raise AgentError("placement must be an AgentPlacement or None")
    if placement is not None and placement.kind not in ("helper", "private"):
        raise AgentError("placement kind must be helper or private")
    if previous is not None and (
        (previous.placement is None and placement is None)
        or (
            previous.placement is not None
            and placement is not None
            and previous.placement.kind == placement.kind
            and previous.placement.owner_id == placement.owner_id
        )
    ):
        return previous.placement
    if placement is not None and previous is not None:
        roster = store.list()
        if private_agents(agent_id, roster):
            raise AgentPlacementError("has_private_agents", "Move its private Agents first")
        if previous.placement is None and not any(
            agent.id != agent_id and agent.placement is None for agent in roster
        ):
            raise AgentPlacementError("last_chat_agent", "At least one Agent stays in chat")
    if placement is None:
        return None
    if placement.kind == "helper":
        if placement.owner_id is not None or placement.owner_binding is not None:
            raise AgentError("a Helper has no owner fields")
        return placement
    owner = store.find(placement.owner_id) if placement.owner_id else None
    if (
        owner is None
        or owner.id == agent_id
        or owner.builtin is not None
        or owner.placement is not None
    ):
        raise AgentPlacementError("owner_invalid", "This Agent cannot be an owner")
    stamp = _delegation._stamp(store, owner.id)
    assert stamp is not None
    return AgentPlacement("private", owner.id, stamp)


def reject_new_private_grants(store: AgentStore, agent: Agent, previous: Agent | None) -> None:
    before = set(subagent_allowed_agents(previous.tools)) if previous is not None else set()
    for target_id in set(subagent_allowed_agents(agent.tools)) - before:
        if not is_valid_agent_id(target_id):
            continue
        if verified_owner(store.find(target_id), store.find) is not None:
            raise AgentError(
                "Private Agents are available automatically within their owner's group; "
                "no grant can be added"
            )


def retarget_owner(store: AgentStore, old_id: str, new_id: str, binding: str | None) -> None:
    if binding is None:
        return
    for agent in store.list():
        placement = agent.placement
        if (
            placement is not None
            and placement.kind == "private"
            and placement.owner_id == old_id
            and placement.owner_binding == binding
        ):
            store._write_agent(replace(agent, placement=replace(placement, owner_id=new_id)))
