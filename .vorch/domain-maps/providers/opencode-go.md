# OpenCode Go Provider

OpenAI-compatible gateway with per-Model protocol and reasoning profiles plus a small set of Anthropic-routed Models.

## Interfaces

- Provider config: `resources/providers/opencode-go.json`
- Adapter selector: `opencode_go`
- Adapter class: `OpenCodeGoAdapter`
- Default runtime endpoint: OpenAI-compatible `POST /chat/completions`
- Alternate runtime endpoint for selected models: `POST /messages` through an internal `AnthropicCompatibleAdapter`

## Runtime Behavior

- OpenAI-routed assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content`.
- **Protocol routing is DATA, not a hardcoded set.** The adapter routes each model by `metadata.opencode_go.protocol` (`"anthropic"` → internal Messages adapter, `"openai"`/anything else → default OpenAI `/chat/completions`), resolved through injected `model_lookup`. The endpoint returns bare ids with no protocol, so the facts live in `resources/models/opencode-go.overrides.json`. An unprofiled Model takes the safe OpenAI default and logs one warning per process. Verified profile table (2026-07-24): openai → `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5`, `glm-5.1`, `glm-5.2`, `grok-4.5`, `hy3`, `kimi-k2.5`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, `mimo-v2.5`, `mimo-v2.5-pro`; anthropic → `minimax-m2.5`, `minimax-m2.7`, `minimax-m3`, `qwen3.5-plus`, `qwen3.6-plus`, `qwen3.7-max`, `qwen3.7-plus`.
- The internal Messages wire uses `x-api-key` with no prefix and `anthropic-version: 2023-06-01` while borrowing the outer adapter's HTTP client and sharing the exact token getter, runtime base URL, `model_lookup`, and debug recorder. The inner wire never closes the borrowed client; `OpenCodeGoAdapter` closes the one connection-scoped client exactly once.
- `send`, `stream`, `normalize_response`, and `wire_media_support` all use the same per-Model protocol decision. With `model_id`, response normalization routes by metadata; shape inference (`choices` = OpenAI, otherwise Messages) remains only for legacy callers that omit it. The Messages wire declares image media types only; the OpenAI wire declares images + WAV/MP3, and Chat still intersects that wire set with each Model's actual input modalities. PDF is not declared because OpenCode Go's gateway has not been verified for native PDF, even though the generic Messages encoder can shape a document block.
- When a catalog entry has a positive `max_output_tokens`, OpenCode Go uses it as request `max_tokens` unless the caller supplied `max_tokens`, `max_completion_tokens`, or `max_output_tokens`. The explicit-vs-ceiling logic now lives in the shared `OpenAICompatibleAdapter` base (`_apply_model_output_limit`; see `providers/request-policy.md` → Request limits and context); this Adapter contributes only its flat vendor-prefixed candidate resolution (`_model_max_output_tokens` override + `_model_lookup_candidates`, matching `id`, the `::`-stripped id, and the post-`/` tail) and applies the resolution in `send`/`stream` so the ceiling is stamped before the request splits to the Anthropic sub-route as well.
- Both Tool wires preserve the canonical schema without Provider strict mode. The OpenAI route omits the field because a live `deepseek-v4-flash` request on 2026-07-28 rejected `strict: true` with HTTP 400; the Messages route also omits it under vBot's Provider-wide non-strict invariant. Runtime argument validation remains authoritative on both routes.

## Provider facts come from the models.dev section (rebuilt 2026-07-24)

The opencode-go endpoint returns **bare ids** — no context window, output cap, modalities, family, or reasoning info. But models.dev carries a per-provider **`opencode-go` section** with all of those, so refresh pulls them into the generated `opencode-go.json` (`discovery._enrich_provider_model` via `models_dev.provider_limits` / `provider_modalities` / `provider_family` / `provider_reasoning_supported`):

- **Limits**: the gateway's own `context.window` / `output` — which legitimately deviate from the lab (e.g. `glm-5` output **32768** vs the canonical 131072; `minimax-m3` **1000000/131072**). "Fill, don't overwrite": a provider that *did* report a limit keeps it.
- **Modalities**: widened to the models.dev set as a strict **superset** of what the endpoint reported (add, never drop) — so `minimax-m3`/`kimi`/`qwen*-plus` are correctly image/video-capable; `vision` is kept consistent.
- **Family** when the entry has none, and the bare **`reasoning: true`** flag (independent of a control ladder), so a model the feed marks reasoning-capable is not flattened to `supported: false`. Where models.dev publishes `reasoning_options`, the typed control is stamped too (`deepseek-v4-*` → `levels [high, max]`, `minimax-m3` → `on_off`); the rest are `{supported: true}` and snap against the adapter floor.

The **override** (`resources/models/opencode-go.overrides.json`) carries verified wire facts and corrections that cannot safely be inferred from a Provider-wide rule:

- **Per-Model protocol and replay:** `protocol` and `reasoning_replay` are independent. Endpoint compatibility does not prove cross-Run reasoning use.
- **Response carrier:** `reasoning_response_field` records the live wire (`reasoning_content` for DeepSeek/GLM/Grok/Kimi; `reasoning_details` for Hy3/MiMo). This is repeated in the override because `metadata` is replaced wholesale at load.
- **Kimi controls:** `thinking_control` distinguishes K2.5/K2.6 binary toggles from K2.7 always-on thinking; K2.6 adds `thinking_keep: "all"`. K3 uses `reasoning_effort` with the official `low/high/max` ladder and is always reasoning.
- **Always-reasoning minimum:** Grok 4.5 and Kimi K3 carry `minimum_reasoning_effort: "low"` so an Agent-selected `none` becomes the cheapest valid level instead of omission falling back to the Provider's high/max default.

Since `metadata` is replaced **wholesale** by the highest layer at load (assembly contract), the override's `metadata.opencode_go` becomes the effective metadata.

## Reasoning Replay

- `reasoning_replay_policy()` reads `metadata.opencode_go.reasoning_replay` directly; protocol never implies replay scope. Unknown/malformed profiles remain `current_run`. Chat owns exact same-Model history shaping.
- Full-history Models: DeepSeek V4, GLM, Grok 4.5, Hy3, Kimi K2.6, Kimi K3, MiMo V2.5, MiniMax, and Qwen. Kimi K2.5 and K2.7 Code remain `current_run`: their official contracts require reasoning replay within a multi-step Tool loop, but do not establish cross-Run Session use.
- Live verification on 2026-07-24 ran all 20 effective Models through a real first request, vBot normalization, Adapter-formatted Assistant replay, and a real second request; all 20 passed. Grok/Kimi-specific off/always-on/effort shapes were probed separately. Acceptance verifies wire validity; the replay scopes above still follow upstream semantics rather than treating HTTP 200 as proof that a Model consumed historical reasoning.
- OpenAI-routed assistant messages with non-empty visible `reasoning` are echoed on the wire as `reasoning_content` (the gateway expects round-tripping); `reasoning_meta` keys (`reasoning_details`, `encrypted_content`) are applied by the shared OpenAI-compatible formatter.
- Anthropic-routed models render replayed `reasoning_meta.content_blocks` through the inner `AnthropicCompatibleAdapter`, including its thinking-disabled guard.
- Kimi K2.5/K2.6 render `thinking.type` from the Agent's on/off intent and never send generic `reasoning_effort`; K2.6 adds `keep: "all"` only while enabled. K2.7 always renders enabled and suppresses unsupported effort/disable fields. K3 and Grok use `reasoning_effort`, with `none` safely mapped to `low`.

## Catalog Integrity

The gateway's `/models` response is retained verbatim in `opencode-go.raw.json`, but the usable projection applies `ProviderConfig.catalog_exclusions`. Live probes on 2026-07-24 proved that `hy3-preview` is rejected as unsupported and `mimo-v2-omni` / `mimo-v2-pro` return 500 before any replay; those exact ids remain inspectable in raw data but are excluded from `opencode-go.json`. The effective catalog therefore contains 20 live Models. Remove an exclusion only after a new live request proves the gateway serves that exact wire id.

## Prompt Caching

Prompt caching works on **both** routes and is accounted by vBot's adapter path for the models probed on 2026-07-09. OpenCode Go publishes no gateway-level conversation-identity contract; do not infer direct-xAI `x-grok-conv-id` support for Grok through this gateway without a separate probe.

- **OpenAI route** (`/chat/completions`): caching is **automatic upstream** — vBot sends no cache directive. Reads come back as `prompt_tokens_details.cached_tokens` and are read by the shared `_openai_cached_prompt_tokens` (`openai_compatible.py`) into `usage.cache_read_tokens`. Cold miss on the first turn, then near-full-prefix reads while the prefix is reused; a unique prefix per turn stays at ~0 (control). Verified: `deepseek-v4-flash` (0 → 2304 → 8064…), `kimi-k2.6` (cold 3 → 7937). **`glm-5` caches but the gateway reports `prompt_tokens_details` as `null` on many turns** — vBot then honestly reports no read; this is an upstream reporting gap, not a vBot bug (`_openai_cached_prompt_tokens` tolerates the `null`).
- **Anthropic route** (`/messages`): OpenCode Go explicitly enables the compatible wire's `cache_control` breakpoints, and those markers **are effective on this gateway**. With markers on, the cold turn writes the whole prefix (`cache_creation_input_tokens` = full prefix) and later turns read it (`cache_read_input_tokens`); with markers off on a fresh prefix the write disappears and reads shrink or delay — verified on `qwen3.7-plus` (read 8480 with markers vs 6272 without) and `minimax-m2.7`. Some upstreams (`minimax-m3`) auto-cache regardless of markers, so the markers are redundant-but-harmless there. Counts are folded into `cache_read_tokens`/`cache_write_tokens` by `apply_anthropic_cache_usage`.
- **Streaming** surfaces the same reads on the OpenAI route and for `minimax-m2.7`/`minimax-m3`; in the probe `qwen3.7-plus`'s streaming `message_start` did not expose the cache breakdown (a gateway streaming detail), so treat the non-streaming usage as authoritative when reconciling per-provider numbers.

## Subscription Limits

- OpenCode Go overload/throttling and exhausted subscription/account allowance can both arrive as HTTP 429. Both Wires inspect the structured error detail before the shared status policy: stable limit identifiers (`GoUsageLimitError`, `FreeUsageLimitError`, `insufficient_quota`) and unambiguous monthly-limit, available-balance, quota, budget, or billing-hard-limit phrases raise a non-retryable Provider error immediately. Other 429 responses remain `ProviderRateLimitError`, retain `Retry-After`, and use the shared bounded retry policy.
- Keep this classification OpenCode-specific. A status-only global 429 change would disable valid transient retries for unrelated Providers, while broad error-word matching could misclassify recoverable throttling.

## Constraints & Gotchas

- Keep provider-specific reasoning wire behavior (the `reasoning_content` echo) in `OpenCodeGoAdapter`; do not add it to the generic OpenAI-compatible adapter. Do not re-add history-wide reasoning strips here — the chat layer owns history shaping.
- Keep the compatible Messages owner borrowed and provider-neutral; do not replace it with the concrete native `AnthropicAdapter`, which would import native discovery, sampling, PDF, and cache defaults into OpenCode Go.
- Constructor signature intentionally matches runtime adapter factory injection, including optional `model_lookup` and `debug_recorder`.
