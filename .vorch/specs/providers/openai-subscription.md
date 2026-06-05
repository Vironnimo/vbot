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

- The flow is marked with `oauth.device_flow: openai_codex`; this is distinct from the standard RFC 8628-style device flow used by GitHub OAuth.
- Device authorization starts with JSON `{"client_id": ...}` against `https://auth.openai.com/api/accounts/deviceauth/usercode`.
- The user verifies at `https://auth.openai.com/codex/device`.
- Polling posts JSON `{"device_auth_id": ..., "user_code": ...}` to the matching `/token` device-auth endpoint. HTTP 403 and 404 are treated as `authorization_pending` for this provider.
- Successful polling returns an authorization code and PKCE verifier; vBot then exchanges them at `https://auth.openai.com/oauth/token` with `grant_type=authorization_code` and `redirect_uri=https://auth.openai.com/deviceauth/callback`.
- Token refresh uses the OAuth `refresh_token` grant against the same token endpoint. Refreshed tokens keep a replacement refresh token when OpenAI sends one and preserve the existing token otherwise.

## Account Header

- Access tokens are JWTs that include the ChatGPT account id under `https://api.openai.com/auth.chatgpt_account_id`.
- The adapter must send both `Authorization: Bearer <token>` and `chatgpt-account-id: <account-id>`.
- If the account id is missing, the adapter raises `ProviderAuthError` and asks the user to reconnect. The Token Store may mirror the account id in token metadata, but request headers should be derived from the current JWT rather than guessed independently.

## Wire Contract

- Requests use the shared Responses payload builder from `github_copilot_responses.py` and post to `/codex/responses` relative to the provider base URL.
- The Codex backend requires an `instructions` field. The adapter uses the assembled vBot system prompt when present and falls back to a neutral default instruction when an Agent has no system prompt.
- The Codex backend requires `store: false`; omission is rejected like an enabled store request.
- The Codex backend rejects `max_output_tokens`; the adapter filters both `max_tokens` and `max_output_tokens` instead of forwarding output-token limits from provider defaults or caller kwargs.
- Reasoning efforts supported by the current Codex model catalog include `low`, `medium`, `high`, and `xhigh`; `max` maps to `xhigh`.
- Visible reasoning is emitted from Responses reasoning summary deltas, reasoning output-item summaries, and completed response output when the backend supplies readable summary text.
- Provider defaults are merged before caller kwargs; caller kwargs win.
- Bundled extra headers are required: `OpenAI-Beta: responses=experimental` and `originator: vbot`.
- Streaming consumes Responses-style SSE events and yields normalized vBot deltas only.
- Non-streaming Responses objects are normalized through the same output, usage, reasoning, and tool-call extraction used for Copilot Responses.

## Catalog

- `models_endpoint` is `/codex/models`; the provider participates in `model.refresh_db` after the OAuth connection is usable.
- Discovery sends the same auth and account-routing headers as runtime requests: `Authorization`, `chatgpt-account-id`, bundled `OpenAI-Beta`, and `originator`. `/codex/models` also requires a `client_version` query parameter in plain semantic-version format; the adapter sends the verified Codex client compatibility version `0.136.0`. Older values such as `0.1.0` can return a valid but empty model list.
- `/codex/models` may return model entries in a top-level `models` list rather than the OpenAI-compatible `data` list, with ids/names exposed as `slug` and `display_name`.
- `OpenAISubscriptionAdapter.normalize_catalog_entry()` owns catalog normalization. It preserves provider-discovered ids, names, modalities, and limits, and normalizes capability parameters to vBot runtime names such as `tools`, `response_format`, `reasoning`, and `parallel_tool_calls`.
- Sparse `/codex/models` entries remain usable as text Codex Responses models: tools, structured output, and reasoning default to supported unless the catalog explicitly says otherwise. Context-window and max-output-token facts stay `0`/`null` when the catalog does not disclose them.
- Do not hand-edit `resources/models/openai-subscription.json`; it is created and overwritten by model refresh like other discovery catalogs.

## Constraints & Gotchas

- This provider represents a user subscription OAuth session, not the standard OpenAI Platform API-key provider.
- Token values, authorization codes, user codes, refresh tokens, and account ids must never be logged.
- Do not route this provider through the generic OpenAI-compatible `/chat/completions` path; the supported runtime path is `/codex/responses`.
- The OpenAI Codex device flow fields are provider-specific metadata parsed by `OAuthConfig`; standard OAuth providers should continue using `device_flow: oauth2`.
