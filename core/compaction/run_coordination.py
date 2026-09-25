"""Compaction-run orchestration executed inside the canonical Run lifecycle."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from core.chat import RequestState, checkpoint_ordinal, has_unconsumed_skill_activation
from core.chat.messages import JsonObject
from core.chat.usage import (
    RequestContextUsage,
    aggregate_session_usage,
    checkpoint_context_usage,
    latest_session_context_usage,
)
from core.compaction.compaction import (
    COMPACTION_POLICY_META_KEY,
    COMPACTION_TRIGGER_MANUAL,
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionError,
    CompactionInsufficientReclaimError,
    CompactionSettings,
    effective_compaction_policy,
)
from core.performance import record_span, session_track
from core.providers.adapter import estimate_wire_request_input_tokens
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
)
from core.sessions import SessionAddress
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.compaction import CompactionService
    from core.runs import Run
    from core.sessions import ChatSession, SessionReadCursor


_LOGGER = get_logger("compaction.coordination")


class ManualCompactionRequest(Protocol):
    """Materialized Chat operation retained for one manual Compaction."""

    @property
    def project_cwd(self) -> Path | None: ...

    @property
    def activation_skill_project_id(self) -> str | None: ...

    @property
    def request_state(self) -> RequestState: ...

    @property
    def request_inputs(self) -> object: ...

    @property
    def active_adapter(self) -> Any: ...

    @property
    def active_model_id(self) -> str: ...

    @property
    def active_provider_id(self) -> str: ...

    @property
    def summary_adapter(self) -> Any: ...

    @property
    def summary_model_id(self) -> str: ...

    @property
    def summary_provider_id(self) -> str: ...

    @property
    def summary_temperature(self) -> float | None: ...

    @property
    def active_temperature(self) -> float | None: ...


class CompactionRunHost(Protocol):
    """Complete Chat-owned request operation used by the coordinator."""

    @property
    def compaction_service(self) -> CompactionService | None:
        """Compaction service of the hosting loop; ``None`` disables Compaction."""
        ...

    @property
    def sessions(self) -> Any: ...

    @property
    def storage(self) -> Any: ...

    @property
    def models(self) -> Any: ...

    async def run_transform(self, function: Any, *args: Any, **kwargs: Any) -> Any: ...

    async def materialize_manual_request(
        self,
        run: Run,
        agent: Any,
        session: ChatSession,
        messages: list[ChatMessage],
        settings: CompactionSettings,
    ) -> ManualCompactionRequest: ...

    async def close_manual_request(self, request: Any) -> None: ...

    async def close_adapter(self, adapter: Any) -> None: ...

    async def finalize_checkpoint(
        self,
        checkpoint: ChatMessage,
        session_messages: list[ChatMessage],
    ) -> ChatMessage: ...

    async def prepare_prompt_refresh(
        self,
        *,
        agent_id: str,
        session_id: str,
        agent: Any,
        project_id: str | None,
        working_project_id: str | None,
        project_cwd: Path | None,
        activation_skill_project_id: str | None,
    ) -> object: ...

    async def commit_checkpoint(
        self,
        session: ChatSession,
        checkpoint: ChatMessage,
        *,
        since: SessionReadCursor,
        prompt_refresh: object | None,
    ) -> bool: ...

    async def commit_automatic_checkpoint(
        self,
        context: Any,
        checkpoint: ChatMessage,
        *,
        prompt_refresh: object | None,
    ) -> bool: ...

    async def project_post_compaction_request(
        self,
        *,
        agent: Any,
        session: ChatSession,
        session_messages: list[ChatMessage],
        checkpoint: ChatMessage,
        context_tokens_before: int,
        request_inputs: object,
        prompt_refresh: object | None,
        active_adapter: Any,
        active_model_id: str,
        live_request_messages: list[JsonObject] | None = None,
        continuation_reminder: str | None = None,
    ) -> tuple[ChatMessage, RequestState]: ...

    async def project_automatic_compaction_request(
        self,
        *,
        context: Any,
        target: Any,
        session_messages: list[ChatMessage],
        checkpoint: ChatMessage,
        context_tokens_before: int,
        prompt_refresh: object | None,
        live_request_messages: list[JsonObject] | None,
        continuation_reminder: str | None,
    ) -> tuple[ChatMessage, RequestState]: ...

    async def refresh_continuation_reminder(
        self,
        context: Any,
        *,
        context_window: int | None,
    ) -> None: ...

    async def rebuild_after_stale_compaction(
        self,
        context: Any,
        target: Any,
        live_request_messages: list[JsonObject],
    ) -> RequestState: ...

    def resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
        *,
        active_provider_id: str,
    ) -> tuple[Any, str, str]: ...

    def resolve_context_window(self, agent: Any, target: Any | None = None) -> int | None: ...

    def resolve_temperature(self, provider_id: str, model_id: str) -> float | None: ...


class CompactionRunCoordinator:
    """Runs manual and automatic Compaction against its host loop's seam.

    Stateless besides its two injected fields: every Run-scoped fact travels
    through the arguments, and loop-owned capabilities are reached through the
    :class:`CompactionRunHost` protocol instead of a ChatLoop import.
    """

    def __init__(
        self,
        *,
        host: CompactionRunHost,
    ) -> None:
        self._host = host

    async def execute_manual_run(
        self,
        run: Run,
        agent: Any,
        session: ChatSession,
        compaction_service: CompactionService,
        *,
        instruction: str | None,
    ) -> ChatMessage:
        """Execute one manual Compaction inside its canonical Run lifecycle."""
        session = session.for_run(run.id)
        request: Any | None = None
        # The divider appears at this emit; its visible duration ends when the
        # checkpoint is stamped below.
        compaction_started_perf = time.perf_counter()
        try:
            own_messages, messages, snapshot_cursor = await self._load_compaction_snapshot(
                run, session
            )
            context_usage = latest_session_context_usage(messages)
            if context_usage is not None:
                run.terminal_payload_extras["context_usage"] = context_usage
            run.emit(
                COMPACTION_STARTED_EVENT,
                {"context_usage": context_usage} if context_usage is not None else {},
            )
            settings = await self._host.run_transform(
                self._load_compaction_settings,
                agent,
                agent_id=run.agent_id,
                session_id=run.session_id,
                project_id=run.project_id,
            )
            try:
                request = await self._host.materialize_manual_request(
                    run,
                    agent,
                    session,
                    messages,
                    settings,
                )
                wire_context_tokens_before = await self._host.run_transform(
                    estimate_wire_request_input_tokens,
                    request.active_adapter,
                    request.request_state.messages,
                    model_id=request.active_model_id,
                    tools=request.request_state.tools,
                )
                context_usage = {
                    "tokens": wire_context_tokens_before,
                    "estimated": True,
                }
                run.terminal_payload_extras["context_usage"] = context_usage
                context_tokens_before = wire_context_tokens_before
                checkpoint = await compaction_service.compact(
                    messages,
                    session_address=session.address,
                    run_id=run.id,
                    owner_name=(
                        run.execution_owner.extension if run.execution_owner is not None else None
                    ),
                    group_id=(
                        run.execution_owner.group_id if run.execution_owner is not None else None
                    ),
                    prompt_cache_affinity_id=await self._host.run_transform(
                        self._host.sessions.prompt_cache_affinity_id,
                        session.address,
                    ),
                    summary_adapter=request.summary_adapter,
                    summary_model_id=request.summary_model_id,
                    storage=self._host.storage,
                    settings=settings,
                    instruction=instruction,
                    request_messages=request.request_state.messages,
                    trigger=COMPACTION_TRIGGER_MANUAL,
                    active_adapter=request.active_adapter,
                    active_model_id=request.active_model_id,
                    active_tools=request.request_state.tools,
                    summary_temperature=request.summary_temperature,
                    active_temperature=request.active_temperature,
                    summary_model_reference=f"{request.summary_provider_id}/{request.summary_model_id}",
                    active_model_reference=f"{request.active_provider_id}/{request.active_model_id}",
                )
            finally:
                if request is not None:
                    await self._host.close_manual_request(request)

            checkpoint = await self._host.finalize_checkpoint(
                checkpoint,
                messages,
            )
            checkpoint = checkpoint.with_compaction_duration_ms(
                duration_ms=round((time.perf_counter() - compaction_started_perf) * 1000)
            )
            prompt_refresh: object | None = None
            try:
                prompt_refresh = await self._host.prepare_prompt_refresh(
                    agent_id=run.agent_id,
                    session_id=run.session_id,
                    agent=agent,
                    project_id=run.project_id,
                    working_project_id=run.working_project_id,
                    project_cwd=request.project_cwd,
                    activation_skill_project_id=request.activation_skill_project_id,
                )
            except Exception:
                _LOGGER.warning(
                    "Prompt context refresh failed after manual Compaction (agent=%s session=%s)",
                    run.agent_id,
                    run.session_id,
                    exc_info=True,
                )
            checkpoint, _ = await self._host.project_post_compaction_request(
                agent=agent,
                session=session,
                session_messages=messages,
                checkpoint=checkpoint,
                context_tokens_before=context_tokens_before,
                request_inputs=request.request_inputs,
                prompt_refresh=prompt_refresh,
                active_adapter=request.active_adapter,
                active_model_id=request.active_model_id,
            )
            if not await self._host.commit_checkpoint(
                session,
                checkpoint,
                since=snapshot_cursor,
                prompt_refresh=prompt_refresh,
            ):
                raise CompactionError("Session context changed during Compaction. Please retry.")
            messages.append(checkpoint)
            own_messages.append(checkpoint)
            self._emit_compaction_completed(run, messages, checkpoint)
            run.terminal_payload_extras["session_usage"] = aggregate_session_usage(own_messages)
            return checkpoint
        except asyncio.CancelledError:
            run.emit(COMPACTION_ABORTED_EVENT, {"reason": "cancelled"})
            raise
        except Exception:
            run.emit(COMPACTION_ABORTED_EVENT, {"reason": "failed"})
            raise
        finally:
            record_span(
                "chat.compaction",
                compaction_started_perf,
                track=session_track(run.agent_id, run.session_id, run.project_id),
                name="compaction",
                args={"run_id": run.id, "trigger": "manual"},
            )

    async def _load_compaction_snapshot(
        self,
        run: Run,
        session: ChatSession,
    ) -> tuple[list[ChatMessage], list[ChatMessage], SessionReadCursor]:
        """Load the own audit and the current view in one snapshot, without racing an append."""
        session_address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        async with self._host.sessions.write_lock(session_address):
            snapshot = await session.load_since_async()
        if snapshot is None:
            raise AssertionError("A full Session snapshot must always produce a cursor")
        return list(snapshot.messages), list(snapshot.active_messages), snapshot.cursor

    async def maybe_auto_compact_state(
        self,
        context: Any,
        target: Any,
        usage: JsonObject | None,
        *,
        continuation_request_messages: list[JsonObject] | None = None,
        allow_continuation: bool = False,
        continue_same_run: bool = True,
    ) -> RequestState:
        """Auto-compact when configured token thresholds are exceeded."""
        if context.request_state is None:
            raise AssertionError("Run request state must exist before Compaction")
        current_state = cast(RequestState, context.request_state)
        run = context.run
        agent = context.agent
        session = context.session
        messages = current_state.messages
        tools = current_state.tools
        if self._host.compaction_service is None:
            return current_state

        settings = await self._host.run_transform(
            self._load_compaction_settings,
            agent,
            agent_id=run.agent_id,
            session_id=run.session_id,
            project_id=run.project_id,
        )
        forced = run.compaction_state == "pending"
        if not settings.auto and not forced:
            return current_state
        if settings.strategy == "continuation" and not allow_continuation:
            if forced and not continue_same_run:
                run.emit(COMPACTION_ABORTED_EVENT, {"reason": "run_finished"})
            return current_state

        context_window = self._host.resolve_context_window(agent, target)
        if context_window is None:
            if forced:
                run.emit(COMPACTION_ABORTED_EVENT, {"reason": "context_window_unavailable"})
            return current_state

        current_request_messages = continuation_request_messages or messages
        accounting = getattr(context, "context_usage", None) or RequestContextUsage()
        effective_context_usage = await self._host.run_transform(
            accounting.project,
            current_request_messages,
            adapter=target.adapter,
            model_id=target.model_id,
            tools=tools,
            scope=context.prompt_cache_affinity_id,
        )
        input_tokens = int(effective_context_usage["tokens"])
        run.terminal_payload_extras["context_usage"] = effective_context_usage

        should_compact = self._host.compaction_service.should_auto_compact(
            input_tokens,
            context_window,
            settings.threshold,
            settings=settings,
        )
        if not should_compact and not forced:
            return current_state
        # Only this Run appends through its snapshot, but out-of-band writers
        # (Notes, deliveries) may have advanced the Session; the delta brings the
        # snapshot current, and its cursor is what the checkpoint commit verifies.
        async with self._host.sessions.write_lock(session.address):
            await context.session_snapshot.refresh(session)
        session_messages = list(context.session_snapshot.active_messages)
        if settings.strategy == "summary_tail" and has_unconsumed_skill_activation(
            session_messages
        ):
            return current_state
        has_new_context = await self._host.run_transform(
            self._host.compaction_service.has_new_compactable_context,
            session_messages,
            settings,
            request_messages=continuation_request_messages or messages,
            active_adapter=target.adapter,
            active_model_id=target.model_id,
            minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
        )
        if not has_new_context:
            if forced:
                run.emit(COMPACTION_ABORTED_EVENT, {"reason": "no_compactable_context"})
            return current_state

        token_limit = (
            settings.trigger_tokens
            if settings.trigger == "input_tokens"
            else settings.max_input_tokens
        )
        _LOGGER.info(
            "Auto-compaction triggered (run=%s agent=%s session=%s input_tokens=%d "
            "context_window=%d trigger=%s ratio_threshold=%s token_limit=%s)",
            run.id,
            run.agent_id,
            run.session_id,
            input_tokens,
            context_window,
            settings.trigger,
            settings.threshold,
            token_limit,
        )
        summary_adapter, summary_model_id, summary_provider_id = self._host.resolve_summary_adapter(
            agent,
            target.adapter,
            target.model_id,
            settings,
            active_provider_id=target.provider_id,
        )
        close_summary_adapter = summary_adapter is not target.adapter
        compaction_started_perf = time.perf_counter()
        compaction_track = session_track(run.agent_id, run.session_id, run.project_id)
        try:
            run.emit(
                COMPACTION_STARTED_EVENT,
                {
                    "context_tokens_before": input_tokens,
                    "context_usage": effective_context_usage,
                },
            )
            try:
                checkpoint = await self._host.compaction_service.compact(
                    session_messages,
                    session_address=session.address,
                    run_id=run.id,
                    owner_name=(
                        run.execution_owner.extension if run.execution_owner is not None else None
                    ),
                    group_id=(
                        run.execution_owner.group_id if run.execution_owner is not None else None
                    ),
                    prompt_cache_affinity_id=context.prompt_cache_affinity_id,
                    summary_adapter=summary_adapter,
                    summary_model_id=summary_model_id,
                    summary_model_reference=f"{summary_provider_id}/{summary_model_id}",
                    active_model_reference=f"{target.provider_id}/{target.model_id}",
                    storage=self._host.storage,
                    settings=settings,
                    request_messages=continuation_request_messages or messages,
                    active_adapter=target.adapter,
                    active_model_id=target.model_id,
                    active_tools=tools,
                    minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
                    summary_temperature=self._host.resolve_temperature(
                        summary_provider_id,
                        summary_model_id,
                    ),
                    active_temperature=self._host.resolve_temperature(
                        target.provider_id,
                        target.model_id,
                    ),
                )
            except CompactionInsufficientReclaimError as exc:
                run.emit(
                    COMPACTION_ABORTED_EVENT,
                    {"reason": "insufficient_reclaim"},
                )
                _LOGGER.info(
                    "Auto-compaction skipped because projected reclaim was too small "
                    "(run=%s session=%s reason=%s)",
                    run.id,
                    run.session_id,
                    exc,
                )
                return current_state
            except Exception:
                run.emit(
                    COMPACTION_ABORTED_EVENT,
                    {"reason": "failed"},
                )
                _LOGGER.warning("Compaction failed; continuing without compaction", exc_info=True)
                return current_state
            finally:
                if close_summary_adapter:
                    await self._host.close_adapter(summary_adapter)

            checkpoint = await self._host.finalize_checkpoint(
                checkpoint,
                session_messages,
            )
            checkpoint = checkpoint.with_compaction_duration_ms(
                duration_ms=round((time.perf_counter() - compaction_started_perf) * 1000)
            )
            prompt_refresh: object | None = None
            try:
                try:
                    prompt_refresh = await self._host.prepare_prompt_refresh(
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        agent=agent,
                        project_id=run.project_id,
                        working_project_id=run.working_project_id,
                        project_cwd=context.project_cwd,
                        activation_skill_project_id=context.skill_project_id,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Prompt context refresh failed after automatic Compaction "
                        "(run=%s agent=%s session=%s)",
                        run.id,
                        run.agent_id,
                        run.session_id,
                        exc_info=True,
                    )

                if continue_same_run:
                    await self._host.refresh_continuation_reminder(
                        context,
                        context_window=self._host.resolve_context_window(agent, target),
                    )
                checkpoint, rebuilt_state = await self._host.project_automatic_compaction_request(
                    context=context,
                    target=target,
                    session_messages=session_messages,
                    checkpoint=checkpoint,
                    context_tokens_before=input_tokens,
                    prompt_refresh=prompt_refresh,
                    live_request_messages=messages if continue_same_run else None,
                    continuation_reminder=(
                        context.continuation_reminder if continue_same_run else None
                    ),
                )
            except Exception:
                run.emit(COMPACTION_ABORTED_EVENT, {"reason": "failed"})
                _LOGGER.warning(
                    "Post-compaction request projection failed; continuing without Compaction",
                    exc_info=True,
                )
                return current_state

            checkpoint_committed = await self._host.commit_automatic_checkpoint(
                context,
                checkpoint,
                prompt_refresh=prompt_refresh,
            )
            if not checkpoint_committed:
                run.emit(COMPACTION_ABORTED_EVENT, {"reason": "stale_context"})
                _LOGGER.info(
                    "Auto-compaction discarded because the Session changed during its Model call "
                    "(run=%s session=%s)",
                    run.id,
                    run.session_id,
                )
                if continue_same_run:
                    try:
                        return await self._host.rebuild_after_stale_compaction(
                            context,
                            target,
                            messages,
                        )
                    except Exception:
                        _LOGGER.warning(
                            "Request rebuild after stale auto-compaction failed; continuing with "
                            "the existing request state",
                            exc_info=True,
                        )
                return current_state
            accounting.reset()
            self._emit_compaction_completed(
                run, context.session_snapshot.active_messages, checkpoint
            )
            checkpoint_usage = checkpoint.usage or {}
            _LOGGER.info(
                "Auto-compaction completed (run=%s session=%s estimated_tokens_after=%d)",
                run.id,
                run.session_id,
                checkpoint_usage.get("context_tokens_after", 0),
            )
            return rebuilt_state
        finally:
            record_span(
                "chat.compaction",
                compaction_started_perf,
                track=compaction_track,
                name="compaction",
                args={"run_id": run.id, "trigger": "auto"},
            )

    @staticmethod
    def _emit_compaction_completed(
        run: Run,
        session_messages: list[ChatMessage],
        checkpoint: ChatMessage,
    ) -> None:
        """Publish the one completed-checkpoint payload used by every trigger."""
        checkpoint_usage = checkpoint.usage or {}
        context_usage = checkpoint_context_usage(checkpoint)
        if context_usage is not None:
            run.terminal_payload_extras["context_usage"] = context_usage
        payload: JsonObject = {
            "message": checkpoint.to_dict(),
            "checkpoint": checkpoint_ordinal(session_messages, checkpoint.id),
            "checkpoint_id": checkpoint.id,
            "history_available": True,
            "context_tokens_before": checkpoint_usage.get("context_tokens_before"),
            "context_tokens_after": checkpoint_usage.get("context_tokens_after"),
        }
        if context_usage is not None:
            payload["context_usage"] = context_usage
        duration_ms = checkpoint_usage.get("compaction_duration_ms")
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        run.emit(
            COMPACTION_COMPLETED_EVENT,
            payload,
        )

    def _load_compaction_settings(
        self,
        agent: Any,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
    ) -> CompactionSettings:
        """Resolve Session override → Agent default → global Compaction Policy."""
        session_policy = self._host.sessions.metadata_value(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
            COMPACTION_POLICY_META_KEY,
        )
        raw_settings = effective_compaction_policy(
            session_policy,
            getattr(agent, "compaction_policy", None),
            self._host.storage.load_compaction_settings,
        )
        trigger = raw_settings["trigger"]
        strategy = raw_settings["strategy"]
        trigger_type = str(trigger["type"])
        return CompactionSettings(
            auto=bool(raw_settings["enabled"]),
            trigger=trigger_type,
            threshold=float(trigger.get("threshold", 0.8)),
            trigger_tokens=int(trigger.get("tokens", 100_000)),
            max_input_tokens=(
                int(trigger["tokens"])
                if trigger_type == "context_ratio" and "tokens" in trigger
                else None
            ),
            strategy=str(strategy["type"]),
            tail_tokens=int(strategy.get("tail_tokens", 15_000)),
            summary_model=strategy.get("summary_model"),
        )
