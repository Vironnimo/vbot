# OpenAI Provider

Direct OpenAI provider configuration and the generic OpenAI-compatible adapter behavior used by fully compatible providers.

## Interfaces

- Provider config: `resources/providers/openai.json`
- Adapter selector: `openai_compatible`
- Adapter class: `OpenAICompatibleAdapter`
- Runtime endpoint: `POST /chat/completions`
- Catalog endpoint: provider `/models` when configured.

## Wire Contract

- Canonical system/user/assistant messages stay in the OpenAI-style `messages` array.
- Canonical `tool` messages become OpenAI `role: tool` messages with `tool_call_id`.
- Canonical assistant `tool_calls` become OpenAI `tools`/function-call structures.
- Provider tool definitions become `{"type":"function","function":{...}}` entries.
- Streaming uses `stream: true` and SSE `data:` lines ending with `[DONE]`.

## Reasoning

- vBot `thinking_effort` maps to nearest safe `reasoning_effort`: `minimal -> low`, `low/medium/high` stay exact, `xhigh/max -> high`.
- Generic OpenAI-compatible gateways omit explicit `none`; the direct OpenAI provider may send `none` only when catalog data confirms support.
- If `model_lookup` says reasoning is unsupported, reasoning controls are stripped.

## Response Normalization

- Text becomes `content` or `content_delta`.
- Reasoning fields such as `reasoning_content`/`thinking` become visible `reasoning`/`reasoning_delta`.
- Opaque reasoning fields such as `encrypted_content`/`reasoning_details` stay in `reasoning_meta` for round-tripping.
- Tool-call argument JSON that is malformed normalizes to an empty argument object instead of leaking parser exceptions.
- Usage chunks are requested with `stream_options: { include_usage: true }` during streaming.

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
- Subclass `OpenAICompatibleAdapter` only when runtime behavior, streaming, reasoning, or catalog normalization differs.
