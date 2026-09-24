"""Live app operations: validation, partial effects, and canonical RPC routing."""

from __future__ import annotations

from collections.abc import Callable
from inspect import isawaitable
from typing import Any

import pytest

from server._live_tools import LiveToolExecutor, LiveUiError
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]


def terminal(terminal_id: str = "t1", **fields: Any) -> JsonObject:
    return {
        "terminal_id": terminal_id,
        "launch_command": "codex",
        "group_id": "g1",
        "screen_revision": 12,
        **fields,
    }


class FakeRpc:
    """In-process RPC double recording every method call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.handlers: dict[str, Callable[[JsonObject], Any]] = {
            "terminal.list": lambda _params: {
                "terminals": [terminal(), terminal("t2")],
                "groups": [{"group_id": "g1", "name": "Codex", "kind": "user"}],
                "launch_history": [{"command": "codex"}],
            },
            "terminal.start": lambda _params: {
                "terminal": terminal(f"new-{self.count('terminal.start')}")
            },
            "terminal.group.create": lambda _params: {"group": {"group_id": "new-group"}},
            "terminal.read": lambda _params: {
                "terminal": terminal(),
                "screen": "fixture",
                "bracketed_paste": True,
            },
            "terminal.input": lambda _params: {"terminal": terminal()},
            "terminal.kill": lambda _params: {"terminal": terminal(state="exited")},
            "terminal.forget": lambda _params: {"terminal": terminal()},
            "terminal.group.rename": lambda _params: {
                "group": {"group_id": "g1", "name": "Review", "kind": "user"}
            },
            "terminal.group.delete": lambda _params: {"group_id": "g1", "terminals_killed": 2},
            "terminal.group.order": lambda params: {
                "group_id": params["group_id"],
                "order": params["order"],
            },
            "chat.history": lambda _params: {"messages": []},
            "session.list": lambda _params: {"sessions": [{"session_id": "s1"}]},
            "chat.stream": lambda params: {
                "run_id": "r1",
                "agent_id": params["agent_id"],
                "session_id": params["session_id"],
                "status": "running",
                "events": [{"type": "run_started"}],
                "controls": {},
                "controls_sequence": 1,
                "sse_url": "/api/runs/r1/events",
            },
        }

    async def __call__(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, params))
        result = self.handlers[method](params)
        if isawaitable(result):
            result = await result
        return result  # type: ignore[no-any-return]

    def count(self, method: str) -> int:
        return sum(1 for name, _params in self.calls if name == method)

    def params(self, method: str) -> list[JsonObject]:
        return [params for name, params in self.calls if name == method]

    def fail(self, method: str, error: Exception) -> None:
        def handler(_params: JsonObject) -> JsonObject:
            raise error

        self.handlers[method] = handler


class FakeUi:
    """Owning accessor double answering UI requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, JsonObject]] = []
        self.error: LiveUiError | None = None
        self.applied = True

    async def __call__(self, action: str, args: JsonObject) -> JsonObject:
        self.requests.append((action, args))
        if self.error is not None:
            raise self.error
        if action == "open":
            return {"applied": self.applied}
        if action == "context":
            return {"view": "chat", "selected_agent_id": "joel"}
        return {"visible_order": ["t1", "t2"]}


class Fixture:
    def __init__(self) -> None:
        self.rpc = FakeRpc()
        self.ui = FakeUi()
        self.active = True
        self.executor = LiveToolExecutor(rpc=self.rpc, ui=self.ui, is_active=lambda: self.active)

    async def app(self, **arguments: Any) -> JsonObject:
        return await self.executor.execute("vbot_app", arguments)

    async def terminal(self, **arguments: Any) -> JsonObject:
        return await self.executor.execute("vbot_terminal", arguments)

    def mutations(self) -> list[str]:
        return [
            name
            for name, _params in self.rpc.calls
            if name not in {"terminal.list", "terminal.read", "chat.history", "session.list"}
        ]


def error_code(result: JsonObject) -> str:
    assert result["ok"] is False
    return str(result["error"]["code"])


@pytest.fixture
def fixture() -> Fixture:
    return Fixture()


# -- Terminal launches -------------------------------------------------------


