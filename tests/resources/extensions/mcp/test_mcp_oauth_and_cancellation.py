"""MCP: OAuth credentials, redaction, retries, lost connections and cancelled calls."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import replace
from types import SimpleNamespace
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
from tests.resources.extensions.mcp.mcp_test_support import (
    StreamServer,
    context,
    runner_for,
    start_service,
)

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
CALLBACK = "http://127.0.0.1:8420/api/oauth/callback"
TOKENS = "VBOT_MCP_EXAMPLE_OAUTH_TOKENS"
CLIENT = "VBOT_MCP_EXAMPLE_OAUTH_CLIENT"


class FakeServers:
    """A protected MCP endpoint and its authorization server, answered in memory.

    Like some firewalls, both refuse OAuth requests that do not identify their client.
    """

    def __init__(self) -> None:
        self.issuer = ISSUER
        # The issuer the resource metadata lists, when not exactly ``issuer``.
        self.listed: str | None = None
        # Fields added to the resource metadata, the server metadata and a registration
        # (a registration field set to None is left out).
        self.resource_metadata: dict = {}
        self.server_metadata: dict = {}
        self.registration: dict = {}
        # Scopes a registration may not ask for.
        self.unregistrable: set[str] = set()
        self.requests: list[httpx2.Request] = []
        self.accepted: set[str] = set()
        self.issued = 0
        self.registered = 0
        # Registered clients the server no longer knows, and refresh tokens it refuses.
        self.forgotten: set[str] = set()
        self.spent: set[str] = set()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = request.url
        if url.path != "/mcp" and (
            not request.headers.get("user-agent", "").startswith("vBot")
            or request.headers.get("accept") != "application/json"
        ):
            return httpx2.Response(403)
        if url.host == "mcp.example.com":
            if url.path == "/.well-known/oauth-protected-resource/mcp":
                return httpx2.Response(
                    200,
                    json={
                        "resource": RESOURCE,
                        "authorization_servers": [self.listed or self.issuer],
                        **self.resource_metadata,
                    },
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
                        **self.server_metadata,
                    },
                )
            if url.path == "/register":
                body = json.loads(request.content)
                if self.unregistrable & set(body.get("scope", "").split()):
                    return httpx2.Response(400, json={"error": "invalid_client_metadata"})
                self.registered += 1
                client_id = "registered-client" + (
                    f"-{self.registered}" if self.registered > 1 else ""
                )
                registered = {**body, "client_id": client_id, **self.registration}
                return httpx2.Response(
                    201, json={key: value for key, value in registered.items() if value is not None}
                )
            if url.path == "/token":
                form = parse_qs(request.content.decode())
                if form.get("client_id", [""])[0] in self.forgotten:
                    return httpx2.Response(401, json={"error": "invalid_client"})
                if form.get("refresh_token", [""])[0] in self.spent:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
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
        return parse_qs(self.last(path).content.decode())

    def last(self, path: str) -> httpx2.Request:
        """The last request with a body to the authorization server's *path*."""
        return next(
            item for item in reversed(self.requests) if item.url.path == path and item.content
        )

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


