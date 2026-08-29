# Server

FastAPI transport and public protocol edge around the core vBot kernel.

## Overview

`server/` owns HTTP routing, the RPC envelope, SSE, WebSocket transport, request/response validation and mapping, process startup, and optional static WebUI serving. It imports runtime/core services but owns no business rules - Agent, Chat, Run, Session, Provider, Project, Tool, Skill, Extension, Prompt, Channel, and storage semantics stay in their domains.

RPC bodies group by owning surface under `server/rpc/*_methods.py`; envelope dispatch, validation, payload sanitation, expected-error mapping, and event bridging live in focused support modules. The registered method tables are the source of truth for names/parameters/return shapes - do not mirror the method catalog here.

## Transport contracts

- `POST /api/rpc` accepts `{method, params?}` JSON, returns `{ok:true,result}` or `{ok:false,error:{code,message}}`; non-JSON media types fail 415 before dispatch.
- `GET /api/runs/{id}/events` is the per-Run SSE stream (complete events plus deltas). The shared `/ws` socket is server-push only: lifecycle summaries, reconnect state, presence, `resource_changed`. Both preserve explicit `contributes_to_agent_activity: false` while omitting the default true - transport never decides attention. Reflection Runs carry resolved `source_session_id` for attribution.
- `/ws/logs` (handoff in `logs.md`) and `/ws/terminals/{id}` (ANSI snapshot then ordered PTY events; control via `terminal.*` RPCs) are dedicated streams outside the shared bus.
- Binary data stays outside RPC: dedicated attachment/speech/image endpoints (semantics in their maps) plus `GET /api/files/{token}` serving Assistant-referenced originals - the token is a stateless per-process HMAC capability over the canonical path, never a caller-supplied path or copied blob.
- **`/health` identity contract:** HTTP 200 with body exactly `{"status":"ok"}` is what CLI/Desktop probes require before treating a listener as vBot. Built WebUI assets serve as SPA when present; the entry document sends no-store so persistent Desktop WebView profiles always refresh, individual static files keep ordinary delivery.
- `POST /_vbot/control/shutdown` is the private cooperative lifecycle edge letting the local CLI trigger normal lifespan teardown; it requires the current per-process secret from `<data_dir>/runtime/server-<port>.json`. The transport-neutral `core/utils/server_control.py` owns secret comparison, atomic persistence, and exact-owner cleanup.

## Source routing

- Handler modules map one-to-one to product surfaces (`agent_methods`, `chat_methods`, `project_methods`, `channel_methods`, `automation_methods`, `connection_methods`, `provider_usage_methods`, `settings_methods`, `extensions_methods`, `catalog_methods`, `skill_methods`, `operations_methods`, `terminal_methods`, debug/statistics/clients); add handlers to the domain-appropriate registry - never a parallel dispatcher over `methods.dispatch_rpc`.
- Non-obvious module contracts: `chat_methods` must not match command names or mutate command-owned state (generic projection only), re-aims an Identity Agent's `current_session_id` on plain sends/edits so restarts reopen the last-written session, and projects folded active History while aggregating Session Usage from raw records; `chat.edit` delegates idle-only edit admission to Chat and never falls back to Queue. `agent_refs.py` owns the cross-domain rename transaction under reference lock plus both Run guards, compensating completed steps on failure and publishing agents invalidation before Sessions/Channels/Cron so accessors remap selection first; `agent.reorder` serializes against rename/delete via the same lock with stable conflict errors; `payloads.py` owns common projections, recursive opaque-metadata stripping, and uniform `output_files` -> Markdown URL replacement; `error_mapping.py` converts typed domain errors (bare `KeyError` stays unexpected - logged with traceback and rethrown as internal failure, never a domain envelope); `file_delivery.py` signs/verifies capabilities keeping no token registry and writing no bytes.
- Statistics methods validate windows, cache the service on app state, and offload reconciliation to workers without owning index semantics. The app wires Session title/completion-read callbacks onto `resource_changed(kind="sessions")`.

## Invariants & conventions

- The server is an edge, not a second business layer: validate/map, call the owning domain, coordinate cross-domain mutations only where the public operation requires it, publish invalidation after success.
- The browser-origin guard runs before routing on HTTP and WebSocket: a supplied Origin must exactly match scheme/host/effective port; foreign/opaque/malformed/duplicate origins fail closed; origin-less requests (CLI, Desktop wakeword worker) stay valid. Never replace this with CORS response headers - they do not stop CORS-simple requests.
- Clients command through RPC/HTTP, never `/ws`; `/ws` must not bridge SSE-only deltas. Public payloads recursively strip `reasoning_meta`, `output_files`, and internal data through one shared projector (signing `/api/files/` URLs when delivery is available); ordinary history filters notes while visible errors/checkpoints/summaries/dividers follow the Chat history contract.
- Handlers projecting history/prompts/catalogs/statistics use named bounded pools, never the Event Loop or default executor - long projections cannot stall SSE/WebSocket delivery.
- Session creation is explicit at this boundary; busy sends delegate to the shared Queue rather than accessor-local state. Replay buffers are bounded and process-local - durable recovery uses Session history.
- Assistant file URLs are process-local capabilities regenerated from canonical History after restart: every request revalidates and reads the original path (later edits serve immediately, deletion/tampering 404s) - reference semantics, deliberately not snapshots.
- Multipart endpoints enforce limits before spool writes and cover bodyless requests with a total-body bound; MIME validates before core services run.

## Constraints & Gotchas

- FastAPI/uvicorn/websockets/multipart are optional dependencies; construction fails clearly when absent.
- Static serving never shadows reserved paths (`/api/*`, `/ws`, `/health`, `/_vbot/control/*`); missing `webui/dist/index.html` unmounts `/` instead of breaking startup.
- A stale control record is not authority: PID validation against the confirmed listener, atomic startup replacement, cleanup only when PID and secret both match. Never expose the token through health/RPC/logs/payloads.
- Runtime stubs must provide the services the server reads directly - no silent fallback construction. Secrets, tokens, and opaque metadata never enter logs or payloads. Endpoint-specific business logic in `app.py` or dispatcher helpers is an ownership error.

## References

Read only when your task matches:

- Shared `/ws` events, reconnect/epoch replay, presence, `resource_changed`, Run bridging, SSE replay, log-socket handoff -> `server/events-and-reconnect.md`
