"""Pinned prompt, history, media and Model-route request construction."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.attachments.images import ImageConverter
from core.chat._message_history import (
    finalize_checkpoint_history_guidance,
    history_available,
)
from core.chat._request_history import _prepare_request_messages, _request_content_resolution_inputs
from core.chat._run_state import RequestBuildInputs, _ModelTarget, _RequestState
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import MediaBlock, content_block_to_dict
from core.chat.errors import ChatError
from core.chat.events import _close_adapter
from core.chat.messages import (
    ChatMessage,
    JsonObject,
)
from core.chat.model_resolution import (
    _first_usable_connection_id,
    _model_connection_allowlist,
    _model_input_modalities,
    _model_input_modalities_for_target,
    _resolve_agent_connection,
    _split_agent_model,
    parse_bare_model,
    parse_model_with_connection,
)
from core.chat.usage import latest_session_context_usage
from core.chat.wire_shaping import limit_request_images
from core.extensions import invoke_extension_handler
from core.projects import ProjectError
from core.prompts import BLOCK_KIND_DATA, BlockDefinition, PinnedSkillCatalog, ProjectPromptContext
from core.prompts.pinned_context import stamp_prompt_files_read
from core.providers.accounts import DEFAULT_ACCOUNT_ID, ConnectionRef, split_connection_id
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.providers import resolve_effective_context_window
from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_POLICY,
    ReasoningReplayPolicy,
)
from core.runs import Run
from core.sessions import (
    SKILL_AVAILABLE_NOTE_PREFIX,
    ChatSession,
    SessionAddress,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    HISTORY_TOOL_NAME,
    ToolAccess,
    project_bash_tool_definitions,
)
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies, _RunExecutionContext
    from core.chat.request_runner import WireRequestRunner
    from core.skills.skills import SkillRegistry

_LOGGER = get_logger("chat")


async def _run_prompt_method(
    manager: Any,
    async_name: str,
    sync_name: str,
    *arguments: Any,
    **keyword_arguments: Any,
) -> Any:
    """Prefer a prompt-owned async boundary and preserve sync test doubles."""
    async_method = getattr(manager, async_name, None)
    if callable(async_method) and inspect.iscoroutinefunction(async_method):
        return await async_method(*arguments, **keyword_arguments)
    return await _CHAT_TRANSFORM_WORKERS.run(
        getattr(manager, sync_name),
        *arguments,
        **keyword_arguments,
    )


def _finalize_compaction_checkpoint(
    checkpoint: ChatMessage,
    session_messages: list[ChatMessage],
) -> ChatMessage:
    ordinal = sum(message.role == "compaction_checkpoint" for message in session_messages) + 1
    return finalize_checkpoint_history_guidance(checkpoint, ordinal=ordinal)


def _resolve_reasoning_replay_policy(adapter: Any, model_id: str) -> ReasoningReplayPolicy:
    """Resolve the adapter's reasoning replay policy for one request build.

    Mirrors the ``set_debug_context`` probe: adapters and test doubles that do
    not expose the hook receive the system ``full_history`` default.
    """
    if hasattr(adapter, "reasoning_replay_policy"):
        return cast(ReasoningReplayPolicy, adapter.reasoning_replay_policy(model_id))
    return DEFAULT_REASONING_REPLAY_POLICY


def _resolve_wire_media_support(adapter: Any, model_id: str) -> frozenset[str]:
    """Resolve the media types the adapter's wire can carry for one request build.

    Mirrors ``_resolve_reasoning_replay_policy``: adapters and test doubles that
    do not expose the hook carry nothing, so the resolver degrades every
    attachment rather than emitting media the wire cannot encode.
    """
    if hasattr(adapter, "wire_media_support"):
        return frozenset(adapter.wire_media_support(model_id))
    return frozenset()


def _resolved_model_reference(
    dependencies: ChatLoopDependencies,
    provider_id: str,
    connection_id: str,
    model_id: str,
) -> str:
    """Return the exact Provider/Model/Connection/Account scope for one request."""

    local_connection_id, explicit_account_id = split_connection_id(provider_id, connection_id)
    resolved_account_id = dependencies.provider_credentials.resolve_account_id(
        provider_id,
        local_connection_id,
        explicit_account_id,
    )
    connection_suffix = local_connection_id
    if resolved_account_id != DEFAULT_ACCOUNT_ID:
        connection_suffix = f"{connection_suffix}:{resolved_account_id}"
    bare_reference = f"{provider_id}/{model_id}"
    return f"{bare_reference}::{connection_suffix}"


SEEN_SKILLS_META_KEY = "seen_skills"


SKILL_AVAILABLE_NEW_SKILLS_HEADER = (
    "New skills are now available to you. Load one by name with the `skill` tool when relevant:"
)


class RequestBuilder:
    """Build Chat requests from canonical history, pinned prompts and Model routes."""

    def __init__(
        self,
        dependencies: ChatLoopDependencies,
        wire_requests: WireRequestRunner,
        attachment_resolver: ContentBlockResolver | None,
    ) -> None:
        self._dependencies = dependencies
        self._wire_requests = wire_requests
        self._attachment_resolver = attachment_resolver
        self._tool_image_converter = ImageConverter()
        self.nesting_depth = 0

    async def _apply_project_skill_context(
        self,
        context: _RunExecutionContext,
        project_id: str,
    ) -> None:
        """Make one explicitly loaded Project's Skills active for an Identity Run.

        The Project Tool Result remains the sole persisted context carrier. This
        updates only the Run-local Skill resolver used by activation and Tool
        dispatch; the current prompt-epoch Skill catalog and therefore the System Prompt
        remain unchanged.
        """
        try:
            registry = await _CHAT_TRANSFORM_WORKERS.run(
                self._dependencies.resolve_skills,
                project_id,
                context.run.agent_id,
            )
        except (ProjectError, OSError) as error:
            _LOGGER.warning(
                "Loaded Project Skill context is unavailable (agent=%s session=%s project=%s): %s",
                context.run.agent_id,
                context.run.session_id,
                project_id,
                error,
            )
            return
        context.skill_project_id = project_id
        context.skill_registry = registry

    def _create_model_target(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
    ) -> _ModelTarget:
        connection = ConnectionRef(provider_id, connection_id)
        adapter = self._dependencies.get_adapter(connection)
        return _ModelTarget(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=model_id,
            model_reference=_resolved_model_reference(
                self._dependencies,
                provider_id,
                connection_id,
                model_id,
            ),
            adapter=adapter,
            replay_policy=_resolve_reasoning_replay_policy(adapter, model_id),
            input_modalities=_model_input_modalities_for_target(
                self._dependencies,
                provider_id,
                model_id,
            ),
            wire_media_types=_resolve_wire_media_support(adapter, model_id),
            chunk_timeout_seconds=self._wire_requests.resolve_chunk_timeout(connection),
        )

    def resolve_project_cwd(self, project_id: str | None) -> Path | None:
        """Resolve a working Project cwd, failing closed when unavailable."""
        if project_id is None:
            return None
        cwd = Path(self._dependencies.projects.get(project_id).cwd)
        if not cwd.is_dir():
            raise ChatError(f"Project repository is unavailable: {cwd}")
        return cwd

    @staticmethod
    def available_skill_names(agent: Any, skill_registry: SkillRegistry) -> list[str] | None:
        """Return the currently advertised Skill names, or ``None`` for a degraded registry."""
        filter_allowed = getattr(skill_registry, "filter_allowed", None)
        if not callable(filter_allowed):
            return None
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        return sorted(str(skill.name) for skill in filter_allowed(allowed))

    def _announce_newly_available_skills(
        self,
        agent_id: str,
        session_id: str,
        session: ChatSession,
        agent: Any,
        skill_registry: SkillRegistry,
        project_id: str | None,
    ) -> None:
        """Tell the model about Skills that became available during this prompt epoch.

        The Session's ``<available_skills>`` block stays pinned between Compactions,
        so a Skill that becomes available mid-epoch does not change the prompt. This
        appends a one-time ``<system-reminder>`` note for each newly available+allowed
        Skill, leaving the cached prefix untouched. Additions only — a Skill that
        becomes unavailable is not announced. The first Run and every successful
        Compaction seed the baseline from the catalog without announcing it. The diff
        uses the registry already resolved for this Run, so it is an in-memory set
        comparison rather than another scan.
        """
        # Minimal/degraded skill registries (e.g. some test doubles) may not expose
        # ``filter_allowed``; the announcement is an optional enhancement, so skip it
        # cleanly rather than break the run — the real ``SkillRegistry`` always has it.
        available_names = self.available_skill_names(agent, skill_registry)
        if available_names is None:
            return
        allowed_skills = getattr(agent, "allowed_skills", None)
        allowed = ["*"] if allowed_skills is None else allowed_skills
        available = {
            str(skill.name): str(skill.description)
            for skill in skill_registry.filter_allowed(allowed)
        }
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        new_names: list[str] = []

        def update(metadata: JsonObject) -> None:
            nonlocal new_names
            seen = metadata.get(SEEN_SKILLS_META_KEY)
            if not isinstance(seen, list):
                metadata[SEEN_SKILLS_META_KEY] = available_names
                return
            new_names = sorted(set(available) - set(seen))
            if new_names:
                metadata[SEEN_SKILLS_META_KEY] = sorted(set(seen) | set(new_names))

        self._dependencies.sessions.mutate_metadata(address, update)
        if not new_names:
            return
        lines = [SKILL_AVAILABLE_NEW_SKILLS_HEADER]
        lines.extend(f"- {name}: {available[name]}" for name in new_names)
        session.add_note(SKILL_AVAILABLE_NOTE_PREFIX + "\n".join(lines))

    async def _build_request_messages(
        self,
        agent: Any,
        session: ChatSession,
        *,
        replay_policy: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY,
        reasoning_scope_model: str | None = None,
        input_modalities: frozenset[str] | None = None,
        wire_media_types: frozenset[str] = frozenset(),
        agent_body: str = "",
        project_context: ProjectPromptContext | None = None,
        working_project_context: str | None = None,
        agent_project_id: str | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_catalog: PinnedSkillCatalog | None = None,
    ) -> list[JsonObject]:
        state = await self.build_request_state(
            agent,
            session,
            inputs=RequestBuildInputs(
                replay_policy=replay_policy,
                reasoning_scope_model=reasoning_scope_model,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
                agent_body=agent_body,
                project_context=project_context,
                working_project_context=working_project_context,
                agent_project_id=agent_project_id,
                skill_registry=skill_registry,
                skill_catalog=skill_catalog,
            ),
        )
        return state.messages

    async def build_request_state(
        self,
        agent: Any,
        session: ChatSession,
        *,
        inputs: RequestBuildInputs,
    ) -> _RequestState:
        # For a project-born session the Working Project context lands in the system
        # prompt; for an unrooted identity session it is empty. The
        # config-agent body is inserted verbatim (never re-expanded) by the builder.
        # ``skill_registry`` scopes the skills block to the project pool (``None`` =
        # the global registry); ``inputs.skill_catalog`` is the current prompt-epoch
        # snapshot the skills block renders from, so only Compaction replaces it. The
        # ``working_project_context`` / ``soul_context`` / ``memory_files_context``
        # prompt-epoch snapshots behave the same way.
        session_messages = (
            await session.load_active_async()
            if inputs.session_messages_override is None
            else list(inputs.session_messages_override)
        )
        system_prompts = self._dependencies.get_system_prompts()
        base_tools = await _run_prompt_method(
            system_prompts,
            "provider_tool_definitions_async",
            "provider_tool_definitions",
            agent,
        )
        history_grants: tuple[str, ...] = (
            (HISTORY_TOOL_NAME,) if history_available(session_messages) else ()
        )
        session_capability = None
        extension_registry = self._dependencies.get_extension_registry()
        if inputs.temporary_binding is not None and extension_registry is not None:
            session_capability = extension_registry.session_capability(
                inputs.temporary_binding, self._dependencies.tools
            )
        session_tool_grants = history_grants + (
            session_capability.tool_names if session_capability is not None else ()
        )
        effective_input_modalities = (
            inputs.input_modalities
            if inputs.input_modalities is not None
            else _model_input_modalities(self._dependencies, agent)
        )
        tools = (
            await _run_prompt_method(
                system_prompts,
                "provider_tool_definitions_async",
                "provider_tool_definitions",
                agent,
                session_tool_grants=session_tool_grants,
            )
            if session_tool_grants
            else base_tools
        )
        tools = await self._route_tool_definitions(
            tools,
            tool_access=agent.tool_access,
            input_modalities=effective_input_modalities,
            wire_media_types=inputs.wire_media_types,
        )
        allowed_tool_names = tuple(
            str(definition["name"])
            for definition in tools
            if isinstance(definition.get("name"), str)
        )
        allowed_tool_name_set = set(allowed_tool_names)
        session_tool_grants = tuple(
            name for name in session_tool_grants if name in allowed_tool_name_set
        )
        if inputs.temporary_binding is not None and (
            session_capability is None
            or not set(session_capability.tool_names).issubset(session_tool_grants)
        ):
            raise ChatError(
                "This Session has an invalid configuration. "
                "Ask the user to check it through its Extension."
            )
        tool_contracts = await _CHAT_TRANSFORM_WORKERS.run(
            self._dependencies.tools.contracts_for_provider_definitions,
            tools,
        )
        request_block_definitions: tuple[BlockDefinition, ...] = ()
        if (
            session_capability is not None
            and inputs.temporary_binding is not None
            and set(session_capability.tool_names).issubset(session_tool_grants)
        ):
            assert extension_registry is not None
            rendered_blocks: list[BlockDefinition] = []
            for declaration in session_capability.prompt_blocks:
                try:
                    rendered = await invoke_extension_handler(
                        declaration.render, inputs.temporary_binding
                    )
                except Exception:
                    _LOGGER.warning(
                        "Session prompt block %r failed", declaration.slug, exc_info=True
                    )
                    continue
                if isinstance(rendered, str) and rendered.strip():
                    rendered_blocks.append(
                        BlockDefinition(
                            id=f"extension_session:{declaration.slug}",
                            owner="always",
                            kind=BLOCK_KIND_DATA,
                            default_text=rendered.strip(),
                            default_rank=10_000,
                        )
                    )
            if not extension_registry.is_registration_current(session_capability.identity):
                raise ChatError(
                    "This Session has an invalid configuration. "
                    "Ask the user to check it through its Extension."
                )
            current_capability = extension_registry.session_capability(
                inputs.temporary_binding, self._dependencies.tools
            )
            if (
                current_capability is None
                or current_capability.identity != session_capability.identity
                or not set(current_capability.tool_names).issubset(session_tool_grants)
            ):
                raise ChatError(
                    "This Session has an invalid configuration. "
                    "Ask the user to check it through its Extension."
                )
            request_block_definitions = tuple(rendered_blocks)
        prompt_read_paths: list[Path] = []
        system_prompt = await _run_prompt_method(
            system_prompts,
            "build_system_prompt_async",
            "build_system_prompt",
            agent,
            agent_body=inputs.agent_body,
            project_context=inputs.project_context,
            working_project_context=inputs.working_project_context,
            soul_context=inputs.soul_context,
            memory_files_context=inputs.memory_files_context,
            agent_project_id=inputs.agent_project_id,
            nesting_depth=self.nesting_depth,
            skill_registry=inputs.skill_registry,
            skill_catalog=inputs.skill_catalog,
            read_paths=prompt_read_paths,
            effective_tool_names=allowed_tool_names,
            session_tool_grants=session_tool_grants,
            request_block_definitions=request_block_definitions,
        )
        # Auto-injected prompt files (SOUL, pinned memory, project auto-load files,
        # workspace includes) count as read for this session, so the agent can edit
        # one directly without a redundant read call. Rebuilt every request, so the
        # stamp always reflects what the model currently sees; a later on-disk change
        # still trips the stale guard and forces a re-read.
        await _CHAT_TRANSFORM_WORKERS.run(
            stamp_prompt_files_read,
            self._dependencies.file_read_state,
            session.id,
            prompt_read_paths,
        )
        prepared_messages = await _CHAT_TRANSFORM_WORKERS.run(
            _prepare_request_messages,
            system_prompt=system_prompt,
            agent_model=agent.model,
            session_messages=session_messages,
            replay_policy=inputs.replay_policy,
            reasoning_scope_model=inputs.reasoning_scope_model or agent.model,
        )
        effective_messages = prepared_messages.effective_messages
        request_messages = prepared_messages.messages

        session.drain_pending_notes()

        if self._attachment_resolver is None:
            return _RequestState(
                request_messages,
                tools,
                allowed_tool_names,
                session_tool_grants,
                tool_contracts,
            )

        current_user_message, read_media_outputs = await _CHAT_TRANSFORM_WORKERS.run(
            _request_content_resolution_inputs,
            effective_messages,
            session_messages,
        )
        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all user content blocks resolve as historical.
        if current_user_message is not None:
            request_messages = await self._attachment_resolver.resolve_messages(
                request_messages,
                current_user_message_id=current_user_message.id,
                input_modalities=effective_input_modalities,
                wire_media_types=inputs.wire_media_types,
            )

        await self._attach_tool_result_content(
            [message for message in request_messages if message.get("role") == "tool"],
            read_media_outputs,
            effective_input_modalities,
            inputs.wire_media_types,
        )
        return _RequestState(
            await _CHAT_TRANSFORM_WORKERS.run(
                limit_request_images, request_messages, budget=inputs.image_budget
            ),
            tools,
            allowed_tool_names,
            session_tool_grants,
            tool_contracts,
        )

    async def _route_tool_definitions(
        self,
        tools: list[JsonObject],
        *,
        tool_access: ToolAccess,
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> list[JsonObject]:
        """Apply effective Model-route gates to route-dependent Tools."""

        tools = project_bash_tool_definitions(tools, nesting_depth=self.nesting_depth)
        if not any(definition.get("name") == ANALYZE_IMAGE_TOOL_NAME for definition in tools):
            return tools
        route_can_view_images = "image" in input_modalities and any(
            media_type.startswith("image/") for media_type in wire_media_types
        )
        image_task_available = (
            False
            if route_can_view_images and ANALYZE_IMAGE_TOOL_NAME not in tool_access.granted
            else await self._dependencies.image_understanding_available()
        )
        if image_task_available:
            return tools
        return [
            definition for definition in tools if definition.get("name") != ANALYZE_IMAGE_TOOL_NAME
        ]

    async def preview_tool_definitions(
        self, agent: Any, *, session_tool_grants: Sequence[str] = ()
    ) -> list[JsonObject]:
        """Apply the production Tool-route rules without starting a Run or calling a Model."""
        tools = await _run_prompt_method(
            self._dependencies.get_system_prompts(),
            "provider_tool_definitions_async",
            "provider_tool_definitions",
            agent,
            session_tool_grants=session_tool_grants,
        )
        if not any(tool.get("name") == ANALYZE_IMAGE_TOOL_NAME for tool in tools):
            return await self._route_tool_definitions(
                tools,
                tool_access=agent.tool_access,
                input_modalities=frozenset(),
                wire_media_types=frozenset(),
            )
        provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
        _, model_id = _split_agent_model(agent.model)
        target = self._create_model_target(provider_id, connection_id, model_id)
        try:
            return await self._route_tool_definitions(
                tools,
                tool_access=agent.tool_access,
                input_modalities=target.input_modalities,
                wire_media_types=target.wire_media_types,
            )
        finally:
            await _close_adapter(target.adapter)

    async def _attach_tool_result_content(
        self,
        tool_messages: list[JsonObject],
        media_outputs: list[JsonObject],
        input_modalities: frozenset[str],
        wire_media_types: frozenset[str],
    ) -> None:
        """Attach resolved media blocks to their correlated Tool Results.

        The persisted Tool message keeps only its compact result envelope. Base64
        blocks live exclusively in the in-flight request, so reading an image does
        not fabricate or persist a user turn.
        """

        if not media_outputs:
            return

        by_tool_call_id: dict[str, list[JsonObject]] = {}
        for media_output in media_outputs:
            tool_call_id = media_output.get("tool_call_id")
            if isinstance(tool_call_id, str):
                by_tool_call_id.setdefault(tool_call_id, []).append(media_output)

        for tool_message in tool_messages:
            tool_call_id = tool_message.get("tool_call_id")
            matching = (
                by_tool_call_id.get(tool_call_id, []) if isinstance(tool_call_id, str) else []
            )
            if not matching:
                continue
            local_content = [
                block
                for media_output in matching
                if "base64" in media_output
                for block in await ContentBlockResolver.resolve_tool_image(
                    media_output, input_modalities, wire_media_types, self._tool_image_converter
                )
            ]
            if local_content:
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = local_content
            matching = [media for media in matching if "attachment_id" in media]
            if not matching or self._attachment_resolver is None:
                continue
            content_blocks = [
                content_block_to_dict(
                    MediaBlock(
                        type="media",
                        attachment_id=media_output["attachment_id"],
                        filename=media_output["filename"],
                        media_type=media_output["media_type"],
                    )
                )
                for media_output in matching
            ]
            transient_message_id = f"tool-result:{tool_call_id}"
            resolved = await self._attachment_resolver.resolve_messages(
                [
                    {
                        "id": transient_message_id,
                        "role": "user",
                        "content": content_blocks,
                    }
                ],
                current_user_message_id=transient_message_id,
                input_modalities=input_modalities,
                wire_media_types=wire_media_types,
            )
            resolved_content = resolved[0].get("content")
            if isinstance(resolved_content, list):
                tool_message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = local_content + resolved_content

    def resolve_context_window(
        self,
        agent: Any,
        target: _ModelTarget | None = None,
    ) -> int | None:
        """Resolve the usable context window for the current Model target.

        Returns ``None`` only when the model string is unusable (no
        ``provider/model`` form). Otherwise the value always resolves through the
        shared effective chain (user-set/capped window for flagged-local models,
        else model window → provider-config default → global floor, see
        :func:`resolve_effective_context_window`), so a model whose window is
        ``None`` still gets a usable budget and auto-compaction keeps working
        instead of silently disabling itself.
        """
        if target is None:
            bare_model = parse_bare_model(agent.model)
            if "/" not in bare_model:
                return None
            provider_id, _, resolved_model_id = bare_model.partition("/")
            if not provider_id or not resolved_model_id:
                return None
            try:
                _resolved_provider_id, connection_id = _resolve_agent_connection(
                    self._dependencies, agent
                )
            except (AttributeError, ChatError, ConfigError, KeyError):
                connection_id = None
        else:
            provider_id = target.provider_id
            resolved_model_id = target.model_id
            connection_id = target.connection_id

        try:
            model_entry = self._dependencies.models.get(provider_id, resolved_model_id)
        except (KeyError, AttributeError):
            return None

        model_context_window = model_entry.context_window
        try:
            if connection_id is None:
                raise ConfigError("Model target has no resolved Provider Connection")
            local_connection_id, _account_id = split_connection_id(provider_id, connection_id)
            context_window_for = getattr(model_entry, "context_window_for", None)
            if callable(context_window_for):
                model_context_window = context_window_for(local_connection_id)
        except (AttributeError, ChatError, ConfigError, KeyError):
            # Status/build callers can be partially wired. Preserve the existing
            # Model-wide fallback when the active Connection cannot be resolved.
            pass

        try:
            local_context_windows = self._dependencies.get_local_context_windows()
        except (AttributeError, KeyError):
            # Tolerant of a missing/partial runtime (test doubles may not
            # implement the local-model settings hook): treated as "no
            # user-configured local window overrides".
            local_context_windows = {}

        return resolve_effective_context_window(
            model_context_window,
            self._lookup_provider_config(provider_id),
            model_metadata=model_entry.metadata,
            model_key=f"{provider_id}/{resolved_model_id}",
            # Live read through the runtime's single source of truth (no reload
            # hook, StorageError-tolerant), so a settings change applies to the
            # next request without re-implementing the storage read here.
            local_context_windows=local_context_windows,
        )

    def _lookup_provider_config(self, provider_id: str) -> Any:
        """Return the ProviderConfig for the read-side window default, or None.

        Tolerant of a missing/partial runtime (the registry may be absent for a
        custom provider): the resolver treats ``None`` as "no provider default"
        and falls back to the global floor.
        """
        try:
            return self._dependencies.providers.get(provider_id)
        except (KeyError, AttributeError):
            return None

    def _raise_if_measured_context_exhausted(
        self,
        session_messages: list[ChatMessage],
        request_messages: list[JsonObject],
        tools: list[JsonObject],
        agent: Any,
        run: Run,
        target: _ModelTarget,
        *,
        context_usage: JsonObject | None = None,
    ) -> None:
        """Fail fast when the projected request already fills the Model window.

        The same measured anchor plus request delta drives the indicator and
        Compaction. Output-limit safety reserves belong to the Adapter and do
        not inflate the displayed Context or override a measured anchor.
        """

        context_window = self.resolve_context_window(agent, target)
        if context_window is None:
            return
        projection = context_usage or latest_session_context_usage(session_messages)
        if projection is None or "provider_input_tokens" not in projection:
            return
        projected_tokens = int(projection["tokens"])
        if projected_tokens < context_window:
            return
        _LOGGER.error(
            "Run %s pre-send context guard tripped (agent=%s session=%s "
            "projected_input_tokens=%d context_window=%d)",
            run.id,
            run.agent_id,
            run.session_id,
            projected_tokens,
            context_window,
        )
        raise ProviderError(
            "Context usage leaves no output capacity in the Model "
            f"context window (projected_input_tokens={projected_tokens}, "
            f"context_window={context_window})",
            retryable=False,
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
        """Resolve compaction summary adapter/model/provider, defaulting to active."""
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id, active_provider_id

        try:
            provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
                summary_model
            )
            if connection_suffix:
                connection_id = f"{provider_id}:{connection_suffix}"
            else:
                connection_id = _first_usable_connection_id(
                    self._dependencies,
                    provider_id,
                    _model_connection_allowlist(self._dependencies, provider_id, summary_model_id),
                )
            summary_adapter = self._dependencies.get_adapter(
                ConnectionRef(provider_id, connection_id)
            )
        except (ChatError, ConfigError, VBotError, KeyError):
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id, active_provider_id

        return summary_adapter, summary_model_id, provider_id
