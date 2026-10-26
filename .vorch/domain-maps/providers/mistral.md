# Mistral Provider

OpenAI-style runtime Provider with Mistral-specific catalog normalization and wire-profile controls. Typed response content is handled by the shared Chat Completions codec, including when Mistral is served through a gateway.

## Interfaces

- Provider config: `resources/providers/mistral.json`
- Wire profile: `resources/wire/mistral.json` (reasoning ladder and off spelling, Tool-call ids, replay fidelity)
- Adapter selector: `mistral`
- Adapter class: `MistralAdapter`
- Runtime endpoint: `POST /chat/completions`
- Catalog endpoint: `GET /models`
- Speech endpoints: `POST /audio/transcriptions`, `POST /audio/speech` (the configured `/v1` base); preset voices: `GET /v2/audio/voices`
- Credential key: `MISTRAL_API_KEY`

## Reasoning

- Mistral's own Models default to binary reasoning: active vBot efforts (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) map to high; `none` sends the explicit off even when support is unknown; unset values omit reasoning parameters. The wire uses a fallback `floor: ["none", "high"]`, so a new Model's exact catalog ladder wins instead of being collapsed to the older binary contract. `/status` describes the same resolved plan.
- Hosted `zai-glm-` Models infer their own `low|high|max` floor and map off to the lowest effort. This follows Mistral's [official reasoning reference](https://docs.mistral.ai/studio/conversations/reasoning), read 2026-10-15, which documents that distinct contract for `zai-glm-5-3`. The family default is scoped to Chat Completions and remains inferred; it creates no Model entry or verification record.
- A Model the catalog marks as non-reasoning gets no reasoning field. Pinned connection suffixes such as `::<connection-local-id>` are stripped before catalog lookup by the profile resolver.
- Tool-call ids are rewritten to Mistral's nine-alphanumeric shape by the shared Chat Completions codec (`request.tool_call_ids: "mistral"`), before the output-limit estimate; canonical history stays untouched.
- Reasoning replay inherits the shared `full_history` default; a non-reasoning Model simply has no native Reasoning to return. Fidelity is the wire profile's `meta_only`: Mistral's docs require the full Assistant message including the thinking trace across turns, so current responses persist the exact original structured content chunks in `reasoning_meta.content_chunks` and replay them verbatim - never a top-level readable field. A Mistral `ThinkChunk` carries more structure than flattened text, so readable `reasoning` is never rebuilt into one: a turn without captured chunks (Sessions from before chunk capture) replays its visible content only. Once a stream enters structured-content mode, generic `delta.tool_calls` fragments are still normalized independently, so mixed thinking/text/Tool streams retain their Tool slots and finish as `tool_calls`. **No thinking-disabled guard is needed** (unlike Anthropic). Probe-verified against the live API (2026-06-13, `mistral-small-latest`): the raw response replay, a reconstructed `[ThinkChunk, TextChunk]` replay, and that same reconstructed replay sent with `reasoning_effort: "none"` all returned 200.

## Catalog Normalization

- Keeps active (not archived) chat Models where `capabilities.completion_chat == true`, offline speech Models advertised by `audio_transcription` or `audio_speech`, and non-chat `*-embed` ids (`mistral-embed`, `codestral-embed`), which become text embedding Models because the catalog has no embedding capability flag. Speech details with explicit `modes` must include `offline`; realtime-only speech, OCR and moderation remain excluded. Other entries raise `CatalogEntrySkipped` for discovery to ignore.
- Maps vision from `capabilities.vision`, Tools from `capabilities.function_calling`, reasoning from `capabilities.reasoning`, and chat audio input from `capabilities.audio` or `audio_transcription`. Audio chat alone does not advertise the separate `speech_to_text` task.
- Dedicated STT has audio input and transcription output; TTS has text input and speech output. Neither advertises chat, Tools, JSON mode or reasoning. `audio_transcription_capabilities` and `audio_speech_capabilities` supply per-Model language, timestamp, diarization, context, format and instruction choices through `task_options`; missing optional capabilities stay unoffered.
- `discover_task_models` enriches TTS with paginated public preset slugs from `/v2/audio/voices?type=preset`, excluding Account-owned voices. The reported `billing_model_name` identifies aliases of `voxtral-mini-tts-2603`, which cannot use the newer `voice_*` presets. Voice discovery failure leaves the Models usable with free-text voice input and the shared `gb_jane_neutral` default.
- `context_window` comes from `max_context_length`.
- `/models` does not provide per-model max output limits, so normalized `max_output_tokens` stays `null`; runtime requests still use provider defaults such as `max_tokens: 8192`.

## Speech

`ProviderSpeechClient` owns speech execution and uses the common task client for
auth, retries and Model Usage; it does not route speech through the chat Adapter.
See `../model_tasks/speech.md` for preparation, streaming, artifacts and errors.
STT uses native multipart fields, including repeated context terms and timestamp
granularities. Diarization requires exactly segment timestamps, so enabling it
overrides the timestamp choice. TTS uses `voice_id` and returns Base64 `audio_data`
in JSON or `speech.audio.delta` SSE events followed by `speech.audio.done`.
The shared decoder supplies early PCM playback and retains the encoded artifact.

