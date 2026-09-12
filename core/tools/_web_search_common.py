"""Internal Web Search domain restrictions and plain-text normalization."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlsplit

import idna
from bs4 import BeautifulSoup

_DOMAIN_LABEL_PATTERN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")


_MAX_DOMAIN_FILTERS = 10


def _normalize_text(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _canonicalize_domain(raw: str) -> tuple[str | None, str | None]:
    text = raw.strip().rstrip(".").lower()
    if not text:
        return None, "must be a non-empty domain"

    try:
        domain = idna.encode(text, uts46=True, std3_rules=True).decode("ascii")
    except idna.IDNAError:
        return None, "must be a valid domain"

    if len(domain) > 253:
        return None, "must be at most 253 characters"

    labels = domain.split(".")
    if any(_DOMAIN_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        return None, "must be a hostname without a scheme, port, path, query, or wildcard"

    return domain, None


def _normalize_domains(raw: Any) -> tuple[list[str], str | None]:
    if raw is None:
        return [], None

    if not isinstance(raw, list):
        return [], "domains must be an array of domain strings"
    if not raw:
        return [], "domains must contain at least one domain when provided"
    if len(raw) > _MAX_DOMAIN_FILTERS:
        return [], f"domains must contain at most {_MAX_DOMAIN_FILTERS} domains"

    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw_domain in enumerate(raw):
        if not isinstance(raw_domain, str):
            return [], f"domains[{index}] must be a string"
        domain, error = _canonicalize_domain(raw_domain)
        if error is not None or domain is None:
            return [], f"domains[{index}] {error or 'must be a valid domain'}"
        if domain not in seen:
            seen.add(domain)
            normalized.append(domain)

    return normalized, None


def _build_search_query(query: str, domains: list[str]) -> str:
    if not domains:
        return query
    domain_expression = " OR ".join(f"site:{domain}" for domain in domains)
    return f"{query} {domain_expression}"


def _url_matches_domains(url: str, domains: list[str]) -> bool:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return False
    if hostname is None:
        return False

    canonical_hostname, error = _canonicalize_domain(hostname)
    if error is not None or canonical_hostname is None:
        return False
    return any(
        canonical_hostname == domain or canonical_hostname.endswith(f".{domain}")
        for domain in domains
    )


def _restrict_results_to_domains(
    results: list[dict[str, Any]],
    domains: list[str],
    count: int,
) -> list[dict[str, Any]]:
    if not domains:
        return results[:count]
    return [
        result
        for result in results
        if _url_matches_domains(_normalize_text(result.get("url")), domains)
    ][:count]


def _clean_snippet(raw: Any) -> str:
    """Normalize provider display text: drop HTML tags and unescape entities.

    Brave decorates titles/descriptions with highlight markup and HTML
    entities; that is pure noise for a model, so result text is flattened to
    plain text before it enters the envelope.
    """
    text = _normalize_text(raw)
    if not text:
        return ""
    if "<" not in text:
        return html.unescape(text).strip()
    return BeautifulSoup(text, "html.parser").get_text().strip()
