"""Internal Web Search domain restrictions and plain-text normalization."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import idna

from core.permissions import PermissionBoundary

_DOMAIN_LABEL_PATTERN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")


_MAX_DOMAIN_FILTERS = 10


_SITE_OPERATOR = re.compile(r"(?<![^\s(])(-?)site:([^\s()\"]+)", re.IGNORECASE)
_EMPTY_GROUP = re.compile(r"\(\s*(?:(?:OR|\|)\s*)*\)")
_CONNECTORS = frozenset({"OR", "|"})
# Path parts that name a file type or a directory default, not what a page is about.
_GENERIC_PATH_WORDS = frozenset(
    {"htm", "html", "shtml", "xhtml", "php", "asp", "aspx", "jsp", "cfm", "cgi", "pdf"}
    | {"index", "default"}
)


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


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _site(operand: str) -> tuple[str | None, list[str] | None]:
    """Return the host a ``site:`` operand names, and the words of its path, if it has one."""
    parts = urlsplit(operand if "://" in operand else f"//{operand}")
    try:
        host = parts.hostname or ""
    except ValueError:
        return None, None
    domain, error = _canonicalize_domain(host)
    if error is not None:
        return None, None
    if not (parts.path.strip("/") or parts.query or parts.fragment):
        return domain, None
    words = [
        word
        for word in re.split(r"[\W_]+", unquote(parts.path))
        if any(char.isalpha() for char in word) and word.lower() not in _GENERIC_PATH_WORDS
    ]
    return domain, words


def split_site_operators(query: str) -> tuple[str, list[str], list[str], list[tuple[str, str]]]:
    """Move ``site:host`` and ``-site:host`` operators out of the query.

    Restrictions cover whole sites, so ``site:host/path`` restricts to ``host``.
    When it is the only site restriction, the words of its path (``iron`` in
    ``site:host/iron/index.html``) join the search words, so the search still
    aims at what the path names; with several sites, the paths belong to
    alternatives and add no words. The last list pairs each such operator with
    how it was read. ``-site:host/path`` stays in the query, since excluding the
    whole site would drop results the Agent wanted; so do operators inside
    quoted phrases and operators that would leave no search words. ``OR``
    connectors and parentheses left empty by the removal are dropped, so
    ``x (site:a.com OR site:b.com)`` becomes ``x`` restricted to both sites.
    """
    included: list[str] = []
    excluded: list[str] = []
    paths: list[tuple[str, str, list[str]]] = []

    def take(match: re.Match[str]) -> str:
        domain, path_words = _site(match[2])
        if domain is None or (path_words is not None and match[1]):
            return match[0]
        (excluded if match[1] else included).append(domain)
        if path_words is not None:
            paths.append((match[0], domain, path_words))
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
    added: list[str] = []
    if len(included) == 1 and paths:
        present = {word.casefold() for word in re.findall(r"\w+", " ".join(words))}
        for word in paths[0][2]:
            if word.casefold() not in present:
                present.add(word.casefold())
                added.append(word)
    if not words and not added:
        return query, [], [], []
    readings = [
        (operator, " ".join([f"site:{domain}", *(added if index == 0 else ())]))
        for index, (operator, domain, _) in enumerate(paths)
    ]
    return " ".join([*words, *added]), _unique(included), _unique(excluded), readings


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


@dataclass(frozen=True)
class WebSearchRequest:
    """One constrained search shared by every engine adapter.

    ``permissions`` is the calling Session's boundary: a result whose address
    it refuses is left out and its address collected in ``refused``.
    """

    query: str
    domains: list[str]
    exclude: list[str]
    count: int
    page: int
    recency: str
    permissions: PermissionBoundary = field(kw_only=True)
    refused: dict[str, str] = field(default_factory=dict, compare=False)

    @staticmethod
    def resolve_sites(
        query: str, raw_domains: Any, raw_exclude: Any
    ) -> tuple[str, list[str], list[str], list[tuple[str, str]], str | None]:
        domains, error = _normalize_domains(raw_domains)
        if error is not None:
            return query, [], [], [], error
        exclude, error = _normalize_domains(raw_exclude, field="exclude_domains")
        if error is not None:
            return query, [], [], [], error
        widened: list[tuple[str, str]] = []
        if not domains and not exclude:
            query, domains, exclude, widened = split_site_operators(query)
        both = [domain for domain in domains if domain in exclude]
        if both:
            return (
                query,
                domains,
                exclude,
                widened,
                (
                    f"{both[0]} is both required and excluded. Keep it in domains or in "
                    "exclude_domains, not both."
                ),
            )
        return query, domains, exclude, widened, None

    def operator_query(self) -> str:
        terms = [self.query]
        if self.domains:
            terms.append(" OR ".join(f"site:{domain}" for domain in self.domains))
        terms.extend(f"-site:{domain}" for domain in self.exclude)
        return " ".join(terms)

    def restrict(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep results inside required domains and outside excluded ones, and
        those the permission rules let this Agent fetch."""
        kept = []
        for result in results:
            url = _normalize_text(result.get("url"))
            if self.domains and not _url_matches_domains(url, self.domains):
                continue
            if self.exclude and _url_matches_domains(url, self.exclude):
                continue
            if (refusal := self.permissions.check_url(url)) is not None:
                # By address: an adapter that restricts twice counts a result once.
                self.refused[url] = refusal.target
                continue
            kept.append(result)
        return kept[: self.count]
