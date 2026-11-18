"""Actual identity management remains scoped to its creating Extension."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agents import AgentError, AgentStore
from core.database import write_bootstrap_marker
from core.runtime._extension_identities import ExtensionIdentityAgents
from core.sessions import ChatSessionManager


@pytest.mark.asyncio
async def test_extension_identity_management_preserves_incarnation_and_creation_ownership(
    tmp_path: Path,
):
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    store = AgentStore(tmp_path, sessions=sessions)
    try:
        # Generation-1 id-only Agent files are valid; the first explicit attach
        # persists its proof rather than exposing the read-time clock fallback.
        path = tmp_path / "agents" / "existing" / "agent.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"format_version": 1, "id": "existing"}))
        current = True

        def require_current(_identity):
            if not current:
                raise ValueError("retired")

        host = ExtensionIdentityAgents(store, SimpleNamespace(name="factory"), require_current)
        attached = await host.get("existing")
        assert attached == await host.get("existing")
        assert json.loads(path.read_text())["created_at"] == attached["incarnation"]
        with pytest.raises(AgentError):
            await host.update(
                "existing", expected_incarnation=attached["incarnation"], name="changed"
            )
        created = await host.create("specialist", request_id="create-specialist", name="Specialist")
        assert created == await host.create(
            "specialist", request_id="create-specialist", name="Specialist"
        )
        with pytest.raises(AgentError):
            await host.create("specialist", request_id="create-specialist", name="Wrong")
        assert created["created_by_owner"] is True
        assert attached["extension_owner"] is None and created["extension_owner"] == "factory"
        profiles = await host.profiles(["existing", "missing", "specialist"])
        assert set(profiles) == {"existing", "specialist"}
        assert profiles["specialist"] == {
            "name": "Specialist",
            "incarnation": created["incarnation"],
            "extension_owner": "factory",
            "created_by_owner": True,
            "creation_request_id": "create-specialist",
            "placement": {"kind": "chat"},
        }
        with pytest.raises(AgentError):
            store.rename("specialist", "renamed")
        changed = await host.update(
            "specialist", expected_incarnation=created["incarnation"], name="Editor"
        )
        assert changed["name"] == "Editor"
        assert changed["incarnation"] == created["incarnation"]
        with pytest.raises(AgentError):
            await host.update("specialist", expected_incarnation="stale", name="wrong")
        current = False
        with pytest.raises(ValueError):
            await host.create("late")
        with pytest.raises(ValueError):
            await host.profiles(["specialist"])
        assert not store.exists("late")
    finally:
        store.close()
        sessions.close()
