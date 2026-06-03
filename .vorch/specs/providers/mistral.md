# Mistral Provider

OpenAI-style runtime provider with Mistral-specific reasoning and model catalog normalization.

## Interfaces

- Provider config: `resources/providers/mistral.json`
- Adapter selector: `mistral`
- Adapter class: `MistralAdapter`
- Runtime endpoint: `POST /chat/completions`
- Catalog endpoint: `GET /models`

## Reasoning

- Mistral accepts only `reasoning_effort: "high" | "none"`.
- vBot active efforts (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) map to `high`.
- vBot `none` maps to `none`; unset values omit the parameter.
- `model_lookup` suppresses reasoning controls when normalized catalog facts say reasoning is unsupported.
- Pinned connection suffixes such as `::<connection-local-id>` are stripped before catalog lookup.

## Catalog Normalization

- Keeps only active chat models where `capabilities.completion_chat == true` and the model is not archived.
- Maps vision from `capabilities.vision`, tools from `capabilities.function_calling`, and reasoning from `capabilities.reasoning`.
- Persists normalized input/output modalities, supported parameters, and task
  types using chat-oriented defaults for Mistral chat models.
- `context_window` comes from `max_context_length`.
- Per-model max output limits are not provided by `/models`, so normalized
  `max_output_tokens` is `null`. Runtime requests still use provider defaults
  such as `max_tokens: 8192`.

## Constraints & Gotchas

- Bundled config uses `Authorization: Bearer <MISTRAL_API_KEY>` through an API-key connection.
- Non-chat or archived catalog entries are skipped through `CatalogEntrySkipped`.
- Do not infer reasoning support by model-id prefix; use raw Mistral capability fields.