def complete_sign_in(inputs: InputRequests, sign_in: dict, iss: str | None = None) -> None:
    """Answer *sign_in* with the address the browser went to, as the user pastes it."""
    issuer = f"&iss={iss}" if iss else ""
    inputs.respond(
        sign_in["id"],
        {"redirect_url": f"{PASTE_REDIRECT_URI}?code=code&state={sign_in['state']}{issuer}"},
    )


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
        complete_sign_in(inputs, sign_in)
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
@pytest.mark.parametrize(
    "refusal, registration",
    [
        ("token", "registered-client-2"),
        ("authorization", "registered-client-2"),
        ("refresh", "registered-client-2"),
        ("spent_refresh", "registered-client"),
    ],
)
async def test_a_registration_the_server_no_longer_accepts_is_replaced(host, refusal, registration):
    servers = FakeServers()
    inputs = InputRequests()
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        request = asyncio.create_task(client.get(RESOURCE))
        complete_sign_in(inputs, await pending_sign_in(inputs, request))
        assert (await asyncio.wait_for(request, 5)).status_code == 200
    stored = json.loads(host.resolve_credential(TOKENS))
    servers.accepted.clear()
    if refusal == "spent_refresh":
        # Only the refresh token is spent; the registration still works.
        servers.spent.add("refresh-1")
    else:
        # The authorization server expired or revoked the registration.
        servers.forgotten.add("registered-client")

    if refusal in {"token", "authorization"}:
        # The next sign-in presents the stored registration and does not complete.
        async with signed_client(oauth_connection(), host, inputs, servers) as client:
            request = asyncio.create_task(client.get(RESOURCE))
            sign_in = await pending_sign_in(inputs, request)
            assert sign_in["client_id"] == "registered-client"
            if refusal == "token":
                complete_sign_in(inputs, sign_in)
                with pytest.raises(OAuthFlowError, match="invalid_client"):
                    await asyncio.wait_for(request, 5)
            else:
                # The authorization page rejected the client; the user cancels.
                inputs.respond(sign_in["id"], {"action": "cancel"})
                with pytest.raises(ValueError, match="cancelled"):
                    await asyncio.wait_for(request, 5)
        assert host.resolve_credential(CLIENT) == ""
    else:
        # A refresh is due; its refusal leads to a new sign-in in the same request.
        host.set_credential(TOKENS, json.dumps({**stored, "expires_at": time.time() + 10}))

    # The sign-in after a refused registration registers again.
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        request = asyncio.create_task(client.get(RESOURCE))
        sign_in = await pending_sign_in(inputs, request)
        assert sign_in["client_id"] == registration
        complete_sign_in(inputs, sign_in)
        assert (await asyncio.wait_for(request, 5)).status_code == 200
    assert json.loads(host.resolve_credential(CLIENT))["client_id"] == registration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deviation", ["issuer_slash", "offline_access", "secret_basic", "secret_post", "no_scopes"]
)
async def test_sign_in_tolerates_authorization_servers_that_deviate_from_the_sdk(host, deviation):
    servers = FakeServers()
    if deviation == "issuer_slash":
        # The resource lists the issuer with a trailing slash the server's own metadata omits.
        servers.listed = f"{ISSUER}/"
    elif deviation == "offline_access":
        servers.server_metadata = {"scopes_supported": ["files", "offline_access"]}
        servers.unregistrable = {"offline_access"}
    elif deviation.startswith("secret"):
        # A client secret without token_endpoint_auth_method, although the
        # registration asked for none.
        servers.registration = {
            "client_secret": "secret-sentinel",
            "token_endpoint_auth_method": None,
        }
        if deviation == "secret_post":
            servers.server_metadata = {
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post"]
            }
    else:
        servers.resource_metadata = {"scopes_supported": []}
        servers.server_metadata = {"scopes_supported": ["offline_access"]}
    inputs = InputRequests()
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        request = asyncio.create_task(client.get(RESOURCE))
        sign_in = await pending_sign_in(inputs, request)
        complete_sign_in(inputs, sign_in, iss=ISSUER)
        assert (await asyncio.wait_for(request, 5)).status_code == 200

    registration = json.loads(servers.last("/register").content)
    token = servers.last("/token")
    if deviation == "issuer_slash":
        stored = json.loads(host.resolve_credential(TOKENS))
        assert stored["authorization_server"]["issuer"] == f"{ISSUER}/"
    elif deviation == "offline_access":
        # Registered once more without it, and still asked for at authorization.
        assert servers.sent("auth.example.com").count("/register") == 2
        assert registration["scope"] == "files"
        assert sign_in["scope"] == "files offline_access"
    elif deviation == "secret_basic":
        basic = base64.b64encode(b"registered-client:secret-sentinel").decode()
        assert token.headers["authorization"] == f"Basic {basic}"
    elif deviation == "secret_post":
        assert servers.form("/token")["client_secret"] == ["secret-sentinel"]
        assert "authorization" not in token.headers
    else:
        assert registration["scope"] == sign_in["scope"] == "offline_access"


@pytest.mark.asyncio
async def test_sign_in_refuses_an_authorization_server_with_another_issuer(host):
    servers = FakeServers()
    servers.server_metadata = {"issuer": "https://example.com"}
    inputs = InputRequests()
    async with signed_client(oauth_connection(), host, inputs, servers) as client:
        with pytest.raises(OAuthFlowError, match="issuer mismatch"):
            await asyncio.wait_for(client.get(RESOURCE), 5)
    assert "/register" not in servers.sent("auth.example.com")


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
