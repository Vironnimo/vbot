"""Schedule script Tool calls and commit their activity outside Model Context."""

from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, override

from core.chat._skill_activation import _active_skill_env_keys
from core.chat._step_outcomes import tool_result_facts
from core.chat._tool_context import ToolDispatchContext
from core.chat._tool_registry import _EmittingToolRegistry, _leaving_call
from core.chat._tool_result_projection import _read_media_outputs
from core.chat.messages import ChatMessage, JsonObject, ToolCall
from core.sessions.history import _skill_context_note_content
from core.skills.policy import SkillPackageRef
from core.tools import ToolCall as ScheduledToolCall
from core.tools import (
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolNotFoundError,
    called_tool_name,
    tool_failure,
)
from core.utils.ids import new_id
from core.utils.workers import settle_before_cancelling


class ProgrammaticTools(_EmittingToolRegistry):
    """One parent call's scheduler, using the ordinary dispatch and persistence owners.

    Each call declares its own durable activity before starting. Completed calls
    settle their Result before answering Python, independently of the parent
    program's success or cancellation. This batch has its own barriers: putting
    children behind their waiting parent would deadlock a serial parent Tool.
    """

    def __init__(
        self, context: ToolDispatchContext, config: ToolExecutionConfig, parent: ToolContext
    ) -> None:
        self._context = context
        self._parent = parent
        self._activate_skill = config.skill_activation_hook
        self._skill_notes: dict[str, ChatMessage] = {}
        self._declarations: dict[str, ChatMessage] = {}
        self._message_ids: dict[str, str] = {}
        super().__init__(
            context.registry,
            context.run,
            context.extension_registry,
            note_hook=config.note_hook,
            denial_resolver=context.tool_denial_resolver,
            tool_restriction=context.tool_restriction,
            result_payloads=context,
            removed_tool_names=context.removed_tool_names,
            parent_tool_call_id=parent.tool_call_id,
            assistant_message_ids=self._message_ids,
        )
        self._config = replace(
            config,
            programmatic=True,
            # The process owns the invocation tasks and cancels them when it
            # closes; this check also stops calls waiting behind a barrier.
            cancellation_hook=lambda: (
                context.run.cancel_requested or context.run.tool_call_cancelled(parent.tool_call_id)
            ),
            skill_activation_hook=self._activate_programmatic_skill,
            # Loading a definition for Python does not establish that the Model
            # consumed it. The existing catalog remains available to the Model.
            tool_load_registrar=None,
        )
        self._batch = ToolExecutor(self).start_batch(
            self._config, context=contextvars.copy_context()
        )

    async def call(
        self, name: str, arguments: JsonObject, positional: Sequence[Any] = ()
    ) -> JsonObject:
        context = self._context
        context.run.raise_if_cancelled()
        name = called_tool_name(
            name,
            self._config.allowed_tools or (),
            registered={
                *context.removed_tool_names,
                *(tool.name for tool in context.registry.list_tools(include_internal=True)),
            },
        )
        if positional:
            try:
                tool = context.registry.get(name)
            except ToolNotFoundError:
                pass  # Dispatch reports the unknown or removed name.
            else:
                parameters = list(tool.contract.input_schema.get("properties") or {})
                try:
                    arguments = _named_arguments(name, parameters, arguments, positional)
                except ValueError as error:
                    return tool_failure("invalid_arguments", str(error))
        call_id = new_id("pcall")
        call = ToolCall(id=call_id, name=name, arguments=arguments)
        # Persist only masked text, as ordinary dispatch does for its events/results.
        safe_arguments = _leaving_call(self._secret_masker(), arguments)
        declaration = replace(
            ChatMessage.assistant(
                model=context.agent.model,
                content=None,
                tool_calls=[replace(call, arguments=safe_arguments)],
            ),
            programmatic=True,
            parent_tool_call_id=self._parent.tool_call_id,
        )
        self._declarations[call_id] = declaration
        self._message_ids[call_id] = declaration.id
        index = len(self._batch)
        self._batch.add(ScheduledToolCall(id=call_id, name=name, arguments=arguments))
        return await self._batch.result(index)

    def _activate_programmatic_skill(
        self, call_id: str, name: str, content: str, package: SkillPackageRef | None
    ) -> bool:
        activated = self._activate_skill is not None and self._activate_skill(
            call_id, name, content, package
        )
        if activated:
            self._skill_notes[call_id] = replace(
                ChatMessage.note(
                    _leaving_call(self._secret_masker(), _skill_context_note_content(name, content))
                ),
                skill_package=package,
            )
        return activated

    @override
    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        if context.is_cancelled():
            raise asyncio.CancelledError
        declaration = self._declarations.pop(context.tool_call_id)
        await settle_before_cancelling(
            self._context.persist_activity(declaration.id, [declaration], {})
        )
        if context.is_cancelled():
            raise asyncio.CancelledError
        scope = self._context.current_skill_context
        project_id, registry = (
            scope()
            if scope is not None
            else (self._context.skill_project_id, self._context.skill_registry)
        )
        context = replace(
            context,
            skill_project_id=project_id,
            skill_env_keys=_active_skill_env_keys(self._context.session, registry),
        )
        result = await super().dispatch(context, arguments, allowed_tools)
        message = replace(
            ChatMessage.tool(
                tool_call_id=context.tool_call_id,
                name=context.tool_name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                timing=self.timing_for_call(context.tool_call_id),
                tool_display=self.display_for_completed_call(context.tool_call_id),
            ),
            programmatic=True,
            parent_tool_call_id=self._parent.tool_call_id,
        )
        media = [
            *_read_media_outputs(
                result, tool_call_id=context.tool_call_id, tool_message_id=message.id
            ),
            *self.take_media_for_call(context.tool_call_id, tool_message_id=message.id),
        ]
        facts = self._context.with_result_payloads(tool_result_facts([message]))
        messages = [message]
        if note := self._skill_notes.pop(context.tool_call_id, None):
            messages.append(note)
        await settle_before_cancelling(
            self._context.persist_activity(declaration.id, messages, facts, media)
        )
        return result

    async def aclose(self) -> None:
        await self._batch.aclose()


def _named_arguments(
    tool: str, parameters: Sequence[str], arguments: JsonObject, values: Sequence[Any]
) -> JsonObject:
    """Give *values* the parameters they fill in a Python call: schema order."""
    if len(values) > len(parameters):
        accepted = (
            f"its parameters are {', '.join(parameters)}" if parameters else "it has no parameters"
        )
        raise ValueError(
            f"Nothing was sent: {tool} received {len(values)} positional values, but {accepted}. "
            f"Pass each value by name, such as tools.{tool}(<parameter>=<value>)."
        )
    named = dict(arguments)
    for parameter, value in zip(parameters, values, strict=False):
        if parameter in named and named[parameter] != value:
            raise ValueError(
                f"Nothing was sent: {tool} received {parameter} twice with different values, "
                "by position and by name. Pass it once."
            )
        named[parameter] = value
    return named
