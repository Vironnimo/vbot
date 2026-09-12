"""Provider Tool probe: scenario history."""

from __future__ import annotations

import json
from typing import Any

from core.tools.history import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS
from core.tools.memory import MEMORY_TOOL_DESCRIPTION, MEMORY_TOOL_NAME, MEMORY_TOOL_PARAMETERS
from core.tools.session_search import (
    SESSION_READ_TOOL_DESCRIPTION,
    SESSION_READ_TOOL_NAME,
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
)
from core.tools.status import STATUS_TOOL_DESCRIPTION, STATUS_TOOL_NAME, STATUS_TOOL_PARAMETERS
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _history_scenario(case_name: str) -> ProbeScenario:
    cursor = "opaque-history-cursor"
    all_roles = [
        "system",
        "user",
        "assistant",
        "tool",
        "note",
        "error",
        "run_summary",
        "agent_takeover",
    ]
    history_arguments: dict[str, dict[str, Any]] = {
        "overview_default": {"action": "overview"},
        "overview_limit_min": {"action": "overview", "limit": 1},
        "overview_limit_max": {"action": "overview", "limit": 100},
        "overview_cursor": {"action": "overview", "cursor": cursor},
        "search_default": {"action": "search", "query": "deployment failure"},
        "search_checkpoint": {
            "action": "search",
            "query": "deployment failure",
            "checkpoint": 1,
        },
        "search_roles_one": {
            "action": "search",
            "query": "deployment failure",
            "roles": ["assistant"],
        },
        "search_roles_all": {
            "action": "search",
            "query": "deployment failure",
            "roles": all_roles,
        },
        "search_match_all_terms": {
            "action": "search",
            "query": "deployment failure",
            "match": "all_terms",
        },
        "search_match_phrase": {
            "action": "search",
            "query": "deployment failure",
            "match": "phrase",
        },
        "search_match_any_term": {
            "action": "search",
            "query": "deployment failure",
            "match": "any_term",
        },
        "search_limit": {
            "action": "search",
            "query": "deployment failure",
            "limit": 37,
        },
        "search_all": {
            "action": "search",
            "query": "deployment failure",
            "checkpoint": 2,
            "roles": ["user", "assistant", "error"],
            "match": "phrase",
            "limit": 37,
        },
        "search_cursor": {"action": "search", "cursor": cursor},
        "read_default": {"action": "read"},
        "read_checkpoint": {"action": "read", "checkpoint": 1},
        "read_roles_empty": {"action": "read", "roles": []},
        "read_roles": {"action": "read", "roles": ["tool", "note"]},
        "read_direction_start": {"action": "read", "direction": "start"},
        "read_direction_end": {"action": "read", "direction": "end"},
        "read_limit": {"action": "read", "limit": 42},
        "read_all": {
            "action": "read",
            "checkpoint": 3,
            "roles": ["system", "agent_takeover"],
            "direction": "end",
            "limit": 42,
        },
        "read_cursor": {"action": "read", "cursor": cursor},
        "around_default": {"action": "around", "message_id": "message-123"},
        "around_checkpoint": {
            "action": "around",
            "message_id": "message-123",
            "checkpoint": 1,
        },
        "around_roles": {
            "action": "around",
            "message_id": "message-123",
            "roles": ["user", "assistant"],
        },
        "around_before_zero": {
            "action": "around",
            "message_id": "message-123",
            "before": 0,
        },
        "around_before_max": {
            "action": "around",
            "message_id": "message-123",
            "before": 100,
        },
        "around_after_zero": {
            "action": "around",
            "message_id": "message-123",
            "after": 0,
        },
        "around_after_max": {
            "action": "around",
            "message_id": "message-123",
            "after": 100,
        },
        "around_all": {
            "action": "around",
            "message_id": "message-123",
            "checkpoint": 4,
            "roles": ["assistant", "error"],
            "before": 17,
            "after": 23,
        },
        "around_cursor": {"action": "around", "cursor": cursor},
    }
    expected_arguments = history_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HISTORY_TOOL_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every value and array item; do not add any field."
    )
    return ProbeScenario(
        "history",
        [
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "parameters": HISTORY_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HISTORY_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


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
            "entry_id": 2,
            "content": "Prefers direct, concise answers.",
        },
        "replace_agent": {
            "action": "replace",
            "scope": "agent",
            "entry_id": 2,
            "content": "Workspace uses PowerShell 7.",
        },
        "remove_user": {"action": "remove", "scope": "user", "entry_id": 2},
        "remove_agent": {"action": "remove", "scope": "agent", "entry_id": 2},
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


def _session_read_scenario(case_name: str) -> ProbeScenario:
    session_id = "session-123"
    agent_id = "tester"
    message_id = "message-123"
    continuation = "r1:1234:" + ("a" * 64)
    session_read_arguments: dict[str, dict[str, Any]] = {
        "whole": {"session_id": session_id},
        "message": {"session_id": session_id, "message_id": message_id},
        "agent": {"session_id": session_id, "agent_id": agent_id},
        "continuation": {
            "session_id": session_id,
            "message_id": message_id,
            "continuation": continuation,
        },
        "all_messages": {"session_id": session_id, "all_messages": True},
        "all": {
            "session_id": session_id,
            "agent_id": agent_id,
            "all_messages": True,
            "continuation": continuation,
        },
    }
    expected_arguments = session_read_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {SESSION_READ_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "session_read",
        [
            {
                "name": SESSION_READ_TOOL_NAME,
                "description": SESSION_READ_TOOL_DESCRIPTION,
                "parameters": SESSION_READ_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SESSION_READ_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _session_search_scenario(case_name: str) -> ProbeScenario:
    query = "Tool schema defaults"
    session_id = "session-123"
    session_search_arguments: dict[str, dict[str, Any]] = {
        "list": {},
        "query": {"query": query},
        "period": {"period": "2026-07-01/2026-07-31"},
        "agent": {"agent_id": "tester"},
        "session": {"query": query, "session_id": session_id},
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
