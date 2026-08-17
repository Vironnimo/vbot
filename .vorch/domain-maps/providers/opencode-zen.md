# OpenCode Zen Provider

OpenCode Zen is a hosted gateway whose one Model namespace spans OpenAI Responses, Anthropic Messages, OpenAI Chat Completions, and native Gemini `generateContent`; `OpenCodeZenAdapter` owns that exact per-Model routing policy.

## Interfaces

- Provider config: `resources/providers/opencode-zen.json`
- Adapter selector and owner: `opencode_zen` / `core/providers/opencode_zen.py::OpenCodeZenAdapter`
- Connections: `opencode-zen:api-key` and `opencode-zen:account`
- Discovery: public `GET https://opencode.ai/zen/v1/models`, normalized by `OpenCodeZenAdapter` and enriched from the models.dev `opencode` section
- Generated inspection/projection: `resources/models/opencode-zen.raw.json` and `resources/models/opencode-zen.json`

## Connections and Authentication

- `api-key` reads only `OPENCODE_API_KEY`; it never infers Connection type from credential text. The default request auth is `Authorization: Bearer`, while the Adapter replaces it with the exact header required by Messages (`x-api-key`) or Gemini (`x-goog-api-key`).
- `account` uses OpenCode's current JSON Device Flow at `https://console.opencode.ai/auth/device/code` and `https://console.opencode.ai/auth/device/token` with client id `opencode-cli`. Polling accepts the standard `authorization_pending` response, and refresh POSTs JSON and persists the rotated access/refresh pair. An authentication failure quarantines the rotating token chain; an ambiguous transport failure is not replayed automatically.
- Both Connections expose the same Zen catalog and base URL. Account identity changes only the credential slot and never the Model id, endpoint family, or catalog policy.

## Exact Model Routing

- `metadata.opencode_zen.protocol` is mandatory and has one of `responses`, `messages`, `chat_completions`, or `gemini_generate_content`. `send`, `stream`, response normalization, and media support resolve that metadata; replay scope separately follows the shared Model → Provider → system hierarchy. Unknown or vendor-prefixed ids fail locally; vBot never guesses by prefix, silently aliases an id, or falls back to another Model.
- Responses Models use `POST /responses` through the deep `OpenAIAdapter`; the Provider override hook classifies Zen errors on both non-streaming and streaming Responses paths.
- Messages Models use `POST /messages` through a borrowed `AnthropicCompatibleAdapter`, `x-api-key`, `anthropic-version: 2023-06-01`, non-strict Tool schemas, PDF/images, prompt-caching breakpoints, and the outer connection-scoped HTTP client/token getter.
- Chat Models use `POST /chat/completions` through `OpenAICompatibleAdapter` with bearer auth and the shared Tool, usage, terminal-outcome, and SSE contracts.
- Gemini Models use `POST /models/{model}:generateContent` and `POST /models/{model}:streamGenerateContent?alt=sse`. The Provider Adapter owns native `systemInstruction`, `contents`, `generationConfig`, Function declarations/calls/responses, inline media, usage, finish reasons, and opaque thought-signature replay.

OpenCode's gateway may fail over between upstream suppliers serving the same requested Model. That is same-Model infrastructure routing, not a vBot alias or cheaper-Model fallback, and vBot must continue sending the exact selected id.

## Catalog Policy

