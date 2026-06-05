# Mistral Provider

OpenAI-style runtime provider with Mistral-specific reasoning and model catalog normalization.

## Interfaces

- Provider config: `resources/providers/mistral.json`
- Adapter selector: `mistral`
- Adapter class: `MistralAdapter`
- Runtime endpoint: `POST /chat/completions`
- Catalog endpoint: `GET /models`
- Credential key: `MISTRAL_API_KEY`

## Reasoning

- Mistral accepts only active high reasoning or disabled reasoning in current vBot wiring. Active vBot efforts (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) map to high; `none` disables reasoning; unset values omit reasoning parameters.
- Most models receive `reasoning_effort: "high"` or `"none"`.
- Models whose id starts with `magistral-medium` use `prompt_mode: "reasoning"` for active reasoning and omit both `prompt_mode` and `reasoning_effort` for `none`.
- Injected `model_lookup` suppresses both `reasoning_effort` and `prompt_mode` when normalized catalog facts say reasoning is unsupported. Pinned connection suffixes such as `::<connection-local-id>` are stripped before catalog lookup by the shared reasoning helper.

## Catalog Normalization

- Keeps only active chat models where `capabilities.completion_chat == true` and the model is not archived; skipped entries raise `CatalogEntrySkipped` for discovery to ignore.
- Maps vision from `capabilities.vision`, tools from `capabilities.function_calling`, reasoning from `capabilities.reasoning`, and audio transcription from `capabilities.audio_transcription`.
- Persists normalized input/output modalities, supported parameters, and chat-oriented task types.
- `context_window` comes from `max_context_length`.
- `/models` does not provide per-model max output limits, so normalized `max_output_tokens` stays `null`; runtime requests still use provider defaults such as `max_tokens: 8192`.

## Response Normalization

- Normal OpenAI-style responses use the generic normalizer.
- If Mistral returns a content-block list, `text` blocks become `content` and `thinking` blocks become visible `reasoning` while usage, tool calls, and reasoning metadata use shared OpenAI-compatible helpers.

## Constraints & Gotchas

- Do not infer reasoning support by model-id prefix; use raw Mistral capability fields or injected catalog facts.
- The `magistral-medium*` `prompt_mode` special case is runtime wire behavior, not a catalog capability.
- Bundled config uses `Authorization: Bearer <MISTRAL_API_KEY>` through an API-key connection.
