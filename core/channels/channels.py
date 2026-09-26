"""Channel configuration, state, and lifecycle management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.attachments import AttachmentStore
from core.channels import _delivery
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
from core.channels.state import ChannelStateStore
from core.channels.storage import ChannelStorage
from core.chat.messages import ReplySurface
from core.config_validation import (
    JsonObject,
)
from core.extensions import InteractionButton, InteractionEvent, InteractionResponder
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents.agents import AgentStore
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.database import Database
    from core.runs import Run
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels")

_ADAPTER_RESTART_INITIAL_DELAY_SECONDS = 1.0
_ADAPTER_RESTART_MAX_DELAY_SECONDS = 30.0
# Fast-retry budget before a channel is marked failed. Exhausting it does NOT
# stop recovery: the channel keeps retrying at the capped backoff interval for
# as long as it is enabled — a channel is the operator's lifeline (e.g. Telegram
# control of the whole server), so a transient network blip at startup must
# never require manual intervention.
_ADAPTER_RESTART_MAX_RETRIES = 3
# An adapter that stayed up at least this long was healthy: its next crash
# starts a fresh restart cycle instead of counting toward chronic failure.
_ADAPTER_HEALTHY_RUN_RESET_SECONDS = 300.0
# Telegram caps an inline button's callback_data at 64 UTF-8 bytes; validated on
# send so an over-long payload fails cleanly rather than at the Bot API.


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
        except BaseException:
            self._state.close()
            raise
        self._whatsapp_setup_tasks: dict[str, asyncio.Task[None]] = {}
        self._whatsapp_setup_states: dict[str, dict[str, Any]] = {}
        self._whatsapp_operations: dict[str, asyncio.Lock] = {}
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_task_created: dict[str, float] = {}
        self._adapter_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_restart_attempts: dict[str, int] = {}
        self._adapter_restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_start_requests: dict[str, tuple[bool, ChannelConfig | None]] = {}
        # Channel ids whose config change (create, update, enable, disable,
        # delete, or the config read of a restart) is in flight off the Event
        # Loop; every other change of such a Channel is refused meanwhile.
        self._pending_config_changes: set[str] = set()
        self._failed_channels: set[str] = set()
        self._failure_reasons: dict[str, str] = {}
        self._started = False
        self._notify_tool_registration_changed_hook: Callable[[], None] = lambda: None

    def start(self) -> None:
        """Start the channel service and launch enabled channel adapter tasks."""
        if self._started:
            return

        self._started = True
        for config in self._storage.load_all():
            if config.enabled:
                try:
                    self.start_channel(config.id)
                except Exception as error:
                    reason = str(error) or type(error).__name__
                    self._mark_channel_failed(config.id, reason)
                    self._schedule_restart(config.id)
                    _LOGGER.error(
                        "Cannot start channel adapter during service startup (channel=%s): %s",
                        config.id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )

    def stop(self) -> None:
        """Stop all active channel adapter tasks. Idempotent."""
        for setup_task in self._whatsapp_setup_tasks.values():
            setup_task.cancel()
        if (
            not self._started
            and not self._adapter_tasks
            and not self._adapter_restart_tasks
            and not self._adapter_stop_tasks
        ):
            return

        self._pending_start_requests.clear()
        for channel_id in list(self._adapter_restart_tasks):
            self._cancel_restart_task(channel_id)
        for channel_id in list(self._adapter_tasks):
            self.stop_channel(channel_id)
        self._adapter_restart_attempts.clear()
        self._adapter_task_created.clear()
        self._failed_channels.clear()
        self._failure_reasons.clear()
        self._started = False

    @property
    def database(self) -> Database:
        """The canonical Channel state database, for data snapshots and status."""
        return self._state.database

    def close(self) -> None:
        """Close the Channel state database after the adapters stopped. Idempotent."""
        self._state.close()

    async def aclose(self) -> None:
        """Stop all channel tasks and await their cancellation/shutdown paths."""
        tasks = [*self._adapter_stop_tasks.values(), *self._adapter_restart_tasks.values()]
        tasks.extend(self._whatsapp_setup_tasks.values())
        self.stop()
        tasks.extend(self._adapter_stop_tasks.values())

        pending_tasks = _unique_pending_tasks(tasks)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    def start_channel(
        self,
        channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        """Start one enabled channel adapter task when not already running."""
        normalized_id = _normalize_channel_id(channel_id)

        if config_override is not None:
            if config_override.id != normalized_id:
                raise ChannelConfigError(
                    f"config_override.id mismatch: {config_override.id} != {normalized_id}"
                )
            config = replace(config_override)
            config.validate()
        else:
            config = self._storage.get(normalized_id)

        self._validate_agent_exists(config.agent_id)
        self._cancel_restart_task(normalized_id)
        if reset_backoff:
            self._adapter_restart_attempts.pop(normalized_id, None)
            self._failed_channels.discard(normalized_id)
            self._failure_reasons.pop(normalized_id, None)

        if self._is_stop_in_progress(normalized_id):
            self._schedule_pending_start(
                normalized_id,
                reset_backoff=reset_backoff,
                config_override=config,
            )
            return

        existing_task = self._adapter_tasks.get(normalized_id)
        if existing_task is not None and not existing_task.done():
            return
        if existing_task is not None and existing_task.done():
            self._adapter_tasks.pop(normalized_id, None)
            self._adapters.pop(normalized_id, None)

        if not config.enabled:
            return

        loop = _get_running_loop_or_none()
        if loop is None:
            _LOGGER.warning(
                "Cannot start channel adapter without a running event loop (channel=%s)",
                normalized_id,
            )
            return

        adapter = self._create_adapter(config)
        task = loop.create_task(
            self._run_adapter(normalized_id, adapter), name=f"channel:{normalized_id}"
        )
        self._adapters[normalized_id] = adapter
        self._adapter_tasks[normalized_id] = task
        self._adapter_task_created[normalized_id] = time.monotonic()

        def on_done(completed_task: asyncio.Task[None], channel: str = normalized_id) -> None:
            self._on_adapter_task_done(channel, completed_task)

        task.add_done_callback(on_done)

    def stop_channel(self, channel_id: str) -> None:
        """Stop one running channel adapter task when active."""
        normalized_id = _normalize_channel_id(channel_id)

        self._pending_start_requests.pop(normalized_id, None)
        self._cancel_restart_task(normalized_id)
        self._adapter_restart_attempts.pop(normalized_id, None)
        self._failed_channels.discard(normalized_id)
        self._failure_reasons.pop(normalized_id, None)

        task = self._adapter_tasks.pop(normalized_id, None)
        self._adapters.pop(normalized_id, None)
        self._adapter_task_created.pop(normalized_id, None)

        if task is not None and not task.done():
            task.cancel()
            loop = _get_running_loop_or_none()
            if loop is not None:
                stop_task = loop.create_task(
                    self._await_adapter_shutdown(normalized_id, task),
                    name=f"channel:{normalized_id}:stop",
                )
                self._adapter_stop_tasks[normalized_id] = stop_task

                def on_stop_done(
                    completed_task: asyncio.Task[None],
                    channel: str = normalized_id,
                ) -> None:
                    self._on_stop_task_done(channel, completed_task)

                stop_task.add_done_callback(on_stop_done)

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
            config = await self._load_config(normalized_id)
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
        )

    async def relay_completion_run(self, run: Run, reply_surface: ReplySurface) -> None:
        """Relay a background completion Run to its Session's latest Channel target."""
        await _delivery.relay_completion_run(self, run, reply_surface)

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
        adapter = self._active_adapter(normalized_id)
        return await adapter.ensure_outbound_session(platform_target, thread_id=thread_id)

    def list_channels(self) -> list[ChannelConfig]:
        """Return all persisted channels, enabled and disabled."""
        return self._storage.load_all()

    async def channel_access(self, channel_id: str) -> JsonObject:
        """Return one Channel's durable identity and per-group access state."""
        return await self._state.access_state(channel_id)

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
        self._validate_agent_exists(config.agent_id)
        self._require_idle(config.id)
        had_enabled_channels = self.has_enabled_channels()

        try:
            self._storage.get(config.id)
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

        with self._config_change(config.id):
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
            config = await self._load_config(normalized_id)
            unknown_fields = sorted(set(fields) - _MUTABLE_FIELDS)
            if unknown_fields:
                joined = ", ".join(unknown_fields)
                raise ChannelConfigError(f"Unsupported channel fields: {joined}")
            if not fields:
                return
            updated = replace(config, **fields)
            updated.validate()
            self._validate_agent_exists(updated.agent_id)
            self._preflight_adapter_start(updated)
            await self._change_config(config, updated)

    async def delete_channel(self, channel_id: str) -> None:
        """Delete one channel config and state, and stop any active adapter task.

        The adapter stops on the Event Loop; removing the Channel directory with
        its ``channel.json`` and then unregistering the Channel, which drops every
        ``channels.db`` row it owns, run as one unit on the state database's
        worker pool. Other changes of this Channel id are refused until the
        delete settles.
        """
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._require_idle(normalized_id)
        if config.platform in {"whatsapp", "slack", "mattermost"} and (
            self._is_running(normalized_id) or self._is_stop_in_progress(normalized_id)
        ):
            raise ChannelError(
                "Disable the Channel and wait for shutdown or setup to finish before removing it"
            )
        had_enabled_channels = self.has_enabled_channels()
        self.stop_channel(normalized_id)
        self._pending_start_requests.pop(normalized_id, None)
        removed = False

        def remove() -> None:
            nonlocal removed
            self._storage.delete(normalized_id)
            # Late saves of the stopping adapter are refused once unregistered.
            self._state.unregister(normalized_id)
            removed = True

        try:
            with self._config_change(normalized_id):
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
            config = await self._load_config(channel_id)
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
            config = await self._load_config(normalized_id)
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
        config = self._storage.get(normalized_id)
        self._state.migrate_group_access(normalized_id, old_chat_id, new_chat_id)
        if old_chat_id not in config.allowed_chat_ids:
            return

        migrated_ids: list[str] = []
        for allowed_chat_id in config.allowed_chat_ids:
            candidate = new_chat_id if allowed_chat_id == old_chat_id else allowed_chat_id
            if candidate not in migrated_ids:
                migrated_ids.append(candidate)
        self._storage.save(replace(config, allowed_chat_ids=migrated_ids))
        _LOGGER.info(
            "Channel allowlist migrated (channel=%s old=%s new=%s)",
            normalized_id,
            old_chat_id,
            new_chat_id,
        )

    def has_active_channels(self) -> bool:
        """Return whether at least one channel adapter task is currently running."""
        return any(not task.done() for task in self._adapter_tasks.values())

    def has_enabled_channels(self) -> bool:
        """Return whether a valid Agent owns at least one enabled Channel config."""
        for config in self._storage.load_all():
            if not config.enabled:
                continue
            try:
                self._validate_agent_exists(config.agent_id)
            except ChannelConfigError:
                continue
            return True
        return False

    def is_running(self, channel_id: str) -> bool:
        """Return whether one channel's adapter task is currently running."""
        return self._is_running(_normalize_channel_id(channel_id))

    def is_failed(self, channel_id: str) -> bool:
        """Return whether one channel is currently marked failed.

        A running adapter is never reported failed: after a failed cycle the
        recovery loop keeps retrying, and a successful attempt must show as
        healthy immediately (the raw marker stays for the next crash).
        """
        normalized_id = _normalize_channel_id(channel_id)
        if self._is_running(normalized_id):
            return False
        return normalized_id in self._failed_channels

    def failure_reason(self, channel_id: str) -> str | None:
        """Return the latest failure reason for one failed channel, if any."""
        normalized_id = _normalize_channel_id(channel_id)
        if self._is_running(normalized_id):
            return None
        return self._failure_reasons.get(normalized_id)

    def denied_chats(self, channel_id: str) -> list[DeniedChatFacts]:
        """Return one channel's recently allowlist-denied inbound chats.

        Empty when the channel is not running: the log lives on the adapter
        instance, so there is nothing to report without an active adapter.
        """
        normalized_id = _normalize_channel_id(channel_id)
        adapter = self._adapters.get(normalized_id)
        if adapter is None:
            return []
        return adapter.denied_chats()

    def _notify_tool_registration_changed(self) -> None:
        try:
            self._notify_tool_registration_changed_hook()
        except Exception:
            _LOGGER.exception("Channel tool-registration hook failed")

    def _mark_channel_failed(self, channel_id: str, reason: str) -> None:
        self._failed_channels.add(channel_id)
        self._failure_reasons[channel_id] = reason

    def _notify_tool_registration_if_changed(self, had_enabled_channels: bool) -> None:
        if had_enabled_channels == self.has_enabled_channels():
            return
        self._notify_tool_registration_changed()

    def _create_adapter(self, config: ChannelConfig) -> ChannelAdapter:
        if config.platform in {"slack", "mattermost", "whatsapp"}:
            from core.channels.mattermost import MattermostChannelAdapter
            from core.channels.slack import SlackChannelAdapter
            from core.channels.whatsapp import WhatsAppChannelAdapter

            adapter_type: Any = {
                "slack": SlackChannelAdapter,
                "mattermost": MattermostChannelAdapter,
                "whatsapp": WhatsAppChannelAdapter,
            }[config.platform]
            adapter: ChannelAdapter = adapter_type(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._credential_resolver,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
                conversation_pointers=self._state,
                received_messages=self._state,
                access_registry=self._state,
                state_dir=self._channel_root / config.id,
            )
            return adapter
        if config.platform == "discord":
            from core.channels.discord import DiscordChannelAdapter

            return DiscordChannelAdapter(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._credential_resolver,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
                conversation_pointers=self._state,
                access_registry=self._state,
            )

        if config.platform == "telegram":
            from core.channels.telegram import TelegramChannelAdapter

            return TelegramChannelAdapter(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._credential_resolver,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
                conversation_pointers=self._state,
                chat_migration_persister=partial(self.record_chat_id_migration, config.id),
                interaction_dispatcher=self._interaction_dispatcher,
                run_button_binding_registry=self._state,
                access_registry=self._state,
                update_offset_store=self._state,
            )

        raise ChannelConfigError(f"Unsupported channel platform: {config.platform}")

    def connection_status(self, channel_id: str) -> dict[str, Any]:
        from core.channels._network_adapter import NetworkChannelAdapter

        adapter = self._adapters.get(_normalize_channel_id(channel_id))
        return adapter.connection_status() if isinstance(adapter, NetworkChannelAdapter) else {}

    async def whatsapp_status(self, channel_id: str) -> dict[str, Any]:
        from core.channels._network_adapter import channel_io
        from core.channels._whatsapp_setup import bridge_ready
        from core.channels.whatsapp import WhatsAppChannelAdapter

        config = self._storage.get(channel_id)
        if config.platform != "whatsapp":
            raise ChannelConfigError("This operation requires a WhatsApp Channel")
        adapter = self._adapters.get(config.id)
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
                self._storage.get(channel_id).enabled
                or self._is_running(channel_id)
                or self._is_stop_in_progress(channel_id)
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
                stopping = self._adapter_stop_tasks.get(channel_id)
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
        self._pending_config_changes.add(channel_id)
        try:
            yield
        finally:
            self._pending_config_changes.discard(channel_id)

    async def _load_config(self, channel_id: str) -> ChannelConfig:
        """Read one ``channel.json`` on the state database's worker pool."""
        return await self._state.database.run_async(self._storage.get, channel_id)

    async def _change_config(self, previous: ChannelConfig, updated: ChannelConfig) -> None:
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
            was_running = self._is_running(channel_id) or self._is_stop_in_progress(channel_id)
            if was_running or not updated.enabled or platform_changed:
                self.stop_channel(channel_id)
            if platform_changed:
                stopping = self._adapter_stop_tasks.get(channel_id)
                if stopping is not None:
                    await asyncio.shield(stopping)
                if await self._state.database.run_async(
                    self._state.bind_platform, channel_id, updated.platform
                ):
                    _LOGGER.info("Channel state reset for a new platform (channel=%s)", channel_id)
            if updated.enabled:
                self.start_channel(channel_id, config_override=updated)
        except BaseException:
            if persisted:
                await self._restore_config(
                    previous, restart_adapter=was_running, rebind_state=platform_changed
                )
            raise
        if had_enabled_channels != has_enabled_channels:
            self._notify_tool_registration_changed()

    def _preflight_adapter_start(self, config: ChannelConfig) -> None:
        if not config.enabled:
            return
        if _get_running_loop_or_none() is None:
            return
        self._create_adapter(config)

    def _validate_agent_exists(self, agent_id: str) -> None:
        try:
            self._agent_store.get(agent_id)
        except Exception as error:
            raise ChannelConfigError(f"Unknown agent_id: {agent_id}") from error

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
        adapter never writes into state of the abandoned platform. The previous
        adapter restarts next, on the Event Loop. Restoring ``channel.json`` on
        the worker pool then runs to its end even when cancelled meanwhile.
        """
        channel_id = previous.id
        self._pending_start_requests.pop(channel_id, None)
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
        if restart_adapter and previous.enabled:
            try:
                self.start_channel(channel_id, config_override=previous)
            except Exception as error:
                _LOGGER.error(
                    "Rollback failed while restarting previous channel adapter (channel=%s): %s",
                    channel_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
        await _settle(
            self._state.database.run_async(self._storage.save, previous),
            f"Rollback failed while restoring previous channel config (channel={channel_id})",
        )
        if cancelled:
            raise asyncio.CancelledError

    def _schedule_pending_start(
        self,
        channel_id: str,
        *,
        reset_backoff: bool,
        config_override: ChannelConfig | None,
    ) -> None:
        if not self._started:
            return
        self._pending_start_requests[channel_id] = (reset_backoff, config_override)

    def _is_stop_in_progress(self, channel_id: str) -> bool:
        task = self._adapter_stop_tasks.get(channel_id)
        return task is not None and not task.done()

    async def _await_adapter_shutdown(self, channel_id: str, task: asyncio.Task[None]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The task was already popped from _adapter_tasks before this runs, so its own
            # done-callback returns early without logging: log the shutdown failure here or
            # it surfaces nowhere.
            _LOGGER.error(
                "Channel adapter shutdown raised during stop (channel=%s)",
                channel_id,
                exc_info=True,
            )

        if self._adapter_stop_tasks.get(channel_id) is asyncio.current_task():
            self._adapter_stop_tasks.pop(channel_id, None)

        pending = self._pending_start_requests.pop(channel_id, None)
        if pending is None or not self._started:
            return

        reset_backoff, config_override = pending
        if config_override is None and not self._can_restart_channel(channel_id):
            return

        try:
            self.start_channel(
                channel_id,
                reset_backoff=reset_backoff,
                config_override=config_override,
            )
        except Exception as error:
            self._mark_channel_failed(channel_id, str(error) or type(error).__name__)
            self._schedule_restart(channel_id)
            _LOGGER.error(
                "Cannot start queued channel adapter after stop completed (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _on_stop_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        if self._adapter_stop_tasks.get(channel_id) is task:
            self._adapter_stop_tasks.pop(channel_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.error(
            "Channel adapter stop task failed for channel=%s: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    def _active_adapter(self, channel_id: str) -> ChannelAdapter:
        task = self._adapter_tasks.get(channel_id)
        adapter = self._adapters.get(channel_id)
        if task is None or adapter is None or task.done():
            raise ChannelNotFoundError(f"Channel not active: {channel_id}")
        return adapter

    def _is_running(self, channel_id: str) -> bool:
        task = self._adapter_tasks.get(channel_id)
        return task is not None and not task.done()

    async def _run_adapter(self, channel_id: str, adapter: ChannelAdapter) -> None:
        try:
            await adapter.start()
        finally:
            try:
                await adapter.stop()
            except Exception as error:
                _LOGGER.error(
                    "Channel adapter stop failed for channel=%s: %s",
                    channel_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def _on_adapter_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        if self._adapter_tasks.get(channel_id) is not task:
            return

        self._adapter_tasks.pop(channel_id, None)
        self._adapters.pop(channel_id, None)
        created_at = self._adapter_task_created.pop(channel_id, None)

        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._adapter_restart_attempts.pop(channel_id, None)
            self._failed_channels.discard(channel_id)
            self._failure_reasons.pop(channel_id, None)
            return

        if created_at is not None:
            runtime_seconds = time.monotonic() - created_at
            if runtime_seconds >= _ADAPTER_HEALTHY_RUN_RESET_SECONDS:
                # The adapter was up long enough to count as healthy: treat the
                # crash as a fresh incident instead of chronic failure.
                self._adapter_restart_attempts.pop(channel_id, None)
                self._failed_channels.discard(channel_id)
                self._failure_reasons.pop(channel_id, None)

        self._failure_reasons[channel_id] = str(error)

        _LOGGER.warning(
            "Channel adapter task failed for channel=%s; scheduling restart: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self._schedule_restart(channel_id)

    def _schedule_restart(self, channel_id: str) -> None:
        if not self._started:
            return

        if self._is_stop_in_progress(channel_id):
            return

        existing_task = self._adapter_restart_tasks.get(channel_id)
        if existing_task is not None and not existing_task.done():
            return

        loop = _get_running_loop_or_none()
        if loop is None:
            _LOGGER.error(
                "Cannot restart channel adapter without a running event loop (channel=%s)",
                channel_id,
            )
            return

        restart_task = loop.create_task(
            self._restart_with_backoff(channel_id),
            name=f"channel:{channel_id}:restart",
        )
        self._adapter_restart_tasks[channel_id] = restart_task

        def on_done(completed_task: asyncio.Task[None], channel: str = channel_id) -> None:
            self._on_restart_task_done(channel, completed_task)

        restart_task.add_done_callback(on_done)

    async def _restart_with_backoff(self, channel_id: str) -> None:
        attempt = self._adapter_restart_attempts.get(channel_id, 0)
        if attempt >= _ADAPTER_RESTART_MAX_RETRIES:
            reason = self._failure_reasons.get(channel_id, "adapter restart attempts exhausted")
            self._mark_channel_failed(channel_id, reason)
            if attempt == _ADAPTER_RESTART_MAX_RETRIES:
                _LOGGER.error(
                    "Channel adapter exceeded max restart attempts and is marked failed; "
                    "recovery attempts continue at the capped backoff interval "
                    "(channel=%s, retries=%s)",
                    channel_id,
                    _ADAPTER_RESTART_MAX_RETRIES,
                )

        next_attempt = attempt + 1
        self._adapter_restart_attempts[channel_id] = next_attempt

        delay_seconds = self._restart_delay_seconds(next_attempt)
        if next_attempt <= _ADAPTER_RESTART_MAX_RETRIES:
            _LOGGER.warning(
                "Restarting channel adapter after %.1fs (channel=%s, attempt=%s/%s)",
                delay_seconds,
                channel_id,
                next_attempt,
                _ADAPTER_RESTART_MAX_RETRIES,
            )
        else:
            _LOGGER.warning(
                "Retrying failed channel adapter after %.1fs (channel=%s, recovery attempt=%s)",
                delay_seconds,
                channel_id,
                next_attempt,
            )
        await asyncio.sleep(delay_seconds)

        if not self._can_restart_channel(channel_id):
            return

        self.start_channel(channel_id, reset_backoff=False)

    def _restart_delay_seconds(self, attempt: int) -> float:
        # Cap the exponent before converting to float, even after years offline.
        delay = _ADAPTER_RESTART_INITIAL_DELAY_SECONDS * (2.0 ** min(max(attempt - 1, 0), 30))
        return float(min(_ADAPTER_RESTART_MAX_DELAY_SECONDS, delay))

    def _can_restart_channel(self, channel_id: str) -> bool:
        if not self._started:
            return False

        if self._is_running(channel_id):
            return False

        if self._is_stop_in_progress(channel_id):
            return False

        try:
            config = self._storage.get(channel_id)
        except ChannelNotFoundError:
            return False
        except ChannelError:
            _LOGGER.exception(
                "Cannot load channel config while checking restart eligibility (channel=%s)",
                channel_id,
            )
            return False

        return config.enabled

    def _cancel_restart_task(self, channel_id: str) -> None:
        task = self._adapter_restart_tasks.get(channel_id)
        if task is None:
            return

        if task is asyncio.current_task():
            return

        self._adapter_restart_tasks.pop(channel_id, None)
        if not task.done():
            task.cancel()

    def _on_restart_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        owns_restart = self._adapter_restart_tasks.get(channel_id) is task
        if owns_restart:
            self._adapter_restart_tasks.pop(channel_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        if owns_restart:
            self._mark_channel_failed(channel_id, str(error) or type(error).__name__)
            self._schedule_restart(channel_id)

        _LOGGER.error(
            "Channel adapter restart task failed for channel=%s: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


async def _settle(work: Awaitable[object], failure_message: str) -> None:
    """Await ``work`` to its end even when cancelled meanwhile, then re-raise the cancellation.

    A failure of ``work`` is logged with ``failure_message``, not raised: this
    runs compensation while another exception is already propagating.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            _LOGGER.error(
                "%s: %s",
                failure_message,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
    if cancelled:
        raise asyncio.CancelledError


def _get_running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


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
