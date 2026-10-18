"""One call's live Home Assistant connection, safe retries and state reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.tools.tools import JsonObject
from core.utils.http_status import is_retryable_status
from core.utils.retry import sleep_for_retry
from core.utils.tls import shared_ssl_context

from ._listing import states

_RETRY_MAX_RETRIES = 2
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_REFUSED_STATUSES = frozenset({429, 503})


@dataclass(frozen=True)
class _Failure:
    """A Home Assistant request that returned no usable payload."""

    status: int | None = None  # HTTP status; None for transport and format problems
    detail: str = ""
    retryable: bool = False
    attempts: int = 1
    sent: bool = True  # whether the request may have reached Home Assistant
    invalid_json: bool = False


def _extract_error_detail(response: httpx.Response) -> str:
    """Extract a human-readable error message from an HA HTTP response."""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    # Home Assistant answers some refusals with a bare "400: Bad Request" body.
    fallback = response.text.strip().removeprefix(f"{response.status_code}: ")
    if fallback:
        return fallback[:300]
    return response.reason_phrase or "request failed"


def _error_text(error: Exception) -> str:
    text = str(error).strip()
    return (f"{type(error).__name__}: {text}" if text else type(error).__name__)[:200]


def _status_retryable(status: int, idempotent: bool) -> bool:
    if idempotent:
        return is_retryable_status(status, idempotent=True)
    return status in _REFUSED_STATUSES


class HomeAssistantClient:
    """Resolved afresh for each Tool call; never retained across configuration changes."""

    def __init__(self, url: str, token: str, logger: Any) -> None:
        self.url = url
        self.token = token
        self.logger = logger

    async def request(
        self,
        method: str,
        path: str,
        json_body: JsonObject | None = None,
    ) -> tuple[Any, _Failure | None]:
        """Make one Home Assistant REST request, retrying only what is safe to repeat.

        A GET repeats on transport errors and transient statuses. A POST service
        call repeats only when Home Assistant cannot have run it: the connection
        was never made, or the status refuses the call (429, 503). The token is
        never logged.

        Returns:
            Tuple of (payload, failure); exactly one is None.
        """
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        idempotent = method == "GET"

        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT, verify=shared_ssl_context()
        ) as client:
            for attempt in range(_RETRY_MAX_RETRIES + 1):
                try:
                    if idempotent:
                        response = await client.get(self.url + path, headers=headers)
                    else:
                        response = await client.post(
                            self.url + path, headers=headers, json=json_body
                        )
                except httpx.RequestError as error:
                    sent = not isinstance(error, _UNSENT_ERRORS)
                    repeatable = idempotent or not sent
                    if repeatable and attempt < _RETRY_MAX_RETRIES:
                        await sleep_for_retry(attempt)
                        continue
                    self.logger.warning("Home Assistant request failed: %s", _error_text(error))
                    return None, _Failure(
                        detail=_error_text(error),
                        retryable=repeatable,
                        attempts=attempt + 1,
                        sent=sent,
                    )

                if response.status_code >= 400:
                    retryable = _status_retryable(response.status_code, idempotent)
                    if retryable and attempt < _RETRY_MAX_RETRIES:
                        await sleep_for_retry(attempt)
                        continue
                    detail = _extract_error_detail(response)
                    self.logger.warning(
                        "Home Assistant request failed: HTTP %s: %s",
                        response.status_code,
                        detail,
                    )
                    return None, _Failure(
                        status=response.status_code,
                        detail=detail,
                        retryable=retryable,
                        attempts=attempt + 1,
                    )

                try:
                    return response.json(), None
                except ValueError:
                    return None, _Failure(detail="not JSON", invalid_json=True)

        return None, _Failure(detail="request failed")

    async def all_states(self) -> list[dict[str, Any]]:
        """Return every entity state for candidate lists; empty when the read fails."""
        payload, failure = await self.request("GET", "/api/states")
        return [] if failure is not None else states(payload)

    async def post_service(
        self,
        path: str,
        body: JsonObject,
        requested: bool,
    ) -> tuple[Any, _Failure | None, str | None]:
        """POST a service call; returns (payload, failure, response-request problem)."""
        payload, failure = await self.request(
            "POST", path + ("?return_response" if requested else ""), json_body=body
        )
        if failure is None or failure.status != 400 or "return_response" not in failure.detail:
            return payload, failure, None
        if requested:
            return None, failure, "unsupported"
        # Home Assistant refuses a data-only service before running it unless the
        # call asks for its data, so asking now runs the call exactly once.
        payload, failure = await self.request("POST", path + "?return_response", json_body=body)
        return payload, failure, None
