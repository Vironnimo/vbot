# GitHub Copilot Provider

GitHub Copilot provider with OAuth Device Flow, endpoint-aware runtime policy, and Copilot-specific catalog metadata.

## Interfaces

- Provider config: `resources/providers/github-copilot.json`
- Adapter selector: `github_copilot`
- Adapter class: `GitHubCopilotAdapter`
- OAuth connection: `github-copilot:oauth`
- Wire profile: `resources/wire/github-copilot.json` selects each Model's protocol and holds the request quirks.
- Runtime helpers: `github_copilot_responses.py` (the shared Responses codec) and `github_copilot_messages.py` build and normalize non-chat endpoint payloads; `_responses_profile.py` derives the Responses request policy and the kwarg filter from the profile and the catalog Model.

## OAuth

- Device Flow scope is `read:user` using GitHub's standard device-code endpoint.
- After GitHub OAuth, vBot exchanges the GitHub OAuth token for a Copilot API token with `Authorization: Bearer <github_oauth_token>`, `Accept: application/json`, `Copilot-Integration-Id: vscode-chat`, and the minimum current client gate `Editor-Version: vscode/1.128.0`.
- `TokenStore` persists the Copilot API token as `access_token`, expiry as `expires_at`, and the GitHub OAuth token in `extra.github_oauth_token` so `OAuthTokenGetter` can refresh by repeating the Copilot token exchange. Numeric Unix and ISO-8601 exchange expiries are accepted.
- Token exchange may return an account-specific `endpoints.api`; vBot stores only a validated official HTTPS GitHub Copilot/GHE endpoint as `extra.copilot_api_endpoint`, with the exchanged token's `proxy-ep` as fallback, and uses that endpoint for both inference and Connection-scoped discovery on the next Adapter/refresh construction. This is required for Enterprise/proxied Accounts; an untrusted host is ignored.
- Do not use GitHub's older `Authorization: token ...` scheme for Copilot exchange.

## Endpoint Policy

- `/chat/completions`: conservative fallback through `OpenAICompatibleAdapter` after the Adapter drops kwargs the catalog Model does not support (Tools, parallel Tool calls, structured output), optional parameters outside the profile's `allowed_parameters` (`max_tokens`, `temperature`, `top_p`), and raw reasoning fields.
- `/responses`: OpenAI Responses-like helper for output items, function calls, usage, reasoning metadata, readable reasoning summaries, and semantic SSE events. Every function Tool carries `strict: false`; GitHub does not publish this private backend's default, so the shared Responses builder prevents an omitted field from inheriting OpenAI Responses' automatic strict normalization. The helper stores the complete original output-item sequence and assistant `phase`, then replays those items instead of reconstructing reasoning; only their readable text has look-alike System Reminder tags neutralized (`chat/request-building.md`). Exact structured in-band error codes enter the shared Provider taxonomy and Chat recovery budget; retryable failures restart only before visible output, while incomplete responses finish with a safe non-Tool terminal outcome. Usage normalization maps `input_tokens_details.cached_tokens` (or `prompt_tokens_details`) to canonical `cache_read_tokens`; cached tokens are already included in the wire's input count.
- `/v1/messages`: Anthropic Messages-like helper for Claude-style models, content blocks, tools, thinking/output config, and SSE normalization. The stream path delegates its content-block/event state machine to the reusable wire's `AnthropicMessagesStreamDecoder` while retaining Copilot's verified differences: safe reasoning metadata, Copilot error text, thinking-shaped `text_delta`, no invented input usage, and stopped-block cleanup. Payload building and non-stream response normalization stay Copilot-owned because their system, media, tool, reasoning, and usage filtering differs materially; Copilot therefore does not compose the full `AnthropicCompatibleAdapter`. Usage normalization reuses the compatible wire's `apply_anthropic_cache_usage`: cache read/write counts become `cache_read_tokens`/`cache_write_tokens` and are added onto `input_tokens` (non-stream and `message_start`). Thinking is planned by the Model's wire profile (`ReasoningWire.plan()` against the resolved `max_tokens`) and spelled by the `anthropic_thinking` dialect: an effort-ladder Model gets `thinking: {type: adaptive, display: summarized}` + `output_config.effort`; a **budget-only Claude** (catalog thinking budget, no effort ladder) gets native `thinking: {type: enabled, budget_tokens}` from the effort scaled by the catalog's maximum thinking budget, kept strictly under `max_tokens`, and no thinking for `none` (rule `control: budget` -> `reasoning.off: omit`). `claude-haiku-4.5` is set to on/off with `reasoning.options.adaptive_on`, so its active efforts send adaptive thinking without an effort. Sampling parameters are sent even while thinking, except `temperature` on `claude-sonnet-4.6` (`drop_while_thinking`).
- `ws:/responses` can appear in catalog metadata but is ignored; websocket Responses frames are not implemented.
- Every inference request carries the configured editor/integration attribution plus `Openai-Intent: conversation-edits`; the first user request is marked `x-initiator: user`, while a request after Tool Results is marked `x-initiator: agent`. Requests whose wire payload contains an image also carry `Copilot-Vision-Request: true`; the header is rebuilt with auth on every retry.

