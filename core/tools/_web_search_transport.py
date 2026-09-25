"""Internal Web Search bounded HTTP transport and failure policy."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any, Literal

import httpx
from bs4 import BeautifulSoup

from core.tools._web_search_common import (
    _normalize_text,
)
from core.utils.http_status import HttpRequestFailure, is_retryable_status, parse_retry_after
from core.utils.logging import get_logger
from core.utils.retry import MAX_RETRIES, sleep_for_retry
from core.utils.tls import shared_ssl_context

_LOGGER = get_logger("tools.web_search")


_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


_MAX_RESPONSE_SIZE_LABEL = "5 MB"


_MAX_DETAIL_CHARS = 200


# Statuses providers use when the account's plan or credits are used up
# (Tavily answers 432/433 for plan and pay-as-you-go limits).
_QUOTA_STATUSES = frozenset({402, 432, 433})


# Brave, for one, answers an invalid key with HTTP 422 naming the token.
_KEY_PROBLEM = re.compile(r"api[ _-]?key|\btoken\b|unauthori[sz]ed", re.IGNORECASE)


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
            raise _ResponseTooLargeError(_too_large_message())

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise _ResponseTooLargeError(_too_large_message())
            body.extend(chunk)

        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )


def _too_large_message() -> str:
    return (
        f"The search provider's response exceeds the {_MAX_RESPONSE_SIZE_LABEL} limit. "
        "Try again with a lower count."
    )


def _status_message(
    provider_label: str,
    status: int,
    detail: str,
    *,
    retryable: bool,
    credential_key: str | None,
    hint: str | None,
) -> str:
    """Say what the provider's HTTP status means for the Agent and what to do next."""
    answer = f"HTTP {status}: {detail}"
    if hint:
        return f"{provider_label} refused the request ({answer}). {hint}"
    if status in _QUOTA_STATUSES:
        return (
            f"{provider_label} refused the search ({answer}); the account's plan or credits "
            "may be used up. Tell the user."
        )
    if credential_key and (
        status in {401, 403} or (status < 500 and _KEY_PROBLEM.search(detail) is not None)
    ):
        return (
            f"{provider_label} rejected the API key ({answer}). Tell the user to check "
            f"{credential_key} in the .env file of the vBot data directory."
        )
    if status == 429:
        return f"{provider_label} is limiting requests ({answer}). Wait before searching again."
    if status >= 500:
        again = " after several attempts" if retryable else ""
        return f"{provider_label} failed to answer{again} ({answer}). Try again later."
    return f"{provider_label} rejected the search ({answer})."


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
    credential_key: str | None = None,
    unreachable_hint: str | None = None,
) -> tuple[httpx.Response | None, HttpRequestFailure | None]:
    """Own bounded search requests, retry timing, and terminal HTTP failures.

    GET retries include 500. Search POSTs are billed per attempt, so use the
    narrower transient set (429/502/503/504) plus explicit vendor exceptions.
    Failure messages name the provider, what its answer means, and the next
    step: ``credential_key`` names the key to check on 401/403, a
    ``status_hints`` entry replaces the guidance for its status, and
    ``unreachable_hint`` replaces the guidance when no connection succeeds.
    """
    async with httpx.AsyncClient(
        headers=_BROWSER_HEADERS,
        timeout=_REQUEST_TIMEOUT,
        verify=shared_ssl_context(),
    ) as client:
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
                    reason = str(error) or type(error).__name__
                    guidance = unreachable_hint or (
                        "The network or the service may be down; try again later."
                    )
                    return None, HttpRequestFailure(
                        f"Could not reach {provider_label} ({reason}). {guidance}",
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
                _LOGGER.warning(
                    "%s web search request failed: HTTP %s: %s",
                    provider_label,
                    response.status_code,
                    detail,
                )
                message = _status_message(
                    provider_label,
                    response.status_code,
                    detail,
                    retryable=retryable,
                    credential_key=credential_key,
                    hint=(status_hints or {}).get(response.status_code),
                )
                return None, HttpRequestFailure(
                    message,
                    retryable=retryable,
                    attempts_made=(MAX_RETRIES + 1) if retryable else None,
                )

            return response, None

    return None, HttpRequestFailure(f"Could not reach {provider_label}.")


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
    credential_key: str | None = None,
    unreachable_hint: str | None = None,
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
        credential_key=credential_key,
        unreachable_hint=unreachable_hint,
    )
    if failure is not None or response is None:
        return None, failure
    try:
        return response.json(), None
    except ValueError:
        return None, HttpRequestFailure(
            f"{provider_label} answered with something other than search results "
            "(invalid JSON). Try again later."
        )


def _error_message(value: Any) -> str:
    """Read a provider error field: text, or an object with detail/message parts."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    message = _normalize_text(value.get("detail")) or _normalize_text(value.get("message"))
    if not message:
        message = _error_message(value.get("error"))
    meta = value.get("meta")
    problems = meta.get("errors") if isinstance(meta, dict) else None
    if isinstance(problems, list):
        # Brave lists each rejected parameter as {"loc": [...], "msg": "..."}.
        for problem in problems[:3]:
            if isinstance(problem, dict) and isinstance(problem.get("msg"), str):
                location = problem.get("loc")
                where = (
                    ".".join(str(part) for part in location) if isinstance(location, list) else ""
                )
                message += f"; {where}: {problem['msg']}" if where else f"; {problem['msg']}"
    return message


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            message = _error_message(payload.get(key))
            if message:
                return message[:_MAX_DETAIL_CHARS]

    text = response.text
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = title or soup.get_text(" ", strip=True)
    fallback = " ".join(text.split())
    if fallback:
        return fallback[:_MAX_DETAIL_CHARS]
    return response.reason_phrase or "no details"
