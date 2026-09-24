"""Opt-in extraction services; one bounded, billable request to one vendor."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from core.utils.tls import shared_ssl_context

_MAX_BYTES = 12 * 1024 * 1024
_DATA_URI = re.compile(
    r"data:(?:[a-z0-9.+-]+/[a-z0-9.+-]+)?(?:;[a-z0-9=.+-]+)*,[^\s\)\]\"'<>]+", re.I
)


class FetchServiceError(Exception):
    """A service failed without a usable page; never expose credential-bearing bodies."""


def clean_service_text(text: str, *, include_links: bool = True) -> str:
    """Embedded bytes are not readable page content and can consume huge Context."""
    text = _DATA_URI.sub("[embedded data omitted]", text)
    if not include_links:
        text = re.sub(r"!?\[([^\]]*)\]\(https?://[^\s)]*\)", r"\1", text)
    return text


def _request(
    provider: str, key: str, url: str, output: str
) -> tuple[str, dict[str, str], dict[str, Any]]:
    bearer = {"Authorization": f"Bearer {key}"}
    if provider == "firecrawl":
        return (
            "https://api.firecrawl.dev/v2/scrape",
            bearer,
            {
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": False,
                "timeout": 45000,
                "maxAge": 0,
            },
        )
    if provider == "tavily":
        return (
            "https://api.tavily.com/extract",
            bearer,
            {
                "urls": [url],
                "extract_depth": "advanced",
                "include_images": True,
                "format": "text" if output == "text" else "markdown",
                "timeout": 45,
            },
        )
    if provider == "exa":
        return (
            "https://api.exa.ai/contents",
            {"x-api-key": key},
            {
                "ids": [url],
                "text": True,
                "maxAgeHours": 0,
            },
        )
    if provider == "parallel":
        return (
            "https://api.parallel.ai/v1/extract",
            {"x-api-key": key},
            {
                "urls": [url],
                "advanced_settings": {
                    "full_content": True,
                    "fetch_policy": {
                        "max_age_seconds": 600,
                        "timeout_seconds": 45,
                        "disable_cache_fallback": True,
                    },
                },
            },
        )
    raise FetchServiceError("Unsupported extraction service. Check Web Fetch settings.")


async def _post(
    endpoint: str, headers: dict[str, str], payload: dict[str, Any], provider: str
) -> dict[str, Any]:
    # A timeout after sending a billable POST is ambiguous. Do not silently
    # retry it (or send it to a second vendor) and risk duplicate charges.
    try:
        async with (
            asyncio.timeout(65),
            httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=5),
                follow_redirects=False,
                verify=shared_ssl_context(),
            ) as client,
            client.stream("POST", endpoint, headers=headers, json=payload) as response,
        ):
            if not 200 <= response.status_code < 300:
                status = response.status_code
                hint = (
                    "Check the API key."
                    if status in {401, 403}
                    else "Check the service quota and account."
                    if status in {402, 429}
                    else "Try another source or try later."
                )
                raise FetchServiceError(f"{provider} returned HTTP {status}. {hint}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_BYTES:
                    raise FetchServiceError(f"{provider} response exceeds the 12 MB limit.")
                body.extend(chunk)
            result = json.loads(body)
    except (httpx.RequestError, TimeoutError) as error:
        raise FetchServiceError(
            f"{provider} request failed or timed out. It was not automatically repeated; "
            "a retry may be billed again."
        ) from error
    except ValueError as error:
        raise FetchServiceError(f"{provider} returned invalid JSON.") from error
    if not isinstance(result, dict):
        raise FetchServiceError(f"{provider} returned an invalid response.")
    return result


async def fetch_service(provider: str, key: str, url: str, output: str) -> dict[str, Any]:
    endpoint, headers, payload = _request(provider, key, url, output)
    result = await _post(endpoint, headers, payload, provider)
    if provider == "firecrawl":
        row = result.get("data")
        if result.get("success") is False or not isinstance(row, dict):
            raise FetchServiceError("firecrawl could not extract the page.")
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = metadata.get("statusCode", 200)
        if isinstance(status, int) and status >= 400:
            raise FetchServiceError(f"firecrawl received HTTP {status} from the page.")
        content = row.get("markdown")
        final_url = metadata.get("sourceURL", url)
        title = metadata.get("title", "")
    else:
        rows = result.get("results")
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise FetchServiceError(f"{provider} could not extract the page.")
        field = {"tavily": "raw_content", "exa": "text", "parallel": "full_content"}[provider]
        content = row.get(field)
        final_url = row.get("url", url)
        title = row.get("title", "")
    if not isinstance(content, str) or not content.strip():
        raise FetchServiceError(
            f"{provider} returned no readable page content. Try another source."
        )
    content = clean_service_text(content, include_links=output != "text")
    return {
        "content": content,
        "url": final_url if isinstance(final_url, str) else url,
        "title": title[:500] if isinstance(title, str) else "",
        "source": provider,
        "warnings": [
            (
                "Saved content covers only the service's extraction; inaccessible "
                "or interactive sections may be missing."
            )
        ],
    }
