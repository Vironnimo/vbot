# Server

FastAPI transport and public protocol edge around the core vBot kernel.

## Overview

`server/` owns HTTP routing, the RPC envelope, Server-Sent Events, WebSocket transport, request/response validation and mapping, process startup, and optional static WebUI serving. It imports runtime/core services but does not own Agent, Chat, Run, Session, Provider, Project, Tool, Skill, Extension, Prompt, Channel, or storage business rules.

RPC method bodies are grouped by owning product/domain surface under `server/rpc/*_methods.py`; shared envelope dispatch, validation, payload sanitation, runtime/provider access, expected-error mapping, and event bridging live in focused support modules. The registered method tables and handlers are the source of truth for individual names, parameters, and return shapes. Domain maps explain the business semantics those handlers expose.

## Terms

### Event Bus

**Definition:** The process-local replayable server-push mechanism behind the shared `/ws` socket. It distributes app-wide lifecycle summaries and invalidations while per-Run streaming stays on SSE.

**Not:** A durable event store, a Provider protocol, or a client command channel.

## Transport contracts

- `POST /api/rpc` accepts `{method, params?}` and always returns `{ok:true,result}` or `{ok:false,error:{code,message}}`, including malformed JSON/params. `server/rpc/dispatcher.py` owns envelope parsing; `server/rpc/methods.py` builds the static `METHODS` table from domain-indexed registries.
- `GET /api/runs/{run_id}/events` is the primary per-Run SSE stream and carries complete Run events plus transient deltas. The shared `/ws` socket carries app-wide lifecycle summaries, active-Run reconnect state, presence, and `resource_changed`; it is server-push only. Both Run events and `connection_ready.active_runs` preserve an explicit `contributes_to_agent_activity: false` from the Runs domain while omitting the default true value; transport does not decide which work needs attention.
- `/ws/logs` is a dedicated selected-file log stream, not part of the shared event bus. Its read/cursor handoff belongs in `logs.md`.
- `/ws/terminals/{terminal_id}` is the dedicated server-push stream for one live or temporarily retained Terminal Session. It starts with an authoritative ANSI snapshot containing bounded scrollback plus the current screen and a monotonic sequence; a live session then carries ordered PTY output/state events, while a finished session ends after the snapshot. Manual start, catalog, client input, resize, kill, and retained-record removal use `terminal.*` RPC methods rather than the socket.
- Attachments, Speech, and generated Image artifacts use dedicated HTTP endpoints because binary data does not belong in the JSON RPC envelope. Their size/MIME/artifact semantics live in `attachments.md` and the relevant `model_tasks/*` maps. Separately, `GET /api/files/{token}` serves an existing original server-local file named by an Assistant: the token is a stateless per-process HMAC capability over the canonical path, never a caller-supplied raw path or a copied blob.
- `/health` is the transport health probe. Built `webui/dist` assets are optional and served as an SPA only when `index.html` exists.
- `POST /_vbot/control/shutdown` is the private cooperative lifecycle edge, not a management API. It exists outside RPC so the local CLI can make Uvicorn enter the normal FastAPI lifespan teardown; it returns not-found without the current per-process secret from `<data_dir>/runtime/server-<port>.json`. The shared transport-neutral `core/utils/server_control.py` owns secret comparison, restrictive atomic persistence, PID/port binding, and exact-owner cleanup so the local CLI does not import the optional Server package.

## Interfaces and source ownership

RPC handlers that load/project Chat history, preview prompts, resolve file mentions/catalog trees, mutate Session state, or build Statistics reports use named bounded pools instead of the Event Loop or the shared default executor. Loop-owned Run/Event Bus state is read or mutated only after the worker result returns; long Session/filesystem projections therefore cannot stall SSE/WebSocket delivery.

