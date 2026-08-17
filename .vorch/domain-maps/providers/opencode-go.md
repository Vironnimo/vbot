# OpenCode Go Provider

OpenAI-compatible gateway with per-Model protocol/request profiles plus a small set of Anthropic-routed Models.

## Interfaces

- Provider config: `resources/providers/opencode-go.json`
- Adapter selector: `opencode_go`
- Adapter class: `OpenCodeGoAdapter`
- Default runtime endpoint: OpenAI-compatible `POST /chat/completions`
- Alternate runtime endpoint for selected models: `POST /messages` through an internal `AnthropicCompatibleAdapter`

## Runtime Behavior

- OpenAI-routed Assistant messages with non-empty visible `reasoning` normally echo it as `reasoning_content`; GLM-5.3 uses its live-verified content-based replay representation described under Reasoning Replay.
- **Protocol routing is DATA, not a hardcoded set.** The adapter routes each model by `metadata.opencode_go.protocol` (`"anthropic"` → internal Messages adapter, `"openai"`/anything else → default OpenAI `/chat/completions`), resolved through injected `model_lookup`. The endpoint returns bare ids with no protocol, so the facts live in `resources/models/opencode-go.overrides.json`. An unprofiled Model takes the safe OpenAI default and logs one warning per process. The OpenAI profile also includes `glm-5.3`, live-verified on 2026-08-17; the remaining profile table was verified on 2026-07-24: openai → `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5`, `glm-5.1`, `glm-5.2`, `grok-4.5`, `hy3`, `kimi-k2.5`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, `mimo-v2.5`, `mimo-v2.5-pro`; anthropic → `minimax-m2.5`, `minimax-m2.7`, `minimax-m3`, `qwen3.5-plus`, `qwen3.6-plus`, `qwen3.7-max`, `qwen3.7-plus`.
- The internal Messages wire uses `x-api-key` with no prefix and `anthropic-version: 2023-06-01` while borrowing the outer adapter's HTTP client and sharing the exact token getter, runtime base URL, `model_lookup`, and debug recorder. The inner wire never closes the borrowed client; `OpenCodeGoAdapter` closes the one connection-scoped client exactly once.
- `send`, `stream`, `normalize_response`, and `wire_media_support` all use the same per-Model protocol decision. With `model_id`, response normalization routes by metadata; shape inference (`choices` = OpenAI, otherwise Messages) remains only for legacy callers that omit it. The Messages wire declares image media types only; the OpenAI wire declares images + WAV/MP3, and Chat still intersects that wire set with each Model's actual input modalities. PDF is not declared because OpenCode Go's gateway has not been verified for native PDF, even though the generic Messages encoder can shape a document block.
- When a catalog entry has a positive `max_output_tokens`, OpenCode Go uses it as request `max_tokens` unless the caller supplied `max_tokens`, `max_completion_tokens`, or `max_output_tokens`. The explicit-vs-ceiling logic now lives in the shared `OpenAICompatibleAdapter` base (`_apply_model_output_limit`; see `providers/request-policy.md` → Request limits and context); this Adapter contributes only its flat vendor-prefixed candidate resolution (`_model_max_output_tokens` override + `_model_lookup_candidates`, matching `id`, the `::`-stripped id, and the post-`/` tail) and applies the resolution in `send`/`stream` so the ceiling is stamped before the request splits to the Anthropic sub-route as well.
- Both Tool wires preserve the canonical schema without Provider strict mode. The OpenAI route omits the field; a live `deepseek-v4-flash` request on 2026-07-31 accepted `strict: true`, but acceptance does not establish enforcement and does not change vBot's Provider-wide non-strict invariant. The Messages route also omits the field, and Runtime argument validation remains authoritative on both routes.

## Provider facts come from the models.dev section (rebuilt 2026-07-24)

The opencode-go endpoint returns **bare ids** — no context window, output cap, modalities, family, or reasoning info. But models.dev carries a per-provider **`opencode-go` section** with all of those, so refresh pulls them into the generated `opencode-go.json` (`discovery._enrich_provider_model` via `models_dev.provider_limits` / `provider_modalities` / `provider_family` / `provider_reasoning_supported`):

