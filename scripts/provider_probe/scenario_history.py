"""Provider Tool probe: scenario history."""

from __future__ import annotations

import json
from typing import Any

from core.tools.memory import MEMORY_TOOL_DESCRIPTION, MEMORY_TOOL_NAME, MEMORY_TOOL_PARAMETERS
from core.tools.session_search import (
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
)
from core.tools.status import STATUS_TOOL_DESCRIPTION, STATUS_TOOL_NAME, STATUS_TOOL_PARAMETERS
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _memory_scenario(case_name: str) -> ProbeScenario:
    memory_arguments: dict[str, dict[str, Any]] = {
        "list_user": {"action": "list", "scope": "user"},
        "list_agent": {"action": "list", "scope": "agent"},
        "add_user": {
            "action": "add",
            "scope": "user",
            "content": "Prefers concise answers.",
        },
        "add_agent": {
            "action": "add",
            "scope": "agent",
            "content": "Workspace uses PowerShell.",
        },
        "replace_user": {
            "action": "replace",
            "scope": "user",
            "old_text": "concise answers",
            "content": "Prefers direct, concise answers.",
        },
        "replace_agent": {
            "action": "replace",
            "scope": "agent",
            "old_text": "PowerShell",
            "content": "Workspace uses PowerShell 7.",
        },
        "remove_user": {"action": "remove", "scope": "user", "old_text": "concise answers"},
        "remove_agent": {"action": "remove", "scope": "agent", "old_text": "PowerShell"},
    }
    expected_arguments = memory_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {MEMORY_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "memory",
        [
            {
                "name": MEMORY_TOOL_NAME,
                "description": MEMORY_TOOL_DESCRIPTION,
                "parameters": MEMORY_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        MEMORY_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _status_scenario(case_name: str) -> ProbeScenario:
    status_arguments = {
        "current": {},
        "session": {"session_id": "session-123"},
        "agent_session": {"session_id": "session-123", "agent_id": "tester"},
    }
    expected_arguments = status_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {STATUS_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "status",
        [
            {
                "name": STATUS_TOOL_NAME,
                "description": STATUS_TOOL_DESCRIPTION,
                "parameters": STATUS_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        STATUS_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _session_search_scenario(case_name: str) -> ProbeScenario:
    query = "Tool schema defaults"
    session_id = "session-123"
    session_search_arguments: dict[str, dict[str, Any]] = {
        "query": {"query": query},
        "period": {"query": query, "period": "2026-07-01/2026-07-31"},
        "agent": {"query": query, "agent_id": "tester"},
        "session": {"query": query, "session_id": session_id},
        "subagents": {"query": query, "include_subagents": True},
        "exclude_subagents": {"query": query, "include_subagents": False},
        "open_period": {"query": query, "period": "2026-07-01/"},
        "all": {
            "query": query,
            "period": "2026-07-01/2026-07-31",
            "agent_id": "tester",
            "session_id": session_id,
        },
    }
    expected_arguments = session_search_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {SESSION_SEARCH_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "session_search",
        [
            {
                "name": SESSION_SEARCH_TOOL_NAME,
                "description": SESSION_SEARCH_TOOL_DESCRIPTION,
                "parameters": SESSION_SEARCH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SESSION_SEARCH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
