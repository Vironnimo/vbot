"""Extension command that starts its bundled workflow Skill as a follow-up Run."""

from __future__ import annotations

from core.chat import (
    CommandFeedback,
    CommandOutcome,
    CommandRun,
    ExtensionCommandContext,
)


async def _start_workflow(
    context: ExtensionCommandContext,
    argument: str | None,
) -> CommandOutcome:
    prompt = "$workflow"
    if argument:
        prompt = f"{prompt}\n\nUser objective:\n{argument}"
    run = await context.start_run(prompt)
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="notice", text="Workflow started."),
        runs=(CommandRun(role="follow_up", run=run),),
    )


def register(api) -> None:
    api.register_command(
        "workflow",
        "Start the bundled workflow Skill.",
        _start_workflow,
        argument="optional",
        catalog_result="notice",
        execution_mode="serialized",
    )
