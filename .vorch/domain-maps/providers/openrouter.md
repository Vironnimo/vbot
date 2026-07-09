# OpenRouter Provider

OpenAI-compatible provider with OpenRouter-specific reasoning and multi-modality catalog normalization.

## Interfaces

- Provider config: `resources/providers/openrouter.json`
- Adapter selector: `openrouter`
- Adapter class: `OpenRouterAdapter`
- Runtime endpoint: OpenAI-compatible `POST /chat/completions`
- Catalog endpoint: `GET /models`

## Reasoning

- Reasoning is resolved through the shared `resolve_reasoning_intent(...)` (see `providers.md` → "Reasoning is one policy, many renders") and rendered by `_render_openrouter_reasoning`. The effort snaps against the model's feed ladder or the `OPENROUTER_REASONING_EFFORTS` floor (`none`/`minimal`/`low`/`medium`/`high`/`xhigh`; vBot `max` → `xhigh`).
  - **effort** *and* **budget** → `reasoning: {effort}` + `include_reasoning: true`. OpenRouter maps effort→budget internally, so a `budget`-control model deliberately sends an effort here, **never** a token budget (the adapter needs no `budget_max`).
  - **on** (an `on_off`-control model) → `reasoning: {enabled: true}` + `include_reasoning: true`.
  - **off** → the byte-identical `reasoning: {effort: "none"}` for an effort-spelled-off wire (a `levels`/unknown control whose ladder has a `none` rung), else the documented toggle off-shape `reasoning: {enabled: false}` for an `on_off` model. The exact `on_off` off-shape is **not live-verified** (no OpenRouter probe in this environment — see FLAGGED.md).
  - **default** (no effort selected) → no `reasoning` field.
- If injected `model_lookup` says reasoning is unsupported, `reasoning`, `include_reasoning`, and generic `reasoning_effort` controls are stripped.
- Reasoning replay policy: `current_run`, and this is the genuinely correct target (not a deferred placeholder). OpenRouter's [reasoning-tokens docs](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) frame `reasoning`/`reasoning_details` preservation as in-run ("useful specifically for tool calling"); cross-run replay is undocumented. The in-run hard requirements are met — some upstreams 400 without echoed reasoning (Gemini "thought_signature" in `reasoning_details` of the `reasoning.encrypted` type), and `current_run` keeps `reasoning_meta` within the run, round-tripped by `_apply_openai_reasoning_meta` and pinned by a test. Replayed blocks must match the original sequence unmodified (docs: "you cannot rearrange or modify the sequence of these blocks"). **Billing of replayed `reasoning_details` is inferred, not documented** — the docs only state that generation bills as output. Revisit `full_history` only per upstream family (the hook's `model_id` supports a split) and only with probes; the same-model gate already blocks cross-model replay.

## Prompt Caching

- **Claude-family only, envelope layout.** OpenRouter forwards Anthropic's own opt-in caching, but on the OpenAI `/chat/completions` wire, so the `cache_control` marker rides **inside a content part**, not as a native top-level `system` block (that is the Anthropic adapter's shape — see `anthropic.md`). `_build_payload` calls `_apply_openrouter_prompt_caching(payload)` last, only when `_is_claude_family(model_id)` (the `claude` substring — covers `anthropic/claude-*`, the `~…-latest` auto-router slug, dated variants). Non-Claude models are left untouched: OpenAI/Gemini cache implicitly, and a stray `cache_control` key risks a strict-upstream 400.
- **Placement.** One marker on the last **system message** (caches tools + system) plus up to `OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS` (3) rolling markers on the most recent non-system messages, capped at `OPENROUTER_CACHE_BREAKPOINT_LIMIT` (4, Anthropic's limit). Marker is `{"type": "ephemeral"}` (5-minute TTL). A string content is wrapped into a single `text` part to carry it; a list content takes it on the last dict part. A message that cannot carry a marker (empty string, or a pure-tool-call assistant turn with `content: None`) is skipped so a breakpoint is never wasted (`_mark_openrouter_message` returns whether it placed one).
- **Read accounting.** OpenRouter reports cache reads as `usage.prompt_tokens_details.cached_tokens`, which the base OpenAI-compatible adapter already folds into `cache_read_tokens` (non-stream and stream). OpenRouter also carries `prompt_tokens_details.cache_write_tokens`, which vBot does **not** read today (reads are the billed-savings signal; the cold write is not surfaced).
- **Verified live 2026-07-09** (`~anthropic/claude-haiku-latest`, stable ~13k-token prefix, 6 spaced turns, real `OpenRouterAdapter.stream`): **without** the fix `cache_read = 0` on every turn (raw usage `cached_tokens: 0`); **with** the fix `cache_read = 13220` on turns 2–6 after the cold write, and per-request cost dropped ~10× ($0.0134 → $0.0013). This is the gap the fix closed — the OpenAI-wire message builder emits flat string content with no marker, so Claude via OpenRouter cached nothing before.
- **Why Claude-only — verified per family (live 2026-07-09, same probe, no markers).** Every non-Claude family either caches implicitly (nothing for vBot to send) or cannot be helped by a marker, so the marker gate stays Claude-only:
  - **Implicit, works — leave alone:** OpenAI `gpt-5-nano` (3/3 warm hits, `cached_tokens` ~12.5k), xAI `grok-4.20` (2/2), Zhipu `glm-4.7-flash` (2/2). Strong upstream prefix caching; vBot already reads `cached_tokens`.
  - **Implicit, best-effort:** Google `gemini-2.5-flash-lite` and DeepSeek `deepseek-chat` each hit ~1/3 (present but inconsistent — server-side, warms up late). No marker mechanism on the chat wire; not a gap.
  - **No controllable caching — do NOT add markers:** Qwen `qwen3-30b-a3b-instruct-2507` and Moonshot `kimi-k2` return ~0 cache reads. Qwen was retested **with** `cache_control` markers forced on and still returned 0 — OpenRouter routes these to an upstream that does not cache, and the marker cannot change that (matches Hermes, which also skips markers for Qwen-on-OpenRouter). Adding markers here is useless noise; the gate correctly excludes them.

## Catalog Normalization

- Reads OpenRouter `/models` fields such as `architecture.input_modalities`, `architecture.output_modalities`, `architecture.modality`, `supported_parameters`, `context_length`, and `top_provider.max_completion_tokens`.
- Reads the top-level `supported_voices` array defensively (defaults to empty when absent or malformed) and normalizes it into `Capabilities.supported_voices` as a sorted tuple of voice-id strings. This field is present on speech-output models (TTS/audio) but may appear empty on non-speech models.
- The default `/models` response only returns text-output models. `OpenRouterAdapter.supplementary_discovery_params()` adds discovery fetches for `output_modalities=transcription`, `speech`, `image`, `audio`, `video`, and `embeddings`; discovery merges and deduplicates those models by id. The `video` fetch is what populates the `video_generation` task type, `embeddings` populates `text_embedding`, and `audio` covers generic audio-generation models that do not also expose text output.
- If `top_provider.max_completion_tokens` is missing or `null`, normalized `max_output_tokens` stays `null` instead of copying request defaults.
- Normalized capabilities preserve input/output modalities, supported parameters, derived task types, and small runtime metadata under `metadata.openrouter`.

## Constraints & Gotchas

- Streaming usage behavior is inherited from the generic OpenAI-compatible adapter.
- OpenRouter fronts many upstream providers; do not infer exact model behavior from canonical model family names without catalog facts or probe evidence.
- Capability facts discovered from `/models` belong in normalization/runtime logic, not hand-edited model overrides.
