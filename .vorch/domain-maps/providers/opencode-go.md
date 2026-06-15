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

## Provider facts come from the models.dev section (rebuilt 2026-06-16)

The opencode-go endpoint returns **bare ids** — no context window, output cap, modalities, family, or reasoning info. But models.dev carries a per-provider **`opencode-go` section** with all of those, so refresh pulls them into the generated `opencode-go.json` (`discovery._enrich_provider_model` via `models_dev.provider_limits` / `provider_modalities` / `provider_family` / `provider_reasoning_supported`):

- **Limits**: the gateway's own `context.window` / `output` — which legitimately deviate from the lab (e.g. `glm-5` output **32768** vs the canonical 131072; `minimax-m3` **512000/131072**). "Fill, don't overwrite": a provider that *did* report a limit keeps it.
- **Modalities**: widened to the models.dev set as a strict **superset** of what the endpoint reported (add, never drop) — so `minimax-m3`/`kimi`/`qwen*-plus` are correctly image/video-capable; `vision` is kept consistent.
- **Family** when the entry has none, and the bare **`reasoning: true`** flag (independent of a control ladder), so a model the feed marks reasoning-capable is not flattened to `supported: false`. Where models.dev publishes `reasoning_options`, the typed control is stamped too (`deepseek-v4-*` → `levels [high, max]`, `minimax-m3` → `on_off`); the rest are `{supported: true}` and snap against the adapter floor.

The **override** (`resources/models/opencode-go.overrides.json`) therefore carries ONLY what neither the endpoint nor models.dev provides — no hand-guessed numbers:

- **The per-model wire `protocol`** (`metadata.opencode_go.protocol: anthropic|openai`) — a vBot-internal routing fact models.dev does not express. (models.dev *hints* it via `provider.npm: @ai-sdk/anthropic`, which matches every model's protocol — kept as an explicit override for safety, since a wrong protocol breaks the request.)
- **`hy3-preview`**: a `canonical` pointer to `tencent/hy3-preview`, because the opencode-go models.dev section has **no limit block** for it; the canonical base fills `context_window`/`max_output_tokens` at load (the at-load merge ignores the provider layer's `null`, so the canonical window flows through).

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
