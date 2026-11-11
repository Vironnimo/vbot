"""Real dispatch executes clear ``cron`` intent written in other shapes.

vBot's earlier cron schemas, other harness dialects, action synonyms and
placeholders reach exactly the job the call meant. Each repair is paired with a
nearby call that means something else and is refused, before any job changes,
with the corrected call. Schedules and time zones: ``test_cron_call_times.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from core.automation.cron import CronWatch
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


@pytest.mark.parametrize(
    ("fields", "session_id"),
    [
        pytest.param({"session_id": ""}, None, id="empty"),
        pytest.param({"session_id": "new"}, None, id="new"),
        pytest.param({"sessionTarget": "isolated"}, None, id="openclaw-isolated"),
        pytest.param({"session": "none"}, None, id="none"),
        pytest.param({"session_id": "sess_abc"}, "sess_abc", id="session-id"),
        pytest.param({"sessionTarget": "main"}, "session-one", id="openclaw-main"),
        pytest.param({"sessionKey": "this"}, "session-one", id="this"),
    ],
)
def test_session_spellings_choose_a_new_or_a_continued_session(
    tool: CronTool, fields: dict[str, Any], session_id: str | None
) -> None:
    job, _text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert job.session_id == session_id


@pytest.mark.parametrize("session", [{"id": "sess_abc"}, "<session id>"])
def test_a_session_that_names_no_session_is_refused(tool: CronTool, session: Any) -> None:
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", "session": session}
    )

    assert '"session" takes current for this conversation or a Session id' in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"every 2h"}}'
    )


@pytest.mark.parametrize(
    ("fields", "delivery"),
    [
        pytest.param({"deliver": "local"}, None, id="hermes-local"),
        pytest.param({"delivery": {"mode": "none"}}, None, id="openclaw-none"),
        pytest.param({"notifyOnCompletion": False}, None, id="no-notification"),
        pytest.param({"deliver": ""}, "app", id="empty"),
        pytest.param({"deliver": "origin"}, "app", id="hermes-origin"),
        pytest.param({"delivery": {"mode": "announce"}}, "app", id="openclaw-announce"),
        pytest.param({"notifyOnCompletion": True}, "app", id="notification"),
        pytest.param({"deliver": "vBot"}, "app", id="vbot"),
        pytest.param({"durable": True}, "app", id="durable"),
        pytest.param({"durable": False}, "app", id="not-durable"),
    ],
)
def test_delivery_spellings_reach_this_conversation_or_nowhere(
    tool: CronTool, fields: dict[str, Any], delivery: str | None
) -> None:
    # Outside a chat, this conversation is vBot.
    job, _text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert (job.delivery and job.delivery.kind) == delivery


@pytest.mark.parametrize(
    ("fields", "cause"),
    [
        pytest.param(
            {"deliver": "telegram:-100123"},
            'deliver "telegram:-100123" names another chat',
            id="hermes-chat",
        ),
        pytest.param(
            {"delivery": {"mode": "announce", "channel": "telegram", "to": "-100123"}},
            'deliver "telegram:-100123" names another chat',
            id="openclaw-chat",
        ),
        pytest.param(
            {"deliver": "email"}, 'deliver "email" is not a delivery target', id="unknown"
        ),
    ],
)
def test_delivery_elsewhere_is_refused_with_the_targets_a_job_has(
    tool: CronTool, fields: dict[str, Any], cause: str
) -> None:
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert cause in message
    assert '"deliver":"here"}' in message and '"deliver":"app"}' in message


_STATUS_URL = "https://status.example/health"


@pytest.mark.parametrize(
    ("fields", "watch"),
    [
        pytest.param(
            {"monitor_url": "status.example/health"},
            CronWatch("url", _STATUS_URL),
            id="hermes-monitor-url",
        ),
        pytest.param({"watch": {"url": _STATUS_URL}}, CronWatch("url", _STATUS_URL), id="rpc"),
        pytest.param(
            {"watch": f"curl -s {_STATUS_URL}"},
            CronWatch("command", f"curl -s {_STATUS_URL}"),
            id="command-with-url",
        ),
        pytest.param(
            {"watch_command": "git status --short"},
            CronWatch("command", "git status --short"),
            id="watch-command",
        ),
        pytest.param({"watch": "none"}, None, id="none"),
        pytest.param({"watch": ""}, None, id="empty"),
    ],
)
def test_watch_spellings_choose_what_the_job_checks(
    tool: CronTool, fields: dict[str, Any], watch: CronWatch | None
) -> None:
    job, _text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert job.watch == watch


@pytest.mark.parametrize(
    ("fields", "cause", "send"),
    [
        pytest.param(
            {"monitor_script": "check_price.py"},
            '"monitor_script" is not available: a job runs no script of its own',
            True,
            id="hermes-monitor-script",
        ),
        pytest.param(
            {"script": "digest.py", "no_agent": True},
            '"no_agent" is not available',
            True,
            id="hermes-script-job",
        ),
        pytest.param(
            {"trigger": {"script": "return {fire: true}"}},
            '"trigger" is not available',
            True,
            id="openclaw-trigger",
        ),
        pytest.param(
            {"watch": "<command>"}, 'watch "<command>" is a stand-in', False, id="stand-in"
        ),
    ],
)
def test_a_check_vbot_cannot_run_is_refused_with_watch(
    tool: CronTool, fields: dict[str, Any], cause: str, send: bool
) -> None:
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
    )

    assert cause in message
    _reason, separator, corrected = message.partition(" Send: ")
    assert bool(separator) == send
    if send:
        assert json.loads(corrected) == {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "every 2h",
            "watch": "<command or URL to check>",
            "watch_runs": "always",
        }


def test_refusal_names_other_fields_the_corrected_call_drops(tool: CronTool) -> None:
    message = tool.refused(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "every 2h",
            "deliver": "telegram:-100123",
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


@pytest.mark.parametrize("key", ["description", "summary", "details"])
@pytest.mark.parametrize("label", ["Daily digest", "a" * 80, "a" * 81, "Daily\ndigest"])
def test_scheduled_task_spelling_uses_a_short_description_as_name(
    tool: CronTool, key: str, label: str
) -> None:
    job, _text = tool.created(
        {
            "action": "create",
            "cronExpression": "0 8 * * *",
            "prompt": JOB_PROMPT,
            key: label,
        },
    )

    assert job.name == (label if len(label) <= 80 and "\n" not in label else JOB_PROMPT)


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
        "cron was not run: list only shows jobs (one job by id, or the jobs of an event_id); it "
        "takes no prompt, schedule. To show jobs, leave them out; to make a job, use create: "
        '{"action":"list"} or '
        f'{{"action":"create","prompt":"{JOB_PROMPT}","schedule":"every 2h"}}'
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


@pytest.mark.parametrize("watch_runs", [None, "", "<on_change or always>"])
def test_placeholder_fields_are_omitted(tool: CronTool, watch_runs: str | None) -> None:
    job, _text = tool.created(
        {
            "action": "create",
            "prompt": JOB_PROMPT,
            "schedule": "every 2h",
            "target": "self",
            "name": "<name>",
            "watch": None,
            "watch_runs": watch_runs,
        },
    )

    assert (job.agent_id, job.name, job.watch) == ("agent-one", JOB_PROMPT, None)


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


@pytest.mark.parametrize("repeat", [0, None, "unlimited"])
def test_repeat_words_and_invalid_counts(tool: CronTool, repeat: Any) -> None:
    tool.add_job(repeat=repeat)
    assert tool.only_job().remaining_runs is None
    tool.succeeded({"action": "delete", "id": tool.only_job().id})
    job_id = tool.add_job(repeat=3)

    tool.succeeded({"action": "update", "id": job_id, "repeat": repeat})
    assert tool.only_job().remaining_runs is None
    tool.refused({"action": "update", "id": job_id, "repeat": -1})
    message = tool.refused({"action": "update", "id": job_id, "schedule": "10m", "repeat": -1})
    corrections = [json.loads(text) for text in re.findall(r"\{[^{}]+\}", message)]
    assert corrections and all(
        item["repeat"] == "<run count, or 0 for no limit>" for item in corrections
    )
    tool.succeeded({**corrections[-1], "repeat": 2})
    assert tool.only_job().remaining_runs == 2


@pytest.mark.parametrize("schedule", ["in 10m", "10m"])
def test_corrected_calls_keep_execution_choices(tool: CronTool, schedule: str) -> None:
    choices = {"working_project": "vbot", "model": "test/model", "thinking_effort": "high"}
    call = {"action": "create", "prompt": JOB_PROMPT, "schedule": schedule, "repeat": 3, **choices}
    message = tool.refused(call)
    corrections = [json.loads(text) for text in re.findall(r"\{[^{}]+\}", message)]
    assert corrections
    for corrected in corrections:
        assert all(corrected[key] == value for key, value in choices.items())
    corrected = corrections[-1]
    job, _ = tool.created(corrected)
    assert job.working_project_id == "vbot"
    assert job.run_overrides is not None
    assert job.run_overrides.as_dict() == {"model": "test/model", "thinking_effort": "high"}


@pytest.mark.parametrize("schedule", ["in 10m", "webhook", "email"])
def test_changing_a_watched_job_offers_to_stop_the_stored_watch(
    tool: CronTool, schedule: str
) -> None:
    job_id = tool.add_job(watch="https://status.example/", watch_runs="always")
    fields = {"email_channel": "mail-main"} if schedule == "email" else {}
    call = {"action": "update", "id": job_id, "schedule": schedule, **fields}
    message = tool.refused(call)
    corrections = [json.loads(text) for text in re.findall(r"\{[^{}]+\}", message)]
    assert {**call, "watch": "none"} in corrections
    repeating = next(item for item in corrections if item["schedule"] == "<repeating schedule>")
    tool.succeeded({**repeating, "schedule": "every 3h"})
    assert tool.only_job().watch is not None
    explicit = tool.refused({**call, "watch": "https://status.example/"})
    assert '"watch":"none"' not in explicit
    tool.succeeded(next(corrected for corrected in corrections if corrected.get("watch") == "none"))
    job = tool.only_job()
    assert job.watch is None
    assert job.schedule_type == ("once" if schedule == "in 10m" else schedule)


@pytest.mark.parametrize("action", ["create", "update"])
def test_invalid_effort_offers_tool_choices_and_preserves_other_fields(
    tool: CronTool, action: str
) -> None:
    fields = {"model": "test/model", "working_project": "vbot"}
    call = (
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "every 2h", **fields}
        if action == "create"
        else {"action": "update", "id": tool.add_job(), **fields}
    )
    message = tool.refused({**call, "thinking_effort": "ultra"})
    assert "''" not in message
    assert "default" in message
    assert ("inherit" in message) == (action == "update")
    corrected = json.loads(message.split(" Send: ", 1)[1])
    assert {key: value for key, value in corrected.items() if key != "thinking_effort"} == call
    corrected["thinking_effort"] = "default" if action == "create" else "inherit"
    tool.succeeded(corrected)
    overrides = tool.only_job().run_overrides
    assert overrides is not None
    assert overrides.as_dict() == (
        {"model": "test/model", "thinking_effort_default": True}
        if action == "create"
        else {"model": "test/model"}
    )


@pytest.mark.parametrize("field", ["limit", "offset"])
def test_list_reports_pagination_fields_without_effect(tool: CronTool, field: str) -> None:
    tool.add_job(name="First")
    tool.add_job(name="Second")
    result, _ = tool.call({"action": "list", field: 1})
    assert result["ok"] is True
    assert result["data"]["jobs"] == 2
    assert field in result["data"]["note"]


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


# -- webhook triggers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"schedule": "Webhook"}, id="schedule"),
        pytest.param({"type": "webhook"}, id="type"),
        pytest.param({"schedule": {"kind": "hook"}}, id="schedule-object"),
        # A webhook job keeps no time, so a time zone changes nothing.
        pytest.param({"schedule_type": "webhook", "timezone": "America/New_York"}, id="zone"),
    ],
)
def test_webhook_spellings_create_a_webhook_job(tool: CronTool, fields: dict[str, Any]) -> None:
    job, text = tool.created({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert job.schedule_type == "webhook"
    assert "note" not in text


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"schedule": "Email", "email_channel": "mail-main"}, id="schedule"),
        pytest.param(
            {"type": "mail", "channel": "mail-main", "from": "ann@example.com, @partner.org"},
            id="type-and-short-names",
        ),
        pytest.param(
            {"schedule": "on_email", "email_channel_id": "mail-main", "subject": "Invoice"},
            id="subject",
        ),
        # An email job keeps no time, so a time zone changes nothing.
        pytest.param(
            {"schedule": "email", "email_channel": "mail-main", "timezone": "America/New_York"},
            id="zone",
        ),
    ],
)
def test_email_spellings_create_an_email_job(tool: CronTool, fields: dict[str, Any]) -> None:
    job, text = tool.created({"action": "create", "prompt": JOB_PROMPT, **fields})

    assert job.schedule_type == "email"
    assert job.email is not None and job.email.channel_id == "mail-main"
    if "from" in fields:
        assert job.email.senders == ("ann@example.com", "@partner.org")
    if "subject" in fields:
        assert job.email.subject == "Invoice"
    assert "note" not in text


def test_a_webhook_route_of_another_harness_keeps_vbots_secret_and_no_event_filter(
    tool: CronTool,
) -> None:
    job, text = tool.created(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "webhook", "secret": "s3cret"}
    )
    message = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "schedule": "webhook", "events": ["push"]}
    )
    timed = tool.refused(
        {"action": "create", "prompt": JOB_PROMPT, "type": "webhook", "schedule": "every 2h"}
    )

    assert job.schedule_type == "webhook"
    assert "s3cret" not in text
    assert 'note: "secret" had no effect' in text
    assert '"events" is not available' in message
    assert message.endswith(
        f'Send: {{"action":"create","prompt":"{JOB_PROMPT}","schedule":"webhook"}}'
    )
    assert 'the schedule "every 2h" is not a webhook schedule' in timed
