"""Identity placement contracts through the Agent store."""

from dataclasses import replace

import pytest

from core.agents import AgentError, AgentPlacement, AgentPlacementError, AgentStore
from core.tools.availability import subagent_allowed_agents
from tests.core.agents.agents_test_support import persisted, rewrite
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir


@pytest.mark.parametrize(
    ("caller", "target", "listed", "wildcard", "allowed"),
    [
        ("owner", "private", False, False, True),
        ("sibling", "private", False, False, True),
        ("outsider", "private", True, True, False),
        ("temporary", "private", True, True, False),
        ("private", "owner", True, True, False),
        ("private", "outsider", True, False, True),
        ("private", "private", False, False, True),
        ("owner", "helper", False, True, False),
        ("owner", "helper", True, True, True),
        ("owner", "outsider", False, True, True),
        ("owner", "outsider", False, False, False),
        ("owner", "unknown", True, False, True),
        ("owner", "unknown", False, True, True),
        ("owner", "orphan", True, True, False),
        ("orphan", "private", True, True, False),
        ("orphan", "outsider", True, False, True),
    ],
)
def test_placement_target_rule(store: AgentStore, caller, target, listed, wildcard, allowed):
    store.create("owner")
    store.create("outsider")
    store.create("helper", placement=AgentPlacement("helper"))
    for name in ("private", "sibling", "orphan"):
        store.create(name, placement=AgentPlacement("private", "owner"))
    orphan = store.get("orphan")
    store._write_agent(
        replace(orphan, placement=AgentPlacement("private", "owner", "2020-01-01T00:00:00Z"))
    )
    assert (
        store.placement_target_allowed(caller, target, listed=listed, wildcard=wildcard) is allowed
    )
    roster = store.list()
    assert (
        store.placement_target_allowed(
            caller, target, listed=listed, wildcard=wildcard, roster=roster
        )
        is allowed
    )
    assert store.automatic_targets("owner", roster) == ["private", "sibling"]
    assert store.automatic_targets("private", roster) == ["sibling"]
    assert store.automatic_targets("orphan", roster) == []
    assert store.automatic_targets("temporary", roster) == []
    assert [agent.id for agent in store.private_agents("owner", roster)] == ["private", "sibling"]


def test_placement_transitions_defaults_and_private_grants(store: AgentStore):
    owner = store.create("owner")
    helper = store.create("helper", placement=AgentPlacement("helper"))
    assert subagent_allowed_agents(owner.tools) == ["*"]
    assert subagent_allowed_agents(helper.tools) == []
    with pytest.raises(AgentPlacementError) as refused:
        store.update("owner", placement=AgentPlacement("helper"))
    assert refused.value.reason == "last_chat_agent"
    outsider = store.create("outsider", tools={"subagent": {"allowed_agents": ["*", "helper"]}})
    store.update("helper", subagent_allow=["helper"])
    private = store.update("helper", placement=AgentPlacement("private", "owner"))
    assert subagent_allowed_agents(store.get("outsider").tools) == ["*"]
    assert subagent_allowed_agents(private.tools) == ["helper"]
    assert subagent_allowed_agents(store.get("helper").tools) == ["helper"]
    assert store.resolved_placement(private) == {
        "kind": "private",
        "owner_id": "owner",
        "orphaned": False,
    }
    assert persisted(store, "helper")["placement"]["owner_binding"] == owner.created_at
    assert (
        store.update("helper", placement=AgentPlacement("private", "owner")).placement
        == private.placement
    )
    with pytest.raises(AgentPlacementError) as refused:
        store.update("owner", placement=AgentPlacement("helper"))
    assert refused.value.reason == "has_private_agents"
    for holder in ("owner", "outsider"):
        with pytest.raises(AgentError):
            store.update(holder, subagent_allow=["helper"])
    # An old, bound literal entry is tolerated during unrelated edits and stays inert.
    store._write_agent(outsider)
    assert store.update("outsider", name="Other").name == "Other"
    assert not store.placement_target_allowed("outsider", "helper", listed=True, wildcard=True)
    store.update("helper", placement=None)
    assert "placement" not in persisted(store, "helper")
    assert subagent_allowed_agents(store.get("helper").tools) == ["helper"]


@pytest.mark.parametrize("owner_id", ["missing", "private", "helper", "self", "librarian"])
def test_private_owner_must_be_another_chat_identity(store: AgentStore, owner_id):
    store.create("owner")
    store.create("helper", placement=AgentPlacement("helper"))
    store.create("private", placement=AgentPlacement("private", "owner"))
    with pytest.raises(AgentPlacementError) as refused:
        store.create("self", placement=AgentPlacement("private", owner_id))
    assert refused.value.reason == "owner_invalid"
    assert not store.exists("self")


def test_private_owner_proof_is_not_inherited_by_a_replacement(store: AgentStore):
    store.create("owner")
    private = store.create("private", placement=AgentPlacement("private", "owner"))
    rewrite(store, "owner", created_at="2020-01-01T00:00:00Z")
    assert store.resolved_placement(private)["orphaned"]
    assert not store.placement_target_allowed("owner", "private", listed=True, wildcard=True)
    assert store.automatic_targets("owner") == []
