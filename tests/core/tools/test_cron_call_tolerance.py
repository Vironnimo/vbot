"""Real dispatch executes clear ``cron`` intent written in other shapes.

vBot's earlier cron schemas, other harness dialects, action synonyms,
placeholders and time zones reach exactly the job the call meant. Each repair
is paired with a nearby call that means something else and is refused, before
any job changes, with the corrected call.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.tools.cron import CRON_TOOL_NAME

from .cron_tool_support import CronTool, cron_tool
from .tools_helpers import clock_at

PROMPT = "Lint the wiki and report broken links."


@pytest.fixture
def tool(tmp_path: Path) -> CronTool:
    return cron_tool(tmp_path)


def refused(tool: CronTool, arguments: Any) -> str:
    """Dispatch a call that must fail without touching any job; return its message."""
    before = [job.to_dict() for job in tool.jobs()]
    envelope, text = tool.call(arguments)
    assert envelope["ok"] is False, text
    assert envelope["error"]["code"] == "invalid_arguments"
    assert [job.to_dict() for job in tool.jobs()] == before
    message = str(envelope["error"]["message"])
    assert message.startswith("cron was not run: ")
    return message


def created(tool: CronTool, arguments: Any) -> tuple[Any, str]:
    envelope, text = tool.call(arguments)
    assert envelope["ok"] is True, text
    return tool.only_job(), text


def existing(tool: CronTool, **fields: Any) -> str:
    envelope, _text = tool.call(
        {"action": "create", "prompt": PROMPT, "schedule": "every 2h", **fields}
    )
    return str(envelope["data"]["id"])


# The weeks around the 2030 clock changes in Europe and North America.
_CLOCK_CHANGE_WINDOWS = (
    (datetime(2030, 3, 3, tzinfo=UTC), datetime(2030, 4, 7, tzinfo=UTC)),
    (datetime(2030, 10, 20, tzinfo=UTC), datetime(2030, 11, 10, tzinfo=UTC)),
)


def fires(tool: CronTool) -> list[datetime]:
    """The instants the scheduler fires the Tool's jobs around the clock changes."""
    return [
        occurrence.fire_at_utc
        for start, end in _CLOCK_CHANGE_WINDOWS
        for occurrence in tool.service.project_occurrences(start, end, max_per_job=10_000)
    ]


def fires_in(tmp_path: Path, zone: str, schedule: str) -> list[datetime]:
    """The instants ``schedule`` means in ``zone``: the scheduler running in that zone."""
    reference = cron_tool(tmp_path, tz=zone)
    created(reference, {"action": "create", "prompt": PROMPT, "schedule": schedule})
    return fires(reference)


# -- vBot's earlier schemas (anonymized shapes from Session history) -----------------------


def test_flat_create_without_prompt_names_one_cause_and_the_call(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "create",
            "agent_id": "builder@vbot",
            "cron_expression": "0 3 */3 * *",
            "schedule_type": "cron",
            "session_id": "",
            "status": "active",
            "timezone": "Europe/Berlin",
        },
    )

    assert message == (
        'cron was not run: create needs "prompt", the complete instruction the Agent runs at '
        'each fire. Send: {"action":"create","target":"builder@vbot","prompt":"<instruction>",'
        '"schedule":"0 3 */3 * *"}'
    )


def test_flat_create_with_prompt_runs(tool: CronTool) -> None:
    job, text = created(
        tool,
        {
            "action": "create",
            "agent_id": "builder@vbot",
            "cron_expression": "0 3 */3 * *",
            "schedule_type": "cron",
            "session_id": "",
            "status": "active",
            "timezone": "Europe/Berlin",
            "prompt": PROMPT,
        },
    )

    assert (job.agent_id, job.project_id, job.cron_expression, job.status) == (
        "builder",
        "vbot",
        "0 3 */3 * *",
        "active",
    )
    assert job.session_id is None
    assert "note" not in text


