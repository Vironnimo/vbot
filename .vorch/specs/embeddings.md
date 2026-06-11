# Embeddings

Provider-neutral text embedding execution for the configured `text_embedding` task-model binding. Returns normalized float vectors consumed by the recall `vector` backend.

## Overview

`core/embeddings/` owns text-to-vector embedding after Settings has selected one concrete task-model target. It resolves the `text_embedding` binding through `TaskModelService`, batches inputs, calls the provider embedding API, and returns `list[list[float]]` vectors preserving input order. It does not own model discovery, model catalogs, settings validation, UI controls, vector storage, or recall search; those live in `core/models/`, `core/model_tasks/`, `core/settings/`, `webui/`, `core/recall/`, and `core/tools/`.

## Interfaces

- `EmbeddingService(runtime)` — runtime-owned service; resolves the `text_embedding` binding and calls the provider embedding client.
- `await EmbeddingService.embed(texts: Sequence[str], *, options: dict | None) -> EmbeddingResult` — validates inputs, resolves the configured binding, merges options over backend defaults, batches inputs through the provider client, and returns normalized vectors in input order.
- `EmbeddingResult` — exposes `vectors: list[list[float]]`, `dimension: int`, `provider_id: str`, `model_id: str`, `usage: dict`, and a `resolved_model_id: str` property for the recall store to pin identity (format: `provider_id/model_id`).
- `await ProviderEmbeddingClient.embed(texts: list[str], *, options: dict | None) -> EmbeddingResult` — provider-bound HTTP entrypoint; posts to the provider's embeddings endpoint and normalizes the response to ordered vectors.
- `EmbeddingError` base class in `core/utils/errors.py` (derives from the shared `TaskError` base for task-model execution errors); subclasses: `EmbeddingConfigurationError` (no binding), `EmbeddingUnsupportedTargetError` (local/rejected target), `EmbeddingExecutionError` (provider failure).

The `EmbeddingResult.resolved_model_id` is the single identity key the recall store pins to detect model switches: when the bound embedding model changes, `resolved_model_id` changes, and the store drops and rebuilds.

## Provider Wire Behavior

`ProviderEmbeddingClient` subclasses `core.providers.task_client.ProviderTaskClient`, the shared plumbing it has in common with `core/image/providers.py` and `core/speech/providers.py` (constructor tuple, `from_runtime` factory, auth headers, POST/classify/parse cycle, retry policy — see `providers.md`). This module owns only the embeddings payload shape and response parsing:

- POSTs `/api/v1/embeddings` with `model`, `input` (array of strings), `encoding_format="float"`, and optional `dimensions` (only when present in options and non-zero).
- Normalizes response `data[]` entries to ordered vectors by sorting on `index` before extracting `embedding` fields.
- The embedding **dimension** is observed from `len(data[0].embedding)` in the API response — it is never trusted from the model catalog (catalogs lack dimension data). The dimension is returned in `EmbeddingResult.dimension` for the recall store to pin.

## Constraints & Gotchas

- Provider targets must use the task-model id shape `provider/model-id::connection-id`. Local targets are rejected with `EmbeddingUnsupportedTargetError`.
- There is no local embedding engine shim; local target descriptors parse successfully in `core/model_tasks/` but embedding execution rejects them.
- The `dimensions` option is forwarded as an integer (OpenAI/OpenRouter wire contract) and dropped when `None`/zero/empty. Only Matryoshka-compatible models respect it; other models may reject it with a 4xx error surfaced as `EmbeddingExecutionError`.
- OpenRouter reports routing/credit/availability failures (e.g. "No endpoints found for `<model>`") as an `error` object with **HTTP 200** and no `data` array — these never reach the 4xx classifier. `_parse_embeddings_response` surfaces the `error` message (+`code`) in the `ProviderError` so the real reason reaches the log, and marks a payload carrying an `error` object **non-retryable** (retrying returns the same error). A genuinely empty `data: []` with no `error` stays retryable.
- Embeddings are batched: the `input` array can contain multiple strings in one request. The client does not impose its own batch-size limit; the caller (recall backend) is responsible for staying within provider rate/batch limits. The recall `vector` backend splits large text sets into batches of `_EMBED_BATCH_SIZE` (64) to respect provider per-request input-count limits.
- Debug trace capture is not wired through `ProviderEmbeddingClient`; the shared `ProviderTaskClient.post_and_parse` constructs a plain `httpx.AsyncClient` (deliberate, like `ProviderImageClient`).
- `EmbeddingService` takes `runtime: TaskClientRuntime` (the narrow protocol from `core.providers.task_client`) — it only reads `runtime.providers` and `runtime.provider_credentials`.
