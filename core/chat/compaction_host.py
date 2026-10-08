"""Chat-owned request operations used by Compaction run coordination."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.agents import skill_subject_id
from core.chat._boundaries import _finish_visible_boundary
from core.chat._request_builder import (
    RequestBuilder,
    _finalize_compaction_checkpoint,
    _resolve_request_image_limit,
    _resolved_model_reference,
)
from core.chat._run_state import (
    ChatLoopDependencies,
    RequestBuildInputs,
    RequestState,
    _CompactionPromptRefresh,
    is_subagent_session,
)
from core.chat.events import _close_adapter
from core.chat.messages import ChatMessage, JsonObject
from core.chat.model_resolution import (
    _model_input_modalities_for_target,
    _resolve_agent_connection,
    _split_agent_model,
)
from core.chat.usage import ContextRoute, RequestContextUsage
from core.chat.wire_shaping import PINNED_IMAGE_RETIREMENT_SLOT, RequestImageBudget
from core.memory import DEFAULT_MEMORY_PROMPT_MODE
from core.projects import resolve_prompt_project, resolve_skill_scope, runtime_agent_body
from core.prompts import ProjectPromptContext
from core.prompts.pinned_context import (
    PINNED_DYNAMIC_BLOCKS_SLOT,
    PINNED_TOOL_DEFINITIONS_SLOT,
    pinned_agent_body,
    pinned_memory_files,
    pinned_skill_catalog,
    pinned_soul_context,
    pinned_working_project_context,
    pins_agent_body,
    prompt_epoch_pins,
    stamp_prompt_files_read,
)
from core.providers.accounts import ConnectionRef
from core.sessions import ChatSession, PromptEpoch

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.compaction.compaction import CompactionService, CompactionSettings
    from core.runs import Run
    from core.sessions import SessionReadCursor


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
    # The route the manual Compaction measures the Session's Context for.
    active_target: ContextRoute
    summary_adapter: Any
    summary_provider_id: str
    summary_model_id: str


class ChatCompactionHost:
    """Complete Chat-side host for Compaction request operations.

    Compaction coordinates policy, Model work, checkpoint persistence, and Run
    events. This host owns every operation that needs Chat request internals so
    the coordinator never needs to import or reconstruct those details.
    """

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        requests: RequestBuilder,
        compaction_service: CompactionService | None,
    ) -> None:
        self._requests = requests
        self._dependencies = dependencies
        self._compaction_service = compaction_service

    @property
    def compaction_service(self) -> Any:
        return self._compaction_service

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
        from core.chat._workers import (
            _CHAT_TRANSFORM_WORKERS,
        )

        return await _CHAT_TRANSFORM_WORKERS.run(function, *args, **kwargs)

    def resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
        *,
        active_provider_id: str,
        active_connection_id: str,
    ) -> tuple[Any, str, str]:
        return self._requests.resolve_summary_adapter(
            agent,
            adapter,
            model_id,
            settings,
            active_provider_id=active_provider_id,
            active_connection_id=active_connection_id,
        )

    def resolve_context_window(self, agent: Any, target: Any) -> int | None:
        return self._requests.resolve_context_window(agent, target)

    def context_accounting(self, messages: list[ChatMessage]) -> RequestContextUsage:
        """The Session's Context accounting, continuing its newest measurement."""
        return RequestContextUsage.resume(messages, self._dependencies.usage_recorder)

    async def materialize_manual_request(
        self,
        run: Run,
        agent: Any,
        session: ChatSession,
        messages: list[ChatMessage],
        settings: CompactionSettings,
    ) -> ManualCompactionRequest:
        """Build the exact manual Compaction request and acquire its adapters."""
        project_cwd = self._requests.resolve_project_cwd(run.working_project_id)
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
                active_connection_id=connection_id,
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
                agent,
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
            agent_body = await self.run_transform(
                pinned_agent_body,
                self._dependencies,
                run.agent_id,
                run.session_id,
                agent,
                run.project_id,
                runtime_agent_body(agent),
            )
            skill_project_id, identity_agent_id = resolve_skill_scope(
                run.project_id,
                prompt_project,
                agent,
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
                skill_project_id=skill_project_id,
            )
            model_reference = _resolved_model_reference(
                self._dependencies,
                provider_id,
                connection_id,
                model_id,
            )
            inputs = RequestBuildInputs(
                replay_policy=adapter.reasoning_replay_policy(model_id),
                reasoning_scope_model=model_reference,
                input_modalities=_model_input_modalities_for_target(
                    self._dependencies,
                    provider_id,
                    model_id,
                ),
                wire_media_types=adapter.wire_media_support(model_id),
                max_image_bytes=self._requests._image_size_limit(adapter, model_id),
                max_request_images=_resolve_request_image_limit(adapter, model_id),
                agent_body=agent_body,
                project_context=prompt_context,
                working_project_context=working_project_context,
                soul_context=soul_context,
                memory_files_context=memory_files_context,
                agent_project_id=run.project_id,
                skill_registry=skill_registry,
                skill_catalog=skill_catalog,
                session_messages_override=messages,
                subagent_session=await self.run_transform(
                    is_subagent_session, self._dependencies.sessions, session.address
                ),
                list_announced_tools=adapter.list_announced_tools(model_id),
            )
            state = await self._requests.build_request_state(agent, session, inputs=inputs)
            return ManualCompactionRequest(
                project_cwd=project_cwd,
                activation_skill_project_id=skill_project_id,
                request_state=state,
                request_inputs=inputs,
                active_adapter=adapter,
                active_provider_id=provider_id,
                active_model_id=model_id,
                active_target=ContextRoute(adapter, model_id, model_reference),
                summary_adapter=summary_adapter,
                summary_provider_id=summary_provider_id,
                summary_model_id=summary_model_id,
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
        from core.agents.temporary import TemporaryAgent

        is_temporary = isinstance(agent, TemporaryAgent)
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
            agent,
        )
        if is_temporary:
            prompt_skill_project_id, prompt_identity_agent_id = working_project_id, None
        prompt_skill_registry = self._dependencies.refresh_skills(
            prompt_skill_project_id,
            prompt_identity_agent_id,
        )
        activation_identity_agent_id = (
            skill_subject_id(agent) if project_id is None and not is_temporary else None
        )
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
        # Temporary configuration is the immutable, already-admitted Session binding.
        # It has no Identity/Team record to reload, including in delegated child Runs.
        refreshed_agent = (
            agent
            if is_temporary
            else self._dependencies.agent_resolver.resolve_agent(
                project_id, agent_id, session_id=session_id
            )
        )
        working_project_context: str | None = None
        soul_context: str | None = None
        memory_files_context: str | None = None
        read_paths: list[Path] = []
        if project_prompt_context is not None:
            working_project_context = system_prompts.render_working_project_context(
                refreshed_agent,
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

        available_skill_names = self._requests.available_skill_names(agent, prompt_skill_registry)
        return _CompactionPromptRefresh(
            agent_body=runtime_agent_body(refreshed_agent),
            project_prompt_context=project_prompt_context,
            working_project_context=working_project_context,
            soul_context=soul_context,
            memory_files_context=memory_files_context,
            skill_registry=activation_skill_registry,
            skill_catalog=skill_catalog,
            skill_project_id=prompt_skill_project_id,
            prompt_read_paths=tuple(read_paths),
            available_skill_names=tuple(available_skill_names),
            memory_prompt_mode=getattr(
                refreshed_agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE
            ),
            pins_agent_body=pins_agent_body(refreshed_agent),
        )

    async def commit_checkpoint(
        self,
        run: Run,
        session: ChatSession,
        checkpoint: ChatMessage,
        *,
        since: SessionReadCursor,
        prompt_refresh: object | None,
        request_state: RequestState,
    ) -> bool:
        """Commit a manual *checkpoint* and its prompt epoch while *since* is current.

        *request_state* is the projected post-Compaction request; its Tool pin
        starts the new epoch. A Stop of *run* while the commit runs waits for
        its outcome, so a stored checkpoint is never reported as cancelled.
        """
        return await _finish_visible_boundary(
            self._commit_manual_checkpoint(
                session,
                checkpoint,
                since=since,
                prompt_refresh=prompt_refresh,
                request_state=request_state,
            ),
            run,
            True,
        )

    async def _commit_manual_checkpoint(
        self,
        session: ChatSession,
        checkpoint: ChatMessage,
        *,
        since: SessionReadCursor,
        prompt_refresh: object | None,
        request_state: RequestState,
    ) -> bool:
        refresh = cast(_CompactionPromptRefresh | None, prompt_refresh)
        async with self.sessions.write_lock(session.address):
            committed = await session.commit_compaction_async(
                checkpoint, since=since, epoch=_prompt_epoch(refresh, request_state)
            )
        if committed is None:
            return False
        await self._stamp_prompt_files_read(session.id, refresh)
        return True

    async def commit_automatic_checkpoint(
        self,
        context: Any,
        checkpoint: ChatMessage,
        *,
        prompt_refresh: object | None,
        request_state: RequestState,
    ) -> bool:
        """Commit an automatic *checkpoint* against the Run snapshot and adopt its epoch.

        The Run snapshot advances past the checkpoint from the same transaction,
        and the Run continues with the refreshed prompt inputs and *request_state*,
        the projected request whose Tool pin starts the new epoch. The prompt-cache
        affinity id stays the Session's.
        """
        refresh = cast(_CompactionPromptRefresh | None, prompt_refresh)
        session = context.session
        async with self.sessions.write_lock(session.address):
            committed = await context.session_snapshot.commit_checkpoint(
                session, checkpoint, epoch=_prompt_epoch(refresh, request_state)
            )
        if not committed:
            return False
        context.image_budget = RequestImageBudget()
        if refresh is not None:
            await self._stamp_prompt_files_read(session.id, refresh)
            self.apply_prompt_refresh(context, refresh)
        return True

    async def _stamp_prompt_files_read(
        self, session_id: str, refresh: _CompactionPromptRefresh | None
    ) -> None:
        if refresh is None or not refresh.prompt_read_paths:
            return
        await self.run_transform(
            stamp_prompt_files_read,
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
        active_target: Any,
        accounting: RequestContextUsage,
        live_request_messages: list[JsonObject] | None = None,
    ) -> tuple[ChatMessage, RequestState]:
        inputs = cast(RequestBuildInputs, request_inputs).merged_with_refresh(
            cast(_CompactionPromptRefresh | None, prompt_refresh)
        )
        # The new epoch lists every Tool offered now and shows every dynamic
        # block as it renders now; the commit persists both pins.
        projected_state = await self._requests.rebuild_live_request_state(
            agent,
            session,
            inputs=replace(inputs, fresh_prompt_epoch=True).with_session_messages(
                [*session_messages, checkpoint]
            ),
            live_messages=live_request_messages,
        )
        # The new prompt epoch has no measurement yet: a whole corrected estimate.
        context_tokens_after = await self.run_transform(
            accounting.estimate,
            projected_state.messages,
            target=active_target,
            tools=projected_state.tools,
        )
        stamped_checkpoint = checkpoint.with_compaction_context_tokens(
            context_tokens_before=context_tokens_before,
            context_tokens_after=context_tokens_after,
            estimate_factor=accounting.factor(active_target),
        )
        return stamped_checkpoint, projected_state

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
    ) -> tuple[ChatMessage, RequestState]:
        return await self.project_post_compaction_request(
            agent=context.agent,
            session=context.session,
            session_messages=session_messages,
            checkpoint=checkpoint,
            context_tokens_before=context_tokens_before,
            request_inputs=RequestBuildInputs.from_context(context, target),
            prompt_refresh=prompt_refresh,
            active_target=target,
            accounting=context.context_usage,
            live_request_messages=live_request_messages,
        )

    async def rebuild_after_stale_compaction(
        self,
        context: Any,
        target: Any,
        live_request_messages: list[JsonObject],
    ) -> RequestState:
        await context.session_snapshot.refresh(context.session)
        return await self._requests.rebuild_live_request_state(
            context.agent,
            context.session,
            inputs=RequestBuildInputs.from_context(context, target).with_session_messages(
                context.session_snapshot.active_messages
            ),
            live_messages=live_request_messages,
        )

    @staticmethod
    def apply_prompt_refresh(context: Any, refresh: object) -> None:
        typed_refresh = cast(_CompactionPromptRefresh, refresh)
        context.agent_body = typed_refresh.agent_body
        context.project_prompt_context = typed_refresh.project_prompt_context
        context.working_project_context = typed_refresh.working_project_context
        context.soul_context = typed_refresh.soul_context
        context.memory_files_context = typed_refresh.memory_files_context
        context.skill_registry = typed_refresh.skill_registry
        context.skill_catalog = typed_refresh.skill_catalog


