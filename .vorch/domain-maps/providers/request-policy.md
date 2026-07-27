# Provider Request Policy

Read this reference only for Adapter request/response/SSE translation, shared HTTP behavior, reasoning, CoT replay, media support, output/context limits, or Provider-backed task HTTP clients.

## Adapter ownership

`ProviderAdapter` defines `send()`, `stream()`, `normalize_response()`, `aclose()`, and the per-request policy hooks. `stream()` yields only normalized delta types for content, reasoning, Tool calls, opaque reasoning metadata, usage, and finish; raw Provider frames stay private.

`OpenAICompatibleAdapter` deeply owns generic Chat Completions mechanics. `AnthropicCompatibleAdapter` deeply owns Messages-compatible requests, content blocks, SSE, usage/cache normalization, and retry. Concrete Provider Adapters extend or compose those mechanics only for verified Provider differences. A Provider-owned router may inspect its injected Model lookup and Connection mode, but Runtime never selects an inner wire per Model.

`normalize_response(response, *, model_id=None)` produces canonical assistant fields. The optional Model id supports data-driven wire selectors; shape inference is compatibility fallback only. Opaque `reasoning_meta` crosses only the Adapter/Chat boundary and is preserved, not interpreted.

## Reasoning and CoT

### Reasoning intent

`resolve_reasoning_intent()` is the single Provider-neutral decision layer. From Model support/control/levels/budget plus Agent effort and output allowance it returns `default`, `off`, `effort`, `budget`, or `on`. It owns snapping, `none`→off, effort-to-budget math, minimum/clamp behavior, and skip rules; each Adapter only renders the result onto its wire.

Adapters must not reimplement this decision policy. Provider-specific maps document each render (for example OpenRouter object fields, Anthropic adaptive/native budget, or binary thinking toggles).

### Chain of Thought

CoT is opaque reasoning output: readable text plus signatures/encrypted/provider metadata required for round-trip continuity. Replay is not a Provider-global capability. The effective policy is scoped to `(Provider Adapter instance, resolved Connection/Wire, Model)`: `reasoning_replay_policy(model_id)` declares `none`, `current_run`, or `full_history`, and an Adapter may combine injected Model facts, Connection mode, exact verified profiles, and conservative fallback. Unknown or unverified Models default to `current_run`; endpoint resemblance alone is not evidence that opaque state is portable.

Keep three facts separate. Replay scope says which canonical Assistant turns vBot may return; wire fidelity says whether it must return exact original blocks/items instead of reconstructing them; Provider render scope says which returned historical reasoning the remote Model actually makes available to the next sample. A `full_history` replay policy does not by itself prove all historical reasoning is rendered.

Chat owns history shaping. `full_history` survives only when the persisted `reasoning_scope` exactly matches the resolved Provider/Model/Connection/Account identity; Model switches, Connection or Account switches, run-local fallback, and textual vBot Compaction are hard boundaries. `phase` is semantic assistant history, not opaque reasoning state, so it survives ordinary request shaping and Compaction. Adapters serialize whatever reasoning survives shaping and must not apply a second history-wide strip except for a documented wire incompatibility such as explicitly disabled thinking.

Responses-shaped adapters persist and replay every original response output item in order; that preserves encrypted reasoning, message `phase`, function/program items, ids, and future item kinds without lossy reconstruction. The ABC default is `current_run`. Within Tool loops, dropping Provider-required signatures or opaque blocks can break continuity even though vBot never reads them.

Reasoning rejection/ignored-effort warnings are diagnostic only. They may warn conservatively on a relevant HTTP 400 or zero reported reasoning tokens, but they do not reclassify status/retry behavior and never log token values.

## Request limits and context

None-valued caller kwargs mean unspecified and are removed before Provider defaults; falsy real values such as `temperature=0.0` remain explicit overrides.

Compatible Adapters resolve a positive output allowance in this order: explicit caller limit, Model catalog `max_output_tokens`, then flat Provider default. Before every request, `resolve_request_output_limit()` clamps that allowance so estimated Provider-visible messages, Tool definitions, a media-aware input reservation, an uncertainty reserve (`max(25% of estimated input, 1% of the effective context window, 256)`), and output fit inside the effective context window. A catalog output ceiling equal to the entire context therefore cannot produce an invalid request; exhausted input fails locally as a non-retryable `ProviderError`. The request estimator in `core/utils/tokens.py` is separate from persisted Usage estimation: native base64/data-URL media receives a fixed per-payload reservation instead of being counted as encoded prose bytes.

