# Debug

First-party Debug Mode for capturing sanitized provider wire traces and running
raw model-endpoint probes.

## Overview

`core/debug/` owns the trace storage lifecycle, structured secret redaction, and
per-request debug recording. Debug Mode is off by default. When enabled through
`settings.json` (`debug.enabled: true`), every provider HTTP request is captured
at the adapter boundary: complete request/response bodies and complete streaming
SSE frames are stored after structured secret redaction, with retention capped
by `debug.trace_limit`.

Captured traces are local-only flat JSON files under `<data_dir>/debug/traces/`.
An `index.json` provides metadata-only access for trace browsing without loading
full bodies.

## Data Model

### Settings

- `debug.enabled: boolean` — default `false`. Controls whether traces are captured.
- `debug.trace_limit: positive integer` — default `50`, max `500`. Maximum number of trace files retained. Oldest traces are deleted after every capture.

### Trace Storage

Each trace is a JSON file `<trace_id>.json` under `<data_dir>/debug/traces/`.
The `index.json` file contains metadata-only entries keyed by `trace_id`:

```json
{
  "<trace_id>": {
    "timestamp": "2026-06-04T12:00:00+00:00",
    "provider_id": "openrouter",
    "model_id": "anthropic/claude-sonnet-4",
    "request_method": "POST",
    "request_url": "https://openrouter.ai/api/v1/chat/completions",
    "status_code": 200,
    "duration_ms": 1234
  }
}
```

Full trace objects include all metadata fields plus `request`, `response`,
`stream_events[]`, `error`, `normalized`, and the debug context
(`run_id`, `agent_id`, `session_id`, `connection_id`, `streaming`, `iteration_number`).

### Redaction Rules

Secrets are redacted before any trace data is written to disk. Redaction is
**structured only** — never scans free-text string values:

- **Headers:** `Authorization`, `x-api-key`, cookies, and any header name containing whole-word matches for `token`, `secret`, `key`, `password`, or `credential` (case-insensitive, split on `-`/`_`).
- **URL query params:** Same key-name rules applied to query parameter names.
- **JSON object keys:** Same key-name rules; matching keys have their values replaced with `"[REDACTED]"`.

Raw prompts and tool output content is **not** redacted. The Debug UI warns that
prompts and tool results are stored locally.

## Interfaces

- `core/debug/__init__.py` exports `DebugTraceStore`, `ProviderDebugRecorder`, `DebugContext`, and redaction utilities (`redact_headers`, `redact_url`, `redact_json_body`).
- `DebugTraceStore(data_dir, trace_limit)` — manages trace file I/O and retention.
  - `save_trace(trace_id, data)` — writes trace JSON, updates index, prunes oldest.
  - `get_traces() -> list` — returns index metadata, newest first.
  - `get_trace(trace_id) -> dict` — returns full trace JSON.
  - `clear_all()` — deletes all traces and index.
- `ProviderDebugRecorder(store)` — per-request trace lifecycle.
  - `start_request(ctx: DebugContext)` — begins recording with run/agent/session/provider/model context.
  - `capture_request(method, url, headers, body)` — captures redacted request.
  - `capture_response(status, headers, body, duration_ms)` — captures redacted response.
  - `capture_stream_event(raw, parsed)` — captures one SSE frame.
  - `capture_error(error)` — captures exception details.
  - `finish()` — persists the trace and triggers retention.
- `DebugContext` — dataclass with `run_id`, `agent_id`, `session_id`, `provider_id`, `connection_id`, `model_id`, `streaming` (bool), `iteration_number` (int).
- `redact_headers(dict) -> dict`, `redact_url(str) -> str`, `redact_json_body(any) -> any` — structured redaction utilities.

## Recorder Lifecycle

1. `Runtime.get_adapter()` creates a `DebugTraceStore` and `ProviderDebugRecorder` when `debug.enabled` is true, and sets `adapter._debug_recorder`.
2. `ChatLoop._send_until_final()` calls `adapter.set_debug_context(ctx)` before each `send()`/`stream()` call, passing run context. Only called when `adapter._debug_recorder` is set (guarded by `hasattr`).
3. Each adapter captures at its HTTP boundary: after mapping/defaults, immediately before HTTP send, and after response/stream completion.
4. Debug context is stored separately from provider-bound payloads — it never enters `**kwargs` or `_build_payload()`.

## RPC Contract

See `.vorch/specs/server.md` for the full server RPC listing.

### Debug RPCs

- `debug.status` → `{ enabled, trace_limit, trace_count, data_directory }` — always available.
- `debug.trace_list` → metadata array from index, newest first. Gated on `debug.enabled`.
- `debug.trace_get` with `{ trace_id }` → full sanitized trace. Gated on `debug.enabled`.
- `debug.trace_clear` → deletes all traces and index. **Always allowed** (not gated).
- `debug.model_probe` with `{ provider_id, connection_id }` → fetches provider's `models_endpoint`, stores a `model_probe` trace, returns raw response + normalized model preview. Does NOT write `resources/models/*.json` or reload the model registry. Gated on `debug.enabled`.

## WebUI

The Debug tab appears in the top-level navigation only when `debug.enabled` is
true. It provides:
- Trace list with metadata columns (timestamp, provider, model, method, status, duration)
- Detail pane with tabs: Metadata, Request, Response, Stream Events, Normalized Data
- Model Endpoint Probe section with provider/connection selectors
- Trace limit control and clear button
- Local-warning copy about raw prompt/tool output storage

Settings includes a Debug panel with the enabled toggle, trace limit input, and
the same local-warning copy.

## Constraints & Gotchas

- Debug Mode is off by default and never exposed through chat history, logs, SSE, or WebSocket run events.
- Trace files have no size truncation — individual traces are bounded by single provider response sizes.
- `trace_limit` controls retained file count, not bytes.
- `debug.model_probe` is diagnostic only; it never mutates model catalog resources.
- OpenCodeGo's inner `AnthropicAdapter` receives the debug recorder via explicit propagation from the outer adapter.
- GitHub Copilot's multi-endpoint routing is captured via shared `_post_json()` and `_connect_stream()` helpers.
- Streaming SSE events are captured per-event as they flow through the stream iterator — no separate body read.
- `debug.trace_clear` is always allowed even when debug is disabled, so users can clean up files after disabling.
