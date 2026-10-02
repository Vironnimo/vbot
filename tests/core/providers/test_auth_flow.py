"""DeviceFlowEngine: per-flow authorization and polling, persistence and managed lifecycle.

Every flow runs through ``connect()`` against mocked Provider endpoints. Poll waits go
through the module's ``_sleep`` seam, so tests either record them or park polling.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, override
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import respx

from core.providers import auth_flow
from core.providers.auth_flow import (
    DEVICE_CODE_GRANT_TYPE,
    MINIMAX_OAUTH_GRANT_TYPE,
    DeviceFlowEngine,
    DeviceFlowSession,
    DeviceFlowTerminalError,
)
from core.providers.providers import OAuthConfig
from core.providers.token_store import TokenStore

from .oauth_test_support import (
    COPILOT_TOKEN_EXCHANGE_URL,
    GITHUB_DEVICE_AUTH_URL,
    GITHUB_TOKEN_URL,
    MINIMAX_DEVICE_AUTH_URL,
    MINIMAX_TOKEN_URL,
    OPENAI_DEVICE_AUTH_URL,
    OPENAI_DEVICE_TOKEN_URL,
    OPENAI_REDIRECT_URI,
    OPENAI_TOKEN_URL,
    OPENAI_VERIFICATION_URI,
    github_oauth_config,
    jwt_with_account,
    minimax_oauth_config,
    nous_oauth_config,
    openai_oauth_config,
    opencode_oauth_config,
    request_body,
    seconds_until,
    xai_oauth_config,
)

GITHUB_AUTHORIZATION = {
    "device_code": "device-code",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
    "interval": 3,
}
PENDING = httpx.Response(200, json={"error": "authorization_pending"})
PENDING_400 = httpx.Response(400, json={"error": "authorization_pending"})


class Completion:
    """``on_complete`` callback that lets a test await the flow's outcome.

    ``connect`` accepts plain and coroutine callbacks; ``awaitable`` selects the latter.
    """

    def __init__(self, *, awaitable: bool = False) -> None:
        self.outcomes: list[bool] = []
        self._done = asyncio.Event()
        self._awaitable = awaitable

    def __call__(self, *, success: bool) -> Awaitable[None] | None:
        self.outcomes.append(success)
        self._done.set()
        return asyncio.sleep(0) if self._awaitable else None

    async def wait(self) -> bool:
        await asyncio.wait_for(self._done.wait(), timeout=5)
        assert len(self.outcomes) == 1
        return self.outcomes[0]


@pytest.fixture
def store(tmp_path) -> TokenStore:
    return TokenStore(tmp_path)


@pytest_asyncio.fixture
async def engine(store: TokenStore) -> AsyncIterator[DeviceFlowEngine]:
    engine = DeviceFlowEngine(store)
    try:
        yield engine
    finally:
        await engine.aclose()


@pytest.fixture
def poll_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every poll wait without sleeping."""

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(auth_flow, "_sleep", record)
    return waits


class ParkedPolls:
    """Poll waits that never elapse: each poll parks after its first pending reply."""

    def __init__(self) -> None:
        self.parked: asyncio.Queue[None] = asyncio.Queue()
        self.cancelled = 0

    async def __call__(self, _seconds: float) -> None:
        self.parked.put_nowait(None)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def next(self) -> None:
        await asyncio.wait_for(self.parked.get(), timeout=5)


@pytest.fixture
def parked_polls(monkeypatch: pytest.MonkeyPatch) -> ParkedPolls:
    parked = ParkedPolls()
    monkeypatch.setattr(auth_flow, "_sleep", parked)
    return parked


def _auth_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "vbot.providers.auth_flow"]


# ---------------------------------------------------------------------------
# Authorization and polling per flow
# ---------------------------------------------------------------------------


