# OpenRouter Provider

OpenAI-compatible provider with OpenRouter-specific reasoning and multi-modality catalog normalization.

## Interfaces

- Provider config: `resources/providers/openrouter.json`
- Adapter selector: `openrouter`
- Adapter class: `OpenRouterAdapter`
- Runtime endpoint: OpenAI-compatible `POST /chat/completions`
- Catalog endpoint: `GET /models`

## Reasoning

- Non-empty vBot `thinking_effort` and raw `reasoning_effort` map to OpenRouter efforts from `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`.
- vBot `max` maps to `xhigh`.
- Runtime sends `reasoning: {effort}` plus `include_reasoning: true` when reasoning is active.
- If injected `model_lookup` says reasoning is unsupported, `reasoning`, `include_reasoning`, and generic `reasoning_effort` controls are stripped.

## Catalog Normalization

- Reads OpenRouter `/models` fields such as `architecture.input_modalities`, `architecture.output_modalities`, `architecture.modality`, `supported_parameters`, `context_length`, and `top_provider.max_completion_tokens`.
- Reads the top-level `supported_voices` array defensively (defaults to empty when absent or malformed) and normalizes it into `Capabilities.supported_voices` as a sorted tuple of voice-id strings. This field is present on speech-output models (TTS/audio) but may appear empty on non-speech models.
- The default `/models` response only returns text-output models. `OpenRouterAdapter.supplementary_discovery_params()` adds discovery fetches for `output_modalities=transcription`, `speech`, `image`, `audio`, and `video`; discovery merges and deduplicates those models by id. The `video` fetch is what populates the `video_generation` task type, and `audio` covers generic audio-generation models that do not also expose text output.
- If `top_provider.max_completion_tokens` is missing or `null`, normalized `max_output_tokens` stays `null` instead of copying request defaults.
- Normalized capabilities preserve input/output modalities, supported parameters, derived task types, and small runtime metadata under `metadata.openrouter`.

## Constraints & Gotchas

- Streaming usage behavior is inherited from the generic OpenAI-compatible adapter.
- OpenRouter fronts many upstream providers; do not infer exact model behavior from canonical model family names without catalog facts or probe evidence.
- Capability facts discovered from `/models` belong in normalization/runtime logic, not hand-edited model overrides.
