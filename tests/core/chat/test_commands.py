"""Tests for commands."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from core.chat import (
    CommandDispatcher,
    ReplySurface,
)
from core.chat.commands import (
    AgentArgument,
    HandoffArgument,
    parse_agent_argument,
    parse_handoff_argument,
)
from core.chat.status_report import (
    STATUS_PLACEHOLDER,
)
from core.projects import AgentResolver, ProjectStore
from core.runs import ChatRunManager, Run, RunCancelledError
from core.sessions import SessionAddress
from tests.core.chat.commands_test_support import (
    _execute,
    _execute_sync,
    _make_agent,
    _prepared,
    _StubProject,
    _StubProjects,
    _StubResolver,
)


class _StubStoredAgent:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id


class _StubAgentStore:
    """Agent store stub exposing only the directory card's ``list`` seam."""

    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_StubStoredAgent(agent_id) for agent_id in agent_ids]

    def list(self) -> list[_StubStoredAgent]:
        return list(self._agents)


class _StubScannedAgent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


class _StubScanReport:
    def __init__(self, team: list[_StubScannedAgent]) -> None:
        self.team = team


class _StubTeamResolver:
    """Resolver stub returning a fixed team for the directory card."""

    def __init__(self, team: list[str]) -> None:
        self._team = [_StubScannedAgent(agent_id) for agent_id in team]

    def scan_project_report(self, project: Any) -> _StubScanReport:
        return _StubScanReport(self._team)


@pytest.mark.asyncio
async def test_dispatch_stop_with_active_run_returns_cancelled_reply() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), execute
    )
    await started.wait()

    dispatcher = CommandDispatcher(manager)
    result = await _execute(dispatcher, " /STOP ")

    assert result.feedback is not None
    assert result.feedback.text
    assert run.cancel_requested is True
    assert run.cancel_reason == "user"

    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


def test_dispatch_stop_with_no_active_run_returns_not_found_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/stop")

    assert result.feedback is not None
    assert result.feedback.text


def test_dispatch_unknown_command_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    assert dispatcher.prepare("/bogus") is None


def test_dispatch_non_command_message_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    assert dispatcher.prepare("hello") is None


def test_built_in_commands_include_current_catalog() -> None:
    assert set(CommandDispatcher.BUILT_IN_COMMANDS) == {
        "agent",
        "compact",
        "handoff",
        "help",
        "learn",
        "model",
        "new",
        "reflect",
        "rename",
        "status",
        "stop",
    }


def test_built_in_commands_declare_argument_and_result_metadata() -> None:
    specs = CommandDispatcher.BUILT_IN_COMMANDS
    argument_modes = {name: spec.argument for name, spec in specs.items()}
    result_kinds = {name: spec.catalog_result for name, spec in specs.items()}

    assert argument_modes == {
        "agent": "optional",
        "compact": "optional",
        "handoff": "optional",
        "help": "none",
        "learn": "optional",
        "model": "optional",
        "new": "none",
        "reflect": "optional",
        "rename": "optional",
        "status": "none",
        "stop": "none",
    }
    assert result_kinds == {
        "agent": "state_change",
        "compact": "notice",
        "handoff": "state_change",
        "help": "detail",
        "learn": "state_change",
        "model": "state_change",
        "new": "state_change",
        "reflect": "state_change",
        "rename": "notice",
        "status": "detail",
        "stop": "notice",
    }


def test_dispatch_help_marks_transient_output() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/help")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"


def test_dispatch_stop_marks_toast_output() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/stop")

    assert result.feedback is not None
    assert result.feedback.kind == "notice"


def test_dispatch_handoff_without_argument_returns_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/handoff")

    assert (result.name, result.argument, result.execution_mode) == ("handoff", None, "serialized")


def test_dispatch_learn_without_argument_returns_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/learn")

    assert (result.name, result.argument, result.execution_mode) == ("learn", None, "serialized")


def test_dispatch_learn_takes_full_remainder_as_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/learn the deploy steps we just did")

    assert (result.name, result.argument) == ("learn", "the deploy steps we just did")


def test_dispatch_reflect_without_argument_returns_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/reflect")

    assert (result.name, result.argument, result.execution_mode) == ("reflect", None, "serialized")


def test_dispatch_reflect_takes_full_remainder_as_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/reflect focus on the memory side")

    assert (result.name, result.argument) == ("reflect", "focus on the memory side")


def test_dispatch_handoff_with_agent_id_returns_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/handoff coder")

    assert (result.name, result.argument) == ("handoff", "coder")


def test_dispatch_handoff_preserves_agent_id_case() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/handoff MyAgent")

    assert result.name == "handoff"
    assert result.argument == "MyAgent"


def test_dispatch_handoff_tolerates_surrounding_whitespace() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "  /handoff coder  ")

    assert (result.name, result.argument) == ("handoff", "coder")


def test_dispatch_handoff_takes_full_remainder_as_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/handoff agent:main do not forget")

    assert (result.name, result.argument) == ("handoff", "agent:main do not forget")