def _standard_flow(
    config: OAuthConfig, *, pending: httpx.Response, scope: str | None = None
) -> dict[str, Any]:
    authorization: dict[str, Any] = {
        "device_code": "device-code",
        "user_code": "USER-CODE",
        "verification_uri": "https://login.example/device",
        "verification_uri_complete": "https://login.example/device?user_code=USER-CODE",
        "expires_in": 900,
        "interval": 5,
    }
    granted: dict[str, Any] = {"access_token": "access", "refresh_token": "refresh"}
    if scope is not None:
        granted["scope"] = scope
    return {
        "config": config,
        "authorization": authorization,
        "authorization_body": {"client_id": config.client_id, "scope": " ".join(config.scopes)},
        "session": DeviceFlowSession(
            "device-code", "USER-CODE", authorization["verification_uri_complete"], 900, 5
        ),
        "pending": pending,
        "granted": {**granted, "expires_in": 900},
        "token_body": {
            "client_id": config.client_id,
            "device_code": "device-code",
            "grant_type": DEVICE_CODE_GRANT_TYPE,
        },
        "extra": {} if scope is None else {"oauth_scope": scope},
    }


_OPENCODE = _standard_flow(opencode_oauth_config(), pending=PENDING_400)
_OPENCODE["authorization_body"] = {"client_id": "opencode-cli"}

DEVICE_FLOWS = {
    "standard": {
        "config": github_oauth_config(),
        "authorization": GITHUB_AUTHORIZATION,
        "authorization_body": {"client_id": "client-id", "scope": "read:user"},
        "session": DeviceFlowSession(
            "device-code", "ABCD-EFGH", "https://github.com/login/device", 900, 3
        ),
        "pending": PENDING,
        "granted": {"access_token": "access", "refresh_token": "refresh", "expires_in": 600},
        "token_body": {
            "client_id": "client-id",
            "device_code": "device-code",
            "grant_type": DEVICE_CODE_GRANT_TYPE,
        },
        "extra": {},
    },
    # xAI and Nous prefer the complete verification URI and poll through HTTP 400.
    "xai": _standard_flow(xai_oauth_config(), pending=PENDING_400),
    "nous": _standard_flow(nous_oauth_config(), pending=PENDING_400, scope="inference:invoke"),
    # OpenCode posts JSON to both endpoints.
    "opencode": _OPENCODE,
}


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("flow", list(DEVICE_FLOWS))
async def test_device_flow_polls_until_granted_and_stores_the_account_token(
    engine: DeviceFlowEngine,
    store: TokenStore,
    poll_waits: list[float],
    caplog: pytest.LogCaptureFixture,
    flow: str,
) -> None:
    case = DEVICE_FLOWS[flow]
    config: OAuthConfig = case["config"]
    authorization = respx.post(config.device_auth_url).mock(
        return_value=httpx.Response(200, json=case["authorization"])
    )
    polling = respx.post(config.token_url).mock(
        side_effect=[case["pending"], httpx.Response(200, json=case["granted"])]
    )
    completion = Completion()

    with caplog.at_level(logging.INFO, logger="vbot.providers.auth_flow"):
        session = await engine.connect("provider", "oauth", config, completion, account_id="work")
        assert await completion.wait() is True

    assert session == case["session"]
    assert request_body(authorization.calls.last.request) == case["authorization_body"]
    assert [request_body(call.request) for call in polling.calls] == [case["token_body"]] * 2
    assert poll_waits == [session.interval]
    token = store.load("provider", "oauth", account_id="work")
    assert token is not None
    assert (token.access_token, token.refresh_token) == ("access", "refresh")
    assert token.extra == case["extra"]
    assert seconds_until(token.expires_at) == pytest.approx(case["granted"]["expires_in"], abs=5)
    assert store.load("provider", "oauth") is None
    logs = _auth_logs(caplog)
    assert "OAuth provider connected (provider=provider connection=oauth)" in logs
    assert all("work" not in message and "access" not in message for message in logs)


