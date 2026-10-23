"""Tests for the ``vbot session`` commands: RPC requests, paging and printed output."""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest

from cli import main
from cli._pagination import encode_cursor
from cli.parser import parse_args
from cli.session_management import SESSION_TEXT_BUDGET
from tests.cli.cli_test_support import FakeRpc, RunCli

DEFAULT_LIST = {
    "agent_id": "assistant",
    "limit": 40,
    "include_subagents": True,
    "include_memory_reflections": True,
    "include_skill_reflections": True,
    "include_cron": True,
}
SESSION = {"agent_id": "assistant", "session_id": "session-one"}


def test_session_list_prints_one_row_per_session(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "session.list",
        {
            "sessions": [
                {
                    "id": "session-one",
                    "created_at": "2026-06-01T08:00:00+00:00",
                    "last_active_at": "2026-06-02T09:00:00+00:00",
                    "title": "Zürich\nNext: injected\u202e",
                    "compaction_policy_override": None,
                    "compaction_policy_effective": {"mode": "test sentinel"},
                },
                {
                    "id": "session-two",
                    "created_at": "2026-06-03T10:00:00+00:00",
                    "last_active_at": "2026-06-03T11:00:00+00:00",
                    "source_channel_id": "tg-main",
                    "compaction_policy_override": {"mode": "test sentinel"},
                },
            ],
            "total_count": 2,
        },
    )

    code, out, _err = run_cli("session", "list", "assistant")

    assert code == 0
    assert rpc.calls == [("session.list", DEFAULT_LIST)]
    assert "showing 2 of 2" in out.splitlines()[0]
    rows = out.splitlines()[1:]
    assert len(rows) == 2
    assert all(f"id=session-{name}" in row for name, row in zip(("one", "two"), rows, strict=True))
    assert "compaction=inherited" in rows[0] and "compaction=override" in rows[1]
    assert "channel=tg-main" in rows[1]
    assert "last_active_at=2026-06-03T11:00:00+00:00" in rows[1]
    assert "test sentinel" not in out and "\u202e" not in out
    assert "\nNext: injected" not in out


def test_session_list_reports_the_empty_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("session.list", {"sessions": []})

    code, out, _err = run_cli("session", "list", "assistant")

    assert code == 0
    assert "assistant" in out