@pytest.mark.asyncio
async def test_keeps_confirmed_launches_when_displaying_the_group_fails(fixture: Fixture) -> None:
    fixture.ui.error = LiveUiError("ui_timeout", uncertain=True)
    result = await fixture.terminal(action="start", program="codex", count=4, workdir="/repo")
    assert len(result["completed"]) == 4
    assert result["layout_error"] == "navigation_not_applied"
    assert fixture.rpc.count("terminal.start") == 4


@pytest.mark.asyncio
async def test_starts_coding_agents_in_the_directory_and_shows_the_first(fixture: Fixture) -> None:
    result = await fixture.terminal(
        action="start", program="codex", count=4, workdir="/projects/vbot"
    )
    assert (
        fixture.rpc.params("terminal.start")
        == [{"command": "codex", "workdir": "/projects/vbot", "group_id": "g1"}] * 4
    )
    assert len(result["completed"]) == 4
    assert result["layout"] == {"visible_order": ["t1", "t2"]}
    assert fixture.ui.requests == [("terminal_view", {"op": "show", "terminal_id": "new-1"})]


@pytest.mark.asyncio
async def test_creates_the_program_group_when_missing(fixture: Fixture) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {"terminals": [], "groups": []}
    result = await fixture.terminal(action="start", program="claude", workdir="/repo", name="A")
    assert fixture.rpc.params("terminal.group.create") == [{"name": "Claude Code"}]
    assert fixture.rpc.params("terminal.start") == [
        {"command": "claude", "workdir": "/repo", "group_id": "new-group", "name": "A"}
    ]
    assert len(result["completed"]) == 1


@pytest.mark.asyncio
async def test_preserves_partial_launches_without_retrying_an_uncertain_start(
    fixture: Fixture,
) -> None:
    def start(_params: JsonObject) -> JsonObject:
        if fixture.rpc.count("terminal.start") == 2:
            raise RuntimeError("fixture transport failure")
        return {"terminal": terminal("new")}

    fixture.rpc.handlers["terminal.start"] = start
    result = await fixture.terminal(action="start", program="codex", count=4, workdir="/repo")
    assert result["ok"] is False
    assert result["completed"] == [terminal("new")]
    assert result["requested_count"] == 4
    assert result["group_id"] == "g1"
    assert result["error"]["code"] == "operation_failed"
    assert result["error"]["delivery_uncertain"] is True
    assert fixture.rpc.count("terminal.start") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"program": "bash"}, "unsupported_program"),
        ({"program": "codex", "count": 1.5}, "invalid_count"),
        ({"program": "codex", "count": "5"}, "invalid_count"),
        ({"program": "codex", "count": float("inf")}, "invalid_count"),
        ({"program": "codex", "count": 2**53}, "invalid_count"),
        ({"program": "codex", "count": True}, "invalid_count"),
        ({"program": "claude", "count": 0}, "invalid_count"),
        ({"program": "codex", "count": 1, "workdir": ""}, "missing_target_or_text"),
        ({"program": "codex", "name": "x" * 81}, "invalid_name"),
        ({"program": "codex", "name": None}, "invalid_name"),
        ({"program": "codex", "group_id": "missing"}, "group_not_found"),
    ],
)
async def test_rejects_an_invalid_launch_before_effects(
    fixture: Fixture, arguments: JsonObject, code: str
) -> None:
    result = await fixture.terminal(action="start", **{"workdir": "/repo", **arguments})
    assert error_code(result) == code
    assert result["error"]["delivery_uncertain"] is False
    assert fixture.mutations() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 5, 12, 33])
async def test_launches_the_requested_count_without_a_live_limit(
    fixture: Fixture, count: int
) -> None:
    result = await fixture.terminal(action="start", program="claude", count=count, workdir="/r")
    assert fixture.rpc.count("terminal.start") == count
    assert len(result["completed"]) == count


@pytest.mark.asyncio
async def test_defaults_an_omitted_launch_count_to_one(fixture: Fixture) -> None:
    await fixture.terminal(action="start", program="codex", workdir="/repo")
    assert fixture.rpc.count("terminal.start") == 1