- **Limits**: the gateway's own `context.window` / `output` — which legitimately deviate from the lab (e.g. `glm-5` output **32768** vs the canonical 131072; `minimax-m3` **1000000/131072**). "Fill, don't overwrite": a provider that *did* report a limit keeps it.
- **Modalities**: widened to the models.dev set as a strict **superset** of what the endpoint reported (add, never drop) — so `minimax-m3`/`kimi`/`qwen*-plus` are correctly image/video-capable; `vision` is kept consistent.
- **Family** when the entry has none, and the bare **`reasoning: true`** flag (independent of a control ladder), so a model the feed marks reasoning-capable is not flattened to `supported: false`. Where models.dev publishes `reasoning_options`, the typed control is stamped too (`deepseek-v4-*` → `levels [high, max]`, `minimax-m3` → `on_off`); the rest are `{supported: true}` and snap against the adapter floor.

The **override** (`resources/models/opencode-go.overrides.json`) carries verified wire facts and corrections that cannot safely be inferred from a Provider-wide rule:

- **Replay hierarchy and per-Model protocol:** the root `reasoning_replay: "full_history"` is the Provider override, an optional top-level Model value can replace it, and `protocol` remains an independent metadata fact. Replay scope never lives inside `metadata.opencode_go`.
- **Response carrier:** `reasoning_response_field` records the live wire (`reasoning_content` for DeepSeek/GLM/Grok/Kimi; `reasoning_details` for Hy3/MiMo). This is repeated in the override because `metadata` is replaced wholesale at load.
- **Kimi controls:** `thinking_control` distinguishes K2.5/K2.6 binary toggles from K2.7 always-on thinking; K2.6 adds `thinking_keep: "all"`. K3 uses `reasoning_effort` with the official `low/high/max` ladder and is always reasoning.
- **Always-reasoning minimum:** Grok 4.5 and Kimi K3 carry `minimum_reasoning_effort: "low"` so an Agent-selected `none` becomes the cheapest valid level instead of omission falling back to the Provider's high/max default.

Since `metadata` is replaced **wholesale** by the highest layer at load (assembly contract), the override's `metadata.opencode_go` becomes the effective metadata.

## Reasoning Replay

