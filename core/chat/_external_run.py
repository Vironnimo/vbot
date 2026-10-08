"""Runs whose turns a Model outside the Agentic Loop produces.

A realtime voice Model talks with the user on its own; vBot does not request
its turns. Its conversation is still one Run of its Agent in a Session of its
own: what the user and the Model said, notes about what vBot told the Model, and
every Tool call the Model made. Those calls run through the same Tool dispatch
as a Chat Run, with the Agent's Tools, events and stored results.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat._step_outcomes import _RegisteredNames, tool_result_facts
from core.chat.errors import ChatError
from core.chat.events import _emit_message_event
from core.chat.messages import (
    ERROR_KIND_PROVIDER_ERROR,
    INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
    ChatMessage,
    JsonObject,
    ToolCall,
    _new_message_id,
)
from core.chat.tool_dispatch import ToolDispatchContext, ToolRound
from core.providers.adapter import normalize_tool_call_candidate
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    USER_MESSAGE_EVENT,
    Run,
    RunAdmission,
    RunKind,
)
from core.sessions import AGENT_DEFAULT_PROJECT, ChatSession, SessionAddress
from core.tools import called_tool_name, tool_failure
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.workers import finish_despite_cancel

if TYPE_CHECKING:
    from core.chat._request_builder import RequestBuilder
    from core.chat._run_state import ChatLoopDependencies

_LOGGER = get_logger("chat")

# How long the end of a Run waits for Tool calls still running to store results.
_SETTLE_SECONDS = 5.0
# Agent-facing: the result of a Tool call stopped before it finished, because it
# took too long or the conversation ended.
_STOPPED = (
    "This Tool call was stopped before it finished; it may or may not have completed. "
    "Nothing was retried."
)


class ExternalRunFailedError(ChatError):
    """The external conversation ended because it failed; the Session says why."""


class ExternalRun:
    """The Run and Session of one externally driven conversation.

    Start it with :meth:`core.chat.ChatLoop.start_external_run`. Record what
    was said with :meth:`record_user` and :meth:`record_assistant`, and what
    vBot told the Model with :meth:`record_note`; run the Model's Tool calls
    with :meth:`run_tool`. :meth:`finish` ends the Run. When the user cancels
    the Run, ``on_cancel`` asks the owner to end the conversation, and calls
    that run meanwhile store a failure result.
    """

    def __init__(
        self,
        *,
        dependencies: ChatLoopDependencies,
        agent: Any,
        session: ChatSession,
        model: str,
        tool_definitions: Sequence[JsonObject],
        allowed_tools: Sequence[str],
        on_cancel: Callable[[], None] | None,
    ) -> None:
        self._dependencies = dependencies
        self._agent = agent
        self._session = session
        self._model = model
        self._tool_definitions = tuple(dict(definition) for definition in tool_definitions)
        self._allowed_tools = tuple(allowed_tools)
        self._on_cancel = on_cancel
        self._run: Run | None = None
        self._write_lock = asyncio.Lock()
        self._iteration = 0
        self._running_calls = 0
        # Calls whose turn is not stored yet, by the id of their Assistant turn.
        self._unstored_calls: dict[str, ToolCall] = {}
        self._calls_settled = asyncio.Event()
        self._calls_settled.set()
        self._done = asyncio.Event()
        self._failure: str | None = None

    @property
    def agent_id(self) -> str:
        return self._session.address.agent_id

    @property
    def session_id(self) -> str:
        return self._session.id

    @property
    def run(self) -> Run:
        """The Run; wait on it to learn how it ended."""
        return self._require_run()

    @property
    def tool_definitions(self) -> tuple[JsonObject, ...]:
        """The Tools the Model can call: ``name``, ``description``, ``parameters`` each."""
        return tuple(dict(definition) for definition in self._tool_definitions)

    @property
    def ended(self) -> bool:
        """Whether the Run ended; recording and Tool calls then do nothing."""
        return self._done.is_set() or self._run is None or self._run.cancel_requested

    async def record_user(self, text: str) -> None:
        """Store what the user said."""
        if text.strip():
            message = ChatMessage.user(text, input_origin=INPUT_ORIGIN_SPEECH_TRANSCRIPTION)
            if await self._append([message]):
                _emit_message_event(self._require_run(), USER_MESSAGE_EVENT, message)

    async def record_assistant(self, text: str) -> None:
        """Store what the Model said."""
        if text.strip():
            message = ChatMessage.assistant(model=self._model, content=text)
            if await self._append([message]):
                _emit_message_event(self._require_run(), ASSISTANT_OUTPUT_EVENT, message)

    async def record_note(self, text: str) -> None:
        """Store something vBot told the Model, such as an update it should speak about."""
        if text.strip():
            await self._append([ChatMessage.note(text)])

    async def run_tool(self, call_id: str, name: str, arguments: Any) -> JsonObject:
        """Run one Tool call of the Model and store it; return its Tool result envelope.

        *name* resolves like a Chat Run's call: a namespace prefix such as
        ``functions.`` or another harness's name for an offered Tool runs that
        Tool. *arguments* are as the Model sent them: an object or its JSON text.
        The call and its result are stored together once it finished, after
        what was said while it ran. Calls can run concurrently; cancelling one
        stores a result saying it was stopped. A cancel that arrives once the
        call finished, while it is being stored, no longer stops it: the call is
        stored with its result, which is returned. A call still running when the
        Run ends is stored as stopped then.
        """
        run = self._require_run()
        if self.ended:
            return tool_failure("tool_stopped", _STOPPED)
        call = ToolCall.from_dict(
            normalize_tool_call_candidate(
                tool_call_id=call_id,
                name=called_tool_name(
                    name,
                    self._allowed_tools,
                    registered=_RegisteredNames(self._dependencies.tools),
                ),
                arguments=arguments,
                fallback_id=new_id("call"),
            )
        )
        self._iteration += 1
        assistant_id = _new_message_id()
        tool_round = ToolRound(
            self._dispatch_context(run),
            assistant_message_id=assistant_id,
            iteration_number=self._iteration,
        )
        self._running_calls += 1
        self._unstored_calls[assistant_id] = call
        self._calls_settled.clear()
        try:
            try:
                tool_round.start([call])
                tool_messages, _media = await tool_round.finish()
            except asyncio.CancelledError:
                await tool_round.aclose()
                ended = _ended_result(call)
                await asyncio.shield(self._store_call(assistant_id, [ended]))
                raise
            await finish_despite_cancel(self._store_call(assistant_id, tool_messages))
        finally:
            self._running_calls -= 1
            if self._running_calls == 0:
                self._calls_settled.set()
        return _result_envelope(tool_messages[0].content if tool_messages else None)

    async def finish(self, *, failure: str | None = None) -> None:
        """End the Run once running Tool calls stored their results.

        A *failure* (what went wrong, for the user) is stored in the Session
        and fails the Run.
        """
        if self._done.is_set():
            return
        await self._settle_calls()
        if failure and self._run is not None and not self._run.cancel_requested:
            self._failure = failure
            await self._append([ChatMessage.error(ERROR_KIND_PROVIDER_ERROR, failure)])
        self._done.set()

    async def discard(self) -> None:
        """End the Run of a conversation that never started and remove its Session."""
        await self.finish()
        if self._run is not None:
            with contextlib.suppress(Exception):
                await self._run.wait()
        await _discard(
            self._dependencies,
            SessionAddress(project_id=None, agent_id=self.agent_id, session_id=self.session_id),
        )

    async def execute(self, run: Run) -> None:
        """The Run's executor: it lasts until :meth:`finish` or a cancel."""
        try:
            await self._done.wait()
        except asyncio.CancelledError:
            if self._on_cancel is not None:
                try:
                    self._on_cancel()
                except Exception:
                    _LOGGER.warning(
                        "Ending an external conversation failed (run=%s)", run.id, exc_info=True
                    )
            await self._settle_calls()
            self._done.set()
            raise
        if self._failure is not None:
            raise ExternalRunFailedError(self._failure)

    def bind(self, run: Run) -> None:
        self._run = run
        self._session = self._session.for_run(run.id)

    def _require_run(self) -> Run:
        if self._run is None:
            raise ChatError("the external Run has not started")
        return self._run

    async def _settle_calls(self) -> None:
        """Wait for running calls to store their results; store the late ones as stopped."""
        try:
            async with asyncio.timeout(_SETTLE_SECONDS):
                await self._calls_settled.wait()
        except TimeoutError:
            _LOGGER.warning(
                "External Run ended before its Tool calls finished (run=%s calls=%d)",
                self.run.id,
                self._running_calls,
            )
            for assistant_id, call in list(self._unstored_calls.items()):
                await self._store_call(assistant_id, [_ended_result(call)])

    async def _store_call(self, assistant_id: str, tool_messages: list[ChatMessage]) -> None:
        """Store a call's turn with *tool_messages*, unless that turn is already stored."""
        call = self._unstored_calls.pop(assistant_id, None)
        if call is None:
            return
        turn = ChatMessage.assistant(
            model=self._model, content=None, tool_calls=[call], message_id=assistant_id
        )
        await self._append(
            [turn, *tool_messages],
            tool_results=tool_result_facts(tool_messages),
            with_notes=True,
        )

    async def _append(
        self,
        messages: list[ChatMessage],
        *,
        tool_results: Any | None = None,
        with_notes: bool = False,
    ) -> bool:
        """Store messages in order; ``False`` once the Run ended.

        *with_notes* adds the notes Tool calls left, after *messages*.
        """
        run = self._run
        if run is None or self._done.is_set():
            return False
        async with self._write_lock:
            if with_notes:
                messages = [*messages, *self._session.take_deferred_notes()]
                self._session.begin_defer_notes()
            try:
                await self._session.append_many_async(messages, tool_results=tool_results)
            except Exception:
                _LOGGER.warning(
                    "External Run could not store messages (run=%s)", run.id, exc_info=True
                )
                return False
        return True

    def _dispatch_context(self, run: Run) -> ToolDispatchContext:
        dependencies = self._dependencies
        return ToolDispatchContext(
            registry=dependencies.tools,
            extension_registry=dependencies.get_extension_registry(),
            agent=self._agent,
            session=self._session,
            run=run,
            vbot_root=Path(dependencies.get_system_prompts().vbot_root),
            data_root=Path(dependencies.storage.data_dir),
            base_allowed_tools=self._allowed_tools,
            change_tracker=dependencies.change_tracker,
        )


