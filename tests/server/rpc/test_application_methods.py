import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import operations, worker
from cli.application.state import ApplicationError, Installation
from core.sessions import SessionNotFoundError
from core.tools.update_handoff import HANDOFF_DIRECTORY, UpdateHandoffs
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


def make_server(tmp_path: Path):
    """Server state whose Session holds the persisted Bash call ``call-one``."""
    handoffs = UpdateHandoffs(tmp_path)
    grant = handoffs.issue(
        run_id="run-one",
        tool_call_id="call-one",
        agent_id="main",
        project_id=None,
        session_id="session-one",
    )
    persisted_calls = {("session-one", "call-one")}

    async def tool_result_persisted_async(address, tool_call_id):
        return (address.session_id, tool_call_id) in persisted_calls

    bootstrap = BootstrapStub()
    runtime = SimpleNamespace(
        storage=SimpleNamespace(data_dir=tmp_path),
        chat_sessions=SimpleNamespace(tool_result_persisted_async=tool_result_persisted_async),
        bootstrap_service=bootstrap,
        update_handoffs=handoffs,
    )
    state = SimpleNamespace(
        runtime=runtime, control_token="secret", agent_delete_lock=asyncio.Lock()
    )
    return state, grant, bootstrap


def make_state(tmp_path: Path, *, acknowledged: bool = True):
    state, grant, bootstrap = make_server(tmp_path)
    ticket = state.runtime.update_handoffs.mint(grant.token)
    if acknowledged:
        grant.acknowledge()
    return state, ticket, bootstrap


def _mint(state, params):
    return dispatch_method(state, "application.update_handoff_mint", params, method_handlers())


@pytest.mark.asyncio
async def test_update_handoff_mint_is_authorized_by_the_live_token_alone(tmp_path: Path) -> None:
    state, grant, _bootstrap = make_server(tmp_path)

    first = await _mint(state, {"handoff_token": grant.token})
    second = await _mint(state, {"handoff_token": grant.token})

    assert first == second
    assert list((tmp_path / HANDOFF_DIRECTORY).iterdir()) == [Path(first["handoff_ticket"])]


@pytest.mark.asyncio
async def test_update_handoff_mint_rejects_unavailable_tokens_and_caller_identity(
    tmp_path: Path,
) -> None:
    state, grant, _bootstrap = make_server(tmp_path)
    released = state.runtime.update_handoffs.issue(
        run_id="run-two",
        tool_call_id="call-two",
        agent_id="main",
        project_id=None,
        session_id="session-one",
    )
    released_token = released.token
    released.release()

    for params in (
        {"handoff_token": "unknown-token"},
        {"handoff_token": released_token},
        {"handoff_token": grant.token, "session_id": "session-other"},
        {"handoff_token": grant.token, "control_token": "secret"},
        {},
    ):
        with pytest.raises(RpcError):
            await _mint(state, params)

    assert not (tmp_path / "runtime").exists()


def _install(root: Path, data_dir: Path) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str(data_dir.resolve()))
    (root / "versions" / "rel_current").mkdir(parents=True)
    (root / "versions" / "rel_current" / "release.json").write_text("{}", encoding="utf-8")
    (root / "active-version").write_text("rel_current\n", encoding="ascii")
    return install


def _route_cli_rpc_to(state, monkeypatch: pytest.MonkeyPatch) -> None:
    def rpc_call(_target, method, params):
        try:
            result = asyncio.run(dispatch_method(state, method, params, method_handlers()))
        except RpcError as exc:
            return SimpleNamespace(ok=False, data={}, message=exc.message)
        return SimpleNamespace(ok=True, data=result, message="")

    monkeypatch.setattr("cli.rpc_client.rpc_call", rpc_call)
    monkeypatch.setattr("cli.application.processes.target", lambda _install: SimpleNamespace())


