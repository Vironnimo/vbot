# Server Events and Reconnect

Task-gated reference for the shared `/ws` event bus, Run bridging, `resource_changed`, window presence, SSE replay, and reconnect handshakes. Read this when changing server-push, invalidation, replay/catch-up, or active-Run recovery; it is not required for ordinary RPC handler work.

## Server Event Bus

`server/events.py::ServerEventBus` is an in-memory replayable bus over `core/event_stream.ReplayEventStream`. Every published event has monotonically increasing process-local `sequence`, a bus-generation `epoch`, allowed `type`, JSON-object `payload`, and UTC `timestamp`. The bus retains a bounded event window and gives each live subscriber a bounded queue; a lagging subscriber is evicted and must reconnect. Sequence values are not reused within one process even after old events fall out of retention.

Allowed server event types are `app_error`, Run lifecycle summaries (`run_started`, `run_output`, `run_completed`, `run_cancelled`, `run_failed`), `provider_auth_completed`, and `resource_changed`. Event type and resource kind allowlists are enforced at publish seams; add new entries there rather than bypassing the bus contract.

The buffers are not durable. Old conversation state comes from Session history, active Run state from the connection handshake, and missed retained Run detail from SSE while the Run still exists.

## `/ws` handshake and replay

The shared `/ws` socket is server-push only. Clients send commands through RPC. On connect `server/app.py` sends one direct, connection-specific frame that never enters bus retention:

`{"type":"connection_ready","epoch":"<bus generation>","last_sequence":<high water>,"active_runs":[{"run_id","agent_id","project_id","session_id","status":"running","sse_url"}]}`

`agent_id` is bare and `project_id` is separate (`null` for Identity Sessions) so clients can rebuild `agent@projekt`. The hello has no event `sequence` field and must not advance client sequence bookkeeping.

The client may reconnect with `epoch` and `after_sequence`. A matching epoch plus positive sequence resumes after the client's value. A missing/stale epoch or zero sequence starts live-only at the hello's `last_sequence`; the active-Run snapshot is authoritative for initial state. The server reads the high-water mark before awaiting the hello send, then subscribes from the chosen floor, so events published during the send remain in the retained deque and replay without a gap.

## Window presence

`server/clients.py::ClientRegistry` is a momentary in-memory roster of open browser/Desktop `/ws` windows. CLI calls and Channels do not register. Each entry has a server-minted unregister `id`, client-minted `connection_id`, normalized accessor, coarse browser/OS labels, UTC `connected_at`, and constant `connected` status.

Connect registers and publishes `resource_changed(kind="clients")` before the hello high-water mark is read. Therefore the connecting window's own presence event is at or below its live-only floor while other windows receive it. After the hello, the shared socket races its outgoing event stream against inbound disconnect detection, so an otherwise idle closed window unregisters immediately instead of waiting for another server event; the handler's `finally` cleans up every exit path and publishes another clients invalidation.

The registry emits one `INFO` log at each logical app-window presence boundary: the first active registration for a client-minted `connection_id` logs connect, and removal of its last active registration logs disconnect with the elapsed duration. Overlapping old/new sockets during a reconnect therefore remain one logged presence cycle even though both momentary socket entries may briefly exist in the roster. Log rows use only the normalized accessor/browser/OS labels and the first eight characters of the client id; they never include the raw User-Agent. Connections without a client-minted id are treated as independent registrations.

## Run bridge

`server/rpc/event_bridge.py` subscribes to every Run started by the shared `ChatRunManager`, including RPC starts, queued starts, Automation, and Sub-Agent work. The bridge is idempotent within a bounded Run-id retention window so manager callbacks and explicit RPC bridging cannot duplicate summaries.

Streaming deltas (`assistant_output_delta`, `reasoning_delta`, `tool_call_delta`, stdout/stderr) are deliberately excluded from `/ws`; SSE is their transport. Stable Run output and terminal events become the small server lifecycle types and carry `run_id`, bare `agent_id`, `project_id`, `session_id`, original Run event type/sequence/timestamp, and sanitized output/terminal fields. Opaque Provider metadata is recursively removed. Terminal bridges also publish `resource_changed(kind="debug_traces")` and a scoped `resource_changed(kind="sessions")` after the terminal summary so trace views and durable completion projections re-fetch.

`working_project_id` is internal execution state and never appears in SSE, WebSocket, Queue, history, or public Run payloads.

## `resource_changed`

`publish_resource_changed(state, kind, scope=None)` is the single generic invalidation seam. Payload is `{kind, scope?}` and contains no resource data; consumers re-fetch through normal RPC. It no-ops when no bus exists (CLI/runtime stubs) and rejects unknown kinds. Current allowed kinds are `models`, `queue`, `sessions`, `agents`, `providers`, `clients`, `channels`, `debug_traces`, `projects`, and `cron`.

Emission belongs to the server mutation edge, never `core/`. Representative ownership:

- Model Refresh → `models`; credential/Connection changes → `providers`; Agent CRUD → `agents`; Project mutations/removal → `projects` and any affected `agents`.
- Session create/rename/delete, title-change callbacks, terminal Runs, and successful completion read acknowledgements → scoped `sessions`; deleting an Identity Agent's current Session also invalidates `agents` because its current pointer changes.
- RPC Queue mutations and the queued branches of `chat.send`/`chat.stream` → scoped `queue`. Core-origin enqueues intentionally do not publish this browser invalidation.
- Channel mutations → `channels`; `/ws` presence lifecycle → `clients`; terminal Run bridge and Debug mutations → `debug_traces`.
- `CronService.add_changed_callback` is bridged in `server/app.py` → `cron`, covering RPC/Tool mutations and scheduler-owned status/health transitions without importing the server bus into core.

The exact emitters remain source-of-truth in `server/rpc/*_methods.py`, `server/rpc/event_bridge.py`, and `server/app.py`. A new consumer normally requires one allowed kind, one mutation-edge emit, and one client reload path rather than a new event family.

## SSE and log WebSocket

`GET /api/runs/{run_id}/events` streams the complete provider-agnostic Run timeline over SSE. It replays after an explicit `after_sequence` query value or, when absent, `Last-Event-ID`; invalid/negative values clamp to zero. Every Run frame uses the Run event sequence as SSE `id`, the Run event type as `event`, and sanitized event JSON as `data`, then follows until terminal state. While a running Run is quiet, the server emits transport-only `heartbeat` events without a Run sequence; clients use them for liveness but never add them to timeline or replay state.

`/ws/logs` is separate from the shared bus. It subscribes to one log file using the cursor returned by `log.read`, preventing the read-to-subscribe gap, and emits file append/reset plus catalog updates. Log transport behavior belongs in `logs.md`; it has no bus epoch or Run semantics.

## Source and tests

- Bus and allowlists: `server/events.py`; `tests/server/test_events.py`.
- Shared WebSocket/SSE and handshake: `server/app.py`; `tests/server/test_websocket.py` and `test_sse.py`.
- Run/resource bridge: `server/rpc/event_bridge.py`; `tests/server/rpc/test_event_bridge.py` and `test_rpc_payload_events.py`.
- Presence: `server/clients.py`; `tests/server/test_clients.py` and `tests/server/rpc/test_client_methods.py`.
