"""MCP: OAuth credentials, redaction, retries and cancellation of connection work."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp.client.auth import OAuthFlowError

from core.extensions.oauth_redirects import OAuthRedirects
from resources.extensions.mcp import client as mcp_client
from resources.extensions.mcp._oauth import PASTE_REDIRECT_URI, ConnectionOAuth, OAuthStorage
from resources.extensions.mcp.client import ConnectionRunner, InvocationNotSentError
from resources.extensions.mcp.config import validate_connection
from resources.extensions.mcp.interactions import InputRequests
from tests.resources.extensions.mcp.mcp_test_support import context, runner_for, start_service

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
CALLBACK = "http://127.0.0.1:8420/api/oauth/callback"
TOKENS = "VBOT_MCP_EXAMPLE_OAUTH_TOKENS"
CLIENT = "VBOT_MCP_EXAMPLE_OAUTH_CLIENT"


class FakeServers:
    """A protected MCP endpoint and its authorization server, answered in memory."""

    def __init__(self) -> None:
        self.issuer = ISSUER
        self.requests: list[httpx2.Request] = []
        self.accepted: set[str] = set()
        self.issued = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "mcp.example.com":
            if url.path == "/.well-known/oauth-protected-resource/mcp":
                return httpx2.Response(
                    200, json={"resource": RESOURCE, "authorization_servers": [self.issuer]}
                )
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if url.path == "/mcp" and token in self.accepted:
                return httpx2.Response(200, json={"ok": True})
            if url.path == "/mcp":
                metadata = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
                return httpx2.Response(
                    401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}
                )
        elif f"{url.scheme}://{url.host}" == self.issuer:
            if url.path == "/.well-known/oauth-authorization-server":
                return httpx2.Response(
                    200,
                    json={
                        "issuer": self.issuer,
                        "authorization_endpoint": f"{self.issuer}/authorize",
                        "token_endpoint": f"{self.issuer}/token",
                        "registration_endpoint": f"{self.issuer}/register",
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            if url.path == "/register":
                return httpx2.Response(
                    201, json={**json.loads(request.content), "client_id": "registered-client"}
                )
            if url.path == "/token":
                self.issued += 1
                self.accepted.add(f"access-{self.issued}")
                return httpx2.Response(
                    200,
                    json={
                        "access_token": f"access-{self.issued}",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": f"refresh-{self.issued}",
                    },
                )
        return httpx2.Response(404)

    def form(self, path: str) -> dict[str, list[str]]:
        """The form of the last request to the authorization server's *path*."""
        request = next(
            item for item in reversed(self.requests) if item.url.path == path and item.content
        )
        return parse_qs(request.content.decode())

    def sent(self, host: str) -> list[str]:
        return [request.url.path for request in self.requests if request.url.host == host]


def oauth_connection(**fields) -> dict:
    return validate_connection(
        {"id": "example", "transport": "http", "url": RESOURCE, "oauth": True, **fields}
    )


def signed_client(config, host, inputs, servers) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.MockTransport(servers.handle),
        auth=ConnectionOAuth(config, host, inputs).provider(),
    )


async def pending_sign_in(inputs: InputRequests, request: asyncio.Task) -> dict:
    """The sign-in input *request* is waiting for, and its authorization parameters."""
    async with asyncio.timeout(5):
        while not inputs.list():
            if request.done():
                await request
            await asyncio.sleep(0)
    pending = inputs.list()[0]
    assert pending["kind"] == "oauth"
    return {
        "id": pending["id"],
        **{
            key: values[0]
            for key, values in parse_qs(urlsplit(pending["payload"]["url"]).query).items()
        },
    }


@pytest.fixture
def redirects() -> OAuthRedirects:
    redirects = OAuthRedirects()
    redirects.bind(CALLBACK)
    return redirects


@pytest.mark.asyncio
async def test_the_browser_callback_completes_a_sign_in_bound_to_its_server(host, redirects):
    host = replace(host, oauth_redirects=redirects)
    servers = FakeServers()
    inputs = InputRequests()
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        request = asyncio.create_task(client.get(RESOURCE))
        sign_in = await pending_sign_in(inputs, request)
        assert sign_in["redirect_uri"] == CALLBACK
        assert sign_in["client_id"] == "registered-client"

        delivered = {"code": "code", "state": sign_in["state"], "iss": ISSUER}
        assert redirects.deliver(delivered)
        response = await asyncio.wait_for(request, 5)

    assert response.status_code == 200
    # The browser's return withdrew the pending input, and works only once.
    assert inputs.list() == []
    assert not redirects.deliver(delivered)
    stored = json.loads(host.resolve_credential(TOKENS))
    assert stored["resource"] == RESOURCE
    assert stored["authorization_server"]["issuer"] == ISSUER
    assert stored["expires_at"] > time.time() + 3000
    # The tokens belong to this server URL: another URL loads none, and they are redacted.
    other = oauth_connection(url="https://other.example.com/mcp")
    assert await OAuthStorage(host, other, CALLBACK).get_tokens() is None
    assert await OAuthStorage(host, oauth_connection(), CALLBACK).get_tokens() is not None
    runner = ConnectionRunner(oauth_connection(), host, InputRequests(), lambda *args: None)
    runner._events.record("log", {"text": f"{stored['access_token']} {stored['refresh_token']}"})
    assert stored["access_token"] not in json.dumps(runner.events())
    assert stored["refresh_token"] not in json.dumps(runner.events())


