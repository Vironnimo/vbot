"""Channel adapter supervision and engine lifetime."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial

from core.channels.adapter import (
    ChannelAdapter,
    DeniedChatFacts,
)
from core.channels.config import (
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    _normalize_channel_id,
)
from core.channels.engine import ChannelConversationEngine
from core.channels.storage import ChannelStorage
from core.utils.logging import get_logger

_ADAPTER_RESTART_INITIAL_DELAY_SECONDS = 1.0
_ADAPTER_RESTART_MAX_DELAY_SECONDS = 30.0
_ADAPTER_RESTART_MAX_RETRIES = 3
_ADAPTER_HEALTHY_RUN_RESET_SECONDS = 300.0


@dataclass
class _AdapterOutage:
    """A supervised outage: since when the Channel is down and how many restarts ran.

    It starts with the first failure of a running Channel and ends with its next
    connection or its stop. Only that first failure logs a WARNING; the failures
    and restarts that follow while it lasts log at DEBUG.
    """

    since: float
    restarts: int = 0


_LOGGER = get_logger("channels")


class ChannelConnections:
    """Supervise each adapter and its conversation engine across reconnects and stops."""

    def __init__(
        self,
        storage: ChannelStorage,
        *,
        get_channel: Callable[[str], Awaitable[ChannelConfig]],
        validate_agent: Callable[[str], None],
        new_engine: Callable[[ChannelConfig], ChannelConversationEngine],
        create_adapter: Callable[[ChannelConfig], ChannelAdapter],
        recover_observations: Callable[..., None],
    ) -> None:
        self._storage = storage
        self._get_channel = get_channel
        self._validate_agent_exists = validate_agent
        self._new_engine = new_engine
        self._create_adapter = create_adapter
        self._recover_observations = recover_observations
        self._engines: dict[str, ChannelConversationEngine] = {}
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_task_created: dict[str, float] = {}
        self._adapter_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_restart_attempts: dict[str, int] = {}
        self._adapter_restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_outages: dict[str, _AdapterOutage] = {}
        self._connected_channels: set[str] = set()
        self._pending_start_requests: dict[str, tuple[bool, ChannelConfig | None]] = {}
        self._failed_channels: set[str] = set()
        self._failure_reasons: dict[str, str] = {}
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def pending_tasks(self) -> list[asyncio.Task[None]]:
        return [*self._adapter_stop_tasks.values(), *self._adapter_restart_tasks.values()]

    def stopping(self, channel_id: str) -> asyncio.Task[None] | None:
        return self._adapter_stop_tasks.get(channel_id)

    def adapter(self, channel_id: str) -> ChannelAdapter | None:
        return self._adapters.get(channel_id)

    def cancel_pending_start(self, channel_id: str) -> None:
        self._pending_start_requests.pop(channel_id, None)

    def observation_owners(self) -> set[str]:
        return {engine.observation_owner for engine in self._engines.values()}

    def start(self) -> None:
        """Start the channel service and launch enabled channel adapter tasks."""
        if self._started:
            return

        self._started = True
        self._recover_observations()
        for config in self._storage.snapshot():
            if config.enabled:
                try:
                    self.start_channel(config.id)
                except Exception as error:
                    reason = str(error) or type(error).__name__
                    self.mark_failed(config.id, reason)
                    self.schedule_restart(config.id)
                    if isinstance(error, ChannelError):
                        # Expected, such as a missing credential: recovery retries it.
                        _LOGGER.warning(
                            "Cannot start channel adapter during service startup "
                            "(channel=%s error_type=%s): %s",
                            config.id,
                            type(error).__name__,
                            error,
                        )
                    else:
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
            and not self._engines
        ):
            return

        self._pending_start_requests.clear()
        for channel_id in list(self._adapter_restart_tasks):
            self._cancel_restart_task(channel_id)
        for channel_id in list(dict.fromkeys([*self._adapter_tasks, *self._engines])):
            self.stop_channel(channel_id)
        self._adapter_restart_attempts.clear()
        self._adapter_outages.clear()
        self._connected_channels.clear()
        self._adapter_task_created.clear()
        self._failed_channels.clear()
        self._failure_reasons.clear()
        self._started = False

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
            config = self._storage.cached(normalized_id)

        self._validate_agent_exists(config.agent_id)
        self._cancel_restart_task(normalized_id)
        if reset_backoff:
            self._adapter_restart_attempts.pop(normalized_id, None)
            self._failed_channels.discard(normalized_id)
            self._failure_reasons.pop(normalized_id, None)

        if self.is_stopping(normalized_id):
            # The old adapter can still persist a Telegram chat-id migration
            # while draining. Resolve its replacement from the config after
            # shutdown, rather than retaining this pre-drain snapshot.
            self._schedule_pending_start(
                normalized_id,
                reset_backoff=reset_backoff,
                config_override=None,
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

        engine = self._engines.get(normalized_id)
        if engine is not None and engine.config != config:
            # The configuration changed while no adapter ran. The old engine ends
            # first, so its owed replies pass to the new one exactly once.
            self.stop_channel(normalized_id)
            self._schedule_pending_start(
                normalized_id, reset_backoff=reset_backoff, config_override=config_override
            )
            return
        if engine is None:
            engine = self._new_engine(config)
            self._engines[normalized_id] = engine
            engine.start()

        adapter = self._create_adapter(config)
        adapter.observe_connection(partial(self._on_adapter_connected, normalized_id, adapter))
        task = loop.create_task(
            self._run_adapter(normalized_id, adapter, engine), name=f"channel:{normalized_id}"
        )
        self._adapters[normalized_id] = adapter
        self._connected_channels.discard(normalized_id)
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
        self._adapter_outages.pop(normalized_id, None)
        self._failed_channels.discard(normalized_id)
        self._failure_reasons.pop(normalized_id, None)

        task = self._adapter_tasks.pop(normalized_id, None)
        self._adapters.pop(normalized_id, None)
        self._connected_channels.discard(normalized_id)
        self._adapter_task_created.pop(normalized_id, None)
        engine = self._engines.pop(normalized_id, None)
        running = task is not None and not task.done()

        if running or engine is not None:
            if task is not None and running:
                task.cancel()
            loop = _get_running_loop_or_none()
            if loop is not None:
                stop_task = loop.create_task(
                    self._await_adapter_shutdown(normalized_id, task if running else None, engine),
                    name=f"channel:{normalized_id}:stop",
                )
                self._adapter_stop_tasks[normalized_id] = stop_task

                def on_stop_done(
                    completed_task: asyncio.Task[None],
                    channel: str = normalized_id,
                ) -> None:
                    self._on_stop_task_done(channel, completed_task)

                stop_task.add_done_callback(on_stop_done)

    def has_active_channels(self) -> bool:
        """Return whether at least one channel adapter task is currently running."""
        return any(not task.done() for task in self._adapter_tasks.values())

    def is_failed(self, channel_id: str) -> bool:
        """Return whether one channel is currently marked failed.

        A running adapter is never reported failed: after a failed cycle the
        recovery loop keeps retrying, and a successful attempt must show as
        healthy immediately (the raw marker stays for the next crash).
        """
        normalized_id = _normalize_channel_id(channel_id)
        if self.is_running(normalized_id):
            return False
        return normalized_id in self._failed_channels

    def failure_reason(self, channel_id: str) -> str | None:
        """Return the latest failure reason for one failed channel, if any."""
        normalized_id = _normalize_channel_id(channel_id)
        if self.is_running(normalized_id):
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

    def mark_failed(self, channel_id: str, reason: str) -> None:
        self._failed_channels.add(channel_id)
        self._failure_reasons[channel_id] = reason

    def engine_for(self, config: ChannelConfig) -> ChannelConversationEngine:
        """The started Channel's engine for its configuration, else an unstarted one.

        An adapter built only to validate a configuration gets an engine of its own.
        """
        engine = self._engines.get(config.id)
        if engine is not None and engine.config == config:
            return engine
        return self._new_engine(config)

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

    def is_stopping(self, channel_id: str) -> bool:
        task = self._adapter_stop_tasks.get(channel_id)
        return task is not None and not task.done()

    async def _await_adapter_shutdown(
        self,
        channel_id: str,
        task: asyncio.Task[None] | None,
        engine: ChannelConversationEngine | None,
    ) -> None:
        """Wait for the adapter to stop, then end the Channel's engine."""
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                _LOGGER.info("Channel adapter stopped (channel=%s)", channel_id)
            except Exception:
                # The task was already popped from _adapter_tasks before this runs, so its
                # own done-callback returns early without logging: log the shutdown failure
                # here or it surfaces nowhere.
                _LOGGER.error(
                    "Channel adapter shutdown raised during stop (channel=%s)",
                    channel_id,
                    exc_info=True,
                )
            else:
                _LOGGER.info("Channel adapter stopped (channel=%s)", channel_id)
        if engine is not None:
            # After the adapter: its last inbound handling still reached the engine.
            try:
                await engine.stop()
                if self._started:
                    self._recover_observations(engine)
            except Exception:
                _LOGGER.error(
                    "Channel conversation engine stop failed (channel=%s)",
                    channel_id,
                    exc_info=True,
                )

        self._adapter_stop_tasks.pop(channel_id, None)

        pending = self._pending_start_requests.pop(channel_id, None)
        if pending is None or not self._started:
            return

        reset_backoff, config_override = pending
        if config_override is None and not self._can_restart_channel(channel_id):
            return

        try:
            if config_override is None:
                config_override = await self._get_channel(channel_id)
            self.start_channel(
                channel_id,
                reset_backoff=reset_backoff,
                config_override=config_override,
            )
        except Exception as error:
            self.mark_failed(channel_id, str(error) or type(error).__name__)
            self.schedule_restart(channel_id)
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

    def active_engine(self, channel_id: str) -> ChannelConversationEngine:
        """The engine of a started Channel, also while its adapter reconnects."""
        engine = self._engines.get(channel_id)
        if engine is None:
            raise ChannelNotFoundError(f"Channel not active: {channel_id}")
        return engine

    def active_adapter(self, channel_id: str) -> ChannelAdapter:
        task = self._adapter_tasks.get(channel_id)
        adapter = self._adapters.get(channel_id)
        if task is None or adapter is None or task.done():
            raise ChannelNotFoundError(f"Channel not active: {channel_id}")
        return adapter

    def is_running(self, channel_id: str) -> bool:
        task = self._adapter_tasks.get(channel_id)
        return task is not None and not task.done()

    async def _run_adapter(
        self, channel_id: str, adapter: ChannelAdapter, engine: ChannelConversationEngine
    ) -> None:
        try:
            await adapter.start()
        finally:
            # Replies wait for the next adapter instead of failing on this one.
            engine.set_connected(False)
            try:
                await adapter.stop()
            except Exception as error:
                _LOGGER.error(
                    "Channel adapter stop failed for channel=%s: %s",
                    channel_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def _on_adapter_connected(self, channel_id: str, adapter: ChannelAdapter) -> None:
        """Log the current adapter's first connection as its start or its recovery."""
        if self._adapters.get(channel_id) is not adapter:
            return
        engine = self._engines.get(channel_id)
        if engine is not None:
            engine.set_connected(True)
        if channel_id in self._connected_channels:
            _LOGGER.debug("Channel adapter reconnected (channel=%s)", channel_id)
            return
        self._connected_channels.add(channel_id)
        outage = self._adapter_outages.pop(channel_id, None)
        if outage is None:
            _LOGGER.info(
                "Channel adapter started (channel=%s platform=%s)", channel_id, adapter.platform
            )
            return
        _LOGGER.info(
            "Channel adapter recovered (channel=%s platform=%s attempts=%d down_for=%.1fs)",
            channel_id,
            adapter.platform,
            outage.restarts,
            time.monotonic() - outage.since,
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

        if channel_id in self._adapter_outages:
            _LOGGER.debug(
                "Channel adapter failed again; restart scheduled (channel=%s attempts=%d error=%s)",
                channel_id,
                self._adapter_restart_attempts.get(channel_id, 0),
                error,
            )
        else:
            _LOGGER.warning(
                "Channel adapter failed; restart scheduled (channel=%s error=%s)",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        self.schedule_restart(channel_id)

    def schedule_restart(self, channel_id: str) -> None:
        if not self._started:
            return

        if self.is_stopping(channel_id):
            return

        existing_task = self._adapter_restart_tasks.get(channel_id)
        if existing_task is not None and not existing_task.done():
            return

        self._adapter_outages.setdefault(channel_id, _AdapterOutage(since=time.monotonic()))
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
            self.mark_failed(channel_id, reason)
            if attempt == _ADAPTER_RESTART_MAX_RETRIES:
                _LOGGER.warning(
                    "Channel adapter exceeded max restart attempts and is marked failed; "
                    "recovery attempts continue at the capped backoff interval "
                    "(channel=%s retries=%d)",
                    channel_id,
                    _ADAPTER_RESTART_MAX_RETRIES,
                )

        next_attempt = attempt + 1
        self._adapter_restart_attempts[channel_id] = next_attempt

        delay_seconds = self._restart_delay_seconds(next_attempt)
        _LOGGER.debug(
            "Channel adapter restart scheduled (channel=%s attempt=%d fast_retries=%d delay=%.1fs)",
            channel_id,
            next_attempt,
            _ADAPTER_RESTART_MAX_RETRIES,
            delay_seconds,
        )
        await asyncio.sleep(delay_seconds)

        if not self._can_restart_channel(channel_id):
            return

        outage = self._adapter_outages.get(channel_id)
        if outage is not None:
            outage.restarts += 1
        config = await self._get_channel(channel_id)
        self.start_channel(channel_id, reset_backoff=False, config_override=config)

    def _restart_delay_seconds(self, attempt: int) -> float:
        # Cap the exponent before converting to float, even after years offline.
        delay = _ADAPTER_RESTART_INITIAL_DELAY_SECONDS * (2.0 ** min(max(attempt - 1, 0), 30))
        return float(min(_ADAPTER_RESTART_MAX_DELAY_SECONDS, delay))

    def _can_restart_channel(self, channel_id: str) -> bool:
        if not self._started:
            return False

        if self.is_running(channel_id):
            return False

        if self.is_stopping(channel_id):
            return False

        try:
            config = self._storage.cached(channel_id)
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

        # A restart runs inside an outage whose first failure was logged already.
        if channel_id in self._adapter_outages:
            _LOGGER.debug(
                "Channel adapter restart failed (channel=%s attempts=%d error=%s)",
                channel_id,
                self._adapter_restart_attempts.get(channel_id, 0),
                error,
            )
        else:
            _LOGGER.error(
                "Channel adapter restart failed (channel=%s error=%s)",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        if owns_restart:
            self.mark_failed(channel_id, str(error) or type(error).__name__)
            self.schedule_restart(channel_id)


def _get_running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