- `server.app.create_app(runtime=None, config=None, server_bind=None, shutdown_token=None, request_shutdown=None)` creates the FastAPI app, owns lifespan startup/shutdown, mounts transports/static assets, initializes app state, and wires runtime-owned `chat_run_manager`, Chat loops, Command dispatcher, event bus, Client registry, Log viewer, and coordination locks. The optional shutdown inputs are supplied only by `server.main` for the authenticated private control route. It also starts/cancels the optional worker-thread Statistics index warmup when the Runtime exposes the complete Session/Agent/Project discovery surface, and cancels and awaits Runtime-scoped timed-out `model.list` refresh tasks before closing that Runtime. Its Session bridges project automatic title changes and successful completion-read callbacks—including Sub-Agent results acknowledged internally after Parent delivery—onto `resource_changed(kind="sessions")`, so every open Accessor refreshes the durable Session listing. Runtime lifecycle/DI remains owned by `runtime.md`.
- `server.rpc.methods.dispatch_rpc(state, request)` is the public RPC dispatch seam over the import-time `METHODS` table. Add a handler to the domain-appropriate `*_methods.py::method_handlers()` registry; do not create a parallel dispatcher.
- `server/rpc/connection_methods.py` owns the `provider.custom_list`, `provider.custom_save`, and `provider.custom_delete` transport orchestration around Settings-owned Custom Providers. Save accepts an optional write-only `api_key`, persists only the secret-free Provider record, reloads Provider/Model registries in place, and publishes both invalidations; delete removes generated data-dir credential keys but deliberately leaves Agent/default/task Model references untouched and unavailable. When `model.list` returns its bounded stale snapshot after discovery times out, its refresh task is tracked by Runtime identity for lifespan shutdown rather than becoming detached process-global work.
- `server/rpc/chat_methods.py` validates Chat RPC inputs and Agent addressing, calls Chat's `prepare`/`execute` contract, and generically maps neutral command feedback/navigation/facts/Runs into the existing `command_handled` or Run response. It must not match a command name, build a command prompt, or mutate command-owned state. Generic command changes flow through `publish_resource_changed`; command-started Runs use the process-wide `ChatRunManager` start callback rather than command-specific bridging.
- `server/rpc/catalog_methods.py::chat.commands` projects the Runtime's live combined Command catalog plus the addressed Agent's available Skills. It does not read the static Built-in table or cache Extension registrations.
- `server/rpc/agent_methods.py::agent.rename` is the explicit Identity Agent id mutation. `server/rpc/agent_refs.py` owns the cross-domain transaction: under the shared reference lock and old/new Run guards it moves the core tree, retargets Agent policies, Session links, Channels, and non-terminal unqualified Cron jobs, and compensates completed steps on failure. Success publishes `resource_changed(kind="agents", scope={old_agent_id,new_agent_id})` before the matching Sessions/Channel/Cron invalidations so connected Accessors can remap local selection before reloading.
- `agent.list` projects the Agents-owned canonical Identity Agent order plus `order_revision`; `agent.reorder` accepts the complete `agent_ids` sequence and `expected_revision`, serializes against Identity Agent rename/delete through the shared reference lock, and publishes `resource_changed(kind="agents")` only after an actual successful order change. Exact-roster or revision mismatch is the stable `agent_order_conflict` error; Project Config Agents are outside this contract.
- `server/rpc/validation.py` owns shared request-shape parsing and transport-level guards such as Agent-address parsing. Domain-specific validation belongs with its handler or core domain: Model usability for `/model` and Project Agent overrides is owned by `ModelConfigurationChecker`, while RPC only maps `ModelConfigurationError` to `invalid_request`.
- `server/rpc/payloads.py` owns common public projections and recursive removal of opaque Provider metadata. It also consumes internal Assistant `output_files` uniformly for RPC, History, SSE, and WebSocket payloads: each exact `file:` marker span becomes a Markdown image for safe raster content or a Markdown link for another regular file while surrounding Assistant text stays unchanged, and the internal reference field is always removed. Legacy line-only references from the original path-delivery format remain readable and replace their complete line. `server/rpc/error_mapping.py` converts expected domain errors to stable provider-agnostic RPC errors; unexpected errors are not hidden.
- `server/file_delivery.py` owns Assistant-file capability signing/verification, current-byte MIME sniffing, Markdown projection, and original-path resolution. It keeps no token registry and writes no bytes; `server/app.py` owns the HTTP response and forces non-image files to attachment disposition with `nosniff`.
- `server/rpc/statistics_methods.py` validates Statistics windows, caches the core `StatisticsService` on app state, exposes the startup warmup seam, and offloads synchronous filesystem/SQLite reconciliation to a worker thread; it does not own index schema or aggregation semantics.
- `server.events.ServerEventBus`, `server.rpc.event_bridge`, and `server.clients.ClientRegistry` own shared server-push, Run lifecycle projection, invalidation, and app-window presence. The Run-output bridge includes both Sub-Agent navigation (`subagent_session_started`) and explicit cancellation status (`subagent_status_changed`) events without moving their business semantics out of `core/subagents/`. Exact replay/handshake behavior is task-gated below.
- `server.main.main(argv=None)` activates OS process containment, persists the per-process control record, constructs an explicit `uvicorn.Server`, and removes its own control record after `Server.run()` returns. Canonical bind resolution lives in `core/utils/config.py`; `server/main.py` re-exports it for compatibility while CLI imports the core seam directly.