@pytest.mark.asyncio
async def test_an_expiring_token_is_refreshed_after_a_restart_without_a_new_sign_in(host):
    servers = FakeServers()
    inputs = InputRequests()
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        request = asyncio.create_task(client.get(RESOURCE))
        sign_in = await pending_sign_in(inputs, request)
        # Without a server callback, the user pastes where the browser went.
        assert sign_in["redirect_uri"] == PASTE_REDIRECT_URI
        inputs.respond(
            sign_in["id"],
            {"redirect_url": f"{PASTE_REDIRECT_URI}?code=code&state={sign_in['state']}"},
        )
        assert (await asyncio.wait_for(request, 5)).status_code == 200
    stored = json.loads(host.resolve_credential(TOKENS))
    host.set_credential(TOKENS, json.dumps({**stored, "expires_at": time.time() + 10}))
    servers.requests.clear()

    # A new provider, as after a restart: it refreshes ahead of the expiry, at the
    # authorization server it signed in with, before sending the request.
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        response = await asyncio.wait_for(client.get(RESOURCE), 5)

    assert response.status_code == 200
    assert inputs.list() == []
    assert servers.sent("auth.example.com") == ["/token"]
    refresh = servers.form("/token")
    assert refresh["grant_type"] == ["refresh_token"]
    assert refresh["refresh_token"] == ["refresh-1"]
    assert refresh["resource"] == [RESOURCE]
    assert servers.requests[-1].headers["authorization"] == "Bearer access-2"
    assert json.loads(host.resolve_credential(TOKENS))["access_token"] == "access-2"


@pytest.mark.asyncio
async def test_a_preregistered_client_skips_registration_and_stays_with_its_server(host, redirects):
    host = replace(host, oauth_redirects=redirects)
    host.set_credential("CLIENT_SECRET", "secret-sentinel")
    config = oauth_connection(
        oauth_client_id="vbot-client",
        oauth_client_secret="CLIENT_SECRET",
        oauth_scopes=["files:read"],
    )
    servers = FakeServers()
    inputs = InputRequests()
    async with signed_client(config, host, inputs, servers) as client:
        request = asyncio.create_task(client.get(RESOURCE))
        sign_in = await pending_sign_in(inputs, request)
        assert sign_in["client_id"] == "vbot-client"
        assert "files:read" in sign_in["scope"].split()
        assert redirects.deliver({"code": "code", "state": sign_in["state"]})
        assert (await asyncio.wait_for(request, 5)).status_code == 200

        assert "/register" not in servers.sent("auth.example.com")
        token = next(item for item in servers.requests if item.url.path == "/token")
        basic = base64.b64encode(b"vbot-client:secret-sentinel").decode()
        assert token.headers["authorization"] == f"Basic {basic}"

        # The server moves to another authorization server: the configured client
        # is not presented to it.
        servers.issuer = "https://other-auth.example.com"
        servers.accepted.clear()
        with pytest.raises(OAuthFlowError, match="now uses the authorization server"):
            await asyncio.wait_for(client.get(RESOURCE), 5)
    assert servers.sent("other-auth.example.com") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reauthorize", "url", "remove"])
async def test_signing_out_or_changing_the_server_forgets_the_sign_in(
    host, monkeypatch, caplog, change
):
    monkeypatch.setattr(ConnectionRunner, "start", lambda runner: None)
    service, _registry = await start_service(host)
    connection = {"id": "example", "transport": "http", "url": RESOURCE, "oauth": True}
    try:
        await service.manage("save", {"connection": connection})
        host.set_credential(TOKENS, json.dumps({"access_token": "a", "resource": RESOURCE}))
        host.set_credential(CLIENT, json.dumps({"client_id": "c", "resource": RESOURCE}))
        assert (await service.manage("status", {"id": "example"}))["oauth"]["signed_in"]

        with caplog.at_level(logging.INFO, logger="test.mcp"):
            if change == "reauthorize":
                status = await service.manage("reauthorize", {"id": "example"})
                assert status["oauth"] == {"redirect_uri": PASTE_REDIRECT_URI, "signed_in": False}
            elif change == "url":
                moved = {**connection, "url": "https://moved.example.com/mcp"}
                await service.manage("save", {"connection": moved})
            else:
                await service.manage("remove", {"id": "example"})

        assert host.resolve_credential(TOKENS) == ""
        assert host.resolve_credential(CLIENT) == ""
        if change == "reauthorize":
            assert "MCP connection signed out (connection=example)" in caplog.text
    finally:
        await service.close()


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
        if outcome == "close":
            await runner.close()
        elif outcome == "slow_close":
            # Close keeps waiting while SDK teardown is stuck in the Client exit;
            # the producers it admitted must settle before teardown finishes.
            closing = asyncio.create_task(runner.close())
            await asyncio.wait_for(cleanup_entered.wait(), 1)
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
        if closing is not None:
            await closing
        await runner.close()
