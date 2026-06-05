# Providers

Provider configuration, credential resolution, adapter creation, retry/error classification, and provider-specific request/response translation.

## Overview

`core/providers/` translates canonical vBot chat requests into external provider wire protocols. Provider configs live in `resources/providers/*.json`; normalized model catalogs live in `resources/models/*.json`; runtime creates adapters with one explicit connection, an async token getter, provider defaults, optional debug capture, and provider-scoped model lookup. Concrete wire behavior belongs in the provider child specs under `.vorch/specs/providers/`; keep the root spec focused on shared contracts and extension rules. Direct OpenAI Platform access and ChatGPT subscription access are separate providers because credentials, endpoints, account routing, and supported runtime APIs differ.

## Data Model

- Provider JSON owns `id`, `name`, adapter selector, base URL, `connections`, optional `defaults`, optional `extra_headers`, and optional `models_endpoint`.
- Connection JSON is provider-local until exposed externally as `<provider_id>:<local_connection_id>`. Supported connection types are `api_key` and `oauth`; API-key connections require `auth.credential_key`, while OAuth Device Flow connections store their flow metadata under `oauth`.
- `ProviderRegistry.load(resources_dir)` parses `resources/providers/*.json`, rejects duplicate provider ids/connection ids, validates connection types/OAuth device-flow names, and caches the registry by resolved resources path.
- `TokenStore` persists OAuth tokens under `<data_dir>/oauth/<provider_id>-<local_connection_id>.json` after validating ids against path traversal. Expired tokens still count as usable when they have a refresh path (`refresh_token` or Copilot's stored `github_oauth_token`).

## Interfaces

- `runtime.get_adapter(provider_id, connection_id)` is the adapter factory. It requires a full compositional connection id, resolves the selected connection, injects `StaticTokenGetter` or `OAuthTokenGetter`, injects `model_lookup(model_id)`, and passes a `ProviderDebugRecorder` when debug mode is enabled.
- `ProviderCredentialResolver` reads API-key credentials from process environment first, then the data-dir `.env` fallback. OAuth credentials come from `TokenStore`; OAuth connections without `oauth` metadata are treated as static-token stubs.
- Server RPC `provider.set_key` only targets API-key connections. It writes the connection's configured `credential_key` to the data-dir `.env`, reloads runtime fallback credentials, and never returns the secret value.
- Model discovery uses `models_endpoint`, the first usable connection, adapter `discovery_headers()`/`discovery_params()`/`supplementary_discovery_params()` hooks, and `normalize_catalog_entry()`. Discovery accepts top-level `data` or `models` lists and writes refreshable catalog artifacts.
- `ProviderAdapter.send()`, `stream()`, and `normalize_response()` are the chat-facing contract. `stream()` yields normalized vBot deltas only; raw provider chunks and SSE event names stay inside adapters.

## Specific Specs

- `providers/openai.md` - Direct OpenAI Platform provider and generic OpenAI-compatible adapter behavior.
- `providers/anthropic.md` - Anthropic Messages adapter.
- `providers/openrouter.md` - OpenRouter runtime, reasoning, and multi-modality catalog discovery.
- `providers/opencode-go.md` - OpenCode Go routing and reasoning replay behavior.
- `providers/mistral.md` - Mistral-specific reasoning and catalog normalization.
- `providers/minimax.md` - MiniMax OpenAI-compatible endpoint, sparse catalog normalization, and thinking controls.
- `providers/github-copilot.md` - GitHub Copilot OAuth, endpoint routing, policy, and catalog metadata.
- `providers/openai-subscription.md` - OpenAI Subscription Codex OAuth, ChatGPT account header, model discovery, and Responses routing.

## Adding A Provider

1. Add `resources/providers/<name>.json` with an adapter selector, at least one connection, and `models_endpoint` only when the catalog is refreshable.
2. Inspect the real models endpoint response before designing catalog normalization; preserve discoverable modalities, request parameters, limits, and small runtime-relevant metadata.
3. Send at least one probe inference request against the runtime endpoint you plan to support, including streaming when the provider advertises it.
4. Verify real behavior for reasoning controls, tools, structured output, streaming, output-token limits, and response shape.
5. Put discoverable facts in adapter normalization/runtime policy. Use model overrides only for durable facts provider APIs do not expose.

## Conventions

- Provider defaults are request defaults, not model facts. They are applied with lower priority than caller kwargs; unknown per-model output limits stay `max_output_tokens: null` in catalogs even when request defaults contain `max_tokens`.
- Provider JSON uses `connections`; old single-provider `auth` JSON is invalid.
- Provider chat HTTP clients use bounded connect/write/pool timeouts and no read timeout; long non-streaming generations may exceed one minute. Streaming stalls are guarded by the chat streaming chunk timeout.
- Sparse OpenAI-compatible catalogs remain usable as text chat catalogs. Missing optional facts are unknown, not authoritative negatives, unless the provider-specific adapter says otherwise.
- Subclass `OpenAICompatibleAdapter` only when runtime behavior, streaming, reasoning, catalog normalization, or request policy differs from the generic `/chat/completions` contract.
- Token values, API keys, authorization codes, user codes, refresh tokens, and account ids must never be logged.

## Constraints & Gotchas

- Runtime adapter creation has no fallback to "first usable connection"; callers must pass the exact connection id. Provider-level credential checks may choose the first usable connection only for status/listing/discovery helper paths.
- API-key credentials resolve when an adapter is created. OAuth tokens may refresh during requests through `OAuthTokenGetter`, so do not cache the raw OAuth access token outside the getter.
- Streaming retry only covers connection establishment. Once an SSE stream is open, mid-stream read/provider errors propagate.
- `ProviderStreamingUnsupportedError` is the only streaming error that triggers the chat loop's non-streaming fallback. Other streaming failures are not silently retried as non-streaming requests.
- Provider catalogs under `resources/models/` are refreshable artifacts. Durable behavior belongs in adapter code, runtime policy, or externally verified overrides, not hand-edited generated model files.
- `NetworkError` is retryable but not provider-specific and must not trigger model fallback.
