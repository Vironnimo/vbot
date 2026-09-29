"""OAuthTokenGetter: stored tokens, per-flow refresh, Account-safe publication and failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from core.providers import token_getter
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.providers.providers import OAuthConfig
from core.providers.token_getter import (
    ROTATING_REFRESH_DEVICE_FLOWS,
    TOKEN_EXCHANGE_FALLBACK_MINUTES,
    OAuthTokenGetter,
)
from core.providers.token_store import OAuthToken, TokenStore
from core.utils import retry
from core.utils.tls import shared_ssl_context

from .oauth_test_support import (
    COPILOT_TOKEN_EXCHANGE_URL,
    OPENAI_TOKEN_URL,
    OPENCODE_TOKEN_URL,
    XAI_TOKEN_URL,
    expired_token,
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

FALLBACK_SECONDS = TOKEN_EXCHANGE_FALLBACK_MINUTES * 60
COPILOT_TOKEN = expired_token(refresh_token=None, extra={"github_oauth_token": "github-secret"})

_LEAKY_DESCRIPTION = "refresh token refresh-secret was revoked for user@example.com"
_LEAKED_FRAGMENTS = ("refresh-secret", "user@example.com", "revoked", "error_description")


@pytest.fixture
def store(tmp_path) -> TokenStore:
    return TokenStore(tmp_path)


def _getter_logs(caplog: pytest.LogCaptureFixture, level: int = logging.INFO) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.providers.token_getter" and record.levelno >= level
    ]


def _assert_sanitized(*texts: str) -> None:
    for text in texts:
        for fragment in _LEAKED_FRAGMENTS:
            assert fragment not in text


# ---------------------------------------------------------------------------
# Stored tokens and reconnect-required states
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_unexpired_stored_token_is_returned_without_refresh(store: TokenStore) -> None:
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken("stored-access", "refresh", datetime.now(UTC) + timedelta(minutes=5)),
    )

    getter = OAuthTokenGetter(store, "github-copilot", "oauth", github_oauth_config())

    assert await getter() == "stored-access"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "warning"),
    [
        pytest.param(None, "No usable OAuth token", id="missing-token"),
        pytest.param(COPILOT_TOKEN, "expired with no refresh path", id="no-refresh-path"),
    ],
)
async def test_unrefreshable_token_requires_reconnect(
    store: TokenStore,
    caplog: pytest.LogCaptureFixture,
    stored: OAuthToken | None,
    warning: str,
) -> None:
    """A missing token, or an exchange token without a configured exchange, needs reconnect."""

    if stored is not None:
        store.save("github-copilot", "oauth", stored)
    getter = OAuthTokenGetter(store, "github-copilot", "oauth", github_oauth_config())

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError),
    ):
        await getter()

    assert any(warning in message for message in _getter_logs(caplog, logging.WARNING))
    assert all(record.exc_info is None for record in caplog.records)


# ---------------------------------------------------------------------------
# Refresh requests per flow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefreshFlow:
    config: OAuthConfig
    reply: Callable[[], dict[str, Any]]
    request: dict[str, str]
    expires_in: float
    stored_extra: dict[str, str] = field(default_factory=dict)
    refreshed_extra: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


_ROTATED = {"access_token": "fresh-access", "refresh_token": "rotated-refresh", "expires_in": 900}
_FORM_REFRESH = {"grant_type": "refresh_token", "refresh_token": "refresh-secret"}

REFRESH_FLOWS = {
    # Non-rotating standard refresh; the OpenAI account claim is re-derived from the new JWT.
    "openai": RefreshFlow(
        config=openai_oauth_config(),
        reply=lambda: {**_ROTATED, "access_token": jwt_with_account("acct_new"), "expires_in": 120},
        request={**_FORM_REFRESH, "client_id": "openai-client-id"},
        expires_in=120,
        stored_extra={"chatgpt_account_id": "acct_old"},
        refreshed_extra={"chatgpt_account_id": "acct_new"},
    ),
    "xai": RefreshFlow(
        config=xai_oauth_config(),
        reply=lambda: _ROTATED,
        request={**_FORM_REFRESH, "client_id": "xai-client-id"},
        expires_in=900,
    ),
    # Nous sends the single-use refresh token in a header and records the granted scope.
    "nous": RefreshFlow(
        config=nous_oauth_config(),
        reply=lambda: {**_ROTATED, "scope": "inference:invoke"},
        request={"grant_type": "refresh_token", "client_id": "hermes-cli"},
        expires_in=900,
        refreshed_extra={"oauth_scope": "inference:invoke"},
        headers={"x-nous-refresh-token": "refresh-secret"},
    ),
    "opencode": RefreshFlow(
        config=opencode_oauth_config(),
        reply=lambda: _ROTATED,
        request={**_FORM_REFRESH, "client_id": "opencode-cli"},
        expires_in=900,
    ),
    # MiniMax reports success in the body and may send an absolute millisecond expiry.
    "minimax": RefreshFlow(
        config=minimax_oauth_config(),
        reply=lambda: {
            "status": "success",
            "access_token": "fresh-access",
            "refresh_token": "rotated-refresh",
            "expired_in": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp() * 1000),
        },
        request={**_FORM_REFRESH, "client_id": "minimax-client-id"},
        expires_in=900,
    ),
}


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("flow", list(REFRESH_FLOWS))
async def test_expired_token_refreshes_through_the_flow_and_publishes_to_its_account(
    store: TokenStore, caplog: pytest.LogCaptureFixture, flow: str
) -> None:
    case = REFRESH_FLOWS[flow]
    reply = case.reply()
    store.save(
        "provider", "subscription", expired_token(extra=case.stored_extra), account_id="work"
    )
    route = respx.post(case.config.token_url).mock(return_value=httpx.Response(200, json=reply))
    getter = OAuthTokenGetter(store, "provider", "subscription", case.config, account_id="work")

    with caplog.at_level(logging.DEBUG, logger="vbot.providers.token_getter"):
        assert await getter() == reply["access_token"]

    request = route.calls.last.request
    assert route.call_count == 1
    assert request_body(request) == case.request
    for header, value in case.headers.items():
        assert request.headers[header] == value
    stored = store.load("provider", "subscription", account_id="work")
    assert stored is not None
    assert (stored.access_token, stored.refresh_token) == (reply["access_token"], "rotated-refresh")
    assert stored.extra == case.refreshed_extra
    assert seconds_until(stored.expires_at) == pytest.approx(case.expires_in, abs=10)
    assert store.load("provider", "subscription") is None
    # A routine refresh is DEBUG detail and never names the Provider Account.
    assert _getter_logs(caplog) == []
    [refreshed] = _getter_logs(caplog, logging.DEBUG)
    assert "subscription" in refreshed
    assert "work" not in refreshed


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expiry_fields", "expires_in"),
    [
        pytest.param({"expires_in": 900}, 900, id="valid"),
        pytest.param({"expires_in": 10**100}, FALLBACK_SECONDS, id="large-integer"),
        pytest.param({"expires_in": "9" * 5000}, FALLBACK_SECONDS, id="integer-limit"),
        pytest.param(
            {"expires_at": "0001-01-01T00:00:00+14:00"}, FALLBACK_SECONDS, id="utc-underflow"
        ),
    ],
)
async def test_unusable_expiry_falls_back_and_still_publishes_the_rotated_token(
    store: TokenStore, expiry_fields: dict[str, object], expires_in: int
) -> None:
    store.save("opencode-zen", "account", expired_token())
    respx.post(OPENCODE_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fresh-access", "refresh_token": "rotated", **expiry_fields},
        )
    )
    getter = OAuthTokenGetter(store, "opencode-zen", "account", opencode_oauth_config())

    assert await getter() == "fresh-access"

    stored = store.load("opencode-zen", "account")
    assert stored is not None
    assert stored.refresh_token == "rotated"
    assert seconds_until(stored.expires_at) == pytest.approx(expires_in, abs=10)


_IN_30_MINUTES = datetime.now(UTC) + timedelta(minutes=30)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expires_in", "endpoint"),
    [
        pytest.param(
            {
                "token": "copilot-token",
                "expires_at": _IN_30_MINUTES.timestamp(),
                "endpoints": {"api": "https://api.example.enterprise.githubcopilot.com/"},
            },
            None,
            "https://api.example.enterprise.githubcopilot.com",
            id="trusted-endpoint",
        ),
        pytest.param(
            {
                "token": "proxy-ep=proxy.business.githubcopilot.com;exp=123",
                "expires_at": "9999-12-31T23:59:59-14:00",
                "endpoints": {"api": "https://attacker.example/api"},
            },
            FALLBACK_SECONDS,
            "https://api.business.githubcopilot.com",
            id="untrusted-endpoint-uses-token-proxy",
        ),
        pytest.param({"token": "copilot-token"}, FALLBACK_SECONDS, None, id="no-endpoint"),
    ],
)
async def test_copilot_exchange_refresh_is_coalesced_and_keeps_only_trusted_endpoints(
    store: TokenStore,
    reply: dict[str, object],
    expires_in: int | None,
    endpoint: str | None,
) -> None:
    store.save("github-copilot", "oauth", COPILOT_TOKEN)
    route = respx.get(COPILOT_TOKEN_EXCHANGE_URL).mock(return_value=httpx.Response(200, json=reply))
    getter = OAuthTokenGetter(
        store, "github-copilot", "oauth", github_oauth_config(token_exchange=True)
    )

    assert await asyncio.gather(getter(), getter()) == [reply["token"]] * 2

    assert route.call_count == 1
    headers = route.calls.last.request.headers
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"] == "Bearer github-secret"
    assert headers["Copilot-Integration-Id"] == "vscode-chat"
    assert headers["Editor-Version"] == "vscode/1.128.0"
    stored = store.load("github-copilot", "oauth")
    assert stored is not None
    assert stored.access_token == reply["token"]
    expected_extra = {"github_oauth_token": "github-secret"}
    if endpoint is not None:
        expected_extra["copilot_api_endpoint"] = endpoint
    assert stored.extra == expected_extra
    if expires_in is None:
        assert stored.expires_at == _IN_30_MINUTES
    else:
        assert seconds_until(stored.expires_at) == pytest.approx(expires_in, abs=10)


# ---------------------------------------------------------------------------
# HTTP client ownership
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_injected_client_serves_refresh_and_stays_open(store: TokenStore) -> None:
    store.save("github-copilot", "oauth", COPILOT_TOKEN)
    route = respx.get(COPILOT_TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"token": "fresh"})
    )

    async with httpx.AsyncClient(verify=shared_ssl_context()) as client:
        getter = OAuthTokenGetter(
            store, "github-copilot", "oauth", github_oauth_config(token_exchange=True), client
        )
        assert await getter() == "fresh"
        assert not client.is_closed

    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_owned_refresh_client_is_closed_after_its_request(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The getter has no observable for its per-request clients, so creation is recorded."""

    created: list[httpx.AsyncClient] = []

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            created.append(self)

    monkeypatch.setattr(token_getter.httpx, "AsyncClient", RecordingClient)
    store.save("github-copilot", "oauth", COPILOT_TOKEN)
    respx.get(COPILOT_TOKEN_EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"token": "fresh"})
    )
    getter = OAuthTokenGetter(
        store, "github-copilot", "oauth", github_oauth_config(token_exchange=True)
    )

    assert await getter() == "fresh"

    assert len(created) == 1
    assert created[0].is_closed


