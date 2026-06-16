# OpenAI Provider

Single `openai` provider covering both OpenAI Platform API-key access and ChatGPT Plus/Pro subscription access. One `OpenAIAdapter` class branches on a per-connection `mode` to pick between `/chat/completions` and `/codex/responses`.

## Interfaces

- Provider config: `resources/providers/openai.json`
- Adapter selector: `openai`
- Adapter class: `OpenAIAdapter` (subclass of `OpenAICompatibleAdapter`)
- Connections:
  - `openai:api-key` — `type: api_key`, `auth.credential_key: OPENAI_API_KEY`, `base_url` defaults to the provider-level OpenAI Platform URL. Default mode (`/chat/completions`).
  - `openai:subscription` — `type: oauth`, `base_url: https://chatgpt.com/backend-api`, `mode: codex_responses`, `models_endpoint: /codex/models`. ChatGPT Plus/Pro Codex OAuth device flow.
- Runtime endpoints: `POST <base_url>/chat/completions` (api-key) and `POST <base_url>/codex/responses` (subscription).
- Catalog: the provider has no provider-level `models_endpoint`. Only the `subscription` connection carries `models_endpoint`; refresh of the `api-key` connection is not supported in this provider.

## Connection Configuration

Per-connection fields carried by `ConnectionConfig`:

- `mode: str | None` — adapter-interpreted wire-variant selector. `OpenAIAdapter` reads it at construction; `None` and the value `"chat_completions"` both mean the generic `/chat/completions` path. The only other defined value is `codex_responses`.
- `models_endpoint: str | None` — discovery endpoint, overrides the provider-level value. Used by `subscription` for `/codex/models`.
- `base_url: str | None` — overrides the provider-level base URL. Used by `subscription` to point at `chatgpt.com/backend-api`.

`mode` and `models_endpoint` must be strings when present; non-string values are a config error.

The adapter is selected by provider `adapter`, not by connection, so the same `OpenAIAdapter` class is instantiated for both connections. `get_adapter` threads the connection's `mode` into the adapter as `connection_mode`.

## Wire Contract

### Chat Completions (default — `api-key` connection)

Used when `connection_mode` is `None` or `chat_completions`. Delegates to `OpenAICompatibleAdapter`; behavior is unchanged from the generic OpenAI-compatible contract:

- Canonical system/user/assistant messages stay in the OpenAI-style `messages` array.
- Canonical `tool` messages become `role: tool` messages with `tool_call_id`.
- Canonical assistant `tool_calls` become OpenAI function-call structures.
- Provider tool definitions become `{"type":"function","function":{...}}` entries.
- Streaming uses `stream: true`, SSE `data:` frames, `[DONE]`, and `stream_options.include_usage: true`; caller-provided `stream_options` are preserved except `include_usage` is forced true.
- The Codex extra headers (`OpenAI-Beta`, `originator`) must **not** be added on this path.

### Codex Responses (`subscription` connection — `mode: codex_responses`)

- Requests use the shared Responses payload builder and post to `/codex/responses` relative to the connection's `base_url`.
- The Codex backend requires an `instructions` field. The adapter uses assembled system instructions when present and falls back to `You are a helpful assistant.`.
- The Codex backend requires `store: false`; omission is rejected like an enabled store request.
- The Codex backend rejects output-token limit parameters. The adapter filters both `max_tokens` and `max_output_tokens` instead of forwarding provider defaults or caller kwargs.
- Streaming consumes Responses-style SSE events and yields normalized vBot deltas only. Non-streaming Responses objects use the shared output, usage, reasoning, and tool-call extraction also used for Copilot Responses.

## OAuth (subscription connection)