@pytest.mark.parametrize("all_pages", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_session_list_reads_one_page_unless_all_pages_are_requested(
    rpc: FakeRpc, run_cli: RunCli, all_pages: bool, json_output: bool
) -> None:
    cursor = {"last_active_at": "2026-01-01", "id": "s1"}
    first_page = {"sessions": [{"id": "s1"}], "next_cursor": cursor, "total_count": 2}
    rpc.reply("session.list", first_page)
    rpc.reply("session.list", {"sessions": [{"id": "s2"}], "next_cursor": None, "total_count": 2})
    flags = (*(("--all",) if all_pages else ()), *(("--json",) if json_output else ()))
    code, out, _err = run_cli("session", "list", "builder@project", *flags)

    assert code == 0
    # The server parses a project-qualified address; the CLI forwards it verbatim.
    assert rpc.calls[0] == ("session.list", DEFAULT_LIST | {"agent_id": "builder@project"})
    assert [params.get("cursor") for _method, params in rpc.calls] == (
        [None, cursor] if all_pages else [None]
    )
    if json_output:
        report = json.loads(out)
        assert report["ok"] and report["total_count"] == 2
        assert [session["id"] for session in report["sessions"]] == (
            ["s1", "s2"] if all_pages else ["s1"]
        )
        assert report["next_cursor"] == (None if all_pages else encode_cursor(cursor))
    else:
        assert "id=s1" in out and ("id=s2" in out) is all_pages
    if not all_pages:
        if json_output:
            tokens = [
                "session",
                "list",
                "builder@project",
                "--cursor",
                report["next_cursor"],
                "--json",
            ]
        else:
            tokens = shlex.split(
                next(
                    line.removeprefix("Next: ")
                    for line in out.splitlines()
                    if line.startswith("Next: ")
                )
            )[1:]
            followup = parse_args(tokens)
            assert followup.agent == "builder@project"
            assert followup.port == 8420 and followup.data_dir
        code, out, _err = run_cli(*tokens)
        assert code == 0 and "s2" in out
        assert rpc.calls[-1][1]["cursor"] == cursor


def test_session_list_all_stops_at_a_repeated_cursor(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("session.list", {"sessions": [], "next_cursor": {"id": "same"}})

    code, _out, _err = run_cli("session", "list", "assistant", "--all")

    assert code == 1
    assert len(rpc.calls) == 2


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("failure", ["domain", "records", "cursor"])
def test_session_list_reports_failures(
    rpc: FakeRpc, run_cli: RunCli, json_output: bool, failure: str
) -> None:
    if failure == "domain":
        rpc.fail("session.list", "not_found", "Unknown agent: missing", status=200)
    else:
        rpc.reply(
            "session.list",
            {"sessions": [{"title": "missing id"}]}
            if failure == "records"
            else {"sessions": [], "next_cursor": "not a cursor"},
        )

    code, out, _err = run_cli("session", "list", "missing", *(("--json",) if json_output else ()))

    assert code == 1
    assert ("not_found: Unknown agent: missing" in out) is (failure == "domain")
    if json_output:
        report = json.loads(out)
        assert report["ok"] is False and report["error"]["message"]
        if failure == "domain":
            assert report["error"]["code"] == "not_found"


@pytest.mark.parametrize("json_output", [False, True])
def test_session_list_bounds_text_without_skipping_sessions_and_keeps_json_complete(
    rpc: FakeRpc, run_cli: RunCli, json_output: bool
) -> None:
    rows = [
        {
            "id": f"sess_{index:012d}",
            "last_active_at": "2026-10-23T10:30:00.000000Z",
            "created_at": "2026-10-22T10:30:00.000000Z",
            "title": "Überblick zu den Projektarbeiten. " * 60,
            "activity_state": "waiting",
            "source_channel_id": "telegram-project-planning",
            "compaction_policy_override": None,
            "compaction_policy_effective": {"instructions": "long policy detail " * 150},
        }
        for index in range(40)
    ]
    cursors = []
    for size in (40, 20, 10):
        cursor = {
            "last_activity_at": rows[size - 1]["last_active_at"],
            "project_id": None,
            "agent_id": "assistant",
            "session_id": rows[size - 1]["id"],
        }
        cursors.append(cursor)
        rpc.reply(
            "session.list", {"sessions": rows[:size], "total_count": 70, "next_cursor": cursor}
        )

    code, out, _err = run_cli("session", "list", "assistant", *(("--json",) if json_output else ()))

    assert code == 0
    if json_output:
        report = json.loads(out)
        assert report["sessions"] == rows
        assert report["next_cursor"] == encode_cursor(cursors[0])
        assert len(rpc.calls) == 1
    else:
        assert len(out) <= SESSION_TEXT_BUDGET
        assert "Note:" in out and "previews" in out and "--json" in out
        assert "long policy detail" not in out
        assert len(rpc.calls) > 1
        assert all("cursor" not in params for _method, params in rpc.calls)
        shown = [line for line in out.splitlines() if line.startswith("- id=")]
        tokens = shlex.split(
            next(
                line.removeprefix("Next: ")
                for line in out.splitlines()
                if line.startswith("Next: ")
            )
        )[1:]
        followup = parse_args(tokens)
        assert followup.cursor["session_id"] == rows[len(shown) - 1]["id"]
        assert followup.limit == len(shown)
        assert f"showing {len(shown)} of 70" in out.splitlines()[0]


@pytest.mark.parametrize("cursor", ["v1.!bad", "v1.e30.invalid", "[]", "cursor-secret-sentinel"])
def test_session_list_refuses_invalid_cursors_without_echoing_the_value(
    cursor: str, capsys: Any
) -> None:
    with pytest.raises(SystemExit) as error:
        main.run(["session", "list", "assistant", "--cursor", cursor])
    assert error.value.code == 2
    assert cursor not in capsys.readouterr().err


@pytest.mark.parametrize("legacy", [False, True])
def test_session_list_accepts_opaque_and_existing_json_cursors(
    rpc: FakeRpc, run_cli: RunCli, legacy: bool
) -> None:
    cursor = {"last_activity_at": "2026-10-23T10:30:00Z", "session_id": "東京", "extra": {"v": 1}}
    rpc.reply("session.list", {"sessions": [], "next_cursor": None, "total_count": 0})
    code, _out, _err = run_cli(
        "session",
        "list",
        "assistant",
        "--cursor",
        json.dumps(cursor) if legacy else encode_cursor(cursor),
    )
    assert code == 0 and rpc.calls[0][1]["cursor"] == cursor


def test_session_list_json_resolution_failure_still_prints_one_document(capsys: Any) -> None:
    def resolve(**kwargs: Any) -> Any:
        raise OSError("test sentinel")

    code = main.run(["session", "list", "assistant", "--json"], resolve=resolve)
    out, _err = capsys.readouterr()
    report = json.loads(out)
    assert code == 1 and report["ok"] is False
    assert "test sentinel" in report["error"]["message"]


@pytest.mark.parametrize(
    ("options", "params", "session_id"),
    [
        pytest.param(
            ("--id", "session-two", "--make-current"),
            {"agent_id": "assistant", "session_id": "session-two", "make_current": True},
            "session-two",
            id="explicit",
        ),
        pytest.param((), {"agent_id": "assistant"}, "generated-id", id="server-generated"),
    ],
)
def test_session_create_sends_only_the_given_fields(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    session_id: str,
) -> None:
    rpc.reply("session.create", {"agent_id": "assistant", "session_id": session_id})

    code, out, _err = run_cli("session", "create", "assistant", *options)

    assert code == 0
    assert rpc.calls == [("session.create", params)]
    assert session_id in out and "assistant" in out


def test_session_permanent_delete_requires_confirmation_before_any_request(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    code, out, _err = run_cli("session", "delete", "assistant", "session-one", "--permanent")

    assert code == 1
    assert "--yes" in out
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("options", "params", "deleted", "shown"),
    [
        pytest.param(
            (),
            SESSION,
            {"archive_entry_id": "arc_3d8n0v6tz2kc", "purged": False},
            "deleted session session-one for assistant (archived as archive entry "
            "arc_3d8n0v6tz2kc); next session: session-two; restore with: "
            "vbot archive restore arc_3d8n0v6tz2kc",
            id="archived",
        ),
        pytest.param(
            ("--permanent", "--yes"),
            {**SESSION, "permanent": True},
            {"archive_entry_id": "arc_3d8n0v6tz2kc", "purged": True},
            "deleted session session-one for assistant permanently; next session: session-two",
            id="permanent",
        ),
    ],
)
def test_session_delete_archives_or_deletes_the_session_and_names_the_next_one(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    deleted: dict[str, Any],
    shown: str,
) -> None:
    rpc.reply(
        "session.delete",
        {**SESSION, "next_session_id": "session-two", "purge_pending": False} | deleted,
    )

    code, out, _err = run_cli("session", "delete", "assistant", "session-one", *options)

    assert code == 0
    assert rpc.calls == [("session.delete", params)]
    assert len(out.splitlines()) == 1
    assert out.startswith(shown)


def test_session_fork_keeps_the_qualified_target_agent(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "session.fork",
        {
            "session": {
                "id": "session-fork",
                "agent_id": "builder",
                "fork_source": {"session_id": "session-one"},
            }
        },
    )

    code, out, _err = run_cli(
        "session", "fork", "assistant", "session-one", "--target-agent", "builder@project"
    )

    assert code == 0
    assert rpc.calls == [("session.fork", {**SESSION, "target_agent_id": "builder@project"})]
    assert "session-fork" in out and "builder@project" in out


@pytest.mark.parametrize(
    ("option", "title", "shown"),
    [
        pytest.param(("--title", "Research notes"), "Research notes", "Research notes", id="set"),
        pytest.param(("--clear-title",), "", "(automatic)", id="clear"),
    ],
)
def test_session_rename_sets_or_clears_the_title(
    rpc: FakeRpc, run_cli: RunCli, option: tuple[str, ...], title: str, shown: str
) -> None:
    rpc.reply("session.rename", {**SESSION, "title": title})

    code, out, _err = run_cli("session", "rename", "assistant", "session-one", *option)

    assert code == 0
    assert rpc.calls == [("session.rename", {**SESSION, "title": title})]
    assert f"title: {shown}" in out


@pytest.mark.parametrize(
    ("option", "policy"),
    [
        pytest.param(("--policy", '{"enabled": false}'), {"enabled": False}, id="override"),
        pytest.param(("--clear",), None, id="clear"),
    ],
)
def test_session_set_compaction_policy_sends_the_override_and_prints_its_source(
    rpc: FakeRpc, run_cli: RunCli, option: tuple[str, ...], policy: dict[str, bool] | None
) -> None:
    rpc.reply(
        "session.set_compaction_policy",
        {**SESSION, "override": policy, "effective": {"enabled": False}, "source": "session"},
    )

    code, out, _err = run_cli(
        "session", "set-compaction-policy", "assistant", "session-one", *option
    )

    assert code == 0
    assert rpc.calls == [("session.set_compaction_policy", {**SESSION, "policy": policy})]
    assert "source: session" in out


def test_session_link_channel_links_the_conversation(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("session.link_channel", {"ok": True})

    code, out, _err = run_cli(
        "session", "link-channel", "assistant", "session-one",
        "--channel", "tg-main", "--conversation", "12345",
    )  # fmt: skip

    assert code == 0
    assert rpc.calls == [
        (
            "session.link_channel",
            {**SESSION, "channel_id": "tg-main", "platform_conv_id": "12345"},
        )
    ]
    for text in ("session-one", "tg-main", "12345"):
        assert text in out