`resolve_context_window()` owns the read-side chain: positive Model fact, then positive Provider-config default, then `GLOBAL_CONTEXT_WINDOW_FLOOR`. `resolve_effective_context_window()` adds local-Model policy: positive user setting, else the chain capped by `LOCAL_CONTEXT_DEFAULT_CAP`. These helpers inform budgeting/status/payloads; only verified local Adapters enforce the effective value on the wire.

## Wire media and request context

`wire_media_support(model_id)` declares exact MIME types the concrete wire can carry. Chat intersects this with Model input modalities and degrades unsupported content; the ABC default is empty so a missing declaration degrades rather than crashes. PDF support is declared only on verified concrete wires, not compatible bases.

`request_context_kwargs(agent_id, session_id, project_id=None)` lets an Adapter derive Provider routing/cache hints from stable conversation identity. The default adds nothing. Chat supplies the Run's Project anchor and merges returned kwargs without knowing their Provider meaning. OpenAI Codex uses Agent + Session for its conversation headers; OpenRouter hashes Project + Agent + Session into its top-level sticky-routing `session_id`.

## HTTP, retry, and streaming

- Chat Adapters construct HTTP clients through `_http_shared.build_async_client()` so timeouts and optional debug capture are consistent. Task clients deliberately use their separate shared client path and have no chat debug capture.
- Chat generation uses bounded connect/write/pool timeouts and no read timeout; Chat's normalized-delta stall guard owns open-stream stalls. Remote streams allow 180 seconds to the first normalized delta, then 900 seconds between normalized deltas; incomplete SSE events, raw bytes, and heartbeats do not reset those windows because Adapter normalization has not yielded a chunk. Local/loopback Providers disable both windows.
- HTTP 401/403 are fatal auth errors; 429 and 502/503/504 are retryable; concrete Adapters or task clients can add verified codes through their explicit extra-status policy (Anthropic and embedding task requests opt into 529). Provider POST 500 is fatal. Retryable errors can carry parsed `Retry-After`, used as a capped floor over backoff.
- Every `httpx.TimeoutException` becomes retryable `ProviderTimeoutError`; other transport failures become retryable `NetworkError`. Malformed 2xx JSON and malformed SSE JSON become non-retryable `ProviderError` with the decode error chained.
- Adapter retry covers request/stream establishment. Once streaming has begun, mid-stream failures propagate to Chat, which owns preservation/recovery. Only `ProviderStreamingUnsupportedError` triggers the nonstreaming fallback.
- Authorization headers are rebuilt inside every retry attempt so OAuth refresh remains effective.

## Provider-backed task clients

`ProviderTaskClient` is the shared HTTP base for speech, image, and embedding Provider clients. Its local structural Runtime/target Protocols avoid importing Runtime or `core/model_tasks/`. `from_runtime()` resolves the Connection and refresh-capable token getter; `post_and_parse()` puts request, status classification, and parsing inside `retry_async`, rebuilding headers per attempt. Keyed Connections contribute their configured auth header; a keyless `none` Connection contributes no auth header while still preserving Provider `extra_headers`.

Task-specific payload/response semantics remain in `model_tasks.md` and its task references. `extra_options` is the common JSON escape hatch: empty placeholders are omitted while `0`, `0.0`, and `False` remain meaningful. Most task wires let non-empty entries override authored fields; embeddings explicitly reserve identity/input/encoding/dimension fields and reject attempts to override them.

## Usage normalization

Across Adapters, `input_tokens` means total prompt tokens including cached tokens. Optional `cache_read_tokens` and `cache_write_tokens` preserve Provider counters. Canonical Session aggregation and display semantics live in `chat/usage.md`; Adapters only normalize their wire's numbers.

## Source and tests

- Base contract: `core/providers/adapter.py`
- Shared HTTP/errors: `core/providers/_http_shared.py`, `errors.py`, `core/utils/http_status.py`, `core/utils/retry.py`
- Reasoning: `core/providers/reasoning.py`
- Request output/context policy: `core/providers/providers.py`, `core/utils/tokens.py`
- Compatible and concrete Adapter modules: `core/providers/*.py`
- Task HTTP base: `core/providers/task_client.py`
- Focused coverage: Adapter/request/response/streaming/error/reasoning/task-client suites under `tests/core/providers/`
