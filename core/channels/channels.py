"""Channel configuration, storage, and lifecycle management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
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
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_task_created: dict[str, float] = {}
        self._adapter_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_restart_attempts: dict[str, int] = {}
        self._adapter_restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_start_requests: dict[str, tuple[bool, ChannelConfig | None]] = {}
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
                    _LOGGER.error(
                        "Cannot start channel adapter during service startup (channel=%s): %s",
                        config.id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )

    def stop(self) -> None:
        """Stop all active channel adapter tasks. Idempotent."""
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

    async def aclose(self) -> None:
        """Stop all channel tasks and await their cancellation/shutdown paths."""
        tasks = [*self._adapter_stop_tasks.values(), *self._adapter_restart_tasks.values()]
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

    def restart_channel(self, channel_id: str) -> bool:
        """Rebuild one enabled channel adapter from its current config and credentials.

        Returns whether the channel is enabled and therefore received a start
        request. Disabled channels keep the updated credential for their next
        normal enable without creating an adapter.
        """
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
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

    def ensure_outbound_session(self, channel_id: str, platform_target: str) -> RouteFacts:
        """Ensure the Session mirroring an outbound target chat exists and return its route."""
        normalized_id = _normalize_channel_id(channel_id)
        if not isinstance(platform_target, str) or not platform_target:
            raise ChannelConfigError("platform_target must be a non-empty string")
        adapter = self._active_adapter(normalized_id)
        return adapter.ensure_outbound_session(platform_target)

    def list_channels(self) -> list[ChannelConfig]:
        """Return all persisted channels, enabled and disabled."""
        return self._storage.load_all()

    def channel_access(self, channel_id: str) -> JsonObject:
        """Return one Channel's durable identity and per-group access state."""
        return self._storage.access_state(channel_id)

    def set_channel_self_user_id(self, channel_id: str, user_id: str) -> JsonObject:
        """Set and return one Channel account's own platform identity."""
        return self._storage.set_self_user_id(channel_id, user_id)

    def grant_channel_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Grant one user admin access in one group and return saved state."""
        return self._storage.grant_group_admin(channel_id, access_scope_id, user_id)

    def revoke_channel_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Revoke one additional group admin and return saved state."""
        return self._storage.revoke_group_admin(channel_id, access_scope_id, user_id)

    def create_channel(self, config: ChannelConfig) -> None:
        """Validate and persist one channel config, then start it when enabled."""
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()
        self._validate_agent_exists(config.agent_id)
        had_enabled_channels = self.has_enabled_channels()

        try:
            self._storage.get(config.id)
        except ChannelNotFoundError:
            pass
        else:
            raise ChannelConfigError(f"Channel already exists: {config.id}")

        self._preflight_adapter_start(config)
        self._storage.save(config)
        if config.enabled:
            try:
                self.start_channel(config.id, config_override=config)
            except Exception:
                self._rollback_created_channel(config.id)
                raise
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def update_channel(self, channel_id: str, **fields: Any) -> None:
        """Update mutable fields, persist, and restart when currently running."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)

        unknown_fields = sorted(set(fields) - _MUTABLE_FIELDS)
        if unknown_fields:
            joined = ", ".join(unknown_fields)
            raise ChannelConfigError(f"Unsupported channel fields: {joined}")
        if not fields:
            return

        had_enabled_channels = self.has_enabled_channels()
        updated = replace(config, **fields)
        updated.validate()
        self._validate_agent_exists(updated.agent_id)
        self._preflight_adapter_start(updated)

        was_running = self._is_running(normalized_id) or self._is_stop_in_progress(normalized_id)

        if was_running:
            self.stop_channel(normalized_id)

        self._storage.save(updated)
        try:
            if updated.enabled:
                self.start_channel(normalized_id, config_override=updated)
            else:
                self._pending_start_requests.pop(normalized_id, None)
        except Exception:
            self._rollback_updated_channel(normalized_id, config, was_running)
            raise
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def delete_channel(self, channel_id: str) -> None:
        """Delete one channel config and stop any active adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        had_enabled_channels = self.has_enabled_channels()
        self.stop_channel(normalized_id)
        self._pending_start_requests.pop(normalized_id, None)
        self._storage.delete(normalized_id)
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def enable_channel(self, channel_id: str) -> None:
        """Enable one channel and start its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._validate_agent_exists(config.agent_id)
        had_enabled_channels = self.has_enabled_channels()
        if not config.enabled:
            self._storage.save(replace(config, enabled=True))
        try:
            self.start_channel(normalized_id)
        finally:
            self._notify_tool_registration_if_changed(had_enabled_channels)

    def disable_channel(self, channel_id: str) -> None:
        """Disable one channel and stop its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        had_enabled_channels = self.has_enabled_channels()
        if config.enabled:
            self._storage.save(replace(config, enabled=False))
        self.stop_channel(normalized_id)
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def record_chat_id_migration(self, channel_id: str, old_chat_id: str, new_chat_id: str) -> None:
        """Persist a platform-side chat-id migration into the channel's allowlist.

        Swaps the old chat id for the new one in ``allowed_chat_ids`` and saves the
        config without restarting the adapter — the running adapter already swapped
        its in-memory allowlist and a restart would drop queued conversation work.
        Idempotent: a config that no longer lists the old id is left untouched.
        """
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._storage.migrate_group_access(normalized_id, old_chat_id, new_chat_id)
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
        if config.platform == "discord":
            from core.channels.discord import DiscordChannelAdapter

            return DiscordChannelAdapter(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._credential_resolver,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
                access_registry=self._storage,
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
                chat_migration_persister=partial(self.record_chat_id_migration, config.id),
                interaction_dispatcher=self._interaction_dispatcher,
                run_button_binding_registry=self._storage,
                access_registry=self._storage,
                update_offset_store=self._storage,
            )

        raise ChannelConfigError(f"Unsupported channel platform: {config.platform}")

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

    def _rollback_created_channel(self, channel_id: str) -> None:
        try:
            self._storage.delete(channel_id)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while deleting newly created channel config (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _rollback_updated_channel(
        self,
        channel_id: str,
        previous_config: ChannelConfig,
        was_running: bool,
    ) -> None:
        self._pending_start_requests.pop(channel_id, None)
        try:
            self._storage.save(previous_config)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while restoring previous channel config (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

        if not was_running or not previous_config.enabled:
            return

        try:
            self.start_channel(channel_id, config_override=previous_config)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while restarting previous channel adapter (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

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
        delay = _ADAPTER_RESTART_INITIAL_DELAY_SECONDS * float(2 ** (attempt - 1))
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
        task = self._adapter_restart_tasks.pop(channel_id, None)
        if task is None or task.done():
            return

        if task is asyncio.current_task():
            return

        task.cancel()

    def _on_restart_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        if self._adapter_restart_tasks.get(channel_id) is task:
            self._adapter_restart_tasks.pop(channel_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.error(
            "Channel adapter restart task failed for channel=%s: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


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
