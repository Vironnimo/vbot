# Debug

Captures raw provider HTTP traffic (request, response, streaming frames) for
local inspection, and probes provider model endpoints. Off by default.

## Overview

`core/debug/` owns trace storage, structured secret redaction, and a single
capture point that records provider wire traffic exactly as it goes over the
socket. Debug Mode is enabled through `settings.json` (`debug.enabled: true`).

Capture happens in **one place**: a debug-aware `httpx.AsyncClient` built by the
shared provider HTTP factory. When a recorder is attached, every request and
response that flows through that client is captured — raw method, URL, headers,
and body, plus raw streaming frames — regardless of which provider adapter
issued it. Adapters do **not** contain capture logic; they only opt in by
building their client through the factory and forwarding the active recorder.

Traces are local-only JSON files under `<data_dir>/debug/traces/`, with a
metadata-only `index.json` for listing without reading full bodies. Retention is
capped by `debug.trace_limit`; oldest traces are pruned after each write.

This domain does **not** normalize, interpret, or transform captured bodies —
they are stored as the raw bytes/text seen on the wire. The only mutation is
secret redaction.

## Data Model

### Settings (`settings.json` → `debug`)

- `enabled: boolean` — default `false`. Controls capture. Read live per request.
- `trace_limit: positive integer` — default `50`, max `500`. Retained file count.

### Trace file (`<data_dir>/debug/traces/<trace_id>.json`)

One canonical shape, shared verbatim by backend writers and the WebUI. Field
names here are the contract — neither side may read or write differently.

```jsonc
{
  "trace_id": "string",                 // uuid4 hex
  "type": "provider_request",           // or "model_probe"
  "timestamp": "ISO-8601 UTC",          // capture start
  "duration_ms": 1234,                  // null until completion

  "context": {                          // provider_request only; from DebugContext
    "run_id": "string",
    "agent_id": "string",
    "session_id": "string",
    "connection_id": "string",
    "iteration_number": 1,
    "streaming": true
  },
  "provider_id": "string",
  "model_id": "string",                 // provider_request only

  "request":  { "method": "POST", "url": "...", "headers": {}, "body": "..." },
  "response": { "status_code": 200,   "headers": {}, "body": "..." },
  "stream":   { "events": ["<raw SSE frame>", "..."] },  // streaming only
  "error":    { "type": "string", "message": "string" }  // present only on failure
}
```

- `request.body` / `response.body` are the **raw** wire payloads as text. No
  parsing, no re-serialization, no `normalized` view.
- `stream.events` holds raw SSE frames in arrival order. Omitted for
  non-streaming requests. `response.body` is `null` for streaming responses
  (the body is the frame list).
- `model_probe` traces have no `context`, `model_id`, or `stream`.

### Index entry (`<data_dir>/debug/index.json`)

Metadata only, one entry per trace, used for the trace list:

```jsonc
{ "trace_id", "type", "timestamp", "provider_id", "model_id",
  "method", "url", "status_code", "duration_ms" }
```

### Redaction

Applied to every trace before it touches disk. **Structured only** — operates on
header and query-parameter *names*, never on body content:

- **Request/response headers** and **URL query params** whose name
  (case-insensitive, split on `-`/`_`) is or whole-word-contains `authorization`,
  `token`, `secret`, `key`, `password`, `credential`, or is a cookie header.
  Matching values become `"[REDACTED]"`. This covers where credentials actually
  live on the wire (the auth header).
- **Bodies are stored raw and never redacted** — request/response bodies
  (prompts, tool output, completions) are kept verbatim. The UI warns that bodies
  are stored locally in full.
- `redact_json_body` stays an exported utility for key-level body redaction, but
  the capture path does not apply it (bodies stay raw).
- Header keys are captured as httpx normalizes them on the wire (lowercase).

## Interfaces

### `core/debug/`

Exports `DebugTraceStore`, `ProviderDebugRecorder`, `DebugContext`,
`redact_headers`, `redact_url`, `redact_json_body`.

- `DebugContext` — frozen dataclass: `run_id`, `agent_id`, `session_id`,
  `provider_id`, `connection_id`, `model_id`, `streaming: bool`,
  `iteration_number: int`.
