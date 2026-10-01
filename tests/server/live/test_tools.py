"""Live Tools as the voice call runs them: overview, Agent Sessions, messages, reading,
stopping and opening, and the refs one call keeps."""

from __future__ import annotations

import pytest

from server.live._context import LiveUiError
from server.rpc.errors import RpcError
from tests.server.live.tools_test_support import (
    AFTER,
    PROJECT_FOLDER_SHOWN,
    Fixture,
    session_row,
)


@pytest.fixture
def fx() -> Fixture:
    return Fixture()


# -- overview ----------------------------------------------------------------------------


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
    # Codex ended (or never started) in term_c; only its shell is still open.
    fx.app.add_terminal("term_c", state="ready")
    fx.app.programs_running = {"term_a": True, "term_c": False}
    fx.context = {
        "view": "chat",
        "selected_agent_id": "main",
        "selected_project_id": "vbot",
        "chat_session": {"agent_id": "writer", "session_id": "ses_ask"},
    }

    text = await fx.ok("overview")

    assert text.splitlines()[:4] == [
        "App: chat view; showing s2 (Writer); selected Agent: Main; selected Project: vBot.",
        "Agents: Main, Coder, Writer.",
        "Team of Project vBot: Reviewer.",
        f"Projects: vBot ({PROJECT_FOLDER_SHOWN}).",
    ]
    assert '- s1 Coder "Fix login": working' in text
    assert (
        '- s2 Writer "Blog post": waiting for an answer: '
        '"I drafted it. Should I use a formal tone?"'
    ) in text
    assert '- s3 Reviewer "Review": finished: "...' in text
    assert 'Done."' in text
    assert "Old draft" not in text
    # Folders read with forward slashes, whatever the host writes.
    assert f'- t1 Codex "Build" in {PROJECT_FOLDER_SHOWN}: idle' in text
    assert (
        f"- t2 Codex in {PROJECT_FOLDER_SHOWN}: Codex is not running; only the command line is open"
    ) in text
    assert f"- t3 pwsh in {PROJECT_FOLDER_SHOWN}: exited" in text
    assert fx.app.effects() == []
    assert fx.ui.requests == []


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
async def test_overview_without_an_app_window_report_leaves_out_the_selection(
    fx: Fixture,
) -> None:
    fx.context = None
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


# -- start_agent_session -----------------------------------------------------------------


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
            "input_origin": "live_voice",
        }
        for index in (1, 2, 3)
    ]


@pytest.mark.asyncio
async def test_starts_a_team_agent_of_the_selected_or_named_project(fx: Fixture) -> None:
    text = await fx.ok("start_agent_session", agent="Reviewer", task="Review it")
    assert text.startswith("Started a Session at Reviewer (vBot team) with the task: s1.")
    assert fx.app.params("session.create") == [{"agent_id": "reviewer@vbot"}]
    assert fx.context is not None
    fx.context["selected_project_id"] = None
    await fx.ok("start_agent_session", agent="reviewer", project="vBot", task="Again")
    assert fx.app.params("session.create")[-1] == {"agent_id": "reviewer@vbot"}
    # Reading a team rescans its Project, so the call reuses it for a while.
    assert fx.app.params("project.show") == [{"project_id": "vbot"}]


@pytest.mark.asyncio
async def test_does_not_guess_an_unknown_or_ambiguous_agent(fx: Fixture) -> None:
    code, message = await fx.failed("start_agent_session", agent="Codr", task="x")
    assert code == "target_not_found"
    assert 'No Agent matches "Codr". Agents: Main, Coder, Writer, Reviewer (vBot team).' in message
    assert "call start_agent_session again with one of them as agent" in message
    fx.app.teams["vbot"] = [{"agent_id": "coder", "display_name": "Coder"}]
    fx.clock.now += 61  # The cached team expires.
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


# -- send_message to Sessions ------------------------------------------------------------


@pytest.mark.asyncio
async def test_sends_to_a_session_by_ref_in_tolerant_spellings(fx: Fixture) -> None:
    fx.app.sessions = [session_row("ses_1", "coder", has_active_run=True)]
    await fx.ok("overview")
    for spelling in ("s1", "S1", "s-1", "#s1", "s 1"):
        text = await fx.ok("send_message", target=spelling, text="yes, go on")
        assert text == "Sent to s1 (Coder). An update follows when it finishes."
    assert {params["session_id"] for params in fx.app.params("chat.stream")} == {"ses_1"}
    assert fx.app.params("chat.stream")[0]["input_origin"] == "live_voice"


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
async def test_unknown_refs_and_targets_name_the_next_call(fx: Fixture) -> None:
    code, message = await fx.failed("send_message", target="s7", text="yes")
    assert (code, message) == (
        "unknown_ref",
        "There is no s7. Call overview to see the current refs, then call "
        "send_message again with one of them as target.",
    )
    code, message = await fx.failed("send_message", target="the other one", text="yes")
    assert code == "target_not_found"
    assert message.startswith('No Session, Terminal or Agent matches "the other one".')
    assert fx.app.effects() == []


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


# -- read and stop Sessions --------------------------------------------------------------


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


# -- open --------------------------------------------------------------------------------


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
async def test_opens_views_that_show_no_single_thing_only_without_a_target(fx: Fixture) -> None:
    assert await fx.ok("open", view="settings") == "Opened the settings view."
    code, message = await fx.failed("open", view="cron", target="Coder")
    assert code == "invalid_view"
    assert message.startswith(
        'The cron view shows no single thing. Call open again with only {"view": "cron"}'
    )
    assert fx.ui.of("open") == [{"view": "settings"}]


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


# -- end_call ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_call_asks_the_call_to_end_after_a_goodbye(fx: Fixture) -> None:
    text = await fx.ok("end_call")
    assert text == "The call ends in a few seconds. Say a short goodbye now; running work goes on."
    assert fx.ended == 1
    assert fx.app.effects() == []


# -- execution and refs ------------------------------------------------------------------


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
async def test_known_refs_say_what_each_ref_names_most_recent_last(fx: Fixture) -> None:
    """A later delegation sees the call's refs without reading them again."""
    assert fx.executor.known_refs() == ""
    fx.app.sessions = [session_row("ses_run", "coder", title="Fix login", has_active_run=True)]
    fx.app.add_terminal("term_a", name="Build")
    await fx.ok("overview")
    # Addressing s1 again keeps its title and makes it the most recent ref.
    await fx.ok("send_message", target="s1", text="go on")
    await fx.ok("start_coding_terminal", program="claude", count=1, name="Docs")

    assert fx.executor.known_refs().splitlines() == [
        '- t1: Codex Terminal "Build"',
        '- s1: Session at Coder "Fix login"',
        '- t2: Claude Code Terminal "Docs"',
    ]
    # A name set in the app stays on its own short line.
    fx.app.add_terminal("term_long", name="Line one\nline two " + "x" * 80)
    await fx.ok("read", target="term_long")
    last = fx.executor.known_refs().splitlines()[-1]
    assert last.startswith('- t3: Codex Terminal "Line one line two xx')
    assert last.endswith('..."')
    assert len(last) < 90


@pytest.mark.asyncio
async def test_session_refs_are_shared_with_run_announcements(fx: Fixture) -> None:
    await fx.ok("start_agent_session", agent="Coder", task="x")
    assert fx.executor.session_ref("coder", "ses_new1") == "s1"
    assert fx.executor.session_ref("writer", "ses_other") == "s2"
