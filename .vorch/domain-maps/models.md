# Models

`core/models/` owns provider-specific model facts, the registry read path, and the sanitized catalog format used by runtime and accessors. A model is always a model at one provider; vBot has no canonical cross-provider model entity and never remaps model IDs.

## Overview

The model registry loads sanitized JSON catalogs from `resources/models/` and indexes them by `(provider_id, model_id)`. Dynamic discovery fetches provider model catalogs, lets the selected provider adapter normalize raw entries into vBot `Model` objects, applies optional human overrides, writes the generated catalog, and invalidates the registry cache. Runtime and server code read model data through `ModelRegistry`; CLI and WebUI access model data through server RPC. Provider APIs and raw discovery files stay outside the normal runtime read path.

## Interfaces

- Data classes live in `core/models/models.py`: `Model`, `Capabilities`, and `ReasoningCapabilities` are frozen. Keep the map at the contract level; the exact field list belongs in the dataclasses.
- `Model.model_id` is the exact string sent to the provider API. `context_window`, `max_output_tokens`, and `capabilities` are provider-specific facts, not canonical claims about an underlying model family.
- `context_window` and `max_output_tokens` are both `int | None`. `None`/absent means the fact is honestly unknown (a thin/window-less endpoint, a custom model), not zero — a missing fact stays missing in the data, never faked with a constant. Read-side callers resolve a usable window through the shared default chain `resolve_context_window(model.context_window, provider_config)` (model value → provider-config `context_window` default → the named global floor `GLOBAL_CONTEXT_WINDOW_FLOOR`), so nothing downstream crashes or divides by zero. The chain is the single source of truth — `core/providers/providers.py` — and is *not* in an adapter (read-side facts resolve at the provider-config level; only request-shaping defaults live in adapters).
- `Model.connections: tuple[str, ...]` binds a model to a subset of its provider's connection ids. Empty tuple means the model is valid on every connection of its provider; a non-empty tuple restricts the model to the listed connection ids. Connection-restricted models do not cross-product against other usable connections during target expansion (see `model_tasks.md`). Refresh tags every discovered model with `connections: [<credential_connection.id>]` so each connection's models stay isolated to that connection in the catalog.
- `Model.metadata` is optional sanitized runtime data for provider adapters. It must stay small, immutable after load, and limited to provider runtime facts the adapter policy needs. Do not store raw provider payloads, provider policy text, credentials, or secrets there.
- `ModelRegistry.load(resources_dir)` reads `resources/models/*.json`, skips `*.raw.json` and `*.overrides.json`, and caches by resolved `resources_dir`.
- `ModelRegistry.get(provider_id, model_id)` raises `KeyError` when the exact provider/model pair is missing. `list_for_provider(provider_id)` returns models sorted by `model_id` and returns an empty list for unknown providers.
- `ModelRegistry.query(model_query: ModelQuery) -> list[tuple[str, Model]]` is the filtered read path. It evaluates capability, task, modality, and context-window filters against every model in the registry, returning matching `(provider_id, model)` tuples sorted by `(provider_id, model_id)`. The query is pure — no credential awareness — and lives in `core/models/query.py`. Callers that need credential gating (e.g. RPC `model.list`, `core/model_tasks/` target discovery) apply it outside the query.
- `ModelRegistry.invalidate(resources_dir)` clears the cached registry for that resource path after refresh.
- `Runtime.models` and `Runtime.get_model(provider_id, model_id)` are available only after `Runtime.start()`; before startup they raise `RuntimeError`. `Runtime.get_model()` delegates to the registry.

## Model IDs

The model ID stored in a catalog is the ID sent on the wire. There is no lookup table, alias layer, canonical ID, or provider-specific rewrite between registry lookup and adapter request.

- OpenRouter example: `anthropic/claude-sonnet-4` is sent as `"model": "anthropic/claude-sonnet-4"`.
- Anthropic example: `claude-sonnet-4-20250219` is sent as `"model": "claude-sonnet-4-20250219"`.
- OpenAI example: `gpt-5.2` is sent as `"model": "gpt-5.2"`.
- User-facing selectors use `<provider>/<model-id-at-provider>`, such as `openrouter/anthropic/claude-sonnet-4`. The provider prefix selects the provider config; the remainder is the exact registry key and provider model ID.