def test_operation_object_with_inert_enable_creates_the_job(tool: CronTool) -> None:
    job, _text = created(
        tool,
        {
            "create": {
                "cron_expression": "0 3 */3 * *",
                "prompt": PROMPT,
                "run_at": "2030-07-25T03:00:00Z",
                "schedule_type": "cron",
            },
            "enable": {"id": "placeholder"},
        },
    )

    # schedule_type selects the cron expression; run_at belongs to one-time jobs.
    assert (job.schedule_type, job.cron_expression, job.status) == ("cron", "0 3 */3 * *", "active")


def test_two_real_operations_are_refused_with_both_calls(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(
        tool,
        {
            "create": {"prompt": PROMPT, "schedule": "every 3h"},
            "delete": {"id": job_id},
        },
    )

    assert "asks for create and delete" in message
    assert '{"action":"create","prompt":"' in message
    assert f'{{"action":"delete","id":"{job_id}"}}' in message


@pytest.mark.parametrize("arguments", [{"list": "{}"}, {"list": {}}, {"list": True}, {}])
def test_list_spellings_list_the_jobs(tool: CronTool, arguments: dict[str, Any]) -> None:
    existing(tool, name="Queue")

    envelope, text = tool.call(arguments)

    assert envelope["ok"] is True
    assert "jobs: 1" in text and "name: Queue" in text
    display = tool.registry.display_for_call(CRON_TOOL_NAME, arguments, result=envelope)
    assert display["facts"][0]["value"] == 1


def test_update_with_old_schedule_fields(tool: CronTool) -> None:
    job_id = existing(tool)

    tool.call({"action": "update", "id": job_id, "cron_expression": "30 0 */2 * *"})
    tool.call({"update": {"id": job_id, "agent_id": "builder", "name": "Wiki lint"}})

    job = tool.only_job()
    assert (job.cron_expression, job.agent_id, job.name) == ("30 0 */2 * *", "builder", "Wiki lint")


# -- state, Session and delivery -----------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "paused"},
        {"status": "disabled"},
        {"enabled": False},
        {"enabled": "false"},
        {"paused": True},
    ],
)
def test_inactive_state_on_create_creates_a_paused_job(
    tool: CronTool, fields: dict[str, Any]
) -> None:
    job, text = created(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", **fields}
    )

    assert job.status == "paused"
    assert f'note: The job is paused; {{"action":"enable","id":"{job.id}"}} starts it.' in text
    tool.trigger.trigger_run.assert_not_called()


@pytest.mark.parametrize("fields", [{"status": "active"}, {"enabled": True}, {"status": "Enabled"}])
def test_active_state_on_create_is_the_default(tool: CronTool, fields: dict[str, Any]) -> None:
    job, text = created(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", **fields}
    )

    assert job.status == "active"
    assert "note" not in text


def test_finished_or_conflicting_states_are_refused(tool: CronTool) -> None:
    completed = refused(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", "status": "completed"}
    )
    both = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "every 2h",
            "status": "active",
            "enabled": False,
        },
    )

    assert 'status "completed" cannot be set' in completed
    assert completed.endswith(
        'Send: {"action":"create","prompt":"' + PROMPT + '","schedule":"every 2h"}'
    )
    assert "both enabled and paused" in both


def test_status_on_update_pauses_and_resumes(tool: CronTool) -> None:
    job_id = existing(tool)

    tool.call({"action": "update", "id": job_id, "status": "paused"})
    assert tool.only_job().status == "paused"
    tool.call({"action": "enable", "id": job_id, "schedule": "every 3h"})

    job = tool.only_job()
    assert (job.status, job.interval_seconds) == ("active", 10800)


@pytest.mark.parametrize("session", ["", "new", "isolated", "none"])
def test_fresh_session_spellings_are_accepted(tool: CronTool, session: str) -> None:
    created(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", "session_id": session}
    )


