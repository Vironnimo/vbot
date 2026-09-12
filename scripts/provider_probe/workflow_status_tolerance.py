"""Status probes with production dispatch and disposable Session storage."""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock

from core.agents.agents import Agent
from core.models.models import ModelRegistry
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager
from core.sessions.format import write_bootstrap_marker
from core.tools import ToolAccess, ToolContext, ToolRegistry, tool_failure
from core.tools.status import register_status_tool


def status_tolerance_cases() -> list[dict[str, Any]]:
    return [
        {"id": "current", "arguments": {}},
        {"id": "session", "arguments": {"session_id": "other"}, "session": "other"},
        {
            "id": "agent_session",
            "arguments": {"agent_id": "reviewer", "session_id": "other"},
            "agent": "reviewer",
            "session": "other",
        },
        {"id": "own_agent", "arguments": {"agent_id": "probe"}},
        {"id": "wrapper", "arguments": {"current": {}}},
        {"id": "request", "arguments": {"request": {"operation": " CURRENT "}}},
        {"id": "action", "arguments": {"action": "current"}},
        {"id": "missing_session", "arguments": {"agent_id": "reviewer"}, "success": False},
        {"id": "unknown_action", "arguments": {"action": "delete"}, "success": False},
    ]


async def status_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-status-tolerance-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        write_bootstrap_marker(root)
        sessions = stack.enter_context(closing(ChatSessionManager(root)))
        for agent_id, session_id in (
            ("probe", "current"),
            ("probe", "other"),
            ("reviewer", "other"),
        ):
            sessions.create(agent_id, session_id=session_id)
        resolver = Mock()
        resolver.resolve_agent.side_effect = lambda project, agent_id: Agent(
            id=agent_id,
            name=agent_id,
            model="openai/fixture",
            fallback_models=[],
            workspace=str(root),
            temperature=0.3,
            thinking_effort="none",
            tool_access=ToolAccess(mode="all"),
            allowed_skills=[],
            tools={},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        registry = ToolRegistry()
        register_status_tool(
            registry, resolver, sessions, ModelRegistry({}), ChatRunManager(), None
        )
        raw = await adapter.send(
            [
                {
                    "role": "system",
                    "content": "Make one diagnostic Tool Call with all supplied "
                    "fields, including action and older wrappers. If action is delete, "
                    "include action: delete in the Tool arguments to test its error response; "
                    "do not substitute agent_id or omit action. Do not infer missing targets. "
                    "Your Agent is probe and your Session is current.",
                },
                {"role": "user", "content": "Arguments: " + json.dumps(case["arguments"])},
            ],
            tools=registry.provider_definitions(),
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 2500,
        )
        calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
        results = []
        for call in calls:
            context = ToolContext(
                agent_id="probe",
                session_id="current",
                run_id="probe",
                tool_call_id=call["id"],
                tool_name=call["name"],
                tool_call_index=0,
                workspace=root,
                vbot_root=root,
                data_root=root,
            )
            try:
                results.append(await registry.dispatch(context, call["arguments"], ["status"]))
            except ValueError as error:
                results.append(tool_failure("invalid_arguments", str(error)))
        checks = {"one_call": len(calls) == len(results) == 1}
        if case.get("success", True):
            data = results[0].get("data") or {} if results else {}
            checks["target"] = (data.get("agent_id"), data.get("session_id")) == (
                case.get("agent", "probe"),
                case.get("session", "current"),
            )
            checks["report"] = bool(data.get("text"))
        else:
            checks["rejected_before_lookup"] = (
                bool(results) and not results[0]["ok"] and (resolver.resolve_agent.call_count == 0)
            )
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": calls,
            "results": results,
        }
