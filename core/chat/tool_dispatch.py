"""Tool lifecycle dispatch and deterministic skill activation."""

from __future__ import annotations

import contextvars
import json
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from core.chat._skill_activation import _active_skill_env_keys
from core.chat._tool_context import (
    ToolDispatchContext as ToolDispatchContext,
)
from core.chat._tool_context import (
    _agent_workspace as _agent_workspace,
)
from core.chat._tool_context import (
    _dispatch_allowed_tools as _dispatch_allowed_tools,
)
from core.chat._tool_context import (
    _resolve_tool_cwd as _resolve_tool_cwd,
)
from core.chat._tool_context import (
    _runtime_allowed_tools as _runtime_allowed_tools,
)
from core.chat._tool_registry import TOOL_REMOVED_ERROR_CODE as TOOL_REMOVED_ERROR_CODE
from core.chat._tool_registry import _EmittingToolRegistry, _safe_schema_fingerprint
from core.chat._tool_result_projection import (
    _read_media_outputs,
    _tool_display_payload,
)
from core.chat.events import _emit_tool_context_event, _timing_payload
from core.chat.messages import ChatMessage, JsonObject, ToolCall, ToolCallRejection
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT
from core.sessions.history import skill_tool_activation_name
from core.skills.policy import SkillPackageRef
from core.tools import ToolCall as ScheduledToolCall
from core.tools import (
    ToolExecutionConfig,
    ToolExecutor,
    tool_failure,
)
from core.tools.availability import (
    agent_tool_settings,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("chat")

# How long tool_result hooks of a finished call may still run after a Run cancel.


class ToolRound:
    """Run one Assistant turn's Tool Calls, each as soon as it is known.

    Chat opens a round before the Model request, so a Tool Call the stream has
    completed starts while the Model still writes the rest of its turn
    (:meth:`start`). :meth:`finish` adds the calls that arrived only with the
    end of the response, waits for every call and returns the Tool Results in
    call order. Calls keep the positions they have in the Assistant turn.

    Notes the Tools add are held until :meth:`finish` and then added to the
    Session, so they land after the Assistant turn they belong to even when a
    call finished before that turn was persisted.
    """

    def __init__(
        self,
        context: ToolDispatchContext,
        *,
        assistant_message_id: str | None,
        iteration_number: int,
    ) -> None:
        run = context.run
        session = context.session
        agent = context.agent
        self._context = context
        self._calls: list[ToolCall] = []
        self._rejections: dict[int, ToolCallRejection] = {}
        self._notes: list[str] = []
        self._skill_packages: dict[str, SkillPackageRef] = {}
        self._registry = _EmittingToolRegistry(
            context.registry,
            run,
            context.extension_registry,
            note_hook=self._notes.append,
            assistant_message_id=assistant_message_id,
            denial_resolver=context.tool_denial_resolver,
            tool_restriction=context.tool_restriction,
            removed_tool_names=context.removed_tool_names,
            rejections=self._rejections,
            result_payloads=context,
        )
        workspace = _agent_workspace(agent, context.data_root)
        config = ToolExecutionConfig(
            agent_id=run.agent_id,
            session_id=run.session_id,
            run_id=run.id,
            workspace=workspace,
            vbot_root=context.vbot_root,
            data_root=context.data_root,
            iteration_number=iteration_number,
            agent_model=agent.model,
            agent_thinking_effort=agent.thinking_effort,
            execution_owner=run.execution_owner,
            cwd=_resolve_tool_cwd(context.project_cwd, workspace),
            # The owning run's project rides onto every ToolContext so the
            # subagent tool can inherit it; None keeps the identity path.
            project_id=context.project_id,
            # The run's effective skill project (its working Project) and, in a Librarian
            # Session, the Agent whose Skills it maintains, so the skill tools
            # resolve the same pool the run's catalog advertises.
            skill_project_id=context.skill_project_id,
            working_project_id=run.working_project_id,
            skill_agent_id=getattr(agent, "skill_agent_id", None),
            run_kind=run.run_kind,
            subagent_session=context.subagent_session,
            permissions=context.permissions,
            reply_surface=context.reply_surface,
            allowed_tools=_dispatch_allowed_tools(
                agent,
                context.registry,
                context.tool_restriction,
                base_allowed_tools=context.base_allowed_tools,
                session_tool_grants=context.session_tool_grants,
            ),
            session_tool_grants=context.session_tool_grants,
            allowed_skills=getattr(agent, "allowed_skills", ["*"]),
            skill_env_keys=_active_skill_env_keys(session, context.skill_registry),
            tool_settings=agent_tool_settings(getattr(agent, "tools", {})),
            delegation_bindings=dict(getattr(agent, "delegation_bindings", {})),
            emit_hook=lambda event_type, payload: _emit_tool_context_event(
                run,
                event_type,
                payload,
            ),
            cancellation_hook=lambda: run.cancel_requested,
            tool_call_cancel_registrar=lambda tool_call_id, callback: run.register_tool_cancel(
                tool_call_id, callback
            ),
            tool_call_cancel_check=lambda tool_call_id: run.tool_call_cancelled(tool_call_id),
            note_hook=self._notes.append,
            skill_activation_hook=self._activate_skill,
            tool_call_result_persisted_registrar=context.register_result_persisted,
            tool_delivery_receipt_registrar=(
                context.register_delivery_receipt if context.allow_owned_effects else None
            ),
            tool_turn_end_registrar=context.request_turn_end,
            tool_result_payload_registrar=context.stage_result_payload,
            input_contracts=context.tool_contracts,
            loadable_tools=context.loadable_tools,
            unloadable_tools=context.unloadable_tools,
            tool_load_registrar=context.record_tool_load,
            worked_paths_registrar=context.record_worked_paths,
            change_tracker=context.change_tracker,
        )
        # Calls run in the context the round was opened in, never in the
        # Model request's scope (its retry ownership belongs to that request).
        self._batch = ToolExecutor(self._registry).start_batch(
            config, context=contextvars.copy_context()
        )

    @property
    def calls(self) -> list[ToolCall]:
        """The calls started so far, in Assistant-turn order."""
        return list(self._calls)

    def start(self, tool_calls: Sequence[ToolCall]) -> None:
        """Start calls at the next positions of the Assistant turn."""
        self._context.run.raise_if_cancelled()
        for tool_call in tool_calls:
            index = len(self._calls)
            if tool_call.rejection is not None:
                self._rejections[index] = tool_call.rejection
            self._calls.append(tool_call)
            self._batch.add(
                ScheduledToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )
            )

    async def finish(
        self, remaining: Sequence[ToolCall] = ()
    ) -> tuple[list[ChatMessage], list[JsonObject]]:
        """Start ``remaining``, wait for every call and return ordered Tool Results."""
        self.start(remaining)
        results = await self._batch.results()
        return self._tool_results(list(zip(self._calls, results, strict=True)))

    async def settle_cancelled(self) -> tuple[list[ChatMessage], list[JsonObject]]:
        """Stop the unfinished calls and return the Results of those that finished.

        After a Run cancel, a call that already finished keeps its real Result;
        the calls that did not finish get none and are repaired as missing
        Results in the next request.
        """
        results = await self._batch.settle()
        return self._tool_results(
            [
                (tool_call, result)
                for tool_call, result in zip(self._calls, results, strict=True)
                if result is not None
            ]
        )

    def _activate_skill(
        self, tool_call_id: str, name: str, content: str, package: SkillPackageRef | None
    ) -> bool:
        activated = self._context.session.register_skill_activation(name, content, package)
        if activated and package is not None:
            self._skill_packages[tool_call_id] = package
        return activated

    def _tool_results(
        self, completed: list[tuple[ToolCall, JsonObject]]
    ) -> tuple[list[ChatMessage], list[JsonObject]]:
        session = self._context.session
        for note in self._notes:
            session.add_note(note)
        self._notes.clear()
        tool_messages: list[ChatMessage] = []
        media_outputs: list[JsonObject] = []
        registry = self._registry
        for tool_call, result in completed:
            tool_message = ChatMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                timing=registry.timing_for_call(tool_call.id),
                tool_display=registry.display_for_completed_call(tool_call.id),
            )
            package = self._skill_packages.pop(tool_call.id, None)
            if package is not None and skill_tool_activation_name(tool_message) is not None:
                tool_message = replace(tool_message, skill_package=package)
            tool_messages.append(tool_message)
            media_outputs.extend(
                _read_media_outputs(
                    result, tool_call_id=tool_call.id, tool_message_id=tool_message.id
                )
            )
            media_outputs.extend(
                registry.take_media_for_call(tool_call.id, tool_message_id=tool_message.id)
            )
        return tool_messages, media_outputs

    async def aclose(self) -> None:
        """Cancel every call that has not finished and wait until all settled."""
        await self._batch.aclose()


