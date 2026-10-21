"""FastAPI HTTP routes and application factory."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast

from core.access.config import AccessConfig, access_config, inferred_access_mode
from core.extensions.extensions import ExtensionUnavailableError
from core.extensions.oauth_redirects import CALLBACK_PATH as OAUTH_CALLBACK_PATH
from core.runs import RunNotFoundError
from core.tools.terminal_manager import TerminalNotFoundError
from core.utils.config import Config
from core.utils.server_control import (
    CONTROL_INITIATOR_HEADER,
    CONTROL_SHUTDOWN_PATH,
    CONTROL_TOKEN_HEADER,
    is_authorized_control_token,
    normalize_stop_initiator,
)
from server._app_lifecycle import (
    _app_chat_runs,
    _fire_extension_startup,
    _initialize_app_state,
    _shutdown_device_flow_engine,
    _shutdown_live_calls,
    _shutdown_local_catalog_refresh,
    _shutdown_log_viewer,
    _shutdown_model_list_refreshes,
    _shutdown_runtime,
    _shutdown_statistics_warmup,
    _start_speech_preload,
    _start_statistics_warmup,
    _unregister_archive_change_bridge,
    _unregister_calendar_change_bridge,
    _unregister_command_status_bridge,
    _unregister_cron_change_bridge,
    _unregister_model_catalog_change_bridge,
    _unregister_recall_index_status_bridge,
    _unregister_run_event_bridge,
    _unregister_session_completion_read_bridge,
    _unregister_session_title_bridge,
    _unregister_skill_change_bridge,
    _unregister_terminal_change_bridge,
)
from server._bind import ServerBindState, _resolve_server_bind, _runtime_config
from server._cron_webhooks import register_webhook_routes, webhook_base_url
from server._http_dependencies import (
    _FASTAPI_IMPORT_ERROR,
    FastAPI,
    FastAPIType,
    FileResponse,
    HTTPException,
    RedirectResponse,
    Request,
    Response,
    StaticFiles,
    StreamingResponse,
    WebSocket,
    WebSocketDisconnect,
)
from server._oauth_callback import callback_url as oauth_callback_url
from server._oauth_callback import oauth_callback_response
from server._origins import _BrowserOriginGuardMiddleware, _configured_browser_origins
from server._request_bodies import _read_json_payload_with_limit, _require_json_media_type
from server._speech_routes import register_speech_routes
from server._streams import (
    REPLAY_STATUS_RESUMED,
    _active_runs_snapshot,
    _bus_epoch,
    _bus_last_sequence,
    _close_log_stream,
    _connection_replay_status,
    _parse_after_sequence,
    _parse_query_string,
    _queues_snapshot,
    _register_ws_client,
    _replay_after_sequence,
    _RunStreamingResponse,
    _server_identity,
    _sse_run_events,
    _stream_websocket_events,
    _unregister_ws_client,
)
from server._terminal_socket import serve_terminal_socket
from server._upload_routes import register_upload_routes
from server.access import (
    AccessGuard,
    AccessService,
    bound_principal,
    register_access_routes,
    scope_principal,
)
from server.access._rules import host_policy
from server.events import (
    RESOURCE_KIND_AGENT_SECRETS,
    RESOURCE_KIND_DEVICES,
    SERVER_STOPPING_EVENT,
)
from server.file_delivery import FILE_PREVIEW_WORKERS, PREVIEW_URL_PREFIX, FileDelivery
from server.live.owner import LIVE_SOCKET_CLOSE_UNKNOWN_CALL
from server.rpc.errors import RPC_ERROR_INTERNAL, RPC_ERROR_INVALID_REQUEST
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.methods import dispatch_rpc
from server.rpc.model_methods import start_installation_catalog_restore

JsonObject = dict[str, Any]

WEBUI_DIST_DIR = Path(__file__).resolve().parents[1] / "webui" / "dist"

WEBUI_DOCUMENT_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class _VerificationOnlyGuardMiddleware:
    """Expose only startup health, static UI reads, and private shutdown."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: JsonObject, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope_type == "http":
            path = str(scope.get("path") or "")
            method = str(scope.get("method") or "")
            allowed = (
                path == "/health"
                or path == CONTROL_SHUTDOWN_PATH
                or (method in {"GET", "HEAD"} and (path == "/" or path.startswith("/assets/")))
            )
            if not allowed:
                body = b'{"detail":"Server is verifying an application update"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def create_app(
    *,
    runtime: Any | None = None,
    config: Config | None = None,
    server_bind: ServerBindState | None = None,
    shutdown_token: str | None = None,
    request_shutdown: Callable[[str], None] | None = None,
    shutdown_event: asyncio.Event | None = None,
    request_restart: Callable[[], None] | None = None,
    safe_startup_mode: Literal["verification", "test"] | None = None,
    on_ready: Callable[[Any], None] | None = None,
    on_stopped: Callable[[bool], None] | None = None,
    access: AccessConfig | None = None,
    local_access_token: str | None = None,
) -> FastAPIType:
    """Create the FastAPI app and wire runtime services into app state.

    ``access`` is the server's access configuration; without one the access
    mode follows the bind host (loopback: ``local``, otherwise ``lan``).
    ``local_access_token`` is this start's local access token, which the
    access guard accepts only from direct loopback peers.

    ``request_shutdown`` receives who asked for the shutdown (a
    ``STOP_INITIATORS`` value or ``unknown``). ``on_ready`` receives the
    started Runtime once at the end of lifespan startup, before the server
    accepts connections; ``on_stopped`` runs once at the end of lifespan
    shutdown, after the Runtime stopped, and receives whether it stopped cleanly.
    A failed Runtime shutdown is reported only that way, not raised to uvicorn:
    the Runtime already logged each failed step.
    The process host sets ``shutdown_event`` when it starts stopping, ending SSE
    before it waits for HTTP responses and enters lifespan teardown.
    """
    if FastAPI is None:
        raise RuntimeError(
            "FastAPI is required to create the server app"
        ) from _FASTAPI_IMPORT_ERROR
    app_runtime = (
        runtime
        if runtime is not None
        else _build_default_runtime(config, safe_startup_mode=safe_startup_mode)
    )
    effective_safe_mode = getattr(app_runtime, "safe_startup_mode", safe_startup_mode)
    resolved_server_bind = _resolve_server_bind(
        config=config or _runtime_config(app_runtime),
        server_bind=server_bind,
    )
    access_settings = access or access_config(
        inferred_access_mode(resolved_server_bind["listen_host"])
    )
    access_service = AccessService(
        access_settings,
        local_token=local_access_token,
        bind_host=resolved_server_bind["listen_host"],
    )
    file_delivery = FileDelivery()

    @asynccontextmanager
    async def lifespan(app: FastAPIType) -> AsyncIterator[None]:
        app_runtime.start()
        from server._log_sources import build_log_viewer

        _initialize_app_state(
            app,
            app_runtime,
            server_bind=resolved_server_bind,
            log_viewer=await build_log_viewer(app_runtime.storage.data_dir),
            file_delivery=file_delivery,
        )
        app.state.shutdown_event = shutdown_event if shutdown_event is not None else asyncio.Event()
        app.state.access = access_service
        await access_service.start(
            app_runtime.storage.data_dir,
            on_change=lambda: publish_resource_changed(app.state, RESOURCE_KIND_DEVICES),
        )
        _publish_secret_changes(app, app_runtime)
        oauth_redirects = getattr(app_runtime, "oauth_redirects", None)
        if oauth_redirects is not None:
            oauth_redirects.bind(_oauth_callback_url(access_settings, resolved_server_bind))
        _bind_webhook_url(app_runtime, access_settings, resolved_server_bind)
        app.state.control_token = shutdown_token
        app.state.server_identity = _server_identity(app_runtime)
        app.state.request_restart = request_restart
        app.state.activity.start()
        app.state.statistics_warmup_task = (
            None if effective_safe_mode is not None else _start_statistics_warmup(app.state)
        )
        if effective_safe_mode is None:
            await _fire_extension_startup(app_runtime)
            _start_speech_preload(app_runtime)
            # A missing or incompatible runtime Model DB is detected before the
            # sweep below can publish a new one.
            start_installation_catalog_restore(app.state)
        # Local model catalogs (auto_refresh connections, e.g. Ollama) refresh
        # in the background — never blocking startup; the method itself is
        # throttled and swallows failures.
        app.state.local_catalog_refresh_task = (
            None
            if effective_safe_mode is not None
            else asyncio.create_task(
                app_runtime.maybe_refresh_local_catalogs(), name="local-catalog-refresh"
            )
        )
        server_logger = logging.getLogger("vbot.server.app")
        server_logger.debug(
            "Server application ready on %s:%s",
            resolved_server_bind["listen_host"],
            resolved_server_bind["listen_port"],
        )
        app_runtime.activate_bootstrap()
        if on_ready is not None:
            on_ready(app_runtime)
        try:
            yield
        finally:
            app.state.shutdown_event.set()
            runtime_stopped_cleanly = False
            try:
                server_logger.debug("Server application stopping")
                await _shutdown_live_calls(app.state, server_logger)
                await _shutdown_local_catalog_refresh(
                    app.state.local_catalog_refresh_task,
                    server_logger,
                )
                await _shutdown_statistics_warmup(app.state.statistics_warmup_task)
                await app.state.activity.aclose()
                _unregister_run_event_bridge(app.state)
                _unregister_session_title_bridge(app.state)
                _unregister_session_completion_read_bridge(app.state)
                _unregister_cron_change_bridge(app.state)
                _unregister_calendar_change_bridge(app.state)
                _unregister_archive_change_bridge(app.state)
                _unregister_skill_change_bridge(app.state)
                _unregister_model_catalog_change_bridge(app.state)
                _unregister_terminal_change_bridge(app.state)
                _unregister_command_status_bridge(app.state)
                _unregister_recall_index_status_bridge(app.state)
                await _shutdown_log_viewer(app.state.log_viewer, server_logger)
                await _shutdown_device_flow_engine(
                    getattr(app.state, "device_flow_engine", None),
                    server_logger,
                )
                await _shutdown_model_list_refreshes(app_runtime)
                await access_service.close()
                runtime_stopped_cleanly = await _shutdown_runtime(app_runtime)
            finally:
                if on_stopped is not None:
                    on_stopped(runtime_stopped_cleanly)

    # No generated API docs: the access guard serves every GET outside the server
    # namespaces as the public WebUI shell.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if effective_safe_mode == "verification":
        app.add_middleware(_VerificationOnlyGuardMiddleware)
    app.add_middleware(
        _BrowserOriginGuardMiddleware,
        allowed_origins=_configured_browser_origins(resolved_server_bind)
        | _public_browser_origins(access_settings),
        same_origin_port=resolved_server_bind["listen_port"],
        same_origin_hosts=host_policy(
            access_settings, bind_host=resolved_server_bind["listen_host"]
        ).names,
        allow_same_origin_ip=resolved_server_bind["listen_host"] in {"0.0.0.0", "::"},
    )
    # Added last, so it runs first: before the Origin guard and routing.
    app.add_middleware(AccessGuard, service=access_service, file_delivery=file_delivery)
    register_access_routes(app, access_service)
    register_webhook_routes(app, access_service)

    @app.get("/health")
    async def health() -> JsonObject:
        return {"status": "ok"}

    @app.get(OAUTH_CALLBACK_PATH, include_in_schema=False)
    async def oauth_callback(request: Request) -> Response:
        redirects = getattr(request.app.state.runtime, "oauth_redirects", None)
        return oauth_callback_response(redirects, request.query_params.multi_items())

    @app.post(CONTROL_SHUTDOWN_PATH, status_code=202, include_in_schema=False)
    async def shutdown(request: Request) -> JsonObject:
        provided_token = request.headers.get(CONTROL_TOKEN_HEADER)
        if not is_authorized_control_token(provided_token, shutdown_token):
            raise HTTPException(status_code=404)
        if request_shutdown is None:
            raise HTTPException(status_code=503, detail="Server shutdown is unavailable")
        initiator = normalize_stop_initiator(request.headers.get(CONTROL_INITIATOR_HEADER))
        # Tell open app clients who stops the server before uvicorn closes their sockets.
        request.app.state.event_bus.publish(SERVER_STOPPING_EVENT, {"initiator": initiator})
        request_shutdown(initiator)
        return {"status": "stopping"}

    @app.post("/api/rpc")
    async def rpc(request: Request, response: Response) -> JsonObject:
        _require_json_media_type(request)
        try:
            payload = await _read_json_payload_with_limit(request)
        except ValueError:
            return {
                "ok": False,
                "error": {
                    "code": RPC_ERROR_INVALID_REQUEST,
                    "message": "RPC request body must be valid JSON",
                },
            }
        try:
            # Only handlers of this request act for its principal, never tasks it starts.
            with bound_principal(scope_principal(request.scope)):
                result = await dispatch_rpc(request.app.state, payload)
                principal = scope_principal(request.scope)
                return cast(
                    JsonObject,
                    file_delivery.bind_payload(result, principal.actor if principal else None),
                )
        except Exception:
            # The dispatcher logged the original failure; never expose its details.
            response.status_code = 500
            return {
                "ok": False,
                "error": {"code": RPC_ERROR_INTERNAL, "message": "Internal server error"},
            }

    register_upload_routes(app)
    register_speech_routes(app)

    @app.get("/api/files/{token}")
    async def get_assistant_file(request: Request, token: str, download: bool = False) -> Response:
        delivery = request.app.state.file_delivery
        delivered = await FILE_PREVIEW_WORKERS.run(delivery.resolve_token, token)
        if delivered is None:
            raise HTTPException(status_code=404)
        if delivered.media_type == "text/html" and not download:
            try:
                preview = await FILE_PREVIEW_WORKERS.run(
                    delivery.open_preview, f"/api/files/{token}"
                )
                principal = scope_principal(request.scope)
                preview = delivery.bind_payload(
                    preview,
                    principal.actor if principal else None,
                    expires_at=request.scope.get("vbot.capability_expires_at"),
                )
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=404) from exc
            return RedirectResponse(
                preview["url"],
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        return FileResponse(
            delivered.path,
            media_type=delivered.media_type,
            filename=delivered.path.name,
            content_disposition_type=(
                "inline" if delivered.inline and not download else "attachment"
            ),
            headers=delivered.response_headers,
        )

    @app.get("/api/preview-assets/{token}/.revision")
    async def get_preview_revision(request: Request, token: str) -> Response:
        delivery = request.app.state.file_delivery
        try:
            revision = await FILE_PREVIEW_WORKERS.run(delivery.preview_revision, token)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404) from exc
        return Response(
            json.dumps({"revision": revision}),
            media_type="application/json",
            headers=delivery.preview_headers(str(request.base_url), token),
        )

    @app.api_route("/api/preview-assets/{token}/{asset_path:path}", methods=["GET", "HEAD"])
    async def get_preview_asset(request: Request, token: str, asset_path: str) -> Response:
        delivery = request.app.state.file_delivery
        try:
            delivered = await FILE_PREVIEW_WORKERS.run(delivery.preview_file, token, asset_path)
            headers = delivery.preview_headers(str(request.base_url), token)
            if (
                delivered.path.name == "index.html"
                and asset_path
                and not asset_path.endswith("/")
                and asset_path.rsplit("/", 1)[-1] != "index.html"
            ):
                return RedirectResponse(
                    str(request.url.replace(path=request.url.path + "/")), headers=headers
                )
            if delivered.media_type == "text/html":
                headers["Content-Disposition"] = "inline"
                content = await FILE_PREVIEW_WORKERS.run(
                    delivery.preview_html, delivered.path, f"{PREVIEW_URL_PREFIX}{token}/.revision"
                )
                return Response(content, media_type="text/html", headers=headers)
            return FileResponse(delivered.path, media_type=delivered.media_type, headers=headers)
        except ValueError, OSError:
            content, headers = await FILE_PREVIEW_WORKERS.run(
                delivery.preview_unavailable, str(request.base_url), token
            )
            return Response(
                content,
                status_code=404,
                media_type="text/html",
                headers=headers,
            )

    @app.api_route("/api/extension-assets/{token}/{asset_path:path}", methods=["GET", "HEAD"])
    async def get_extension_asset(request: Request, token: str, asset_path: str) -> Response:
        """Serve a page asset only while its exact Extension registration survives."""
        delivery = request.app.state.file_delivery
        try:
            claims = await FILE_PREVIEW_WORKERS.run(delivery.extension_page_claims, token)
            from core.extensions import ExtensionRegistrationIdentity

            identity = ExtensionRegistrationIdentity(claims["extension"], claims["epoch"])
            registry = request.app.state.runtime.extensions
            if registry is None or not registry.is_registration_current(identity):
                raise ValueError("Extension page is unavailable")
            declarations = await FILE_PREVIEW_WORKERS.run(registry.page_declarations)
            current = next(
                (
                    (declaration, entry)
                    for candidate, declaration, entry in declarations
                    if candidate == identity and declaration.page_id == claims["page"]
                ),
                None,
            )
            if current is None or current[1].parent != Path(claims["root"]):
                raise ValueError("Extension page is unavailable")
            _claims, delivered = await FILE_PREVIEW_WORKERS.run(
                delivery.extension_page_asset, token, asset_path
            )
            if (
                request.app.state.runtime.extensions is not registry
                or not registry.is_registration_current(identity)
            ):
                raise ValueError("Extension page is unavailable")
            return FileResponse(
                delivered.path,
                media_type=delivered.media_type,
                headers=delivery.extension_page_headers(str(request.base_url), token),
            )
        except OSError, ValueError, KeyError:
            return Response(status_code=404)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        chat_runs = _app_chat_runs(request.app.state)
        try:
            run = chat_runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        after_sequence = _replay_after_sequence(request)
        return _RunStreamingResponse(
            _sse_run_events(
                run,
                after_sequence=after_sequence,
                shutdown_event=request.app.state.shutdown_event,
                file_delivery=request.app.state.file_delivery,
                file_actor=_file_actor(request.scope),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # Prevent reverse proxies (e.g. nginx) from buffering the
                # incremental Run timeline into one late flush.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/timeline")
    async def run_timeline(
        request: Request, run_id: str, child_limit: int = 0
    ) -> StreamingResponse:
        from server.timeline import TimelineStreamingResponse, sse_timeline

        if not 0 <= child_limit <= 200:
            raise HTTPException(status_code=400, detail="child_limit must be between 0 and 200")

        try:
            run = _app_chat_runs(request.app.state).get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TimelineStreamingResponse(
            sse_timeline(
                request.app.state,
                run,
                shutdown_event=request.app.state.shutdown_event,
                child_limit=child_limit,
                file_actor=_file_actor(request.scope),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/extension-runs/{token}/events")
    @app.get("/api/extension-runs/{token}/timeline")
    async def extension_run_events(
        request: Request, token: str, child_limit: int = 0
    ) -> StreamingResponse:
        """Stream only a Run still owned by the page identity in its capability."""
        if not 0 <= child_limit <= 200:
            raise HTTPException(status_code=400, detail="child_limit must be between 0 and 200")
        delivery = request.app.state.file_delivery
        try:
            claims = delivery.extension_run_claims(token)
            registry = request.app.state.runtime.extensions
            if registry is None:
                raise ValueError("Extension Run is unavailable")
            identity, page = _current_extension_page(registry, claims)
            if identity is None or page is None:
                raise ValueError("Extension Run is unavailable")
            host = registry.host_for(identity)
            temporary_agents = host.temporary_agents
            if temporary_agents is None:
                raise ValueError("Extension Run is unavailable")
            inspection = await temporary_agents.owned_run(claims["group_id"], claims["run_id"])
            # The checked host may be retired while awaiting durable ownership.
            # Re-resolve page and owner before exposing the SSE iterator; the
            # durable inspection stays valid while that registration still owns it.
            if request.app.state.runtime.extensions is not registry:
                raise ValueError("Extension Run is unavailable")
            verified_identity, page = _current_extension_page(registry, claims)
            if (
                verified_identity != identity
                or page is None
                or registry.host_for(identity).temporary_agents is None
            ):
                raise ValueError("Extension Run is unavailable")
            if inspection.run is None:
                raise ValueError("Extension Run is not live")
        except KeyError, ValueError, ExtensionUnavailableError:
            raise HTTPException(status_code=404, detail="Extension Run is unavailable") from None
        if request.url.path.endswith("/timeline"):
            from server.timeline import TimelineStreamingResponse, sse_timeline

            async def projected_events() -> AsyncGenerator[str]:
                async with aclosing(
                    sse_timeline(
                        request.app.state,
                        inspection.run,
                        shutdown_event=request.app.state.shutdown_event,
                        include_file_urls=True,
                        child_limit=child_limit,
                        file_actor=_file_actor(request.scope),
                        file_expires_at=request.scope.get("vbot.capability_expires_at"),
                    )
                ) as stream:
                    async for chunk in stream:
                        # Projection awaits History and file capabilities. A reload
                        # or disable in that time must retire the stream too.
                        try:
                            current = (
                                request.app.state.runtime.extensions is registry
                                and registry.current_page(identity, claims["page"]) is not None
                                and registry.host_for(identity).temporary_agents is not None
                            )
                        except KeyError, ValueError, ExtensionUnavailableError:
                            break
                        if not current:
                            break
                        yield chunk

            return TimelineStreamingResponse(
                projected_events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return _RunStreamingResponse(
            _sse_run_events(
                inspection.run,
                after_sequence=claims["after_sequence"],
                shutdown_event=request.app.state.shutdown_event,
                file_delivery=delivery,
                include_file_urls=True,
                file_actor=_file_actor(request.scope),
                file_expires_at=request.scope.get("vbot.capability_expires_at"),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        event_bus = websocket.app.state.event_bus
        client_epoch = _parse_query_string(websocket.query_params.get("epoch"))
        client_after_sequence = _parse_after_sequence(websocket.query_params.get("after_sequence"))
        # Register this window in the presence roster *before* reading the hello
        # high-water mark and *before* the subscribe loop. Publishing the connect
        # signal first means its sequence sits at or below the live-only floor,
        # so this window does not replay its own presence event while other
        # windows still see it; the floor is read before the hello send, so
        # events arriving during that send are not skipped (no replay gap).
        client_entry = _register_ws_client(websocket)
        try:
            # Read last_sequence *before* sending the hello frame so any events
            # published during the await are still in the retained deque and get
            # replayed by the subsequent subscribe.
            last_sequence_at_hello = _bus_last_sequence(event_bus)
            replay_status = _connection_replay_status(
                event_bus,
                client_epoch=client_epoch,
                client_after_sequence=client_after_sequence,
                last_sequence=last_sequence_at_hello,
            )
            active_runs = _active_runs_snapshot(websocket.app.state)
            hello_frame: JsonObject = {
                "type": "connection_ready",
                "epoch": _bus_epoch(event_bus),
                "last_sequence": last_sequence_at_hello,
                "replay_status": replay_status,
                "active_runs": active_runs,
                "queues": _queues_snapshot(websocket.app.state),
                "server": websocket.app.state.server_identity,
            }
            await websocket.send_json(
                file_delivery.bind_payload(hello_frame, _file_actor(websocket.scope))
            )
            if replay_status == REPLAY_STATUS_RESUMED:
                subscribe_after_sequence = client_after_sequence
            else:
                subscribe_after_sequence = last_sequence_at_hello
            async with aclosing(
                event_bus.subscribe(after_sequence=subscribe_after_sequence)
            ) as stream:
                await _stream_websocket_events(
                    websocket,
                    stream,
                    file_delivery=file_delivery,
                    file_actor=_file_actor(websocket.scope),
                )
        except WebSocketDisconnect:
            return
        finally:
            _unregister_ws_client(websocket.app.state, client_entry)

    @app.websocket("/ws/logs")
    async def websocket_logs(websocket: WebSocket) -> None:
        await websocket.accept()
        file_name = websocket.query_params.get("file")
        cursor = websocket.query_params.get("cursor")
        stream = websocket.app.state.log_viewer.subscribe(file_name or "", cursor=cursor)
        try:
            if await _stream_websocket_events(websocket, stream):
                # The file's watcher stopped; the accessor reads again and reconnects.
                await websocket.close(code=1011, reason="log stream ended")
        except ValueError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except FileNotFoundError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except OSError as exc:
            # A share lock or a virus scanner can deny a read for a moment; the
            # accessor's reconnect reads again and resubscribes.
            logging.getLogger("vbot.server.app").warning(
                "Live-log stream closed after a failed file read (file=%s error=%s: %s)",
                file_name,
                type(exc).__name__,
                exc,
            )
            await websocket.close(code=1011, reason="log file could not be read")
        except WebSocketDisconnect:
            return
        finally:
            await _close_log_stream(stream)

    @app.websocket("/ws/terminals/{terminal_id}")
    async def websocket_terminal(websocket: WebSocket, terminal_id: str) -> None:
        await websocket.accept()
        manager = websocket.app.state.runtime.terminal_manager
        try:
            await serve_terminal_socket(websocket, manager, terminal_id)
        except TerminalNotFoundError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/live/{call_id}")
    async def websocket_live(websocket: WebSocket, call_id: str) -> None:
        await websocket.accept()
        owner = websocket.app.state.live_calls.attach(call_id)
        if owner is None:
            await websocket.close(code=LIVE_SOCKET_CLOSE_UNKNOWN_CALL, reason="Unknown Live call")
            return
        try:
            async with aclosing(owner.frames()) as frames:
                await _stream_websocket_events(
                    websocket,
                    frames,
                    on_binary=owner.receive_audio,
                    on_text=owner.receive_text,
                    file_delivery=file_delivery,
                    file_actor=_file_actor(websocket.scope),
                )
            if owner.finished and owner.close_code is not None:
                await websocket.close(code=owner.close_code)
        except WebSocketDisconnect:
            return
        finally:
            owner.detach()

    _mount_webui(app)

    return app


def _file_actor(scope: Mapping[str, Any]) -> str | None:
    principal = scope_principal(scope)
    return principal.actor if principal is not None else None


def _build_default_runtime(
    config: Config | None,
    *,
    safe_startup_mode: Literal["verification", "test"] | None = None,
) -> Any:
    from core.runtime import Runtime

    return Runtime(config or Config(), safe_startup_mode=safe_startup_mode)


def _mount_webui(app: FastAPIType) -> None:
    webui_dist_dir = WEBUI_DIST_DIR
    webui_index_file = webui_dist_dir / "index.html"
    if not webui_dist_dir.is_dir() or not webui_index_file.is_file():
        return

    webui_assets_dir = webui_dist_dir / "assets"
    if webui_assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=webui_assets_dir), name="webui-assets")

    @app.get("/", include_in_schema=False)
    async def webui_index() -> FileResponse:
        return FileResponse(webui_index_file, headers=WEBUI_DOCUMENT_CACHE_HEADERS)

    @app.get("/{path:path}", include_in_schema=False)
    async def webui_fallback(path: str) -> FileResponse:
        if _is_reserved_server_path(path):
            raise HTTPException(status_code=404, detail="Not Found")
        requested_file = _safe_webui_file_path(webui_dist_dir, path)
        if requested_file is not None:
            return FileResponse(requested_file)
        return FileResponse(webui_index_file, headers=WEBUI_DOCUMENT_CACHE_HEADERS)


def _is_reserved_server_path(path: str) -> bool:
    return path == "health" or any(
        path == namespace or path.startswith(f"{namespace}/")
        for namespace in ("ws", "api", "auth", "_vbot")
    )


def _public_browser_origins(access: AccessConfig) -> frozenset[tuple[str, str, int]]:
    """The configured public origin of ``authenticated`` mode, if any."""
    origin = access.public_origin()
    return frozenset() if origin is None else frozenset({origin})


def _bind_webhook_url(runtime: Any, access: AccessConfig, server_bind: ServerBindState) -> None:
    """Tell Cron the address its webhook URLs start with; a Runtime without Cron has none."""
    try:
        cron_service = runtime.cron_service
    except AttributeError, RuntimeError:
        return
    cron_service.bind_webhook_url(*webhook_base_url(access, server_bind))


def _publish_secret_changes(app: Any, app_runtime: Any) -> None:
    """Clients reload Agent Secrets when a Secret or a pending Secret request changes."""
    try:
        secrets = app_runtime.agent_secrets
        requests = app_runtime.secret_requests
    except AttributeError, RuntimeError:  # a Runtime without Agent Secrets
        return
    secrets.add_change_listener(
        lambda _change: publish_resource_changed(app.state, RESOURCE_KIND_AGENT_SECRETS)
    )
    requests.set_change_listener(
        lambda _request_id: publish_resource_changed(app.state, RESOURCE_KIND_AGENT_SECRETS)
    )


def _oauth_callback_url(access: AccessConfig, server_bind: ServerBindState) -> str | None:
    """Behind a public URL the browser comes back there; otherwise to loopback."""
    if access.public_url is not None:
        return f"{access.public_url}{OAUTH_CALLBACK_PATH}"
    return oauth_callback_url(server_bind)


def _current_extension_page(registry: Any, claims: JsonObject) -> tuple[Any | None, Any | None]:
    """Resolve the exact current page identity carried by a Run capability.

    Reads only registry memory, so it runs on the Event Loop.
    """
    from core.extensions import ExtensionRegistrationIdentity

    extension = claims.get("extension")
    page = claims.get("page")
    epoch = claims.get("epoch")
    if not all(isinstance(value, str) and value for value in (extension, page, epoch)):
        return None, None
    identity = ExtensionRegistrationIdentity(cast(str, extension), cast(str, epoch))
    current = registry.current_page(identity, cast(str, page))
    if current is None:
        return None, None
    return identity, current[0]


def _safe_webui_file_path(webui_dist_dir: Path, requested_path: str) -> Path | None:
    file_path = webui_dist_dir / requested_path
    try:
        resolved_file_path = file_path.resolve()
        resolved_dist_dir = webui_dist_dir.resolve()
        resolved_file_path.relative_to(resolved_dist_dir)
    except ValueError:
        return None

    if resolved_file_path.is_file():
        return resolved_file_path
    return None