def _prompt_epoch(
    refresh: _CompactionPromptRefresh | None, request_state: RequestState
) -> PromptEpoch:
    """The prompt epoch a committed checkpoint starts: fresh pins and seen Skills.

    The Tool pin and the dynamic block pin of the projected request always start
    the new epoch, even when the rest of the prompt refresh failed: the
    checkpoint drops the notes that announced their changes, so the old pins
    would lose them.
    """
    tool_epoch = request_state.tool_epoch
    prompt_blocks = request_state.prompt_blocks
    request_pins = {
        PINNED_TOOL_DEFINITIONS_SLOT: (
            tool_epoch.pin.to_payload() if tool_epoch is not None else None
        ),
        PINNED_DYNAMIC_BLOCKS_SLOT: (
            prompt_blocks.to_payload() if prompt_blocks is not None else None
        ),
        # Compacted history keeps every remaining image until a limit retires it anew.
        PINNED_IMAGE_RETIREMENT_SLOT: None,
    }
    if refresh is None:
        return PromptEpoch(pins=request_pins)
    return PromptEpoch(
        pins={
            **request_pins,
            **prompt_epoch_pins(
                skill_catalog=refresh.skill_catalog,
                skill_project_id=refresh.skill_project_id,
                working_project_context=refresh.working_project_context,
                working_project_id=(
                    refresh.project_prompt_context.project_id
                    if refresh.project_prompt_context is not None
                    else None
                ),
                soul_context=refresh.soul_context,
                memory_files_context=refresh.memory_files_context,
                memory_prompt_mode=refresh.memory_prompt_mode,
                agent_body=refresh.agent_body if refresh.pins_agent_body else None,
            ),
        },
        seen_skills=refresh.available_skill_names,
    )