- The flow is marked with `oauth.device_flow: openai_codex`; this is distinct from the standard RFC 8628-style Device Flow used by GitHub OAuth.
- Device authorization posts JSON `{"client_id": ...}` to `https://auth.openai.com/api/accounts/deviceauth/usercode`; the user verifies at `https://auth.openai.com/codex/device`.
- Polling posts JSON `{"device_auth_id": ..., "user_code": ...}` to the matching `/token` device-auth endpoint. HTTP 403 and 404 are treated as `authorization_pending` for this provider.
- Successful polling returns an authorization code and PKCE verifier; vBot exchanges them at `https://auth.openai.com/oauth/token` with `grant_type=authorization_code` and `redirect_uri=https://auth.openai.com/deviceauth/callback`.
- Refresh uses the OAuth `refresh_token` grant against the same token endpoint. Refreshed tokens keep a replacement refresh token when OpenAI sends one and preserve the existing token otherwise.
- The OAuth token file path is `<data_dir>/oauth/openai-subscription.json` for the `default` account and `openai-subscription--<account>.json` for additional named accounts (see `providers.md` → Accounts).

## ChatGPT Account Header

- Access tokens are JWTs whose payload contains the claim `https://api.openai.com/auth`; that claim contains `chatgpt_account_id`.
- Runtime and discovery requests for the `subscription` connection must send both `Authorization: Bearer <token>` and `chatgpt-account-id: <account-id>`.
- If the account id is missing or blank, the adapter raises `ProviderAuthError` and asks the user to reconnect.
- `TokenStore` may mirror the account id in token metadata, but request headers are derived from the current JWT rather than guessed independently.

## Adapter-Owned Codex Headers

The Codex required extra headers live in the adapter, not in provider-level `extra_headers`:

```
CODEX_EXTRA_HEADERS = {"OpenAI-Beta": "responses=experimental", "originator": "vbot"}
```

`OpenAIAdapter._build_headers()` (and `discovery_headers()`) merge `CODEX_EXTRA_HEADERS` **only** on the `codex_responses` path. The chat-completions path uses the inherited `OpenAICompatibleAdapter._build_headers()` and must never include them.

## Reasoning

- vBot `thinking_effort` and raw `reasoning_effort` map to the nearest safe OpenAI effort: `minimal -> low`, `low/medium/high` stay exact, `xhigh/max -> high`.
- Generic OpenAI-compatible gateways omit explicit `none`; the direct OpenAI provider may send `none` only when catalog data confirms reasoning support.
- If injected `model_lookup` says reasoning is unsupported, reasoning request controls are stripped.
- Opaque reasoning fields such as `encrypted_content` and `reasoning_details` stay in `reasoning_meta` for round-tripping.
- On the Codex Responses path, supported reasoning efforts are `low`, `medium`, `high`, and `xhigh`; `max` maps to `xhigh`.

## Response And Catalog Normalization

- Text becomes `content` or `content_delta`; provider reasoning text fields such as `reasoning_content`/`thinking` become visible `reasoning`/`reasoning_delta`.
- Malformed tool-call argument JSON is ignored for that tool call instead of becoming fake empty arguments; valid sibling tool calls are preserved.
- Generic `/models` entries may expose modalities, supported parameters, context windows, and output limits through raw fields, `architecture`, or `top_provider`. Normalize discoverable facts into `Model.capabilities` and `Model.metadata`; do not treat sparse catalogs as negative evidence for every missing capability.
- Missing per-model output-token limits remain `max_output_tokens: null`; request fallback limits come from provider defaults such as `max_tokens: 8192`.
- `OpenAIAdapter.normalize_catalog_entry()` preserves provider-discovered ids, names, modalities, and limits, and normalizes capability parameters to vBot runtime names such as `tools`, `response_format`, `reasoning`, and `parallel_tool_calls`. Today only the `subscription` connection runs discovery; if `api-key` ever gains a `models_endpoint`, the adapter normalization must be reviewed for that path.

## Codex Catalog (`/codex/models`)

