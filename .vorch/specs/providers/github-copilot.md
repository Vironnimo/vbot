# GitHub Copilot Provider

GitHub Copilot provider with OAuth Device Flow, endpoint-aware runtime policy, and Copilot-specific catalog metadata.

## Interfaces

- Provider config: `resources/providers/github-copilot.json`
- Adapter selector: `github_copilot`
- Adapter class: `GitHubCopilotAdapter`
- OAuth connection: `github-copilot:oauth`
- Runtime helpers: `github_copilot_policy.py` chooses endpoint/request policy; `github_copilot_responses.py` and `github_copilot_messages.py` build and normalize non-chat endpoint payloads.

## OAuth

- Device Flow scope is `read:user` using GitHub's standard device-code endpoint.
- After GitHub OAuth, vBot exchanges the GitHub OAuth token for a Copilot API token with `Authorization: Bearer <github_oauth_token>`, `Accept: application/json`, `Copilot-Integration-Id: vscode-chat`, and `Editor-Version: vBot/0.1.0`.
- `TokenStore` persists the Copilot API token as `access_token`, expiry as `expires_at`, and the GitHub OAuth token in `extra.github_oauth_token` so `OAuthTokenGetter` can refresh by repeating the Copilot token exchange.
- Do not use GitHub's older `Authorization: token ...` scheme for Copilot exchange.

## Endpoint Policy

- `/chat/completions`: conservative fallback through `OpenAICompatibleAdapter` after Copilot policy filters unsupported kwargs.
- `/responses`: OpenAI Responses-like helper for output items, function calls, usage, reasoning metadata, readable reasoning summaries, and semantic SSE events.
- `/v1/messages`: Anthropic Messages-like helper for Claude-style models, content blocks, tools, thinking/output config, and SSE normalization.
- `ws:/responses` can appear in catalog metadata but is ignored; websocket Responses frames are not implemented.

Endpoint selection uses sanitized model metadata first:

- Claude-like models prefer `/v1/messages` when advertised.
- Gemini-like models stay chat-first when chat is advertised.
- OpenAI/GPT-like models prefer `/responses` when advertised.
- Unknown or metadata-poor models default to `/chat/completions`, with no explicit reasoning/thinking controls, no tools, and no structured-output controls unless exact fallback facts say otherwise.

## Runtime Policy

- `GitHubCopilotAdapter` receives provider-scoped `model_lookup` and computes policy once per send/stream request.
- `metadata.github_copilot` is the primary runtime source for vendor, family, supported endpoints, reasoning efforts, thinking budget bounds, adaptive thinking, tools, streaming, and structured output.
- Static fallback facts and exact-model overrides in `github_copilot_policy.py` cover validated quirks only.
- Unsupported optional features are omitted rather than sent optimistically. For `/responses`, `temperature` is omitted unless policy explicitly proves support.

## Catalog Normalization

- `normalize_catalog_entry()` reads Copilot `capabilities.limits.max_context_window_tokens`, `capabilities.limits.max_output_tokens`, and `capabilities.supports`.
- Missing or non-numeric `max_output_tokens` is stored as `null`; reported numeric values remain authoritative even when small.
- Reasoning is supported when Copilot advertises reasoning-effort values or thinking-budget bounds.
- Normalized `Model.capabilities` includes chat-oriented modality/task defaults plus Copilot-advertised vision/tools/structured-output/reasoning facts.
- Only sanitized runtime metadata is stored under `metadata.github_copilot`; raw provider data, policy terms, picker flags, and credentials are not stored.

## Constraints & Gotchas

- Exact-model quirks belong in `core/providers/github_copilot_policy.py`, not hand-edited `resources/models/github-copilot.json`.
- Copilot Responses tool calls may use nested `function.{name,arguments}`; helper code must preserve a non-empty name from either top-level or nested fields.
- Partial metadata stays conservative: omit uncertain controls instead of forwarding them.
- Token values must never be logged.