@pytest.mark.parametrize(
    "fields", [{"session_id": "sess_abc"}, {"sessionTarget": "main"}, {"session": "current"}]
)
def test_chosen_session_is_refused(tool: CronTool, fields: dict[str, Any]) -> None:
    message = refused(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", **fields}
    )

    assert "every fire starts a fresh Session" in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{PROMPT}","schedule":"every 2h"}}'
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"deliver": "local"},
        {"deliver": ""},
        {"delivery": {"mode": "none"}},
        {"notifyOnCompletion": False},
        {"durable": True},
        {"durable": False},
    ],
)
def test_fields_that_request_nothing_extra_run(tool: CronTool, fields: dict[str, Any]) -> None:
    created(tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", **fields})


@pytest.mark.parametrize(
    "fields",
    [
        {"deliver": "origin"},
        {"deliver": "telegram:-100123"},
        {"delivery": {"mode": "announce", "channel": "telegram"}},
        {"notifyOnCompletion": True},
    ],
)
def test_requested_delivery_is_refused(tool: CronTool, fields: dict[str, Any]) -> None:
    message = refused(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", **fields}
    )

    assert "a job cannot deliver its reply" in message
    assert "Say in prompt how the result should reach the user" in message


def test_refusal_names_other_fields_the_corrected_call_drops(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "every 2h",
            "deliver": "origin",
            "skills": ["ops"],
        },
    )

    assert '"skills" is not a parameter.' in message


# -- schedule spellings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "schedule_type", "stored"),
    [
        ({"schedule": "every 2 hours"}, "interval", 7200),
        ({"schedule": "every 90 minutes"}, "interval", 5400),
        ({"schedule": "every 1 week"}, "interval", 604800),
        ({"interval": "PT30M"}, "interval", 1800),
        ({"interval_seconds": 7200}, "interval", 7200),
        ({"everyMs": 3600000}, "interval", 3600),
        ({"every": "2h"}, "interval", 7200),
        ({"schedule": "30m", "schedule_type": "interval"}, "interval", 1800),
        ({"schedule": "@daily"}, "cron", "0 0 * * *"),
        ({"cron": "0 9 * * 1-5"}, "cron", "0 9 * * 1-5"),
        ({"cronExpression": "0 8 * * *"}, "cron", "0 8 * * *"),
        ({"schedule": {"kind": "cron", "expr": "0 8 * * *"}}, "cron", "0 8 * * *"),
        ({"schedule": {"kind": "every", "everyMs": 900000}}, "interval", 900),
    ],
)
def test_schedule_spellings_reach_the_schedule(
    tool: CronTool, fields: dict[str, Any], schedule_type: str, stored: object
) -> None:
    job, _text = created(tool, {"action": "create", "prompt": PROMPT, **fields})

    assert job.schedule_type == schedule_type
    assert (job.interval_seconds if schedule_type == "interval" else job.cron_expression) == stored


@pytest.mark.parametrize(
    "fields",
    [
        {"schedule": "in 45 minutes"},
        {"schedule": "30m", "schedule_type": "once"},
        {"delay": "PT2H"},
        {"run_at": "2030-01-01T09:00:00Z", "schedule_type": "once"},
        {"fireAt": "2030-01-01T10:00:00+01:00"},
        {"at": 1893484800000},
        {"schedule": {"kind": "at", "at": "2030-01-01T09:00:00Z"}},
    ],
)
def test_one_time_spellings_create_one_fire(tool: CronTool, fields: dict[str, Any]) -> None:
    job, _text = created(tool, {"action": "create", "prompt": PROMPT, **fields})

    assert (job.schedule_type, job.remaining_runs) == ("once", 1)


def test_bare_duration_is_refused_with_both_readings(tool: CronTool) -> None:
    message = refused(tool, {"action": "create", "prompt": PROMPT, "schedule": "30m"})

    assert f'{{"action":"create","prompt":"{PROMPT}","schedule":"in 30m"}}' in message
    assert f'{{"action":"create","prompt":"{PROMPT}","schedule":"every 30m"}}' in message


