"""Real dispatch executes clear ``cron`` intent written in other shapes.

vBot's earlier cron schemas, other harness dialects, action synonyms and
placeholders reach exactly the job the call meant. Each repair is paired with a
nearby call that means something else and is refused, before any job changes,
with the corrected call. Schedules and time zones: ``test_cron_call_times.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.tools.cron import CRON_TOOL_NAME
from tests.core.tools.scheduling_tool_support import JOB_PROMPT, CronTool, cron_tool


@pytest.fixture
def tool(tmp_path: Path) -> CronTool:
    return cron_tool(tmp_path)


# -- vBot's earlier schemas (anonymized shapes from Session history) -----------------------


def test_flat_create_without_prompt_names_one_cause_and_the_call(tool: CronTool) -> None:
    message = tool.refused(
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
    job, text = tool.created(
        {
            "action": "create",
            "agent_id": "builder@vbot",
            "cron_expression": "0 3 */3 * *",
            "schedule_type": "cron",
            "session_id": "",
            "status": "active",
            "timezone": "Europe/Berlin",
            "prompt": JOB_PROMPT,
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
    job, _text = tool.created(
        {
            "create": {
                "cron_expression": "0 3 */3 * *",
                "prompt": JOB_PROMPT,
                "run_at": "2030-07-25T03:00:00Z",
                "schedule_type": "cron",
            },
            "enable": {"id": "placeholder"},
        },
    )

    # schedule_type selects the cron expression; run_at belongs to one-time jobs.
    assert (job.schedule_type, job.cron_expression, job.status) == ("cron", "0 3 */3 * *", "active")


def test_two_real_operations_are_refused_with_both_calls(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused(
        {
            "create": {"prompt": JOB_PROMPT, "schedule": "every 3h"},
            "delete": {"id": job_id},
        },
    )

    assert "asks for create and delete" in message
    assert '{"action":"create","prompt":"' in message
    assert f'{{"action":"delete","id":"{job_id}"}}' in message


@pytest.mark.parametrize("arguments", [{"list": "{}"}, {"list": {}}, {"list": True}, {}])
def test_list_spellings_list_the_jobs(tool: CronTool, arguments: dict[str, Any]) -> None:
    tool.add_job(name="Queue")

    envelope, text = tool.call(arguments)

    assert envelope["ok"] is True
    assert "jobs: 1" in text and "name: Queue" in text
    display = tool.registry.display_for_call(CRON_TOOL_NAME, arguments, result=envelope)
    assert display["facts"][0]["value"] == 1


def test_update_with_old_schedule_fields(tool: CronTool) -> None:
    job_id = tool.add_job()

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
    job, text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert job.status == "paused"
    assert f'note: The job is paused; {{"action":"enable","id":"{job.id}"}} starts it.' in text
    tool.trigger.trigger_run.assert_not_called()


@pytest.mark.parametrize("fields", [{"status": "active"}, {"enabled": True}, {"status": "Enabled"}])
def test_active_state_on_create_is_the_default(tool: CronTool, fields: dict[str, Any]) -> None:
    job, text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert job.status == "active"
    assert "note" not in text


def test_finished_or_conflicting_states_are_refused(tool: CronTool) -> None:
    completed = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", "status": "completed"}
    )
    both = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "every 2h",
            "status": "active",
            "enabled": False,
        },
    )

    assert 'status "completed" cannot be set' in completed
    assert completed.endswith(
        'Send: {"action":"create","prompt":"' + JOB_PROMPT + '","schedule":"every 2h"}'
    )
    assert "both enabled and paused" in both


def test_status_on_update_pauses_and_resumes(tool: CronTool) -> None:
    job_id = tool.add_job()

    tool.call({"action": "update", "id": job_id, "status": "paused"})
    assert tool.only_job().status == "paused"
    tool.call({"action": "enable", "id": job_id, "schedule": "every 3h"})

    job = tool.only_job()
    assert (job.status, job.interval_seconds) == ("active", 10800)


@pytest.mark.parametrize("session", ["", "new", "isolated", "none"])
def test_fresh_session_spellings_are_accepted(tool: CronTool, session: str) -> None:
    tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", "session_id": session}
    )


@pytest.mark.parametrize(
    "fields", [{"session_id": "sess_abc"}, {"sessionTarget": "main"}, {"session": "current"}]
)
def test_chosen_session_is_refused(tool: CronTool, fields: dict[str, Any]) -> None:
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert "every fire starts a fresh Session" in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"every 2h"}}'
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
    tool.created({"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields})


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
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert "a job cannot deliver its reply" in message
    assert "Say in prompt how the result should reach the user" in message


def test_refusal_names_other_fields_the_corrected_call_drops(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "every 2h",
            "deliver": "origin",
            "skills": ["ops"],
            "max_delay": "1h",
        },
    )

    assert '"skills" is not a parameter.' in message
    assert '"max_delay" has no effect.' in message


# -- Claude Code, Hermes, OpenClaw, scheduled-task Tools --------------------------------------


def test_claude_code_cron_create_shapes(tool: CronTool) -> None:
    recurring, _ = tool.created(
        {"cron": "0 9 * * 1-5", "prompt": JOB_PROMPT, "recurring": True, "durable": True}
    )
    tool.call({"action": "delete", "id": recurring.id})
    once, text = tool.created({"cron": "30 14 * * *", "prompt": JOB_PROMPT, "recurring": False})

    assert (recurring.cron_expression, recurring.remaining_runs) == ("0 9 * * 1-5", None)
    assert (once.cron_expression, once.remaining_runs) == ("30 14 * * *", 1)
    assert "repeat: 1" in text


def test_single_fire_of_an_interval_is_refused_with_both_readings(tool: CronTool) -> None:
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", "recurring": False}
    )

    assert '"schedule":"every 2h"}' in message and '"schedule":"in 2h"}' in message


def test_claude_code_cron_delete_shape_names_the_choices(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused({"id": job_id})

    for action in ("delete", "disable", "enable", "list"):
        assert f'{{"action":"{action}","id":"{job_id}"}}' in message


@pytest.mark.parametrize(
    ("action", "status"),
    [("pause", "paused"), ("suspend", "paused"), ("resume", "active"), ("remove", None)],
)
def test_hermes_actions_with_job_id(tool: CronTool, action: str, status: str | None) -> None:
    job_id = tool.add_job()
    if action == "resume":
        tool.call({"action": "disable", "id": job_id})

    envelope, _text = tool.call({"action": action, "job_id": job_id})

    assert envelope["ok"] is True
    assert [job.status for job in tool.jobs()] == ([status] if status else [])


def test_run_now_is_refused_with_a_one_time_job(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused({"action": "run", "job_id": job_id})

    assert "a job cannot be fired on demand" in message
    assert '"schedule":"in 1m"' in message
    tool.trigger.trigger_run.assert_not_called()


@pytest.mark.parametrize("word", ["stop", "cancel"])
def test_stop_words_name_pause_and_delete(tool: CronTool, word: str) -> None:
    job_id = tool.add_job()

    message = tool.refused({"action": word, "id": job_id})

    assert f'{{"action":"disable","id":"{job_id}"}}' in message
    assert f'{{"action":"delete","id":"{job_id}"}}' in message


def test_openclaw_job_object(tool: CronTool) -> None:
    job, _text = tool.created(
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
    message = tool.refused(
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
    job, _text = tool.created(
        {
            "action": "create",
            "cronExpression": "0 8 * * *",
            "prompt": JOB_PROMPT,
            "description": "Daily digest",
        },
    )

    assert job.name == "Daily digest"


# -- actions, placeholders and fields that do not fit their action ----------------------------


def test_missing_action_is_inferred(tool: CronTool) -> None:
    job, _text = tool.created({"prompt": JOB_PROMPT, "schedule": "every 2h"})
    tool.call({"id": job.id, "schedule": "every 5h"})

    assert tool.only_job().interval_seconds == 18000


def test_create_with_an_id_names_update_and_create(tool: CronTool) -> None:
    message = tool.refused(
        {"action": "create", "id": "daily-digest", "prompt": JOB_PROMPT, "schedule": "0 8 * * *"}
    )

    assert '{"action":"update","id":"daily-digest",' in message
    assert f'{{"action":"create","prompt":"{JOB_PROMPT}","schedule":"0 8 * * *"}}' in message


def test_update_without_id_names_create(tool: CronTool) -> None:
    message = tool.refused({"action": "update", "prompt": JOB_PROMPT, "schedule": "0 8 * * *"})

    assert f'{{"action":"create","prompt":"{JOB_PROMPT}","schedule":"0 8 * * *"}}' in message


def test_list_with_job_fields_offers_list_or_create(tool: CronTool) -> None:
    message = tool.refused({"action": "list", "prompt": JOB_PROMPT, "schedule": "every 2h"})

    assert message == (
        "cron was not run: list only shows jobs (optionally one job by id); it takes no prompt, "
        'schedule. To show jobs, leave them out; to make a job, use create: {"action":"list"} '
        f'or {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"every 2h"}}'
    )


def test_delete_with_changes_offers_delete_or_update(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused({"action": "delete", "id": job_id, "schedule": "every 3h"})

    assert message.endswith(
        f'Either delete it or change it: {{"action":"delete","id":"{job_id}"}} or '
        f'{{"action":"update","id":"{job_id}","schedule":"every 3h"}}'
    )


def test_list_with_an_id_and_changes_offers_list_or_update(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused({"action": "list", "id": job_id, "schedule": "every 3h"})

    assert message.endswith(
        f'to change job "{job_id}", use update: {{"action":"list","id":"{job_id}"}} or '
        f'{{"action":"update","id":"{job_id}","schedule":"every 3h"}}'
    )


@pytest.mark.parametrize("prompt", ["<instruction>", "placeholder", "TBD"])
def test_placeholder_prompt_is_refused(tool: CronTool, prompt: str) -> None:
    message = tool.refused({"action": "create", "prompt": prompt, "schedule": "every 2h"})

    assert message == (
        f'cron was not run: prompt "{prompt}" is a placeholder. Send the complete instruction '
        "the Agent should run at each fire."
    )


def test_stand_in_prompt_on_update_is_refused_and_keeps_the_job(tool: CronTool) -> None:
    job_id = tool.add_job()

    message = tool.refused(
        {"action": "update", "id": job_id, "prompt": "<the prompt from this call>"}
    )

    assert tool.only_job().prompt == JOB_PROMPT
    assert message == (
        'cron was not run: prompt "<the prompt from this call>" is a stand-in. Send the '
        "complete instruction the Agent should run at each fire in its place, or leave prompt "
        "out to keep the job's current one."
    )


@pytest.mark.parametrize(
    ("call", "default"),
    [
        (
            {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h"},
            "run the job as yourself",
        ),
        ({"action": "update", "schedule": "every 3h"}, "keep the job's current target"),
    ],
)
def test_stand_in_target_is_refused(tool: CronTool, call: dict[str, Any], default: str) -> None:
    if call["action"] == "update":
        call = {**call, "id": tool.add_job()}

    message = tool.refused({**call, "target": "<agent or agent@project>"})

    assert message == (
        'cron was not run: target "<agent or agent@project>" is a stand-in. Send an existing '
        "Agent id, or agent@project for a Project member, in its place, or leave target out "
        f"to {default}."
    )


def test_placeholder_fields_are_omitted(tool: CronTool) -> None:
    job, _text = tool.created(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "every 2h",
            "target": "self",
            "name": "<name>",
        },
    )

    assert (job.agent_id, job.name) == ("agent-one", JOB_PROMPT)


@pytest.mark.parametrize("word", ["self", "me", "current"])
def test_self_target_on_update_moves_the_job_to_the_calling_agent(
    tool: CronTool, word: str
) -> None:
    job_id = tool.add_job(target="builder@vbot")

    alone, _ = tool.call({"action": "update", "id": job_id, "target": word})
    assert alone["ok"] is True
    assert (tool.only_job().agent_id, tool.only_job().project_id) == ("agent-one", None)

    tool.call({"action": "update", "id": job_id, "target": "builder@vbot"})
    with_schedule, text = tool.call(
        {"action": "update", "id": job_id, "target": word, "schedule": "every 3h"}
    )

    job = tool.only_job()
    assert with_schedule["ok"] is True
    assert (job.agent_id, job.interval_seconds) == ("agent-one", 3 * 3600)
    assert "target: agent-one" in text


def test_repeat_words_and_invalid_counts(tool: CronTool) -> None:
    job_id = tool.add_job(repeat=3)

    tool.call({"action": "update", "id": job_id, "repeat": "unlimited"})
    message = tool.refused({"action": "update", "id": job_id, "repeat": 0})

    assert tool.only_job().remaining_runs is None
    assert "must be 1 or more" in message


@pytest.mark.parametrize(
    ("fields", "note"),
    [
        ({"max_delay": "2h"}, '"max_delay" has no effect.'),
        ({"maxDelay": "PT30M"}, '"maxDelay" has no effect.'),
        ({"misfire_grace_time": 3600}, '"misfire_grace_time" has no effect.'),
        (
            {"catch_up_missed": False, "skipMissedJobs": "true"},
            '"catch_up_missed" and "skipMissedJobs" have no effect.',
        ),
        ({"max_delay": None}, None),
        ({"max_delay": "none"}, None),
    ],
)
def test_late_start_limits_have_no_effect_and_the_result_says_so(
    tool: CronTool, fields: dict[str, Any], note: str | None
) -> None:
    envelope, text = tool.call(
        {"action": "create", "prompt": "Brief", "schedule": "0 7 * * *", **fields}
    )

    assert envelope["ok"] is True, text
    data = envelope["data"]
    assert "max_delay" not in data
    if note is None:
        assert "note" not in data
    else:
        assert data["note"].startswith(note)
