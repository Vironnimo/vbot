"""MCP: OAuth credentials, redaction, retries, lost connections and cancelled calls."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.shared.auth import OAuthToken

from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp._oauth import ConnectionOAuth, OAuthStorage
from resources.extensions.mcp.client import ConnectionRunner, InvocationNotSentError
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.interactions import InputRequests
from tests.resources.extensions.mcp.mcp_test_support import StreamServer, context, runner_for


@pytest.mark.asyncio
async def test_oauth_tokens_use_the_host_store_and_are_redacted(host):
    storage = OAuthStorage(host, "example")
    token = OAuthToken(access_token="secret-access-sentinel", token_type="Bearer")
    await storage.set_tokens(token)
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "http", "url": "https://example.com"}),
        host,
        InputRequests(),
        lambda *args: None,
    )

    loaded = await storage.get_tokens()
    runner._events.record("log", {"text": "secret-access-sentinel"})

    assert loaded.access_token == token.access_token
    assert "secret-access-sentinel" not in json.dumps(runner.events())
    assert "secret-access-sentinel" not in json.dumps(runner.status())


@pytest.mark.parametrize(
    "secret", ['quoted"credential', "slash\\credential", "line\ncredential", "n"]
)
def test_event_redaction_handles_json_escaping_without_corrupting_events(host, secret):
    host.set_credential("TEST_CREDENTIAL", secret)
    runner = ConnectionRunner(
        validate_connection(
            {
                "id": "example",
                "transport": "stdio",
                "command": "unused",
                "credential_environment": {"TOKEN": "TEST_CREDENTIAL"},
            }
        ),
        host,
        InputRequests(),
        lambda *args: None,
    )
    runner._events.record("log", {secret: [secret], "payload": {"text": "\n" + secret}})
    payload = runner.events()["events"][0]["payload"]
    assert payload["[redacted]"] == ["[redacted]"]
    assert payload["payload"]["text"] == "\n[redacted]"


@pytest.mark.asyncio
async def test_cancelled_oauth_does_not_leave_a_pending_request(host):
    inputs = InputRequests()
    oauth = ConnectionOAuth(
        validate_connection({"id": "example", "transport": "http", "url": "https://example.com"}),
        host,
        inputs,
    )
    task = asyncio.create_task(oauth.callback())
    await asyncio.sleep(0)
    pending = inputs.list()[0]

    inputs.respond(pending["id"], {"action": "cancel"})

    with pytest.raises(ValueError):
        await task
    assert inputs.list() == []


@pytest.mark.asyncio
async def test_cancelled_mutation_is_not_replayed(host, server, monkeypatch):
    entered = asyncio.Event()
    calls = []

    @server.tool()
    async def mutate() -> str:
        calls.append("mutated")
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    runner = runner_for(host, server, monkeypatch)
    try:
        task = asyncio.create_task(runner.invoke("tools/call", {"name": "mutate"}, context(host)))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(runner.invoke("ping", {}), 5)
        assert calls == ["mutated"]
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, expected_calls", [("resources/read", 4), ("tools/call", 1)])
async def test_read_failures_retry_but_mutations_are_not_replayed(
    host, server, monkeypatch, operation, expected_calls
):
    runner = runner_for(host, server, monkeypatch)
    calls = []

    async def fail(*args):
        calls.append(args)
        raise TimeoutError("test-owned-timeout")

    async def sleep(delay):
        pass

    monkeypatch.setattr(runner, "_perform", fail)
    monkeypatch.setattr(mcp_client, "_sleep", sleep)
    try:
        with pytest.raises(ValueError, match="test-owned-timeout"):
            await asyncio.wait_for(runner.invoke(operation, {}), 10)
    finally:
        await runner.close()

    assert len(calls) == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["tools/call", "resources/read"])
async def test_a_lost_connection_settles_its_calls_and_reconnects(
    host, server, monkeypatch, operation
):
    # The server process ends while a call runs: the mutation reports an unknown
    # outcome and is not replayed, the read runs again once the connection is back.
    entered = asyncio.Event()
    runs = []

    @server.tool()
    async def mutate() -> str:
        runs.append("mutate")
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    @server.resource("test://slow")
    async def slow() -> str:
        runs.append("read")
        if len(runs) == 1:
            entered.set()
            await asyncio.Event().wait()
        return "test-owned-after-reconnect"

    async def sleep(delay):
        pass

    monkeypatch.setattr(mcp_client, "_sleep", sleep)
    stream = StreamServer(server)
    runner = runner_for(host, stream, monkeypatch)
    arguments = {"name": "mutate"} if operation == "tools/call" else {"uri": "test://slow"}
    call = asyncio.create_task(runner.invoke(operation, arguments, context(host)))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            await stream.kill()
            if operation == "tools/call":
                with pytest.raises(ValueError) as lost:
                    await call
                assert not isinstance(lost.value, InvocationNotSentError)
                assert str(lost.value) == (
                    "The connection was lost while the call ran: "
                    "the MCP server closed the connection"
                )
                echoed = await runner.invoke(
                    "tools/call", {"name": "echo", "arguments": {"value": "after"}}
                )
                assert echoed["structuredContent"] == {"value": "after"}
                assert runs == ["mutate"]
            else:
                result = await call
                assert result["contents"][0]["text"] == "test-owned-after-reconnect"
                assert runs == ["read", "read"]
        assert stream.connections == 2
        failures = [
            event["payload"]["error"]
            for event in runner.events()["events"]
            if event["kind"] == "connection_failed"
        ]
        assert failures == ["Connection lost: the MCP server closed the connection"]
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["close", "slow_close", "lost", "drain"])
async def test_full_queue_producers_settle_with_their_connection(host, monkeypatch, outcome):
    # One call at a time, so the others wait in the queue or for room in it.
    monkeypatch.setattr(mcp_client, "CONNECTION_CONCURRENCY", 1)
    monkeypatch.setattr(mcp_client, "CONNECTION_QUEUE_LIMIT", 2)
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "unused"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    closing: asyncio.Task[None] | None = None
    entered = asyncio.Event()
    release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    performed = []

    class Client:
        def __init__(self, *args, **kwargs):
            async def validate(name, result):
                pass

            self.session = SimpleNamespace(validate_tool_result=validate)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            if outcome == "slow_close":
                cleanup_entered.set()
                while not cleanup_release.is_set():
                    try:
                        await cleanup_release.wait()
                    except asyncio.CancelledError:
                        continue

    async def transport(stack):
        return None

    async def nothing():
        return {}

    async def perform(operation, arguments, context):
        performed.append(arguments["index"])
        entered.set()
        await release.wait()
        return arguments

    monkeypatch.setattr(mcp_client, "Client", Client)
    monkeypatch.setattr(runner, "_transport", transport)
    monkeypatch.setattr(runner, "_refresh", nothing)
    monkeypatch.setattr(runner, "_watch_catalog", nothing)
    monkeypatch.setattr(runner, "_perform", perform)
    calls = [asyncio.create_task(runner.invoke("tools/call", {"index": 0}))]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        started = [asyncio.Event() for _ in range(6)]

        async def invoke(index, event):
            event.set()
            return await runner.invoke("tools/call", {"index": index})

        calls.extend(
            asyncio.create_task(invoke(index, event))
            for index, event in enumerate(started, start=1)
        )
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
        assert runner._queue.full()
        assert all(not task.done() for task in calls)
        if outcome == "close":
            await runner.close()
        elif outcome == "slow_close":
            # Close keeps waiting while SDK teardown is stuck in the Client exit;
            # the producers it admitted must settle before teardown finishes.
            closing = asyncio.create_task(runner.close())
            await asyncio.wait_for(cleanup_entered.wait(), 1)
        elif outcome == "lost":
            runner._lose("test-owned-loss")
        else:
            release.set()
        _, pending = await asyncio.wait(calls, timeout=1)
        assert not pending, "Queue producers must not outlive the connection that admitted them"
        if outcome == "slow_close":
            assert not runner._task.done()
        results = await asyncio.gather(*calls, return_exceptions=True)
        if outcome == "drain":
            assert results == [{"index": index} for index in range(len(calls))]
            assert performed == list(range(len(calls)))
        else:
            # The running call is settled, never cancelled: its caller learns why.
            assert type(results[0]) is ValueError
            assert str(results[0]) == (
                "The connection was lost while the call ran: test-owned-loss"
                if outcome == "lost"
                else "The MCP connection was closed or reconfigured while the call ran"
            )
            assert all(isinstance(result, InvocationNotSentError) for result in results[1:])
            assert performed == [0]
        assert runner._queue.empty()
    finally:
        cleanup_release.set()
        for task in calls:
            task.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        if closing is not None:
            await closing
        await runner.close()
