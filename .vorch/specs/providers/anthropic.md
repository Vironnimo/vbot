# Anthropic Provider

Anthropic Messages API adapter and Anthropic-style request/response normalization.

## Interfaces

- Provider config: `resources/providers/anthropic.json`
- Adapter selector: `anthropic`
- Adapter class: `AnthropicAdapter`
- Runtime endpoint: `POST /messages`
- Required headers: provider auth header, `anthropic-version: 2023-06-01`, plus configured extra headers.

## Wire Contract

- System-role messages are extracted into top-level `system`.
- Messages use Anthropic content blocks, not flat OpenAI content strings.
- Consecutive canonical `tool` messages become one user message containing multiple `tool_result` blocks.
- Canonical assistant tool calls become `tool_use` blocks.
- Provider tool definitions become Anthropic `tools` entries with `input_schema`.

## Reasoning

- `AnthropicAdapter` accepts injected `model_lookup` and strips thinking controls for known non-reasoning models.
- Thinking controls may include `thinking.type`, `thinking.budget_tokens`, `output_config.effort`, and `thinking.display`.
- Opaque thinking/redacted-thinking blocks are preserved under `reasoning_meta.content_blocks` for round-tripping.

## Response Normalization

- `text` blocks concatenate into `content`.
- `thinking` blocks concatenate into visible `reasoning` when readable.
- `tool_use` blocks map to canonical `tool_calls`.
- Streaming tracks content-block indexes and yields normalized vBot deltas only.

## Error Classification

- 401/403 -> `ProviderAuthError`
- 429 -> `ProviderRateLimitError`
- 529, 502, 503 -> retryable provider overload errors
- Other errors -> non-retryable `ProviderError`

## Constraints & Gotchas

- Auth usually uses `x-api-key` with no Bearer prefix.
- Anthropic SSE uses both `event:` and `data:` lines; do not parse it as OpenAI-style `[DONE]` only.
- Current-turn reasoning metadata may be resent for tool-use continuation, but stale completed-turn metadata must not be sent on later turns.
