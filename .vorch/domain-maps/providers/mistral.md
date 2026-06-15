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
- Models carrying the per-model wire fact `metadata.mistral.prompt_mode == "reasoning"` use `prompt_mode: "reasoning"` for active reasoning and omit both `prompt_mode` and `reasoning_effort` for `none`. This is DATA, not a name-prefix guess (the `magistral-medium` prefix tuple `MISTRAL_PROMPT_MODE_REASONING_MODEL_PREFIXES` was removed in Phase 5). The fact lives in the **override** (`resources/models/mistral.overrides.json`) for `magistral-medium-2509`/`magistral-medium-latest`. An adapter with no `model_lookup`, or a model with no such metadata, uses the default `reasoning_effort` wire.
- Injected `model_lookup` suppresses both `reasoning_effort` and `prompt_mode` when normalized catalog facts say reasoning is unsupported. Pinned connection suffixes such as `::<connection-local-id>` are stripped before catalog lookup by the shared reasoning helper.
- Reasoning replay policy: `full_history`. Mistral's docs are explicit and cross-turn — always replay the full assistant message including the thinking trace; dropping it degrades output quality. The adapter does **not** persist `reasoning_meta` (Mistral carries no `encrypted_content`/`reasoning_details`); instead `_format_assistant_message` reconstructs the wire ThinkChunk from the persisted visible `reasoning` text, which the chat layer keeps on same-model replayed and in-run assistant turns. **No thinking-disabled guard is needed** (unlike Anthropic). Probe-verified against the live API (2026-06-13, `mistral-small-latest`): the raw response replay, a reconstructed `[ThinkChunk, TextChunk]` replay, and that same reconstructed replay sent with `reasoning_effort: "none"` all returned 200.

## Catalog Normalization

- Keeps only active chat models where `capabilities.completion_chat == true` and the model is not archived; skipped entries raise `CatalogEntrySkipped` for discovery to ignore.
- Maps vision from `capabilities.vision`, tools from `capabilities.function_calling`, reasoning from `capabilities.reasoning`, and audio transcription from `capabilities.audio_transcription`.
- Persists normalized input/output modalities, supported parameters, and chat-oriented task types.
- `context_window` comes from `max_context_length`.
- `/models` does not provide per-model max output limits, so normalized `max_output_tokens` stays `null`; runtime requests still use provider defaults such as `max_tokens: 8192`.

## Response Normalization

- Normal OpenAI-style responses use the generic normalizer.
- If Mistral returns a content-block list, `text` blocks become `content` and `thinking` blocks become visible `reasoning` while usage, tool calls, and reasoning metadata use shared OpenAI-compatible helpers.
- A `thinking` block's payload is itself a chunk list on current reasoning models (`{"type": "thinking", "thinking": [{"type": "text", "text": …}], "closed": true}`); `_flatten_thinking` flattens both that nested form and the older magistral plain-string form to reasoning text. The earlier string-only parse silently dropped reasoning from the current models.

## Constraints & Gotchas

- Do not infer reasoning support or the reasoning mode by model-id prefix; use raw Mistral capability fields, injected catalog reasoning facts, or the `metadata.mistral.prompt_mode` wire fact.
- The `prompt_mode` selector is runtime wire behavior driven by `metadata.mistral.prompt_mode` (Phase 5); the wire MECHANICS (building the `prompt_mode` vs `reasoning_effort` request) stay in the adapter.
- **magistral generation is deprecated** (docs state 2026-06): `magistral-small-latest`/`magistral-medium-latest` are superseded by `mistral-small-latest` and `mistral-medium-3-5`, which take `reasoning_effort` (`"high"`/`"none"`) — the same mapping the adapter already applies to non-magistral models. The `prompt_mode` branch only matters while the deprecated magistral models stay reachable; drop the `mistral.overrides.json` `prompt_mode` entries once the catalog refresh removes them.
- Bundled config uses `Authorization: Bearer <MISTRAL_API_KEY>` through an API-key connection.
