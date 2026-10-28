"""Channel configuration, state, and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentStore
from core.channels import _delivery
from core.channels._adapters import create_adapter
from core.channels._connections import ChannelConnections
from core.channels._observations import ChannelObservationInbox
from core.channels.adapter import (
    ChannelAdapter,
    DeniedChatFacts,
    FileData,
    RouteFacts,
)
from core.channels.config import (
    _MUTABLE_FIELDS,
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    _normalize_channel_id,
)
from core.channels.engine import ChannelConversationEngine
from core.channels.mail import InboundMailSink
from core.channels.state import RUN_BUTTON_PRUNE_BATCH_SIZE, ChannelStateStore
from core.channels.storage import ChannelStorage
from core.chat.messages import ReplySurface
from core.config_validation import (
    JsonObject,
)
from core.database import DatabaseError
from core.extensions import InteractionButton, InteractionEvent, InteractionResponder
from core.utils.ids import is_reserved_name, reserved_name_message
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.agents.agents import AgentStore
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.database import Database
    from core.runs import Run
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels")
_RUN_BUTTON_SWEEP_SECONDS = 60 * 60
_sleep = asyncio.sleep


class ChannelService:
    """Manage channel config CRUD and adapter task lifecycle."""

    def __init__(
        self,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        *,
        agent_store: AgentStore,
        data_root: str | Path,
        credential_resolver: Callable[[str], str],
        attachment_store: AttachmentStore | None = None,
        tool_catalog: Callable[[], Sequence[Any]] = tuple,
        command_dispatcher: CommandDispatcher,
        interaction_dispatcher: (
            Callable[[InteractionEvent, InteractionResponder], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._agent_store = agent_store
        self._credential_resolver = credential_resolver
        self._attachment_store = attachment_store
        self._tool_catalog = tool_catalog
        self._command_dispatcher = command_dispatcher
        self._interaction_dispatcher = interaction_dispatcher
        self._storage = ChannelStorage(Path(data_root))
        self._channel_root = Path(data_root) / "channels"
        # channels.db holds the durable state of every configured Channel. A
        # Channel whose config exists is registered, including after its state
        # database came back from an older data snapshot. State recorded for
        # another platform than the config names (a platform change that did
        # not reach channels.db, or a hand-edited config) is reset here.
        self._state = ChannelStateStore.open(Path(data_root))
        try:
            for reset_id in self._state.adopt(self._storage.platforms()):
                _LOGGER.info("Channel state reset for a new platform (channel=%s)", reset_id)
            self._storage.load_all()
        except BaseException:
            self._state.close()
            raise
        self._observations = ChannelObservationInbox(self._state, chat_sessions, self._storage.get)
        self._observation_tasks: set[asyncio.Task[None]] = set()
        self._retention_tasks: set[asyncio.Task[None]] = set()
        self._retention_conditions = LoggedConditions()
        self._whatsapp_setup_tasks: dict[str, asyncio.Task[None]] = {}
        self._whatsapp_setup_states: dict[str, dict[str, Any]] = {}
        self._whatsapp_operations: dict[str, asyncio.Lock] = {}
        # Channel ids whose config change (create, update, enable, disable,
        # delete, or the config read of a restart) is in flight off the Event
        # Loop; every other change of such a Channel is refused meanwhile.
        self._pending_config_changes: set[str] = set()
        # Public changes own the lease across their off-loop read/write steps.
        # Telegram migration handlers take the same lease on their worker, so
        # neither can save a full config based on the other's stale snapshot.
        self._config_leases: dict[str, threading.Lock] = {}
        self._held_config_leases: set[str] = set()
        # A drained Telegram migration must leave a replacement platform alone,
        # but its new chat id still belongs in Telegram if the switch rolls back.
        self._rollback_chat_migrations: dict[str, list[tuple[str, str]]] = {}
        self._connections = ChannelConnections(
            self._storage,
            get_channel=self.get_channel,
            validate_agent=self._validate_agent_exists,
            new_engine=self._new_engine,
            create_adapter=lambda config: self._create_adapter(config),
            recover_observations=self._recover_observations,
        )
        self._notify_tool_registration_changed_hook: Callable[[], None] = lambda: None
        self._mail_sink: InboundMailSink | None = None

    def set_mail_sink(self, sink: InboundMailSink | None) -> None:
        """Bind where email Channels hand the mails no conversation takes (Cron)."""
        self._mail_sink = sink

    def start(self) -> None:
        self._connections.start()
        loop = _get_running_loop_or_none()
        if loop is not None and not any(
            not task.done() and not task.cancelling() for task in self._retention_tasks
        ):
            task = loop.create_task(self._retain_run_buttons(), name="channel-button-retention")
            self._retention_tasks.add(task)
            task.add_done_callback(self._retention_tasks.discard)

    async def _retain_run_buttons(self) -> None:
        """Prune at start and hourly, yielding between bounded database batches."""
        while True:
            try:
                while (
                    await self._state.run_async(self._state.prune_run_button_bindings)
                    == RUN_BUTTON_PRUNE_BATCH_SIZE
                ):
                    pass
            except Exception as error:
                if self._retention_conditions.started("run-buttons", type(error).__name__):
                    expected = isinstance(error, (DatabaseError, OSError))
                    _LOGGER.log(
                        logging.WARNING if expected else logging.ERROR,
                        "Run-button cleanup deferred (error_type=%s)",
                        type(error).__name__,
                        exc_info=not expected,
                    )
            else:
                if self._retention_conditions.ended("run-buttons"):
                    _LOGGER.info("Run-button cleanup recovered")
            await _sleep(_RUN_BUTTON_SWEEP_SECONDS)

    def stop(self) -> None:
        """Stop the adapters and observation recovery without awaiting them.

        Recovery is durable and resumes at the next start; cancelling it here
        keeps it from outliving the state database when no ``aclose`` follows,
        as after a failed Runtime start.
        """
        self._stop_adapters()
        for task in self._retention_tasks:
            task.cancel()
        for task in self._observation_tasks:
            task.cancel()

    def _stop_adapters(self) -> None:
        for setup_task in self._whatsapp_setup_tasks.values():
            setup_task.cancel()
        self._connections.stop()

    def start_channel(
        self,
        channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        self._connections.start_channel(
            channel_id, reset_backoff=reset_backoff, config_override=config_override
        )

    def stop_channel(self, channel_id: str) -> None:
        self._connections.stop_channel(channel_id)

    def has_active_channels(self) -> bool:
        return self._connections.has_active_channels()

    def is_running(self, channel_id: str) -> bool:
        return self._connections.is_running(_normalize_channel_id(channel_id))

    def is_failed(self, channel_id: str) -> bool:
        return self._connections.is_failed(channel_id)

    def failure_reason(self, channel_id: str) -> str | None:
        return self._connections.failure_reason(channel_id)

    def denied_chats(self, channel_id: str) -> list[DeniedChatFacts]:
        return self._connections.denied_chats(channel_id)

    @property
    def database(self) -> Database:
        """The canonical Channel state database, for data snapshots and status."""
        return self._state.database

    def close(self) -> None:
        """Close the Channel state database after the adapters stopped. Idempotent."""
        for task in self._retention_tasks:
            task.cancel()
        self._state.close()

    async def aclose(self) -> None:
        """Stop all channel tasks and await their cancellation/shutdown paths."""
        tasks = self._connections.pending_tasks()
        tasks.extend(self._retention_tasks)
        tasks.extend(self._whatsapp_setup_tasks.values())
        self._stop_adapters()
        for task in self._retention_tasks:
            task.cancel()
        tasks.extend(self._connections.pending_tasks())

        pending_tasks = _unique_pending_tasks(tasks)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        # Runs still own their Session leases until Runtime stops Chat next.
        # The inbox is durable; never wait for those leases during shutdown.
        observations = tuple(self._observation_tasks)
        for task in observations:
            task.cancel()
        if observations:
            await asyncio.gather(*observations, return_exceptions=True)
        self._observation_tasks.clear()

    async def restart_channel(self, channel_id: str) -> bool:
        """Rebuild one enabled channel adapter from its current config and credentials.

        Returns whether the channel is enabled and therefore received a start
        request. Disabled channels keep the updated credential for their next
        normal enable without creating an adapter. ``channel.json`` is read on
        the state database's worker pool; other changes of this Channel are
        refused until that read settles.
        """
        normalized_id = _normalize_channel_id(channel_id)
        self._require_idle(normalized_id)
        with self._config_change(normalized_id):
            config = await self.get_channel(normalized_id)
        self._validate_agent_exists(config.agent_id)
        self._preflight_adapter_start(config)

        if not config.enabled:
            return False

        self.stop_channel(normalized_id)
        self.start_channel(normalized_id, config_override=config)
        return True

    async def send(
        self,
        channel_id: str,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
        run_origin: RouteFacts | None = None,
        expected_agent_id: str | None = None,
    ) -> None:
        """Delegate an outbound send to a running channel adapter."""
        await _delivery.send(
            self,
            channel_id,
            message,
            platform_target,
            files=files,
            thread_id=thread_id,
            buttons=buttons,
            run_origin=run_origin,
            expected_agent_id=expected_agent_id,
        )

    async def relay_completion_run(self, run: Run, reply_surface: ReplySurface) -> None:
        """Relay a background completion Run to its Session's latest Channel target."""
        await _delivery.relay_completion_run(self, run, reply_surface)

    async def relay_run_to_chat(
        self,
        run: Run,
        channel_id: str,
        platform_target: str,
        *,
        thread_id: str | None = None,
    ) -> None:
        """Relay one Run's answer to a chat of its Agent's Channel, owed durably first.

        Returns once the Run ended and its answer is owed; the answer reaches the
        chat once the Channel is connected. Raises ``ChannelNotFoundError`` for a
        Channel that is missing or not started and ``ChannelConfigError`` for a
        Channel of another Agent, an invalid target, or a Project Session's Run.
        """
        await _delivery.relay_run_to_chat(
            self, run, channel_id, platform_target, thread_id=thread_id
        )

    async def record_outbound_note(
        self,
        channel_id: str,
        platform_target: str,
        note: str,
        *,
        thread_id: str | None = None,
        skip_session_id: str | None = None,
    ) -> bool:
        """Add a Note about a message sent to one chat to the Session mirroring that chat.

        The Session is created when missing; the Channel's adapter must be
        running. Returns False without writing when that Session is
        ``skip_session_id``, the Session the message came from.
        """
        return await _delivery.record_outbound_note(
            self,
            channel_id,
            platform_target,
            note,
            thread_id=thread_id,
            skip_session_id=skip_session_id,
        )

    async def ensure_outbound_session(
        self, channel_id: str, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        """Ensure the Session mirroring an outbound target chat exists and return its route.

        The adapter resolves the conversation on the Event Loop; its pointer read
        runs on the Channel state's pool and the Session work on the Session
        database's pool.
        """
        normalized_id = _normalize_channel_id(channel_id)
        if not isinstance(platform_target, str) or not platform_target:
            raise ChannelConfigError("platform_target must be a non-empty string")
        adapter = self._connections.active_adapter(normalized_id)
        return await adapter.ensure_outbound_session(platform_target, thread_id=thread_id)

    def list_channels(self) -> list[ChannelConfig]:
        """Return all persisted channels, enabled and disabled.

        Reads the owner's published configuration snapshot without disk I/O.
        ``list_channels_async`` refreshes that snapshot from current files.
        """
        return self._storage.snapshot()

    async def list_channels_async(self) -> list[ChannelConfig]:
        """Return all persisted channels, read on the state database's worker pool.

        Like ``list_channels``, a config that fails to load is skipped with a
        logged warning.
        """
        return await self._state.database.run_async(self._storage.load_all)

    async def get_channel(self, channel_id: str) -> ChannelConfig:
        """Read one ``channel.json`` on the state database's worker pool.

        Raises ``ChannelNotFoundError`` when the Channel has no config and
        ``ChannelConfigError`` when its id is invalid or its config fails to
        load, which a listing skips instead.
        """
        return await self._state.database.run_async(self._storage.get, channel_id)

    async def channel_access(self, channel_id: str) -> JsonObject:
        """Return one Channel's durable identity and per-group access state."""
        return await self._state.access_state(channel_id)

    async def set_channel_member_tools(
        self, channel_id: str, policy: Any, access_scope_id: str | None = None
    ) -> JsonObject:
        """Replace one member Tool ceiling without disturbing the connection."""
        result = await self._state.set_member_tools(channel_id, policy, access_scope_id)
        _LOGGER.info(
            "Channel member Tools updated (channel=%s scope=%s)",
            channel_id,
            "channel" if access_scope_id is None else "group",
        )
        return result

    async def set_channel_self_user_id(self, channel_id: str, user_id: str) -> JsonObject:
        """Set and return one Channel account's own platform identity."""
        return await self._state.set_self_user_id(channel_id, user_id)

    async def grant_channel_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Grant one user admin access in one group and return saved state."""
        return await self._state.grant_group_admin(channel_id, access_scope_id, user_id)

    async def revoke_channel_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Revoke one additional group admin and return saved state."""
        return await self._state.revoke_group_admin(channel_id, access_scope_id, user_id)

    async def create_channel(self, config: ChannelConfig) -> None:
        """Validate and persist one channel config, then start it when enabled.

        Writing ``channel.json`` and registering the Channel in ``channels.db``
        run as one unit on the state database's worker pool, off the Event Loop.
        A failure of either write, of the adapter start, or a cancellation after
        the unit ran removes both again. Other changes of this Channel id are
        refused until the create settles.
        """
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()
        # The id names the Channel's directory; existing Channels keep theirs.
        if is_reserved_name(config.id):
            raise ChannelConfigError(reserved_name_message("Channel id", config.id))
        self._validate_agent_exists(config.agent_id)
        self._require_idle(config.id)
        had_enabled_channels = self.has_enabled_channels()

        with self._config_change(config.id):
            try:
                await self.get_channel(config.id)
            except ChannelNotFoundError:
                pass
            else:
                raise ChannelConfigError(f"Channel already exists: {config.id}")

            self._preflight_adapter_start(config)
            persisted = False

            def persist() -> None:
                nonlocal persisted
                self._persist_created_channel(config)
                persisted = True

            try:
                await self._state.database.run_async(persist)
                if config.enabled:
                    self.start_channel(config.id, config_override=config)
            except BaseException:
                if persisted:
                    await _settle(
                        self._state.database.run_async(self._rollback_created_channel, config.id),
                        "Rollback failed while removing newly created channel "
                        f"(channel={config.id})",
                    )
                raise
        self._notify_tool_registration_if_changed(had_enabled_channels)

    async def update_channel(self, channel_id: str, **fields: Any) -> None:
        """Update mutable fields, persist them, and rebuild the adapter to match.

        ``channel.json`` is read and written on the state database's worker
        pool, off the Event Loop; ordering and rollback follow
        ``_change_config``. Other changes of this Channel are refused until the
        update settles.
        """
        normalized_id = _normalize_channel_id(channel_id)
        self._require_idle(normalized_id)
        with self._config_change(normalized_id):
            config = await self.get_channel(normalized_id)
            unknown_fields = sorted(set(fields) - _MUTABLE_FIELDS)
            if unknown_fields:
                joined = ", ".join(unknown_fields)
                raise ChannelConfigError(f"Unsupported channel fields: {joined}")
            if not fields:
                return
            if fields.get("platform", "email") != "email" and "email" not in fields:
                # The mail account belongs to the email platform only.
                fields = {**fields, "email": None}
            elif isinstance(fields.get("email"), Mapping) and config.email is not None:
                # An email update changes only the account fields it names.
                fields = {**fields, "email": config.email.updated(fields["email"])}
            updated = replace(config, **fields)
            updated.validate()
            self._validate_agent_exists(updated.agent_id)
            self._preflight_adapter_start(updated)
            await self._change_config(config, updated)

    def retarget_agent(self, agent_id: str, new_agent_id: str) -> tuple[str, ...]:
        """Point every Channel that answers as ``agent_id`` at ``new_agent_id``.

        One step of an Identity Agent rename while this service is not started,
        such as one that completes during startup: only ``channel.json`` changes
        and no adapter starts. Blocking. Once the service runs, adapters hold
        their config; :meth:`retarget_agent_async` then changes each Channel
        through :meth:`update_channel`. Returns the ids of the changed Channels.
        """
        if self._connections.started:
            raise ChannelError("Retarget running Channels through update_channel")
        changed: list[str] = []
        for config in self._storage.load_all():
            if config.agent_id != agent_id:
                continue
            self._storage.save(replace(config, agent_id=new_agent_id))
            changed.append(config.id)
            _LOGGER.debug(
                "Channel retargeted (channel=%s agent=%s new_agent=%s)",
                config.id,
                agent_id,
                new_agent_id,
            )
        return tuple(changed)

    async def retarget_agent_async(self, agent_id: str, new_agent_id: str) -> tuple[str, ...]:
        """Point every Channel that answers as ``agent_id`` at ``new_agent_id`` from the Event Loop.

        While this service is not started, only ``channel.json`` changes, on the
        state database's worker pool, and no adapter starts (:meth:`retarget_agent`).
        A started service changes each Channel through :meth:`update_channel`,
        which rebuilds a running adapter for its new Agent. Returns the ids of the
        changed Channels.
        """
        if not self._connections.started:
            return await self._state.database.run_async(self.retarget_agent, agent_id, new_agent_id)
        changed: list[str] = []
        for config in await self.list_channels_async():
            if config.agent_id == agent_id:
                self._require_idle(config.id)
                with self._config_change(config.id):
                    current = await self.get_channel(config.id)
                    if current.agent_id != agent_id:
                        continue
                    updated = replace(current, agent_id=new_agent_id)
                    self._validate_agent_exists(new_agent_id)
                    await self._change_config(current, updated, agent_renamed=True)
                changed.append(config.id)
        return tuple(changed)

    async def delete_channel(self, channel_id: str) -> None:
        """Delete one channel config and state, and stop any active adapter task.

        The adapter stops on the Event Loop; removing the Channel directory with
        its ``channel.json`` and then unregistering the Channel, which drops every
        ``channels.db`` row it owns, run as one unit on the state database's
        worker pool. Other changes of this Channel id are refused until the
        delete settles.
        """
        normalized_id = _normalize_channel_id(channel_id)
        self._require_idle(normalized_id)
        had_enabled_channels = self.has_enabled_channels()
        removed = False

        def remove() -> None:
            nonlocal removed
            self._storage.delete(normalized_id)
            # Late saves of the stopping adapter are refused once unregistered.
            self._state.unregister(normalized_id)
            removed = True

        try:
            with self._config_change(normalized_id):
                config = await self.get_channel(normalized_id)
                if config.platform in {"whatsapp", "slack", "mattermost"} and (
                    self._connections.is_running(normalized_id)
                    or self._connections.is_stopping(normalized_id)
                ):
                    raise ChannelError(
                        "Disable the Channel and wait for shutdown or setup "
                        "to finish before removing it"
                    )
                self.stop_channel(normalized_id)
                self._connections.cancel_pending_start(normalized_id)
                self._release_config_lease(normalized_id)
                stopping = self._connections.stopping(normalized_id)
                if stopping is not None:
                    await asyncio.shield(stopping)
                await self._state.database.run_async(remove)
        finally:
            # The worker settles before a cancellation arrives here, so a
            # completed removal is always followed through.
            if removed:
                self._whatsapp_setup_states.pop(normalized_id, None)
                self._whatsapp_setup_tasks.pop(normalized_id, None)
                self._whatsapp_operations.pop(normalized_id, None)
                self._notify_tool_registration_if_changed(had_enabled_channels)

    async def enable_channel(self, channel_id: str) -> None:
        """Enable one channel and start its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        self._require_idle(normalized_id)
        await self._enable_channel(normalized_id)

    async def _enable_channel(self, channel_id: str) -> None:
        """Apply enable after public exclusion checks or inside the pairing owner."""
        with self._config_change(channel_id):
            config = await self.get_channel(channel_id)
            self._validate_agent_exists(config.agent_id)
            if config.enabled:
                # Nothing to persist; (re)start a stopped or failed adapter.
                self.start_channel(channel_id, config_override=config)
                return
            await self._change_config(config, replace(config, enabled=True))

    async def disable_channel(self, channel_id: str) -> None:
        """Disable one channel and stop its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        self._require_idle(normalized_id)
        with self._config_change(normalized_id):
            config = await self.get_channel(normalized_id)
            if not config.enabled:
                self.stop_channel(normalized_id)
                return
            await self._change_config(config, replace(config, enabled=False))

    def record_chat_id_migration(self, channel_id: str, old_chat_id: str, new_chat_id: str) -> None:
        """Persist a platform-side chat-id migration into allowlist and group access.

        Moves the group's admins and participants to the new chat id, swaps the old
        chat id for the new one in ``allowed_chat_ids`` and saves the config without
        restarting the adapter — the running adapter already swapped its in-memory
        allowlist and a restart would drop queued conversation work. Idempotent: a
        config that no longer lists the old id is left untouched. Blocking; the
        adapter calls it from a worker thread.
        """
        normalized_id = _normalize_channel_id(channel_id)
        with self._config_lease(normalized_id):
            config = self._storage.get(normalized_id)
            # A platform change persists before draining the old Telegram
            # handler. Its ids must not change the replacement's config/state.
            if config.platform != "telegram":
                rollback = self._rollback_chat_migrations.get(normalized_id)
                if rollback is not None:
                    rollback.append((old_chat_id, new_chat_id))
                return
            self._state.migrate_group_access(normalized_id, old_chat_id, new_chat_id)
            if old_chat_id not in config.allowed_chat_ids:
                return
            self._storage.save(_with_migrated_chat_id(config, old_chat_id, new_chat_id))
        # Both chat ids are Telegram-owned; the allowlist itself shows them.
        _LOGGER.info("Channel allowlist migrated (channel=%s)", normalized_id)

    def has_enabled_channels(self) -> bool:
        """Return whether a valid Agent owns at least one enabled Channel config."""
        for config in self._storage.snapshot():
            if not config.enabled:
                continue
            try:
                self._validate_agent_exists(config.agent_id)
            except ChannelConfigError:
                continue
            return True
        return False

    def _notify_tool_registration_changed(self) -> None:
        try:
            self._notify_tool_registration_changed_hook()
        except Exception:
            _LOGGER.exception("Channel tool-registration hook failed")

    def _notify_tool_registration_if_changed(self, had_enabled_channels: bool) -> None:
        if had_enabled_channels == self.has_enabled_channels():
            return
        self._notify_tool_registration_changed()

    def _new_engine(self, config: ChannelConfig) -> ChannelConversationEngine:
        return ChannelConversationEngine(
            config,
            self._trigger_service,
            self._chat_sessions,
            command_dispatcher=self._command_dispatcher,
            conversation_pointers=self._state,
            pending_replies=self._state,
            observation_inbox=self._observations,
            previous_observations=tuple(self._observation_tasks),
            run_button_binding_registry=self._state if config.platform == "telegram" else None,
            access_registry=self._state,
            tool_catalog=self._tool_catalog,
        )

    def _create_adapter(self, config: ChannelConfig) -> ChannelAdapter:
        return create_adapter(
            config,
            engine_for=self._connections.engine_for,
            credential_resolver=self._credential_resolver,
            attachment_store=self._attachment_store,
            state=self._state,
            channel_root=self._channel_root,
            chat_migration_persister=self.record_chat_id_migration,
            interaction_dispatcher=self._interaction_dispatcher,
            mail_sink=lambda: self._mail_sink,
        )

    def connection_status(self, channel_id: str) -> dict[str, Any]:
        from core.channels._network_adapter import NetworkChannelAdapter
        from core.channels.email import EmailChannelAdapter

        adapter = self._connections.adapter(_normalize_channel_id(channel_id))
        if isinstance(adapter, (NetworkChannelAdapter, EmailChannelAdapter)):
            return adapter.connection_status()
        return {}

    async def whatsapp_status(self, channel_id: str) -> dict[str, Any]:
        from core.channels._network_adapter import channel_io
        from core.channels._whatsapp_setup import bridge_ready
        from core.channels.whatsapp import WhatsAppChannelAdapter

        config = await self.get_channel(channel_id)
        if config.platform != "whatsapp":
            raise ChannelConfigError("This operation requires a WhatsApp Channel")
        adapter = self._connections.adapter(config.id)
        return {
            "id": config.id,
            "installed": await channel_io(bridge_ready, self._channel_root / config.id),
            **self._whatsapp_setup_states.get(config.id, {}),
            **(
                adapter.pairing_status()
                if isinstance(adapter, WhatsAppChannelAdapter)
                else {"state": "disconnected", "qr_image": None}
            ),
        }

    def setup_activities(self) -> list[dict[str, Any]]:
        """Each WhatsApp support installation of this process, from memory only.

        ``{channel_id, state: running}``, ``{channel_id, state: completed}``
        or ``{channel_id, state: failed, error: "setup_failed", message}``; a
        cancelled installation reports nothing.
        """
        activities: list[dict[str, Any]] = []
        for channel_id, setup in self._whatsapp_setup_states.items():
            if setup.get("setup") == "installing":
                activities.append({"channel_id": channel_id, "state": "running"})
            elif setup.get("setup") == "ready":
                activities.append({"channel_id": channel_id, "state": "completed"})
            elif setup.get("setup") == "failed":
                activities.append(
                    {
                        "channel_id": channel_id,
                        "state": "failed",
                        "error": "setup_failed",
                        "message": setup.get("error") or "",
                    }
                )
        return activities

    async def setup_whatsapp(self, channel_id: str) -> dict[str, Any]:
        from core.channels._whatsapp_setup import install_bridge

        channel_id = _normalize_channel_id(channel_id)
        operation = self._whatsapp_operations.setdefault(channel_id, asyncio.Lock())
        async with operation:
            self._require_no_config_change(channel_id)
            status = await self.whatsapp_status(channel_id)
            existing = self._whatsapp_setup_tasks.get(channel_id)
            if existing is not None and not existing.done():
                return status
            if (
                (await self.get_channel(channel_id)).enabled
                or self._connections.is_running(channel_id)
                or self._connections.is_stopping(channel_id)
            ):
                raise ChannelConfigError("Disable this WhatsApp Channel before installing support")
            self._whatsapp_setup_states[channel_id] = {"setup": "installing", "error": None}

            async def install() -> None:
                try:
                    await install_bridge(self._channel_root / channel_id)
                    self._whatsapp_setup_states[channel_id] = {"setup": "ready", "error": None}
                    _LOGGER.info("WhatsApp support installed (channel=%s)", channel_id)
                except asyncio.CancelledError:
                    self._whatsapp_setup_states[channel_id] = {"setup": "cancelled", "error": None}
                    raise
                except Exception as error:
                    reason = (
                        str(error) if isinstance(error, ChannelError) else "WhatsApp setup failed"
                    )
                    self._whatsapp_setup_states[channel_id] = {"setup": "failed", "error": reason}
                    _LOGGER.warning("WhatsApp support installation failed (channel=%s)", channel_id)

            self._whatsapp_setup_tasks[channel_id] = asyncio.create_task(
                install(), name=f"channel:{channel_id}:setup"
            )
            return await self.whatsapp_status(channel_id)

    async def pair_whatsapp(self, channel_id: str, *, reset: bool = False) -> dict[str, Any]:
        channel_id = _normalize_channel_id(channel_id)
        operation = self._whatsapp_operations.setdefault(channel_id, asyncio.Lock())
        async with operation:
            self._require_no_config_change(channel_id)
            status = await self.whatsapp_status(channel_id)
            if not status["installed"]:
                raise ChannelConfigError("Install WhatsApp support first")
            if status.get("setup") == "installing":
                raise ChannelConfigError("Wait for WhatsApp support installation to finish")
            if reset or status["state"] == "logged_out":
                from core.channels._network_adapter import channel_io
                from core.channels._whatsapp_setup import reset_pairing

                self.stop_channel(channel_id)
                stopping = self._connections.stopping(channel_id)
                if stopping is not None:
                    await asyncio.shield(stopping)
                await channel_io(reset_pairing, self._channel_root / channel_id)
            await self._enable_channel(channel_id)
            _LOGGER.info("WhatsApp pairing requested (channel=%s reset=%s)", channel_id, reset)
            return await self.whatsapp_status(channel_id)

    def _require_idle(self, channel_id: str) -> None:
        """Refuse a change while a config change or WhatsApp operation of it is in flight."""
        self._require_no_config_change(channel_id)
        operation = self._whatsapp_operations.get(channel_id)
        if operation is not None and operation.locked():
            raise ChannelError("Wait for the WhatsApp connection operation to finish")
        setup = self._whatsapp_setup_tasks.get(channel_id)
        if setup is not None and not setup.done():
            raise ChannelError("Wait for WhatsApp setup to finish before changing this Channel")

    def _require_no_config_change(self, channel_id: str) -> None:
        if channel_id in self._pending_config_changes:
            raise ChannelError("Wait for the current change of this Channel to finish")

    @contextmanager
    def _config_change(self, channel_id: str) -> Iterator[None]:
        """Mark a config change of ``channel_id`` in flight; refuse overlapping ones."""
        self._require_no_config_change(channel_id)
        if not self._config_lease(channel_id).acquire(blocking=False):
            raise ChannelError("Wait for the current change of this Channel to finish")
        self._held_config_leases.add(channel_id)
        self._pending_config_changes.add(channel_id)
        try:
            yield
        finally:
            self._release_config_lease(channel_id)
            self._pending_config_changes.discard(channel_id)

    def _config_lease(self, channel_id: str) -> threading.Lock:
        return self._config_leases.setdefault(channel_id, threading.Lock())

    def _release_config_lease(self, channel_id: str) -> None:
        if channel_id in self._held_config_leases:
            self._held_config_leases.remove(channel_id)
            self._config_lease(channel_id).release()

    async def _change_config(
        self, previous: ChannelConfig, updated: ChannelConfig, *, agent_renamed: bool = False
    ) -> None:
        """Persist ``updated``, then bring the adapter in line with it.

        The caller holds this Channel's config-change mark. ``channel.json`` is
        written on the state database's worker pool before a healthy adapter is
        disturbed, so a failed write leaves config and connection untouched.
        The adapter then stops and starts on the Event Loop. A platform change
        waits for the old adapter to finish stopping, so its last state writes
        land first, and then resets the Channel state for the new platform
        (``ChannelStateStore.bind_platform``) before the new adapter starts. A
        failed adapter start, or a cancellation once the write ran, restores
        ``previous`` and the adapter it ran.
        """
        channel_id = updated.id
        platform_changed = updated.platform != previous.platform
        agent_changed = updated.agent_id != previous.agent_id and not agent_renamed
        persisted = False
        was_running = False

        def persist() -> tuple[bool, bool]:
            nonlocal persisted
            had_enabled_channels = self.has_enabled_channels()
            self._storage.save(updated)
            persisted = True
            return had_enabled_channels, self.has_enabled_channels()

        try:
            had_enabled_channels, has_enabled_channels = await self._state.database.run_async(
                persist
            )
            was_running = self._connections.is_running(channel_id) or self._connections.is_stopping(
                channel_id
            )
            if platform_changed and previous.platform == "telegram":
                self._rollback_chat_migrations[channel_id] = []
            if was_running or not updated.enabled or platform_changed or agent_changed:
                self.stop_channel(channel_id)
            if platform_changed or agent_changed:
                # Drained Telegram handlers may be waiting for this lease.
                # They leave the replacement platform alone while retaining id
                # rewrites for rollback. Release before waiting for shutdown.
                self._release_config_lease(channel_id)
                stopping = self._connections.stopping(channel_id)
                if stopping is not None:
                    await asyncio.shield(stopping)
                if platform_changed and await self._state.database.run_async(
                    self._state.bind_platform, channel_id, updated.platform
                ):
                    _LOGGER.info("Channel state reset for a new platform (channel=%s)", channel_id)
                if agent_changed:
                    await self._state.database.run_async(self._state.clear_agent_routes, channel_id)
                    # A drained Telegram handler may have migrated its chat id
                    # after persistence. The replacement needs that allowlist.
                    updated = await self.get_channel(channel_id)
            if updated.enabled:
                try:
                    self.start_channel(channel_id, config_override=updated)
                except Exception as error:
                    if not agent_renamed:
                        raise
                    # The Agent and its Sessions already moved. Missing platform
                    # credentials cannot undo that identity change: retain the
                    # new config and expose/retry the adapter failure normally.
                    self._connections.mark_failed(channel_id, str(error) or type(error).__name__)
                    self._connections.schedule_restart(channel_id)
                    _LOGGER.error(
                        "Cannot restart channel adapter after Agent rename (channel=%s): %s",
                        channel_id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
        except BaseException:
            if persisted:
                await self._restore_config(
                    previous, restart_adapter=was_running, rebind_state=platform_changed
                )
            raise
        finally:
            self._rollback_chat_migrations.pop(channel_id, None)
        if had_enabled_channels != has_enabled_channels:
            self._notify_tool_registration_changed()

    def _preflight_adapter_start(self, config: ChannelConfig) -> None:
        if not config.enabled:
            return
        if _get_running_loop_or_none() is None:
            return
        self._create_adapter(config)

    def _validate_agent_exists(self, agent_id: str) -> None:
        """Require one of the user's Agents; a built-in Agent never serves a Channel."""
        try:
            agent = self._agent_store.get(agent_id)
        except Exception as error:
            raise ChannelConfigError(f"Unknown agent_id: {agent_id}") from error
        if agent.builtin is not None:
            raise ChannelConfigError(
                f"agent_id {agent_id} is vBot's built-in {agent.name}; choose one of your Agents"
            )

    def _persist_created_channel(self, config: ChannelConfig) -> None:
        """Write ``channel.json``, then register the Channel with empty state; all or nothing.

        Blocking; runs on the state database's worker pool.
        """
        self._storage.save(config)
        try:
            # A new Channel never inherits state rows left behind under its id.
            self._state.reset(config.id, config.platform)
        except BaseException:
            self._rollback_created_channel(config.id)
            raise

    def _rollback_created_channel(self, channel_id: str) -> None:
        """Remove a created Channel's config and registration, logging failures. Blocking."""
        try:
            self._storage.delete(channel_id)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while deleting newly created channel config (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        try:
            self._state.unregister(channel_id)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while removing newly created channel state (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _restore_config(
        self, previous: ChannelConfig, *, restart_adapter: bool, rebind_state: bool = False
    ) -> None:
        """Undo a persisted config change; failures are logged, not raised.

        With ``rebind_state`` the Channel state is first bound back to the
        previous platform, which resets it again when the change already reset
        it; this runs to its end even when cancelled meanwhile, so the previous
        adapter never writes into state of the abandoned platform. Restoring
        ``channel.json`` runs to its end before requesting the previous adapter,
        so a deferred start can safely reload the persisted configuration.
        """
        channel_id = previous.id
        self._connections.cancel_pending_start(channel_id)
        cancelled = False
        if rebind_state:
            try:
                await _settle(
                    self._state.database.run_async(
                        self._state.bind_platform, channel_id, previous.platform
                    ),
                    "Rollback failed while restoring the previous platform of channel state "
                    f"(channel={channel_id})",
                )
            except asyncio.CancelledError:
                cancelled = True

        holds_lease = channel_id in self._held_config_leases
        restored = previous

        def restore() -> None:
            nonlocal restored
            with nullcontext() if holds_lease else self._config_lease(channel_id):
                for old_chat_id, new_chat_id in self._rollback_chat_migrations.get(channel_id, ()):
                    # Cancellation can restore Telegram before the first
                    # platform reset, so its existing group access still needs
                    # the migration. After a reset this is an empty-state no-op.
                    self._state.migrate_group_access(channel_id, old_chat_id, new_chat_id)
                    restored = _with_migrated_chat_id(restored, old_chat_id, new_chat_id)
                self._storage.save(restored)

        try:
            await _settle(
                self._state.database.run_async(restore),
                f"Rollback failed while restoring previous channel config (channel={channel_id})",
            )
        except asyncio.CancelledError:
            cancelled = True
        if restart_adapter and previous.enabled:
            try:
                self.start_channel(channel_id, config_override=restored)
            except Exception as error:
                _LOGGER.error(
                    "Rollback failed while restarting previous channel adapter (channel=%s): %s",
                    channel_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
        if cancelled:
            raise asyncio.CancelledError

    def _recover_observations(self, engine: ChannelConversationEngine | None = None) -> None:
        loop = _get_running_loop_or_none()
        if loop is None:
            return
        task = loop.create_task(
            self._observations.recover(exclude_owners=self._connections.observation_owners)
            if engine is None
            else engine.recover_observations(),
            name="channels:observed-context",
        )
        self._observation_tasks.add(task)
        task.add_done_callback(self._observation_tasks.discard)


def _with_migrated_chat_id(
    config: ChannelConfig, old_chat_id: str, new_chat_id: str
) -> ChannelConfig:
    allowed_chat_ids = list(
        dict.fromkeys(
            new_chat_id if chat_id == old_chat_id else chat_id
            for chat_id in config.allowed_chat_ids
        )
    )
    return replace(config, allowed_chat_ids=allowed_chat_ids)


async def _settle(work: Awaitable[object], failure_message: str) -> None:
    """Finish compensation before cancellation, logging its failure without replacing the cause."""
    task = asyncio.ensure_future(work)
    if task.cancelled():
        return

    async def compensate() -> None:
        try:
            await task
        except Exception as error:
            _LOGGER.error(
                "%s: %s",
                failure_message,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    await settle_before_cancelling(compensate())


def _unique_pending_tasks(tasks: list[asyncio.Task[None]]) -> list[asyncio.Task[None]]:
    seen: set[asyncio.Task[None]] = set()
    pending_tasks: list[asyncio.Task[None]] = []
    for task in tasks:
        if task.done() or task in seen:
            continue
        seen.add(task)
        pending_tasks.append(task)
    return pending_tasks


__all__ = [
    "ChannelService",
]


def _get_running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
