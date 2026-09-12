"""Internal Web Search bounded HTTP transport and failure policy."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal

import httpx

from core.tools._web_search_common import (
    _normalize_text,
)
from core.utils.http_status import HttpRequestFailure, is_retryable_status, parse_retry_after
from core.utils.logging import get_logger
from core.utils.retry import MAX_RETRIES, sleep_for_retry

_LOGGER = get_logger("tools.web_search")


_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


_MAX_RESPONSE_SIZE_LABEL = "5 MB"


_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class _ResponseTooLargeError(Exception):
    """Raised before a search response can exceed its in-memory limit."""


def _declared_response_size(headers: Mapping[str, str]) -> int | None:
    """Return a valid declared body size, when the provider sent one."""
    value = headers.get("content-length")
    if value is None:
        return None
    try:
        size = int(value)
    except ValueError:
        return None
    return size if size >= 0 else None


async def _read_bounded_response(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST"],
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Read one search response without exceeding the in-memory body limit."""
    async with client.stream(
        method,
        url,
        params=params,
        json=dict(payload) if payload is not None else None,
        headers=headers,
    ) as response:
        declared_size = _declared_response_size(response.headers)
        if declared_size is not None and declared_size > _MAX_RESPONSE_BYTES:
            raise _ResponseTooLargeError(
                f"provider response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit"
            )

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise _ResponseTooLargeError(
                    f"provider response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit"
                )
            body.extend(chunk)

        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )


async def _request_bounded(
    method: Literal["GET", "POST"],
    url: str,
    *,
    provider_label: str,
    params: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    status_hints: Mapping[int, str] | None = None,
    extra_retryable_statuses: Collection[int] | None = None,
) -> tuple[httpx.Response | None, HttpRequestFailure | None]:
    """Own bounded search requests, retry timing, and terminal HTTP failures.

    GET retries include 500. Search POSTs are billed per attempt, so use the
    narrower transient set (429/502/503/504) plus explicit vendor exceptions.
    """
    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await _read_bounded_response(
                    client,
                    method,
                    url,
                    params=params,
                    payload=payload,
                    headers=headers,
                )
            except httpx.RequestError as error:
                if attempt >= MAX_RETRIES:
                    _LOGGER.warning("%s web search request failed: %s", provider_label, error)
                    return None, HttpRequestFailure(
                        f"request failed: {error}",
                        retryable=True,
                        attempts_made=MAX_RETRIES + 1,
                    )
                await sleep_for_retry(attempt)
                continue

            if response.status_code >= 400:
                retryable = is_retryable_status(
                    response.status_code,
                    idempotent=method == "GET",
                    extra=extra_retryable_statuses,
                )
                if retryable and attempt < MAX_RETRIES:
                    await sleep_for_retry(attempt, parse_retry_after(response.headers))
                    continue
                detail = _extract_error_detail(response)
                if hint := (status_hints or {}).get(response.status_code):
                    detail = f"{detail}; {hint}"
                _LOGGER.warning(
                    "%s web search request failed: HTTP %s: %s",
                    provider_label,
                    response.status_code,
                    detail,
                )
                return None, HttpRequestFailure(
                    f"HTTP {response.status_code}: {detail}",
                    retryable=retryable,
                    attempts_made=(MAX_RETRIES + 1) if retryable else None,
                )

            return response, None

    return None, HttpRequestFailure("request failed")


async def _request_json(
    method: Literal["GET", "POST"],
    url: str,
    *,
    provider_label: str,
    params: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    status_hints: Mapping[int, str] | None = None,
    extra_retryable_statuses: Collection[int] | None = None,
) -> tuple[Any | None, HttpRequestFailure | None]:
    """Decode JSON only after bounded transport and HTTP failure handling."""
    response, failure = await _request_bounded(
        method,
        url,
        provider_label=provider_label,
        params=params,
        payload=payload,
        headers=headers,
        status_hints=status_hints,
        extra_retryable_statuses=extra_retryable_statuses,
    )
    if failure is not None or response is None:
        return None, failure
    try:
        return response.json(), None
    except ValueError:
        return None, HttpRequestFailure("provider returned invalid JSON")


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            message = _normalize_text(detail.get("error", detail.get("message")))
            if message:
                return message

        message = _normalize_text(payload.get("error", payload.get("message")))
        if message:
            return message

    fallback = _normalize_text(response.text)
    if fallback:
        return fallback[:300]
    return response.reason_phrase or "request failed"
