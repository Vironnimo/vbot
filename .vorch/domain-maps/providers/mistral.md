# Mistral Provider

OpenAI-style runtime provider with Mistral-specific reasoning and model catalog normalization.

## Interfaces

- Provider config: `resources/providers/mistral.json`
- Wire profile: `resources/wire/mistral.json` (reasoning ladder and off spelling, Tool-call ids, replay fidelity)
- Adapter selector: `mistral`
- Adapter class: `MistralAdapter`
- Runtime endpoint: `POST /chat/completions`
- Catalog endpoint: `GET /models`
- Credential key: `MISTRAL_API_KEY`

## Reasoning

- Mistral accepts only active high reasoning or disabled reasoning in current vBot wiring. Active vBot efforts (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) map to high; `none` disables reasoning; unset values omit reasoning parameters.
- Every Model receives `reasoning_effort: "high"` or `"none"`. The wire profile's explicit `levels: ["none", "high"]` beat any catalog ladder, and `off: "none"` sends the explicit off even to Models whose reasoning support is unknown. `/status` describes the same plan (`high` / `off`).
- A Model the catalog marks as non-reasoning gets no reasoning field. Pinned connection suffixes such as `::<connection-local-id>` are stripped before catalog lookup by the profile resolver.
- Tool-call ids are rewritten to Mistral's nine-alphanumeric shape by the shared Chat Completions codec (`request.tool_call_ids: "mistral"`), before the output-limit estimate; canonical history stays untouched.
- Reasoning replay inherits the shared `full_history` default; a non-reasoning Model simply has no native Reasoning to return. Fidelity is the wire profile's `meta_only`: Mistral's docs require the full Assistant message including the thinking trace across turns, so current responses persist the exact original structured content chunks in `reasoning_meta.content_chunks` and replay them verbatim - never a top-level readable field. A Mistral `ThinkChunk` carries more structure than flattened text, so readable `reasoning` is never rebuilt into one: a turn without captured chunks (Sessions from before chunk capture) replays its visible content only. Once a stream enters structured-content mode, generic `delta.tool_calls` fragments are still normalized independently, so mixed thinking/text/Tool streams retain their Tool slots and finish as `tool_calls`. **No thinking-disabled guard is needed** (unlike Anthropic). Probe-verified against the live API (2026-06-13, `mistral-small-latest`): the raw response replay, a reconstructed `[ThinkChunk, TextChunk]` replay, and that same reconstructed replay sent with `reasoning_effort: "none"` all returned 200.

## Catalog Normalization

- Keeps active (not archived) chat models where `capabilities.completion_chat == true`, plus non-chat `*-embed` ids (`mistral-embed`, `codestral-embed`), which become text embedding Models because the catalog has no embedding capability flag. Other entries raise `CatalogEntrySkipped` for discovery to ignore.
- Maps vision from `capabilities.vision`, tools from `capabilities.function_calling`, reasoning from `capabilities.reasoning`, and audio transcription from `capabilities.audio_transcription`.
- Persists normalized input/output modalities, supported parameters, and task types (chat-oriented, or only `text_embedding` for embedding Models).
- `context_window` comes from `max_context_length`.
- `/models` does not provide per-model max output limits, so normalized `max_output_tokens` stays `null`; runtime requests still use provider defaults such as `max_tokens: 8192`.

## Response Normalization

- Normal OpenAI-style responses use the generic normalizer.
- If Mistral returns a content-block list, `text` blocks become `content` and `thinking` blocks become visible `reasoning` while usage, tool calls, and reasoning metadata use shared OpenAI-compatible helpers.
- A `thinking` block's payload is itself a chunk list on current reasoning models (`{"type": "thinking", "thinking": [{"type": "text", "text": ...}], "closed": true}`); `_flatten_thinking` flattens both that nested form and the older magistral plain-string form to reasoning text. The earlier string-only parse silently dropped reasoning from the current models.

## Prompt Caching

- **Automatic upstream - no lever, no marker.** la Plateforme prompt-caches on its own; vBot sends no cache directive and needs none. Reads come back as `usage.prompt_tokens_details.cached_tokens` and are folded into `usage.cache_read_tokens` by the shared `_openai_cached_prompt_tokens` (`openai_compatible.py`). There is no conversation-identity header (no Codex-style trap): the cache keys off prompt-prefix content + the API key.
- **Verified live 2026-07-09** (`open-mistral-nemo`, stable ~13.5k-token prefix, 5 spaced turns, real `MistralAdapter.stream`): cold miss on turn 1, then `cached_tokens = 13504` on **3/4** later turns (one turn missed - the automatic cache is best-effort, warms/evicts server-side; not a vBot gap). No `cache_control` markers are sent or needed.

## Constraints & Gotchas

- Do not infer reasoning support by model-id prefix; use raw Mistral capability fields or injected catalog reasoning facts.
- **magistral generation is deprecated** (docs state 2026-06): `magistral-small-latest`/`magistral-medium-latest` are superseded by `mistral-small-latest` and `mistral-medium-3-5`, which take `reasoning_effort` (`"high"`/`"none"`). The former `prompt_mode: "reasoning"` wire for magistral-medium was removed with its override entries once the catalog stopped publishing those Models; vBot never sends `prompt_mode`.
- Bundled config uses `Authorization: Bearer <MISTRAL_API_KEY>` through an API-key connection.
