# Ollama Provider

Local-first provider speaking Ollama's **native** `/api/chat` wire (not the OpenAI-compatible shim). One adapter (`core/providers/ollama.py`, `OllamaAdapter(ProviderAdapter)`) serves two connections: keyless `local` (`http://localhost:11434`, type `none`, `auto_refresh: true`) and API-key `cloud` (`https://ollama.com`, `OLLAMA_API_KEY`). Wire facts below were verified live against Ollama 0.24.0 on 2026-07-07; the direct cloud connection is **docs-only, untested** (no API key yet — see FLAGGED.md).

**The `local` connection is disabled by default** (keyless connections need an explicit opt-in — `providers.md` → Terms → Usable). Until the user enables it (Settings → Providers toggle, `vbot provider enable ollama --connection ollama:local`, or the `connection.set_enabled` RPC), vBot never probes localhost, lists no Ollama models, and refuses adapter creation with a clear "connection is disabled" error. Enabling an auto-refresh connection forces an immediate catalog probe so the caller gets live reachability feedback; "enabled but not running" is a valid state — the enable sticks and the models appear on the next successful probe.

## Wire protocol

- Non-streaming: `POST /api/chat` with `"stream": false` → one JSON object: `message` (`content`, optional `thinking`, optional `tool_calls`), `done_reason`, usage counters `prompt_eval_count` (input) / `eval_count` (output).
- Streaming: `"stream": true` → **NDJSON lines** (one JSON object per line, no SSE framing); the final line has `"done": true` plus the usage counters. The adapter maps chunks to the normalized deltas; a stream ending without a done chunk is a `NetworkError`; an in-band `{"error": …}` line is a fatal `ProviderError`.
- **Tool-call `function.arguments` is a JSON object, not a string** (unlike OpenAI). vBot's canonical arguments are also a dict, so both directions map without JSON string encoding. Streamed tool calls arrive whole in one chunk — the adapter emits one `tool_call_delta` with the full name and `json.dumps(arguments)` as the arguments delta. A response tool call without an `id` gets a positional `tool_call_{index}` fallback.
- Tool results replay as `{"role": "tool", "content", "tool_call_id"}`; assistant reasoning replays via the `thinking` field.
- Sampling/runtime parameters ride under `options`: `temperature`, `max_tokens` → `num_predict`, `top_p` — and the enforced `num_ctx` (see below).
- Images: per-message `images` array of bare base64 strings (`wire_media_support` → images only). Non-image media blocks raise; documents degrade upstream via the wire-media intersection.
- Errors come as `{"error": "..."}`; the adapter folds the message into the classified error detail.

## Reasoning

Binary `think: true|false` toggle; `/api/show` `capabilities` containing `"thinking"` marks support (projected to `reasoning: {supported: true, control: "on_off"}`). The shared intent resolver applies; render policy (v1): the toggle is sent **only when the catalog positively marks the model thinking-capable** (Ollama rejects `think` on models that cannot reason — unknown support means the field stays absent). `off` → `think: false`; `effort`/`budget`/`on` → `think: true` (no level strings yet).

## Catalog discovery

- `GET /api/tags` lists installed models — conservative baseline (text-only, no tools, no window; `family` from `details.family`). **Locality is stamped here:** an entry with `remote_host` is a proxied cloud model (`metadata.ollama.remote: true`; the `:cloud` name suffix is convention, `remote_host` is the fact), otherwise `metadata.ollama.local: true`.
- `enrich_discovered_models` (the discovery POST hook) calls `POST /api/show` per model (bounded concurrency 8): `capabilities` list → tools/vision/thinking; context window from `model_info["<architecture>.context_length"]` where `<architecture>` = `model_info["general.architecture"]` — **only that exact key**; a suffix scan would wrongly match `*.rope.scaling.original_context_length`. Absent architecture/key → window stays honestly unknown.
- The `local` connection auto-refreshes **only while enabled** (startup + `model.list`, 30s throttle — `models.md` → "Local catalogs auto-refresh"); Ollama down keeps the last known catalog. Each probe outcome is recorded (`Runtime.connection_reachability`) and surfaces as a "not reachable" marker on the connection payloads and as `reachable: false` on the affected `model.list` entries — the models stay listed and selectable.
- A refused/failed TCP connect is wrapped into a `NetworkError` that names the base URL and asks "is the Ollama service running?" (still retryable, unchanged classification) — so a chat against a stopped Ollama fails with the real cause instead of a bare socket error.

## Effective context window / num_ctx enforcement

Ollama reports the model's *theoretical* max (262144 for an 8B model) and **silently truncates** the prompt when the loaded context is smaller. vBot therefore budgets flagged-local models against the effective window (user knob or `min(32768, max)` — `resolve_effective_context_window`) **and enforces exactly that window on the wire**: the runtime injects `local_context_resolver` (keyword-only constructor arg, DI like `model_lookup`) into the adapter, which sets `options.num_ctx` on every send/stream for flagged-local models (`None` → field omitted — proxied `:cloud` and unknown models get no `num_ctx`). Settings are read live per call; verified via `GET /api/ps` → `context_length` of the loaded model. Assumption == reality.

## Constraints & Gotchas

- The cloud connection assumes ollama.com speaks the same native API with `Authorization: Bearer` — **unverified**; refresh failures are non-fatal per connection, so a wrong assumption degrades gracefully. Revisit when a key exists (FLAGGED.md).
- Locality stamping keys off `remote_host` absence. Models discovered on the **direct cloud connection** would also lack `remote_host` and would be mis-stamped local (cap + knob). Acceptable while cloud is untested; fix when verifying cloud.
- `/api/ps` (loaded models + actual context) and `/api/version` are live-testing aids only — vBot does not consume them.
- Changing `num_ctx` between requests makes Ollama reload the model with the new window (slow first call after a knob change; expected).
