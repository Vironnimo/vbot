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
- After GitHub OAuth, vBot exchanges the GitHub OAuth token for a Copilot API token with `Authorization: Bearer <github_oauth_token>`, `Accept: application/json`, `Copilot-Integration-Id: vscode-chat`, and the minimum current client gate `Editor-Version: vscode/1.128.0`.
- `TokenStore` persists the Copilot API token as `access_token`, expiry as `expires_at`, and the GitHub OAuth token in `extra.github_oauth_token` so `OAuthTokenGetter` can refresh by repeating the Copilot token exchange. Numeric Unix and ISO-8601 exchange expiries are accepted.
- Token exchange may return an account-specific `endpoints.api`; vBot stores only a validated official HTTPS GitHub Copilot/GHE endpoint as `extra.copilot_api_endpoint`, with the exchanged token's `proxy-ep` as fallback, and uses that endpoint for both inference and Connection-scoped discovery on the next Adapter/refresh construction. This is required for Enterprise/proxied Accounts; an untrusted host is ignored.
- Do not use GitHub's older `Authorization: token ...` scheme for Copilot exchange.

## Endpoint Policy

- `/chat/completions`: conservative fallback through `OpenAICompatibleAdapter` after Copilot policy filters unsupported kwargs.
- `/responses`: OpenAI Responses-like helper for output items, function calls, usage, reasoning metadata, readable reasoning summaries, and semantic SSE events. Every function Tool carries `strict: false`; GitHub does not publish this private backend's default, so the shared Responses builder prevents an omitted field from inheriting OpenAI Responses' automatic strict normalization. The helper stores the complete original output-item sequence and assistant `phase`, then replays those items verbatim instead of reconstructing reasoning. Exact structured in-band error codes enter the shared Provider taxonomy and Chat recovery budget; retryable failures restart only before visible output, while incomplete responses finish with a safe non-Tool terminal outcome. Usage normalization maps `input_tokens_details.cached_tokens` (or `prompt_tokens_details`) to canonical `cache_read_tokens`; cached tokens are already included in the wire's input count.
- `/v1/messages`: Anthropic Messages-like helper for Claude-style models, content blocks, tools, thinking/output config, and SSE normalization. The stream path delegates its content-block/event state machine to the reusable wire's `AnthropicMessagesStreamDecoder` while retaining Copilot's verified differences: safe reasoning metadata, Copilot error text, thinking-shaped `text_delta`, no invented input usage, and stopped-block cleanup. Payload building and non-stream response normalization stay Copilot-owned because their system, media, tool, reasoning, and usage filtering differs materially; Copilot therefore does not compose the full `AnthropicCompatibleAdapter`. Usage normalization reuses the compatible wire's `apply_anthropic_cache_usage`: cache read/write counts become `cache_read_tokens`/`cache_write_tokens` and are added onto `input_tokens` (non-stream and `message_start`). Thinking is policy-driven: an adaptive-thinking model maps an effort to `thinking: {type: adaptive, display: summarized}` (+ `output_config.effort` when efforts are allowed); a **budget-only Claude** (thinking budget, no adaptive thinking) derives a native `thinking: {type: enabled, budget_tokens}` from the effort via the shared `effort_to_budget` (ceiling = `max_thinking_budget`), clamped into the policy's `[min_thinking_budget, max_thinking_budget]` and kept strictly under the resolved `max_tokens`.
- `ws:/responses` can appear in catalog metadata but is ignored; websocket Responses frames are not implemented.
- Every inference request carries the configured editor/integration attribution plus `Openai-Intent: conversation-edits`; the first user request is marked `x-initiator: user`, while a request after Tool Results is marked `x-initiator: agent`. Requests whose wire payload contains an image also carry `Copilot-Vision-Request: true`; the header is rebuilt with auth on every retry.

Endpoint selection uses sanitized model metadata first:

- Claude-like models prefer `/v1/messages` when advertised.
- Gemini-like models stay chat-first when chat is advertised.
- OpenAI/GPT-like models prefer `/responses` when advertised.
- Unknown or metadata-poor models default to `/chat/completions`, with no explicit reasoning/thinking controls, no tools, and no structured-output controls unless exact fallback facts say otherwise.

## Runtime Policy