async def start_external_run(
    dependencies: ChatLoopDependencies,
    requests: RequestBuilder,
    agent_id: str,
    *,
    model: str,
    title: str,
    run_kind: RunKind,
    extra_tools: Sequence[str],
    on_cancel: Callable[[], None] | None,
) -> ExternalRun:
    """Create the Session and start the Run of an external conversation at *agent_id*."""
    resolver = dependencies.agent_resolver
    agent = await resolver.resolve_agent_async(None, agent_id)
    working_project_id = await resolver.resolve_working_project_async(
        None, agent, requested=AGENT_DEFAULT_PROJECT
    )
    definitions = await requests.preview_tool_definitions(agent)
    offered = {str(definition.get("name")) for definition in definitions}
    extra = [name for name in dict.fromkeys(extra_tools) if name not in offered]
    if extra:
        definitions = [
            *definitions,
            *(
                definition
                for definition in dependencies.tools.provider_definitions(extra)
                if definition.get("name") in extra
            ),
        ]
    allowed = [str(definition.get("name")) for definition in definitions]
    session = await dependencies.sessions.create_async(
        agent_id,
        run_kind=run_kind,
        metadata={"auto_title": title, "auto_title_initialized": True},
        working_project_id=working_project_id,
    )
    session.begin_defer_notes()
    handle = ExternalRun(
        dependencies=dependencies,
        agent=agent,
        session=session,
        model=model,
        tool_definitions=_neutral_definitions(definitions),
        allowed_tools=allowed,
        on_cancel=on_cancel,
    )
    address = SessionAddress(project_id=None, agent_id=agent_id, session_id=session.id)
    try:
        run = await dependencies.run_manager.start(
            address,
            handle.execute,
            admission=RunAdmission(
                working_project_id=working_project_id,
                run_kind=run_kind,
                contributes_to_agent_activity=False,
                expected_session_generation_id=session.generation_id,
            ),
        )
        handle.bind(run)
        await run.wait_admitted()
    except BaseException:
        await _discard(dependencies, address)
        raise
    return handle


async def _discard(dependencies: ChatLoopDependencies, address: SessionAddress) -> None:
    sessions = dependencies.sessions
    try:
        await sessions.run_async(sessions.delete, address)
    except Exception:
        _LOGGER.warning(
            "Could not remove the unused Session (agent=%s session=%s)",
            address.agent_id,
            address.session_id,
            exc_info=True,
        )


def _neutral_definitions(definitions: Sequence[JsonObject]) -> list[JsonObject]:
    return [
        {
            "name": definition.get("name"),
            "description": definition.get("description", ""),
            "parameters": definition.get("parameters") or {"type": "object", "properties": {}},
        }
        for definition in definitions
    ]


def _ended_result(call: ToolCall) -> ChatMessage:
    return ChatMessage.tool(
        tool_call_id=call.id,
        name=call.name,
        content=json.dumps(
            tool_failure("tool_stopped", _STOPPED),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _result_envelope(content: object) -> JsonObject:
    try:
        result = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        result = None
    if isinstance(result, dict):
        return result
    return tool_failure("tool_failed", "The Tool returned no result.")