- `reasoning_replay_policy()` uses the shared Model → Provider → system hierarchy. The Provider root and explicit GLM-5.2/GLM-5.3 Model records are `full_history`; every other current and future Model inherits the same Provider policy unless a demonstrated compatibility problem earns a narrower top-level Model override. Chat owns exact same-route history shaping.
- Live verification on 2026-07-24 ran the then-effective catalog through a real first request, vBot normalization, Adapter-formatted Assistant replay, and a real second request; Grok/Kimi-specific off/always-on/effort shapes were probed separately. GLM-5.3 received a stronger behavioral probe on 2026-08-17: the gateway returned first-turn Reasoning as `reasoning_content`, but replaying that same field did not expose it to the next sample. Its profile therefore declares `reasoning_request_format: "content_think_and_history"`: the exact text is rendered inside both `<think>` and `<reasoning_history>` before the prior final content. A real second turn returned Model-generated first-turn Reasoning byte-for-byte with matching SHA-256. `<reasoning_history>` is request-only markup: Chat strips a leading copy from new Assistant content on ingest (Models sometimes echo it beside the real reasoning field), and the Adapter strips any leading copy from historical content before re-wrapping so payloads never nest the marker.
- Other OpenAI-routed Assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content`; `reasoning_meta` keys (`reasoning_details`, `encrypted_content`) are applied by the shared OpenAI-compatible formatter.
- Anthropic-routed models render replayed `reasoning_meta.content_blocks` through the inner `AnthropicCompatibleAdapter`, including its thinking-disabled guard.
- Kimi K2.5/K2.6 render `thinking.type` from the Agent's on/off intent and never send generic `reasoning_effort`; K2.6 adds `keep: "all"` only while enabled. K2.7 always renders enabled and suppresses unsupported effort/disable fields. K3 and Grok use `reasoning_effort`, with `none` safely mapped to `low`.

## Catalog Integrity

The gateway's `/models` response is retained verbatim in `opencode-go.raw.json`, but the usable projection applies `ProviderConfig.catalog_exclusions`. Live probes on 2026-07-24 proved that `hy3-preview` is rejected as unsupported and `mimo-v2-omni` / `mimo-v2-pro` return 500 before any replay; those exact ids remain inspectable in raw data but are excluded from `opencode-go.json`. The effective catalog therefore contains 20 live Models. Remove an exclusion only after a new live request proves the gateway serves that exact wire id.

## Prompt Caching

Prompt caching works on **both** routes and is accounted by vBot's adapter path for the models probed on 2026-07-09. OpenCode Go publishes no gateway-level conversation-identity contract; do not infer direct-xAI `x-grok-conv-id` support for Grok through this gateway without a separate probe.

- **OpenAI route** (`/chat/completions`): caching is **automatic upstream** — vBot sends no cache directive. Reads come back as `prompt_tokens_details.cached_tokens` and are read by the shared `_openai_cached_prompt_tokens` (`openai_compatible.py`) into `usage.cache_read_tokens`. Cold miss on the first turn, then near-full-prefix reads while the prefix is reused; a unique prefix per turn stays at ~0 (control). Verified: `deepseek-v4-flash` (0 → 2304 → 8064…), `kimi-k2.6` (cold 3 → 7937). **`glm-5` caches but the gateway reports `prompt_tokens_details` as `null` on many turns** — vBot then honestly reports no read; this is an upstream reporting gap, not a vBot bug (`_openai_cached_prompt_tokens` tolerates the `null`).
- **Anthropic route** (`/messages`): OpenCode Go explicitly enables the compatible wire's `cache_control` breakpoints, and those markers **are effective on this gateway**. With markers on, the cold turn writes the whole prefix (`cache_creation_input_tokens` = full prefix) and later turns read it (`cache_read_input_tokens`); with markers off on a fresh prefix the write disappears and reads shrink or delay — verified on `qwen3.7-plus` (read 8480 with markers vs 6272 without) and `minimax-m2.7`. Some upstreams (`minimax-m3`) auto-cache regardless of markers, so the markers are redundant-but-harmless there. Counts are folded into `cache_read_tokens`/`cache_write_tokens` by `apply_anthropic_cache_usage`.
- **Streaming** surfaces the same reads on the OpenAI route and for `minimax-m2.7`/`minimax-m3`. OpenCode Go's MiniMax M3 Messages stream was observed on 2026-08-14 to report zero Usage at `message_start` and the complete fresh-input, cache-read, output, and Thinking counters only in the terminal `message_delta`; the shared compatible Messages decoder therefore prefers a usable terminal input/cache snapshot over the start snapshot. In an earlier probe `qwen3.7-plus`'s streaming `message_start` did not expose the cache breakdown (a gateway streaming detail), so absent counters remain unknown rather than being reconstructed as measured Provider values.

## Subscription Limits

- OpenCode Go overload/throttling and exhausted subscription/account allowance can both arrive as HTTP 429. Both Wires inspect the structured error detail before the shared status policy: stable limit identifiers (`GoUsageLimitError`, `FreeUsageLimitError`, `insufficient_quota`) and unambiguous monthly-limit, available-balance, quota, budget, or billing-hard-limit phrases raise a non-retryable Provider error immediately. Other 429 responses remain `ProviderRateLimitError`, retain `Retry-After`, and use the shared bounded retry policy.
- Keep this classification OpenCode-specific. A status-only global 429 change would disable valid transient retries for unrelated Providers, while broad error-word matching could misclassify recoverable throttling.

## Constraints & Gotchas

- Keep GLM-5.3's content-based replay and other OpenCode-specific Reasoning mechanics in `OpenCodeGoAdapter`. The generic OpenAI-compatible Adapter owns only its lossless `reasoning_content` fallback. Do not re-add history-wide Reasoning strips here — Chat owns history shaping.
- Keep the compatible Messages owner borrowed and provider-neutral; do not replace it with the concrete native `AnthropicAdapter`, which would import native discovery, sampling, PDF, and cache defaults into OpenCode Go.
- Constructor signature intentionally matches runtime adapter factory injection, including optional `model_lookup` and `debug_recorder`.
