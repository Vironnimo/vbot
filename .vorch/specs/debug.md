# Debug

Captures complete raw provider HTTP requests and responses for local inspection, and probes provider model endpoints. Off by default.

## Overview

`core/debug/` owns trace storage, structured secret redaction, and the recorder that captures provider wire traffic exactly as it goes over the socket. Debug Mode is enabled through `settings.json` (`debug.enabled: true`).

Capture happens in **one place**: a debug-aware `httpx.AsyncClient` built by the shared provider HTTP factory. When a recorder is attached, every request and response that flows through that client is captured — raw method, URL, headers, and complete body — regardless of whether the provider call is streaming or non-streaming and regardless of which provider adapter issued it. Adapters do **not** contain capture logic; they only opt in by building their client through the factory and forwarding the active recorder.

Traces are local-only JSON files under `<data_dir>/debug/traces/`, with a metadata-only `index.json` for listing without reading full bodies. Retention is capped by `debug.trace_limit`; oldest traces are pruned after each write.

This domain does **not** normalize, interpret, or transform captured bodies — they are stored as the raw bytes/text seen on the wire. The only mutation is secret redaction.

## Data Model

### Settings (`settings.json` → `debug`)

- `enabled: boolean` — default `false`. Controls capture. Read live per request.
- `trace_limit: positive integer` — default `50`, max `500`. Retained file count.

### Trace file (`<data_dir>/debug/traces/<trace_id>.json`)

One canonical shape, shared verbatim by backend writers and the WebUI. Field names here are the contract — neither side may read or write differently.

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
  "error":    { "type": "string", "message": "string" }  // present only on failure
}
```

- `request.body` / `response.body` are the **raw** wire payloads as text. No parsing, no re-serialization, no `normalized` view.
- For streaming provider calls, `response.body` is the complete aggregate raw streaming HTTP body exactly as captured from the transport, including SSE framing text such as `data:` lines and frame separators. Debug traces do **not** split successful streaming responses into per-event records.
- `model_probe` traces omit `context`; `model_id` is the empty string.

### Index entry (`<data_dir>/debug/index.json`)

Metadata only, one entry per trace, used for the trace list:

```jsonc
{ "trace_id", "type", "timestamp", "provider_id", "model_id",
  "method", "url", "status_code", "duration_ms" }
