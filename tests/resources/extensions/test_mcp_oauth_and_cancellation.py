"""Mcp: oauth and cancellation behavior."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.shared.auth import OAuthToken

from resources.extensions.mcp.client import ConnectionRunner, OAuthStorage
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
    monkeypatch.setattr(asyncio, "sleep", sleep)

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
