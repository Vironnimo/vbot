# OpenCode Go Provider

OpenAI-compatible gateway with full-history reasoning replay and a small set of Anthropic-routed models.

## Interfaces

- Provider config: `resources/providers/opencode-go.json`
- Adapter selector: `opencode_go`
- Adapter class: `OpenCodeGoAdapter`
- Default runtime endpoint: OpenAI-compatible `POST /chat/completions`
- Alternate runtime endpoint for selected models: `POST /messages` through an internal `AnthropicAdapter`

## Runtime Behavior

- OpenAI-routed assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content`.
- Only `minimax-m2.7`, `minimax-m2.5`, and `qwen3.5-plus` route through the internal Anthropic Messages adapter. `qwen3.6-plus` and all other models use the default OpenAI-compatible path unless code adds them to the Anthropic routing set.
- The internal Anthropic adapter uses `x-api-key` with no prefix while sharing the selected runtime base URL, selected credential key, `model_lookup`, and debug recorder.
- When a catalog entry has a positive `max_output_tokens`, OpenCode Go uses it as request `max_tokens` unless the caller supplied `max_tokens`, `max_completion_tokens`, or `max_output_tokens`.

## Reasoning Replay

- `reasoning_replay_policy()` returns `full_history` for every model id — both routes. The chat layer owns history shaping (same-model gate); the adapter no longer strips reasoning from history itself (`_bound_assistant_reasoning_replay` was retired in the Phase-3 rollout, 2026-06-13).
- Live probe against the real gateway (2026-06-13): the OpenAI route accepted `reasoning_content` on a completed historical assistant message across a run boundary (`deepseek-v4-flash`, 200), and the Anthropic route accepted a replayed signed `thinking` block across a run boundary (`minimax-m2.5`, 200).
- OpenAI-routed assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content` (the gateway expects round-tripping); `reasoning_meta` keys (`reasoning_details`, `encrypted_content`) are applied by the shared OpenAI-compatible formatter.
- Anthropic-routed models render replayed `reasoning_meta.content_blocks` through the inner `AnthropicAdapter`, including its thinking-disabled guard.

## Constraints & Gotchas

- Keep provider-specific reasoning wire behavior (the `reasoning_content` echo) in `OpenCodeGoAdapter`; do not add it to the generic OpenAI-compatible adapter. Do not re-add history-wide reasoning strips here — the chat layer owns history shaping.
- `normalize_response()` delegates to the Anthropic normalizer only for non-OpenAI-shaped responses.
- Constructor signature intentionally matches runtime adapter factory injection, including optional `model_lookup` and `debug_recorder`.