@pytest.mark.asyncio
async def test_accepts_a_whole_number_count_sent_as_a_float(fixture: Fixture) -> None:
    await fixture.terminal(action="start", program="codex", count=2.0, workdir="/repo")
    assert fixture.rpc.count("terminal.start") == 2


@pytest.mark.asyncio
async def test_reports_server_capacity_failure_with_confirmed_launches_and_no_retry(
    fixture: Fixture,
) -> None:
    def start(_params: JsonObject) -> JsonObject:
        if fixture.rpc.count("terminal.start") == 6:
            raise RpcError("invalid_request", "fixture capacity exhausted")
        return {"terminal": terminal(f"started-{fixture.rpc.count('terminal.start')}")}

    fixture.rpc.handlers["terminal.start"] = start
    result = await fixture.terminal(action="start", program="codex", count=12, workdir="/repo")
    assert result["requested_count"] == 12
    assert len(result["completed"]) == 5
    assert result["error"] == {
        "code": "invalid_request",
        "message": "fixture capacity exhausted",
        "delivery_uncertain": True,
    }
    assert fixture.rpc.count("terminal.start") == 6


@pytest.mark.asyncio
async def test_stops_a_multi_launch_when_the_call_stops(fixture: Fixture) -> None:
    def start(_params: JsonObject) -> JsonObject:
        fixture.active = False
        return {"terminal": terminal("first")}

    fixture.rpc.handlers["terminal.start"] = start
    result = await fixture.terminal(action="start", program="codex", count=4, workdir="/repo")
    assert [item["terminal_id"] for item in result["completed"]] == ["first"]
    assert result["error"]["code"] == "voice_stopped"
    assert fixture.rpc.count("terminal.start") == 1


# -- Terminal discovery, reads and input ----------------------------------------


@pytest.mark.asyncio
async def test_bounds_quoted_chat_context_and_omits_launch_history(fixture: Fixture) -> None:
    fixture.rpc.handlers["chat.history"] = lambda params: {
        "messages": [
            {"id": index, "role": "assistant", "content": "x" * 12_000}
            for index in range(params["limit"])
        ]
    }
    chat = await fixture.app(action="read", agent_id="joel", session_id="s")
    assert sum(len(message["content"]) for message in chat["messages"]) <= 8_000
    assert chat["messages"][-1] == {
        "id": 19,
        "role": "assistant",
        "content": "x" * 8_000,
        "truncated": True,
    }
    assert fixture.rpc.params("chat.history") == [
        {"agent_id": "joel", "session_id": "s", "limit": 1},
        {"agent_id": "joel", "session_id": "s", "limit": 20},
    ]
    catalog = await fixture.terminal(action="list")
    assert "launch_history" not in catalog
    assert catalog["terminals"][0]["terminal_id"] == "t1"
    assert catalog["layout"] == {"visible_order": ["t1", "t2"]}


@pytest.mark.asyncio
async def test_read_keeps_only_visible_roles_newest_first_within_budget(fixture: Fixture) -> None:
    fixture.rpc.handlers["chat.history"] = lambda _params: {
        "messages": [
            {"id": "a", "role": "user", "content": "question"},
            {"id": "b", "role": "tool", "content": "hidden"},
            {"id": "c", "role": "assistant", "content": ["blocks"]},
            {"id": "d", "role": "error", "content": "failed"},
        ]
    }
    chat = await fixture.app(action="read", agent_id="joel", session_id="s")
    assert [message["id"] for message in chat["messages"]] == ["a", "d"]


@pytest.mark.asyncio
async def test_list_does_not_hide_groups_or_terminals_beyond_forty(fixture: Fixture) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {
        "groups": [{"group_id": f"g{index}", "kind": "user"} for index in range(45)],
        "terminals": [terminal(f"t{index}") for index in range(45)],
    }
    result = await fixture.terminal(action="list")
    assert result["groups"][-1]["group_id"] == "g44"
    assert result["terminals"][-1]["terminal_id"] == "t44"


@pytest.mark.asyncio
async def test_list_keeps_the_catalog_when_no_window_describes_the_layout(
    fixture: Fixture,
) -> None:
    fixture.ui.error = LiveUiError("ui_unavailable")
    result = await fixture.terminal(action="list")
    assert len(result["terminals"]) == 2
    assert result["layout_error"] == "ui_unavailable"
    assert "layout" not in result