- `models_endpoint` is `/codex/models`; the `subscription` connection participates in `model.refresh_db` after OAuth is usable.
- Discovery sends the same account-routing and beta/originator headers as runtime requests. `/codex/models` also requires `client_version=0.136.0`; older values such as `0.1.0` can return a valid but empty model list.
- `/codex/models` may return entries in a top-level `models` list rather than `data`, with ids/names exposed as `slug` and `display_name`.
- Sparse `/codex/models` entries remain usable as text Codex Responses models: tools, structured output, and reasoning default to supported unless the catalog explicitly says otherwise. Unknown context-window and max-output-token facts stay `null` (the OpenAI-compatible base normalizer the Codex path delegates to emits honest `None`, never a placeholder `0`); the read-side `resolve_context_window` chain fills a window when needed.
- Do not hand-edit `resources/models/openai.json` for Codex entries; model refresh owns that file.

## Per-Model `connections` Allowlist

Each `Model` carries `connections: tuple[str, ...]`, loaded from `Model.connections` in the sanitized catalog:

- Empty tuple means the model is valid on every connection of its provider.
- A non-empty tuple restricts the model to the listed connection ids of its provider. Connection-bound Codex models (`connections: ["subscription"]`) are only offered on the subscription connection; Platform models (`connections: ["api-key"]`) only on the api-key connection.
- Target expansion in `core/model_tasks/` skips a connection for a model when `model.connections` is non-empty and the connection id is not in the list, so connection-restricted models do not produce cross-product targets against all usable connections.
- Refresh tags every discovered model with `connections: [<credential_connection.id>]` and merges into the existing catalog by replacing only models whose `connections` include the current connection id; models belonging to other connections are preserved.

## Usage Probe (`/wham/usage`)

The subscription usage fetcher in `core/providers/usage.py` (see `providers.md` →
Provider Usage Probe). Live-verified against the real endpoint 2026-06-16 (HTTP 200):

- `GET <connection.base_url>/wham/usage` (base_url `https://chatgpt.com/backend-api`).
- Headers mirror the Codex runtime path: `Authorization: Bearer <oauth token>`,
  `chatgpt-account-id: <id>` (from the JWT via `extract_chatgpt_account_id`, falling
  back to token-store `extra.chatgpt_account_id`), plus `CODEX_EXTRA_HEADERS`
  (`OpenAI-Beta`, `originator`). A missing account id → snapshot error "Reconnect required".
- Body (verified shape): `rate_limit.primary_window` + `secondary_window`, each
  `{used_percent, limit_window_seconds, reset_at}` with `reset_at` an **epoch-seconds**
  int; top-level `plan_type` (lowercase, e.g. `"plus"`); `credits.{has_credits, balance}`
  where `balance` is a **string**.
- Normalization: primary window label = `{hours}h` from `limit_window_seconds`;
  secondary label = `Week` / `Day` / `{hours}h` by cadence; `plan = plan_type`, with
  `· <balance> credits` appended only when `has_credits` is true and the (string)
  balance parses > 0.

## Error Classification

- 401/403 -> `ProviderAuthError`
- 429 -> `ProviderRateLimitError`
- 502/503 -> retryable `ProviderError`
- Other 4xx/5xx -> non-retryable `ProviderError`
- Timeout -> `ProviderTimeoutError`
- Connect errors -> `NetworkError`

## Constraints & Gotchas

- Provider defaults are merged with `setdefault`; caller kwargs win.
- Extra headers are merged after auth headers.
- The Codex `OpenAI-Beta` and `originator` headers are adapter-owned and must never leak into the chat-completions path. Adding them to provider-level `extra_headers` is forbidden.
- Only one adapter class (`OpenAIAdapter`) exists for this provider; the wire variant is selected per construction from `connection_mode`. Do not introduce a separate `openai_subscription` provider or adapter.
- Do not route the `subscription` connection through the generic `/chat/completions` path; its supported runtime path is `/codex/responses`.
- The OpenAI Codex Device Flow fields are provider-specific metadata parsed by `OAuthConfig`; standard OAuth providers should continue using `device_flow: oauth2`.
- Token values, authorization codes, user codes, refresh tokens, and account ids must never be logged.
