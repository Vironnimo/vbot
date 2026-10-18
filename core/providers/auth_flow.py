"""OAuth Device Flow orchestration for provider authentication."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.providers import _auth_wire as _wire
from core.providers._auth_wire import (
    ACCESS_DENIED_ERROR as ACCESS_DENIED_ERROR,
)
from core.providers._auth_wire import (
    AUTHORIZATION_PENDING_ERROR as AUTHORIZATION_PENDING_ERROR,
)
from core.providers._auth_wire import (
    DEFAULT_COPILOT_TOKEN_LIFETIME_MINUTES as DEFAULT_COPILOT_TOKEN_LIFETIME_MINUTES,
)
from core.providers._auth_wire import (
    DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS as DEFAULT_DEVICE_FLOW_INTERVAL_SECONDS,
)
from core.providers._auth_wire import (
    DEFAULT_OPENAI_DEVICE_FLOW_EXPIRES_IN_SECONDS as DEFAULT_OPENAI_DEVICE_FLOW_EXPIRES_IN_SECONDS,
)
from core.providers._auth_wire import (
    DEVICE_CODE_GRANT_TYPE as DEVICE_CODE_GRANT_TYPE,
)
from core.providers._auth_wire import (
    EXPIRED_TOKEN_ERROR as EXPIRED_TOKEN_ERROR,
)
from core.providers._auth_wire import (
    HTTP_TIMEOUT_SECONDS as HTTP_TIMEOUT_SECONDS,
)
from core.providers._auth_wire import (
    MILLISECONDS_PER_SECOND as MILLISECONDS_PER_SECOND,
)
from core.providers._auth_wire import (
    MINIMAX_DEFAULT_POLL_INTERVAL_MILLISECONDS as MINIMAX_DEFAULT_POLL_INTERVAL_MILLISECONDS,
)
from core.providers._auth_wire import (
    MINIMAX_MINIMUM_POLL_INTERVAL_SECONDS as MINIMAX_MINIMUM_POLL_INTERVAL_SECONDS,
)
from core.providers._auth_wire import (
    MINIMAX_OAUTH_GRANT_TYPE as MINIMAX_OAUTH_GRANT_TYPE,
)
from core.providers._auth_wire import (
    OPENAI_DEVICE_CALLBACK_URI as OPENAI_DEVICE_CALLBACK_URI,
)
from core.providers._auth_wire import (
    OPENAI_DEVICE_PENDING_STATUS_CODES as OPENAI_DEVICE_PENDING_STATUS_CODES,
)
from core.providers._auth_wire import (
    OPENCODE_DEVICE_CODE_PATH as OPENCODE_DEVICE_CODE_PATH,
)
from core.providers._auth_wire import (
    SLOW_DOWN_ERROR as SLOW_DOWN_ERROR,
)
from core.providers._auth_wire import (
    SLOW_DOWN_INTERVAL_INCREMENT_SECONDS as SLOW_DOWN_INTERVAL_INCREMENT_SECONDS,
)
from core.providers._auth_wire import (
    STANDARD_DEVICE_FLOW_ERRORS as STANDARD_DEVICE_FLOW_ERRORS,
)
from core.providers._auth_wire import DeviceFlowSession as DeviceFlowSession
from core.providers._auth_wire import DeviceFlowTerminalError as DeviceFlowTerminalError
from core.providers.accounts import Account
from core.providers.providers import (
    OAuthConfig,
)
from core.providers.token_store import TokenStore
from core.utils.errors import ProviderError
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.tls import shared_ssl_context
from core.utils.workers import cancel_and_wait

_LOGGER = get_logger("providers.auth_flow")

# Tests patch this seam instead of the process-wide ``asyncio.sleep``.
_sleep = asyncio.sleep

OnCompleteCallback = Callable[..., None | Awaitable[None]]


class DeviceFlowEngine:
    """Runs OAuth Device Flow polling and persists completed provider tokens."""

    def __init__(self, token_store: TokenStore) -> None:
        self._token_store = token_store
        self._active_flows: dict[Account, asyncio.Task[None]] = {}
        self._poll_tasks: set[asyncio.Task[None]] = set()
        self._active_authorizations: dict[Account, asyncio.Task[DeviceFlowSession]] = {}
        self._authorization_tasks: set[asyncio.Task[DeviceFlowSession]] = set()
        self._closed = False
        self._flow_locks: dict[Account, asyncio.Lock] = {}
        self._flow_epochs: dict[Account, int] = {}
        self._minimax_code_verifiers: dict[tuple[Account, str], str] = {}

    async def connect(
        self,
        account: Account,
        oauth_config: OAuthConfig,
        on_complete: OnCompleteCallback,
    ) -> DeviceFlowSession:
        """Authorize and own polling before returning the user-facing session.

        A replacement cancels previous authorization/polling for this Account. Cancelled
        tasks remain owned until settled, so shutdown also drains replacements.
        """
        if self._closed:
            raise RuntimeError("Device Flow engine is closed")
        flow_key = account
        async with self._flow_lock(flow_key):
            await self._cancel_flow_and_wait(account)
            if self._closed:
                raise RuntimeError("Device Flow engine is closed")
            authorization = asyncio.create_task(
                self._request_device_session(account, oauth_config),
                name=f"device-flow-authorize:{account.provider_id}:{account.local_connection_id}",
            )
            self._active_authorizations[flow_key] = authorization
            self._authorization_tasks.add(authorization)
        try:
            session = await authorization
            if self._closed or self._active_authorizations.get(flow_key) is not authorization:
                self._minimax_code_verifiers.pop((flow_key, session.user_code), None)
                raise asyncio.CancelledError
        except BaseException:
            # Cancellation may arrive after the child stored a verifier but
            # before this await delivered its session. Retire only this Account's
            # still-current authorization, never a replacement flow's verifier.
            if self._active_authorizations.get(flow_key) is authorization:
                self._drop_minimax_verifiers(flow_key)
            raise
        finally:
            self._authorization_tasks.discard(authorization)
            if self._active_authorizations.get(flow_key) is authorization:
                self._active_authorizations.pop(flow_key, None)
        task = asyncio.create_task(
            self._poll_for_token(
                account,
                oauth_config,
                session.device_code,
                session.interval,
                session.expires_in,
                on_complete,
                user_code=session.user_code,
            ),
            name=f"device-flow-poll:{account.provider_id}:{account.local_connection_id}",
        )
        self._active_flows[flow_key] = task
        self._poll_tasks.add(task)

        def settled(completed: asyncio.Task[None]) -> None:
            self._poll_tasks.discard(completed)
            if self._active_flows.get(flow_key) is completed:
                self._active_flows.pop(flow_key, None)
            # Also runs when cancellation preceded the coroutine's first step.
            self._minimax_code_verifiers.pop((flow_key, session.user_code), None)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    _LOGGER.error(
                        "OAuth polling task failed (provider=%s connection=%s): %s",
                        account.provider_id,
                        account.local_connection_id,
                        type(error).__name__,
                    )

        task.add_done_callback(settled)
        return session

    def is_flow_active(
        self,
        account: Account,
    ) -> bool:
        """Whether this Account has pending authorization or unfinished polling."""
        key = account
        task = self._active_flows.get(key)
        authorization = self._active_authorizations.get(key)
        return (task is not None and not task.done()) or authorization is not None

    async def _request_device_session(
        self,
        account: Account,
        oauth_config: OAuthConfig,
    ) -> DeviceFlowSession:
        """Request a Device Flow session from the provider."""

        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS,
            verify=shared_ssl_context(),
        ) as client:
            if _wire._is_minimax_flow(oauth_config):
                code_verifier, code_challenge, state = _wire._minimax_pkce_pair()
                response = await retry_async(
                    _wire._post_minimax_device_authorization,
                    client,
                    oauth_config,
                    code_challenge,
                    state,
                )
                session = _wire._minimax_device_session_from_response(
                    response.json(), expected_state=state
                )
                verifier_key = (account, session.user_code)
                self._minimax_code_verifiers[verifier_key] = code_verifier
            else:
                response = await retry_async(
                    _wire._post_device_authorization,
                    client,
                    oauth_config,
                )
                session = _wire._device_session_from_response(oauth_config, response.json())
        _LOGGER.debug(
            "Started OAuth device flow (provider=%s connection=%s)",
            account.provider_id,
            account.local_connection_id,
        )
        return session

    async def _poll_for_token(
        self,
        account: Account,
        oauth_config: OAuthConfig,
        device_code: str,
        interval: int,
        expires_in: int,
        on_complete: OnCompleteCallback,
        user_code: str = "",
    ) -> None:
        """Poll for Device Flow completion, store the token, and notify the caller."""

        try:
            await self._poll_until_complete(
                account, oauth_config, device_code, interval, expires_in, user_code
            )
        except asyncio.CancelledError:
            raise
        except DeviceFlowTerminalError as error:
            _LOGGER.warning(
                "OAuth device flow failed (provider=%s connection=%s): %s",
                account.provider_id,
                account.local_connection_id,
                error,
            )
            await self._notify_complete(on_complete, success=False)
        except ProviderError as error:
            _LOGGER.warning(
                "OAuth device flow failed (provider=%s connection=%s): %s",
                account.provider_id,
                account.local_connection_id,
                error.__class__.__name__,
            )
            await self._notify_complete(on_complete, success=False)
        except Exception:
            _LOGGER.error(
                "OAuth device flow crashed (provider=%s connection=%s)",
                account.provider_id,
                account.local_connection_id,
            )
            await self._notify_complete(on_complete, success=False)
            raise
        else:
            _LOGGER.info(
                "OAuth provider connected (provider=%s connection=%s)",
                account.provider_id,
                account.local_connection_id,
            )
            await self._notify_complete(on_complete, success=True)
        finally:
            self._minimax_code_verifiers.pop(
                (account, user_code),
                None,
            )

    def _flow_lock(self, key: Account) -> asyncio.Lock:
        return self._flow_locks.setdefault(key, asyncio.Lock())

    async def cancel_flow_and_wait(
        self,
        account: Account,
    ) -> None:
        """Drain any admitted token save before disconnect removes its credential."""
        async with self._flow_lock(account):
            await self._cancel_flow_and_wait(account)

    async def _cancel_flow_and_wait(
        self,
        account: Account,
    ) -> None:
        key = account
        tasks = [
            task
            for task in (self._active_authorizations.get(key), self._active_flows.get(key))
            if task is not None
        ]
        self.cancel_flow(account)
        epoch = self._flow_epochs[key]
        await cancel_and_wait(*tasks)
        if self._flow_epochs.get(key) != epoch:
            raise asyncio.CancelledError

    def cancel_flow(
        self,
        account: Account,
    ) -> None:
        """Cancel authorization and polling for the provider connection account."""
        key = account
        self._flow_epochs[key] = self._flow_epochs.get(key, 0) + 1

        authorization = self._active_authorizations.pop(account, None)
        if authorization is not None and not authorization.done():
            authorization.cancel()
        task = self._active_flows.pop(account, None)
        if task is not None and not task.done():
            task.cancel()
        self._drop_minimax_verifiers(account)

    async def aclose(self) -> None:
        """Close the engine and drain authorization and polling tasks."""
        self._closed = True
        tasks = [*self._poll_tasks, *self._authorization_tasks]
        self._active_authorizations.clear()
        self._active_flows.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

        pending_tasks = [task for task in tasks if not task.done()]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._minimax_code_verifiers.clear()

    async def _poll_until_complete(
        self,
        account: Account,
        oauth_config: OAuthConfig,
        device_code: str,
        interval: int,
        expires_in: int,
        user_code: str = "",
    ) -> None:
        poll_interval = interval
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        code_verifier = ""
        if _wire._is_minimax_flow(oauth_config):
            code_verifier = self._minimax_code_verifiers.get(
                (account, user_code),
                "",
            )
            if not code_verifier:
                raise DeviceFlowTerminalError("missing_pkce_verifier")
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS,
            verify=shared_ssl_context(),
        ) as client:
            while True:
                if datetime.now(UTC) >= expires_at:
                    raise DeviceFlowTerminalError(EXPIRED_TOKEN_ERROR)

                data = await _wire._request_device_token(
                    client,
                    oauth_config,
                    device_code,
                    user_code,
                    code_verifier,
                )
                if self._is_pending_response(data):
                    poll_interval = self._next_interval(data, poll_interval)
                    await _sleep(self._bounded_poll_sleep(poll_interval, expires_at))
                    continue

                if self._is_terminal_error(data):
                    raise DeviceFlowTerminalError(data["error"])

                token = await _wire._build_token(client, oauth_config, data)
                await self._token_store.save_async(account, token)
                return

    def _drop_minimax_verifiers(
        self,
        account: Account,
    ) -> None:
        for verifier_key in [key for key in self._minimax_code_verifiers if key[0] == account]:
            self._minimax_code_verifiers.pop(verifier_key, None)

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

    async def _notify_complete(self, on_complete: OnCompleteCallback, *, success: bool) -> None:
        result = on_complete(success=success)
        if inspect.isawaitable(result):
            await result
