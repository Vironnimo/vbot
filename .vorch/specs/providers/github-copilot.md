# GitHub Copilot Provider

GitHub Copilot provider with OAuth Device Flow, endpoint-aware runtime policy, and Copilot-specific catalog metadata.

## Interfaces

- Provider config: `resources/providers/github-copilot.json`
- Adapter selector: `github_copilot`
- Adapter class: `GitHubCopilotAdapter`
- OAuth connection: `github-copilot:oauth`
- OAuth client id: public GitHub Copilot app client id from bundled provider config.
- Endpoint helpers: `github_copilot_responses.py`, `github_copilot_messages.py`, and `github_copilot_policy.py`.

## OAuth

- Device Flow scope is `read:user`.
- After GitHub OAuth, Copilot token exchange is a `GET` with `Authorization: Bearer <github_oauth_token>`, `Accept: application/json`, `Copilot-Integration-Id: vscode-chat`, and `Editor-Version: vBot/0.1.0`.
- The Token Store persists the Copilot API token as `access_token`, expiry as `expires_at`, and the GitHub OAuth token in `extra.github_oauth_token` for refresh.
- Do not use GitHub's older `Authorization: token ...` scheme for Copilot exchange.

## Endpoint Policy

- `/chat/completions`: conservative fallback through `OpenAICompatibleAdapter` after Copilot policy filters kwargs.
- `/responses`: OpenAI Responses-like helper that normalizes output items, tool calls, usage, reasoning metadata, readable reasoning summaries, and semantic SSE events.
- `/v1/messages`: Anthropic Messages-like helper for Claude-style models, content blocks, tools, thinking/output config, and SSE normalization.
- `ws:/responses` is ignored; websocket Responses frames are not implemented.

Endpoint selection uses model metadata first:

- Claude-like models prefer `/v1/messages` when advertised.
- Gemini-like models stay chat-first and are not forced onto `/responses`.
- OpenAI/GPT-like models prefer `/responses` when advertised.
- Unknown models default to `/chat/completions`, no explicit reasoning/thinking controls, no tools, and no structured-output controls.

## Runtime Policy

- `GitHubCopilotAdapter` receives provider-scoped `model_lookup` and calls policy once per send/stream request.
- `metadata.github_copilot` is the primary source for vendor, family, supported endpoints, reasoning efforts, thinking budget bounds, adaptive thinking, tools, streaming, and structured output.
- Static policy entries are fallback facts or exact-model overrides for validated quirks.
- Unsupported optional features are omitted rather than sent optimistically.
- For `/responses`, `temperature` is omitted unless a future policy explicitly proves support.

## Catalog Normalization

- `normalize_catalog_entry()` reads Copilot `capabilities.limits.max_context_window_tokens`, `capabilities.limits.max_output_tokens`, and `capabilities.supports`.
- Missing or non-numeric `capabilities.limits.max_output_tokens` is stored as `null`; reported numeric values remain authoritative even when they are small values such as `4096`.
- Reasoning is supported when Copilot advertises reasoning effort or thinking-budget support.
- Normalized `Model.capabilities` includes chat-oriented modality/task defaults plus Copilot-advertised vision/tools/structured-output/reasoning facts.
- Sanitized runtime metadata only is stored under `metadata.github_copilot`; raw provider data, policy terms, and credentials are not stored.

## Constraints & Gotchas

- Exact-model quirks belong in `core/providers/github_copilot_policy.py`, not hand-edited `resources/models/github-copilot.json`.
- Copilot Responses tool calls may use nested `function.{name,arguments}`; helper code must preserve a non-empty name from either top-level or nested fields.
- Partial metadata stays conservative: omit uncertain controls instead of forwarding them.
- Token values must never be logged.