# ---------------------------------------------------------------------------
# Delayed refresh results against concurrent Account changes
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "stored", "reply", "replacement"),
    [
        pytest.param(
            github_oauth_config(token_exchange=True),
            COPILOT_TOKEN,
            httpx.Response(200, json={"token": "late-result"}),
            None,
            id="exchange-result-after-disconnect",
        ),
        pytest.param(
            minimax_oauth_config(),
            expired_token(),
            httpx.Response(
                200,
                json={
                    "status": "success",
                    "access_token": "late-result",
                    "refresh_token": "late-rotation",
                    "expired_in": 3600,
                },
            ),
            OAuthToken("new-login", "new-refresh"),
            id="refresh-result-after-reconnect",
        ),
        pytest.param(
            minimax_oauth_config(),
            expired_token(),
            httpx.Response(400, json={"error": "invalid_grant"}),
            OAuthToken("new-login", "new-refresh"),
            id="rejected-rotating-refresh-after-reconnect",
        ),
    ],
)
async def test_delayed_refresh_cannot_revive_or_remove_replaced_credentials(
    store: TokenStore,
    config: OAuthConfig,
    stored: OAuthToken,
    reply: httpx.Response,
    replacement: OAuthToken | None,
) -> None:
    """Disconnect or reconnect during the token request supersedes its result."""

    store.save("provider", "oauth", stored, account_id="work")
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return reply

    route = respx.route(url=config.token_exchange_url or config.token_url)
    route.mock(side_effect=respond)
    getter = OAuthTokenGetter(store, "provider", "oauth", config, account_id="work")
    pending = asyncio.create_task(getter())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if replacement is None:
            store.delete("provider", "oauth", account_id="work")
        else:
            store.save("provider", "oauth", replacement, account_id="work")
        release.set()

        if replacement is not None and reply.status_code == 200:
            assert await pending == replacement.access_token
        else:
            with pytest.raises(ProviderAuthError):
                await pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)

    assert store.load("provider", "oauth", account_id="work") == replacement
    assert store.load("provider", "oauth") is None


