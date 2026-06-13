"""Built-in web_fetch tool for fetching URLs and extracting readable content."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString, PageElement

from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.http_status import is_retryable_status
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.web_fetch")

_MAX_URL_BYTES = 100 * 1024
_RESPONSE_TRUNCATED_MARKER = "\n\n[... response truncated ...]"
_CONTENT_TRUNCATED_MARKER = "\n\n[... content truncated ...]"

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

_STRIP_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "object",
        "embed",
        "canvas",
        "map",
        "source",
        "template",
    }
)

_JUNK_PATTERNS: re.Pattern[str] = re.compile(
    r"cookie[-_]?(?:bar|banner|consent|notice|popup|overlay)"
    r"|gdpr|consent[-_]?(?:bar|banner|modal)"
    r"|ad[-_]?(?:banner|slot|wrapper|container|unit)"
    r"|popup[-_]?overlay|modal[-_]?backdrop"
    r"|newsletter[-_]?(?:signup|popup|modal)",
    re.IGNORECASE,
)

_MULTI_SPACE: re.Pattern[str] = re.compile(r"[ \t]+")
_MULTI_NEWLINE: re.Pattern[str] = re.compile(r"\n{3,}")

_BLOCK_NAMES: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "section",
        "blockquote",
        "figcaption",
        "dt",
        "dd",
        "header",
        "footer",
        "nav",
        "aside",
        "main",
        "article",
        "figure",
    }
)

_HEADING_NAMES: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

_CHILD_BLOCK_NAMES: frozenset[str] = (
    _BLOCK_NAMES | _HEADING_NAMES | frozenset({"ul", "ol", "table", "pre"})
)

_SELF_RENDERED_TAGS: frozenset[str] = _HEADING_NAMES | frozenset({"a", "img", "pre", "tr", "li"})

_RETRY_MAX_RETRIES = 3
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_BACKOFF_FACTOR = 2
_RETRY_JITTER_FACTOR = 0.5
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_MAX_REDIRECTS = 10
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

WEB_FETCH_TOOL_NAME = "web_fetch"
WEB_FETCH_TOOL_DESCRIPTION = (
    "Fetch a public HTTP or HTTPS URL and return the page content as clean, readable text."
)
WEB_FETCH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "HTTP or HTTPS URL to fetch.",
        },
        "include_links": {
            "type": "boolean",
            "description": (
                "Preserve hyperlinks as [text](url) in the output. "
                "Set to false when URLs are not needed."
            ),
            "default": True,
        },
        "raw": {
            "type": "boolean",
            "description": ("Return the unmodified HTTP response body instead of cleaned text."),
            "default": False,
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}


def _make_client() -> httpx.AsyncClient:
    """Create an AsyncClient with browser-like headers and manual redirects."""
    return httpx.AsyncClient(
        headers=_BROWSER_HEADERS,
        follow_redirects=False,
        timeout=_REQUEST_TIMEOUT,
    )


def _attr_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(part) for part in value)
    return str(value)


def _is_live(node: object) -> bool:
    """Return True if a tag has not been decomposed or detached."""
    return isinstance(node, Tag) and node.attrs is not None


def _has_junk_attr(node: object) -> bool:
    """Return True if a tag class/id matches known junk patterns."""
    if not isinstance(node, Tag):
        return False

    for attr in ("class", "id"):
        text = _attr_to_text(node.get(attr)).strip()
        if not text:
            continue
        if _JUNK_PATTERNS.search(text):
            return True
    return False


def _strip_noise(root: BeautifulSoup | Tag) -> None:
    """Remove non-text elements from the soup tree in-place."""
    for comment in list(root.find_all(string=lambda t: isinstance(t, Comment))):
        comment.extract()

    for tag_name in _STRIP_TAGS:
        for tag in list(root.find_all(tag_name)):
            if _is_live(tag):
                tag.decompose()

    for tag in list(root.find_all(attrs={"hidden": True})):
        if _is_live(tag):
            tag.decompose()
    for tag in list(root.find_all(attrs={"aria-hidden": "true"})):
        if _is_live(tag):
            tag.decompose()

    junk = [node for node in root.find_all(True) if _is_live(node) and _has_junk_attr(node)]
    for tag in junk:
        if _is_live(tag):
            tag.decompose()


def _heading_prefix(tag_name: str) -> str:
    """Map h1-h6 to markdown-style heading prefixes."""
    if tag_name and tag_name[0] == "h" and len(tag_name) == 2 and tag_name[1].isdigit():
        level = int(tag_name[1])
        if 1 <= level <= 6:
            return "#" * level + " "
    return ""


def _render_inline(node: Tag, base_url: str, include_links: bool) -> str:
    """Render inline content preserving links, code, images, and breaks."""
    parts: list[str] = []

    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child).replace("\r", "").replace("\n", " ")
            text = _MULTI_SPACE.sub(" ", text)
            parts.append(text)
            continue

        if not isinstance(child, Tag):
            continue

        child_name = child.name

        if child_name == "a":
            inner = _render_inline(child, base_url, include_links).strip()
            if not include_links:
                if inner:
                    parts.append(inner)
                continue
            href = _attr_to_text(child.get("href", "")).strip()
            if href and not href.startswith(("#", "javascript:")):
                absolute = urljoin(base_url, href)
                if inner and inner != absolute:
                    parts.append(f"[{inner}]({absolute})")
                elif inner:
                    parts.append(inner)
                else:
                    parts.append(absolute)
            elif inner:
                parts.append(inner)
            continue

        if child_name == "code":
            if child.parent and child.parent.name == "pre":
                continue
            inner = child.get_text().strip()
            if inner:
                parts.append(f"`{inner}`")
            continue

        if child_name == "img":
            alt = _attr_to_text(child.get("alt", "")).strip()
            src = _attr_to_text(child.get("src", "")).strip()
            if alt and (src or not include_links):
                if include_links and src:
                    parts.append(f"![{alt}]({urljoin(base_url, src)})")
                else:
                    parts.append(alt)
            continue

        if child_name == "br":
            parts.append("\n")
            continue

        if child_name in _STRIP_TAGS:
            continue

        parts.append(_render_inline(child, base_url, include_links))

    result = "".join(parts)
    result = _MULTI_SPACE.sub(" ", result)
    return result.strip()


def _render_inline_oneline(node: Tag, base_url: str, include_links: bool) -> str:
    """Render inline content on one line by collapsing line breaks."""
    text = _render_inline(node, base_url, include_links).replace("\n", " ")
    return _MULTI_SPACE.sub(" ", text).strip()


def _render_tag(tag: Tag, base_url: str, include_links: bool) -> str:
    """Render a single tag to plain text with lightweight markdown hints."""
    name = tag.name

    if name in _HEADING_NAMES:
        text = _render_inline_oneline(tag, base_url, include_links)
        return f"\n\n{_heading_prefix(name)}{text}\n" if text else ""

    if name == "a":
        inner = _render_inline(tag, base_url, include_links)
        if not include_links:
            return inner or ""
        href = _attr_to_text(tag.get("href", "")).strip()
        if href and not href.startswith(("#", "javascript:")):
            absolute = urljoin(base_url, href)
            if inner and inner != absolute:
                return f"[{inner}]({absolute})"
            if inner:
                return inner
            return absolute
        return inner or ""

    if name == "img":
        alt = _attr_to_text(tag.get("alt", "")).strip()
        src = _attr_to_text(tag.get("src", "")).strip()
        if alt and include_links and src:
            return f"![{alt}]({urljoin(base_url, src)})"
        if alt:
            return alt
        return ""

    if name == "pre":
        code = tag.get_text()
        return f"\n\n```\n{code.strip()}\n```\n" if code.strip() else ""

    if name == "code":
        if tag.parent and tag.parent.name == "pre":
            return ""
        text = tag.get_text()
        return f"`{text.strip()}`" if text.strip() else ""

    if name == "li":
        text = _render_inline_oneline(tag, base_url, include_links)
        return f"\n- {text}" if text else ""

    if name == "tr":
        cells = tag.find_all(["td", "th"], recursive=False)
        rendered = [
            _render_inline_oneline(cell, base_url, include_links)
            for cell in cells
            if isinstance(cell, Tag)
        ]
        if any(rendered_cell for rendered_cell in rendered):
            return f"\n| {' | '.join(rendered)} |"
        return ""

    if name in _BLOCK_NAMES:
        text = _render_inline(tag, base_url, include_links)
        if text:
            prefix = "> " if name == "blockquote" else ""
            return f"\n\n{prefix}{text}"
        return ""

    if name == "hr":
        return "\n\n---\n"
    if name == "br":
        return "\n"

    return ""


def _tree_to_text(root: BeautifulSoup | Tag, base_url: str, include_links: bool) -> str:
    """Walk the DOM tree and produce clean text."""
    parts: list[str] = []

    def _walk(node: PageElement | BeautifulSoup) -> None:
        if isinstance(node, NavigableString):
            text = _MULTI_SPACE.sub(" ", str(node))
            if text.strip():
                parts.append(text)
            return

        if isinstance(node, BeautifulSoup):
            for child in node.children:
                if isinstance(child, (PageElement, BeautifulSoup)):
                    _walk(child)
            return

        if not isinstance(node, Tag):
            return

        name = node.name

        if name in _SELF_RENDERED_TAGS:
            rendered = _render_tag(node, base_url, include_links)
            if rendered:
                parts.append(rendered)
            return

        if name in _BLOCK_NAMES:
            has_child_blocks = any(
                isinstance(child, Tag) and child.name in _CHILD_BLOCK_NAMES
                for child in node.children
            )
            if not has_child_blocks:
                rendered = _render_tag(node, base_url, include_links)
                if rendered:
                    parts.append(rendered)
                return
            for child in node.children:
                _walk(child)
            return

        if name == "code":
            rendered = _render_tag(node, base_url, include_links)
            if rendered:
                parts.append(rendered)
            return

        if name == "table":
            for row in node.find_all("tr"):
                if not isinstance(row, Tag):
                    continue
                if row.find_parent("table") is not node:
                    continue
                rendered = _render_tag(row, base_url, include_links)
                if rendered:
                    parts.append(rendered)
            parts.append("\n")
            return

        if name in ("ul", "ol"):
            for list_item in node.find_all("li", recursive=False):
                if not isinstance(list_item, Tag):
                    continue
                rendered = _render_tag(list_item, base_url, include_links)
                if rendered:
                    parts.append(rendered)
            parts.append("\n")
            return

        if name in ("hr", "br"):
            rendered = _render_tag(node, base_url, include_links)
            if rendered:
                parts.append(rendered)
            return

        for child in node.children:
            _walk(child)

    _walk(root)
    return "".join(parts)


def _extract_metadata(soup: BeautifulSoup) -> dict[str, str]:
    """Extract title and description metadata from the document."""
    metadata: dict[str, str] = {}

    title_tag = soup.find("title")
    if isinstance(title_tag, Tag):
        title = title_tag.get_text(strip=True)
        if title:
            metadata["title"] = title

    if "title" not in metadata:
        og_title_tag = soup.find("meta", attrs={"property": "og:title"})
        if isinstance(og_title_tag, Tag):
            og_title = _attr_to_text(og_title_tag.get("content", "")).strip()
            if og_title:
                metadata["title"] = og_title

    description = ""
    description_tag = soup.find("meta", attrs={"name": "description"})
    if isinstance(description_tag, Tag):
        description = _attr_to_text(description_tag.get("content", "")).strip()

    if not description:
        og_description_tag = soup.find("meta", attrs={"property": "og:description"})
        if isinstance(og_description_tag, Tag):
            og_description = _attr_to_text(og_description_tag.get("content", "")).strip()
            if og_description:
                description = og_description

    if description:
        metadata["description"] = description

    return metadata


def extract_content(html: str, url: str, include_links: bool = True) -> tuple[str, dict[str, str]]:
    """Convert HTML to clean text while preserving textual information."""
    soup = BeautifulSoup(html, "html.parser")
    metadata = _extract_metadata(soup)

    body_candidate = soup.find("body")
    body: BeautifulSoup | Tag = body_candidate if isinstance(body_candidate, Tag) else soup
    _strip_noise(body)

    text = _tree_to_text(body, url, include_links)
    text = _MULTI_NEWLINE.sub("\n\n", text).strip()

    return text, metadata


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Return text truncated to max_bytes when encoded as UTF-8."""
    if max_bytes <= 0:
        return ""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _truncate_utf8_with_suffix(text: str, max_bytes: int, suffix: str) -> str:
    """Truncate text to a UTF-8 byte budget and append suffix when possible."""
    if max_bytes <= 0:
        return ""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) >= max_bytes:
        return _truncate_utf8(text, max_bytes)

    head = _truncate_utf8(text, max_bytes - len(suffix_bytes))
    return head + suffix


