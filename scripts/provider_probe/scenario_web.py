"""Provider Tool probe: scenario web."""

from __future__ import annotations

import json
from typing import Any

from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
)
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
)
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _web_fetch_scenario(case_name: str) -> ProbeScenario:
    url = "https://example.com/provider-tool-probe"
    web_fetch_arguments: dict[str, dict[str, Any]] = {
        "default": {"url": url},
        "markdown": {"url": url, "output": "markdown"},
        "text": {"url": url, "output": "text"},
        "raw": {"url": url, "output": "raw"},
    }
    expected_arguments = web_fetch_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {WEB_FETCH_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "web_fetch",
        [
            {
                "name": WEB_FETCH_TOOL_NAME,
                "description": WEB_FETCH_TOOL_DESCRIPTION,
                "parameters": WEB_FETCH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WEB_FETCH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _web_search_scenario(case_name: str) -> ProbeScenario:
    query = "vBot Tool schemas"
    web_search_arguments: dict[str, dict[str, Any]] = {
        "default": {"query": query},
        "operator_query": {"query": 'vBot "Tool schema" -deprecated'},
        "domains_one": {"query": query, "domains": ["openai.com"]},
        "domains_many": {
            "query": query,
            "domains": ["docs.python.org", "openai.com"],
        },
        "count_min": {"query": query, "count": 1},
        "count_max": {"query": query, "count": 20},
        "page_first": {"query": query, "page": 1},
        "page_later": {"query": query, "page": 3},
        "recency_day": {"query": query, "recency": "day"},
        "recency_month": {"query": query, "recency": "month"},
        "recency_year": {"query": query, "recency": "year"},
        "all": {
            "query": query,
            "domains": ["docs.python.org", "openai.com"],
            "count": 20,
            "page": 3,
            "recency": "month",
        },
    }
    expected_arguments = web_search_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {WEB_SEARCH_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "web_search",
        [
            {
                "name": WEB_SEARCH_TOOL_NAME,
                "description": WEB_SEARCH_TOOL_DESCRIPTION,
                "parameters": WEB_SEARCH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WEB_SEARCH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
