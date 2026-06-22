# Anthropic Provider

Anthropic Messages API adapter and Anthropic-style request/response normalization.

## Interfaces

- Provider config: `resources/providers/anthropic.json`
- Adapter selector: `anthropic`
- Adapter class: `AnthropicAdapter`
- Runtime endpoint: `POST /messages`
- Auth/header shape: usually `x-api-key` with no Bearer prefix, plus `anthropic-version: 2023-06-01` and configured extra headers.
- Catalog discovery: `GET /models` (provider `models_endpoint: /models`), registered in `discovery._DISCOVERY_ADAPTER_MAP` like every other provider — a Model-DB refresh fetches Anthropic too. `discovery_headers` adds `anthropic-version`; `discovery_params` pages at `limit=1000`; `normalize_catalog_entry` maps the live capability tree (see Catalog Discovery).

## Catalog Discovery

The live `/models` listing is authoritative and rich — each entry carries `max_input_tokens` (context window), `max_tokens` (output limit), and a `capabilities` tree (`image_input`, `pdf_input`, `structured_outputs`, `thinking.{supported, types.{adaptive,enabled}}`, and an `effort` ladder). `normalize_catalog_entry` reads it directly; provider request defaults are not model facts and are ignored.

- **Reasoning control is derived from the live caps** (`_anthropic_reasoning_control`): `adaptive` supported → `levels` (effort ladder = the supported `effort` levels, rendered as adaptive thinking + `output_config.effort`); else native `enabled` thinking → `budget` (native `thinking.budget_tokens`); supported-but-neither → no control (snaps against the adapter floor). This is what the adapter's render needs, not the lab's labelling — a model that exposes an effort ladder but **not** adaptive (Opus 4.5) maps to `budget`, because sending adaptive thinking there is a 400.
- **The canonical (models.dev) layer wins at load.** Refresh writes a `canonical` auto-pointer per Claude, so the assembled reasoning is inherited from `models.json` (currently accurate for the lineup). The normalizer's own derivation is the offline-refresh fallback (no catalog) and is correct on its own — except where canonical disagrees with the adapter render (Opus 4.5, see Sampling & the override).
- `tools=True` is a provider-wide constant (the listing has no per-model tool flag; every Claude supports tool use). `json_mode` = `structured_outputs`. `input_modalities` = `text` + `image`/`pdf` per the caps.
- `metadata.anthropic.supports_temperature` is stamped per model (see Sampling).

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
- `budget_max` is left `None` for Anthropic (the feed publishes none), so a `budget` intent derives its token budget via the absolute fallback ladder clamped under `max_tokens`. Verified live 2026-06-22: native `enabled` budget thinking is accepted on the budget-control Claudes (4.6 and earlier) and **rejected** (400) on the adaptive-only ones (Opus 4.7+, Fable 5), which is exactly the `levels`-vs-`budget` split the catalog encodes.
- **Opus 4.5 override.** `resources/models/anthropic.overrides.json` pins `claude-opus-4-5-20251101` to `control: budget`. The model exposes an effort ladder (so models.dev/canonical label it `levels`) but does **not** support adaptive thinking — the `levels` render (`thinking: {type: adaptive}`) 400s there (verified live). The override is the one durable correction needed; everything else inherits correct reasoning from canonical. It carries no `budget_max` (none to seed).
- If injected `model_lookup` says reasoning is unsupported, Anthropic thinking/reasoning controls are stripped.

## Sampling parameters (temperature/top_p/top_k)

- Anthropic removed sampling on the adaptive-only generation (Opus 4.7+, Fable 5): `temperature` (and `top_p`/`top_k`) return a 400 there, regardless of thinking. Every model that still offers native `enabled` thinking (4.6 and earlier) keeps sampling. `normalize_catalog_entry` derives this from the live caps and stamps `metadata.anthropic.supports_temperature` (`= enabled` supported); `_anthropic_supports_temperature` is the helper.
- `_build_payload` drops all sampling params (caller value **and** provider default) when **either** thinking is active **or** the model's `supports_temperature` flag is `False` (`_model_supports_temperature` reads the metadata; an absent lookup/flag defaults to "keep"). This makes the flagship adaptive-only models work with no effort selected — the prior provider-level `temperature` default broke them.
- The provider config no longer carries a `temperature` default (it only breaks the adaptive-only models and Anthropic's own default is fine for the rest).
- **Replay policy:** `reasoning_replay_policy` returns `full_history` — persisted `reasoning`/`reasoning_meta` replay across runs for assistant entries that pass the chat layer's same-model gate (Anthropic guidance: thinking blocks go back unchanged for the whole same-model conversation; stripping risks signature/ordering 400s and provider-side prompt-cache misses). Cross-model entries are stripped by the gate; same-model reasoning-only turns stay in the request history.
- Opaque `thinking` and `redacted_thinking` blocks from provider responses are preserved under `reasoning_meta.content_blocks` and are resent for the active tool-use continuation and, via `full_history`, for prior same-model runs.
- **Thinking-disabled guard:** when the outgoing request explicitly disables thinking (`thinking: {type: disabled}`, e.g. from `thinking_effort: none`) or the catalog marks the model reasoning-unsupported, `_build_payload` strips replayed `reasoning_meta` thinking blocks; an assistant turn left without content blocks is dropped from the request (the wire rejects empty content arrays). An absent thinking parameter does **not** strip — omitting blocks is the risk, the server drops unusable ones.
- Live probe of API tolerance for replayed thinking blocks under explicitly disabled thinking was **not re-performed** in the 2026-06-22 provider round (it is a niche replay edge, separate from the catalog/reasoning/sampling work). The conservative guard above stands; Anthropic credentials now exist, so it can be verified if it ever bites (see FLAGGED.md).
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
