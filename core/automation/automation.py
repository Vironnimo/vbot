"""Automation primitives for programmatic chat run triggering."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop
from core.chat.chat import (
    _close_adapter,
    _resolve_agent_connection,
    _split_agent_model,
    parse_model_with_connection,
)
from core.chat.content_blocks import ContentBlock
from core.chat.errors import ChatError
from core.compaction import CompactionSettings
from core.runs import ActiveRunError, ChatRunManager, Run

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime


class TriggerService:
    """Start programmatic chat runs and queue triggers behind active runs."""

    def __init__(
        self,
        chat_loop: ChatLoop,
        chat_run_manager: ChatRunManager,
        runtime: Runtime,
        *,
        trigger_chat_loop: ChatLoop | None = None,
    ) -> None:
        self._chat_loop = chat_loop
        self._trigger_chat_loop = trigger_chat_loop or chat_loop
        self._chat_run_manager = chat_run_manager
        self._runtime = runtime

    async def trigger_run(
        self,
        agent_id: str,
        message: str | list[ContentBlock],
        session_id: str | None = None,
        *,
        internal: bool = False,
    ) -> Run:
        """Start a run immediately, or queue it until the target session is idle."""
        target_session_id = session_id
        if target_session_id is None:
            target_session_id = self._runtime.chat_sessions.create(agent_id).id

        try:
            if internal:
                return await self._trigger_chat_loop.start_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                )
            return await self._trigger_chat_loop.start_run(
                agent_id,
                message,
                session_id=target_session_id,
            )
        except ActiveRunError:
            if internal:
                queued_item = await self._trigger_chat_loop.queue_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                    internal=True,
                )
            else:
                queued_item = await self._trigger_chat_loop.queue_run(
                    agent_id,
                    message,
                    session_id=target_session_id,
                )
            return await queued_item.future

    async def retry_run(self, agent_id: str, session_id: str) -> Run:
        """Retry the last user turn for a channel or automation entry point."""
        return await self._trigger_chat_loop.retry_run(agent_id, session_id)

    async def compact_session(self, agent_id: str, session_id: str) -> str:
        """Compact a session and return a user-facing command reply."""
        compaction_service = getattr(self._chat_loop, "_compaction_service", None)
        if compaction_service is None:
            return "Compaction is not available."

        active_run = self._chat_run_manager.active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return "Cannot compact while a run is active for this session."

        agent = self._runtime.agents.get(agent_id)
        session = self._runtime.chat_sessions.get(agent_id, session_id)
        messages = session.load()
        raw_settings = self._runtime.storage.load_compaction_settings()
        settings = CompactionSettings(
            auto=raw_settings["auto"],
            threshold=raw_settings["threshold"],
            tail_tokens=raw_settings["tail_tokens"],
            summary_model=raw_settings["summary_model"],
        )

        adapter: Any | None = None
        summary_adapter: Any | None = None

        try:
            provider_id, connection_id = _resolve_agent_connection(self._runtime, agent)
            adapter = self._runtime.get_adapter(provider_id, connection_id)
            _model_provider_id, model_id = _split_agent_model(agent.model)
            summary_adapter = adapter
            summary_model_id = model_id
            summary_adapter, summary_model_id = _resolve_summary_adapter_for_compact(
                self._runtime,
                adapter,
                model_id,
                settings,
            )
            checkpoint = await compaction_service.compact(
                messages,
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=self._runtime.storage,
                settings=settings,
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


def _resolve_summary_adapter_for_compact(
    runtime: Any,
    adapter: Any,
    model_id: str,
    settings: CompactionSettings,
) -> tuple[Any, str]:
    summary_model = settings.summary_model
    if not isinstance(summary_model, str):
        return adapter, model_id

    normalized_summary_model = summary_model.strip()
    if not normalized_summary_model:
        return adapter, model_id

    try:
        provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
            normalized_summary_model
        )
    except ChatError:
        return adapter, model_id

    connection_id: str | None = None
    if connection_suffix:
        connection_id = f"{provider_id}:{connection_suffix}"
    else:
        try:
            provider = runtime.providers.get(provider_id)
        except Exception:
            return adapter, model_id

        credential_resolver = getattr(runtime, "provider_credentials", None)
        if credential_resolver is None:
            return adapter, model_id

        for connection in provider.connections:
            candidate_connection_id = f"{provider_id}:{connection.id}"
            if credential_resolver.has_credentials(provider_id, candidate_connection_id):
                connection_id = candidate_connection_id
                break

    if connection_id is None:
        return adapter, model_id

    try:
        summary_adapter = runtime.get_adapter(provider_id, connection_id)
    except Exception:
        return adapter, model_id

    return summary_adapter, summary_model_id
