"""Mcp: oauth and cancellation behavior."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.shared.auth import OAuthToken

from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp.client import ConnectionRunner, InvocationNotSentError, OAuthStorage
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.interactions import InputRequests
from tests.resources.extensions.mcp_helpers import (
    host as host,
)


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
    runner._record("log", {"text": "secret-access-sentinel"})

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
    runner._record("log", {secret: [secret], "payload": {"text": "\n" + secret}})
    payload = runner.events()["events"][0]["payload"]
    assert payload["[redacted]"] == ["[redacted]"]
    assert payload["payload"]["text"] == "\n[redacted]"


@pytest.mark.asyncio
async def test_cancelled_oauth_does_not_leave_a_pending_request(host):
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "http", "url": "https://example.com"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    task = asyncio.create_task(runner._oauth_callback())
    await asyncio.sleep(0)
    pending = runner.inputs.list()[0]

    runner.inputs.respond(pending["id"], {"action": "cancel"})

    with pytest.raises(ValueError):
        await task
    assert runner.inputs.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, expected_calls", [("resources/read", 4), ("tools/call", 1)])
async def test_read_failures_retry_but_mutations_are_not_replayed(
    host, monkeypatch, operation, expected_calls
):
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "unused"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    calls = []

    async def fail(*args):
        calls.append(args)
        raise TimeoutError("test-owned-timeout")

    async def sleep(delay):
        pass

    monkeypatch.setattr(runner, "_perform", fail)
    monkeypatch.setattr(mcp_client, "_sleep", sleep)

    with pytest.raises(TimeoutError):
        await runner._perform_with_retries(operation, {})

    assert len(calls) == expected_calls


@pytest.mark.asyncio
async def test_owner_cancellation_exits_an_active_request(host):
    from resources.extensions.mcp.client import Invocation

    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "unused"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    runner._perform_with_retries = wait
    result = asyncio.get_running_loop().create_future()
    await runner._queue.put(Invocation("tools/call", {}, None, result))
    owner = asyncio.create_task(runner._serve())
    await entered.wait()

    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 1)
    assert result.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["close", "slow_close", "failure", "drain"])
async def test_full_queue_producers_settle_with_their_connection(host, monkeypatch, outcome):
    monkeypatch.setattr(mcp_client, "CONNECTION_QUEUE_LIMIT", 2)
    monkeypatch.setattr(mcp_client, "CONNECTION_CLOSE_TIMEOUT_SECONDS", 0.01)
    runner = ConnectionRunner(
        validate_connection({"id": "example", "transport": "stdio", "command": "unused"}),
        host,
        InputRequests(),
        lambda *args: None,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    performed = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

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

    async def perform(operation, arguments):
        performed.append(arguments["index"])
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise RuntimeError("test-owned-connection-failure")
        return arguments

    monkeypatch.setattr(mcp_client, "Client", Client)
    monkeypatch.setattr(runner, "_transport", transport)
    monkeypatch.setattr(runner, "_refresh", nothing)
    monkeypatch.setattr(runner, "_watch_catalog", nothing)
    monkeypatch.setattr(runner, "_perform_with_retries", perform)
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
        if outcome in {"close", "slow_close"}:
            await runner.close()
            if outcome == "slow_close":
                assert cleanup_entered.is_set()
                assert not runner._task.done()
        else:
            release.set()
        _, pending = await asyncio.wait(calls, timeout=1)
        assert not pending, "Queue producers must not outlive the connection that admitted them"
        results = await asyncio.gather(*calls, return_exceptions=True)
        if outcome == "drain":
            assert results == [{"index": index} for index in range(len(calls))]
            assert performed == list(range(len(calls)))
        else:
            assert isinstance(
                results[0], RuntimeError if outcome == "failure" else asyncio.CancelledError
            )
            assert all(isinstance(result, InvocationNotSentError) for result in results[1:])
            assert performed == [0]
        assert runner._queue.empty()
    finally:
        cleanup_release.set()
        for task in calls:
            task.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await runner.close()
