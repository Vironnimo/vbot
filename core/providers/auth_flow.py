"""OAuth Device Flow orchestration for provider authentication."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.providers import OAuthConfig
from core.providers.token_store import OAuthToken, TokenStore
from core.utils.errors import ProviderError
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.auth_flow")

DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS = 5
DEFAULT_COPILOT_TOKEN_LIFETIME_MINUTES = 25
HTTP_TIMEOUT_SECONDS = 60.0
COPILOT_INTEGRATION_ID = "vscode-chat"
COPILOT_EDITOR_VERSION = "vBot/0.1.0"

AUTHORIZATION_PENDING_ERROR = "authorization_pending"
SLOW_DOWN_ERROR = "slow_down"
EXPIRED_TOKEN_ERROR = "expired_token"
ACCESS_DENIED_ERROR = "access_denied"
SLOW_DOWN_INTERVAL_INCREMENT_SECONDS = 5


OnCompleteCallback = Callable[..., None | Awaitable[None]]


@dataclass(frozen=True)
class DeviceFlowSession:
    """Initial Device Flow response shown to the user."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceFlowEngine:
    """Runs OAuth Device Flow polling and persists completed provider tokens."""

    def __init__(self, token_store: TokenStore) -> None:
        self._token_store = token_store
        self._active_flows: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def start_device_flow(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
    ) -> DeviceFlowSession:
        """Request a Device Flow session from the provider."""

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await retry_async(
                self._post_device_authorization,
                client,
                oauth_config,
            )

        data = response.json()
        session = DeviceFlowSession(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data.get("verification_uri") or data["verification_url"],
            expires_in=int(data["expires_in"]),
            interval=int(data.get("interval", DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS)),
        )
        _LOGGER.info(
            "Started OAuth device flow for provider '%s' connection '%s'",
            provider_id,
            local_connection_id,
        )
        return session

    async def _poll_for_token(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
        device_code: str,
        interval: int,
        expires_in: int,
        on_complete: OnCompleteCallback,
    ) -> None:
        """Poll for Device Flow completion, store the token, and notify the caller."""

        flow_key = (provider_id, local_connection_id)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_flows[flow_key] = current_task

        try:
            await self._poll_until_complete(
                provider_id,
                local_connection_id,
                oauth_config,
                device_code,
                interval,
                expires_in,
            )
        except asyncio.CancelledError:
            raise
        except DeviceFlowTerminalError as error:
            _LOGGER.warning(
                "OAuth device flow failed for provider '%s' connection '%s': %s",
                provider_id,
                local_connection_id,
                error,
            )
            await self._notify_complete(on_complete, success=False)
        except ProviderError as error:
            _LOGGER.warning(
                "OAuth device flow failed for provider '%s' connection '%s': %s",
                provider_id,
                local_connection_id,
                error.__class__.__name__,
            )
            await self._notify_complete(on_complete, success=False)
        except Exception:
            _LOGGER.error(
                "OAuth device flow crashed for provider '%s' connection '%s'",
                provider_id,
                local_connection_id,
            )
            await self._notify_complete(on_complete, success=False)
            raise
        else:
            await self._notify_complete(on_complete, success=True)
        finally:
            if self._active_flows.get(flow_key) is current_task:
                self._active_flows.pop(flow_key, None)

    def cancel_flow(self, provider_id: str, local_connection_id: str) -> None:
        """Cancel any in-flight polling task for the provider connection."""

        task = self._active_flows.pop((provider_id, local_connection_id), None)
        if task is not None and not task.done():
            task.cancel()

    async def aclose(self) -> None:
        """Cancel and await all active Device Flow polling tasks."""
        tasks = list(self._active_flows.values())
        self._active_flows.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

        pending_tasks = [task for task in tasks if not task.done()]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    async def _poll_until_complete(
        self,
        provider_id: str,
        local_connection_id: str,
        oauth_config: OAuthConfig,
        device_code: str,
        interval: int,
        expires_in: int,
    ) -> None:
        poll_interval = interval
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            while True:
                if datetime.now(UTC) >= expires_at:
                    raise DeviceFlowTerminalError(EXPIRED_TOKEN_ERROR)

                data = await self._request_device_token(client, oauth_config, device_code)
                if self._is_pending_response(data):
                    poll_interval = self._next_interval(data, poll_interval)
                    await asyncio.sleep(self._bounded_poll_sleep(poll_interval, expires_at))
                    continue

                if self._is_terminal_error(data):
                    raise DeviceFlowTerminalError(data["error"])

                github_oauth_token = str(data["access_token"])
                token = await self._build_token(client, oauth_config, data, github_oauth_token)
                self._token_store.save(provider_id, local_connection_id, token)
                return

    async def _post_device_authorization(
        self,
        client: httpx.AsyncClient,
        oauth_config: OAuthConfig,
    ) -> httpx.Response:
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

        classify_http_status(response.status_code, detail=response.text)
        return response

    async def _request_device_token(
        self,
        client: httpx.AsyncClient,
        oauth_config: OAuthConfig,
        device_code: str,
    ) -> dict[str, Any]:
        response = await retry_async(
            self._post_device_token,
            client,
            oauth_config,
            device_code,
        )
        return dict(response.json())

    async def _post_device_token(
        self,
        client: httpx.AsyncClient,
        oauth_config: OAuthConfig,
        device_code: str,
    ) -> httpx.Response:
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

        classify_http_status(response.status_code, detail=response.text)
        return response

    async def _build_token(
        self,
        client: httpx.AsyncClient,
        oauth_config: OAuthConfig,
        token_data: dict[str, Any],
        github_oauth_token: str,
    ) -> OAuthToken:
        if oauth_config.token_exchange_url:
            return await self._exchange_copilot_token(
                client,
                oauth_config.token_exchange_url,
                github_oauth_token,
            )

        return OAuthToken(
            access_token=github_oauth_token,
            refresh_token=token_data.get("refresh_token"),
            expires_at=self._expires_at_from_response(token_data),
        )

    async def _exchange_copilot_token(
        self,
        client: httpx.AsyncClient,
        token_exchange_url: str,
        github_oauth_token: str,
    ) -> OAuthToken:
        response = await retry_async(
            self._get_token_exchange,
            client,
            token_exchange_url,
            github_oauth_token,
        )
        data = response.json()
        expires_at = self._parse_expires_at(data.get("expires_at"))
        if expires_at is None:
            expires_at = datetime.now(UTC) + timedelta(
                minutes=DEFAULT_COPILOT_TOKEN_LIFETIME_MINUTES
            )
            _LOGGER.warning("Copilot token exchange response did not include expires_at")

        return OAuthToken(
            access_token=str(data["token"]),
            refresh_token=None,
            expires_at=expires_at,
            extra={"github_oauth_token": github_oauth_token},
        )

    async def _get_token_exchange(
        self,
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

        classify_http_status(response.status_code, detail=response.text)
        return response

    def _is_pending_response(self, data: dict[str, Any]) -> bool:
        return data.get("error") in {AUTHORIZATION_PENDING_ERROR, SLOW_DOWN_ERROR}

    def _is_terminal_error(self, data: dict[str, Any]) -> bool:
        return data.get("error") in {EXPIRED_TOKEN_ERROR, ACCESS_DENIED_ERROR}

    def _next_interval(self, data: dict[str, Any], current_interval: int) -> int:
        if data.get("error") == SLOW_DOWN_ERROR:
            return current_interval + SLOW_DOWN_INTERVAL_INCREMENT_SECONDS
        return current_interval

    def _bounded_poll_sleep(self, poll_interval: int, expires_at: datetime) -> float:
        remaining_seconds = (expires_at - datetime.now(UTC)).total_seconds()
        if remaining_seconds <= 0:
            return 0.0
        return min(float(poll_interval), remaining_seconds)

    def _expires_at_from_response(self, data: dict[str, Any]) -> datetime | None:
        expires_at = self._parse_expires_at(data.get("expires_at"))
        if expires_at is not None:
            return expires_at

        expires_in = data.get("expires_in")
        if expires_in is None:
            return None
        return datetime.now(UTC) + timedelta(seconds=int(expires_in))

    def _parse_expires_at(self, value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    async def _notify_complete(self, on_complete: OnCompleteCallback, *, success: bool) -> None:
        result = on_complete(success=success)
        if inspect.isawaitable(result):
            await result


class DeviceFlowTerminalError(ProviderError):
    """Terminal OAuth Device Flow failure."""

    def __init__(self, error_code: str) -> None:
        super().__init__(f"Device flow failed with error '{error_code}'", retryable=False)
