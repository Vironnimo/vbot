# Anthropic Provider

Anthropic Messages API adapter and Anthropic-style request/response normalization.

## Interfaces

- Provider config: `resources/providers/anthropic.json`
- Adapter selector: `anthropic`
- Adapter class: `AnthropicAdapter`
- Runtime endpoint: `POST /messages`
- Auth/header shape: usually `x-api-key` with no Bearer prefix, plus `anthropic-version: 2023-06-01` and configured extra headers.

## Wire Contract

- System-role messages are removed from the conversation array and merged into top-level `system`. Multiple string system messages are joined with blank lines; system content blocks are concatenated as blocks.
- User content uses Anthropic content blocks. Consecutive canonical `tool` messages become one user message containing multiple `tool_result` blocks.
- Canonical assistant tool calls become `tool_use` blocks. Provider tool definitions become Anthropic `tools` entries with `input_schema`.
- Anthropic SSE uses framed `event:`/`data:` events; consume complete SSE data payloads rather than parsing it as OpenAI-style line JSON.

## Reasoning

- `thinking_effort: none` sends `thinking: {type: disabled}`. Active Anthropic efforts send adaptive thinking, summarized display, and `output_config.effort` for efforts above `minimal`.
- Anthropic rejects a sampling `temperature` while thinking is active. When the outgoing request activates thinking (adaptive via effort, or a raw `thinking` kwarg with type `adaptive`/`enabled`), `_build_payload` drops the caller `temperature` and skips the provider-default `temperature`. `thinking: {type: disabled}` does not conflict — temperature stays.
- If injected `model_lookup` says reasoning is unsupported, Anthropic thinking/reasoning controls are stripped.
- Opaque `thinking` and `redacted_thinking` blocks from provider responses are preserved under `reasoning_meta.content_blocks` and may be resent for the active tool-use continuation.
- Plain readable `reasoning` text without opaque metadata is not converted into Anthropic thinking blocks.

## Response Normalization

- `text` blocks concatenate into `content`.
- Readable `thinking` blocks concatenate into visible `reasoning`; redacted thinking remains opaque metadata only.
- `tool_use` blocks map to canonical `tool_calls`.
- Streaming tracks content-block indexes and yields normalized vBot deltas only.

## Error Classification

- 401/403 -> `ProviderAuthError`
- 429 -> `ProviderRateLimitError`
- 529, 502, 503 -> retryable provider overload errors
- Other errors -> non-retryable `ProviderError`

## Constraints & Gotchas

- Current-turn reasoning metadata may be resent for tool-use continuation, but stale completed-turn metadata must not be sent on later turns.
- Preserve Anthropic signatures and redacted thinking bytes unchanged; vBot never interprets their contents.
- Keep Anthropic protocol behavior in `AnthropicAdapter` or provider-specific wrappers such as `OpenCodeGoAdapter`; do not add Anthropic content-block rules to the generic OpenAI-compatible adapter.