Official contracts read 2026-10-26: [transcriptions](https://docs.mistral.ai/api/endpoint/audio/transcriptions),
[file transcription](https://docs.mistral.ai/studio/audio/speech-to-text/transcribe-files),
[speech](https://docs.mistral.ai/api/endpoint/audio/speech),
[streaming](https://docs.mistral.ai/studio/audio/text-to-speech/stream-speech),
[voices](https://docs.mistral.ai/studio/audio/text-to-speech/voices), and
[Voxtral migration](https://docs.mistral.ai/studio/audio/migrate-from-voxtral-2).
The Voxtral 3 generation is currently documented as public preview.

Live evidence on 2026-10-26, `mistral:api-key`, synthetic English audio/text only:

| Models | Endpoint and shape | Result |
| --- | --- | --- |
| `voxtral-mini-2602`, `voxtral-mini-latest`, `voxtral-mini-transcribe-3-0`, `voxtral-mini-transcribe-3`, `voxtral-small-transcribe-3-0`, `voxtral-small-transcribe-3` | `/v1/audio/transcriptions`, multipart WAV through the vBot upload route | All six selectable and executable; correct transcript. Old Mini accepts word timestamps; both 3.0 families return segment timestamps with speaker labels. |
| `voxtral-mini-2602`, `voxtral-mini-transcribe-3-0`, `voxtral-small-transcribe-3-0` | Raw HTTP and vBot STT; language, repeated context terms, diarization/segment timestamps; vBot FLAC with automatic language | Correct transcripts, segments and usage. WebM/Opus upload also succeeds through vBot conversion on Mini 3.0. |
| `voxtral-mini-tts-2603`, `voxtral-mini-tts-latest`, `voxtral-mini-tts-3-0`, `voxtral-mini-tts-3` | `/v1/audio/speech`, vBot MP3 SSE with the default preset | All four selectable and executable; playback announced before the result, decodable PCM playback and final artifacts. Raw HTTP buffered WAV and MP3 SSE also succeeded on both exact versions. |
| `voxtral-mini-tts-2603`, `voxtral-mini-tts-3-0` | vBot buffered PCM, WAV, FLAC and Opus | All decode successfully. PCM is mono PCM16 at 24 kHz and vBot wraps it in WAV; MP3 playback preserves its reported 22.05 kHz rate. |
| `voxtral-mini-tts-2603`, `voxtral-mini-tts-3-0` | `voice_481` versus `gb_jane_neutral`; public voice listing | Old TTS rejects `voice_481` with HTTP 400; 3.0 accepts it. Both accept `gb_jane_neutral`. Discovery exposes 30 compatible presets on old TTS and 66 on 3.0; other individual voices were not exhaustively exercised. |

Raw STT also rejects diarization with word timestamps, including on the old Mini;
repeated multipart context fields were confirmed in the API's validation response.
These checks establish transport and integration, not transcription quality or
every language/voice combination. Realtime speech, OCR and moderation were not
implemented or verified by this change.

## Response Normalization

- Both string and typed content use the shared Chat Completions normalizer; `MistralAdapter` owns only its catalog specialization.
- If Mistral returns a content-block list, `text` blocks become `content` and `thinking` blocks become visible `reasoning` while usage, tool calls, and reasoning metadata use shared OpenAI-compatible helpers.
- A `thinking` block's payload is itself a chunk list on current reasoning models (`{"type": "thinking", "thinking": [{"type": "text", "text": ...}], "closed": true}`); `_chat_content_chunk_text` in `_chat_completions_wire.py` flattens both that nested form and the older magistral plain-string form to reasoning text. The earlier string-only parse silently dropped reasoning from the current models.

The 2026-10-14 gateway integration moved the existing typed-content handling into the shared codec and fixed mixed streams: after thinking chunks, a plain-string answer is emitted and retained as a text chunk for replay. Tests cover both the direct Adapter and Zen Mistral Large 4. Live evidence for the latter (including full-history input accounting) belongs to `opencode-zen.md`; it does not re-verify the direct API.

## Prompt Caching

- **Automatic upstream - no lever, no marker.** la Plateforme prompt-caches on its own; vBot sends no cache directive and needs none. Reads come back as `usage.prompt_tokens_details.cached_tokens` and are folded into `usage.cache_read_tokens` by the shared `_openai_cached_prompt_tokens` (`_chat_completions_wire.py`). There is no conversation-identity header (no Codex-style trap): the cache keys off prompt-prefix content + the API key.
- **Verified live 2026-07-09** (`open-mistral-nemo`, stable ~13.5k-token prefix, 5 spaced turns, real `MistralAdapter.stream`): cold miss on turn 1, then `cached_tokens = 13504` on **3/4** later turns (one turn missed - the automatic cache is best-effort, warms/evicts server-side; not a vBot gap). No `cache_control` markers are sent or needed.

## Constraints & Gotchas

- Do not infer reasoning support by model-id prefix; use raw Mistral capability fields or injected catalog reasoning facts.
- **magistral generation is deprecated** (docs state 2026-06): `magistral-small-latest`/`magistral-medium-latest` are superseded by `mistral-small-latest` and `mistral-medium-3-5`, which take `reasoning_effort` (`"high"`/`"none"`). The former `prompt_mode: "reasoning"` wire for magistral-medium was removed with its override entries once the catalog stopped publishing those Models; vBot never sends `prompt_mode`.
- Bundled config uses `Authorization: Bearer <MISTRAL_API_KEY>` through an API-key connection.