## Catalog Files

The model system uses up to three sibling files under `resources/models/`; only the generated catalog/raw files are written by refresh:

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `<provider>.json` | `refresh_models()` or bundled static data | `ModelRegistry.load()` | Sanitized app catalog |
| `<provider>.raw.json` | `refresh_models()` | Nobody at runtime | Inspection/debugging copy of parsed provider response data |
| `<provider>.overrides.json` | Human research | `refresh_models()` only | Narrow corrections for externally verified facts provider APIs do not expose |

`<provider>.raw.json` preserves the parsed provider response data before `raw_filter`, adapter normalization, `model_filter`, or overrides are applied. Use raw files to debug missing catalog entries: if a model is present in raw output but absent from `<provider>.json`, the fix belongs in filtering, adapter normalization, or override validation, not in the registry read path.

Critical: `resources/models/<provider>.json` is a generated catalog for refresh-backed providers. Every successful model refresh rewrites the whole `resources/models/<provider>.json` file from the current normalized discovery result plus optional overrides. Do not hand-edit that file for lasting fixes; the next refresh can delete, replace, reorder, or recompute any entry in it.

`resources/models/<provider>.overrides.json` is never created by refresh. It is a manually maintained input file: if it exists, refresh reads it and applies those overrides while generating `<provider>.json`; if it does not exist, refresh simply uses normalized discovery output.

Lasting model-catalog fixes belong in adapter catalog normalization, adapter runtime policy, or a manual `resources/models/<provider>.overrides.json` only when the fact is durable, externally verified, and not discoverable from provider catalog APIs or probe requests. The app, runtime, and UI use only the sanitized `<provider>.json` files; raw files are for humans, and override files are applied during refresh but skipped by the registry.

`ModelRegistry.load()` reads only top-level `provider_id` and `models` from sanitized catalogs and ignores top-level metadata such as `source` or `fetched_at`. Generated catalogs may include optional per-model `metadata`; the registry freezes nested metadata mappings/lists so loaded model data remains immutable. `max_output_tokens: null` means discovery did not expose a trustworthy per-model output limit; it is not the same thing as a runtime request default. `context_window: null`/absent is the same kind of honest gap — discovery did not expose a window — and is read-side-resolved through the default chain (see Interfaces). Adapter normalizers emit `null` (never a placeholder `0`) when an endpoint reports no window; discovery validation reads both limits with `_read_optional_int`.

## Capabilities & Tasks

Capabilities are facts about one model through one provider. The same underlying model family can have different tools, reasoning, modalities, context window, and output limits depending on provider.

`reasoning.supported` is a boolean only. It says whether the provider advertises some reasoning/thinking capability for that model; effort levels, budgets, endpoint choice, and request payload shape remain adapter responsibilities. A catalog entry saying reasoning is supported does not by itself authorize sending a specific control field such as OpenAI-style `reasoning_effort`.

`input_modalities`, `output_modalities`, `supported_parameters`, `supported_voices`, and `task_types` preserve sanitized provider-catalog facts when available. `supported_voices` is a tuple of plain voice-id strings (e.g. `["af_alloy", "af_aoede", ...]`), read defensively from provider catalog responses. It is provider-specific — different providers expose different voice lists for the same underlying TTS model family. `task_types` is a coarse filtering and routing projection used by accessors and task-model discovery; it is not provider request shaping. The authoritative task ordering and derivation logic live in `core/models/models.py`, and task-model bindings must stay aligned with `.vorch/domain-maps/model_tasks.md`.

Known `task_types` currently follow `MODEL_TASK_ORDER`: `chat`, `text_output`, `image_input`, `image_understanding`, `file_input`, `file_understanding`, `audio_input`, `speech_to_text`, `video_input`, `video_understanding`, `image_generation`, `audio_generation`, `text_to_speech`, `text_embedding`, and `video_generation`.

