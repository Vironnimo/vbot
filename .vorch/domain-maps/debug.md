# Debug

Captures complete raw provider HTTP or WebSocket exchanges for local inspection, and probes provider model endpoints. Off by default.

## Overview

`core/debug/` owns trace storage, structured secret redaction, and the recorder capturing provider wire traffic exactly as it crosses the socket. Enabled via `settings.json` (`debug.enabled`, read live per request). All Provider traffic feeds one canonical recorder contract: HTTP capture happens inside a debug-aware client built by the shared Provider HTTP factory, and the sanctioned non-HTTP exception is OpenAI Subscription WebSocket streaming - each `response.create` exchange opens its own capture because those frames never pass through httpx.

Traces are local-only JSON files under `<data_dir>/artifacts/debug/traces/` plus a metadata-only `index.json` for listing without reading bodies (placement owned by Storage; schema/redaction/retention/authorization owned here). Retention caps file count at `debug.trace_limit` (default 50, max 500), pruning oldest after each write. The domain does **not** normalize or interpret captured bodies - the only mutation is secret redaction.

## Trace contract (hard)

```jsonc
{
  "trace_id": "uuid4-hex",
  "type": "provider_request",      // or "model_probe"
  "timestamp": "ISO-8601 UTC",
  "duration_ms": 1234,             // null until completion
  "context": {                     // provider_request only
    "run_id", "agent_id", "session_id",
    "connection_id", "iteration_number", "streaming"
  },
  "provider_id": "...",
  "model_id": "...",               // provider_request only
  "request":  { "method", "url", "headers", "body" },
  "response": { "status_code", "headers", "body" },
  "error":    { "type", "message" }   // present only on failure
}
```

- Bodies are the **raw** wire payloads as text - no parsing, re-serialization, or normalized view. Streaming HTTP aggregates the complete transport body including SSE framing; WebSocket calls record method `WEBSOCKET`, upgrade status, sent frame, and newline-joined received frames. Successful streams are **not** split into per-event records. `model_probe` traces omit context and carry empty model id. The index entry holds only `{trace_id, type, timestamp, provider_id, model_id, method, url, status_code, duration_ms}`.

### Redaction

Applied before disk, **structured only** - on header and query-parameter *names*, never body content: names matching exactly `authorization`/`x-api-key` (lower-cased) or containing a whole split-on-dash/underscore/dot word from `{token, secret, key, password, credential}` redact to `[REDACTED]` (`x-api-key`, `x_token_header`, `api-secret`, and `auth.token` match; `donkey` does not). There is **no** cookie rule - cookies capture raw unless named into a match. Bodies stay verbatim (the UI warns they persist locally in full); `redact_json_body` remains exported for other uses but the capture path does not apply it. Header keys record as httpx lower-cases them.

## Interfaces

- `DebugTraceStore(data_dir, trace_limit)`: save (writes file, updates index, prunes - index reads method/url/status from inside the request/response objects, so flattening writers break the index), list newest-first, full get validating canonical lowercase uuid4 hex **before any filesystem access**, clear-all.
- `ProviderDebugRecorder(store)` holds one shared `DebugContext`; transports drive it via `begin_capture(method/url/headers/body)` which redacts request URL+headers, stores the body raw, and returns a **fresh per-request capture** teeing the response until `finalize()` persists - concurrent/retried calls never share buffers.
- `build_async_client(...)` is the single HTTP client factory: with a recorder its transport wraps capture (teeing streaming bytes into the aggregate body), without one it is a plain zero-overhead client. There is no per-client headers argument.
- Adapter contract: `set_debug_context` is base-class only (subclasses never override); HTTP-only adapters add no capture code, while stateful non-HTTP transports must feed the same recorder explicitly. Isolated `analyze_image` subrequests set the complete context immediately before send, reusing the parent Iteration without advancing it.
- Lifecycle: Runtime builds a fresh recorder + store **per adapter construction**, reading settings live - toggling Debug Mode takes effect on the next adapter construction rather than mutating an active Run's transport.
- RPCs: `debug.status` and `debug.trace_clear` are always available (so users can clean up after disabling); listing/getting gate on enabled. `model_probe` resolves credentials, GETs the provider's models endpoint over a deliberately raw client, stores its own trace, and never mutates the catalog. Trace-list freshness rides the event bus via `resource_changed(kind: "debug_traces")` on terminal Runs, probes, and clears - DebugView has no manual refresh.

## Conventions & Gotchas

- Capture is best-effort and must never affect results: redaction/persistence/transport failures log warn and swallow so the Provider call still returns.
- The trace shape above is a hard contract - a previous drift (nested write vs flat read) produced empty detail panes; both sides use exactly these field names.
- HTTP capture lives only in the client factory/transport - never reintroduce per-adapter capture blocks; non-HTTP transports capture at their wire-exchange boundary under the same contract.
- Streaming capture tees bytes without buffering the whole stream first (latency/back-pressure); aggregation happens from already-tee-captured bytes, never inside adapter `stream()` implementations.
- Traces are not size-truncated - `trace_limit` caps count, not bytes. Trace data never crosses chat/SSE/WebSocket boundaries; only `debug.*` RPCs reach it. Debug context stays out of `**kwargs` and provider-bound bodies. WebUI detail is request/response-first and must not present stream events as the response; UI strings go through i18n.
- Multi-endpoint providers (e.g. GitHub Copilot) trace for free through factory-built clients; a provider constructing a raw client silently escapes tracing - the sanctioned exception is `model_probe` writing its own trace with the same redaction helpers.
