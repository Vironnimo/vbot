"""Interpret web_search arguments, including the shapes other search Tools use.

Everything here runs before validation: field aliases (``q``, ``num_results``,
``allowed_domains``, ``freshness``...), result-age spellings (``pw``, ``7d``,
``past_week``, ``qdr:w``), domain lists written as text or addresses, and date
filters. A value either has one clear meaning or the call is rejected with the
reason before anything is searched.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from core.tools._argument_repair import normalize_call_arguments
from core.tools._spelling_aliases import SpellingAliases, spelling
from core.tools.contracts import ToolContract, ToolContractError

RECENCY_VALUES = ("day", "week", "month", "year")

# Names other search Tools and Agents use for the same fields.
FIELD_ALIASES = SpellingAliases(
    {
        "query": (
            "q",
            "search",
            "search_term",
            "search_query",
            "search_string",
            "query_string",
            "query_text",
            "question",
            "queries",
            "search_queries",
        ),
        "domains": (
            "domain",
            "site",
            "sites",
            "allowed_domains",
            "include_domains",
            "included_domains",
            "domain_filter",
            "search_domain_filter",
            "site_filter",
            "allowed_sites",
            "only_domains",
        ),
        "exclude_domains": (
            "blocked_domains",
            "excluded_domains",
            "block_domains",
            "exclude_sites",
            "excluded_sites",
            "blocked_sites",
            "deny_domains",
            "exclude",
        ),
        "count": (
            "num_results",
            "max_results",
            "limit",
            "num",
            "n",
            "k",
            "top_k",
            "number_of_results",
            "result_count",
            "results",
            "max_count",
            "size",
            "per_page",
        ),
        "page": ("page_number", "pageno", "page_num"),
        "recency": (
            "freshness",
            "time_range",
            "timeframe",
            "time_frame",
            "time_period",
            "period",
            "date_range",
            "recency_filter",
            "search_recency_filter",
            "tbs",
            "max_age",
            "age",
        ),
        "days": (
            "recency_days",
            "max_age_days",
            "past_days",
            "last_days",
            "days_back",
            "within_days",
            "number_of_days",
        ),
        "date_after": (
            "after",
            "since",
            "start_date",
            "from_date",
            "date_from",
            "published_after",
            "start_published_date",
            "min_date",
        ),
        "date_before": (
            "before",
            "until",
            "end_date",
            "to_date",
            "date_to",
            "published_before",
            "end_published_date",
            "max_date",
        ),
    }
)

_RECENCY_WORDS = {
    **dict.fromkeys(
        ("day", "d", "pd", "daily", "today", "pastday", "lastday", "qdr:d", "past24hours"), "day"
    ),
    **dict.fromkeys(
        ("week", "w", "pw", "weekly", "pastweek", "lastweek", "thisweek", "qdr:w"), "week"
    ),
    **dict.fromkeys(
        ("month", "m", "pm", "monthly", "pastmonth", "lastmonth", "thismonth", "qdr:m"), "month"
    ),
    **dict.fromkeys(
        ("year", "y", "py", "yearly", "annual", "pastyear", "lastyear", "thisyear", "qdr:y"),
        "year",
    ),
    **dict.fromkeys(("hour", "h", "ph", "pasthour", "lasthour", "qdr:h"), f"{1 / 24:g}d"),
}
_NO_RECENCY = frozenset({"any", "all", "anytime", "alltime", "none", "no", "off", "unlimited"})
_DURATION = re.compile(
    r"(?:past|last|within)?(\d+(?:\.\d+)?)"
    r"(h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|m|mo|mos|month|months|y|yr|yrs|year|years)"
    r"(?:ago|old)?"
)
_UNIT_DAYS = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}
# Durations that name a whole recency window exactly.
_EXACT_WINDOWS = {
    1: "day",
    7: "week",
    **dict.fromkeys((28, 30, 31), "month"),
    **dict.fromkeys((360, 364, 365, 366), "year"),
}
_DATE_RANGE = re.compile(r"\d{4}-\d{2}-\d{2}\s*(?:to|\.\.|/|until)\s*\d{4}-\d{2}-\d{2}")
_DATE = re.compile(r"(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?")
_WRAPPERS = {("<", ">"), ('"', '"'), ("'", "'"), ("`", "`")}
_DATE_BEFORE_REFUSAL = (
    "web_search cannot return only results published before a date: it can limit results "
    "to the past day, week, month or year (recency). Remove date_before, or name the "
    "period in query, for example 2024."
)
_DATE_RANGE_REFUSAL = (
    "web_search cannot limit results to a date range: it can limit results to the past "
    "day, week, month or year (recency). Use recency, or name the period in query, for "
    "example 2024."
)


def recency_token(value: Any) -> str | None:
    """Return a canonical window, ``"<days>d"`` for another duration, or None."""
    if not isinstance(value, str):
        return None
    if _DATE_RANGE.fullmatch(value.strip().lower()):
        raise ToolContractError(_DATE_RANGE_REFUSAL)
    key = spelling(value)
    if key in RECENCY_VALUES:
        return key
    if key in _RECENCY_WORDS:
        return _RECENCY_WORDS[key]
    match = _DURATION.fullmatch(key)
    if match is None:
        return None
    days = float(match[1]) * _UNIT_DAYS[match[2][0]]
    if days <= 0:
        return None
    if days.is_integer() and int(days) in _EXACT_WINDOWS:
        return _EXACT_WINDOWS[int(days)]
    return f"{days:g}d"


def parse_date(value: str) -> datetime | None:
    """Parse an ISO-like date or timestamp as an aware UTC datetime."""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        match = _DATE.fullmatch(text)
        if match is None:
            return None
        try:
            parsed = datetime(int(match[1]), int(match[2] or 1), int(match[3] or 1))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _unwrapped(value: str) -> str:
    text = value.strip()
    while len(text) >= 2 and (text[0], text[-1]) in _WRAPPERS:
        text = text[1:-1].strip()
    return text


def _query(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    queries = [item.strip() if isinstance(item, str) else item for item in value]
    queries = [item for item in queries if item != ""]
    if len(queries) > 1:
        raise ToolContractError(
            f"web_search runs one query per call; received {len(queries)}. Make one "
            "web_search call per query; the calls can run at the same time."
        )
    return queries[0] if queries else ""


def _domain(item: str) -> str:
    """Reduce one written site to a host name, keeping a leading "-" exclusion mark."""
    text = _unwrapped(item)
    mark = ""
    if text.startswith("-"):
        mark, text = "-", text[1:].strip()
    if text.lower().startswith("site:"):
        text = text[5:]
    has_scheme = "://" in text
    parts = urlsplit(text if has_scheme else f"//{text}")
    try:
        host = parts.hostname or ""
    except ValueError:
        return item
    if parts.path.strip("/") or parts.query or parts.fragment:
        suggestion = host.removeprefix("*.").strip(".") or "example.com"
        raise ToolContractError(
            f'domains take site names such as example.com, not addresses with a path ("{item}"). '
            f'Pass {{"domains": ["{suggestion}"]}} and put words from the path in query.'
        )
    if not host:
        return item
    return mark + host.removeprefix("*.").lstrip(".")


def _domains(value: Any) -> Any:
    if isinstance(value, str):
        value = [part for part in re.split(r"[\s,;|]+", value) if part]
    if not isinstance(value, list):
        return value
    domains: list[Any] = []
    for item in value:
        domain = _domain(item) if isinstance(item, str) else item
        if domain == "":
            continue
        if domain not in domains:
            domains.append(domain)
    return domains or None


def _recency(value: Any) -> Any:
    if isinstance(value, str) and spelling(value) in _NO_RECENCY | {""}:
        return None
    return recency_token(value) or value


def _set_once(arguments: dict[str, Any], field: str, value: Any) -> None:
    if field in arguments and arguments[field] != value:
        raise ToolContractError(f"Conflicting values for {field}; provide one intended value.")
    arguments[field] = value


def _settle_result_age(arguments: dict[str, Any]) -> None:
    """Turn durations into ``days`` and refuse date filters web_search cannot apply."""
    if arguments.get("date_before") not in (None, ""):
        raise ToolContractError(_DATE_BEFORE_REFUSAL)
    arguments.pop("date_before", None)
    after = arguments.get("date_after")
    if isinstance(after, str) and parse_date(after) is None:
        token = recency_token(after)
        if token is None:
            raise ToolContractError(
                f'date_after must be a date such as 2026-09-01; received "{after}".'
            )
        del arguments["date_after"]
        _set_once(arguments, "recency", token)
    recency = arguments.get("recency")
    if isinstance(recency, str) and recency.endswith("d") and recency not in RECENCY_VALUES:
        with_days = re.fullmatch(r"\d+(?:\.\d+)?d", recency)
        if with_days:
            del arguments["recency"]
            _set_once(arguments, "days", float(recency[:-1]))


def _settle_domains(arguments: dict[str, Any]) -> None:
    """Move "-example.com" entries of domains into exclude_domains."""
    domains = arguments.get("domains")
    if not isinstance(domains, list):
        return
    excluded = [item[1:] for item in domains if isinstance(item, str) and item.startswith("-")]
    if not excluded:
        return
    kept = [item for item in domains if not (isinstance(item, str) and item.startswith("-"))]
    if kept:
        arguments["domains"] = kept
    else:
        del arguments["domains"]
    current = arguments.get("exclude_domains")
    merged = list(current) if isinstance(current, list) else []
    arguments["exclude_domains"] = merged + [item for item in excluded if item not in merged]


def _strip_exclusion_marks(arguments: dict[str, Any]) -> None:
    """A "-" before an entry of exclude_domains repeats what the field already says."""
    excluded = arguments.get("exclude_domains")
    if isinstance(excluded, list):
        cleaned: list[Any] = []
        for item in excluded:
            item = item.removeprefix("-") if isinstance(item, str) else item
            if item not in cleaned:
                cleaned.append(item)
        arguments["exclude_domains"] = cleaned


def normalize_web_search_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return canonical web_search arguments or raise with the reason and a fix."""
    repaired = normalize_call_arguments(
        contract,
        arguments,
        field_aliases=FIELD_ALIASES,
        field_normalizers={
            "query": _query,
            "domains": _domains,
            "exclude_domains": _domains,
            "recency": _recency,
        },
        empty_as_omitted=(
            "recency",
            "domains",
            "exclude_domains",
            "count",
            "page",
            "days",
            "date_after",
            "date_before",
        ),
    )
    if not isinstance(repaired, dict):
        return repaired
    _settle_result_age(repaired)
    _strip_exclusion_marks(repaired)
    _settle_domains(repaired)
    return repaired


__all__ = [
    "FIELD_ALIASES",
    "RECENCY_VALUES",
    "normalize_web_search_arguments",
    "parse_date",
    "recency_token",
]
