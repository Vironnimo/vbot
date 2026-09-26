"""Live Tools as the voice call runs them: refs, targets, effects, and result texts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from server._live_context import LiveUiError
from server._live_terminals import TerminalTimings
from server._live_tools import LiveToolExecutor
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]

CALL_START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
BEFORE = "2026-09-25T09:00:00+00:00"
AFTER = "2026-09-25T10:05:00+00:00"

SHELL = "PowerShell 7.6.6\nPS C:\\work\\vbot>"
CODEX_HEADER = (
    "╭──────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.153.2)   │\n"
    "│ model:     {model}           │\n"
    "╰──────────────────────────────╯\n"
)
CODEX_LOADING = (
    SHELL + " codex\n" + CODEX_HEADER.format(model="loading") + "\n› Ask Codex to do anything\n"
)
CODEX_READY = (
    SHELL + " codex\n" + CODEX_HEADER.format(model="gpt-5") + "\n› Ask Codex to do anything\n"
)
CODEX_UPDATE = (
    "PowerShell 7.6.6\n  ✨ Update available! 0.153.2 -> 0.157.0\n\n"
    "› 1. Update now\n  2. Skip\n  3. Skip until next version\n\n  Press enter to continue"
)
CLAUDE_READY = (
    SHELL + " claude\n Claude Code v2.1.280\n────\n❯\xa0Try something\n────\n  ⏵⏵ auto mode on"
)
CLAUDE_TRUST = (
    SHELL + " claude\n Quick safety check\n ❯ No, exit\n   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel"
)
STALE = RpcError(
    "invalid_request", "Terminal screen changed; inspect status before sending this input"
)


def session_row(session_id: str, agent: str, **fields: Any) -> JsonObject:
    return {
        "id": session_id,
        "agent_address": agent,
        "created_at": BEFORE,
        "last_active_at": BEFORE,
        "has_active_run": False,
        **fields,
    }


class FakeApp:
    """In-process RPC double holding a small app: Agents, Sessions, Terminals."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.agents = [
            {"id": "main", "name": "Main"},
            {"id": "coder", "name": "Coder"},
            {"id": "writer", "name": "Writer"},
        ]
        self.projects = [{"project_id": "vbot", "display_name": "vBot", "cwd": "C:\\work\\vbot"}]
        self.teams = {"vbot": [{"agent_id": "reviewer", "display_name": "Reviewer"}]}
        self.sessions: list[JsonObject] = []
        self.histories: dict[str, JsonObject] = {}
        self.results: dict[str, str] = {}
        self.terminals: list[JsonObject] = []
        self.groups: list[JsonObject] = [
            {"group_id": "grp_mine", "name": "Mine", "kind": "user"},
            {"group_id": "grp_done", "name": "Finished", "kind": "finished"},
        ]
        self.screens: dict[str, list[str]] = {}
        self.inputs: list[tuple[str, str, int | None]] = []
        self.failures: dict[str, list[Exception | None]] = {}
        self.queued = False
        self.created = 0

    def fail(self, method: str, *errors: Exception | None) -> None:
        """Answer the next calls of *method* with these errors; ``None`` lets one pass."""
        self.failures[method] = list(errors)

    def count(self, method: str) -> int:
        return sum(1 for name, _params in self.calls if name == method)

    def params(self, method: str) -> list[JsonObject]:
        return [params for name, params in self.calls if name == method]

    def effects(self) -> list[str]:
        reads = {
            "agent.list",
            "project.list",
            "project.show",
            "session.list",
            "chat.history",
            "chat.run_result",
            "terminal.list",
            "terminal.read",
        }
        return [name for name, _params in self.calls if name not in reads]

    def add_terminal(self, terminal_id: str, command: str = "codex", **fields: Any) -> JsonObject:
        terminal = {
            "terminal_id": terminal_id,
            "group_id": "grp_mine",
            "name": None,
            "state": "ready",
            "command": "pwsh.exe",
            "launch_command": command,
            "workdir": "C:\\work\\vbot",
            "screen_revision": 5,
            **fields,
        }
        self.terminals.append(terminal)
        return terminal

    async def __call__(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, dict(params)))
        pending = self.failures.get(method)
        if pending:
            error = pending.pop(0)
            if error is not None:
                raise error
        handler = getattr(self, "_" + method.replace(".", "_"))
        return handler(params)  # type: ignore[no-any-return]

    def _agent_list(self, _params: JsonObject) -> JsonObject:
        return {"agents": self.agents}

    def _project_list(self, _params: JsonObject) -> JsonObject:
        return {"projects": self.projects}

    def _project_show(self, params: JsonObject) -> JsonObject:
        return {"project": {}, "scan": {"team": self.teams.get(params["project_id"], [])}}

    def _session_list(self, params: JsonObject) -> JsonObject:
        addresses = params.get("agent_ids") or [params["agent_id"]]
        rows = [
            row
            for row in self.sessions
            if row["agent_address"] in addresses
            and (params.get("include_cron", True) or "cron" not in row.get("run_kinds", ()))
            and (params.get("include_channels", True) or not row.get("platform_conv_id"))
        ]
        rows.sort(key=lambda row: row["last_active_at"], reverse=True)
        return {"sessions": rows[: params["limit"]]}

    def _session_create(self, params: JsonObject) -> JsonObject:
        self.created += 1
        session_id = f"ses_new{self.created}"
        self.sessions.append(session_row(session_id, params["agent_id"], last_active_at=AFTER))
        return {"agent_id": params["agent_id"].split("@")[0], "session_id": session_id}

    def _chat_stream(self, params: JsonObject) -> JsonObject:
        for row in self.sessions:
            if row["id"] == params["session_id"]:
                row["has_active_run"] = True
        if self.queued:
            return {"queued": True, "item": {"item_id": "q1"}}
        return {"run_id": "run_1", "status": "running", "events": [], "sse_url": "/x"}

    def _chat_history(self, params: JsonObject) -> JsonObject:
        return self.histories.get(params["session_id"], {"messages": []})

    def _chat_run_result(self, params: JsonObject) -> JsonObject:
        return {"content": self.results.get(params["run_id"], ""), "truncated": False}

    def _chat_cancel(self, params: JsonObject) -> JsonObject:
        return {"run_id": params["run_id"], "status": "cancelled"}

    def _terminal_list(self, _params: JsonObject) -> JsonObject:
        return {"terminals": self.terminals, "groups": self.groups, "launch_history": []}

    def _terminal_start(self, params: JsonObject) -> JsonObject:
        terminal_id = f"term_start{len(self.terminals) + 1}"
        terminal = self.add_terminal(
            terminal_id,
            params["command"],
            group_id=params["group_id"],
            workdir=params["workdir"],
            name=params.get("name"),
            state="starting",
        )
        ready = CODEX_READY if params["command"] == "codex" else CLAUDE_READY
        self.screens.setdefault(terminal_id, [SHELL, ready])
        return {"terminal": terminal, "launch_history": []}

    def _terminal_read(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        script = self.screens.setdefault(params["terminal_id"], [CODEX_READY])
        screen = script.pop(0) if len(script) > 1 else script[0]
        return {"terminal": terminal, "screen": screen, "bracketed_paste": True}

    def _terminal_input(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        expected = params.get("expected_screen_revision")
        self.inputs.append((params["terminal_id"], params["data"], expected))
        terminal["screen_revision"] += 1
        data = params["data"]
        if data.startswith("\x1b[200~"):
            # The program echoes pasted text on its input line.
            text = data.removeprefix("\x1b[200~").removesuffix("\x1b[201~")
            script = self.screens.setdefault(params["terminal_id"], [CODEX_READY])
            script[:] = [re.sub(r"^([›❯])[ \xa0].*$", rf"\1 {text}", script[-1], flags=re.M)]
        return {"terminal": terminal}

    def _terminal_kill(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        terminal["state"] = "exited"
        return {"terminal": terminal}

    def _terminal_forget(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        self.terminals.remove(terminal)
        return {"terminal": terminal}

    def _terminal_group_create(self, params: JsonObject) -> JsonObject:
        group = {"group_id": f"grp_new{len(self.groups)}", "name": params["name"], "kind": "user"}
        self.groups.append(group)
        return {"group": group}

    def _terminal_group_rename(self, params: JsonObject) -> JsonObject:
        return {"group": {"group_id": params["group_id"], "name": params["name"]}}

    def _terminal_group_delete(self, params: JsonObject) -> JsonObject:
        return {"group_id": params["group_id"], "terminals_killed": 2}

    def _terminal_group_order(self, params: JsonObject) -> JsonObject:
        return {"group_id": params["group_id"], "order": params["order"]}

    def _terminal(self, terminal_id: str) -> JsonObject:
        for terminal in self.terminals:
            if terminal["terminal_id"] == terminal_id:
                return terminal
        raise RpcError("invalid_request", f"Terminal Session not found: {terminal_id}")


class FakeUi:
    """The owning app window: its context, navigation, and Terminal layout."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, JsonObject]] = []
        self.error: LiveUiError | None = None
        self.applied = True
        self.context: JsonObject = {
            "view": "chat",
            "selected_agent_id": "main",
            "selected_project_id": "vbot",
            "agents": [
                {"agent_id": "main", "name": "Main"},
                {"agent_id": "coder", "name": "Coder"},
                {"agent_id": "writer", "name": "Writer"},
            ],
            "projects": [{"project_id": "vbot", "name": "vBot", "cwd": "C:\\work\\vbot"}],
            "selected_project_team": [{"agent_id": "reviewer@vbot", "name": "Reviewer"}],
        }

    async def __call__(self, action: str, args: JsonObject) -> JsonObject:
        self.requests.append((action, args))
        if self.error is not None:
            raise self.error
        if action == "context":
            return self.context
        if action == "open":
            return {"applied": self.applied}
        return {}

    def of(self, action: str) -> list[JsonObject]:
        return [args for name, args in self.requests if name == action]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class Fixture:
    def __init__(self) -> None:
        self.app = FakeApp()
        self.ui = FakeUi()
        self.clock = FakeClock()
        self.active = True
        self.executor = LiveToolExecutor(
            rpc=self.app,
            ui=self.ui,
            is_active=lambda: self.active,
            started_at=CALL_START,
            timings=TerminalTimings(),
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    async def call(self, tool: str, **arguments: Any) -> JsonObject:
        return await self.executor.execute(tool, arguments)

    async def ok(self, tool: str, **arguments: Any) -> str:
        result = await self.call(tool, **arguments)
        assert result["ok"] is True, result
        return str(result["data"]["content"])

    async def failed(self, tool: str, **arguments: Any) -> tuple[str, str]:
        result = await self.call(tool, **arguments)
        assert result["ok"] is False, result
        return str(result["error"]["code"]), str(result["error"]["message"])


@pytest.fixture
def fx() -> Fixture:
    return Fixture()


# -- overview ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_shows_selection_agents_sessions_and_terminals(fx: Fixture) -> None:
    fx.app.sessions = [
        session_row("ses_old", "writer", title="Old draft"),
        session_row("ses_run", "coder", title="Fix login", has_active_run=True),
        session_row(
            "ses_ask",
            "writer",
            title="Blog post",
            last_active_at=AFTER,
            latest_completion_run_id="run_ask",
        ),
        session_row(
            "ses_done",
            "reviewer@vbot",
            title="Review",
            last_active_at=AFTER,
            latest_completion_run_id="run_done",
        ),
    ]
    fx.app.results = {
        "run_ask": "I drafted it.\n\nShould I use a formal tone?",
        "run_done": "All checks pass. " + "x" * 300 + " Done.",
    }
    fx.app.add_terminal("term_a", name="Build")
    fx.app.add_terminal("term_b", "pwsh", state="exited")

    text = await fx.ok("overview")

    assert text.splitlines()[:4] == [
        "App: chat view; selected Agent: Main; selected Project: vBot.",
        "Agents: Main, Coder, Writer.",
        "Team of Project vBot: Reviewer.",
        "Projects: vBot (C:\\work\\vbot).",
    ]
    assert '- s1 Coder "Fix login": working' in text
    assert (
        '- s2 Writer "Blog post": waiting for an answer: '
        '"I drafted it. Should I use a formal tone?"'
    ) in text
    assert '- s3 Reviewer "Review": finished: "...' in text
    assert 'Done."' in text
    assert "Old draft" not in text
    assert '- t1 Codex "Build" in C:\\work\\vbot: idle' in text
    assert "- t2 pwsh in C:\\work\\vbot: exited" in text
    assert fx.app.effects() == []


@pytest.mark.asyncio
async def test_overview_keeps_refs_stable_and_caps_long_lists(fx: Fixture) -> None:
    fx.app.sessions = [
        session_row(
            f"ses_{index:02d}",
            "coder",
            has_active_run=True,
            last_active_at=f"2026-09-25T10:{index:02d}:00+00:00",
        )
        for index in range(15)
    ]
    first = await fx.ok("overview")
    assert "- and 3 more" in first
    second = await fx.ok("overview")
    assert first == second
    assert "s1 Coder: working" in first


@pytest.mark.asyncio
async def test_overview_without_an_app_window_uses_the_canonical_catalogs(fx: Fixture) -> None:
    fx.ui.error = LiveUiError("ui_unavailable")
    text = await fx.ok("overview")
    assert text.splitlines()[0] == "Agents: Main, Coder, Writer."
    assert "Team of" not in text
    assert fx.app.count("agent.list") == 1
    assert fx.app.count("project.list") == 1


@pytest.mark.asyncio
async def test_overview_for_one_agent_lists_its_sessions(fx: Fixture) -> None:
    fx.app.sessions = [
        session_row("ses_1", "coder", title="Old one"),
        session_row("ses_2", "writer", title="Other agent"),
    ]
    text = await fx.ok("overview", agent="coder")
    assert text == 'Sessions of Coder, most recent first:\n- s1 Coder "Old one": finished'


# -- start_agent_session ---------------------------------------------------------


@pytest.mark.asyncio
async def test_starts_several_sessions_at_an_agent_as_speech(fx: Fixture) -> None:
    text = await fx.ok("start_agent_session", agent="coder", task="Fix the tests", count=3)
    assert text == (
        "Started 3 Sessions at Coder with the task: s1, s2, s3. They work in the background; "
        "an update follows when each finishes."
    )
    assert fx.app.params("session.create") == [{"agent_id": "coder"}] * 3
    assert fx.app.params("chat.stream") == [
        {
            "agent_id": "coder",
            "session_id": f"ses_new{index}",
            "content": "Fix the tests",
            "input_origin": "speech_transcription",
        }
        for index in (1, 2, 3)
    ]


@pytest.mark.asyncio
async def test_starts_a_team_agent_of_the_selected_or_named_project(fx: Fixture) -> None:
    text = await fx.ok("start_agent_session", agent="Reviewer", task="Review it")
    assert text.startswith("Started a Session at Reviewer (vBot team) with the task: s1.")
    assert fx.app.params("session.create") == [{"agent_id": "reviewer@vbot"}]
    fx.ui.context["selected_project_id"] = ""
    fx.ui.context["selected_project_team"] = []
    await fx.ok("start_agent_session", agent="reviewer", project="vBot", task="Again")
    assert fx.app.params("session.create")[-1] == {"agent_id": "reviewer@vbot"}
    assert fx.app.params("project.show") == [{"project_id": "vbot"}]


@pytest.mark.asyncio
async def test_does_not_guess_an_unknown_or_ambiguous_agent(fx: Fixture) -> None:
    code, message = await fx.failed("start_agent_session", agent="Codr", task="x")
    assert code == "target_not_found"
    assert 'No Agent matches "Codr". Agents: Main, Coder, Writer, Reviewer (vBot team).' in message
    assert "call start_agent_session again with one of them as agent" in message
    fx.ui.context["selected_project_team"] = [{"agent_id": "coder@vbot", "name": "Coder"}]
    # The exact id decides; the shared name does not.
    text = await fx.ok("start_agent_session", agent="coder", task="x")
    assert text.startswith("Started a Session at Coder with")
    code, message = await fx.failed("start_agent_session", agent="Coder", task="x")
    assert code == "ambiguous_target"
    assert "(id coder)" in message
    assert "(vBot team, id coder@vbot)" in message
    assert fx.app.count("chat.stream") == 1
    # The id the failure lists selects that Agent.
    await fx.ok("start_agent_session", agent="coder@vbot", task="x")
    assert fx.app.params("session.create")[-1] == {"agent_id": "coder@vbot"}


@pytest.mark.asyncio
async def test_reports_started_sessions_when_a_later_start_fails_without_retrying(
    fx: Fixture,
) -> None:
    fx.app.fail("session.create", None, RpcError("queue_full", "The Queue is full."))
    code, message = await fx.failed("start_agent_session", agent="Coder", task="x", count=3)
    assert code == "partial"
    assert message == (
        "Started 1 of 3 Sessions at Coder: s1. Starting Session 2 of 3 failed: The Queue is "
        "full. Nothing was retried."
    )
    assert fx.app.count("session.create") == 2


@pytest.mark.asyncio
async def test_reports_a_created_session_whose_task_may_not_have_arrived(fx: Fixture) -> None:
    fx.app.fail("chat.stream", RuntimeError("socket closed"))
    code, message = await fx.failed("start_agent_session", agent="Coder", task="x")
    assert code == "start_failed"
    assert message == (
        "s1 was created, but the task was not delivered to it: It failed. Nothing was retried. "
        "It may or may not have been delivered; do not send it again."
    )


# -- start_coding_terminal -------------------------------------------------------


@pytest.mark.asyncio
async def test_starts_codex_and_types_the_task_once_it_is_ready(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CODEX_LOADING, CODEX_READY]
    text = await fx.ok("start_coding_terminal", program="codex", task='Fix "a" & 100%')
    assert text == (
        "Started Codex in a Terminal in C:\\work\\vbot: t1. Typed the task into t1 and sent it."
    )
    start = fx.app.params("terminal.start")
    # The task never becomes part of the command line.
    assert start == [{"command": "codex", "workdir": "C:\\work\\vbot", "group_id": "grp_new2"}]
    assert fx.app.params("terminal.group.create") == [{"name": "Codex"}]
    assert [(data, revision) for _id, data, revision in fx.app.inputs] == [
        ('\x1b[200~Fix "a" & 100%\x1b[201~', 5),
        ("\r", 6),
    ]
    assert fx.ui.of("terminal_view")[0] == {"op": "show", "terminal_id": "term_start1"}


@pytest.mark.asyncio
async def test_never_types_into_a_program_that_did_not_become_ready(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL]
    text = await fx.ok("start_coding_terminal", program="codex", task="Fix it")
    assert fx.app.inputs == []
    assert (
        "t1 did not show Codex's input line within 25 seconds, so the task was not typed." in text
    )
    assert 'send_message with {"target": "t1", "text": "<the task>"}' in text


@pytest.mark.asyncio
async def test_does_not_answer_a_trust_question_for_the_user(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CLAUDE_TRUST]
    text = await fx.ok("start_coding_terminal", program="claude", task="Fix it")
    assert fx.app.inputs == []
    assert (
        "Claude Code in t1 asks whether to trust the folder, so the task was not typed. Ask the "
        'user; if they agree, call terminal with {"action": "key", "target": "t1", "key": '
        '"down"} then {"action": "key", "target": "t1", "key": "enter"}.'
    ) in text
    assert text.endswith(
        'Afterwards, call send_message with {"target": "t1", "text": "<the task>"}.'
    )


@pytest.mark.asyncio
async def test_names_every_terminal_that_needs_the_same_answer(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CLAUDE_TRUST]
    fx.app.screens["term_start2"] = [SHELL, CLAUDE_TRUST]
    text = await fx.ok("start_coding_terminal", program="claude", task="Fix it", count=2)
    assert fx.app.inputs == []
    assert "Claude Code in t1 and t2 asks whether to trust the folder" in text
    assert text.endswith('"<the task>"}. Do the same for t2.')


@pytest.mark.asyncio
async def test_reports_a_pending_codex_update_question(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL, CODEX_LOADING, CODEX_UPDATE]
    text = await fx.ok("start_coding_terminal", program="codex", task="Fix it")
    assert fx.app.inputs == []
    assert "Codex in t1 offers an update and waits, so the task was not typed." in text


@pytest.mark.asyncio
async def test_starts_several_terminals_in_the_existing_program_group(fx: Fixture) -> None:
    fx.app.groups.append({"group_id": "grp_codex", "name": "codex", "kind": "user"})
    text = await fx.ok("start_coding_terminal", program="codex", task="Go", count=2, name="Pair")
    assert text.endswith("t1, t2. Typed the task into t1 and t2 and sent it.")
    assert [params["group_id"] for params in fx.app.params("terminal.start")] == ["grp_codex"] * 2
    assert all(params["name"] == "Pair" for params in fx.app.params("terminal.start"))
    assert fx.app.count("terminal.group.create") == 0
    assert len(fx.app.inputs) == 4


@pytest.mark.asyncio
async def test_retypes_after_a_stale_screen_rejection(fx: Fixture) -> None:
    fx.app.fail("terminal.input", STALE)
    await fx.ok("start_coding_terminal", program="codex", task="Go")
    assert [data for _id, data, _revision in fx.app.inputs] == ["\x1b[200~Go\x1b[201~", "\r"]
    assert fx.app.count("terminal.input") == 3


@pytest.mark.asyncio
async def test_resolves_the_folder_from_a_project_or_an_existing_path(
    fx: Fixture, tmp_path: Path
) -> None:
    fx.app.projects.append({"project_id": "site", "display_name": "Site", "cwd": "C:\\work\\site"})
    fx.ui.context["projects"].append(
        {"project_id": "site", "name": "Site", "cwd": "C:\\work\\site"}
    )
    await fx.ok("start_coding_terminal", program="codex", folder="site")
    await fx.ok("start_coding_terminal", program="codex", folder=str(tmp_path))
    assert [params["workdir"] for params in fx.app.params("terminal.start")] == [
        "C:\\work\\site",
        str(tmp_path),
    ]
    code, message = await fx.failed(
        "start_coding_terminal", program="codex", folder=str(tmp_path / "missing")
    )
    assert code == "folder_not_found"
    assert "Projects: vBot, Site. Ask the user which Project or folder to use" in message


@pytest.mark.asyncio
async def test_asks_for_a_folder_when_no_project_is_selected(fx: Fixture) -> None:
    fx.ui.context["selected_project_id"] = ""
    code, message = await fx.failed("start_coding_terminal", program="claude", task="Fix it")
    assert code == "folder_missing"
    assert message.startswith("No folder was given and no Project is selected in the app.")
    assert fx.app.effects() == []


@pytest.mark.asyncio
async def test_reports_started_terminals_when_a_later_start_fails(fx: Fixture) -> None:
    fx.app.fail("terminal.start", None, RpcError("invalid_request", "Too many Terminals."))
    code, message = await fx.failed("start_coding_terminal", program="codex", task="Go", count=3)
    assert code == "partial"
    assert message == (
        "Started Codex in t1; starting Terminal 2 of 3 failed: Too many Terminals. Nothing was "
        "retried and no task was typed."
    )
    assert fx.app.inputs == []


@pytest.mark.asyncio
async def test_reports_a_program_that_exits_before_it_is_ready(fx: Fixture) -> None:
    fx.app.screens["term_start1"] = [SHELL]

    def exit_on_read(params: JsonObject) -> JsonObject:
        terminal = fx.app._terminal(params["terminal_id"])
        terminal["state"] = "exited"
        return {"terminal": terminal, "screen": SHELL, "bracketed_paste": False}

    fx.app._terminal_read = exit_on_read  # type: ignore[method-assign]
    text = await fx.ok("start_coding_terminal", program="codex", task="Go")
    assert text.endswith("t1 ended before Codex was ready; the task was not typed.")


# -- send_message ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sends_to_a_session_by_ref_in_tolerant_spellings(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder", has_active_run=True)]
    await fx.ok("overview")
    for spelling in ("s1", "S1", "s-1", "#s1", "s 1"):
        text = await fx.ok("send_message", target=spelling, text="yes, go on")
        assert text == "Sent to s1 (Coder). An update follows when it finishes."
    assert {params["session_id"] for params in fx.app.params("chat.stream")} == {"ses_1"}
    assert fx.app.params("chat.stream")[0]["input_origin"] == "speech_transcription"


@pytest.mark.asyncio
async def test_a_working_session_queues_the_message(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder", has_active_run=True)]
    fx.app.queued = True
    text = await fx.ok("send_message", target="ses_1", text="and add tests")
    assert text == (
        "Sent to s1 (Coder). It is still working, so the message waits in its Queue and runs next."
    )


@pytest.mark.asyncio
async def test_an_agent_name_needs_one_clear_session(fx: Fixture) -> None:
    fx.app.sessions = [
        session_row("ses_old", "coder"),
        session_row("ses_run", "coder", has_active_run=True, title="Fix login"),
    ]
    await fx.ok("send_message", target="Coder", text="yes")
    assert fx.app.params("chat.stream")[-1]["session_id"] == "ses_run"
    await fx.ok("start_agent_session", agent="Coder", task="Another")
    code, message = await fx.failed("send_message", target="coder", text="yes")
    assert code == "ambiguous_target"
    assert message.startswith(
        'Coder has several Sessions: s2 Coder: working, s1 Coder "Fix login": working.'
    )
    assert message.endswith("then call send_message again with its ref as target.")
    code, message = await fx.failed("send_message", target="Writer", text="yes")
    assert code == "no_session"
    assert 'Call overview with {"agent": "Writer"}' in message


@pytest.mark.asyncio
async def test_an_agent_name_skips_cron_and_channel_sessions_while_it_has_others(
    fx: Fixture,
) -> None:
    fx.app.sessions = [
        session_row("ses_own", "coder", has_active_run=True),
        session_row("ses_cron", "coder", has_active_run=True, run_kinds=["cron"]),
        session_row(
            "ses_chan", "coder", has_active_run=True, platform="telegram", platform_conv_id="42"
        ),
    ]
    await fx.ok("send_message", target="Coder", text="yes")
    assert [params["session_id"] for params in fx.app.params("chat.stream")] == ["ses_own"]

    # Only Cron and Channel Sessions are working: the name selects none of them.
    fx.app.sessions[0]["has_active_run"] = False
    fx.app.histories = {"ses_cron": {"messages": [], "active_run": {"run_id": "run_cron"}}}
    code, message = await fx.failed("stop", target="Coder")
    assert code == "no_session"
    assert "Cron or Channel Sessions" in message
    assert fx.app.count("chat.cancel") == 0
    listing = await fx.ok("overview", agent="Coder")
    assert "(Cron or Channel Session): working" in listing
    assert listing.count("Cron or Channel Session") == 2


@pytest.mark.asyncio
async def test_an_agent_name_selects_a_channel_session_when_it_has_no_other(fx: Fixture) -> None:
    fx.app.sessions = [
        session_row(
            "ses_chan", "writer", has_active_run=True, platform="telegram", platform_conv_id="42"
        )
    ]
    await fx.ok("send_message", target="Writer", text="yes")
    assert fx.app.params("chat.stream")[-1]["session_id"] == "ses_chan"


@pytest.mark.asyncio
async def test_a_name_shared_by_a_terminal_and_an_agent_is_ambiguous(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder", has_active_run=True)]
    fx.app.add_terminal("term_a", name="coder")
    # Neither the Agent's exact id nor the Terminal's name wins by kind.
    for target in ("coder", "Coder"):
        code, message = await fx.failed("send_message", target=target, text="yes")
        assert code == "ambiguous_target"
        assert "t1 (coder)" in message
        assert "Agent Coder (id coder)" in message
        assert 'call overview with {"agent": "<its id>"}' in message
    assert fx.app.effects() == []
    # The ways out the failure names work.
    await fx.ok("send_message", target="t1", text="yes")
    assert "s1 Coder: working" in await fx.ok("overview", agent="coder")


@pytest.mark.asyncio
async def test_open_picks_the_kind_by_view_when_names_are_shared(fx: Fixture) -> None:
    fx.app.groups.append({"group_id": "grp_vbot", "name": "vBot", "kind": "user"})
    code, message = await fx.failed("open", target="vBot")
    assert code == "ambiguous_target"
    assert 'group "vBot" (id grp_vbot)' in message
    assert "Project vBot (id vbot)" in message
    assert "view of the kind meant" in message
    assert fx.ui.of("open") == [] and fx.ui.of("terminal_view") == []
    await fx.ok("open", target="vBot", view="projects")
    await fx.ok("open", target="vBot", view="terminals")
    assert fx.ui.of("open") == [{"view": "projects", "project_id": "vbot"}]
    assert fx.ui.of("terminal_view") == [{"op": "show_group", "group_id": "grp_vbot"}]


@pytest.mark.asyncio
async def test_unknown_refs_and_targets_name_the_next_call(fx: Fixture) -> None:
    code, message = await fx.failed("send_message", target="s7", text="yes")
    assert (code, message) == (
        "unknown_ref",
        "There is no s7 in this call. Call overview to see the current refs, then call "
        "send_message again with one of them as target.",
    )
    code, message = await fx.failed("send_message", target="the other one", text="yes")
    assert code == "target_not_found"
    assert message.startswith('No Session, Terminal or Agent matches "the other one".')
    assert fx.app.effects() == []


@pytest.mark.asyncio
async def test_types_a_message_into_a_coding_terminal(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", name="Build")
    text = await fx.ok("send_message", target="Build", text="line one\nline two")
    assert text == "Sent to t1 (Codex)."
    assert [data for _id, data, _revision in fx.app.inputs] == [
        "\x1b[200~line one\nline two\x1b[201~",
        "\r",
    ]


@pytest.mark.asyncio
async def test_does_not_type_into_a_shell_a_menu_or_with_control_characters(fx: Fixture) -> None:
    fx.app.add_terminal("term_shell", "pwsh")
    fx.app.add_terminal("term_menu")
    fx.app.screens["term_menu"] = [CODEX_UPDATE]
    code, message = await fx.failed("send_message", target="term_shell", text="dir")
    assert code == "not_a_coding_terminal"
    assert message.startswith("t1 does not run Codex or Claude Code;")
    code, message = await fx.failed("send_message", target="term_menu", text="yes")
    assert code == "not_sent"
    assert "is asking a startup question, so nothing was sent" in message
    code, _message = await fx.failed("send_message", target="term_menu", text="a\x1b[201~b")
    assert code == "invalid_text"
    assert fx.app.inputs == []


@pytest.mark.asyncio
async def test_reports_send_failures_with_their_certainty(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder")]
    fx.app.fail("chat.stream", RpcError("invalid_request", "Session is archived."), RuntimeError())
    code, message = await fx.failed("send_message", target="ses_1", text="hi")
    assert (code, message) == (
        "invalid_request",
        "Nothing was sent to s1 (Coder): Session is archived.",
    )
    code, message = await fx.failed("send_message", target="ses_1", text="hi")
    assert code == "send_failed"
    assert message.endswith("It may or may not have been delivered; do not send it again.")


# -- read and stop -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_a_session_as_quoted_text_within_the_budget(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder")]
    fx.app.histories["ses_1"] = {
        "messages": [
            {"role": "user", "content": "old " * 3000},
            {"role": "tool", "content": "hidden"},
            {"role": "user", "content": "Fix it"},
            {"role": "assistant", "content": "Done.\nShould I push?"},
        ],
        "active_run": None,
    }
    text = await fx.ok("read", target="ses_1")
    assert text.startswith(
        "s1 at Coder, not working. Latest messages, quoted:\nUser (earlier part cut):\n> old old"
    )
    assert text.endswith("User:\n> Fix it\nCoder:\n> Done.\n> Should I push?")
    assert "hidden" not in text


@pytest.mark.asyncio
async def test_reads_the_latest_session_of_an_agent_without_a_recent_one(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "writer")]
    text = await fx.ok("read", target="Writer")
    assert text == "s1 at Writer (not working) has no messages yet."


@pytest.mark.asyncio
async def test_reads_a_coding_terminal_screen_as_quoted_text(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", state="working")
    fx.app.screens["term_a"] = ["x" * 9000 + "\nlast line\n\n"]
    text = await fx.ok("read", target="term_a")
    assert text.startswith("t1 (Codex), working. Screen, last part, quoted:\n> ")
    assert text.endswith("> last line")


@pytest.mark.asyncio
async def test_stops_the_active_run_of_a_session_or_says_nothing_changed(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder", has_active_run=True)]
    fx.app.histories["ses_1"] = {"messages": [], "active_run": {"run_id": "run_9"}}
    text = await fx.ok("stop", target="coder")
    assert text == "Stopped the current work of s1 (Coder). It stays open for new messages."
    assert fx.app.params("chat.cancel") == [{"run_id": "run_9"}]
    fx.app.histories["ses_1"] = {"messages": [], "active_run": None}
    text = await fx.ok("stop", target="s1")
    assert text == "s1 (Coder) is not working on anything; nothing changed."
    assert fx.app.count("chat.cancel") == 1


@pytest.mark.asyncio
async def test_stops_a_coding_terminal_with_its_interrupt_key(fx: Fixture) -> None:
    fx.app.add_terminal("term_a", state="working")
    text = await fx.ok("stop", target="term_a")
    assert text.startswith("Pressed Escape in t1 to interrupt it.")
    assert fx.app.inputs == [("term_a", "\x1b", 5)]


# -- open ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opens_sessions_terminals_groups_projects_and_views(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder")]
    fx.app.add_terminal("term_a")
    assert await fx.ok("open", target="ses_1") == "Showing s1 (Coder) in the chat."
    assert await fx.ok("open", target="term_a") == "Showing t1 in the Terminals view."
    assert await fx.ok("open", target="Mine") == 'Showing the group "Mine" in the Terminals view.'
    assert await fx.ok("open", target="vBot") == "Showing the page of Project vBot."
    assert await fx.ok("open", view="terminals") == "Opened the terminals view."
    assert fx.ui.of("open") == [
        {"view": "chat", "agent_id": "coder", "session_id": "ses_1"},
        {"view": "projects", "project_id": "vbot"},
        {"view": "terminals"},
    ]
    assert fx.ui.of("terminal_view") == [
        {"op": "show", "terminal_id": "term_a"},
        {"op": "show_group", "group_id": "grp_mine"},
    ]


@pytest.mark.asyncio
async def test_opens_an_agent_by_its_latest_session_or_its_page(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder")]
    text = await fx.ok("open", target="Coder")
    assert text == "Showing the latest Session of Coder, s1, in the chat."
    assert await fx.ok("open", target="Coder", view="agents") == "Showing the page of Agent Coder."
    assert await fx.ok("open", target="Writer") == "Showing the page of Agent Writer."
    text = await fx.ok("open", target="Reviewer")
    assert text == (
        "Reviewer belongs to the team of Project vBot, which has no Agent page; showing that "
        "Project's page."
    )
    assert fx.ui.of("open")[1:] == [
        {"view": "agents", "agent_id": "coder"},
        {"view": "agents", "agent_id": "writer"},
        {"view": "projects", "project_id": "vbot"},
    ]


@pytest.mark.asyncio
async def test_open_reports_navigation_that_did_not_happen(fx: Fixture) -> None:
    code, message = await fx.failed("open")
    assert code == "missing_target"
    fx.ui.applied = False
    code, message = await fx.failed("open", view="agents")
    assert (code, message) == ("navigation_not_applied", "The app did not switch to it.")
    fx.ui.error = LiveUiError("ui_timeout", uncertain=True)
    code, message = await fx.failed("open", view="agents")
    assert code == "ui_timeout"
    assert "may or may not show the change" in message


# -- terminal ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arranges_terminals_and_presses_keys(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    assert await fx.ok("terminal", action="maximize", target="term_a") == (
        "Maximized t1 in the Terminals view."
    )
    assert await fx.ok("terminal", action="restore") == (
        "Restored the Terminals view to its group layout."
    )
    text = await fx.ok("terminal", action="key", target="t1", key="enter")
    assert text == "Pressed Enter in t1. Call read with t1 to see the result."
    assert fx.app.inputs == [("term_a", "\r", 5)]
    code, message = await fx.failed("terminal", action="key", target="t1")
    assert code == "missing_key"
    assert '{"action": "key", "target": "t1", "key": "enter"}' in message


@pytest.mark.asyncio
async def test_closes_a_terminal_by_stopping_then_removing_it(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    assert await fx.ok("terminal", action="close", target="term_a") == (
        "Closed t1: stopped and removed."
    )
    assert fx.app.effects() == ["terminal.kill", "terminal.forget"]
    assert fx.ui.of("terminal_view")[-1] == {"op": "refresh"}


@pytest.mark.asyncio
async def test_close_reports_a_confirmed_stop_when_removal_fails(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    fx.app.fail("terminal.forget", RpcError("invalid_request", "Busy."))
    code, message = await fx.failed("terminal", action="close", target="term_a")
    assert code == "partial"
    assert message == (
        "t1 was stopped but may not have been removed. Busy. Call overview to check before "
        "closing it again."
    )


@pytest.mark.asyncio
async def test_manages_editable_groups_only(fx: Fixture) -> None:
    text = await fx.ok("terminal", action="create_group", name="Review")
    assert text == 'Created the Terminal group "Review".'
    text = await fx.ok("terminal", action="rename_group", target="mine", name="Ours")
    assert text == 'Renamed the group "Mine" to "Ours".'
    text = await fx.ok("terminal", action="delete_group", target="Mine")
    assert text == 'Deleted the group "Mine" and stopped 2 Terminals.'
    code, message = await fx.failed("terminal", action="delete_group", target="Finished")
    assert code == "group_not_editable"
    assert fx.app.count("terminal.group.delete") == 1


@pytest.mark.asyncio
async def test_reorders_a_group_by_refs_and_infers_the_group(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    fx.app.add_terminal("term_b")
    await fx.ok("overview")
    text = await fx.ok("terminal", action="reorder", order=["t2", "t1"])
    assert text == 'Reordered the group "Mine": t2, t1.'
    assert fx.app.params("terminal.group.order") == [
        {"group_id": "grp_mine", "order": ["term_b", "term_a"]}
    ]
    code, message = await fx.failed("terminal", action="reorder", order=["t2"])
    assert code == "invalid_order"
    assert 'every Terminal of the group "Mine" exactly once: t1, t2.' in message


@pytest.mark.asyncio
async def test_keeps_a_completed_change_when_the_layout_refresh_fails(fx: Fixture) -> None:
    fx.app.add_terminal("term_a")
    fx.ui.error = LiveUiError("ui_unavailable")
    text = await fx.ok("terminal", action="close", target="term_a")
    assert text == (
        "Closed t1: stopped and removed. The app window did not update its Terminals view."
    )


# -- execution -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_does_nothing_once_the_call_stopped(fx: Fixture) -> None:
    fx.active = False
    code, message = await fx.failed("overview")
    assert code == "voice_stopped"
    assert fx.app.calls == []


@pytest.mark.asyncio
async def test_reports_an_unexpected_failure_as_uncertain(fx: Fixture) -> None:
    fx.app.fail("terminal.list", RuntimeError("boom"))
    code, message = await fx.failed("terminal", action="close", target="t9x")
    assert code == "operation_failed"
    assert "It may or may not have been delivered; do not send it again." in message
    fx.app.fail("terminal.list", RuntimeError("boom"))
    code, message = await fx.failed("read", target="term_x")
    assert (code, message) == (
        "operation_failed",
        "read failed unexpectedly; nothing was changed. Call it again once, and tell the user if "
        "it fails again.",
    )


@pytest.mark.asyncio
async def test_session_refs_are_shared_with_run_announcements(fx: Fixture) -> None:
    await fx.ok("start_agent_session", agent="Coder", task="x")
    assert fx.executor.session_ref("coder", "ses_new1") == "s1"
    assert fx.executor.session_ref("writer", "ses_other") == "s2"
