# OpenRouter Provider

OpenAI-compatible provider with OpenRouter-specific reasoning and catalog normalization.

## Interfaces

- Provider config: `resources/providers/openrouter.json`
- Adapter selector: `openrouter`
- Adapter class: `OpenRouterAdapter`
- Runtime endpoint: OpenAI-compatible `/chat/completions`

## Reasoning

- Non-`none` vBot `thinking_effort` values map to OpenRouter-supported efforts from `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`.
- vBot `max` maps to `xhigh`.
- Runtime sends `reasoning: { effort }` plus `include_reasoning: true` when reasoning is active.

## Catalog Normalization

- Reads OpenRouter `/models` fields such as `architecture.input_modalities`, `supported_parameters`, `context_length`, and `top_provider.max_completion_tokens`.
- Capability facts discovered from the models endpoint should live in normalization/runtime logic, not model overrides.

## Constraints & Gotchas

- Streaming usage is inherited from the generic OpenAI-compatible behavior.
- OpenRouter supports many upstream providers; do not infer exact model behavior from canonical model family names without catalog or probe evidence.