def test_two_schedules_without_a_type_are_refused(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "cron_expression": "0 3 * * *",
            "run_at": "2030-01-01T03:00",
        },
    )

    assert "it names more than one schedule" in message
    assert '"schedule":"0 3 * * *"' in message and '"schedule":"2030-01-01T03:00"' in message


def test_repeating_schedule_marked_one_time_is_refused(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "cron_expression": "0 3 * * *",
            "schedule_type": "once",
        },
    )

    assert '"schedule":"0 3 * * *"}' in message
    assert '"schedule":"0 3 * * *","repeat":1}' in message


@pytest.mark.parametrize(
    ("schedule", "text", "stand_in"),
    [
        (
            "2030-01-01",
            "has no time of day",
            "<2030-01-01 with a time of day, as 2030-01-01THH:MM>",
        ),
        (
            "0 0 9 * * *",
            "cron here takes exactly five",
            "<five cron fields: minute hour day-of-month month day-of-week>",
        ),
    ],
)
def test_incomplete_schedules_are_refused_with_a_stand_in(
    tool: CronTool, schedule: str, text: str, stand_in: str
) -> None:
    message = refused(tool, {"action": "create", "prompt": PROMPT, "schedule": schedule})

    assert text in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{PROMPT}","schedule":"{stand_in}"}}'
    )
    # Sent back unchanged, the stand-in schedules nothing.
    assert "create needs" in refused(
        tool, {"action": "create", "prompt": PROMPT, "schedule": stand_in}
    )


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        ({"interval": 30}, "has no unit"),
        ({"interval_seconds": 90}, "is not a whole number of minutes"),
    ],
)
def test_unclear_interval_numbers_are_refused(
    tool: CronTool, fields: dict[str, Any], text: str
) -> None:
    assert text in refused(tool, {"action": "create", "prompt": PROMPT, **fields})


# -- Claude Code, Hermes, OpenClaw, scheduled-task Tools --------------------------------------


def test_claude_code_cron_create_shapes(tool: CronTool) -> None:
    recurring, _ = created(
        tool, {"cron": "0 9 * * 1-5", "prompt": PROMPT, "recurring": True, "durable": True}
    )
    tool.call({"action": "delete", "id": recurring.id})
    once, text = created(tool, {"cron": "30 14 * * *", "prompt": PROMPT, "recurring": False})

    assert (recurring.cron_expression, recurring.remaining_runs) == ("0 9 * * 1-5", None)
    assert (once.cron_expression, once.remaining_runs) == ("30 14 * * *", 1)
    assert "repeat: 1" in text


def test_single_fire_of_an_interval_is_refused_with_both_readings(tool: CronTool) -> None:
    message = refused(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "every 2h", "recurring": False}
    )

    assert '"schedule":"every 2h"}' in message and '"schedule":"in 2h"}' in message