def test_dispatch_agent_without_argument_lists_personal_and_team_directory() -> None:
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, _StubTeamResolver(["builder", "planner"])),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
        agents=cast(Any, _StubAgentStore(["assistant", "coder"])),
    )

    result = _execute_sync(dispatcher, "/agent", agent_id="assistant")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    assert not result.facts
    reply = result.feedback.text
    assert "assistant" in reply
    assert "coder" in reply
    # Team agents are shown project-qualified, teaching the address the move expects.
    assert "builder@vbot" in reply
    assert "planner@vbot" in reply


def test_dispatch_agent_with_address_returns_move_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/agent planner")

    assert (result.name, result.argument, result.execution_mode) == (
        "agent",
        "planner",
        "serialized",
    )


def test_dispatch_agent_keeps_task_in_raw_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/agent builder@vbot ship the fix")

    assert (result.name, result.argument) == ("agent", "builder@vbot ship the fix")


@pytest.mark.parametrize("message", ["/agent", "/agent planner"])
def test_agent_is_unavailable_on_every_channel_form(message: str) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    prepared = _prepared(dispatcher, message)

    unavailable = dispatcher.unavailability(
        prepared,
        ReplySurface.channel(
            platform="telegram",
            platform_display_name="Telegram",
            channel_id="tg-assistant",
        ),
    )

    assert unavailable is not None
    assert unavailable.command == "/agent"
    assert dispatcher.unavailability(prepared, ReplySurface.webui()) is None


def test_dispatch_model_with_value_returns_set_model_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/model openai/gpt-5")

    assert (result.name, result.argument, result.execution_mode) == (
        "model",
        "openai/gpt-5",
        "serialized",
    )


def test_dispatch_model_reset_returns_set_model_action() -> None:
    # The reset token is passed through verbatim; the accessor layer interprets it.
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/model reset")

    assert (result.name, result.argument) == ("model", "reset")


def test_dispatch_model_without_argument_shows_identity_source() -> None:
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, _StubResolver(_make_agent(), model_source="agent")),
    )

    result = _execute_sync(dispatcher, "/model")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    assert not result.facts
    reply = result.feedback.text
    assert "openai/gpt-5.2" in reply
    assert "agent configuration" in reply


def _model_reply(
    *,
    project_id: str | None,
    model_value: str | None,
    model_source: str | None,
) -> str:
    """Dispatch a bare /model and return the reply for a chosen effective-model tier.

    Drives the origin wording purely through the stub resolver's ``effective_config``
    (value + source), the only seam ``/model`` now reads for provenance.
    """
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(
            AgentResolver,
            _StubResolver(
                _make_agent(),
                model_value=model_value,
                model_source=model_source,
            ),
        ),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot")))
        if project_id is not None
        else None,
    )
    result = _execute_sync(dispatcher, "/model", project_id=project_id)
    assert result.feedback is not None
    return result.feedback.text


def test_dispatch_model_identity_global_default_origin() -> None:
    reply = _model_reply(
        project_id=None, model_value="openai/gpt-5.2", model_source="global_default"
    )

    assert "global default" in reply


def test_dispatch_model_identity_none_source_origin() -> None:
    reply = _model_reply(project_id=None, model_value=None, model_source=None)

    assert "not configured" in reply
    assert STATUS_PLACEHOLDER in reply


def test_dispatch_model_project_override_origin() -> None:
    # A project session whose winning tier is the override resolves to it and labels it.
    reply = _model_reply(project_id="vbot", model_value="openai/gpt-mini", model_source="override")

    assert "openai/gpt-mini" in reply
    assert "override (set via /model)" in reply


def test_dispatch_model_project_agent_origin() -> None:
    reply = _model_reply(project_id="vbot", model_value="openai/gpt-5.2", model_source="agent")

    assert "agent file in repo" in reply


def test_dispatch_model_project_project_default_origin() -> None:
    reply = _model_reply(
        project_id="vbot", model_value="openai/gpt-5.2", model_source="project_default"
    )

    assert "project default" in reply


def test_dispatch_model_project_global_default_origin() -> None:
    reply = _model_reply(
        project_id="vbot", model_value="openai/gpt-5.2", model_source="global_default"
    )

    assert "global default" in reply


def test_dispatch_model_project_none_source_origin() -> None:
    reply = _model_reply(project_id="vbot", model_value=None, model_source=None)

    assert "not configured" in reply
    assert STATUS_PLACEHOLDER in reply


def test_dispatch_model_without_services_degrades_to_placeholder() -> None:
    # A minimally constructed dispatcher (no resolver/projects) must not crash.
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/model")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    assert STATUS_PLACEHOLDER in result.feedback.text
    assert "not configured" in result.feedback.text


def test_parse_agent_argument_splits_first_token_as_address() -> None:
    assert parse_agent_argument("planner") == AgentArgument(address="planner", task=None)
    assert parse_agent_argument("builder@vbot do X") == AgentArgument(
        address="builder@vbot", task="do X"
    )
    assert parse_agent_argument("  planner   ship it  ") == AgentArgument(
        address="planner", task="ship it"
    )