- `GitHubCopilotAdapter` receives provider-scoped `model_lookup` and computes policy once per send/stream request.
- `metadata.github_copilot` is the primary runtime source for vendor, supported endpoints, reasoning efforts, thinking budget bounds, adaptive thinking, tools, streaming, and structured output.
- **Family is `Model.family`, not a name guess.** `_policy_for_model` passes `model.family` into `copilot_model_policy`; the policy's endpoint *decision* (Claude→`/v1/messages`, GPT→`/responses`, Gemini→chat-first) reads that data-driven lineage. `Model.family` wins over the metadata's own `family`; it falls back to `metadata.github_copilot.family` only when `Model.family` is empty (e.g. a catalog not yet regenerated with the top-level family — `github-copilot.json` is blocked on OAuth). The `family_or_model` property still falls back to the model id only when family AND version are both unknown.
- Static fallback facts and exact-model overrides in `github_copilot_policy.py` cover validated quirks only.
- Unsupported optional features are omitted rather than sent optimistically. For `/responses`, `temperature` is omitted unless policy explicitly proves support.
- **Reasoning replay:** endpoint family alone is not evidence. Exact verified Model profiles select `full_history`: `gpt-5-mini` on `/responses` and `claude-sonnet-4.6` on `/v1/messages`; every other Copilot Model defaults to `current_run` until its live contract is verified. Live probe (2026-06-13): `/responses` accepted replayed reasoning items including `encrypted_content` across a Run boundary (`gpt-5-mini`, 200); `/v1/messages` accepted a replayed signed `thinking` block across a Run boundary (`claude-sonnet-4.6`, 200; `claude-haiku-4.5` returned no thinking blocks under `thinking: enabled`, so there was nothing to replay).

## Catalog Normalization

- Discovery retains the full raw `/models` response but skips entries with `model_picker_enabled: false`, a non-chat `capabilities.type`, or no implemented HTTP chat endpoint (`/chat/completions`, `/responses`, `/v1/messages`). This prevents utility, embedding, internal duplicate, and websocket-only entries from becoming selectable Chat Models.
- Exact officially retired wire ids that remain in an older captured catalog are `catalog_exclusions`: `gemini-2.5-pro`, `gemini-3-flash-preview`, `gpt-4.1`, and `gpt-4.1-2025-04-14`. The current account catalog remains the authority for plan/organization entitlement; vBot does not invent aliases or silently fall back to replacement Models.
- `normalize_catalog_entry()` reads Copilot `capabilities.limits.max_context_window_tokens`, `capabilities.limits.max_output_tokens`, and `capabilities.supports`.
- `max_prompt_tokens` is preserved as Copilot runtime metadata and enforced separately from the total context/output clamp before any request. Vision metadata preserves the advertised MIME allowlist, per-image byte ceiling, and per-request image count; the Adapter narrows Chat media support and fails locally when those limits are exceeded.
- Missing or non-numeric `max_output_tokens` is stored as `null`; reported numeric values remain authoritative even when small.
- Reasoning is supported when Copilot advertises reasoning-effort values or thinking-budget bounds. The catalog projects exact effort ladders or the maximum native thinking budget into typed `ReasoningCapabilities`, while Copilot runtime metadata retains endpoint-specific controls.
- Normalized `Model.capabilities` includes chat-oriented modality/task defaults plus Copilot-advertised vision/tools/structured-output/reasoning facts.
- Only sanitized runtime metadata is stored under `metadata.github_copilot`; raw provider data, policy terms, picker flags, and credentials are not stored.

## Usage Probe (`copilot_internal/user`)

The Copilot usage fetcher in `core/providers/usage.py` (see `providers/usage.md`). **Blind, best-effort** — implemented from openclaw's verified field names, not yet live-verified (no Copilot login in this environment):

- `GET https://api.github.com/copilot_internal/user` — GitHub's host, NOT the Copilot
  API host. Authenticates with `Authorization: token <github_oauth_token>` (the GitHub
  OAuth token from token-store `extra.github_oauth_token`, **not** the exchanged Copilot
  bearer), plus `Accept: application/json`, `Copilot-Integration-Id`, `Editor-Version`.
  Missing `github_oauth_token` → snapshot error "Reconnect required".
- Expected body: `quota_snapshots.{premium_interactions,chat}.percent_remaining`
  (→ window `used = 100 - percent_remaining`, labels `Premium` / `Chat`),
  `copilot_plan` → plan, `quota_reset_date` → each window's reset. Missing/unknown
  snapshots yield empty windows (dropped), never a crash.

## Constraints & Gotchas

- Exact-model quirks belong in `core/providers/github_copilot_policy.py`, not hand-edited `resources/models/github-copilot.json`.
- Copilot Responses tool calls may use nested `function.{name,arguments}`; helper code must preserve a non-empty name from either top-level or nested fields.
- All three wire builders carry user image `media` blocks for vision models: `/chat/completions` via the inherited OpenAI-compatible path (`image_url`), `/v1/messages` as an Anthropic-style `image`/`source` block, `/responses` as an `input_image` data-URI part. Non-image media raises `ProviderError` rather than being dropped. A new wire builder must translate `media` blocks too — the vision-capability gate (`block_resolver.py`, `chat.md`) only resolves images for vision models, so by the time a `media` block reaches a wire builder it must be sent, never silently filtered to text.
- Partial metadata stays conservative: omit uncertain controls instead of forwarding them.
- Token values must never be logged.
- GitHub officially documents current Model availability, retirements, plan/client restrictions, configurable reasoning, token types for Copilot SDK/CLI, and the Copilot domain allowlist; the direct generation endpoints and token-exchange response schema remain private backend contracts. Without a live Copilot Account, current endpoint payload behavior, Enterprise endpoint rotation, entitlements, and rate-limit/error bodies remain unproved and must stay in `.vorch/FLAGGED.md`.
