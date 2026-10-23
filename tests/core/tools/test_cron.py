"""The cron Tool against a real CronService: canonical calls, results, and errors."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

import core.tools.cron as cron_tool_module
from core.automation.cron import CronDelivery, CronJobStatus, WatchCheckError
from core.chat import ReplySurface
from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.sessions import SessionAddress
from core.tools._cron_arguments import render_call
from core.tools.cron import CRON_TOOL_NAME, CRON_TOOL_PARAMETERS
from tests.core.tools.scheduling_tool_support import (
    WEBHOOK_BASE_URL,
    CronTool,
    cron_tool,
    scheduling_tools,
)

PROMPT = "Check the nightly build and summarize failures."
BERLIN_TIME = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0[12]:00"


def _error(envelope: dict[str, Any]) -> dict[str, Any]:
    assert envelope["ok"] is False
    return cast(dict[str, Any], envelope["error"])


def test_schema_advertises_only_the_canonical_fields() -> None:
    properties = cast(dict[str, Any], CRON_TOOL_PARAMETERS["properties"])
    # Accepted time zone and paused-state spellings are never advertised.
    assert list(properties) == [
        "action",
        "id",
        "target",
        "name",
        "prompt",
        "schedule",
        "event_id",
        "repeat",
        "session",
        "deliver",
        "watch",
        "watch_runs",
    ]
    assert properties["deliver"]["enum"] == ["here", "app", "none"]
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


@pytest.mark.parametrize(("action", "repeat"), [("create", 2), ("create", None), ("update", None)])
def test_one_time_schedule_refuses_another_repeat(
    tmp_path: Path, action: str, repeat: int | None
) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 1d"})
    subject = {"prompt": PROMPT} if action == "create" else {"id": created["data"]["id"]}

    envelope, _text = tool.call(
        {"action": action, **subject, "schedule": "in 30m", "repeat": repeat}
    )

    error = _error(envelope)
    assert error["code"] == "invalid_arguments"
    assert "fires once" in error["message"]
    assert '"repeat":1' in error["message"]
    assert "retryable" not in error
    assert [(job.schedule_type, job.remaining_runs) for job in tool.jobs()] == [("interval", None)]


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
    quiet = next(job for job in tool.jobs() if job.name == "Digest")
    tool.service._jobs[quiet.id].last_outcome = "success"
    tool.service._jobs[quiet.id].last_run_silent = True
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
    assert "last_outcome: success (answered [SILENT])" in digest
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


def test_a_restricted_session_bounds_the_jobs_it_creates_or_changes(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"write": "scratch"}),
        BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
    )
    create = {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *"}

    bounded, _ = tool.call(create, permissions=boundary)
    unbounded, _ = tool.call(create)

    def ceiling(envelope: dict[str, Any]) -> Any:
        return tool.service.get_job(envelope["data"]["id"]).ceiling()

    assert ceiling(bounded) == boundary.ceiling(source="cron")
    assert ceiling(unbounded) is None
    # A full-access change keeps the creator's boundary; a restricted one adds its own.
    tool.call({"action": "update", "id": bounded["data"]["id"], "name": "Renamed"})
    tool.call(
        {"action": "update", "id": unbounded["data"]["id"], "name": "Renamed"}, permissions=boundary
    )
    assert ceiling(bounded) == ceiling(unbounded) == boundary.ceiling(source="cron")
    # Another restricted Session's change never widens the job: both boundaries apply.
    other = PermissionBoundary.build(
        normalize_permission_rules({"web": "listed", "allow": ["Web(example.com)"]}),
        BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
    )
    tool.call({"action": "update", "id": bounded["data"]["id"], "name": "Again"}, permissions=other)
    both = ceiling(bounded)
    first, second = boundary.ceiling(source="cron"), other.ceiling(source="cron")
    assert both is not None and first is not None and second is not None
    assert both.layers == (*first.layers, *second.layers)
    # A watch the job's boundary refuses is refused before the job changes.
    tool.checks.refusal = boundary.refusal(
        "web", "intranet.example", "not_permitted", base="listed"
    )
    refused, _ = tool.call(
        {"action": "update", "id": bounded["data"]["id"], "watch": "https://intranet.example/"},
        permissions=boundary,
    )
    assert refused["error"]["code"] == "permission_denied"
    assert tool.service.get_job(bounded["data"]["id"]).watch is None


def test_a_restricted_session_pins_jobs_only_to_its_own_session_tree(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    boundary = PermissionBoundary.build(
        normalize_permission_rules({"write": "scratch"}),
        BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
    )
    own = SessionAddress(None, "agent-one", "session-one")
    tool.parents[SessionAddress(None, "agent-one", "child")] = [own]
    tool.parents[SessionAddress(None, "agent-one", "grandchild")] = [
        SessionAddress(None, "agent-one", "child"),
        own,
    ]
    create = {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *"}

    pinned = {
        session: tool.call({**create, "session": session}, permissions=boundary)[0]
        for session in ("current", "session-one", "new", "child", "grandchild", "main-chat")
    }
    users_job, _ = tool.call({**create, "session": "main-chat"})
    changed, _ = tool.call(
        {"action": "update", "id": users_job["data"]["id"], "name": "Mine"}, permissions=boundary
    )

    assert {session: envelope["ok"] for session, envelope in pinned.items()} == {
        "current": True,
        "session-one": True,
        "new": True,
        "child": True,
        "grandchild": True,
        "main-chat": False,
    }
    reason = (
        'Session "main-chat": the job runs under your permission rules, and that Session would '
        "keep them for all its later Runs."
    )
    assert pinned["main-chat"]["error"] == {
        "code": "permission_denied",
        "message": f"cron was not run: the permission rules refuse running a job in {reason} "
        'Pin a job only to this conversation ("current") or a Session of your Sub-Agents. '
        f"Send: {render_call({**create, 'session': 'new'})}",
        "retryable": False,
    }
    # Changing the user's job pinned elsewhere would restrict that Session too.
    assert changed["error"]["code"] == "permission_denied"
    assert changed["error"]["message"] == (
        f"cron was not run: the permission rules refuse changing a job that runs in {reason} "
        "If the task needs this change, tell the user."
    )
    job = tool.service.get_job(users_job["data"]["id"])
    assert job.name != "Mine" and job.ceiling() is None
    assert len(tool.jobs()) == 6


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


@pytest.mark.parametrize("fields", [{}, {"max_delay": "1h"}])
def test_update_needs_a_change(tmp_path: Path, fields: dict[str, Any]) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 2h"})
    job_id = created["data"]["id"]

    envelope, _text = tool.call({"action": "update", "id": job_id, **fields})

    message = _error(envelope)["message"]
    assert message.startswith("cron was not run: update needs a field to change")
    # A field without effect is named, not dropped silently.
    assert ('"max_delay" has no effect.' in message) == bool(fields)
    assert message.endswith(f'Send: {{"action":"update","id":"{job_id}","schedule":"<when>"}}')
    before = tool.only_job().to_dict()

    resent, _text = tool.call({"action": "update", "id": job_id, "schedule": "<when>"})

    assert resent["ok"] is False
    assert tool.only_job().to_dict() == before


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
        assert message.endswith('"schedule":"<future time>"}')
    resent, _ = tool.call({"action": "create", "prompt": "Remind me", "schedule": "<future time>"})
    assert resent["ok"] is False
    assert [stored.id for stored in tool.jobs()] == [job_id]
    tool.trigger.trigger_run.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "missed"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "enable"},
        {"action": "update", "schedule": "in 30m"},
        {"action": "update", "prompt": "Changed instruction"},
    ],
)
def test_finished_job_cannot_resume_or_change(
    tmp_path: Path, status: CronJobStatus, arguments: dict[str, Any]
) -> None:
    tool = cron_tool(tmp_path)
    created, _ = tool.call({"action": "create", "prompt": PROMPT, "schedule": "every 2h"})
    job_id = created["data"]["id"]
    tool.service._jobs[job_id].status = status
    before = tool.only_job().to_dict()
    jobs_path = tmp_path / "cron" / "jobs.json"
    saved = jobs_path.read_bytes()

    envelope, _text = tool.call({**arguments, "id": job_id})

    message = _error(envelope)["message"]
    assert "The job has finished; create a new job instead" in message
    assert f'{{"action":"delete","id":"{job_id}"}}' in message
    assert _error(envelope)["code"] == "invalid_arguments"
    assert tool.only_job().to_dict() == before
    assert jobs_path.read_bytes() == saved
    tool.trigger.trigger_run.assert_not_called()


@pytest.mark.parametrize(
    ("fields", "value"),
    [
        ({"schedule": "whenever"}, "whenever"),
        # Digits int() cannot read are no epoch time or compact date.
        ({"run_at": "\u00b2" * 10}, "\u00b2" * 10),
        # A date has no time of day for Z to place in UTC, in any time zone.
        ({"schedule": "2030-01-10Z", "timezone": "Asia/Tokyo"}, "2030-01-10Z"),
    ],
)
def test_invalid_schedule_lists_the_forms(
    tmp_path: Path, fields: dict[str, str], value: str
) -> None:
    tool = cron_tool(tmp_path)

    envelope, _text = tool.call({"action": "create", "prompt": PROMPT, **fields})

    message = _error(envelope)["message"]
    assert message.startswith(f'cron was not run: schedule "{value}" is not valid')
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
    job = asyncio.run(
        tool.service.create_job(
            agent_id="agent-one", prompt="Ping", schedule_type="interval", interval_seconds=7200
        )
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


@pytest.mark.parametrize("action", ["create", "update"])
def test_target_choices_wait_for_the_reference_lock(tmp_path: Path, action: str) -> None:
    """A removal that holds the lock ends before a call can select its Agent or Project."""
    tool = cron_tool(tmp_path)
    job_id = tool.add_job()
    arguments = {
        "create": {"action": "create", "prompt": PROMPT, "schedule": "every 3h"},
        "update": {"action": "update", "id": job_id, "target": "builder@vbot"},
    }[action]

    def jobs() -> list[dict[str, Any]]:
        return [job.to_dict() for job in tool.jobs()]

    before = jobs()

    async def while_a_removal_holds_the_lock() -> list[dict[str, Any]]:
        async with tool.reference_lock:
            call = asyncio.create_task(tool.call_async(arguments))
            for _ in range(5):
                await asyncio.sleep(0)
            held = jobs()
        envelope, text = await call
        assert envelope["ok"] is True, text
        return held

    assert asyncio.run(while_a_removal_holds_the_lock()) == before
    assert jobs() != before


# -- jobs at calendar events -----------------------------------------------------------------


def _chat(thread_id: str | None = None) -> ReplySurface:
    """The calling Run answers chat-42 of agent-one's Telegram Channel tg-main."""
    return ReplySurface(
        kind="channel",
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-main",
        conversation_kind="direct",
        platform_target="chat-42",
        thread_id=thread_id,
    )