def test_claude_code_cron_delete_shape_names_the_choices(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(tool, {"id": job_id})

    for action in ("delete", "disable", "enable", "list"):
        assert f'{{"action":"{action}","id":"{job_id}"}}' in message


@pytest.mark.parametrize(
    ("action", "status"),
    [("pause", "paused"), ("suspend", "paused"), ("resume", "active"), ("remove", None)],
)
def test_hermes_actions_with_job_id(tool: CronTool, action: str, status: str | None) -> None:
    job_id = existing(tool)
    if action == "resume":
        tool.call({"action": "disable", "id": job_id})

    envelope, _text = tool.call({"action": action, "job_id": job_id})

    assert envelope["ok"] is True
    assert [job.status for job in tool.jobs()] == ([status] if status else [])


def test_run_now_is_refused_with_a_one_time_job(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(tool, {"action": "run", "job_id": job_id})

    assert "a job cannot be fired on demand" in message
    assert '"schedule":"in 1m"' in message
    tool.trigger.trigger_run.assert_not_called()


@pytest.mark.parametrize("word", ["stop", "cancel"])
def test_stop_words_name_pause_and_delete(tool: CronTool, word: str) -> None:
    job_id = existing(tool)

    message = refused(tool, {"action": word, "id": job_id})

    assert f'{{"action":"disable","id":"{job_id}"}}' in message
    assert f'{{"action":"delete","id":"{job_id}"}}' in message


def test_openclaw_job_object(tool: CronTool) -> None:
    job, _text = created(
        tool,
        {
            "action": "add",
            "job": {
                "name": "Digest",
                "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "Europe/Berlin"},
                "payload": {"kind": "agentTurn", "message": "Write the daily digest."},
                "sessionTarget": "isolated",
                "delivery": {"mode": "none"},
                "enabled": True,
            },
        },
    )

    assert (job.name, job.prompt, job.cron_expression) == (
        "Digest",
        "Write the daily digest.",
        "0 8 * * *",
    )


def test_openclaw_system_event_is_refused(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "add",
            "job": {
                "schedule": {"kind": "every", "everyMs": 3600000},
                "payload": {"kind": "systemEvent", "text": "Check the queue."},
            },
        },
    )

    assert 'payload kind "systemEvent" is not available' in message


def test_scheduled_task_spelling_uses_the_description_as_name(tool: CronTool) -> None:
    job, _text = created(
        tool,
        {
            "action": "create",
            "cronExpression": "0 8 * * *",
            "prompt": PROMPT,
            "description": "Daily digest",
        },
    )

    assert job.name == "Daily digest"


# -- actions, placeholders and fields that do not fit their action ----------------------------


def test_missing_action_is_inferred(tool: CronTool) -> None:
    job, _text = created(tool, {"prompt": PROMPT, "schedule": "every 2h"})
    tool.call({"id": job.id, "schedule": "every 5h"})

    assert tool.only_job().interval_seconds == 18000


def test_create_with_an_id_names_update_and_create(tool: CronTool) -> None:
    message = refused(
        tool, {"action": "create", "id": "daily-digest", "prompt": PROMPT, "schedule": "0 8 * * *"}
    )

    assert '{"action":"update","id":"daily-digest",' in message
    assert f'{{"action":"create","prompt":"{PROMPT}","schedule":"0 8 * * *"}}' in message


def test_update_without_id_names_create(tool: CronTool) -> None:
    message = refused(tool, {"action": "update", "prompt": PROMPT, "schedule": "0 8 * * *"})

    assert f'{{"action":"create","prompt":"{PROMPT}","schedule":"0 8 * * *"}}' in message


def test_delete_with_changes_is_refused(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(tool, {"action": "delete", "id": job_id, "schedule": "every 3h"})

    assert f'{{"action":"delete","id":"{job_id}"}}' in message
    assert f'{{"action":"update","id":"{job_id}","schedule":"every 3h"}}' in message


def test_list_with_job_fields_offers_list_or_create(tool: CronTool) -> None:
    message = refused(tool, {"action": "list", "prompt": PROMPT, "schedule": "every 2h"})

    assert message == (
        "cron was not run: list only shows jobs (optionally one job by id); it takes no prompt, "
        'schedule. To show jobs, leave them out; to make a job, use create: {"action":"list"} '
        f'or {{"action":"create","prompt":"{PROMPT}","schedule":"every 2h"}}'
    )


def test_delete_with_changes_offers_delete_or_update(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(tool, {"action": "delete", "id": job_id, "schedule": "every 3h"})

    assert message.endswith(
        f'Either delete it or change it: {{"action":"delete","id":"{job_id}"}} or '
        f'{{"action":"update","id":"{job_id}","schedule":"every 3h"}}'
    )


def test_list_with_an_id_and_changes_offers_list_or_update(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(tool, {"action": "list", "id": job_id, "schedule": "every 3h"})

    assert message.endswith(
        f'to change job "{job_id}", use update: {{"action":"list","id":"{job_id}"}} or '
        f'{{"action":"update","id":"{job_id}","schedule":"every 3h"}}'
    )


@pytest.mark.parametrize("prompt", ["<instruction>", "placeholder", "TBD"])
def test_placeholder_prompt_is_refused(tool: CronTool, prompt: str) -> None:
    message = refused(tool, {"action": "create", "prompt": prompt, "schedule": "every 2h"})

    assert message == (
        f'cron was not run: prompt "{prompt}" is a placeholder. Send the complete instruction '
        "the Agent should run at each fire."
    )


def test_stand_in_prompt_on_update_is_refused_and_keeps_the_job(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(
        tool, {"action": "update", "id": job_id, "prompt": "<the prompt from this call>"}
    )

    assert tool.only_job().prompt == PROMPT
    assert message == (
        'cron was not run: prompt "<the prompt from this call>" is a stand-in. Send the '
        "complete instruction the Agent should run at each fire in its place, or leave prompt "
        "out to keep the job's current one."
    )


@pytest.mark.parametrize(
    ("call", "default"),
    [
        ({"action": "create", "prompt": PROMPT, "schedule": "every 2h"}, "run the job as yourself"),
        ({"action": "update", "schedule": "every 3h"}, "keep the job's current target"),
    ],
)
def test_stand_in_target_is_refused(tool: CronTool, call: dict[str, Any], default: str) -> None:
    if call["action"] == "update":
        call = {**call, "id": existing(tool)}

    message = refused(tool, {**call, "target": "<agent or agent@project>"})

    assert message == (
        'cron was not run: target "<agent or agent@project>" is a stand-in. Send an existing '
        "Agent id, or agent@project for a Project member, in its place, or leave target out "
        f"to {default}."
    )


def test_placeholder_fields_are_omitted(tool: CronTool) -> None:
    job, _text = created(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "every 2h",
            "target": "self",
            "name": "<name>",
        },
    )

    assert (job.agent_id, job.name) == ("agent-one", PROMPT)


def test_repeat_words_and_invalid_counts(tool: CronTool) -> None:
    job_id = existing(tool, repeat=3)

    tool.call({"action": "update", "id": job_id, "repeat": "unlimited"})
    message = refused(tool, {"action": "update", "id": job_id, "repeat": 0})

    assert tool.only_job().remaining_runs is None
    assert "must be 1 or more" in message


# -- time zones --------------------------------------------------------------------------------


def test_timezone_equal_to_the_server_zone_is_dropped(tool: CronTool) -> None:
    job, text = created(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "0 9 * * *",
            "timezone": "europe/berlin",
        },
    )

    assert job.cron_expression == "0 9 * * *"
    assert "note" not in text


@pytest.mark.parametrize("schedule", ["in 30m", "every 2h"])
def test_timezone_of_a_relative_schedule_is_irrelevant(tool: CronTool, schedule: str) -> None:
    created(
        tool, {"action": "create", "prompt": PROMPT, "schedule": schedule, "tz": "Mars/Olympus"}
    )


def test_local_time_in_another_zone_converts_exactly(tool: CronTool) -> None:
    job, text = created(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "2030-01-01T09:00",
            "timezone": "Pacific/Kiritimati",
        },
    )

    assert job.run_at == "2029-12-31T19:00:00+00:00"
    assert "schedule: 2029-12-31T20:00:00+01:00" in text
    assert (
        'note: Read "2030-01-01T09:00" as Pacific/Kiritimati time: 2029-12-31T20:00:00+01:00 in '
        "the server time zone Europe/Berlin."
    ) in text


def test_offset_timestamp_in_its_own_zone_passes(tool: CronTool) -> None:
    job, _text = created(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "2030-01-01T09:00:00-05:00",
            "timezone": "America/New_York",
        },
    )

    assert job.run_at == "2030-01-01T14:00:00+00:00"


def test_offset_timestamp_in_another_zone_is_refused_with_both_readings(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "2030-01-01T09:00:00Z",
            "timezone": "America/New_York",
        },
    )

    assert '"schedule":"2030-01-01T09:00:00Z"' in message
    assert '"schedule":"2030-01-01T15:00:00+01:00"' in message


