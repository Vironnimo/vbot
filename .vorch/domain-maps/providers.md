# Providers

`core/providers/` owns Provider configuration, credential and Account resolution, adapter contracts, shared wire policy, and Provider-specific request/response translation.

## Overview

Providers translate canonical vBot requests and responses at the external-service boundary. Bundled Provider and Connection definitions live in `resources/providers/*.json`; user-defined OpenAI-compatible Providers live as secret-free records under `settings.json` `providers.custom` and are materialized through the same registry. Model facts and refreshable catalogs belong to the Models domain. Runtime resolves one exact Provider Connection, creates the configured outer Adapter, and injects credentials, Model lookup, connection mode, and optional debug capture. Chat and task domains consume normalized interfaces and must not know Provider wire fields.

A Provider can expose multiple Connection variants through one Adapter or route Models to different wire implementations inside a Provider-owned Adapter. Runtime selects only the outer Adapter from Provider config; per-Model protocol selection remains Provider policy.

## Terms

Core terms Provider, Model, and Reasoning live in `.vorch/GLOSSARY.md`; Model-DB terms live in `models.md`.

### Adapter

**Definition:** The code owner that translates between vBot's normalized contracts and one or more external Provider wire protocols. It owns request shaping, response/SSE normalization, wire capability, and Provider-specific policy.

**Not:** A Provider config, Connection, Account, or Model.

### Connection

**Definition:** A statically declared authentication and wire variant inside one Provider, addressed `provider:connection`. It selects auth type, optional base URL, optional discovery endpoint, wire mode, and automatic-catalog behavior.

**Not:** An Account or an HTTP connection. Accounts choose credentials without changing wire or catalog identity.

### Account

**Definition:** A named credential slot on one Connection, addressed `provider:connection[:account]`; the omitted Account resolves deterministically to the first usable slot (`default`, then sorted named Accounts).

**Not:** A product user identity or a distinct Model catalog.

### Usable

**Definition:** A Connection that is both enabled and credentialed. `ProviderCredentialResolver.is_usable()` is the behavior gate; `has_credentials()` answers only the credential-management question.

**Not:** Merely configured or reachable. A keyless Connection is credentialed by definition but disabled by default until the user opts in.

## Boundaries and invariants

- `ProviderRegistry` owns immutable Provider/Connection configuration assembled from bundled JSON plus the current data directory's normalized Custom Provider overlay, including exact `catalog_exclusions` for ids a Provider advertises but cannot serve. Custom registries are never shared through the bundled-only cache: two Runtimes using the same resources but different data directories must remain isolated. `ProviderCredentialResolver` owns credentials, Accounts, enabled overrides, and usability. Runtime owns wiring them into live Adapters.
- Connection identity determines wire/auth/catalog behavior; Account identity determines only which credential is used. Discovery and task-target expansion stay Connection-scoped and never multiply catalogs per Account.
- `ProviderAdapter.send()`, `stream()`, and `normalize_response()` are the Chat-facing translation boundary. Completed responses and stream finishes carry one canonical terminal outcome; streaming Tool fragments use a stable slot so a Provider id that arrives late can still become the final call identity. Raw SSE event names, response chunks, opaque auth state, and Provider payloads never escape the Adapter layer.
- `core/providers/tool_schema.py` owns canonical-to-wire Tool rendering through the `explicit_non_strict` and `omit_strict` profiles. vBot never enables Provider strict mode and never rewrites canonical Tool schemas to satisfy a Provider's strict-schema subset. Every Responses wire emits `strict: false` because OpenAI Responses may otherwise normalize an omitted flag into strict mode; contracts without the field receive the unchanged canonical schema without it. Runtime argument validation remains authoritative on every wire.
- `core/providers/adapter.py::normalize_tool_call_ids()` owns target-wire Tool-call identifier constraints. It deep-copies the outgoing request view and rewrites each Assistant call together with its immediately following Tool Result; canonical Session ids never change. Only Adapters with an explicit verified profile apply this transform.
- Provider-specific wire selectors and replay profiles are scoped to the resolved Connection/Wire plus Model; mechanics stay in the Adapter. A Provider-wide default may be a conservative fallback, never evidence that every Model shares the same reasoning contract. Do not add generic Model fields for one Provider's protocol quirk or route Provider Models in Runtime/Chat.
- Model facts remain honest. Unknown context/output limits remain absent in catalogs; read-side helpers resolve safe effective values without writing fake facts back to generated files.
- All secrets and tokens remain behind credential resolvers or token getters. Never log API keys, access/refresh tokens, authorization/user codes, Provider Account ids, or raw auth headers.

## Cross-task interfaces and source routing

