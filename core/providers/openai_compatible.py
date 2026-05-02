"""OpenAI-compatible provider adapter.

Handles the ``/chat/completions`` endpoint format used by OpenAI, OpenRouter,
Groq, Together, and other providers that follow the OpenAI API convention.
Differences in base URL, auth headers, and default parameters are expressed
through ``ProviderConfig`` — no subclassing needed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.providers.adapter import ProviderAdapter
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.providers import ProviderConfig
from core.utils.retry import retry_async

# ---------------------------------------------------------------------------
# HTTP status constants
# ---------------------------------------------------------------------------

HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_RATE_LIMITED = 429
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503

RETRYABLE_STATUS_CODES = {HTTP_RATE_LIMITED, HTTP_BAD_GATEWAY, HTTP_SERVICE_UNAVAILABLE}
AUTH_ERROR_STATUS_CODES = {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN}

# ---------------------------------------------------------------------------
# SSE parsing constants
# ---------------------------------------------------------------------------

SSE_DATA_PREFIX = "data: "
SSE_DONE_MARKER = "[DONE]"
CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"


class OpenAICompatibleAdapter(ProviderAdapter):
    """Adapter for OpenAI-compatible API providers.

    Uses the ``/chat/completions`` endpoint with the standard OpenAI request
    and response format.  Provider-specific differences (base URL, auth header,
    extra headers, default parameters) come from ``ProviderConfig`` — no
    subclassing required.

    Args:
        config: Immutable provider configuration.
        api_key: API key for authentication.
    """

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=60.0,
        )

    # ------------------------------------------------------------------
    # Header / payload helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Build request headers from provider config auth and extra_headers."""
        headers: dict[str, str] = {
            self._config.auth.header: f"{self._config.auth.prefix}{self._api_key}",
        }
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        return headers

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the request payload with model, messages, defaults, and overrides."""
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
        }
        # Apply provider defaults (lower priority — caller kwargs win)
        if self._config.defaults:
            for key, value in self._config.defaults.items():
                payload.setdefault(key, value)
        # Apply caller overrides (highest priority)
        payload.update(kwargs)
        return payload

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_status(status_code: int, reason: str = "") -> None:
        """Classify an HTTP status code and raise the appropriate provider error.

        If *status_code* indicates success (< 400) the function returns
        silently.  Otherwise it raises the correct subclass of
        ``ProviderError`` with the ``retryable`` flag set appropriately.

        Args:
            status_code: HTTP response status code.
            reason: Optional reason phrase or response body for context.

        Raises:
            ProviderAuthError: 401 / 403 (not retryable).
            ProviderRateLimitError: 429 (retryable).
            ProviderError: Other 4xx/5xx (retryable only for 502/503).
        """
        detail = f"{status_code} {reason}".strip() if reason else str(status_code)

        if status_code in AUTH_ERROR_STATUS_CODES:
            raise ProviderAuthError(f"Authentication error: {detail}")
        if status_code == HTTP_RATE_LIMITED:
            raise ProviderRateLimitError(f"Rate limited: {detail}")
        if status_code >= 400:
            retryable = status_code in RETRYABLE_STATUS_CODES
            raise ProviderError(f"Provider error: {detail}", retryable=retryable)

    # ------------------------------------------------------------------
    # Network error wrapping
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_network_error(exc: Exception) -> ProviderTimeoutError:
        """Wrap a network-level exception in ``ProviderTimeoutError``."""
        return ProviderTimeoutError(f"Request failed: {exc}")

    # ------------------------------------------------------------------
    # send() — non-streaming
    # ------------------------------------------------------------------

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a non-streaming chat completion request.

        Retries on retryable errors (429, 502, 503) via ``retry_async``.
        Fails immediately on auth errors (401/403).

        Args:
            messages: Conversation messages in OpenAI format.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (temperature, max_tokens, …).

        Returns:
            Parsed response dict from the provider.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            ProviderTimeoutError: Connection / timeout errors (retried, then raised).
            ProviderError: Other HTTP errors.
        """

        async def _do_request() -> dict[str, Any]:
            headers = self._build_headers()
            payload = self._build_payload(messages, model_id, **kwargs)
            try:
                response = await self._client.post(
                    CHAT_COMPLETIONS_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                raise self._wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
                raise self._wrap_network_error(exc) from exc

            self._classify_status(response.status_code, response.text)
            return dict(response.json())

        return await retry_async(_do_request)

    # ------------------------------------------------------------------
    # stream() — SSE streaming
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming chat completion request and yield SSE chunks.

        Retries the initial connection on retryable errors (429, 502, 503).
        Once the stream is established, yields parsed SSE data chunks as
        dicts until the ``[DONE]`` marker is received.

        Args:
            messages: Conversation messages in OpenAI format.
            model_id: Exact model identifier sent to the API.
            **kwargs: Additional parameters (temperature, max_tokens, …).

        Yields:
            Parsed response chunk dicts from the SSE event stream.

        Raises:
            ProviderAuthError: 401 / 403 responses.
            ProviderRateLimitError: 429 responses (retried, then raised).
            ProviderTimeoutError: Connection / timeout errors (retried, then raised).
            ProviderError: Other HTTP errors.
        """
        headers = self._build_headers()
        payload = self._build_payload(messages, model_id, **kwargs)
        payload["stream"] = True

        async def _connect_stream() -> httpx.Response:
            request = self._client.build_request(
                "POST",
                CHAT_COMPLETIONS_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise self._wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
                raise self._wrap_network_error(exc) from exc

            # If the status indicates an error, read and close the response
            # before classifying — this frees the connection for retry.
            if response.status_code >= 400:
                error_body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                self._classify_status(response.status_code, error_body)
                # _classify_status always raises for >= 400; this is unreachable
                # but satisfies type checkers.
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)

            return response

        response = await retry_async(_connect_stream)

        try:
            async for line in response.aiter_lines():
                if not line.startswith(SSE_DATA_PREFIX):
                    continue
                data = line[len(SSE_DATA_PREFIX) :]
                if data.strip() == SSE_DONE_MARKER:
                    break
                yield json.loads(data)
        finally:
            await response.aclose()
