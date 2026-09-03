"""Chat-owned request operations used by Compaction run coordination."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.chat.chat import (
    SEEN_SKILLS_META_KEY,
    ChatLoop,
    ChatLoopDependencies,
    RequestBuildInputs,
    RequestState,
    _CompactionPromptRefresh,
    _finalize_compaction_checkpoint,
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
from core.chat.messages import ChatMessage, JsonObject
from core.chat.model_resolution import (
    _model_input_modalities_for_target,
    _split_agent_model,
    resolve_request_temperature,
)
from core.chat.wire_shaping import _restore_in_run_assistant_reasoning
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
from core.providers.adapter import estimate_wire_request_input_tokens
from core.sessions import ChatSession, SessionAddress

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.compaction.compaction import CompactionSettings
    from core.runs import Run


@dataclass(frozen=True)
class ManualCompactionRequest:
    """Materialized Chat request and adapters for one manual Compaction."""

    project_cwd: Path | None
    activation_skill_project_id: str | None
    request_state: RequestState
    request_inputs: RequestBuildInputs
    active_adapter: Any
    active_provider_id: str
    active_model_id: str
    summary_adapter: Any
    summary_provider_id: str
    summary_model_id: str
    summary_temperature: float | None
    active_temperature: float | None


class ChatCompactionHost:
    """Complete Chat-side host for Compaction request operations.

    Compaction coordinates policy, Model work, checkpoint persistence, and Run
    events. This host owns every operation that needs Chat request internals so
    the coordinator never needs to import or reconstruct those details.
    """

    def __init__(self, loop: ChatLoop) -> None:
        self._loop = loop
        self._dependencies: ChatLoopDependencies = loop._dependencies

    @property
    def compaction_service(self) -> Any:
        return self._loop.compaction_service

    @property
    def sessions(self) -> Any:
        return self._dependencies.sessions

    @property
    def storage(self) -> Any:
        return self._dependencies.storage

    @property
    def models(self) -> Any:
        return self._dependencies.models

    async def run_transform(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        from core.chat.chat import _CHAT_TRANSFORM_WORKERS

        return await _CHAT_TRANSFORM_WORKERS.run(function, *args, **kwargs)

    async def record_run_kind(self, run: Run) -> None:
        await self.run_transform(
            self.sessions.record_run_kind,
            SessionAddress(
                project_id=run.project_id,
                agent_id=run.agent_id,
                session_id=run.session_id,
            ),
            run.run_kind,
        )

    def resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
        *,
        active_provider_id: str,
    ) -> tuple[Any, str, str]:
        return self._loop.resolve_summary_adapter(
            agent,
            adapter,
            model_id,
            settings,
            active_provider_id=active_provider_id,
        )

    def resolve_context_window(self, agent: Any, target: Any | None = None) -> int | None:
        return self._loop.resolve_context_window(agent, target)

    def resolve_temperature(self, provider_id: str, model_id: str) -> float | None:
        return resolve_request_temperature(None, self.models, provider_id, model_id)

    async def materialize_manual_request(
        self,
        run: Run,
        agent: Any,
        session: ChatSession,
        messages: list[ChatMessage],
        settings: CompactionSettings,
    ) -> ManualCompactionRequest:
        """Build the exact manual Compaction request and acquire its adapters."""
        project_cwd = self._loop.resolve_project_cwd(run.working_project_id)
        provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
        adapter = self._dependencies.get_adapter(ConnectionRef(provider_id, connection_id))
        _model_provider_id, model_id = _split_agent_model(agent.model)
        summary_adapter: Any | None = None
        try:
            summary_adapter, summary_model_id, summary_provider_id = self.resolve_summary_adapter(
                agent,
                adapter,
                model_id,
                settings,
                active_provider_id=provider_id,
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
            working_project_context = await self.run_transform(
                pinned_working_project_context,
                self._dependencies,
                run.agent_id,
                run.session_id,
                prompt_project,
                prompt_context,
                run.project_id,
            )
            soul_context = await self.run_transform(
                pinned_soul_context,
                self._dependencies,
                run.agent_id,
                run.session_id,
                agent,
                run.project_id,
            )
            memory_files_context = await self.run_transform(
                pinned_memory_files,
                self._dependencies,
                run.agent_id,
                run.session_id,
                agent,
                run.project_id,
            )
            skill_project_id, identity_agent_id = resolve_skill_scope(
                run.project_id,
                prompt_project,
                run.agent_id,
            )
            skill_registry = await self.run_transform(
                self._dependencies.resolve_skills,
                skill_project_id,
                identity_agent_id,
            )
            skill_catalog = await self.run_transform(
                pinned_skill_catalog,
                self._dependencies,
                run.agent_id,
                run.session_id,
                agent,
                skill_registry,
                run.project_id,
            )
            inputs = RequestBuildInputs(
                replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
                reasoning_scope_model=_resolved_model_reference(
                    self._dependencies,
                    provider_id,
                    connection_id,
                    model_id,
                ),
                input_modalities=_model_input_modalities_for_target(
                    self._dependencies,
                    provider_id,
                    model_id,
                ),
                wire_media_types=_resolve_wire_media_support(adapter, model_id),
                agent_body=runtime_agent_body(agent),
                project_context=prompt_context,
                working_project_context=working_project_context,
                soul_context=soul_context,
                memory_files_context=memory_files_context,
                agent_project_id=run.project_id,
                skill_registry=skill_registry,
                skill_catalog=skill_catalog,
                session_messages_override=messages,
            )
            state = await self._loop.build_request_state(agent, session, inputs=inputs)
            return ManualCompactionRequest(
                project_cwd=project_cwd,
                activation_skill_project_id=skill_project_id,
                request_state=state,
                request_inputs=inputs,
                active_adapter=adapter,
                active_provider_id=provider_id,
                active_model_id=model_id,
                summary_adapter=summary_adapter,
                summary_provider_id=summary_provider_id,
                summary_model_id=summary_model_id,
                summary_temperature=resolve_request_temperature(
                    None,
                    self.models,
                    summary_provider_id,
                    summary_model_id,
                ),
                active_temperature=resolve_request_temperature(
                    None,
                    self.models,
                    provider_id,
                    model_id,
                ),
            )
        except BaseException:
            if summary_adapter is not None and summary_adapter is not adapter:
                await _close_adapter(summary_adapter)
            await _close_adapter(adapter)
            raise

    async def close_manual_request(self, request: ManualCompactionRequest) -> None:
        await _close_adapter(request.active_adapter)
        if request.summary_adapter is not request.active_adapter:
            await _close_adapter(request.summary_adapter)

    async def close_adapter(self, adapter: Any) -> None:
        await _close_adapter(adapter)

    async def finalize_checkpoint(
        self,
        checkpoint: ChatMessage,
        session_messages: list[ChatMessage],
    ) -> ChatMessage:
        return cast(
            ChatMessage,
            await self.run_transform(
                _finalize_compaction_checkpoint,
                checkpoint,
                session_messages,
            ),
        )

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
    ) -> _CompactionPromptRefresh:
        return cast(
            _CompactionPromptRefresh,
            await self.run_transform(
                self._prepare_prompt_refresh_sync,
                agent_id=agent_id,
                session_id=session_id,
                agent=agent,
                project_id=project_id,
                working_project_id=working_project_id,
                project_cwd=project_cwd,
                activation_skill_project_id=activation_skill_project_id,
            ),
        )

    def _prepare_prompt_refresh_sync(
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
            working_project_context = system_prompts.render_working_project_context(
                project_prompt_context,
                on_read=read_paths.append,
            )
        if getattr(refreshed_agent, "workspace", None):
            identity_read_paths: list[Path] = []
            soul_context = system_prompts.render_soul(
                refreshed_agent,
                on_read=identity_read_paths.append,
            )
            memory_files_context = system_prompts.render_memory_files(
                refreshed_agent,
                on_read=identity_read_paths.append,
            )
            read_paths.extend(identity_read_paths)

        available_skill_names = self._loop.available_skill_names(agent, prompt_skill_registry)
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

    async def commit_prompt_refresh(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        refresh: object,
    ) -> None:
        typed_refresh = cast(_CompactionPromptRefresh, refresh)
        await self.run_transform(
            self._commit_prompt_refresh_sync,
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            refresh=typed_refresh,
        )

    def _commit_prompt_refresh_sync(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        refresh: _CompactionPromptRefresh,
    ) -> None:
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)

        def update(metadata: JsonObject) -> None:
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

        self.sessions.mutate_metadata(address, update)
        stamp_prompt_files_read(
            self._dependencies.file_read_state,
            session_id,
            list(refresh.prompt_read_paths),
        )

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
    ) -> tuple[ChatMessage, RequestState]:
        inputs = cast(RequestBuildInputs, request_inputs).merged_with_refresh(
            cast(_CompactionPromptRefresh | None, prompt_refresh)
        )
        projected_state = await self._loop.build_request_state(
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
        context_tokens_after = await self.run_transform(
            estimate_wire_request_input_tokens,
            active_adapter,
            projected_messages,
            model_id=active_model_id,
            tools=projected_state.tools,
        )
        stamped_checkpoint = checkpoint.with_compaction_context_tokens(
            context_tokens_before=context_tokens_before,
            context_tokens_after=context_tokens_after,
        )
        return stamped_checkpoint, RequestState(
            projected_messages,
            projected_state.tools,
            projected_state.allowed_tool_names,
            projected_state.session_tool_grants,
            projected_state.tool_contracts,
        )

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
    ) -> tuple[ChatMessage, RequestState]:
        return await self.project_post_compaction_request(
            agent=context.agent,
            session=context.session,
            session_messages=session_messages,
            checkpoint=checkpoint,
            context_tokens_before=context_tokens_before,
            request_inputs=RequestBuildInputs.from_context(context, target),
            prompt_refresh=prompt_refresh,
            active_adapter=target.adapter,
            active_model_id=target.model_id,
            live_request_messages=live_request_messages,
            continuation_reminder=continuation_reminder,
        )

    async def refresh_continuation_reminder(
        self,
        context: Any,
        *,
        context_window: int | None,
    ) -> None:
        if context.continuation_reminder is None or context.continuation_tracker is None:
            return
        active_continuation = fold_continuation_records(
            await context.session.load_continuation_records_async()
        )
        if active_continuation is not None:
            context.continuation_reminder = render_continuation_reminder(
                active_continuation,
                context_window=context_window,
            )

    async def rebuild_after_stale_compaction(
        self,
        context: Any,
        target: Any,
        live_request_messages: list[JsonObject],
    ) -> RequestState:
        await context.session_snapshot.refresh(context.session)
        refreshed_state = await self._loop.build_request_state(
            context.agent,
            context.session,
            inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
                context.session_snapshot.active_messages
            ),
        )
        refreshed_messages = _restore_in_run_tool_result_content(
            _restore_in_run_assistant_reasoning(
                refreshed_state.messages,
                live_request_messages,
            ),
            live_request_messages,
        )
        if context.continuation_reminder is not None:
            refreshed_messages = inject_continuation_reminder(
                refreshed_messages,
                context.continuation_reminder,
            )
        return RequestState(
            refreshed_messages,
            refreshed_state.tools,
            refreshed_state.allowed_tool_names,
            refreshed_state.session_tool_grants,
            refreshed_state.tool_contracts,
        )

    async def rotate_prompt_cache_affinity(self, run: Run) -> str:
        return cast(
            str,
            await self.run_transform(
                self.sessions.rotate_prompt_cache_affinity_id,
                SessionAddress(
                    project_id=run.project_id,
                    agent_id=run.agent_id,
                    session_id=run.session_id,
                ),
            ),
        )

    @staticmethod
    def apply_prompt_refresh(context: Any, refresh: object) -> None:
        typed_refresh = cast(_CompactionPromptRefresh, refresh)
        context.agent_body = typed_refresh.agent_body
        context.project_prompt_context = typed_refresh.project_prompt_context
        context.working_project_context = typed_refresh.working_project_context
        context.skill_registry = typed_refresh.skill_registry
        context.skill_catalog = typed_refresh.skill_catalog