Endpoint selection is a set of protocol rules in `resources/wire/github-copilot.json` over the sanitized `metadata.github_copilot` (`supported_endpoints`, `vendor`) and the Model id; a later matching rule wins, so the vendor preferences rank Anthropic over Google over OpenAI:

- Without a vendor preference the first advertised wire wins in the order `/chat/completions`, `/v1/messages`, `/responses`.
- Vendor `OpenAI` or `Azure OpenAI`, or an id starting `gpt-`/`gpt_`/`o1`/`o3`/`o4`, prefers `/responses` when advertised.
- Vendor `Google`, or an id starting `gemini`, prefers `/chat/completions` when advertised.
- Vendor `Anthropic`, or an id starting `claude`, prefers `/v1/messages` when advertised.
- A Model the catalog does not know gets `/chat/completions` with no reasoning controls, no Tools, and no structured-output controls (rule `unknown: true`); `gpt-5-mini` keeps Tools, structured output, and `low`/`medium`/`high` `reasoning_effort` there.

## Runtime Policy

- `GitHubCopilotAdapter` receives provider-scoped `model_lookup` and resolves the Model's wire profile per send/stream request; `send`/`stream` dispatch on `profile.protocol` only, and the Messages and Responses builders take that same profile.
- `metadata.github_copilot` carries the vendor and supported endpoints the rules match, plus vision limits and `max_prompt_tokens`. Tool, parallel-Tool, and structured-output support come from the normalized catalog capabilities; reasoning comes from the catalog's typed `ReasoningCapabilities` (effort ladder or maximum thinking budget) through the wire profile.
- **Routing reads reported facts, not a family guess.** The rules match the exact reported vendor and otherwise an id prefix; `Model.family` and the metadata's `family`/`version` take no part. Every bundled catalog Model reports one of the four matched vendors.
- Model entries and rules in `resources/wire/github-copilot.json` cover the known quirks only: Haiku 4.5 adaptive thinking without an effort, Gemini 3.1 Pro Preview without `reasoning_effort`, Sonnet 4.6 `temperature` dropped while thinking, and the `gpt-5-mini` fallback.
- Send and stream on all three wires run through the shared rejection learning (`providers/request-policy.md` -> HTTP, retry, and streaming): `/chat/completions` through the inherited compatible base, `/responses` and `/v1/messages` around the Copilot builders. The `claude-sonnet-4.6` Model entry outranks a learned `temperature` rejection: the retry drops the parameter, but later requests send it again unless thinking is active (`test_wire_learning.py`).
- Unsupported optional features are omitted rather than sent optimistically. For `/responses`, the optional parameters are `max_tokens`, `max_output_tokens`, and `top_p` (`allowed_parameters`), so `temperature` is not sent, and effort `none` omits `reasoning`.
- **Reasoning replay:** every Copilot Model inherits the shared `full_history` default unless a future top-level Model override demonstrates a narrower requirement; endpoint family no longer selects scope. Endpoint mechanics still differ: `/responses` round-trips complete output items and `/v1/messages` round-trips signed thinking blocks. Live probe (2026-06-13): `/responses` accepted replayed reasoning items including `encrypted_content` across a Run boundary (`gpt-5-mini`, 200); `/v1/messages` accepted a replayed signed `thinking` block across a Run boundary (`claude-sonnet-4.6`, 200; `claude-haiku-4.5` returned no thinking blocks under `thinking: enabled`, so there was nothing to replay).