@respx.mock
@pytest.mark.asyncio
async def test_openai_flow_polls_usercode_then_exchanges_the_authorization_code(
    engine: DeviceFlowEngine, store: TokenStore, poll_waits: list[float]
) -> None:
    config = openai_oauth_config()
    authorization = respx.post(OPENAI_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"device_auth_id": "device-auth-id", "user_code": "WXYZ-1234", "interval": 2}
        )
    )
    polling = respx.post(OPENAI_DEVICE_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(403, json={"message": "not authorized yet"}),
            httpx.Response(
                200, json={"authorization_code": "authorization-code", "code_verifier": "verifier"}
            ),
        ]
    )
    access_token = jwt_with_account("acct_openai")
    exchange = respx.post(OPENAI_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": access_token, "refresh_token": "refresh", "expires_in": 3600},
        )
    )
    completion = Completion(awaitable=True)

    session = await engine.connect("openai", "subscription", config, completion)

    assert await completion.wait() is True
    assert session == DeviceFlowSession(
        "device-auth-id", "WXYZ-1234", OPENAI_VERIFICATION_URI, 600, 2
    )
    assert request_body(authorization.calls.last.request) == {"client_id": config.client_id}
    assert [request_body(call.request) for call in polling.calls] == [
        {"device_auth_id": "device-auth-id", "user_code": "WXYZ-1234"}
    ] * 2
    assert poll_waits == [2]
    assert request_body(exchange.calls.last.request) == {
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "code": "authorization-code",
        "code_verifier": "verifier",
        "redirect_uri": OPENAI_REDIRECT_URI,
    }
    token = store.load("openai", "subscription")
    assert token is not None
    assert (token.access_token, token.refresh_token) == (access_token, "refresh")
    assert token.extra == {"chatgpt_account_id": "acct_openai"}


@respx.mock
@pytest.mark.asyncio
async def test_minimax_flow_uses_pkce_state_and_millisecond_fields(
    engine: DeviceFlowEngine, store: TokenStore, poll_waits: list[float]
) -> None:
    authorization_form: dict[str, Any] = {}

    def authorize(request: httpx.Request) -> httpx.Response:
        authorization_form.update(request_body(request))
        expires_at_ms = int((datetime.now(UTC) + timedelta(minutes=10)).timestamp() * 1000)
        return httpx.Response(
            200,
            json={
                "user_code": "MINIMAX-CODE",
                "verification_uri": "https://api.minimax.io/oauth/verify",
                "expired_in": expires_at_ms,
                "interval": 2500,
                "state": authorization_form["state"],
            },
        )

    authorization = respx.post(MINIMAX_DEVICE_AUTH_URL).mock(side_effect=authorize)
    polling = respx.post(MINIMAX_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"status": "pending"}),
            # A small ``expired_in`` is a TTL in seconds, not a millisecond timestamp.
            httpx.Response(
                200,
                json={
                    "status": "success",
                    "access_token": "minimax-access",
                    "refresh_token": "minimax-refresh",
                    "expired_in": 900,
                },
            ),
        ]
    )
    completion = Completion()

    session = await engine.connect("minimax", "subscription", minimax_oauth_config(), completion)

    assert await completion.wait() is True
    assert (session.device_code, session.user_code, session.verification_uri) == (
        "MINIMAX-CODE",
        "MINIMAX-CODE",
        "https://api.minimax.io/oauth/verify",
    )
    assert 595 <= session.expires_in <= 600
    assert session.interval == 3
    assert poll_waits == [3]
    assert authorization.calls.last.request.headers["x-request-id"]
    assert {
        key: authorization_form[key]
        for key in ("response_type", "client_id", "scope", "code_challenge_method")
    } == {
        "response_type": "code",
        "client_id": "minimax-client-id",
        "scope": "group_id profile model.completion",
        "code_challenge_method": "S256",
    }
    token_form = request_body(polling.calls.last.request)
    assert token_form["grant_type"] == MINIMAX_OAUTH_GRANT_TYPE
    assert token_form["user_code"] == "MINIMAX-CODE"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(token_form["code_verifier"].encode()).digest()
    )
    assert authorization_form["code_challenge"] == challenge.decode().rstrip("=")
    token = store.load("minimax", "subscription")
    assert token is not None
    assert (token.access_token, token.refresh_token) == ("minimax-access", "minimax-refresh")
    assert seconds_until(token.expires_at) == pytest.approx(900, abs=5)


