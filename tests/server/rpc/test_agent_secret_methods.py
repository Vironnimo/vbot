"""Agent Secret RPCs: values enter freely and leave only through ``reveal``."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.secrets._agent_secret_model import SecretRecordError
from core.secrets.agent_secrets import AgentSecrets, Lifetime
from core.secrets.requests import SecretRequests
from core.sessions import SessionAddress
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
    state.runtime.agent_secrets.substitute(
        SessionAddress(None, "main", "s1"), "{{secret:GITHUB.password}}", "typing"
    )
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
    assert (
        state.runtime.agent_secrets.lookup(SessionAddress(None, "main", "s1"), "STRIPE") is not None
    )
    assert stale["message"] == "This request is no longer waiting; the Agent stopped asking."
    assert (await rpc_result(state, "agent_secrets.list"))["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overlap", ["cancel", "answer", "decline", "replacement", "invalid", "timeout"]
)
async def test_an_accepted_answer_settles_once_before_another_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlap: str
) -> None:
    state = _state(tmp_path)
    requests = state.runtime.secret_requests
    secrets = state.runtime.agent_secrets
    address = SessionAddress(None, "main", "s1")
    secret = secrets.create(
        name="API", kind="token", values={"value": "old"}, actor="user", agents=["main"]
    )
    deadline = asyncio.timeout(None)
    if overlap == "timeout":
        timeout = asyncio.timeout
        monkeypatch.setattr(
            asyncio,
            "timeout",
            lambda delay: deadline if delay == requests._timeout else timeout(delay),
        )

    async def ask() -> Any:
        return await requests.request(
            agent_id="main",
            session_id="s1",
            project_id=None,
            name="API",
            kind="token",
            description="",
            lifetime="session",
        )

    asked = asyncio.create_task(ask())
    await asyncio.sleep(0)
    pending = requests.pending_for(address, "API")
    assert pending is not None
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    store = requests._store
    rejected = False

    def blocked_store(*args: Any) -> Any:
        nonlocal rejected
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        if overlap == "invalid" and not rejected:
            rejected = True
            raise SecretRecordError("The answer could not be stored.")
        return store(*args)

    monkeypatch.setattr(requests, "_store", blocked_store)
    call = rpc_error if overlap == "invalid" else rpc_result
    answer = asyncio.create_task(
        call(
            state,
            "agent_secrets.answer",
            id=pending.id,
            values={"value": PASSWORD},
        )
    )
    replacement = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        if overlap == "cancel":
            answer.cancel()
            await asyncio.sleep(0)
            answer.cancel()
            await asyncio.sleep(0)
            assert not answer.done()
        elif overlap == "timeout":
            deadline.reschedule(loop.time() - 1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not asked.done()
        elif overlap in {"answer", "decline"}:
            params = {"values": {"value": "competing"}} if overlap == "answer" else {}
            error = await rpc_error(state, f"agent_secrets.{overlap}", id=pending.id, **params)
            assert error["code"] == "invalid_request"
            assert not asked.done()
        elif overlap == "replacement":
            replacement = asyncio.create_task(ask())
            await asyncio.sleep(0)
            assert requests.pending_for(address, "API") is pending
            assert not asked.done()
    finally:
        release.set()
    if overlap == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await answer
    else:
        await answer
    if overlap == "invalid":
        assert requests.pending_for(address, "API") is pending
        await rpc_result(state, "agent_secrets.answer", id=pending.id, values={"value": PASSWORD})
    outcome = await asyncio.wait_for(asked, 2)
    assert outcome.status == "stored"
    assert outcome.secret == secrets.get(secret.id)
    current = secrets.get(secret.id)
    assert current is not None
    target = current.field_named("value")
    assert target is not None and secrets.field_value(current, target) == PASSWORD
    if replacement is not None:
        await asyncio.sleep(0)
        next_request = requests.pending_for(address, "API")
        assert next_request is not None and next_request.id != pending.id
        assert next_request.replaces == secret.id
        await rpc_result(state, "agent_secrets.decline", id=next_request.id)
        assert (await replacement).status == "declined"
    assert requests.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["give", "answer"])
@pytest.mark.parametrize("lifetime", ["session", "once", "permanent", "until", None])
async def test_replacing_a_secret_applies_the_chosen_lifetime_or_keeps_its_own(
    tmp_path: Path,
    path: str,
    lifetime: Lifetime | None,
) -> None:
    state = _state(tmp_path)
    secrets = state.runtime.agent_secrets
    state.runtime.chat_sessions.create("main", session_id="s1")
    address = SessionAddress(None, "main", "s1")
    child = SessionAddress(None, "main", "child")
    old = secrets.create(
        name="API",
        kind="token",
        values={"value": "old-credential"},
        actor="user",
        lifetime="until",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        agents=["main", "other"],
        session_id="s1",
    )
    secrets.pass_to_session(address, ["API"], child)
    expiry = datetime(2031, 1, 1, tzinfo=UTC) if lifetime == "until" else None
    params: dict[str, Any] = {"values": {"value": PASSWORD}}
    if lifetime is not None:
        params["lifetime"] = lifetime
    if expiry is not None:
        params["expires_at"] = expiry.isoformat()
    if path == "answer":
        requests = state.runtime.secret_requests
        task = asyncio.create_task(
            requests.request(
                agent_id="main",
                session_id="s1",
                project_id=None,
                name="API",
                kind="token",
                description="Sign in again",
                lifetime="session",
            )
        )
        await asyncio.sleep(0)
        pending = requests.pending_for(address, "API")
        assert pending is not None
        result = await rpc_result(state, "agent_secrets.answer", id=pending.id, **params)
        assert (await task).status == "stored"
    else:
        result = await rpc_result(
            state,
            "agent_secrets.give",
            agent_id="main",
            session_id="s1",
            name="API",
            kind="token",
            **params,
        )
    assert result["secret"]["id"] == old.id
    if path == "give":
        # The result says when the replacement changed the lifetime.
        assert result["previous_lifetime"] == (None if lifetime in {None, "until"} else "until")
    secrets.reload()
    updated = secrets.get(old.id)
    if lifetime is None:
        # Without a chosen lifetime the Secret keeps its own, its expiry and its grants
        # (the request suggested session).
        assert (updated.lifetime, updated.expires_at) == ("until", old.expires_at)
        assert updated.agents == old.agents
    else:
        assert updated.lifetime == lifetime and updated.expires_at == expiry
    assert updated.fields[0].value == PASSWORD
    assert secrets.lookup(address, "API") is not None
    local = lifetime in {"session", "once"}
    assert (secrets.lookup(child, "API", passed_only=True) is None) is local
    assert (secrets.lookup(SessionAddress(None, "other", "s1"), "API") is None) is local
    assert "old-credential" not in secrets.masker.mask("old-credential")


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
