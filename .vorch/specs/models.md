# Models

`core/models/` owns provider-specific model facts, the registry read path, and the sanitized catalog format used by runtime and accessors. A model is always a model at one provider; vBot has no canonical cross-provider model entity and never remaps model IDs.

## Overview

The model registry loads sanitized JSON catalogs from `resources/models/` and indexes them by `(provider_id, model_id)`. Dynamic discovery fetches provider model catalogs, lets the selected provider adapter normalize raw entries into vBot `Model` objects, applies optional human overrides, writes the generated catalog, and invalidates the registry cache. Runtime, server, CLI, and WebUI read only the sanitized catalog through the registry; provider APIs and raw discovery files stay outside the read path.

## Interfaces

- Data classes live in `core/models/models.py`: `Model`, `Capabilities`, and `ReasoningCapabilities` are frozen. Keep the spec at the contract level; the exact field list belongs in the dataclasses.
- `Model.model_id` is the exact string sent to the provider API. `context_window`, `max_output_tokens`, and `capabilities` are provider-specific facts, not canonical claims about an underlying model family.
- `Model.metadata` is optional sanitized runtime data for provider adapters. It must stay small, immutable after load, and free of raw provider payloads, policy terms, credentials, and secrets.
- `ModelRegistry.load(resources_dir)` reads `resources/models/*.json`, skips `*.raw.json` and `*.overrides.json`, and caches by resolved `resources_dir`.
- `ModelRegistry.get(provider_id, model_id)` raises `KeyError` when the exact provider/model pair is missing. `list_for_provider(provider_id)` returns models sorted by `model_id` and returns an empty list for unknown providers.
- `ModelRegistry.invalidate(resources_dir)` clears the cached registry for that resource path after refresh.
- `Runtime.models` and `Runtime.get_model(provider_id, model_id)` are available only after `Runtime.start()`; before startup they raise `RuntimeError`. `Runtime.get_model()` delegates to the registry.

## Model IDs

The model ID stored in a catalog is the ID sent on the wire. There is no lookup table, alias layer, canonical ID, or provider-specific rewrite between registry lookup and adapter request.

- OpenRouter example: `anthropic/claude-sonnet-4` is sent as `"model": "anthropic/claude-sonnet-4"`.
- Anthropic example: `claude-sonnet-4-20250219` is sent as `"model": "claude-sonnet-4-20250219"`.
- OpenAI example: `gpt-5.2` is sent as `"model": "gpt-5.2"`.
- User-facing selectors use `<provider>/<model-id-at-provider>`, such as `openrouter/anthropic/claude-sonnet-4`. The provider prefix selects the provider config; the remainder is the exact registry key and provider model ID.

## Catalog Files

Model discovery may write three sibling files under `resources/models/`:

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `<provider>.json` | `refresh_models()` or bundled static data | `ModelRegistry.load()` | Sanitized app catalog |
| `<provider>.raw.json` | `refresh_models()` | Nobody at runtime | Inspection/debugging copy of parsed provider response data |
| `<provider>.overrides.json` | Human research | `refresh_models()` only | Narrow corrections for externally verified facts provider APIs do not expose |

Critical: `resources/models/<provider>.json` is a generated catalog for refresh-backed providers. Every successful model refresh rewrites the whole `resources/models/<provider>.json` file from the current normalized discovery result plus optional overrides. Do not hand-edit that file for lasting fixes; the next refresh can delete, replace, reorder, or recompute any entry in it.

Lasting model-catalog fixes belong in adapter catalog normalization, adapter runtime policy, or `resources/models/<provider>.overrides.json` only when the fact is durable, externally verified, and not discoverable from provider catalog APIs or probe requests. The app, runtime, and UI use only the sanitized `<provider>.json` files; raw files are for humans, and override files are applied during refresh but skipped by the registry.

`ModelRegistry.load()` reads only `provider_id` and `models` from sanitized catalogs and ignores top-level metadata such as `source` or `fetched_at`. Generated catalogs may include optional per-model `metadata`; the registry freezes nested mappings/lists so loaded model data remains immutable. `max_output_tokens: null` means discovery did not expose a trustworthy per-model output limit; it is not the same thing as a runtime request default.

## Capabilities & Tasks

Capabilities are facts about one model through one provider. The same underlying model family can have different tools, reasoning, modalities, context window, and output limits depending on provider.

`reasoning.supported` is a boolean only. It says whether the provider advertises some reasoning/thinking capability for that model; effort levels, budgets, endpoint choice, and request payload shape remain adapter responsibilities. A catalog entry saying reasoning is supported does not by itself authorize sending a specific control field such as OpenAI-style `reasoning_effort`.

`input_modalities`, `output_modalities`, `supported_parameters`, and `task_types` preserve sanitized provider-catalog facts when available. `task_types` is a coarse filtering and routing projection used by accessors and task-model discovery; it is not provider request shaping. The authoritative task ordering and derivation logic live in `core/models/models.py`, and task-model bindings must stay aligned with `.vorch/specs/model_tasks.md`.

Sparse catalogs remain usable. Missing modality data defaults to text-in/text-out, and local or OpenAI-compatible providers with conservative optional facts should not disappear from model selection merely because fields such as `tools` or large `context_window` are missing.

Speech/audio modality aliases are intentionally strict: `transcription` output counts as text output and enables STT filtering; `speech` output enables TTS and audio-generation filtering; generic `audio` output enables `audio_generation` only and does not imply `text_to_speech`.

## Discovery & Refresh

`core/models/discovery.py` owns fetch, normalization, override application, generated file writes, and registry invalidation. The registry remains the read path and does not call provider APIs.

Discovery dispatches by `ProviderConfig.adapter` to `adapter_class.normalize_catalog_entry(raw, defaults)`. Provider-specific catalog schemas belong in provider adapters and provider specs, not in provider-ID branches inside discovery. Examples: OpenRouter-specific catalog behavior lives in `OpenRouterAdapter` and `.vorch/specs/providers/openrouter.md`; GitHub Copilot catalog metadata and runtime policy live in `GitHubCopilotAdapter` and `.vorch/specs/providers/github-copilot.md`.

Discovery builds auth headers from the selected connection plus provider `extra_headers`, then calls optional adapter hooks such as `discovery_headers(provider_config, credential_value, headers)`, `discovery_params()`, and `supplementary_discovery_params()`. Use these hooks for catalog-only requirements such as OpenAI Subscription account headers or OpenRouter supplementary STT/TTS fetches.

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
