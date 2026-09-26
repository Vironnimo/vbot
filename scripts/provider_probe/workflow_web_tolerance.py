"""Web retrieval probes through production dispatch with isolated HTTP responses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, patch

from core.tools import ToolContext, ToolRegistry, tool_failure
from core.tools._public_http import PublicResponse
from core.tools.web_fetch import register_web_fetch_tool


def web_tolerance_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"id": mode or "default", "arguments": {"output": mode} if mode else {}, "mode": mode}
        for mode in (None, "markdown", "text", "raw")
    ]
    cases.extend(
        [
            {"id": "raw_true", "arguments": {"raw": "TRUE"}, "mode": "raw"},
            {"id": "raw_false", "arguments": {"raw": "false"}, "mode": "markdown"},
            {"id": "links_true", "arguments": {"include-links": "yes"}, "mode": "markdown"},
            {"id": "links_false", "arguments": {"include_links": "false"}, "mode": "text"},
            {"id": "matching", "arguments": {"output": "markdown", "include_links": True}},
            {
                "id": "raw_links",
                "arguments": {"raw": True, "include_links": False},
                "success": False,
            },
            {
                "id": "raw_links_true",
                "arguments": {"raw": True, "include_links": True},
                "mode": "raw",
            },
            {
                "id": "raw_output_links_false",
                "arguments": {"output": "raw", "include_links": False},
                "success": False,
            },
            {"id": "raw_conflict", "arguments": {"raw": True, "output": "text"}, "success": False},
            {
                "id": "links_conflict",
                "arguments": {"include_links": True, "output": "text"},
                "success": False,
            },
            {"id": "invalid_boolean", "arguments": {"raw": "maybe"}, "success": False},
        ]
    )
    for case in cases:
        case["arguments"]["url"] = "https://example.com/fixture"
    return cases


async def web_case(adapter: Any, args: argparse.Namespace, case: dict[str, Any]) -> dict[str, Any]:
    registry = ToolRegistry()
    register_web_fetch_tool(registry, attachment_store=None)
    raw = await adapter.send(
        [
            {
                "role": "system",
                "content": "Make one diagnostic Tool Call with the supplied "
                "arguments. Preserve all fields and conflicting values, including raw and "
                "include_links even though the preferred schema only advertises output.",
            },
            {"role": "user", "content": "Arguments: " + json.dumps(case["arguments"])},
        ],
        tools=registry.provider_definitions(),
        model_id=args.model,
        thinking_effort=args.thinking_effort,
        max_tokens=args.max_tokens or 2500,
    )
    calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
    html = '<html><body><p>Fixture article <a href="https://example.org/link">source</a>.</p></body></html>'
    fetch = AsyncMock(
        return_value=PublicResponse(
            200, {"content-type": "text/html"}, html, "https://example.com/fixture", html.encode()
        )
    )
    results = []
    with (
        TemporaryDirectory(prefix="vbot-web-tolerance-") as temporary,
        patch("core.tools._public_http._fetch_with_retry", fetch),
        patch("core.tools._public_http._make_session", return_value=AsyncMock()),
    ):
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
                results.append(await registry.dispatch(context, call["arguments"], ["web_fetch"]))
            except ValueError as error:
                results.append(tool_failure("invalid_arguments", str(error)))
    checks = {"one_call": len(calls) == len(results) == 1}
    if case.get("success", True):
        content = (results[0].get("data") or {}).get("content", "") if results else ""
        mode = case.get("mode") or "markdown"
        checks["content"] = "Fixture article" in content
        checks["raw"] = ("<html>" in content) == (mode == "raw")
        checks["links"] = ("https://example.org/link" in content) == (mode != "text")
        checks["destination"] = (
            fetch.await_count == 1
            and fetch.await_args is not None
            and fetch.await_args.args[1] == ("https://example.com/fixture")
        )
    else:
        checks["rejected_without_fetch"] = (
            bool(results) and not results[0]["ok"] and (fetch.await_count == 0)
        )
    return {
        "case": case["id"],
        "passed": all(checks.values()),
        "checks": checks,
        "observed": calls,
        "results": results,
    }
