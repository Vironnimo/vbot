"""Model-route acquisition, media limits and Context policy for Chat requests."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.chat._run_state import _ModelTarget
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.errors import ChatError
from core.chat.events import _close_adapter
from core.chat.messages import ChatMessage, JsonObject
from core.chat.model_resolution import (
    _ensure_provider_exists,
    _first_usable_connection_id,
    _model_connection_allowlist,
    _model_family_for_target,
    _model_input_modalities_for_target,
    _resolve_agent_connection,
    _split_agent_model,
    parse_model_with_connection,
)
from core.chat.streaming import stream_stall_timeout
from core.chat.usage import latest_session_context_usage
from core.providers.accounts import DEFAULT_ACCOUNT_ID, ConnectionRef, split_connection_id
from core.providers.providers import context_capacity, resolve_effective_context_window
from core.runs import Run
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies

_LOGGER = get_logger("chat")


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


def _resolve_image_size_limit(adapter: Any, model_id: str) -> int | None:
    value = adapter.image_size_limit(model_id)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _resolve_request_image_limit(adapter: Any, model_id: str) -> int | None:
    value = adapter.request_image_limit(model_id)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class RequestTargets:
    """Resolve a request route and own Adapter acquisition until handed to its caller."""

    def __init__(self, dependencies: ChatLoopDependencies, *, max_image_bytes: int | None) -> None:
        self._dependencies = dependencies
        self._max_image_bytes = max_image_bytes

    async def for_agent(self, agent: Any) -> _ModelTarget:
        """Resolve the Agent's Connection and acquire its exact request target offloop."""

        def binding() -> tuple[str, str, str, str]:
            provider_id, connection_id = _resolve_agent_connection(self._dependencies, agent)
            _ensure_provider_exists(self._dependencies.providers, provider_id)
            _, model_id = _split_agent_model(agent.model)
            return provider_id, connection_id, model_id, agent.model

        return await self._acquire(binding)

    async def create(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        *,
        public_model: str,
    ) -> _ModelTarget:
        """Acquire an already chosen route without losing an Adapter on cancellation."""
        return await self._acquire(lambda: (provider_id, connection_id, model_id, public_model))

    async def _acquire(self, binding: Callable[[], tuple[str, str, str, str]]) -> _ModelTarget:
        adapter: Any | None = None

        def prepare() -> _ModelTarget:
            nonlocal adapter
            provider_id, connection_id, model_id, public_model = binding()
            adapter = self._dependencies.get_adapter(ConnectionRef(provider_id, connection_id))
            return self._create_model_target(
                provider_id, connection_id, model_id, public_model=public_model, adapter=adapter
            )

        try:
            return await _CHAT_TRANSFORM_WORKERS.run(prepare)
        except BaseException:
            # The worker settles before cancellation is raised. Retain the Adapter
            # separately so both cancelled results and failed route metadata close it.
            if adapter is not None:
                try:
                    await settle_before_cancelling(_close_adapter(adapter))
                except Exception:
                    _LOGGER.warning("Could not close aborted Chat Adapter setup", exc_info=True)
            raise

    def _create_model_target(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        *,
        public_model: str,
        adapter: Any,
    ) -> _ModelTarget:
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
            public_model=public_model,
            adapter=adapter,
            replay_policy=adapter.reasoning_replay_policy(model_id),
            input_modalities=_model_input_modalities_for_target(
                self._dependencies,
                provider_id,
                model_id,
            ),
            wire_media_types=adapter.wire_media_support(model_id),
            chunk_timeout_seconds=stream_stall_timeout(adapter),
            max_image_bytes=self.image_size_limit(adapter, model_id),
            max_request_images=_resolve_request_image_limit(adapter, model_id),
            unlisted_tool_calls=not adapter.list_announced_tools(model_id),
            on_demand_tools=adapter.on_demand_tools(model_id),
            model_family=_model_family_for_target(self._dependencies, provider_id, model_id),
        )

    def image_size_limit(self, adapter: Any, model_id: str) -> int | None:
        limit = _resolve_image_size_limit(adapter, model_id)
        configured = self._max_image_bytes
        return (
            min(configured, limit)
            if configured is not None and limit is not None
            else (configured if configured is not None else limit)
        )

    async def resolve_context_capacity_async(self, agent: Any, target: _ModelTarget) -> int | None:
        """Read the live Settings-derived Context capacity on the Chat workers."""
        return await _CHAT_TRANSFORM_WORKERS.run(self.resolve_context_capacity, agent, target)

    def resolve_context_capacity(self, agent: Any, target: _ModelTarget) -> int | None:
        """The input tokens one request to the target may hold.

        The context window, or the Provider's smaller input limit for the Model
        when it reports one (``ProviderAdapter.input_token_limit``). The
        Session's Context usage is measured against this capacity.
        """
        context_window = self.resolve_context_window(agent, target)
        if context_window is None:
            return None
        return context_capacity(context_window, target.adapter.input_token_limit(target.model_id))

    def resolve_context_window(self, agent: Any, target: _ModelTarget) -> int | None:
        """Resolve the usable context window for the current Model target.

        Returns ``None`` only when the Model catalog has no entry for the target.
        Otherwise the value always resolves through the shared effective chain
        (user-set/capped window for flagged-local models, else model window →
        provider-config default → global floor, see
        :func:`resolve_effective_context_window`), so a model whose window is
        ``None`` still gets a usable budget and auto-compaction keeps working
        instead of silently disabling itself.
        """
        del agent  # The target carries the resolved Model and Connection.
        provider_id = target.provider_id
        resolved_model_id = target.model_id
        connection_id = target.connection_id

        try:
            model_entry = self._dependencies.models.get(provider_id, resolved_model_id)
        except KeyError, AttributeError:
            return None

        model_context_window = model_entry.context_window
        try:
            if connection_id is None:
                raise ConfigError("Model target has no resolved Provider Connection")
            local_connection_id, _account_id = split_connection_id(provider_id, connection_id)
            context_window_for = getattr(model_entry, "context_window_for", None)
            if callable(context_window_for):
                model_context_window = context_window_for(local_connection_id)
        except AttributeError, ChatError, ConfigError, KeyError:
            # Status/build callers can be partially wired. Preserve the existing
            # Model-wide fallback when the active Connection cannot be resolved.
            pass

        local_context_windows = self._dependencies.get_local_context_windows()

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
        except KeyError, AttributeError:
            return None

    @staticmethod
    def raise_if_measured_context_exhausted(
        session_messages: list[ChatMessage],
        run: Run,
        context_capacity: int | None,
        *,
        context_usage: JsonObject | None = None,
    ) -> None:
        """Fail fast when the measured request already fills the Model's Context capacity.

        The same measured anchor plus request delta drives the indicator and
        Compaction. The estimation margin belongs to the Adapter's output clamp
        and does not inflate the displayed Context or override a measured anchor.
        """

        if context_capacity is None:
            return
        projection = context_usage or latest_session_context_usage(session_messages)
        if projection is None or "provider_input_tokens" not in projection:
            return
        projected_tokens = int(projection["tokens"])
        if projected_tokens < context_capacity:
            return
        _LOGGER.error(
            "Run %s pre-send context guard tripped (agent=%s session=%s "
            "projected_input_tokens=%d context_capacity=%d)",
            run.id,
            run.agent_id,
            run.session_id,
            projected_tokens,
            context_capacity,
        )
        raise ProviderError(
            "Context usage fills the Model's Context capacity; compact the Context before "
            f"retrying (projected_input_tokens={projected_tokens}, "
            f"context_capacity={context_capacity})",
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
        active_connection_id: str,
    ) -> tuple[Any, str, str]:
        """Resolve compaction summary adapter/model/provider, defaulting to active.

        A configured summary Model that names the active Model, without another
        Connection, is the active target: it reuses the active adapter so the
        summary request keeps the Run's Provider prompt cache.
        """
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id, active_provider_id

        try:
            provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
                summary_model
            )
            if (
                provider_id == active_provider_id
                and summary_model_id == model_id
                and (
                    not connection_suffix
                    or f"{provider_id}:{connection_suffix}" == active_connection_id
                )
            ):
                return adapter, model_id, active_provider_id
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
        except ChatError, ConfigError, VBotError, KeyError:
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id, active_provider_id

        return summary_adapter, summary_model_id, provider_id