@respx.mock
@pytest.mark.asyncio
async def test_minimax_authorization_with_foreign_state_is_rejected(
    engine: DeviceFlowEngine,
) -> None:
    respx.post(MINIMAX_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "user_code": "MINIMAX-CODE",
                "verification_uri": "https://api.minimax.io/oauth/verify",
                "expired_in": 600,
                "state": "wrong-state",
            },
        )
    )

    with pytest.raises(DeviceFlowTerminalError, match="state_mismatch"):
        await engine.connect("minimax", "subscription", minimax_oauth_config(), Completion())

    assert not engine.is_flow_active("minimax", "subscription")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_succeeds", [True, False])
async def test_copilot_flow_exchanges_the_github_token_before_storing(
    engine: DeviceFlowEngine, store: TokenStore, poll_waits: list[float], exchange_succeeds: bool
) -> None:
    expires_at = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    respx.post(GITHUB_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(200, json=GITHUB_AUTHORIZATION)
    )
    respx.post(GITHUB_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "github-oauth-secret"})
    )
    exchange = respx.get(COPILOT_TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "copilot-api-secret",
                "expires_at": expires_at.timestamp(),
                "endpoints": {"api": "https://api.business.githubcopilot.com"},
            },
        )
        if exchange_succeeds
        else httpx.Response(403, json={"message": "forbidden"})
    )
    completion = Completion()

    await engine.connect(
        "github-copilot", "oauth", github_oauth_config(token_exchange=True), completion
    )

    assert await completion.wait() is exchange_succeeds
    headers = exchange.calls.last.request.headers
    assert headers["Authorization"] == "Bearer github-oauth-secret"
    assert headers["Copilot-Integration-Id"] == "vscode-chat"
    assert headers["Editor-Version"] == "vscode/1.128.0"
    token = store.load("github-copilot", "oauth")
    if not exchange_succeeds:
        assert token is None
        return
    assert token is not None
    assert (token.access_token, token.refresh_token) == ("copilot-api-secret", None)
    assert token.expires_at == expires_at
    assert token.extra == {
        "github_oauth_token": "github-oauth-secret",
        "copilot_api_endpoint": "https://api.business.githubcopilot.com",
    }


@respx.mock
@pytest.mark.asyncio
async def test_polling_waits_the_interval_and_slow_down_adds_five_seconds(
    engine: DeviceFlowEngine, poll_waits: list[float]
) -> None:
    respx.post(GITHUB_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(200, json={**GITHUB_AUTHORIZATION, "interval": 7})
    )
    respx.post(GITHUB_TOKEN_URL).mock(
        side_effect=[
            PENDING,
            httpx.Response(200, json={"error": "slow_down"}),
            PENDING,
            httpx.Response(200, json={"access_token": "access"}),
        ]
    )
    completion = Completion()

    await engine.connect("github-copilot", "oauth", github_oauth_config(), completion)

    assert await completion.wait() is True
    assert poll_waits == [7, 12, 12]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expires_in", "replies"),
    [
        pytest.param(github_oauth_config(), 0, [], id="session-expired"),
        pytest.param(
            github_oauth_config(),
            900,
            [httpx.Response(200, json={"error": "access_denied"})],
            id="access-denied",
        ),
        pytest.param(
            xai_oauth_config(),
            900,
            [httpx.Response(400, json={"error": "invalid_client"})],
            id="non-polling-http-400",
        ),
        pytest.param(
            nous_oauth_config(),
            900,
            [
                httpx.Response(
                    200, json={"access_token": "a", "refresh_token": "r", "scope": "profile"}
                )
            ],
            id="nous-without-inference-scope",
        ),
    ],
)
async def test_polling_ends_unsuccessfully_without_storing_a_token(
    engine: DeviceFlowEngine,
    store: TokenStore,
    poll_waits: list[float],
    config: OAuthConfig,
    expires_in: int,
    replies: list[httpx.Response],
) -> None:
    respx.post(config.device_auth_url).mock(
        return_value=httpx.Response(200, json={**GITHUB_AUTHORIZATION, "expires_in": expires_in})
    )
    polling = respx.post(config.token_url).mock(side_effect=replies)
    completion = Completion()

    await engine.connect("provider", "oauth", config, completion)

    assert await completion.wait() is False
    assert polling.call_count == len(replies)
    assert poll_waits == []
    assert store.load("provider", "oauth") is None