- Configuration, registry, Connection defaults, context-window helpers: `core/providers/providers.py`
- Account id grammar and environment-key derivation: `core/providers/accounts.py`
- Credential, enablement, and usability resolution: `core/providers/credentials.py`
- OAuth persistence/refresh and device flow: `core/providers/token_store.py`, `token_getter.py`, `auth_flow.py`
- Adapter contract, canonical terminal outcomes, target-wire Tool-call identifier profiles, and shared HTTP/error layer: `core/providers/adapter.py`, `_http_shared.py`, `errors.py`
- Shared reasoning decision policy: `core/providers/reasoning.py`
- Shared non-strict Tool-schema rendering: `core/providers/tool_schema.py`
- Runtime Adapter factory: `_ADAPTER_MAP` and `Runtime.get_adapter()` in `core/runtime/runtime.py`
- Model discovery integration: `core/models/discovery.py`; Model data semantics remain in `models.md`

## Conventions and gotchas

- Adapter creation requires an exact compositional Connection id. It never silently falls back to another Connection; only an omitted Account within that Connection resolves to the first usable Account.
- Provider JSON uses `connections`; the obsolete single-provider `auth` shape is invalid. Provider ids cannot contain `:`; Connection ids cannot contain `:` or `--`.
- A Custom Provider cannot shadow a bundled Provider id. Its normalized Settings record selects the allowlisted `openai_compatible` Adapter, materializes one `default` Connection, and derives `VBOT_CUSTOM_<ID>_API_KEY` for bearer-key auth; `auth: "none"` is the keyless form. Settings/RPC never contain the key value.
- A mostly OpenAI Chat Completions-compatible Provider extends `OpenAICompatibleAdapter` only when it has real runtime/discovery/policy differences. A Messages-compatible branch composes `AnthropicCompatibleAdapter`; it must not compose concrete `AnthropicAdapter` and accidentally inherit Anthropic-native discovery/media/cache policy.
- Adapters rebuild authorization headers inside retry attempts so a refreshed OAuth token is used after backoff. Do not cache raw OAuth tokens outside `TokenGetter`.
- `NetworkError` is retryable but deliberately not Provider-specific, so it must not trigger model fallback. Only `ProviderStreamingUnsupportedError` permits Chat's streaming-to-nonstreaming fallback.
- Provider strict mode is forbidden. OpenAI Chat and every Responses-family wire (OpenAI, OpenRouter, and GitHub Copilot) must retain the explicit `strict: false` opt-out; OpenAI Responses can otherwise enable strict normalization when the field is absent. Anthropic Messages enables strict Tool use only when explicitly requested, while the Anthropic, GitHub Copilot Messages, MiniMax, Mistral, Ollama, OpenCode Go, OpenRouter Chat, GitHub Copilot Chat, and Custom Provider wires omit the unsupported or unnecessary field. Never make optional fields required, add nullable placeholders, or strip schema keywords for Provider compatibility; preserve the canonical schema and runtime enforcement.
- Generated Provider catalogs are refresh artifacts. Durable behavior belongs in Adapter code or verified override files, not hand edits to generated `resources/models/<provider>.json`.
- A Provider listing that contains proven-unusable ids uses `catalog_exclusions` in its static Provider config; discovery preserves the raw response and omits only those exact ids from the usable Model projection. Do not use this as a preference allow/deny list.

## References

Read these only when your task matches — not by default.

- Changing Provider/Connection config, Accounts, credentials, enablement, OAuth/device flow, token storage/refresh, Connection RPCs, or local reachability → `providers/connections.md`
- Changing Model endpoint discovery, catalog normalization, connection-scoped merge, supplementary/task feeds, refresh retry behavior, or local auto-refresh → `providers/catalog-discovery.md`
- Changing Adapter request/response/SSE behavior, error/retry policy, reasoning, CoT replay, media support, output/context limits, or shared task HTTP plumbing → `providers/request-policy.md`
- Changing live subscription limits, usage fetchers/parsers, caching, timeout/fail-open behavior, or `provider.usage` → `providers/usage.md`
- Adding a Provider, Connection variant, Adapter selector, discovery normalizer, or Provider-specific map → `providers/add-a-provider.md`
- Changing native Anthropic Messages behavior → `providers/anthropic.md`
- Changing GitHub Copilot auth, routing, policy, or catalog metadata → `providers/github-copilot.md`
- Changing Kimi Coding Plan/Platform Connections, reasoning replay, media, or catalog behavior → `providers/kimi.md`
- Changing MiniMax wire, reasoning, catalog, or usage parsing → `providers/minimax.md`
- Changing Mistral request policy or catalog normalization → `providers/mistral.md`
- Changing Ollama native chat, local/cloud Connections, enrichment, or context enforcement → `providers/ollama.md`
- Changing LM Studio native discovery, lazy loading, or OpenAI-compatible Chat behavior → `providers/lmstudio.md`
- Changing OpenAI Platform or ChatGPT subscription behavior → `providers/openai.md`
- Changing OpenCode Go's per-Model OpenAI/Messages routing → `providers/opencode-go.md`
- Changing OpenRouter runtime, routing, prompt caching, catalog, reasoning, or task discovery → `providers/openrouter.md`
- Changing xAI API-key/SuperGrok auth, Responses policy, reasoning replay, media, or catalog behavior → `providers/xai.md`
