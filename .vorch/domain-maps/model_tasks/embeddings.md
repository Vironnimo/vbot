# Embeddings

Provider-neutral text embedding execution for the configured `text_embedding` task-model binding. Returns normalized float vectors consumed by the recall `vector` backend.

## Overview

`core/model_tasks/` (`embeddings*.py`) owns text-to-vector embedding after Settings has selected one concrete task-model target. It resolves the `text_embedding` binding through `TaskModelService`, calls the provider embedding API, and returns normalized float vectors preserving input order. `TaskModelService.update()` owns server-side embedding-option validation; this domain does not own model discovery, model catalogs, UI controls, vector storage, or recall search.

## Terms

Domain-specific vocabulary for embedding execution. The user-facing Semantic Recall term lives in `.vorch/GLOSSARY.md`.

### Embedding Model
**Definition:** A specialized model that converts text into numerical vectors (embeddings) for semantic comparison. In vBot, this is a configurable `text_embedding` task-model binding used by the recall `vector` backend to find meaning-related past sessions (e.g. "car" and "vehicle" are nearby in embedding space).
**Not:** A chat model, a TTS model, or an image generation model. The embedding model produces vectors, not text, speech, or images.

## Interfaces

- `EmbeddingService(runtime)` — runtime-owned service; resolves the `text_embedding` binding and calls the provider embedding client.
- `await EmbeddingService.embed(texts: list[str], *, purpose: Literal["query", "document"] | None = None) -> EmbeddingResult` — validates inputs, resolves the configured binding, merges options over backend defaults, calls the provider client, and returns normalized vectors in input order. Recall always supplies a purpose; `None` preserves symmetric behavior for other callers.
- `EmbeddingService.resolve_space() -> EmbeddingSpaceIdentity` — returns provider/model plus a stable SHA-256 fingerprint over the normalized target (including Connection and Account), effective options, and embedding wire-contract version without executing a request.
- `EmbeddingResult` — exposes `vectors: tuple[list[float], ...]`, `dimension`, `provider_id`, configured `model_id`, provider-reported `response_model_id`, `space_fingerprint`, normalized `usage`, and the compatibility `resolved_model_id` tuple projected from the actual model.
- `await ProviderEmbeddingClient.embed(texts: list[str], *, options: dict, purpose: str | None = None) -> ProviderEmbeddingResponse` — provider-bound HTTP entrypoint; posts to the provider's embeddings endpoint and normalizes ordered vectors, actual model identity, token Usage, and optional cost.
- `EmbeddingError` base class in `core/utils/errors.py` (derives from the shared `TaskError` base for task-model execution errors); subclasses: `EmbeddingConfigurationError` (no binding), `EmbeddingUnsupportedTargetError` (local/rejected target), `EmbeddingExecutionError` (provider failure).

Recall pins `EmbeddingSpaceIdentity.fingerprint` together with provider, model, observed dimension, and index policy. A target, Connection, Account, option, dimension, policy, or schema change therefore invalidates the derived vectors instead of reusing an incompatible space.

## Provider Wire Behavior

`ProviderEmbeddingClient` subclasses `core.providers.task_client.ProviderTaskClient`, the shared plumbing it has in common with `core/model_tasks/image_providers.py` and `core/model_tasks/speech_providers.py` (constructor tuple, `from_runtime` factory, auth headers, POST/classify/parse cycle, retry policy — see `providers.md`). This module owns only the embeddings payload shape and response parsing:

- POSTs `/api/v1/embeddings` with authored `model`, `input` (array of strings), `encoding_format="float"`, and optional positive-integer `dimensions`. For the verified OpenRouter target only, Recall purpose maps to provider-owned `input_type="search_query"` or `input_type="search_document"`; other providers omit this field and remain symmetric. `extra_options` may add non-empty Provider-specific fields but cannot override `model`, `input`, `encoding_format`, `dimensions`, or `input_type`.
- Normalizes response `data[]` entries to ordered vectors. A complete integer `index` set must map every input exactly once; when every entry omits `index`, wire order is preserved. Mixed, duplicate, missing, or out-of-range mappings are rejected.
- Every vector must be non-empty, finite, numeric, and the same dimension; malformed shapes are rejected before they reach Recall.
- A present response `model` must be a non-empty string and becomes the actual model identity; omission falls back to the configured model. `usage.prompt_tokens`/`input_tokens`, `usage.total_tokens`, and optional non-negative finite `usage.cost` normalize into `EmbeddingUsage`. Report counters distinguish a real zero from missing or malformed telemetry, which never invalidates otherwise valid vectors.
- The embedding **dimension** is observed from `len(data[0].embedding)` in the API response — it is never trusted from the model catalog (catalogs lack dimension data). The dimension is returned in `EmbeddingResult.dimension` for the recall store to pin.

## Constraints & Gotchas

- Provider targets must use the task-model id shape `provider/model-id::connection-id`. Local targets are rejected with `EmbeddingUnsupportedTargetError`.
- There is no local embedding engine shim; local target descriptors parse successfully in `core/model_tasks/` but embedding execution rejects them.
- The `dimensions` option is omitted only when absent or `None`; any configured value must be a positive non-boolean integer. `TaskModelService.update()` rejects invalid values, and the wire builder repeats the check defensively. Only Matryoshka-compatible models respect the option; other models may reject it with a 4xx error surfaced as `EmbeddingExecutionError`.
- OpenRouter reports routing/credit/availability failures (e.g. "No endpoints found for `<model>`") as an `error` object with **HTTP 200** and no `data` array — these never reach the 4xx classifier. `_parse_embeddings_response` surfaces the `error` message (+`code`) in the `ProviderError` so the real reason reaches the log, and marks a payload carrying an `error` object **non-retryable** (retrying returns the same error). A genuinely empty `data: []` with no `error` stays retryable.
- Embeddings are batched: the `input` array can contain multiple strings in one request. The client does not impose its own batch-size limit; the caller (recall backend) is responsible for staying within provider rate/batch limits. The recall `vector` backend splits large text sets into batches of `_EMBED_BATCH_SIZE` (64) to respect provider per-request input-count limits.
- Recall recursively divides a multi-input batch when the Provider reports context overflow, preserving input order and aggregating Usage across successful subrequests. It never mutates a single rejected text in the retry path; Passage/chunk construction must own any truncation so stored text always matches embedded text.
- Embedding task requests treat HTTP 529 as retryable overload (needed by OpenRouter); the shared task client still keeps 529 opt-in through the concrete task client's extra-status policy.
- Debug trace capture is not wired through `ProviderEmbeddingClient`; the shared `ProviderTaskClient.post_and_parse` constructs a plain `httpx.AsyncClient` (deliberate, like `ProviderImageClient`).
- `EmbeddingService` takes `runtime: TaskClientRuntime` (the narrow protocol from `core.providers.task_client`) — the Provider client reads `runtime.providers` and `runtime.get_connection_token_getter()`.
