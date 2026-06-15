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
- **Protocol routing is DATA, not a hardcoded set.** The adapter routes each model by the per-model wire fact `metadata.opencode_go.protocol` (`"anthropic"` → internal Messages adapter, `"openai"`/anything-else → default OpenAI `/chat/completions`), resolved via the injected `model_lookup`. The endpoint returns bare ids with no protocol, so the facts live in the opencode-go **override** (`resources/models/opencode-go.overrides.json`), keyed by wire-id under `metadata.opencode_go.protocol`. A model the override does not mark (no metadata / no `protocol`) is **unknown**: it takes the safe OpenAI default AND the adapter logs a `warn` (`vbot.providers.opencode_go`) so a newly added model is never silently misrouted. The stale `_ANTHROPIC_MESSAGES_MODELS` frozenset was removed in Phase 5. Published protocol table (the override's source of truth): openai → `glm-5.1, glm-5, kimi-k2.7, kimi-k2.6, deepseek-v4-pro, deepseek-v4-flash, mimo-v2.5, mimo-v2.5-pro`; anthropic → `minimax-m3, minimax-m2.7, minimax-m2.5, qwen3.7-max, qwen3.7-plus, qwen3.6-plus`.
- The internal Anthropic adapter uses `x-api-key` with no prefix while sharing the selected runtime base URL, selected credential key, `model_lookup`, and debug recorder.
- When a catalog entry has a positive `max_output_tokens`, OpenCode Go uses it as request `max_tokens` unless the caller supplied `max_tokens`, `max_completion_tokens`, or `max_output_tokens`.

## Override Reconciliation (Phase 5)

The generated `opencode-go.json` carries the models.dev reasoning ladders but no context window — the bare endpoint reports none, so `context_window` is honestly `null` (Phase 6). The **override** supplies the missing base facts two ways: most models carry explicit hand `context_window`/`max_output_tokens` (the gateway's own limits, which may deviate from the lab), while `minimax-m3` / `qwen3.7-max` instead carry a `canonical` pointer and inherit `context_window`/`max_output_tokens`/`family` from the canonical base at load (the gateway matches the lab for those, so a pointer avoids duplicating the numbers). Every override entry also carries the per-model `metadata.opencode_go.protocol`. Per-model reasoning in the override:

- `deepseek-v4-flash` / `deepseek-v4-pro`: the override carries **no** `capabilities` block, so the generated `{control: levels, levels: [high, max]}` ladder is inherited at load. Effective `deepseek-v4-pro` reasoning == `{supported: true, control: "levels", levels: ["high", "max"]}`.
- The other override models: keep `capabilities.reasoning: {supported: true}` — a verified hand fact the bare endpoint and the models.dev feed (`reasoning: false`) lack. They snap against the adapter floor (empty `levels`), which is expected.
- `minimax-m3`: its `canonical` pointer joins `minimax/MiniMax-M3` (effective `context_window: 512000`, `on_off` reasoning), proven against the live gateway 2026-06-15. The canonical join only fills fields the provider layer leaves `null`/absent — the provider layer still wins every capability sub-field it defines (e.g. text-only modalities), because the at-load merge ignores a higher layer's `null` (fill, don't overwrite). The other generated-only models with no override (e.g. `kimi-k2.7-code`) are `context_window: null` → resolved read-side to the global floor, and route the OpenAI default + warn.

Since `metadata` is replaced **wholesale** by the highest layer at load (assembly contract), the override's `metadata.opencode_go` becomes the effective metadata.

## Reasoning Replay

- `reasoning_replay_policy()` returns `full_history` for every model id — both routes. The chat layer owns history shaping (same-model gate); the adapter no longer strips reasoning from history itself (`_bound_assistant_reasoning_replay` was retired in the Phase-3 rollout, 2026-06-13).
- Live probe against the real gateway (2026-06-13): the OpenAI route accepted `reasoning_content` on a completed historical assistant message across a run boundary (`deepseek-v4-flash`, 200), and the Anthropic route accepted a replayed signed `thinking` block across a run boundary (`minimax-m2.5`, 200).
- OpenAI-routed assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content` (the gateway expects round-tripping); `reasoning_meta` keys (`reasoning_details`, `encrypted_content`) are applied by the shared OpenAI-compatible formatter.
- Anthropic-routed models render replayed `reasoning_meta.content_blocks` through the inner `AnthropicAdapter`, including its thinking-disabled guard.

## Constraints & Gotchas

- Keep provider-specific reasoning wire behavior (the `reasoning_content` echo) in `OpenCodeGoAdapter`; do not add it to the generic OpenAI-compatible adapter. Do not re-add history-wide reasoning strips here — the chat layer owns history shaping.
- `normalize_response()` delegates to the Anthropic normalizer only for non-OpenAI-shaped responses.
- Constructor signature intentionally matches runtime adapter factory injection, including optional `model_lookup` and `debug_recorder`.
