# OpenAI Provider

Direct OpenAI Platform provider configuration plus the generic OpenAI-compatible `/chat/completions` adapter used by fully compatible providers.

## Interfaces

- Provider config: `resources/providers/openai.json`
- Adapter selector: `openai_compatible`
- Adapter class: `OpenAICompatibleAdapter`
- Runtime endpoint: `POST /chat/completions`
- Connections: `openai:oauth` uses the static `OPENAI_OAUTH_TOKEN` credential stub; `openai:api-key` uses `OPENAI_API_KEY`. Neither connection is the ChatGPT subscription Codex Device Flow; that lives in `openai-subscription`.
- Catalog: direct OpenAI currently has no bundled `models_endpoint`; the checked-in catalog is a resource artifact unless the config gains refresh metadata.

## Wire Contract

- Canonical system/user/assistant messages stay in the OpenAI-style `messages` array.
- Canonical `tool` messages become `role: tool` messages with `tool_call_id`.
- Canonical assistant `tool_calls` become OpenAI function-call structures.
- Provider tool definitions become `{"type":"function","function":{...}}` entries.
- Streaming uses `stream: true`, SSE `data:` frames, `[DONE]`, and `stream_options.include_usage: true`; caller-provided `stream_options` are preserved except `include_usage` is forced true.

## Reasoning

- vBot `thinking_effort` and raw `reasoning_effort` map to the nearest safe OpenAI effort: `minimal -> low`, `low/medium/high` stay exact, `xhigh/max -> high`.
- Generic OpenAI-compatible gateways omit explicit `none`; the direct OpenAI provider may send `none` only when catalog data confirms reasoning support.
- If injected `model_lookup` says reasoning is unsupported, reasoning request controls are stripped.
- Opaque reasoning fields such as `encrypted_content` and `reasoning_details` stay in `reasoning_meta` for round-tripping.

## Response And Catalog Normalization

- Text becomes `content` or `content_delta`; provider reasoning text fields such as `reasoning_content`/`thinking` become visible `reasoning`/`reasoning_delta`.
- Malformed tool-call argument JSON is ignored for that tool call instead of becoming fake empty arguments; valid sibling tool calls are preserved.
- Generic `/models` entries may expose modalities, supported parameters, context windows, and output limits through raw fields, `architecture`, or `top_provider`. Normalize discoverable facts into `Model.capabilities` and `Model.metadata`; do not treat sparse catalogs as negative evidence for every missing capability.
- Missing per-model output-token limits remain `max_output_tokens: null`; request fallback limits come from provider defaults such as `max_tokens: 8192`.

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
- This spec describes the generic OpenAI-compatible chat adapter. Provider-specific Responses APIs, Anthropic-compatible endpoints, reasoning protocols, or catalog quirks belong in a subclass and its child spec.
