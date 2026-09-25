"""The cron Tool against a real CronService: canonical calls, results, and errors."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

import core.tools.cron as cron_tool_module
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.tools.cron import CRON_TOOL_NAME, CRON_TOOL_PARAMETERS

from .cron_tool_support import cron_tool

PROMPT = "Check the nightly build and summarize failures."
BERLIN_TIME = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0[12]:00"


def _error(envelope: dict[str, Any]) -> dict[str, Any]:
    assert envelope["ok"] is False
    return cast(dict[str, Any], envelope["error"])


def test_schema_exposes_flat_action_contract() -> None:
    assert CRON_TOOL_PARAMETERS["type"] == "object"
    assert "oneOf" not in CRON_TOOL_PARAMETERS
    assert "additionalProperties" not in CRON_TOOL_PARAMETERS
    properties = cast(dict[str, Any], CRON_TOOL_PARAMETERS["properties"])
    # Accepted time zone and paused-state spellings are never advertised.
    assert list(properties) == ["action", "id", "target", "name", "prompt", "schedule", "repeat"]
    assert properties["action"]["enum"] == [
        "create",
        "list",
        "update",
        "delete",
        "enable",
        "disable",
    ]
    assert CRON_TOOL_PARAMETERS["required"] == ["action"]
    assert properties["repeat"]["type"] == ["integer", "null"]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )


def test_create_returns_the_job_without_echoing_the_prompt(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    envelope, text = tool.call(
        {"action": "create", "name": "Build check", "prompt": PROMPT, "schedule": "0 9 * * *"}
    )

    assert envelope["ok"] is True
    job = tool.only_job()
    assert (job.agent_id, job.project_id, job.status) == ("agent-one", None, "active")
    assert (job.schedule_type, job.cron_expression, job.remaining_runs) == (
        "cron",
        "0 9 * * *",
        None,
    )
    assert job.prompt == PROMPT
    assert text.splitlines()[:4] == [
        f"id: {job.id}",
        "name: Build check",
        "status: active",
        "schedule: 0 9 * * *",
    ]
    assert re.search(r"^next_run: \d{4}-\d\d-\d\dT09:00:00\+0[12]:00$", text, re.MULTILINE)
    assert "target: agent-one" in text
    assert PROMPT not in text
    assert "repeat" not in text
    tool.trigger.trigger_run.assert_not_called()


def test_create_defaults_to_current_agent_and_project(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    _envelope, text = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *"}, project_id="vbot"
    )

    job = tool.only_job()
    assert (job.agent_id, job.project_id) == ("agent-one", "vbot")
    assert "target: agent-one@vbot" in text


def test_create_derives_the_name_from_the_prompt(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    _envelope, text = tool.call(
        {"action": "create", "prompt": "Run this later", "schedule": "*/5 * * * *"}
    )

    assert tool.only_job().name == "Run this later"
    assert "name: Run this later" in text


def test_limited_interval_shows_the_remaining_fires(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    _envelope, text = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "every 2h", "repeat": 3}
    )

    job = tool.only_job()
    assert (job.schedule_type, job.interval_seconds, job.remaining_runs) == ("interval", 7200, 3)
    assert "schedule: every 2h" in text
    assert "repeat: 3" in text


def test_one_time_job_shows_its_local_time_once(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    _envelope, text = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "2030-01-01T09:00"}
    )

    job = tool.only_job()
    assert (job.schedule_type, job.remaining_runs) == ("once", 1)
    assert "schedule: 2030-01-01T09:00:00+01:00" in text
    # The time is the next run; it is not repeated.
    assert "next_run" not in text
    assert "repeat" not in text


@pytest.mark.parametrize("repeat", [2, None])
def test_one_time_schedule_refuses_another_repeat(tmp_path: Path, repeat: int | None) -> None:
    tool = cron_tool(tmp_path)

    envelope, _text = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "in 30m", "repeat": repeat}
    )

    error = _error(envelope)
    assert error["code"] == "invalid_arguments"
    assert "fires once" in error["message"]
    assert '"repeat":1' in error["message"]
    assert "retryable" not in error
    assert tool.jobs() == []


def test_list_shows_every_job_as_a_readable_block(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    tool.call(
        {
            "action": "create",
            "name": "Digest",
            "prompt": "Write the digest.\nKeep it short.",
            "schedule": "0 8 * * *",
        }
    )
    paused, _ = tool.call(
        {"action": "create", "name": "Paused", "prompt": PROMPT, "schedule": "every 2h"}
    )
    paused_id = paused["data"]["id"]
    tool.call({"action": "disable", "id": paused_id})
    failed = tool.service._jobs[paused_id]
    failed.status = "failed"
    failed.last_outcome = "failed"
    failed.last_error = "Model unavailable"
    failed.consecutive_failures = 5
    failed.last_fired_at = "2026-01-05T08:00:00+00:00"

    envelope, text = tool.call({"action": "list"})

    assert envelope["data"]["jobs"] == 2
    header, digest, stopped = text.split("\n\n")
    assert header == "jobs: 2\ntimezone: Europe/Berlin"
    assert "name: Digest\nstatus: active\nschedule: 0 8 * * *\n" in digest
    assert digest.endswith("prompt: Write the digest.\n  Keep it short.")
    assert "status: failed" in stopped
    assert "next_run" not in stopped
    assert "last_run: 2026-01-05T09:00:00+01:00" in stopped
    assert "last_outcome: failed\nlast_error: Model unavailable\nfailures_in_a_row: 5" in stopped
    assert (
        f'note: stopped after failed runs; {{"action":"enable","id":"{paused_id}"}} restarts it.'
        in stopped
    )
    display = tool.registry.display_for_call(CRON_TOOL_NAME, {"action": "list"}, result=envelope)
    assert display["facts"] == [{"kind": "count", "value": 2, "unit": "results", "at_least": False}]


def test_list_with_an_id_shows_that_job(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    first, _ = tool.call(
        {"action": "create", "name": "First", "prompt": PROMPT, "schedule": "every 2h"}
    )
    tool.call({"action": "create", "name": "Second", "prompt": PROMPT, "schedule": "every 3h"})

    _envelope, text = tool.call({"action": "list", "id": first["data"]["id"]})

    assert "jobs: 1" in text
    assert "name: First" in text
    assert "Second" not in text


def test_empty_list_has_no_body(tmp_path: Path) -> None:
    _envelope, text = cron_tool(tmp_path).call({"action": "list"})

    assert text == "jobs: 0\ntimezone: Europe/Berlin"


def test_update_changes_only_the_named_fields(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "every 2h", "repeat": 3}
    )
    job_id = created["data"]["id"]

    _envelope, text = tool.call(
        {
            "action": "update",
            "id": job_id,
            "name": "Renamed",
            "prompt": "New prompt",
            "schedule": "every 4h",
        }
    )

    job = tool.only_job()
    assert (job.name, job.prompt, job.interval_seconds, job.remaining_runs) == (
        "Renamed",
        "New prompt",
        14400,
        3,
    )
    assert "schedule: every 4h" in text
    assert "repeat: 3" in text


def test_update_with_null_repeat_removes_the_limit(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "every 2h", "repeat": 3}
    )

    tool.call({"action": "update", "id": created["data"]["id"], "repeat": None})

    assert tool.only_job().remaining_runs is None


def test_update_to_a_one_time_schedule_fires_once(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 1d"})

    envelope, text = tool.call(
        {"action": "update", "id": created["data"]["id"], "schedule": "in 2d"}
    )

    assert envelope["ok"] is True
    job = tool.only_job()
    assert (job.schedule_type, job.remaining_runs, job.status) == ("once", 1, "active")
    assert re.search(rf"^schedule: {BERLIN_TIME}$", text, re.MULTILINE)


def test_update_to_a_one_time_schedule_refuses_null_repeat(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 1d"})

    envelope, _text = tool.call(
        {"action": "update", "id": created["data"]["id"], "schedule": "in 30m", "repeat": None}
    )

    assert '"repeat":1' in _error(envelope)["message"]
    assert tool.only_job().schedule_type == "interval"


def test_update_needs_a_change(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 2h"})
    job_id = created["data"]["id"]

    envelope, _text = tool.call({"action": "update", "id": job_id})

    message = _error(envelope)["message"]
    assert message.startswith("cron was not run: update needs a field to change")
    assert f'{{"action":"update","id":"{job_id}","schedule":"every 4h"}}' in message


def test_delete_enable_disable_and_paused_update(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call(
        {"action": "create", "name": "Queue", "prompt": PROMPT, "schedule": "every 2h"}
    )
    job_id = created["data"]["id"]

    _envelope, disabled = tool.call({"action": "disable", "id": job_id})
    assert tool.only_job().status == "paused"
    assert "status: paused" in disabled
    assert "next_run" not in disabled
    _envelope, enabled = tool.call({"action": "enable", "id": job_id})
    assert tool.only_job().status == "active"
    assert "status: active" in enabled
    tool.call({"action": "update", "id": job_id, "enabled": False})
    assert tool.only_job().status == "paused"
    _envelope, deleted = tool.call({"action": "delete", "id": job_id})
    assert deleted == f"id: {job_id}\nname: Queue\nstatus: deleted"
    assert tool.jobs() == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "update", "id": "missing", "name": "x"},
        {"action": "delete", "id": "missing"},
        {"action": "enable", "id": "missing"},
        {"action": "disable", "id": "missing"},
        {"action": "list", "id": "missing"},
    ],
)
def test_unknown_id_names_the_list_call(tmp_path: Path, arguments: dict[str, Any]) -> None:
    envelope, _text = cron_tool(tmp_path).call(arguments)

    error = _error(envelope)
    assert error["code"] == "job_not_found"
    assert error["message"] == (
        'No job has id "missing". {"action":"list"} shows the current jobs and their ids.'
    )


@pytest.mark.parametrize("action", ["update", "delete", "enable", "disable"])
def test_missing_id_names_the_list_call(tmp_path: Path, action: str) -> None:
    envelope, _text = cron_tool(tmp_path).call({"action": action})

    message = _error(envelope)["message"]
    assert message.startswith(f'cron was not run: {action} needs the job "id"')
    assert '{"action":"list"}' in message


def test_past_one_time_schedule_asks_for_a_future_time(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    past = "2020-01-01T09:00:00"

    created, _ = tool.call({"action": "create", "prompt": "Remind me", "schedule": past})
    job, _ = tool.call({"action": "create", "prompt": "Remind me", "schedule": "in 30m"})
    job_id = job["data"]["id"]
    updated, _ = tool.call({"action": "update", "id": job_id, "schedule": past})

    for envelope in (created, updated):
        message = _error(envelope)["message"]
        assert "Choose a future time" in message
        assert '"schedule":"in 30m"' in message
    assert [stored.id for stored in tool.jobs()] == [job_id]
    tool.trigger.trigger_run.assert_not_called()


def test_finished_job_cannot_resume(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 2h"})
    job_id = created["data"]["id"]
    tool.service._jobs[job_id].status = "completed"

    envelope, _text = tool.call({"action": "enable", "id": job_id})

    message = _error(envelope)["message"]
    assert "The job has finished; create a new job instead" in message
    assert f'{{"action":"delete","id":"{job_id}"}}' in message


def test_invalid_schedule_lists_the_forms(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    envelope, _text = tool.call({"action": "create", "prompt": PROMPT, "schedule": "whenever"})

    message = _error(envelope)["message"]
    assert message.startswith('cron was not run: schedule "whenever" is not valid')
    assert '"0 9 * * 1-5"' in message and '"every 2h"' in message and '"in 30m"' in message
    assert tool.jobs() == []


def test_invalid_cron_fields_are_refused(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    envelope, _text = tool.call({"action": "create", "prompt": PROMPT, "schedule": "61 9 * * *"})

    assert "is not a valid five-field cron expression" in _error(envelope)["message"]
    assert tool.jobs() == []


def test_invalid_action_is_a_contract_failure(tmp_path: Path) -> None:
    envelope, _text = cron_tool(tmp_path).call({"action": "invalid"})

    message = _error(envelope)["message"]
    assert '"action" must be one of "create", "list"' in message
    assert 'received "invalid"' in message


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize(
    ("resolver_error", "code", "reason", "recommendation"),
    [
        (
            ResolutionAgentNotFoundError("agent 'ghost' is not on project 'vbot' team"),
            "agent_not_found",
            None,
            cron_tool_module._TARGET_ADDRESS_RECOMMENDATION,
        ),
        (
            ResolutionProjectNotFoundError("Project not found: vbot"),
            "project_not_found",
            None,
            cron_tool_module._TARGET_ADDRESS_RECOMMENDATION,
        ),
        (
            AgentResolutionError("agent 'ghost' has no usable model"),
            "agent_unavailable",
            "agent 'ghost' has no usable model",
            cron_tool_module._TARGET_UNAVAILABLE_RECOMMENDATION,
        ),
    ],
)
def test_unresolvable_target_gets_target_guidance(
    tmp_path: Path,
    action: str,
    resolver_error: AgentResolutionError,
    code: str,
    reason: str | None,
    recommendation: str,
) -> None:
    resolver = Mock()
    tool = cron_tool(tmp_path, agent_resolver=resolver)
    job = tool.service.create_job(
        agent_id="agent-one", prompt="Ping", schedule_type="interval", interval_seconds=7200
    )
    resolver.resolve_agent.reset_mock()
    resolver.resolve_agent.side_effect = resolver_error
    arguments: dict[str, object] = (
        {"action": "create", "prompt": "Ping", "schedule": "every 2h", "target": "ghost@vbot"}
        if action == "create"
        else {"action": "update", "id": job.id, "target": "ghost@vbot"}
    )

    envelope, _text = tool.call(arguments)

    error = _error(envelope)
    assert error["code"] == code
    assert error["message"].endswith(f"{recommendation}.")
    if reason is not None:
        assert reason in error["message"]
    resolver.resolve_agent.assert_called_once_with("vbot", "ghost")
    assert [(stored.id, stored.project_id) for stored in tool.jobs()] == [(job.id, None)]
    tool.trigger.trigger_run.assert_not_called()


def test_malformed_target_gets_target_guidance(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    envelope, _text = tool.call(
        {"action": "create", "prompt": "Ping", "schedule": "every 2h", "target": "ghost@"}
    )

    error = _error(envelope)
    # A malformed address names no target, so it is an argument error.
    assert error["code"] == "invalid_arguments"
    assert error["message"].endswith(f"{cron_tool_module._TARGET_ADDRESS_RECOMMENDATION}.")
    assert tool.jobs() == []


def test_conflicting_target_spellings_create_nothing(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    envelope, _text = tool.call(
        {
            "action": "create",
            "prompt": "Ping",
            "schedule": "every 2h",
            "target": "builder@vbot",
            "agent_id": "reviewer@vbot",
        }
    )

    assert "Conflicting values for target" in _error(envelope)["message"]
    assert tool.jobs() == []
