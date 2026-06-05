# OpenCode Go Provider

OpenAI-compatible gateway with provider-specific reasoning replay and a small set of Anthropic-routed models.

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

- Anthropic-routed models apply `_bound_assistant_reasoning_replay()` before request building, send, and stream.
- That helper strips stale completed-turn `reasoning` and `reasoning_meta` from history, but preserves the active continuation turn: the latest assistant tool-call message followed only by tool results and optional synthetic system-reminder user messages.
- OpenAI-routed models replay visible assistant reasoning for every historical assistant message because the gateway expects `reasoning_content` round-tripping.

## Constraints & Gotchas

- Keep provider-specific replay behavior in `OpenCodeGoAdapter`; do not add it to the generic OpenAI-compatible adapter.
- `normalize_response()` delegates to the Anthropic normalizer only for non-OpenAI-shaped responses.
- Constructor signature intentionally matches runtime adapter factory injection, including optional `model_lookup` and `debug_recorder`.