@pytest.mark.asyncio
async def test_bounds_terminal_summaries_and_screens(fixture: Fixture) -> None:
    fixture.rpc.handlers["terminal.read"] = lambda _params: {
        "terminal": terminal(name="n" * 2_000, secret="unused"),
        "screen": "s" * 9_000,
    }
    result = await fixture.terminal(action="read", terminal_id="t1")
    assert len(result["terminal"]["name"]) == 1_000
    assert "secret" not in result["terminal"]
    assert len(result["screen"]) == 8_000
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_preserves_multiline_paste_and_guards_the_screen_revision(fixture: Fixture) -> None:
    await fixture.terminal(action="input", terminal_id="t1", text="one\ntwo")
    assert fixture.rpc.params("terminal.input") == [
        {
            "terminal_id": "t1",
            "data": "\x1b[200~one\ntwo\x1b[201~\r",
            "expected_screen_revision": 12,
        }
    ]


@pytest.mark.asyncio
async def test_sends_plain_text_without_submit_and_named_keys(fixture: Fixture) -> None:
    fixture.rpc.handlers["terminal.read"] = lambda _params: {
        "terminal": terminal(),
        "screen": "",
        "bracketed_paste": False,
    }
    await fixture.terminal(action="input", terminal_id="t1", text="one\ntwo", submit=False)
    await fixture.terminal(action="input", terminal_id="t1", key="ctrl-c")
    assert [params["data"] for params in fixture.rpc.params("terminal.input")] == [
        "one\ntwo",
        "\x03",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"text": "x", "key": "enter"},
        {"key": "enter", "submit": True},
        {"key": "home"},
        {"text": "x", "submit": "yes"},
        {"text": "\x1b[31m"},
        {"text": "y" * 16_001},
    ],
)
async def test_rejects_mixed_input_modes_and_control_sequences(
    fixture: Fixture, arguments: JsonObject
) -> None:
    result = await fixture.terminal(action="input", terminal_id="t1", **arguments)
    assert error_code(result) == "invalid_input"
    assert fixture.rpc.count("terminal.input") == 0


@pytest.mark.asyncio
async def test_does_not_operate_a_shell_as_a_coding_agent(fixture: Fixture) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {
        "terminals": [terminal(launch_command="bash")]
    }
    result = await fixture.terminal(action="input", terminal_id="t1", text="rm file")
    assert error_code(result) == "not_a_coding_terminal"
    assert fixture.rpc.count("terminal.input") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["codex", "Claude.EXE", "C:\\tools\\codex.cmd", "/usr/bin/claude"]
)
async def test_recognizes_coding_agent_launch_commands(fixture: Fixture, command: str) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {
        "terminals": [terminal(launch_command=command)]
    }
    result = await fixture.terminal(action="read", terminal_id="t1")
    assert result["screen"] == "fixture"


# -- Terminal layout and groups --------------------------------------------------


@pytest.mark.asyncio
async def test_closes_the_exact_terminal_by_stopping_then_forgetting(fixture: Fixture) -> None:
    result = await fixture.terminal(action="close", terminal_id="t2")
    assert result == {"terminal_id": "t2", "stopped": True, "removed": True}
    assert [name for name, _params in fixture.rpc.calls][1:] == ["terminal.kill", "terminal.forget"]
    assert fixture.rpc.params("terminal.forget") == [{"terminal_id": "t2"}]
    assert fixture.ui.requests == [("terminal_view", {"op": "refresh"})]


@pytest.mark.asyncio
async def test_removes_a_finished_terminal_without_stopping_it_again(fixture: Fixture) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {
        "terminals": [terminal(state="exited")]
    }
    await fixture.terminal(action="close", terminal_id="t1")
    assert fixture.rpc.count("terminal.kill") == 0
    assert fixture.rpc.params("terminal.forget") == [{"terminal_id": "t1"}]


