"""Plain-text, transient Extension attention and page-link contracts.

The owner-bound host authorizes declared pages. These pure rules are shared
with native accessors; neither notifications nor their keys form an inbox.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol
from urllib.parse import quote

MAX_NOTIFICATION_TITLE = 200
MAX_NOTIFICATION_BODY = 2000
MAX_NOTIFICATION_KEY = 128
MAX_PAGE_ROUTE = 1024
MAX_PAGE_BADGE = 2**31 - 1
PAGE_BADGES_RESOURCE = "page_badges"


class Notify(Protocol):
    async def __call__(
        self, title: str, body: str, *, page_id: str, route: str = "", key: str | None = None
    ) -> None: ...


def validate_page_route(route: str) -> str:
    """A plain page-local route, not a URL, query, fragment or encoded path."""
    if not isinstance(route, str) or len(route) > MAX_PAGE_ROUTE:
        raise ValueError(f"Page route must be text of at most {MAX_PAGE_ROUTE} characters")
    if not route:
        return route
    if (
        not route.startswith("/")
        or any(character in route for character in "\\?#%")
        or any(unicodedata.category(character).startswith("C") for character in route)
        or any(segment in {"", ".", ".."} for segment in route[1:].split("/"))
    ):
        raise ValueError("Page route must be empty or /segment/... without URL escaping")
    return route


def notification_text(value: str, *, name: str, maximum: int, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Notification {name} must be text of at most {maximum} characters")
    if any(
        unicodedata.category(character).startswith("C")
        and not (multiline and character in "\n\r\t")
        for character in value
    ) or (not multiline and any(character in value for character in "\n\r\u2028\u2029")):
        raise ValueError(f"Notification {name} contains unsupported control characters")
    if name in {"title", "key"} and not value.strip():
        raise ValueError(f"Notification {name} must not be empty")
    return value


def page_link(extension: str, page_id: str, route: str = "") -> str:
    """Build only a WebUI fragment; never open a supplied external destination."""
    if (
        not isinstance(extension, str)
        or not extension.strip()
        or any(character in extension for character in "/\\:?#%")
        or any(unicodedata.category(character).startswith("C") for character in extension)
        or not isinstance(page_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", page_id) is None
    ):
        raise ValueError("Extension page requires valid owner and page identifiers")
    validate_page_route(route)
    place = "/".join(quote(segment, safe=":@") for segment in route[1:].split("/")) if route else ""
    view = quote(f"extension:{extension}:{page_id}", safe=":@")
    return f"/#{view}" + (f"/{place}" if place else "")