def _fail_tool_calls_without_dispatch(
    context: ToolDispatchContext,
    tool_calls: list[ToolCall],
    *,
    code: str,
    message: str,
    retryable: bool | None = None,
    start_index: int = 0,
) -> list[ChatMessage]:
    """Produce normal correlated failure Results without invoking any handler.

    Chat uses this when the Provider's terminal outcome proves the emitted Tool
    Calls are unsafe to execute. Lifecycle events and canonical envelopes stay
    identical in shape to ordinary dispatch; extensions and handlers are
    deliberately bypassed because no untrusted call may cross the execution
    boundary.
    """

    tool_messages: list[ChatMessage] = []
    for index, tool_call in enumerate(tool_calls, start=start_index):
        context.run.tool_call_count += 1
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        contract = context.tool_contracts.get(tool_call.name)
        fingerprint = (
            contract.schema_fingerprint
            if contract is not None
            else _safe_schema_fingerprint(context.registry, tool_call.name)
        )
        display = _tool_display_payload(
            context.registry,
            tool_call.name,
            tool_call.arguments,
        )
        context.run.emit(
            TOOL_CALL_STARTED_EVENT,
            {
                "assistant_message_id": context.session.assistant_message_id,
                "tool_call": {
                    "id": tool_call.id,
                    "index": index,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                },
                "display": display,
                "schema_fingerprint": fingerprint,
            },
        )
        result = tool_failure(code, message, retryable=retryable)
        timing = _timing_payload(started_at, started_perf)
        context.run.emit(
            TOOL_CALL_RESULT_EVENT,
            {
                "assistant_message_id": context.session.assistant_message_id,
                "tool_call": {
                    "id": tool_call.id,
                    "index": index,
                    "name": tool_call.name,
                },
                "result": result,
                "display": display,
                "timing": timing,
                "schema_fingerprint": fingerprint,
                "error_code": code,
            },
        )
        tool_messages.append(
            ChatMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                timing=timing,
                tool_display=display,
            )
        )
    return tool_messages
