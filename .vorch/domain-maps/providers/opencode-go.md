# OpenCode Go Provider

OpenAI-compatible gateway with full-history reasoning replay and a small set of Anthropic-routed models.

## Interfaces

- Provider config: `resources/providers/opencode-go.json`
- Adapter selector: `opencode_go`
- Adapter class: `OpenCodeGoAdapter`
- Default runtime endpoint: OpenAI-compatible `POST /chat/completions`
- Alternate runtime endpoint for selected models: `POST /messages` through an internal `AnthropicCompatibleAdapter`

## Runtime Behavior

- OpenAI-routed assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content`.
- **Protocol routing is DATA, not a hardcoded set.** The adapter routes each model by the per-model wire fact `metadata.opencode_go.protocol` (`"anthropic"` → internal Messages adapter, `"openai"`/anything-else → default OpenAI `/chat/completions`), resolved via the injected `model_lookup`. The endpoint returns bare ids with no protocol, so the facts live in the opencode-go **override** (`resources/models/opencode-go.overrides.json`), keyed by wire-id under `metadata.opencode_go.protocol`. A model the override does not mark (no metadata / no `protocol`) is **unknown**: it takes the safe OpenAI default AND the adapter logs a `warn` (`vbot.providers.opencode_go`) so a newly added model is never silently misrouted. Published protocol table (the override's source of truth): openai → `glm-5.1, glm-5, kimi-k2.7, kimi-k2.6, deepseek-v4-pro, deepseek-v4-flash, mimo-v2.5, mimo-v2.5-pro`; anthropic → `minimax-m3, minimax-m2.7, minimax-m2.5, qwen3.7-max, qwen3.7-plus, qwen3.6-plus`.
- The internal Messages wire uses `x-api-key` with no prefix and `anthropic-version: 2023-06-01` while borrowing the outer adapter's HTTP client and sharing the exact token getter, runtime base URL, `model_lookup`, and debug recorder. The inner wire never closes the borrowed client; `OpenCodeGoAdapter` closes the one connection-scoped client exactly once.
- `send`, `stream`, `normalize_response`, and `wire_media_support` all use the same per-Model protocol decision. With `model_id`, response normalization routes by metadata; shape inference (`choices` = OpenAI, otherwise Messages) remains only for legacy callers that omit it. The Messages wire declares image media types only; the OpenAI wire declares images + WAV/MP3, and Chat still intersects that wire set with each Model's actual input modalities. PDF is not declared because OpenCode Go's gateway has not been verified for native PDF, even though the generic Messages encoder can shape a document block.
- When a catalog entry has a positive `max_output_tokens`, OpenCode Go uses it as request `max_tokens` unless the caller supplied `max_tokens`, `max_completion_tokens`, or `max_output_tokens`. The explicit-vs-ceiling logic now lives in the shared `OpenAICompatibleAdapter` base (`_apply_model_output_limit`; see `providers/request-policy.md` → Request limits and context); this Adapter contributes only its flat vendor-prefixed candidate resolution (`_model_max_output_tokens` override + `_model_lookup_candidates`, matching `id`, the `::`-stripped id, and the post-`/` tail) and applies the resolution in `send`/`stream` so the ceiling is stamped before the request splits to the Anthropic sub-route as well.

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
- Anthropic-routed models render replayed `reasoning_meta.content_blocks` through the inner `AnthropicCompatibleAdapter`, including its thinking-disabled guard.

## Prompt Caching

Prompt caching works on **both** routes and is already correctly accounted by vBot's own adapter path — no opencode-go-specific lever, and no conversation-identity header (no Codex-style trap: the cache keys off prompt-prefix content + the API key, not a session id). Live-verified against the real gateway 2026-07-09 across 6 spaced turns per model, with a control run (see the harness note below).

- **OpenAI route** (`/chat/completions`): caching is **automatic upstream** — vBot sends no cache directive. Reads come back as `prompt_tokens_details.cached_tokens` and are read by the shared `_openai_cached_prompt_tokens` (`openai_compatible.py`) into `usage.cache_read_tokens`. Cold miss on the first turn, then near-full-prefix reads while the prefix is reused; a unique prefix per turn stays at ~0 (control). Verified: `deepseek-v4-flash` (0 → 2304 → 8064…), `kimi-k2.6` (cold 3 → 7937). **`glm-5` caches but the gateway reports `prompt_tokens_details` as `null` on many turns** — vBot then honestly reports no read; this is an upstream reporting gap, not a vBot bug (`_openai_cached_prompt_tokens` tolerates the `null`).
- **Anthropic route** (`/messages`): OpenCode Go explicitly enables the compatible wire's `cache_control` breakpoints, and those markers **are effective on this gateway**. With markers on, the cold turn writes the whole prefix (`cache_creation_input_tokens` = full prefix) and later turns read it (`cache_read_input_tokens`); with markers off on a fresh prefix the write disappears and reads shrink or delay — verified on `qwen3.7-plus` (read 8480 with markers vs 6272 without) and `minimax-m2.7`. Some upstreams (`minimax-m3`) auto-cache regardless of markers, so the markers are redundant-but-harmless there. Counts are folded into `cache_read_tokens`/`cache_write_tokens` by `apply_anthropic_cache_usage`.
- **Streaming** surfaces the same reads on the OpenAI route and for `minimax-m2.7`/`minimax-m3`; in the probe `qwen3.7-plus`'s streaming `message_start` did not expose the cache breakdown (a gateway streaming detail), so treat the non-streaming usage as authoritative when reconciling per-provider numbers.

## Constraints & Gotchas

- Keep provider-specific reasoning wire behavior (the `reasoning_content` echo) in `OpenCodeGoAdapter`; do not add it to the generic OpenAI-compatible adapter. Do not re-add history-wide reasoning strips here — the chat layer owns history shaping.
- Keep the compatible Messages owner borrowed and provider-neutral; do not replace it with the concrete native `AnthropicAdapter`, which would import native discovery, sampling, PDF, and cache defaults into OpenCode Go.
- Constructor signature intentionally matches runtime adapter factory injection, including optional `model_lookup` and `debug_recorder`.
