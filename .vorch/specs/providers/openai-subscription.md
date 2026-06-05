# OpenAI Subscription Provider

ChatGPT Plus/Pro subscription access through the OpenAI Codex OAuth device flow and the ChatGPT Codex backend.

## Interfaces

- Provider config: `resources/providers/openai-subscription.json`
- Adapter selector: `openai_subscription`
- Adapter class: `OpenAISubscriptionAdapter`
- OAuth connection: `openai-subscription:oauth`
- Catalog endpoint: `GET https://chatgpt.com/backend-api/codex/models`
- Runtime endpoint: `POST https://chatgpt.com/backend-api/codex/responses`

## OAuth

- The flow is marked with `oauth.device_flow: openai_codex`; this is distinct from the standard RFC 8628-style Device Flow used by GitHub OAuth.
- Device authorization posts JSON `{"client_id": ...}` to `https://auth.openai.com/api/accounts/deviceauth/usercode`; the user verifies at `https://auth.openai.com/codex/device`.
- Polling posts JSON `{"device_auth_id": ..., "user_code": ...}` to the matching `/token` device-auth endpoint. HTTP 403 and 404 are treated as `authorization_pending` for this provider.
- Successful polling returns an authorization code and PKCE verifier; vBot exchanges them at `https://auth.openai.com/oauth/token` with `grant_type=authorization_code` and `redirect_uri=https://auth.openai.com/deviceauth/callback`.
- Refresh uses the OAuth `refresh_token` grant against the same token endpoint. Refreshed tokens keep a replacement refresh token when OpenAI sends one and preserve the existing token otherwise.

## Account Header

- Access tokens are JWTs whose payload contains the claim `https://api.openai.com/auth`; that claim contains `chatgpt_account_id`.
- Runtime and discovery requests must send both `Authorization: Bearer <token>` and `chatgpt-account-id: <account-id>`.
- If the account id is missing or blank, the adapter raises `ProviderAuthError` and asks the user to reconnect.
- `TokenStore` may mirror the account id in token metadata, but request headers are derived from the current JWT rather than guessed independently.

## Wire Contract

- Requests use the shared Responses payload builder from `github_copilot_responses.py` and post to `/codex/responses` relative to the provider base URL.
- The Codex backend requires an `instructions` field. The adapter uses assembled system instructions when present and falls back to `You are a helpful assistant.`.
- The Codex backend requires `store: false`; omission is rejected like an enabled store request.
- The Codex backend rejects output-token limit parameters. The adapter filters both `max_tokens` and `max_output_tokens` instead of forwarding provider defaults or caller kwargs.
- Supported reasoning efforts are `low`, `medium`, `high`, and `xhigh`; `max` maps to `xhigh`.
- Bundled extra headers are required: `OpenAI-Beta: responses=experimental` and `originator: vbot`.
- Streaming consumes Responses-style SSE events and yields normalized vBot deltas only. Non-streaming Responses objects use the shared output, usage, reasoning, and tool-call extraction also used for Copilot Responses.

## Catalog

- `models_endpoint` is `/codex/models`; the provider participates in `model.refresh_db` after the OAuth connection is usable.
- Discovery sends the same account-routing and beta/originator headers as runtime requests. `/codex/models` also requires `client_version=0.136.0`; older values such as `0.1.0` can return a valid but empty model list.
- `/codex/models` may return entries in a top-level `models` list rather than `data`, with ids/names exposed as `slug` and `display_name`.
- `OpenAISubscriptionAdapter.normalize_catalog_entry()` preserves provider-discovered ids, names, modalities, and limits, and normalizes capability parameters to vBot runtime names such as `tools`, `response_format`, `reasoning`, and `parallel_tool_calls`.
- Sparse `/codex/models` entries remain usable as text Codex Responses models: tools, structured output, and reasoning default to supported unless the catalog explicitly says otherwise. Unknown context-window and max-output-token facts stay `0`/`null`.
- Do not hand-edit `resources/models/openai-subscription.json`; model refresh owns that file.

## Constraints & Gotchas

- This provider represents a user ChatGPT subscription OAuth session, not the standard OpenAI Platform API-key provider.
- Do not route this provider through generic OpenAI-compatible `/chat/completions`; the supported runtime path is `/codex/responses`.
- The OpenAI Codex Device Flow fields are provider-specific metadata parsed by `OAuthConfig`; standard OAuth providers should continue using `device_flow: oauth2`.
- Token values, authorization codes, user codes, refresh tokens, and account ids must never be logged.
