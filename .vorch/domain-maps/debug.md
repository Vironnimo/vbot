# Debug

Captures complete raw provider HTTP or WebSocket exchanges for local inspection, and probes provider model endpoints. Off by default.

## Overview

`core/debug/` owns trace storage, structured secret redaction, and the recorder capturing provider wire traffic exactly as it crosses the socket. Enabled via `settings.json` (`debug.enabled`, read live per request). All Provider traffic feeds one canonical recorder contract: HTTP capture happens inside a debug-aware client built by the shared Provider HTTP factory, and the sanctioned non-HTTP exception is OpenAI Subscription WebSocket streaming - each `response.create` exchange opens its own capture because those frames never pass through httpx.

Traces are local-only JSON files under `<data_dir>/artifacts/debug/traces/` plus a metadata-only `index.json` for listing without reading bodies (placement owned by Storage; schema/redaction/retention/authorization owned here). Retention caps file count at `debug.trace_limit` (default 50, max 500), pruning oldest after each write. The domain does **not** normalize or interpret captured bodies - the only mutation is secret redaction.

## Trace contract (hard)

Trace files and the index use atomic replacement. Every store operation in the process, across adapter instances, runs on one dedicated trace thread (the `debug-traces` `OrderedWorker`, `runtime.md`) in submission order, never on the Event Loop (a save fsyncs two files: seconds on a busy disk). A capture is handed off without waiting; a read or clear queued later observes it, so a listing refreshed after a terminal Run includes that Run's traces. At most `MAX_PENDING_CAPTURES` (32) handed-off captures wait at once; a further capture is dropped with a WARNING. Runtime shutdown awaits `drain_debug_traces()`. Index publication precedes pruning, so a failed index write preserves previously listed traces and removes the new unindexed capture. Each save also removes orphan captures and trace temporary files left by interrupted writes before adding another capture. Clear removes the owned trace tree, including nested leftovers, without following directory links.

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

Applied before disk, **structured only** - on request and response header and query-parameter *names*, never body content: names matching exactly `authorization`/`x-api-key` (lower-cased) or containing a whole split-on-dash/underscore/dot word from the credential words `{token, secret, key, password, credential}` or the identifying words `{account, organization, cookie}` redact to `[REDACTED]` (`x-api-key`, `x_token_header`, `api-secret`, `auth.token`, `chatgpt-account-id`, `openai-organization`, `cookie`, `set-cookie`, and an `account_id` query parameter match; `donkey` and `accounting` do not). The identifying words keep Provider Account ids and session cookies out of traces. The Provider capture path and `model_probe` share these helpers. Bodies stay verbatim (the UI warns they persist locally in full); `redact_json_body` remains exported for other uses but the capture path does not apply it. Header keys record as httpx lower-cases them.

## Interfaces

- `DebugTraceStore(data_dir, trace_limit)`: save (writes file, updates index, prunes - index reads method/url/status from inside the request/response objects, so flattening writers break the index), list newest-first, full get validating canonical lowercase uuid4 hex **before any filesystem access**, clear-all. Each exists as a blocking method (`save_trace`, `get_traces`, `get_trace`, `clear_all`) and as an Event-Loop-safe `*_async` twin the `debug.*` and `settings.get` RPCs use; `save_trace_in_background(trace_id, build_trace)` hands a capture off and returns `False` when the backlog is full.
- `ProviderDebugRecorder(store)` holds one shared `DebugContext`; transports drive it via `begin_capture(method/url/headers/body)` which redacts request URL+headers, stores the body raw, and returns a **fresh per-request capture** teeing the response until `finalize()` - concurrent/retried calls never share buffers. `finalize()` fixes duration, timestamp and received body, then hands building and writing the trace to the trace thread: the Provider call never waits for the durable write.
- `build_async_client(...)` is the single HTTP client factory: with a recorder its transport wraps capture (teeing streaming bytes into the aggregate body), without one it is a plain zero-overhead client. There is no per-client headers argument.
- Adapter contract: `set_debug_context` is base-class only (subclasses never override); HTTP-only adapters add no capture code, while stateful non-HTTP transports must feed the same recorder explicitly. Isolated `analyze_image` subrequests set the complete context immediately before send, reusing the parent Iteration without advancing it.
- Lifecycle: Runtime builds a fresh recorder + store **per adapter construction**, reading settings live - toggling Debug Mode takes effect on the next adapter construction rather than mutating an active Run's transport.
- RPCs: `debug.status` and `debug.trace_clear` are always available (so users can clean up after disabling); listing/getting gate on enabled. `model_probe` resolves the same discovery Connection and credential as `model.refresh_db` (keyless and public catalogs send no auth header), sends discovery's primary catalog request from `build_discovery_request` (effective Connection endpoint, Adapter discovery params and headers) over a deliberately raw client, stores its own trace, and never mutates the catalog. A Connection without an effective `models_endpoint` is a domain error. Trace-list freshness rides the event bus via `resource_changed(kind: "debug_traces")` on terminal Runs, probes, and clears - DebugView has no manual refresh.

## Conventions & Gotchas

- Capture is best-effort and must never affect results: redaction/persistence/transport failures log warn and swallow so the Provider call still returns.
- The trace shape above is a hard contract - a previous drift (nested write vs flat read) produced empty detail panes; both sides use exactly these field names.
- HTTP capture lives only in the client factory/transport - never reintroduce per-adapter capture blocks; non-HTTP transports capture at their wire-exchange boundary under the same contract.
- Streaming capture tees bytes without buffering the whole stream first (latency/back-pressure); aggregation happens from already-tee-captured bytes, never inside adapter `stream()` implementations.
- Traces are not size-truncated - `trace_limit` caps count, not bytes. Trace data never crosses chat/SSE/WebSocket boundaries; only `debug.*` RPCs reach it. Debug context stays out of `**kwargs` and provider-bound bodies. WebUI detail is request/response-first and must not present stream events as the response; UI strings go through i18n.
- Multi-endpoint providers (e.g. GitHub Copilot) trace for free through factory-built clients; a provider constructing a raw client silently escapes tracing - the sanctioned exception is `model_probe` writing its own trace with the same redaction helpers.

## WebUI inspection

`webui/src/components/DebugView.svelte` owns selection, stale-request protection and event-driven list refresh. `lib/debugView.js` retains the fetched detail while its id remains in the index; clearing or pruning invalidates pending detail loads. Missing index statuses/durations stay null, not zero. Index status filters describe HTTP/WS codes only: a successful status does not prove that a captured stream completed; full-trace errors remain visible in the detail header.

`components/debug/` owns the searchable Provider/status-filtered list, Request/Response/Metadata inspector and lazy JSON reading view. Readable and formatted JSON are presentation-only alternatives; Raw, body Copy, and whole-trace JSON export retain the captured body strings. SSE/WebSocket aggregates remain complete text, never a replacement per-event view. Literal body search switches from the reading view to Raw and highlights one navigable match without dropping surrounding text. Retention uses shared autosave; capture/storage and the Model Probe use secondary disclosures. Visual arrangement: `webui/design.md` -> Debug. Coverage: `components/__tests__/DebugView.test.js`, `components/debug/__tests__/DebugBody.test.js`, `lib/__tests__/debugView.test.js`, and App debug-invalidation tests.
