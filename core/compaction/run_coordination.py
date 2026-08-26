"""Compaction-run orchestration executed inside the canonical Run lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from core.chat.chat import (
    _CHAT_TRANSFORM_WORKERS,
    _LOGGER,
    SEEN_SKILLS_META_KEY,
    ChatLoopDependencies,
    RequestBuildInputs,
    _CompactionPromptRefresh,
    _finalize_compaction_checkpoint,
    _RequestState,
    _resolve_agent_connection,
    _resolve_reasoning_replay_policy,
    _resolve_wire_media_support,
    _resolved_model_reference,
    _restore_in_run_tool_result_content,
)
from core.chat.continuation import (
    fold_continuation_records,
    inject_continuation_reminder,
    render_continuation_reminder,
)
from core.chat.events import _close_adapter
from core.chat.messages import (
    JsonObject,
    checkpoint_ordinal,
    has_unconsumed_skill_activation,
)
from core.chat.model_resolution import (
    _model_input_modalities_for_target,
    _split_agent_model,
    resolve_request_temperature,
)
from core.chat.usage import (
    aggregate_session_usage,
    build_model_step_context_usage,
    checkpoint_context_usage,
    latest_session_context_usage,
)
from core.chat.wire_shaping import _restore_in_run_assistant_reasoning
from core.compaction.compaction import (
    COMPACTION_POLICY_META_KEY,
    COMPACTION_TRIGGER_MANUAL,
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionInsufficientReclaimError,
    CompactionSettings,
)
from core.projects import resolve_prompt_project, resolve_skill_scope, runtime_agent_body
from core.prompts import ProjectPromptContext
from core.prompts.pinned_context import (
    PINNED_MEMORY_FILES_META_KEY,
    PINNED_SKILL_CATALOG_META_KEY,
    PINNED_SOUL_CONTEXT_META_KEY,
    PINNED_WORKING_PROJECT_CONTEXT_META_KEY,
    pinned_memory_files,
    pinned_skill_catalog,
    pinned_soul_context,
    pinned_working_project_context,
    stamp_prompt_files_read,
)
from core.providers.accounts import ConnectionRef
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
)
from core.sessions import SessionAddress
from core.settings.normalizers import normalize_compaction_policy
from core.utils.tokens import estimate_request_input_tokens

if TYPE_CHECKING:
    from core.chat.chat import _ModelTarget, _RunExecutionContext
    from core.chat.messages import ChatMessage
    from core.compaction import CompactionService
    from core.runs import Run
    from core.sessions import ChatSession, SessionReadCursor
    from core.skills.skills import SkillRegistry


class CompactionRunHost(Protocol):
    """The ChatLoop-execution seam the coordinator needs."""

    @property
    def compaction_service(self) -> CompactionService | None:
        """Compaction service of the hosting loop; ``None`` disables Compaction."""
        ...

    async def build_request_state(
        self,
        agent: Any,
        session: ChatSession,
        *,
        inputs: RequestBuildInputs,
    ) -> _RequestState:
        """Build one provider request state for *agent* from *inputs*."""
        ...

    def resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
        *,
        active_provider_id: str,
    ) -> tuple[Any, str, str]:
        """Resolve the compaction summary adapter/model/provider."""
        ...

    def resolve_project_cwd(self, project_id: str | None) -> Path | None:
        """Resolve a working Project cwd, failing closed when unavailable."""
        ...

    def resolve_context_window(self, agent: Any) -> int | None:
        """Resolve the usable context window for the active agent model."""
        ...

    def available_skill_names(
        self,
        agent: Any,
        skill_registry: SkillRegistry,
    ) -> list[str] | None:
        """Return the currently advertised Skill names, or ``None`` when degraded."""
        ...


class CompactionRunCoordinator:
    """Runs manual and automatic Compaction against its host loop's seam.

    Stateless besides its two injected fields: every Run-scoped fact travels
    through the arguments, and loop-owned capabilities are reached through the
    :class:`CompactionRunHost` protocol instead of a ChatLoop import.
    """

    def __init__(
        self,
        *,
        dependencies: ChatLoopDependencies,
        host: CompactionRunHost,
    ) -> None:
        self._dependencies = dependencies
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
        await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.record_run_kind,
            SessionAddress(
                project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
            ),
            run.run_kind,
        )
        adapter: Any | None = None
        summary_adapter: Any | None = None
        try:
            messages = await session.load_async()
            context_usage = latest_session_context_usage(messages)
            if context_usage is not None:
                run.terminal_payload_extras["context_usage"] = context_usage
            run.emit(
                COMPACTION_STARTED_EVENT,
                {"context_usage": context_usage} if context_usage is not None else {},
            )
            settings = await _CHAT_TRANSFORM_WORKERS.run(
                self._load_compaction_settings,
                agent,
                agent_id=run.agent_id,
                session_id=run.session_id,
                project_id=run.project_id,
            )
            try:
                project_cwd = self._host.resolve_project_cwd(run.working_project_id)
                provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
                adapter = self._dependencies.get_adapter(ConnectionRef(provider_id, connection_id))
                _model_provider_id, model_id = _split_agent_model(agent.model)
                summary_adapter, summary_model_id, summary_provider_id = (
                    self._host.resolve_summary_adapter(
                        agent,
                        adapter,
                        model_id,
                        settings,
                        active_provider_id=provider_id,
                    )
                )
                prompt_project = resolve_prompt_project(
                    self._dependencies.projects,
                    run.working_project_id,
                )
                prompt_context = (
                    ProjectPromptContext.from_project(
                        prompt_project.project_id,
                        prompt_project.display_name,
                        prompt_project.cwd,
                        prompt_project.auto_load,
                    )
                    if prompt_project is not None
                    else None
                )
                working_project_context = await _CHAT_TRANSFORM_WORKERS.run(
                    pinned_working_project_context,
                    self._dependencies,
                    run.agent_id,
                    run.session_id,
                    prompt_project,
                    prompt_context,
                    run.project_id,
                )
                soul_context = await _CHAT_TRANSFORM_WORKERS.run(
                    pinned_soul_context,
                    self._dependencies,
                    run.agent_id,
                    run.session_id,
                    agent,
                    run.project_id,
                )
                memory_files_context = await _CHAT_TRANSFORM_WORKERS.run(
                    pinned_memory_files,
                    self._dependencies,
                    run.agent_id,
                    run.session_id,
                    agent,
                    run.project_id,
                )
                skill_project_id, identity_agent_id = resolve_skill_scope(
                    run.project_id, prompt_project, run.agent_id
                )
                skill_registry = await _CHAT_TRANSFORM_WORKERS.run(
                    self._dependencies.resolve_skills,
                    skill_project_id,
                    identity_agent_id,
                )
                replay_policy = _resolve_reasoning_replay_policy(adapter, model_id)
                reasoning_scope_model = _resolved_model_reference(
                    self._dependencies,
                    provider_id,
                    connection_id,
                    model_id,
                )
                input_modalities = _model_input_modalities_for_target(
                    self._dependencies,
                    provider_id,
                    model_id,
                )
                wire_media_types = _resolve_wire_media_support(adapter, model_id)
                agent_body = runtime_agent_body(agent)
                skill_catalog = await _CHAT_TRANSFORM_WORKERS.run(
                    pinned_skill_catalog,
                    self._dependencies,
                    run.agent_id,
                    run.session_id,
                    agent,
                    skill_registry,
                    run.project_id,
                )
                inputs = RequestBuildInputs(
                    replay_policy=replay_policy,
                    reasoning_scope_model=reasoning_scope_model,
                    input_modalities=input_modalities,
                    wire_media_types=wire_media_types,
                    agent_body=agent_body,
                    project_context=prompt_context,
                    working_project_context=working_project_context,
                    soul_context=soul_context,
                    memory_files_context=memory_files_context,
                    agent_project_id=run.project_id,
                    skill_registry=skill_registry,
                    skill_catalog=skill_catalog,
                    session_messages_override=messages,
                )
                request_state = await self._host.build_request_state(agent, session, inputs=inputs)
                estimated_context_tokens_before, _ = await _CHAT_TRANSFORM_WORKERS.run(
                    estimate_request_input_tokens,
                    request_state.messages,
                    request_state.tools,
                )
                if context_usage is None:
                    context_usage = {
                        "tokens": estimated_context_tokens_before,
                        "estimated": True,
                    }
                    run.terminal_payload_extras["context_usage"] = context_usage
                context_tokens_before = int(context_usage["tokens"])
                checkpoint = await compaction_service.compact(
                    messages,
                    agent=agent,
                    summary_adapter=summary_adapter,
                    summary_model_id=summary_model_id,
                    storage=self._dependencies.storage,
                    settings=settings,
                    instruction=instruction,
                    request_messages=request_state.messages,
                    trigger=COMPACTION_TRIGGER_MANUAL,
                    active_adapter=adapter,
                    active_model_id=model_id,
                    active_tools=request_state.tools,
                    summary_temperature=resolve_request_temperature(
                        None,
                        self._dependencies.models,
                        summary_provider_id,
                        summary_model_id,
                    ),
                    active_temperature=resolve_request_temperature(
                        None,
                        self._dependencies.models,
                        provider_id,
                        model_id,
                    ),
                )
            finally:
                if adapter is not None:
                    await _close_adapter(adapter)
                if summary_adapter is not None and summary_adapter is not adapter:
                    await _close_adapter(summary_adapter)

            checkpoint = await _CHAT_TRANSFORM_WORKERS.run(
                _finalize_compaction_checkpoint,
                checkpoint,
                messages,
            )
            prompt_refresh: _CompactionPromptRefresh | None = None
            try:
                prompt_refresh = await _CHAT_TRANSFORM_WORKERS.run(
                    self._prepare_prompt_context_after_compaction,
                    agent_id=run.agent_id,
                    session_id=run.session_id,
                    agent=agent,
                    project_id=run.project_id,
                    working_project_id=run.working_project_id,
                    project_cwd=project_cwd,
                    activation_skill_project_id=skill_project_id,
                )
            except Exception:
                _LOGGER.warning(
                    "Prompt context refresh failed after manual Compaction (agent=%s session=%s)",
                    run.agent_id,
                    run.session_id,
                    exc_info=True,
                )
            inputs = inputs.merged_with_refresh(prompt_refresh)
            checkpoint, _ = await self._project_post_compaction_request(
                agent=agent,
                session=session,
                session_messages=messages,
                checkpoint=checkpoint,
                context_tokens_before=context_tokens_before,
                inputs=inputs.with_session_messages(messages),
            )
            await session.append_async(checkpoint)
            messages.append(checkpoint)
            await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.sessions.rotate_prompt_cache_affinity_id,
                SessionAddress(
                    project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
                ),
            )
            if prompt_refresh is not None:
                try:
                    await _CHAT_TRANSFORM_WORKERS.run(
                        self._commit_prompt_context_after_compaction,
                        agent_id=run.agent_id,
                        session_id=run.session_id,
                        project_id=run.project_id,
                        refresh=prompt_refresh,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Prompt context persistence failed after manual Compaction "
                        "(agent=%s session=%s)",
                        run.agent_id,
                        run.session_id,
                        exc_info=True,
                    )
            self._emit_compaction_completed(run, messages, checkpoint)
            run.terminal_payload_extras["session_usage"] = aggregate_session_usage(messages)
            return checkpoint
        except asyncio.CancelledError:
            run.emit(COMPACTION_ABORTED_EVENT, {"reason": "cancelled"})
            raise
        except Exception:
            run.emit(COMPACTION_ABORTED_EVENT, {"reason": "failed"})
            raise

    def _prepare_prompt_context_after_compaction(
        self,
        *,
        agent_id: str,
        session_id: str,
        agent: Any,
        project_id: str | None,
        working_project_id: str | None,
        project_cwd: Path | None,
        activation_skill_project_id: str | None,
    ) -> _CompactionPromptRefresh:
        """Prepare the next prompt epoch without committing Session metadata.

        Only prompt inputs are refreshed. The admitted Run keeps its resolved Agent,
        Model target, Tool policy, Project identity, and cwd; a freshly resolved
        Config Agent contributes only its prompt body.
        """
        prompt_project = resolve_prompt_project(self._dependencies.projects, working_project_id)
        project_prompt_context = (
            ProjectPromptContext.from_project(
                prompt_project.project_id,
                prompt_project.display_name,
                project_cwd if project_cwd is not None else prompt_project.cwd,
                prompt_project.auto_load,
            )
            if prompt_project is not None
            else None
        )
        prompt_skill_project_id, prompt_identity_agent_id = resolve_skill_scope(
            project_id,
            prompt_project,
            agent_id,
        )
        prompt_skill_registry = self._dependencies.refresh_skills(
            prompt_skill_project_id,
            prompt_identity_agent_id,
        )
        activation_identity_agent_id = agent_id if project_id is None else None
        if (
            activation_skill_project_id == prompt_skill_project_id
            and activation_identity_agent_id == prompt_identity_agent_id
        ):
            activation_skill_registry = prompt_skill_registry
        else:
            activation_skill_registry = self._dependencies.resolve_skills(
                activation_skill_project_id,
                activation_identity_agent_id,
            )

        system_prompts = self._dependencies.get_system_prompts()
        skill_catalog = system_prompts.render_skill_catalog(agent, prompt_skill_registry)
        refreshed_agent = self._dependencies.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_context: str | None = None
        soul_context: str | None = None
        memory_files_context: str | None = None
        read_paths: list[Path] = []
        if project_prompt_context is not None:
            # Rooted Identity Agents and Project Config Agents alike pin their
            # Working Project frame for the new prompt epoch.
            working_project_context = system_prompts.render_working_project_context(
                project_prompt_context,
                on_read=read_paths.append,
            )
        if getattr(refreshed_agent, "workspace", None):
            soul_read_paths: list[Path] = []
            soul_context = system_prompts.render_soul(
                refreshed_agent,
                on_read=soul_read_paths.append,
            )
            memory_files_context = system_prompts.render_memory_files(
                refreshed_agent,
                on_read=soul_read_paths.append,
            )
            read_paths.extend(soul_read_paths)

        available_skill_names = self._host.available_skill_names(agent, prompt_skill_registry)
        return _CompactionPromptRefresh(
            agent_body=runtime_agent_body(refreshed_agent),
            project_prompt_context=project_prompt_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            skill_registry=activation_skill_registry,
            skill_catalog=skill_catalog,
            prompt_read_paths=tuple(read_paths),
            available_skill_names=(
                tuple(available_skill_names) if available_skill_names is not None else None
            ),
        )

    def _commit_prompt_context_after_compaction(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        refresh: _CompactionPromptRefresh,
    ) -> None:
        """Persist one prepared prompt epoch after its checkpoint commit."""
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        metadata = self._dependencies.sessions.get_metadata(address)
        metadata[PINNED_SKILL_CATALOG_META_KEY] = {
            "catalog_text": refresh.skill_catalog.catalog_text
        }
        if refresh.available_skill_names is not None:
            metadata[SEEN_SKILLS_META_KEY] = list(refresh.available_skill_names)
        for pin_key, pin_text in (
            (PINNED_WORKING_PROJECT_CONTEXT_META_KEY, refresh.working_project_context),
            (PINNED_SOUL_CONTEXT_META_KEY, refresh.soul_context),
            (PINNED_MEMORY_FILES_META_KEY, refresh.memory_files_context),
        ):
            if pin_text is None:
                metadata.pop(pin_key, None)
            else:
                metadata[pin_key] = {"text": pin_text}
        stamp_prompt_files_read(
            self._dependencies.file_read_state,
            session_id,
            list(refresh.prompt_read_paths),
        )
        self._dependencies.sessions.set_metadata(address, metadata)

    async def _project_post_compaction_request(
        self,
        *,
        agent: Any,
        session: ChatSession,
        session_messages: list[ChatMessage],
        checkpoint: ChatMessage,
        context_tokens_before: int,
        inputs: RequestBuildInputs,
        live_request_messages: list[JsonObject] | None = None,
        continuation_reminder: str | None = None,
    ) -> tuple[ChatMessage, _RequestState]:
        """Build and count the exact request projection committed by Compaction."""
        projected_state = await self._host.build_request_state(
            agent,
            session,
            inputs=inputs.with_session_messages([*session_messages, checkpoint]),
        )
        projected_messages = projected_state.messages
        if continuation_reminder is not None:
            projected_messages = inject_continuation_reminder(
                projected_messages,
                continuation_reminder,
            )
        if live_request_messages is not None:
            projected_messages = _restore_in_run_tool_result_content(
                _restore_in_run_assistant_reasoning(
                    projected_messages,
                    live_request_messages,
                ),
                live_request_messages,
            )
        context_tokens_after, _ = await _CHAT_TRANSFORM_WORKERS.run(
            estimate_request_input_tokens,
            projected_messages,
            projected_state.tools,
        )
        stamped_checkpoint = checkpoint.with_compaction_context_tokens(
            context_tokens_before=context_tokens_before,
            context_tokens_after=context_tokens_after,
        )
        return stamped_checkpoint, _RequestState(
            projected_messages,
            projected_state.tools,
            projected_state.allowed_tool_names,
            projected_state.session_tool_grants,
            projected_state.tool_contracts,
        )

    async def _load_compaction_snapshot(
        self,
        run: Run,
        session: ChatSession,
    ) -> tuple[list[ChatMessage], SessionReadCursor]:
        """Load one complete Session snapshot without racing an append."""
        session_address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        async with self._dependencies.sessions.write_lock(session_address):
            snapshot = await session.load_since_async()
        if snapshot is None:
            raise AssertionError("A full Session snapshot must always produce a cursor")
        return list(snapshot.messages), snapshot.cursor

    async def _append_compaction_checkpoint_if_current(
        self,
        run: Run,
        session: ChatSession,
        checkpoint: ChatMessage,
        snapshot_cursor: SessionReadCursor,
    ) -> bool:
        """Append *checkpoint* only while its Session snapshot is still current."""
        session_address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        async with self._dependencies.sessions.write_lock(session_address):
            appended = await session.load_since_async(snapshot_cursor)
            if appended is None or appended.messages:
                return False
            await session.append_async(checkpoint)
            return True

    async def maybe_auto_compact_state(
        self,
        context: _RunExecutionContext,
        target: _ModelTarget,
        usage: JsonObject | None,
        *,
        continuation_request_messages: list[JsonObject] | None = None,
        context_usage: JsonObject | None = None,
        allow_continuation: bool = False,
        continue_same_run: bool = True,
    ) -> _RequestState:
        """Auto-compact when configured token thresholds are exceeded."""
        if context.request_state is None:
            raise AssertionError("Run request state must exist before Compaction")
        current_state = context.request_state
        run = context.run
        agent = context.agent
        session = context.session
        messages = current_state.messages
        tools = current_state.tools
        if self._host.compaction_service is None:
            return current_state

        settings = await _CHAT_TRANSFORM_WORKERS.run(
            self._load_compaction_settings,
            agent,
            agent_id=run.agent_id,
            session_id=run.session_id,
            project_id=run.project_id,
        )
        if not settings.auto:
            return current_state
        if settings.strategy == "continuation" and not allow_continuation:
            return current_state

        context_window = self._host.resolve_context_window(agent)
        if context_window is None:
            return current_state

        current_request_messages = continuation_request_messages or messages
        resolved_context_usage = context_usage
        if resolved_context_usage is None:
            resolved_context_usage = await _CHAT_TRANSFORM_WORKERS.run(
                build_model_step_context_usage,
                usage,
                current_request_messages,
            )
        context_tokens = resolved_context_usage.get("tokens")
        if isinstance(context_tokens, bool) or not isinstance(context_tokens, int):
            raise AssertionError("Context Usage must carry an integer token count")
        input_tokens = context_tokens
        run.terminal_payload_extras["context_usage"] = dict(resolved_context_usage)

        if settings.trigger == "context_ratio":
            should_compact = self._host.compaction_service.should_auto_compact(
                input_tokens,
                context_window,
                settings.threshold,
            )
        else:
            should_compact = self._host.compaction_service.should_auto_compact(
                input_tokens,
                context_window,
                settings.threshold,
                settings=settings,
            )
        if not should_compact:
            return current_state
        session_messages, snapshot_cursor = await self._load_compaction_snapshot(run, session)
        if settings.strategy == "summary_tail" and has_unconsumed_skill_activation(
            session_messages
        ):
            return current_state
        has_new_context = await _CHAT_TRANSFORM_WORKERS.run(
            self._host.compaction_service.has_new_compactable_context,
            session_messages,
            settings,
        )
        if not has_new_context:
            return current_state

        _LOGGER.info(
            "Auto-compaction triggered (run=%s agent=%s session=%s input_tokens=%d "
            "context_window=%d threshold=%s)",
            run.id,
            run.agent_id,
            run.session_id,
            input_tokens,
            context_window,
            settings.threshold,
        )
        summary_adapter, summary_model_id, summary_provider_id = self._host.resolve_summary_adapter(
            agent,
            target.adapter,
            target.model_id,
            settings,
            active_provider_id=target.provider_id,
        )
        close_summary_adapter = summary_adapter is not target.adapter
        run.emit(
            COMPACTION_STARTED_EVENT,
            {
                "context_tokens_before": input_tokens,
                "context_usage": dict(resolved_context_usage),
            },
        )
        try:
            checkpoint = await self._host.compaction_service.compact(
                session_messages,
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=self._dependencies.storage,
                settings=settings,
                request_messages=continuation_request_messages or messages,
                active_adapter=target.adapter,
                active_model_id=target.model_id,
                active_tools=tools,
                minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
                summary_temperature=resolve_request_temperature(
                    None,
                    self._dependencies.models,
                    summary_provider_id,
                    summary_model_id,
                ),
                active_temperature=resolve_request_temperature(
                    None,
                    self._dependencies.models,
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
                await _close_adapter(summary_adapter)

        checkpoint = await _CHAT_TRANSFORM_WORKERS.run(
            _finalize_compaction_checkpoint,
            checkpoint,
            session_messages,
        )
        prompt_refresh: _CompactionPromptRefresh | None = None
        try:
            try:
                prompt_refresh = await _CHAT_TRANSFORM_WORKERS.run(
                    self._prepare_prompt_context_after_compaction,
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

            projection_inputs = RequestBuildInputs.from_context(
                context, target
            ).merged_with_refresh(prompt_refresh)
            if (
                continue_same_run
                and context.continuation_reminder is not None
                and context.continuation_tracker is not None
            ):
                active_continuation = fold_continuation_records(
                    await session.load_continuation_records_async()
                )
                if active_continuation is not None:
                    context.continuation_reminder = render_continuation_reminder(
                        active_continuation,
                        context_window=self._host.resolve_context_window(agent),
                    )
            checkpoint, rebuilt_state = await self._project_post_compaction_request(
                agent=agent,
                session=session,
                session_messages=session_messages,
                checkpoint=checkpoint,
                context_tokens_before=input_tokens,
                inputs=projection_inputs.with_session_messages(session_messages),
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

        checkpoint_committed = await self._append_compaction_checkpoint_if_current(
            run,
            session,
            checkpoint,
            snapshot_cursor,
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
                    await context.session_snapshot.refresh(session)
                    refreshed_state = await self._host.build_request_state(
                        agent,
                        session,
                        inputs=RequestBuildInputs.from_context(
                            context, target
                        ).with_session_messages(context.session_snapshot.messages),
                    )
                    refreshed_messages = _restore_in_run_tool_result_content(
                        _restore_in_run_assistant_reasoning(
                            refreshed_state.messages,
                            messages,
                        ),
                        messages,
                    )
                    if context.continuation_reminder is not None:
                        refreshed_messages = inject_continuation_reminder(
                            refreshed_messages,
                            context.continuation_reminder,
                        )
                    return _RequestState(
                        refreshed_messages,
                        refreshed_state.tools,
                        refreshed_state.allowed_tool_names,
                        refreshed_state.session_tool_grants,
                        refreshed_state.tool_contracts,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Request rebuild after stale auto-compaction failed; continuing with "
                        "the existing request state",
                        exc_info=True,
                    )
            return current_state
        await context.session_snapshot.refresh(session)
        context.prompt_cache_affinity_id = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.sessions.rotate_prompt_cache_affinity_id,
            SessionAddress(
                project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
            ),
        )
        if prompt_refresh is not None:
            try:
                await _CHAT_TRANSFORM_WORKERS.run(
                    self._commit_prompt_context_after_compaction,
                    agent_id=run.agent_id,
                    session_id=run.session_id,
                    project_id=run.project_id,
                    refresh=prompt_refresh,
                )
            except Exception:
                _LOGGER.warning(
                    "Prompt context persistence failed after automatic Compaction "
                    "(run=%s agent=%s session=%s)",
                    run.id,
                    run.agent_id,
                    run.session_id,
                    exc_info=True,
                )
            context.agent_body = prompt_refresh.agent_body
            context.project_prompt_context = prompt_refresh.project_prompt_context
            context.working_project_context = prompt_refresh.working_project_context
            context.skill_registry = prompt_refresh.skill_registry
            context.skill_catalog = prompt_refresh.skill_catalog
        self._emit_compaction_completed(run, context.session_snapshot.messages, checkpoint)
        checkpoint_usage = checkpoint.usage or {}
        _LOGGER.info(
            "Auto-compaction completed (run=%s session=%s estimated_tokens_after=%d)",
            run.id,
            run.session_id,
            checkpoint_usage.get("context_tokens_after", 0),
        )
        return rebuilt_state

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
        metadata = self._dependencies.sessions.get_metadata(
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        )
        session_policy = metadata.get(COMPACTION_POLICY_META_KEY)
        agent_policy = getattr(agent, "compaction_policy", None)
        raw_settings = normalize_compaction_policy(
            session_policy
            if isinstance(session_policy, dict)
            else agent_policy
            if isinstance(agent_policy, dict)
            else self._dependencies.storage.load_compaction_settings(),
            use_defaults=True,
        )
        trigger = raw_settings["trigger"]
        strategy = raw_settings["strategy"]
        return CompactionSettings(
            auto=bool(raw_settings["enabled"]),
            trigger=str(trigger["type"]),
            threshold=float(trigger.get("threshold", 0.8)),
            trigger_tokens=int(trigger.get("tokens", 100_000)),
            strategy=str(strategy["type"]),
            tail_tokens=int(strategy.get("tail_tokens", 15_000)),
            summary_model=strategy.get("summary_model"),
        )