- The public endpoint returns ids but no endpoint family or trustworthy capability/limit detail. Normalization therefore admits only exact ids in the reviewed protocol table and fills context/output limits, modalities, family, Tool support, and reasoning controls from the exact models.dev `opencode` entry. New endpoint ids remain in the raw response and unusable until their official route is reviewed and added.
- Retired Models are rejected by the Adapter and repeated in `catalog_exclusions` so raw inspection keeps them while the usable projection omits them. As of 2026-08-04 these are `gpt-5.2-codex`, `gpt-5.1-codex`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini`, `gpt-5-codex`, `claude-sonnet-4`, and `glm-5`.
- `claude-opus-4-1`, `minimax-m2.5`, and `kimi-k2.5` remain usable on 2026-08-04 but carry `metadata.opencode_zen.deprecates_at: 2026-08-05`. Refresh or remove them after that boundary; do not silently retarget stored selections.
- Free Models carry `privacy: free_model_data_collection`. This metadata is policy evidence and must remain separate from wire capability.

## Reasoning, Replay, and Prompt Caching

- Responses, Messages, Chat, and Gemini inherit the shared `full_history` replay default unless an explicit top-level override narrows it. Gemini uses the Model's exact models.dev effort ladder, maps Agent effort to the closest supported level, and requests visible thoughts because Google requires opaque `thoughtSignature` parts to accompany later turns and Function responses. Legacy `metadata.opencode_zen.reasoning_replay` values in an older generated snapshot are ignored by runtime policy and disappear on the next catalog refresh.
- Gemini normalization persists the complete native parts in `reasoning_meta.gemini_parts`; replay sends those parts unchanged so signatures are not reconstructed or lost. Streaming emits progressively complete reasoning metadata before the terminal outcome. A stream without a Gemini `finishReason` is an unknown transport outcome and fails rather than inventing success.
- Messages enables explicit compatible cache breakpoints. Responses and Chat retain their shared automatic-cache accounting. Gemini reports `cachedContentTokenCount` as `cache_read_tokens`; vBot does not invent a `cachedContent` resource or claim cache creation when Zen returned none. Live cache effectiveness remains Provider/upstream-dependent.

## Limits, Sampling, Tools, and Media

- Request output is clamped to the selected Model's catalog ceiling and remaining context. Gemini accepts exactly one effective output field and emits native `maxOutputTokens`; duplicate aliases collapse to the smallest valid value.
- Gemini validates `temperature` 0–2, `top_p` 0–1, positive integer `top_k`, penalties -2–2, integer seed, stop strings, supported JSON response formats, and Tool choice locally. Unknown request fields fail locally instead of leaking OpenAI-only parameters onto the Google wire. `parallel_tool_calls` is intentionally omitted because native Gemini controls parallel Function Calls without that OpenAI flag.
- Gemini supports inline PNG/JPEG/WEBP/HEIC/HEIF images, documented audio/video types, and PDF. The Adapter enforces Google's 20 MB inline request limit and 3,600-image maximum before network I/O. Responses/Chat declare only their established image wire; Messages declares images and PDF. Catalog modalities still intersect these wire ceilings at Chat ingress.
- All four wires preserve canonical non-strict Tool schemas and normalize Tool Calls, usage, and one terminal outcome. Gemini maps STOP, MAX_TOKENS, safety/content blocks, malformed/unexpected Tool Calls, and unknown reasons without treating every transport completion as successful generation.

## Errors, Regions, and Privacy

- Zen `AuthError` is reconnectable authentication failure. `CreditsError`, `MonthlyLimitError`, `UserLimitError`, and `ModelError` are fatal account/entitlement/Model-access failures even though Zen returns HTTP 401 for them. `RegionError` is fatal HTTP 403.
- Stable allowance exhaustion markers such as `FreeUsageLimitError`, `GoUsageLimitError`, `BlackUsageLimitError`, monthly/weekly usage limits, and quota exhaustion make HTTP 429 fatal. A genuine burst `RateLimitError` remains retryable and honors `Retry-After` through the shared bounded retry policy.
- OpenCode documents Zen hosting in the United States. Standard paid requests are described as zero-retention except that OpenAI- and Anthropic-routed requests may be retained for 30 days; free Models permit data collection. Treat these as product/privacy constraints, not Adapter behavior, and re-check them before making deployment or compliance promises.

## Constraints & Gotchas

- Do not replace this composite Adapter with a generic OpenAI-compatible JSON entry: header selection, Gemini translation/signature replay, Messages caching, terminal outcomes, exact routing, and Zen error semantics are genuine Provider policy.
- Do not inherit OpenAI Codex discovery query parameters or ChatGPT account headers merely because the outer Adapter extends `OpenAIAdapter`; Zen explicitly overrides discovery parameters and headers.
- The public catalog proves presence, not entitlement, regional availability, Tool correctness, caching, or successful inference. Tests use mocked provider responses; `.vorch/FLAGGED.md` owns the remaining credentialed live probes.

## Official Sources

- Zen Models, endpoints, deprecations, privacy, and operational policy: https://opencode.ai/docs/zen/
- Public current Model listing: https://opencode.ai/zen/v1/models
- Account Device Flow and gateway handler behavior: current `anomalyco/opencode` source under `packages/core/src/plugin/provider/opencode.ts` and `packages/console/app/src/routes/zen/v1/`
- Gemini `generateContent`, thinking signatures, Function Calling, and media limits: https://ai.google.dev/api/generate-content and the linked official Gemini guides
