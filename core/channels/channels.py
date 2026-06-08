"""Channel configuration, storage, and lifecycle management."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.attachments import AttachmentStore
from core.channels.adapter import ChannelAdapter, FileData, RouteFacts
from core.settings import SettingsValidationError, load_validated_channel_json
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels")

_CHANNEL_CONFIG_FILENAME = "channel.json"
_DEFAULT_DM_SCOPE = "per_conversation"
_ALLOWED_DM_SCOPES = frozenset(("per_conversation", "main", "per_peer", "per_account_channel_peer"))
_ALLOWED_PLATFORMS = frozenset(("telegram",))
_CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ADAPTER_RESTART_INITIAL_DELAY_SECONDS = 1.0
_ADAPTER_RESTART_MAX_DELAY_SECONDS = 30.0
_ADAPTER_RESTART_MAX_RETRIES = 3
_MUTABLE_FIELDS = frozenset(
    (
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "enabled",
    )
)


class ChannelError(VBotError):
    """Base class for expected channel-domain errors."""


class ChannelNotFoundError(ChannelError):
    """Raised when a channel id is unknown."""


class ChannelConfigError(ChannelError):
    """Raised when channel config data is invalid."""


@dataclass(slots=True)
class ChannelConfig:
    """Persisted channel configuration."""

    id: str
    platform: str
    agent_id: str
    dm_scope: str = _DEFAULT_DM_SCOPE
    allowed_chat_ids: list[int] = field(default_factory=list)
    token_env_var: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize one channel config to JSON-compatible data."""
        return {
            "id": self.id,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "dm_scope": self.dm_scope,
            "allowed_chat_ids": list(self.allowed_chat_ids),
            "token_env_var": self.token_env_var,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChannelConfig:
        """Create one ChannelConfig from persisted JSON data."""
        config = cls(
            id=payload.get("id", ""),
            platform=payload.get("platform", ""),
            agent_id=payload.get("agent_id", ""),
            dm_scope=payload.get("dm_scope", _DEFAULT_DM_SCOPE),
            allowed_chat_ids=list(payload.get("allowed_chat_ids") or []),
            token_env_var=payload.get("token_env_var", ""),
            enabled=payload.get("enabled", True),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Validate and normalize one channel config in-place."""
        if not isinstance(self.id, str) or not self.id.strip():
            raise ChannelConfigError("id must be a non-empty string")
        self.id = self.id.strip()
        if _CHANNEL_ID_PATTERN.fullmatch(self.id) is None:
            raise ChannelConfigError(
                "id must contain only letters, numbers, underscore, and hyphen"
            )

        if not isinstance(self.platform, str) or self.platform not in _ALLOWED_PLATFORMS:
            platforms = ", ".join(sorted(_ALLOWED_PLATFORMS))
            raise ChannelConfigError(f"platform must be one of: {platforms}")

        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ChannelConfigError("agent_id must be a non-empty string")
        self.agent_id = self.agent_id.strip()

        if not isinstance(self.dm_scope, str) or self.dm_scope not in _ALLOWED_DM_SCOPES:
            scopes = ", ".join(sorted(_ALLOWED_DM_SCOPES))
            raise ChannelConfigError(f"dm_scope must be one of: {scopes}")

        if not isinstance(self.allowed_chat_ids, list):
            raise ChannelConfigError("allowed_chat_ids must be a list of integers")
        normalized_chat_ids: list[int] = []
        for chat_id in self.allowed_chat_ids:
            if not isinstance(chat_id, int) or isinstance(chat_id, bool):
                raise ChannelConfigError("allowed_chat_ids must contain integers only")
            normalized_chat_ids.append(chat_id)
        self.allowed_chat_ids = normalized_chat_ids

        if not isinstance(self.token_env_var, str) or not self.token_env_var.strip():
            raise ChannelConfigError("token_env_var must be a non-empty string")
        self.token_env_var = self.token_env_var.strip()

        if not isinstance(self.enabled, bool):
            raise ChannelConfigError("enabled must be a boolean")


class ChannelStorage:
    """Persist channel configs under <data_root>/channels/<id>/channel.json."""

    def __init__(self, data_root: str | Path) -> None:
        self._data_root = Path(data_root).expanduser()
        self._channels_dir = self._data_root / "channels"

    def load_all(self) -> list[ChannelConfig]:
        """Load all persisted channel configs in stable id-order."""
        if not self._channels_dir.exists():
            return []

        configs: list[ChannelConfig] = []
        for channel_dir in sorted(self._channels_dir.iterdir(), key=lambda path: path.name):
            if not channel_dir.is_dir():
                continue
            config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
            if not config_path.is_file():
                continue
            configs.append(self._read_config(config_path))

        return sorted(configs, key=lambda config: config.id)

    def save(self, config: ChannelConfig) -> None:
        """Persist one channel config using atomic replace."""
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()

        channel_dir = self._channel_dir(config.id)
        config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
        temp_path = config_path.with_name(f"{config_path.name}.{uuid4().hex}.tmp")

        try:
            channel_dir.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(config.to_dict(), file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temp_path, config_path)
        except OSError as error:
            self._safe_remove_temporary_file(temp_path)
            raise ChannelError(f"Cannot write {config_path}: {error}") from error

    def delete(self, channel_id: str) -> None:
        """Delete one channel directory from storage."""
        normalized_id = _normalize_channel_id(channel_id)
        channel_dir = self._channel_dir(normalized_id)
        if not channel_dir.exists():
            raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
        if not channel_dir.is_dir():
            raise ChannelError(f"Channel path is not a directory: {channel_dir}")
        try:
            shutil.rmtree(channel_dir)
        except OSError as error:
            raise ChannelError(f"Cannot delete channel directory {channel_dir}: {error}") from error

    def get(self, channel_id: str) -> ChannelConfig:
        """Load one channel config by id."""
        normalized_id = _normalize_channel_id(channel_id)
        config_path = self._channel_dir(normalized_id) / _CHANNEL_CONFIG_FILENAME
        if not config_path.is_file():
            raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
        return self._read_config(config_path)

    def _channel_dir(self, channel_id: str) -> Path:
        return self._channels_dir / channel_id

    def _read_config(self, config_path: Path) -> ChannelConfig:
        try:
            payload = load_validated_channel_json(config_path)
        except SettingsValidationError as error:
            raise ChannelConfigError(str(error)) from error

        config = ChannelConfig.from_dict(payload)
        if config.id != config_path.parent.name:
            raise ChannelConfigError(
                "Channel id mismatch for "
                f"{config_path}: expected {config_path.parent.name}, got {config.id}"
            )
        return config

    @staticmethod
    def _safe_remove_temporary_file(temp_path: Path) -> None:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            return


class ChannelService:
    """Manage channel config CRUD and adapter task lifecycle."""

    def __init__(
        self,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        runtime: object,
        attachment_store: AttachmentStore | None = None,
        *,
        command_dispatcher: CommandDispatcher,
    ) -> None:
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._runtime = runtime
        self._attachment_store = attachment_store
        self._command_dispatcher = command_dispatcher
        self._storage = ChannelStorage(_resolve_runtime_data_root(runtime))
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_tasks: dict[str, asyncio.Task[None]] = {}
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
                except ChannelError as error:
                    self._mark_channel_failed(config.id, str(error))
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

        had_active_channels = self.has_active_channels()
        adapter = self._create_adapter(config)
        task = loop.create_task(
            self._run_adapter(normalized_id, adapter), name=f"channel:{normalized_id}"
        )
        self._adapters[normalized_id] = adapter
        self._adapter_tasks[normalized_id] = task

        def on_done(completed_task: asyncio.Task[None], channel: str = normalized_id) -> None:
            self._on_adapter_task_done(channel, completed_task)

        task.add_done_callback(on_done)
        self._notify_tool_registration_if_changed(had_active_channels)

    def stop_channel(self, channel_id: str) -> None:
        """Stop one running channel adapter task when active."""
        normalized_id = _normalize_channel_id(channel_id)
        had_active_channels = self.has_active_channels()

        self._pending_start_requests.pop(normalized_id, None)
        self._cancel_restart_task(normalized_id)
        self._adapter_restart_attempts.pop(normalized_id, None)
        self._failed_channels.discard(normalized_id)
        self._failure_reasons.pop(normalized_id, None)

        task = self._adapter_tasks.pop(normalized_id, None)
        self._adapters.pop(normalized_id, None)

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

        self._notify_tool_registration_if_changed(had_active_channels)

    async def send(
        self,
        channel_id: str,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        """Delegate an outbound send to a running channel adapter."""
        normalized_id = _normalize_channel_id(channel_id)
        if not isinstance(platform_target, str) or not platform_target:
            raise ChannelConfigError("platform_target must be a non-empty string")

        normalized_message: str | None
        if message is None:
            normalized_message = None
        elif isinstance(message, str) and message.strip():
            normalized_message = message.strip()
        else:
            raise ChannelConfigError("message must be a non-empty string when provided")

        normalized_files: list[FileData] | None
        if files is None:
            normalized_files = None
        elif not isinstance(files, list):
            raise ChannelConfigError("files must be a list of FileData when provided")
        else:
            normalized_files = []
            for file_data in files:
                if not isinstance(file_data, FileData):
                    raise ChannelConfigError("files must contain FileData values only")
                normalized_files.append(file_data)

        if normalized_message is None and not normalized_files:
            raise ChannelConfigError("at least one of message or files must be provided")

        adapter = self._active_adapter(normalized_id)
        if normalized_files is None:
            await adapter.send(normalized_message, platform_target)
            return

        await adapter.send(normalized_message, platform_target, files=normalized_files)

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

    def create_channel(self, config: ChannelConfig) -> None:
        """Validate and persist one channel config, then start it when enabled."""
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()
        self._validate_agent_exists(config.agent_id)

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

    def delete_channel(self, channel_id: str) -> None:
        """Delete one channel config and stop any active adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        self.stop_channel(normalized_id)
        self._pending_start_requests.pop(normalized_id, None)
        self._storage.delete(normalized_id)

    def enable_channel(self, channel_id: str) -> None:
        """Enable one channel and start its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._validate_agent_exists(config.agent_id)
        if not config.enabled:
            self._storage.save(replace(config, enabled=True))
        self.start_channel(normalized_id)

    def disable_channel(self, channel_id: str) -> None:
        """Disable one channel and stop its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        if config.enabled:
            self._storage.save(replace(config, enabled=False))
        self.stop_channel(normalized_id)

    def has_active_channels(self) -> bool:
        """Return whether at least one channel adapter task is currently running."""
        return any(not task.done() for task in self._adapter_tasks.values())

    def is_failed(self, channel_id: str) -> bool:
        """Return whether one channel is currently marked failed."""
        normalized_id = _normalize_channel_id(channel_id)
        return normalized_id in self._failed_channels

    def failure_reason(self, channel_id: str) -> str | None:
        """Return the latest failure reason for one failed channel, if any."""
        normalized_id = _normalize_channel_id(channel_id)
        return self._failure_reasons.get(normalized_id)

    def _notify_tool_registration_changed(self) -> None:
        try:
            self._notify_tool_registration_changed_hook()
        except Exception:
            _LOGGER.exception("Channel tool-registration hook failed")

    def _mark_channel_failed(self, channel_id: str, reason: str) -> None:
        self._failed_channels.add(channel_id)
        self._failure_reasons[channel_id] = reason

    def _notify_tool_registration_if_changed(self, had_active_channels: bool) -> None:
        if had_active_channels == self.has_active_channels():
            return
        self._notify_tool_registration_changed()

    def _create_adapter(self, config: ChannelConfig) -> ChannelAdapter:
        if config.platform == "telegram":
            from core.channels.telegram import TelegramChannelAdapter

            return TelegramChannelAdapter(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._runtime,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
            )

        raise ChannelConfigError(f"Unsupported channel platform: {config.platform}")

    def _preflight_adapter_start(self, config: ChannelConfig) -> None:
        if not config.enabled:
            return
        if _get_running_loop_or_none() is None:
            return
        self._create_adapter(config)

    def _validate_agent_exists(self, agent_id: str) -> None:
        runtime_obj = cast(Any, self._runtime)
        agents = getattr(runtime_obj, "_agents", None)
        if agents is None:
            try:
                agents = runtime_obj.agents
            except Exception:
                agents = None

        if agents is None:
            raise ChannelConfigError("Runtime agent store is unavailable")
        try:
            agents.get(agent_id)
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
            pass

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

    def _cancel_stop_task(self, channel_id: str) -> None:
        task = self._adapter_stop_tasks.pop(channel_id, None)
        if task is None or task.done():
            return

        if task is asyncio.current_task():
            return

        task.cancel()

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

        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._adapter_restart_attempts.pop(channel_id, None)
            self._failed_channels.discard(channel_id)
            self._failure_reasons.pop(channel_id, None)
            self._notify_tool_registration_changed()
            return

        self._failure_reasons[channel_id] = str(error)

        _LOGGER.warning(
            "Channel adapter task failed for channel=%s; scheduling restart: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self._schedule_restart(channel_id)
        self._notify_tool_registration_changed()

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
            _LOGGER.error(
                "Channel adapter exceeded max restart attempts and is marked failed "
                "(channel=%s, retries=%s)",
                channel_id,
                _ADAPTER_RESTART_MAX_RETRIES,
            )
            return

        next_attempt = attempt + 1
        self._adapter_restart_attempts[channel_id] = next_attempt

        delay_seconds = self._restart_delay_seconds(next_attempt)
        _LOGGER.warning(
            "Restarting channel adapter after %.1fs (channel=%s, attempt=%s/%s)",
            delay_seconds,
            channel_id,
            next_attempt,
            _ADAPTER_RESTART_MAX_RETRIES,
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


def _normalize_channel_id(channel_id: str) -> str:
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise ChannelConfigError("channel_id must be a non-empty string")
    return channel_id.strip()


def _resolve_runtime_data_root(runtime: object) -> Path:
    try:
        runtime_obj = cast(Any, runtime)
        storage = runtime_obj.storage
    except Exception:
        storage = None
    data_dir = getattr(storage, "data_dir", None)
    if data_dir is not None:
        return Path(data_dir).expanduser()

    runtime_data_dir = getattr(runtime, "data_dir", None)
    if runtime_data_dir is not None:
        return Path(runtime_data_dir).expanduser()

    runtime_private_data_dir = getattr(runtime, "_data_dir", None)
    if runtime_private_data_dir is not None:
        return Path(runtime_private_data_dir).expanduser()

    raise ChannelConfigError("Runtime does not expose a data directory")


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
    "ChannelConfig",
    "ChannelConfigError",
    "ChannelError",
    "ChannelNotFoundError",
    "ChannelService",
    "ChannelStorage",
]
