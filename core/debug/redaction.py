"""Sensitive-data redaction utilities for debug traces.

Redacts credentials and secrets from provider wire captures before they
are written to disk.  The rules are intentionally conservative: exact
header-name matches plus whole-word patterns on header names, URL query
parameters, and JSON object keys.

Free-text redaction inside string values is intentionally out of scope —
only structured keys are checked.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Header names that are always redacted (case-insensitive).
_SENSITIVE_HEADERS = {"authorization", "x-api-key"}

# Header/keyword fragments that trigger redaction when they appear as
# a whole word inside a header name, query-param name, or JSON key.
_SENSITIVE_WORDS = {"token", "secret", "key", "password", "credential"}

_REDACTED = "[REDACTED]"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with sensitive values replaced.

    A header is considered sensitive when its name (case-insensitive)
    is ``Authorization``, ``x-api-key``, or contains any whole word
    from the sensitive-word list: ``token``, ``secret``, ``key``,
    ``password``, ``credential``.

    Whole-word matching splits on hyphens and underscores so that
    ``x-api-key`` and ``x_token_header`` both match while ``donkey``
    does not.
    """
    return {
        name: _REDACTED if _is_sensitive_key(name) else value for name, value in headers.items()
    }


def redact_url(url: str) -> str:
    """Return *url* with sensitive query-parameter values redacted.

    Query-parameter names are checked against the same rules as header
    names (see :func:`redact_headers`).  If the URL cannot be parsed it
    is returned unchanged.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    if not parsed.query:
        return url

    query_params = parse_qs(parsed.query, keep_blank_values=True)
    redacted_params = {
        name: [_REDACTED] * len(values) if _is_sensitive_key(name) else values
        for name, values in query_params.items()
    }

    new_query = urlencode(redacted_params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def redact_json_body(body: Any) -> Any:
    """Recursively redact sensitive keys in a JSON-like structure.

    Only *keys* are inspected — string *values* are never scanned for
    secrets.  When a key is flagged as sensitive its associated value
    is replaced with the literal string ``"[REDACTED]"``, regardless
    of the original value's type.

    Returns:
        The redacted structure (*body* is not mutated), or *body*
        unchanged when it is not a ``dict`` or ``list``.
    """
    if isinstance(body, dict):
        return {
            key: _REDACTED if _is_sensitive_key(key) else redact_json_body(value)
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [redact_json_body(item) for item in body]
    return body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: str) -> bool:
    """Return ``True`` when *key* matches a sensitive pattern."""
    lower = key.lower()
    if lower in _SENSITIVE_HEADERS:
        return True
    parts = lower.replace("_", "-").split("-")
    return any(part in _SENSITIVE_WORDS for part in parts)
