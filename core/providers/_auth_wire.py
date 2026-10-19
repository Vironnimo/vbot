"""OAuth device authorization and token wire requests, response decoding and exchanges."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.providers.http import classify_http_status, wrap_network_error
from core.providers.openai_subscription_auth import openai_subscription_token_extra
from core.providers.providers import (
    MINIMAX_OAUTH_DEVICE_FLOW,
    NOUS_OAUTH_DEVICE_FLOW,
    OPENAI_CODEX_DEVICE_FLOW,
    OPENCODE_OAUTH_DEVICE_FLOW,
    XAI_OAUTH_DEVICE_FLOW,
    OAuthConfig,
    resolve_minimax_oauth_expiry,
)
from core.providers.token_getter import (
    COPILOT_EDITOR_VERSION,
    COPILOT_INTEGRATION_ID,
    OAUTH_SCOPE_EXTRA_KEY,
    copilot_token_extra,
    oauth_scope_value,
    validate_nous_oauth_scope,
)
from core.providers.token_store import OAuthToken
from core.utils.errors import ProviderError
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.auth_flow")

DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS = 5
DEFAULT_OPENAI_DEVICE_FLOW_EXPIRES_IN_SECONDS = 600
DEFAULT_COPILOT_TOKEN_LIFETIME_MINUTES = 25
HTTP_TIMEOUT_SECONDS = 60.0
OPENAI_DEVICE_CALLBACK_URI = "https://auth.openai.com/deviceauth/callback"
OPENAI_DEVICE_PENDING_STATUS_CODES = frozenset({403, 404})

AUTHORIZATION_PENDING_ERROR = "authorization_pending"
SLOW_DOWN_ERROR = "slow_down"
EXPIRED_TOKEN_ERROR = "expired_token"
ACCESS_DENIED_ERROR = "access_denied"
STANDARD_DEVICE_FLOW_ERRORS = frozenset(
    {
        AUTHORIZATION_PENDING_ERROR,
        SLOW_DOWN_ERROR,
        EXPIRED_TOKEN_ERROR,
        ACCESS_DENIED_ERROR,
    }
)
SLOW_DOWN_INTERVAL_INCREMENT_SECONDS = 5
MINIMAX_OAUTH_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:user_code"
MINIMAX_DEFAULT_POLL_INTERVAL_MILLISECONDS = 2000
MINIMAX_MINIMUM_POLL_INTERVAL_SECONDS = 2
MILLISECONDS_PER_SECOND = 1000
OPENCODE_DEVICE_CODE_PATH = "/auth/device/code"


def _is_standard_device_flow_error(response: httpx.Response) -> bool:
    """Allow RFC 8628 polling states through even when returned as HTTP 400."""

    if response.status_code != 400:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("error") in STANDARD_DEVICE_FLOW_ERRORS


def _minimax_pkce_pair() -> tuple[str, str, str]:
    """Generate MiniMax's PKCE verifier, S256 challenge, and CSRF state."""

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    return verifier, challenge.decode().rstrip("="), state


def _opencode_server_url(oauth_config: OAuthConfig) -> str:
    """Return the OpenCode console server that relative device-flow paths extend.

    OpenCode answers with paths such as ``/console/device?...``; its own client
    appends them to the server URL below which ``/auth/device/code`` lives.
    """

    return oauth_config.device_auth_url.removesuffix(OPENCODE_DEVICE_CODE_PATH)