@pytest.mark.parametrize(
    ("surface", "fields", "delivery", "shown"),
    [
        pytest.param(None, {}, CronDelivery(kind="app"), "app", id="vbot"),
        pytest.param(
            _chat(),
            {},
            CronDelivery(kind="chat", channel_id="tg-main", platform_target="chat-42"),
            "chat chat-42 of Channel tg-main",
            id="chat",
        ),
        pytest.param(
            _chat("7"),
            {"deliver": "here"},
            CronDelivery(
                kind="chat", channel_id="tg-main", platform_target="chat-42", thread_id="7"
            ),
            "chat chat-42, thread 7, of Channel tg-main",
            id="chat-thread",
        ),
        # Hermes names the platform of the chat the job was made in.
        pytest.param(
            _chat(),
            {"deliver": "Telegram"},
            CronDelivery(kind="chat", channel_id="tg-main", platform_target="chat-42"),
            "chat chat-42 of Channel tg-main",
            id="this-platform",
        ),
        pytest.param(_chat(), {"deliver": "app"}, CronDelivery(kind="app"), "app", id="app"),
        pytest.param(_chat(), {"deliver": "none"}, None, "none", id="none"),
        # The chat belongs to the caller's Channel; another Agent's job reports in vBot.
        pytest.param(
            _chat(), {"target": "builder"}, CronDelivery(kind="app"), "app", id="other-agent"
        ),
    ],
)
def test_answers_go_to_the_conversation_the_job_was_made_in_unless_told_otherwise(
    tmp_path: Path,
    surface: ReplySurface | None,
    fields: dict[str, Any],
    delivery: CronDelivery | None,
    shown: str,
) -> None:
    tool = cron_tool(tmp_path)

    envelope, text = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *", **fields},
        reply_surface=surface,
    )

    assert envelope["ok"] is True, text
    assert tool.only_job().delivery == delivery
    assert f"deliver: {shown}" in text
    assert ("not to builder" in text) == ("target" in fields)