def test_cron_in_a_zone_with_a_constant_offset_converts(tool: CronTool) -> None:
    job, text = created(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "0 9 * * 1-5",
            "timezone": "Europe/London",
        },
    )

    assert job.cron_expression == "0 10 * * 1-5"
    assert 'note: Read "0 9 * * 1-5" as Europe/London time: "0 10 * * 1-5"' in text


@pytest.mark.parametrize(
    ("server", "zone", "schedule", "converted"),
    [
        ("UTC", "Asia/Tokyo", "30 3,9-17 * * *", "30 18,0-8 * * *"),
        ("UTC", "Asia/Tokyo", "0 3 * * 1", "0 18 * * 0"),
        ("UTC", "Asia/Tokyo", "0 1-2 * * mon-fri", "0 16-17 * * 0-4"),
        ("UTC", "Pacific/Kiritimati", "0 9 * * 0,3", "0 19 * * 2,6"),
        ("UTC", "Asia/Kolkata", "0 9 * * *", "30 3 * * *"),
        ("UTC", "Asia/Kolkata", "0 * * * *", "30 * * * *"),
        ("Europe/Berlin", "Europe/London", "*/15 1-3 * * *", "*/15 2-4 * * *"),
        ("Europe/Berlin", "Europe/London", "*/15 * * * *", "*/15 * * * *"),
    ],
)
def test_cron_under_a_constant_offset_fires_at_the_moments_the_zone_means(
    tmp_path: Path, server: str, zone: str, schedule: str, converted: str
) -> None:
    tool = cron_tool(tmp_path / "server", tz=server)

    job, text = created(
        tool, {"action": "create", "prompt": PROMPT, "schedule": schedule, "timezone": zone}
    )

    assert job.cron_expression == converted
    assert ("note:" in text) is (converted != schedule)
    assert fires(tool) == fires_in(tmp_path / "zone", zone, schedule)


