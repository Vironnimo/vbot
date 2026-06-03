# OpenCode Go Provider

OpenAI-compatible provider with provider-specific reasoning replay and selected Anthropic-routed models.

## Interfaces

- Provider config: `resources/providers/opencode-go.json`
- Adapter selector: `opencode_go`
- Adapter class: `OpenCodeGoAdapter`
- Default runtime endpoint: OpenAI-compatible `/chat/completions`

## Runtime Behavior

- Internal assistant messages with non-empty `reasoning` are echoed on the wire as `reasoning_content`.
- `minimax-m2.7`, `minimax-m2.5`, and `qwen3.5-plus` route through an internal `AnthropicAdapter` to `POST /messages`.
- `qwen3.6-plus` is live-verified on `/chat/completions`; all other OpenCode Go models use the default OpenAI-compatible path.
- When a catalog entry has a known `max_output_tokens`, OpenCode Go uses it as
  the request `max_tokens` unless the caller supplied an explicit output limit.
  Unknown (`null`) catalog limits fall back to the provider request default.

## Reasoning Replay

- Anthropic-routed models apply `_bound_assistant_reasoning_replay()` before request building, send, and stream.
- The helper strips stale completed-turn reasoning from history but preserves the active continuation turn: the most recent assistant tool-call message followed by tool results and optional synthetic reminders.

## Constraints & Gotchas

- Keep provider-specific replay behavior in `OpenCodeGoAdapter`; do not add it to the generic OpenAI-compatible adapter.
- Constructor accepts the shared optional `model_lookup` even when current runtime behavior does not use it.
