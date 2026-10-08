"""Tests for the ``vbot cron`` commands: schedule options, RPC requests and output."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

CREATE = ("cron", "create", "assistant", "--name", "Morning news", "--prompt", "Check the news")
CREATED = {"agent_id": "assistant", "prompt": "Check the news", "name": "Morning news"}


@pytest.mark.parametrize(
    ("options", "schedule"),
    [
        pytest.param(
            ("--cron", "0 9 * * *", "--session", "session-one"),
            {"schedule_type": "cron", "cron_expression": "0 9 * * *", "session_id": "session-one"},
            id="recurring-in-session",
        ),
        pytest.param(
            ("--every", "15", "--repeat", "3"),
            {"schedule_type": "interval", "interval_seconds": 900, "repeat": 3},
            id="interval-minutes",
        ),
        pytest.param(
            ("--at", "2026-07-01T09:00:00+00:00"),
            {"schedule_type": "once", "run_at": "2026-07-01T09:00:00+00:00"},
            id="once",
        ),
        # The server reads the event time.
        pytest.param(
            ("--event", "evt_1", "--event-time", "start - 30m"),
            {"schedule_type": "event", "event_id": "evt_1", "event_time": "start - 30m"},
            id="event",
        ),
    ],
)
def test_cron_create_sends_the_schedule_fields(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], schedule: dict[str, Any]
) -> None:
    rpc.reply("cron.create", {"id": "job-1"})

    code, out, _err = run_cli(*CREATE, *options)

    assert code == 0
    assert rpc.calls == [("cron.create", CREATED | schedule)]
    assert "job-1" in out


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(
            ("--cron", "0 9 * * *", "--at", "2026-07-01T09:00:00+00:00"), id="cron-and-at"
        ),
        pytest.param((), id="no-schedule"),
        pytest.param(("--cron", "0 9 * * *", "--timezone", "Europe/Berlin"), id="per-job-timezone"),
    ],
)
def test_cron_create_rejects_invalid_schedule_options(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli(*CREATE, *options)

    assert exc_info.value.code == 2
    assert rpc.calls == []


def test_cron_create_refuses_an_event_time_without_an_event(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, _err = run_cli(*CREATE, "--cron", "0 9 * * *", "--event-time", "end")

    assert code == 1
    assert "--event" in out
    assert rpc.calls == []


def test_cron_create_confirms_the_target_schedule_and_next_fire(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "cron.create",
        {
            "id": "job-1",
            "agent_id": "builder",
            "project_id": "vbot",
            "target": "builder@vbot",
            "name": "Build check",
            "prompt": "Check the build",
            "schedule_type": "cron",
            "cron_expression": "0 9 * * *",
            "run_at": None,
            "status": "active",
            "next_fire_at": "2026-07-21T07:00:00+00:00",
            "last_outcome": None,
        },
    )

    code, out, _err = run_cli(
        "cron", "create", "builder@vbot", "--name", "Build check", "--prompt", "Check the build",
        "--cron", "0 9 * * *",
    )  # fmt: skip

    assert code == 0
    assert rpc.params("cron.create")["agent_id"] == "builder@vbot"
    for text in (
        "name=Build check",
        "agent=builder@vbot",
        "next_fire_at=2026-07-21T07:00:00+00:00",
    ):
        assert text in out


def test_cron_list_prints_one_row_per_job(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "cron.list",
        {
            "jobs": [
                {
                    "id": "job-1",
                    "agent_id": "assistant",
                    "name": "Morning news",
                    "prompt": "Check the news",
                    "schedule_type": "cron",
                    "cron_expression": "0 9 * * *",
                    "run_at": None,
                    "status": "active",
                    "next_fire_at": "2026-06-12T07:00:00+00:00",
                },
                {
                    "id": "job-2",
                    "agent_id": "coder",
                    "name": "One-time audit",
                    "prompt": "A" * 100,
                    "schedule_type": "once",
                    "cron_expression": None,
                    "run_at": "2026-07-01T09:00:00+00:00",
                    "status": "paused",
                    "next_fire_at": None,
                },
                {
                    "id": "job-3",
                    "agent_id": "assistant",
                    "name": "Prepare standup",
                    "prompt": "Prepare the standup",
                    "schedule_type": "event",
                    "schedule": "start - 30m",
                    "event_id": "evt_1",
                    "status": "active",
                    "next_fire_at": "2026-06-15T06:30:00+00:00",
                },
            ]
        },
    )

    code, out, _err = run_cli("cron", "list")

    assert code == 0
    assert rpc.calls == [("cron.list", {})]
    assert out.splitlines()[1:] == [
        "- name=Morning news id=job-1 agent=assistant status=active "
        "schedule=cron[0 9 * * *] remaining_runs=unlimited "
        "next_fire_at=2026-06-12T07:00:00+00:00 session=new last_outcome=- "
        "last_error=- prompt=Check the news",
        "- name=One-time audit id=job-2 agent=coder status=paused "
        "schedule=once[2026-07-01T09:00:00+00:00] remaining_runs=1 "
        "next_fire_at=- session=new last_outcome=- last_error=- "
        "prompt=" + "A" * 57 + "...",
        "- name=Prepare standup id=job-3 agent=assistant status=active "
        "schedule=event[evt_1 start - 30m] remaining_runs=unlimited "
        "next_fire_at=2026-06-15T06:30:00+00:00 session=new last_outcome=- "
        "last_error=- prompt=Prepare the standup",
    ]


def test_cron_list_reports_the_empty_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("cron.list", {"jobs": []})

    code, out, _err = run_cli("cron", "list")

    assert code == 0
    assert out.strip()


@pytest.mark.parametrize(
    "jobs",
    [
        pytest.param([], id="no-jobs"),
        pytest.param([{"id": "another"}], id="other-job"),
        pytest.param(
            [{"id": "wanted", "prompt": "long prompt " * 40, "session_id": "pinned"}],
            id="exact-job",
        ),
    ],
)
def test_cron_show_prints_exactly_the_complete_saved_job(
    rpc: FakeRpc, run_cli: RunCli, jobs: list[dict[str, Any]]
) -> None:
    rpc.reply("cron.list", {"jobs": jobs})

    code, out, _err = run_cli("cron", "show", "wanted")

    assert rpc.calls == [("cron.list", {})]
    if jobs and jobs[0]["id"] == "wanted":
        assert (code, json.loads(out)) == (0, jobs[0])
    else:
        assert code == 1


@pytest.mark.parametrize(
    ("options", "changes"),
    [
        pytest.param(
            ("--cron", "30 7 * * 1-5"),
            {"schedule_type": "cron", "cron_expression": "30 7 * * 1-5"},
            id="schedule",
        ),
        pytest.param(("--status", "paused"), {"status": "paused"}, id="status"),
        pytest.param(
            ("--event", "evt_1", "--event-time", "end + 1h"),
            {"schedule_type": "event", "event_id": "evt_1", "event_time": "end + 1h"},
            id="event",
        ),
        # A job already bound to an event keeps it.
        pytest.param(("--event-time", "end"), {"event_time": "end"}, id="event-time"),
    ],
)
def test_cron_update_sends_only_the_given_changes(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], changes: dict[str, Any]
) -> None:
    rpc.reply("cron.update", {"ok": True})

    code, out, _err = run_cli("cron", "update", "job-1", *options)

    assert code == 0
    assert rpc.calls == [("cron.update", {"id": "job-1", **changes})]
    assert "job-1" in out


def test_cron_update_without_changes_names_every_option(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, _err = run_cli("cron", "update", "job-1")

    assert code == 1
    assert rpc.calls == []
    for option in (
        "--agent",
        "--name",
        "--prompt",
        "--cron",
        "--every",
        "--at",
        "--event",
        "--event-time",
        "--repeat",
        "--session",
        "--status",
    ):
        assert option in out


@pytest.mark.parametrize("command", ["delete", "enable", "disable"])
def test_cron_job_commands_address_the_job_by_id(
    rpc: FakeRpc, run_cli: RunCli, command: str
) -> None:
    rpc.reply(f"cron.{command}", {"ok": True})

    code, out, _err = run_cli("cron", command, "job-1")

    assert code == 0
    assert rpc.calls == [(f"cron.{command}", {"id": "job-1"})]
    assert "job-1" in out
