from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.tools._bash_update_handoff import (
    acknowledge_update_handoff_ticket,
    create_update_handoff_ticket,
)
from server.rpc.application_methods import method_handlers
from server.rpc.dispatcher import dispatch_method
from server.rpc.errors import RpcError


class BootstrapStub:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    def list_jobs(self):
        return list(self.jobs)

    def create_job(self, **fields):
        job = SimpleNamespace(id="boot-one", **fields)
        self.jobs.append(job)
        return job


def make_state(tmp_path: Path, *, acknowledged: bool = True):
    ticket = create_update_handoff_ticket(
        tmp_path,
        run_id="run-one",
        tool_call_id="call-one",
        agent_id="main",
        project_id=None,
        session_id="session-one",
    )
    if acknowledged:
        acknowledge_update_handoff_ticket(ticket)
    assistant = SimpleNamespace(
        role="assistant", tool_calls=[SimpleNamespace(id="call-one")], tool_call_id=None
    )
    tool = SimpleNamespace(role="tool", tool_calls=None, tool_call_id="call-one")
    session = SimpleNamespace(load=lambda: [assistant, tool])
    bootstrap = BootstrapStub()
    runtime = SimpleNamespace(
        storage=SimpleNamespace(data_dir=tmp_path),
        chat_sessions=SimpleNamespace(get=lambda _address: session),
        bootstrap_service=bootstrap,
    )
    runs = SimpleNamespace(
        maintenance_begin=AsyncMock(return_value={"safe_to_stop": True}),
        maintenance_status=AsyncMock(return_value={"safe_to_stop": True}),
        maintenance_end=AsyncMock(return_value={"active": False}),
        active_run=lambda **_kwargs: None,
        cancel=AsyncMock(),
    )
    return (
        SimpleNamespace(runtime=runtime, chat_runs=runs, control_token="secret"),
        ticket,
        bootstrap,
    )


@pytest.mark.asyncio
async def test_private_maintenance_derives_origin_from_acknowledged_ticket(tmp_path: Path) -> None:
    state, ticket, _bootstrap = make_state(tmp_path)
    result = await dispatch_method(
        state,
        "application.maintenance_begin",
        {
            "control_token": "secret",
            "operation_id": "operation-one",
            "handoff_ticket_id": ticket.ticket_id,
        },
        method_handlers(),
    )
    assert result == {"safe_to_stop": True}
    origin = state.chat_runs.maintenance_begin.await_args.kwargs["origin"]
    assert origin[0].session_id == "session-one"
    assert origin[1] == "run-one"


@pytest.mark.asyncio
async def test_private_maintenance_cancels_only_the_exact_acknowledged_origin(
    tmp_path: Path,
) -> None:
    state, ticket, _bootstrap = make_state(tmp_path)
    exact = SimpleNamespace(id="run-one")
    state.chat_runs.active_run = lambda **_kwargs: exact

    await dispatch_method(
        state,
        "application.maintenance_begin",
        {
            "control_token": "secret",
            "operation_id": "operation-one",
            "handoff_ticket_id": ticket.ticket_id,
        },
        method_handlers(),
    )

    state.chat_runs.cancel.assert_awaited_once_with("run-one", reason="application_update")
    state.chat_runs.maintenance_status.assert_awaited_once_with("operation-one")


@pytest.mark.asyncio
async def test_private_maintenance_never_cancels_a_different_active_run(tmp_path: Path) -> None:
    state, ticket, _bootstrap = make_state(tmp_path)
    state.chat_runs.active_run = lambda **_kwargs: SimpleNamespace(id="run-later")

    await dispatch_method(
        state,
        "application.maintenance_begin",
        {
            "control_token": "secret",
            "operation_id": "operation-one",
            "handoff_ticket_id": ticket.ticket_id,
        },
        method_handlers(),
    )

    state.chat_runs.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_maintenance_requires_durable_acknowledgement_before_cancel(
    tmp_path: Path,
) -> None:
    state, ticket, _bootstrap = make_state(tmp_path, acknowledged=False)
    state.chat_runs.active_run = lambda **_kwargs: SimpleNamespace(id="run-one")

    with pytest.raises(RpcError, match="durably acknowledged"):
        await dispatch_method(
            state,
            "application.maintenance_begin",
            {
                "control_token": "secret",
                "operation_id": "operation-one",
                "handoff_ticket_id": ticket.ticket_id,
            },
            method_handlers(),
        )

    state.chat_runs.maintenance_begin.assert_not_awaited()
    state.chat_runs.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_application_rpc_rejects_wrong_control_token(tmp_path: Path) -> None:
    state, _ticket, _bootstrap = make_state(tmp_path)
    with pytest.raises(RpcError):
        await dispatch_method(
            state,
            "application.maintenance_status",
            {"control_token": "wrong", "operation_id": "operation-one"},
            method_handlers(),
        )


@pytest.mark.asyncio
async def test_update_continuation_is_unique_across_receipt_retry(tmp_path: Path) -> None:
    state, ticket, bootstrap = make_state(tmp_path)
    params = {
        "control_token": "secret",
        "operation_id": "operation-one",
        "handoff_ticket_id": ticket.ticket_id,
    }
    first = await dispatch_method(
        state, "application.update_continuation", params, method_handlers()
    )
    second = await dispatch_method(
        state, "application.update_continuation", params, method_handlers()
    )
    assert first["created"] is True
    assert second == {
        "operation_id": "operation-one",
        "bootstrap_job_id": "boot-one",
        "created": False,
    }
    assert len(bootstrap.jobs) == 1