# ---------------------------------------------------------------------------
# Refresh failures: classification, sanitizing, quarantine and no replay
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "stored", "reply", "auth_failure"),
    [
        pytest.param(
            github_oauth_config(token_exchange=True),
            COPILOT_TOKEN,
            httpx.Response(200, text="<html>captive portal</html>"),
            False,
            id="copilot-exchange-not-json",
        ),
        pytest.param(
            xai_oauth_config(),
            expired_token(),
            httpx.Response(200, text="<html>captive portal</html>"),
            False,
            id="rotating-refresh-not-json",
        ),
        pytest.param(
            openai_oauth_config(),
            expired_token(),
            httpx.Response(200, json=["not", "an", "object"]),
            True,
            id="json-that-is-not-an-object",
        ),
    ],
)
async def test_malformed_token_endpoint_json_is_fatal_provider_error_and_keeps_token(
    store: TokenStore,
    config: OAuthConfig,
    stored: OAuthToken,
    reply: httpx.Response,
    auth_failure: bool,
) -> None:
    """A non-JSON 2xx reply is malformed output: not replayed and never a quarantine."""

    store.save("provider", "oauth", stored)
    route = respx.route(url=config.token_exchange_url or config.token_url).mock(return_value=reply)
    getter = OAuthTokenGetter(store, "provider", "oauth", config)

    with pytest.raises(ProviderError) as caught:
        await getter()

    assert isinstance(caught.value, ProviderAuthError) is auth_failure
    assert caught.value.retryable is False
    if not auth_failure:
        assert isinstance(caught.value.__cause__, ValueError)
    assert route.call_count == 1
    assert store.load("provider", "oauth") == stored


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "quarantined"),
    [
        pytest.param(openai_oauth_config(), False, id="standard-keeps-token"),
        pytest.param(xai_oauth_config(), True, id="rotating-quarantines-token"),
    ],
)
async def test_terminal_refresh_failure_reports_status_and_oauth_error_code(
    store: TokenStore,
    caplog: pytest.LogCaptureFixture,
    config: OAuthConfig,
    quarantined: bool,
) -> None:
    """A rejected refresh names the HTTP status and RFC 6749 code, never the body.

    Only rotating flows delete the stored login.
    """

    original = expired_token()
    store.save("provider", "subscription", original)
    route = respx.post(config.token_url).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": _LEAKY_DESCRIPTION}
        )
    )
    getter = OAuthTokenGetter(store, "provider", "subscription", config)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError) as caught,
    ):
        await getter()

    message = str(caught.value)
    assert message == "OAuth token refresh failed (HTTP 400, invalid_grant) — please reconnect"
    warnings = _getter_logs(caplog, logging.WARNING)
    assert any("HTTP 400, invalid_grant" in warning for warning in warnings)
    assert all(record.exc_info is None for record in caplog.records)
    _assert_sanitized(message, *warnings)
    assert route.call_count == 1
    assert store.load("provider", "subscription") == (None if quarantined else original)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_detail"),
    [
        pytest.param(
            {"error": {"code": "refresh_token_reused", "message": _LEAKY_DESCRIPTION}},
            "HTTP 401, refresh_token_reused",
            id="nested-code",
        ),
        pytest.param(
            {"error": "unauthenticated:bad-credentials"},
            "HTTP 401, unauthenticated:bad-credentials",
            id="colon-code",
        ),
        pytest.param({"error": "invalid_grant refresh-secret"}, "HTTP 401", id="whitespace"),
        pytest.param({"error": "invalid_grant\n"}, "HTTP 401", id="trailing-newline"),
        pytest.param({"error": "x" * 65}, "HTTP 401", id="too-long"),
        pytest.param({"error": 401}, "HTTP 401", id="non-string"),
        pytest.param(["invalid_grant"], "HTTP 401", id="non-object"),
        pytest.param(f"invalid_grant: {_LEAKY_DESCRIPTION}", "HTTP 401", id="plain-text"),
    ],
)
async def test_refresh_failure_keeps_only_a_valid_oauth_error_code(
    store: TokenStore,
    caplog: pytest.LogCaptureFixture,
    body: object,
    expected_detail: str,
) -> None:
    store.save("openai", "subscription", expired_token())
    reply = (
        httpx.Response(401, text=body) if isinstance(body, str) else httpx.Response(401, json=body)
    )
    respx.post(OPENAI_TOKEN_URL).mock(return_value=reply)
    getter = OAuthTokenGetter(store, "openai", "subscription", openai_oauth_config())

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderAuthError) as caught,
    ):
        await getter()

    message = str(caught.value)
    assert message == f"OAuth token refresh failed ({expected_detail}) — please reconnect"
    _assert_sanitized(message, *_getter_logs(caplog, logging.WARNING))


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_code", "error_type", "expected_message"),
    [
        (
            429,
            "slow_down",
            ProviderRateLimitError,
            "OAuth token refresh rate limited (HTTP 429, slow_down)",
        ),
        (
            503,
            "temporarily_unavailable",
            ProviderError,
            "OAuth token endpoint unavailable (HTTP 503, temporarily_unavailable)",
        ),
    ],
)
async def test_retryable_refresh_failure_is_sanitized_and_keeps_token(
    store: TokenStore,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    error_code: str,
    error_type: type[ProviderError],
    expected_message: str,
) -> None:
    original = expired_token()
    store.save("xai", "subscription", original)
    respx.post(XAI_TOKEN_URL).mock(
        return_value=httpx.Response(
            status_code, json={"error": error_code, "error_description": _LEAKY_DESCRIPTION}
        )
    )
    getter = OAuthTokenGetter(store, "xai", "subscription", xai_oauth_config())

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderError) as caught,
    ):
        await getter()

    assert type(caught.value) is error_type
    assert caught.value.retryable is True
    message = str(caught.value)
    assert message == expected_message
    warnings = _getter_logs(caplog, logging.WARNING)
    assert any(expected_message in warning for warning in warnings)
    _assert_sanitized(message, *warnings)
    assert store.load("xai", "subscription") == original


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "status_code"),
    [
        pytest.param(openai_oauth_config(), 500, id="standard-500"),
        pytest.param(xai_oauth_config(), 522, id="rotating-cdn-522"),
        pytest.param(opencode_oauth_config(), 408, id="rotating-408"),
    ],
)
async def test_server_side_refresh_failure_is_not_an_authentication_failure(
    store: TokenStore,
    caplog: pytest.LogCaptureFixture,
    config: OAuthConfig,
    status_code: int,
) -> None:
    """A server-side failure is neither replayed nor reported as a login to reconnect."""

    original = expired_token()
    store.save("provider", "subscription", original)
    route = respx.post(config.token_url).mock(
        return_value=httpx.Response(
            status_code, json={"error": "server_error", "error_description": _LEAKY_DESCRIPTION}
        )
    )
    getter = OAuthTokenGetter(store, "provider", "subscription", config)

    with (
        caplog.at_level(logging.WARNING, logger="vbot.providers.token_getter"),
        pytest.raises(ProviderError) as caught,
    ):
        await getter()

    assert type(caught.value) is ProviderError
    assert caught.value.retryable is False
    message = str(caught.value)
    assert f"HTTP {status_code}, server_error" in message
    _assert_sanitized(message, *_getter_logs(caplog, logging.WARNING))
    assert route.call_count == 1
    assert store.load("provider", "subscription") == original


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "failure"),
    [
        pytest.param(xai_oauth_config(), httpx.ReadError("lost"), id="xai-lost-response"),
        pytest.param(opencode_oauth_config(), httpx.ReadError("lost"), id="opencode-lost-response"),
        pytest.param(nous_oauth_config(), httpx.Response(503), id="nous-503"),
        pytest.param(minimax_oauth_config(), httpx.Response(503), id="minimax-503"),
        # Neither a server-side failure nor an auth failure without the
        # ``invalid_grant`` code proves the refresh token dead.
        pytest.param(
            xai_oauth_config(), httpx.Response(500, json={"error": "server_error"}), id="xai-500"
        ),
        pytest.param(
            xai_oauth_config(),
            httpx.Response(500, json={"error": "invalid_grant"}),
            id="xai-500-with-invalid-grant",
        ),
        pytest.param(
            opencode_oauth_config(), httpx.Response(522, text="error code: 522"), id="opencode-522"
        ),
        pytest.param(minimax_oauth_config(), httpx.Response(408), id="minimax-408"),
        pytest.param(
            nous_oauth_config(),
            httpx.Response(403, text="<html>Attention Required</html>"),
            id="nous-waf-403",
        ),
        pytest.param(opencode_oauth_config(), httpx.Response(404), id="opencode-misrouted-404"),
        pytest.param(xai_oauth_config(), httpx.Response(401), id="xai-401-without-oauth-error"),
        pytest.param(
            minimax_oauth_config(),
            httpx.Response(400, text="<html>Bad Request</html>"),
            id="minimax-400-not-json",
        ),
        pytest.param(
            xai_oauth_config(),
            httpx.Response(400, json={"error": "invalid_request"}),
            id="xai-400-invalid-request",
        ),
        pytest.param(
            opencode_oauth_config(),
            httpx.Response(401, json={"error": "invalid_client"}),
            id="opencode-401-invalid-client",
        ),
    ],
)
async def test_rotating_refresh_is_never_replayed_and_preserves_token(
    store: TokenStore,
    monkeypatch: pytest.MonkeyPatch,
    config: OAuthConfig,
    failure: httpx.Response | Exception,
) -> None:
    """Every rotating flow follows ``ROTATING_REFRESH_DEVICE_FLOWS``.

    A replay after a lost response would send the retired refresh token; its auth
    rejection would then delete the still-valid stored login. Only a definite
    rejection (``test_rotating_refresh_rejection_quarantines_the_login``) deletes
    it; every other failure leaves the login for the next attempt.
    """

    assert config.device_flow in ROTATING_REFRESH_DEVICE_FLOWS
    retry_waits: list[float] = []

    async def record_wait(seconds: float) -> None:
        retry_waits.append(seconds)

    monkeypatch.setattr(retry, "_sleep", record_wait)
    original = expired_token()
    store.save("provider", "subscription", original)
    outcome = (
        {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
    )
    route = respx.post(config.token_url).mock(**outcome)
    getter = OAuthTokenGetter(store, "provider", "subscription", config)

    with pytest.raises(NetworkError if isinstance(failure, Exception) else ProviderError):
        await getter()

    assert route.call_count == 1
    assert retry_waits == []
    assert store.load("provider", "subscription") == original


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "reply"),
    [
        pytest.param(
            nous_oauth_config(),
            httpx.Response(400, text="refresh_token_reused: reuse detected"),
            id="nous-reuse-detected",
        ),
        pytest.param(
            minimax_oauth_config(),
            httpx.Response(200, json={"status": "error"}),
            id="minimax-unsuccessful-status",
        ),
        pytest.param(
            xai_oauth_config(),
            httpx.Response(401, json={"error": "invalid_grant"}),
            id="xai-401-invalid-grant",
        ),
        pytest.param(
            opencode_oauth_config(),
            httpx.Response(400, json={"error": {"code": "invalid_grant"}}),
            id="opencode-400-nested-invalid-grant",
        ),
    ],
)
async def test_rotating_refresh_rejection_quarantines_the_login(
    store: TokenStore, config: OAuthConfig, reply: httpx.Response
) -> None:
    store.save("provider", "subscription", expired_token())
    route = respx.post(config.token_url).mock(return_value=reply)
    getter = OAuthTokenGetter(store, "provider", "subscription", config)

    with pytest.raises(ProviderAuthError):
        await getter()

    assert route.call_count == 1
    assert store.load("provider", "subscription") is None
