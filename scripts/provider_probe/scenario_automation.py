"""Provider Tool probe: scenario automation."""

from __future__ import annotations

import json
from typing import Any

from core.tools.calendar import (
    CALENDAR_TOOL_DESCRIPTION,
    CALENDAR_TOOL_NAME,
    CALENDAR_TOOL_PARAMETERS,
)
from core.tools.cron import CRON_TOOL_DESCRIPTION, CRON_TOOL_NAME, CRON_TOOL_PARAMETERS
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _calendar_scenario(case_name: str) -> ProbeScenario:
    calendar_arguments: dict[str, dict[str, Any]] = {
        "add_action_default": {
            "action": "add_action",
            "id": "event-123",
            "when": "start - 1h",
            "prompt": "Prepare the meeting notes.",
        },
        "add_action_target": {
            "action": "add_action",
            "id": "event-123",
            "when": "end + 30m",
            "prompt": "Review the meeting.",
            "target": "builder@project",
        },
        "add_action_session": {
            "action": "add_action",
            "id": "event-123",
            "when": "start",
            "prompt": "Start the meeting.",
            "session": "session-123",
        },
        "add_action_at_end": {
            "action": "add_action",
            "id": "event-123",
            "when": "end",
            "prompt": "Review the meeting.",
        },
        "update_action_time": {
            "action": "update_action",
            "id": "action-123",
            "when": "start - 30m",
        },
        "update_action_prompt": {
            "action": "update_action",
            "id": "action-123",
            "prompt": "Prepare the updated notes.",
        },
        "update_action_target": {
            "action": "update_action",
            "id": "action-123",
            "target": "builder@project",
        },
        "update_action_session": {
            "action": "update_action",
            "id": "action-123",
            "session": "session-123",
        },
        "delete_action": {"action": "delete_action", "id": "action-123"},
        "list_default": {"action": "list"},
        "list_when_week": {"action": "list", "when": "this week"},
        "list_when_range": {"action": "list", "when": "2026-09-10..2026-09-14"},
        "create_timed": {
            "action": "create",
            "title": "Dentist",
            "start": "2026-09-10T15:00",
        },
        "create_timed_full": {
            "action": "create",
            "title": "Dentist",
            "start": "2026-09-10T15:00",
            "duration": 45,
            "notes": "Bring the insurance card.",
        },
        "create_allday": {
            "action": "create",
            "title": "Trip",
            "start": "2026-09-14",
            "duration": 3,
        },
        "create_recurring": {
            "action": "create",
            "title": "Standup",
            "start": "2026-08-31T09:00",
            "rrule": {"freq": "weekly", "by_weekday": ["mo", "we"]},
        },
        "create_recurring_count": {
            "action": "create",
            "title": "Focus block",
            "start": "2026-09-01T09:00",
            "rrule": {"freq": "daily", "count": 10},
        },
        "update_fields": {
            "action": "update",
            "id": "event-123",
            "title": "Dentist moved",
            "start": "2026-09-10T16:00",
        },
        "update_stop_repeat": {
            "action": "update",
            "id": "event-123",
            "rrule": None,
        },
        "update_notes": {
            "action": "update",
            "id": "event-123",
            "notes": "Rescheduled by the practice.",
        },
        "delete_whole": {"action": "delete", "id": "event-123"},
        "delete_occurrence": {
            "action": "delete",
            "id": "event-123",
            "start": "2026-09-14T09:00:00",
        },
        "find_free_default": {"action": "find_free"},
        "find_free_when": {
            "action": "find_free",
            "when": "next week",
            "duration": 60,
        },
    }
    expected_arguments = calendar_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {CALENDAR_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "calendar",
        [
            {
                "name": CALENDAR_TOOL_NAME,
                "description": CALENDAR_TOOL_DESCRIPTION,
                "parameters": CALENDAR_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        CALENDAR_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _cron_scenario(case_name: str) -> ProbeScenario:
    cron_arguments: dict[str, dict[str, Any]] = {
        "create_cron": {
            "action": "create",
            "prompt": "Prepare the daily operations summary.",
            "schedule": "0 9 * * *",
        },
        "create_interval": {
            "action": "create",
            "prompt": "Check the service health.",
            "schedule": "every 2h",
        },
        "create_once_relative": {
            "action": "create",
            "prompt": "Send the deployment reminder.",
            "schedule": "in 30m",
            "repeat": 1,
        },
        "create_once_iso": {
            "action": "create",
            "prompt": "Send the deployment reminder.",
            "schedule": "2026-08-01T09:00:00+02:00",
        },
        "create_target": {
            "action": "create",
            "target": "reviewer@vbot",
            "prompt": "Review the current release state.",
            "schedule": "0 10 * * 1",
        },
        "create_name": {
            "action": "create",
            "name": "Daily operations summary",
            "prompt": "Prepare the daily operations summary.",
            "schedule": "0 9 * * *",
        },
        "create_repeat": {
            "action": "create",
            "prompt": "Check the service health.",
            "schedule": "every 2h",
            "repeat": 3,
        },
        "create_repeat_null": {
            "action": "create",
            "prompt": "Check the service health.",
            "schedule": "every 2h",
            "repeat": None,
        },
        "create_all": {
            "action": "create",
            "target": "reviewer@vbot",
            "name": "Release review",
            "prompt": "Review the current release state.",
            "schedule": "0 10 * * 1",
            "repeat": 6,
        },
        "list": {"action": "list"},
        "update_target": {
            "action": "update",
            "id": "job-123",
            "target": "reviewer@vbot",
        },
        "update_name": {
            "action": "update",
            "id": "job-123",
            "name": "Updated operations summary",
        },
        "update_prompt": {
            "action": "update",
            "id": "job-123",
            "prompt": "Prepare the revised operations summary.",
        },
        "update_schedule": {
            "action": "update",
            "id": "job-123",
            "schedule": "every 4h",
        },
        "update_repeat": {
            "action": "update",
            "id": "job-123",
            "repeat": 4,
        },
        "update_repeat_null": {
            "action": "update",
            "id": "job-123",
            "repeat": None,
        },
        "update_all": {
            "action": "update",
            "id": "job-123",
            "target": "reviewer@vbot",
            "name": "Updated release review",
            "prompt": "Review the revised release state.",
            "schedule": "0 11 * * 1",
            "repeat": 8,
        },
        "delete": {"action": "delete", "id": "job-123"},
        "enable": {"action": "enable", "id": "job-123"},
        "disable": {"action": "disable", "id": "job-123"},
    }
    expected_arguments = cron_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {CRON_TOOL_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "cron",
        [
            {
                "name": CRON_TOOL_NAME,
                "description": CRON_TOOL_DESCRIPTION,
                "parameters": CRON_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        CRON_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
