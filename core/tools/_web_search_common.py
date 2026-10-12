"""Internal Web Search domain restrictions and plain-text normalization."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlsplit

import idna

_DOMAIN_LABEL_PATTERN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")


_MAX_DOMAIN_FILTERS = 10


_SITE_OPERATOR = re.compile(r"(?<![^\s(])(-?)site:([^\s()\"]+)", re.IGNORECASE)
_EMPTY_GROUP = re.compile(r"\(\s*(?:(?:OR|\|)\s*)*\)")
_CONNECTORS = frozenset({"OR", "|"})


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


def _normalize_domains(raw: Any, field: str = "domains") -> tuple[list[str], str | None]:
    if raw is None:
        return [], None

    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw_domain in enumerate(raw):
        domain, error = _canonicalize_domain(raw_domain)
        if error is not None or domain is None:
            return [], f"{field}[{index}] {error or 'must be a valid domain'}"
        if domain not in seen:
            seen.add(domain)
            normalized.append(domain)

    return normalized, None


def _build_search_query(query: str, domains: list[str], exclude: list[str] | None = None) -> str:
    terms = [query]
    if domains:
        terms.append(" OR ".join(f"site:{domain}" for domain in domains))
    terms.extend(f"-site:{domain}" for domain in exclude or ())
    return " ".join(terms)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _site(operand: str) -> tuple[str | None, bool]:
    """Return the host a ``site:`` operand names, and whether it names a path too."""
    parts = urlsplit(operand if "://" in operand else f"//{operand}")
    try:
        host = parts.hostname or ""
    except ValueError:
        return None, False
    domain, error = _canonicalize_domain(host)
    if error is not None:
        return None, False
    return domain, bool(parts.path.strip("/") or parts.query or parts.fragment)


def split_site_operators(query: str) -> tuple[str, list[str], list[str], list[tuple[str, str]]]:
    """Move ``site:host`` and ``-site:host`` operators out of the query.

    Restrictions cover whole sites, so ``site:host/path`` restricts to ``host``;
    the last list names each such operator with the site it was read as.
    ``-site:host/path`` stays in the query, since excluding the whole site would
    drop results the Agent wanted; so do operators inside quoted phrases and
    operators that would leave no search words. ``OR`` connectors and parentheses
    left empty by the removal are dropped, so ``x (site:a.com OR site:b.com)``
    becomes ``x`` restricted to both sites.
    """
    included: list[str] = []
    excluded: list[str] = []
    widened: list[tuple[str, str]] = []

    def take(match: re.Match[str]) -> str:
        domain, path = _site(match[2])
        if domain is None or (path and match[1]):
            return match[0]
        (excluded if match[1] else included).append(domain)
        if path:
            widened.append((match[0], domain))
        return " "

    parts = re.split(r'("[^"]*")', query)
    rest = "".join(
        part
        if part.startswith('"') and part.endswith('"') and len(part) > 1
        else _SITE_OPERATOR.sub(take, part)
        for part in parts
    )
    if not included and not excluded:
        return query, [], [], []
    rest = _EMPTY_GROUP.sub(" ", rest)
    words: list[str] = []
    for word in rest.split():
        if word in _CONNECTORS and (not words or words[-1] in _CONNECTORS):
            continue
        words.append(word)
    while words and words[-1] in _CONNECTORS:
        words.pop()
    if not words:
        return query, [], [], []
    return " ".join(words), _unique(included), _unique(excluded), widened


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
    exclude: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep results inside ``domains`` (when given) and outside ``exclude``."""
    kept = []
    for result in results:
        url = _normalize_text(result.get("url"))
        if domains and not _url_matches_domains(url, domains):
            continue
        if exclude and _url_matches_domains(url, exclude):
            continue
        kept.append(result)
    return kept[:count]


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
    from bs4 import BeautifulSoup

    return BeautifulSoup(text, "html.parser").get_text().strip()
