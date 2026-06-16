"""Chat message primitives and chat loop execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat.content_blocks import ContentBlock, MediaBlock
from core.chat.errors import ChatError, ChatSessionError, ToolIterationLimitError
from core.chat.events import (
    _close_adapter,
    _emit_assistant_events,
    _emit_message_event,
    _emit_streaming_assistant_events,
    _is_model_fallback_trigger,
    _is_stream_restartable_error,
    _is_streaming_fallback_error,
    _maybe_persist_partial_thinking,
    _persist_run_error,
    _timing_payload,
)
from core.chat.events import (
    _exception_to_error_kind as _exception_to_error_kind,
)
from core.chat.messages import (
    COMPACTION_TAIL_RECOVERED_HINT,
    SYSTEM_REMINDER_CLOSE_TAG,
    SYSTEM_REMINDER_OPEN_TAG,
    ChatMessage,
    JsonObject,
    _append_input_origin_note,
    _apply_usage_estimation,
    _assistant_continuation_dict,
    _assistant_message_from_response,
    _display_content_preview,
    _embed_notes_into_request,
    _last_user_message,
    _last_user_message_with_content_blocks,
    _latest_compaction_checkpoint,
    _message_to_request_dict,
    _notes_to_synthetic_user_message,
    _resolve_preserved_tail,
    _restore_in_run_assistant_reasoning,
    _session_has_any_content_blocks,
    _strip_assistant_reasoning_fields,
)
from core.chat.messages import (
    ERROR_KIND_AUTH as ERROR_KIND_AUTH,
)
from core.chat.messages import (
    ERROR_KIND_CONFIG as ERROR_KIND_CONFIG,
)
from core.chat.messages import (
    ERROR_KIND_NETWORK as ERROR_KIND_NETWORK,
)
from core.chat.messages import (
    ERROR_KIND_PROVIDER_ERROR as ERROR_KIND_PROVIDER_ERROR,
)
from core.chat.messages import (
    ERROR_KIND_PROVIDER_FATAL as ERROR_KIND_PROVIDER_FATAL,
)
from core.chat.messages import (
    ERROR_KIND_PROVIDER_OVERLOAD as ERROR_KIND_PROVIDER_OVERLOAD,
)
from core.chat.messages import (
    ERROR_KIND_RATE_LIMIT as ERROR_KIND_RATE_LIMIT,
)
from core.chat.messages import (
    ERROR_KIND_TIMEOUT as ERROR_KIND_TIMEOUT,
)
from core.chat.messages import (
    ERROR_KIND_TOOL_ITERATIONS as ERROR_KIND_TOOL_ITERATIONS,
)
from core.chat.messages import (
    INPUT_ORIGIN_SPEECH_TRANSCRIPTION as INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
)
from core.chat.messages import (
    InputOrigin as InputOrigin,
)
from core.chat.messages import (
    MessageSender as MessageSender,
)
from core.chat.messages import (
    ToolCall as ToolCall,
)
from core.chat.messages import (
    _validate_assistant_message as _validate_assistant_message,
)
from core.chat.messages import (
    error_kind_llm_visible as error_kind_llm_visible,
)
from core.chat.model_resolution import (
    _ensure_provider_exists,
    _first_usable_connection_id,
    _model_input_modalities,
    _resolve_agent_connection,
    _resolve_fallback,
    _split_agent_model,
)
from core.chat.model_resolution import (
    parse_bare_model as parse_bare_model,
)
from core.chat.model_resolution import (
    parse_model_with_connection as parse_model_with_connection,
)
from core.chat.streaming import (
    STREAM_CHUNK_TIMEOUT_SECONDS,
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    iter_with_chunk_timeout,
)
from core.chat.tool_dispatch import (
    _activate_triggered_skills,
    _dispatch_tool_calls,
    _sync_skill_context_messages,
)
from core.debug import DebugContext
from core.extensions import HookContext
from core.providers.errors import NetworkError
from core.providers.providers import resolve_context_window
from core.providers.reasoning import REASONING_REPLAY_CURRENT_RUN, ReasoningReplayPolicy
from core.runs import (
    COMPACTION_COMPLETED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    USER_MESSAGE_EVENT,
    QueuedRunItem,
    Run,
    RunExecutor,
)
from core.sessions import ChatSession
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.block_resolver import ContentBlockResolver
    from core.compaction import CompactionService, CompactionSettings
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("chat")

MAX_TOOL_ITERATIONS = 1000

# How often a streaming attempt may be restarted from scratch after a transient
# drop that occurred before any visible output. Each restart re-issues the whole
# request (the adapter's own connect-level retry still applies per attempt), so
# this bounds only the post-connect mid-stream replays.
MAX_STREAM_RESTARTS = 2


class _StreamRestartNeeded(Exception):  # noqa: N818 — control-flow signal, not an error
    """Internal signal: a streaming attempt dropped before any visible output.

    Raised by ``_consume_stream_attempt`` and caught by
    ``_send_streaming_assistant_request`` to replay the stream. It never escapes
    the chat loop — the final attempt cannot restart and re-raises the real
    error instead.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _resolve_reasoning_replay_policy(adapter: Any, model_id: str) -> ReasoningReplayPolicy:
    """Resolve the adapter's reasoning replay policy for one request build.

    Mirrors the ``set_debug_context`` probe: adapters and test doubles that do
    not expose the hook get the historical ``current_run`` shaping.
    """
    if hasattr(adapter, "reasoning_replay_policy"):
        return cast(ReasoningReplayPolicy, adapter.reasoning_replay_policy(model_id))
    return REASONING_REPLAY_CURRENT_RUN


