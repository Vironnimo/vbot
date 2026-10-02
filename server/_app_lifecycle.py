"""Application state, core event bridges and lifespan cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from core.runs import ChatRunManager
from core.storage.layout import DataDirectoryLayout
from core.utils.log_viewer import LogViewer
from server._bind import ServerBindState
from server._http_dependencies import FastAPIType
from server.activity import ActivityMonitor
from server.clients import ClientRegistry
from server.events import (
    ACTIVITY_STATUS_EVENT,
    RESOURCE_KIND_ARCHIVE,
    RESOURCE_KIND_CALENDAR,
    RESOURCE_KIND_CRON,
    RESOURCE_KIND_EXTENSIONS,
    RESOURCE_KIND_SKILLS,
    RESOURCE_KIND_TERMINALS,
    ServerEventBus,
)
from server.file_delivery import FileDelivery
from server.live._record import LiveCallRecorder
from server.live.registry import LiveCallRegistry
from server.rpc.dispatcher import dispatch_method
from server.rpc.event_bridge import (
    bridge_run_to_event_bus,
    publish_bash_process_status_changed,
    publish_recall_index_status,
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


def _initialize_app_state(
    app: FastAPIType, runtime: Runtime, *, server_bind: ServerBindState
) -> None:
    app.state.runtime = runtime
    app.state.chat_runs = runtime.chat_run_manager
    app.state.event_bus = ServerEventBus()
    runtime.set_extension_change_publisher(
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
    app.state.archive_change_bridge_unsubscribe = _register_archive_change_bridge(app.state)
    app.state.skill_change_bridge_unsubscribe = _register_skill_change_bridge(app.state)
    app.state.terminal_change_bridge_unsubscribe = _register_terminal_change_bridge(app.state)
    app.state.bash_process_change_bridge_unsubscribe = _register_bash_process_change_bridge(
        app.state
    )
    app.state.activity = ActivityMonitor(
        runtime,
        lambda activities: app.state.event_bus.publish(
            ACTIVITY_STATUS_EVENT, {"activities": activities}
        ),
    )
    app.state.recall_index_status_bridge_unsubscribe = _register_recall_index_status_bridge(
        app.state
    )
    app.state.chat_loop = runtime.chat_loop
    app.state.streaming_chat_loop = runtime.streaming_chat_loop
    app.state.command_dispatcher = runtime.command_dispatcher
    app.state.log_viewer = LogViewer(runtime.storage.data_dir)
    # One lock for every reference check and reference edit, shared with the
    # Tools and commands that select Sessions for automations.
    app.state.agent_delete_lock = runtime.automation_references.lock
    app.state.server_bind = dict(server_bind)
    app.state.live_calls = _build_live_call_registry(
        app.state, LiveCallRecorder(DataDirectoryLayout(runtime.storage.data_dir).live_calls)
    )


def _build_live_call_registry(state: Any, recorder: LiveCallRecorder) -> LiveCallRegistry:
    """Live calls run their Tools through the canonical RPC handlers."""

    async def dispatch(method: str, params: JsonObject) -> JsonObject:
        return await dispatch_method(state, method, params, METHODS)

    def recording() -> bool:
        # Records hold what the Models sent and read; only Debug Mode keeps them.
        return bool(state.runtime.storage.load_debug_settings()["enabled"])

    return LiveCallRegistry(
        events=state.event_bus, rpc=dispatch, recorder=recorder, recording=recording
    )


async def _shutdown_live_calls(state: Any, logger: logging.Logger) -> None:
    """End Live calls before the runtime their Tools and providers rely on closes."""
    try:
        await asyncio.wait_for(state.live_calls.aclose(), LIVE_CALLS_SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Timed out while ending Live calls")


def _register_run_event_bridge(state: Any) -> Any:
    return _app_chat_runs(state).add_run_started_callback(
        lambda run: bridge_run_to_event_bus(state, run)
    )


def _start_statistics_warmup(state: Any) -> asyncio.Task[None]:
    """Reconcile the Statistics index in the background."""
    return asyncio.create_task(_warm_statistics_index(statistics_service(state)))


def _start_speech_preload(runtime: Any) -> None:
    """Start loading a local STT model whose binding asks to be loaded at startup."""
    runtime.speech.preload_configured()


async def _warm_statistics_index(service: Any) -> None:
    try:
        await service.warm_index_async()
    except Exception:
        logging.getLogger("vbot.server.app").warning(
            "Statistics index warmup failed",
            exc_info=True,
        )


def _unregister_run_event_bridge(state: Any) -> None:
    unsubscribe = state.run_event_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.run_event_bridge_unsubscribe = None


def _register_session_title_bridge(state: Any) -> Any:
    return state.runtime.chat_sessions.add_title_changed_callback(
        lambda address: publish_session_changed(
            state, address.project_id, address.agent_id, address.session_id
        )
    )


def _unregister_session_title_bridge(state: Any) -> None:
    unsubscribe = state.session_title_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.session_title_bridge_unsubscribe = None


def _register_session_completion_read_bridge(state: Any) -> Any:
    # The acknowledged Run id lets other windows clear their unread marker
    # without re-reading activity when they already know that completion.
    return state.runtime.chat_sessions.add_completion_read_callback(
        lambda address, run_id: publish_session_changed(
            state,
            address.project_id,
            address.agent_id,
            address.session_id,
            read_run_id=run_id,
        )
    )


def _unregister_session_completion_read_bridge(state: Any) -> None:
    unsubscribe = state.session_completion_read_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.session_completion_read_bridge_unsubscribe = None


def _register_cron_change_bridge(state: Any) -> Any:
    return state.runtime.cron_service.add_changed_callback(
        lambda: publish_resource_changed(state, RESOURCE_KIND_CRON)
    )


def _unregister_cron_change_bridge(state: Any) -> None:
    unsubscribe = state.cron_change_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.cron_change_bridge_unsubscribe = None


def _register_calendar_change_bridge(state: Any) -> Any:
    return state.runtime.calendar_service.add_changed_callback(
        lambda: publish_resource_changed(state, RESOURCE_KIND_CALENDAR)
    )


def _unregister_calendar_change_bridge(state: Any) -> None:
    unsubscribe = state.calendar_change_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.calendar_change_bridge_unsubscribe = None


def _register_archive_change_bridge(state: Any) -> Any:
    """Publish every archive entry change, including ones no RPC request made."""
    return state.runtime.archive.add_changed_callback(
        lambda: publish_resource_changed(state, RESOURCE_KIND_ARCHIVE)
    )


def _unregister_archive_change_bridge(state: Any) -> None:
    unsubscribe = state.archive_change_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.archive_change_bridge_unsubscribe = None


def _register_skill_change_bridge(state: Any) -> Any:
    """Publish Skill changes an Agent makes through its Skill authoring Tool."""
    return state.runtime.add_skill_changed_callback(
        lambda: publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    )


def _unregister_skill_change_bridge(state: Any) -> None:
    unsubscribe = state.skill_change_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.skill_change_bridge_unsubscribe = None


def _register_terminal_change_bridge(state: Any) -> Any:
    return state.runtime.terminal_manager.add_changed_callback(
        lambda terminal_id: publish_resource_changed(
            state,
            RESOURCE_KIND_TERMINALS,
            scope={"terminal_id": terminal_id},
        )
    )


def _unregister_terminal_change_bridge(state: Any) -> None:
    unsubscribe = state.terminal_change_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.terminal_change_bridge_unsubscribe = None


def _register_bash_process_change_bridge(state: Any) -> Any:
    return state.runtime.process_manager.add_terminal_callback(
        lambda notification: publish_bash_process_status_changed(state, notification)
    )


def _unregister_bash_process_change_bridge(state: Any) -> None:
    unsubscribe = state.bash_process_change_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.bash_process_change_bridge_unsubscribe = None


def _register_recall_index_status_bridge(state: Any) -> Any:
    def forward(status: Any) -> None:
        state.activity.set_recall_status(status.to_dict())
        publish_recall_index_status(state, status)

    return state.runtime.recall.add_index_status_listener(forward)


def _unregister_recall_index_status_bridge(state: Any) -> None:
    unsubscribe = state.recall_index_status_bridge_unsubscribe
    if unsubscribe is not None:
        unsubscribe()
    state.recall_index_status_bridge_unsubscribe = None


def _app_chat_runs(state: Any) -> ChatRunManager:
    return cast(ChatRunManager, state.chat_runs)


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


async def _shutdown_runtime(runtime: Any) -> bool:
    """Shut the Runtime down; return whether every shutdown step succeeded.

    The Runtime logs each failed step with its name and traceback, so the failure
    is not raised again: uvicorn would log the same traceback a second time.
    """
    aclose = getattr(runtime, "aclose", None)
    try:
        if callable(aclose):
            await aclose()
        else:
            runtime.stop()
    except Exception:
        return False
    return True


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