@pytest.mark.asyncio
async def test_preserves_a_confirmed_stop_when_removal_fails(fixture: Fixture) -> None:
    fixture.rpc.fail("terminal.forget", RpcError("invalid_request", "fixture removal failed"))
    result = await fixture.terminal(action="close", terminal_id="t1")
    assert result["ok"] is False
    assert result["stopped"] is True
    assert result["removed"] is None
    assert result["terminal_id"] == "t1"
    assert result["error"]["delivery_uncertain"] is True
    assert fixture.rpc.count("terminal.kill") == 1
    assert fixture.rpc.count("terminal.forget") == 1


@pytest.mark.asyncio
async def test_does_not_remove_a_terminal_after_an_uncertain_stop(fixture: Fixture) -> None:
    fixture.rpc.fail("terminal.kill", RuntimeError("fixture stop failed"))
    result = await fixture.terminal(action="close", terminal_id="t1")
    assert result["ok"] is False
    assert result["stopped"] is None
    assert result["removed"] is False
    assert fixture.rpc.count("terminal.forget") == 0


@pytest.mark.asyncio
async def test_stops_a_close_sequence_when_the_call_stops_after_the_kill(
    fixture: Fixture,
) -> None:
    def kill(_params: JsonObject) -> JsonObject:
        fixture.active = False
        return {}

    fixture.rpc.handlers["terminal.kill"] = kill
    result = await fixture.terminal(action="close", terminal_id="t1")
    assert result["ok"] is False
    assert result["stopped"] is True
    assert result["removed"] is False
    assert result["error"]["code"] == "voice_stopped"
    assert fixture.rpc.count("terminal.forget") == 0


@pytest.mark.asyncio
async def test_creates_and_displays_an_empty_group_then_selects_it_by_id(
    fixture: Fixture,
) -> None:
    result = await fixture.terminal(action="create_group", name="Review")
    assert result["group"]["group_id"] == "new-group"
    assert fixture.rpc.params("terminal.group.create") == [{"name": "Review"}]
    await fixture.terminal(action="show_group", group_id="g1")
    assert fixture.ui.requests == [
        ("terminal_view", {"op": "show_group", "group_id": "new-group"}),
        ("terminal_view", {"op": "show_group", "group_id": "g1"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "agent"])
async def test_renames_and_deletes_user_and_agent_groups(fixture: Fixture, kind: str) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {
        "groups": [{"group_id": "g1", "kind": kind}],
        "terminals": [terminal()],
    }
    renamed = await fixture.terminal(action="rename_group", group_id="g1", name="Review")
    assert fixture.rpc.params("terminal.group.rename") == [{"group_id": "g1", "name": "Review"}]
    assert renamed["group"]["name"] == "Review"
    deleted = await fixture.terminal(action="delete_group", group_id="g1")
    assert fixture.rpc.params("terminal.group.delete") == [{"group_id": "g1"}]
    assert deleted == {"group_id": "g1", "terminals_killed": 2}
    assert len(fixture.ui.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["automatic", "finished"])
async def test_rejects_edits_of_built_in_groups_before_mutation(
    fixture: Fixture, kind: str
) -> None:
    fixture.rpc.handlers["terminal.list"] = lambda _params: {
        "groups": [{"group_id": "g1", "kind": kind}]
    }
    renamed = await fixture.terminal(action="rename_group", group_id="g1", name="New")
    deleted = await fixture.terminal(action="delete_group", group_id="g1")
    assert error_code(renamed) == "group_not_editable"
    assert error_code(deleted) == "group_not_editable"
    assert fixture.mutations() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"action": "close"}, "terminal_not_found"),
        ({"action": "close", "terminal_id": "missing"}, "terminal_not_found"),
        ({"action": "rename_group", "group_id": "g1", "name": ""}, "missing_target_or_text"),
        ({"action": "rename_group", "group_id": "g1", "name": "x" * 81}, "invalid_name"),
        ({"action": "rename_group", "group_id": "missing", "name": "New"}, "group_not_found"),
        ({"action": "delete_group", "group_id": "missing"}, "group_not_found"),
        ({"action": "delete_group", "group_id": "g1", "name": "unexpected"}, "invalid_arguments"),
        ({"action": "unknown"}, "invalid_arguments"),
        ({"action": ["list"]}, "invalid_arguments"),
        ({}, "invalid_arguments"),
    ],
)
async def test_rejects_invalid_targets_and_fields_before_effects(
    fixture: Fixture, arguments: JsonObject, code: str
) -> None:
    result = await fixture.executor.execute("vbot_terminal", arguments)
    assert error_code(result) == code
    assert result["error"]["delivery_uncertain"] is False
    assert fixture.mutations() == []