def _read_media_text_note(filename: str, media_type: str) -> JsonObject:
    """Plain-text fallback when a read-media image cannot be shown to the model."""
    return {
        "role": "user",
        "content": (
            f"[Loaded media {filename} ({media_type}) from disk, but it cannot be "
            "shown to this model directly.]"
        ),
    }


class ChatLoop:
    """Minimal agentic chat loop."""

    def __init__(
        self,
        runtime: RuntimeServices,
        *,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
        streaming: bool = False,
        attachment_resolver: ContentBlockResolver | None = None,
        compaction_service: CompactionService | None = None,
    ) -> None:
        if max_tool_iterations < 0:
            raise ChatError("max tool iterations must not be negative")
        self._runtime = runtime
        self._max_tool_iterations = max_tool_iterations
        self._streaming = streaming
        self._attachment_resolver = attachment_resolver
        self._compaction_service = compaction_service
        self._nesting_depth = 0

    def child_loop(self, *, nesting_depth: int) -> ChatLoop:
        """Create a sub-agent child loop sharing this loop's wiring.

        The child reuses the attachment resolver and compaction service so
        child runs behave like normal live runs; only the nesting depth
        differs.
        """
        child = ChatLoop(
            self._runtime,
            max_tool_iterations=self._max_tool_iterations,
            streaming=self._streaming,
            attachment_resolver=self._attachment_resolver,
            compaction_service=self._compaction_service,
        )
        child._nesting_depth = nesting_depth
        return child

    def run_executor(self, content: str | list[ContentBlock]) -> RunExecutor:
        """Return a run-manager executor that runs *content* through this loop."""
        return lambda run: self._execute_run(run, content)

    async def send(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str | None = None,
        input_origin: InputOrigin | None = None,
    ) -> ChatMessage:
        """Run one persisted non-streaming chat turn and return the final assistant message."""
        run = await self._start_run(
            agent_id,
            content,
            session_id=session_id,
            create_missing=True,
            input_origin=input_origin,
        )
        return cast(ChatMessage, await run.wait())

    async def start_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
    ) -> Run:
        """Start one chat run against an existing session for server-facing callers."""
        return await self._start_run(
            agent_id,
            content,
            session_id=session_id,
            create_missing=False,
            internal=internal,
            input_origin=input_origin,
            sender=sender,
        )

    async def queue_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
    ) -> QueuedRunItem:
        """Queue one chat run for a busy session or start it immediately when idle."""
        agent = self._runtime.agents.get(agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        session = self._get_session(agent_id, session_id, create_missing=False)
        manager = self._runtime.chat_run_manager
        return await manager.enqueue(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(
                run,
                content,
                internal=internal,
                input_origin=input_origin,
                sender=sender,
            ),
            display_content=_display_content_preview(content),
            internal=internal,
        )

    def build_queue_update(
        self,
        agent_id: str,
        session_id: str,
        content: str | list[ContentBlock],
        input_origin: InputOrigin | None = None,
    ) -> tuple[str, RunExecutor, str]:
        """Build replacement data for a queued run without mutating queue state."""
        agent = self._runtime.agents.get(agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        session = self._get_session(agent_id, session_id, create_missing=False)
        return (
            session.id,
            lambda run: self._execute_run(run, content, input_origin=input_origin),
            _display_content_preview(content),
        )

    async def retry_run(self, agent_id: str, session_id: str) -> Run:
        """Retry the last user turn without adding a new user message.

        Only valid when the session already contains at least one user message.
        """
        session = self._get_session(agent_id, session_id, create_missing=False)
        messages = session.load()
        if not any(message.role == "user" for message in messages):
            raise ChatSessionError("no user message in session to retry")
        manager = self._runtime.chat_run_manager
        return await manager.start(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(run, content=None, retry=True),
        )

    async def compact_session(
        self, agent_id: str, session_id: str, instruction: str | None = None
    ) -> str:
        """Manually compact a session and return a user-facing command reply.

        Refuses while a run is active for the session. On success one
        compaction checkpoint is appended to the session; failures inside
        the compaction itself are converted into a reply string instead of
        raising, matching the `/compact` command contract. ``instruction`` is the
        optional free-text argument from `/compact <instruction>` and is woven
        into the summarization prompt.
        """
        if self._compaction_service is None:
            return "Compaction is not available."

        manager = self._runtime.chat_run_manager
        if manager.active_run(agent_id=agent_id, session_id=session_id) is not None:
            return "Cannot compact while a run is active for this session."

        agent = self._runtime.agents.get(agent_id)
        session = self._get_session(agent_id, session_id, create_missing=False)
        messages = session.load()
        settings = self._load_compaction_settings()

        adapter: Any | None = None
        summary_adapter: Any | None = None
        try:
            provider_id, connection_id = _resolve_agent_connection(self._runtime, agent)
            adapter = self._runtime.get_adapter(provider_id, connection_id)
            _model_provider_id, model_id = _split_agent_model(agent.model)
            summary_adapter, summary_model_id = self._resolve_summary_adapter(
                agent,
                adapter,
                model_id,
                settings,
            )
            checkpoint = await self._compaction_service.compact(
                messages,
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=self._runtime.storage,
                settings=settings,
                instruction=instruction,
            )
            session.append(checkpoint)
        except Exception as exc:
            return f"Compaction failed: {exc}"
        finally:
            if adapter is not None:
                await _close_adapter(adapter)
            if summary_adapter is not None and summary_adapter is not adapter:
                await _close_adapter(summary_adapter)

        return "Context compacted."

    async def _start_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock] | None = None,
        *,
        session_id: str | None,
        create_missing: bool,
        internal: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
    ) -> Run:
        agent = self._runtime.agents.get(agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        session = self._get_session(agent_id, session_id, create_missing=create_missing)
        manager = self._runtime.chat_run_manager
        return await manager.start(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(
                run,
                content,
                internal=internal,
                input_origin=input_origin,
                sender=sender,
            ),
        )

    async def _execute_run(
        self,
        run: Run,
        content: str | list[ContentBlock] | None = None,
        *,
        internal: bool = False,
        retry: bool = False,
        input_origin: InputOrigin | None = None,
        sender: MessageSender | None = None,
    ) -> ChatMessage:
        agent = self._runtime.agents.get(run.agent_id)
        _model_provider_id, model_id = _split_agent_model(agent.model)
        provider_id, connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        adapter = self._runtime.get_adapter(provider_id, connection_id)
        run.add_cancel_callback(lambda: _close_adapter(adapter))
        process_manager = self._runtime.process_manager
        run.add_cancel_callback(lambda: process_manager.cancel_scope(run.id))
        session = self._runtime.chat_sessions.get(
            run.agent_id,
            run.session_id,
        )
        run_timing_started_at = datetime.now(UTC)
        run_timing_started_perf = time.perf_counter()
        _run_succeeded = True

        try:
            extension_registry = self._runtime.extensions
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                await extension_registry.dispatch_run_start(
                    extension_ctx,
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                )

            run.raise_if_cancelled()
            if retry:
                pass
            elif internal:
                if not isinstance(content, str):
                    raise ChatError("internal runs require string content")
                session.add_note(content)
            else:
                if content is None:
                    raise ChatError("content is required for non-retry runs")
                _append_input_origin_note(session, input_origin)
                user_message = ChatMessage.user(content, sender=sender)
                session.append(user_message)
                _emit_message_event(run, USER_MESSAGE_EVENT, user_message)
                if isinstance(content, str):
                    _activate_triggered_skills(self._runtime, agent, session, content)
            run.raise_if_cancelled()
            messages = await self._build_request_messages(
                agent,
                session,
                replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
            )
            tools = self._runtime.system_prompts.provider_tool_definitions(agent)

            extension_registry = self._runtime.extensions
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                prompt_appends = await extension_registry.dispatch_before_agent_start(
                    extension_ctx,
                    agent=agent,
                    session=session,
                    messages=messages,
                    run=run,
                )
                if prompt_appends and messages:
                    system_content = messages[0].get("content")
                    if isinstance(system_content, str):
                        messages[0] = dict(messages[0])
                        messages[0]["content"] = system_content + "\n" + "\n".join(prompt_appends)
                    else:
                        _LOGGER.debug(
                            "before_agent_start: system message content is not a string; "
                            "skipping append"
                        )

            try:
                return await self._send_until_final(
                    agent,
                    adapter,
                    model_id,
                    session,
                    messages,
                    tools,
                    run,
                    provider_id=provider_id,
                    connection_id=connection_id,
                )
            except ProviderError as primary_exc:
                if _is_model_fallback_trigger(primary_exc):
                    fallback = _resolve_fallback(self._runtime, agent)
                    if fallback is not None:
                        fallback_model_str, fb_provider_id, fb_connection_id = fallback
                        _, fallback_model_id = _split_agent_model(fallback_model_str)
                        try:
                            fallback_adapter = self._runtime.get_adapter(
                                fb_provider_id,
                                fb_connection_id,
                            )
                        except (ConfigError, VBotError) as construction_exc:
                            _run_succeeded = False
                            _persist_run_error(run, session, construction_exc)
                            raise
                        run.add_cancel_callback(lambda: _close_adapter(fallback_adapter))
                        run.emit(
                            MODEL_FALLBACK_ACTIVATED_EVENT,
                            {"from_model": agent.model, "to_model": fallback_model_str},
                        )
                        session.add_note(
                            "Primary model unavailable. Switched to "
                            f"{fallback_model_str} for this run."
                        )
                        # The reused messages list may carry current-turn
                        # reasoning/reasoning_meta from the primary provider;
                        # stale meta must never reach the fallback provider.
                        _strip_assistant_reasoning_fields(messages)
                        try:
                            return await self._send_until_final(
                                agent,
                                fallback_adapter,
                                fallback_model_id,
                                session,
                                messages,
                                tools,
                                run,
                                provider_id=fb_provider_id,
                                connection_id=fb_connection_id,
                            )
                        except (ProviderError, ChatError, ConfigError, VBotError) as fallback_exc:
                            _run_succeeded = False
                            _persist_run_error(run, session, fallback_exc)
                            raise fallback_exc
                        finally:
                            await _close_adapter(fallback_adapter)

                _run_succeeded = False
                _persist_run_error(run, session, primary_exc)
                raise
            except (ChatError, ConfigError, VBotError) as exc:
                _run_succeeded = False
                _persist_run_error(run, session, exc)
                raise
            except asyncio.CancelledError:
                raise
            except BaseException:
                _run_succeeded = False
                raise
        finally:
            outcome: Literal["success", "error", "cancelled"]
            if run.cancel_requested:
                outcome = "cancelled"
            elif _run_succeeded:
                outcome = "success"
            else:
                outcome = "error"
            session.append(
                ChatMessage.run_summary(
                    run_id=run.id,
                    status={"success": "completed", "error": "failed", "cancelled": "cancelled"}[
                        outcome
                    ],
                    timing=_timing_payload(run_timing_started_at, run_timing_started_perf),
                )
            )

            extension_registry = self._runtime.extensions
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                await extension_registry.dispatch_run_end(
                    extension_ctx,
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    outcome=outcome,
                )

            await _close_adapter(adapter)

    def _get_session(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        create_missing: bool,
    ) -> ChatSession:
        session_manager = self._runtime.chat_sessions
        if session_id is None:
            if not create_missing:
                raise ChatSessionError("session id is required")
            return session_manager.create(agent_id)
        try:
            return session_manager.get(agent_id, session_id)
        except ChatSessionError:
            if not create_missing:
                raise
            return session_manager.create(agent_id, session_id=session_id)

    async def _build_request_messages(
        self,
        agent: Any,
        session: ChatSession,
        *,
        replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
    ) -> list[JsonObject]:
        system_prompt = self._runtime.system_prompts.build_system_prompt(agent)
        system_messages = (
            [ChatMessage.system(system_prompt, agent.model).to_dict()]
            if system_prompt.strip()
            else []
        )
        session_messages = session.load()
        checkpoint = _latest_compaction_checkpoint(session_messages)

        if checkpoint is None:
            history = _embed_notes_into_request(
                session_messages,
                replay_policy=replay_policy,
                agent_model=agent.model,
            )
            request_messages = [
                *system_messages,
                *session.skill_context_messages(session_messages),
                *history,
            ]
        else:
            tail_messages, tail_recovered = _resolve_preserved_tail(session_messages, checkpoint)
            if tail_recovered:
                _LOGGER.warning(
                    "Compaction tail boundary %r not found for session %s; "
                    "recovering from post-checkpoint history",
                    checkpoint.tail_boundary_id,
                    session.id,
                )
            summary_text = checkpoint.content if isinstance(checkpoint.content, str) else ""
            if tail_recovered:
                summary_text = (
                    f"{summary_text}\n\n{COMPACTION_TAIL_RECOVERED_HINT}"
                    if summary_text
                    else COMPACTION_TAIL_RECOVERED_HINT
                )
            summary_synthetic_message: JsonObject = {
                "role": "user",
                "content": (
                    f"{SYSTEM_REMINDER_OPEN_TAG}\n{summary_text}\n{SYSTEM_REMINDER_CLOSE_TAG}"
                ),
            }
            history = _embed_notes_into_request(
                tail_messages,
                replay_policy=replay_policy,
                agent_model=agent.model,
            )
            request_messages = [
                *system_messages,
                *session.skill_context_messages(session_messages),
                summary_synthetic_message,
                *history,
            ]

        session.drain_pending_notes()

        if self._attachment_resolver is None:
            return request_messages
        if not _session_has_any_content_blocks(session_messages):
            return request_messages

        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all content blocks resolve as historical.
        current_user_message = _last_user_message_with_content_blocks(
            session_messages
        ) or _last_user_message(session_messages)
        if current_user_message is None:
            return request_messages

        return await self._attachment_resolver.resolve_messages(
            request_messages,
            current_user_message_id=current_user_message.id,
            input_modalities=_model_input_modalities(self._runtime, agent),
        )

    async def _send_until_final(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        session: ChatSession,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        provider_id: str,
        connection_id: str,
    ) -> ChatMessage:
        replay_policy = _resolve_reasoning_replay_policy(adapter, model_id)
        tool_iteration_count = 0
        iteration_number = 1
        for _ in range(self._max_tool_iterations + 1):
            run.raise_if_cancelled()
            pending_notes = session.drain_pending_notes()
            if pending_notes:
                messages.append(_notes_to_synthetic_user_message(pending_notes))
            _sync_skill_context_messages(messages, session)
            extension_registry = self._runtime.extensions
            messages_for_request = [dict(message) for message in messages]
            if extension_registry is not None:
                extension_ctx = HookContext(
                    session_id=run.session_id,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    add_note=session.add_note,
                )
                messages_for_request = await extension_registry.dispatch_context(
                    extension_ctx,
                    messages=messages_for_request,
                )

            if hasattr(adapter, "set_debug_context"):
                adapter.set_debug_context(
                    DebugContext(
                        run_id=run.id,
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        provider_id=provider_id,
                        connection_id=connection_id,
                        model_id=model_id,
                        streaming=self._streaming,
                        iteration_number=iteration_number,
                    )
                )
            assistant_message = await self._send_assistant_request(
                agent,
                adapter,
                model_id,
                messages_for_request,
                tools,
                run,
                note_hook=session.add_note,
            )
            run.raise_if_cancelled()
            if assistant_message.usage is None:
                assistant_message = _apply_usage_estimation(assistant_message, messages)
            # Hold the per-session append lock from the assistant tool-call
            # message through its tool results so a writer on another accessor
            # (a channel observed note, session.link_channel) cannot land between
            # them and break the tool-cycle ordering invariant.
            async with self._runtime.chat_sessions.write_lock(run.agent_id, run.session_id):
                session.append(assistant_message)
                if not self._streaming:
                    _emit_assistant_events(run, assistant_message)
                messages.append(
                    _assistant_continuation_dict(assistant_message, replay_policy=replay_policy)
                )

                if not assistant_message.tool_calls:
                    if self._compaction_service is not None:
                        messages = await self._maybe_auto_compact(
                            agent,
                            adapter,
                            model_id,
                            session,
                            messages,
                            usage=assistant_message.usage,
                            run=run,
                        )
                    return assistant_message

                if tool_iteration_count >= self._max_tool_iterations:
                    raise ToolIterationLimitError("maximum tool iterations exceeded")
                tool_iteration_count += 1  # noqa: SIM113 - paired with iteration_number; enumerate would obscure the pre-increment limit check.
                iteration_number += 1

                session.begin_defer_notes()
                try:
                    tool_messages, media_injections = await _dispatch_tool_calls(
                        self._runtime,
                        agent,
                        assistant_message.tool_calls,
                        session,
                        run,
                        nesting_depth=self._nesting_depth,
                    )
                    for tool_message in tool_messages:
                        session.append(tool_message)
                        messages.append(_message_to_request_dict(tool_message))
                    # A tool may ask to show media (e.g. read on an image): inject it
                    # as a synthetic current-turn user message after the tool results
                    # so the tool-cycle invariant (results before any non-tool message)
                    # is preserved.
                    for injection in media_injections:
                        await self._inject_read_media(agent, session, messages, injection)
                    # Honor cancellation only after every sibling tool result has
                    # been persisted, so a mid-cycle cancel never leaves an
                    # assistant turn with dangling tool_calls in JSONL history.
                    run.raise_if_cancelled()
                finally:
                    session.flush_deferred_notes()

            if self._compaction_service is not None:
                messages = await self._maybe_auto_compact(
                    agent,
                    adapter,
                    model_id,
                    session,
                    messages,
                    usage=None,
                    run=run,
                )

        raise ToolIterationLimitError("maximum tool iterations exceeded")

    async def _inject_read_media(
        self,
        agent: Any,
        session: ChatSession,
        messages: list[JsonObject],
        injection: JsonObject,
    ) -> None:
        """Inject a tool-loaded media file as a synthetic current-turn user message.

        Only the small ``MediaBlock`` reference is persisted to the session, so a
        later run degrades it to a path note through the once-at-start resolver
        and context stays small. The base64-resolved request dict is appended to
        the in-flight ``messages`` so the model sees the image this turn — the
        resolver does not run again inside the tool loop. A non-vision model (or a
        missing resolver) gets a plain text note instead of a hard error, so the
        run never aborts.
        """
        media_type = injection["media_type"]
        filename = injection["filename"]
        media_block = MediaBlock(
            type="media",
            attachment_id=injection["attachment_id"],
            filename=filename,
            media_type=media_type,
        )
        user_message = ChatMessage.user([media_block])
        session.append(user_message)

        input_modalities = _model_input_modalities(self._runtime, agent)
        vision_unavailable = media_type.startswith("image/") and "image" not in input_modalities
        if self._attachment_resolver is None or vision_unavailable:
            messages.append(_read_media_text_note(filename, media_type))
            return

        resolved = await self._attachment_resolver.resolve_messages(
            [_message_to_request_dict(user_message)],
            current_user_message_id=user_message.id,
            input_modalities=input_modalities,
        )
        messages.append(resolved[0])

    async def _maybe_auto_compact(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        session: ChatSession,
        messages: list[JsonObject],
        usage: JsonObject | None,
        *,
        run: Run,
    ) -> list[JsonObject]:
        """Auto-compact when configured token thresholds are exceeded."""
        if self._compaction_service is None:
            return messages

        settings = self._load_compaction_settings()
        if not settings.auto:
            return messages

        context_window = self._resolve_context_window(agent)
        if context_window is None:
            return messages

        if isinstance(usage, dict):
            input_tokens_raw = usage.get("input_tokens")
            input_tokens = (
                input_tokens_raw
                if isinstance(input_tokens_raw, int) and not isinstance(input_tokens_raw, bool)
                else 0
            )
        else:
            input_tokens = self._compaction_service.estimate_messages_tokens(messages)

        if not self._compaction_service.should_auto_compact(
            input_tokens,
            context_window,
            settings.threshold,
        ):
            return messages

        summary_adapter, summary_model_id = self._resolve_summary_adapter(
            agent,
            adapter,
            model_id,
            settings,
        )
        close_summary_adapter = summary_adapter is not adapter
        try:
            checkpoint = await self._compaction_service.compact(
                session.load(),
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=self._runtime.storage,
                settings=settings,
            )
        except Exception:
            _LOGGER.warning("Compaction failed; continuing without compaction", exc_info=True)
            return messages
        finally:
            if close_summary_adapter:
                await _close_adapter(summary_adapter)

        session.append(checkpoint)
        run.emit(COMPACTION_COMPLETED_EVENT, {"message": checkpoint.to_dict()})
        rebuilt_messages = await self._build_request_messages(
            agent,
            session,
            replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
        )
        return _restore_in_run_assistant_reasoning(rebuilt_messages, messages)

    def _load_compaction_settings(self) -> CompactionSettings:
        """Build typed compaction settings from the persisted normalized section."""
        # Local import: core.compaction imports core.chat at module load, so
        # chat must not import it back at module level (runtime cycle).
        from core.compaction import CompactionSettings

        raw_settings = self._runtime.storage.load_compaction_settings()
        return CompactionSettings(
            auto=bool(raw_settings["auto"]),
            threshold=float(raw_settings["threshold"]),
            tail_tokens=int(raw_settings["tail_tokens"]),
            summary_model=raw_settings["summary_model"],
        )

    def _resolve_context_window(self, agent: Any) -> int | None:
        """Resolve the usable context window for the active agent model.

        Returns ``None`` only when the model string is unusable (no
        ``provider/model`` form). Otherwise the value always resolves through the
        shared default chain (model window → provider-config default → global
        floor, see :func:`resolve_context_window`), so a model whose window is
        ``None`` still gets a usable budget and auto-compaction keeps working
        instead of silently disabling itself.
        """
        bare_model = parse_bare_model(agent.model)
        if "/" not in bare_model:
            return None

        provider_id, _, resolved_model_id = bare_model.partition("/")
        if not provider_id or not resolved_model_id:
            return None

        try:
            model_entry = self._runtime.models.get(provider_id, resolved_model_id)
        except (KeyError, AttributeError):
            return None

        return resolve_context_window(
            model_entry.context_window,
            self._lookup_provider_config(provider_id),
        )

    def _lookup_provider_config(self, provider_id: str) -> Any:
        """Return the ProviderConfig for the read-side window default, or None.

        Tolerant of a missing/partial runtime (the registry may be absent for a
        custom provider): the resolver treats ``None`` as "no provider default"
        and falls back to the global floor.
        """
        try:
            return self._runtime.providers.get(provider_id)
        except (KeyError, AttributeError):
            return None

    def _resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
    ) -> tuple[Any, str]:
        """Resolve compaction summary adapter/model, defaulting to active run target."""
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id

        try:
            provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
                summary_model
            )
            if connection_suffix:
                connection_id = f"{provider_id}:{connection_suffix}"
            else:
                connection_id = _first_usable_connection_id(self._runtime, provider_id)
            summary_adapter = self._runtime.get_adapter(provider_id, connection_id)
        except (ChatError, ConfigError, VBotError, KeyError):
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id

        return summary_adapter, summary_model_id

    async def _send_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        note_hook: Callable[[str], None] | None = None,
    ) -> ChatMessage:
        if self._streaming:
            return await self._send_streaming_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
                run,
                note_hook=note_hook,
            )

        return await self._send_non_streaming_assistant_request(
            agent, adapter, model_id, messages, tools
        )

    async def _send_non_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
    ) -> ChatMessage:
        response = await adapter.send(
            messages,
            model_id=model_id,
            temperature=agent.temperature,
            thinking_effort=agent.thinking_effort,
            tools=tools,
        )
        normalized = adapter.normalize_response(response, model_id=model_id)
        return _assistant_message_from_response(agent.model, normalized)

    async def _send_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        note_hook: Callable[[str], None] | None = None,
    ) -> ChatMessage:
        # A transient drop before any visible output is replayed as a full stream
        # restart (the not-yet-visible analogue of the non-streaming fallback).
        # Once anything visible has been emitted, the failure propagates instead —
        # partial output cannot be replayed cleanly.
        for attempt in range(MAX_STREAM_RESTARTS + 1):
            try:
                return await self._consume_stream_attempt(
                    agent,
                    adapter,
                    model_id,
                    messages,
                    tools,
                    run,
                    note_hook,
                    can_restart=attempt < MAX_STREAM_RESTARTS,
                )
            except _StreamRestartNeeded as restart:
                _LOGGER.warning(
                    "Streaming attempt %d/%d dropped before any visible output "
                    "(%s: %s); restarting stream",
                    attempt + 1,
                    MAX_STREAM_RESTARTS + 1,
                    type(restart.cause).__name__,
                    restart.cause,
                )
        # Unreachable: the final attempt runs with can_restart=False, so it either
        # returns a message or re-raises the underlying error.
        raise AssertionError("stream restart loop exited without returning")

    async def _consume_stream_attempt(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        note_hook: Callable[[str], None] | None,
        *,
        can_restart: bool,
    ) -> ChatMessage:
        accumulator = StreamingAccumulator()
        emitted_visible_delta = False
        stream = adapter.stream(
            messages,
            model_id=model_id,
            temperature=agent.temperature,
            thinking_effort=agent.thinking_effort,
            tools=tools,
        )

        try:
            async for delta in iter_with_chunk_timeout(
                stream,
                timeout_seconds=STREAM_CHUNK_TIMEOUT_SECONDS,
            ):
                run.raise_if_cancelled()
                visible_deltas = accumulator.add_delta(delta)
                for visible_delta in visible_deltas:
                    run.emit(visible_delta.event_type, visible_delta.payload)
                    emitted_visible_delta = True
                run.raise_if_cancelled()
            if accumulator.finish_reason is None:
                raise NetworkError("Provider stream ended without finish delta")
            assistant_fields = accumulator.finalize_assistant_fields()
        except (ProviderError, NetworkError, StreamingChunkTimeoutError) as exc:
            if not emitted_visible_delta and _is_streaming_fallback_error(exc):
                assistant_message = await self._send_non_streaming_assistant_request(
                    agent,
                    adapter,
                    model_id,
                    messages,
                    tools,
                )
                _emit_assistant_events(run, assistant_message)
                return assistant_message
            # A chunk stall before any visible output is the not-yet-visible
            # analogue of the transient-drop restart: replay the whole stream.
            # Once visible output exists, this falls through and re-raises.
            if can_restart and not emitted_visible_delta and _is_stream_restartable_error(exc):
                raise _StreamRestartNeeded(exc) from exc
            _maybe_persist_partial_thinking(accumulator, note_hook)
            raise
        except BaseException:
            _maybe_persist_partial_thinking(accumulator, note_hook)
            raise

        assistant_message = _assistant_message_from_response(
            agent.model,
            assistant_fields.to_response_dict(),
        )
        _emit_streaming_assistant_events(run, assistant_message)
        return assistant_message