def _format_output(
    url: str,
    metadata: dict[str, str],
    text: str,
    raw_size: int,
    clean_size: int,
) -> str:
    """Build a structured output string for cleaned content."""
    lines: list[str] = []

    if metadata.get("title"):
        lines.append(f"Title: {metadata['title']}")
    lines.append(f"URL: {url}")
    if metadata.get("description"):
        lines.append(f"Description: {metadata['description']}")

    reduction = ((raw_size - clean_size) / raw_size * 100) if raw_size > 0 else 0
    lines.append(f"Content-Size: {raw_size:,} -> {clean_size:,} bytes ({reduction:.0f}% reduced)")
    lines.append("---")
    lines.append(text)

    return "\n".join(lines)


def _truncate_formatted_output(output: str, text: str) -> str:
    """Truncate formatted output while preserving the metadata header."""
    if len(output.encode("utf-8")) <= _MAX_URL_BYTES:
        return output

    header_marker = "---\n"
    header_end = output.find(header_marker)
    if header_end < 0:
        return _truncate_utf8_with_suffix(output, _MAX_URL_BYTES, _CONTENT_TRUNCATED_MARKER)

    header = output[: header_end + len(header_marker)]
    header_size = len(header.encode("utf-8"))
    if header_size >= _MAX_URL_BYTES:
        return _truncate_utf8_with_suffix(header, _MAX_URL_BYTES, _CONTENT_TRUNCATED_MARKER)

    remaining = _MAX_URL_BYTES - header_size
    return header + _truncate_utf8_with_suffix(text, remaining, _CONTENT_TRUNCATED_MARKER)