Sparse catalogs remain usable. Missing modality data defaults to text-in/text-out, and local or OpenAI-compatible providers with conservative optional facts should not disappear from model selection merely because fields such as `tools` or large `context_window` are missing.

Speech/audio modality aliases are intentionally strict: `transcription` output counts as text output and enables STT filtering; `speech` output enables TTS and audio-generation filtering; generic `audio` output enables `audio_generation` only and does not imply `text_to_speech`.

### ModelQuery — the shared capability/task filter

`core/models/query.py` owns the reusable `ModelQuery` dataclass and its `from_filters` builder. It is the single place where model capability, task type, modality, and context-window matching happens. Every caller that needs to filter models by these criteria routes through `ModelRegistry.query()` — including the RPC `model.list` handler and `core/model_tasks/` provider target discovery. The query is pure: it takes no credentials or runtime state, only filter criteria.

`ModelQuery.from_filters(raw_params)` normalizes raw filter values (lowercase, trim, dedupe, expand alias field names such as `task`/`task_type`) into a frozen query object. Callers that need credential gating, connection expansion, or response shaping apply those outside the query. This layering keeps the core reusable without coupling it to server or credentials concerns.

## Discovery & Refresh

`core/models/discovery.py` owns fetch, normalization, override application, generated file writes, and registry invalidation. The registry remains the read path and does not call provider APIs.

`refresh_models()` writes files and invalidates the class-level registry cache, but it does not mutate an already-held `ModelRegistry` instance. The server `model.refresh_db` RPC reloads `runtime._models = ModelRegistry.load(resources_dir)` after refresh; any new live-refresh entry point must also replace/reload the runtime registry or require a runtime restart.

Discovery dispatches by `ProviderConfig.adapter` to `adapter_class.normalize_catalog_entry(raw, defaults)`. Provider-specific catalog schemas belong in provider adapters and provider maps, not in provider-ID branches inside discovery. Examples: OpenRouter-specific catalog behavior lives in `OpenRouterAdapter` and `.vorch/domain-maps/providers/openrouter.md`; GitHub Copilot catalog metadata and runtime policy live in `GitHubCopilotAdapter` and `.vorch/domain-maps/providers/github-copilot.md`.

Discovery builds auth headers from the selected connection plus provider `extra_headers`, then calls optional adapter hooks such as `discovery_headers(provider_config, credential_value, headers)`, `discovery_params()`, and `supplementary_discovery_params()`. Use these hooks for catalog-only requirements such as OpenAI Subscription account headers/client version parameters or OpenRouter supplementary speech/image-generation fetches.

Supplementary discovery fetches append adapter-provided query parameters to the main models endpoint, merge new models by ID, and log warnings on supplementary failure without blocking the main catalog save. This keeps task-specific models discoverable when a provider's default `/models` response omits them.

Override fields replace fetched top-level model fields; nested objects are replaced wholesale rather than deep-merged. Override-only models are exceptional and must provide the full current `Model` shape because the app does not support legacy catalog schemas.

## Constraints & Gotchas

- Model facts are provider-specific. Do not treat one provider's context window, output limit, modality list, or reasoning behavior as canonical for another provider.
- No model ID remapping. The model ID in `resources/models/<provider>.json` is exactly what goes on the wire.
- Generated model catalogs are refreshable artifacts, not durable fix locations. A refresh recreates `resources/models/<provider>.json` from discovery output plus overrides, so hand edits to generated catalogs are temporary at best.
- Overrides are for research-only gaps. If a fact can be obtained from provider catalogs or by probing a provider endpoint, implement it in adapter normalization or runtime behavior instead.
- Capability facts are not always runtime-control permissions. Adapters decide which optional request parameters are safe for the selected provider/model/endpoint.
- GitHub Copilot is dynamic-first: when `metadata.github_copilot` exists, endpoint selection and optional request-feature gating use those catalog facts; static policy entries are fallback or exact-model override rules only.
- Model objects are immutable after load. Change catalog generation or input files, then invalidate/reload the registry instead of mutating loaded `Model` instances.