@pytest.mark.parametrize(
    ("fields", "cause"),
    [
        pytest.param(
            {"target": "builder", "deliver": "here"},
            "this chat belongs to your Channel, so a job of builder cannot deliver here",
            id="another-agents-job",
        ),
        pytest.param(
            {"deliver": "slack"}, "this conversation is not a slack chat", id="another-platform"
        ),
        pytest.param({}, "delivery Channel tg-main is not available", id="channel-gone"),
    ],
)
def test_a_chat_that_cannot_take_the_answers_is_refused_with_vbot_instead(
    tmp_path: Path, fields: dict[str, Any], cause: str
) -> None:
    tool = cron_tool(tmp_path)
    if not fields:
        tool.chats.owners.clear()

    message = tool.refused(
        {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *", **fields},
        reply_surface=_chat(),
    )

    assert cause in message
    assert message.endswith('"deliver":"app"}')


@pytest.mark.asyncio
async def test_a_watch_is_checked_at_once_and_none_stops_it(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    missing = WatchCheckError("The check command exited with code 1: status.txt not found")
    tool.checks.answers.extend(["Status: up", missing])
    tool.service.start()
    try:
        envelope, created = await tool.call_async(
            {
                "action": "create",
                "prompt": PROMPT,
                "schedule": "every 1h",
                "watch": "https://status.example/",
            }
        )
        job_id = envelope["data"]["id"]
        _envelope, broken = await tool.call_async(
            {"action": "update", "id": job_id, "watch": "Get-Content status.txt"}
        )
        # The same watch is not checked again.
        _envelope, renamed = await tool.call_async(
            {"action": "update", "id": job_id, "name": "Status", "watch": "Get-Content status.txt"}
        )
        _envelope, stopped = await tool.call_async(
            {"action": "update", "id": job_id, "watch": "none"}
        )
    finally:
        await tool.service.aclose()

    assert "watch: https://status.example/" in created
    assert re.search(rf"^last_check: {BERLIN_TIME}$", created, re.MULTILINE)
    assert "last_check_error" not in created
    assert "The first check recorded the current output" in created
    assert "watch: Get-Content status.txt" in broken
    assert "last_check_error: The check command exited with code 1: status.txt not found" in broken
    assert "The first check failed" in broken
    assert "first check" not in renamed
    assert "watch" not in stopped and "last_check" not in stopped
    assert tool.only_job().watch is None
    assert tool.checks.calls == [
        ("url", "https://status.example/"),
        ("command", "Get-Content status.txt", "agent-one", ""),
    ]


@pytest.mark.asyncio
async def test_watch_runs_always_starts_every_run_and_stays_with_its_watch(
    tmp_path: Path,
) -> None:
    tool = cron_tool(tmp_path)
    tool.checks.answers.extend(["Status: up", "v1"])
    tool.service.start()
    try:
        envelope, created = await tool.call_async(
            {
                "action": "create",
                "prompt": PROMPT,
                "schedule": "every 1h",
                "watch": "https://status.example/",
                "watch_runs": "every time",
            }
        )
        job_id = envelope["data"]["id"]
        # A new watch target keeps the mode; watch_runs alone changes it.
        await tool.call_async({"action": "update", "id": job_id, "watch": "Get-Content v.txt"})
        always = tool.only_job().watch
        _envelope, on_change = await tool.call_async(
            {"action": "update", "id": job_id, "watch_runs": "on_change"}
        )
    finally:
        await tool.service.aclose()

    assert "watch_runs: always" in created
    assert "each run gets the current output" in created
    assert always is not None and always.always
    watch = tool.only_job().watch
    assert watch is not None and (watch.target, watch.always) == ("Get-Content v.txt", False)
    assert "first check" not in on_change
    assert "watch_runs" not in on_change

    # Without a watch, there is nothing for watch_runs to change.
    await tool.call_async({"action": "update", "id": job_id, "watch": "none"})
    envelope, refused = await tool.call_async(
        {"action": "update", "id": job_id, "watch_runs": "always"}
    )
    assert envelope["ok"] is False
    assert "watch_runs needs watch, the command or http(s) URL to check" in refused
    assert '"watch":"<command or URL to check>"' in refused
    assert tool.only_job().watch is None


@pytest.mark.parametrize(
    ("fields", "cause", "send"),
    [
        pytest.param(
            {"schedule": "in 2h", "watch": "https://status.example/"},
            'watch needs a repeating schedule (five cron fields or "every <duration>")',
            '"schedule":"<repeating schedule>","watch":"https://status.example/"} or '
            f'{{"action":"create","prompt":"{PROMPT}","schedule":"in 2h"}}',
            id="one-time",
        ),
        pytest.param(
            {"schedule": "every 1h", "watch": "x" * 4001},
            "a command can have at most 4000",
            '"watch":"<command or URL to check>"}',
            id="long-command",
        ),
    ],
)
def test_a_watch_the_job_cannot_check_is_refused(
    tmp_path: Path, fields: dict[str, Any], cause: str, send: str
) -> None:
    message = cron_tool(tmp_path).refused({"action": "create", "prompt": PROMPT, **fields})

    assert cause in message
    assert message.endswith(send)


def test_session_current_continues_this_conversation_until_new(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)

    job, created = tool.created(
        {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *", "session": "current"}
    )
    refused = tool.refused(
        {"action": "update", "id": job.id, "target": "builder", "session": "current"}
    )
    _envelope, renewed = tool.call({"action": "update", "id": job.id, "session": "new"})

    assert job.session_id == "session-one"
    assert "session: session-one" in created
    assert 'session "current" is your conversation, so a job of builder cannot continue it' in (
        refused
    )
    assert tool.only_job().session_id is None
    assert "session:" not in renewed


def _standup(tool: CronTool) -> str:
    """Mondays 09:00-09:15 from 2030-01-07; return its id."""
    return tool.calendar.create_event(
        title="Standup", start="2030-01-07T09:00", end="2030-01-07T09:15", rrule="FREQ=WEEKLY"
    ).id


@pytest.mark.parametrize(
    ("schedule", "edge", "offset", "next_run"),
    [
        ("start - 30m", "start", -30, "2030-01-07T08:30:00+01:00"),
        ("start", "start", 0, "2030-01-07T09:00:00+01:00"),
        ("end + 1h", "end", 60, "2030-01-07T10:15:00+01:00"),
    ],
)
def test_event_job_runs_before_at_or_after_each_occurrence(
    tmp_path: Path, schedule: str, edge: str, offset: int, next_run: str
) -> None:
    tool = cron_tool(tmp_path)
    standup = _standup(tool)

    job, text = tool.created(
        {"action": "create", "event_id": standup, "prompt": PROMPT, "schedule": schedule}
    )

    assert (job.schedule_type, job.event_id, job.event_edge, job.event_offset_minutes) == (
        "event",
        standup,
        edge,
        offset,
    )
    assert job.remaining_runs is None
    assert text.splitlines()[2:] == [
        "status: active",
        f"schedule: {schedule}",
        f"event: Standup ({standup})",
        f"next_run: {next_run}",
        "target: agent-one",
        "deliver: app",
    ]


def test_event_job_without_an_occurrence_ahead_says_so(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    past = tool.calendar.create_event(title="Old", start="2020-01-07T09:00").id

    _job, text = tool.created(
        {"action": "create", "event_id": past, "prompt": PROMPT, "schedule": "start"}
    )

    assert "next_run: none; no occurrence of the event lies ahead" in text


def test_list_with_an_event_id_shows_the_jobs_at_that_event(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    standup = _standup(tool)
    tool.add_job(name="Elsewhere")
    tool.succeeded(
        {
            "action": "create",
            "event_id": standup,
            "name": "Agenda",
            "prompt": PROMPT,
            "schedule": "start",
        }
    )

    envelope, text = tool.call({"action": "list", "event_id": standup})

    assert envelope["data"]["jobs"] == 1
    assert "name: Agenda" in text and "Elsewhere" not in text
    display = tool.registry.display_for_call(
        CRON_TOOL_NAME, {"action": "list", "event_id": standup}
    )
    assert [part["value"] for part in display["primary"]] == ["list", standup]


@pytest.mark.parametrize("action", ["create", "update"])
def test_event_job_refuses_repeat(tmp_path: Path, action: str) -> None:
    tool = cron_tool(tmp_path)
    standup = _standup(tool)
    call = {"action": "create", "event_id": standup, "prompt": PROMPT, "schedule": "start"}
    if action == "update":
        job_id = tool.created(call)[0].id
        call = {"action": "update", "id": job_id, "name": "Agenda"}

    message = tool.refused({**call, "repeat": 3})

    assert message == (
        "cron was not run: a job at a calendar event runs at every occurrence of the event, so "
        f"it takes no repeat. Nothing was changed. Send: {render_call(call)}"
    )


@pytest.mark.parametrize("calendar_offered", [True, False])
def test_unknown_event_is_refused(tmp_path: Path, calendar_offered: bool) -> None:
    tool = scheduling_tools(
        tmp_path, offered=("calendar", "cron") if calendar_offered else ("cron",)
    )[1]

    message = tool.refused(
        {"action": "create", "event_id": "evt_missing", "prompt": PROMPT, "schedule": "start"}
    )

    listing = " The calendar list shows events and their ids." if calendar_offered else ""
    assert message == f'cron was not run: no calendar event has id "evt_missing".{listing}'


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            {"action": "create", "event_id": "{event}", "prompt": PROMPT},
            'cron was not run: create needs "schedule", start or end of the event, optionally '
            '+/- a duration. Send: {"action":"create","event_id":"{event}","prompt":"'
            + PROMPT
            + '","schedule":"<start or end, e.g. start - 30m>"}',
        ),
        (
            {"action": "create", "event_id": "{event}", "prompt": PROMPT, "schedule": "lunch"},
            'cron was not run: schedule "lunch" is not an event time: event time must be start '
            "or end, optionally + or - a duration such as 'start - 30m'. Send: "
            '{"action":"create","event_id":"{event}","prompt":"'
            + PROMPT
            + '","schedule":"<start or end, e.g. start - 30m>"}',
        ),
        (
            {"action": "create", "prompt": PROMPT, "schedule": "start - 30m"},
            'cron was not run: schedule "start - 30m" is a time at a calendar event; send its '
            'event_id. Send: {"action":"create","event_id":"<calendar event id>","prompt":"'
            + PROMPT
            + '","schedule":"start - 30m"}',
        ),
    ],
)
def test_event_time_needs_its_event_and_an_edge(
    tmp_path: Path, call: dict[str, Any], message: str
) -> None:
    tool = cron_tool(tmp_path)
    standup = _standup(tool)
    filled = {key: value.replace("{event}", standup) for key, value in call.items()}

    assert tool.refused(filled) == message.replace("{event}", standup)


def test_update_moves_an_event_job_between_times_events_and_schedules(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    standup = _standup(tool)
    review = tool.calendar.create_event(title="Review", start="2030-01-08T14:00").id
    job_id = tool.created(
        {"action": "create", "event_id": standup, "prompt": PROMPT, "schedule": "end + 1h"}
    )[0].id

    def fields() -> tuple[Any, ...]:
        job = tool.only_job()
        return (job.schedule_type, job.event_id, job.event_edge, job.event_offset_minutes)

    # Another event keeps the event time; a new event time keeps the event.
    tool.succeeded({"action": "update", "id": job_id, "event_id": review})
    assert fields() == ("event", review, "end", 60)
    tool.succeeded({"action": "update", "id": job_id, "schedule": "start - 10m"})
    assert fields() == ("event", review, "start", -10)
    # A schedule of another kind ends the binding.
    tool.succeeded({"action": "update", "id": job_id, "schedule": "every 2h"})
    assert fields() == ("interval", None, None, None)


def test_a_webhook_job_shows_its_url_and_hands_out_its_secret_once(tmp_path: Path) -> None:
    tool = cron_tool(tmp_path)
    timed_id = tool.add_job()

    envelope, created = tool.call({"action": "create", "prompt": PROMPT, "schedule": "webhook"})
    job_id = envelope["data"]["id"]
    secret = asyncio.run(tool.service.webhook_secret(job_id))
    renamed = tool.succeeded({"action": "update", "id": job_id, "name": "GitHub events"})
    switched = tool.succeeded({"action": "update", "id": timed_id, "schedule": "webhook"})
    switched_secret = asyncio.run(tool.service.webhook_secret(timed_id))
    received = asyncio.run(
        tool.service.receive_webhook(job_id, [("authorization", f"Bearer {secret}")], b"{}")
    )
    listed = tool.succeeded({"action": "list", "id": job_id})

    job = tool.service.get_job(job_id)
    assert (job.schedule_type, job.cron_expression, job.interval_seconds) == ("webhook", None, None)
    url = f"url: {WEBHOOK_BASE_URL}/api/hooks/{job_id}"
    assert f"schedule: webhook\n{url}\nreachable_from: the internet\n" in created
    assert "next_run" not in created
    assert f"secret: {secret}" in created
    assert f'"Authorization: Bearer {secret}"' in created
    assert "X-Hub-Signature-256" in created
    # The secret comes with the call that made the webhook, never again.
    assert secret not in renamed
    assert f"secret: {switched_secret}" in switched
    assert received == "accepted"
    assert url in listed
    assert "requests_waiting: 1" in listed
    assert re.search(rf"^last_request: {BERLIN_TIME}$", listed, re.MULTILINE)
    assert secret not in listed
