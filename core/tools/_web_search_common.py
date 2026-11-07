"""Internal Web Search domain restrictions and plain-text normalization."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import idna

_DOMAIN_LABEL_PATTERN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")


_MAX_DOMAIN_FILTERS = 10


_SITE_OPERATOR = re.compile(r"(?<![^\s(])(-?)site:([^\s()\"]+)", re.IGNORECASE)
_QUERY_TOKEN = re.compile(r'"[^\"]*"|[()]|[^\s()\"]+|"')
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


def _site_factors(query: str) -> list[str] | None:
    """Split only a global conjunction, keeping every parenthesized group intact.

    This deliberately does not interpret mixed Boolean expressions. An OR or
    unary NOT outside a group prevents extraction; a group containing sites
    must consist solely of positive site alternatives (checked by the caller).
    """
    factors: list[str] = []
    depth = 0
    start = 0
    conjunction = False
    for match in _QUERY_TOKEN.finditer(query):
        word = match[0]
        if word == "(":
            if depth == 0:
                if match.start() and not query[match.start() - 1].isspace():
                    return None
                start = match.start()
            depth += 1
        elif word == ")":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                factors.append(query[start : match.end()])
                conjunction = False
        elif depth == 0:
            if word in {"OR", "|", "NOT", "-"}:
                return None
            if word == "AND":
                if not factors or conjunction:
                    return None
                conjunction = True
            else:
                factors.append(word)
                conjunction = False
    return None if depth or conjunction else factors


def _site_alternatives(factor: str) -> list[re.Match[str]] | None:
    """Recognize one site or a group of positive site alternatives, nothing else."""
    tokens = [token for token in _QUERY_TOKEN.findall(factor) if token not in {"(", ")"}]
    matches: list[re.Match[str]] = []
    for index, token in enumerate(tokens):
        if index % 2:
            if token not in {"OR", "|"}:
                return None
        else:
            match = _SITE_OPERATOR.fullmatch(token)
            if match is None or (len(tokens) > 1 and match[1]):
                return None
            matches.append(match)
    return matches if len(tokens) % 2 else None


def _has_site_operator(query: str) -> bool:
    return any(
        not part.startswith('"') and _SITE_OPERATOR.search(part)
        for part in re.split(r'("[^\"]*")', query)
    )


def split_site_operators(
    query: str,
) -> tuple[str, list[str], list[str], list[tuple[str, str]]] | None:
    """Move only globally conjoined site restrictions out of the query.

    Restrictions cover whole sites, so ``site:host/path`` restricts to ``host``.
    When it is the only site restriction, the words of its path (``iron`` in
    ``site:host/iron/index.html``) join the search words, so the search still
    aims at what the path names; with several sites, the paths belong to
    alternatives and add no words. The last list pairs each such operator with
    how it was read. Quoted operators stay literal. ``None`` means the filters
    cannot express the query: keep the complete query for operator providers,
    or refuse it for native-filter providers. In particular, never flatten
    branch-specific sites, disjoint site conjunctions, NOT, or a negative site path.
    """
    if not _has_site_operator(query):
        return query, [], [], []
    factors = _site_factors(query)
    if factors is None:
        return None
    included: list[str] = []
    excluded: list[str] = []
    paths: list[tuple[str, str, list[str]]] = []
    words: list[str] = []
    for factor in factors:
        if not _has_site_operator(factor):
            words.append(factor)
            continue
        matches = _site_alternatives(factor)
        if not matches:
            return None
        factor_domains: list[str] = []
        factor_paths: list[tuple[str, str, list[str]]] = []
        for match in matches:
            domain, path_words = _site(match[2])
            if domain is None or (path_words is not None and match[1]):
                return None
            factor_domains.append(domain)
            if path_words is not None:
                factor_paths.append((match[0], domain, path_words))
        if matches[0][1]:
            excluded.extend(factor_domains)
        elif included:
            # Independent required site clauses intersect; an empty intersection
            # or conjoined paths cannot become a broader list of alternatives.
            if paths or factor_paths:
                return None
            included = _intersect_domains(included, factor_domains)
            if not included:
                return None
        else:
            included = _unique(factor_domains)
        paths.extend(factor_paths)
    added: list[str] = []
    if len(included) == 1 and paths:
        present = {word.casefold() for word in re.findall(r"\w+", " ".join(words))}
        for word in paths[0][2]:
            if word.casefold() not in present:
                present.add(word.casefold())
                added.append(word)
    if not words and not added:
        return None
    readings = [
        (operator, " ".join([f"site:{domain}", *(added if index == 0 else ())]))
        for index, (operator, domain, _) in enumerate(paths)
    ]
    return " ".join([*words, *added]), _unique(included), _unique(excluded), readings


def _intersect_domains(first: list[str], second: list[str]) -> list[str]:
    """A global filter and an inline filter both apply, including subdomains."""
    return _unique(
        [
            narrower
            for left in first
            for right in second
            for narrower, broader in ((left, right), (right, left))
            if narrower == broader or narrower.endswith(f".{broader}")
        ]
    )


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
    """One constrained search shared by every engine adapter."""

    query: str
    domains: list[str]
    exclude: list[str]
    count: int
    page: int
    recency: str

    @staticmethod
    def resolve_sites(
        query: str,
        raw_domains: Any,
        raw_exclude: Any,
        *,
        supports_site_expressions: bool = False,
    ) -> tuple[str, list[str], list[str], list[tuple[str, str]], str | None]:
        domains, error = _normalize_domains(raw_domains)
        if error is not None:
            return query, [], [], [], error
        exclude, error = _normalize_domains(raw_exclude, field="exclude_domains")
        if error is not None:
            return query, [], [], [], error
        widened: list[tuple[str, str]] = []
        split = split_site_operators(query)
        if split is None:
            if not supports_site_expressions:
                return (
                    query,
                    domains,
                    exclude,
                    widened,
                    "No search was run. This provider cannot preserve the site conditions "
                    "in this query. Use separate calls for branches with different sites. "
                    "For a global restriction, pass sites in domains or exclude_domains; "
                    "check result URLs for path restrictions.",
                )
        else:
            query, inline_domains, inline_exclude, widened = split
            if domains and inline_domains:
                intersection = _intersect_domains(domains, inline_domains)
                if not intersection:
                    return (
                        query,
                        domains,
                        exclude,
                        widened,
                        "No search was run. domains and the query's site conditions require "
                        "different sites. Keep the intended sites in domains and remove the "
                        "site conditions from query.",
                    )
                domains = intersection
            else:
                domains = domains or inline_domains
            exclude = _unique([*exclude, *inline_exclude])
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
        query = self.query
        if (self.domains or self.exclude) and _site_factors(query) is None:
            query = f"({query})"
        terms = [query]
        if self.domains:
            sites = " OR ".join(f"site:{domain}" for domain in self.domains)
            terms.append(f"({sites})" if len(self.domains) > 1 else sites)
        terms.extend(f"-site:{domain}" for domain in self.exclude)
        return " ".join(terms)

    def restrict(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep results inside required domains and outside excluded ones."""
        kept = []
        for result in results:
            url = _normalize_text(result.get("url"))
            if self.domains and not _url_matches_domains(url, self.domains):
                continue
            if self.exclude and _url_matches_domains(url, self.exclude):
                continue
            kept.append(result)
        return kept[: self.count]
