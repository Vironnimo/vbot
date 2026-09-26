"""Application state, core event bridges and lifespan cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from core.runs import ChatRunManager
from core.statistics import StatisticsIndex
from core.storage.layout import DataDirectoryLayout
from core.utils.log_viewer import LogViewer
from server._bind import ServerBindState
from server._http_dependencies import FastAPIType, HTTPException
from server._live_record import LiveCallRecorder
from server.clients import ClientRegistry
from server.events import (
    RESOURCE_KIND_CALENDAR,
    RESOURCE_KIND_CRON,
    RESOURCE_KIND_EXTENSIONS,
    RESOURCE_KIND_TERMINALS,
    ServerEventBus,
)
from server.file_delivery import FileDelivery
from server.live import LiveCallRegistry
from server.rpc.dispatcher import dispatch_method
from server.rpc.event_bridge import (
    bridge_run_to_event_bus,
    publish_bash_process_status_changed,
    publish_resource_changed,
    publish_session_changed,
)
from server.rpc.methods import METHODS
from server.rpc.statistics_methods import statistics_service

if TYPE_CHECKING:
    from core.runtime import Runtime


JsonObject = dict[str, Any]

# Bounds ending Live calls at shutdown; each call also bounds its own close.
LIVE_CALLS_SHUTDOWN_TIMEOUT_SECONDS = 10.0

# The Session methods a Statistics index warmup reads through.
_STATISTICS_SESSION_METHODS = (
    "list_summaries",
    "list_owned_session_summaries",
    "list_history_versions",
    "get",
)


def _initialize_app_state(
    app: FastAPIType, runtime: Runtime, *, server_bind: ServerBindState
) -> None:
    app.state.runtime = runtime
    app.state.chat_runs = runtime.chat_run_manager
    app.state.event_bus = ServerEventBus()
    set_extension_change_publisher = getattr(runtime, "set_extension_change_publisher", None)
    if callable(set_extension_change_publisher):
        set_extension_change_publisher(
            lambda owner, resource, ids, revision: publish_resource_changed(
                app.state,
                RESOURCE_KIND_EXTENSIONS,
                scope={
                    "owner": owner,
                    "resource": resource,
                    "ids": list(ids),
                    "revision": revision,
                },
            )
        )
    app.state.client_registry = ClientRegistry()
    app.state.file_delivery = FileDelivery()
    app.state.run_event_bridge_run_ids = OrderedDict()
    app.state.run_event_bridge_unsubscribe = _register_run_event_bridge(app.state)
    app.state.session_title_bridge_unsubscribe = _register_session_title_bridge(app.state)
    app.state.session_completion_read_bridge_unsubscribe = _register_session_completion_read_bridge(
        app.state
    )
    app.state.cron_change_bridge_unsubscribe = _register_cron_change_bridge(app.state)
    app.state.calendar_change_bridge_unsubscribe = _register_calendar_change_bridge(app.state)
    app.state.terminal_change_bridge_unsubscribe = _register_terminal_change_bridge(app.state)
    app.state.bash_process_change_bridge_unsubscribe = _register_bash_process_change_bridge(
        app.state
    )
    app.state.chat_loop = runtime.chat_loop
    app.state.streaming_chat_loop = runtime.streaming_chat_loop
    app.state.command_dispatcher = runtime.command_dispatcher
    app.state.log_viewer = LogViewer(runtime.storage.data_dir)
    app.state.agent_delete_lock = asyncio.Lock()
    app.state.server_bind = dict(server_bind)
    app.state.live_calls = _build_live_call_registry(
        app.state, LiveCallRecorder(DataDirectoryLayout(runtime.storage.data_dir).live_calls)
    )


def _build_live_call_registry(state: Any, recorder: LiveCallRecorder) -> LiveCallRegistry:
    """Live calls run their Tools through the canonical RPC handlers."""

    async def dispatch(method: str, params: JsonObject) -> JsonObject:
        return await dispatch_method(state, method, params, METHODS)

    return LiveCallRegistry(events=state.event_bus, rpc=dispatch, recorder=recorder)


async def _shutdown_live_calls(state: Any, logger: logging.Logger) -> None:
    """End Live calls before the runtime their Tools and providers rely on closes."""
    try:
        await asyncio.wait_for(state.live_calls.aclose(), LIVE_CALLS_SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Timed out while ending Live calls")


def _register_run_event_bridge(state: Any) -> Any:
    chat_runs = _app_chat_runs(state)
    add_callback = getattr(chat_runs, "add_run_started_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(lambda run: bridge_run_to_event_bus(state, run))


def _start_statistics_warmup(state: Any) -> asyncio.Task[None] | None:
    """Reconcile the Statistics index in the background when the runtime serves it."""
    runtime = state.runtime
    sessions = getattr(runtime, "chat_sessions", None)
    agents = getattr(runtime, "agents", None)
    projects = getattr(runtime, "projects", None)
    models = getattr(runtime, "models", None)
    if not (
        isinstance(getattr(runtime, "statistics_index", None), StatisticsIndex)
        and all(callable(getattr(sessions, name, None)) for name in _STATISTICS_SESSION_METHODS)
        and callable(getattr(agents, "list", None))
        and callable(getattr(projects, "list", None))
        and callable(getattr(projects, "session_owning_agents", None))
        and callable(getattr(models, "pricing_for", None))
    ):
        return None
    service = statistics_service(state)
    return asyncio.create_task(_warm_statistics_index(service))


async def _warm_statistics_index(service: Any) -> None:
    try:
        await service.warm_index_async()
    except Exception:
        logging.getLogger("vbot.server.app").warning(
            "Statistics index warmup failed",
            exc_info=True,
        )


def _unregister_run_event_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "run_event_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.run_event_bridge_unsubscribe = None


def _register_session_title_bridge(state: Any) -> Any:
    sessions = getattr(state.runtime, "chat_sessions", None)
    add_callback = getattr(sessions, "add_title_changed_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(
        lambda address: publish_session_changed(
            state, address.project_id, address.agent_id, address.session_id
        )
    )


def _unregister_session_title_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "session_title_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.session_title_bridge_unsubscribe = None


def _register_session_completion_read_bridge(state: Any) -> Any:
    sessions = getattr(state.runtime, "chat_sessions", None)
    add_callback = getattr(sessions, "add_completion_read_callback", None)
    if not callable(add_callback):
        return None
    # The acknowledged Run id lets other windows clear their unread marker
    # without re-reading activity when they already know that completion.
    return add_callback(
        lambda address, run_id: publish_session_changed(
            state,
            address.project_id,
            address.agent_id,
            address.session_id,
            read_run_id=run_id,
        )
    )


def _unregister_session_completion_read_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "session_completion_read_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.session_completion_read_bridge_unsubscribe = None


def _register_cron_change_bridge(state: Any) -> Any:
    cron_service = getattr(state.runtime, "cron_service", None)
    add_callback = getattr(cron_service, "add_changed_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(lambda: publish_resource_changed(state, RESOURCE_KIND_CRON))


def _unregister_cron_change_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "cron_change_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.cron_change_bridge_unsubscribe = None


def _register_calendar_change_bridge(state: Any) -> Any:
    calendar_service = getattr(state.runtime, "calendar_service", None)
    add_callback = getattr(calendar_service, "add_changed_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(lambda: publish_resource_changed(state, RESOURCE_KIND_CALENDAR))


def _unregister_calendar_change_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "calendar_change_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.calendar_change_bridge_unsubscribe = None


def _register_terminal_change_bridge(state: Any) -> Any:
    manager = getattr(state.runtime, "terminal_manager", None)
    add_callback = getattr(manager, "add_changed_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(
        lambda terminal_id: publish_resource_changed(
            state,
            RESOURCE_KIND_TERMINALS,
            scope={"terminal_id": terminal_id},
        )
    )


def _unregister_terminal_change_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "terminal_change_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.terminal_change_bridge_unsubscribe = None


def _register_bash_process_change_bridge(state: Any) -> Any:
    manager = getattr(state.runtime, "process_manager", None)
    add_callback = getattr(manager, "add_terminal_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(
        lambda notification: publish_bash_process_status_changed(state, notification)
    )


def _unregister_bash_process_change_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "bash_process_change_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.bash_process_change_bridge_unsubscribe = None


def _app_chat_runs(state: Any) -> ChatRunManager:
    run_manager = getattr(state, "chat_runs", None)
    if isinstance(run_manager, ChatRunManager):
        return run_manager
    raise HTTPException(status_code=503, detail="Chat run manager is unavailable")


async def _shutdown_local_catalog_refresh(
    task: asyncio.Task[Any] | None, logger: logging.Logger
) -> None:
    """Cancel the startup catalog-refresh task so shutdown never leaves it orphaned."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("Local model catalog refresh failed during shutdown", exc_info=True)