RPC source routing:

- Agent/Session → `agent_methods.py`; Chat/Queue/Command execution → `chat_methods.py`; Projects → `project_methods.py`; Channels → `channel_methods.py`; Automation → `automation_methods.py`.
- Models/Connections/credentials → `connection_methods.py`; Provider Usage → `provider_usage_methods.py`; Settings/task Models → `settings_methods.py`; Extensions → `extensions_methods.py`.
- Tool/Skill/file/Command catalogs → `catalog_methods.py`; Skill authoring → `skill_methods.py`; Prompts/Logs → `operations_methods.py`; Terminal operator start/mutations/catalog → `terminal_methods.py`; Debug/Statistics/Clients → their matching method modules.

## Invariants and conventions

- Server is an edge, not a second business layer. Handlers validate/map, call the owning domain, coordinate cross-domain mutations only where the public operation requires it, and publish invalidation after success.
- Clients command the server through RPC/HTTP, never `/ws`. SSE is the full per-Run output channel; `/ws` must not bridge SSE-only deltas.
- Public Run/history/SSE/WebSocket payloads recursively strip `reasoning_meta`, `output_files`, and other internal data through one shared projector. When file delivery is available, that projector replaces only the referenced Assistant marker spans with freshly signed `/api/files/` Markdown URLs; ordinary history filters `role: "note"`, while visible errors, checkpoints, run summaries, and takeover dividers follow the Chat/public-history contract.
- Session creation is explicit at the product/server boundary. Only one active Run per Session is a core Runs invariant; server busy-send behavior delegates to the shared Queue rather than maintaining accessor-local state.
- Binary upload/download remains outside RPC. Blob size/MIME validation happens before the owning core service is called, and expected domain failures map to appropriate HTTP errors.
- WebSocket/SSE replay buffers are bounded and process-local. Durable recovery uses Session history; reconnect cannot assume an unlimited event archive.
- Assistant file URLs are process-local capabilities regenerated from canonical History after restart. Every request revalidates and reads the original path, so later edits are served immediately and deletion or invalid/tampered tokens return 404; historical messages deliberately have reference rather than snapshot semantics.
- Exact domain RPC payloads belong in handler source plus the owning Domain Map/Reference. Do not mirror the complete method catalog in this always-read map.
- Routine uvicorn/WebSocket lifecycle noise is suppressed through the managed logging pipeline; actual transport errors remain visible.

## Constraints & Gotchas

- FastAPI, uvicorn, websockets, and multipart parsing are optional server dependencies; server construction must fail clearly when the group is absent.
- Static serving must never shadow reserved paths: `/api/*`, `/ws`, `/health`, and the private `/_vbot/control/*` lifecycle path. A missing `webui/dist/index.html` leaves `/` unmounted instead of breaking startup.
- A stale control record is not authority: CLI validates its PID against the actual confirmed vBot listener, startup atomically replaces it, and shutdown cleanup removes it only when both PID and secret still match. Never expose the token through health, RPC, logs, or public payloads.
- Runtime stubs supplied to `create_app` must provide the runtime-owned services the server reads directly; the server does not silently construct fallback Chat/Run services.
- Provider/account secrets, OAuth tokens/codes, and opaque metadata must never enter logs or public payloads.
- Schema decisions remain at the RPC/transport edge or owning core domain. Adding endpoint-specific business logic to `server/app.py` or a generic dispatcher helper is an ownership error.

## References

Read this only when your task matches — not by default.

- Changing shared `/ws` events, active-Run reconnect, sequence/epoch replay, app-window presence, `resource_changed`, Run bridging, SSE replay, or the log-socket handoff → `server/events-and-reconnect.md`