def test_claimed_update_handoff_arms_the_continuation_after_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    state, grant, bootstrap = make_server(data_dir)
    install = _install(tmp_path / "install", data_dir)
    _route_cli_rpc_to(state, monkeypatch)
    monkeypatch.setattr(operations, "spawn_worker", lambda _install, _operation: None)

    operation = operations.request_update(install, handoff_token=grant.token)
    repeated = operations.request_update(install, handoff_token=grant.token)

    def server(method: str, **params: str):
        base = {"control_token": "secret", "operation_id": operation.id}
        return asyncio.run(
            dispatch_method(state, f"application.{method}", base | params, method_handlers())
        )

    assert repeated.id == operation.id
    assert operation.handoff_ticket is not None
    ticket_id = Path(operation.handoff_ticket).stem
    # Nothing is armed before the Tool result is durably acknowledged.
    with pytest.raises(RpcError, match="durably acknowledged"):
        server("update_continuation", handoff_ticket_id=ticket_id)
    assert bootstrap.jobs == []

    grant.acknowledge()

    assert worker.wait_for_handoff(install, operation) == ticket_id
    assert server("update_continuation", handoff_ticket_id=ticket_id)["created"] is True
    [job] = bootstrap.jobs
    assert (job.agent_id, job.project_id, job.session_id, job.mode) == (
        "main",
        None,
        "session-one",
        "once",
    )
    assert f"vbot update status {operation.id}" in job.prompt


def test_update_request_with_an_unclaimable_token_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    state, grant, _bootstrap = make_server(data_dir)
    install = _install(tmp_path / "install", data_dir)
    token = grant.token
    grant.release()
    _route_cli_rpc_to(state, monkeypatch)
    monkeypatch.setattr(
        operations, "spawn_worker", lambda *_args: pytest.fail("no update may start")
    )

    with pytest.raises(ApplicationError, match="No update was started") as raised:
        operations.request_update(install, handoff_token=token)

    assert "update handoff is not active on this server" in str(raised.value)
    assert token not in str(raised.value)
    assert operations.operations(install) == []
    assert not (data_dir / "runtime").exists()


@pytest.mark.asyncio
async def test_update_continuation_requires_the_durable_tool_call_and_its_session(
    tmp_path: Path,
) -> None:
    state, ticket, bootstrap = make_state(tmp_path)
    params = {
        "control_token": "secret",
        "operation_id": "operation-one",
        "handoff_ticket_id": ticket.ticket_id,
    }

    async def not_persisted(_address, _tool_call_id):
        return False

    state.runtime.chat_sessions.tool_result_persisted_async = not_persisted
    with pytest.raises(RpcError, match="does not match a durable Tool call"):
        await dispatch_method(state, "application.update_continuation", params, method_handlers())

    async def session_gone(_address, _tool_call_id):
        raise SessionNotFoundError("session-one")

    state.runtime.chat_sessions.tool_result_persisted_async = session_gone
    with pytest.raises(RpcError, match="Session is unavailable"):
        await dispatch_method(state, "application.update_continuation", params, method_handlers())
    assert bootstrap.jobs == []


@pytest.mark.asyncio
async def test_private_application_rpc_rejects_wrong_control_token(tmp_path: Path) -> None:
    state, ticket, bootstrap = make_state(tmp_path)
    with pytest.raises(RpcError):
        await dispatch_method(
            state,
            "application.update_continuation",
            {
                "control_token": "wrong",
                "operation_id": "operation-one",
                "handoff_ticket_id": ticket.ticket_id,
            },
            method_handlers(),
        )
    assert bootstrap.jobs == []


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


@pytest.mark.asyncio
async def test_update_continuation_creates_its_job_under_the_reference_lock(
    tmp_path: Path,
) -> None:
    """A Session or Agent removal that holds the lock ends before the job selects the Session."""
    state, ticket, bootstrap = make_state(tmp_path)
    params = {
        "control_token": "secret",
        "operation_id": "operation-one",
        "handoff_ticket_id": ticket.ticket_id,
    }

    async with state.agent_delete_lock:
        request = asyncio.create_task(
            dispatch_method(state, "application.update_continuation", params, method_handlers())
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert bootstrap.jobs == []
    result = await request

    assert result["created"] is True
    assert [job.session_id for job in bootstrap.jobs] == ["session-one"]