@respx.mock
@pytest.mark.asyncio
async def test_polling_crash_still_reports_failure_and_is_logged(
    engine: DeviceFlowEngine, store: TokenStore
) -> None:
    respx.post(GITHUB_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(200, json=GITHUB_AUTHORIZATION)
    )
    respx.post(GITHUB_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    logged = asyncio.Event()

    class _Signal(logging.Handler):
        @override
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage().startswith("OAuth polling task failed"):
                logged.set()

    handler = _Signal(level=logging.ERROR)
    logger = logging.getLogger("vbot.providers.auth_flow")
    logger.addHandler(handler)
    completion = Completion()
    try:
        await engine.connect("github-copilot", "oauth", github_oauth_config(), completion)
        assert await completion.wait() is False
        # Closing right after the crash must not swallow the task's failure report.
        await engine.aclose()
        await asyncio.wait_for(logged.wait(), timeout=5)
    finally:
        logger.removeHandler(handler)

    assert store.load("github-copilot", "oauth") is None
    assert not engine.is_flow_active("github-copilot", "oauth")


# ---------------------------------------------------------------------------
# Managed lifecycle: authorization and polling are owned per Account
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "close", "replace"])
async def test_pending_authorization_is_owned_and_stoppable(
    engine: DeviceFlowEngine, parked_polls: ParkedPolls, stop: str
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def authorize(_request: httpx.Request) -> httpx.Response:
        if not started.is_set():
            started.set()
            await release.wait()
        return httpx.Response(200, json=GITHUB_AUTHORIZATION)

    respx.post(GITHUB_DEVICE_AUTH_URL).mock(side_effect=authorize)
    polling = respx.post(GITHUB_TOKEN_URL).mock(return_value=PENDING)
    pending = asyncio.create_task(
        engine.connect("provider", "oauth", github_oauth_config(), Completion())
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert engine.is_flow_active("provider", "oauth")

        if stop == "cancel":
            engine.cancel_flow("provider", "oauth")
        elif stop == "close":
            await engine.aclose()
        else:
            await engine.connect("provider", "oauth", github_oauth_config(), Completion())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=5)

        if stop == "replace":
            await parked_polls.next()
            assert engine.is_flow_active("provider", "oauth")
            assert polling.call_count == 1
        else:
            assert not engine.is_flow_active("provider", "oauth")
            assert polling.call_count == 0
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "close"])
async def test_flow_stopped_before_polling_starts_never_polls(
    engine: DeviceFlowEngine, parked_polls: ParkedPolls, stop: str
) -> None:
    respx.post(GITHUB_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(200, json=GITHUB_AUTHORIZATION)
    )
    polling = respx.post(GITHUB_TOKEN_URL).mock(return_value=PENDING)
    completion = Completion()

    await engine.connect("provider", "oauth", github_oauth_config(), completion)
    assert engine.is_flow_active("provider", "oauth")
    if stop == "cancel":
        engine.cancel_flow("provider", "oauth")
        assert not engine.is_flow_active("provider", "oauth")
    await engine.aclose()

    assert not engine.is_flow_active("provider", "oauth")
    assert polling.call_count == 0
    assert completion.outcomes == []


