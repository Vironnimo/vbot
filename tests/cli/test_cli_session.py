"""Tests for the ``vbot session`` commands: RPC requests, paging and printed output."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

DEFAULT_LIST = {
    "agent_id": "assistant",
    "limit": 100,
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
                },
                {
                    "id": "session-two",
                    "created_at": "2026-06-03T10:00:00+00:00",
                    "last_active_at": "2026-06-03T11:00:00+00:00",
                    "source_channel_id": "tg-main",
                },
            ]
        },
    )

    code, out, _err = run_cli("session", "list", "assistant")

    assert code == 0
    assert rpc.calls == [("session.list", DEFAULT_LIST)]
    assert out.splitlines()[1:] == [
        "- id=session-one created_at=2026-06-01T08:00:00+00:00 "
        "last_active_at=2026-06-02T09:00:00+00:00",
        "- id=session-two created_at=2026-06-03T10:00:00+00:00 "
        "last_active_at=2026-06-03T11:00:00+00:00 channel=tg-main",
    ]


def test_session_list_reports_the_empty_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("session.list", {"sessions": []})

    code, out, _err = run_cli("session", "list", "assistant")

    assert code == 0
    assert "assistant" in out


def test_session_list_reads_one_page_unless_all_pages_are_requested(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    cursor = {"last_active_at": "2026-01-01", "id": "s1"}
    first_page = {"sessions": [{"id": "s1"}], "next_cursor": cursor, "total_count": 2}
    rpc.reply("session.list", first_page)

    code, out, _err = run_cli("session", "list", "builder@project")

    assert code == 0
    # The server parses a project-qualified address; the CLI forwards it verbatim.
    assert rpc.calls == [("session.list", DEFAULT_LIST | {"agent_id": "builder@project"})]
    assert '"id": "s1"' in out and "s2" not in out

    rpc.calls.clear()
    rpc.reply("session.list", {"sessions": [{"id": "s2"}], "next_cursor": None, "total_count": 2})

    code, out, _err = run_cli("session", "list", "builder@project", "--all")

    assert code == 0
    assert [params.get("cursor") for _method, params in rpc.calls] == [None, cursor]
    assert "id=s1" in out and "id=s2" in out


def test_session_list_all_stops_at_a_repeated_cursor(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("session.list", {"sessions": [], "next_cursor": {"id": "same"}})

    code, _out, _err = run_cli("session", "list", "assistant", "--all")

    assert code == 1
    assert len(rpc.calls) == 2


def test_session_list_reports_a_domain_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("session.list", "not_found", "Unknown agent: missing", status=200)

    code, out, _err = run_cli("session", "list", "missing")

    assert code == 1
    assert "not_found: Unknown agent: missing" in out


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


def test_session_delete_requires_confirmation_before_any_request(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    code, out, _err = run_cli("session", "delete", "assistant", "session-one")

    assert code == 1
    assert "--yes" in out
    assert rpc.calls == []


def test_session_delete_archives_the_session_and_names_the_next_one(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("session.delete", {**SESSION, "next_session_id": "session-two"})

    code, out, _err = run_cli("session", "delete", "assistant", "session-one", "--yes")

    assert code == 0
    assert rpc.calls == [("session.delete", SESSION)]
    assert "archived" in out and "session-two" in out


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
