# Server

FastAPI transport layer around the core kernel.

## Overview

`server/` owns HTTP, Server-Sent Events (SSE), WebSocket, process startup, and
request/response mapping. It imports `core/` services but does not own chat,
agent, provider, model, tool, skill, or storage business logic.

Clients call the vBot server contract; provider wire details stay behind
`core/providers/` adapters.

## Data model

- RPC envelope: `POST /api/rpc` accepts a JSON object with `method` and optional
  `params`, and returns `{ "ok": true, "result": ... }` or `{ "ok": false,
  "error": { "code": ..., "message": ... } }`.
- WebUI-facing RPC methods include `agent.list`, `agent.create`, `agent.update`,
  `agent.delete`, `session.create`, `chat.history`, `chat.send`, `chat.stream`,
  and `chat.cancel`.
- `agent.delete` rejects deletion when it would leave zero Agents.
- `agent.delete` serializes the list/check/delete sequence with a process-local
  `asyncio.Lock` so concurrent deletes in one server process cannot leave zero
  Agents. Cross-process/shared-data-dir locking is out of scope.
- Public Agent create/update RPCs validate mutable fields and reject unsupported
  fields. `workspace` is intentionally not accepted through public RPC in Phase 4.
- `session.create` accepts optional `make_current: true`; when set, the created
  Session ID is persisted to the Agent's `current_session_id`.
- `chat.history` returns visible persisted messages for `{ agent_id,
  session_id? }`. If `session_id` is omitted, it loads the Agent's
  `current_session_id`.
- `chat.send` and `chat.stream` target an existing Session and start a core Run
  through the shared `ChatLoop.start_run()` execution model.
- `chat.stream` returns a `run_id` and SSE URL; the SSE endpoint streams stable
  vBot Run events, not provider chunks.
- `chat.cancel` targets a Run ID, not a Session.
- Server event bus events contain lifecycle summaries for WebSocket clients:
  `run_started`, `run_output`, `run_completed`, `run_cancelled`, and
  `run_failed`. Agent CRUD events: `agent.created`, `agent.updated`,
  `agent.deleted` (full agent payload via `_agent_response`).

## Interfaces

- `server.app.create_app(runtime=None, config=None)` — creates the FastAPI app,
  starts/stops `Runtime` during lifespan, and wires `runtime`, `ChatRunManager`,
  `ChatLoop`, and the server event bus into `app.state`.
- `server.delegates.dispatch_rpc(state, request)` — validates and dispatches RPC
  methods to transport-only delegates.
- `GET /api/runs/{run_id}/events` — streams one Run timeline as SSE using
  `text/event-stream`, replaying existing events and then following new events
  until a terminal Run event. Each SSE event includes `id: <RunEvent.sequence>`
  so native EventSource reconnect can resume with `Last-Event-ID`.
- The Run SSE endpoint supports replay filtering with optional
  `after_sequence`; explicit query parameter wins over `Last-Event-ID`, and
  malformed/negative values clamp to replay from the beginning.
- `server.events.ServerEventBus` — in-memory replayable bus for general server
  lifecycle events sent over `/ws`. Supports `after_sequence` query param for
  reconnect replay: clients pass the last sequence number they saw, and the bus
  replays all events with a higher sequence before streaming new ones.
- `server.main.main(argv=None)` — starts uvicorn. Port priority is `--port` >
  `VBOT_SERVER_PORT` > `settings.json` > `8420`; ambient `PORT` /
  `SERVER_PORT` process environment variables are ignored unless they are keys
  inside `settings.json`.
- FastAPI serves built WebUI assets from `webui/dist` when `index.html` exists.
  `/assets/*` maps to Vite assets and non-reserved routes fall back to
  `index.html` for single-page-app refreshes.

## Conventions

- Server code maps expected domain errors to provider-agnostic RPC errors.
- Opaque provider metadata such as `reasoning_meta` must not appear in public
  server payloads, including nested SSE/WebSocket event payloads.
- Session creation is explicit at the server/product boundary.
- Only one active Run is allowed per Session; parallel Runs in different
  Sessions are allowed.
- WebSocket is the persistent signalling channel for app-wide events (connection
  status, agent CRUD, run lifecycle summaries). It is server-push only; clients
  send requests via `POST /api/rpc`.
- SSE is the primary per-Run output stream and should remain event-level and
  provider-agnostic.
- Public history, Run, SSE, and WebSocket payloads must strip opaque provider
  metadata such as `reasoning_meta` recursively.
- Streaming delta Run events (`assistant_output_delta`, `reasoning_delta`,
  `tool_call_delta`) are SSE-only. They must not be bridged to WebSocket
  lifecycle summaries.

## Constraints & gotchas

- The server optional dependency group provides FastAPI, uvicorn, and websockets.
  Server code should fail clearly if these extras are not installed.
- Exact long-term payload schemas remain intentionally lightweight in Phase 3;
  keep schema decisions isolated in `server/delegates.py` and transport files.
- WebUI static serving is optional at runtime. If `webui/dist/index.html` is
  absent, `/` remains unmounted/404 rather than failing server startup.
- Static single-page-app fallback must not shadow reserved server paths:
  `/api/*`, `/ws`, and `/health`.