@pytest.mark.asyncio
async def test_rejects_unknown_tools_and_non_object_arguments(fixture: Fixture) -> None:
    assert error_code(await fixture.executor.execute("shell", {"action": "list"})) == (
        "invalid_arguments"
    )
    assert error_code(await fixture.executor.execute("vbot_app", ["context"])) == (
        "invalid_arguments"
    )
    assert fixture.rpc.calls == []


@pytest.mark.asyncio
async def test_keeps_successful_mutations_when_the_layout_refresh_fails(fixture: Fixture) -> None:
    fixture.ui.error = LiveUiError("ui_timeout", uncertain=True)
    created = await fixture.terminal(action="create_group", name="Review")
    renamed = await fixture.terminal(action="rename_group", group_id="g1", name="Review")
    reordered = await fixture.terminal(action="reorder", group_id="g1", order=["t2", "t1"])
    deleted = await fixture.terminal(action="delete_group", group_id="g1")
    closed = await fixture.terminal(action="close", terminal_id="t1")
    assert created["group"]["group_id"] == "new-group"
    assert renamed["group"]["name"] == "Review"
    assert reordered["order"] == ["t2", "t1"]
    assert deleted["terminals_killed"] == 2
    assert closed["stopped"] is True and closed["removed"] is True
    for result in (created, renamed, reordered, deleted, closed):
        assert result["layout_error"] == "refresh_failed"
    assert fixture.rpc.count("terminal.group.delete") == 1
    assert fixture.rpc.count("terminal.kill") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [["t1", "t1"], ["t1"], ["t1", "t2", "t3"], ["t1", "missing"], [1, 2], "t1,t2"],
)
async def test_reorder_uses_actual_group_members_exactly_once(fixture: Fixture, order: Any) -> None:
    result = await fixture.terminal(action="reorder", group_id="g1", order=order)
    assert error_code(result) == "invalid_order"
    assert fixture.rpc.count("terminal.group.order") == 0


@pytest.mark.asyncio
async def test_reorders_a_group_and_refreshes_the_layout(fixture: Fixture) -> None:
    result = await fixture.terminal(action="reorder", group_id="g1", order=["t2", "t1"])
    assert fixture.rpc.params("terminal.group.order") == [{"group_id": "g1", "order": ["t2", "t1"]}]
    assert result == {"group_id": "g1", "order": ["t2", "t1"]}
    assert fixture.ui.requests == [("terminal_view", {"op": "refresh"})]


@pytest.mark.asyncio
async def test_routes_show_maximize_and_restore_through_the_terminal_view(
    fixture: Fixture,
) -> None:
    shown = await fixture.terminal(action="show", terminal_id="t1")
    await fixture.terminal(action="maximize", terminal_id="t2")
    await fixture.terminal(action="restore")
    assert shown == {"visible_order": ["t1", "t2"]}
    assert fixture.ui.requests == [
        ("terminal_view", {"op": "show", "terminal_id": "t1"}),
        ("terminal_view", {"op": "maximize", "terminal_id": "t2"}),
        ("terminal_view", {"op": "restore"}),
    ]


@pytest.mark.asyncio
async def test_reports_an_unanswered_display_change_as_uncertain(fixture: Fixture) -> None:
    fixture.ui.error = LiveUiError("ui_timeout", uncertain=True)
    result = await fixture.terminal(action="maximize", terminal_id="t2")
    assert result["error"]["code"] == "ui_timeout"
    assert result["error"]["delivery_uncertain"] is True


# -- Chat and navigation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_forwards_a_reply_to_its_exact_session_as_speech(fixture: Fixture) -> None:
    result = await fixture.app(
        action="send", agent_id="joel@project", session_id="s1", text="Use option one."
    )
    assert fixture.rpc.params("chat.stream") == [
        {
            "agent_id": "joel@project",
            "session_id": "s1",
            "content": "Use option one.",
            "input_origin": "speech_transcription",
        }
    ]
    assert result == {
        "agent_id": "joel@project",
        "session_id": "s1",
        "run_id": "r1",
        "status": "running",
    }


