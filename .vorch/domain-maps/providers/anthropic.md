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

- Reasoning is resolved through the shared `resolve_reasoning_intent(...)` (see `providers.md` → "Reasoning is one policy, many renders") and rendered onto Anthropic's `thinking` shape. `_apply_reasoning` snaps against the model's feed ladder or `ANTHROPIC_EFFORT_FLOOR` (every active effort, so the effort path is byte-identical), then `_render_reasoning` materializes the intent:
  - **effort** → `thinking: {type: adaptive, display: summarized}` plus `output_config.effort` for efforts above `minimal`.
  - **budget** (a `budget`-control Claude) → native `thinking: {type: enabled, budget_tokens: N}`, where `N` is the effort→budget mapping scaled by `budget_max` when seeded (else the absolute fallback ladder), clamped strictly under `max_tokens`.
  - **on** → enabled with the floor budget; skipped with a `warn` when even the floor cannot fit `max_tokens`.
  - **off** (`thinking_effort: none`) → `thinking: {type: disabled}`.
  - **default** (no effort selected) → `thinking` omitted.
- `budget_max` for the reachable budget Claudes is hand-seeded in `resources/models/anthropic.overrides.json` (the feed leaves it `None`); the numbers are conservative and **not live-verified** (no Anthropic credentials — see FLAGGED.md).
- Anthropic rejects a sampling `temperature` while thinking is active. When the outgoing request activates thinking (adaptive via effort, or a raw `thinking` kwarg with type `adaptive`/`enabled`), `_build_payload` drops the caller `temperature` and skips the provider-default `temperature`. `thinking: {type: disabled}` does not conflict — temperature stays.
- If injected `model_lookup` says reasoning is unsupported, Anthropic thinking/reasoning controls are stripped.
- **Replay policy:** `reasoning_replay_policy` returns `full_history` — persisted `reasoning`/`reasoning_meta` replay across runs for assistant entries that pass the chat layer's same-model gate (Anthropic guidance: thinking blocks go back unchanged for the whole same-model conversation; stripping risks signature/ordering 400s and provider-side prompt-cache misses). Cross-model entries are stripped by the gate; same-model reasoning-only turns stay in the request history.
- Opaque `thinking` and `redacted_thinking` blocks from provider responses are preserved under `reasoning_meta.content_blocks` and are resent for the active tool-use continuation and, via `full_history`, for prior same-model runs.
- **Thinking-disabled guard:** when the outgoing request explicitly disables thinking (`thinking: {type: disabled}`, e.g. from `thinking_effort: none`) or the catalog marks the model reasoning-unsupported, `_build_payload` strips replayed `reasoning_meta` thinking blocks; an assistant turn left without content blocks is dropped from the request (the wire rejects empty content arrays). An absent thinking parameter does **not** strip — omitting blocks is the risk, the server drops unusable ones.
- Live probe of API tolerance for replayed thinking blocks under explicitly disabled thinking was **not performed** (2026-06-13 — no Anthropic credentials in this environment; see FLAGGED.md). The guard above is the conservative default until probed.
- Plain readable `reasoning` text without opaque metadata is not converted into Anthropic thinking blocks.

## Response Normalization

- `text` blocks concatenate into `content`.
- Readable `thinking` blocks concatenate into visible `reasoning`; redacted thinking remains opaque metadata only.
- `tool_use` blocks map to canonical `tool_calls`.
- Streaming tracks content-block indexes and yields normalized vBot deltas only.
- Usage: Anthropic reports `cache_read_input_tokens`/`cache_creation_input_tokens` **separately** from `input_tokens`. `apply_anthropic_cache_usage()` maps them to canonical `cache_read_tokens`/`cache_write_tokens` and adds both onto `input_tokens` so the canonical value is the total prompt (non-stream and the stream `message_start` path). `github_copilot_messages` reuses this helper for Copilot's Anthropic-style wire.

## Error Classification

- 401/403 -> `ProviderAuthError`
- 429 -> `ProviderRateLimitError`
- 529, 502, 503 -> retryable provider overload errors
- Other errors -> non-retryable `ProviderError`

## Constraints & Gotchas

- Same-model thinking blocks replay across the whole conversation (`full_history` policy); the chat layer's same-model gate strips cross-model entries, and the thinking-disabled guard strips them at payload build. Do not re-add history-wide reasoning strips in the adapter — the chat layer owns history shaping.
- Preserve Anthropic signatures and redacted thinking bytes unchanged; vBot never interprets their contents.
- Keep Anthropic protocol behavior in `AnthropicAdapter` or provider-specific wrappers such as `OpenCodeGoAdapter`; do not add Anthropic content-block rules to the generic OpenAI-compatible adapter.
