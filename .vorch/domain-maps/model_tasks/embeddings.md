# Embeddings

Provider-neutral text embedding execution for the configured `text_embedding` task-model binding. Returns normalized float vectors consumed by the recall `vector` backend.

## Overview

`core/model_tasks/` (`embeddings*.py`) owns text-to-vector embedding after Settings has selected one concrete task-model target. It resolves the `text_embedding` binding through `TaskModelService`, calls the provider embedding API, and returns normalized float vectors preserving input order. `TaskModelService.update()` owns server-side embedding-option validation; this domain does not own model discovery, model catalogs, UI controls, vector storage, or recall search.

## Terms

Domain-specific vocabulary for embedding execution. The user-facing Semantic Recall term lives in `recall.md`.

### Embedding Model
**Definition:** A specialized model that converts text into numerical vectors (embeddings) for semantic comparison. In vBot, this is a configurable `text_embedding` task-model binding used by the recall `vector` backend to find meaning-related past sessions (e.g. "car" and "vehicle" are nearby in embedding space).
**Not:** A chat model, a TTS model, or an image generation model. The embedding model produces vectors, not text, speech, or images.

## Interfaces

- `EmbeddingService(runtime)` - runtime-owned service; resolves the `text_embedding` binding and calls the provider embedding client.
- `await EmbeddingService.embed(texts: list[str], *, purpose: Literal["query", "document"] | None = None) -> EmbeddingResult` - validates inputs, resolves the configured binding, merges options over backend defaults, shapes the request with the Model family's embedding profile, calls the provider client, and returns normalized vectors in input order. Recall always supplies a purpose; `None` sends the texts unchanged without `input_type` for other callers.
- `EmbeddingService.resolve_space() -> EmbeddingSpaceIdentity` - returns provider/model plus a stable SHA-256 fingerprint over the normalized target (including Connection and Account), effective options, the embedding profile's request-shaping facts with its contract version, and the embedding wire-contract version without executing a request.
- `embedding_profile(model_id) -> EmbeddingProfile` (`embedding_profiles.py`) - the curated profile for a Model family, or the symmetric unknown profile; see Embedding Profiles.
- `EmbeddingResult` - exposes `vectors: tuple[list[float], ...]`, `dimension`, `provider_id`, configured `model_id`, provider-reported `response_model_id`, `space_fingerprint`, and normalized `usage`. `actual_model_id` falls back to the configured Model when the Provider omits its Model id.
- `await ProviderEmbeddingClient.embed(texts: list[str], *, options: dict, input_type: str | None = None) -> ProviderEmbeddingResponse` - provider-bound HTTP entrypoint; posts the already shaped texts to the provider's embeddings endpoint and normalizes ordered vectors, actual model identity, token Usage, and optional cost.
- `EmbeddingError` base class in `core/utils/errors.py` (derives from the shared `TaskError` base for task-model execution errors); subclasses: `EmbeddingConfigurationError` (no binding), `EmbeddingUnsupportedTargetError` (local/rejected target), `EmbeddingExecutionError` (provider failure).

Recall pins `EmbeddingSpaceIdentity.fingerprint` together with provider, model, observed dimension, and index policy. A target, Connection, Account, option, profile handling, dimension, policy, or schema change therefore invalidates the derived vectors instead of reusing an incompatible space.

## Embedding Profiles

`core/model_tasks/embedding_profiles.py` owns one curated table of Model families, matched case-insensitively on the Model id independent of the Provider (`qwen/qwen3-embedding-8b`, Ollama `qwen3-embedding:0.6b`, and LM Studio `text-embedding-qwen3-embedding-0.6b` are one family). Order matters: the first matching family wins, so a narrower family (`nomic-embed-text-v2`, `multilingual-e5`) precedes its broader sibling. An unknown Model is symmetric with no recommendation. Each `EmbeddingProfile` carries:

- purpose handling: `symmetric`, `input_type` (query/document values for the Provider's `input_type` field), or `prefix` (query/document text prefixes; an empty side stays unchanged);
- selection facts: `multilingual` (`None` = unknown), `recommended_rank` (1 first, `None` = not recommended), `max_input_tokens` (the Model's own limit per text, when known), and a short English `note`.

`EmbeddingProfile.request(texts, purpose)` shapes one request. The table is Provider-independent by design: a local embedding engine must apply the same profile so a family behaves identically wherever it runs (none exists yet). Prefixes exist only on the wire: callers, including Recall, keep and store their original text, and the profile's handling plus `EMBEDDING_PROFILE_CONTRACT_VERSION` enter the space fingerprint. Bump that version when a table change alters what an existing family sends; rank and note changes do not change a space. A prefix adds a few tokens to every query or document, which count against `max_input_tokens`.

Model-card evidence lives in comments beside non-obvious entries (for example, Qwen3's official query prompt ends in `Query:` without a trailing space, while Harrier's ends in `Query: ` with one). `input_type` is used only for families whose server-side handling was verified: OpenRouter documents the field only as "The type of input (e.g. search_query, search_document)". A live probe on 2026-10-01 compared query and document vectors for the same text: `voyage-4`/`voyage-4-lite`, `gemini-embedding-001`, `gemini-embedding-2`, and `nemotron-3-embed-1b` changed (cosine 0.69-0.91; Gemini and Nemotron treat an omitted field like a query), while `qwen3-embedding-8b`/`-4b`, `text-embedding-3-small`, `pplx-embed`, `bge-m3`, `mistral-embed-2312`, and `multilingual-e5-large` returned identical vectors. Families that ignore the field rely on their documented prefixes instead, or are symmetric.

### Target facts

`task_model.list_targets` descriptors (`TaskModelTarget.facts`) for `text_embedding` carry the selection facts a settings UI shows per target, for Provider and local targets alike: `local` (the Model runs on this machine: every local target, and Provider Models whose metadata flags locality, such as Ollama and LM Studio installs; proxied Ollama Cloud Models are not local), `multilingual`, `recommended_rank`, and `note` from the profile, `max_input_tokens` (profile limit, else the Model's catalog `context_window`, else `null`), and `input_price_per_million` (USD from the Model DB's base input rate, `null` when unknown; OpenRouter embedding Models get it from OpenRouter's own catalog, other Provider Models from models.dev when it lists them). Coverage: `tests/core/model_tasks/test_model_task_targets.py`.

## Provider Wire Behavior

`ProviderEmbeddingClient` subclasses `core.providers.task_client.ProviderTaskClient`, the shared plumbing it has in common with `core/model_tasks/image_providers.py` and `core/model_tasks/speech_providers.py` (constructor tuple, `from_runtime` factory, auth headers, POST/classify/parse cycle, retry policy - see `providers.md`). This module owns only the embeddings payload shape and response parsing:

- POSTs `/embeddings` below the Adapter's OpenAI-compatible base (`providers/request-policy.md` -> Provider-backed task clients: OpenRouter `https://openrouter.ai/api/v1`, OpenAI and Mistral `.../v1`, Ollama and LM Studio `<native base>/v1`) with authored `model`, `input` (array of strings), `encoding_format="float"`, and optional positive-integer `dimensions`. `input_type` is sent only when the Model family's profile supplies one for the purpose. `extra_options` may add non-empty Provider-specific fields but cannot override `model`, `input`, `encoding_format`, `dimensions`, or `input_type`.
- Normalizes response `data[]` entries to ordered vectors. A complete integer `index` set must map every input exactly once; when every entry omits `index`, wire order is preserved. Explicit null indices are invalid, not omitted. Mixed, duplicate, missing, or out-of-range mappings are rejected.
- Every vector must be non-empty, finite, numeric, and the same dimension; malformed shapes and values outside floating-point range are rejected as Provider errors before they reach Recall. Unrepresentable optional cost telemetry is instead discarded without invalidating usable vectors.
- A present response `model` must be a non-empty string and becomes the actual model identity; omission falls back to the configured model. `usage.prompt_tokens`/`input_tokens`, `usage.total_tokens`, and optional non-negative finite `usage.cost` normalize into `EmbeddingUsage`. Report counters distinguish a real zero from missing or malformed telemetry, which never invalidates otherwise valid vectors.
- Each Provider POST attempt records durable `text_embedding` Usage, including rejected responses and retry attempts. `EmbeddingUsage.input_token_reports` separately preserves whether input was reported: a total-only report never invents zero input. Reported embeddings have zero completion tokens; absent token/cost reports remain unknown. Recall's aggregate batch Usage is diagnostic and is not recorded a second time. `test_embeddings_providers.py` parses total-only, zero, malformed and missing reports through `embed()`; `test_task_usage.py` covers their Usage projection (including cost-only) and normalized result accounting.
- The embedding **dimension** is observed from `len(data[0].embedding)` in the API response - it is never trusted from the model catalog (catalogs lack dimension data). The dimension is returned in `EmbeddingResult.dimension` for the recall store to pin.

## Constraints & Gotchas

- Provider targets must use the task-model id shape `provider/model-id::connection-id`. Local targets are rejected with `EmbeddingUnsupportedTargetError`.
- Provider targets are the Models that discovery tags `text_embedding` (OpenRouter, OpenAI Platform, Mistral, Ollama, LM Studio; per-Provider facts in `providers/catalog-discovery.md`). LM Studio embedding requests do not run the chat path's explicit native load, so they rely on LM Studio loading the Model on demand (`providers/lmstudio.md`).
- There is no local embedding engine shim; local target descriptors parse successfully in `core/model_tasks/` but embedding execution rejects them.
- The `dimensions` option is omitted only when absent or `None`; any configured value must be a positive non-boolean integer. `TaskModelService.update()` rejects invalid values, and the wire builder repeats the check defensively. Only Matryoshka-compatible models respect the option; other models may reject it with a 4xx error surfaced as `EmbeddingExecutionError`.
- OpenRouter reports routing/credit/availability failures (e.g. "No endpoints found for `<model>`") as an `error` object with **HTTP 200** and no `data` array - these never reach the 4xx classifier. `_parse_embeddings_response` surfaces the `error` message (+`code`) in the `ProviderError` so the real reason reaches the log, and marks a payload carrying an `error` object **non-retryable** (retrying returns the same error). A genuinely empty `data: []` with no `error` stays retryable.
- Embeddings are batched: the `input` array can contain multiple strings in one request. The client does not impose its own batch-size limit; the caller (recall backend) is responsible for staying within provider rate/batch limits. The recall `vector` backend splits large text sets into batches of `_EMBED_BATCH_SIZE` (64) to respect provider per-request input-count limits.
- Recall recursively divides a multi-input batch when the Provider reports context overflow, preserving input order and aggregating Usage across successful subrequests. It never mutates a single rejected text in the retry path; Passage/chunk construction must own any truncation so stored text always matches embedded text.
- Embedding task requests treat HTTP 529 as retryable overload (needed by OpenRouter); the shared task client still keeps 529 opt-in through the concrete task client's extra-status policy.
- Debug trace capture is not wired through `ProviderEmbeddingClient`; the shared `ProviderTaskClient.post_and_parse` constructs a plain `httpx.AsyncClient` (deliberate, like `ProviderImageClient`).
- `EmbeddingService` takes `runtime: TaskClientRuntime` (the narrow protocol from `core.providers.task_client`) - the Provider client reads `runtime.providers` and `runtime.get_connection_token_getter()`.