@pytest.mark.parametrize(
    "schedule",
    [
        "0 1,12 * * 1",  # 01:00 moves to Sunday, 12:00 stays on Monday
        "0 3 1 * *",  # the day before the 1st is no fixed day of the month
        "0 3 * 1 *",  # 03:00 on January 1st is December 31st in UTC
        "0 * * * 1",  # Monday's hours start on Sunday in UTC
    ],
)
def test_cron_whose_fires_cannot_follow_the_zone_is_refused(tmp_path: Path, schedule: str) -> None:
    tool = cron_tool(tmp_path, tz="UTC")

    message = refused(
        tool,
        {"action": "create", "prompt": PROMPT, "schedule": schedule, "timezone": "Asia/Tokyo"},
    )

    assert "Convert the fields to server time and omit timezone." in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{PROMPT}",'
        '"schedule":"<five cron fields in server time>"}'
    )


def test_cron_in_a_zone_with_a_changing_offset_is_refused(tool: CronTool) -> None:
    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "0 9 * * *",
            "timezone": "America/New_York",
        },
    )

    assert "jobs use the server time zone Europe/Berlin" in message
    assert "changes during the year" in message
    assert re.search(
        r'Send: \{"action":"create","prompt":"[^"]+","schedule":"0 1[45] \* \* \*"\}$', message
    )


def test_offsets_that_differ_for_less_than_a_week_are_refused(
    tool: CronTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Jerusalem moves its clocks two days before Berlin in March and two hours before it in
    # October; from this Monday a weekly look at the offsets sees neither.
    monkeypatch.setattr("core.tools.cron.datetime", clock_at(datetime(2026, 9, 28, 12, tzinfo=UTC)))

    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "0 9 * * *",
            "timezone": "Asia/Jerusalem",
        },
    )

    assert "changes during the year" in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{PROMPT}","schedule":"0 8 * * *"}}'
    )