def test_parse_handoff_argument_empty_is_neither_target_nor_instruction() -> None:
    assert parse_handoff_argument(None) == HandoffArgument(target_agent_id=None, instruction=None)
    assert parse_handoff_argument("   ") == HandoffArgument(target_agent_id=None, instruction=None)


def test_parse_handoff_argument_agent_prefix_only_selects_target() -> None:
    assert parse_handoff_argument("agent:main") == HandoffArgument(
        target_agent_id="main", instruction=None
    )


def test_parse_handoff_argument_instruction_only_keeps_current_agent() -> None:
    assert parse_handoff_argument("don't forget the plates!") == HandoffArgument(
        target_agent_id=None, instruction="don't forget the plates!"
    )


def test_parse_handoff_argument_agent_prefix_with_instruction() -> None:
    assert parse_handoff_argument("agent:main don't forget the plates!") == HandoffArgument(
        target_agent_id="main", instruction="don't forget the plates!"
    )


def test_parse_handoff_argument_keyword_is_case_insensitive_id_keeps_case() -> None:
    assert parse_handoff_argument("Agent:MyReviewer review carefully") == HandoffArgument(
        target_agent_id="MyReviewer", instruction="review carefully"
    )


def test_parse_handoff_argument_bare_agent_prefix_falls_through_to_instruction() -> None:
    # ``agent:`` with no id is not a valid target slot.
    assert parse_handoff_argument("agent: do the thing") == HandoffArgument(
        target_agent_id=None, instruction="agent: do the thing"
    )


def test_parse_handoff_argument_colon_in_free_text_does_not_capture_target() -> None:
    assert parse_handoff_argument("remember: call bob") == HandoffArgument(
        target_agent_id=None, instruction="remember: call bob"
    )


def test_dispatch_compact_with_instruction_returns_action_with_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/compact keep the API design")

    assert (result.name, result.argument) == ("compact", "keep the API design")


def test_dispatch_compact_without_instruction_returns_action_without_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/compact")

    assert (result.name, result.argument) == ("compact", None)


@pytest.mark.asyncio
async def test_execute_compact_exposes_the_compaction_run_as_primary() -> None:
    run = Run(run_id="run-compact", agent_id="coder", session_id="session-one")

    class Trigger:
        async def start_compaction_run(
            self,
            agent_id: str,
            session_id: str,
            instruction: str | None,
            *,
            project_id: str | None,
        ) -> Run:
            assert (agent_id, session_id, instruction, project_id) == (
                "coder",
                "session-one",
                "keep the API design",
                "project-one",
            )
            return run

    result = await _execute(
        CommandDispatcher(ChatRunManager(), trigger_service=Trigger()),
        "/compact keep the API design",
        project_id="project-one",
    )

    assert result.feedback is None
    assert len(result.runs) == 1
    assert result.runs[0].role == "primary"
    assert result.runs[0].run is run


def test_dispatch_no_argument_command_with_trailing_text_is_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    assert dispatcher.prepare("/status now") is None


@pytest.mark.parametrize(
    ("message", "command_name"),
    [
        ("/compact", "compact"),
        ("/new", "new"),
        ("/rename", "rename"),
    ],
)
def test_prepare_serialized_commands(message: str, command_name: str) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, message)

    assert result.name == command_name
    assert result.execution_mode == "serialized"


def test_dispatch_rename_with_value_threads_title_argument() -> None:
    # The raw title travels verbatim; the accessor owns normalization and the write.
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/rename Release planning")

    assert (result.name, result.argument) == ("rename", "Release planning")


def test_dispatch_rename_without_argument_clears_via_none() -> None:
    # No argument is the clear signal: it reaches the accessor as ``None``.
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _prepared(dispatcher, "/rename")

    assert (result.name, result.argument) == ("rename", None)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/stop", True),
        (" /STOP ", True),
        ("/handoff", True),
        ("/handoff coder", True),
        ("/handoff a b", True),
        ("/compact keep the design", True),
        ("/rename", True),
        ("/rename Release planning", True),
        ("/bogus", False),
        ("/stop now", False),
        ("hello", False),
    ],
)
def test_prepare_recognizes_the_command_catalog(message: str, expected: bool) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    assert (dispatcher.prepare(message) is not None) is expected


@pytest.mark.asyncio
async def test_recognizes_does_not_execute_command_side_effects() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), execute
    )
    await started.wait()

    dispatcher = CommandDispatcher(manager)
    recognized = dispatcher.prepare("/stop") is not None

    assert recognized is True
    assert run.cancel_requested is False

    release.set()
    assert await run.wait() == "done"


def test_dispatch_help_returns_current_command_list() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/help")

    assert result.feedback is not None
    reply = result.feedback.text
    assert "/compact - Compact the current session's context immediately." in reply
    assert "/continue" not in reply
    assert "/retry" not in reply
    assert "$skill-name" in reply