async def _shutdown_statistics_warmup(task: asyncio.Task[None] | None) -> None:
    """Cancel the optional derived-index warmup during server shutdown."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _shutdown_log_viewer(log_viewer: LogViewer, logger: logging.Logger) -> None:
    try:
        await asyncio.wait_for(log_viewer.aclose(), timeout=1)
    except TimeoutError:
        logger.warning("Timed out while shutting down log viewer")


async def _fire_extension_startup(runtime: Any) -> None:
    fire = getattr(runtime, "fire_extension_startup", None)
    if callable(fire):
        await fire()


async def _shutdown_runtime(runtime: Any) -> None:
    aclose = getattr(runtime, "aclose", None)
    if callable(aclose):
        await aclose()
        return
    runtime.stop()


async def _shutdown_model_list_refreshes(runtime: Any) -> None:
    """Drain refresh tasks spawned by timed-out model.list requests."""
    from server.rpc.model_methods import shutdown_background_refresh_tasks

    await shutdown_background_refresh_tasks(runtime)


async def _shutdown_device_flow_engine(engine: Any, logger: logging.Logger) -> None:
    if engine is None:
        return
    aclose = getattr(engine, "aclose", None)
    if not callable(aclose):
        return
    try:
        await asyncio.wait_for(aclose(), timeout=1)
    except TimeoutError:
        logger.warning("Timed out while shutting down OAuth device flow engine")