- `DebugTraceStore(data_dir, trace_limit)`
  - `save_trace(trace_id, data: dict)` — write file, update index, prune oldest.
  - `get_traces() -> list[dict]` — index entries, newest first.
  - `get_trace(trace_id) -> dict` — full trace; raises `FileNotFoundError`.
  - `clear_all()` — delete all traces and the index.
- `ProviderDebugRecorder(store)` — holds the current `DebugContext` and receives
  raw capture callbacks from the HTTP capture transport. Owns redaction and
  persistence of one request/response cycle. It is **not** called from adapter
  bodies; the capture transport drives it.
  - `set_context(ctx: DebugContext)` — set context for the next request.

### Provider HTTP capture (`core/providers/_http_shared.py`)

- `build_async_client(*, base_url, headers, timeout, debug_recorder=None) -> httpx.AsyncClient`
  — the single client factory. With `debug_recorder`, the returned client's
  transport is wrapped so it captures request + response (teeing the stream for
  SSE) and feeds the recorder. With no recorder, returns a plain client.

### Adapter contract (`core/providers/adapter.py`)

- `ProviderAdapter.set_debug_context(ctx: DebugContext)` — base-class method,
  forwards to `recorder.set_context`. Subclasses do **not** override it and add
  no capture code. The only adapter change is constructing their client via
  `build_async_client(..., debug_recorder=...)`.

### RPC (`server/rpc/debug_methods.py`)

See `.vorch/specs/server.md` for the envelope. All gated on `debug.enabled`
except where noted.

- `debug.status` → `{ enabled, trace_limit, trace_count, data_directory }`.
  **Always available** (ungated).
- `debug.trace_list` → `{ traces }` — index entries, newest first.
- `debug.trace_get` `{ trace_id }` → `{ trace }` — full sanitized trace.
- `debug.trace_clear` → `{ cleared: true }` — delete all. **Always allowed**
  (ungated, so users can clean up after disabling).
- `debug.model_probe` `{ provider_id, connection_id }` →
  `{ trace_id, status_code, duration_ms, raw_response, model_preview }`, where
  `model_preview` is `{ model_count, models: [{ id, name }] }` (first 10).
  Fetches the connection's `models_endpoint`, stores a `model_probe` trace, and
  does **not** write `resources/models/*.json` or reload the registry.

## Conventions

- Settings are read live from storage per request; debug state is never cached
  across the enable/disable toggle.
- Capture is best-effort and must never affect provider results: a failure in
  redaction, persistence, or the capture transport is logged `warn` and
  swallowed so the provider call still returns.
- Debug context is stored separately from provider payloads. It must never enter
  `**kwargs`, `_build_payload()`, or any provider-bound request body.
- Trace data never crosses the chat/SSE/WebSocket boundary. It is reachable only
  through `debug.*` RPCs.
- All `debug.*` user-visible UI strings go through i18n.

## Constraints & Gotchas

- The trace shape in **Data Model** is a hard contract. The previous version
  drifted (backend wrote nested `request`/`response`; the WebUI read flat
  `request_body`/`response_status`/`normalized_response`), so the detail view
  showed empty panes. Backend writers and the WebUI must use these exact field
  names.
- Capture lives only in `build_async_client` / its transport. Do not reintroduce
  `if self._debug_recorder is not None:` capture blocks inside adapter
  `send()` / `stream()` bodies — that scatter was the source of the original
  bloat.
- Streaming bodies are captured by teeing the response byte stream. The tee must
  not buffer the whole stream before yielding to the adapter, or it changes
  streaming latency/back-pressure.
- Trace files are not size-truncated; a single trace is bounded only by the
  provider response size. `trace_limit` caps file count, not bytes.
- `debug.model_probe` is diagnostic only and never mutates the model catalog.
- Providers that route across multiple endpoints (e.g. GitHub Copilot) get
  capture for free as long as every endpoint call goes through a client built by
  `build_async_client`. A provider that constructs a raw `httpx.AsyncClient`
  directly will silently not be traced.
