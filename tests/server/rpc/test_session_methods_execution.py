"""Only the operator chooses the computers a Session and its first Run may use."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.computers import ComputerError, ComputerInfo
from core.sessions import SessionAddress
from core.subagents.links import link_session
from core.tools import ToolContext, tool_success
from server.access import bound_principal
from server.access._principal import HOST
from tests.core.chat.chat_loop_tools_test_support import final, tool_turn
from tests.server.rpc_test_support import StubAdapter, make_state, rpc_error, rpc_result

SERVER = {"computer_id": "server", "allowed_computer_ids": ["server"]}
LAPTOP = {"computer_id": "laptop", "allowed_computer_ids": ["laptop", "server"]}


@pytest.fixture(autouse=True)
def _usable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _state(tmp_path: Path, adapter: StubAdapter | None = None) -> Any:
    state = make_state(tmp_path, adapter or StubAdapter())

    def get(computer_id: str) -> ComputerInfo:
        if computer_id not in {"server", "laptop"}:
            raise ComputerError("computer_unknown", f"Unknown computer: {computer_id}")
        # An offline laptop with sharing currently disabled remains a known
        # selection. Actual Tool dispatch checks its current capability.
        return ComputerInfo(computer_id, computer_id, "win32", computer_id == "server", ())

    state.runtime.computers = SimpleNamespace(get=get)
    return state


@pytest.mark.asyncio
async def test_only_an_operator_can_grant_known_computers_to_an_idle_session(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    address = SessionAddress(None, "coder", "s1")
    state.runtime.chat_sessions.create("coder", session_id=address.session_id)
    params = {"agent_id": "coder", "session_id": address.session_id}
    got = await rpc_result(state, "session.get", **params)
    assert got["session"]["execution"] == SERVER

    refused = await rpc_error(state, "session.set_execution", **params, execution=LAPTOP)
    assert refused["code"] == "access_denied"
    state.runtime.chat_sessions.mutate_metadata(
        address, lambda metadata: metadata.update(execution={**SERVER, "future_field": True})
    )
    with bound_principal(HOST):
        changed = await rpc_result(state, "session.set_execution", **params, execution=LAPTOP)
        unknown = await rpc_error(
            state,
            "session.set_execution",
            **params,
            execution={"computer_id": "missing", "allowed_computer_ids": ["missing"]},
        )
        excluded = await rpc_error(
            state,
            "session.set_execution",
            **params,
            execution={"computer_id": "server", "allowed_computer_ids": ["laptop"]},
        )
    assert changed["execution"] == {**LAPTOP, "future_field": True}
    assert unknown["code"] == excluded["code"] == "invalid_request"
    assert state.runtime.chat_sessions.metadata_value(address, "execution") == {
        **LAPTOP,
        "future_field": True,
    }
    got = await rpc_result(state, "session.get", **params)
    assert got["session"]["execution"] == LAPTOP


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_new_session_selection_is_persisted_before_its_first_tool_call(
    tmp_path: Path, method: str
) -> None:
    state = _state(tmp_path, StubAdapter([tool_turn(("call_1", "probe")), final("Done")]))
    observed: list[dict[str, Any]] = []

    def probe(context: ToolContext, _arguments: Any) -> Any:
        observed.append(context.execution)
        assert (
            state.runtime.chat_sessions.metadata_value(
                SessionAddress(context.project_id, context.agent_id, context.session_id),
                "execution",
            )
            == LAPTOP
        )
        return tool_success({})

    state.runtime.tools.register("probe", "Probe.", {"type": "object"}, probe)
    params = {
        "agent_id": "coder",
        "content": "Work",
        "input_id": "first-input",
        "new_session": {"session_id": "s1", "execution": LAPTOP},
    }
    denied = await rpc_error(state, method, **params)
    assert denied["code"] == "access_denied"
    with bound_principal(HOST):
        submitted = await rpc_result(state, method, **params)
    await state.chat_runs.get(submitted["run_id"]).wait()
    assert observed == [LAPTOP]
    with bound_principal(HOST):
        repeated = await rpc_result(state, method, **params)
    assert repeated["run_id"] == submitted["run_id"]
    assert observed == [LAPTOP]


@pytest.mark.asyncio
async def test_creating_and_forking_a_session_preserve_its_operator_selection(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    params = {"agent_id": "coder", "session_id": "s1", "execution": LAPTOP}
    denied = await rpc_error(state, "session.create", **params)
    assert denied["code"] == "access_denied"
    with bound_principal(HOST):
        created = await rpc_result(state, "session.create", **params)
    assert created["execution"] == LAPTOP
    source = SessionAddress(None, "coder", "s1")
    fork = await state.runtime.chat_sessions.fork(source)
    result = await rpc_result(state, "session.get", agent_id="coder", session_id=fork.id)
    assert result["session"]["execution"] == LAPTOP


@pytest.mark.asyncio
async def test_operator_configuration_cannot_expand_a_linked_subagents_authority(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    sessions = state.runtime.chat_sessions
    parent = sessions.create("coder", session_id="parent").address
    child = sessions.create("coder", session_id="child").address
    link_session(
        sessions,
        child,
        subagent_id="sub-one",
        parent=parent,
        run_id="parent-run",
        tool_call_id="call-one",
        tool_call_index=0,
    )
    with bound_principal(HOST):
        refused = await rpc_error(
            state, "session.set_execution", agent_id="coder", session_id="child", execution=LAPTOP
        )
        allowed = await rpc_result(
            state, "session.set_execution", agent_id="coder", session_id="child", execution=SERVER
        )
    assert refused["code"] == "invalid_request"
    assert allowed["execution"] == SERVER


@pytest.mark.asyncio
async def test_active_work_cannot_be_retargeted_and_existing_sends_keep_the_policy(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(block=True)
    state = _state(tmp_path, adapter)
    params = {"agent_id": "coder", "session_id": "s1"}
    with bound_principal(HOST):
        await rpc_result(state, "session.create", **params, execution=LAPTOP)
        submitted = await rpc_result(
            state, "chat.stream", **params, content="Work", input_id="active-input"
        )
        refused = await rpc_error(state, "session.set_execution", **params, execution=SERVER)
        ignored = await rpc_error(
            state,
            "chat.stream",
            **params,
            content="Change silently",
            input_id="bad-input",
            execution=SERVER,
        )
    try:
        assert refused["code"] == "session_busy"
        assert ignored["code"] == "invalid_request"
        got = await rpc_result(state, "session.get", **params)
        assert got["session"]["execution"] == LAPTOP
    finally:
        adapter.release.set()
        await state.chat_runs.get(submitted["run_id"]).wait()
