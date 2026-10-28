"""Tests for the ``vbot cron`` commands: schedule options, RPC requests and output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

CREATE = ("cron", "create", "assistant", "--name", "Morning news", "--prompt", "Check the news")
CREATED = {"agent_id": "assistant", "prompt": "Check the news", "name": "Morning news"}
HOOK_URL = "https://vbot.example.com/api/hooks/job-1"


def _webhook(reach: str = "internet") -> dict[str, Any]:
    return {
        "url": HOOK_URL,
        "path": "/api/hooks/job-1",
        "reach": reach,
        "last_received_at": None,
        "waiting": 0,
        "dropped": 0,
    }


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
        pytest.param(
            (
                "--every",
                "15",
                "--working-project",
                "builds",
                "--model",
                "test/model",
                "--thinking-effort",
                "low",
            ),
            {
                "schedule_type": "interval",
                "interval_seconds": 900,
                "working_project_id": "builds",
                "run_overrides": {"model": "test/model", "thinking_effort": "low"},
            },
            id="fixed-project-and-run-settings",
        ),
        pytest.param(
            ("--every", "15", "--no-project", "--thinking-effort", "default"),
            {
                "schedule_type": "interval",
                "interval_seconds": 900,
                "working_project_id": None,
                "run_overrides": {"thinking_effort_default": True},
            },
            id="workspace-and-model-default-reasoning",
        ),
        # The server reads the event time.
        pytest.param(
            ("--event", "evt_1", "--event-time", "start - 30m"),
            {"schedule_type": "event", "event_id": "evt_1", "event_time": "start - 30m"},
            id="event",
        ),
        pytest.param(
            (
                "--email",
                "mail-main",
                "--email-from",
                "ann@example.com",
                "--email-from",
                "@partner.org",
                "--email-subject",
                "Invoice",
            ),
            {
                "schedule_type": "email",
                "email": {
                    "channel_id": "mail-main",
                    "senders": ["ann@example.com", "@partner.org"],
                    "subject": "Invoice",
                },
            },
            id="email",
        ),
        pytest.param(
            ("--cron", "0 9 * * *", "--deliver", "app", "--watch", "https://example.com/status"),
            {
                "schedule_type": "cron",
                "cron_expression": "0 9 * * *",
                "delivery": {"kind": "app"},
                "watch": {"url": "https://example.com/status"},
            },
            id="app-delivery-and-page-watch",
        ),
        # A chat named alone can only mean delivery to it.
        pytest.param(
            (
                "--every",
                "15",
                "--channel",
                "telegram",
                "--chat",
                "-100123",
                "--watch",
                "git log -1",
            ),
            {
                "schedule_type": "interval",
                "interval_seconds": 900,
                "delivery": {
                    "kind": "chat",
                    "channel_id": "telegram",
                    "platform_target": "-100123",
                },
                "watch": {"command": "git log -1"},
            },
            id="chat-delivery-and-command-watch",
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


@pytest.mark.parametrize("command", ["create", "update"])
@pytest.mark.parametrize("valid_file", [True, False])
def test_cron_prompt_file_is_decoded_before_the_request(
    rpc: FakeRpc, run_cli: RunCli, tmp_path: Path, command: str, valid_file: bool
) -> None:
    prompt = 'Prüfe den Build.\nAntworte mit "fertig".\n'
    source = tmp_path / "prompt.txt"
    source.write_bytes(prompt.encode("utf-16") if valid_file else b"\xffnot-valid")
    rpc.reply(f"cron.{command}", {"id": "job-one"})
    target = ("assistant", "--every", "15") if command == "create" else ("job-one",)

    code, _out, _err = run_cli("cron", command, *target, "--file", str(source))

    assert code == (0 if valid_file else 1)
    if valid_file:
        assert rpc.params(f"cron.{command}")["prompt"] == prompt
    else:
        assert rpc.calls == []


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(
            ("--cron", "0 9 * * *", "--at", "2026-07-01T09:00:00+00:00"), id="cron-and-at"
        ),
        pytest.param((), id="no-schedule"),
        pytest.param(("--cron", "0 9 * * *", "--timezone", "Europe/Berlin"), id="per-job-timezone"),
        pytest.param(
            ("--every", "15", "--working-project", "builds", "--no-project"),
            id="conflicting-project",
        ),
    ],
)
def test_cron_create_rejects_invalid_schedule_options(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli(*CREATE, *options)

    assert exc_info.value.code == 2
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("options", "named"),
    [
        pytest.param(("--event-time", "end"), "--event", id="event-time-without-event"),
        pytest.param(("--deliver", "chat", "--channel", "telegram"), "--chat", id="chat-unnamed"),
        pytest.param(("--deliver", "app", "--chat", "-100123"), "--chat", id="chat-for-app"),
    ],
)
def test_cron_create_refuses_options_that_need_another(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], named: str
) -> None:
    code, out, _err = run_cli(*CREATE, "--cron", "0 9 * * *", *options)

    assert code == 1
    assert named in out
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
            "working_project_id": "vbot",
            "run_overrides": {"model": "test/model", "thinking_effort": "low"},
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
        "working_project=vbot",
        "model=test/model",
        "thinking_effort=low",
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
                    "delivery": {"kind": "app"},
                    "watch": {"url": "https://example.com/news"},
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
                    "delivery": {
                        "kind": "chat",
                        "channel_id": "telegram",
                        "platform_target": "-100123",
                        "thread_id": "7",
                    },
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
                {
                    "id": "job-1",
                    "agent_id": "assistant",
                    "name": "Triage issues",
                    "prompt": "Triage the issue",
                    "schedule_type": "webhook",
                    "status": "active",
                    "next_fire_at": None,
                    "webhook": _webhook(),
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
        "next_fire_at=2026-06-12T07:00:00+00:00 session=new delivery=app "
        "watch=url[https://example.com/news] last_outcome=- last_error=- prompt=Check the news",
        "- name=One-time audit id=job-2 agent=coder status=paused "
        "schedule=once[2026-07-01T09:00:00+00:00] remaining_runs=1 "
        "next_fire_at=- session=new delivery=chat[telegram -100123 7] watch=- "
        "last_outcome=- last_error=- prompt=" + "A" * 57 + "...",
        "- name=Prepare standup id=job-3 agent=assistant status=active "
        "schedule=event[evt_1 start - 30m] remaining_runs=unlimited "
        "next_fire_at=2026-06-15T06:30:00+00:00 session=new delivery=none watch=- "
        "last_outcome=- last_error=- prompt=Prepare the standup",
        "- name=Triage issues id=job-1 agent=assistant status=active "
        f"schedule=webhook[{HOOK_URL}] remaining_runs=unlimited "
        "next_fire_at=- session=new delivery=none watch=- last_outcome=- last_error=- "
        "prompt=Triage the issue",
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
            ("--no-project", "--clear-model", "--clear-thinking-effort"),
            {"working_project_id": None, "run_overrides": {"model": None, "thinking_effort": None}},
            id="clear-execution-overrides",
        ),
        pytest.param(
            ("--thinking-effort", "high"),
            {"run_overrides": {"thinking_effort": "high"}},
            id="effort-patch-keeps-model",
        ),
        pytest.param(
            ("--event", "evt_1", "--event-time", "end + 1h"),
            {"schedule_type": "event", "event_id": "evt_1", "event_time": "end + 1h"},
            id="event",
        ),
        # A job already bound to an event keeps it.
        pytest.param(("--event-time", "end"), {"event_time": "end"}, id="event-time"),
        pytest.param(
            ("--deliver", "chat", "--channel", "slack", "--chat", "C01", "--thread", "1712.5"),
            {
                "delivery": {
                    "kind": "chat",
                    "channel_id": "slack",
                    "platform_target": "C01",
                    "thread_id": "1712.5",
                }
            },
            id="chat-delivery",
        ),
        pytest.param(
            ("--deliver", "none", "--clear-watch"),
            {"delivery": {"kind": "none"}, "watch": None},
            id="no-delivery-no-watch",
        ),
        pytest.param(
            ("--watch", "git log -1", "--watch-runs", "always"),
            {"watch": {"command": "git log -1", "always": True}},
            id="watch-runs-always",
        ),
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


@pytest.mark.parametrize(
    ("stored", "options", "watch"),
    [
        pytest.param(
            {"command": "git log -1"},
            ("--watch-runs", "always"),
            {"command": "git log -1", "always": True},
            id="mode-only",
        ),
        pytest.param(
            {"command": "git log -1", "always": True},
            ("--watch", "git status"),
            {"command": "git status", "always": True},
            id="target-keeps-mode",
        ),
    ],
)
def test_cron_update_keeps_what_the_watch_options_leave_out(
    rpc: FakeRpc,
    run_cli: RunCli,
    stored: dict[str, Any],
    options: tuple[str, ...],
    watch: dict[str, Any],
) -> None:
    rpc.reply("cron.list", {"jobs": [{"id": "job-1", "watch": stored}]})
    rpc.reply("cron.update", {"ok": True})

    code, _out, _err = run_cli("cron", "update", "job-1", *options)

    assert code == 0
    assert rpc.calls[-1] == ("cron.update", {"id": "job-1", "watch": watch})


@pytest.mark.parametrize(
    ("options", "email"),
    [
        pytest.param(
            ("--email-subject", "Invoice"),
            {"channel_id": "mail-main", "senders": ["ann@example.com"], "subject": "Invoice"},
            id="subject-keeps-channel-and-senders",
        ),
        pytest.param(
            ("--email", "mail-other", "--clear-email-from"),
            {"channel_id": "mail-other", "senders": [], "subject": "Bill"},
            id="channel-and-any-sender",
        ),
    ],
)
def test_cron_update_keeps_what_the_email_options_leave_out(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], email: dict[str, Any]
) -> None:
    stored = {
        "channel_id": "mail-main",
        "senders": ["ann@example.com"],
        "subject": "Bill",
        "waiting": 2,
    }
    rpc.reply("cron.list", {"jobs": [{"id": "job-1", "email": stored}]})
    rpc.reply(
        "cron.update",
        {"id": "job-1", "schedule_type": "email", "email": {**email, "waiting": 2}},
    )

    code, out, _err = run_cli("cron", "update", "job-1", *options)

    assert code == 0
    assert rpc.calls[-1][1]["email"] == email
    assert f"schedule=email[{email['channel_id']}" in out
    assert "mails_waiting=2" in out


def test_email_filters_need_an_email_job(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("cron.list", {"jobs": [{"id": "job-1", "email": None}]})

    created, created_out, _err = run_cli(*CREATE, "--every", "15", "--email-subject", "Bill")
    updated, updated_out, _err = run_cli("cron", "update", "job-1", "--email-from", "@a.org")

    assert (created, updated) == (1, 1)
    assert "narrow an --email <channel-id> job" in created_out
    assert "cron job job-1 runs on no mail; add --email <channel-id>" in updated_out
    assert [method for method, _params in rpc.calls] == ["cron.list"]


def test_watch_runs_needs_a_watch(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("cron.list", {"jobs": [{"id": "job-1", "watch": None}]})

    created, created_out, _err = run_cli(*CREATE, "--every", "15", "--watch-runs", "always")
    updated, updated_out, _err = run_cli("cron", "update", "job-1", "--watch-runs", "always")

    assert (created, updated) == (1, 1)
    assert "--watch-runs needs --watch <command-or-url>" in created_out
    assert "cron job job-1 has none" in updated_out
    assert [method for method, _params in rpc.calls] == ["cron.list"]


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
        "--webhook",
        "--email",
        "--event-time",
        "--repeat",
        "--session",
        "--status",
        "--deliver",
        "--watch",
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


@pytest.mark.parametrize(
    ("command", "reach"),
    [
        pytest.param(CREATE, "internet", id="create"),
        # Only a caller on the same computer reaches it, so GitHub cannot.
        pytest.param(("cron", "update", "job-1"), "computer", id="update"),
    ],
)
def test_a_webhook_job_prints_its_url_and_secret(
    rpc: FakeRpc, run_cli: RunCli, command: tuple[str, ...], reach: str
) -> None:
    method = f"cron.{command[1]}"
    rpc.reply(method, {"id": "job-1", "schedule_type": "webhook", "webhook": _webhook(reach)})
    rpc.reply("cron.webhook_secret", {"secret": "vbw_s3cret", "webhook": _webhook(reach)})

    code, out, _err = run_cli(*command, "--webhook")

    assert code == 0
    assert rpc.calls[0][1]["schedule_type"] == "webhook"
    assert rpc.calls[1] == ("cron.webhook_secret", {"id": "job-1"})
    assert f"schedule=webhook[{HOOK_URL}]" in out
    assert f"url: {HOOK_URL}" in out
    assert "secret: vbw_s3cret" in out
    assert '"Authorization: Bearer vbw_s3cret"' in out
    assert ("cannot reach this URL" in out) is (reach != "internet")


def test_a_created_webhook_job_names_how_to_show_an_unreadable_secret(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("cron.create", {"id": "job-1", "schedule_type": "webhook", "webhook": _webhook()})
    rpc.fail("cron.webhook_secret", "storage_error", "The Secret Store is locked")

    code, out, _err = run_cli(*CREATE, "--webhook")

    assert code == 0
    assert "job-1" in out
    assert "vbot cron secret job-1" in out


@pytest.mark.parametrize(
    ("options", "method"),
    [
        pytest.param((), "cron.webhook_secret", id="show"),
        pytest.param(("--rotate",), "cron.rotate_webhook_secret", id="rotate"),
    ],
)
def test_cron_secret_shows_or_replaces_the_secret_with_the_url(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], method: str
) -> None:
    rpc.reply(method, {"secret": "vbw_s3cret", "webhook": _webhook()})

    code, out, _err = run_cli("cron", "secret", "job-1", *options)

    assert code == 0
    assert rpc.calls == [(method, {"id": "job-1"})]
    assert f"url: {HOOK_URL} (reachable from the internet)" in out
    assert "secret: vbw_s3cret" in out
    assert ("old secret" in out) is bool(options)
