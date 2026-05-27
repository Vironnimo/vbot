# Providers

Provider configuration, credential resolution, adapter creation, retry/error classification, and provider-specific request/response translation.

## Overview

`core/providers/` translates canonical vBot chat requests into external provider wire protocols. Provider configs live in `resources/providers/*.json`; normalized model catalogs live in `resources/models/*.json`; runtime wires adapters with connection-scoped credentials and provider-scoped model lookup. Concrete provider behavior lives in child specs under `.vorch/specs/providers/`.

## Data Model

- `AuthConfig`: auth header name, value prefix, and credential key for API-key connections.
- `OAuthConfig`: device-flow endpoints, scopes, optional token exchange URL.
- `ConnectionConfig`: provider-local connection id, type (`api_key` or `oauth`), display label, auth metadata, optional base URL override, optional OAuth metadata.
- `ProviderConfig`: id, name, adapter selector, base URL, connections, defaults, extra headers, and optional `models_endpoint`.
- `ProviderRegistry`: loads provider JSON configs, rejects duplicate ids, and returns configs by id.
- `TokenStore`: persists OAuth tokens below `<data_dir>/oauth/` using `<provider_id>-<local_connection_id>.json`.
- `TokenGetter`: async credential source used by adapters (`StaticTokenGetter` or `OAuthTokenGetter`).

## Interfaces

- `ProviderRegistry.load(resources_dir) -> ProviderRegistry`
- `ProviderRegistry.get(provider_id) -> ProviderConfig`
- `ProviderRegistry.list_ids() -> list[str]`
- `runtime.get_adapter(provider_id, connection_id) -> ProviderAdapter`
- `ProviderCredentialResolver.has_credentials(provider_id, connection_id=None) -> bool`
- `ProviderCredentialResolver.get_credentials(provider_id, connection_id=None) -> str`
- Server RPC `provider.set_key` writes API-key connection credentials into the
    data-dir `.env` using the connection's configured `credential_key`, then
    reloads runtime provider credential fallback state.
- `ProviderAdapter.send(messages, *, model_id, **kwargs) -> dict`
- `ProviderAdapter.stream(messages, *, model_id, **kwargs) -> AsyncIterator[dict]`
- `ProviderAdapter.normalize_response(response) -> dict`

## Specific Specs

- `providers/openai.md` - OpenAI provider and generic OpenAI-compatible adapter behavior
- `providers/anthropic.md` - Anthropic Messages adapter
- `providers/openrouter.md` - OpenRouter-specific runtime/catalog behavior
- `providers/opencode-go.md` - OpenCode Go routing and reasoning replay behavior
- `providers/mistral.md` - Mistral-specific reasoning and catalog normalization
- `providers/github-copilot.md` - GitHub Copilot OAuth, endpoint routing, policy, and catalog metadata

## Adding A Provider

1. Add `resources/providers/<name>.json` with adapter selector, connections, and `models_endpoint` if refreshable.
2. Inspect the real models endpoint response before designing catalog normalization.
3. Send at least one probe inference request against the runtime endpoint you plan to support.
4. Verify real behavior for reasoning controls, tools, streaming, output-token limits, and response shape.
5. Put discoverable facts in adapter normalization/runtime logic. Use model overrides only for durable facts that provider APIs do not expose.

## Conventions

- Connection ids exposed outside provider config are compositional: `<provider_id>:<local_connection_id>`.
- Provider JSON uses `connections`; old single-provider `auth` JSON is not supported.
- Runtime adapter creation injects provider-scoped `model_lookup(model_id) -> Model | None` so adapters can use normalized catalog facts without file I/O.
- Provider defaults are applied with lower priority than caller kwargs.
- Streaming adapters yield normalized vBot deltas only, never raw provider chunks.
- Catalog normalization should preserve discoverable model modalities,
  supported request parameters, and other small runtime-relevant facts in the
  sanitized `Model.capabilities`/`Model.metadata` shape. Sparse local or
  OpenAI-compatible catalogs should remain usable rather than being interpreted
  as authoritative negatives for every missing capability.
- Token values and API keys must never be logged.

## Constraints & Gotchas

- `runtime.get_adapter()` requires an explicit connection id; there is no runtime fallback to the first usable connection.
- API-key credentials resolve at adapter creation from process env or data-dir `.env`; OAuth credentials come from `TokenStore` and may refresh during requests.
- API-key provider setup through CLI/RPC is credential-key based: callers name a
    provider and optional connection, and vBot chooses the configured env key from
    provider metadata. CLI output must not include credential values.
- Streaming retry only covers connection establishment. Once an SSE stream is open, mid-stream errors propagate.
- Provider catalogs under `resources/models/` are refreshable artifacts. Durable behavior belongs in adapter code or policy, not hand-edited generated model files.
- Network failures use `NetworkError` and are retryable but do not trigger model fallback.