def test_hourly_cron_across_zones_changing_clocks_on_other_dates_is_refused(
    tmp_path: Path,
) -> None:
    tool = cron_tool(tmp_path / "server")
    schedule = "*/15 * * * *"

    message = refused(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": schedule,
            "timezone": "America/New_York",
        },
    )

    assert "change their clocks on different dates" in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{PROMPT}","schedule":"{schedule}"}}'
    )
    # Sent as offered, the job fires in server time, which differs around the changes.
    created(tool, {"action": "create", "prompt": PROMPT, "schedule": schedule})
    assert fires(tool) != fires_in(tmp_path / "zone", "America/New_York", schedule)


@pytest.mark.parametrize(
    ("schedule", "reason", "meant"),
    [
        (
            "2030-03-31T01:30",
            "does not exist in Europe/London: the clocks jump over it that day.",
            [
                ("2030-03-31T01:30:00+01:00", "2030-03-31T00:30+00:00 in Europe/London"),
                ("2030-03-31T03:30:00+02:00", "2030-03-31T02:30+01:00 in Europe/London"),
            ],
        ),
        (
            "2030-10-27T01:30",
            "happens twice in Europe/London: the clocks go back over it that day.",
            [
                ("2030-10-27T02:30:00+02:00", "2030-10-27T01:30+01:00 in Europe/London"),
                ("2030-10-27T02:30:00+01:00", "2030-10-27T01:30+00:00 in Europe/London"),
            ],
        ),
    ],
)
def test_local_time_the_zone_skips_or_repeats_is_refused_with_each_instant(
    tool: CronTool, schedule: str, reason: str, meant: list[tuple[str, str]]
) -> None:
    message = refused(
        tool,
        {"action": "create", "prompt": PROMPT, "schedule": schedule, "timezone": "Europe/London"},
    )

    calls = " or ".join(
        f'{{"action":"create","prompt":"{PROMPT}","schedule":"{server}"}} ({label})'
        for server, label in meant
    )
    assert message == (
        f'cron was not run: "{schedule}" {reason} Send the one that is meant: {calls}.'
    )
    # Each offered call is valid as sent and fires at its own instant.
    job, _text = created(tool, {"action": "create", "prompt": PROMPT, "schedule": meant[1][0]})
    assert datetime.fromisoformat(job.run_at or "") == datetime.fromisoformat(meant[1][0])


def test_offset_of_the_repeated_hour_names_its_instant(tool: CronTool) -> None:
    job, _text = created(
        tool,
        {
            "action": "create",
            "prompt": PROMPT,
            "schedule": "2030-10-27T01:30:00+00:00",
            "timezone": "Europe/London",
        },
    )

    assert job.run_at == "2030-10-27T01:30:00+00:00"


def test_unknown_zone_for_a_wall_clock_schedule_is_refused(tool: CronTool) -> None:
    message = refused(
        tool,
        {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *", "timezone": "Mars/Olympus"},
    )

    assert '"timezone" "Mars/Olympus" is not a known time zone' in message


def test_timezone_alone_on_update_is_refused(tool: CronTool) -> None:
    job_id = existing(tool)

    message = refused(tool, {"action": "update", "id": job_id, "timezone": "America/New_York"})

    assert "cannot keep America/New_York" in message


@pytest.mark.parametrize("zone", ["UTC", "Z", "+00:00", "Etc/UTC"])
def test_utc_spellings_match_a_utc_server(tmp_path: Path, zone: str) -> None:
    tool = cron_tool(tmp_path, tz="UTC")

    job, _text = created(
        tool, {"action": "create", "prompt": PROMPT, "schedule": "0 9 * * *", "timezone": zone}
    )

    assert job.cron_expression == "0 9 * * *"