@dataclass(frozen=True)
class DeviceFlowSession:
    """Initial Device Flow response shown to the user."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

    def __post_init__(self) -> None:
        # Every accessor may open this Provider-supplied value in a browser.
        # Validate after flow-specific relative paths have been resolved.
        if not isinstance(self.verification_uri, str):
            raise DeviceFlowTerminalError("invalid_authorization_response")
        try:
            uri = urlsplit(self.verification_uri)
            valid = (
                uri.scheme in {"http", "https"}
                and bool(uri.hostname)
                and not any(ord(char) < 33 for char in self.verification_uri)
            )
            # Access validates a supplied port, even when it is not needed here.
            _ = uri.port
        except ValueError:
            valid = False
        if not valid:
            raise DeviceFlowTerminalError("invalid_authorization_response")


class DeviceFlowTerminalError(ProviderError):
    """Terminal OAuth Device Flow failure."""

    def __init__(self, error_code: str) -> None:
        super().__init__(f"Device flow failed with error '{error_code}'", retryable=False)


async def _post_device_authorization(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
) -> httpx.Response:
    if _is_openai_codex_flow(oauth_config) or _is_opencode_flow(oauth_config):
        return await _post_json_device_authorization(client, oauth_config)

    try:
        response = await client.post(
            oauth_config.device_auth_url,
            data={
                "client_id": oauth_config.client_id,
                "scope": " ".join(oauth_config.scopes),
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _post_json_device_authorization(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
) -> httpx.Response:
    try:
        response = await client.post(
            oauth_config.device_auth_url,
            json={"client_id": oauth_config.client_id},
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _post_minimax_device_authorization(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    code_challenge: str,
    state: str,
) -> httpx.Response:
    try:
        response = await client.post(
            oauth_config.device_auth_url,
            data={
                "response_type": "code",
                "client_id": oauth_config.client_id,
                "scope": " ".join(oauth_config.scopes),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-request-id": str(uuid.uuid4()),
            },
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


def _device_session_from_response(
    oauth_config: OAuthConfig,
    data: dict[str, Any],
) -> DeviceFlowSession:
    if _is_openai_codex_flow(oauth_config):
        expires_in = data.get("expires_in", oauth_config.expires_in)
        if expires_in is None:
            expires_in = DEFAULT_OPENAI_DEVICE_FLOW_EXPIRES_IN_SECONDS
        verification_uri = (
            data.get("verification_uri")
            or data.get("verification_url")
            or oauth_config.verification_uri
        )
        if not isinstance(verification_uri, str) or not verification_uri:
            raise KeyError("verification_uri")
        return DeviceFlowSession(
            device_code=str(data["device_auth_id"]),
            user_code=str(data["user_code"]),
            verification_uri=verification_uri,
            expires_in=int(expires_in),
            interval=int(data.get("interval", DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS)),
        )

    verification_uri = (
        data.get("verification_uri_complete")
        or data.get("verification_uri")
        or data["verification_url"]
    )
    if _is_opencode_flow(oauth_config) and verification_uri.startswith("/"):
        verification_uri = _opencode_server_url(oauth_config) + verification_uri
    return DeviceFlowSession(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=verification_uri,
        expires_in=int(data["expires_in"]),
        interval=int(data.get("interval", DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS)),
    )


def _minimax_device_session_from_response(
    data: dict[str, Any],
    *,
    expected_state: str,
) -> DeviceFlowSession:
    if data.get("state") != expected_state:
        raise DeviceFlowTerminalError("state_mismatch")

    user_code = data["user_code"]
    verification_uri = data["verification_uri"]
    if not isinstance(user_code, str) or not user_code:
        raise DeviceFlowTerminalError("invalid_authorization_response")
    if not isinstance(verification_uri, str) or not verification_uri:
        raise DeviceFlowTerminalError("invalid_authorization_response")

    now = datetime.now(UTC)
    expires_at = resolve_minimax_oauth_expiry(data["expired_in"], now=now)
    expires_in = max(1, int((expires_at - now).total_seconds()))
    interval_milliseconds = int(data.get("interval", MINIMAX_DEFAULT_POLL_INTERVAL_MILLISECONDS))
    interval = max(
        MINIMAX_MINIMUM_POLL_INTERVAL_SECONDS,
        (interval_milliseconds + MILLISECONDS_PER_SECOND - 1) // MILLISECONDS_PER_SECOND,
    )
    return DeviceFlowSession(
        device_code=user_code,
        user_code=user_code,
        verification_uri=verification_uri,
        expires_in=expires_in,
        interval=interval,
    )


async def _request_device_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    device_code: str,
    user_code: str = "",
    code_verifier: str = "",
) -> dict[str, Any]:
    response = await retry_async(
        _post_device_token,
        client,
        oauth_config,
        device_code,
        user_code,
        code_verifier,
    )
    if (
        _is_openai_codex_flow(oauth_config)
        and response.status_code in OPENAI_DEVICE_PENDING_STATUS_CODES
    ):
        return {"error": AUTHORIZATION_PENDING_ERROR}
    data = dict(response.json())
    if _is_minimax_flow(oauth_config):
        status = data.get("status")
        if status == "success":
            return data
        if status == "error":
            return {"error": ACCESS_DENIED_ERROR}
        return {"error": AUTHORIZATION_PENDING_ERROR}
    return data


async def _post_device_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    device_code: str,
    user_code: str = "",
    code_verifier: str = "",
) -> httpx.Response:
    if _is_openai_codex_flow(oauth_config):
        return await _post_openai_device_token(
            client,
            oauth_config,
            device_code,
            user_code,
        )
    if _is_minimax_flow(oauth_config):
        return await _post_minimax_device_token(
            client,
            oauth_config,
            user_code,
            code_verifier,
        )
    if _is_opencode_flow(oauth_config):
        return await _post_opencode_device_token(
            client,
            oauth_config,
            device_code,
        )

    try:
        response = await client.post(
            oauth_config.token_url,
            data={
                "client_id": oauth_config.client_id,
                "device_code": device_code,
                "grant_type": DEVICE_CODE_GRANT_TYPE,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    if (
        _is_xai_flow(oauth_config) or _is_nous_flow(oauth_config)
    ) and _is_standard_device_flow_error(response):
        return response
    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _post_opencode_device_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    device_code: str,
) -> httpx.Response:
    try:
        response = await client.post(
            oauth_config.token_url,
            json={
                "grant_type": DEVICE_CODE_GRANT_TYPE,
                "device_code": device_code,
                "client_id": oauth_config.client_id,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    if _is_standard_device_flow_error(response):
        return response
    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _post_minimax_device_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    user_code: str,
    code_verifier: str,
) -> httpx.Response:
    try:
        response = await client.post(
            oauth_config.token_url,
            data={
                "grant_type": MINIMAX_OAUTH_GRANT_TYPE,
                "client_id": oauth_config.client_id,
                "user_code": user_code,
                "code_verifier": code_verifier,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _post_openai_device_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    device_auth_id: str,
    user_code: str,
) -> httpx.Response:
    try:
        response = await client.post(
            oauth_config.device_auth_url.replace("/usercode", "/token"),
            json={
                "device_auth_id": device_auth_id,
                "user_code": user_code,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    if response.status_code in OPENAI_DEVICE_PENDING_STATUS_CODES:
        return response
    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _build_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    token_data: dict[str, Any],
) -> OAuthToken:
    if _is_openai_codex_flow(oauth_config):
        return await _exchange_openai_codex_token(client, oauth_config, token_data)

    if _is_minimax_flow(oauth_config):
        if token_data.get("status") != "success":
            raise DeviceFlowTerminalError("invalid_token_response")
        for required_field in ("access_token", "refresh_token", "expired_in"):
            if not token_data.get(required_field):
                raise DeviceFlowTerminalError("invalid_token_response")
        return OAuthToken(
            access_token=str(token_data["access_token"]),
            refresh_token=str(token_data["refresh_token"]),
            expires_at=resolve_minimax_oauth_expiry(
                token_data["expired_in"], now=datetime.now(UTC)
            ),
        )

    if _is_nous_flow(oauth_config):
        for required_field in ("access_token", "refresh_token"):
            if not token_data.get(required_field):
                raise DeviceFlowTerminalError("invalid_token_response")
        try:
            validate_nous_oauth_scope(token_data, oauth_config.scopes)
        except ProviderError as exc:
            raise DeviceFlowTerminalError("missing_inference_invoke_scope") from exc
        return OAuthToken(
            access_token=str(token_data["access_token"]),
            refresh_token=str(token_data["refresh_token"]),
            expires_at=_expires_at_from_response(token_data),
            extra={OAUTH_SCOPE_EXTRA_KEY: oauth_scope_value(token_data, oauth_config.scopes)},
        )

    provider_oauth_token = str(token_data["access_token"])
    if oauth_config.token_exchange_url:
        return await _exchange_copilot_token(
            client,
            oauth_config.token_exchange_url,
            provider_oauth_token,
        )

    return _oauth_token_from_response(token_data)


async def _exchange_openai_codex_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    authorization_data: dict[str, Any],
) -> OAuthToken:
    response = await retry_async(
        _post_authorization_code_token,
        client,
        oauth_config,
        str(authorization_data["authorization_code"]),
        str(authorization_data["code_verifier"]),
    )
    token_data = response.json()
    if not isinstance(token_data, dict):
        raise DeviceFlowTerminalError("invalid_token_response")
    return _oauth_token_from_response(token_data, include_openai_extra=True)


async def _post_authorization_code_token(
    client: httpx.AsyncClient,
    oauth_config: OAuthConfig,
    authorization_code: str,
    code_verifier: str,
) -> httpx.Response:
    try:
        response = await client.post(
            oauth_config.token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": oauth_config.client_id,
                "code": authorization_code,
                "code_verifier": code_verifier,
                "redirect_uri": oauth_config.redirect_uri or OPENAI_DEVICE_CALLBACK_URI,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    classify_http_status(
        response.status_code,
        idempotent=False,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


async def _exchange_copilot_token(
    client: httpx.AsyncClient,
    token_exchange_url: str,
    github_oauth_token: str,
) -> OAuthToken:
    response = await retry_async(
        _get_token_exchange,
        client,
        token_exchange_url,
        github_oauth_token,
    )
    data = response.json()
    expires_at = _parse_expires_at(data.get("expires_at"))
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(minutes=DEFAULT_COPILOT_TOKEN_LIFETIME_MINUTES)
        _LOGGER.warning("Copilot token exchange response did not include expires_at")

    access_token = str(data["token"])
    return OAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=expires_at,
        extra=copilot_token_extra(data, github_oauth_token, access_token),
    )


async def _get_token_exchange(
    client: httpx.AsyncClient,
    token_exchange_url: str,
    github_oauth_token: str,
) -> httpx.Response:
    try:
        response = await client.get(
            token_exchange_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {github_oauth_token}",
                "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
                "Editor-Version": COPILOT_EDITOR_VERSION,
            },
        )
    except httpx.HTTPError as error:
        raise wrap_network_error(error) from error

    classify_http_status(
        response.status_code,
        idempotent=True,
        detail=response.text,
        response_headers=response.headers,
    )
    return response


def _oauth_token_from_response(
    data: dict[str, Any],
    *,
    include_openai_extra: bool = False,
) -> OAuthToken:
    access_token = str(data["access_token"])
    extra = openai_subscription_token_extra(access_token) if include_openai_extra else {}
    return OAuthToken(
        access_token=access_token,
        refresh_token=data.get("refresh_token"),
        expires_at=_expires_at_from_response(data),
        extra=extra,
    )


def _is_openai_codex_flow(oauth_config: OAuthConfig) -> bool:
    return oauth_config.device_flow == OPENAI_CODEX_DEVICE_FLOW


def _is_minimax_flow(oauth_config: OAuthConfig) -> bool:
    return oauth_config.device_flow == MINIMAX_OAUTH_DEVICE_FLOW


def _is_nous_flow(oauth_config: OAuthConfig) -> bool:
    return oauth_config.device_flow == NOUS_OAUTH_DEVICE_FLOW


def _is_opencode_flow(oauth_config: OAuthConfig) -> bool:
    return oauth_config.device_flow == OPENCODE_OAUTH_DEVICE_FLOW


def _is_xai_flow(oauth_config: OAuthConfig) -> bool:
    return oauth_config.device_flow == XAI_OAUTH_DEVICE_FLOW


def _expires_at_from_response(data: dict[str, Any]) -> datetime | None:
    expires_at = _parse_expires_at(data.get("expires_at"))
    if expires_at is not None:
        return expires_at

    expires_in = data.get("expires_in")
    if expires_in is None:
        return None
    return datetime.now(UTC) + timedelta(seconds=int(expires_in))


def _parse_expires_at(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
    if not isinstance(value, str) or not value:
        return None
    if value.isdecimal():
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