@pytest.mark.asyncio
async def test_validates_an_exact_session_once_per_call(fixture: Fixture) -> None:
    for _ in range(2):
        await fixture.app(action="send", agent_id="joel", session_id="s1", text="next")
    assert fixture.rpc.params("chat.history") == [
        {"agent_id": "joel", "session_id": "s1", "limit": 1}
    ]
    assert fixture.rpc.count("chat.stream") == 2


@pytest.mark.asyncio
async def test_does_not_send_to_a_session_the_server_rejects(fixture: Fixture) -> None:
    fixture.rpc.fail("chat.history", RpcError("domain_error", "Session not found"))
    result = await fixture.app(action="send", agent_id="joel", session_id="gone", text="x")
    assert result["error"] == {
        "code": "domain_error",
        "message": "Session not found",
        "delivery_uncertain": False,
    }
    assert fixture.rpc.count("chat.stream") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"agent_id": "joel", "text": "continue"}, "missing_target_or_text"),
        ({"agent_id": "joel", "session_id": "s1"}, "missing_target_or_text"),
        ({"agent_id": "joel", "session_id": "s1", "text": "x" * 16_001}, "text_too_long"),
    ],
)
async def test_does_not_guess_a_session_or_send_invalid_text(
    fixture: Fixture, arguments: JsonObject, code: str
) -> None:
    result = await fixture.app(action="send", **arguments)
    assert error_code(result) == code
    assert fixture.rpc.count("chat.stream") == 0


@pytest.mark.asyncio
async def test_reports_an_unexpected_send_failure_as_uncertain(fixture: Fixture) -> None:
    fixture.rpc.fail("chat.stream", RuntimeError("fixture failure"))
    result = await fixture.app(action="send", agent_id="joel", session_id="s1", text="x")
    assert result["error"]["code"] == "operation_failed"
    assert result["error"]["delivery_uncertain"] is True
    assert "fixture failure" not in result["error"]["message"]


@pytest.mark.asyncio
async def test_lists_sessions_and_reads_app_context(fixture: Fixture) -> None:
    sessions = await fixture.app(action="sessions", agent_id="joel")
    context = await fixture.app(action="context")
    assert fixture.rpc.params("session.list") == [{"agent_id": "joel", "limit": 30}]
    assert sessions == {"sessions": [{"session_id": "s1"}]}
    assert context == {"view": "chat", "selected_agent_id": "joel"}
    assert fixture.ui.requests == [("context", {})]


@pytest.mark.asyncio
async def test_opens_an_exact_chat_after_validating_its_session(fixture: Fixture) -> None:
    result = await fixture.app(action="open", view="chat", agent_id="joel", session_id="s1")
    assert result == {"view": "chat", "agent_id": "joel", "session_id": "s1"}
    assert fixture.rpc.params("chat.history") == [
        {"agent_id": "joel", "session_id": "s1", "limit": 1}
    ]
    assert fixture.ui.requests == [
        ("open", {"view": "chat", "agent_id": "joel", "session_id": "s1"})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"view": "settings"}, "invalid_view"),
        ({"view": "terminals", "agent_id": "joel", "session_id": "s1"}, "invalid_arguments"),
        ({"view": "chat", "agent_id": "joel"}, "missing_target_or_text"),
    ],
)
async def test_rejects_invalid_navigation_before_asking_the_app(
    fixture: Fixture, arguments: JsonObject, code: str
) -> None:
    result = await fixture.app(action="open", **arguments)
    assert error_code(result) == code
    assert fixture.ui.requests == []


@pytest.mark.asyncio
async def test_reports_navigation_the_app_did_not_apply(fixture: Fixture) -> None:
    fixture.ui.applied = False
    result = await fixture.app(action="open", view="terminals")
    assert error_code(result) == "navigation_not_applied"


@pytest.mark.asyncio
async def test_rejects_every_operation_once_the_call_stopped(fixture: Fixture) -> None:
    fixture.active = False
    result = await fixture.app(action="context")
    assert error_code(result) == "voice_stopped"
    assert fixture.ui.requests == []
