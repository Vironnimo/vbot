"""Tests for commands extensions."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from core.chat import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    ExtensionCommandContext,
    ReplySurface,
)
from core.runs import ChatRunManager, Run
from tests.core.chat.commands_test_support import (
    _execute,
    _execute_sync,
    _prepared,
)


def test_extension_command_registers_in_catalog_and_executes_sync_handler() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    observed: list[tuple[str, str | None]] = []

    def handler(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
        observed.append((context.session_id, argument))
        return CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text="Workflow started."),
        )

    registration_id = dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
    )

    prepared = _prepared(dispatcher, "/workflow review this")
    result = _execute_sync(dispatcher, "/workflow review this")

    assert registration_id > 0
    assert prepared.registration_id == registration_id
    assert [spec.name for spec in dispatcher.catalog()][-1] == "workflow"
    assert result.feedback == CommandFeedback(kind="notice", text="Workflow started.")
    assert observed == [("session-one", "review this")]


def test_extension_command_leaves_same_named_dollar_skill_trigger_unclaimed() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=lambda _context, _argument: CommandOutcome(command="workflow"),
    )

    assert dispatcher.prepare("/workflow") is not None
    assert dispatcher.prepare("$workflow") is None


@pytest.mark.asyncio
async def test_extension_command_supports_async_handler_and_follow_up_run() -> None:
    follow_up = cast(Run, object())

    class Trigger:
        async def trigger_run(self, *args: object, **kwargs: object) -> Run:
            assert args[:3] == ("coder", "$workflow inspect", "session-one")
            assert kwargs["internal"] is True
            assert kwargs["project_id"] == "project-one"
            return follow_up

    async def handler(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
        run = await context.start_run(f"$workflow {argument}", internal=True)
        return CommandOutcome(command="workflow", facts={"same_run": run is follow_up})

    dispatcher = CommandDispatcher(ChatRunManager(), trigger_service=Trigger())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
        argument="required",
    )

    result = await _execute(
        dispatcher,
        "/workflow inspect",
        project_id="project-one",
    )

    assert result.facts == {"same_run": True}


def test_extension_command_rejects_invalid_metadata() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    with pytest.raises(ValueError):
        dispatcher.register_extension_command(
            "workflow_ext",
            name="Bad Name",
            description="Bad.",
            handler=lambda _context, _argument: CommandOutcome(command="bad"),
        )

    with pytest.raises(ValueError):
        dispatcher.register_extension_command(
            "workflow_ext",
            name="help",
            description="Shadow help.",
            handler=lambda _context, _argument: CommandOutcome(command="help"),
        )

    with pytest.raises(ValueError):
        dispatcher.register_extension_command(
            "workflow_ext",
            name="workflow",
            description="Bad argument metadata.",
            handler=lambda _context, _argument: CommandOutcome(command="workflow"),
            argument=cast(Any, []),
        )


@pytest.mark.asyncio
async def test_stale_extension_command_returns_neutral_feedback() -> None:
    called = False

    def handler(_context: ExtensionCommandContext, _argument: str | None) -> CommandOutcome:
        nonlocal called
        called = True
        return CommandOutcome(command="workflow")

    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
    )
    prepared = _prepared(dispatcher, "/workflow")
    dispatcher.unregister_extension_commands("workflow_ext")

    result = await dispatcher.execute(
        prepared,
        CommandExecutionContext(
            agent_id="coder",
            session_id="session-one",
            project_id=None,
            reply_surface=ReplySurface.webui(),
        ),
    )

    assert called is False
    assert result.feedback is not None
    assert "no longer available" in result.feedback.text


@pytest.mark.asyncio
async def test_extension_command_failure_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_context: ExtensionCommandContext, _argument: str | None) -> CommandOutcome:
        raise RuntimeError("boom")

    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
    )

    result = await _execute(dispatcher, "/workflow")

    assert result.feedback is not None
    assert result.feedback.text
    assert caplog.records


@pytest.mark.asyncio
async def test_extension_command_invalid_nested_outcome_is_isolated() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Start the workflow.",
        handler=lambda _context, _argument: CommandOutcome(
            command="workflow",
            feedback=cast(Any, "not feedback"),
        ),
    )

    result = await _execute(dispatcher, "/workflow")

    assert result.feedback is not None
    assert result.feedback.text


def test_dispatch_status_marks_transient_output() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["workflow_ext", "another_ext"])
async def test_extension_command_opens_only_its_registered_page(owner: str) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    navigation = CommandNavigation(
        kind="open_extension_page", extension=owner, page="overview", route="items/ä-1"
    )
    dispatcher.register_extension_command(
        owner,
        name="workflow",
        description="Open the workflow.",
        page_ids=frozenset({"overview"}),
        handler=lambda _context, _argument: CommandOutcome(
            command="workflow", navigation=navigation
        ),
    )

    result = await _execute(dispatcher, "/workflow")

    assert result.navigation == navigation
    assert result.runs == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"extension": "foreign"},
        {"page": "undeclared"},
        {"page": None},
        {"route": "https://example.org"},
        {"route": "/outside"},
        {"route": "items/../outside"},
        {"route": "items\\outside"},
        {"route": "x" * 2049},
        {"route": None},
        {"agent_id": "forged"},
        {"session_id": "forged"},
        {"project_id": "forged"},
    ],
)
async def test_extension_command_rejects_invalid_page_navigation(
    overrides: dict[str, Any],
) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    values: dict[str, Any] = {
        "kind": "open_extension_page",
        "extension": "workflow_ext",
        "page": "overview",
    }
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Open the workflow.",
        page_ids=frozenset({"overview"}),
        handler=lambda _context, _argument: CommandOutcome(
            command="workflow", navigation=CommandNavigation(**(values | overrides))
        ),
    )

    result = await _execute(dispatcher, "/workflow")

    assert result.navigation is None
    assert result.feedback is not None


@pytest.mark.asyncio
async def test_extension_command_drops_navigation_after_owner_retirement() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    entered = asyncio.Event()
    released = asyncio.Event()

    async def handler(_context: ExtensionCommandContext, _argument: str | None) -> CommandOutcome:
        entered.set()
        await released.wait()
        return CommandOutcome(
            command="workflow",
            navigation=CommandNavigation(
                kind="open_extension_page", extension="workflow_ext", page="overview"
            ),
        )

    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Open the workflow.",
        handler=handler,
        page_ids=frozenset({"overview"}),
    )
    task = asyncio.create_task(_execute(dispatcher, "/workflow"))
    await entered.wait()
    dispatcher.unregister_extension_commands("workflow_ext")
    released.set()

    result = await task

    assert result.navigation is None
    assert result.feedback is not None