def _coerce_bool(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _default_port_for_scheme(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _parse_ipv4_component(value: str) -> int | None:
    if not value:
        return None
    if value.startswith(("+", "-")):
        return None

    if value.lower().startswith("0x"):
        digits = value[2:]
        if not digits:
            return None
        base = 16
    elif len(value) > 1 and value.startswith("0"):
        digits = value[1:]
        if not digits:
            return 0
        base = 8
    else:
        digits = value
        base = 10

    try:
        return int(digits, base)
    except ValueError:
        return None


def _parse_obfuscated_ipv4(host: str) -> ipaddress.IPv4Address | None:
    parts = host.split(".")

    if len(parts) == 1:
        value = _parse_ipv4_component(parts[0])
        if value is None or value > 0xFFFFFFFF:
            return None
        return ipaddress.IPv4Address(value)

    if len(parts) < 2 or len(parts) > 4:
        return None

    parsed_parts: list[int] = []
    for part in parts:
        value = _parse_ipv4_component(part)
        if value is None:
            return None
        parsed_parts.append(value)

    if len(parsed_parts) == 2:
        first, second = parsed_parts
        if first > 0xFF or second > 0xFFFFFF:
            return None
        packed = (first << 24) | second
        return ipaddress.IPv4Address(packed)

    if len(parsed_parts) == 3:
        first, second, third = parsed_parts
        if first > 0xFF or second > 0xFF or third > 0xFFFF:
            return None
        packed = (first << 24) | (second << 16) | third
        return ipaddress.IPv4Address(packed)

    first, second, third, fourth = parsed_parts
    if first > 0xFF or second > 0xFF or third > 0xFF or fourth > 0xFF:
        return None
    return ipaddress.IPv4Address((first << 24) | (second << 16) | (third << 8) | fourth)


def _parse_ip_literal(host: str) -> IpAddress | None:
    host_without_zone = host.split("%", 1)[0]

    try:
        return ipaddress.ip_address(host_without_zone)
    except ValueError:
        pass

    return _parse_obfuscated_ipv4(host_without_zone)


def _is_blocked_ip(address: IpAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_blocked_ip(address.ipv4_mapped)

    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _resolve_host_addresses(host: str, port: int) -> list[IpAddress]:
    loop = asyncio.get_running_loop()

    try:
        info = await loop.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as error:
        raise ValueError(f"unable to resolve host: {host}") from error

    addresses: list[IpAddress] = []
    seen: set[str] = set()
    for family, _, _, _, socket_address in info:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue

        address_text = socket_address[0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            continue

        key = str(address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)

    if not addresses:
        raise ValueError(f"unable to resolve host: {host}")

    return addresses


async def _validate_public_target(url: httpx.URL) -> None:
    if url.scheme not in {"http", "https"}:
        raise ValueError("only http/https URLs are allowed")

    host = url.host
    if host is None:
        raise ValueError("url must include a valid host")

    normalized_host = host.rstrip(".").lower()
    if not normalized_host:
        raise ValueError("url must include a valid host")

    if normalized_host == "localhost":
        raise ValueError("URL blocked (private/loopback address)")

    literal_address = _parse_ip_literal(normalized_host)
    if literal_address is not None:
        if _is_blocked_ip(literal_address):
            raise ValueError("URL blocked (private/loopback address)")
        return

    port = url.port if url.port is not None else _default_port_for_scheme(url.scheme)
    for resolved in await _resolve_host_addresses(normalized_host, port):
        if _is_blocked_ip(resolved):
            raise ValueError("URL blocked (private/loopback address)")


async def _sleep_for_retry(attempt: int) -> None:
    base_delay = _RETRY_INITIAL_DELAY_SECONDS * (_RETRY_BACKOFF_FACTOR**attempt)
    jitter = random.uniform(0, base_delay * _RETRY_JITTER_FACTOR)
    await asyncio.sleep(base_delay + jitter)


async def _request_with_retry(client: httpx.AsyncClient, url: httpx.URL) -> httpx.Response:
    """Fetch a URL and retry retryable status codes with backoff and jitter."""
    for attempt in range(_RETRY_MAX_RETRIES + 1):
        response = await client.get(url)
        if response.status_code >= 400:
            # GET is idempotent — safe to repeat (includes a transient 500).
            if attempt >= _RETRY_MAX_RETRIES or not is_retryable_status(
                response.status_code, idempotent=True
            ):
                response.raise_for_status()
            await _sleep_for_retry(attempt)
            continue

        return response

    raise RuntimeError("unreachable retry loop state")


async def _fetch_with_retry(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Fetch a URL, validating each redirect hop against SSRF rules."""
    current_url = httpx.URL(url)

    for redirect_count in range(_MAX_REDIRECTS + 1):
        await _validate_public_target(current_url)

        response = await _request_with_retry(client, current_url)
        if response.status_code not in _REDIRECT_STATUS_CODES:
            return response

        next_request = response.next_request
        if next_request is None:
            return response

        if redirect_count >= _MAX_REDIRECTS:
            raise httpx.HTTPStatusError(
                "too many redirects while fetching URL",
                request=response.request,
                response=response,
            )

        current_url = next_request.url

    raise RuntimeError("unreachable retry loop state")


async def web_fetch_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Handle a web_fetch tool call and return a stable vBot result envelope."""
    del context

    unknown_arguments = set(arguments) - {"url", "include_links", "raw"}
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return tool_failure("validation_error", f"Unknown argument(s): {names}", retryable=False)

    url_argument = arguments.get("url")
    if not isinstance(url_argument, str) or not url_argument.strip():
        return tool_failure("validation_error", "url must be a non-empty string", retryable=False)

    try:
        include_links = _coerce_bool(
            arguments.get("include_links"), field_name="include_links", default=True
        )
        raw = _coerce_bool(arguments.get("raw"), field_name="raw", default=False)
    except ValueError as error:
        return tool_failure("validation_error", str(error), retryable=False)

    url = url_argument.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return tool_failure("validation_error", "only http/https URLs are allowed", retryable=False)
    if not parsed.netloc or parsed.hostname is None:
        return tool_failure("validation_error", "url must include a valid host", retryable=False)

    try:
        async with _make_client() as client:
            response = await _fetch_with_retry(client, url)
    except ValueError as error:
        return tool_failure("validation_error", str(error), retryable=False)
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code if error.response is not None else None
        status = status_code if status_code is not None else "unknown"
        _LOGGER.warning("web_fetch request failed: HTTP %s for %s", status, url)
        # A retryable status only reaches here after the retry loop exhausted its
        # attempts; a non-retryable status (e.g. 404) failed on the first try.
        retryable = status_code is not None and is_retryable_status(status_code, idempotent=True)
        return tool_failure(
            "request_error",
            f"HTTP {status} while fetching URL: {url}",
            retryable=retryable,
            attempts_made=(_RETRY_MAX_RETRIES + 1) if retryable else None,
        )
    except httpx.RequestError as error:
        # Transport errors are not retried by web_fetch's status-only retry loop,
        # so the tool made a single attempt; the failure is still transient.
        _LOGGER.warning("web_fetch request failed for %s: %s", url, error)
        return tool_failure(
            "request_error",
            f"request failed while fetching URL: {error}",
            retryable=True,
            attempts_made=1,
        )

    raw_body = response.text
    raw_size = len(raw_body.encode("utf-8"))

    content_type = response.headers.get("Content-Type", "")
    if raw or "html" not in content_type.lower():
        content = _truncate_utf8_with_suffix(
            raw_body,
            _MAX_URL_BYTES,
            _RESPONSE_TRUNCATED_MARKER,
        )
        return tool_success({"content": content})

    final_url = str(response.url)
    text, metadata = extract_content(raw_body, final_url, include_links=include_links)
    clean_size = len(text.encode("utf-8"))

    output = _format_output(final_url, metadata, text, raw_size, clean_size)
    output = _truncate_formatted_output(output, text)
    return tool_success({"content": output})


def register_web_fetch_tool(registry: ToolRegistry) -> None:
    """Register the web_fetch tool with a vBot tool registry."""
    registry.register(
        WEB_FETCH_TOOL_NAME,
        WEB_FETCH_TOOL_DESCRIPTION,
        WEB_FETCH_TOOL_PARAMETERS,
        web_fetch_handler,
        display=ToolDisplay(summary_fields=("url",)),
    )


__all__ = [
    "WEB_FETCH_TOOL_DESCRIPTION",
    "WEB_FETCH_TOOL_NAME",
    "WEB_FETCH_TOOL_PARAMETERS",
    "extract_content",
    "register_web_fetch_tool",
    "web_fetch_handler",
]