@respx.mock
@pytest.mark.asyncio
async def test_replacing_one_account_flow_keeps_other_accounts_and_close_drains_all(
    engine: DeviceFlowEngine, parked_polls: ParkedPolls
) -> None:
    device_codes = iter(["device-1", "device-2", "device-3"])
    respx.post(GITHUB_DEVICE_AUTH_URL).mock(
        side_effect=lambda _request: httpx.Response(
            200, json={**GITHUB_AUTHORIZATION, "device_code": next(device_codes)}
        )
    )
    polling = respx.post(GITHUB_TOKEN_URL).mock(return_value=PENDING)
    config = github_oauth_config()

    await engine.connect("provider", "oauth", config, Completion())
    await parked_polls.next()
    await engine.connect("provider", "oauth", config, Completion(), account_id="work")
    await parked_polls.next()
    await engine.connect("provider", "oauth", config, Completion())
    await parked_polls.next()

    assert [request_body(call.request)["device_code"] for call in polling.calls] == [
        "device-1",
        "device-2",
        "device-3",
    ]
    assert parked_polls.cancelled == 1  # the replaced default-Account poll
    assert engine.is_flow_active("provider", "oauth")
    assert engine.is_flow_active("provider", "oauth", "work")

    engine.cancel_flow("provider", "oauth", "work")

    assert not engine.is_flow_active("provider", "oauth", "work")
    assert engine.is_flow_active("provider", "oauth")

    await engine.aclose()

    assert parked_polls.cancelled == 3
    assert not engine.is_flow_active("provider", "oauth")


@respx.mock
@pytest.mark.asyncio
async def test_closed_engine_rejects_new_authorization(engine: DeviceFlowEngine) -> None:
    authorization = respx.post(GITHUB_DEVICE_AUTH_URL).mock(
        return_value=httpx.Response(200, json=GITHUB_AUTHORIZATION)
    )
    await engine.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await engine.connect("provider", "oauth", github_oauth_config(), Completion())

    assert authorization.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_disconnect_after_authorization_response_cannot_start_polling(
    engine: DeviceFlowEngine, parked_polls: ParkedPolls
) -> None:
    def authorize(_request: httpx.Request) -> httpx.Response:
        # The response is ready, but connect's continuation has not run yet.
        asyncio.get_running_loop().call_soon(engine.cancel_flow, "provider", "oauth")
        return httpx.Response(200, json=GITHUB_AUTHORIZATION)

    respx.post(GITHUB_DEVICE_AUTH_URL).mock(side_effect=authorize)
    polling = respx.post(GITHUB_TOKEN_URL).mock(return_value=PENDING)

    with pytest.raises(asyncio.CancelledError):
        await engine.connect("provider", "oauth", github_oauth_config(), Completion())
    await engine.aclose()

    assert polling.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response_ready", [False, True])
async def test_cancelled_connect_retires_the_authorization_verifier(
    engine: DeviceFlowEngine, response_ready: bool
) -> None:
    """Cancellation can land after authorization stored a PKCE verifier.

    Staging that interleaving needs the private authorization seam, and the verifier
    store has no public observable (design smell kept deliberately).
    """

    async def authorize(*_args: object, **_kwargs: object) -> DeviceFlowSession:
        engine._minimax_code_verifiers[("provider", "oauth", "default", "user")] = "verifier"
        asyncio.get_running_loop().call_soon(pending.cancel)
        if not response_ready:
            # Cancellation can also arrive during HTTP-client cleanup after
            # authorization has already installed its verifier.
            await asyncio.Event().wait()
        return DeviceFlowSession("device", "user", "https://example.test", 900, 5)

    with (
        patch.object(engine, "_request_device_session", side_effect=authorize),
        patch.object(engine, "_poll_until_complete", AsyncMock()) as poll,
    ):
        pending = asyncio.create_task(
            engine.connect("provider", "oauth", minimax_oauth_config(), Completion())
        )
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert not engine._minimax_code_verifiers
    assert not engine.is_flow_active("provider", "oauth")
    poll.assert_not_awaited()