```

### Redaction

Applied to every trace before it touches disk. **Structured only** — operates on header and query-parameter *names*, never on body content:

- **Request/response headers** and **URL query params** are redacted to `"[REDACTED]"` when their name (lower-cased) is an exact match for `authorization` or `x-api-key`, **or** when splitting the name on `-`/`_` yields a whole word in `{token, secret, key, password, credential}`. So `x-api-key`, `x_token_header`, and `api-secret` all match; `donkey` does not. There is **no** cookie-name rule — a `Cookie` / `Set-Cookie` header is captured raw unless its name happens to contain one of those words.
- **Bodies are stored raw and never redacted** — request/response bodies (prompts, tool output, completions) are kept verbatim. The UI warns that bodies are stored locally in full.
- `redact_json_body` stays an exported utility for key-level body redaction, but the capture path does not apply it (bodies stay raw).
- Header keys are captured as httpx normalizes them on the wire (lowercase).

## Interfaces

### `core/debug/`

Exports `DebugTraceStore`, `ProviderDebugRecorder`, `DebugContext`, `redact_headers`, `redact_url`, `redact_json_body`.

- `DebugContext` — frozen dataclass: `run_id`, `agent_id`, `session_id`, `provider_id`, `connection_id`, `model_id`, `streaming: bool`, `iteration_number: int`.
- `DebugTraceStore(data_dir, trace_limit)`
  - `save_trace(trace_id, data: dict)` — write file, update index, prune oldest. The index entry reads `method`/`url` from `data["request"]` and `status_code` from `data["response"]`, so a writer that flattens those fields produces a broken index.
  - `get_traces() -> list[dict]` — index entries, newest first.
  - `get_trace(trace_id) -> dict` — full trace; raises `FileNotFoundError`.
  - `clear_all()` — delete all traces and the index.
  - `get_data_dir() -> Path` — the `<data_dir>/debug/` directory.
- `ProviderDebugRecorder(store)` — holds one shared `DebugContext` and is the entry point the capture transport drives; it keeps **no** per-request state and is never called from adapter bodies.
  - `set_context(ctx: DebugContext)` — set the context applied to the next captured request(s).
  - `begin_capture(*, method, url, headers, body) -> capture` — called by the transport before the request goes out. Redacts the request URL + headers, stores the body raw, and returns a **fresh per-request capture**; that capture tees the response body and, on `finalize()`, builds the canonical trace and persists it. A separate capture per request means concurrent or retried calls never share buffers.

### Provider HTTP capture (`core/providers/_http_shared.py`)

- `build_async_client(*, base_url, timeout=None, debug_recorder=None) -> httpx.AsyncClient` — the single client factory. There is no per-client `headers` argument; headers are passed per request. With `debug_recorder`, the returned client's transport is wrapped (`_DebugCaptureTransport`) so it captures request + response, teeing the byte stream for streaming responses into the aggregate `response.body`, and feeds the recorder. With no recorder, returns a plain client with zero capture overhead.

### Adapter contract (`core/providers/adapter.py`)

- `ProviderAdapter.set_debug_context(ctx: DebugContext)` — base-class method, forwards to `recorder.set_context`. Subclasses do **not** override it and add no capture code. The only adapter change is constructing their client via `build_async_client(..., debug_recorder=...)`.
- Recorder lifecycle: `Runtime._build_debug_recorder()` (`core/runtime/runtime.py`) builds a fresh `ProviderDebugRecorder` + `DebugTraceStore` **each time it constructs an adapter**, reading `debug.enabled` / `trace_limit` live, and returns `None` when debug is off. Adapters are built per provider call, so toggling Debug Mode takes effect on the next call — no restart, no cached recorder.

### RPC (`server/rpc/debug_methods.py`)

See `.vorch/specs/server.md` for the envelope. All gated on `debug.enabled` except where noted.

- `debug.status` → `{ enabled, trace_limit, trace_count, data_directory }`. **Always available** (ungated).
- `debug.trace_list` → `{ traces }` — index entries, newest first.
- `debug.trace_get` `{ trace_id }` → `{ trace }` — full sanitized trace.
- `debug.trace_clear` → `{ cleared: true }` — delete all. **Always allowed** (ungated, so users can clean up after disabling).
- `debug.model_probe` `{ provider_id, connection_id }` → `{ trace_id, status_code, duration_ms, raw_response, model_preview }`. `model_preview` is `{ model_count, models: [{ id, name }] }` (first 10) on a 200 with parseable JSON, or `{ error, models: [] }` on a non-200 / non-JSON response. Resolves the connection credential (API key or OAuth), GETs the provider's `models_endpoint` over a **raw** `httpx.AsyncClient`, stores a `model_probe` trace, and does **not** write `resources/models/*.json` or reload the registry.

## Conventions

- Settings are read live from storage per request; debug state is never cached across the enable/disable toggle.
- Capture is best-effort and must never affect provider results: a failure in redaction, persistence, or the capture transport is logged `warn` and swallowed so the provider call still returns.
- Debug context is stored separately from provider payloads. It must never enter `**kwargs`, `_build_payload()`, or any provider-bound request body.
- Trace data never crosses the chat/SSE/WebSocket boundary. It is reachable only through `debug.*` RPCs.
- The WebUI Debug detail view is request/response-first: primary trace detail tabs are Metadata, Request, and Response. It must not present individual stream events as the response.
- All `debug.*` user-visible UI strings go through i18n.

## Constraints & Gotchas

- The trace shape in **Data Model** is a hard contract. The previous version drifted (backend wrote nested `request`/`response`; the WebUI read flat `request_body`/`response_status`/`normalized_response`), so the detail view showed empty panes. Backend writers and the WebUI must use these exact field names.
- Capture lives only in `build_async_client` / its transport. Do not reintroduce `if self._debug_recorder is not None:` capture blocks inside adapter `send()` / `stream()` bodies — that scatter was the source of the original bloat.
- Streaming bodies are captured by teeing the response byte stream. The tee must not buffer the whole stream before yielding to the adapter, or it changes streaming latency/back-pressure. Aggregation into `response.body` happens from bytes already tee-captured by the recorder and must not move into adapter `stream()` implementations.
- Trace files are not size-truncated; a single trace is bounded only by the provider response size. `trace_limit` caps file count, not bytes.
- `debug.model_probe` is diagnostic only and never mutates the model catalog.
- Providers that route across multiple endpoints (e.g. GitHub Copilot) get capture for free as long as every endpoint call goes through a client built by `build_async_client`. A provider that constructs a raw `httpx.AsyncClient` directly will silently not be traced — the one sanctioned exception is `debug.model_probe`, which uses a raw client on purpose and writes its own trace via `_save_model_probe_trace` with the same `redact_headers` / `redact_url`.
