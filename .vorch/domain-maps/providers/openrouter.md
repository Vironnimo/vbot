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
