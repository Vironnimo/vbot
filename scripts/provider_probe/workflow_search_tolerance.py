"""Web search probes that inspect the actual provider request and returned results."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import respx

from core.tools import ToolContext, ToolRegistry, tool_failure
from core.tools.web_search import register_web_search_tool
from scripts.provider_probe.choices import WEB_SEARCH_CASES
from scripts.provider_probe.scenario_web import _web_search_scenario


def search_tolerance_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"id": name, "arguments": _web_search_scenario(name).expected_arguments}
        for name in WEB_SEARCH_CASES
    ]
    for value, expected in (
        ("DAY", "day"),
        ("week", "week"),
        ("month", "month"),
        ("year", "year"),
        ("pd", "day"),
        ("pw", "week"),
        ("pm", "month"),
        ("py", "year"),
    ):
        cases.append(
            {
                "id": "freshness_" + value,
                "arguments": {"query": "fixture", "freshness": value},
                "recency": expected,
            }
        )
    cases.extend(
        [
            {
                "id": "matching",
                "arguments": {"query": "fixture", "freshness": "pd", "recency": "day"},
            },
            {
                "id": "conflict",
                "arguments": {"query": "fixture", "freshness": "day", "recency": "year"},
                "success": False,
            },
            {
                "id": "unsupported",
                "arguments": {"query": "fixture", "freshness": "decade"},
                "success": False,
            },
        ]
    )
    return cases


async def search_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    registry = ToolRegistry()
    register_web_search_tool(registry, lambda _: "fixture", lambda: {"default_count": 3})
    raw = await adapter.send(
        [
            {
                "role": "system",
                "content": "Make one diagnostic Tool Call with all supplied fields. "
                "Omit every field that is not supplied. If freshness appears, preserve it "
                "even though recency is preferred. Preserve contradictory "
                "and unsupported values so the Tool can return its own error.",
            },
            {"role": "user", "content": "Arguments: " + json.dumps(case["arguments"])},
        ],
        tools=registry.provider_definitions(),
        model_id=args.model,
        thinking_effort=args.thinking_effort,
        max_tokens=args.max_tokens or 2500,
    )
    calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
    received = []

    def receive(request: httpx.Request) -> httpx.Response:
        received.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Fixture " + str(i),
                            "url": "https://openai.com/fixture/" + str(i),
                            "description": "Fixture result",
                        }
                        for i in range(20)
                    ]
                }
            },
        )

    results = []
    with (
        TemporaryDirectory(prefix="vbot-search-tolerance-") as temporary,
        respx.mock(assert_all_called=False) as router,
    ):
        router.get("https://api.search.brave.com/res/v1/web/search").mock(side_effect=receive)
        root = Path(temporary)
        for call in calls:
            context = ToolContext(
                agent_id="probe",
                session_id="probe",
                run_id="probe",
                tool_call_id=call["id"],
                tool_name=call["name"],
                tool_call_index=0,
                workspace=root,
                vbot_root=root,
                data_root=root,
            )
            try:
                results.append(await registry.dispatch(context, call["arguments"], ["web_search"]))
            except ValueError as error:
                results.append(tool_failure("invalid_arguments", str(error)))
    checks = {"one_call": len(calls) == len(results) == 1}
    if case.get("success", True):
        request = case["arguments"]
        expected_age = case.get("recency", request.get("recency", ""))
        expected_count = request.get("count", 3)
        params = received[0] if received else {}
        checks["query"] = request["query"] in params.get("q", "")
        checks["domains"] = all(
            "site:" + d in params.get("q", "") for d in request.get("domains", [])
        )
        checks["count"] = params.get("count") == str(expected_count)
        checks["page"] = int(params.get("offset", "0")) == request.get("page", 1) - 1
        checks["age"] = (
            params.get("freshness", "")
            == {"day": "pd", "week": "pw", "month": "pm", "year": "py", "": ""}[expected_age]
        )
        data = results[0].get("data") or {} if results else {}
        numbered = re.findall(r"^\d+\. ", str(data.get("content", "")), re.MULTILINE)
        checks["results"] = len(numbered) == expected_count
        checks["returned_age"] = data.get("recency", "") == expected_age
    else:
        checks["rejected_without_fetch"] = bool(results) and not results[0]["ok"] and not received
    return {
        "case": case["id"],
        "passed": all(checks.values()),
        "checks": checks,
        "observed": calls,
        "results": results,
        "received": received,
    }