## Catalog Normalization

- Discovery retains the full raw `/models` response but skips entries with `model_picker_enabled: false`, a non-chat `capabilities.type`, or no implemented HTTP chat endpoint (`/chat/completions`, `/responses`, `/v1/messages`). This prevents utility, embedding, internal duplicate, and websocket-only entries from becoming selectable Chat Models.
- Exact officially retired wire ids that remain in an older captured catalog are `catalog_exclusions`: `gemini-2.5-pro`, `gemini-3-flash-preview`, `gpt-4.1`, and `gpt-4.1-2025-04-14`. The current account catalog remains the authority for plan/organization entitlement; vBot does not invent aliases or silently fall back to replacement Models.
- `normalize_catalog_entry()` reads Copilot `capabilities.limits.max_context_window_tokens`, `capabilities.limits.max_output_tokens`, and `capabilities.supports`.
- `max_prompt_tokens` is preserved as Copilot runtime metadata and enforced separately from the total context/output clamp before any request. Vision metadata preserves the advertised MIME allowlist, per-image byte ceiling, and per-request image count; the Adapter narrows Chat media support, exposes positive per-image bytes through `image_size_limit(model_id)` for preparation, and retains local final-payload validation for byte/count/type limits.
- Missing or non-numeric `max_output_tokens` is stored as `null`; reported numeric values remain authoritative even when small.
- Reasoning is supported when Copilot advertises reasoning-effort values or thinking-budget bounds. The catalog projects exact effort ladders or the maximum native thinking budget into typed `ReasoningCapabilities`, while Copilot runtime metadata retains endpoint-specific controls.
- Normalized `Model.capabilities` includes chat-oriented modality/task defaults plus Copilot-advertised vision/tools/structured-output/reasoning facts.
- Only sanitized runtime metadata is stored under `metadata.github_copilot`; raw provider data, policy terms, picker flags, and credentials are not stored.

## Usage Probe (`copilot_internal/user`)

The Copilot usage fetcher in `core/providers/usage.py` (see `providers/usage.md`). **Blind, best-effort** - implemented from openclaw's verified field names, not yet live-verified (no Copilot login in this environment):

- `GET https://api.github.com/copilot_internal/user` - GitHub's host, NOT the Copilot
  API host. Authenticates with `Authorization: token <github_oauth_token>` (the GitHub
  OAuth token from token-store `extra.github_oauth_token`, **not** the exchanged Copilot
  bearer), plus `Accept: application/json`, `Copilot-Integration-Id`, `Editor-Version`.
  Missing `github_oauth_token` -> snapshot error "Reconnect required".
- Expected body: `quota_snapshots.{premium_interactions,chat}.percent_remaining`
  (-> window `used = 100 - percent_remaining`, labels `Premium` / `Chat`),
  `copilot_plan` -> plan, `quota_reset_date` -> each window's reset. Missing/unknown
  snapshots yield empty windows (dropped), never a crash.

## Constraints & Gotchas

- Exact-model quirks belong in `resources/wire/github-copilot.json`, not hand-edited `resources/models/github-copilot.json`.
- Copilot Responses tool calls may use nested `function.{name,arguments}`; helper code must preserve a non-empty name from either top-level or nested fields.
- All three wire builders carry user image `media` blocks for vision models: `/chat/completions` via the inherited OpenAI-compatible path (`image_url`), `/v1/messages` as an Anthropic-style `image`/`source` block, `/responses` as an `input_image` data-URI part. Non-image media raises `ProviderError` rather than being dropped. A new wire builder must translate `media` blocks too - the vision-capability gate (`block_resolver.py`, `chat.md`) only resolves images for vision models, so by the time a `media` block reaches a wire builder it must be sent, never silently filtered to text.
- Partial metadata stays conservative: omit uncertain controls instead of forwarding them.
- Token values must never be logged.
- GitHub officially documents current Model availability, retirements, plan/client restrictions, configurable reasoning, token types for Copilot SDK/CLI, and the Copilot domain allowlist; the direct generation endpoints and token-exchange response schema remain private backend contracts. Without a live Copilot Account, current endpoint payload behavior, Enterprise endpoint rotation, entitlements, and rate-limit/error bodies remain unproved and must stay in `.vorch/FLAGGED.md`.
