"""Agent Secret RPCs: values enter freely and leave only through ``reveal``."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.secrets.agent_secrets import AgentSecrets
from core.secrets.requests import SecretRequests
from server.app import _publish_secret_changes
from tests.core.secrets.secrets_test_support import data_directory, open_store
from tests.core.secrets.test_agent_secrets import Clock, FakeSource
from tests.server.rpc_test_support import (
    StubAdapter,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

PASSWORD = "hunter2-very-secret"


def _state(tmp_path: Path) -> Any:
    state = make_state(tmp_path, StubAdapter())
    clock = Clock()
    secrets = AgentSecrets(open_store(tmp_path, data_directory(tmp_path, "secrets")), clock=clock)
    secrets.reload()
    state.runtime.agent_secrets = secrets
    state.runtime.secret_requests = SecretRequests(secrets, clock=clock)
    _publish_secret_changes(SimpleNamespace(state=state), state.runtime)
    return state


@pytest.mark.asyncio
async def test_secrets_are_managed_and_only_reveal_returns_a_concealed_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    state = _state(tmp_path)

    created = await rpc_result(
        state,
        "agent_secrets.create",
        name="github",
        kind="login",
        values={"username": "julian", "password": PASSWORD},
        agents=["main"],
        description="GitHub account",
    )
    secret_id = created["secret"]["id"]
    state.runtime.agent_secrets.substitute("main", "s1", "{{secret:GITHUB.password}}", "typing")
    listed = await rpc_result(state, "agent_secrets.list")
    updated = await rpc_result(
        state, "agent_secrets.update", id=secret_id, sinks=["terminal"], agents=["main", "coder"]
    )
    revealed = await rpc_result(state, "agent_secrets.reveal", id=secret_id, field="password")
    invalid = await rpc_error(
        state, "agent_secrets.create", name="other", kind="token", values={"value": [PASSWORD]}
    )
    deleted = await rpc_result(state, "agent_secrets.delete", id=secret_id)

    [secret] = listed["secrets"]
    assert secret["name"] == "GITHUB"
    assert secret["fields"] == [
        {"name": "username", "concealed": False, "reference": "{{secret:GITHUB.username}}"}
        | {"value": "julian"},
        {"name": "password", "concealed": True, "reference": "{{secret:GITHUB.password}}"},
    ]
    assert [(use["field"], use["sink"], use["agent_id"]) for use in secret["recent_uses"]] == [
        ("password", "typing", "main")
    ]
    assert listed["requests"] == []
    assert updated["secret"]["sinks"] == ["terminal"]
    assert updated["secret"]["agents"] == ["main", "coder"]
    assert revealed == {"id": secret_id, "field": "password", "value": PASSWORD}
    assert invalid["message"] == "params.values must map field names to strings"
    assert deleted == {"id": secret_id, "deleted": True}
    assert (await rpc_result(state, "agent_secrets.list"))["secrets"] == []
    assert resource_changes(state, "agent_secrets") == [{"kind": "agent_secrets"}] * 3
    outside_reveal = json.dumps([created, listed, updated, invalid, deleted])
    assert PASSWORD not in outside_reveal
    assert PASSWORD not in caplog.text


@pytest.mark.asyncio
async def test_answering_a_request_stores_the_value_for_the_waiting_agent(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    requests = state.runtime.secret_requests

    async def ask(name: str) -> Any:
        return await requests.request(
            agent_id="main",
            session_id="s1",
            project_id=None,
            name=name,
            kind="token",
            description="Stripe test key",
            lifetime="session",
        )

    answered = asyncio.create_task(ask("STRIPE"))
    declined = asyncio.create_task(ask("OPENAI"))
    given = asyncio.create_task(ask("GITHUB"))
    await asyncio.sleep(0)
    pending = {
        item["name"]: item for item in (await rpc_result(state, "agent_secrets.list"))["requests"]
    }

    stored = await rpc_result(
        state, "agent_secrets.answer", id=pending["STRIPE"]["id"], values={"value": PASSWORD}
    )
    await rpc_result(state, "agent_secrets.decline", id=pending["OPENAI"]["id"])
    # The /secret form answers a pending request of the same name in the Session.
    gave = await rpc_result(
        state,
        "agent_secrets.give",
        agent_id="main",
        session_id="s1",
        name="github",
        kind="token",
        values={"value": PASSWORD},
    )
    stale = await rpc_error(state, "agent_secrets.decline", id=pending["OPENAI"]["id"])

    assert pending["STRIPE"]["fields"] == [{"name": "value", "concealed": True, "optional": False}]
    assert (await answered).status == "stored"
    assert (await declined).status == "declined"
    assert (await given).status == "stored"
    assert gave["replaced"] is False
    assert stored["secret"]["lifetime"] == "session"
    assert stored["secret"]["session_id"] == "s1"
    assert PASSWORD not in json.dumps([stored, gave])
    assert state.runtime.agent_secrets.lookup("main", "s1", "STRIPE") is not None
    assert stale["message"] == "This request is no longer waiting; the Agent stopped asking."
    assert (await rpc_result(state, "agent_secrets.list"))["requests"] == []


@pytest.mark.asyncio
async def test_sources_are_unlocked_locked_and_reported_by_state(tmp_path: Path) -> None:
    state = _state(tmp_path)
    source = FakeSource({"password": PASSWORD})
    state.runtime.agent_secrets.replace_sources([source])

    locked = await rpc_result(state, "agent_secrets.source_lock", source="vault")
    unlocked = await rpc_result(
        state, "agent_secrets.source_unlock", source="vault", inputs={"password": "master"}
    )
    missing = await rpc_error(state, "agent_secrets.source_lock", source="keepass")
    [listed] = (await rpc_result(state, "agent_secrets.list"))["sources"]

    assert locked["state"] == "locked"
    assert unlocked["state"] == "available"
    assert listed == {
        "name": "vault",
        "label": "Vault",
        "state": "available",
        "reason": None,
        "capabilities": [],
        "unlock_fields": [
            {"name": "password", "label": "Password", "concealed": True, "required": True}
        ],
    }
    assert missing["message"] == "keepass is not an available Secret Source"
    assert resource_changes(state, "agent_secrets") == [{"kind": "agent_secrets"}] * 2
